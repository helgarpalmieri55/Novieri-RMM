"""People API shapes. No response carries `org_id` (invariant 5)."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.i18n import tr

PersonStatus = Literal["starting", "active", "leaving", "left"]


def _email_shape(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    local, at, domain = value.partition("@")
    if not at or not local or "." not in domain or any(c.isspace() for c in value):
        raise ValueError(tr("that is not an email address"))
    return value


class PersonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The client the person works for (plan 2026-10-26 Task 5). Required:
    #: a person belongs to exactly one client, and nothing else in the body
    #: says which. Never changes afterwards (`PersonUpdate` has no client).
    client_id: UUID
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    department: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    start_date: date | None = None

    _email = field_validator("email")(_email_shape)


class PersonUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    department: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    start_date: date | None = None
    end_date: date | None = None

    _email = field_validator("email")(_email_shape)


class AccountLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID | None


class LoginCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=256)


class DevicePersonSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: UUID | None


class SeatLink(BaseModel):
    """`person_id` null means: use or add the person the seat's address names."""

    model_config = ConfigDict(extra="forbid")

    person_id: UUID | None = None


class ChecklistStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["starter", "leaver"]


class StepUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["pending", "done", "skipped"]
    note: str = Field(default="", max_length=500)


class PersonOut(BaseModel):
    """One person who works here, whether or not they can sign in."""

    id: UUID
    client_id: UUID
    display_name: str
    email: str | None
    user_id: UUID | None
    status: PersonStatus
    department: str | None
    job_title: str | None
    start_date: date | None
    end_date: date | None
    source: str
    created_at: datetime


class PersonListRow(PersonOut):
    """A person, with the number of computers linked to them."""

    device_count: int


class LoginOut(BaseModel):
    id: UUID
    login: str
    created_at: datetime


class AccountOut(BaseModel):
    user_id: UUID
    #: `staff` signs in to the console; `contact` is a client's person, whose
    #: sign-in is switched off through `PATCH /contacts/{id}`, not `/users`.
    kind: Literal["staff", "contact"]
    email: str
    status: str
    last_login_at: datetime | None
    console: bool
    roles: list[str] | None


class PersonDeviceOut(BaseModel):
    device_id: UUID
    hostname: str | None
    os_family: str | None
    lifecycle: str
    device_group_id: UUID | None
    group_name: str | None
    last_seen_at: datetime | None
    source: str
    observed_login: str | None
    observed_at: datetime | None


class PersonLicenceOut(BaseModel):
    allocation_id: UUID
    license_id: UUID
    product_name: str
    through_device_id: UUID | None
    allocated_at: datetime


class PersonSeatOut(BaseModel):
    seat_id: UUID
    subscription_id: UUID
    service_name: str
    plan: str
    assigned_at: datetime


class PersonTicketOut(BaseModel):
    ticket_id: UUID
    number: str
    subject: str
    status: str
    priority: str
    created_at: datetime


class StepOut(BaseModel):
    key: str
    position: int
    title: str
    help: str
    link: str
    destructive: bool
    checked: bool | None
    evidence: str
    marked: str
    state: Literal["pending", "done", "skipped"]
    note: str
    updated_at: datetime | None


class ChecklistOut(BaseModel):
    id: UUID
    kind: Literal["starter", "leaver"]
    status: Literal["open", "completed", "cancelled"]
    started_at: datetime
    closed_at: datetime | None
    steps: list[StepOut]
    can_complete: bool


class PastChecklistOut(BaseModel):
    id: UUID
    kind: str
    status: str
    closed_at: datetime | None


class PersonDetailOut(BaseModel):
    """The Person page. A section is null when the viewer cannot read it."""

    person: PersonOut
    logins: list[LoginOut]
    account: AccountOut | None
    devices: list[PersonDeviceOut]
    licences: list[PersonLicenceOut] | None
    seats: list[PersonSeatOut] | None
    tickets: list[PersonTicketOut] | None
    checklist: ChecklistOut | None
    past_checklists: list[PastChecklistOut]


class DevicePersonOut(BaseModel):
    """Who uses a computer, how that was decided, and who last signed in."""

    device_id: UUID
    person: PersonOut | None
    source: Literal["inventory", "operator"] | None
    observed_login: str | None
    observed_at: datetime | None


class UnmatchedSeat(BaseModel):
    seat_id: UUID
    subscription_id: UUID
    service_name: str
    assignee_name: str
    assignee_email: str


class UnmatchedDevice(BaseModel):
    device_id: UUID
    hostname: str | None
    owner: str | None
    observed_login: str | None


class UnmatchedAllocation(BaseModel):
    allocation_id: UUID
    license_id: UUID
    product_name: str
    user_id: UUID


class UnmatchedOut(BaseModel):
    """Records that name someone but are not linked to a person."""

    seats: list[UnmatchedSeat]
    devices: list[UnmatchedDevice]
    allocations: list[UnmatchedAllocation]
