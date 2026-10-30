"""Renewal projection and idempotent materialization (spec §6).

Covers: `project_costs` writes NOTHING (a pure read-time projection);
`materialize` is idempotent under `uq_expense_autofeed` -- a second call over
the same window creates nothing, and a VOIDED row is never resurrected (spec
§2's deliberate design: the unique index still covers voided rows); an
unconvertible projected cost is still created, with `amount_base = NULL`, and
counted; cancelled/trial subscriptions are excluded from projection (mirrors
subscriptions/spec.md §7); a missing `system_key` category fails loudly and
namably, and BEFORE any row is written; the materialization window is bounded
at `MAX_MATERIALIZE_MONTHS`; licenses project both `renews_at` and
`maintenance_expires_at`; and a load-bearing cross-org test proves the
`org_id` predicate inside `project_costs`, not RLS, is what keeps org B's
subscription out of org A's projection.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.modules.budget.feeds as feeds_module
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Expense, License, SoftwareProduct, User
from app.modules.budget.errors import BudgetValidationError
from app.modules.budget.expenses import void_expense
from app.modules.budget.feeds import (
    MAX_MATERIALIZE_MONTHS,
    MAX_MATERIALIZE_ROWS,
    ProjectedCost,
    materialize,
    merge_colliding_projections,
    project_costs,
)
from app.modules.budget.refdata import (
    category_by_system_key,
    create_category,
    seed_default_categories,
    update_category,
)
from app.modules.subscriptions.models import Subscription


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Mirrors test_budget_expenses.py's helper of the same name/shape -- not
    imported across test modules, per convention."""
    org_id = await api_env.create_org("feeds")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _seed_subscription(
    session: AsyncSession,
    org_id: UUID,
    user_id: UUID,
    *,
    renews_at: date,
    status: str = "active",
    billing_cycle: str = "monthly",
    cost_per_cycle: Decimal = Decimal("10.00"),
    currency: str = "USD",
    service_name: str = "Acme SaaS",
) -> Subscription:
    sub = Subscription(
        org_id=org_id,
        client_id=org_id,
        service_name=service_name,
        billing_cycle=billing_cycle,
        cost_per_cycle=cost_per_cycle,
        currency=currency,
        renews_at=renews_at,
        status=status,
        created_by=user_id,
    )
    session.add(sub)
    await session.flush()
    return sub


async def _seed_license(
    session: AsyncSession,
    org_id: UUID,
    user_id: UUID,
    *,
    cost: Decimal | None,
    currency: str | None,
    renews_at: date | None = None,
    maintenance_expires_at: date | None = None,
) -> License:
    product = SoftwareProduct(org_id=org_id, name="Acme Suite", created_by=user_id)
    session.add(product)
    await session.flush()

    lic = License(
        org_id=org_id,
        client_id=org_id,
        product_id=product.id,
        type="volume",
        entitlements=10,
        cost=cost,
        currency=currency,
        renews_at=renews_at,
        maintenance_expires_at=maintenance_expires_at,
        created_by=user_id,
    )
    session.add(lic)
    await session.flush()
    return lic


async def _expense_count(session: AsyncSession, org_id: UUID) -> int:
    return len(
        (await session.execute(select(Expense.id).where(Expense.org_id == org_id)))
        .scalars()
        .all()
    )


async def _expenses_for_org(session: AsyncSession, org_id: UUID) -> list[Expense]:
    return list(
        (
            await session.execute(
                select(Expense).where(Expense.org_id == org_id).order_by(Expense.id)
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# project_costs: pure, writes nothing
# ---------------------------------------------------------------------------


async def test_project_costs_writes_nothing(api_env) -> None:
    """The forecast must never persist -- that is what keeps it from drifting."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        await _seed_subscription(session, org_id, user_id, renews_at=date(2026, 2, 1))

        before = await _expense_count(session, org_id)
        result = await project_costs(
            session, org_id, client_id=org_id, from_date=date(2026, 1, 1), to_date=date(2026, 6, 30)
        )
        after = await _expense_count(session, org_id)

    assert before == 0
    assert after == before
    # Not vacuous -- the projection actually found something to (not) write.
    assert len(result) > 0
    assert all(isinstance(p, ProjectedCost) for p in result)


async def test_project_costs_rejects_from_date_after_to_date(api_env) -> None:
    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(BudgetValidationError):
            await project_costs(
                session, org_id, client_id=org_id,
                from_date=date(2026, 6, 1), to_date=date(2026, 1, 1)
            )


# ---------------------------------------------------------------------------
# materialize: idempotency
# ---------------------------------------------------------------------------


async def test_materialize_twice_creates_nothing_the_second_time(api_env) -> None:
    """The idempotency guarantee. Not an assumption -- a test."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        await _seed_subscription(session, org_id, user_id, renews_at=date(2026, 2, 1))

        first = await materialize(
            session, org_id, user_id, client_id=org_id,
            from_date=date(2026, 1, 1), to_date=date(2026, 6, 30)
        )
        second = await materialize(
            session, org_id, user_id, client_id=org_id,
            from_date=date(2026, 1, 1), to_date=date(2026, 6, 30)
        )

    assert first["created"] > 0
    assert second["created"] == 0
    assert second["skipped_existing"] == first["created"]


async def test_a_voided_entry_is_not_resurrected(api_env) -> None:
    """Spec §2: voided rows deliberately stay under the unique constraint.
    An operator voided it on purpose; re-running must not undo that."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        sub = await _seed_subscription(session, org_id, user_id, renews_at=date(2026, 2, 1))

        first = await materialize(
            session, org_id, user_id, client_id=org_id,
            from_date=date(2026, 2, 1), to_date=date(2026, 2, 28)
        )
        assert first["created"] == 1

        created_expense = (
            await session.execute(
                select(Expense).where(
                    Expense.org_id == org_id,
                    Expense.source == "subscription",
                    Expense.link_id == sub.id,
                    Expense.period_key == "2026-02",
                )
            )
        ).scalar_one()
        await void_expense(session, org_id, user_id, created_expense.id)

        second = await materialize(
            session, org_id, user_id, client_id=org_id,
            from_date=date(2026, 2, 1), to_date=date(2026, 2, 28)
        )

        rows = await _expenses_for_org(session, org_id)

    assert second["created"] == 0
    assert second["skipped_existing"] == 1
    # Exactly one row exists for this (source, link_id, period_key), and it
    # is still void -- not resurrected, not duplicated.
    assert len(rows) == 1
    assert rows[0].status == "void"
    assert rows[0].id == created_expense.id


async def test_unconvertible_projection_is_created_and_counted(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        # No fx_rate row exists for JPY -> the org's default base (USD).
        await _seed_subscription(
            session,
            org_id,
            user_id,
            renews_at=date(2026, 2, 1),
            currency="JPY",
            cost_per_cycle=Decimal("5000.00"),
        )

        result = await materialize(
            session, org_id, user_id, client_id=org_id,
            from_date=date(2026, 2, 1), to_date=date(2026, 2, 28)
        )

        expense = (
            await session.execute(
                select(Expense).where(Expense.org_id == org_id, Expense.currency == "JPY")
            )
        ).scalar_one()

    assert result["created"] == 1
    assert result["skipped_unconvertible"] == 1
    assert expense.amount_base is None
    assert expense.fx_rate is None
    assert expense.amount == Decimal("5000.00")


async def test_cancelled_and_trial_subscriptions_are_excluded(api_env) -> None:
    """Mirrors subscriptions/spec.md §7. The trial exclusion is flagged there
    as an open product question -- if it changes, it changes in both places."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        active = await _seed_subscription(
            session, org_id, user_id, renews_at=date(2026, 2, 1), status="active",
            service_name="Active SaaS",
        )
        await _seed_subscription(
            session, org_id, user_id, renews_at=date(2026, 2, 1), status="cancelled",
            service_name="Cancelled SaaS",
        )
        await _seed_subscription(
            session, org_id, user_id, renews_at=date(2026, 2, 1), status="trial",
            service_name="Trial SaaS",
        )

        result = await project_costs(
            session, org_id, client_id=org_id, from_date=date(2026, 1, 1), to_date=date(2026, 6, 30)
        )

    link_ids = {p.link_id for p in result}
    assert link_ids == {active.id}


async def test_missing_system_key_category_fails_loudly(api_env) -> None:
    """Spec §6.1: no fallback guess. The error names the missing key. Task 6
    scenario: a hand-made category with a colliding `key` keeps a NULL
    `system_key` -- this path is reachable in practice, not hypothetical."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        # Hand-made category colliding with the seeded key, left with a NULL
        # system_key -- no seeding happened here at all.
        await create_category(
            session, org_id, user_id, client_id=org_id,
            key="saas_subscriptions", name="My Own Category"
        )
        await _seed_subscription(session, org_id, user_id, renews_at=date(2026, 2, 1))

        with pytest.raises(BudgetValidationError) as exc:
            await materialize(
                session, org_id, user_id, client_id=org_id,
                from_date=date(2026, 1, 1), to_date=date(2026, 6, 30)
            )
        assert "saas_subscriptions" in str(exc.value)

        # Nothing was partially written -- the check runs before any Expense
        # is constructed or added.
        count = await _expense_count(session, org_id)

    assert count == 0


async def test_retired_category_fails_materialization_loudly(api_env) -> None:
    """K2 fix: spec §6.1 says "no ACTIVE category with that system_key" --
    `category_by_system_key` didn't check `active` at all. Money scenario:
    finance retires "SaaS Subscriptions" after moving SaaS under a new
    category; the next materialization must fail loudly and namably, not
    silently book spend to the retired row."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        cat = await category_by_system_key(session, org_id, "saas_subscriptions", client_id=org_id)
        assert cat is not None
        await update_category(session, org_id, user_id, cat.id, {"active": False})

        await _seed_subscription(session, org_id, user_id, renews_at=date(2026, 2, 1))

        # Confirm the category genuinely still exists (retired, not deleted)
        # and is simply no longer resolvable by system_key.
        retired_lookup = await category_by_system_key(
            session, org_id, "saas_subscriptions", client_id=org_id
        )
        assert retired_lookup is None

        with pytest.raises(BudgetValidationError) as exc:
            await materialize(
                session, org_id, user_id, client_id=org_id,
                from_date=date(2026, 1, 1), to_date=date(2026, 6, 30)
            )
        assert "saas_subscriptions" in str(exc.value)

        # Nothing was partially written.
        count = await _expense_count(session, org_id)

    assert count == 0


async def test_window_longer_than_24_months_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)

        with pytest.raises(BudgetValidationError):
            await materialize(
                session,
                org_id,
                user_id, client_id=org_id,
                from_date=date(2026, 1, 1),
                to_date=date(2028, 2, 28),  # 26 months, inclusive
            )


async def test_window_of_exactly_24_months_is_accepted(api_env) -> None:
    """The bound rejects LONGER than 24 months, not 24 itself."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        await _seed_subscription(session, org_id, user_id, renews_at=date(2026, 3, 1))

        result = await materialize(
            session,
            org_id,
            user_id, client_id=org_id,
            from_date=date(2026, 1, 1),
            to_date=date(2027, 12, 31),  # exactly 24 months, inclusive
        )

    assert MAX_MATERIALIZE_MONTHS == 24
    assert result["created"] > 0


def test_max_materialize_rows_constant() -> None:
    """I4: pinned at 2000 -- ample for any real materialization run, and the
    row-limit tests below monkeypatch this down rather than actually
    creating thousands of subscriptions, so this pins the real value."""
    assert MAX_MATERIALIZE_ROWS == 2000


async def test_materialize_rejects_projection_count_over_row_limit(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I4: `MAX_MATERIALIZE_MONTHS` bounds the WINDOW, not the write -- 500
    monthly subscriptions x 24 months is 12,000 inserts (and as many audit
    events, each taking `audit_service.record`'s
    `pg_advisory_xact_lock(hashtext('audit:' || org))`) in ONE transaction,
    serializing every other audited action in that org for the run's
    duration. Monkeypatches the limit down to 2 (rather than creating
    thousands of real subscriptions) so the boundary itself is exercised;
    `test_max_materialize_rows_constant` above pins the real 2000 value."""
    monkeypatch.setattr(feeds_module, "MAX_MATERIALIZE_ROWS", 2)
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        for i in range(3):
            await _seed_subscription(
                session,
                org_id,
                user_id,
                renews_at=date(2026, 1, 15),
                service_name=f"Sub {i}",
            )

        with pytest.raises(BudgetValidationError) as exc:
            await materialize(
                session,
                org_id,
                user_id, client_id=org_id,
                from_date=date(2026, 1, 1),
                to_date=date(2026, 1, 31),
            )
        assert "3" in str(exc.value)  # names the count
        assert "2" in str(exc.value)  # and the limit

        # Checked BEFORE any write: nothing landed.
        count = await _expense_count(session, org_id)

    assert count == 0


async def test_materialize_accepts_projection_count_at_row_limit(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound rejects MORE than the limit, not the limit itself -- same
    "exactly N is accepted" shape as the 24-month boundary test above."""
    monkeypatch.setattr(feeds_module, "MAX_MATERIALIZE_ROWS", 2)
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        for i in range(2):
            await _seed_subscription(
                session,
                org_id,
                user_id,
                renews_at=date(2026, 1, 15),
                service_name=f"Sub {i}",
            )

        result = await materialize(
            session,
            org_id,
            user_id, client_id=org_id,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
        )

    assert result["created"] == 2


async def test_licenses_project_renewal_and_maintenance_dates(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        lic = await _seed_license(
            session,
            org_id,
            user_id,
            cost=Decimal("500.00"),
            currency="USD",
            renews_at=date(2026, 3, 15),
            maintenance_expires_at=date(2026, 5, 20),
        )

        result = await project_costs(
            session, org_id, client_id=org_id,
            from_date=date(2026, 1, 1), to_date=date(2026, 12, 31)
        )

        materialized = await materialize(
            session, org_id, user_id, client_id=org_id,
            from_date=date(2026, 1, 1), to_date=date(2026, 12, 31)
        )
        rows = await _expenses_for_org(session, org_id)

    assert {(p.link_id, p.period_key) for p in result} == {
        (lic.id, "2026-03"),
        (lic.id, "2026-05"),
    }
    for p in result:
        assert p.source == "license"
        assert p.link_type == "license"
        assert p.category_system_key == "software_licenses"
        assert p.amount == Decimal("500.00")
        assert p.currency == "USD"

    assert materialized["created"] == 2
    assert len(rows) == 2
    assert {r.period_key for r in rows} == {"2026-03", "2026-05"}
    assert {r.expense_date for r in rows} == {date(2026, 3, 15), date(2026, 5, 20)}
    for r in rows:
        assert r.source == "license"
        assert r.link_id == lic.id
        assert r.amount == Decimal("500.00")


async def test_license_with_no_cost_is_not_projected(api_env) -> None:
    """A license with no recorded cost/currency has nothing to project --
    materialization must never fabricate an amount."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        await _seed_license(
            session,
            org_id,
            user_id,
            cost=None,
            currency=None,
            renews_at=date(2026, 3, 15),
        )

        result = await project_costs(
            session, org_id, client_id=org_id,
            from_date=date(2026, 1, 1), to_date=date(2026, 12, 31)
        )

    assert result == []


# ---------------------------------------------------------------------------
# K1: same-key collisions are MERGED, not dropped
# ---------------------------------------------------------------------------


async def test_same_month_license_renewal_and_maintenance_are_merged(api_env) -> None:
    """K1 money scenario: a license renews 2026-03-15 for $500 and its
    maintenance expires 2026-03-28 for $500 -- both land in period_key
    "2026-03". `uq_expense_autofeed` can only ever hold ONE row per
    (org_id, source, link_id, period_key), so these two must MERGE into a
    single $1,000 projection naming both events, not silently drop one.
    The forecast (`project_costs`) and the write (`materialize`) must
    agree: one row, created == 1, skipped_existing == 0."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        lic = await _seed_license(
            session,
            org_id,
            user_id,
            cost=Decimal("500.00"),
            currency="USD",
            renews_at=date(2026, 3, 15),
            maintenance_expires_at=date(2026, 3, 28),
        )

        result = await project_costs(
            session, org_id, client_id=org_id,
            from_date=date(2026, 1, 1), to_date=date(2026, 12, 31)
        )
        materialized = await materialize(
            session, org_id, user_id, client_id=org_id,
            from_date=date(2026, 1, 1), to_date=date(2026, 12, 31)
        )
        rows = await _expenses_for_org(session, org_id)

    assert len(result) == 1
    proj = result[0]
    assert proj.link_id == lic.id
    assert proj.period_key == "2026-03"
    assert proj.amount == Decimal("1000.00")
    assert proj.currency == "USD"
    # Names both events -- not the generic, indistinguishable-across-licenses
    # description the original implementation used.
    assert "renewal" in proj.description
    assert "maintenance" in proj.description
    assert "Acme Suite" in proj.description
    # Earliest of the two colliding dates.
    assert proj.on_date == date(2026, 3, 15)

    assert materialized["created"] == 1
    assert materialized["skipped_existing"] == 0
    assert len(rows) == 1
    assert rows[0].amount == Decimal("1000.00")
    assert rows[0].period_key == "2026-03"


def test_merge_colliding_projections_rejects_mismatched_currency() -> None:
    """Merging is only valid within a single currency (the per-record
    currency rule) -- colliding projections that disagree on currency must
    raise `BudgetValidationError` naming the record rather than sum across
    currency codes. This is a pure unit test of the merge step itself,
    mirroring how `fx.convert`/`resolve_rate` are unit-tested directly in
    test_budget_fx.py -- the current License schema has one currency field
    per record, so a real mixed-currency collision can't be produced
    through `project_costs`/the DB today; the guard is exercised directly."""
    lic_id = uuid4()
    usd_leg = ProjectedCost(
        source="license",
        link_id=lic_id,
        link_type="license",
        period_key="2026-03",
        on_date=date(2026, 3, 15),
        amount=Decimal("500.00"),
        currency="USD",
        description="Acme Suite license renewal",
        vendor_id=None,
        category_system_key="software_licenses",
    )
    eur_leg = ProjectedCost(
        source="license",
        link_id=lic_id,
        link_type="license",
        period_key="2026-03",
        on_date=date(2026, 3, 28),
        amount=Decimal("450.00"),
        currency="EUR",
        description="Acme Suite license maintenance",
        vendor_id=None,
        category_system_key="software_licenses",
    )

    with pytest.raises(BudgetValidationError) as exc:
        merge_colliding_projections([usd_leg, eur_leg])

    assert str(lic_id) in str(exc.value)
    assert "USD" in str(exc.value)
    assert "EUR" in str(exc.value)


def test_merge_colliding_projections_passes_through_non_colliding() -> None:
    """No collision -- e.g. two different licenses, or the same license in
    two different months -- means nothing is merged or altered."""
    proj_a = ProjectedCost(
        source="license",
        link_id=uuid4(),
        link_type="license",
        period_key="2026-03",
        on_date=date(2026, 3, 15),
        amount=Decimal("500.00"),
        currency="USD",
        description="Acme Suite license renewal",
        vendor_id=None,
        category_system_key="software_licenses",
    )
    proj_b = ProjectedCost(
        source="license",
        link_id=uuid4(),
        link_type="license",
        period_key="2026-03",
        on_date=date(2026, 3, 20),
        amount=Decimal("300.00"),
        currency="USD",
        description="Other Suite license renewal",
        vendor_id=None,
        category_system_key="software_licenses",
    )

    result = merge_colliding_projections([proj_a, proj_b])

    assert result == [proj_a, proj_b]


# ---------------------------------------------------------------------------
# K3: the write itself is conflict-safe, not just pre-checked
# ---------------------------------------------------------------------------


async def test_materialize_concurrent_calls_do_not_race_the_expense_insert(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Two overlapping `materialize` calls can both pass the in-memory
    `existing_keys` pre-check before either has written its row -- the DB's
    `uq_expense_autofeed` index is the real backstop. The fix mirrors the
    Task-5 F2 precedent in `fx.get_settings`: the actual write goes through
    `ON CONFLICT DO NOTHING`, so the loser is counted as `skipped_existing`
    rather than raising a raw `IntegrityError` (spec §8 forbids that
    surfacing as a 500 on a financial path).

    Deterministic interleave, same technique as
    test_get_settings_concurrent_first_calls_do_not_race in
    test_budget_fx.py: session A's pre-check SELECT against `expense` is
    intercepted, and right after it returns (a miss -- nothing exists yet),
    session B inserts and commits the EXACT row session A is about to try
    to create. That reproduces the race window between the pre-check and
    the write, deterministically rather than hoping two coroutines happen
    to interleave."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        cat = await category_by_system_key(session, org_id, "saas_subscriptions", client_id=org_id)
        assert cat is not None
        # COP, with no COP->USD rate on file, so this projection is BOTH
        # unconvertible and (below) a race loser. That combination is what
        # discriminates: counting `skipped_unconvertible` before the insert
        # is attempted puts this one projection in two buckets at once.
        sub = await _seed_subscription(
            session, org_id, user_id, renews_at=date(2026, 2, 1), currency="COP"
        )
        category_id = cat.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "expense.period_key" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    winner = Expense(
                        org_id=org_id, client_id=org_id,
                        expense_date=date(2026, 2, 1),
                        amount=Decimal("10.00"),
                        currency="USD",
                        category_id=category_id,
                        description="raced in first",
                        source="subscription",
                        link_type="subscription",
                        link_id=sub.id,
                        period_key="2026-02",
                        status="expected",
                        entered_by=user_id,
                    )
                    session_b.add(winner)
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        # Single-month window -- the subscription renews monthly, so a wider
        # window would produce more than one projection and make the
        # expected counts below depend on how many months are in range
        # rather than on the race itself.
        result = await materialize(
            session_a, org_id, user_id, client_id=org_id,
            from_date=date(2026, 2, 1), to_date=date(2026, 2, 28)
        )
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = await _expenses_for_org(session, org_id)

    # Session A never raised -- the conflict was absorbed, not thrown.
    assert result["created"] == 0
    assert result["skipped_existing"] == 1
    # Exactly one row exists -- session B's, not a duplicate.
    assert len(rows) == 1
    assert rows[0].description == "raced in first"
    # The projection is counted in exactly ONE bucket. It was unconvertible
    # (no COP rate on file), but it also lost the race, so nothing was
    # written by this call and `skipped_unconvertible` must not claim
    # otherwise: 1 projection, 1 disposition.
    assert result["skipped_unconvertible"] == 0
    assert (
        result["created"] + result["skipped_existing"] + result["skipped_unconvertible"]
    ) == 1


# ---------------------------------------------------------------------------
# K4: the audit path
# ---------------------------------------------------------------------------


async def test_materialize_audits_one_summary_and_one_created_event_per_row(api_env) -> None:
    """K4 fix: the audit path had zero test coverage. Assert exactly one
    `budget.expenses_materialized` summary event carrying the three counts,
    and exactly one `expense.created` per row actually created -- so a bug
    that moved the summary inside the loop, or typo'd the action name,
    cannot pass silently the way it could before this test existed."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        # Both renew in the SAME month and the window covers exactly that
        # one month -- a monthly billing_cycle would otherwise produce one
        # projection per month in range, making "created" depend on how
        # wide the window is rather than on what this test is checking.
        await _seed_subscription(
            session, org_id, user_id, renews_at=date(2026, 2, 1), service_name="First SaaS"
        )
        await _seed_subscription(
            session, org_id, user_id, renews_at=date(2026, 2, 1), service_name="Second SaaS"
        )

        result = await materialize(
            session, org_id, user_id, client_id=org_id,
            from_date=date(2026, 2, 1), to_date=date(2026, 2, 28)
        )

        summary_events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "budget.expenses_materialized",
                    )
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
        created_events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "expense.created",
                    )
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )

    assert result["created"] == 2
    assert len(summary_events) == 1
    assert summary_events[0].meta == {
        "from_date": "2026-02-01",
        "to_date": "2026-02-28",
        "created": result["created"],
        "skipped_existing": result["skipped_existing"],
        "skipped_unconvertible": result["skipped_unconvertible"],
    }
    assert summary_events[0].actor_type == "user"
    assert summary_events[0].actor_id == user_id

    assert len(created_events) == result["created"] == 2
    for event in created_events:
        assert event.target_type == "expense"
        assert event.actor_type == "user"
        assert event.actor_id == user_id
        assert event.meta["status"] == "expected"
        assert event.meta["source"] == "subscription"


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session
# ---------------------------------------------------------------------------


async def test_tenancy_project_costs_subscription_resolution_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org B's subscription AND license must never appear in org A's
    projection, even on a raw BYPASSRLS admin session where org B's rows
    are fully visible. `project_costs`'s `Subscription.org_id == org_id`
    and `License.org_id == org_id` predicates are the SOLE guards -- there
    is no RLS backstop on this session.

    K6 fix: org B's license is seeded here alongside its subscription so
    the `License.org_id == org_id` guard is mutation-verifiable IN
    ISOLATION. Before this fix, this test only seeded a subscription for
    org B, so deleting the `License` predicate passed when this test ran
    alone -- it only failed when the whole file ran, because OTHER tests'
    licenses (left behind in the same test DB across the file) happened to
    leak into org A's result. That made the guard's failure signal depend
    on execution order/residue rather than this test's own setup.

    Asked for ORG B's internal client (its id is org B's; plan 2026-10-26
    Task 8, lesson 2): `project_costs` also filters by client, and org B's
    rows ARE that client's, so the client predicate lets them through and
    only the org predicates keep them out. Asked for org A's own client, the
    client predicate alone would have hidden them."""
    org_a, _user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        sub_b = Subscription(
            org_id=org_b,
            client_id=org_b,
            service_name="Org B SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("10.00"),
            currency="USD",
            renews_at=date(2026, 2, 1),
            status="active",
            created_by=user_b,
        )
        session.add(sub_b)

        product_b = SoftwareProduct(org_id=org_b, name="Org B Suite", created_by=user_b)
        session.add(product_b)
        await session.flush()

        lic_b = License(
            org_id=org_b,
            client_id=org_b,
            product_id=product_b.id,
            type="volume",
            entitlements=5,
            cost=Decimal("500.00"),
            currency="USD",
            renews_at=date(2026, 3, 1),
            created_by=user_b,
        )
        session.add(lic_b)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        result = await project_costs(
            session, org_a, client_id=org_b, from_date=date(2026, 1, 1), to_date=date(2026, 6, 30)
        )

    assert result == []
