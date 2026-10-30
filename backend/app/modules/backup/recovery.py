"""Recovery keys: reading the off-site copy without the ITMS host
(spec-server-backup.md §6.4, §6.5).

Every repository key lives in the vault under the org DEK, whose wrapping key
lives on the ITMS host. Lose the host and its platform backup, and the off-site
copy is ciphertext nobody can read. So each repository key is also wrapped to
an operator-held X25519 public key and written next to the data off-site.

The operator generates the keypair offline (`scripts/itms_recover.py keygen`),
pastes the public key, and proves possession by decrypting a challenge. The
private key never reaches the server. Off-site copying cannot be switched on
until the org has a proven key (operator decision 2026-09-23).

Wrap format (`X25519-HKDF-SHA256-AES-256-GCM`, version 1), shared with the
recovery tool, which reimplements it without importing this module:

    shared = X25519(ephemeral_private, recipient_public)
    key    = HKDF-SHA256(shared, salt=epk || recipient_public, info=INFO, 32)
    ct     = AES-256-GCM(key, nonce, plaintext, aad)
    object = {v: 1, alg, recipient: fingerprint, epk, nonce, ct}   (base64)
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.modules.backup.integrity import violated_constraint
from app.modules.backup.models import BackupRecoveryKey

ALG = "X25519-HKDF-SHA256-AES-256-GCM"
_WRAP_INFO = b"itms recovery key wrap v1"
_CHALLENGE_AAD = b"itms recovery challenge v1:"
_CHUNK_NAME_INFO = b"itms offsite chunk names v1"
CHALLENGE_TTL = timedelta(hours=24)


class RecoveryKeyError(ValueError):
    """An operator-supplied key or answer that cannot be used; the message is
    safe to show."""


def parse_public_key(text: str) -> bytes:
    """The raw 32 bytes of a pasted public key (base64, optional
    `itms-recovery-public:` prefix, surrounding whitespace ignored)."""
    value = text.strip()
    if value.startswith("itms-recovery-public:"):
        value = value.split(":", 1)[1].strip()
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RecoveryKeyError(tr("That is not a recovery public key.")) from exc
    if len(raw) != 32:
        raise RecoveryKeyError(tr("That is not a recovery public key."))
    return raw


def fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()[:32]


def _derive(shared: bytes, epk: bytes, recipient: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=epk + recipient, info=_WRAP_INFO
    ).derive(shared)


def wrap(public_key: bytes, plaintext: bytes, aad: bytes) -> dict[str, Any]:
    ephemeral = X25519PrivateKey.generate()
    epk = ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(public_key))
    nonce = os.urandom(12)
    ct = AESGCM(_derive(shared, epk, public_key)).encrypt(nonce, plaintext, aad)
    return {
        "v": 1,
        "alg": ALG,
        "recipient": fingerprint(public_key),
        "epk": base64.b64encode(epk).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(ct).decode(),
    }


def unwrap(private_key: bytes, obj: dict[str, Any], aad: bytes) -> bytes:
    """Inverse of `wrap`. The server never holds a private key; this exists for
    the tests that prove the recovery tool and the server agree."""
    priv = X25519PrivateKey.from_private_bytes(private_key)
    recipient = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    epk = base64.b64decode(obj["epk"])
    shared = priv.exchange(X25519PublicKey.from_public_bytes(epk))
    return AESGCM(_derive(shared, epk, recipient)).decrypt(
        base64.b64decode(obj["nonce"]), base64.b64decode(obj["ct"]), aad
    )


def repository_key_object(public_key: bytes, repo_key: bytes, repository_id: UUID) -> bytes:
    """`keys/repository-key.<fingerprint>.json`, AAD = the repository id."""
    return json.dumps(
        wrap(public_key, repo_key, str(repository_id).encode()), sort_keys=True
    ).encode()


def chunk_name_key(repo_key: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_CHUNK_NAME_INFO).derive(
        repo_key
    )


def offsite_chunk_name(name_key: bytes, chunk_id: str) -> str:
    """`chunks/{n[:2]}/{n}` with n = HMAC-SHA256(K_name, chunk_id): the plain id
    is a hash of the plaintext and would let a provider test for known files."""
    n = hmac.new(name_key, chunk_id.encode(), hashlib.sha256).hexdigest()
    return f"chunks/{n[:2]}/{n}"


def _challenge_aad(key_id: UUID) -> bytes:
    return _CHALLENGE_AAD + str(key_id).encode()


def _issue_challenge(row: BackupRecoveryKey, now: datetime) -> str:
    secret = os.urandom(32)
    row.challenge_sha256 = hashlib.sha256(secret).hexdigest()
    row.challenge_expires_at = now + CHALLENGE_TTL
    blob = {
        "kind": "itms-recovery-challenge",
        "key_id": str(row.id),
        **wrap(parse_public_key(row.public_key), secret, _challenge_aad(row.id)),
    }
    return base64.b64encode(json.dumps(blob, sort_keys=True).encode()).decode()


#: The active-key key (migration 0231, Task 8b): unique per client, the global
#: keys among themselves.
ACTIVE_KEY = "uq_backup_recovery_keys_client_active"


async def _active_key_exists(
    session: AsyncSession, org_id: UUID, client_id: UUID | None, fp: str
) -> bool:
    """Is a key of this fingerprint active in `client_id`'s namespace (None:
    the global keys')? As `ACTIVE_KEY` is."""
    found = (
        await session.execute(
            select(BackupRecoveryKey.id).where(
                BackupRecoveryKey.org_id == org_id,
                BackupRecoveryKey.client_id.is_not_distinct_from(client_id),
                BackupRecoveryKey.fingerprint == fp,
                BackupRecoveryKey.retired_at.is_(None),
            )
        )
    ).first()
    return found is not None


def _already_registered() -> RecoveryKeyError:
    return RecoveryKeyError(tr("This recovery key is already registered."))


async def register_recovery_key(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, public_key_text: str, now: datetime
) -> tuple[BackupRecoveryKey, str]:
    """Store a public key and return it with a challenge to prove possession.

    The key is GLOBAL (phase 1 writes no client): a client's key of the same
    fingerprint is another row and does not refuse it."""
    raw = parse_public_key(public_key_text)
    fp = fingerprint(raw)
    if await _active_key_exists(session, org_id, None, fp):
        raise _already_registered()
    row = BackupRecoveryKey(
        org_id=org_id,
        public_key=base64.b64encode(raw).decode(),
        fingerprint=fp,
        created_by=actor_id,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Two registrations raced past the check: the key's own refusal.
        if violated_constraint(exc) == ACTIVE_KEY:
            raise _already_registered() from None
        raise
    challenge = _issue_challenge(row, now)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="backup.recovery_key_registered",
        target_type="backup_recovery_key",
        target_id=str(row.id),
        metadata={"fingerprint": fp},
    )
    await session.flush()
    return row, challenge


async def get_recovery_key(
    session: AsyncSession, org_id: UUID, key_id: UUID
) -> BackupRecoveryKey | None:
    return (
        await session.execute(
            select(BackupRecoveryKey).where(
                BackupRecoveryKey.org_id == org_id, BackupRecoveryKey.id == key_id
            )
        )
    ).scalar_one_or_none()


async def reissue_challenge(
    session: AsyncSession, row: BackupRecoveryKey, *, now: datetime
) -> str:
    if row.retired_at is not None:
        raise RecoveryKeyError(tr("This recovery key has been retired."))
    challenge = _issue_challenge(row, now)
    await session.flush()
    return challenge


async def prove_recovery_key(
    session: AsyncSession,
    row: BackupRecoveryKey,
    *,
    answer: str,
    actor_id: UUID,
    now: datetime,
) -> None:
    """Accept the decrypted challenge. Raises `RecoveryKeyError` otherwise."""
    if row.retired_at is not None:
        raise RecoveryKeyError(tr("This recovery key has been retired."))
    if (
        row.challenge_sha256 is None
        or row.challenge_expires_at is None
        or row.challenge_expires_at <= now
    ):
        raise RecoveryKeyError(tr("The check has expired. Ask for a new one and try again."))
    try:
        secret = base64.b64decode(answer.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RecoveryKeyError(
            tr("That answer does not match. Check you used the right key.")
        ) from exc
    if not hmac.compare_digest(hashlib.sha256(secret).hexdigest(), row.challenge_sha256):
        await audit_service.record(
            session,
            org_id=row.org_id,
            actor_type="user",
            actor_id=actor_id,
            action="backup.recovery_key_proof_failed",
            target_type="backup_recovery_key",
            target_id=str(row.id),
            metadata={"fingerprint": row.fingerprint},
        )
        await session.flush()
        raise RecoveryKeyError(tr("That answer does not match. Check you used the right key."))
    row.proven_at = now
    row.challenge_sha256 = None
    row.challenge_expires_at = None
    await audit_service.record(
        session,
        org_id=row.org_id,
        actor_type="user",
        actor_id=actor_id,
        action="backup.recovery_key_proven",
        target_type="backup_recovery_key",
        target_id=str(row.id),
        metadata={"fingerprint": row.fingerprint},
    )
    await session.flush()


async def proven_recovery_keys(
    session: AsyncSession, org_id: UUID, *, client_id: UUID
) -> list[BackupRecoveryKey]:
    """The proven, unretired keys a repository of `client_id` may be wrapped
    to: the global ones and that client's own (MSP clients plan 2026-10-26
    Task 13). Another client's key would let its holders open this client's
    off-site copy."""
    return list(
        (
            await session.execute(
                select(BackupRecoveryKey)
                .where(
                    BackupRecoveryKey.org_id == org_id,
                    or_(
                        BackupRecoveryKey.client_id.is_(None),
                        BackupRecoveryKey.client_id == client_id,
                    ),
                    BackupRecoveryKey.proven_at.is_not(None),
                    BackupRecoveryKey.retired_at.is_(None),
                )
                .order_by(BackupRecoveryKey.created_at)
            )
        )
        .scalars()
        .all()
    )


async def list_recovery_keys(session: AsyncSession, org_id: UUID) -> list[BackupRecoveryKey]:
    return list(
        (
            await session.execute(
                select(BackupRecoveryKey)
                .where(BackupRecoveryKey.org_id == org_id)
                .order_by(BackupRecoveryKey.created_at)
            )
        )
        .scalars()
        .all()
    )
