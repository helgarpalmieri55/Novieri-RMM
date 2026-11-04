"""Failed attempts per person and per factor: the wait, and the stop at 100.

Design `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §2.3.

* COUNTED PER ACCOUNT AND PER FACTOR, never per address alone. OWASP: "The
  counter of failed logins should be associated with the account itself,
  rather than the source IP address" [S5]. Per-address limits are a second
  layer (`app/core/rate_limit.py`), not this one.
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
  At `DISABLE_AT` the row gets `disabled_at`, and that is audited -- once, on
  the transition. A success does NOT clear a disabled factor ("Authenticators
  that have been disabled SHALL be required to rebind"); `reset_factor` is the
  administrator's act.
* A SUCCESS CLEARS THE COUNT (NIST: "the verifier SHOULD disregard any
  previous failed attempts for the authenticators used in the successful
  authentication").

THE FAILURES THEMSELVES ARE NOT AUDITED (D16): a failed attempt for an account
that may not exist has no organization to chain to, and one row per wrong
code would flood the chain. The lockout is.

ONE STATEMENT PER FAILURE. `record_failure` is an INSERT ... ON CONFLICT DO
UPDATE ... RETURNING: concurrent attempts each add one, so running attempts in
parallel does not stay under 100.

THE CALLER'S SESSION MUST COMMIT. Verify, record, close the session block,
and refuse AFTER it: an HTTPException raised inside the block rolls the
failure back, and the counter never reaches 100.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Literal
from uuid import UUID

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.models import User
from app.models.auth import AuthFailure

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
    when they may not yet; `disabled` means only an administrator can say yes."""

    allowed: bool
    wait_s: int
    disabled: bool


def backoff_s(consecutive: int) -> int:
    """Seconds the next attempt waits after `consecutive` failures in a row."""
    if consecutive < BACKOFF_FROM:
        return 0
    return min(1 << (consecutive - BACKOFF_FROM), BACKOFF_CAP_S)


def _state(
    consecutive: int, last_failed_at: datetime, disabled_at: datetime | None, now: datetime
) -> FactorState:
    if disabled_at is not None:
        return FactorState(allowed=False, wait_s=0, disabled=True)
    wait = backoff_s(consecutive)
    remaining = (last_failed_at + timedelta(seconds=wait) - now).total_seconds()
    if wait == 0 or remaining <= 0:
        return FactorState(allowed=True, wait_s=0, disabled=False)
    return FactorState(allowed=False, wait_s=ceil(remaining), disabled=False)


async def check_allowed(
    session: AsyncSession, user_id: UUID, factor: Factor, *, now: datetime | None = None
) -> FactorState:
    row = (
        await session.execute(
            select(AuthFailure).where(AuthFailure.user_id == user_id, AuthFailure.factor == factor)
        )
    ).scalar_one_or_none()
    if row is None:
        return FactorState(allowed=True, wait_s=0, disabled=False)
    return _state(row.consecutive, row.last_failed_at, row.disabled_at, now or datetime.now(UTC))


async def record_failure(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    factor: Factor,
    now: datetime | None = None,
) -> FactorState:
    """Count one failure and answer the state it leaves. At `DISABLE_AT` the
    factor is disabled and the lockout is audited, exactly once."""
    at = now or datetime.now(UTC)
    upsert = (
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
            set_={
                "consecutive": AuthFailure.consecutive + 1,
                "last_failed_at": at,
                "disabled_at": case(
                    (
                        AuthFailure.consecutive + 1 >= DISABLE_AT,
                        func.coalesce(AuthFailure.disabled_at, at),
                    ),
                    else_=AuthFailure.disabled_at,
                ),
            },
        )
        .returning(AuthFailure.consecutive, AuthFailure.disabled_at)
    )
    consecutive, disabled_at = (await session.execute(upsert)).one()
    if consecutive == DISABLE_AT:
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="auth.factor_disabled",
            target_type="user",
            target_id=str(user_id),
            metadata={"factor": factor, "consecutive": consecutive},
            # Informative: a contact's own client, NULL for a technician.
            client_id=await session.scalar(select(User.client_id).where(User.id == user_id)),
        )
    return _state(consecutive, at, disabled_at, at)


async def record_success(session: AsyncSession, user_id: UUID, factor: Factor) -> None:
    """Clear the factor's failures -- unless it is disabled, which only
    `reset_factor` undoes."""
    await session.execute(
        delete(AuthFailure).where(
            AuthFailure.user_id == user_id,
            AuthFailure.factor == factor,
            AuthFailure.disabled_at.is_(None),
        )
    )


async def reset_factor(session: AsyncSession, user_id: UUID, factor: Factor) -> None:
    """An administrator's act (design §7, "Restablecer factores"), audited by
    the caller: the factor's count and its disabled state go."""
    await session.execute(
        delete(AuthFailure).where(AuthFailure.user_id == user_id, AuthFailure.factor == factor)
    )
