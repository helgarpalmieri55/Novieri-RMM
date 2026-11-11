"""Who is signing in, before anybody is (design §6.2).

Without an organization pinned, RLS lets nothing be read, and that is right.
`its_auth_principal` (migration 0245, `SECURITY DEFINER`) takes the
organization's slug and an address and answers four facts -- the user, the
organization, the kind and the status -- and nothing else; with them the
caller pins the organization and reads the credentials under RLS as usual.

ONLY THE TECHNICIAN. The function may answer two rows for one address (a
technician and a contact of a client share it; staff first, Task 1). The
console signs in technicians only, so the query keeps the staff row and a
contact's address is "nobody" here -- the same answer, after the same work.

A RAW CONNECTION WITH NO GUC (Task 1's contract): the function needs no
organization pinned, and none is.

`org_of_slug` is the third such read (0246): the organization of a slug, for a
sign-in that has no address to look up, and for phase 3's anonymous OAuth
routes, which must not open the admin engine to find it.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.identity import STAFF_KIND


@dataclass(frozen=True)
class PreIdentity:
    user_id: UUID
    org_id: UUID
    status: str


async def find_staff(engine: AsyncEngine, *, org_slug: str, email: str) -> PreIdentity | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT user_id, org_id, status FROM its_auth_principal(:slug, :email) "
                    "WHERE kind = :staff"
                ),
                {"slug": org_slug, "email": email.strip().lower(), "staff": STAFF_KIND},
            )
        ).first()
    if row is None:
        return None
    return PreIdentity(user_id=row.user_id, org_id=row.org_id, status=row.status)


async def org_of_slug(engine: AsyncEngine, slug: str) -> UUID | None:
    """The organization `slug` names, or None (`its_auth_org`, migration 0246;
    plan decision D36): what a sign-in that brings no address -- a passkey
    without one, Microsoft -- pins before it reads anything under RLS."""
    async with engine.connect() as connection:
        value: UUID | None = (
            await connection.execute(text("SELECT its_auth_org(:slug)"), {"slug": slug})
        ).scalar_one()
    return value
