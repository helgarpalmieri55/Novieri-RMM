"""MCP server: the `mcp_revocation` table and the `mcp.use` permission.

`mcp_revocation` -- one row cuts an AI app (`client_id`, the token's `azp`) off
for the whole organization (`user_id` NULL) or for one person. The MCP server
reads it on every request. Tenant-scoped under forced RLS, the 0200 pattern. The
`(org_id, client_id)` index is the one lookup the server makes.

`mcp.use` -- who may connect an AI app and act through it as themselves. Seeded
to `super-admin` and `org-admin` only, the 0170 pattern: a technician gets it
when an admin grants it, not by default.

The `mcp_enabled` org switch needs no migration: it is a key in
`organizations.settings` (JSONB), absent meaning off.

Forward-only. `downgrade()` has never run in production (rollback is
restore-from-backup). CI runs upgrade->downgrade->upgrade.

Revision ID: 0210
Revises: 0200
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0210"
down_revision: str | None = "0200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

PERMISSIONS = [("mcp.use", "mcp", "use")]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": ["mcp.use"],
    "org-admin": ["mcp.use"],
}


def upgrade() -> None:
    op.create_table(
        "mcp_revocation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "revoked_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_mcp_revocation_lookup", "mcp_revocation", ["org_id", "client_id"])
    op.execute("ALTER TABLE mcp_revocation ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mcp_revocation FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON mcp_revocation "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )

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
    code_list = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({code_list}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({code_list})")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON mcp_revocation")
    op.drop_table("mcp_revocation")
