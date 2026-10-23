"""agent self-update: release channels, rollouts in rings, version reporting

`docs/modules/agent-distribution/spec-self-update.md` §3.3, §6, §8.3.

- `agent_releases.channel` (`pilot|stable`, default `pilot`): the platform
  operator decides maturity; a non-pilot ring needs `stable`.
- `agent_releases.release_signature`: the offline Ed25519 release key's
  signature over version, platform and digest, passed to the agent as-is.
- `agents.agent_version_reported_at`: when the gateway last recorded the
  version header. `agent_version` and `capabilities` already exist.
- Three org-scoped tables under forced RLS. One active rollout per org is a
  partial unique index, so it holds under concurrency.
- Permission codes: `agent.update.read` (org-admin, technician),
  `agent.update.rollout` and `agent.update.rollback` (org-admin). None is a
  platform-operator code: they act on one org's devices.

Revision ID: 0095
Revises: 0083
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0095"
down_revision: str | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

_TABLES = ["agent_update_rollouts", "agent_update_rings", "agent_update_targets"]

PERMISSIONS = [
    ("agent.update.read", "agent", "update.read"),
    ("agent.update.rollout", "agent", "update.rollout"),
    ("agent.update.rollback", "agent", "update.rollback"),
]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": [c for c, _, _ in PERMISSIONS],
    "org-admin": [c for c, _, _ in PERMISSIONS],
    "technician": ["agent.update.read"],
}


def _uuid(name: str, *fk: str, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        *(sa.ForeignKey(f) for f in fk),
        nullable=nullable,
    )


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if now else None,
    )


def upgrade() -> None:
    op.add_column(
        "agent_releases",
        sa.Column("channel", sa.String(16), nullable=False, server_default="pilot"),
    )
    op.create_check_constraint(
        "ck_agent_releases_channel", "agent_releases", "channel IN ('pilot', 'stable')"
    )
    op.add_column("agent_releases", sa.Column("release_signature", sa.String(128), nullable=True))
    op.add_column("agents", _ts("agent_version_reported_at"))

    op.create_table(
        "agent_update_rollouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", "organizations.id"),
        sa.Column("target_version", sa.String(64), nullable=False),
        sa.Column("is_rollback", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("batch_size", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("failure_threshold_pct", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("health_window_s", sa.Integer(), nullable=False, server_default="600"),
        sa.Column("reason", sa.String(500), nullable=False, server_default=""),
        sa.Column("pause_reason", sa.String(64), nullable=True),
        _uuid("created_by", "users.id"),
        _uuid("started_by", "users.id", nullable=True),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        _ts("started_at"),
        _ts("completed_at"),
        sa.CheckConstraint(
            "state IN ('draft', 'running', 'paused', 'completed', 'cancelled')",
            name="ck_agent_update_rollouts_state",
        ),
        sa.CheckConstraint("batch_size BETWEEN 1 AND 100", name="ck_agent_update_rollouts_batch"),
        sa.CheckConstraint(
            "failure_threshold_pct BETWEEN 0 AND 100", name="ck_agent_update_rollouts_threshold"
        ),
        sa.CheckConstraint(
            "health_window_s BETWEEN 120 AND 3600", name="ck_agent_update_rollouts_window"
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_update_rollouts_active ON agent_update_rollouts (org_id)"
        " WHERE state IN ('running', 'paused')"
    )

    op.create_table(
        "agent_update_rings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", "organizations.id"),
        _uuid("rollout_id", "agent_update_rollouts.id"),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_pilot", sa.Boolean(), nullable=False, server_default=sa.false()),
        _uuid("device_group_id", "device_groups.id", nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        _uuid("activated_by", "users.id", nullable=True),
        _ts("activated_at"),
        _ts("completed_at"),
        sa.UniqueConstraint("rollout_id", "ordinal", name="uq_agent_update_rings_ordinal"),
        sa.CheckConstraint(
            "state IN ('pending', 'active', 'done')", name="ck_agent_update_rings_state"
        ),
        sa.CheckConstraint("NOT is_pilot OR ordinal = 0", name="ck_agent_update_rings_pilot"),
    )

    op.create_table(
        "agent_update_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid("org_id", "organizations.id"),
        _uuid("rollout_id", "agent_update_rollouts.id"),
        _uuid("ring_id", "agent_update_rings.id"),
        _uuid("device_id", "devices.id"),
        _uuid("release_id", "agent_releases.id", nullable=True),
        sa.Column("from_version", sa.String(64), nullable=False, server_default=""),
        _uuid("job_id", nullable=True),
        _uuid("probe_job_id", nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(64), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("next_attempt_at"),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.UniqueConstraint("rollout_id", "device_id", name="uq_agent_update_targets_device"),
        sa.CheckConstraint(
            "state IN ('pending', 'dispatched', 'verifying', 'succeeded', 'rolled_back', "
            "'failed', 'refused', 'needs_manual_update', 'lost', 'skipped')",
            name="ck_agent_update_targets_state",
        ),
    )
    op.create_index(
        "ix_agent_update_targets_ring_state", "agent_update_targets", ["ring_id", "state"]
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
    code_list = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({code_list}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({code_list})")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    for table in reversed(_TABLES):
        op.drop_table(table)
    op.drop_column("agents", "agent_version_reported_at")
    op.drop_column("agent_releases", "release_signature")
    op.drop_constraint("ck_agent_releases_channel", "agent_releases", type_="check")
    op.drop_column("agent_releases", "channel")
