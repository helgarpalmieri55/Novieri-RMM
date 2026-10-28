"""FX rate resolution and conversion (docs/modules/budget/spec.md §3).

This module owns FX for the whole financial trio -- Licenses and
Subscriptions both explicitly defer currency conversion to it. Every query
here carries an explicit `org_id` predicate (invariant #5), mirroring
`app.modules.subscriptions.service`'s style.

Each CLIENT has its own base currency and fiscal year (MSP clients plan
2026-10-26 Task 8): conversion is into the base currency of the client the
amount belongs to. The rates themselves (`fx_rate`) are the organisation's:
one EUR->USD rate serves every client whose base is USD.

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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import BudgetSettings, FxRate
from app.modules.budget.errors import BudgetValidationError

# What a client's settings are until somebody sets them (spec §2).
DEFAULT_BASE_CURRENCY = "USD"
DEFAULT_FISCAL_YEAR_START_MONTH = 1

_TWO_PLACES = Decimal("0.01")

# Numeric(14,2) ceiling -- the column `convert`'s result ultimately lands in
# (`expense.amount_base`). 12 integer digits, 2 decimal.
_MAX_BASE_AMOUNT = Decimal("999999999999.99")


async def get_settings(session: AsyncSession, org_id: UUID, client_id: UUID) -> BudgetSettings:
    """The client's `budget_settings` row, or -- until somebody has set it --
    the documented defaults (spec §2), as an UNSAVED row: `updated_by` and
    `updated_at` are `None`, which is what marks it never operator-set.

    PURE: this never writes (plan 2026-10-26 Task 8). It used to insert the
    row lazily on the first read, which on a client without the budget module
    wrote a row on a GET, and on any client attributed a write to a reader.
    The row is created by the first change to it
    (`rates.settings_for_write`). The `org_id` predicate mirrors RLS rather
    than relying on it, so a caller on the admin engine cannot read another
    organisation's client's settings.

    `AsyncSession` defaults to `autoflush=True` and nothing overrides it
    (`app.core.tenancy.org_session`/`admin_session` construct plain
    `AsyncSession`), so the SELECT below autoflushes ALL pending objects on
    the session on EVERY call to this function. A caller that does
    `session.add(expense)` before calling `convert_for` (which calls this)
    gets `expense` written to the table before `amount_base` is computed,
    including the case where `convert`'s overflow guard then raises and
    forces a rollback of a row that already hit the table."""
    settings = (
        await session.execute(
            select(BudgetSettings).where(
                BudgetSettings.org_id == org_id, BudgetSettings.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if settings is not None:
        return settings
    return BudgetSettings(
        org_id=org_id,
        client_id=client_id,
        base_currency=DEFAULT_BASE_CURRENCY,
        fiscal_year_start_month=DEFAULT_FISCAL_YEAR_START_MONTH,
        updated_by=None,
    )


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
    session: AsyncSession, org_id: UUID, from_currency: str, on_date: date, *, client_id: UUID
) -> Decimal | None:
    """Resolve the rate for `from_currency` -> the base currency of
    `client_id`, as of `on_date`, from the organisation's rates.

    Base -> base is exactly `1` with no lookup. Otherwise the greatest
    `effective_date <= on_date` for the (from_currency, base_currency) pair
    is used -- never a later rate, and never the inverted opposite pair."""
    settings = await get_settings(session, org_id, client_id)
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
    *,
    client_id: UUID,
) -> tuple[Decimal | None, Decimal | None]:
    """Resolve the rate for `currency` as of `on_date` and convert `amount`
    into the base currency of `client_id`, the client it belongs to.

    Returns `(fx_rate, amount_base)`; both `None` when the rate is
    unresolvable rather than silently defaulting to 1 or skipping
    conversion. Can also raise `BudgetValidationError` -- propagated
    unchanged from `convert`'s `Numeric(14,2)` overflow guard -- so a
    caller that only handles the `(None, None)` unresolvable case will let
    that overflow escape."""
    rate = await resolve_rate(session, org_id, currency, on_date, client_id=client_id)
    if rate is None:
        return None, None
    return rate, convert(amount, rate)
