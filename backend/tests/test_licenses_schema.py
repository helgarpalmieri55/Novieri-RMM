"""Tests for the Licenses/SAM module's API schemas and typed errors
(docs/modules/licenses/spec.md, Task 3).
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.modules.licenses.errors import LicenseNotFoundError, LicenseValidationError
from app.modules.licenses.schema import (
    LicenseCreate,
    LicenseOut,
    MatchRule,
    RenewalRow,
)


def test_license_not_found_is_a_validation_error_subclass() -> None:
    assert issubclass(LicenseNotFoundError, LicenseValidationError)
    assert isinstance(LicenseNotFoundError("nope"), LicenseValidationError)


def test_license_create_type_rejects_out_of_domain_value() -> None:
    with pytest.raises(ValidationError):
        LicenseCreate(
            client_id="00000000-0000-0000-0000-0000000000c1",
            product_id="00000000-0000-0000-0000-000000000001",
            type="subscription",  # not in the allowed Literal
            entitlements=10,
        )


def test_license_create_valid_with_cost_and_currency() -> None:
    lic = LicenseCreate(
        client_id="00000000-0000-0000-0000-0000000000c1",
        product_id="00000000-0000-0000-0000-000000000001",
        type="perpetual",
        entitlements=10,
        cost=Decimal("10.00"),
        currency="USD",
    )
    assert lic.cost == Decimal("10.00")
    assert lic.currency == "USD"


def test_license_currency_rejects_over_length_value() -> None:
    # currency maps to a varchar(3) column; an over-long value must be a clean
    # 422 at the schema boundary, not a Postgres truncation 500 (spec §9).
    with pytest.raises(ValidationError):
        LicenseCreate(
            client_id="00000000-0000-0000-0000-0000000000c1",
            product_id="00000000-0000-0000-0000-000000000001",
            type="perpetual",
            entitlements=10,
            currency="US Dollars",
        )


def test_license_update_type_rejects_out_of_domain_value() -> None:
    from app.modules.licenses.schema import LicenseUpdate

    with pytest.raises(ValidationError):
        LicenseUpdate(type="subscription")  # not in the shared LicenseType Literal


def test_license_out_has_no_key_or_key_sealed_field() -> None:
    assert "key" not in LicenseOut.model_fields
    assert "key_sealed" not in LicenseOut.model_fields
    assert "has_key" in LicenseOut.model_fields


def test_allocation_create_target_type_rejects_out_of_domain_value() -> None:
    from app.modules.licenses.schema import AllocationCreate

    with pytest.raises(ValidationError):
        AllocationCreate(
            target_type="group",  # not "device" or "user"
            target_id="00000000-0000-0000-0000-000000000001",
        )


def test_match_rule_accepts_all_three_types_and_defaults_publisher_none() -> None:
    for rule_type in ("exact", "prefix", "regex"):
        rule = MatchRule(type=rule_type, pattern="Acme*")
        assert rule.publisher is None
        assert rule.type == rule_type


def test_match_rule_rejects_out_of_domain_type() -> None:
    with pytest.raises(ValidationError):
        MatchRule(type="fuzzy", pattern="Acme*")


def test_renewal_row_trigger_rejects_out_of_domain_value() -> None:
    with pytest.raises(ValidationError):
        RenewalRow(
            license_id="00000000-0000-0000-0000-000000000001",
            product_id="00000000-0000-0000-0000-000000000002",
            trigger="expiry",  # not "renewal" or "maintenance"
            due_date="2026-01-01",
            due_in_days=30,
        )
