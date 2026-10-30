"""`patch-compliance` -- devices x patch (spec §5.2).

`requires = {devices.read, patch.read, jobs.read}`. "Are we patched" is the most-asked
compliance question, and answering it correctly requires joining APPROVAL STATE
(per ring, per group) to DEVICE STATE -- which is precisely why nobody
reconstructs it by hand correctly, and why it is here rather than in either
module (§0.12).

**THE APPROVAL PREDICATE IS THE PATCH MODULE'S OWN, REPRODUCED, NOT INVENTED.**
Reporting reads tables and not services (§1.3), so this file cannot call
`patch.policy.resolve_patch_policy` or `patch.approve._devices_in_ring`; it
reproduces what they compute, and the reproduction is stated here so a reviewer
can check it against those files line by line:

* `policy.resolve_patch_policy` resolves a device's ring as **the device's group
  policy -> the org-default policy (`device_group_id IS NULL`, lowest `id` when
  more than one exists, because a NULL never collides in a Postgres UNIQUE) ->
  the hardcoded `ResolvedPatchPolicy()` default, `"broad"`.** `_device_ring`
  below is that, as a `COALESCE` over a LEFT JOIN, a scalar sub-select and a
  literal, in that order. The column-wise `COALESCE` is equivalent to the
  row-wise resolution because every column it reads is `NOT NULL`: if a group
  policy row exists, every value comes from it.
* `approve.approve_patch` writes one `patch_approvals` row per `(patch, ring)`
  and `revoke_approval` sets `revoked = True` in place rather than deleting, so
  **"approved for this device" is: a `patch_approvals` row for this patch, for
  this device's ring, with `revoked = false`.** `_is_approved_for_device` is
  exactly that.

**WHY NOT `device_patch_status.state == "approved"`.** That column is the patch
module's own cache of the same fact and it is written only by the approve/revoke
paths, over the devices that were in the ring AT THAT MOMENT
(`approve._devices_in_ring`). A device moved into an approved ring afterwards, or
created afterwards, keeps `state = "missing"` while the patch is genuinely
approved for it. Reading the cache would UNDERSTATE the outstanding-approved
count for exactly the devices whose group membership changed -- so this report
recomputes from `patch_approvals`, which is the record, and counts every
outstanding state (`missing`, `approved`, `failed`) alike.

**THE HONESTY REQUIREMENT (§2, §5.2, §12.13): `scan_state = "never"` IS ITS OWN
BUCKET AND IS NEVER COMPLIANT.** Both joins onto `devices` -- the patch-status
aggregate and the scan aggregate -- are LEFT OUTER JOINs so a device that has
never been scanned still has a row, and `devices_with_zero_missing` counts only
devices whose `scan_state` is not `"never"`. An inner join would drop the device
from the denominator entirely; treating "no rows" as "nothing missing" would
count it as compliant. Both make the total read 2 instead of 1 on the seeded
fixture in `tests/test_reporting_patch_compliance.py`, which is an observably
different number rather than a test that the query ran.

**`last_scan_at` IS READ FROM `jobs`, AND `device_patch_status.detected_at` IS
THE WRONG ANSWER THAT WAS HERE BEFORE.** `_scans` below carries the full
reasoning; the short version is that `detected_at` is stamped when a status row
is INSERTED and no later scan rewrites it, so it dates a patch and not a scan,
and this report told an operator that a settled fleet was last scanned on the
day it was enrolled. Reading `jobs` is why `sources` names it and why `requires`
therefore includes `jobs.read` -- a widening of this report's gate that §5.2 did
not originally list, recorded in the spec rather than taken quietly. Every
seeded role holding `patch.read` (technician, auditor, read-only, admin) already
holds `jobs.read`, so no role that could run this report yesterday is locked out
of it today.

**A DIVERGENCE RECORDED RATHER THAN SMUGGLED: `failed_in_window` IS MEASURED ON
`updated_at`.** `apply.py:224` sets `state = "failed"` and `last_error` and
NOTHING else -- `applied_at` stays NULL on a failure, and `patch_runs` (which
does carry a timestamp) is not one of §5.2's sources. `device_patch_status.
updated_at` is therefore the only record of when a failure landed, and it moves
on ANY later change to the row. The field description says so; the alternative
was to omit a field §5.2 requires.

**EVERY STATEMENT CARRIES AN EXPLICIT `Device.org_id == org_id` (§6.4)**, in
ADDITION to RLS, and so does every subquery and every join condition over
another module's table. RLS is the guarantee; the predicate is the half a test
can pin on the admin engine, where RLS is not doing the rejecting.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, Select, and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import aliased

from app.core.i18n import N_
from app.models import (
    Device,
    DevicePatchStatus,
    Job,
    JobStatus,
    Patch,
    PatchApproval,
    PatchPolicy,
)
from app.modules.reporting.registry import ReportDefinition
from app.modules.reporting.service import DEFAULT_MAX_LIMIT

KEY = "patch-compliance"

# `app/modules/patch/reporting.py`'s NON_COMPLIANT_STATES: the states in which a
# device has an APPLICABLE patch it has not successfully applied. RESTATED
# rather than imported (§1.3), with a drift alarm --
# `tests/test_reporting_patch_compliance.py` asserts this equals the patch
# module's own frozenset, so the restatement is a pinned copy rather than a
# guess.
_OUTSTANDING_STATES: frozenset[str] = frozenset({"missing", "approved", "failed"})

# `app/modules/patch/ingest.py`'s AUTO_APPROVE_SEVERITIES -- the patch module's
# own definition of "a security patch", and the only one in this codebase.
# Pinned by the same drift alarm.
_SECURITY_SEVERITIES: frozenset[str] = frozenset({"critical", "important"})

# `policy.ResolvedPatchPolicy()`'s defaults, for a device whose org has no
# policy row at any scope. Pinned by the same drift alarm.
_DEFAULT_RING = "broad"
_DEFAULT_SCAN_INTERVAL_HOURS = 24


def _scan_criteria(org_id: UUID, device: Any) -> tuple[ColumnElement[bool], ...]:
    """The WHERE that identifies a completed patch scan of a device this org
    still has.

    RESTATED from `app/modules/patch/reporting.py::_succeeded_scan_criteria`
    (§1.3, like `_OUTSTANDING_STATES` above), and pinned by a drift alarm in
    `tests/test_reporting_patch_compliance.py` that compares the two COMPILED,
    binds included -- so a patch module that starts writing `patch.rescan`, or
    that stops requiring `succeeded`, turns this file red rather than making
    the console and this report disagree about when a machine was measured.
    Clause order is part of that comparison; keep it.

    Every clause earns its place. `Job.status == succeeded` because
    `JobEngine.complete` flips a job only after the patch module's ingest
    handler has returned without raising -- a failed or expired scan measured
    nothing and must date nothing. `device.org_id` alongside `Job.org_id`
    because the join is on `Job.target_id`, a bare UUID column with no foreign
    key: nothing else ties a job to the tenant whose device it names. That
    second predicate is §6.4 defence in depth here rather than the only
    barrier -- `_base` joins this subquery to a `Device` already filtered by
    org, so removing it changes no answer today -- and it is written anyway
    because §6.4 asks every subquery in this file to carry its own.
    """
    return (
        Job.org_id == org_id,
        device.org_id == org_id,
        Job.type == "patch.scan",
        Job.target_type == "device",
        Job.status == JobStatus.succeeded,
    )


def _scans(org_id: UUID) -> Any:
    """When each device last COMPLETED a patch scan, one row per device.

    **NOT `max(device_patch_status.detected_at)`, WHICH IS WHAT THIS COLUMN
    USED TO BE AND WHICH IS NOT A SCAN TIMESTAMP AT ALL.** `detected_at` is
    stamped when a status ROW IS FIRST CREATED (`patch/ingest.py` §3b) and no
    later scan rewrites it -- `models.py` declares it `server_default=func.
    now()` with no `onupdate`, and every update path in `ingest.py` touches
    `state`/`applied_at` and leaves it alone. So it dates the discovery of a
    PATCH, not the running of a SCAN, and on a device whose patch set has not
    changed since enrolment it never moves: a fleet scanned hourly for a month
    reported `last_scan_at` as its enrolment day, and `scan_state` as `stale`
    for machines that were being measured on schedule. The same false reading
    drove `patch_scan_sweep_org`'s cadence until cad191a; this is its twin in
    report form, and it is worse here, because a report is the artefact people
    take decisions from and hand to auditors.

    A SET-BASED SUBQUERY, NOT `patch.reporting.last_successful_scan_at` IN A
    LOOP. That helper answers for ONE device and this report answers for every
    device in the org; calling it per row would be an N+1 on a paged path that
    also has to compute `totals` over the whole match. One grouped aggregate,
    LEFT JOINed once in `_base`, is the same fact in the shape this statement
    needs -- and it is a shape only reporting wants, so it lives here rather
    than being added to the patch module (§1.3: reporting adds nothing to any
    module).

    Devices, joined: a scan naming a device this org no longer has dates
    nothing, exactly as `fleet_scan_coverage` has it.
    """
    scan_device = aliased(Device)
    return (
        select(
            Job.target_id.label("device_id"),
            func.max(Job.finished_at).label("last_scan_at"),
        )
        .select_from(Job)
        .join(scan_device, scan_device.id == Job.target_id)
        .where(*_scan_criteria(org_id, scan_device))
        .group_by(Job.target_id)
        .subquery()
    )


class PatchComplianceParams(BaseModel):
    """Query parameters. `extra="forbid"`, so an unknown query parameter is a
    422 rather than a filter silently ignored (§6.5)."""

    model_config = ConfigDict(extra="forbid")

    os_family: str | None = Field(
        default=None, description=N_("Restrict to one OS family, e.g. 'windows'.")
    )
    device_group_id: UUID | None = Field(
        default=None, description=N_("Restrict to devices in one device group.")
    )
    window_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description=N_(
            "How far back `applied_in_window` and `failed_in_window` look. Does not "
            "filter the device list: a device with no activity in the window still "
            "appears, with zero counts."
        ),
    )
    scan_stale_after_intervals: int = Field(
        default=2,
        ge=1,
        le=10,
        description=N_(
            "How many of the device's own policy scan intervals may elapse before its "
            "last scan is reported stale. The interval comes from the device's resolved "
            "patch policy, so an org that scans weekly is not judged against an org that "
            "scans daily."
        ),
    )
    limit: int = Field(
        default=50, ge=1, le=DEFAULT_MAX_LIMIT, description=N_("Maximum rows to return.")
    )
    offset: int = Field(default=0, ge=0, description=N_("Rows to skip before the first returned."))


class PatchComplianceRow(BaseModel):
    """One device."""

    device_id: UUID = Field(description="The device this row is about.")
    hostname: str = Field(description="The device's hostname as last reported by its agent.")
    os_family: str = Field(description="windows | linux | macos.")
    missing_approved: int = Field(
        description=(
            "Patches applicable to this device, approved for its ring, and not "
            "successfully applied (state missing, approved or failed). This is the "
            "number that is meant to be zero."
        )
    )
    missing_approved_security: int = Field(
        description=(
            "The subset of `missing_approved` whose severity is critical or important "
            "-- the patch module's own definition of a security patch "
            "(`ingest.AUTO_APPROVE_SEVERITIES`)."
        )
    )
    oldest_missing_detected_at: datetime | None = Field(
        description=(
            "When the longest-outstanding approved-and-unapplied patch was first "
            "detected on this device, or null when there are none. Exposure age, not "
            "patch age."
        )
    )
    applied_in_window: int = Field(
        description="Patches this device installed within the window (`applied_at`)."
    )
    failed_in_window: int = Field(
        description=(
            "Patches whose state is failed and whose row last changed within the window. "
            "Measured on `updated_at` because a failure sets no timestamp of its own; a "
            "later edit to the same row moves it back into the window."
        )
    )
    last_scan_at: datetime | None = Field(
        description=(
            "When this device last COMPLETED a patch scan -- the newest `finished_at` "
            "of a succeeded `patch.scan` job against it -- or null when it never has. "
            "Deliberately not `max(device_patch_status.detected_at)`, which dates a "
            "patch's first detection and never moves again."
        )
    )
    scan_state: str = Field(
        description=(
            "fresh | stale | never. `never` means NO patch scan has ever completed for "
            "this device -- it is its own bucket and is never counted as compliant, "
            "because absence of a scan is absence of a record, not evidence of health."
        )
    )


def _org_default(column: Any, org_id: UUID) -> Any:
    """The org-default patch policy's `column`, or NULL.

    `ORDER BY id LIMIT 1` rather than `scalar_one_or_none`, because
    `UNIQUE(org_id, device_group_id)` does NOT stop an org holding two rows with
    a NULL `device_group_id` -- NULLs never collide in a Postgres unique
    constraint. `policy.resolve_patch_policy` takes the lowest `id` for exactly
    this reason, and this takes the same one.
    """
    return (
        select(column)
        .where(PatchPolicy.org_id == org_id, PatchPolicy.device_group_id.is_(None))
        .order_by(PatchPolicy.id)
        .limit(1)
        .scalar_subquery()
    )


def _stats(org_id: UUID, params: PatchComplianceParams) -> Any:
    """Per-device patch counts, aggregated BEFORE the join to `devices`.

    Aggregating first rather than grouping the whole statement: a device joined
    row-per-status and then grouped would make every `devices` column a
    `GROUP BY` term, and the device count in `totals` would be a count of groups
    -- correct today and one added column away from silently becoming a count of
    patch rows.
    """
    group_policy = aliased(PatchPolicy)
    window_start = datetime.now(UTC) - timedelta(days=params.window_days)

    device_ring = func.coalesce(
        group_policy.ring_name,
        _org_default(PatchPolicy.ring_name, org_id),
        literal(_DEFAULT_RING),
    )
    # APPROVED FOR THIS MACHINE: by its own client's approval or a global one
    # (MSP clients plan 2026-10-26 Task 14). `device_patch_status.client_id`
    # is its device's (trigger, 0231). Explicit, because a report run under
    # scope all would otherwise count X's approval for Y's machine.
    approved = exists(
        select(PatchApproval.id).where(
            PatchApproval.org_id == org_id,
            PatchApproval.patch_id == DevicePatchStatus.patch_id,
            PatchApproval.ring_name == device_ring,
            PatchApproval.revoked.is_(False),
            or_(
                PatchApproval.client_id.is_(None),
                PatchApproval.client_id == DevicePatchStatus.client_id,
            ),
        )
    )
    outstanding = DevicePatchStatus.state.in_(sorted(_OUTSTANDING_STATES))
    outstanding_approved = and_(outstanding, approved)
    security = Patch.severity.in_(sorted(_SECURITY_SEVERITIES))

    return (
        select(
            DevicePatchStatus.device_id.label("device_id"),
            func.count().filter(outstanding_approved).label("missing_approved"),
            func.count()
            .filter(and_(outstanding_approved, security))
            .label("missing_approved_security"),
            func.min(DevicePatchStatus.detected_at)
            .filter(outstanding_approved)
            .label("oldest_missing_detected_at"),
            func.count()
            .filter(
                and_(
                    DevicePatchStatus.state == "installed",
                    DevicePatchStatus.applied_at.is_not(None),
                    DevicePatchStatus.applied_at >= window_start,
                )
            )
            .label("applied_in_window"),
            func.count()
            .filter(
                and_(
                    DevicePatchStatus.state == "failed",
                    DevicePatchStatus.updated_at >= window_start,
                )
            )
            .label("failed_in_window"),
            # NO `last_scan_at` HERE. It used to be
            # `max(DevicePatchStatus.detected_at)` and this is the wrong table
            # to look for it in -- see `_scans`, which reads the record of a
            # scan rather than the record of a patch.
        )
        .select_from(DevicePatchStatus)
        .join(
            Patch,
            and_(Patch.id == DevicePatchStatus.patch_id, Patch.org_id == org_id),
        )
        .join(
            Device,
            and_(Device.id == DevicePatchStatus.device_id, Device.org_id == org_id),
        )
        .outerjoin(
            group_policy,
            and_(
                group_policy.org_id == org_id,
                group_policy.device_group_id == Device.device_group_id,
            ),
        )
        # §6.4: explicit, IN ADDITION to RLS, on every table in the statement.
        .where(DevicePatchStatus.org_id == org_id)
        .group_by(DevicePatchStatus.device_id)
        .subquery()
    )


def _base(*, org_id: UUID, params: PatchComplianceParams) -> Select[Any]:
    stats = _stats(org_id, params)
    scans = _scans(org_id)
    scan_policy = aliased(PatchPolicy)

    scan_interval_hours = func.coalesce(
        scan_policy.scan_interval_hours,
        _org_default(PatchPolicy.scan_interval_hours, org_id),
        literal(_DEFAULT_SCAN_INTERVAL_HOURS),
    )
    fresh_since = func.now() - func.make_interval(
        0, 0, 0, 0, scan_interval_hours * literal(params.scan_stale_after_intervals)
    )
    scan_state = case(
        # NEVER FIRST. A device with no succeeded `patch.scan` job reaches here
        # through the LEFT OUTER JOIN with a NULL `last_scan_at`, and it is
        # `never` -- not `stale`, and above all not compliant (§2, §12.13).
        #
        # "NO SUCCEEDED SCAN", NOT "NO `device_patch_status` ROW", and the two
        # are different populations in both directions: a device can carry
        # status rows minted by an ingest whose job later failed, and a device
        # scanned clean can carry no rows at all. `never` now means what this
        # field's description has always claimed it means.
        (scans.c.last_scan_at.is_(None), literal("never")),
        (scans.c.last_scan_at >= fresh_since, literal("fresh")),
        else_=literal("stale"),
    )

    stmt = (
        select(
            Device.id.label("device_id"),
            Device.hostname.label("hostname"),
            Device.os_family.label("os_family"),
            func.coalesce(stats.c.missing_approved, 0).label("missing_approved"),
            func.coalesce(stats.c.missing_approved_security, 0).label(
                "missing_approved_security"
            ),
            stats.c.oldest_missing_detected_at.label("oldest_missing_detected_at"),
            func.coalesce(stats.c.applied_in_window, 0).label("applied_in_window"),
            func.coalesce(stats.c.failed_in_window, 0).label("failed_in_window"),
            scans.c.last_scan_at.label("last_scan_at"),
            scan_state.label("scan_state"),
        )
        .select_from(Device)
        # OUTER. See the module docstring: an inner join here drops exactly the
        # devices this report exists to surface.
        .outerjoin(stats, stats.c.device_id == Device.id)
        # OUTER for the same reason, and it is the join that makes the scan
        # subquery tenant-safe: `jobs.target_id` has no foreign key, so a job
        # naming a device of another org lands on no row here.
        .outerjoin(scans, scans.c.device_id == Device.id)
        .outerjoin(
            scan_policy,
            and_(
                scan_policy.org_id == org_id,
                scan_policy.device_group_id == Device.device_group_id,
            ),
        )
        .where(Device.org_id == org_id)
    )
    if params.os_family is not None:
        stmt = stmt.where(Device.os_family == params.os_family)
    if params.device_group_id is not None:
        stmt = stmt.where(Device.device_group_id == params.device_group_id)
    # Stable and TOTAL (§6.3): hostnames are not unique in this schema.
    return stmt.order_by(Device.hostname, Device.id)


def build(*, org_id: UUID, params: PatchComplianceParams) -> Select[Any]:
    return _base(org_id=org_id, params=params)


def build_totals(*, org_id: UUID, params: PatchComplianceParams) -> Select[Any]:
    """Aggregates over the WHOLE match, not the page (§6.3), built from the same
    `_base` statement the rows come from -- so a filter can never apply to one
    and not the other."""
    matched = _base(org_id=org_id, params=params).subquery()
    return select(
        func.count().label("devices"),
        # THE COMPLIANCE NUMBER, AND THE `never` EXCLUSION IS THE WHOLE POINT
        # (§12.13). A never-scanned device has `missing_approved = 0` because
        # nothing has ever been reported about it, not because it is patched.
        func.count()
        .filter(
            and_(matched.c.scan_state != literal("never"), matched.c.missing_approved == 0)
        )
        .label("devices_with_zero_missing"),
        func.count()
        .filter(matched.c.scan_state == literal("never"))
        .label("devices_never_scanned"),
    ).select_from(matched)


DEFINITION = ReportDefinition(
    key=KEY,
    summary=N_("Every device with its outstanding approved patches and when it was last scanned."),
    description=N_(
        "One row per device: how many patches are approved for that device's ring and "
        "still not applied, how many of those are security patches, how long the oldest "
        "has been outstanding, what was applied or failed inside the window, and when "
        "the device was last scanned. A device that has never been scanned is reported "
        "with scan_state 'never' and is counted as neither compliant nor non-compliant "
        "-- absence of a scan is absence of a record. Approval is recomputed from "
        "patch_approvals against each device's resolved ring rather than read from the "
        "device_patch_status cache, so a device moved into an approved ring after the "
        "approval is counted correctly. Computed live; it is a statement about the "
        "record at meta.generated_at."
    ),
    params=PatchComplianceParams,
    row_model=PatchComplianceRow,
    # `device_groups` is DECLARED AND NOT READ, and that is deliberate: §5.2
    # lists it as a source, `verify_definition`'s check is `read ⊆ declared`
    # precisely so a conservative declaration is legal, and it costs nothing --
    # `device_groups` maps to `devices.read`, which this report already
    # requires. Over-declaring can only make a report harder to run.
    sources=frozenset(
        {
            "devices",
            "device_groups",
            "device_patch_status",
            "patches",
            "patch_approvals",
            "patch_policies",
            # `jobs`, because a succeeded `patch.scan` job is the only durable
            # record that a device was scanned (`_scans`). It brings
            # `jobs.read` into `requires` with it, which is the correct
            # accounting and not an accident: the jobs API gates this table on
            # that code, and §0.4 is that a report never shows what the module
            # API would refuse.
            "jobs",
        }
    ),
    build=build,
    build_totals=build_totals,
    max_limit=DEFAULT_MAX_LIMIT,
    export_columns=(
        "device_id",
        "hostname",
        "os_family",
        "missing_approved",
        "missing_approved_security",
        "oldest_missing_detected_at",
        "applied_in_window",
        "failed_in_window",
        "last_scan_at",
        "scan_state",
    ),
    # The only agent-supplied free text here. Every other column is a UUID, a
    # timestamp, an int, or a fixed-set enum the server produced.
    export_free_text=frozenset({"hostname"}),
    export_row_cap=50_000,
    # Current-state sources: `device_patch_status` is the live per-device state,
    # not a history of past scans. Re-running may return different numbers
    # because the FLEET changed, not because the record of a past period was
    # rewritten.
    reproducible=True,
)
