"""Patch management: catalog, status, policies, approvals, runs, RBAC.

Revision ID: 0011
Revises: 0010
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("patch.read", "patch", "read"),
    ("patch.scan", "patch", "scan"),
    ("patch.approve", "patch", "approve"),
    ("patch.apply", "patch", "apply"),
    ("patch.policy.manage", "patch", "policy.manage"),
]
_ALL_PATCH = ["patch.read", "patch.scan", "patch.approve", "patch.apply", "patch.policy.manage"]
ROLE_GRANTS = {
    "super-admin": _ALL_PATCH,
    "org-admin": _ALL_PATCH,
    "technician": ["patch.read", "patch.scan", "patch.apply"],
    "auditor": ["patch.read"],
    "read-only": ["patch.read"],
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
        CREATE TABLE patches (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            os_family varchar(8) NOT NULL,
            source varchar(20) NOT NULL,
            identifier varchar(400) NOT NULL,
            title text NOT NULL,
            severity varchar(12) NOT NULL,
            kb varchar(40),
            cve text[],
            superseded_by uuid REFERENCES patches(id),
            priority integer,
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, os_family, source, identifier)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE device_patch_status (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_id uuid NOT NULL REFERENCES devices(id),
            patch_id uuid NOT NULL REFERENCES patches(id),
            state varchar(16) NOT NULL,
            detected_at timestamptz NOT NULL DEFAULT now(),
            applied_at timestamptz,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (device_id, patch_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_dps_state ON device_patch_status (org_id, state)")
    op.execute("CREATE INDEX ix_dps_patch ON device_patch_status (org_id, patch_id)")
    op.execute(
        """
        CREATE TABLE patch_policies (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_group_id uuid REFERENCES device_groups(id),
            ring_name varchar(40) NOT NULL DEFAULT 'broad',
            ring_order integer NOT NULL DEFAULT 100,
            maintenance_window varchar(120),
            auto_approve_security boolean NOT NULL DEFAULT false,
            reboot varchar(12) NOT NULL DEFAULT 'if_required',
            reboot_deferral_hours integer NOT NULL DEFAULT 24,
            scan_interval_hours integer NOT NULL DEFAULT 24,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, device_group_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE patch_approvals (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            patch_id uuid NOT NULL REFERENCES patches(id),
            ring_name varchar(40) NOT NULL,
            approved_by uuid NOT NULL REFERENCES users(id),
            approved_at timestamptz NOT NULL DEFAULT now(),
            revoked boolean NOT NULL DEFAULT false,
            revoked_by uuid REFERENCES users(id),
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, patch_id, ring_name)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE patch_runs (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            job_id uuid NOT NULL REFERENCES jobs(id),
            device_id uuid NOT NULL REFERENCES devices(id),
            results jsonb NOT NULL DEFAULT '{}'::jsonb,
            reboot_required boolean NOT NULL DEFAULT false,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_patch_runs_device ON patch_runs (org_id, device_id, id)")

    for t in ("patches", "device_patch_status", "patch_policies", "patch_approvals", "patch_runs"):
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
    op.execute("DELETE FROM role_permissions WHERE permission_id IN "
               "(SELECT id FROM permissions WHERE module = 'patch')")
    op.execute("DELETE FROM permissions WHERE module = 'patch'")
    for t in ("patch_runs", "patch_approvals", "patch_policies", "device_patch_status", "patches"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
