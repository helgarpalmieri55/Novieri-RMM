"""Pure fiscal-period math (spec §4). Calendar-correct bounds -- no 30-day
approximations, and February/leap years are explicit cases."""

from datetime import date

import pytest

from app.modules.budget.errors import BudgetValidationError
from app.modules.budget.periods import (
    period_bounds,
    period_count,
    period_for_date,
    period_key_for_date,
)


def test_period_count_by_granularity() -> None:
    assert period_count("month") == 12
    assert period_count("quarter") == 4


def test_unknown_granularity_raises() -> None:
    with pytest.raises(BudgetValidationError):
        period_count("weekly")


def test_calendar_year_month_bounds() -> None:
    assert period_bounds(2026, 1, "month", 1) == (date(2026, 1, 1), date(2026, 1, 31))
    assert period_bounds(2026, 1, "month", 12) == (date(2026, 12, 1), date(2026, 12, 31))


def test_february_is_28_days_and_leap_february_is_29() -> None:
    assert period_bounds(2026, 1, "month", 2) == (date(2026, 2, 1), date(2026, 2, 28))
    assert period_bounds(2028, 1, "month", 2) == (date(2028, 2, 1), date(2028, 2, 29))


def test_non_calendar_fiscal_year_starting_in_july() -> None:
    """FY2026 starting in month 7 runs 2026-07-01 .. 2027-06-30."""
    assert period_bounds(2026, 7, "month", 1) == (date(2026, 7, 1), date(2026, 7, 31))
    assert period_bounds(2026, 7, "month", 12) == (date(2027, 6, 1), date(2027, 6, 30))


def test_quarter_bounds_span_three_months() -> None:
    assert period_bounds(2026, 1, "quarter", 1) == (date(2026, 1, 1), date(2026, 3, 31))
    assert period_bounds(2026, 1, "quarter", 4) == (date(2026, 10, 1), date(2026, 12, 31))


def test_non_calendar_quarter_crosses_the_year_boundary() -> None:
    assert period_bounds(2026, 7, "quarter", 3) == (date(2027, 1, 1), date(2027, 3, 31))


@pytest.mark.parametrize("bad", [0, 13])
def test_month_period_index_out_of_range_raises(bad: int) -> None:
    with pytest.raises(BudgetValidationError):
        period_bounds(2026, 1, "month", bad)


@pytest.mark.parametrize("bad", [0, 5])
def test_quarter_period_index_out_of_range_raises(bad: int) -> None:
    # A quarterly budget has 4 periods, so 5 is the first index past the end.
    # 12 would be legal for a MONTHLY budget -- the bound is granularity-
    # specific, which is exactly what makes this worth testing on both sides.
    with pytest.raises(BudgetValidationError):
        period_bounds(2026, 1, "quarter", bad)


def test_period_for_date_at_both_inclusive_edges() -> None:
    assert period_for_date(2026, 7, "month", date(2026, 7, 1)) == 1
    assert period_for_date(2026, 7, "month", date(2026, 7, 31)) == 1
    assert period_for_date(2026, 7, "month", date(2026, 8, 1)) == 2
    assert period_for_date(2026, 7, "month", date(2027, 6, 30)) == 12


def test_period_for_date_outside_the_fiscal_year_is_none() -> None:
    assert period_for_date(2026, 7, "month", date(2026, 6, 30)) is None
    assert period_for_date(2026, 7, "month", date(2027, 7, 1)) is None


def test_period_key_is_canonical_year_month() -> None:
    assert period_key_for_date(date(2026, 3, 9)) == "2026-03"
    assert period_key_for_date(date(2026, 11, 30)) == "2026-11"
