"""Remembered devices: 15 days without the second factor (P2 design §2.6, decision S2b).

WHAT THE OPERATOR ACCEPTED. NIST SP 800-63B-4 §5.1.1: "Cookies and similar
"remember my browser" features SHALL NOT be used instead of authentication"
[S1]. On a remembered device the platform does NOT meet AAL2 (design §0). The
limits below bound that; they do not remove it.

A remembered device is a row in `auth_trusted_devices` (Task 1) and a second
cookie, `__Host-itms_device`, with the same attributes as the session's
(`app/core/auth/sessions.py::set_device_cookie`) and 32 random bytes of its
own, kept only as a SHA-256. It is bound to ONE person: another account signing
in on the same browser gets no benefit from it.

THE LIMITS, each enforced HERE, so a caller that forgets one cannot undo it:

- it expires after the policy's `remember_device_days`, counted from when it
  was remembered (`expires_at`), and it is refused while the organisation has
  the feature switched off -- at once, for every device;
- it stops at the person's next password change: a device remembered BEFORE
  `auth_passwords.changed_at` is not recognised, whether or not the caller
  remembered to call `revoke_all_for_user` (falsifier 5);
- the person, or an administrator, revokes it (`revoke`), and so does a
  factor reset or disabling the account (`revoke_all_for_user`, Task 8); a
  revoked device also ends the sessions it started;
- the acts of design §2.5 still ask for the second factor: a session started
  on a remembered device has no `step_up_at` (`sessions.start_session`).

Auditing and the notice by email (design §2.6: "cada equipo recordado se audita
y se avisa por correo") are the callers' (Tasks 7 and 8), like everything in
`app/core/auth/sessions.py`.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.auth.session_policy import read_policy
from app.core.auth.sessions import DEVICE_COOKIE, cookie_token, new_token, token_hash
from app.models import User
from app.models.auth import AuthPassword, AuthSession, AuthTrustedDevice

#: The label a person sees in "My trusted devices": the browser's own words,
#: cut short. Not trusted for anything.
_LABEL_MAX = 120


class RememberingOff(Exception):
    """The organisation switched "remember this device" off (design §2.4)."""


def _now() -> datetime:
    return datetime.now(UTC)


async def remember(
    session: AsyncSession, user: User, request: Request
) -> tuple[AuthTrustedDevice, str]:
    """Remember the requesting browser for `user`: the row, and the value for
    `sessions.set_device_cookie(response, token, expires_at=row.expires_at)`.
    Raises `RememberingOff` when the organisation does not allow it."""
    policy = await read_policy(session, user.org_id)
    if not policy.remember_device_enabled:
        raise RememberingOff
    now = _now()
    token = new_token()
    device = AuthTrustedDevice(
        org_id=user.org_id,
        user_id=user.id,
        token_hash=token_hash(token),
        label=request.headers.get("user-agent", "")[:_LABEL_MAX],
        created_at=now,
        expires_at=now + policy.device_lifetime(),
    )
    session.add(device)
    await session.flush()
    return device, token


async def recognised(
    session: AsyncSession, user_id: UUID, request: Request
) -> AuthTrustedDevice | None:
    """The remembered device the request's cookie names, when it is `user_id`'s
    and still valid under every limit of the module docstring; else None. A
    recognised device's `last_used_at` is renewed."""
    token = cookie_token(request, DEVICE_COOKIE)
    if token is None:
        return None
    now = _now()
    device = (
        await session.execute(
            select(AuthTrustedDevice).where(
                AuthTrustedDevice.token_hash == token_hash(token),
                AuthTrustedDevice.user_id == user_id,
                AuthTrustedDevice.revoked_at.is_(None),
                AuthTrustedDevice.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    if device is None:
        return None
    if not (await read_policy(session, device.org_id)).remember_device_enabled:
        return None
    changed_at = (
        await session.execute(
            select(AuthPassword.changed_at).where(AuthPassword.user_id == user_id)
        )
    ).scalar_one_or_none()
    if changed_at is not None and changed_at >= device.created_at:
        return None
    device.last_used_at = now
    await session.flush()
    return device


async def _end_sessions_of(session: AsyncSession, device_ids: list[UUID]) -> None:
    if device_ids:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.trusted_device_id.in_(device_ids), AuthSession.ended_at.is_(None))
            .values(ended_at=_now(), ended_reason="device_revoked")
        )


async def revoke(session: AsyncSession, user_id: UUID, device_id: UUID) -> bool:
    """Revoke one of `user_id`'s remembered devices, and end the sessions it
    started. False when there was no such live device of theirs."""
    revoked = (
        await session.execute(
            update(AuthTrustedDevice)
            .where(
                AuthTrustedDevice.id == device_id,
                AuthTrustedDevice.user_id == user_id,
                AuthTrustedDevice.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
            .returning(AuthTrustedDevice.id)
        )
    ).scalars().all()
    await _end_sessions_of(session, list(revoked))
    return bool(revoked)


async def revoke_all_for_user(session: AsyncSession, user_id: UUID) -> int:
    """Revoke every live remembered device of a person, and end the sessions
    they started; how many were revoked."""
    revoked = (
        await session.execute(
            update(AuthTrustedDevice)
            .where(AuthTrustedDevice.user_id == user_id, AuthTrustedDevice.revoked_at.is_(None))
            .values(revoked_at=_now())
            .returning(AuthTrustedDevice.id)
        )
    ).scalars().all()
    await _end_sessions_of(session, list(revoked))
    return len(revoked)
