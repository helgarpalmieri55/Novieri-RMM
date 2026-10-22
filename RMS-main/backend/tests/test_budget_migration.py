"""Migration 0020: seven budget tables + RLS + the four budget permissions.

Mirrors tests/test_subscriptions_migration.py. These are CATALOG assertions:
pg_class.relrowsecurity/relforcerowsecurity and pg_policies are readable
regardless of BYPASSRLS, so admin_engine is the right connection here and the
checks are still real -- a migration missing ENABLE/FORCE or the policy fails
them. They prove the policy EXISTS, not that it isolates data; the
load-bearing data-level tenancy tests spec §8/§12 calls for live at the
application layer (fx.py onward) and run on a raw BYPASSRLS admin session so
the app's own org_id predicate is the sole guard.

Note: the permissions table's identifying column is `code` (see
migrations/versions/0001_core_schema.py and every prior module's migration
test, e.g. tests/test_subscriptions_migration.py), not `name` -- the queries
below use `code` to match the actual schema and existing convention.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

BUDGET_TABLES = [
    "budget_settings",
    "cost_center",
    "expense_category",
    "budget",
    "budget_line",
    "fx_rate",
    "expense",
]


@pytest.mark.parametrize("table", BUDGET_TABLES)
async def test_table_has_rls_enabled_and_forced(admin_engine, table: str) -> None:
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert row.relrowsecurity is True, f"{table} missing ENABLE ROW LEVEL SECURITY"
    assert row.relforcerowsecurity is True, f"{table} missing FORCE ROW LEVEL SECURITY"


@pytest.mark.parametrize("table", BUDGET_TABLES)
async def test_table_has_tenant_isolation_policy(admin_engine, table: str) -> None:
    async with AsyncSession(admin_engine) as session:
        names = (
            await session.execute(
                text("SELECT policyname FROM pg_policies WHERE tablename = :t"),
                {"t": table},
            )
        ).scalars().all()
    assert "tenant_isolation" in names


async def test_budget_permissions_are_seeded(admin_engine) -> None:
    async with AsyncSession(admin_engine) as session:
        codes = (
            await session.execute(
                text(
                    "SELECT code FROM permissions WHERE code LIKE 'budget.%' "
                    "OR code LIKE 'expense.%'"
                )
            )
        ).scalars().all()
    assert set(codes) == {
        "budget.read",
        "budget.manage",
        "expense.record",
        "expense.confirm",
    }


async def test_technician_is_granted_no_budget_permission(admin_engine) -> None:
    """Spec §0.5 / §8: the first module where technician gets nothing --
    budget and expense data is sensitive even inside IT."""
    async with AsyncSession(admin_engine) as session:
        codes = (
            await session.execute(
                text(
                    "SELECT p.code FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE r.name = 'technician' AND r.org_id IS NULL "
                    "AND (p.code LIKE 'budget.%' OR p.code LIKE 'expense.%')"
                )
            )
        ).scalars().all()
    assert codes == []


async def test_auditor_gets_read_only(admin_engine) -> None:
    async with AsyncSession(admin_engine) as session:
        codes = (
            await session.execute(
                text(
                    "SELECT p.code FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE r.name = 'auditor' AND r.org_id IS NULL "
                    "AND (p.code LIKE 'budget.%' OR p.code LIKE 'expense.%')"
                )
            )
        ).scalars().all()
    assert codes == ["budget.read"]
