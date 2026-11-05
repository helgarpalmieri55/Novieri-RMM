"""Signing in, before -- and around -- the console session (design §2).

EVERY FUNCTION HERE STARTS FROM SOMETHING THE DOOR DOES NOT ADMIT: an address
and a password, a session waiting for its second factor, a one-time link. So
the organization comes first -- from `its_auth_principal` (`lookup`), from the
session's own row (`sessions.resolve`) or from the link (`action_tokens`) --
and everything else is read and written under `_identity_session`: that
organization and EVERY client. A technician's auth rows belong to no client
(`O("users", "user_id")`, `users.client_id` NULL), and migration 0231's O
policies let only an every-client scope write such a row. This module is the
reviewed place that holds that scope for sign-in (`ALL_SCOPE_ALLOWLIST`); a
route file never does.

WHAT A REFUSAL TELLS (design §2.3, plan decision 5). Before the password is
proved, one answer for everything, after one argon2 verification. After it,
the person learns whether to wait, that a factor is locked, or that a code
was wrong.

ONE TRANSACTION PER ATTEMPT (Task 4's `failures.check_allowed`): the check,
the verification and the count run in one `_identity_session` block, and a
refusal that follows a write -- a counted failure, a spent passkey challenge
-- is raised AFTER the block has committed. A refusal raised inside the block
would roll the count (or the spend) back. Refusals that wrote nothing may be
raised from inside.

THE IDENTIFIER CHANGES (design §2.4): the password makes a pending session
(`start_session`, which also ends any session the browser's cookie still
named); the factor gives it a NEW value (`mark_factor`, Task 6).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID

from fastapi import FastAPI, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.auth import action_tokens, notices, staff_password
from app.core.auth.devices import RememberingOff, presented, recognised, revoke_all_for_user
from app.core.auth.devices import remember as remember_device
from app.core.auth.factors import clear_second_factors, count_failure, gate, summary
from app.core.auth.failures import check_allowed, record_success, reset_factor
from app.core.auth.guards import (
    relying_party,
    require_fresh,
    require_full,
    require_pending,
    sign_in_again,
    signed_user,
)
from app.core.auth.keys import UnknownKeyVersion
from app.core.auth.lookup import find_staff
from app.core.auth.passkeys import (
    PasskeyRejected,
    authentication_options,
    registration_options,
    verify_authentication,
    verify_registration,
)
from app.core.auth.passwords import verify_against_nobody, verify_password
from app.core.auth.recovery import remaining, replace_codes, use_code
from app.core.auth.refusals import AuthRefusal
from app.core.auth.session_policy import read_policy
from app.core.auth.sessions import (
    SessionPrincipal,
    end_all_for_user,
    mark_factor,
    mark_step_up,
    resumable,
    start_session,
)
from app.core.auth.sessions import end as end_session
from app.core.auth.sessions import named as named_session
from app.core.auth.totp import (
    confirm_secret,
    has_totp,
    new_secret,
    otpauth_uri,
    remove_totp,
    stage_secret,
    verify_totp,
)
from app.core.client_scope import ClientScope
from app.core.i18n import tr, use_locale
from app.core.identity import ACTIVE_STATUS, STAFF_KIND
from app.core.rate_limit import hit
from app.core.rbac_admin import SUPER_ADMIN
from app.core.tenancy import org_session
from app.core.vault import VaultService
from app.models import Role, User, UserRole
from app.models.auth import AuthPassword, AuthSession, AuthTrustedDevice

#: How long a pending session may take to finish enrolling (plan decision 6).
ENROLLED_S = 900
#: Links a single address may be sent in an hour (plan decision 8).
RESET_MAILS_PER_HOUR = 3

#: How long a new authenticator-app secret waits to replace the confirmed one
#: (plan decision 7).
REPLACING_S = 600

#: The organization an address with no account is "verified" in: the pepper
#: HMAC binds to it, and the work is the same (Task 2's `verify_against_nobody`).
_NO_ORG = UUID(int=0)


@dataclass(frozen=True)
class SignIn:
    """Where a sign-in stands, and the cookies the route sets for it."""

    state: Literal["factor_required", "enroll_required", "signed_in"]
    methods: tuple[str, ...] = ()
    #: With `factor_required`: how long "remember this device" would last,
    #: None when the organization does not remember devices (decision 15).
    remember_device_days: int | None = None
    session_token: str | None = None
    expires_at: datetime | None = None
    device_token: str | None = None
    device_expires_at: datetime | None = None


@asynccontextmanager
async def _identity_session(org_id: UUID, engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """See the module docstring: one organization, every client, auth rows only."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        yield session


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.app_engine
    return engine


def _vault(request: Request) -> VaultService:
    vault: VaultService = request.app.state.vault
    return vault


def _now() -> datetime:
    return datetime.now(UTC)


def _invalid_credentials() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_401_UNAUTHORIZED,
        code="invalid_credentials",
        detail=tr("the email address or the password is not right"),
    )


def _factor_failed(status_code: int = status.HTTP_401_UNAUTHORIZED) -> AuthRefusal:
    """401 on the public sign-in screen; 400 where the console calls it with a
    session open -- a step-up, an enrolment -- because there a 401 ends the
    session (plan decision 14)."""
    return AuthRefusal(
        status_code,
        code="factor_failed",
        detail=tr("the second factor was not accepted; try again"),
    )


def _unavailable() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_409_CONFLICT,
        code="method_unavailable",
        detail=tr("that sign-in method is not set up on this account"),
    )


def _invalid_link() -> AuthRefusal:
    """410 for every dead link -- used, expired, replaced or unknown, of any
    purpose: the page says one thing and learns nothing more (decision 3)."""
    return AuthRefusal(
        status.HTTP_410_GONE,
        code="invalid_link",
        detail=tr(
            "this link is not valid: it has expired, was already used or was replaced by a "
            "newer one"
        ),
    )


async def _audit(session: AsyncSession, user: User, action: str, meta: dict[str, Any]) -> None:
    """The one audit wrapper of this file (plan decision 12): every caller
    passes its action as a literal, listed in the vocabulary guard's DYNAMIC."""
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


async def _remember(
    session: AsyncSession, request: Request, user: User
) -> tuple[str | None, datetime | None]:
    """Remember this browser when the organization allows it (Task 6 says
    whether): audited and told (design §2.6)."""
    try:
        device, token = await remember_device(session, user, request)
    except RememberingOff:
        return None, None
    days = (device.expires_at - device.created_at).days
    await _audit(session, user, "auth.device_remembered", {"days": days})
    await notices.tell(
        session, user, "device_remembered", vault=_vault(request), actor_id=user.id, days=days
    )
    return token, device.expires_at


async def _factor_done(
    session: AsyncSession,
    request: Request,
    user: User,
    principal: SessionPrincipal,
    *,
    method: str,
    remember: bool,
) -> SignIn:
    """The pending session proved its factor: a new identifier and the recent
    confirmation (`mark_factor`), audited; the browser remembered when asked."""
    try:
        token = await mark_factor(session, principal.session_id)
    except LookupError as exc:
        raise sign_in_again() from exc
    await _audit(session, user, "auth.signed_in", {"method": method, "trusted_device": False})
    device_token, device_expires_at = (
        await _remember(session, request, user) if remember else (None, None)
    )
    return SignIn(
        state="signed_in",
        session_token=token,
        expires_at=principal.expires_at,
        device_token=device_token,
        device_expires_at=device_expires_at,
    )


async def _started(
    session: AsyncSession,
    request: Request,
    user: User,
    *,
    state: Literal["factor_required", "enroll_required", "signed_in"],
    factor_done: bool = False,
    device: AuthTrustedDevice | None = None,
    resume: AuthSession | None = None,
    methods: tuple[str, ...] = (),
    remember_device_days: int | None = None,
) -> SignIn:
    record, token = await start_session(
        session,
        user,
        kind=STAFF_KIND,
        request=request,
        factor_done=factor_done,
        trusted_device_id=device.id if device is not None else None,
        resume=resume,
    )
    return SignIn(
        state=state,
        methods=methods,
        remember_device_days=remember_device_days,
        session_token=token,
        expires_at=record.expires_at,
    )


# --------------------------------------------------------------------------- #
# the password
# --------------------------------------------------------------------------- #


async def _password_matches(org_id: UUID, plain: str, stored: AuthPassword) -> bool | None:
    """Whether `plain` is the stored password; None when it cannot be told.

    A verifier kept under a pepper version the key backend does not hold
    (the local backend's `UnknownKeyVersion`, a LookupError) can never verify
    again: that is not the person failing, so nothing is counted, and the same
    argon2 work is done as for a wrong password (falsifier 2), so the answer
    -- the generic 401 -- takes the same time. OpenBao answers an unknown
    version with an error, which is `AuthKeysUnavailable` and the 503 of plan
    decision 13."""
    try:
        return await verify_password(org_id, plain, stored.hash, stored.pepper_version)
    except UnknownKeyVersion:
        await verify_against_nobody(org_id, plain)
        return None


async def login(request: Request, *, email: str, password: str) -> SignIn:
    slug = request.app.state.settings.default_org_slug
    if not slug:
        raise AuthRefusal(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="sign_in_unavailable",
            detail=tr(
                "signing in is not set up on this installation: it names no organization to "
                "sign in to"
            ),
        )
    if not password or len(password) > staff_password.SUBMITTED_MAX:
        # Plan decision 16: the bound lives here, not on `LoginIn` (a field
        # failure repeats the password in its 422). No password the rule
        # accepts is empty or this long, so this is a wrong one -- answered
        # alike whoever the address names, before the pepper sees it.
        raise _invalid_credentials()
    found = await find_staff(_engine(request), org_slug=slug, email=email)
    if found is None or found.status != ACTIVE_STATUS:
        # Falsifier 2: the same argon2 work as a wrong password, so neither
        # the body nor the time says whether the address has an account.
        await verify_against_nobody(found.org_id if found is not None else _NO_ORG, password)
        raise _invalid_credentials()
    outcome: SignIn | None = None
    untried = False
    async with _identity_session(found.org_id, _engine(request)) as session:
        user = await session.get(User, found.user_id)
        stored = await session.get(AuthPassword, found.user_id)
        # The counter this browser waits and counts on (design §2.3, operator
        # decision 2026-11-05): its device's when `recognised`'s rule accepts
        # the cookie for THIS account, else the account's. Chosen before the
        # password is tried, and changes no answer.
        device = await presented(session, found.user_id, request)
        counter = device.id if device is not None else None
        state = await check_allowed(session, found.user_id, "password", device_id=counter)
        if user is None or stored is None or not state.allowed:
            untried = True
        else:
            matches = await _password_matches(found.org_id, password, stored)
            if matches is False:
                await count_failure(
                    session, user, "password", vault=_vault(request), device_id=counter
                )
            elif matches:
                await record_success(session, user.id, "password")
                outcome = await _after_password(session, request, user)
    if untried:
        # Falsifier 2: the same argon2 work as a wrong password. Done after
        # the block, so a refused attempt does not hold the account's lock
        # through it while the person's own browser waits its turn.
        await verify_against_nobody(found.org_id, password)
    # The count, when there was one, is committed: refuse only now.
    if outcome is None:
        raise _invalid_credentials()
    return outcome


async def _after_password(session: AsyncSession, request: Request, user: User) -> SignIn:
    """The password is right. In order: an idle session of this browser is
    resumed with the password alone (design §2.4, Task 6's `resumable`); a
    person with no second factor enrols one; a remembered device stands in for
    the factor (§2.6); everybody else is asked for it."""
    idle = await resumable(session, request, user.id)
    if idle is not None:
        await _audit(session, user, "auth.signed_in", {"method": "resume", "trusted_device": False})
        return await _started(session, request, user, state="signed_in", resume=idle)
    has_passkey, has_totp, codes = await summary(session, user.id)
    if not (has_passkey or has_totp):
        return await _started(session, request, user, state="enroll_required")
    device = await recognised(session, user.id, request)
    if device is not None:
        await _audit(
            session, user, "auth.signed_in", {"method": "password", "trusted_device": True}
        )
        return await _started(
            session, request, user, state="signed_in", factor_done=True, device=device
        )
    offered = (("passkey", has_passkey), ("totp", has_totp), ("recovery", codes > 0))
    policy = await read_policy(session, user.org_id)
    return await _started(
        session,
        request,
        user,
        state="factor_required",
        methods=tuple(name for name, available in offered if available),
        remember_device_days=(
            policy.remember_device_days if policy.remember_device_enabled else None
        ),
    )


# --------------------------------------------------------------------------- #
# the second factor, after the password
# --------------------------------------------------------------------------- #


async def factor_totp(
    request: Request, principal: SessionPrincipal, *, code: str, remember: bool
) -> SignIn:
    require_pending(principal)
    result: SignIn | None = None
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        if not (await summary(session, user.id))[1]:
            raise _unavailable()
        await gate(session, user.id, "totp")
        if await verify_totp(session, user.id, code, vault=_vault(request), now=_now()):
            await record_success(session, user.id, "totp")
            result = await _factor_done(
                session, request, user, principal, method="totp", remember=remember
            )
        else:
            await count_failure(session, user, "totp", vault=_vault(request))
    if result is None:
        raise _factor_failed()
    return result


async def _spend_recovery_code(
    session: AsyncSession, request: Request, user: User, code: str
) -> bool:
    if not await use_code(session, user.id, code):
        await count_failure(session, user, "recovery", vault=_vault(request))
        return False
    await record_success(session, user.id, "recovery")
    left = await remaining(session, user.id)
    await _audit(session, user, "auth.recovery_code_used", {"left": left})
    await notices.tell(
        session, user, "recovery_code_used", vault=_vault(request), actor_id=user.id, left=left
    )
    return True


async def factor_recovery(
    request: Request, principal: SessionPrincipal, *, code: str, remember: bool
) -> SignIn:
    require_pending(principal)
    result: SignIn | None = None
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        if (await summary(session, user.id))[2] == 0:
            raise _unavailable()
        await gate(session, user.id, "recovery")
        if await _spend_recovery_code(session, request, user, code):
            result = await _factor_done(
                session, request, user, principal, method="recovery", remember=remember
            )
    if result is None:
        raise _factor_failed()
    return result


async def _passkey_options(
    request: Request, principal: SessionPrincipal, purpose: Literal["authenticate", "step_up"]
) -> dict[str, Any]:
    rp_id, _origin = relying_party(request.app.state.settings)
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        if not (await summary(session, user.id))[0]:
            raise _unavailable()
        await gate(session, user.id, "passkey")
        return await authentication_options(
            session, user.id, org_id=user.org_id, rp_id=rp_id, purpose=purpose
        )


async def factor_passkey_options(request: Request, principal: SessionPrincipal) -> dict[str, Any]:
    require_pending(principal)
    return await _passkey_options(request, principal, "authenticate")


async def _passkey_matches(
    session: AsyncSession,
    request: Request,
    user: User,
    credential: dict[str, Any],
    purpose: Literal["authenticate", "step_up"],
) -> bool:
    """The assertion verified (Task 5) AND the passkey is this person's. A
    failure of either is one indistinguishable, counted failure (W3C / OWASP
    [S10]: "make it indistinguishable whether verification failed because the
    signature is invalid or because no such user or credential is registered").

    `PasskeyRejected` is caught HERE, never let through the caller's block:
    Task 5 spent the challenge in this transaction, and only a commit keeps
    it spent (`passkeys.py`, "THE CALLER'S TRANSACTION")."""
    rp_id, origin = relying_party(request.app.state.settings)
    try:
        key = await verify_authentication(
            session, credential, expected_origin=origin, rp_id=rp_id, purpose=purpose
        )
    except PasskeyRejected:
        key = None
    if key is None or key.user_id != user.id:
        await count_failure(session, user, "passkey", vault=_vault(request))
        return False
    await record_success(session, user.id, "passkey")
    return True


async def factor_passkey(
    request: Request, principal: SessionPrincipal, *, credential: dict[str, Any], remember: bool
) -> SignIn:
    require_pending(principal)
    result: SignIn | None = None
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        await gate(session, user.id, "passkey")
        if await _passkey_matches(session, request, user, credential, "authenticate"):
            result = await _factor_done(
                session, request, user, principal, method="passkey", remember=remember
            )
    if result is None:
        raise _factor_failed()
    return result


# --------------------------------------------------------------------------- #
# enrolment: the first factor, or one more from My security
# --------------------------------------------------------------------------- #


def _enrolled_key(session_id: UUID) -> str:
    return f"auth:enrolled:{session_id}"


async def _enrollable(session: AsyncSession, principal: SessionPrincipal) -> User:
    """Plan decision 6. A full session adds a factor with a recent confirmation;
    a pending one only when the person has no second factor at all -- the
    password alone must never let somebody add theirs."""
    user = await signed_user(session, principal)
    if principal.factor_done:
        require_fresh(principal)
    else:
        has_passkey, has_totp, _codes = await summary(session, user.id)
        if has_passkey or has_totp:
            raise AuthRefusal(
                status.HTTP_403_FORBIDDEN,
                code="factor_required",
                detail=tr("confirm your second factor first"),
            )
    return user


async def _enrolled(request: Request, principal: SessionPrincipal) -> None:
    if not principal.factor_done:
        await request.app.state.redis.set(_enrolled_key(principal.session_id), "1", ex=ENROLLED_S)


def _replacing_key(session_id: UUID) -> str:
    return f"auth:totp-replace:{session_id}"


async def enroll_totp_start(request: Request, principal: SessionPrincipal) -> str:
    """The `otpauth://` URI, shown once. The secret waits, sealed: as the
    person's pending TOTP (Task 4), or -- when a full session re-sets a
    confirmed one -- in Redis under this session until a code from it is
    confirmed (plan decision 7)."""
    secret = new_secret()
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await _enrollable(session, principal)
        if principal.factor_done and await has_totp(session, user.id):
            sealed = await _vault(request).seal_bytes(
                session, org_id=user.org_id, key_id=user.id, plaintext=secret
            )
            await request.app.state.redis.set(
                _replacing_key(principal.session_id), sealed, ex=REPLACING_S
            )
        else:
            await stage_secret(
                session, org_id=user.org_id, user_id=user.id, secret=secret, vault=_vault(request)
            )
    return otpauth_uri(secret, account=user.email, issuer=notices.PRODUCT)


async def _replace_totp(
    session: AsyncSession, request: Request, user: User, sealed: str, code: str
) -> None:
    """Plan decision 7: the old TOTP out, the new one in, a code from it
    accepted -- one transaction. A wrong code raises HERE, inside the
    caller's block, so all three are undone and the old app keeps working."""
    vault = _vault(request)
    secret = await vault.open_bytes(session, org_id=user.org_id, key_id=user.id, sealed=sealed)
    await remove_totp(session, user.id)
    await stage_secret(session, org_id=user.org_id, user_id=user.id, secret=secret, vault=vault)
    if not await confirm_secret(session, user.id, code, vault=vault, now=_now()):
        raise _factor_failed(status.HTTP_400_BAD_REQUEST)


async def enroll_totp_confirm(request: Request, principal: SessionPrincipal, *, code: str) -> None:
    redis = request.app.state.redis
    key = _replacing_key(principal.session_id)
    sealed: str | None = await redis.get(key) if principal.factor_done else None
    accepted = False
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await _enrollable(session, principal)
        if sealed is not None:
            await _replace_totp(session, request, user, sealed, code)
            accepted = True
        else:
            accepted = await confirm_secret(
                session, user.id, code, vault=_vault(request), now=_now()
            )
        if accepted:
            # No `reset_factor` here: a new secret does not clear the TOTP's
            # failures (Task 4's decision 7, NIST SP 800-63B-4 §3.1.3:
            # "Generating a new authentication secret SHALL NOT reset the
            # failed authentication count").
            replaced = sealed is not None
            await _audit(
                session, user, "auth.factor_added", {"factor": "totp", "replaced": replaced}
            )
            await notices.tell(
                session,
                user,
                "factor_added",
                vault=_vault(request),
                actor_id=user.id,
                factor="totp",
            )
    if not accepted:
        raise _factor_failed(status.HTTP_400_BAD_REQUEST)
    if sealed is not None:
        await redis.delete(key)
    await _enrolled(request, principal)


async def enroll_passkey_options(request: Request, principal: SessionPrincipal) -> dict[str, Any]:
    rp_id, _origin = relying_party(request.app.state.settings)
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await _enrollable(session, principal)
        return await registration_options(session, user, rp_id=rp_id, rp_name=notices.PRODUCT)


async def enroll_passkey(
    request: Request, principal: SessionPrincipal, *, credential: dict[str, Any], name: str
) -> None:
    rp_id, origin = relying_party(request.app.state.settings)
    rejected = False
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await _enrollable(session, principal)
        try:
            await verify_registration(
                session, user, credential, name=name, expected_origin=origin, rp_id=rp_id
            )
        except PasskeyRejected:
            # Task 5 spent the challenge in THIS transaction: the block must
            # commit before the refusal, or the challenge stays live and the
            # same response can be tried again (`passkeys.py`).
            rejected = True
        else:
            await _audit(session, user, "auth.factor_added", {"factor": "passkey", "name": name})
            await notices.tell(
                session,
                user,
                "factor_added",
                vault=_vault(request),
                actor_id=user.id,
                factor="passkey",
            )
    if rejected:
        raise AuthRefusal(
            status.HTTP_400_BAD_REQUEST,
            code="passkey_rejected",
            detail=tr("the passkey was not accepted; try again"),
        )
    await _enrolled(request, principal)


async def enroll_finish(request: Request, principal: SessionPrincipal) -> tuple[list[str], SignIn]:
    """The ten recovery codes, shown once, and the full session (design §2.2)."""
    require_pending(principal)
    redis = request.app.state.redis
    if await redis.get(_enrolled_key(principal.session_id)) is None:
        raise AuthRefusal(
            status.HTTP_409_CONFLICT,
            code="enroll_required",
            detail=tr("set up a passkey or an authenticator app first"),
        )
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        codes = await replace_codes(session, org_id=user.org_id, user_id=user.id)
        await _audit(session, user, "auth.factor_added", {"factor": "recovery_codes"})
        result = await _factor_done(
            session, request, user, principal, method="enrolment", remember=False
        )
    await redis.delete(_enrolled_key(principal.session_id))
    return codes, result


# --------------------------------------------------------------------------- #
# the recent confirmation (design §2.5)
# --------------------------------------------------------------------------- #


async def step_up_totp(request: Request, principal: SessionPrincipal, *, code: str) -> None:
    require_full(principal)
    accepted = False
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        if not (await summary(session, user.id))[1]:
            raise _unavailable()
        await gate(session, user.id, "totp")
        if await verify_totp(session, user.id, code, vault=_vault(request), now=_now()):
            await record_success(session, user.id, "totp")
            await mark_step_up(session, principal.session_id)
            accepted = True
        else:
            await count_failure(session, user, "totp", vault=_vault(request))
    if not accepted:
        raise _factor_failed(status.HTTP_400_BAD_REQUEST)


async def step_up_recovery(request: Request, principal: SessionPrincipal, *, code: str) -> None:
    require_full(principal)
    accepted = False
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        if (await summary(session, user.id))[2] == 0:
            raise _unavailable()
        await gate(session, user.id, "recovery")
        accepted = await _spend_recovery_code(session, request, user, code)
        if accepted:
            await mark_step_up(session, principal.session_id)
    if not accepted:
        raise _factor_failed(status.HTTP_400_BAD_REQUEST)


async def step_up_passkey_options(request: Request, principal: SessionPrincipal) -> dict[str, Any]:
    require_full(principal)
    return await _passkey_options(request, principal, "step_up")


async def step_up_passkey(
    request: Request, principal: SessionPrincipal, *, credential: dict[str, Any]
) -> None:
    require_full(principal)
    accepted = False
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        await gate(session, user.id, "passkey")
        accepted = await _passkey_matches(session, request, user, credential, "step_up")
        if accepted:
            await mark_step_up(session, principal.session_id)
    if not accepted:
        raise _factor_failed(status.HTTP_400_BAD_REQUEST)


# --------------------------------------------------------------------------- #
# signing out
# --------------------------------------------------------------------------- #


async def logout(request: Request) -> None:
    """The session the cookie names -- idle or not (final review B-M1:
    `sessions.named`, not `resolve`) -- and only it; a remembered device stays
    remembered (that is what remembering it means). Answered alike with no
    session."""
    named = await named_session(request)
    if named is None:
        return
    async with _identity_session(named.org_id, _engine(request)) as session:
        await end_session(session, named.session_id, "signed_out")


async def logout_everywhere(request: Request, principal: SessionPrincipal) -> None:
    """Every session of the person, this one included, and every remembered
    device: what somebody does who thinks their account was used."""
    require_full(principal)
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        await end_all_for_user(session, user.id, "signed_out_everywhere")
        await revoke_all_for_user(session, user.id)
        await _audit(session, user, "auth.sessions_ended", {"by": "self", "scope": "everywhere"})


# --------------------------------------------------------------------------- #
# forgot and reset (design §2.8)
# --------------------------------------------------------------------------- #


async def _has_second_factor(session: AsyncSession, user_id: UUID) -> bool:
    """A passkey or a CONFIRMED authenticator app. Recovery codes alone do
    not count: they are a fallback of a factor, not one (design §2.2)."""
    has_passkey, has_totp, _codes = await summary(session, user_id)
    return has_passkey or has_totp


async def send_reset_link(app: FastAPI, *, email: str, locale: str) -> None:
    """Run AFTER the 202 (plan decision 8): nothing below changes the answer
    or its time, so nothing tells whether the address has an account.

    NEVER TO AN ACCOUNT WITHOUT A SECOND FACTOR (final review C-I1; design
    §2.8, "después sigue pidiendo el segundo factor"; Q5, "nunca solo por
    correo"). Such an account -- after an administrator's factor reset, an
    invitation or a recovery link whose enrolment was abandoned, the
    first-boot owner before their first sign-in -- would let the mailbox set
    the password and then enrol ITS OWN factor: the mailbox alone, and a full
    session. It is sent nothing, as an unknown address is; the person with the
    password signs in and enrols, and one who has lost both uses the server's
    recovery command. So the mail's "You will still need your second factor"
    is true when it is sent."""
    with use_locale(locale):
        settings = app.state.settings
        engine: AsyncEngine = app.state.app_engine
        if not settings.default_org_slug or not settings.public_console_url:
            return
        address = email.strip().lower()
        per_address = sha256(f"{settings.default_org_slug}|{address}".encode()).hexdigest()
        if not await hit(
            app.state.redis,
            f"auth-forgot:address:{per_address}",
            limit=RESET_MAILS_PER_HOUR,
            window_s=3600,
        ):
            return
        found = await find_staff(engine, org_slug=settings.default_org_slug, email=address)
        if found is None or found.status != ACTIVE_STATUS:
            return
        async with _identity_session(found.org_id, engine) as session:
            user = await session.get(User, found.user_id)
            if (
                user is None
                or await session.get(AuthPassword, user.id) is None
                or not await _has_second_factor(session, user.id)
            ):
                return
            token = await action_tokens.issue(
                session,
                org_id=user.org_id,
                user_id=user.id,
                purpose=action_tokens.RESET,
                ttl=action_tokens.RESET_TTL,
                created_by=None,
            )
            with use_locale(user.locale or locale):
                subject, text = notices.reset_mail(
                    action_tokens.link(settings.public_console_url, "reset-password", token)
                )
            await notices.deliver(
                session,
                org_id=user.org_id,
                to=user.email,
                subject=subject,
                text=text,
                vault=app.state.vault,
                actor_id=None,
            )


async def reset_password(request: Request, *, token: str, password: str) -> None:
    """A reset link: a new password, every session ended, every device
    forgotten, the password's failures cleared (plan decision 9). No session
    is started (OWASP [S8]). The server's recovery link has routes of its own
    (Task 8) and is refused here."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        row = await action_tokens.consume(
            session, token=token, purposes=(action_tokens.RESET,)
        )
        user = await session.get(User, row.user_id) if row is not None else None
        if (
            row is None
            or user is None
            or user.kind != STAFF_KIND
            or user.status != ACTIVE_STATUS
            # A link sent while the account had a factor, used after the
            # factors went (an administrator's reset): the same mailbox-only
            # path as `send_reset_link`'s, refused like every dead link. The
            # refusal rolls the `consume` back; nothing was written.
            or not await _has_second_factor(session, user.id)
        ):
            raise _invalid_link()
        # Raises a 422 that rolls the `consume` back: the link stays usable.
        await staff_password.require_acceptable(
            password,
            email=user.email,
            display_name=user.display_name,
            org_name=await staff_password.organization_name(session, org_id),
        )
        await staff_password.store(session, org_id=org_id, user_id=user.id, plain=password)
        await reset_factor(session, user.id, "password")
        await end_all_for_user(session, user.id, "password_changed")
        await revoke_all_for_user(session, user.id)
        await _audit(session, user, "auth.password_changed", {"via": row.purpose})
        await notices.tell(session, user, "password_changed", vault=_vault(request), actor_id=None)


# --------------------------------------------------------------------------- #
# the invitation (Task 8, design §7)
# --------------------------------------------------------------------------- #


async def _holds_super_admin(session: AsyncSession, user_id: UUID) -> bool:
    """Whether the person holds `super-admin`, by name, as every rail compares it."""
    held = (
        await session.execute(
            select(UserRole.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == user_id, Role.name == SUPER_ADMIN)
            .limit(1)
        )
    ).first()
    return held is not None


async def _invited(session: AsyncSession, token: str, *, consume: bool) -> User:
    """The invitee of a live, unaccepted invitation, or `invalid_link`.

    NEVER A SUPER-ADMIN (Task 8 review). No organization can grant the role,
    so none invites its holder (`user_admin.service.resend_invite` refuses it);
    a link that names one anyway -- however it came to exist -- sets no
    password, and answers what every dead link answers.

    Nothing is written before a refusal here but the `consume`, and the
    refusal rolls it back with the block: a link that names nobody usable was
    never a link to spend."""
    if consume:
        row = await action_tokens.consume(session, token=token, purposes=(action_tokens.INVITE,))
    else:
        row = await action_tokens.peek(session, token=token, purposes=(action_tokens.INVITE,))
    user = await session.get(User, row.user_id) if row is not None else None
    if (
        row is None
        or user is None
        or user.kind != STAFF_KIND
        or user.status != ACTIVE_STATUS
        or await session.get(AuthPassword, user.id) is not None
        or await _holds_super_admin(session, user.id)
    ):
        raise _invalid_link()
    return user


async def invite_details(request: Request, *, token: str) -> tuple[str, str]:
    """The address and the name an invitation is for. Leaves the link unused."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        user = await _invited(session, token, consume=False)
        return user.email, user.display_name


async def accept_invite(request: Request, *, token: str, password: str) -> SignIn:
    """The invitee's own password, and a session waiting for its first factor
    (design §2.2: the person reaches the console only after registering one).
    A refused password rolls the `consume` back: the link stays usable."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        user = await _invited(session, token, consume=True)
        # Raises a 422 that rolls the `consume` back: the link stays usable.
        await staff_password.require_acceptable(
            password,
            email=user.email,
            display_name=user.display_name,
            org_name=await staff_password.organization_name(session, org_id),
        )
        await staff_password.store(session, org_id=org_id, user_id=user.id, plain=password)
        await _audit(session, user, "auth.password_changed", {"via": "invite"})
        return await _started(session, request, user, state="enroll_required")


# --------------------------------------------------------------------------- #
# the server's recovery link (Task 8, design §7; plan decision 10)
# --------------------------------------------------------------------------- #


async def _recovering(session: AsyncSession, token: str, *, consume: bool) -> User:
    """The holder of a live recovery link, still an active technician, or
    `invalid_link`. Only `app.core.auth.recover` issues one (falsifier 9)."""
    purposes = (action_tokens.RECOVER,)
    if consume:
        row = await action_tokens.consume(session, token=token, purposes=purposes)
    else:
        row = await action_tokens.peek(session, token=token, purposes=purposes)
    user = await session.get(User, row.user_id) if row is not None else None
    if row is None or user is None or user.kind != STAFF_KIND or user.status != ACTIVE_STATUS:
        raise _invalid_link()
    return user


async def recovery_details(request: Request, *, token: str) -> str:
    """The address a recovery link is for. Leaves the link unused."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        return (await _recovering(session, token, consume=False)).email


async def accept_recovery(request: Request, *, token: str) -> SignIn:
    """Every second factor, every remembered device and every session go; a
    session waiting for a new first factor is opened. The password stays
    (plan decision 10: who recovers lost the factors, not the password), and
    the session opened here is pending until `enroll/finish`."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        user = await _recovering(session, token, consume=True)
        await clear_second_factors(session, user.id)
        await end_all_for_user(session, user.id, "factors_reset")
        await revoke_all_for_user(session, user.id)
        await _audit(session, user, "user.recovery_link_used", {"email": user.email})
        await notices.tell(session, user, "factors_reset", vault=_vault(request), actor_id=None)
        return await _started(session, request, user, state="enroll_required")
