"""Scheduled test restores (spec-server-backup.md §7.4; operator decision
2026-09-23: files monthly, on a sample).

Once a device's newest successful test restore is older than
`TEST_RESTORE_EVERY`, the sweep dispatches a `verify` restore of its newest
complete point in a repository of the device's own client: a random sample of
up to `SAMPLE_FILES` files, capped at `SAMPLE_BYTES`, restored into the
agent's scratch directory, every chunk checked, then deleted. The result
stamps `restore_tested_at` with level `read` and scope `sampled`, so a sample
is never reported as a full restore.

A released agent verifies the whole point whatever the selection says, so a
point larger than the sample cap is not test-restored until the agent honours
the selection in verify mode; it stays visible through the
`backup.restore_test_age_d` alert instead of filling a server's disk.
"""

import random
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.storage import ObjectStore
from app.core.vault import VaultService
from app.models import Device
from app.modules.backup.dispatch import _JobEngineLike
from app.modules.backup.models import BackupPoint, BackupRepository, RestoreJob
from app.modules.backup.policy import resolve_backup_policy
from app.modules.backup.restore import browse_manifest, dispatch_restore
from app.modules.backup.schema import BackupManifest

TEST_RESTORE_EVERY = timedelta(days=30)
SAMPLE_FILES = 200
SAMPLE_BYTES = 5 * 1024**3


def sample_selection(
    manifest: BackupManifest, *, rng: random.Random, files: int = SAMPLE_FILES,
    max_bytes: int = SAMPLE_BYTES,
) -> list[str]:
    """Up to `files` random regular files whose sizes sum to at most `max_bytes`."""
    candidates = [
        e for s in manifest.streams for e in s.entries if e.kind == "file" and e.size <= max_bytes
    ]
    rng.shuffle(candidates)
    chosen: list[str] = []
    total = 0
    for entry in candidates:
        if len(chosen) >= files:
            break
        if total + entry.size > max_bytes:
            continue
        chosen.append(entry.path)
        total += entry.size
    return sorted(chosen)


async def dispatch_due_test_restores(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    vault: VaultService,
    object_store: ObjectStore,
    *,
    org_id: UUID,
    now: datetime,
    rng: random.Random | None = None,
) -> list[tuple[UUID, UUID, dict[str, Any]]]:
    """Dispatch the test restores due in `org_id`. Same publish contract as
    `dispatch_restore`: the caller publishes after commit."""
    rng = rng or random.Random()
    pending: list[tuple[UUID, UUID, dict[str, Any]]] = []
    devices = (
        await session.execute(
            select(Device.id, Device.client_id).where(
                Device.org_id == org_id,
                Device.status == "active",
                Device.lifecycle == "in_service",
                Device.os_family.is_not(None),
            )
        )
    ).all()
    for device_id, client_id in devices:
        if await resolve_backup_policy(session, org_id, device_id) is None:
            continue
        last_tested = (
            await session.execute(
                select(func.max(BackupPoint.restore_tested_at)).where(
                    BackupPoint.org_id == org_id, BackupPoint.device_id == device_id
                )
            )
        ).scalar_one()
        if last_tested is not None and now - last_tested < TEST_RESTORE_EVERY:
            continue
        busy = (
            await session.execute(
                select(RestoreJob.id)
                .where(
                    RestoreJob.org_id == org_id,
                    RestoreJob.device_id == device_id,
                    RestoreJob.state == "running",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if busy is not None:
            continue
        # Only a point in a repository of the DEVICE'S client (5a, C1; final
        # review D). A point from before 0242 can sit in a repository that is
        # now another client's -- the internal one's -- and restoring it would
        # send this device that repository's key and read grants over its
        # whole prefix. This sweep is nobody's decision; a person's manual
        # restore of such a point is an audited act of its own.
        point = (
            await session.execute(
                select(BackupPoint)
                .join(
                    BackupRepository,
                    (BackupRepository.id == BackupPoint.repository_id)
                    & (BackupRepository.org_id == org_id),
                )
                .where(
                    BackupPoint.org_id == org_id,
                    BackupPoint.device_id == device_id,
                    BackupPoint.state == "complete",
                    BackupRepository.client_id == client_id,
                )
                .order_by(BackupPoint.taken_at.desc(), BackupPoint.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if point is None or point.size > SAMPLE_BYTES:
            continue
        manifest = await browse_manifest(
            session, object_store, vault, org_id=org_id, backup_point_id=point.id
        )
        selection = sample_selection(manifest, rng=rng)
        if not selection:
            continue
        dispatched = await dispatch_restore(
            session,
            job_engine,
            vault,
            org_id=org_id,
            backup_point_id=point.id,
            selection=selection,
            target={},
            mode="verify",
            now=now,
            actor_type="system",
        )
        for job_id, _device, _envelope in dispatched:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="backup.test_restore_dispatched",
                target_type="device",
                target_id=str(device_id),
                metadata={
                    "backup_point_id": str(point.id),
                    "job_id": str(job_id),
                    "files": len(selection),
                    "scope": "sampled",
                },
            )
        pending.extend(dispatched)
    await session.flush()
    return pending
