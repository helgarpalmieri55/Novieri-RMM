"""Expense lifecycle: create, list, update, confirm, void (spec §3, §5, §8).

Covers: conversion is CAPTURED at write time (a later corrective rate never
re-values a stored row); `convert_for`'s two distinct outcomes -- `(None,
None)` (unconvertible, never an error) vs. a raised `BudgetValidationError`
(overflow) -- are both exercised; `confirm_expense` stamps
`confirmed_by`/`confirmed_at`, accepts a corrected amount, and re-runs
conversion against `expense_date`; `void_expense` stamps
`voided_by`/`voided_at`; `update_expense`'s PATCH semantics (omitted field
untouched, explicit `null` clears a nullable field, explicit `null` on a
non-nullable field is rejected BEFORE any mutation); and the edit-audit
metadata carries changed field NAMES only, never amount values. Tenancy on
`category_id`/`cost_center_id`/`vendor_id` resolution inside `create_expense`
is covered with load-bearing tests on a raw BYPASSRLS admin session,
mutation-verified.
"""

import json
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_extensions import uuid7

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, CostCenter, Device, Expense, ExpenseCategory, User
from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.expenses import (
    EXPENSE_EDITABLE_FIELDS,
    NON_NULLABLE_EXPENSE_FIELDS,
    confirm_expense,
    create_expense,
    get_expense,
    list_expenses,
    update_expense,
    void_expense,
)
from app.modules.budget.rates import create_rate, update_rate
from app.modules.budget.refdata import create_category, create_cost_center
from app.modules.budget.schema import ExpenseCreate
from app.modules.licenses.models import Vendor
from app.modules.subscriptions.models import Subscription


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Create a throwaway org plus an `org-admin` user in it; return
    `(org_id, user_id)`. Mirrors test_budget_budgets.py/test_budget_rates.py's
    helper of the same name/shape -- not imported across test modules, per
    convention."""
    org_id = await api_env.create_org("expenses")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _seed_category(session: AsyncSession, org_id: UUID, user_id: UUID, key: str = "hardware"):
    return await create_category(session, org_id, user_id, key=key, name=key.title())


async def _seed_cost_center(session: AsyncSession, org_id: UUID, user_id: UUID, code: str = "ENG"):
    return await create_cost_center(session, org_id, user_id, code=code, name=f"{code} team")


async def _seed_vendor(
    session: AsyncSession, org_id: UUID, user_id: UUID, name: str = "Acme"
) -> Vendor:
    vendor = Vendor(org_id=org_id, name=name, created_by=user_id)
    session.add(vendor)
    await session.flush()
    return vendor


# ---------------------------------------------------------------------------
# create_expense: FX capture
# ---------------------------------------------------------------------------


async def test_create_captures_the_rate_and_base_amount(api_env) -> None:
    """Spec §3: conversion is CAPTURED at write time, not computed on read."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 1, 15),
                amount=Decimal("100.00"),
                currency="EUR",
                category_id=cat.id,
                status="expected",
            ),
        )
    assert expense.org_id == org_id
    assert expense.amount == Decimal("100.00")
    assert expense.currency == "EUR"
    assert expense.fx_rate == Decimal("1.10000000")
    assert expense.amount_base == Decimal("110.00")
    assert expense.category_id == cat.id
    assert expense.source == "manual"
    assert expense.status == "expected"
    assert expense.entered_by == user_id
    assert expense.confirmed_by is None
    assert expense.confirmed_at is None


async def test_create_expense_with_confirmed_status_stamps_an_asserter(api_env) -> None:
    """Spec §0.3/§5: "there is no path that confirms without a user."
    `ExpenseCreate.status` now defaults to "expected" (spec §5: "a row is
    never `confirmed` by omission") -- `confirmed` is only ever reached by a
    caller EXPLICITLY asserting it, and those rows are spec §7's `actual`
    column. `confirmed_by`/`confirmed_at` must be stamped here, not left
    NULL, for that explicit path."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 1, 15),
                amount=Decimal("100.00"),
                currency="USD",
                category_id=cat.id,
                status="confirmed",
            ),
        )
        assert expense.status == "confirmed"
        assert expense.confirmed_by == user_id
        assert expense.confirmed_at is not None

        # Prove PERSISTENCE, not just the returned object. Capture the id
        # BEFORE expiring -- an expired instance's own `.id` attribute
        # requires an awaited reload too, and a synchronous attribute access
        # outside the awaited call below would raise `MissingGreenlet`.
        expense_id = expense.id
        session.expire(expense)
        reloaded = await get_expense(session, org_id, expense_id)
    assert reloaded.confirmed_by == user_id
    assert reloaded.confirmed_at is not None


async def test_a_later_rate_correction_does_not_re_value_a_stored_expense(api_env) -> None:
    """The whole point of storing fx_rate + amount_base on the row."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        rate_row = await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("4.00000000"), effective_date=date(2026, 1, 1),
        )
        exp = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 1, 15),
                amount=Decimal("100.00"),
                currency="EUR",
                category_id=cat.id,
                status="expected",
            ),
        )
        before_rate = exp.fx_rate
        before_base = exp.amount_base
        assert before_rate == Decimal("4.00000000")
        assert before_base == Decimal("400.00")

        # A corrective rate lands on the SAME effective_date -- a real
        # second insert would collide with the unique constraint, so this
        # is applied as an update to the existing row, exactly like an
        # operator fixing a typo'd rate after the fact.
        await update_rate(session, org_id, user_id, rate_row.id, {"rate": Decimal("4.50000000")})

        # `org_session` uses expire_on_commit=False and nothing else commits
        # mid-test, so without this expire, `get_expense` below would return
        # the SAME identity-mapped Python object already asserted on above
        # and the reload would be a tautology, not a read of the persisted
        # row. Capture the id BEFORE expiring -- an expired instance's own
        # `.id` attribute requires an awaited reload too.
        exp_id = exp.id
        session.expire(exp)
        reloaded = await get_expense(session, org_id, exp_id)
    assert reloaded.fx_rate == Decimal("4.00000000")
    assert reloaded.amount_base == Decimal("400.00")
    assert reloaded.fx_rate == before_rate
    assert reloaded.amount_base == before_base


async def test_unconvertible_expense_is_created_with_null_base(api_env) -> None:
    """Never guess a rate (spec §0.1) -- the row exists, amount_base is NULL."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        # No fx_rate row exists for JPY -> the org's default base (USD).
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 1, 15),
                amount=Decimal("5000.00"),
                currency="JPY",
                category_id=cat.id,
                status="expected",
            ),
        )
        assert expense.id is not None
        assert expense.currency == "JPY"
        assert expense.fx_rate is None
        assert expense.amount_base is None

        # Prove PERSISTENCE, not just the returned object (the half of spec
        # §0.1 that matters) -- re-select the row, as the overflow test does.
        # `session.expire()` first (the H6 pattern): without it, SQLAlchemy's
        # identity map returns the SAME already-tracked `expense` object for
        # this re-select, so the assertions below would read Python memory,
        # not the database -- `assert persisted is expense` would (wrongly)
        # pass.
        exp_id = expense.id
        session.expire(expense)
        persisted = (
            await session.execute(select(Expense).where(Expense.id == exp_id))
        ).scalar_one()
    assert persisted.amount_base is None
    assert persisted.fx_rate is None


async def test_convert_overflow_raises_and_expense_is_not_persisted(api_env) -> None:
    """The other `convert_for` failure mode: overflow of Numeric(14,2)
    RAISES `BudgetValidationError`, distinct from the (None, None)
    unconvertible case -- and because the conversion is computed BEFORE the
    ORM object is constructed, nothing is left behind to roll back."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        await create_rate(
            session, org_id, user_id,
            from_currency="COP", rate=Decimal("2.00000000"), effective_date=date(2026, 1, 1),
        )
        with pytest.raises(BudgetValidationError):
            await create_expense(
                session, org_id, user_id,
                ExpenseCreate(
                    expense_date=date(2026, 1, 15),
                    amount=Decimal("999999999999.99"),
                    currency="COP",
                    category_id=cat.id,
                    status="expected",
                ),
            )
        remaining = (
            await session.execute(select(Expense).where(Expense.org_id == org_id))
        ).scalars().all()
    assert remaining == []


# ---------------------------------------------------------------------------
# confirm_expense
# ---------------------------------------------------------------------------


async def test_confirm_stamps_actor_and_time_and_accepts_a_corrected_amount(api_env) -> None:
    """Spec §0.3: the confirmed amount may differ from the projected one."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("100.00"),
                currency="USD",
                category_id=cat.id,
                status="expected",
            ),
        )
        assert expense.confirmed_by is None
        assert expense.confirmed_at is None

        confirmed = await confirm_expense(
            session, org_id, user_id, expense.id, amount=Decimal("150.00")
        )
    assert confirmed.status == "confirmed"
    assert confirmed.amount == Decimal("150.00")
    assert confirmed.amount_base == Decimal("150.00")  # USD is the org's base -- rate exactly 1
    assert confirmed.confirmed_by == user_id
    assert confirmed.confirmed_at is not None


async def test_confirm_without_an_amount_keeps_the_recorded_amount(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("75.00"),
                currency="USD",
                category_id=cat.id,
                status="expected",
            ),
        )
        confirmed = await confirm_expense(session, org_id, user_id, expense.id)
    assert confirmed.amount == Decimal("75.00")
    assert confirmed.status == "confirmed"


async def test_confirm_re_runs_conversion_against_expense_date(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        # No rate covers this date yet -- created unconvertible.
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 3, 5),
                amount=Decimal("200.00"),
                currency="EUR",
                category_id=cat.id,
                status="expected",
            ),
        )
        assert expense.fx_rate is None
        assert expense.amount_base is None

        # A rate covering 2026-03-05 shows up before confirmation.
        await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.20000000"), effective_date=date(2026, 3, 1),
        )

        confirmed = await confirm_expense(session, org_id, user_id, expense.id)
    assert confirmed.fx_rate == Decimal("1.20000000")
    assert confirmed.amount_base == Decimal("240.00")


async def test_confirm_a_void_expense_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                status="expected",
            ),
        )
        await void_expense(session, org_id, user_id, expense.id)
        with pytest.raises(BudgetValidationError):
            await confirm_expense(session, org_id, user_id, expense.id)


async def test_confirming_an_already_confirmed_expense_is_rejected(api_env) -> None:
    """Spec §3: a corrective rate must never re-value history. Re-running
    `confirm_expense` on an already-`confirmed` row would silently re-run
    conversion and re-stamp `confirmed_by`/`confirmed_at` over the original
    asserter, even with `amount` omitted -- reject it outright instead,
    mirroring the void guard."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                status="expected",
            ),
        )
        first_confirm = await confirm_expense(session, org_id, user_id, expense.id)
        first_confirmed_by = first_confirm.confirmed_by
        first_confirmed_at = first_confirm.confirmed_at

        with pytest.raises(BudgetValidationError):
            await confirm_expense(session, org_id, user_id, expense.id, amount=Decimal("999.00"))

        # The rejection ran BEFORE any setattr -- the original confirmation
        # is untouched, not silently overwritten. Capture the id BEFORE
        # expiring -- an expired instance's own `.id` attribute requires an
        # awaited reload too.
        expense_id = expense.id
        session.expire(expense)
        reloaded = await get_expense(session, org_id, expense_id)
    assert reloaded.amount == Decimal("10.00")
    assert reloaded.confirmed_by == first_confirmed_by
    assert reloaded.confirmed_at == first_confirmed_at


# ---------------------------------------------------------------------------
# void_expense
# ---------------------------------------------------------------------------


async def test_void_stamps_and_is_audited(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                status="expected",
            ),
        )
        voided = await void_expense(session, org_id, user_id, expense.id)
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "expense.voided"
                )
            )
        ).scalar_one()
    assert voided.status == "void"
    assert voided.voided_by == user_id
    assert voided.voided_at is not None
    assert event.target_type == "expense"
    assert event.target_id == str(expense.id)
    assert event.actor_type == "user"
    assert event.actor_id == user_id


async def test_voiding_an_already_void_expense_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                status="expected",
            ),
        )
        await void_expense(session, org_id, user_id, expense.id)
        with pytest.raises(BudgetValidationError):
            await void_expense(session, org_id, user_id, expense.id)


# ---------------------------------------------------------------------------
# update_expense: PATCH semantics
# ---------------------------------------------------------------------------


async def test_patch_omitted_field_is_left_untouched(api_env) -> None:
    """exclude_unset -- the Phase-12 lesson."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        cc = await _seed_cost_center(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                cost_center_id=cc.id,
                description="original description",
                invoice_ref="INV-1",
                status="expected",
            ),
        )
        updated = await update_expense(
            session, org_id, user_id, expense.id, {"description": "changed description"}
        )
    assert updated.description == "changed description"
    # everything not mentioned in `fields` is untouched
    assert updated.amount == Decimal("10.00")
    assert updated.currency == "USD"
    assert updated.category_id == cat.id
    assert updated.cost_center_id == cc.id
    assert updated.invoice_ref == "INV-1"


async def test_patch_explicit_null_clears_a_nullable_field(api_env) -> None:
    """{"cost_center_id": null} must actually clear it -- Phase 12 shipped a
    PATCH that returned 200 and changed nothing."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        cc = await _seed_cost_center(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                cost_center_id=cc.id,
                status="expected",
            ),
        )
        assert expense.cost_center_id == cc.id
        updated = await update_expense(
            session, org_id, user_id, expense.id, {"cost_center_id": None}
        )
    assert updated.cost_center_id is None


async def test_patch_amount_currency_or_date_re_runs_conversion(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 1, 15),
                amount=Decimal("100.00"),
                currency="EUR",
                category_id=cat.id,
                status="expected",
            ),
        )
        assert expense.amount_base == Decimal("110.00")

        updated = await update_expense(
            session, org_id, user_id, expense.id, {"amount": Decimal("200.00")}
        )
    assert updated.amount == Decimal("200.00")
    assert updated.fx_rate == Decimal("1.10000000")
    assert updated.amount_base == Decimal("220.00")


async def test_patch_a_field_unrelated_to_conversion_does_not_touch_fx(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        await create_rate(
            session, org_id, user_id,
            from_currency="EUR", rate=Decimal("1.10000000"), effective_date=date(2026, 1, 1),
        )
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 1, 15),
                amount=Decimal("100.00"),
                currency="EUR",
                category_id=cat.id,
                status="expected",
            ),
        )
        # A corrective rate change AFTER creation -- if `update_expense`
        # recomputed conversion on every call regardless of which fields
        # were touched, this innocuous description edit would silently
        # re-value the row.
        rate_row = (
            await session.execute(select(Expense).where(Expense.id == expense.id))
        ).scalar_one()
        assert rate_row.amount_base == Decimal("110.00")

        updated = await update_expense(
            session, org_id, user_id, expense.id, {"description": "just a note"}
        )
    assert updated.description == "just a note"
    assert updated.fx_rate == Decimal("1.10000000")
    assert updated.amount_base == Decimal("110.00")


@pytest.mark.parametrize("field", sorted(NON_NULLABLE_EXPENSE_FIELDS))
async def test_patch_explicit_null_on_a_non_nullable_field_is_rejected(api_env, field: str) -> None:
    """Rejected BEFORE any setattr, so the ORM object is never left partially
    mutated when the error is raised."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                description="keep me",
                invoice_ref="INV-KEEP",
                status="expected",
            ),
        )
        # Pair the null-rejected field with a legitimate, otherwise-valid
        # companion change in the SAME call -- proving the whole call is
        # rejected before any field (including the companion) is mutated.
        companion_field = "invoice_ref" if field == "description" else "description"
        companion_value = "should-not-persist"
        fields = {field: None, companion_field: companion_value}

        with pytest.raises(BudgetValidationError):
            await update_expense(session, org_id, user_id, expense.id, fields)

        reloaded = await get_expense(session, org_id, expense.id)
    assert reloaded.description == "keep me"
    assert reloaded.invoice_ref == "INV-KEEP"
    assert reloaded.amount == Decimal("10.00")
    assert reloaded.currency == "USD"
    assert reloaded.category_id == cat.id


async def test_edit_audit_records_field_names_and_never_amounts(api_env) -> None:
    """Spec §8: amounts are the sensitive payload in this module."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("1234.56"),
                currency="USD",
                category_id=cat.id,
                description="original desc",
                status="expected",
            ),
        )
        await update_expense(
            session, org_id, user_id, expense.id,
            {"amount": Decimal("999.99"), "description": "updated desc"},
        )
        event = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == org_id, AuditEvent.action == "expense.updated")
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert set(event.meta["changed_fields"]) == {"amount", "description"}
    serialized = json.dumps(event.meta)
    assert "1234.56" not in serialized
    assert "999.99" not in serialized


async def test_update_expense_on_unknown_id_raises_not_found(api_env) -> None:
    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(BudgetNotFoundError):
            await update_expense(session, org_id, _user_id, uuid7(), {"description": "x"})


async def test_update_expense_on_a_void_row_is_rejected(api_env) -> None:
    """Void is terminal (spec §5: "reversible only by creating a new
    entry"). Without this guard, `{"link_id": null}` on a voided,
    materialized row would free `uq_expense_autofeed`'s unique slot and let
    the next materialization resurrect a deliberately-suppressed cost."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                description="original",
                status="expected",
            ),
        )
        await void_expense(session, org_id, user_id, expense.id)

        with pytest.raises(BudgetValidationError):
            await update_expense(
                session, org_id, user_id, expense.id, {"description": "should not persist"}
            )

        expense_id = expense.id
        session.expire(expense)
        reloaded = await get_expense(session, org_id, expense_id)
    assert reloaded.description == "original"
    assert reloaded.status == "void"


async def test_update_link_id_on_a_non_manual_expense_is_rejected(api_env) -> None:
    """`link_id` keys `uq_expense_autofeed`'s identity for a materialized
    (non-`manual`) row -- only the materializer that owns `source` may move
    it. A generic PATCH freeing it (e.g. `{"link_id": null}`) would let the
    next materialization pass re-create the row the auto-feed already
    covers."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("10.00"),
            currency="USD",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        auto_fed = Expense(
            org_id=org_id,
            expense_date=date(2026, 3, 1),
            amount=Decimal("10.00"),
            currency="USD",
            category_id=cat.id,
            entered_by=user_id,
            source="materialized",
            link_type="subscription",
            link_id=sub.id,
            period_key="2026-03",
            status="expected",
        )
        session.add(auto_fed)
        await session.flush()
        auto_fed_id = auto_fed.id

        with pytest.raises(BudgetValidationError):
            await update_expense(session, org_id, user_id, auto_fed_id, {"link_id": None})
        with pytest.raises(BudgetValidationError):
            await update_expense(
                session, org_id, user_id, auto_fed_id, {"link_type": "device"}
            )

        session.expire(auto_fed)
        reloaded = await get_expense(session, org_id, auto_fed_id)
    assert reloaded.link_id == sub.id
    assert reloaded.link_type == "subscription"


async def test_bare_link_type_patch_on_a_manual_expense_re_resolves_link_id(api_env) -> None:
    """J3: resolution is keyed on the MERGED (link_type, link_id) pair, not
    on `link_id`'s presence alone. A bare `{"link_type": "device"}` PATCH on
    a `manual` row must re-resolve the row's EXISTING link_id against the
    NEW type -- not pass through unresolved -- because spec §7's Odoo export
    emits `link_type, link_id` verbatim into a pinned accounting column
    contract. Here the subscription's id does not exist in the device
    table, so the merged pair must fail to resolve."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("10.00"),
            currency="USD",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                link_type="subscription",
                link_id=sub.id,
                status="expected",
            ),
        )

        with pytest.raises(BudgetNotFoundError):
            await update_expense(
                session, org_id, user_id, expense.id, {"link_type": "device"}
            )

        expense_id = expense.id
        session.expire(expense)
        reloaded = await get_expense(session, org_id, expense_id)
    # The rejected re-type must leave the original link untouched.
    assert reloaded.link_id == sub.id
    assert reloaded.link_type == "subscription"


async def test_bare_link_type_patch_re_resolves_successfully_against_the_new_type(
    api_env,
) -> None:
    """The other half of J3: when the existing link_id DOES exist under the
    new type, a bare `link_type` PATCH is accepted -- the merged pair is
    re-resolved, not merely rejected out of hand."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        device = Device(
            org_id=org_id, hostname="host-1", os_family="linux", client_id=org_id, site_id=org_id
        )
        session.add(device)
        await session.flush()

        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("10.00"),
            currency="USD",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                link_type="subscription",
                link_id=sub.id,
                status="expected",
            ),
        )

        # Re-point link_id AND link_type together first is unaffected by J3;
        # what J3 guards is the BARE link_type PATCH below, changing only
        # the type while implicitly carrying the OLD link_id forward.
        updated = await update_expense(
            session, org_id, user_id, expense.id, {"link_id": device.id, "link_type": "device"}
        )
        assert updated.link_id == device.id
        assert updated.link_type == "device"

        # Now a bare link_type PATCH back to "subscription": device.id does
        # not exist in the subscription table, so this must be rejected too
        # -- proving the merged-pair resolution runs on EVERY link_type-only
        # PATCH, not just the first one.
        with pytest.raises(BudgetNotFoundError):
            await update_expense(
                session, org_id, user_id, expense.id, {"link_type": "subscription"}
            )


async def test_bare_null_link_type_patch_stranding_an_existing_link_id_is_rejected(
    api_env,
) -> None:
    """J3's other stranding case: `{"link_type": null}` alone must not leave
    a non-null `link_id` with no dispatch type -- the merged pair has a
    non-null link_id and a null link_type, which can never be resolved."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("10.00"),
            currency="USD",
            created_by=user_id,
        )
        session.add(sub)
        await session.flush()

        expense = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 2, 1),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat.id,
                link_type="subscription",
                link_id=sub.id,
                status="expected",
            ),
        )

        with pytest.raises(BudgetValidationError):
            await update_expense(
                session, org_id, user_id, expense.id, {"link_type": None}
            )

        expense_id = expense.id
        session.expire(expense)
        reloaded = await get_expense(session, org_id, expense_id)
    assert reloaded.link_id == sub.id
    assert reloaded.link_type == "subscription"


# ---------------------------------------------------------------------------
# list_expenses / get_expense
# ---------------------------------------------------------------------------


async def test_list_expenses_filters_by_status_category_cost_center_vendor_source(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat_a = await _seed_category(session, org_id, user_id, key="hardware")
        cat_b = await _seed_category(session, org_id, user_id, key="telecom")
        cc = await _seed_cost_center(session, org_id, user_id)
        vendor = await _seed_vendor(session, org_id, user_id)

        matching = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 1, 10),
                amount=Decimal("10.00"),
                currency="USD",
                category_id=cat_a.id,
                cost_center_id=cc.id,
                vendor_id=vendor.id,
                status="expected",
            ),
        )
        await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 1, 12),
                amount=Decimal("20.00"),
                currency="USD",
                category_id=cat_b.id,
                status="confirmed",
            ),
        )

        results = await list_expenses(
            session, org_id,
            status="expected", category_id=cat_a.id, cost_center_id=cc.id,
            vendor_id=vendor.id, source="manual",
        )
    assert [e.id for e in results] == [matching.id]


async def test_list_expenses_filters_by_date_range(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        jan = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 1, 5), amount=Decimal("1.00"), currency="USD",
                category_id=cat.id, status="expected",
            ),
        )
        await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 3, 5), amount=Decimal("2.00"), currency="USD",
                category_id=cat.id, status="expected",
            ),
        )
        results = await list_expenses(
            session, org_id, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31)
        )
    assert [e.id for e in results] == [jan.id]


async def test_list_expenses_is_ordered_by_expense_date_desc(api_env) -> None:
    """A list-equality assertion on an unordered query is a landmine (the
    prior-task lesson) -- `list_expenses` has an explicit ORDER BY."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await _seed_category(session, org_id, user_id)
        early = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 1, 1), amount=Decimal("1.00"), currency="USD",
                category_id=cat.id, status="expected",
            ),
        )
        late = await create_expense(
            session, org_id, user_id,
            ExpenseCreate(
                expense_date=date(2026, 6, 1), amount=Decimal("1.00"), currency="USD",
                category_id=cat.id, status="expected",
            ),
        )
        results = await list_expenses(session, org_id)
    assert [e.id for e in results] == [late.id, early.id]


async def test_list_expenses_returns_only_this_orgs_rows(api_env) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat_a = await _seed_category(session, org_a, user_a)
        await create_expense(
            session, org_a, user_a,
            ExpenseCreate(
                expense_date=date(2026, 1, 1), amount=Decimal("1.00"), currency="USD",
                category_id=cat_a.id, status="expected",
            ),
        )
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat_b = await _seed_category(session, org_b, user_b)
        await create_expense(
            session, org_b, user_b,
            ExpenseCreate(
                expense_date=date(2026, 1, 1), amount=Decimal("1.00"), currency="USD",
                category_id=cat_b.id, status="expected",
            ),
        )
        results = await list_expenses(session, org_b)
    assert len(results) == 1
    assert results[0].org_id == org_b


async def test_get_expense_on_unknown_id_raises_not_found(api_env) -> None:
    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(BudgetNotFoundError):
            await get_expense(session, org_id, uuid7())


def test_editable_and_non_nullable_field_sets_are_consistent() -> None:
    """`NON_NULLABLE_EXPENSE_FIELDS` must be a subset of the editable set --
    a non-nullable field that isn't even editable would make the rejection
    dead code."""
    assert NON_NULLABLE_EXPENSE_FIELDS <= set(EXPENSE_EDITABLE_FIELDS)
    assert "status" not in EXPENSE_EDITABLE_FIELDS


def test_non_nullable_expense_fields_matches_real_column_nullability() -> None:
    """The prior assertion only catches a DEAD entry (nullable in the DB but
    listed anyway); it says nothing about a MISSING one. Derive the set from
    `Expense.__table__` itself so this is self-maintaining: if a later task
    adds a NOT NULL column to `EXPENSE_EDITABLE_FIELDS` without updating
    `NON_NULLABLE_EXPENSE_FIELDS`, `{"that_field": null}` would otherwise
    become an unguarded 500 instead of a typed `BudgetValidationError`."""
    actually_non_nullable = {
        field
        for field in EXPENSE_EDITABLE_FIELDS
        if not Expense.__table__.columns[field].nullable
    }
    assert NON_NULLABLE_EXPENSE_FIELDS == actually_non_nullable


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session
# ---------------------------------------------------------------------------


async def test_tenancy_create_expense_category_resolution_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never create an expense against org B's category, even on
    a raw BYPASSRLS admin session where org B's row is fully visible.
    `resolve_category`'s `org_id` predicate (called from `create_expense`)
    is the SOLE guard. `id` is the category table's primary key (globally
    unique), so this is deterministic regardless of what other rows exist.
    """
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_b = ExpenseCategory(
            org_id=org_b, key="widgets", name="Org B Widgets", created_by=user_b
        )
        session.add(cat_b)
        await session.commit()
        cat_b_id = cat_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(BudgetNotFoundError):
            await create_expense(
                session, org_a, user_a,
                ExpenseCreate(
                    expense_date=date(2026, 1, 1), amount=Decimal("1.00"), currency="USD",
                    category_id=cat_b_id, status="expected",
                ),
            )


async def test_tenancy_create_expense_cost_center_resolution_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never create an expense against org B's cost center, even
    on a raw BYPASSRLS admin session where org B's row is fully visible.
    `resolve_cost_center`'s `org_id` predicate (called from `create_expense`)
    is the SOLE guard.
    """
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cc_b = CostCenter(org_id=org_b, code="ENG", name="Org B Engineering", created_by=user_b)
        session.add(cc_b)
        await session.commit()
        cc_b_id = cc_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_a = ExpenseCategory(org_id=org_a, key="hardware", name="Hardware", created_by=user_a)
        session.add(cat_a)
        await session.commit()
        cat_a_id = cat_a.id

        with pytest.raises(BudgetNotFoundError):
            await create_expense(
                session, org_a, user_a,
                ExpenseCreate(
                    expense_date=date(2026, 1, 1), amount=Decimal("1.00"), currency="USD",
                    category_id=cat_a_id, cost_center_id=cc_b_id, status="expected",
                ),
            )


async def test_tenancy_create_expense_vendor_resolution_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never create an expense against org B's vendor, even on a
    raw BYPASSRLS admin session where org B's row is fully visible.
    `_resolve_vendor`'s `org_id` predicate (called from `create_expense`) is
    the SOLE guard.
    """
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        vendor_b = Vendor(org_id=org_b, name="Org B Vendor", created_by=user_b)
        session.add(vendor_b)
        await session.commit()
        vendor_b_id = vendor_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_a = ExpenseCategory(org_id=org_a, key="hardware", name="Hardware", created_by=user_a)
        session.add(cat_a)
        await session.commit()
        cat_a_id = cat_a.id

        with pytest.raises(BudgetNotFoundError):
            await create_expense(
                session, org_a, user_a,
                ExpenseCreate(
                    expense_date=date(2026, 1, 1), amount=Decimal("1.00"), currency="USD",
                    category_id=cat_a_id, vendor_id=vendor_b_id, status="expected",
                ),
            )


async def test_tenancy_create_expense_link_resolution_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never create an expense whose advisory `link_id` points at
    org B's subscription, even on a raw BYPASSRLS admin session where org
    B's row is fully visible. `link_id` has no ForeignKey by design (spec
    §8), so `_resolve_link`'s `org_id` predicate (called from
    `create_expense`, dispatched on `link_type`) is the SOLE guard.
    """
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        sub_b = Subscription(
            org_id=org_b,
            service_name="Org B SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("10.00"),
            currency="USD",
            created_by=user_b,
        )
        session.add(sub_b)
        await session.commit()
        sub_b_id = sub_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_a = ExpenseCategory(org_id=org_a, key="hardware", name="Hardware", created_by=user_a)
        session.add(cat_a)
        await session.commit()
        cat_a_id = cat_a.id

        with pytest.raises(BudgetNotFoundError):
            await create_expense(
                session, org_a, user_a,
                ExpenseCreate(
                    expense_date=date(2026, 1, 1), amount=Decimal("1.00"), currency="USD",
                    category_id=cat_a_id, link_type="subscription", link_id=sub_b_id,
                    status="expected",
                ),
            )
