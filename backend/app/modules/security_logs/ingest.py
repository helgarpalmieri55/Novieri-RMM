"""Security-event ingestion (docs/modules/security-logs/spec.md §6).

Delivery is at-least-once from the agent's offline buffer, so inserts dedupe
on (device_id, event_uid). The device identity comes from the certificate-
bound connection — callers pass the Device row, never anything from message
content. No per-event audit records (volume); a rejected batch is audited.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.protocol import SecurityEventDoc, SecurityEventType
from app.models import Device, Policy, SecurityEvent

DEFAULT_POLICY: dict[str, Any] = {"retention_days": 365, "ship_interval_s": 60}

VALID_TYPES: frozenset[str] = frozenset(SecurityEventType.__args__)  # type: ignore[attr-defined]


class SecurityIngestError(Exception):
    pass


async def ingest_events(
    session: AsyncSession, device: Device, events: list[SecurityEventDoc]
) -> int:
    """Bulk-insert a shipped batch for the bound device; returns the number of
    NEW rows (redelivered event_uids are skipped). Raises SecurityIngestError
    on any event outside the taxonomy — the whole batch is rejected."""
    for event in events:
        if event.event_type not in VALID_TYPES:
            raise SecurityIngestError(  # i18n-exempt: the gateway writes it to the audit chain
                f"unknown event_type {event.event_type!r}"
            )

    from uuid_extensions import uuid7

    rows = [
        {
            "id": uuid7(),
            "org_id": device.org_id,
            "device_id": device.id,
            "event_uid": event.event_uid,
            "ts": datetime.fromisoformat(event.ts),
            "event_type": event.event_type,
            "username": event.username,
            "source": event.source,
            "outcome": event.outcome,
            "raw": event.raw,
        }
        for event in events
    ]
    statement = (
        pg_insert(SecurityEvent)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["device_id", "event_uid"])
        .returning(SecurityEvent.id)
    )
    inserted = len((await session.execute(statement)).all())

    # Seed the org's default shipping/retention policy once.
    has_policy = (
        await session.execute(
            select(Policy.id).where(Policy.module == "security_logs").limit(1)
        )
    ).scalar_one_or_none()
    if has_policy is None:
        session.add(
            Policy(org_id=device.org_id, module="security_logs", config=dict(DEFAULT_POLICY))
        )
    await session.flush()
    return inserted
