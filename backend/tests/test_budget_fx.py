"""FX resolution and conversion (spec §3).

Four rules, each a real failure mode if broken:
  - pick the greatest effective_date <= d (never a later rate)
  - never extrapolate backwards from a later rate
  - never invert the opposite pair
  - base -> base is exactly 1 with no lookup
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Connection, event, func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.util import await_only

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import BudgetSettings, FxRate, User
from app.modules.budget.errors import BudgetValidationError
from app.modules.budget.fx import convert, convert_for, get_settings, resolve_rate
from app.modules.budget.rates import settings_for_write


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Create a throwaway org plus an `org-admin` user in it; return
    `(org_id, user_id)`. Later budget-module tests reuse this shape."""
    org_id = await api_env.create_org("fx")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _seed_rate(session, org_id, user_id, frm, rate, on) -> None:
    session.add(
        FxRate(
            org_id=org_id, from_currency=frm, to_currency="USD",
            rate=Decimal(rate), effective_date=on, created_by=user_id,
        )
    )
    await session.flush()


def test_convert_rounds_half_up_once() -> None:
    assert convert(Decimal("10.005"), Decimal("1")) == Decimal("10.01")
    assert convert(Decimal("100"), Decimal("0.00456")) == Decimal("0.46")


def test_convert_rounds_once_not_twice() -> None:
    """Discriminates a true single-rounding implementation from a
    quantize-then-multiply one: both cases below pass under the existing
    two assertions but give a different (wrong) result if `amount` or
    `rate` is quantized before the multiply (F3)."""
    assert convert(Decimal("0.125"), Decimal("10")) == Decimal("1.25")
    assert convert(Decimal("1234.567"), Decimal("3")) == Decimal("3703.70")


def test_convert_accepts_the_numeric_14_2_boundary() -> None:
    assert convert(Decimal("999999999999.99"), Decimal("1")) == Decimal("999999999999.99")


def test_convert_raises_over_the_numeric_14_2_boundary() -> None:
    """A high-nominal base currency (COP, IDR, VND) can push the product over
    `expense.amount_base`'s Numeric(14,2) ceiling; this must raise a typed
    validation error, not a raw DB `numeric field overflow` 500 (F4)."""
    with pytest.raises(BudgetValidationError):
        convert(Decimal("999999999999.99"), Decimal("1.01"))


async def test_settings_default_to_the_documented_values_and_write_nothing(api_env) -> None:
    """Spec §2's defaults. Until plan 2026-10-26 Task 8 the row was created
    lazily on this first read; `get_settings` is pure now -- a client's row is
    created by the first change to it (`rates.settings_for_write`) -- so the
    defaults come back unsaved and nothing is written."""
    org_id = await api_env.create_org("fx")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        settings = await get_settings(session, org_id, client_id=org_id)
    assert settings.base_currency == "USD"
    assert settings.fiscal_year_start_month == 1
    assert settings.updated_by is None  # never operator-set
    assert settings.updated_at is None
    async with AsyncSession(api_env.admin_engine) as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(BudgetSettings)
                .where(BudgetSettings.org_id == org_id)
            )
        ).scalar_one()
    assert count == 0


async def test_resolve_picks_greatest_effective_date_not_later_than_target(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await _seed_rate(session, org_id, user_id, "COP", "4000", date(2026, 1, 1))
        await _seed_rate(session, org_id, user_id, "COP", "4200", date(2026, 6, 1))
        await _seed_rate(session, org_id, user_id, "COP", "4500", date(2026, 9, 1))
        rate = await resolve_rate(session, org_id, "COP", date(2026, 7, 15), client_id=org_id)
    assert rate == Decimal("4200.00000000")


async def test_no_backward_extrapolation_from_a_later_rate(api_env) -> None:
    """A rate effective AFTER the date must not be used -- returning 4200 here
    would silently value a January expense at a June rate."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await _seed_rate(session, org_id, user_id, "COP", "4200", date(2026, 6, 1))
        rate = await resolve_rate(session, org_id, "COP", date(2026, 1, 15), client_id=org_id)
    assert rate is None


async def test_pair_is_not_inverted(api_env) -> None:
    """Only USD->? is seeded; COP->USD must stay unresolvable rather than
    silently using 1/rate."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        session.add(
            FxRate(
                org_id=org_id, from_currency="USD", to_currency="COP",
                rate=Decimal("4200"), effective_date=date(2026, 1, 1),
                created_by=user_id,
            )
        )
        await session.flush()
        rate = await resolve_rate(session, org_id, "COP", date(2026, 6, 1), client_id=org_id)
    assert rate is None


async def test_base_to_base_is_one_without_a_lookup(api_env) -> None:
    org_id, _ = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rate, base = await convert_for(
            session, org_id, Decimal("50.00"), "USD", date(2026, 1, 1), client_id=org_id
        )
    assert rate == Decimal("1")
    assert base == Decimal("50.00")


async def test_unresolvable_currency_yields_none_none(api_env) -> None:
    org_id, _ = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rate, base = await convert_for(
            session, org_id, Decimal("50.00"), "COP", date(2026, 1, 1), client_id=org_id
        )
    assert rate is None
    assert base is None


async def test_tenancy_fx_rate_lookup_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org B's rate must never resolve for org A.

    Runs on a raw AsyncSession(admin_engine) -- its_admin is BYPASSRLS
    (tests/conftest.py:111-117) -- so org B's row is fully visible on this
    connection and resolve_rate's own org_id predicate is the SOLE guard.
    Under org_session(engine=app_engine) RLS would mask it and this test
    would pass with the predicate deleted.
    """
    org_a, _user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await _seed_rate(session, org_b, user_b, "COP", "4200", date(2026, 1, 1))
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rate = await resolve_rate(session, org_a, "COP", date(2026, 6, 1), client_id=org_a)
    assert rate is None


async def test_tenancy_get_settings_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org B's budget_settings row must never resolve for org A.

    Runs on a raw AsyncSession(admin_engine) -- its_admin is BYPASSRLS
    (tests/conftest.py:111-117) -- so org B's row is fully visible on this
    connection and get_settings's own org_id predicate is the SOLE guard.
    Only org B's row is seeded (org A's is never created) so a missing
    predicate returns org B's row outright rather than tripping
    MultipleResultsFound -- see F1 in the Task-5 fix directive for why that
    ordering artifact from a whole-file run is not itself a tenancy
    assertion. It asks for ORG B's internal client (its id is org B's; plan
    2026-10-26 Task 8, lesson 2): the client predicate matches org B's row,
    so only the org predicate keeps it out.

    Money impact: with the predicate gone, get_settings(org_a) would return
    org B's base_currency; every resolve_rate for org A then filters
    to_currency == <org B's base>, matches nothing, and every
    foreign-currency expense records amount_base = NULL, understating org
    A's real spend.
    """
    org_a, _user_a = await _org_with_user(api_env)
    org_b, _user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            BudgetSettings(
                org_id=org_b, client_id=org_b,
                base_currency="COP",
                fiscal_year_start_month=1,
                updated_by=None,
            )
        )
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        settings = await get_settings(session, org_a, client_id=org_b)
    assert settings.base_currency == "USD"


async def test_tenancy_settings_for_write_reselect_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """The F2 get-or-create's re-SELECT must scope to the caller's org. Until
    plan 2026-10-26 Task 8 it lived in `get_settings`; reads are pure now and
    the get-or-create is the writers' (`rates.settings_for_write`), so this
    pins the same predicate where it now lives.

    Org B's (non-USD) row is seeded and committed first. Org A's writer is
    then asked for ORG B's internal client (its id is org B's; lesson 2):
    its `ON CONFLICT DO NOTHING` meets org B's row on the primary key and
    inserts nothing, so the re-SELECT is what decides what the writer goes on
    to change -- and the client predicate matches org B's row. Only the org
    predicate keeps it out: the writer finds nothing, rather than handing
    back (to be changed) another organisation's settings. In production the
    module guard refuses another org's client first (`require_budget`); this
    is the predicate under it."""
    org_b, _user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            BudgetSettings(
                org_id=org_b, client_id=org_b,
                base_currency="COP",
                fiscal_year_start_month=1,
                updated_by=None,
            )
        )
        await session.commit()

    org_a, _user_a = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(NoResultFound):
            await settings_for_write(session, org_a, org_b)
    async with AsyncSession(admin_engine) as session:
        theirs = (
            await session.execute(
                select(BudgetSettings.base_currency).where(BudgetSettings.client_id == org_b)
            )
        ).scalar_one()
    assert theirs == "COP"


async def test_settings_for_write_concurrent_first_writes_do_not_race(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Two colleagues both make a client's very first settings change at
    once. `client_id` is the PK, so a naive check-then-act would give the
    loser an `IntegrityError` on `budget_settings_pkey` that aborts its
    transaction; the fix is `ON CONFLICT DO NOTHING` + a locking re-select
    (F2). Until plan 2026-10-26 Task 8 this race was `get_settings`'s, which
    created the row on the first READ; reads are pure now and the row is
    made by the first write (`rates.settings_for_write`), so the race is
    pinned there.

    A DETERMINISTIC interleave rather than hoping `asyncio.gather` overlaps
    two sessions at the right instant (G1 in the Task-5 fix directive #2).
    Session B inserts and commits the same client's row inside the window a
    check-then-act lives in, keyed on session A's FIRST statement that
    touches `budget_settings`:

      - if it is a SELECT, B commits right AFTER it -- the check has just
        missed and the act has not happened yet, so a check-then-act's
        INSERT then hits B's key and raises `IntegrityError`;
      - if it is an INSERT, B commits right BEFORE it -- so an insert
        without `ON CONFLICT` raises the same, and the real
        `ON CONFLICT DO NOTHING` finds B's row and hands it back locked.

    Whichever comes first, and only once. B always commits before A has
    written anything, so B never waits on an uncommitted key of A's and the
    test cannot hang. The hook is on A's connection (cursor events), not on
    `AsyncSession.execute`, so a check made with `session.get` and an
    insert made by a `flush` are inside the window too.

    Money impact: the loser's transaction aborts, and with it the change of
    base currency or fiscal year it carried."""
    org_id, _ = await _org_with_user(api_env)
    raced: list[str] = []

    async def b_wins() -> None:
        async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
            session_b.add(
                BudgetSettings(
                    org_id=org_id,
                    client_id=org_id,
                    base_currency="EUR",
                    fiscal_year_start_month=1,
                    updated_by=None,
                )
            )
            await session_b.commit()

    def first_touch(statement: str, kind: str) -> bool:
        return (
            not raced
            and "budget_settings" in statement
            and statement.lstrip().upper().startswith(kind)
        )

    def before_a_statement(
        conn: Connection, cursor: object, statement: str,
        parameters: object, context: object, executemany: bool,
    ) -> None:
        if first_touch(statement, "INSERT"):
            raced.append("before A's INSERT")
            await_only(b_wins())

    def after_a_statement(
        conn: Connection, cursor: object, statement: str,
        parameters: object, context: object, executemany: bool,
    ) -> None:
        if first_touch(statement, "SELECT"):
            raced.append("after A's SELECT")
            await_only(b_wins())

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        connection = (await session_a.connection()).sync_connection
        event.listen(connection, "before_cursor_execute", before_a_statement)
        event.listen(connection, "after_cursor_execute", after_a_statement)
        settings = await settings_for_write(session_a, org_id, org_id)
        await session_a.commit()

    assert raced
    assert settings.base_currency == "EUR"  # B's row, locked and handed back

    async with AsyncSession(admin_engine) as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(BudgetSettings)
                .where(BudgetSettings.org_id == org_id)
            )
        ).scalar_one()
    assert count == 1
