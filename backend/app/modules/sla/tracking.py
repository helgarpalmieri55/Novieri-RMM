"""Materialising a ticket's promise onto `ticket_sla`, stopping its clock while
we are blocked, and recording when the promise was kept
(docs/modules/sla/spec.md §0.3, §0.4, §0.6, §2.5, §3, §5; Tasks 5, 6 and 7).

Seven entry points, one shared rule for the four that write a deadline:
**every deadline this module writes is computed from `ticket.created_at`,
never from "now"**. `start_tracking` runs at ticket creation;
`recompute_for_retriage` runs on a HUMAN re-triage (a priority or queue change
with a human actor); `pause`/`resume` run when the ticket enters or leaves a
`PAUSING_STATUSES` status. The two met-detection hooks (`note_public_comment`,
`note_resolved`) write no deadline at all -- they only record the instant a
clock stopped -- and `note_cancelled` writes only `voided_at`, the instant the
promise stopped meaning anything because the ticket reached `cancelled` or
`rejected`.

WHERE THE CALLERS ARE. Every one of these is wired into the ticket core and
nowhere else, which is the whole point: a hook with no production caller is an
engine that is inert on every real ticket while its own tests stay green.
`tickets.create_ticket` -> `start_tracking`; `tickets.update_ticket` (human
priority change) and `transitions.assign_ticket` (human queue change) ->
`recompute_for_retriage`; `transitions.change_status` -> `pause`/`resume` on
the `PAUSING_STATUSES` edges, `note_resolved` on the resolved-like branch and
`note_cancelled` on the `FINAL_STATUSES` branch; `threads.add_comment` ->
`note_public_comment` on its public branch. The two recompute call sites are
guarded on `actor_type == "user"`, which is §0.4's loop guard in its structural
form -- see `recompute_for_retriage` below.

Every one of those call sites wraps the call in
`sla_misconfiguration_is_not_fatal`: an admin's broken calendar may cost a
ticket its SLA row, but it may never cost a customer the ability to file a
ticket. That helper's docstring holds the reasoning.

PAUSE, IN ONE PARAGRAPH (§5). Entering `pending` (incident) or
`pending_approval` (request) stamps `paused_at`; leaving it measures the pause
in WORKING minutes, adds them to `paused_minutes_total`, re-derives every
deadline still running as `target + paused_minutes_total` working minutes from
`ticket.created_at`, and clears `paused_at`. `paused_minutes_total` (migration
0037) is what makes that survive a later recompute: before it, re-deriving from
`created_at` overwrote the only surviving record of a completed pause -- the
offset already inside `due_at` -- and a re-triage that changed nothing silently
destroyed every minute of credit the ticket had been given back.

WHY RECOMPUTING FROM TICKET START LOOKS LIKE A BUG AND IS NOT (spec §0.3). A
ticket filed `normal` (8h target) at 09:00 and re-triaged to `urgent` (1h) at
14:00 comes out with a deadline five hours in the past -- already breached the
instant the re-triage lands. That is the intended outcome, not an off-by-
something: the work was urgent all along and nobody treated it that way, so
the breach is information rather than an artifact. The spec records the cost
it accepts (a perverse incentive not to escalate) and why both alternatives
fail worse and less visibly:

* Freezing the policy at creation lets a misfiled priority permanently cap the
  promise -- file everything `low` and never breach.
* Recomputing from the moment of change is the same hole with extra steps --
  miss the target, bump the priority, get a fresh clock, repeat.

The mitigation is attribution, not a softer clock, and `policy_source` is the
attribution: `initial` (materialised at creation) vs `retriage` (a human
changed the triage) makes "breached because we were slow" and "breached
retroactively because we re-triaged at hour five" distinguishable in
reporting, where the second reads as a triage-quality problem rather than a
response-time failure.

`policy_source = 'escalation'` is Task 9's alone and is NEVER written here.
That value is §0.4's loop guard: breach escalation raises the priority, and if
it also recomputed, the ticket would breach again instantly, escalate again,
and fire a burst of notifications in one second. The guard is structural --
the escalation path simply does not call `recompute_for_retriage` -- so this
module writes only the two human-originated sources and the CHECK constraint
(`POLICY_SOURCES`) keeps the third from being written here by accident.

`start_tracking` returns `None` and writes NO ROW when no policy matches (spec
§3). Not a zero, not a default, not a silent infinity: an org that has
configured no policy must visibly have no SLA, because a fabricated target is
worse than an absent one.

WHY NOTHING HERE IS AUDITED. Spec §8's audited list is explicit -- calendar
CRUD, policy CRUD, settings changes, every escalation, every auto-close. SLA
materialisation is none of those: it follows automatically from a ticket
create or a ticket update that the ticket core has already audited, and
auditing it here would add one hash-chained row per ticket for an event that
records no decision a human made. Escalation (Task 9) IS audited, which is
where the privileged, system-originated write actually happens.

ORG SCOPING. Every query this module issues carries an explicit `org_id`
(invariant #5), because every service here is also callable on the BYPASSRLS
admin engine, where RLS masks nothing. Three helpers hold every predicate in
this file -- `_load_tracking` (the module's only `ticket_sla` read),
`_load_ticket_in_org` (the resume and public-comment paths) and
`_load_policy_in_org` (the resume path) -- and `resolve_policy` (Task 4) and
`load_calendar_spec` (Task 3) carry their own. None of them is decorative:
`ticket_sla.ticket_id` and `ticket_sla.policy_id` are plain FKs that do not
check org membership, so a row one org owns can legally point at another org's
ticket or policy, and a dropped predicate reads that other tenant's data
straight into this tenant's deadlines. `_require_same_org` is the cheaper,
stronger form of the same guard for the two entry points that are handed a
`Ticket` object rather than an id: an in-memory comparison cannot be satisfied
by a row this org can see under a different id.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.modules.service_desk.models import Ticket, TicketStatusHistory
from app.modules.sla.calendar import (
    MAX_LOOKAHEAD_DAYS,
    CalendarSpec,
    add_working_minutes,
    working_minutes_between,
)
from app.modules.sla.calendars import load_calendar_spec
from app.modules.sla.errors import SlaValidationError, UnsatisfiableCalendar
from app.modules.sla.models import SlaPolicy, TicketSla
from app.modules.sla.policies import resolve_policy

__all__ = [
    "PAUSING_STATUSES",
    "POLICY_SOURCES",
    "note_cancelled",
    "note_public_comment",
    "note_resolved",
    "pause",
    "recompute_for_move",
    "recompute_for_retriage",
    "resume",
    "sla_misconfiguration_is_not_fatal",
    "start_tracking",
]

logger = logging.getLogger("its.sla.tracking")

# Must stay in agreement with `ck_ticket_sla_policy_source_valid` (migration
# 0033, `models.py::TicketSla`) -- `test_sla_tracking.py` reads the live
# constraint definition and compares, so a value added on one side and not the
# other fails rather than drifting. Only `initial` and `retriage` are ever
# written by this module; `escalation` belongs to Task 9 (§0.4).
POLICY_SOURCES = ("initial", "retriage", "escalation")

# The subset of POLICY_SOURCES this module is ever allowed to write (§0.4);
# `_materialise`'s `source` parameter is typed to it AND checked at runtime
# below -- the type hint alone is erased at runtime and would not stop a
# future edit from passing a caller-supplied or otherwise non-literal string
# through to the INSERT/UPDATE.
_WRITABLE_POLICY_SOURCES: tuple[Literal["initial", "retriage"], ...] = ("initial", "retriage")

# The statuses that stop the clock (spec §5). `pending` is the incident
# branch's and `pending_approval` the request branch's -- both mean *blocked on
# someone who is not us*, which is the reason the ticket core made `pending` a
# first-class state rather than a flag (its §3.1). Named here, once, so the
# ticket-core wiring (Task 8) tests membership against the SLA module's own
# definition instead of repeating two string literals somewhere else.
#
# Deliberately NOT derived from `lifecycle.INCIDENT_STATUSES` /
# `REQUEST_STATUSES`: those are "what statuses exist", this is "which of them
# mean we are not on the clock", and a status added to the ticket core must not
# silently join this set.
PAUSING_STATUSES: frozenset[str] = frozenset({"pending", "pending_approval"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _unsatisfiable_calendar_fields(exc: UnsatisfiableCalendar) -> dict[str, object]:
    """The `extra=` payload for `sla_misconfiguration_is_not_fatal`'s log
    line, below -- OMITTING any of the four attributes that is `None`.

    I-1 (fix wave 9): `UnsatisfiableCalendar`'s four keyword-only attributes
    default to `None`, not `""`/`0`/an epoch date, precisely so a
    RECONSTRUCTED exception (Celery's JSON-serializer path calls
    `cls(message)` with no keywords at all -- see that class's docstring) is
    distinguishable from a PROPERLY RAISED one on the one place an operator
    actually reads these values: this log line. Writing `None` into `extra=`
    would not do that -- `JsonFormatter` renders a `None` value as a real
    JSON `null`, which is exactly as present-looking as `0` or `""` was
    before this fix, just spelled differently. Only OMITTING the key
    produces the property this fix exists for: a reconstructed exception's
    log line is missing `requested_minutes` entirely, not carrying it as
    `null`, `0`, or any other value that could be mistaken for a
    measurement.

    F-8 (fix wave 10): this omission is DEFENCE IN DEPTH, not a live path,
    at this call site today. `sla_misconfiguration_is_not_fatal`'s `async
    with` only ever sees an `UnsatisfiableCalendar` that a real raise site
    (`app/modules/sla/calendar.py`) constructs with all four keyword-only
    arguments named -- a RECONSTRUCTED instance (Celery's JSON-serializer
    path, `cls(*exc_message)`) never arrives through this context manager;
    only `tests/test_logging_kernel.py`'s
    `test_sla_misconfiguration_log_line_omits_absent_fields_on_a_
    reconstructed_exception` raises one by hand to exercise this function
    directly. The omission stays correct and cheap regardless, guarding a
    future raise site this module has not been written yet.
    """
    fields: dict[str, object] = {}
    if exc.timezone is not None:
        fields["calendar_timezone"] = exc.timezone
    if exc.requested_minutes is not None:
        fields["requested_minutes"] = exc.requested_minutes
    if exc.start is not None:
        fields["calendar_start"] = exc.start
    if exc.outstanding_minutes is not None:
        fields["outstanding_minutes"] = exc.outstanding_minutes
    return fields


@asynccontextmanager
async def sla_misconfiguration_is_not_fatal(
    operation: str, ticket_id: UUID
) -> AsyncIterator[None]:
    """Wrap a ticket-core call into this module so a MISCONFIGURED CALENDAR
    cannot fail the ticket operation that triggered it (Task 8's wiring).

    THE DECISION, stated plainly because it is a trade and not a free win: an
    SLA failure may NOT fail a ticket write. `start_tracking` legitimately
    raises `UnsatisfiableCalendar` when the resolved policy's calendar has no
    working windows at all, or cannot deliver the target inside
    `MAX_LOOKAHEAD_DAYS` (spec §4) -- an admin saving a policy against an empty
    calendar is enough. Left to propagate out of `create_ticket`, that admin
    error becomes: **no one in the organisation can file a ticket.** Not the
    portal, not email intake, not the technician console. A customer locked out
    of raising an incident because somebody mis-saved a calendar is a strictly
    worse outcome than the alternative, and the alternative is not even a new
    state: spec §3 already declares "no policy resolved -> no `ticket_sla` row"
    a legitimate, visible condition an org must be able to see, and this
    degrades to exactly it. It is also recoverable without operator surgery --
    `recompute_for_retriage` materialises the row on the next human re-triage,
    once the calendar is fixed.

    The same reasoning covers the other three hooks, and each is a real case:
    `recompute_for_retriage` must not make a technician unable to re-prioritise
    a ticket, and `resume` must not make them unable to move a ticket off
    `pending` -- being trapped in a pausing status because the clock cannot be
    computed is the worst possible place to be stuck.

    WHAT IS **NOT** SWALLOWED, and why the catch is narrow. Only
    `UnsatisfiableCalendar` -- which is what an admin's bad calendar raises
    (spec §4's bounded iteration finding no window at all) -- is tolerated.

    `ValueError` is deliberately NOT in the tuple (review sla-breach-wiring,
    F3), even though `calendar.py` raises it in three places
    (`_to_utc`'s naive-datetime guard, `working_minutes_between`'s
    span-exceeds-`MAX_LOOKAHEAD_DAYS` guard, `add_working_minutes`'
    negative-minutes guard). All three are PROGRAMMING-error guards, not
    admin misconfiguration: a stored calendar's timezone is validated at
    write time (`calendars._validate_timezone`) so it can never reach here
    malformed, `ticket.created_at` is a DB-supplied `TIMESTAMPTZ` so it is
    never naive, and `sla_policy`'s target-minute columns carry `CHECK ...
    > 0` so a negative target cannot be stored. Reaching one of these three in
    production means this module's own arithmetic went wrong -- computed a
    corrupt timestamp, or handed a negative credit into
    `add_working_minutes`' `minutes` argument -- and swallowing THAT
    degrades a genuine bug into "no SLA row and one ERROR log line" for
    every ticket in the org, silently, which is the exact failure class this
    whole task exists to fix, one exception type over.

    THE ONE PLACE THAT REASONING WAS FALSE, and how it was repaired rather
    than widened (review F2). `resume` measured an open pause with
    `working_minutes_between(paused_at, now)`, whose span is not a computed
    instant at all -- it is real elapsed wall time in a pausing status, and a
    ticket blocked on a vendor for thirteen months exceeds the horizon without
    anything being corrupt. So this wrapper let a `ValueError` out of the one
    hook whose failure it explicitly calls "the worst possible place to be
    stuck", trapping the ticket in `pending` past resume, resolve, close and
    cancel alike. The fix belongs at that call site and is there
    (`_open_pause_minutes`), NOT here: adding `ValueError` to this tuple would
    have unstuck the ticket by blinding the module to every genuine arithmetic
    fault at the same time. The paragraph above is true again because the one
    counterexample was removed, not because it was reclassified.

    A broader `except SlaValidationError` would also swallow
    `_require_same_org`/`_load_ticket_in_org`'s cross-org refusals, which are
    tenancy failures that must stay loud (invariant #5), and a bare
    `except Exception` would
    additionally swallow `IntegrityError` and every other DBAPI failure --
    which would be actively unsafe, because a failed statement aborts the
    transaction and everything the caller does afterwards would fail anyway,
    hiding a database fault behind a log line.

    `UnsatisfiableCalendar` is raised in pure Python (inside
    `add_working_minutes`), BEFORE any statement this module would have
    issued for it, so the session is still usable and the caller's
    transaction is intact. That is what makes swallowing it safe here and
    would not be true of a DBAPI error.

    LOGGED AT ERROR, not audited. §8's audited list is calendar/policy/settings
    changes, escalations and auto-closes; a materialisation that could not
    happen is an operational fault, not a decision a human made (the module
    docstring's reasoning for not auditing materialisation at all). It is
    logged loudly with the ticket id, because a silently absent SLA row is the
    exact failure mode this whole task exists to fix and it must not be
    reintroduced one layer down.
    """
    try:
        yield
    except UnsatisfiableCalendar as exc:
        # I-5: the calendar, the target and how far short it fell -- read off
        # the exception's own ATTRIBUTES (see `UnsatisfiableCalendar`), never
        # its formatted text, which §2 forbids on a log line regardless of
        # how credential-free it happens to be. `exc_type` alone
        # (`UnsatisfiableCalendar`) is a compile-time constant here -- this
        # is the ONLY raise site and the ONLY type this wrapper catches -- so
        # without these fields an operator sees a static line and nothing
        # that distinguishes an empty calendar from one that is merely too
        # short for this target.
        #
        # I-1 (fix wave 9): built via `_unsatisfiable_calendar_fields`, not a
        # dict literal reading the four attributes directly -- a
        # RECONSTRUCTED exception (Celery's JSON-serializer path) now
        # defaults every one of them to `None`, and this call OMITS a
        # `None` field rather than logging it, so a reconstructed exception's
        # line is visibly missing `requested_minutes`/etc. instead of
        # carrying a value indistinguishable from a real measurement. See
        # that function's docstring for why omission, not a JSON `null`, is
        # what the property actually requires.
        logger.error(
            "sla.%s failed for ticket %s and was tolerated; the ticket operation "
            "succeeded and this ticket has no updated SLA state",
            operation,
            ticket_id,
            extra=_unsatisfiable_calendar_fields(exc),
            exc_info=exc,
        )


def _require_same_org(org_id: UUID, ticket: Ticket) -> None:
    """The caller hands us a `Ticket` object rather than an id, so the org
    check is an in-memory comparison rather than a query -- strictly stronger
    than a re-SELECT (it cannot be satisfied by a row this org can see under
    a different id) and free. `ticket_sla.ticket_id`'s FK checks that the
    ticket EXISTS, never that it belongs to `org_id`."""
    if ticket.org_id != org_id:
        raise SlaValidationError(
            tr(
                "ticket {id} does not belong to org {org_id}; refusing to track it",
                id=ticket.id,
                org_id=org_id,
            )
        )


async def _load_tracking(
    session: AsyncSession, org_id: UUID, ticket_id: UUID, *, for_update: bool = False
) -> TicketSla | None:
    """The module's ONLY `ticket_sla` read (invariant #5: the `org_id`
    predicate is explicit, not left to RLS -- every service in this module is
    also callable on the BYPASSRLS admin engine, where RLS masks nothing).

    `for_update=True` takes `SELECT ... FOR UPDATE` on any row found, so a
    concurrent writer of the SAME row blocks until this transaction commits or
    rolls back rather than racing it (F7, Task 5 review). Every caller that
    only reads (`start_tracking`'s existing-row check, `_materialise`'s
    post-INSERT re-read) leaves this False -- locking a row nobody is about to
    mutate would only serialise unrelated readers for no benefit.
    `recompute_for_retriage`, `pause` and `resume` are this module's
    read-modify-writes and all three pass True. They contend on the same row
    and the same columns -- `resume` writes the very `response_due_at` /
    `resolution_due_at` that a concurrent re-triage recomputes, and a
    concurrent `pause` and `resume` racing on `paused_at` would produce either
    a lost pause or a pause credited twice. Locked, the loser blocks and reads
    the winner's committed state; unlocked, both read the same starting row and
    one write silently clobbers the other on commit.
    """
    stmt = select(TicketSla).where(TicketSla.org_id == org_id, TicketSla.ticket_id == ticket_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def _entered_current_status_at(
    session: AsyncSession, org_id: UUID, ticket: Ticket
) -> datetime:
    """The instant `ticket` most recently entered its CURRENT (`status`)
    value, read from `ticket_status_history` (spec §2, §3.3 -- "every
    transition, including creation, gets a status-history row").

    Falls back to `ticket.created_at` when no matching row is found -- which
    is the correct answer for that case, not a defensive placeholder:
    `create_ticket` always writes a `from_status=None` history row in the same
    transaction as the ticket itself, so a miss here means either the ticket
    has never actually been in `ticket.status` (unreachable through any
    application path) or this is being exercised directly by a test that
    skipped seeding history, and in the latter case `created_at` is exactly
    what "entered its very first status" means.

    Ordered `DESC LIMIT 1` rather than taking the ticket's very first entry
    into this status: a ticket can leave a pausing status and later re-enter
    it (`pending -> open -> pending` is a legal cycle in `lifecycle
    ._TRANSITIONS`), and every entry before the most recent one was already
    closed by its own `pause`/`resume` pair, whose credit already lives in
    `paused_minutes_total` on any row that existed at the time. Only a row
    materialising for the FIRST time reaches this function while the ticket
    is already paused (see `_materialise`'s docstring), so the credit that
    matters is exclusively the CURRENT, still-open pause.
    """
    stmt = (
        select(TicketStatusHistory.created_at)
        .where(
            TicketStatusHistory.org_id == org_id,
            TicketStatusHistory.ticket_id == ticket.id,
            TicketStatusHistory.to_status == ticket.status,
        )
        .order_by(TicketStatusHistory.created_at.desc())
        .limit(1)
    )
    entered_at = (await session.execute(stmt)).scalar_one_or_none()
    return entered_at if entered_at is not None else ticket.created_at


async def _materialise(
    session: AsyncSession,
    org_id: UUID,
    ticket: Ticket,
    policy: SlaPolicy,
    spec: CalendarSpec,
    source: Literal["initial", "retriage"],
) -> TicketSla:
    """INSERT a fresh `ticket_sla` row with both deadlines measured from
    `ticket.created_at` (§0.3).

    `source` is typed as `Literal["initial", "retriage"]` AND validated below
    against `_WRITABLE_POLICY_SOURCES` at runtime: the `Literal` is a static
    check only (erased once the code runs), so it stops a *typo* at review
    time but not a value that arrives through a variable `mypy` cannot narrow.
    `escalation` is `POLICY_SOURCES`' third value and must never reach this
    function (§0.4) -- today both call sites pass a literal, so this can only
    ever fire if a future edit changes that, and it is cheap insurance against
    exactly the kind of caller-supplied value this docstring warns about.

    `ticket_sla.ticket_id` is UNIQUE (spec §2.5: one SLA per ticket, multiple
    concurrent SLAs deferred to §13), so a second concurrent materialisation
    would raise `IntegrityError` out of the service. `ON CONFLICT DO NOTHING`
    + re-read makes the loser of that race return the WINNER's row instead --
    the same idempotent no-op a sequential second call gets from
    `start_tracking`'s early return, and never a half-written second promise.
    The conflict target is `index_elements=["ticket_id"]`, not `constraint=`:
    the UNIQUE comes from `sa.Column(..., unique=True)` in migration 0033, so
    its name is Postgres' implicit `ticket_sla_ticket_id_key` rather than
    anything this codebase chose, and column inference does not depend on it.

    Reaching the conflict path and STILL seeing no row in this org means the
    ticket's one `ticket_sla` row is owned by a different org -- impossible
    through any application path (`_require_same_org` above rejects the
    cross-org ticket first), and reported as a typed error rather than
    silently returning another tenant's promise.

    A ROW MATERIALISED ONTO A TICKET THAT IS *ALREADY* IN A PAUSING STATUS IS
    BORN PAUSED (§5; Task 8's wiring). `paused_at` is set in the INSERT rather
    than by a follow-up `pause` call, and three things about that matter:

    * **That it happens at all.** A `request` whose category requires approval
      is created directly into `pending_approval` -- `lifecycle.initial_status`
      returns it, so the ticket never *transitions* into that status and the
      ticket core's `change_status` pause hook never fires for it. Without
      this, every approval-gated request runs its clock for the whole time it
      is blocked on an approver, and the `pending_approval -> in_progress`
      transition then calls `resume` on a row with `paused_at IS NULL`, which
      is a documented no-op -- so the entire approval wait is charged to the
      service desk. §5 says entering a pausing status stops the clock; being
      *created* into one is entering it. The same applies to
      `recompute_for_retriage` materialising a row for the first time on a
      ticket already sitting in `pending`.
    * **That the stamp is WHEN THE TICKET ENTERED THE STATUS, not when this
      function happens to run** (review sla-breach-wiring, F4). The creation
      path and the retriage path reach this line at very different distances
      from the actual pause: a request born into `pending_approval` reaches
      here in the same transaction as its own creation, so "now" and "entered
      the status" are the same instant to within a flush -- but
      `recompute_for_retriage` materialising a row for the FIRST time onto a
      ticket that has already been sitting in `pending` for days (no policy
      matched at creation; one now resolves) would, stamped at "now",
      silently discard every minute of that wait: the ticket was blocked on
      someone who is not us for days, and crediting only the instant of the
      retriage understates exactly the credit born-pause exists to give.
      `_entered_current_status_at` reads the true instant off
      `ticket_status_history` (falling back to `ticket.created_at`, which is
      correct for the creation path -- see its own docstring) so both paths
      credit the whole time actually spent paused, not just the tail of it.
    * **That it is an INSERT and not an UPDATE.** `portal_user` holds INSERT
      on `ticket_sla` and deliberately no UPDATE (migration 0038), because a
      requester must not be able to move a deadline. A portal-filed request in
      an approval-gated category reaches exactly this line, so expressing
      "born paused" as `pause()`'s UPDATE would have forced a grant that
      undoes that whole restriction to fix a clock.

    A ticket in an ordinary status materialises with `paused_at` NULL, which is
    every other case.
    """
    if source not in _WRITABLE_POLICY_SOURCES:
        raise SlaValidationError(
            tr(
                "policy_source {source} may not be written by tracking._materialise; only "
                "{allowed} are legal here -- 'escalation' is Task 9's alone",
                source=repr(source),
                allowed=_WRITABLE_POLICY_SOURCES,
            )
        )

    response_due = add_working_minutes(spec, ticket.created_at, policy.response_target_minutes)
    resolution_due = add_working_minutes(spec, ticket.created_at, policy.resolution_target_minutes)
    now = _utcnow()

    # Born paused when the ticket is already blocked -- see docstring. The
    # stamp is the instant the ticket ENTERED the status (F4), not `now`.
    born_paused_at: datetime | None = None
    if ticket.status in PAUSING_STATUSES:
        born_paused_at = await _entered_current_status_at(session, org_id, ticket)

    await session.execute(
        pg_insert(TicketSla)
        .values(
            org_id=org_id,
            ticket_id=ticket.id,
            policy_id=policy.id,
            calendar_id=policy.calendar_id,
            response_due_at=response_due,
            resolution_due_at=resolution_due,
            paused_at=born_paused_at,
            policy_source=source,
            last_recomputed_at=now,
        )
        .on_conflict_do_nothing(index_elements=["ticket_id"])
    )

    row = await _load_tracking(session, org_id, ticket.id)
    if row is None:
        raise SlaValidationError(
            tr(
                "ticket {id} already has an SLA record that is not visible in org {org_id}; "
                "refusing to create a second one",
                id=ticket.id,
                org_id=org_id,
            )
        )
    return row


async def start_tracking(session: AsyncSession, org_id: UUID, ticket: Ticket) -> TicketSla | None:
    """Materialise a ticket's SLA at creation: `policy_source = 'initial'`,
    both deadlines from `ticket.created_at`.

    Returns `None` and writes NO row when `resolve_policy` finds nothing (spec
    §3) -- see the module docstring.

    CALLING THIS TWICE FOR ONE TICKET IS AN IDEMPOTENT NO-OP that returns the
    EXISTING row (the Task 5 brief's item 5; `ticket_sla.ticket_id` is UNIQUE,
    so the alternatives were an `IntegrityError` escaping the service or a
    typed error). No-op rather than error because the realistic second caller
    is a retry -- a redelivered create hook, a re-run backfill -- and failing
    those turns an at-least-once delivery into an error the operator must
    interpret. It deliberately does NOT refresh the row: re-running creation
    over an existing promise would silently rewrite deadlines under whatever
    policy happens to match today, which is exactly the unattributed recompute
    §0.3/§0.4 exist to prevent. The only sanctioned rewrite is
    `recompute_for_retriage`, and it stamps `policy_source` to say so.

    Raises `UnsatisfiableCalendar` (Task 2) if the resolved policy's calendar
    cannot deliver the target -- surfaced, never silently defaulted, and no
    row is written.
    """
    _require_same_org(org_id, ticket)

    existing = await _load_tracking(session, org_id, ticket.id)
    if existing is not None:
        return existing

    policy = await resolve_policy(
        session,
        org_id,
        client_id=ticket.client_id,
        queue_id=ticket.queue_id,
        priority=ticket.priority,
    )
    if policy is None:
        return None

    spec = await load_calendar_spec(session, org_id, policy.calendar_id)
    return await _materialise(session, org_id, ticket, policy, spec, "initial")


async def recompute_for_retriage(
    session: AsyncSession, org_id: UUID, ticket: Ticket
) -> TicketSla | None:
    """Re-resolve the policy after a HUMAN re-triage and recompute the still-
    running deadlines **from `ticket.created_at`** (§0.3), stamping
    `policy_source = 'retriage'`.

    Call this only for a human-originated priority or queue change. Breach
    escalation must NOT call it (§0.4): it writes `policy_source =
    'escalation'` and leaves the deadlines alone, which is what stops the
    escalate -> instant re-breach -> escalate cascade. This function does not
    inspect the EXISTING `policy_source`, deliberately -- a human re-triaging a
    ticket that was previously escalated is ordinary triage and must recompute
    like any other; the guard lives at the caller, where the actor is known.

    A deadline already met or breached is NEVER rewritten -- that clock has
    stopped and its due-at is now history that reporting reads (spec §7's
    "recompute touches only tickets whose clock is still running"). The two
    clocks stop independently, so a met response and a running resolution
    recompute exactly one of the two. When BOTH have stopped the row is
    returned entirely untouched, `policy_id` and `policy_source` included:
    rewriting the policy of a row with no running clock would re-attribute a
    finished promise to a policy that never governed it, destroying the
    §0.3 distinction the column exists for.

    A ticket with no `ticket_sla` row yet -- created when no policy matched
    (§3) and re-triaged into one that does -- gets its row materialised here,
    with `policy_source = 'retriage'` because that is why the policy applies.
    A re-triage that resolves to NO policy leaves any existing row exactly as
    it is (returning it): a promise already made is not withdrawn because the
    ticket moved somewhere unpoliced, and deleting the row would destroy the
    record of what was promised.

    PAUSE CREDIT IS FOLDED BACK IN (spec §5; migration 0037, Task 7). Deriving
    from `created_at` re-derives a deadline that never knew about pause, so
    without `paused_minutes_total` this function silently destroyed the credit
    of every pause that had already ENDED: the only record of a completed pause
    was the offset already inside `due_at`, and this function overwrites
    `due_at`. Measured before the fix -- 24/7 calendar, filed Monday 09:00,
    480-minute target, 120 completed paused minutes: 17:00 -> 19:00 at unpause
    -> back to **17:00** after a re-triage onto a policy with IDENTICAL
    targets. Two hours destroyed by a recompute that changed nothing.

    The credit is added to the `minutes` ARGUMENT of `add_working_minutes`, not
    as a `timedelta` on its result. That is the same distinction §5 makes about
    measuring pause itself: spending 120 minutes of credit in wall-clock on a
    Friday afternoon buys the whole weekend, which is precisely the free time
    §5 forbids. Folded into the target, the credit is spent in working time
    like every other minute of the promise.

    A pause still OPEN contributes nothing here and must not: its credit is not
    known until it ends, and is applied by `resume`, which re-derives with the
    same formula. `paused_at` is deliberately untouched by this function.

    KNOWN LIMITATION: `policy_id` CANNOT be made fully accurate for a row with
    exactly ONE clock stopped (Task 5 review F4). §2.5 gives the row a single
    `policy_id`/`calendar_id`/`policy_source` for BOTH deadlines; when only one
    clock is running, this function updates all three to the freshly resolved
    policy so they correctly describe the still-open half of the promise --
    the half that will actually be checked for breach and that a further
    retriage will recompute again. The stopped clock's `*_due_at` is (correctly)
    left untouched, which means it may now sit next to a `policy_id` that never
    produced it: e.g. a met response frozen at the `normal` policy's 60-minute
    target, on a row whose `policy_id` now reads `urgent`. This is NOT an
    oversight; it is the better of two wrong answers. The alternative --
    leaving `policy_id` pointed at the OLD policy whenever any clock is
    stopped -- was evaluated and rejected: it would leave the row's `policy_id`
    permanently stale from the first partial stop onward, misattributing every
    subsequent retriage's recompute of the still-running clock, which is the
    deadline that actually matters operationally (it is what future breach
    detection and escalation act on). Neither option is correct, because
    correctness here requires a policy attribution PER CLOCK, which this
    single-column schema cannot express -- that is squarely a §13 item
    (multiple/per-target SLA attribution), not a Task 5 bug to work around with
    a schema change this task does not own. Reporting that joins `policy_id` to
    interpret a STOPPED clock's `*_due_at` must not treat `policy_id` as
    authoritative for that clock; the both-clocks-stopped case (above) is the
    only state where `policy_id` is guaranteed to describe the whole row.

    Raises `UnsatisfiableCalendar` (Task 2) if the resolved policy's calendar
    cannot deliver a target for a clock that is still running -- surfaced,
    never silently defaulted, and no partial write survives it (the exception
    propagates out of the `add_working_minutes` call below, before either
    `*_due_at` is assigned on `tracking`, so an unsatisfiable calendar leaves
    the row exactly as it was found).
    """
    _require_same_org(org_id, ticket)

    # `for_update=True`: this function reads then writes the same row, and
    # Task 6's unpause writes the same due-at columns concurrently (F7) --
    # locked so the loser of that race blocks and re-reads rather than
    # clobbering the winner's write on commit.
    tracking = await _load_tracking(session, org_id, ticket.id, for_update=True)
    policy = await resolve_policy(
        session,
        org_id,
        client_id=ticket.client_id,
        queue_id=ticket.queue_id,
        priority=ticket.priority,
    )
    if policy is None:
        return tracking
    return await _retriage_against(session, org_id, ticket, tracking, policy)


async def _retriage_against(
    session: AsyncSession,
    org_id: UUID,
    ticket: Ticket,
    tracking: TicketSla | None,
    policy: SlaPolicy,
) -> TicketSla:
    """`recompute_for_retriage` once a policy has resolved: materialise a row
    the ticket did not have, or recompute the still-running deadlines of the
    one it has. Shared with `recompute_for_move`, which decides differently
    only when NO policy resolves."""
    if tracking is None:
        spec = await load_calendar_spec(session, org_id, policy.calendar_id)
        return await _materialise(session, org_id, ticket, policy, spec, "retriage")

    response_running = tracking.response_met_at is None and tracking.response_breached_at is None
    resolution_running = (
        tracking.resolution_met_at is None and tracking.resolution_breached_at is None
    )
    if not response_running and not resolution_running:
        return tracking

    # Loaded only once a clock is actually running: an unsatisfiable calendar
    # must not be raised at a ticket whose deadlines this call would not have
    # touched anyway.
    spec = await load_calendar_spec(session, org_id, policy.calendar_id)
    # Into the `minutes` argument, never onto the result -- see the docstring.
    credit = tracking.paused_minutes_total
    if response_running:
        tracking.response_due_at = add_working_minutes(
            spec, ticket.created_at, policy.response_target_minutes + credit
        )
    if resolution_running:
        tracking.resolution_due_at = add_working_minutes(
            spec, ticket.created_at, policy.resolution_target_minutes + credit
        )
    tracking.policy_id = policy.id
    tracking.calendar_id = policy.calendar_id
    tracking.policy_source = "retriage"
    tracking.last_recomputed_at = _utcnow()
    await session.flush()
    return tracking


async def recompute_for_move(
    session: AsyncSession, org_id: UUID, ticket: Ticket
) -> Literal["voided"] | None:
    """The SLA of a ticket that has just moved to another client (MSP clients
    spec §2.4; plan 2026-10-26, D2), called by `tickets.move_ticket` after the
    ticket's `client_id` changed.

    When a policy resolves for the new client -- its own, or a global one when
    it has none -- this is `recompute_for_retriage`, unchanged: a move is a
    human re-triage of whose promise applies.

    When NONE resolves, the ticket has NO SLA, and its row is DELETED, which is
    where a move parts from a re-triage. `recompute_for_retriage` keeps a
    promise already made when a ticket is re-triaged somewhere unpoliced -- the
    same client's contract still stands behind it. A move ends that contract:
    the row would go on naming the old client's policy, which a technician of
    the new client cannot read (`resume` -> `_load_policy_in_org` fails for
    them), and spec §3 wants an absent SLA to be visibly absent, never a
    reference to someone else's. Returns `"voided"` when a row was deleted, so
    the caller can say so in the move's audit; `None` otherwise.
    """
    _require_same_org(org_id, ticket)
    tracking = await _load_tracking(session, org_id, ticket.id, for_update=True)
    policy = await resolve_policy(
        session,
        org_id,
        client_id=ticket.client_id,
        queue_id=ticket.queue_id,
        priority=ticket.priority,
    )
    if policy is not None:
        await _retriage_against(session, org_id, ticket, tracking, policy)
        return None
    if tracking is None:
        return None
    await session.delete(tracking)
    await session.flush()
    return "voided"


# ---------------------------------------------------------------------------
# Pause and resume (spec §5; Task 7)
# ---------------------------------------------------------------------------


async def _load_ticket_in_org(session: AsyncSession, org_id: UUID, ticket_id: UUID) -> Ticket:
    """The ticket, constrained to `org_id` (invariant #5).

    `pause`/`resume`/`note_public_comment` take a `ticket_id` rather than a
    `Ticket` (the plan's declared interface), so unlike `start_tracking` there
    is no in-memory `_require_same_org` check available -- this predicate IS
    the tenancy guard on those paths, and it is load-bearing rather than
    decorative: `ticket_sla.ticket_id` is a plain FK that does not check org
    membership, so a row this org owns can legally point at another org's
    ticket. Without the `org_id` predicate, `resume` would read that foreign
    ticket's `created_at` and re-derive this org's deadlines from another
    tenant's data, and `note_public_comment` would read that foreign ticket's
    `requester_id` and decide this org's response SLA from it.

    Raises rather than returning None: reaching here means this module has been
    asked to act on that ticket's SLA state, and every caller reached it
    through a path where the ticket's existence in this org is already implied
    (`resume` from a `ticket_sla` row, `note_public_comment` from a comment the
    ticket core accepted after its own org-scoped `get_ticket`). A miss is
    therefore a broken invariant, not an ordinary absence a caller could
    sensibly ignore.
    """
    ticket = (
        await session.execute(select(Ticket).where(Ticket.org_id == org_id, Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        raise SlaValidationError(
            tr(
                "org {org_id} asked this module to act on ticket {ticket_id}'s SLA state, "
                "but the ticket itself is not visible in that org; refusing to act on it",
                org_id=org_id,
                ticket_id=ticket_id,
            )
        )
    return ticket


async def _load_policy_in_org(session: AsyncSession, org_id: UUID, policy_id: UUID) -> SlaPolicy:
    """The `ticket_sla` row's OWN policy, constrained to `org_id` (invariant
    #5). Same reasoning as `_load_ticket_in_org`: `ticket_sla.policy_id` is a
    plain FK with no org check, so without the predicate `resume` would
    re-derive this org's deadlines against another tenant's targets.

    Loaded by id, NOT via `resolve_policy`. Resume is not a re-triage: it must
    apply the promise the ticket is already under, and re-resolving here would
    quietly perform §0.3's recompute every time somebody took a ticket off
    `pending` -- a policy change would then land on tickets by way of an
    unrelated status change, unattributed, with `policy_source` still reading
    whatever it read before.

    `active` is deliberately NOT filtered: a retired policy still governs the
    tickets already under it (§9 -- deleting a policy is refused precisely
    because `ticket_sla` rows reference it), and skipping it here would make
    resume fail on exactly those tickets.
    """
    policy = (
        await session.execute(
            select(SlaPolicy).where(SlaPolicy.org_id == org_id, SlaPolicy.id == policy_id)
        )
    ).scalar_one_or_none()
    if policy is None:
        raise SlaValidationError(
            tr(
                "policy {policy_id} referenced by the SLA record for ticket in org {org_id} "
                "is not visible in that org; refusing to act on it",
                policy_id=policy_id,
                org_id=org_id,
            )
        )
    return policy


def _open_pause_minutes(
    spec: CalendarSpec, paused_at: datetime, now: datetime, ticket_id: UUID
) -> int:
    """The working minutes of a STILL-OPEN pause, measured over at most
    `MAX_LOOKAHEAD_DAYS`.

    WHY THE CLAMP EXISTS (review F2). `working_minutes_between` raises
    `ValueError` on a span wider than `MAX_LOOKAHEAD_DAYS`, on the reasoning
    that "no target reachable on any calendar can sit further out than the
    lookahead horizon, so a wider span is a corrupt timestamp rather than a
    long-lived ticket". That reasoning is sound where it is written -- about a
    DEADLINE -- and false here. `paused_at` to now is not a computed instant; it
    is real elapsed wall time in a pausing status, and an incident that sits in
    `pending` waiting on a vendor RMA for thirteen months is an ordinary,
    entirely non-corrupt fact about an MSP.

    Left unclamped it is also unrecoverable. `ValueError` is deliberately not in
    `sla_misconfiguration_is_not_fatal`'s catch (see its docstring, and it
    should stay out -- it is the guard against this module's own arithmetic
    going wrong). So the exception escapes `change_status` and rolls back the
    transaction, and since EVERY exit from a pausing status runs `resume`, the
    ticket cannot be resumed, resolved, closed OR CANCELLED. There is no
    in-application recovery from that; it is precisely the outcome that
    docstring names as the worst possible place to be stuck.

    WHAT IS TRADED. Past the horizon the credit is UNDERSTATED: only the last
    `MAX_LOOKAHEAD_DAYS` of the pause are measured and the earlier part is
    dropped. Understating is the direction to fail in -- a pause credit is time
    handed back to the ticket, so too little makes the deadline tighter, never
    looser, and can never let a ticket buy free time (§5's whole concern). The
    alternative -- crediting the pause in full -- is not actually available:
    the re-derivation this feeds, `add_working_minutes(created_at, target +
    credit)`, cannot express an instant more than `MAX_LOOKAHEAD_DAYS` of
    working time out either, and would raise `UnsatisfiableCalendar` instead.
    A pause this long has left the range of promise the module can represent;
    what matters is that it says so loudly and leaves the ticket movable.

    THE TAIL, NOT THE HEAD. The window kept is the most recent
    `MAX_LOOKAHEAD_DAYS`, not the first. Keeping the head would freeze the
    credit the moment the pause crossed the horizon, so two tickets paused on
    the same day and resumed a year apart would be owed the same; keeping the
    tail leaves the credit growing with the length of the pause, which is at
    least the right shape.

    LOGGED AT WARNING, not error and not audited: nothing is broken and no
    human decided anything, but a ticket paused for over a year is worth
    surfacing on its own account.

    The clamp is a day short of the horizon because
    `working_minutes_between`'s bound is measured in LOCAL DATES, not elapsed
    time: a 366-day wall-clock span can straddle 367 local dates and trip the
    guard the clamp exists to avoid.
    """
    floor = now - timedelta(days=MAX_LOOKAHEAD_DAYS - 1)
    if paused_at < floor:
        logger.warning(
            "sla.resume: ticket %s has been paused since %s, more than "
            "MAX_LOOKAHEAD_DAYS=%d; crediting only the working time since %s and "
            "discarding the earlier part of the pause",
            ticket_id,
            paused_at.isoformat(),
            MAX_LOOKAHEAD_DAYS,
            floor.isoformat(),
        )
        paused_at = floor
    return working_minutes_between(spec, paused_at, now)


async def pause(session: AsyncSession, org_id: UUID, ticket_id: UUID) -> None:
    """Stamp `paused_at` because the ticket entered a `PAUSING_STATUSES`
    status (spec §5).

    Stamping is all that happens. Nothing is added to any deadline here,
    because how long the pause lasts is not known yet -- `resume` is where the
    working-time credit is measured and applied.

    A DOUBLE PAUSE IS A NO-OP, not a second stamp. This is the load-bearing
    behaviour, not a defensive nicety: re-stamping `paused_at` on a row that is
    already paused would move the pause's start forward to now and throw away
    everything accrued since the real pause began -- a redelivered status hook,
    or a `pending -> pending_approval`-shaped sequence, would silently shorten
    the credit the ticket is owed. Doing nothing is also what makes this safe
    to call from an at-least-once event path.

    A ticket with no `ticket_sla` row (no policy matched, §3) is a no-op: there
    is no promise to stop.

    `paused_at` is stamped even when both clocks have already stopped. It costs
    nothing (`resume`'s running-clock guard extends neither deadline) and
    keeps the column meaning exactly what §5 says it means -- "this ticket is
    in a pausing status right now" -- rather than "this ticket is in a pausing
    status AND we still owe something", which is a different predicate that the
    sweep's `paused_at IS NULL` filter would then be reading wrongly.
    """
    tracking = await _load_tracking(session, org_id, ticket_id, for_update=True)
    if tracking is None or tracking.paused_at is not None:
        return

    tracking.paused_at = _utcnow()
    await session.flush()


async def resume(session: AsyncSession, org_id: UUID, ticket_id: UUID) -> None:
    """Close an open pause: measure it in WORKING time, give the credit back to
    every deadline still running, and clear `paused_at` (spec §5).

    PAUSE IS MEASURED IN WORKING TIME, NOT WALL CLOCK. A pause from Friday
    16:00 to Monday 09:00 on a Mon-Fri 09:00-17:00 calendar extends the
    deadline by **60 minutes** -- the one working hour on the Friday -- not by
    the 65 hours that elapsed. The weekend was never time we owed anyone, so
    giving it back would be inventing time. Measuring the pause in wall-clock
    while measuring the target in working time is the specific failure §5
    calls out: it lets a Friday-afternoon pause buy two free days.

    THE RUNNING-CLOCK GUARD. A deadline whose clock has stopped -- met or
    breached -- is never extended and never re-derived. That clock's `due_at`
    is now history reporting reads, and moving it would rewrite what was
    promised after the fact. The two clocks stop independently, so a met
    response beside a running resolution credits exactly one of the two.

    RE-DERIVED, NOT MUTATED. Each running deadline is recomputed from
    `ticket.created_at` as `target + paused_minutes_total` working minutes,
    which is character-for-character the formula `recompute_for_retriage` uses.
    One formula, one place to be wrong: the alternative -- adding the pause
    onto the stored `due_at` -- is only *equivalent* while every other writer
    of `due_at` also derives from `created_at` under the same calendar, and it
    stops being equivalent the moment one does not. Deriving from the ticket's
    start makes the two paths agree by construction rather than by coincidence.

    The credit is folded into the `minutes` ARGUMENT, never added to the result
    as a `timedelta` -- see `recompute_for_retriage`'s docstring; a `timedelta`
    spends the credit in wall-clock and buys back the weekend §5 forbids.

    ONE ACCUMULATOR SERVES BOTH CLOCKS, and that is sound rather than a
    simplification: a clock only ever transitions running -> stopped, never
    back, so any clock that is running NOW was running during every pause
    already in `paused_minutes_total` and is owed all of it. A clock that was
    stopped during some earlier pause is stopped still, and the guard above
    means it is never re-derived, so the credit it did not earn is never spent
    on it. Per-clock pause accounting would be dead weight the schema cannot
    currently express (§2.5 gives the row one of everything) and that no state
    reachable by this module can distinguish.

    NOT paused, or no `ticket_sla` row at all: a no-op. Like `pause`, this is
    safe to call from an at-least-once status-change path and safe to call for
    a ticket that never had a policy (§3).

    **LEAVING THE PAUSING STATUS ALWAYS WINS OVER GETTING THE DEADLINE RIGHT**
    (review F2). This function runs on every exit from `pending` /
    `pending_approval`, and every status move a ticket can make runs through
    `change_status`, so anything that can make `resume` fail durably makes the
    ticket unresolvable, unclosable and uncancellable. Two paths could:
    measuring an over-horizon pause (`_open_pause_minutes` bounds it) and
    re-deriving a deadline the calendar cannot deliver. The second cannot be
    prevented -- an admin may empty the calendar at any moment -- so the pause
    is closed and FLUSHED before the re-derivation is attempted. A ticket with
    stale deadlines and a loud log line is recoverable by a human; a ticket
    stuck in `pending` with `paused_at` set is not recoverable at all.

    Which calendar: `ticket_sla.calendar_id`, the row's own record of the
    calendar its deadlines were derived on -- not `policy.calendar_id`, which
    an admin may have repointed since. Both the pause measurement and the
    re-derivation use that one spec, so they cannot disagree with each other.

    KNOWN INTERACTION WITH BREACH ESCALATION (§0.4, Task 9). Escalation writes
    `policy_source = 'escalation'` and deliberately does NOT recompute
    deadlines. If Task 9 also repoints `ticket_sla.policy_id` at the escalated
    priority's policy, then re-deriving here would apply that shorter target to
    the still-running clock -- reintroducing §0.4's cascade through an
    unrelated status change. Task 9 must therefore either leave `policy_id`
    alone or accept that a later human action (this, and equally
    `recompute_for_retriage`) re-derives under it. Recorded here rather than
    guarded against, because guessing which side Task 9 will take and coding
    to the guess is how the two tasks end up disagreeing silently.
    """
    tracking = await _load_tracking(session, org_id, ticket_id, for_update=True)
    if tracking is None or tracking.paused_at is None:
        return

    spec = await load_calendar_spec(session, org_id, tracking.calendar_id)
    # `working_minutes_between` floors at zero, so a clock skew that puts
    # `paused_at` in the future cannot hand back negative time; and
    # `_open_pause_minutes` bounds the span, because a pause longer than
    # `MAX_LOOKAHEAD_DAYS` is a real thing an incident does and a `ValueError`
    # out of here would trap the ticket in the pausing status forever (F2).
    credit = tracking.paused_minutes_total + _open_pause_minutes(
        spec, tracking.paused_at, _utcnow(), ticket_id
    )

    # CLOSING THE PAUSE IS WRITTEN AND FLUSHED **BEFORE** ANY DEADLINE IS
    # RE-DERIVED, and the order is load-bearing rather than stylistic (review
    # F2b). `add_working_minutes` below can raise `UnsatisfiableCalendar` --
    # an admin repointing the policy at an empty calendar is enough, and so is
    # a `target + credit` that no longer fits inside the lookahead horizon,
    # which is exactly what a very long pause produces. That exception is
    # SWALLOWED by `sla_misconfiguration_is_not_fatal`, so the ticket's status
    # move succeeds regardless. With the clear at the end, the row keeps
    # `paused_at` set on a ticket that is no longer in a pausing status: the
    # sweep's `paused_at IS NULL` filter then excludes it forever (it can never
    # breach), `views.target_state` renders "paused" on an open ticket, and
    # `count_tickets_without_sla` does not see it either -- an invisible,
    # permanently frozen promise. Clearing first degrades the same failure to
    # "the pause closed, the deadlines did not move, and there is an ERROR line
    # saying so", which is what that wrapper's docstring promises.
    #
    # Flushed here rather than left to the unit of work: if the re-derivation
    # raises, this function never reaches its final flush, and whether the
    # pending attribute change still reaches the database would depend on the
    # caller happening to flush afterwards.
    tracking.paused_minutes_total = credit
    tracking.paused_at = None
    await session.flush()

    response_running = tracking.response_met_at is None and tracking.response_breached_at is None
    resolution_running = (
        tracking.resolution_met_at is None and tracking.resolution_breached_at is None
    )
    if response_running or resolution_running:
        # Loaded only once a clock is actually running -- nothing below is
        # needed to close a pause on a ticket whose promise is already settled.
        ticket = await _load_ticket_in_org(session, org_id, ticket_id)
        policy = await _load_policy_in_org(session, org_id, tracking.policy_id)
        if response_running:
            tracking.response_due_at = add_working_minutes(
                spec, ticket.created_at, policy.response_target_minutes + credit
            )
        if resolution_running:
            tracking.resolution_due_at = add_working_minutes(
                spec, ticket.created_at, policy.resolution_target_minutes + credit
            )
        await session.flush()


# ---------------------------------------------------------------------------
# Met detection (spec §0.6; Task 6)
# ---------------------------------------------------------------------------


def _response_running(tracking: TicketSla) -> bool:
    """The response clock is running iff neither stop column is set.

    Factored out because §5.1 amended the guard from "already met" to "already
    stopped" -- met OR breached -- and this module now reads it from four
    places (`recompute_for_retriage`, `resume`, and both hooks below). The
    duplicated inline pairs those two functions still carry predate this and
    are left alone rather than churned; what matters is that no NEW reading of
    the guard invents a narrower one. A `*_met_at`-only guard is Task 5's F1
    and §5.1's "same constant-pairing gap".

    **THIS IS NOT THE BREACH GUARD.** `_response_breachable` below is, and the
    two deliberately disagree on exactly one row shape (met late). Do not
    merge them and do not "widen" this one to match: see that function's
    docstring for the four callers whose correctness depends on this one
    staying a plain is-it-stopped question.
    """
    return tracking.response_met_at is None and tracking.response_breached_at is None


def _resolution_running(tracking: TicketSla) -> bool:
    """See `_response_running`. Independent of it: the two clocks stop
    separately, and a met response beside a running resolution is ordinary."""
    return tracking.resolution_met_at is None and tracking.resolution_breached_at is None


# ---------------------------------------------------------------------------
# Breach eligibility (spec §7; the other half of the sweep's predicate)
# ---------------------------------------------------------------------------


def _breachable(
    *, due_at: datetime, met_at: datetime | None, breached_at: datetime | None
) -> bool:
    """Was this promise BROKEN -- i.e. may `<target>_breached_at` be stamped?

    True iff the clock has not already been stamped breached AND it was not
    stopped on time::

        breached_at IS NULL AND (met_at IS NULL OR met_at > due_at)

    which is `sweep._breach_pass`'s selection predicate minus the parts that
    are about *when* to look (`due_at <= now`) and about whether the promise
    still exists at all (`paused_at`, `voided_at`, the ticket's status). Those
    stay in the sweep; this is the per-row question `breach.mark_breached` has
    to answer about a row it has already been handed.

    WHY THIS IS A DIFFERENT QUESTION FROM `_response_running`, AND WHY THE TWO
    MUST NOT BE COLLAPSED INTO ONE. "Is this clock running?" and "was this
    promise broken?" agree on every row except one, and that row is the whole
    reason this function exists:

        met_at IS NOT NULL AND met_at > due_at   -- a LATE reply

    Such a clock is NOT running -- contact happened, the stamp is real, and
    `recompute_for_retriage` / `resume` / `note_public_comment` /
    `note_resolved` must all keep their hands off it, which is exactly what
    `_response_running` returning False buys them. But the promise was still
    broken: the reply landed after the deadline. Asking the running question
    on the breach path is the gap this predicate closes -- §7's sweep already
    SELECTS such a row (`met_at IS NULL OR met_at > due_at`), so with the
    running guard on the stamping side the row was selected and skipped every
    300 seconds, forever, and never stamped.

    **`met_at` AND `breached_at` ARE NOT MUTUALLY EXCLUSIVE**, and a row
    carrying both is the correct record of a late reply rather than a
    contradiction: `met_at` says *when contact happened*, `breached_at` says
    *that the promise was broken*. Attainment reads `breached_at`; response
    time reads `met_at`; neither can be derived from the other once a reply is
    late.

    THE BOUNDARY IS `>`, NOT `>=`. `met_at == due_at` is met -- a promise of
    "within 60 minutes" is kept by a reply at minute 60 exactly, and `<=` is
    also what makes this the exact complement of the sweep's `met > due`
    (which is a strict `>` in SQL). Both readings look right in isolation and
    differ on precisely one instant, so `test_sla_breach.py` parametrises the
    met stamp at due-1s, due exactly, and due+1s.
    """
    if breached_at is not None:
        return False
    return met_at is None or met_at > due_at


def _response_breachable(tracking: TicketSla) -> bool:
    """`_breachable` for the response clock. See it for the full reasoning --
    in particular for why this is NOT `not _response_running(tracking)`."""
    return _breachable(
        due_at=tracking.response_due_at,
        met_at=tracking.response_met_at,
        breached_at=tracking.response_breached_at,
    )


def _resolution_breachable(tracking: TicketSla) -> bool:
    """`_breachable` for the resolution clock. Independent of the response
    one, and independently late: a ticket answered late and resolved early is
    an ordinary row, and it must breach `response` ONLY."""
    return _breachable(
        due_at=tracking.resolution_due_at,
        met_at=tracking.resolution_met_at,
        breached_at=tracking.resolution_breached_at,
    )


async def note_public_comment(
    session: AsyncSession, org_id: UUID, ticket_id: UUID, *, author_id: UUID | None
) -> None:
    """Stop the response clock because somebody who is not the requester
    posted a PUBLIC comment (spec §0.6).

    WHY THIS EVENT AND NOT ANOTHER. A response SLA is a promise to the person
    waiting, and a public comment from someone else is the only candidate they
    can actually perceive. The two rejected alternatives both stop the clock on
    activity the requester cannot see, which turns the metric into a measure of
    internal bookkeeping and, once true, makes the number meaningless to the
    customer it was promised to:

    * **First assignment** -- a ticket can be assigned and then sit untouched
      for a day with a green response SLA.
    * **First status change out of `new`** -- the same, one keystroke cheaper.

    **An internal note must never reach this function.** That is a WIRING
    guarantee, not a check here: `threads.add_comment` calls this only on the
    public branch of the very `is_internal` test that already decides whether
    it publishes `ticket.comment_added` or `ticket.internal_note_added`. The
    two event types exist precisely so staff discussion cannot be mistaken for
    customer contact, and re-deriving visibility inside this module would be a
    second answer to a question the ticket core already answers -- exactly the
    duplication §1's boundary forbids.

    A `None` `author_id` -- `threads.add_comment`'s `author_type="system"`
    shape, which `ck_comment_author` enforces at the database layer -- does NOT
    stop the clock (spec §0.6.1). §0.6 stops it for "someone who is not the
    requester", and an automated note has no someone behind it. The realistic
    producer is the change-management adapter's public "Change X is now Y"
    note on a linked ticket (`service_desk.adapters`), and letting that
    satisfy a response promise is the machine-generated form of the
    internal-bookkeeping metric §0.6 rejects -- and, unlike a human's mistake,
    one that would auto-satisfy every ticket linked to that change at once.
    This is a NARROWING of the brief's "the author is not the ticket's
    requester", written into the spec rather than left as only this
    docstring's decision.

    FIRST CONTACT WINS. `response_met_at` is stamped only while the clock is
    still RUNNING, so a second public comment never moves it -- response time
    is measured to the FIRST reply, and an overwriting implementation would
    silently become time-to-last-comment, growing without bound on a chatty
    ticket.

    "Running" means met AND breached are both NULL (§5.1), matching this
    module's existing guards rather than inventing a parallel rule. A reply
    that arrives after the sweep has already recorded the miss therefore leaves
    the breach standing and does not also mark the promise kept: a row reading
    breached AND met is not interpretable by reporting, and "we eventually
    replied" is what the ticket thread records. The brief names only
    `response_met_at IS NULL`; the breached half is the addition, for the same
    reason §5.1 gave when it widened `resume`'s guard.

    A ticket with no `ticket_sla` row (no policy matched, §3) is a no-op --
    materialising one here would attribute a promise to a comment rather than
    to creation or a re-triage, and §3 is explicit that an org which configured
    no policy must visibly have no SLA.

    No deadline is written and `paused_at`/`paused_minutes_total` are untouched:
    a promise kept during a pause is still kept, and the accumulator belongs to
    `resume`. `resume`'s own running-clock guard then declines to re-derive the
    stopped response deadline, so the credit lands only on whatever is still
    running.

    NOT AUDITED, for the module docstring's reason: §8's audited list names
    calendar/policy/settings changes, escalations and auto-closes. This follows
    automatically from a comment the ticket core has already audited and
    records no decision a human made.

    THE REQUESTER COMPARISON MUST PRECEDE ANY `ticket_sla` ACCESS. THIS
    ORDERING IS LOAD-BEARING, NOT STYLISTIC. The end-user portal runs on its
    own least-privilege database role (`portal_user`, migrations 0028/0030),
    which holds NO grant on `ticket_sla` at all -- it has no business reading
    the org's promises. `portal.tickets.add_my_comment` reaches `add_comment`,
    and therefore this hook, on every comment a requester posts, always with
    `author_id == requester_id` (the portal has exactly one comment path and
    it forces both). Because the requester test above runs BEFORE
    `_load_tracking`'s `ticket_sla` SELECT, every portal-originated call
    returns at that test having touched only `ticket` -- which `portal_user`
    may read -- and never reaches the table it has no grant on. Swap the two
    blocks and this still passes every test that calls the service directly,
    because none of them runs as `portal_user`; only a portal comment would
    start failing, with `asyncpg.exceptions.InsufficientPrivilegeError:
    permission denied for table ticket_sla`, on a caller several layers away
    from this file. `test_a_portal_requesters_public_comment_never_touches_
    ticket_sla` (`test_sla_met.py`) pins the ordering itself, through a real
    `portal_session` connection, precisely so that failure reads as an SLA
    regression rather than an unrelated portal bug. The cost of ordering it
    this way is one indexed primary-key SELECT on `ticket` per public comment
    in orgs that run no SLA at all; the alternative was a migration granting
    `portal_user` read access to a table it must not see.

    WHY THIS RE-SELECTS `ticket` INSTEAD OF TAKING ONE FROM THE CALLER, even
    though `threads.add_comment` has already loaded it. Deliberate, not an
    oversight: this function is called two ways -- through the ticket core
    with a `ticket_id` (the ordinary path, above) and directly, by this
    module's own tenancy tests, with a `ticket_id` for a ticket that does NOT
    belong to `org_id`. Loading by id, org-scoped, is what makes the second
    case a typed `SlaValidationError` instead of trusting an object the
    caller could have loaded under the wrong org. Accepting an already-loaded
    `Ticket` instead would move that guarantee to every caller and remove the
    one place it is currently proven (the tenancy test at the bottom of this
    file). The cost is the second SELECT this paragraph already prices; the
    alternative was trusting a caller-supplied object's provenance instead of
    re-checking it here.
    """
    if author_id is None:
        return

    ticket = await _load_ticket_in_org(session, org_id, ticket_id)
    if ticket.requester_id == author_id:
        return

    tracking = await _load_tracking(session, org_id, ticket_id, for_update=True)
    if tracking is None or not _response_running(tracking):
        return

    tracking.response_met_at = _utcnow()
    await session.flush()


async def note_resolved(session: AsyncSession, org_id: UUID, ticket_id: UUID) -> None:
    """Stop the resolution clock -- and the response clock with it -- because
    the ticket reached a resolved-like status (spec §0.6).

    **Resolution also stops the RESPONSE clock**, which is the half of §0.6
    that is easy to leave out: a ticket resolved outright with no reply -- a
    duplicate, say -- cannot owe a response forever, and leaving that clock
    running would have the sweep breach and escalate a ticket that is already
    finished.

    Wired from `transitions.change_status`'s existing
    `to_status in _RESOLVED_LIKE_STATUSES` branch -- the same test that stamps
    `ticket.resolved_at` and publishes `ticket.resolved`. `fulfilled` is the
    request lifecycle's counterpart to incident's `resolved` and the ticket
    core already treats the two as one concept there; keying off that branch
    rather than the string `"resolved"` is what keeps every request's promise
    from running forever. Nothing about the transition table is re-derived
    here (§1: this module defines no status and no transition).

    EACH CLOCK IS CONSIDERED SEPARATELY, and only while still running (§5.1:
    met OR breached). All four combinations matter and none implies another:
    an earlier `response_met_at` is history that must not be overwritten with
    the resolution instant (that would report every ticket's response time as
    its resolution time), a breached clock is not retroactively converted into
    a kept promise, and a re-resolve after a reopen must not move the original
    stamp.

    A REOPEN DOES NOT UN-STAMP anything, here or anywhere: §2.5.1 records that
    a clock only ever moves running -> stopped, never back. The ticket core
    clears its own `resolved_at` on a reopen; the SLA row keeps its met stamps,
    because the promise really was kept at that instant and erasing it would
    let a reopen launder a met ticket into a fresh, unmeasured one.

    A ticket with no `ticket_sla` row is a no-op (§3), and this is safe to call
    from an at-least-once status path.
    """
    tracking = await _load_tracking(session, org_id, ticket_id, for_update=True)
    if tracking is None:
        return

    now = _utcnow()
    stamped = False
    if _resolution_running(tracking):
        tracking.resolution_met_at = now
        stamped = True
    if _response_running(tracking):
        tracking.response_met_at = now
        stamped = True

    if stamped:
        await session.flush()


async def note_cancelled(session: AsyncSession, org_id: UUID, ticket_id: UUID) -> None:
    """VOID the promise, because the ticket reached a terminal-not-resolved
    status -- `cancelled` or `rejected`, i.e. `lifecycle.FINAL_STATUSES`
    (spec §6.0's "stale SLA promises on terminal tickets are voided at the
    source"; migration 0038).

    WHAT WAS BROKEN WITHOUT THIS. Nothing stopped the clocks on a cancelled or
    rejected ticket, so both deadlines went on being due on a ticket nobody
    would ever work again. **Every one of them breaches with certainty** once
    past its deadline -- at a 5-10% cancellation rate that is roughly 0.1-0.2
    phantom `ticket.sla_breached` events per ticket ever filed, each one
    indistinguishable in reporting from a promise the team really missed.
    Worse, §6's escalation refuses a ticket in `TERMINAL_STATUSES` by RAISING,
    so an org swept inside one transaction would have that raise roll back the
    cycle's real breaches and then retry the identical failure every sweep
    interval, forever.

    A VOIDED PROMISE IS NEITHER MET NOR BREACHED. That is why this needs its
    own column instead of borrowing one of the four stop columns.
    **Met-stamping on cancellation was rejected and must stay rejected**: it
    claims the promise was *kept*, which makes "cancel it" a one-click way to
    inflate SLA attainment -- the metric-laundering shape §0.6.1 already
    refuses for automated comments, here with a human pressing the button.

    A SWEEP FILTER WAS ALSO CONSIDERED AND REJECTED. `WHERE ticket.status NOT
    IN ('cancelled','rejected')` in §7's query stops the phantom events and
    nothing else: the invariant would live inside the one consumer that knows
    about it, so §9.1's ticket detail panel would still render a ticking red
    countdown on a cancelled ticket, and every future reader -- attainment
    reporting, an export, the frontend -- would have to rediscover the same
    join or repeat the same bug. `voided_at` is column-local, needs no join,
    and is readable by everything that already reads the row.

    IT ERASES NOTHING. Only `voided_at` is written. A clock that had ALREADY
    stopped keeps its `*_met_at`/`*_breached_at` exactly as it was, so
    cancelling a ticket that already breached does NOT launder the breach away
    -- the exact mirror of the met-stamping hazard above, and the reason this
    function has no "clear the stop columns" branch. `resolved -> cancelled` is
    a legal transition (`lifecycle._TRANSITIONS`), so that combination is
    reachable, not hypothetical: such a row reads as "met, and later voided",
    which is the truth.

    `paused_at` and `paused_minutes_total` are likewise untouched. A ticket
    cancelled while `pending` is voided, and `change_status` runs `resume`
    before this on that edge, so the pause is closed honestly rather than
    abandoned mid-flight.

    ALREADY VOIDED IS A NO-OP, not a re-stamp -- `voided_at` records WHEN the
    promise died, and a redelivered status hook must not move it forward.
    Unreachable through the transition table today (nothing leaves
    `FINAL_STATUSES`), which is precisely why it is written down rather than
    relied upon: this is the same at-least-once safety `pause` and `resume`
    carry.

    A ticket with no `ticket_sla` row is a no-op (§3), and nothing here is
    audited, for the module docstring's reason.
    """
    tracking = await _load_tracking(session, org_id, ticket_id, for_update=True)
    if tracking is None or tracking.voided_at is not None:
        return

    tracking.voided_at = _utcnow()
    await session.flush()
