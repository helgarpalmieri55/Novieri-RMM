"""Resolving user ids to the name the directory holds for them.

WHAT THIS IS FOR. The audit log identifies an actor by id, which is correct —
it is what the chain hashes and what survives a rename — and unreadable, which
is finding B64 of the 2026-08-19 UX audit: "you cannot tell who did what". This
is the join that lets a response carry the name beside the id without the
console having to fetch the staff directory to read a page of events.

WHY NOT `GET /users`. That route exists (`app/modules/user_admin/api.py`) and
is gated on `users.read`, which `auditor` does not hold (migration 0003 gives
it `devices.read`, `jobs.read`, `audit.read`, `credentials.read`) — so the one
role that exists to read the audit log is the one a client-side lookup leaves
looking at UUIDs. It also returns every email and every role assignment in the
tenant, which is a great deal more than a page of names.

WHAT IT DELIBERATELY WILL NOT DO. It does not fall back to the email address
when `display_name` is empty. An address is the directory route's disclosure
and `audit.read` is not that gate; a null returns the caller to the id it was
already showing, which is a worse label and not a leak.

THE NAME IS CURRENT, NOT HISTORICAL. Nothing here is written anywhere and
nothing here is hashed: an immutable log stores the id, and this answers "who
is that id now". A response carrying it must keep the id alongside.
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def resolve_display_names(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_ids: Iterable[UUID],
) -> dict[UUID, str]:
    """`{user_id: display_name}` for the ids in this org that have a name.

    ONE STATEMENT FOR THE WHOLE PAGE, not one per row: the audit list serves
    up to 500 events and a per-event lookup would be an N+1 on a read path,
    the same reasoning `app/services/device_groups.py` records for its member
    count. Ids with no row, no name, or another tenant's row are simply absent
    from the mapping, so a caller writes `.get(id)` and gets `None`.

    THE `org_id` PREDICATE IS EXPLICIT AND IS NOT DECORATION. `audit_events.
    actor_id` is a plain UUID column with no cross-org constraint, so nothing
    in the schema stops a row naming a foreign id. RLS scopes `users` as well
    when this runs inside `org_session`, and invariant #5 asks for both — see
    `tests/test_audit_actor_names.py`, which pins this predicate against the
    BYPASSRLS engine where RLS is not the thing rejecting.
    """
    wanted = {uid for uid in user_ids}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.display_name).where(
                User.org_id == org_id, User.id.in_(wanted)
            )
        )
    ).all()
    return {user_id: name for user_id, name in rows if name}
