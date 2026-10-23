"""The inbound-message log: spec §10's diagnosis surface.

Not a route-local query. `api.py` resolves the principal, calls this, and maps
errors; the filtering and -- more importantly -- the semantics of the
quarantined count live here, where they can be reasoned about and tested
directly.

**THE TIME FILTER AND THE SORT ORDER USE `created_at`, NOT `received_at`.**
`received_at` is the sender's `Date:` header (`parsing._extract_received_at`),
falling back to `now()` only when it is absent or unparseable -- so it is
ATTACKER-CHOSEN, by anyone who can send mail to a monitored mailbox. Using it
to decide visibility hands that decision to the sender:

* dated 2001, the message is absent from the operator's "last 24 hours" view
  AND missing from that view's quarantined count -- the alarm goes quiet for
  precisely the message that was buried;
* dated 2099, the message owns row 1 of every page, forever, pushing real
  mail off the front of the operator's only diagnosis surface.

Quarantined mail is what an operator most needs to see, so neither is
acceptable. `created_at` is when THIS PLATFORM wrote the row, which no sender
influences, and it is what `since`/`until` and the ordering both use.
`received_at` remains in the response -- it is genuinely useful, and a wild
value is itself diagnostic -- but it decides nothing. Ordering ties break on
`id`, which is a uuid7 and therefore monotonic in insertion order.

**THE QUARANTINED COUNT IS A COUNT, NOT A PAGE TALLY.** Quarantined mail is
mail a human expected to become a ticket and which did not (spec §10, and the
`quarantined` disposition Task 5 records for a queue-less default category).
Counting the rows in the returned page would make the number silently depend
on `limit`/`offset`: a mailbox with a hundred quarantined messages would
report "50" on page one and "0" on page three, and the alert would be worth
nothing. It is a `SELECT count(*)` over the whole scope.

**`disposition` NARROWS THE LIST BUT NOT THE QUARANTINED COUNT** -- a
deliberate asymmetry, and the one judgement call in this file. `mailbox_id`
and the time range are SCOPE: they say which mail the operator is looking at,
and the quarantined count should describe exactly that scope, so they apply to
both. `disposition` is a VIEW over that scope, and applying it to the count
makes the count degenerate -- `?disposition=created` would report
`quarantined_count: 0`, which is true of the filtered rows and useless as the
number spec §10 asks to be surfaced prominently. Worse, it reads as "nothing
is quarantined" at precisely the moment the operator narrowed the view to
look at something else. So the count answers one fixed question -- "how much
mail in this scope needs a human?" -- regardless of which disposition is
currently being listed.

`total` is the OTHER number and obeys the opposite rule: it is the extent of
the LIST -- scope and `disposition` both -- because its only job is to let a
caller page through what it is looking at. Two counts with two different
predicates is deliberate; one number cannot answer both questions.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.modules.email_intake.models import InboundMessage
from app.modules.email_intake.schema import Disposition, InboundMessageOut, MessageLogOut

# The page bound. Matches the ticket core's own list endpoints (`le=200`);
# an unbounded message log is an unbounded response on a table that grows
# with every message the platform ever received.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# THE LIST SERVES AN EXCERPT OF `body_text`, NOT THE WHOLE BODY, AND THIS IS
# WHAT BOUNDS THE RESPONSE. `parsing._MAX_BODY_CHARS` caps a stored body at
# 1 MiB, and anyone who can send mail to a monitored mailbox chooses that
# size -- a quarantined row carries a body too, so it does not even have to
# become a ticket. At `MAX_LIMIT` that is a ~200 MB list response, both as
# ORM rows and again as serialised JSON, on request by any
# `email_intake.read` holder. 4 KiB x 200 rows bounds a full page at under
# 1 MB of body text instead.
#
# Spec §10 requires `body_text` to be AVAILABLE to readers, which it is: this
# is a triage surface, and the first few dozen lines are what tell an
# operator why a message quarantined (an unknown sender, a queue-less
# category, an auto-reply header). `body_truncated` says when there is more,
# so the excerpt never masquerades as the whole message. Serving the full
# body would need a per-message detail route, which is an amendment to §10's
# route table rather than something to smuggle in under a review fix; it is
# the right follow-up if operators find the excerpt too short.
BODY_EXCERPT_CHARS = 4096

QUARANTINED = "quarantined"


def _scope(
    org_id: UUID,
    *,
    mailbox_id: UUID | None,
    since: datetime | None,
    until: datetime | None,
) -> list[ColumnElement[bool]]:
    """The SCOPE predicates -- which mail is being looked at. Shared by the
    list and both counts, deliberately (module docstring). `org_id` is
    included even though RLS already constrains every statement in an
    `org_session`: belt and braces, the same discipline every other service
    here follows.

    `since`/`until` bound `created_at`, NOT `received_at` -- see the module
    docstring. A sender must not be able to choose whether their message
    appears in the operator's window.
    """
    where: list[ColumnElement[bool]] = [InboundMessage.org_id == org_id]
    if mailbox_id is not None:
        where.append(InboundMessage.mailbox_id == mailbox_id)
    if since is not None:
        where.append(InboundMessage.created_at >= since)
    if until is not None:
        where.append(InboundMessage.created_at <= until)
    return where


def _excerpt(body: str | None) -> tuple[str | None, bool]:
    """`(what to serve, whether it was cut)`. See `BODY_EXCERPT_CHARS`."""
    if body is None or len(body) <= BODY_EXCERPT_CHARS:
        return body, False
    return body[:BODY_EXCERPT_CHARS], True


def _message_out(row: InboundMessage) -> InboundMessageOut:
    """No `org_id`, no raw RFC822, nothing credential-shaped -- `body_text` is
    the PARSED text and is deliberately exposed to `email_intake.read`
    holders (spec §10): it is how an operator diagnoses a quarantined
    message. It is served as an excerpt; see `BODY_EXCERPT_CHARS`.

    `disposition` is passed through as whatever the column holds. The check
    constraint `ck_inbound_message_disposition_valid` is the authority for
    the vocabulary and `InboundMessageOut.disposition` is therefore typed
    `str` rather than the `Disposition` literal -- ON PURPOSE. Validating it
    here would mean that one row outside the vocabulary (a migration defect,
    a hand-edited row) raises and takes out the WHOLE PAGE, i.e. destroys the
    operator's only diagnosis surface at exactly the moment something is
    wrong with the data. A message that cannot be described is the one an
    operator most needs to see, so it is shown, not hidden and not fatal.

    Both timestamps are surfaced: `created_at` is when this platform recorded
    the message and is what the list is ordered and filtered by; `received_at`
    is the sender's own `Date:` header, shown so that a wild value is visible
    AS a wild value (module docstring).
    """
    body_text, body_truncated = _excerpt(row.body_text)
    return InboundMessageOut(
        id=row.id,
        mailbox_id=row.mailbox_id,
        message_id=row.message_id,
        from_address=row.from_address,
        subject=row.subject,
        received_at=row.received_at,
        created_at=row.created_at,
        disposition=row.disposition,
        reason=row.reason,
        ticket_id=row.ticket_id,
        body_text=body_text,
        body_truncated=body_truncated,
    )


async def list_messages(
    session: AsyncSession,
    org_id: UUID,
    *,
    mailbox_id: UUID | None = None,
    disposition: Disposition | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> MessageLogOut:
    """One page of the disposition log, newest RECORDED first, plus the
    extent of the filtered list (`total`) and the quarantined count over the
    whole scope (module docstring)."""
    scope = _scope(org_id, mailbox_id=mailbox_id, since=since, until=until)
    listed: list[ColumnElement[bool]] = list(scope)
    if disposition is not None:
        listed.append(InboundMessage.disposition == disposition)

    rows = (
        (
            await session.execute(
                select(InboundMessage)
                .where(*listed)
                # `created_at`, never `received_at`: the sender chooses the
                # latter (module docstring).
                .order_by(InboundMessage.created_at.desc(), InboundMessage.id.desc())
                .limit(min(limit, MAX_LIMIT))
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    total = (
        await session.execute(select(func.count()).select_from(InboundMessage).where(*listed))
    ).scalar_one()

    quarantined = (
        await session.execute(
            select(func.count())
            .select_from(InboundMessage)
            .where(*scope, InboundMessage.disposition == QUARANTINED)
        )
    ).scalar_one()

    return MessageLogOut(
        messages=[_message_out(row) for row in rows],
        total=int(total),
        quarantined_count=int(quarantined),
    )
