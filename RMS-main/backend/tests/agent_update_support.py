"""Seeding helpers for the agent self-update tests."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Agent, Device, DeviceGroup, Job, JobStatus
from app.models.core import AgentRelease

SHA = "ab" * 32


async def seed_group(admin_engine: AsyncEngine, org_id: UUID, name: str) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        g = DeviceGroup(org_id=org_id, name=f"{name}-{uuid4().hex[:6]}")
        s.add(g)
        await s.commit()
        return g.id


async def seed_device(
    admin_engine: AsyncEngine,
    org_id: UUID,
    *,
    group_id: UUID | None = None,
    version: str = "1.0.0",
    capable: bool = True,
    online: bool = True,
    os_family: str = "linux",
    arch: str = "amd64",
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        d = Device(
            org_id=org_id,
            hostname=f"host-{uuid4().hex[:8]}",
            os_family=os_family,
            architecture=arch,
            device_group_id=group_id,
        )
        s.add(d)
        await s.flush()
        s.add(
            Agent(
                org_id=org_id,
                device_id=d.id,
                cert_fingerprint=uuid4().hex + uuid4().hex,
                cert_serial=uuid4().hex,
                cert_not_after=datetime.now(UTC) + timedelta(days=300),
                enrolled_at=datetime.now(UTC),
                agent_version=version,
                capabilities={"agent.update": True, "hello": True} if capable else {"hello": True},
                status="online" if online else "offline",
            )
        )
        await s.commit()
        return d.id


async def seed_release(
    admin_engine: AsyncEngine,
    version: str,
    *,
    os_name: str = "linux",
    arch: str = "amd64",
    channel: str = "pilot",
    signature: str | None = None,
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        r = AgentRelease(
            version=version,
            os=os_name,
            arch=arch,
            storage_ref=f"agent-releases/{version}/{os_name}-{arch}",
            size_bytes=1234,
            sha256=SHA,
            channel=channel,
            release_signature=signature,
            published_at=datetime.now(UTC),
        )
        s.add(r)
        await s.commit()
        return r.id


def unique_version(major: int = 2) -> str:
    """Releases are global, so every test uses its own version."""
    return f"{major}.{uuid4().int % 100000}.0"


async def set_job(
    admin_engine: AsyncEngine,
    job_id: UUID,
    status: JobStatus,
    result: dict[str, object] | None = None,
    *,
    attempts: int | None = None,
    expires_at: datetime | None = None,
) -> None:
    values: dict[str, object] = {"status": status, "result": result}
    if attempts is not None:
        values["attempts"] = attempts
    if expires_at is not None:
        values["expires_at"] = expires_at
    async with AsyncSession(admin_engine) as s:
        await s.execute(update(Job).where(Job.id == job_id).values(**values))
        await s.commit()


async def set_online(admin_engine: AsyncEngine, device_id: UUID, online: bool) -> None:
    async with AsyncSession(admin_engine) as s:
        await s.execute(
            update(Agent)
            .where(Agent.device_id == device_id)
            .values(status="online" if online else "offline")
        )
        await s.commit()


async def job_for(admin_engine: AsyncEngine, job_id: UUID) -> Job:
    async with AsyncSession(admin_engine) as s:
        return (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
