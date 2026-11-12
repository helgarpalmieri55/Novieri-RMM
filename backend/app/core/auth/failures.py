"""Failed attempts per person and per factor: the wait, and the stop at 100.

Design `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §2.3.

* COUNTED PER ACCOUNT AND PER FACTOR, never per address alone. OWASP: "The
  counter of failed logins should be associated with the account itself,
  rather than the source IP address" [S5]. Per-address limits are a second
  layer (`app/core/rate_limit.py`), not this one.
* THE PASSWORD HAS TWO COUNTERS (operator decision 2026-11-05, design §2.3).
  An attempt from a browser whose `__Host-itms_device` cookie
  `devices.recognised` accepts for the account (`devices.presented`, the same
  rule without renewing the device) waits and counts on THAT device's own
  counter, `auth_trusted_devices.password_failures` -- pass its id as
  `device_id`. Every other attempt on the account's row here. So whoever
  knows an address but holds no cookie of the person's keeps only the
  unrecognised browsers waiting: OWASP, "Temporary account lockout after
  several failed attempts is too simple of a target for DoS attacks against
  legitimate users" ("Slow Down Online Guessing Attacks with Device
  Cookies"); NIST §3.2.2, "Additional techniques MAY be used to reduce the
  likelihood that an attacker will lock the legitimate claimant out". Only
  the password: the other factors run after a correct password, and only
  their holder can fail them.
* THE WAIT. From the `BACKOFF_FROM`-th consecutive failure the next attempt
  waits `2 ** (n - BACKOFF_FROM)` seconds after the n-th, capped at
  `BACKOFF_CAP_S`: 1 s, 2 s, 4 s ... 15 min. NIST SP 800-63B-4 §3.2.2 names the
  technique ("wait after a failed attempt for a period of time that increases
  as the subscriber account approaches its maximum allowance") and OWASP its
  shape ("starts as a very short period (e.g., one second), but doubles after
  each failed login attempt"). An attempt during the wait is not verified and
  not counted: the caller asks `check_allowed` first.
* THE STOP. NIST §3.2.2: "the verifier SHALL limit consecutive failed
  authentication attempts using a specific authenticator on a single
  subscriber account to no more than 100 by disabling that authenticator."
  Per account, so for the password it counts BOTH counters: the account's
  row plus every device's count of the person (`_consecutive`). At
  `DISABLE_AT` the account's row gets `disabled_at` -- the factor's state for
  every browser -- and that is audited, once, by the one statement that set
  it (`_disable`). A success does NOT clear a disabled factor
  ("Authenticators that have been disabled SHALL be required to rebind");
  `reset_factor` does.
* A SUCCESS CLEARS THE COUNT (NIST: "the verifier SHOULD disregard any
  previous failed attempts for the authenticators used in the successful
  authentication"): for the password, from any browser, both counters.

THE FAILURES THEMSELVES ARE NOT AUDITED (D16): a failed attempt for an account
that may not exist has no organization to chain to, and one row per wrong
code would flood the chain. The lockout is.

ONE STATEMENT PER FAILURE. `record_failure` adds one with a single INSERT ...
ON CONFLICT DO UPDATE (or, on a device, one UPDATE) ... RETURNING: concurrent
attempts each add one, so running attempts in parallel does not stay under
100. ONE LOCK FOR BOTH COUNTERS: `check_allowed` takes it per (person,
factor), whichever counter the attempt uses, so the sum the stop reads is
never raced.

THE CALLER'S SESSION MUST COMMIT. Verify, record, close the session block,
and refuse AFTER it: an HTTPException raised inside the block rolls the
failure back, and the counter never reaches 100.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.models import User
from app.models.auth import AuthFailure, AuthPassword, AuthTrustedDevice

__all__ = [
    "BACKOFF_CAP_S",
    "BACKOFF_FROM",
    "DISABLE_AT",
    "Factor",
    "FactorState",
    "backoff_s",
    "check_allowed",
    "record_failure",
    "record_success",
    "reset_factor",
]

Factor = Literal["password", "totp", "passkey", "recovery"]

#: The consecutive failure from which the next attempt waits.
BACKOFF_FROM = 5
#: The longest wait, in seconds: 15 minutes.
BACKOFF_CAP_S = 900
#: Consecutive failures that disable the factor (NIST's upper bound).
DISABLE_AT = 100


@dataclass(frozen=True)
class FactorState:
    """May the person try this factor now? `wait_s` is how long until they may,
    when they may not yet; `disabled` means only a reset can say yes."""

    allowed: bool
    wait_s: int
    disabled: bool


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError(  # i18n-exempt: programming error, never reaches a person
            "a failure check needs a timezone-aware time"
        )
    return now


def _device_counter(factor: Factor, device_id: UUID | None) -> UUID | None:
    if device_id is not None and factor != "password":
        raise ValueError(  # i18n-exempt: programming error, never reaches a person
            "only the password is counted per device"
        )
    return device_id


def backoff_s(consecutive: int) -> int:
    """Seconds the next attempt waits after `consecutive` failures in a row."""
    if consecutive < BACKOFF_FROM:
        return 0
    return min(1 << (consecutive - BACKOFF_FROM), BACKOFF_CAP_S)


def _state(
    consecutive: int, last_failed_at: datetime | None, disabled_at: datetime | None, now: datetime
) -> FactorState:
    if disabled_at is not None:
        return FactorState(allowed=False, wait_s=0, disabled=True)
    wait = backoff_s(consecutive)
    if wait == 0 or last_failed_at is None:
        return FactorState(allowed=True, wait_s=0, disabled=False)
    remaining = (last_failed_at + timedelta(seconds=wait) - now).total_seconds()
    if remaining <= 0:
        return FactorState(allowed=True, wait_s=0, disabled=False)
    return FactorState(allowed=False, wait_s=ceil(remaining), disabled=False)


async def _device_count(
    session: AsyncSession, user_id: UUID, device_id: UUID
) -> tuple[int, datetime | None] | None:
    """The device's own count and last failure; None when `device_id` is not
    one of `user_id`'s devices -- then there is no separate counter. Read by
    column, never as an entity: the identity map would hand back a row loaded
    before the lock was taken."""
    row = (
        await session.execute(
            select(
                AuthTrustedDevice.password_failures, AuthTrustedDevice.password_failed_at
            ).where(AuthTrustedDevice.id == device_id, AuthTrustedDevice.user_id == user_id)
        )
    ).one_or_none()
    return None if row is None else (row.password_failures, row.password_failed_at)


async def check_allowed(
    session: AsyncSession,
    user_id: UUID,
    factor: Factor,
    *,
    device_id: UUID | None = None,
    now: datetime | None = None,
) -> FactorState:
    """May the person try `factor` now? With `device_id` (the password only: a
    device `devices.presented` accepted for this person), the wait is that
    device's own; a disabled factor is disabled for every browser.

    TAKES A TRANSACTION-LEVEL ADVISORY LOCK on (user, factor), held until the
    caller's transaction ends, whichever counter the attempt uses. The caller
    MUST keep check -> verify -> record in ONE transaction: then concurrent
    attempts are serialised, and the n-th sees the failures of the n-1 before
    it, so the back-off and the stop at `DISABLE_AT` hold per attempt, not
    per burst."""
    at = _aware(now) if now is not None else datetime.now(UTC)
    device_id = _device_counter(factor, device_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"auth_failures:{user_id}:{factor}"},
    )
    row = (
        await session.execute(
            select(AuthFailure.consecutive, AuthFailure.last_failed_at, AuthFailure.disabled_at)
            .where(AuthFailure.user_id == user_id, AuthFailure.factor == factor)
        )
    ).one_or_none()
    disabled_at = row.disabled_at if row is not None else None
    if device_id is not None:
        own = await _device_count(session, user_id, device_id)
        if own is not None:
            return _state(own[0], own[1], disabled_at, at)
    if row is None:
        return FactorState(allowed=True, wait_s=0, disabled=False)
    return _state(row.consecutive, row.last_failed_at, disabled_at, at)


async def _consecutive(
    session: AsyncSession, user_id: UUID, factor: Factor
) -> tuple[int, datetime | None]:
    """The person's consecutive failures of `factor` -- for the password, the
    account's row plus every device's count -- and the factor's `disabled_at`."""
    row = (
        await session.execute(
            select(AuthFailure.consecutive, AuthFailure.disabled_at).where(
                AuthFailure.user_id == user_id, AuthFailure.factor == factor
            )
        )
    ).one_or_none()
    total: int = row.consecutive if row is not None else 0
    if factor == "password":
        on_devices: int = (
            await session.execute(
                select(func.coalesce(func.sum(AuthTrustedDevice.password_failures), 0)).where(
                    AuthTrustedDevice.user_id == user_id
                )
            )
        ).scalar_one()
        total += on_devices
    return total, row.disabled_at if row is not None else None


async def _disable(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, factor: Factor, at: datetime
) -> bool:
    """Set the factor's `disabled_at`; True only for the statement that set it.
    The conditional upsert answers a row only when it inserted one or found
    the factor enabled, so the transition is audited once however the calls
    interleave."""
    disabled = (
        await session.execute(
            pg_insert(AuthFailure)
            .values(
                org_id=org_id,
                user_id=user_id,
                factor=factor,
                consecutive=0,
                last_failed_at=at,
                disabled_at=at,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "factor"],
                set_={"disabled_at": at},
                where=AuthFailure.disabled_at.is_(None),
            )
            .returning(AuthFailure.disabled_at)
        )
    ).one_or_none()
    return disabled is not None


async def record_failure(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    factor: Factor,
    device_id: UUID | None = None,
    now: datetime | None = None,
) -> FactorState:
    """Count one failure -- on the device's counter with `device_id`, else on
    the account's -- and answer the state it leaves for that counter. At
    `DISABLE_AT` consecutive failures, both counters together, the factor is
    disabled and the lockout is audited, exactly once."""
    at = _aware(now) if now is not None else datetime.now(UTC)
    device_id = _device_counter(factor, device_id)
    own: int | None = None
    if device_id is not None:
        own = (
            await session.execute(
                update(AuthTrustedDevice)
                .where(AuthTrustedDevice.id == device_id, AuthTrustedDevice.user_id == user_id)
                .values(
                    password_failures=AuthTrustedDevice.password_failures + 1,
                    password_failed_at=at,
                )
                .returning(AuthTrustedDevice.password_failures)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
    if own is None:
        own = (
            await session.execute(
                pg_insert(AuthFailure)
                .values(
                    org_id=org_id,
                    user_id=user_id,
                    factor=factor,
                    consecutive=1,
                    last_failed_at=at,
                    disabled_at=None,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "factor"],
                    set_={"consecutive": AuthFailure.consecutive + 1, "last_failed_at": at},
                )
                .returning(AuthFailure.consecutive)
            )
        ).scalar_one()
    total, disabled_at = await _consecutive(session, user_id, factor)
    if total >= DISABLE_AT and await _disable(
        session, org_id=org_id, user_id=user_id, factor=factor, at=at
    ):
        disabled_at = at
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="auth.factor_disabled",
            target_type="user",
            target_id=str(user_id),
            metadata={"factor": factor, "consecutive": total},
            # Informative: a contact's own client, NULL for a technician.
            client_id=await session.scalar(select(User.client_id).where(User.id == user_id)),
        )
    return _state(own, at, disabled_at, at)


async def _clear_devices(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        update(AuthTrustedDevice)
        .where(AuthTrustedDevice.user_id == user_id, AuthTrustedDevice.password_failures > 0)
        .values(password_failures=0, password_failed_at=None)
        .execution_options(synchronize_session=False)
    )


async def record_success(
    session: AsyncSession, user_id: UUID, factor: Factor, *, clear_mail_mark: bool
) -> None:
    """Clear the factor's failures -- for the password, the account's row and
    every device's count, whichever browser succeeded -- unless it is
    disabled, which only `reset_factor` undoes.

    A SECOND FACTOR VERIFIED -- a passkey, a TOTP code or a recovery code, at
    sign-in or at step-up: every one passes here -- also clears the "password
    set by mail" mark (`AuthPassword.mail_reset_at`, operator decision
    2026-11-05): whoever set that password holds a factor too, so an
    administrator's factor reset is no longer refused. The password's own
    success is not a second factor and leaves it.

    `clear_mail_mark=False` leaves the mark for a factor proved WITHOUT THE
    PASSWORD PRESENTED in its session -- a contact's passkey without an
    address (own authentication phase 2, Task 6, coordinator decision of the
    fix round), and any second factor of a session that Microsoft, a passkey
    alone or the recovery link started (Task 9b, item B: the steps pass
    `flows._after_the_password`, the session's `first_step`): nothing then
    shows who set the password, so the mark stays -- for an administrator's
    factor reset and a later contact access reset to read. The failures are
    cleared all the same.

    REQUIRED, NO DEFAULT (Task 9b fix round 1), as `sessions.start_session`'s
    `first_step`: every caller says whether its success clears the mark, so a
    new one cannot clear it by forgetting to say. The password's own success
    passes False, which is what it does whatever it passes."""
    if factor != "password" and clear_mail_mark:
        await session.execute(
            update(AuthPassword)
            .where(AuthPassword.user_id == user_id, AuthPassword.mail_reset_at.is_not(None))
            .values(mail_reset_at=None)
            .execution_options(synchronize_session=False)
        )
    await session.execute(
        delete(AuthFailure).where(
            AuthFailure.user_id == user_id,
            AuthFailure.factor == factor,
            AuthFailure.disabled_at.is_(None),
        )
    )
    if factor == "password":
        # What the delete left is a disabled row: then the counts stay too.
        left = await session.scalar(
            select(AuthFailure.disabled_at).where(
                AuthFailure.user_id == user_id, AuthFailure.factor == factor
            )
        )
        if left is None:
            await _clear_devices(session, user_id)


async def reset_factor(session: AsyncSession, user_id: UUID, factor: Factor) -> None:
    """The factor's count and its disabled state go -- for the password, every
    device's count too. Audited by the caller. Three acts call it: an
    administrator's factor reset and the server's recovery link (every factor,
    through `factors.clear_second_factors`), and a password reset by mail (the
    password only, plan decision 9, so the wait is not a denial of service)."""
    await session.execute(
        delete(AuthFailure).where(AuthFailure.user_id == user_id, AuthFailure.factor == factor)
    )
    if factor == "password":
        await _clear_devices(session, user_id)
