"""Scheduled `logs.ship` (docs/modules/security-logs/spec.md).

The agent holds no standing log subscription and schedules nothing locally;
the cadence is the org policy's `ship_interval_s`, delivered as signed jobs
from here. These functions create and sign inside the caller's tenant
transaction; the caller publishes after commit.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.jobs.engine import JobEngine
from app.models import Agent, Device, Job, JobStatus, Policy
from app.modules.security_logs.ingest import DEFAULT_POLICY

JOB_TYPE = "logs.ship"

# Only these platforms have a collector (agent `logs` source_windows/darwin).
SHIP_OS_FAMILIES: tuple[str, ...] = ("windows", "macos")

# How often the beat asks "who is due". The effective cadence is the policy
# interval rounded up to a multiple of this.
SWEEP_INTERVAL_S = 300.0

# The policy default is 60 s, which as a signed job per device is a signature,
# a job row and an audit row every minute per machine. Floor it; one job ships
# up to 5,000 events, so a 15-minute cadence loses nothing but latency.
MIN_SHIP_INTERVAL_S = 900
MAX_SHIP_INTERVAL_S = 86400

_OUTSTANDING = (JobStatus.queued, JobStatus.dispatched, JobStatus.running)


async def ship_interval_s(session: AsyncSession, org_id: UUID) -> int:
    """The org's effective interval: its org-wide `security_logs` policy's
    `ship_interval_s` (default when absent or malformed), clamped."""
    config = (
        await session.execute(
            select(Policy.config)
            .where(
                Policy.org_id == org_id,
                Policy.module == "security_logs",
                Policy.device_group_id.is_(None),
                Policy.enabled.is_(True),
            )
            .order_by(Policy.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    raw = (config or {}).get("ship_interval_s", DEFAULT_POLICY["ship_interval_s"])
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(DEFAULT_POLICY["ship_interval_s"])
    return min(max(value, MIN_SHIP_INTERVAL_S), MAX_SHIP_INTERVAL_S)


async def due_device_ids(
    session: AsyncSession, org_id: UUID, *, now: datetime, interval_s: int
) -> list[UUID]:
    """Online Windows/macOS devices with no `logs.ship` in flight and none
    issued within the interval."""
    online = list(
        (
            await session.execute(
                select(Device.id)
                .join(Agent, Agent.device_id == Device.id)
                .where(
                    Device.org_id == org_id,
                    Agent.org_id == org_id,
                    Device.status == "active",
                    Device.review_state == "approved",
                    Device.os_family.in_(SHIP_OS_FAMILIES),
                    Agent.status == "online",
                    Agent.cert_revoked.is_(False),
                )
            )
        ).scalars()
    )
    if not online:
        return []
    recent_cutoff = now - timedelta(seconds=interval_s)
    busy = set(
        (
            await session.execute(
                select(Job.target_id)
                .where(
                    Job.org_id == org_id,
                    Job.type == JOB_TYPE,
                    Job.target_type == "device",
                    Job.target_id.in_(online),
                    (Job.status.in_(_OUTSTANDING) & (Job.expires_at > now))
                    | (Job.issued_at > recent_cutoff),
                )
                .distinct()
            )
        ).scalars()
    )
    return [device_id for device_id in online if device_id not in busy]


async def dispatch_due(
    session: AsyncSession, job_engine: JobEngine, org_id: UUID, *, now: datetime
) -> list[tuple[UUID, dict[str, Any]]]:
    """Create and sign one `logs.ship` per due device, with a TTL of one
    interval so an undelivered job never overlaps the next. Returns `(device_id,
    envelope)` pairs for the caller to publish after commit.

    Audited once per org per pass rather than per job: each job is audited
    individually on delivery (`jobs.dispatched`), and at this cadence a second
    per-job row would double the audit volume for no extra fact.
    """
    interval = await ship_interval_s(session, org_id)
    pending: list[tuple[UUID, dict[str, Any]]] = []
    for device_id in await due_device_ids(session, org_id, now=now, interval_s=interval):
        job = await job_engine.create(
            session,
            org_id=org_id,
            type=JOB_TYPE,
            target_device_id=device_id,
            params={},
            requested_by=None,
            ttl_s=interval,
        )
        pending.append((device_id, await job_engine.signed_envelope(job)))
    if pending:
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="security_logs.ship_scheduled",
            target_type="organization",
            target_id=str(org_id),
            metadata={"count": len(pending), "interval_s": interval},
        )
    return pending
