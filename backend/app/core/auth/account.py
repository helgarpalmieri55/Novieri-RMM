"""My security: the caller's own factors, remembered devices and sessions
(design §2.4-§2.6).

PERSONAL, SO IT RUNS UNDER EVERY CLIENT, for the reason `own_profile.py`
gives: a technician's auth rows are global rows of an O table, which a
per-client scope may read and not change, and which the empty scope of a
person with no grant yet cannot even read. Every query below is the caller's
OWN rows, by the `user_id` of the session the door admitted -- never a request
field: an id in a path (`/me/passkeys/{id}`) is matched together with that
`user_id`, so another person's id is a 404.

THE CHANGES ASK FOR THE RECENT CONFIRMATION (design §2.5) at the route
(`deps.require_step_up`, Task 6): password, a factor removed, new recovery
codes. Forgetting a device and ending a session take something away from the
caller's own access and do not.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Request, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.auth import notices, staff_password
from app.core.auth.devices import revoke, revoke_all_for_user
from app.core.auth.factors import count_failure, summary
from app.core.auth.failures import check_allowed, record_success
from app.core.auth.guards import signed_user
from app.core.auth.keys import UnknownKeyVersion
from app.core.auth.passwords import verify_password
from app.core.auth.recovery import replace_codes
from app.core.auth.refusals import AuthRefusal
from app.core.auth.session_policy import read_policy
from app.core.auth.sessions import (
    DEVICE_COOKIE,
    SessionPrincipal,
    cookie_token,
    end_all_for_user,
    token_hash,
)
from app.core.auth.sessions import end as end_session
from app.core.auth.totp import remove_totp as drop_totp
from app.core.client_scope import ClientScope
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.core.vault import VaultService
from app.models import User
from app.models.auth import AuthPasskey, AuthPassword, AuthSession, AuthTrustedDevice


@dataclass(frozen=True)
class PasskeyView:
    id: UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    backup_state: bool


@dataclass(frozen=True)
class DeviceView:
    id: UUID
    label: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    current: bool


@dataclass(frozen=True)
class SessionView:
    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str
    ip: str
    current: bool


@dataclass(frozen=True)
class MySecurity:
    has_totp: bool
    passkeys: list[PasskeyView]
    recovery_codes_left: int
    trusted_devices: list[DeviceView]
    sessions: list[SessionView]


@asynccontextmanager
async def _own_session(
    principal: SessionPrincipal, request: Request
) -> AsyncIterator[AsyncSession]:
    """See the module docstring: the caller's own auth rows, every client."""
    engine: AsyncEngine = request.app.state.app_engine
    async with org_session(principal.org_id, engine=engine, clients=ClientScope.all()) as session:
        yield session


def _vault(request: Request) -> VaultService:
    vault: VaultService = request.app.state.vault
    return vault


async def _record(session: AsyncSession, user: User, action: str, meta: dict[str, str]) -> None:
    """The one audit wrapper of this file (plan decision 12)."""
    await audit_service.record(
        session,
        org_id=user.org_id,
        actor_type="user",
        actor_id=user.id,
        action=action,
        target_type="user",
        target_id=str(user.id),
        metadata=meta,
    )


def _not_found(what: str) -> AuthRefusal:
    return AuthRefusal(status.HTTP_404_NOT_FOUND, code="not_found", detail=what)


async def my_security(request: Request, principal: SessionPrincipal) -> MySecurity:
    now = datetime.now(UTC)
    # Which listed device is this browser: compared by the cookie's hash, so a
    # read does not renew the device as `devices.recognised` would.
    here = cookie_token(request, DEVICE_COOKIE)
    here_hash = token_hash(here) if here is not None else None
    async with _own_session(principal, request) as session:
        user = await signed_user(session, principal)
        _has_passkey, has_totp, codes = await summary(session, user.id)
        keys = (
            await session.execute(
                select(AuthPasskey)
                .where(AuthPasskey.user_id == user.id)
                .order_by(AuthPasskey.created_at)
            )
        ).scalars().all()
        devices = (
            await session.execute(
                select(AuthTrustedDevice)
                .where(
                    AuthTrustedDevice.user_id == user.id,
                    AuthTrustedDevice.revoked_at.is_(None),
                    AuthTrustedDevice.expires_at > now,
                )
                .order_by(AuthTrustedDevice.created_at.desc())
            )
        ).scalars().all()
        sessions = (
            await session.execute(
                select(AuthSession)
                .where(
                    AuthSession.user_id == user.id,
                    AuthSession.ended_at.is_(None),
                    AuthSession.expires_at > now,
                )
                .order_by(AuthSession.last_seen_at.desc())
            )
        ).scalars().all()
    return MySecurity(
        has_totp=has_totp,
        passkeys=[
            PasskeyView(
                id=k.id,
                name=k.name,
                created_at=k.created_at,
                last_used_at=k.last_used_at,
                backup_state=k.backup_state,
            )
            for k in keys
        ],
        recovery_codes_left=codes,
        trusted_devices=[
            DeviceView(
                id=d.id,
                label=d.label,
                created_at=d.created_at,
                expires_at=d.expires_at,
                last_used_at=d.last_used_at,
                current=here_hash is not None and d.token_hash == here_hash,
            )
            for d in devices
        ],
        sessions=[
            SessionView(
                id=s.id,
                created_at=s.created_at,
                last_seen_at=s.last_seen_at,
                expires_at=s.expires_at,
                user_agent=s.user_agent,
                ip=s.ip,
                current=s.id == principal.session_id,
            )
            for s in sessions
        ],
    )


async def _detach_from_device(session: AsyncSession, principal: SessionPrincipal) -> None:
    """The caller's own session, when a remembered device started it, stops
    depending on that device before every device is revoked: Task 6's
    `revoke_all_for_user` ends the sessions a revoked device started, and the
    caller is not one of the OTHER sessions a password change ends (design
    §2.8, OWASP [S8]). It keeps no more than an ordinary session's lifetime
    from its sign-in, so no session outlives the trust that gave it its length.

    THE ANCHOR IS `factor_at`, not `created_at`. `sessions.start_session` sets
    a new session's `expires_at` from `now`, which is its `created_at` and, on
    a remembered device, its `factor_at` too; a RESUMED session inherits the
    `expires_at` and the `factor_at` of the one it resumed, so there only
    `factor_at` still names the sign-in the lifetime was counted from."""
    policy = await read_policy(session, principal.org_id)
    ordinary = policy.lifetime(principal.kind, remembered=False)
    await session.execute(
        update(AuthSession)
        .where(
            AuthSession.id == principal.session_id,
            AuthSession.trusted_device_id.is_not(None),
        )
        .values(
            trusted_device_id=None,
            expires_at=func.least(AuthSession.expires_at, AuthSession.factor_at + ordinary),
        )
    )


async def change_password(
    request: Request, principal: SessionPrincipal, *, current: str, new: str
) -> None:
    """The current password again (OWASP [S5]: "require the current credentials
    for an account before updating ... the user's password"), then: the other
    sessions end and every remembered device is forgotten (design §2.6). The
    caller's own session stays, detached from any device first
    (`_detach_from_device`).

    One transaction per attempt (Task 4's advisory lock): check, verify, count
    or change, and the refusal raised after the block commits the count."""
    changed = False
    async with _own_session(principal, request) as session:
        user = await signed_user(session, principal)
        stored = await session.get(AuthPassword, user.id)
        state = await check_allowed(session, user.id, "password")
        if stored is not None and state.allowed:
            # Plan decision 16: an over-long current password is a wrong one,
            # counted, and never handed to the pepper (`SUBMITTED_MAX`).
            matches: bool | None = False
            if len(current) <= staff_password.SUBMITTED_MAX:
                try:
                    matches = await verify_password(
                        user.org_id, current, stored.hash, stored.pepper_version
                    )
                except UnknownKeyVersion:
                    # A verifier under a pepper version the key backend does
                    # not hold can never verify: refused, and not counted --
                    # the person did not fail (`flows._password_matches`).
                    matches = None
            if matches:
                # A refused new password raises its 422 here, inside the block
                # and BEFORE anything below has run: nothing is written, the
                # failure count is left as it was (`record_success` comes after
                # the rule), and the rollback only releases the advisory lock.
                await staff_password.require_acceptable(
                    new,
                    email=user.email,
                    display_name=user.display_name,
                    org_name=await staff_password.organization_name(session, user.org_id),
                )
                await staff_password.store(session, org_id=user.org_id, user_id=user.id, plain=new)
                await record_success(session, user.id, "password")
                await end_all_for_user(
                    session, user.id, "password_changed", except_id=principal.session_id
                )
                await _detach_from_device(session, principal)
                await revoke_all_for_user(session, user.id)
                await _record(session, user, "auth.password_changed", {"via": "self"})
                await notices.tell(
                    session, user, "password_changed", vault=_vault(request), actor_id=user.id
                )
                changed = True
            elif matches is False:
                await count_failure(session, user, "password", vault=_vault(request))
    if not changed:
        raise AuthRefusal(
            status.HTTP_403_FORBIDDEN,
            code="invalid_credentials",
            detail=tr("the current password is not right"),
        )


def _last_factor() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_409_CONFLICT,
        code="last_factor",
        detail=tr("this is your only second factor; add another one before removing it"),
    )


async def remove_passkey(request: Request, principal: SessionPrincipal, passkey_id: UUID) -> None:
    async with _own_session(principal, request) as session:
        user = await signed_user(session, principal)
        key = (
            await session.execute(
                select(AuthPasskey).where(
                    AuthPasskey.id == passkey_id, AuthPasskey.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if key is None:
            raise _not_found(tr("no such passkey"))
        keys = (
            await session.execute(
                select(func.count()).select_from(AuthPasskey).where(AuthPasskey.user_id == user.id)
            )
        ).scalar_one()
        _has_passkey, has_totp, _codes = await summary(session, user.id)
        if keys == 1 and not has_totp:
            raise _last_factor()
        await session.delete(key)
        await _record(session, user, "auth.factor_removed", {"factor": "passkey", "name": key.name})
        await notices.tell(
            session,
            user,
            "factor_removed",
            vault=_vault(request),
            actor_id=user.id,
            factor="passkey",
        )


async def remove_totp(request: Request, principal: SessionPrincipal) -> None:
    async with _own_session(principal, request) as session:
        user = await signed_user(session, principal)
        has_passkey, has_totp, _codes = await summary(session, user.id)
        if not has_totp:
            raise _not_found(tr("that sign-in method is not set up on this account"))
        if not has_passkey:
            raise _last_factor()
        await drop_totp(session, user.id)
        await _record(session, user, "auth.factor_removed", {"factor": "totp"})
        await notices.tell(
            session, user, "factor_removed", vault=_vault(request), actor_id=user.id, factor="totp"
        )


async def new_recovery_codes(request: Request, principal: SessionPrincipal) -> list[str]:
    async with _own_session(principal, request) as session:
        user = await signed_user(session, principal)
        codes = await replace_codes(session, org_id=user.org_id, user_id=user.id)
        await _record(session, user, "auth.factor_added", {"factor": "recovery_codes"})
        await notices.tell(
            session,
            user,
            "factor_added",
            vault=_vault(request),
            actor_id=user.id,
            factor="recovery_codes",
        )
    return codes


async def forget_device(request: Request, principal: SessionPrincipal, device_id: UUID) -> None:
    async with _own_session(principal, request) as session:
        user = await signed_user(session, principal)
        if not await revoke(session, user.id, device_id):
            raise _not_found(tr("no such remembered device"))
        await _record(session, user, "auth.device_revoked", {"by": "self"})


async def end_my_session(request: Request, principal: SessionPrincipal, session_id: UUID) -> None:
    async with _own_session(principal, request) as session:
        user = await signed_user(session, principal)
        mine = (
            await session.execute(
                select(AuthSession.id).where(
                    AuthSession.id == session_id,
                    AuthSession.user_id == user.id,
                    AuthSession.ended_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if mine is None:
            raise _not_found(tr("no such session"))
        await end_session(session, session_id, "signed_out")
        await _record(session, user, "auth.sessions_ended", {"by": "self", "scope": "one"})
