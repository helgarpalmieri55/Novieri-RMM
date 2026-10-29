"""Shared helpers for tests that exercise the MCP channel.

`marked_client` reaches the app the way `app/mcp/server.py` will: in-process,
through a wrapper that adds the ASGI scope marker. A network client cannot set
a scope key, which is the whole point of the marker being one.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.mcp_channel import EXTENSION_KEY
from app.models import Organization

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


def mcp_token(api_env, org_slug: str, sub: str, azp: str = "claude") -> str:
    return api_env.idp.token(sub, org_slug, aud=MCP_AUD, azp=azp, email=f"{sub}@test.local")


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

