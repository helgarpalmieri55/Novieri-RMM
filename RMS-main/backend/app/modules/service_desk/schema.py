"""API-layer request/response models for the Service Desk module's HTTP
routes (docs/modules/service-desk/spec.md).

These are plain API-layer request/response shapes, not wire contracts --
this module dispatches no agent jobs (control-plane only, no worker; spec
§10), so there is no signed-job params/result pair here and no
`extra="forbid"` base is needed.

Per security invariant #5, no response model here carries `org_id` --
mirroring `budget/schema.py`'s `ExpenseOut`/`CategoryOut` convention.
`TicketOut` specifically exposes `has_attachments: bool` rather than any
attachment object or key, for the same reason `ExpenseOut` exposes
`has_attachment`/`invoice_filename` instead of the raw `invoice_object_key`
(that key leaks the tenant UUID and bucket name -- .git/sdd's post-review
note on budget's controller fix). `AttachmentOut` likewise never carries
`object_key`.

Every `*Update` model has all-optional fields (`X | None = None`) so the
route layer can apply `model_dump(exclude_unset=True)` (spec §9) -- an
omitted field is left untouched, while an explicitly-`null` field is a
caller's request to clear it. The service layer decides by key PRESENCE,
not by value. Non-nullable columns that receive an explicit `null` are
rejected as a typed 400 by the service layer before any mutation is
applied (that check lives there, not here -- this shape only needs to
support the omitted-vs-null distinction, same as Budget's `ExpenseUpdate`).

`status` is deliberately **absent** from `TicketUpdate`: a ticket's status
moves only through the dedicated `POST .../status` transition endpoint
(spec §9), never as a side effect of a generic `PATCH` -- the transition
endpoint is what runs lifecycle validation and writes
`ticket_status_history`; a bare status flip through `PATCH` would bypass
both. Budget's `ExpenseUpdate` excludes `status` for the identical reason
(its `POST .../confirm` and void path own that column instead).

`TicketStatusChange.to_status` is a bounded `str`, not a `Literal`
enumerating the two lifecycles' status vocabularies: spec §3.3 requires
that the allowed statuses per type and the full transition table live in
exactly one place, `lifecycle.py`, used by both the service layer and the
tests -- "there is no second copy". A `Literal` here listing status names
would be that second copy. The schema only bounds the string to the
column's length (`String(32)`, spec §2); `lifecycle.py` is the sole
authority on which values, and which transitions between them, are legal.

Schema-boundary bounds (Phase-11/12 lesson, carried from budget/schema.py
§8): every string bounded by `max_length`, every integer bounded on
**both** ends. There is no global exception handler in `backend/app/`, so
an out-of-range value that reaches Postgres surfaces as an unhandled
`22001`/`DataError: value out of int32 range` -- a 500.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.modules.sla.due import Target, TargetState

# --- Domain literals (spec §2/§3). Used by every *Create/*Update model so
# the accepted input domain can never drift between them. Match the DB
# `varchar` columns and the check constraints in
# migrations/versions/0023_service_desk.py.
#
# Every *Out model below deliberately widens these same fields (`type`,
# `priority`, `origin`, `visibility`, `status`) to plain `str` rather than
# reusing the alias. That widening is intentional, not an oversight: an
# *Out model is built from an already-persisted DB row that a CHECK
# constraint has already validated on the way in, so re-validating it on
# the way out buys nothing and would only make the response model brittle
# to a still-valid stored value in a future migration adding a domain
# member. This mirrors `budget/schema.py`'s `BudgetOut.granularity: str` /
# `ExpenseOut.status: str` convention -- response models built from DB rows
# type domain fields as `str`, only *Create/*Update constrain them via
# `Literal`.
TicketType = Literal["incident", "request"]
TicketPriority = Literal["low", "normal", "high", "urgent"]
# "integration" (whole-branch security review, finding A3) is the fifth
# origin: a ticket opened by another platform module reacting to ITS OWN
# event (`service_desk.adapters.push_change`, spec §7.1) rather than by a
# human filling in the create form (`"technician"`) or Phase 15's alert
# pipeline (`"alert"`). `create_ticket` maps it to `actor_type="system"` for
# the CREATE action's attribution, same as `"alert"` -- but, unlike
# `"alert"`, still requires a real human `requester_id`/`created_by`: the
# ticket genuinely has a human behind it (e.g. the change's author), only the
# act of creating it was system-driven, not that person sitting down and
# filing a helpdesk ticket.
TicketOrigin = Literal["technician", "alert", "portal", "email", "integration"]
CommentVisibility = Literal["public", "internal"]

# --- String / count bounds, mirrored from migrations/versions/0023_service_desk.py.
_MAX_KEY = 64
_MAX_NAME = 255
_MAX_SUBJECT = 255
# Text columns (queue/category description, ticket description, comment
# body, status-change/approval reason) are unbounded at rest, but the
# schema boundary still bounds them (Phase-11/12 lesson) so an oversized
# payload never reaches Postgres unrejected. None of these three numbers is
# spec-stated -- there is no migration column width to mirror here, since
# the underlying columns are `text`. They are this schema's own judgment
# call, sized to the field's expected use, not derived from any cap
# elsewhere:
#   _MAX_SHORT_TEXT -- a queue/category description: a line or two of
#     operator-authored text, not a document.
#   _MAX_BODY -- a ticket description or comment body: long enough for a
#     pasted error log or a detailed repro, short of a full file dump.
#   _MAX_REASON -- a status-change or approval reason: a sentence or a
#     short paragraph explaining a decision, not a policy document.
# All three exist to give the API a clean 422 instead of an unbounded
# payload landing in Postgres and consuming memory/bandwidth on the way in
# -- the same reasoning as the int32 bound below, just without a column
# width to match against.
_MAX_SHORT_TEXT = 1_000
_MAX_BODY = 20_000
_MAX_REASON = 2_000

# `q` (spec §9's ticket-list free-text filter, whole-branch review round C's
# C1 fix -- this was UNDELIVERED SPEC: declared in the spec and in
# `types.ts`, but with no route parameter, no service-layer argument, and no
# client method at all). It is a search TERM, not a stored column, so it is
# bounded the same judgment-call way `_MAX_SHORT_TEXT`/`_MAX_BODY`/
# `_MAX_REASON` are rather than mirrored from a migration column width.
# Public (no leading underscore) because `api.py`'s `list_tickets_route`
# imports it directly to bound the `Query` parameter at the schema boundary
# (the Phase-11/12 lesson) -- the same "one constant, not a duplicated
# number" discipline `MAX_ATTACHMENT_BYTES` (`threads.py`) already
# established for this module. Sized to `_MAX_SUBJECT`: a search term wider
# than the longest possible `subject` match target is already pointless,
# and it stays comfortably under `_MAX_BODY` (the other column `q` matches
# against) too.
MAX_SEARCH_Q = _MAX_SUBJECT

# Postgres `Integer` (int4) columns. `ge=0` alone still lets an
# out-of-range value reach asyncpg, which raises
# `DataError: value out of int32 range` -- unhandled, so a 500.
_MAX_COUNT = 2_147_483_647

_OptionalKey = Field(default=None, max_length=_MAX_KEY)
_OptionalName = Field(default=None, max_length=_MAX_NAME)
_OptionalShortText = Field(default=None, max_length=_MAX_SHORT_TEXT)
_OptionalBody = Field(default=None, max_length=_MAX_BODY)
_OptionalReason = Field(default=None, max_length=_MAX_REASON)
_OptionalCount = Field(default=None, ge=0, le=_MAX_COUNT)
_SortOrder = Field(default=0, ge=0, le=_MAX_COUNT)


# --- Queues (spec §2 `ticket_queue`, §5) --------------------------------


class QueueCreate(BaseModel):
    key: str = Field(max_length=_MAX_KEY)
    name: str = Field(max_length=_MAX_NAME)
    description: str = Field(default="", max_length=_MAX_SHORT_TEXT)
    sort_order: int = _SortOrder


class QueueUpdate(BaseModel):
    # `key` is deliberately not updatable: it is the stable identifier
    # queues are looked up and referenced by (`ticket_category.default_queue_id`,
    # filters). Renaming is done via `name` only, mirroring budget's
    # `CategoryUpdate` excluding its own `key`.
    name: str | None = _OptionalName
    description: str | None = _OptionalShortText
    active: bool | None = None
    sort_order: int | None = _OptionalCount


class QueueOut(BaseModel):
    # No `org_id` field here -- security invariant #5.
    id: UUID
    key: str
    name: str
    description: str
    active: bool
    sort_order: int
    created_by: UUID
    created_at: datetime


# --- Categories (spec §2 `ticket_category`, §4) -------------------------


class CategoryCreate(BaseModel):
    key: str = Field(max_length=_MAX_KEY)
    name: str = Field(max_length=_MAX_NAME)
    type: TicketType
    # Meaningful only when `type == "request"` (spec §2); a mismatched
    # combination is rejected by the service layer as a typed validation
    # error, and pinned again by a DB check constraint. Not re-validated
    # here -- this schema only shapes the request.
    requires_approval: bool = False
    approver_role_id: UUID | None = None
    default_queue_id: UUID | None = None
    sort_order: int = _SortOrder


class CategoryUpdate(BaseModel):
    # `key`/`type` are deliberately not updatable: `type` decides which
    # lifecycle and which status vocabulary a ticket in this category may
    # use (spec §3.3), and changing it out from under existing tickets
    # would silently invalidate them -- the same reasoning budget's
    # `BudgetUpdate` uses to keep `granularity` immutable.
    name: str | None = _OptionalName
    requires_approval: bool | None = None
    approver_role_id: UUID | None = None
    default_queue_id: UUID | None = None
    active: bool | None = None
    sort_order: int | None = _OptionalCount


class CategoryOut(BaseModel):
    # No `org_id` field here -- security invariant #5.
    id: UUID
    key: str
    name: str
    type: str
    requires_approval: bool
    approver_role_id: UUID | None
    default_queue_id: UUID | None
    active: bool
    sort_order: int
    created_by: UUID
    created_at: datetime


# --- Tickets (spec §2 `ticket`, §3) -------------------------------------


class TicketCreate(BaseModel):
    type: TicketType
    category_id: UUID
    # Optional: defaults from `ticket_category.default_queue_id` when
    # omitted (spec §5). Creation fails as a typed validation error if
    # neither resolves -- that resolution is the service layer's job, not
    # this schema's.
    queue_id: UUID | None = None
    subject: str = Field(max_length=_MAX_SUBJECT)
    description: str = Field(default="", max_length=_MAX_BODY)
    priority: TicketPriority = "normal"
    # Nullable only for machine-origin (`origin == "alert"`) tickets (spec
    # §2); the service layer enforces the human-attribution check that the
    # DB constraint also pins. Not re-validated here.
    requester_id: UUID | None = None
    assigned_to: UUID | None = None
    device_id: UUID | None = None
    # `origin` is deliberately NOT a field here. It is a `create_ticket`
    # keyword argument decided by the CALLER (the route passes "technician";
    # Phase 15 passes "alert"), never by the request body.
    #
    # If a client could set it, POSTing {"origin": "alert"} would create a
    # ticket with `requester_id=None` and `created_by=None` — the machine
    # path — and `ck_ticket_human_attribution` permits exactly that for
    # `origin="alert"`. The result is a forged system-attributed ticket with
    # no human on it. There is no `data.origin` for `create_ticket` to read
    # or ignore -- the field simply is not here, so a future route author has
    # nothing to forward in the first place.


class TicketUpdate(BaseModel):
    """All fields `X | None = None` so the route layer can apply this with
    `model_dump(exclude_unset=True)` (spec §9) -- an omitted field is left
    untouched, an explicitly-`null` field is a caller's request to clear
    it. Non-nullable columns (`category_id`, `queue_id`, `subject`) reject
    an explicit `null` in a pass the service layer runs before any
    mutation, not here.

    `status` is deliberately absent -- see the module docstring. `type`,
    `requester_id`, `origin`, and `source_alert_id` are immutable after
    creation and are likewise absent.
    """

    # `queue_id` and `assigned_to` are deliberately ABSENT. Task 8 made
    # `assign_ticket` their only write path AFTER CREATION -- `TicketCreate`
    # still carries its own `assigned_to`, a narrow, deliberate carve-out
    # (task 8's S6) -- because spec §5 requires both to need `ticket.assign`
    # (not `ticket.write`) and §8.3 files them under the `ticket.assigned`
    # audit action.
    #
    # Leaving them declared here would make a `PATCH {"queue_id": ...}` a
    # silent no-op returning 200: the field validates, the service ignores it,
    # and the caller believes the ticket moved. A silent no-op is worse than an
    # error — it is an error the client cannot see. Absent, the same request is
    # dropped by Pydantic's `extra="ignore"`, and a route that wants to reject
    # it explicitly can, but no code path can quietly pretend it worked.
    #
    # This is the same reasoning that removed `origin` from `TicketCreate`: a
    # field that exists is a field someone forwards.
    category_id: UUID | None = None
    priority: TicketPriority | None = None
    subject: str | None = Field(default=None, max_length=_MAX_SUBJECT)
    description: str | None = _OptionalBody
    device_id: UUID | None = None


class TicketSlaSummary(BaseModel):
    """A ticket's SLA deadlines, as the queue shows them.

    The two states are the SLA engine's own verdict (`sla.views.target_state`);
    `next_due_at` is the earliest deadline still owed, and is null when the
    clock is paused, void or fully met.
    """

    response_due_at: datetime
    resolution_due_at: datetime
    response_state: TargetState
    resolution_state: TargetState
    next_due_at: datetime | None
    next_due_target: Target | None
    breached: bool
    paused: bool


class TicketOut(BaseModel):
    """One ticket as a `ticket.read` holder sees it.

    Documented here as well as on `TicketListResponse` because the tool-surface
    sweep reads only the OUTERMOST response model: wrapping the list in an
    envelope moved this class out of its view, and a class that stopped being
    inspected is not a class that became self-describing.
    """

    # No `org_id` field here -- security invariant #5.
    id: UUID
    number: str
    type: str
    category_id: UUID
    queue_id: UUID
    status: str
    priority: str
    subject: str
    description: str
    requester_id: UUID | None
    assigned_to: UUID | None
    device_id: UUID | None
    origin: str
    source_alert_id: UUID | None
    resolved_at: datetime | None
    closed_at: datetime | None
    # A count or list of attachment objects would either leak `object_key`
    # indirectly or force a second query on every list row; a plain
    # boolean is all a caller needs to know before opening the detail view
    # and fetching `GET .../attachments` explicitly.
    has_attachments: bool
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    # Null when no SLA policy applies. Optional so older clients are unaffected.
    sla: TicketSlaSummary | None = None


class TicketListResponse(BaseModel):
    """One page of tickets and the size of the set it was cut from.

    THE `total` IS THE POINT (closeout rec. 10). The list route returned a
    bare `list[TicketOut]` capped at 200, so a full page and a complete queue
    looked identical from the console -- `TicketListPage` could say "the list
    is capped and there is probably more" and nothing truer. Counted from the
    same filtered set as `items`, BEFORE `limit`/`offset`, so the two never
    answer different questions.
    """

    items: list[TicketOut]
    total: int


class TicketStatusChange(BaseModel):
    # See module docstring: bounded `str`, not a `Literal` -- `lifecycle.py`
    # is the sole authority on the valid vocabulary and transition table
    # (spec §3.3), so it is not duplicated here.
    to_status: str = Field(max_length=32)
    reason: str = Field(default="", max_length=_MAX_REASON)


class TicketAssign(BaseModel):
    """Backs the single `POST .../assign` endpoint that covers assign,
    reassign, unassign, and move-queue (spec §5, §9) -- all four require
    `ticket.assign`. Both fields are optional and read with
    `model_dump(exclude_unset=True)` by the service layer, same presence
    convention as `TicketUpdate`, so a caller can move the queue without
    touching the assignee or vice versa. An explicit `assigned_to: null`
    unassigns.
    """

    assigned_to: UUID | None = None
    queue_id: UUID | None = None


class ApprovalDecision(BaseModel):
    """Backs both `POST .../approve` and `POST .../reject` (spec §4, §9).
    `reason` defaults to empty for an approval; the service layer rejects
    an empty `reason` specifically on the reject path, since that
    requirement depends on which endpoint is calling, not on this shape.
    """

    reason: str = Field(default="", max_length=_MAX_REASON)


# --- Comments & attachments (spec §6) -----------------------------------


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=_MAX_BODY)
    visibility: CommentVisibility = "public"


class TicketResolve(BaseModel):
    """Resolve a ticket, optionally posting a reply first, in one transaction."""

    reply: CommentCreate | None = None


class CommentOut(BaseModel):
    # No `org_id` field here -- security invariant #5.
    id: UUID
    ticket_id: UUID
    author_id: UUID | None
    author_type: str
    body: str
    visibility: str
    created_at: datetime


class AttachmentOut(BaseModel):
    # No `org_id` field here -- security invariant #5. `object_key` is
    # never exposed: it is server-derived and would leak the tenant UUID
    # (spec §2), the same reasoning budget's `ExpenseOut` uses to withhold
    # `invoice_object_key`.
    id: UUID
    ticket_id: UUID
    filename: str
    byte_size: int
    uploaded_by: UUID
    created_at: datetime


class StatusHistoryOut(BaseModel):
    # No `org_id` field here -- security invariant #5.
    id: UUID
    ticket_id: UUID
    from_status: str | None
    to_status: str
    actor_type: str
    actor_id: UUID | None
    reason: str
    created_at: datetime


# --------------------------------------------------------------------------- #
# Canned responses
# --------------------------------------------------------------------------- #

_MAX_CANNED_TITLE = 100
CannedTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=_MAX_CANNED_TITLE)
]


class CannedResponseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: CannedTitle
    body: str = Field(min_length=1, max_length=_MAX_BODY)


class CannedResponseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: CannedTitle | None = None
    body: str | None = Field(default=None, min_length=1, max_length=_MAX_BODY)


class CannedResponseOut(BaseModel):
    """A saved reply an operator can insert into a ticket comment."""

    # No `org_id` field here -- security invariant #5.
    id: UUID
    title: str
    body: str
    created_by: UUID
    created_at: datetime
    updated_by: UUID | None
    updated_at: datetime | None
