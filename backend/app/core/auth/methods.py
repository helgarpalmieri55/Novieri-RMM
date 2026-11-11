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

from uuid import UUID

from sqlalchemy import select
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
