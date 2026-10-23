"""Session-recording finalize + per-org retention sweep (docs/modules/remote-control/spec.md).

When a remote session ends, its guacd recording is uploaded to per-tenant
object storage and linked onto the RemoteSession row (finalize_recording). A
daily sweep (apply_recording_retention) deletes recordings past the org's
retention window, resolved from the org-default RemotePolicy the same way
Task 3's resolve_policy does: Postgres's UNIQUE(org_id, device_group_id) does
not prevent multiple NULL-device_group_id rows for the same org, so we order
deterministically and take the first row rather than raising on duplicates.

Nulling recording_ref/recording_bytes runs through the admin engine (mirroring
app/modules/security_logs/retention.py's admin-engine/app-engine split): the
sweep is a privileged maintenance operation, not ordinary request traffic, and
keeping it off the RLS-scoped app session keeps the batch delete-then-update
atomic under one connection. The audit write goes through the app engine's
org_session, consistent with every other audited mutation in this codebase.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import RemotePolicy, RemoteSession

DEFAULT_RETENTION_DAYS = 90


async def finalize_recording(
    session: AsyncSession,
    store: ObjectStore,
    *,
    org_id: UUID,
    session_id: UUID,
    data: bytes,
) -> tuple[str, int]:
    """Upload the recording and link it onto the RemoteSession row.

    Returns (ref, size). Caller is responsible for committing/closing session.
    """
    ref = await store.put_remote_recording(org_id, session_id, data)
    size = len(data)
    row = (
        await session.execute(select(RemoteSession).where(RemoteSession.id == session_id))
    ).scalar_one()
    row.recording_ref = ref
    row.recording_bytes = size
    await session.flush()
    return ref, size


async def finalize_recording_file(
    session: AsyncSession,
    store: ObjectStore,
    *,
    org_id: UUID,
    session_id: UUID,
    path: Path,
    truncated: bool,
) -> tuple[str, int]:
    """Upload a spooled ``.rsp`` recording and link it onto the session row.

    A recording that hit the policy cap is kept (it is everything up to the
    cap), flagged on the row and audited.
    """
    ref = await store.put_remote_recording_stream(org_id, session_id, path)
    size = path.stat().st_size
    row = (
        await session.execute(select(RemoteSession).where(RemoteSession.id == session_id))
    ).scalar_one()
    row.recording_ref = ref
    row.recording_bytes = size
    row.recording_truncated = truncated
    if truncated:
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="remote.recording.truncated",
            target_type="session",
            target_id=str(session_id),
            metadata={"device_id": str(row.device_id), "bytes": size},
        )
    await session.flush()
    return ref, size


async def _resolve_retention_days(org_id: UUID, app_engine: AsyncEngine) -> int:
    async with org_session(org_id, engine=app_engine) as session:
        days = (
            await session.execute(
                select(RemotePolicy.recording_retention_days)
                .where(RemotePolicy.org_id == org_id, RemotePolicy.device_group_id.is_(None))
                .order_by(RemotePolicy.id)
                .limit(1)
            )
        ).scalar_one_or_none()
    return days if days is not None else DEFAULT_RETENTION_DAYS


async def apply_recording_retention(
    org_id: UUID,
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    store: ObjectStore,
) -> int:
    """Delete this org's expired session recordings. Returns the deleted count;
    audits the sweep once (remote.recording_expired) unless nothing was deleted."""
    retention_days = await _resolve_retention_days(org_id, app_engine)
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        expired = (
            await session.execute(
                select(RemoteSession.id, RemoteSession.recording_ref).where(
                    RemoteSession.org_id == org_id,
                    RemoteSession.recording_ref.is_not(None),
                    RemoteSession.ended_at < cutoff,
                )
            )
        ).all()
        for _sid, ref in expired:
            await store.delete_object(ref)
        if expired:
            ids = [row.id for row in expired]
            await session.execute(
                update(RemoteSession)
                .where(RemoteSession.id.in_(ids))
                .values(recording_ref=None, recording_bytes=None)
            )
            await session.commit()

    total = len(expired)
    if total:
        async with org_session(org_id, engine=app_engine) as session:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="remote.recording_expired",
                target_type="org",
                target_id=str(org_id),
                metadata={"cutoff": cutoff.isoformat(), "deleted": total},
            )
    return total
