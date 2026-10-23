"""Which deadline a ticket owes next, in SQL (for sorting) and in Python (for
the row's summary).

Both forms live here so the queue's order and the "Due" it prints cannot
disagree. The rule: a clock that has not been met is outstanding, including
one already breached (it still needs an answer). A paused or voided row owes
nothing right now, so it has no next deadline.

This module imports nothing from the ticket core, so `service_desk` can use it
without an import cycle through `views`.
"""

from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, and_, case, func

from app.modules.sla.models import TicketSla

TargetState = Literal["none", "void", "breached", "met", "paused", "running"]
Target = Literal["response", "resolution"]


def next_due(row: TicketSla) -> tuple[datetime | None, Target | None]:
    """The earliest outstanding deadline and which clock it belongs to."""
    if row.voided_at is not None or row.paused_at is not None:
        return None, None
    candidates: list[tuple[datetime, Target]] = []
    if row.response_met_at is None:
        candidates.append((row.response_due_at, "response"))
    if row.resolution_met_at is None:
        candidates.append((row.resolution_due_at, "resolution"))
    if not candidates:
        return None, None
    # `min` keeps the first of equal instants, so a tie reads as "response".
    due, target = min(candidates, key=lambda pair: pair[0])
    return due, target


def next_due_clause() -> ColumnElement[datetime]:
    """`next_due` as a column expression over an outer-joined `ticket_sla`.

    NULL (sorted last) for no row, a paused or voided row, or a row with
    every clock met. Postgres's LEAST ignores NULL arguments.
    """
    return case(
        (
            and_(TicketSla.voided_at.is_(None), TicketSla.paused_at.is_(None)),
            func.least(
                case((TicketSla.response_met_at.is_(None), TicketSla.response_due_at)),
                case((TicketSla.resolution_met_at.is_(None), TicketSla.resolution_due_at)),
            ),
        ),
    )
