"""platform: seed `platform.status.read`

docs/superpowers/specs/2026-08-15-platform-health-design.md §5.

NO TABLES HERE. The samples table is 0055; this migration is one permission row
and its grants, kept separate so the RBAC change can be reviewed and reverted
without touching schema.

WHO GETS IT. `org-admin` is not a choice -- `tests/test_rbac.py::
test_org_admin_has_all_permissions` asserts an org-admin's codes equal
set(PERMISSIONS), so a code in the dict and not seeded here turns that red.
Platform health is infrastructure-wide and not tenant data, so no other role is
seeded: an org that wants a technician to hold it grants it deliberately.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "platform.status.read"


def upgrade() -> None:
    op.execute(
        "INSERT INTO permissions (id, code, module, action) "
        f"VALUES (gen_random_uuid(), '{_CODE}', 'platform', 'status.read')"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        f"WHERE r.name = 'org-admin' AND p.code = '{_CODE}'"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code = '{_CODE}')"
    )
    op.execute(f"DELETE FROM permissions WHERE code = '{_CODE}'")
