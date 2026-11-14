"""The MCP access token (autenticación propia design §5.3, §6.3; plan
2026-11-06, Task 2).

What is pinned here:

- it is ES256, `typ: at+jwt`, ten minutes, audience EXACTLY the MCP resource,
  issuer the console address, and it names the person, the organization, the
  AI app and the grant (RFC 9068 §2.2 plus `org` and `fam`);
- verification fixes all of it (RFC 8725 [S38]): another `typ`, audience,
  issuer, algorithm or key, a longer life, a missing claim -- each is None;
- the assistant's internal token is never an MCP token, nor one it (RFC 8725
  §3.12);
- an unknown `kid` reads the public keys again at most once per
  `REFRESH_EVERY_S` (plan D14).
"""

import base64
import json
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest

from app.core.auth import keys, loopback
from app.core.auth.oauth import access

BASE = "https://itms.example.com"
RESOURCE = f"{BASE}/mcp"
SETTINGS = SimpleNamespace(public_console_url=BASE)


@pytest.fixture(autouse=True)
def local_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[keys.LocalAuthKeys]:
    backend = keys.LocalAuthKeys.generate()
    keys.set_backend(backend)
    # Every test starts with the refresh throttle open (plan D14).
    monkeypatch.setattr(access, "_next_refresh_at", float("-inf"))
    yield backend
    keys.set_backend(None)


async def _mint(**overrides: Any) -> tuple[str, dict[str, Any]]:
    ids = {"user_id": uuid4(), "org_id": uuid4(), "family_id": uuid4()}
    token, expires_in = await access.mint_access_token(
        settings=SETTINGS, client_id="itms_app", **ids, **overrides
    )
    assert expires_in == 600
    return token, ids


async def _signed(claims: dict[str, Any], typ: str = "at+jwt") -> str:
    return await keys.sign_jwt(claims, typ=typ)


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


async def test_the_token_is_es256_at_jwt_for_the_mcp_resource() -> None:
    token, ids = await _mint()
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["typ"] == "at+jwt"
    assert header["kid"] == (await keys.jwks())["keys"][0]["kid"]
    claims = _claims(token)
    assert claims["iss"] == BASE
    assert claims["aud"] == RESOURCE
    assert claims["sub"] == str(ids["user_id"])
    assert claims["org"] == str(ids["org_id"])
    assert claims["fam"] == str(ids["family_id"])
    assert claims["client_id"] == "itms_app"
    assert claims["scope"] == "mcp"
    assert claims["exp"] - claims["iat"] == 600
    assert len(claims["jti"]) >= 16
    assert "email" not in claims


async def test_a_token_verifies_back_to_its_grant() -> None:
    token, ids = await _mint()
    grant = await access.verify_access_token(token, settings=SETTINGS)
    assert grant is not None
    assert grant.user_id == ids["user_id"]
    assert grant.org_id == ids["org_id"]
    assert grant.family_id == ids["family_id"]
    assert grant.client_id == grant.azp == "itms_app"
    assert grant.exp - grant.iat == 600
    assert grant.email == ""


async def test_the_issuer_must_be_an_https_console() -> None:
    no_console = SimpleNamespace(public_console_url="http://itms.example.com")
    with pytest.raises(ValueError):
        await access.mint_access_token(
            settings=no_console, user_id=uuid4(), org_id=uuid4(), client_id="a", family_id=uuid4()
        )
    token, _ = await _mint()
    assert await access.verify_access_token(token, settings=no_console) is None


def _base_claims() -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": BASE,
        "aud": RESOURCE,
        "sub": str(uuid4()),
        "client_id": "itms_app",
        "org": str(uuid4()),
        "fam": str(uuid4()),
        "scope": "mcp",
        "jti": "j" * 22,
        "iat": now,
        "exp": now + 600,
    }


#: Each case changes the good claims; the times are relative to the good
#: token's own `iat`, computed when the test runs, never at collection.
CHANGES: dict[str, Any] = {
    "two-audiences": lambda c: {"aud": [RESOURCE, BASE]},  # the resource AND another
    "console-audience": lambda c: {"aud": BASE},
    "loopback-audience": lambda c: {"aud": "loopback"},
    "keycloak-issuer": lambda c: {"iss": "https://idp.test/realms/it-suite"},
    "loopback-issuer": lambda c: {"iss": "urn:itms:loopback"},
    "wider-scope": lambda c: {"scope": "mcp offline_access"},
    "bad-sub": lambda c: {"sub": "not-a-uuid"},
    "bad-org": lambda c: {"org": 7},
    "no-client": lambda c: {"client_id": ""},
    "too-long": lambda c: {"exp": c["iat"] + 601},
    "future-iat": lambda c: {"iat": c["iat"] + 3600, "exp": c["iat"] + 4200},
    "aud-list": lambda c: {"aud": [RESOURCE]},
    "sub-urn": lambda c: {"sub": f"urn:uuid:{c['sub']}"},
    "sub-braces": lambda c: {"sub": "{" + c["sub"] + "}"},
    "org-unhyphenated": lambda c: {"org": c["org"].replace("-", "")},
    "fam-upper": lambda c: {"fam": c["fam"].upper()},
    "expired": lambda c: {"iat": c["iat"] - 700, "exp": c["iat"] - 100},
}


@pytest.mark.parametrize("case", sorted(CHANGES))
async def test_any_other_claim_is_refused(case: str) -> None:
    good = _base_claims()
    assert await access.verify_access_token(await _signed(good), settings=SETTINGS) is not None
    bad = {**good, **CHANGES[case](good)}
    assert await access.verify_access_token(await _signed(bad), settings=SETTINGS) is None


@pytest.mark.parametrize(
    "missing", ["iss", "aud", "sub", "client_id", "org", "fam", "scope", "jti", "iat", "exp"]
)
async def test_every_claim_is_required(missing: str) -> None:
    claims = _base_claims()
    del claims[missing]
    assert await access.verify_access_token(await _signed(claims), settings=SETTINGS) is None


@pytest.mark.parametrize(
    "typ", ["JWT", "loopback+jwt", "application/jwt", "application/at+jwt", "at+JWT"]
)
async def test_another_typ_is_refused(typ: str) -> None:
    """RFC 9068 §4 allows "at+jwt" or "application/at+jwt"; this check is
    deliberately stricter: the platform is the only issuer and emits only
    `at+jwt`, so `application/at+jwt` is refused too."""
    token = await _signed(_base_claims(), typ=typ)
    assert await access.verify_access_token(token, settings=SETTINGS) is None


async def test_another_algorithm_is_refused() -> None:
    kid = (await keys.jwks())["keys"][0]["kid"]
    unsigned = jwt.encode(
        _base_claims(), key=None, algorithm="none", headers={"typ": "at+jwt", "kid": kid}
    )
    hmac = jwt.encode(
        _base_claims(), key="k" * 32, algorithm="HS256", headers={"typ": "at+jwt", "kid": kid}
    )
    assert await access.verify_access_token(unsigned, settings=SETTINGS) is None
    assert await access.verify_access_token(hmac, settings=SETTINGS) is None
    assert await access.verify_access_token("not.a.jwt", settings=SETTINGS) is None


async def test_a_signature_by_another_key_is_refused(local_keys: keys.LocalAuthKeys) -> None:
    keys.set_backend(keys.LocalAuthKeys.generate())
    stranger = await _signed(_base_claims())
    keys.set_backend(local_keys)
    assert await access.verify_access_token(stranger, settings=SETTINGS) is None


def _b64(data: dict[str, Any]) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )


async def test_a_genuine_header_over_a_strangers_payload_and_signature_is_refused(
    local_keys: keys.LocalAuthKeys,
) -> None:
    """Reaches `jwt.decode` (the kid is real): only the signature check stops it."""
    genuine = await _signed(_base_claims())
    keys.set_backend(keys.LocalAuthKeys.generate())
    stranger = await _signed(_base_claims())
    keys.set_backend(local_keys)
    header = genuine.split(".")[0]
    _, sp, ss = stranger.split(".")
    assert await access.verify_access_token(genuine, settings=SETTINGS) is not None
    assert await access.verify_access_token(f"{header}.{sp}.{ss}", settings=SETTINGS) is None


@pytest.mark.parametrize("change", [{"sub": str(uuid4())}, {"exp": int(time.time()) + 590}])
async def test_a_modified_payload_under_the_genuine_signature_is_refused(
    change: dict[str, Any],
) -> None:
    good = _base_claims()
    h, _, sig = (await _signed(good)).split(".")
    forged = f"{h}.{_b64({**good, **change})}.{sig}"
    assert await access.verify_access_token(forged, settings=SETTINGS) is None


async def test_the_assistants_internal_token_is_never_an_mcp_token() -> None:
    principal = SimpleNamespace(
        factor_done=True, user_id=uuid4(), org_id=uuid4(), session_id=uuid4(), kind="staff"
    )
    internal = await loopback.mint_loopback(principal)  # type: ignore[arg-type]
    assert await access.verify_access_token(internal, settings=SETTINGS) is None
    # ...nor an MCP token the internal one (its verifier fixes typ, aud and iss).
    token, _ = await _mint()
    assert await loopback._verified_claims(token) is None


async def test_an_unknown_kid_reads_the_keys_again_at_most_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshes = 0
    real = keys.jwks

    async def counting(*, refresh: bool = False) -> dict[str, Any]:
        nonlocal refreshes
        refreshes += refresh
        return await real(refresh=refresh)

    clock = [1000.0]
    monkeypatch.setattr(keys, "jwks", counting)
    monkeypatch.setattr(access, "_monotonic", lambda: clock[0])
    forged = jwt.encode(
        _base_claims(), key="k" * 32, algorithm="HS256", headers={"typ": "at+jwt", "kid": "nope"}
    )
    # The header check refuses HS256 before any key is read: use a real ES256
    # signature under an unknown kid instead.
    backend = keys.LocalAuthKeys.generate()
    keys.set_backend(backend)
    unknown = await _signed(_base_claims())
    keys.set_backend(keys.LocalAuthKeys.generate())
    for _ in range(50):
        assert await access.verify_access_token(unknown, settings=SETTINGS) is None
    assert refreshes == 1
    clock[0] += access.REFRESH_EVERY_S
    assert await access.verify_access_token(unknown, settings=SETTINGS) is None
    assert refreshes == 2
    assert await access.verify_access_token(forged, settings=SETTINGS) is None
    assert refreshes == 2


async def test_a_rotated_key_is_read_again_before_the_token_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another api process signed with the new version; this one's copy of the
    public keys is older. One forced read finds it."""
    token, _ = await _mint()
    real = keys.jwks

    async def stale(*, refresh: bool = False) -> dict[str, Any]:
        return await real() if refresh else {"keys": []}

    monkeypatch.setattr(keys, "jwks", stale)
    grant = await access.verify_access_token(token, settings=SETTINGS)
    assert grant is not None
    assert isinstance(grant.family_id, UUID)


def test_the_unverified_client_id_is_a_label_only() -> None:
    token = jwt.encode({"client_id": "itms_x"}, key="k" * 32, algorithm="HS256")
    assert access.unverified_client_id(token) == "itms_x"
    assert access.unverified_client_id(jwt.encode({"client_id": 5}, key="k" * 32)) == ""
    assert access.unverified_client_id("garbage") == ""
