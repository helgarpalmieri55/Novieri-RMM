"""The expiry/retry sweep logic, exercised directly (no broker)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import Device, Job, JobStatus, Organization
from app.worker import sweep_org

REDIS_URL = "redis://localhost:6379/9"


async def _org_device(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"wrk-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(org_id=org.id, hostname="w1", os_family="linux")
        session.add(device)
        await session.commit()
        return org.id, device.id


async def test_sweep_expires_overdue_and_leaves_fresh(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, device_id = await _org_device(admin_engine)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    async with org_session(org_id, engine=app_engine) as session:
        overdue = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        fresh = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        overdue.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.add(overdue)
        overdue_id, fresh_id = overdue.id, fresh.id

    expired, redispatched = await sweep_org(org_id, engine, app_engine, redis)
    await redis.aclose()
    assert expired == 1
    assert redispatched == 0

    async with org_session(org_id, engine=app_engine) as session:
        jobs = {
            j.id: j.status
            for j in (await session.execute(select(Job))).scalars()
        }
    assert jobs[overdue_id] is JobStatus.expired
    assert jobs[fresh_id] is JobStatus.queued


async def test_sweep_redispatches_stuck_dispatched_job_with_fresh_nonce(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, device_id = await _org_device(admin_engine)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await redis.flushdb()

    async with org_session(org_id, engine=app_engine) as session:
        job = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await engine.mark_dispatched(session, job)
        # simulate a delivery that never got a response for too long
        job.updated_at = datetime.now(UTC) - timedelta(minutes=30)
        session.add(job)
        job_id, old_nonce = job.id, job.nonce

    pubsub = redis.pubsub()
    await pubsub.psubscribe(f"dispatch:{device_id}")
    expired, redispatched = await sweep_org(org_id, engine, app_engine, redis)
    assert redispatched == 1

    published = None
    for _ in range(30):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if message and message["type"] == "pmessage":
            published = json.loads(message["data"])
            break
    await redis.aclose()
    assert published is not None
    assert published["job"]["nonce"] != old_nonce

    async with org_session(org_id, engine=app_engine) as session:
        refreshed = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one()
        assert refreshed.status is JobStatus.queued  # re-queued for delivery
        assert refreshed.attempts == 2


# ---------------------------------------------------------------------------
# §3.4 of docs/superpowers/specs/2026-08-22-job-lifecycle-design.md: the
# stuck-dispatch sweep must never resurrect a job somebody asked to cancel.
# The design calls this "the quietest defect in the design" -- the operator
# cancels, and ten minutes later the platform dispatches it again.
# ---------------------------------------------------------------------------


async def test_sweep_never_redispatches_a_job_whose_cancel_was_requested(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A `dispatched` job carrying `cancel_requested_at` is left alone.

    THE ROW IS WRITTEN DIRECTLY AND THAT IS DELIBERATE. Cycle 1 can only reach
    `cancel_requested_at` through withdrawal, which leaves the job `cancelled`
    rather than `dispatched`, so this combination is not reachable through any
    route today. It is the state cycle 2 produces the moment an interruption is
    asked for and the endpoint has not answered yet, and the guard has to be in
    the query BEFORE that path exists -- a sweep that re-signs and republishes
    a job an operator cancelled is a signed command going to a machine against
    an explicit instruction not to.

    Deleting `Job.cancel_requested_at.is_(None)` from `sweep_org` turns this
    red: the job is re-signed, its `attempts` goes to 2 and an envelope is
    published.
    """
    org_id, device_id = await _org_device(admin_engine)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await redis.flushdb()

    async with org_session(org_id, engine=app_engine) as session:
        job = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await engine.mark_dispatched(session, job)
        job.updated_at = datetime.now(UTC) - timedelta(minutes=30)
        job.cancel_requested_at = datetime.now(UTC)
        session.add(job)
        job_id, nonce_before = job.id, job.nonce

    pubsub = redis.pubsub()
    await pubsub.subscribe(f"dispatch:{device_id}")
    expired, redispatched = await sweep_org(org_id, engine, app_engine, redis)
    assert (expired, redispatched) == (0, 0)
    assert await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5) is None
    await pubsub.aclose()
    await redis.aclose()

    async with org_session(org_id, engine=app_engine) as session:
        row = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert row.status is JobStatus.dispatched
        assert row.nonce == nonce_before
        assert row.attempts == 1


async def test_sweep_leaves_a_withdrawn_job_alone(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The reachable half of the same guarantee.

    A withdrawn job is `cancelled`, which is terminal, so neither the expiry
    pass nor the stuck-dispatch pass may touch it however long it sits there.
    This one IS reachable through the route today, which is why it is asserted
    beside the defensive test above rather than instead of it.
    """
    org_id, device_id = await _org_device(admin_engine)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await redis.flushdb()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        from app.models import User

        user = User(org_id=org_id, external_subject=f"s-{uuid4().hex[:8]}", email="a@b.test")
        session.add(user)
        await session.commit()
        actor = user.id

    async with org_session(org_id, engine=app_engine) as session:
        job = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
        await engine.withdraw(session, job, requested_by=actor)
        job.updated_at = datetime.now(UTC) - timedelta(minutes=30)
        job.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        session.add(job)
        job_id = job.id

    expired, redispatched = await sweep_org(org_id, engine, app_engine, redis)
    await redis.aclose()
    assert (expired, redispatched) == (0, 0)

    async with org_session(org_id, engine=app_engine) as session:
        row = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert row.status is JobStatus.cancelled
    assert row.cancel_outcome == "withdrawn"
