"""asset inventory: seed `assets.manage`

docs/modules/asset-inventory/spec.md §7. Same shape as 0060: no schema at all,
so this RBAC change can be reviewed and reverted without touching a table.

ONE CODE, NOT THREE. `assets.read` is deliberately absent -- `devices.read`
already gates the fleet list and an asset IS a fleet row, so a second read gate
would blind every existing read-only role to part of that list on the day this
ships, with nothing on screen to explain it. `assets.discover` belongs to the
discovery cycle, where the job it guards exists; a permission that precedes the
capability it protects is one nobody can test.

WHO GETS IT. `org-admin`, because `tests/test_rbac.py::
test_org_admin_has_all_permissions` asserts an org-admin's codes equal
set(PERMISSIONS) -- a code in the dict and unseeded here turns that red. Also
`technician`, and that IS a free choice worth stating: recording the switch you
are standing in front of is the ordinary work of the role, and unlike
`agent.release.manage` this code publishes no executable and mints no
credential. The worst a holder can do is write a wrong row, which is visible
and editable.

Revision ID: 0062
Revises: 0061
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "assets.manage"


def upgrade() -> None:
    op.execute(
        "INSERT INTO permissions (id, code, module, action) "
        f"VALUES (gen_random_uuid(), '{_CODE}', 'assets', 'manage')"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        f"WHERE r.name IN ('org-admin', 'technician') AND p.code = '{_CODE}'"
    )


def downgrade() -> None:
    # role_permissions first: it references permissions, and dropping those
    # rows out from under it is what leaves a downgrade half-applied.
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code = '{_CODE}')"
    )
    op.execute(f"DELETE FROM permissions WHERE code = '{_CODE}'")
