"""Change management: change requests, approvals, links, audit index.

docs/modules/change_management/spec.md §4 (data model) and §11 (RBAC).

Revision ID: 0016
Revises: 0015
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("change.read", "change", "read"),
    ("change.request", "change", "request"),
    ("change.approve", "change", "approve"),
    ("change.manage", "change", "manage"),
]
_ALL_CHANGE = ["change.read", "change.request", "change.approve", "change.manage"]
ROLE_GRANTS = {
    "super-admin": _ALL_CHANGE,
    "org-admin": _ALL_CHANGE,
    "technician": ["change.read", "change.request"],
    "auditor": ["change.read"],
    "read-only": ["change.read"],
}


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (org_id = current_setting('app.current_org', true)::uuid) "
        "WITH CHECK (org_id = current_setting('app.current_org', true)::uuid)"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE change_request (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            type varchar(12) NOT NULL,
            title varchar(255) NOT NULL,
            description text NOT NULL,
            risk varchar(8) NOT NULL,
            affected jsonb NOT NULL DEFAULT '[]'::jsonb,
            window_start timestamptz,
            window_end timestamptz,
            rollback_plan text,
            state varchar(16) NOT NULL DEFAULT 'draft',
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_change_request_state ON change_request (org_id, state)")
    op.execute(
        "CREATE INDEX ix_change_request_window ON change_request (org_id, window_start)"
    )

    op.execute(
        """
        CREATE TABLE change_approval (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            change_id uuid NOT NULL REFERENCES change_request(id),
            approver_id uuid NOT NULL REFERENCES users(id),
            decision varchar(10) NOT NULL,
            comment text,
            is_retrospective boolean NOT NULL DEFAULT false,
            decided_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_change_approval_change ON change_approval (org_id, change_id)"
    )

    op.execute(
        """
        CREATE TABLE change_link (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            change_id uuid NOT NULL REFERENCES change_request(id),
            action_type varchar(20) NOT NULL,
            action_id uuid NOT NULL,
            linked_at timestamptz NOT NULL DEFAULT now(),
            linked_by uuid NOT NULL REFERENCES users(id),
            CONSTRAINT uq_change_link UNIQUE (org_id, change_id, action_type, action_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_change_link_change ON change_link (org_id, change_id)")

    op.execute(
        """
        CREATE TABLE change_external_ref (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            change_id uuid NOT NULL REFERENCES change_request(id),
            system varchar(16) NOT NULL,
            external_id text NOT NULL,
            synced_at timestamptz
        )
        """
    )

    for t in ("change_request", "change_approval", "change_link", "change_external_ref"):
        _rls(t)

    op.execute(
        "CREATE INDEX ix_audit_target ON audit_events (org_id, target_type, target_id)"
    )

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for name, codes in ROLE_GRANTS.items():
        code_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{name}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE module = 'change')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'change'")
    op.execute("DROP INDEX IF EXISTS ix_audit_target")
    for t in (
        "change_external_ref",
        "change_link",
        "change_approval",
        "change_request",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t}")
