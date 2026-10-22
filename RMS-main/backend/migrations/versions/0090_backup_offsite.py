"""backup off-site copy: targets, copy bookkeeping, recovery keys

spec-server-backup.md §6.2-§6.6 and the operator decisions of 2026-09-23:
local MinIO plus an off-site S3 copy, Object Lock in compliance mode for 30
days, and a recovery key that must be proven before off-site is enabled.

- `backup_offsite_targets`: one S3-compatible bucket per row. The credential is
  an ordinary `credentials` row of type `s3_offsite` (vault-sealed); this table
  holds only its id. `lock_mode` has no `none`: a bucket without Object Lock is
  refused, because the off-site copy is the ransomware answer.
- `backup_repositories.offsite_target_id` attaches a target; attaching is what
  "off-site on" means.
- `backup_point_copies` / `backup_chunk_copies`: per-target bookkeeping that
  makes replication resumable at chunk granularity and lets off-site retention
  differ from local.
- `backup_recovery_keys`: an operator-held X25519 public key and the proof of
  possession. The private key never reaches the server.
- `backup_points.restore_test_level` / `restore_test_scope`: what a test
  restore proved, so a sampled read is never reported as a full restore.

Permissions `backup.offsite.manage` and `backup.recovery.manage` go to
super-admin and org-admin, not technician (§9).

Revision ID: 0090
Revises: 0083
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0090"
down_revision: str | None = "0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

_TABLES = [
    "backup_offsite_targets",
    "backup_point_copies",
    "backup_chunk_copies",
    "backup_recovery_keys",
]

PERMISSIONS = [
    ("backup.offsite.manage", "backup", "offsite.manage"),
    ("backup.recovery.manage", "backup", "recovery.manage"),
]
_ALL = [c for c, _, _ in PERMISSIONS]
ROLE_GRANTS: dict[str, list[str]] = {"super-admin": _ALL, "org-admin": _ALL}


def _uuid(name: str, *, fk: str | None = None, nullable: bool = False) -> sa.Column:
    args: list[sa.SchemaItem] = [sa.ForeignKey(fk)] if fk else []
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, nullable=nullable)


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if default_now else None,
    )


def upgrade() -> None:
    op.create_table(
        "backup_offsite_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", fk="organizations.id"),
        sa.Column("name", sa.String(200), nullable=False),
        # NULL = the provider's default endpoint (AWS S3).
        sa.Column("endpoint", sa.Text, nullable=True),
        sa.Column("region", sa.String(64), nullable=False, server_default="us-east-1"),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("prefix", sa.String(255), nullable=False, server_default=""),
        sa.Column("addressing", sa.String(8), nullable=False, server_default="path"),
        _uuid("credential_id", fk="credentials.id"),
        sa.Column("lock_mode", sa.String(12), nullable=False, server_default="compliance"),
        sa.Column("immutable_days", sa.Integer, nullable=False, server_default="30"),
        sa.Column(
            "retention_gfs",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("max_mbps", sa.Integer, nullable=True),
        sa.Column("state", sa.String(12), nullable=False, server_default="untested"),
        _ts("test_requested_at"),
        sa.Column(
            "last_test", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        _ts("last_ok_at"),
        sa.Column("last_error", sa.Text, nullable=True),
        _uuid("created_by", fk="users.id", nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        sa.UniqueConstraint("org_id", "name", name="uq_backup_offsite_targets_org_name"),
        sa.CheckConstraint("addressing IN ('path', 'virtual')", name="ck_offsite_addressing"),
        sa.CheckConstraint(
            "lock_mode IN ('compliance', 'governance')", name="ck_offsite_lock_mode"
        ),
        sa.CheckConstraint(
            "immutable_days BETWEEN 1 AND 90", name="ck_offsite_immutable_days"
        ),
        sa.CheckConstraint("max_mbps IS NULL OR max_mbps > 0", name="ck_offsite_max_mbps"),
        sa.CheckConstraint(
            "state IN ('untested', 'testing', 'ok', 'failing')", name="ck_offsite_state"
        ),
    )

    op.add_column(
        "backup_repositories",
        sa.Column(
            "offsite_target_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_offsite_targets.id"),
            nullable=True,
        ),
    )

    op.create_table(
        "backup_point_copies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", fk="organizations.id"),
        _uuid("point_id", fk="backup_points.id"),
        _uuid("target_id", fk="backup_offsite_targets.id"),
        sa.Column("state", sa.String(12), nullable=False, server_default="pending"),
        _ts("copied_at"),
        sa.Column("bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("chunks_copied", sa.Integer, nullable=False, server_default="0"),
        _ts("locked_until"),
        sa.Column("manifest_object", sa.Text, nullable=True),
        sa.Column("manifest_version_id", sa.Text, nullable=True),
        sa.Column("catalog_version_id", sa.Text, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        sa.UniqueConstraint("point_id", "target_id", name="uq_backup_point_copies_point_target"),
        sa.CheckConstraint(
            "state IN ('pending', 'copying', 'complete', 'failed', 'expired')",
            name="ck_backup_point_copies_state",
        ),
    )
    op.create_index(
        "ix_backup_point_copies_target_state",
        "backup_point_copies",
        ["org_id", "target_id", "state"],
    )

    op.create_table(
        "backup_chunk_copies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", fk="organizations.id"),
        _uuid("repository_id", fk="backup_repositories.id"),
        _uuid("target_id", fk="backup_offsite_targets.id"),
        sa.Column("chunk_id", sa.String(64), nullable=False),
        sa.Column("object_name", sa.Text, nullable=False),
        sa.Column("version_id", sa.Text, nullable=True),
        _ts("locked_until"),
        _ts("verified_at"),
        _ts("created_at", nullable=False, default_now=True),
        sa.UniqueConstraint(
            "target_id", "repository_id", "chunk_id", name="uq_backup_chunk_copies_target_chunk"
        ),
    )

    op.create_table(
        "backup_recovery_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", fk="organizations.id"),
        # Base64 of the raw 32-byte X25519 public key. Public by definition.
        sa.Column("public_key", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        # SHA-256 of the outstanding challenge; the plaintext is never stored.
        sa.Column("challenge_sha256", sa.String(64), nullable=True),
        _ts("challenge_expires_at"),
        _ts("proven_at"),
        _uuid("created_by", fk="users.id", nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ts("retired_at"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_backup_recovery_keys_active"
        " ON backup_recovery_keys (org_id, fingerprint) WHERE retired_at IS NULL"
    )

    op.add_column(
        "backup_points", sa.Column("restore_test_level", sa.String(12), nullable=True)
    )
    op.add_column(
        "backup_points", sa.Column("restore_test_scope", sa.String(12), nullable=True)
    )

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
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
    code_list = ", ".join(f"'{c}'" for c in _ALL)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({code_list}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({code_list})")
    op.drop_column("backup_points", "restore_test_scope")
    op.drop_column("backup_points", "restore_test_level")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("backup_recovery_keys")
    op.drop_table("backup_chunk_copies")
    op.drop_index("ix_backup_point_copies_target_state", table_name="backup_point_copies")
    op.drop_table("backup_point_copies")
    op.drop_column("backup_repositories", "offsite_target_id")
    op.drop_table("backup_offsite_targets")
