"""TOTP (RFC 6238): the app factor, and the one rule pyotp does not keep for us.

Design `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §2.2:
30-second step, 6 digits, one step of tolerance either way, a 160-bit secret
[S12, S13]; a code accepted once is never accepted again; the secret is sealed
by the vault.

WHAT PYOTP DOES AND DOES NOT DO. `pyotp` 2.10.0 computes the code
(`OTP.generate_otp`, the RFC 4226 truncation) and builds the `otpauth://` URI
(`TOTP.provisioning_uri`). It keeps no state, so it cannot refuse a code it
has already accepted: its README tells the application to store "the most
recently authenticated timestamp" and reject what is not newer, and the helper
for that (`verify_and_get_timecode`) is on its development branch, not in the
release [S43]. NIST SP 800-63B-4 §3.1.4: "Verifiers SHALL accept a given OTP
only once while it is valid". So this module does not call `TOTP.verify`: it
computes the three candidate steps itself, compares each in constant time,
and accepts a match only for a step strictly greater than
`auth_totp.last_step` -- written with a conditional UPDATE, so two requests
racing with one code cannot both win.

PENDING AND CONFIRMED. A row whose `last_step` is NULL has never accepted a
code: it is an enrolment in progress, not a factor. `verify_totp` and
`has_totp` ignore it; `confirm_secret` is the only thing that accepts against
it, and accepting writes `last_step`, which is what turns it into a factor.
`stage_secret` never replaces a confirmed row: removing a factor is its own
act (`remove_totp`), with its own recent confirmation upstream (§2.5).

THE SECRET. 20 random bytes, sealed with `VaultService.seal_bytes` bound to
the person's id (AAD `org|user_id`, `app/core/vault.py:190-212`), so a sealed
secret copied onto another person's row does not open. It is opened for one
comparison and dropped; never logged, never returned (the URI is shown once,
at enrolment, by the caller).

FAILURES ARE NOT COUNTED HERE: the caller wraps every call in
`app/core/auth/failures.py`; see that module.
"""

import base64
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime
from uuid import UUID

import pyotp
from cryptography.exceptions import InvalidTag
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.vault import VaultService
from app.models.auth import AuthTotp

__all__ = [
    "DIGITS",
    "SECRET_BYTES",
    "STEP_S",
    "WINDOW",
    "TotpAlreadyEnrolled",
    "confirm_secret",
    "has_totp",
    "new_secret",
    "otpauth_uri",
    "remove_totp",
    "stage_secret",
    "step_of",
    "verify_totp",
]

logger = logging.getLogger(__name__)

STEP_S = 30
DIGITS = 6
#: Steps of tolerance either side of the current one.
WINDOW = 1
#: 160 bits, RFC 4226's recommended key length.
SECRET_BYTES = 20

_CODE = re.compile(r"[0-9]{6}")


class TotpAlreadyEnrolled(Exception):
    """A confirmed TOTP exists; it is removed first, never overwritten."""


def new_secret() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)


def _b32(secret: bytes) -> str:
    # 20 bytes are 32 base32 characters exactly: there is no padding to strip.
    return base64.b32encode(secret).decode("ascii")


def otpauth_uri(secret: bytes, *, account: str, issuer: str) -> str:
    """The Key Uri Format string an authenticator app scans. 6 digits, 30 s and
    SHA-1 are its defaults, so pyotp leaves them out."""
    return pyotp.TOTP(
        _b32(secret), digits=DIGITS, interval=STEP_S, name=account, issuer=issuer
    ).provisioning_uri()


def step_of(now: datetime) -> int:
    """The RFC 6238 time step `now` falls in. A naive datetime is refused: its
    `timestamp()` is local time."""
    if now.tzinfo is None:
        raise ValueError(  # i18n-exempt: programming error, never reaches a person
            "a TOTP check needs a timezone-aware time"
        )
    return int(now.timestamp()) // STEP_S


def _matching_step(secret: bytes, code: str, now: datetime, after: int | None) -> int | None:
    """The newest step within the window whose code is `code` and that is newer
    than `after`; None otherwise. Every candidate is compared, so the time taken
    does not say which step matched."""
    otp = pyotp.TOTP(_b32(secret), digits=DIGITS, interval=STEP_S)
    current = step_of(now)
    accepted: int | None = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        if step < 0:
            continue
        matches = hmac.compare_digest(otp.generate_otp(step), code)
        if matches and (after is None or step > after):
            accepted = step
    return accepted


async def stage_secret(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    secret: bytes,
    vault: VaultService,
    now: datetime | None = None,
) -> None:
    """Keep `secret` as the person's PENDING TOTP until `confirm_secret` accepts
    a code from it. Replaces a pending one; raises `TotpAlreadyEnrolled` when a
    confirmed one exists (the upsert's `where`: no row comes back)."""
    sealed = (
        await vault.seal_bytes(session, org_id=org_id, key_id=user_id, plaintext=secret)
    ).encode("ascii")
    at = now or datetime.now(UTC)
    upsert = (
        pg_insert(AuthTotp)
        .values(user_id=user_id, org_id=org_id, secret_sealed=sealed, last_step=None, created_at=at)
        .on_conflict_do_update(
            index_elements=["user_id"],
            set_={"secret_sealed": sealed, "created_at": at},
            where=AuthTotp.last_step.is_(None),
        )
        .returning(AuthTotp.user_id)
    )
    if (await session.execute(upsert)).scalar_one_or_none() is None:
        raise TotpAlreadyEnrolled()


async def _open(session: AsyncSession, vault: VaultService, row: AuthTotp) -> bytes | None:
    try:
        return await vault.open_bytes(
            session, org_id=row.org_id, key_id=row.user_id, sealed=row.secret_sealed.decode("ascii")
        )
    except (InvalidTag, ValueError):
        # A seal that does not open for THIS person: moved from another row,
        # or damaged. Refused, never a 500, and never the seal in a log.
        logger.warning("totp secret did not open for its row; the code is refused")
        return None


async def _accept(
    session: AsyncSession,
    user_id: UUID,
    code: str,
    *,
    vault: VaultService,
    now: datetime,
    confirmed: bool,
) -> bool:
    typed = code.replace(" ", "")
    if not _CODE.fullmatch(typed):
        return False
    row = (
        await session.execute(select(AuthTotp).where(AuthTotp.user_id == user_id))
    ).scalar_one_or_none()
    if row is None or (row.last_step is not None) != confirmed:
        return False
    secret = await _open(session, vault, row)
    if secret is None:
        return False
    step = _matching_step(secret, typed, now, row.last_step)
    del secret
    if step is None:
        return False
    # THE GUARD IS THE REPLAY PROTECTION. Pending: still pending, and still the
    # secret the code was checked against. Confirmed: no newer step accepted
    # in between.
    guard = (
        AuthTotp.last_step < step
        if confirmed
        else (AuthTotp.last_step.is_(None)) & (AuthTotp.secret_sealed == row.secret_sealed)
    )
    won = (
        await session.execute(
            update(AuthTotp)
            .where(AuthTotp.user_id == user_id, guard)
            .values(last_step=step)
            .returning(AuthTotp.user_id)
        )
    ).scalar_one_or_none()
    return won is not None


async def confirm_secret(
    session: AsyncSession, user_id: UUID, code: str, *, vault: VaultService, now: datetime
) -> bool:
    """Accept `code` against the PENDING secret; on success it is a factor."""
    return await _accept(session, user_id, code, vault=vault, now=now, confirmed=False)


async def verify_totp(
    session: AsyncSession, user_id: UUID, code: str, *, vault: VaultService, now: datetime
) -> bool:
    """Accept `code` against the CONFIRMED secret: ±`WINDOW` steps, and only a
    step newer than the last one accepted."""
    return await _accept(session, user_id, code, vault=vault, now=now, confirmed=True)


async def has_totp(session: AsyncSession, user_id: UUID) -> bool:
    found = await session.scalar(
        select(AuthTotp.user_id).where(AuthTotp.user_id == user_id, AuthTotp.last_step.is_not(None))
    )
    return found is not None


async def remove_totp(session: AsyncSession, user_id: UUID) -> bool:
    """Drop the person's TOTP, pending or confirmed. True if there was one."""
    removed = (
        await session.execute(
            delete(AuthTotp).where(AuthTotp.user_id == user_id).returning(AuthTotp.user_id)
        )
    ).scalar_one_or_none()
    return removed is not None
