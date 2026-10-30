"""API-layer request/response models for the Budget module's HTTP routes
(docs/modules/budget/spec.md).

These are plain API-layer request/response shapes, not wire contracts —
this module dispatches no agent jobs (control-plane only, no worker; spec
§10), so there is no signed-job params/result pair here and no
`extra="forbid"` base is needed. Per security invariant #3, no model here
carries a secret value: `invoice_ref` is a reference string (invoice/PO
number), not a credential (spec §8). Per security invariant #5, no
response model leaks `org_id`, mirroring `subscriptions/schema.py`'s
`SubscriptionOut` and `licenses/schema.py`'s `LicenseOut` convention.

Money handling (spec §8): `Decimal` throughout, `Numeric(14,2)` at rest.
Pydantic v2 serializes `Decimal` fields to JSON **strings** by default
(`model_dump(mode="json")` / `model_dump_json()` — verified against
`SubscriptionOut`, which relies on this same default rather than a custom
`field_serializer`). So a plain `Decimal` type annotation on a response
model is already the correct wire shape; this module does not invent a
second money convention.

Schema-boundary bounds (spec §8, the Phase-11/12 lesson): every string
bounded by `max_length`, every money field `max_digits=14, decimal_places=2`,
`rate` `max_digits=18, decimal_places=8, gt=0`, and every integer bounded on
**both** ends. There is no global exception handler in `backend/app/`, so an
out-of-range value that reaches Postgres surfaces as an unhandled
`22001`/`22003`/`DataError: value out of int32 range` — a 500. Shared `Field`
constants below keep Create and Update models from drifting apart on their
bounds, the drift Phase 12 hit where `currency` was guarded and four sibling
strings were not.

Client (MSP clients plan 2026-10-26 Task 8): every create names the client
its row belongs to (`client_id`, required), and every C output carries it.
Settings are one client's, named in the query (`?client_id=`), not the body.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# --- Domain literals, shared by Create/Update so the accepted domain can
# never drift between the two (spec §2). Match the DB `varchar` columns and
# the migration-seeded values.
Granularity = Literal["month", "quarter"]
BudgetStatus = Literal["draft", "active", "closed"]
# Narrower than BudgetStatus: the PATCH surface (BudgetUpdate.status) only
# ever covers the rename / draft<->active path. "closed" is reachable solely
# through the dedicated POST .../close endpoint (spec §9), which also runs
# the line-freeze and audit event that a bare status flip would bypass.
BudgetUpdateStatus = Literal["draft", "active"]
ExpenseStatus = Literal["expected", "confirmed", "void"]
ExpenseSource = Literal["manual", "subscription", "license"]
LinkType = Literal["subscription", "license", "device", "change"]

# --- String / money bounds, mirrored from migrations/versions/0020_budget.py.
_MAX_TEXT = 255
_MAX_DESC = 1000
_MAX_CODE = 32
_MAX_KEY = 64
_MAX_EXTERNAL_REF = 64
_MAX_INVOICE_REF = 128
_MONEY_DIGITS = 14
_MONEY_PLACES = 2
_RATE_DIGITS = 18
_RATE_PLACES = 8

# Postgres `Integer` (int4) columns. `ge=0` alone still lets an out-of-range
# value reach asyncpg, which raises `DataError: value out of int32 range` --
# unhandled, so a 500. Bound both ends at the schema boundary (spec §8).
_MAX_COUNT = 2_147_483_647

_Money = Field(max_digits=_MONEY_DIGITS, decimal_places=_MONEY_PLACES)
_OptionalMoney = Field(default=None, max_digits=_MONEY_DIGITS, decimal_places=_MONEY_PLACES)
_Currency = Field(min_length=3, max_length=3)
_OptionalCurrency = Field(default=None, min_length=3, max_length=3)
_OptionalText = Field(default=None, max_length=_MAX_TEXT)
_OptionalDesc = Field(default=None, max_length=_MAX_DESC)
_OptionalInvoiceRef = Field(default=None, max_length=_MAX_INVOICE_REF)
_OptionalExternalRef = Field(default=None, max_length=_MAX_EXTERNAL_REF)
_Rate = Field(max_digits=_RATE_DIGITS, decimal_places=_RATE_PLACES, gt=0)
_OptionalRate = Field(default=None, max_digits=_RATE_DIGITS, decimal_places=_RATE_PLACES, gt=0)
# fiscal_year: spec §8 pins this exact range.
_FiscalYear = Field(ge=2000, le=2100)
_OptionalFiscalYear = Field(default=None, ge=2000, le=2100)
# fiscal_year_start_month: a calendar month, 1-12 (spec §2).
_StartMonth = Field(ge=1, le=12)
_OptionalStartMonth = Field(default=None, ge=1, le=12)
# period_index: 1-12 for monthly granularity, 1-4 for quarterly (spec §2).
# The granularity-specific range is a business rule enforced by
# `periods.py`/`budgets.py` (out of scope here); the schema boundary still
# bounds it to the widest legal range so an int32-range value never reaches
# Postgres.
_PeriodIndex = Field(ge=1, le=12)
_OptionalCount = Field(default=None, ge=0, le=_MAX_COUNT)


class SettingsUpdate(BaseModel):
    base_currency: str | None = _OptionalCurrency
    fiscal_year_start_month: int | None = _OptionalStartMonth
    # spec §3: once any non-void expense carries an `amount_base`, changing
    # the base currency is rejected unless this is explicitly true.
    acknowledge_mixed_basis: bool = False


class SettingsOut(BaseModel):
    client_id: UUID
    base_currency: str
    fiscal_year_start_month: int
    updated_by: UUID | None
    # `None` until somebody sets the client's settings: until then they are
    # the documented defaults, and no row exists (`fx.get_settings`).
    updated_at: datetime | None


class CategoryCreate(BaseModel):
    client_id: UUID
    key: str = Field(max_length=_MAX_KEY)
    name: str = Field(max_length=_MAX_TEXT)
    sort_order: int = Field(default=0, ge=0, le=_MAX_COUNT)


class CategoryUpdate(BaseModel):
    # `key`/`system_key` are deliberately not updatable here: `system_key` is
    # the stable identifier the auto-feeds resolve against (spec §2/§6.1) and
    # is never operator-set; renaming is done via `name` only.
    name: str | None = _OptionalText
    active: bool | None = None
    sort_order: int | None = _OptionalCount


class CategoryOut(BaseModel):
    id: UUID
    client_id: UUID
    key: str
    name: str
    system_key: str | None
    active: bool
    sort_order: int
    created_at: datetime


class CostCenterCreate(BaseModel):
    client_id: UUID
    code: str = Field(max_length=_MAX_CODE)
    name: str = Field(max_length=_MAX_TEXT)
    external_ref: str | None = _OptionalExternalRef


class CostCenterUpdate(BaseModel):
    name: str | None = _OptionalText
    external_ref: str | None = _OptionalExternalRef
    active: bool | None = None


class CostCenterOut(BaseModel):
    id: UUID
    client_id: UUID
    code: str
    name: str
    external_ref: str | None
    active: bool
    created_at: datetime


class FxRateCreate(BaseModel):
    from_currency: str = _Currency
    # Each client has its own base currency, and one rate table serves them
    # all, so a rate names the currency it converts into. Omitted: the
    # internal client's base currency, the organisation's own (`rates.py`).
    to_currency: str | None = _OptionalCurrency
    rate: Decimal = _Rate
    effective_date: date


class FxRateUpdate(BaseModel):
    # `from_currency`/`to_currency`/`effective_date` identify the rate row
    # (spec §2 unique constraint); only the rate value itself is patchable.
    rate: Decimal | None = _OptionalRate


class FxRateOut(BaseModel):
    id: UUID
    from_currency: str
    to_currency: str
    rate: Decimal
    effective_date: date
    created_at: datetime


class BudgetCreate(BaseModel):
    client_id: UUID
    fiscal_year: int = _FiscalYear
    name: str = Field(max_length=_MAX_TEXT)
    granularity: Granularity


class BudgetUpdate(BaseModel):
    # `fiscal_year`/`granularity` are immutable after creation -- budget
    # lines are validated against granularity, so changing it out from under
    # existing lines would silently invalidate them. Status transitions to
    # "closed" go through the dedicated `POST .../close` endpoint (spec §9);
    # this only covers the rename / draft<->active path -- `status` is typed
    # `BudgetUpdateStatus`, not `BudgetStatus`, so "closed" is rejected here
    # at the type level, not just by convention.
    name: str | None = _OptionalText
    status: BudgetUpdateStatus | None = None


class BudgetOut(BaseModel):
    id: UUID
    client_id: UUID
    fiscal_year: int
    name: str
    granularity: str
    status: str
    created_at: datetime


class BudgetLineInput(BaseModel):
    category_id: UUID
    cost_center_id: UUID | None = None
    period_index: int = _PeriodIndex
    planned_amount: Decimal = _Money


class BudgetLinesPut(BaseModel):
    lines: list[BudgetLineInput]


class BudgetLineOut(BaseModel):
    id: UUID
    budget_id: UUID
    category_id: UUID
    cost_center_id: UUID | None
    period_index: int
    planned_amount: Decimal
    created_at: datetime


class ExpenseCreate(BaseModel):
    client_id: UUID
    expense_date: date
    amount: Decimal = _Money
    currency: str = _Currency
    category_id: UUID
    cost_center_id: UUID | None = None
    vendor_id: UUID | None = None
    description: str = Field(default="", max_length=_MAX_DESC)
    invoice_ref: str | None = _OptionalInvoiceRef
    link_type: LinkType | None = None
    link_id: UUID | None = None
    # Defaults to "expected", matching spec §5 verbatim: "a row is never
    # `confirmed` by omission." The confirmation queue
    # (`GET /budget/expenses?status=expected`, spec §0.3) only works if rows
    # actually arrive in it -- a default of "confirmed" would mean an unbound
    # UI form field, a CSV importer, or an ops-runbook curl books costs
    # straight into spec §7's `actual` column with nobody having performed
    # the assertion step. A caller recording a real cost says
    # `status="confirmed"` explicitly -- one field, and `create_expense`'s
    # H4 stamping (`confirmed_by`/`confirmed_at`) already records who they
    # were, so that explicit choice is never confirmed WITHOUT a user
    # either. This Pydantic default and the DB-column-level default on
    # `Expense.status` (models.py) now agree; the DB default remains the
    # lower-level safety floor for any insert that bypasses this Pydantic
    # model entirely (e.g. a future direct ORM construction).
    status: Literal["expected", "confirmed"] = "expected"


class ExpenseUpdate(BaseModel):
    """Every field is `X | None = None` so the route layer can apply this
    with `exclude_unset=True` (Task 9) -- an omitted field is left
    untouched, while an explicitly-`null` field is a caller's request to
    clear it. Non-nullable columns (`expense_date`, `amount`, `currency`,
    `category_id`) reject an explicit `null` in a pass the service layer
    runs before any mutation (Task 9), not here -- this shape only needs to
    support that distinction, per the brief."""

    expense_date: date | None = None
    amount: Decimal | None = _OptionalMoney
    currency: str | None = _OptionalCurrency
    category_id: UUID | None = None
    cost_center_id: UUID | None = None
    vendor_id: UUID | None = None
    description: str | None = _OptionalDesc
    invoice_ref: str | None = _OptionalInvoiceRef
    link_type: LinkType | None = None
    link_id: UUID | None = None


class ExpenseConfirm(BaseModel):
    # spec §0.3/§5: the confirmed amount may differ from the projected one;
    # omitting it confirms at the currently-recorded amount.
    amount: Decimal | None = _OptionalMoney


class MaterializeRequest(BaseModel):
    # One client's subscriptions and licenses, filed under that client.
    client_id: UUID
    from_date: date
    to_date: date


class ExpenseOut(BaseModel):
    # No `org_id` field here -- security invariant #5.
    id: UUID
    client_id: UUID
    expense_date: date
    amount: Decimal
    currency: str
    fx_rate: Decimal | None
    amount_base: Decimal | None
    vendor_id: UUID | None
    category_id: UUID
    cost_center_id: UUID | None
    description: str
    source: str
    link_type: str | None
    link_id: UUID | None
    period_key: str | None
    invoice_ref: str | None
    # P5 of the post-review controller fix (.git/sdd/task-12-report.md): the
    # raw `invoice_object_key` (an `s3://{bucket}/{org_id}/...` URI) is never
    # exposed here -- it leaks the tenant UUID and the internal bucket name to
    # every `budget.read` caller, directly contradicting this module's own
    # "no org_id field here" invariant-#5 discipline. `has_attachment` and the
    # sanitized `invoice_filename` (migration 0021) are the only attachment
    # facts a caller needs; the bytes themselves are served exclusively by the
    # dedicated download route.
    has_attachment: bool
    invoice_filename: str | None
    status: str
    entered_by: UUID
    created_at: datetime
    confirmed_by: UUID | None
    confirmed_at: datetime | None
    voided_by: UUID | None
    voided_at: datetime | None


class VarianceRow(BaseModel):
    """One row of `GET /budget/{id}/variance` (spec §7): per category ×
    period × optional cost center. `planned`, `actual`, `committed`, and
    `projected` are four distinct columns, never collapsed into one --
    collapsing `committed` and `projected` is how a forecast double-counts.
    `variance_pct` is `None` on a zero-plan denominator, never `Infinity`.
    """

    category_id: UUID
    cost_center_id: UUID | None
    period_index: int
    planned: Decimal
    actual: Decimal
    committed: Decimal
    projected: Decimal
    variance: Decimal
    variance_pct: Decimal | None


class PositionOut(BaseModel):
    """`GET /budget/{id}/position` (spec §7): year-to-date burn and a
    straight-line projection to year end, computed from the fiscal calendar
    (`periods.py`), never from wall-clock month count."""

    ytd_actual: Decimal
    pct_of_plan: Decimal | None
    avg_monthly_burn: Decimal
    elapsed_periods: int
    projected_year_end: Decimal
    # spec §3: every aggregate that sums `amount_base` also discloses what it
    # could not convert -- a total is never quietly understated.
    unconvertible_count: int
    unconvertible_by_currency: dict[str, str]


class ForecastRow(BaseModel):
    """One row of `GET /budget/forecast` (spec §6.2): a pure read-time
    projection, computed entirely from upstream Subscriptions/Licenses data
    so it can never drift. `materialized` separates rows that already have a
    written `expected` expense from rows that are still pure projection --
    the two are never summed into one ambiguous figure."""

    category_id: UUID
    period_key: str
    amount: Decimal
    currency: str
    amount_base: Decimal | None
    materialized: bool
