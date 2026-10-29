"""Device-group backup-policy resolution (docs/modules/backup/spec.md §8),
and the write that sets one (`upsert_policy`).

Resolution order (MSP clients plan 2026-10-26 Task 13): the device's group
policy -> its CLIENT'S default policy (`client_id = device.client_id`,
`device_group_id IS NULL`) -> a policy for every client (no client, no group)
-> None (no enabled policy applies; the caller treats this as "nothing
scheduled for this device").

A policy for every client names no repository: a repository is one client's
(migration 0242), and `its_backup_policy_repository_guard` refuses the pairing.
So such a policy serves only what is not the repository (the window, the
retention), and resolving to one gives `repository_id=None` -- whatever a
row from before that guard says -- so it never hands a device a key.

The two group-less steps are one query, ordered client's first: Postgres's
UNIQUE constraints do not prevent several rows with a NULL device_group_id
(NULLs are never equal for uniqueness), so more than one default can exist
for one client. We order deterministically and take the first row rather
than raising when duplicates exist.

Only `enabled` policies are considered at every scope.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.models import Device
from app.modules.backup.clients import client_for_policy
from app.modules.backup.models import BackupPolicy
from app.services.client_sites import loaded_client


@dataclass(frozen=True)
class ResolvedBackupPolicy:
    repository_id: UUID | None
    sources: dict[str, Any]
    window_cron: str | None
    retention_gfs: dict[str, Any]
    id: UUID | None = None
    created_at: datetime | None = None


def _from_row(row: BackupPolicy, *, repository: bool = True) -> ResolvedBackupPolicy:
    return ResolvedBackupPolicy(
        repository_id=row.repository_id if repository else None,
        sources=row.sources,
        window_cron=row.window_cron,
        retention_gfs=row.retention_gfs,
        id=row.id,
        created_at=row.created_at,
    )


async def resolve_backup_policy(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> ResolvedBackupPolicy | None:
    device = (
        await session.execute(
            select(Device.device_group_id, Device.client_id).where(
                Device.id == device_id, Device.org_id == org_id
            )
        )
    ).one_or_none()
    if device is None:
        return None
    group_id, client_id = device
    if group_id is not None:
        row = (
            await session.execute(
                select(BackupPolicy).where(
                    BackupPolicy.org_id == org_id,
                    BackupPolicy.device_group_id == group_id,
                    BackupPolicy.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return _from_row(row)
    default = (
        await session.execute(
            select(BackupPolicy)
            .where(
                BackupPolicy.org_id == org_id,
                BackupPolicy.device_group_id.is_(None),
                or_(BackupPolicy.client_id == client_id, BackupPolicy.client_id.is_(None)),
                BackupPolicy.enabled.is_(True),
            )
            # The device's client's default before one for every client.
            .order_by(BackupPolicy.client_id.is_(None), BackupPolicy.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if default is None:
        return None
    return _from_row(default, repository=default.client_id is not None)


async def upsert_policy(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    device_group_id: UUID | None,
    client_id: UUID | None,
    repository_id: UUID,
    sources: dict[str, Any],
    window_cron: str | None,
    retention_gfs: dict[str, Any],
    enabled: bool,
) -> tuple[BackupPolicy, UUID]:
    """Create or replace the policy of one group, or one client's default
    (no group), and audit `backup.policy_changed` in its client. Returns the
    row and its client.

    The client, and the repository's being that client's, come first
    (`clients.client_for_policy`: `BackupClientError` 404/409/422, the
    client read FOR SHARE as this write's first lock), before anything is
    written or audited.

    As in patch's upsert_policy: for a default (device_group_id IS NULL)
    there is no unique constraint preventing duplicate rows, so we
    deterministically take the first existing one of the client rather than
    risk inserting a second."""
    policy_client = await client_for_policy(
        session,
        org_id=org_id,
        device_group_id=device_group_id,
        client_id=client_id,
        repository_id=repository_id,
    )
    query = select(BackupPolicy).where(BackupPolicy.org_id == org_id)
    if device_group_id is None:
        query = query.where(
            BackupPolicy.device_group_id.is_(None), BackupPolicy.client_id == policy_client
        )
    else:
        query = query.where(BackupPolicy.device_group_id == device_group_id)
    row = (await session.execute(query.order_by(BackupPolicy.id).limit(1))).scalar_one_or_none()

    created = row is None
    if row is None:
        # A group's policy takes the group's client from the trigger; a
        # default is the application's to name.
        row = BackupPolicy(org_id=org_id, device_group_id=device_group_id)
        if device_group_id is None:
            row.client_id = policy_client
        session.add(row)
    row.repository_id = repository_id
    row.sources = sources
    row.window_cron = window_cron
    row.retention_gfs = retention_gfs
    row.enabled = enabled
    await session.flush()
    stored_client = await loaded_client(session, row)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="backup.policy_changed",
        target_type="backup_policy",
        target_id=str(row.id),
        metadata={
            "device_group_id": str(device_group_id) if device_group_id else None,
            "repository_id": str(repository_id),
            "created": created,
        },
        client_id=stored_client,
    )
    return row, stored_client
