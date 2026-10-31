"""SECURITY GATE 2: the in-process loopback executor.

docs/modules/ai-admin/spec.md §0.1, §0.12, §4.4, §10.2, §10.5, §13.

**THE LOOPBACK CALL IS THE ENFORCEMENT. THE PREFILTER IS NOT.** §0.12: the
per-caller tool prefilter shortens the tool list and saves tokens; it is an
ergonomics optimization. The control is this module, which re-enters the whole
ASGI stack so `auth_context` re-derives `org_id` FROM THE TOKEN CLAIM and
`require_permission(code)` runs verbatim against the same permission rows inside
the same tenant session. A reader who mistakes the prefilter for the control
will eventually "optimize" this away -- calling the target module's service
function directly, which reads three times faster -- and turn the agent into a
privilege-escalation primitive. That optimization is exactly the mutation
`test_ai_admin_executor_authz.py` demonstrates: it makes every parametrized
authorization case return 200s with real data for a caller holding no module
permission at all.

WHY THE STACK AND NOT THE SERVICE LAYER, verified against this codebase rather
than assumed:

  * authorization is enforced ONLY in route handlers, via `require_permission`;
  * services take an `org_id` and TRUST THEIR CALLER FOR ROLE -- calling one
    enforces nothing;
  * `org_session` pins `app.current_org` only, so RLS scopes by ORG, never by
    role: a read-only auditor and an org-admin get identical rows from it;
  * a large part of the read surface has no service function at all, so a
    service-layer path would mean a second copy of query logic that drifts.

MIDDLEWARE RE-ENTRANCY, checked before this was written: `create_app()` installs
exactly one middleware, `app/api/security_headers.py::security_headers_middleware`.
It reads `request.url.path` and sets two response headers, holds no state and
takes no lock, so it is re-entrant. `auth_context` re-running does write
`users.last_login_at` once per tool call -- §0.1 notes this; it is already true
of any console page that fires eight XHRs, and it is not a defect to fix here.

THE ORDER OF THE SEVEN STEPS BELOW IS THE SECURITY PROPERTY. Nothing is
skippable and nothing may be reordered: an unknown name must never become an
HTTP request, arguments must be validated before a URL exists, and the URL must
be built from the tool's own path template rather than from anything the model
produced.
"""

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from app.core.ai.scrub import Blocked, scrub
from app.core.ai.types import ReadTool, ToolResult

logger = logging.getLogger("its.ai.executor")

# §10.1: a single tool call's wall clock. The whole-turn deadline is separate
# and is enforced by the orchestrator; this bounds one call so a slow route
# fails that call and lets the turn continue.
MAX_TOOL_CALL_SECONDS = 10.0

# The loopback never leaves the process -- `ASGITransport` calls the app object
# directly -- so this host is never resolved and never connected to. It exists
# because `httpx` requires an absolute URL.
LOOPBACK_BASE_URL = "http://ai-loopback.invalid"

ToolErrorKind = Literal[
    "unknown_tool",
    "invalid_arguments",
    "forbidden",
    "not_found",
    "session_expired",
    "upstream_error",
    "timeout",
    "blocked",
]


@dataclass(frozen=True)
class ToolError:
    """A structured, MODEL-FACING error. Never an exception that ends the turn.

    §8.3 and §10.5: a malformed or denied call is information the model can act
    on -- correct itself, or tell the admin the truth -- so it goes back as a
    tool result block and the turn continues. The one exception is
    `session_expired`, which the orchestrator turns into "your session expired,
    sign in and ask again"; the turn ends, the conversation row survives, and it
    is NOT retried with a stale token.
    """

    tool: str
    kind: ToolErrorKind
    message: str
    status: int | None = None


def _failed(tool_name: str, exc: Exception, phase: str) -> ToolError:
    """One failed tool call, told to the model WITHOUT the server's internals.

    The model is given the exception's TYPE NAME and nothing else -- never
    `str(exc)`. A `DBAPIError` stringifies to the failing SQL and its bound
    parameters, an `OSError` to a path; putting either into model context would
    put estate detail and server internals into a third party's prompt, and then
    into a transcript row and an answer. The full exception goes to the server
    log, which is where it belongs.
    """
    logger.warning("ai tool call %s failed while %s", tool_name, phase, exc_info=exc)
    return ToolError(  # i18n-exempt: model-facing, tool result read by the model
        tool=tool_name,
        kind="upstream_error",
        message=f"That lookup failed: {type(exc).__name__}. The rest of the answer stands.",
    )


def _transport(app: FastAPI) -> httpx.AsyncBaseTransport:
    """The in-process transport. A named seam so tests can wrap it with a spy
    that records the exact request this module built -- method, full URL, and
    the complete header set -- rather than inferring it from a response."""
    return httpx.ASGITransport(app=app)


def _split_args(tool: ReadTool, values: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    """`(path substitutions, query parameters)` from VALIDATED values only.

    A field is a path substitution if and only if it appears as a `{placeholder}`
    in the tool's own path template. Everything else declared on the args model
    is a query parameter. Nothing else can become either: the model supplies
    values for declared fields, never names.
    """
    placeholders = {
        segment.strip("{}") for segment in tool.path.split("/") if segment.startswith("{")
    }
    path_values: dict[str, str] = {}
    query: dict[str, Any] = {}
    for name, value in values.items():
        if name in placeholders:
            # `quote` with an empty safe set: a validated `UUID` cannot contain
            # a separator, and this stays correct if a future tool takes a
            # typed non-UUID path parameter.
            path_values[name] = quote(str(value), safe="")
        elif value is not None:
            query[name] = value
    return path_values, query


def _clamp_limit(tool: ReadTool, query: dict[str, Any]) -> dict[str, Any]:
    """§10.2. Applied BEFORE the request is built, so the model cannot request
    10,000 rows and cannot observe having asked for them."""
    if tool.max_limit is None:
        return query
    requested = query.get("limit")
    query["limit"] = (
        tool.max_limit if requested is None else max(1, min(int(requested), tool.max_limit))
    )
    return query


def _shape(tool: ReadTool, payload: Any, effective_limit: int | None) -> tuple[Any, int, bool]:
    """`(payload, result_count, truncated)`, reporting truncation HONESTLY.

    §10.2: a clamped list comes back as
    `{"items": [...], "returned": 50, "total": null, "truncated": true}` so the
    model cannot present 50 rows as the whole answer. `total` is `null` when the
    route does not report one -- which is the truthful answer, and better than
    inventing a number the model would then quote to an admin.
    """
    if not isinstance(payload, list):
        return payload, 1, False
    returned = len(payload)
    truncated = effective_limit is not None and returned >= effective_limit
    return (
        {"items": payload, "returned": returned, "total": None, "truncated": truncated},
        returned,
        truncated,
    )


def map_status(tool: ReadTool, status_code: int) -> ToolError | None:
    """One HTTP status, mapped to a MODEL-FACING outcome. `None` means "carry on".

    **THE MODEL IS TOLD THE TRUTH ABOUT A 403, NEVER GIVEN AN EMPTY LIST.** An
    empty result presented as fact is worse than a refusal: it produces a
    confident wrong answer instead of a visible failure (§13 test 1). A 401 is
    distinguishable and deliberately NOT retried -- a retry with a stale token is
    a second failure and a second audit line. A 404 is what a cross-org (or, on
    the portal, a cross-REQUESTER) row looks like, and it must never be another
    principal's data.
    """
    if status_code == 401:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool.name,
            kind="session_expired",
            message="The signed-in session expired.",
            status=401,
        )
    if status_code == 403:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool.name,
            kind="forbidden",
            message=(
                f"You do not have permission to see that: {tool.permission} is required. "
                f"Say so rather than answering as if there were no matching records."
            ),
            status=403,
        )
    if status_code == 404:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool.name, kind="not_found", message="No such record.", status=404
        )
    if status_code >= 400:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool.name,
            kind="upstream_error",
            message=f"That lookup returned {status_code}.",
            status=status_code,
        )
    return None


async def invoke_tool(
    *,
    tool_name: str,
    raw_args: Mapping[str, Any],
    app: FastAPI,
    bearer: str,
    deadline: float,
    registry: Mapping[str, ReadTool],
    requested_client: UUID | None = None,
) -> ToolResult | ToolError:
    """Resolve -> validate -> build -> clamp -> GET -> map. In that order.

    `requested_client` is the client the turn's own request named in
    `X-Client-Id`, from `TurnContext` -- never from the model -- and is
    forwarded unchanged (step 5).

    **THIS FUNCTION RETURNS; IT DOES NOT RAISE.** Every failure a tool call can
    have -- an unknown name, arguments the model got wrong, a denial, a timeout,
    an exception from inside the target route -- comes back as a `ToolError` the
    turn continues from. The orchestrator's loop and the turn route's audit write
    both depend on that: an exception escaping here ends the turn *before* it is
    persisted, and a turn that read real data and was never persisted was never
    audited either.
    """

    # 1. Resolve the name in the static dict. An unknown name NEVER becomes an
    #    HTTP request (§8.3): it becomes a tool error and the turn continues.
    #    This is the step that makes a fabricated `credentials.list` harmless.
    tool = registry.get(tool_name)
    if tool is None:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool_name,
            kind="unknown_tool",
            message=f"There is no tool named {tool_name!r}. Use one of the tools offered.",
        )

    # 2. Validate against the closed args model. Arguments are NEVER coerced,
    #    repaired or guessed (§10.5) -- a best-effort repair is the executor
    #    deciding what the model meant, which is the executor writing tool calls.
    try:
        parsed = tool.args.model_validate(dict(raw_args))
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "(root)"
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool_name,
            kind="invalid_arguments",
            message=f"Argument {field!r} is not acceptable: {first.get('msg', 'invalid')}.",
        )

    # 3. Build the URL from the tool's own path template and the validated args,
    #    and from NOTHING ELSE (§4.4). No URL, path suffix, header, body or
    #    query string is accepted from the model.
    path_values, query = _split_args(tool, parsed.model_dump())
    # 4. Clamp before the request exists.
    query = _clamp_limit(tool, query)
    url = tool.path.format(**path_values)

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool_name, kind="timeout", message="The turn ran out of time before this call."
        )
    timeout = min(MAX_TOOL_CALL_SECONDS, remaining)
    headers = {"Authorization": bearer}
    if requested_client is not None:
        headers["X-Client-Id"] = str(requested_client)

    # 5. Issue it. The caller's own token, unmodified, so `auth_context` and
    #    `require_permission` re-run exactly as they would for the human --
    #    and, when the turn's request was narrowed by the console's selector,
    #    its `X-Client-Id`, so the route sees the clients the turn saw and no
    #    other (phase 3 design §5.2; `app.mcp.invoke` forwards it the same
    #    way). Never wider: the route narrows by it, it does not grant. Those
    #    two headers and nothing else; the model sets neither.
    #
    #    **THE BROAD `except Exception` IS LOAD-BEARING, NOT LAZY.** The loopback
    #    is an `ASGITransport`, which calls the app object directly and therefore
    #    RE-RAISES APPLICATION EXCEPTIONS instead of turning them into a 500
    #    response the way a socket would. Anything a target route raises that is
    #    not an `HTTPException` -- a `DBAPIError` from a `str` argument carrying a
    #    NUL byte, a `KeyError` in a serializer, anything a future route grows --
    #    would otherwise propagate out of this function, out of `run_turn`, and
    #    out of the turn route, which answers 500 having persisted nothing and
    #    audited nothing. Tool arguments are MODEL OUTPUT and model output is
    #    influenced by retrieved text, so that path is a route to an UNAUDITED
    #    read (invariant #4) plus a per-turn denial of service. One tool call
    #    failing is a tool error the model can act on; it is never the end of the
    #    turn. `except Exception` does not catch `CancelledError`, so a cancelled
    #    request still unwinds.
    try:
        async with httpx.AsyncClient(
            transport=_transport(app), base_url=LOOPBACK_BASE_URL, timeout=timeout
        ) as client:
            response = await client.get(url, params=query, headers=headers)
    except httpx.TimeoutException:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool_name,
            kind="timeout",
            message="That lookup took too long and was abandoned. The rest of the answer stands.",
        )
    except Exception as exc:  # noqa: BLE001 - see the note above; totality is the point
        return _failed(tool_name, exc, "issuing")

    # 6. Map the response. SHARED WITH THE PORTAL EXECUTOR (§0.2): the two
    #    executors differ in WHICH PRINCIPAL and WHICH SURFACE they may reach,
    #    never in what a 403 means to a model. Two copies of this mapping is two
    #    answers to "were you refused or was there nothing there", and the second
    #    of those is the one that produces a confident wrong answer.
    error = map_status(tool, response.status_code)
    if error is not None:
        return error

    # 7. Scrub BEFORE anything can enter model context (§7.3). Fail closed: a
    #    secret-shaped key at any depth rejects the ENTIRE result, because
    #    partial redaction leaves the model reasoning over data whose shape it
    #    cannot see. This layer catches what the registry structurally cannot --
    #    an allowlisted route that has since grown a field.
    #
    #    DECODING IS INSIDE THE GUARD TOO. `response.json()` raises on a 200 that
    #    is not JSON -- a proxy's HTML error page, a truncated body -- and it sat
    #    outside every `try` until this was written, which made a non-JSON 200 an
    #    exception escaping the executor rather than a failed tool call.
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - a body we cannot read is a failed call
        return _failed(tool_name, exc, "decoding")

    outcome = scrub(tool, payload)
    if isinstance(outcome, Blocked):
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool_name,
            kind="blocked",
            message=(
                "This tool returned a field that may carry secret material and "
                "was blocked. Continue without it and say the data was unavailable."
            ),
            status=response.status_code,
        )

    payload, result_count, truncated = _shape(tool, outcome.payload, query.get("limit"))
    # `mutating` is never passed: read mode leaves it at its `False` default
    # (§12.2), and no code path in this module sets it.
    return ToolResult(
        payload=payload,
        status=response.status_code,
        result_count=result_count,
        truncated=truncated,
    )
