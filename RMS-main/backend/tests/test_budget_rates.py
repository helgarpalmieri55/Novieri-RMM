"""FX-rate CRUD + the base-currency change guard (spec §3, §8).

Covers: `create_rate` always takes `to_currency` from the org's settings,
never from caller input; a duplicate `(from_currency, to_currency,
effective_date)` is rejected as a typed error, not a raw IntegrityError;
`update_rate` only ever touches `rate`; every mutation audits; and the
base-currency rule -- free before any converted expense, blocked once one
exists, proceeds with the explicit acknowledgement, never retro-converts
history, and a voided expense does not block. Tenancy on the rate
lookup/update path and on the base-currency guard is covered with
load-bearing tests on a raw BYPASSRLS admin session (spec §8).
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, Expense, FxRate, User
from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.fx import get_settings
from app.modules.budget.rates import (
    create_rate,
    list_rates,
    set_base_currency,
    set_fiscal_year_start,
    update_rate,
)
from app.modules.budget.refdata import create_category


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Create a throwaway org plus an `org-admin` user in it; return
    `(org_id, user_id)`. Mirrors test_budget_fx.py / test_budget_refdata.py's
    helper of the same name/shape -- not imported across test modules, per
    convention."""
    org_id = await api_env.create_org("rates")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _seed_converted_expense(
    session: AsyncSession, org_id: UUID, user_id: UUID, *, status: str = "confirmed"
) -> Expense:
    """A real expense row carrying a non-NULL `amount_base` -- the trigger
    condition for spec §3's base-currency guard. Needs a real category
    (FK, NOT NULL)."""
    cat = await create_category(session, org_id, user_id, key="hardware", name="Hardware")
    exp = Expense(
        org_id=org_id,
        expense_date=date(2026, 1, 15),
        amount=Decimal("100.00"),
        currency="EUR",
        fx_rate=Decimal("1.10000000"),
        amount_base=Decimal("110.00"),
        category_id=cat.id,
        source="manual",
        status=status,
        entered_by=user_id,
    )
    session.add(exp)
    await session.flush()
    return exp


# ---------------------------------------------------------------------------
# create_rate
# ---------------------------------------------------------------------------


async def test_create_rate_takes_to_currency_from_settings_not_caller(api_env) -> None:
    """The interface takes no `to_currency` parameter at all -- this proves
    the persisted row's `to_currency` matches the org's actual base
    currency, which only `get_settings` can supply."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        settings = await get_settings(session, org_id)
        assert settings.base_currency == "USD"
        rate = await create_rate(
            session,
            org_id,
            user_id,
            from_currency="EUR",
            rate=Decimal("1.10000000"),
            effective_date=date(2026, 1, 1),
        )
    assert rate.from_currency == "EUR"
    assert rate.to_currency == "USD"
    assert rate.rate == Decimal("1.10000000")
    assert rate.effective_date == date(2026, 1, 1)
    assert rate.org_id == org_id


async def test_create_rate_uses_the_currently_set_base_currency(api_env) -> None:
    """After the base currency changes, a NEW rate must target the NEW
    base -- never a value baked in at some earlier point."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await set_base_currency(session, org_id, user_id, base_currency="COP")
        rate = await create_rate(
            session,
            org_id,
            user_id,
            from_currency="EUR",
            rate=Decimal("4500.00000000"),
            effective_date=date(2026, 1, 1),
        )
    assert rate.to_currency == "COP"


async def test_create_rate_audits(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        rate = await create_rate(
            session,
            org_id,
            user_id,
            from_currency="EUR",
            rate=Decimal("1.10000000"),
            effective_date=date(2026, 1, 1),
        )
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.fx_rate_created",
                )
            )
        ).scalar_one()
    assert event.target_type == "fx_rate"
    assert event.target_id == str(rate.id)
    assert event.actor_type == "user"
    assert event.actor_id == user_id
    assert event.meta["from_currency"] == "EUR"
    assert event.meta["to_currency"] == "USD"
    assert event.meta["effective_date"] == "2026-01-01"


async def test_duplicate_rate_in_one_org_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await create_rate(
            session,
            org_id,
            user_id,
            from_currency="EUR",
            rate=Decimal("1.10000000"),
            effective_date=date(2026, 1, 1),
        )
        with pytest.raises(BudgetValidationError):
            await create_rate(
                session,
                org_id,
                user_id,
                from_currency="EUR",
                rate=Decimal("1.20000000"),
                effective_date=date(2026, 1, 1),
            )


async def test_create_rate_concurrent_duplicate_raises_typed_error_not_integrity_error(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Two colleagues both try to record the same day's EUR->USD rate at
    once. The pre-check in `create_rate` is a fast path, not the sole
    guard: two concurrent calls for the exact same `(org_id, from_currency,
    to_currency, effective_date)` can both miss it before either has
    written. The fix mirrors `fx.get_settings` (Task-5 finding F2): the
    actual insert goes through `ON CONFLICT DO NOTHING` against
    `uq_fx_rate_org_pair_date` -- but unlike `get_settings`'s
    get-or-create shape, a duplicate rate is a genuine caller error, so the
    race loser must raise the SAME typed `BudgetValidationError` the
    pre-check raises, not silently return someone else's rate or 500 on a
    raw `IntegrityError`.

    Deterministic interleave, same technique as
    test_get_settings_concurrent_first_calls_do_not_race in
    test_budget_fx.py: session A's pre-check SELECT against `fx_rate` is
    intercepted, and right after it returns (a miss -- nothing exists
    yet), session B inserts and commits the EXACT row session A is about
    to try to create."""
    org_id, user_id = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "fx_rate.id" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    winner = FxRate(
                        org_id=org_id,
                        from_currency="EUR",
                        to_currency="USD",
                        rate=Decimal("1.05000000"),
                        effective_date=date(2026, 2, 1),
                        created_by=user_id,
                    )
                    session_b.add(winner)
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        with pytest.raises(BudgetValidationError, match="fx rate already exists"):
            await create_rate(
                session_a,
                org_id,
                user_id,
                from_currency="EUR",
                rate=Decimal("1.10000000"),
                effective_date=date(2026, 2, 1),
            )
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(FxRate).where(
                        FxRate.org_id == org_id,
                        FxRate.from_currency == "EUR",
                        FxRate.effective_date == date(2026, 2, 1),
                    )
                )
            )
            .scalars()
            .all()
        )
    # Exactly one row exists -- session B's, not a duplicate, and session A
    # never got a row of its own written.
    assert len(rows) == 1
    assert rows[0].rate == Decimal("1.05000000")


async def test_same_rate_pair_and_date_in_two_orgs_is_fine(api_env) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_a, engine=api_env.app_engine) as session:
        rate_a = await create_rate(
            session,
            org_a,
            user_a,
            from_currency="EUR",
            rate=Decimal("1.10000000"),
            effective_date=date(2026, 1, 1),
        )
    async with org_session(org_b, engine=api_env.app_engine) as session:
        rate_b = await create_rate(
            session,
            org_b,
            user_b,
            from_currency="EUR",
            rate=Decimal("1.15000000"),
            effective_date=date(2026, 1, 1),
        )
    assert rate_a.from_currency == rate_b.from_currency == "EUR"
    assert rate_a.effective_date == rate_b.effective_date
    assert rate_a.id != rate_b.id


async def test_a_different_effective_date_is_not_a_duplicate(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await create_rate(
            session,
            org_id,
            user_id,
            from_currency="EUR",
            rate=Decimal("1.10000000"),
            effective_date=date(2026, 1, 1),
        )
        second = await create_rate(
            session,
            org_id,
            user_id,
            from_currency="EUR",
            rate=Decimal("1.12000000"),
            effective_date=date(2026, 2, 1),
        )
    assert second.effective_date == date(2026, 2, 1)


# ---------------------------------------------------------------------------
# list_rates
# ---------------------------------------------------------------------------


async def test_list_rates_returns_only_this_orgs_rates(api_env) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_a, engine=api_env.app_engine) as session:
        await create_rate(
            session, org_a, user_a,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
    async with org_session(org_b, engine=api_env.app_engine) as session:
        await create_rate(
            session, org_b, user_b,
            from_currency="EUR", rate=Decimal("1.20000000"), effective_date=date(2026, 1, 1),
        )
        rates_b = await list_rates(session, org_b)
    assert len(rates_b) == 1
    assert rates_b[0].org_id == org_b


async def test_list_rates_filters_by_from_currency(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
        await create_rate(
            session, org_id, user_id,
            from_currency="GBP", rate=Decimal("1.30000000"), effective_date=date(2026, 1, 1),
        )
        eur_only = await list_rates(session, org_id, from_currency="EUR")
    assert len(eur_only) == 1
    assert eur_only[0].from_currency == "EUR"


async def test_list_rates_is_ordered_by_currency_then_effective_date_desc(api_env) -> None:
    """A list-equality assertion on an unordered query is a landmine (the
    prior-task lesson) -- `list_rates` has an explicit ORDER BY, and this
    test asserts against that exact order rather than insertion order."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await create_rate(
            session, org_id, user_id,
            from_currency="GBP", rate=Decimal("1.30000000"), effective_date=date(2026, 1, 1),
        )
        await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
        await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.12000000"), effective_date=date(2026, 3, 1),
        )
        rates = await list_rates(session, org_id)
    assert [(r.from_currency, r.effective_date) for r in rates] == [
        ("EUR", date(2026, 3, 1)),
        ("EUR", date(2026, 1, 1)),
        ("GBP", date(2026, 1, 1)),
    ]


# ---------------------------------------------------------------------------
# update_rate
# ---------------------------------------------------------------------------


async def test_update_rate_changes_the_rate_value(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        rate = await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
        updated = await update_rate(
            session, org_id, user_id, rate.id, {"rate": Decimal("1.15000000")}
        )
    assert updated.rate == Decimal("1.15000000")
    # identifying fields are untouched
    assert updated.from_currency == "EUR"
    assert updated.to_currency == "USD"
    assert updated.effective_date == date(2026, 1, 1)


async def test_update_rate_ignores_from_currency_and_effective_date(api_env) -> None:
    """`fields` may carry extra keys (e.g. a caller passing the whole
    payload back); only `rate` is ever applied."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        rate = await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
        updated = await update_rate(
            session,
            org_id,
            user_id,
            rate.id,
            {
                "rate": Decimal("1.15000000"),
                "from_currency": "GBP",
                "effective_date": date(2099, 1, 1),
            },
        )
    assert updated.rate == Decimal("1.15000000")
    assert updated.from_currency == "EUR"
    assert updated.effective_date == date(2026, 1, 1)


async def test_update_rate_rejects_null_rate(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        rate = await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
        with pytest.raises(BudgetValidationError):
            await update_rate(session, org_id, user_id, rate.id, {"rate": None})


async def test_update_rate_on_unknown_id_raises_not_found(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        from uuid_extensions import uuid7

        with pytest.raises(BudgetNotFoundError):
            await update_rate(session, org_id, user_id, uuid7(), {"rate": Decimal("1.0")})


async def test_update_rate_audits_only_the_changed_field_names(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        rate = await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
        # same value + a no-op key -- must not report "rate" as changed
        await update_rate(
            session, org_id, user_id, rate.id, {"rate": Decimal("1.10000000")}
        )
        event = (
            await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.fx_rate_updated",
                )
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert event.meta["changed_fields"] == []
    assert event.target_type == "fx_rate"
    assert event.target_id == str(rate.id)


async def test_update_rate_reports_the_changed_field(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        rate = await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
        await update_rate(session, org_id, user_id, rate.id, {"rate": Decimal("1.25000000")})
        event = (
            await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.fx_rate_updated",
                )
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert event.meta["changed_fields"] == ["rate"]


# ---------------------------------------------------------------------------
# base-currency change rule (spec §3) -- the four required cases
# ---------------------------------------------------------------------------


async def test_base_currency_change_is_free_before_any_converted_expense(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        settings = await set_base_currency(session, org_id, user_id, base_currency="COP")
    assert settings.base_currency == "COP"
    assert settings.updated_by == user_id


async def test_base_currency_change_is_blocked_once_an_expense_is_converted(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await _seed_converted_expense(session, org_id, user_id)
        with pytest.raises(BudgetValidationError):
            await set_base_currency(session, org_id, user_id, base_currency="COP")
        # the settings row must be untouched by the rejected attempt
        settings = await get_settings(session, org_id)
    assert settings.base_currency == "USD"
    assert settings.updated_by is None


async def test_base_currency_change_proceeds_with_explicit_acknowledgement(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await _seed_converted_expense(session, org_id, user_id)
        settings = await set_base_currency(
            session, org_id, user_id, base_currency="COP", acknowledge_mixed_basis=True
        )
    assert settings.base_currency == "COP"
    assert settings.updated_by == user_id


async def test_base_currency_change_does_not_retro_convert_history(api_env) -> None:
    """Spec §3: stored rows keep the basis they were converted at."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        exp = await _seed_converted_expense(session, org_id, user_id)
        before_rate, before_base = exp.fx_rate, exp.amount_base
        await set_base_currency(
            session, org_id, user_id, base_currency="COP", acknowledge_mixed_basis=True
        )
        await session.refresh(exp)
    assert exp.fx_rate == before_rate
    assert exp.amount_base == before_base
    assert exp.currency == "EUR"


async def test_a_voided_expense_does_not_block_the_change(api_env) -> None:
    """The rule says NON-void -- a voided converted expense must not
    trigger the guard even without the acknowledgement."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await _seed_converted_expense(session, org_id, user_id, status="void")
        settings = await set_base_currency(session, org_id, user_id, base_currency="COP")
    assert settings.base_currency == "COP"


async def test_an_expected_expense_with_amount_base_still_blocks(api_env) -> None:
    """"non-void" includes both "expected" and "confirmed" -- only "void"
    is exempt."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await _seed_converted_expense(session, org_id, user_id, status="expected")
        with pytest.raises(BudgetValidationError):
            await set_base_currency(session, org_id, user_id, base_currency="COP")


async def test_base_currency_change_audits_old_and_new_codes(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await set_base_currency(session, org_id, user_id, base_currency="COP")
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.base_currency_changed",
                )
            )
        ).scalar_one()
    assert event.meta["old"] == "USD"
    assert event.meta["new"] == "COP"
    assert event.actor_type == "user"
    assert event.actor_id == user_id


# ---------------------------------------------------------------------------
# set_fiscal_year_start
# ---------------------------------------------------------------------------


async def test_set_fiscal_year_start_updates_settings(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        settings = await set_fiscal_year_start(session, org_id, user_id, start_month=4)
    assert settings.fiscal_year_start_month == 4
    assert settings.updated_by == user_id


async def test_set_fiscal_year_start_audits_old_and_new_months(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await set_fiscal_year_start(session, org_id, user_id, start_month=7)
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.fiscal_year_start_changed",
                )
            )
        ).scalar_one()
    assert event.meta["old"] == 1
    assert event.meta["new"] == 7


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session
# ---------------------------------------------------------------------------


async def test_tenancy_update_rate_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never update org B's FX rate via its id.

    Runs on a raw AsyncSession(admin_engine) -- its_admin is BYPASSRLS
    (tests/conftest.py:60) -- so org B's row is fully visible on this
    connection and `update_rate`'s (via `resolve_rate_row`) own org_id
    predicate is the SOLE guard. Under org_session(engine=app_engine) RLS
    would mask org B's row and this test would pass even with the
    predicate deleted.

    `id` is the table's primary key (globally unique, not just per-org),
    so dropping the `org_id` predicate does not create a
    `MultipleResultsFound` ambiguity here regardless of what other rows
    exist -- `select(FxRate).where(FxRate.id == rate_b_id)` matches exactly
    org B's row either way. That makes this a clean, deterministic
    assertion about WHICH org's rate is reachable, not an incidental
    row-count artifact.
    """
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rate_b = FxRate(
            org_id=org_b,
            from_currency="EUR",
            to_currency="USD",
            rate=Decimal("1.10000000"),
            effective_date=date(2026, 1, 1),
            created_by=user_b,
        )
        session.add(rate_b)
        await session.commit()
        rate_b_id = rate_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(BudgetNotFoundError):
            await update_rate(session, org_a, user_a, rate_b_id, {"rate": Decimal("9.0")})


async def test_tenancy_base_currency_guard_is_org_scoped(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org B's converted expense must never block org A's base-currency
    change.

    Runs on a raw AsyncSession(admin_engine) -- its_admin is BYPASSRLS --
    so org B's expense row is fully visible on this connection and the
    guard's own `org_id` predicate is the SOLE thing keeping it from
    leaking into org A's check. Under org_session(engine=app_engine) RLS
    would mask org B's row and this test would pass even with the
    predicate deleted.
    """
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await _seed_converted_expense(session, org_b, user_b)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        # org A has no converted expense of its own -- this must succeed
        # WITHOUT acknowledge_mixed_basis, despite org B having one.
        settings = await set_base_currency(session, org_a, user_a, base_currency="COP")
        await session.commit()
    assert settings.base_currency == "COP"
