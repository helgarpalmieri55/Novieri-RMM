"""BCP/DR: plans, tests, findings with RLS and RBAC.

docs/modules/bcp/spec.md §4 (data model) and §11 (RBAC).

Revision ID: 0017
Revises: 0016
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("bcp.read", "bcp", "read"),
    ("bcp.manage", "bcp", "manage"),
    ("bcp.approve", "bcp", "approve"),
]
_ALL_BCP = ["bcp.read", "bcp.manage", "bcp.approve"]
ROLE_GRANTS = {
    "super-admin": _ALL_BCP,
    "org-admin": _ALL_BCP,
    "technician": ["bcp.read", "bcp.manage"],
    "auditor": ["bcp.read"],
    "read-only": ["bcp.read"],
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
        CREATE TABLE bcp_plan (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            name varchar(255) NOT NULL,
            description text NOT NULL,
            scope jsonb NOT NULL DEFAULT '[]'::jsonb,
            rto_minutes integer,
            rpo_minutes integer,
            procedure_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            dependencies jsonb NOT NULL DEFAULT '[]'::jsonb,
            review_interval_days integer NOT NULL DEFAULT 365,
            last_reviewed_at timestamptz,
            approved_by uuid REFERENCES users(id),
            state varchar(16) NOT NULL DEFAULT 'active',
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_bcp_plan_state ON bcp_plan (org_id, state)")

    op.execute(
        """
        CREATE TABLE bcp_test (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            plan_id uuid NOT NULL REFERENCES bcp_plan(id),
            type varchar(16) NOT NULL,
            scheduled_for timestamptz,
            executed_at timestamptz,
            executed_by uuid REFERENCES users(id),
            outcome varchar(10) NOT NULL DEFAULT 'pending',
            rto_met boolean,
            rpo_met boolean,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            notes text,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_bcp_test_plan ON bcp_test (org_id, plan_id)")

    op.execute(
        """
        CREATE TABLE bcp_test_step (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            test_id uuid NOT NULL REFERENCES bcp_test(id),
            ordinal integer NOT NULL,
            description text NOT NULL,
            result varchar(10) NOT NULL DEFAULT 'pending',
            evidence_ref text
        )
        """
    )
    op.execute("CREATE INDEX ix_bcp_test_step_test ON bcp_test_step (org_id, test_id)")

    op.execute(
        """
        CREATE TABLE bcp_finding (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            test_id uuid NOT NULL REFERENCES bcp_test(id),
            description text NOT NULL,
            severity varchar(10) NOT NULL,
            remediation_state varchar(16) NOT NULL DEFAULT 'open',
            change_ref uuid,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX ix_bcp_finding_test ON bcp_finding (org_id, test_id)")
    op.execute(
        "CREATE INDEX ix_bcp_finding_state ON bcp_finding (org_id, remediation_state)"
    )

    for t in ("bcp_plan", "bcp_test", "bcp_test_step", "bcp_finding"):
        _rls(t)

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
        "(SELECT id FROM permissions WHERE module = 'bcp')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'bcp'")
    for t in (
        "bcp_finding",
        "bcp_test_step",
        "bcp_test",
        "bcp_plan",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t}")
