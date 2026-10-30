"""Licenses/SAM reporting (docs/modules/licenses/spec.md §8): read-only
compliance summary, renewal calendar, per-product/currency cost view, and
flat CSV export rows.

Pure read-only aggregation over the Licenses/SAM module's tables. This module
performs **no writes** and records **no audit events** -- it exists solely to
answer "what is our current compliance position" (`compliance_summary`),
"what's coming due" (`renewal_calendar`), "what are we spending, by product
and currency" (`cost_view`), and "give me a flat row set for true-up /
Power BI" (`export_rows`). Mirrors `app.modules.bcp.reporting`'s style and its
no-audit-on-view invariant: viewing a report must never be indistinguishable
from mutating anything, so nothing here ever calls `audit_service`. (Per spec
§8/§9, the CSV *export endpoint* itself IS audited as `license.export` --
that audit call belongs to the API layer that wraps `export_rows`, never to
this pure read function.)

Every query below filters on `org_id` for every table involved (`License`,
`SoftwareProduct`, and -- via `matching.client_titles` --
`InstalledSoftware`) per the tenancy invariant (#5) -- RLS is the backstop,
not the only line of defense.

A license is ONE client's (plan 2026-10-26 Task 6), and nothing here adds
two clients together: compliance is counted per client (X's breach is not
hidden by Y's spare seats), costs are summed per client, and each renewal row
names its license's client. The client is an explicit predicate or grouping
column, never left to RLS, which under scope all narrows nothing.

`export_rows` never includes `key` or `key_sealed` (invariant #3): CSV export
exists for entitlement true-up, not credential export.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, License, SoftwareProduct
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.licenses.matching import client_titles, devices_matching


async def reported_clients(session: AsyncSession, org_id: UUID) -> list[UUID]:
    """The clients a compliance position is computed for: the active ones
    this session sees. "Sin asignar" holds no sites, so no devices and no
    licenses (C10), and has none; an archived client takes nothing new."""
    return list(
        (
            await session.execute(
                select(Client.id)
                .where(
                    Client.org_id == org_id,
                    Client.status == "active",
                    Client.kind != CLIENT_KIND_UNASSIGNED,
                )
                .order_by(Client.id)
            )
        ).scalars()
    )


async def compliance_by_client(
    session: AsyncSession, org_id: UUID, client_ids: Sequence[UUID]
) -> list[dict[str, Any]]:
    """Per-product compliance position of each of `client_ids`, org-scoped,
    computed live: the rows of the first client, then the second's, each in
    catalogue order.

    For each `SoftwareProduct` in the org (the catalogue is shared) and each
    client: `entitled` is the sum of `entitlements` over that product's
    `License` rows of that client (`0` if it has none); `installed` is the
    count of distinct devices of that client with software matching any of
    the product's match rules (`devices_matching`); `delta = entitled -
    installed`; `breach` is `True` when `installed > entitled`. Both sides
    carry the org and the client as explicit predicates.

    THE STATEMENT COUNT DOES NOT GROW WITH THE CATALOGUE (review M1 of plan
    2026-10-26 Task 6): one read of the products, one entitlement query
    grouped by (client, product) for every client at once, and one load of
    each client's installed titles, matched against every product in memory.
    It was two queries per client per product, on a read `/licenses` makes
    on every page load.
    """
    if not client_ids:
        return []
    products = (
        (await session.execute(select(SoftwareProduct).where(SoftwareProduct.org_id == org_id)))
        .scalars()
        .all()
    )
    entitled: dict[tuple[UUID, UUID], int] = {
        (client_id, product_id): int(total)
        for client_id, product_id, total in (
            await session.execute(
                select(License.client_id, License.product_id, func.sum(License.entitlements))
                .where(License.org_id == org_id, License.client_id.in_(client_ids))
                .group_by(License.client_id, License.product_id)
            )
        ).all()
    }

    result: list[dict[str, Any]] = []
    for client_id in client_ids:
        titles = await client_titles(session, org_id, client_id=client_id)
        for product in products:
            owned = entitled.get((client_id, product.id), 0)
            installed = len(devices_matching(product, titles))
            result.append(
                {
                    "client_id": client_id,
                    "product_id": product.id,
                    "name": product.name,
                    "entitled": owned,
                    "installed": installed,
                    "delta": owned - installed,
                    "breach": installed > owned,
                }
            )
    return result


async def compliance_summary(
    session: AsyncSession, org_id: UUID, *, client_id: UUID
) -> list[dict[str, Any]]:
    """Per-product compliance position of ONE client: `compliance_by_client`
    for that client alone (three statements whatever the catalogue holds)."""
    return await compliance_by_client(session, org_id, [client_id])


async def renewal_calendar(
    session: AsyncSession, org_id: UUID, *, now: datetime, within_days: int = 30
) -> list[dict[str, Any]]:
    """Licenses whose `renews_at` and/or `maintenance_expires_at` falls in the
    inclusive window `[now.date(), now.date() + within_days]`, org-scoped.

    `now` is passed in (pure/testable) rather than read from the clock. A
    license with BOTH dates qualifying emits TWO rows -- one per triggering
    date, each with its own `trigger` (`"renewal"` or `"maintenance"`) and
    `due_in_days = (due_date - now.date()).days`. Sorted by `due_date` asc.
    Each row names its license's client.
    """
    today = now.date()
    window_end = today + timedelta(days=within_days)

    licenses = (
        (
            await session.execute(
                select(License).where(
                    License.org_id == org_id,
                    or_(
                        License.renews_at.between(today, window_end),
                        License.maintenance_expires_at.between(today, window_end),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    rows: list[dict[str, Any]] = []
    for lic in licenses:
        if lic.renews_at is not None and today <= lic.renews_at <= window_end:
            rows.append(
                {
                    "license_id": lic.id,
                    "client_id": lic.client_id,
                    "product_id": lic.product_id,
                    "trigger": "renewal",
                    "due_date": lic.renews_at,
                    "due_in_days": (lic.renews_at - today).days,
                }
            )
        if (
            lic.maintenance_expires_at is not None
            and today <= lic.maintenance_expires_at <= window_end
        ):
            rows.append(
                {
                    "license_id": lic.id,
                    "client_id": lic.client_id,
                    "product_id": lic.product_id,
                    "trigger": "maintenance",
                    "due_date": lic.maintenance_expires_at,
                    "due_in_days": (lic.maintenance_expires_at - today).days,
                }
            )

    rows.sort(key=lambda row: row["due_date"])
    return rows


async def cost_view(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """Cost grouped by `(client_id, product_id, currency)`, org-scoped --
    summed `cost` per group. Licenses with a NULL `cost` or NULL `currency`
    are skipped (there is nothing meaningful to sum or group them by).
    Deliberately NO cross-currency sum: a product's USD and EUR licenses stay
    in separate rows rather than being added together -- and no cross-client
    sum either: what X pays is not what Y pays.
    """
    rows = (
        await session.execute(
            select(License.client_id, License.product_id, License.currency, func.sum(License.cost))
            .where(
                License.org_id == org_id,
                License.cost.is_not(None),
                License.currency.is_not(None),
            )
            .group_by(License.client_id, License.product_id, License.currency)
            .order_by(License.client_id, License.product_id, License.currency)
        )
    ).all()

    return [
        {
            "client_id": client_id,
            "product_id": product_id,
            "currency": currency,
            "total_cost": total_cost,
        }
        for client_id, product_id, currency, total_cost in rows
    ]


async def export_rows(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """Flat, org-scoped license rows for CSV export (true-up / Power BI).

    Deliberately excludes `key`/`key_sealed` (invariant #3) -- only
    non-secret entitlement/cost/lifecycle fields plus the resolved product
    name. Ordered deterministically by `product_name` then `license_id` so
    repeated exports of unchanged data produce byte-identical output.
    """
    rows = (
        await session.execute(
            select(License, SoftwareProduct.name)
            .join(SoftwareProduct, SoftwareProduct.id == License.product_id)
            .where(License.org_id == org_id, SoftwareProduct.org_id == org_id)
            .order_by(SoftwareProduct.name, License.id)
        )
    ).all()

    return [
        {
            "license_id": lic.id,
            "product_id": lic.product_id,
            "product_name": product_name,
            "vendor_id": lic.vendor_id,
            "type": lic.type,
            "entitlements": lic.entitlements,
            "cost": lic.cost,
            "currency": lic.currency,
            "po_ref": lic.po_ref,
            "purchased_at": lic.purchased_at,
            "maintenance_expires_at": lic.maintenance_expires_at,
            "renews_at": lic.renews_at,
            "notes": lic.notes,
        }
        for lic, product_name in rows
    ]
