"""Which tokens each door accepts, by ORIGIN rather than by audience alone.

The audience says which resource a token is for; it does not say what kind of
token it is or who obtained it. Keycloak's ID token for a client is signed by
the realm and carries `aud: <that client id>` -- so once open client
registration lets anyone create a client whose id is the console's API
audience (`it-suite`), its ID tokens pass an audience-only check as console
tokens, with the signed-in person's `sub`, `org` and full RBAC. And a client
anyone registered can enable a service account and mint `client_credentials`
tokens that name no person at all.

So, pinned here:

- `validate_token` admits only access tokens: `typ` exactly "Bearer", as
  Keycloak issues every access token. ID, refresh, offline and logout tokens,
  and a token with no `typ`, are refused on every door;
- no non-MCP door reads a bearer at all: the console signs in by cookie since
  own authentication phase 1 (Task 13);
- `/mcp` refuses a token the console's client obtained, and any service-account
  token, before admission provisions anything: an MCP session is a token issued
  to a person after their own sign-in (invariant 1c, MCP server design §9).
"""

import ast
import asyncio
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
import websockets
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.security import (
    JwksCache,
    TokenClaims,
    TokenValidationError,
    require_person_mcp_token,
    validate_token,
)
from app.models import User
from app.models.auth import AuthSession
from app.modules.remote.tunnel import TunnelAuthError, authorize_tunnel_session
from tests.fake_idp import CONSOLE_CLIENT_ID, ISSUER, JWKS_URL, FakeIdp, jwks_client
from tests.mcp_support import MCP_AUD, mcp_client, mcp_token, running_mcp
from tests.session_support import CONSOLE, refusal_code, sign_in, socket_connection, ws_cookie

NOT_ACCESS_EN = "this token is not an access token"
NOT_ACCESS_ES = "este token no es un token de acceso"
CONSOLE_OVER_MCP_EN = "this token was issued to the console and is not accepted over MCP"
NOT_A_PERSON_EN = "MCP access must be granted by a person signing in"
NOT_A_PERSON_ES = "el acceso por MCP debe concederlo una persona al iniciar sesión"

METADATA_URL = "https://itms.example.com/.well-known/oauth-protected-resource/mcp"
REFUSED_CHALLENGE = f'Bearer resource_metadata="{METADATA_URL}", error="invalid_token"'
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    },
}


def _settings(**overrides: Any) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@localhost/x",
        database_admin_url="postgresql+asyncpg://x:x@localhost/x",
        oidc_issuer=ISSUER,
        oidc_jwks_url=JWKS_URL,
        ca_dir="/tmp/ca",
        **overrides,
    )


def _id_token(idp: FakeIdp, sub: str, slug: str | None = "toeshee", **kw: Any) -> str:
    """What Keycloak issues as the ID token of a client registered as
    `it-suite`: `typ` "ID", `aud` and `azp` both that client id."""
    return idp.token(sub, slug, aud="it-suite", azp="it-suite", typ="ID", **kw)


# --------------------------------------------------------------------------- #
# The settings default
# --------------------------------------------------------------------------- #


def test_the_console_client_id_defaults_to_the_spa_client() -> None:
    assert _settings().oidc_console_client_id == "it-suite-web"
    assert CONSOLE_CLIENT_ID == "it-suite-web"


def test_the_console_client_id_is_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("ITS_OIDC_CONSOLE_CLIENT_ID", "other-spa")
    assert _settings().oidc_console_client_id == "other-spa"


# --------------------------------------------------------------------------- #
# validate_token: access tokens only
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("typ", ["ID", "Refresh", "Offline", "Logout", "bearer", "BEARER", ""])
async def test_a_token_that_is_not_an_access_token_is_refused(typ: str) -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    with pytest.raises(TokenValidationError) as exc:
        await validate_token(idp.token("s", typ=typ), settings=_settings(), jwks=cache)
    assert str(exc.value) == NOT_ACCESS_EN


async def test_a_token_with_no_typ_is_refused() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    with pytest.raises(TokenValidationError) as exc:
        await validate_token(idp.token("s", typ=None), settings=_settings(), jwks=cache)
    assert str(exc.value) == NOT_ACCESS_EN


async def test_an_id_token_carrying_the_console_audience_is_refused() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    with pytest.raises(TokenValidationError):
        await validate_token(_id_token(idp, "s"), settings=_settings(), jwks=cache)


async def test_a_bearer_token_is_admitted_and_carries_its_origin() -> None:
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    claims = await validate_token(
        idp.token("s", extra={"preferred_username": "alice", "client_id": "x"}),
        settings=_settings(),
        jwks=cache,
    )
    assert claims.azp == "it-suite-web"
    assert claims.preferred_username == "alice"
    assert claims.client_id_claim is True
    plain = await validate_token(idp.token("s"), settings=_settings(), jwks=cache)
    assert plain.preferred_username == ""
    assert plain.client_id_claim is False


# --------------------------------------------------------------------------- #
# The two origin rules, as units
# --------------------------------------------------------------------------- #


def _claims(**kw: Any) -> TokenClaims:
    return TokenClaims(sub="s", email="", name="", org_slug="o", **kw)


@pytest.mark.parametrize("azp", ["it-suite-web", "other-spa"])
async def test_no_client_s_token_is_a_console_credential(api_env, azp: str) -> None:
    """Was: the console-token origin rule admitted only `oidc_console_client_id`
    (two tests: the default client, and the client the setting names). Now no
    access token is a console credential, whoever obtained it and whatever the
    setting names."""
    api_env.settings.oidc_console_client_id = azp
    _, slug, sub = await _person(api_env)
    token = api_env.idp.token(sub, slug, azp=azp, email=f"{sub}@test.local")
    resp = await api_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert refusal_code(resp) == "auth_required"


def test_require_person_mcp_token() -> None:
    settings = _settings()
    require_person_mcp_token(
        _claims(azp="claude", preferred_username="alice", sid="s-1"), settings
    )
    with pytest.raises(TokenValidationError) as exc:
        require_person_mcp_token(_claims(azp="it-suite-web"), settings)
    assert str(exc.value) == CONSOLE_OVER_MCP_EN
    for claims in (
        _claims(azp="claude", preferred_username="service-account-claude"),
        _claims(azp="claude", client_id_claim=True),
        _claims(azp="claude", preferred_username="alice"),  # no `sid`: no sign-in
    ):
        with pytest.raises(TokenValidationError) as exc:
            require_person_mcp_token(claims, settings)
        assert str(exc.value) == NOT_A_PERSON_EN


async def test_an_mcp_token_off_the_channel_is_no_credential(mcp_env) -> None:
    """Was: the console-token origin rule refused the MCP audience. Off the channel
    no bearer is read at all, so an MCP grant is still never replayable
    against the console."""
    _, slug, sub = await _person(mcp_env)
    token = mcp_env.idp.token(sub, slug, aud=MCP_AUD, azp="claude", email=f"{sub}@test.local")
    resp = await mcp_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert refusal_code(resp) == "auth_required"


async def test_the_legacy_client_id_claim_marks_a_service_account() -> None:
    """Keycloak before 25 hardcoded the service-account mapper as `clientId`."""
    idp = FakeIdp()
    cache = JwksCache(JWKS_URL, client=jwks_client(idp))
    claims = await validate_token(
        idp.token("s", extra={"clientId": "x"}), settings=_settings(), jwks=cache
    )
    assert claims.client_id_claim is True
    assert claims.is_service_account is True
    with pytest.raises(TokenValidationError) as exc:
        require_person_mcp_token(
            await validate_token(
                idp.token("s", azp="claude", extra={"clientId": "claude"}),
                settings=_settings(),
                jwks=cache,
            ),
            _settings(),
        )
    assert str(exc.value) == NOT_A_PERSON_EN


# --------------------------------------------------------------------------- #
# The sweep: no door validates a token without an origin rule
# --------------------------------------------------------------------------- #

APP = Path(__file__).resolve().parents[1] / "app"
ORIGIN_RULES = frozenset({"require_person_mcp_token"})


def _called(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _unruled_validations(source: str) -> list[int]:
    """Lines of `validate_token(` calls whose enclosing function calls no
    origin rule after them."""
    tree = ast.parse(source)
    found: list[int] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls = [n for n in ast.walk(fn) if _called(n) is not None]
        rules = [n.lineno for n in calls if _called(n) in ORIGIN_RULES]
        for call in calls:
            if _called(call) == "validate_token" and not any(r >= call.lineno for r in rules):
                found.append(call.lineno)
    return found


def test_the_sweep_catches_a_validation_with_no_origin_rule() -> None:
    """Non-vacuity: the detector flags the shape it exists to forbid, and
    passes the shape every door has."""
    bad = "async def door(t):\n    claims = await validate_token(t)\n    return claims\n"
    good = (
        "async def door(t, s):\n    claims = await validate_token(t)\n"
        "    require_person_mcp_token(claims, s)\n"
    )
    assert _unruled_validations(bad) == [2]
    assert _unruled_validations(good) == []


def test_every_validate_token_call_in_app_is_followed_by_an_origin_rule() -> None:
    offenders: list[str] = []
    sites = 0
    for path in sorted(APP.rglob("*.py")):
        if path == APP / "core" / "security.py":
            continue  # the definition, and the helpers themselves
        source = path.read_text()
        if "validate_token(" not in source:
            continue
        sites += source.count("validate_token(")
        rel = path.relative_to(APP.parent)
        offenders += [f"{rel}:{line}" for line in _unruled_validations(source)]
    # Exactly one since own authentication phase 1 (Task 13), which took the
    # console's bearer out of the API, the UI WebSocket and the remote tunnel.
    assert sites == 1, (
        f"{sites}: the MCP branch of admit() is the only door that validates a "
        "token; a second one is a decision"
    )
    assert offenders == [], (
        "validate_token( with no require_person_mcp_token( "
        f"after it in the same function: {offenders}"
    )


# --------------------------------------------------------------------------- #
# The HTTP door (auth_context, unmarked)
# --------------------------------------------------------------------------- #


async def _person(env: Any, role: str = "org-admin") -> tuple[UUID, str, str]:
    org_id = await env.create_org("origin")
    sub = await env.create_user(org_id, role)
    return org_id, await env.org_slug(org_id), sub


async def test_a_console_token_no_longer_reaches_api_me(api_env) -> None:
    """Own authentication phase 1: the console signs in by cookie, and the
    token it used to send is no credential at /api/v1."""
    org_id, slug, sub = await _person(api_env)
    token = api_env.idp.token(sub, slug, email=f"{sub}@test.local")
    resp = await api_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert refusal_code(resp) == "auth_required"
    resp = await api_env.client.get("/api/v1/me", headers=await api_env.headers(org_id, sub))
    assert resp.status_code == 200, resp.text


async def test_an_id_token_is_refused_on_api_me(api_env) -> None:
    """Still a 401 in both languages; since own authentication Task 13 the
    door refuses before reading any bearer (`auth_required`), where it used to
    refuse the token as no access token."""
    _, slug, sub = await _person(api_env)
    token = _id_token(api_env.idp, sub, slug, email=f"{sub}@test.local")
    resp = await api_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert refusal_code(resp) == "auth_required"
    resp = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}", "Accept-Language": "es"}
    )
    assert resp.status_code == 401
    assert refusal_code(resp) == "auth_required"


@pytest.mark.parametrize("azp", ["attacker", "it-suite", None])
async def test_an_access_token_another_client_obtained_is_refused_on_api(
    api_env, azp: str | None
) -> None:
    _, slug, sub = await _person(api_env)
    token = api_env.idp.token(sub, slug, azp=azp, email=f"{sub}@test.local")
    resp = await api_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert refusal_code(resp) == "auth_required"
    resp = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}", "Accept-Language": "es"}
    )
    assert resp.status_code == 401
    assert refusal_code(resp) == "auth_required"


async def test_a_refused_origin_provisions_nobody(api_env) -> None:
    org_id = await api_env.create_org("origin")
    slug = await api_env.org_slug(org_id)
    stranger = f"stranger-{uuid4()}"
    for token in (
        _id_token(api_env.idp, stranger, slug),
        api_env.idp.token(stranger, slug, azp="attacker"),
    ):
        resp = await api_env.client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
    assert await _users_with_sub(api_env, stranger) == 0


async def _users_with_sub(env: Any, sub: str) -> int:
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(
                select(func.count()).select_from(User).where(User.external_subject == sub)
            )
        ).scalar_one()


# --------------------------------------------------------------------------- #
# The UI WebSocket, over a real socket
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
async def live_api(api_env):
    port = _free_port()
    config = uvicorn.Config(api_env.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield port
    server.should_exit = True
    await task


async def _ws_admits(
    port: int, *, token: str | None = None, cookie: str | None = None, origin: str = CONSOLE
) -> bool:
    """Whether `/api/v1/ws` stays open for a handshake offering `token` the way
    the console used to (the `bearer` subprotocol) or carrying `cookie`."""
    url = f"ws://127.0.0.1:{port}/api/v1/ws"
    offered: dict[str, Any] = (
        {"subprotocols": ["bearer", token]}
        if token is not None
        else ws_cookie(cookie or "", origin=origin)
    )
    try:
        async with websockets.connect(url, **offered) as ws:
            # The door closes a refused token before or right after the
            # handshake; one that is admitted stays open.
            try:
                await asyncio.wait_for(ws.recv(), timeout=0.5)
            except TimeoutError:
                return True
            return True
    except (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed):
        return False


async def test_the_ui_websocket_admits_no_token_at_all(api_env, live_api) -> None:
    """Was `..._admits_only_console_access_tokens`, where the console's own
    token opened the socket. Since own authentication Task 13 none does --
    that one included -- and the person's session cookie is the control."""
    org_id, slug, sub = await _person(api_env, "read-only")
    for token in (
        api_env.idp.token(sub, slug),
        _id_token(api_env.idp, sub, slug),
        api_env.idp.token(sub, slug, azp="attacker"),
        api_env.idp.token(sub, slug, typ=None),
    ):
        assert await _ws_admits(live_api, token=token) is False
    signed = await sign_in(api_env, org_id, sub)
    assert await _ws_admits(live_api, cookie=signed.token) is True


async def test_the_ui_websocket_refuses_a_token_carrying_the_mcp_audience(
    api_env, live_api
) -> None:
    """With an MCP resource configured, a token naming it opens nothing; the
    person's session from the configured console is the control."""
    api_env.settings.public_console_url = "https://itms.example.com"
    org_id, slug, sub = await _person(api_env, "read-only")
    signed = await sign_in(api_env, org_id, sub)
    assert (
        await _ws_admits(live_api, cookie=signed.token, origin="https://itms.example.com")
        is True
    )
    both = api_env.idp.token(sub, slug, extra={"aud": ["it-suite", MCP_AUD]})
    assert await _ws_admits(live_api, token=both) is False


# --------------------------------------------------------------------------- #
# The remote tunnel's gate
# --------------------------------------------------------------------------- #


async def _authorize(env: Any, cookie: str) -> tuple[UUID, UUID, ClientScope]:
    return await authorize_tunnel_session(
        connection=socket_connection(env, cookie),
        session_id=uuid4(),
        settings=env.settings,
        app_engine=env.app_engine,
    )


async def test_the_remote_tunnel_admits_only_a_live_session(api_env) -> None:
    """Was `..._admits_only_console_access_tokens`: the genuine console token
    passed and three other tokens did not. The tunnel reads no token since own
    authentication Task 13; a cookie that names no session is what is refused."""
    org_id, _, sub = await _person(api_env, "technician")
    # Genuine: authenticated, then 4404 for a session that does not exist --
    # past the credential check, which is what this control shows.
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, (await sign_in(api_env, org_id, sub)).token)
    assert exc.value.code == 4404
    for cookie in ("garbage", "A" * 43, ""):
        with pytest.raises(TunnelAuthError) as exc:
            await _authorize(api_env, cookie)
        assert exc.value.code == 4401, cookie


# --------------------------------------------------------------------------- #
# /mcp: a person's token, never the console's, never a service account's
# --------------------------------------------------------------------------- #


@pytest.fixture()
async def mcp_env(api_env):
    api_env.settings.public_console_url = "https://itms.example.com"
    return api_env


async def _mcp_org(env: Any) -> tuple[UUID, str]:
    from sqlalchemy import update

    from app.models import Organization

    org_id = await env.create_org("mcp-origin")
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(Organization)
            .where(Organization.id == org_id)
            .values(settings={"mcp_enabled": True})
        )
        await session.commit()
    return org_id, await env.org_slug(org_id)


def _bearer(token: str, **extra: str) -> dict[str, str]:
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}", **extra}


async def test_a_persons_mcp_token_is_admitted(mcp_env) -> None:
    org_id, slug = await _mcp_org(mcp_env)
    sub = await mcp_env.create_user(org_id, "org-admin")
    token = mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=_bearer(token))
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    ("kw", "detail"),
    [
        ({"azp": "claude", "extra": {"preferred_username": "service-account-x"}}, NOT_A_PERSON_EN),
        ({"azp": "claude", "extra": {"client_id": "claude"}}, NOT_A_PERSON_EN),
        ({"azp": "it-suite-web"}, CONSOLE_OVER_MCP_EN),
        ({"azp": "claude", "typ": "ID"}, NOT_ACCESS_EN),
        # Proven live (deploy/keycloak/ci_mcp_flow.py): a person's token always
        # carries `sid`, a `client_credentials` token never does.
        ({"azp": "claude", "sid": None}, NOT_A_PERSON_EN),
    ],
    ids=["service-account-username", "client-id-claim", "console-azp", "id-token", "no-sid"],
)
async def test_a_token_no_person_signed_in_for_is_refused_at_mcp(
    mcp_env, kw: dict[str, Any], detail: str
) -> None:
    org_id, slug = await _mcp_org(mcp_env)
    # A sub with no `users` row: a refusal must come before admission, which
    # would otherwise provision one (and write its first-seen `auth.login`).
    sub = f"svc-{uuid4()}"
    token = mcp_env.idp.token(sub, slug, aud=MCP_AUD, email=f"{sub}@test.local", **kw)
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=_bearer(token))
        resp_es = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(token, **{"Accept-Language": "es"})
        )
    assert resp.status_code == 401, resp.text
    assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE
    assert resp.json()["detail"] == detail
    if detail == NOT_A_PERSON_EN:
        assert resp_es.json()["detail"] == NOT_A_PERSON_ES
    assert await _users_with_sub(mcp_env, sub) == 0


async def test_the_remote_tunnel_refuses_an_ended_session(api_env) -> None:
    """Was `..._refuses_a_token_carrying_the_mcp_audience`: the tunnel reads no
    token since own authentication Task 13, so the both-audiences token is no
    case any more. The same shape holds for the credential it does read: live,
    past the door (4404 for no such remote session); ended, 4401."""
    org_id, _, sub = await _person(api_env, "technician")
    signed = await sign_in(api_env, org_id, sub)
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, signed.token)
    assert exc.value.code == 4404
    async with AsyncSession(api_env.admin_engine) as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == signed.session_id)
            .values(ended_at=datetime.now(UTC), ended_reason="signed_out")
        )
        await session.commit()
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, signed.token)
    assert exc.value.code == 4401


async def test_the_api_refuses_a_console_token_carrying_the_mcp_audience(mcp_env) -> None:
    _, slug, sub = await _person(mcp_env)
    both = mcp_env.idp.token(
        sub, slug, email=f"{sub}@test.local", extra={"aud": ["it-suite", MCP_AUD]}
    )
    resp = await mcp_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {both}"})
    assert resp.status_code == 401
    assert refusal_code(resp) == "auth_required"


async def test_a_legacy_client_id_claim_is_refused_at_mcp(mcp_env) -> None:
    org_id, slug = await _mcp_org(mcp_env)
    sub = f"svc-{uuid4()}"
    token = mcp_env.idp.token(
        sub, slug, aud=MCP_AUD, azp="claude", email=f"{sub}@test.local",
        extra={"clientId": "claude"},
    )
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=_bearer(token))
    assert resp.status_code == 401, resp.text
    assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE
    assert resp.json()["detail"] == NOT_A_PERSON_EN
    assert await _users_with_sub(mcp_env, sub) == 0
