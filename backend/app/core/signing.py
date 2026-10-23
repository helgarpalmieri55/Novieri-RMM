"""Job signing (docs/03-security-model.md, the signed-job invariant).

Every job payload is signed with Ed25519 before dispatch. Production uses an
OpenBao transit key — the private key never leaves the vault. Development and
tests use a local key file with identical semantics. There is deliberately no
"noop" signer and no code path that skips signing.
"""

import base64
from pathlib import Path
from typing import Protocol

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

from app.core.config import Settings


class SigningService(Protocol):
    async def sign(self, payload: bytes) -> str:
        """Sign payload, returning a base64 Ed25519 signature."""
        ...

    async def public_key_pem(self) -> str:
        """The verification key agents pin at enrollment (PEM)."""
        ...


def verify_signature(public_key_pem: str, payload: bytes, signature_b64: str) -> bool:
    """Mirror of the agent-side check; used by tests and the e2e demo agent."""
    try:
        key = load_pem_public_key(public_key_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(base64.b64decode(signature_b64), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


class MissingSigningKeyError(RuntimeError):
    """Raised when the signing key is absent and auto-generation is disallowed
    (production). Silently generating one would rotate the fleet-wide signing
    identity — every already-enrolled agent would reject every job — or let the
    API and gateway processes diverge onto different keys."""


class LocalEd25519Signer:
    def __init__(self, key_path: Path, *, allow_generate: bool = True) -> None:
        self._key_path = Path(key_path)
        self._key: Ed25519PrivateKey
        if self._key_path.exists():
            loaded = load_pem_private_key(self._key_path.read_bytes(), password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise ValueError(  # i18n-exempt: startup config error, logged
                    f"{key_path} is not an Ed25519 private key"
                )
            self._key = loaded
        elif not allow_generate:
            raise MissingSigningKeyError(  # i18n-exempt: startup config error, logged
                f"job-signing key {key_path} not found and auto-generation is "
                "disabled in production; provision the key before starting"
            )
        else:
            self._key = Ed25519PrivateKey.generate()
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            self._key_path.touch(mode=0o600)
            self._key_path.write_bytes(
                self._key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            )
            self._key_path.chmod(0o600)

    async def sign(self, payload: bytes) -> str:
        return base64.b64encode(self._key.sign(payload)).decode("ascii")

    async def public_key_pem(self) -> str:
        return (
            self._key.public_key()
            .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
            .decode("ascii")
        )


class OpenBaoTransitSigner:
    def __init__(
        self,
        addr: str,
        token: str,
        *,
        key_name: str = "job-signing",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._key_name = key_name
        self._client = client or httpx.AsyncClient(timeout=10)
        self._headers = {"X-Vault-Token": token}

    async def sign(self, payload: bytes) -> str:
        response = await self._client.post(
            f"{self._addr}/v1/transit/sign/{self._key_name}",
            headers=self._headers,
            json={"input": base64.b64encode(payload).decode("ascii")},
        )
        response.raise_for_status()
        signature: str = response.json()["data"]["signature"]
        # format: vault:v<key_version>:<base64 raw signature>
        return signature.split(":", 2)[2]

    async def public_key_pem(self) -> str:
        response = await self._client.get(
            f"{self._addr}/v1/transit/keys/{self._key_name}", headers=self._headers
        )
        response.raise_for_status()
        data = response.json()["data"]
        latest = str(data["latest_version"])
        raw = base64.b64decode(data["keys"][latest]["public_key"])
        key = Ed25519PublicKey.from_public_bytes(raw)
        return key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode("ascii")


def get_signer(settings: Settings) -> SigningService:
    if settings.signing_backend == "local":
        key_path = settings.signing_key_path or settings.ca_dir / "job-signing.key"
        return LocalEd25519Signer(
            key_path, allow_generate=settings.environment != "production"
        )
    if not settings.openbao_addr or not settings.openbao_token:
        raise ValueError(  # i18n-exempt: startup config error, logged
            "openbao signing backend requires ITS_OPENBAO_ADDR and ITS_OPENBAO_TOKEN"
        )
    return OpenBaoTransitSigner(settings.openbao_addr, settings.openbao_token)
