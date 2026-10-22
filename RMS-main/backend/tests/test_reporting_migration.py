"""The one kernel change this module makes (spec §4.3, §13, §16.10).

There is no schema here -- reporting owns no tables -- so these tests are
entirely about the permission code and the roles it reaches.

WHY EACH ASSERTION IS SHAPED THE WAY IT IS:

* The description is asserted on a SUBSTRING, not on the key's presence. A code
  seeded with a description nobody wrote is the actual failure mode -- §4.3
  requires the "grants NOTHING on its own" phrasing because an org-admin reading
  the role editor must be told that granting this alone grants nothing, and a
  key with an empty or generic description tells them the opposite.

* The seeding assertions query the LIVE `permissions`/`role_permissions` tables
  on the admin engine, not `app.core.rbac.PERMISSIONS`. The dict and the table
  are two different facts: a code added to the dict without a migration passes
  every dict-based assertion and 403s every caller at runtime, because
  `user_permission_codes` reads rows.

* `technician` is asserted NEGATIVELY, on purpose. It is the fact
  `tests/test_api_routes.py::test_me_returns_identity_and_permissions` pins as a
  literal set, so a later migration that widens the grant "for convenience"
  turns another module's test red for a reason nobody can trace back here. This
  is the test that points at the cause.
"""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.rbac import PERMISSIONS
from app.models import Base, Permission, Role, RolePermission

REPORTING_READ = "reporting.read"


async def _roles_granted(admin_engine: AsyncEngine, code: str) -> set[str]:
    """Every GLOBAL role name (org_id IS NULL) holding `code`, read from the
    live tables -- the seeding, not the Python dict."""
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(Role.name)
                .join(RolePermission, RolePermission.role_id == Role.id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(Permission.code == code, Role.org_id.is_(None))
            )
        ).scalars()
        return set(rows)


async def test_reporting_read_is_in_the_permission_registry() -> None:
    assert REPORTING_READ in PERMISSIONS
    description = PERMISSIONS[REPORTING_READ]
    # §4.3's required phrasing, asserted on the content rather than on the key:
    # a code whose description does not say this is a code an org-admin will
    # mis-grant.
    assert "Grants NOTHING on its own" in description, description
    assert "403" in description, description


async def test_reporting_read_is_seeded_into_the_permissions_table(
    admin_engine: AsyncEngine,
) -> None:
    """The migration ran. `PERMISSIONS` is a Python dict; RBAC reads rows."""
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(Permission.id, Permission.module, Permission.action).where(
                    Permission.code == REPORTING_READ
                )
            )
        ).one_or_none()
    assert row is not None, "reporting.read is not seeded; the migration did not run"
    permission_id, module, action = row
    assert isinstance(permission_id, UUID)
    assert (module, action) == ("reporting", "read")


async def test_reporting_read_is_granted_to_both_admin_roles(
    admin_engine: AsyncEngine,
) -> None:
    """`org-admin` is MANDATORY, not a choice:
    `tests/test_rbac.py::test_org_admin_has_all_permissions` asserts an
    org-admin's granted codes `== set(PERMISSIONS)`, so a code in the dict and
    not seeded to `org-admin` turns that red."""
    granted = await _roles_granted(admin_engine, REPORTING_READ)
    assert {"super-admin", "org-admin"} <= granted, granted


async def test_reporting_read_is_not_granted_to_technician(
    admin_engine: AsyncEngine,
) -> None:
    """`tests/test_api_routes.py::test_me_returns_identity_and_permissions` pins
    the technician role's exact code set. Widening it there would be a change to
    another module's test on this module's behalf (§4.3: an org that wants
    technicians to hold this grants it deliberately, and gains nothing by it
    until they also hold the source codes)."""
    granted = await _roles_granted(admin_engine, REPORTING_READ)
    assert "technician" not in granted, granted
    # The grant is exactly the two admin roles -- not "at least" them, so a
    # migration that also seeded `auditor` or `read-only` is red here.
    assert granted == {"super-admin", "org-admin"}, granted


async def test_there_is_no_reporting_admin_and_no_reporting_export_code() -> None:
    """§4.3 rejects both by name. There is nothing to administer (the catalogue
    is code, there are no schedules in v1, there is no per-org configuration),
    and an export returns exactly the rows the same caller already sees."""
    reporting_codes = {c for c in PERMISSIONS if c.startswith("reporting.")}
    assert reporting_codes == {REPORTING_READ}, reporting_codes


async def test_the_migration_adds_no_table() -> None:
    """§13: reporting owns no tables, so there is no DDL, no RLS policy to add,
    and nothing for `test_schema_drift.py` to compare. A `report*` table in the
    metadata means someone built the thing §0.3 refused."""
    assert not [t for t in Base.metadata.tables if t.startswith("report")]


@pytest.mark.parametrize("code", sorted(PERMISSIONS))
async def test_every_registered_permission_code_is_seeded(
    admin_engine: AsyncEngine, code: str
) -> None:
    """Liveness for the assertion above: if `PERMISSIONS` were somehow empty the
    reporting-specific tests would still pass on their own terms, and this one
    would collect zero cases. Assert the registry is non-empty first."""
    assert PERMISSIONS, "the permission registry is empty; every case here is vacuous"
    async with AsyncSession(admin_engine) as session:
        found = (
            await session.execute(select(Permission.code).where(Permission.code == code))
        ).scalar_one_or_none()
    assert found == code, f"{code} is in PERMISSIONS but not seeded into `permissions`"
