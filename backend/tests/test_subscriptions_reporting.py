"""Tests for the Subscriptions reporting module (docs/modules/subscriptions/
spec.md §7): spend summary, renewal calendar, idle-seat report, and flat CSV
export rows.

Mirrors `tests/test_licenses_reporting.py`'s fixture style (throwaway
org/user seeded via `admin_engine`, subscriptions/seats seeded via direct
model inserts so tests control every field precisely) and its
`test_tenancy_report_org_filters_are_load_bearing` pattern: `spend_summary`
is additionally run on an RLS-bypassing `admin_engine` session with a second
org's data present, so `spend_summary`'s own `org_id` predicate -- not RLS
-- is proven to be what excludes it (mutation-verified: see
`.git/sdd/task-8-report.md`).

Subscriptions are seeded via direct `Subscription`/`SeatAssignment` model
inserts (bypassing the service layer, which is exercised by
`tests/test_subscriptions_service.py`) so each test controls exactly the
fields its hand-computed assertions depend on.
"""

import datetime as _dt
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Organization, SeatAssignment, Subscription, User, Vendor
from app.modules.subscriptions.reporting import (
    export_rows,
    idle_seats,
    renewal_calendar,
    spend_summary,
)

# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"subrpt-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _user(admin_engine: AsyncEngine, org_id: UUID, tag: str) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        user = User(
            org_id=org_id, external_subject=f"owner-{tag}-{uuid4().hex[:6]}", email=f"{tag}@x.com"
        )
        s.add(user)
        await s.commit()
        return user.id


async def _vendor(
    admin_engine: AsyncEngine, org_id: UUID, actor_id: UUID, *, name: str = "Vendor"
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        vendor = Vendor(org_id=org_id, name=name, website=None, contacts=[], created_by=actor_id)
        s.add(vendor)
        await s.commit()
        return vendor.id


async def _seed_subscription(
    admin_engine: AsyncEngine,
    org_id: UUID,
    actor_id: UUID,
    *,
    service_name: str = "Sub",
    vendor_id: UUID | None = None,
    category: str = "",
    owner_user_id: UUID | None = None,
    billing_cycle: str = "monthly",
    cost_per_cycle: Decimal = Decimal("10.00"),
    currency: str = "USD",
    seats: int = 0,
    started_at: _dt.date | None = None,
    renews_at: _dt.date | None = None,
    auto_renew: bool = False,
    notice_days: int = 0,
    payment_ref: str | None = None,
    license_ref: UUID | None = None,
    status: str = "active",
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        sub = Subscription(
            org_id=org_id,
            client_id=org_id,
            vendor_id=vendor_id,
            service_name=service_name,
            plan="",
            category=category,
            billing_cycle=billing_cycle,
            cost_per_cycle=cost_per_cycle,
            currency=currency,
            seats=seats,
            owner_user_id=owner_user_id,
            started_at=started_at,
            renews_at=renews_at,
            auto_renew=auto_renew,
            notice_days=notice_days,
            payment_ref=payment_ref,
            license_ref=license_ref,
            status=status,
            created_by=actor_id,
        )
        s.add(sub)
        await s.commit()
        return sub.id


async def _seed_seat(
    admin_engine: AsyncEngine,
    org_id: UUID,
    actor_id: UUID,
    sub_id: UUID,
    *,
    released: bool = False,
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        seat = SeatAssignment(
            org_id=org_id,
            subscription_id=sub_id,
            assignee_name=f"Person {uuid4().hex[:6]}",
            assignee_email=f"{uuid4().hex[:6]}@example.com",
            released_at=_dt.datetime.now(_dt.UTC) if released else None,
            created_by=actor_id,
        )
        s.add(seat)
        await s.commit()
        return seat.id


# ---------------------------------------------------------------------------
# spend_summary
# ---------------------------------------------------------------------------


async def test_spend_summary_hand_computed_by_currency_vendor_category_owner(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    vendor_1 = await _vendor(admin_engine, org_id, actor_id, name="Acme Corp")
    vendor_2 = await _vendor(admin_engine, org_id, actor_id, name="Globex")
    owner_1 = await _user(admin_engine, org_id, "owner1")
    owner_2 = await _user(admin_engine, org_id, "owner2")

    # two monthly USD subs under vendor_1: 10 + 20 -> monthly_total 30, annual 360
    await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="S1",
        vendor_id=vendor_1,
        category="cat-a",
        owner_user_id=owner_1,
        cost_per_cycle=Decimal("10.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )
    await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="S2",
        vendor_id=vendor_1,
        category="cat-b",
        owner_user_id=owner_2,
        cost_per_cycle=Decimal("20.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )
    # a EUR sub -- must stay in a separate currency bucket, never summed with USD
    await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="S3",
        vendor_id=vendor_2,
        category="cat-a",
        owner_user_id=owner_1,
        cost_per_cycle=Decimal("15.00"),
        currency="EUR",
        billing_cycle="monthly",
        status="active",
    )
    # a cancelled sub with a large cost -- must be excluded entirely
    await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="Cancelled",
        cost_per_cycle=Decimal("1000.00"),
        currency="USD",
        billing_cycle="monthly",
        status="cancelled",
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        report = await spend_summary(s, org_id)

    by_currency = {row["currency"]: row for row in report["by_currency"]}
    assert by_currency["USD"]["monthly_total"] == Decimal("30.00")
    assert by_currency["USD"]["annual_total"] == Decimal("360.00")
    assert by_currency["EUR"]["monthly_total"] == Decimal("15.00")
    assert by_currency["EUR"]["annual_total"] == Decimal("180.00")

    for row in report["by_currency"]:
        assert row["group_key"] is None
        assert row["group_type"] is None
        assert row["group_id"] is None

    by_vendor = {(row["group_id"], row["currency"]): row for row in report["by_vendor"]}
    assert by_vendor[(vendor_1, "USD")]["monthly_total"] == Decimal("30.00")
    assert by_vendor[(vendor_1, "USD")]["annual_total"] == Decimal("360.00")
    assert by_vendor[(vendor_2, "EUR")]["monthly_total"] == Decimal("15.00")
    # vendor roll-up rows carry the vendor's actual name in group_key and
    # its id in group_id, not a raw id -- self-contained for a CSV/BI
    # consumer with no follow-up lookup.
    assert by_vendor[(vendor_1, "USD")]["group_key"] == "Acme Corp"
    assert by_vendor[(vendor_1, "USD")]["group_type"] == "vendor"
    assert by_vendor[(vendor_2, "EUR")]["group_key"] == "Globex"
    assert by_vendor[(vendor_2, "EUR")]["group_type"] == "vendor"

    by_category = {(row["group_key"], row["currency"]): row for row in report["by_category"]}
    assert by_category[("cat-a", "USD")]["monthly_total"] == Decimal("10.00")
    assert by_category[("cat-b", "USD")]["monthly_total"] == Decimal("20.00")
    assert by_category[("cat-a", "EUR")]["monthly_total"] == Decimal("15.00")
    assert by_category[("cat-a", "USD")]["group_type"] == "category"
    assert by_category[("cat-a", "USD")]["group_id"] is None

    by_owner = {(row["group_id"], row["currency"]): row for row in report["by_owner"]}
    assert by_owner[(owner_1, "USD")]["monthly_total"] == Decimal("10.00")
    assert by_owner[(owner_2, "USD")]["monthly_total"] == Decimal("20.00")
    assert by_owner[(owner_1, "EUR")]["monthly_total"] == Decimal("15.00")
    # owner roll-up rows carry the owner's actual email in group_key.
    assert by_owner[(owner_1, "USD")]["group_key"] == "owner1@x.com"
    assert by_owner[(owner_1, "USD")]["group_type"] == "owner"
    assert by_owner[(owner_2, "USD")]["group_key"] == "owner2@x.com"

    # the cancelled sub's 1000.00 must not appear anywhere
    all_monthly_usd = sum(
        (row["monthly_total"] for row in report["by_currency"] if row["currency"] == "USD"),
        Decimal("0"),
    )
    assert all_monthly_usd == Decimal("30.00")


async def test_spend_summary_null_vendor_and_owner_grouped_together(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="NoVendor",
        vendor_id=None,
        owner_user_id=None,
        cost_per_cycle=Decimal("5.00"),
        currency="USD",
        billing_cycle="monthly",
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        report = await spend_summary(s, org_id)

    # a subscription with no vendor rolls up under a bucket with both
    # group_key and group_id None (never a placeholder string), still
    # tagged group_type="vendor" so a consumer can tell what the bucket is.
    vendor_row = next(row for row in report["by_vendor"] if row["currency"] == "USD")
    assert vendor_row["group_id"] is None
    assert vendor_row["group_key"] is None
    assert vendor_row["group_type"] == "vendor"
    assert vendor_row["monthly_total"] == Decimal("5.00")

    owner_row = next(row for row in report["by_owner"] if row["currency"] == "USD")
    assert owner_row["group_id"] is None
    assert owner_row["group_key"] is None
    assert owner_row["group_type"] == "owner"
    assert owner_row["monthly_total"] == Decimal("5.00")


async def test_spend_summary_foreign_org_vendor_name_does_not_leak(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A vendor id that belongs to a *different* org (a dangling/foreign
    reference -- the FK itself doesn't encode tenancy) must never have its
    name resolved into org A's report: the vendor-name lookup in
    `spend_summary` is itself org-scoped (`Vendor.org_id == org_id`), so a
    cross-org id simply fails to resolve and falls back to `group_key=None`
    rather than leaking org B's vendor name into org A's data."""
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    foreign_vendor = await _vendor(admin_engine, org_b, actor_b, name="Org B Secret Vendor")

    await _seed_subscription(
        admin_engine,
        org_a,
        actor_a,
        service_name="CrossOrgVendorSub",
        vendor_id=foreign_vendor,
        cost_per_cycle=Decimal("7.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        report = await spend_summary(s, org_a)

    vendor_row = next(row for row in report["by_vendor"] if row["currency"] == "USD")
    assert vendor_row["group_key"] is None
    assert vendor_row["group_id"] is None
    assert vendor_row["group_key"] != "Org B Secret Vendor"


# ---------------------------------------------------------------------------
# renewal_calendar
# ---------------------------------------------------------------------------


async def test_renewal_calendar_hand_computed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)

    # renews in 40 days, notice_days=14 -> notice_deadline 26 days out (Jan 27),
    # within the 30-day window even though renews_at itself (Feb 10) is not.
    sub_notice_soon = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="NoticeSoon",
        renews_at=_dt.date(2026, 2, 10),
        notice_days=14,
        auto_renew=True,
        status="active",
    )
    # renews in 5 days (Jan 6), notice_days=0 -> notice_deadline == renews_at,
    # both in the future -- included via the renews_at clause.
    sub_renews_soon = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="RenewsSoon",
        renews_at=_dt.date(2026, 1, 6),
        notice_days=0,
        auto_renew=True,
        status="active",
    )
    # renews in 10 days (Jan 11), notice_days=15 -> notice_deadline is in the
    # past (Dec 27); the notice window has already passed, auto_renew=True
    # -> action_required is True.
    sub_action_required = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="ActionRequired",
        renews_at=_dt.date(2026, 1, 11),
        notice_days=15,
        auto_renew=True,
        status="active",
    )
    # identical dates but auto_renew=False -> action_required must be False
    sub_no_auto_renew = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="NoAutoRenew",
        renews_at=_dt.date(2026, 1, 11),
        notice_days=15,
        auto_renew=False,
        status="active",
    )
    # renews in 90 days, notice_days=5 -> both notice_deadline and renews_at
    # fall outside the 30-day window -- excluded entirely
    await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="FarOut",
        renews_at=_dt.date(2026, 4, 1),
        notice_days=5,
        status="active",
    )
    # cancelled, renews soon -- excluded (not active)
    await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="Cancelled",
        renews_at=_dt.date(2026, 1, 5),
        status="cancelled",
    )
    # active but no renews_at at all -- excluded entirely
    await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="NoRenewal",
        renews_at=None,
        status="active",
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await renewal_calendar(s, org_id, now=now)

    by_sub = {row["subscription_id"]: row for row in rows}
    assert set(by_sub) == {
        sub_notice_soon,
        sub_renews_soon,
        sub_action_required,
        sub_no_auto_renew,
    }

    notice_soon = by_sub[sub_notice_soon]
    assert notice_soon["renews_at"] == _dt.date(2026, 2, 10)
    assert notice_soon["notice_deadline"] == _dt.date(2026, 1, 27)
    assert notice_soon["days_to_notice"] == 26
    assert notice_soon["days_to_renewal"] == 40
    assert notice_soon["action_required"] is False

    renews_soon = by_sub[sub_renews_soon]
    assert renews_soon["notice_deadline"] == _dt.date(2026, 1, 6)
    assert renews_soon["days_to_notice"] == 5
    assert renews_soon["days_to_renewal"] == 5
    assert renews_soon["action_required"] is False

    action_required = by_sub[sub_action_required]
    assert action_required["notice_deadline"] == _dt.date(2025, 12, 27)
    assert action_required["days_to_notice"] == -5
    assert action_required["days_to_renewal"] == 10
    assert action_required["action_required"] is True

    no_auto_renew = by_sub[sub_no_auto_renew]
    assert no_auto_renew["notice_deadline"] == _dt.date(2025, 12, 27)
    assert no_auto_renew["action_required"] is False

    # sorted by notice_deadline ascending
    deadlines = [row["notice_deadline"] for row in rows]
    assert deadlines == sorted(deadlines)


async def test_renewal_calendar_within_days_boundary_is_inclusive(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)

    sub_boundary = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        renews_at=_dt.date(2026, 1, 31),  # exactly 30 days out
        notice_days=0,
        status="active",
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await renewal_calendar(s, org_id, now=now, within_days=30)

    assert any(
        row["subscription_id"] == sub_boundary and row["days_to_renewal"] == 30 for row in rows
    )


async def test_renewal_calendar_notice_deadline_today_is_inclusive(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`notice_deadline == now.date()` (day-offset 0) is the lower inclusive
    bound of the window -- hand-computed: renews in 10 days, notice_days=10
    -> notice_deadline is exactly `today`. `auto_renew=True` plus
    `today >= notice_deadline` (True, since they're equal) means
    `action_required` must also be True."""
    org_id, actor_id = await _org_with_user(admin_engine)
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)

    sub_id = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="NoticeDeadlineToday",
        renews_at=_dt.date(2026, 1, 11),  # 10 days out
        notice_days=10,  # notice_deadline = 2026-01-01 == now.date()
        auto_renew=True,
        status="active",
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await renewal_calendar(s, org_id, now=now)

    row = next(r for r in rows if r["subscription_id"] == sub_id)
    assert row["notice_deadline"] == _dt.date(2026, 1, 1)
    assert row["days_to_notice"] == 0
    assert row["action_required"] is True


async def test_renewal_calendar_renews_at_today_is_inclusive(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`renews_at == now.date()` (day-offset 0) is also inclusive at the
    lower bound -- hand-computed: renews today, notice_days=0 -> both
    `notice_deadline` and `renews_at` equal `now.date()`."""
    org_id, actor_id = await _org_with_user(admin_engine)
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)

    sub_id = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="RenewsToday",
        renews_at=_dt.date(2026, 1, 1),  # renews today
        notice_days=0,
        auto_renew=False,
        status="active",
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await renewal_calendar(s, org_id, now=now)

    row = next(r for r in rows if r["subscription_id"] == sub_id)
    assert row["renews_at"] == _dt.date(2026, 1, 1)
    assert row["days_to_renewal"] == 0
    assert row["days_to_notice"] == 0


# ---------------------------------------------------------------------------
# idle_seats
# ---------------------------------------------------------------------------


async def test_idle_seats_hand_computed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    sub_id = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="SeatedSub",
        seats=5,
        cost_per_cycle=Decimal("100.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )
    await _seed_seat(admin_engine, org_id, actor_id, sub_id, released=False)
    await _seed_seat(admin_engine, org_id, actor_id, sub_id, released=False)
    await _seed_seat(admin_engine, org_id, actor_id, sub_id, released=True)  # not active

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await idle_seats(s, org_id)

    assert len(rows) == 1
    row = rows[0]
    assert row["subscription_id"] == sub_id
    assert row["service_name"] == "SeatedSub"
    assert row["seats"] == 5
    assert row["assigned"] == 2
    assert row["idle"] == 3
    assert row["currency"] == "USD"
    assert row["idle_monthly_cost"] == Decimal("60.00")  # 3 * (100.00 / 5)


async def test_idle_seats_zero_seats_guarded(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    sub_id = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        seats=0,
        cost_per_cycle=Decimal("50.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await idle_seats(s, org_id)

    row = next(r for r in rows if r["subscription_id"] == sub_id)
    assert row["assigned"] == 0
    assert row["idle"] == 0
    assert row["idle_monthly_cost"] == Decimal("0.00")


async def test_idle_seats_clamps_over_assigned_subscription_to_zero(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`idle` is clamped at 0 (M3). More active assignments than `seats` is
    reachable for legacy/imported rows, and an unclamped `seats - assigned`
    renders as a NEGATIVE cost on the spend dashboard."""
    org_id, actor_id = await _org_with_user(admin_engine)

    sub_id = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="OverAssigned",
        seats=2,
        cost_per_cycle=Decimal("100.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )
    for _ in range(7):
        await _seed_seat(admin_engine, org_id, actor_id, sub_id, released=False)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await idle_seats(s, org_id)

    row = next(r for r in rows if r["subscription_id"] == sub_id)
    assert row["assigned"] == 7
    assert row["idle"] == 0
    assert row["idle_monthly_cost"] == Decimal("0.00")


async def test_idle_seats_excludes_cancelled_subscriptions(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    await _seed_subscription(
        admin_engine, org_id, actor_id, seats=10, status="cancelled"
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await idle_seats(s, org_id)

    assert rows == []


# ---------------------------------------------------------------------------
# export_rows
# ---------------------------------------------------------------------------


async def test_export_rows_has_expected_fields_and_no_org_id(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    vendor_id = await _vendor(admin_engine, org_id, actor_id)

    sub_id = await _seed_subscription(
        admin_engine,
        org_id,
        actor_id,
        service_name="Export Sub",
        vendor_id=vendor_id,
        category="productivity",
        billing_cycle="annual",
        cost_per_cycle=Decimal("1200.00"),
        currency="USD",
        seats=3,
        started_at=_dt.date(2025, 1, 1),
        renews_at=_dt.date(2026, 1, 1),
        auto_renew=True,
        notice_days=30,
        payment_ref="INV-42",
        status="active",
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await export_rows(s, org_id)

    assert len(rows) == 1
    row = rows[0]
    assert "org_id" not in row
    assert row["subscription_id"] == sub_id
    assert row["vendor_id"] == vendor_id
    assert row["service_name"] == "Export Sub"
    assert row["category"] == "productivity"
    assert row["billing_cycle"] == "annual"
    assert row["cost_per_cycle"] == Decimal("1200.00")
    assert row["currency"] == "USD"
    assert row["seats"] == 3
    assert row["started_at"] == _dt.date(2025, 1, 1)
    assert row["renews_at"] == _dt.date(2026, 1, 1)
    assert row["auto_renew"] is True
    assert row["notice_days"] == 30
    assert row["payment_ref"] == "INV-42"
    assert row["status"] == "active"


async def test_export_rows_ordered_deterministically_by_service_name(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    await _seed_subscription(admin_engine, org_id, actor_id, service_name="Zeta")
    await _seed_subscription(admin_engine, org_id, actor_id, service_name="Alpha")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await export_rows(s, org_id)

    names = [row["service_name"] for row in rows]
    assert names == ["Alpha", "Zeta"]


# ---------------------------------------------------------------------------
# tenancy
# ---------------------------------------------------------------------------


async def test_a_second_orgs_rows_are_excluded_from_all_four_reports_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)

    sub_a = await _seed_subscription(
        admin_engine,
        org_a,
        actor_a,
        service_name="OrgA Sub",
        seats=2,
        cost_per_cycle=Decimal("10.00"),
        currency="USD",
        renews_at=_dt.date(2026, 1, 5),
        notice_days=1,
        status="active",
    )
    await _seed_subscription(
        admin_engine,
        org_b,
        actor_b,
        service_name="OrgB Sub",
        seats=99,
        cost_per_cycle=Decimal("999.00"),
        currency="USD",
        renews_at=_dt.date(2026, 1, 5),
        notice_days=1,
        status="active",
    )
    await _seed_seat(admin_engine, org_a, actor_a, sub_a, released=False)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        spend = await spend_summary(s, org_a)
        renewals = await renewal_calendar(s, org_a, now=now)
        idle = await idle_seats(s, org_a)
        export = await export_rows(s, org_a)

    assert all(row["monthly_total"] < Decimal("100") for row in spend["by_currency"])
    assert {row["subscription_id"] for row in renewals} == {sub_a}
    assert {row["subscription_id"] for row in idle} == {sub_a}
    assert {row["subscription_id"] for row in export} == {sub_a}


async def test_tenancy_spend_summary_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Run `spend_summary` on an RLS-bypassing `admin_engine` session (the
    `its_admin` role has BYPASSRLS -- see `tests/conftest.py`) with a second
    org's subscription present, so `spend_summary`'s own `org_id` predicate
    -- not RLS -- is the SOLE guard proven here against cross-tenant
    leakage. Mirrors `tests/test_licenses_reporting.py`'s
    `test_tenancy_report_org_filters_are_load_bearing`.

    Mutation-verified: temporarily deleting the `Subscription.org_id ==
    org_id` predicate from `spend_summary` makes this test fail (org B's
    999.00/mo sub leaks into org A's USD total); restoring it makes the
    test pass again.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    await _seed_subscription(
        admin_engine,
        org_a,
        actor_a,
        service_name="OrgA Sub",
        cost_per_cycle=Decimal("10.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )
    # org B: much larger cost -- would inflate org A's total if leaked
    await _seed_subscription(
        admin_engine,
        org_b,
        actor_b,
        service_name="OrgB Sub",
        cost_per_cycle=Decimal("999.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )

    # RLS-bypassing admin session: org B's row is visible on this
    # connection, so only spend_summary's own org_id predicate can exclude it.
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        report = await spend_summary(s, org_a)

    by_currency = {row["currency"]: row for row in report["by_currency"]}
    assert by_currency["USD"]["monthly_total"] == Decimal("10.00")


async def test_tenancy_idle_seats_seat_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`idle_seats`'s own `SeatAssignment.org_id == org_id` predicate -- not
    RLS -- must be what keeps a foreign org's seat row out of org A's
    assigned count.

    `seat_assignment.subscription_id` is a plain FK to `subscription.id`: it
    does not encode tenancy, so a row owned by org B can reference org A's
    subscription. `test_a_second_orgs_rows_are_excluded_from_all_four_reports_by_rls`
    runs under `org_session(..., engine=app_engine)` -- the non-BYPASSRLS
    `app_user` role -- where RLS already hides org B's seat row, so it cannot
    prove the application-level predicate does anything. Here `idle_seats`
    runs on a raw `AsyncSession(admin_engine)` (`its_admin` is BYPASSRLS --
    see `tests/conftest.py`), so org B's seat row is fully visible on this
    connection and only the explicit `SeatAssignment.org_id` predicate can
    exclude it from the count.

    Mutation-verified: deleting the `SeatAssignment.org_id == org_id`
    predicate from `idle_seats` makes this test fail (org B's seat inflates
    org A's `assigned` to 2 and deflates `idle` to 3); restoring it makes the
    test pass again. See `.git/sdd/phase12-fix-backend-report.md` for the
    observed failure output.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    sub_a = await _seed_subscription(
        admin_engine,
        org_a,
        actor_a,
        service_name="OrgA Seated Sub",
        seats=5,
        cost_per_cycle=Decimal("100.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )

    # org A's own active seat
    await _seed_seat(admin_engine, org_a, actor_a, sub_a, released=False)
    # org B's seat, pointing at org A's subscription -- the FK permits it.
    await _seed_seat(admin_engine, org_b, actor_b, sub_a, released=False)

    # RLS-bypassing admin session: org B's seat row is visible on this
    # connection, so only idle_seats's own SeatAssignment.org_id predicate can
    # keep it out of org A's count.
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        rows = await idle_seats(s, org_a)

    row = next(r for r in rows if r["subscription_id"] == sub_a)
    assert row["assigned"] == 1
    assert row["idle"] == 4
    assert row["idle_monthly_cost"] == Decimal("80.00")  # 4 * (100.00 / 5)


async def test_tenancy_vendor_name_lookup_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`_resolve_vendor_names`'s own `Vendor.org_id == org_id` predicate --
    not RLS -- must be what keeps a foreign vendor's name out of org A's
    report. `test_spend_summary_foreign_org_vendor_name_does_not_leak` runs
    under `org_session(..., engine=app_engine)`, which uses the non-BYPASSRLS
    `app_user` role -- RLS alone would already hide org B's vendor row on
    that connection, so that test cannot prove the application-level
    predicate does anything. Here `spend_summary` runs on a raw
    `AsyncSession(admin_engine)` (the `its_admin` role has BYPASSRLS -- see
    `tests/conftest.py`), so org B's vendor row is fully visible on this
    connection and only `_resolve_vendor_names`'s own `org_id` predicate can
    keep its name out of org A's `by_vendor` rows.

    Mutation-verified: temporarily deleting the `Vendor.org_id == org_id`
    predicate from `_resolve_vendor_names` makes this test fail (org B's
    vendor name resolves into org A's report); restoring it makes the test
    pass again. See `.git/sdd/task-8-report.md` for the observed failure
    transcript.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    foreign_vendor = await _vendor(admin_engine, org_b, actor_b, name="Org B Secret Vendor")

    await _seed_subscription(
        admin_engine,
        org_a,
        actor_a,
        service_name="CrossOrgVendorSub",
        vendor_id=foreign_vendor,
        cost_per_cycle=Decimal("7.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )

    # RLS-bypassing admin session: org B's vendor row is visible on this
    # connection, so only _resolve_vendor_names's own org_id predicate can
    # exclude it from resolving.
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        report = await spend_summary(s, org_a)

    vendor_row = next(row for row in report["by_vendor"] if row["currency"] == "USD")
    assert vendor_row["group_key"] is None
    assert vendor_row["group_key"] != "Org B Secret Vendor"
    assert "Org B Secret Vendor" not in {
        row["group_key"] for rows in report.values() for row in rows
    }


async def test_tenancy_owner_email_lookup_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`_resolve_user_emails`'s own `User.org_id == org_id` predicate -- not
    RLS -- must be what keeps a foreign owner's email out of org A's report.
    `spend_summary` runs here on a raw `AsyncSession(admin_engine)` (the
    `its_admin` role has BYPASSRLS -- see `tests/conftest.py`), so org B's
    user row is fully visible on this connection and only
    `_resolve_user_emails`'s own `org_id` predicate can keep its email out of
    org A's `by_owner` rows.

    Mutation-verified: temporarily deleting the `User.org_id == org_id`
    predicate from `_resolve_user_emails` makes this test fail (org B's
    owner email resolves into org A's report); restoring it makes the test
    pass again. See `.git/sdd/task-8-report.md` for the observed failure
    transcript.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    foreign_owner = await _user(admin_engine, org_b, "orgb-owner")

    await _seed_subscription(
        admin_engine,
        org_a,
        actor_a,
        service_name="CrossOrgOwnerSub",
        owner_user_id=foreign_owner,
        cost_per_cycle=Decimal("7.00"),
        currency="USD",
        billing_cycle="monthly",
        status="active",
    )

    # RLS-bypassing admin session: org B's user row is visible on this
    # connection, so only _resolve_user_emails's own org_id predicate can
    # exclude it from resolving.
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        report = await spend_summary(s, org_a)

    owner_row = next(row for row in report["by_owner"] if row["currency"] == "USD")
    assert owner_row["group_key"] is None
    # The raw id is suppressed alongside the label: emitting an unresolvable
    # owner id would leak a cross-tenant identifier even with the email gone.
    assert owner_row["group_id"] is None
    assert owner_row["group_key"] != "orgb-owner@x.com"
    assert "orgb-owner@x.com" not in {
        row["group_key"] for rows in report.values() for row in rows
    }
