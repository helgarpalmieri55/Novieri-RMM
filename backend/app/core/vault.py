"""Credential vault: envelope encryption (docs/kernel/vault.md).

Two layers so the application database never holds a key that can decrypt on
its own: each secret is AES-256-GCM encrypted under a per-org data key (DEK);
the DEK is wrapped by the vault root (OpenBao transit in production, a local
root key file in dev/test). The plaintext DEK exists only transiently in
server memory during store/checkout. There is no plaintext-at-rest path in
either backend.
"""

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.health.types import ReasonCode
from app.core.i18n import tr
from app.models import OrgVaultKey

DEK_BYTES = 32  # AES-256
NONCE_BYTES = 12


@dataclass(frozen=True)
class StoredSecret:
    ciphertext_b64: str  # nonce || ciphertext, base64
    wrapped_dek: str
    key_version: int


def _aad(org_id: UUID, credential_id: UUID) -> bytes:
    return f"{org_id}|{credential_id}".encode()


def _encrypt_secret(dek: bytes, plaintext: str, aad: bytes) -> str:
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode(), aad)
    return base64.b64encode(nonce + ct).decode()


def _decrypt_secret(dek: bytes, ciphertext_b64: str, aad: bytes) -> str:
    blob = base64.b64decode(ciphertext_b64)
    nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    return AESGCM(dek).decrypt(nonce, ct, aad).decode()


def _encrypt_bytes_with_dek(dek: bytes, plaintext: bytes, aad: bytes) -> str:
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(dek).encrypt(nonce, plaintext, aad)
    return base64.b64encode(nonce + ct).decode()


def _decrypt_bytes_with_dek(dek: bytes, ciphertext_b64: str, aad: bytes) -> bytes:
    blob = base64.b64decode(ciphertext_b64)
    nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    return AESGCM(dek).decrypt(nonce, ct, aad)


class VaultService(Protocol):
    async def store(
        self, session: AsyncSession, *, org_id: UUID, credential_id: UUID, plaintext: str
    ) -> StoredSecret: ...

    async def checkout(
        self, session: AsyncSession, *, org_id: UUID, credential_id: UUID
    ) -> str: ...

    async def delete(
        self, session: AsyncSession, *, org_id: UUID, credential_id: UUID
    ) -> None: ...

    async def seal_bytes(
        self, session: AsyncSession, *, org_id: UUID, key_id: UUID, plaintext: bytes
    ) -> str: ...

    async def open_bytes(
        self, session: AsyncSession, *, org_id: UUID, key_id: UUID, sealed: str
    ) -> bytes: ...

    async def health(self) -> tuple[bool, "ReasonCode | None"]:
        """Is the vault usable, without moving any secret material?

        Every other method on this Protocol -- store, checkout, delete,
        seal_bytes, open_bytes -- moves a credential. A health probe must call
        none of them. This exists so the probe has something safe to call.
        """
        ...


class _EnvelopeVault:
    """Shared envelope logic; subclasses provide DEK wrap/unwrap against a root."""

    async def _wrap_dek(self, org_id: UUID, dek: bytes) -> tuple[str, int]:  # pragma: no cover
        raise NotImplementedError

    async def _unwrap_dek(self, org_id: UUID, wrapped_dek: str) -> bytes:  # pragma: no cover
        raise NotImplementedError

    async def _org_dek(self, session: AsyncSession, org_id: UUID) -> tuple[bytes, str, int]:
        """Return (dek, wrapped_dek, key_version), creating the org's key on first use.

        `org_id` is `OrgVaultKey`'s primary key, so at most one row can ever
        exist per org. Two concurrent first-ever calls for the same org (e.g.
        two `store()` calls racing to create an org's very first credential)
        must not both attempt a plain insert: naively, the loser blocks on
        the PK's row lock until the winner commits, then raises
        `IntegrityError` -- a 500 on the credential-storage path -- AFTER it
        has already generated and wrapped its own DEK.

        The blast radius of that race, if a caller were to swallow the error
        and press on with its own locally-generated DEK anyway, would be a
        secret encrypted under a DEK the org's canonical `org_vault_keys` row
        does not reflect. Today nothing re-derives a DEK from `OrgVaultKey`
        to decrypt any *particular* secret -- `checkout()` unwraps
        `Credential.wrapped_dek` directly and `open_bytes()` unwraps the
        `wrapped_dek` embedded in its own sealed payload, both self-contained
        -- so the pre-existing bug was "only" an availability 500, not a
        silently-unrecoverable credential. The fix below must not regress
        that property: it is `ON CONFLICT DO NOTHING` + unconditional
        re-select, so EVERY caller -- winner or loser -- returns the
        (dek, wrapped_dek, key_version) unwrapped from the row that actually
        landed in `org_vault_keys`, never a dek/wrapped pair that was
        generated locally but discarded. A caller can never receive a DEK
        that differs from the one persisted for this org.
        """
        row = (
            await session.execute(select(OrgVaultKey).where(OrgVaultKey.org_id == org_id))
        ).scalar_one_or_none()
        if row is not None:
            return await self._unwrap_dek(org_id, row.wrapped_dek), row.wrapped_dek, row.key_version
        dek = os.urandom(DEK_BYTES)
        wrapped, version = await self._wrap_dek(org_id, dek)
        await session.execute(
            pg_insert(OrgVaultKey)
            .values(org_id=org_id, wrapped_dek=wrapped, key_version=version)
            .on_conflict_do_nothing(index_elements=["org_id"])
        )
        await session.flush()
        # Re-select unconditionally (whether we won or lost the race) and
        # unwrap THAT row -- never the dek we generated locally above, which
        # the canonical row may not reflect if we lost.
        row = (
            await session.execute(select(OrgVaultKey).where(OrgVaultKey.org_id == org_id))
        ).scalar_one()
        return await self._unwrap_dek(org_id, row.wrapped_dek), row.wrapped_dek, row.key_version

    async def store(
        self, session: AsyncSession, *, org_id: UUID, credential_id: UUID, plaintext: str
    ) -> StoredSecret:
        dek, wrapped, version = await self._org_dek(session, org_id)
        ciphertext = _encrypt_secret(dek, plaintext, _aad(org_id, credential_id))
        return StoredSecret(ciphertext_b64=ciphertext, wrapped_dek=wrapped, key_version=version)

    async def checkout(
        self, session: AsyncSession, *, org_id: UUID, credential_id: UUID
    ) -> str:
        from app.models import Credential

        # Explicit org_id predicate (defense in depth) so a future caller on a
        # BYPASSRLS/admin session still cannot decrypt across tenants; the AAD
        # binding would already fail such a fetch, but never rely on one layer.
        credential = (
            await session.execute(
                select(Credential).where(
                    Credential.id == credential_id, Credential.org_id == org_id
                )
            )
        ).scalar_one_or_none()
        if credential is None or credential.ciphertext is None or credential.wrapped_dek is None:
            raise ValueError(tr("no secret stored for this credential"))
        dek = await self._unwrap_dek(org_id, credential.wrapped_dek)
        return _decrypt_secret(dek, credential.ciphertext, _aad(org_id, credential_id))

    async def delete(
        self, session: AsyncSession, *, org_id: UUID, credential_id: UUID
    ) -> None:
        # ciphertext lives on the credential row; the caller deletes that row.
        return None

    async def seal_bytes(
        self, session: AsyncSession, *, org_id: UUID, key_id: UUID, plaintext: bytes
    ) -> str:
        """Envelope-encrypt raw bytes under the org DEK into a single
        self-contained sealed string (no separate DB columns needed — callers
        like the backup repository store this whole string in one column)."""
        dek, wrapped, version = await self._org_dek(session, org_id)
        ct_b64 = _encrypt_bytes_with_dek(dek, plaintext, _aad(org_id, key_id))
        payload: dict[str, str | int] = {
            "wrapped_dek": wrapped,
            "key_version": version,
            "ct_b64": ct_b64,
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    async def open_bytes(
        self, session: AsyncSession, *, org_id: UUID, key_id: UUID, sealed: str
    ) -> bytes:
        payload = json.loads(base64.b64decode(sealed).decode())
        wrapped_dek: str = payload["wrapped_dek"]
        ct_b64: str = payload["ct_b64"]
        dek = await self._unwrap_dek(org_id, wrapped_dek)
        return _decrypt_bytes_with_dek(dek, ct_b64, _aad(org_id, key_id))


class LocalVaultService(_EnvelopeVault):
    """Dev/test root: a local AES-256 key file wraps DEKs. Same envelope
    semantics as OpenBao — no plaintext-at-rest path."""

    def __init__(self, root_key_path: Path) -> None:
        path = Path(root_key_path)
        if path.exists():
            self._root = path.read_bytes()
        else:
            self._root = os.urandom(DEK_BYTES)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600)
            path.write_bytes(self._root)
            path.chmod(0o600)

    async def _wrap_dek(self, org_id: UUID, dek: bytes) -> tuple[str, int]:
        nonce = os.urandom(NONCE_BYTES)
        wrapped = AESGCM(self._root).encrypt(nonce, dek, str(org_id).encode())
        return base64.b64encode(nonce + wrapped).decode(), 1

    async def _unwrap_dek(self, org_id: UUID, wrapped_dek: str) -> bytes:
        blob = base64.b64decode(wrapped_dek)
        nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        return AESGCM(self._root).decrypt(nonce, ct, str(org_id).encode())

    async def health(self) -> tuple[bool, ReasonCode | None]:
        """In-memory: usable whenever the process is."""
        return True, None


class OpenBaoVaultService(_EnvelopeVault):
    """Production root: OpenBao transit wraps the per-org DEK (root key never
    leaves the vault). The secret layer is AES-256-GCM as above."""

    def __init__(self, addr: str, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._addr = addr.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=10)
        self._headers = {"X-Vault-Token": token}

    def _key_name(self, org_id: UUID) -> str:
        return f"org-{org_id}"

    async def _ensure_key(self, org_id: UUID) -> None:
        # Idempotent create; existing key returns 204/400 which we ignore.
        await self._client.post(
            f"{self._addr}/v1/transit/keys/{self._key_name(org_id)}", headers=self._headers
        )

    async def _wrap_dek(self, org_id: UUID, dek: bytes) -> tuple[str, int]:
        await self._ensure_key(org_id)
        response = await self._client.post(
            f"{self._addr}/v1/transit/encrypt/{self._key_name(org_id)}",
            headers=self._headers,
            json={"plaintext": base64.b64encode(dek).decode()},
        )
        response.raise_for_status()
        wrapped: str = response.json()["data"]["ciphertext"]
        version = int(wrapped.split(":")[1].lstrip("v")) if ":" in wrapped else 1
        return wrapped, version

    async def _unwrap_dek(self, org_id: UUID, wrapped_dek: str) -> bytes:
        response = await self._client.post(
            f"{self._addr}/v1/transit/decrypt/{self._key_name(org_id)}",
            headers=self._headers,
            json={"ciphertext": wrapped_dek},
        )
        response.raise_for_status()
        return base64.b64decode(response.json()["data"]["plaintext"])

    async def health(self) -> tuple[bool, ReasonCode | None]:
        """`GET /v1/sys/health`: initialised and seal state, no secret material.

        A SEALED vault reports (False, SEALED) rather than raising -- it is a
        normal operational state with a specific remedy (unseal), and an
        operator needs to see that rather than `unreachable`.
        """
        try:
            response = await self._client.get(f"{self._addr}/v1/sys/health")
        except Exception:
            return False, ReasonCode.UNREACHABLE
        if response.status_code == 503:
            return False, ReasonCode.SEALED
        if response.status_code == 501:
            return False, ReasonCode.MISCONFIGURED  # not initialised
        return response.status_code == 200, None


def get_vault(settings: Settings) -> VaultService:
    if settings.vault_backend == "local":
        path = settings.vault_root_key_path or settings.ca_dir / "vault-root.key"
        return LocalVaultService(path)
    if not settings.openbao_addr or not settings.openbao_token:
        raise ValueError(  # i18n-exempt: startup config error, logged
            "openbao vault backend requires ITS_OPENBAO_ADDR and ITS_OPENBAO_TOKEN"
        )
    return OpenBaoVaultService(settings.openbao_addr, settings.openbao_token)
