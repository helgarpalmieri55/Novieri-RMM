"""Race-safe ticket-number allocation (spec §2.1).

The number is user-visible and permanent, so a duplicate is not a transient
error -- it is two tickets that can never be told apart again. Allocation is
therefore a SINGLE atomic `UPDATE ... RETURNING`, never a SELECT followed by
an UPDATE: between those two statements a concurrent caller reads the same
value and both allocate the same number.

The counter row is created lazily with `on_conflict_do_nothing` so two
concurrent first-ever calls in one org cannot both insert (the same shape
`fx.get_settings` and `refdata.seed_default_categories` use).

Gaps are acceptable: a rolled-back transaction consumes its number and does
not return it. Reusing numbers would be far worse than skipping them.
"""

from uuid import UUID

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import TicketNumberCounter
from app.modules.service_desk.errors import TicketValidationError

_PREFIX = {"incident": "INC", "request": "REQ"}
_COLUMN = {
    "incident": TicketNumberCounter.next_incident,
    "request": TicketNumberCounter.next_request,
}


async def next_number(session: AsyncSession, org_id: UUID, ticket_type: str) -> str:
    if ticket_type not in _PREFIX:
        raise TicketValidationError(
            tr(
                "unknown ticket type: {ticket_type}",
                ticket_type=ticket_type,
            )
        )

    await session.execute(
        pg_insert(TicketNumberCounter)
        .values(org_id=org_id)
        .on_conflict_do_nothing(index_elements=["org_id"])
    )

    column = _COLUMN[ticket_type]
    allocated = (
        await session.execute(
            update(TicketNumberCounter)
            .where(TicketNumberCounter.org_id == org_id)
            .values({column: column + 1})
            .returning(column)
        )
    ).scalar_one()

    return f"{_PREFIX[ticket_type]}-{allocated - 1}"
