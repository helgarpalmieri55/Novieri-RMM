"""`/mcp`: the platform's MCP server over Streamable HTTP, behind the OAuth gate.

THE ONLY DOOR AN EXTERNAL AI APP HAS (MCP server design §2, §3, §6). Every
request is authenticated by `McpAsgi` before the SDK sees a byte of it, and
nothing reaches a tool handler without an `McpPrincipal` on the scope. There is
no API key, no static token, no basic auth: a request without a valid OAuth
access token gets a 401 whose `WWW-Authenticate` names the protected-resource
metadata below, and nothing else.

ORDER OF THE GATE, and why:

1. no MCP resource (`ITS_PUBLIC_CONSOLE_URL` unset) -> 503;
2. the request is not addressed to the public host -> 421, or comes from a
   foreign browser origin -> 403 (DNS rebinding; see `_off_host` below);
3. no bearer -> 401 with the challenge;
4. `auth_context` on a scope this gate marks itself: the MCP audience only, and
   the console's whole admission path (org lookup, email domain, user status,
   self-provisioning) -> 401 / 403 / 503 as it decides;
5. the org switch `mcp_enabled` -> 403;
6. `mcp.use` -> 403;
7. `mcp_revocation` -> 401, so the client runs the OAuth flow again and the
   person reconnects deliberately.

Steps 5-7 are read inside `org_session` on the application engine, so RLS
scopes them to the token's organization as well as the `WHERE` clause does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

import anyio
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    ToolAnnotations,
)
from mcp.types import Tool as SdkTool
from redis.exceptions import RedisError
from sqlalchemy import exists, or_, select
from starlette.datastructures import Headers
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from app.api.deps import AuthContext, auth_context, requested_client
from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.i18n import parse_accept_language, tr, use_locale
from app.core.mcp_channel import EXTENSION_KEY, McpCall, mcp_resource, use_mcp_call
from app.core.rbac import codes_held_anywhere
from app.core.router_inspect import flatten
from app.core.security import TokenClaims
from app.core.tenancy import org_session
from app.mcp import invoke as _invoke
from app.mcp.catalog import Tool, build_catalog
from app.mcp.guard import dispatch_table
from app.models import Organization
from app.models.mcp import McpRevocation

if TYPE_CHECKING:
    from fastapi import FastAPI
    from mcp.server.context import ServerRequestContext

#: Where the SDK handlers find the caller. A scope key, not a contextvar: the
#: SDK may run a handler in a task of its own task group, and a scope key
#: travels with the `Request` the SDK hands the handler (`ctx.request`).
PRINCIPAL_KEY = "mcp_principal"

#: Requests one person may make per minute, over every AI app they connect
#: (design §7): every request that passes the gate, counted once, at the door
#: (`McpAsgi`). A module constant so a test can lower it.
MCP_CALLS_PER_MINUTE = 120
_RATE_WINDOW_S = 60
#: How long the shielded `mcp.tool_called` write may take before it is
#: abandoned (`_ToolHandlers._audit_shielded`). The shield keeps a client's
#: disconnect from skipping the row; this keeps a database that never answers
#: from holding the handler, and shutdown, for ever.
_AUDIT_DEADLINE_S = 10.0
#: How long a write still running at shutdown is given to finish before it is
#: cancelled (`_run`).
_SHUTDOWN_GRACE_S = 30.0
#: JSON-RPC "server error" range (-32000..-32099): a refused `tools/list`.
_RATE_LIMITED_CODE = -32029
_TOOL_NAME = re.compile(r"[a-z0-9_]{1,64}")

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"https": 443, "http": 80}


@dataclass(frozen=True)
class McpPrincipal:
    """Who is calling, as the gate admitted them. `bearer` is the Authorization
    header exactly as received, so an in-process call acts as the person and
    nobody else; it is kept out of `repr`, so it never reaches a log line or a
    traceback."""

    org_id: UUID
    user_id: UUID
    claims: TokenClaims
    bearer: str = field(repr=False)
    areas: frozenset[str] | None
    accept_language: str
    #: The person's client scope, from the admitted `AuthContext` -- every
    #: session a handler opens is held to it (MSP clients spec §2.3).
    clients: ClientScope
    #: The client the MCP request's `X-Client-Id` named, if any. `clients` is
    #: already narrowed by it; the in-process call forwards the same header
    #: (`app.mcp.invoke`), so the route a tool reaches runs under the same
    #: scope as the listing, the gate and the audit row here.
    requested_client: UUID | None = None
    #: `(count, seconds left in the window)` when this request was over the
    #: per-person limit at the door, else None. Counted there, once; the
    #: handlers only read it (`tools/call` to word and audit its refusal).
    limited: tuple[int, int] | None = None


def principal_from(ctx: ServerRequestContext[Any, Any]) -> McpPrincipal:
    """The principal the gate put on this request's scope. Fails closed: a
    handler reached without one is a wiring defect, never an anonymous call."""
    request = ctx.request
    scope = getattr(request, "scope", None)
    principal = (scope or {}).get("state", {}).get(PRINCIPAL_KEY)
    if not isinstance(principal, McpPrincipal):
        raise RuntimeError(  # i18n-exempt: wiring defect, logged by the SDK
            "MCP handler reached without a principal"
        )
    return principal


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


async def _json(
    send: Send, status_code: int, body: dict[str, Any], headers: dict[str, str] | None = None
) -> None:
    payload = json.dumps(body).encode()
    raw = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
        *((k.lower().encode(), v.encode()) for k, v in (headers or {}).items()),
    ]
    await send({"type": "http.response.start", "status": status_code, "headers": raw})
    await send({"type": "http.response.body", "body": payload})


def _metadata_url(resource: str) -> str:
    return resource.removesuffix("/mcp") + "/.well-known/oauth-protected-resource/mcp"


async def _unauthorised(send: Send, resource: str, *, detail: str | None = None) -> None:
    """401 with the challenge that starts the OAuth flow (RFC 9728 §5.1). With
    a token presented and refused, also `error="invalid_token"` (RFC 6750 §3.1),
    which tells the client to get a new one rather than retry this one."""
    challenge = f'Bearer resource_metadata="{_metadata_url(resource)}"'
    if detail is not None:
        challenge += ', error="invalid_token"'
    body = {"detail": detail if detail is not None else tr("missing bearer token")}
    await _json(send, 401, body, {"WWW-Authenticate": challenge})


# --------------------------------------------------------------------------- #
# DNS rebinding
# --------------------------------------------------------------------------- #


def _public_host(console_url: str) -> tuple[frozenset[str], frozenset[str]]:
    """The `Host` values and browser `Origin`s the public console URL answers
    to, with and without an explicit default port."""
    parts = urlsplit(console_url.strip())
    scheme = parts.scheme.lower()
    name = (parts.hostname or "").lower()
    if ":" in name:  # IPv6 literal: `Host` carries it in brackets
        name = f"[{name}]"
    default = _DEFAULT_PORTS.get(scheme)
    port = parts.port
    hosts = {f"{name}:{port}"} if port is not None else set()
    if port is None or port == default:
        hosts.add(name)
        if default is not None:
            hosts.add(f"{name}:{default}")
    return frozenset(hosts), frozenset(f"{scheme}://{h}" for h in hosts)


def _off_host(headers: Headers, console_url: str, resource: str) -> tuple[int, str] | None:
    """Refuse a request not addressed to the public host, or sent by a page on
    another web origin.

    WHY HERE, AND NOT THE SDK'S OWN CHECK. The SDK has the same check
    (`TransportSecuritySettings`), but it is fixed when the session manager is
    built, and the public host is a setting this gate reads per request, as
    it reads the resource itself. One check, from the same value the token's
    audience is checked against: a client using any other host name could not
    present a token for this resource anyway (RFC 9728 §3.3), so refusing it
    costs nothing and closes the rebinding path. Traefik routes `/mcp` by path
    alone and passes `Host` through unchanged, so this is the only place the
    host is ever checked."""
    hosts, origins = _public_host(console_url)
    if headers.get("host", "").lower() not in hosts:
        return 421, tr("this MCP server answers only at {resource}", resource=resource)
    origin = headers.get("origin")
    if origin is not None and origin.lower() not in origins:
        return 403, tr("the MCP server does not accept requests from another web origin")
    return None


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def _unverified_azp(token: str) -> str:
    """The app a token claims to be for, WITHOUT verifying it -- used only to
    label the scope marker before `auth_context` verifies the same token."""
    try:
        azp = jwt.decode(token, options={"verify_signature": False}).get("azp")
    except Exception:  # noqa: BLE001 - a label, never a decision; validation follows
        return ""
    return azp if isinstance(azp, str) else ""


async def _gate(fastapi_app: FastAPI, ctx: AuthContext) -> tuple[int, str] | None:
    """The org switch, `mcp.use` and revocation, in that order; None when all
    three allow the call. Checked on EVERY request (design §6): turning the
    switch off, removing the permission or revoking the app stops the next
    call, not the next sign-in."""
    issued_at = datetime.fromtimestamp(ctx.claims.iat, UTC)
    async with org_session(
        ctx.org_id, engine=fastapi_app.state.app_engine, clients=ctx.clients
    ) as session:
        org_settings = (
            await session.execute(
                select(Organization.settings).where(Organization.id == ctx.org_id)
            )
        ).scalar_one_or_none()
        # `is True`, not truthiness: the switch is on only when someone turned
        # it on, and a malformed value is off.
        if (org_settings or {}).get("mcp_enabled") is not True:
            return 403, tr(
                "MCP is turned off for this organization. An org-admin turns it on in "
                "Settings → AI connections."
            )
        # Held on SOME client, as `require_permission` admits a route (spec
        # §2.1): what the person may then reach is their scope's business.
        if "mcp.use" not in await codes_held_anywhere(session, ctx.user_id):
            return 403, tr("requires {code}", code="mcp.use")
        # A row with no `user_id` revokes the app for everyone in THIS org; the
        # `org_id` predicate and RLS both keep another org's row out. Strictly
        # later than `iat`, which is whole seconds: a token issued in the same
        # second as the revocation is refused, the safe side of the rounding.
        revoked = (
            await session.execute(
                select(
                    exists().where(
                        McpRevocation.org_id == ctx.org_id,
                        McpRevocation.client_id == ctx.claims.azp,
                        or_(
                            McpRevocation.user_id.is_(None),
                            McpRevocation.user_id == ctx.user_id,
                        ),
                        McpRevocation.revoked_at > issued_at,
                    )
                )
            )
        ).scalar_one()
        if revoked:
            return 401, tr("this AI app's access was revoked; connect it again to continue")
    return None


class McpAsgi:
    """The only door to the MCP server. Every request is authenticated here, then
    handed to the SDK; nothing reaches a tool handler without a principal."""

    def __init__(
        self, fastapi_app: FastAPI, session_manager: StreamableHTTPSessionManager
    ) -> None:
        self._app = fastapi_app
        self._sm = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # `Route` only routes http here; belt and braces
            return
        settings = self._app.state.settings
        resource = mcp_resource(settings)
        if resource is None:
            detail = tr("MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set.")
            return await _json(send, 503, {"detail": detail})
        request = Request(scope, receive)
        off_host = _off_host(request.headers, settings.public_console_url, resource)
        if off_host is not None:
            return await _json(send, off_host[0], {"detail": off_host[1]})
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return await _unauthorised(send, resource)

        # REUSE THE CONSOLE'S WHOLE ADMISSION PATH (org lookup, email domain,
        # user status, self-provisioning) with the MCP audience, by marking the
        # scope the way an in-process MCP call is marked.
        #
        # THE APP LABEL IS READ FROM THE TOKEN UNVERIFIED, and that is safe:
        # it only labels the marker, so that an admission audit event written
        # inside `auth_context` (a first-seen `auth.login`) says `via: "mcp"`
        # and which app. `auth_context` verifies the token -- signature,
        # issuer, MCP audience, expiry -- before it writes anything, in the
        # same call, so a forged token is refused before its label is used.
        # And the label is compared with the verified `azp` below.
        label = _unverified_azp(authorization.removeprefix("Bearer "))
        # Revocation is keyed on `azp`: a token that names no app could not be
        # revoked before it expired, so it is not admitted at all. Refused
        # before admission, so it provisions nobody; the check after admission
        # (`claims.azp != label`) makes the unverified read binding.
        if not label:
            return await _unauthorised(
                send, resource, detail=tr("this token does not name the AI app it was issued to")
            )
        marked: Scope = {
            **scope,
            "app": self._app,
            "extensions": {
                **(scope.get("extensions") or {}),
                EXTENSION_KEY: {"azp": label, "tool": ""},
            },
        }
        try:
            with use_mcp_call(McpCall(azp=label, tool="")):
                ctx = await auth_context(Request(marked, receive))
        except HTTPException as exc:
            if exc.status_code == 401:
                return await _unauthorised(send, resource, detail=str(exc.detail))
            return await _json(send, exc.status_code, {"detail": exc.detail})
        if ctx.claims.azp != label:
            return await _unauthorised(
                send, resource, detail=tr("the MCP channel marker is malformed")
            )

        refusal = await _gate(self._app, ctx)
        if refusal is not None:
            status_code, detail = refusal
            if status_code == 401:
                return await _unauthorised(send, resource, detail=detail)
            return await _json(send, status_code, {"detail": detail})

        # POST ONLY. Stateless with JSON responses, the server never has
        # anything to push, so the SDK's standalone GET stream would sit open
        # forever holding a task per client, and DELETE has no session to end.
        # The MCP transport lets a server answer GET with 405 (Streamable HTTP,
        # "Listening for Messages from the Server"). After the gate, so an
        # unauthenticated GET still gets the challenge that starts OAuth.
        if scope["method"] != "POST":
            detail = tr("the MCP server accepts only POST requests")
            return await _json(send, 405, {"detail": detail}, {"Allow": "POST"})

        # EVERY ADMITTED REQUEST COUNTS, ONCE, HERE: a ping or an initialize
        # has cost the gate's reads as a call has. The refusal itself is the
        # handlers' to word, because only `tools/call` knows the tool name its
        # audit row needs. A request over the limit that is neither
        # (initialize, ping, a notification) is answered by the SDK: it reads
        # and changes nothing, and refusing it would turn a minute's limit
        # into a broken connection.
        limited = await _rate_limited(self._app.state.redis, ctx.user_id)

        areas = frozenset(a for a in request.query_params.get("areas", "").split(",") if a)
        scope.setdefault("state", {})[PRINCIPAL_KEY] = McpPrincipal(
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            claims=ctx.claims,
            bearer=authorization,
            areas=areas or None,
            accept_language=request.headers.get("accept-language", ""),
            clients=ctx.clients,
            # Already validated by `auth_context` (a malformed one was a 400).
            requested_client=requested_client(request),
            limited=limited,
        )
        await self._sm.handle_request(scope, receive, send)


# --------------------------------------------------------------------------- #
# Protected-resource metadata (RFC 9728)
# --------------------------------------------------------------------------- #

metadata_router = APIRouter()


@metadata_router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@metadata_router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def protected_resource(request: Request) -> JSONResponse:
    """Public by necessity: it is how an OAuth client learns where to sign in.
    It names the resource and the realm, both of which the 401 challenge and
    the realm's own discovery document already publish."""
    settings = request.app.state.settings
    resource = mcp_resource(settings)
    if resource is None:
        raise HTTPException(503, tr("MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set."))
    return JSONResponse(
        {
            "resource": resource,
            "authorization_servers": [settings.oidc_issuer],
            "scopes_supported": ["mcp"],
            "bearer_methods_supported": ["header"],
        }
    )


# --------------------------------------------------------------------------- #
# The SDK server: tools/list and tools/call (design §4.2, §4.3, §7)
# --------------------------------------------------------------------------- #


def _catalog(fastapi_app: FastAPI) -> dict[str, Tool]:
    """The tool catalogue, built when the server started (`McpMount.lifespan`).
    Missing means the server was never started: fail closed, never serve an
    empty or half-built list as if it were the person's."""
    catalog: dict[str, Tool] | None = getattr(fastapi_app.state, "mcp_catalog", None)
    if catalog is None:
        raise RuntimeError(  # i18n-exempt: wiring defect, logged by the SDK
            "MCP tool catalogue was not built; the MCP server was not started"
        )
    return catalog


def _visible(tool: Tool, held: set[str], areas: frozenset[str] | None) -> bool:
    """Shown to, and callable by, this person: a permission they hold (or one
    of the few routes open to any signed-in user, `permission is None`), in
    the areas this connection asked for."""
    if areas is not None and tool.area not in areas:
        return False
    return tool.permission is None or tool.permission in held


def _sdk_tool(tool: Tool) -> SdkTool:
    return SdkTool(
        name=tool.name,
        description=tool.description or None,
        input_schema=tool.input_schema,
        annotations=ToolAnnotations.model_validate(tool.annotations),
    )


def _args_sha256(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _bump(redis: Any, key: str, window_s: int) -> tuple[int, int]:
    """`INCR`, then the key's TTL: `(count, seconds left in the window)`.

    A FIXED WINDOW: it starts at the first request and ends `window_s` later,
    whatever happens in between (`EXPIRE` is never pushed out). A key with no
    TTL at all -- the `EXPIRE` after a first `INCR` never landed, a crash or a
    lost connection between the two -- gets one here, on any request, so a
    person can never be locked out for good."""
    count = int(await redis.incr(key))
    ttl = int(await redis.ttl(key))
    if ttl < 0:
        await redis.expire(key, window_s)
        ttl = window_s
    return count, ttl


def _result(outcome: _invoke.InvokeOutcome) -> CallToolResult:
    """`structuredContent` is always an object (MCP requires one): a dict as
    it is, a list as `{"items": [...]}`, any other JSON value as
    `{"value": ...}`, a 204 as `{}`. The same JSON, as the route returned it,
    is the text content for clients that read only text."""
    if outcome.error is not None:
        return CallToolResult(content=[TextContent(type="text", text=outcome.error)], is_error=True)
    payload = outcome.payload
    if isinstance(payload, dict):
        structured = payload
    elif isinstance(payload, list):
        structured = {"items": payload}
    else:
        structured = {"value": payload}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=structured,
        is_error=False,
    )


def _rate_key(user_id: UUID) -> str:
    return f"mcp:rate:user:{user_id}"


async def _rate_limited(redis: Any, user_id: UUID) -> tuple[int, int] | None:
    """Count one request; `(count, seconds until the window ends)` when it is
    over the limit, else None."""
    try:
        count, ttl = await _bump(redis, _rate_key(user_id), _RATE_WINDOW_S)
    except RedisError:
        # FAILS OPEN, as the in-app AI's limits do: Redis down must not
        # take every AI connection down with it. RBAC, the gate and the
        # audit row do not depend on Redis.
        logger.warning("mcp rate limit unavailable; redis error", exc_info=True)
        return None
    if count <= MCP_CALLS_PER_MINUTE:
        return None
    return count, ttl if ttl > 0 else _RATE_WINDOW_S


def _audit_name(name: str) -> str:
    """The tool name as the audit log records it: a well-formed name as sent
    (known or not), anything else as `<invalid>` -- a client's arbitrary
    string is not the audit log's to keep."""
    return name if _TOOL_NAME.fullmatch(name) else "<invalid>"


class _ToolHandlers:
    """The two SDK handlers, bound to the API they call into.

    THE SDK RUNS A HANDLER IN ITS OWN TASK GROUP, so the request's context
    variables -- its language, its MCP channel -- are not set here. Both are
    re-established from the principal the gate put on the scope: the language
    around each handler's own sentences, the channel around the
    `mcp.tool_called` event. The route the call reaches gets both from its own
    request (`Accept-Language` is forwarded; the scope marker is set by
    `invoke`)."""

    def __init__(self, fastapi_app: FastAPI) -> None:
        self._app = fastapi_app

    async def list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        principal = principal_from(ctx)
        with use_locale(parse_accept_language(principal.accept_language)):
            # Counted at the door (`McpAsgi`), like every request; a refused
            # listing is not audited -- it reads nothing and changes nothing.
            if principal.limited is not None:
                _, seconds = principal.limited
                raise MCPError(
                    _RATE_LIMITED_CODE,
                    tr(
                        "at most {count} requests per minute to this MCP server; try again in "
                        "{seconds} seconds",
                        count=MCP_CALLS_PER_MINUTE,
                        seconds=seconds,
                    ),
                    {"status": 429, "retry_after": seconds},
                )
        catalog = _catalog(self._app)
        # Every code held on at least one client: exactly the tools whose
        # route gate (`require_permission`) would admit this person.
        async with org_session(
            principal.org_id, engine=self._app.state.app_engine, clients=principal.clients
        ) as session:
            held = await codes_held_anywhere(session, principal.user_id)
        return ListToolsResult(
            tools=[_sdk_tool(t) for t in catalog.values() if _visible(t, held, principal.areas)]
        )

    async def call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: CallToolRequestParams
    ) -> CallToolResult:
        principal = principal_from(ctx)
        args: dict[str, Any] = dict(params.arguments or {})
        with use_locale(parse_accept_language(principal.accept_language)):
            # EVERY REQUEST COUNTS, a refused one included, and was counted at
            # the door (`McpAsgi`): the limit bounds what an AI app can make
            # this server do. Only the refusal is worded here.
            if principal.limited is not None:
                count, seconds = principal.limited
                outcome = _invoke.InvokeOutcome(
                    429,
                    None,
                    tr(
                        "at most {count} requests per minute to this MCP server; try again in "
                        "{seconds} seconds",
                        count=MCP_CALLS_PER_MINUTE,
                        seconds=seconds,
                    ),
                )
                # ONE AUDIT ROW PER WINDOW for refusals: a client hammering the
                # limit must not be able to grow the hash-chained log without
                # bound. The rest are logged, not audited.
                if await self._first_refusal(principal.user_id, seconds):
                    await self._audit_shielded(principal, params.name, args, outcome)
                else:
                    logger.info(
                        "mcp tool call refused by the rate limit",
                        extra={"person": principal.user_id, "count": count},
                    )
                return _result(outcome)
            called: _invoke.InvokeOutcome | None = None
            try:
                called = await self._call(principal, params.name, args)
            except Exception:
                # Audited all the same, as a 500; the AI app is told nothing of
                # the exception, whose text is the platform's, not the person's.
                logger.exception("mcp tool call raised", extra={"tool": _audit_name(params.name)})
                called = _invoke.InvokeOutcome(
                    500,
                    None,
                    tr(
                        "the platform could not complete this action (status {status})",
                        status=500,
                    ),
                )
            finally:
                # THE AUDIT ROW IS WRITTEN WHATEVER HAPPENED, a cancelled handler
                # included: a client that disconnects mid-call cancels this
                # task, and the call may already have acted. Shielded, so the
                # cancellation cannot skip it; recorded as 499 (the client
                # went away) with an unknown outcome.
                if called is None:
                    called = _invoke.InvokeOutcome(499, None, None, outcome_unknown=True)
                await self._audit_shielded(principal, params.name, args, called)
            return _result(called)

    async def _call(
        self, principal: McpPrincipal, name: str, args: dict[str, Any]
    ) -> _invoke.InvokeOutcome:
        tool = _catalog(self._app).get(name)
        if tool is None or (principal.areas is not None and tool.area not in principal.areas):
            return _invoke.InvokeOutcome(404, None, tr("there is no tool named {name}", name=name))
        # A tool the person was not shown is not callable either. Refused
        # here, before the route, in the API's own words; the route would
        # refuse it again with the same check (`require_permission`).
        if tool.permission is not None:
            async with org_session(
                principal.org_id, engine=self._app.state.app_engine, clients=principal.clients
            ) as session:
                allowed = tool.permission in await codes_held_anywhere(
                    session, principal.user_id
                )
            if not allowed:
                return _invoke.InvokeOutcome(403, None, tr("requires {code}", code=tool.permission))
        return await _invoke.invoke(
            app=self._app,
            tool=tool,
            args=args,
            principal=principal,
            background=getattr(self._app.state, "mcp_background", None),
        )

    async def _first_refusal(self, user_id: UUID, seconds: int) -> bool:
        """True for the first refused call in this person's window. Redis
        failing answers True: an extra audit row is the safe side."""
        try:
            first = await self._app.state.redis.set(
                f"{_rate_key(user_id)}:refusal_audited", 1, nx=True, ex=seconds
            )
        except RedisError:
            return True
        return bool(first)

    async def _audit_shielded(
        self,
        principal: McpPrincipal,
        name: str,
        args: dict[str, Any],
        outcome: _invoke.InvokeOutcome,
    ) -> None:
        """`_audit`, shielded from the caller's cancellation and bounded by a
        deadline of its own INSIDE the shield. Without the deadline a database
        that never answers would hold this handler past any disconnect and the
        server's shutdown past its grace; abandoned, the call is logged so the
        missing row is not silent."""
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(_AUDIT_DEADLINE_S) as deadline:
                await self._audit(principal, name, args, outcome)
        if deadline.cancelled_caught:
            logger.error(
                "mcp.tool_called audit write abandoned after its deadline",
                extra={
                    "tool": _audit_name(name),
                    "status": outcome.status,
                    "deadline_s": _AUDIT_DEADLINE_S,
                },
            )

    async def _audit(
        self,
        principal: McpPrincipal,
        name: str,
        args: dict[str, Any],
        outcome: _invoke.InvokeOutcome,
    ) -> None:
        """`mcp.tool_called`, for every call whatever its outcome, as the person,
        with the status they saw. A hash of the arguments, NEVER the arguments:
        they can carry anything a person typed (design §7). Written on the MCP
        channel, so it says `via: "mcp"` and which app, like every other event
        an MCP call writes."""
        tool = _audit_name(name)
        metadata: dict[str, Any] = {
            "tool": tool,
            "app": principal.claims.azp,
            "status": outcome.status,
            "args_sha256": _args_sha256(args),
        }
        if outcome.blocked:
            metadata["blocked"] = True
        if outcome.outcome_unknown:
            metadata["outcome_unknown"] = True
        with use_mcp_call(McpCall(azp=principal.claims.azp, tool=tool)):
            async with org_session(
                principal.org_id, engine=self._app.state.app_engine, clients=principal.clients
            ) as session:
                await audit_service.record(
                    session,
                    org_id=principal.org_id,
                    actor_type="user",
                    actor_id=principal.user_id,
                    action="mcp.tool_called",
                    metadata=metadata,
                )


@dataclass(frozen=True)
class McpMount:
    asgi: McpAsgi
    session_manager: StreamableHTTPSessionManager
    metadata_router: APIRouter
    fastapi_app: FastAPI

    def lifespan(self) -> AbstractAsyncContextManager[None]:
        """Builds the tool catalogue, then runs the session manager. Entered
        from the app's lifespan; once only, which is the SDK's rule, not ours."""
        return _run(self.fastapi_app, self.session_manager)

    def routes(self) -> list[Route]:
        """`/mcp` AND `/mcp/`, every method, no redirect. Not a `Mount`:
        Starlette answers a mount's bare prefix with a 307 to the slashed
        form, and an MCP client POSTs to the exact URL it was given."""
        return [Route(path, self.asgi, include_in_schema=False) for path in ("/mcp", "/mcp/")]


@asynccontextmanager
async def _run(
    fastapi_app: FastAPI, session_manager: StreamableHTTPSessionManager
) -> AsyncIterator[None]:
    # THE CATALOGUE IS BUILT HERE, ONCE, WHEN THE SERVER STARTS: after every
    # module has registered its routes (`create_app` is long finished), and
    # before the first request. Not in `build_mcp`, which runs inside
    # `create_app` before `modules.register_all` -- the catalogue would miss
    # every module route -- and not at the end of `create_app`, which every
    # API test builds and which would pay the OpenAPI generation (seconds)
    # for a server most of them never start. A malformed catalogue raises
    # here and the server does not start.
    if getattr(fastapi_app.state, "mcp_catalog", None) is None:
        fastapi_app.state.mcp_catalog = build_catalog(fastapi_app)
        # The same snapshot of the router, in dispatch order, for the guard's
        # "a tool reaches exactly its own route" check (`app.mcp.guard`).
        fastapi_app.state.mcp_dispatch_table = dispatch_table(flatten(fastapi_app.router))
    # WRITE TOOL CALLS RUN HERE, not in the request's task, so a call that
    # overruns its timeout is never cancelled half way (`invoke`). At shutdown
    # a write still running gets `_SHUTDOWN_GRACE_S` to finish, then is
    # cancelled: a deadline, so one stuck write cannot hold the process.
    async with anyio.create_task_group() as background:
        fastapi_app.state.mcp_background = background
        try:
            async with session_manager.run():
                yield
        finally:
            fastapi_app.state.mcp_background = None
            background.cancel_scope.deadline = anyio.current_time() + _SHUTDOWN_GRACE_S


def build_mcp(fastapi_app: FastAPI) -> McpMount:
    handlers = _ToolHandlers(fastapi_app)

    def input_schema(name: str) -> dict[str, Any] | None:
        # For the SDK's `Mcp-Param-*` header check on the 2026 wire. Without
        # it the SDK runs the whole `tools/list` handler (a database read) on
        # every call to find one schema. The schema of a tool the person may
        # not call is no secret: the catalogue is the API's own surface.
        tool = getattr(fastapi_app.state, "mcp_catalog", {}).get(name)
        return tool.input_schema if tool is not None else None

    server: Server[Any] = Server(
        "itms",
        version=fastapi_app.version,
        get_tool_input_schema=input_schema,
        on_list_tools=handlers.list_tools,
        on_call_tool=handlers.call_tool,
    )
    session_manager = StreamableHTTPSessionManager(
        app=server,
        # Stateless with JSON responses (design §2): any worker serves any
        # request, nothing is pinned to a process, and no session outlives the
        # request that the gate authenticated.
        stateless=True,
        json_response=True,
        # The SDK's Host/Origin check is OFF because the gate does it, per
        # request, from the live setting -- see `_off_host`. Explicit rather
        # than defaulted, so the choice is visible here.
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return McpMount(
        asgi=McpAsgi(fastapi_app, session_manager),
        session_manager=session_manager,
        metadata_router=metadata_router,
        fastapi_app=fastapi_app,
    )
