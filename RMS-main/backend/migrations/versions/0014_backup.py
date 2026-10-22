"""Backup engine: repositories, policies, runs, points, restore jobs, RBAC.

docs/modules/backup/spec.md §4 (data model) and §11 (RBAC).

Revision ID: 0014
Revises: 0013
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("backup.read", "backup", "read"),
    ("backup.policy.manage", "backup", "policy.manage"),
    ("backup.run", "backup", "run"),
    ("backup.restore", "backup", "restore"),
]
_ALL_BACKUP = ["backup.read", "backup.policy.manage", "backup.run", "backup.restore"]
ROLE_GRANTS = {
    "super-admin": _ALL_BACKUP,
    "org-admin": _ALL_BACKUP,
    "technician": ["backup.read", "backup.run", "backup.restore"],
    "auditor": ["backup.read"],
    "read-only": ["backup.read"],
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
        CREATE TABLE backup_repositories (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            name varchar(200) NOT NULL,
            storage_ref text NOT NULL,
            encryption_key_ref text NOT NULL,
            dedup_params jsonb NOT NULL DEFAULT '{}'::jsonb,
            state varchar(16) NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, name)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE backup_policies (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            repository_id uuid NOT NULL REFERENCES backup_repositories(id),
            device_group_id uuid REFERENCES device_groups(id),
            sources jsonb NOT NULL DEFAULT '{}'::jsonb,
            window_cron varchar(120),
            retention_gfs jsonb NOT NULL DEFAULT '{}'::jsonb,
            enabled boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE backup_runs (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_id uuid NOT NULL REFERENCES devices(id),
            repository_id uuid NOT NULL REFERENCES backup_repositories(id),
            job_id uuid REFERENCES jobs(id),
            state varchar(16) NOT NULL,
            bytes_protected bigint,
            bytes_stored bigint,
            chunk_count integer,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            error text,
            requested_by uuid REFERENCES users(id),
            actor_type varchar(10) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_backup_runs_state ON backup_runs (org_id, state)")
    op.execute(
        """
        CREATE TABLE backup_points (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_id uuid NOT NULL REFERENCES devices(id),
            repository_id uuid NOT NULL REFERENCES backup_repositories(id),
            backup_run_id uuid NOT NULL REFERENCES backup_runs(id),
            taken_at timestamptz NOT NULL DEFAULT now(),
            manifest_ref text NOT NULL,
            size bigint NOT NULL,
            dedup_stats jsonb NOT NULL DEFAULT '{}'::jsonb,
            state varchar(16) NOT NULL,
            verified_at timestamptz,
            restore_tested_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_backup_points_device ON backup_points (org_id, device_id, taken_at)"
    )
    op.execute(
        """
        CREATE TABLE restore_jobs (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_id uuid NOT NULL REFERENCES devices(id),
            backup_point_id uuid NOT NULL REFERENCES backup_points(id),
            job_id uuid REFERENCES jobs(id),
            selection jsonb NOT NULL DEFAULT '{}'::jsonb,
            target jsonb NOT NULL DEFAULT '{}'::jsonb,
            mode varchar(12) NOT NULL,
            state varchar(16) NOT NULL,
            restored_bytes bigint,
            finished_at timestamptz,
            error text,
            requested_by uuid REFERENCES users(id),
            actor_type varchar(10) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_restore_jobs_state ON restore_jobs (org_id, state)")

    for t in (
        "backup_repositories",
        "backup_policies",
        "backup_runs",
        "backup_points",
        "restore_jobs",
    ):
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
        "(SELECT id FROM permissions WHERE module = 'backup')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'backup'")
    for t in (
        "restore_jobs",
        "backup_points",
        "backup_runs",
        "backup_policies",
        "backup_repositories",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t}")
