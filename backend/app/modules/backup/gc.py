"""Repository garbage collection (mark-and-sweep) + integrity verification for
the backup engine (docs/modules/backup/spec.md).

This is the module's load-bearing correctness surface: GC reclaims storage by
deleting chunk objects no live backup point references, and `verify_point`
proves a point's chunks are intact by re-reading and BLAKE3-checking them. A GC
bug deletes live backup data; a verify bug reports corrupt backups as good — so
both are conservative by construction:

- GC only ever deletes a chunk that is referenced by NO live point (a point
  whose `state != "pruned"`). Chunks shared between points survive as long as
  ANY live point still references them; a `pruned` point's chunks are not
  protected (reclaiming them is the point of pruning).
- `verify_point` fails on ANY corrupt, undecryptable, or missing chunk and only
  stamps `verified_at` when every referenced chunk verifies.

Chunk objects use the SAME codec as the manifest (`seal_manifest`/
`open_manifest` in `app.modules.backup.restore`): AES-256-GCM under the
repository master key, wire layout `nonce(12) || ciphertext||tag(16)`, no AAD.
A chunk id is `blake3(plaintext).hexdigest()` — the BLAKE3-256 hex digest of the
chunk's PLAINTEXT (pre-encryption), which is what verification recomputes.

Both entry points are org-scoped on every query (invariant #5) and operate only
within the org's object prefix (`list_backup_chunks`/`*_backup_chunk` are
`(org_id, repository_id)`-scoped), and both write the hash-chained,
system-attributed audit event their action demands (invariant #4) carrying
counts/flags only — never chunk ids, plaintext, or key material (invariant #3).
"""

import os
from datetime import datetime
from uuid import UUID

from blake3 import blake3
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.storage import ObjectStore
from app.core.vault import VaultService
from app.models import BackupPoint, BackupRepository
from app.modules.backup.dedup import reconcile_chunk_index
from app.modules.backup.manifest import referenced_chunk_ids
from app.modules.backup.repository import open_repo_key

_NONCE_BYTES = 12


def seal_chunk(repo_key: bytes, plaintext: bytes) -> bytes:
    """Encrypt a chunk's plaintext under the repository master key for storage
    as a chunk object. Wire layout: `nonce(12) || ciphertext||tag(16)`,
    identical to `seal_manifest`."""
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(repo_key).encrypt(nonce, plaintext, None)
    return nonce + ct


def open_chunk(repo_key: bytes, blob: bytes) -> bytes:
    """Inverse of `seal_chunk`; raises on a tampered/truncated object (the
    GCM tag fails to authenticate), which verification treats as corruption."""
    nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    return AESGCM(repo_key).decrypt(nonce, ct, None)


async def _referenced_chunk_ids(
    object_store: ObjectStore,
    repo_key: bytes,
    manifest_ref: str,
    *,
    org_id: UUID,
    repository_id: UUID,
) -> set[str]:
    """Every chunk object the manifest at `manifest_ref` keeps alive: data
    chunks and, for a v2 manifest, its entry and id pages (`manifest.py`)."""
    blob = await object_store.get_backup_object(manifest_ref)

    async def fetch(chunk_id: str) -> bytes:
        return await object_store.get_backup_chunk(org_id, repository_id, chunk_id)

    return await referenced_chunk_ids(repo_key, blob, fetch)


async def gc_repository(
    session: AsyncSession,
    object_store: ObjectStore,
    vault: VaultService,
    *,
    org_id: UUID,
    repository_id: UUID,
    now: datetime,
) -> int:
    """Mark-and-sweep GC for `repository_id`: delete every stored chunk object
    not referenced by ANY live (`state != "pruned"`) backup point, and return
    the number deleted.

    `open_repo_key` fails closed (`NoResultFound`) if the repository does not
    resolve in this org (invariant #5). The live-point query carries an
    explicit `org_id` predicate, and both listing and deletion are confined to
    this `(org_id, repository_id)` prefix — another org's chunks are never
    listed, read, or deleted.

    `now` is accepted for a uniform signature with `verify_point` (and future
    grace-window logic); GC itself is time-independent. The `backup.gc` audit
    event records counts only (invariant #3).
    """
    repo_key = await open_repo_key(session, vault, org_id=org_id, repository_id=repository_id)
    # Its events are the repository's client's (plan 2026-10-26 Task 13).
    client_id = (
        await session.execute(
            select(BackupRepository.client_id).where(
                BackupRepository.org_id == org_id, BackupRepository.id == repository_id
            )
        )
    ).scalar_one()

    points = (
        (
            await session.execute(
                select(BackupPoint).where(
                    BackupPoint.org_id == org_id,
                    BackupPoint.repository_id == repository_id,
                    BackupPoint.state != "pruned",
                )
            )
        )
        .scalars()
        .all()
    )

    # Mark: the union of chunk ids referenced by every live point's manifest.
    referenced: set[str] = set()
    for point in points:
        referenced |= await _referenced_chunk_ids(
            object_store, repo_key, point.manifest_ref, org_id=org_id, repository_id=repository_id
        )

    # Sweep: delete stored chunks that no live point references.
    deleted = 0
    surviving: set[str] = set()
    for chunk_id in await object_store.list_backup_chunks(org_id, repository_id):
        if chunk_id in referenced:
            surviving.add(chunk_id)
            continue
        await object_store.delete_backup_chunk(org_id, repository_id, chunk_id)
        deleted += 1

    # Reconcile the dedup index against what actually survived. THIS IS NOT
    # BOOKKEEPING. `backup_chunks` is what the server answers "you already have
    # this chunk" from, so an index row outliving its object makes the next
    # run skip an upload and write a manifest referencing an object nobody
    # will ever store — the exact failure GC exists to avoid causing. It also
    # repairs a row an agent created for a chunk it never actually wrote, which
    # is the one thing the dedup exchange takes on trust
    # (`dedup.reconcile_chunk_index`). Dropping a row is fail-safe: the worst
    # outcome is one redundant upload.
    forgotten = await reconcile_chunk_index(
        session, org_id=org_id, repository_id=repository_id, present=surviving
    )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="backup.gc",
        target_type="repository",
        target_id=str(repository_id),
        metadata={
            "deleted": deleted,
            "live_points": len(points),
            "referenced_chunks": len(referenced),
            "index_rows_dropped": forgotten,
        },
        client_id=client_id,
    )

    return deleted


async def verify_point(
    session: AsyncSession,
    object_store: ObjectStore,
    vault: VaultService,
    *,
    org_id: UUID,
    backup_point_id: UUID,
    now: datetime,
) -> bool:
    """Integrity-check `backup_point_id`: re-read and decrypt each referenced
    chunk and confirm `blake3(plaintext).hexdigest()` equals the chunk id.

    Any mismatch, decrypt failure, or missing chunk fails verification: return
    `False` WITHOUT stamping `verified_at`. On full success, stamp
    `BackupPoint.verified_at = now` and return `True`. Either way, write a
    system-attributed `backup.verify` audit event carrying only `ok`/`checked`
    (invariant #3).

    The point lookup carries an explicit `org_id` predicate and fails closed
    (`NoResultFound`) for a foreign/nonexistent point (invariant #5); chunk
    reads are confined to this org's `(org_id, repository_id)` prefix.

    v1 verifies ALL referenced chunks — deterministic and strictly stronger
    than the spec's "sampling"; fractional sampling is a later optimization.
    """
    point = (
        await session.execute(
            select(BackupPoint).where(
                BackupPoint.org_id == org_id, BackupPoint.id == backup_point_id
            )
        )
    ).scalar_one()

    repo_key = await open_repo_key(
        session, vault, org_id=org_id, repository_id=point.repository_id
    )
    referenced = await _referenced_chunk_ids(
        object_store,
        repo_key,
        point.manifest_ref,
        org_id=org_id,
        repository_id=point.repository_id,
    )

    ok = True
    checked = 0
    for chunk_id in referenced:
        try:
            blob = await object_store.get_backup_chunk(org_id, point.repository_id, chunk_id)
            plaintext = open_chunk(repo_key, blob)
        except Exception:
            # Missing object, tampered/truncated ciphertext (GCM tag failure) —
            # any read/decrypt failure is corruption. Fail closed.
            ok = False
            break
        if blake3(plaintext).hexdigest() != chunk_id:
            ok = False
            break
        checked += 1

    if ok:
        point.verified_at = now
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="backup.verify",
        target_type="backup_point",
        target_id=str(backup_point_id),
        metadata={"ok": ok, "checked": checked},
    )

    return ok
