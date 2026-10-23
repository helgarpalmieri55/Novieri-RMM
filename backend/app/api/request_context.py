"""Per-request correlation context.

Mirrors `security_headers.py`: a module-level async middleware registered in
`create_app`.

THE REQUEST ID IS ALWAYS MINTED HERE AND AN INBOUND `X-Request-ID` IS IGNORED
(spec §3). Honouring a caller-supplied id would let one org collide with
another's, or inject chosen values into an operator's log search. The minted id
is echoed on the response, so a client can still correlate what it sent.

KNOWN GAP -- the response header is not set for unhandled exceptions. The
header is attached to `response` only after `call_next` returns; if a handler
raises something no exception handler catches, that line never runs and
Starlette's `ServerErrorMiddleware` builds the eventual 500 without it. The
contextvar itself is unaffected -- it is set before `call_next` and reset in a
`finally`, so log lines emitted while handling the failing request still carry
the request id; only the response header is missing, which is a client
convenience, not the log correlation guarantee. Fixing this would require a
pure-ASGI rewrite or an app-wide exception handler, more blast radius than the
gap warrants, so it is left as-is and pinned by
`test_an_unhandled_exception_has_no_request_id_header` in
`tests/test_logging_kernel.py`.

THIS MIDDLEWARE LOGS THE EXCEPTION ITSELF, BEFORE THE CONTEXTVAR RESETS.
`ServerErrorMiddleware` sits OUTSIDE this middleware in the stack, so by the
time it (or uvicorn's access log) writes anything about a 500, the request_id
contextvar this module set has already been reset back to `None` by the
`finally` below -- neither uvicorn's error line nor its access line for that
request carries a `request_id`, and there would otherwise be no correlated log
line for the failure at all. The `except` clause here runs before the
`finally` (ordinary `try/except/finally` semantics), so `logger.exception(...)`
executes while the contextvar is still set, producing exactly one record that
does carry `request_id` -- then the exception is re-raised unchanged so
`ServerErrorMiddleware` still builds the response. The log message is a static
template with the exception attached via `exc_info` (never `str(exc)`), per
§2 of the logging kernel spec.

TASK 3: `capture_exception(exc, where="api")` RUNS IN THIS SAME `except`
CLAUSE, FOR THE SAME REASON `logger.exception` DOES. Error monitoring's whole
design (spec §3) is capture at seams that already exist, and this is the
API's one seam -- inside the correlation contextvars, not after
`ServerErrorMiddleware` has already seen the exception with them reset.
`capture_exception` never raises (spec §3 falsifier #2), so it cannot change
this handler's own re-raise below.

`org_id`/`user_id` ALSO REACH THIS CAPTURE, when the request authenticated --
but NOT via the `request_id_var` CONTEXTVAR PATTERN above, and the reason is
an asyncio Task boundary, not a timing one. Registering a function with
`app.middleware("http")` (this one, and `security_headers_middleware`) wraps
it in Starlette's `BaseHTTPMiddleware`, whose `call_next` runs the ENTIRE
downstream ASGI app -- routing, every dependency (including
`app.api.deps.auth_context`, which resolves org_id/user_id), the endpoint --
inside a SEPARATE task its own internal `anyio.create_task_group()` spawns.
A `contextvars.Token` minted by `bind(org_id=..., user_id=...)` inside that
child task cannot reset the same contextvar from THIS frame (verified: doing
exactly that raises "Token ... was created in a different Context"), and
more fundamentally, a contextvar SET inside a child Task is never visible
back in the parent's frame at all, by design -- so no amount of "when do we
exit the binding" restructuring inside a route dependency can make `org_id_
var.get()`/`user_id_var.get()` return anything but `None` right here, no
matter which dependency style resolves them.

So `auth_context` stashes org_id/user_id on `request.state` instead -- a
PLAIN object attribute, not a contextvar, on the one `Request` object both
this frame and the (separate-Task) dependency graph hold a reference to,
which crosses the Task boundary with no such restriction. This `except`
clause reads them back off `request.state` and enters `bind()` ITSELF, in
THIS frame, scoped tightly around the one `capture_exception` call that
needs them -- no cross-Task handoff of the contextvar binding itself is
attempted or needed. `app.api.deps.require_permission`'s own dependency
binds org_id/user_id as contextvars TOO, separately, for ordinary log lines
emitted during route handling -- that one works with an ordinary `with`
block because everything from its `yield` onward runs in the SAME (child)
Task as the binding itself; see that function's own comment for why the two
mechanisms are not one.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from app.core.errors.capture import capture_exception
from app.core.logging import bind, request_id_var

_HEADER = "X-Request-ID"

logger = logging.getLogger(__name__)


async def request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = uuid.uuid4().hex
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    except Exception as exc:
        # Emitted BEFORE the finally resets the contextvar below, so this is
        # the one log line for an unhandled exception that is guaranteed to
        # carry request_id -- ServerErrorMiddleware and uvicorn's own lines
        # for this request are written after the reset and will not.
        logger.exception("unhandled exception while handling request")
        # `None` when the request never authenticated (`auth_context` never
        # ran) -- matches the "absent, not empty-string" rule the rest of
        # this kernel's correlation ids already follow.
        org_id = getattr(request.state, "org_id", None)
        user_id = getattr(request.state, "user_id", None)
        with bind(org_id=org_id, user_id=user_id):
            capture_exception(exc, where="api")
        raise
    finally:
        request_id_var.reset(token)
    response.headers[_HEADER] = request_id
    return response
