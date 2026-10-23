"""Unit tests for pure cost-normalization + renewal-date helpers
(docs/modules/subscriptions/spec.md §5). No DB, no async — plain functions.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.modules.subscriptions.costs import (
    advance_renewal,
    annualized,
    monthly_equivalent,
)
from app.modules.subscriptions.errors import SubscriptionValidationError


class TestMonthlyEquivalent:
    def test_annual(self) -> None:
        assert monthly_equivalent(Decimal("120"), "annual") == Decimal("10.00")

    def test_quarterly(self) -> None:
        assert monthly_equivalent(Decimal("30"), "quarterly") == Decimal("10.00")

    def test_monthly(self) -> None:
        assert monthly_equivalent(Decimal("10"), "monthly") == Decimal("10.00")

    def test_round_half_up(self) -> None:
        # 100 / 12 = 8.3333... -> rounds to 8.33
        assert monthly_equivalent(Decimal("100"), "annual") == Decimal("8.33")

    def test_unknown_cycle_raises(self) -> None:
        with pytest.raises(SubscriptionValidationError):
            monthly_equivalent(Decimal("10"), "weekly")


class TestAnnualized:
    def test_monthly(self) -> None:
        assert annualized(Decimal("10"), "monthly") == Decimal("120.00")

    def test_quarterly(self) -> None:
        assert annualized(Decimal("30"), "quarterly") == Decimal("120.00")

    def test_annual(self) -> None:
        assert annualized(Decimal("50"), "annual") == Decimal("50.00")

    def test_unknown_cycle_raises(self) -> None:
        with pytest.raises(SubscriptionValidationError):
            annualized(Decimal("10"), "weekly")


class TestAdvanceRenewal:
    def test_monthly_clamps_to_month_end(self) -> None:
        assert advance_renewal(date(2026, 1, 31), "monthly") == date(2026, 2, 28)

    def test_monthly_clamps_to_leap_day(self) -> None:
        assert advance_renewal(date(2024, 1, 31), "monthly") == date(2024, 2, 29)

    def test_annual(self) -> None:
        assert advance_renewal(date(2026, 1, 15), "annual") == date(2027, 1, 15)

    def test_quarterly(self) -> None:
        assert advance_renewal(date(2026, 1, 15), "quarterly") == date(2026, 4, 15)

    def test_quarterly_year_rollover_and_clamp(self) -> None:
        assert advance_renewal(date(2026, 11, 30), "quarterly") == date(2027, 2, 28)

    def test_unknown_cycle_raises(self) -> None:
        with pytest.raises(SubscriptionValidationError):
            advance_renewal(date(2026, 1, 15), "weekly")
