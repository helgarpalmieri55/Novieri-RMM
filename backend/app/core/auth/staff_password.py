"""A person's password: the rule of design §2.1 (§3 for a contact) and where the hash goes.

THE RULE IS TASK 2's (`passwords.check_new_password`): at least the person's
minimum (`minimum_for(kind)`: 12 for a technician, who always has a second
factor, design §2.1; 15 for a contact, whose second factor is optional,
design §3 -- `passwords.STAFF_MINIMUM` and `passwords.CONTACT_MINIMUM`), 128
at most, whole against the shipped blocklist and against the words of the
context -- the address and its parts, the organization's name, the person's
name and the platform's names (`passwords.context_words`; Task 2's decision
5). This module applies it to a technician or a contact (own authentication
phase 2) and says the reason (NIST SP 800-63B-4 §3.1.1.2; plan decision 11):
`PasswordRejected.message` is already in the request's language.

A PASSWORD IN A REQUEST BODY HAS NO PYDANTIC BOUND (plan decision 16): a field
failure repeats its INPUT in the 422, and the input is the password. A new
password is bounded by this rule (`too_short`, `too_long`); a password to
VERIFY is held to `SUBMITTED_MAX` by its flow before the pepper and argon2 see
it (`flows.login`, `account.change_password`).

THE BLOCKLIST LOADS ON FIRST USE (~0.5 s), so the check runs in a worker
thread, as Task 2 asks: an anonymous reset must not stall the event loop.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.passwords import (
    CONTACT_MINIMUM,
    STAFF_MINIMUM,
    PasswordRejected,
    check_new_password,
    context_words,
    hash_password,
)
from app.core.auth.refusals import AuthRefusal
from app.core.identity import CONTACT_KIND
from app.models import Organization
from app.models.auth import AuthPassword

#: The longest password a request may hand to the pepper and argon2 for
#: VERIFYING: longer than any the rule accepts (`passwords.MAX_LENGTH`, 128
#: code points after NFC, even typed decomposed), short enough that an
#: anonymous caller cannot send OpenBao a megabyte to HMAC.
SUBMITTED_MAX = 1024


def minimum_for(kind: str) -> int:
    """The shortest password a person of `kind` may choose: 12 for a
    technician, who always has a second factor (design §2.1); 15 for a
    contact, whose second factor is optional (design §3)."""
    return CONTACT_MINIMUM if kind == CONTACT_KIND else STAFF_MINIMUM


async def organization_name(session: AsyncSession, org_id: UUID) -> str:
    """The organization's name, for the password's context (Task 2's decision
    5). Read in the caller's session, whose RLS shows exactly this
    organization's row -- as `app/modules/remote/service.py:487` reads it."""
    name: str = (
        await session.execute(select(Organization.name).where(Organization.id == org_id))
    ).scalar_one()
    return name


async def require_acceptable(
    plain: str, *, email: str, display_name: str, org_name: str, minimum: int = STAFF_MINIMUM
) -> None:
    """The rule of design §2.1 (§3 for a contact: `minimum_for`), or a 422
    `password_rejected` whose `detail` says why. The context is the address,
    the organization's name and the person's name."""
    try:
        await asyncio.to_thread(
            check_new_password,
            plain,
            minimum=minimum,
            context=context_words(email, org_name, display_name),
        )
    except PasswordRejected as exc:
        raise AuthRefusal(
            status.HTTP_422_UNPROCESSABLE_CONTENT, code="password_rejected", detail=exc.message
        ) from exc


async def store(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    plain: str,
    now: datetime | None = None,
    by_mail: bool = False,
) -> None:
    """argon2id over the pepper's HMAC (Task 2); one row per person. `now`,
    when given, is the change's own aware moment (`account.change_password`
    uses the same one for the caller's session).

    `by_mail`: the password comes from a MAILED reset link (`flows.
    reset_password`), so `mail_reset_at` is set and an administrator's factor
    reset is refused until a second factor is verified (operator decision
    2026-11-05). Every other password -- changed while signed in, an
    invitation's, first boot's -- is the person's own and clears it."""
    digest, version = await hash_password(org_id, plain)
    now = now or datetime.now(UTC)
    mark = now if by_mail else None
    row = await session.get(AuthPassword, user_id)
    if row is None:
        session.add(
            AuthPassword(
                user_id=user_id,
                org_id=org_id,
                hash=digest,
                pepper_version=version,
                changed_at=now,
                mail_reset_at=mark,
            )
        )
    else:
        row.hash, row.pepper_version, row.changed_at = digest, version, now
        row.mail_reset_at = mark
    await session.flush()
