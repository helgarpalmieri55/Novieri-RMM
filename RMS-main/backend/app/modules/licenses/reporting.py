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
`SoftwareProduct`, and -- via `product_installed_device_ids` --
`InstalledSoftware`) per the tenancy invariant (#5) -- RLS is the backstop,
not the only line of defense.

`export_rows` never includes `key` or `key_sealed` (invariant #3): CSV export
exists for entitlement true-up, not credential export.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import License, SoftwareProduct
from app.modules.licenses.matching import product_installed_device_ids


async def compliance_summary(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """Per-product compliance position, org-scoped, computed live.

    For each `SoftwareProduct` in the org: `entitled` is the sum of
    `entitlements` over that product's org-scoped `License` rows (`0` if it
    has none); `installed` is `len(product_installed_device_ids(...))` --
    the count of distinct org devices with software matching any of the
    product's match rules; `delta = entitled - installed`; `breach` is `True`
    when `installed > entitled`.
    """
    products = (
        (await session.execute(select(SoftwareProduct).where(SoftwareProduct.org_id == org_id)))
        .scalars()
        .all()
    )

    result: list[dict[str, Any]] = []
    for product in products:
        entitled = (
            await session.execute(
                select(func.coalesce(func.sum(License.entitlements), 0)).where(
                    License.org_id == org_id, License.product_id == product.id
                )
            )
        ).scalar_one()
        entitled = int(entitled)

        installed = len(await product_installed_device_ids(session, org_id, product))

        result.append(
            {
                "product_id": product.id,
                "name": product.name,
                "entitled": entitled,
                "installed": installed,
                "delta": entitled - installed,
                "breach": installed > entitled,
            }
        )

    return result


async def renewal_calendar(
    session: AsyncSession, org_id: UUID, *, now: datetime, within_days: int = 30
) -> list[dict[str, Any]]:
    """Licenses whose `renews_at` and/or `maintenance_expires_at` falls in the
    inclusive window `[now.date(), now.date() + within_days]`, org-scoped.

    `now` is passed in (pure/testable) rather than read from the clock. A
    license with BOTH dates qualifying emits TWO rows -- one per triggering
    date, each with its own `trigger` (`"renewal"` or `"maintenance"`) and
    `due_in_days = (due_date - now.date()).days`. Sorted by `due_date` asc.
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
                    "product_id": lic.product_id,
                    "trigger": "maintenance",
                    "due_date": lic.maintenance_expires_at,
                    "due_in_days": (lic.maintenance_expires_at - today).days,
                }
            )

    rows.sort(key=lambda row: row["due_date"])
    return rows


async def cost_view(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """Cost grouped by `(product_id, currency)`, org-scoped -- summed `cost`
    per group. Licenses with a NULL `cost` or NULL `currency` are skipped
    (there is nothing meaningful to sum or group them by). Deliberately NO
    cross-currency sum: a product's USD and EUR licenses stay in separate
    rows rather than being added together.
    """
    rows = (
        await session.execute(
            select(License.product_id, License.currency, func.sum(License.cost))
            .where(
                License.org_id == org_id,
                License.cost.is_not(None),
                License.currency.is_not(None),
            )
            .group_by(License.product_id, License.currency)
        )
    ).all()

    return [
        {"product_id": product_id, "currency": currency, "total_cost": total_cost}
        for product_id, currency, total_cost in rows
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
