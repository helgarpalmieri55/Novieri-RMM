"""Patch compliance reporting + evidence export (docs/modules/patch/spec.md).

Pure read-only aggregation over the patch module's tables. This module
performs **no writes** and dispatches **no jobs** — it exists solely to answer
"what is our patch compliance posture" for the API (Task 10) and frontend
(Task 12), and to produce the flat, stable-ordered export used as SOC 2
compliance evidence.

Compliance definition (used throughout this module)
-----------------------------------------------------
A `DevicePatchStatus.state` is COMPLIANT when it is `"installed"` or
`"not_applicable"` — the patch is either applied or does not apply to the
device. A state is NON-COMPLIANT (equivalently: "outstanding" / "applicable
and unresolved") when it is `"missing"`, `"approved"`, or `"failed"` — the
device has an applicable patch it has not yet successfully applied. A device
is "exposed" if it has at least one non-compliant status row.

`fleet_report`'s two compliance percentages use two different granularities,
both defined precisely here so a reviewer can check the math:

- **By os_family**: DEVICE-level. A device counts as compliant only if EVERY
  `DevicePatchStatus` row it has is compliant (zero outstanding patches).
  Devices with no `DevicePatchStatus` rows at all are excluded from the
  denominator (there is nothing to be compliant or non-compliant *about*).
- **By severity**: ROW-level. Of all `DevicePatchStatus` rows across the
  fleet whose patch has that severity, what fraction are compliant. This
  answers "how exposed are we to critical-severity patches right now,"
  independent of which device carries them.

"Never scanned" is not "nothing outstanding"
-----------------------------------------------------
Both `device_report` and `fleet_report` carry `last_scan_at`, because a count
of zero answers two different questions and a console cannot tell them apart
from the count alone — the failure the 2026-08-21 UX closeout filed against
`/reports`, where nine tiles read `0` on a page nothing had measured.

The timestamp is read from the `jobs` table and NOT from a new column. A patch
scan IS a job: `patch.scan`, `target_type="device"`, one per device, and
`JobEngine.complete` writes `finished_at` and flips the row to `succeeded`
only after this module's ingest handler has run without raising. So a
`succeeded` `patch.scan` job is already the durable, per-device record that a
scan completed AND its document was ingested; a `devices.last_scan_at` column
would be a second copy of that fact, able to disagree with it.

`DevicePatchStatus.detected_at` is deliberately not the source. It is set when
a status row is first created, so on a device whose patch set has not changed
it does not move at all — a fleet scanned hourly for a month would report its
last scan as the day it was enrolled.

`last_scan_at` is therefore a PER-DEVICE fact first. `fleet_report` publishes
the fleet-wide maximum, but never on its own: `devices_scanned` /
`devices_total` travel with it, because a maximum reproduces the very defect
it is meant to close — one scanned machine out of five hundred would date the
whole report as freshly measured.

Every query below filters on `org_id` for every table involved (Patch,
DevicePatchStatus, PatchRun, Device, Job) per the tenancy invariant — RLS is
the backstop, not the only line of defense.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, case, func, null, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Device,
    DevicePatchStatus,
    Job,
    JobStatus,
    Patch,
    PatchApproval,
    PatchPolicy,
    PatchReboot,
    PatchRun,
)
from app.modules.patch.schema import APP_SOURCES
from app.modules.patch.words import said_reason

ALL_STATES = ("missing", "approved", "installed", "failed", "not_applicable")
ALL_SEVERITIES = ("critical", "important", "moderate", "low", "unknown")

COMPLIANT_STATES = frozenset({"installed", "not_applicable"})
NON_COMPLIANT_STATES = frozenset({"missing", "approved", "failed"})

#: The severities the console calls "critical exposure". Defined HERE, beside
#: the data, because it was previously a `new Set([...])` in
#: `frontend/src/modules/patch/FleetPatchPage.tsx` -- so what counted as
#: critical was a browser constant, and every other consumer of this report
#: (the export, the AI tool surface, a future alert) either invented its own
#: answer or had none. Lowercased at the comparison: `Patch.severity` is
#: vendor-supplied text, and Ubuntu writes "important" where Microsoft writes
#: "Important".
CRITICAL_SEVERITIES = frozenset({"critical", "important", "high"})

# critical > important > moderate > low > unknown
_SEVERITY_RANK = case(
    (Patch.severity == "critical", 0),
    (Patch.severity == "important", 1),
    (Patch.severity == "moderate", 2),
    (Patch.severity == "low", 3),
    else_=4,
)


def _succeeded_scan_criteria(org_id: UUID) -> tuple[ColumnElement[bool], ...]:
    """The WHERE that identifies a completed patch scan of a device this org
    still has. Used by both scan reads below so they cannot drift apart.

    Every clause earns its place. `Job.status == succeeded` because
    `JobEngine.complete` flips a job to `succeeded` only after this module's
    ingest handler has run without raising — a failed scan measured nothing
    and must not date anything. `Device.org_id` alongside `Job.org_id` because
    the join is on `Job.target_id`, which is a bare UUID column with no
    foreign key, so nothing but this predicate ties the two tenants together.
    The join itself keeps a deleted machine's scan from dating a report about
    the fleet that remains, and keeps `devices_scanned` from ever exceeding
    `devices_total`.
    """
    return (
        Job.org_id == org_id,
        Device.org_id == org_id,
        Job.type == "patch.scan",
        Job.target_type == "device",
        Job.status == JobStatus.succeeded,
    )


async def last_successful_scan_at(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> datetime | None:
    """When this device last COMPLETED a patch scan, or `None` if it never
    has. See the module docstring for why this is read off `jobs` rather than
    stored on `devices` or inferred from `DevicePatchStatus.detected_at`."""
    return (
        await session.execute(
            select(func.max(Job.finished_at))
            .select_from(Job)
            .join(Device, Device.id == Job.target_id)
            .where(*_succeeded_scan_criteria(org_id), Job.target_id == device_id)
        )
    ).scalar_one_or_none()


def _app_sources_of(result: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    raw = (result or {}).get("app_sources")
    if not isinstance(raw, list):
        return None
    return [
        {"source": str(r.get("source", "")), "state": str(r.get("state", "")),
         "detail": r.get("detail")}
        for r in raw
        if isinstance(r, dict)
    ]


async def device_app_sources(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> list[dict[str, Any]] | None:
    """What this device's latest completed scan said about its app package
    managers; None when it said nothing (Linux, or an older agent)."""
    result = (
        await session.execute(
            select(Job.result)
            .join(Device, Device.id == Job.target_id)
            .where(*_succeeded_scan_criteria(org_id), Job.target_id == device_id)
            .order_by(Job.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _app_sources_of(result)


async def app_source_summary(
    session: AsyncSession, org_id: UUID, os_family: str | None
) -> list[dict[str, Any]]:
    """Per (source, state), how many devices' latest completed scan said so,
    with one example reason. Read off `jobs.result`; no column of its own."""
    latest = (
        select(Job.target_id, Job.result)
        .join(Device, Device.id == Job.target_id)
        .where(*_succeeded_scan_criteria(org_id))
        .order_by(Job.target_id, Job.finished_at.desc())
        .distinct(Job.target_id)
    )
    if os_family:
        latest = latest.where(Device.os_family == os_family)
    counts: dict[tuple[str, str], dict[str, Any]] = {}
    for _device_id, result in (await session.execute(latest)).all():
        for row in _app_sources_of(result) or []:
            key = (row["source"], row["state"])
            entry = counts.setdefault(
                key, {"source": key[0], "state": key[1], "devices": 0, "detail": None}
            )
            entry["devices"] += 1
            if entry["detail"] is None and row["detail"]:
                entry["detail"] = row["detail"]
    return sorted(counts.values(), key=lambda r: (r["source"], r["state"]))


async def fleet_scan_coverage(session: AsyncSession, org_id: UUID) -> dict[str, Any]:
    """The fleet's scan coverage: the most recent successful scan anywhere in
    the org, how many distinct devices have ever completed one, and how many
    devices the org has.

    The three travel together on purpose. A maximum on its own reproduces the
    defect it is meant to close — one scanned machine out of five hundred
    would date the whole report as freshly measured — so the console is given
    the denominator in the same payload. Devices, not scans: scanning one
    machine hourly is one machine measured, the same lesson
    `exposed_device_count` above records.
    """
    scanned_at, scanned_devices = (
        await session.execute(
            select(
                func.max(Job.finished_at),
                func.count(func.distinct(Job.target_id)),
            )
            .select_from(Job)
            .join(Device, Device.id == Job.target_id)
            .where(*_succeeded_scan_criteria(org_id))
        )
    ).one()
    devices_total = (
        await session.execute(
            select(func.count()).select_from(Device).where(Device.org_id == org_id)
        )
    ).scalar_one()
    return {
        "last_scan_at": scanned_at,
        "devices_scanned": scanned_devices,
        "devices_total": devices_total,
    }


async def device_report(session: AsyncSession, org_id: UUID, device_id: UUID) -> dict[str, Any]:
    """Per-device compliance snapshot: counts by state, counts by severity of
    outstanding (non-compliant) patches, whether the device's latest
    `PatchRun` left a reboot pending, and when this device last completed a
    scan (`None` = never, which is what tells a caller that the zeros above
    are an absence of measurement rather than an absence of exposure).
    """
    state_rows = (
        await session.execute(
            select(DevicePatchStatus.state, func.count())
            .where(
                DevicePatchStatus.org_id == org_id,
                DevicePatchStatus.device_id == device_id,
            )
            .group_by(DevicePatchStatus.state)
        )
    ).all()
    counts_by_state = dict.fromkeys(ALL_STATES, 0)
    for state, count in state_rows:
        counts_by_state[state] = count

    severity_rows = (
        await session.execute(
            select(Patch.severity, func.count())
            .select_from(DevicePatchStatus)
            .join(Patch, Patch.id == DevicePatchStatus.patch_id)
            .where(
                DevicePatchStatus.org_id == org_id,
                Patch.org_id == org_id,
                DevicePatchStatus.device_id == device_id,
                DevicePatchStatus.state.in_(NON_COMPLIANT_STATES),
            )
            .group_by(Patch.severity)
        )
    ).all()
    counts_by_severity = dict.fromkeys(ALL_SEVERITIES, 0)
    for severity, count in severity_rows:
        counts_by_severity[severity] = count

    latest_reboot_required = (
        await session.execute(
            select(PatchRun.reboot_required)
            .where(PatchRun.org_id == org_id, PatchRun.device_id == device_id)
            .order_by(PatchRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    restart = (
        await session.execute(
            select(PatchReboot)
            .where(PatchReboot.org_id == org_id, PatchReboot.device_id == device_id)
            .order_by(PatchReboot.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return {
        "device_id": device_id,
        "counts_by_state": counts_by_state,
        "counts_by_severity": counts_by_severity,
        "pending_reboot": bool(latest_reboot_required),
        "last_scan_at": await last_successful_scan_at(session, org_id, device_id),
        "restart": restart_row(restart) if restart is not None else None,
        "app_sources": await device_app_sources(session, org_id, device_id),
    }


def restart_row(row: PatchReboot) -> dict[str, Any]:
    return {
        "id": row.id,
        "device_id": row.device_id,
        "state": row.state,
        "origin": row.origin,
        "scheduled_for": row.scheduled_for,
        "user_present": row.user_present,
        "reason": said_reason(row.reason),
        "created_at": row.created_at,
        "closed_at": row.closed_at,
    }


async def open_restarts(session: AsyncSession, org_id: UUID, limit: int) -> list[dict[str, Any]]:
    """Every device still owed a restart, oldest first, with its name."""
    rows = (
        await session.execute(
            select(PatchReboot, Device.hostname)
            .join(Device, Device.id == PatchReboot.device_id)
            .where(
                PatchReboot.org_id == org_id,
                Device.org_id == org_id,
                PatchReboot.state.in_(("pending", "dispatched", "scheduled")),
            )
            .order_by(PatchReboot.created_at)
            .limit(limit)
        )
    ).all()
    return [{**restart_row(row), "hostname": hostname} for row, hostname in rows]


async def fleet_report(session: AsyncSession, org_id: UUID) -> dict[str, Any]:
    """Fleet-wide compliance: compliant % by os_family (device-level) and by
    severity (row-level), plus an exposed-device list (one row per
    non-compliant (device, patch) pair) ordered by `Patch.priority` DESC with
    NULLs last, then by severity (critical first), then by hostname/identifier
    for a fully deterministic tie-break, plus the fleet's scan coverage
    (`fleet_scan_coverage`) so an empty exposure list can be read as measured
    or unmeasured rather than guessed at.
    """
    device_status_rows = (
        await session.execute(
            select(
                Device.os_family,
                func.bool_or(DevicePatchStatus.state.in_(NON_COMPLIANT_STATES)).label(
                    "has_noncompliant"
                ),
            )
            .select_from(DevicePatchStatus)
            .join(Device, Device.id == DevicePatchStatus.device_id)
            .where(DevicePatchStatus.org_id == org_id, Device.org_id == org_id)
            .group_by(DevicePatchStatus.device_id, Device.os_family)
        )
    ).all()

    os_family_totals: dict[str, int] = {}
    os_family_compliant: dict[str, int] = {}
    for os_family, has_noncompliant in device_status_rows:
        os_family_totals[os_family] = os_family_totals.get(os_family, 0) + 1
        if not has_noncompliant:
            os_family_compliant[os_family] = os_family_compliant.get(os_family, 0) + 1

    compliant_pct_by_os_family = {
        os_family: (os_family_compliant.get(os_family, 0) / total) * 100
        for os_family, total in os_family_totals.items()
    }

    severity_rows = (
        await session.execute(
            select(
                Patch.severity,
                func.count().filter(DevicePatchStatus.state.in_(COMPLIANT_STATES)),
                func.count(),
            )
            .select_from(DevicePatchStatus)
            .join(Patch, Patch.id == DevicePatchStatus.patch_id)
            .where(DevicePatchStatus.org_id == org_id, Patch.org_id == org_id)
            .group_by(Patch.severity)
        )
    ).all()
    compliant_pct_by_severity = {
        severity: (compliant / total) * 100 for severity, compliant, total in severity_rows
    }

    exposed_rows = (
        await session.execute(
            select(
                DevicePatchStatus.device_id,
                Device.hostname,
                Patch.id.label("patch_id"),
                Patch.identifier,
                Patch.severity,
                Patch.priority,
                DevicePatchStatus.state,
            )
            .select_from(DevicePatchStatus)
            .join(Patch, Patch.id == DevicePatchStatus.patch_id)
            .join(Device, Device.id == DevicePatchStatus.device_id)
            .where(
                DevicePatchStatus.org_id == org_id,
                Patch.org_id == org_id,
                Device.org_id == org_id,
                DevicePatchStatus.state.in_(NON_COMPLIANT_STATES),
            )
            .order_by(
                Patch.priority.desc().nulls_last(),
                _SEVERITY_RANK.asc(),
                Device.hostname.asc(),
                Patch.identifier.asc(),
            )
        )
    ).all()
    exposures = [
        {
            "device_id": row.device_id,
            "hostname": row.hostname,
            "patch_id": row.patch_id,
            "identifier": row.identifier,
            "severity": row.severity,
            "priority": row.priority,
            "state": row.state,
        }
        for row in exposed_rows
    ]

    # DEVICES, NOT FINDINGS, and this is the fix for what the operator saw on
    # 2026-08-20: "it thinks there are 66 devices, it is just 1 device with 66
    # pending updates". The list above is one row per (device, patch) pair --
    # which it always was, and which this function's docstring always said --
    # but it was called `exposed_devices`, and the console rendered its length
    # under the label "Exposed devices". A single machine missing 66 packages
    # was reported as 66 exposed machines, and on a one-device fleet "Patches
    # in catalog" and "Exposed devices" printed the same number.
    #
    # Counted HERE rather than derived in the browser. A count taken from a
    # list is correct only while the list is complete: this endpoint returns
    # every exposure today, and the first time a large fleet forces a cap, a
    # browser-side `new Set(rows).size` would start under-reporting silently
    # while still looking like arithmetic.
    exposed_device_count = len({row.device_id for row in exposed_rows})
    critical_device_count = len(
        {
            row.device_id
            for row in exposed_rows
            if (row.severity or "").lower() in CRITICAL_SEVERITIES
        }
    )

    return {
        "compliant_pct_by_os_family": compliant_pct_by_os_family,
        "compliant_pct_by_severity": compliant_pct_by_severity,
        "exposures": exposures,
        "exposed_device_count": exposed_device_count,
        "critical_device_count": critical_device_count,
        # WHEN, and OVER HOW MUCH OF THE FLEET, the numbers above were taken.
        # Everything else in this dict counts findings; an empty findings list
        # is the same shape whether the fleet is clean or has never answered,
        # and the console had no way to tell those apart.
        **await fleet_scan_coverage(session, org_id),
    }


async def export_rows(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """Flat device x patch rows for SOC 2 compliance-evidence export: one row
    per `(device, patch)` pair via `DevicePatchStatus`, ordered deterministically
    by hostname then patch identifier so repeated exports are stable/diffable.
    """
    rows = (
        await session.execute(
            select(
                Device.id.label("device_id"),
                Device.hostname,
                Device.os_family,
                Patch.id.label("patch_id"),
                Patch.identifier,
                Patch.severity,
                Patch.kb,
                Patch.cve,
                Patch.source,
                Patch.priority,
                DevicePatchStatus.state,
                DevicePatchStatus.detected_at,
                DevicePatchStatus.applied_at,
                DevicePatchStatus.last_error,
            )
            .select_from(DevicePatchStatus)
            .join(Device, Device.id == DevicePatchStatus.device_id)
            .join(Patch, Patch.id == DevicePatchStatus.patch_id)
            .where(
                DevicePatchStatus.org_id == org_id,
                Device.org_id == org_id,
                Patch.org_id == org_id,
            )
            .order_by(Device.hostname.asc(), Patch.identifier.asc())
        )
    ).all()

    return [
        {
            "device_id": row.device_id,
            "hostname": row.hostname,
            "os_family": row.os_family,
            "patch_id": row.patch_id,
            "identifier": row.identifier,
            "severity": row.severity,
            "kb": row.kb,
            "cve": row.cve,
            "source": row.source,
            "priority": row.priority,
            "state": row.state,
            "detected_at": row.detected_at,
            "applied_at": row.applied_at,
            "last_error": row.last_error,
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# The fleet ROLLUPS (docs/modules/patch/view-redesign.md §3)
#
# WHY THEY ARE HERE RATHER THAN IN THE BROWSER. `/patch` used to render
# `fleet_report()["exposures"]` -- one row per `(device, patch)` pair, uncapped
# -- above a catalogue page that carried no fleet count at all, so one Ubuntu
# box with 300 upgradable packages was ~600 rows saying one thing. The number
# that makes a patch list actionable, "41 of your 60 devices are missing this",
# is a `count(distinct device_id)` per patch: a console that fetched 500 rows
# and grouped them would be right only until the cap bit, and then silently
# wrong. So it is computed in SQL, paged in SQL, and filtered in SQL.
#
# EVERY QUERY BELOW NAMES `org_id` FOR EVERY TABLE IT TOUCHES. RLS is the
# backstop, not the only line: `org_session` runs as NOBYPASSRLS `app_user`,
# where a dropped predicate turns no route test red. A leaked ROW is visible; a
# leaked row inside `count(distinct device_id)` is a plausible number.
# --------------------------------------------------------------------------- #

#: Severest first. Used to order the update list and to pick a device's worst
#: outstanding severity, so the two cannot disagree about what "worse" means.
_SEVERITY_ORDER = {name: rank for rank, name in enumerate(ALL_SEVERITIES)}

#: What `sort=` accepts. An unrecognised value falls back to `severity` rather
#: than raising: the sort is a view preference, and a stale bookmark should
#: render the list, not a 422.
UPDATE_SORTS = ("severity", "devices", "identifier", "first_seen")

#: The rollout order of the three rings spec PD-04 names, used ONLY for a ring
#: no `patch_policies` row declares an order for.
#:
#: It matters on a fresh deployment, which is the common case rather than an
#: edge one: with no policy rows at all, every ring is unranked, and sorting
#: `["test", "pilot"]` alphabetically would print `pilot, test` -- the rollout
#: backwards, in the column an operator reads to decide how far an update has
#: already travelled. `frontend/src/modules/patch/UpdatesPanel.tsx` offers the
#: same three for the same reason.
_CANONICAL_RING_ORDER = {"test": 0, "pilot": 50, "broad": 100}

#: The page the console asks for by default, and the ceiling it may ask for.
#: A page bigger than this is the long list again with a scrollbar.
UPDATE_PAGE_DEFAULT = 25
UPDATE_PAGE_MAX = 200


def _outstanding() -> ColumnElement[bool]:
    """The one definition of "this machine does not have this patch", shared by
    both rollups so a per-patch count and a per-device count cannot disagree."""
    return DevicePatchStatus.state.in_(NON_COMPLIANT_STATES)


async def _cleared_rings_by_patch(
    session: AsyncSession, org_id: UUID, patch_ids: list[UUID]
) -> dict[UUID, list[str]]:
    """The rings each of these patches is currently cleared for, in rollout
    order.

    A SECOND QUERY RATHER THAN A JOIN. Joining `patch_approvals` into the
    grouped aggregate above would fan each patch out by its approvals, and
    while `count(distinct device_id)` survives that, nothing else about the
    query would stay readable. Keyed on the page's ids, so it is bounded by the
    page size rather than by the catalogue.

    REVOKED APPROVALS ARE EXCLUDED. They are soft-revoked so the trail holds
    (spec §5) and the Approvals panel is where they stay on the record; this
    answers what is IN FORCE, which is a different question.

    ORDERED BY `patch_policies.ring_order`, the same column `GET /rings` orders
    by -- test before pilot before broad is the staged rollout, and a list
    sorted alphabetically invites the reader to reach for the widest ring
    first. A ring no policy names has no declared order, so it sorts last by
    name rather than being dropped: an approval that exists must be shown.
    """
    if not patch_ids:
        return {}
    orders: dict[str, int] = {
        ring_name: ring_order
        for ring_name, ring_order in (
            await session.execute(
                select(PatchPolicy.ring_name, func.min(PatchPolicy.ring_order))
                .where(PatchPolicy.org_id == org_id)
                .group_by(PatchPolicy.ring_name)
            )
        ).all()
    }
    rows = (
        await session.execute(
            select(PatchApproval.patch_id, PatchApproval.ring_name).where(
                PatchApproval.org_id == org_id,
                PatchApproval.patch_id.in_(patch_ids),
                PatchApproval.revoked.is_(False),
            )
        )
    ).all()
    by_patch: dict[UUID, list[str]] = {}
    for patch_id, ring_name in rows:
        by_patch.setdefault(patch_id, []).append(ring_name)
    unranked = max([*orders.values(), *_CANONICAL_RING_ORDER.values()], default=0) + 1

    def rank(ring_name: str) -> tuple[int, str]:
        declared = orders.get(ring_name)
        if declared is not None:
            return (declared, ring_name)
        return (_CANONICAL_RING_ORDER.get(ring_name, unranked), ring_name)

    for rings in by_patch.values():
        rings.sort(key=rank)
    return by_patch


def _update_filters(
    org_id: UUID,
    *,
    os_family: str | None,
    source: str | None,
    q: str | None,
    kind: str | None = None,
) -> list[ColumnElement[bool]]:
    """The predicates that narrow the CATALOGUE half of the rollup.

    Severity is deliberately not here: the severity histogram applies every
    other filter and not this one, so the console's "292 more are outside the
    severity filter" line is true in the current context.
    """
    where: list[ColumnElement[bool]] = [Patch.org_id == org_id]
    if os_family:
        where.append(Patch.os_family == os_family)
    if source:
        where.append(Patch.source == source)
    if kind == "apps":
        where.append(Patch.source.in_(APP_SOURCES))
    elif kind == "os":
        where.append(Patch.source.not_in(APP_SOURCES))
    if q:
        needle = f"%{q.strip()}%"
        where.append(Patch.identifier.ilike(needle) | Patch.title.ilike(needle))
    return where


#: Most failures named on one run summary. The rest are in the CSV export.
RUN_FAILURE_CAP = 20


async def update_rollup(
    session: AsyncSession,
    org_id: UUID,
    *,
    severities: frozenset[str] | None = None,
    os_family: str | None = None,
    source: str | None = None,
    ring: str | None = None,
    device_id: UUID | None = None,
    q: str | None = None,
    outstanding_only: bool = True,
    sort: str = "severity",
    limit: int = UPDATE_PAGE_DEFAULT,
    offset: int = 0,
    kind: str | None = None,
) -> dict[str, Any]:
    """One row per PATCH, with the fleet counts that make the row actionable.

    `severities=None` means every severity. THE SERVER IMPOSES NO SEVERITY
    DEFAULT -- the console has one (`critical` + `important`), and keeping it
    there is what stops the evidence export and the API from inheriting a
    narrowing that exists to shorten a screen.

    `device_id` makes this one query serve three surfaces: the fleet list, a
    device row's expansion, and the device detail page's Patch tab. A
    per-device patch list is this query with one more predicate.

    The response carries `total_unfiltered` and `severity_counts` alongside the
    page because the tiles above the table must be the same arithmetic as the
    table. A tile counted in the browser from a capped page is how this module
    shipped a tile/table disagreement twice.
    """
    status_join = [
        DevicePatchStatus.patch_id == Patch.id,
        DevicePatchStatus.org_id == org_id,
    ]
    if device_id is not None:
        status_join.append(DevicePatchStatus.device_id == device_id)

    def counted(state: str) -> ColumnElement[int]:
        return func.count(func.distinct(DevicePatchStatus.device_id)).filter(
            DevicePatchStatus.state == state
        )

    outstanding_count = func.count(func.distinct(DevicePatchStatus.device_id)).filter(
        _outstanding()
    )

    where = _update_filters(org_id, os_family=os_family, source=source, q=q, kind=kind)
    if ring:
        where.append(
            select(PatchApproval.id)
            .where(
                PatchApproval.patch_id == Patch.id,
                PatchApproval.org_id == org_id,
                PatchApproval.ring_name == ring,
                PatchApproval.revoked.is_(False),
            )
            .exists()
        )

    def grouped(selection: Any) -> Any:
        query = (
            select(*selection)
            .select_from(Patch)
            .outerjoin(DevicePatchStatus, and_(*status_join))
            .where(*where)
            .group_by(Patch.id)
        )
        if outstanding_only:
            query = query.having(outstanding_count > 0)
        return query

    # The severity histogram: every filter EXCEPT severity, one entry per
    # severity present. `total_unfiltered` is its sum by construction, so the
    # console's "showing N of M" can never drift from its own breakdown.
    histogram = grouped((Patch.id, Patch.severity)).subquery()
    histogram_rows = (
        await session.execute(
            select(histogram.c.severity, func.count()).group_by(histogram.c.severity)
        )
    ).all()
    severity_counts = {severity: count for severity, count in histogram_rows}
    total_unfiltered = sum(severity_counts.values())

    if severities is not None:
        where = [*where, Patch.severity.in_(severities)]

    # THE OUTCOME OF THE LAST APPLY, and only when this query is scoped to one
    # device. Without them a device-scoped row could say a patch is
    # outstanding but never why: `failed` and `missing` render identically,
    # so an apply that errored looked exactly like one that was never
    # attempted. `device_id` in `status_join` means the outer join matches at
    # most one status row per patch, so `max()` here is "that row's value"
    # rather than an aggregate over several -- and when `device_id` is None
    # these collapse across the fleet, which would be meaningless, so they are
    # NULLed out below rather than reported.
    def device_scoped(column: Any) -> ColumnElement[Any]:
        return func.max(column) if device_id is not None else null()

    page_columns = (
        Patch.id,
        Patch.os_family,
        Patch.source,
        Patch.identifier,
        Patch.title,
        Patch.severity,
        Patch.kb,
        Patch.cve,
        Patch.priority,
        Patch.first_seen_at,
        counted("missing").label("devices_missing"),
        counted("approved").label("devices_approved"),
        counted("failed").label("devices_failed"),
        counted("installed").label("devices_installed"),
        outstanding_count.label("devices_outstanding"),
        device_scoped(DevicePatchStatus.state).label("device_state"),
        device_scoped(DevicePatchStatus.last_error).label("device_last_error"),
        device_scoped(DevicePatchStatus.applied_at).label("device_applied_at"),
    )
    query = grouped(page_columns)
    total = (
        await session.execute(
            select(func.count()).select_from(
                grouped((Patch.id,)).subquery()
            )
        )
    ).scalar_one()

    if sort == "devices":
        order = [outstanding_count.desc(), _SEVERITY_RANK.asc(), Patch.identifier.asc()]
    elif sort == "identifier":
        order = [Patch.identifier.asc()]
    elif sort == "first_seen":
        order = [Patch.first_seen_at.desc(), Patch.identifier.asc()]
    else:
        order = [_SEVERITY_RANK.asc(), outstanding_count.desc(), Patch.identifier.asc()]
    rows = (await session.execute(query.order_by(*order).limit(limit).offset(offset))).all()

    cleared = await _cleared_rings_by_patch(session, org_id, [row.id for row in rows])
    items = [
        {
            "id": row.id,
            "os_family": row.os_family,
            "source": row.source,
            "identifier": row.identifier,
            "title": row.title,
            "severity": row.severity,
            "kb": row.kb,
            "cve": row.cve,
            "priority": row.priority,
            "first_seen_at": row.first_seen_at,
            "devices_missing": row.devices_missing,
            "devices_approved": row.devices_approved,
            "devices_failed": row.devices_failed,
            "devices_installed": row.devices_installed,
            "devices_outstanding": row.devices_outstanding,
            "cleared_rings": cleared.get(row.id, []),
            "device_state": row.device_state,
            "device_last_error": row.device_last_error,
            "device_applied_at": row.device_applied_at,
        }
        for row in rows
    ]
    return {
        "items": items,
        "total": total,
        "total_unfiltered": total_unfiltered,
        "severity_counts": severity_counts,
        # WHEN, AND OVER HOW MUCH OF THE FLEET. An empty page is the same shape
        # on a clean fleet and on one nothing has ever scanned.
        **await fleet_scan_coverage(session, org_id),
    }


async def device_rollup(
    session: AsyncSession,
    org_id: UUID,
    *,
    q: str | None = None,
    os_family: str | None = None,
    limit: int = UPDATE_PAGE_DEFAULT,
    offset: int = 0,
) -> dict[str, Any]:
    """One row per DEVICE that has something outstanding, worst first.

    NEVER SEVERITY-FILTERED, deliberately. The update list narrows to the
    severities that demand a decision; this panel is the guarantee that
    narrowing hides nothing -- a device's row always states its TOTAL
    outstanding count, so a Windows machine whose cumulative update Microsoft
    left unrated (`agent/internal/capabilities/patch/winupdate.go`:
    `windowsSeverity`) is still visibly carrying something.

    `total` is machines that need something; `devices_total` is the fleet. The
    pair is the 2026-08-20 defect in its other direction -- "1 device needs
    patches" is only readable beside "of 1 device".
    """
    where: list[ColumnElement[bool]] = [
        DevicePatchStatus.org_id == org_id,
        Device.org_id == org_id,
        Patch.org_id == org_id,
        _outstanding(),
    ]
    if os_family:
        where.append(Device.os_family == os_family)
    if q:
        where.append(Device.hostname.ilike(f"%{q.strip()}%"))

    worst_rank = func.min(_SEVERITY_RANK)
    outstanding = func.count()
    # HOW MANY OF THOSE WERE TRIED AND DID NOT TAKE. `_outstanding()` already
    # counts `failed` as outstanding -- correctly, the machine still does not
    # have the patch -- which is exactly why the number needs saying
    # separately: a row reading "12 outstanding" is a different call to action
    # when four of them are errors from a run that already happened.
    failed = func.count().filter(DevicePatchStatus.state == "failed")

    def grouped(selection: Any) -> Any:
        return (
            select(*selection)
            .select_from(DevicePatchStatus)
            .join(Device, Device.id == DevicePatchStatus.device_id)
            .join(Patch, Patch.id == DevicePatchStatus.patch_id)
            .where(*where)
            .group_by(DevicePatchStatus.device_id, Device.hostname, Device.os_family)
        )

    total = (
        await session.execute(
            select(func.count()).select_from(
                grouped((DevicePatchStatus.device_id,)).subquery()
            )
        )
    ).scalar_one()

    severity_columns = [
        func.count().filter(Patch.severity == severity).label(f"sev_{severity}")
        for severity in ALL_SEVERITIES
    ]
    rows = (
        await session.execute(
            grouped(
                (
                    DevicePatchStatus.device_id,
                    Device.hostname,
                    Device.os_family,
                    outstanding.label("outstanding"),
                    failed.label("failed"),
                    worst_rank.label("worst_rank"),
                    *severity_columns,
                )
            )
            .order_by(worst_rank.asc(), outstanding.desc(), Device.hostname.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    scans = await _last_scan_by_device(session, org_id, [row.device_id for row in rows])
    by_rank = {rank: name for name, rank in _SEVERITY_ORDER.items()}
    items = [
        {
            "device_id": row.device_id,
            "hostname": row.hostname,
            "os_family": row.os_family,
            "outstanding": row.outstanding,
            "failed": row.failed,
            "worst_severity": by_rank.get(row.worst_rank),
            "counts_by_severity": {
                severity: getattr(row, f"sev_{severity}") for severity in ALL_SEVERITIES
            },
            "last_scan_at": scans.get(row.device_id),
        }
        for row in rows
    ]
    return {
        "items": items,
        "total": total,
        **await fleet_scan_coverage(session, org_id),
    }


async def recent_runs(
    session: AsyncSession,
    org_id: UUID,
    *,
    device_id: UUID | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """The apply RUNS themselves, newest first — what a dispatch actually did.

    `patch_runs` has existed since the module shipped and NO route read it, so
    the answer to "did the updates I just applied install?" lived only in the
    job engine's raw result JSON. `DevicePatchStatus` carries the end state per
    patch, which is the durable truth, but it cannot say *this run* — a patch
    that failed on Tuesday and installed on Wednesday reads as `installed`,
    with no record on the patch page that Tuesday happened.

    `installed`/`failed` are derived HERE from the stored `results` document
    rather than stored as columns, because the document is the agent's own
    report and a denormalized count would be a second source of truth for the
    same fact. A run still in flight has `finished_at IS NULL` and reports
    zeroes; that is distinguishable from a finished run that installed
    nothing, which is the distinction "0 installed" alone cannot make.

    Ordered by `id` (uuid7, so time-ordered) rather than `finished_at`, which
    is NULL while the job is out — an in-flight run must sort to the top, not
    the bottom.
    """
    query = select(PatchRun).where(PatchRun.org_id == org_id)
    if device_id is not None:
        query = query.where(PatchRun.device_id == device_id)
    rows = (
        await session.execute(query.order_by(PatchRun.id.desc()).limit(limit))
    ).scalars().all()

    items: list[dict[str, Any]] = []
    for run in rows:
        outcomes = (run.results or {}).get("results") or []
        installed = sum(1 for o in outcomes if o.get("state") == "installed")
        failed = sum(1 for o in outcomes if o.get("state") == "failed")
        items.append(
            {
                "id": run.id,
                "job_id": run.job_id,
                "device_id": run.device_id,
                "installed": installed,
                "failed": failed,
                "reboot_required": run.reboot_required,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                # The failures, named. A count tells the operator to go
                # looking; this tells them what to look at. Capped because a
                # run over 300 packages that failed on all of them must not
                # become a 300-entry response body on a summary route.
                "failures": [
                    {
                        "source": o.get("source"),
                        "identifier": o.get("identifier"),
                        "error": o.get("error"),
                    }
                    for o in outcomes
                    if o.get("state") == "failed"
                ][:RUN_FAILURE_CAP],
            }
        )
    return items


async def _last_scan_by_device(
    session: AsyncSession, org_id: UUID, device_ids: list[UUID]
) -> dict[UUID, datetime]:
    """`last_successful_scan_at` for a page of devices, in one query.

    PER DEVICE, not the fleet maximum: a row reading "0 outstanding" on a
    machine that has never answered is a different fact from one on a machine
    scanned ten minutes ago, and the fleet maximum would date both as fresh.
    """
    if not device_ids:
        return {}
    rows = (
        await session.execute(
            select(Job.target_id, func.max(Job.finished_at))
            .select_from(Job)
            .join(Device, Device.id == Job.target_id)
            .where(*_succeeded_scan_criteria(org_id), Job.target_id.in_(device_ids))
            .group_by(Job.target_id)
        )
    ).all()
    return {device_id: finished_at for device_id, finished_at in rows}
