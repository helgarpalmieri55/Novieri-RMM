"""Remote-session lifecycle service (docs/modules/remote-control/spec.md).

This is the security-central integration point of the remote-control module. It
ties together policy resolution, the credential vault, the signed-job engine,
the session relay, and the audit log into one state machine:

    awaiting_consent --(consent granted)--> connecting --(agent live)--> active
           |                                     |                          |
           +---------------(consent denied /  end / error / timeout)-------+---> ended

Two protocol families share this state machine:

* ``screen`` (docs/modules/remote-control/spec-screen-share.md): no credential;
  the relay registration lives in Redis (``RelayRegistry``) so the gateway,
  which terminates agent mTLS, can admit the agent leg. The gateway drives the
  consent / active / ended transitions from rsp/1 STATE frames through the
  methods below.
* ``rdp`` / ``vnc`` / ``ssh``: the frozen guacd path, registered in the
  in-process ``SessionRelay``. Refused by the API unless ``remote_guacd_enabled``.

Security invariants enforced here (not optional features):

1. The checked-out credential secret is NEVER written to the RemoteSession row,
   to a log, or returned from any method. Only ``credential_id`` and
   ``relay_token_hash`` are persisted. The plaintext lives solely in an
   in-memory per-session params map on this service instance, consumed later to
   build the guacd connection params.
2. Every privileged transition is audited (requested / started / ended /
   denied) against the immutable hash-chained log.
3. The relay has no self-eviction: every terminal path (consent-deny, end for
   any reason) drops the relay registration and the in-memory params entry, so
   a registered session can never leak.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.rbac import has_permission
from app.core.wire_words import say
from app.gateway.dispatch import publish_job
from app.models import Agent, Device, Organization, RemoteSession, User
from app.modules.agent_push.credential_scope import refuse_push_install_credential
from app.modules.remote.capability import SCREEN_JOB_TYPE, can_share_screen
from app.modules.remote.policy import resolve_policy
from app.modules.remote.relay import (
    RelayRegistration,
    RelayRegistry,
    SessionRelay,
    mint_relay_token,
)
from app.modules.remote.words import SESSION_STATE_WORDS

SCREEN = "screen"
# The relay token is usable for this long after the job is created.
CONNECT_WINDOW_S = 60
DEFAULT_MAX_DURATION_S = 8 * 3600
# An awaiting_consent screen session older than this cannot go live: the agent
# had CONNECT_WINDOW_S to attach, then at most the longest consent timeout
# (120 s) plus the gateway's grace before the gateway ends it.
STALE_AFTER_S = CONNECT_WINDOW_S + 120 + 30

# Default TCP port guacd/agent listens on locally per protocol; the agent maps
# the relayed tunnel to this port on the endpoint.
_LOCAL_PORT: dict[str, int] = {"rdp": 3389, "vnc": 5900, "ssh": 22}

# Legal state transitions. Terminal `ended` never transitions onward. End is
# reachable from every live state (deny / error / timeout / normal completion).
_ALLOWED: dict[str, set[str]] = {
    "awaiting_consent": {"connecting", "ended"},
    "connecting": {"active", "ended"},
    "active": {"ended"},
    "ended": set(),
}

ActorType = Literal["user", "agent", "system"]


class InvalidSessionState(Exception):
    """Raised when a lifecycle method is called for a state that does not permit
    the requested transition."""


class ScreenUnavailable(Exception):
    """A screen session cannot start on this device right now (HTTP 409)."""


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


class _VaultLike(Protocol):
    async def checkout(
        self, session: AsyncSession, *, org_id: UUID, credential_id: UUID
    ) -> str: ...


class _RedisLike(Protocol):
    async def publish(self, channel: str, data: str) -> Any: ...


class RemoteSessionService:
    """Owns the remote-session state machine and its side effects."""

    def __init__(
        self,
        job_engine: _JobEngineLike,
        relay: SessionRelay,
        vault: _VaultLike | None,
        *,
        relay_url: str = "wss://localhost:8443/relay",
        registry: RelayRegistry | None = None,
        screen_max_duration_s: int = DEFAULT_MAX_DURATION_S,
    ) -> None:
        self._jobs = job_engine
        self._relay = relay
        self._vault = vault
        self._relay_url = relay_url
        self._registry = registry
        self._screen_max_duration_s = screen_max_duration_s
        # session_id -> connection params (incl. the plaintext secret for
        # unattended sessions). In-memory only; never persisted or logged.
        self._params: dict[UUID, dict[str, Any]] = {}
        # session_id -> viewer ticket for the api's relay leg. Only its hash
        # leaves this process (in the Redis registration).
        self._viewer_tickets: dict[UUID, str] = {}

    @property
    def relay(self) -> SessionRelay:
        """The process-lived relay this service owns (the media tunnel route
        wires it into the guacd-side pump)."""
        return self._relay

    def connection_params(self, session_id: UUID) -> dict[str, Any] | None:
        """Return the in-memory connection params for a live session, or None
        once the session has reached a terminal state. Used by the media path to
        build guacd connection params; not exposed over the API."""
        return self._params.get(session_id)

    def take_viewer_ticket(self, session_id: UUID) -> str | None:
        """Hand out the viewer ticket once: one viewer leg per session."""
        return self._viewer_tickets.pop(session_id, None)

    def discard_params(self, session_id: UUID) -> None:
        """Idempotently drop the in-memory params (incl. the plaintext secret)
        for a session, without touching the relay registration or the DB row.

        ``end_session`` already does this via ``_terminate_relay``. This is a
        belt-and-suspenders entry point for callers (the tunnel orchestration)
        that must guarantee the secret is gone from memory even when the
        accompanying ``end_session`` write itself raised before reaching
        ``_terminate_relay`` (e.g. a DB error mid-transaction)."""
        self._params.pop(session_id, None)
        self._viewer_tickets.pop(session_id, None)

    def _transition(self, row: RemoteSession, to: str) -> None:
        if to not in _ALLOWED.get(row.state, set()):
            raise InvalidSessionState(
                tr(
                    "{from_state} -> {to_state}",
                    from_state=say(SESSION_STATE_WORDS, row.state),
                    to_state=say(SESSION_STATE_WORDS, to),
                )
            )
        row.state = to

    async def _load(self, session: AsyncSession, session_id: UUID) -> RemoteSession:
        # FOR UPDATE: the api and the gateway can both try to end a screen
        # session; the second must see "ended" rather than end it twice.
        row = (
            await session.execute(
                select(RemoteSession).where(RemoteSession.id == session_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise InvalidSessionState(tr("no remote session {session_id}", session_id=session_id))
        return row

    def _terminate_relay(self, session_id: UUID) -> None:
        """Idempotent cleanup for any terminal path: drop relay registration and
        the in-memory params entry so nothing leaks."""
        self._relay.unregister(session_id)
        self._params.pop(session_id, None)
        self._viewer_tickets.pop(session_id, None)

    async def _terminate(self, row: RemoteSession) -> None:
        self._terminate_relay(row.id)
        if row.protocol == SCREEN and self._registry is not None:
            await self._registry.stop(row.id)

    async def dispatch_pending(
        self, pending: list[dict[str, Any]], redis: _RedisLike
    ) -> None:
        """Perform the external side effects deferred by ``start_session``.

        Called by the caller ONLY AFTER its tenant transaction has committed.
        Registering the relay session, stashing the in-memory params, and
        publishing the signed job to the agent are all durable-state-dependent:
        if the transaction rolled back, none of them may run (otherwise a job
        would reach the agent and the relay would hold a registration for a
        session row that does not exist). See ``queue_ui_event``'s post-commit
        drain pattern in ``app/gateway/dispatch.py``.
        """
        for item in pending:
            session_id: UUID = item["session_id"]
            device_id: UUID = item["device_id"]
            registration: RelayRegistration | None = item.get("registration")
            if registration is not None:
                if self._registry is None:
                    raise RuntimeError(  # i18n-exempt: server misconfiguration, a logged 500
                        "screen sessions need a relay registry"
                    )
                await self._registry.register(registration)
                self._viewer_tickets[session_id] = item["viewer_ticket"]
            else:
                self._relay.register(session_id, device_id, item["token_hash"])
            self._params[session_id] = item["params"]
            await publish_job(redis, device_id, item["envelope"])  # type: ignore[arg-type]

    async def start_session(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        technician_id: UUID,
        device_id: UUID,
        protocol: str,
        requested_mode: str,
        credential_id: UUID | None,
        reason: str,
    ) -> RemoteSession:
        if protocol == SCREEN:
            return await self._start_screen(
                session,
                org_id=org_id,
                technician_id=technician_id,
                device_id=device_id,
                requested_mode=requested_mode,
                credential_id=credential_id,
                reason=reason,
            )
        policy = await resolve_policy(session, org_id, device_id)

        # Unattended only when BOTH the policy allows it and the caller asked
        # for it; otherwise silently downgrade to attended (consent required).
        unattended = policy.allow_unattended and requested_mode == "unattended"
        consent_mode = "unattended" if unattended else "attended"

        # Connection params held only in memory. The plaintext secret is stashed
        # here for the media path and must never reach the row / logs / returns.
        # `record` carries the resolved policy decision so the media path knows
        # whether to tee + finalize a recording (it never re-resolves policy).
        params: dict[str, Any] = {
            "protocol": protocol,
            "consent_mode": consent_mode,
            "record": policy.record_sessions,
        }
        persisted_credential_id: UUID | None = None
        if unattended:
            if credential_id is None:
                raise ValueError(tr("unattended sessions require a credential_id"))
            if self._vault is None:
                raise RuntimeError(  # i18n-exempt: server misconfiguration, a logged 500
                    "this service instance has no vault"
                )
            # A PUSH-INSTALL CREDENTIAL MAY NOT BE USED HERE, and this is the
            # only other place in this repository that presents a stored
            # secret to a machine, so it is the only other place the rule can
            # live (`docs/modules/agent-distribution/spec-push-install.md`
            # §6.2). The push credential is DEDICATED: it authenticates to
            # machines nobody has enrolled, over a transport that cannot
            # authenticate the target on first contact, and reusing it for
            # remote control would mean one theft costs both subsystems at
            # once and rotation becomes impossible to reason about. Refused by
            # TYPE, in the narrowest possible form -- one type is denied, no
            # existing credential type is affected, and the push side enforces
            # the mirror rule in `agent_push/credential_scope.py`.
            await refuse_push_install_credential(
                session, org_id=org_id, credential_id=credential_id
            )
            secret = await self._vault.checkout(
                session, org_id=org_id, credential_id=credential_id
            )
            params["secret"] = secret
            persisted_credential_id = credential_id

        # The RAW relay token is intentionally carried in the SIGNED
        # ``remote.session.open`` job payload below (``job_params["relay_token"]``)
        # — the agent needs it to authenticate to the relay, and the job engine
        # persists job params plaintext in the ``jobs`` table. This is an
        # accepted, mitigated exposure (spec §6/§14): the token is single-use,
        # short-lived (~60s to connect), and additionally device-bound (the relay
        # also requires the agent's mTLS device cert). On the ``remote_sessions``
        # row and in every API response, ONLY ``relay_token_hash`` is ever stored.
        token, token_hash = mint_relay_token()

        row = RemoteSession(
            org_id=org_id,
            device_id=device_id,
            technician_id=technician_id,
            protocol=protocol,
            consent_mode=consent_mode,
            credential_id=persisted_credential_id,
            state="awaiting_consent",
            relay_token_hash=token_hash,
        )
        session.add(row)
        await session.flush()

        job_params: dict[str, Any] = {
            "session_id": str(row.id),
            "protocol": protocol,
            "local_port": _LOCAL_PORT.get(protocol, 0),
            "relay_url": self._relay_url,
            "relay_token": token,
            "consent_mode": consent_mode,
            "consent_prompt": None if unattended else {"reason": reason},
        }
        job = await self._jobs.create(
            session,
            org_id=org_id,
            type="remote.session.open",
            target_device_id=device_id,
            params=job_params,
            requested_by=technician_id,
        )
        envelope = await self._jobs.signed_envelope(job)

        # Defer the external side effects (relay registration, in-memory params,
        # signed-job publish) until AFTER the caller's transaction commits. If
        # the transaction rolls back, none of this runs — no orphaned relay
        # registration and no job dispatched for a session row that never
        # existed. The caller drains this via dispatch_pending() post-commit.
        session.info.setdefault("remote_pending", []).append(
            {
                "session_id": row.id,
                "device_id": device_id,
                "token_hash": token_hash,
                "params": params,
                "envelope": envelope,
            }
        )

        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=technician_id,
            action="remote.session.requested",
            target_type="device",
            target_id=str(device_id),
            metadata={
                "session_id": str(row.id),
                "protocol": protocol,
                "consent_mode": consent_mode,
                "reason": reason,
                "job_id": str(job.id),
            },
        )

        # Invariant #4: the credential ACCESS itself is a privileged action and
        # must be audited. The unattended path checked out the vault secret above
        # (``vault.checkout`` does not itself audit, and the REST
        # ``credential.checked_out`` event is a different code path not reached
        # here). Emit ``remote.session.credential_used`` in the SAME tenant
        # transaction as the row + requested audit for atomicity. The secret is
        # NEVER placed in the metadata — only the credential/session/device ids.
        if unattended:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="user",
                actor_id=technician_id,
                action="remote.session.credential_used",
                target_type="credential",
                target_id=str(credential_id),
                metadata={"session_id": str(row.id), "device_id": str(device_id)},
            )
        return row

    async def _start_screen(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        technician_id: UUID,
        device_id: UUID,
        requested_mode: str,
        credential_id: UUID | None,
        reason: str,
    ) -> RemoteSession:
        """Create a screen session and its signed ``remote.screen.open`` job.

        No credential and no vault: the helper runs in the user's own session
        (spec §3.6). External side effects are queued for ``dispatch_pending``.
        """
        if credential_id is not None:
            raise ValueError(tr("screen sessions take no credential"))

        capabilities = (
            await session.execute(select(Agent.capabilities).where(Agent.device_id == device_id))
        ).scalar_one_or_none()
        # The same rule `GET /devices` serves as `screen_capable`
        # (`capability.py`), so the console's warning and this refusal agree.
        if not can_share_screen(capabilities):
            raise ScreenUnavailable(tr("agent too old to share its screen"))

        # Serialise starts per device so two concurrent requests cannot both
        # pass the one-live-session check below.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('remote-screen:' || :d))"),
            {"d": str(device_id)},
        )
        live = (
            await session.execute(
                select(RemoteSession.id, RemoteSession.state, RemoteSession.created_at).where(
                    RemoteSession.device_id == device_id,
                    RemoteSession.protocol == SCREEN,
                    RemoteSession.state != "ended",
                )
            )
        ).all()
        stale_before = datetime.now(UTC) - timedelta(seconds=STALE_AFTER_S)
        for other_id, other_state, created_at in live:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if other_state == "awaiting_consent" and created_at < stale_before:
                # Its registration expired with connect_by, so it can never
                # go live; without this the device stays blocked until stopped.
                await self.end_session(
                    session, session_id=other_id, reason="expired", actor_type="system"
                )
            else:
                raise ScreenUnavailable(tr("device already has a live screen session"))

        policy = await resolve_policy(session, org_id, device_id)
        # The permission is asked about THE DEVICE'S CLIENT (MSP clients spec
        # §2.1): `remote.unattended` on X lets a technician skip consent on X's
        # machines and on no other client's.
        device_client = (
            await session.execute(
                select(Device.client_id).where(Device.org_id == org_id, Device.id == device_id)
            )
        ).scalar_one_or_none()
        # Unattended needs the policy, the permission and the request; anything
        # less is attended (the existing silent downgrade, spec §3.4).
        unattended = (
            requested_mode == "unattended"
            and policy.allow_unattended
            and device_client is not None
            and await has_permission(
                session, technician_id, "remote.unattended", client_id=device_client
            )
        )
        consent_mode = "unattended" if unattended else "attended"

        technician = (
            await session.execute(
                select(User.display_name, User.email).where(User.id == technician_id)
            )
        ).one()
        org_name = (
            await session.execute(select(Organization.name).where(Organization.id == org_id))
        ).scalar_one()

        token, token_hash = mint_relay_token()
        ticket, ticket_hash = mint_relay_token()
        connect_by = datetime.now(UTC) + timedelta(seconds=CONNECT_WINDOW_S)
        max_duration_s = self._screen_max_duration_s

        row = RemoteSession(
            org_id=org_id,
            device_id=device_id,
            technician_id=technician_id,
            protocol=SCREEN,
            consent_mode=consent_mode,
            state="awaiting_consent",
            relay_token_hash=token_hash,
            input_enabled=policy.allow_input,
        )
        session.add(row)
        await session.flush()

        # The raw token rides only inside the signed params (spec §3.2); the
        # job type's redactor and scrubber keep it out of the API, and out of
        # the row once the job is over.
        job_params: dict[str, Any] = {
            "session_id": str(row.id),
            "relay_url": self._relay_url,
            "relay_token": token,
            "connect_by": connect_by.isoformat(),
            "consent_mode": consent_mode,
            "consent": {
                "technician": technician.display_name or technician.email,
                "org": org_name,
                "reason": reason,
                "timeout_s": policy.consent_timeout_s,
            },
            "max_duration_s": max_duration_s,
            "allow_input": policy.allow_input,
            "record": policy.record_sessions,
            "desktop": "user",
        }
        job = await self._jobs.create(
            session,
            org_id=org_id,
            type=SCREEN_JOB_TYPE,
            target_device_id=device_id,
            params=job_params,
            requested_by=technician_id,
            ttl_s=max_duration_s + policy.consent_timeout_s + 120,
        )
        envelope = await self._jobs.signed_envelope(job)

        session.info.setdefault("remote_pending", []).append(
            {
                "session_id": row.id,
                "device_id": device_id,
                "registration": RelayRegistration(
                    session_id=row.id,
                    org_id=org_id,
                    device_id=device_id,
                    token_hash=token_hash,
                    viewer_ticket_hash=ticket_hash,
                    connect_by=connect_by,
                    consent_mode=consent_mode,
                    allow_input=policy.allow_input,
                    max_duration_s=max_duration_s,
                    consent_timeout_s=policy.consent_timeout_s,
                ),
                "viewer_ticket": ticket,
                "params": {
                    "protocol": SCREEN,
                    "consent_mode": consent_mode,
                    "record": policy.record_sessions,
                    "recording_max_bytes": policy.recording_max_bytes,
                },
                "envelope": envelope,
            }
        )

        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=technician_id,
            action="remote.session.requested",
            target_type="device",
            target_id=str(device_id),
            metadata={
                "session_id": str(row.id),
                "protocol": SCREEN,
                "consent_mode": consent_mode,
                "requested_mode": requested_mode,
                "reason": reason,
                "job_id": str(job.id),
            },
        )
        return row

    async def record_event(
        self,
        session: AsyncSession,
        *,
        session_id: UUID,
        action: str,
        actor_type: ActorType,
        actor_id: UUID | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Audit a session event reported over the relay (consent, input
        changes, privacy blanking). Written by the gateway, which sees them.
        A ``user`` actor without an id is the session's technician."""
        row = await self._load(session, session_id)
        if actor_type == "user" and actor_id is None:
            actor_id = row.technician_id
        await audit_service.record(
            session,
            org_id=row.org_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target_type="session",
            target_id=str(row.id),
            metadata={"device_id": str(row.device_id), **(metadata or {})},
        )

    async def set_input_enabled(
        self, session: AsyncSession, *, session_id: UUID, enabled: bool
    ) -> None:
        row = await self._load(session, session_id)
        row.input_enabled = enabled
        await session.flush()

    async def on_consent(
        self, session: AsyncSession, *, session_id: UUID, accepted: bool
    ) -> None:
        row = await self._load(session, session_id)
        if accepted:
            self._transition(row, "connecting")
            await session.flush()
            return

        # Rejected: terminal. End the session, audit the denial, clean up relay.
        self._transition(row, "ended")
        row.ended_at = datetime.now(UTC)
        row.end_reason = "denied_consent"
        await audit_service.record(
            session,
            org_id=row.org_id,
            actor_type="user",
            actor_id=row.technician_id,
            action="remote.session.denied",
            target_type="session",
            target_id=str(row.id),
            metadata={"device_id": str(row.device_id)},
        )
        await session.flush()
        await self._terminate(row)

    async def mark_active(
        self, session: AsyncSession, *, session_id: UUID, audit_ref: str
    ) -> None:
        row = await self._load(session, session_id)
        self._transition(row, "active")
        row.started_at = datetime.now(UTC)
        event = await audit_service.record(
            session,
            org_id=row.org_id,
            actor_type="user",
            actor_id=row.technician_id,
            action="remote.session.started",
            target_type="session",
            target_id=str(row.id),
            metadata={"device_id": str(row.device_id), "audit_ref": audit_ref},
        )
        row.audit_event_id = event.id
        await session.flush()

    async def end_session(
        self,
        session: AsyncSession,
        *,
        session_id: UUID,
        reason: str,
        actor_type: ActorType = "user",
        actor_id: UUID | None = None,
    ) -> None:
        """End a live session. The actor defaults to the session's technician;
        the stop route names whoever stopped it (phase 3 design §5.3), and the
        gateway names the agent when the endpoint side ended it. Audited in
        the session's client."""
        row = await self._load(session, session_id)
        # Read before anything is written: a flush expires it
        # (`server_onupdate`, written by trigger), and an async session cannot
        # lazy-load it.
        client_id = row.client_id
        self._transition(row, "ended")
        ended_at = datetime.now(UTC)
        row.ended_at = ended_at
        row.end_reason = reason
        duration_s: float | None = None
        if row.started_at is not None:
            started = row.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            duration_s = (ended_at - started).total_seconds()
        if actor_type == "user" and actor_id is None:
            actor_id = row.technician_id
        await audit_service.record(
            session,
            org_id=row.org_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="remote.session.ended",
            target_type="session",
            target_id=str(row.id),
            metadata={
                "device_id": str(row.device_id),
                "reason": reason,
                "duration_s": duration_s,
            },
            client_id=client_id,
        )
        await session.flush()
        await self._terminate(row)


async def end_session_from_result(
    session: AsyncSession, *, session_id: str, device_id: UUID, reason: str
) -> None:
    """End a screen session from its job result, unless it has already ended.

    Runs in the gateway's job-completion transaction, where no service
    instance exists. By then the relay registration has been consumed or has
    expired with ``connect_by``, so there is nothing else to clean up.
    """
    try:
        parsed = UUID(session_id)
    except ValueError:
        return
    row = (
        await session.execute(
            select(RemoteSession)
            .where(
                RemoteSession.id == parsed,
                RemoteSession.device_id == device_id,
                RemoteSession.protocol == SCREEN,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.state == "ended":
        return
    # Read before the writes below (phase 3 design §5.3: the same action, the
    # same client, whichever writer records it).
    client_id = row.client_id
    row.state = "ended"
    row.ended_at = datetime.now(UTC)
    row.end_reason = reason
    await audit_service.record(
        session,
        org_id=row.org_id,
        actor_type="agent",
        actor_id=None,
        action="remote.session.ended",
        target_type="session",
        target_id=str(row.id),
        metadata={"device_id": str(row.device_id), "reason": reason, "source": "job_result"},
        client_id=client_id,
    )
    await session.flush()
