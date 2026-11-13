"""The consent screen's routes (own authentication design §5.2; plan
2026-11-06, Task 8):

    POST /api/v1/oauth/authorize/details    what an AI app's request asks
    POST /api/v1/oauth/authorize/decision   the person's answer: a code, or access_denied

UNGATED BY DESIGN (`tests/test_route_gating.py::UNGATED_BY_DESIGN`): the
caller's own console session -- not a token (`_console_ctx`: the door must
have admitted a console principal) -- and the deployment's organization,
`mcp.use` and the org switch checked in the service, so a refusal can go back to the AI app
(`consent._refusal`, plan D11). The CSRF rule (`app/core/auth/csrf.py`) holds
both: a POST under `/api/v1` on the session cookie.

NOT THE ASSISTANT'S INTERNAL TOKEN, and not because of `_console_ctx`: that
token yields a console principal too (`app/api/deps.py`). It is kept out
because `verify_loopback` admits it only for GET or HEAD
(`app/core/auth/loopback.py`, its rule 4) and both routes are POST; a request
that says it is the assistant's never falls through to the cookie (re-review
f3-2, M-5).

APPROVING NEEDS A RECENT SECOND FACTOR (operator decision C1, 2026-11-06):
`require_step_up` before the decision when `approve`, on a remembered device
too; declining does not, and is never answered with a redirect.

NEVER AN AI APP'S (1c): the decision answers a one-time code that becomes the
person's MCP tokens, so it is in `NEVER_EXPOSED` and `NO_STORE_ROUTES`, and
refused on the MCP channel twice; the details are not a tool (`NOT_FOR_MCP`),
and need the console session the channel never has.

THE HANDLERS ARE THIN: the rules live in `app.core.auth.oauth.consent`, the
only caller of `oauth_session` here (`tests/test_client_catalog.py::
OAUTH_SESSION_USERS`), which also takes the person's row first in an
approval (operator decision D2).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, bound_auth_context, require_step_up
from app.core.auth.guards import sign_in_again
from app.core.auth.oauth import consent
from app.core.auth.oauth.errors import no_store

router = APIRouter(prefix="/oauth", tags=["oauth"])


class ConsentQueryIn(BaseModel):
    """The query of `/oauth/authorize`, as the browser has it, without the `?`."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(max_length=8192)


class ConsentDecisionIn(ConsentQueryIn):
    approve: bool


async def _console_ctx(ctx: AuthContext = Depends(bound_auth_context)) -> AuthContext:
    """The door admitted somebody; these routes need it to have been the
    console session (`ctx.principal`), as `/me/security` does."""
    if ctx.principal is None:
        raise sign_in_again()
    return ctx


@router.post("/authorize/details")
async def authorize_details(
    body: ConsentQueryIn, request: Request, ctx: AuthContext = Depends(_console_ctx)
) -> JSONResponse:
    """What an AI app's authorization request asks of the signed-in person."""
    return no_store(await consent.details(request, ctx, query=body.query))


@router.post("/authorize/decision")
async def authorize_decision(
    body: ConsentDecisionIn, request: Request, ctx: AuthContext = Depends(_console_ctx)
) -> JSONResponse:
    """The person's answer: back to the app with a one-time code, or shown
    with the way back and `access_denied`."""
    if body.approve:
        require_step_up(ctx)
    return no_store(await consent.decide(request, ctx, query=body.query, approve=body.approve))
