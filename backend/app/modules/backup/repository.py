"""Backup repository provisioning (docs/modules/backup/spec.md §4/§7).

A `BackupRepository` is the org-scoped, deduplicated, encrypted storage
target that a device's backup runs write chunks/manifests into. Creation
mints a random 32-byte repository master key, seals it through the vault
(the app database only ever holds the sealed reference, never the raw key),
and audits the creation. The master key is opened back out only at the
point a signed `backup.run`/`backup.restore` job is dispatched (elsewhere) —
never returned by any API, never logged, never placed in audit metadata.
"""

import os
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.vault import VaultService
from app.modules.backup.models import BackupRepository

DEFAULT_DEDUP = {
    "algo": "buzhash",
    "avg": 1048576,
    "min": 524288,
    "max": 4194304,
    "hash": "blake3-256",
}


async def create_repository(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    dedup_params: dict[str, Any] | None = None,
) -> BackupRepository:
    master_key = os.urandom(32)

    repo = BackupRepository(
        org_id=org_id,
        name=name,
        storage_ref="",
        encryption_key_ref="",
        dedup_params=dedup_params or DEFAULT_DEDUP,
        state="active",
    )
    session.add(repo)
    await session.flush()  # populate repo.id (uuid7) before it is used as the seal key_id

    repo.storage_ref = f"{org_id}/backup/{repo.id}"
    sealed = await vault.seal_bytes(session, org_id=org_id, key_id=repo.id, plaintext=master_key)
    repo.encryption_key_ref = sealed
    session.add(repo)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="backup.repository_created",
        target_type="backup_repository",
        target_id=str(repo.id),
        metadata={"name": name, "storage_ref": repo.storage_ref},
    )

    return repo


async def open_repo_key(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    repository_id: UUID,
) -> bytes:
    repo = (
        await session.execute(
            select(BackupRepository).where(
                BackupRepository.org_id == org_id, BackupRepository.id == repository_id
            )
        )
    ).scalar_one()
    return await vault.open_bytes(
        session, org_id=org_id, key_id=repository_id, sealed=repo.encryption_key_ref
    )
