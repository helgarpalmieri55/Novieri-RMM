"""Security-event retention (docs/modules/security-logs/spec.md §7).

The app role cannot delete security_events (append-only grants); retention is
a privileged maintenance operation running through the admin engine, batched,
and audited — the evidence trail records exactly what was removed and when.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Policy, SecurityEvent

DEFAULT_RETENTION_DAYS = 365
DELETE_BATCH = 10_000


async def apply_retention(
    org_id: UUID, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> int:
    """Delete this org's security events older than its retention window.
    Returns the deleted count; audits the sweep unless nothing was deleted."""
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        config: dict[str, Any] = (
            (
                await session.execute(
                    select(Policy.config).where(Policy.module == "security_logs").limit(1)
                )
            ).scalar_one_or_none()
            or {}
        )
    retention_days = int(config.get("retention_days", DEFAULT_RETENTION_DAYS))
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    total = 0
    while True:
        async with AsyncSession(admin_engine) as session:
            batch_ids = (
                (
                    await session.execute(
                        select(SecurityEvent.id)
                        .where(SecurityEvent.org_id == org_id, SecurityEvent.ts < cutoff)
                        .limit(DELETE_BATCH)
                    )
                )
                .scalars()
                .all()
            )
            if not batch_ids:
                break
            await session.execute(delete(SecurityEvent).where(SecurityEvent.id.in_(batch_ids)))
            await session.commit()
            total += len(batch_ids)

    if total:
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="security_logs.retention_applied",
                target_type="org",
                target_id=str(org_id),
                metadata={"cutoff": cutoff.isoformat(), "deleted": total},
            )
    return total
