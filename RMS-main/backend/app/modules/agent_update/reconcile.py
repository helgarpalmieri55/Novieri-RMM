"""The rollout reconciler: `agent_update.tick`, every 60 s (spec §6.4).

It derives every target state from the `jobs` row, so succeeded, failed,
refused and expired are handled one way. It continues rings a person
started; it never creates a rollout and never advances a ring.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.tenancy import org_session
from app.models import Agent, Job, JobStatus
from app.models.core import AgentRelease
from app.modules.agent_update.jobtypes import JOB_TYPE, AgentUpdateParams
from app.modules.agent_update.models import (
    COUNTED_FAILURES,
    IN_FLIGHT,
    UNRESOLVED,
    AgentUpdateRing,
    AgentUpdateRollout,
    AgentUpdateTarget,
)
from app.modules.agent_update.reasons import RETRYABLE_REFUSALS, UNKNOWN_TYPE, reason_from_result
from app.modules.agent_update.service import pause_rollout

__all__ = [
    "PROBE_TTL_S",
    "RETRY_BACKOFF",
    "TICK_INTERVAL_S",
    "Presign",
    "job_ttl_s",
    "tick",
    "tick_all_orgs",
]

logger = logging.getLogger(__name__)

TICK_INTERVAL_S = 60.0
PROBE_TTL_S = 600
RETRY_BACKOFF = timedelta(minutes=15)
MAX_RETRY_ATTEMPTS = 3
PILOT_FIRST_FAILURE_BELOW = 20
RECENT_FAILURE_RUN = 3

#: (version, os, arch, expires_in) -> a presigned https GET.
Presign = Callable[[str, str, str, int], str]
Envelope = tuple[UUID, dict[str, Any]]
PublishEnvelope = Callable[[UUID, dict[str, Any]], Awaitable[None]]

_LIVE_JOB = (JobStatus.queued, JobStatus.dispatched, JobStatus.running)


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
    ) -> Job: ...

    async def signed_envelope(self, job: Job) -> dict[str, Any]: ...


def job_ttl_s(health_window_s: int) -> int:
    """Download and staging, the trial, and margin (§6.4)."""
    return 1800 + health_window_s + 600


async def _resolve_target(
    session: AsyncSession,
    rollout: AgentUpdateRollout,
    target: AgentUpdateTarget,
    state: str,
    reason: str,
    now: datetime,
) -> None:
    target.state, target.reason, target.updated_at = state, reason[:64], now
    await audit_service.record(
        session,
        org_id=rollout.org_id,
        actor_type="system",
        actor_id=None,
        action="agent.update.target_resolved",
        target_type="device",
        target_id=str(target.device_id),
        metadata={
            "rollout_id": str(rollout.id),
            "from_version": target.from_version,
            "to_version": rollout.target_version,
            "state": state,
            "reason": reason,
            "job_id": str(target.job_id) if target.job_id else None,
        },
    )


async def _on_behalf_of(session: AsyncSession, target: AgentUpdateTarget) -> UUID | None:
    return (
        await session.execute(
            select(AgentUpdateRing.activated_by).where(AgentUpdateRing.id == target.ring_id)
        )
    ).scalar_one_or_none()


async def _resolve(
    session: AsyncSession,
    rollout: AgentUpdateRollout,
    job_engine: _JobEngineLike,
    now: datetime,
) -> list[Envelope]:
    targets = list(
        (
            await session.execute(
                select(AgentUpdateTarget).where(
                    AgentUpdateTarget.rollout_id == rollout.id,
                    AgentUpdateTarget.state.in_(IN_FLIGHT),
                )
            )
        ).scalars()
    )
    ids = {t.job_id for t in targets if t.job_id} | {
        t.probe_job_id for t in targets if t.probe_job_id
    }
    jobs = (
        {j.id: j for j in (await session.execute(select(Job).where(Job.id.in_(ids)))).scalars()}
        if ids
        else {}
    )
    envelopes: list[Envelope] = []
    for t in targets:
        if t.state == "verifying":
            probe = jobs.get(t.probe_job_id) if t.probe_job_id else None
            if probe is None:
                await _resolve_target(session, rollout, t, "failed", "degraded", now)
            elif probe.status is JobStatus.succeeded:
                await _resolve_target(session, rollout, t, "succeeded", "", now)
            elif probe.status in _LIVE_JOB and probe.expires_at > now:
                continue
            else:
                await _resolve_target(session, rollout, t, "failed", "degraded", now)
            continue

        job = jobs.get(t.job_id) if t.job_id else None
        if job is None:
            await _resolve_target(session, rollout, t, "lost", "job_missing", now)
            continue
        result = job.result if isinstance(job.result, dict) else {}
        status = job.status
        if status is JobStatus.succeeded:
            if result.get("outcome") == "already_current":
                await _resolve_target(session, rollout, t, "succeeded", "already_current", now)
                continue
            # A connection alone does not prove the new binary verifies and
            # runs a signed job; the probe does (§6.4).
            probe = await job_engine.create(
                session,
                org_id=rollout.org_id,
                type="hello",
                target_device_id=t.device_id,
                params={"message": "agent.update post-update probe"},
                requested_by=await _on_behalf_of(session, t),
                ttl_s=PROBE_TTL_S,
            )
            t.probe_job_id, t.state, t.updated_at = probe.id, "verifying", now
            envelopes.append((probe.id, await job_engine.signed_envelope(probe)))
        elif status is JobStatus.failed:
            state = "rolled_back" if result.get("outcome") == "rolled_back" else "failed"
            await _resolve_target(session, rollout, t, state, reason_from_result(result), now)
        elif status is JobStatus.refused:
            token = reason_from_result(result)
            if token == UNKNOWN_TYPE:
                await _resolve_target(session, rollout, t, "needs_manual_update", token, now)
            elif token in RETRYABLE_REFUSALS and t.attempts + 1 < MAX_RETRY_ATTEMPTS:
                t.attempts += 1
                t.state, t.reason, t.job_id = "pending", token, None
                t.next_attempt_at, t.updated_at = now + RETRY_BACKOFF, now
            else:
                await _resolve_target(session, rollout, t, "refused", token, now)
        elif status is JobStatus.expired:
            if job.attempts == 0:
                # Never written to a socket: not a failure (§6.3).
                t.state, t.reason, t.job_id, t.updated_at = "pending", "not_delivered", None, now
            else:
                await _resolve_target(session, rollout, t, "lost", "expired", now)
        elif status is JobStatus.cancelled:
            await _resolve_target(session, rollout, t, "failed", "cancelled", now)
        elif status in (JobStatus.dispatched, JobStatus.running) and job.expires_at < now:
            # The kernel only expires queued and dispatched jobs.
            await _resolve_target(session, rollout, t, "lost", "expired", now)
    await session.flush()
    return envelopes


async def _active_ring(session: AsyncSession, rollout_id: UUID) -> AgentUpdateRing | None:
    return (
        await session.execute(
            select(AgentUpdateRing).where(
                AgentUpdateRing.rollout_id == rollout_id, AgentUpdateRing.state == "active"
            )
        )
    ).scalar_one_or_none()


async def threshold_breached(
    session: AsyncSession, rollout: AgentUpdateRollout, ring: AgentUpdateRing
) -> bool:
    """§6.4 step 2: too many failures, three in a row, or any pilot failure
    in a pilot ring under 20 devices."""
    rows = (
        await session.execute(
            select(AgentUpdateTarget.state, func.count())
            .where(
                AgentUpdateTarget.ring_id == ring.id,
                AgentUpdateTarget.state.not_in(("needs_manual_update", "skipped")),
            )
            .group_by(AgentUpdateTarget.state)
        )
    ).all()
    size = sum(n for _, n in rows)
    failures = sum(n for s, n in rows if s in COUNTED_FAILURES)
    if failures == 0:
        return False
    if ring.is_pilot and size < PILOT_FIRST_FAILURE_BELOW:
        return True
    if failures > (rollout.failure_threshold_pct * size) // 100:
        return True
    recent = list(
        (
            await session.execute(
                select(AgentUpdateTarget.state)
                .where(
                    AgentUpdateTarget.ring_id == ring.id,
                    AgentUpdateTarget.state.in_(COUNTED_FAILURES | {"succeeded"}),
                )
                .order_by(AgentUpdateTarget.updated_at.desc(), AgentUpdateTarget.id.desc())
                .limit(RECENT_FAILURE_RUN)
            )
        ).scalars()
    )
    return len(recent) == RECENT_FAILURE_RUN and all(s in COUNTED_FAILURES for s in recent)


async def _top_up(
    session: AsyncSession,
    rollout: AgentUpdateRollout,
    ring: AgentUpdateRing,
    job_engine: _JobEngineLike,
    presign: Presign,
    now: datetime,
) -> list[Envelope]:
    in_flight = (
        await session.execute(
            select(func.count())
            .select_from(AgentUpdateTarget)
            .where(
                AgentUpdateTarget.rollout_id == rollout.id,
                AgentUpdateTarget.state.in_(IN_FLIGHT),
            )
        )
    ).scalar_one()
    room = rollout.batch_size - in_flight
    if room <= 0:
        return []
    busy = exists().where(
        Job.target_id == AgentUpdateTarget.device_id,
        text("jobs.status::text IN ('queued', 'dispatched', 'running')"),
    )
    candidates = list(
        (
            await session.execute(
                select(AgentUpdateTarget)
                .join(Agent, Agent.device_id == AgentUpdateTarget.device_id)
                .where(
                    AgentUpdateTarget.ring_id == ring.id,
                    AgentUpdateTarget.state == "pending",
                    (AgentUpdateTarget.next_attempt_at.is_(None))
                    | (AgentUpdateTarget.next_attempt_at <= now),
                    Agent.status == "online",
                    ~busy,
                )
                .order_by(AgentUpdateTarget.id)
                .limit(room)
            )
        ).scalars()
    )
    envelopes: list[Envelope] = []
    ttl = job_ttl_s(rollout.health_window_s)
    for t in candidates:
        release = (
            (
                await session.execute(select(AgentRelease).where(AgentRelease.id == t.release_id))
            ).scalar_one_or_none()
            if t.release_id
            else None
        )
        if release is None or not release.sha256:
            await _resolve_target(session, rollout, t, "skipped", "no_artifact", now)
            continue
        if not ring.is_pilot and release.channel != "stable":
            await _resolve_target(session, rollout, t, "skipped", "release_not_stable", now)
            continue
        params = AgentUpdateParams(
            target_version=rollout.target_version,
            os=release.os,
            arch=release.arch,
            artifact_url=presign(release.version, release.os, release.arch, ttl + 300),
            artifact_sha256=release.sha256,
            artifact_size=release.size_bytes,
            rollback=rollout.is_rollback,
            health_window_s=rollout.health_window_s,
            rollout_id=str(rollout.id),
            release_signature=release.release_signature,
        )
        job = await job_engine.create(
            session,
            org_id=rollout.org_id,
            type=JOB_TYPE,
            target_device_id=t.device_id,
            params=params.model_dump(),
            requested_by=ring.activated_by,
            ttl_s=ttl,
        )
        t.job_id, t.probe_job_id, t.state, t.updated_at = job.id, None, "dispatched", now
        await audit_service.record(
            session,
            org_id=rollout.org_id,
            actor_type="system",
            actor_id=None,
            action="agent.update.target_dispatched",
            target_type="job",
            target_id=str(job.id),
            metadata={
                "rollout_id": str(rollout.id),
                "device": str(t.device_id),
                "to_version": rollout.target_version,
                "on_behalf_of": str(ring.activated_by) if ring.activated_by else None,
            },
        )
        envelopes.append((job.id, await job_engine.signed_envelope(job)))
    await session.flush()
    return envelopes


async def _finish_ring(
    session: AsyncSession, rollout: AgentUpdateRollout, ring: AgentUpdateRing, now: datetime
) -> None:
    unresolved = (
        await session.execute(
            select(func.count())
            .select_from(AgentUpdateTarget)
            .where(AgentUpdateTarget.ring_id == ring.id, AgentUpdateTarget.state.in_(UNRESOLVED))
        )
    ).scalar_one()
    if unresolved:
        return
    ring.state, ring.completed_at = "done", now
    pending_rings = (
        await session.execute(
            select(func.count())
            .select_from(AgentUpdateRing)
            .where(AgentUpdateRing.rollout_id == rollout.id, AgentUpdateRing.state == "pending")
        )
    ).scalar_one()
    if pending_rings == 0:
        rollout.state, rollout.completed_at = "completed", now
        await audit_service.record(
            session,
            org_id=rollout.org_id,
            actor_type="system",
            actor_id=None,
            action="agent.update.rollout_completed",
            target_type="agent_update_rollout",
            target_id=str(rollout.id),
            metadata={"version": rollout.target_version, "is_rollback": rollout.is_rollback},
        )
    await session.flush()


async def tick(
    session: AsyncSession,
    org_id: UUID,
    *,
    job_engine: _JobEngineLike,
    presign: Presign,
    now: datetime | None = None,
) -> list[Envelope]:
    """One pass over this org's rollouts. Returns envelopes to publish after
    the caller's transaction commits."""
    now = now or datetime.now(UTC)
    has_in_flight = exists().where(
        AgentUpdateTarget.rollout_id == AgentUpdateRollout.id,
        AgentUpdateTarget.state.in_(IN_FLIGHT),
    )
    rollouts = list(
        (
            await session.execute(
                select(AgentUpdateRollout)
                .where(AgentUpdateRollout.state.in_(("running", "paused")) | has_in_flight)
                .order_by(AgentUpdateRollout.created_at)
            )
        ).scalars()
    )
    envelopes: list[Envelope] = []
    for rollout in rollouts:
        envelopes += await _resolve(session, rollout, job_engine, now)
        if rollout.state != "running":
            continue
        ring = await _active_ring(session, rollout.id)
        if ring is None:
            continue
        if await threshold_breached(session, rollout, ring):
            await pause_rollout(
                session, rollout_id=rollout.id, actor_id=None, reason="failure_threshold"
            )
            continue
        envelopes += await _top_up(session, rollout, ring, job_engine, presign, now)
        await _finish_ring(session, rollout, ring, now)
    return envelopes


async def _org_ids(admin_engine: AsyncEngine) -> list[UUID]:
    async with AsyncSession(admin_engine) as session:
        rows = await session.execute(
            select(AgentUpdateRollout.org_id)
            .where(
                AgentUpdateRollout.state.in_(("running", "paused"))
                | exists().where(
                    AgentUpdateTarget.rollout_id == AgentUpdateRollout.id,
                    AgentUpdateTarget.state.in_(IN_FLIGHT),
                )
            )
            .distinct()
        )
        return list(rows.scalars())


async def tick_all_orgs(
    *,
    engine: AsyncEngine,
    admin_engine: AsyncEngine,
    job_engine: _JobEngineLike,
    presign: Presign,
    publish: PublishEnvelope,
) -> int:
    """One org per transaction; publish only after it commits. Returns at
    once when no org has a rollout in progress."""
    total = 0
    for org_id in await _org_ids(admin_engine):
        try:
            async with org_session(org_id, engine=engine) as session:
                envelopes = await tick(session, org_id, job_engine=job_engine, presign=presign)
        except Exception:  # noqa: BLE001 - one org must not stop the others
            logger.error("agent_update.tick: org %s failed; others continue", org_id, exc_info=True)
            continue
        for job_id, envelope in envelopes:
            await publish(job_id, envelope)
        total += len(envelopes)
    return total
