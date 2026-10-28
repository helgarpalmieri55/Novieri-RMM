"""Tests for the Budget module's API schemas and typed errors
(docs/modules/budget/spec.md, Task 3).

Schema-boundary bounds (spec §8). Every one of these, unbounded, is a
reachable 500: the value passes Pydantic, reaches asyncpg, and raises an
unhandled DataError. Phase 11 hit it with currency; Phase 12 hit it with
strings, cost precision, and int32 counts.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.schema import (
    BudgetCreate,
    BudgetLineInput,
    BudgetLinesPut,
    BudgetOut,
    BudgetUpdate,
    CategoryCreate,
    CategoryUpdate,
    CostCenterCreate,
    CostCenterUpdate,
    ExpenseConfirm,
    ExpenseCreate,
    ExpenseOut,
    ExpenseUpdate,
    ForecastRow,
    FxRateCreate,
    FxRateUpdate,
    MaterializeRequest,
    PositionOut,
    SettingsUpdate,
    VarianceRow,
)

_UUID = uuid4()
_DATE = date(2026, 1, 1)


def test_not_found_is_a_validation_error_subclass() -> None:
    assert issubclass(BudgetNotFoundError, BudgetValidationError)
    assert isinstance(BudgetNotFoundError("nope"), BudgetValidationError)


# ---------------------------------------------------------------------------
# Expense: currency, money precision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["USDD", "US", ""])
def test_expense_rejects_bad_currency_length(bad: str) -> None:
    with pytest.raises(ValidationError):
        ExpenseCreate(
            client_id=_UUID, amount=Decimal("1"), currency=bad, category_id=_UUID,
            expense_date=_DATE,
        )


def test_expense_rejects_amount_beyond_numeric_precision() -> None:
    # numeric(14,2) holds at most 12 integer digits
    with pytest.raises(ValidationError):
        ExpenseCreate(
            client_id=_UUID, amount=Decimal("1234567890123.00"), currency="USD",
            category_id=_UUID, expense_date=_DATE,
        )


def test_expense_rejects_amount_with_too_many_decimal_places() -> None:
    with pytest.raises(ValidationError):
        ExpenseCreate(
            client_id=_UUID, amount=Decimal("1.234"), currency="USD",
            category_id=_UUID, expense_date=_DATE,
        )


def test_expense_accepts_amount_at_precision_limit() -> None:
    exp = ExpenseCreate(
        client_id=_UUID, amount=Decimal("999999999999.99"), currency="USD",
        category_id=_UUID, expense_date=_DATE,
    )
    assert exp.amount == Decimal("999999999999.99")


def test_expense_create_defaults_to_expected_and_empty_description() -> None:
    # spec §5: "a row is never `confirmed` by omission" -- an unbound UI
    # form field, a CSV importer, or an ops-runbook curl that omits `status`
    # must land in the confirmation queue (`status=expected`), never book
    # straight into spec §7's `actual` column.
    exp = ExpenseCreate(
        client_id=_UUID, amount=Decimal("1"), currency="USD", category_id=_UUID, expense_date=_DATE
    )
    assert exp.status == "expected"
    assert exp.description == ""
    assert exp.cost_center_id is None
    assert exp.vendor_id is None


def test_expense_create_rejects_out_of_domain_status() -> None:
    with pytest.raises(ValidationError):
        ExpenseCreate(
            client_id=_UUID, amount=Decimal("1"), currency="USD", category_id=_UUID,
            expense_date=_DATE,
            status="void",  # not in the Create Literal -- confirm/void have dedicated endpoints
        )


def test_expense_create_rejects_out_of_domain_link_type() -> None:
    with pytest.raises(ValidationError):
        ExpenseCreate(
            client_id=_UUID, amount=Decimal("1"), currency="USD", category_id=_UUID,
            expense_date=_DATE,
            link_type="invoice",
        )


def test_expense_create_rejects_over_length_description() -> None:
    with pytest.raises(ValidationError):
        ExpenseCreate(
            client_id=_UUID, amount=Decimal("1"), currency="USD", category_id=_UUID,
            expense_date=_DATE,
            description="x" * 1001,
        )


def test_expense_create_accepts_max_length_description() -> None:
    exp = ExpenseCreate(
        client_id=_UUID, amount=Decimal("1"), currency="USD", category_id=_UUID, expense_date=_DATE,
        description="x" * 1000,
    )
    assert len(exp.description) == 1000


def test_expense_create_rejects_over_length_invoice_ref() -> None:
    with pytest.raises(ValidationError):
        ExpenseCreate(
            client_id=_UUID, amount=Decimal("1"), currency="USD", category_id=_UUID,
            expense_date=_DATE,
            invoice_ref="x" * 129,
        )


def test_expense_create_accepts_max_length_invoice_ref() -> None:
    # expense.invoice_ref is varchar(128) (migration 0020).
    exp = ExpenseCreate(
        client_id=_UUID, amount=Decimal("1"), currency="USD", category_id=_UUID, expense_date=_DATE,
        invoice_ref="x" * 128,
    )
    assert exp.invoice_ref is not None and len(exp.invoice_ref) == 128


def test_expense_out_has_no_org_id_field() -> None:
    assert "org_id" not in ExpenseOut.model_fields
    assert "id" in ExpenseOut.model_fields


def test_expense_out_serializes_money_as_json_strings() -> None:
    # spec §8: Decimal throughout, JSON strings on the wire. Pydantic v2
    # serializes Decimal to a JSON string by default -- this is the exact
    # convention SubscriptionOut relies on (no field_serializer needed).
    out = ExpenseOut(
        client_id=_UUID, id=_UUID, expense_date=_DATE, amount=Decimal("1234.56"),
        currency="USD",
        fx_rate=Decimal("4100.12345678"), amount_base=Decimal("5061851.75"),
        vendor_id=None, category_id=_UUID, cost_center_id=None, description="",
        source="manual", link_type=None, link_id=None, period_key=None,
        invoice_ref=None, has_attachment=False, invoice_filename=None, status="confirmed",
        entered_by=_UUID, created_at="2026-01-01T00:00:00Z",
        confirmed_by=None, confirmed_at=None, voided_by=None, voided_at=None,
    )
    dumped = out.model_dump(mode="json")
    assert dumped["amount"] == "1234.56"
    assert dumped["fx_rate"] == "4100.12345678"
    assert dumped["amount_base"] == "5061851.75"
    assert isinstance(dumped["amount"], str)


def test_expense_update_every_field_defaults_to_none() -> None:
    # Task 9 applies this with exclude_unset=True -- every field must accept
    # omission (default) so an untouched field is distinguishable from an
    # explicit null.
    upd = ExpenseUpdate()
    for name in ExpenseUpdate.model_fields:
        assert getattr(upd, name) is None, name


# ---------------------------------------------------------------------------
# FxRate: rate must be positive, both ends of numeric(18,8) bounded
# ---------------------------------------------------------------------------


def test_fx_rate_rejects_zero_and_negative() -> None:
    for bad in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValidationError):
            FxRateCreate(from_currency="COP", rate=bad, effective_date=_DATE)


def test_fx_rate_rejects_over_length_currency() -> None:
    with pytest.raises(ValidationError):
        FxRateCreate(from_currency="COPX", rate=Decimal("1"), effective_date=_DATE)


def test_fx_rate_accepts_max_precision_rate() -> None:
    rate = FxRateCreate(
        from_currency="COP", rate=Decimal("9999999999.99999999"), effective_date=_DATE
    )
    assert rate.rate == Decimal("9999999999.99999999")


def test_fx_rate_update_rejects_zero_and_negative() -> None:
    for bad in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValidationError):
            FxRateUpdate(rate=bad)


def test_fx_rate_update_defaults_to_none() -> None:
    assert FxRateUpdate().rate is None


# ---------------------------------------------------------------------------
# Budget: fiscal_year bounds, granularity domain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [1999, 2101])
def test_budget_rejects_out_of_range_fiscal_year(bad: int) -> None:
    with pytest.raises(ValidationError):
        BudgetCreate(client_id=_UUID, fiscal_year=bad, name="FY", granularity="month")


def test_budget_accepts_boundary_fiscal_years() -> None:
    for year in (2000, 2100):
        assert BudgetCreate(
            client_id=_UUID, fiscal_year=year, name="FY", granularity="month"
        ).fiscal_year == year


def test_budget_rejects_out_of_domain_granularity() -> None:
    with pytest.raises(ValidationError):
        BudgetCreate(client_id=_UUID, fiscal_year=2026, name="FY", granularity="weekly")


def test_budget_rejects_over_length_name() -> None:
    with pytest.raises(ValidationError):
        BudgetCreate(client_id=_UUID, fiscal_year=2026, name="x" * 256, granularity="month")


def test_budget_accepts_max_length_name() -> None:
    # budget.name is varchar(255) (migration 0020).
    b = BudgetCreate(client_id=_UUID, fiscal_year=2026, name="x" * 255, granularity="month")
    assert len(b.name) == 255


def test_budget_out_has_no_org_id_field() -> None:
    assert "org_id" not in BudgetOut.model_fields


def test_budget_update_rejects_out_of_domain_status() -> None:
    with pytest.raises(ValidationError):
        BudgetUpdate(status="deleted")  # type: ignore[arg-type]


def test_budget_update_rejects_closed_status() -> None:
    # "closed" is a real BudgetStatus value (a stored budget can be closed),
    # but the PATCH surface must not accept it -- the transition has to go
    # through POST .../close, which also runs the line-freeze and audit
    # event a bare status flip would bypass (spec §4, §9).
    with pytest.raises(ValidationError):
        BudgetUpdate(status="closed")  # type: ignore[arg-type]


def test_budget_update_all_fields_optional() -> None:
    upd = BudgetUpdate()
    assert upd.name is None
    assert upd.status is None


# ---------------------------------------------------------------------------
# Categories / cost centers: string bounds, positive-int bounds on both ends
# ---------------------------------------------------------------------------


def test_category_create_rejects_over_length_key() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(client_id=_UUID, key="x" * 65, name="Hardware")


def test_category_create_accepts_max_length_key() -> None:
    # expense_category.key is varchar(64) (migration 0020) -- a bound one
    # below this would silently 422 input the database would accept.
    cat = CategoryCreate(client_id=_UUID, key="x" * 64, name="Hardware")
    assert len(cat.key) == 64


def test_category_create_rejects_over_length_name() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(client_id=_UUID, key="hardware", name="x" * 256)


def test_category_create_accepts_max_length_name() -> None:
    # expense_category.name is varchar(255) (migration 0020).
    cat = CategoryCreate(client_id=_UUID, key="hardware", name="x" * 255)
    assert len(cat.name) == 255


def test_category_create_rejects_negative_sort_order() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(client_id=_UUID, key="hardware", name="Hardware", sort_order=-1)


def test_category_create_rejects_sort_order_above_int32() -> None:
    # sort_order maps to Postgres Integer (int4); above int32 range asyncpg
    # raises DataError -- unhandled -- so this must be a clean 422 instead.
    with pytest.raises(ValidationError):
        CategoryCreate(client_id=_UUID, key="hardware", name="Hardware", sort_order=2_147_483_648)


def test_category_create_accepts_sort_order_at_int32_max() -> None:
    cat = CategoryCreate(client_id=_UUID, key="hardware", name="Hardware", sort_order=2_147_483_647)
    assert cat.sort_order == 2_147_483_647


def test_category_update_has_no_key_or_system_key_field() -> None:
    # system_key is the stable identifier the auto-feeds resolve against
    # (spec §2/§6.1) and is never operator-set; key is not renamed via PATCH.
    assert "key" not in CategoryUpdate.model_fields
    assert "system_key" not in CategoryUpdate.model_fields


def test_cost_center_create_rejects_over_length_code() -> None:
    with pytest.raises(ValidationError):
        CostCenterCreate(client_id=_UUID, code="x" * 33, name="Ops")


def test_cost_center_create_accepts_max_length_code() -> None:
    # cost_center.code is varchar(32) (migration 0020).
    cc = CostCenterCreate(client_id=_UUID, code="x" * 32, name="Ops")
    assert len(cc.code) == 32


def test_cost_center_create_rejects_over_length_external_ref() -> None:
    with pytest.raises(ValidationError):
        CostCenterCreate(client_id=_UUID, code="ops", name="Ops", external_ref="x" * 65)


def test_cost_center_create_accepts_max_length_external_ref() -> None:
    # cost_center.external_ref is varchar(64) (migration 0020).
    cc = CostCenterCreate(client_id=_UUID, code="ops", name="Ops", external_ref="x" * 64)
    assert cc.external_ref is not None and len(cc.external_ref) == 64


def test_cost_center_update_rejects_over_length_name() -> None:
    with pytest.raises(ValidationError):
        CostCenterUpdate(name="x" * 256)


def test_cost_center_update_accepts_max_length_name() -> None:
    # cost_center.name is varchar(255) (migration 0020).
    upd = CostCenterUpdate(name="x" * 255)
    assert upd.name is not None and len(upd.name) == 255


# ---------------------------------------------------------------------------
# Budget lines: period_index bounded, money bounded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 13])
def test_budget_line_rejects_out_of_range_period_index(bad: int) -> None:
    with pytest.raises(ValidationError):
        BudgetLineInput(category_id=_UUID, period_index=bad, planned_amount=Decimal("1"))


def test_budget_line_accepts_boundary_period_indexes() -> None:
    for idx in (1, 12):
        line = BudgetLineInput(category_id=_UUID, period_index=idx, planned_amount=Decimal("1"))
        assert line.period_index == idx


def test_budget_line_rejects_amount_beyond_numeric_precision() -> None:
    with pytest.raises(ValidationError):
        BudgetLineInput(
            category_id=_UUID, period_index=1, planned_amount=Decimal("1234567890123.00")
        )


def test_budget_lines_put_wraps_a_list_of_lines() -> None:
    payload = BudgetLinesPut(
        lines=[
            BudgetLineInput(category_id=_UUID, period_index=1, planned_amount=Decimal("100.00")),
            BudgetLineInput(category_id=_UUID, period_index=2, planned_amount=Decimal("200.00")),
        ]
    )
    assert len(payload.lines) == 2


# ---------------------------------------------------------------------------
# Settings: fiscal_year_start_month bounded on both ends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 13])
def test_settings_update_rejects_out_of_range_start_month(bad: int) -> None:
    with pytest.raises(ValidationError):
        SettingsUpdate(fiscal_year_start_month=bad)


def test_settings_update_accepts_boundary_start_months() -> None:
    assert SettingsUpdate(fiscal_year_start_month=1).fiscal_year_start_month == 1
    assert SettingsUpdate(fiscal_year_start_month=12).fiscal_year_start_month == 12


def test_settings_update_rejects_bad_currency_length() -> None:
    with pytest.raises(ValidationError):
        SettingsUpdate(base_currency="US Dollars")


def test_settings_update_acknowledge_mixed_basis_defaults_false() -> None:
    assert SettingsUpdate().acknowledge_mixed_basis is False


# ---------------------------------------------------------------------------
# Materialize / confirm / report rows
# ---------------------------------------------------------------------------


def test_materialize_request_accepts_a_date_window() -> None:
    req = MaterializeRequest(client_id=_UUID, from_date=date(2026, 1, 1), to_date=date(2026, 6, 30))
    assert req.from_date == date(2026, 1, 1)


def test_expense_confirm_amount_is_optional() -> None:
    assert ExpenseConfirm().amount is None
    assert ExpenseConfirm(amount=Decimal("42.00")).amount == Decimal("42.00")


def test_expense_confirm_rejects_amount_beyond_numeric_precision() -> None:
    with pytest.raises(ValidationError):
        ExpenseConfirm(amount=Decimal("1234567890123.00"))


def test_variance_row_returns_none_for_zero_denominator_pct() -> None:
    row = VarianceRow(
        category_id=_UUID, cost_center_id=None, period_index=1,
        planned=Decimal("0"), actual=Decimal("0"), committed=Decimal("0"),
        projected=Decimal("0"), variance=Decimal("0"), variance_pct=None,
    )
    assert row.variance_pct is None


def test_position_out_carries_the_unconvertible_disclosure() -> None:
    pos = PositionOut(
        ytd_actual=Decimal("100.00"), pct_of_plan=None, avg_monthly_burn=Decimal("10.00"),
        elapsed_periods=3, projected_year_end=Decimal("120.00"),
        unconvertible_count=1, unconvertible_by_currency={"COP": "500000.00"},
    )
    assert pos.unconvertible_count == 1
    dumped = pos.model_dump(mode="json")
    assert dumped["ytd_actual"] == "100.00"


def test_forecast_row_distinguishes_materialized_from_projected() -> None:
    row = ForecastRow(
        category_id=_UUID, period_key="2026-03", amount=Decimal("9.99"), currency="USD",
        amount_base=None, materialized=False,
    )
    assert row.materialized is False


def test_uuid_type_annotations_are_real_uuids() -> None:
    assert isinstance(_UUID, UUID)


# ---------------------------------------------------------------------------
# Client (MSP clients plan 2026-10-26 Task 8): every create names its client
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, fields",
    [
        (CategoryCreate, {"key": "hardware", "name": "Hardware"}),
        (CostCenterCreate, {"code": "ENG", "name": "Engineering"}),
        (BudgetCreate, {"fiscal_year": 2026, "name": "FY26", "granularity": "month"}),
        (ExpenseCreate, {"amount": Decimal("1"), "currency": "USD", "category_id": _UUID,
                         "expense_date": _DATE}),
        (MaterializeRequest, {"from_date": _DATE, "to_date": _DATE}),
    ],
)
def test_requires_client_id(model: type, fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="client_id"):
        model(**fields)
    assert model(client_id=_UUID, **fields).client_id == _UUID


def test_an_fx_rate_may_name_the_currency_it_converts_into() -> None:
    """Each client has its own base currency (Task 8): a rate names its
    `to_currency`, or leaves it to the internal client's."""
    implied = FxRateCreate(from_currency="EUR", rate=Decimal("1.1"), effective_date=_DATE)
    named = FxRateCreate(
        from_currency="EUR", to_currency="COP", rate=Decimal("4000"), effective_date=_DATE
    )
    assert (implied.to_currency, named.to_currency) == (None, "COP")
    with pytest.raises(ValidationError):
        FxRateCreate(from_currency="EUR", to_currency="PESOS", rate=Decimal("1"),
                     effective_date=_DATE)
