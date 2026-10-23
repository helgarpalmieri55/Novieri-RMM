"""backup health checks: four server-evaluated check types, seeded

spec-server-backup.md §8.1. Backup reuses the health module's alert lifecycle
and ticket path instead of building its own, so a failed or missed backup
raises an alert and, on critical, opens a ticket through the existing
`service_desk` route.

- `ck_health_check_type` gains the four types (it restates the registry in
  SQL; `tests/test_health_check_registry.py` compares both ways).
- Each org's DEFAULT profile (the org-wide assignment) gets the checks, where
  that profile has room under the 32-check cap and does not already hold one.
  Critical checks declare `open_ticket`; warnings only notify. An org with no
  default profile gets nothing: the health module never fabricates a profile.
  `policy_version` is not bumped: server-evaluated checks never reach an agent.

Revision ID: 0091
Revises: 0090
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0091"
down_revision: str | None = "0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASE_TYPES = (
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
)
# (type, comparator, threshold_num, threshold_bool, severity)
_SEEDS = (
    ("backup.success_age_h", "gt", "50", None, "critical"),
    ("backup.last_run_failed", "is", None, "true", "critical"),
    ("backup.offsite_lag_h", "gt", "24", None, "warning"),
    ("backup.restore_test_age_d", "gt", "35", None, "warning"),
)
_NEW_TYPES = tuple(t for t, *_ in _SEEDS)


def _constraint(types: Sequence[str]) -> str:
    listed = ", ".join(f"'{t}'" for t in types)
    return (
        "ALTER TABLE health_check ADD CONSTRAINT ck_health_check_type "
        f"CHECK (check_type IN ({listed}))"
    )


def upgrade() -> None:
    op.execute("ALTER TABLE health_check DROP CONSTRAINT ck_health_check_type")
    op.execute(_constraint(_BASE_TYPES + _NEW_TYPES))
    for check_type, comparator, num, flag, severity in _SEEDS:
        num_sql = num if num is not None else "NULL"
        flag_sql = flag if flag is not None else "NULL"
        ticket = "true" if severity == "critical" else "false"
        op.execute(
            f"""
            INSERT INTO health_check
                (id, org_id, profile_id, check_type, selector, comparator, threshold_num,
                 threshold_bool, for_ticks, severity, os_family, enabled, declares_notify,
                 declares_open_ticket, created_at, updated_at)
            SELECT gen_random_uuid(), a.org_id, a.profile_id, '{check_type}', '', '{comparator}',
                   {num_sql}, {flag_sql}, 1, '{severity}', NULL, true, true, {ticket},
                   now(), now()
              FROM health_profile_assignment a
             WHERE a.device_id IS NULL AND a.device_group_id IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM health_check c
                    WHERE c.profile_id = a.profile_id AND c.check_type = '{check_type}'
                      AND c.retired_at IS NULL)
               AND (SELECT count(*) FROM health_check c
                     WHERE c.profile_id = a.profile_id AND c.retired_at IS NULL) < 32
            """
        )


def downgrade() -> None:
    listed = ", ".join(f"'{t}'" for t in _NEW_TYPES)
    checks = f"SELECT id FROM health_check WHERE check_type IN ({listed})"
    alerts = f"SELECT id FROM health_alert WHERE check_id IN ({checks})"
    op.execute(f"DELETE FROM health_alert_action WHERE alert_id IN ({alerts})")
    op.execute(
        f"UPDATE health_alert SET previous_alert_id = NULL WHERE previous_alert_id IN ({alerts})"
    )
    op.execute(f"DELETE FROM health_alert WHERE check_id IN ({checks})")
    op.execute(f"DELETE FROM device_health_check_state WHERE check_id IN ({checks})")
    op.execute(f"DELETE FROM health_flap_state WHERE check_id IN ({checks})")
    op.execute(f"DELETE FROM health_check WHERE check_type IN ({listed})")
    op.execute("ALTER TABLE health_check DROP CONSTRAINT ck_health_check_type")
    op.execute(_constraint(_BASE_TYPES))
