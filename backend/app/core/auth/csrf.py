"""A console write comes from the console (P2 design §6.4).

THE THREAT. The session is a cookie, and a browser attaches a cookie to a
request another site makes it send. `SameSite=Strict` stops that today, but
OWASP: "Treat SameSite as defense in depth against CSRF, not as a replacement
for a CSRF token" [S6, S63]. So every write that rides on the session cookie
must also prove it came from the console's own page:

1. **`X-ITMS-Request: 1`.** A cross-site page cannot add a custom header
   without a CORS preflight, and this API answers no preflight -- it has no
   CORS middleware (`app/main.py`). OWASP: "All modern browsers designate
   requests with custom headers as "to be preflighted"" [S63].
2. **`Origin` equal to the console's** (`ITS_PUBLIC_CONSOLE_URL`). A write with
   no `Origin`, or with `Origin: null`, is refused: "Missing headers and Origin:
   null are not evidence of a same-origin request" [S63].
3. **`Sec-Fetch-Site`, when the browser sends it, is `same-origin`.**

WHICH REQUESTS. Every method but GET, HEAD and OPTIONS under `/api/v1/` that
carries `__Host-itms_session` -- and every one under `/api/v1/auth/` whether or
not it carries a cookie, because the sign-in routes (Task 7) are where a session
is MADE: a cross-site form that signs the victim's browser into the attacker's
account is login CSRF, and it carries no session cookie yet. A request with a
bearer token and no session cookie is not a browser riding on ambient
authority (the MCP channel's in-process calls, the assistant's loopback):
nothing here applies to it.

WITHOUT `ITS_PUBLIC_CONSOLE_URL` there is no console origin to compare with, so
every such write is refused: a deployment cannot sign in with the platform's
own sessions until it says where the console is. The passkey RP ID needs the
same setting (`console_host`).

A PURE-ASGI MIDDLEWARE, like `app/mcp/guard.py`, registered inside the locale
and security-header layers (`app/main.py`): the 403 is translated and carries
the security headers. And INSIDE `McpNeverExposedGuard`: an in-process MCP call
to a never-exposed route (every `/api/v1/auth/` route) is the guard's to
refuse, so it must never meet this rule's `csrf` first. It reads headers only;
the body is never touched.
"""

from urllib.parse import urlsplit

from starlette._utils import get_route_path
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.auth.refusals import CSRF
from app.core.auth.sessions import SESSION_COOKIE
from app.core.i18n import tr

CSRF_HEADER = "X-ITMS-Request"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
API_PREFIX = "/api/v1/"
SIGN_IN_PREFIX = "/api/v1/auth/"
_DEFAULT_PORTS = {"https": 443, "http": 80}


def _console(settings: object) -> tuple[str, str, int | None] | None:
    raw = (getattr(settings, "public_console_url", None) or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    port = parts.port
    return scheme, host, None if port == _DEFAULT_PORTS[scheme] else port


def console_origin(settings: object) -> str | None:
    """The console's web origin as a browser writes it in `Origin`
    (`https://itms.example.com`, a non-default port kept), or None when
    `ITS_PUBLIC_CONSOLE_URL` is unset or is not an http(s) URL."""
    console = _console(settings)
    if console is None:
        return None
    scheme, host, port = console
    name = f"[{host}]" if ":" in host else host
    return f"{scheme}://{name}" if port is None else f"{scheme}://{name}:{port}"


def console_host(settings: object) -> str | None:
    """The console's host name: the WebAuthn RP ID (design §2.2, Task 5)."""
    console = _console(settings)
    return None if console is None else console[1]


def origin_allowed(origin: str | None, settings: object) -> bool:
    """`Origin` names the console. Absent, `null` or anything else: False."""
    expected = console_origin(settings)
    return expected is not None and origin is not None and origin.strip().lower() == expected


def refusal_reason(connection: HTTPConnection, settings: object) -> str | None:
    """Why this request is not the console's own (`header`, `origin`,
    `fetch-site`), or None. Not logged: the refusal is the response's, and the
    logging kernel's count of call sites (`tests/test_logging_discipline.py`)
    is not moved for a refusal the console already shows."""
    if connection.headers.get(CSRF_HEADER) != "1":
        return "header"
    if not origin_allowed(connection.headers.get("origin"), settings):
        return "origin"
    site = connection.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        return "fetch-site"
    return None


class RequireConsoleRequestHeader:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        path = get_route_path(scope)
        if not path.startswith(API_PREFIX):
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        if SESSION_COOKIE not in connection.cookies and not path.startswith(SIGN_IN_PREFIX):
            await self.app(scope, receive, send)
            return
        reason = refusal_reason(connection, scope["app"].state.settings)
        if reason is None:
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {
                "detail": tr("this request was refused because it did not come from the console"),
                "code": CSRF,
            },
            status_code=403,
        )
        await response(scope, receive, send)
