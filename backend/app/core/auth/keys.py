"""The key material of the platform's own sign-in: the password pepper and the
token signing key (design `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md`
§2.1, §6.3; plan decisions D6 and D7).

THE PEPPER NEVER ENTERS THIS PROCESS. It is the OpenBao transit key
`auth-pepper` (type `hmac`, not exportable): the platform sends the bytes to
MAC and receives the MAC, `vault:v<N>:<base64>`. N is the key version, stored
next to each password hash so a rotated pepper still verifies old ones
(`transit/hmac` accepts `key_version`). NIST SP 800-63B-4 §3.1.1.2 asks for
exactly this: "an additional iteration of a keyed hashing ... using a secret
key known only to the verifier ... stored separately from the hashed
passwords". Every MAC is bound to its organization: the input is
`org_id.bytes` (16 bytes, fixed length) followed by the caller's bytes, so a
verifier copied into another organization's row does not verify there.

THE SIGNING KEY NEVER ENTERS THIS PROCESS EITHER. It is the transit key
`auth-signing` (type `ecdsa-p256`): `transit/sign` with
`marshaling_algorithm=jws` returns the JWS form of the signature, R||S in
base64url (OpenBao `sdk/helper/keysutil/policy.go`, `MarshalingTypeJWS`), which
is what ES256 (RFC 7518 §3.4) puts in a JWT. The public halves are read from
`transit/keys/auth-signing` (PEM per version) and published at
`/.well-known/jwks.json`: the current version and the one before, so a token
signed just before a rotation still verifies for its short life (§6.3). A
`kid` is the key's RFC 7638 thumbprint, so it names the key itself and not a
version number two deployments could share.

Verified against OpenBao 2.6.1, the version `deploy/docker-compose.yml` runs:
`builtin/logical/transit/path_hmac.go` (`algorithm`, `key_version`, the
`vault:v<N>:` prefix), `path_sign_verify.go` (`hash_algorithm`,
`marshaling_algorithm`, `key_version` in the response) and `path_keys.go`
(`keys[<N>].public_key`, a PEM for ECDSA).

THE LOCAL BACKEND has the same interface for development and tests: two files
in `ITS_AUTH_KEYS_DIR` (default `ITS_CA_DIR`), `auth-pepper.key` (32 random
bytes) and `auth-signing.key` (a P-256 key, PKCS#8 PEM), both 0600, each
version 1. In production it refuses to create them -- a fresh pepper would make
every stored password unverifiable -- and `ITS_AUTH_KEYS_BACKEND=openbao` is
the deployment's setting (`deploy/.env.example`).

`configure(settings)` installs the backend once, in `app.main.create_app`, the
`audit_service.set_hmac_key` pattern. The three functions the rest of the
platform calls are `pepper_hmac`, `sign_es256` and `jwks`.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
    load_pem_public_key,
)

from app.core.config import Settings

#: The transit key names (`deploy/openbao/init.sh`, `deploy/openbao/its-app.hcl`).
PEPPER_KEY = "auth-pepper"
SIGNING_KEY = "auth-signing"
#: How long the public keys read from OpenBao are reused. A signature with a
#: version not in the cache reads them again at once.
PUBLIC_KEYS_TTL_S = 300.0
#: The local backend's files.
PEPPER_FILE = "auth-pepper.key"
SIGNING_FILE = "auth-signing.key"
_PEPPER_BYTES = 32
_P256_BYTES = 32


class MissingAuthKeyError(RuntimeError):
    """A local key file is absent where it may not be created (production)."""


class AuthKeysUnavailable(RuntimeError):
    """OpenBao did not answer, or answered something this module cannot read.
    The caller answers 503; nothing is ever done without the key."""


class AuthKeysNotConfigured(RuntimeError):
    """`configure` was never called in this process."""


class UnknownKeyVersion(LookupError):
    """The local backend holds no key of the version asked for."""


class AuthKeys(Protocol):
    async def hmac(self, data: bytes, *, version: int | None) -> tuple[bytes, int]:
        """HMAC-SHA256 of `data` with the pepper (`version`, or the latest),
        and the version used."""
        ...

    async def sign(self, signing_input: bytes) -> tuple[bytes, int]:
        """ES256 over `signing_input` (SHA-256 inside): R||S, 64 bytes, and the
        signing key's version."""
        ...

    async def public_keys(self, *, refresh: bool = False) -> dict[int, ec.EllipticCurvePublicKey]:
        """Every version's public key."""
        ...


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _p256(key: object) -> ec.EllipticCurvePublicKey:
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise AuthKeysUnavailable(  # i18n-exempt: mapped to a translated 503 by the caller
            "the signing key is not an ECDSA P-256 key"
        )
    return key


def public_jwk(key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    """The key's RFC 7638 required members, in lexicographic order."""
    numbers = key.public_numbers()
    return {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64url(numbers.x.to_bytes(_P256_BYTES, "big")),
        "y": _b64url(numbers.y.to_bytes(_P256_BYTES, "big")),
    }


def thumbprint(key: ec.EllipticCurvePublicKey) -> str:
    """RFC 7638 §3: SHA-256 over the required members, no whitespace, sorted."""
    canonical = json.dumps(public_jwk(key), separators=(",", ":"), sort_keys=True)
    return _b64url(hashlib.sha256(canonical.encode("ascii")).digest())


class LocalAuthKeys:
    """Development and tests: the keys in memory, loaded from (or written to)
    two files. Same semantics as OpenBao: versioned pepper, versioned P-256."""

    def __init__(
        self,
        *,
        pepper: Mapping[int, bytes],
        signing: Mapping[int, ec.EllipticCurvePrivateKey],
    ) -> None:
        if not pepper or not signing:
            raise ValueError(  # i18n-exempt: programming error, never reaches a person
                "LocalAuthKeys needs at least one pepper and one signing key"
            )
        self._pepper = dict(pepper)
        self._signing = dict(signing)

    @classmethod
    def generate(cls) -> "LocalAuthKeys":
        """Fresh keys in memory, version 1 each (tests)."""
        return cls(
            pepper={1: os.urandom(_PEPPER_BYTES)},
            signing={1: ec.generate_private_key(ec.SECP256R1())},
        )

    @classmethod
    def from_dir(cls, directory: Path, *, allow_generate: bool) -> "LocalAuthKeys":
        pepper_path = Path(directory) / PEPPER_FILE
        signing_path = Path(directory) / SIGNING_FILE
        missing = [p for p in (pepper_path, signing_path) if not p.exists()]
        if missing and not allow_generate:
            raise MissingAuthKeyError(  # i18n-exempt: startup config error, logged
                f"authentication key {missing[0]} not found and auto-generation is disabled "
                "in production; set ITS_AUTH_KEYS_BACKEND=openbao"
            )
        if not pepper_path.exists():
            _write_private(pepper_path, os.urandom(_PEPPER_BYTES))
        if not signing_path.exists():
            private = ec.generate_private_key(ec.SECP256R1())
            _write_private(
                signing_path,
                private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()),
            )
        pepper = pepper_path.read_bytes()
        if len(pepper) != _PEPPER_BYTES:
            raise MissingAuthKeyError(  # i18n-exempt: startup config error, logged
                f"{pepper_path} does not hold {_PEPPER_BYTES} bytes"
            )
        loaded = load_pem_private_key(signing_path.read_bytes(), password=None)
        if not isinstance(loaded, ec.EllipticCurvePrivateKey) or not isinstance(
            loaded.curve, ec.SECP256R1
        ):
            raise MissingAuthKeyError(  # i18n-exempt: startup config error, logged
                f"{signing_path} is not an ECDSA P-256 private key"
            )
        return cls(pepper={1: pepper}, signing={1: loaded})

    async def hmac(self, data: bytes, *, version: int | None) -> tuple[bytes, int]:
        used = version if version is not None else max(self._pepper)
        key = self._pepper.get(used)
        if key is None:
            raise UnknownKeyVersion(used)  # i18n-exempt: caller maps it, never shown
        return hmac.new(key, data, hashlib.sha256).digest(), used

    async def sign(self, signing_input: bytes) -> tuple[bytes, int]:
        version = max(self._signing)
        der = self._signing[version].sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return r.to_bytes(_P256_BYTES, "big") + s.to_bytes(_P256_BYTES, "big"), version

    async def public_keys(self, *, refresh: bool = False) -> dict[int, ec.EllipticCurvePublicKey]:
        return {version: key.public_key() for version, key in self._signing.items()}


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.write_bytes(data)
    path.chmod(0o600)


def _versioned(value: str) -> tuple[int, str]:
    """`vault:v<N>:<body>` -> (N, body)."""
    prefix, version, body = value.split(":", 2)
    if prefix != "vault" or not version.startswith("v"):
        raise ValueError(  # i18n-exempt: caught below, becomes AuthKeysUnavailable
            "not a transit value"
        )
    return int(version[1:]), body


class OpenBaoAuthKeys:
    """Production: transit `auth-pepper` (hmac) and `auth-signing` (ecdsa-p256)."""

    def __init__(
        self,
        addr: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=10)
        self._headers = {"X-Vault-Token": token}
        self._clock = clock
        self._keys: dict[int, ec.EllipticCurvePublicKey] = {}
        self._read_at: float | None = None
        self._lock = asyncio.Lock()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self._addr}/v1/{path}", headers=self._headers, json=body
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()["data"]
            if not isinstance(data, dict):
                raise TypeError(  # i18n-exempt: caught below, becomes AuthKeysUnavailable
                    "transit answered a data member that is not an object"
                )
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise AuthKeysUnavailable(  # i18n-exempt: mapped to a translated 503 by the caller
                f"OpenBao refused {path}"
            ) from exc
        return data

    async def hmac(self, data: bytes, *, version: int | None) -> tuple[bytes, int]:
        body: dict[str, Any] = {
            "input": base64.b64encode(data).decode("ascii"),
            "algorithm": "sha2-256",
        }
        if version is not None:
            body["key_version"] = version
        answer = await self._post(f"transit/hmac/{PEPPER_KEY}", body)
        try:
            used, mac = _versioned(str(answer["hmac"]))
            return base64.b64decode(mac), used
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise AuthKeysUnavailable(  # i18n-exempt: mapped to a translated 503 by the caller
                "OpenBao answered an HMAC this module cannot read"
            ) from exc

    async def sign(self, signing_input: bytes) -> tuple[bytes, int]:
        answer = await self._post(
            f"transit/sign/{SIGNING_KEY}",
            {
                "input": base64.b64encode(signing_input).decode("ascii"),
                "hash_algorithm": "sha2-256",
                "marshaling_algorithm": "jws",
            },
        )
        try:
            prefix_version, encoded = _versioned(str(answer["signature"]))
            version = int(answer.get("key_version", prefix_version))
            signature = _b64url_decode(encoded)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise AuthKeysUnavailable(  # i18n-exempt: mapped to a translated 503 by the caller
                "OpenBao answered a signature this module cannot read"
            ) from exc
        if len(signature) != 2 * _P256_BYTES:
            raise AuthKeysUnavailable(  # i18n-exempt: mapped to a translated 503 by the caller
                "OpenBao answered a signature that is not ES256"
            )
        return signature, version

    async def public_keys(self, *, refresh: bool = False) -> dict[int, ec.EllipticCurvePublicKey]:
        async with self._lock:
            fresh = (
                self._read_at is not None and self._clock() - self._read_at < PUBLIC_KEYS_TTL_S
            )
            if fresh and not refresh:
                return dict(self._keys)
            try:
                response = await self._client.get(
                    f"{self._addr}/v1/transit/keys/{SIGNING_KEY}", headers=self._headers
                )
                response.raise_for_status()
                entries = response.json()["data"]["keys"]
                keys = {
                    int(version): _p256(load_pem_public_key(entry["public_key"].encode()))
                    for version, entry in entries.items()
                }
            except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError) as exc:
                raise AuthKeysUnavailable(  # i18n-exempt: mapped to a translated 503 by the caller
                    "OpenBao did not answer the signing key's public halves"
                ) from exc
            self._keys, self._read_at = keys, self._clock()
            return dict(keys)


def build(settings: Settings) -> AuthKeys:
    """The backend `settings` asks for (the `get_vault` / `get_signer` shape)."""
    if settings.auth_keys_backend == "local":
        return LocalAuthKeys.from_dir(
            settings.auth_keys_dir or settings.ca_dir,
            allow_generate=settings.environment != "production",
        )
    if not settings.openbao_addr or not settings.openbao_token:
        raise ValueError(  # i18n-exempt: startup config error, logged
            "openbao auth keys backend requires ITS_OPENBAO_ADDR and ITS_OPENBAO_TOKEN"
        )
    return OpenBaoAuthKeys(settings.openbao_addr, settings.openbao_token)


_backend: AuthKeys | None = None


def configure(settings: Settings) -> None:
    """Install the process's backend (`app.main.create_app`)."""
    set_backend(build(settings))


def set_backend(backend: AuthKeys | None) -> None:
    """Install `backend` directly (tests), or clear it."""
    global _backend
    _backend = backend


def _current() -> AuthKeys:
    if _backend is None:
        raise AuthKeysNotConfigured(  # i18n-exempt: programming error, logged
            "app.core.auth.keys.configure() was not called"
        )
    return _backend


async def pepper_hmac(
    org_id: UUID, data: bytes, *, version: int | None = None
) -> tuple[bytes, int]:
    """HMAC-SHA256 of `data` with the pepper, bound to `org_id`, and the pepper
    version used: the latest, or `version` to verify something stored under it."""
    return await _current().hmac(org_id.bytes + data, version=version)


async def sign_es256(signing_input: bytes) -> tuple[bytes, str]:
    """ES256 over a JWS signing input (`base64url(header) "." base64url(payload)`):
    the 64-byte R||S signature and the `kid` of the key that made it."""
    backend = _current()
    signature, version = await backend.sign(signing_input)
    keys = await backend.public_keys()
    if version not in keys:
        keys = await backend.public_keys(refresh=True)
    if version not in keys:
        raise AuthKeysUnavailable(  # i18n-exempt: mapped to a translated 503 by the caller
            "the signing key's version has no public half"
        )
    return signature, thumbprint(keys[version])


def _segment(value: Mapping[str, Any]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


async def sign_jwt(claims: Mapping[str, Any], *, typ: str = "JWT") -> str:
    """A compact JWS (RFC 7515 §7.1) of `claims`, ES256, with the signing key's
    `kid` and `typ` in its header. `typ` is RFC 8725 §3.11 explicit typing: the
    assistant's internal token (Task 9) is `loopback+jwt`, so it cannot be taken
    for any other token this key signs. The `kid` goes into the signed header
    BEFORE the signature exists, so a key rotated in between (OpenBao signs with
    its latest version; this process's copy of the public keys may be older) is
    caught by comparing it with the one `sign_es256` reports -- which has read
    the keys again on meeting the new version -- and the token is made once
    more."""
    backend = _current()
    for _attempt in range(2):
        keys = await backend.public_keys()
        if not keys:
            raise AuthKeysUnavailable(  # i18n-exempt: mapped to a translated 503 by the caller
                "the signing key has no public half to name in a token"
            )
        kid = thumbprint(keys[max(keys)])
        signing_input = f"{_segment({'alg': 'ES256', 'kid': kid, 'typ': typ})}.{_segment(claims)}"
        signature, used = await sign_es256(signing_input.encode("ascii"))
        if used == kid:
            return f"{signing_input}.{_b64url(signature)}"
    raise AuthKeysUnavailable(  # i18n-exempt: mapped to a translated 503 by the caller
        "the signing key changed while a token was being signed"
    )


async def jwks(*, refresh: bool = False) -> dict[str, Any]:
    """The JWK Set: the current signing key and the one before, newest first.

    `refresh` reads the public keys again rather than from this process's copy
    (up to `PUBLIC_KEYS_TTL_S` old): what a verifier does, once, on meeting a
    `kid` that copy lacks -- a token signed just after a rotation, by another
    process (`app.core.auth.loopback`)."""
    keys = await _current().public_keys(refresh=refresh)
    newest = sorted(keys, reverse=True)[:2]
    return {
        "keys": [
            {**public_jwk(keys[v]), "kid": thumbprint(keys[v]), "alg": "ES256", "use": "sig"}
            for v in newest
        ]
    }
