"""Migration 0016 (change management): RLS enforcement and RBAC seeding.

Mirrors the RLS-shape assertions used for prior modules: every tenant-
scoped table added by this module must have RLS ENABLE+FORCE and a
`tenant_isolation` policy. Also verifies the 4 permissions are seeded
and role grants are correct.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

CHANGE_MGMT_TABLES = {
    "change_request",
    "change_approval",
    "change_link",
    "change_external_ref",
}


async def test_change_mgmt_migration_0016_applied(admin_engine: AsyncEngine) -> None:
    """Verify the change-management migration (0016) has been applied.

    Revision ids are zero-padded, monotonically increasing strings, so a
    lexical `>=` check confirms 0016 (and everything before it) is applied
    without pinning to the tip -- later migrations (0017+) must not break
    this assertion.
    """
    async with admin_engine.connect() as conn:
        head_revision = (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
    assert head_revision is not None and head_revision >= "0016", (
        f"Expected head >= 0016 (change mgmt applied), got {head_revision}"
    )


async def test_change_mgmt_tables_exist(admin_engine: AsyncEngine) -> None:
    """Verify all 4 change management tables exist."""
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        )
        tables = {r.table_name for r in rows}
    missing = CHANGE_MGMT_TABLES - tables
    assert not missing, f"missing tables: {missing}"


async def test_change_mgmt_tables_have_forced_rls(admin_engine: AsyncEngine) -> None:
    """Verify all 4 change mgmt tables have RLS enabled and forced."""
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
    for table in sorted(CHANGE_MGMT_TABLES):
        if flags.get(table) != (True, True):
            problems.append(f"{table}: RLS not enabled+forced {flags.get(table)}")
    assert not problems, problems


async def test_change_mgmt_tables_have_tenant_isolation_policy(admin_engine: AsyncEngine) -> None:
    """Verify all 4 change mgmt tables have tenant_isolation policy."""
    async with admin_engine.connect() as conn:
        policies = {
            (r.tablename, r.policyname)
            for r in await conn.execute(
                text("SELECT tablename, policyname FROM pg_policies WHERE schemaname='public'")
            )
        }
    problems = []
    for table in sorted(CHANGE_MGMT_TABLES):
        if (table, "tenant_isolation") not in policies:
            problems.append(f"{table}: no tenant_isolation policy")
    assert not problems, problems


async def test_change_permissions_seeded(admin_engine: AsyncEngine) -> None:
    """Verify the 4 change permissions are seeded."""
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT code FROM permissions WHERE module = 'change'")
        )
        codes = {r.code for r in rows}
    assert codes == {
        "change.read",
        "change.request",
        "change.approve",
        "change.manage",
        # Migration 0122: change gates.
        "change.gate.manage",
    }


async def test_change_role_grants(admin_engine: AsyncEngine) -> None:
    """Verify role grants match the spec: super/org-admin get all 4,
    technician gets read+request only (NOT approve/manage), auditor/read-only get read."""
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.name AS role_name, p.code AS code FROM role_permissions rp"
                " JOIN roles r ON r.id = rp.role_id"
                " JOIN permissions p ON p.id = rp.permission_id"
                " WHERE p.module = 'change' AND r.org_id IS NULL"
            )
        )
        grants: dict[str, set[str]] = {}
        for r in rows:
            grants.setdefault(r.role_name, set()).add(r.code)

    all_five = {
        "change.read",
        "change.request",
        "change.approve",
        "change.manage",
        "change.gate.manage",
    }
    assert grants["super-admin"] == all_five
    assert grants["org-admin"] == all_five
    assert grants["technician"] == {
        "change.read",
        "change.request",
    }, f"technician perms wrong: {grants.get('technician')}"
    assert grants["auditor"] == {"change.read"}
    assert grants["read-only"] == {"change.read"}


async def test_audit_events_index_exists(admin_engine: AsyncEngine) -> None:
    """Verify the ix_audit_target index exists on audit_events."""
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT indexname FROM pg_indexes"
                " WHERE tablename='audit_events' AND indexname='ix_audit_target'"
            )
        )
        indexes = {r.indexname for r in rows}
    assert "ix_audit_target" in indexes, "ix_audit_target index not found on audit_events"
