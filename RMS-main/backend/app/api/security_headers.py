"""Response security headers (defense in depth).

The operator API returns JSON and the SPA is served same-origin behind Traefik,
so TLS/HSTS live at the edge. These headers harden the application tier:
- X-Content-Type-Options: nosniff on every response (no MIME sniffing).
- Cache-Control: no-store on any response that can carry a decrypted secret
  (the credential checkout path), so a plaintext secret is never cached by a
  browser or intermediary.
"""

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

# Path suffix whose responses may contain a decrypted secret.
_NO_STORE_SUFFIX = "/checkout"


async def security_headers_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.url.path.endswith(_NO_STORE_SUFFIX):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response
