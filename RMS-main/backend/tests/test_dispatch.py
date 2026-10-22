import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.gateway.connections import ConnectionRegistry
from app.gateway.dispatch import (
    CLOSE_REVOKED,
    DispatchSubscriber,
    RevokeSubscriber,
    handle_agent_message,
    publish_job,
    publish_revoke,
)
from app.gateway.protocol import parse_agent_message
from app.jobs.engine import JobEngine
from app.models import AuditEvent, Device, JobLog, JobStatus, Organization

REDIS_URL = "redis://localhost:6379/9"


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


@pytest.fixture()
async def redis_client():
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


async def _org_device(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"dsp-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(org_id=org.id, hostname="d1", os_family="linux")
        session.add(device)
        await session.commit()
        return org.id, device.id


async def _device_in(api_env, org_id: UUID, hostname: str = "d1") -> UUID:
    """A device row in an EXISTING org, written on the admin engine."""
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=hostname, os_family="linux")
        session.add(device)
        await session.commit()
        return device.id


async def test_published_job_reaches_registered_connection(redis_client) -> None:
    registry = ConnectionRegistry()
    device_id = uuid4()
    ws = FakeWs()
    registry.add(device_id, ws)
    subscriber = DispatchSubscriber(registry, redis_client)
    task = asyncio.create_task(subscriber.run())
    try:
        await asyncio.sleep(0.2)  # let psubscribe settle
        envelope = {"v": 1, "kind": "job", "job": {"id": "j1"}, "sig": "s"}
        await publish_job(redis_client, device_id, envelope)
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
    assert ws.sent and json.loads(ws.sent[0]) == envelope


async def test_revoke_event_closes_live_connection(redis_client) -> None:
    # Deferred finding B: a revoke event drops the device's live connection on
    # whichever replica holds it; other devices are untouched.
    registry = ConnectionRegistry()
    revoked_device, other_device = uuid4(), uuid4()
    revoked_ws, other_ws = FakeWs(), FakeWs()
    registry.add(revoked_device, revoked_ws)
    registry.add(other_device, other_ws)
    task = asyncio.create_task(RevokeSubscriber(registry, redis_client).run())
    try:
        await asyncio.sleep(0.2)  # let subscribe settle
        await publish_revoke(redis_client, revoked_device)
        for _ in range(50):
            if revoked_ws.closed:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
    assert revoked_ws.closed == (CLOSE_REVOKED, "certificate revoked")
    assert other_ws.closed is None


async def test_job_result_completes_job_and_emits_ui_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, redis_client, tmp_path: Path
) -> None:
    org_id, device_id = await _org_device(admin_engine)
    job_engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=app_engine) as session:
        job = await job_engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await job_engine.mark_dispatched(session, job)
        job_id = job.id

    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"ui:{org_id}")

    running = parse_agent_message(
        json.dumps({"v": 1, "kind": "job_status", "job_id": str(job_id), "state": "running"})
    )
    await handle_agent_message(
        app_engine, job_engine, redis_client, running, device_id=device_id, org_id=org_id
    )
    log = parse_agent_message(
        json.dumps(
            {"v": 1, "kind": "job_log", "job_id": str(job_id), "level": "info", "msg": "hi"}
        )
    )
    await handle_agent_message(
        app_engine, job_engine, redis_client, log, device_id=device_id, org_id=org_id
    )
    result = parse_agent_message(
        json.dumps(
            {
                "v": 1,
                "kind": "job_result",
                "job_id": str(job_id),
                "state": "succeeded",
                "result": {"echo": "ping"},
            }
        )
    )
    await handle_agent_message(
        app_engine, job_engine, redis_client, result, device_id=device_id, org_id=org_id
    )

    async with org_session(org_id, engine=app_engine) as session:
        from app.models import Job

        refreshed = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert refreshed.status is JobStatus.succeeded
        assert refreshed.result == {"echo": "ping"}
        logs = (
            (await session.execute(select(JobLog).where(JobLog.job_id == job_id))).scalars().all()
        )
        assert [entry.message for entry in logs] == ["hi"]

    events = []
    for _ in range(50):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message:
            events.append(json.loads(message["data"]))
        if len(events) >= 3:
            break
    kinds = {e["kind"] for e in events}
    assert "job_status" in kinds


async def test_message_for_another_devices_job_is_refused_and_audited(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, redis_client, tmp_path: Path
) -> None:
    org_id, device_a = await _org_device(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device_b_row = Device(org_id=org_id, hostname="d2", os_family="linux")
        session.add(device_b_row)
        await session.commit()
        device_b = device_b_row.id

    job_engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=app_engine) as session:
        job = await job_engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_a, params={}
        )
        await job_engine.mark_dispatched(session, job)
        job_id = job.id

    # a connection bound to device B claims a result for device A's job: the
    # forged message is discarded (device A's job is untouched) and audited
    forged = parse_agent_message(
        json.dumps(
            {"v": 1, "kind": "job_result", "job_id": str(job_id), "state": "succeeded",
             "result": {}}
        )
    )
    await handle_agent_message(
        app_engine, job_engine, redis_client, forged, device_id=device_b, org_id=org_id
    )

    async with org_session(org_id, engine=app_engine) as session:
        from app.models import Job

        refreshed = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert refreshed.status is JobStatus.dispatched
        assert refreshed.result is None
        actions = {
            r.action
            for r in (
                await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id))
            ).scalars()
        }
    assert "gateway.identity_mismatch" in actions


# ---------------------------------------------------------------------------
# §3.3 of docs/superpowers/specs/2026-08-22-job-lifecycle-design.md:
# `queued` has to MEAN "never written to a socket", because withdrawal's whole
# safety argument is built on it. Two properties make it true, and each is
# asserted here rather than reasoned about.
# ---------------------------------------------------------------------------


async def test_delivery_is_marked_before_the_envelope_reaches_the_socket(
    redis_client,
) -> None:
    """The live-dispatch path marks first and writes second.

    It used to do the reverse -- `await ws.send_text(data)` and THEN
    `on_delivered` -- which left a window where the envelope was already on the
    wire and the row still said `queued`. `_drain_queued` has always marked
    inside its transaction before writing; this makes the two paths agree.

    THE FALSIFIER IS THE ORDER ITSELF, so the test records it rather than
    checking that both things eventually happened.
    """
    registry = ConnectionRegistry()
    device_id = uuid4()
    order: list[str] = []

    class OrderingWs:
        async def send_text(self, data: str) -> None:
            order.append("sent")

    registry.add(device_id, OrderingWs())

    async def marked(_device: UUID, _envelope: dict) -> None:
        order.append("marked")

    subscriber = DispatchSubscriber(registry, redis_client, on_delivered=marked)
    task = asyncio.create_task(subscriber.run())
    try:
        await asyncio.sleep(0.2)  # let psubscribe settle
        await publish_job(
            redis_client, device_id, {"v": 1, "kind": "job", "job": {"id": "j1"}, "sig": "s"}
        )
        for _ in range(50):
            if len(order) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
    assert order == ["marked", "sent"]


async def test_on_delivered_reads_the_status_under_a_row_lock(
    api_env, redis_client, tmp_path: Path
) -> None:
    """A concurrent writer that reached the row first is not clobbered.

    Under READ COMMITTED an unlocked `SELECT` returns the pre-commit row, so
    `_on_delivered` read `queued`, blocked on the other transaction's lock at
    UPDATE time, and then wrote `dispatched` over whatever that transaction had
    just committed -- `_transition`'s guard never fires, because the status it
    checks was read before the other writer committed.

    Deleting `.with_for_update()` from `_on_delivered` turns this red.
    """
    from app.gateway.main import GatewayServer
    from app.models import Job

    org_id = await api_env.create_org("deliver-lock")
    device_id = await _device_in(api_env, org_id)
    job_engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=api_env.app_engine) as session:
        job = await job_engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        job_id = job.id
        envelope = await job_engine.signed_envelope(job)

    server = GatewayServer(
        settings=api_env.settings,
        app_engine=api_env.app_engine,
        admin_engine=api_env.admin_engine,
        job_engine=job_engine,
        redis=redis_client,
    )
    server._device_org[device_id] = org_id

    locked = asyncio.Event()
    released = asyncio.Event()

    async def competing_writer() -> None:
        """Holds the row lock, moves the job to a terminal state, commits."""
        async with org_session(org_id, engine=api_env.app_engine) as session:
            row = (
                await session.execute(
                    select(Job).where(Job.id == job_id).with_for_update()
                )
            ).scalar_one()
            locked.set()
            await released.wait()
            row.status = JobStatus.expired
            session.add(row)
            await session.flush()

    writer = asyncio.create_task(competing_writer())
    await locked.wait()
    delivery = asyncio.create_task(server._on_delivered(device_id, envelope))
    # `_on_delivered` must be BLOCKED on the lock, not reading around it.
    await asyncio.sleep(0.2)
    assert not delivery.done(), "_on_delivered read the row without taking its lock"
    released.set()
    await writer
    await delivery

    async with org_session(org_id, engine=api_env.app_engine) as session:
        refreshed = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert refreshed.status is JobStatus.expired


async def test_draining_a_reconnecting_agents_queue_locks_the_rows_it_marks(
    api_env, redis_client, tmp_path: Path
) -> None:
    """The other dispatch path takes the same lock, for the same reason.

    `_drain_queued` already marked before writing, but it read the queue with
    no lock at all -- so a withdrawal committing between its `SELECT` and its
    `UPDATE` was overwritten and the job went to the endpoint anyway. Deleting
    `.with_for_update()` from `JobEngine.queued_for_device` turns this red.
    """
    from app.models import Job

    org_id = await api_env.create_org("drain-lock")
    device_id = await _device_in(api_env, org_id)
    job_engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=api_env.app_engine) as session:
        job = await job_engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        job_id = job.id

    locked = asyncio.Event()
    released = asyncio.Event()

    async def competing_writer() -> None:
        async with org_session(org_id, engine=api_env.app_engine) as session:
            row = (
                await session.execute(
                    select(Job).where(Job.id == job_id).with_for_update()
                )
            ).scalar_one()
            locked.set()
            await released.wait()
            row.status = JobStatus.expired
            session.add(row)
            await session.flush()

    async def drain() -> list[UUID]:
        async with org_session(org_id, engine=api_env.app_engine) as session:
            return [j.id for j in await job_engine.queued_for_device(session, device_id)]

    writer = asyncio.create_task(competing_writer())
    await locked.wait()
    reader = asyncio.create_task(drain())
    await asyncio.sleep(0.2)
    assert not reader.done(), "queued_for_device read the queue without locking it"
    released.set()
    await writer
    assert await reader == []

    async with org_session(org_id, engine=api_env.app_engine) as session:
        refreshed = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert refreshed.status is JobStatus.expired
