"""Redis bridge: job dispatch down to agents, status fan-out up to the UI.

Channels:
- dispatch:{device_id} — signed job envelopes; every gateway replica
  psubscribes and forwards to the connections it holds.
- ui:{org_id} — live status events consumed by the operator UI WebSocket.

handle_agent_message() is the single entry point for agent->server messages.
Identity is never taken from message content: the device binding comes from
the mTLS certificate, and any message referencing a job that does not belong
to that device is discarded and audited.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.gateway.connections import ConnectionRegistry
from app.gateway.protocol import (
    AgentMessage,
    BackupChunkQueryMsg,
    BackupReadQueryMsg,
    HealthReportMsg,
    HeartbeatMsg,
    JobLogMsg,
    JobResultMsg,
    JobStatusMsg,
    SecurityEventsMsg,
)
from app.jobs.engine import JobEngine
from app.models import Agent, Device, Job, JobLog

if TYPE_CHECKING:
    from app.core.ca import CAService

logger = logging.getLogger("its.gateway")

DISPATCH_PATTERN = "dispatch:*"
REVOKE_CHANNEL = "agent_revoke"

OnDelivered = Callable[[UUID, dict[str, Any]], Awaitable[None]]


def dispatch_channel(device_id: UUID) -> str:
    return f"dispatch:{device_id}"


async def publish_revoke(redis: aioredis.Redis, device_id: UUID) -> None:
    """Announce an agent revocation so every gateway replica drops the device's
    live connection (the connect-time check only guards new connections)."""
    await redis.publish(REVOKE_CHANNEL, json.dumps({"device_id": str(device_id)}))


def ui_channel(org_id: UUID) -> str:
    return f"ui:{org_id}"


async def publish_job(
    redis: aioredis.Redis, device_id: UUID, envelope: dict[str, Any]
) -> None:
    await redis.publish(dispatch_channel(device_id), json.dumps(envelope))


async def publish_ui_event(redis: aioredis.Redis, org_id: UUID, event: dict[str, Any]) -> None:
    await redis.publish(ui_channel(org_id), json.dumps(event))


def queue_ui_event(session: AsyncSession, org_id: UUID, event: dict[str, Any]) -> None:
    """Modules queue UI events during a tenant transaction; the kernel drains
    and publishes them only after the transaction commits (see
    handle_agent_message) — never before the data is durable."""
    session.info.setdefault("ui_events", []).append((org_id, event))


class DispatchSubscriber:
    """Forwards published job envelopes to locally held agent connections.

    on_delivered (optional) fires when an envelope is about to be written to a
    connection this replica holds — the gateway uses it to flip the job to
    dispatched, so job state reflects delivery, not merely publication.

    IT RUNS BEFORE THE WRITE, NOT AFTER, and the order is the whole point
    (2026-08-22-job-lifecycle-design.md §3.3). Sending first and marking second
    left a window in which the envelope was on the wire and the row still read
    `queued`; `GatewayServer._drain_queued` has always marked inside its
    transaction and written afterwards, so the two dispatch paths disagreed
    about what `queued` means. Withdrawal (`POST /jobs/{id}/cancel`) is built
    entirely on `queued` meaning "never written to a socket", so a job the
    console reports as withdrawn could otherwise have been delivered.

    THE COST IS STATED RATHER THAN DISCOVERED: a job recorded `dispatched`
    whose write then failed. That case already existed on the drain path and is
    already handled -- `app/worker.py::sweep_org` re-signs and republishes a
    `dispatched` job that has been silent for `STUCK_DISPATCH_AFTER`. This
    trades a rare invisible over-execution for a rare visible re-dispatch."""

    def __init__(
        self,
        registry: ConnectionRegistry,
        redis: aioredis.Redis,
        *,
        on_delivered: "OnDelivered | None" = None,
    ) -> None:
        self._registry = registry
        self._redis = redis
        self._on_delivered = on_delivered

    async def run(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.psubscribe(DISPATCH_PATTERN)
        async for message in pubsub.listen():
            if message["type"] != "pmessage":
                continue
            channel = message["channel"]
            if isinstance(channel, bytes):
                channel = channel.decode()
            try:
                device_id = UUID(channel.split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            ws = self._registry.get(device_id)
            if ws is not None:
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                if self._on_delivered is not None:
                    await self._on_delivered(device_id, json.loads(data))
                await ws.send_text(data)


CLOSE_REVOKED = 4403


class RevokeSubscriber:
    """Drops live agent connections on revocation. Every gateway replica
    subscribes; the one holding the revoked device's connection closes it, so
    revocation takes effect immediately rather than only at the next connect."""

    def __init__(self, registry: ConnectionRegistry, redis: aioredis.Redis) -> None:
        self._registry = registry
        self._redis = redis

    async def run(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(REVOKE_CHANNEL)
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            data = message["data"]
            if isinstance(data, bytes):
                data = data.decode()
            try:
                device_id = UUID(json.loads(data)["device_id"])
            except (ValueError, KeyError, TypeError):
                continue
            ws = self._registry.get(device_id)
            if ws is not None:
                await ws.close(CLOSE_REVOKED, "certificate revoked")


async def _load_job_for_device(
    session: AsyncSession, job_id: str, device_id: UUID
) -> Job | None:
    try:
        parsed = UUID(job_id)
    except ValueError:
        return None
    job = (await session.execute(select(Job).where(Job.id == parsed))).scalar_one_or_none()
    if job is None or job.target_id != device_id:
        return None
    return job


async def _audit_mismatch(
    session: AsyncSession, org_id: UUID, device_id: UUID, msg: AgentMessage
) -> None:
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="agent",
        actor_id=None,
        action="gateway.identity_mismatch",
        target_type="device",
        target_id=str(device_id),
        metadata={"message_kind": msg.kind, "job_id": getattr(msg, "job_id", None)},
    )


async def _handle_security_events(
    app_engine: AsyncEngine,
    redis: aioredis.Redis,
    msg: SecurityEventsMsg,
    *,
    device_id: UUID,
    org_id: UUID,
) -> None:
    from app.modules.security_logs.ingest import SecurityIngestError, ingest_events

    inserted = 0
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one_or_none()
        if device is None:
            await _audit_mismatch(session, org_id, device_id, msg)
            return
        try:
            inserted = await ingest_events(session, device, msg.events)
        except SecurityIngestError as exc:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="agent",
                actor_id=None,
                action="security_logs.ingest_rejected",
                target_type="device",
                target_id=str(device_id),
                metadata={"reason": str(exc), "batch_size": len(msg.events)},
            )
            return
    # transaction committed — safe to notify UI consumers
    await publish_ui_event(
        redis,
        org_id,
        {"kind": "security_events_ingested", "device_id": str(device_id), "count": inserted},
    )


async def _handle_health_report(
    app_engine: AsyncEngine,
    redis: aioredis.Redis,
    msg: HealthReportMsg,
    *,
    device_id: UUID,
    org_id: UUID,
) -> None:
    """Endpoint-health edges and summaries (endpoint-health spec §6).

    Follows _handle_security_events exactly, and for the same reasons: the
    device comes from the certificate binding and never from message content;
    there is NO per-report audit record (volume would swamp a hash-chained log
    whose value is that a human can read it); one audit record for a REJECTED
    batch; and the UI event is published only after the tenant transaction has
    committed, so the console never refreshes onto data that then rolls back.

    The discard count rides that UI event rather than a table — endpoint-health
    spec §0.8's amendment: exactly three health tables retain rows over time,
    and a per-batch counter table would be a time series wearing an ops-metrics
    hat.
    """
    from app.modules.health.ingest import HealthIngestError, IngestOutcome, ingest_report

    outcome = IngestOutcome()
    queued_events: list[tuple[UUID, dict[str, Any]]] = []
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        queued_events = session.info.setdefault("ui_events", [])
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one_or_none()
        if device is None:
            await _audit_mismatch(session, org_id, device_id, msg)
            return
        try:
            outcome = await ingest_report(session, org_id, device, msg)
        except HealthIngestError as exc:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="agent",
                actor_id=None,
                action="health.ingest_rejected",
                target_type="device",
                target_id=str(device_id),
                metadata={"reason": str(exc), "batch_size": len(msg.reports)},
            )
            return
    # transaction committed — safe to notify UI consumers
    await publish_ui_event(
        redis,
        org_id,
        {
            "kind": "health_reports_ingested",
            "device_id": str(device_id),
            "applied": outcome.applied,
            "discarded": outcome.discarded,
        },
    )
    for event_org, event in queued_events:
        await publish_ui_event(redis, event_org, event)


# ---------------------------------------------------------------------------
# Heartbeat -> agents.last_seen_at
# ---------------------------------------------------------------------------

# How often one device's heartbeat is allowed to write. NOT 60s, which is the
# agent's own ticker interval: at equal values, ordinary scheduling jitter drops
# every other beat and the effective refresh becomes ~120s. 30s keeps the write
# rate bounded at 2/min/device for a hostile agent while leaving the agent's own
# 60s beat unthrottled.
HEARTBEAT_TOUCH_INTERVAL_S = 30

# Bounded by the fleet size held by THIS gateway replica -- entries are only
# created for devices that passed mTLS admission, so it cannot be grown by an
# unauthenticated peer. The cap and the sweep exist for the replica that has
# held ten thousand short-lived connections since it started.
_TOUCH_CACHE_MAX = 20_000

_last_heartbeat_touch: dict[UUID, datetime] = {}


def reset_heartbeat_rate_limit() -> None:
    """Test seam: drop the in-process rate-limiter state.

    Process-global mutable state is a liability in a test suite, not in
    production -- one gateway process, one dict. Exported so a test can make its
    own assertions deterministic rather than reaching into a private name.
    """
    _last_heartbeat_touch.clear()


def _should_touch(device_id: UUID, now: datetime) -> bool:
    last = _last_heartbeat_touch.get(device_id)
    if last is not None and (now - last).total_seconds() < HEARTBEAT_TOUCH_INTERVAL_S:
        return False
    if len(_last_heartbeat_touch) >= _TOUCH_CACHE_MAX:
        cutoff = now - timedelta(seconds=HEARTBEAT_TOUCH_INTERVAL_S)
        for stale in [k for k, v in _last_heartbeat_touch.items() if v < cutoff]:
            _last_heartbeat_touch.pop(stale, None)
    _last_heartbeat_touch[device_id] = now
    return True


async def _touch_agent(app_engine: AsyncEngine, *, device_id: UUID, org_id: UUID) -> None:
    """Refresh `agents.last_seen_at` from a heartbeat.

    WHY THIS EXISTS (C2). `last_seen_at` used to be written only by
    `_mark_online` at connect and `_mark_offline` at disconnect, and health's
    server-evaluated `agent.offline` check reads exactly that column with a
    default `offline_after_s` of 900s. A healthy agent that stayed connected for
    sixteen minutes was therefore reported offline with its socket open.
    `tests/test_gateway_heartbeat.py` pins both halves.

    THE TIMESTAMP IS THE SERVER'S. `HeartbeatMsg.ts` is agent-supplied and
    therefore forgeable; a device claiming a timestamp in 2099 would make itself
    permanently un-reportable as offline. The message's only role is to say
    "I am here now".

    NO AUDIT RECORD. Per-minute per-device volume, and
    `_handle_health_report`'s docstring already states the rule: a hash-chained
    log whose value is that a human can read it must not carry a heartbeat.

    TENANT-SCOPED (invariant #5). The device binding comes from the mTLS
    certificate, but the UPDATE still runs inside the connection's org session
    and carries an explicit `org_id` predicate, so a device_id belonging to
    another org matches zero rows rather than silently succeeding.
    """
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await session.execute(
            update(Agent)
            .where(Agent.device_id == device_id, Agent.org_id == org_id)
            .values(last_seen_at=datetime.now(UTC))
        )


async def handle_agent_message(
    app_engine: AsyncEngine,
    job_engine: JobEngine,
    redis: aioredis.Redis,
    msg: AgentMessage,
    *,
    device_id: UUID,
    org_id: UUID,
    ca: "CAService | None" = None,
) -> None:
    if isinstance(msg, HeartbeatMsg):
        if _should_touch(device_id, datetime.now(UTC)):
            await _touch_agent(app_engine, device_id=device_id, org_id=org_id)
        return

    if isinstance(msg, SecurityEventsMsg):
        await _handle_security_events(app_engine, redis, msg, device_id=device_id, org_id=org_id)
        return

    if isinstance(msg, HealthReportMsg):
        await _handle_health_report(app_engine, redis, msg, device_id=device_id, org_id=org_id)
        return

    if isinstance(msg, BackupChunkQueryMsg):
        # The backup dedup exchange (docs/modules/backup/spec.md §5.2). It
        # carries a `job_id` but is NOT a job-state message: it does not touch
        # the job row, and it must not fall through to `_load_job_for_device`
        # below, whose only application-level predicate is `Job.id` — the
        # exchange's own resolver carries `Job.org_id` too, and needs the
        # refusal to be silent rather than an identity-mismatch audit.
        from app.modules.backup.dedup import handle_chunk_query

        await handle_chunk_query(
            app_engine, redis, msg, device_id=device_id, org_id=org_id
        )
        return

    if isinstance(msg, BackupReadQueryMsg):
        # The backup READ exchange. Like the dedup query above it carries a
        # `job_id` and is NOT a job-state message: it does not touch the job
        # row, and it must not fall through to `_load_job_for_device` below,
        # whose only application-level predicate is `Job.id` — this exchange's
        # own resolver carries `Job.org_id` too, and needs the refusal to be
        # silent rather than an identity-mismatch audit.
        from app.modules.backup.read import handle_read_query

        await handle_read_query(
            app_engine, redis, msg, device_id=device_id, org_id=org_id
        )
        return

    queued_events: list[tuple[UUID, dict[str, Any]]] = []
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        queued_events = session.info.setdefault("ui_events", [])
        job = await _load_job_for_device(session, msg.job_id, device_id)
        if job is None:
            # forged/cross-device reference: discard the message, never touch
            # the real job (a compromised device must not affect others' jobs)
            await _audit_mismatch(session, org_id, device_id, msg)
            return

        job_engine_event: dict[str, Any]
        if isinstance(msg, JobStatusMsg):
            job_engine_event = {"kind": "job_status", "job_id": str(job.id), "state": msg.state}
            await job_engine.mark_running(session, job)
        elif isinstance(msg, JobLogMsg):
            session.add(
                JobLog(
                    org_id=org_id,
                    job_id=job.id,
                    ts=datetime.now(UTC),
                    level=msg.level,
                    message=msg.msg,
                    artifact_ref=None,
                )
            )
            await session.flush()
            job_engine_event = {
                "kind": "job_log",
                "job_id": str(job.id),
                "level": msg.level,
                "msg": msg.msg,
            }
        elif isinstance(msg, JobResultMsg):
            if msg.state == "refused":
                await job_engine.refuse(
                    session, job, reason=str(msg.result.get("reason", "agent refused"))
                )
            else:
                await job_engine.complete(session, job, state=msg.state, result=msg.result)
            job_engine_event = {
                "kind": "job_status",
                "job_id": str(job.id),
                "state": msg.state,
                "result": msg.result,
            }
        else:  # pragma: no cover - exhaustive over AgentMessage
            return

        job_type = job.type

    # transaction committed: publish the kernel event plus anything modules queued
    await publish_ui_event(redis, org_id, job_engine_event)
    for event_org, event in queued_events:
        await publish_ui_event(redis, event_org, event)

    # Certificate rotation is a two-leg exchange
    # (docs/modules/agent/cert-renewal.md §3.1). Advancing it here means the
    # second leg follows the first down the SAME connection rather than waiting
    # for the next one; the gateway's connect path runs the identical state
    # machine, so this is an optimisation, not a second mechanism. AFTER the
    # commit, so the CSR the next step reads is durable.
    if isinstance(msg, JobResultMsg):
        await _advance_cert_renewal(
            app_engine, job_engine, redis, ca, org_id, device_id, job_type
        )


async def _advance_cert_renewal(
    app_engine: AsyncEngine,
    job_engine: JobEngine,
    redis: aioredis.Redis,
    ca: "CAService | None",
    org_id: UUID,
    device_id: UUID,
    job_type: str,
) -> None:
    """Second leg of a certificate rotation, published after the first commits.

    `ca` is passed in rather than resolved from `get_settings()` on purpose: the
    gateway process holds ONE CA (built from the settings it was constructed
    with), and reaching for the process-global settings here would mean this
    code path could sign against a different root than the connect path does. A
    None ca simply skips -- every caller that is not the gateway (the API, the
    worker, a direct unit test) has no business rotating identities.
    """
    from app.services.cert_renewal import CERT_JOB_TYPES, advance_for_device

    if ca is None or job_type not in CERT_JOB_TYPES:
        return
    envelope = await advance_for_device(
        app_engine, job_engine, ca, org_id=org_id, device_id=device_id
    )
    if envelope is not None:
        await publish_job(redis, device_id, envelope)
