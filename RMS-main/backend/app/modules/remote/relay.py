"""Relay registration and byte glue for remote-control sessions.

Two things live here:

* ``RelayRegistry``: the Redis-backed registration for screen sessions
  (docs/modules/remote-control/spec-screen-share.md §2.2). The api writes it
  after its transaction commits; the gateway, which terminates agent mTLS,
  consumes it with GETDEL on agent attach, so the relay token is one-shot by
  construction and dies with ``connect_by`` via the key's TTL. Only hashes of
  the relay token and the viewer ticket are stored.
* ``SessionRelay``: the in-memory registry of the frozen guacd path. It only
  works inside one process and has no production agent caller; it stays until
  guacd is removed (spec-screen-share.md, operator decisions 2026-09-23).

Reader/writer parameters are duck-typed to the subset of the
``asyncio.StreamReader`` / ``asyncio.StreamWriter`` interface used here, so
tests may pass real asyncio streams or minimal fakes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

_CHUNK_SIZE = 65536


@runtime_checkable
class StreamReaderLike(Protocol):
    async def read(self, n: int = -1) -> bytes: ...


@runtime_checkable
class StreamWriterLike(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


def mint_relay_token() -> tuple[str, str]:
    """Generate a new per-session relay token and return `(token, sha256_hex)`.

    The raw token is handed to the agent (via the signed job params) and to
    guacd's connection setup; only the hash is ever persisted server-side.
    """
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    """SHA-256 hex digest of a relay token, used for storage/comparison."""
    return hashlib.sha256(token.encode()).hexdigest()


def registration_key(session_id: UUID) -> str:
    return f"remote:relay:{session_id}"


def stop_channel(session_id: UUID) -> str:
    return f"remote:stop:{session_id}"


STOP_PATTERN = "remote:stop:*"


@dataclass(frozen=True)
class RelayRegistration:
    """What the gateway needs to admit both legs of one screen session.

    Also carries the policy bits the gateway enforces on its own (input
    allowed, duration, how long to wait for consent), so a compromised or
    buggy endpoint cannot widen them by sending frames.
    """

    session_id: UUID
    org_id: UUID
    device_id: UUID
    token_hash: str
    viewer_ticket_hash: str
    connect_by: datetime
    consent_mode: str
    allow_input: bool
    max_duration_s: int
    consent_timeout_s: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "session_id": str(self.session_id),
                "org_id": str(self.org_id),
                "device_id": str(self.device_id),
                "token_hash": self.token_hash,
                "viewer_ticket_hash": self.viewer_ticket_hash,
                "connect_by": self.connect_by.isoformat(),
                "consent_mode": self.consent_mode,
                "allow_input": self.allow_input,
                "max_duration_s": self.max_duration_s,
                "consent_timeout_s": self.consent_timeout_s,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> RelayRegistration | None:
        try:
            data = json.loads(raw)
            return cls(
                session_id=UUID(data["session_id"]),
                org_id=UUID(data["org_id"]),
                device_id=UUID(data["device_id"]),
                token_hash=str(data["token_hash"]),
                viewer_ticket_hash=str(data["viewer_ticket_hash"]),
                connect_by=datetime.fromisoformat(data["connect_by"]),
                consent_mode=str(data["consent_mode"]),
                allow_input=bool(data["allow_input"]),
                max_duration_s=int(data["max_duration_s"]),
                consent_timeout_s=int(data["consent_timeout_s"]),
            )
        except (ValueError, KeyError, TypeError):
            return None


class RelayRegistry:
    """Screen-session relay registrations in Redis, keyed by session id."""

    def __init__(self, redis: Any) -> None:
        # A redis.asyncio client with decode_responses=True.
        self._redis = redis

    async def register(self, reg: RelayRegistration) -> None:
        ttl_ms = int((reg.connect_by - datetime.now(UTC)).total_seconds() * 1000)
        if ttl_ms <= 0:
            # Already unusable; writing it would only let the gateway refuse
            # it later with a less precise reason.
            return
        await self._redis.set(registration_key(reg.session_id), reg.to_json(), px=ttl_ms)

    async def peek(self, session_id: UUID) -> RelayRegistration | None:
        raw = await self._redis.get(registration_key(session_id))
        return None if raw is None else RelayRegistration.from_json(raw)

    async def consume(self, session_id: UUID) -> RelayRegistration | None:
        raw = await self._redis.getdel(registration_key(session_id))
        return None if raw is None else RelayRegistration.from_json(raw)

    async def stop(self, session_id: UUID) -> None:
        """Make a not-yet-attached agent unable to attach, and tell the
        gateway to tear down a live join."""
        await self._redis.delete(registration_key(session_id))
        await self._redis.publish(stop_channel(session_id), "stop")


@dataclass
class _SessionEntry:
    device_id: UUID
    token_hash: str
    agent_reader: StreamReaderLike | None = field(default=None)
    agent_writer: StreamWriterLike | None = field(default=None)


class SessionRelay:
    """In-memory registry of active relay sessions, keyed by session_id.

    Not tenant-scoped and not persisted — this is pure transport-layer state
    that lives only as long as the process. Tenancy/authorization for who may
    open a session at all is enforced upstream, before `register` is called.
    """

    def __init__(self) -> None:
        self._sessions: dict[UUID, _SessionEntry] = {}

    def register(self, session_id: UUID, device_id: UUID, token_hash: str) -> None:
        """Record that `session_id` expects an agent for `device_id` presenting
        a token whose hash is `token_hash`. Call this before the agent dials in."""
        self._sessions[session_id] = _SessionEntry(device_id=device_id, token_hash=token_hash)

    async def attach_agent(
        self,
        session_id: UUID,
        device_id: UUID,
        token: str,
        agent_reader: StreamReaderLike,
        agent_writer: StreamWriterLike,
    ) -> bool:
        """Validate the presented token + device against the registered
        session and, on success, attach the agent's stream. Returns False (and
        attaches nothing) on any mismatch — unknown session, wrong device, or
        wrong token. Auditing the outcome is the caller's job, not this
        method's."""
        entry = self._sessions.get(session_id)
        if entry is None:
            return False
        if entry.device_id != device_id:
            return False
        if not hmac.compare_digest(token_hash(token), entry.token_hash):
            return False
        entry.agent_reader = agent_reader
        entry.agent_writer = agent_writer
        return True

    async def pump(
        self,
        session_id: UUID,
        guacd_reader: StreamReaderLike,
        guacd_writer: StreamWriterLike,
    ) -> None:
        """Bidirectionally copy bytes between the attached agent stream and
        the given guacd stream until either side hits EOF or closes. Raises
        LookupError if no agent is attached to `session_id` yet.

        Both copy directions are always joined, and both writers are always
        closed, even if one leg fails with an exception (e.g. an abrupt
        socket disconnect raising `ConnectionResetError`/`OSError` instead of
        reaching a clean EOF). A failure on either leg is reported by raising
        that exception from this method once both legs have finished — no
        leg is ever left running as an orphaned, unretrieved background task."""
        entry = self._sessions.get(session_id)
        if entry is None:
            raise LookupError(  # i18n-exempt: pump invariant, never a response
                f"no relay session registered for {session_id}"
            )
        if entry.agent_reader is None or entry.agent_writer is None:
            raise LookupError(  # i18n-exempt: pump invariant, never a response
                f"no agent attached to relay session {session_id}"
            )
        agent_reader, agent_writer = entry.agent_reader, entry.agent_writer

        tasks = [
            asyncio.create_task(_copy(agent_reader, guacd_writer)),
            asyncio.create_task(_copy(guacd_reader, agent_writer)),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                raise result

    def unregister(self, session_id: UUID) -> None:
        """Drop all in-memory state for `session_id`. Does not close any
        attached streams — callers that hold them are responsible for that."""
        self._sessions.pop(session_id, None)


async def _copy(src: StreamReaderLike, dst: StreamWriterLike) -> None:
    """Copy bytes from `src` to `dst` until EOF, then close `dst` so its
    reader side unblocks its own counterpart loop with EOF too."""
    try:
        while True:
            chunk = await src.read(_CHUNK_SIZE)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    finally:
        try:
            dst.close()
        except OSError:
            pass
        wait_closed = getattr(dst, "wait_closed", None)
        if wait_closed is not None:
            try:
                await wait_closed()
            except OSError:
                pass
