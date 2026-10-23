"""Budget reporting: variance, position, forecast, and the two CSV exports
(spec §7). This is the module's payoff -- everything else exists so these
five functions can answer "where does spend stand against plan".

Covers: the four-column rule (`planned`/`actual`/`committed`/`projected`
never collapsed, and the double-count guard that keeps a materialized row
from ALSO appearing as a projection); `actual` is `status='confirmed'` only,
`committed` is `status='expected'` only; voided expenses are excluded from
every figure, everywhere (variance, position, and both exports); unconvertible
rows are excluded from base-currency sums and always disclosed
(`unconvertible_count`/`unconvertible_by_currency`); `variance_pct`/
`pct_of_plan` return `None` on a zero-plan denominator, never `Infinity`;
`position()` computes elapsed periods from the fiscal calendar
(`periods.period_for_date`), not wall-clock month arithmetic; both exports
stringify money and the Odoo variant emits the exact pinned column contract;
no report writes an audit event; and a load-bearing, mutation-verified
cross-org test proves the `org_id` predicate inside `variance()`'s own
expense scan -- not RLS -- is what keeps another org's expense out of this
org's report. A separate load-bearing test proves an unresolvable
`vendor_id` is suppressed together with its id in `export_rows` (the
Phase-12 `group_id` lesson).
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Expense, ExpenseCategory, User
from app.modules.budget.budgets import create_budget, put_lines
from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.expenses import (
    confirm_expense,
    create_expense,
    update_expense,
    void_expense,
)
from app.modules.budget.feeds import materialize
from app.modules.budget.refdata import create_category, seed_default_categories
from app.modules.budget.reporting import (
    export_rows,
    forecast,
    odoo_export_rows,
    position,
    variance,
)
from app.modules.budget.schema import BudgetLineInput, ExpenseCreate
from app.modules.licenses.models import Vendor
from app.modules.subscriptions.models import Subscription


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Mirrors test_budget_expenses.py/test_budget_feeds.py's helper of the
    same name/shape -- not imported across test modules, per convention."""
    org_id = await api_env.create_org("reporting")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _make_budget(session, org_id, user_id, *, fiscal_year=2026, granularity="month"):
    return await create_budget(
        session, org_id, user_id, fiscal_year=fiscal_year, name="IT Budget", granularity=granularity
    )


async def _make_expense(
    session,
    org_id,
    user_id,
    *,
    category_id,
    expense_date,
    amount=Decimal("100.00"),
    currency="USD",
    status="confirmed",
    cost_center_id=None,
    vendor_id=None,
) -> Expense:
    return await create_expense(
        session,
        org_id,
        user_id,
        ExpenseCreate(
            expense_date=expense_date,
            amount=amount,
            currency=currency,
            category_id=category_id,
            cost_center_id=cost_center_id,
            vendor_id=vendor_id,
            status=status,
        ),
    )


def _row_for(
    rows: list[dict], *, category_id: UUID, period_index: int, cost_center_id=None
) -> dict:
    for row in rows:
        if (
            row["category_id"] == category_id
            and row["period_index"] == period_index
            and row["cost_center_id"] == cost_center_id
        ):
            return row
    raise AssertionError(f"no row for category={category_id} period={period_index}")


# ---------------------------------------------------------------------------
# variance: the four-column rule
# ---------------------------------------------------------------------------


async def test_variance_keeps_four_columns_separate(api_env) -> None:
    """planned / actual / committed / projected are never collapsed --
    collapsing committed and projected is how a forecast double-counts."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id)
        await put_lines(
            session,
            org_id,
            user_id,
            budget.id,
            [
                BudgetLineInput(
                    category_id=hardware.id, period_index=1, planned_amount=Decimal("1000.00")
                )
            ],
        )
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 15),
            amount=Decimal("100.00"), status="confirmed",
        )

        result = await variance(session, org_id, budget.id, user_id)

    assert len(result["rows"]) >= 1
    for row in result["rows"]:
        assert set(row) >= {
            "planned", "actual", "committed", "projected", "variance", "variance_pct",
            "category_id", "cost_center_id", "period_index",
        }
    assert "unconvertible_count" in result
    assert "unconvertible_by_currency" in result


async def test_actual_counts_only_confirmed_and_committed_only_expected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id)

        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 15),
            amount=Decimal("100.00"), status="confirmed",
        )
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 2, 10),
            amount=Decimal("50.00"), status="expected",
        )

        result = await variance(session, org_id, budget.id, user_id)

    row_p1 = _row_for(result["rows"], category_id=hardware.id, period_index=1)
    row_p2 = _row_for(result["rows"], category_id=hardware.id, period_index=2)

    assert row_p1["actual"] == Decimal("100.00")
    assert row_p1["committed"] == Decimal("0.00")
    assert row_p2["committed"] == Decimal("50.00")
    assert row_p2["actual"] == Decimal("0.00")


async def test_projected_excludes_periods_that_already_have_a_materialized_row(api_env) -> None:
    """The double-count guard, stated as a test: BEFORE materialization the
    subscription's renewal shows up as `projected`; after materializing it,
    the SAME period shows up as `committed` and `projected` drops to zero --
    never both at once."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        saas = next(c for c in cats if c.key == "saas_subscriptions")
        budget = await _make_budget(session, org_id, user_id)

        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("20.00"),
            currency="USD",
            renews_at=date(2026, 3, 1),
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        before = await variance(session, org_id, budget.id, user_id)
        row_before = _row_for(before["rows"], category_id=saas.id, period_index=3)
        assert row_before["projected"] == Decimal("20.00")
        assert row_before["committed"] == Decimal("0.00")

        await materialize(
            session, org_id, user_id, from_date=date(2026, 3, 1), to_date=date(2026, 3, 31)
        )

        after = await variance(session, org_id, budget.id, user_id)
        row_after = _row_for(after["rows"], category_id=saas.id, period_index=3)

    assert row_after["committed"] == Decimal("20.00")
    assert row_after["projected"] == Decimal("0.00")


async def test_materialized_then_confirmed_renewal_is_actual_not_projected(api_env) -> None:
    """L1: the module's designed happy path (spec §0.3) is materialize ->
    operator CONFIRMS. The guard that keeps a materialized renewal from also
    projecting must recognize the row by its MATERIALIZATION IDENTITY
    (source, link_id, period_key), which survives the `expected` ->
    `confirmed` transition -- a guard keyed on `status == 'expected'` (the
    bug) loses track of the row the instant it is confirmed, and the same
    renewal reappears as `projected` on top of its own `actual`."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        saas = next(c for c in cats if c.key == "saas_subscriptions")
        budget = await _make_budget(session, org_id, user_id)

        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("20.00"),
            currency="USD",
            renews_at=date(2026, 3, 1),
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        await materialize(
            session, org_id, user_id, from_date=date(2026, 3, 1), to_date=date(2026, 3, 31)
        )
        materialized = (
            await session.execute(
                select(Expense).where(Expense.org_id == org_id, Expense.link_id == sub.id)
            )
        ).scalar_one()
        await confirm_expense(session, org_id, user_id, materialized.id)

        result = await variance(session, org_id, budget.id, user_id)

    row = _row_for(result["rows"], category_id=saas.id, period_index=3)
    assert row["actual"] == Decimal("20.00")
    assert row["committed"] == Decimal("0.00")
    assert row["projected"] == Decimal("0.00")


async def test_editing_expense_date_does_not_resurrect_its_projection(api_env) -> None:
    """I1 money scenario: materialization writes `expected` for subscription
    S, `expense_date` 2026-03-20, `period_key` "2026-03", $12,000. The
    invoice actually lands April 2 and the operator PATCHes the date -- a
    routine correction, permitted on an auto-fed row (`update_expense` only
    guards `link_id`/`link_type` to `source == "manual"`; `expense_date` is
    freely editable on any non-void row). The guard must key on the STORED
    `period_key` column, not re-derive it from the (now-edited)
    `expense_date` -- deriving it would move the guard's key to
    ("subscription", S, "2026-04"), leaving March's slot unguarded so the
    SAME renewal reappears as `projected` for period 3 on top of its own
    (moved) `committed` in period 4 -- inflating the fiscal-year total by
    $12,000."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        saas = next(c for c in cats if c.key == "saas_subscriptions")
        budget = await _make_budget(session, org_id, user_id)

        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("12000.00"),
            currency="USD",
            renews_at=date(2026, 3, 20),
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        await materialize(
            session, org_id, user_id, from_date=date(2026, 3, 1), to_date=date(2026, 3, 31)
        )
        materialized = (
            await session.execute(
                select(Expense).where(Expense.org_id == org_id, Expense.link_id == sub.id)
            )
        ).scalar_one()
        assert materialized.period_key == "2026-03"

        # The routine correction: the invoice actually landed April 2.
        await update_expense(
            session, org_id, user_id, materialized.id, {"expense_date": date(2026, 4, 2)}
        )

        result = await variance(session, org_id, budget.id, user_id)

    rows_for_saas = [r for r in result["rows"] if r["category_id"] == saas.id]
    row_period_3 = next((r for r in rows_for_saas if r["period_index"] == 3), None)
    row_period_4 = next((r for r in rows_for_saas if r["period_index"] == 4), None)

    # Period 3's slot must not resurface at all -- the guard still
    # recognizes the moved row via its stored period_key, so nothing
    # contributes to period 3 any more (not committed, since the row moved;
    # not projected, since the guard still suppresses it).
    assert row_period_3 is None

    # Period 4 carries the corrected committed amount, plus the SEPARATE
    # April 20 renewal (not yet materialized) as its own projected figure --
    # two distinct renewals, $12,000 each, never collapsed into one, and
    # never inflated by double-counting the March renewal a second time.
    assert row_period_4 is not None
    assert row_period_4["committed"] == Decimal("12000.00")
    assert row_period_4["projected"] == Decimal("12000.00")


async def test_unmaterialized_renewal_still_projects_despite_a_sibling_materialized(
    api_env,
) -> None:
    """L2: the guard must be keyed on the MATERIALIZED ROW'S OWN IDENTITY
    (source, link_id, period_key), never on (category_id, period_index) --
    the bug's key -- or materializing ONE renewal blanks out every OTHER
    projection sharing its category and period, however large."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        saas = next(c for c in cats if c.key == "saas_subscriptions")
        budget = await _make_budget(session, org_id, user_id)

        small = Subscription(
            org_id=org_id,
            service_name="Small SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("20.00"),
            currency="USD",
            renews_at=date(2026, 3, 1),
            status="active",
            created_by=user_id,
        )
        big = Subscription(
            org_id=org_id,
            service_name="Big SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("5000.00"),
            currency="USD",
            renews_at=date(2026, 3, 20),
            status="active",
            created_by=user_id,
        )
        session.add_all([small, big])
        await session.flush()

        # Materialize ONLY the small renewal's occurrence (Mar 1-10) -- the
        # big renewal (Mar 20) is deliberately left un-materialized.
        await materialize(
            session, org_id, user_id, from_date=date(2026, 3, 1), to_date=date(2026, 3, 10)
        )

        result = await variance(session, org_id, budget.id, user_id)

    row = _row_for(result["rows"], category_id=saas.id, period_index=3)
    assert row["committed"] == Decimal("20.00")
    assert row["projected"] == Decimal("5000.00")


# ---------------------------------------------------------------------------
# M1: a hand-pre-recorded renewal (never materialized) must not double-count
# ---------------------------------------------------------------------------


async def test_manual_prerecorded_renewal_is_committed_not_projected(api_env) -> None:
    """M1: spec §5 blesses hand pre-recording a known upcoming renewal as a
    first-class workflow, not a workaround. An operator who records a $20
    renewal manually (status='expected', `link_type`/`link_id` pointing at
    the subscription, Task 9) instead of running `materialize` must see it
    ONCE -- `committed=20.00, projected=0.00` -- never `committed=20.00,
    projected=20.00`. The guard's old `(source, link_id, period_key)` key
    could never match this row: a manual row's `source` is always the
    literal `"manual"`, never `"subscription"`."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        saas = next(c for c in cats if c.key == "saas_subscriptions")
        budget = await _make_budget(session, org_id, user_id)

        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("20.00"),
            currency="USD",
            renews_at=date(2026, 3, 1),
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        await create_expense(
            session,
            org_id,
            user_id,
            ExpenseCreate(
                expense_date=date(2026, 3, 1),
                amount=Decimal("20.00"),
                currency="USD",
                category_id=saas.id,
                link_type="subscription",
                link_id=sub.id,
                status="expected",
            ),
        )

        result = await variance(session, org_id, budget.id, user_id)

    row = _row_for(result["rows"], category_id=saas.id, period_index=3)
    assert row["committed"] == Decimal("20.00")
    assert row["projected"] == Decimal("0.00")


async def test_manual_prerecorded_renewal_does_not_suppress_sibling_renewal(api_env) -> None:
    """M1 must not regress L2: a hand-pre-recorded $20 renewal must not
    blank out an unrelated, unmaterialized $5,000 renewal sharing the same
    category and period -- the guard still keys on the ROW'S OWN identity
    (now `(link_type, link_id, period_key)`), never on `(category_id,
    period_index)`."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        saas = next(c for c in cats if c.key == "saas_subscriptions")
        budget = await _make_budget(session, org_id, user_id)

        small = Subscription(
            org_id=org_id,
            service_name="Small SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("20.00"),
            currency="USD",
            renews_at=date(2026, 3, 1),
            status="active",
            created_by=user_id,
        )
        big = Subscription(
            org_id=org_id,
            service_name="Big SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("5000.00"),
            currency="USD",
            renews_at=date(2026, 3, 20),
            status="active",
            created_by=user_id,
        )
        session.add_all([small, big])
        await session.flush()

        # Hand pre-record ONLY the small renewal -- the big one is left as a
        # pure, unmaterialized projection.
        await create_expense(
            session,
            org_id,
            user_id,
            ExpenseCreate(
                expense_date=date(2026, 3, 1),
                amount=Decimal("20.00"),
                currency="USD",
                category_id=saas.id,
                link_type="subscription",
                link_id=small.id,
                status="expected",
            ),
        )

        result = await variance(session, org_id, budget.id, user_id)

    row = _row_for(result["rows"], category_id=saas.id, period_index=3)
    assert row["committed"] == Decimal("20.00")
    assert row["projected"] == Decimal("5000.00")


async def test_manual_expense_without_link_id_does_not_suppress_projection(api_env) -> None:
    """M1's widened guard must still require a non-NULL `link_id` on a
    manual row -- an ordinary hand-entered expense with no `link_id` at all
    (the common case: most manual rows aren't pre-recording a renewal)
    carries no materialization identity and must not suppress ANY sibling
    projection sharing its category/period."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        saas = next(c for c in cats if c.key == "saas_subscriptions")
        budget = await _make_budget(session, org_id, user_id)

        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("20.00"),
            currency="USD",
            renews_at=date(2026, 3, 1),
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        # An unrelated manual expense in the SAME category/period, with no
        # link_id -- must not suppress the subscription's projection.
        await create_expense(
            session,
            org_id,
            user_id,
            ExpenseCreate(
                expense_date=date(2026, 3, 5),
                amount=Decimal("9.99"),
                currency="USD",
                category_id=saas.id,
                status="expected",
            ),
        )

        result = await variance(session, org_id, budget.id, user_id)

    row = _row_for(result["rows"], category_id=saas.id, period_index=3)
    assert row["committed"] == Decimal("9.99")
    assert row["projected"] == Decimal("20.00")


# ---------------------------------------------------------------------------
# L3: a voided materialized cost must never resurrect as `projected`
# ---------------------------------------------------------------------------


async def test_variance_voided_materialized_renewal_does_not_resurrect_as_projected(
    api_env,
) -> None:
    """L3: `feeds.py`'s materialization pre-check carries NO status filter on
    purpose (spec §2: a voided auto-fed row must never resurrect) --
    `variance()`'s guard must match that exactly, or a cost the operator
    explicitly retracted comes back as live forward-looking spend forever
    (`materialize` correctly refuses to recreate the row it already voided,
    so nothing would ever put it back in `committed` to keep suppressing
    it)."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        saas = next(c for c in cats if c.key == "saas_subscriptions")
        budget = await _make_budget(session, org_id, user_id)

        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("20.00"),
            currency="USD",
            renews_at=date(2026, 3, 1),
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        await materialize(
            session, org_id, user_id, from_date=date(2026, 3, 1), to_date=date(2026, 3, 31)
        )
        materialized = (
            await session.execute(
                select(Expense).where(Expense.org_id == org_id, Expense.link_id == sub.id)
            )
        ).scalar_one()
        await void_expense(session, org_id, user_id, materialized.id)

        result = await variance(session, org_id, budget.id, user_id)

    # No row at all for (saas, period 3): a voided materialized renewal
    # contributes to NONE of actual/committed/projected -- if L3 regressed,
    # the guard would have kept the projection suppressed for the wrong
    # reason (masking the bug) or the row would resurface with a nonzero
    # `projected`, either of which a plain `_row_for` lookup would need to
    # accommodate rather than rule out. Absence of the row is the strongest
    # statement: the voided renewal is invisible everywhere, exactly like
    # it never happened.
    assert not any(
        r["category_id"] == saas.id and r["period_index"] == 3 for r in result["rows"]
    )


async def test_forecast_voided_materialized_renewal_does_not_resurrect(api_env) -> None:
    """L3, `forecast()` side: the same voided row must still report
    `materialized: True` (never fall back to pure `projected`), which is
    what `forecast()`'s no-status-filter guard is for."""
    org_id, user_id = await _org_with_user(api_env)
    today = date.today()
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id)
        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("15.00"),
            currency="USD",
            renews_at=today,
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        await materialize(session, org_id, user_id, from_date=today, to_date=today)
        materialized = (
            await session.execute(
                select(Expense).where(Expense.org_id == org_id, Expense.link_id == sub.id)
            )
        ).scalar_one()
        await void_expense(session, org_id, user_id, materialized.id)

        result = await forecast(session, org_id, user_id, months=1)

    period_key = f"{today.year:04d}-{today.month:02d}"
    rows = [r for r in result["rows"] if r["period_key"] == period_key]
    assert rows
    assert all(r["materialized"] is True for r in rows)


# ---------------------------------------------------------------------------
# voided expenses excluded from every figure
# ---------------------------------------------------------------------------


async def test_voided_expenses_are_excluded_from_every_figure(api_env) -> None:
    """A real (non-voided) confirmed row shares the SAME category/period as
    the two voided ones, so a variance row necessarily exists to check
    against -- proving the voided rows contribute nothing, rather than the
    weaker (and vacuously-passable) claim that no row exists at all."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id)

        kept = await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 12),
            amount=Decimal("7.00"), status="confirmed",
        )
        assert kept.status == "confirmed"
        confirmed = await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 15),
            amount=Decimal("100.00"), status="confirmed",
        )
        expected = await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 20),
            amount=Decimal("40.00"), status="expected",
        )
        await void_expense(session, org_id, user_id, confirmed.id)
        await void_expense(session, org_id, user_id, expected.id)

        var_result = await variance(session, org_id, budget.id, user_id)
        pos_result = await position(session, org_id, budget.id, as_of=date(2026, 12, 31))
        export = await export_rows(session, org_id)
        odoo_export = await odoo_export_rows(session, org_id)

    row = _row_for(var_result["rows"], category_id=hardware.id, period_index=1)
    assert row["actual"] == Decimal("7.00")  # only the kept row, never the voided 100.00
    assert row["committed"] == Decimal("0.00")  # the voided 40.00 never counted
    assert pos_result["ytd_actual"] == Decimal("7.00")
    assert len(export) == 1
    assert export[0]["amount"] == "7.00"
    assert len(odoo_export) == 1
    assert odoo_export[0]["amount"] == "7.00"


# ---------------------------------------------------------------------------
# unconvertible rows: excluded from totals, always disclosed
# ---------------------------------------------------------------------------


async def test_unconvertible_rows_are_excluded_from_totals_and_disclosed(api_env) -> None:
    """Spec §3: a total is never quietly understated. No fx_rate exists for
    COP -> the org's default base (USD), so the COP expense cannot be
    converted -- it must not silently vanish from `unconvertible_count`/
    `unconvertible_by_currency`, and must not corrupt `ytd_actual`."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id)

        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 10),
            amount=Decimal("100.00"), currency="USD", status="confirmed",
        )
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 12),
            amount=Decimal("500000.00"), currency="COP", status="confirmed",
        )

        report = await position(session, org_id, budget.id, as_of=date(2026, 12, 31))

    assert report["ytd_actual"] == Decimal("100.00")
    assert report["unconvertible_count"] == 1
    assert report["unconvertible_by_currency"] == {"COP": "500000.00"}


# ---------------------------------------------------------------------------
# L6: spec §3 disclosure, on ALL of variance()'s and forecast()'s paths --
# previously only position() had a disclosure test, and L1's bug produced a
# variance() double-count that a disclosure test on the CONFIRMED path alone
# would have caught (the auto-fed cost was disclosed once as the expense,
# once again as the resurrected projection).
# ---------------------------------------------------------------------------


async def test_variance_discloses_unconvertible_confirmed_expense(api_env) -> None:
    """A convertible USD row shares the SAME category/period as the
    unconvertible COP one (mirrors the voided-expenses test's rationale),
    so a variance row necessarily exists to check `actual` against, rather
    than the weaker claim that no row exists at all."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id)
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 5),
            amount=Decimal("1.00"), currency="USD", status="confirmed",
        )
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 10),
            amount=Decimal("500000.00"), currency="COP", status="confirmed",
        )

        result = await variance(session, org_id, budget.id, user_id)

    assert result["unconvertible_count"] == 1
    assert result["unconvertible_by_currency"] == {"COP": "500000.00"}
    row = _row_for(result["rows"], category_id=hardware.id, period_index=1)
    assert row["actual"] == Decimal("1.00")  # only the convertible USD row


async def test_variance_discloses_unconvertible_expected_expense(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id)
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 2, 5),
            amount=Decimal("1.00"), currency="USD", status="expected",
        )
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 2, 10),
            amount=Decimal("500000.00"), currency="COP", status="expected",
        )

        result = await variance(session, org_id, budget.id, user_id)

    assert result["unconvertible_count"] == 1
    assert result["unconvertible_by_currency"] == {"COP": "500000.00"}
    row = _row_for(result["rows"], category_id=hardware.id, period_index=2)
    assert row["committed"] == Decimal("1.00")  # only the convertible USD row


async def test_variance_discloses_unconvertible_projection(api_env) -> None:
    """A subscription priced in a currency with no configured rate must be
    excluded from the `projected` sum but still counted/disclosed -- exactly
    like an unconvertible confirmed or expected row. Renewing monthly from
    Mar 1, the subscription lands in every period from March through the
    fiscal year end (10 occurrences, period_index 3..12) -- the budget's
    full-year window, not just the one period a naive read might expect."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        saas = next(c for c in cats if c.key == "saas_subscriptions")
        budget = await _make_budget(session, org_id, user_id)
        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("500000.00"),
            currency="COP",
            renews_at=date(2026, 3, 1),
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        result = await variance(session, org_id, budget.id, user_id)

    assert result["unconvertible_count"] == 10
    assert result["unconvertible_by_currency"] == {"COP": "5000000.00"}
    # None of the 10 occurrences contribute a `projected` figure anywhere --
    # an unconvertible row is excluded from the sum, never guessed at 0 AND
    # silently present as a row (there is nothing else to make a row exist
    # at these category/periods).
    assert not any(
        r["category_id"] == saas.id and r["projected"] != Decimal("0.00")
        for r in result["rows"]
    )


async def test_forecast_discloses_unconvertible_projection(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    today = date.today()
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id)
        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("500000.00"),
            currency="COP",
            renews_at=today,
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        result = await forecast(session, org_id, user_id, months=1)

    assert result["unconvertible_count"] == 1
    assert result["unconvertible_by_currency"] == {"COP": "500000.00"}
    assert all(r["amount_base"] is None for r in result["rows"])


# ---------------------------------------------------------------------------
# division guards
# ---------------------------------------------------------------------------


async def test_variance_pct_with_zero_plan_is_none_not_infinity(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id)
        # committed spend with NO budget_line at all -> planned == 0.
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 2, 10),
            amount=Decimal("50.00"), status="expected",
        )

        result = await variance(session, org_id, budget.id, user_id)

    row = _row_for(result["rows"], category_id=hardware.id, period_index=2)
    assert row["planned"] == Decimal("0.00")
    assert row["variance_pct"] is None


async def test_position_pct_of_plan_is_none_on_zero_plan(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id)
        # No budget_line at all -> total plan is 0.
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 10),
            amount=Decimal("50.00"), status="confirmed",
        )

        result = await position(session, org_id, budget.id, as_of=date(2026, 6, 30))

    assert result["pct_of_plan"] is None
    assert result["ytd_actual"] == Decimal("50.00")


async def test_position_avg_burn_is_zero_before_any_period_has_elapsed(api_env) -> None:
    """Not a raise, not Infinity, and not None either -- a burn rate before
    any period has elapsed is meaningfully zero (mirrors
    `subscriptions.reporting.idle_seats`'s zero-seat guard)."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id)
        budget = await _make_budget(session, org_id, user_id)

        result = await position(session, org_id, budget.id, as_of=date(2025, 12, 31))

    assert result["elapsed_periods"] == 0
    assert result["avg_monthly_burn"] == Decimal("0.00")
    assert result["projected_year_end"] == Decimal("0.00")


# ---------------------------------------------------------------------------
# position: fiscal calendar, never wall-clock month count
# ---------------------------------------------------------------------------


async def test_position_uses_the_fiscal_calendar_not_wall_clock_months(api_env) -> None:
    """A July-start fiscal year reports correctly in January: period 7
    (Jul..Jan), not `1 - 7 = -6` (the wall-clock-month-subtraction bug this
    test exists to catch)."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        # Set fiscal_year_start_month=7 before anything else touches settings.
        from app.modules.budget import fx as fx_module

        settings = await fx_module.get_settings(session, org_id)
        settings.fiscal_year_start_month = 7
        await session.flush()

        await seed_default_categories(session, org_id, user_id)
        budget = await _make_budget(session, org_id, user_id, fiscal_year=2025)

        result = await position(session, org_id, budget.id, as_of=date(2026, 1, 15))

    assert result["elapsed_periods"] == 7


async def test_position_avg_monthly_burn_on_quarterly_budget_is_truly_monthly(api_env) -> None:
    """L5: `avg_monthly_burn` is published as a MONTHLY figure regardless of
    the budget's granularity. On a quarterly budget a "period" is a quarter,
    so dividing YTD actual by elapsed PERIODS (the bug) reports a rate 3x
    too high; $900 spent by the end of Q2 (2 elapsed quarters = 6 elapsed
    months) must read as $150/month, never $450/month. `projected_year_end`
    -- a straight-line doubling of $900 at the half-year mark -- must stay
    $1,800 either way."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id, granularity="quarter")
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 15),
            amount=Decimal("900.00"), status="confirmed",
        )

        result = await position(session, org_id, budget.id, as_of=date(2026, 6, 30))

    assert result["elapsed_periods"] == 2
    assert result["avg_monthly_burn"] == Decimal("150.00")
    assert result["projected_year_end"] == Decimal("1800.00")


async def test_position_projected_year_end_rounds_once_not_after_avg_burn(api_env) -> None:
    """M2: `$900 by end of Q2` (the existing quarterly test) can't catch this
    -- `900 / 6` is exact, so rounding `avg_monthly_burn` first and THEN
    multiplying by 12 happens to agree with rounding once at the end. `$100
    by end of Q1` is NOT exact: `100 / 3 = 33.333...`, which quantizes to
    `avg_monthly_burn=33.33` -- but `33.33 * 12 = 399.96`, three cents short
    of the true straight-line figure. `projected_year_end` must be computed
    from the UNROUNDED quotient (`(100/3) * 12 == 400` exactly) and
    quantized only once, so it reads `400.00`, matching what a reader who
    hand-computes `ytd_actual / elapsed_months * 12` would get."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id, granularity="quarter")
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 15),
            amount=Decimal("100.00"), status="confirmed",
        )

        result = await position(session, org_id, budget.id, as_of=date(2026, 3, 31))

    assert result["elapsed_periods"] == 1
    assert result["avg_monthly_burn"] == Decimal("33.33")
    assert result["projected_year_end"] == Decimal("400.00")


# ---------------------------------------------------------------------------
# forecast
# ---------------------------------------------------------------------------


async def test_forecast_separates_materialized_from_projected(api_env) -> None:
    """`forecast()` walks forward from wall-clock `date.today()` (the
    interface takes no injectable reference date), so the subscription is
    seeded to renew exactly today -- the first, and only relevant,
    occurrence for this test."""
    org_id, user_id = await _org_with_user(api_env)
    today = date.today()
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id)
        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("15.00"),
            currency="USD",
            renews_at=today,
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        before = await forecast(session, org_id, user_id, months=1)
        period_key = f"{today.year:04d}-{today.month:02d}"
        before_rows = [r for r in before["rows"] if r["period_key"] == period_key]
        assert before_rows
        assert all(r["materialized"] is False for r in before_rows)

        await materialize(session, org_id, user_id, from_date=today, to_date=today)

        after = await forecast(session, org_id, user_id, months=1)

    after_rows = [r for r in after["rows"] if r["period_key"] == period_key]
    assert after_rows
    assert all(r["materialized"] is True for r in after_rows)


async def test_forecast_rejects_non_positive_months(api_env) -> None:
    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(BudgetValidationError):
            await forecast(session, org_id, _user_id, months=0)


async def test_forecast_months_spans_exactly_n_calendar_months_including_current(
    api_env,
) -> None:
    """L4: `forecast(months=N)` must span exactly N calendar months
    INCLUDING the current one, not N+1. The bug (`_add_months_end(today,
    months)`) made the window "the rest of this month PLUS N full months" --
    `months=12` returned 13 rows, overstating an annual forecast by one
    month's charge (8.3%) per monthly subscription. A subscription renewing
    TODAY on a monthly cycle produces exactly one occurrence per calendar
    month in the window, so the row count pins the window width directly,
    and the first/last `period_key` pin its exact ends."""
    org_id, user_id = await _org_with_user(api_env)
    today = date.today()
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id)
        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("10.00"),
            currency="USD",
            renews_at=today,
            status="active",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        result = await forecast(session, org_id, user_id, months=12)

    period_keys = sorted({r["period_key"] for r in result["rows"]})
    total = today.year * 12 + (today.month - 1) + 11
    last_year, last_month = divmod(total, 12)
    last_month += 1
    assert len(period_keys) == 12
    assert period_keys[0] == f"{today.year:04d}-{today.month:02d}"
    assert period_keys[-1] == f"{last_year:04d}-{last_month:02d}"


# ---------------------------------------------------------------------------
# exports: money is a string, the pinned Odoo column contract
# ---------------------------------------------------------------------------


async def test_export_rows_money_is_a_string(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 15),
            amount=Decimal("123.45"), status="confirmed",
        )

        rows = await export_rows(session, org_id)

    assert len(rows) == 1
    assert isinstance(rows[0]["amount"], str)
    assert rows[0]["amount"] == "123.45"
    assert isinstance(rows[0]["amount_base"], str)
    assert rows[0]["amount_base"] == "123.45"


async def test_odoo_export_emits_the_pinned_column_contract(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 15),
            amount=Decimal("100.00"), status="confirmed",
        )

        rows = await odoo_export_rows(session, org_id)

    assert len(rows) == 1
    assert list(rows[0]) == [
        "date", "reference", "vendor", "category", "cost_center",
        "description", "amount", "currency", "amount_base", "base_currency",
        "link_type", "link_id",
    ]
    assert rows[0]["amount"] == "100.00"
    assert rows[0]["base_currency"] == "USD"


async def test_odoo_export_defaults_to_confirmed_only(api_env) -> None:
    """I2 money scenario: an org materializes Jan-Dec -- 12 `expected` rows
    per monthly subscription at PROJECTED amounts -- then exports to Odoo at
    month end. The pinned Odoo contract has NO `status` column, so without
    this default Odoo books 12 months of SaaS as real cost for 11 renewals
    that have not happened, at amounts nobody confirmed. `odoo_export_rows`
    must default to CONFIRMED rows only; the general `export_rows` (self-
    describing -- it carries `status` as a column) is unaffected."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 15),
            amount=Decimal("100.00"), status="confirmed",
        )
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 20),
            amount=Decimal("40.00"), status="expected",
        )

        general_export = await export_rows(session, org_id)
        odoo_default = await odoo_export_rows(session, org_id)
        odoo_opt_in = await odoo_export_rows(session, org_id, include_unconfirmed=True)

    # The general-purpose export is untouched -- both rows, `status`
    # self-describes which is which.
    assert len(general_export) == 2
    assert {r["status"] for r in general_export} == {"confirmed", "expected"}

    # The Odoo default excludes the unconfirmed `expected` row entirely.
    assert len(odoo_default) == 1
    assert odoo_default[0]["amount"] == "100.00"

    # The explicit opt-in restores the unconfirmed row.
    assert len(odoo_opt_in) == 2
    assert {r["amount"] for r in odoo_opt_in} == {"100.00", "40.00"}


async def test_export_date_range_filters(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 5),
            amount=Decimal("10.00"), status="confirmed",
        )
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 6, 5),
            amount=Decimal("20.00"), status="confirmed",
        )

        rows = await export_rows(
            session, org_id, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31)
        )

    assert len(rows) == 1
    assert rows[0]["amount"] == "10.00"


# ---------------------------------------------------------------------------
# no-audit-on-view (invariant #4)
# ---------------------------------------------------------------------------


async def test_reports_write_no_audit_event(api_env) -> None:
    """Only the export ROUTES audit (Task 12) -- the reporting layer itself
    writes nothing."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id)
        hardware = next(c for c in cats if c.key == "hardware")
        budget = await _make_budget(session, org_id, user_id)
        await _make_expense(
            session, org_id, user_id, category_id=hardware.id, expense_date=date(2026, 1, 15),
            amount=Decimal("100.00"), status="confirmed",
        )

    async def _audit_count() -> int:
        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
            return len(
                (await s.execute(select(AuditEvent.id).where(AuditEvent.org_id == org_id)))
                .scalars()
                .all()
            )

    before = await _audit_count()
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await variance(session, org_id, budget.id, user_id)
        await position(session, org_id, budget.id, as_of=date(2026, 6, 30))
        await forecast(session, org_id, user_id, months=3)
        await export_rows(session, org_id)
        await odoo_export_rows(session, org_id)
    after = await _audit_count()

    assert after == before


async def test_reports_write_no_user_audit_event_on_a_fresh_unseeded_org(api_env) -> None:
    """N1 regression: `test_reports_write_no_audit_event` above pre-seeds at
    its own top (mirroring the old `seed_default_categories` call before the
    fix), which means it could never have caught a per-row
    `budget.category_created` written by the LAZY seed inside `variance`/
    `forecast` themselves -- by the time it asserts, the org was already
    seeded and neither report needed to insert anything.

    This variant does NOT pre-seed. `budget_id` doesn't even exist yet
    either, so `variance()` 404s before it can seed -- `forecast()` is the
    one that actually triggers the org's first-ever lazy seed here. The
    rule still holds: no `budget.category_created` event may appear, and at
    most one system-attributed `budget.categories_seeded`."""
    org_id, user_id = await _org_with_user(api_env)

    async def _events_of(action: str) -> list[AuditEvent]:
        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
            return list(
                (
                    await s.execute(
                        select(AuditEvent).where(
                            AuditEvent.org_id == org_id, AuditEvent.action == action
                        )
                    )
                )
                .scalars()
                .all()
            )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await forecast(session, org_id, user_id, months=3)

    assert await _events_of("budget.category_created") == []
    seeded = await _events_of("budget.categories_seeded")
    assert len(seeded) == 1
    assert seeded[0].actor_type == "system"
    assert seeded[0].actor_id is None

    # A second forecast call must not add anything -- the org is now seeded.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await forecast(session, org_id, user_id, months=3)
    assert await _events_of("budget.category_created") == []
    assert len(await _events_of("budget.categories_seeded")) == 1


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session, mutation-verified
# ---------------------------------------------------------------------------


async def test_tenancy_variance_excludes_other_orgs_expenses(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org B's confirmed expense must never appear in org A's `variance()`,
    even on a raw BYPASSRLS admin session where org B's rows are fully
    visible. `variance()`'s `Expense.org_id == org_id` predicate on its own
    expense scan is the SOLE guard here -- there is no RLS backstop on this
    session. Org B's category/expense are seeded INSIDE this test (not
    relying on residue from another test in the same file) so the guard is
    mutation-verifiable IN ISOLATION."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_b = ExpenseCategory(
            org_id=org_b, key="hardware", name="Hardware", system_key="hardware", created_by=user_b
        )
        session.add(cat_b)
        await session.flush()

        expense_b = Expense(
            org_id=org_b,
            expense_date=date(2026, 1, 15),
            amount=Decimal("9999.00"),
            currency="USD",
            fx_rate=Decimal("1"),
            amount_base=Decimal("9999.00"),
            category_id=cat_b.id,
            description="org B hardware",
            source="manual",
            status="confirmed",
            entered_by=user_b,
        )
        session.add(expense_b)
        await session.commit()
        cat_b_id = cat_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await seed_default_categories(session, org_a, user_a)
        budget_a = await _make_budget(session, org_a, user_a)
        await session.commit()
        budget_a_id = budget_a.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        result = await variance(session, org_a, budget_a_id, user_a)

    seen_category_ids = {row["category_id"] for row in result["rows"]}
    assert cat_b_id not in seen_category_ids
    total_actual = sum((row["actual"] for row in result["rows"]), Decimal("0.00"))
    assert total_actual == Decimal("0.00")


async def test_tenancy_position_excludes_other_orgs_expenses(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_b = ExpenseCategory(
            org_id=org_b, key="hardware", name="Hardware", system_key="hardware", created_by=user_b
        )
        session.add(cat_b)
        await session.flush()
        session.add(
            Expense(
                org_id=org_b,
                expense_date=date(2026, 1, 15),
                amount=Decimal("9999.00"),
                currency="USD",
                fx_rate=Decimal("1"),
                amount_base=Decimal("9999.00"),
                category_id=cat_b.id,
                description="org B hardware",
                source="manual",
                status="confirmed",
                entered_by=user_b,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await seed_default_categories(session, org_a, user_a)
        budget_a = await _make_budget(session, org_a, user_a)
        await session.commit()
        budget_a_id = budget_a.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        result = await position(session, org_a, budget_a_id, as_of=date(2026, 12, 31))

    assert result["ytd_actual"] == Decimal("0.00")


async def test_tenancy_forecast_excludes_other_orgs_materialized_row_from_the_guard(
    api_env, admin_engine: AsyncEngine
) -> None:
    """L7: `forecast()` is org-wide, not budget-scoped -- its `materialized`
    flag is the one figure this function computes that could be corrupted
    by another org's data. Org B's row is planted directly (via a raw
    BYPASSRLS admin session -- `materialize` itself would never let a
    cross-org `link_id` in) keyed on ORG A's OWN subscription id, seeded
    INSIDE this test, so `_materialized_keys`'s `Expense.org_id == org_id`
    predicate is the SOLE guard standing between it and org A's report --
    mutation-verified in isolation below."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    today = date.today()
    period_key = f"{today.year:04d}-{today.month:02d}"

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await seed_default_categories(session, org_a, user_a)
        sub_a = Subscription(
            org_id=org_a,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("15.00"),
            currency="USD",
            renews_at=today,
            status="active",
            created_by=user_a,
        )
        session.add(sub_a)
        await session.flush()
        sub_a_id = sub_a.id
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_b = await create_category(session, org_b, user_b, key="saas_subscriptions", name="SaaS")
        session.add(
            Expense(
                org_id=org_b,
                expense_date=today,
                amount=Decimal("15.00"),
                currency="USD",
                fx_rate=Decimal("1"),
                amount_base=Decimal("15.00"),
                category_id=cat_b.id,
                description="org B row planted on org A's subscription id",
                source="subscription",
                link_type="subscription",
                link_id=sub_a_id,
                period_key=period_key,
                status="expected",
                entered_by=user_b,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        result = await forecast(session, org_a, user_a, months=1)

    rows = [r for r in result["rows"] if r["period_key"] == period_key]
    assert rows
    assert all(r["materialized"] is False for r in rows)


async def test_tenancy_export_rows_excludes_other_orgs_expenses(
    api_env, admin_engine: AsyncEngine
) -> None:
    """L7: `export_rows` is one of the two artifacts that "physically leave
    the building" -- org B's expense, seeded INSIDE this test, must never
    appear in org A's export."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_b = ExpenseCategory(
            org_id=org_b, key="hardware", name="Hardware", system_key="hardware", created_by=user_b
        )
        session.add(cat_b)
        await session.flush()
        session.add(
            Expense(
                org_id=org_b,
                expense_date=date(2026, 1, 15),
                amount=Decimal("9999.00"),
                currency="USD",
                fx_rate=Decimal("1"),
                amount_base=Decimal("9999.00"),
                category_id=cat_b.id,
                description="org B hardware",
                source="manual",
                status="confirmed",
                entered_by=user_b,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_a = await create_category(session, org_a, user_a, key="hardware", name="Hardware")
        await _make_expense(
            session, org_a, user_a, category_id=cat_a.id, expense_date=date(2026, 1, 15),
            amount=Decimal("7.00"), status="confirmed",
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = await export_rows(session, org_a)

    assert len(rows) == 1
    assert rows[0]["amount"] == "7.00"


async def test_tenancy_odoo_export_rows_excludes_other_orgs_expenses(
    api_env, admin_engine: AsyncEngine
) -> None:
    """L7, the Odoo variant -- same proof as `export_rows` above, seeded
    independently so this test is not relying on residue from the other."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_b = ExpenseCategory(
            org_id=org_b, key="hardware", name="Hardware", system_key="hardware", created_by=user_b
        )
        session.add(cat_b)
        await session.flush()
        session.add(
            Expense(
                org_id=org_b,
                expense_date=date(2026, 1, 15),
                amount=Decimal("9999.00"),
                currency="USD",
                fx_rate=Decimal("1"),
                amount_base=Decimal("9999.00"),
                category_id=cat_b.id,
                description="org B hardware",
                source="manual",
                status="confirmed",
                entered_by=user_b,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_a = await create_category(session, org_a, user_a, key="hardware", name="Hardware")
        await _make_expense(
            session, org_a, user_a, category_id=cat_a.id, expense_date=date(2026, 1, 15),
            amount=Decimal("7.00"), status="confirmed",
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = await odoo_export_rows(session, org_a)

    assert len(rows) == 1
    assert rows[0]["amount"] == "7.00"


async def test_export_suppresses_unresolvable_vendor_id_and_label(
    api_env, admin_engine: AsyncEngine
) -> None:
    """The Phase-12 `group_id` lesson: a `vendor_id` that does not resolve
    in THIS org (here, a raw cross-org id planted directly via a
    BYPASSRLS session -- `create_expense` itself would refuse this at
    write time, so simulating it requires bypassing the service layer, the
    same way a deleted vendor row would leave a stale id behind) must be
    suppressed together with its label -- emitting the raw id while
    blanking the name would still leak another tenant's identifier."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        vendor_b = Vendor(org_id=org_b, name="Org B Vendor", created_by=user_b)
        session.add(vendor_b)
        await session.flush()
        vendor_b_id = vendor_b.id

        cat_a = await create_category(session, org_a, user_a, key="hardware", name="Hardware")
        expense_a = Expense(
            org_id=org_a,
            expense_date=date(2026, 1, 15),
            amount=Decimal("100.00"),
            currency="USD",
            fx_rate=Decimal("1"),
            amount_base=Decimal("100.00"),
            vendor_id=vendor_b_id,  # cross-org reference, planted directly
            category_id=cat_a.id,
            description="expense with an unresolvable vendor",
            source="manual",
            status="confirmed",
            entered_by=user_a,
        )
        session.add(expense_a)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = await export_rows(session, org_a)

    assert len(rows) == 1
    assert rows[0]["vendor_id"] is None
    assert rows[0]["vendor"] is None
    # The raw id must not have leaked into any OTHER field either.
    assert str(vendor_b_id) not in str(rows[0])


async def test_variance_unknown_budget_raises_not_found(api_env) -> None:
    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(BudgetNotFoundError):
            await variance(
                session, org_id, UUID("00000000-0000-0000-0000-000000000000"), _user_id
            )
