from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import (
    BackupPoint,
    BackupPolicy,
    BackupRepository,
    BackupRun,
    Device,
    Organization,
    RestoreJob,
    User,
)


async def _org_device_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="bk", slug=f"bk-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="windows")
        s.add(dev)
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, dev.id, usr.id


async def test_backup_models_roundtrip(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        repo = BackupRepository(
            org_id=org_id,
            name="primary",
            storage_ref="s3://bucket/org",
            encryption_key_ref="vault://key/1",
            dedup_params={"chunk_avg": 1048576, "hash": "buzhash"},
        )
        s.add(repo)
        await s.flush()
        assert repo.state == "active"
        assert repo.dedup_params == {"chunk_avg": 1048576, "hash": "buzhash"}

        policy = BackupPolicy(
            org_id=org_id,
            repository_id=repo.id,
            retention_gfs={"daily": 7, "weekly": 4, "monthly": 12, "yearly": 1},
        )
        s.add(policy)
        await s.flush()
        assert policy.enabled is True
        assert policy.retention_gfs == {"daily": 7, "weekly": 4, "monthly": 12, "yearly": 1}

        run = BackupRun(
            org_id=org_id,
            device_id=device_id,
            repository_id=repo.id,
            requested_by=user_id,
            actor_type="user",
        )
        s.add(run)
        await s.flush()
        assert run.state == "pending"
        assert run.job_id is None and run.finished_at is None

        point = BackupPoint(
            org_id=org_id,
            device_id=device_id,
            repository_id=repo.id,
            backup_run_id=run.id,
            manifest_ref="s3://bucket/org/manifests/1",
            size=4096,
            state="complete",
        )
        s.add(point)
        await s.flush()
        assert point.state == "complete"
        assert point.verified_at is None and point.restore_tested_at is None

        restore = RestoreJob(
            org_id=org_id,
            device_id=device_id,
            backup_point_id=point.id,
            mode="verify",
            requested_by=user_id,
            actor_type="user",
        )
        s.add(restore)
        await s.flush()
        assert restore.state == "pending"
        assert restore.mode == "verify"

    async with org_session(org_id, engine=app_engine) as s:
        got_repo = (
            await s.execute(select(BackupRepository).where(BackupRepository.id == repo.id))
        ).scalar_one()
        assert got_repo.state == "active"

        got_run = (await s.execute(select(BackupRun).where(BackupRun.id == run.id))).scalar_one()
        assert got_run.state == "pending"

        got_point = (
            await s.execute(select(BackupPoint).where(BackupPoint.id == point.id))
        ).scalar_one()
        assert got_point.state == "complete"

        got_restore = (
            await s.execute(select(RestoreJob).where(RestoreJob.id == restore.id))
        ).scalar_one()
        assert got_restore.state == "pending"
        assert got_restore.mode == "verify"
