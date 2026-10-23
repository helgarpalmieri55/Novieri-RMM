"""The outer refusal of `NEVER_EXPOSED` routes on the MCP channel.

A PURE-ASGI MIDDLEWARE, NOT A DEPENDENCY, because the refusal must not depend
on authentication: `POST /api/v1/setup/claim` is unauthenticated by necessity
and never runs `auth_context`. This layer looks at the request before routing,
and only when the scope carries the MCP marker (`app.core.mcp_channel`); an
unmarked request costs it one dict lookup and nothing else.

Registered INSIDE the locale middleware (`app/main.py`), so the 403 is in the
request's language. `auth_context` repeats the check by route identity
(`app.state.mcp_never_route_ids`) as a second, independent refusal.

A TOOL CALL REACHES EXACTLY ITS OWN ROUTE. A marker that names a route
(`McpCall.route`, set by `app/mcp/invoke.py`) is refused unless the router
would dispatch this request to that very route -- see `dispatched_route`.

IT ALSO BINDS THE CHANNEL, ONCE, FOR THE WHOLE REQUEST. A marked request that
passes is served inside `use_mcp_call(call)`, so every audit event written while
handling it carries `via: "mcp"` and the app (`app.core.audit`) -- whatever
dependency resolved the caller. Binding it in one auth dependency missed the
portal, which resolves its principal through `auth_context` directly. The
router, every dependency and the handler run in this same Task; sync
dependencies and handlers run in a threadpool that copies the context.

Matching is by the route's own path template, compiled the way Starlette
compiles it, against the route path Starlette itself would match -- and by
method, with `HEAD` riding along with `GET` as Starlette routes it. A path that
merely LOOKS like a never-exposed one but routes elsewhere is refused too;
over-refusal on this channel is the safe direction.
"""

import re
from collections.abc import Iterable
from typing import Any

from starlette._utils import get_route_path
from starlette.responses import JSONResponse
from starlette.routing import Mount, WebSocketRoute, compile_path
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.i18n import tr
from app.core.mcp_channel import EXTENSION_KEY, McpCall, mcp_call_from_scope, use_mcp_call
from app.mcp.exclusions import NEVER_EXPOSED

NeverRoutes = tuple[tuple[frozenset[str], re.Pattern[str]], ...]
DispatchTable = tuple[tuple[frozenset[str] | None, re.Pattern[str], str], ...]


def _methods(path: str, route: Any) -> frozenset[str]:
    methods = {m for m in getattr(route, "methods", None) or () if (m, path) in NEVER_EXPOSED}
    if "GET" in methods:
        methods.add("HEAD")
    return frozenset(methods)


def never_exposed_routes(flat: Iterable[tuple[str, Any]]) -> tuple[frozenset[int], NeverRoutes]:
    """From `flatten(app.router)`: the route objects' ids (for `auth_context`)
    and `(methods, compiled full path)` matchers (for the guard)."""
    ids: set[int] = set()
    matchers: list[tuple[frozenset[str], re.Pattern[str]]] = []
    for path, route in flat:
        methods = _methods(path, route)
        if methods:
            ids.add(id(route))
            matchers.append((methods, compile_path(path)[0]))
    return frozenset(ids), tuple(matchers)


def missing_never_exposed(flat: Iterable[tuple[str, Any]]) -> frozenset[tuple[str, str]]:
    """`NEVER_EXPOSED` pairs that name no route in `flat` -- non-empty means a
    route was renamed or removed and would silently fall out of the guard."""
    real = {(m, path) for path, route in flat for m in getattr(route, "methods", None) or ()}
    return NEVER_EXPOSED - real


class McpNeverExposedGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and EXTENSION_KEY in (scope.get("extensions") or {}):
            call_or_refusal = _admit(scope)
            if isinstance(call_or_refusal, JSONResponse):
                await call_or_refusal(scope, receive, send)
                return
            with use_mcp_call(call_or_refusal):
                await self.app(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _admit(scope: Scope) -> McpCall | JSONResponse:
    """The marked request's call, or the response refusing it."""
    try:
        call = mcp_call_from_scope(scope)
    except ValueError:
        call = None
    if call is None:  # the key is present, so `None` here is a marker of `None`
        return JSONResponse({"detail": tr("the MCP channel marker is malformed")}, 401)
    # Read per request from the app being served, because the table is built
    # after every module has registered -- later than this layer is added.
    # A missing table raises: fail closed, never pass through unchecked.
    routes: NeverRoutes = scope["app"].state.mcp_never_routes
    method, path = scope["method"], get_route_path(scope)
    for methods, pattern in routes:
        if method in methods and pattern.match(path):
            return JSONResponse({"detail": tr("this action is never available to an AI app")}, 403)
    if call.route:
        # Read per request, like the never-exposed table: it is built when the
        # MCP server starts. Missing means not started -- refuse, never pass.
        table: DispatchTable | None = getattr(scope["app"].state, "mcp_dispatch_table", None)
        if table is None or dispatched_route(table, method, path) != call.route:
            return JSONResponse({"detail": tr("this tool cannot reach that route")}, 403)
    return call


def dispatch_table(flat: Iterable[tuple[str, Any]]) -> DispatchTable:
    """From `flatten(app.router)`, in registration order: `(methods, compiled
    full path, full path template)` for every HTTP route, and for a `Mount`
    (`methods` None) its prefix with everything beneath it."""
    rows: list[tuple[frozenset[str] | None, re.Pattern[str], str]] = []
    for path, route in flat:
        if isinstance(route, Mount):
            rows.append((None, compile_path(path.rstrip("/") + "/{path:path}")[0], path))
            continue
        if isinstance(route, WebSocketRoute):  # never an HTTP dispatch
            continue
        # A route without `methods` (a raw ASGI `Route`) serves EVERY method, as
        # Starlette treats it; skipping it would let it win dispatch unseen.
        methods = getattr(route, "methods", None)
        rows.append((frozenset(methods) if methods else None, compile_path(path)[0], path))
    return tuple(rows)


def dispatched_route(table: DispatchTable, method: str, path: str) -> str | None:
    """`"METHOD /full/path/{template}"` of the route this request WILL be
    dispatched to, or None when no route would serve it.

    A TOOL REACHES EXACTLY ITS OWN ROUTE. A value that matches the tool's
    `{param}` can also be a literal sibling's whole segment
    (`/budget/expenses/{expense_id}` with `export` is `/budget/expenses/export`),
    and the router dispatches to whichever route was REGISTERED FIRST. This
    mirrors that rule over the concrete routes in registration order: the first
    full match (path AND method) wins. A path match with the wrong method (a
    405) or no match at all (a 404) is None -- not the tool's route.

    Over `flatten(app.router)`, not `app.router.routes`: FastAPI wraps every
    included router in a private `_IncludedRouter` whose `matches()` does not
    say which concrete route it selected, and its internals are not an API."""
    for methods, pattern, full in table:
        if pattern.match(path) and (methods is None or method in methods):
            return f"{method} {full}"
    return None
