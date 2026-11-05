"""A person's second factors: what they have, wiping them, and a failure counted.

A FAILURE IS COUNTED IN THE DATABASE AND THE LOCKOUT IS AUDITED (design §2.3,
D16): `failures.record_failure` (Task 4) disables the factor at the hundredth
consecutive failure and writes `auth.factor_disabled` itself; this module adds
the notice to the person (design §2.7).

ONE TRANSACTION PER ATTEMPT. `gate` calls `failures.check_allowed`, which
takes an advisory lock on (person, factor) held until the caller's
transaction ends (Task 4). The caller keeps `gate` -> verify ->
`count_failure` / `record_success` in that ONE transaction and refuses after
it commits: then concurrent attempts are serialised and the wait the n-th
failure starts already stops the (n+1)-th, rather than the whole burst.
"""

from uuid import UUID

from fastapi import status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import notices
from app.core.auth.failures import Factor, FactorState, check_allowed, record_failure, reset_factor
from app.core.auth.recovery import remaining
from app.core.auth.refusals import AuthRefusal
from app.core.auth.totp import has_totp, remove_totp
from app.core.i18n import tr
from app.core.vault import VaultService
from app.models import User
from app.models.auth import AuthPasskey, AuthRecoveryCode

_EVERY_FACTOR: tuple[Factor, ...] = ("password", "totp", "passkey", "recovery")


async def summary(session: AsyncSession, user_id: UUID) -> tuple[bool, bool, int]:
    """(has a passkey, has a CONFIRMED TOTP, unused recovery codes)."""
    passkeys = (
        await session.execute(
            select(func.count()).select_from(AuthPasskey).where(AuthPasskey.user_id == user_id)
        )
    ).scalar_one()
    return passkeys > 0, await has_totp(session, user_id), await remaining(session, user_id)


async def clear_second_factors(session: AsyncSession, user_id: UUID) -> None:
    """Every passkey, the TOTP (pending or confirmed), every recovery code and
    every failure counter -- a disabled factor's included -- of one person (a
    recovery link here; an administrator's reset in Task 8)."""
    await session.execute(delete(AuthPasskey).where(AuthPasskey.user_id == user_id))
    await remove_totp(session, user_id)
    await session.execute(delete(AuthRecoveryCode).where(AuthRecoveryCode.user_id == user_id))
    for factor in _EVERY_FACTOR:
        await reset_factor(session, user_id, factor)


async def gate(
    session: AsyncSession, user_id: UUID, factor: Factor, *, device_id: UUID | None = None
) -> None:
    """Refuse a factor step that is waiting or disabled (design §2.3). Said to
    the person: they hold a session already -- a second-factor step follows a
    proved password, and the password's own gate (`account.change_password`)
    follows a full sign-in. `device_id`: the password's per-device counter
    (`failures.check_allowed`)."""
    state = await check_allowed(session, user_id, factor, device_id=device_id)
    if state.disabled:
        raise AuthRefusal(
            status.HTTP_403_FORBIDDEN,
            code="factor_disabled",
            detail=(
                tr(
                    "your password is locked after too many failed attempts; set a new one with "
                    "the link on the sign-in page, or ask an administrator to reset your sign-in "
                    "methods"
                )
                if factor == "password"
                else tr(
                    "this second factor is locked after too many failed attempts; an "
                    "administrator can reset your factors"
                )
            ),
        )
    if not state.allowed:
        refusal = AuthRefusal(
            status.HTTP_429_TOO_MANY_REQUESTS,
            code="factor_wait",
            detail=tr(
                "too many failed attempts; try again in {seconds} seconds", seconds=state.wait_s
            ),
            extra={"wait_s": state.wait_s},
        )
        refusal.headers = {"Retry-After": str(state.wait_s)}
        raise refusal


async def count_failure(
    session: AsyncSession,
    user: User,
    factor: Factor,
    *,
    vault: VaultService,
    device_id: UUID | None = None,
) -> FactorState:
    """One failure (Task 4 counts, disables and audits) -- on the device's
    counter with `device_id`, the password only; when it was the one that
    locked the factor, tell the person. Called only after `gate` or
    `check_allowed` allowed the attempt, so `disabled` here means THIS
    failure disabled it."""
    state = await record_failure(
        session, org_id=user.org_id, user_id=user.id, factor=factor, device_id=device_id
    )
    if state.disabled:
        await notices.tell(
            session, user, "factor_disabled", vault=vault, actor_id=None, factor=factor
        )
    return state
