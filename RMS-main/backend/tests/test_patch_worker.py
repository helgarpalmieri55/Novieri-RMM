"""Tests for the patch-management worker sweeps (Task 11):

- ``patch_scan_sweep_org`` / ``patch_scan_sweep_all_orgs`` -- scan-cadence:
  dispatch a signed ``patch.scan`` job to a device whose latest scan is
  missing or stale relative to its resolved policy.
- ``patch_apply_sweep_all_orgs`` -- window-apply: fan `dispatch_apply` out
  across every org and sum the dispatched counts.

The per-org scan test exercises ``patch_scan_sweep_org`` directly (mirrors
``test_worker.py``'s direct use of ``sweep_org``) so it only ever touches the
one org's devices -- the pg_cluster database is shared session-wide across
every test file (see ``test_remote_retention_worker.py``), so sweeping
*all* orgs for real would also dispatch scans for every device any other
test file has created. The apply-sweep test exercises the real all-orgs fan-
out (that's the behavior under test) but monkeypatches ``dispatch_apply`` and
therefore asserts the same summation-invariant style as
``test_remote_retention_worker.py`` rather than an absolute total.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, override_settings
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.jobs.registry import UnknownJobType, get_job_type
from app.jobs.registry import _registry as _job_registry
from app.models import (
    Device,
    DevicePatchStatus,
    Job,
    JobStatus,
    Organization,
    Patch,
    PatchPolicy,
)
from app.modules.patch.handler import register_job_types

REDIS_URL = "redis://localhost:6379/9"

# The job-type registry is populated at app-startup time (app.modules.patch.
# register(app)), not merely on import. Register directly so this file's
# job_engine.create(type="patch.scan") calls succeed even when this test
# module runs standalone, before any fixture builds the FastAPI app.
register_job_types()


async def _make_org(admin_engine: AsyncEngine, slug_prefix: str) -> UUID:
    slug = f"{slug_prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _make_agent(
    admin_engine: AsyncEngine, org_id: UUID, device_id: UUID, *, status: str, now: datetime
) -> None:
    """An enrolled agent for a device.

    EVERY DEVICE IN THESE TESTS NEEDS ONE NOW, and that is the point of the
    2026-08-20 change rather than an accident of the fixture: a device with no
    `agents` row has nothing that can execute a signed job, so dispatching to
    it produces a job that expires unread. The cadence tests below gained this
    call when the sweep learned to check.
    """
    from app.models import Agent

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Agent(
                org_id=org_id,
                device_id=device_id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=now + timedelta(days=365),
                enrolled_at=now - timedelta(days=30),
                status=status,
                last_seen_at=now,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# patch_scan_sweep_org (scan-cadence)
# ---------------------------------------------------------------------------


async def test_scan_sweep_dispatches_never_scanned_and_skips_recent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id = await _make_org(admin_engine, "scn")
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        # org-default policy: scan every 24h
        session.add(PatchPolicy(org_id=org_id, scan_interval_hours=24))
        never_scanned = Device(org_id=org_id, hostname="never", os_family="linux")
        recently_scanned = Device(org_id=org_id, hostname="recent", os_family="linux")
        stale_scanned = Device(org_id=org_id, hostname="stale", os_family="linux")
        session.add_all([never_scanned, recently_scanned, stale_scanned])
        await session.flush()
        never_id, recent_id, stale_id = (
            never_scanned.id,
            recently_scanned.id,
            stale_scanned.id,
        )
        await session.commit()

    # ONLINE AGENTS, so this test still measures what it was written to
    # measure. It predates the connectivity guard; without them the sweep now
    # correctly skips all three and the cadence assertions below would pass
    # for the wrong reason.
    for device_id in (never_id, recent_id, stale_id):
        await _make_agent(admin_engine, org_id, device_id, status="online", now=now)

    # WHAT DATES A SCAN IS A SUCCEEDED `patch.scan` JOB, not a status row.
    # This test read `DevicePatchStatus.detected_at` until 2026-08-21, which
    # dates the discovery of a patch rather than the running of a scan; see the
    # cadence section at the foot of this file. The status rows below stay
    # because a scanned device realistically has some, and they are stamped to
    # AGREE with the jobs here so this test measures the three-way cadence and
    # nothing else -- the tests that pull the two apart are down there.
    await _succeeded_scan(admin_engine, org_id, recent_id, finished_at=now - timedelta(hours=1))
    await _succeeded_scan(admin_engine, org_id, stale_id, finished_at=now - timedelta(hours=48))

    async with org_session(org_id, engine=app_engine) as session:
        patch = Patch(
            org_id=org_id,
            os_family="linux",
            source="apt",
            identifier="pkg1",
            title="pkg1 update",
            severity="important",
        )
        session.add(patch)
        await session.flush()
        session.add(
            DevicePatchStatus(
                org_id=org_id,
                device_id=recent_id,
                patch_id=patch.id,
                state="missing",
                detected_at=now - timedelta(hours=1),  # within the 24h interval
            )
        )
        session.add(
            DevicePatchStatus(
                org_id=org_id,
                device_id=stale_id,
                patch_id=patch.id,
                state="missing",
                detected_at=now - timedelta(hours=48),  # older than the 24h interval
            )
        )
        await session.flush()

    from app import worker

    dispatched_device_ids: list[UUID] = []

    async def fake_publish_job(redis, device_id, envelope):
        dispatched_device_ids.append(device_id)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "publish_job", fake_publish_job)

        job_engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
        redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        try:
            count = await worker.patch_scan_sweep_org(
                org_id, job_engine, app_engine, redis, now=now
            )
        finally:
            await redis.aclose()

    assert count == 2
    assert set(dispatched_device_ids) == {never_id, stale_id}
    assert recent_id not in dispatched_device_ids

    async with org_session(org_id, engine=app_engine) as session:
        jobs = (
            await session.execute(
                select(Job).where(Job.type == "patch.scan", Job.status == JobStatus.queued)
            )
        ).scalars().all()
        assert {j.target_id for j in jobs} == {never_id, stale_id}
        for job in jobs:
            assert job.signature  # never dispatch an unsigned job


async def test_scan_sweep_all_orgs_delegates_per_org(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    """patch_scan_sweep_all_orgs is a thin fan-out around patch_scan_sweep_org
    (mirrors sweep_all_orgs/sweep_org); confirm the wiring by monkeypatching
    the per-org function and checking it's invoked once per org with `now`
    threaded through."""
    from app import worker

    org_a = await _make_org(admin_engine, "scnall-a")
    org_b = await _make_org(admin_engine, "scnall-b")

    calls: list[tuple[UUID, datetime]] = []

    async def fake_sweep_org(org_id, job_engine, app_engine, redis, *, now):
        calls.append((org_id, now))
        return 1

    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "patch_scan_sweep_org", fake_sweep_org)
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
            total = await worker.patch_scan_sweep_all_orgs(now)

    visited = {org_id: n for org_id, n in calls}
    assert org_a in visited
    assert org_b in visited
    assert visited[org_a] == now
    assert visited[org_b] == now
    assert total == sum(1 for _ in calls)


# ---------------------------------------------------------------------------
# patch_apply_sweep_all_orgs (window-apply)
# ---------------------------------------------------------------------------


async def test_apply_sweep_visits_all_orgs_and_sums_dispatch_apply_counts(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    """Mirrors test_remote_retention_worker.py's summation-invariant style:
    the pg_cluster database is shared session-wide, so other test files'
    orgs are already sitting in it. We don't assert an absolute total --
    only that the two orgs we create are among those visited, and that the
    total equals the sum of what (the monkeypatched) dispatch_apply returned
    per org actually visited."""
    from app import worker

    calls: list[UUID] = []

    async def fake_dispatch_apply(session, job_engine, *, org_id, now, device_ids=None):
        calls.append(org_id)
        # fixed count (2) per org, regardless of org — the pending
        # (job_id, device_id, envelope) triples the sweep now accumulates and
        # publishes post-commit
        return [
            (uuid4(), uuid4(), {"v": 1, "kind": "job", "job": {}, "sig": "x"}),
            (uuid4(), uuid4(), {"v": 1, "kind": "job", "job": {}, "sig": "x"}),
        ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "dispatch_apply", fake_dispatch_apply)

        org_a = await _make_org(admin_engine, "aps-a")
        org_b = await _make_org(admin_engine, "aps-b")

        now = datetime(2026, 7, 19, 3, 0, tzinfo=UTC)
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
            total = await worker.patch_apply_sweep_all_orgs(now)

    assert {org_a, org_b} <= set(calls)
    assert total == 2 * len(calls)


# ---------------------------------------------------------------------------
# worker bootstrap registers patch job types (Finding 1: the Celery
# worker+beat process never called register_job_types(), so the first sweep
# to fire in production would raise UnknownJobType). This file's own
# module-level `register_job_types()` call above (mirroring the pre-existing
# import-time registration) would mask that bug for every other test in this
# file, so this test explicitly deletes the registry entries first and drives
# the worker's own bootstrap path (`register_worker_job_types`/
# `create_celery`) rather than relying on the import-time call.
# ---------------------------------------------------------------------------


def test_worker_bootstrap_registers_patch_job_types() -> None:
    from app import worker

    for name in ("patch.scan", "patch.apply"):
        _job_registry.pop(name, None)

    with pytest.raises(UnknownJobType):
        get_job_type("patch.scan")

    worker.register_worker_job_types()

    assert get_job_type("patch.scan").name == "patch.scan"
    assert get_job_type("patch.apply").name == "patch.apply"


def test_create_celery_registers_patch_job_types(pg_cluster, tmp_path: Path) -> None:
    """create_celery() -- what actually runs when the worker process builds
    its Celery app -- must also leave the registry populated, since that's
    the real bootstrap path in production (not register_worker_job_types()
    called directly)."""
    from app import worker

    for name in ("patch.scan", "patch.apply"):
        _job_registry.pop(name, None)

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
        worker.create_celery()

    assert get_job_type("patch.scan").name == "patch.scan"
    assert get_job_type("patch.apply").name == "patch.apply"


# --- the scan storm (2026-08-20) -------------------------------------------
#
# REPORTED FROM THE LIVE DEPLOYMENT, and the operator's question is the right
# one: "there's a running job trying to find patches on that device and the job
# is running endlessly, if it failed 2 times it must stop trying, even more if
# the device says is offline, why keep trying?"
#
# 29 `patch.scan` jobs, one per hour, every one `expired` with 0 attempts,
# against a single device that had been offline for two days.
#
# The sweep's only condition was "the last SUCCESSFUL scan is older than the
# policy interval". A device that cannot answer never produces a successful
# scan, so that condition is permanently true and the sweep re-dispatched for
# ever. Each pass is a signature, an audit-chain row and a Redis publish; on a
# fleet with 500 machines off overnight that is 500 of each, every hour,
# describing work nothing can do.


async def _one_device_org(
    admin_engine: AsyncEngine, prefix: str, *, agent_status: str, now: datetime
) -> tuple[UUID, UUID]:
    """An org with a 24h scan policy, one device, and an agent in `agent_status`."""
    org_id = await _make_org(admin_engine, prefix)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(PatchPolicy(org_id=org_id, scan_interval_hours=24))
        device = Device(org_id=org_id, hostname=prefix, os_family="linux")
        session.add(device)
        await session.flush()
        device_id = device.id
        await session.commit()
    await _make_agent(admin_engine, org_id, device_id, status=agent_status, now=now)
    return org_id, device_id


async def _sweep(org_id: UUID, app_engine: AsyncEngine, tmp_path: Path, now: datetime) -> int:
    from app import worker

    async def fake_publish_job(redis, device_id, envelope):
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "publish_job", fake_publish_job)
        job_engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
        redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        try:
            return await worker.patch_scan_sweep_org(
                org_id, job_engine, app_engine, redis, now=now
            )
        finally:
            await redis.aclose()


async def _expire_scan_jobs(app_engine: AsyncEngine, org_id: UUID, before: datetime) -> None:
    """Age every `patch.scan` job so it has expired as of `before`.

    WHY A TEST HAS TO ARRANGE THIS RATHER THAN WAIT FOR IT. `JobEngine.create`
    takes no clock: it stamps `expires_at` from the real one. The sweep compares
    `Job.expires_at > now` against the `now` a caller INJECTS. So a test that
    advances a fake clock two days does not age the job it created -- it only
    moves the comparison point, and the job's real `expires_at` sits wherever
    wall-clock time happens to be.

    That made `test_an_expired_scan_does_not_block_the_next_one_for_ever` a test
    that expires. It pinned `now` to 2026-08-20 and swept at `now + 2 days`,
    which passed for as long as real time was near 2026-08-20 and began failing
    the moment real time moved past 2026-08-22 12:00 -- no code change, no
    commit, just the calendar. Bisecting it finds nothing, because nothing broke.
    """
    from app.models import Job

    async with org_session(org_id, engine=app_engine) as session:
        await session.execute(
            update(Job)
            .where(Job.org_id == org_id, Job.type == "patch.scan")
            .values(expires_at=before - timedelta(seconds=1))
        )
        await session.commit()


async def _scan_jobs(app_engine: AsyncEngine, org_id: UUID) -> list[Any]:
    from app.models import Job

    async with org_session(org_id, engine=app_engine) as session:
        return list(
            (
                await session.execute(
                    select(Job).where(Job.org_id == org_id, Job.type == "patch.scan")
                )
            ).scalars()
        )


async def test_no_scan_is_dispatched_to_an_agent_that_is_not_connected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """THE OPERATOR'S SECOND QUESTION -- "even more if the device says is
    offline, why keep trying?" -- and it is the cheaper of the two guards: a
    job for a disconnected agent is not delayed work, it is work that will
    expire unread."""
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    org_id, _ = await _one_device_org(admin_engine, "storm-off", agent_status="offline", now=now)

    assert await _sweep(org_id, app_engine, tmp_path, now) == 0
    assert await _scan_jobs(app_engine, org_id) == []


async def test_a_reconnected_agent_is_scanned_on_the_next_sweep(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """NON-VACUITY FOR THE GUARD ABOVE, and the property that makes skipping
    safe rather than starving: the device is not struck off, it is passed over
    while it cannot answer. The sweep runs hourly, so coming back costs at most
    one interval -- against 24 dead jobs a day for the old behaviour."""
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    org_id, device_id = await _one_device_org(
        admin_engine, "storm-back", agent_status="offline", now=now
    )
    assert await _sweep(org_id, app_engine, tmp_path, now) == 0

    from app.models import Agent

    async with AsyncSession(admin_engine) as session:
        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
        agent.status = "online"
        await session.commit()

    assert await _sweep(org_id, app_engine, tmp_path, now) == 1


async def test_a_scan_already_outstanding_is_not_dispatched_again(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """THE SECOND GUARD, covering what the first cannot: an agent that is
    online but slow, or whose scan is genuinely still running. `last_scan` only
    moves when a scan SUCCEEDS, so between dispatch and completion the sweep's
    condition stays true and it would queue a second job for work already in
    flight."""
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    org_id, _ = await _one_device_org(admin_engine, "storm-dup", agent_status="online", now=now)
    assert await _sweep(org_id, app_engine, tmp_path, now) == 1

    again = await _sweep(org_id, app_engine, tmp_path, now + timedelta(minutes=1))

    assert again == 0
    assert len(await _scan_jobs(app_engine, org_id)) == 1


async def test_an_expired_scan_does_not_block_the_next_one_for_ever(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """THE OTHER HALF OF THE OUTSTANDING-JOB GUARD, and without this test that
    guard could be written as "never dispatch if any scan job exists", which
    would silently stop patch scanning across the fleet for good. Only an
    UNEXPIRED job counts."""
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    org_id, _ = await _one_device_org(admin_engine, "storm-exp", agent_status="online", now=now)
    assert await _sweep(org_id, app_engine, tmp_path, now) == 1

    # The outstanding job is dead, so the device is due. Its expiry is ARRANGED
    # rather than waited for -- see `_expire_scan_jobs` for why advancing the
    # fake clock alone does not age a job stamped from the real one.
    later = now + timedelta(days=2)
    await _expire_scan_jobs(app_engine, org_id, later)

    assert await _sweep(org_id, app_engine, tmp_path, later) == 1


# --- the cadence read backwards (2026-08-21) -------------------------------
#
# The sweep asked `MAX(device_patch_status.detected_at)` when it meant "when
# did this device last complete a scan". Those are different facts, and on the
# machines that matter most they disagree permanently: `detected_at` is stamped
# when a status ROW IS FIRST CREATED (`patch/ingest.py` §3b) and never again,
# so a device whose patch set has not changed since enrolment mints no new rows
# and its maximum never moves. `scan_interval_hours` was therefore dead in
# exactly one direction -- a settled machine was re-queued on EVERY hourly beat
# (24x its 24h policy), while a churning one, minting fresh rows each scan,
# looked freshly measured and was scanned less eagerly.
#
# `reporting.last_successful_scan_at` is the fact the sweep wanted: MAX
# `jobs.finished_at` over `succeeded` `patch.scan` jobs.


async def _succeeded_scan(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    *,
    finished_at: datetime,
) -> None:
    """A completed `patch.scan` job -- the durable record that a scan ran AND
    that its document was ingested, since `JobEngine.complete` only flips a job
    to `succeeded` after the handler returns without raising. Mirrors
    `tests/test_patch_reporting.py::_scan_job`.

    Deliberately terminal, so the sweep's outstanding-job guard cannot be what
    makes these tests pass: `succeeded` is not in
    (queued, dispatched, running).
    """
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Job(
                org_id=org_id,
                type="patch.scan",
                target_type="device",
                target_id=device_id,
                params={},
                nonce=uuid4().hex,
                issued_at=finished_at - timedelta(minutes=5),
                expires_at=finished_at + timedelta(hours=1),
                status=JobStatus.succeeded,
                finished_at=finished_at,
            )
        )
        await session.commit()


async def _settled_patch_set(
    app_engine: AsyncEngine, org_id: UUID, device_id: UUID, *, detected_at: datetime
) -> None:
    """A device whose patch set has not changed since `detected_at`: one status
    row, stamped once, never rewritten. This is the ordinary steady state of a
    managed machine, not an edge case."""
    async with org_session(org_id, engine=app_engine) as session:
        patch = Patch(
            org_id=org_id,
            os_family="linux",
            source="apt",
            identifier=f"pkg-{uuid4().hex[:8]}",
            title="a package update",
            severity="important",
        )
        session.add(patch)
        await session.flush()
        session.add(
            DevicePatchStatus(
                org_id=org_id,
                device_id=device_id,
                patch_id=patch.id,
                state="missing",
                detected_at=detected_at,
            )
        )
        await session.flush()


async def test_a_device_scanned_inside_its_interval_is_not_rescanned(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """THE REGRESSION TEST FOR THE DEFECT: a machine scanned an hour ago under
    a 24h policy, whose patch set has been stable since enrolment.

    Under the old `MAX(detected_at)` read this device was due -- its only
    status row was stamped a month ago and nothing has restamped it -- so the
    sweep signed, audited and published a `patch.scan` on every hourly beat
    while its policy asked for one a day. Revert the fix and this goes red.
    """
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    org_id, device_id = await _one_device_org(
        admin_engine, "cadence-fresh", agent_status="online", now=now
    )
    await _settled_patch_set(
        app_engine, org_id, device_id, detected_at=now - timedelta(days=30)
    )
    await _succeeded_scan(admin_engine, org_id, device_id, finished_at=now - timedelta(hours=1))

    assert await _sweep(org_id, app_engine, tmp_path, now) == 0

    scans = await _scan_jobs(app_engine, org_id)
    assert [job.status for job in scans] == [JobStatus.succeeded]  # only the seeded one


async def test_a_device_past_its_interval_is_rescanned_though_its_patch_set_just_moved(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """THE INVERSE, so the test above cannot pass by never queueing anything --
    and the other half of the defect in its own right.

    This device last completed a scan two days ago and is genuinely overdue,
    but a patch appeared an hour ago, so `MAX(detected_at)` reads as one hour
    old. Under the old code the churn masked the staleness and the sweep stood
    down; the cadence ran fastest on the machines it had least reason to
    re-measure and slowest on the ones it had most.
    """
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    org_id, device_id = await _one_device_org(
        admin_engine, "cadence-stale", agent_status="online", now=now
    )
    await _settled_patch_set(
        app_engine, org_id, device_id, detected_at=now - timedelta(hours=1)
    )
    await _succeeded_scan(admin_engine, org_id, device_id, finished_at=now - timedelta(hours=48))

    assert await _sweep(org_id, app_engine, tmp_path, now) == 1

    queued = [
        job
        for job in await _scan_jobs(app_engine, org_id)
        if job.status != JobStatus.succeeded
    ]
    assert len(queued) == 1
    assert queued[0].target_id == device_id
    assert queued[0].signature  # never dispatch an unsigned job


async def test_a_neighbour_tenants_scan_does_not_satisfy_this_orgs_cadence(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """`jobs.target_id` is a bare UUID with no foreign key, so nothing but an
    explicit `org_id` predicate ties a scan to the tenant whose device it
    measured. Both orgs below hold a `succeeded` `patch.scan` finished one hour
    ago against org A's device id; only org B's own device may be dated by its
    own scan.

    The two halves are what make this non-vacuous: same job type, same
    recency, same target -- different tenant, opposite outcome.
    """
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    org_a, device_a = await _one_device_org(
        admin_engine, "cadence-ten-a", agent_status="online", now=now
    )
    org_b, device_b = await _one_device_org(
        admin_engine, "cadence-ten-b", agent_status="online", now=now
    )

    # Org B's row, pointing at ORG A's device: a foreign tenant's scan.
    await _succeeded_scan(admin_engine, org_b, device_a, finished_at=now - timedelta(hours=1))
    # Org B's own device, genuinely scanned an hour ago.
    await _succeeded_scan(admin_engine, org_b, device_b, finished_at=now - timedelta(hours=1))

    assert await _sweep(org_a, app_engine, tmp_path, now) == 1  # A has never been scanned
    assert await _sweep(org_b, app_engine, tmp_path, now) == 0  # B was, an hour ago

    dispatched = [job for job in await _scan_jobs(app_engine, org_a) if job.org_id == org_a]
    assert [job.target_id for job in dispatched] == [device_a]
