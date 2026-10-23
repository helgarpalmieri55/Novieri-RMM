"""Patch management REST API (docs/modules/patch/spec.md §12).

Routes under ``/api/v1/patch``. Every route is gated server-side by
``require_permission`` (the frontend never gates security, invariant #6); no
response model carries a secret (the module has none — patch apply runs with
the agent's local privilege, never a vaulted credential).

``POST /scan`` and ``POST /apply`` build+publish signed jobs the same way
``app.modules.remote.service.RemoteSessionService.start_session`` does: the
job is created and signed inside the tenant transaction, but the actual
``publish_job`` (the external Redis side effect) is deferred until AFTER the
transaction commits, so a rolled-back request never dispatches a job to a
device that doesn't durably have one. ``POST /apply`` gets this by publishing
the ``(job_id, device_id, envelope)`` triples ``dispatch_apply`` returns
(``app.modules.patch.apply.dispatch_apply`` itself never publishes) —
mirrors ``app.modules.deploy.api``'s ``POST /deployments/{id}/execute`` /
``POST /scripts/{id}/run``.
"""

import csv
import io
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.csv_export import escape_formula
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job
from app.models import Device, DevicePatchStatus, Patch, PatchApproval, PatchPolicy
from app.modules.change_mgmt.gates import (
    authorizing_change,
    gate_said,
    link_actions,
    patch_approval_gate,
    record_auto_change,
    record_gate_refusal,
)
from app.modules.change_mgmt.state import ChangeNotFoundError, ChangeValidationError
from app.modules.patch.apply import dispatch_apply
from app.modules.patch.approve import (
    BULK_APPROVAL_MAX,
    ApprovalOutcome,
    UnknownPatchError,
    approve_patches,
    devices_in_ring,
    list_approvals,
    patch_identifiers,
    revoke_approval,
)
from app.modules.patch.policy import resolve_ring_populations
from app.modules.patch.reboot import (
    RestartUnavailable,
    request_operator_restart,
    restart_window,
)
from app.modules.patch.reporting import (
    UPDATE_PAGE_DEFAULT,
    UPDATE_PAGE_MAX,
    UPDATE_SORTS,
    app_source_summary,
    device_report,
    device_rollup,
    export_rows,
    fleet_report,
    open_restarts,
    recent_runs,
    restart_row,
    update_rollup,
)
from app.modules.patch.schema import PatchScanParams

router = APIRouter(prefix="/patch", tags=["patch"])


# --------------------------------------------------------------------------- #
# Models — none carry a secret (the module has none).
# --------------------------------------------------------------------------- #


class PatchOut(BaseModel):
    id: UUID
    os_family: str
    source: str
    identifier: str
    title: str
    severity: str
    kb: str | None
    cve: list[str] | None
    superseded_by: UUID | None
    priority: int | None
    first_seen_at: datetime


class PatchListResponse(BaseModel):
    items: list[PatchOut]
    total: int


class RestartOut(BaseModel):
    """A restart a device owes after patching. `state`: pending (waiting for
    the maintenance window), dispatched, scheduled (the OS has it booked for
    `scheduled_for`), completed, cancelled or failed (`reason` says why)."""

    id: UUID
    device_id: UUID
    state: str
    origin: str
    scheduled_for: datetime | None
    user_present: bool | None
    reason: str | None
    created_at: datetime
    closed_at: datetime | None


class RestartWindowOut(BaseModel):
    """When a restart asked for now would be sent. `opens_at` is `now` while
    the window is open and None when it does not open within eight days. A
    signed-in user on a workstation gets `user_warning_minutes` of notice;
    anyone else, five minutes, inside the window."""

    open_now: bool
    opens_at: datetime | None
    user_warning_minutes: int


class RestartRequestOut(BaseModel):
    restart: RestartOut
    #: True when a restart was already booked; nothing new was recorded.
    already_requested: bool
    window: RestartWindowOut


class RestartListItem(RestartOut):
    """An open restart with the device's name."""

    hostname: str | None


class RestartListResponse(BaseModel):
    items: list[RestartListItem]
    total: int


class AppSourceOut(BaseModel):
    """One app package manager on one device: scanned, unsupported or failed."""

    source: str
    state: str
    detail: str | None


class AppSourceSummaryOut(BaseModel):
    """How many devices' latest scan put an app package manager in a state."""

    source: str
    state: str
    devices: int
    #: One device's reason, for unsupported and failed.
    detail: str | None


class AppSourceSummaryResponse(BaseModel):
    items: list[AppSourceSummaryOut]


class DeviceStatusOut(BaseModel):
    device_id: UUID
    counts_by_state: dict[str, int]
    counts_by_severity: dict[str, int]
    pending_reboot: bool
    #: When this device last COMPLETED a patch scan; `None` = never. Without
    #: it the counts above are unreadable -- an all-zero `counts_by_state` is
    #: the same shape on a fully patched machine and on one that has never
    #: answered. Read off the `jobs` table, no new column; see
    #: `app.modules.patch.reporting`.
    last_scan_at: datetime | None
    #: The device's latest restart record; None if it never owed one.
    restart: RestartOut | None = None
    #: What the latest scan said about winget / Homebrew; None where the OS
    #: has no app package manager or the agent predates it.
    app_sources: list[AppSourceOut] | None = None
    #: When a restart asked for now would happen, for the console to say so
    #: before anyone confirms one.
    restart_window: RestartWindowOut | None = None


class ScanRequest(BaseModel):
    device_ids: list[UUID] | None = None


class ScanResponse(BaseModel):
    job_ids: list[UUID]


class ApprovalCreateRequest(BaseModel):
    patch_id: UUID
    #: 40 characters because `patch_approvals.ring_name` is `String(40)`;
    #: without the bound an over-long name reached the database as an error
    #: rather than a 422 naming the field.
    ring_name: str = Field(min_length=1, max_length=40)
    #: An approved change this approval implements. Required where a change
    #: gate covers the ring's machines; otherwise one is recorded for it.
    change_id: UUID | None = None


class BulkApprovalCreateRequest(BaseModel):
    """One ring decision over a SELECTION of patches.

    `patch_ids` is an explicit list rather than a filter, deliberately. A filter
    ("approve everything critical") is evaluated on the server AFTER the operator
    has read the screen, so the set that gets cleared is not the set they looked
    at; a list is exactly what they ticked. `max_length` matches the ceiling
    `GET /patch/patches` puts on a page, so a console can approve everything it
    was able to show and no more.
    """

    patch_ids: list[UUID] = Field(min_length=1, max_length=BULK_APPROVAL_MAX)
    ring_name: str = Field(min_length=1, max_length=40)
    change_id: UUID | None = None


class ApprovalOut(BaseModel):
    """One approval of one patch into one ring.

    `id` is the value `DELETE /approvals/{approval_id}` takes; `revoked` is
    `True` for an approval that has been withdrawn — the row survives the
    revoke (spec §5) so the trail holds, and `revoked_by`/`revoked_at` say
    who withdrew it and when. `approved_by` is `None` for a system
    auto-approval (PD-02), not for an unknown actor.
    """

    id: UUID
    patch_id: UUID
    #: The update this clears, NAMED -- `KB5031354`, `linux-image-6.8`.
    #:
    #: `patch_id` alone was the whole row, so a console could not print what an
    #: approval was FOR without a second request; this one paid for it by
    #: fetching a 500-row page of `GET /patch/patches` on every visit purely to
    #: build an id -> identifier map, and rendered a raw uuid for any approval
    #: whose patch fell outside that page.
    #:
    #: `None` MEANS THIS ORG HAS NO PATCH BY THAT ID, which is a fact worth
    #: seeing rather than a formatting gap -- see `approve.ApprovalListing`.
    patch_identifier: str | None
    ring_name: str
    approved_by: UUID | None
    approved_at: datetime
    revoked: bool
    revoked_by: UUID | None
    revoked_at: datetime | None


class ApprovalListResponse(BaseModel):
    """A page of this org's approvals, newest first, and `total` — the count
    matching the filters BEFORE `limit`/`offset`, so a caller can tell a short
    page from the end of the list."""

    items: list[ApprovalOut]
    total: int


class BulkApprovalResponse(BaseModel):
    """The outcome of one bulk approval, one entry per patch named.

    `items` is in REQUEST ORDER so a console can pair each result with the row
    the operator ticked. `newly_approved` and `already_in_force` are reported
    separately because they are different facts about the audit log: a patch
    already cleared for this ring was not re-approved and wrote no event, and a
    console reporting the batch size as approvals would be describing writes
    that never happened.
    """

    items: list[ApprovalOut]
    total: int
    newly_approved: int
    already_in_force: int


class RingOut(BaseModel):
    """One deployment ring, and how many of this org's devices land in it.

    THE BLAST RADIUS OF AN APPROVAL. Clearing a patch for a ring clears it for
    every machine that resolves into that ring, now and at every future scan --
    so this count is the reach of the decision itself rather than of any one
    install. `ring_order` is the rollout position the ring's policies declare
    (test before pilot before broad), which is why this list is not alphabetical.
    """

    ring_name: str
    ring_order: int
    device_count: int


class RingListResponse(BaseModel):
    """Every ring this org has, with `device_total` for the fleet behind them.

    The rings PARTITION the fleet -- every device resolves into exactly one, and
    a device in no group or in a group with no policy lands in the org-default
    ring -- so `sum(device_count) == device_total`. Without the total a count of
    17 says nothing about whether that is most of the estate or a corner of it.
    """

    items: list[RingOut]
    total: int
    device_total: int



# --------------------------------------------------------------------------- #
# The fleet ROLLUPS (docs/modules/patch/view-redesign.md §3)
# --------------------------------------------------------------------------- #


class UpdateRollupOut(BaseModel):
    """One catalogued update, aggregated across the fleet.

    `devices_outstanding` IS THE NUMBER THAT MAKES THE ROW ACTIONABLE, and it
    is the one `GET /patches` could not say: an update missing on one machine
    and one missing on forty rendered identically, because the catalogue route
    returns bare `patches` rows with no per-patch fleet count on them. It is
    the distinct devices carrying this patch in a NON-COMPLIANT state
    (`missing`, `approved` or `failed` -- cleared-but-not-installed and
    tried-and-not-installed are still a patch the machine does not have), so
    it agrees with the compliance percentages by construction.

    `cleared_rings` is the rings this patch is currently approved into, in
    rollout order. It is a property of the row rather than a browser-side join
    against a capped `GET /approvals` page, which is what the console used to
    do -- and had to print a disclaimer saying the column under-reports.
    Revoked approvals are absent: this answers what is IN FORCE.
    """

    id: UUID
    os_family: str
    source: str
    identifier: str
    title: str
    severity: str
    kb: str | None
    cve: list[str] | None
    priority: int | None
    first_seen_at: datetime
    devices_missing: int
    devices_approved: int
    devices_failed: int
    devices_installed: int
    devices_outstanding: int
    cleared_rings: list[str]
    # THE OUTCOME ON ONE MACHINE, and `None` unless this query named a device
    # (`?device_id=`). A device-scoped row without them cannot say why a patch
    # is still outstanding: `failed` and `missing` are both "the machine does
    # not have it", so an apply that errored rendered identically to one that
    # was never dispatched, and the operator's only route to the error string
    # was the CSV export. Fleet-wide these three would be an aggregate across
    # machines with different outcomes, which is not a statement about
    # anything -- so they are NULL there rather than misleading.
    device_state: str | None = None
    device_last_error: str | None = None
    device_applied_at: datetime | None = None


class UpdateRollupListResponse(BaseModel):
    """A page of updates, and everything needed to read the page honestly.

    `total` matches every filter; `total_unfiltered` matches them all EXCEPT
    severity, and `severity_counts` breaks that set down. The three travel
    together because the console defaults to the severities that demand a
    decision, and a filtered count that looks like a total is worse than the
    long list it replaced -- "8 updates" on a machine missing 300 is a lie the
    operator cannot detect from the page alone. `sum(severity_counts) ==
    total_unfiltered` by construction.

    THE TILES ABOVE THE TABLE ARE THESE FIELDS. Counted here rather than in the
    browser: a tile derived from a page of rows is correct only while the page
    is the whole set, and this module has shipped a tile/table disagreement of
    exactly that shape twice.

    `last_scan_at` / `devices_scanned` / `devices_total` are the provenance of
    the zero. An empty page is the same shape on a clean fleet and on one
    nothing has ever scanned.
    """

    items: list[UpdateRollupOut]
    total: int
    total_unfiltered: int
    severity_counts: dict[str, int]
    last_scan_at: datetime | None
    devices_scanned: int
    devices_total: int


class DeviceRollupOut(BaseModel):
    """One device that has something outstanding.

    `outstanding` is a TOTAL and is never severity-filtered, which is the
    guarantee that the update list's severity default hides nothing: a Windows
    machine whose cumulative update Microsoft left unrated still visibly
    carries something here. `worst_severity` is the severest outstanding patch
    on the machine, and `None` only when nothing is outstanding.
    """

    device_id: UUID
    hostname: str
    os_family: str
    outstanding: int
    #: How many of `outstanding` were TRIED AND DID NOT TAKE. A `failed` row is
    #: outstanding -- the machine still lacks the patch -- so without this the
    #: number folds an apply that errored into one that never ran, and the
    #: operator who just dispatched an apply cannot tell the two apart.
    failed: int
    worst_severity: str | None
    counts_by_severity: dict[str, int]
    last_scan_at: datetime | None


class DeviceRollupListResponse(BaseModel):
    """`total` is machines that need something; `devices_total` is the fleet.

    The pair is the 2026-08-20 defect in its other direction. That one read a
    findings list's length as a device count ("66 devices" for one machine
    missing 66 packages); this one would be a device count with no denominator,
    where "1 device needs patches" says nothing about whether that is the whole
    estate or a corner of it.
    """

    items: list[DeviceRollupOut]
    total: int
    last_scan_at: datetime | None
    devices_scanned: int
    devices_total: int


class PatchRunFailureOut(BaseModel):
    """One update that did not install, and the agent's own words for why."""

    source: str | None
    identifier: str | None
    error: str | None


class PatchRunOut(BaseModel):
    """One apply RUN — the record that a dispatch happened and what came back.

    `finished_at is None` means the job is still out; the counts are zero and
    say nothing yet. That is a different state from a finished run that
    installed nothing, and the page must be able to tell them apart, which is
    why the field is exposed rather than folded into a status string here.
    """

    id: UUID
    job_id: UUID
    device_id: UUID
    installed: int
    failed: int
    reboot_required: bool
    started_at: datetime
    finished_at: datetime | None
    failures: list[PatchRunFailureOut]


class PatchRunListResponse(BaseModel):
    """A page of apply runs, newest first.

    Unpaged by design: the route caps `limit` at 50 and this answers "what did
    the last few applies do", not "every apply ever". A cursor here would
    imply a history surface that nothing asks for.
    """

    items: list[PatchRunOut]


class ApplyRequest(BaseModel):
    device_ids: list[UUID] | None = None


class ApplyResponse(BaseModel):
    job_ids: list[UUID]


class PolicyOut(BaseModel):
    id: UUID
    device_group_id: UUID | None
    ring_name: str
    ring_order: int
    maintenance_window: str | None
    auto_approve_security: bool
    reboot: str
    reboot_deferral_hours: int
    scan_interval_hours: int


class PolicyUpsertRequest(BaseModel):
    device_group_id: UUID | None = None
    ring_name: str = "broad"
    ring_order: int = 100
    maintenance_window: str | None = None
    auto_approve_security: bool = False
    # The agent refuses any other value, so storing one would fail every apply.
    reboot: Literal["never", "if_required", "forced"] = "if_required"
    reboot_deferral_hours: int = Field(default=24, ge=0, le=168)
    scan_interval_hours: int = 24


class ExposureOut(BaseModel):
    device_id: UUID
    hostname: str
    patch_id: UUID
    identifier: str
    severity: str
    priority: int | None
    state: str


class FleetReportOut(BaseModel):
    compliant_pct_by_os_family: dict[str, float]
    compliant_pct_by_severity: dict[str, float]
    #: One entry per non-compliant (device, patch) pair. RENAMED from
    #: `exposed_devices` on 2026-08-20: it was always a findings list, its
    #: docstring always said so, and the name was believed anyway -- the
    #: console rendered its length under "Exposed devices", so one machine
    #: missing 66 packages was reported as 66 exposed machines.
    exposures: list[ExposureOut]
    #: Distinct devices, counted server-side. See `reporting.fleet_report`.
    exposed_device_count: int
    critical_device_count: int
    #: WHEN, and OVER HOW MUCH OF THE FLEET, everything above was measured.
    #: `last_scan_at` is `None` until some device in the org has completed a
    #: scan; `devices_scanned`/`devices_total` are what stop a single scanned
    #: machine out of five hundred from dating the whole report as fresh. A
    #: console reading only `exposures == []` cannot tell a clean fleet from
    #: an unmeasured one, which is the defect the 2026-08-21 UX closeout
    #: filed against /reports (nine tiles reading 0 on an unmeasured page).
    last_scan_at: datetime | None
    devices_scanned: int
    devices_total: int


def _patch_out(row: Patch) -> PatchOut:
    return PatchOut(
        id=row.id,
        os_family=row.os_family,
        source=row.source,
        identifier=row.identifier,
        title=row.title,
        severity=row.severity,
        kb=row.kb,
        cve=row.cve,
        superseded_by=row.superseded_by,
        priority=row.priority,
        first_seen_at=row.first_seen_at,
    )


def _approval_out(row: PatchApproval, patch_identifier: str | None) -> ApprovalOut:
    """`patch_identifier` IS A REQUIRED ARGUMENT, not a defaulted one. Every
    route returning `ApprovalOut` has to say where the name came from; a
    default would let one of them quietly answer `null` forever and leave a
    caller unable to tell "unnamed" from "this route never bothered"."""
    return ApprovalOut(
        id=row.id,
        patch_id=row.patch_id,
        patch_identifier=patch_identifier,
        ring_name=row.ring_name,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        revoked=row.revoked,
        revoked_by=row.revoked_by,
        revoked_at=row.revoked_at,
    )


def _policy_out(row: PatchPolicy) -> PolicyOut:
    return PolicyOut(
        id=row.id,
        device_group_id=row.device_group_id,
        ring_name=row.ring_name,
        ring_order=row.ring_order,
        maintenance_window=row.maintenance_window,
        auto_approve_security=row.auto_approve_security,
        reboot=row.reboot,
        reboot_deferral_hours=row.reboot_deferral_hours,
        scan_interval_hours=row.scan_interval_hours,
    )


# --------------------------------------------------------------------------- #
# Catalog + per-device status
# --------------------------------------------------------------------------- #


@router.get("/patches", response_model=PatchListResponse)
async def list_patches(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
    os_family: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    state: str | None = Query(default=None),
    ring: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> PatchListResponse:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        query = select(Patch).where(Patch.org_id == ctx.org_id)
        if os_family:
            query = query.where(Patch.os_family == os_family)
        if severity:
            query = query.where(Patch.severity == severity)
        if state:
            query = query.where(
                select(DevicePatchStatus.id)
                .where(
                    DevicePatchStatus.patch_id == Patch.id,
                    DevicePatchStatus.org_id == ctx.org_id,
                    DevicePatchStatus.state == state,
                )
                .exists()
            )
        if ring:
            query = query.where(
                select(PatchApproval.id)
                .where(
                    PatchApproval.patch_id == Patch.id,
                    PatchApproval.org_id == ctx.org_id,
                    PatchApproval.ring_name == ring,
                    PatchApproval.revoked.is_(False),
                )
                .exists()
            )
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = (
            (await session.execute(query.order_by(Patch.id).limit(limit).offset(offset)))
            .scalars()
            .all()
        )
        return PatchListResponse(items=[_patch_out(r) for r in rows], total=total)



@router.get("/updates", response_model=UpdateRollupListResponse)
async def list_updates(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
    severity: list[str] | None = Query(default=None),
    os_family: str | None = Query(default=None),
    source: str | None = Query(default=None),
    ring: str | None = Query(default=None),
    device_id: UUID | None = Query(default=None),
    q: str | None = Query(default=None),
    outstanding_only: bool = Query(default=True),
    sort: str = Query(default="severity"),
    limit: int = Query(default=UPDATE_PAGE_DEFAULT, ge=1, le=UPDATE_PAGE_MAX),
    offset: int = Query(default=0, ge=0),
    kind: Literal["os", "apps"] | None = Query(default=None),
) -> UpdateRollupListResponse:
    """The fleet update list: ONE ROW PER PATCH, aggregated across devices.

    THE ROUTE THE OPERATOR'S 2026-08-25 REPORT NEEDED. `/patch` rendered one
    row per `(device, patch)` pair above a catalogue with no fleet counts, so
    a single Ubuntu box with 300 upgradable packages was ~600 rows saying one
    thing. Approving is already "a decision about a patch and a ring, not about
    a device" -- `patch_approvals` is `(org_id, patch_id, ring_name)` with no
    device column -- so the list an operator works down is keyed the same way.

    `severity` is REPEATABLE and empty values are dropped, so `?severity=` is
    the URL for "the filter is explicitly cleared" and an absent parameter is
    the same thing. THE SERVER IMPOSES NO SEVERITY DEFAULT: the console has one
    and keeping it there is what stops the API and the evidence export
    inheriting a narrowing that exists to shorten a screen.

    `device_id` makes this one route serve three surfaces -- the fleet list, a
    device row's expansion, and the device detail page's Patch tab -- because a
    per-device patch list is this query with one more predicate.
    """
    severities = frozenset(value for value in (severity or []) if value.strip()) or None
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        page = await update_rollup(
            session,
            ctx.org_id,
            severities=severities,
            os_family=os_family,
            source=source,
            ring=ring,
            device_id=device_id,
            q=q,
            outstanding_only=outstanding_only,
            # An unrecognised sort falls back rather than raising: the sort is
            # a view preference, and a stale bookmark should render the list.
            sort=sort if sort in UPDATE_SORTS else "severity",
            limit=limit,
            offset=offset,
            kind=kind,
        )
    return UpdateRollupListResponse(
        items=[UpdateRollupOut(**row) for row in page["items"]],
        total=page["total"],
        total_unfiltered=page["total_unfiltered"],
        severity_counts=page["severity_counts"],
        last_scan_at=page["last_scan_at"],
        devices_scanned=page["devices_scanned"],
        devices_total=page["devices_total"],
    )


@router.get("/device-rollup", response_model=DeviceRollupListResponse)
async def list_device_rollup(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
    q: str | None = Query(default=None),
    os_family: str | None = Query(default=None),
    limit: int = Query(default=UPDATE_PAGE_DEFAULT, ge=1, le=UPDATE_PAGE_MAX),
    offset: int = Query(default=0, ge=0),
) -> DeviceRollupListResponse:
    """Which machines need attention, worst first -- one row per DEVICE.

    This is what replaced the "Exposed devices" table, which was one row per
    `(device, patch)` pair and therefore printed a hostname once per missing
    package. It is deliberately NOT severity-filterable: the update list above
    narrows to the severities that demand a decision, and this panel is the
    guarantee that the narrowing hides no machine.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        page = await device_rollup(
            session, ctx.org_id, q=q, os_family=os_family, limit=limit, offset=offset
        )
    return DeviceRollupListResponse(
        items=[DeviceRollupOut(**row) for row in page["items"]],
        total=page["total"],
        last_scan_at=page["last_scan_at"],
        devices_scanned=page["devices_scanned"],
        devices_total=page["devices_total"],
    )


@router.get("/runs", response_model=PatchRunListResponse)
async def list_runs(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
    device_id: UUID | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
) -> PatchRunListResponse:
    """What the last apply jobs DID — the route `patch_runs` never had.

    The table has been written on every dispatch and finalized on every result
    since the module shipped, and nothing read it, so "I applied the updates,
    did they install?" was answerable only from the job engine's raw result
    JSON. `DevicePatchStatus` holds the durable per-patch end state; this holds
    the event.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await recent_runs(session, ctx.org_id, device_id=device_id, limit=limit)
    return PatchRunListResponse(items=[PatchRunOut(**row) for row in rows])


@router.get("/app-sources", response_model=AppSourceSummaryResponse)
async def list_app_sources(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
    os_family: str | None = Query(default=None),
) -> AppSourceSummaryResponse:
    """Whether winget / Homebrew could be scanned, counted over each device's
    latest completed scan."""
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await app_source_summary(session, ctx.org_id, os_family)
    return AppSourceSummaryResponse(items=[AppSourceSummaryOut(**r) for r in rows])


@router.get("/restarts", response_model=RestartListResponse)
async def list_restarts(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
    limit: int = Query(default=200, ge=1, le=500),
) -> RestartListResponse:
    """Devices still owed a restart after patching, oldest first."""
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await open_restarts(session, ctx.org_id, limit)
    return RestartListResponse(items=[RestartListItem(**r) for r in rows], total=len(rows))


@router.get("/devices/{device_id}/status", response_model=DeviceStatusOut)
async def device_status(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
) -> DeviceStatusOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one_or_none()
        if device is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device"))
        report = await device_report(session, ctx.org_id, device_id)
        window = await restart_window(session, ctx.org_id, device_id, datetime.now(UTC))
        return DeviceStatusOut(**report, restart_window=RestartWindowOut(**asdict(window)))


@router.post(
    "/devices/{device_id}/restart",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RestartRequestOut,
)
async def request_restart(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.apply")),
) -> RestartRequestOut:
    """Book a restart for the device's next maintenance window.

    Sends nothing itself: the restart sweep dispatches the signed
    `patch.reboot` inside the window, with the policy's notice for a signed-in
    user. Asking again while one is booked changes nothing (202 either way).
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        device = (
            await session.execute(
                select(Device.id).where(Device.org_id == ctx.org_id, Device.id == device_id)
            )
        ).scalar_one_or_none()
        if device is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device"))
        try:
            row, existed = await request_operator_restart(
                session, org_id=ctx.org_id, device_id=device_id, actor_id=ctx.user_id
            )
        except RestartUnavailable as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        window = await restart_window(session, ctx.org_id, device_id, datetime.now(UTC))
        return RestartRequestOut(
            restart=RestartOut(**restart_row(row)),
            already_requested=existed,
            window=RestartWindowOut(**asdict(window)),
        )


# --------------------------------------------------------------------------- #
# POST /scan
# --------------------------------------------------------------------------- #


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED, response_model=ScanResponse)
async def trigger_scan(
    body: ScanRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.scan")),
) -> ScanResponse:
    state = request.app.state
    job_ids: list[UUID] = []
    pending: list[tuple[UUID, dict[str, Any]]] = []
    async with org_session(ctx.org_id, engine=state.app_engine) as session:
        query = select(Device.id).where(Device.org_id == ctx.org_id)
        if body.device_ids:
            query = query.where(Device.id.in_(body.device_ids))
        else:
            # "Every device" means every APPROVED device: one machine waiting
            # for review (or rejected and being removed) must not turn the
            # whole scan into the gate's 409. A device NAMED in the request
            # still meets that 409 (final review I2; plan D4).
            query = query.where(Device.review_state == "approved")
        device_ids = (await session.execute(query)).scalars().all()

        for device_id in device_ids:
            job = await state.job_engine.create(
                session,
                org_id=ctx.org_id,
                type="patch.scan",
                target_device_id=device_id,
                params=PatchScanParams().model_dump(),
                requested_by=ctx.user_id,
            )
            envelope = await state.job_engine.signed_envelope(job)
            pending.append((device_id, envelope))
            job_ids.append(job.id)
            await audit_service.record(
                session,
                org_id=ctx.org_id,
                actor_type="user",
                actor_id=ctx.user_id,
                action="patch.scan_dispatched",
                target_type="device",
                target_id=str(device_id),
                metadata={"job_id": str(job.id)},
            )
    # Transaction committed: safe to publish the signed jobs to the agents.
    for device_id, envelope in pending:
        await publish_job(state.redis, device_id, envelope)
    return ScanResponse(job_ids=job_ids)


# --------------------------------------------------------------------------- #
# GET /approvals, POST /approvals, DELETE /approvals/{id}
# --------------------------------------------------------------------------- #


@router.get("/approvals", response_model=ApprovalListResponse)
async def list_approvals_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
    patch_id: UUID | None = Query(default=None),
    ring: str | None = Query(default=None),
    revoked: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ApprovalListResponse:
    """List this org's approvals. `patch.read`, not `patch.approve`: reading
    which patches are cleared for which ring is read work, and the same
    technician who may see this list is still refused the DELETE below.

    Without this route `DELETE /approvals/{approval_id}` was unreachable —
    an approval id was returned only by the POST that created it, so an
    approval made in an earlier session could never be revoked over HTTP.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows, total = await list_approvals(
            session,
            org_id=ctx.org_id,
            patch_id=patch_id,
            ring_name=ring,
            revoked=revoked,
            limit=limit,
            offset=offset,
        )
        return ApprovalListResponse(
            items=[_approval_out(r.approval, r.patch_identifier) for r in rows],
            total=total,
        )


@router.post(
    "/approvals", status_code=status.HTTP_201_CREATED, response_model=ApprovalOut
)
async def create_approval(
    body: ApprovalCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.approve")),
) -> ApprovalOut:
    outcomes = await _approve_with_change(
        request, ctx, patch_ids=[body.patch_id], ring_name=body.ring_name, change_id=body.change_id
    )
    return outcomes[0][1]


@router.post(
    "/approvals/bulk",
    status_code=status.HTTP_201_CREATED,
    response_model=BulkApprovalResponse,
)
async def create_approvals_bulk(
    body: BulkApprovalCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.approve")),
) -> BulkApprovalResponse:
    """Clear a selection of patches for one ring, in one decision.

    SAME GATE AND SAME TRAIL AS THE SINGLE POST. `patch.approve`, tenant-scoped,
    and ONE `patch.approved` audit event per approval -- never one for the batch,
    which would stop the log answering "who cleared this patch". Every event
    commits in the same transaction as the row it describes.

    A patch id that is not this org's refuses the WHOLE batch with 404. A
    partly-applied batch is a set of clearances the operator never consciously
    granted, and finding which ones landed would mean diffing the audit log
    against their own selection.
    """
    outcomes = await _approve_with_change(
        request, ctx, patch_ids=body.patch_ids, ring_name=body.ring_name, change_id=body.change_id
    )
    items = [out for _, out in outcomes]
    created = sum(1 for outcome, _ in outcomes if outcome.created)
    return BulkApprovalResponse(
        items=items,
        total=len(items),
        newly_approved=created,
        already_in_force=len(items) - created,
    )


async def _approve_with_change(
    request: Request,
    ctx: AuthContext,
    *,
    patch_ids: list[UUID],
    ring_name: str,
    change_id: UUID | None,
) -> list[tuple[ApprovalOutcome, ApprovalOut]]:
    """Approve under the org's change policy.

    With `change_id`, the change must be approved or being implemented, and
    every approval is linked to it. Without one, a change gate covering the
    ring's machines refuses (409, audited, committed); with no gate, an `auto`
    change record is written for the new approvals so the evidence exists.
    """
    refusal: str | None = None
    result: list[tuple[ApprovalOutcome, ApprovalOut]] = []
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            change = (
                await authorizing_change(session, ctx.org_id, change_id)
                if change_id is not None
                else None
            )
            if change is None:
                ring = await devices_in_ring(session, ctx.org_id, ring_name)
                refusal = await patch_approval_gate(session, ctx.org_id, ring, ring_name)
            if refusal is not None:
                await record_gate_refusal(
                    session,
                    org_id=ctx.org_id,
                    actor_id=ctx.user_id,
                    target_type="patch_ring",
                    target_id=ring_name,
                    reason=refusal,
                    metadata={"patch_ids": [str(p) for p in patch_ids]},
                )
            else:
                outcomes = await approve_patches(
                    session,
                    org_id=ctx.org_id,
                    actor_id=ctx.user_id,
                    patch_ids=patch_ids,
                    ring_name=ring_name,
                )
                approval_ids = [o.approval.id for o in outcomes]
                if change is not None:
                    await link_actions(
                        session,
                        org_id=ctx.org_id,
                        actor_id=ctx.user_id,
                        change=change,
                        action_type="patch_approval",
                        action_ids=approval_ids,
                    )
                elif any(o.created for o in outcomes):
                    new = [o.approval.id for o in outcomes if o.created]
                    await record_auto_change(
                        session,
                        org_id=ctx.org_id,
                        actor_id=ctx.user_id,
                        title=f"Patch approval: {len(new)} update(s) for the {ring_name} ring",
                        description=(
                            "Recorded automatically: updates were approved in the patch "
                            "console without a change request."
                        ),
                        affected=[{"kind": "ring", "name": ring_name}],
                        action_type="patch_approval",
                        action_ids=new,
                    )
                names = await patch_identifiers(
                    session,
                    org_id=ctx.org_id,
                    patch_ids=[o.approval.patch_id for o in outcomes],
                )
                result = [
                    (o, _approval_out(o.approval, names.get(o.approval.patch_id)))
                    for o in outcomes
                ]
        except UnknownPatchError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                tr(
                    "{count} of the patches named are not in this organisation",
                    count=len(exc.patch_ids),
                )
                if len(patch_ids) > 1
                else tr("no such patch in this organisation"),
            ) from exc
        except ChangeNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such change")) from None
        except ChangeValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    if refusal is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, gate_said(refusal))
    return result


@router.delete("/approvals/{approval_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_approval(
    approval_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.approve")),
) -> None:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        revoked = await revoke_approval(
            session, org_id=ctx.org_id, actor_id=ctx.user_id, approval_id=approval_id
        )
        if not revoked:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such approval"))


# --------------------------------------------------------------------------- #
# POST /apply
# --------------------------------------------------------------------------- #


@router.post("/apply", response_model=ApplyResponse)
async def apply_patches(
    body: ApplyRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.apply")),
) -> ApplyResponse:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine) as session:
        pending = await dispatch_apply(
            session,
            state.job_engine,
            org_id=ctx.org_id,
            now=datetime.now(UTC),
            device_ids=set(body.device_ids) if body.device_ids else None,
            actor_id=ctx.user_id,
            actor_type="user",
        )
    # Transaction committed: safe to publish the signed jobs to the agents.
    for _job_id, device_id, envelope in pending:
        await publish_job(state.redis, device_id, envelope)
    return ApplyResponse(job_ids=[job_id for job_id, _, _ in pending])


# --------------------------------------------------------------------------- #
# GET|PUT /policies
# --------------------------------------------------------------------------- #


@router.get("/rings", response_model=RingListResponse)
async def list_rings(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
) -> RingListResponse:
    """Every deployment ring in this org, with the devices each one reaches.

    THE NUMBER THAT HAS TO BE ON SCREEN BEFORE AN APPROVAL, not after it.
    "Approve 42 updates for pilot" is unreadable without knowing what "pilot"
    means in machines, and that is not derivable in a browser: it wants
    `patch_policies` joined to `devices` through `device_groups`, with the
    org-default fallback -- which is `resolve_patch_policy`'s job and is done
    here by `resolve_ring_populations`, the same resolver approval itself uses,
    so the number shown and the machines flipped cannot disagree.

    `patch.read`, not `patch.approve`: which ring a machine is in is read work,
    and the technician who may see it is still refused the approval it informs.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        populations = await resolve_ring_populations(session, ctx.org_id)
    items = [
        RingOut(
            ring_name=population.ring_name,
            ring_order=population.ring_order,
            device_count=len(population.device_ids),
        )
        for population in populations
    ]
    return RingListResponse(
        items=items,
        total=len(items),
        device_total=sum(item.device_count for item in items),
    )


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
) -> list[PolicyOut]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = (
            (
                await session.execute(
                    select(PatchPolicy)
                    .where(PatchPolicy.org_id == ctx.org_id)
                    .order_by(PatchPolicy.id)
                )
            )
            .scalars()
            .all()
        )
        return [_policy_out(r) for r in rows]


@router.put("/policies", response_model=PolicyOut)
async def upsert_policy(
    body: PolicyUpsertRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.policy.manage")),
) -> PolicyOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        # As in `remote`'s upsert_policy: for the org-default (device_group_id
        # IS NULL), Postgres's UNIQUE allows duplicate NULLs, so we deterministically
        # take the first existing default rather than risk inserting a second.
        query = select(PatchPolicy).where(PatchPolicy.org_id == ctx.org_id)
        if body.device_group_id is None:
            query = query.where(PatchPolicy.device_group_id.is_(None))
        else:
            query = query.where(PatchPolicy.device_group_id == body.device_group_id)
        row = (
            await session.execute(query.order_by(PatchPolicy.id).limit(1))
        ).scalar_one_or_none()

        created = row is None
        if row is None:
            row = PatchPolicy(org_id=ctx.org_id, device_group_id=body.device_group_id)
            session.add(row)
        row.ring_name = body.ring_name
        row.ring_order = body.ring_order
        row.maintenance_window = body.maintenance_window
        row.auto_approve_security = body.auto_approve_security
        row.reboot = body.reboot
        row.reboot_deferral_hours = body.reboot_deferral_hours
        row.scan_interval_hours = body.scan_interval_hours
        await session.flush()

        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="patch.policy.changed",
            target_type="patch_policy",
            target_id=str(row.id),
            metadata={
                "device_group_id": (
                    str(body.device_group_id) if body.device_group_id else None
                ),
                "created": created,
            },
        )
        return _policy_out(row)


# --------------------------------------------------------------------------- #
# GET /report, GET /report/export
# --------------------------------------------------------------------------- #


@router.get("/report", response_model=FleetReportOut)
async def report(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
) -> FleetReportOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        data = await fleet_report(session, ctx.org_id)
        return FleetReportOut(**data)


_EXPORT_COLUMNS = (
    "device_id",
    "hostname",
    "os_family",
    "patch_id",
    "identifier",
    "severity",
    "kb",
    "cve",
    "source",
    "priority",
    "state",
    "detected_at",
    "applied_at",
    "last_error",
)

# CSV formula-injection escaping (`app.core.csv_export.escape_formula`) is
# applied per column, not per row. Escaped: `hostname` (the device's own
# free-text name), `identifier`/`kb`/`cve`/`source` (all plain, unconstrained
# strings straight off the vendor/OS patch feed the AGENT reported --
# `app.modules.patch.schema.PatchScanDoc`: `identifier: str`, `kb: str |
# None`, `cve: list[str] | None`, `source: str`, none `Literal`-constrained,
# so any of them can carry attacker-shaped text from a compromised feed or
# device), and `last_error` (a free-form error string the agent reports on a
# failed apply). NOT escaped: `device_id`/`patch_id` (UUIDs), `os_family`
# (pattern/`Literal`-constrained to `windows|linux|macos`), `severity`
# (`Literal["critical","important","moderate","low","unknown"]`), `priority`
# (int), `state` (`DevicePatchStatus.state` is set from a fixed internal
# vocabulary -- `missing`/`approved`/`installed`/`failed` -- never raw agent
# text), and the two `*_at` timestamps this file itself isoformats -- none
# of those can carry a formula payload, so escaping them would only risk
# corrupting a value that was never free text.
_EXPORT_ESCAPED_COLUMNS: frozenset[str] = frozenset(
    {"hostname", "identifier", "kb", "cve", "source", "last_error"}
)


def _csv_cell(col: str, value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        value = ",".join(value)
    if col in _EXPORT_ESCAPED_COLUMNS:
        return escape_formula(value)
    return value


async def _export_csv_lines(rows: list[dict[str, Any]]) -> AsyncIterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_EXPORT_COLUMNS)
    yield buffer.getvalue()
    for row in rows:
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(_csv_cell(col, row[col]) for col in _EXPORT_COLUMNS)
        yield buffer.getvalue()


@router.get("/report/export")
async def report_export(
    request: Request,
    ctx: AuthContext = Depends(require_permission("patch.read")),
) -> StreamingResponse:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await export_rows(session, ctx.org_id)
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="patch.report_exported",
            target_type="organization",
            target_id=str(ctx.org_id),
            metadata={"row_count": len(rows)},
        )
    return StreamingResponse(
        _export_csv_lines(rows),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="patch-compliance.csv"'},
    )
