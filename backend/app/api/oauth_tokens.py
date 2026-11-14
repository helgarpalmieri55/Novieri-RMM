"""The OAuth server's token and revocation endpoints (design §5.3; plan
2026-11-06, Task 7):

    POST /oauth/token    authorization_code | refresh_token   RFC 6749 §4.1.3, §6
    POST /oauth/revoke   RFC 7009

THE REQUEST IS RFC 6749's: `application/x-www-form-urlencoded` (Anthropic:
"Your /token endpoint must accept Content-Type:
application/x-www-form-urlencoded per RFC 6749 section 4.1.3" [S33]), at most
`MAX_REQUEST_BYTES` (an RFC 6749 413 past it), no parameter twice ("Request
and response parameters MUST NOT be included more than once", §3.2) --
`resource` excepted, which RFC 8707 §2 lets repeat -- and an empty one is
absent ("Parameters sent without a value MUST be treated as if they were
omitted"). Every `resource` must name the MCP resource (`invalid_target`); none
at all is the MCP resource (RFC 8707 §2.1; plan B3-05). `redirect_uri` is
optional (OAuth 2.1-17 §10.2; plan B3-23).

THE CHARACTER SETS (the review of Task 7, fix round 1). `parse_qsl` decodes
any escape, `%00` and `%C3%A9` included, and PostgreSQL refuses a NUL in a
text value: a `client_id` holding one was a 500, and an ERROR line, from two
endpoints anybody may call. So every value these routes read is held to its
set before anything uses it, and one outside it is `invalid_request`
(`MALFORMED`):
- `code_verifier`: RFC 7636 §4.1, `43*128unreserved` (`pkce.valid_verifier`);
- every other one -- `grant_type`, `client_id`, `code`, `redirect_uri`,
  `resource`, `refresh_token`, `scope`, `token` -- VSCHAR, `%x20-7E`
  (`policy.vschar`, which the consent holds its `client_id` to as well): RFC
  6749 Appendix A's set for `client_id` (A.1), `code` (A.11) and
  `refresh_token` (A.17), and a superset of what A.4, A.6, A.10 and RFC 8707
  allow for `scope`, `redirect_uri`, `grant_type` and `resource`, whose own
  checks follow.
A parameter neither route reads -- `token_type_hint`, an unknown one -- is
held to nothing: "The authorization server MUST ignore unrecognized request
parameters" (RFC 6749 §3.2), and RFC 7009 §2.2 ignores the hint.

ONE NETWORK, `TOKEN_REQUESTS_PER_MINUTE` A MINUTE, ACROSS BOTH (RFC 7009 §5;
plan B3-24), counted before anything else is read; it fails open.

PUBLIC CLIENTS ONLY (plan D4): `client_id` in the body, never an
`Authorization` header; one that sends it is `invalid_client` with a 401 and
`WWW-Authenticate` (RFC 6749 §5.2).

NO GRANT BUT THESE TWO: no `client_credentials`, no `password` (RFC 9700: "MUST
NOT be used" [S16]), no implicit. Every token names a person who consented
(invariant 1c(a)).

Outside `/api/v1`: no MCP tool reaches it, no cookie is read, and the CSRF rule
(`app/core/auth/csrf.py`, `/api/v1/` only) has nothing to say about a request
that carries no ambient credential.
"""

from collections.abc import Callable
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.core.auth.keys import AuthKeysUnavailable
from app.core.auth.oauth import grants, pkce
from app.core.auth.oauth.errors import (
    BAD_SCOPE,
    BAD_TARGET,
    BUSY,
    CLIENT_AUTH,
    KEYS_UNAVAILABLE,
    MALFORMED,
    MISSING,
    NO_STORE,
    NOT_FORM,
    REPEATED,
    TOO_LARGE,
    UNAVAILABLE,
    UNSUPPORTED_GRANT,
    OAuthError,
    no_store,
)
from app.core.auth.oauth.policy import (
    MAX_REQUEST_BYTES,
    REVOKE_PATH,
    SCOPE,
    SCOPES_SUPPORTED,
    TOKEN_PATH,
    TOKEN_REQUESTS_PER_MINUTE,
    issuer,
    network_of,
    vschar,
)
from app.core.auth.oauth.store import deployment_org
from app.core.client_address import client_address
from app.core.http import read_bounded_body
from app.core.mcp_channel import mcp_resource
from app.core.rate_limit import hit

router = APIRouter()

Form = dict[str, list[str]]
Charset = Callable[[str], bool]
_GRANTS = frozenset({"authorization_code", "refresh_token"})
#: The limit's window, in seconds, and what `Retry-After` says (B3-24).
_WINDOW_S = 60


async def _within_limit(request: Request) -> None:
    """FAILS OPEN (`rate_limit.hit`'s default, which logs it): what these
    endpoints guard is a 256-bit secret no flood guesses, and without Redis
    the limit is lost, not every AI connection (Task 7, decision 8)."""
    network = network_of(client_address(request, request.app.state.trusted_proxies))
    allowed = await hit(
        request.app.state.redis,
        f"oauth-token:{network}",
        limit=TOKEN_REQUESTS_PER_MINUTE,
        window_s=_WINDOW_S,
    )
    if not allowed:
        raise OAuthError(BUSY, headers={"Retry-After": str(_WINDOW_S)})


async def _ready(request: Request) -> UUID:
    settings = request.app.state.settings
    if issuer(settings) is None or mcp_resource(settings) is None:
        raise OAuthError(UNAVAILABLE)
    org_id = await deployment_org(request.app)
    if org_id is None:
        raise OAuthError(UNAVAILABLE)
    return org_id


async def _form(request: Request) -> Form:
    if "authorization" in request.headers:
        raise OAuthError(CLIENT_AUTH, headers={"WWW-Authenticate": 'Basic realm="itms"'})
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/x-www-form-urlencoded":
        raise OAuthError(NOT_FORM)
    try:
        raw = await read_bounded_body(request, MAX_REQUEST_BYTES)
    except HTTPException as exc:
        # The API's 413 is a translated `detail`; this answer is RFC 6749's.
        raise OAuthError(TOO_LARGE) from exc
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise OAuthError(MALFORMED) from exc
    form: Form = {}
    for name, value in parse_qsl(text, keep_blank_values=True):
        if value:
            form.setdefault(name, []).append(value)
    return form


def _optional(form: Form, name: str, *, charset: Charset = vschar) -> str | None:
    """The one value of `name`, in `charset`; None when absent. Every value
    these routes read passes here (or `_check_resource`), so none outside its
    set reaches a query or a comparison."""
    values = form.get(name, [])
    if len(values) > 1:
        raise OAuthError(REPEATED)
    if not values:
        return None
    if not charset(values[0]):
        raise OAuthError(MALFORMED)
    return values[0]


def _one(form: Form, name: str, *, charset: Charset = vschar) -> str:
    value = _optional(form, name, charset=charset)
    if value is None:
        raise OAuthError(MISSING)
    return value


def _check_resource(form: Form, request: Request) -> None:
    resource = mcp_resource(request.app.state.settings)
    assert resource is not None  # `_ready` refused without it
    values = form.get("resource", [])
    if not all(vschar(value) for value in values):
        raise OAuthError(MALFORMED)
    if not all(grants.names_resource(value, resource) for value in values):
        raise OAuthError(BAD_TARGET)


def _check_scope(form: Form) -> None:
    scope = _optional(form, "scope")
    if scope is not None and not set(scope.split()) <= set(SCOPES_SUPPORTED):
        raise OAuthError(BAD_SCOPE)


@router.post(TOKEN_PATH, include_in_schema=False)
async def token(request: Request) -> JSONResponse:
    await _within_limit(request)
    org_id = await _ready(request)
    form = await _form(request)
    grant_type = _one(form, "grant_type")
    if grant_type not in _GRANTS:
        raise OAuthError(UNSUPPORTED_GRANT)
    client_id = _one(form, "client_id")
    _check_resource(form, request)
    try:
        if grant_type == "authorization_code":
            issued = await grants.exchange_code(
                request.app,
                org_id=org_id,
                client_id=client_id,
                code=_one(form, "code"),
                redirect_uri=_optional(form, "redirect_uri"),
                code_verifier=_one(form, "code_verifier", charset=pkce.valid_verifier),
            )
        else:
            _check_scope(form)
            issued = await grants.refresh_grant(
                request.app,
                org_id=org_id,
                client_id=client_id,
                refresh_token=_one(form, "refresh_token"),
            )
    except AuthKeysUnavailable as exc:
        raise OAuthError(KEYS_UNAVAILABLE) from exc
    return no_store(
        {
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "refresh_token": issued.refresh_token,
            "scope": SCOPE,
        }
    )


@router.post(REVOKE_PATH, include_in_schema=False)
async def revoke(request: Request) -> Response:
    await _within_limit(request)
    org_id = await _ready(request)
    form = await _form(request)
    # `token_type_hint` is read by nobody: "An invalid token type hint value
    # is ignored" (RFC 7009 §2.2), and so is a valid one.
    client_id = _one(form, "client_id")
    presented = _one(form, "token")
    try:
        await grants.revoke_presented(
            request.app, org_id=org_id, client_id=client_id, token=presented
        )
    except AuthKeysUnavailable as exc:
        raise OAuthError(KEYS_UNAVAILABLE) from exc
    return Response(status_code=200, headers=NO_STORE)
