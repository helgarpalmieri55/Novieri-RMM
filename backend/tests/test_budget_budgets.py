"""Budget entity + budget_line grid bulk upsert (spec §2, §4, §8).

Covers: budget CRUD (create/list/get/update/close), each mutation audited;
`put_lines` resolves every `category_id`/`cost_center_id` org-scoped and
validates every `period_index` against the parent budget's granularity
BEFORE writing anything -- a cross-org reference leaves no partial write
behind; `put_lines` upserts (a repeated call for the same cell updates the
amount, never duplicates); the NULL-`cost_center_id` case is covered by a
direct-ORM test proving `uq_budget_line_nocc` exists and works (a single
unique index over a nullable column enforces nothing for NULLs -- Postgres
treats them as distinct); a closed budget rejects both `put_lines` and
`delete_line`. Tenancy on `get_budget` and on `put_lines`' cross-reference
resolution is covered with load-bearing tests on a raw BYPASSRLS admin
session (spec §8).
"""

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_extensions import uuid7

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Budget, BudgetLine, CostCenter, ExpenseCategory, User
from app.modules.budget.budgets import (
    close_budget,
    create_budget,
    delete_line,
    get_budget,
    list_budgets,
    put_lines,
    update_budget,
)
from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.refdata import create_category, create_cost_center
from app.modules.budget.schema import BudgetLineInput


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Create a throwaway org plus an `org-admin` user in it; return
    `(org_id, user_id)`. Mirrors test_budget_fx.py / test_budget_rates.py /
    test_budget_refdata.py's helper of the same name/shape -- not imported
    across test modules, per convention."""
    org_id = await api_env.create_org("budgets")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _seed_category(session: AsyncSession, org_id: UUID, user_id: UUID, key: str = "hardware"):
    return await create_category(session, org_id, user_id, client_id=org_id,
                                 key=key, name=key.title())


async def _seed_cost_center(session: AsyncSession, org_id: UUID, user_id: UUID, code: str = "ENG"):
    return await create_cost_center(session, org_id, user_id, client_id=org_id,
                                    code=code, name=f"{code} team")


# ---------------------------------------------------------------------------
# create_budget / list_budgets / get_budget
# ---------------------------------------------------------------------------


async def test_create_budget_persists_fields_and_defaults_to_draft(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26 IT Budget", granularity="month"
        )
    assert budget.org_id == org_id
    assert budget.fiscal_year == 2026
    assert budget.name == "FY26 IT Budget"
    assert budget.granularity == "month"
    assert budget.status == "draft"
    assert budget.created_by == user_id


async def test_create_budget_audits(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="quarter"
        )
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "budget.created"
                )
            )
        ).scalar_one()
    assert event.target_type == "budget"
    assert event.target_id == str(budget.id)
    assert event.actor_type == "user"
    assert event.actor_id == user_id
    assert event.meta["fiscal_year"] == 2026
    assert event.meta["name"] == "FY26"
    assert event.meta["granularity"] == "quarter"


async def test_duplicate_fiscal_year_and_name_in_one_org_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        with pytest.raises(BudgetValidationError):
            await create_budget(
                session, org_id, user_id, client_id=org_id,
                fiscal_year=2026, name="FY26", granularity="quarter"
            )


async def test_create_budget_concurrent_duplicate_raises_typed_error_not_integrity_error(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Two concurrent `create_budget` calls for the same `(org_id,
    fiscal_year, name)` can both miss the pre-check before either has
    written. The race loser must raise the SAME typed
    `BudgetValidationError` the pre-check raises, not a raw
    `IntegrityError` -- same shape as `rates.create_rate` /
    `refdata.create_category` / `refdata.create_cost_center`.

    Deterministic interleave: session A's pre-check SELECT against
    `budget` is intercepted, and right after it returns (a miss), session
    B inserts and commits the EXACT row session A is about to try to
    create."""
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
                and "budget.id" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    winner = Budget(
                        org_id=org_id, client_id=org_id,
                        fiscal_year=2026,
                        name="FY26",
                        granularity="quarter",
                        created_by=user_id,
                    )
                    session_b.add(winner)
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        with pytest.raises(BudgetValidationError, match="budget already exists"):
            await create_budget(
                session_a, org_id, user_id, client_id=org_id,
                fiscal_year=2026, name="FY26", granularity="month"
            )
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(Budget).where(
                        Budget.org_id == org_id,
                        Budget.fiscal_year == 2026,
                        Budget.name == "FY26",
                    )
                )
            )
            .scalars()
            .all()
        )
    # Exactly one row exists -- session B's, not a duplicate.
    assert len(rows) == 1
    assert rows[0].granularity == "quarter"


async def test_same_fiscal_year_and_name_in_two_orgs_is_fine(api_env) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget_a = await create_budget(
            session, org_a, user_a, client_id=org_a,
            fiscal_year=2026, name="FY26", granularity="month"
        )
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget_b = await create_budget(
            session, org_b, user_b, client_id=org_b,
            fiscal_year=2026, name="FY26", granularity="month"
        )
    assert budget_a.id != budget_b.id
    assert budget_a.org_id != budget_b.org_id


async def test_list_budgets_returns_only_this_orgs_budgets(api_env) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_budget(
            session, org_a, user_a, client_id=org_a,
            fiscal_year=2026, name="FY26", granularity="month"
        )
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_budget(
            session, org_b, user_b, client_id=org_b,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        budgets_b = await list_budgets(session, org_b)
    assert len(budgets_b) == 1
    assert budgets_b[0].org_id == org_b


async def test_list_budgets_is_ordered_by_fiscal_year_desc_then_name(api_env) -> None:
    """A list-equality assertion on an unordered query is a landmine (the
    prior-task lesson) -- `list_budgets` has an explicit ORDER BY, and this
    test asserts against that exact order rather than insertion order."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2025, name="FY25", granularity="month"
        )
        await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="Zeta", granularity="month"
        )
        await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="Alpha", granularity="month"
        )
        budgets = await list_budgets(session, org_id)
    assert [(b.fiscal_year, b.name) for b in budgets] == [
        (2026, "Alpha"),
        (2026, "Zeta"),
        (2025, "FY25"),
    ]


async def test_get_budget_on_unknown_id_raises_not_found(api_env) -> None:
    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(BudgetNotFoundError):
            await get_budget(session, org_id, uuid7())


# ---------------------------------------------------------------------------
# update_budget
# ---------------------------------------------------------------------------


async def test_update_budget_changes_name_and_status(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        updated = await update_budget(
            session, org_id, user_id, budget.id, {"name": "FY26 Renamed", "status": "active"}
        )
    assert updated.name == "FY26 Renamed"
    assert updated.status == "active"


async def test_update_budget_ignores_fiscal_year_and_granularity(api_env) -> None:
    """`fields` may carry extra keys; only `name`/`status` are ever applied
    -- `fiscal_year`/`granularity` must never move under existing lines."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        updated = await update_budget(
            session,
            org_id,
            user_id,
            budget.id,
            {"name": "FY26 Renamed", "fiscal_year": 2099, "granularity": "quarter"},
        )
    assert updated.name == "FY26 Renamed"
    assert updated.fiscal_year == 2026
    assert updated.granularity == "month"


async def test_update_budget_rejects_null_name(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        with pytest.raises(BudgetValidationError):
            await update_budget(session, org_id, user_id, budget.id, {"name": None})


async def test_update_budget_audits_only_the_changed_field_names(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        await update_budget(
            session, org_id, user_id, budget.id, {"name": "FY26", "status": "active"}
        )
        event = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == org_id, AuditEvent.action == "budget.updated")
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert event.meta["changed_fields"] == ["status"]


# ---------------------------------------------------------------------------
# close_budget
# ---------------------------------------------------------------------------


async def test_close_budget_sets_status_closed(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        closed = await close_budget(session, org_id, user_id, budget.id)
    assert closed.status == "closed"


async def test_closing_a_budget_is_audited(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        await close_budget(session, org_id, user_id, budget.id)
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "budget.closed"
                )
            )
        ).scalar_one()
    assert event.target_type == "budget"
    assert event.target_id == str(budget.id)
    assert event.actor_type == "user"
    assert event.actor_id == user_id


# ---------------------------------------------------------------------------
# put_lines
# ---------------------------------------------------------------------------


async def test_put_lines_creates_lines_resolving_category_and_cost_center(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        cat = await _seed_category(session, org_id, user_id)
        cc = await _seed_cost_center(session, org_id, user_id)
        lines = await put_lines(
            session,
            org_id,
            user_id,
            budget.id,
            [
                BudgetLineInput(
                    category_id=cat.id,
                    cost_center_id=cc.id,
                    period_index=1,
                    planned_amount=Decimal("1000.00"),
                )
            ],
        )
    assert len(lines) == 1
    line = lines[0]
    assert line.budget_id == budget.id
    assert line.category_id == cat.id
    assert line.cost_center_id == cc.id
    assert line.period_index == 1
    assert line.planned_amount == Decimal("1000.00")
    assert line.org_id == org_id


async def test_put_lines_accepts_null_cost_center_for_an_org_wide_line(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        cat = await _seed_category(session, org_id, user_id)
        lines = await put_lines(
            session,
            org_id,
            user_id,
            budget.id,
            [
                BudgetLineInput(
                    category_id=cat.id,
                    cost_center_id=None,
                    period_index=1,
                    planned_amount=Decimal("500.00"),
                )
            ],
        )
    assert lines[0].cost_center_id is None


async def test_put_lines_upserts_rather_than_duplicating(api_env) -> None:
    """Called twice with the same (category, cost_center, period) it updates
    the amount -- it does not create a second line."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        cat = await _seed_category(session, org_id, user_id)
        cc = await _seed_cost_center(session, org_id, user_id)

        first = await put_lines(
            session,
            org_id,
            user_id,
            budget.id,
            [
                BudgetLineInput(
                    category_id=cat.id,
                    cost_center_id=cc.id,
                    period_index=3,
                    planned_amount=Decimal("100.00"),
                )
            ],
        )
        second = await put_lines(
            session,
            org_id,
            user_id,
            budget.id,
            [
                BudgetLineInput(
                    category_id=cat.id,
                    cost_center_id=cc.id,
                    period_index=3,
                    planned_amount=Decimal("250.00"),
                )
            ],
        )
        rows = (
            await session.execute(
                select(BudgetLine).where(
                    BudgetLine.org_id == org_id,
                    BudgetLine.budget_id == budget.id,
                    BudgetLine.category_id == cat.id,
                    BudgetLine.period_index == 3,
                )
            )
        ).scalars().all()
    assert first[0].id == second[0].id
    assert second[0].planned_amount == Decimal("250.00")
    assert len(rows) == 1
    assert rows[0].planned_amount == Decimal("250.00")


async def test_put_lines_dedupes_a_repeated_key_within_one_call(api_env) -> None:
    """The same (category, cost_center, period) cell submitted twice in ONE
    call must not race itself into the partial unique index -- last value
    wins and exactly one row results."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        cat = await _seed_category(session, org_id, user_id)
        lines = await put_lines(
            session,
            org_id,
            user_id,
            budget.id,
            [
                BudgetLineInput(
                    category_id=cat.id, cost_center_id=None, period_index=2,
                    planned_amount=Decimal("10.00"),
                ),
                BudgetLineInput(
                    category_id=cat.id, cost_center_id=None, period_index=2,
                    planned_amount=Decimal("20.00"),
                ),
            ],
        )
    assert len(lines) == 1
    assert lines[0].planned_amount == Decimal("20.00")


async def test_duplicate_org_wide_line_is_rejected_by_the_partial_index(api_env) -> None:
    """The NULL-cost_center_id case. A single unique index over a nullable
    column enforces NOTHING here (Postgres treats NULLs as distinct), so
    this is the test that proves `uq_budget_line_nocc` exists and works --
    it exercises the raw ORM/DB layer directly, not `put_lines`, so it
    cannot be masked by upsert logic."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        cat = await _seed_category(session, org_id, user_id)

        session.add(
            BudgetLine(
                org_id=org_id, budget_id=budget.id, category_id=cat.id,
                cost_center_id=None, period_index=1, planned_amount=Decimal("1.00"),
                created_by=user_id,
            )
        )
        await session.flush()

        session.add(
            BudgetLine(
                org_id=org_id, budget_id=budget.id, category_id=cat.id,
                cost_center_id=None, period_index=1, planned_amount=Decimal("2.00"),
                created_by=user_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_period_index_is_validated_against_granularity(api_env) -> None:
    """period_index=5 is legal for a monthly budget and illegal for a
    quarterly one."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        monthly = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="Monthly", granularity="month"
        )
        cat = await _seed_category(session, org_id, user_id)
        lines = await put_lines(
            session,
            org_id,
            user_id,
            monthly.id,
            [
                BudgetLineInput(
                    category_id=cat.id, cost_center_id=None, period_index=5,
                    planned_amount=Decimal("10.00"),
                )
            ],
        )
        assert lines[0].period_index == 5

        quarterly = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="Quarterly", granularity="quarter"
        )
        with pytest.raises(BudgetValidationError):
            await put_lines(
                session,
                org_id,
                user_id,
                quarterly.id,
                [
                    BudgetLineInput(
                        category_id=cat.id, cost_center_id=None, period_index=5,
                        planned_amount=Decimal("10.00"),
                    )
                ],
            )


async def test_put_lines_rejects_a_cross_org_category_id_without_partial_write(api_env) -> None:
    """A batch with one valid line and one invalid (cross-org category) line
    must write NEITHER -- resolution happens fully before any write."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        foreign_cat = await _seed_category(session, org_b, user_b, key="foreign")

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_a, user_a, client_id=org_a,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        own_cat = await _seed_category(session, org_a, user_a)

        with pytest.raises(BudgetNotFoundError):
            await put_lines(
                session,
                org_a,
                user_a,
                budget.id,
                [
                    BudgetLineInput(
                        category_id=own_cat.id, cost_center_id=None, period_index=1,
                        planned_amount=Decimal("10.00"),
                    ),
                    BudgetLineInput(
                        category_id=foreign_cat.id, cost_center_id=None, period_index=2,
                        planned_amount=Decimal("20.00"),
                    ),
                ],
            )

        remaining = (
            await session.execute(
                select(BudgetLine).where(
                    BudgetLine.org_id == org_a, BudgetLine.budget_id == budget.id
                )
            )
        ).scalars().all()
    assert remaining == []


async def test_put_lines_rejects_a_cross_org_cost_center_id(api_env) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        foreign_cc = await _seed_cost_center(session, org_b, user_b, code="FOREIGN")

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_a, user_a, client_id=org_a,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        own_cat = await _seed_category(session, org_a, user_a)

        with pytest.raises(BudgetNotFoundError):
            await put_lines(
                session,
                org_a,
                user_a,
                budget.id,
                [
                    BudgetLineInput(
                        category_id=own_cat.id, cost_center_id=foreign_cc.id, period_index=1,
                        planned_amount=Decimal("10.00"),
                    )
                ],
            )


async def test_put_lines_audits_once_per_call_with_the_line_count(api_env) -> None:
    """Not once per line -- that would flood the hash chain."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        cat = await _seed_category(session, org_id, user_id)
        cc = await _seed_cost_center(session, org_id, user_id)
        await put_lines(
            session,
            org_id,
            user_id,
            budget.id,
            [
                BudgetLineInput(
                    category_id=cat.id, cost_center_id=None, period_index=1,
                    planned_amount=Decimal("1.00"),
                ),
                BudgetLineInput(
                    category_id=cat.id, cost_center_id=cc.id, period_index=1,
                    planned_amount=Decimal("2.00"),
                ),
                BudgetLineInput(
                    category_id=cat.id, cost_center_id=None, period_index=2,
                    planned_amount=Decimal("3.00"),
                ),
            ],
        )
        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "budget.lines_updated"
                )
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].meta["line_count"] == 3
    assert events[0].target_type == "budget"
    assert events[0].target_id == str(budget.id)
    assert events[0].actor_type == "user"
    assert events[0].actor_id == user_id


# ---------------------------------------------------------------------------
# delete_line
# ---------------------------------------------------------------------------


async def test_delete_line_removes_the_row_and_audits(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        cat = await _seed_category(session, org_id, user_id)
        lines = await put_lines(
            session,
            org_id,
            user_id,
            budget.id,
            [
                BudgetLineInput(
                    category_id=cat.id, cost_center_id=None, period_index=1,
                    planned_amount=Decimal("1.00"),
                )
            ],
        )
        line_id = lines[0].id

        await delete_line(session, org_id, user_id, budget.id, line_id)

        remaining = (
            await session.execute(select(BudgetLine).where(BudgetLine.id == line_id))
        ).scalar_one_or_none()
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "budget.line_deleted"
                )
            )
        ).scalar_one()
    assert remaining is None
    assert event.target_type == "budget_line"
    assert event.target_id == str(line_id)
    assert event.actor_type == "user"
    assert event.actor_id == user_id


async def test_delete_line_on_unknown_id_raises_not_found(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        with pytest.raises(BudgetNotFoundError):
            await delete_line(session, org_id, user_id, budget.id, uuid7())


# ---------------------------------------------------------------------------
# closed budget rejects line mutations (spec §4)
# ---------------------------------------------------------------------------


async def test_update_budget_cannot_reopen_a_closed_budget(api_env) -> None:
    """The line-freeze in spec §4 is only as strong as `closed` being
    one-way. If PATCH can set status back to "active", an operator reopens
    the budget and put_lines/delete_line start succeeding again on lines
    that were supposed to be frozen -- the reported year shifts under a
    reader by a route that never mentions lines at all.

    (BudgetUpdate.status is typed draft|active so PATCH cannot SET "closed";
    this is the other half -- it must not move a budget OFF "closed" either.)
    """
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        cat = await _seed_category(session, org_id, user_id)
        await close_budget(session, org_id, user_id, budget.id)

        with pytest.raises(BudgetValidationError):
            await update_budget(session, org_id, user_id, budget.id, {"status": "active"})
        # a rename is refused too -- a closed budget is frozen, not
        # selectively frozen on the one field that reopens it
        with pytest.raises(BudgetValidationError):
            await update_budget(session, org_id, user_id, budget.id, {"name": "FY26 v2"})

        reloaded = await get_budget(session, org_id, budget.id)
        assert reloaded.status == "closed"
        assert reloaded.name == "FY26"

        # and the freeze it protects is still in force
        with pytest.raises(BudgetValidationError):
            await put_lines(
                session,
                org_id,
                user_id,
                budget.id,
                [BudgetLineInput(category_id=cat.id, period_index=1, planned_amount=Decimal("5"))],
            )


async def test_a_closed_budget_rejects_line_mutations(api_env) -> None:
    """Spec §4: a reported year must not shift under a reader."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        budget = await create_budget(
            session, org_id, user_id, client_id=org_id,
            fiscal_year=2026, name="FY26", granularity="month"
        )
        cat = await _seed_category(session, org_id, user_id)
        lines = await put_lines(
            session,
            org_id,
            user_id,
            budget.id,
            [
                BudgetLineInput(
                    category_id=cat.id, cost_center_id=None, period_index=1,
                    planned_amount=Decimal("1.00"),
                )
            ],
        )
        line_id = lines[0].id
        await close_budget(session, org_id, user_id, budget.id)

        line = BudgetLineInput(
            category_id=cat.id, cost_center_id=None, period_index=2,
            planned_amount=Decimal("5.00"),
        )
        with pytest.raises(BudgetValidationError):
            await put_lines(session, org_id, user_id, budget.id, [line])

        with pytest.raises(BudgetValidationError):
            await delete_line(session, org_id, user_id, budget.id, line_id)

        # the pre-existing line must survive both rejected mutation attempts
        still_there = (
            await session.execute(select(BudgetLine).where(BudgetLine.id == line_id))
        ).scalar_one_or_none()
    assert still_there is not None
    assert still_there.planned_amount == Decimal("1.00")


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session
# ---------------------------------------------------------------------------


async def test_tenancy_get_budget_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never fetch org B's budget by id.

    Runs on a raw AsyncSession(admin_engine) -- its_admin is BYPASSRLS
    (tests/conftest.py:60) -- so org B's row is fully visible on this
    connection and `get_budget`'s own `org_id` predicate is the SOLE guard.
    Under org_session(engine=app_engine) RLS would mask org B's row and this
    test would pass even with the predicate deleted.

    `id` is the table's primary key (globally unique, not just per-org), so
    dropping the `org_id` predicate does not create a `MultipleResultsFound`
    ambiguity here regardless of what other rows exist -- `select(Budget)
    .where(Budget.id == budget_b_id)` matches exactly org B's row either
    way. That makes this a clean, deterministic assertion about WHICH org's
    budget is reachable, not an incidental row-count artifact.
    """
    org_a, _user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        budget_b = Budget(
            org_id=org_b, client_id=org_b, fiscal_year=2026, name="FY26", granularity="month",
            status="draft", created_by=user_b,
        )
        session.add(budget_b)
        await session.commit()
        budget_b_id = budget_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(BudgetNotFoundError):
            await get_budget(session, org_a, budget_b_id)


async def test_tenancy_put_lines_category_resolution_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never write a budget line against org B's category, even
    on a raw BYPASSRLS admin session where org B's row is fully visible.

    `resolve_category` (called by `put_lines`) is the SOLE guard here: its
    `org_id` predicate is what rejects the cross-org id. `id` is the
    category table's primary key (globally unique), so this is deterministic
    regardless of what other rows exist.
    """
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_b = ExpenseCategory(
            org_id=org_b, client_id=org_b, key="widgets", name="Org B Widgets", created_by=user_b
        )
        session.add(cat_b)
        await session.commit()
        cat_b_id = cat_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        budget_a = Budget(
            org_id=org_a, client_id=org_a, fiscal_year=2026, name="FY26", granularity="month",
            status="draft", created_by=user_a,
        )
        session.add(budget_a)
        await session.commit()

        with pytest.raises(BudgetNotFoundError):
            await put_lines(
                session,
                org_a,
                user_a,
                budget_a.id,
                [
                    BudgetLineInput(
                        category_id=cat_b_id, cost_center_id=None, period_index=1,
                        planned_amount=Decimal("10.00"),
                    )
                ],
            )


async def test_tenancy_put_lines_cost_center_resolution_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never write a budget line against org B's cost center,
    even on a raw BYPASSRLS admin session where org B's row is fully
    visible. `resolve_cost_center`'s `org_id` predicate is the SOLE guard.
    """
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cc_b = CostCenter(
            org_id=org_b, client_id=org_b, code="ENG", name="Org B Engineering", created_by=user_b
        )
        session.add(cc_b)
        await session.commit()
        cc_b_id = cc_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_a = ExpenseCategory(
            org_id=org_a, client_id=org_a, key="hardware", name="Hardware", created_by=user_a
        )
        session.add(cat_a)
        budget_a = Budget(
            org_id=org_a, client_id=org_a, fiscal_year=2026, name="FY26", granularity="month",
            status="draft", created_by=user_a,
        )
        session.add(budget_a)
        await session.commit()

        with pytest.raises(BudgetNotFoundError):
            await put_lines(
                session,
                org_a,
                user_a,
                budget_a.id,
                [
                    BudgetLineInput(
                        category_id=cat_a.id, cost_center_id=cc_b_id, period_index=1,
                        planned_amount=Decimal("10.00"),
                    )
                ],
            )
