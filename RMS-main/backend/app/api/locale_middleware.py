"""The request's language, from `Accept-Language` (spanish-language spec §3.1).

Follows the `request_id_var` pattern in `request_context.py`: a contextvar set
before `call_next` and reset after it. Registered AFTER
`request_context_middleware` in `create_app`, so (Starlette runs the
last-registered middleware outermost) the language is already set when every
other middleware, dependency, route handler and `add_exception_handler` handler
runs. `call_next` runs the downstream app in a child task that copies the
context at creation, so the value set here is what they read; the reset happens
in this frame, where the token was minted.

Every response also carries `Vary: Accept-Language`, merged into any `Vary` a
route set, because the body now depends on the header.

Not covered: `ServerErrorMiddleware` sits outside every `app.middleware("http")`
layer, so the generic 500 body it builds is English, which is also all it says.
"""

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from app.core.i18n import parse_accept_language, use_locale


async def locale_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    lang = parse_accept_language(request.headers.get("accept-language"))
    with use_locale(lang):
        response = await call_next(request)
    response.headers.setdefault("Content-Language", lang)
    _vary_on_language(response)
    return response


def _vary_on_language(response: Response) -> None:
    """Add `Accept-Language` to `Vary`, keeping every value a route already set
    (`agent_distribution/link_page.py` sets its own). Never overwrites."""
    existing = response.headers.getlist("vary")
    tokens = [t.strip() for value in existing for t in value.split(",") if t.strip()]
    lowered = {t.lower() for t in tokens}
    if "*" in lowered or "accept-language" in lowered:
        return
    response.headers["Vary"] = ", ".join([*tokens, "Accept-Language"])
