"""Scheduled `inventory.collect`: a daily refresh per device, plus one on the
first connection after enrolment.

The schedule lives on the server, not in the agent: a collect is a signed job
like any other, so the cadence arrives as signed work and nothing on the
endpoint runs on a standing instruction.

These functions only CREATE and SIGN jobs inside the caller's tenant
transaction. Publishing is the caller's job, after commit (the worker's
`inventory_collect_sweep_*`, or the gateway's connect-time queue drain).
"""

import math
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.jobs.engine import JobEngine
from app.models import Agent, Device, Job, JobStatus, Policy
from app.modules.inventory.ingest import DEFAULT_SWEEP_POLICY

JOB_TYPE = "inventory.collect"

# Beat cadence. Hourly passes over "what is due" rather than one daily burst.
SWEEP_INTERVAL_S = 3600.0

# The cadence is the org's `inventory` policy `interval_hours` (seeded at first
# ingest, default 24). A device that collected, or tried to, within 5/6 of it
# (20 h of 24) is not due: slack so a machine online at about the same time
# each day is collected every day rather than every other day.
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 168
_SLACK = 5 / 6

# Spread: one pass dispatches at most 1/PASSES_PER_FLEET of the org's online
# devices (never fewer than MIN_PER_PASS), oldest collection first. A fleet
# that has never collected is worked through over about half a day instead of
# all at once.
PASSES_PER_FLEET = 12
MIN_PER_PASS = 25

_OUTSTANDING = (JobStatus.queued, JobStatus.dispatched, JobStatus.running)
_RAN = (JobStatus.succeeded, JobStatus.failed)


def per_pass_cap(online_devices: int) -> int:
    return max(MIN_PER_PASS, math.ceil(online_devices / PASSES_PER_FLEET))


async def collect_every(session: AsyncSession, org_id: UUID) -> timedelta:
    """How recent a collect must be for a device not to be due, from the
    org-wide `inventory` policy (default when absent or malformed)."""
    config = (
        await session.execute(
            select(Policy.config)
            .where(
                Policy.org_id == org_id,
                Policy.module == "inventory",
                Policy.device_group_id.is_(None),
                Policy.enabled.is_(True),
            )
            .order_by(Policy.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    raw = (config or {}).get("interval_hours", DEFAULT_SWEEP_POLICY["interval_hours"])
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        hours = int(DEFAULT_SWEEP_POLICY["interval_hours"])
    hours = min(max(hours, MIN_INTERVAL_HOURS), MAX_INTERVAL_HOURS)
    return timedelta(hours=hours) * _SLACK


async def _online_device_ids(session: AsyncSession, org_id: UUID) -> list[UUID]:
    """Devices whose agent is connected now. A device with no `agents` row, a
    revoked certificate or an offline agent has nothing that can run the job,
    and dispatching to it only produces a job that expires unread."""
    return list(
        (
            await session.execute(
                select(Device.id)
                .join(Agent, Agent.device_id == Device.id)
                .where(
                    Device.org_id == org_id,
                    Agent.org_id == org_id,
                    Device.status == "active",
                    # Pending machines ARE inventoried (install-link spec §1.5);
                    # a rejected one may receive only `agent.uninstall`, and the
                    # gate would abort this org's pass on it.
                    Device.review_state != "rejected",
                    Agent.status == "online",
                    Agent.cert_revoked.is_(False),
                )
            )
        ).scalars()
    )


async def due_device_ids(session: AsyncSession, org_id: UUID, *, now: datetime) -> list[UUID]:
    """Online devices due a collect this pass, oldest successful collect first
    (never-collected devices lead), bounded by `per_pass_cap`.

    Not due: a collect already in flight (queued/dispatched/running and not
    yet expired), or one that finished -- succeeded or failed -- within
    `collect_every`. A failed collect counts as a run so a device whose
    collector is broken is retried daily, not hourly. An expired or cancelled
    job never ran and does not count.
    """
    online = await _online_device_ids(session, org_id)
    if not online:
        return []

    recent_cutoff = now - await collect_every(session, org_id)
    busy = set(
        (
            await session.execute(
                select(Job.target_id).where(
                    Job.org_id == org_id,
                    Job.type == JOB_TYPE,
                    Job.target_type == "device",
                    Job.target_id.in_(online),
                    (
                        (Job.status.in_(_OUTSTANDING) & (Job.expires_at > now))
                        | (Job.status.in_(_RAN) & (Job.finished_at >= recent_cutoff))
                    ),
                )
            )
        ).scalars()
    )

    last_success: dict[UUID, datetime] = {
        row.target_id: row.last
        for row in (
            await session.execute(
                select(Job.target_id, func.max(Job.finished_at).label("last"))
                .where(
                    Job.org_id == org_id,
                    Job.type == JOB_TYPE,
                    Job.target_type == "device",
                    Job.target_id.in_(online),
                    Job.status == JobStatus.succeeded,
                )
                .group_by(Job.target_id)
            )
        ).all()
    }

    due = [device_id for device_id in online if device_id not in busy]
    due.sort(key=lambda d: (d in last_success, last_success.get(d) or now, str(d)))
    return due[: per_pass_cap(len(online))]


async def _create(
    session: AsyncSession,
    job_engine: JobEngine,
    org_id: UUID,
    device_id: UUID,
    *,
    trigger: str,
) -> Job:
    job = await job_engine.create(
        session,
        org_id=org_id,
        type=JOB_TYPE,
        target_device_id=device_id,
        params={},
        requested_by=None,
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="inventory.collect_scheduled",
        target_type="device",
        target_id=str(device_id),
        metadata={"job_id": str(job.id), "trigger": trigger},
    )
    return job


async def dispatch_scheduled(
    session: AsyncSession, job_engine: JobEngine, org_id: UUID, *, now: datetime
) -> list[tuple[UUID, dict[str, Any]]]:
    """Create and sign one collect per due device. Returns `(device_id,
    envelope)` pairs for the caller to publish after commit."""
    pending: list[tuple[UUID, dict[str, Any]]] = []
    for device_id in await due_device_ids(session, org_id, now=now):
        job = await _create(session, job_engine, org_id, device_id, trigger="scheduled")
        pending.append((device_id, await job_engine.signed_envelope(job)))
    return pending


async def queue_first_collect(
    session: AsyncSession, job_engine: JobEngine, org_id: UUID, device_id: UUID
) -> Job | None:
    """Queue a collect if this device has had none since its agent enrolled.

    Called on the gateway's connect path; the job is left `queued` so the
    connect-time drain delivers it on the same connection. Keyed on the
    agent's `enrolled_at`, so a re-enrolled machine collects again. The agent
    row is locked so two overlapping connects cannot both queue one.
    """
    agent = (
        await session.execute(
            select(Agent)
            .where(Agent.org_id == org_id, Agent.device_id == device_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if agent is None or agent.cert_revoked:
        return None
    already = (
        await session.execute(
            select(Job.id)
            .where(
                Job.org_id == org_id,
                Job.type == JOB_TYPE,
                Job.target_type == "device",
                Job.target_id == device_id,
                Job.issued_at >= agent.enrolled_at,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if already is not None:
        return None
    return await _create(session, job_engine, org_id, device_id, trigger="first_connect")
