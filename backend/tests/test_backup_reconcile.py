"""Stuck runs (spec-server-backup.md §3.3, defect D4).

The result handler runs only for a `succeeded` job, so a refused, expired or
failed `backup.run` left its run on `running` for ever: invisible to reports and
alerts, and (now) blocking the next scheduled run. The reconcile sweep moves
those rows to `failed` with the reason. Runs on the admin engine for the
tenancy assertions, so the `org_id` predicate is what is being tested, not RLS.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import (
    BackupPoint,
    BackupRepository,
    BackupRun,
    Device,
    Job,
    Organization,
    RestoreJob,
)
from app.models.core import JobStatus
from app.modules.backup.reconcile import reconcile_stuck

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


async def _org(admin_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    slug = f"rec-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        device = Device(
            org_id=org.id, hostname="srv", os_family="linux", client_id=org.id, site_id=org.id
        )
        repo = BackupRepository(org_id=org.id, name="r", storage_ref="x",
                                encryption_key_ref="x", dedup_params={}, state="active")
        s.add_all([device, repo])
        await s.commit()
        return org.id, device.id, repo.id


async def _run_with_job(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    repo_id: UUID,
    *,
    status: JobStatus,
    result: dict | None = None,
    expires_at: datetime | None = None,
    job_type: str = "backup.run",
) -> tuple[UUID, UUID]:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        job = Job(org_id=org_id, type=job_type, target_type="device", target_id=device_id,
                  params={}, nonce=uuid4().hex, issued_at=NOW - timedelta(hours=2),
                  expires_at=expires_at or NOW + timedelta(hours=1), status=status,
                  result=result)
        s.add(job)
        await s.flush()
        run = BackupRun(org_id=org_id, device_id=device_id, repository_id=repo_id,
                        job_id=job.id, state="running", actor_type="system",
                        started_at=NOW - timedelta(hours=2))
        s.add(run)
        await s.commit()
        return run.id, job.id


async def _run(admin_engine: AsyncEngine, run_id: UUID) -> BackupRun:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        return (await s.execute(select(BackupRun).where(BackupRun.id == run_id))).scalar_one()


@pytest.mark.parametrize(
    ("status", "result", "fragment"),
    [
        (JobStatus.refused, {"refused": "unknown_type"}, "unknown_type"),
        (JobStatus.expired, None, "expired"),
        (JobStatus.failed, {"state": "failed", "error": "snapshot: no LVM volume"},
         "snapshot: no LVM volume"),
        (JobStatus.cancelled, None, "cancelled"),
    ],
)
async def test_a_terminal_unsuccessful_job_fails_its_run_with_the_reason(
    admin_engine: AsyncEngine, status: JobStatus, result: dict | None, fragment: str
) -> None:
    org_id, device_id, repo_id = await _org(admin_engine)
    run_id, _ = await _run_with_job(admin_engine, org_id, device_id, repo_id,
                                    status=status, result=result)
    async with AsyncSession(admin_engine) as s:
        counts = await reconcile_stuck(s, org_id, now=NOW)
        await s.commit()
    run = await _run(admin_engine, run_id)
    assert counts["runs"] == 1
    assert run.state == "failed"
    assert run.finished_at == NOW
    assert fragment in (run.error or "")


async def test_a_live_job_leaves_its_run_alone(admin_engine: AsyncEngine) -> None:
    org_id, device_id, repo_id = await _org(admin_engine)
    run_id, _ = await _run_with_job(admin_engine, org_id, device_id, repo_id,
                                    status=JobStatus.running)
    async with AsyncSession(admin_engine) as s:
        counts = await reconcile_stuck(s, org_id, now=NOW)
        await s.commit()
    assert counts["runs"] == 0
    assert (await _run(admin_engine, run_id)).state == "running"


async def test_a_running_job_long_past_its_lifetime_fails_its_run(
    admin_engine: AsyncEngine,
) -> None:
    # The agent died mid-run: nothing will ever report, and the job engine does
    # not expire a running job.
    org_id, device_id, repo_id = await _org(admin_engine)
    run_id, _ = await _run_with_job(admin_engine, org_id, device_id, repo_id,
                                    status=JobStatus.running,
                                    expires_at=NOW - timedelta(hours=2))
    async with AsyncSession(admin_engine) as s:
        await reconcile_stuck(s, org_id, now=NOW)
        await s.commit()
    run = await _run(admin_engine, run_id)
    assert run.state == "failed"
    assert "stopped reporting" in (run.error or "")


async def test_another_orgs_stuck_run_is_not_touched(admin_engine: AsyncEngine) -> None:
    org_a, dev_a, repo_a = await _org(admin_engine)
    org_b, dev_b, repo_b = await _org(admin_engine)
    run_b, _ = await _run_with_job(admin_engine, org_b, dev_b, repo_b,
                                   status=JobStatus.refused, result={"refused": "x"})
    async with AsyncSession(admin_engine) as s:
        await reconcile_stuck(s, org_a, now=NOW)
        await s.commit()
    assert (await _run(admin_engine, run_b)).state == "running"


async def test_a_stuck_restore_job_is_failed_too(admin_engine: AsyncEngine) -> None:
    org_id, device_id, repo_id = await _org(admin_engine)
    run_id, _ = await _run_with_job(admin_engine, org_id, device_id, repo_id,
                                    status=JobStatus.succeeded)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        point = BackupPoint(org_id=org_id, device_id=device_id, repository_id=repo_id,
                            backup_run_id=run_id, manifest_ref="m", size=1, state="complete")
        s.add(point)
        await s.flush()
        job = Job(org_id=org_id, type="backup.restore", target_type="device",
                  target_id=device_id, params={}, nonce=uuid4().hex,
                  issued_at=NOW - timedelta(hours=1), expires_at=NOW - timedelta(minutes=1),
                  status=JobStatus.expired)
        s.add(job)
        await s.flush()
        restore = RestoreJob(org_id=org_id, device_id=device_id, backup_point_id=point.id,
                             job_id=job.id, selection={}, target={}, mode="restore",
                             state="running", actor_type="user")
        s.add(restore)
        await s.commit()
        restore_id = restore.id

    async with AsyncSession(admin_engine) as s:
        counts = await reconcile_stuck(s, org_id, now=NOW)
        await s.commit()
    async with AsyncSession(admin_engine) as s:
        row = (await s.execute(select(RestoreJob).where(RestoreJob.id == restore_id))
               ).scalar_one()
    assert counts["restores"] == 1
    assert row.state == "failed"
    assert "expired" in (row.error or "")
