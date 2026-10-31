"""`remote.supervise`: stopping or joining another technician's remote session
(phase 3 design §5.3, `docs/superpowers/specs/2026-10-31-msp-clientes-fase3-consola-design.md`).

Until now the owner-or-admin rail of the stop route and the screen tunnel
(`app/modules/remote/tunnel.py::owner_or_admin`) asked for `orgs.manage` on the
session's client -- a code whose description is "organization settings, users,
and roles" and says nothing about watching or cutting somebody's remote
session. The rail now asks for a code of its own.

WHO GETS IT: EVERY ROLE THAT HOLDS `orgs.manage` WHEN THIS RUNS -- the two
global ones (`super-admin`, `org-admin`) AND every role an organisation made
for itself, the copy-on-write clone of `org-admin` that a role edit makes
(`app/core/rbac_admin.py`) included. Granting by NAME, the 0210/0232 way,
would reach only the global rows: an organisation whose administrators hold a
local clone of `org-admin` would find, after the upgrade, that nobody can stop
a runaway session any more. Granting by HOLDING is what "nobody loses the
power without being told" means. The grant is the role's, so a person holding
the role on one client holds the new code on that client -- exactly where
`orgs.manage` reached.

`technician`, `auditor`, `read-only` and `requester` hold no `orgs.manage` and
get nothing: supervising another person's session is admin tier. A role made
AFTER this upgrade gets the code only if someone ticks it.

`downgrade()` takes back every grant of the code -- the ones this upgrade made
and any a person added since in the role editor -- and then the code.
`orgs.manage` is never touched, so a downgrade hands the rail back to it with
the roles it had.

The two codes are written here, not imported: a migration that imports
application code runs against whatever that code has become.

Forward-only. `downgrade()` has never run in production (rollback is
restore-from-backup). CI runs upgrade->downgrade->upgrade.

Revision ID: 0244
Revises: 0243
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0244"
down_revision: str | None = "0243"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CODE = "remote.supervise"
#: Whoever holds this when the upgrade runs gets CODE.
HELD_WITH = "orgs.manage"


def upgrade() -> None:
    op.execute(
        "INSERT INTO permissions (id, code, module, action) "
        f"VALUES (gen_random_uuid(), '{CODE}', 'remote', 'supervise')"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT rp.role_id, granted.id FROM role_permissions rp "
        "JOIN permissions held ON held.id = rp.permission_id "
        f"JOIN permissions granted ON granted.code = '{CODE}' "
        f"WHERE held.code = '{HELD_WITH}'"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code = '{CODE}')"
    )
    op.execute(f"DELETE FROM permissions WHERE code = '{CODE}'")
