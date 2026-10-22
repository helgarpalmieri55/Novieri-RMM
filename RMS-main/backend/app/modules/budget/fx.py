"""FX rate resolution and conversion (docs/modules/budget/spec.md §3).

This module owns FX for the whole financial trio -- Licenses and
Subscriptions both explicitly defer currency conversion to it. Every query
here carries an explicit `org_id` predicate (invariant #5), mirroring
`app.modules.subscriptions.service`'s style.

Four rules, each a real failure mode if broken:
  - pick the greatest effective_date <= d (never a later rate)
  - never extrapolate backwards from a later rate
  - never invert the opposite pair
  - base -> base is exactly 1 with no lookup
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import BudgetSettings, FxRate
from app.modules.budget.errors import BudgetValidationError

_TWO_PLACES = Decimal("0.01")

# Numeric(14,2) ceiling -- the column `convert`'s result ultimately lands in
# (`expense.amount_base`). 12 integer digits, 2 decimal.
_MAX_BASE_AMOUNT = Decimal("999999999999.99")


async def get_settings(session: AsyncSession, org_id: UUID) -> BudgetSettings:
    """Load the org's `budget_settings` row, creating it lazily on first read
    with the documented defaults (spec §2). `updated_by` stays `None` --
    that is what marks the row as never operator-set.

    `AsyncSession` defaults to `autoflush=True` and nothing overrides it
    (`app.core.tenancy.org_session`/`admin_session` construct plain
    `AsyncSession`), so the initial SELECT below autoflushes ALL pending
    objects on the session on EVERY call to this function -- not just once
    per org, and not just on the first-ever call. A caller that does
    `session.add(expense)` before calling `convert_for` (which calls this)
    gets `expense` written to the table before `amount_base` is computed,
    including the case where `convert`'s overflow guard then raises and
    forces a rollback of a row that already hit the table.

    The insert is `ON CONFLICT DO NOTHING` + re-select rather than a plain
    insert: `org_id` is the PK, so two concurrent first-ever calls for the
    same org would otherwise both miss the SELECT and both attempt the
    insert, and the loser would get an `IntegrityError` that aborts its
    transaction (see the Task-5 fix directive, finding F2)."""
    settings = (
        await session.execute(
            select(BudgetSettings).where(BudgetSettings.org_id == org_id)
        )
    ).scalar_one_or_none()
    if settings is not None:
        return settings
    await session.execute(
        pg_insert(BudgetSettings)
        .values(
            org_id=org_id,
            base_currency="USD",
            fiscal_year_start_month=1,
            updated_by=None,
        )
        .on_conflict_do_nothing(index_elements=["org_id"])
    )
    await session.flush()
    return (
        await session.execute(
            select(BudgetSettings).where(BudgetSettings.org_id == org_id)
        )
    ).scalar_one()


def convert(amount: Decimal, rate: Decimal) -> Decimal:
    """Apply a rate and round ONCE, at the end.

    Raises `BudgetValidationError` if the converted result would overflow
    `expense.amount_base`'s `Numeric(14,2)` column (~1e12): `amount` is
    bounded (14,2) and `rate` (18,8), but their product is unbounded, and a
    high-nominal base currency (COP, IDR, VND) can cross that ceiling on an
    ordinary expense. Spec §8 forbids a raw DB overflow surfacing as a 500."""
    result = (amount * rate).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if abs(result) > _MAX_BASE_AMOUNT:
        raise BudgetValidationError(
            tr(
                "converted amount {result} (amount={amount}, rate={rate}) exceeds the maximum "
                "representable base amount {max_base_amount}",
                result=result,
                amount=amount,
                rate=rate,
                max_base_amount=_MAX_BASE_AMOUNT,
            )
        )
    return result


async def resolve_rate(
    session: AsyncSession, org_id: UUID, from_currency: str, on_date: date
) -> Decimal | None:
    """Resolve the rate for `from_currency` -> the org's base currency, as of
    `on_date`.

    Base -> base is exactly `1` with no lookup. Otherwise the greatest
    `effective_date <= on_date` for the (from_currency, base_currency) pair
    is used -- never a later rate, and never the inverted opposite pair."""
    settings = await get_settings(session, org_id)
    if from_currency == settings.base_currency:
        return Decimal("1")
    stmt = (
        select(FxRate.rate)
        .where(
            FxRate.org_id == org_id,  # invariant #5
            FxRate.from_currency == from_currency,
            FxRate.to_currency == settings.base_currency,
            FxRate.effective_date <= on_date,  # never a later rate
        )
        .order_by(FxRate.effective_date.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def convert_for(
    session: AsyncSession,
    org_id: UUID,
    amount: Decimal,
    currency: str,
    on_date: date,
) -> tuple[Decimal | None, Decimal | None]:
    """Resolve the rate for `currency` as of `on_date` and convert `amount`.

    Returns `(fx_rate, amount_base)`; both `None` when the rate is
    unresolvable rather than silently defaulting to 1 or skipping
    conversion. Can also raise `BudgetValidationError` -- propagated
    unchanged from `convert`'s `Numeric(14,2)` overflow guard -- so a
    caller that only handles the `(None, None)` unresolvable case will let
    that overflow escape."""
    rate = await resolve_rate(session, org_id, currency, on_date)
    if rate is None:
        return None, None
    return rate, convert(amount, rate)
