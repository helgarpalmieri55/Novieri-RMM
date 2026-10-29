"""The off-site copy (spec-server-backup.md §6.2-§6.6).

A server-side worker copies already-encrypted objects from the local MinIO
repository to one S3-compatible bucket per repository. Endpoints never see the
off-site target, its credential or its existence: doubling their upload and
putting a grant for the ransomware-recovery copy on the machines most likely
to be compromised is what §6.3 rejects.

Security properties, each tested:

- The target credential is an ordinary vault-sealed `credentials` row of type
  `s3_offsite`. The API only ever encrypts it; `open_target_client` is the one
  place it is decrypted, and only the worker calls it (invariant 3). Every
  decrypt is audited `credential.accessed`.
- A bucket without Object Lock is refused (operator decision 2026-09-23:
  compliance mode, 30 days). Every object is written with a retain-until date,
  and a chunk shared by a newer point has its lock extended.
- Off-site chunk names are keyed (`recovery.offsite_chunk_name`), so the
  provider cannot test for a known file by its content hash.
- Only ciphertext moves: the worker opens the repository key to read the
  manifest's chunk list and to name chunks, never to decrypt a data chunk.
- Nothing is copied until the org has a proven recovery key, and each
  repository key is written off-site wrapped to every proven key that is
  global or of the repository's client -- never to another client's, whose
  operators could then open this client's copy (MSP clients plan 2026-10-26
  Task 13). A repository's target is global or of its client, too.
- Rows make replication resumable at chunk granularity; each step commits.

Off-site layout, under `{prefix}{repository_id}/`:
`chunks/..`, `manifests/<name>`, `keys/repository-key.<fp>.json`,
`catalog/repository.json`, `catalog/points/<point_id>`.
"""

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import N_, tr, tr_marked
from app.core.storage import ObjectStore
from app.core.stored_english import stored_in_english
from app.core.vault import VaultService
from app.models import Credential
from app.modules.backup.manifest import referenced_chunk_ids
from app.modules.backup.models import (
    BackupChunkCopy,
    BackupOffsiteTarget,
    BackupPoint,
    BackupPointCopy,
    BackupRepository,
)
from app.modules.backup.recovery import (
    chunk_name_key,
    offsite_chunk_name,
    parse_public_key,
    proven_recovery_keys,
    repository_key_object,
)
from app.modules.backup.repository import open_repo_key
from app.modules.backup.retention import select_gfs

CREDENTIAL_TYPE = "s3_offsite"
# Chunks copied per step; each step is its own transaction.
STEP_CHUNKS = 256

LOCK_REQUIRED_MESSAGE = N_(
    "This bucket does not have Object Lock turned on. Off-site copies need a bucket "
    "that was created with Object Lock, so that nobody, including someone who takes "
    "over this server, can delete or change a backup before its lock runs out. "
    "Create a new bucket with Object Lock enabled and use that one."
)
NO_RECOVERY_KEY_MESSAGE = N_(
    "Set up a recovery key and confirm it before turning on off-site copies. "
    "Without it, the off-site copy cannot be read if this server is lost."
)
NOT_TESTED_MESSAGE = N_(
    "Test the off-site storage successfully before turning on off-site copies."
)


class OffsiteError(ValueError):
    """A refusal whose message is safe to show the operator."""


class OffsiteClientError(OffsiteError):
    """A target of another client than the repository's (MSP clients plan
    2026-10-26 Task 13): a 422 with the sentence, not the 409 of a target
    that is not ready yet."""


@dataclass(frozen=True)
class OffsiteSecret:
    access_key_id: str
    secret_access_key: str

    def as_vault_plaintext(self) -> str:
        return json.dumps(
            {"access_key_id": self.access_key_id, "secret_access_key": self.secret_access_key}
        )


def parse_offsite_secret(raw: str) -> OffsiteSecret:
    try:
        parsed = json.loads(raw)
        return OffsiteSecret(str(parsed["access_key_id"]), str(parsed["secret_access_key"]))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise OffsiteError(tr("The stored off-site credential is not usable.")) from exc


ClientFactory = Callable[[BackupOffsiteTarget, OffsiteSecret], Any]


def build_client(target: BackupOffsiteTarget, secret: OffsiteSecret) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=target.endpoint or None,
        region_name=target.region,
        aws_access_key_id=secret.access_key_id,
        aws_secret_access_key=secret.secret_access_key,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": target.addressing},
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def repository_base(target: BackupOffsiteTarget, repository_id: UUID) -> str:
    prefix = target.prefix.strip("/")
    return f"{prefix}/{repository_id}/" if prefix else f"{repository_id}/"


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# S3 calls (sync boto3 run off the event loop)
# --------------------------------------------------------------------------- #


async def _call(fn: Callable[..., Any], /, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, **kwargs)


def _lock(target: BackupOffsiteTarget, until: datetime) -> dict[str, Any]:
    return {
        "ObjectLockMode": target.lock_mode.upper(),
        "ObjectLockRetainUntilDate": until,
        # Object Lock buckets refuse a PUT without an integrity checksum.
        "ChecksumAlgorithm": "SHA256",
    }


async def _put_locked(
    client: Any, target: BackupOffsiteTarget, key: str, body: bytes, until: datetime
) -> str | None:
    response = await _call(
        client.put_object, Bucket=target.bucket, Key=key, Body=body, **_lock(target, until)
    )
    version = response.get("VersionId")
    return str(version) if version else None


async def _exists(client: Any, bucket: str, key: str) -> bool:
    try:
        await _call(client.head_object, Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


async def object_lock_enabled(client: Any, bucket: str) -> bool:
    try:
        response = await _call(client.get_object_lock_configuration, Bucket=bucket)
    except ClientError:
        return False
    return bool(
        response.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") == "Enabled"
    )


# --------------------------------------------------------------------------- #
# Targets and switching off-site on (API side; never decrypts the credential)
# --------------------------------------------------------------------------- #


async def get_target(
    session: AsyncSession, org_id: UUID, target_id: UUID
) -> BackupOffsiteTarget | None:
    return (
        await session.execute(
            select(BackupOffsiteTarget).where(
                BackupOffsiteTarget.org_id == org_id, BackupOffsiteTarget.id == target_id
            )
        )
    ).scalar_one_or_none()


#: The target-name key (migration 0231, Task 8b): unique per client, the global
#: targets among themselves.
TARGET_NAME_KEY = "uq_backup_offsite_targets_client_name"


async def target_name_taken(
    session: AsyncSession, org_id: UUID, client_id: UUID | None, name: str
) -> bool:
    """Is `name` another target's in `client_id`'s namespace (None: the global
    targets')? As `TARGET_NAME_KEY` is: a client's target does not take a
    global name, nor one global target another client's."""
    found = (
        await session.execute(
            select(BackupOffsiteTarget.id).where(
                BackupOffsiteTarget.org_id == org_id,
                BackupOffsiteTarget.client_id.is_not_distinct_from(client_id),
                BackupOffsiteTarget.name == name,
            )
        )
    ).first()
    return found is not None


async def _store_secret(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    secret: OffsiteSecret,
    credential: Credential | None = None,
) -> Credential:
    if credential is None:
        credential = Credential(
            org_id=org_id, name=name, type=CREDENTIAL_TYPE, vault_ref="", created_by=actor_id
        )
        session.add(credential)
        await session.flush()
        action = "credential.created"
    else:
        action = "credential.rotated"
    stored = await vault.store(
        session, org_id=org_id, credential_id=credential.id, plaintext=secret.as_vault_plaintext()
    )
    credential.ciphertext = stored.ciphertext_b64
    credential.wrapped_dek = stored.wrapped_dek
    credential.key_version = stored.key_version
    credential.vault_ref = f"envelope:{credential.id}"
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type="credential",
        target_id=str(credential.id),
        metadata={"name": credential.name, "type": CREDENTIAL_TYPE},
    )
    return credential


async def create_target(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    settings: dict[str, Any],
    secret: OffsiteSecret,
) -> BackupOffsiteTarget:
    credential = await _store_secret(
        session,
        vault,
        org_id=org_id,
        actor_id=actor_id,
        name=f"Off-site storage: {settings['name']}"[:200],
        secret=secret,
    )
    target = BackupOffsiteTarget(
        org_id=org_id, credential_id=credential.id, created_by=actor_id, **settings
    )
    session.add(target)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="backup.offsite_target_created",
        target_type="backup_offsite_target",
        target_id=str(target.id),
        metadata={"name": target.name, "lock_mode": target.lock_mode,
                  "immutable_days": target.immutable_days},
    )
    await session.flush()
    return target


# Changing where or how the copy is written invalidates the last test.
_RETEST_FIELDS = frozenset({"endpoint", "region", "bucket", "prefix", "addressing", "lock_mode"})


async def update_target(
    session: AsyncSession,
    vault: VaultService,
    target: BackupOffsiteTarget,
    *,
    actor_id: UUID,
    settings: dict[str, Any],
    secret: OffsiteSecret | None,
) -> BackupOffsiteTarget:
    changed = sorted(k for k, v in settings.items() if getattr(target, k) != v)
    for key, value in settings.items():
        setattr(target, key, value)
    if secret is not None:
        credential = (
            await session.execute(
                select(Credential).where(
                    Credential.org_id == target.org_id, Credential.id == target.credential_id
                )
            )
        ).scalar_one()
        await _store_secret(
            session, vault, org_id=target.org_id, actor_id=actor_id, name=credential.name,
            secret=secret, credential=credential,
        )
        changed.append("credential")
    if secret is not None or _RETEST_FIELDS & set(changed):
        target.state = "untested"
    await audit_service.record(
        session,
        org_id=target.org_id,
        actor_type="user",
        actor_id=actor_id,
        action="backup.offsite_target_updated",
        target_type="backup_offsite_target",
        target_id=str(target.id),
        metadata={"changed": changed},
    )
    await session.flush()
    return target


async def request_test(
    session: AsyncSession, target: BackupOffsiteTarget, *, actor_id: UUID, now: datetime
) -> None:
    """The worker runs the test (it alone may decrypt the credential)."""
    target.test_requested_at = now
    target.state = "testing"
    await audit_service.record(
        session,
        org_id=target.org_id,
        actor_type="user",
        actor_id=actor_id,
        action="backup.offsite_target_test_requested",
        target_type="backup_offsite_target",
        target_id=str(target.id),
        metadata={},
    )
    await session.flush()


async def set_repository_target(
    session: AsyncSession,
    *,
    org_id: UUID,
    repository_id: UUID,
    target_id: UUID | None,
    actor_id: UUID,
) -> BackupRepository:
    """Switch off-site copying on (a tested target) or off (None) for one
    repository. On requires a target that is global or of the repository's
    client (`OffsiteClientError`, a 422), a proven recovery key its key can
    be wrapped to (global or of that client, as `prepare_repository` wraps),
    and a passed test. Audited in the repository's client."""
    repo = (
        await session.execute(
            select(BackupRepository).where(
                BackupRepository.org_id == org_id, BackupRepository.id == repository_id
            )
        )
    ).scalar_one_or_none()
    if repo is None:
        raise LookupError(tr("no such repository"))
    client_id = repo.client_id
    if target_id is not None:
        target = await get_target(session, org_id, target_id)
        if target is None:
            raise LookupError(tr("no such off-site target"))
        if target.client_id is not None and target.client_id != client_id:
            raise OffsiteClientError(tr("that off-site target belongs to another client"))
        if not await proven_recovery_keys(session, org_id, client_id=client_id):
            raise OffsiteError(tr_marked(NO_RECOVERY_KEY_MESSAGE))
        if target.state != "ok":
            raise OffsiteError(tr_marked(NOT_TESTED_MESSAGE))
    previous = repo.offsite_target_id
    repo.offsite_target_id = target_id
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="backup.offsite_enabled" if target_id else "backup.offsite_disabled",
        target_type="backup_repository",
        target_id=str(repository_id),
        metadata={
            "target_id": str(target_id) if target_id else None,
            "previous_target_id": str(previous) if previous else None,
        },
        client_id=client_id,
    )
    await session.flush()
    return repo


# --------------------------------------------------------------------------- #
# Worker side
# --------------------------------------------------------------------------- #


async def open_target_client(
    session: AsyncSession,
    vault: VaultService,
    target: BackupOffsiteTarget,
    client_factory: ClientFactory,
    *,
    purpose: str,
    now: datetime,
) -> Any:
    """Decrypt the target credential at the point of use and build a client.
    Audited `credential.accessed`; the secret goes nowhere but the client."""
    credential = (
        await session.execute(
            select(Credential).where(
                Credential.org_id == target.org_id, Credential.id == target.credential_id
            )
        )
    ).scalar_one()
    raw = await vault.checkout(session, org_id=target.org_id, credential_id=credential.id)
    secret = parse_offsite_secret(raw)
    credential.last_used_at = now
    await audit_service.record(
        session,
        org_id=target.org_id,
        actor_type="system",
        actor_id=None,
        action="credential.accessed",
        target_type="credential",
        target_id=str(credential.id),
        metadata={"purpose": purpose, "offsite_target_id": str(target.id)},
    )
    await session.flush()
    return client_factory(target, secret)


def _safe_error(exc: BaseException) -> str:
    """An S3 error code, never a message that may echo keys or URLs."""
    if isinstance(exc, ClientError):
        return f"storage error {exc.response.get('Error', {}).get('Code', 'unknown')}"
    if isinstance(exc, BotoCoreError):
        return f"cannot reach the storage ({type(exc).__name__})"
    if isinstance(exc, OffsiteError):
        return str(exc)
    return f"unexpected error ({type(exc).__name__})"


@stored_in_english
async def run_target_test(
    session: AsyncSession,
    vault: VaultService,
    target: BackupOffsiteTarget,
    client_factory: ClientFactory,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Check the bucket can hold the off-site copy: Object Lock on, write, read,
    the lock applied, and a locked version refusing deletion."""
    steps: list[dict[str, Any]] = []

    def record(step: str, ok: bool, detail: str = "") -> bool:
        steps.append({"step": step, "ok": ok, "detail": detail})
        return ok

    try:
        client = await open_target_client(
            session, vault, target, client_factory, purpose="backup.offsite_test", now=now
        )
        locked = await object_lock_enabled(client, target.bucket)
        if record("object_lock", locked, "" if locked else LOCK_REQUIRED_MESSAGE):
            prefix = target.prefix.strip("/")
            key = f"{prefix}/" if prefix else ""
            key += f".itms-probe/{os.urandom(8).hex()}"
            body = os.urandom(64)
            version = await _put_locked(client, target, key, body, now + timedelta(days=1))
            record("write", True)
            got = await _call(client.get_object, Bucket=target.bucket, Key=key)
            record("read", got["Body"].read() == body, "")
            head = await _call(client.head_object, Bucket=target.bucket, Key=key)
            mode = head.get("ObjectLockMode")
            record(
                "lock",
                mode == target.lock_mode.upper(),
                "" if mode else "Objects are stored without a lock.",
            )
            refused = False
            if version:
                try:
                    await _call(
                        client.delete_object, Bucket=target.bucket, Key=key, VersionId=version
                    )
                except ClientError:
                    refused = True
            record(
                "delete_refused",
                refused,
                "" if refused else "A locked backup could be deleted. Check the bucket's lock.",
            )
    except Exception as exc:  # noqa: BLE001 - reported as a failed step
        record("connect", False, _safe_error(exc))

    passed = all(s["ok"] for s in steps) and bool(steps)
    target.state = "ok" if passed else "failing"
    target.test_requested_at = None
    target.last_test = {"at": now.isoformat(), "steps": steps}
    if passed:
        target.last_ok_at = now
        target.last_error = None
    else:
        target.last_error = next((s["detail"] for s in steps if not s["ok"]), "test failed")
    await audit_service.record(
        session,
        org_id=target.org_id,
        actor_type="system",
        actor_id=None,
        action="backup.offsite_target_tested",
        target_type="backup_offsite_target",
        target_id=str(target.id),
        metadata={"passed": passed, "failed_steps": [s["step"] for s in steps if not s["ok"]]},
    )
    await session.flush()
    return target.last_test


async def manifest_chunk_ids(
    local_store: ObjectStore, point: BackupPoint, repo_key: bytes, manifest_blob: bytes
) -> list[str]:
    """Every chunk object a point keeps alive (data chunks and v2 pages),
    sorted so a resumed copy walks them in the same order."""

    async def fetch(chunk_id: str) -> bytes:
        return await local_store.get_backup_chunk(point.org_id, point.repository_id, chunk_id)

    return sorted(await referenced_chunk_ids(repo_key, manifest_blob, fetch))


def _object_suffix(manifest_ref: str) -> str:
    """`manifests/<name>` from a local manifest ref."""
    marker = "/manifests/"
    return "manifests/" + manifest_ref.rsplit(marker, 1)[-1]


def seal_catalog_point(repo_key: bytes, point_id: UUID, document: dict[str, Any]) -> bytes:
    nonce = os.urandom(12)
    aad = b"itms catalog point v1:" + str(point_id).encode()
    return nonce + AESGCM(repo_key).encrypt(nonce, json.dumps(document).encode(), aad)


@dataclass(frozen=True)
class PendingCopy:
    point_id: UUID
    target_id: UUID
    repository_id: UUID


async def pending_copies(session: AsyncSession, org_id: UUID) -> list[PendingCopy]:
    """Complete points in repositories with a target that are not yet fully
    copied there, oldest first."""
    rows = (
        await session.execute(
            select(BackupPoint.id, BackupRepository.offsite_target_id, BackupRepository.id)
            .join(
                BackupRepository,
                (BackupRepository.id == BackupPoint.repository_id)
                & (BackupRepository.org_id == BackupPoint.org_id),
            )
            .join(
                BackupOffsiteTarget,
                (BackupOffsiteTarget.id == BackupRepository.offsite_target_id)
                & (BackupOffsiteTarget.org_id == BackupRepository.org_id),
            )
            .outerjoin(
                BackupPointCopy,
                (BackupPointCopy.point_id == BackupPoint.id)
                & (BackupPointCopy.target_id == BackupRepository.offsite_target_id)
                & (BackupPointCopy.org_id == BackupPoint.org_id),
            )
            .where(
                BackupPoint.org_id == org_id,
                BackupPoint.state == "complete",
                BackupOffsiteTarget.state == "ok",
                (BackupPointCopy.id.is_(None))
                | BackupPointCopy.state.in_(("pending", "copying", "failed")),
            )
            .order_by(BackupPoint.taken_at, BackupPoint.id)
        )
    ).all()
    return [PendingCopy(r[0], r[1], r[2]) for r in rows if r[1] is not None]


async def prepare_repository(
    session: AsyncSession,
    client: Any,
    target: BackupOffsiteTarget,
    repo: BackupRepository,
    repo_key: bytes,
    *,
    now: datetime,
) -> None:
    """Refuse an unlockable bucket and a keyless org, then make sure the key
    objects and the repository catalog are there. Raises `OffsiteError`.

    The repository key is wrapped to the proven recovery keys that are global
    or of the repository's client, and to no other client's (module
    docstring)."""
    if not await object_lock_enabled(client, target.bucket):
        raise OffsiteError(tr_marked(LOCK_REQUIRED_MESSAGE))
    keys = await proven_recovery_keys(session, repo.org_id, client_id=repo.client_id)
    if not keys:
        raise OffsiteError(tr_marked(NO_RECOVERY_KEY_MESSAGE))
    base = repository_base(target, repo.id)
    until = now + timedelta(days=target.immutable_days)
    for key in keys:
        name = f"{base}keys/repository-key.{key.fingerprint}.json"
        if not await _exists(client, target.bucket, name):
            await _put_locked(
                client, target, name,
                repository_key_object(parse_public_key(key.public_key), repo_key, repo.id),
                until,
            )
    catalog = json.dumps(
        {
            "format": 1,
            "repository_id": str(repo.id),
            "dedup_params": repo.dedup_params,
            "chunk_names": "hmac-sha256-v1",
            "key_fingerprints": sorted(k.fingerprint for k in keys),
        },
        sort_keys=True,
    ).encode()
    name = f"{base}catalog/repository.json"
    current: bytes | None = None
    try:
        got = await _call(client.get_object, Bucket=target.bucket, Key=name)
        current = got["Body"].read()
    except ClientError:
        current = None
    if current != catalog:
        await _put_locked(client, target, name, catalog, until)


async def _throttle(target: BackupOffsiteTarget, sent: int, started: float) -> None:
    if not target.max_mbps:
        return
    due = sent * 8 / (target.max_mbps * 1_000_000)
    elapsed = time.monotonic() - started
    if due > elapsed:
        await asyncio.sleep(due - elapsed)


async def replicate_step(
    session: AsyncSession,
    *,
    local_store: ObjectStore,
    client: Any,
    target: BackupOffsiteTarget,
    point: BackupPoint,
    repo_key: bytes,
    now: datetime,
    hostname: str | None = None,
    batch: int = STEP_CHUNKS,
) -> bool:
    """Copy up to `batch` missing chunks of `point`; when none remain, extend
    locks, write the manifest and catalog entry and mark the copy complete.
    Returns True once the copy is complete. Idempotent: re-running a step
    after a crash re-copies at most the chunks of the uncommitted batch."""
    org_id = point.org_id
    copy = (
        await session.execute(
            select(BackupPointCopy).where(
                BackupPointCopy.org_id == org_id,
                BackupPointCopy.point_id == point.id,
                BackupPointCopy.target_id == target.id,
            )
        )
    ).scalar_one_or_none()
    if copy is None:
        copy = BackupPointCopy(org_id=org_id, point_id=point.id, target_id=target.id,
                               state="copying", attempts=1)
        session.add(copy)
        await session.flush()
    elif copy.state == "complete":
        return True
    elif copy.state != "copying":
        copy.state = "copying"
        copy.attempts += 1

    base = repository_base(target, point.repository_id)
    name_key = chunk_name_key(repo_key)
    lock_until = now + timedelta(days=target.immutable_days)
    manifest_blob = await local_store.get_backup_object(point.manifest_ref)
    chunk_ids = await manifest_chunk_ids(local_store, point, repo_key, manifest_blob)

    existing: dict[str, BackupChunkCopy] = {}
    for start in range(0, len(chunk_ids), 1000):
        rows = (
            await session.execute(
                select(BackupChunkCopy).where(
                    BackupChunkCopy.org_id == org_id,
                    BackupChunkCopy.target_id == target.id,
                    BackupChunkCopy.repository_id == point.repository_id,
                    BackupChunkCopy.chunk_id.in_(chunk_ids[start : start + 1000]),
                )
            )
        ).scalars().all()
        existing.update({r.chunk_id: r for r in rows})

    missing = [c for c in chunk_ids if c not in existing]
    started = time.monotonic()
    sent = 0
    for cid in missing[:batch]:
        blob = await local_store.get_backup_chunk(org_id, point.repository_id, cid)
        name = base + offsite_chunk_name(name_key, cid)
        version = await _put_locked(client, target, name, blob, lock_until)
        session.add(
            BackupChunkCopy(
                org_id=org_id, repository_id=point.repository_id, target_id=target.id,
                chunk_id=cid, object_name=name, version_id=version, locked_until=lock_until,
            )
        )
        sent += len(blob)
        copy.bytes += len(blob)
        copy.chunks_copied += 1
        await _throttle(target, sent, started)
    if len(missing) > batch:
        await session.flush()
        return False

    # A chunk shared with older points may be near the end of its lock. Extend
    # to twice the window so each chunk is extended at most once per window.
    extend_to = now + timedelta(days=2 * target.immutable_days)
    for row in existing.values():
        if row.locked_until is not None and _utc(row.locked_until) >= lock_until:
            continue
        retention: dict[str, Any] = {
            "Bucket": target.bucket,
            "Key": row.object_name,
            "Retention": {"Mode": target.lock_mode.upper(), "RetainUntilDate": extend_to},
            "ChecksumAlgorithm": "SHA256",
        }
        if row.version_id:
            retention["VersionId"] = row.version_id
        await _call(client.put_object_retention, **retention)
        row.locked_until = extend_to

    manifest_name = base + _object_suffix(point.manifest_ref)
    copy.manifest_object = manifest_name
    copy.manifest_version_id = await _put_locked(
        client, target, manifest_name, manifest_blob, lock_until
    )
    catalog_doc = {
        "point_id": str(point.id),
        "device_id": str(point.device_id),
        "hostname": hostname,
        "taken_at": point.taken_at.isoformat(),
        "manifest_object": manifest_name.removeprefix(base),
        "size": point.size,
    }
    copy.catalog_version_id = await _put_locked(
        client, target, f"{base}catalog/points/{point.id}",
        seal_catalog_point(repo_key, point.id, catalog_doc), lock_until,
    )
    copy.state = "complete"
    copy.copied_at = now
    copy.locked_until = lock_until
    copy.last_error = None
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="backup.replicated",
        target_type="backup_point",
        target_id=str(point.id),
        metadata={
            "target_id": str(target.id),
            "chunks_copied": copy.chunks_copied,
            "chunks_total": len(chunk_ids),
            "bytes": copy.bytes,
        },
    )
    await session.flush()
    return True


async def record_copy_failure(
    session: AsyncSession, *, org_id: UUID, point_id: UUID, target_id: UUID, error: BaseException
) -> None:
    copy = (
        await session.execute(
            select(BackupPointCopy).where(
                BackupPointCopy.org_id == org_id,
                BackupPointCopy.point_id == point_id,
                BackupPointCopy.target_id == target_id,
            )
        )
    ).scalar_one_or_none()
    if copy is None:
        copy = BackupPointCopy(org_id=org_id, point_id=point_id, target_id=target_id)
        session.add(copy)
    copy.state = "failed"
    copy.last_error = _safe_error(error)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="backup.replication_failed",
        target_type="backup_point",
        target_id=str(point_id),
        metadata={"target_id": str(target_id), "error": copy.last_error},
    )
    await session.flush()


@stored_in_english
async def replicate_point(
    session_factory: Callable[[], Any],
    *,
    vault: VaultService,
    local_store: ObjectStore,
    client_factory: ClientFactory,
    item: PendingCopy,
    org_id: UUID,
    now: datetime,
    deadline: float | None = None,
    batch: int = STEP_CHUNKS,
) -> bool:
    """Drive `replicate_step` to completion for one point, one transaction per
    step (`session_factory` yields an `org_session`). Returns whether the copy
    completed; failures are recorded on the copy row, never raised."""
    client: Any = None
    try:
        while True:
            async with session_factory() as session:
                target = await get_target(session, org_id, item.target_id)
                point = (
                    await session.execute(
                        select(BackupPoint).where(
                            BackupPoint.org_id == org_id, BackupPoint.id == item.point_id
                        )
                    )
                ).scalar_one_or_none()
                repo = (
                    await session.execute(
                        select(BackupRepository).where(
                            BackupRepository.org_id == org_id,
                            BackupRepository.id == item.repository_id,
                        )
                    )
                ).scalar_one_or_none()
                if target is None or point is None or repo is None:
                    return False
                repo_key = await open_repo_key(
                    session, vault, org_id=org_id, repository_id=repo.id
                )
                if client is None:
                    client = await open_target_client(
                        session, vault, target, client_factory,
                        purpose="backup.replication", now=now,
                    )
                    await prepare_repository(session, client, target, repo, repo_key, now=now)
                hostname = await _hostname(session, org_id, point.device_id)
                done = await replicate_step(
                    session, local_store=local_store, client=client, target=target,
                    point=point, repo_key=repo_key, now=now, hostname=hostname, batch=batch,
                )
            if done:
                return True
            if deadline is not None and time.monotonic() > deadline:
                return False
    except Exception as exc:  # noqa: BLE001 - one point's failure is recorded, not raised
        async with session_factory() as session:
            await record_copy_failure(
                session, org_id=org_id, point_id=item.point_id, target_id=item.target_id,
                error=exc,
            )
            if isinstance(exc, OffsiteError):
                target = await get_target(session, org_id, item.target_id)
                if target is not None:
                    target.last_error = str(exc)
        return False


async def _hostname(session: AsyncSession, org_id: UUID, device_id: UUID) -> str | None:
    from app.models import Device

    return (
        await session.execute(
            select(Device.hostname).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Retention per copy and off-site GC (§6.6)
# --------------------------------------------------------------------------- #


async def offsite_gc(
    session: AsyncSession,
    *,
    vault: VaultService,
    local_store: ObjectStore,
    client_factory: ClientFactory,
    org_id: UUID,
    now: datetime,
) -> dict[str, int]:
    """Apply each target's own GFS retention to its complete copies, then
    delete off-site object versions nothing live references once their lock
    has passed. Locks make early deletion impossible anyway; checking first
    avoids a refused call per object."""
    expired = deleted = 0
    targets = (
        await session.execute(
            select(BackupOffsiteTarget).where(BackupOffsiteTarget.org_id == org_id)
        )
    ).scalars().all()
    for target in targets:
        if not target.retention_gfs:
            continue
        rows = (
            await session.execute(
                select(BackupPointCopy, BackupPoint)
                .join(
                    BackupPoint,
                    (BackupPoint.id == BackupPointCopy.point_id)
                    & (BackupPoint.org_id == BackupPointCopy.org_id),
                )
                .where(
                    BackupPointCopy.org_id == org_id,
                    BackupPointCopy.target_id == target.id,
                    BackupPointCopy.state.in_(("complete", "expired")),
                )
            )
        ).all()
        if not rows:
            continue
        by_device: dict[tuple[UUID, UUID], list[BackupPoint]] = {}
        copies = {point.id: copy for copy, point in rows}
        points = {point.id: point for _copy, point in rows}
        for copy, point in rows:
            if copy.state == "complete":
                by_device.setdefault((point.repository_id, point.device_id), []).append(point)
        for group in by_device.values():
            # Retention is judged on copies, whatever the local point's state.
            proxies = [_GfsProxy(p.id, p.taken_at) for p in group]
            _keep, prune = select_gfs(proxies, target.retention_gfs, now)  # type: ignore[arg-type]
            for pid in prune:
                copies[pid].state = "expired"
                expired += 1
        await session.flush()

        client = None
        for repository_id in {p.repository_id for p in points.values()}:
            live = [
                points[pid] for pid, c in copies.items()
                if c.state == "complete" and points[pid].repository_id == repository_id
            ]
            repo_key = await open_repo_key(session, vault, org_id=org_id,
                                           repository_id=repository_id)
            referenced: set[str] = set()
            try:
                for point in live:
                    blob = await local_store.get_backup_object(point.manifest_ref)
                    referenced.update(
                        await manifest_chunk_ids(local_store, point, repo_key, blob)
                    )
            except Exception:  # noqa: BLE001 - unreadable manifest: delete nothing
                continue
            orphans = (
                await session.execute(
                    select(BackupChunkCopy).where(
                        BackupChunkCopy.org_id == org_id,
                        BackupChunkCopy.target_id == target.id,
                        BackupChunkCopy.repository_id == repository_id,
                    )
                )
            ).scalars().all()
            for row in orphans:
                if row.chunk_id in referenced:
                    continue
                if row.locked_until is not None and _utc(row.locked_until) > now:
                    continue
                if client is None:
                    client = await open_target_client(
                        session, vault, target, client_factory, purpose="backup.offsite_gc",
                        now=now,
                    )
                kwargs: dict[str, Any] = {"Bucket": target.bucket, "Key": row.object_name}
                if row.version_id:
                    kwargs["VersionId"] = row.version_id
                await _call(client.delete_object, **kwargs)
                await session.delete(row)
                deleted += 1
        await session.flush()
    if expired or deleted:
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="backup.offsite_gc",
            target_type="organization",
            target_id=str(org_id),
            metadata={"copies_expired": expired, "objects_deleted": deleted},
        )
        await session.flush()
    return {"expired": expired, "deleted": deleted}


@dataclass(frozen=True)
class _GfsProxy:
    id: UUID
    taken_at: datetime
    state: str = "complete"


async def offsite_hold(
    session: AsyncSession, org_id: UUID, point_ids: set[UUID]
) -> set[UUID]:
    """Of `point_ids`, those local GC must keep: their repository has an
    off-site target and their copy there is not complete (§6.1)."""
    if not point_ids:
        return set()
    rows = (
        await session.execute(
            select(BackupPoint.id, BackupPointCopy.state)
            .join(
                BackupRepository,
                (BackupRepository.id == BackupPoint.repository_id)
                & (BackupRepository.org_id == BackupPoint.org_id),
            )
            .outerjoin(
                BackupPointCopy,
                (BackupPointCopy.point_id == BackupPoint.id)
                & (BackupPointCopy.target_id == BackupRepository.offsite_target_id),
            )
            .where(
                BackupPoint.org_id == org_id,
                BackupPoint.id.in_(point_ids),
                BackupRepository.offsite_target_id.is_not(None),
            )
        )
    ).all()
    return {pid for pid, state in rows if state != "complete"}
