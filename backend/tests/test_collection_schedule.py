"""Scheduled `inventory.collect` and `logs.ship` (platform review 2026-09-23).

Per-org functions are exercised directly, against one org's devices, because
the test database is shared session-wide and an all-orgs sweep would dispatch
to every device any other test file created.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import Agent, AuditEvent, Device, Job, JobStatus, Organization, Policy
from app.modules.inventory import schedule as inventory_schedule
from app.modules.security_logs import schedule as logs_schedule

REDIS_URL = "redis://localhost:6379/9"


async def _org(admin_engine: AsyncEngine) -> UUID:
    slug = f"sched-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _device(
    admin_engine: AsyncEngine,
    org_id: UUID,
    *,
    os_family: str = "windows",
    agent_status: str | None = "online",
    revoked: bool = False,
    last_seen: datetime | None = None,
    enrolled_at: datetime | None = None,
) -> UUID:
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=f"h-{uuid4().hex[:6]}", os_family=os_family)
        session.add(device)
        await session.flush()
        if agent_status is not None:
            session.add(
                Agent(
                    org_id=org_id,
                    device_id=device.id,
                    cert_fingerprint=uuid4().hex,
                    cert_serial=uuid4().hex[:16],
                    cert_not_after=now + timedelta(days=365),
                    cert_revoked=revoked,
                    enrolled_at=enrolled_at or now - timedelta(days=30),
                    status=agent_status,
                    last_seen_at=last_seen,
                )
            )
        await session.commit()
        return device.id


async def _job(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    job_type: str,
    *,
    status: JobStatus,
    issued_at: datetime,
    finished_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> None:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Job(
                org_id=org_id,
                type=job_type,
                target_type="device",
                target_id=device_id,
                params={},
                nonce=uuid4().hex,
                issued_at=issued_at,
                expires_at=expires_at or issued_at + timedelta(hours=1),
                status=status,
                finished_at=finished_at,
            )
        )
        await session.commit()


def _engine(tmp_path: Path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))


async def _jobs(app_engine: AsyncEngine, org_id: UUID, job_type: str) -> list[Job]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        return list(
            (await session.execute(select(Job).where(Job.type == job_type))).scalars()
        )


# ---------------------------------------------------------------------------
# inventory.collect -- daily sweep
# ---------------------------------------------------------------------------


async def test_inventory_sweep_dispatches_only_to_due_online_devices(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id = await _org(admin_engine)
    now = datetime.now(UTC)
    never = await _device(admin_engine, org_id)
    stale = await _device(admin_engine, org_id, os_family="linux")
    recent_ok = await _device(admin_engine, org_id)
    recent_failed = await _device(admin_engine, org_id)
    in_flight = await _device(admin_engine, org_id)
    offline = await _device(admin_engine, org_id, agent_status="offline")
    revoked = await _device(admin_engine, org_id, revoked=True)
    no_agent = await _device(admin_engine, org_id, agent_status=None)

    t = "inventory.collect"
    await _job(admin_engine, org_id, stale, t, status=JobStatus.succeeded,
               issued_at=now - timedelta(hours=30), finished_at=now - timedelta(hours=30))
    await _job(admin_engine, org_id, recent_ok, t, status=JobStatus.succeeded,
               issued_at=now - timedelta(hours=5), finished_at=now - timedelta(hours=5))
    await _job(admin_engine, org_id, recent_failed, t, status=JobStatus.failed,
               issued_at=now - timedelta(hours=5), finished_at=now - timedelta(hours=5))
    await _job(admin_engine, org_id, in_flight, t, status=JobStatus.dispatched,
               issued_at=now - timedelta(minutes=5))

    from app import worker

    published: list[UUID] = []

    async def fake_publish(_redis: Any, device_id: UUID, _envelope: dict[str, Any]) -> None:
        published.append(device_id)

    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(worker, "publish_job", fake_publish)
            count = await worker.inventory_collect_sweep_org(
                org_id, _engine(tmp_path), app_engine, redis, now=now
            )
    finally:
        await redis.aclose()

    assert count == 2
    assert set(published) == {never, stale}
    for skipped in (recent_ok, recent_failed, in_flight, offline, revoked, no_agent):
        assert skipped not in published

    queued = [j for j in await _jobs(app_engine, org_id, t) if j.status is JobStatus.queued]
    assert {j.target_id for j in queued} == {never, stale}
    assert all(j.signature for j in queued)
    assert all(j.requested_by is None for j in queued)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        audited = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "inventory.collect_scheduled")
            )
        ).scalars().all()
    assert {a.target_id for a in audited} == {str(never), str(stale)}
    assert all(a.actor_type == "system" and a.meta["trigger"] == "scheduled" for a in audited)


async def test_inventory_sweep_does_not_duplicate_on_the_next_pass(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id = await _org(admin_engine)
    now = datetime.now(UTC)
    await _device(admin_engine, org_id)
    job_engine = _engine(tmp_path)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        first = await inventory_schedule.dispatch_scheduled(session, job_engine, org_id, now=now)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        second = await inventory_schedule.dispatch_scheduled(
            session, job_engine, org_id, now=now + timedelta(minutes=30)
        )
    assert len(first) == 1
    assert second == []


async def test_inventory_sweep_is_capped_and_oldest_first(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spread: a pass takes at most the cap, and never-collected devices lead,
    then the longest-uncollected."""
    monkeypatch.setattr(inventory_schedule, "MIN_PER_PASS", 2)
    org_id = await _org(admin_engine)
    now = datetime.now(UTC)
    older = await _device(admin_engine, org_id)
    newer = await _device(admin_engine, org_id)
    never = await _device(admin_engine, org_id)
    t = "inventory.collect"
    await _job(admin_engine, org_id, older, t, status=JobStatus.succeeded,
               issued_at=now - timedelta(days=3), finished_at=now - timedelta(days=3))
    await _job(admin_engine, org_id, newer, t, status=JobStatus.succeeded,
               issued_at=now - timedelta(days=1), finished_at=now - timedelta(days=1))

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        due = await inventory_schedule.due_device_ids(session, org_id, now=now)
    assert due == [never, older]


async def test_inventory_cadence_follows_the_org_policy(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    default_org = await _org(admin_engine)
    weekly_org = await _org(admin_engine)
    junk_org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(Policy(org_id=weekly_org, module="inventory", config={"interval_hours": 168}))
        session.add(Policy(org_id=junk_org, module="inventory", config={"interval_hours": "x"}))
        await session.commit()

    got = {}
    for org_id in (default_org, weekly_org, junk_org):
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            got[org_id] = await inventory_schedule.collect_every(session, org_id)
    assert got[default_org] == timedelta(hours=20)
    assert got[weekly_org] == timedelta(hours=140)
    assert got[junk_org] == timedelta(hours=20)


def test_per_pass_cap_covers_the_fleet_within_a_day() -> None:
    assert inventory_schedule.per_pass_cap(0) == inventory_schedule.MIN_PER_PASS
    fleet = 1000
    cap = inventory_schedule.per_pass_cap(fleet)
    assert cap * inventory_schedule.PASSES_PER_FLEET >= fleet
    assert cap < fleet


async def test_inventory_due_devices_are_scoped_by_the_applications_own_predicate(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """On a BYPASSRLS session only the `org_id` predicates scope the result."""
    org_a = await _org(admin_engine)
    org_b = await _org(admin_engine)
    mine = await _device(admin_engine, org_a)
    theirs = await _device(admin_engine, org_b)
    due = await inventory_schedule.due_device_ids(
        bypassrls_session, org_a, now=datetime.now(UTC)
    )
    assert mine in due
    assert theirs not in due


# ---------------------------------------------------------------------------
# inventory.collect -- first connection after enrolment
# ---------------------------------------------------------------------------


async def test_first_collect_is_queued_once_per_enrolment(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id = await _org(admin_engine)
    now = datetime.now(UTC)
    enrolled = now - timedelta(minutes=1)
    device_id = await _device(admin_engine, org_id, enrolled_at=enrolled)
    # A collect from BEFORE this enrolment (a previous life of the machine)
    # does not count.
    await _job(admin_engine, org_id, device_id, "inventory.collect",
               status=JobStatus.succeeded, issued_at=now - timedelta(days=10),
               finished_at=now - timedelta(days=10))
    job_engine = _engine(tmp_path)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        first = await inventory_schedule.queue_first_collect(
            session, job_engine, org_id, device_id
        )
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        again = await inventory_schedule.queue_first_collect(
            session, job_engine, org_id, device_id
        )

    assert first is not None
    assert first.status is JobStatus.queued and first.signature
    assert again is None
    queued = [
        j for j in await _jobs(app_engine, org_id, "inventory.collect")
        if j.status is JobStatus.queued
    ]
    assert [j.target_id for j in queued] == [device_id]


async def test_first_collect_refuses_a_revoked_agent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id = await _org(admin_engine)
    device_id = await _device(admin_engine, org_id, revoked=True)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert (
            await inventory_schedule.queue_first_collect(
                session, _engine(tmp_path), org_id, device_id
            )
            is None
        )


# ---------------------------------------------------------------------------
# logs.ship
# ---------------------------------------------------------------------------


async def _set_ship_policy(admin_engine: AsyncEngine, org_id: UUID, interval: Any) -> None:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Policy(
                org_id=org_id,
                module="security_logs",
                config={"retention_days": 365, "ship_interval_s": interval},
            )
        )
        await session.commit()


async def test_ship_interval_reads_the_policy_and_clamps_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    no_policy = await _org(admin_engine)
    long_policy = await _org(admin_engine)
    short_policy = await _org(admin_engine)
    junk_policy = await _org(admin_engine)
    await _set_ship_policy(admin_engine, long_policy, 3600)
    await _set_ship_policy(admin_engine, short_policy, 60)
    await _set_ship_policy(admin_engine, junk_policy, "soon")

    results = {}
    for org_id in (no_policy, long_policy, short_policy, junk_policy):
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            results[org_id] = await logs_schedule.ship_interval_s(session, org_id)

    floor = logs_schedule.MIN_SHIP_INTERVAL_S
    assert results[no_policy] == floor
    assert results[long_policy] == 3600
    assert results[short_policy] == floor
    assert results[junk_policy] == floor


async def test_logs_ship_sweep_targets_windows_and_macos_only_without_duplicates(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id = await _org(admin_engine)
    await _set_ship_policy(admin_engine, org_id, 3600)
    now = datetime.now(UTC)
    win = await _device(admin_engine, org_id, os_family="windows")
    mac = await _device(admin_engine, org_id, os_family="macos")
    linux = await _device(admin_engine, org_id, os_family="linux")
    offline = await _device(admin_engine, org_id, agent_status="offline")
    recently_shipped = await _device(admin_engine, org_id)
    long_ago = await _device(admin_engine, org_id, os_family="macos")
    in_flight_old = await _device(admin_engine, org_id)
    t = "logs.ship"
    await _job(admin_engine, org_id, recently_shipped, t, status=JobStatus.succeeded,
               issued_at=now - timedelta(minutes=30), finished_at=now - timedelta(minutes=29))
    await _job(admin_engine, org_id, long_ago, t, status=JobStatus.succeeded,
               issued_at=now - timedelta(hours=2), finished_at=now - timedelta(hours=2))
    # Issued before the interval but still running and unexpired: in flight.
    await _job(admin_engine, org_id, in_flight_old, t, status=JobStatus.running,
               issued_at=now - timedelta(hours=2), expires_at=now + timedelta(hours=1))

    from app import worker

    published: list[UUID] = []

    async def fake_publish(_redis: Any, device_id: UUID, _envelope: dict[str, Any]) -> None:
        published.append(device_id)

    job_engine = _engine(tmp_path)
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(worker, "publish_job", fake_publish)
            first = await worker.logs_ship_sweep_org(
                org_id, job_engine, app_engine, redis, now=now
            )
            second = await worker.logs_ship_sweep_org(
                org_id, job_engine, app_engine, redis, now=now + timedelta(minutes=5)
            )
    finally:
        await redis.aclose()

    assert first == 3
    assert set(published) == {win, mac, long_ago}
    assert second == 0
    for skipped in (linux, offline, recently_shipped, in_flight_old):
        assert skipped not in published

    queued = [j for j in await _jobs(app_engine, org_id, t) if j.status is JobStatus.queued]
    assert len(queued) == 3
    for job in queued:
        assert job.signature
        # TTL is one interval, so an undelivered job cannot overlap the next.
        assert job.expires_at - job.issued_at == timedelta(seconds=3600)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        audited = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "security_logs.ship_scheduled")
            )
        ).scalars().all()
    assert [a.meta["count"] for a in audited] == [3]


async def test_logs_ship_due_devices_are_scoped_by_the_applications_own_predicate(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    org_a = await _org(admin_engine)
    org_b = await _org(admin_engine)
    mine = await _device(admin_engine, org_a)
    theirs = await _device(admin_engine, org_b)
    due = await logs_schedule.due_device_ids(
        bypassrls_session, org_a, now=datetime.now(UTC), interval_s=900
    )
    assert mine in due
    assert theirs not in due


async def test_ship_interval_ignores_another_orgs_policy(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    org_a = await _org(admin_engine)
    org_b = await _org(admin_engine)
    await _set_ship_policy(admin_engine, org_b, 7200)
    assert await logs_schedule.ship_interval_s(bypassrls_session, org_a) == (
        logs_schedule.MIN_SHIP_INTERVAL_S
    )


# ---------------------------------------------------------------------------
# beat wiring
# ---------------------------------------------------------------------------


def test_both_sweeps_are_scheduled(pg_cluster: Any, tmp_path: Path) -> None:
    from app.core.config import Settings, override_settings
    from app.worker import create_celery

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url=REDIS_URL,
    )
    with override_settings(settings):
        schedule = create_celery().conf.beat_schedule

    inv = schedule["inventory-collect-sweep"]
    assert inv["task"] == "app.worker.inventory_collect_sweep"
    assert inv["schedule"] == inventory_schedule.SWEEP_INTERVAL_S
    logs = schedule["logs-ship-sweep"]
    assert logs["task"] == "app.worker.logs_ship_sweep"
    assert logs["schedule"] == logs_schedule.SWEEP_INTERVAL_S
    assert logs_schedule.SWEEP_INTERVAL_S <= logs_schedule.MIN_SHIP_INTERVAL_S
