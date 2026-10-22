"""Pure cost-normalization + renewal-date helpers (docs/modules/subscriptions/spec.md §5).

No DB, no async — plain functions, fully unit-testable in isolation. All
monetary math is per-record (see spec §0.4: per-record currency, no FX; the
Budget module owns cross-currency roll-ups).
"""

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.core.i18n import tr
from app.modules.subscriptions.errors import SubscriptionValidationError

_CYCLE_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}

_TWO_PLACES = Decimal("0.01")


def _months(billing_cycle: str) -> int:
    """Look up the number of months in a billing cycle, or raise."""
    try:
        return _CYCLE_MONTHS[billing_cycle]
    except KeyError:
        raise SubscriptionValidationError(
            tr("unknown billing_cycle {billing_cycle}", billing_cycle=repr(billing_cycle))
        ) from None


def monthly_equivalent(cost_per_cycle: Decimal, billing_cycle: str) -> Decimal:
    """Normalize a per-cycle cost to a monthly-equivalent amount."""
    months = _months(billing_cycle)
    return (cost_per_cycle / months).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def annualized(cost_per_cycle: Decimal, billing_cycle: str) -> Decimal:
    """Normalize a per-cycle cost to an annualized amount."""
    months = _months(billing_cycle)
    cycles_per_year = 12 // months
    return (cost_per_cycle * cycles_per_year).quantize(
        _TWO_PLACES, rounding=ROUND_HALF_UP
    )


def advance_renewal(current: date, billing_cycle: str) -> date:
    """Advance a renewal date by one billing cycle, clamping the day to the
    target month's last valid day (e.g. Jan 31 + 1mo -> Feb 28/29)."""
    months = _months(billing_cycle)

    total_months = current.month - 1 + months
    year = current.year + total_months // 12
    month = total_months % 12 + 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
