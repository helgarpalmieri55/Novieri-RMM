"""Sign in with Microsoft, the protocol half (`app/core/auth/entra.py`; plan
2026-11-06, Task 5, decisions 1-5). Every rule decision 2 lists fails a token
that breaks it -- the signing key's issuer and `idp` included; the authorize
URL carries exactly the parameters [M1] names, at `organizations` only; the
platform's certificate and its client assertion are what Entra asks for
([M9]); the exchange sends the verifier and the assertion, never a secret,
and logs the error code only."""

import base64
import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.auth import entra
from app.core.auth.entra import EntraKeys, EntraNotConfigured, EntraRefused, EntraUnavailable
from app.core.config import Settings
from tests.entra_support import (
    APP,
    ASSERTION_TYPE,
    CONSUMERS,
    QUOTED_REFUSAL,
    TENANT,
    FakeMicrosoft,
)

NONCE = "n-0S6_WzA2Mj"
OTHER = uuid4()


@pytest.fixture()
def microsoft(monkeypatch: pytest.MonkeyPatch) -> FakeMicrosoft:
    fake = FakeMicrosoft()
    fake.install(monkeypatch)
    return fake


def _keys(fake: FakeMicrosoft, clock: list[float] | None = None) -> EntraKeys:
    tick = clock if clock is not None else [0.0]
    return EntraKeys(client_factory=fake.client, clock=lambda: tick[0])


def test_the_authorize_url_says_exactly_what_the_flow_needs_at_organizations() -> None:
    url = entra.authorize_url(
        client_id=APP,
        redirect_uri="https://itms.test/sign-in/microsoft",
        state="st",
        nonce=NONCE,
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    )
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
    )
    assert {k: v[0] for k, v in parse_qs(parts.query).items()} == {
        "client_id": str(APP),
        "response_type": "code",
        "redirect_uri": "https://itms.test/sign-in/microsoft",
        "response_mode": "fragment",
        "scope": "openid profile",
        "state": "st",
        "nonce": NONCE,
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }


def test_the_challenge_is_rfc7636s() -> None:
    """RFC 7636 Appendix B's own example."""
    assert entra.challenge_of("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def _settings(url: str | None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir="/tmp/ca",
        public_console_url=url,
    )


def test_the_redirect_is_the_consoles_own_https_address() -> None:
    redirect = entra.redirect_uri(_settings("https://itms.test/"))
    assert redirect == "https://itms.test/sign-in/microsoft"
    for unusable in (None, "", "http://itms.test", "https://itms.test/?x=1"):
        with pytest.raises(EntraNotConfigured):
            entra.redirect_uri(_settings(unusable))


async def test_a_well_formed_token_names_its_tenant_and_account(microsoft: FakeMicrosoft) -> None:
    oid = uuid4()
    token = microsoft.id_token(nonce=NONCE, oid=oid)
    found = await entra.validate_id_token(token, client_id=APP, nonce=NONCE, keys=_keys(microsoft))
    assert found == entra.EntraIdentity(tenant_id=TENANT, object_id=oid, name="Dana Okafor")


async def test_an_idp_naming_the_tokens_own_tenant_is_a_member(microsoft: FakeMicrosoft) -> None:
    token = microsoft.id_token(nonce=NONCE, idp=f"https://sts.windows.net/{TENANT}/")
    found = await entra.validate_id_token(token, client_id=APP, nonce=NONCE, keys=_keys(microsoft))
    assert found.tenant_id == TENANT


def _forged_hs256(fake: FakeMicrosoft) -> str:
    return jwt.encode(
        {"aud": str(APP), "tid": str(TENANT), "oid": str(uuid4()), "nonce": NONCE},
        "secret",
        algorithm="HS256",
        headers={"kid": fake.kid},
    )


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda f: f.id_token(nonce=NONCE, aud=uuid4()), "claims"),
        (lambda f: f.id_token(nonce=NONCE, iss=f"{entra.MICROSOFT_HOST}/{uuid4()}/v2.0"), "claims"),
        (lambda f: f.id_token(nonce=NONCE, iss=f"{entra.MICROSOFT_HOST}/{TENANT}"), "claims"),
        (lambda f: f.id_token(nonce=NONCE, ver="1.0"), "claims"),
        (lambda f: f.id_token(nonce=NONCE, exp=1), "claims"),
        (lambda f: f.id_token(nonce=NONCE, nbf=None), "claims"),
        (lambda f: f.id_token(nonce=NONCE, oid="not-a-guid"), "claims"),
        (lambda f: f.id_token(nonce=NONCE, tid=CONSUMERS), "tenant"),
        # A guest, a personal account invited, another tenant's STS (decision 2, `idp`).
        (lambda f: f.id_token(nonce=NONCE, idp="live.com"), "tenant"),
        (lambda f: f.id_token(nonce=NONCE, idp=f"https://sts.windows.net/{CONSUMERS}/"), "tenant"),
        (lambda f: f.id_token(nonce=NONCE, idp=f"https://sts.windows.net/{OTHER}/"), "tenant"),
        (lambda f: f.id_token(nonce="another-nonce"), "nonce"),
        (lambda f: f.id_token(nonce=None), "nonce"),
        (lambda f: f.id_token(nonce=NONCE, headers={"kid": "unknown"}), "signature"),
        (
            lambda f: f.id_token(
                nonce=NONCE, key=rsa.generate_private_key(public_exponent=65537, key_size=2048)
            ),
            "signature",
        ),
        (_forged_hs256, "token"),
        (lambda f: "not.a.token", "token"),
    ],
)
async def test_every_rule_of_decision_2_refuses_a_token_that_breaks_it(
    microsoft: FakeMicrosoft, make: Any, reason: str
) -> None:
    with pytest.raises(EntraRefused) as refused:
        await entra.validate_id_token(
            make(microsoft), client_id=APP, nonce=NONCE, keys=_keys(microsoft)
        )
    assert refused.value.reason == reason


@pytest.mark.parametrize(
    "key_issuer",
    [
        f"https://login.microsoftonline.com/{CONSUMERS}/v2.0",
        f"https://login.microsoftonline.com/{OTHER}/v2.0",
    ],
)
async def test_a_key_scoped_to_another_issuer_does_not_sign_for_this_tenant(
    microsoft: FakeMicrosoft, key_issuer: str
) -> None:
    """[M6]: a key whose issuer is one tenant's GUID signs only that tenant's
    tokens. A consumer-scoped key signing a work tenant's token is refused."""
    microsoft.key_issuer = key_issuer
    with pytest.raises(EntraRefused) as refused:
        await entra.validate_id_token(
            microsoft.id_token(nonce=NONCE), client_id=APP, nonce=NONCE, keys=_keys(microsoft)
        )
    assert refused.value.reason == "signature"


async def test_unknown_kids_read_the_keys_at_most_once_a_minute(microsoft: FakeMicrosoft) -> None:
    clock = [0.0]
    keys = _keys(microsoft, clock)
    for _ in range(5):
        with pytest.raises(EntraRefused):
            await keys.key("nobody-has-this-kid")
    assert microsoft.jwks_reads == 1
    clock[0] += entra.JWKS_MIN_REFETCH_S
    with pytest.raises(EntraRefused):
        await keys.key("nobody-has-this-kid")
    assert microsoft.jwks_reads == 2
    assert (await keys.key(microsoft.kid)).issuer == microsoft.key_issuer
    assert microsoft.jwks_reads == 2


async def test_no_keys_at_all_is_unavailable_and_stale_keys_still_serve(
    microsoft: FakeMicrosoft,
) -> None:
    """Decision 3: a first read that fails is an outage, not a bad signature;
    once keys are known, a failed re-read keeps them."""
    microsoft.jwks_down = True
    clock = [0.0]
    keys = _keys(microsoft, clock)
    with pytest.raises(EntraUnavailable):
        await keys.key(microsoft.kid)
    microsoft.jwks_down = False
    clock[0] += entra.JWKS_MIN_REFETCH_S
    assert await keys.key(microsoft.kid) is not None
    microsoft.jwks_down = True
    clock[0] += entra.JWKS_TTL_S
    assert await keys.key(microsoft.kid) is not None


def test_the_platform_makes_its_own_certificate() -> None:
    """Decision 5 (C4): an RSA key, a self-signed certificate for less than
    twelve months, and the `x5t#S256` thumbprint of its DER ([M9])."""
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    made = entra.new_certificate(app_client_id=APP, now=now)
    certificate = x509.load_pem_x509_certificate(made.certificate_pem.encode())
    der = certificate.public_bytes(serialization.Encoding.DER)
    assert made.thumbprint == (
        base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    )
    assert made.expires_at == certificate.not_valid_after_utc
    assert made.expires_at - now < timedelta(days=365)
    assert certificate.issuer == certificate.subject
    assert "BEGIN PRIVATE KEY" in made.private_pem
    assert "PRIVATE" not in made.certificate_pem


def test_the_client_assertion_is_the_one_entra_describes() -> None:
    """[M9]: PS256, `typ` JWT, `x5t#S256`; `aud` the token endpoint it goes
    to, `iss` = `sub` = the application, a unique `jti`, five minutes."""
    made = entra.new_certificate(app_client_id=APP, now=datetime.now(UTC))
    certificate = x509.load_pem_x509_certificate(made.certificate_pem.encode())
    assertion = entra.client_assertion(
        client_id=APP, private_pem=made.private_pem, thumbprint=made.thumbprint
    )
    header = jwt.get_unverified_header(assertion)
    assert header == {"alg": "PS256", "typ": "JWT", "x5t#S256": made.thumbprint}
    claims = jwt.decode(
        assertion,
        certificate.public_key(),
        algorithms=["PS256"],
        audience=entra.TOKEN_URL,
        issuer=str(APP),
    )
    assert claims["sub"] == str(APP)
    assert claims["exp"] - claims["nbf"] == entra.ASSERTION_TTL_S <= 600
    assert abs(claims["iat"] - time.time()) < 60
    other = entra.client_assertion(
        client_id=APP, private_pem=made.private_pem, thumbprint=made.thumbprint
    )
    assert jwt.decode(other, options={"verify_signature": False})["jti"] != claims["jti"]


async def test_the_exchange_sends_the_verifier_and_the_assertion_and_no_secret(
    microsoft: FakeMicrosoft,
) -> None:
    made = entra.new_certificate(app_client_id=APP, now=datetime.now(UTC))
    microsoft.trust(made.certificate_pem)
    code = microsoft.issue_code(nonce=NONCE)
    async with microsoft.client() as client:
        id_token = await entra.exchange(
            client=client,
            client_id=APP,
            client_assertion=entra.client_assertion(
                client_id=APP, private_pem=made.private_pem, thumbprint=made.thumbprint
            ),
            code=code,
            redirect_uri="https://itms.test/sign-in/microsoft",
            code_verifier="v" * 43,
        )
    assert id_token.count(".") == 2
    [sent] = microsoft.token_requests
    assert sent.pop("client_assertion").count(".") == 2
    assert sent == {
        "client_id": str(APP),
        "scope": "openid profile",
        "code": code,
        "redirect_uri": "https://itms.test/sign-in/microsoft",
        "grant_type": "authorization_code",
        "code_verifier": "v" * 43,
        "client_assertion_type": ASSERTION_TYPE,
    }


async def test_a_refused_exchange_logs_the_error_code_and_nothing_it_quoted(
    microsoft: FakeMicrosoft, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="its.auth.entra")
    made = entra.new_certificate(app_client_id=APP, now=datetime.now(UTC))
    async with microsoft.client() as client:
        with pytest.raises(EntraRefused) as refused:
            await entra.exchange(
                client=client,
                client_id=APP,
                client_assertion=entra.client_assertion(
                    client_id=APP, private_pem=made.private_pem, thumbprint=made.thumbprint
                ),
                code="never-issued",
                redirect_uri="https://itms.test/sign-in/microsoft",
                code_verifier="v" * 43,
            )
    assert refused.value.reason == "exchange"
    [record] = caplog.records
    assert record.getMessage() == "microsoft sign-in: the token endpoint refused the code"
    assert record.__dict__["error"] == "invalid_client"
    assert QUOTED_REFUSAL not in caplog.text
    assert "PRIVATE" not in caplog.text


def test_keys_of_keeps_one_cache_per_process() -> None:
    class _App:
        class state:  # noqa: N801 - a stand-in for Starlette's app.state
            pass

    app: Any = _App()
    assert entra.keys_of(app) is entra.keys_of(app)
    assert isinstance(entra.keys_of(app), EntraKeys)
