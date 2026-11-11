"""What each sign-in step needs from the session it is called with.

- pending: the password is proved and the second factor is not (a session the
  door refuses; `sessions.resolve` returns it with `factor_done=False`);
- full: the factor is proved (what the door admits);
- fresh: full, and a factor verified in the last ten minutes (design §2.5).

And the WebAuthn relying party: the console's host and origin
(`csrf.console_host` / `csrf.console_origin`, Task 6), from one setting and
never the request's Host header.
"""

from datetime import UTC, datetime

from fastapi import Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.csrf import console_host, console_origin
from app.core.auth.keys import AuthKeysUnavailable
from app.core.auth.refusals import AUTH_REQUIRED, STEP_UP_REQUIRED, AuthRefusal
from app.core.auth.sessions import SessionPrincipal, resolve, step_up_fresh
from app.core.config import Settings
from app.core.i18n import tr
from app.core.identity import ACTIVE_STATUS, CONTACT_KIND, STAFF_KIND
from app.models import User


async def keys_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """OpenBao did not answer (Task 2's `AuthKeysUnavailable`): 503, one body
    whatever was being done (plan decision 13). Nothing about the account,
    the request or the transport goes back."""
    assert isinstance(exc, AuthKeysUnavailable)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "code": "sign_in_unavailable",
            "detail": tr("signing in is not available right now; try again in a moment"),
        },
    )


def relying_party(settings: Settings) -> tuple[str, str]:
    """(rp_id, expected origin); 503 without `ITS_PUBLIC_CONSOLE_URL`."""
    host, origin = console_host(settings), console_origin(settings)
    if host is None or origin is None:
        raise AuthRefusal(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="sign_in_unavailable",
            detail=tr(
                "signing in is not set up on this installation: its console address is not "
                "configured"
            ),
        )
    return host, origin


def sign_in_again() -> AuthRefusal:
    """401 `auth_required`: the session a step needs no longer exists."""
    return AuthRefusal(
        status.HTTP_401_UNAUTHORIZED, code=AUTH_REQUIRED, detail=tr("sign in to do this")
    )


async def session_of(request: Request) -> SessionPrincipal:
    principal = await resolve(request)
    if principal is None:
        raise sign_in_again()
    return principal


def require_pending(principal: SessionPrincipal) -> None:
    if principal.factor_done:
        raise AuthRefusal(
            status.HTTP_409_CONFLICT,
            code="already_signed_in",
            detail=tr("you are already signed in"),
        )


def require_full(principal: SessionPrincipal) -> None:
    if not principal.factor_done:
        raise AuthRefusal(
            status.HTTP_403_FORBIDDEN,
            code="factor_required",
            detail=tr("confirm your second factor first"),
        )


def require_fresh(principal: SessionPrincipal) -> None:
    """Task 6's rule (`deps.require_step_up`) for a step that holds a
    principal and no `AuthContext`: an enrolment from a full session."""
    require_full(principal)
    if not step_up_fresh(principal, datetime.now(UTC)):
        raise AuthRefusal(
            status.HTTP_403_FORBIDDEN,
            code=STEP_UP_REQUIRED,
            detail=tr("confirm your second factor again to do this"),
        )


async def signed_user(session: AsyncSession, principal: SessionPrincipal) -> User:
    """The session's person, still active and of the session's own kind: a
    technician or, since own authentication phase 2, a client's contact (plan
    decision D25). `its_auth_session` already refuses a session whose kind is
    not its person's; this reads the same rule again in the caller's
    transaction, where a disabled person or a kind changed since is caught."""
    user = (
        await session.execute(
            select(User).where(User.id == principal.user_id, User.org_id == principal.org_id)
        )
    ).scalar_one_or_none()
    if (
        user is None
        or user.kind != principal.kind
        or user.kind not in (STAFF_KIND, CONTACT_KIND)
        or user.status != ACTIVE_STATUS
    ):
        raise sign_in_again()
    return user
