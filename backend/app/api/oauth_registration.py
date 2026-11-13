"""The OAuth server's public face before there is a person (design §5.1,
§5.4; plan 2026-11-06, Task 6):

    GET  /.well-known/oauth-authorization-server   RFC 8414 metadata
    POST /oauth/register                           RFC 7591 open registration

PUBLIC BY NECESSITY, outside `/api/v1` like `/.well-known/jwks.json`: neither
is an MCP tool nor behind the console's door, and Traefik routes both to the
api (`deploy/docker-compose.yml`). Registering grants nothing (§5.4): without
a person's consent there is no token. No CORS (B3-26).

THE LIMITS ON REGISTERING (§5.4, B3-02): only with `ITS_MCP_REGISTRATION=open`
and MCP on (decision C2), 20 an hour per network -- an IPv6 network being its
/64 -- failing closed, 200 live registrations, 16 KiB of JSON, at most ten
redirects; each registration removes up to a hundred idle ones (plan D10).
The rules are `clients.register_new`'s; this file reads the request.

THE METADATA (RFC 8414 §2) names the platform as its own issuer (plan D1);
`none` in `token_endpoint_auth_methods_supported` and
`client_id_metadata_document_supported`, always, are what Claude needs to use
a client metadata document instead of registering [S33]; the registration
endpoint only while registration is offered; `S256` alone in
`code_challenge_methods_supported` [S17, S30]; and RFC 9207's
`authorization_response_iss_parameter_supported`. It is cached for 300
seconds, so turning MCP on or off shows in `registration_endpoint` up to five
minutes late; and a database that does not answer leaves the endpoint out
rather than turning the metadata into a 500 (re-review f3-2, M-2). Being
public and not rate-limited, it reads the database at most once per
`clients.OFFERED_FOR_S` (30 s) per process (fix round 1), which adds up to
that much to the five minutes.

THE NETWORK'S LIMIT IS COUNTED BEFORE ANYTHING IS READ (re-review f3-2, M-2):
its key needs no organization, so a flood of registrations costs Redis, never
the database, as at the token endpoint (Task 7).
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.core.auth.oauth import clients
from app.core.auth.oauth.errors import (
    NOT_JSON,
    REGISTRATION_CLOSED,
    SLOW_DOWN,
    TOO_LARGE,
    UNAVAILABLE,
    OAuthError,
    no_store,
)
from app.core.auth.oauth.policy import (
    AUTHORIZE_PATH,
    GRANT_TYPES,
    JWKS_PATH,
    MAX_REQUEST_BYTES,
    METADATA_PATH,
    REGISTER_PATH,
    REVOKE_PATH,
    SCOPES_SUPPORTED,
    TOKEN_PATH,
    issuer,
    network_of,
    registration_open,
)
from app.core.auth.oauth.store import deployment_org
from app.core.client_address import client_address
from app.core.http import read_bounded_body
from app.core.rate_limit import hit

router = APIRouter()

#: The metadata changes only when the deployment's settings do.
CACHE_CONTROL = "public, max-age=300"


@router.get(METADATA_PATH, include_in_schema=False)
async def authorization_server_metadata(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    iss = issuer(settings)
    if iss is None:
        raise OAuthError(UNAVAILABLE)
    document: dict[str, object] = {
        "issuer": iss,
        "authorization_endpoint": iss + AUTHORIZE_PATH,
        "token_endpoint": iss + TOKEN_PATH,
        "revocation_endpoint": iss + REVOKE_PATH,
        "jwks_uri": iss + JWKS_PATH,
        "scopes_supported": list(SCOPES_SUPPORTED),
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": list(GRANT_TYPES),
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
        "client_id_metadata_document_supported": True,
    }
    try:
        offered = await clients.registration_offered(request.app)
    except (SQLAlchemyError, OSError):
        offered = False  # the metadata still answers; registering would not
    if offered:
        document["registration_endpoint"] = iss + REGISTER_PATH
    return JSONResponse(document, headers={"Cache-Control": CACHE_CONTROL})


@router.post(REGISTER_PATH, include_in_schema=False)
async def register_client(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    if issuer(settings) is None:
        raise OAuthError(UNAVAILABLE)
    if not registration_open(settings):
        raise OAuthError(REGISTRATION_CLOSED)
    network = network_of(client_address(request, request.app.state.trusted_proxies))
    within = await hit(
        request.app.state.redis,
        f"oauth-register:{network}",
        limit=clients.REGISTRATIONS_PER_HOUR,
        window_s=3600,
        fail_closed=True,
    )
    if not within:
        raise OAuthError(SLOW_DOWN, headers={"Retry-After": "3600"})
    org_id = await deployment_org(request.app)
    if org_id is None:
        raise OAuthError(UNAVAILABLE)
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json":
        raise OAuthError(NOT_JSON)
    try:
        raw = await read_bounded_body(request, MAX_REQUEST_BYTES)
    except HTTPException as exc:
        # The API's 413 is a translated `detail`; this answer is RFC 6749's.
        raise OAuthError(TOO_LARGE) from exc
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # RecursionError: 16 KiB of `[` nests past the parser's depth, and is
        # not a ValueError (fix round 1).
        raise OAuthError(NOT_JSON) from exc
    registration = clients.parse_registration(body)
    answer = await clients.register_new(
        request.app, org_id=org_id, registration=registration, now=datetime.now(UTC)
    )
    return no_store(answer, status_code=201)
