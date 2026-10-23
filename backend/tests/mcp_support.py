"""Shared helpers for tests that exercise the MCP channel.

`marked_client` reaches the app the way `app/mcp/server.py` will: in-process,
through a wrapper that adds the ASGI scope marker. A network client cannot set
a scope key, which is the whole point of the marker being one.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.core.mcp_channel import EXTENSION_KEY

MCP_AUD = "https://itms.example.com/mcp"


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
