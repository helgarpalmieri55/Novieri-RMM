"""Backup repository provisioning (docs/modules/backup/spec.md §4/§7).

A `BackupRepository` is ONE client's deduplicated, encrypted storage target
that its devices' backup runs write chunks/manifests into (MSP clients plan
2026-10-26 Task 13, C1: the key travels to every device using it, so it is
never shared between clients). Creation files it under the client the request
names (`clients.client_for_new_repository`, the write's first lock), mints a
random 32-byte repository master key, seals it through the vault (the app
database only ever holds the sealed reference, never the raw key), and
audits the creation in that client. The master key is opened back out only at
the point a signed `backup.run`/`backup.restore` job is dispatched
(elsewhere) — never returned by any API, never logged, never placed in audit
metadata.
"""

import os
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.vault import VaultService
from app.modules.backup.clients import client_for_new_repository
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
    client_id: UUID,
    name: str,
    dedup_params: dict[str, Any] | None = None,
) -> BackupRepository:
    """`BackupClientError` for a client that cannot take one (404, 409, 422),
    before anything is written. A name the client already uses is the
    IntegrityError of `uq_backup_repositories_client_name` at the first
    flush, which the route answers 409 (`api.REPOSITORY_NAME_KEY`): the key
    is per client, so it says nothing about another client's names."""
    await client_for_new_repository(session, org_id=org_id, client_id=client_id)
    master_key = os.urandom(32)

    repo = BackupRepository(
        org_id=org_id,
        client_id=client_id,
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
        client_id=client_id,
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
