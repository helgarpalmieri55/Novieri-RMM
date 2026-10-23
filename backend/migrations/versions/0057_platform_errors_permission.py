"""platform: seed `platform.errors.read` and `platform.errors.write`

docs/superpowers/specs/2026-08-15-error-monitoring-design.md §5.

NO TABLES HERE, same shape as 0054 (`platform.status.read`): the group/event
tables are 0056, kept separate so this RBAC change can be reviewed and
reverted without touching schema.

TWO CODES, NOT ONE (spec §5): reading which code paths are failing and
*changing triage state* (resolving a group) are different privileges --
this project's convention is a read code per module with writes gated
separately, matching `patch.read`/`patch.apply`,
`health.read`/`health.alert.manage`, and every other read/write split in
`app/core/rbac.py`.

WHO GETS THEM. `org-admin` is not a choice -- `tests/test_rbac.py::
test_org_admin_has_all_permissions` asserts an org-admin's codes equal
set(PERMISSIONS), so a code in the dict and not seeded here turns that red.
Platform errors are infrastructure-wide and not tenant data (spec §4.1), so
no other role is seeded: an org that wants a technician to hold either code
grants it deliberately, same reasoning as 0054.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODES = ("platform.errors.read", "platform.errors.write")


def upgrade() -> None:
    for code in _CODES:
        action = code.removeprefix("platform.")
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', 'platform', '{action}')"
        )
    code_list = ", ".join(f"'{c}'" for c in _CODES)
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        f"WHERE r.name = 'org-admin' AND p.code IN ({code_list})"
    )


def downgrade() -> None:
    code_list = ", ".join(f"'{c}'" for c in _CODES)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({code_list}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({code_list})")
