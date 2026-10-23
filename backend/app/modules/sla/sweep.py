"""The breach sweep, auto-close, and the calendar-edit recompute
(docs/modules/sla/spec.md §7, §8, §10.11, §10.12, §10.13, §12; Task 10).

This is the file that makes the rest of the module do anything. Everything
Tasks 1-9 built is passive: `tracking.py` writes deadlines, `breach.py` marks a
row it is handed, and neither of them ever asks "which rows are due?". §7's
sweep is the only thing that does, so until this exists the whole engine is
inert on every real ticket while its own tests stay green.

Two passes per org, in one Celery beat task:

1. **Breach detection** -- select the rows whose promise is past due and not
   already settled, and hand each to `breach.mark_breached`, which stamps,
   publishes `ticket.sla_breached`, and escalates at most once per ticket.
2. **Auto-close** -- tickets sitting in `resolved`/`fulfilled` longer than
   `sla_org_settings.auto_close_after_days` are moved to `closed` through
   `transitions.change_status` with `actor_type="system"`.

---

THE BREACH PREDICATE, AND WHY IT IS NOT §7'S LITERAL TEXT.

§7 writes the predicate as `due_at <= now AND breached_at IS NULL AND met_at
IS NULL AND paused_at IS NULL`. What this module actually issues is::

    due_at <= now
    AND breached_at IS NULL
    AND (met_at IS NULL OR met_at > due_at)
    AND paused_at IS NULL
    AND voided_at IS NULL
    AND ticket.status NOT IN ('cancelled', 'rejected')   -- joined

All three differences are load-bearing and every one of them was found by
review rather than by writing the query down from the spec.

**`met_at > due_at` -- a LATE reply is not a kept promise.** `met_at IS NULL`
alone makes the outcome depend on which of two independent things ran first. A
response deadline falls at 10:00; the beat fires at 10:04. If nobody replies,
the sweep stamps a breach and `note_public_comment`'s running-clock guard then
refuses to met-stamp the 10:06 reply -- the promise reads breached, correctly.
If the technician replies at 10:02 instead, `response_met_at` is stamped four
minutes into a deadline that had already passed, the naive predicate excludes
the row forever, and the identical failure to answer in time reads as MET.
Attainment then measures the phase of the beat schedule rather than the
service desk. Comparing `met_at` to `due_at` asks the question that actually
matters -- was the promise kept *on time* -- and is order-independent by
construction.

    **THE STAMPING HALF LIVES IN `breach.py`, AND IT USED TO BE MISSING.**
    Widening this predicate only makes the sweep SELECT such a row; for one
    release `breach.mark_breached` guarded on `tracking._response_running` --
    met OR breached -- so every late-met row was selected, handed over and
    silently skipped, on every cycle, forever. `mark_breached` now guards on
    `tracking._breachable` (`breached_at IS NULL AND (met_at IS NULL OR
    met_at > due_at)`), which is this predicate's per-row half restated in
    Python. The two must stay in agreement, boundary included: `met_at ==
    due_at` is MET on both sides, which is why the SQL is a strict `>`.

**`voided_at IS NULL` -- a cancelled ticket's promise is neither met nor
broken.** `note_cancelled` (migration 0038) stamps `voided_at` when a ticket
reaches `cancelled`/`rejected`, and NOTHING ELSE stops those clocks: the four
stop columns stay NULL, deliberately, because voiding must not launder a real
breach and must not claim a promise was kept. Without this filter every
cancelled ticket in the fleet breaches with certainty the moment its deadline
passes -- at a 5-10% cancellation rate, roughly 0.1-0.2 phantom
`ticket.sla_breached` events per ticket ever filed, each indistinguishable in
reporting from a promise the team really missed. `mark_breached` does not
filter it (it is handed a row and marks it, by design), so this is the only
place the invariant can live for the sweep.

**AND the joined status exclusion, which is NOT redundant with it.** Migration
0038 added `voided_at` and deliberately did NOT backfill it -- it refuses to
fabricate a past instant for tickets cancelled before the column existed. The
consequence is that **every pre-0038 cancelled or rejected ticket carries
`voided_at IS NULL`**, so the column filter alone does not exclude a single
one of them, and the FIRST sweep in any existing deployment would emit one
phantom breach per historical cancelled ticket -- each of which, where the
matching policy enables escalation, then takes the raising path this filter
exists to keep out of the cycle. Joining `ticket` and excluding
`lifecycle.FINAL_STATUSES` covers exactly those rows, without inventing a
`voided_at` value for them.

Belt AND braces, and neither is a substitute for the other: the column is the
fast path and carries the semantics ("this promise is void" -- it is what a
future reader, a report or the frontend keys on, with no join), while the
status join is what makes the invariant true for rows written before the
column existed. `test_sla_sweep.py` pins each one with a fixture only IT can
catch: a row voided without a terminal status, and a terminal ticket with no
`voided_at`.

`FINAL_STATUSES` and not `TERMINAL_STATUSES`, mirroring `note_cancelled`
exactly: `closed` is in the wider set but a closed ticket's promise was
DISCHARGED, not voided -- it passed through `resolved`/`fulfilled` where
`note_resolved` stopped both clocks -- and it is deliberately reopenable.

The join predicate also carries `ticket.org_id = :org` (invariant #5):
`ticket_sla.ticket_id` is a plain FK with no org check, so a row this org owns
can legally point at another tenant's ticket, and without it the sweep would
read that foreign ticket's status to decide this org's breach.

**THE INDEXES WERE REDEFINED TO MATCH (migration 0039).** Task 1's
`ix_ticket_sla_org_response_due` was partial on `response_breached_at IS NULL
AND response_met_at IS NULL`, so it did not contain the late-met rows this
query must consider and Postgres could not use it at all -- the widening
turned an index scan into a sequential one. Migration 0039 redefines both
indexes on `<target>_breached_at IS NULL AND paused_at IS NULL AND voided_at
IS NULL`: the three columns of this predicate that are pure `IS NULL` tests on
`ticket_sla` itself, i.e. the half that is stable under every widening this
predicate has taken. `met_at` is deliberately NOT in the partial condition any
more -- it is now compared to `due_at` rather than to NULL, and a partial
index cannot express a cross-column comparison. It is filtered on the heap
instead, on a row set the index has already narrowed by org and deadline.

---

THE TRANSACTION BOUNDARY: ONE TRANSACTION PER ORG, ONE SAVEPOINT PER TICKET.

`mark_breached` never commits -- it flushes onto the caller's session
precisely so the caller can decide what a failure undoes ("Task 10's sweep
owns per-ticket containment", `breach.py`). This is that decision, and it has
two halves.

**One transaction per org, because `org_session` leaves no choice.** It pins
the RLS tenant with `SET LOCAL app.current_org`, which is scoped to the
transaction: committing mid-sweep would silently un-pin the tenant and every
statement afterwards would run with no tenant context at all. So a commit
between tickets is not available, and this function does not commit -- the
caller's `org_session` block commits once, on the way out.

**One SAVEPOINT per ticket, because one bad row must not cost the cycle.**
`session.begin_nested()` wraps each unit of work, so a failure rolls back only
that ticket's writes and the rest of the org's sweep still commits. A plain
`try`/`except` would not do: it leaves a transaction that has hit a DBAPI
error in an aborted state, where every later statement fails too, so a single
`IntegrityError` on ticket #3 would take out tickets #4-#900 with it and the
whole cycle would be retried, identically, every `SWEEP_INTERVAL_SECONDS`
forever. `ROLLBACK TO SAVEPOINT` is the only construct that clears that.

FOUR KNOWN RAISING PATHS, AND WHY THE HANDLER IS NOT A TUPLE OF THEM.

* `breach.mark_breached` raises `SlaValidationError` on an off-ladder
  `ticket.priority` -- reachable because `ticket.priority` carries no DB CHECK
  (only `sla_policy.priority` does) and `update_ticket` takes a `dict[str,
  Any]`.
* It raises `TicketNotFoundError` when the policy's `escalate_to_queue_id`
  names another tenant's queue -- a plain FK with no org check (invariant #5).
* The recompute path raises `UnsatisfiableCalendar` (a calendar with no
  windows) and `ValueError` (`calendar.MAX_LOOKAHEAD_DAYS`).
* And `ZoneInfo(cal.timezone)` raises `ZoneInfoNotFoundError`, which
  **subclasses `KeyError`** -- not `SlaValidationError`, not `ValueError`.

That last one is the argument for the handler's shape. Three careful reviews
of this module enumerated exception types and the fourth one still escaped,
because it is a `KeyError` from the standard library arriving through an
admin's typo in a timezone field. A sweep that dies on one org's misspelled
calendar is the failure this containment exists to prevent, so the handler
catches `Exception` -- the convention `worker.py` already follows in
`deliver_pending` and `backup_verification_sweep_all_orgs` -- logs at ERROR
with `exc_info` (fix wave 6/7: under this process's configured `JsonFormatter`
that renders as the exception's TYPE only, `exc_type`, never a traceback or
`str(exc)` -- §2, `docs/superpowers/specs/2026-08-14-logging-kernel-design.md`),
counts the failure into the returned dict, and continues. Nothing is
swallowed silently: `failed` is a first-class number the caller and the
tests assert on.

---

WHAT IS AUDITED, AND WHAT IS NOT (§8, invariant #4). §8 audits "every
escalation, and every auto-close", and says sweep cycles themselves are not
audited -- they are operational and would swamp the log. So: the breach pass
writes no audit row of its own (`mark_breached` audits the escalation it
performs), and the auto-close pass writes one `sla.auto_closed` row per ticket
IN ADDITION to `change_status`'s own `ticket.status_changed`. The second row
is not redundant: `ticket.status_changed` records only
`resolved -> closed, actor_type=system`, which says a machine closed the
ticket but not WHICH machine or under what rule, and cannot answer "was this
org's auto-close window respected" -- the `auto_close_after_days` that drove
the decision appears nowhere else. Same reasoning `breach._escalate` gave for
`sla.escalated` sitting beside `ticket.updated`.

ORG SCOPING (invariant #5). Every query here carries an explicit `org_id`
predicate as well as running under `org_session`'s RLS, because `sweep_org`
takes a session rather than opening one and is therefore callable on the
BYPASSRLS admin engine, where RLS masks nothing. `test_sla_sweep.py`'s
isolation test is pinned on that engine for exactly this reason.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.audit import audit_service
from app.core.tenancy import org_session
from app.models import Organization

# `cancelled`/`rejected` -- the statuses `note_cancelled` voids a promise on.
# Imported, not restated: §1 forbids this module owning any part of the ticket
# lifecycle's definition.
from app.modules.service_desk.lifecycle import FINAL_STATUSES
from app.modules.service_desk.models import Ticket

# Imported, never restated as the two string literals `'resolved'`/
# `'fulfilled'`. §1 forbids this module owning any part of the ticket
# lifecycle's definition, and a status added to the resolved-like set on the
# ticket core's side must reach auto-close automatically rather than by
# somebody remembering this file. Private on that side, like the
# `tracking._response_running` / `_load_policy_in_org` predicates `breach.py`
# already imports across the same boundary, and for the same reason: a
# predicate copied into a second file is a predicate that can drift in one
# of them.
from app.modules.service_desk.transitions import _RESOLVED_LIKE_STATUSES, change_status
from app.modules.sla.breach import mark_breached
from app.modules.sla.calendar import CalendarSpec, add_working_minutes
from app.modules.sla.calendars import load_calendar_spec
from app.modules.sla.models import SlaOrgSettings, TicketSla
from app.modules.sla.tracking import (
    _load_policy_in_org,
    _load_ticket_in_org,
    _resolution_running,
    _response_running,
)

__all__ = [
    "DEFAULT_AUTO_CLOSE_AFTER_DAYS",
    "SWEEP_INTERVAL_SECONDS",
    "any_clock_running_clause",
    "recompute_for_calendar_change",
    "recompute_for_policy_change",
    "sweep_all_orgs",
    "sweep_org",
]

logger = logging.getLogger("its.sla.sweep")

# The beat interval, in seconds. Defined here rather than as a literal in
# `worker.py`'s `beat_schedule` so the number the module documents and the
# number the scheduler uses cannot drift apart -- `test_sla_sweep.py` asserts
# the beat entry carries this exact value.
#
# Five minutes is a deliberate compromise: a breach notification is useful for
# hours, so a tighter interval buys little, while every cycle is two indexed
# queries per org plus one transaction. It also bounds the window in which the
# late-reply race above can occur.
SWEEP_INTERVAL_SECONDS = 300.0

# §7 / spec §2.6: what applies to an org with no `sla_org_settings` row. The
# column's own `server_default` is 7 as well, but that default only applies to
# a row that EXISTS; most orgs will never have one, and an org that has not
# configured auto-close must still get the documented behaviour rather than
# no auto-close at all.
DEFAULT_AUTO_CLOSE_AFTER_DAYS = 7

Target = Literal["response", "resolution"]

# Declared as parallel maps keyed on the target, never as an
# `if target == "response"` branch -- `breach.py`'s reasoning verbatim. Task
# 5's F1 was a constant column-to-clock pairing, and a query that compared
# `resolution_due_at` while checking `response_met_at` is invisible to any
# test that exercises one target at a time. Keyed once, the pairing is data
# the tests enumerate.
_TARGETS: tuple[Target, ...] = ("response", "resolution")
_DUE_COLUMN: dict[str, InstrumentedAttribute[datetime]] = {
    "response": TicketSla.response_due_at,
    "resolution": TicketSla.resolution_due_at,
}
_MET_COLUMN: dict[str, InstrumentedAttribute[datetime | None]] = {
    "response": TicketSla.response_met_at,
    "resolution": TicketSla.resolution_met_at,
}
_BREACHED_COLUMN: dict[str, InstrumentedAttribute[datetime | None]] = {
    "response": TicketSla.response_breached_at,
    "resolution": TicketSla.resolution_breached_at,
}
_BREACHED_ATTR: dict[str, str] = {
    "response": "response_breached_at",
    "resolution": "resolution_breached_at",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _clock_running_clause(target: Target) -> ColumnElement[bool]:
    """The SQL half of `tracking._response_running`/`_resolution_running`:
    neither stop column set.

    Built from `_MET_COLUMN`/`_BREACHED_COLUMN` rather than by naming the four
    columns again, so the target-to-column pairing has exactly one definition
    in this file (the reason those maps exist at all). The Python predicates
    stay authoritative -- every caller of this clause re-checks each row with
    them -- so this is a NARROWING that must never be tighter than they are,
    which is why it tests only the two columns they test.
    """
    return and_(_MET_COLUMN[target].is_(None), _BREACHED_COLUMN[target].is_(None))


def any_clock_running_clause() -> ColumnElement[bool]:
    """Rows with at least one running clock and a promise that still means
    something -- the row set a recompute can actually move.

    Public because `views.count_stale_ticket_sla` needs the same narrowing on
    a READ path, where loading every historical `ticket_sla` row in the org to
    discard all but the running ones is not acceptable. The recompute
    functions themselves do not narrow in SQL (they select by calendar/policy
    and let `_recompute_row`'s guards decide), which is fine for a write path
    already proportional to the edit.
    """
    return and_(
        TicketSla.voided_at.is_(None),
        or_(_clock_running_clause("response"), _clock_running_clause("resolution")),
    )


def _derive_deadlines(
    spec: CalendarSpec,
    created_at: datetime,
    *,
    response_minutes: int | None,
    resolution_minutes: int | None,
    credit: int,
) -> tuple[datetime | None, datetime | None]:
    """THE DEADLINE FORMULA, in one place (§5's "one formula, one place to be
    wrong").

    `None` in means "that clock is not running, do not derive it", and `None`
    comes back for it. Pause credit is folded into the MINUTES, never added to
    the result as a `timedelta`: a `timedelta` would spend the credit in
    wall-clock and buy back the weekend §5 forbids.

    Extracted so `_recompute_row` (which WRITES the answer) and
    `views.count_stale_ticket_sla` (which only asks whether the stored answer
    still equals it) cannot disagree about what the current configuration
    implies. A staleness measure computed from a second copy of this
    arithmetic would report drift between two implementations rather than
    between the configuration and the rows.
    """
    return (
        None
        if response_minutes is None
        else add_working_minutes(spec, created_at, response_minutes + credit),
        None
        if resolution_minutes is None
        else add_working_minutes(spec, created_at, resolution_minutes + credit),
    )


async def sweep_org(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> dict[str, int]:
    """Run both §7 passes for one org on the caller's session and return the
    counts.

    Returns ``{"breached": n, "auto_closed": n, "failed": n}``:

    * `breached` counts breach columns actually STAMPED, not rows the query
      returned. The two differ -- `mark_breached` is a documented no-op on a
      row whose clock has already stopped -- and counting selections instead
      of writes would make the number grow every cycle on a fleet where
      nothing changed.
    * `auto_closed` counts tickets moved to `closed`.
    * `failed` counts per-ticket units of work that raised and were rolled
      back to their savepoint. A non-zero value is a real fault that has been
      logged with `exc_info` (type only, under this kernel's formatter -- see
      the module docstring above); it is returned rather than only logged so
      tests can assert containment happened rather than asserting the absence
      of an exception (which every broken implementation also satisfies).

    `now` is injectable so a test can place a deadline in the past without
    waiting for it and so both passes share one instant -- a sweep that read
    the clock twice could breach against 10:00:00 and auto-close against
    10:00:01, which is harmless but makes an exact-instant assertion
    impossible. It defaults to the real clock, which is what the beat task
    passes.

    DOES NOT COMMIT, and takes the session rather than opening one: see the
    module docstring's transaction-boundary section. The caller's
    `org_session` block owns the commit; every ticket here owns a savepoint.
    """
    now = now or _utcnow()
    breached, breach_failed = await _breach_pass(session, org_id, now)
    auto_closed, close_failed = await _auto_close_pass(session, org_id, now)
    return {
        "breached": breached,
        "auto_closed": auto_closed,
        "failed": breach_failed + close_failed,
    }


async def _breach_pass(session: AsyncSession, org_id: UUID, now: datetime) -> tuple[int, int]:
    """§7 pass 1. Both targets, in spec order, each in its own query.

    The two clocks stop independently, so a row can be selected by BOTH
    queries in one cycle -- a ticket that answered nobody and fixed nothing
    breaches twice and is stamped twice. That is correct and is not a
    double-escalation: `escalated_at` is the latch, so the second
    `mark_breached` escalates nothing (`breach.py` §10.7).

    Ordered by `TicketSla.id` so a failure's position in the batch is
    reproducible; `uuid7` primary keys make that creation order, which is what
    the containment tests seed against.
    """
    breached = failed = 0
    for target in _TARGETS:
        due = _DUE_COLUMN[target]
        met = _MET_COLUMN[target]
        rows = (
            (
                await session.execute(
                    select(TicketSla)
                    # Joined for the status exclusion below. `Ticket.org_id`
                    # is part of the join predicate because
                    # `ticket_sla.ticket_id` is a plain FK with no org check
                    # (invariant #5) -- an inner join on both also drops a
                    # cross-org row rather than letting it reach
                    # `mark_breached`, which would refuse it anyway.
                    .join(
                        Ticket,
                        (Ticket.id == TicketSla.ticket_id) & (Ticket.org_id == org_id),
                    )
                    .where(
                        TicketSla.org_id == org_id,
                        due <= now,
                        _BREACHED_COLUMN[target].is_(None),
                        # Not `met.is_(None)`: a reply that landed AFTER the
                        # deadline did not keep the promise. See the module
                        # docstring.
                        or_(met.is_(None), met > due),
                        TicketSla.paused_at.is_(None),
                        TicketSla.voided_at.is_(None),
                        # The pre-0038 half of the void guard -- see the
                        # module docstring. NOT redundant with the column
                        # above: nothing backfilled `voided_at`.
                        Ticket.status.not_in(sorted(FINAL_STATUSES)),
                    )
                    .order_by(TicketSla.id)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            # Read BEFORE the savepoint. Rolling one back EXPIRES every
            # attribute the failed unit touched, and re-reading an expired
            # attribute is lazy IO -- which under asyncio raises
            # `MissingGreenlet` from inside the error handler, turning a
            # contained per-ticket failure back into an uncontained one that
            # aborts the org. The same applies to every other identifier this
            # module logs after a rollback.
            row_id = row.id
            stamped = False
            try:
                async with session.begin_nested():
                    await mark_breached(session, org_id, row, target=target)
                    stamped = getattr(row, _BREACHED_ATTR[target]) is not None
            except Exception:  # noqa: BLE001 -- see the module docstring
                failed += 1
                logger.error(
                    "sla.sweep: marking ticket_sla %s breached on target %r failed in org %s; "
                    "rolled back to its savepoint, the rest of the cycle continues",
                    row_id,
                    target,
                    org_id,
                    exc_info=True,
                )
            else:
                breached += int(stamped)
    return breached, failed


async def _auto_close_after_days(session: AsyncSession, org_id: UUID) -> int:
    """This org's configured auto-close window, or the documented default.

    Org-scoped explicitly (invariant #5): `sla_org_settings.org_id` is UNIQUE
    across the whole table, so without the predicate this would return
    whichever org's row the planner happened to hand back first and close
    another tenant's tickets on this tenant's schedule.
    """
    configured = (
        await session.execute(
            select(SlaOrgSettings.auto_close_after_days).where(SlaOrgSettings.org_id == org_id)
        )
    ).scalar_one_or_none()
    return configured if configured is not None else DEFAULT_AUTO_CLOSE_AFTER_DAYS


async def _auto_close_pass(session: AsyncSession, org_id: UUID, now: datetime) -> tuple[int, int]:
    """§7 pass 2: close tickets that have sat in `resolved`/`fulfilled` longer
    than this org's window.

    `resolved_at IS NOT NULL` is explicit rather than implied by the `<`
    comparison. It states the intent -- a ticket whose resolution instant is
    unknown has no measurable age and must NOT be closed on a guess -- where
    the SQL three-valued logic that produces the same result is silent about
    it, and it survives a later rewrite of the comparison.

    Idempotent by construction (§7): a ticket this pass closed is no longer in
    a resolved-like status, so the next cycle's query does not return it. A
    REOPENED ticket is likewise excluded, because `change_status` clears
    `resolved_at` on every reopen edge.
    """
    days = await _auto_close_after_days(session, org_id)
    cutoff = now - timedelta(days=days)
    tickets = (
        (
            await session.execute(
                select(Ticket)
                .where(
                    Ticket.org_id == org_id,
                    Ticket.status.in_(sorted(_RESOLVED_LIKE_STATUSES)),
                    Ticket.resolved_at.is_not(None),
                    Ticket.resolved_at < cutoff,
                )
                .order_by(Ticket.id)
            )
        )
        .scalars()
        .all()
    )

    closed = failed = 0
    for ticket in tickets:
        # Read before the savepoint -- see `_breach_pass`'s note on expiry.
        ticket_id = ticket.id
        ticket_number = ticket.number
        from_status = ticket.status
        resolved_at = ticket.resolved_at
        try:
            async with session.begin_nested():
                await change_status(
                    session,
                    org_id,
                    ticket_id,
                    to_status="closed",
                    actor_id=None,
                    actor_type="system",
                    reason=f"auto-closed after {days} day(s) in {from_status}",
                )
                # §8's "every auto-close" is audited. See the module
                # docstring for why `ticket.status_changed` alone is not it.
                await audit_service.record(
                    session,
                    org_id=org_id,
                    actor_type="system",
                    actor_id=None,
                    action="sla.auto_closed",
                    target_type="ticket",
                    target_id=str(ticket_id),
                    metadata={
                        "ticket_number": ticket_number,
                        "auto_close_after_days": days,
                        "resolved_at": resolved_at.isoformat() if resolved_at else None,
                    },
                )
        except Exception:  # noqa: BLE001 -- see the module docstring
            failed += 1
            logger.error(
                "sla.sweep: auto-closing ticket %s failed in org %s; rolled back to its "
                "savepoint, the rest of the cycle continues",
                ticket_id,
                org_id,
                exc_info=True,
            )
        else:
            closed += 1
    return closed, failed


async def recompute_for_calendar_change(
    session: AsyncSession, org_id: UUID, calendar_id: UUID
) -> dict[str, int]:
    """Re-derive the deadlines of every still-running clock materialised on
    `calendar_id` (§7's "calendar and policy edits recompute affected open
    tickets").

    Returns ``{"recomputed": n, "skipped": n, "failed": n}``.

    A DEADLINE IS A DERIVED VALUE, AND ITS INPUT JUST CHANGED. `ticket_sla`
    stores `response_due_at`/`resolution_due_at` as instants, computed once
    from the calendar as it stood. Move the working windows, add a holiday,
    fix a wrong timezone -- and every stored deadline on that calendar is now
    an answer to a question nobody asked. Without this, an admin correcting a
    calendar leaves the whole existing backlog measured against the broken
    one, and the correction silently applies only to tickets filed after it.

    ONLY RUNNING CLOCKS ARE TOUCHED (§7, and the rule `resume` and
    `recompute_for_retriage` already hold): a met or breached deadline is
    history that reporting reads, and rewriting it would change what was
    promised after the fact -- retroactively turning a breach into a
    non-breach with an admin's calendar edit is exactly the metric laundering
    §0.6.1 refuses elsewhere. The two clocks stop independently, so a row with
    a met response and a running resolution recomputes exactly one of the two.
    A VOIDED row is skipped whole: its promise stopped meaning anything when
    the ticket was cancelled, and re-deriving it is churn on a dead row.

    THE POLICY IS LOADED BY ID, NEVER RE-RESOLVED -- `resume`'s reasoning
    exactly. Re-resolving would perform §0.3's unattributed recompute through
    an unrelated door: a policy change would land on tickets because somebody
    edited a *calendar*, with `policy_source` still reading whatever it read
    before. `policy_id`, `calendar_id` and `policy_source` are therefore left
    alone; only the two deadline columns and `last_recomputed_at` move.

    PAUSE CREDIT IS FOLDED INTO THE `minutes` ARGUMENT, not added to the
    result as a `timedelta` -- character-for-character the formula
    `resume`/`recompute_for_retriage` use (§5). One formula, one place to be
    wrong; a `timedelta` would spend the credit in wall-clock and buy back the
    weekend §5 forbids.

    WIRED (Task 11) into the calendar mutators that can actually move a
    deadline: `calendars.replace_windows`, `calendars.add_holiday`,
    `calendars.remove_holiday`, and `calendars.update_calendar` WHEN it
    changed the timezone -- each calls this inside the same transaction as
    the edit, so the edit and the recompute commit or roll back together.
    Renaming a calendar or toggling its `active` flag does not call it: neither
    is an input to `add_working_minutes`, so the recompute would be guaranteed
    churn. §7's other half is `recompute_for_policy_change` below.

    Per-row containment, per the module docstring: an unsatisfiable calendar,
    a `MAX_LOOKAHEAD_DAYS` overflow or an unknown timezone fails ONE row and
    is counted, never the pass. The rows share a calendar, so a broken
    calendar usually fails all of them -- but a row can fail alone (a large
    `paused_minutes_total` pushes only that row past the horizon), and one
    org's empty calendar must never fail somebody else's tickets.
    """
    rows = (
        (
            await session.execute(
                select(TicketSla)
                .where(TicketSla.org_id == org_id, TicketSla.calendar_id == calendar_id)
                .order_by(TicketSla.id)
            )
        )
        .scalars()
        .all()
    )
    return await _recompute_rows(session, org_id, rows)


async def recompute_for_policy_change(
    session: AsyncSession, org_id: UUID, policy_id: UUID
) -> dict[str, int]:
    """Re-derive the deadlines of every still-running clock materialised under
    `policy_id` -- §7's other half, keyed on the policy instead of the
    calendar.

    Returns ``{"recomputed": n, "skipped": n, "failed": n}``.

    THE ROW ADOPTS THE POLICY'S `calendar_id`. This is the decision Task 10
    recorded and deliberately did not take alone, so it is argued here rather
    than asserted.

    The premise that settles it: a `ticket_sla` row's TARGETS ALREADY TRACK
    THE POLICY ROW, and always have. Nothing copies
    `response_target_minutes` onto `ticket_sla`; `resume`,
    `recompute_for_retriage` and `_recompute_row` all load the policy BY ID
    and read whatever minutes it currently holds. So "an edit to this policy
    changes what an open ticket under it is promised" is not a new behaviour
    this function introduces -- it is the existing behaviour, arriving lazily
    and invisibly on whatever event happens to recompute the row next. What
    this function changes is only WHEN that lands: at the audited admin edit,
    on every affected row at once, instead of at the next unrelated pause.

    Given that, leaving `calendar_id` alone is the incoherent option, not the
    conservative one. A policy repointed from a 24/7 calendar to Mon-Fri
    would otherwise leave open tickets deriving the policy's NEW target on the
    OLD calendar -- a (target, calendar) pair that exists nowhere in the
    org's configuration and that no admin action short of a human re-triage
    can ever reconcile, because `resume` reads `row.calendar_id` too. The row
    would be permanently measured against a calendar its own policy no longer
    names.

    THE COST, STATED: a row with a MET response and a running resolution keeps
    its frozen `response_due_at` while `calendar_id` moves underneath it, so
    that instant is no longer re-derivable from the row's own columns. That is
    not a regression this introduces. The frozen instant already stopped being
    re-derivable the moment the policy's minutes changed -- a stop column
    records WHAT WAS PROMISED as an instant, not a formula, which is exactly
    why §7 freezes it instead of recomputing it.

    THIS DOES NOT REOPEN §0.4's DOOR. §0.4 forbids an ESCALATION repointing
    `policy_id`/`calendar_id`, because that would let a later `resume` apply
    the escalated target to a ticket nobody re-triaged -- the engine reacting
    to its own output, unattributed, with `policy_source` still reading
    `initial`. Every load-bearing part of that is absent here: `policy_id` is
    never written (rows are SELECTED by it, so it cannot change), and
    `policy_source` is never written, so nothing about WHICH policy applies or
    WHY moves. The trigger is a human `sla.admin` edit that
    `policies.update_policy` audits, not a system escalation, and the
    escalation path reaches neither this function nor `update_policy`. A later
    `resume` on an affected row therefore applies the same policy row it would
    have applied anyway -- only the calendar the same target is measured on
    has changed, to the one that policy now actually names.

    Frozen, voided and paused-out rows are untouched, `calendar_id` included:
    the adoption happens inside `_recompute_row`, AFTER the running-clock
    guards, so a row this function skips is not repointed either. History
    keeps the calendar it was really promised under.
    """
    rows = (
        (
            await session.execute(
                select(TicketSla)
                .where(TicketSla.org_id == org_id, TicketSla.policy_id == policy_id)
                .order_by(TicketSla.id)
            )
        )
        .scalars()
        .all()
    )
    return await _recompute_rows(session, org_id, rows, adopt_policy_calendar=True)


async def _recompute_rows(
    session: AsyncSession,
    org_id: UUID,
    rows: Sequence[TicketSla],
    *,
    adopt_policy_calendar: bool = False,
) -> dict[str, int]:
    recomputed = skipped = failed = 0
    for row in rows:
        # Read before the savepoint -- see `_breach_pass`'s note on expiry.
        row_id = row.id
        try:
            async with session.begin_nested():
                changed = await _recompute_row(
                    session, org_id, row, adopt_policy_calendar=adopt_policy_calendar
                )
        except Exception:  # noqa: BLE001 -- see the module docstring
            failed += 1
            logger.error(
                "sla.sweep: recomputing ticket_sla %s failed in org %s; rolled back to its "
                "savepoint, the remaining rows are still recomputed",
                row_id,
                org_id,
                exc_info=True,
            )
        else:
            recomputed += int(changed)
            skipped += int(not changed)
    return {"recomputed": recomputed, "skipped": skipped, "failed": failed}


async def _recompute_row(
    session: AsyncSession, org_id: UUID, row: TicketSla, *, adopt_policy_calendar: bool = False
) -> bool:
    """Re-derive one row's still-running deadlines. Returns whether anything
    was written.

    `adopt_policy_calendar` is set ONLY by `recompute_for_policy_change` (see
    its docstring for the decision and the §0.4 reasoning). It is deliberately
    applied here, below the running-clock guards, so a row this function skips
    keeps the calendar it was actually promised under.
    """
    if row.voided_at is not None:
        return False
    response_running = _response_running(row)
    resolution_running = _resolution_running(row)
    if not response_running and not resolution_running:
        return False

    # Loaded only once a clock is actually running: a broken calendar must not
    # be raised at a row whose deadlines this call would not have touched.
    ticket = await _load_ticket_in_org(session, org_id, row.ticket_id)
    policy = await _load_policy_in_org(session, org_id, row.policy_id)
    if adopt_policy_calendar:
        row.calendar_id = policy.calendar_id
    # The ROW's calendar, not `policy.calendar_id` -- the row's own record of
    # the calendar its deadlines were derived on, which is also what selected
    # it into this batch (`resume` makes the same choice for the same reason).
    # On the policy-edit path the two are the same by the assignment above,
    # which is the point: the deadline is derived on the calendar the row will
    # still be carrying afterwards.
    spec = await load_calendar_spec(session, org_id, row.calendar_id)

    response_due, resolution_due = _derive_deadlines(
        spec,
        ticket.created_at,
        response_minutes=policy.response_target_minutes if response_running else None,
        resolution_minutes=policy.resolution_target_minutes if resolution_running else None,
        credit=row.paused_minutes_total,
    )
    if response_due is not None:
        row.response_due_at = response_due
    if resolution_due is not None:
        row.resolution_due_at = resolution_due
    row.last_recomputed_at = _utcnow()
    await session.flush()
    return True


async def sweep_all_orgs(
    *, engine: AsyncEngine, admin_engine: AsyncEngine, now: datetime | None = None
) -> int:
    """Sweep every org once. Returns the total number of ACTIONS taken --
    breach stamps plus auto-closes -- across all of them.

    Orgs come off the admin (RLS-bypassing) engine and everything else runs
    through `org_session`, so each org's rows are read and written under its
    own tenant, exactly as every other `*_all_orgs` in `worker.py` does.

    ONE ORG PER TRANSACTION. That is the largest unit that can commit: the
    tenant GUC is `SET LOCAL`, so a transaction cannot span two tenants
    without un-pinning the first. It is also the right blast radius -- an org
    whose sweep fails wholesale (a failure in the org's own query or in the
    commit, which is outside any per-ticket savepoint) loses that cycle and
    nothing else, and is retried in `SWEEP_INTERVAL_SECONDS`.

    EVERY ORG IS WRAPPED, defense in depth on top of `sweep_org`'s per-ticket
    savepoints -- `deliver_pending_all_orgs` and
    `backup_verification_sweep_all_orgs`' convention. One org being
    unsweepable must never skip every org queued behind it; on a fleet where
    org #2 of 300 has a broken row, the alternative is that 298 orgs never get
    swept again and the only symptom is a log line.
    """
    async with AsyncSession(admin_engine) as session:
        org_ids = list((await session.execute(select(Organization.id))).scalars().all())

    total = 0
    for org_id in org_ids:
        try:
            async with org_session(org_id, engine=engine) as session:
                counts = await sweep_org(session, org_id, now=now)
        except Exception:  # noqa: BLE001
            logger.error(
                "sla.sweep: sweeping org %s failed; its cycle is skipped and every other "
                "org is still swept",
                org_id,
                exc_info=True,
            )
            continue
        total += counts["breached"] + counts["auto_closed"]
    return total
