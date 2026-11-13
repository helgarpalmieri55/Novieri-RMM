"""The MCP door with the platform's own access token (autenticación propia
design §5.3, §6.5; plan 2026-11-06, Task 9).

What is pinned here:

- a token the platform issued after the person's consent is admitted at
  `/mcp` and on an in-process call, as that person, with their row's address;
- a Keycloak token is refused whatever its audience (§5.3: "otro token se
  rechaza"), and so is one of ours for any audience but the MCP resource;
- the grant the token names is read on every call (plan D6): ending it stops
  the next call, not the next token; another app's or another person's grant
  admits nobody;
- the channel marker must name the token's app;
- the door provisions nobody and writes nothing about the person;
- the grant admits the person, never their second factor: an MCP call
  never counts as a recent confirmation (phase 2's Task 14);
- with no https console there is no issuer, and the channel says so (503).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth import grants
from app.core.auth.oauth.access import verify_access_token
from app.core.auth.oauth.store import oauth_session
from app.models import AuditEvent, User
from tests.mcp_support import (
    MCP_AUD,
    MCP_BASE,
    marked_client,
    mcp_claims,
    mcp_client,
    mcp_org,
    mcp_token,
    running_mcp,
    signed_mcp_token,
)
from tests.session_support import refusal_code, sign_in, user_id_of

METADATA_URL = f"{MCP_BASE}/.well-known/oauth-protected-resource/mcp"
REFUSED_CHALLENGE = f'Bearer resource_metadata="{METADATA_URL}", error="invalid_token"'
REFUSED_EN = "this access token was refused"
REFUSED_ES = "se rechazó este token de acceso"
REVOKED_EN = "this AI app's access was revoked; connect it again to continue"
MARKER_EN = "the MCP channel marker is malformed"
NO_ISSUER_EN = (
    "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not an https address without a path."
)
CONFIRM_AGAIN = "confirm your second factor again to do this"
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


@pytest.fixture()
async def mcp_env(api_env):
    """`api_env` with the console the MCP gate answers at (`MCP_BASE`)."""
    api_env.settings.public_console_url = MCP_BASE
    return api_env


async def _admin(env: Any) -> tuple[UUID, str, str]:
    org_id, slug = await mcp_org(env)
    return org_id, slug, await env.create_user(org_id, "org-admin")


async def _initialize(env: Any, token: str, **extra: str) -> Any:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}", **extra}
    async with mcp_client(env) as client:
        return await client.post("/mcp", json=INITIALIZE, headers=headers)


async def _me(env: Any, token: str, *, azp: str = "claude") -> Any:
    """The in-process call a tool makes (`app/mcp/invoke.py`), marked for `azp`."""
    async with marked_client(env, azp=azp) as client:
        return await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})


async def _users(env: Any, org_id: UUID) -> int:
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(
                select(func.count()).select_from(User).where(User.org_id == org_id)
            )
        ).scalar_one()


async def _auth_events(env: Any, org_id: UUID) -> list[str]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent.action).where(
                        AuditEvent.org_id == org_id, AuditEvent.action.like("auth.%")
                    )
                )
            ).scalars()
        )


async def test_a_token_the_platform_issued_is_admitted_as_the_person(mcp_env) -> None:
    org_id, slug, sub = await _admin(mcp_env)
    token = await mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env):
        initialized = await _initialize(mcp_env, token)
    me = await _me(mcp_env, token)
    assert initialized.status_code == 200, initialized.text
    assert me.status_code == 200, me.text
    assert me.json()["user_id"] == str(await user_id_of(mcp_env, sub))
    assert me.json()["email"] == f"{sub}@test.local"


async def test_a_keycloak_token_is_refused_whatever_its_audience(mcp_env) -> None:
    """Both carry a `client_id` claim, so the gate's label is not what refuses
    them: the signature is not the platform's."""
    _, slug, sub = await _admin(mcp_env)
    for token in (
        mcp_env.idp.token(
            sub,
            slug,
            aud=MCP_AUD,
            azp="claude",
            email=f"{sub}@test.local",
            extra={"client_id": "claude"},
        ),
        mcp_env.idp.token(sub, slug, email=f"{sub}@test.local", extra={"client_id": "claude"}),
    ):
        resp = await _initialize(mcp_env, token)
        resp_es = await _initialize(mcp_env, token, **{"Accept-Language": "es"})
        me = await _me(mcp_env, token)
        assert resp.status_code == 401, resp.text
        assert resp.headers["www-authenticate"] == REFUSED_CHALLENGE
        assert resp.json()["detail"] == REFUSED_EN
        assert resp_es.json()["detail"] == REFUSED_ES
        assert (me.status_code, me.json()["detail"]) == (401, REFUSED_EN)


@pytest.mark.parametrize(
    "aud",
    [MCP_BASE, [MCP_AUD, MCP_BASE], "https://elsewhere.example/mcp"],
    ids=["the-console", "two-audiences", "another-server"],
)
async def test_one_of_our_tokens_for_another_audience_is_refused(mcp_env, aud: Any) -> None:
    _, slug, sub = await _admin(mcp_env)
    genuine = await mcp_token(mcp_env, slug, sub)
    claims = mcp_claims(genuine)
    claims["aud"] = aud
    async with running_mcp(mcp_env):
        refused = await _initialize(mcp_env, await signed_mcp_token(claims))
        # Non-vacuity: the same claims, re-signed unchanged, are admitted.
        control = await _initialize(mcp_env, await signed_mcp_token(mcp_claims(genuine)))
    assert refused.status_code == 401
    assert refused.json()["detail"] == REFUSED_EN
    assert control.status_code == 200, control.text


async def test_ending_the_grant_stops_the_next_call(mcp_env) -> None:
    org_id, slug, sub = await _admin(mcp_env)
    token = await mcp_token(mcp_env, slug, sub)
    family_id = UUID(mcp_claims(token)["fam"])
    async with running_mcp(mcp_env):
        before = await _initialize(mcp_env, token)
        async with oauth_session(org_id, mcp_env.app_engine) as session:
            assert await grants.revoke_family(
                session, family_id, "revoked_in_settings", datetime.now(UTC)
            )
        after = await _initialize(mcp_env, token)
    me = await _me(mcp_env, token)
    assert before.status_code == 200, before.text
    assert after.status_code == 401
    assert after.headers["www-authenticate"] == REFUSED_CHALLENGE
    assert after.json()["detail"] == REVOKED_EN
    assert (me.status_code, me.json()["detail"]) == (401, REVOKED_EN)
    # The token is still a genuine one: what ended is the grant it names.
    assert await verify_access_token(token, settings=mcp_env.settings) is not None


async def test_another_apps_or_another_persons_grant_admits_nobody(mcp_env) -> None:
    org_id, slug, sub = await _admin(mcp_env)
    colleague = await mcp_env.create_user(org_id, "org-admin")
    other_app = mcp_claims(await mcp_token(mcp_env, slug, sub, "cursor"))
    theirs = mcp_claims(await mcp_token(mcp_env, slug, colleague))
    for fam in (other_app["fam"], theirs["fam"]):
        claims = mcp_claims(await mcp_token(mcp_env, slug, sub))
        claims["fam"] = fam
        resp = await _initialize(mcp_env, await signed_mcp_token(claims))
        assert resp.status_code == 401
        assert resp.json()["detail"] == REVOKED_EN


async def test_the_channel_marker_must_name_the_tokens_app(mcp_env) -> None:
    _, slug, sub = await _admin(mcp_env)
    token = await mcp_token(mcp_env, slug, sub)
    other = await _me(mcp_env, token, azp="cursor")
    own = await _me(mcp_env, token)
    assert (other.status_code, other.json()["detail"]) == (401, MARKER_EN)
    assert own.status_code == 200, own.text


async def test_a_token_for_nobody_provisions_nobody(mcp_env) -> None:
    org_id, slug, sub = await _admin(mcp_env)
    claims = mcp_claims(await mcp_token(mcp_env, slug, sub))
    claims["sub"] = str(uuid4())
    token = await signed_mcp_token(claims)
    before = await _users(mcp_env, org_id)
    resp = await _initialize(mcp_env, token)
    me = await _me(mcp_env, token)
    assert (resp.status_code, resp.json()["detail"]) == (401, REFUSED_EN)
    assert (me.status_code, me.json()["detail"]) == (401, REFUSED_EN)
    assert await _users(mcp_env, org_id) == before
    assert await _auth_events(mcp_env, org_id) == []


async def test_an_mcp_call_writes_nothing_about_the_person(mcp_env) -> None:
    org_id, slug, sub = await _admin(mcp_env)
    token = await mcp_token(mcp_env, slug, sub)

    async def row() -> tuple[Any, ...]:
        async with AsyncSession(mcp_env.admin_engine) as session:
            user = (
                await session.execute(select(User).where(User.external_subject == sub))
            ).scalar_one()
            return (user.email, user.display_name, user.last_login_at, user.status)

    seen = await row()
    async with running_mcp(mcp_env):
        for _ in range(2):
            assert (await _initialize(mcp_env, token)).status_code == 200
    assert (await _me(mcp_env, token)).status_code == 200
    assert await row() == seen
    assert await _auth_events(mcp_env, org_id) == []


async def test_an_mcp_call_never_counts_as_a_recent_confirmation(mcp_env) -> None:
    """Phase 2's Task 14 (its plan's "Puntos de encuentro con la fase 3", 4):
    the grant admits the person, never their second factor. They confirmed it
    in the console a moment ago; over MCP, `require_step_up` -- the first
    question `factors/reset` asks, before it reads anyone -- still refuses.
    Give the MCP branch of `admit` a principal with a `step_up_at`, or the
    person's console session, and this call answers what the console's does."""
    org_id, slug, sub = await _admin(mcp_env)
    console = await sign_in(mcp_env, org_id, sub, step_up=True)
    token = await mcp_token(mcp_env, slug, sub)
    reset = f"/api/v1/users/{uuid4()}/factors/reset"
    async with marked_client(mcp_env) as client:
        over_mcp = await client.post(reset, headers={"Authorization": f"Bearer {token}"})
    in_console = await mcp_env.client.post(reset, headers=console.headers)
    assert (over_mcp.status_code, refusal_code(over_mcp)) == (403, "step_up_required")
    assert over_mcp.json()["detail"] == CONFIRM_AGAIN
    # Non-vacuity: the same person's console session gets past the question,
    # to the unknown person the path names.
    assert in_console.status_code == 404, in_console.text


async def test_without_an_https_console_there_is_no_issuer_and_mcp_is_503(mcp_env) -> None:
    _, slug, sub = await _admin(mcp_env)
    token = await mcp_token(mcp_env, slug, sub)
    mcp_env.settings.public_console_url = "http://itms.example.com"
    resp = await _initialize(mcp_env, token)
    me = await _me(mcp_env, token)
    assert (resp.status_code, resp.json()["detail"]) == (503, NO_ISSUER_EN)
    assert (me.status_code, me.json()["detail"]) == (503, NO_ISSUER_EN)
