"""health: profiles, checks, assignments, alerts, and per-device state

docs/modules/endpoint-health-alerting/spec.md §2.1-§2.8, §2.11.

Nine tables. Only THREE of them retain rows over time (§0.12) -- `health_alert`,
`device_health_check_state`, `health_flap_state` -- and the boundary that
follows from it is the whole design: *"what was this device's CPU last
Tuesday?" must be unanswerable from this schema.* That is why
`health_alert.observed_value` is `varchar(64)` and not a numeric column, and why
there is no `health_ingest_stats` table (§0.8's amendment: the discard count is
returned to the caller and logged, never stored).

THREE THINGS HERE ARE NOT ORDINARY DDL AND EACH CLOSES A SPECIFIC FAILURE:

1. `uq_health_alert_open_key` is a PARTIAL unique index. Without the predicate a
   key that has ever alarmed can never alarm again; without the index an alert
   re-opens every tick. Both halves are tested.

2. The three `health_profile_assignment` indexes are partial, one per resolution
   level, because NULLs do not collide in a Postgres unique constraint --
   `UNIQUE (org_id, device_id, device_group_id)` does not prevent two
   device-level rows for one device.

3. The 32-check cap is a STATEMENT-level trigger with a transition table,
   because a CHECK cannot count siblings. It is NOT statement-level to catch
   multi-row inserts: that claim was tested against this schema and is false on
   PostgreSQL 16 -- a PL/pgSQL trigger function increments the command counter
   before each of its own statements, so a `FOR EACH ROW` trigger counting
   siblings already sees the rows the same statement inserted. The reason is
   cost: one count per statement instead of one per row, which matters on the
   32-row `replace_checks` path. Transition tables cannot be shared across
   trigger events, so INSERT and UPDATE get one trigger each over one function;
   the UPDATE one exists because clearing `retired_at` on twenty rows is
   otherwise an unpoliced route back over the cap.

Revision ID: 0042
Revises: 0041
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"
_TABLES = [
    "health_profile",
    "health_check",
    "health_profile_assignment",
    "health_alert",
    "health_alert_action",
    "device_health_check_state",
    "health_flap_state",
    "health_org_settings",
    "device_health_policy_ack",
]

# §4.1's closed registry, restated in SQL because SQL cannot consult
# `app/modules/health/registry.py`. Hardcoded rather than imported: a migration
# that imports application code changes meaning when that code changes, which
# is the opposite of forward-only. `tests/test_health_check_registry.py`
# compares this list against the Python registry in BOTH directions.
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
)
_COMPARATORS = ("gt", "gte", "lt", "lte", "eq", "neq", "is")
_OS_FAMILIES = ("windows", "linux", "macos")


def _in_list(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "health_profile",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tick_interval_s", sa.Integer(), server_default="300", nullable=False),
        sa.Column("posture_interval_s", sa.Integer(), server_default="3600", nullable=False),
        sa.Column("summary_interval_s", sa.Integer(), server_default="1800", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        # Server-owned (§2.1). No API accepts it; a client-supplied version
        # would let a caller silence a fleet by claiming convergence.
        sa.Column("policy_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_health_profile_org_name"),
        # §0.11: the three numbers that make §0.5's idle claim checkable.
        sa.CheckConstraint(
            "tick_interval_s BETWEEN 60 AND 3600", name="ck_health_profile_tick_interval"
        ),
        sa.CheckConstraint("posture_interval_s >= 900", name="ck_health_profile_posture_interval"),
        sa.CheckConstraint(
            "posture_interval_s >= tick_interval_s",
            name="ck_health_profile_posture_not_faster_than_tick",
        ),
        sa.CheckConstraint(
            "summary_interval_s BETWEEN 300 AND 86400", name="ck_health_profile_summary_interval"
        ),
    )

    op.create_table(
        "health_check",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("health_profile.id"),
            nullable=False,
        ),
        sa.Column("check_type", sa.String(40), nullable=False),
        sa.Column("selector", sa.String(200), server_default="", nullable=False),
        sa.Column("comparator", sa.String(4), nullable=False),
        sa.Column("threshold_num", sa.Numeric(24, 4), nullable=True),
        sa.Column("threshold_bool", sa.Boolean(), nullable=True),
        sa.Column("for_ticks", sa.Integer(), server_default="3", nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("os_family", sa.String(8), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        # Server-set, never accepted from a request (§2.2). A retired check is
        # history: excluded from resolution, from every push, and from the view.
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_in_list("check_type", _CHECK_TYPES), name="ck_health_check_type"),
        sa.CheckConstraint(_in_list("comparator", _COMPARATORS), name="ck_health_check_comparator"),
        sa.CheckConstraint(
            "os_family IS NULL OR " + _in_list("os_family", _OS_FAMILIES),
            name="ck_health_check_os_family",
        ),
        sa.CheckConstraint(
            "(threshold_num IS NULL) <> (threshold_bool IS NULL)",
            name="ck_health_check_one_threshold",
        ),
        sa.CheckConstraint("for_ticks BETWEEN 1 AND 60", name="ck_health_check_for_ticks"),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name="ck_health_check_severity"
        ),
    )
    # PARTIAL on `retired_at IS NULL` (§2.2's amendment) so a retired row does
    # not make re-adding the same check permanently impossible; NULLS NOT
    # DISTINCT because `os_family` NULL means "any OS" and NULLs do not collide
    # in Postgres, so without it "cpu > 95 on any OS" is storable twice.
    op.create_index(
        "uq_health_check_profile_type_selector_os",
        "health_check",
        ["profile_id", "check_type", "selector", "os_family"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_index("ix_health_check_profile_live", "health_check", ["profile_id", "retired_at"])

    op.create_table(
        "health_profile_assignment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("health_profile.id"),
            nullable=False,
        ),
        sa.Column(
            "device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=True
        ),
        sa.Column(
            "device_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("device_groups.id"),
            nullable=True,
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "NOT (device_id IS NOT NULL AND device_group_id IS NOT NULL)",
            name="ck_health_assignment_one_target",
        ),
    )
    # One partial unique index PER RESOLUTION LEVEL (§0.2). A single
    # `UNIQUE (org_id, device_id, device_group_id)` enforces none of these:
    # `(org, dev, NULL)` does not collide with itself.
    op.create_index(
        "uq_health_assignment_device",
        "health_profile_assignment",
        ["org_id", "device_id"],
        unique=True,
        postgresql_where=sa.text("device_id IS NOT NULL"),
    )
    op.create_index(
        "uq_health_assignment_group",
        "health_profile_assignment",
        ["org_id", "device_group_id"],
        unique=True,
        postgresql_where=sa.text("device_group_id IS NOT NULL"),
    )
    op.create_index(
        "uq_health_assignment_org",
        "health_profile_assignment",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("device_id IS NULL AND device_group_id IS NULL"),
    )
    op.create_index(
        "ix_health_assignment_profile", "health_profile_assignment", ["org_id", "profile_id"]
    )

    op.create_table(
        "health_alert",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False
        ),
        # ON DELETE RESTRICT (§2.4's amendment): the DATABASE, not application
        # care, is what keeps an alert's `check_id` resolvable. Removing a check
        # from a profile is a retire, not a row delete.
        sa.Column(
            "check_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("health_check.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("health_profile.id"),
            nullable=False,
        ),
        sa.Column("selector", sa.String(200), server_default="", nullable=False),
        sa.Column("check_type", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("state", sa.String(14), server_default="active", nullable=False),
        # A STRING, deliberately: "97.4", "stopped" and "false" must all fit
        # without this table acquiring a numeric column someone will later
        # aggregate over time (§0.12).
        sa.Column("observed_value", sa.String(64), server_default="", nullable=False),
        sa.Column("threshold_display", sa.String(64), server_default="", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_policy_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("flapping", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("flap_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "acknowledged_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "muted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_reason", sa.String(24), nullable=True),
        sa.Column(
            "cleared_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column(
            "ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ticket.id"), nullable=True
        ),
        sa.Column(
            "previous_alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("health_alert.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('active', 'acknowledged', 'muted', 'cleared')", name="ck_health_alert_state"
        ),
        sa.CheckConstraint(
            "cleared_reason IS NULL OR cleared_reason IN ('recovery', 'summary_ok', "
            "'unavailable_timeout', 'policy_removed', 'device_inactive', 'manual')",
            name="ck_health_alert_cleared_reason",
        ),
        sa.CheckConstraint(
            "(state = 'cleared') = (cleared_at IS NOT NULL)",
            name="ck_health_alert_cleared_at_matches_state",
        ),
        sa.CheckConstraint(
            "(state = 'muted') = (muted_until IS NOT NULL)",
            name="ck_health_alert_muted_until_matches_state",
        ),
        sa.CheckConstraint(
            "muted_until IS NULL OR muted_until <= opened_at + interval '30 days'",
            name="ck_health_alert_mute_bounded",
        ),
    )
    # THE SINGLE MOST IMPORTANT LINE IN THIS SCHEMA (§0.8). At most one
    # non-cleared alert per key, enforced by Postgres rather than by care.
    op.create_index(
        "uq_health_alert_open_key",
        "health_alert",
        ["org_id", "device_id", "check_id", "selector"],
        unique=True,
        postgresql_where=sa.text("state <> 'cleared'"),
    )
    op.create_index("ix_health_alert_console", "health_alert", ["org_id", "state", "severity"])
    op.create_index("ix_health_alert_device", "health_alert", ["org_id", "device_id"])
    op.create_index(
        "ix_health_alert_open_opened_at",
        "health_alert",
        ["org_id", "opened_at"],
        postgresql_where=sa.text("state <> 'cleared'"),
    )

    op.create_table(
        "health_alert_action",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("health_alert.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("state", sa.String(20), server_default="pending", nullable=False),
        sa.Column(
            "approved_action_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approved_action.id"),
            nullable=True,
        ),
        # The KERNEL `jobs.id`. What actually ran is audited in `jobs` +
        # `audit_events`; it is not duplicated here.
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ticket.id"), nullable=True
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # §0.10's idempotency guarantee: one ticket, one remediation dispatch,
        # one notify, per alert, forever.
        sa.UniqueConstraint("alert_id", "kind", name="uq_health_alert_action_alert_kind"),
        sa.CheckConstraint(
            "kind IN ('remediate', 'open_ticket', 'notify')", name="ck_health_alert_action_kind"
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'awaiting_approval', 'dispatched', 'done', 'skipped', "
            "'blocked_rate_limit', 'blocked_flapping', 'expired', 'dead')",
            name="ck_health_alert_action_state",
        ),
    )
    op.create_index(
        "ix_health_alert_action_pending",
        "health_alert_action",
        ["org_id", "state", "next_attempt_at"],
    )

    op.create_table(
        "device_health_check_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False
        ),
        sa.Column(
            "check_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("health_check.id"),
            nullable=False,
        ),
        sa.Column("selector", sa.String(200), server_default="", nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("last_value", sa.String(64), server_default="", nullable=False),
        sa.Column("policy_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # UPSERT ONLY. No history row is ever inserted for the same key (§0.12).
        sa.UniqueConstraint(
            "org_id", "device_id", "check_id", "selector", name="uq_device_health_check_state_key"
        ),
        sa.CheckConstraint(
            "state IN ('ok', 'breaching', 'unavailable')",
            name="ck_device_health_check_state_state",
        ),
    )

    op.create_table(
        "health_flap_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False
        ),
        sa.Column(
            "check_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("health_check.id"),
            nullable=False,
        ),
        sa.Column("selector", sa.String(200), server_default="", nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("suppressed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "device_id", "check_id", "selector", name="uq_health_flap_state_key"
        ),
    )

    op.create_table(
        "health_org_settings",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            primary_key=True,
        ),
        sa.Column("flap_window_s", sa.Integer(), server_default="3600", nullable=False),
        sa.Column("flap_threshold", sa.Integer(), server_default="4", nullable=False),
        sa.Column("flap_cooldown_s", sa.Integer(), server_default="3600", nullable=False),
        sa.Column(
            "unavailable_clear_after_s", sa.Integer(), server_default="86400", nullable=False
        ),
        sa.Column("alert_retention_days", sa.Integer(), server_default="400", nullable=False),
        sa.Column("escalate_after_s", sa.Integer(), server_default="3600", nullable=False),
        sa.Column("offline_after_s", sa.Integer(), server_default="900", nullable=False),
        sa.Column("approval_ttl_s", sa.Integer(), server_default="86400", nullable=False),
        sa.Column(
            "default_alert_queue_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ticket_queue.id"),
            nullable=True,
        ),
        sa.Column(
            "default_ticket_category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ticket_category.id"),
            nullable=True,
        ),
        sa.Column("monitoring_deep_link_template", sa.String(500), nullable=True),
        sa.Column(
            "auto_resolve_alert_tickets", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "flap_window_s BETWEEN 300 AND 86400", name="ck_health_org_settings_flap_window"
        ),
        sa.CheckConstraint(
            "flap_threshold BETWEEN 2 AND 50", name="ck_health_org_settings_flap_threshold"
        ),
        sa.CheckConstraint(
            "flap_cooldown_s BETWEEN 300 AND 86400", name="ck_health_org_settings_flap_cooldown"
        ),
        sa.CheckConstraint(
            "unavailable_clear_after_s BETWEEN 3600 AND 2592000",
            name="ck_health_org_settings_unavailable_clear",
        ),
        sa.CheckConstraint(
            "alert_retention_days BETWEEN 90 AND 3650",
            name="ck_health_org_settings_alert_retention",
        ),
        sa.CheckConstraint(
            "escalate_after_s BETWEEN 300 AND 604800",
            name="ck_health_org_settings_escalate_after",
        ),
        sa.CheckConstraint(
            "offline_after_s BETWEEN 300 AND 86400", name="ck_health_org_settings_offline_after"
        ),
        sa.CheckConstraint(
            "approval_ttl_s BETWEEN 300 AND 604800", name="ck_health_org_settings_approval_ttl"
        ),
    )

    op.create_table(
        "device_health_policy_ack",
        # PK is `device_id`: one row per device, UPSERTed, never a history
        # (§2.11). A row per acknowledgement would be a `policy_version` time
        # series keyed by device, i.e. §0.12's boundary conceded for bookkeeping.
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id"),
            primary_key=True,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("health_profile.id"),
            nullable=True,
        ),
        sa.Column("policy_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "device_id", name="uq_device_health_policy_ack_org_device"
        ),
    )

    # The 32-check cap (§0.11, §2.2's amendment). See the module docstring for
    # why this is a statement trigger with a transition table and why there are
    # two of them.
    op.execute(
        """
        CREATE FUNCTION health_check_cap() RETURNS trigger AS $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM (SELECT DISTINCT profile_id FROM new_rows) t
            WHERE (SELECT count(*) FROM health_check c
                   WHERE c.profile_id = t.profile_id AND c.retired_at IS NULL) > 32
          ) THEN
            RAISE EXCEPTION
              'a health profile may hold at most 32 live checks (spec 0.11)';
          END IF;
          RETURN NULL;
        END $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_health_check_cap_insert AFTER INSERT ON health_check "
        "REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT "
        "EXECUTE FUNCTION health_check_cap()"
    )
    op.execute(
        "CREATE TRIGGER trg_health_check_cap_update AFTER UPDATE ON health_check "
        "REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT "
        "EXECUTE FUNCTION health_check_cap()"
    )

    # Standard tenant_isolation pattern (0002_rls.py, 0033_sla.py, 0041). WITH
    # CHECK is stated explicitly. It is NOT filling a gap -- for an ALL-command
    # policy PostgreSQL reuses the USING expression as WITH CHECK whenever WITH
    # CHECK is omitted, so a USING-only policy here would reject a cross-org
    # INSERT identically. The explicit clause matters the day a future edit
    # gives this policy a different read predicate than write predicate.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_health_check_cap_update ON health_check")
    op.execute("DROP TRIGGER IF EXISTS trg_health_check_cap_insert ON health_check")
    op.execute("DROP FUNCTION IF EXISTS health_check_cap()")
    op.drop_table("device_health_policy_ack")
    op.drop_table("health_org_settings")
    op.drop_table("health_flap_state")
    op.drop_table("device_health_check_state")
    op.drop_index("ix_health_alert_action_pending", table_name="health_alert_action")
    op.drop_table("health_alert_action")
    op.drop_index("ix_health_alert_open_opened_at", table_name="health_alert")
    op.drop_index("ix_health_alert_device", table_name="health_alert")
    op.drop_index("ix_health_alert_console", table_name="health_alert")
    op.drop_index("uq_health_alert_open_key", table_name="health_alert")
    op.drop_table("health_alert")
    op.drop_index("ix_health_assignment_profile", table_name="health_profile_assignment")
    op.drop_index("uq_health_assignment_org", table_name="health_profile_assignment")
    op.drop_index("uq_health_assignment_group", table_name="health_profile_assignment")
    op.drop_index("uq_health_assignment_device", table_name="health_profile_assignment")
    op.drop_table("health_profile_assignment")
    op.drop_index("ix_health_check_profile_live", table_name="health_check")
    op.drop_index("uq_health_check_profile_type_selector_os", table_name="health_check")
    op.drop_table("health_check")
    op.drop_table("health_profile")
