"""The format-2 wire members, server side (spec-server-backup.md §5.1, §5.3).

Every member is optional, so a format-1 agent validates unchanged; a format-2
result's consistency and warnings are stored on the run and the point, where
the console and the coverage report read them.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from moto import mock_aws
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import BackupPoint, BackupRun, Job, RestoreJob
from app.models.core import JobStatus
from app.modules.backup.dispatch import ingest_backup_result
from app.modules.backup.restore import ingest_restore_result
from app.modules.backup.schema import (
    BackupRestoreParams,
    BackupRestoreResult,
    BackupRunParams,
    BackupRunResult,
)
from tests.backup_offsite_fixtures import local_settings, seed_org

V2_RESULT: dict[str, Any] = {
    "state": "success",
    "manifest_ref": "manifests/abc",
    "bytes_protected": 10,
    "bytes_stored": 5,
    "chunk_count": 1,
    "dedup_stats": {"manifest_pages": 2, "manifest_bytes": 900, "files_changed_during_read": 3},
    "error": None,
    "workload_id": None,
    "warnings": ["not_snapshot_consistent", "files_changed_during_read"],
    "consistency": {
        "level": "live",
        "crash_consistent": False,
        "snapshot": "none",
        "writers": [],
        "files_changed_during_read": 3,
        "non_transactional_tables": 0,
        "databases": [],
    },
}


def test_a_format_1_result_and_params_still_validate() -> None:
    BackupRunResult(state="success", manifest_ref="manifests/x", bytes_protected=1,
                    bytes_stored=1, chunk_count=1)
    params = BackupRunParams(repository_id="r", storage_ref="s", repo_key="k", snapshot="lvm")
    assert params.format == 1 and params.workload is None and params.vss is None


def test_the_format_2_members_validate() -> None:
    BackupRunResult(**V2_RESULT)
    BackupRunParams(repository_id="r", storage_ref="s", repo_key="k", snapshot="auto", format=2,
                    workload={"id": "w", "kind": "files",
                              "config": {"paths": ["/"], "allow_live": True}},
                    vss={"backup_type": "copy", "fail_on_writer": True},
                    not_after_start="2026-09-23T08:00:00Z")
    BackupRestoreParams(repository_id="r", storage_ref="s", manifest_ref="m", selection=["/a"],
                        target={"path": "/restore", "conflict": "rename"}, mode="restore",
                        repo_key="k", format=2, restore_kind="files")
    BackupRestoreResult(state="success", restored_bytes=1, verified=True,
                        warnings=["ownership_not_applied"])


def test_an_undeclared_member_is_still_refused() -> None:
    with pytest.raises(ValidationError):
        BackupRunResult(**{**V2_RESULT, "surprise": 1})


@pytest.fixture()
def store(tmp_path: Path):
    with mock_aws():
        yield ObjectStore(local_settings(tmp_path))


async def _job(admin_engine: AsyncEngine, org: Any, job_type: str) -> Job:
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        job = Job(org_id=org.org_id, type=job_type, target_type="device", target_id=org.device_id,
                  params={}, nonce=uuid4().hex, issued_at=now,
                  expires_at=now + timedelta(hours=1), status=JobStatus.running)
        s.add(job)
        await s.commit()
        return job


async def test_a_live_point_is_stored_as_not_crash_consistent_with_its_warnings(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, store: ObjectStore, tmp_path: Path
) -> None:
    org = await seed_org(admin_engine, app_engine, LocalVaultService(tmp_path / "k"), "f2")
    job = await _job(admin_engine, org, "backup.run")
    async with AsyncSession(admin_engine) as s:
        s.add(BackupRun(org_id=org.org_id, device_id=org.device_id,
                        repository_id=org.repository_id, job_id=job.id, state="running",
                        actor_type="system"))
        await s.commit()
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await ingest_backup_result(session, lambda: store, job, BackupRunResult(**V2_RESULT))
    async with AsyncSession(admin_engine) as s:
        point = (await s.execute(select(BackupPoint).where(
            BackupPoint.org_id == org.org_id))).scalar_one()
        run = (await s.execute(select(BackupRun).where(BackupRun.job_id == job.id))).scalar_one()
    assert (point.consistency_level, point.crash_consistent) == ("live", False)
    assert point.warnings == ["not_snapshot_consistent", "files_changed_during_read"]
    assert point.consistency is not None and point.consistency["files_changed_during_read"] == 3
    assert run.warnings == point.warnings


async def test_restore_warnings_are_kept(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, store: ObjectStore, tmp_path: Path
) -> None:
    org = await seed_org(admin_engine, app_engine, LocalVaultService(tmp_path / "k"), "f2r")
    job = await _job(admin_engine, org, "backup.restore")
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        run = BackupRun(org_id=org.org_id, device_id=org.device_id,
                        repository_id=org.repository_id, state="success", actor_type="system")
        s.add(run)
        await s.flush()
        point = BackupPoint(org_id=org.org_id, device_id=org.device_id,
                            repository_id=org.repository_id, backup_run_id=run.id,
                            manifest_ref="m", size=1, state="complete")
        s.add(point)
        await s.flush()
        s.add(RestoreJob(org_id=org.org_id, device_id=org.device_id, backup_point_id=point.id,
                         job_id=job.id, selection={"paths": ["/a"]}, target={}, mode="restore",
                         state="running", actor_type="user"))
        await s.commit()
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await ingest_restore_result(session, job, BackupRestoreResult(
            state="success", restored_bytes=1, verified=True,
            warnings=["ownership_not_applied"]))
    async with AsyncSession(admin_engine) as s:
        row = (await s.execute(select(RestoreJob).where(RestoreJob.job_id == job.id))
               ).scalar_one()
    assert row.warnings == ["ownership_not_applied"]
