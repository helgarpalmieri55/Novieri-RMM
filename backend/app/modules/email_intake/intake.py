"""The disposition engine -- spec §8's table, executed.

THE `inbound_message` ROW IS WRITTEN **FIRST**, IN THE SAME TRANSACTION AS
THE TICKET OR COMMENT IT PRODUCES (spec §4 rule 1). IMAP has no
transactions: the fetch, the ticket write and the \\Seen flag are three
operations across two systems. A redelivered message must collide on
`uq_inbound_message_org_message_id` instead of creating a second ticket.
`AlreadyProcessed` is that collision, and it is a NORMAL OUTCOME -- the
poller marks the message \\Seen and moves on. Treating it as a failure is
how one stuck message blocks a mailbox forever (§4 rule 3).

FIRST is load-bearing, not stylistic. An earlier revision wrote the row
LAST; same-transaction atomicity still made a duplicate ticket impossible,
but `threads.add_attachment` performs an OBJECT-STORAGE PUT before the
transaction commits (its docstring carries the orphan risk explicitly), and
object storage is not covered by the rollback. Row-last therefore re-ran
`create_ticket` *and* the PUT on every redelivery and then discarded only
the database side: one delivery plus three redeliveries of a message with
one attachment left four objects in the bucket behind a single
`ticket_attachment` row, with nothing pointing at the orphans and no sweep
to collect them. Row-first makes the collision happen before any ticket
work and before any storage write, so a redelivery costs one failed INSERT.
The row is inserted with a provisional disposition and `UPDATE`d with the
real outcome before the same transaction commits -- see `_begin_row` and
`_finish_row`.

THE TOKEN IS NOT AUTHORITY. `identity.py` deliberately exposes the two
checks §0.2 requires as two separate facts and offers no helper that
combines them; THIS MODULE IS THE ONLY PLACE THEY MAY BE COMBINED. After a
token resolves to a ticket, the sender must ALSO be that ticket's
`requester_id`. The token identifies the ticket -- it sits in the headers of
every message in the thread, is forwarded wherever the requester forwards
it, and outlives their employment. A valid token from the wrong sender is
QUARANTINED: no comment, no reopen, and no new ticket either.

THIS MODULE ADDS NO TICKET RULE. It calls `tickets.create_ticket`,
`threads.add_comment`, `threads.add_attachment` and
`transitions.change_status`. It defines no status, no transition, and no
second answer to who may do what. A behaviour change belongs in
`app/modules/service_desk/`, where its tests live.

REOPEN HAPPENS BEFORE THE COMMENT. `lifecycle.TERMINAL_STATUSES` contains
`closed`, and `threads.add_comment` rejects a terminal ticket -- commenting
first would raise on every reply to a closed ticket. Spec §8 lists the two
in the other order; the spec is wrong, and the Task 10 spec amendment
records the correction (C1). `fulfilled -> in_progress` is the request-type
reopen edge §8's parenthetical omits (C2). Both live in ONE map,
`_REOPEN_TARGETS`, asserted against `lifecycle.can_transition` at import
time so it cannot drift from the state machine.

THE REOPEN WINDOW IS MEASURED FROM `closed_at` FOR A CLOSED TICKET AND FROM
`resolved_at` OTHERWISE, falling back to `updated_at` when neither is
stamped. Spec §0.3 says "beyond the window" without naming the timestamp,
and which one it is materially changes behaviour, so the choice is recorded
here. `transitions.change_status` stamps `resolved_at` on entering
`resolved`/`fulfilled` and `closed_at` on entering `closed`, so this anchor
is exactly "when the ticket entered the state the reply is arriving into" --
the thing the requester's "you closed this two days ago" intuition is about.
`updated_at` alone would be wrong: any later administrative edit (a
reassignment, a category correction) would silently extend the window on a
ticket nobody had touched for a year. It is kept only as the fallback for a
ticket whose status was set without going through `change_status`, where the
alternative is a crash.

SECTION 6's OTHER ARM IS HERE, NOT IN `parsing.py`. §6 refuses mail whose
`From:` is "the org's own notification sender or this mailbox's own
address". The parser owns the second half; the first needs the org's
notification-channel config, which a pure parser cannot look up (see
`parsing._detect_loop`'s docstring). Without it the platform's own outbound
notification becomes a comment, which notifies again -- the mail loop §6
exists to prevent.

EVERY ATTACKER-CONTROLLED STRING IS TRUNCATED TO ITS COLUMN'S WIDTH BEFORE
INSERT. `parsing.py` bounds its own `refusal_reason`, but the reasons this
module composes (unknown sender, ambiguous sender, wrong-sender token) embed
the `From:` header verbatim. An over-long value must not turn hostile input
into an insert error -- that is one message stopping a mailbox (§11.10),
routed around `parse_message`'s no-raise guarantee.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import use_locale
from app.core.storage import ObjectStore
from app.models import User
from app.modules.email_intake.identity import (
    extract_reply_token,
    resolve_sender,
    resolve_ticket_by_token,
)
from app.modules.email_intake.models import DISPOSITIONS, EmailMailbox, InboundMessage
from app.modules.email_intake.parsing import ParsedMessage
from app.modules.notification.models import NotificationChannel
from app.modules.service_desk.errors import TicketValidationError
from app.modules.service_desk.lifecycle import TERMINAL_STATUSES, can_transition
from app.modules.service_desk.models import Ticket, TicketCategory, TicketComment

# `_MAX_SUBJECT` (255) and `_MAX_BODY` (20_000) are `service_desk.schema`'s
# own bounds on `TicketCreate.subject`/`.description`, and they are PYDANTIC
# constraints: an inbound body longer than `_MAX_BODY` would raise a
# `ValidationError` out of `create_ticket`, i.e. one oversized mail stopping
# the mailbox. Imported rather than restated so this module's truncation and
# the ticket core's validation can never disagree -- a second, hand-copied
# `20_000` here is exactly the two-sources-of-truth shape `lifecycle.py`'s
# docstring warns about.
from app.modules.service_desk.schema import (
    _MAX_BODY,
    _MAX_SUBJECT,
    TicketCreate,
    TicketType,
)
from app.modules.service_desk.threads import add_attachment, add_comment
from app.modules.service_desk.tickets import create_ticket
from app.modules.service_desk.transitions import change_status

# `inbound_message` column widths (models.py). Every one of these is a
# `String(n)`; exceeding it is a database error on attacker-controlled input.
_REASON_MAX = 200
_MESSAGE_SUBJECT_MAX = 512
_FROM_ADDRESS_MAX = 320
_MESSAGE_ID_MAX = 998

# A mail with an empty body is common (a subject-only "it's broken", an
# attachment with no covering text). An empty `ticket_comment.body` reads as
# a rendering bug in the thread; this says what actually happened.
_EMPTY_BODY_PLACEHOLDER = "(no message body)"

# Spec §0.2's central claim, as the operator sees it on the quarantine list.
# Deliberately CONSTANT and content-free: the row already carries the
# targeted ticket in `ticket_id` and the sender in `sender_user_id`, and a
# reason that embedded the ticket's subject or number's surrounding text
# would leak one person's ticket content onto a row about someone else's
# message.
_WRONG_SENDER_REASON = "reply token does not belong to this sender"

# The disposition an `inbound_message` row carries between its INSERT (first,
# spec §4 rule 1) and the UPDATE that records the real outcome. Both happen
# inside one transaction, so this value is never observable outside it. It is
# `quarantined` rather than `created` so that it fails CLOSED if it ever were:
# a stray provisional row surfaces on §10's prominently-displayed quarantine
# list with a reason saying what happened, rather than reading as a message
# that became a ticket.
_PROVISIONAL_DISPOSITION = "quarantined"
_PROVISIONAL_REASON = "processing did not complete"

# A dropped-attachment note is appended to the ticket description or comment
# body, so a filename is attacker-controlled text landing in ticket text.
# Bounded twice: each name to the sanitizer's own 255-char display limit, and
# the whole note to `_MAX_NOTE`, so a mail carrying a hundred junk filenames
# cannot crowd out the words the requester actually wrote.
_MAX_FILENAME = 255
_MAX_NOTE = 1_000

# ---------------------------------------------------------------------------
# Rate limiting (spec §6's second arm)
# ---------------------------------------------------------------------------
#
# THE FAILURE THIS PREVENTS: we notify a requester, their out-of-office
# auto-replies, the reply becomes a comment, the comment notifies them again
# -- and the two systems generate mail until someone notices. Mail loops are
# fast and expensive, and the header checks in `parsing.py` catch only
# auto-repliers that announce themselves (an `Auto-Submitted` header, a
# mailer-daemon address); these two counters catch the ones that don't.
#
# COUNTED FROM `inbound_message`, NEVER FROM AN IN-PROCESS COUNTER. The
# poller runs in Celery workers -- several may run at once against the same
# mailbox -- and a per-process count would both reset on every worker
# restart and disagree with every other worker's count. The table is the
# only thing every worker shares.
#
# TWO INDEPENDENT LIMITS, because they defend against two different shapes
# of loop: a fast one-sender flood (an auto-replier round-tripping with this
# platform's own notifications, many messages per minute) and a slow flood
# on one ticket (a forwarded thread looping between two systems a few times
# an hour -- never fast enough to trip the per-sender limit, but still
# capable of burying one ticket in noise forever).
SENDER_RATE_LIMIT = 10
SENDER_RATE_WINDOW = timedelta(minutes=1)
TICKET_RATE_LIMIT = 20
TICKET_RATE_WINDOW = timedelta(hours=1)

_SENDER_RATE_REASON = (
    f"more than {SENDER_RATE_LIMIT} messages from this sender within {SENDER_RATE_WINDOW}"
)
_TICKET_RATE_REASON = (
    f"more than {TICKET_RATE_LIMIT} inbound messages on this ticket within {TICKET_RATE_WINDOW}"
)

# THE DISPOSITIONS THAT MEAN "THIS TICKET ACTUALLY RECEIVED INBOUND CONTENT".
# `inbound_message.ticket_id` answers "which ticket was this message ABOUT",
# which is not the same question -- `disposition` is the sole authority on
# whether the message landed. A wrong-sender QUARANTINE records the targeted
# ticket for forensics (see `_WRONG_SENDER_REASON`'s call site) and
# contributed nothing to the thread, so it must not count against the
# ticket's budget: counting it let anyone who had been forwarded the thread
# and held the token lock the ticket's own requester out of replying by
# email, indefinitely and silently, by sending quarantined mail at the limit
# rate. That is a third party silencing a requester on their own ticket, on
# this module's primary path.
_TICKET_RATE_DISPOSITIONS = ("commented", "reopened", "refused_rate")


async def _sender_rate_limited(
    session: AsyncSession, org_id: UUID, from_address: str
) -> str | None:
    """§6's per-sender arm: no more than `SENDER_RATE_LIMIT` messages from one
    `From:` address per `SENDER_RATE_WINDOW`, in this org.

    Matching is case-insensitive, the same fold `resolve_sender` uses.
    `_begin_row` has already inserted THIS message's own row (with this
    address) by the time this runs, so the count below already includes it
    -- one query, one comparison, no separate "+1" to keep in sync with the
    insert order.

    EXPLICITLY `org_id`-scoped (invariant #5): RLS makes a cross-org count
    unreachable through the ordinary `org_session` path, but this is a query
    this module owns, and the invariant does not delegate to RLS alone --
    see `test_the_sender_rate_limit_org_predicate_is_load_bearing`, which
    runs on the RLS-bypassing admin engine specifically so this predicate,
    not RLS, is what the test depends on.
    """
    address = from_address.strip().lower()
    if not address:
        return None
    window_start = datetime.now(UTC) - SENDER_RATE_WINDOW
    count = (
        await session.execute(
            select(func.count())
            .select_from(InboundMessage)
            .where(
                InboundMessage.org_id == org_id,
                func.lower(InboundMessage.from_address) == address,
                InboundMessage.created_at >= window_start,
            )
        )
    ).scalar_one()
    return _SENDER_RATE_REASON if count > SENDER_RATE_LIMIT else None


async def _ticket_rate_limited(session: AsyncSession, org_id: UUID, ticket_id: UUID) -> str | None:
    """§6's per-ticket arm: no more than `TICKET_RATE_LIMIT` inbound messages
    landing on one ticket per `TICKET_RATE_WINDOW`.

    Counted from `inbound_message.ticket_id` NARROWED BY `disposition` to
    `_TICKET_RATE_DISPOSITIONS` -- the rows that represent this ticket
    actually receiving inbound content (`commented`, `reopened`, or a
    previous `refused_rate` recorded against it while already over the
    limit). `ticket_id` alone is NOT the right predicate: a wrong-sender
    quarantine also records the targeted ticket (deliberately, for
    forensics) while contributing nothing to it, and counting those rows
    made the limit a denial-of-service weapon against the ticket's own
    requester -- see `_TICKET_RATE_DISPOSITIONS`.

    THIS message is never counted against itself either way: its `ticket_id`
    is not written until after this check runs, and its provisional
    disposition is `quarantined`, which is not in the set.

    EXPLICITLY `org_id`-scoped alongside `ticket_id`, matching the pairing
    every other ticket-scoped query in `service_desk` uses (e.g.
    `tickets.py`'s `Ticket.org_id == org_id, Ticket.id == ticket_id`) even
    though `ticket_id` alone already disambiguates a UUID primary key --
    invariant #5 as a standing discipline, not only where it is the sole
    defence.
    """
    window_start = datetime.now(UTC) - TICKET_RATE_WINDOW
    count = (
        await session.execute(
            select(func.count())
            .select_from(InboundMessage)
            .where(
                InboundMessage.org_id == org_id,
                InboundMessage.ticket_id == ticket_id,
                InboundMessage.disposition.in_(_TICKET_RATE_DISPOSITIONS),
                InboundMessage.created_at >= window_start,
            )
        )
    ).scalar_one()
    return _TICKET_RATE_REASON if count >= TICKET_RATE_LIMIT else None


class AlreadyProcessed(Exception):
    """This `(org_id, message_id)` already has an `inbound_message` row.

    A NORMAL OUTCOME, not an error (spec §4 rule 3): IMAP redelivers, and a
    crash between the ticket write and the `\\Seen` flag is expected rather
    than exceptional. The poller marks the message seen and moves on.

    Because the row is written FIRST, this is raised before any ticket work
    and before any object-storage write has happened -- a redelivery costs
    one failed INSERT and rolls back a transaction that changed nothing.
    """


# ---------------------------------------------------------------------------
# The reopen map (spec §0.3, §8; corrections C1/C2)
# ---------------------------------------------------------------------------

# ONE place derives the reopen target from `(type, status)`. `closed` is in
# `TERMINAL_STATUSES` but deliberately reopenable; `cancelled`/`rejected`
# (`FINAL_STATUSES`) are not here at all and never will be.
_REOPEN_TARGETS: dict[tuple[str, str], str] = {
    ("incident", "resolved"): "open",
    ("incident", "closed"): "open",
    ("request", "fulfilled"): "in_progress",
    ("request", "closed"): "in_progress",
}


def _assert_reopen_map_matches_lifecycle() -> None:
    """Run at import time. `lifecycle.py` is the sole authority on which
    transitions exist (its docstring); this map is a SELECTION from that
    machine, not a second copy of it, and must fail loudly at import rather
    than as a `TicketValidationError` on a live inbound message if the two
    ever drift."""
    for (ticket_type, status), target in _REOPEN_TARGETS.items():
        if not can_transition(ticket_type, status, target):
            raise RuntimeError(  # i18n-exempt: import-time self-check, stops startup
                f"_REOPEN_TARGETS maps {ticket_type} {status!r} -> {target!r}, "
                "which lifecycle.can_transition rejects"
            )


_assert_reopen_map_matches_lifecycle()


def reopen_target(ticket_type: str, status: str) -> str | None:
    """The status an inbound reply reopens `(ticket_type, status)` into, or
    `None` if the ticket needs no reopen (it is already live) or can never be
    reopened (`cancelled`, `rejected`)."""
    return _REOPEN_TARGETS.get((ticket_type, status))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: str, limit: int) -> str:
    return value[:limit]


def _body_or_placeholder(body: str) -> str:
    return _clamp(body.strip() or _EMPTY_BODY_PLACEHOLDER, _MAX_BODY)


def _reopen_anchor(ticket: Ticket) -> datetime:
    """The timestamp the reopen window is measured from (module docstring)."""
    stamp = ticket.closed_at if ticket.status == "closed" else ticket.resolved_at
    anchor = stamp or ticket.updated_at
    if anchor.tzinfo is None:
        # Defensive: the column is `DateTime(timezone=True)`, so this should
        # be unreachable -- but a naive value here would raise `TypeError`
        # mid-message and stop the mailbox for a timezone detail.
        return anchor.replace(tzinfo=UTC)
    return anchor


def _within_reopen_window(ticket: Ticket, mailbox: EmailMailbox) -> bool:
    return datetime.now(UTC) - _reopen_anchor(ticket) <= timedelta(
        days=mailbox.reopen_window_days
    )


async def _notification_sender_loop_reason(
    session: AsyncSession, org_id: UUID, from_address: str
) -> str | None:
    """Spec §6's arm the parser cannot evaluate (module docstring).

    Compares against EVERY email channel's `from_address`, active or not: a
    channel that was switched off yesterday is still the address yesterday's
    notifications went out from, and those can still be auto-replied to
    today. Matching is case-insensitive, the same fold `resolve_sender` uses.
    """
    address = from_address.strip().lower()
    if not address:
        return None
    configs = (
        (
            await session.execute(
                select(NotificationChannel.config).where(
                    NotificationChannel.org_id == org_id,
                    NotificationChannel.kind == "email",
                )
            )
        )
        .scalars()
        .all()
    )
    for config in configs:
        sender = config.get("from_address") if isinstance(config, dict) else None
        if isinstance(sender, str) and sender.strip().lower() == address:
            return "From address matches the org's own notification sender"
    return None


async def _attach_all(
    session: AsyncSession,
    org_id: UUID,
    ticket_id: UUID,
    parsed: ParsedMessage,
    *,
    actor_id: UUID,
    store: ObjectStore,
) -> tuple[str, ...]:
    """Store every attachment through `threads.add_attachment` -- never a
    hand-rolled object key, never a hand-rolled filename check. Task 3
    deliberately does NOT sanitize filenames (it strips NUL/C0 only, as a
    storage hazard); path-traversal sanitization lives in
    `app.core.attachments` and is applied by `add_attachment`.

    Returns the filenames that were DROPPED, in arrival order (empty if all
    of them landed).

    A rejection is SKIPPED, not raised. `sanitize_attachment_filename` is
    ASCII-only by design (a non-Latin name breaks latin-1 response headers),
    so an entirely ordinary "発票.pdf" from a non-Latin locale is rejected.
    Letting that propagate would abort the transaction and leave the message
    to be redelivered and rejected again forever -- one perfectly legitimate
    message stopping a mailbox (§11.10). The words still reach the ticket.

    THE NAMES ARE RETURNED, NOT JUST THE COUNT, because a count is not
    adequate surfacing: `process_message` writes them into the ticket text
    itself. A `reason` counter on a row whose disposition is `created` tells
    the requester nothing (from their side the file arrived), and §10's admin
    UI surfaces the QUARANTINED count prominently -- not this. Naming the
    files in the ticket body reaches both the requester and the technician
    through the channel they are already reading, needs no schema change and
    no new disposition, and tells the requester exactly what to resend.
    """
    dropped: list[str] = []
    for attachment in parsed.attachments:
        try:
            await add_attachment(
                session,
                org_id,
                actor_id,
                ticket_id,
                filename=attachment.filename,
                data=attachment.content,
                store=store,
            )
        except TicketValidationError:
            dropped.append(attachment.filename)
    return tuple(dropped)


def _dropped_attachment_note(dropped: tuple[str, ...]) -> str:
    """The line naming dropped attachments in the ticket text (D5)."""
    names = ", ".join(_clamp(name, _MAX_FILENAME) for name in dropped)
    return _clamp(
        f"[{len(dropped)} attachment(s) could not be stored and were not "
        f"added to this ticket: {names}. Please resend them with plain "
        "ASCII filenames.]",
        _MAX_NOTE,
    )


def _refusal_outcome(refusal: str, refusal_reason: str) -> tuple[str, str]:
    """The `(disposition, reason)` a PARSER refusal records.

    ONE definition, used by both places a refusal is recorded -- the
    unresolvable-sender branch and the tail of `process_message` -- so the
    two can never disagree about what a refusal value means.

    FAIL CLOSED. A refusal value this module does not recognise is still a
    refusal: recording it as `created` would discard the parser's reason and
    report a damaged message as a clean one -- the one thing §2.2 exists to
    prevent. `refused_parse` is the generic "the message did not arrive
    intact" disposition, and the unrecognised value is named in the reason so
    the mismatch between this module and `parsing.py` is visible rather than
    silent.
    """
    if refusal in DISPOSITIONS:
        return refusal, refusal_reason
    return (
        "refused_parse",
        f"unrecognised parser refusal {refusal!r}: {refusal_reason}",
    )


def _with_note(body: str, note: str) -> str:
    """Append `note` to `body` so that the NOTE survives the `_MAX_BODY`
    clamp -- the body is trimmed to make room, never the note. Same reasoning
    as the row-4 citation being prepended rather than appended."""
    return f"{_clamp(body, max(_MAX_BODY - len(note) - 2, 0))}\n\n{note}"


async def _begin_row(
    session: AsyncSession,
    org_id: UUID,
    mailbox: EmailMailbox,
    parsed: ParsedMessage,
) -> UUID:
    """Write the `inbound_message` row FIRST, before any disposition work
    (spec §4 rule 1), and return its id.

    `ON CONFLICT DO NOTHING ... RETURNING id` on
    `uq_inbound_message_org_message_id`: the constraint is the authority on
    "have we seen this", never a check-then-insert. No row returned means a
    redelivery, and `AlreadyProcessed` is raised here -- before a ticket is
    created and before `add_attachment` has PUT anything into object storage,
    which is the whole point of the ordering (module docstring).

    The row lands with `_PROVISIONAL_DISPOSITION` and no `ticket_id`;
    `_finish_row` replaces both inside this same transaction.
    """
    inserted_id = (
        await session.execute(
            pg_insert(InboundMessage)
            .values(
                org_id=org_id,
                mailbox_id=mailbox.id,
                message_id=_clamp(parsed.message_id, _MESSAGE_ID_MAX),
                from_address=_clamp(parsed.from_address, _FROM_ADDRESS_MAX),
                subject=_clamp(parsed.subject, _MESSAGE_SUBJECT_MAX),
                received_at=parsed.received_at,
                disposition=_PROVISIONAL_DISPOSITION,
                reason=_PROVISIONAL_REASON,
                ticket_id=None,
                sender_user_id=None,
                # The PARSED plain text, never the raw RFC822 message
                # (models.py: retaining full raw messages is a privacy
                # liability this product has not asked for).
                body_text=parsed.body_text,
            )
            .on_conflict_do_nothing(constraint="uq_inbound_message_org_message_id")
            .returning(InboundMessage.id)
        )
    ).scalar_one_or_none()

    if inserted_id is None:
        raise AlreadyProcessed(  # i18n-exempt: normal outcome, the poller counts it
            f"message {parsed.message_id!r} has already been processed for this org"
        )
    return inserted_id


async def _finish_row(
    session: AsyncSession,
    row_id: UUID,
    *,
    disposition: str,
    reason: str = "",
    ticket_id: UUID | None = None,
    sender_user_id: UUID | None = None,
) -> InboundMessage:
    """Record the real outcome on the row `_begin_row` inserted, in the same
    transaction as the ticket or comment it describes."""
    if disposition not in DISPOSITIONS:  # pragma: no cover -- defensive
        raise ValueError(  # i18n-exempt: defensive guard, the poller logs the class name
            f"unknown disposition: {disposition!r}"
        )

    # Flush the ORM's pending work (the ticket, the comment, any appended
    # note) so the row's final state and what it describes are one write.
    await session.flush()

    await session.execute(
        update(InboundMessage)
        .where(InboundMessage.id == row_id)
        .values(
            disposition=disposition,
            reason=_clamp(reason, _REASON_MAX),
            ticket_id=ticket_id,
            sender_user_id=sender_user_id,
        )
    )
    return (
        await session.execute(select(InboundMessage).where(InboundMessage.id == row_id))
    ).scalar_one()


async def _category_type(session: AsyncSession, org_id: UUID, category_id: UUID) -> TicketType:
    """The ticket type a new email-created ticket takes: the CONFIGURED
    CATEGORY's type, not a hardcoded `"incident"`.

    `create_ticket` rejects a `type`/`category.type` mismatch as a typed
    error, and Task 2's `mailboxes._resolve_category` does not constrain
    `default_category_id` to incident categories -- so an org-admin who
    points the mailbox at a `request` category would otherwise make every
    inbound message raise, permanently, with no way to tell from the
    mailbox form that they had done it. This adds no rule: the ticket core
    already requires the two to agree; this reads which one the operator
    chose. Org-scoped (invariant #5), and anything unrecognized falls back
    to `"incident"` so a future category type can never widen `TicketType`
    here by accident.
    """
    found = (
        await session.execute(
            select(TicketCategory.type).where(
                TicketCategory.org_id == org_id, TicketCategory.id == category_id
            )
        )
    ).scalar_one_or_none()
    return "request" if found == "request" else "incident"


# ---------------------------------------------------------------------------
# process_message
# ---------------------------------------------------------------------------


async def process_message(
    session: AsyncSession,
    org_id: UUID,
    mailbox: EmailMailbox,
    parsed: ParsedMessage,
    *,
    store: ObjectStore,
) -> InboundMessage:
    """Decide what `parsed` becomes, do it, and record it (spec §8).

    Returns the `inbound_message` row. Raises `AlreadyProcessed` -- and only
    that -- when the message has been seen before; see its docstring for why
    that is a normal outcome.
    """
    # Invariant #5's module-level half. RLS and Task 7's poller both make a
    # cross-org `mailbox` unreachable in practice, but this function takes
    # the tenant and the mailbox as INDEPENDENT arguments and then writes
    # `mailbox_id` and reads `mailbox.default_category_id` -- so the defence
    # here is absent rather than present-and-redundant without this line.
    if mailbox.org_id != org_id:
        raise ValueError(  # i18n-exempt: defensive guard, the poller logs the class name
            f"mailbox {mailbox.id} belongs to org {mailbox.org_id}, not {org_id}"
        )

    # --- §4 rule 1: THE ROW GOES IN FIRST. --------------------------------
    # Everything below happens in the same transaction as this insert. A
    # redelivery collides here and raises `AlreadyProcessed` having done no
    # ticket work and, crucially, no object-storage write (module docstring).
    row_id = await _begin_row(session, org_id, mailbox, parsed)

    # --- §6: loop refusals. No ticket, no comment, ever. -------------------
    if parsed.refusal == "refused_loop":
        return await _finish_row(
            session,
            row_id,
            disposition="refused_loop",
            reason=parsed.refusal_reason,
        )

    loop_reason = await _notification_sender_loop_reason(session, org_id, parsed.from_address)
    if loop_reason is not None:
        return await _finish_row(
            session, row_id, disposition="refused_loop", reason=loop_reason
        )

    # --- §0.2 check 1: the sender must be a user in THIS org. -------------
    sender = await resolve_sender(session, org_id, parsed.from_address)
    if sender.user_id is None:
        # A MESSAGE THAT DID NOT PARSE IS RECORDED AS THE PARSE FAILURE, NOT
        # AS A QUARANTINE. `parse_message` returns `refused_parse` for mail
        # with no `From:`, an unshaped `From:`, or a broken MIME structure --
        # and such a message has, by construction, no resolvable sender, so
        # it would otherwise always land here and be filed as `quarantined`
        # with the parser's reason discarded. `quarantined_count` is the
        # alarm spec §10 designates for "mail a human expected to become a
        # ticket and which did not"; against a published helpdesk address,
        # letting malformed spam land in it would make the number mean
        # nothing. The refusal dispositions are the ones for "the message
        # did not arrive intact", and they are what this records.
        if parsed.refusal is not None:
            disposition, reason = _refusal_outcome(parsed.refusal, parsed.refusal_reason)
            return await _finish_row(session, row_id, disposition=disposition, reason=reason)
        return await _finish_row(
            session, row_id, disposition="quarantined", reason=sender.reason
        )
    sender_user_id = sender.user_id

    # --- §6: the per-sender rate limit. -----------------------------------
    # Runs immediately after sender resolution succeeds -- a rate-limited
    # message must still be attributed to its sender on the row -- and
    # before ANY ticket write, whether or not the message carries a valid
    # token: a flooding sender is refused whether it would have created a
    # ticket or commented on one.
    sender_rate_reason = await _sender_rate_limited(session, org_id, parsed.from_address)
    if sender_rate_reason is not None:
        return await _finish_row(
            session,
            row_id,
            disposition="refused_rate",
            reason=sender_rate_reason,
            sender_user_id=sender_user_id,
        )

    # --- §0.2 check 2: the token identifies a ticket, and only that. ------
    outcome: str | None = None
    ticket_id: UUID | None = None
    cited_number: str | None = None
    comment: TicketComment | None = None
    new_ticket: Ticket | None = None

    token = extract_reply_token(parsed.recipients)
    if token is not None:
        ticket = await resolve_ticket_by_token(session, org_id, token)
        if ticket is not None:
            # THE CENTRAL CLAIM (§0.2). Possession of the token is not
            # authority -- it travels with every forward of the thread.
            # Quarantine: no comment, no reopen, and no new ticket either.
            if ticket.requester_id != sender_user_id:
                return await _finish_row(
                    session,
                    row_id,
                    disposition="quarantined",
                    reason=_WRONG_SENDER_REASON,
                    # The TARGETED ticket, recorded so an operator asking
                    # "did somebody try to post into ACME-123 as someone
                    # else" can answer it with a query rather than by reading
                    # every quarantined row. `ticket_id` here does NOT mean
                    # "the message landed there" -- `disposition` is the sole
                    # authority on that, and `quarantined` means it did not.
                    # `_ticket_rate_limited` reads it that way too: this row
                    # is NOT counted against the ticket's inbound budget, or
                    # holding the token would be enough to lock the ticket's
                    # own requester out (see `_TICKET_RATE_DISPOSITIONS`).
                    ticket_id=ticket.id,
                    sender_user_id=sender_user_id,
                )

            target = reopen_target(ticket.type, ticket.status)
            if target is None and ticket.status not in TERMINAL_STATUSES:
                # --- §6: the per-ticket rate limit. -----------------------
                # A steady trickle of replies from the legitimate requester
                # -- never fast enough to trip the per-sender limit above --
                # must still not bury one ticket in comments forever.
                ticket_rate_reason = await _ticket_rate_limited(session, org_id, ticket.id)
                if ticket_rate_reason is not None:
                    return await _finish_row(
                        session,
                        row_id,
                        disposition="refused_rate",
                        reason=ticket_rate_reason,
                        ticket_id=ticket.id,
                        sender_user_id=sender_user_id,
                    )
                # Already live: comment, no transition (§8 row 2).
                comment = await add_comment(
                    session,
                    org_id,
                    ticket.id,
                    body=_body_or_placeholder(parsed.body_text),
                    author_id=sender_user_id,
                    # §11.8: ALWAYS public. An internal note is not
                    # expressible from email, by construction.
                    visibility="public",
                )
                outcome, ticket_id = "commented", ticket.id
            elif target is not None and _within_reopen_window(ticket, mailbox):
                # --- §6: the per-ticket rate limit, same reasoning as above.
                ticket_rate_reason = await _ticket_rate_limited(session, org_id, ticket.id)
                if ticket_rate_reason is not None:
                    return await _finish_row(
                        session,
                        row_id,
                        disposition="refused_rate",
                        reason=ticket_rate_reason,
                        ticket_id=ticket.id,
                        sender_user_id=sender_user_id,
                    )
                # REOPEN FIRST (C1): `closed` is terminal for comments, so
                # `add_comment` would reject it in the other order.
                await change_status(
                    session,
                    org_id,
                    ticket.id,
                    to_status=target,
                    actor_id=sender_user_id,
                    reason="reopened by an inbound email reply",
                )
                comment = await add_comment(
                    session,
                    org_id,
                    ticket.id,
                    body=_body_or_placeholder(parsed.body_text),
                    author_id=sender_user_id,
                    visibility="public",
                )
                outcome, ticket_id = "reopened", ticket.id
            else:
                # Beyond the window (§8 row 4), or `cancelled`/`rejected`:
                # no reopen edge exists and the ticket accepts no comments,
                # so the reply takes row 4's route rather than raising. The
                # old number is cited in TEXT -- parent/child linking is
                # explicitly not sequenced (service-desk §14).
                cited_number = ticket.number

    # --- Creation. --------------------------------------------------------
    if outcome is None:
        body = _body_or_placeholder(parsed.body_text)
        if cited_number is not None:
            # The citation goes FIRST so the `_MAX_BODY` clamp below can
            # never truncate it away.
            body = _clamp(
                f"Continues ticket {cited_number}, which is no longer open.\n\n{body}",
                _MAX_BODY,
            )
        # The sender's client when the sender is a contact, else the internal
        # client (whose id is the organisation's).
        # plan 2026-10-26 Task 4 decides by domain and Unassigned (spec §3.3)
        sender_kind, sender_client = (
            await session.execute(
                select(User.kind, User.client_id).where(
                    User.org_id == org_id, User.id == sender_user_id
                )
            )
        ).one()
        ticket_client = (
            sender_client if sender_kind == "contact" and sender_client is not None else org_id
        )
        try:
            # The refusal is stored in the row's `reason`: English, whatever the locale.
            with use_locale("en"):
                new_ticket = await create_ticket(
                    session,
                    org_id,
                    sender_user_id,
                    TicketCreate(
                        type=await _category_type(session, org_id, mailbox.default_category_id),
                        category_id=mailbox.default_category_id,
                        subject=_clamp(parsed.subject.strip() or "(no subject)", _MAX_SUBJECT),
                        description=body,
                        requester_id=sender_user_id,
                    ),
                    origin="email",
                    client_id=ticket_client,
                )
        except TicketValidationError as exc:
            # A MISCONFIGURED MAILBOX, NOT A BAD MESSAGE -- the commonest
            # case being a `default_category_id` whose category has no
            # `default_queue_id`, which `create_ticket` refuses outright.
            # Letting this propagate wrote NO row at all: the poller could
            # not mark `\Seen`, the message was refetched and failed
            # identically every interval, and the operator saw nothing --
            # §2.2's "every fetched message gets a row" broken in exactly the
            # way §11.10 forbids, with the diagnosis surface empty.
            #
            # `quarantined` is the disposition, chosen from the existing
            # seven (a new value would need a migration and a change to
            # `ck_inbound_message_disposition_valid`). It is the right one on
            # the merits: quarantine means "held, unactioned, a human must
            # look at it", which is exactly the state -- and §10 surfaces the
            # quarantined count prominently, so a mailbox pointed at a
            # queue-less category announces itself instead of going quiet.
            # `refused_parse` would have been wrong: the message parsed fine.
            return await _finish_row(
                session,
                row_id,
                disposition="quarantined",
                reason=f"ticket creation refused: {exc}",
                sender_user_id=sender_user_id,
            )
        outcome, ticket_id = "created", new_ticket.id

    # --- Attachments. -----------------------------------------------------
    # Only for an accepted message: spec §7 is explicit that a bounds
    # violation keeps `body_text` and drops the attachments, and `parsing.py`
    # already returns `attachments=()` in that case. This is the second belt.
    dropped: tuple[str, ...] = ()
    if parsed.refusal is None and parsed.attachments and ticket_id is not None:
        dropped = await _attach_all(
            session, org_id, ticket_id, parsed, actor_id=sender_user_id, store=store
        )

    # D5's surfacing: NAME the dropped files in the ticket text, the same
    # channel the empty-body placeholder and the row-4 citation already use.
    # A counter on the row reaches neither the requester (who has no reason
    # to think anything was lost) nor §10's quarantine list (this row says
    # `created`); a line in the thread reaches both, and says what to resend.
    attachment_reason = ""
    if dropped:
        note = _dropped_attachment_note(dropped)
        if comment is not None:
            comment.body = _with_note(comment.body, note)
        elif new_ticket is not None:
            new_ticket.description = _with_note(new_ticket.description or "", note)
        # The counter stays on the row too: it is what an operator scanning
        # the message list sorts and filters on.
        attachment_reason = f"{len(dropped)} attachment(s) dropped: unusable filename or size"

    # --- The row. ---------------------------------------------------------
    # A refusal that still produced a ticket (spec §7's oversized message,
    # whose words must still reach the ticket) is recorded as the REFUSAL:
    # the disposition column answers "why did my email not arrive intact",
    # and `ticket_id` on the same row already answers "where did it land".
    if parsed.refusal is not None:
        # Same mapping the unresolvable-sender branch above uses, including
        # its fail-closed arm -- see `_refusal_outcome`.
        disposition, reason = _refusal_outcome(parsed.refusal, parsed.refusal_reason)
    else:
        disposition, reason = outcome, attachment_reason

    return await _finish_row(
        session,
        row_id,
        disposition=disposition,
        reason=reason,
        ticket_id=ticket_id,
        sender_user_id=sender_user_id,
    )
