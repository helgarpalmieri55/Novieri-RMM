"""FX-rate CRUD plus the org base-currency change guard
(docs/modules/budget/spec.md §3, §8).

Mirrors `refdata.py`'s style: org-scoped load + audit + flush for every
mutation (invariant #4), an explicit `org_id` predicate on every query
(invariant #5), and a typed `BudgetValidationError`/`BudgetNotFoundError`
pre-check in place of letting a DB constraint surface as a 500.

Rates are the organisation's (`fx_rate` is G); settings are each CLIENT's
(MSP clients plan 2026-10-26 Task 8): a base currency and a fiscal year per
client. So `create_rate` converts INTO the currency the caller names
(`to_currency`), or, when it names none, into the internal client's base
currency -- the organisation's own, which is what "the base currency" meant
when there was one (spec §3: "`to_currency` must equal the org's base currency
at write time"). `set_base_currency` implements spec §3's rule, stated
exactly, per client: once ANY non-void expense OF THAT CLIENT carries a
non-NULL `amount_base`, changing its base currency requires
`acknowledge_mixed_basis=True` and is rejected without it. The guard runs as a
query BEFORE any mutation, and a base-currency change NEVER retro-converts
stored rows -- existing expenses keep the `fx_rate`/`amount_base` they were
written with; only `budget_settings.base_currency` moves.

A client's settings row is created by its first change
(`settings_for_write`), never by a read (`fx.get_settings` is pure). Every
settings write calls `client_for_budget_settings` first: an archived client is
the 409 a create gets, and a client without the module a 404, before anything
is written or audited.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import BudgetSettings, Expense, FxRate
from app.modules.budget.clients import client_for_budget_settings, visible_client
from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.fx import (
    DEFAULT_BASE_CURRENCY,
    DEFAULT_FISCAL_YEAR_START_MONTH,
    get_settings,
)

# `update_rate`'s editable-field set, decided by key PRESENCE in `fields`
# (the refdata.py convention -- callers pass `model_dump(exclude_unset=True)`).
# `from_currency`/`to_currency`/`effective_date` identify the row (spec §2's
# unique constraint) and are deliberately absent -- they are never touched
# here even if present in `fields`.
_RATE_EDITABLE_FIELDS = ("rate",)
_RATE_NON_NULLABLE_FIELDS = frozenset({"rate"})


def _reject_null_for_non_nullable(
    fields: dict[str, Any], non_nullable: frozenset[str]
) -> None:
    """A supplied `None` for a NOT NULL column (or one guarded by the `rate
    > 0` check constraint) would otherwise reach Postgres as a constraint
    violation and surface as a 500; reject it up front as a typed 400
    instead (the `refdata.py` pattern)."""
    for field in non_nullable:
        if field in fields and fields[field] is None:
            raise BudgetValidationError(tr("{field} cannot be null", field=field))


# ---------------------------------------------------------------------------
# fx_rate
# ---------------------------------------------------------------------------


async def create_rate(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    *,
    from_currency: str,
    rate: Decimal,
    effective_date: date,
    to_currency: str | None = None,
) -> FxRate:
    """`to_currency` is the currency the caller names -- each client has its
    own base currency, and one rate table serves them all -- or, when it
    names none, the internal client's CURRENT base currency (spec §3's "the
    org's base currency"; the internal client's id is the organisation's). A
    duplicate `(org_id, from_currency, to_currency,
    effective_date)` is rejected up front as a typed `BudgetValidationError`
    rather than surfacing the `uq_fx_rate_org_pair_date` violation as a 500;
    the same triple in a different org is unaffected (the constraint, and
    this check, are both org-scoped).

    The pre-check above is a fast path, not the sole guard: two concurrent
    `create_rate` calls for the exact same `(org_id, from_currency,
    to_currency, effective_date)` can both miss it before either has
    written. The insert itself is therefore `ON CONFLICT DO NOTHING`
    against `uq_fx_rate_org_pair_date` (mirrors `fx.get_settings`, Task-5
    finding F2) rather than a plain `session.add` -- but unlike
    `get_settings`'s get-or-create shape, a duplicate here is a genuine
    caller error, not a row to silently hand back. So the loser of the
    race raises the SAME typed `BudgetValidationError` the pre-check would
    have raised, rather than either 500ing on a raw `IntegrityError` or
    quietly returning someone else's rate."""
    if to_currency is None:
        to_currency = (await get_settings(session, org_id, org_id)).base_currency

    existing = (
        await session.execute(
            select(FxRate.id).where(
                FxRate.org_id == org_id,
                FxRate.from_currency == from_currency,
                FxRate.to_currency == to_currency,
                FxRate.effective_date == effective_date,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise BudgetValidationError(
            tr(
                "fx rate already exists for {from_currency}->{to_currency} on {effective_date}",
                from_currency=from_currency,
                to_currency=to_currency,
                effective_date=effective_date.isoformat(),
            )
        )

    rate_id = uuid7()
    inserted_id = (
        await session.execute(
            pg_insert(FxRate)
            .values(
                id=rate_id,
                org_id=org_id,
                from_currency=from_currency,
                to_currency=to_currency,
                rate=rate,
                effective_date=effective_date,
                created_by=actor_id,
            )
            .on_conflict_do_nothing(
                index_elements=["org_id", "from_currency", "to_currency", "effective_date"]
            )
            .returning(FxRate.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise BudgetValidationError(
            tr(
                "fx rate already exists for {from_currency}->{to_currency} on {effective_date}",
                from_currency=from_currency,
                to_currency=to_currency,
                effective_date=effective_date.isoformat(),
            )
        )
    fx = (await session.execute(select(FxRate).where(FxRate.id == inserted_id))).scalar_one()
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.fx_rate_created",
        target_type="fx_rate",
        target_id=str(fx.id),
        metadata={
            "from_currency": from_currency,
            "to_currency": to_currency,
            "effective_date": effective_date.isoformat(),
        },
    )
    await session.flush()
    return fx


async def list_rates(
    session: AsyncSession, org_id: UUID, *, from_currency: str | None = None
) -> list[FxRate]:
    stmt = select(FxRate).where(FxRate.org_id == org_id)
    if from_currency is not None:
        stmt = stmt.where(FxRate.from_currency == from_currency)
    # Explicit ORDER BY -- any caller asserting on result order depends on
    # this, not on incidental insertion order.
    stmt = stmt.order_by(FxRate.from_currency, FxRate.effective_date.desc())
    return list((await session.execute(stmt)).scalars().all())


async def resolve_rate_row(session: AsyncSession, org_id: UUID, rate_id: UUID) -> FxRate:
    """Org-scoped load. A rate id from another org is indistinguishable
    from a nonexistent one -- both raise `BudgetNotFoundError` (spec §8,
    invariant #5)."""
    fx = (
        await session.execute(
            select(FxRate).where(FxRate.org_id == org_id, FxRate.id == rate_id)
        )
    ).scalar_one_or_none()
    if fx is None:
        raise BudgetNotFoundError(tr("fx rate {rate_id} not found", rate_id=rate_id))
    return fx


async def update_rate(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    rate_id: UUID,
    fields: dict[str, Any],
) -> FxRate:
    """Apply the SUPPLIED fields to a rate, org-scoped-loaded. Only `rate`
    is editable -- `from_currency`/`to_currency`/`effective_date` identify
    the row and are never touched here even if present in `fields`."""
    _reject_null_for_non_nullable(fields, _RATE_NON_NULLABLE_FIELDS)
    fx = await resolve_rate_row(session, org_id, rate_id)

    changed_fields = []
    for field in _RATE_EDITABLE_FIELDS:
        if field not in fields:
            continue
        if getattr(fx, field) != fields[field]:
            changed_fields.append(field)
        setattr(fx, field, fields[field])
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.fx_rate_updated",
        target_type="fx_rate",
        target_id=str(fx.id),
        metadata={"changed_fields": sorted(changed_fields)},
    )
    await session.flush()
    return fx


# ---------------------------------------------------------------------------
# budget_settings: base currency + fiscal year start
# ---------------------------------------------------------------------------


async def client_settings(
    session: AsyncSession, org_id: UUID, client_id: UUID
) -> BudgetSettings:
    """`GET /budget/settings?client_id=`: a client this session can see
    (else 404), and its settings -- the defaults until somebody sets them,
    with nothing written (`fx.get_settings`)."""
    await visible_client(session, org_id, client_id)
    return await get_settings(session, org_id, client_id)


async def settings_for_write(
    session: AsyncSession, org_id: UUID, client_id: UUID
) -> BudgetSettings:
    """The client's settings row, created with the documented defaults if it
    does not exist yet, and locked FOR UPDATE until commit. Call it after
    `client_for_budget_settings`.

    `ON CONFLICT DO NOTHING` and then a locking SELECT, not a check and an
    insert: two concurrent first writes for one client would otherwise both
    miss the row and the loser's INSERT would abort its transaction on
    `budget_settings_pkey` (the Task-5 F2 race, which used to live in
    `get_settings`). `populate_existing`, so what is changed is what was
    locked, even if this session read the row before."""
    await session.execute(
        pg_insert(BudgetSettings)
        .values(
            client_id=client_id,
            org_id=org_id,
            base_currency=DEFAULT_BASE_CURRENCY,
            fiscal_year_start_month=DEFAULT_FISCAL_YEAR_START_MONTH,
            updated_by=None,
        )
        .on_conflict_do_nothing(index_elements=["client_id"])
    )
    return (
        await session.execute(
            select(BudgetSettings)
            .where(BudgetSettings.org_id == org_id, BudgetSettings.client_id == client_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def update_settings(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    *,
    client_id: UUID,
    base_currency: str | None,
    fiscal_year_start_month: int | None,
    acknowledge_mixed_basis: bool = False,
) -> BudgetSettings:
    """`PUT /budget/settings?client_id=`: a client this session can see (else
    the 404 of one that does not exist), not archived (else the 409 a create
    gets, before anything is written or audited), that uses budget (else the
    module's 404) -- `client_for_budget_settings` -- then each supplied field
    through its own audited setter. A body that supplies neither writes
    nothing."""
    await client_for_budget_settings(session, org_id, client_id)
    if base_currency is not None:
        await set_base_currency(
            session,
            org_id,
            actor_id,
            client_id=client_id,
            base_currency=base_currency,
            acknowledge_mixed_basis=acknowledge_mixed_basis,
        )
    if fiscal_year_start_month is not None:
        await set_fiscal_year_start(
            session, org_id, actor_id, client_id=client_id, start_month=fiscal_year_start_month
        )
    return await get_settings(session, org_id, client_id)


async def set_base_currency(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    *,
    client_id: UUID,
    base_currency: str,
    acknowledge_mixed_basis: bool = False,
) -> BudgetSettings:
    """Spec §3's base-currency change rule, stated exactly, for one client:
    once ANY non-void expense of `client_id` carries a non-NULL
    `amount_base`, changing its base currency requires
    `acknowledge_mixed_basis=True` and is rejected without it.

    The guard runs as a query BEFORE any mutation -- org- and client-scoped
    (invariant #5, and each client's figures are in its own base), so another
    org's or another client's converted expenses can never block this
    client's change. Changing the base currency NEVER retro-converts stored
    rows: only `budget_settings.base_currency` (and the `updated_by`/
    `updated_at` marker) moves here; every existing `expense.fx_rate`/
    `amount_base` is left exactly as it was written."""
    await client_for_budget_settings(session, org_id, client_id)
    settings = await settings_for_write(session, org_id, client_id)
    old_currency = settings.base_currency

    if not acknowledge_mixed_basis:
        has_converted = (
            await session.execute(
                select(Expense.id)
                .where(
                    Expense.org_id == org_id,  # invariant #5
                    Expense.client_id == client_id,
                    Expense.status != "void",
                    Expense.amount_base.is_not(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if has_converted is not None:
            raise BudgetValidationError(
                tr(
                    "base currency cannot change: at least one non-void expense already carries a "
                    "converted amount_base; pass acknowledge_mixed_basis=True to proceed with a "
                    "mixed basis"
                )
            )

    settings.base_currency = base_currency
    settings.updated_by = actor_id
    settings.updated_at = datetime.now(UTC)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.base_currency_changed",
        target_type="budget_settings",
        target_id=str(client_id),
        client_id=client_id,
        metadata={"old": old_currency, "new": base_currency},
    )
    await session.flush()
    return settings


async def set_fiscal_year_start(
    session: AsyncSession, org_id: UUID, actor_id: UUID, *, client_id: UUID, start_month: int
) -> BudgetSettings:
    await client_for_budget_settings(session, org_id, client_id)
    settings = await settings_for_write(session, org_id, client_id)
    old_month = settings.fiscal_year_start_month

    settings.fiscal_year_start_month = start_month
    settings.updated_by = actor_id
    settings.updated_at = datetime.now(UTC)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.fiscal_year_start_changed",
        target_type="budget_settings",
        target_id=str(client_id),
        client_id=client_id,
        metadata={"old": old_month, "new": start_month},
    )
    await session.flush()
    return settings
