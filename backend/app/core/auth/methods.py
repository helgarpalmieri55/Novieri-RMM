"""The ways into an account (own authentication phase 2, plan decisions D25,
D27, D40; operator decisions C5 and C6).

A WAY IN is what can start a sign-in on its own: a password, a passkey used
without an address (a contact's, D28), or a linked Microsoft account (Task 9).
For a technician a passkey is a second factor, but a technician holds one only
after having signed in, so "has this person a way in" -- "have they accepted
their invitation" -- has the same answer for both kinds. For a contact a
passkey is ANOTHER way in and never a second factor (C5).

Read in the caller's session (one organization, every client: a technician's
rows are global, a contact's are their client's, migration 0231).
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import AuthExternalIdentity, AuthPasskey, AuthPassword


async def has_password(session: AsyncSession, user_id: UUID) -> bool:
    found = await session.scalar(
        select(AuthPassword.user_id).where(AuthPassword.user_id == user_id)
    )
    return found is not None


async def has_passkey(session: AsyncSession, user_id: UUID) -> bool:
    found = await session.scalar(
        select(AuthPasskey.user_id).where(AuthPasskey.user_id == user_id).limit(1)
    )
    return found is not None


async def has_microsoft(session: AsyncSession, user_id: UUID) -> bool:
    found = await session.scalar(
        select(AuthExternalIdentity.user_id).where(AuthExternalIdentity.user_id == user_id)
    )
    return found is not None


async def has_sign_in_method(session: AsyncSession, user_id: UUID) -> bool:
    """A password, a passkey or a Microsoft link."""
    return (
        await has_password(session, user_id)
        or await has_passkey(session, user_id)
        or await has_microsoft(session, user_id)
    )


async def microsoft_only(session: AsyncSession, user_id: UUID) -> bool:
    """Signs in with Microsoft and nothing else: no password, no passkey
    (operator decision C6: such a contact is sent no reset link)."""
    return (
        await has_microsoft(session, user_id)
        and not await has_password(session, user_id)
        and not await has_passkey(session, user_id)
    )


async def contact_may_reset(session: AsyncSession, user_id: UUID) -> bool:
    """A contact's password may be reset by mail -- the link sent, and later
    used (plan decision 4, operator decision C6): any way in, EXCEPT
    Microsoft alone. `has_sign_in_method and not microsoft_only` is exactly
    "a password or a passkey", so that is what is read, in one query."""
    found = await session.scalar(
        select(
            or_(
                select(AuthPassword.user_id).where(AuthPassword.user_id == user_id).exists(),
                select(AuthPasskey.user_id).where(AuthPasskey.user_id == user_id).exists(),
            )
        )
    )
    return bool(found)


async def with_sign_in_method(session: AsyncSession, user_ids: Sequence[UUID]) -> set[UUID]:
    """`has_sign_in_method` for many people in ONE query (a client's contacts
    list): the same three tables -- a password, a passkey, a Microsoft link.
    `test_contact_access_api.py` holds the two to the same answer."""
    ids = list(user_ids)
    if not ids:
        return set()
    rows = await session.execute(
        union(
            select(AuthPassword.user_id).where(AuthPassword.user_id.in_(ids)),
            select(AuthPasskey.user_id).where(AuthPasskey.user_id.in_(ids)),
            select(AuthExternalIdentity.user_id).where(AuthExternalIdentity.user_id.in_(ids)),
        )
    )
    return set(rows.scalars())
