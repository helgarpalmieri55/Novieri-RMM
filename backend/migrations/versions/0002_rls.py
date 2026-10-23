"""Row-Level Security: tenant isolation enforced by the database.

Every table carrying org_id gets ENABLE + FORCE RLS and a tenant_isolation
policy pinned to the app.current_org GUC (set per-transaction by
app.core.tenancy.org_session). The GUC read uses missing_ok=true so a session
with no tenant context sees zero rows and cannot insert.

Revision ID: 0002
Revises: 0001
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORG_CTX = "current_setting('app.current_org', true)::uuid"

# Tables whose rows belong to exactly one tenant via org_id.
TENANT_TABLES = [
    "users",
    "user_roles",
    "device_groups",
    "devices",
    "device_tags",
    "agents",
    "policies",
    "credentials",
    "jobs",
    "job_logs",
    "audit_events",
    "enrollment_tokens",
]


def upgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {ORG_CTX}) WITH CHECK (org_id = {ORG_CTX})"
        )

    # organizations: a tenant session sees only its own org row.
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organizations FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON organizations "
        f"USING (id = {ORG_CTX}) WITH CHECK (id = {ORG_CTX})"
    )

    # roles: global built-ins (org_id IS NULL) are readable by every tenant,
    # but a tenant can only write roles belonging to itself.
    op.execute("ALTER TABLE roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY roles_select ON roles FOR SELECT "
        f"USING (org_id IS NULL OR org_id = {ORG_CTX})"
    )
    op.execute(f"CREATE POLICY roles_insert ON roles FOR INSERT WITH CHECK (org_id = {ORG_CTX})")
    op.execute(
        f"CREATE POLICY roles_update ON roles FOR UPDATE "
        f"USING (org_id = {ORG_CTX}) WITH CHECK (org_id = {ORG_CTX})"
    )
    op.execute(f"CREATE POLICY roles_delete ON roles FOR DELETE USING (org_id = {ORG_CTX})")
    # permissions and role_permissions are global reference data: no org_id, no RLS.


def downgrade() -> None:
    for table in [*TENANT_TABLES, "organizations"]:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    for policy in ["roles_select", "roles_insert", "roles_update", "roles_delete"]:
        op.execute(f"DROP POLICY {policy} ON roles")
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles DISABLE ROW LEVEL SECURITY")
