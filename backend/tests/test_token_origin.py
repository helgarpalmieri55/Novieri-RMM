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
- every non-MCP door (`auth_context`, the UI WebSocket, the remote tunnel)
  admits only a token the console's own client obtained
  (`azp == Settings.oidc_console_client_id`);
- `/mcp` refuses a token the console's client obtained, and any service-account
  token, before admission provisions anything: an MCP session is a token issued
  to a person after their own sign-in (invariant 1c, MCP server design §9).
"""

import ast
import asyncio
import socket
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
import websockets
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.ws import AUTH_SUBPROTOCOL
from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.security import (
    JwksCache,
    TokenClaims,
    TokenValidationError,
    require_console_token,
    require_person_mcp_token,
    validate_token,
)
from app.models import User
from app.modules.remote.tunnel import TunnelAuthError, authorize_tunnel
from tests.fake_idp import CONSOLE_CLIENT_ID, ISSUER, JWKS_URL, FakeIdp, jwks_client
from tests.mcp_support import MCP_AUD, mcp_client, mcp_token, running_mcp

NOT_ACCESS_EN = "this token is not an access token"
NOT_ACCESS_ES = "este token no es un token de acceso"
NOT_CONSOLE_EN = "this token was not issued to the console"
NOT_CONSOLE_ES = "este token no se emitió para la consola"
CONSOLE_OVER_MCP_EN = "this token was issued to the console and is not accepted over MCP"
NOT_A_PERSON_EN = "MCP access must be granted by a person signing in"
NOT_A_PERSON_ES = "el acceso por MCP debe concederlo una persona al iniciar sesión"
MCP_ONLY_EN = "this token is for the MCP server and is accepted only through it"

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


def test_require_console_token_admits_only_the_console_client() -> None:
    require_console_token(_claims(azp="it-suite-web"), _settings())
    for azp in ("it-suite", "attacker", "claude", ""):
        with pytest.raises(TokenValidationError) as exc:
            require_console_token(_claims(azp=azp), _settings())
        assert str(exc.value) == NOT_CONSOLE_EN, azp


def test_require_console_token_follows_the_setting() -> None:
    settings = _settings(oidc_console_client_id="other-spa")
    require_console_token(_claims(azp="other-spa"), settings)
    with pytest.raises(TokenValidationError):
        require_console_token(_claims(azp="it-suite-web"), settings)


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


def test_require_console_token_refuses_the_mcp_audience() -> None:
    """One rule for all three console doors: a token naming the MCP resource
    is an MCP grant even when it also names the console and the console's
    client obtained it, and is never replayable against the console."""
    settings = _settings(public_console_url="https://itms.example.com")
    require_console_token(_claims(azp="it-suite-web", aud=("it-suite",)), settings)
    with pytest.raises(TokenValidationError) as exc:
        require_console_token(_claims(azp="it-suite-web", aud=("it-suite", MCP_AUD)), settings)
    assert str(exc.value) == MCP_ONLY_EN
    # With no public console URL there is no MCP resource, so nothing to refuse.
    require_console_token(_claims(azp="it-suite-web", aud=("it-suite", MCP_AUD)), _settings())


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
ORIGIN_RULES = frozenset({"require_console_token", "require_person_mcp_token"})


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
        "    require_console_token(claims, s)\n"
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
    assert sites >= 3, "the sweep found no doors; it is looking in the wrong place"
    assert offenders == [], (
        "validate_token( with no require_console_token( / require_person_mcp_token( "
        f"after it in the same function: {offenders}"
    )


# --------------------------------------------------------------------------- #
# The HTTP door (auth_context, unmarked)
# --------------------------------------------------------------------------- #


async def _person(env: Any, role: str = "org-admin") -> tuple[UUID, str, str]:
    org_id = await env.create_org("origin")
    sub = await env.create_user(org_id, role)
    return org_id, await env.org_slug(org_id), sub


async def test_a_console_token_reaches_api_me(api_env) -> None:
    _, slug, sub = await _person(api_env)
    token = api_env.idp.token(sub, slug, email=f"{sub}@test.local")
    resp = await api_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text


async def test_an_id_token_is_refused_on_api_me(api_env) -> None:
    _, slug, sub = await _person(api_env)
    token = _id_token(api_env.idp, sub, slug, email=f"{sub}@test.local")
    resp = await api_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == NOT_ACCESS_EN
    resp = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}", "Accept-Language": "es"}
    )
    assert resp.json()["detail"] == NOT_ACCESS_ES


@pytest.mark.parametrize("azp", ["attacker", "it-suite", None])
async def test_an_access_token_another_client_obtained_is_refused_on_api(
    api_env, azp: str | None
) -> None:
    _, slug, sub = await _person(api_env)
    token = api_env.idp.token(sub, slug, azp=azp, email=f"{sub}@test.local")
    resp = await api_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == NOT_CONSOLE_EN
    resp = await api_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}", "Accept-Language": "es"}
    )
    assert resp.json()["detail"] == NOT_CONSOLE_ES


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


async def _ws_admits(port: int, token: str) -> bool:
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/api/v1/ws", subprotocols=[AUTH_SUBPROTOCOL, token]
        ) as ws:
            # The door closes a refused token before or right after the
            # handshake; one that is admitted stays open.
            try:
                await asyncio.wait_for(ws.recv(), timeout=0.5)
            except TimeoutError:
                return True
            return True
    except (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed):
        return False


async def test_the_ui_websocket_admits_only_console_access_tokens(api_env, live_api) -> None:
    _, slug, sub = await _person(api_env, "read-only")
    assert await _ws_admits(live_api, api_env.idp.token(sub, slug)) is True
    assert await _ws_admits(live_api, _id_token(api_env.idp, sub, slug)) is False
    assert await _ws_admits(live_api, api_env.idp.token(sub, slug, azp="attacker")) is False
    assert await _ws_admits(live_api, api_env.idp.token(sub, slug, typ=None)) is False


async def test_the_ui_websocket_refuses_a_token_carrying_the_mcp_audience(
    api_env, live_api
) -> None:
    api_env.settings.public_console_url = "https://itms.example.com"
    _, slug, sub = await _person(api_env, "read-only")
    assert await _ws_admits(live_api, api_env.idp.token(sub, slug)) is True
    both = api_env.idp.token(sub, slug, extra={"aud": ["it-suite", MCP_AUD]})
    assert await _ws_admits(live_api, both) is False


# --------------------------------------------------------------------------- #
# The remote tunnel's gate
# --------------------------------------------------------------------------- #


async def _authorize(env: Any, token: str) -> tuple[UUID, UUID, ClientScope]:
    return await authorize_tunnel(
        token=token,
        session_id=uuid4(),
        settings=env.settings,
        jwks=env.app.state.jwks,
        admin_engine=env.admin_engine,
        app_engine=env.app_engine,
    )


async def test_the_remote_tunnel_admits_only_console_access_tokens(api_env) -> None:
    _, slug, sub = await _person(api_env, "technician")
    # Genuine: authenticated, then 4404 for a session that does not exist --
    # past the token check, which is what this control shows.
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, api_env.idp.token(sub, slug))
    assert exc.value.code == 4404
    for token in (
        _id_token(api_env.idp, sub, slug),
        api_env.idp.token(sub, slug, azp="attacker"),
        api_env.idp.token(sub, slug, typ=None),
    ):
        with pytest.raises(TunnelAuthError) as exc:
            await _authorize(api_env, token)
        assert exc.value.code == 4401


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


async def test_the_remote_tunnel_refuses_a_token_carrying_the_mcp_audience(api_env) -> None:
    api_env.settings.public_console_url = "https://itms.example.com"
    _, slug, sub = await _person(api_env, "technician")
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, api_env.idp.token(sub, slug))
    assert exc.value.code == 4404
    both = api_env.idp.token(sub, slug, extra={"aud": ["it-suite", MCP_AUD]})
    with pytest.raises(TunnelAuthError) as exc:
        await _authorize(api_env, both)
    assert exc.value.code == 4401


async def test_the_api_refuses_a_console_token_carrying_the_mcp_audience(mcp_env) -> None:
    _, slug, sub = await _person(mcp_env)
    both = mcp_env.idp.token(
        sub, slug, email=f"{sub}@test.local", extra={"aud": ["it-suite", MCP_AUD]}
    )
    resp = await mcp_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {both}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == MCP_ONLY_EN


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
