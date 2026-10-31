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

A repository can be RETIRED (phase 3 design §5.5, `retire_repository`): it
takes no new backup and keeps its points, restorable.
"""

import os
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.vault import VaultService
from app.modules.backup.clients import client_for_new_repository
from app.modules.backup.models import REPOSITORY_RETIRED, BackupPolicy, BackupRepository

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


class RetireRefused(Exception):
    """A refused retire, with the status the API answers and its sentence:
    404 for a repository this request cannot see, 409 for one an enabled
    policy uses or one already retired."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def retire_repository(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, repository_id: UUID
) -> BackupRepository:
    """Retire one repository (phase 3 design §5.5, operator decision Q5).

    It takes no new backup: the dispatch refuses it (`dispatch.py`), the
    dedup exchange grants it no writes (`dedup.py`) and no policy may name it
    (`clients.policy_repository`). Its points stay, still occupying their
    space, and stay restorable (`read.py` grants reads on `retired`);
    verification, GC and the off-site copy of its points go on. There is no
    way back to `active` in this phase.

    `backup.policy.manage` on the repository's client is the ROUTE's gate: it
    runs under the clients where the person holds that code, so another
    client's repository is not found here -- the 404 a missing one gets (MSP
    clients spec §3.2's existence oracle), never a 403.

    A 409 while an ENABLED policy names it: retiring it would silently stop
    those devices' backups. The check sees the policies this request can
    see; a policy of another client naming it can only be one 0242 left
    behind, and the dispatch already refuses that one as another client's. A
    second retire is a 409 too, and writes no second event.

    LOCKS. The repository row, FOR NO KEY UPDATE, is the first lock; the audit
    chain's is the last. A policy write reads the repository FOR SHARE
    (`policy_repository`), so a write naming it and this retire serialise:
    neither passes its check while the other is between its check and its
    write. NO KEY, because the foreign-key checks of rows that only reference
    the repository (a run, a point) take FOR KEY SHARE and need not wait. No
    client lock: a retire creates no row of the client, and a repository of
    an archived client can still be retired."""
    repo = (
        await session.execute(
            select(BackupRepository)
            .where(BackupRepository.org_id == org_id, BackupRepository.id == repository_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    if repo is None:
        raise RetireRefused(404, tr("no such repository"))
    if repo.state == REPOSITORY_RETIRED:
        raise RetireRefused(409, tr("this repository is already retired"))
    in_use = (
        await session.execute(
            select(BackupPolicy.id)
            .where(
                BackupPolicy.org_id == org_id,
                BackupPolicy.repository_id == repo.id,
                BackupPolicy.enabled.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if in_use is not None:
        raise RetireRefused(
            409,
            tr(
                "an enabled backup policy still uses this repository; switch it off or "
                "point it at another repository first"
            ),
        )
    repo.state = REPOSITORY_RETIRED
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="backup.repository_retired",
        target_type="backup_repository",
        target_id=str(repo.id),
        metadata={"name": repo.name},
        client_id=repo.client_id,
    )
    return repo
