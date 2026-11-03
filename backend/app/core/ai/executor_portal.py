"""SECURITY GATE 2, PORTAL EDITION: the resolver's own loopback executor.

docs/modules/ai-automation.md §0.2, §0.9, §3.1, §9.1.

**A SECOND EXECUTOR, NOT A BRANCH, AND THAT IS THE WHOLE DESIGN.** §0.2: "one
executor with a `principal kind` branch" is REJECTED, because a branch is how the
staff path and the portal path end up sharing a code path only one of them was
audited for. There are two functions, and this one physically cannot issue a
staff request:

* it refuses any tool whose path is not under `/api/v1/portal/`, and any tool
  whose gate is not a `portal.*` code, **before a URL exists** -- so even a staff
  `ReadTool` mistakenly placed in the resolver's tool set never becomes an HTTP
  request. `_refuse_off_surface` is that guard, it is fail-closed, and
  `tests/test_ai_portal.py` proves it with the tape rather than with the answer;
* the principal it serves is a `PortalContext`, which
  `app/modules/portal/principal.py` deliberately makes **not** an `AuthContext`.
  There is no `AuthContext` in reach here to build a staff request from.

**IT IS A LOOPBACK, NOT A SERVICE CALL, AND THAT WAS A DECISION.** §0.2 describes
the resolver's tools as "the portal's existing routes, invoked in-process under
`portal_session` with the requester pinned", while §12 gates the surface with
`require_portal_permission`. Those are two different mechanisms. This build
chooses the loopback, matching the staff executor: an `httpx.ASGITransport`
against the portal's own routes carrying the portal principal's own bearer token,
so `resolve_portal_principal` and `require_portal_permission` RE-RUN VERBATIM and
`portal_session` is opened by the route rather than by this layer.

Direct service calls would make the AI layer decide which GUCs get pinned, and
`portal_session`'s fail-closed property is only fail-closed if BOTH are
(`app.current_org` and `app.current_requester`). A layer that pins one of them is
a layer that has re-implemented tenancy, badly, for the persona that reads the
most attacker-controlled text on the platform.

**THE PREMISE, VERIFIED, AND WHAT WOULD BREAK IT.** v1 portal principals
authenticate through `auth_context`, so a bearer token exists and this works.
`principal.py`'s own docstring says external end users will need a SECOND
implementation of `resolve_portal_principal`. **If that implementation lands
without a bearer token, this executor stops working, and the correct move is to
report it -- never to mint a service token for the AI layer.** That is the
privilege-escalation primitive `executor_staff.py`'s docstring warns about, and
it would be worse here: the resolver serves a principal with almost no
permissions, so a service token would hand it every permission it lacks.
"""

import logging
import time
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from app.core.ai.executor_staff import (
    LOOPBACK_BASE_URL,
    MAX_TOOL_CALL_SECONDS,
    ToolError,
    _clamp_limit,
    _failed,
    _shape,
    _split_args,
    map_status,
)
from app.core.ai.scrub import Blocked, scrub
from app.core.ai.types import ReadTool, ToolResult

logger = logging.getLogger("its.ai.executor_portal")

__all__ = ["PORTAL_GATE_PREFIX", "PORTAL_PATH_PREFIX", "invoke_portal_tool"]

#: **THE SURFACE THIS EXECUTOR MAY REACH, AS A PREFIX, CHECKED BEFORE A URL
#: EXISTS.** Not a convention and not a review note: a tool naming a staff path
#: is refused here, with no HTTP request made, whatever tool set it arrived in.
#: The portal's routes are the only ones written to be safe for a principal whose
#: entire entitlement is `portal.ticket.read` -- they exclude internal notes,
#: staff identities, queues and assignees BY MODEL SHAPE, not by filter.
PORTAL_PATH_PREFIX = "/api/v1/portal/"

#: And the gate must be a portal permission. Path and gate are checked TOGETHER
#: because either alone is defeatable: a staff route mounted under `/portal/`
#: would pass a path check, and a portal-gated route living elsewhere would pass
#: a gate check. Both have to hold.
PORTAL_GATE_PREFIX = "portal."


def _transport(app: FastAPI) -> httpx.AsyncBaseTransport:
    """The in-process transport. A named seam, mirroring the staff executor's,
    so a test can wrap it with a spy that records the exact request this module
    built -- and, more importantly, can assert that NO request was built at all
    for a tool this executor refuses."""
    return httpx.ASGITransport(app=app)


def _refuse_off_surface(tool: ReadTool) -> ToolError | None:
    """The guard that makes "this executor cannot construct a staff request" a
    property of the code rather than of the tool set that happens to be passed.

    Returns a MODEL-FACING error, not an exception: a turn that ends because a
    tool set was misconfigured is a denial of service, and the model can say
    "that lookup is not available to me" perfectly well. The log line is what an
    operator needs, and it is at ERROR because reaching this branch means a
    persona profile is wrong.
    """
    if tool.path.startswith(PORTAL_PATH_PREFIX) and tool.permission.startswith(
        PORTAL_GATE_PREFIX
    ):
        return None
    logger.error(
        "portal executor refused an off-surface tool: %s -> %s (gate %s); a portal "
        "principal must never reach a staff route",
        tool.name,
        tool.path,
        tool.permission,
    )
    return ToolError(  # i18n-exempt: model-facing, tool result read by the model
        tool=tool.name,
        kind="forbidden",
        message="That lookup is not available here. Answer from what you already have.",
    )


async def invoke_portal_tool(
    *,
    tool_name: str,
    raw_args: Mapping[str, Any],
    app: FastAPI,
    bearer: str,
    deadline: float,
    registry: Mapping[str, ReadTool],
    requested_client: UUID | None = None,
) -> ToolResult | ToolError:
    """Resolve -> REFUSE OFF-SURFACE -> validate -> build -> clamp -> GET -> map.

    `requested_client` exists because `LoopbackExecutor` names it, and it is
    never forwarded: a portal turn is not narrowed by the console's selector
    (phase 3 design §5.2 -- the portal does not change), and the portal route
    passes none. Any other value than None is refused before anything else,
    so nothing can quietly drop a narrowing here.

    The same seven steps as the staff executor with one inserted second, and the
    order is the security property: an unknown name must never become an HTTP
    request, an off-surface tool must never become one either, and arguments are
    validated before a URL exists.

    **THIS FUNCTION RETURNS; IT DOES NOT RAISE.** Same contract as the staff
    executor, for the same reason: an exception escaping here ends the turn
    before it is persisted, and a turn that read real data and was never
    persisted was never audited either.
    """
    if requested_client is not None:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool_name,
            kind="forbidden",
            message="This assistant does not take a client selection.",
        )
    tool = registry.get(tool_name)
    if tool is None:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool_name,
            kind="unknown_tool",
            message=f"There is no tool named {tool_name!r}. Use one of the tools offered.",
        )

    off_surface = _refuse_off_surface(tool)
    if off_surface is not None:
        return off_surface

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

    path_values, query = _split_args(tool, parsed.model_dump())
    query = _clamp_limit(tool, query)
    url = tool.path.format(**path_values)

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool_name, kind="timeout", message="The turn ran out of time before this call."
        )

    # **THE THIRD AND LAST HTTP-CLIENT CONSTRUCTION SITE IN `app/core/ai/`**, and
    # `tests/test_ai_boundaries.py` asserts that count as an EQUALITY so a fourth
    # is a decision somebody makes rather than a thing that happens. Exactly one
    # header is set, and it carries the PORTAL principal's own token unmodified,
    # so `resolve_portal_principal` and `require_portal_permission` re-run and
    # `portal_session` pins both GUCs inside the route.
    try:
        async with httpx.AsyncClient(
            transport=_transport(app),
            base_url=LOOPBACK_BASE_URL,
            timeout=min(MAX_TOOL_CALL_SECONDS, remaining),
        ) as client:
            response = await client.get(url, params=query, headers={"Authorization": bearer})
    except httpx.TimeoutException:
        return ToolError(  # i18n-exempt: model-facing, tool result read by the model
            tool=tool_name,
            kind="timeout",
            message="That lookup took too long and was abandoned. The rest of the answer stands.",
        )
    except Exception as exc:  # noqa: BLE001 - totality is the point; see executor_staff
        return _failed(tool_name, exc, "issuing")

    error = map_status(tool, response.status_code)
    if error is not None:
        return error

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
    # `mutating` is never passed. This persona proposes NOTHING at any dial
    # position (§0.9), so there is no path here that could set it.
    return ToolResult(
        payload=payload,
        status=response.status_code,
        result_count=result_count,
        truncated=truncated,
    )
