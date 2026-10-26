"""Kernel transactional outbox (docs/modules/notification/spec.md §3).

The atomicity test is the point of this file. A publish() that commits, or
that opens its own transaction, passes every other test here and produces
notifications about facts that were rolled back.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EVENT_TYPES, UnknownEventType, publish
from app.models import DomainEvent


async def test_publish_writes_an_event(api_env) -> None:
    org_id = await api_env.create_org("evt-write")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        await publish(
            s,
            org_id=org_id,
            event_type="ticket.assigned",
            subject_type="ticket",
            subject_id=uuid4(),
            payload={"ticket_number": "INC-1"},
            actor_user_id=None,
            client_id=None,
        )
        await s.commit()
        got = (
            await s.execute(select(DomainEvent).where(DomainEvent.org_id == org_id))
        ).scalars().all()
    assert [e.event_type for e in got] == ["ticket.assigned"]
    assert got[0].payload == {"ticket_number": "INC-1"}


async def test_publish_rejects_an_unregistered_event_type(api_env) -> None:
    """A typo'd event type matches no rule and delivers nothing -- invisible in
    production. Fail loudly at publish instead of silently never notifying."""
    org_id = await api_env.create_org("evt-typo")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        with pytest.raises(UnknownEventType):
            await publish(
                s,
                org_id=org_id,
                event_type="ticket.assinged",
                subject_type="ticket",
                subject_id=uuid4(),
                payload={},
                actor_user_id=None,
                client_id=None,
            )


async def test_publish_accepts_a_null_actor(api_env) -> None:
    """Spec §2.1: a Phase 16 alert event has no human actor. Inventing a
    synthetic user to satisfy a NOT NULL would put a fake person in the record."""
    org_id = await api_env.create_org("evt-machine")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        await publish(
            s,
            org_id=org_id,
            event_type="ticket.status_changed",
            subject_type="ticket",
            subject_id=uuid4(),
            payload={},
            actor_user_id=None,
            client_id=None,
        )
        await s.commit()
        got = (
            await s.execute(select(DomainEvent).where(DomainEvent.org_id == org_id))
        ).scalars().one()
    assert got.actor_user_id is None


async def test_a_rolled_back_transaction_leaves_no_event(api_env) -> None:
    """Spec §12.8 -- outbox atomicity, the property the whole design rests on.

    publish() must not commit and must not open its own transaction. If it did
    either, an event would survive the rollback of the state change it
    describes, and the system would notify about something that never happened.
    """
    org_id = await api_env.create_org("evt-atomic")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        await publish(
            s,
            org_id=org_id,
            event_type="ticket.resolved",
            subject_type="ticket",
            subject_id=uuid4(),
            payload={},
            actor_user_id=None,
            client_id=None,
        )
        await s.rollback()

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        got = (
            await s.execute(select(DomainEvent).where(DomainEvent.org_id == org_id))
        ).scalars().all()
    assert got == [], "publish() committed or opened its own transaction"


def test_public_and_internal_comments_are_distinct_event_types() -> None:
    """Spec §2.1 / §6.4.

    The write-time rejection of internal-note -> requester rules depends on
    internal notes having their OWN event type. Collapsing these two into one
    type with a visibility flag would silently move the leak guard from write
    time to render time, one template edit away from disclosing staff
    discussion to the requester.
    """
    assert "ticket.comment_added" in EVENT_TYPES
    assert "ticket.internal_note_added" in EVENT_TYPES
