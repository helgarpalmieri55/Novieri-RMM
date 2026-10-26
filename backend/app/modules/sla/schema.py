"""Request/response models for the SLA admin API (docs/modules/sla/spec.md
§9, §9.1).

THE TIMEZONE IS ON EVERY VIEW OF A CALENDAR (spec §9.1): "a working window is
meaningless without it and a reader who assumes their own timezone will
misread every row". `CalendarOut` carries it, and so does the detail model the
windows hang off, so no response can present windows without the timezone they
are expressed in.

WILDCARDS ARE `None`, AND THE FRONTEND RENDERS THEM AS "any" (spec §9.1) --
`PolicyOut.queue_id`/`priority` are nullable because that is what §2.4's
resolution levels are, not because they are optional to supply. On
`PolicyUpdate` an explicit `null` is therefore a REAL edit ("apply to any
queue"), which is why `supplied()` is `exclude_unset` and never
`exclude_none` -- see that model's docstring.

NOTHING HERE IS A SECOND IMPLEMENTATION OF THE CLOCK. `TicketSlaOut` carries
the STORED instants and the state strings `views.target_state` derived from
them (spec §9.1: "Breach state is never computed in the browser"). It exposes
no target-minutes/elapsed pair a browser could subtract into a rival verdict.
"""

from datetime import date, datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.sla.settings import MAX_AUTO_CLOSE_AFTER_DAYS
from app.modules.sla.views import CalendarHealth, TargetState

_NAME_MAX_LENGTH = 100
_TZ_MAX_LENGTH = 64

Priority = Literal["low", "normal", "high", "urgent"]


class _StrictIn(BaseModel):
    """Every REQUEST model forbids unknown keys (`backup.schema`'s convention).

    This is not tidiness: `policies.update_policy` deliberately REFUSES an
    unknown field rather than ignoring it (Task 4 review F6 -- silently
    dropping one returned 200 for a typo'd body having changed nothing, and
    audited a no-op edit), and a Pydantic model that ignores extras would
    strip the typo before the service could ever see it, reinstating exactly
    that defect for every caller arriving over HTTP. Forbidding here is what
    carries the service's decision across the wire boundary, as a 422 naming
    the offending key.

    Response models are deliberately NOT strict: they are constructed
    in-process by this module's own builders, never parsed from input.
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Calendars
# --------------------------------------------------------------------------- #


class CalendarCreate(_StrictIn):
    name: str = Field(min_length=1, max_length=_NAME_MAX_LENGTH)
    timezone: str = Field(min_length=1, max_length=_TZ_MAX_LENGTH)
    active: bool = True


class CalendarUpdate(_StrictIn):
    """Every field optional: `calendars.update_calendar` applies only the
    SUPPLIED ones, and rejects a supplied `None` for a NOT NULL column."""

    name: str | None = Field(default=None, min_length=1, max_length=_NAME_MAX_LENGTH)
    timezone: str | None = Field(default=None, min_length=1, max_length=_TZ_MAX_LENGTH)
    active: bool | None = None

    def supplied(self) -> dict[str, Any]:
        """Only the keys the client actually sent. `exclude_unset` rather than
        `exclude_none`, so "not mentioned" and "explicitly null" stay
        distinguishable -- the service refuses the latter for a NOT NULL
        column with a typed 400 instead of silently ignoring it."""
        return self.model_dump(exclude_unset=True)


class WindowIn(_StrictIn):
    """One working window. `end_time == 00:00` is the midnight-end-of-day
    sentinel (spec §2.2, migration 0035), which is why there is no
    `end_time > start_time` validator here -- `calendars._validate_windows`
    owns that rule, including the sentinel and the overlap check across the
    whole set, and duplicating half of it here would be a second place for it
    to be wrong."""

    weekday: int = Field(ge=0, le=6, description="Monday=0 .. Sunday=6")
    start_time: time
    end_time: time


class WindowsReplace(_StrictIn):
    """The FULL week, replaced as a set (spec §9): a weekday's windows only
    make sense together, and a partial edit can leave a calendar with
    overlapping or inverted windows. An empty list is accepted and means "no
    working time at all" -- it is refused nowhere because it is how a
    calendar under construction legitimately looks; the resulting
    `no_working_windows` health flag (`views.calendar_health`) is what makes
    that state visible rather than silent."""

    windows: list[WindowIn] = Field(default_factory=list, max_length=7 * 24)


class WindowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    weekday: int
    start_time: time
    end_time: time


class HolidayCreate(_StrictIn):
    holiday_date: date
    name: str = Field(min_length=1, max_length=_NAME_MAX_LENGTH)


class HolidayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    calendar_id: UUID
    holiday_date: date
    name: str


class CalendarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    timezone: str
    active: bool
    # Why a broken calendar is no longer invisible -- see `views.py`'s module
    # docstring. `ok` for a healthy one, so a green list is a real assertion
    # rather than an absence.
    health: CalendarHealth
    created_at: datetime


class CalendarDetail(CalendarOut):
    """The by-id view, with the week attached.

    Spec §9's table gives windows a `PUT` and no `GET`. Without them here they
    would be WRITE-ONLY -- §9.1's weekly-grid editor could never load the grid
    it edits, and an admin could not read back what they just replaced. They
    hang off the calendar rather than getting a route of their own because
    that is exactly what §9's table says the surface is.
    """

    windows: list[WindowOut]


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #


class PolicyCreate(_StrictIn):
    # The client whose tickets this policy is for (MSP clients plan
    # 2026-10-26, D2: a client with active policies of its own resolves among
    # those alone). Absent, the policy is global -- every client's -- and only
    # a technician of every client may create one.
    client_id: UUID | None = None
    queue_id: UUID | None = None
    priority: Priority | None = None
    calendar_id: UUID
    response_target_minutes: int = Field(gt=0)
    resolution_target_minutes: int = Field(gt=0)
    escalate_priority: bool = False
    escalate_to_queue_id: UUID | None = None
    active: bool = True


class PolicyUpdate(_StrictIn):
    """Every field optional, and an explicit `null` on a wildcard-capable
    field is a REAL edit.

    `queue_id`, `priority` and `escalate_to_queue_id` are nullable columns
    used as wildcards (spec §2.4), so `{"queue_id": null}` means "make this
    policy apply to any queue" -- it is not the absence of an edit.
    `policies.update_policy` keys on `field in fields` precisely because it
    must tell those two apart, so `supplied()` below is `exclude_unset`, NEVER
    `exclude_none`: the latter would silently discard every wildcarding edit
    this API can express, which is a whole resolution level (§3's levels 2-4)
    becoming unreachable with a 200 and no complaint.

    The non-nullable fields are typed `| None` anyway, deliberately: a client
    that sends `{"calendar_id": null}` must reach
    `update_policy`'s `_reject_null_for_non_nullable` and get its typed 400
    naming the field, not a Pydantic 422 that says "input should be a valid
    UUID" about a column the caller cannot know is NOT NULL.
    """

    queue_id: UUID | None = None
    priority: Priority | None = None
    calendar_id: UUID | None = None
    response_target_minutes: int | None = Field(default=None, gt=0)
    resolution_target_minutes: int | None = Field(default=None, gt=0)
    escalate_priority: bool | None = None
    escalate_to_queue_id: UUID | None = None
    active: bool | None = None

    def supplied(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    # `None`: a global policy, every client's (D2).
    client_id: UUID | None
    # `None` means "any" (spec §9.1) -- the frontend renders it as the word,
    # never as a blank cell, so an unset cell stays distinguishable from one
    # that deliberately matches everything.
    queue_id: UUID | None
    priority: Priority | None
    calendar_id: UUID
    response_target_minutes: int
    resolution_target_minutes: int
    escalate_priority: bool
    escalate_to_queue_id: UUID | None
    active: bool
    created_at: datetime


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


class SettingsUpdate(_StrictIn):
    auto_close_after_days: int = Field(ge=1, le=MAX_AUTO_CLOSE_AFTER_DAYS)


class SettingsOut(BaseModel):
    auto_close_after_days: int
    # `False` when the org has never configured one and is running on
    # `sweep.DEFAULT_AUTO_CLOSE_AFTER_DAYS`. Without it the screen cannot tell
    # "we chose 7" from "nobody has ever looked at this", and the two lead to
    # different conversations.
    configured: bool
    # The consequence half of the misconfiguration surface (see `views.py`).
    # It rides on the settings response rather than a route of its own because
    # spec §9's table is the authoritative route list and this is the SLA
    # administration screen's own payload.
    tickets_without_sla: int
    # `views.count_stale_ticket_sla`: running clocks whose stored deadline is
    # not what the configuration in force would produce -- the residue of a
    # recompute that failed on them and reported success anyway. Beside
    # `tickets_without_sla` because they are complementary blindnesses: that
    # one counts promises that are MISSING, this one counts promises that are
    # PRESENT and wrong, which no count of rows-by-presence can see.
    stale_ticket_sla: int


# --------------------------------------------------------------------------- #
# The per-ticket view (gated on `ticket.read`, spec §9)
# --------------------------------------------------------------------------- #


class TicketSlaOut(BaseModel):
    ticket_id: UUID
    # False -> every field below is None and both states are "none". Spec §3:
    # an org with no policy configured (or a broken calendar, which degrades
    # to the same thing) must SEE "no SLA", never a fabricated target.
    has_sla: bool
    response_state: TargetState
    resolution_state: TargetState
    policy_id: UUID | None = None
    calendar_id: UUID | None = None
    # Display names for the two ids above. A policy has no name of its own,
    # so `policy_name` describes what it matches ("Urgent tickets in ...").
    policy_name: str | None = None
    calendar_name: str | None = None
    response_due_at: datetime | None = None
    resolution_due_at: datetime | None = None
    response_met_at: datetime | None = None
    resolution_met_at: datetime | None = None
    response_breached_at: datetime | None = None
    resolution_breached_at: datetime | None = None
    paused_at: datetime | None = None
    paused_minutes_total: int | None = None
    voided_at: datetime | None = None
    escalated_at: datetime | None = None
    # §0.3's attribution requirement: "breached because we were slow" and
    # "breached retroactively because we re-triaged at hour five" must be
    # distinguishable, and this is the field that distinguishes them.
    policy_source: str | None = None
