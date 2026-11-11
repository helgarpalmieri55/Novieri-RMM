"""The portal role a contact receives on accepting an invitation (design §3;
plan decision D40): `requester` -- which holds `portal.ticket.read` and
`portal.ticket.write` and nothing else (migration 0028) -- on the contact's
own client, and on no other. Granted once; a second acceptance (Microsoft
after a reset, say) finds it there."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity import CONTACT_KIND
from app.models import Role, User, UserRole

REQUESTER_ROLE = "requester"


async def grant_portal(session: AsyncSession, user: User) -> bool:
    """True when the grant was made now, False when it was there already."""
    if user.kind != CONTACT_KIND or user.client_id is None:
        raise ValueError(  # i18n-exempt: a caller's defect
            "only a contact of a client is granted the portal"
        )
    role_id = await session.scalar(
        select(Role.id).where(Role.name == REQUESTER_ROLE, Role.org_id.is_(None))
    )
    if role_id is None:
        raise LookupError(  # i18n-exempt: a deployment defect
            "the requester role is missing (migration 0028)"
        )
    held = await session.scalar(
        select(UserRole.id).where(
            UserRole.user_id == user.id,
            UserRole.role_id == role_id,
            UserRole.client_id == user.client_id,
        )
    )
    if held is not None:
        return False
    session.add(
        UserRole(org_id=user.org_id, user_id=user.id, role_id=role_id, client_id=user.client_id)
    )
    await session.flush()
    return True
