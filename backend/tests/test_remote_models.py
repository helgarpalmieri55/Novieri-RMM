from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, Organization, RemotePolicy, RemoteSession, User


async def _org_device_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="rc", slug=f"rc-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        dev = Device(
            org_id=org.id, hostname="h", os_family="linux", client_id=org.id, site_id=org.id
        )
        s.add(dev)
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, dev.id, usr.id


async def test_remote_session_roundtrip(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = RemoteSession(
            org_id=org_id, device_id=device_id, technician_id=user_id,
            protocol="rdp", consent_mode="attended", state="requested",
        )
        s.add(row)
        await s.flush()
        sid = row.id
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        got = (await s.execute(select(RemoteSession).where(RemoteSession.id == sid))).scalar_one()
        assert got.protocol == "rdp" and got.state == "requested" and got.recording_ref is None


async def test_remote_policy_defaults(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, _, _ = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        p = RemotePolicy(org_id=org_id, device_group_id=None)
        s.add(p)
        await s.flush()
        assert p.allow_unattended is False and p.record_sessions is True
        assert p.recording_retention_days == 90
