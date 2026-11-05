"""Recovery codes: ten look-up secrets, shown once, stored salted and hashed.

Design `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §2.2
("10 códigos de recuperación de un solo uso, mostrados una vez y guardados
como hash" [S7]).

THE HASH IS A PASSWORD HASH, AND NIST SAYS WHY. A code is ten symbols of
Crockford base32: 50 bits. NIST SP 800-63B-4 §3.1.2.2: "Look-up secrets that
are shorter than the minimum security strength specified in the latest
revision of [SP800-131A] (i.e., 112 bits as of the date of this publication)
SHALL be stored in a salted and hashed form using a suitable password hashing
scheme ... The salt value SHALL be at least 32 bits in length". A bare SHA-256
of 50 bits is hours of GPU time from the database dump it was meant to
survive. So: argon2id with a 16-byte random salt per code (argon2-cffi's
`PasswordHasher`, whose encoded string carries salt and parameters together).

AT OWASP'S MINIMUM, NOT THE PASSWORD DEFAULT. A sign-in with a recovery code
checks the attempt against up to ten stored hashes; at argon2-cffi's default
(64 MiB, t=3, p=4, what `passwords.py` uses) that is ten password checks in one
request. OWASP's minimum, "m=19456 (19 MiB), t=2, p=1" [S4], is the documented
floor for a guessable human password; a random 50-bit code behind it is far
out of reach offline, and online attempts are bounded by `failures.py`. No
pepper: it would need a key version per row that `auth_recovery_codes` does
not carry, and the salted argon2id hash is what §3.1.2.2 asks for.

SINGLE USE (§3.1.2.2: "SHALL be used successfully only once"): a conditional
UPDATE ... WHERE used_at IS NULL, so two requests with one code cannot both
win.

THE FORMAT. `XXXXX-XXXXX` in Crockford's alphabet (no I, L, O, U): read aloud
and typed by hand. On the way in, case, spaces and the hyphen are ignored and
the confusable letters are read the way Crockford says (O as 0, I and L as 1).

Replacing the codes does not touch the recovery factor's failure count
(`failures.py`, Decisión del plan 7 of the plan that built this).
"""

import asyncio
import secrets
from datetime import UTC, datetime
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import AuthRecoveryCode

__all__ = [
    "ALPHABET",
    "CODE_COUNT",
    "new_codes",
    "normalise",
    "remaining",
    "replace_codes",
    "use_code",
]

CODE_COUNT = 10
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LENGTH = 10
_ALIASES = str.maketrans({"O": "0", "I": "1", "L": "1"})
_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16)


def new_codes() -> list[str]:
    """`CODE_COUNT` distinct codes, `XXXXX-XXXXX`."""
    codes: list[str] = []
    while len(codes) < CODE_COUNT:
        raw = "".join(secrets.choice(ALPHABET) for _ in range(_LENGTH))
        code = f"{raw[:5]}-{raw[5:]}"
        if code not in codes:
            codes.append(code)
    return codes


def normalise(code: str) -> str | None:
    """The ten symbols a typed code means, or None if it cannot be one."""
    cleaned = code.upper().replace("-", "").replace(" ", "").translate(_ALIASES)
    if len(cleaned) != _LENGTH or any(ch not in ALPHABET for ch in cleaned):
        return None
    return cleaned


def _hash_all(normalised: list[str]) -> list[bytes]:
    return [_HASHER.hash(code).encode("ascii") for code in normalised]


def _find(rows: list[tuple[UUID, bytes]], wanted: str) -> UUID | None:
    for row_id, stored in rows:
        try:
            if _HASHER.verify(stored, wanted):
                return row_id
        except (VerificationError, InvalidHashError):
            continue
    return None


async def replace_codes(session: AsyncSession, *, org_id: UUID, user_id: UUID) -> list[str]:
    """Issue a fresh set and forget the old one, used codes included. The
    plaintext codes are returned ONCE, for the caller to show."""
    codes = new_codes()
    hashes = await asyncio.to_thread(_hash_all, [code.replace("-", "") for code in codes])
    await session.execute(delete(AuthRecoveryCode).where(AuthRecoveryCode.user_id == user_id))
    session.add_all(
        [AuthRecoveryCode(org_id=org_id, user_id=user_id, code_hash=h) for h in hashes]
    )
    await session.flush()
    return codes


async def use_code(
    session: AsyncSession, user_id: UUID, code: str, *, now: datetime | None = None
) -> bool:
    """Spend `code` if it is one of the person's unused codes. True once."""
    wanted = normalise(code)
    if wanted is None:
        return False
    rows = (
        await session.execute(
            select(AuthRecoveryCode.id, AuthRecoveryCode.code_hash).where(
                AuthRecoveryCode.user_id == user_id, AuthRecoveryCode.used_at.is_(None)
            )
        )
    ).all()
    match = await asyncio.to_thread(_find, [(row.id, row.code_hash) for row in rows], wanted)
    if match is None:
        return False
    won = (
        await session.execute(
            update(AuthRecoveryCode)
            .where(AuthRecoveryCode.id == match, AuthRecoveryCode.used_at.is_(None))
            .values(used_at=now or datetime.now(UTC))
            .returning(AuthRecoveryCode.id)
        )
    ).scalar_one_or_none()
    return won is not None


async def remaining(session: AsyncSession, user_id: UUID) -> int:
    """Unused codes left, for `GET /me/security`."""
    count = await session.scalar(
        select(func.count())
        .select_from(AuthRecoveryCode)
        .where(AuthRecoveryCode.user_id == user_id, AuthRecoveryCode.used_at.is_(None))
    )
    return int(count or 0)
