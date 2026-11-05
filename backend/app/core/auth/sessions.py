"""The console's sessions: server-side, named by a cookie (P2 design §2.4, §6.4, §6.5).

A SESSION IS A ROW, THE COOKIE ONLY NAMES IT. `auth_sessions` (Task 1) holds
who, since when, until when and how far the sign-in got; the browser holds 32
random bytes in `__Host-itms_session` (`HttpOnly; Secure; SameSite=Strict;
Path=/`) and the row holds only their SHA-256 [S1 §5.1.1, S6, S36]. A leaked
database row cannot be replayed as a cookie, and the JavaScript of the page
cannot read the cookie at all.

THE STATES, AND WHO MOVES A SESSION BETWEEN THEM:

- **signed in with the password only** (`factor_at` NULL): `start_session(...,
  factor_done=False)`. Good for the sign-in routes of Task 7 and nothing else:
  `app/api/deps.py::admit` refuses it.
- **signed in** (`factor_at` set): `mark_factor` after the second factor --
  which also CHANGES THE IDENTIFIER ("El identificador de sesión cambia al
  iniciar sesión y al confirmar el segundo factor", design §2.4; OWASP: "The
  session ID must be renewed or regenerated ... after any privilege level
  change" [S6]) -- or `start_session(..., factor_done=True)` on a remembered
  device.
- **idle** (no request for longer than the policy allows): `resolve` answers
  None, but the row is not ended. Within the maximum, the password alone
  brings it back (`resumable`, then `start_session(..., resume=...)`): NIST
  800-63B-4 §2.2.3 lets AAL2 "reauthenticate using only a successful password
  ... in conjunction with the session secret" [S1].
- **ended** (`ended_at` set): signed out, replaced, or cut by an administrator,
  a password change or a disabled account. Final.

RECENT CONFIRMATION (design §2.5) is `step_up_at`: the last time a second factor
was actually verified in this session. A sign-in on a remembered device verified
none, so it starts without one -- which is what keeps the vault asking
(falsifier 5).

WHERE THE ORGANISATION COMES FROM. A cookie arrives before anybody knows the
organisation, and RLS shows nothing without one. `resolve` asks the one
`SECURITY DEFINER` function Task 1 made for this, `its_auth_session(hash)`,
which answers that one row's fields and nothing else (design §6.2); every
other read here runs with the organisation pinned. `resolve` is in
`ALL_SCOPE_ALLOWLIST`: a staff person's session row has no client, so it is
read and touched under every client, as `admit` reads the person.

THE OTHER FUNCTIONS TAKE THE CALLER'S SESSION, pinned to the organisation and
with every client in scope (Tasks 7 and 8 open it in a reviewed function of
their own), and never audit or mail: the act that calls them does, once.
"""

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection, Request
from starlette.responses import Response

from app.core.auth.session_policy import read_policy
from app.core.client_address import client_address
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import User
from app.models.auth import AuthSession

logger = logging.getLogger("its.auth.sessions")

SESSION_COOKIE = "__Host-itms_session"
DEVICE_COOKIE = "__Host-itms_device"

#: Random bytes behind a session or device cookie (256 bits, design §6.4).
TOKEN_BYTES = 32
#: `secrets.token_urlsafe(32)`: 43 characters of base64url, no padding. A cookie
#: of any other shape names no session and is not hashed.
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")
#: How recent a second factor must be for the acts of design §2.5.
STEP_UP_WINDOW = timedelta(minutes=10)
#: `last_seen_at` is written at most this often: an idle limit measured to the
#: minute does not need a database write on every request.
TOUCH_EVERY = timedelta(seconds=60)
#: What a request may say about itself, at most, in the session list.
_USER_AGENT_MAX = 300

EndReason = Literal[
    "signed_out",
    "signed_out_everywhere",
    "replaced",
    "resumed",
    "password_changed",
    "factors_reset",
    "disabled",
    "ended_by_admin",
    "device_revoked",
]


@dataclass(frozen=True)
class SessionPrincipal:
    """A live session, as the door sees it. `factor_done`: the second factor
    (or a remembered device) completed the sign-in. `step_up_at`: the last
    second factor actually verified in this session (design §2.5)."""

    session_id: UUID
    user_id: UUID
    org_id: UUID
    kind: str
    factor_done: bool
    step_up_at: datetime | None
    email: str
    display_name: str
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def new_token() -> str:
    """A fresh cookie value (32 random bytes, base64url)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> bytes:
    """What the database keeps of a cookie value: its SHA-256."""
    return hashlib.sha256(token.encode("ascii")).digest()


def cookie_token(connection: HTTPConnection, name: str) -> str | None:
    """The cookie's value when it has the shape of one of ours, else None."""
    raw = connection.cookies.get(name)
    return raw if raw is not None and _TOKEN_SHAPE.fullmatch(raw) else None


def _set(response: Response, name: str, value: str, max_age: int) -> None:
    # `__Host-` binds the cookie to this origin: Secure, Path=/ and no Domain
    # [S36]. HttpOnly keeps it from the page's JavaScript; Strict keeps it off
    # every cross-site request [S6]. A browser refuses a `__Host-` cookie set
    # any other way, the deletion included -- hence one helper for both.
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _seconds_until(moment: datetime) -> int:
    return max(0, int((moment - _now()).total_seconds()))


def set_session_cookie(response: Response, token: str, *, expires_at: datetime) -> None:
    """The session cookie, expiring with the session [S1 §5.1.1]."""
    _set(response, SESSION_COOKIE, token, _seconds_until(expires_at))


def clear_session_cookie(response: Response) -> None:
    _set(response, SESSION_COOKIE, "", 0)


def set_device_cookie(response: Response, token: str, *, expires_at: datetime) -> None:
    """The remembered-device cookie (design §2.6), expiring with the device."""
    _set(response, DEVICE_COOKIE, token, _seconds_until(expires_at))


def clear_device_cookie(response: Response) -> None:
    _set(response, DEVICE_COOKIE, "", 0)


def step_up_fresh(principal: SessionPrincipal, now: datetime) -> bool:
    """A second factor was verified in this session in the last 10 minutes."""
    return principal.step_up_at is not None and now - principal.step_up_at <= STEP_UP_WINDOW


async def resolve(connection: HTTPConnection, *, touch: bool = True) -> SessionPrincipal | None:
    """The live session the request's cookie names, or None.

    None for: no cookie, a cookie of the wrong shape, an unknown hash, an
    ended session, one past its maximum (`expires_at`), one idle for longer
    than the organisation's policy allows, and a person whose account is not
    `active` -- disabling somebody stops their sessions on the next request,
    whatever else forgot to end them (and a live session in that state is
    logged once per request as the defect it is). `touch` renews
    `last_seen_at` (at most once a minute). The console WebSocket's re-check
    passes False, so an open tab is not activity; the remote screen's
    watchdog passes True, so watching an open screen is (coordinator
    decision, Task 6 review: a person at a remote desktop sends no HTTP)."""
    token = cookie_token(connection, SESSION_COOKIE)
    if token is None:
        return None
    state = connection.app.state
    async with AsyncSession(state.app_engine) as lookup:
        row = (
            await lookup.execute(
                text(
                    "SELECT session_id, user_id, org_id, kind, status, last_seen_at, "
                    "expires_at, factor_at, step_up_at, ended_at "
                    "FROM its_auth_session(:token_hash)"
                ),
                {"token_hash": token_hash(token)},
            )
        ).one_or_none()
    now = _now()
    if row is None or row.ended_at is not None or row.expires_at <= now:
        return None
    if row.status != "active":
        # A 401 / 4401 at the door, and no admission-refusal line (core/
        # identity, "THE STATUS RULE ON A SESSION DOOR"). But disabling and
        # deleting end every session in the same transaction, so a LIVE one
        # here means a lifecycle path forgot to: a defect, said once per
        # request that meets it. The row id and the status; never the token.
        logger.warning(
            "a live session belongs to a person who is not active; it was not ended",
            extra={"session_id": row.session_id, "status": row.status},
        )
        return None
    async with org_session(
        row.org_id, engine=state.app_engine, clients=ClientScope.all()
    ) as session:
        policy = await read_policy(session, row.org_id)
        if now - row.last_seen_at > policy.idle_limit(row.kind):
            return None
        person = (
            await session.execute(
                select(User.email, User.display_name).where(User.id == row.user_id)
            )
        ).one()
        if touch and now - row.last_seen_at >= TOUCH_EVERY:
            await session.execute(
                update(AuthSession)
                .where(AuthSession.id == row.session_id, AuthSession.ended_at.is_(None))
                .values(last_seen_at=now)
            )
    return SessionPrincipal(
        session_id=row.session_id,
        user_id=row.user_id,
        org_id=row.org_id,
        kind=row.kind,
        factor_done=row.factor_at is not None,
        step_up_at=row.step_up_at,
        email=person.email,
        display_name=person.display_name,
        expires_at=row.expires_at,
    )


async def start_session(
    session: AsyncSession,
    user: User,
    *,
    kind: str,
    request: Request,
    factor_done: bool,
    trusted_device_id: UUID | None,
    resume: AuthSession | None = None,
) -> tuple[AuthSession, str]:
    """A new session for `user` and its cookie value (set it with
    `set_session_cookie`). A new identifier every time (design §2.4).

    The session the request's cookie still names, if any, is ENDED
    (`replaced`): a sign-in never inherits an identifier somebody else may have
    planted (session fixation, [S6]).

    `factor_done`: the second factor was verified now (`step_up_at` = now), or
    -- with `trusted_device_id` -- a remembered device stood in for it, which
    verifies nothing (`step_up_at` NULL). `resume`: an idle session of this
    person from this browser (`resumable`); the new one keeps its maximum and
    its second factor, and it is ended (`resumed`)."""
    if kind != user.kind:
        raise ValueError("the session's kind is the person's")  # i18n-exempt: a caller's defect
    now = _now()
    if resume is not None:
        if resume.user_id != user.id or resume.ended_at is not None or resume.expires_at <= now:
            raise ValueError("not a resumable session")  # i18n-exempt: a caller's defect
        expires_at, factor_at, step_up_at = resume.expires_at, resume.factor_at, None
        trusted_device_id = resume.trusted_device_id
        resume.ended_at, resume.ended_reason = now, "resumed"
        # Written now, so the fixation sweep below no longer matches it.
        await session.flush()
    else:
        policy = await read_policy(session, user.org_id)
        expires_at = now + policy.lifetime(kind, remembered=trusted_device_id is not None)
        factor_at = now if factor_done else None
        step_up_at = now if factor_done and trusted_device_id is None else None
    previous = cookie_token(request, SESSION_COOKIE)
    if previous is not None:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.token_hash == token_hash(previous), AuthSession.ended_at.is_(None))
            .values(ended_at=now, ended_reason="replaced")
        )
    if factor_at is not None:
        user.last_login_at = now
    token = new_token()
    # A request built by hand (a test, the recovery command) may carry no app.
    app = request.scope.get("app")
    address = client_address(request, () if app is None else app.state.trusted_proxies)
    row = AuthSession(
        org_id=user.org_id,
        user_id=user.id,
        token_hash=token_hash(token),
        kind=kind,
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
        factor_at=factor_at,
        step_up_at=step_up_at,
        trusted_device_id=trusted_device_id,
        user_agent=request.headers.get("user-agent", "")[:_USER_AGENT_MAX],
        ip="" if address is None else str(address),
    )
    session.add(row)
    await session.flush()
    return row, token


async def resumable(
    session: AsyncSession, request: Request, user_id: UUID
) -> AuthSession | None:
    """The session this browser's cookie names, when it is `user_id`'s,
    completed its second factor and has neither ended nor reached its maximum
    -- typically one `resolve` no longer admits because it went idle (module
    docstring). The password alone may resume it; anything else signs in from
    the start."""
    token = cookie_token(request, SESSION_COOKIE)
    if token is None:
        return None
    now = _now()
    row = (
        await session.execute(
            select(AuthSession).where(
                AuthSession.token_hash == token_hash(token),
                AuthSession.user_id == user_id,
                AuthSession.ended_at.is_(None),
                AuthSession.expires_at > now,
                AuthSession.factor_at.is_not(None),
            )
        )
    ).scalar_one_or_none()
    return row


async def mark_factor(session: AsyncSession, session_id: UUID) -> str:
    """The second factor was verified: the session is signed in, recently
    confirmed, and gets a NEW identifier, returned (design §2.4)."""
    now = _now()
    token = new_token()
    row = (
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.ended_at.is_(None))
            .values(
                token_hash=token_hash(token), factor_at=now, step_up_at=now, last_seen_at=now
            )
            .returning(AuthSession.user_id)
        )
    ).one_or_none()
    if row is None:
        raise LookupError("no live session")  # i18n-exempt: the caller answers 401
    await session.execute(update(User).where(User.id == row.user_id).values(last_login_at=now))
    return token


async def rotate(session: AsyncSession, session_id: UUID) -> str:
    """A NEW identifier for a live session, returned; nothing else changes --
    not `factor_at`, `step_up_at` nor the expiry. For a password change (final
    review B-I1): OWASP "The session ID must be renewed or regenerated ...
    after any privilege level change" [S6], and a password change is what a
    person does who fears their cookie was copied. NOT at a step-up: the
    screen's watchdog re-reads the cookie of its handshake
    (`modules/remote/tunnel.py`), and a new one there would cut the screen at
    every vault checkout."""
    token = new_token()
    row = (
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.ended_at.is_(None))
            .values(token_hash=token_hash(token))
            .returning(AuthSession.id)
        )
    ).one_or_none()
    if row is None:
        raise LookupError("no live session")  # i18n-exempt: the caller answers 401
    return token


async def mark_step_up(session: AsyncSession, session_id: UUID) -> None:
    """A second factor was verified again (design §2.5)."""
    now = _now()
    await session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.ended_at.is_(None))
        .values(step_up_at=now, last_seen_at=now)
    )


async def end(session: AsyncSession, session_id: UUID, reason: EndReason) -> bool:
    """End one session; False when it had already ended."""
    result = await session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.ended_at.is_(None))
        .values(ended_at=_now(), ended_reason=reason)
        .returning(AuthSession.id)
    )
    return result.one_or_none() is not None


async def end_all_for_user(
    session: AsyncSession, user_id: UUID, reason: EndReason, except_id: UUID | None = None
) -> int:
    """End every live session of a person but `except_id`; how many ended."""
    query = (
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.ended_at.is_(None))
        .values(ended_at=_now(), ended_reason=reason)
        .returning(AuthSession.id)
    )
    if except_id is not None:
        query = query.where(AuthSession.id != except_id)
    return len((await session.execute(query)).all())
