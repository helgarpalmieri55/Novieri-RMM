"""Response security headers (defense in depth).

The operator API returns JSON and the SPA is served same-origin behind Traefik,
so TLS/HSTS live at the edge. These headers harden the application tier:
- X-Content-Type-Options: nosniff on every response (no MIME sniffing).
- Cache-Control: no-store (and Pragma: no-cache) on every answer that carries
  a secret once (`NO_STORE_ROUTES`, final review C-M9), so it is never kept
  by a browser or an intermediary: a credential's plaintext, a licence key,
  enrollment material, and own authentication's TOTP secret, recovery codes
  and invitation link. These are mostly POSTs on HTTPS, which caches do not
  normally keep; the header makes the intent explicit instead of relying on
  that. `tests/test_security_headers.py` lists them, and checks that every
  never-exposed MCP route whose reason says it returns a secret is here.
"""

import re
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

#: (method, route template) of every answer that carries a secret once, and
#: what it carries. A template names the route exactly as FastAPI does.
NO_STORE_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/credentials/{credential_id}/checkout"): "a credential's plaintext secret",
    ("GET", "/api/v1/licenses/{license_id}/key"): "a licence's plaintext key",
    ("POST", "/api/v1/enrollment-tokens"): "an enrollment token",
    ("PUT", "/api/v1/sites/{site_id}/install-link"): "an install link's code",
    ("POST", "/api/v1/sites/{site_id}/install-link/regenerate"): "an install link's new code",
    ("POST", "/api/v1/agent/releases/{release_id}/install-command"): "an enrollment token",
    ("POST", "/api/v1/agent/releases/{release_id}/deployment-token"): "an enrollment token",
    ("POST", "/api/v1/auth/enroll/totp/start"): "a new authenticator-app secret",
    ("POST", "/api/v1/auth/enroll/finish"): "the ten recovery codes",
    ("POST", "/api/v1/me/recovery-codes"): "ten new recovery codes",
    ("POST", "/api/v1/users/invite"): "a one-time invitation link, when no mail carried it",
    ("POST", "/api/v1/users/{user_id}/invite/resend"): (
        "a new one-time invitation link, when no mail carried it"
    ),
    ("POST", "/api/v1/clients/{client_id}/contacts/{contact_id}/invite"): (
        "a contact's one-time portal invitation link, when no mail carried it"
    ),
    ("POST", "/api/v1/clients/{client_id}/contacts/{contact_id}/access/reset"): (
        "a contact's new one-time invitation link, when no mail carried it"
    ),
}


def _pattern(template: str) -> re.Pattern[str]:
    """A route template as a whole-path pattern: `{name}` is one segment."""
    parts = re.split(r"(\{[^/{}]+\})", template)
    return re.compile(
        "".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts) + r"\Z"
    )


_NO_STORE: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, _pattern(template)) for method, template in NO_STORE_ROUTES
)


def no_store(method: str, path: str) -> bool:
    """Whether the answer to `method path` is one of `NO_STORE_ROUTES`."""
    return any(method == m and pattern.match(path) for m, pattern in _NO_STORE)


async def security_headers_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if no_store(request.method, request.url.path):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response
