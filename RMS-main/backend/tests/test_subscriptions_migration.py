"""Migration 0019 (Subscriptions): RLS enforcement and RBAC seeding.

Mirrors the RLS-shape assertions used for prior modules: every tenant-
scoped table added by this module must have RLS ENABLE+FORCE and a
`tenant_isolation` policy. Also verifies the 2 permissions are seeded
and role grants are correct.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

SUBSCRIPTIONS_TABLES = {
    "subscription",
    "seat_assignment",
    "subscription_event",
}


async def test_subscriptions_migration_0019_applied(admin_engine: AsyncEngine) -> None:
    """Verify the subscriptions migration (0019) has been applied.

    Revision ids are zero-padded, monotonically increasing strings, so a
    lexical `>=` check confirms 0019 (and everything before it) is applied
    without pinning to the tip -- later migrations (0020+) must not break
    this assertion.
    """
    async with admin_engine.connect() as conn:
        head_revision = (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
    assert head_revision is not None and head_revision >= "0019", (
        f"Expected head >= 0019 (subscriptions applied), got {head_revision}"
    )


async def test_subscriptions_tables_exist(admin_engine: AsyncEngine) -> None:
    """Verify all 3 subscriptions tables exist."""
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        )
        tables = {r.table_name for r in rows}
    missing = SUBSCRIPTIONS_TABLES - tables
    assert not missing, f"missing tables: {missing}"


async def test_subscriptions_tables_have_forced_rls(admin_engine: AsyncEngine) -> None:
    """Verify all 3 subscriptions tables have RLS enabled and forced."""
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
    for table in sorted(SUBSCRIPTIONS_TABLES):
        if flags.get(table) != (True, True):
            problems.append(f"{table}: RLS not enabled+forced {flags.get(table)}")
    assert not problems, problems


async def test_subscriptions_tables_have_tenant_isolation_policy(
    admin_engine: AsyncEngine,
) -> None:
    """Verify all 3 subscriptions tables have tenant_isolation policy."""
    async with admin_engine.connect() as conn:
        policies = {
            (r.tablename, r.policyname)
            for r in await conn.execute(
                text("SELECT tablename, policyname FROM pg_policies WHERE schemaname='public'")
            )
        }
    problems = []
    for table in sorted(SUBSCRIPTIONS_TABLES):
        if (table, "tenant_isolation") not in policies:
            problems.append(f"{table}: no tenant_isolation policy")
    assert not problems, problems


async def test_subscriptions_permissions_seeded(admin_engine: AsyncEngine) -> None:
    """Verify the 2 subscriptions permissions are seeded."""
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT code FROM permissions WHERE module = 'subscriptions'")
        )
        codes = {r.code for r in rows}
    assert codes == {
        "subscriptions.read",
        "subscriptions.manage",
    }


async def test_subscriptions_role_grants(admin_engine: AsyncEngine) -> None:
    """Verify role grants match the spec: super/org-admin get both,
    technician gets read+manage, auditor/read-only get read only."""
    async with admin_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.name AS role_name, p.code AS code FROM role_permissions rp"
                " JOIN roles r ON r.id = rp.role_id"
                " JOIN permissions p ON p.id = rp.permission_id"
                " WHERE p.module = 'subscriptions' AND r.org_id IS NULL"
            )
        )
        grants: dict[str, set[str]] = {}
        for r in rows:
            grants.setdefault(r.role_name, set()).add(r.code)

    all_two = {
        "subscriptions.read",
        "subscriptions.manage",
    }
    assert grants["super-admin"] == all_two
    assert grants["org-admin"] == all_two
    assert grants["technician"] == all_two, (
        f"technician perms wrong: {grants.get('technician')}"
    )
    assert grants["auditor"] == {"subscriptions.read"}
    assert grants["read-only"] == {"subscriptions.read"}
