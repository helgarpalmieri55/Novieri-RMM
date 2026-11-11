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
from uuid import UUID

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
from app.models.auth import AuthSession


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


def _admitted(user: User | None, principal: SessionPrincipal) -> User:
    if (
        user is None
        or user.kind != principal.kind
        or user.kind not in (STAFF_KIND, CONTACT_KIND)
        or user.status != ACTIVE_STATUS
    ):
        raise sign_in_again()
    return user


async def signed_user(session: AsyncSession, principal: SessionPrincipal) -> User:
    """The session's person, still active and of the session's own kind: a
    technician or, since own authentication phase 2, a client's contact (plan
    decision D25). `its_auth_session` already refuses a session whose kind is
    not its person's; this reads the same rule again in the caller's
    transaction, where a disabled person or a kind changed since is caught.

    A READ: a step that writes the person's rows takes `locked_user` instead."""
    user = (
        await session.execute(
            select(User).where(User.id == principal.user_id, User.org_id == principal.org_id)
        )
    ).scalar_one_or_none()
    return _admitted(user, principal)


async def lock_user_row(session: AsyncSession, user_id: UUID) -> User | None:
    """THE FIRST LOCK OF EVERY TRANSACTION THAT WRITES A PERSON'S ROWS (own
    authentication phase 2, Task 9b): their `users` row FOR NO KEY UPDATE,
    re-read (`populate_existing`), so what a writer committed while this
    waited -- a switch-off, a deletion -- is what the caller then checks.

    THE LOCK ORDER of a person's account, kept by the administrators' acts on
    a person (`contact_access._contact`, the archive's `_lock_contacts`,
    `update_contact`, `user_admin.service._target(lock=True)`), by every use
    of their links (`flows._consumed`) and by their own steps here: THE
    PERSON'S ROW; then, when the transaction takes it, their advisory lock
    (`factors.lock_person`); then their links, sessions, devices and
    credentials -- `users.last_login_at` among them, already held -- and
    LAST the organization's audit chain (`audit_service`'s advisory lock),
    which every transaction of the organization that audits takes, so it is
    never held while waiting on a row. Two transactions that both take the
    row wait for each other here, before either holds anything the other
    needs: no 40P01 (`tests/test_auth_sign_in_races.py`).

    NO KEY UPDATE, not UPDATE: a row inserted meanwhile that refers to the
    person (a session, a link) takes KEY SHARE on it, which this does not
    block. None when the organization has no such row."""
    return (
        await session.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def locked_user(session: AsyncSession, principal: SessionPrincipal) -> User:
    """`signed_user` for a step that writes: the person's row locked first
    (`lock_user_row`), then the same checks -- and the session still live.
    A writer that ended it (a reset of the person's access, a deletion, a
    sign-out everywhere) took the row before ending it, so it has committed
    by now: a step that waited for it must not go on as if signed in. 401
    `auth_required`, as a session ended at `mark_factor` or `mark_step_up`
    already answers."""
    user = await lock_user_row(session, principal.user_id)
    if user is not None and user.org_id != principal.org_id:
        user = None
    user = _admitted(user, principal)
    await require_live(session, principal)
    return user


async def require_live(session: AsyncSession, principal: SessionPrincipal) -> None:
    """The caller's session has not ended since the door (or `resolve`)
    admitted it: 401 `auth_required` otherwise. Read under whatever lock
    the caller holds, so a writer that ended it under the same lock and
    committed is seen (plan 2026-11-06, A2-38; Task 9b)."""
    live = await session.scalar(
        select(AuthSession.id).where(
            AuthSession.id == principal.session_id,
            AuthSession.user_id == principal.user_id,
            AuthSession.ended_at.is_(None),
        )
    )
    if live is None:
        raise sign_in_again()
