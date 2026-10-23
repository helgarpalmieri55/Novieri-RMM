"""Behavioural cross-org tenancy isolation for the notification-core module.

Migration 0025 (docs/modules/notification/spec.md sec 12.10) put RLS
ENABLE + FORCE and a `tenant_isolation` policy (USING and WITH CHECK) on six
tables: domain_events, notification_channels, notification_rules,
queue_members, notification_deliveries, webhook_allowlist_entries. The
committed tests elsewhere only check CATALOG METADATA -- "RLS is enabled",
"a policy named tenant_isolation exists". None of that proves the policy
actually behaves: if a future migration dropped WITH CHECK, or narrowed
either predicate, the whole suite would stay green while cross-tenant reads
or writes started succeeding.

These tests instead prove the policies BEHAVE, in the two directions that
matter, for all six tables:

  * READ isolation: a row written under org A is invisible to a session
    scoped to org B (the USING clause).
  * WRITE isolation: a session scoped to org B cannot INSERT a row carrying
    org A's org_id (the WITH CHECK clause -- exactly the direction a
    USING-only policy would silently permit).

ENGINE CHOICE. Every assertion below runs through `app_engine` (the
NOBYPASSRLS `app_user` role) via `app.core.tenancy.org_session`, matching
test_rls.py. `admin_engine` runs as `its_admin`, which carries BYPASSRLS
(see tests/conftest.py's `pg_cluster` fixture) specifically so it can seed
cross-org fixtures and run migrations -- it would pass every test in this
file even with all six policies dropped entirely, so it is used ONLY to
seed the prerequisite rows (org, users, channel, queue, domain event) that
each table's NOT NULL foreign keys require, never to make the assertion.

RED-VERIFICATION (full transcript in .git/sdd/fix-tenancy-report.md): before
trusting this file, the write-isolation test for webhook_allowlist_entries
and the read-isolation test for domain_events were each independently
confirmed to go red by temporarily recreating that one table's policy in a
broken form -- `WITH CHECK (true)` for the write direction, `USING (true)`
for the read direction -- rerunning just that test, observing the failure,
and then restoring the real tenant_isolation policy from migration 0025
before moving on.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    DomainEvent,
    NotificationChannel,
    NotificationDelivery,
    NotificationRule,
    QueueMember,
    TicketQueue,
    WebhookAllowlistEntry,
)

RLS_VIOLATION = (DBAPIError, ProgrammingError)


def _now() -> datetime:
    return datetime.now(UTC)


def _domain_event(org_id: UUID, payload: dict[str, object]) -> DomainEvent:
    now = _now()
    return DomainEvent(
        org_id=org_id,
        event_type="ticket.created",
        subject_type="ticket",
        subject_id=uuid4(),
        payload=payload,
        occurred_at=now,
        created_at=now,
    )


class OrgFixture:
    """Prerequisite rows for one org: a user plus one row in every table that
    the six notification tables' NOT NULL foreign keys point at."""

    def __init__(
        self,
        org_id: UUID,
        user_id: UUID,
        channel_id: UUID,
        queue_id: UUID,
        event_id: UUID,
    ) -> None:
        self.org_id = org_id
        self.user_id = user_id
        self.channel_id = channel_id
        self.queue_id = queue_id
        self.event_id = event_id


async def _seed_org(admin_engine: AsyncEngine, api_env, prefix: str) -> OrgFixture:
    """Build one org with a user, a notification channel, a ticket queue, and
    a domain event -- everything the six tables' FKs require -- via the
    BYPASSRLS admin engine. This is fixture setup, not the assertion under
    test."""
    org_id = await api_env.create_org(prefix)
    sub = await api_env.create_user(org_id, "org-admin")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE external_subject = :sub"),
                {"sub": sub},
            )
        ).scalar_one()

        channel = NotificationChannel(
            org_id=org_id,
            kind="webhook",
            name=f"{prefix}-channel",
            config={"url": "https://example.internal/hook"},
            created_by=user_id,
        )
        queue = TicketQueue(
            org_id=org_id, key=f"{prefix}-queue", name=f"{prefix} queue", created_by=user_id
        )
        event = _domain_event(org_id, {"ok": True})
        session.add_all([channel, queue, event])
        await session.commit()
        return OrgFixture(org_id, user_id, channel.id, queue.id, event.id)


@pytest.fixture()
async def two_orgs(admin_engine: AsyncEngine, api_env) -> tuple[OrgFixture, OrgFixture]:
    org_a = await _seed_org(admin_engine, api_env, "notif-a")
    org_b = await _seed_org(admin_engine, api_env, "notif-b")
    return org_a, org_b


# ---------------------------------------------------------------------------
# domain_events
# ---------------------------------------------------------------------------


async def test_domain_events_read_isolation(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = _domain_event(org_a.org_id, {"secret": "org-a-only"})
        session.add(row)
        await session.commit()
        event_id = row.id

    async with org_session(org_b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        seen = (
            (await session.execute(select(DomainEvent).where(DomainEvent.id == event_id)))
            .scalars()
            .all()
        )
    assert seen == []


async def test_domain_events_write_isolation(
    app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    with pytest.raises(RLS_VIOLATION):
        async with org_session(
            org_b.org_id, engine=app_engine, clients=ClientScope.all()
        ) as session:
            session.add(_domain_event(org_a.org_id, {"evil": True}))
            await session.flush()


# ---------------------------------------------------------------------------
# notification_channels
# ---------------------------------------------------------------------------


async def test_notification_channels_read_isolation(
    app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    async with org_session(org_b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        seen = (
            (
                await session.execute(
                    select(NotificationChannel).where(NotificationChannel.id == org_a.channel_id)
                )
            )
            .scalars()
            .all()
        )
    assert seen == []


async def test_notification_channels_write_isolation(
    app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    with pytest.raises(RLS_VIOLATION):
        async with org_session(
            org_b.org_id, engine=app_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                NotificationChannel(
                    org_id=org_a.org_id,
                    kind="webhook",
                    name="evil-channel",
                    config={},
                    created_by=org_a.user_id,
                )
            )
            await session.flush()


# ---------------------------------------------------------------------------
# notification_rules
# ---------------------------------------------------------------------------


async def test_notification_rules_read_isolation(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = NotificationRule(
            org_id=org_a.org_id,
            event_type="ticket.created",
            channel_id=org_a.channel_id,
            queue_id=org_a.queue_id,
            recipient_kind="channel_default",
            created_by=org_a.user_id,
        )
        session.add(row)
        await session.commit()
        rule_id = row.id

    async with org_session(org_b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        seen = (
            (await session.execute(select(NotificationRule).where(NotificationRule.id == rule_id)))
            .scalars()
            .all()
        )
    assert seen == []


async def test_notification_rules_write_isolation(
    app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    with pytest.raises(RLS_VIOLATION):
        async with org_session(
            org_b.org_id, engine=app_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                NotificationRule(
                    org_id=org_a.org_id,
                    event_type="ticket.created",
                    channel_id=org_a.channel_id,
                    queue_id=org_a.queue_id,
                    recipient_kind="channel_default",
                    created_by=org_a.user_id,
                )
            )
            await session.flush()


# ---------------------------------------------------------------------------
# queue_members
# ---------------------------------------------------------------------------


async def test_queue_members_read_isolation(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = QueueMember(
            org_id=org_a.org_id,
            queue_id=org_a.queue_id,
            user_id=org_a.user_id,
            created_by=org_a.user_id,
        )
        session.add(row)
        await session.commit()
        member_id = row.id

    async with org_session(org_b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        seen = (
            (await session.execute(select(QueueMember).where(QueueMember.id == member_id)))
            .scalars()
            .all()
        )
    assert seen == []


async def test_queue_members_write_isolation(
    app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    with pytest.raises(RLS_VIOLATION):
        async with org_session(
            org_b.org_id, engine=app_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                QueueMember(
                    org_id=org_a.org_id,
                    queue_id=org_a.queue_id,
                    user_id=org_a.user_id,
                    created_by=org_a.user_id,
                )
            )
            await session.flush()


# ---------------------------------------------------------------------------
# notification_deliveries
# ---------------------------------------------------------------------------


async def test_notification_deliveries_read_isolation(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = NotificationDelivery(
            org_id=org_a.org_id,
            event_id=org_a.event_id,
            channel_id=org_a.channel_id,
            recipient="ops@org-a.example",
            status="pending",
        )
        session.add(row)
        await session.commit()
        delivery_id = row.id

    async with org_session(org_b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        seen = (
            (
                await session.execute(
                    select(NotificationDelivery).where(NotificationDelivery.id == delivery_id)
                )
            )
            .scalars()
            .all()
        )
    assert seen == []


async def test_notification_deliveries_write_isolation(
    app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    with pytest.raises(RLS_VIOLATION):
        async with org_session(
            org_b.org_id, engine=app_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                NotificationDelivery(
                    org_id=org_a.org_id,
                    event_id=org_a.event_id,
                    channel_id=org_a.channel_id,
                    recipient="evil@example.com",
                    status="pending",
                )
            )
            await session.flush()


# ---------------------------------------------------------------------------
# webhook_allowlist_entries
# ---------------------------------------------------------------------------


async def test_webhook_allowlist_entries_read_isolation(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = WebhookAllowlistEntry(
            org_id=org_a.org_id,
            host="internal-a.example.net",
            created_by=org_a.user_id,
        )
        session.add(row)
        await session.commit()
        entry_id = row.id

    async with org_session(org_b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        seen = (
            (
                await session.execute(
                    select(WebhookAllowlistEntry).where(WebhookAllowlistEntry.id == entry_id)
                )
            )
            .scalars()
            .all()
        )
    assert seen == []


async def test_webhook_allowlist_entries_write_isolation(
    app_engine: AsyncEngine, two_orgs: tuple[OrgFixture, OrgFixture]
) -> None:
    org_a, org_b = two_orgs
    with pytest.raises(RLS_VIOLATION):
        async with org_session(
            org_b.org_id, engine=app_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                WebhookAllowlistEntry(
                    org_id=org_a.org_id,
                    host="evil.example.net",
                    created_by=org_a.user_id,
                )
            )
            await session.flush()
