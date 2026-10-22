"""Device-group backup-policy resolution (docs/modules/backup/spec.md §8).

Resolution order: device's group policy -> org-default policy
(device_group_id IS NULL) -> None (no enabled policy applies; the caller
treats this as "nothing scheduled for this device").

The org-default lookup is deliberately not `.scalar_one_or_none()`-on-a-plain-
select: Postgres's UNIQUE(org_id, device_group_id) constraint does not prevent
multiple rows with a NULL device_group_id (NULLs are never considered equal
for uniqueness purposes), so more than one org-default row can exist for the
same org. We order deterministically and take the first row rather than
raising when duplicates exist.

Only `enabled` policies are considered at either scope.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.modules.backup.models import BackupPolicy


@dataclass(frozen=True)
class ResolvedBackupPolicy:
    repository_id: UUID | None
    sources: dict[str, Any]
    window_cron: str | None
    retention_gfs: dict[str, Any]
    id: UUID | None = None
    created_at: datetime | None = None


def _from_row(row: BackupPolicy) -> ResolvedBackupPolicy:
    return ResolvedBackupPolicy(
        repository_id=row.repository_id,
        sources=row.sources,
        window_cron=row.window_cron,
        retention_gfs=row.retention_gfs,
        id=row.id,
        created_at=row.created_at,
    )


async def resolve_backup_policy(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> ResolvedBackupPolicy | None:
    group_id = (
        await session.execute(
            select(Device.device_group_id).where(
                Device.id == device_id, Device.org_id == org_id
            )
        )
    ).scalar_one_or_none()
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
    org_default = (
        await session.execute(
            select(BackupPolicy)
            .where(
                BackupPolicy.org_id == org_id,
                BackupPolicy.device_group_id.is_(None),
                BackupPolicy.enabled.is_(True),
            )
            .order_by(BackupPolicy.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return _from_row(org_default) if org_default is not None else None
