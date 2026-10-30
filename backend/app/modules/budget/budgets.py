"""Budget entity + budget_line grid (docs/modules/budget/spec.md §2, §4, §8).

Mirrors `refdata.py`/`rates.py`'s style: org-scoped load + audit + flush for
every mutation (invariant #4), an explicit `org_id` predicate on every query
(invariant #5), and a typed `BudgetValidationError`/`BudgetNotFoundError`
pre-check in place of letting a DB constraint surface as a 500.

`put_lines` resolves EVERY `category_id` and `cost_center_id`, org-scoped and
held to the budget's client via `refdata.category_of_client`/
`cost_center_of_client` (the same check an expense's references get), and
validates every
`period_index` against the parent budget's granularity (`periods.py`'s
`period_count`, not reimplemented here) -- all of it BEFORE any row is added
to the session. A cross-org reference or an out-of-range `period_index`
anywhere in the batch is rejected as though nothing in the call had run;
because no `session.add`/mutation happens until every line has been
resolved and validated, there is no partial write to roll back. Duplicate
keys *within one call's line list* are collapsed (last one wins) before any
DB round-trip, so a caller resubmitting the same cell twice in one payload
upserts rather than racing itself into the partial unique index.

A closed budget rejects all line mutations (spec §4: a reported year must
not shift under a reader) -- `put_lines` and `delete_line` both check
`budget.status` first, before touching anything else.

A budget belongs to ONE client (MSP clients plan 2026-10-26 Task 8), and its
lines inherit it (H, by trigger). It is created for the client the caller
names (`clients.client_for_new_budget_rows`); every other write loads it and
calls `require_budget` with its client before anything else
(`_budget_for_write`). A line's category and cost center are the budget's
client's: another client's is a 422 (`BudgetClientError`), and the database
holds the same rule (`fk_budget_line_*_client`).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.wire_words import say
from app.models import Budget, BudgetLine
from app.modules.budget.clients import client_for_new_budget_rows, require_budget
from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.periods import period_count
from app.modules.budget.refdata import category_of_client, cost_center_of_client
from app.modules.budget.schema import BudgetLineInput
from app.modules.budget.words import GRANULARITY_WORDS

# update_budget's editable-field set, decided by key PRESENCE in `fields`
# (the refdata.py/rates.py convention -- callers pass
# `model_dump(exclude_unset=True)`). `fiscal_year`/`granularity` are absent:
# budget lines are validated against granularity, so changing it out from
# under existing lines would silently invalidate them (spec §2). `status`
# here is only ever `BudgetUpdateStatus` ("draft"/"active") at the schema
# boundary -- "closed" reaches the budget solely through `close_budget`,
# which also runs the audit event a bare status flip would bypass.
_BUDGET_EDITABLE_FIELDS = ("name", "status")
_BUDGET_NON_NULLABLE_FIELDS = frozenset({"name", "status"})


def _reject_null_for_non_nullable(
    fields: dict[str, Any], non_nullable: frozenset[str]
) -> None:
    """A supplied `None` for a NOT NULL column would otherwise reach Postgres
    as a NOT NULL violation and surface as a 500; reject it up front as a
    typed 400 instead (the `refdata.py`/`rates.py` pattern)."""
    for field in non_nullable:
        if field in fields and fields[field] is None:
            raise BudgetValidationError(tr("{field} cannot be null", field=field))


def _validate_period_index(period_index: int, granularity: str) -> None:
    """Business-rule check the schema layer deliberately cannot make: a
    Pydantic field on `BudgetLineInput` has no visibility into its parent
    budget's `granularity`, so `schema.py` only bounds `period_index` to the
    widest legal range (1-12). This is the granularity-specific check."""
    count = period_count(granularity)
    if not 1 <= period_index <= count:
        raise BudgetValidationError(
            tr(
                "period_index {period_index} out of range 1..{count} for {granularity} budgets",
                period_index=period_index,
                count=count,
                granularity=say(GRANULARITY_WORDS, granularity, quoted=True),
            )
        )


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------


async def create_budget(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    *,
    client_id: UUID,
    fiscal_year: int,
    name: str,
    granularity: str,
) -> Budget:
    """A budget of `client_id` (`client_for_new_budget_rows`). A duplicate
    `(fiscal_year, name)` within the client is rejected up front as a typed
    `BudgetValidationError` rather than surfacing the
    `uq_budget_client_year_name` violation as a 500; the same pair in a
    different client or org is unaffected (the constraint, and this check,
    are both per client).

    The pre-check above is a fast path, not the sole guard: two concurrent
    `create_budget` calls for the same `(org_id, client_id, fiscal_year,
    name)` can both miss it before either has written. A plain `session.add`
    here would let the loser surface a raw `IntegrityError` as a 500, so the
    actual insert instead goes through `ON CONFLICT DO NOTHING` against
    `uq_budget_client_year_name` (same shape as `rates.create_rate` /
    `refdata.create_category`) -- the race loser gets the identical typed
    `BudgetValidationError` the pre-check raises, never someone else's
    row."""
    await client_for_new_budget_rows(session, org_id, client_id)
    existing = (
        await session.execute(
            select(Budget.id).where(
                Budget.org_id == org_id,
                Budget.client_id == client_id,
                Budget.fiscal_year == fiscal_year,
                Budget.name == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise BudgetValidationError(
            tr(
                "budget already exists for fiscal_year={fiscal_year} name={name}",
                fiscal_year=fiscal_year,
                name=repr(name),
            )
        )

    budget_id = uuid7()
    inserted_id = (
        await session.execute(
            pg_insert(Budget)
            .values(
                id=budget_id,
                org_id=org_id,
                client_id=client_id,
                fiscal_year=fiscal_year,
                name=name,
                granularity=granularity,
                created_by=actor_id,
            )
            .on_conflict_do_nothing(
                index_elements=["org_id", "client_id", "fiscal_year", "name"]
            )
            .returning(Budget.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise BudgetValidationError(
            tr(
                "budget already exists for fiscal_year={fiscal_year} name={name}",
                fiscal_year=fiscal_year,
                name=repr(name),
            )
        )
    budget = (await session.execute(select(Budget).where(Budget.id == inserted_id))).scalar_one()
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.created",
        target_type="budget",
        target_id=str(budget.id),
        client_id=client_id,
        metadata={"fiscal_year": fiscal_year, "name": name, "granularity": granularity},
    )
    await session.flush()
    return budget


async def list_budgets(
    session: AsyncSession, org_id: UUID, *, client_id: UUID | None = None
) -> list[Budget]:
    """Every visible client's budgets, or `client_id`'s alone."""
    stmt = select(Budget).where(Budget.org_id == org_id)
    if client_id is not None:
        stmt = stmt.where(Budget.client_id == client_id)
    stmt = stmt.order_by(Budget.fiscal_year.desc(), Budget.name)
    return list((await session.execute(stmt)).scalars().all())


async def get_budget(session: AsyncSession, org_id: UUID, budget_id: UUID) -> Budget:
    """Org-scoped load. A budget id from another org is indistinguishable
    from a nonexistent one -- both raise `BudgetNotFoundError` (spec §8,
    invariant #5)."""
    budget = (
        await session.execute(
            select(Budget).where(Budget.org_id == org_id, Budget.id == budget_id)
        )
    ).scalar_one_or_none()
    if budget is None:
        raise BudgetNotFoundError(tr("budget {budget_id} not found", budget_id=budget_id))
    return budget


async def _budget_for_write(session: AsyncSession, org_id: UUID, budget_id: UUID) -> Budget:
    """The budget a write is about, of a client that uses budget: a budget
    this session cannot see is the 404 it always was, and one whose client
    has the module off is `ModuleNotEnabled` (404) -- before anything is
    written (the client is the write's first lock, Task 7)."""
    budget = await get_budget(session, org_id, budget_id)
    await require_budget(session, org_id, budget.client_id)
    return budget


async def update_budget(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    budget_id: UUID,
    fields: dict[str, Any],
) -> Budget:
    """Apply the SUPPLIED fields to a budget, org-scoped-loaded. Only `name`
    and `status` are editable here -- `fiscal_year`/`granularity` are never
    touched even if present in `fields`, and `status` transitioning to
    "closed" happens solely through `close_budget`.

    A CLOSED budget rejects every edit, including a status change back to
    draft/active. `close_budget` freezes line mutations so a reported year
    cannot shift under a reader (spec §4) -- but that freeze is only as
    strong as `closed` being one-way. Without this guard, PATCHing
    `{"status": "active"}` reopens the budget and `put_lines`/`delete_line`
    start succeeding again on lines that were supposed to be frozen.
    Reopening, if it is ever wanted, needs its own audited action rather
    than arriving as a side effect of a rename."""
    budget = await _budget_for_write(session, org_id, budget_id)
    if budget.status == "closed":
        raise BudgetValidationError(
            tr("budget {budget_id} is closed and cannot be edited", budget_id=budget_id)
        )
    _reject_null_for_non_nullable(fields, _BUDGET_NON_NULLABLE_FIELDS)

    changed_fields = []
    for field in _BUDGET_EDITABLE_FIELDS:
        if field not in fields:
            continue
        if getattr(budget, field) != fields[field]:
            changed_fields.append(field)
        setattr(budget, field, fields[field])
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.updated",
        target_type="budget",
        target_id=str(budget.id),
        client_id=budget.client_id,
        metadata={"changed_fields": sorted(changed_fields)},
    )
    await session.flush()
    return budget


async def close_budget(
    session: AsyncSession, org_id: UUID, actor_id: UUID, budget_id: UUID
) -> Budget:
    """Spec §4: `draft -> active -> closed`. Closing is the only path that
    ever sets `status = "closed"` (the dedicated route/service call, not the
    generic `update_budget` PATCH surface), and it is audited."""
    budget = await _budget_for_write(session, org_id, budget_id)
    budget.status = "closed"
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.closed",
        target_type="budget",
        target_id=str(budget.id),
        client_id=budget.client_id,
        metadata={},
    )
    await session.flush()
    return budget


# ---------------------------------------------------------------------------
# budget_line
# ---------------------------------------------------------------------------


async def _find_line(
    session: AsyncSession,
    org_id: UUID,
    budget_id: UUID,
    category_id: UUID,
    cost_center_id: UUID | None,
    period_index: int,
) -> BudgetLine | None:
    """The upsert lookup, matching whichever of the two partial unique
    indexes applies (`uq_budget_line_cc` / `uq_budget_line_nocc`)."""
    stmt = select(BudgetLine).where(
        BudgetLine.org_id == org_id,
        BudgetLine.budget_id == budget_id,
        BudgetLine.category_id == category_id,
        BudgetLine.period_index == period_index,
    )
    if cost_center_id is not None:
        stmt = stmt.where(BudgetLine.cost_center_id == cost_center_id)
    else:
        stmt = stmt.where(BudgetLine.cost_center_id.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


async def put_lines(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    budget_id: UUID,
    lines: list[BudgetLineInput],
) -> list[BudgetLine]:
    """Bulk upsert of `budget_line` rows for one budget.

    Two passes, deliberately not interleaved:

    1. Resolve every `category_id`/`cost_center_id` org-scoped, hold each to
       the budget's client (another client's is a 422), and validate every
       `period_index` against `budget.granularity`. Nothing is added to the
       session in this pass -- a cross-org or cross-client reference or an
       out-of-range period anywhere in `lines` raises before any write, so
       there is no partial write to roll back.
    2. Upsert the resolved, deduplicated rows (by `(category_id,
       cost_center_id, period_index)`, last-one-wins for a key repeated
       within `lines`) -- update `planned_amount` on a match, insert a new
       row otherwise.

    A single `budget.lines_updated` audit event covers the whole call, with
    the line count in metadata -- one event per line would flood the hash
    chain for a large grid paste.
    """
    budget = await _budget_for_write(session, org_id, budget_id)
    if budget.status == "closed":
        raise BudgetValidationError(
            tr("budget {budget_id} is closed: line mutations are rejected", budget_id=budget_id)
        )

    # Pass 1: resolve + validate everything before any write.
    order: list[tuple[UUID, UUID | None, int]] = []
    amounts: dict[tuple[UUID, UUID | None, int], BudgetLineInput] = {}
    for line in lines:
        category = await category_of_client(
            session, org_id, budget.client_id, line.category_id
        )
        cost_center_id: UUID | None = None
        if line.cost_center_id is not None:
            cost_center = await cost_center_of_client(
                session, org_id, budget.client_id, line.cost_center_id
            )
            cost_center_id = cost_center.id
        _validate_period_index(line.period_index, budget.granularity)

        key = (category.id, cost_center_id, line.period_index)
        if key not in amounts:
            order.append(key)
        amounts[key] = line

    # Pass 2: upsert the resolved, deduplicated set.
    results: list[BudgetLine] = []
    for key in order:
        category_id, cost_center_id, period_index = key
        line = amounts[key]
        existing = await _find_line(
            session, org_id, budget_id, category_id, cost_center_id, period_index
        )
        if existing is not None:
            existing.planned_amount = line.planned_amount
            results.append(existing)
        else:
            new_line = BudgetLine(
                org_id=org_id,
                budget_id=budget_id,
                category_id=category_id,
                cost_center_id=cost_center_id,
                period_index=period_index,
                planned_amount=line.planned_amount,
                created_by=actor_id,
            )
            session.add(new_line)
            results.append(new_line)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.lines_updated",
        target_type="budget",
        target_id=str(budget_id),
        client_id=budget.client_id,
        metadata={"line_count": len(results)},
    )
    await session.flush()
    return results


async def delete_line(
    session: AsyncSession, org_id: UUID, actor_id: UUID, budget_id: UUID, line_id: UUID
) -> None:
    """Org- and budget-scoped delete. A closed budget rejects this (spec
    §4), checked before the line lookup so a closed budget never leaks
    whether a given `line_id` exists via a different error shape."""
    budget = await _budget_for_write(session, org_id, budget_id)
    if budget.status == "closed":
        raise BudgetValidationError(
            tr("budget {budget_id} is closed: line mutations are rejected", budget_id=budget_id)
        )

    line = (
        await session.execute(
            select(BudgetLine).where(
                BudgetLine.org_id == org_id,
                BudgetLine.budget_id == budget_id,
                BudgetLine.id == line_id,
            )
        )
    ).scalar_one_or_none()
    if line is None:
        raise BudgetNotFoundError(tr("budget line {line_id} not found", line_id=line_id))

    await session.delete(line)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.line_deleted",
        target_type="budget_line",
        target_id=str(line_id),
        client_id=budget.client_id,
        metadata={},
    )
    await session.flush()
