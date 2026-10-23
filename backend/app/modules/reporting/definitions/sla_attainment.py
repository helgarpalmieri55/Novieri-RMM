"""`sla-attainment` -- service desk x SLA (spec §5.4).

`requires = {ticket.read, sla.read}`. **THIS IS THE ONE v1 REPORT THAT DOES NOT
REQUIRE `devices.read`**, which is what makes the four `requires` sets genuinely
distinct (§5): a suite that only ever exercised `devices.read` would miss a whole
class of conjunction bug.

**WHY IT EXISTS.** SLA spec §0.3 accepts a real perverse incentive -- a human
re-triage recomputes a deadline from ticket start, so a ticket that was going to
breach can be made not to -- on the explicit promise that attainment reporting
will distinguish "we were slow" from "we re-triaged at hour five".
`ticket_sla.policy_source` exists for that and **nothing in this codebase reads
it**. `breached_after_retriage` is that promise, finally rendered.

**THE MET/BREACHED PRECEDENCE IS `views.target_state`'s, REPRODUCED (§1.3).**
That function is the single server-side answer to "what happened to this
promise", and its order is a decision rather than an ordering convenience:
`void` outranks everything (a void promise is neither kept nor broken), and
**`breached` OUTRANKS `met`** because the two genuinely co-occur -- a reply that
lands after the deadline stamps `met_at` on a row whose `breached_at` is already
set. Counting that as met would let any breach be laundered by eventually
answering, which is the metric laundering the SLA spec refuses elsewhere. Every
`*_met` expression below therefore carries `*_breached_at IS NULL`, and
`tests/test_reporting_sla_attainment.py` seeds a row with BOTH stamps to pin it.

**A TICKET WITH NO `ticket_sla` ROW IS COUNTED IN `tickets` AND IN NOTHING ELSE
(§2, §3).** SLA spec §3's legitimate state is "no policy matched -> no
`ticket_sla` row", and a broken calendar degrades to the same thing. The join is
a LEFT OUTER JOIN so those tickets stay in the denominator -- an inner join
would quietly shrink `tickets` to the promised subset and every attainment
percentage would look better for an org whose SLA configuration is broken, which
is the exact inversion §2 forbids. They are in no met/breached/void/running
bucket, so `tickets` is deliberately NOT the sum of the others, and the field
descriptions say so.

**NO TICKET BODIES, NO SUBJECTS, NO REQUESTER IDENTITY (§5.4, §0.8).** The report
is counts by queue and priority. A manager who wants the tickets follows
`GET /api/v1/tickets` with the same filters, gated as it already is.
`ticket_comment` is not a source and no row field names a subject, a description
or a requester; a structural test asserts both rather than a reviewer noticing.

**`meta.reproducible` IS `False` (§0.3 cost 2, §5.4).** `ticket_sla` is mutable
by design: a re-triage recomputes a deadline from ticket start, so a ticket that
read "met" last month can read "breached" today for the SAME window. This is the
only v1 report where the flag is False, and having both cases in the suite is the
only thing that catches a `reproducible` hardcoded either way.

**EVERY STATEMENT CARRIES AN EXPLICIT `Ticket.org_id == org_id` (§6.4)**, in
addition to RLS, and so does every join condition -- but the obvious test for
that is worthless here, and the finding is recorded rather than left for the
next reader to trip over. Deleting `Ticket.org_id == org_id` leaves a two-org
tenancy test GREEN EVEN ON THE ADMIN ENGINE, because `TicketQueue.org_id ==
org_id` in the join already restricts the statement to this org's queues; the
two predicates are mutually redundant on well-formed data, so neither is
observable by seeding two well-formed orgs. What makes each individually
observable is a MALFORMED cross-tenant row, which the schema permits: `ticket.
queue_id` and `ticket_sla.ticket_id` are plain foreign keys with no cross-org
check, so a ticket in org B can point at org A's queue and an SLA row in org B
can attach to org A's ticket. `tests/test_reporting_sla_attainment.py::
test_a_cross_org_row_pointed_at_this_orgs_queue_is_still_excluded` seeds exactly
those two rows, and deleting either predicate turns it red.
"""

from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Integer, Numeric, Select, and_, cast, func, literal, or_, select

from app.core.i18n import N_, tr
from app.models import Ticket, TicketQueue, TicketSla
from app.modules.reporting.registry import ReportDefinition
from app.modules.reporting.service import DEFAULT_MAX_LIMIT

KEY = "sla-attainment"

# `ticket_sla.policy_source` is `initial` | `retriage` | `escalation`
# (`ck_ticket_sla_policy_source_valid`). RESTATED rather than imported (§1.3),
# with a drift alarm in `tests/test_reporting_sla_attainment.py` -- and it is
# worth more here than anywhere else in this module, because a typo would make
# `breached_after_retriage` read a permanent zero, which looks exactly like
# "nobody is gaming the clock".
_RETRIAGE = "retriage"

# The window ceiling (§5.4). A 422, never a silent clamp: a report that quietly
# narrowed a three-year request to one year would answer a different question
# than the one it was asked, under the label of the one it was asked.
MAX_WINDOW_DAYS = 366

PRIORITIES = ("low", "normal", "high", "urgent")


class SlaAttainmentParams(BaseModel):
    """Query parameters over a CLOSED window -- `from` and `to` are required.

    There is no default window on purpose: every other report in this module is
    a statement about NOW, and this one is a statement about a PERIOD. A default
    would make the period invisible in the request that produced a number
    somebody pastes into a board pack.
    """

    model_config = ConfigDict(extra="forbid")

    # `from` is a Python keyword, so the field is `window_from` with `from` as
    # its wire alias. `service.execute_report` reads `window_from`/`window_to`
    # off the params object to populate `meta.window_from`/`window_to`, so these
    # names are load-bearing beyond this file.
    window_from: datetime = Field(
        alias="from",
        description=N_("Inclusive start of the window, matched against ticket creation."),
    )
    window_to: datetime = Field(
        alias="to",
        description=N_("Exclusive end of the window, matched against ticket creation."),
    )
    queue_id: UUID | None = Field(default=None, description=N_("Restrict to one queue."))
    priority: Literal["low", "normal", "high", "urgent"] | None = Field(
        default=None, description=N_("Restrict to one priority.")
    )
    limit: int = Field(
        default=50, ge=1, le=DEFAULT_MAX_LIMIT, description=N_("Maximum rows to return.")
    )
    offset: int = Field(default=0, ge=0, description=N_("Rows to skip before the first returned."))

    @model_validator(mode="after")
    def _window_is_sane(self) -> "SlaAttainmentParams":
        if self.window_to < self.window_from:
            raise ValueError(tr("'to' must not be before 'from'"))
        if self.window_to - self.window_from > timedelta(days=MAX_WINDOW_DAYS):
            raise ValueError(
                tr("the window may not exceed {max_days} days", max_days=MAX_WINDOW_DAYS)
            )
        return self


class SlaAttainmentRow(BaseModel):
    """One `(queue, priority)` pair. NOT one ticket -- see the module docstring
    for what is deliberately absent."""

    queue_id: UUID = Field(description="The queue this row aggregates.")
    queue_name: str = Field(description="That queue's display name.")
    priority: str = Field(description="low | normal | high | urgent.")
    tickets: int = Field(
        description=(
            "Every ticket created in the window in this queue at this priority, "
            "INCLUDING tickets with no SLA row at all (no policy matched, or the "
            "calendar was misconfigured). Those appear in no other count here, so this "
            "is deliberately not the sum of the columns beside it."
        )
    )
    response_met: int = Field(
        description=(
            "Tickets whose response clock stopped on time. A clock that was breached "
            "and later answered counts as breached, never as met."
        )
    )
    response_breached: int = Field(
        description="Tickets whose response deadline passed before the clock stopped."
    )
    resolution_met: int = Field(
        description="Tickets whose resolution clock stopped on time, breach outranking met."
    )
    resolution_breached: int = Field(
        description="Tickets whose resolution deadline passed before the clock stopped."
    )
    void: int = Field(
        description=(
            "Tickets that reached a terminal-not-resolved status (cancelled, rejected). "
            "The promise is neither kept nor broken, so they are excluded from every "
            "other count and from both percentages -- cancelling a ticket must not be a "
            "way to raise attainment."
        )
    )
    still_running: int = Field(
        description=(
            "Non-void tickets whose RESOLUTION clock has neither been met nor breached: "
            "the promise is live or paused. Not yet an outcome, so not in either "
            "percentage."
        )
    )
    response_attainment_pct: float | None = Field(
        description=(
            "response_met / (response_met + response_breached), as a percentage to two "
            "decimals. NULL -- never 0 -- when no response clock has stopped yet, "
            "because no outcome is not a bad outcome."
        )
    )
    resolution_attainment_pct: float | None = Field(
        description="The same, for the resolution clock. NULL when nothing has stopped."
    )
    breached_after_retriage: int = Field(
        description=(
            "Non-void tickets that breached AND whose current policy was applied by a "
            "human re-triage rather than at creation. A re-triage recomputes the "
            "deadline from ticket start, so this separates 'we were slow' from 'we "
            "moved the goalposts and were still slow'."
        )
    )


def _pct(met: Any, breached: Any) -> Any:
    """A percentage that is NULL rather than 0 when nothing has been decided.

    `nullif(..., 0)` is the whole point: a queue with three tickets all still
    running has not achieved 0% attainment, and a dashboard that renders 0% for
    it is worse than one that renders nothing.
    """
    return func.round(
        cast(met, Numeric) * literal(100) / func.nullif(cast(met + breached, Numeric), 0),
        2,
    )


def _base(*, org_id: UUID, params: SlaAttainmentParams) -> Select[Any]:
    # `views.target_state`'s precedence, as SQL. `has_sla` is what keeps a
    # ticket with no promise out of every bucket; `live` is "has a promise that
    # was not voided".
    has_sla = TicketSla.id.is_not(None)
    live = and_(has_sla, TicketSla.voided_at.is_(None))
    is_void = and_(has_sla, TicketSla.voided_at.is_not(None))

    response_breached = and_(live, TicketSla.response_breached_at.is_not(None))
    # BREACHED OUTRANKS MET -- see the module docstring. Without the
    # `breached_at IS NULL` term a late reply counts as met and every breach can
    # be laundered by eventually answering.
    response_met = and_(
        live,
        TicketSla.response_breached_at.is_(None),
        TicketSla.response_met_at.is_not(None),
    )
    resolution_breached = and_(live, TicketSla.resolution_breached_at.is_not(None))
    resolution_met = and_(
        live,
        TicketSla.resolution_breached_at.is_(None),
        TicketSla.resolution_met_at.is_not(None),
    )
    still_running = and_(
        live,
        TicketSla.resolution_breached_at.is_(None),
        TicketSla.resolution_met_at.is_(None),
    )
    breached_after_retriage = and_(
        live,
        TicketSla.policy_source == literal(_RETRIAGE),
        or_(
            TicketSla.response_breached_at.is_not(None),
            TicketSla.resolution_breached_at.is_not(None),
        ),
    )

    response_met_count = func.count().filter(response_met)
    response_breached_count = func.count().filter(response_breached)
    resolution_met_count = func.count().filter(resolution_met)
    resolution_breached_count = func.count().filter(resolution_breached)

    stmt = (
        select(
            Ticket.queue_id.label("queue_id"),
            TicketQueue.name.label("queue_name"),
            Ticket.priority.label("priority"),
            func.count().label("tickets"),
            response_met_count.label("response_met"),
            response_breached_count.label("response_breached"),
            resolution_met_count.label("resolution_met"),
            resolution_breached_count.label("resolution_breached"),
            func.count().filter(is_void).label("void"),
            func.count().filter(still_running).label("still_running"),
            _pct(response_met_count, response_breached_count).label(
                "response_attainment_pct"
            ),
            _pct(resolution_met_count, resolution_breached_count).label(
                "resolution_attainment_pct"
            ),
            func.count().filter(breached_after_retriage).label("breached_after_retriage"),
        )
        .select_from(Ticket)
        .join(
            TicketQueue,
            and_(TicketQueue.id == Ticket.queue_id, TicketQueue.org_id == org_id),
        )
        # OUTER. A ticket with no promise stays in the denominator; see the
        # module docstring.
        .outerjoin(
            TicketSla,
            and_(TicketSla.ticket_id == Ticket.id, TicketSla.org_id == org_id),
        )
        # §6.4: explicit, IN ADDITION to RLS.
        .where(
            Ticket.org_id == org_id,
            Ticket.created_at >= params.window_from,
            Ticket.created_at < params.window_to,
        )
    )
    if params.queue_id is not None:
        stmt = stmt.where(Ticket.queue_id == params.queue_id)
    if params.priority is not None:
        stmt = stmt.where(Ticket.priority == params.priority)
    return stmt.group_by(Ticket.queue_id, TicketQueue.name, Ticket.priority).order_by(
        # Stable and TOTAL (§6.3). `ticket_queue` is UNIQUE on `(org_id, key)`
        # and NOT on `name`, so two queues can share a display name; the
        # `queue_id` tiebreaker is what makes offset paging sound. `priority`
        # sorts alphabetically rather than by severity -- an ordering, not a
        # ranking, and the row carries the value so a reader is never guessing.
        TicketQueue.name,
        Ticket.priority,
        Ticket.queue_id,
    )


def build(*, org_id: UUID, params: SlaAttainmentParams) -> Select[Any]:
    return _base(org_id=org_id, params=params)


def build_totals(*, org_id: UUID, params: SlaAttainmentParams) -> Select[Any]:
    """Sums over the WHOLE match, not the page (§6.3), built from the same
    `_base` statement the rows come from.

    NO PERCENTAGE IS TOTALLED. `ReportPage.totals` is `dict[str, int]` by
    design, and an average of per-queue percentages is not the fleet percentage
    anyway -- a client that wants the overall figure computes it from the
    totalled counts, which are the numbers the arithmetic is actually defined
    on.
    """
    matched = _base(org_id=org_id, params=params).subquery()
    counted = (
        "tickets",
        "response_met",
        "response_breached",
        "resolution_met",
        "resolution_breached",
        "void",
        "still_running",
        "breached_after_retriage",
    )
    return select(
        func.count().label("queue_priority_pairs"),
        *[
            cast(func.coalesce(func.sum(matched.c[name]), 0), Integer).label(name)
            for name in counted
        ],
    ).select_from(matched)


DEFINITION = ReportDefinition(
    key=KEY,
    summary=N_("SLA attainment by queue and priority over a closed window."),
    description=N_(
        "One row per (queue, priority) pair for tickets created inside the window: how "
        "many there were, how many met or breached each clock, how many are void or "
        "still running, the two attainment percentages, and how many breached under a "
        "policy applied by a human re-triage rather than at creation. A breach outranks "
        "a met on the same clock, so answering late cannot launder it, and a void ticket "
        "is excluded from both percentages so cancelling cannot inflate them. Tickets "
        "with no SLA row at all -- no policy matched, or the calendar is misconfigured "
        "-- stay in the ticket count and appear in no other column, so a broken SLA "
        "configuration cannot make attainment look better. NOT reproducible: ticket_sla "
        "is mutable by design, so the same window can read differently tomorrow."
    ),
    params=SlaAttainmentParams,
    row_model=SlaAttainmentRow,
    sources=frozenset({"ticket", "ticket_queue", "ticket_sla"}),
    build=build,
    build_totals=build_totals,
    max_limit=DEFAULT_MAX_LIMIT,
    export_columns=(
        "queue_id",
        "queue_name",
        "priority",
        "tickets",
        "response_met",
        "response_breached",
        "resolution_met",
        "resolution_breached",
        "void",
        "still_running",
        "response_attainment_pct",
        "resolution_attainment_pct",
        "breached_after_retriage",
    ),
    # The only operator-supplied free text here. Everything else is a UUID, an
    # int, a percentage or a fixed-set enum.
    export_free_text=frozenset({"queue_name"}),
    export_row_cap=50_000,
    # FALSE, and it is the only v1 report where it is (§0.3 cost 2, §5.4).
    reproducible=False,
)
