"""The sign-in's key material (`app.core.auth.keys`; design §2.1, §6.3; plan
Task 2, decisions D6 and D7).

- the pepper is an HMAC bound to the organization, versioned, and a stored
  version is honoured;
- a token signed here verifies against the published JWK Set with a stock
  JWT library (PyJWT), and the `kid` is the RFC 7638 thumbprint;
- the set holds the current key and the one before, never a private part;
- the OpenBao backend speaks transit as OpenBao 2.6.1 does (the request
  bodies and the `vault:v<N>:` answers), reads the public keys once per TTL
  and again when a signature names a version it has not seen, and turns any
  failure into `AuthKeysUnavailable`;
- the factory picks the backend; production refuses the local one outright,
  files or not (final review A-M2), and the local backend never invents a key
  it was told not to.
"""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core.auth import keys
from app.core.auth.keys import (
    AuthKeysNotConfigured,
    AuthKeysUnavailable,
    LocalAuthKeys,
    LocalAuthKeysInProduction,
    MissingAuthKeyError,
    OpenBaoAuthKeys,
    UnknownKeyVersion,
)
from app.core.config import Settings


@pytest.fixture()
def installed() -> Iterator[Callable[[keys.AuthKeys], None]]:
    """Install a backend for one test, and clear it afterwards."""
    yield keys.set_backend
    keys.set_backend(None)


def _p256() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _rfc7638(public: ec.EllipticCurvePublicKey) -> str:
    """The thumbprint computed from PyJWT's own JWK, not from the module."""
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(public, as_dict=True)
    members = {name: jwk[name] for name in ("crv", "kty", "x", "y")}
    canonical = json.dumps(members, sort_keys=True, separators=(",", ":")).encode()
    return _b64url(hashlib.sha256(canonical).digest())


async def test_the_pepper_is_an_hmac_bound_to_the_organization(installed: Any) -> None:
    pepper = b"\x01" * 32
    installed(LocalAuthKeys(pepper={1: pepper}, signing={1: _p256()}))
    org, other = uuid4(), uuid4()

    mac, version = await keys.pepper_hmac(org, b"secret")

    assert version == 1
    assert mac == hmac.new(pepper, org.bytes + b"secret", hashlib.sha256).digest()
    assert (await keys.pepper_hmac(other, b"secret"))[0] != mac


async def test_the_latest_pepper_is_used_and_a_stored_version_honoured(installed: Any) -> None:
    old, new = b"\x01" * 32, b"\x02" * 32
    installed(LocalAuthKeys(pepper={1: old, 2: new}, signing={1: _p256()}))
    org = uuid4()

    assert (await keys.pepper_hmac(org, b"x"))[1] == 2
    mac, version = await keys.pepper_hmac(org, b"x", version=1)
    assert (mac, version) == (hmac.new(old, org.bytes + b"x", hashlib.sha256).digest(), 1)
    with pytest.raises(UnknownKeyVersion):
        await keys.pepper_hmac(org, b"x", version=3)


async def test_a_token_signed_here_verifies_against_the_published_set(installed: Any) -> None:
    installed(LocalAuthKeys.generate())
    now = int(time.time())

    token = await keys.sign_jwt({"sub": "u-1", "aud": "loopback", "iat": now, "exp": now + 300})

    header = jwt.get_unverified_header(token)
    assert (header["alg"], header["typ"]) == ("ES256", "JWT")
    public = jwt.PyJWKSet.from_dict(await keys.jwks())[header["kid"]].key
    claims = jwt.decode(token, public, algorithms=["ES256"], audience="loopback")
    assert claims["sub"] == "u-1"

    head, payload, signature = token.split(".")
    forged = _b64url(json.dumps({"sub": "u-2", "aud": "loopback", "exp": now + 300}).encode())
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            f"{head}.{forged}.{signature}", public, algorithms=["ES256"], audience="loopback"
        )


async def test_sign_jwt_puts_the_typ_it_is_given_in_the_signed_header(installed: Any) -> None:
    """`typ` is RFC 8725 §3.11 explicit typing (Task 9's `loopback+jwt`): it is
    part of the SIGNED header, so changing it afterwards breaks the signature."""
    installed(LocalAuthKeys.generate())
    now = int(time.time())

    token = await keys.sign_jwt({"sub": "u-1", "exp": now + 300}, typ="loopback+jwt")

    header = jwt.get_unverified_header(token)
    assert (header["alg"], header["typ"]) == ("ES256", "loopback+jwt")
    public = jwt.PyJWKSet.from_dict(await keys.jwks())[header["kid"]].key
    assert jwt.decode(token, public, algorithms=["ES256"])["sub"] == "u-1"
    _, payload, signature = token.split(".")
    retyped = _b64url(json.dumps({**header, "typ": "JWT"}, separators=(",", ":")).encode())
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(f"{retyped}.{payload}.{signature}", public, algorithms=["ES256"])


async def test_sign_es256_answers_r_and_s_and_the_kid(installed: Any) -> None:
    private = _p256()
    installed(LocalAuthKeys(pepper={1: b"\x00" * 32}, signing={1: private}))

    signature, kid = await keys.sign_es256(b"header.payload")

    assert len(signature) == 64
    der = encode_dss_signature(
        int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    )
    private.public_key().verify(der, b"header.payload", ec.ECDSA(hashes.SHA256()))
    assert kid == _rfc7638(private.public_key())


async def test_the_set_holds_the_current_key_and_the_one_before(installed: Any) -> None:
    first, second, third = _p256(), _p256(), _p256()
    installed(LocalAuthKeys(pepper={1: b"\x00" * 32}, signing={1: first, 2: second, 3: third}))

    document = await keys.jwks()

    assert [k["kid"] for k in document["keys"]] == [
        _rfc7638(third.public_key()),
        _rfc7638(second.public_key()),
    ]
    for key in document["keys"]:
        assert set(key) == {"kty", "crv", "x", "y", "kid", "alg", "use"}  # never "d"
        assert (key["kty"], key["crv"], key["alg"], key["use"]) == ("EC", "P-256", "ES256", "sig")


async def test_nothing_is_signed_before_the_process_is_configured(installed: Any) -> None:
    installed(None)
    with pytest.raises(AuthKeysNotConfigured):
        await keys.pepper_hmac(uuid4(), b"x")


class _Transit:
    """A stand-in for OpenBao's transit mount, answering as 2.6.1 does."""

    def __init__(self) -> None:
        self.pepper = {1: b"\x01" * 32, 2: b"\x02" * 32}
        self.signing = {1: _p256(), 2: _p256()}
        self.sign_with = 2
        self.requests: list[httpx.Request] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(503, json={"errors": ["Vault is sealed"]})
        path = request.url.path
        if path == "/v1/transit/hmac/auth-pepper":
            body = json.loads(request.content)
            version = int(body.get("key_version") or max(self.pepper))
            mac = hmac.new(
                self.pepper[version], base64.b64decode(body["input"]), hashlib.sha256
            ).digest()
            return httpx.Response(
                200, json={"data": {"hmac": f"vault:v{version}:{base64.b64encode(mac).decode()}"}}
            )
        if path == "/v1/transit/sign/auth-signing":
            body = json.loads(request.content)
            der = self.signing[self.sign_with].sign(
                base64.b64decode(body["input"]), ec.ECDSA(hashes.SHA256())
            )
            r, s = decode_dss_signature(der)
            jws = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
            return httpx.Response(
                200,
                json={
                    "data": {
                        "signature": f"vault:v{self.sign_with}:{jws}",
                        "key_version": self.sign_with,
                    }
                },
            )
        if path == "/v1/transit/keys/auth-signing":
            versions = {
                str(v): {
                    "name": "P-256",
                    "public_key": k.public_key()
                    .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
                    .decode(),
                }
                for v, k in self.signing.items()
            }
            return httpx.Response(200, json={"data": {"latest_version": 2, "keys": versions}})
        return httpx.Response(404)

    def backend(self, clock: Callable[[], float] = time.monotonic) -> OpenBaoAuthKeys:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return OpenBaoAuthKeys("https://bao.test", "token", client=client, clock=clock)


async def test_openbao_macs_with_the_pepper_and_honours_the_version(installed: Any) -> None:
    transit = _Transit()
    installed(transit.backend())
    org = uuid4()

    mac, version = await keys.pepper_hmac(org, b"secret", version=1)

    assert version == 1
    assert mac == hmac.new(transit.pepper[1], org.bytes + b"secret", hashlib.sha256).digest()
    body = json.loads(transit.requests[-1].content)
    assert body == {
        "input": base64.b64encode(org.bytes + b"secret").decode(),
        "algorithm": "sha2-256",
        "key_version": 1,
    }
    assert transit.requests[-1].headers["X-Vault-Token"] == "token"
    assert (await keys.pepper_hmac(org, b"secret"))[1] == 2
    assert "key_version" not in json.loads(transit.requests[-1].content)


async def test_openbao_signs_in_jws_form_and_names_the_key(installed: Any) -> None:
    transit = _Transit()
    installed(transit.backend())

    signature, kid = await keys.sign_es256(b"header.payload")

    sign = next(r for r in transit.requests if r.url.path.endswith("/transit/sign/auth-signing"))
    assert json.loads(sign.content) == {
        "input": base64.b64encode(b"header.payload").decode(),
        "hash_algorithm": "sha2-256",
        "marshaling_algorithm": "jws",
    }
    public = transit.signing[2].public_key()
    der = encode_dss_signature(
        int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    )
    public.verify(der, b"header.payload", ec.ECDSA(hashes.SHA256()))
    assert kid == _rfc7638(public)


async def test_openbao_public_keys_are_read_once_per_ttl_and_on_a_new_version(
    installed: Any,
) -> None:
    transit = _Transit()
    now = [1000.0]
    installed(transit.backend(clock=lambda: now[0]))

    def reads() -> int:
        return sum(1 for r in transit.requests if r.url.path.endswith("/keys/auth-signing"))

    await keys.jwks()
    await keys.jwks()
    assert reads() == 1
    now[0] += keys.PUBLIC_KEYS_TTL_S + 1
    await keys.jwks()
    assert reads() == 2

    # A rotation: OpenBao signs with version 3, which the cached copy lacks.
    transit.signing[3] = _p256()
    transit.sign_with = 3
    token = await keys.sign_jwt({"sub": "u-1"})
    assert reads() == 3
    assert jwt.get_unverified_header(token)["kid"] == _rfc7638(transit.signing[3].public_key())


@pytest.mark.parametrize("call", ["hmac", "sign", "jwks"])
async def test_openbao_failing_is_unavailable_never_a_guess(call: str, installed: Any) -> None:
    transit = _Transit()
    transit.fail = True
    installed(transit.backend())
    with pytest.raises(AuthKeysUnavailable):
        if call == "hmac":
            await keys.pepper_hmac(uuid4(), b"x")
        elif call == "sign":
            await keys.sign_es256(b"x")
        else:
            await keys.jwks()


@pytest.mark.parametrize("answer", [{"data": None}, [], {"data": ["x"]}, {"data": {"hmac": None}}])
@pytest.mark.parametrize("call", ["hmac", "sign", "jwks", "jwt"])
async def test_an_unreadable_200_is_unavailable_not_a_500(
    call: str, answer: Any, installed: Any
) -> None:
    def odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answer)

    installed(
        OpenBaoAuthKeys(
            "https://bao.test", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(odd))
        )
    )
    with pytest.raises(AuthKeysUnavailable):
        if call == "hmac":
            await keys.pepper_hmac(uuid4(), b"x")
        elif call == "sign":
            await keys.sign_es256(b"x")
        elif call == "jwks":
            await keys.jwks()
        else:
            await keys.sign_jwt({"sub": "u-1"})


async def test_an_empty_key_set_cannot_sign_a_token(installed: Any) -> None:
    class Empty(LocalAuthKeys):
        async def public_keys(self, *, refresh: bool = False) -> dict[int, Any]:
            return {}

    installed(Empty.generate())
    with pytest.raises(AuthKeysUnavailable):
        await keys.sign_jwt({"sub": "u-1"})


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=str(tmp_path),
    )
    return Settings(**(base | overrides))


def test_the_factory_selects_the_backend(tmp_path: Path) -> None:
    assert isinstance(keys.build(_settings(tmp_path)), LocalAuthKeys)
    bao = _settings(
        tmp_path, auth_keys_backend="openbao", openbao_addr="https://bao.test", openbao_token="t"
    )
    assert isinstance(keys.build(bao), OpenBaoAuthKeys)
    with pytest.raises(ValueError):
        keys.build(_settings(tmp_path, auth_keys_backend="openbao"))


def test_production_refuses_the_local_backend_and_creates_no_file(tmp_path: Path) -> None:
    """A-M2, the half phase 1 had: nothing is created. Re-pointed from
    `MissingAuthKeyError` (plan 2026-11-06, Task 2, decision 1). What a test
    can see is that no file appears; that no file was READ is the code's
    order (`build` refuses before `from_dir`), not something asserted here."""
    with pytest.raises(LocalAuthKeysInProduction):
        keys.build(_settings(tmp_path, environment="production"))
    assert not (tmp_path / keys.PEPPER_FILE).exists()
    assert not (tmp_path / keys.SIGNING_FILE).exists()


def test_the_local_backend_never_invents_a_key_it_was_told_not_to(tmp_path: Path) -> None:
    """`from_dir(..., allow_generate=False)` on a missing file still raises
    `MissingAuthKeyError`: after D38 no production path reaches it, and the
    contract stays."""
    with pytest.raises(MissingAuthKeyError):
        LocalAuthKeys.from_dir(tmp_path, allow_generate=False)
    assert not (tmp_path / keys.PEPPER_FILE).exists()


def test_production_refuses_the_local_backend_even_with_its_files(tmp_path: Path) -> None:
    """A-M2, the half it lacked: files a development start left on the same
    volume do not make `local` acceptable in production."""
    LocalAuthKeys.from_dir(tmp_path, allow_generate=True)
    assert (tmp_path / keys.PEPPER_FILE).exists()
    with pytest.raises(LocalAuthKeysInProduction) as refused:
        keys.build(_settings(tmp_path, environment="production"))
    assert "ITS_AUTH_KEYS_BACKEND=openbao" in str(refused.value)


def test_production_refuses_a_separate_local_keys_directory_too(tmp_path: Path) -> None:
    keys_dir = tmp_path / "auth-keys"
    LocalAuthKeys.from_dir(keys_dir, allow_generate=True)
    with pytest.raises(LocalAuthKeysInProduction):
        keys.build(_settings(tmp_path, environment="production", auth_keys_dir=str(keys_dir)))


def test_production_takes_openbao(tmp_path: Path) -> None:
    bao = _settings(
        tmp_path,
        environment="production",
        auth_keys_backend="openbao",
        openbao_addr="https://bao.test",
        openbao_token="t",
    )
    assert isinstance(keys.build(bao), OpenBaoAuthKeys)


@pytest.mark.parametrize("environment", ["dev", "test"])
def test_development_and_tests_keep_the_local_backend(tmp_path: Path, environment: str) -> None:
    built = keys.build(_settings(tmp_path, environment=environment))
    assert isinstance(built, LocalAuthKeys)
    assert (tmp_path / keys.PEPPER_FILE).exists()


async def test_the_local_files_are_private_and_reused(tmp_path: Path, installed: Any) -> None:
    first = LocalAuthKeys.from_dir(tmp_path, allow_generate=True)
    for name in (keys.PEPPER_FILE, keys.SIGNING_FILE):
        assert ((tmp_path / name).stat().st_mode & 0o777) == 0o600, name
    again = LocalAuthKeys.from_dir(tmp_path, allow_generate=False)
    org = uuid4()
    assert await first.hmac(org.bytes, version=None) == await again.hmac(org.bytes, version=None)
    installed(first)
    kid_first = (await keys.jwks())["keys"][0]["kid"]
    installed(again)
    assert (await keys.jwks())["keys"][0]["kid"] == kid_first
