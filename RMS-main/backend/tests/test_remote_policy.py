from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import Device, DeviceGroup, Organization, RemotePolicy
from app.modules.remote.policy import resolve_policy


async def _setup(admin_engine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="rp", slug=f"rp-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        grp = DeviceGroup(org_id=org.id, name="servers")
        s.add(grp)
        await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="linux", device_group_id=grp.id)
        s.add(dev)
        await s.commit()
        return org.id, grp.id, dev.id


async def test_defaults_when_no_policy(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, _, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        pol = await resolve_policy(s, org_id, device_id)
    assert pol.allow_unattended is False and pol.record_sessions is True
    assert pol.recording_retention_days == 90


async def test_group_policy_wins_over_org_default(admin_engine, app_engine) -> None:
    org_id, group_id, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        s.add(RemotePolicy(org_id=org_id, device_group_id=None, allow_unattended=False))
        s.add(RemotePolicy(org_id=org_id, device_group_id=group_id, allow_unattended=True,
                           recording_retention_days=30))
        await s.flush()
        pol = await resolve_policy(s, org_id, device_id)
    assert pol.allow_unattended is True and pol.recording_retention_days == 30


async def test_org_default_resolves_when_duplicate_rows_exist(admin_engine, app_engine) -> None:
    """Postgres UNIQUE(org_id, device_group_id) does not prevent multiple NULL
    device_group_id rows for the same org, so more than one org-default policy
    row can exist. resolve_policy must not raise MultipleResultsFound and must
    deterministically pick the lowest-id row."""
    org_id, _, device_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        first = RemotePolicy(org_id=org_id, device_group_id=None, allow_unattended=True,
                              recording_retention_days=45)
        s.add(first)
        await s.flush()
        second = RemotePolicy(org_id=org_id, device_group_id=None, allow_unattended=False,
                               recording_retention_days=60)
        s.add(second)
        await s.flush()
        pol = await resolve_policy(s, org_id, device_id)
    assert pol.allow_unattended is True
    assert pol.recording_retention_days == 45
