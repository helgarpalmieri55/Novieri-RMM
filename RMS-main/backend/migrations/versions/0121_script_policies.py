"""script_policies: scheduled scripts per device group

docs/modules/app-deploy/spec.md §9.

- `script_policies`: script x device group x interval, optionally held to each
  device's patch maintenance window, with output retention and an optional
  alert on failure. FORCE RLS.
- `script_runs` gains `policy_id` (the schedule that dispatched it) and
  `output` (what the script printed, bounded; cleared past `keep_runs`).
- `ck_health_check_type` gains `script.policy_failed`, a server-evaluated check
  a health profile holds to route a failed scheduled run to notify / ticket.
- `deploy.script.schedule` for super-admin and org-admin.

Revision ID: 0121
Revises: 0120
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0121"
down_revision: str | None = "0120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

_CHECK_TYPES = (
    "cpu.busy_pct",
    "memory.used_pct",
    "disk.free_pct",
    "disk.free_bytes",
    "service.state",
    "process.present",
    "disk.smart_ok",
    "posture.disk_encryption",
    "posture.antivirus_healthy",
    "posture.firewall_enabled",
    "eventlog.disk_error",
    "eventlog.service_crash",
    "eventlog.kernel_fault",
    "agent.offline",
    "backup.success_age_h",
    "backup.last_run_failed",
    "backup.offsite_lag_h",
    "backup.restore_test_age_d",
)
_NEW_CHECK = "script.policy_failed"

PERMISSIONS = [("deploy.script.schedule", "deploy", "script.schedule")]
ROLES = ("super-admin", "org-admin")


def _check_constraint(types: Sequence[str]) -> str:
    listed = ", ".join(f"'{t}'" for t in types)
    return (
        "ALTER TABLE health_check ADD CONSTRAINT ck_health_check_type "
        f"CHECK (check_type IN ({listed}))"
    )


def upgrade() -> None:
    op.create_table(
        "script_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "script_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scripts.id"), nullable=False
        ),
        sa.Column(
            "device_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("device_groups.id"),
            nullable=False,
        ),
        sa.Column("interval_hours", sa.Integer(), nullable=False),
        sa.Column("in_maintenance_window", sa.Boolean(), nullable=False),
        sa.Column("alert_on_failure", sa.Boolean(), nullable=False),
        sa.Column("keep_runs", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("org_id", "name", name="uq_script_policies_org_name"),
        sa.CheckConstraint("interval_hours BETWEEN 1 AND 720", name="ck_script_policies_interval"),
        sa.CheckConstraint("keep_runs BETWEEN 1 AND 500", name="ck_script_policies_keep_runs"),
    )
    op.execute("ALTER TABLE script_policies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE script_policies FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON script_policies "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )

    op.add_column(
        "script_runs",
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("script_policies.id"),
            nullable=True,
        ),
    )
    op.add_column("script_runs", sa.Column("output", sa.Text(), nullable=True))
    op.create_index(
        "ix_script_runs_policy",
        "script_runs",
        ["org_id", "policy_id", "device_id", "created_at"],
    )

    op.execute("ALTER TABLE health_check DROP CONSTRAINT ck_health_check_type")
    op.execute(_check_constraint(_CHECK_TYPES + (_NEW_CHECK,)))

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    code_list = ", ".join(f"'{c}'" for c, _, _ in PERMISSIONS)
    for role in ROLES:
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

    checks = f"SELECT id FROM health_check WHERE check_type = '{_NEW_CHECK}'"
    alerts = f"SELECT id FROM health_alert WHERE check_id IN ({checks})"
    op.execute(f"DELETE FROM health_alert_action WHERE alert_id IN ({alerts})")
    op.execute(
        f"UPDATE health_alert SET previous_alert_id = NULL WHERE previous_alert_id IN ({alerts})"
    )
    op.execute(f"DELETE FROM health_alert WHERE check_id IN ({checks})")
    op.execute(f"DELETE FROM device_health_check_state WHERE check_id IN ({checks})")
    op.execute(f"DELETE FROM health_flap_state WHERE check_id IN ({checks})")
    op.execute(f"DELETE FROM health_check WHERE check_type = '{_NEW_CHECK}'")
    op.execute("ALTER TABLE health_check DROP CONSTRAINT ck_health_check_type")
    op.execute(_check_constraint(_CHECK_TYPES))

    op.drop_index("ix_script_runs_policy", table_name="script_runs")
    op.drop_column("script_runs", "output")
    op.drop_column("script_runs", "policy_id")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON script_policies")
    op.drop_table("script_policies")
