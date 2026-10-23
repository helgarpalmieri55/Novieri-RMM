"""The agent gateway: outbound-only mTLS WebSocket endpoint agents dial.

Built directly on asyncio + `websockets` so the TLS peer certificate is
first-class: identity is the certificate fingerprint, resolved to an Agent
row before any message is read. Unknown, revoked, or expired certificates are
closed with code 4401 and audited. The connection then parks on a blocking
read; jobs are pushed down (queued jobs drained at connect, live dispatch via
Redis), agent messages flow up through handle_agent_message().

One path is routed elsewhere: ``/relay`` is the agent's second connection for
a screen session (docs/modules/remote-control/spec-screen-share.md §2.2) and is
handed to ``ScreenRelayHub``. Every other path is the agent channel, exactly
as before, because deployed agents dial whatever URL they were enrolled with.
An optional internal listener (no TLS, compose network only) serves the api's
side of the same relay on ``/relay-internal``.
"""

import asyncio
import hashlib
import json
import logging
import os
import ssl
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import redis.asyncio as aioredis
import websockets
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.typing import Subprotocol

from app.core.audit import audit_service
from app.core.ca import CAService, get_ca
from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.health.heartbeat import HEARTBEAT_INTERVAL_S, write_heartbeat
from app.core.tenancy import org_session
from app.gateway.agent_headers import AgentSelfReport, parse_agent_headers
from app.gateway.connections import ConnectionRegistry
from app.gateway.dispatch import (
    DispatchSubscriber,
    RevokeSubscriber,
    handle_agent_message,
)
from app.gateway.protocol import parse_agent_message
from app.jobs.engine import JobEngine
from app.models import Agent
from app.modules.remote import rsp
from app.modules.remote.relay import RelayRegistry, SessionRelay
from app.modules.remote.screen_relay import (
    RELAY_INTERNAL_PATH,
    RELAY_PATH,
    AgentIdentity,
    ScreenRelayHub,
)
from app.modules.remote.service import RemoteSessionService

logger = logging.getLogger("its.gateway")

CLOSE_UNAUTHORIZED = 4401


def select_subprotocol(
    _connection: ServerConnection, offered: Any
) -> Subprotocol | None:
    """Pick ``itms-rsp.1`` when offered and accept no subprotocol otherwise.

    Passing ``subprotocols=`` to ``serve`` instead would make websockets
    reject every client that offers none with HTTP 400, which is every
    deployed agent on ``/agent``.
    """
    return Subprotocol(rsp.SUBPROTOCOL) if rsp.SUBPROTOCOL in offered else None


def _path(connection: ServerConnection) -> str:
    return urlsplit(connection.request.path).path if connection.request else ""


class _WsAdapter:
    """Adapts a websockets connection to the WsLike protocol (send_text)."""

    def __init__(self, connection: ServerConnection) -> None:
        self._connection = connection

    async def send_text(self, data: str) -> None:
        await self._connection.send(data)

    async def close(self, code: int, reason: str) -> None:
        await self._connection.close(code, reason)


def register_module_job_types() -> None:
    """Register every module's job types (and IPAM's inventory hook) into the
    kernel registry for THIS process.

    Agent results complete in the gateway — `dispatch.py` calls
    `JobEngine.complete`, which looks the job type up by name to find its result
    handler — and this process never imports `app.modules.register_all`. A module
    that registers its handler only in `register(app)` therefore has a handler
    that is DEAD IN PRODUCTION while every in-process test passes. That is not
    hypothetical: it is exactly the hole the IPAM inventory hook was found in.

    Lifted out of `GatewayServer.start` so the property is testable without
    standing up TLS, a socket and Redis: a test can call this and assert the
    registry afterwards. `register()` is an idempotent dict assignment, so
    calling it here as well as in the API and worker processes is always safe.
    """
    from app.modules.agent_push.jobtypes import (
        register_job_types as register_agent_push_job_types,
    )
    from app.modules.agent_update.jobtypes import (
        register_job_types as register_agent_update_job_types,
    )
    from app.modules.backup.handler import register_job_types as register_backup_job_types
    from app.modules.deploy.handler import register_job_types as register_deploy_job_types
    from app.modules.health.jobtypes import register_job_types as register_health_job_types
    from app.modules.inventory.handler import register_job_type
    from app.modules.ipam import register_inventory_hook
    from app.modules.ipam.handler import register_job_types as register_ipam_job_types
    from app.modules.patch.handler import register_job_types as register_patch_job_types
    from app.modules.people import register_inventory_hook as register_people_hook
    from app.modules.provisioning.handler import (
        register_job_types as register_provisioning_job_types,
    )
    from app.modules.remote.jobtypes import register_job_type as register_remote_job_type
    from app.modules.site_server.handler import (
        register_job_types as register_site_server_job_types,
    )

    register_job_type()
    # IPAM observes inventory results through inventory's post-ingest hook, and
    # `net.sweep` results are checked by IPAM's own result handler (§6.4). Both
    # complete HERE, not in the API process.
    register_inventory_hook()
    register_ipam_job_types()
    # People sets a device's primary user from inventory's `console_user`.
    register_people_hook()
    register_remote_job_type()
    register_patch_job_types()
    register_deploy_job_types()
    register_backup_job_types()
    register_provisioning_job_types()
    register_health_job_types()
    # `site.wake`'s result handler rejects a report whose `broadcast` differs
    # from the signed params, and results complete HERE, not in the API process.
    register_site_server_job_types()
    # Agent push install (spec §14). The probe- and install-result handlers
    # complete HERE, in the gateway process, not the API process: they pin the
    # observed host key and correlate the enrolment back to the push.
    register_agent_push_job_types()
    register_agent_update_job_types()


class GatewayServer:
    def __init__(
        self,
        *,
        settings: Settings,
        app_engine: AsyncEngine,
        admin_engine: AsyncEngine,
        job_engine: JobEngine,
        redis: aioredis.Redis,
        registry: ConnectionRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._app_engine = app_engine
        self._admin_engine = admin_engine
        self._job_engine = job_engine
        self._redis = redis
        self.registry = registry or ConnectionRegistry()
        self._subscriber_task: asyncio.Task[None] | None = None
        self._revoke_task: asyncio.Task[None] | None = None
        self._device_org: dict[UUID, UUID] = {}
        self._ca_service: CAService | None = None
        self._internal_server: Server | None = None
        self._relay_task: asyncio.Task[None] | None = None
        # The gateway's service never dispatches or holds a vault: it only
        # drives transitions the relay observes.
        self.relay = ScreenRelayHub(
            app_engine=app_engine,
            redis=redis,
            service=RemoteSessionService(job_engine, SessionRelay(), None),
            registry=RelayRegistry(redis),
        )

    def _ca(self) -> CAService:
        """The CA, built on FIRST USE rather than at construction.

        The gateway signs CSRs for certificate renewal
        (docs/modules/agent/cert-renewal.md §0.6), so this process needs CA
        access — it already mounts the `its-ca` volume in
        `deploy/docker-compose.yml`. Building it lazily means a deployment that
        somehow lacks the key fails at the one rotation that needs it, audited
        and logged, rather than refusing to start and taking the whole fleet's
        connectivity with it.
        """
        if self._ca_service is None:
            self._ca_service = get_ca(self._settings)
        return self._ca_service

    def _ca_or_none(self) -> CAService | None:
        """The CA, or None with an exception logged.

        A gateway that cannot reach its CA must still ADMIT AGENTS: the
        certificates in the field are valid for months and the rotation retries
        on the next connect. Losing the whole fleet's connectivity over a
        signing key would be a far worse outage than a deferred rotation.
        """
        try:
            return self._ca()
        except Exception:  # noqa: BLE001 - see docstring
            logger.exception("no CA available; certificate renewal is deferred")
            return None

    async def start(
        self,
        *,
        host: str,
        port: int,
        certfile: str,
        keyfile: str,
        ca_certs: str,
        relay_internal_host: str | None = None,
        relay_internal_port: int | None = None,
    ) -> Server:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(certfile, keyfile)
        ctx.load_verify_locations(ca_certs)
        ctx.verify_mode = ssl.CERT_REQUIRED

        # Module job types must exist in the gateway process too — agent
        # results complete jobs here, and job types are validated where created.
        register_module_job_types()

        subscriber = DispatchSubscriber(
            self.registry, self._redis, on_delivered=self._on_delivered
        )
        self._subscriber_task = asyncio.create_task(subscriber.run())
        self._revoke_task = asyncio.create_task(
            RevokeSubscriber(self.registry, self._redis).run()
        )

        self._relay_task = asyncio.create_task(self.relay.run_stop_listener())

        server = await serve(
            self._handle,
            host,
            port,
            ssl=ctx,
            select_subprotocol=select_subprotocol,
            max_size=rsp.MAX_MESSAGE_BYTES,
        )
        logger.info("gateway listening on wss://%s:%s/agent", host, port)
        if relay_internal_port is not None:
            self._internal_server = await serve(
                self._handle_internal,
                relay_internal_host or "0.0.0.0",
                relay_internal_port,
                select_subprotocol=select_subprotocol,
                max_size=rsp.MAX_MESSAGE_BYTES,
            )
            logger.info(
                "relay internal listener on ws://%s:%s%s",
                relay_internal_host,
                relay_internal_port,
                RELAY_INTERNAL_PATH,
            )
        return server

    async def stop(self) -> None:
        if self._internal_server is not None:
            self._internal_server.close()
            await self._internal_server.wait_closed()
            self._internal_server = None
        for attr in ("_subscriber_task", "_revoke_task", "_relay_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                setattr(self, attr, None)

    async def _resolve_agent(self, fingerprint: str) -> Agent | None:
        # Pre-tenant lookup: the certificate IS the tenant proof; the admin
        # engine resolves fingerprint -> (device, org) before any org context.
        async with AsyncSession(self._admin_engine) as session:
            return (
                await session.execute(
                    select(Agent).where(Agent.cert_fingerprint == fingerprint)
                )
            ).scalar_one_or_none()

    async def _handle(self, connection: ServerConnection) -> None:
        if _path(connection) == RELAY_PATH:
            await self._handle_relay(connection)
            return
        ssl_object = connection.transport.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        if not der:
            await connection.close(CLOSE_UNAUTHORIZED, "client certificate required")
            return
        fingerprint = hashlib.sha256(der).hexdigest()

        agent = await self._resolve_agent(fingerprint)
        now = datetime.now(UTC)
        if agent is None or agent.cert_revoked or agent.cert_not_after < now:
            logger.warning("rejected connection, fingerprint=%s", fingerprint[:16])
            await connection.close(CLOSE_UNAUTHORIZED, "unrecognized or revoked certificate")
            return

        device_id, org_id = agent.device_id, agent.org_id
        # Read before `_mark_online` stamps it: enrolment leaves it NULL, so
        # NULL here means this is the agent's first connection.
        first_connect = agent.last_seen_at is None
        peer = connection.remote_address
        # Read only now, after the certificate authenticated the agent.
        report = (
            parse_agent_headers(connection.request.headers)
            if connection.request is not None
            else AgentSelfReport(version=None, capabilities=None)
        )
        await self._mark_online(org_id, device_id, agent.id, peer[0] if peer else None, report)
        self._device_org[device_id] = org_id
        self.registry.add(device_id, _WsAdapter(connection))
        try:
            # Certificate rotation is driven from HERE
            # (docs/modules/agent/cert-renewal.md §0.6): the Agent row and its
            # cert_not_after are already loaded, so the check costs an integer
            # comparison, and it runs at exactly the moment the device can
            # actually receive the job. A machine that was off for two months
            # rotates on the connect it comes back on — which is the case a
            # scheduled sweep cannot serve.
            #
            # Anything it creates is left QUEUED and picked up by
            # _drain_queued below, so there is no second publish and no extra
            # round trip.
            await self._advance_cert_renewal(org_id, device_id)
            if first_connect:
                await self._queue_first_inventory(org_id, device_id)
            await self._drain_queued(org_id, device_id, connection)
            async for raw in connection:
                try:
                    message = parse_agent_message(
                        raw if isinstance(raw, str | bytes) else bytes(raw)
                    )
                except ValidationError:
                    logger.warning("unparseable message from device %s", device_id)
                    continue
                try:
                    await handle_agent_message(
                        self._app_engine,
                        self._job_engine,
                        self._redis,
                        message,
                        device_id=device_id,
                        org_id=org_id,
                        # The gateway's own CA -- the one built from the settings
                        # this server was constructed with, so the second leg of
                        # a rotation signs against the same root the connect path
                        # would have used.
                        ca=self._ca_or_none(),
                    )
                except Exception:  # noqa: BLE001 - one bad message must not
                    # kill the connection; the failure is logged and the agent
                    # keeps its channel.
                    logger.exception(
                        "error handling %s from device %s", message.kind, device_id
                    )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.registry.remove(device_id)
            self._device_org.pop(device_id, None)
            await self._mark_offline(org_id, agent.id)

    async def _handle_relay(self, connection: ServerConnection) -> None:
        """The agent leg of a screen session: same certificate check as the
        agent channel, then the relay hub. Marks nothing online and drains no
        jobs; the agent's control channel does that."""
        ssl_object = connection.transport.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        if not der:
            await connection.close(CLOSE_UNAUTHORIZED, "client certificate required")
            return
        agent = await self._resolve_agent(hashlib.sha256(der).hexdigest())
        if agent is None:
            await connection.close(CLOSE_UNAUTHORIZED, "unrecognized certificate")
            return
        if agent.cert_revoked or agent.cert_not_after < datetime.now(UTC):
            await self.relay.refuse_certificate(
                connection,
                org_id=agent.org_id,
                device_id=agent.device_id,
                reason="cert_revoked" if agent.cert_revoked else "cert_expired",
            )
            return
        await self.relay.handle_agent(
            connection,
            AgentIdentity(agent_id=agent.id, org_id=agent.org_id, device_id=agent.device_id),
        )

    async def _handle_internal(self, connection: ServerConnection) -> None:
        if _path(connection) != RELAY_INTERNAL_PATH:
            await connection.close(rsp.CLOSE_PROTOCOL, "unknown path")
            return
        await self.relay.handle_viewer(connection)

    async def _advance_cert_renewal(self, org_id: UUID, device_id: UUID) -> None:
        """One step of certificate rotation, and never a reason to fail a connect."""
        from app.services.cert_renewal import advance_for_device

        ca = self._ca_or_none()
        if ca is None:
            return
        await advance_for_device(
            self._app_engine, self._job_engine, ca, org_id=org_id, device_id=device_id
        )

    async def _queue_first_inventory(self, org_id: UUID, device_id: UUID) -> None:
        """Queue the first `inventory.collect` for a newly enrolled device; the
        drain that follows delivers it. Never a reason to fail a connect: the
        daily sweep collects anything this misses."""
        from app.modules.inventory.schedule import queue_first_collect

        try:
            async with org_session(
                org_id, engine=self._app_engine, clients=ClientScope.all()
            ) as session:
                await queue_first_collect(session, self._job_engine, org_id, device_id)
        except Exception:  # noqa: BLE001 - see docstring
            logger.exception("first inventory collect not queued for device %s", device_id)

    async def _on_delivered(self, device_id: UUID, envelope: dict[str, Any]) -> None:
        """A published job envelope is about to be written to a live
        connection: flip the job to dispatched so state reflects delivery.

        RUNS BEFORE THE WRITE (2026-08-22-job-lifecycle-design.md §3.3) -- see
        `DispatchSubscriber`, which owns the ordering and the reasoning.

        `FOR UPDATE`, AND THE RE-READ INSIDE THE LOCK IS THE POINT. Under READ
        COMMITTED an unlocked SELECT returns the row as it stood before a
        concurrent transaction committed, so this read `queued`, blocked on
        that transaction's lock when it came to UPDATE, and then wrote
        `dispatched` over whatever had just been committed --
        `JobEngine._transition`'s guard cannot fire, because the status it
        checks was read before the other writer landed. A withdrawal racing a
        delivery is exactly that race, and losing it means the console says
        `cancelled` for a job that went to the endpoint.
        """
        org_id = self._device_org.get(device_id)
        job_id = envelope.get("job", {}).get("id")
        if org_id is None or not job_id:
            return
        from app.models import Job, JobStatus

        async with org_session(
            org_id, engine=self._app_engine, clients=ClientScope.all()
        ) as session:
            job = (
                await session.execute(
                    select(Job).where(Job.id == UUID(job_id)).with_for_update()
                )
            ).scalar_one_or_none()
            if job is not None and job.status is JobStatus.queued:
                await self._job_engine.mark_dispatched(session, job)

    async def _drain_queued(
        self, org_id: UUID, device_id: UUID, connection: ServerConnection
    ) -> None:
        envelopes: list[dict[str, Any]] = []
        async with org_session(
            org_id, engine=self._app_engine, clients=ClientScope.all()
        ) as session:
            for job in await self._job_engine.queued_for_device(session, device_id):
                envelopes.append(await self._job_engine.signed_envelope(job))
                await self._job_engine.mark_dispatched(session, job)
        for envelope in envelopes:
            await connection.send(json.dumps(envelope))

    async def _mark_online(
        self,
        org_id: UUID,
        device_id: UUID,
        agent_id: UUID,
        ip: str | None,
        report: AgentSelfReport | None = None,
    ) -> None:
        async with org_session(
            org_id, engine=self._app_engine, clients=ClientScope.all()
        ) as session:
            row = (
                await session.execute(select(Agent).where(Agent.id == agent_id))
            ).scalar_one()
            now = datetime.now(UTC)
            row.status = "online"
            row.last_seen_at = now
            row.last_ip = ip
            if report is not None and report.capabilities is not None:
                row.capabilities = report.capabilities
            if report is not None and report.version is not None:
                previous = row.agent_version or None
                row.agent_version = report.version
                row.agent_version_reported_at = now
                if previous != report.version:
                    # Every version change, including a manual reinstall.
                    await audit_service.record(
                        session,
                        org_id=org_id,
                        actor_type="agent",
                        actor_id=agent_id,
                        action="agent.version_changed",
                        target_type="device",
                        target_id=str(device_id),
                        metadata={"from": previous, "to": report.version},
                    )
            session.add(row)
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="agent",
                actor_id=agent_id,
                action="gateway.connected",
                target_type="device",
                target_id=str(device_id),
                metadata={"ip": ip},
            )

    async def _mark_offline(self, org_id: UUID, agent_id: UUID) -> None:
        try:
            async with org_session(
                org_id, engine=self._app_engine, clients=ClientScope.all()
            ) as session:
                row = (
                    await session.execute(select(Agent).where(Agent.id == agent_id))
                ).scalar_one()
                row.status = "offline"
                row.last_seen_at = datetime.now(UTC)
                session.add(row)
        except Exception:  # noqa: BLE001 - shutdown path must not raise
            logger.exception("failed to mark agent %s offline", agent_id)


async def write_gateway_heartbeat_once(
    redis: aioredis.Redis, instance: str, version: str
) -> None:
    """Write ONE heartbeat for this gateway process (Task 6,
    `app.core.health.heartbeat`), swallowing and logging any failure -- a
    heartbeat write must never take the gateway down.

    Extracted out of `_heartbeat_loop` so a test can invoke exactly one
    iteration against a fake Redis, without a running loop (see
    `tests/test_platform_heartbeat.py`)."""
    try:
        await write_heartbeat(redis, "gateway", instance, version, now=time.time())
    except Exception:  # noqa: BLE001 - see docstring
        logger.error("failed to write gateway heartbeat", exc_info=True)


async def _heartbeat_loop(redis: aioredis.Redis) -> None:
    """Writes this gateway process's own heartbeat every HEARTBEAT_INTERVAL_S
    (Task 6, `app.core.health.heartbeat`) -- the API cannot probe the gateway
    (a mutual-TLS WebSocket server with no HTTP surface), so the gateway is
    the only thing that can report it is alive. A write failure must never
    take the gateway down, so it is swallowed and logged, not raised."""
    instance = os.environ.get("HOSTNAME", uuid4().hex[:8])
    version = os.environ.get("ITS_IMAGE_TAG", "dev")
    while True:
        await write_gateway_heartbeat_once(redis, instance, version)
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


def run_gateway() -> None:  # pragma: no cover - process entrypoint
    """Entrypoint for the gateway container/process."""
    import redis.asyncio as _redis
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings
    from app.core.errors.asyncio_handler import install_asyncio_exception_handler
    from app.core.logging import configure_logging
    from app.core.signing import get_signer

    # C-1: this process used to install the stdlib default `Formatter` via
    # `logging.basicConfig`, which -- unlike `JsonFormatter` -- renders
    # `str(exc)` into the traceback tail whenever a call here passes
    # `exc_info=exc` (e.g. `_ca_or_none`, `_handle`'s per-message catch).
    # Measured: a CSR-signing failure whose exception text carried a
    # connection string leaked it in full under `basicConfig` and would have
    # emitted only `exc_type` under the kernel. `configure_logging()` is what
    # makes this process one of the ones §4 actually covers.
    configure_logging()
    settings = get_settings()

    # The gateway writes audit events too; it must authenticate them with the
    # same HMAC key as the API or their chains diverge (fail closed in prod).
    from app.core.audit import audit_service, load_audit_hmac_key

    audit_service.set_hmac_key(load_audit_hmac_key(settings))

    async def _main() -> None:
        # Spec §6's first carried follow-up, same rationale as `app.main`'s
        # own installation -- see `app.core.errors.asyncio_handler`'s module
        # docstring for the leak this closes. Installed first, before
        # anything else in this coroutine schedules a task or callback that
        # could hit it.
        install_asyncio_exception_handler(asyncio.get_running_loop(), where="gateway")

        gateway_redis = _redis.from_url(settings.redis_url, decode_responses=True)
        server = GatewayServer(
            settings=settings,
            app_engine=create_async_engine(settings.database_url, pool_pre_ping=True),
            admin_engine=create_async_engine(settings.database_admin_url, pool_pre_ping=True),
            job_engine=JobEngine(signer=get_signer(settings)),
            redis=gateway_redis,
        )
        # Task 6: this process's own liveness, on the same Redis client the
        # gateway already holds -- see `_heartbeat_loop` and
        # `app.core.health.heartbeat`. Held in a local so the task survives
        # for the lifetime of `_main` (asyncio only holds a WEAK reference to
        # a task once nothing else does, per `asyncio.create_task`'s own
        # docs) -- `_main` itself lives until `wait_closed()` returns, i.e.
        # for the whole process lifetime.
        _heartbeat_task = asyncio.create_task(_heartbeat_loop(gateway_redis))  # noqa: F841

        handle = await server.start(
            host=os.environ.get("ITS_GATEWAY_HOST", "0.0.0.0"),
            port=int(os.environ.get("ITS_GATEWAY_PORT", "8443")),
            certfile=os.environ["ITS_GATEWAY_TLS_CERT"],
            keyfile=os.environ["ITS_GATEWAY_TLS_KEY"],
            ca_certs=os.environ["ITS_GATEWAY_CLIENT_CA"],
            # Screen-share relay, api side. Internal network only: compose
            # does not publish it and Traefik does not route it.
            relay_internal_host=os.environ.get("ITS_RELAY_INTERNAL_HOST", "0.0.0.0"),
            relay_internal_port=int(os.environ.get("ITS_RELAY_INTERNAL_PORT", "8444")),
        )
        await handle.wait_closed()

    asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    run_gateway()
