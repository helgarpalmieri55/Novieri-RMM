"""API-layer request/response models for the Licenses/SAM module's HTTP
routes (docs/modules/licenses/spec.md).

These are plain API-layer request/response shapes, not wire contracts —
this module dispatches no agent jobs, so there is no signed-job params/
result pair here and no `extra="forbid"` base is needed. Per security
invariant #3, no model in this file carries a secret value except the
one deliberate, audited reveal path (spec §5): `License`/`LicenseOut`
never expose the license key or its sealed form — only a `has_key: bool`
— and `LicenseKeyOut` exists solely as the response shape for the
dedicated `GET /licenses/{id}/key` reveal endpoint.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# License lifecycle type, shared by LicenseCreate/LicenseUpdate so the accepted
# domain can never drift between the two (spec §2). Matches the DB `type
# varchar(16)` and the migration-seeded values.
LicenseType = Literal["perpetual", "volume", "oem", "cal", "per_device", "per_user"]

# ISO-4217 codes are exactly three characters; the DB column is `varchar(3)`.
# Constraining length at the schema boundary turns an over-long value into a
# clean 422 instead of a Postgres truncation error surfacing as a 500 (spec §9,
# the never-500 invariant).
_Currency = Field(default=None, min_length=3, max_length=3)


class MatchRule(BaseModel):
    type: Literal["exact", "prefix", "regex"]
    pattern: str
    publisher: str | None = None


class VendorCreate(BaseModel):
    name: str
    website: str | None = None
    contacts: list[dict[str, Any]] = []


class VendorUpdate(BaseModel):
    name: str | None = None
    website: str | None = None
    contacts: list[dict[str, Any]] | None = None


class VendorOut(BaseModel):
    id: UUID
    name: str
    website: str | None
    contacts: list[dict[str, Any]]
    created_at: datetime


class ProductCreate(BaseModel):
    name: str
    publisher: str = ""
    match_rules: list[MatchRule] = []


class ProductUpdate(BaseModel):
    name: str | None = None
    publisher: str | None = None
    match_rules: list[MatchRule] | None = None


class ProductOut(BaseModel):
    id: UUID
    name: str
    publisher: str
    match_rules: list[MatchRule]
    created_at: datetime


class LicenseCreate(BaseModel):
    # The client the license is bought for (plan 2026-10-26 Task 6). A
    # license is one client's: it holds that client's devices and people
    # only, and never changes client -- `LicenseUpdate` has no such field.
    client_id: UUID
    product_id: UUID
    vendor_id: UUID | None = None
    type: LicenseType
    entitlements: int
    key: str | None = None
    cost: Decimal | None = None
    currency: str | None = _Currency
    po_ref: str | None = None
    purchased_at: date | None = None
    maintenance_expires_at: date | None = None
    renews_at: date | None = None
    agreement_ref: str | None = None
    notes: str = ""


class LicenseUpdate(BaseModel):
    product_id: UUID | None = None
    vendor_id: UUID | None = None
    type: LicenseType | None = None
    entitlements: int | None = None
    key: str | None = None
    cost: Decimal | None = None
    currency: str | None = _Currency
    po_ref: str | None = None
    purchased_at: date | None = None
    maintenance_expires_at: date | None = None
    renews_at: date | None = None
    agreement_ref: str | None = None
    notes: str | None = None


class LicenseOut(BaseModel):
    # No `key` / `key_sealed` field here — spec §5: list/detail responses
    # carry `has_key` only. Reveal is a separate audited endpoint that
    # returns `LicenseKeyOut`.
    id: UUID
    client_id: UUID
    product_id: UUID
    vendor_id: UUID | None
    type: str
    entitlements: int
    has_key: bool
    cost: Decimal | None
    currency: str | None
    po_ref: str | None
    purchased_at: date | None
    maintenance_expires_at: date | None
    renews_at: date | None
    agreement_ref: str | None
    notes: str
    created_at: datetime


class LicenseKeyOut(BaseModel):
    """Response shape for the dedicated, audited key-reveal endpoint only."""

    key: str


class AllocationCreate(BaseModel):
    target_type: Literal["device", "user", "person"]
    target_id: UUID


class AllocationOut(BaseModel):
    id: UUID
    license_id: UUID
    target_type: str
    target_id: UUID
    person_id: UUID | None = None
    allocated_at: datetime
    released_at: datetime | None


class ReviewQueueRow(BaseModel):
    name: str
    publisher: str
    device_count: int


class ReviewMapRequest(BaseModel):
    name: str
    product_id: UUID


class ReviewIgnoreRequest(BaseModel):
    name: str
    # The client whose computers stop showing it (plan 2026-10-26 Task 6).
    # Without one the ignore is global -- every client's computers -- and,
    # like every global row, written only with a scope of every client.
    client_id: UUID | None = None


class ReviewIgnoreOut(BaseModel):
    id: UUID
    name: str
    client_id: UUID | None


class ComplianceRow(BaseModel):
    # One client's position on one product: never summed across clients.
    client_id: UUID
    product_id: UUID
    name: str
    entitled: int
    installed: int
    delta: int
    breach: bool


class RenewalRow(BaseModel):
    license_id: UUID
    client_id: UUID
    product_id: UUID
    trigger: Literal["renewal", "maintenance"]
    due_date: date
    due_in_days: int


class CostRow(BaseModel):
    client_id: UUID
    product_id: UUID
    currency: str
    total_cost: Decimal
