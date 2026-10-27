"""Subscriptions reporting (docs/modules/subscriptions/spec.md §7): read-only
spend summary, renewal calendar, idle-seat report, and flat CSV export rows.

Pure read-only aggregation over the Subscriptions module's tables. This
module performs **no writes** and records **no audit events** -- it exists
solely to answer "what are we spending, and on what" (`spend_summary`),
"what's coming up for renewal or cancellation notice" (`renewal_calendar`),
"which paid seats are sitting unused" (`idle_seats`), and "give me a flat row
set for a true-up / spreadsheet export" (`export_rows`). Mirrors
`app.modules.licenses.reporting`'s style and its no-audit-on-view invariant:
viewing a report must never be indistinguishable from mutating anything, so
nothing here ever calls `audit_service`.

Every query below filters on `org_id` for every table involved (`Subscription`
and, for `idle_seats`, `SeatAssignment`) per the tenancy invariant (#5) --
RLS is the backstop, not the only line of defense.

Money is `Decimal` throughout (invariant #6: never sum across currencies).
A subscription is ONE client's (plan 2026-10-26 Task 6), and spend is never
summed across clients either: every spend row is one client's.
`spend_summary` and `idle_seats` reuse `costs.monthly_equivalent` /
`costs.annualized` rather than re-deriving billing-cycle math. A
subscription's `currency` is used as-is for grouping -- `None` groups under
the key `None` rather than being coerced to a sentinel string. (The DB column
is currently `NOT NULL`, so this path is defensive rather than reachable
today; it costs nothing to handle correctly.)

`export_rows` never includes `org_id` (invariant #3/#5) -- only the
subscription's own scalar fields plus a `subscription_id` (the pattern
`licenses/reporting.py`'s `export_rows` uses for `license_id`).
"""

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SeatAssignment, Subscription, User, Vendor
from app.modules.subscriptions.costs import annualized, monthly_equivalent

_TWO_PLACES = Decimal("0.01")
_ZERO = Decimal("0.00")


def _bucket_add(
    buckets: dict[Any, dict[str, Any]],
    key: Any,
    fields: dict[str, Any],
    monthly: Decimal,
    annual: Decimal,
) -> None:
    """Accumulate `monthly`/`annual` into `buckets[key]`, creating the row
    (seeded with `fields` plus zeroed totals) on first sight of `key`."""
    row = buckets.setdefault(key, {**fields, "monthly_total": _ZERO, "annual_total": _ZERO})
    row["monthly_total"] += monthly
    row["annual_total"] += annual


async def _resolve_vendor_names(
    session: AsyncSession, org_id: UUID, vendor_ids: set[UUID]
) -> dict[UUID, str]:
    """One org-scoped lookup for the vendor names of `vendor_ids` (skipped
    entirely when empty). An id that doesn't resolve -- deleted row or a
    cross-org id that never should have been passed in -- is simply absent
    from the returned map rather than raising."""
    if not vendor_ids:
        return {}
    rows = await session.execute(
        select(Vendor.id, Vendor.name).where(Vendor.org_id == org_id, Vendor.id.in_(vendor_ids))
    )
    return {vendor_id: name for vendor_id, name in rows.all()}


async def _resolve_user_emails(
    session: AsyncSession, org_id: UUID, user_ids: set[UUID]
) -> dict[UUID, str]:
    """One org-scoped lookup for the emails of `user_ids` (skipped entirely
    when empty). An id that doesn't resolve is simply absent from the
    returned map rather than raising."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(User.id, User.email).where(User.org_id == org_id, User.id.in_(user_ids))
    )
    return {user_id: email for user_id, email in rows.all()}


async def spend_summary(session: AsyncSession, org_id: UUID) -> dict[str, list[dict[str, Any]]]:
    """Spend across all *active* subscriptions, org-scoped, per client.

    Each active subscription's `cost_per_cycle` is normalized once via
    `monthly_equivalent`/`annualized`, then rolled up four ways -- overall by
    `(client_id, currency)`, and by `(client_id, vendor_id, currency)`,
    `(client_id, category, currency)`, and `(client_id, owner_user_id,
    currency)` -- so no roll-up is ever summed across currencies or across
    clients (plan 2026-10-26 Task 6): what X pays is not what Y pays.
    `vendor_id`/`owner_user_id` group under `None` rather than being dropped
    when unset. Cancelled/trial
    subscriptions are excluded entirely (spend for inactive services isn't
    "current spend").

    Every row in all four lists conforms to `schema.SpendRow`: `client_id`,
    `currency`,
    `monthly_total`, `annual_total`, `group_key` (a human-readable label),
    `group_type`, `group_id` (the underlying id for linking). This makes the
    report self-contained -- a CSV/BI consumer reading `by_vendor` gets the
    vendor's actual name, not just its id -- via exactly one extra org-scoped
    query per entity type (vendor names, owner emails), each restricted to
    the ids actually present in this org's active subscriptions and skipped
    entirely when that id set is empty.

    - `by_currency` rows carry `group_key=None`, `group_type=None`,
      `group_id=None` -- there's no grouping entity for the overall total.
    - `by_vendor` rows carry `group_type="vendor"`; `group_key` is the
      vendor's name and `group_id` its id, or both `None` for the "no
      vendor" bucket. A `vendor_id` that doesn't resolve (deleted row, or a
      cross-org id) falls back to `group_key=None` AND `group_id=None`
      rather than raising: emitting the raw id while suppressing the name
      would still leak another tenant's identifier into this org's report,
      so the two are suppressed together.
    - `by_category` rows carry `group_type="category"`, `group_key` the
      category value itself (categories have no id), `group_id=None`
      always.
    - `by_owner` rows carry `group_type="owner"`; `group_key` is the
      owner's email and `group_id` its user id, with the same
      None-together/non-resolving-id handling as vendors.
    """
    subs = (
        (
            await session.execute(
                select(Subscription).where(
                    Subscription.org_id == org_id, Subscription.status == "active"
                )
            )
        )
        .scalars()
        .all()
    )

    by_currency: dict[tuple[UUID, str], dict[str, Any]] = {}
    by_vendor: dict[tuple[UUID, UUID | None, str], dict[str, Any]] = {}
    by_category: dict[tuple[UUID, str, str], dict[str, Any]] = {}
    by_owner: dict[tuple[UUID, UUID | None, str], dict[str, Any]] = {}

    for sub in subs:
        monthly = monthly_equivalent(sub.cost_per_cycle, sub.billing_cycle)
        annual = annualized(sub.cost_per_cycle, sub.billing_cycle)
        currency = sub.currency
        client_id = sub.client_id

        _bucket_add(
            by_currency,
            (client_id, currency),
            {"client_id": client_id, "currency": currency},
            monthly,
            annual,
        )
        _bucket_add(
            by_vendor,
            (client_id, sub.vendor_id, currency),
            {"client_id": client_id, "vendor_id": sub.vendor_id, "currency": currency},
            monthly,
            annual,
        )
        _bucket_add(
            by_category,
            (client_id, sub.category, currency),
            {"client_id": client_id, "category": sub.category, "currency": currency},
            monthly,
            annual,
        )
        _bucket_add(
            by_owner,
            (client_id, sub.owner_user_id, currency),
            {"client_id": client_id, "owner_user_id": sub.owner_user_id, "currency": currency},
            monthly,
            annual,
        )

    vendor_ids = {vendor_id for _, vendor_id, _ in by_vendor if vendor_id is not None}
    owner_ids = {owner_id for _, owner_id, _ in by_owner if owner_id is not None}
    vendor_names = await _resolve_vendor_names(session, org_id, vendor_ids)
    owner_emails = await _resolve_user_emails(session, org_id, owner_ids)

    by_currency_rows = [
        {
            "client_id": row["client_id"],
            "currency": row["currency"],
            "monthly_total": row["monthly_total"],
            "annual_total": row["annual_total"],
            "group_key": None,
            "group_type": None,
            "group_id": None,
        }
        for row in by_currency.values()
    ]
    # A vendor id that failed to resolve against `Vendor.org_id == org_id`
    # (deleted row, or a cross-org id) is suppressed in BOTH `group_key` and
    # `group_id` -- emitting the raw id alone would still leak another
    # tenant's identifier into this org's report.
    by_vendor_rows = [
        {
            "client_id": row["client_id"],
            "currency": row["currency"],
            "monthly_total": row["monthly_total"],
            "annual_total": row["annual_total"],
            "group_key": vendor_names.get(row["vendor_id"]) if row["vendor_id"] else None,
            "group_type": "vendor",
            "group_id": row["vendor_id"] if row["vendor_id"] in vendor_names else None,
        }
        for row in by_vendor.values()
    ]
    by_category_rows = [
        {
            "client_id": row["client_id"],
            "currency": row["currency"],
            "monthly_total": row["monthly_total"],
            "annual_total": row["annual_total"],
            "group_key": row["category"],
            "group_type": "category",
            "group_id": None,
        }
        for row in by_category.values()
    ]
    by_owner_rows = [
        {
            "client_id": row["client_id"],
            "currency": row["currency"],
            "monthly_total": row["monthly_total"],
            "annual_total": row["annual_total"],
            "group_key": owner_emails.get(row["owner_user_id"]) if row["owner_user_id"] else None,
            "group_type": "owner",
            # Same both-or-neither suppression as `by_vendor` above.
            "group_id": row["owner_user_id"] if row["owner_user_id"] in owner_emails else None,
        }
        for row in by_owner.values()
    ]

    return {
        "by_currency": by_currency_rows,
        "by_vendor": by_vendor_rows,
        "by_category": by_category_rows,
        "by_owner": by_owner_rows,
    }


async def renewal_calendar(
    session: AsyncSession, org_id: UUID, *, now: datetime, within_days: int = 30
) -> list[dict[str, Any]]:
    """Active subscriptions whose cancellation-notice deadline or renewal
    date is imminent, org-scoped.

    A subscription with `renews_at IS NULL` is excluded entirely -- there is
    no date to report on. For the rest, `notice_deadline = renews_at -
    notice_days`; a subscription is included when EITHER `notice_deadline`
    OR `renews_at` falls in the inclusive window `[now.date(), now.date() +
    within_days]` -- so a subscription with a long notice period surfaces
    here well before its renewal date does. `days_to_notice`/
    `days_to_renewal` are signed (negative once the date has passed) and
    computed against `now.date()`, not against `today()` (pure/testable, no
    wall-clock read). `action_required` is `True` only when the
    subscription both auto-renews and today is on/after the notice
    deadline -- i.e. the org must act now to prevent an unwanted renewal.
    Sorted by `notice_deadline` ascending.
    """
    today = now.date()
    window_end = today + timedelta(days=within_days)

    subs = (
        (
            await session.execute(
                select(Subscription).where(
                    Subscription.org_id == org_id,
                    Subscription.status == "active",
                    Subscription.renews_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    rows: list[dict[str, Any]] = []
    for sub in subs:
        # narrowed by the `renews_at.is_not(None)` query filter above
        assert sub.renews_at is not None
        renews_at: date = sub.renews_at
        notice_deadline = renews_at - timedelta(days=sub.notice_days)

        notice_in_window = today <= notice_deadline <= window_end
        renewal_in_window = today <= renews_at <= window_end
        if not (notice_in_window or renewal_in_window):
            continue

        rows.append(
            {
                "subscription_id": sub.id,
                "service_name": sub.service_name,
                "renews_at": renews_at,
                "notice_deadline": notice_deadline,
                "days_to_notice": (notice_deadline - today).days,
                "days_to_renewal": (renews_at - today).days,
                "action_required": sub.auto_renew and today >= notice_deadline,
            }
        )

    rows.sort(key=lambda row: row["notice_deadline"])
    return rows


async def idle_seats(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """Unused-seat cost per active subscription, org-scoped.

    `assigned` is the count of that subscription's `SeatAssignment` rows
    with `released_at IS NULL` (the module's one definition of "active
    seat" -- reused here, not reinvented); `idle = seats - assigned`.
    Per-seat monthly cost is `monthly_equivalent(cost_per_cycle,
    billing_cycle) / seats`, quantized to 0.01 like every other money value
    in this module; `seats == 0` is guarded to `0.00` rather than raising
    `ZeroDivisionError` (a misconfigured/legacy zero-seat subscription
    should report as "nothing idle", not crash the report).
    """
    subs = (
        (
            await session.execute(
                select(Subscription).where(
                    Subscription.org_id == org_id, Subscription.status == "active"
                )
            )
        )
        .scalars()
        .all()
    )

    rows: list[dict[str, Any]] = []
    for sub in subs:
        assigned = (
            await session.execute(
                select(func.count())
                .select_from(SeatAssignment)
                .where(
                    SeatAssignment.org_id == org_id,
                    SeatAssignment.subscription_id == sub.id,
                    SeatAssignment.released_at.is_(None),
                )
            )
        ).scalar_one()

        # Clamped at 0: more active assignments than `seats` is reachable for
        # legacy/imported rows, and an unclamped `seats - assigned` would
        # render as a NEGATIVE idle cost on the spend dashboard.
        # `update_subscription` separately refuses to reduce `seats` below the
        # active count, so this is the backstop, not the only guard.
        idle = max(0, sub.seats - assigned)
        if sub.seats > 0:
            monthly = monthly_equivalent(sub.cost_per_cycle, sub.billing_cycle)
            per_seat = (monthly / sub.seats).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        else:
            per_seat = _ZERO

        rows.append(
            {
                "subscription_id": sub.id,
                "service_name": sub.service_name,
                "seats": sub.seats,
                "assigned": assigned,
                "idle": idle,
                "currency": sub.currency,
                "idle_monthly_cost": per_seat * idle,
            }
        )

    return rows


async def export_rows(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """Flat, org-scoped subscription rows for CSV export.

    Every scalar field on `Subscription` except `org_id` (invariant #5,
    never leak the tenant id) and `created_by` (an actor reference, not
    subscription data -- mirrors `licenses/reporting.py::export_rows`
    excluding its own actor-only fields). `payment_ref` is included as a
    low-sensitivity reference (spec §8) -- no vault secret exists on this
    model to exclude. Ordered deterministically by `service_name` then
    `id` so repeated exports of unchanged data produce byte-identical
    output.
    """
    subs = (
        (
            await session.execute(
                select(Subscription)
                .where(Subscription.org_id == org_id)
                .order_by(Subscription.service_name, Subscription.id)
            )
        )
        .scalars()
        .all()
    )

    return [
        {
            "subscription_id": sub.id,
            "vendor_id": sub.vendor_id,
            "service_name": sub.service_name,
            "plan": sub.plan,
            "category": sub.category,
            "billing_cycle": sub.billing_cycle,
            "cost_per_cycle": sub.cost_per_cycle,
            "currency": sub.currency,
            "seats": sub.seats,
            "owner_user_id": sub.owner_user_id,
            "started_at": sub.started_at,
            "renews_at": sub.renews_at,
            "auto_renew": sub.auto_renew,
            "notice_days": sub.notice_days,
            "payment_ref": sub.payment_ref,
            "license_ref": sub.license_ref,
            "status": sub.status,
            "created_at": sub.created_at,
        }
        for sub in subs
    ]
