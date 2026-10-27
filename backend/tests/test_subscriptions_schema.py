"""Tests for the Subscriptions module's API schemas and typed errors
(docs/modules/subscriptions/spec.md, Task 3).
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.modules.subscriptions.errors import (
    SubscriptionNotFoundError,
    SubscriptionValidationError,
)
from app.modules.subscriptions.schema import (
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionUpdate,
)


def test_subscription_not_found_is_a_validation_error_subclass() -> None:
    assert issubclass(SubscriptionNotFoundError, SubscriptionValidationError)
    assert isinstance(SubscriptionNotFoundError("nope"), SubscriptionValidationError)


def test_subscription_create_billing_cycle_rejects_out_of_domain_value() -> None:
    with pytest.raises(ValidationError):
        SubscriptionCreate(
            client_id="00000000-0000-0000-0000-0000000000c1",
            service_name="Acme SaaS",
            billing_cycle="weekly",  # not in the allowed Literal
            cost_per_cycle=Decimal("9.99"),
            currency="USD",
        )


def test_subscription_create_status_rejects_out_of_domain_value() -> None:
    with pytest.raises(ValidationError):
        SubscriptionCreate(
            client_id="00000000-0000-0000-0000-0000000000c1",
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("9.99"),
            currency="USD",
            status="expired",  # not in the allowed Literal
        )


def test_subscription_create_currency_rejects_over_length_value() -> None:
    # currency maps to a varchar(3) column; an over-long value must be a clean
    # 422 at the schema boundary, not a Postgres truncation 500 (spec §8).
    with pytest.raises(ValidationError):
        SubscriptionCreate(
            client_id="00000000-0000-0000-0000-0000000000c1",
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("9.99"),
            currency="US Dollars",
        )


def test_subscription_update_currency_rejects_over_length_value() -> None:
    with pytest.raises(ValidationError):
        SubscriptionUpdate(currency="US Dollars")


# ---------------------------------------------------------------------------
# Bounded fields (I4): every schema field that maps to a bounded DB column must
# reject an out-of-range value at the schema boundary (422), because there is
# no global exception handler -- an unbounded value reaches Postgres and comes
# back as an unhandled 22001/22003 -> 500.
# ---------------------------------------------------------------------------


def _create(**overrides: object) -> SubscriptionCreate:
    payload: dict[str, object] = {
        "client_id": "00000000-0000-0000-0000-0000000000c1",
        "service_name": "Acme SaaS",
        "billing_cycle": "monthly",
        "cost_per_cycle": Decimal("9.99"),
        "currency": "USD",
    }
    payload.update(overrides)
    return SubscriptionCreate(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["service_name", "plan", "category", "payment_ref"])
def test_subscription_create_rejects_over_length_string(field: str) -> None:
    # each maps to a `String(255)` column
    with pytest.raises(ValidationError):
        _create(**{field: "x" * 256})


@pytest.mark.parametrize("field", ["service_name", "plan", "category", "payment_ref"])
def test_subscription_update_rejects_over_length_string(field: str) -> None:
    with pytest.raises(ValidationError):
        SubscriptionUpdate(**{field: "x" * 256})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["service_name", "plan", "category", "payment_ref"])
def test_subscription_create_accepts_max_length_string(field: str) -> None:
    sub = _create(**{field: "x" * 255})
    assert len(getattr(sub, field)) == 255


def test_subscription_create_rejects_cost_exceeding_numeric_precision() -> None:
    # cost_per_cycle maps to `Numeric(14, 2)`: at most 12 digits before the
    # decimal point. 1e13 overflows it (Postgres 22003).
    with pytest.raises(ValidationError):
        _create(cost_per_cycle=Decimal("1e13"))


def test_subscription_update_rejects_cost_exceeding_numeric_precision() -> None:
    with pytest.raises(ValidationError):
        SubscriptionUpdate(cost_per_cycle=Decimal("1e13"))


def test_subscription_create_rejects_cost_with_too_many_decimal_places() -> None:
    with pytest.raises(ValidationError):
        _create(cost_per_cycle=Decimal("1.234"))


def test_subscription_create_accepts_cost_at_numeric_precision_limit() -> None:
    sub = _create(cost_per_cycle=Decimal("999999999999.99"))
    assert sub.cost_per_cycle == Decimal("999999999999.99")


@pytest.mark.parametrize("field", ["seats", "notice_days"])
def test_subscription_create_rejects_negative_count(field: str) -> None:
    # a negative `seats` makes `available_seats`/`idle` negative
    with pytest.raises(ValidationError):
        _create(**{field: -5})


@pytest.mark.parametrize("field", ["seats", "notice_days"])
def test_subscription_update_rejects_negative_count(field: str) -> None:
    with pytest.raises(ValidationError):
        SubscriptionUpdate(**{field: -5})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["seats", "notice_days"])
def test_subscription_create_rejects_count_above_int32(field: str) -> None:
    # Both columns are Postgres `Integer` (int4). Without an upper bound the
    # value reaches asyncpg, which raises `DataError: value out of int32
    # range` -- unhandled, so a 500 rather than a 422.
    with pytest.raises(ValidationError):
        _create(**{field: 2_147_483_648})


@pytest.mark.parametrize("field", ["seats", "notice_days"])
def test_subscription_update_rejects_count_above_int32(field: str) -> None:
    with pytest.raises(ValidationError):
        SubscriptionUpdate(**{field: 2_147_483_648})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["seats", "notice_days"])
def test_subscription_create_accepts_count_at_int32_max(field: str) -> None:
    sub = _create(**{field: 2_147_483_647})
    assert getattr(sub, field) == 2_147_483_647


@pytest.mark.parametrize("field", ["seats", "notice_days"])
def test_subscription_create_accepts_zero_count(field: str) -> None:
    sub = _create(**{field: 0})
    assert getattr(sub, field) == 0


def test_subscription_create_valid_with_cost_and_currency() -> None:
    sub = SubscriptionCreate(
        client_id="00000000-0000-0000-0000-0000000000c1",
        service_name="Acme SaaS",
        billing_cycle="monthly",
        cost_per_cycle=Decimal("9.99"),
        currency="USD",
    )
    assert sub.cost_per_cycle == Decimal("9.99")
    assert sub.currency == "USD"
    assert sub.billing_cycle == "monthly"
    assert sub.status == "active"


def test_subscription_out_has_no_org_id_field() -> None:
    assert "org_id" not in SubscriptionOut.model_fields
    assert "id" in SubscriptionOut.model_fields
