"""Remote control: sessions, policies, RBAC (docs/modules/remote-control/spec.md).

Revision ID: 0010
Revises: 0009
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("remote.control", "remote", "control"),
    ("remote.recordings.read", "remote", "recordings.read"),
    ("remote.policy.manage", "remote", "policy.manage"),
]
ROLE_GRANTS = {
    "super-admin": ["remote.control", "remote.recordings.read", "remote.policy.manage"],
    "org-admin": ["remote.control", "remote.recordings.read", "remote.policy.manage"],
    "technician": ["remote.control"],
    "auditor": ["remote.recordings.read"],
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
        CREATE TABLE remote_sessions (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_id uuid NOT NULL REFERENCES devices(id),
            technician_id uuid NOT NULL REFERENCES users(id),
            protocol varchar(8) NOT NULL,
            consent_mode varchar(12) NOT NULL,
            credential_id uuid REFERENCES credentials(id),
            state varchar(20) NOT NULL,
            relay_token_hash varchar(64),
            recording_ref text,
            recording_bytes bigint,
            started_at timestamptz,
            ended_at timestamptz,
            end_reason varchar(20),
            audit_event_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_remote_sessions_device ON remote_sessions (org_id, device_id, id)")
    op.execute("CREATE INDEX ix_remote_sessions_state ON remote_sessions (org_id, state)")
    op.execute(
        "CREATE INDEX ix_remote_sessions_recording ON remote_sessions (org_id, ended_at) "
        "WHERE recording_ref IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE remote_policies (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            device_group_id uuid REFERENCES device_groups(id),
            allow_unattended boolean NOT NULL DEFAULT false,
            record_sessions boolean NOT NULL DEFAULT true,
            allow_file_transfer boolean NOT NULL DEFAULT false,
            allow_clipboard boolean NOT NULL DEFAULT false,
            recording_retention_days integer NOT NULL DEFAULT 90,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, device_group_id)
        )
        """
    )
    _rls("remote_sessions")
    _rls("remote_policies")

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
        "(SELECT id FROM permissions WHERE module = 'remote')"
    )
    op.execute("DELETE FROM permissions WHERE module = 'remote'")
    op.execute("DROP TABLE IF EXISTS remote_sessions")
    op.execute("DROP TABLE IF EXISTS remote_policies")
