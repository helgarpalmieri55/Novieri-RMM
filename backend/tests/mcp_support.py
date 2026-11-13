"""Shared helpers for tests that exercise the MCP channel.

`marked_client` reaches the app the way `app/mcp/server.py` will: in-process,
through a wrapper that adds the ASGI scope marker. A network client cannot set
a scope key, which is the whole point of the marker being one.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import httpx2
import jwt
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import keys
from app.core.auth.oauth import grants
from app.core.auth.oauth.access import mint_access_token
from app.core.auth.oauth.policy import ACCESS_TYP
from app.core.auth.oauth.store import oauth_session
from app.core.mcp_channel import EXTENSION_KEY, mcp_resource
from app.models import Organization
from app.models.oauth import OAuthClient
from tests.session_support import user_id_of

MCP_AUD = "https://itms.example.com/mcp"
#: The public console address the tests set as `ITS_PUBLIC_CONSOLE_URL`: the
#: MCP gate answers only there.
MCP_BASE = "https://itms.example.com"


def marked(app, azp: str = "claude", tool: str = "t"):
    async def wrapper(scope, receive, send):
        if scope["type"] == "http":
            scope = {
                **scope,
                "extensions": {
                    **(scope.get("extensions") or {}),
                    EXTENSION_KEY: {"azp": azp, "tool": tool},
                },
            }
        await app(scope, receive, send)

    return wrapper


def marked_client(api_env, azp: str = "claude", tool: str = "t") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=marked(api_env.app, azp, tool)),
        base_url="http://mcp.test",
    )


#: Anthropic: the hosted Claude apps "register exactly this redirect URI" [S33].
_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


async def mcp_app(api_env: Any, org_id: UUID, client_id: str = "claude") -> UUID:
    """The organization's AI app `client_id`, registered once -- what
    `POST /oauth/register` leaves behind -- as its `oauth_clients.id`."""
    now = datetime.now(UTC)
    async with oauth_session(org_id, api_env.app_engine) as session:
        found: UUID | None = (
            await session.execute(
                select(OAuthClient.id).where(
                    OAuthClient.org_id == org_id, OAuthClient.client_id == client_id
                )
            )
        ).scalar_one_or_none()
        if found is not None:
            return found
        row = OAuthClient(
            org_id=org_id,
            client_id=client_id,
            kind="registered",
            client_name=client_id,
            redirect_uris=[_CALLBACK],
            created_at=now,
            last_used_at=now,
        )
        session.add(row)
        await session.flush()
        return row.id


async def mcp_token(
    api_env: Any, org_slug: str, sub: str, azp: str = "claude", *, iat: int | None = None
) -> str:
    """What `/oauth/token` hands the AI app `azp` after `sub` approved it
    (autenticación propia design §5.3; plan 2026-11-06, Task 9): the app's
    registration, an open grant, and the ten-minute access token naming both,
    issued at `iat` (now when None). The consent screen and the code exchange
    are `tests/test_oauth_consent.py` and `tests/test_oauth_token.py`; this is
    their result, for the tests of what an MCP call may do."""
    async with AsyncSession(api_env.admin_engine) as session:
        org_id: UUID = (
            await session.execute(select(Organization.id).where(Organization.slug == org_slug))
        ).scalar_one()
    user_id = await user_id_of(api_env, sub)
    app_id = await mcp_app(api_env, org_id, azp)
    resource = mcp_resource(api_env.settings)
    assert resource is not None, "set ITS_PUBLIC_CONSOLE_URL before minting an MCP token"
    async with oauth_session(org_id, api_env.app_engine) as session:
        family, _ = await grants.open_family(
            session,
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=app_id,
            resource=resource,
            now=datetime.now(UTC),
        )
        family_id = family.id
    token, _ = await mint_access_token(
        settings=api_env.settings,
        user_id=user_id,
        org_id=org_id,
        client_id=azp,
        family_id=family_id,
        now=iat,
    )
    return token


def mcp_claims(token: str) -> dict[str, Any]:
    """A token's claims, unverified: where a test that changes one starts."""
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return claims


async def signed_mcp_token(claims: Mapping[str, Any]) -> str:
    """`claims` signed by the platform's own key as an MCP access token: for
    the tests of what the door checks beyond the signature."""
    return await keys.sign_jwt(claims, typ=ACCESS_TYP)


@asynccontextmanager
async def running_mcp(api_env) -> AsyncIterator[None]:
    """Run the MCP session manager for the test. `api_env` reaches the app
    through `httpx.ASGITransport`, which never runs the lifespan, so nothing
    else would; without it the SDK refuses every request that reaches it."""
    async with api_env.app.state.mcp.lifespan():
        yield


def mcp_client(api_env) -> httpx.AsyncClient:
    """A network-shaped client for `/mcp`: no scope marker, and the public
    host `ITS_PUBLIC_CONSOLE_URL` names, because the gate answers only there."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_env.app), base_url="https://itms.example.com"
    )


async def mcp_org(env: Any) -> tuple[UUID, str]:
    """A new organisation with MCP switched on, and its slug."""
    org_id = await env.create_org("mcp")
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(Organization)
            .where(Organization.id == org_id)
            .values(settings={"mcp_enabled": True})
        )
        await session.commit()
    return org_id, await env.org_slug(org_id)


@asynccontextmanager
async def mcp_session(
    env: Any,
    token: str,
    *,
    query: str = "",
    language: str | None = None,
    client: str | None = None,
) -> AsyncIterator[ClientSession]:
    """The SDK's own client, initialised, over an in-process transport.
    `client` is sent as `X-Client-Id` on every MCP request."""
    headers = {"Authorization": f"Bearer {token}"}
    if language is not None:
        headers["Accept-Language"] = language
    if client is not None:
        headers["X-Client-Id"] = client
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=env.app), base_url=MCP_BASE, headers=headers
    )
    async with (
        http,
        streamable_http_client(f"{MCP_BASE}/mcp{query}", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def tool_error_text(result: CallToolResult) -> str:
    """The one text block of a tool call that failed."""
    assert result.is_error is True, result
    assert result.structured_content is None
    assert len(result.content) == 1
    return result.content[0].text  # type: ignore[union-attr]

