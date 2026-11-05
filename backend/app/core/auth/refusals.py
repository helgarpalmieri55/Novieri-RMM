"""The sign-in refusals the console tells apart (P2 phase 1, Task 6).

Every other refusal of the API is `{"detail": "<translated sentence>"}`. These
also carry a `code`, because the console acts on them rather than showing them:
`auth_required` (401) goes to the sign-in page, `step_up_required` (403) asks
for the second factor again (design §2.5), `csrf` (403) is a request the console
did not send (`app/core/auth/csrf.py`). The `detail` stays, translated as
always: a test or a person reading the body still gets a sentence.

`AuthRefusal` is an `HTTPException`, so everything that handles one -- the MCP
server's own wrapper (`app/mcp/server.py`), the in-process tool call's
`error_text` -- keeps working; `app/main.py` registers `auth_refusal_handler`
for it, which Starlette finds before the generic one by walking the MRO.
"""

from collections.abc import Mapping

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

AUTH_REQUIRED = "auth_required"
STEP_UP_REQUIRED = "step_up_required"
CSRF = "csrf"


class AuthRefusal(HTTPException):
    def __init__(
        self,
        status_code: int,
        *,
        code: str,
        detail: str,
        extra: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(status_code, detail)
        self.code = code
        #: Fields of the body beside `detail` and `code` -- `wait_s` on a 429
        #: (own authentication Task 7). Never a secret: numbers only.
        self.extra: dict[str, int] = dict(extra or {})


async def auth_refusal_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AuthRefusal)
    return JSONResponse(
        status_code=exc.status_code,
        content={**exc.extra, "detail": exc.detail, "code": exc.code},
        headers=exc.headers,
    )
