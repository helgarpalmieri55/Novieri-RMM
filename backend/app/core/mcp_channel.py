"""The MCP channel: how the API knows a request came from the platform's own MCP server.

AN ASGI SCOPE EXTENSION, NEVER A HEADER. `app/mcp/server.py` calls routes
in-process through a wrapper that adds `scope["extensions"][EXTENSION_KEY]`; a
network request reaches the app through uvicorn and cannot set a scope key. So
a client cannot claim to be "not MCP" (its token has only the MCP audience) and
cannot claim to be MCP without coming through `/mcp`.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

EXTENSION_KEY = "itms.mcp"

#: Never reachable over MCP, whatever the person holds (spec §5.1; the in-app AI's
#: `_FORBIDDEN_GATES`). Invariant 3 is not amended.
MCP_FORBIDDEN_CODES: frozenset[str] = frozenset(
    {"credentials.read", "credentials.checkout", "credentials.manage", "licenses.keys.read"}
)


@dataclass(frozen=True)
class McpCall:
    azp: str
    tool: str
    #: `"METHOD /api/v1/path/{template}"` of the one route this call is for, or
    #: "" for the `/mcp` gate's own admission marker, which calls no route. A
    #: marked request with a route is refused unless Starlette would dispatch
    #: it to exactly that route (`app.mcp.guard`).
    route: str = ""


mcp_call_var: ContextVar[McpCall | None] = ContextVar("mcp_call", default=None)


@contextmanager
def use_mcp_call(call: McpCall) -> Iterator[None]:
    token = mcp_call_var.set(call)
    try:
        yield
    finally:
        mcp_call_var.reset(token)


def mcp_call_from_scope(scope: Mapping[str, Any]) -> McpCall | None:
    marker = (scope.get("extensions") or {}).get(EXTENSION_KEY)
    if marker is None:
        return None
    if not isinstance(marker, Mapping):
        raise ValueError("malformed MCP scope marker")  # i18n-exempt: becomes a 401
    azp, tool, route = marker.get("azp"), marker.get("tool", ""), marker.get("route", "")
    if not isinstance(azp, str) or not isinstance(tool, str) or not isinstance(route, str):
        raise ValueError("malformed MCP scope marker")  # i18n-exempt: becomes a 401
    return McpCall(azp=azp, tool=tool, route=route)


def mcp_resource(settings: Any) -> str | None:
    base = (getattr(settings, "public_console_url", None) or "").strip().rstrip("/")
    return f"{base}/mcp" if base else None
