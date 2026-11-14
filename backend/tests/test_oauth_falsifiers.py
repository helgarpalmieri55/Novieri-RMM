"""Falsifier 6 of autenticación propia design §10, end to end over the real
routes (plan 2026-11-06, Task 12), and an AI app connected the whole way.

§10.6: "Reutilizar un refresh token revoca su familia; un token MCP con otra
audiencia se rechaza; el token interno no vale en rutas públicas." Each clause
is a test that goes red when its protection goes:

- drop the family check from `grants.refresh_grant`, and the reused refresh
  token mints a token; drop the `family_is_live` block from `deps.admit`, and
  the family's newest access token still works;
- drop the exact-audience check from `access.verify_access_token`, and a token
  of ours for two audiences is admitted;
- let the MCP door read the assistant's internal token, and it is admitted;
  let `/oauth/token` take it, and the refusal changes. `/mcp` refuses it in
  three independent places, so no single change turns that clause red (the
  task report says which).

And the whole way, as Claude does it -- register (or be known by a client
metadata document, with no registration opt-in: operator decision C2), consent
in the console, exchange the code, call a tool -- by a technician the platform
invited, whose subject no identity provider issued (phase 1's D5, closed by
this phase).
"""

from typing import Any
from uuid import uuid4

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import cimd
from app.models import AuditEvent
from app.modules.user_admin.service import LOCAL_SUBJECT_PREFIX
from tests.mcp_support import (
    marked_client,
    mcp_claims,
    mcp_client,
    mcp_session,
    running_mcp,
    signed_mcp_token,
)
from tests.oauth_support import (
    BASE,
    CLAUDE_CALLBACK,
    RESOURCE,
    connect,
    oauth_http,
    oauth_org,
    register,
    token_request,
)
from tests.session_support import user_id_of

DOCUMENT_URL = "https://claude.example/oauth/client-metadata.json"
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


async def _initialize(env: Any, authorization: str) -> int:
    async with mcp_client(env) as client:
        resp = await client.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": authorization}
        )
    return resp.status_code


async def _refresh(env: Any, client_id: str, refresh_token: str) -> httpx.Response:
    return await token_request(
        env, grant_type="refresh_token", refresh_token=refresh_token, client_id=client_id
    )


async def _tool_called(env: Any, org_id: Any) -> AuditEvent:
    async with AsyncSession(env.admin_engine) as session:
        (event,) = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == "mcp.tool_called"
                    )
                )
            )
            .scalars()
            .all()
        )
    return event


async def test_reusing_a_refresh_token_ends_its_family_and_what_it_issued(api_env) -> None:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    first = await connect(api_env, org_id, sub)
    async with running_mcp(api_env):
        assert await _initialize(api_env, f"Bearer {first['access_token']}") == 200
        rotated = await _refresh(api_env, first["client_id"], first["refresh_token"])
        assert rotated.status_code == 200, rotated.text
        second = rotated.json()
        assert await _initialize(api_env, f"Bearer {second['access_token']}") == 200

        reused = await _refresh(api_env, first["client_id"], first["refresh_token"])

        assert (reused.status_code, reused.json()["error"]) == (400, "invalid_grant")
        # The whole family ended: its newest access token stops at the next
        # call, not in ten minutes (plan D6) ...
        assert await _initialize(api_env, f"Bearer {second['access_token']}") == 401
    # ... and its newest refresh token mints nothing.
    after = await _refresh(api_env, first["client_id"], second["refresh_token"])
    assert (after.status_code, after.json()["error"]) == (400, "invalid_grant")


@pytest.mark.parametrize(
    "aud",
    [BASE, [RESOURCE, BASE], "https://elsewhere.example/mcp"],
    ids=["the-console", "two-audiences", "another-server"],
)
async def test_an_mcp_token_for_another_audience_is_refused(api_env, aud: Any) -> None:
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    issued = await connect(api_env, org_id, sub)
    claims = mcp_claims(issued["access_token"])
    claims["aud"] = aud
    forged = await signed_mcp_token(claims)
    async with running_mcp(api_env):
        assert await _initialize(api_env, f"Bearer {forged}") == 401
        # Non-vacuity: the token the server issued is admitted.
        assert await _initialize(api_env, f"Bearer {issued['access_token']}") == 200
    async with marked_client(api_env, azp=issued["client_id"]) as client:
        me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}"})
    assert me.status_code == 401


async def test_the_internal_token_is_nothing_on_a_public_route(api_env) -> None:
    """The assistant's internal token (design §5.6) is admitted only on the
    assistant's own in-process call, which says so (`X-ITMS-Loopback`). On
    `/mcp`, on the OAuth server and on an MCP call it is nothing. The same
    token sent from the network WITH that header is
    `tests/test_loopback.py::test_the_token_from_the_network_is_refused`'s
    (A3-66); this one holds the routes it never belongs on."""
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin")
    app = await register(api_env)
    bearer = await api_env.loopback_bearer(org_id, sub)

    async with running_mcp(api_env):
        mcp = await _initialize(api_env, bearer)
    async with oauth_http(api_env) as client:
        token = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "x",
                "client_id": app["client_id"],
            },
            headers={"Authorization": bearer},
        )
    async with marked_client(api_env, azp=app["client_id"]) as client:
        marked = await client.get("/api/v1/me", headers={"Authorization": bearer})
    unmarked = await api_env.client.get("/api/v1/me", headers={"Authorization": bearer})

    assert mcp == 401
    assert (token.status_code, token.json()["error"]) == (401, "invalid_client")
    assert (marked.status_code, marked.json()["detail"]) == (401, "this access token was refused")
    assert unmarked.status_code == 401


async def test_an_invited_technician_connects_an_ai_app_the_whole_way(api_env) -> None:
    """Phase 1 left an invited technician -- whose subject no provider issued
    (`LOCAL_SUBJECT_PREFIX`) -- unable to connect an AI app until this phase
    (its D5). Registered, approved in the console, exchanged, and a tool
    called: as that person, audited with the app's client id."""
    org_id, _ = await oauth_org(api_env)
    sub = await api_env.create_user(org_id, "org-admin", sub=f"{LOCAL_SUBJECT_PREFIX}{uuid4()}")
    issued = await connect(api_env, org_id, sub)

    async with running_mcp(api_env), mcp_session(api_env, issued["access_token"]) as session:
        result = await session.call_tool("me_get", {})

    assert result.is_error is False, result
    event = await _tool_called(api_env, org_id)
    assert event.actor_id == await user_id_of(api_env, sub)
    assert event.meta["app"] == issued["client_id"]
    assert issued["client_id"].startswith("itms_")


async def test_an_app_known_by_its_metadata_document_needs_no_registration_opt_in(
    api_env, monkeypatch
) -> None:
    """Operator decision C2 (2026-11-06): with MCP on, an app that publishes a
    client metadata document connects with `ITS_MCP_REGISTRATION` unset --
    and the same deployment refuses a registration."""

    async def document(client_id: str) -> cimd.ClientDocument:
        assert client_id == DOCUMENT_URL
        return cimd.ClientDocument(
            client_id=DOCUMENT_URL,
            client_name="Claude",
            redirect_uris=(CLAUDE_CALLBACK,),
            cache_for=cimd.MIN_CACHE,
        )

    monkeypatch.setattr(cimd, "fetch_document", document)
    org_id, _ = await oauth_org(api_env)
    api_env.settings.mcp_registration = ""
    sub = await api_env.create_user(org_id, "org-admin")
    async with oauth_http(api_env) as client:
        registration = await client.post(
            "/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]}
        )
    assert (registration.status_code, registration.json()["error"]) == (403, "access_denied")

    issued = await connect(api_env, org_id, sub, client_id=DOCUMENT_URL)
    async with running_mcp(api_env), mcp_session(api_env, issued["access_token"]) as session:
        result = await session.call_tool("me_get", {})

    assert result.is_error is False, result
    assert (await _tool_called(api_env, org_id)).meta["app"] == DOCUMENT_URL
