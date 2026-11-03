"""Restore-side of the backup module: manifest browse + signed `backup.restore`
dispatch (docs/modules/backup/spec.md).

Mirrors `app.modules.backup.dispatch.dispatch_backup`: this is the module's
application-security gate for *restores* — the only path by which an operator
(or an automated verify sweep) turns a backup point + selection into a signed
`backup.restore` job. Every device job is built via the job engine, signed,
and (by the caller, post-commit) published; no unsigned path exists
(invariant #1).

`browse_manifest` lets an operator inspect what a backup point contains
before choosing a restore selection. The manifest object in repository
storage is encrypted at rest under the repository's master key — the same
key `dispatch_restore` hands to the agent — using the codec below
(`seal_manifest`/`open_manifest`): AES-256-GCM over the manifest's canonical
JSON, wire layout `nonce(12) || ciphertext||tag(16)`, no AAD (the manifest
ref itself, not a bound context string, identifies the object; keeping this
symmetric with the agent's simpler write path). `browse_manifest` returns
manifest METADATA only (paths, sizes, chunk ids) — it never fetches chunk
objects, so it never touches file contents.
"""

import base64
import os
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.storage import ObjectStore
from app.core.vault import VaultService
from app.models import BackupPoint, BackupRepository, Job, RestoreJob
from app.modules.backup.dispatch import _JobEngineLike
from app.modules.backup.manifest import browse_entries
from app.modules.backup.repository import open_repo_key
from app.modules.backup.schedule import RESTORE_JOB_TTL
from app.modules.backup.schema import BackupManifest, BackupRestoreParams, BackupRestoreResult

_NONCE_BYTES = 12


def seal_manifest(repo_key: bytes, manifest: BackupManifest) -> bytes:
    """Encrypt a manifest under the repository master key for storage as a
    repository object. Wire layout: `nonce(12) || ciphertext||tag(16)`."""
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(repo_key).encrypt(nonce, manifest.model_dump_json().encode(), None)
    return nonce + ct


def open_manifest(repo_key: bytes, blob: bytes) -> BackupManifest:
    """Inverse of `seal_manifest`."""
    nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    plaintext = AESGCM(repo_key).decrypt(nonce, ct, None)
    return BackupManifest.model_validate_json(plaintext)


async def browse_manifest(
    session: AsyncSession,
    object_store: ObjectStore,
    vault: VaultService,
    *,
    org_id: UUID,
    backup_point_id: UUID,
) -> BackupManifest:
    """Fetch and decrypt `backup_point_id`'s manifest, returning its metadata
    (paths/sizes/chunk ids) only — this never fetches chunk objects, so it
    never touches file contents.

    The `BackupPoint` lookup carries an explicit `org_id` predicate
    (invariant #5); a point id from another org raises `NoResultFound`
    (fail-closed) rather than resolving to a nonexistent/foreign point.
    """
    point = (
        await session.execute(
            select(BackupPoint).where(
                BackupPoint.org_id == org_id, BackupPoint.id == backup_point_id
            )
        )
    ).scalar_one()
    key = await open_repo_key(session, vault, org_id=org_id, repository_id=point.repository_id)
    blob = await object_store.get_backup_object(point.manifest_ref)

    async def fetch(chunk_id: str) -> bytes:
        return await object_store.get_backup_chunk(org_id, point.repository_id, chunk_id)

    return await browse_entries(key, blob, fetch)


async def dispatch_restore(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    vault: VaultService,
    *,
    org_id: UUID,
    backup_point_id: UUID,
    selection: list[str],
    target: dict[str, Any],
    mode: Literal["restore", "verify"],
    now: datetime,
    actor_id: UUID | None = None,
    actor_type: Literal["user", "agent", "system"] = "system",
) -> list[tuple[UUID, UUID, dict[str, Any]]]:
    """Build one signed `backup.restore` job for `backup_point_id`, restoring
    (or, in `mode="verify"`, proving-restorable) `selection` into `target` on
    the device that produced the point.

    Does NOT publish the job to Redis — mirrors `dispatch_backup`: publishing
    a signed job before the caller's tenant transaction commits would hand an
    agent a job with no durable server record if the commit later failed.
    Instead, returns the pending publish as a single-element list of
    `(job_id, device_id, envelope)` triples; the CALLER must publish it via
    `app.gateway.dispatch.publish_job` only AFTER its `org_session` block
    exits (i.e. after commit).

    The pending list is empty if `backup_point_id` doesn't resolve in this
    org, or its repository doesn't exist in this org (fail-closed rather than
    dispatching against a dangling reference). `actor_id`/`actor_type`
    attribute the `backup.restore_dispatched` audit event — a human-initiated
    restore passes `actor_id=<user>, actor_type="user"`; an automated
    proven-restore sweep passes `actor_type="system"` (the default).

    The repository master key is opened from the vault only to place it
    inside `BackupRestoreParams.repo_key` (base64), which rides to the agent
    solely inside the signed job params — it is never written to the audit
    metadata or logged (invariant #3).

    EVERY query (`BackupPoint`, `BackupRepository`) carries an explicit
    `org_id` predicate (invariant #5) — a backup point id from another org
    must never be reachable through this path. `device_id` is resolved from
    the (org-scoped) point, never taken from caller input.
    """
    point = (
        await session.execute(
            select(BackupPoint).where(
                BackupPoint.org_id == org_id, BackupPoint.id == backup_point_id
            )
        )
    ).scalar_one_or_none()
    if point is None:
        return []

    repo = (
        await session.execute(
            select(BackupRepository).where(
                BackupRepository.org_id == org_id, BackupRepository.id == point.repository_id
            )
        )
    ).scalar_one_or_none()
    if repo is None:
        return []

    device_id = point.device_id

    key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo.id)

    params = BackupRestoreParams(
        repository_id=str(repo.id),
        storage_ref=repo.storage_ref,
        manifest_ref=point.manifest_ref,
        selection=selection,
        target=target,
        mode=mode,
        repo_key=base64.b64encode(key).decode(),
    )

    job = await job_engine.create(
        session,
        org_id=org_id,
        type="backup.restore",
        target_device_id=device_id,
        params=params.model_dump(),
        requested_by=actor_id,
        ttl_s=int(RESTORE_JOB_TTL.total_seconds()),
    )
    envelope = await job_engine.signed_envelope(job)

    session.add(
        RestoreJob(
            org_id=org_id,
            device_id=device_id,
            backup_point_id=backup_point_id,
            job_id=job.id,
            selection={"paths": selection},
            target=target,
            mode=mode,
            state="running",
            actor_type=actor_type,
            requested_by=actor_id,
        )
    )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="backup.restore_dispatched",
        target_type="device",
        target_id=str(device_id),
        metadata={
            "backup_point_id": str(backup_point_id),
            "device_id": str(device_id),
            "job_id": str(job.id),
            "mode": mode,
        },
        client_id=point.client_id,
    )

    await session.flush()
    return [(job.id, device_id, envelope)]


# `restore_jobs.state` domain a completed restore may land on (docs/modules/
# backup/spec.md). `BackupRestoreResult.state` is itself typed
# `Literal["success", "failed"]`, so in the normal (validated) path only
# those two values ever reach this function; the explicit membership check
# below is defense in depth against a value that bypasses the wire model's
# validation (e.g. a handler that skips `BackupRestoreResult(**result)`),
# mirroring `ingest_backup_result`'s out-of-domain coercion (the Task-8-
# deploy lesson): an unrecognized state must still land on a terminal
# `failed` state — never raise, never wedge the restore job — with the
# original value recorded in the audit log so the operator can see why.
_VALID_RESTORE_RESULT_STATES = {"success", "failed"}


async def ingest_restore_result(
    session: AsyncSession, job: Job, result: BackupRestoreResult
) -> None:
    """Reconcile a completed `backup.restore` job's result (docs/modules/
    backup/spec.md).

    Finds the `RestoreJob` row `dispatch_restore` created for this job. An
    agent report for a job with no matching row (e.g. the row was never
    created, or this is a stale/duplicate report) is skipped gracefully
    rather than raising — the agent's report must never be able to fail the
    job over server-side bookkeeping.

    `result.state == "success"` flips the restore job to `success`; any
    other case — `"failed"`, or an out-of-domain value that bypassed wire
    validation — lands it on `failed` with an `error` message (the original
    out-of-domain value is also recorded in the audit metadata). Either way
    `restored_bytes`/`finished_at` are always set.

    Proven-restore: a `verify`-mode restore job whose result is a verified
    success with no path mismatches proves the linked `BackupPoint` is
    actually restorable, so its `restore_tested_at` is stamped. A `restore`
    -mode success (no verification performed) or a `verify` with a non-empty
    `mismatch` must NOT stamp it.

    Finishes with a single `backup.restore_result` audit event, system-
    attributed since this runs in the kernel's result handler, not on behalf
    of any user. The metadata carries only `state`/`verified`/
    `mismatch_count` — never the mismatch paths themselves or any other
    file/key data (invariant #3).

    EVERY query carries an explicit `org_id` predicate (invariant #5).
    """
    org_id = job.org_id
    now = datetime.now(UTC)

    restore_job = (
        await session.execute(
            select(RestoreJob).where(
                RestoreJob.org_id == org_id,
                RestoreJob.job_id == job.id,
            )
        )
    ).scalar_one_or_none()
    if restore_job is None:
        return  # no matching row; the agent's report must never fail the job
    # Read now: the state update below expires it (`server_onupdate`, written
    # by trigger), and an async session cannot lazy-load it.
    client_id = restore_job.client_id

    audit_metadata: dict[str, Any] = {}

    if result.state == "success":
        restore_job.state = "success"
    else:
        restore_job.state = "failed"
        restore_job.error = result.error or f"unexpected result state {result.state!r}"
        if result.state not in _VALID_RESTORE_RESULT_STATES:
            audit_metadata["unexpected_result_state"] = result.state

    restore_job.restored_bytes = result.restored_bytes
    restore_job.finished_at = now
    restore_job.warnings = result.warnings

    if (
        restore_job.mode == "verify"
        and result.state == "success"
        and result.verified is True
        and not result.mismatch
    ):
        point = (
            await session.execute(
                select(BackupPoint).where(
                    BackupPoint.org_id == org_id,
                    BackupPoint.id == restore_job.backup_point_id,
                )
            )
        ).scalar_one_or_none()
        if point is not None:
            point.restore_tested_at = now
            # What was proven: files read back and hash-checked, either a
            # sample (a scheduled test) or the whole point.
            paths = (restore_job.selection or {}).get("paths") or []
            point.restore_test_level = "read"
            point.restore_test_scope = "sampled" if paths else "full"

    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="backup.restore_result",
        target_type="device",
        target_id=str(job.target_id),
        metadata={
            **audit_metadata,
            "state": restore_job.state,
            "verified": result.verified,
            "mismatch_count": len(result.mismatch or []),
        },
        client_id=client_id,
    )

    await session.flush()
