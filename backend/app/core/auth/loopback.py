"""The assistant's internal token (autenticación propia design §5.6).

THE PROBLEM IT SOLVES. The in-app assistant reads by re-entering the whole API
in-process (`app.core.ai.executor_staff`, `app.core.ai.executor_portal`), so
`admit()`, `require_permission` and RLS decide what it may see exactly as they
would for the person. It used to carry the person's own Keycloak bearer back
in. A console signed in by cookie has no bearer, and the session cookie must
never leave the browser's own requests -- so each turn mints this instead.

WHAT IT IS. A JWT, ES256, signed by the platform's key in OpenBao
(`app.core.auth.keys.sign_jwt`, which signs through `sign_es256`), audience
`loopback`, issuer `urn:itms:loopback`, `typ` `loopback+jwt` (RFC 8725 §3.11),
five minutes, naming the person (`sub`), their organisation (`org`) and THE
SESSION it was minted from (`sid`). It carries no permission of its own, and
it is not a service token: its authority is one live person's session, and
the route it reaches runs `admit()`, `require_permission` /
`require_portal_permission` and RLS with that person's rows.

WHERE IT IS ACCEPTED, AND NOWHERE ELSE. `verify_loopback` admits it only when
ALL of these hold, and refuses with one 401 otherwise:

1. the request is in-process: its ASGI scope carries `LOOPBACK_EXTENSION`,
   which only `LoopbackMarked` sets -- a request from the network reaches the
   app through uvicorn, and nothing a client sends can add a scope key (the
   mechanism `app.core.mcp_channel` uses for MCP);
2. it says so: exactly one `X-ITMS-Loopback: 1`;
3. it is not an MCP call: the two in-process channels never meet;
4. it only reads: GET or HEAD;
5. the token verifies against the published JWKS, with `alg`, `typ`, `aud`,
   `iss`, `exp` and `iat` fixed, and lives no longer than five minutes;
6. its session is still live: not ended, not past its maximum, not idle past
   the organisation's limit, second factor done.

A request that says it is the assistant's call (the header) is admitted here
or by nothing: it never falls through to the cookie or the bearer
(`app.api.deps._session_principal`).

The principal it yields never carries a recent confirmation (`step_up_at`
is None): §2.5's actions belong to the person, never to the assistant. And a
request this token admitted mints no other (`loopback_authorization`).

THE IDLE LIMIT IS RE-CHECKED, though a loopback read renews nothing (it is the
person's own turn, not new activity). The token is minted by the turn's own
request, which `resolve` admitted; but `resolve` writes `last_seen_at` at most
once a minute (`sessions.TOUCH_EVERY`), so "just renewed" is up to a minute
stale, and the shortest idle setting (5 minutes, `auth_org_settings` CHECK
5..480) is the token's own lifetime. Reading the policy again costs one query
and makes "while the session lives" mean what `resolve` means by it.
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import jwt
from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.auth import keys
from app.core.auth.session_policy import read_policy
from app.core.auth.sessions import SessionPrincipal
from app.core.client_scope import ClientScope
from app.core.i18n import tr
from app.core.mcp_channel import mcp_call_from_scope
from app.core.tenancy import org_session
from app.models import User
from app.models.auth import AuthSession

logger = logging.getLogger("its.auth.loopback")

LOOPBACK_AUDIENCE = "loopback"
LOOPBACK_ISSUER = "urn:itms:loopback"
LOOPBACK_TYP = "loopback+jwt"
#: Seconds. Design §5.6: "un token interno de 5 min".
LOOPBACK_TTL_S = 300
LOOPBACK_HEADER = "X-ITMS-Loopback"
#: The ASGI scope extension that proves a request never left the process.
LOOPBACK_EXTENSION = "itms.loopback"

_READS = frozenset({"GET", "HEAD"})
_REQUIRED_CLAIMS = ["exp", "iat", "sub", "org", "sid", "aud", "iss"]

#: Why the door refused a token: the server log's `extra`, never a response
#: and never the token. A closed set, so the log can only ever say one of these.
RefusalReason = Literal[
    "not an in-process call",
    "malformed loopback header",
    "an MCP call carries no loopback token",
    "the loopback token only reads",
    "no token",
    "the token does not verify",
    "the session is not live",
    "a loopback call mints no loopback token",
]


def _now() -> int:
    """A named seam, so a test can mint a token in the past."""
    return int(time.time())


def _keys_unavailable() -> HTTPException:
    # Nothing is admitted and nothing is minted without the key: a 503, so the
    # assistant reports a failed lookup rather than an expired session. The
    # sentence `/.well-known/jwks.json` answers in the same case.
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        tr("The signing keys are not available right now; try again in a moment."),
    )


async def mint_loopback(principal: SessionPrincipal) -> str:
    """A token for one turn of this person's assistant, bound to their session."""
    if not principal.factor_done:
        raise ValueError(  # i18n-exempt: a wiring defect, never shown to a person
            "a session without its second factor mints no loopback token"
        )
    issued = _now()
    payload: dict[str, Any] = {
        "iss": LOOPBACK_ISSUER,
        "aud": LOOPBACK_AUDIENCE,
        "sub": str(principal.user_id),
        "org": str(principal.org_id),
        "sid": str(principal.session_id),
        "kind": principal.kind,
        "iat": issued,
        "exp": issued + LOOPBACK_TTL_S,
    }
    # Task 2's `sign_jwt` puts `alg`, the signing key's `kid` and this `typ` in
    # the signed header, and signs once more if OpenBao rotated in between: the
    # one copy of that logic in the code base.
    return await keys.sign_jwt(payload, typ=LOOPBACK_TYP)


def is_loopback_token(authorization: str) -> bool:
    """Whether an `Authorization` value carries a token minted here. Reads the
    UNVERIFIED header only to choose what to send; the door verifies it all."""
    if not authorization.startswith("Bearer "):
        return False
    try:
        header = jwt.get_unverified_header(authorization.removeprefix("Bearer "))
    except jwt.InvalidTokenError:
        return False
    return header.get("typ") == LOOPBACK_TYP


def loopback_headers(authorization: str) -> dict[str, str]:
    """The headers a loopback call carries: the token, and the claim that this
    is the assistant's own call when (and only when) it is an internal token."""
    headers = {"Authorization": authorization}
    if is_loopback_token(authorization):
        headers[LOOPBACK_HEADER] = "1"
    return headers


class LoopbackMarked:
    """`app`, with every HTTP request marked as the assistant's in-process call.

    The mark is inert by itself: the door looks for it only on a request that
    also says `X-ITMS-Loopback: 1`, and only to refuse a request that says so
    without it. The executors wrap the app they were handed in this and
    nothing else does."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope = {
                **scope,
                "extensions": {**(scope.get("extensions") or {}), LOOPBACK_EXTENSION: {}},
            }
        await self._app(scope, receive, send)


def loopback_app(app: ASGIApp) -> LoopbackMarked:
    return LoopbackMarked(app)


def claims_loopback(request: Request) -> bool:
    """Whether the request says it is the assistant's own call. Such a request
    is admitted by `verify_loopback` or by nothing."""
    return LOOPBACK_HEADER in request.headers


def _refused(*, reason: RefusalReason) -> HTTPException:
    # One answer for every reason: the caller learns nothing about which check
    # failed. The reason -- one of `RefusalReason`'s fixed phrases, never the
    # token -- goes to the server log in `extra` (tests/test_logging_discipline.py);
    # the response carries only the translated sentence below.
    logger.warning("loopback token refused", extra={"reason": reason})
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, tr("the assistant's internal token was refused")
    )


@dataclass(frozen=True)
class _Claims:
    user_id: UUID
    org_id: UUID
    session_id: UUID


async def _signing_key(kid: str) -> jwt.PyJWK | None:
    """The published key named `kid`. Read once more before giving up: after
    a rotation another api process may hold the public keys for up to
    `keys.PUBLIC_KEYS_TTL_S`, and a token signed with the new version names a
    `kid` that copy does not have yet."""
    for refresh in (False, True):
        try:
            return jwt.PyJWKSet.from_dict(await keys.jwks(refresh=refresh))[kid]
        except (KeyError, jwt.PyJWKSetError):
            continue
    return None


async def _verified_claims(token: str) -> _Claims | None:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        return None
    kid = header.get("kid")
    if header.get("alg") != "ES256" or header.get("typ") != LOOPBACK_TYP:
        return None
    if not isinstance(kid, str) or not kid:
        return None
    key = await _signing_key(kid)
    if key is None:
        return None
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["ES256"],
            audience=LOOPBACK_AUDIENCE,
            issuer=LOOPBACK_ISSUER,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.InvalidTokenError:
        return None
    try:
        if int(claims["exp"]) - int(claims["iat"]) > LOOPBACK_TTL_S:
            return None
        return _Claims(
            user_id=UUID(str(claims["sub"])),
            org_id=UUID(str(claims["org"])),
            session_id=UUID(str(claims["sid"])),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _live_session(engine: AsyncEngine, claims: _Claims) -> SessionPrincipal | None:
    """The token's session, if it is still live -- read under every client,
    because the person's scope is computed later from these rows (reviewed in
    `ALL_SCOPE_ALLOWLIST`). The org comes from a token this platform signed."""
    now = datetime.now(UTC)
    async with org_session(claims.org_id, engine=engine, clients=ClientScope.all()) as session:
        found = (
            await session.execute(
                select(AuthSession, User.email, User.display_name)
                .join(User, User.id == AuthSession.user_id)
                .where(
                    AuthSession.id == claims.session_id,
                    AuthSession.user_id == claims.user_id,
                    AuthSession.org_id == claims.org_id,
                )
            )
        ).one_or_none()
        if found is None:
            return None
        row, email, display_name = found
        if row.ended_at is not None or row.expires_at <= now or row.factor_at is None:
            return None
        policy = await read_policy(session, claims.org_id)
        if now - row.last_seen_at > policy.idle_limit(row.kind):
            return None
    return SessionPrincipal(
        session_id=row.id,
        user_id=row.user_id,
        org_id=row.org_id,
        kind=row.kind,
        factor_done=True,
        # Decision (3) of the plan: never a recent confirmation.
        step_up_at=None,
        email=email,
        display_name=display_name,
        expires_at=row.expires_at,
    )


async def verify_loopback(request: Request) -> SessionPrincipal:
    """Admit an in-process assistant call, or refuse it with a 401. Never
    returns None: a request that claims to be the assistant falls through to
    no other door."""
    if LOOPBACK_EXTENSION not in (request.scope.get("extensions") or {}):
        raise _refused(reason="not an in-process call")
    if request.headers.getlist(LOOPBACK_HEADER) != ["1"]:
        raise _refused(reason="malformed loopback header")
    try:
        on_mcp = mcp_call_from_scope(request.scope) is not None
    except ValueError:
        on_mcp = True
    if on_mcp:
        raise _refused(reason="an MCP call carries no loopback token")
    if request.method not in _READS:
        raise _refused(reason="the loopback token only reads")
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise _refused(reason="no token")
    try:
        claims = await _verified_claims(authorization.removeprefix("Bearer "))
    except keys.AuthKeysUnavailable as exc:
        raise _keys_unavailable() from exc
    if claims is None:
        raise _refused(reason="the token does not verify")
    principal = await _live_session(request.app.state.app_engine, claims)
    if principal is None:
        raise _refused(reason="the session is not live")
    return principal


async def loopback_authorization(request: Request) -> str:
    """The `Authorization` value an assistant turn's loopback calls carry.

    TRANSITION (plan decision D21), removed by Task 13: a request the
    console's Keycloak bearer admitted has no session, and its own header is
    forwarded as before. Every other admitted request has a session principal
    (`admit()` leaves it on `request.state`) and gets a token of its own --
    the request's own `Authorization`, if it brought one, is never read."""
    principal: SessionPrincipal | None = getattr(request.state, "session_principal", None)
    if principal is None:
        return request.headers.get("Authorization", "")
    if getattr(request.state, "via_loopback", False):
        # A turn route reached by the assistant itself: no chains.
        raise _refused(reason="a loopback call mints no loopback token")
    try:
        return f"Bearer {await mint_loopback(principal)}"
    except keys.AuthKeysUnavailable as exc:
        raise _keys_unavailable() from exc
