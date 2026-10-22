"""change_gates: require an approved change before patching or deploying

docs/modules/change-mgmt/spec.md §11.

- `change_gates`: per org, a device group (or NULL = every device) whose patch
  approvals and/or deployments need a linked change in state approved or
  implementing. One row per (org, group), NULLS NOT DISTINCT so there is one
  org-wide row at most. FORCE RLS.
- `change_request.origin`: `manual`, or `auto` for the evidence record the
  platform writes when a patch approval or deployment happens with no change
  and no gate.
- `change.gate.manage` for super-admin and org-admin.

Revision ID: 0122
Revises: 0121
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0122"
down_revision: str | None = "0121"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

PERMISSIONS = [("change.gate.manage", "change", "gate.manage")]
ROLES = ("super-admin", "org-admin")


def upgrade() -> None:
    op.create_table(
        "change_gates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "device_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("device_groups.id"),
            nullable=True,
        ),
        sa.Column("applies_to_patches", sa.Boolean(), nullable=False),
        sa.Column("applies_to_deployments", sa.Boolean(), nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index(
        "uq_change_gates_org_group",
        "change_gates",
        ["org_id", "device_group_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.execute("ALTER TABLE change_gates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE change_gates FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON change_gates "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )

    op.add_column(
        "change_request",
        sa.Column("origin", sa.String(8), nullable=False, server_default="manual"),
    )
    op.create_check_constraint(
        "ck_change_request_origin", "change_request", "origin IN ('manual', 'auto')"
    )

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    code_list = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    for role in ROLES:
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
    op.drop_constraint("ck_change_request_origin", "change_request", type_="check")
    op.drop_column("change_request", "origin")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON change_gates")
    op.drop_index("uq_change_gates_org_group", table_name="change_gates")
    op.drop_table("change_gates")
