"""Maintenance-window matching + signed `patch.apply` dispatch
(docs/modules/patch/spec.md).

This is the module's application-security gate: approval (Task 6,
``app.modules.patch.approve``) is what decides a patch is *allowed* to reach a
ring; this module decides which approved, un-revoked, in-window patches are
actually shipped to a device right now, and builds the signed job.

The gate, precisely (see Task 6 review): a ``(device, patch)`` pair is
dispatched only when ALL of:

1. An UN-REVOKED ``patch_approvals`` row exists for
   ``(org_id, patch_id, ring_name == device's resolved ring)`` whose owner is
   the device's client or every client (``client_id IS NULL``; MSP clients
   plan 2026-10-26 Task 14). This is re-checked here, keyed off the approval
   table itself — never inferred solely from the `device_patch_status`
   snapshot, which can go stale (a device rescanned after its ring was
   approved may sit at `missing` while still being legitimately dispatchable;
   see point 2). THE CLIENT PREDICATE IS EXPLICIT because the window sweep
   runs under scope "all", where RLS would let X's approval clear Y's machine.
2. The device's `device_patch_status.state` for that patch is `"approved"` or
   `"missing"` — never `"installed"`, `"failed"`, or `"not_applicable"`.
3. `in_window(resolved_policy.maintenance_window, now)` is true for the
   device's resolved patch policy (each painted hour is open for the whole
   hour).
4. The device is in `device_ids`, if given.

All of a device's eligible patches are batched into ONE `patch.apply` job.
"""

from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.schedule import in_window, window_slot_end
from app.jobs.canonical import rfc3339
from app.models import Device, DevicePatchStatus, Job, Patch, PatchApproval, PatchRun
from app.modules.change_mgmt.gates import (
    Gates,
    gates_for,
    has_authorizing_change,
    record_gate_refusal,
)
from app.modules.patch.ingest import parse_wire_time
from app.modules.patch.policy import resolve_patch_policy
from app.modules.patch.reboot import request_reboot
from app.modules.patch.schema import PatchApplyParams, PatchApplyResult

_ELIGIBLE_STATUS_STATES = {"approved", "missing"}


class _JobEngineLike(Protocol):
    async def create(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        type: str,
        target_device_id: UUID,
        params: dict[str, Any],
        requested_by: UUID | None = None,
        ttl_s: int | None = None,
    ) -> Any: ...

    async def signed_envelope(self, job: Any) -> dict[str, Any]: ...


def _approves_client(client_id: UUID) -> ColumnElement[bool]:
    """An approval that clears `client_id`'s machines: that client's own, or
    a global one (every client's)."""
    return or_(PatchApproval.client_id.is_(None), PatchApproval.client_id == client_id)


async def _ring_approved_patch_ids(
    session: AsyncSession,
    *,
    org_id: UUID,
    ring_name: str,
    patch_ids: set[UUID],
    client_id: UUID,
) -> set[UUID]:
    """The patches cleared for `ring_name` FOR A MACHINE OF `client_id`: by
    an approval of that client or a global one -- never another client's."""
    if not patch_ids:
        return set()
    rows = (
        await session.execute(
            select(PatchApproval.patch_id).where(
                PatchApproval.org_id == org_id,
                PatchApproval.ring_name == ring_name,
                PatchApproval.revoked.is_(False),
                PatchApproval.patch_id.in_(patch_ids),
                _approves_client(client_id),
            )
        )
    ).scalars().all()
    return set(rows)


def _not_after(window: str | None, now: datetime) -> str | None:
    end = window_slot_end(window, now)
    return rfc3339(end) if end is not None else None


async def _change_gate(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    client_id: UUID,
    ring_name: str,
    approved_ids: set[UUID],
    gates: Gates,
    actor_id: UUID | None,
) -> set[UUID]:
    """The approved patches a change gate lets through to this device (of
    `client_id`): all of them when no gate covers its group, otherwise those
    one of whose approvals -- the client's own or a global one -- is linked
    to a change that is approved or being implemented, OF THE APPROVAL'S
    CLIENT (C8): X's approval by a change of X, a global approval by a change
    of the internal client, whose id is the org's. A link to any other
    client's change -- the legacy shape migration 0237 left, every change
    filed under the internal client -- authorizes nothing. A withheld patch
    is audited `change.gate_refused` under the device's client."""
    group_id = (
        await session.execute(
            select(Device.device_group_id).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    if not approved_ids or not gates.applies_to_group(group_id):
        return approved_ids
    approvals = (
        await session.execute(
            select(PatchApproval.patch_id, PatchApproval.id, PatchApproval.client_id).where(
                PatchApproval.org_id == org_id,
                PatchApproval.ring_name == ring_name,
                PatchApproval.revoked.is_(False),
                PatchApproval.patch_id.in_(approved_ids),
                _approves_client(client_id),
            )
        )
    ).all()
    allowed = {
        patch_id
        for patch_id, approval_id, owner in approvals
        if await has_authorizing_change(
            session,
            org_id,
            "patch_approval",
            approval_id,
            client_id=org_id if owner is None else owner,
        )
    }
    withheld = approved_ids - allowed
    if withheld:
        await record_gate_refusal(
            session,
            org_id=org_id,
            actor_id=actor_id,
            target_type="device",
            target_id=str(device_id),
            reason=(
                f"{len(withheld)} approved update(s) were not installed: this device's group "
                "needs an approved change linked to the approval"
            ),
            metadata={"patch_ids": sorted(str(p) for p in withheld)},
            # The refused act is this device's install: its client's.
            client_id=client_id,
        )
    return allowed


async def dispatch_apply(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    *,
    org_id: UUID,
    now: datetime,
    device_ids: set[UUID] | None = None,
    actor_id: UUID | None = None,
    actor_type: Literal["user", "agent", "system"] = "system",
) -> list[tuple[UUID, UUID, dict[str, Any]]]:
    """Build one signed `patch.apply` job per eligible device.

    Does NOT publish the jobs to Redis — publishing before the caller's
    tenant transaction commits could hand an agent a signed job with no
    durable server record if the commit later failed. Instead, returns the
    pending publishes as `(job_id, device_id, envelope)` triples; the CALLER
    must publish each one via `app.gateway.dispatch.publish_job` only AFTER
    its `org_session` block exits (i.e. after commit) — mirrors
    `app.modules.patch.api`'s `POST /scan` idiom.

    The pending list is empty for a device outside its maintenance window, or
    with no patch left eligible after the approval re-check.

    `actor_id`/`actor_type` attribute the `patch.apply_dispatched` audit event.
    Default is `actor_type="system"` (the worker's window-apply sweep); a
    human-initiated `POST /apply` passes `actor_id=<user>, actor_type="user"`
    so the immutable audit log records who deployed the patch.
    """
    status_query = select(DevicePatchStatus, Patch).join(
        Patch, Patch.id == DevicePatchStatus.patch_id
    ).where(
        DevicePatchStatus.org_id == org_id,
        Patch.org_id == org_id,
        DevicePatchStatus.state.in_(_ELIGIBLE_STATUS_STATES),
    )
    if device_ids is not None:
        status_query = status_query.where(DevicePatchStatus.device_id.in_(device_ids))

    rows = (await session.execute(status_query)).all()

    by_device: dict[UUID, list[tuple[DevicePatchStatus, Patch]]] = {}
    for status, patch in rows:
        by_device.setdefault(status.device_id, []).append((status, patch))

    # A device meets ITS CLIENT'S change gates (MSP clients plan 2026-10-26
    # Task 7, D4): the clients are read once for the sweep, and each client's
    # gates once, however many of its devices are due.
    device_clients: dict[UUID, UUID] = dict(
        (
            await session.execute(
                select(Device.id, Device.client_id).where(
                    Device.org_id == org_id, Device.id.in_(list(by_device))
                )
            )
        )
        .tuples()
        .all()
    )
    gates_by_client: dict[UUID, Gates] = {}
    pending: list[tuple[UUID, UUID, dict[str, Any]]] = []
    for device_id, status_patches in by_device.items():
        policy = await resolve_patch_policy(session, org_id, device_id)
        if not in_window(policy.maintenance_window, now):
            continue
        client_id = device_clients.get(device_id)
        if client_id is None:
            continue  # gone, or invisible to this session: nothing to send it
        gates = gates_by_client.get(client_id)
        if gates is None:
            gates = await gates_for(session, org_id, "patches", client_id=client_id)
            gates_by_client[client_id] = gates

        candidate_patch_ids = {patch.id for _, patch in status_patches}
        approved_ids = await _ring_approved_patch_ids(
            session,
            org_id=org_id,
            ring_name=policy.ring_name,
            patch_ids=candidate_patch_ids,
            client_id=client_id,
        )
        if gates.any:
            approved_ids = await _change_gate(
                session,
                org_id=org_id,
                device_id=device_id,
                client_id=client_id,
                ring_name=policy.ring_name,
                approved_ids=approved_ids,
                gates=gates,
                actor_id=actor_id,
            )
        eligible = [
            (status, patch) for status, patch in status_patches if patch.id in approved_ids
        ]
        if not eligible:
            continue

        params = PatchApplyParams(
            patches=[
                {"source": patch.source, "identifier": patch.identifier} for _, patch in eligible
            ],
            reboot=policy.reboot,
            deferral_hours=policy.reboot_deferral_hours,
            reboot_not_after=_not_after(policy.maintenance_window, now),
        )
        job = await job_engine.create(
            session,
            org_id=org_id,
            type="patch.apply",
            target_device_id=device_id,
            params=params.model_dump(),
            requested_by=None,
        )
        envelope = await job_engine.signed_envelope(job)
        pending.append((job.id, device_id, envelope))

        session.add(PatchRun(org_id=org_id, job_id=job.id, device_id=device_id))

        await audit_service.record(
            session,
            org_id=org_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="patch.apply_dispatched",
            target_type="device",
            target_id=str(device_id),
            metadata={"patch_count": len(eligible), "job_id": str(job.id)},
            client_id=client_id,
        )

    await session.flush()
    return pending


async def ingest_apply_result(session: AsyncSession, job: Job, result: PatchApplyResult) -> None:
    """Reconcile a completed `patch.apply` job's result (docs/modules/patch/spec.md).

    Per-outcome: resolve the catalog `Patch` for `(org_id, source, identifier)`
    and flip the device's `DevicePatchStatus` to `installed` or `failed`.
    An outcome for a patch not in this org's catalog is skipped gracefully
    (mirrors the `installed_identifiers` lookup style in `ingest.py`) rather
    than raising — the agent's report should never be able to fail the whole
    job over a catalog that hasn't caught up.

    Then finalizes the `PatchRun` row created at dispatch (Task 7; exactly one
    per apply job) and records a single `patch.applied` audit event summarizing
    the outcome counts.
    """
    now = datetime.now(UTC)
    org_id = job.org_id
    device_id = job.target_id

    installed_count = 0
    failed_count = 0

    for outcome in result.results:
        patch = (
            await session.execute(
                select(Patch).where(
                    Patch.org_id == org_id,
                    Patch.source == outcome.source,
                    Patch.identifier == outcome.identifier,
                )
            )
        ).scalar_one_or_none()
        if patch is None:
            continue  # not in the catalog; nothing to reconcile

        status = (
            await session.execute(
                select(DevicePatchStatus).where(
                    DevicePatchStatus.org_id == org_id,
                    DevicePatchStatus.device_id == device_id,
                    DevicePatchStatus.patch_id == patch.id,
                )
            )
        ).scalar_one_or_none()
        if status is None:
            continue  # no status row for this device/patch pair; nothing to update

        if outcome.state == "installed":
            status.state = "installed"
            status.applied_at = now
            status.last_error = None
            installed_count += 1
        else:
            status.state = "failed"
            status.last_error = outcome.error
            failed_count += 1

    run = (
        await session.execute(
            select(PatchRun).where(
                PatchRun.org_id == org_id,
                PatchRun.job_id == job.id,
            )
        )
    ).scalar_one_or_none()
    if run is not None:
        run.results = result.model_dump()
        run.reboot_required = result.reboot_required
        run.finished_at = now

    await _record_reboot(session, job, result, now)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="patch.applied",
        target_type="device",
        target_id=str(device_id),
        metadata={
            "installed": installed_count,
            "failed": failed_count,
            "reboot_required": result.reboot_required,
            "reboot_scheduled_for": result.reboot_scheduled_for,
        },
    )

    await session.flush()


async def _record_reboot(
    session: AsyncSession, job: Job, result: PatchApplyResult, now: datetime
) -> None:
    """A restart the agent booked is recorded as scheduled; one it could not
    book, under a policy that restarts, waits for the window."""
    scheduled_for = parse_wire_time(result.reboot_scheduled_for)
    if scheduled_for is None and not result.reboot_required:
        return
    policy = await resolve_patch_policy(session, job.org_id, job.target_id)
    if scheduled_for is None and policy.reboot == "never":
        return
    await request_reboot(
        session,
        org_id=job.org_id,
        device_id=job.target_id,
        origin="apply",
        now=now,
        apply_job_id=job.id,
        scheduled_for=scheduled_for,
        user_present=result.user_present,
    )
