"""`backup-coverage` -- devices x backup (spec §5.3).

`requires = {devices.read, backup.read}`. `docs/06-roadmap.md`'s commercialization
gate requires "backups with **proven restores**, not just schedules", and
`backup_points.restore_tested_at` is a column nothing currently surfaces in
aggregate. This report is the evidence for that gate, which is why
`last_restore_tested_at` is a first-class column here and not an afterthought.

**THE POLICY RESOLUTION IS THE BACKUP MODULE'S OWN, REPRODUCED (§1.3).**
`backup.policy.resolve_backup_policy` resolves: **the device's group policy ->
the org-default policy (`device_group_id IS NULL`) -> `None`, meaning nothing is
scheduled for this device**, and it considers **only `enabled` policies at either
scope**. `_effective_repository` below is exactly that, and the `enabled` filter
is the load-bearing half: a device whose only policy is switched off has nothing
scheduled, and reporting it as covered would be the same lie as reporting a
device with no policy at all as covered.

**ONE DELIBERATE DIVERGENCE, AND IT IS A ROBUSTNESS ONE.** `backup_policies` has
**no unique constraint on `(org_id, device_group_id)`** -- verified against
migration `0014_backup.py`, which creates the table with none -- so an org CAN
hold two policies for one group. `resolve_backup_policy` uses
`.scalar_one_or_none()` at the group scope and would RAISE on that data; a report
that raised would take the whole page down for every device in the org. This
resolves with `ORDER BY id LIMIT 1`, the same rule the module already applies at
the org-default scope for the same reason (a NULL never collides in a Postgres
unique constraint), so the answer is deterministic rather than absent.

**THE HONESTY REQUIREMENT (§2, §5.3, §12.13): A DEVICE WITH NO POLICY IS NEVER
`covered`.** `coverage` is a CASE whose first branch is `unprotected`, so no
later branch can reach a device with no enabled policy however good its point
history looks. Every join to a backup table is a LEFT OUTER JOIN: making the
policy join inner would delete the device from the report entirely, and
`unprotected` would read 0 while `covered` stayed unchanged -- an observably
different number, which is what `tests/test_reporting_backup_coverage.py`
asserts.

**EXCLUDED ON PURPOSE (§5.3, §0.8): `backup_repositories.encryption_key_ref` and
`storage_ref`.** A repository's storage target and key reference are
infrastructure identifiers, not coverage facts. They are on `_NEVER_A_COLUMN` in
`registry.py`, so §12.3's test enforces the exclusion rather than a reviewer
remembering it.

**EVERY STATEMENT CARRIES AN EXPLICIT `Device.org_id == org_id` (§6.4)**, in
addition to RLS, and so does every subquery over another module's table.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, Select, and_, case, func, literal, or_, select

from app.core.i18n import N_
from app.models import (
    BackupPoint,
    BackupPointCopy,
    BackupPolicy,
    BackupRepository,
    BackupRun,
    Device,
)
from app.modules.reporting.registry import ReportDefinition
from app.modules.reporting.service import DEFAULT_MAX_LIMIT

KEY = "backup-coverage"

# `app/modules/backup/reporting.py`: "A backup point only ever represents a
# RESTORABLE snapshot when its state is `complete`". RESTATED rather than
# imported (§1.3), with a drift alarm in
# `tests/test_reporting_backup_coverage.py`.
_RESTORABLE_POINT_STATE = "complete"

# `BackupRun.state`: pending | running | success | failed | missed. The last
# two say the schedule is not working.
_FAILED_RUN_STATES = ("failed", "missed")

OFFSITE_VALUES: tuple[str, ...] = ("off", "current", "behind")

COVERAGE_VALUES: tuple[str, ...] = ("covered", "stale", "failing", "unprotected")


class BackupCoverageParams(BaseModel):
    """Query parameters. `extra="forbid"`, so an unknown query parameter is a
    422 rather than a filter silently ignored (§6.5)."""

    model_config = ConfigDict(extra="forbid")

    os_family: str | None = Field(
        default=None, description=N_("Restrict to one OS family, e.g. 'windows'.")
    )
    device_group_id: UUID | None = Field(
        default=None, description=N_("Restrict to devices in one device group.")
    )
    stale_after_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description=N_(
            "A device whose most recent restorable backup point is older than this is "
            "reported stale rather than covered. A device with no restorable point at "
            "all is always stale, never covered."
        ),
    )
    limit: int = Field(
        default=50, ge=1, le=DEFAULT_MAX_LIMIT, description=N_("Maximum rows to return.")
    )
    offset: int = Field(default=0, ge=0, description=N_("Rows to skip before the first returned."))


class BackupCoverageRow(BaseModel):
    """One device."""

    device_id: UUID = Field(description="The device this row is about.")
    hostname: str = Field(description="The device's hostname as last reported by its agent.")
    policy_present: bool = Field(
        description=(
            "True when a backup policy row applies to this device at group or org scope, "
            "whether or not it is enabled. False means nobody has ever configured one."
        )
    )
    policy_enabled: bool = Field(
        description=(
            "True when the policy that actually applies is enabled. Policy present and "
            "not enabled means somebody switched backups off for this device, which is "
            "reported as unprotected -- the same coverage as having no policy, with a "
            "different cause."
        )
    )
    repository_name: str | None = Field(
        description=(
            "The repository the effective policy writes to, or null when no enabled "
            "policy applies. The repository's storage target and encryption key "
            "reference are deliberately never reported."
        )
    )
    last_point_taken_at: datetime | None = Field(
        description=(
            "When the most recent backup point of ANY state was taken, or null if there "
            "has never been one. Coverage is judged on the most recent RESTORABLE point, "
            "which may be older than this."
        )
    )
    last_point_state: str | None = Field(
        description=(
            "complete | failed | pruned for that most recent point, or null. A 'failed' "
            "value here beside an older restorable point is the shape worth acting on."
        )
    )
    days_since_last_point: int | None = Field(
        description="Whole days since `last_point_taken_at`, or null if there is none."
    )
    last_verified_at: datetime | None = Field(
        description=(
            "The most recent integrity verification of any of this device's backup "
            "points, or null. Verified is weaker than restore-tested."
        )
    )
    last_restore_tested_at: datetime | None = Field(
        description=(
            "The most recent point at which a restore was actually exercised against any "
            "of this device's backup points, or null. THIS is the proven-restore "
            "evidence; a covered device with a null here has schedules, not proof."
        )
    )
    consistency_level: str | None = Field(
        description=(
            "How the newest restorable point was taken: snapshot, live, application or "
            "partial; null for points from agents that do not report it."
        )
    )
    crash_consistent: bool | None = Field(
        description=(
            "False when the newest restorable point was read while files were in use "
            "(not crash-consistent); null when the agent did not report it."
        )
    )
    offsite: str = Field(
        description=(
            "off | current | behind. off means the repository keeps no off-site copy. "
            "current means a restorable point inside the window is copied off-site; "
            "behind means none is, and the device is then reported stale."
        )
    )
    last_offsite_at: datetime | None = Field(
        description="When a point of this device was last copied off-site, or null."
    )
    coverage: str = Field(
        description=(
            "covered | stale | failing | unprotected. unprotected means no enabled policy "
            "applies and is checked FIRST, so a device with no policy can never be "
            "reported as covered whatever its point history says. failing means the most "
            "recent run failed or was missed. stale means the most recent restorable "
            "point is older than the window, including a device that has never produced "
            "one, or that no point inside the window has reached the off-site copy."
        )
    )


def _group_policy(column: Any, org_id: UUID, *, enabled_only: bool) -> Any:
    """The device's group policy's `column`, or NULL.

    Correlated on `devices.device_group_id`, so an ungrouped device (NULL) never
    matches and falls through to the org default -- which is exactly what
    `resolve_backup_policy` does when `Device.device_group_id` is None.
    `ORDER BY id LIMIT 1` because the table has no unique constraint at this
    scope; see the module docstring.
    """
    stmt = select(column).where(
        BackupPolicy.org_id == org_id,
        BackupPolicy.device_group_id == Device.device_group_id,
    )
    if enabled_only:
        stmt = stmt.where(BackupPolicy.enabled.is_(True))
    return stmt.order_by(BackupPolicy.id).limit(1).scalar_subquery()


def _org_policy(column: Any, org_id: UUID, *, enabled_only: bool) -> Any:
    """The org-default policy's `column`, or NULL. `device_group_id IS NULL`."""
    stmt = select(column).where(
        BackupPolicy.org_id == org_id, BackupPolicy.device_group_id.is_(None)
    )
    if enabled_only:
        stmt = stmt.where(BackupPolicy.enabled.is_(True))
    return stmt.order_by(BackupPolicy.id).limit(1).scalar_subquery()


def _points(org_id: UUID) -> Any:
    """Per-device point aggregates. `verified_at` and `restore_tested_at` are
    MAXIMA over every point, not values read off the newest one: the question
    "has a restore ever been proven for this device" is not answered by the most
    recent point, which may have been taken an hour ago and never tested."""
    return (
        select(
            BackupPoint.device_id.label("device_id"),
            func.max(BackupPoint.taken_at)
            .filter(BackupPoint.state == literal(_RESTORABLE_POINT_STATE))
            .label("last_restorable_taken_at"),
            func.max(BackupPoint.verified_at).label("last_verified_at"),
            func.max(BackupPoint.restore_tested_at).label("last_restore_tested_at"),
        )
        .where(BackupPoint.org_id == org_id)
        .group_by(BackupPoint.device_id)
        .subquery()
    )


def _last_point(org_id: UUID) -> Any:
    """The newest point of ANY state per device, via `DISTINCT ON`. Ordered by
    `(taken_at DESC, id DESC)` so two points taken in the same instant still
    resolve to one deterministic row."""
    return (
        select(
            BackupPoint.device_id.label("device_id"),
            BackupPoint.taken_at.label("taken_at"),
            BackupPoint.state.label("state"),
        )
        .where(BackupPoint.org_id == org_id)
        .distinct(BackupPoint.device_id)
        .order_by(BackupPoint.device_id, BackupPoint.taken_at.desc(), BackupPoint.id.desc())
        .subquery()
    )


def _last_restorable(org_id: UUID) -> Any:
    """The newest restorable point per device, for its consistency."""
    return (
        select(
            BackupPoint.device_id.label("device_id"),
            BackupPoint.consistency_level.label("consistency_level"),
            BackupPoint.crash_consistent.label("crash_consistent"),
        )
        .where(
            BackupPoint.org_id == org_id,
            BackupPoint.state == literal(_RESTORABLE_POINT_STATE),
        )
        .distinct(BackupPoint.device_id)
        .order_by(BackupPoint.device_id, BackupPoint.taken_at.desc(), BackupPoint.id.desc())
        .subquery()
    )


def _offsite(org_id: UUID) -> Any:
    """Per-device off-site aggregates over COMPLETE copies: when a copy last
    finished, and the newest point that has one."""
    return (
        select(
            BackupPoint.device_id.label("device_id"),
            func.max(BackupPointCopy.copied_at).label("last_offsite_at"),
            func.max(BackupPoint.taken_at).label("last_offsite_point_at"),
        )
        .join(
            BackupPoint,
            and_(
                BackupPoint.id == BackupPointCopy.point_id,
                BackupPoint.org_id == BackupPointCopy.org_id,
            ),
        )
        .where(
            BackupPointCopy.org_id == org_id,
            BackupPoint.org_id == org_id,
            BackupPointCopy.state == literal("complete"),
        )
        .group_by(BackupPoint.device_id)
        .subquery()
    )


def _last_run(org_id: UUID) -> Any:
    """The newest run of any state per device."""
    return (
        select(
            BackupRun.device_id.label("device_id"),
            BackupRun.state.label("state"),
        )
        .where(BackupRun.org_id == org_id)
        .distinct(BackupRun.device_id)
        .order_by(BackupRun.device_id, BackupRun.started_at.desc(), BackupRun.id.desc())
        .subquery()
    )


def _base(*, org_id: UUID, params: BackupCoverageParams) -> Select[Any]:
    points = _points(org_id)
    last_point = _last_point(org_id)
    last_run = _last_run(org_id)
    offsite_agg = _offsite(org_id)
    restorable = _last_restorable(org_id)
    stale_before = datetime.now(UTC) - timedelta(days=params.stale_after_days)

    effective_repository_id = func.coalesce(
        _group_policy(BackupPolicy.repository_id, org_id, enabled_only=True),
        _org_policy(BackupPolicy.repository_id, org_id, enabled_only=True),
    )
    # `repository_id` is NOT NULL, so a non-null coalesce IS "an enabled policy
    # resolved" -- the module's `resolve_backup_policy(...) is not None`.
    policy_enabled = effective_repository_id.is_not(None)
    policy_present = or_(
        _group_policy(BackupPolicy.id, org_id, enabled_only=False).is_not(None),
        _org_policy(BackupPolicy.id, org_id, enabled_only=False).is_not(None),
    )

    offsite_current = and_(
        offsite_agg.c.last_offsite_point_at.is_not(None),
        offsite_agg.c.last_offsite_point_at >= stale_before,
    )
    offsite = case(
        (BackupRepository.offsite_target_id.is_(None), literal("off")),
        (offsite_current, literal("current")),
        else_=literal("behind"),
    )

    coverage = case(
        # UNPROTECTED FIRST, AND THE ORDER IS THE PROPERTY (§12.13). No later
        # branch can reach a device with no enabled policy.
        (~policy_enabled, literal("unprotected")),
        (last_run.c.state.in_(_FAILED_RUN_STATES), literal("failing")),
        (
            or_(
                points.c.last_restorable_taken_at.is_(None),
                points.c.last_restorable_taken_at < stale_before,
            ),
            literal("stale"),
        ),
        (
            and_(BackupRepository.offsite_target_id.is_not(None), ~offsite_current),
            literal("stale"),
        ),
        else_=literal("covered"),
    )

    stmt = (
        select(
            Device.id.label("device_id"),
            Device.hostname.label("hostname"),
            policy_present.label("policy_present"),
            policy_enabled.label("policy_enabled"),
            BackupRepository.name.label("repository_name"),
            last_point.c.taken_at.label("last_point_taken_at"),
            last_point.c.state.label("last_point_state"),
            func.cast(
                func.floor(
                    func.extract("epoch", func.now() - last_point.c.taken_at) / 86400.0
                ),
                Integer,
            ).label("days_since_last_point"),
            points.c.last_verified_at.label("last_verified_at"),
            points.c.last_restore_tested_at.label("last_restore_tested_at"),
            restorable.c.consistency_level.label("consistency_level"),
            restorable.c.crash_consistent.label("crash_consistent"),
            offsite.label("offsite"),
            offsite_agg.c.last_offsite_at.label("last_offsite_at"),
            coverage.label("coverage"),
        )
        .select_from(Device)
        # OUTER, all of them. An inner join anywhere here deletes exactly the
        # devices this report exists to surface.
        .outerjoin(points, points.c.device_id == Device.id)
        .outerjoin(last_point, last_point.c.device_id == Device.id)
        .outerjoin(last_run, last_run.c.device_id == Device.id)
        .outerjoin(offsite_agg, offsite_agg.c.device_id == Device.id)
        .outerjoin(restorable, restorable.c.device_id == Device.id)
        .outerjoin(
            BackupRepository,
            and_(
                BackupRepository.id == effective_repository_id,
                BackupRepository.org_id == org_id,
            ),
        )
        # §6.4: explicit, IN ADDITION to RLS.
        .where(Device.org_id == org_id)
    )
    if params.os_family is not None:
        stmt = stmt.where(Device.os_family == params.os_family)
    if params.device_group_id is not None:
        stmt = stmt.where(Device.device_group_id == params.device_group_id)
    # Stable and TOTAL (§6.3): hostnames are not unique in this schema.
    return stmt.order_by(Device.hostname, Device.id)


def build(*, org_id: UUID, params: BackupCoverageParams) -> Select[Any]:
    return _base(org_id=org_id, params=params)


def build_totals(*, org_id: UUID, params: BackupCoverageParams) -> Select[Any]:
    """One total per `coverage` value (§5.3), over the WHOLE match, built from
    the same `_base` statement the rows come from.

    The four buckets are exhaustive and mutually exclusive by construction -- a
    `CASE` returns exactly one value -- so `covered + stale + failing +
    unprotected == devices`, and `tests/test_reporting_backup_coverage.py`
    asserts that rather than trusting it.
    """
    matched = _base(org_id=org_id, params=params).subquery()
    return select(
        func.count().label("devices"),
        *[
            func.count().filter(matched.c.coverage == literal(value)).label(value)
            for value in COVERAGE_VALUES
        ],
        func.count()
        .filter(matched.c.last_restore_tested_at.is_(None))
        .label("devices_never_restore_tested"),
    ).select_from(matched)


DEFINITION = ReportDefinition(
    key=KEY,
    summary=N_("Every device with the backup policy that applies to it and how protected it is."),
    description=N_(
        "One row per device: whether a backup policy applies and whether it is switched "
        "on, which repository it writes to, when the last backup point was taken and in "
        "what state, when a point was last verified, when a restore was last actually "
        "exercised, and a single coverage verdict. A device with no enabled policy is "
        "reported unprotected and can never be reported covered, whatever point history "
        "it has -- absence of a schedule is absence of protection. last_restore_tested_at "
        "is the proven-restore evidence the roadmap's commercialization gate asks for: a "
        "covered device with a null there has schedules, not proof. Computed live; it is "
        "a statement about the record at meta.generated_at."
    ),
    params=BackupCoverageParams,
    row_model=BackupCoverageRow,
    # `device_groups` is DECLARED AND NOT READ -- §5.3 lists it, and
    # `verify_definition`'s check is `read ⊆ declared` precisely so a
    # conservative declaration is legal. It maps to `devices.read`, which this
    # report already requires, so it widens nothing.
    sources=frozenset(
        {
            "devices",
            "device_groups",
            "backup_policies",
            "backup_repositories",
            "backup_points",
            "backup_runs",
            "backup_point_copies",
        }
    ),
    build=build,
    build_totals=build_totals,
    max_limit=DEFAULT_MAX_LIMIT,
    export_columns=(
        "device_id",
        "hostname",
        "policy_present",
        "policy_enabled",
        "repository_name",
        "last_point_taken_at",
        "last_point_state",
        "days_since_last_point",
        "last_verified_at",
        "last_restore_tested_at",
        "consistency_level",
        "crash_consistent",
        "offsite",
        "last_offsite_at",
        "coverage",
    ),
    # Both are operator-supplied free text. Every other column is a UUID, a
    # timestamp, a bool, an int, or a fixed-set enum the server produced.
    export_free_text=frozenset({"hostname", "repository_name"}),
    export_row_cap=50_000,
    # Current-state sources: policies, the newest point and the newest run all
    # describe now.
    reproducible=True,
)
