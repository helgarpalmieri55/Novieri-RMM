"""RFC 6749 error bodies for the OAuth endpoints (design §5; plan D13).

`{"error": <code>, "error_description": <sentence>}`, RFC 6749 §5.2, with
`Cache-Control: no-store` and `Pragma: no-cache` on every answer (§5.1: "in
any response containing tokens, credentials, or other sensitive
information").

NOT TRANSLATED, ON PURPOSE. §5.2 addresses `error_description` to "the client
developer" and limits it to `%x20-21 / %x23-5B / %x5D-7E`: no accent, no
quote, no backslash. So each error is a `Problem` constant here, in English,
and a route raises `OAuthError(BAD_CODE)` -- no call site holds an English
literal. `OAuthError` refuses a description outside the set when it is built,
and `tests/test_oauth_primitives.py` builds every constant, so a bad one fails
in the suite, never in front of a client. What a PERSON reads (the consent
screen, `/mcp`, Settings) goes through `tr()` as always.
"""

import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from starlette.requests import Request
from starlette.responses import JSONResponse

#: RFC 6749 §5.2's character set for `error` and `error_description`.
ALLOWED = re.compile(r"[\x20-\x21\x23-\x5b\x5d-\x7e]+")
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_NO_STORE_KEYS = frozenset(k.lower() for k in NO_STORE)


class Problem(NamedTuple):
    error: str
    description: str
    status: int = 400


class OAuthError(Exception):
    """One RFC 6749 error, answered by `oauth_error_handler`."""

    def __init__(self, problem: Problem, *, headers: Mapping[str, str] | None = None) -> None:
        if not ALLOWED.fullmatch(problem.error) or not ALLOWED.fullmatch(problem.description):
            raise ValueError(  # i18n-exempt: programming error, never reaches a person
                "an OAuth error is ASCII without quotes or backslashes (RFC 6749 §5.2)"
            )
        super().__init__(problem.error)
        self.problem = problem
        self.headers = dict(headers or {})


def no_store(content: Mapping[str, Any], status_code: int = 200) -> JSONResponse:
    """A JSON answer that no cache keeps (RFC 6749 §5.1)."""
    return JSONResponse(dict(content), status_code=status_code, headers=NO_STORE)


async def oauth_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, OAuthError)
    return JSONResponse(
        {"error": exc.problem.error, "error_description": exc.problem.description},
        status_code=exc.problem.status,
        headers={
            **{k: v for k, v in exc.headers.items() if k.lower() not in _NO_STORE_KEYS},
            **NO_STORE,
        },
    )


# --------------------------------------------------------------------------- #
# The errors. RFC 6749 §5.2 and RFC 7591 §3.2.2 codes; RFC 8707 §2's
# `invalid_target`. `temporarily_unavailable` and `access_denied` are §4.1.2.1's,
# reused for the refusals RFC 7591 gives no code for.
# --------------------------------------------------------------------------- #

UNAVAILABLE = Problem(
    "temporarily_unavailable",
    "the authorization server is not configured on this installation",
    503,
)
KEYS_UNAVAILABLE = Problem(
    "temporarily_unavailable",
    "the signing keys are not available right now; try again shortly",
    503,
)
REGISTRATION_CLOSED = Problem("access_denied", "client registration is closed on this server", 403)
REGISTRATIONS_FULL = Problem(
    "temporarily_unavailable",
    "this server holds as many client registrations as it allows; try again later",
    503,
)
SLOW_DOWN = Problem(
    "temporarily_unavailable", "too many registrations from this network; try again later", 429
)
BUSY = Problem(
    "temporarily_unavailable", "too many requests from this network; try again shortly", 429
)
TOO_LARGE = Problem("invalid_request", "the request body is too large", 413)
NOT_JSON = Problem("invalid_client_metadata", "the registration must be one JSON object")
NO_REDIRECTS = Problem("invalid_redirect_uri", "redirect_uris must list between one and ten URIs")
BAD_REDIRECT = Problem(
    "invalid_redirect_uri",
    "each redirect URI must be ASCII, https or http to 127.0.0.1, [::1] or localhost, "
    "with no fragment and no authorization response parameter in its query",
)
BAD_GRANT_TYPES = Problem(
    "invalid_client_metadata",
    "grant_types must hold authorization_code, and may add refresh_token",
)
BAD_RESPONSE_TYPES = Problem("invalid_client_metadata", "response_types may hold only code")
BAD_NAME = Problem("invalid_client_metadata", "client_name must be text")
NOT_FORM = Problem("invalid_request", "send application/x-www-form-urlencoded")
MALFORMED = Problem("invalid_request", "the request is malformed")
REPEATED = Problem("invalid_request", "a parameter appears more than once")
MISSING = Problem("invalid_request", "a required parameter is missing")
CLIENT_AUTH = Problem(
    "invalid_client",
    "this server has public clients only: send client_id and no client authentication",
    401,
)
UNKNOWN_CLIENT = Problem(
    "invalid_client", "the client is not registered here, or its registration expired"
)
UNSUPPORTED_GRANT = Problem(
    "unsupported_grant_type", "only authorization_code and refresh_token are supported"
)
BAD_CODE = Problem("invalid_grant", "the authorization code is not valid")
BAD_VERIFIER = Problem("invalid_grant", "the code_verifier does not match the code_challenge")
BAD_REFRESH = Problem("invalid_grant", "the refresh token is not valid")
WRONG_CLIENT = Problem("invalid_grant", "the token was issued to another client")
BAD_TARGET = Problem(
    "invalid_target", "the only resource this server issues tokens for is its MCP server"
)
BAD_SCOPE = Problem("invalid_scope", "the only scope this server grants is mcp")
