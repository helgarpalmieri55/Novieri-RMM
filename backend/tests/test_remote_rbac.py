from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Permission, Role, RolePermission


async def _perms_for(session, role_name: str) -> set[str]:
    rows = (
        await session.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .where(Role.name == role_name, Role.org_id.is_(None))
        )
    ).scalars()
    return set(rows)


async def test_remote_permissions_seeded_and_granted(admin_engine: AsyncEngine) -> None:
    async with AsyncSession(admin_engine) as session:
        codes = set(
            (await session.execute(select(Permission.code))).scalars()
        )
        assert {"remote.control", "remote.recordings.read", "remote.policy.manage"} <= codes

        technician = await _perms_for(session, "technician")
        assert "remote.control" in technician
        assert "remote.recordings.read" not in technician  # techs operate, not audit

        auditor = await _perms_for(session, "auditor")
        assert "remote.recordings.read" in auditor  # read privileged-access evidence
        assert "remote.control" not in auditor

        remote_codes = {"remote.control", "remote.recordings.read", "remote.policy.manage"}
        for role in ("super-admin", "org-admin"):
            assert remote_codes <= await _perms_for(session, role)
