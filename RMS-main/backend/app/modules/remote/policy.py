"""Device-group remote-policy resolution (docs/modules/remote-control/spec.md).

Resolution order: device's group policy -> org-default policy
(device_group_id IS NULL) -> hardcoded safe defaults.

The org-default lookup is deliberately not `.scalar_one_or_none()`-on-a-plain-
select: Postgres's UNIQUE(org_id, device_group_id) constraint does not prevent
multiple rows with a NULL device_group_id (NULLs are never considered equal
for uniqueness purposes), so more than one org-default row can exist for the
same org. We order deterministically and take the first row rather than
raising when duplicates exist.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, RemotePolicy
from app.modules.remote.models import RECORDING_MAX_BYTES


@dataclass(frozen=True)
class ResolvedPolicy:
    allow_unattended: bool = False
    record_sessions: bool = True
    allow_file_transfer: bool = False
    allow_clipboard: bool = False
    recording_retention_days: int = 90
    consent_timeout_s: int = 30
    allow_input: bool = True
    allow_privacy_blank: bool = False
    recording_max_bytes: int = RECORDING_MAX_BYTES


def _from_row(row: RemotePolicy) -> ResolvedPolicy:
    return ResolvedPolicy(
        allow_unattended=row.allow_unattended,
        record_sessions=row.record_sessions,
        allow_file_transfer=row.allow_file_transfer,
        allow_clipboard=row.allow_clipboard,
        recording_retention_days=row.recording_retention_days,
        consent_timeout_s=row.consent_timeout_s,
        allow_input=row.allow_input,
        allow_privacy_blank=row.allow_privacy_blank,
        recording_max_bytes=row.recording_max_bytes,
    )


async def resolve_policy(session: AsyncSession, org_id: UUID, device_id: UUID) -> ResolvedPolicy:
    group_id = (
        await session.execute(select(Device.device_group_id).where(Device.id == device_id))
    ).scalar_one_or_none()
    if group_id is not None:
        row = (
            await session.execute(
                select(RemotePolicy).where(
                    RemotePolicy.org_id == org_id, RemotePolicy.device_group_id == group_id
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return _from_row(row)
    org_default = (
        await session.execute(
            select(RemotePolicy)
            .where(RemotePolicy.org_id == org_id, RemotePolicy.device_group_id.is_(None))
            .order_by(RemotePolicy.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return _from_row(org_default) if org_default is not None else ResolvedPolicy()
