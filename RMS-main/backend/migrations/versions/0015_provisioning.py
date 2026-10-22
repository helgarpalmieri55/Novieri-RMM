"""Provisioning engine: OS images, provision profiles, provision runs, RBAC.

docs/modules/provisioning/spec.md §4 (data model) and §11 (RBAC).

Revision ID: 0015
Revises: 0014
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("provisioning.read", "provisioning", "read"),
    ("provisioning.manage", "provisioning", "manage"),
    ("provisioning.run", "provisioning", "run"),
]
_ALL_PROVISIONING = ["provisioning.read", "provisioning.manage", "provisioning.run"]
ROLE_GRANTS = {
    "super-admin": _ALL_PROVISIONING,
    "org-admin": _ALL_PROVISIONING,
    "technician": ["provisioning.read", "provisioning.run"],
    "auditor": ["provisioning.read"],
    "read-only": ["provisioning.read"],
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
        CREATE TABLE os_image (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            os_family varchar(16) NOT NULL,
            name varchar(255) NOT NULL,
            kind varchar(16) NOT NULL,
            artifact_ref text,
            artifact_sha256 varchar(64),
            params jsonb NOT NULL DEFAULT '{}'::jsonb,
            state varchar(16) NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_os_image_family ON os_image (org_id, os_family)")
    op.execute(
        """
        CREATE TABLE provision_profile (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            name varchar(255) NOT NULL,
            image_id uuid NOT NULL REFERENCES os_image(id),
            post_config jsonb NOT NULL DEFAULT '{}'::jsonb,
            target_scope jsonb NOT NULL DEFAULT '{}'::jsonb,
            enabled boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provision_profile_enabled ON provision_profile (org_id, enabled)"
    )
    op.execute(
        """
        CREATE TABLE provision_run (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            profile_id uuid NOT NULL REFERENCES provision_profile(id),
            job_id uuid REFERENCES jobs(id),
            device_id uuid REFERENCES devices(id),
            target_ref text,
            enrollment_token_id uuid REFERENCES enrollment_tokens(id),
            answer_ref text,
            state varchar(20) NOT NULL DEFAULT 'pending',
            log_ref text,
            started_at timestamptz,
            finished_at timestamptz,
            error text,
            requested_by uuid REFERENCES users(id),
            actor_type varchar(10),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_provision_run_state ON provision_run (org_id, state)")

    for t in ("os_image", "provision_profile", "provision_run"):
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
        "(SELECT id FROM permissions WHERE module = 'provisioning')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'provisioning'")
    for t in ("provision_run", "provision_profile", "os_image"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
