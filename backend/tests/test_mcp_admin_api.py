"""The console's side of the MCP server: which AI apps acted for this
organization, and revoking one (MCP server plan, Task 9; design §3.4, §7, §8).

What is pinned here:

- `GET /api/v1/mcp/connections` lists the apps that called tools in THIS org,
  from `mcp.tool_called`, with how many people used each and when, and whether
  a revocation newer than that use stands; then the apps holding a live grant
  that called nothing yet (plan B3-28);
- `POST /api/v1/mcp/connections/{app_id}/revoke` ends the app's grants -- the
  person's, or the organization's -- and writes the revocation row and its
  audit event in ONE transaction; no identity provider is called (own
  authentication phase 3, plan 2026-11-06 Task 10);
- the same revocation ends the app's unspent authorization codes first, so a
  code approved a moment before it no longer exchanges (operator decision D2,
  part c);
- "everyone" needs `orgs.manage` on top of the route's `mcp.use`, and is
  refused before anything is written;
- one organization's revocation never reaches another's users;
- `PATCH /orgs/current` audits `mcp.settings_changed` when the switch moves.

Keycloak is still `httpx.MockTransport`: for `kc_client.revoke_consent`'s own
tests, which stay until phase 4, and to prove that revoking sends it nothing.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import connections, grants
from app.core.auth.oauth.store import oauth_session
from app.core.mcp_channel import mcp_resource
from app.mcp import exclusions
from app.models import AuditEvent, Organization, Permission, Role, RolePermission, User, UserRole
from app.models.mcp import McpRevocation
from app.models.oauth import OAuthRefreshFamily
from app.modules.user_admin import kc_client
from app.modules.user_admin.kc_client import (
    KeycloakAdminConfig,
    KeycloakAdminError,
    delete_user,
    execute_actions_email,
    revoke_consent,
    set_enabled,
    set_locale_attribute,
)
from tests.mcp_support import mcp_app, mcp_client, mcp_token, running_mcp
from tests.oauth_support import authorize_query, code_of, connect, decide, pkce_pair, token_request
from tests.race_helpers import normalised, pause_at, until_blocked_by

BASE = "https://itms.example.com"
_REAL_CLIENT = httpx.AsyncClient

CONFIG = KeycloakAdminConfig(
    base_url="https://keycloak.test",
    realm="it-suite",
    client_id="it-suite-provisioning",
    client_secret="not-a-real-secret",
)
TOKEN = "tok-never-log-me"

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

ROUTES = (
    ("GET", "/api/v1/mcp/connections"),
    ("POST", "/api/v1/mcp/connections/{app_id}/revoke"),
)


# --------------------------------------------------------------------------- #
# Fake Keycloak: the token endpoint and the consents endpoint, nothing else
# --------------------------------------------------------------------------- #


class FakeKeycloak:
    def __init__(self) -> None:
        #: (Keycloak user id, client id) of every consent DELETE, in order.
        self.deleted: list[tuple[str, str]] = []
        self.raw_paths: list[str] = []
        #: Every request that reached the transport, token exchanges included.
        self.requests: list[httpx.Request] = []
        self.token_requests = 0
        #: How many `httpx.AsyncClient`s were built for Keycloak.
        self.clients = 0
        self.delete_status = 204
        self.raise_connect = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_connect:
            raise httpx.ConnectError("boom", request=request)
        path = request.url.path
        if path.endswith("/protocol/openid-connect/token"):
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": TOKEN})
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        prefix = "/admin/realms/it-suite/users/"
        assert request.method == "DELETE", f"unexpected call {request.method} {path}"
        assert path.startswith(prefix), f"unexpected path {path}"
        user_id, consents, client_id = path.removeprefix(prefix).split("/")
        assert consents == "consents"
        self.raw_paths.append(request.url.raw_path.decode())
        self.deleted.append((user_id, client_id))
        return httpx.Response(self.delete_status)


def _install(monkeypatch: pytest.MonkeyPatch, kc: FakeKeycloak) -> None:
    """Every client built WITHOUT a transport is Keycloak's -- that is how
    `kc_client` builds one. A client that names its own transport (the MCP
    server's in-process call, the tests' ASGI client) keeps it."""

    def factory(*args: Any, **kw: Any) -> httpx.AsyncClient:
        if "transport" in kw:
            return _REAL_CLIENT(*args, **kw)
        kc.clients += 1
        return _REAL_CLIENT(*args, transport=httpx.MockTransport(kc.handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture()
async def env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    api_env.settings.public_console_url = BASE
    kc = FakeKeycloak()
    _install(monkeypatch, kc)
    monkeypatch.setattr(
        KeycloakAdminConfig, "from_settings", classmethod(lambda cls, settings: CONFIG)
    )
    return SimpleNamespace(api=api_env, kc=kc)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _org(env: Any, *, mcp_enabled: bool = True) -> tuple[UUID, str]:
    org_id = await env.api.create_org("mcpadm")
    if mcp_enabled:
        async with AsyncSession(env.api.admin_engine) as session:
            await session.execute(
                update(Organization)
                .where(Organization.id == org_id)
                .values(settings={"mcp_enabled": True})
            )
            await session.commit()
    return org_id, await env.api.org_slug(org_id)


async def _user_id(env: Any, sub: str) -> UUID:
    async with AsyncSession(env.api.admin_engine) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _grant(env: Any, org_id: UUID, sub: str, *codes: str) -> None:
    user_id = await _user_id(env, sub)
    async with AsyncSession(env.api.admin_engine) as session:
        role = Role(org_id=org_id, name=f"mcp-{uuid4().hex[:6]}")
        session.add(role)
        await session.flush()
        for code in codes:
            perm = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=perm))
        session.add(UserRole(org_id=org_id, user_id=user_id, role_id=role.id))
        await session.commit()


async def _rows(env: Any, org_id: UUID) -> list[McpRevocation]:
    async with AsyncSession(env.api.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(McpRevocation).where(McpRevocation.org_id == org_id)
                )
            ).scalars()
        )


async def _events(env: Any, org_id: UUID, action: str) -> list[AuditEvent]:
    async with AsyncSession(env.api.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def _old_token(env: Any, slug: str, sub: str, azp: str = "claude") -> str:
    """An MCP access token issued a minute ago, before any revocation below."""
    return await mcp_token(env.api, slug, sub, azp, iat=int(time.time()) - 60)


async def _initialize(env: Any, token: str) -> int:
    async with mcp_client(env.api) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}
        )
    return resp.status_code


@asynccontextmanager
async def _session(env: Any, token: str) -> AsyncIterator[ClientSession]:
    """The SDK's own client, initialised, over an in-process transport."""
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=env.api.app),
        base_url=BASE,
        headers={"Authorization": f"Bearer {token}"},
    )
    async with (
        http,
        streamable_http_client(f"{BASE}/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def _call_a_tool(env: Any, slug: str, sub: str, azp: str = "claude") -> None:
    token = await mcp_token(env.api, slug, sub, azp)
    async with _session(env, token) as session:
        result = await session.call_tool("me_get", {})
    assert result.is_error is False, result


async def _connections(env: Any, org_id: UUID, sub: str) -> httpx.Response:
    return await env.api.client.get(
        "/api/v1/mcp/connections", headers=await env.api.headers(org_id, sub)
    )


async def _revoke(
    env: Any, org_id: UUID, sub: str, scope: str, app: UUID | str | None = None
) -> httpx.Response:
    """Revoke `app` (the org's "claude" when None: registered here if it was
    not yet) as `sub`."""
    target = app if app is not None else await mcp_app(env.api, org_id)
    return await env.api.client.post(
        f"/api/v1/mcp/connections/{target}/revoke",
        headers=await env.api.headers(org_id, sub),
        json={"scope": scope},
    )


async def _families(env: Any, org_id: UUID) -> list[OAuthRefreshFamily]:
    async with AsyncSession(env.api.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(OAuthRefreshFamily)
                    .where(OAuthRefreshFamily.org_id == org_id)
                    .order_by(OAuthRefreshFamily.created_at)
                )
            ).scalars()
        )


async def _live(env: Any, org_id: UUID, sub: str) -> int:
    user_id = await _user_id(env, sub)
    return sum(
        1 for f in await _families(env, org_id) if f.user_id == user_id and f.revoked_at is None
    )


async def _approved(env: Any, org_id: UUID, slug: str, sub: str) -> dict[str, Any]:
    """The person approves "claude" on the consent screen and its code is
    exchanged (Task 8): a live grant and its refresh token, and no tool called.
    The consent is redeemed in the deployment's organization (plan B3-09)."""
    env.api.settings.default_org_slug = slug
    await mcp_app(env.api, org_id)
    return await connect(env.api, org_id, sub, client_id="claude")


async def _pending_code(env: Any, org_id: UUID, slug: str, sub: str) -> tuple[str, str]:
    """The person approves "claude" on the consent screen and the app has not
    exchanged the code yet: `(code, verifier)`."""
    env.api.settings.default_org_slug = slug
    await mcp_app(env.api, org_id)
    verifier, challenge = pkce_pair()
    approved = await decide(env.api, org_id, sub, authorize_query("claude", challenge=challenge))
    assert approved.status_code == 200, approved.text
    return code_of(approved.json()["redirect_to"]), verifier


async def _exchange(env: Any, code: str, verifier: str) -> httpx.Response:
    return await token_request(
        env.api,
        grant_type="authorization_code",
        code=code,
        client_id="claude",
        code_verifier=verifier,
    )


# --------------------------------------------------------------------------- #
# 1. Listing
# --------------------------------------------------------------------------- #


async def test_two_people_using_one_app_list_as_one_app_with_two_people(env: Any) -> None:
    org_id, slug = await _org(env)
    ada = await env.api.create_user(org_id, "org-admin")
    bob = await env.api.create_user(org_id, "org-admin")
    # Another organization's use of another app is not this org's business.
    other_id, other_slug = await _org(env)
    stranger = await env.api.create_user(other_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, slug, ada)
        await _call_a_tool(env, slug, bob)
        await _call_a_tool(env, other_slug, stranger, azp="someone-elses-app")

    resp = await _connections(env, org_id, ada)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["address"] == f"{BASE}/mcp"
    (app,) = body["apps"]
    assert app["client_id"] == "claude"
    assert app["id"] == str(await mcp_app(env.api, org_id))
    assert app["name"] == "claude"
    assert (app["host"], app["kind"]) == ("claude.ai", "registered")
    assert app["people"] == 2
    assert app["last_used_at"] is not None
    assert app["revoked_for_me"] is False
    assert app["revoked_for_everyone"] is False


async def test_an_app_approved_and_not_yet_used_is_listed_and_can_be_revoked(env: Any) -> None:
    """An app approved a moment ago has called no tool: it is listed all the
    same, with the id revoking names, and revoking it ends its grant."""
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    await _approved(env, org_id, slug, me)

    (app,) = (await _connections(env, org_id, me)).json()["apps"]
    assert app == {
        "client_id": "claude",
        "id": str(await mcp_app(env.api, org_id)),
        "name": "claude",
        "host": "claude.ai",
        "kind": "registered",
        "people": 0,
        "last_used_at": None,
        "revoked_for_me": False,
        "revoked_for_everyone": False,
    }
    resp = await _revoke(env, org_id, me, "me", app=app["id"])

    assert resp.json() == {"revoked": True, "grants_revoked": 1}
    assert await _live(env, org_id, me) == 0
    assert (await _connections(env, org_id, me)).json()["apps"] == []


async def test_the_list_says_when_the_switch_is_off_and_there_is_no_address(env: Any) -> None:
    env.api.settings.public_console_url = None
    org_id, _ = await _org(env, mcp_enabled=False)
    ada = await env.api.create_user(org_id, "org-admin")

    resp = await _connections(env, org_id, ada)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": False, "address": None, "apps": []}


async def test_listing_needs_mcp_use(env: Any) -> None:
    org_id, _ = await _org(env)
    tech = await env.api.create_user(org_id, "technician")
    resp = await _connections(env, org_id, tech)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "requires mcp.use"


# --------------------------------------------------------------------------- #
# 2. Revoke for me
# --------------------------------------------------------------------------- #


async def test_revoke_for_me_cuts_off_my_old_token_and_not_my_colleagues(env: Any) -> None:
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    colleague = await env.api.create_user(org_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, slug, me)
        await _call_a_tool(env, slug, colleague)

        resp = await _revoke(env, org_id, me, "me")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"revoked": True, "grants_revoked": 1}
        (row,) = await _rows(env, org_id)
        assert row.user_id == await _user_id(env, me)
        assert row.client_id == "claude"
        assert row.revoked_by == await _user_id(env, me)
        # No identity provider is asked anything.
        assert env.kc.requests == []
        (event,) = await _events(env, org_id, "mcp.app_revoked")
        assert event.actor_id == await _user_id(env, me)
        assert event.meta == {"client_id": "claude", "scope": "me", "grants_revoked": 1}
        # My grant ended, my colleague's stands (before the `_old_token`s
        # below: each opens a grant of its own).
        assert await _live(env, org_id, me) == 0
        assert await _live(env, org_id, colleague) == 1
        assert {f.revoked_reason for f in await _families(env, org_id) if f.revoked_at} == {
            "revoked_in_settings"
        }

        assert await _initialize(env, await _old_token(env, slug, me)) == 401
        assert await _initialize(env, await _old_token(env, slug, colleague)) == 200

    mine = (await _connections(env, org_id, me)).json()["apps"][0]
    theirs = (await _connections(env, org_id, colleague)).json()["apps"][0]
    assert (mine["revoked_for_me"], mine["revoked_for_everyone"]) == (True, False)
    assert (theirs["revoked_for_me"], theirs["revoked_for_everyone"]) == (False, False)


async def test_a_settings_revoke_also_ends_the_refresh_token(env: Any) -> None:
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    tokens = await _approved(env, org_id, slug, me)

    assert (await _revoke(env, org_id, me, "me")).status_code == 200

    resp = await token_request(
        env.api,
        grant_type="refresh_token",
        refresh_token=tokens["refresh_token"],
        client_id="claude",
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


@pytest.mark.parametrize("scope", ["me", "everyone"])
async def test_a_code_approved_before_a_revoke_no_longer_exchanges(env: Any, scope: str) -> None:
    """Operator decision D2, part c, for Settings: the revocation ends the
    app's unspent codes first -- the person's for "me", everyone's in the
    organization for "everyone" -- by their expiry, never by `used_at`. A
    code approved a moment before it is a plain `invalid_grant` and opens
    no grant; a colleague's survives "me" and not "everyone"."""
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    colleague = await env.api.create_user(org_id, "org-admin")
    mine = await _pending_code(env, org_id, slug, me)
    theirs = await _pending_code(env, org_id, slug, colleague)

    resp = await _revoke(env, org_id, me, scope)

    assert resp.json() == {"revoked": True, "grants_revoked": 0}
    refused = await _exchange(env, *mine)
    assert (refused.status_code, refused.json()["error"]) == (400, "invalid_grant")
    assert await _live(env, org_id, me) == 0
    other = await _exchange(env, *theirs)
    if scope == "me":
        assert other.status_code == 200, other.text
        assert await _live(env, org_id, colleague) == 1
    else:
        assert (other.status_code, other.json()["error"]) == (400, "invalid_grant")
        assert await _live(env, org_id, colleague) == 0
    # Ended, not spent: never read as a reuse.
    assert await _events(env, org_id, "mcp.code_reused") == []


# --------------------------------------------------------------------------- #
# 3-4. Revoke for everyone
# --------------------------------------------------------------------------- #


async def test_revoke_for_everyone_without_orgs_manage_is_403_and_writes_nothing(
    env: Any,
) -> None:
    org_id, slug = await _org(env)
    tech = await env.api.create_user(org_id, "technician")
    await _grant(env, org_id, tech, "mcp.use")
    # After `_grant`: without `mcp.use` the consent gives back no code.
    await _approved(env, org_id, slug, tech)

    resp = await _revoke(env, org_id, tech, "everyone")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "requires orgs.manage"
    assert await _rows(env, org_id) == []
    assert await _events(env, org_id, "mcp.app_revoked") == []
    assert env.kc.requests == []
    assert await _live(env, org_id, tech) == 1
    # Non-vacuity: the same person may revoke for themselves.
    assert (await _revoke(env, org_id, tech, "me")).status_code == 200


async def test_revoking_for_me_an_app_only_a_colleague_holds_shows_it_revoked_for_me(
    env: Any,
) -> None:
    """An `orgs.manage` holder lists the apps everyone holds. Revoking for
    themselves one that only a colleague holds ends nothing, and the row says
    "revoked for you"; the colleague's grant and row stand."""
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    colleague = await env.api.create_user(org_id, "org-admin")
    await _approved(env, org_id, slug, colleague)
    app_id = str(await mcp_app(env.api, org_id))

    resp = await _revoke(env, org_id, me, "me", app=app_id)

    assert resp.json() == {"revoked": True, "grants_revoked": 0}
    (app,) = (await _connections(env, org_id, me)).json()["apps"]
    assert (app["id"], app["revoked_for_me"], app["revoked_for_everyone"]) == (app_id, True, False)
    (theirs,) = (await _connections(env, org_id, colleague)).json()["apps"]
    assert (theirs["revoked_for_me"], theirs["revoked_for_everyone"]) == (False, False)
    assert await _live(env, org_id, colleague) == 1


async def test_revoke_for_everyone_cuts_off_the_whole_org_and_no_other(env: Any) -> None:
    org_id, slug = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    colleague = await env.api.create_user(org_id, "org-admin")
    other_id, other_slug = await _org(env)
    stranger = await env.api.create_user(other_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, slug, admin)
        await _call_a_tool(env, slug, colleague)
        await _call_a_tool(env, other_slug, stranger)

        resp = await _revoke(env, org_id, admin, "everyone")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"revoked": True, "grants_revoked": 2}
        (row,) = await _rows(env, org_id)
        assert row.user_id is None
        assert row.revoked_by == await _user_id(env, admin)
        assert await _rows(env, other_id) == []
        assert env.kc.requests == []
        (event,) = await _events(env, org_id, "mcp.app_revoked")
        assert event.meta == {"client_id": "claude", "scope": "everyone", "grants_revoked": 2}
        # Before the `_old_token`s below: each opens a grant of its own.
        assert await _live(env, org_id, admin) == 0
        assert await _live(env, org_id, colleague) == 0
        assert await _live(env, other_id, stranger) == 1

        assert await _initialize(env, await _old_token(env, slug, admin)) == 401
        assert await _initialize(env, await _old_token(env, slug, colleague)) == 401
        # A metadata-document client has the same `client_id` in every org: the
        # other org's grant and token are untouched.
        assert await _initialize(env, await _old_token(env, other_slug, stranger)) == 200


async def test_a_revocation_shows_as_revoked_for_everyone(env: Any) -> None:
    org_id, slug = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, slug, admin)
    assert (await _revoke(env, org_id, admin, "everyone")).status_code == 200
    (app,) = (await _connections(env, org_id, admin)).json()["apps"]
    assert app["revoked_for_everyone"] is True


# --------------------------------------------------------------------------- #
# 5. No identity provider, one transaction
# --------------------------------------------------------------------------- #


async def test_revoking_calls_no_identity_provider(env: Any) -> None:
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    env.kc.raise_connect = True

    resp = await _revoke(env, org_id, me, "me")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True, "grants_revoked": 0}
    assert env.kc.requests == []
    (row,) = await _rows(env, org_id)
    assert row.user_id == await _user_id(env, me)
    async with running_mcp(env.api):
        assert await _initialize(env, await _old_token(env, slug, me)) == 401


@pytest.mark.parametrize("fails", ["grants", "audit"])
async def test_a_revocation_is_written_whole_or_not_at_all(
    env: Any, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    """The grants, the row and the event are one transaction (plan D12): if
    ending the grants or writing the event fails, nothing is written."""
    from app.core.audit import audit_service

    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, slug, me)

    async def broken(*args: Any, **kw: Any) -> Any:
        raise OSError("database went away")

    if fails == "grants":
        monkeypatch.setattr(connections.grants, "revoke_app", broken)
    else:
        real_record = audit_service.record

        async def flaky_record(session: Any, **kw: Any) -> Any:
            if kw.get("action") == "mcp.app_revoked":
                raise OSError("database went away")
            return await real_record(session, **kw)

        monkeypatch.setattr(audit_service, "record", flaky_record)
    app_id = await mcp_app(env.api, org_id)
    transport = httpx.ASGITransport(app=env.api.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as http:
        resp = await http.post(
            f"/api/v1/mcp/connections/{app_id}/revoke",
            headers={**await env.api.headers(org_id, me), "Origin": BASE},
            json={"scope": "me"},
        )

    assert resp.status_code == 500
    assert await _rows(env, org_id) == []
    assert await _events(env, org_id, "mcp.app_revoked") == []
    assert await _live(env, org_id, me) == 1


async def test_revoking_needs_no_provisioning_client(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        KeycloakAdminConfig, "from_settings", classmethod(lambda cls, settings: None)
    )
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, slug, me)
    resp = await _revoke(env, org_id, me, "me")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True, "grants_revoked": 1}
    assert len(await _rows(env, org_id)) == 1
    assert env.kc.requests == []


async def test_revoking_for_a_large_organization_ends_every_grant_in_the_request(
    env: Any,
) -> None:
    """Forty people with a grant each: one revocation ends all forty, inside
    the request, with no call per person to anyone."""
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    app_id = await mcp_app(env.api, org_id)
    resource = mcp_resource(env.api.settings)
    assert resource is not None
    subs = [await env.api.create_user(org_id, "technician") for _ in range(40)]
    people = [await _user_id(env, sub) for sub in subs]
    async with oauth_session(org_id, env.api.app_engine) as session:
        for user_id in people:
            await grants.open_family(
                session,
                org_id=org_id,
                user_id=user_id,
                oauth_client_id=app_id,
                resource=resource,
                now=datetime.now(UTC),
            )

    started = time.monotonic()
    resp = await _revoke(env, org_id, admin, "everyone")

    assert time.monotonic() - started < 5
    assert resp.json() == {"revoked": True, "grants_revoked": 40}
    assert all(f.revoked_at is not None for f in await _families(env, org_id))
    assert env.kc.requests == []


async def test_a_grant_past_its_idle_limit_is_neither_ended_nor_counted(env: Any) -> None:
    """Dead already: a revocation leaves it as it is, and `grants_revoked`
    counts the grants it ended, not those that had."""
    org_id, slug = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    await _approved(env, org_id, slug, admin)
    resource = mcp_resource(env.api.settings)
    assert resource is not None
    app_id = await mcp_app(env.api, org_id)
    async with oauth_session(org_id, env.api.app_engine) as session:
        await grants.open_family(
            session,
            org_id=org_id,
            user_id=await _user_id(env, admin),
            oauth_client_id=app_id,
            resource=resource,
            now=datetime.now(UTC) - timedelta(days=31),
        )

    resp = await _revoke(env, org_id, admin, "everyone")

    assert resp.json() == {"revoked": True, "grants_revoked": 1}
    expired, live = sorted(await _families(env, org_id), key=lambda f: f.expires_at)
    assert (expired.revoked_at, expired.revoked_reason) == (None, None)
    assert live.revoked_reason == "revoked_in_settings"


async def test_an_exchange_in_flight_during_a_revoke_leaves_no_live_grant(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coordinator's notes on `revoke_app_access`: the codes first, then
    the families in a SEPARATE UPDATE. A colleague's app is exchanging its
    code -- held where it opens the grant, the code locked FOR UPDATE --
    when an administrator revokes the app for everyone. The codes UPDATE
    waits for the exchange, which commits its grant; the families UPDATE
    begins after that commit, reads the new grant and ends it. One UPDATE
    for both, or the families first, would have read the grant table before
    the exchange committed and left its grant live."""
    org_id, slug = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    colleague = await env.api.create_user(org_id, "org-admin")
    code, verifier = await _pending_code(env, org_id, slug, colleague)
    # Both read before the exchange holds the colleague's row and code.
    headers = await env.api.headers(org_id, admin)
    app_id = await mcp_app(env.api, org_id)
    paused, go, seen = pause_at(monkeypatch, grants, "open_family")
    exchanging = asyncio.create_task(_exchange(env, code, verifier))
    await asyncio.wait_for(paused.wait(), 30)
    revoking = asyncio.create_task(
        env.api.client.post(
            f"/api/v1/mcp/connections/{app_id}/revoke",
            headers=headers,
            json={"scope": "everyone"},
        )
    )
    try:
        waiting = await until_blocked_by(env.api.app_engine, seen["pid"])
        held = not revoking.done()
    finally:
        go.set()
    exchanged, revoked = await asyncio.wait_for(
        asyncio.gather(exchanging, revoking, return_exceptions=True), timeout=60
    )

    assert held
    assert [normalised(q).split(" ")[:2] for q in waiting] == [["UPDATE", "OAUTH_CODES"]], waiting
    assert not isinstance(exchanged, BaseException), exchanged
    assert exchanged.status_code == 200, exchanged.text
    assert not isinstance(revoked, BaseException), revoked
    assert revoked.json() == {"revoked": True, "grants_revoked": 1}
    assert await _live(env, org_id, colleague) == 0


# --------------------------------------------------------------------------- #
# The request's shape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dots", ["%2E%2E", "%2e%2e", ".%2E", "%2E.", "%2E", "..x", ".hidden"])
async def test_a_dot_segment_is_no_app_id_and_reaches_nothing(env: Any, dots: str) -> None:
    """A path segment that is not one UUID names no app: 422 before anything is
    read or written. Until own authentication phase 3 a dot segment here
    reached Keycloak's URL and deleted identities."""
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, admin, "everyone", app=dots)
    assert resp.status_code == 422, resp.text
    assert await _rows(env, org_id) == []
    assert await _events(env, org_id, "mcp.app_revoked") == []
    assert env.kc.requests == []


async def test_a_bare_dot_reaches_nothing(env: Any) -> None:
    """A literal `.` is resolved away by the client before it is sent, so it
    names no route; nothing is written either way."""
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, admin, "everyone", app=".")
    assert resp.status_code in (404, 405, 422), resp.text
    assert await _rows(env, org_id) == []
    assert env.kc.requests == []


@pytest.mark.parametrize(
    "first_party",
    [
        "it-suite",
        "it-suite-web",
        "it-suite-provisioning",
        "it-suite-events",
        "account",
        "account-console",
        "admin-cli",
        "security-admin-console",
        "realm-management",
        "broker",
    ],
)
async def test_the_platforms_own_clients_cannot_even_be_named(
    env: Any, first_party: str
) -> None:
    """They have no registration in `oauth_clients`, and a name is no app id:
    422, nothing written, nothing sent."""
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, admin, "everyone", app=first_party)
    assert resp.status_code == 422, resp.text
    assert await _rows(env, org_id) == []
    assert await _events(env, org_id, "mcp.app_revoked") == []
    assert env.kc.requests == []


async def test_an_unknown_app_is_404_in_the_persons_language(env: Any) -> None:
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    resp = await env.api.client.post(
        f"/api/v1/mcp/connections/{uuid4()}/revoke",
        headers={**await env.api.headers(org_id, admin), "Accept-Language": "es"},
        json={"scope": "me"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "no existe esa aplicación de IA en esta organización"
    assert await _rows(env, org_id) == []


async def test_another_organizations_app_is_404_and_writes_nothing(env: Any) -> None:
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    other_id, other_slug = await _org(env)
    stranger = await env.api.create_user(other_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, other_slug, stranger)
    theirs = await mcp_app(env.api, other_id)

    resp = await _revoke(env, org_id, admin, "everyone", app=theirs)

    assert resp.status_code == 404, resp.text
    assert await _rows(env, org_id) == [] and await _rows(env, other_id) == []
    assert await _live(env, other_id, stranger) == 1


@pytest.mark.parametrize("bad", ["has space", "a+b", "a@b", "x" * 256, "ü"])
async def test_a_malformed_app_id_is_422_and_writes_nothing(env: Any, bad: str) -> None:
    org_id, _ = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, me, "me", app=bad)
    assert resp.status_code == 422, resp.text
    assert await _rows(env, org_id) == []
    assert env.kc.requests == []


async def test_an_app_id_cannot_add_a_path_segment(env: Any) -> None:
    """An encoded slash is decoded by the router before matching, so it names
    no route at all; either way nothing is written and Keycloak is not asked."""
    org_id, _ = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, me, "me", app="a%2F..%2Fb")
    assert resp.status_code in (404, 422), resp.text
    assert await _rows(env, org_id) == []
    assert env.kc.requests == []


@pytest.mark.parametrize("body", [{"scope": "all"}, {}, {"scope": "me", "user_id": "x"}])
async def test_a_malformed_body_is_422(env: Any, body: dict[str, Any]) -> None:
    org_id, _ = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    resp = await env.api.client.post(
        f"/api/v1/mcp/connections/{await mcp_app(env.api, org_id)}/revoke",
        headers=await env.api.headers(org_id, me),
        json=body,
    )
    assert resp.status_code == 422, resp.text
    assert await _rows(env, org_id) == []


async def test_revoking_needs_mcp_use(env: Any) -> None:
    org_id, _ = await _org(env)
    tech = await env.api.create_user(org_id, "technician")
    resp = await _revoke(env, org_id, tech, "me")
    assert resp.status_code == 403
    assert await _rows(env, org_id) == []


# --------------------------------------------------------------------------- #
# The Keycloak call itself
# --------------------------------------------------------------------------- #


async def test_revoke_consent_deletes_the_consent_and_quotes_the_client_id(
    env: Any,
) -> None:
    await revoke_consent(CONFIG, user_id="u-1", client_id="a:b.c")
    assert env.kc.raw_paths == ["/admin/realms/it-suite/users/u-1/consents/a%3Ab.c"]


async def test_revoke_consent_treats_404_as_done(env: Any) -> None:
    env.kc.delete_status = 404
    await revoke_consent(CONFIG, user_id="u-1", client_id="claude")
    assert env.kc.deleted == [("u-1", "claude")]


async def test_revoke_consent_raises_on_any_other_refusal(env: Any) -> None:
    env.kc.delete_status = 500
    with pytest.raises(httpx.HTTPStatusError):
        await revoke_consent(CONFIG, user_id="u-1", client_id="claude")


@pytest.mark.parametrize(
    ("user_id", "client_id"),
    [
        ("u-1", ".."),
        ("u-1", "."),
        ("u-1", ""),
        ("u-1", "a/b"),
        ("u-1", "a\\b"),
        ("..", "claude"),
        (".", "claude"),
        ("", "claude"),
        ("a/b", "claude"),
        ("a\\b", "claude"),
    ],
)
async def test_revoke_consent_refuses_an_id_that_is_not_one_segment_before_sending(
    env: Any, user_id: str, client_id: str
) -> None:
    with pytest.raises(KeycloakAdminError):
        await revoke_consent(CONFIG, user_id=user_id, client_id=client_id)
    # Not even the token exchange: nothing reached the transport.
    assert env.kc.requests == []


@pytest.mark.parametrize("bad", ["..", ".", "", "a/b", "a\\b"])
async def test_every_user_operation_refuses_a_user_id_that_is_not_one_segment(
    env: Any, bad: str
) -> None:
    for call in (
        delete_user(CONFIG, user_id=bad),
        set_enabled(CONFIG, user_id=bad, enabled=False),
        execute_actions_email(CONFIG, user_id=bad, actions=["UPDATE_PASSWORD"]),
        set_locale_attribute(CONFIG, user_id=bad, locale="es"),
    ):
        with pytest.raises(KeycloakAdminError):
            await call
    assert env.kc.requests == []


async def test_the_sent_path_must_end_with_exactly_the_quoted_segments(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second layer, on its own: with the segment guard switched off, a
    `..` is resolved away by URL normalisation, and the check on the URL httpx
    would actually send refuses it without sending."""
    monkeypatch.setattr(kc_client, "_path_segment", lambda value: value)
    with pytest.raises(KeycloakAdminError):
        await revoke_consent(CONFIG, user_id="u-1", client_id="..")
    with pytest.raises(KeycloakAdminError):
        await delete_user(CONFIG, user_id="u-1/..")
    assert env.kc.requests == []


# --------------------------------------------------------------------------- #
# 6. The switch is audited when it moves
# --------------------------------------------------------------------------- #


async def test_turning_the_switch_on_writes_mcp_settings_changed(env: Any) -> None:
    org_id, _ = await _org(env, mcp_enabled=False)
    admin = await env.api.create_user(org_id, "org-admin")
    headers = await env.api.headers(org_id, admin)

    on = await env.api.client.patch(
        "/api/v1/orgs/current", headers=headers, json={"settings": {"mcp_enabled": True}}
    )
    assert on.status_code == 200, on.text
    (event,) = await _events(env, org_id, "mcp.settings_changed")
    assert event.meta == {"mcp_enabled": True}
    assert event.actor_id == await _user_id(env, admin)

    # Saving the same value again, or another key, moves nothing.
    again = await env.api.client.patch(
        "/api/v1/orgs/current", headers=headers, json={"settings": {"mcp_enabled": True}}
    )
    other = await env.api.client.patch(
        "/api/v1/orgs/current", headers=headers, json={"settings": {"timezone": "UTC"}}
    )
    assert again.status_code == 200 and other.status_code == 200, (again.text, other.text)
    assert len(await _events(env, org_id, "mcp.settings_changed")) == 1

    # Clearing it turns it off, which is a change.
    off = await env.api.client.patch(
        "/api/v1/orgs/current", headers=headers, json={"settings": {"mcp_enabled": None}}
    )
    assert off.status_code == 200, off.text
    events = await _events(env, org_id, "mcp.settings_changed")
    assert [e.meta for e in events] == [{"mcp_enabled": True}, {"mcp_enabled": False}]


# --------------------------------------------------------------------------- #
# 7. Exclusions
# --------------------------------------------------------------------------- #


def test_the_connection_routes_are_not_for_mcp() -> None:
    for key in ROUTES:
        assert exclusions.NOT_FOR_MCP[key] == (
            "connections are managed by the person in the console"
        )


async def test_the_connections_read_has_its_own_partial_index(env: Any) -> None:
    """`audit_events` has no `ts` index, so without this the 90-day window would
    read the org's whole audit history (migration 0220)."""
    from sqlalchemy import text

    async with AsyncSession(env.api.admin_engine) as session:
        definition = (
            await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'audit_events' AND indexname = 'ix_audit_mcp_tool_called'"
                )
            )
        ).scalar_one()
    assert "(org_id, ts)" in definition
    assert "'mcp.tool_called'" in definition


def _usage_sql() -> str:
    """`list_connections`' aggregate as PostgreSQL would receive it, with every
    bound parameter as `$n` -- the shape of a prepared statement."""
    import re
    from datetime import UTC, datetime

    from sqlalchemy.dialects import postgresql

    from app.mcp.api import usage_query

    compiled = str(
        usage_query(org_id=uuid4(), user_id=uuid4(), since=datetime.now(UTC)).compile(
            dialect=postgresql.dialect()
        )
    )
    names: list[str] = []

    def positional(match: "re.Match[str]") -> str:
        names.append(match.group(1))
        return f"${len(names)}"

    return re.sub(r"%\((\w+)\)s", positional, compiled)


def test_the_connections_read_states_the_index_predicate_as_a_literal() -> None:
    """A bound `action = $n` can never prove a partial index's predicate in a
    generic plan, which asyncpg's prepared statements reach after a few runs."""
    sql = _usage_sql()
    assert "audit_events.action = 'mcp.tool_called'" in sql
    assert "->> 'app'" in sql


async def test_a_generic_plan_of_the_connections_read_uses_the_partial_index(
    env: Any, pg_cluster: Any
) -> None:
    from tests.conftest import _psql

    plan = _psql(
        "SET enable_seqscan = off; EXPLAIN (GENERIC_PLAN) " + _usage_sql(),
        pg_cluster.dbname,
    )
    assert "ix_audit_mcp_tool_called" in plan, plan
