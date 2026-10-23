"""Outbox drain and delivery worker tasks (docs/modules/notification/spec.md
§7 delivery/retry/dead-lettering, §10 worker integration).

FIXTURE PATTERN. `db_session`/`org`/`an_event`/`rule_for`/`vault`/
`stuck_delivery`/`failing_delivery_at_max` from the task brief do not exist
in this codebase's test suite -- exactly the note `test_notification_
routing.py`'s own docstring makes about the same brief-style names. This file
uses the real project convention instead: `api_env`, `AsyncSession(api_env.
admin_engine, ...)`, `app.core.events.publish`, and `create_channel`/
`create_rule` (Tasks 6/7) to build real rows -- plus direct ORM construction
for the `NotificationDelivery` rows this task itself introduces, since no
service writes those yet (mirrors `test_notification_delivery.py`'s own
`_delivery` helper).

STUBBING DELIVERY (no real network calls, per the task instructions).
`deliver_pending` calls `attempt_delivery` by the bare name it imported into
`app.worker`'s module namespace -- monkeypatching `app.worker.attempt_
delivery` replaces exactly what `deliver_pending` calls, without touching a
socket or DNS resolver, for every test here that only cares about the
worker's own claim/return/count bookkeeping (drain tests never call
`attempt_delivery` at all; the stuck-claim test does not care what the
attempt's outcome is, only that one happened). `test_exhausted_delivery_
becomes_dead` is the one test that needs `attempt_delivery`'s REAL backoff
math (Task 9) to prove `deliver_pending` actually drives a delivery to
`dead` on exhaustion, so it leaves `attempt_delivery` itself alone and
instead stubs the webhook transport one level down -- `app.core.egress.
_resolve_all`/`_build_transport` -- exactly as `test_notification_
delivery.py` does.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

import app.worker as worker
from app.core.config import override_settings
from app.core.events import publish
from app.core.vault import LocalVaultService
from app.models import DomainEvent, NotificationChannel, NotificationDelivery, User
from app.modules.notification.channels import create_channel
from app.modules.notification.delivery import MAX_ATTEMPTS
from app.modules.notification.rules import create_rule
from app.modules.notification.schema import ChannelCreate, RuleCreate
from app.worker import STUCK_NOTIFICATION_CLAIM_AFTER, deliver_pending, drain_outbox


@pytest.fixture(autouse=True)
def _egress_settings(request: pytest.FixtureRequest):
    """`post_json` calls `get_settings()` against the module-level singleton,
    which `api_env` never populates on its own -- same autouse baseline
    `test_notification_delivery.py` and `test_egress_guard.py` already use,
    needed here only by `test_exhausted_delivery_becomes_dead` (the one test
    that drives a real webhook attempt through `attempt_delivery`)."""
    if "api_env" not in request.fixturenames:
        yield
        return
    api_env = request.getfixturevalue("api_env")
    with override_settings(api_env.settings):
        yield


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    org_id = await api_env.create_org("notif-worker")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


def _vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


async def _webhook_channel(
    session: AsyncSession,
    org_id: UUID,
    user_id: UUID,
    vault: LocalVaultService,
    *,
    name: str = "ops-webhook",
    url: str = "https://hooks.example.com/x",
) -> NotificationChannel:
    return await create_channel(
        session,
        org_id=org_id,
        actor_id=user_id,
        data=ChannelCreate(kind="webhook", name=name, config={"url": url}),
        secret=None,
        vault=vault,
    )


async def _rule(
    session: AsyncSession,
    org_id: UUID,
    user_id: UUID,
    *,
    event_type: str,
    channel_id: UUID,
    recipient_kind: str = "assignee",
) -> None:
    await create_rule(
        session,
        org_id=org_id,
        actor_id=user_id,
        data=RuleCreate(
            event_type=event_type, channel_id=channel_id, recipient_kind=recipient_kind
        ),
    )


async def _event(
    session: AsyncSession, org_id: UUID, *, event_type: str = "ticket.assigned"
) -> DomainEvent:
    await publish(
        session,
        org_id=org_id,
        event_type=event_type,
        subject_type="ticket",
        subject_id=uuid7(),
        payload={"assignee_id": None},
        actor_user_id=None,
    )
    await session.flush()
    return (
        (
            await session.execute(
                select(DomainEvent)
                .where(DomainEvent.org_id == org_id, DomainEvent.event_type == event_type)
                .order_by(DomainEvent.created_at.desc())
            )
        )
        .scalars()
        .first()
    )


async def _delivery(
    session: AsyncSession,
    org_id: UUID,
    *,
    channel_id: UUID,
    event_id: UUID,
    recipient: str = "hooks.example.com",
    attempts: int = 0,
    status: str = "pending",
    next_attempt_at: datetime | None = None,
    claimed_at: datetime | None = None,
) -> NotificationDelivery:
    delivery = NotificationDelivery(
        org_id=org_id,
        event_id=event_id,
        channel_id=channel_id,
        recipient=recipient,
        status=status,
        attempts=attempts,
        next_attempt_at=next_attempt_at,
        claimed_at=claimed_at,
    )
    session.add(delivery)
    await session.flush()
    return delivery


async def _stub_attempt_sends(
    session: AsyncSession, *, delivery: NotificationDelivery, vault
) -> None:
    """A stand-in for `attempt_delivery` that never opens a socket -- used by
    every test below except `test_exhausted_delivery_becomes_dead`, which
    needs the real backoff math instead."""
    delivery.attempts += 1
    delivery.status = "sent"
    delivery.sent_at = datetime.now(UTC)
    delivery.last_error = None
    delivery.next_attempt_at = None
    session.add(delivery)
    await session.flush()


# ---------------------------------------------------------------------------
# drain_outbox
# ---------------------------------------------------------------------------


async def test_drain_marks_events_processed(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        await _rule(session, org_id, user_id, event_type="ticket.assigned", channel_id=channel.id)
        await _event(session, org_id)
        await session.commit()

    assert await drain_outbox(org_id, api_env.app_engine) == 1


async def test_drain_is_idempotent(api_env, tmp_path: Path) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        await _rule(session, org_id, user_id, event_type="ticket.assigned", channel_id=channel.id)
        await _event(session, org_id)
        await session.commit()

    assert await drain_outbox(org_id, api_env.app_engine) == 1
    reprocessed = await drain_outbox(org_id, api_env.app_engine)
    assert reprocessed == 0, "reprocessed an already-drained event"


async def test_an_event_with_no_rules_is_still_marked_processed(api_env) -> None:
    """Spec §6.2. If it were left unprocessed it would be re-drained forever."""
    org_id, _user_id = await _org_with_user(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        await _event(session, org_id)
        await session.commit()

    assert await drain_outbox(org_id, api_env.app_engine) == 1
    assert await drain_outbox(org_id, api_env.app_engine) == 0


async def test_drain_only_processes_the_named_org(api_env, tmp_path: Path) -> None:
    """Invariant #5: an event that belongs to a DIFFERENT org must never be
    claimed (and so never processed) by a `drain_outbox` call for this org."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, _user_b = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_a, user_a, vault)
        await _rule(session, org_a, user_a, event_type="ticket.assigned", channel_id=channel.id)
        await _event(session, org_a)
        await _event(session, org_b)
        await session.commit()

    assert await drain_outbox(org_a, api_env.app_engine) == 1

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        other_event = (
            await session.execute(select(DomainEvent).where(DomainEvent.org_id == org_b))
        ).scalar_one()
    assert other_event.processed_at is None


# ---------------------------------------------------------------------------
# deliver_pending
# ---------------------------------------------------------------------------


async def test_deliver_pending_attempts_a_due_delivery(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await session.commit()

    monkeypatch.setattr(worker, "attempt_delivery", _stub_attempt_sends)
    delivered, returned = await deliver_pending(org_id, api_env.app_engine, vault)
    assert delivered == 1
    assert returned == 0


async def test_deliver_pending_is_idempotent(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running `deliver_pending` twice must not reattempt (and so must not
    duplicate) a delivery the first call already finished (invariant #5's
    idempotency requirement, mirrored from `drain_outbox`'s)."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await session.commit()

    monkeypatch.setattr(worker, "attempt_delivery", _stub_attempt_sends)
    delivered_1, _ = await deliver_pending(org_id, api_env.app_engine, vault)
    delivered_2, _ = await deliver_pending(org_id, api_env.app_engine, vault)
    assert delivered_1 == 1
    assert delivered_2 == 0, "reattempted an already-sent delivery"


async def test_a_stuck_claim_is_returned(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that died mid-attempt leaves a claimed-but-unsent row. Without
    the sweep it is never retried -- the same failure app/worker.py's
    existing `STUCK_DISPATCH_AFTER` sweep exists to prevent."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        stuck_claimed_at = datetime.now(UTC) - STUCK_NOTIFICATION_CLAIM_AFTER - timedelta(minutes=1)
        await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            attempts=1,
            status="failed",
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
            claimed_at=stuck_claimed_at,
        )
        await session.commit()

    monkeypatch.setattr(worker, "attempt_delivery", _stub_attempt_sends)
    _, returned = await deliver_pending(org_id, api_env.app_engine, vault)
    assert returned == 1


async def test_a_fresh_claim_is_not_returned(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim that is still well within the threshold (another worker is
    plausibly mid-attempt right now) must not be swept up as stuck."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            status="pending",
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
            claimed_at=datetime.now(UTC),
        )
        await session.commit()

    monkeypatch.setattr(worker, "attempt_delivery", _stub_attempt_sends)
    delivered, returned = await deliver_pending(org_id, api_env.app_engine, vault)
    assert returned == 0
    assert delivered == 0, "claimed a row another worker might still be mid-attempt on"


async def _broken_email_channel(
    session: AsyncSession, org_id: UUID, user_id: UUID
) -> NotificationChannel:
    """An email channel with a config `_send_email` cannot parse -- built by
    DIRECT ORM construction, bypassing `create_channel`/`schema.
    validate_channel_config` entirely (which C1's schema/service-layer fix
    now enforces on both create and update), to simulate a pre-existing
    poisoned row -- exactly the scenario the whole-branch review demonstrated
    against a live system, and the one C1's schema fix alone cannot protect
    against retroactively. Missing `host` -- `_send_email` reads
    `config["host"]` unguarded and raises an uncaught `KeyError`."""
    channel = NotificationChannel(
        org_id=org_id,
        kind="email",
        name="broken-email",
        config={"port": 587, "from_address": "noreply@toeshee.test"},
        active=True,
        created_by=user_id,
    )
    session.add(channel)
    await session.flush()
    return channel


async def test_deliver_pending_dead_letters_a_malformed_channel_config_instead_of_raising(
    api_env, tmp_path: Path
) -> None:
    """C1(b), the load-bearing proof: a delivery whose channel config
    `_send_email` cannot parse must degrade to a TERMINAL, dead-lettered
    outcome on that one row -- never an uncaught exception that escapes
    `deliver_pending`. Before this fix, `claimed_at` was already committed in
    the claim transaction, so the crash meant `attempts` never incremented
    and the row could never reach `dead` -- `STUCK_NOTIFICATION_CLAIM_AFTER`
    would reclaim it and it would crash again, forever."""
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _broken_email_channel(session, org_id, user_id)
        event = await _event(session, org_id)
        delivery = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            recipient="someone@example.com",
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await session.commit()
        delivery_id = delivery.id

    # Must not raise.
    delivered, returned = await deliver_pending(org_id, api_env.app_engine, vault)
    assert delivered == 1
    assert returned == 0

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        refreshed = (
            await session.execute(
                select(NotificationDelivery).where(NotificationDelivery.id == delivery_id)
            )
        ).scalar_one()
    assert refreshed.status == "dead", "a malformed channel must dead-letter, not loop forever"
    assert refreshed.attempts == 1
    assert refreshed.claimed_at is None
    assert refreshed.next_attempt_at is None


async def test_deliver_pending_all_orgs_a_broken_org_does_not_stop_the_rest(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1(b)'s second, defense-in-depth layer: even if `deliver_pending`
    itself somehow still raised for one org (a regression in the per-delivery
    catch above, a bug in the claim query), `deliver_pending_all_orgs` must
    not let that org's failure skip every org processed after it in the same
    sweep run -- the exact bug the whole-branch review demonstrated
    (`deliver_pending_all_orgs`'s per-org loop had no such guard at all).
    Mirrors `backup_verification_sweep_all_orgs`'s `except Exception:
    continue` convention."""
    org_a, _user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel_b = await _webhook_channel(session, org_b, user_b, vault)
        event_b = await _event(session, org_b)
        await _delivery(
            session,
            org_b,
            channel_id=channel_b.id,
            event_id=event_b.id,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await session.commit()

    real_deliver_pending = worker.deliver_pending
    # The sweep walks EVERY org in the shared test database, so a retry another
    # test left behind can come due while this one runs and add to the sweep's
    # total. What this test owns is org_b, so that is what it counts.
    delivered_for_b: list[int] = []

    async def _boom(org_id: UUID, app_engine, vault):
        if org_id == org_a:
            raise RuntimeError("simulated total failure for this org")
        result = await real_deliver_pending(org_id, app_engine, vault)
        if org_id == org_b:
            delivered_for_b.append(result[0])
        return result

    monkeypatch.setattr(worker, "deliver_pending", _boom)
    monkeypatch.setattr(worker, "attempt_delivery", _stub_attempt_sends)

    with override_settings(api_env.settings):
        total_delivered, _total_returned = await worker.deliver_pending_all_orgs()

    assert delivered_for_b == [1], "org_b's due delivery must still be attempted"
    assert total_delivered >= 1


async def test_exhausted_delivery_becomes_dead(
    api_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    vault = _vault(tmp_path)
    monkeypatch.setattr("app.core.egress._resolve_all", lambda h, p: ["93.184.216.34"])
    monkeypatch.setattr(
        "app.core.egress._build_transport",
        lambda ip: httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        channel = await _webhook_channel(session, org_id, user_id, vault)
        event = await _event(session, org_id)
        failing_delivery_at_max = await _delivery(
            session,
            org_id,
            channel_id=channel.id,
            event_id=event.id,
            attempts=MAX_ATTEMPTS - 1,
            status="failed",
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await session.commit()
        delivery_id = failing_delivery_at_max.id

    await deliver_pending(org_id, api_env.app_engine, vault)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        refreshed = (
            await session.execute(
                select(NotificationDelivery).where(NotificationDelivery.id == delivery_id)
            )
        ).scalar_one()
        assert refreshed.status == "dead"
        assert refreshed.attempts == MAX_ATTEMPTS
        assert refreshed.claimed_at is None
