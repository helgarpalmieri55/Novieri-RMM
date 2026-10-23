"""Restart orchestration after patching (docs/modules/patch/spec.md §8.1).

The agent schedules a restart itself inside `patch.apply` when the policy asks
for one and the control plane announced `result_version` 2. Everything else
lands here as a `patch_reboots` row:

- an apply that left the OS wanting a restart without booking one (an older
  agent, a restart that would have run past the window end, an OS error);
- a scan that finds a restart pending (`reboot_required`);
- an operator asking for one from the device page (`request_operator_restart`).

`run_reboot_sweep` dispatches a signed `patch.reboot` for each pending row
whose device's maintenance window is open, and only then: servers restart
inside their window, and a signed-in laptop user gets the policy's deferral as
notice. A later scan whose `booted_at` is after the row closes it. Every
transition is audited.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.schedule import in_window, next_window_start, window_slot_end
from app.jobs.canonical import rfc3339
from app.models import Agent, Device, Job, JobStatus, PatchReboot
from app.modules.patch.policy import ResolvedPatchPolicy, resolve_patch_policy
from app.modules.patch.schema import PatchRebootParams, PatchRebootResult, PatchScanParams
from app.modules.patch.words import (
    REASON_AGENT_CANNOT_RESTART,
    REASON_DEVICE_RESTARTED,
    REASON_OS_NO_LONGER_NEEDS,
    REASON_POLICY_NEVER_RESTARTS,
)

OPEN_STATES = ("pending", "dispatched", "scheduled")
# The agent's bounds on a signed-in user's notice (agent/internal/capabilities/
# patch/reboot.go: minUserWarning, maxUserWarning).
_MIN_WARNING_MINUTES = 15
_MAX_WARNING_MINUTES = 7 * 24 * 60
# A dispatched restart whose job failed is retried this many times.
MAX_ATTEMPTS = 3
# How long after the booked time before a confirming scan is requested.
VERIFY_AFTER = timedelta(minutes=15)
_FAILED_JOB = (JobStatus.failed, JobStatus.expired, JobStatus.refused, JobStatus.cancelled)


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


async def open_reboot(session: AsyncSession, org_id: UUID, device_id: UUID) -> PatchReboot | None:
    return (
        await session.execute(
            select(PatchReboot).where(
                PatchReboot.org_id == org_id,
                PatchReboot.device_id == device_id,
                PatchReboot.state.in_(OPEN_STATES),
            )
        )
    ).scalar_one_or_none()


async def _audit(
    session: AsyncSession, row: PatchReboot, action: str, **metadata: Any
) -> None:
    await audit_service.record(
        session,
        org_id=row.org_id,
        actor_type="system",
        actor_id=None,
        action=action,
        target_type="device",
        target_id=str(row.device_id),
        metadata={"reboot_id": str(row.id), **metadata},
    )


async def _close(
    session: AsyncSession, row: PatchReboot, state: str, reason: str, now: datetime
) -> None:
    row.state, row.reason, row.closed_at = state, reason, now
    await session.flush()
    await _audit(session, row, f"patch.reboot_{state}", reason=reason)


async def request_reboot(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    origin: str,
    now: datetime,
    apply_job_id: UUID | None = None,
    scheduled_for: datetime | None = None,
    user_present: bool | None = None,
) -> PatchReboot:
    """Record that `device_id` owes a restart. Reuses the open row if there is
    one. With `scheduled_for`, the OS already has the restart booked."""
    row = await open_reboot(session, org_id, device_id)
    created = row is None
    if row is None:
        row = PatchReboot(
            org_id=org_id,
            device_id=device_id,
            origin=origin,
            state="pending",
            apply_job_id=apply_job_id,
            attempts=0,
        )
        session.add(row)
        await session.flush()
    if scheduled_for is not None:
        row.state, row.scheduled_for, row.user_present = "scheduled", scheduled_for, user_present
        await session.flush()
        await _audit(
            session,
            row,
            "patch.reboot_scheduled",
            scheduled_for=rfc3339(scheduled_for),
            user_present=user_present,
            by="patch.apply",
        )
    elif created:
        await _audit(session, row, "patch.reboot_pending", origin=origin)
    return row


class RestartUnavailable(Exception):
    """The device cannot be restarted through the platform. The message says
    why, in words an operator can act on."""


@dataclass(frozen=True)
class RestartWindow:
    """When the sweep would send a restart, and the notice a signed-in user
    gets. `opens_at` is None when the window does not open within eight days."""

    open_now: bool
    opens_at: datetime | None
    user_warning_minutes: int


async def restart_window(
    session: AsyncSession, org_id: UUID, device_id: UUID, now: datetime
) -> RestartWindow:
    policy = await resolve_patch_policy(session, org_id, device_id)
    opens_at = next_window_start(policy.maintenance_window, now)
    warning = policy.reboot_deferral_hours * 60
    return RestartWindow(
        open_now=opens_at == now,
        opens_at=opens_at,
        user_warning_minutes=min(max(warning, _MIN_WARNING_MINUTES), _MAX_WARNING_MINUTES),
    )


async def request_operator_restart(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    actor_id: UUID,
) -> tuple[PatchReboot, bool]:
    """Book a restart an operator asked for; the sweep sends it in the window.

    Returns the open row and whether one was already open. Asking again is a
    no-op, except that a restart still waiting for the window becomes the
    operator's, so a scan that finds none pending cannot cancel it.
    """
    agent = (
        await session.execute(
            select(Agent).where(Agent.org_id == org_id, Agent.device_id == device_id)
        )
    ).scalar_one_or_none()
    if agent is None or agent.cert_revoked:
        raise RestartUnavailable(
            tr("this device has no agent, so it cannot be restarted from here")
        )
    review_state = (
        await session.execute(
            select(Device.review_state).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    if review_state != "approved":
        raise RestartUnavailable(
            tr("this computer is waiting for review; approve it before restarting it from here")
        )
    capabilities = agent.capabilities
    if not (isinstance(capabilities, dict) and capabilities.get("patch.reboot") is True):
        raise RestartUnavailable(
            tr("this device's agent cannot restart it; update the agent and try again")
        )

    row = await open_reboot(session, org_id, device_id)
    existed = row is not None
    if row is None:
        row = PatchReboot(
            org_id=org_id, device_id=device_id, origin="operator", state="pending", attempts=0
        )
        session.add(row)
    elif row.origin == "operator" or row.state != "pending":
        return row, True
    else:
        row.origin = "operator"
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="patch.restart_requested",
        target_type="device",
        target_id=str(device_id),
        metadata={"reboot_id": str(row.id), "joined_existing": existed},
    )
    return row, existed


async def note_scan(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    reboot_required: bool | None,
    booted_at: datetime | None,
    now: datetime,
) -> None:
    """Reconcile a scan against the device's open restart, if any.

    A boot time after the row closes it: the machine restarted, whoever did it.
    A scan that finds a restart pending with no row opens one, unless the
    policy never restarts.
    """
    row = await open_reboot(session, org_id, device_id)
    if row is not None:
        if booted_at is not None and booted_at > row.created_at:
            await _close(session, row, "completed", REASON_DEVICE_RESTARTED, now)
            return
        if reboot_required is False and row.state == "pending" and row.origin == "scan":
            await _close(session, row, "cancelled", REASON_OS_NO_LONGER_NEEDS, now)
        return
    if reboot_required:
        policy = await resolve_patch_policy(session, org_id, device_id)
        if policy.reboot != "never":
            await request_reboot(
                session, org_id=org_id, device_id=device_id, origin="scan", now=now
            )


async def ingest_reboot_result(
    session: AsyncSession, job: Job, result: PatchRebootResult, now: datetime
) -> None:
    row = (
        await session.execute(
            select(PatchReboot).where(
                PatchReboot.org_id == job.org_id, PatchReboot.job_id == job.id
            )
        )
    ).scalar_one_or_none()
    if row is None or row.state not in OPEN_STATES:
        return
    row.user_present = result.user_present
    if result.state == "scheduled" and result.scheduled_for is not None:
        row.state = "scheduled"
        row.scheduled_for = datetime.fromisoformat(result.scheduled_for.replace("Z", "+00:00"))
        await session.flush()
        await _audit(
            session,
            row,
            "patch.reboot_scheduled",
            scheduled_for=result.scheduled_for,
            user_present=result.user_present,
            workstation=result.workstation,
            by="patch.reboot",
            job_id=str(job.id),
        )
    elif result.state == "not_needed":
        await _close(session, row, "cancelled", REASON_OS_NO_LONGER_NEEDS, now)
    else:
        row.state = "pending"
        await session.flush()
        await _audit(
            session, row, "patch.reboot_deferred", reason="too little of the window was left"
        )


def _params(
    policy: ResolvedPatchPolicy, not_after: datetime | None, origin: str
) -> PatchRebootParams:
    return PatchRebootParams(
        # An operator's restart happens whether or not the OS asks for one.
        only_if_required=policy.reboot != "forced" and origin != "operator",
        deferral_hours=policy.reboot_deferral_hours,
        not_after=rfc3339(not_after) if not_after is not None else None,
    )


async def run_reboot_sweep(
    session: AsyncSession, job_engine: _JobEngineLike, *, org_id: UUID, now: datetime
) -> list[tuple[UUID, dict[str, Any]]]:
    """Advance every open restart in this org. Returns `(device_id, envelope)`
    pairs for the caller to publish after commit."""
    rows = (
        await session.execute(
            select(PatchReboot).where(
                PatchReboot.org_id == org_id, PatchReboot.state.in_(OPEN_STATES)
            )
        )
    ).scalars().all()
    online = set(
        (
            await session.execute(
                select(Device.id)
                .join(Agent, Agent.device_id == Device.id)
                .where(
                    Device.org_id == org_id,
                    Agent.status == "online",
                    # The gate refuses `patch.reboot` to a device waiting for
                    # review; one that is never "online" here is never sent it.
                    Device.review_state == "approved",
                )
            )
        ).scalars()
    )
    pending: list[tuple[UUID, dict[str, Any]]] = []
    for row in rows:
        if row.state == "dispatched":
            await _reconcile_dispatched(session, row, now)
        if row.state == "scheduled":
            envelope = await _maybe_verify(session, job_engine, row, now, online)
            if envelope is not None:
                pending.append((row.device_id, envelope))
            continue
        if row.state != "pending" or row.device_id not in online:
            continue
        policy = await resolve_patch_policy(session, org_id, row.device_id)
        # "never" governs restarts patching causes, not one a person asked for.
        if policy.reboot == "never" and row.origin != "operator":
            await _close(session, row, "cancelled", REASON_POLICY_NEVER_RESTARTS, now)
            continue
        if not in_window(policy.maintenance_window, now):
            continue
        job = await job_engine.create(
            session,
            org_id=org_id,
            type="patch.reboot",
            target_device_id=row.device_id,
            params=_params(
                policy, window_slot_end(policy.maintenance_window, now), row.origin
            ).model_dump(),
            requested_by=None,
        )
        pending.append((row.device_id, await job_engine.signed_envelope(job)))
        row.state, row.job_id = "dispatched", job.id
        row.attempts += 1
        await session.flush()
        await _audit(session, row, "patch.reboot_dispatched", job_id=str(job.id))
    await session.flush()
    return pending


async def _reconcile_dispatched(session: AsyncSession, row: PatchReboot, now: datetime) -> None:
    if row.job_id is None:
        row.state = "pending"
        return
    status = (
        await session.execute(
            select(Job.status).where(Job.org_id == row.org_id, Job.id == row.job_id)
        )
    ).scalar_one_or_none()
    if status not in _FAILED_JOB:
        return
    if status == JobStatus.refused:
        await _close(session, row, "failed", REASON_AGENT_CANNOT_RESTART, now)
    elif row.attempts >= MAX_ATTEMPTS:
        await _close(session, row, "failed", f"the restart job {status.value} three times", now)
    else:
        row.state = "pending"
        await session.flush()


async def _maybe_verify(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    row: PatchReboot,
    now: datetime,
    online: set[UUID],
) -> dict[str, Any] | None:
    """Once the booked time has passed, ask for one scan to confirm it."""
    if (
        row.scheduled_for is None
        or row.verify_requested_at is not None
        or now < row.scheduled_for + VERIFY_AFTER
        or row.device_id not in online
    ):
        return None
    job = await job_engine.create(
        session,
        org_id=row.org_id,
        type="patch.scan",
        target_device_id=row.device_id,
        params=PatchScanParams().model_dump(),
        requested_by=None,
    )
    row.verify_requested_at = now
    await session.flush()
    await _audit(session, row, "patch.reboot_verify_dispatched", job_id=str(job.id))
    envelope: dict[str, Any] = await job_engine.signed_envelope(job)
    return envelope
