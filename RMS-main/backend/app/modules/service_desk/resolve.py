"""Reply & resolve: an optional reply and the move to resolved, as one act.

Everything here runs on the caller's session and neither commits nor opens a
transaction, so the route's `org_session` makes it all-or-nothing: a refused
transition after the reply was written takes the reply, its audit event and
its outbox event down with it. Each step is the ordinary service call, so the
lifecycle, audit and SLA rules are the same ones the separate routes apply.
"""

from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.core.ticket_words import status_quoted
from app.models import Ticket
from app.modules.service_desk.errors import TicketValidationError
from app.modules.service_desk.lifecycle import resolve_path
from app.modules.service_desk.threads import add_comment
from app.modules.service_desk.tickets import get_ticket
from app.modules.service_desk.transitions import change_status


async def reply_and_resolve(
    session: AsyncSession,
    org_id: UUID,
    ticket_id: UUID,
    *,
    actor_id: UUID,
    reply_body: str | None = None,
    reply_visibility: Literal["public", "internal"] = "public",
) -> Ticket:
    ticket = await get_ticket(session, org_id, ticket_id)
    steps = resolve_path(ticket.type, ticket.status)
    if steps is None:
        raise TicketValidationError(
            tr(
                "ticket {number} is {status} and cannot be resolved from there",
                number=ticket.number,
                status=status_quoted(ticket.status),
            )
        )
    # Reply first, so the requester's copy of the answer and the SLA's
    # response stamp come before the resolution, as they would by hand.
    if reply_body is not None:
        await add_comment(
            session,
            org_id,
            ticket.id,
            body=reply_body,
            author_id=actor_id,
            visibility=reply_visibility,
        )
    for step in steps:
        ticket = await change_status(
            session, org_id, ticket.id, to_status=step, actor_id=actor_id
        )
    return ticket
