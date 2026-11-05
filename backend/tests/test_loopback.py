"""The assistant's internal token (autenticación propia design §5.6, D1).

Until this task the in-app assistant re-entered the API carrying the caller's
own `Authorization` header. A console signed in by cookie sends no such
header, so every loopback read would have been a 401. Now each turn mints its
own token: ES256 through `keys.sign_jwt`, audience `loopback`, five minutes,
bound to the person's session by `sid`. The door admits it ONLY on an
in-process call -- an ASGI scope extension no network client can set -- that
also says `X-ITMS-Loopback: 1`, only to read, and only while the session lives.

Each refusal below is one check of `app.core.auth.loopback.verify_loopback`
removed; the comment on each test names the check it holds.
"""

import base64
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

import app as app_package
from app.core.ai import executor_portal, executor_staff
from app.core.ai.executor_staff import ToolError
from app.core.ai.providers import TextBlock, ToolCallBlock
from app.core.ai.tools import REGISTRY
from app.core.ai.types import ToolResult
from app.core.auth import keys, loopback
from app.core.mcp_channel import EXTENSION_KEY
from app.models import AiSettings, User
from app.models.auth import AuthSession
from app.modules.ai_admin import api as admin_api
from app.modules.ai_portal import api as portal_api
from tests.ai_admin_fakes import ScriptedProvider, ai_user
from tests.session_support import console_headers, principal_for, sign_in, use_console


@pytest.fixture()
def console(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """`api_env` as a deployment with a console address: the CSRF rule (Task 6)
    compares a cookie write's `Origin` with it."""
    use_console(monkeypatch, api_env)
    return api_env


async def _signed_in(env: Any, *codes: str) -> tuple[UUID, UUID, UUID, dict[str, str]]:
    """`(org, user, session, headers)` of a person holding exactly `codes`,
    signed in with a recent confirmation of the second factor."""
    org_id = await env.create_org("loopback")
    user_id, sub = await ai_user(env, org_id, *codes)
    signed = await sign_in(env, org_id, sub, step_up=True)
    return org_id, user_id, signed.session_id, signed.headers


async def _minted(env: Any, session_id: UUID) -> str:
    return await loopback.mint_loopback(await principal_for(env, session_id))


def _in_process(env: Any) -> httpx.AsyncClient:
    """What the executors build: the app, marked as the assistant's own call."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=loopback.loopback_app(env.app)),
        base_url="http://ai-loopback.invalid",
    )


def _claimed(token: str) -> dict[str, str]:
    """The token, saying it is the assistant's call whatever `typ` it carries
    (so a refusal is the verifier's, not `loopback_headers` declining)."""
    return {"Authorization": f"Bearer {token}", loopback.LOOPBACK_HEADER: "1"}


def _inner_request(
    env: Any, token: str, *, method: str = "GET", extensions: dict[str, Any] | None = None
) -> Request:
    headers = loopback.loopback_headers(f"Bearer {token}")
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/v1/me",
            "raw_path": b"/api/v1/me",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("127.0.0.1", 123),
            "server": ("ai-loopback.invalid", 80),
            "scheme": "http",
            "app": env.app,
            "extensions": (
                {loopback.LOOPBACK_EXTENSION: {}} if extensions is None else extensions
            ),
        }
    )


def _install_tape(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[tuple[httpx.Request, int]]:
    """Point an executor's transport seam at a real `ASGITransport` with a tape,
    built over the app the EXECUTOR passes -- the marked one."""
    seen: list[tuple[httpx.Request, int]] = []

    class _Tape(httpx.AsyncBaseTransport):
        def __init__(self, app: Any) -> None:
            self._inner = httpx.ASGITransport(app=app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await self._inner.handle_async_request(request)
            seen.append((request, response.status_code))
            return response

    monkeypatch.setattr(module, "_transport", _Tape)
    return seen


async def _ai_settings(env: Any, org_id: UUID) -> None:
    async with AsyncSession(env.admin_engine) as session:
        session.add(AiSettings(org_id=org_id))
        await session.commit()


def _unverified(authorization: str) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(
        authorization.removeprefix("Bearer "), options={"verify_signature": False}
    )
    return claims


async def _set_session(env: Any, session_id: UUID, **values: Any) -> None:
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(AuthSession).where(AuthSession.id == session_id).values(**values)
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# The token
# --------------------------------------------------------------------------- #


async def test_the_token_is_es256_for_loopback_and_names_the_session(console: Any) -> None:
    org_id, user_id, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)

    header = jwt.get_unverified_header(token)
    assert (header["alg"], header["typ"]) == ("ES256", loopback.LOOPBACK_TYP)
    key = jwt.PyJWKSet.from_dict(await keys.jwks())[header["kid"]]
    claims = jwt.decode(
        token,
        key=key,
        algorithms=["ES256"],
        audience=loopback.LOOPBACK_AUDIENCE,
        issuer=loopback.LOOPBACK_ISSUER,
    )
    assert claims["sub"] == str(user_id)
    assert claims["org"] == str(org_id)
    assert claims["sid"] == str(session_id)
    assert claims["exp"] - claims["iat"] == loopback.LOOPBACK_TTL_S == 300


async def test_a_session_without_its_second_factor_mints_nothing(console: Any) -> None:
    org_id = await console.create_org("loopback")
    _, sub = await ai_user(console, org_id, "devices.read")
    session_id = (await sign_in(console, org_id, sub, factor_done=False)).session_id
    with pytest.raises(ValueError):
        await _minted(console, session_id)


def test_only_a_loopback_token_is_marked() -> None:
    assert loopback.loopback_headers("Bearer not-a-jwt") == {"Authorization": "Bearer not-a-jwt"}
    assert loopback.loopback_headers("") == {"Authorization": ""}
    assert loopback.is_loopback_token("Bearer not-a-jwt") is False


# --------------------------------------------------------------------------- #
# The door
# --------------------------------------------------------------------------- #


async def test_an_in_process_call_is_admitted_as_the_person(console: Any) -> None:
    """The control: every refusal below is this request with one thing wrong."""
    _, user_id, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)
    assert loopback.is_loopback_token(f"Bearer {token}") is True

    async with _in_process(console) as client:
        me = await client.get("/api/v1/me", headers=loopback.loopback_headers(f"Bearer {token}"))

    assert me.status_code == 200, me.text
    assert me.json()["user_id"] == str(user_id)


@pytest.mark.parametrize("path", ["/api/v1/me", "/api/v1/portal/tickets"])
async def test_the_token_from_the_network_is_refused(console: Any, path: str) -> None:
    """Holds `LOOPBACK_EXTENSION in scope["extensions"]`: the same token and
    the same header, through the network-shaped client, is a 401 -- at the
    console's door and at the portal's. This is design §10.6's "el token
    interno no vale en rutas públicas". The person holds what both routes
    ask, so without the check each would answer 200."""
    _, _, session_id, _ = await _signed_in(console, "devices.read", "portal.ticket.read")
    token = await _minted(console, session_id)
    async with _in_process(console) as client:
        control = await client.get(path, headers=loopback.loopback_headers(f"Bearer {token}"))
    assert control.status_code == 200, control.text

    refused = await console.client.get(path, headers=loopback.loopback_headers(f"Bearer {token}"))

    assert refused.status_code == 401


async def test_a_network_request_claiming_the_loopback_never_falls_through(
    console: Any,
) -> None:
    """A request that says it is the assistant is admitted by the token or by
    nothing: the person's own live cookie, sent from the network with
    `X-ITMS-Loopback: 1`, does not rescue it (`_session_principal` never
    reaches `resolve` once `claims_loopback` holds)."""
    _, _, _, headers = await _signed_in(console, "devices.read")
    assert (await console.client.get("/api/v1/me", headers=headers)).status_code == 200

    me = await console.client.get(
        "/api/v1/me", headers={**headers, loopback.LOOPBACK_HEADER: "1"}
    )

    assert me.status_code == 401


async def test_a_network_caller_cannot_amplify_the_refusal_warning(
    console: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Final review C-M8 (Task 9 M5). "not an in-process call" is the one
    refusal ANYBODY can cause -- an anonymous request carrying
    `X-ITMS-Loopback` -- so it is rate-limited, not downgraded: one WARNING
    per `NETWORK_REFUSAL_EVERY_S` per process, and the next one says how many
    went unlogged. INFO would still write a line per request at the default
    level, so it would move the amplification, not remove it. Every request
    is still the same 401. Log every refusal again and twenty requests are
    twenty lines: red. The in-process reasons, which no network client can
    reach, are still logged every time."""
    clock = [1000.0]
    monkeypatch.setattr(loopback, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(loopback, "_network_refusals", loopback.RefusalThrottle())

    def lines() -> list[Any]:
        return [r for r in caplog.records if r.name == "its.auth.loopback"]

    with caplog.at_level(logging.INFO, logger="its.auth.loopback"):
        answers = [
            await console.client.get("/api/v1/me", headers={loopback.LOOPBACK_HEADER: "1"})
            for _ in range(20)
        ]
        assert [a.status_code for a in answers] == [401] * 20
        (first,) = lines()
        assert first.levelno == logging.WARNING
        assert (first.reason, first.unlogged) == (
            "not an in-process call",
            0,
        )

        clock[0] += loopback.NETWORK_REFUSAL_EVERY_S
        caplog.clear()
        assert (
            await console.client.get("/api/v1/me", headers={loopback.LOOPBACK_HEADER: "1"})
        ).status_code == 401
        (later,) = lines()
        assert later.unlogged == 19

        caplog.clear()
        _, _, session_id, _ = await _signed_in(console, "devices.read")
        token = await _minted(console, session_id)
        async with _in_process(console) as client:
            for _ in range(2):
                bad = await client.get(
                    "/api/v1/me",
                    headers={"Authorization": f"Bearer {token}", loopback.LOOPBACK_HEADER: "0"},
                )
                assert bad.status_code == 401
        assert [r.reason for r in lines()] == ["malformed loopback header"] * 2


@pytest.mark.parametrize("value", ["0", "1, 1", ""])
async def test_a_malformed_loopback_header_is_refused(console: Any, value: str) -> None:
    """Holds `getlist(LOOPBACK_HEADER) == ["1"]`: in-process, with a genuine
    token, a header that says anything but exactly `1` is refused -- and,
    having claimed the loopback, falls through to nothing."""
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)

    async with _in_process(console) as client:
        me = await client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token}", loopback.LOOPBACK_HEADER: value},
        )

    assert me.status_code == 401


async def test_without_its_header_the_token_is_not_a_console_bearer(console: Any) -> None:
    """Marked, but without `X-ITMS-Loopback`: the request takes the ordinary
    path, where the internal token is no credential at all."""
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)

    async with _in_process(console) as client:
        me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 401


async def test_the_token_dies_with_its_session(console: Any) -> None:
    """Holds `ended_at IS NULL` in `_live_session`: signing out, or an admin
    ending the session, ends the assistant's reach inside the same turn."""
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)
    await _set_session(
        console, session_id, ended_at=datetime.now(UTC), ended_reason="signed_out"
    )

    async with _in_process(console) as client:
        me = await client.get("/api/v1/me", headers=loopback.loopback_headers(f"Bearer {token}"))

    assert me.status_code == 401


async def test_the_token_dies_with_an_idle_session(console: Any) -> None:
    """Holds the idle check in `_live_session`: a session `resolve` no longer
    admits (idle past the organisation's limit, 30 minutes by default) is not
    live for the assistant either, though it has neither ended nor expired."""
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)
    await _set_session(
        console, session_id, last_seen_at=datetime.now(UTC) - timedelta(minutes=31)
    )

    async with _in_process(console) as client:
        me = await client.get("/api/v1/me", headers=loopback.loopback_headers(f"Bearer {token}"))

    assert me.status_code == 401


async def test_the_token_dies_with_a_session_past_its_maximum(console: Any) -> None:
    """Holds `expires_at > now` in `_live_session`: the session's own maximum
    (12 hours by default) ends the token too, whatever the token's `exp`."""
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)
    await _set_session(console, session_id, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    async with _in_process(console) as client:
        me = await client.get("/api/v1/me", headers=loopback.loopback_headers(f"Bearer {token}"))

    assert me.status_code == 401


async def test_a_session_without_its_second_factor_is_not_live(console: Any) -> None:
    """Holds `factor_at IS NOT NULL` in `_live_session`. `mint_loopback`
    refuses such a session, so the token is signed here by hand -- what a
    caller that skipped `mint_loopback` would produce. `_live_session` says
    `factor_done=True` of what it returns, so without the check the door
    would admit a password-only session."""
    org_id = await console.create_org("loopback")
    user_id, sub = await ai_user(console, org_id, "devices.read")
    session_id = (await sign_in(console, org_id, sub, factor_done=False)).session_id
    issued = int(time.time())
    token = await keys.sign_jwt(
        {
            "iss": loopback.LOOPBACK_ISSUER,
            "aud": loopback.LOOPBACK_AUDIENCE,
            "sub": str(user_id),
            "org": str(org_id),
            "sid": str(session_id),
            "kind": "staff",
            "iat": issued,
            "exp": issued + loopback.LOOPBACK_TTL_S,
        },
        typ=loopback.LOOPBACK_TYP,
    )

    async with _in_process(console) as client:
        me = await client.get("/api/v1/me", headers=loopback.loopback_headers(f"Bearer {token}"))

    assert me.status_code == 401


async def test_the_token_lasts_five_minutes(console: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Holds the `exp` check: a token minted five minutes and a second ago is
    refused while its session is still live."""
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    monkeypatch.setattr(loopback, "_now", lambda: int(time.time()) - loopback.LOOPBACK_TTL_S - 1)
    token = await _minted(console, session_id)

    async with _in_process(console) as client:
        me = await client.get("/api/v1/me", headers=loopback.loopback_headers(f"Bearer {token}"))

    assert me.status_code == 401


async def test_the_token_only_reads(console: Any) -> None:
    """Holds `request.method in ("GET", "HEAD")`. The write carries the CSRF
    header and the console's Origin too, so whatever the CSRF rule decides
    about a request with no cookie, the refusal seen is the door's."""
    _, user_id, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)

    async with _in_process(console) as client:
        patched = await client.patch(
            "/api/v1/me",
            headers={**loopback.loopback_headers(f"Bearer {token}"), **console_headers()},
            json={"phone": "+57 300 000 0000"},
        )

    assert patched.status_code == 401
    async with AsyncSession(console.admin_engine) as session:
        assert (await session.get(User, user_id)).phone is None  # type: ignore[union-attr]


async def test_a_signature_from_another_key_is_refused(console: Any) -> None:
    """Holds the signature check against the published JWKS: same claims, same
    `kid`, another P-256 key."""
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    genuine = await _minted(console, session_id)
    payload = json.loads(base64.urlsafe_b64decode(genuine.split(".")[1] + "=="))
    forged = jwt.encode(
        payload,
        ec.generate_private_key(ec.SECP256R1()),
        algorithm="ES256",
        headers={"kid": jwt.get_unverified_header(genuine)["kid"], "typ": loopback.LOOPBACK_TYP},
    )

    async with _in_process(console) as client:
        me = await client.get("/api/v1/me", headers=loopback.loopback_headers(f"Bearer {forged}"))

    assert me.status_code == 401


@pytest.mark.parametrize(
    ("change", "typ"),
    [
        ({}, "JWT"),
        ({"aud": "https://itms.test/mcp"}, loopback.LOOPBACK_TYP),
        ({"iss": "https://itms.test"}, loopback.LOOPBACK_TYP),
        ({"exp": loopback.LOOPBACK_TTL_S + 1}, loopback.LOOPBACK_TYP),
        ({"sid": None}, loopback.LOOPBACK_TYP),
        ({"sub": "00000000-0000-4000-8000-000000000000"}, loopback.LOOPBACK_TYP),
    ],
    ids=[
        "another-typ",
        "another-audience",
        "another-issuer",
        "longer-life",
        "no-session",
        "another-person",
    ],
)
async def test_a_genuine_signature_on_another_token_is_refused(
    console: Any, change: dict[str, Any], typ: str
) -> None:
    """Signed by the platform's own key -- which will also sign MCP's tokens
    in phase 3 -- but not a loopback token: another `typ` (RFC 8725 §3.11),
    another audience or issuer, a life longer than five minutes, no session,
    a session that is not the named person's. Each is one fixed check of
    `_verified_claims` or `_live_session`; the control is the same claims,
    unchanged, signed the same way."""
    org_id, user_id, session_id, _ = await _signed_in(console, "devices.read")
    issued = int(time.time())
    genuine: dict[str, Any] = {
        "iss": loopback.LOOPBACK_ISSUER,
        "aud": loopback.LOOPBACK_AUDIENCE,
        "sub": str(user_id),
        "org": str(org_id),
        "sid": str(session_id),
        "kind": "staff",
        "iat": issued,
        "exp": issued + loopback.LOOPBACK_TTL_S,
    }
    other = dict(genuine)
    for name, value in change.items():
        if value is None:
            other.pop(name)
        elif name == "exp":
            other["exp"] = issued + value
        else:
            other[name] = value

    async with _in_process(console) as client:
        control = await client.get(
            "/api/v1/me",
            headers=_claimed(await keys.sign_jwt(genuine, typ=loopback.LOOPBACK_TYP)),
        )
        me = await client.get("/api/v1/me", headers=_claimed(await keys.sign_jwt(other, typ=typ)))

    assert control.status_code == 200, control.text
    assert me.status_code == 401


async def test_a_rotated_key_is_read_again_before_the_token_is_refused(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another api process may hold the public keys for up to
    `keys.PUBLIC_KEYS_TTL_S` after a rotation (Task 2): a token signed with
    the new version names a `kid` its cached JWKS does not have yet. The
    verifier reads the keys again once before refusing."""
    older, newer = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    signer = keys.LocalAuthKeys(pepper={1: os.urandom(32)}, signing={1: older, 2: newer})

    class _StaleCopy:
        """What another process sees: version 1 only, until it reads again."""

        def __init__(self) -> None:
            self.reads_again = 0

        async def hmac(self, data: bytes, *, version: int | None) -> tuple[bytes, int]:
            return await signer.hmac(data, version=version)

        async def sign(self, signing_input: bytes) -> tuple[bytes, int]:
            return await signer.sign(signing_input)

        async def public_keys(
            self, *, refresh: bool = False
        ) -> dict[int, ec.EllipticCurvePublicKey]:
            if refresh:
                self.reads_again += 1
            every = await signer.public_keys()
            return every if self.reads_again else {1: every[1]}

    _, _, session_id, _ = await _signed_in(console, "devices.read")
    monkeypatch.setattr(keys, "_backend", signer)
    token = await _minted(console, session_id)
    stale = _StaleCopy()
    monkeypatch.setattr(keys, "_backend", stale)

    async with _in_process(console) as client:
        me = await client.get("/api/v1/me", headers=loopback.loopback_headers(f"Bearer {token}"))

    assert me.status_code == 200, me.text
    assert stale.reads_again == 1


async def test_without_the_keys_nothing_is_admitted_and_nothing_is_minted(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenBao down: the door answers 503 rather than admitting, and a turn
    route mints nothing -- also a 503, never the request's own header."""
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)

    class _Down:
        async def hmac(self, data: bytes, *, version: int | None) -> tuple[bytes, int]:
            raise keys.AuthKeysUnavailable("down")

        async def sign(self, signing_input: bytes) -> tuple[bytes, int]:
            raise keys.AuthKeysUnavailable("down")

        async def public_keys(
            self, *, refresh: bool = False
        ) -> dict[int, ec.EllipticCurvePublicKey]:
            raise keys.AuthKeysUnavailable("down")

    monkeypatch.setattr(keys, "_backend", _Down())

    async with _in_process(console) as client:
        me = await client.get("/api/v1/me", headers=loopback.loopback_headers(f"Bearer {token}"))
    request = SimpleNamespace(
        state=SimpleNamespace(
            session_principal=await principal_for(console, session_id), via_loopback=False
        ),
        headers=httpx.Headers({"Authorization": "Bearer the-persons-own-header"}),
    )
    with pytest.raises(HTTPException) as refused:
        await loopback.loopback_authorization(request)  # type: ignore[arg-type]

    assert me.status_code == 503
    assert refused.value.status_code == 503


async def test_the_mcp_channel_never_carries_the_token(console: Any) -> None:
    """Holds the `mcp_call_from_scope(...) is not None` refusal: the two
    in-process channels never meet. Called directly, so nothing else on the
    request is wrong."""
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)
    both = {
        loopback.LOOPBACK_EXTENSION: {},
        EXTENSION_KEY: {"azp": "claude", "tool": "me_get", "route": "GET /api/v1/me"},
    }

    assert (await loopback.verify_loopback(_inner_request(console, token))).session_id == session_id
    with pytest.raises(HTTPException) as refused:
        await loopback.verify_loopback(_inner_request(console, token, extensions=both))
    assert refused.value.status_code == 401


async def test_the_token_never_counts_as_a_recent_confirmation(console: Any) -> None:
    """Decision (3): the session confirmed its second factor a moment ago; the
    principal the token yields carries no confirmation at all."""
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    assert (await principal_for(console, session_id)).step_up_at is not None
    token = await _minted(console, session_id)

    principal = await loopback.verify_loopback(_inner_request(console, token))

    assert principal.factor_done is True
    assert principal.step_up_at is None


async def test_a_request_the_token_admitted_mints_no_other(console: Any) -> None:
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    request = SimpleNamespace(
        state=SimpleNamespace(
            session_principal=await principal_for(console, session_id), via_loopback=True
        ),
        headers=httpx.Headers(),
    )
    with pytest.raises(HTTPException) as refused:
        await loopback.loopback_authorization(request)  # type: ignore[arg-type]
    assert refused.value.status_code == 401


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(SimpleNamespace(), id="admit-never-ran"),
        pytest.param(
            SimpleNamespace(session_principal=None, via_loopback=False), id="no-session"
        ),
    ],
)
async def test_a_turn_with_no_session_forwards_nothing(state: Any) -> None:
    """FAIL CLOSED (own authentication Task 13, which removed D21's transition
    branch). A request `admit()` never admitted by a session -- or never ran
    for at all -- gets no `Authorization` for its loopback calls: a 401, and
    never the header the request brought. Put the old
    `return request.headers.get("Authorization", "")` back and the person's
    own header is forwarded again: red."""
    request = SimpleNamespace(
        state=state,
        headers=httpx.Headers({"Authorization": "Bearer the-persons-own-header"}),
    )
    forwarded: str | None = None
    with pytest.raises(HTTPException) as refused:
        forwarded = await loopback.loopback_authorization(request)  # type: ignore[arg-type]
    assert refused.value.status_code == 401
    assert forwarded is None
    assert "the-persons-own-header" not in str(refused.value.detail)


# --------------------------------------------------------------------------- #
# The executors and the turns
# --------------------------------------------------------------------------- #


async def test_the_staff_executor_reads_with_the_token(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install_tape(monkeypatch, executor_staff)
    _, _, session_id, _ = await _signed_in(console, "ai.use", "health.read")
    token = await _minted(console, session_id)

    outcome = await executor_staff.invoke_tool(
        tool_name="health.alerts",
        raw_args={},
        app=console.app,
        bearer=f"Bearer {token}",
        deadline=time.monotonic() + 30.0,
        registry=REGISTRY,
    )

    assert isinstance(outcome, ToolResult), outcome
    ((request, status),) = seen
    assert status == 200
    assert request.headers["x-itms-loopback"] == "1"
    assert request.headers["authorization"] == f"Bearer {token}"


async def test_an_admin_turn_signed_in_by_cookie_reads_with_its_own_token(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect D1 names, end to end: a console that sends a cookie still
    gets an assistant that reads -- with a token bound to its session, never
    with the cookie itself, and never with an `Authorization` header the
    request happened to bring along with the cookie."""
    seen = _install_tape(monkeypatch, executor_staff)
    org_id, user_id, session_id, headers = await _signed_in(console, "ai.use", "health.read")
    await _ai_settings(console, org_id)
    provider = ScriptedProvider(
        [
            [ToolCallBlock(id="1", name="health.alerts", arguments={})],
            [TextBlock(text="Nothing is alerting.")],
        ]
    )
    monkeypatch.setattr(admin_api, "_provider_for", lambda: provider)
    brought = {**headers, "Authorization": "Bearer the-persons-own-header"}

    created = await console.client.post("/api/v1/ai/sessions", headers=headers)
    assert created.status_code == 201, created.text
    turn = await console.client.post(
        f"/api/v1/ai/sessions/{created.json()['id']}/turns",
        headers=brought,
        json={"question": "what is alerting?"},
    )

    assert turn.status_code == 200, turn.text
    ((request, status),) = seen
    assert status == 200
    assert request.headers["x-itms-loopback"] == "1"
    assert "cookie" not in request.headers
    assert "the-persons-own-header" not in request.headers["authorization"]
    claims = _unverified(request.headers["authorization"])
    assert (claims["aud"], claims["sid"], claims["sub"]) == (
        "loopback",
        str(session_id),
        str(user_id),
    )


async def test_a_technician_reaches_the_portal_with_the_cookie(console: Any) -> None:
    """The portal for technicians (design §5.6, phase 1): the same door, the
    cookie instead of a bearer, and the portal gate unchanged."""
    org_id = await console.create_org("portal-cookie")
    sub = await console.create_user(org_id, "requester")
    signed = await sign_in(console, org_id, sub)

    tickets = await console.client.get("/api/v1/portal/tickets", headers=signed.headers)

    assert tickets.status_code == 200, tickets.text


async def test_a_resolver_turn_signed_in_by_cookie_reads_the_portal_with_its_own_token(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install_tape(monkeypatch, executor_portal)
    org_id = await console.create_org("resolver-cookie")
    await _ai_settings(console, org_id)
    sub = await console.create_user(org_id, "requester")
    signed = await sign_in(console, org_id, sub)
    session_id, headers = signed.session_id, signed.headers
    provider = ScriptedProvider(
        [
            [ToolCallBlock(id="1", name="portal.my_tickets", arguments={})],
            [TextBlock(text="You have no open tickets.")],
        ]
    )
    monkeypatch.setattr(portal_api, "_provider_for", lambda: provider)

    created = await console.client.post("/api/v1/portal/ai/sessions", headers=headers)
    assert created.status_code == 201, created.text
    turn = await console.client.post(
        f"/api/v1/portal/ai/sessions/{created.json()['id']}/turns",
        headers=headers,
        json={"question": "what is happening with my ticket?"},
    )

    assert turn.status_code == 200, turn.text
    ((request, status),) = seen
    assert httpx.URL(str(request.url)).path.startswith("/api/v1/portal/")
    assert status == 200
    assert request.headers["x-itms-loopback"] == "1"
    assert _unverified(request.headers["authorization"])["sid"] == str(session_id)


async def test_a_staff_tool_still_never_leaves_the_portal_executor(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The portal executor's surface guard runs before the token is even
    looked at (`executor_portal._refuse_off_surface`)."""
    seen = _install_tape(monkeypatch, executor_portal)
    _, _, session_id, _ = await _signed_in(console, "devices.read")
    token = await _minted(console, session_id)

    outcome = await executor_portal.invoke_portal_tool(
        tool_name="health.alerts",
        raw_args={},
        app=console.app,
        bearer=f"Bearer {token}",
        deadline=time.monotonic() + 30.0,
        registry={"health.alerts": REGISTRY["health.alerts"]},
    )

    assert isinstance(outcome, ToolError) and outcome.kind == "forbidden"
    assert seen == []


def test_no_assistant_route_forwards_the_callers_authorization_header() -> None:
    """The regression D1 exists to prevent: an assistant route reading the
    raw header again would 401 every loopback call of a cookie console."""
    modules = Path(app_package.__file__).resolve().parent / "modules"
    routes = sorted(modules.glob("ai_*/api.py"))
    assert [path.parent.name for path in routes] == ["ai_admin", "ai_copilot", "ai_portal"]
    offenders = [
        f"{path.parent.name}/api.py:{number}"
        for path in routes
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if 'headers.get("Authorization"' in line
    ]
    assert offenders == []


def test_the_loopback_reader_is_reviewed() -> None:
    """`ALL_SCOPE_ALLOWLIST` names the one function here that reads under
    every client, with its reason; `tests/test_client_catalog.py` fails if
    any other function of the module does, or if this one stops."""
    from app.core.client_scope import ALL_SCOPE_ALLOWLIST

    reason = ALL_SCOPE_ALLOWLIST["app/core/auth/loopback.py::_live_session"]
    assert "internal token" in reason
