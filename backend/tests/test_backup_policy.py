from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import BackupPolicy, BackupRepository, Device, DeviceGroup, Organization
from app.modules.backup.policy import resolve_backup_policy


async def _setup(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="bp", slug=f"bp-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        grp = DeviceGroup(org_id=org.id, name="servers")
        s.add(grp)
        await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="linux", device_group_id=grp.id)
        s.add(dev)
        repo = BackupRepository(
            org_id=org.id, name="primary", storage_ref="x", encryption_key_ref="x"
        )
        s.add(repo)
        await s.commit()
        return org.id, grp.id, dev.id, repo.id


async def test_no_policy_returns_none(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, _, device_id, _ = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        pol = await resolve_backup_policy(s, org_id, device_id)
    assert pol is None


async def test_group_policy_wins_over_org_default(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, group_id, device_id, repo_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(
            BackupPolicy(
                org_id=org_id,
                repository_id=repo_id,
                device_group_id=None,
                window_cron="0 3 * * *",
                retention_gfs={"daily": 7},
            )
        )
        s.add(
            BackupPolicy(
                org_id=org_id,
                repository_id=repo_id,
                device_group_id=group_id,
                window_cron="0 1 * * *",
                retention_gfs={"daily": 30},
            )
        )
        await s.flush()
        pol = await resolve_backup_policy(s, org_id, device_id)
    assert pol is not None
    assert pol.window_cron == "0 1 * * *"
    assert pol.retention_gfs == {"daily": 30}


async def test_org_default_resolves_when_duplicate_null_rows_exist(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _, device_id, repo_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        first = BackupPolicy(
            org_id=org_id, repository_id=repo_id, device_group_id=None,
            window_cron="first", retention_gfs={"daily": 1},
        )
        s.add(first)
        await s.flush()
        second = BackupPolicy(
            org_id=org_id, repository_id=repo_id, device_group_id=None,
            window_cron="second", retention_gfs={"daily": 2},
        )
        s.add(second)
        await s.flush()
        # must not raise MultipleResultsFound, and must be deterministic
        pol = await resolve_backup_policy(s, org_id, device_id)
    assert pol is not None
    assert pol.window_cron == "first"


async def test_disabled_policy_is_not_returned(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, group_id, device_id, repo_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(
            BackupPolicy(
                org_id=org_id,
                repository_id=repo_id,
                device_group_id=group_id,
                enabled=False,
                window_cron="0 1 * * *",
            )
        )
        await s.flush()
        pol = await resolve_backup_policy(s, org_id, device_id)
    assert pol is None
