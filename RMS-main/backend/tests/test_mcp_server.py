"""`/mcp` over Streamable HTTP, behind the OAuth gate (MCP server plan, Task 7).

The only door an external AI app has. What is pinned here:

- with `ITS_PUBLIC_CONSOLE_URL` unset there is no MCP resource, and both `/mcp`
  and its protected-resource metadata say so with a 503;
- the metadata (RFC 9728) names the resource and the realm, at both paths;
- a request with no token, or a token that is refused, gets 401 carrying
  `WWW-Authenticate: Bearer resource_metadata=...` -- the one thing an OAuth
  client needs to start the flow, and nothing else;
- a valid MCP token is then held to the org switch (403), `mcp.use` (403) and
  `mcp_revocation` (401, so the client signs in again), in that order, and the
  refusals are in the request's language;
- what passes reaches the SDK with the principal on the scope, at `/mcp` and
  `/mcp/` alike, with no redirect.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import anyio
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Organization, User
from app.models.mcp import McpRevocation
from tests.mcp_support import MCP_AUD, mcp_client, mcp_token, running_mcp

METADATA_URL = "https://itms.example.com/.well-known/oauth-protected-resource/mcp"
CHALLENGE = f'Bearer resource_metadata="{METADATA_URL}"'
REFUSED_CHALLENGE = f'Bearer resource_metadata="{METADATA_URL}", error="invalid_token"'

UNSET_EN = "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set."
SWITCH_OFF_EN = (
    "MCP is turned off for this organization. An org-admin turns it on in "
    "Settings → AI connections."
)
SWITCH_OFF_ES = (
    "MCP está desactivado para esta organización. Un administrador de la organización lo "
    "activa en Configuración → Conexiones de IA."
)
REVOKED_EN = "this AI app's access was revoked; connect it again to continue"
REVOKED_ES = "se revocó el acceso de esta aplicación de IA; vuelve a conectarla para continuar"

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
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@pytest.fixture()
async def mcp_env(api_env):
    """`api_env` with `ITS_PUBLIC_CONSOLE_URL` set, so the MCP resource is
    `https://itms.example.com/mcp` (as in `tests/test_mcp_channel_api.py`)."""
    api_env.settings.public_console_url = "https://itms.example.com"
    return api_env


def _bearer(token: str, **extra: str) -> dict[str, str]:
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}", **extra}


async def _org(env: Any, *, mcp_enabled: bool) -> tuple[UUID, str]:
    org_id = await env.create_org("mcp")
    if mcp_enabled:
        await _set_switch(env, org_id, True)
    return org_id, await env.org_slug(org_id)


async def _set_switch(env: Any, org_id: UUID, on: bool) -> None:
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(Organization)
            .where(Organization.id == org_id)
            .values(settings={"mcp_enabled": on})
        )
        await session.commit()


async def _user_id(env: Any, sub: str) -> UUID:
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _revoke(
    env: Any, org_id: UUID, *, client_id: str, at: datetime, user_id: UUID | None = None
) -> None:
    async with AsyncSession(env.admin_engine) as session:
        session.add(
            McpRevocation(org_id=org_id, user_id=user_id, client_id=client_id, revoked_at=at)
        )
        await session.commit()


def _token_issued_at(env: Any, slug: str, sub: str, iat: int, azp: str = "claude") -> str:
    return env.idp.token(
        sub, slug, aud=MCP_AUD, azp=azp, email=f"{sub}@test.local", extra={"iat": iat}
    )


# --------------------------------------------------------------------------- #
# 1-2. No resource without a console URL; the metadata when there is one
# --------------------------------------------------------------------------- #


async def test_without_a_console_url_mcp_and_its_metadata_are_503(api_env) -> None:
    assert api_env.settings.public_console_url is None
    for path in (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ):
        resp = await api_env.client.get(path)
        assert resp.status_code == 503, path
        assert resp.json()["detail"] == UNSET_EN
    async with mcp_client(api_env) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert resp.status_code == 503
    assert resp.json()["detail"] == UNSET_EN


async def test_the_protected_resource_metadata_is_served_at_both_paths(mcp_env) -> None:
    for path in (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ):
        resp = await mcp_env.client.get(path)
        assert resp.status_code == 200, path
        assert resp.json() == {
            "resource": "https://itms.example.com/mcp",
            "authorization_servers": ["https://idp.test/realms/it-suite"],
            "scopes_supported": ["mcp"],
            "bearer_methods_supported": ["header"],
        }
        assert resp.json()["authorization_servers"] == [mcp_env.settings.oidc_issuer]


async def test_a_trailing_slash_on_the_console_url_does_not_double_up(mcp_env) -> None:
    mcp_env.settings.public_console_url = "https://itms.example.com/"
    resp = await mcp_env.client.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.json()["resource"] == "https://itms.example.com/mcp"
    async with mcp_client(mcp_env) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert resp.headers["www-authenticate"] == CHALLENGE


# --------------------------------------------------------------------------- #
# 3-4. 401 and the challenge that starts the OAuth flow
# --------------------------------------------------------------------------- #


async def test_no_token_is_401_with_the_resource_metadata_challenge(mcp_env) -> None:
    async with mcp_client(mcp_env) as client:
        for path in ("/mcp", "/mcp/"):
            resp = await client.post(path, json=INITIALIZE, headers=MCP_HEADERS)
            # Not a 307 to `/mcp/`: an MCP client POSTs to the exact URL, and a
            # redirected POST is the kind of thing clients get wrong.
            assert resp.status_code == 401, path
            assert resp.headers["www-authenticate"] == CHALLENGE


async def test_a_non_bearer_authorization_is_treated_as_no_token(mcp_env) -> None:
    async with mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp",
            json=INITIALIZE,
            headers={**MCP_HEADERS, "Authorization": "Basic dXNlcjpwYXNz"},
        )
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == CHALLENGE


async def test_a_console_token_is_401(mcp_env) -> None:
    org_id, _ = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    console = await mcp_env.headers(org_id, sub)
    async with mcp_client(mcp_env) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **console})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE


async def test_a_token_with_both_audiences_is_401(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    token = mcp_env.idp.token(
        sub,
        slug,
        aud=MCP_AUD,
        azp="claude",
        email=f"{sub}@test.local",
        extra={"aud": [MCP_AUD, "it-suite"]},
    )
    async with mcp_client(mcp_env) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=_bearer(token))
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE


async def test_a_token_that_names_no_app_is_401(mcp_env) -> None:
    """Revocation is keyed on `azp`. A token that names no app could not be
    revoked before it expired, so it is not admitted at all."""
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    token = mcp_env.idp.token(sub, slug, aud=MCP_AUD, azp=None, email=f"{sub}@test.local")
    async with mcp_client(mcp_env) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=_bearer(token))
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE


async def test_a_token_without_an_org_claim_is_401_in_multi_tenant_mode(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    # A default org would admit a claimless token in single-tenant mode; it is
    # multi-tenant mode, and nothing else, that refuses it here.
    mcp_env.settings.default_org_slug = slug
    mcp_env.settings.multi_tenant = True
    token = mcp_env.idp.token(sub, None, aud=MCP_AUD, azp="claude", email=f"{sub}@test.local")
    async with mcp_client(mcp_env) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=_bearer(token))
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE


# --------------------------------------------------------------------------- #
# 5-6. The org switch, then `mcp.use`
# --------------------------------------------------------------------------- #


async def test_the_org_switch_off_is_403(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=False)
    sub = await mcp_env.create_user(org_id, "org-admin")
    async with mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(mcp_token(mcp_env, slug, sub))
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == SWITCH_OFF_EN


async def test_turning_the_switch_off_stops_the_next_request(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    token = mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        first = await client.post("/mcp", json=INITIALIZE, headers=_bearer(token))
        assert first.status_code == 200, first.text
        await _set_switch(mcp_env, org_id, False)
        resp = await client.post("/mcp", json=INITIALIZE, headers=_bearer(token))
    assert resp.status_code == 403


async def test_without_mcp_use_is_403(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "technician")
    async with mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(mcp_token(mcp_env, slug, sub))
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "requires mcp.use"


async def test_the_403s_are_in_the_requests_language(mcp_env) -> None:
    off_id, off_slug = await _org(mcp_env, mcp_enabled=False)
    off_sub = await mcp_env.create_user(off_id, "org-admin")
    on_id, on_slug = await _org(mcp_env, mcp_enabled=True)
    tech = await mcp_env.create_user(on_id, "technician")
    es = {"Accept-Language": "es"}
    async with mcp_client(mcp_env) as client:
        off = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(mcp_token(mcp_env, off_slug, off_sub), **es)
        )
        denied = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(mcp_token(mcp_env, on_slug, tech), **es)
        )
    assert off.status_code == 403
    assert off.json()["detail"] == SWITCH_OFF_ES
    assert denied.status_code == 403
    assert denied.json()["detail"] == "requiere mcp.use"


# --------------------------------------------------------------------------- #
# 7. Revocation
# --------------------------------------------------------------------------- #


async def test_an_org_wide_revocation_refuses_earlier_tokens_and_admits_later_ones(
    mcp_env,
) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    now = int(time.time())
    await _revoke(
        mcp_env, org_id, client_id="claude", at=datetime.fromtimestamp(now - 30, UTC)
    )
    before = _token_issued_at(mcp_env, slug, sub, now - 60)
    after = _token_issued_at(mcp_env, slug, sub, now)
    other_app = _token_issued_at(mcp_env, slug, sub, now - 60, azp="another-app")
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        refused = await client.post("/mcp", json=INITIALIZE, headers=_bearer(before))
        refused_es = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(before, **{"Accept-Language": "es"})
        )
        reconnected = await client.post("/mcp", json=INITIALIZE, headers=_bearer(after))
        unaffected = await client.post("/mcp", json=INITIALIZE, headers=_bearer(other_app))
    # 401, not 403: the client should run the OAuth flow again, and the person
    # reconnect deliberately.
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == REFUSED_CHALLENGE
    assert refused.json()["detail"] == REVOKED_EN
    assert refused_es.json()["detail"] == REVOKED_ES
    assert reconnected.status_code == 200, reconnected.text
    assert unaffected.status_code == 200, unaffected.text


async def test_a_per_user_revocation_affects_only_that_user(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    revoked = await mcp_env.create_user(org_id, "org-admin")
    colleague = await mcp_env.create_user(org_id, "org-admin")
    now = int(time.time())
    await _revoke(
        mcp_env,
        org_id,
        client_id="claude",
        at=datetime.fromtimestamp(now - 30, UTC),
        user_id=await _user_id(mcp_env, revoked),
    )
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        mine = await client.post(
            "/mcp",
            json=INITIALIZE,
            headers=_bearer(_token_issued_at(mcp_env, slug, revoked, now - 60)),
        )
        theirs = await client.post(
            "/mcp",
            json=INITIALIZE,
            headers=_bearer(_token_issued_at(mcp_env, slug, colleague, now - 60)),
        )
    assert mine.status_code == 401
    assert mine.json()["detail"] == REVOKED_EN
    assert theirs.status_code == 200, theirs.text


async def test_one_organizations_revocation_never_reaches_another(mcp_env) -> None:
    """Registered apps are realm-wide, so the same `azp` is in use in every
    organization. Org A revoking "claude" says nothing about org B's users."""
    a_id, a_slug = await _org(mcp_env, mcp_enabled=True)
    b_id, b_slug = await _org(mcp_env, mcp_enabled=True)
    a_user = await mcp_env.create_user(a_id, "org-admin")
    b_user = await mcp_env.create_user(b_id, "org-admin")
    now = int(time.time())
    later = datetime.fromtimestamp(now, UTC) + timedelta(hours=1)
    await _revoke(mcp_env, a_id, client_id="claude", at=later)
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        in_a = await client.post(
            "/mcp",
            json=INITIALIZE,
            headers=_bearer(_token_issued_at(mcp_env, a_slug, a_user, now - 60)),
        )
        in_b = await client.post(
            "/mcp",
            json=INITIALIZE,
            headers=_bearer(_token_issued_at(mcp_env, b_slug, b_user, now - 60)),
        )
    # Non-vacuity: the row does refuse in the org that wrote it.
    assert in_a.status_code == 401
    assert in_b.status_code == 200, in_b.text


# --------------------------------------------------------------------------- #
# Admission over MCP is audited as MCP
# --------------------------------------------------------------------------- #


async def test_a_first_seen_sign_in_over_mcp_is_audited_as_via_mcp(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = "sub-first-seen-over-mcp"
    async with mcp_client(mcp_env) as client:
        # No role yet, so the gate refuses -- after admission has provisioned
        # the person, which is the event under test.
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(mcp_token(mcp_env, slug, sub))
        )
    assert resp.status_code == 403
    async with AsyncSession(mcp_env.admin_engine) as session:
        events = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == "auth.login"
                    )
                )
            ).scalars()
        )
    assert len(events) == 1
    assert events[0].meta["via"] == "mcp"
    assert events[0].meta["mcp_app"] == "claude"


# --------------------------------------------------------------------------- #
# What passes reaches the SDK, with the principal
# --------------------------------------------------------------------------- #


async def test_an_admitted_request_reaches_the_sdk_at_both_paths(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    token = mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        for path in ("/mcp", "/mcp/"):
            resp = await client.post(path, json=INITIALIZE, headers=_bearer(token))
            assert resp.status_code == 200, (path, resp.text)
            assert resp.json()["result"]["serverInfo"]["name"] == "itms"


async def test_the_tool_handlers_read_the_principal_from_the_scope(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.server as server

    seen: list[Any] = []
    real = server.principal_from

    def spy(ctx: Any) -> Any:
        principal = real(ctx)
        seen.append(principal)
        return principal

    monkeypatch.setattr(server, "principal_from", spy)
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    token = mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp?areas=devices,,patch",
            json=LIST_TOOLS,
            headers=_bearer(token, **{"Accept-Language": "es"}),
        )
    assert resp.status_code == 200, resp.text
    # The listing honours the principal's areas (tools/list itself is pinned
    # in `tests/test_mcp_tools.py`).
    catalog = mcp_env.app.state.mcp_catalog
    listed = [t["name"] for t in resp.json()["result"]["tools"]]
    assert listed
    assert {catalog[name].area for name in listed} == {"devices", "patch"}
    assert len(seen) == 1
    principal = seen[0]
    assert principal.org_id == org_id
    assert principal.user_id == await _user_id(mcp_env, sub)
    assert principal.claims.azp == "claude"
    assert principal.bearer == f"Bearer {token}"
    assert principal.areas == frozenset({"devices", "patch"})
    assert principal.accept_language == "es"
    # The bearer token is never in a repr, so never in a log line or traceback.
    assert token not in repr(principal)


async def test_the_tool_handlers_read_the_principal_on_the_2026_wire_too(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK serves protocol 2026-07-28 on a separate, single-exchange path
    (no `initialize`). The principal must reach the handler there as well."""
    import app.mcp.server as server

    seen: list[Any] = []
    real = server.principal_from
    monkeypatch.setattr(server, "principal_from", lambda ctx: seen.append(real(ctx)) or seen[-1])
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    body = {
        **LIST_TOOLS,
        "params": {
            "_meta": {
                PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
                CLIENT_CAPABILITIES_META_KEY: {},
            }
        },
    }
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        resp = await client.post(
            "/mcp",
            json=body,
            headers=_bearer(
                mcp_token(mcp_env, slug, sub),
                **{"MCP-Protocol-Version": LATEST_PROTOCOL_VERSION, "Mcp-Method": "tools/list"},
            ),
        )
    assert resp.status_code == 200, resp.text
    assert "devices_list" in {t["name"] for t in resp.json()["result"]["tools"]}
    assert [p.org_id for p in seen] == [org_id]


async def test_only_post_reaches_the_sdk(mcp_env) -> None:
    """Stateless with JSON responses, a GET would open an event stream that
    never carries anything and never closes. Refused, with `Allow: POST`."""
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    token = mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        with anyio.fail_after(10):
            got = await client.get("/mcp", headers=_bearer(token))
            deleted = await client.delete("/mcp", headers=_bearer(token))
            anonymous = await client.get("/mcp")
    assert got.status_code == 405
    assert got.headers["allow"] == "POST"
    assert deleted.status_code == 405
    # Unauthenticated, the challenge comes first: that is how OAuth starts.
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == CHALLENGE


# --------------------------------------------------------------------------- #
# DNS rebinding: the gate answers only on the public host
# --------------------------------------------------------------------------- #


async def test_another_host_is_421_before_anything_else(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    async with running_mcp(mcp_env):
        resp = await mcp_env.client.post(  # Host: api.test
            "/mcp", json=INITIALIZE, headers=_bearer(mcp_token(mcp_env, slug, sub))
        )
    assert resp.status_code == 421


async def test_a_foreign_origin_is_403_and_the_consoles_own_is_admitted(mcp_env) -> None:
    org_id, slug = await _org(mcp_env, mcp_enabled=True)
    sub = await mcp_env.create_user(org_id, "org-admin")
    token = mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        foreign = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(token, Origin="https://evil.example")
        )
        own = await client.post(
            "/mcp", json=INITIALIZE, headers=_bearer(token, Origin="https://itms.example.com")
        )
    assert foreign.status_code == 403
    assert own.status_code == 200, own.text
