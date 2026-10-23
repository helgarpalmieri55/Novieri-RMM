"""Narrow, portal-specific response/request models (docs/modules/portal/spec.md
§5, §6).

**These are NOT the staff `service_desk.schema.TicketOut`/`CommentOut`, and
they do not import, subclass, or wrap them.** Spec §5: "No portal response
may contain an internal comment, another user's identity beyond a display
name on a public comment, a queue, an assignee, or any field whose value is
staff-only. Response models are portal-specific and narrow; they do not
reuse the staff `TicketOut`." `PortalTicketDetail` in particular carries no
`queue_id`, no `assigned_to`, and no `internal_notes` -- a structural
guarantee (`test_portal_detail_exposes_no_staff_fields` asserts this against
`model_fields`, not against any runtime filter).

Concretely narrower than `TicketOut`/`CommentOut`:

- No `queue_id` -- spec §2.1: a requester never *chooses* a queue and never
  *sees* one, even though `ticket.queue_id` is NOT NULL at the DB layer.
- No `assigned_to` -- who (if anyone) is working a ticket is staff-only.
- No `category_id`/`device_id`/`origin`/`source_alert_id`/`created_by` --
  none of these are anything a requester needs to see or would recognise.
- `PortalCommentOut` carries no `author_id`/`author_type`: even though
  migration `0030` grants `portal_user` a restricted SELECT on `users` (own
  row only, via a new `portal_isolation` policy -- NOT "no SELECT grant at
  all", corrected here after an earlier, now-stale version of this docstring
  said otherwise), that grant resolves only the CALLER's own row, never a
  staff author's, so there is still no way to turn a staff `author_id` into
  a display name here, and a raw UUID is not information a requester can act
  on. Only ever-public comment bodies are ever modelled here (spec §2.3) --
  there is no `visibility` field because there is only one value a requester
  can ever see.
- `PortalStatusHistoryOut`/`PortalApprovalOut` likewise drop
  `actor_id`/`actor_type`/`decided_by` for the identical reason: a staff
  member's identity, not a display name, and portal has no way to resolve
  one anyway.
- `PortalStatusHistoryOut` ALSO drops `reason` -- a distinct, security
  decision, not merely narrowness-by-default. See that class's own
  docstring and spec §5.

Every string bounded by `max_length` (the Phase-11/12 lesson, carried from
`service_desk/schema.py`) -- there is no global exception handler in
`backend/app/`, so an unbounded payload reaching Postgres would surface as an
unhandled error rather than a clean 422.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Deliberately NOT imported from `service_desk.schema` -- this module adds no
# business logic (module docstring in `docs/modules/portal/spec.md`), but a
# shared Python type alias is not a second definition of ticket *behaviour*;
# it is only the string domain `create_my_ticket` is allowed to pass through
# to `service_desk.tickets.create_ticket`, which is itself the sole authority
# on what those strings mean. Kept as a narrow, local copy anyway (rather
# than importing the alias) so this schema module has zero import-time
# coupling to `service_desk.schema` -- a future rename or removal of that
# alias cannot break the portal by surprise.
PortalTicketPriority = Literal["low", "normal", "high", "urgent"]

_MAX_SUBJECT = 255
_MAX_BODY = 20_000


# --- Categories (read-only org refdata; spec §2.1) ----------------------


class PortalCategoryOut(BaseModel):
    id: UUID
    key: str
    name: str
    # Widened to `str` rather than a `Literal` -- this is built from an
    # already-persisted, already-validated DB row (mirrors `TicketOut`'s own
    # "*Out models widen domain literals to str" convention). A requester
    # needs `type` only to know incident vs. request; nothing here decides
    # ticket type validity.
    type: str


# --- Ticket create (spec §6.2) -------------------------------------------


class PortalTicketCreate(BaseModel):
    """No `requester_id`, no `type`, no `queue_id`, no `assigned_to`, no
    `origin` -- none of these are the caller's to set.

    `requester_id`/`created_by` come from `PortalContext` only (spec §6.2);
    there is no field here for either, so there is nothing for a request body
    to smuggle. `type` is likewise absent: `create_my_ticket` resolves it
    from the chosen category's own `type` (`ticket.category_id`'s `type` must
    already match `ticket.type` -- `create_ticket` enforces this identically
    for staff), so a requester picking a category never has to separately
    pick a matching type, and can never pick a MISMATCHED one either.
    """

    category_id: UUID
    subject: str = Field(max_length=_MAX_SUBJECT)
    description: str = Field(default="", max_length=_MAX_BODY)
    priority: PortalTicketPriority = "normal"


# --- Comments (spec §2.3, §6.2) ------------------------------------------


class PortalCommentCreate(BaseModel):
    """No `visibility` field. `add_my_comment` forces `visibility="public"`
    unconditionally (spec §2.2's `WITH CHECK` already refuses anything else
    at the database layer; this schema additionally gives a request body
    nothing to set in the first place)."""

    body: str = Field(min_length=1, max_length=_MAX_BODY)


class PortalCommentOut(BaseModel):
    """No `author_id`/`author_type`/`visibility` -- see module docstring."""

    id: UUID
    body: str
    created_at: datetime


# --- Attachments (spec §6.3) ----------------------------------------------


class PortalAttachmentOut(BaseModel):
    """No `object_key` -- it is server-derived and would leak the tenant
    UUID/bucket layout, the identical reasoning `service_desk.schema.
    AttachmentOut` already applies. No `uploaded_by` either: this model backs
    both the upload response (always the requester's own id) AND the
    download route's response, and the download route (I1 fix,
    `api.py::download_my_attachment_route`) is now itself scoped to
    `uploaded_by = requester GUC` -- a staff-uploaded attachment on the
    requester's own ticket is a 404, not a 200 -- so every attachment this
    model can ever describe is, once again, genuinely the requester's own.
    Omitting the field anyway keeps this model narrow by the same discipline
    as everything else here; it is not what makes the property true."""

    id: UUID
    filename: str
    byte_size: int
    created_at: datetime


# --- Status history & approval (spec §0.3, §5) ----------------------------


class PortalStatusHistoryOut(BaseModel):
    """No `actor_id`/`actor_type` -- see module docstring. **No `reason`
    either -- deliberately, and this one is a security decision, not a
    narrowness-by-default one (docs/modules/portal/spec.md §5's amendment).**

    `ticket_status_history.reason` is unbounded staff free text with no
    visibility column: unlike `ticket_comment` (which has a `visibility`
    column and an RLS predicate that makes an internal note structurally
    unreadable by `portal_user`), there is no way to distinguish a
    customer-safe reason from an internal one written by a technician about
    the requester ("INTERNAL: repeat offender, see HR case 42"). Spec §2.3
    calls disclosing internal staff discussion about a person to that person
    the worst thing this module could do -- returning this field verbatim
    would be exactly that, one GET away, with no special knowledge required.
    A requester sees THAT their ticket moved from one status to another, and
    WHEN -- never WHY, in staff's own words. See spec §5 for the full
    reasoning and the deferred customer-visible-reason field this leaves for
    later (§11)."""

    from_status: str | None
    to_status: str
    created_at: datetime


class PortalApprovalOut(BaseModel):
    """No `decided_by` -- see module docstring. Exposes exactly what spec
    §0.3 asks for: outcome, reason, and when -- closing the read-path gap the
    ticket core left (`ticket_approval.decided_by`/`decided_at`/`reason` had
    no read path anywhere before this module)."""

    state: str
    reason: str
    decided_at: datetime | None


# --- Tickets: list & detail (spec §5) -------------------------------------


class PortalTicketSummary(BaseModel):
    """Backs `GET /portal/tickets` (the "My tickets" list, spec §7): status,
    last update, and ticket number, and nothing a requester cannot already
    read off their own tickets. No `queue_id`, no `assigned_to`, no
    `category_id`."""

    id: UUID
    number: str
    type: str
    status: str
    priority: str
    subject: str
    created_at: datetime
    updated_at: datetime


class PortalTicketDetail(BaseModel):
    """Backs `GET /portal/tickets/{id}` (spec §5): number, type, status,
    subject, description, created/updated timestamps, public comments,
    status history, attachments, and the approval outcome when one exists.

    Deliberately absent (spec §5, structural guarantee -- see
    `test_portal_detail_exposes_no_staff_fields`): `queue_id`, `assigned_to`
    (nor any `assignee_id` alias for it -- the real column is
    `assigned_to`), `category_id`, `device_id`, `origin`, `source_alert_id`,
    `created_by`, and `internal_notes` (there is no such column anywhere in
    this schema -- internal comments are filtered out of `comments` entirely,
    both by the RLS policy and by this module's own query, spec §2.3).

    `attachments` (I2 fix) lists only the requester's OWN uploads --
    `tickets.get_my_ticket` filters on `uploaded_by = requester_id`, matching
    I1's scoping of the download route exactly, so nothing in this list ever
    points at a staff-uploaded file the download route would then 404 on.
    """

    id: UUID
    number: str
    type: str
    status: str
    priority: str
    subject: str
    description: str
    created_at: datetime
    updated_at: datetime
    comments: list[PortalCommentOut]
    status_history: list[PortalStatusHistoryOut]
    attachments: list[PortalAttachmentOut]
    approval: PortalApprovalOut | None


class PortalHelpSuggestion(BaseModel):
    """A help article that may answer what the requester is about to report."""

    id: UUID
    title: str


class PortalHelpArticle(BaseModel):
    """One help article as a requester reads it: title and body only. No
    category, links, revision history or author, which are staff concerns."""

    id: UUID
    title: str
    body: str
