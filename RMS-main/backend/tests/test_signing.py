from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.core.signing import (
    LocalEd25519Signer,
    MissingSigningKeyError,
    OpenBaoTransitSigner,
    get_signer,
    verify_signature,
)


async def test_local_sign_verify_roundtrip(tmp_path: Path) -> None:
    signer = LocalEd25519Signer(tmp_path / "job-signing.key")
    payload = b'{"a":1}'
    sig = await signer.sign(payload)
    pub = await signer.public_key_pem()
    assert verify_signature(pub, payload, sig)


async def test_tampered_payload_fails(tmp_path: Path) -> None:
    signer = LocalEd25519Signer(tmp_path / "k.key")
    sig = await signer.sign(b'{"amount":10}')
    pub = await signer.public_key_pem()
    assert not verify_signature(pub, b'{"amount":99}', sig)


async def test_wrong_key_fails(tmp_path: Path) -> None:
    signer_a = LocalEd25519Signer(tmp_path / "a.key")
    signer_b = LocalEd25519Signer(tmp_path / "b.key")
    sig = await signer_a.sign(b"payload")
    assert not verify_signature(await signer_b.public_key_pem(), b"payload", sig)


async def test_key_persisted_and_reloaded(tmp_path: Path) -> None:
    path = tmp_path / "k.key"
    first = LocalEd25519Signer(path)
    pub1 = await first.public_key_pem()
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    second = LocalEd25519Signer(path)
    assert await second.public_key_pem() == pub1
    assert verify_signature(pub1, b"x", await second.sign(b"x"))


async def test_openbao_transit_signer_parses_vault_response(tmp_path: Path) -> None:
    # OpenBao returns base64(raw sig) behind a "vault:v1:" prefix; the public
    # key endpoint returns base64 raw 32-byte ed25519 key material.
    reference = LocalEd25519Signer(tmp_path / "ref.key")
    payload = b"job-payload"
    real_sig = await reference.sign(payload)
    raw_pub = (await reference.public_key_pem()).strip()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transit/sign/job-signing"):
            return httpx.Response(200, json={"data": {"signature": f"vault:v1:{real_sig}"}})
        if request.url.path.endswith("/transit/keys/job-signing"):
            import base64

            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                PublicFormat,
                load_pem_public_key,
            )

            key = load_pem_public_key(raw_pub.encode())
            raw = key.public_bytes(Encoding.Raw, PublicFormat.Raw)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "latest_version": 1,
                        "keys": {"1": {"public_key": base64.b64encode(raw).decode()}},
                    }
                },
            )
        return httpx.Response(404)

    signer = OpenBaoTransitSigner(
        "https://bao.test",
        "token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    sig = await signer.sign(payload)
    pub = await signer.public_key_pem()
    assert verify_signature(pub, payload, sig)


async def test_production_fails_closed_on_missing_signing_key(tmp_path: Path) -> None:
    # Deferred finding C: production must not silently mint a fresh signing key
    # (that would rotate the fleet-wide identity and reject every job).
    with pytest.raises(MissingSigningKeyError):
        LocalEd25519Signer(tmp_path / "absent.key", allow_generate=False)


async def test_production_accepts_existing_signing_key(tmp_path: Path) -> None:
    path = tmp_path / "k.key"
    LocalEd25519Signer(path)  # dev run provisions it
    reloaded = LocalEd25519Signer(path, allow_generate=False)  # prod reuses it
    assert await reloaded.public_key_pem()


def test_factory_fails_closed_in_production(tmp_path: Path) -> None:
    base = dict(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=str(tmp_path),
    )
    prod = Settings(
        **base, environment="production", signing_key_path=tmp_path / "absent.key"
    )
    with pytest.raises(MissingSigningKeyError):
        get_signer(prod)
    # dev happily generates
    dev = Settings(**base, environment="dev", signing_key_path=tmp_path / "dev.key")
    assert isinstance(get_signer(dev), LocalEd25519Signer)


def test_factory_selects_backend(tmp_path: Path) -> None:
    base = dict(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=str(tmp_path),
    )
    local = Settings(**base, signing_backend="local", signing_key_path=tmp_path / "k.key")
    assert isinstance(get_signer(local), LocalEd25519Signer)
    bao = Settings(
        **base, signing_backend="openbao", openbao_addr="https://bao.test", openbao_token="t"
    )
    assert isinstance(get_signer(bao), OpenBaoTransitSigner)
    with pytest.raises(ValueError):
        get_signer(Settings(**base, signing_backend="openbao"))  # missing addr/token
