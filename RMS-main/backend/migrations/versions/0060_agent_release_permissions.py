"""agent distribution: seed `agent.release.read` and `agent.release.manage`

docs/modules/agent-distribution/spec.md §4.3.

NO TABLES HERE, the same shape as 0057 (`platform.errors.*`) and 0054
(`platform.status.read`): the table is 0059, kept separate so this RBAC change
can be reviewed and reverted without touching schema.

TWO CODES, NOT ONE. Reading the release list and *publishing an artefact* are
different privileges, and the gap between them is wider than the usual
read/write split in this file. `agent.release.manage` writes the bytes every
endpoint in the fleet will execute -- it is the closest thing in this platform
to arbitrary code execution on every managed machine, gated only by whoever
holds the code. `agent.release.read` is needed by anyone who installs an agent,
because generating an install command is a read of the catalogue plus a token
mint.

WHO GETS THEM. `org-admin` only, and that is not a free choice --
`tests/test_rbac.py::test_org_admin_has_all_permissions` asserts an org-admin's
codes equal set(PERMISSIONS), so a code in the dict and unseeded here turns that
red (it did, which is how this migration came to be written).

Deliberately NOT seeded to `technician`, unlike most read codes. A technician
installing an agent is a normal thing to want, and an org that wants it should
grant `agent.release.read` explicitly -- because the same page also mints
enrollment tokens, and a token mints a device identity inside the tenant
(agent/packaging/README.md treats it as a credential). Widening that by default
across every deployment is not this migration's call to make.

Revision ID: 0060
Revises: 0059
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODES = ("agent.release.read", "agent.release.manage")


def upgrade() -> None:
    for code in _CODES:
        action = code.removeprefix("agent.")
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', 'agent', '{action}')"
        )
    code_list = ", ".join(f"'{c}'" for c in _CODES)
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        f"WHERE r.name = 'org-admin' AND p.code IN ({code_list})"
    )


def downgrade() -> None:
    # role_permissions first: it references permissions, and dropping the
    # permission rows out from under it is what leaves a downgrade half-applied.
    code_list = ", ".join(f"'{c}'" for c in _CODES)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({code_list}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({code_list})")
