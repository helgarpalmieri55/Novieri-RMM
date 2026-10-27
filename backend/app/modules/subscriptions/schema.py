"""API-layer request/response models for the Subscriptions module's HTTP
routes (docs/modules/subscriptions/spec.md).

These are plain API-layer request/response shapes, not wire contracts —
this module dispatches no agent jobs (control-plane only, no worker; spec
§10), so there is no signed-job params/result pair here and no
`extra="forbid"` base is needed. Per security invariant #3, no model in
this file carries a secret value: `payment_ref` is a low-sensitivity
reference (invoice/PO/last-4-style), not vault-grade (spec §8). Per
security invariant #5, no response model leaks `org_id` — `SubscriptionOut`
omits it, mirroring `licenses/schema.py`'s `LicenseOut` convention.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Subscription billing cadence and lifecycle status, shared by
# SubscriptionCreate/SubscriptionUpdate so the accepted domain can never
# drift between the two (spec §2). Match the DB `varchar(10)` columns and
# the migration-seeded values.
BillingCycle = Literal["monthly", "quarterly", "annual"]
SubStatus = Literal["active", "trial", "cancelled"]

# Every field below that maps to a BOUNDED database column is bounded here
# too. There is no global exception handler in `backend/app/`, so an
# out-of-range value that reaches Postgres comes back as an unhandled
# `22001` (string_data_right_truncation) or `22003` (numeric_value_out_of_
# range) and surfaces as a 500. Constraining at the schema boundary turns
# each of those into a clean 422 (spec §8, the Phase-11 lesson).
#
# `models.Subscription` column bounds mirrored here:
#   service_name / plan / category / payment_ref -> String(255)
#   currency                                     -> String(3)
#   cost_per_cycle                               -> Numeric(14, 2)
# `seats`/`notice_days` are unbounded `Integer`s in the DB but are counts:
# a negative `seats` is nonsense that makes `available_seats` and the
# idle-seat report's `idle` go negative, so `ge=0` is enforced here.
_MAX_TEXT = 255
_COST_MAX_DIGITS = 14
_COST_DECIMAL_PLACES = 2
# `seats` and `notice_days` are Postgres `Integer` (int4). `ge=0` alone still
# leaves an out-of-range value reaching the driver, where asyncpg raises
# `DataError: value out of int32 range` -- unhandled, so a 500. Bound both
# ends at the schema boundary so it is a clean 422 instead.
_MAX_COUNT = 2_147_483_647

# Shared field definitions so `SubscriptionCreate` and `SubscriptionUpdate`
# can never drift apart on their bounds (the drift that let `currency` be
# guarded while the other four strings were not).
_Currency = Field(default=None, min_length=3, max_length=3)
_OptionalText = Field(default=None, max_length=_MAX_TEXT)
_OptionalCost = Field(
    default=None, max_digits=_COST_MAX_DIGITS, decimal_places=_COST_DECIMAL_PLACES
)
_OptionalCount = Field(default=None, ge=0, le=_MAX_COUNT)


class SubscriptionCreate(BaseModel):
    # The client the service is paid for (plan 2026-10-26 Task 6). One
    # client's: its seats are held by that client's people, and it never
    # changes client -- `SubscriptionUpdate` has no such field.
    client_id: UUID
    vendor_id: UUID | None = None
    service_name: str = Field(max_length=_MAX_TEXT)
    plan: str = Field(default="", max_length=_MAX_TEXT)
    category: str = Field(default="", max_length=_MAX_TEXT)
    billing_cycle: BillingCycle
    cost_per_cycle: Decimal = Field(
        max_digits=_COST_MAX_DIGITS, decimal_places=_COST_DECIMAL_PLACES
    )
    currency: str = Field(min_length=3, max_length=3)
    seats: int = Field(default=0, ge=0, le=_MAX_COUNT)
    owner_user_id: UUID | None = None
    started_at: date | None = None
    renews_at: date | None = None
    auto_renew: bool = False
    notice_days: int = Field(default=0, ge=0, le=_MAX_COUNT)
    payment_ref: str | None = _OptionalText
    license_ref: UUID | None = None
    status: SubStatus = "active"


class SubscriptionUpdate(BaseModel):
    vendor_id: UUID | None = None
    service_name: str | None = _OptionalText
    plan: str | None = _OptionalText
    category: str | None = _OptionalText
    billing_cycle: BillingCycle | None = None
    cost_per_cycle: Decimal | None = _OptionalCost
    currency: str | None = _Currency
    seats: int | None = _OptionalCount
    owner_user_id: UUID | None = None
    started_at: date | None = None
    renews_at: date | None = None
    auto_renew: bool | None = None
    notice_days: int | None = _OptionalCount
    payment_ref: str | None = _OptionalText
    license_ref: UUID | None = None
    status: SubStatus | None = None


class SubscriptionOut(BaseModel):
    # No `org_id` field here — security invariant #5: response models never
    # leak the tenant id, mirroring `licenses/schema.py`'s `LicenseOut`.
    id: UUID
    client_id: UUID
    vendor_id: UUID | None
    service_name: str
    plan: str
    category: str
    billing_cycle: str
    cost_per_cycle: Decimal
    currency: str
    seats: int
    owner_user_id: UUID | None
    started_at: date | None
    renews_at: date | None
    auto_renew: bool
    notice_days: int
    payment_ref: str | None
    license_ref: UUID | None
    status: str
    created_at: datetime


class SeatAssignCreate(BaseModel):
    assignee_name: str
    assignee_email: str
    # Optional: without it, the seat links to whoever holds this address.
    person_id: UUID | None = None


class SeatAssignmentOut(BaseModel):
    id: UUID
    subscription_id: UUID
    assignee_name: str
    assignee_email: str
    person_id: UUID | None = None
    assigned_at: datetime
    released_at: datetime | None


class EventOut(BaseModel):
    id: UUID
    subscription_id: UUID
    type: str
    details: dict[str, Any]
    created_at: datetime


class SpendRow(BaseModel):
    """Spend-summary report row (spec §7). One flexible shape covers both
    the top-level per-currency totals and the vendor/category/owner
    roll-ups: `group_key`/`group_type`/`group_id` are `None` on the
    top-level totals row and populated on a roll-up row (e.g.
    `group_type="vendor"`, `group_key=<vendor name>`,
    `group_id=<vendor_id>`).

    `group_key` is the human-readable **label** for the row -- the
    vendor's name, the category value, or the owner's email. `group_id` is
    the underlying id to link back to that entity (the vendor's id or the
    owner user's id); it is always `None` for category rows, which have no
    id, and `None` on the top-level per-currency totals row.

    `group_key` and `group_id` are always `None` **together**, in both of
    the cases where a row has no resolvable entity: the "unset" bucket (no
    vendor / no owner on the subscription), and a reference the org-scoped
    lookup could not resolve. A vendor or owner id is never emitted
    without its label -- suppressing the name while still returning the
    raw id would leak a cross-tenant identifier.

    `client_id` is the client every subscription in the row belongs to (plan
    2026-10-26 Task 6): each roll-up is per client, and nothing is summed
    across two clients any more than across two currencies."""

    client_id: UUID
    currency: str
    monthly_total: Decimal
    annual_total: Decimal
    group_key: str | None = None
    group_type: str | None = None
    group_id: UUID | None = None


class RenewalRow(BaseModel):
    subscription_id: UUID
    service_name: str
    renews_at: date | None
    notice_deadline: date | None
    days_to_notice: int | None
    days_to_renewal: int | None
    action_required: bool


class IdleSeatRow(BaseModel):
    subscription_id: UUID
    service_name: str
    seats: int
    assigned: int
    idle: int
    currency: str
    idle_monthly_cost: Decimal
