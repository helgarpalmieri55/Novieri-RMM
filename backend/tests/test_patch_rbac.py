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


async def test_patch_permissions_seeded_and_granted(admin_engine: AsyncEngine) -> None:
    async with AsyncSession(admin_engine) as session:
        codes = set((await session.execute(select(Permission.code))).scalars())
        assert {"patch.read", "patch.scan", "patch.approve", "patch.apply",
                "patch.policy.manage"} <= codes

        tech = await _perms_for(session, "technician")
        assert {"patch.read", "patch.scan", "patch.apply"} <= tech
        assert "patch.approve" not in tech            # approve is privileged
        assert "patch.policy.manage" not in tech

        auditor = await _perms_for(session, "auditor")
        assert "patch.read" in auditor
        assert "patch.apply" not in auditor and "patch.approve" not in auditor

        readonly = await _perms_for(session, "read-only")
        assert readonly & {"patch.read"} == {"patch.read"}
        assert not (readonly & {"patch.scan", "patch.apply", "patch.approve"})

        for role in ("super-admin", "org-admin"):
            assert {"patch.read", "patch.scan", "patch.approve", "patch.apply",
                    "patch.policy.manage"} <= await _perms_for(session, role)
