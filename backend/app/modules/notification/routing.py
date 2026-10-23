"""Event-to-recipients resolution (docs/modules/notification/spec.md §6).

PURE RESOLUTION LOGIC. This module performs no delivery and opens no network
connection -- it reads `notification_rules`, `queue_members`, and `users`,
and produces a deduplicated list of `Recipient`s. Delivery (rendering,
sending, retry, dead-lettering) is Task 9, `app/modules/notification/delivery.py`.

THE EVENT PAYLOAD CONTRACT (Task 12 -- ticket core -- must publish events
shaped exactly like this; see `test_notification_routing.py`'s module
docstring for the full rationale and worked examples):

    queue_id:      str(UUID) | None -- the ticket's queue. Used BOTH to match
                   a rule's own `queue_id` scope (§6.1: NULL means "any
                   queue") and, when a matching rule's `queue_id` is itself
                   NULL, to know WHICH queue's membership to expand for
                   `recipient_kind="queue_members"` -- there is no other
                   candidate queue in that case.
    assignee_id:   str(UUID) | None -- for `recipient_kind="assignee"`.
    requester_id:  str(UUID) | None -- for `recipient_kind="requester"`, AND
                   read for EVERY event type regardless of whether any rule
                   actually uses `recipient_kind="requester"` -- see
                   "THE INTERNAL-NOTE GUARD" below for why this one field is
                   read unconditionally.

    Every id is a STRING (`str(uuid)`), never a bare `UUID` object -- that is
    what a real `domain_events.payload` JSONB column holds once
    `app.core.events.publish` writes it (a bare `UUID` is not
    JSON-serializable). `_uuid_or_none` below accepts either shape so a
    caller that already parsed its own payload does not have to re-stringify
    it, but the contract Task 12 must satisfy is: strings.

    A `ticket.internal_note_added` event's payload carries NEITHER `body`
    NOR anything else that discloses the note's text (spec §6.4 mechanism
    1) -- only the fact (`subject_type`/`subject_id`, already on
    `DomainEvent` itself), the author (`actor_user_id`, likewise already a
    `DomainEvent` column), and whatever routing-only ids this contract
    lists.

THE INTERNAL-NOTE GUARD (spec §6.4, mechanism 2's last line of defence).
Three earlier layers already make a rule pairing
`event_type="ticket.internal_note_added"` with `recipient_kind="requester"`
unrepresentable: `RuleCreate`'s Pydantic validator, `create_rule`'s own
unconditional service-layer check, and the DB's
`ck_rule_no_internal_to_requester` CHECK constraint (all in `rules.py` /
`schema.py` / `models.py`, Task 7). Because that pairing cannot exist as a
row, this module's guard is NOT "skip rows with that pairing" -- there are
none to skip. It is broader and independent of any specific rule: the
requester (`payload["requester_id"]`) is a legitimate member of the SAME
queue their own ticket belongs to often enough (spec §0.4 -- "a requester is
an ordinary users row"; nothing stops a staff member who files their own
ticket from also being a member of that ticket's queue), so a plain,
correctly-written `recipient_kind="queue_members"` rule can resolve them as
a candidate through a path that has nothing to do with the forbidden
pairing. `resolve_recipients` therefore drops the requester from the
resolved set for `ticket.internal_note_added` UNCONDITIONALLY, regardless of
which `recipient_kind` produced the candidate -- this is what makes it a
LAST line rather than a restatement of the write-time check.

THE ORG RE-CHECK (spec §6.3). Every user-shaped candidate (`assignee`,
`requester`, `queue_members`) is resolved through a query that filters
`User.org_id == event.org_id` explicitly, in addition to whatever
`queue_members`/rule scoping already narrowed it to. Queue membership is
already org-scoped when written through `rules.add_queue_member` (its own
`_resolve_user` rejects a cross-org user id at write time) -- but a
`queue_members` row can still exist with a foreign `user_id` if it was ever
written by a path that skips that check (direct SQL, a future bug, a
BYPASSRLS admin action) -- see `test_recipients_outside_the_event_org_are_dropped`,
which seeds exactly that row. This re-check is what catches it here, the
last point before a `Recipient` is produced.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DomainEvent, NotificationChannel, NotificationRule, QueueMember, User

# Spec §6.4: internal notes have their own event type precisely so this
# module boundary check is possible at all -- see app/core/events.py.
_INTERNAL_NOTE_EVENT_TYPE = "ticket.internal_note_added"


@dataclass(frozen=True)
class Recipient:
    """One resolved delivery target: a channel and the address to reach it
    at (a user's email for `assignee`/`requester`/`queue_members`, or the
    channel's own configured destination for `channel_default`)."""

    channel_id: UUID
    address: str


def _uuid_or_none(value: object) -> UUID | None:
    """Accepts either a `str(uuid)` (the real payload contract -- see this
    module's docstring) or a bare `UUID` (convenient for callers that already
    parsed their own payload, e.g. this module's own tests). `None` and a
    missing key both mean "not present"."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _channel_default_address(channel: NotificationChannel) -> str:
    """The `recipient_kind="channel_default"` target: the channel's OWN
    configured destination, not a user -- spec §2.3: "a webhook channel uses
    `channel_default` ... and ignores the rest." No org re-check applies
    here; there is no user to re-check."""
    if channel.kind == "webhook":
        return str(channel.config.get("url", ""))
    return str(channel.config.get("from_address", ""))


async def _resolve_single_user(
    session: AsyncSession, *, org_id: UUID, user_id: UUID | None
) -> list[tuple[UUID, str]]:
    """Resolve one `assignee_id`/`requester_id`-shaped candidate, org-scoped
    (spec §6.3). Returns at most one `(user_id, email)` pair; empty if the id
    is absent or does not resolve inside THIS org."""
    if user_id is None:
        return []
    row = (
        await session.execute(
            select(User.id, User.email).where(User.id == user_id, User.org_id == org_id)
        )
    ).one_or_none()
    if row is None:
        return []
    return [(row.id, row.email)]


async def _resolve_queue_members(
    session: AsyncSession, *, org_id: UUID, queue_id: UUID | None
) -> list[tuple[UUID, str]]:
    """Resolve every member of `queue_id`, org-re-checked (spec §6.3): the
    join filters `User.org_id == org_id` explicitly, independent of whatever
    `queue_members.org_id` already says, so a `queue_members` row whose
    `user_id` belongs to a different org (however it got there) is dropped
    here rather than trusted."""
    if queue_id is None:
        return []
    rows = (
        await session.execute(
            select(User.id, User.email)
            .select_from(QueueMember)
            .join(User, User.id == QueueMember.user_id)
            .where(
                QueueMember.queue_id == queue_id,
                # Whole-branch review finding M5: this table's own `org_id`
                # is defense-in-depth, in addition to the `User.org_id`
                # re-check the docstring's "THE ORG RE-CHECK" section already
                # describes -- both queries run inside `org_session` (RLS
                # scopes them today either way), but this module's own
                # discipline (every query carries an explicit `org_id`
                # predicate) should not rely on RLS alone.
                QueueMember.org_id == org_id,
                User.org_id == org_id,
            )
        )
    ).all()
    return [(row.id, row.email) for row in rows]


async def _expand_rule(
    session: AsyncSession,
    *,
    rule: NotificationRule,
    channel: NotificationChannel,
    org_id: UUID,
    event_queue_id: UUID | None,
    assignee_id: UUID | None,
    requester_id: UUID | None,
) -> list[tuple[UUID | None, str]]:
    """Expand one rule's `recipient_kind` into `(user_id, address)`
    candidates. `user_id` is `None` for `channel_default` (there is no user
    behind a webhook's own destination) -- carried through only so the
    internal-note guard in `resolve_recipients` has something uniform to
    compare against `requester_id`."""
    if rule.recipient_kind == "assignee":
        return list(await _resolve_single_user(session, org_id=org_id, user_id=assignee_id))
    if rule.recipient_kind == "requester":
        return list(await _resolve_single_user(session, org_id=org_id, user_id=requester_id))
    if rule.recipient_kind == "queue_members":
        # NULL rule.queue_id means "any queue" (spec §2.3) -- the queue whose
        # membership actually gets expanded is always the EVENT's own queue;
        # a specific rule.queue_id only narrows which rules match at all
        # (the query in resolve_recipients), never which queue's members.
        effective_queue_id = rule.queue_id if rule.queue_id is not None else event_queue_id
        return list(
            await _resolve_queue_members(session, org_id=org_id, queue_id=effective_queue_id)
        )
    # "channel_default"
    return [(None, _channel_default_address(channel))]


async def resolve_recipients(session: AsyncSession, *, event: DomainEvent) -> list[Recipient]:
    """Resolve a `DomainEvent` into the deduplicated list of recipients that
    should be notified (spec §6.1-§6.4).

    Steps, in order:

    1. Select ACTIVE rules for `(event.org_id, event.event_type)` whose
       `queue_id` is NULL or matches `event.payload["queue_id"]` (§6.1,
       §6.2 -- inactive rules are skipped; zero matching rules is a valid
       state and returns `[]`, not an error).
    2. Expand each rule's `recipient_kind` into `(user_id, address)`
       candidates, every user-shaped one re-checked against `event.org_id`
       (§6.3 -- see `_resolve_single_user`/`_resolve_queue_members`).
    3. Drop the requester from the result UNCONDITIONALLY when
       `event.event_type == "ticket.internal_note_added"` (§6.4 -- see this
       module's "THE INTERNAL-NOTE GUARD" docstring section).
    4. Deduplicate on `(channel_id, address)` (§6.1) and return.
    """
    payload = event.payload
    event_queue_id = _uuid_or_none(payload.get("queue_id"))
    assignee_id = _uuid_or_none(payload.get("assignee_id"))
    requester_id = _uuid_or_none(payload.get("requester_id"))

    rule_query = select(NotificationRule).where(
        NotificationRule.org_id == event.org_id,
        NotificationRule.event_type == event.event_type,
        NotificationRule.active.is_(True),
    )
    if event_queue_id is not None:
        rule_query = rule_query.where(
            NotificationRule.queue_id.is_(None) | (NotificationRule.queue_id == event_queue_id)
        )
    else:
        # No queue on this event at all -- only queue-agnostic rules can
        # possibly apply; a rule scoped to a specific queue has nothing to
        # match against.
        rule_query = rule_query.where(NotificationRule.queue_id.is_(None))
    rule_query = rule_query.order_by(NotificationRule.created_at, NotificationRule.id)

    rules = list((await session.execute(rule_query)).scalars().all())
    if not rules:
        return []

    channel_ids = {rule.channel_id for rule in rules}
    channels = {
        channel.id: channel
        for channel in (
            await session.execute(
                select(NotificationChannel).where(
                    # Whole-branch review finding M5: `channel_ids` only ever
                    # comes from `rules`, already filtered on
                    # `NotificationRule.org_id == event.org_id` above, so this
                    # predicate is defense in depth (RLS via `org_session`
                    # already scopes this query today) rather than a fix for
                    # a reachable leak -- but this module's own docstring
                    # states "an explicit org_id predicate on every query",
                    # and this was the one query that relied on RLS alone.
                    NotificationChannel.org_id == event.org_id,
                    NotificationChannel.id.in_(channel_ids),
                )
            )
        )
        .scalars()
        .all()
    }

    is_internal_note = event.event_type == _INTERNAL_NOTE_EVENT_TYPE

    seen: set[tuple[UUID, str]] = set()
    recipients: list[Recipient] = []
    for rule in rules:
        channel = channels.get(rule.channel_id)
        if channel is None:
            continue
        candidates = await _expand_rule(
            session,
            rule=rule,
            channel=channel,
            org_id=event.org_id,
            event_queue_id=event_queue_id,
            assignee_id=assignee_id,
            requester_id=requester_id,
        )
        for user_id, address in candidates:
            if is_internal_note and requester_id is not None and user_id == requester_id:
                # Spec §6.4, last line of defence -- see this module's
                # "THE INTERNAL-NOTE GUARD" docstring section.
                continue
            key = (channel.id, address)
            if key in seen:
                continue
            seen.add(key)
            recipients.append(Recipient(channel_id=channel.id, address=address))

    return recipients
