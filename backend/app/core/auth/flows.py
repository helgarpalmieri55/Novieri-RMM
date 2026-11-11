"""Signing in, before -- and around -- the console session (design §2).

EVERY FUNCTION HERE STARTS FROM SOMETHING THE DOOR DOES NOT ADMIT: an address
and a password, a session waiting for its second factor, a one-time link, a
passkey alone. So the organization comes first -- from `its_auth_principal`
(`lookup`), from the session's own row (`sessions.resolve`), from the link
(`action_tokens`) or, for a passkey without an address, from the deployment's
slug alone (`its_auth_org`, phase 2) -- and everything else is read and
written under `_identity_session`: that organization and EVERY client. A
technician's auth rows belong to no client (`O("users", "user_id")`,
`users.client_id` NULL), and migration 0231's O policies let only an
every-client scope write such a row. This module is the reviewed place that
holds that scope for sign-in (`ALL_SCOPE_ALLOWLIST`); a route file never does.

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
from app.core.auth import action_tokens, entra_settings, notices, staff_password
from app.core.auth.devices import RememberingOff, presented, recognised, revoke_all_for_user
from app.core.auth.devices import remember as remember_device
from app.core.auth.factors import (
    clear_second_factors,
    count_failure,
    gate,
    lock_person,
    set_by_mail,
    summary,
)
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
from app.core.auth.lookup import find_person, org_of_slug
from app.core.auth.methods import contact_may_reset, has_sign_in_method
from app.core.auth.passkeys import (
    PasskeyRejected,
    authentication_options,
    registration_options,
    unanswerable_options,
    verify_authentication,
    verify_registration,
)
from app.core.auth.passwords import verify_against_nobody, verify_password
from app.core.auth.portal_access import grant_portal
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
from app.core.identity import ACTIVE_STATUS, CONTACT_KIND, STAFF_KIND
from app.core.rate_limit import hit
from app.core.rbac_admin import SUPER_ADMIN
from app.core.tenancy import org_session
from app.core.vault import VaultService
from app.models import Client, Organization, Role, User, UserRole
from app.models.auth import AuthActionToken, AuthPassword, AuthSession, AuthTrustedDevice

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


def _no_organization() -> AuthRefusal:
    """No `ITS_DEFAULT_ORG_SLUG`: a fact about the installation, the same for
    every caller, so it may be said."""
    return AuthRefusal(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        code="sign_in_unavailable",
        detail=tr(
            "signing in is not set up on this installation: it names no organization to sign in to"
        ),
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


def _passkey_rejected(status_code: int = status.HTTP_401_UNAUTHORIZED) -> AuthRefusal:
    """A passkey that is not accepted, whatever the reason (W3C: indistinguishable
    from "no such credential"). 401 on the sign-in screen (`passkey_sign_in`);
    400 where a session is open -- an enrolment -- because there a 401 ends it,
    as `_factor_failed` says; 400 too where an invitation is accepted
    (`invite_passkey`, the plan's Global Constraints)."""
    return AuthRefusal(
        status_code,
        code="passkey_rejected",
        detail=tr("the passkey was not accepted; try again"),
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


async def _audit(
    session: AsyncSession,
    user: User,
    action: str,
    meta: dict[str, Any],
    *,
    client_id: UUID | None = None,
) -> None:
    """The one audit wrapper of this file (plan decision 12): every caller
    passes its action as a literal, listed in the vocabulary guard's DYNAMIC.
    `client_id` is the row's informative client (invariant 5a: the audit
    table has none of its own), given for a contact's `contact.*` act."""
    await audit_service.record(
        session,
        org_id=user.org_id,
        actor_type="user",
        actor_id=user.id,
        action=action,
        target_type="user",
        target_id=str(user.id),
        metadata=meta,
        client_id=client_id,
    )


async def _consumed(
    session: AsyncSession, *, digest: bytes, purposes: tuple[str, ...]
) -> AuthActionToken | None:
    """A one-time link consumed IN THE LOCK ORDER (Task 8, fix rounds 1-2):
    THE PERSON'S `users` ROW FIRST, FOR NO KEY UPDATE, THEN THE LINK'S ROW,
    then -- whenever the caller audits -- the chain. For a technician as for
    a contact: an invitation's acceptance (`invitee`) and a password reset
    (`reset_password`) serve both kinds, and so must every administrator's
    act that touches a person's links or sessions and writes their row:
    - a contact's: the archive (`services.clients._lock_contacts`, then
      `_cut_access`), a switch-off or an address change (`update_contact`'s
      UPDATE, then the links), and `services.contact_access._contact`;
    - a technician's: `user_admin.service.cancel_invite` and `delete_user`
      (`_target(lock=True)`, fix round 2), and `set_enabled`, which writes
      the row before it ends anything.
    A use that took the link first and wrote the person's row last
    (`users.last_login_at`, in `sessions.start_session`; a reset's sessions)
    waited on them while they waited on it: a 40P01, and a 500
    (`tests/test_contact_access_races.py`, `tests/test_user_admin_races.py`).

    NOT YET COVERED -- the person's OWN concurrent sign-in steps, which do
    not take their row first: `sessions.mark_factor` locks a session and
    then writes the row; `login` takes its failure rows and the audit chain
    before `start_session` writes it. Any users-first writer above -- this
    function's callers included, a reset of the person's own password among
    them -- can meet one of them the other way round, and one of the two is
    a 40P01. That class is a lock-order task of its own, after Task 9.

    The link is read unlocked, only to learn whose it is; the person is
    locked, re-read (`populate_existing`: what a switch-off committed
    meanwhile is what the caller then checks); and only then is the link
    consumed, under its own row lock and re-read too -- one revoked meanwhile
    is None. `session.get(User, row.user_id)` then answers the locked row."""
    seen = await action_tokens.peek_digest(session, digest=digest, purposes=purposes)
    if seen is None:
        return None
    await session.execute(
        select(User)
        .where(User.id == seen.user_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    return await action_tokens.consume_digest(session, digest=digest, purposes=purposes)


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


async def started(
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
    """A new session for `user`, of the person's own kind: a contact's is a
    contact session, which only the portal's door admits (design §3). Public
    since phase 2 (plan decision 2): Microsoft's sign-in starts one too."""
    record, token = await start_session(
        session,
        user,
        kind=user.kind,
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
        raise _no_organization()
    if not password or len(password) > staff_password.SUBMITTED_MAX:
        # Plan decision 16: the bound lives here, not on `LoginIn` (a field
        # failure repeats the password in its 422). No password the rule
        # accepts is empty or this long, so this is a wrong one -- answered
        # alike whoever the address names, before the pepper sees it.
        raise _invalid_credentials()
    found = await find_person(_engine(request), org_slug=slug, email=email)
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
                outcome = await after_first_factor(session, request, user)
    if untried:
        # Falsifier 2: the same argon2 work as a wrong password. Done after
        # the block, so a refused attempt does not hold the account's lock
        # through it while the person's own browser waits its turn.
        await verify_against_nobody(found.org_id, password)
    # The count, when there was one, is committed: refuse only now.
    if outcome is None:
        raise _invalid_credentials()
    return outcome


async def after_first_factor(
    session: AsyncSession, request: Request, user: User, *, method: str = "password"
) -> SignIn:
    """The first factor is right -- the password, or (phase 2, Task 9) a
    linked Microsoft account, named by `method` in the audit. In order: an
    idle session of this browser is resumed with THE PASSWORD alone (design
    §2.4, Task 6's `resumable`; never with Microsoft, plan decision 5); a
    person with no second factor enrols one -- or, a contact, whose second
    factor is optional, is signed in (design §3, plan decision D27); a
    remembered device stands in for the factor (§2.6); everybody else is
    asked for it. A CONTACT'S SECOND FACTOR IS THE AUTHENTICATOR APP ALONE:
    their passkey is another way in (operator decision C5, plan decision 3),
    so it is neither asked for nor offered here."""
    if method == "password":
        idle = await resumable(session, request, user.id)
        if idle is not None:
            await _audit(
                session, user, "auth.signed_in", {"method": "resume", "trusted_device": False}
            )
            return await started(session, request, user, state="signed_in", resume=idle)
    has_passkey, has_totp, codes = await summary(session, user.id)
    # C5: for a contact, a passkey is not a second factor of the password.
    passkey_factor = has_passkey and user.kind == STAFF_KIND
    if not (passkey_factor or has_totp):
        if user.kind == CONTACT_KIND:
            await _audit(
                session, user, "auth.signed_in", {"method": method, "trusted_device": False}
            )
            return await started(session, request, user, state="signed_in", factor_done=True)
        return await started(session, request, user, state="enroll_required")
    device = await recognised(session, user.id, request)
    if device is not None:
        await _audit(session, user, "auth.signed_in", {"method": method, "trusted_device": True})
        return await started(
            session, request, user, state="signed_in", factor_done=True, device=device
        )
    offered = (("passkey", passkey_factor), ("totp", has_totp), ("recovery", codes > 0))
    policy = await read_policy(session, user.org_id)
    return await started(
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
) -> int | None:
    """Spend the code: the codes left, or None for a wrong one (counted). The
    caller tells the person (`_tell_recovery_code_used`) only after its own
    last check that can still refuse -- a notice is mail, and mail does not
    roll back (fix round 3)."""
    if not await use_code(session, user.id, code):
        await count_failure(session, user, "recovery", vault=_vault(request))
        return None
    await record_success(session, user.id, "recovery")
    left = await remaining(session, user.id)
    await _audit(session, user, "auth.recovery_code_used", {"left": left})
    return left


async def _tell_recovery_code_used(
    session: AsyncSession, request: Request, user: User, left: int
) -> None:
    """The LAST act of the step's transaction (fix round 3; re-review of fix
    round 2, Important 1). `notices.tell` sends synchronously, and with a
    relay that signs in, `mail._sign_in` checks the relay's secret out of the
    vault and records `email.credential_used` in THIS session. Sent before a
    refusal that rolls the block back -- the session ended meanwhile, at
    `_stepped_up` or `mark_factor` -- it told the person of a code that was
    not used, and left a vault checkout whose audit row rolled back
    (invariant 4)."""
    await notices.tell(
        session, user, "recovery_code_used", vault=_vault(request), actor_id=user.id, left=left
    )


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
        left = await _spend_recovery_code(session, request, user, code)
        if left is not None:
            # `mark_factor` refuses inside the block when the session ended
            # meanwhile: the notice comes after it.
            result = await _factor_done(
                session, request, user, principal, method="recovery", remember=remember
            )
            await _tell_recovery_code_used(session, request, user, left)
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
    it spent (`passkeys.py`, "THE CALLER'S TRANSACTION").

    THE CHALLENGE MUST BE THIS PERSON'S (`owner`, phase 2 Task 6, decision 4):
    since `/auth/passkey/options` issues challenges that are nobody's, one of
    those must not answer this step."""
    rp_id, origin = relying_party(request.app.state.settings)
    try:
        key = await verify_authentication(
            session,
            credential,
            expected_origin=origin,
            rp_id=rp_id,
            purpose=purpose,
            owner=user.id,
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
        # A contact's passkey signs in without an address, so the browser must
        # make it discoverable or fail (phase 2 Task 6, decision 3; D28).
        return await registration_options(
            session,
            user,
            rp_id=rp_id,
            rp_name=notices.PRODUCT,
            discoverable=user.kind == CONTACT_KIND,
        )


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
        raise _passkey_rejected(status.HTTP_400_BAD_REQUEST)
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


async def _stepped_up(session: AsyncSession, principal: SessionPrincipal) -> None:
    """The recent confirmation on a session that must still be live. Raised
    INSIDE the step-up's block on purpose, unlike a counted failure: a
    session ended while the step-up ran (a mail reset ends every session)
    rolls the whole step-up back -- the factor's success, the clearing of the
    "password set by mail" mark, a recovery code's spend -- so nothing of it
    commits and nothing is audited (re-review of fix round 1, Important 1).
    The answer is a dead session's: 401 `auth_required`."""
    try:
        await mark_step_up(session, principal.session_id)
    except LookupError as exc:
        raise sign_in_again() from exc


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
            await _stepped_up(session, principal)
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
        left = await _spend_recovery_code(session, request, user, code)
        if left is not None:
            # `_stepped_up` refuses inside the block when the session ended
            # meanwhile: the notice comes after it.
            await _stepped_up(session, principal)
            await _tell_recovery_code_used(session, request, user, left)
            accepted = True
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
            await _stepped_up(session, principal)
    if not accepted:
        raise _factor_failed(status.HTTP_400_BAD_REQUEST)


async def step_up_password(request: Request, principal: SessionPrincipal, *, password: str) -> None:
    """A contact WITHOUT A SECOND FACTOR confirms with the password (plan
    decisions D29 and 6): their second factor is optional, and the password
    is what one without it has. Everybody else gets 409 `method_unavailable`,
    as for a factor the account does not have: a technician, who confirms
    with a second factor (design §2.5); a contact who turned the app on, who
    confirms with it (design §2.6: "las acciones de §2.5 siguen pidiendo el
    segundo factor"; operator decision C5); and a contact without a
    password. The password's own counters and wait (design §2.3). An empty
    password, or one longer than `SUBMITTED_MAX` (plan decision 16), never
    reaches the pepper or argon2 -- but unlike `login`, which refuses it before
    looking anybody up, this person is known: it is a wrong password, counted
    as a failure and answered 400 `factor_failed`."""
    require_full(principal)
    accepted = False
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        stored = await session.get(AuthPassword, user.id)
        if (
            user.kind != CONTACT_KIND
            or stored is None
            or await _has_second_factor(session, user)
        ):
            raise _unavailable()
        device = await presented(session, user.id, request)
        counter = device.id if device is not None else None
        # Raised inside the block: nothing is written before it.
        await gate(session, user.id, "password", device_id=counter)
        matches: bool | None = False
        if password and len(password) <= staff_password.SUBMITTED_MAX:
            matches = await _password_matches(user.org_id, password, stored)
        if matches:
            await record_success(session, user.id, "password")
            await _stepped_up(session, principal)
            accepted = True
        elif matches is False:
            await count_failure(
                session, user, "password", vault=_vault(request), device_id=counter
            )
    if not accepted:
        raise _factor_failed(status.HTTP_400_BAD_REQUEST)


# --------------------------------------------------------------------------- #
# a passkey alone, without an address (phase 2, Task 6; design §3, D28)
# --------------------------------------------------------------------------- #

#: The `organizations.status` whose people may sign in (the column's default;
#: `Organization.status`).
_ORG_ACTIVE = "active"


async def _deployment_org(request: Request) -> UUID | None:
    """The organization a sign-in without an address signs in to: the
    deployment's (`default_org_slug`), through `its_auth_org` (D36). No slug
    is the installation's 503, as `login` says it. A slug that names no
    organization is None, which the callers answer as they answer everybody
    else (Task 6 review note): `org_of_slug` names an organization whatever its
    status, and nothing for a slug that names none."""
    slug = request.app.state.settings.default_org_slug
    if not slug:
        raise _no_organization()
    return await org_of_slug(_engine(request), slug)


async def passkey_sign_in_options(request: Request) -> dict[str, Any]:
    """A challenge that belongs to nobody yet, and no list of credentials:
    the browser offers the person's own discoverable passkeys (D28). Nothing
    in the answer says whether any address has an account -- nor whether the
    organization is active or exists: a suspended one is issued a challenge
    like an active one, and a slug that names none gets options of the same
    shape whose challenge is stored nowhere (`unanswerable_options`).

    THE TIME IS NOT THE SAME for a slug that names none: it opens no
    session, sweeps nothing and inserts nothing, so it answers sooner. That
    tells nobody anything they could choose to ask: the slug is the server's
    configuration (`ITS_DEFAULT_ORG_SLUG`), never part of the request."""
    rp_id, _origin = relying_party(request.app.state.settings)
    org_id = await _deployment_org(request)
    if org_id is None:
        return unanswerable_options(rp_id=rp_id)
    async with _identity_session(org_id, _engine(request)) as session:
        return await authentication_options(
            session, None, org_id=org_id, rp_id=rp_id, purpose="authenticate"
        )


async def passkey_sign_in(request: Request, *, credential: dict[str, Any]) -> SignIn:
    """A contact's passkey alone: user verification required and checked
    (Task 5 of phase 1), so it is two factors and the session is complete
    (D27). Refused alike -- 401 `passkey_rejected` -- for an unknown, spent
    or forged assertion, a technician's passkey (decision 1), a person not
    active, a passkey factor locked or waiting (decision 2), and an
    organization that is not active or that the slug does not name (Task 6
    review note). Nothing is counted (decision 2). The challenge is spent in
    this transaction, which commits before the refusal (`passkeys.py`, "THE
    CALLER'S TRANSACTION").

    WHERE EACH IS REFUSED. The assertion is verified before anything about
    the organization is looked at: a suspended organization's is verified in
    full, as an active one's, and then refused. A slug that names no
    organization goes through the same verification call, pinned to
    `_NO_ORG`, which holds no challenge: it is refused AT THE CHALLENGE, as a
    replayed or expired assertion is -- sooner than one whose signature is
    checked. Its answer is the same; its time may differ, and the slug is
    the server's configuration, never the request's, so nobody can choose
    to probe with it."""
    rp_id, origin = relying_party(request.app.state.settings)
    org_id = await _deployment_org(request) or _NO_ORG
    result: SignIn | None = None
    async with _identity_session(org_id, _engine(request)) as session:
        try:
            key = await verify_authentication(
                session, credential, expected_origin=origin, rp_id=rp_id, purpose="authenticate"
            )
        except PasskeyRejected:
            key = None
        organization = await session.get(Organization, org_id)
        user = await session.get(User, key.user_id) if key is not None else None
        if (
            organization is not None
            and organization.status == _ORG_ACTIVE
            and user is not None
            and user.kind == CONTACT_KIND
            and user.status == ACTIVE_STATUS
        ):
            state = await check_allowed(session, user.id, "passkey")
            if state.allowed:
                # No password was presented: the "password set by mail" mark
                # stays (fix round 1, coordinator decision; not the plan's
                # Step 4). The passkey's failures are cleared.
                await record_success(session, user.id, "passkey", clear_mail_mark=False)
                await _audit(
                    session, user, "auth.signed_in", {"method": "passkey", "trusted_device": False}
                )
                result = await started(session, request, user, state="signed_in", factor_done=True)
    if result is None:
        raise _passkey_rejected()
    return result


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


async def _has_second_factor(session: AsyncSession, user: User) -> bool:
    """A SECOND FACTOR ACTIVATED, by the person's kind (plan decision 3). A
    technician: a passkey or a CONFIRMED authenticator app. A contact: the
    confirmed app alone -- a contact's passkey is another way in, not a second
    factor (operator decision C5). Recovery codes alone never count: they are
    a fallback of a factor, not one (design §2.2)."""
    has_passkey, has_totp, _codes = await summary(session, user.id)
    if user.kind == CONTACT_KIND:
        return has_totp
    return has_passkey or has_totp


async def _may_be_sent_a_reset(session: AsyncSession, user: User) -> bool:
    """Plan decision 4. A technician: a password and a second factor (final
    review C-I1, design §2.8). A contact: any way in, with or without a
    password, EXCEPT Microsoft alone (operator decision C6): the mailbox does
    not stand in for the client's own Microsoft policy. A second factor the
    contact turned on is still asked for at the next sign-in."""
    if user.kind == STAFF_KIND:
        return await session.get(AuthPassword, user.id) is not None and await _has_second_factor(
            session, user
        )
    return user.kind == CONTACT_KIND and await contact_may_reset(session, user.id)


async def send_reset_link(app: FastAPI, *, email: str, locale: str) -> None:
    """Run AFTER the 202 (phase 1 plan decision 8): nothing below changes the
    answer or its time, so nothing tells whether the address has an account
    -- or which kind.

    WHO IS SENT ONE is `_may_be_sent_a_reset`: never a technician without a
    second factor (final review C-I1; Q5, "nunca solo por correo"), never a
    contact who has not accepted an invitation or who signs in only with
    Microsoft (C6). The mail promises the second factor only to somebody who
    has one activated."""
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
            # The only cap on the mails one address is sent (final review
            # A-M1): during a Redis outage none goes. After the 202, so the
            # answer does not change.
            fail_closed=True,
        ):
            return
        found = await find_person(engine, org_slug=settings.default_org_slug, email=address)
        if found is None or found.status != ACTIVE_STATUS:
            return
        async with _identity_session(found.org_id, engine) as session:
            user = await session.get(User, found.user_id)
            if user is None or not await _may_be_sent_a_reset(session, user):
                return
            second_factor = await _has_second_factor(session, user)
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
                    action_tokens.link(settings.public_console_url, "reset-password", token),
                    second_factor=second_factor,
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


async def _may_still_reset(session: AsyncSession, user: User) -> bool:
    """At the link's use, under `lock_person`: a technician still has a second
    factor (§2.8); a contact still has a way in, and not Microsoft alone (plan
    decision 4, C6)."""
    if user.kind == STAFF_KIND:
        return await _has_second_factor(session, user)
    return await contact_may_reset(session, user.id)


async def reset_password(request: Request, *, token: str, password: str) -> None:
    """A reset link: a new password, every session ended, every device
    forgotten, the password's failures cleared (plan decision 9). No session
    is started (OWASP [S8]). The server's recovery link has routes of its own
    (Task 8) and is refused here."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    spent_dead = False
    async with _identity_session(org_id, _engine(request)) as session:
        # The person, then the link (`_consumed`, Task 8 fix round 1).
        row = await _consumed(
            session, digest=action_tokens.digest(token), purposes=(action_tokens.RESET,)
        )
        user = await session.get(User, row.user_id) if row is not None else None
        if (
            row is None
            or user is None
            or user.kind not in (STAFF_KIND, CONTACT_KIND)
            or user.status != ACTIVE_STATUS
        ):
            raise _invalid_link()
        # One lock with an administrator's factor reset (`factors.
        # lock_person`, item 8): the factors read below, and the mark the store
        # writes, cannot interleave with a reset that removes the factors.
        await lock_person(session, user.id)
        if not await _may_still_reset(session, user):
            # A link sent while the account could be reset, used after that
            # stopped being true (an administrator's factor reset; a contact's
            # access reset by a technician, Task 8): the same mailbox-only
            # path as `send_reset_link`'s. It is SPENT, then refused like
            # every dead link (re-review Minor 5): nothing else is written,
            # the `consume` commits with the block, and the 410 is raised
            # after it. Left live, the link would work again once the person
            # enrols a new factor.
            spent_dead = True
        else:
            # Raises a 422 that rolls the `consume` back: the link stays usable.
            await staff_password.require_acceptable(
                password,
                email=user.email,
                display_name=user.display_name,
                org_name=await staff_password.organization_name(session, org_id),
                minimum=staff_password.minimum_for(user.kind),
            )
            # `by_mail`: the mailbox chose this password, so an administrator's
            # factor reset is refused until a second factor is verified
            # (operator decision 2026-11-05).
            await staff_password.store(
                session, org_id=org_id, user_id=user.id, plain=password, by_mail=True
            )
            await reset_factor(session, user.id, "password")
            await end_all_for_user(session, user.id, "password_changed")
            await revoke_all_for_user(session, user.id)
            await _audit(session, user, "auth.password_changed", {"via": row.purpose})
            await notices.tell(
                session, user, "password_changed", vault=_vault(request), actor_id=None
            )
    if spent_dead:
        raise _invalid_link()


# --------------------------------------------------------------------------- #
# the invitation: a technician's (Task 8, design §7) or, since phase 2, a
# client's contact's (phase 2 Task 8, design §3; D40)
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


@dataclass(frozen=True)
class Invitation:
    """What an invitation's page needs (design §3, §7): who it is for, and how
    they may accept it."""

    email: str
    display_name: str
    kind: str
    password_minimum: int
    #: A contact may accept with a passkey instead of a password (D40).
    passkey: bool
    #: Sign-in with Microsoft is set up for this person (Task 9 offers it).
    microsoft: bool


async def invitee(session: AsyncSession, *, digest: bytes, consume: bool) -> User:
    """The person of a live, unaccepted invitation -- a technician or a
    client's contact -- or `invalid_link`. With `consume`, the link is spent
    in the lock order: the person, then the link (`_consumed`). See
    `_invitation`."""
    _row, user = await _invitation(session, digest=digest, consume=consume)
    return user


async def _invitation(
    session: AsyncSession, *, digest: bytes, consume: bool
) -> tuple[AuthActionToken, User]:
    """`invitee`, and the link's row with it (`invite_passkey` puts it back).

    NEVER A SUPER-ADMIN (Task 8 review, phase 1). No organization can grant
    the role, so none invites its holder (`user_admin.service.resend_invite`
    refuses it); a link that names one anyway -- however it came to exist --
    sets no password, and answers what every dead link answers. NEVER
    SOMEBODY WHO ALREADY HAS A WAY IN (`methods.has_sign_in_method`: a
    password, a passkey or a Microsoft link).

    Nothing is written before a refusal here but the `consume`, and the
    refusal rolls it back with the block: a link that names nobody usable was
    never a link to spend."""
    purposes = (action_tokens.INVITE,)
    if consume:
        row = await _consumed(session, digest=digest, purposes=purposes)
    else:
        row = await action_tokens.peek_digest(session, digest=digest, purposes=purposes)
    user = await session.get(User, row.user_id) if row is not None else None
    if (
        row is None
        or user is None
        or user.kind not in (STAFF_KIND, CONTACT_KIND)
        or user.status != ACTIVE_STATUS
        or await has_sign_in_method(session, user.id)
        or (user.kind == STAFF_KIND and await _holds_super_admin(session, user.id))
    ):
        raise _invalid_link()
    return row, user


async def microsoft_offered(session: AsyncSession, user: User) -> bool:
    """Whether this person could sign in with Microsoft here: it is on and,
    for a technician, Novieri's tenant is set; for a contact, their client
    lists a tenant (design §4, D32)."""
    config = await entra_settings.read(session, user.org_id)
    if not config.usable:
        return False
    if user.kind == STAFF_KIND:
        return config.staff_tenant_id is not None
    tenants = await session.scalar(
        select(Client.microsoft_tenants).where(Client.id == user.client_id)
    )
    return bool(tenants)


async def contact_admitted(
    session: AsyncSession, request: Request, user: User, *, method: str
) -> SignIn:
    """A contact accepted their invitation (D40): the portal role on their own
    client (`grant_portal`), audited `contact.invite_accepted`, and a complete
    session -- a contact has no second factor to enrol. Public: Task 9's
    Microsoft acceptance ends here too."""
    await grant_portal(session, user)
    await _audit(
        session,
        user,
        "contact.invite_accepted",
        {"client_id": str(user.client_id), "method": method},
        client_id=user.client_id,
    )
    await _audit(session, user, "auth.signed_in", {"method": "invite", "trusted_device": False})
    return await started(session, request, user, state="signed_in", factor_done=True)


async def invite_details(request: Request, *, token: str) -> Invitation:
    """Who an invitation is for and how they may accept it. Leaves the link
    unused."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        user = await invitee(session, digest=action_tokens.digest(token), consume=False)
        return Invitation(
            email=user.email,
            display_name=user.display_name,
            kind=user.kind,
            password_minimum=staff_password.minimum_for(user.kind),
            passkey=user.kind == CONTACT_KIND,
            microsoft=await microsoft_offered(session, user),
        )


async def accept_invite(request: Request, *, token: str, password: str) -> SignIn:
    """The invitee's own password. A technician: a session waiting for the
    first factor (design §2.2: the person reaches the console only after
    registering one). A contact: signed in, the portal granted
    (`contact_admitted`). A refused password rolls the `consume` back: the
    link stays usable."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        user = await invitee(session, digest=action_tokens.digest(token), consume=True)
        # Raises a 422 that rolls the `consume` back: the link stays usable.
        await staff_password.require_acceptable(
            password,
            email=user.email,
            display_name=user.display_name,
            org_name=await staff_password.organization_name(session, org_id),
            minimum=staff_password.minimum_for(user.kind),
        )
        await staff_password.store(session, org_id=org_id, user_id=user.id, plain=password)
        await _audit(session, user, "auth.password_changed", {"via": "invite"})
        if user.kind == CONTACT_KIND:
            return await contact_admitted(session, request, user, method="password")
        return await started(session, request, user, state="enroll_required")


async def invite_passkey_options(request: Request, *, token: str) -> dict[str, Any]:
    """The creation options of a contact's first passkey, from the invitation
    (D40): discoverable (D28), user verification required. Leaves the link
    unused. A technician's invitation chooses a password (409)."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    rp_id, _origin = relying_party(request.app.state.settings)
    async with _identity_session(org_id, _engine(request)) as session:
        user = await invitee(session, digest=action_tokens.digest(token), consume=False)
        if user.kind != CONTACT_KIND:
            raise _unavailable()
        return await registration_options(
            session, user, rp_id=rp_id, rp_name=notices.PRODUCT, discoverable=True
        )


async def invite_passkey(
    request: Request, *, token: str, credential: dict[str, Any], name: str
) -> SignIn:
    """A contact accepts with a passkey. Task 8's decision 2 (B2-30): the link
    is consumed under its row lock BEFORE the passkey is verified; a refused
    passkey puts it back (`used_at` NULL), the challenge stays spent, and the
    block commits both before the 400 (`_passkey_rejected`: 400, not 401,
    where the page is not a sign-in screen). A technician's invitation
    chooses a password (409), and its link is left as it was."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    rp_id, origin = relying_party(request.app.state.settings)
    digest = action_tokens.digest(token)
    result: SignIn | None = None
    async with _identity_session(org_id, _engine(request)) as session:
        # The kind first, with the link left as it is: a technician's is a
        # 409 that spends nothing.
        if (await invitee(session, digest=digest, consume=False)).kind != CONTACT_KIND:
            raise _unavailable()
        # Then consumed in the lock order -- the person, then the link
        # (`_consumed`) -- with every check made again under the locks.
        row, user = await _invitation(session, digest=digest, consume=True)
        try:
            await verify_registration(
                session, user, credential, name=name, expected_origin=origin, rp_id=rp_id
            )
        except PasskeyRejected:
            # The challenge was spent in THIS transaction (`passkeys.py`); the
            # link is put back in it, and the block commits both before the
            # refusal. Nobody else took the link meanwhile: its row is locked.
            row.used_at = None
            await session.flush()
        else:
            # A2-47: audited as every passkey is (`enroll_passkey`); no
            # notice, as a password chosen here has none -- the person is on
            # the page, accepting.
            await _audit(session, user, "auth.factor_added", {"factor": "passkey", "name": name})
            result = await contact_admitted(session, request, user, method="passkey")
    if result is None:
        raise _passkey_rejected(status.HTTP_400_BAD_REQUEST)
    return result


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


#: The `code` of `accept_recovery`'s refusal while an emailed link set the
#: password and the request brought no new one (operator decision 2026-11-05,
#: «Pedir contraseña nueva si hay marca»). The console's recovery page
#: matches it: change it with `frontend/src/modules/auth/RecoverPage.tsx`.
RECOVERY_NEEDS_PASSWORD = "recovery_needs_password"


def _needs_password() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_409_CONFLICT,
        code=RECOVERY_NEEDS_PASSWORD,
        detail=tr(
            "Your password was last set with an emailed link. Choose a new password to finish "
            "recovering your account."
        ),
    )


async def recovery_details(request: Request, *, token: str) -> tuple[str, bool]:
    """The address a recovery link is for, and whether accepting it needs a
    new password: the "password set by mail" mark (`factors.set_by_mail`).
    Leaves the link unused.

    Read WITHOUT the person's lock: it is what the page asks first, and only
    a hint. `accept_recovery` reads the mark again under the lock, so a mail
    reset that lands between this answer and the press is caught there."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        user = await _recovering(session, token, consume=False)
        return user.email, await set_by_mail(session, user.id)


async def accept_recovery(request: Request, *, token: str, password: str | None = None) -> SignIn:
    """Every second factor, every remembered device and every session go; a
    session waiting for a new first factor is opened, pending until
    `enroll/finish`.

    A NEW PASSWORD IS ALWAYS OFFERED, AND APPLIED WHEN GIVEN (operator
    decision 2026-11-05, «Ofrecerla siempre, aplicar»; design §7). The link
    already grants the whole account, so letting its holder also set the
    password grants nothing new; and it closes what the mark alone cannot: a
    mark cleared between the details and this press, and a later recovery
    after somebody holding the mailbox and a factor cleared it by signing in.
    A given `password` is held to `staff_password.require_acceptable` (422
    `password_rejected`, the answer of every other password route), stored by
    `staff_password.store` (which clears the mark) in this transaction, and
    recorded and told as a mailed reset's is (`reset_password`):
    `auth.password_changed` with `via` the link's purpose, and the
    `password_changed` notice. No password, and an empty one, is none: the
    password stays (plan decision 10: who recovers lost the factors, not the
    password) --

    UNLESS AN EMAILED LINK SET IT (operator decision 2026-11-05, «Pedir
    contraseña nueva si hay marca»). While the "password set by mail" mark is
    set (`factors.set_by_mail`) the current password may be whoever holds the
    mailbox's. Kept, it would still work after the person recovered; and the
    mark goes at their first factor, so a later factor reset would hand the
    account to the mailbox again. So then no password is a 409
    `recovery_needs_password`, and the page asks.

    Both refusals are raised inside the block, when nothing but the `consume`
    has been written and BEFORE any notice: it rolls back, the link stays
    usable, and no mail went. The mark is read under `factors.lock_person` --
    the lock `reset_password` and `user_admin.service.reset_factors` take --
    and before `clear_second_factors`, which clears it; so a mail reset that
    commits between the details (`recovery_details`) and this press is waited
    for and seen, never read as absent.

    THE NOTICES ARE THE LAST ACTS (fix round 3): a notice is mail, which does
    not roll back. Both follow the new session's creation, the last step that
    can fail: `password_changed` first, when this press set one, then
    `factors_reset`."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        user = await _recovering(session, token, consume=True)
        await lock_person(session, user.id)
        # Read HERE, under the lock and before `clear_second_factors`, which
        # clears the mark: read after it, this refusal would never run.
        if not password and await set_by_mail(session, user.id):
            raise _needs_password()
        password_set = False
        if password:
            # Raises a 422 that rolls the `consume` back: the link stays usable.
            await staff_password.require_acceptable(
                password,
                email=user.email,
                display_name=user.display_name,
                org_name=await staff_password.organization_name(session, org_id),
            )
            await staff_password.store(session, org_id=org_id, user_id=user.id, plain=password)
            await _audit(session, user, "auth.password_changed", {"via": action_tokens.RECOVER})
            password_set = True
        await clear_second_factors(session, user.id)
        await end_all_for_user(session, user.id, "factors_reset")
        await revoke_all_for_user(session, user.id)
        await _audit(session, user, "user.recovery_link_used", {"email": user.email})
        signed = await started(session, request, user, state="enroll_required")
        # The last acts (fix round 3): after the new session is made, so a
        # failure there cannot leave a mail -- and a relay checkout whose
        # audit row rolled back -- for a recovery that did not happen. The
        # password first, in the order the acts were done, then the factors.
        if password_set:
            await notices.tell(
                session, user, "password_changed", vault=_vault(request), actor_id=None
            )
        await notices.tell(session, user, "factors_reset", vault=_vault(request), actor_id=None)
        return signed
