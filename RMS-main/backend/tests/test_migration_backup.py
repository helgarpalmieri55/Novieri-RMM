"""Migration 0014 (backup): RLS enforcement on the five new tables.

Mirrors the RLS-shape assertions used for prior modules (see test_rls.py's
generic `test_every_org_scoped_table_has_forced_rls_policy`): every tenant-
scoped table added by this module must have RLS ENABLE+FORCE and a
`tenant_isolation` policy.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

BACKUP_TABLES = {
    "backup_repositories",
    "backup_policies",
    "backup_runs",
    "backup_points",
    "restore_jobs",
}


async def test_backup_tables_have_forced_rls(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        flags = {
            r.relname: (r.relrowsecurity, r.relforcerowsecurity)
            for r in await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
                    " JOIN pg_namespace n ON n.oid=c.relnamespace"
                    " WHERE n.nspname='public' AND c.relkind='r'"
                )
            )
        }
    problems = []
    for table in sorted(BACKUP_TABLES):
        if flags.get(table) != (True, True):
            problems.append(f"{table}: RLS not enabled+forced {flags.get(table)}")
    assert not problems, problems


async def test_backup_tables_have_tenant_isolation_policy(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        policies = {
            (r.tablename, r.policyname)
            for r in await conn.execute(
                text("SELECT tablename, policyname FROM pg_policies WHERE schemaname='public'")
            )
        }
    problems = []
    for table in sorted(BACKUP_TABLES):
        if (table, "tenant_isolation") not in policies:
            problems.append(f"{table}: no tenant_isolation policy")
    assert not problems, problems


async def test_backup_tables_exist(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        )
        tables = {r.table_name for r in rows}
    missing = BACKUP_TABLES - tables
    assert not missing, f"missing tables: {missing}"


async def test_backup_permissions_seeded(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT code FROM permissions WHERE module = 'backup'")
        )
        codes = {r.code for r in rows}
    assert codes == {
        "backup.read",
        "backup.policy.manage",
        "backup.run",
        "backup.restore",
        # 0090: the off-site copy and its recovery key.
        "backup.offsite.manage",
        "backup.recovery.manage",
    }


async def test_backup_role_grants(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.name AS role_name, p.code AS code FROM role_permissions rp"
                " JOIN roles r ON r.id = rp.role_id"
                " JOIN permissions p ON p.id = rp.permission_id"
                " WHERE p.module = 'backup' AND r.org_id IS NULL"
            )
        )
        grants: dict[str, set[str]] = {}
        for r in rows:
            grants.setdefault(r.role_name, set()).add(r.code)

    every = {
        "backup.read",
        "backup.policy.manage",
        "backup.run",
        "backup.restore",
        "backup.offsite.manage",
        "backup.recovery.manage",
    }
    assert grants["super-admin"] == every
    assert grants["org-admin"] == every
    assert grants["technician"] == {
        "backup.read",
        "backup.run",
        "backup.restore",
    }
    assert grants["auditor"] == {"backup.read"}
    assert grants["read-only"] == {"backup.read"}
