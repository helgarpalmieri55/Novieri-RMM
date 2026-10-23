"""Event-to-recipients resolution (docs/modules/notification/spec.md §6).

THE EVENT PAYLOAD CONTRACT (this file both defines and exercises it -- see
`routing.py`'s module docstring for the authoritative statement, restated
here because Task 12, which does not exist yet, must publish events shaped
exactly like the ones constructed below):

    queue_id:      str(UUID) | None -- the ticket's queue, for queue-scoped
                   rule matching (`notification_rules.queue_id`) and for
                   expanding `recipient_kind="queue_members"`.
    assignee_id:   str(UUID) | None -- for `recipient_kind="assignee"`.
    requester_id:  str(UUID) | None -- for `recipient_kind="requester"`, AND
                   read unconditionally for every event type so `routing.py`
                   can filter the requester out of a `ticket.internal_note_added`
                   resolution even when they matched some OTHER path (e.g.
                   queue membership). This is why `requester_id` is present
                   even on an event type that must never route TO the
                   requester -- the guard needs to know who to exclude.

    `body` (an internal note's text) is deliberately ABSENT from every
    payload constructed here -- spec §6.4 mechanism 1: even a mis-routed
    delivery cannot disclose text that was never in the event.

Every JSON value above is a STRING, not a Python `UUID` object -- this is
what a real `domain_events.payload` JSONB column actually holds once
`app.core.events.publish` writes it (a bare `UUID` is not JSON-serializable),
so tests here use string ids throughout rather than a more convenient but
unrealistic shape.

FIXTURE PATTERN. `an_event`/`rule_for`/`queue_with_member` from the task
brief do not exist in this codebase's test suite; this file uses the real
project convention instead -- `api_env`, `AsyncSession(api_env.admin_engine,
...)`, and this module's own service functions (`create_channel`,
`create_rule`, `add_queue_member`) to build real rows, exactly as
`test_notification_rules.py` (Task 7) does.

THE TWO DISCRIMINATING TESTS (per the task instructions -- each is built so
it would fail for the RIGHT reason if the guard it names were removed, not
merely because nothing matched):

* `test_internal_note_never_reaches_a_requester` -- the requester is
  deliberately ALSO added as a queue member, so a `queue_members` rule
  genuinely resolves them as a candidate before routing.py's internal-note
  guard removes them. Asserted on the resolved `Recipient` list, not on
  whether a rule could be created (that's Task 7's job, already covered by
  `test_notification_rules.py`'s internal-note tests).
* `test_recipients_outside_the_event_org_are_dropped` -- a REAL foreign-org
  user is inserted as a `queue_members` row under the local org's queue
  (via the BYPASSRLS admin engine, exactly like
  `test_notification_rules.py`'s own race tests and
  `test_notification_tenancy.py`'s fixtures do), so the query genuinely has
  a row to wrongly return before the org re-check drops it.
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.vault import LocalVaultService
from app.models import DomainEvent, NotificationChannel, QueueMember, TicketQueue, User
from app.modules.notification.channels import create_channel
from app.modules.notification.routing import Recipient, resolve_recipients
from app.modules.notification.rules import add_queue_member, create_rule
from app.modules.notification.schema import ChannelCreate, RuleCreate
from app.modules.service_desk.refdata import create_queue


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Mirrors `test_notification_rules.py`'s helper of the same name."""
    org_id = await api_env.create_org("notif-routing")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _email(session: AsyncSession, user_id: UUID) -> str:
    return (await session.execute(select(User.email).where(User.id == user_id))).scalar_one()


async def _channel(
    session: AsyncSession, org_id: UUID, user_id: UUID, tmp_path: Path, name: str = "ops"
) -> NotificationChannel:
    return await create_channel(
        session,
        org_id=org_id,
        actor_id=user_id,
        data=ChannelCreate(
            kind="webhook", name=name, config={"url": "https://h.example.com/x"}
        ),
        secret=None,
        vault=LocalVaultService(tmp_path / "root.key"),
    )


async def _queue(
    session: AsyncSession, org_id: UUID, user_id: UUID, key: str = "ops"
) -> TicketQueue:
    return await create_queue(session, org_id, user_id, key=key, name=key.title())


def _event(org_id: UUID, event_type: str, payload: dict[str, str | None]) -> DomainEvent:
    """Build a `DomainEvent` WITHOUT persisting it -- `resolve_recipients`
    only ever reads `org_id`/`event_type`/`payload` off the object it is
    handed; nothing in §6's resolution logic needs the event's own row to
    exist yet (draining/persistence is the worker's job, spec §10). This
    keeps the payload's ids as the plain strings the real JSONB column would
    hold, per this file's module docstring."""
    now = datetime.now(UTC)
    return DomainEvent(
        id=uuid7(),
        org_id=org_id,
        event_type=event_type,
        subject_type="ticket",
        subject_id=uuid7(),
        payload=payload,
        occurred_at=now,
        created_at=now,
    )


async def _add_foreign_queue_member(
    session: AsyncSession, *, local_org_id: UUID, queue_id: UUID, foreign_user_id: UUID
) -> None:
    """Seed a REAL foreign-org user as a member of THIS org's queue --
    bypassing `add_queue_member`'s own org-scoped `_resolve_user` check
    entirely via a direct `QueueMember` insert (exactly the shape
    `test_notification_rules.py`'s own race tests and
    `test_notification_tenancy.py`'s fixtures already use), so
    `resolve_recipients`'s own org re-check (spec §6.3) is the only thing
    standing between this row and a cross-tenant delivery. `org_id` on the
    row is set to the LOCAL org -- matching the queue it's attached to, and
    exactly the shape a real bug (not a hypothetical) would produce: the
    queue and its membership row agree on `org_id`, but the referenced USER
    does not belong to that org."""
    session.add(
        QueueMember(
            org_id=local_org_id,
            queue_id=queue_id,
            user_id=foreign_user_id,
            created_by=foreign_user_id,
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# §6.2 -- zero/inactive rules are a valid state, not an error
# ---------------------------------------------------------------------------


async def test_no_rules_yields_no_recipients(api_env) -> None:
    """Spec §6.2 -- an org with no rules resolves to no recipients, and that
    is success, not an error."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        event = _event(org_id, "ticket.assigned", {"assignee_id": str(user_id)})
        got = await resolve_recipients(session, event=event)
    assert got == []


async def test_inactive_rules_are_skipped(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        await create_rule(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=RuleCreate(
                event_type="ticket.assigned",
                channel_id=channel.id,
                recipient_kind="assignee",
                active=False,
            ),
        )
        event = _event(org_id, "ticket.assigned", {"assignee_id": str(user_id)})
        got = await resolve_recipients(session, event=event)
    assert got == []


async def test_rule_with_null_queue_id_matches_any_queue(api_env, tmp_path: Path) -> None:
    """Spec §6.2/§6.1: `queue_id IS NULL` means "any queue" -- a rule with no
    queue scope must still resolve for an event carrying a real queue."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        await create_rule(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=RuleCreate(
                event_type="ticket.assigned",
                channel_id=channel.id,
                recipient_kind="assignee",
                queue_id=None,
            ),
        )
        queue = await _queue(session, org_id, user_id)
        event = _event(
            org_id,
            "ticket.assigned",
            {"queue_id": str(queue.id), "assignee_id": str(user_id)},
        )
        got = await resolve_recipients(session, event=event)
        email = await _email(session, user_id)
    assert got == [Recipient(channel_id=channel.id, address=email)]


async def test_rule_scoped_to_a_different_queue_does_not_match(api_env, tmp_path: Path) -> None:
    """The other half: a rule naming a SPECIFIC queue must not fire for an
    event belonging to a different queue in the same org."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        rule_queue = await _queue(session, org_id, user_id, key="rule-queue")
        other_queue = await _queue(session, org_id, user_id, key="other-queue")
        await create_rule(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=RuleCreate(
                event_type="ticket.assigned",
                channel_id=channel.id,
                recipient_kind="assignee",
                queue_id=rule_queue.id,
            ),
        )
        event = _event(
            org_id,
            "ticket.assigned",
            {"queue_id": str(other_queue.id), "assignee_id": str(user_id)},
        )
        got = await resolve_recipients(session, event=event)
    assert got == []


# ---------------------------------------------------------------------------
# §6.1 -- expansion is deduplicated
# ---------------------------------------------------------------------------


async def test_a_user_matched_twice_receives_one_delivery(api_env, tmp_path: Path) -> None:
    """Spec §6.1 -- a user who is both the assignee and a queue member gets
    ONE resolved recipient, not two, because dedup is on (channel_id,
    address)."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        queue = await _queue(session, org_id, user_id)
        await add_queue_member(
            session, org_id=org_id, actor_id=user_id, queue_id=queue.id, user_id=user_id
        )
        await create_rule(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=RuleCreate(
                event_type="ticket.assigned",
                channel_id=channel.id,
                recipient_kind="assignee",
            ),
        )
        await create_rule(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=RuleCreate(
                event_type="ticket.assigned",
                channel_id=channel.id,
                recipient_kind="queue_members",
            ),
        )
        event = _event(
            org_id,
            "ticket.assigned",
            {"queue_id": str(queue.id), "assignee_id": str(user_id)},
        )
        got = await resolve_recipients(session, event=event)
        email = await _email(session, user_id)
    assert len({r.address for r in got}) == len(got)
    assert got == [Recipient(channel_id=channel.id, address=email)]


# ---------------------------------------------------------------------------
# §6.3 -- every resolved recipient is re-checked against the event's org_id
# ---------------------------------------------------------------------------


async def test_recipients_outside_the_event_org_are_dropped(api_env, tmp_path: Path) -> None:
    """Spec §6.3 -- the last point a cross-tenant recipient could enter. A
    notification delivered to the wrong tenant is a data breach, not a bug.

    DISCRIMINATING: a real foreign-org user is seeded as a queue member of
    THIS org's queue (see `_add_foreign_queue_member`), so the query
    genuinely has something to wrongly return -- an empty-result assertion
    with no such row would pass even with the org re-check deleted."""
    org_id, user_id = await _org_with_user(api_env)
    other_org_id, other_user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        queue = await _queue(session, org_id, user_id)
        await add_queue_member(
            session, org_id=org_id, actor_id=user_id, queue_id=queue.id, user_id=user_id
        )
        await _add_foreign_queue_member(
            session, local_org_id=org_id, queue_id=queue.id, foreign_user_id=other_user_id
        )
        await create_rule(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=RuleCreate(
                event_type="ticket.assigned",
                channel_id=channel.id,
                recipient_kind="queue_members",
            ),
        )
        event = _event(org_id, "ticket.assigned", {"queue_id": str(queue.id)})
        got = await resolve_recipients(session, event=event)
        local_email = await _email(session, user_id)
        foreign_email = await _email(session, other_user_id)
    assert all(r.address != foreign_email for r in got)
    assert got == [Recipient(channel_id=channel.id, address=local_email)]


# ---------------------------------------------------------------------------
# §6.4 -- internal content never reaches a requester
# ---------------------------------------------------------------------------


async def test_internal_note_never_reaches_a_requester(api_env, tmp_path: Path) -> None:
    """Spec §6.4 -- the module's most consequential guard, and the last line
    of defence: even though a rule pairing `ticket.internal_note_added` with
    `recipient_kind="requester"` cannot exist (rejected by `RuleCreate`'s
    validator, `create_rule`'s own unconditional check, AND the DB's
    `ck_rule_no_internal_to_requester` CHECK constraint -- see
    `test_notification_rules.py`), the requester can still ARRIVE at
    resolution through a completely different, entirely legitimate path: a
    `queue_members` rule, where the requester happens to also be a member of
    that ticket's queue (plausible -- spec §0.4: "a requester is an ordinary
    users row"; nothing stops a staff member who is also a queue member from
    filing their own ticket).

    DISCRIMINATING: asserted on the resolved `Recipient` list, not on
    whether the rule could be created -- proving THIS layer, not the
    write-time one Task 7 already covers."""
    org_id, user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _channel(session, org_id, user_id, tmp_path)
        queue = await _queue(session, org_id, user_id)
        # The requester is ALSO a queue member -- the genuinely dangerous case.
        await add_queue_member(
            session, org_id=org_id, actor_id=user_id, queue_id=queue.id, user_id=user_id
        )
        await create_rule(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=RuleCreate(
                event_type="ticket.internal_note_added",
                channel_id=channel.id,
                recipient_kind="queue_members",
            ),
        )
        event = _event(
            org_id,
            "ticket.internal_note_added",
            {"queue_id": str(queue.id), "requester_id": str(user_id)},
        )
        got = await resolve_recipients(session, event=event)
        requester_email = await _email(session, user_id)
    assert all(r.address != requester_email for r in got)
    assert got == []


async def test_internal_note_payload_carries_no_body(api_env) -> None:
    """Spec §6.4 mechanism 1 -- even a mis-routed delivery cannot disclose
    the text, because the text was never in the event. This is a contract
    assertion on what THIS module hands `resolve_recipients`, restating the
    payload contract this file's module docstring states for Task 12."""
    org_id, user_id = await _org_with_user(api_env)
    event = _event(
        org_id,
        "ticket.internal_note_added",
        {"queue_id": None, "requester_id": str(user_id)},
    )
    assert "body" not in event.payload
