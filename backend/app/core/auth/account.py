"""My security: the caller's own factors, remembered devices and sessions
(design §2.4-§2.6).

PERSONAL. Every query below is the caller's OWN rows, by the `user_id` of the
session the door admitted -- never a request field: an id in a path
(`/me/passkeys/{id}`) is matched together with that `user_id`, so another
person's id is a 404. The SCOPE depends on who the caller is (`_own_session`):

- A TECHNICIAN runs under every client, for the reason `own_profile.py` gives:
  their auth rows are global rows of an O table, which a per-client scope may
  read and not change, and which the empty scope of a person with no grant yet
  cannot even read.
- A CLIENT'S CONTACT (own authentication phase 2, D26: the same routes) runs
  under the scope the door established for them -- their own client, never
  every client (invariant 5a; Task 7 fix round 1). Their auth rows are C rows
  of that client, so the database confines the request as well as the
  `user_id` filter: a row of another client does not exist for it.

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
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.auth import notices, staff_password
from app.core.auth.devices import presented, revoke, revoke_all_for_user
from app.core.auth.factors import count_failure, gate, lock_person, summary
from app.core.auth.failures import record_success
from app.core.auth.guards import require_fresh, sign_in_again, signed_user
from app.core.auth.keys import UnknownKeyVersion
from app.core.auth.methods import has_password, has_sign_in_method
from app.core.auth.passwords import verify_password
from app.core.auth.recovery import replace_codes
from app.core.auth.refusals import AuthRefusal
from app.core.auth.session_policy import read_policy
from app.core.auth.sessions import (
    DEVICE_COOKIE,
    SessionPrincipal,
    cookie_token,
    end_all_for_user,
    rotate,
    token_hash,
)
from app.core.auth.sessions import end as end_session
from app.core.auth.totp import remove_totp as drop_totp
from app.core.client_scope import ClientScope
from app.core.i18n import tr
from app.core.identity import CONTACT_KIND
from app.core.tenancy import org_session
from app.core.vault import VaultService
from app.models import User
from app.models.auth import (
    ENTRA,
    AuthExternalIdentity,
    AuthPasskey,
    AuthPassword,
    AuthRecoveryCode,
    AuthSession,
    AuthTrustedDevice,
)


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
class MicrosoftLinkView:
    tenant_id: UUID
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True)
class MySecurity:
    kind: str
    has_password: bool
    has_totp: bool
    passkeys: list[PasskeyView]
    recovery_codes_left: int
    trusted_devices: list[DeviceView]
    sessions: list[SessionView]
    microsoft: MicrosoftLinkView | None


@asynccontextmanager
async def _own_session(
    principal: SessionPrincipal, request: Request, clients: ClientScope
) -> AsyncIterator[AsyncSession]:
    """See the module docstring: the caller's own auth rows. A technician's
    session is every client. A contact's is `clients`, the scope the door
    established (`AuthContext.clients`: their own client, or none when
    `X-Client-Id` named another); every client is refused for a contact,
    so a caller that passed it fails closed rather than reopening 5a."""
    engine: AsyncEngine = request.app.state.app_engine
    if principal.kind != CONTACT_KIND:
        scope = ClientScope.all()
    elif clients.is_all:
        raise sign_in_again()
    else:
        scope = clients
    async with org_session(principal.org_id, engine=engine, clients=scope) as session:
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


async def my_security(
    request: Request, principal: SessionPrincipal, *, clients: ClientScope
) -> MySecurity:
    now = datetime.now(UTC)
    # Which listed device is this browser: compared by the cookie's hash, so a
    # read does not renew the device as `devices.recognised` would.
    here = cookie_token(request, DEVICE_COOKIE)
    here_hash = token_hash(here) if here is not None else None
    async with _own_session(principal, request, clients) as session:
        user = await signed_user(session, principal)
        _has_passkey, has_totp, codes = await summary(session, user.id)
        password = await has_password(session, user.id)
        link = (
            await session.execute(
                select(AuthExternalIdentity).where(
                    AuthExternalIdentity.user_id == user.id, AuthExternalIdentity.provider == ENTRA
                )
            )
        ).scalar_one_or_none()
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
        kind=user.kind,
        has_password=password,
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
        microsoft=None
        if link is None
        else MicrosoftLinkView(
            tenant_id=link.tenant_id, created_at=link.created_at, last_used_at=link.last_used_at
        ),
    )


async def _detach_from_device(
    session: AsyncSession, principal: SessionPrincipal, *, now: datetime
) -> None:
    """The caller's own session, when a remembered device started it, stops
    depending on that device before every device is revoked: Task 6's
    `revoke_all_for_user` ends the sessions a revoked device started, and the
    caller is not one of the OTHER sessions a password change ends (design
    §2.8, OWASP [S8]). It keeps no more than an ordinary session's lifetime,
    so no session outlives the trust that gave it its length.

    THE CAP COUNTS FROM THE CHANGE, `now` (coordinator decision, Task 7 fix
    round 2), not from the sign-in. At this point the session has just proved
    the current password (`change_password` verifies it) and holds a recent
    step-up (the route's `require_step_up`, ten minutes): a fresh proof of both
    factors, which is what a fresh sign-in would have. So it gets at most what
    a fresh ordinary sign-in would get, an ordinary lifetime from now. Counted
    from `factor_at`, a remembered session signed in more than 12 h ago -- the
    usual case, as such a session may last 168 h -- would get an expiry in the
    past: a 204, then a 401. `least` never lengthens a session; `factor_at` and
    `step_up_at` are left alone, Task 6's record of when each was proved."""
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
            expires_at=func.least(AuthSession.expires_at, now + ordinary),
        )
    )


async def change_password(
    request: Request, principal: SessionPrincipal, *, current: str, new: str, clients: ClientScope
) -> tuple[str, datetime]:
    """The current password again (OWASP [S5]: "require the current credentials
    for an account before updating ... the user's password"), then: the other
    sessions end and every remembered device is forgotten (design §2.6). The
    caller's own session stays -- detached from any device first and capped
    (`_detach_from_device`) -- under a NEW identifier (`sessions.rotate`,
    final review B-I1): the answer is that cookie's value and the session's
    capped expiry, for the route to set. The session is kept (coordinator
    decision, keep, detach and cap); only its cookie value changes, so a copy
    of the old one stops working.

    A recent confirmation is asked HERE as well as at the route
    (`guards.require_fresh`; the route's `require_step_up` is the first).

    THE WAIT IS TOLD (B-M4): the caller is signed in, so a waiting or disabled
    password is `factors.gate`'s 429 `factor_wait` / 403 `factor_disabled`,
    never "the current password is not right". It waits on the counter this
    browser waits on at sign-in (design §2.3): its remembered device's when
    `devices.presented` accepts the cookie, else the account's -- or whoever
    holds the sign-in wait would hold this remedy too.

    One transaction per attempt (Task 4's advisory lock): check, verify, count
    or change, and the refusal raised after the block commits the count."""
    require_fresh(principal)
    changed: tuple[str, datetime] | None = None
    async with _own_session(principal, request, clients) as session:
        user = await signed_user(session, principal)
        stored = await session.get(AuthPassword, user.id)
        device = await presented(session, user.id, request)
        counter = device.id if device is not None else None
        # Raised inside the block: nothing is written before it.
        await gate(session, user.id, "password", device_id=counter)
        if stored is not None:
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
                    minimum=staff_password.minimum_for(user.kind),
                )
                # The moment of the change: the password's `changed_at` and
                # the start of the caller's capped lifetime.
                now = datetime.now(UTC)
                await staff_password.store(
                    session, org_id=user.org_id, user_id=user.id, plain=new, now=now
                )
                await record_success(session, user.id, "password")
                await end_all_for_user(
                    session, user.id, "password_changed", except_id=principal.session_id
                )
                await _detach_from_device(session, principal, now=now)
                await revoke_all_for_user(session, user.id)
                try:
                    token = await rotate(session, principal.session_id)
                except LookupError as exc:
                    raise sign_in_again() from exc
                expires_at = (
                    await session.execute(
                        select(AuthSession.expires_at).where(
                            AuthSession.id == principal.session_id
                        )
                    )
                ).scalar_one()
                await _record(session, user, "auth.password_changed", {"via": "self"})
                await notices.tell(
                    session, user, "password_changed", vault=_vault(request), actor_id=user.id
                )
                changed = (token, expires_at)
            elif matches is False:
                await count_failure(
                    session, user, "password", vault=_vault(request), device_id=counter
                )
    if changed is None:
        raise AuthRefusal(
            status.HTTP_403_FORBIDDEN,
            code="invalid_credentials",
            detail=tr("the current password is not right"),
        )
    return changed


def _password_exists() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_409_CONFLICT,
        code="password_exists",
        detail=tr("you already have a password; change it instead"),
    )


async def _require_live(session: AsyncSession, principal: SessionPrincipal) -> None:
    """Under `lock_person`: the caller's session has not ended since the door
    admitted it (plan 2026-11-06, A2-38). A reset of the person's access
    takes the same lock (`factors.clear_second_factors`), so one that won
    the race has committed by now, and what it meant to cut must not add a
    way in after it."""
    live = await session.scalar(
        select(AuthSession.id).where(
            AuthSession.id == principal.session_id, AuthSession.ended_at.is_(None)
        )
    )
    if live is None:
        raise sign_in_again()


async def add_password(
    request: Request, principal: SessionPrincipal, *, new: str, clients: ClientScope
) -> None:
    """The first password of an account that has none (plan 2026-11-06, Task
    7, decision 4): in practice a contact who entered with a passkey or with
    Microsoft -- a technician always has one (operator decision C3). With a
    recent confirmation (the route's `require_step_up`, and here) and the rule
    of the person's kind; a person who has one changes it (`change_password`;
    409 `password_exists` here). Under the person's lock, on a session still
    live (A2-38): two at once are one 204 and one 409, never a 500 on the
    primary key. The other sessions stay (B2-27): adding a way in is not what
    somebody does who fears for the account, and the notice goes all the
    same (design §2.7)."""
    require_fresh(principal)
    async with _own_session(principal, request, clients) as session:
        user = await signed_user(session, principal)
        await lock_person(session, user.id)
        await _require_live(session, principal)
        if await has_password(session, user.id):
            raise _password_exists()
        # A refused password raises its 422 here, before anything is written.
        await staff_password.require_acceptable(
            new,
            email=user.email,
            display_name=user.display_name,
            org_name=await staff_password.organization_name(session, user.org_id),
            minimum=staff_password.minimum_for(user.kind),
        )
        await staff_password.store(session, org_id=user.org_id, user_id=user.id, plain=new)
        await _record(session, user, "auth.password_changed", {"via": "self_added"})
        await notices.tell(
            session, user, "password_changed", vault=_vault(request), actor_id=user.id
        )


def _last_factor() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_409_CONFLICT,
        code="last_factor",
        detail=tr("this is your only second factor; add another one before removing it"),
    )


def _last_method() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_409_CONFLICT,
        code="last_method",
        detail=tr("this is your only way to sign in; add another one before removing it"),
    )


async def remove_passkey(
    request: Request, principal: SessionPrincipal, passkey_id: UUID, *, clients: ClientScope
) -> None:
    """Remove one of the caller's passkeys. A technician never loses the last
    second factor (`last_factor`); a contact never loses the last way in
    (`last_method`, decision 2). Under the person's lock: two removals at
    once each see the other's key still there, and without it both would
    pass and leave no way in."""
    async with _own_session(principal, request, clients) as session:
        user = await signed_user(session, principal)
        await lock_person(session, user.id)
        key = (
            await session.execute(
                select(AuthPasskey).where(
                    AuthPasskey.id == passkey_id, AuthPasskey.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if key is None:
            raise _not_found(tr("no such passkey"))
        if user.kind == CONTACT_KIND:
            await session.delete(key)
            await session.flush()
            if not await has_sign_in_method(session, user.id):
                # Raised after the delete, inside the block: it rolls back.
                raise _last_method()
        else:
            keys = (
                await session.execute(
                    select(func.count())
                    .select_from(AuthPasskey)
                    .where(AuthPasskey.user_id == user.id)
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


async def remove_totp(
    request: Request, principal: SessionPrincipal, *, clients: ClientScope
) -> None:
    """Remove the caller's authenticator app. A technician keeps a second
    factor (`last_factor`); a contact's is optional (D27), and their
    recovery codes go with it (decision 3, B2-06): codes made for an app
    that is gone must not come back as the backup of the next one."""
    async with _own_session(principal, request, clients) as session:
        user = await signed_user(session, principal)
        await lock_person(session, user.id)
        has_passkey, has_totp, _codes = await summary(session, user.id)
        if not has_totp:
            raise _not_found(tr("that sign-in method is not set up on this account"))
        if user.kind != CONTACT_KIND and not has_passkey:
            raise _last_factor()
        await drop_totp(session, user.id)
        meta = {"factor": "totp"}
        if user.kind == CONTACT_KIND:
            await session.execute(
                delete(AuthRecoveryCode).where(AuthRecoveryCode.user_id == user.id)
            )
            meta["recovery_codes"] = "removed"
        await _record(session, user, "auth.factor_removed", meta)
        await notices.tell(
            session, user, "factor_removed", vault=_vault(request), actor_id=user.id, factor="totp"
        )


async def new_recovery_codes(
    request: Request, principal: SessionPrincipal, *, clients: ClientScope
) -> list[str]:
    """A new set of recovery codes, the old set spent. A contact has them
    only with an authenticator app (decision 3, B2-06): with none, 409
    `method_unavailable` -- a passkey is a contact's way in, not a second
    factor (operator decision C5), and codes back up a second factor."""
    async with _own_session(principal, request, clients) as session:
        user = await signed_user(session, principal)
        await lock_person(session, user.id)
        # A2-38, as `add_password`: a reset that won the lock has wiped the
        # codes; the session it cut must not mint new ones after it.
        await _require_live(session, principal)
        if user.kind == CONTACT_KIND:
            _has_passkey, has_totp, _codes = await summary(session, user.id)
            if not has_totp:
                raise AuthRefusal(
                    status.HTTP_409_CONFLICT,
                    code="method_unavailable",
                    detail=tr("turn on an authenticator app before asking for recovery codes"),
                )
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


async def forget_device(
    request: Request, principal: SessionPrincipal, device_id: UUID, *, clients: ClientScope
) -> None:
    async with _own_session(principal, request, clients) as session:
        user = await signed_user(session, principal)
        if not await revoke(session, user.id, device_id):
            raise _not_found(tr("no such remembered device"))
        await _record(session, user, "auth.device_revoked", {"by": "self"})


async def end_my_session(
    request: Request, principal: SessionPrincipal, session_id: UUID, *, clients: ClientScope
) -> None:
    async with _own_session(principal, request, clients) as session:
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
