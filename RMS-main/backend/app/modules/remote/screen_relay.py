"""The screen-share relay, as run by the gateway process (spec-screen-share.md §2.2, §3).

Two legs meet here, joined by session id:

* the **agent leg**: the agent's second outbound mTLS WebSocket on ``/relay``.
  Admitted only if its certificate resolves to a live agent, that agent's
  device is the one the session was registered for, the one-shot relay token
  matches, ``connect_by`` has not passed and the session is still waiting for
  it. The registration is consumed (GETDEL) before any of that is checked, so
  a token is good for one attempt.
* the **viewer leg**: the api's WebSocket on the internal listener
  (``/relay-internal``), admitted by the viewer ticket.

The gateway does not parse frame bodies except STATE (which drives the
session state machine and the audit log) and CONTROL (so view-only is enforced
here as well as on the endpoint). It drops frames that travel in the wrong
direction, media before consent, and input while the session is view-only.

Joins live in this process only: one gateway process serves both legs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.audit import audit_service
from app.core.tenancy import org_session
from app.models import RemoteSession
from app.modules.remote import rsp
from app.modules.remote.jobtypes import end_reason_for_outcome
from app.modules.remote.relay import STOP_PATTERN, RelayRegistration, RelayRegistry, token_hash
from app.modules.remote.service import ActorType, InvalidSessionState, RemoteSessionService

logger = logging.getLogger("its.gateway.relay")

RELAY_PATH = "/relay"
RELAY_INTERNAL_PATH = "/relay-internal"

AUTH_TIMEOUT_S = 10.0
# How long a viewer waits past connect_by for the agent to arrive.
AGENT_GRACE_S = 5.0
# How long an attached agent waits for a viewer beyond the consent window.
VIEWER_GRACE_S = 120.0
# How long past the consent timeout the gateway waits for the agent to report
# an answer before it ends the session itself.
CONSENT_GRACE_S = 10.0
_BACKLOG_FRAMES = 64

# Reasons an agent may name in STATE ended{reason}, as stored on the row.
_END_REASONS = frozenset(
    {
        "completed",
        "ended_by_user",
        "consent_timeout",
        "denied_consent",
        "no_user_session",
        "capture_unavailable",
        "permission_missing",
        "unsupported_display",
        "expired",
        "error",
    }
)


class Conn(Protocol):
    """The subset of a websockets connection the relay uses."""

    @property
    def subprotocol(self) -> str | None: ...

    async def recv(self) -> str | bytes: ...

    async def send(self, message: bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...

    def __aiter__(self) -> Any: ...


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: UUID
    org_id: UUID
    device_id: UUID


@dataclass(eq=False)
class _Join:
    reg: RelayRegistration
    agent: AgentIdentity
    agent_conn: Conn
    viewer_conn: Conn | None = None
    viewer_claimed: bool = False
    consented: bool = False
    active: bool = False
    input_enabled: bool = True
    user_revoked_control: bool = False
    end_reason: str | None = None
    ended_in_db: bool = False
    torn_down: bool = False
    dropped: int = 0
    backlog: deque[bytes] = field(default_factory=lambda: deque(maxlen=_BACKLOG_FRAMES))
    viewer_attached: asyncio.Event = field(default_factory=asyncio.Event)
    consent_given: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)


class ScreenRelayHub:
    """Admits and joins the two legs of every screen session on this gateway."""

    def __init__(
        self,
        *,
        app_engine: AsyncEngine,
        redis: Any,
        service: RemoteSessionService,
        registry: RelayRegistry,
        auth_timeout_s: float = AUTH_TIMEOUT_S,
        agent_grace_s: float = AGENT_GRACE_S,
        viewer_grace_s: float = VIEWER_GRACE_S,
        consent_grace_s: float = CONSENT_GRACE_S,
    ) -> None:
        self._app_engine = app_engine
        self._redis = redis
        self._service = service
        self._registry = registry
        self._auth_timeout_s = auth_timeout_s
        self._agent_grace_s = agent_grace_s
        self._viewer_grace_s = viewer_grace_s
        self._consent_grace_s = consent_grace_s
        self._joins: dict[UUID, _Join] = {}
        # Session ids a viewer is waiting on; set when the agent attaches.
        self._agent_arrived: dict[UUID, asyncio.Event] = {}

    # ------------------------------------------------------------------ #
    # audit helpers
    # ------------------------------------------------------------------ #

    async def _audit(
        self,
        org_id: UUID,
        action: str,
        *,
        target_id: str,
        actor_type: ActorType = "agent",
        actor_id: UUID | None = None,
        metadata: dict[str, Any],
    ) -> None:
        try:
            async with org_session(org_id, engine=self._app_engine) as s:
                await audit_service.record(
                    s,
                    org_id=org_id,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action=action,
                    target_type="session",
                    target_id=target_id,
                    metadata=metadata,
                )
        except Exception:  # noqa: BLE001 - a refusal is still a refusal
            logger.exception("failed to audit %s", action)

    async def _refuse(
        self,
        conn: Conn,
        *,
        org_id: UUID | None,
        action: str,
        session_id: str,
        reason: str,
        code: int = rsp.CLOSE_REFUSED,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        logger.warning("relay refused (%s): session=%s reason=%s", action, session_id, reason)
        if org_id is not None:
            await self._audit(
                org_id,
                action,
                target_id=session_id,
                metadata={"reason": reason, **(metadata or {})},
            )
        with contextlib.suppress(Exception):
            await conn.close(code, reason)

    async def refuse_certificate(
        self, conn: Conn, *, org_id: UUID, device_id: UUID, reason: str
    ) -> None:
        """A known agent whose certificate is revoked or expired."""
        await self._refuse(
            conn,
            org_id=org_id,
            action="remote.relay.identity_mismatch",
            session_id="",
            reason=reason,
            code=rsp.CLOSE_UNAUTHORIZED,
            metadata={"device_id": str(device_id), "leg": "agent"},
        )

    # ------------------------------------------------------------------ #
    # agent leg
    # ------------------------------------------------------------------ #

    async def handle_agent(self, conn: Conn, agent: AgentIdentity) -> None:
        if conn.subprotocol != rsp.SUBPROTOCOL:
            await conn.close(rsp.CLOSE_PROTOCOL, f"subprotocol {rsp.SUBPROTOCOL} required")
            return
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=self._auth_timeout_s)
        except TimeoutError:
            await conn.close(rsp.CLOSE_TIMEOUT, "no AUTH")
            return
        except Exception:  # noqa: BLE001 - peer went away before AUTH
            return

        try:
            if not isinstance(raw, bytes):
                raise rsp.FrameError(  # i18n-exempt: caught in the same try, leg refused
                    "text frame"
                )
            sid_text, token = rsp.parse_auth(rsp.decode(raw), secret_field="token")
            session_id = UUID(sid_text)
        except (rsp.FrameError, ValueError):
            await self._refuse(
                conn,
                org_id=agent.org_id,
                action="remote.relay.token_rejected",
                session_id="",
                reason="malformed_auth",
                metadata={"device_id": str(agent.device_id), "leg": "agent"},
            )
            return

        reg = await self._registry.consume(session_id)
        common = {"device_id": str(agent.device_id), "leg": "agent"}
        if reg is None:
            # Never registered, already used, or past connect_by (the TTL).
            await self._refuse(
                conn,
                org_id=agent.org_id,
                action="remote.relay.token_rejected",
                session_id=str(session_id),
                reason="no_registration",
                metadata=common,
            )
            return
        if reg.device_id != agent.device_id or reg.org_id != agent.org_id:
            await self._refuse(
                conn,
                org_id=reg.org_id,
                action="remote.relay.identity_mismatch",
                session_id=str(session_id),
                reason="device_mismatch",
                metadata={**common, "expected_device_id": str(reg.device_id)},
            )
            return
        if not hmac.compare_digest(token_hash(token), reg.token_hash):
            await self._refuse(
                conn,
                org_id=reg.org_id,
                action="remote.relay.token_rejected",
                session_id=str(session_id),
                reason="bad_token",
                metadata=common,
            )
            return
        if datetime.now(UTC) > reg.connect_by:
            await self._refuse(
                conn,
                org_id=reg.org_id,
                action="remote.relay.token_rejected",
                session_id=str(session_id),
                reason="expired",
                metadata=common,
            )
            return
        state = await self._session_state(reg)
        if state not in ("awaiting_consent", "connecting"):
            await self._refuse(
                conn,
                org_id=reg.org_id,
                action="remote.relay.token_rejected",
                session_id=str(session_id),
                reason="session_state",
                metadata={**common, "state": state},
            )
            return
        if session_id in self._joins:
            await conn.close(rsp.CLOSE_CONFLICT, "agent already attached")
            return

        join = _Join(reg=reg, agent=agent, agent_conn=conn, input_enabled=reg.allow_input)
        if reg.consent_mode == "unattended":
            join.consented = True
            join.consent_given.set()
            if state == "awaiting_consent":
                async with org_session(reg.org_id, engine=self._app_engine) as s:
                    await self._service.on_consent(s, session_id=session_id, accepted=True)
        self._joins[session_id] = join
        waiting = self._agent_arrived.pop(session_id, None)
        if waiting is not None:
            waiting.set()

        try:
            await conn.send(rsp.auth_ok_frame(str(session_id)))
            await self._run_agent(join)
        finally:
            await self._teardown(join, default_reason="agent_disconnected")

    async def _session_state(self, reg: RelayRegistration) -> str | None:
        async with org_session(reg.org_id, engine=self._app_engine) as s:
            return (
                await s.execute(
                    select(RemoteSession.state).where(RemoteSession.id == reg.session_id)
                )
            ).scalar_one_or_none()

    async def _run_agent(self, join: _Join) -> None:
        """Pump agent frames until the agent leaves, the session runs past its
        maximum, or no viewer arrives in time."""
        watchdog = asyncio.create_task(self._viewer_watchdog(join))
        try:
            async with asyncio.timeout(join.reg.max_duration_s + join.reg.consent_timeout_s):
                async for raw in join.agent_conn:
                    await self._on_agent_message(join, raw)
                    if join.torn_down:
                        return
        except TimeoutError:
            join.end_reason = join.end_reason or "expired"
        except Exception:  # noqa: BLE001 - socket errors end the session
            logger.debug("agent leg ended", exc_info=True)
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog

    async def _viewer_watchdog(self, join: _Join) -> None:
        """End a join nobody is watching, or one whose agent never reports a
        consent answer. The endpoint enforces its own timeout; this bounds
        the session if it does not."""
        consent_window = join.reg.consent_timeout_s + self._consent_grace_s
        try:
            await asyncio.wait_for(join.consent_given.wait(), timeout=consent_window)
        except TimeoutError:
            # Shielded: _run_agent cancels this task once the agent leg closes.
            await asyncio.shield(self._expire_consent(join))
            return
        try:
            await asyncio.wait_for(
                join.viewer_attached.wait(),
                timeout=join.reg.consent_timeout_s + self._viewer_grace_s,
            )
        except TimeoutError:
            join.end_reason = join.end_reason or "expired"
            with contextlib.suppress(Exception):
                await join.agent_conn.close(rsp.CLOSE_TIMEOUT, "no viewer")

    async def _expire_consent(self, join: _Join) -> None:
        if join.torn_down:
            return
        join.end_reason = join.end_reason or "consent_timeout"
        try:
            await self._session_event(
                join, "remote.session.consent_timeout", {"source": "gateway"}
            )
        except Exception:  # noqa: BLE001 - still end the session
            logger.exception("failed to audit consent timeout")
        await self._teardown(join, default_reason="consent_timeout")

    async def _on_agent_message(self, join: _Join, raw: str | bytes) -> None:
        if not isinstance(raw, bytes):
            join.dropped += 1
            return
        try:
            frame = rsp.decode(raw)
        except rsp.FrameError:
            join.dropped += 1
            return
        if not rsp.agent_to_viewer(frame.type):
            join.dropped += 1
            return
        if frame.type == rsp.STATE:
            state = rsp.parse_state(frame)
            await self._forward_to_viewer(join, raw)
            if state is not None:
                await self._on_state(join, state)
            return
        if not join.consented:
            # Nothing but STATE reaches a viewer before the user has allowed it.
            join.dropped += 1
            return
        if frame.type == rsp.HELLO and not join.active:
            join.active = True
            async with org_session(join.reg.org_id, engine=self._app_engine) as s:
                await self._service.mark_active(
                    s, session_id=join.reg.session_id, audit_ref="rsp/1"
                )
        await self._forward_to_viewer(join, raw)

    async def _forward_to_viewer(self, join: _Join, raw: bytes) -> None:
        if join.viewer_conn is None:
            join.backlog.append(raw)
            return
        with contextlib.suppress(Exception):
            await join.viewer_conn.send(raw)

    async def _session_event(
        self, join: _Join, action: str, metadata: dict[str, Any] | None = None
    ) -> None:
        async with org_session(join.reg.org_id, engine=self._app_engine) as s:
            await self._service.record_event(
                s,
                session_id=join.reg.session_id,
                action=action,
                actor_type="agent",
                actor_id=join.agent.agent_id,
                metadata=metadata,
            )

    async def _on_state(self, join: _Join, state: rsp.State) -> None:
        sid = join.reg.session_id
        attended = join.reg.consent_mode == "attended"
        name = state.state
        if name == "consent_pending":
            await self._session_event(join, "remote.session.consent_prompted")
        elif name == "consent_granted" and attended and not join.consented:
            async with org_session(join.reg.org_id, engine=self._app_engine) as s:
                await self._service.on_consent(s, session_id=sid, accepted=True)
            await self._session_event(join, "remote.session.consent_granted")
            join.consented = True
            join.consent_given.set()
        elif name == "consent_denied" and attended and not join.consented:
            join.end_reason = "denied_consent"
            join.ended_in_db = True
            async with org_session(join.reg.org_id, engine=self._app_engine) as s:
                await self._service.on_consent(s, session_id=sid, accepted=False)
            await self._teardown(join, default_reason="denied_consent")
        elif name == "consent_timeout" and attended and not join.consented:
            await self._session_event(join, "remote.session.consent_timeout")
            join.end_reason = "consent_timeout"
            await self._teardown(join, default_reason="consent_timeout")
        elif name == "control_revoked_by_user":
            join.user_revoked_control = True
            if join.input_enabled:
                join.input_enabled = False
                async with org_session(join.reg.org_id, engine=self._app_engine) as s:
                    await self._service.set_input_enabled(s, session_id=sid, enabled=False)
                await self._session_event(
                    join, "remote.session.input_disabled", {"by": "endpoint_user"}
                )
        elif name in ("privacy_blank_on", "privacy_blank_off"):
            await self._session_event(
                join, "remote.session.privacy_blank", {"state": name.rsplit("_", 1)[1]}
            )
        elif name == "ended":
            reason = state.reason or "completed"
            if reason not in _END_REASONS:
                reason = end_reason_for_outcome(reason)
            if reason == "ended_by_user":
                await self._session_event(join, "remote.session.ended_by_user")
            join.end_reason = reason
        # Anything else (input_desktop_unavailable, a newer state) is forwarded only.

    # ------------------------------------------------------------------ #
    # viewer leg
    # ------------------------------------------------------------------ #

    async def handle_viewer(self, conn: Conn) -> None:
        if conn.subprotocol != rsp.SUBPROTOCOL:
            await conn.close(rsp.CLOSE_PROTOCOL, f"subprotocol {rsp.SUBPROTOCOL} required")
            return
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=self._auth_timeout_s)
            if not isinstance(raw, bytes):
                raise rsp.FrameError(  # i18n-exempt: caught in the same try, leg refused
                    "text frame"
                )
            sid_text, ticket = rsp.parse_auth(rsp.decode(raw), secret_field="ticket")
            session_id = UUID(sid_text)
        except TimeoutError:
            await conn.close(rsp.CLOSE_TIMEOUT, "no AUTH")
            return
        except (rsp.FrameError, ValueError):
            await conn.close(rsp.CLOSE_PROTOCOL, "malformed AUTH")
            return
        except Exception:  # noqa: BLE001 - peer went away before AUTH
            return

        join = self._joins.get(session_id)
        reg = join.reg if join is not None else await self._registry.peek(session_id)
        if reg is None or not hmac.compare_digest(token_hash(ticket), reg.viewer_ticket_hash):
            await self._refuse(
                conn,
                org_id=reg.org_id if reg is not None else None,
                action="remote.relay.token_rejected",
                session_id=str(session_id),
                reason="bad_ticket" if reg is not None else "no_registration",
                metadata={"leg": "viewer"},
            )
            return

        if join is not None and (join.viewer_claimed or join.torn_down):
            await conn.close(rsp.CLOSE_CONFLICT, "viewer already attached")
            return
        # Admitted now; the agent may still be up to connect_by away.
        try:
            await conn.send(rsp.auth_ok_frame(str(session_id)))
        except Exception:  # noqa: BLE001 - viewer went away
            return
        if join is None:
            join = await self._wait_for_agent(reg)
            if join is None:
                await self._expire_unconnected(reg)
                with contextlib.suppress(Exception):
                    await conn.close(rsp.CLOSE_TIMEOUT, "agent did not connect")
                return
        if join.viewer_claimed or join.torn_down:
            await conn.close(rsp.CLOSE_CONFLICT, "viewer already attached")
            return

        join.viewer_claimed = True
        join.viewer_attached.set()
        try:
            # Frames that arrive while the backlog drains join its tail, so the
            # viewer is switched to direct forwarding only once it is empty.
            while join.backlog:
                await conn.send(join.backlog.popleft())
            join.viewer_conn = conn
            async for message in conn:
                await self._on_viewer_message(join, message)
                if join.torn_down:
                    return
        except Exception:  # noqa: BLE001 - socket errors end the session
            logger.debug("viewer leg ended", exc_info=True)
        finally:
            await self._teardown(join, default_reason="completed", ended_by_viewer=True)

    async def _wait_for_agent(self, reg: RelayRegistration) -> _Join | None:
        event = self._agent_arrived.setdefault(reg.session_id, asyncio.Event())
        remaining = (reg.connect_by - datetime.now(UTC)).total_seconds() + self._agent_grace_s
        try:
            await asyncio.wait_for(event.wait(), timeout=max(remaining, 0.0))
        except TimeoutError:
            return None
        finally:
            if self._agent_arrived.get(reg.session_id) is event:
                self._agent_arrived.pop(reg.session_id, None)
        return self._joins.get(reg.session_id)

    async def _expire_unconnected(self, reg: RelayRegistration) -> None:
        try:
            async with org_session(reg.org_id, engine=self._app_engine) as s:
                await self._service.end_session(
                    s, session_id=reg.session_id, reason="expired", actor_type="system"
                )
        except InvalidSessionState:
            pass

    async def _on_viewer_message(self, join: _Join, raw: str | bytes) -> None:
        if not isinstance(raw, bytes):
            join.dropped += 1
            return
        try:
            frame = rsp.decode(raw)
        except rsp.FrameError:
            join.dropped += 1
            return
        if not rsp.viewer_to_agent(frame.type) or not join.consented:
            join.dropped += 1
            return
        if frame.type == rsp.CONTROL:
            wanted = rsp.parse_control(frame)
            if wanted is None:
                join.dropped += 1
                return
            if wanted and (join.user_revoked_control or not join.reg.allow_input):
                # Only a new session with a new prompt can give control back.
                join.dropped += 1
                return
            if wanted != join.input_enabled:
                join.input_enabled = wanted
                async with org_session(join.reg.org_id, engine=self._app_engine) as s:
                    await self._service.set_input_enabled(
                        s, session_id=join.reg.session_id, enabled=wanted
                    )
                    await self._service.record_event(
                        s,
                        session_id=join.reg.session_id,
                        action=(
                            "remote.session.input_enabled"
                            if wanted
                            else "remote.session.input_disabled"
                        ),
                        actor_type="user",
                        actor_id=None,  # the session's technician
                        metadata={"by": "technician"},
                    )
        elif frame.type in rsp.INPUT_TYPES and not join.input_enabled:
            join.dropped += 1
            return
        with contextlib.suppress(Exception):
            await join.agent_conn.send(raw)

    # ------------------------------------------------------------------ #
    # teardown and external stop
    # ------------------------------------------------------------------ #

    async def _teardown(
        self, join: _Join, *, default_reason: str, ended_by_viewer: bool = False
    ) -> None:
        if join.torn_down:
            await join.finished.wait()
            return
        join.torn_down = True
        try:
            if self._joins.get(join.reg.session_id) is join:
                del self._joins[join.reg.session_id]
            reason = join.end_reason or default_reason
            if not join.ended_in_db:
                join.ended_in_db = True
                # The viewer leaving is the technician ending the session;
                # anything else was decided on the endpoint side.
                by_viewer = ended_by_viewer and join.end_reason is None
                try:
                    async with org_session(join.reg.org_id, engine=self._app_engine) as s:
                        await self._service.end_session(
                            s,
                            session_id=join.reg.session_id,
                            reason=reason,
                            actor_type="user" if by_viewer else "agent",
                            actor_id=None if by_viewer else join.agent.agent_id,
                        )
                except InvalidSessionState:
                    pass  # already ended by the api (stop) or a job result
                except Exception:  # noqa: BLE001 - still close both legs
                    logger.exception("failed to end screen session %s", join.reg.session_id)
            if join.dropped:
                logger.info(
                    "screen session %s: dropped %d frames", join.reg.session_id, join.dropped
                )
            for conn in (join.viewer_conn, join.agent_conn):
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close(rsp.CLOSE_ENDED, reason)
        finally:
            join.finished.set()

    async def stop(self, session_id: UUID, reason: str = "completed") -> None:
        """Tear down a live join (the session was stopped elsewhere)."""
        join = self._joins.get(session_id)
        if join is not None:
            await self._teardown(join, default_reason=reason)

    async def stop_device(self, device_id: UUID) -> None:
        """Tear down every join for a device whose certificate was revoked."""
        for join in [j for j in self._joins.values() if j.agent.device_id == device_id]:
            join.end_reason = join.end_reason or "error"
            await self._teardown(join, default_reason="error")

    async def run_stop_listener(self) -> None:
        """Follow ``remote:stop:*`` (api stop) and certificate revocation."""
        from app.gateway.dispatch import REVOKE_CHANNEL

        pubsub = self._redis.pubsub()
        await pubsub.psubscribe(STOP_PATTERN)
        await pubsub.subscribe(REVOKE_CHANNEL)
        try:
            async for message in pubsub.listen():
                kind = message.get("type")
                channel = message.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode()
                try:
                    if kind == "pmessage" and isinstance(channel, str):
                        await self.stop(UUID(channel.rsplit(":", 1)[1]))
                    elif kind == "message" and channel == REVOKE_CHANNEL:
                        data = message.get("data")
                        if isinstance(data, bytes):
                            data = data.decode()
                        await self.stop_device(UUID(json.loads(data)["device_id"]))
                except (ValueError, KeyError, TypeError):
                    continue
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()

