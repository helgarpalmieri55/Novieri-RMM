"""Seed helpers shared by every `test_health_*.py` module.

WHY THESE LIVE IN ONE PLACE. Tasks 5-13 each need an org with a device, a
profile and a check before they can assert anything, and a per-file copy of
that setup is how two tests come to disagree about what a "default profile"
is. These are deliberately THIN -- they insert rows on the admin (RLS-
bypassing) engine and return ids, with no service-layer call anywhere, so a
test that exercises a service is never seeded by the same code path it is
testing.

Every helper accepts either an `AsyncEngine` or an object carrying one as
`.admin_engine` (the `api_env` fixture), because some tests hold one and some
the other.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


def admin_engine_of(env_or_engine: Any) -> AsyncEngine:
    """`api_env` or a bare engine -> the admin engine."""
    engine = getattr(env_or_engine, "admin_engine", env_or_engine)
    assert isinstance(engine, AsyncEngine)
    return engine


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class HealthOrg:
    """One org with the kernel rows every health test needs."""

    org_id: UUID
    user_id: UUID
    device_id: UUID
    group_id: UUID


async def seed_org(env_or_engine: Any, tag: str = "h") -> HealthOrg:
    from app.models import Device, DeviceGroup, Organization, User

    engine = admin_engine_of(env_or_engine)
    slug = f"{tag}-{uuid4().hex[:8]}"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@test.local")
        session.add(user)
        group = DeviceGroup(org_id=org.id, name=f"group-{slug}")
        session.add(group)
        await session.flush()
        device = Device(
            org_id=org.id,
            hostname=f"host-{slug}",
            os_family="windows",
            device_group_id=group.id,
        )
        session.add(device)
        await session.commit()
        return HealthOrg(
            org_id=org.id, user_id=user.id, device_id=device.id, group_id=group.id
        )


async def seed_agent(
    env_or_engine: Any, org_id: UUID, device_id: UUID, *, status: str = "online"
) -> None:
    """An enrolled agent for `device_id`, connected unless told otherwise. The
    policy reconcile pushes only to a device whose agent is connected."""
    from datetime import UTC, datetime, timedelta

    from app.models import Agent

    now = datetime.now(UTC)
    async with AsyncSession(admin_engine_of(env_or_engine)) as session:
        session.add(
            Agent(
                org_id=org_id,
                device_id=device_id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=now + timedelta(days=365),
                enrolled_at=now - timedelta(days=1),
                status=status,
                last_seen_at=now,
            )
        )
        await session.commit()


async def seed_device(env_or_engine: Any, org_id: UUID, **kw: Any) -> UUID:
    from app.models import Device

    engine = admin_engine_of(env_or_engine)
    values: dict[str, Any] = {
        "hostname": f"host-{uuid4().hex[:8]}",
        "os_family": "windows",
    }
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, **values)
        session.add(device)
        await session.commit()
        return device.id


async def seed_group(env_or_engine: Any, org_id: UUID, **kw: Any) -> UUID:
    from app.models import DeviceGroup

    engine = admin_engine_of(env_or_engine)
    values: dict[str, Any] = {"name": f"group-{uuid4().hex[:8]}"}
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        group = DeviceGroup(org_id=org_id, **values)
        session.add(group)
        await session.commit()
        return group.id


async def seed_profile(env_or_engine: Any, org_id: UUID, **kw: Any) -> UUID:
    from app.models import HealthProfile

    engine = admin_engine_of(env_or_engine)
    values: dict[str, Any] = {"name": f"profile-{uuid4().hex[:8]}", "description": ""}
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        profile = HealthProfile(org_id=org_id, **values)
        session.add(profile)
        await session.commit()
        return profile.id


async def seed_check(env_or_engine: Any, org_id: UUID, profile_id: UUID, **kw: Any) -> UUID:
    """One `health_check`. Defaults to a numeric CPU check so a caller that
    only cares about "a check exists" writes nothing."""
    from app.models import HealthCheck

    engine = admin_engine_of(env_or_engine)
    values: dict[str, Any] = {
        "check_type": "cpu.busy_pct",
        "selector": "",
        "comparator": "gt",
        "threshold_num": 95,
        "threshold_bool": None,
        "for_ticks": 3,
        "severity": "warning",
        "os_family": None,
    }
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        check = HealthCheck(org_id=org_id, profile_id=profile_id, **values)
        session.add(check)
        await session.commit()
        return check.id


async def seed_assignment(
    env_or_engine: Any,
    org_id: UUID,
    profile_id: UUID,
    *,
    device_id: UUID | None = None,
    device_group_id: UUID | None = None,
) -> UUID:
    from app.models import HealthProfileAssignment

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = HealthProfileAssignment(
            org_id=org_id,
            profile_id=profile_id,
            device_id=device_id,
            device_group_id=device_group_id,
        )
        session.add(row)
        await session.commit()
        return row.id


async def seed_alert(
    env_or_engine: Any,
    org: HealthOrg,
    *,
    profile_id: UUID,
    check_id: UUID,
    device_id: UUID | None = None,
    **kw: Any,
) -> UUID:
    from app.models import HealthAlert

    engine = admin_engine_of(env_or_engine)
    now = utcnow()
    values: dict[str, Any] = {
        "selector": "",
        "check_type": "cpu.busy_pct",
        "severity": "warning",
        "state": "active",
        "observed_value": "97.4",
        "threshold_display": "> 95",
        "opened_at": now,
        "opened_policy_version": 1,
        "last_seen_at": now,
    }
    values.update(kw)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        alert = HealthAlert(
            org_id=org.org_id,
            device_id=device_id or org.device_id,
            check_id=check_id,
            profile_id=profile_id,
            **values,
        )
        session.add(alert)
        await session.commit()
        return alert.id


async def seed_settings(env_or_engine: Any, org_id: UUID, **kw: Any) -> None:
    from app.models import HealthOrgSettings

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(HealthOrgSettings(org_id=org_id, **kw))
        await session.commit()
