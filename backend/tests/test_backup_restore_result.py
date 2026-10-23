"""Tests for `backup.restore` result ingestion + proven-restore status
(docs/modules/backup/spec.md). Mirrors `tests/test_backup_result.py`'s
ingest-side assertions: a `RestoreJob` row dispatch created for a device job
flips to a terminal state on report, a `verify`-mode success with no
mismatch marks the linked `BackupPoint` as proven-restorable
(`restore_tested_at`), and the ingestion is graceful (never raises) when
there's no matching row for the job.

Most tests here call `ingest_restore_result` directly to isolate its own
reconciliation logic. `test_valid_result_through_job_engine_reconciles_and_
queues_ui_event` instead drives the real kernel path (`JobEngine.complete`
-> `app.modules.backup.handler.handle_restore_result` ->
`ingest_restore_result`), so the handler's `BackupRestoreResult(**result)`
parse and `queue_ui_event` call are actually exercised end-to-end, and
`test_both_job_types_registered` checks `backup.run`/`backup.restore` are
both wired after `register_job_types()`.
"""

import datetime as _dt
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.jobs.registry import _registry
from app.models import (
    AuditEvent,
    BackupPoint,
    BackupRepository,
    BackupRun,
    Device,
    Job,
    JobStatus,
    Organization,
    RestoreJob,
)
from app.modules.backup.handler import register_job_types
from app.modules.backup.restore import ingest_restore_result
from app.modules.backup.schema import BackupRestoreResult

# The job-type registry is populated at app-startup time (app.modules.backup.
# register(app), a later task), not merely on import. Register directly so
# this file's own kernel round-trip test doesn't depend on import order,
# mirroring test_backup_result.py's module-level call.
register_job_types()


async def _setup_restore_job(
    admin_engine: AsyncEngine,
    *,
    mode: Literal["restore", "verify"] = "verify",
    state: str = "running",
) -> tuple[UUID, UUID, UUID, Job]:
    """Returns (org_id, backup_point_id, device_id, job) with a dispatched
    `RestoreJob(mode=mode, state=state)` row wired to the job."""
    slug = f"bkrst-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        device = Device(org_id=org.id, hostname="h1", os_family="windows")
        session.add(device)
        await session.flush()

        repo = BackupRepository(
            org_id=org.id,
            name="repo-1",
            storage_ref="s3://bucket/repo-1",
            encryption_key_ref="vault://key-1",
        )
        session.add(repo)
        await session.flush()

        run = BackupRun(
            org_id=org.id,
            device_id=device.id,
            repository_id=repo.id,
            state="success",
            actor_type="system",
            started_at=_dt.datetime.now(_dt.UTC),
        )
        session.add(run)
        await session.flush()

        point = BackupPoint(
            org_id=org.id,
            device_id=device.id,
            repository_id=repo.id,
            backup_run_id=run.id,
            manifest_ref="s3://bucket/repo-1/manifests/x",
            size=1000,
            state="complete",
        )
        session.add(point)
        await session.flush()

        job = Job(
            org_id=org.id,
            type="backup.restore",
            target_type="device",
            target_id=device.id,
            params={},
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
            signature="fake-signature",
        )
        session.add(job)
        await session.flush()

        session.add(
            RestoreJob(
                org_id=org.id,
                device_id=device.id,
                backup_point_id=point.id,
                job_id=job.id,
                selection={"paths": ["/a", "/b"]},
                target={"path": "/restore"},
                mode=mode,
                state=state,
                actor_type="system",
            )
        )
        await session.flush()

        await session.commit()
        return org.id, point.id, device.id, job


async def test_verify_success_no_mismatch_marks_point_proven(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, point_id, device_id, job = await _setup_restore_job(admin_engine, mode="verify")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRestoreResult(
            state="success", restored_bytes=1000, verified=True, mismatch=None
        )
        await ingest_restore_result(session, job, result)

        rj = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job.id))
        ).scalar_one()
        assert rj.state == "success"
        assert rj.restored_bytes == 1000
        assert rj.finished_at is not None

        point = (
            await session.execute(select(BackupPoint).where(BackupPoint.id == point_id))
        ).scalar_one()
        assert point.restore_tested_at is not None
        # The restore named paths, so it proved a sample, not the whole point.
        assert (point.restore_test_level, point.restore_test_scope) == ("read", "sampled")

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.restore_result")
            )
        ).scalar_one()
        assert event.actor_type == "system"
        assert event.actor_id is None
        assert event.target_type == "device"
        assert event.target_id == str(device_id)
        assert event.meta["state"] == "success"
        assert event.meta["verified"] is True
        assert event.meta["mismatch_count"] == 0


async def test_verify_success_with_mismatch_does_not_mark_point(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, point_id, _device_id, job = await _setup_restore_job(admin_engine, mode="verify")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRestoreResult(
            state="success", restored_bytes=1000, verified=True, mismatch=["/a"]
        )
        await ingest_restore_result(session, job, result)

        rj = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job.id))
        ).scalar_one()
        assert rj.state == "success"  # the restore job itself still succeeds

        point = (
            await session.execute(select(BackupPoint).where(BackupPoint.id == point_id))
        ).scalar_one()
        assert point.restore_tested_at is None  # NOT proven-restorable


async def test_restore_mode_success_does_not_mark_point(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, point_id, _device_id, job = await _setup_restore_job(admin_engine, mode="restore")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRestoreResult(
            state="success", restored_bytes=1000, verified=True, mismatch=None
        )
        await ingest_restore_result(session, job, result)

        rj = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job.id))
        ).scalar_one()
        assert rj.state == "success"

        point = (
            await session.execute(select(BackupPoint).where(BackupPoint.id == point_id))
        ).scalar_one()
        assert point.restore_tested_at is None  # only "verify" mode proves restorability


async def test_failed_result_fails_restore_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, point_id, _device_id, job = await _setup_restore_job(admin_engine, mode="verify")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRestoreResult(
            state="failed", restored_bytes=0, verified=False, error="disk full"
        )
        await ingest_restore_result(session, job, result)

        rj = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job.id))
        ).scalar_one()
        assert rj.state == "failed"
        assert rj.error == "disk full"
        assert rj.finished_at is not None

        point = (
            await session.execute(select(BackupPoint).where(BackupPoint.id == point_id))
        ).scalar_one()
        assert point.restore_tested_at is None


async def test_out_of_domain_state_coerced_to_failed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`BackupRestoreResult.state` is itself typed `Literal["success",
    "failed"]`, so this exercises defense in depth against a value that
    bypassed the wire model's own validation, mirroring
    `ingest_backup_result`'s out-of-domain coercion: the restore job must
    still land on a terminal `failed` state — never raise, never wedge the
    job — with the original value recorded in the audit metadata."""
    org_id, _point_id, _device_id, job = await _setup_restore_job(admin_engine, mode="verify")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRestoreResult.model_construct(
            state="running",  # type: ignore[arg-type]  # deliberately out-of-domain
            restored_bytes=0,
            verified=False,
            mismatch=None,
            error=None,
        )
        await ingest_restore_result(session, job, result)  # must not raise

        rj = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job.id))
        ).scalar_one()
        assert rj.state == "failed"  # coerced, not "running" verbatim
        assert "running" in (rj.error or "")

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.restore_result")
            )
        ).scalar_one()
        assert event.meta["unexpected_result_state"] == "running"


async def test_audit_metadata_carries_mismatch_count_not_paths(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Invariant #3: no file/path data in the audit log — only the count."""
    org_id, _point_id, _device_id, job = await _setup_restore_job(admin_engine, mode="verify")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRestoreResult(
            state="success",
            restored_bytes=1000,
            verified=True,
            mismatch=["/secret/a", "/secret/b"],
        )
        await ingest_restore_result(session, job, result)

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.restore_result")
            )
        ).scalar_one()
        assert event.meta["mismatch_count"] == 2
        meta_str = str(event.meta)
        assert "/secret/a" not in meta_str
        assert "/secret/b" not in meta_str
        assert "mismatch" not in event.meta or event.meta.get("mismatch") is None


async def test_no_matching_restore_job_row_is_a_graceful_no_op(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """An agent report for a job with no dispatch-created `RestoreJob` row
    must never raise — the agent's report should never be able to fail the
    job over server-side bookkeeping."""
    org_id, _point_id, _device_id, job = await _setup_restore_job(admin_engine, mode="verify")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        rj = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job.id))
        ).scalar_one()
        rj.job_id = None
        await session.flush()

        result = BackupRestoreResult(
            state="success", restored_bytes=1000, verified=True, mismatch=None
        )
        await ingest_restore_result(session, job, result)  # must not raise

        events = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.restore_result")
            )
        ).scalars().all()
        assert events == []


async def test_another_orgs_job_row_is_invisible_to_the_result_session_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A job/restore-job row belonging to org A must not be reachable/
    updatable through org B's session, even when handed org A's Job object
    directly."""
    org_a, _point_a, _device_a, job_a = await _setup_restore_job(admin_engine, mode="verify")
    org_b, _point_b, _device_b, _job_b = await _setup_restore_job(admin_engine, mode="verify")

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRestoreResult(
            state="success", restored_bytes=1000, verified=True, mismatch=None
        )
        # job_a.org_id is org A's id; the RLS-scoped session belongs to org B.
        await ingest_restore_result(session, job_a, result)

        rows = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job_a.id))
        ).scalars().all()
        assert rows == []  # org B's RLS-scoped session cannot see org A's row

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        rj = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job_a.id))
        ).scalar_one()
        assert rj.state == "running"  # untouched by the org B attempt


def test_both_job_types_registered() -> None:
    assert "backup.run" in _registry
    assert "backup.restore" in _registry


# ---------------------------------------------------------------------------
# kernel round-trip: every other test in this file calls ingest_restore_result
# directly, which never exercises app.modules.backup.handler's
# BackupRestoreResult(**result) parse or its queue_ui_event call. This test
# drives the real job_engine.complete() path instead, mirroring
# test_backup_result.py's test_valid_result_through_job_engine_reconciles_
# and_queues_ui_event.
# ---------------------------------------------------------------------------


async def test_valid_result_through_job_engine_reconciles_and_queues_ui_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, point_id, device_id, job = await _setup_restore_job(admin_engine, mode="verify")
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
        await engine.mark_dispatched(session, job)
        await engine.complete(
            session,
            job,
            state="succeeded",
            result={
                "state": "success",
                "restored_bytes": 1000,
                "verified": True,
                "mismatch": None,
            },
        )
        assert job.status is JobStatus.succeeded

        rj = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job.id))
        ).scalar_one()
        assert rj.state == "success"

        point = (
            await session.execute(select(BackupPoint).where(BackupPoint.id == point_id))
        ).scalar_one()
        assert point.restore_tested_at is not None

        # the handler queued a post-commit UI event on the session
        queued = session.info.get("ui_events", [])
        assert [e["kind"] for _, e in queued] == ["backup_status_updated"]
        assert queued[0][1]["device_id"] == str(device_id)
        assert queued[0][1]["job_id"] == str(job.id)
