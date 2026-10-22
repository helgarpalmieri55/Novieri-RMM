"""Pure fiscal-period helpers (docs/modules/budget/spec.md §4).

No DB, no async -- plain functions, fully unit-testable in isolation, in the
same spirit as app/modules/subscriptions/costs.py. Period boundaries are
NEVER stored: they are derived here from (fiscal_year, start_month,
granularity) so a single helper is the only place that knows the calendar.
"""

import calendar
from datetime import date

from app.core.i18n import tr
from app.core.wire_words import say
from app.modules.budget.errors import BudgetValidationError
from app.modules.budget.words import GRANULARITY_WORDS

_PERIODS = {"month": 12, "quarter": 4}
_MONTHS_PER = {"month": 1, "quarter": 3}


def period_count(granularity: str) -> int:
    try:
        return _PERIODS[granularity]
    except KeyError:
        raise BudgetValidationError(
            tr("unknown granularity {granularity}", granularity=repr(granularity))
        ) from None


def _add_months(y: int, m: int, delta: int) -> tuple[int, int]:
    total = (y * 12 + (m - 1)) + delta
    return total // 12, total % 12 + 1


def period_bounds(
    fiscal_year: int, start_month: int, granularity: str, period_index: int
) -> tuple[date, date]:
    count = period_count(granularity)
    if not 1 <= period_index <= count:
        raise BudgetValidationError(
            tr(
                "period_index {period_index} out of range 1..{count} for {granularity}",
                period_index=period_index,
                count=count,
                granularity=say(GRANULARITY_WORDS, granularity),
            )
        )
    span = _MONTHS_PER[granularity]
    sy, sm = _add_months(fiscal_year, start_month, (period_index - 1) * span)
    ey, em = _add_months(fiscal_year, start_month, period_index * span - 1)
    return date(sy, sm, 1), date(ey, em, calendar.monthrange(ey, em)[1])


def period_for_date(
    fiscal_year: int, start_month: int, granularity: str, d: date
) -> int | None:
    for idx in range(1, period_count(granularity) + 1):
        start, end = period_bounds(fiscal_year, start_month, granularity, idx)
        if start <= d <= end:
            return idx
    return None


def period_key_for_date(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"
