"""Immutable, hash-chained audit log (docs/03-security-model.md).

Each event's hash covers the previous event's hash plus the canonical encoding
of the event itself, so deleting or altering any historical row breaks every
subsequent link. Appends take a per-org advisory transaction lock so the chain
never forks under concurrency. The table is append-only: UPDATE/DELETE are
revoked from the app role and blocked by trigger for everyone else.

Two ways in, one chain: `record` appends ONE event and is what almost every
privileged action calls; `record_many` appends N under a SINGLE lock and one
head read, for the one caller that produces audited writes in bulk (a `net.sweep`
recording every host it found). Both build their links with `_link`, both take
the same lock, and events written either way are indistinguishable to `verify`.

The chain is also *authenticated*: when an HMAC key is configured, each link is
an HMAC-SHA256 keyed by a secret the database role cannot read. Tamper-evidence
alone is not enough — the algorithm and head hash are readable and `app_user`
holds INSERT, so a compromised app process or a SQL-write primitive could
otherwise append valid-looking *forged* events (e.g. attributing a credential
checkout to another actor) that verify() would still pass. Without the key, an
attacker can neither recompute a tampered link nor forge a new one.
"""

import hashlib
import hmac
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.config import Settings
from app.core.mcp_channel import mcp_call_var
from app.jobs.canonical import canonical_bytes, rfc3339
from app.models import AuditEvent

GENESIS_HASH = "0" * 64


class MissingAuditKeyError(RuntimeError):
    """Raised when audit-log authentication is required (production) but no HMAC
    key is provisioned. Running unauthenticated in production would let a
    SQL-write primitive forge valid-looking audit events."""


def load_audit_hmac_key(settings: Settings) -> bytes | None:
    """Resolve the audit HMAC key. Production requires it (fail closed); dev
    runs unkeyed unless a key path is configured. The API and gateway processes
    must load the *same* key or their chains diverge."""
    path = settings.audit_hmac_key_path
    if path is None:
        if settings.environment == "production":
            raise MissingAuditKeyError(  # i18n-exempt: startup config error, logged
                "audit-log authentication is required in production; set "
                "ITS_AUDIT_HMAC_KEY_PATH to a provisioned key"
            )
        return None
    key = Path(path).read_bytes()
    if not key:
        raise MissingAuditKeyError(  # i18n-exempt: startup config error, logged
            f"audit HMAC key {path} is empty"
        )
    return key


def _canonical_fields(
    *,
    org_id: UUID,
    ts: datetime,
    actor_type: str,
    actor_id: UUID | None,
    action: str,
    target_type: str,
    target_id: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "org_id": str(org_id),
        "ts": rfc3339(ts),
        "actor_type": actor_type,
        "actor_id": str(actor_id) if actor_id else None,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "metadata": metadata,
    }


ActorType = Literal["user", "agent", "system"]


@dataclass(frozen=True)
class AuditEntry:
    """One event to append, as a value -- the arguments of `record` minus the
    session and the org, which are properties of the batch rather than of the
    event.

    Frozen and defaulted exactly as `record`'s signature is, so a caller moving
    from N `record` calls to one `record_many` is transcribing, not translating.
    """

    actor_type: ActorType
    actor_id: UUID | None
    action: str
    target_type: str = ""
    target_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    count: int
    broken_at: UUID | None


def _with_channel(metadata: dict[str, Any]) -> dict[str, Any]:
    """Every event written during an MCP call says so, and names the AI app
    (MCP server design §7). Merged here, at the two choke points every append
    passes through, so no caller has to remember it. Outside an MCP call the
    metadata is returned as given -- the same object, byte-identical links."""
    call = mcp_call_var.get()
    if call is not None:
        metadata = {**metadata, "via": "mcp", "mcp_app": call.azp}
    return metadata


class AuditService:
    def __init__(self, hmac_key: bytes | None = None) -> None:
        self._hmac_key = hmac_key

    def set_hmac_key(self, key: bytes | None) -> None:
        """Configure (or clear) the process-wide audit HMAC key at startup.
        Always called during app/gateway wiring — with None when unconfigured —
        so a keyed instance from one context never leaks into another."""
        self._hmac_key = key

    def _link(self, prev_hash: str, fields: dict[str, Any]) -> str:
        material = prev_hash.encode("ascii") + canonical_bytes(fields)
        if self._hmac_key is not None:
            return hmac.new(self._hmac_key, material, hashlib.sha256).hexdigest()
        return hashlib.sha256(material).hexdigest()

    async def record(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        actor_type: Literal["user", "agent", "system"],
        actor_id: UUID | None,
        action: str,
        target_type: str = "",
        target_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        metadata = _with_channel(metadata or {})
        # serialize appends per org so the chain stays linear
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :org))"),
            {"org": str(org_id)},
        )
        prev_hash = (
            await session.execute(
                select(AuditEvent.hash)
                .where(AuditEvent.org_id == org_id)
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or GENESIS_HASH

        ts = datetime.now(UTC).replace(microsecond=0)
        fields = _canonical_fields(
            org_id=org_id,
            ts=ts,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
        )
        event = AuditEvent(
            org_id=org_id,
            ts=ts,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            meta=metadata,
            prev_hash=prev_hash,
            hash=self._link(prev_hash, fields),
        )
        session.add(event)
        await session.flush()
        return event

    async def record_many(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        entries: Sequence[AuditEntry],
    ) -> list[AuditEvent]:
        """Append N events to this org's chain under ONE advisory lock.

        **THE LOCK IS NOT DROPPED, IT IS TAKEN ONCE.** The chain is hash-linked,
        so two transactions appending to the same org concurrently would read
        the same head hash and fork it; the per-org advisory lock is what
        serialises that, and dropping it would be a correctness bug, not an
        optimisation. `pg_advisory_xact_lock` is held until the transaction
        ends however many times it is taken, so N acquisitions inside one
        transaction buy nothing the first one has not already bought -- they
        cost N round trips to acquire a lock this transaction already holds.

        What this saves is the round trips, and there are three per event on the
        path that motivated it: the lock, the head-hash SELECT, and a flush.
        `asset.auto_created` runs once per host a sweep found, so a /24 paid
        them 254 times and a /16 far more (measured: 4096 rows, 14.7s).

        **ORDER IS THE CHAIN, AND THE CHAIN IS READ BACK BY `id`.** `verify`
        orders by `AuditEvent.id` and the head read below takes `max(id)`, so
        the ids assigned here must ascend in the order the links were computed.
        They are assigned EXPLICITLY rather than left to the column default for
        exactly that reason: `uuid7` is time-ordered and monotonic within a
        process, and stamping the ids here makes this function's correctness
        depend on that visibly instead of on SQLAlchemy's flush order. The same
        property already underwrites consecutive `record` calls.

        Returns the events in the order written; `entries=[]` writes nothing and
        takes no lock, because a sweep that found nothing must not serialise an
        org's audit chain to say so.
        """
        if not entries:
            return []
        # serialize appends per org so the chain stays linear -- ONCE for the
        # whole batch. Identical statement and identical key to `record`'s, so
        # a batch and a single append contend with each other exactly as two
        # single appends do.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :org))"),
            {"org": str(org_id)},
        )
        prev_hash = (
            await session.execute(
                select(AuditEvent.hash)
                .where(AuditEvent.org_id == org_id)
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or GENESIS_HASH

        ts = datetime.now(UTC).replace(microsecond=0)
        events: list[AuditEvent] = []
        for entry in entries:
            metadata = _with_channel(entry.metadata or {})
            fields = _canonical_fields(
                org_id=org_id,
                ts=ts,
                actor_type=entry.actor_type,
                actor_id=entry.actor_id,
                action=entry.action,
                target_type=entry.target_type,
                target_id=entry.target_id,
                metadata=metadata,
            )
            link = self._link(prev_hash, fields)
            events.append(
                AuditEvent(
                    id=uuid7(),
                    org_id=org_id,
                    ts=ts,
                    actor_type=entry.actor_type,
                    actor_id=entry.actor_id,
                    action=entry.action,
                    target_type=entry.target_type,
                    target_id=entry.target_id,
                    meta=metadata,
                    prev_hash=prev_hash,
                    hash=link,
                )
            )
            prev_hash = link
        session.add_all(events)
        await session.flush()
        return events

    async def verify(self, session: AsyncSession, org_id: UUID) -> VerifyResult:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
        prev_hash = GENESIS_HASH
        for event in events:
            fields = _canonical_fields(
                org_id=event.org_id,
                ts=event.ts,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                action=event.action,
                target_type=event.target_type,
                target_id=event.target_id,
                metadata=event.meta,
            )
            if event.prev_hash != prev_hash or not hmac.compare_digest(
                event.hash, self._link(prev_hash, fields)
            ):
                return VerifyResult(ok=False, count=len(events), broken_at=event.id)
            prev_hash = event.hash
        return VerifyResult(ok=True, count=len(events), broken_at=None)


audit_service = AuditService()
