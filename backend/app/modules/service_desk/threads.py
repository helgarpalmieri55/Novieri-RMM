"""Ticket comments and attachments (docs/modules/service-desk/spec.md §2, §6,
§8.3).

**Comments are append-only** (spec §6) -- there is no `update_comment` or
`delete_comment` in this file, deliberately: a ticket thread is a record of
what was said, and an editable thread is worth less as evidence. Correction
is a further comment, not an edit to an existing one.

**A terminal ticket accepts no new comments, and no new attachments** (spec
§6, §3.3). Both are gated on `lifecycle.TERMINAL_STATUSES` (`closed`,
`cancelled`, `rejected`) -- the same set `tickets._reject_if_terminal` uses
for field edits -- and NEVER on the narrower `lifecycle.FINAL_STATUSES`.
`closed` is deliberately reopenable (`closed -> open`/`in_progress`, spec
§3.1/§3.2); using `FINAL_STATUSES` here would be the assignment rule, not the
comment/attachment rule, and would wrongly let a closed ticket keep
collecting comments and files right up until someone reopens it. An
attachment is thread content in the same sense a comment is -- a ticket that
"accepts no new comments" while silently accepting new file uploads would be
incoherent, and the portal sub-project would otherwise let an end user upload
into a closed ticket.

**Attachment storage inherits Budget's hardened implementation wholesale**
(spec §2, §6 -- "does not re-derive it"). Two details, in particular, are not
this file's to re-derive:

- The filename sanitizer itself now lives in `app.core.attachments` --
  EXTRACTED there (not copied) so Budget's expense-invoice upload and this
  module's ticket-attachment upload share one implementation of a
  security-critical guard. `sanitize_attachment_filename` below is a thin
  wrapper translating the shared function's plain `ValueError` into this
  module's own `TicketValidationError`.
- The object-storage key is **entirely server-derived** --
  `{org_id}/service-desk/tickets/{ticket_id}/{attachment_id}` -- with no
  client-supplied segment anywhere in it (`app.core.storage.
  ObjectStore._ticket_attachment_key`). The client-supplied `filename` is the
  sanitized DISPLAY name only, stored in `ticket_attachment.filename`; it
  never reaches the key or a response header unsanitized.

Unlike Budget's expense (one attachment slot, a column on an existing row), a
ticket can carry many attachments, each its own `ticket_attachment` row --
so `add_attachment` generates the row's id itself, BEFORE the object-storage
write, so the same id can be embedded in the key. This mirrors the
pre-generated-id pattern `budget.budgets.create_budget`/`budget.rates.
create_rate`/`service_desk.refdata` already use for a row whose id must be
known before something else references it. Note: unlike Budget's fixed-key
overwrite, a failure between the object-storage write succeeding and the
transaction committing (or a caller that never commits) leaves that object
orphaned in storage with no `ticket_attachment` row pointing to it -- carried
as a known limitation, not fixed here.

**Upload size** is capped at `MAX_ATTACHMENT_BYTES` (10 MiB -- ample for a
screenshot or a log excerpt, the two things a technician actually attaches to
a ticket) and enforced here, on the fully-read `data: bytes` this function is
handed, as a defence-in-depth belt independent of whatever called it. This
function takes bytes directly (there is no FastAPI `Request` at this layer),
so it cannot itself run `app.core.http.reject_oversized_content_length`/
`read_bounded_body` -- those are `Request`-shaped, streaming guards. The
future API route for this module uploads bytes to `add_attachment` calls
BOTH of those helpers first, exactly as `app.modules.budget.api.
upload_attachment_route` already does, before ever reading the body into
memory; the check here is a second, independent line of defence, not a
substitute for that streaming guard.

Every mutation here calls `audit_service.record` and then flushes (invariant
#4); every query carries an explicit `org_id` predicate (invariant #5,
spec §8.1).
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.attachments import sanitize_attachment_filename as _sanitize_attachment_filename
from app.core.audit import audit_service
from app.core.events import publish
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.core.ticket_words import status_quoted
from app.models import TicketAttachment, TicketComment
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.lifecycle import TERMINAL_STATUSES
from app.modules.service_desk.tickets import get_ticket, hold_ticket_client
from app.modules.sla.tracking import note_public_comment

# Spec §2 `ticket_comment.author_type`: NOT a free-form string. Checked here,
# before the row is built, so an unrecognized value is a typed 400 rather
# than a NOT NULL/length surprise or a value the check constraint below
# silently accepts uninspected.
_VALID_AUTHOR_TYPES: frozenset[str] = frozenset({"user", "system"})

# Spec §2 `ticket_comment.visibility`. Not enforced by a DB check constraint
# (unlike `author_type`/`author_id`'s `ck_comment_author`), so this is the
# only place an invalid value is rejected before it reaches the column.
_VALID_VISIBILITIES: frozenset[str] = frozenset({"public", "internal"})

# An ample cap for a screenshot or a log excerpt -- the two things a
# technician actually attaches to a ticket. See module docstring for why the
# enforcement here is a defence-in-depth belt, not the primary (streaming)
# guard. Public (no leading underscore): Task 11's upload route needs the
# exact same limit for its `reject_oversized_content_length`/`read_bounded_
# body` streaming guards, and a second, independently-maintained copy of this
# number is exactly the kind of duplication the sanitizer was extracted out
# of `app.core.attachments` to avoid.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MiB


def sanitize_attachment_filename(filename: str) -> str:
    """Sanitize a client-supplied display filename (see `app.core.
    attachments.sanitize_attachment_filename` for the full reasoning: reject
    outright rather than store-and-hope, `os.path.basename` defence in
    depth, the ASCII-only `fullmatch` allow-list, the 255-char cap). Every
    rejection here is a typed `TicketValidationError`."""
    try:
        return _sanitize_attachment_filename(filename)
    except ValueError as exc:
        raise TicketValidationError(str(exc)) from exc


def _reject_if_terminal_for_thread_content(ticket_id: UUID, status: str, *, kind: str) -> None:
    """Spec §6/§3.3: `TERMINAL_STATUSES` (`closed`, `cancelled`, `rejected`)
    gates new comments AND new attachments -- never `FINAL_STATUSES`, which
    would wrongly keep accepting thread content on a reopenable `closed`
    ticket right up until the moment it is actually reopened (module
    docstring). `kind` is `"comments"` or `"attachments"`, only for the
    error message -- both callers share the identical gate."""
    if status in TERMINAL_STATUSES:
        raise TicketValidationError(
            tr(
                "ticket {ticket_id} is {status} and accepts no new {kind}",
                ticket_id=ticket_id,
                status=status_quoted(status),
                kind=kind,
            )
        )


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


async def add_comment(
    session: AsyncSession,
    org_id: UUID,
    ticket_id: UUID,
    *,
    body: str,
    author_id: UUID | None,
    author_type: Literal["user", "system"] = "user",
    visibility: str = "public",
) -> TicketComment:
    """Append a comment to a ticket (spec §2, §6, §8.3).

    Order of checks, all before anything is mutated: the ticket exists
    (org-scoped, via `tickets.get_ticket`) and is not terminal for comments;
    `author_type` and `visibility` are recognized values; a `system` comment
    carries no `author_id` and a `user` comment carries one -- the same rule
    `ck_comment_author` enforces at the database layer, checked here first so
    a caller sees a typed 400 rather than an `IntegrityError` 500.

    The ticket's client is held still first (`tickets.hold_ticket_client`), so
    a comment never lands in the client a ticket is being moved out of; the
    ticket is then read afresh, after any move in flight has committed.
    """
    await hold_ticket_client(session, ticket_id)
    ticket = await get_ticket(session, org_id, ticket_id, fresh=True)
    _reject_if_terminal_for_thread_content(ticket.id, ticket.status, kind=tr("comments"))

    if author_type not in _VALID_AUTHOR_TYPES:
        raise TicketValidationError(
            tr(
                "unknown author_type: {author_type}",
                author_type=repr(author_type),
            )
        )
    if visibility not in _VALID_VISIBILITIES:
        raise TicketValidationError(
            tr(
                "unknown visibility: {visibility}",
                visibility=repr(visibility),
            )
        )
    if author_type == "user" and author_id is None:
        raise TicketValidationError(tr("author_id is required when author_type='user'"))
    if author_type == "system" and author_id is not None:
        raise TicketValidationError(tr("author_id must be None when author_type='system'"))

    comment = TicketComment(
        org_id=org_id,
        ticket_id=ticket.id,
        author_id=author_id,
        author_type=author_type,
        body=body,
        visibility=visibility,
    )
    session.add(comment)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=author_type,
        actor_id=author_id,
        action="ticket.comment_added",
        target_type="ticket",
        target_id=str(ticket.id),
        client_id=ticket.client_id,
        # Identifiers and state only -- never the comment body (spec §8.3: a
        # user can type anything into it, and the audit log is not the place
        # to duplicate it).
        metadata={"comment_id": str(comment.id), "visibility": visibility},
    )
    await session.flush()

    # Notification outbox (docs/modules/notification/spec.md §3, §6.4).
    # Public comments and internal notes are DIFFERENT event types -- an
    # internal note's body never enters the payload, so even a mis-routed
    # delivery cannot disclose staff discussion to the requester. Payload
    # shape (every id `str(uuid)` or `None`, `requester_id` present
    # unconditionally, even here) matches `notification.routing.
    # resolve_recipients`'s fixed contract -- that resolver reads
    # `requester_id` on every event type, internal notes included, to
    # exclude the requester regardless of which `recipient_kind` surfaced
    # them as a candidate.
    is_internal = comment.visibility == "internal"
    await publish(
        session,
        org_id=org_id,
        event_type="ticket.internal_note_added" if is_internal else "ticket.comment_added",
        subject_type="ticket",
        subject_id=ticket.id,
        payload={
            "ticket_number": ticket.number,
            "queue_id": str(ticket.queue_id) if ticket.queue_id else None,
            "assignee_id": str(ticket.assigned_to) if ticket.assigned_to else None,
            "requester_id": str(ticket.requester_id) if ticket.requester_id else None,
            "author_id": str(comment.author_id) if comment.author_id else None,
            # Spec §6.4 mechanism 1: the note's text never enters the event.
            **({} if is_internal else {"body": comment.body}),
        },
        actor_user_id=author_id,
        client_id=ticket.client_id,
    )

    # SLA met detection (docs/modules/sla/spec.md §0.6). Gated on the SAME
    # `is_internal` that chose the event type above, and deliberately on that
    # variable rather than a second read of `comment.visibility`: an internal
    # note must never reach the hook, and one test deciding both facts is what
    # makes "the clock stops on customer contact" and "the note stays private"
    # impossible to drift apart. The SLA module does not re-derive visibility
    # itself -- that is the ticket core's answer to give (SLA spec §1).
    #
    # No-ops for a ticket with no `ticket_sla` row, which is every ticket in an
    # org that has configured no SLA policy (SLA spec §3), so this costs one
    # indexed lookup on the common path. Same transaction as the comment
    # itself: a stamped `response_met_at` and the comment that justifies it are
    # committed together or not at all.
    if not is_internal:
        await note_public_comment(session, org_id, ticket.id, author_id=comment.author_id)
    return comment


async def list_comments(
    session: AsyncSession,
    org_id: UUID,
    ticket_id: UUID,
    *,
    include_internal: bool = True,
) -> list[TicketComment]:
    """List a ticket's comments, oldest first (spec §2, §6).

    `include_internal=False` excludes `visibility="internal"` comments --
    the distinction the future end-user portal depends on existing now, even
    though nothing external can read comments yet (spec §6). Org-scoped
    load of the ticket first, so a cross-org `ticket_id` raises
    `TicketNotFoundError` rather than silently returning an empty list.
    """
    await get_ticket(session, org_id, ticket_id)

    stmt = select(TicketComment).where(
        TicketComment.org_id == org_id, TicketComment.ticket_id == ticket_id
    )
    if not include_internal:
        stmt = stmt.where(TicketComment.visibility != "internal")
    stmt = stmt.order_by(TicketComment.created_at, TicketComment.id)
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


async def add_attachment(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    ticket_id: UUID,
    *,
    filename: str,
    data: bytes,
    store: ObjectStore,
) -> TicketAttachment:
    """Upload an attachment to a ticket (spec §2, §6, §8.3).

    The object key is entirely server-derived
    (`{org_id}/service-desk/tickets/{ticket_id}/{attachment_id}`, `app.core.
    storage.ObjectStore._ticket_attachment_key`) -- `filename` never reaches
    it. The attachment's own id is generated here, before the object-storage
    write, so the id can be embedded in the key (module docstring); the row
    is only added to the session -- with that same id -- after the object
    store confirms the write.

    Gated on `lifecycle.TERMINAL_STATUSES`, same as `add_comment` and for the
    same reason (module docstring): an attachment is thread content, and a
    ticket that accepts no new comments accepts no new files either.
    """
    sanitized_filename = sanitize_attachment_filename(filename)

    if len(data) > MAX_ATTACHMENT_BYTES:
        raise TicketValidationError(
            tr(
                "attachment exceeds the {limit}-byte limit",
                limit=MAX_ATTACHMENT_BYTES,
            )
        )

    # Held before the read, as `add_comment` does: an attachment is never left
    # in the client its ticket is being moved out of.
    await hold_ticket_client(session, ticket_id)
    ticket = await get_ticket(session, org_id, ticket_id, fresh=True)
    _reject_if_terminal_for_thread_content(ticket.id, ticket.status, kind=tr("attachments"))

    attachment_id = uuid7()
    object_key = await store.put_ticket_attachment(org_id, ticket.id, attachment_id, data)

    attachment = TicketAttachment(
        id=attachment_id,
        org_id=org_id,
        ticket_id=ticket.id,
        object_key=object_key,
        filename=sanitized_filename,
        byte_size=len(data),
        uploaded_by=actor_id,
    )
    session.add(attachment)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ticket.attachment_uploaded",
        target_type="ticket",
        target_id=str(ticket.id),
        client_id=ticket.client_id,
        metadata={"attachment_id": str(attachment.id), "filename": sanitized_filename},
    )
    await session.flush()
    return attachment


async def get_attachment(
    session: AsyncSession, org_id: UUID, attachment_id: UUID
) -> TicketAttachment:
    """Org-scoped load. A cross-org attachment id is indistinguishable from a
    nonexistent one (spec §8.1, invariant #5). Does not audit -- attachment
    DOWNLOAD is the audited read (spec §6, §8.3), and that is a route-level
    concern (mirrors `budget.expenses.get_expense`, which is likewise
    read-only-and-silent; `budget.api.download_attachment_route` is what
    audits `expense.attachment_downloaded`)."""
    attachment = (
        await session.execute(
            select(TicketAttachment).where(
                TicketAttachment.org_id == org_id, TicketAttachment.id == attachment_id
            )
        )
    ).scalar_one_or_none()
    if attachment is None:
        raise TicketNotFoundError(
            tr(
                "attachment {attachment_id} not found",
                attachment_id=attachment_id,
            )
        )
    return attachment
