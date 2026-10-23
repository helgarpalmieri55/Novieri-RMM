"""reporting: seed `reporting.read`, and nothing else

docs/modules/reporting/spec.md §4.3, §13, §16.10.

**NO TABLES. NO DDL. NO RLS POLICY.** Reporting owns no facts (spec preamble,
§0.3): it stores no measurement, adds no snapshot table, and computes nothing on
a schedule. Every report is a bounded SQL query executed live inside
`org_session` at request time, against tables other modules already own. So the
entire migration is one permission row and its two role grants, and there is
nothing for `test_schema_drift.py` to compare -- which is the point, not an
oversight.

**ONE CODE, NOT THREE.** §4.3 rejects `reporting.admin` (there is nothing to
administer: the catalogue is code, there are no schedules in v1, there is no
per-org configuration) and `reporting.export` (an export returns exactly the
rows the same caller already sees on screen, and every existing exporter in this
repo is gated on its module's own read code). A grant with nothing behind it is
a grant an org-admin has to reason about for no benefit.

**WHY THE TWO ADMIN ROLES ONLY**, following `0045_ai_admin.py`'s `ai.use`
precedent for the same stated reason. `reporting.read` grants nothing on its
own -- §0.4's conjunction means every report additionally requires the read code
of every module it draws from -- so seeding it broadly would mean every existing
technician silently gains a reporting surface on upgrade without anyone deciding
to, and gains no data by it. An org that wants technicians to hold it grants it
deliberately.

`org-admin` is not a choice: `tests/test_rbac.py::test_org_admin_has_all_
permissions` asserts an org-admin's granted codes `== set(PERMISSIONS)`, so a
code in the dict and not seeded here turns that red. `technician` is excluded
and that is also not a choice: `tests/test_api_routes.py::
test_me_returns_identity_and_permissions` pins the technician role's exact code
set, and widening it there would be a change to another module's test on this
module's behalf.

NOTE: the live `permissions` table is (id, code, module, action) -- there is NO
`resource` and NO `description` column. Descriptions live only in
`app/core/rbac.py::PERMISSIONS`, matching every other module's migration.

Revision ID: 0047
Revises: 0046
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("reporting.read", "reporting", "read"),
]

_ALL = [c for c, _, _ in PERMISSIONS]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": _ALL,
    "org-admin": _ALL,
}


def upgrade() -> None:
    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for role, codes in ROLE_GRANTS.items():
        code_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{role}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE module = 'reporting')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'reporting'")
