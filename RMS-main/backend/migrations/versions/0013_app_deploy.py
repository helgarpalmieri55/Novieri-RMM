"""Application deployment: packages, deployments, results, scripts, script runs, RBAC.

docs/modules/app-deploy/spec.md §4 (data model) and §11 (RBAC).

Revision ID: 0013
Revises: 0012
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("deploy.read", "deploy", "read"),
    ("deploy.package.manage", "deploy", "package.manage"),
    ("deploy.create", "deploy", "create"),
    ("deploy.execute", "deploy", "execute"),
    ("deploy.script.manage", "deploy", "script.manage"),
    ("deploy.script.run", "deploy", "script.run"),
]
_ALL_DEPLOY = [
    "deploy.read",
    "deploy.package.manage",
    "deploy.create",
    "deploy.execute",
    "deploy.script.manage",
    "deploy.script.run",
]
ROLE_GRANTS = {
    "super-admin": _ALL_DEPLOY,
    "org-admin": _ALL_DEPLOY,
    "technician": ["deploy.read", "deploy.create", "deploy.execute", "deploy.script.run"],
    "auditor": ["deploy.read"],
    "read-only": ["deploy.read"],
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
        CREATE TABLE packages (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            name varchar(200) NOT NULL,
            version varchar(100) NOT NULL,
            os_family varchar(8) NOT NULL,
            installer_type varchar(20) NOT NULL,
            artifact_ref text,
            artifact_sha256 varchar(64),
            detection_rule jsonb NOT NULL DEFAULT '{}'::jsonb,
            uninstall_rule jsonb NOT NULL DEFAULT '{}'::jsonb,
            install_args text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, name, version, os_family)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE deployments (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            package_id uuid NOT NULL REFERENCES packages(id),
            action varchar(12) NOT NULL,
            target_type varchar(8) NOT NULL,
            target_id uuid NOT NULL,
            window_cron varchar(120),
            requested_by uuid REFERENCES users(id),
            state varchar(16) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_deployments_state ON deployments (org_id, state)")
    op.execute(
        """
        CREATE TABLE deployment_results (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            deployment_id uuid NOT NULL REFERENCES deployments(id),
            device_id uuid NOT NULL REFERENCES devices(id),
            job_id uuid REFERENCES jobs(id),
            state varchar(16) NOT NULL,
            exit_code integer,
            log_ref text,
            detected_after boolean,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (deployment_id, device_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_deployment_results_state ON deployment_results (org_id, state)")
    op.execute(
        """
        CREATE TABLE scripts (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            name varchar(200) NOT NULL,
            os_family varchar(8) NOT NULL,
            interpreter varchar(20) NOT NULL,
            body text NOT NULL,
            version integer NOT NULL DEFAULT 1,
            created_by uuid REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, name, version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE script_runs (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            script_id uuid NOT NULL REFERENCES scripts(id),
            script_version integer NOT NULL,
            target_type varchar(8) NOT NULL,
            target_id uuid NOT NULL,
            device_id uuid NOT NULL REFERENCES devices(id),
            job_id uuid REFERENCES jobs(id),
            state varchar(16) NOT NULL,
            exit_code integer,
            log_ref text,
            requested_by uuid REFERENCES users(id),
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (script_id, device_id, requested_by, created_at)
        )
        """
    )
    op.execute("CREATE INDEX ix_script_runs_state ON script_runs (org_id, state)")

    for t in ("packages", "deployments", "deployment_results", "scripts", "script_runs"):
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
        "(SELECT id FROM permissions WHERE module = 'deploy')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'deploy'")
    for t in ("script_runs", "scripts", "deployment_results", "deployments", "packages"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
