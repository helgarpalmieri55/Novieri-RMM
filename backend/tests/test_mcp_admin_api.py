"""The console's side of the MCP server: which AI apps acted for this
organization, and revoking one (MCP server plan, Task 9; design §3.4, §7, §8).

What is pinned here:

- `GET /api/v1/mcp/connections` lists the apps that called tools in THIS org,
  from `mcp.tool_called`, with how many people used each and when, and whether
  a revocation newer than that use stands;
- `POST /api/v1/mcp/connections/{client_id}/revoke` writes the revocation row
  and its audit event FIRST, then removes the app's consent in Keycloak per
  person, best effort -- so the gate refuses old tokens even when Keycloak is
  down, and the response counts what Keycloak did not do;
- "everyone" needs `orgs.manage` on top of the route's `mcp.use`, and is
  refused before anything is written;
- one organization's revocation never reaches another's users;
- `PATCH /orgs/current` audits `mcp.settings_changed` when the switch moves.

Keycloak is `httpx.MockTransport` (the `tests/test_me_locale.py` pattern): it
proves the requests sent, not what a real Keycloak does with them.
"""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import anyio
import httpx
import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp import exclusions
from app.models import AuditEvent, Organization, Permission, Role, RolePermission, User, UserRole
from app.models.mcp import McpRevocation
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
from tests.mcp_support import MCP_AUD, mcp_client, running_mcp

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
    ("POST", "/api/v1/mcp/connections/{client_id}/revoke"),
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


def _old_token(env: Any, slug: str, sub: str, azp: str = "claude") -> str:
    """An MCP access token issued a minute ago, before any revocation below."""
    return env.api.idp.token(
        sub,
        slug,
        aud=MCP_AUD,
        azp=azp,
        email=f"{sub}@test.local",
        extra={"iat": int(time.time()) - 60},
    )


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
    token = env.api.idp.token(sub, slug, aud=MCP_AUD, azp=azp, email=f"{sub}@test.local")
    async with _session(env, token) as session:
        result = await session.call_tool("me_get", {})
    assert result.is_error is False, result


async def _connections(env: Any, org_id: UUID, sub: str) -> httpx.Response:
    return await env.api.client.get(
        "/api/v1/mcp/connections", headers=await env.api.headers(org_id, sub)
    )


async def _revoke(
    env: Any, org_id: UUID, sub: str, scope: str, client_id: str = "claude"
) -> httpx.Response:
    return await env.api.client.post(
        f"/api/v1/mcp/connections/{client_id}/revoke",
        headers=await env.api.headers(org_id, sub),
        json={"scope": scope},
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
    assert app["people"] == 2
    assert app["last_used_at"] is not None
    assert app["revoked_for_me"] is False
    assert app["revoked_for_everyone"] is False


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
        assert resp.json() == {"revoked": True, "keycloak_failures": 0}
        (row,) = await _rows(env, org_id)
        assert row.user_id == await _user_id(env, me)
        assert row.client_id == "claude"
        assert row.revoked_by == await _user_id(env, me)
        # My consent only; the Keycloak user id is my `external_subject`.
        assert env.kc.deleted == [(me, "claude")]
        (event,) = await _events(env, org_id, "mcp.app_revoked")
        assert event.actor_id == await _user_id(env, me)
        assert event.meta == {"client_id": "claude", "scope": "me"}
        assert await _events(env, org_id, "mcp.app_revoke_incomplete") == []

        assert await _initialize(env, _old_token(env, slug, me)) == 401
        assert await _initialize(env, _old_token(env, slug, colleague)) == 200

    mine = (await _connections(env, org_id, me)).json()["apps"][0]
    theirs = (await _connections(env, org_id, colleague)).json()["apps"][0]
    assert (mine["revoked_for_me"], mine["revoked_for_everyone"]) == (True, False)
    assert (theirs["revoked_for_me"], theirs["revoked_for_everyone"]) == (False, False)


# --------------------------------------------------------------------------- #
# 3-4. Revoke for everyone
# --------------------------------------------------------------------------- #


async def test_revoke_for_everyone_without_orgs_manage_is_403_and_writes_nothing(
    env: Any,
) -> None:
    org_id, _ = await _org(env)
    tech = await env.api.create_user(org_id, "technician")
    await _grant(env, org_id, tech, "mcp.use")

    resp = await _revoke(env, org_id, tech, "everyone")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "requires orgs.manage"
    assert await _rows(env, org_id) == []
    assert await _events(env, org_id, "mcp.app_revoked") == []
    assert env.kc.deleted == []
    # Non-vacuity: the same person may revoke for themselves.
    assert (await _revoke(env, org_id, tech, "me")).status_code == 200


async def test_revoke_for_everyone_cuts_off_the_whole_org_and_no_other(env: Any) -> None:
    org_id, slug = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    colleague = await env.api.create_user(org_id, "org-admin")
    other_id, other_slug = await _org(env)
    stranger = await env.api.create_user(other_id, "org-admin")
    async with running_mcp(env.api):
        resp = await _revoke(env, org_id, admin, "everyone")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"revoked": True, "keycloak_failures": 0}
        (row,) = await _rows(env, org_id)
        assert row.user_id is None
        assert row.revoked_by == await _user_id(env, admin)
        assert await _rows(env, other_id) == []
        # Every person of THIS org, and nobody of the other.
        assert sorted(env.kc.deleted) == sorted([(admin, "claude"), (colleague, "claude")])
        # One admin token and one client for the whole run, not one per person.
        assert env.kc.token_requests == 1
        assert env.kc.clients == 1
        (event,) = await _events(env, org_id, "mcp.app_revoked")
        assert event.meta == {"client_id": "claude", "scope": "everyone"}

        assert await _initialize(env, _old_token(env, slug, admin)) == 401
        assert await _initialize(env, _old_token(env, slug, colleague)) == 401
        # Registered apps are realm-wide: the same `azp` in another org is untouched.
        assert await _initialize(env, _old_token(env, other_slug, stranger)) == 200


async def test_a_revocation_shows_as_revoked_for_everyone(env: Any) -> None:
    org_id, slug = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    async with running_mcp(env.api):
        await _call_a_tool(env, slug, admin)
    assert (await _revoke(env, org_id, admin, "everyone")).status_code == 200
    (app,) = (await _connections(env, org_id, admin)).json()["apps"]
    assert app["revoked_for_everyone"] is True


# --------------------------------------------------------------------------- #
# 5. Keycloak down, or not configured
# --------------------------------------------------------------------------- #


async def test_keycloak_down_still_writes_the_revocation(env: Any) -> None:
    org_id, slug = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    env.kc.raise_connect = True

    resp = await _revoke(env, org_id, me, "me")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True, "keycloak_failures": 1}
    (row,) = await _rows(env, org_id)
    assert row.user_id == await _user_id(env, me)
    assert len(await _events(env, org_id, "mcp.app_revoked")) == 1
    # What Keycloak did not do is on the record too, not only in the response.
    (incomplete,) = await _events(env, org_id, "mcp.app_revoke_incomplete")
    assert incomplete.actor_id == await _user_id(env, me)
    assert incomplete.meta == {"client_id": "claude", "scope": "me", "keycloak_failures": 1}
    async with running_mcp(env.api):
        assert await _initialize(env, _old_token(env, slug, me)) == 401


async def test_a_keycloak_refusal_counts_as_a_failure(env: Any) -> None:
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    await env.api.create_user(org_id, "org-admin")
    env.kc.delete_status = 403
    resp = await _revoke(env, org_id, admin, "everyone")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True, "keycloak_failures": 2}
    (incomplete,) = await _events(env, org_id, "mcp.app_revoke_incomplete")
    assert incomplete.meta == {
        "client_id": "claude",
        "scope": "everyone",
        "keycloak_failures": 2,
    }


async def test_without_a_provisioning_client_every_person_is_a_failure(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No provisioning client means no consent was removed, so the app's
    refresh token can still mint access tokens: that is a failure to report,
    not a quiet success."""
    monkeypatch.setattr(
        KeycloakAdminConfig, "from_settings", classmethod(lambda cls, settings: None)
    )
    org_id, _ = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, me, "me")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True, "keycloak_failures": 1}
    assert len(await _rows(env, org_id)) == 1
    assert env.kc.deleted == []


async def test_consent_removal_is_bounded_in_concurrency_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large organization never has more than `KEYCLOAK_CONCURRENCY` calls in
    flight, and a wedged Keycloak costs the request the budget, not one
    timeout per person. Everyone not reached inside it is a failure."""
    from app.mcp import api as mcp_api

    in_flight = 0
    peak = 0
    reached: list[str] = []

    async def slow_revoke(
        config: Any, *, user_id: str, client_id: str, connection: Any = None
    ) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            if user_id.startswith("fast"):
                reached.append(user_id)
                return
            await anyio.sleep(60)
        finally:
            in_flight -= 1

    class OneConnection:
        opened = 0

        def __init__(self, config: Any) -> None:
            pass

        async def __aenter__(self) -> "OneConnection":
            OneConnection.opened += 1
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(mcp_api, "revoke_consent", slow_revoke)
    monkeypatch.setattr(mcp_api, "KeycloakAdminConnection", OneConnection)
    monkeypatch.setattr(mcp_api, "KEYCLOAK_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(
        KeycloakAdminConfig, "from_settings", classmethod(lambda cls, settings: CONFIG)
    )
    subjects = [f"fast-{i}" for i in range(10)] + [f"slow-{i}" for i in range(40)]

    started = time.monotonic()
    failures = await mcp_api.remove_consents(
        SimpleNamespace(), org_id=uuid4(), subjects=subjects, client_id="claude"
    )

    assert time.monotonic() - started < 5
    assert peak == mcp_api.KEYCLOAK_CONCURRENCY
    assert len(reached) == 10
    assert failures == 40
    assert OneConnection.opened == 1


# --------------------------------------------------------------------------- #
# The request's shape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dots", ["%2E%2E", "%2e%2e", ".%2E", "%2E.", "%2E", "..x", ".hidden"])
async def test_a_dot_segment_client_id_is_422_and_reaches_nothing(env: Any, dots: str) -> None:
    """THE CRITICAL ONE. `..` survives `quote(..., safe="")` and httpx resolves
    `.../users/{sub}/consents/..` to `.../users/{sub}` -- a DELETE of the
    person's whole Keycloak identity, for everyone in the org under
    scope "everyone". Refused at the route, before anything is written."""
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, admin, "everyone", client_id=dots)
    assert resp.status_code == 422, resp.text
    assert await _rows(env, org_id) == []
    assert await _events(env, org_id, "mcp.app_revoked") == []
    assert env.kc.requests == []


async def test_a_bare_dot_reaches_nothing(env: Any) -> None:
    """A literal `.` is resolved away by the client before it is sent, so it
    names no route; nothing is written either way."""
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, admin, "everyone", client_id=".")
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
async def test_the_platforms_own_clients_cannot_be_revoked(
    env: Any, first_party: str
) -> None:
    """Revoking the console's own client for everyone would sign the whole
    organization out of the console; it is not an AI app."""
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, admin, "everyone", client_id=first_party)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == (
        f"{first_party} is part of the platform, not an AI app, and cannot be revoked"
    )
    assert await _rows(env, org_id) == []
    assert await _events(env, org_id, "mcp.app_revoked") == []
    assert env.kc.requests == []


async def test_the_first_party_refusal_is_in_the_persons_language(env: Any) -> None:
    org_id, _ = await _org(env)
    admin = await env.api.create_user(org_id, "org-admin")
    resp = await env.api.client.post(
        "/api/v1/mcp/connections/it-suite-web/revoke",
        headers={**await env.api.headers(org_id, admin), "Accept-Language": "es"},
        json={"scope": "me"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == (
        "it-suite-web forma parte de la plataforma, no es una aplicación de IA, y no se "
        "puede revocar"
    )


async def test_a_failed_incomplete_audit_never_fails_a_committed_revoke(
    env: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The revocation row and `mcp.app_revoked` are committed before Keycloak
    is called. If recording what Keycloak did not do then fails, the person
    is still told the truth -- revoked, with the failure count -- not a 500
    that reads as "nothing happened"."""
    from app.core.audit import audit_service

    real_record = audit_service.record

    async def flaky_record(session: Any, **kw: Any) -> Any:
        if kw.get("action") == "mcp.app_revoke_incomplete":
            raise OSError("database went away")
        return await real_record(session, **kw)

    monkeypatch.setattr(audit_service, "record", flaky_record)
    org_id, _ = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    env.kc.raise_connect = True

    resp = await _revoke(env, org_id, me, "me")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True, "keycloak_failures": 1}
    assert len(await _rows(env, org_id)) == 1
    assert len(await _events(env, org_id, "mcp.app_revoked")) == 1
    assert await _events(env, org_id, "mcp.app_revoke_incomplete") == []
    (warning,) = [
        r for r in caplog.records if r.getMessage().startswith("could not record that")
    ]
    assert warning.client_id == "claude"  # type: ignore[attr-defined]
    assert warning.org_id == org_id  # type: ignore[attr-defined]


@pytest.mark.parametrize("bad", ["has space", "a+b", "a@b", "x" * 256, "ü"])
async def test_a_malformed_client_id_is_422_and_writes_nothing(env: Any, bad: str) -> None:
    org_id, _ = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, me, "me", client_id=bad)
    assert resp.status_code == 422, resp.text
    assert await _rows(env, org_id) == []
    assert env.kc.deleted == []


async def test_a_client_id_cannot_add_a_path_segment(env: Any) -> None:
    """An encoded slash is decoded by the router before matching, so it names
    no route at all; either way nothing is written and Keycloak is not asked."""
    org_id, _ = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    resp = await _revoke(env, org_id, me, "me", client_id="a%2F..%2Fb")
    assert resp.status_code in (404, 422), resp.text
    assert await _rows(env, org_id) == []
    assert env.kc.deleted == []


@pytest.mark.parametrize("body", [{"scope": "all"}, {}, {"scope": "me", "user_id": "x"}])
async def test_a_malformed_body_is_422(env: Any, body: dict[str, Any]) -> None:
    org_id, _ = await _org(env)
    me = await env.api.create_user(org_id, "org-admin")
    resp = await env.api.client.post(
        "/api/v1/mcp/connections/claude/revoke",
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
