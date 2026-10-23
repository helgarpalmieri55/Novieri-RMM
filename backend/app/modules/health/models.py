"""The nine health tables (`docs/modules/endpoint-health-alerting/spec.md`
§2.1-§2.8, §2.11).

EVERY COLUMN HERE MUST MATCH `migrations/versions/0042_health.py` EXACTLY in
type, length and nullability -- `test_schema_drift.py` compares
`Base.metadata` to the live schema. That test does NOT compare defaults, so any
Python-side default below is load-bearing on its own; and it DOES compare
indexes, which is why every partial unique index is declared in
`__table_args__` here as well as in the migration. Without the declaration a
future `alembic revision --autogenerate` proposes DROPPING all of them --
including `uq_health_alert_open_key`, which the entire alert lifecycle rests
on.

ONLY THREE OF THESE TABLES RETAIN ROWS OVER TIME (§0.12): `health_alert` (one
row per opened alert, an incident record), `device_health_check_state` (one row
per `(device, check, selector)`, UPSERTed) and `health_flap_state` (one row per
alert key). The rest are configuration, per-alert bookkeeping bounded by
`UNIQUE (alert_id, kind)`, or one row per device. The boundary expressed as a
property: *"what was this device's CPU last Tuesday?" must be unanswerable from
this schema.* Adding a column that stores a value beside a timestamp which is
never overwritten breaks that property, and it is the whole reason
`health_alert.observed_value` is a string.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk

__all__ = [
    "DeviceHealthCheckState",
    "DeviceHealthPolicyAck",
    "HealthAlert",
    "HealthAlertAction",
    "HealthBaselineSeed",
    "HealthCheck",
    "HealthFlapState",
    "HealthOrgSettings",
    "HealthProfile",
    "HealthProfileAssignment",
]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HealthProfile(Base):
    """An ordered set of checks plus the three intervals that bound §0.5's idle
    claim (spec §2.1).

    `policy_version` IS SERVER-OWNED AND NO API ACCEPTS IT. It increments on any
    write to the profile or to its checks: it is what §0.4's reconciliation
    sweep compares against `device_health_policy_ack`, and what §0.8's open
    predicate tolerates exactly one version of drift on. A client-supplied
    version would let a caller silence a fleet by claiming convergence.
    """

    __tablename__ = "health_profile"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_health_profile_org_name"),
        # §0.11. These three are CHECK constraints rather than documentation
        # because "a 5-second tick with 400 checks" dies quietly: nobody
        # re-measures the idle budget after a config change.
        CheckConstraint(
            "tick_interval_s BETWEEN 60 AND 3600", name="ck_health_profile_tick_interval"
        ),
        CheckConstraint(
            "posture_interval_s >= 900", name="ck_health_profile_posture_interval"
        ),
        CheckConstraint(
            "posture_interval_s >= tick_interval_s",
            name="ck_health_profile_posture_not_faster_than_tick",
        ),
        CheckConstraint(
            "summary_interval_s BETWEEN 300 AND 86400",
            name="ck_health_profile_summary_interval",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    tick_interval_s: Mapped[int] = mapped_column(Integer, server_default="300", default=300)
    posture_interval_s: Mapped[int] = mapped_column(
        Integer, server_default="3600", default=3600
    )
    summary_interval_s: Mapped[int] = mapped_column(
        Integer, server_default="1800", default=1800
    )
    active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    policy_version: Mapped[int] = mapped_column(Integer, server_default="1", default=1)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class HealthCheck(Base):
    """One threshold on one profile (spec §2.2).

    THE UNIQUE INDEX IS PARTIAL AND `NULLS NOT DISTINCT`, and both halves are
    load-bearing. Partial on `retired_at IS NULL` because §2.4's amendment
    keeps a retired row forever; a non-partial index would make re-adding a
    check an operator once removed permanently impossible, which is the failure
    the retire mechanism exists to avoid rather than cause. `NULLS NOT
    DISTINCT` because `os_family` NULL means "any OS" and NULLs do not collide
    in Postgres -- without it "cpu > 95 on any OS" is storable twice in one
    profile and pushed twice to every device.

    `retired_at` is SERVER-SET, never accepted from a request. A retired check
    is excluded from resolution (§3), from every policy push (§5.2), and from
    the effective-profile view (§13).

    THE THREE ACTION-DECLARATION COLUMNS (§0.10, migration 0044). "A check may
    declare up to one of each action kind" is enforced by there being exactly
    one slot per kind: a second `notify` on one check is UNREPRESENTABLE, not
    rejected -- no `UNIQUE (check_id, kind)` to get right and no partial-index
    NULL trap. `declares_notify` false is §7.3's silent-by-design check, the
    only shape in which an alert is written, audited and counted but never
    routed.

    `remediate` IS DECLARED BY NAMING AN APPROVED ACTION, not by a boolean, and
    the asymmetry is the point. `health_alert_action.approved_action_id` is
    what §7.1's dispatcher resolves and `actions.py::_execute_remediate` marks
    a row `skipped` without it, so a boolean would mint rows that can only ever
    be skipped and would leave "which approved action answers this check?"
    unanswered. NULL by default: remediation is off for every row that exists
    and every row created, until a human names an action (invariant #1).
    """

    __tablename__ = "health_check"
    __table_args__ = (
        Index(
            "uq_health_check_profile_type_selector_os",
            "profile_id",
            "check_type",
            "selector",
            "os_family",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
        Index("ix_health_check_profile_live", "profile_id", "retired_at"),
        # The closed registry restated in SQL (§4.1). SQL cannot consult
        # `app/modules/health/registry.py`, so these three lists are compared
        # against it BOTH WAYS by tests/test_health_check_registry.py.
        CheckConstraint(
            "check_type IN ('cpu.busy_pct', 'memory.used_pct', 'disk.free_pct', "
            "'disk.free_bytes', 'service.state', 'process.present', 'disk.smart_ok', "
            "'posture.disk_encryption', 'posture.antivirus_healthy', "
            "'posture.firewall_enabled', 'eventlog.disk_error', "
            "'eventlog.service_crash', 'eventlog.kernel_fault', 'agent.offline', "
            "'backup.success_age_h', 'backup.last_run_failed', 'backup.offsite_lag_h', "
            "'backup.restore_test_age_d', 'script.policy_failed')",
            name="ck_health_check_type",
        ),
        CheckConstraint(
            "comparator IN ('gt', 'gte', 'lt', 'lte', 'eq', 'neq', 'is')",
            name="ck_health_check_comparator",
        ),
        CheckConstraint(
            "os_family IS NULL OR os_family IN ('windows', 'linux', 'macos')",
            name="ck_health_check_os_family",
        ),
        # Exactly one threshold column. WHICH one is right for the check type is
        # a registry question SQL cannot ask -- `registry.threshold_column_for`
        # plus Task 5's validation is the only thing standing between a boolean
        # check and a numeric threshold.
        CheckConstraint(
            "(threshold_num IS NULL) <> (threshold_bool IS NULL)",
            name="ck_health_check_one_threshold",
        ),
        CheckConstraint("for_ticks BETWEEN 1 AND 60", name="ck_health_check_for_ticks"),
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name="ck_health_check_severity"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("health_profile.id"))
    check_type: Mapped[str] = mapped_column(String(40))
    selector: Mapped[str] = mapped_column(String(200), server_default="", default="")
    comparator: Mapped[str] = mapped_column(String(4))
    # 24 digits with 4 decimals: `disk.free_bytes` on a petabyte array must fit
    # without becoming a float, because a float threshold compares differently
    # on the agent than it reads in the console.
    threshold_num: Mapped[float | None] = mapped_column(Numeric(24, 4), default=None)
    threshold_bool: Mapped[bool | None] = mapped_column(Boolean, default=None)
    for_ticks: Mapped[int] = mapped_column(Integer, server_default="3", default=3)
    severity: Mapped[str] = mapped_column(String(10))
    os_family: Mapped[str | None] = mapped_column(String(8), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    # §0.10's declaration, one slot per kind (migration 0044).
    declares_notify: Mapped[bool] = mapped_column(
        Boolean, server_default=true(), default=True
    )
    declares_open_ticket: Mapped[bool] = mapped_column(
        Boolean, server_default=true(), default=True
    )
    remediate_approved_action_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("approved_action.id", ondelete="RESTRICT"), default=None
    )
    retired_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class HealthProfileAssignment(Base):
    """Which profile applies to a device, a group, or the whole org (spec §2.3,
    §0.2).

    THREE PARTIAL UNIQUE INDEXES, ONE PER RESOLUTION LEVEL, because NULLs do
    not collide in a plain Postgres unique constraint: `UNIQUE (org_id,
    device_id, device_group_id)` does NOT prevent two device-level rows for one
    device, nor two org-default rows. It would leave "which of these two
    applies" to a tiebreak nobody chose.
    """

    __tablename__ = "health_profile_assignment"
    __table_args__ = (
        client_org_fk("health_profile_assignment"),
        Index(
            "uq_health_assignment_device",
            "org_id",
            "device_id",
            unique=True,
            postgresql_where=text("device_id IS NOT NULL"),
        ),
        Index(
            "uq_health_assignment_group",
            "org_id",
            "device_group_id",
            unique=True,
            postgresql_where=text("device_group_id IS NOT NULL"),
        ),
        Index(
            "uq_health_assignment_org",
            "org_id",
            unique=True,
            postgresql_where=text("device_id IS NULL AND device_group_id IS NULL"),
        ),
        Index("ix_health_assignment_profile", "org_id", "profile_id"),
        CheckConstraint(
            "NOT (device_id IS NOT NULL AND device_group_id IS NOT NULL)",
            name="ck_health_assignment_one_target",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client (the org-wide
    # assignment). TWO PARENTS, one trigger each (migration 0231):
    # its_client_from_parent copies the device's client, its_client_from_group
    # the group's. The value is read back after the INSERT.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("health_profile.id"))
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), default=None)
    device_group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("device_groups.id"), default=None
    )
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class HealthAlert(Base):
    """One opened alert -- an INCIDENT RECORD, not a sample (spec §2.4).

    `uq_health_alert_open_key` is the single most important line in this
    schema. At most one non-cleared alert per `(org_id, device_id, check_id,
    selector)`, enforced by Postgres: a duplicate breach report cannot create a
    second alert, it can only bump `last_seen_at` and `occurrence_count`. The
    `WHERE state <> 'cleared'` predicate is equally load-bearing in the other
    direction -- without it a key that has ever alarmed could never alarm again.

    `observed_value` IS A STRING, DELIBERATELY. It must hold "97.4" and
    "stopped" and "false" without this table acquiring a numeric column that
    someone will later be tempted to aggregate over time (§0.12).

    `check_id` IS NOT NULL AND ITS FK IS `ON DELETE RESTRICT` (§2.4's
    amendment). It is part of the dedup key above, and NULLs do not collide, so
    a nullable dedup component would reintroduce the exact problem §0.2's
    partial indexes exist to prevent -- on the one index the whole lifecycle
    rests on. Removing a check from a profile is therefore a RETIRE, not a row
    delete; RESTRICT is the database backstop under that, so "deleting a check
    leaves existing alerts intact" survives a direct SQL fixup.
    """

    __tablename__ = "health_alert"
    __table_args__ = (
        Index(
            "uq_health_alert_open_key",
            "org_id",
            "device_id",
            "check_id",
            "selector",
            unique=True,
            postgresql_where=text("state <> 'cleared'"),
        ),
        Index("ix_health_alert_console", "org_id", "state", "severity"),
        Index("ix_health_alert_device", "org_id", "device_id"),
        Index(
            "ix_health_alert_open_opened_at",
            "org_id",
            "opened_at",
            postgresql_where=text("state <> 'cleared'"),
        ),
        CheckConstraint(
            "state IN ('active', 'acknowledged', 'muted', 'cleared')",
            name="ck_health_alert_state",
        ),
        CheckConstraint(
            "cleared_reason IS NULL OR cleared_reason IN ('recovery', 'summary_ok', "
            "'unavailable_timeout', 'policy_removed', 'device_inactive', 'manual')",
            name="ck_health_alert_cleared_reason",
        ),
        CheckConstraint(
            "(state = 'cleared') = (cleared_at IS NOT NULL)",
            name="ck_health_alert_cleared_at_matches_state",
        ),
        CheckConstraint(
            "(state = 'muted') = (muted_until IS NOT NULL)",
            name="ck_health_alert_muted_until_matches_state",
        ),
        # A mute is time-boxed on purpose (§0.8): unbounded mutes are how a
        # fleet accumulates permanently silent alarms everyone believes are
        # still armed.
        CheckConstraint(
            "muted_until IS NULL OR muted_until <= opened_at + interval '30 days'",
            name="ck_health_alert_mute_bounded",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    check_id: Mapped[UUID] = mapped_column(
        ForeignKey("health_check.id", ondelete="RESTRICT")
    )
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("health_profile.id"))
    selector: Mapped[str] = mapped_column(String(200), server_default="", default="")
    check_type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(14), server_default="active", default="active")
    observed_value: Mapped[str] = mapped_column(String(64), server_default="", default="")
    threshold_display: Mapped[str] = mapped_column(String(64), server_default="", default="")
    opened_at: Mapped[datetime] = mapped_column(default=_utcnow)
    opened_policy_version: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    last_seen_at: Mapped[datetime] = mapped_column(default=_utcnow)
    occurrence_count: Mapped[int] = mapped_column(Integer, server_default="1", default=1)
    flapping: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    flap_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    # §2.4's amendment. Stamped once, never cleared: without it §12's escalation
    # sweep has no way to remember it already escalated, and a sweep that
    # re-fires on every pass turns one problem into an hourly mail.
    escalated_at: Mapped[datetime | None] = mapped_column(default=None)
    acknowledged_at: Mapped[datetime | None] = mapped_column(default=None)
    acknowledged_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    muted_until: Mapped[datetime | None] = mapped_column(default=None)
    muted_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    cleared_at: Mapped[datetime | None] = mapped_column(default=None)
    cleared_reason: Mapped[str | None] = mapped_column(String(24), default=None)
    cleared_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    ticket_id: Mapped[UUID | None] = mapped_column(ForeignKey("ticket.id"), default=None)
    previous_alert_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("health_alert.id"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class HealthAlertAction(Base):
    """One action an alert triggered, and its outcome (spec §2.5).

    `UNIQUE (alert_id, kind)` is §0.10's idempotency guarantee: one ticket, one
    remediation dispatch, one notify, per alert, FOREVER. An at-least-once
    evaluation -- a redelivered breach message, a re-run sweep, a summary that
    repairs a missed edge, a worker that dies mid-action and retries -- lands on
    this index rather than producing one ticket per tick.

    `job_id` is set only for `kind='remediate'` and is the KERNEL `jobs.id`: the
    audit trail of what actually ran lives in `jobs` + `audit_events` and is not
    duplicated here.
    """

    __tablename__ = "health_alert_action"
    __table_args__ = (
        UniqueConstraint("alert_id", "kind", name="uq_health_alert_action_alert_kind"),
        Index("ix_health_alert_action_pending", "org_id", "state", "next_attempt_at"),
        CheckConstraint(
            "kind IN ('remediate', 'open_ticket', 'notify')",
            name="ck_health_alert_action_kind",
        ),
        CheckConstraint(
            "state IN ('pending', 'awaiting_approval', 'dispatched', 'done', 'skipped', "
            "'blocked_rate_limit', 'blocked_flapping', 'expired', 'dead')",
            name="ck_health_alert_action_state",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the health_alert row named by alert_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    alert_id: Mapped[UUID] = mapped_column(ForeignKey("health_alert.id"))
    kind: Mapped[str] = mapped_column(String(12))
    state: Mapped[str] = mapped_column(String(20), server_default="pending", default="pending")
    approved_action_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("approved_action.id"), default=None
    )
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    ticket_id: Mapped[UUID | None] = mapped_column(ForeignKey("ticket.id"), default=None)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    approved_at: Mapped[datetime | None] = mapped_column(default=None)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class DeviceHealthCheckState(Base):
    """The current state of one check on one device (spec §2.6).

    UPSERT ONLY. No history row is ever inserted for the same key: `updated_at`
    moves and nothing accumulates, so cardinality is devices x 32 and CONSTANT
    IN TIME. This is §0.12 clause 2, and it is the table an innocent "just keep
    the last ten values" would turn into the monitoring product.
    """

    __tablename__ = "device_health_check_state"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "device_id",
            "check_id",
            "selector",
            name="uq_device_health_check_state_key",
        ),
        CheckConstraint(
            "state IN ('ok', 'breaching', 'unavailable')",
            name="ck_device_health_check_state_state",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    check_id: Mapped[UUID] = mapped_column(ForeignKey("health_check.id"))
    selector: Mapped[str] = mapped_column(String(200), server_default="", default="")
    state: Mapped[str] = mapped_column(String(12))
    last_value: Mapped[str] = mapped_column(String(64), server_default="", default="")
    policy_version: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    reported_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class HealthFlapState(Base):
    """The open-rate window for one alert key (spec §2.7, §0.9).

    One row per key, upserted. Suppression removes the ACTIONS -- the mail, the
    ticket, the remediation job -- never the evidence: the alert row is still
    written with `flapping = true`, because a design that suppresses the record
    turns "this device flapped 40 times overnight" into "it was fine".
    """

    __tablename__ = "health_flap_state"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "device_id", "check_id", "selector", name="uq_health_flap_state_key"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    check_id: Mapped[UUID] = mapped_column(ForeignKey("health_check.id"))
    selector: Mapped[str] = mapped_column(String(200), server_default="", default="")
    window_start: Mapped[datetime] = mapped_column(default=_utcnow)
    open_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    suppressed_until: Mapped[datetime | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class HealthOrgSettings(Base):
    """The org's health tunables (spec §2.8), one optional row per org.

    OPTIONAL IS THE POINT: `settings.get_settings` returns EFFECTIVE values and
    writes nothing, so a `health.read` request never becomes a write. Only the
    PUT creates the row. Every bound below is a CHECK rather than a Pydantic
    validator because the ranges are what make §0.9's suppression and §12's
    sweeps bounded, and a Python-only bound survives neither a direct SQL fixup
    nor a second caller.

    `alert_retention_days` carries an UPPER bound as well as §0.12's lower one
    of 90. An unbounded retention setting is how "retention" silently becomes
    "forever", which is the state §0.12 says this module must be able to prove
    it is not in.
    """

    __tablename__ = "health_org_settings"
    __table_args__ = (
        CheckConstraint(
            "flap_window_s BETWEEN 300 AND 86400", name="ck_health_org_settings_flap_window"
        ),
        CheckConstraint(
            "flap_threshold BETWEEN 2 AND 50", name="ck_health_org_settings_flap_threshold"
        ),
        CheckConstraint(
            "flap_cooldown_s BETWEEN 300 AND 86400",
            name="ck_health_org_settings_flap_cooldown",
        ),
        # 1 hour to 30 days. §0.8 clause 3 gives the default (24 h) and not the
        # range; below an hour the "removed sensor" clear races an agent that is
        # merely rebooting, and above a month the alert is retention, not signal.
        CheckConstraint(
            "unavailable_clear_after_s BETWEEN 3600 AND 2592000",
            name="ck_health_org_settings_unavailable_clear",
        ),
        CheckConstraint(
            "alert_retention_days BETWEEN 90 AND 3650",
            name="ck_health_org_settings_alert_retention",
        ),
        CheckConstraint(
            "escalate_after_s BETWEEN 300 AND 604800",
            name="ck_health_org_settings_escalate_after",
        ),
        CheckConstraint(
            "offline_after_s BETWEEN 300 AND 86400",
            name="ck_health_org_settings_offline_after",
        ),
        CheckConstraint(
            "approval_ttl_s BETWEEN 300 AND 604800",
            name="ck_health_org_settings_approval_ttl",
        ),
    )

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    flap_window_s: Mapped[int] = mapped_column(Integer, server_default="3600", default=3600)
    flap_threshold: Mapped[int] = mapped_column(Integer, server_default="4", default=4)
    flap_cooldown_s: Mapped[int] = mapped_column(Integer, server_default="3600", default=3600)
    unavailable_clear_after_s: Mapped[int] = mapped_column(
        Integer, server_default="86400", default=86400
    )
    alert_retention_days: Mapped[int] = mapped_column(
        Integer, server_default="400", default=400
    )
    escalate_after_s: Mapped[int] = mapped_column(Integer, server_default="3600", default=3600)
    offline_after_s: Mapped[int] = mapped_column(Integer, server_default="900", default=900)
    approval_ttl_s: Mapped[int] = mapped_column(Integer, server_default="86400", default=86400)
    default_alert_queue_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ticket_queue.id"), default=None
    )
    default_ticket_category_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ticket_category.id"), default=None
    )
    # A URL template with `{hostname}`. THE SERVER NEVER FETCHES IT (§0.12) --
    # it is rendered as a link in the browser, so it is not an SSRF surface and
    # does not touch `app/core/egress.py`.
    monitoring_deep_link_template: Mapped[str | None] = mapped_column(
        String(500), default=None
    )
    auto_resolve_alert_tickets: Mapped[bool] = mapped_column(
        Boolean, server_default=true(), default=True
    )
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class DeviceHealthPolicyAck(Base):
    """The last policy version a device acknowledged (spec §2.11).

    PK IS `device_id`; ONE ROW PER DEVICE, UPSERTed, never inserted twice. It is
    bounded by the fleet and constant in time -- the same shape and the same
    bound as `device_health_check_state`, which is why it does not breach
    §0.12. A row per acknowledgement would be a `policy_version` time series
    keyed by device.

    TWO WRITERS, BOTH IDEMPOTENT: `policy_push.record_policy_ack` (the
    `health.policy.apply` result handler -- promptness) and
    `policy_push.record_policy_ack_from_summary`, driven by `ingest_report` for
    every report carrying a `summary` line (correctness -- the summary is the
    channel that survives a lost result). A LOWER `policy_version` never
    overwrites a higher one FOR THE SAME `profile_id`; a different profile, a
    disarm, or a stored row whose profile could not be identified all yield to
    the newer statement. `policy_push._scoped_version` is that rule, and its
    docstring says why each of its three branches is there.

    THE SUMMARY WRITER IS NOT AN OPTIMISATION, IT IS THE ONLY RECOVERY PATH.
    §5.2's known limit turns on it by name: a policy sent but whose result was
    lost leaves no ack row, `run_policy_reconcile` reads that as "never armed,
    nothing to withdraw", and without a second writer the device evaluates a
    deleted profile forever while the reconcile pushes nothing forever.

    A MISSING ROW READS AS `policy_version = 0`. §0.8 clause 2 is then vacuously
    satisfied and §12 always re-pushes; both are the right way to fail, because
    a device that has never acknowledged a policy must not have its breaches
    discarded and must not be assumed converged.
    """

    __tablename__ = "device_health_policy_ack"
    __table_args__ = (
        # Alongside the PK, so every query in this module can carry its explicit
        # `org_id` predicate against an index (invariant #5).
        UniqueConstraint("org_id", "device_id", name="uq_device_health_policy_ack_org_device"),
    )

    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("health_profile.id"), default=None
    )
    policy_version: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    acknowledged_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class HealthBaselineSeed(Base):
    """Whether this org's default baseline profile was seeded (migration 0084).

    One row per org, written once: `seeded`, or `skipped` because the org
    already had profiles. Its existence is what stops the seed from running
    again, so deleting the baseline profile deletes it for good.
    """

    __tablename__ = "health_baseline_seed"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('seeded', 'skipped')", name="ck_health_baseline_seed_outcome"
        ),
    )

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(10))
    profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("health_profile.id", ondelete="SET NULL"), default=None
    )
    recorded_at: Mapped[datetime] = mapped_column(server_default=text("now()"), default=_utcnow)
