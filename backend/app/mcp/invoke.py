"""One MCP tool call, run in-process through the whole API as the person (design §4.3).

NOTHING IS REIMPLEMENTED HERE. The route runs exactly as it does for the
console -- authentication, tenancy (RLS), RBAC, validation, its own audit --
through the full ASGI stack (`httpx.ASGITransport` over the FastAPI app with
every middleware), carrying the person's own bearer token and language. What
this module adds is only what sits around the call:

1. the arguments are checked against the tool's input schema first, and a
   call that fails it never reaches the route;
2. the URL is built from the route's own path template. Quoting is NOT what
   keeps a path value inside its segment: the ASGI scope carries the DECODED
   path, so a quoted `%2F` is a `/` by the time the router sees it. A path
   value that is empty, `.`, `..`, or contains `/` or `\\` is refused before
   any request, and the in-process wrapper refuses any request whose method
   and decoded path do not match the tool's own route template;
3. the request is marked as an MCP call by an ASGI SCOPE EXTENSION
   (`app.core.mcp_channel`), never a header: a network client cannot set a
   scope key. The API then accepts only the MCP audience, refuses the
   never-exposed routes a second time, and tags every audit event the route
   writes with `via: "mcp"` (`app.mcp.guard`);
4. the JSON result passes the scrub (design §5.3): a secret-looking key that
   is not on the reviewed allowlist BLOCKS THE WHOLE RESULT, never redacted;
5. a 2xx must be `application/json`; a status that is not 2xx becomes the
   API's own `detail`, framed per status, and a structured detail passes the
   scrub too;
6. the wait is bounded by `TOOL_TIMEOUT_S`. A READ that overruns is
   cancelled. A WRITE never is: it runs in the MCP server's own task group and
   only the waiting stops, because cancelling a write half way through leaves
   an outcome nobody can know -- the person is told exactly that.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import anyio
import httpx
from anyio.abc import TaskGroup
from jsonschema import Draft202012Validator
from starlette.routing import compile_path
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.i18n import tr
from app.core.mcp_channel import EXTENSION_KEY, McpCall
from app.mcp.scrub_allowlist import first_unreviewed_secret_path, secret_key_path

if TYPE_CHECKING:
    from app.mcp.catalog import Tool
    from app.mcp.server import McpPrincipal

logger = logging.getLogger(__name__)

#: Seconds one tool call may take, end to end (design §4.3). A module constant
#: so a test can shorten it.
TOOL_TIMEOUT_S = 30.0
#: The in-process call never leaves the process; `.invalid` (RFC 6761) makes
#: sure that, if it ever tried to, it could not resolve anywhere.
LOOPBACK = "http://mcp-loopback.invalid"
#: The longest a WRITE that overran `TOOL_TIMEOUT_S` may keep running in the
#: background before it is cancelled after all (seconds from its start).
WRITE_HARD_CAP_S = 300.0
#: Formats (`uuid`, `date`, `email`, ...) are checked as a cheap first layer:
#: a `{expense_id}` that is not a UUID never becomes a request. Formats whose
#: checker needs an optional library jsonschema does not have are skipped,
#: and the API validates them anyway. A module constant so a test can turn it
#: off to reach the layers behind it.
_FORMAT_CHECKER: Any = Draft202012Validator.FORMAT_CHECKER
#: How many schema errors one refusal names. Enough to fix a call; a bounded
#: answer to an argument object built to produce thousands.
_MAX_SCHEMA_ERRORS = 5


@dataclass(frozen=True)
class InvokeOutcome:
    """What the person saw: the status (the route's, or the one this module
    stands in for it -- 422 for arguments refused before the route, 504 for a
    timeout), the JSON payload on success, or the error text. `blocked` is set
    when the scrub withheld a result the route did return; `outcome_unknown`
    when a write was still running when the wait ended."""

    status: int
    payload: Any | None
    error: str | None
    blocked: bool = False
    outcome_unknown: bool = False


class _Marked:
    """`app`, serving only this tool's own route, with every HTTP request
    marked as this MCP call.

    A request whose method or DECODED path does not match the tool's route
    template is refused here -- the app is never called -- and `refused` is
    set. Defence in depth behind `path_value_problems`: whatever a future
    change does to how the URL is built, this call can reach one route only."""

    def __init__(self, app: ASGIApp, call: McpCall, tool: Tool) -> None:
        self._app = app
        self._call = call
        self._route = f"{tool.method} {tool.path}"
        self._method = tool.method
        self._path = compile_path(tool.path)[0]
        self.refused = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if scope.get("method") != self._method or not self._path.fullmatch(scope.get("path", "")):
            self.refused = True
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        scope = {
            **scope,
            "extensions": {
                **(scope.get("extensions") or {}),
                # The ROUTE travels too: the never-exposed guard refuses the
                # request unless the router would dispatch it to exactly this
                # route (`app.mcp.guard.dispatched_route`).
                EXTENSION_KEY: {
                    "azp": self._call.azp,
                    "tool": self._call.tool,
                    "route": self._route,
                },
            },
        }
        await self._app(scope, receive, send)


def _marked(app: ASGIApp, call: McpCall, tool: Tool) -> _Marked:
    return _Marked(app, call, tool)


def schema_errors(tool: Tool, args: dict[str, Any]) -> list[dict[str, Any]]:
    """The arguments' schema violations, FastAPI-shaped (`loc`, `msg`), at most
    `_MAX_SCHEMA_ERRORS` of them; empty when the arguments are valid."""
    validator = Draft202012Validator(tool.input_schema, format_checker=_FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(args), key=lambda e: list(map(str, e.absolute_path)))
    return [{"loc": list(e.absolute_path), "msg": e.message} for e in errors[:_MAX_SCHEMA_ERRORS]]


def path_value_problems(tool: Tool, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Path values that could leave their own segment once decoded: empty,
    `.` or `..` (dot segments, which URL normalisation removes), or containing
    `/` or `\\`. FastAPI-shaped, like `schema_errors`."""
    problems = []
    for name in tool.path_params:
        value = str(args[name])
        if value in ("", ".", "..") or "/" in value or "\\" in value:
            problems.append(
                {
                    "loc": [name],
                    "msg": tr("a path value cannot be empty, '.', '..', or contain / or \\"),
                }
            )
    return problems


def _detail(response: httpx.Response) -> Any:
    """The API's own `detail`, or the status phrase when the body has none."""
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return response.reason_phrase


def _as_text(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    # A STRUCTURED DETAIL PASSES THE SCRUB TOO: an error body is a result the
    # AI app reads. Any secret-looking key withholds the whole detail.
    try:
        hit = secret_key_path(detail)
    except TypeError:
        hit = "(unreadable)"
    if hit is not None:
        return tr("the error's details were withheld because they contained a secret-looking field")
    return json.dumps(detail)


def _is_json(response: httpx.Response) -> bool:
    media_type = str(response.headers.get("content-type", "")).split(";", 1)[0]
    return media_type.strip().lower() == "application/json"


def error_text(status: int, detail: Any) -> str:
    """The MCP error for a non-2xx status: the API's own words, framed so an AI
    app knows what to do next (design §4.3)."""
    text = _as_text(detail)
    if status == 401:
        return tr("re-authenticate: {detail}", detail=text)
    if status == 404:
        return tr("not found: {detail}", detail=text)
    if status == 422:
        return tr("invalid arguments: {detail}", detail=text)
    if status in (403, 409, 429):
        return text
    if status >= 500:
        # The detail of a 5xx is never shown: it is the platform's fault, not
        # something the person or the AI app can act on, and its body is
        # whatever the failing layer wrote.
        return tr("the platform could not complete this action (status {status})", status=status)
    return tr("the action failed (status {status}): {detail}", status=status, detail=text)


def _timeout_seconds() -> str:
    return f"{TOOL_TIMEOUT_S:g}"


async def invoke(
    *,
    app: ASGIApp,
    tool: Tool,
    args: dict[str, Any],
    principal: McpPrincipal,
    background: TaskGroup | None = None,
) -> InvokeOutcome:
    """Run `tool` as `principal`. `background` is the MCP server's own task
    group, in which a write runs so that a timeout never cancels it; a write
    without one is refused (a wiring defect), never run cancellable."""
    problems = schema_errors(tool, args) or path_value_problems(tool, args)
    if problems:
        return InvokeOutcome(422, None, error_text(422, problems))
    read_only = tool.method == "GET"
    if not read_only and background is None:
        raise RuntimeError(  # i18n-exempt: wiring defect, never shown to a person
            "a write tool needs the MCP server's background task group"
        )

    path_values = {p: quote(str(args[p]), safe="") for p in tool.path_params}
    query = {q: args[q] for q in tool.query_params if args.get(q) is not None}
    url = tool.path.format(**path_values)
    headers = {"Authorization": principal.bearer, "Accept": "application/json"}
    if principal.accept_language:
        headers["Accept-Language"] = principal.accept_language
    # THE SAME CLIENT NARROWING THE MCP REQUEST CARRIED (MSP clients spec
    # §2.2). Without it the route would re-admit the person un-narrowed: never
    # wider than their grants, but wider than the scope this call was listed,
    # checked and audited under.
    if principal.requested_client is not None:
        headers["X-Client-Id"] = str(principal.requested_client)
    body = args["body"] if tool.has_body and "body" in args else None
    marked = _marked(app, McpCall(azp=principal.claims.azp, tool=tool.name), tool)

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            # A route that raises still answers with a 500 here (Starlette's
            # error middleware writes one), rather than surfacing its
            # exception inside the MCP handler.
            transport=httpx.ASGITransport(app=marked, raise_app_exceptions=False),
            base_url=LOOPBACK,
        ) as client:
            return await client.request(tool.method, url, params=query, headers=headers, json=body)

    if read_only:
        # `fail_after`, not the client's own timeout: `ASGITransport` never
        # touches a socket, so httpx's timeouts are never consulted.
        try:
            with anyio.fail_after(TOOL_TIMEOUT_S):
                response = await request()
        except TimeoutError:
            return InvokeOutcome(
                504,
                None,
                tr("the action took longer than {seconds} seconds", seconds=_timeout_seconds()),
            )
    else:
        assert background is not None
        finished = anyio.Event()
        box: dict[str, Any] = {}

        async def run() -> None:
            # Runs in the server's task group: an exception here would take
            # the whole MCP server down, so every one is caught and kept.
            try:
                with anyio.move_on_after(WRITE_HARD_CAP_S) as cap:
                    box["response"] = await request()
                if cap.cancelled_caught:
                    box["cancelled"] = True
                    logger.warning(
                        "mcp write tool call cancelled at the hard cap", extra={"tool": tool.name}
                    )
                elif box.get("abandoned"):
                    logger.info(
                        "mcp write tool call finished after its caller stopped waiting",
                        extra={"tool": tool.name, "status": box["response"].status_code},
                    )
            except Exception as exc:  # noqa: BLE001 - reported below, never lost
                box["error"] = exc
                logger.warning(
                    "mcp write tool call raised", extra={"tool": tool.name}, exc_info=True
                )
            finally:
                finished.set()

        background.start_soon(run)
        with anyio.move_on_after(TOOL_TIMEOUT_S):
            await finished.wait()
        if not finished.is_set() or box.get("cancelled"):
            # Still running (it goes on, up to `WRITE_HARD_CAP_S`), or cut at
            # the cap: either way nobody can say what it did.
            box["abandoned"] = True
            return InvokeOutcome(
                504,
                None,
                tr(
                    "the action is still running after {seconds} seconds and its outcome is "
                    "unknown; check before retrying",
                    seconds=_timeout_seconds(),
                ),
                outcome_unknown=True,
            )
        if "error" in box:
            raise box["error"]
        response = box["response"]

    if marked.refused:
        return InvokeOutcome(
            422,
            None,
            error_text(422, tr("the arguments do not address this tool's own route")),
        )
    status = response.status_code
    if not 200 <= status < 300:
        if status >= 500:
            logger.warning(
                "mcp tool call failed in the api", extra={"tool": tool.name, "status": status}
            )
        return InvokeOutcome(status, None, error_text(status, _detail(response)))
    if not response.content:
        return InvokeOutcome(status, {}, None)
    try:
        if not _is_json(response):
            raise ValueError("not application/json")  # i18n-exempt: caught below
        payload = response.json()
    except ValueError:
        # Files and streams are never tools (design §5.2); a 2xx that is not
        # JSON is a route the catalogue should not have exposed.
        logger.warning(
            "mcp tool answered a 2xx that is not json",
            extra={"tool": tool.name, "status": status},
        )
        return InvokeOutcome(
            status,
            None,
            tr("the action returned something other than JSON, which an AI app cannot receive"),
            blocked=True,
        )

    # THE SCRUB. A hit not reviewed for this route blocks the whole result.
    # `secret_key_paths` refuses anything but decoded JSON with a TypeError;
    # that, too, is a block.
    try:
        hit = first_unreviewed_secret_path(f"{tool.method} {tool.path}", payload)
    except TypeError:
        hit = "(unreadable)"
    if hit is not None:
        logger.warning(
            "mcp tool result blocked by the scrub", extra={"tool": tool.name, "field": hit}
        )
        return InvokeOutcome(
            status,
            None,
            tr("blocked: the result contained a secret-looking field at {path}", path=hit),
            blocked=True,
        )
    return InvokeOutcome(status, payload, None)
