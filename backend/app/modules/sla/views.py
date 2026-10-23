"""Read-side projections the admin API needs and no earlier task built
(docs/modules/sla/spec.md §9, §9.1).

Three things live here, and all three exist because a value the API must show
is DERIVED rather than stored.

1. **A ticket's SLA state** (`ticket_sla_state`). The per-ticket read is gated
   on `ticket.read`, not `sla.read` (spec §9): a technician needs to see a
   deadline without holding SLA administration rights.

2. **Whether a target was met, breached, paused or is still running**
   (`target_state`). Spec §9.1: "Breach state is never computed in the
   browser. The server owns whether a deadline was met" -- two
   implementations of the clock would disagree and the browser's would be the
   one the customer quotes back. So the string the UI renders is decided
   here, from the stored columns, once.

3. **THE MISCONFIGURATION SURFACE** (`calendar_health`,
   `count_tickets_without_sla`). See below -- this is the whole reason this
   module exists rather than the API reading columns directly.

---

WHY A MISCONFIGURED CALENDAR NEEDED SURFACING AT ALL.
`tracking.sla_misconfiguration_is_not_fatal` is correct and stays: an
admin's broken calendar must not stop an organisation filing tickets, so
`start_tracking` degrades to spec §3's legitimate "no policy matched -> no
`ticket_sla` row" and logs at ERROR. But a log line on the API host is not a
surface anybody in the ORG can see. The org's own experience of that state is
that everything looks normal, every ticket quietly has no promise attached,
and the first person to find out is a customer asking why their SLA report is
empty. The degradation is right; the silence around it is not.

Two derived reads end it, and they are deliberately different kinds of
answer:

* `calendar_health` names the CAUSE, per calendar, on the screen where it can
  be fixed. `no_working_windows` is precisely the state that makes
  `add_working_minutes` raise `UnsatisfiableCalendar` (spec §4) -- the
  condition the tolerant wrapper swallows -- and `invalid_timezone` is the
  one `test_sla_sweep`'s own fixture proves can exist on a stored row
  (validated at write time, so only a direct DB edit or a tzdata rollback
  produces it, but the sweep then fails every row on that calendar).
* `count_tickets_without_sla` measures the CONSEQUENCE, org-wide, and needs
  no diagnosis to be alarming. It is the number that is 0 in a healthy org
  and grows once an hour in a broken one, and it catches causes neither
  `calendar_health` nor anything else enumerates -- most importantly spec
  §3's "no policy matched", which is not a fault at all until the org
  believes it has configured SLAs.

Neither is exhaustive and neither pretends to be: a calendar whose every
working day inside `MAX_LOOKAHEAD_DAYS` is a holiday is also unsatisfiable
and is NOT reported by `calendar_health`, because deciding that requires
walking the horizon (spec §4's bounded iteration) rather than counting rows.
That case still shows up in `count_tickets_without_sla`, which is the point
of having a consequence measure beside the cause list.

---

AND NEITHER CATCHES A FAILED RECOMPUTE (`count_stale_ticket_sla`, below).

`sweep.recompute_for_{policy,calendar}_change` contain every per-row failure
in a savepoint and COUNT it. That containment is correct -- one row that
cannot be derived must not roll back an admin's whole edit -- but until now
the count went nowhere: `policies.update_policy` and
`calendars._recompute_open_clocks` inspect it only to choose a log level. So
an admin who repoints a policy at a calendar with a corrupted timezone gets a
200 with the new targets, the edit commits, EVERY affected row fails, and the
entire open backlog keeps deadlines derived from the configuration that no
longer exists. The only record is one WARNING line on the API host.

`count_tickets_without_sla` does not see it, and this is the important part:
those rows EXIST. They have a policy, a calendar and two deadlines. They are
merely answers to a question nobody is asking any more, which is invisible to
every measure that counts rows by presence.

WHY THE MEASURE IS A RE-DERIVATION AND NOT A TIMESTAMP COMPARISON. The
obvious cheap form -- "`ticket_sla.last_recomputed_at` predates its policy's
or calendar's `updated_at`" -- does not work here, and the reasons are
properties of the schema rather than preferences:

* `sla_calendar_window` and `sla_calendar_holiday` carry NO timestamp
  columns at all, and `replace_windows`/`add_holiday`/`remove_holiday` never
  touch the parent `sla_calendar` row. The three most common deadline-moving
  calendar edits therefore leave `sla_calendar.updated_at` exactly where it
  was: a timestamp measure would report a healthy org for precisely the edits
  it exists to police.
* `updated_at` moves for edits that CANNOT move a deadline -- a calendar
  rename, a policy `active` toggle. Both are ordinary admin actions that
  deliberately recompute nothing (`calendars.update_calendar`,
  `policies.update_policy`), so every running row would read as stale
  afterwards and stay that way. A number that cannot return to 0 stops being
  read, which is the failure `count_tickets_without_sla` is written to avoid.

Asking the question directly instead -- "would a recompute right now move
this row?" -- has neither failure mode, because it is not a proxy. It is 0
the instant a recompute succeeds, 0 after a rename, and N after a recompute
that failed on N rows.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Imported, never restated: §1 forbids this module owning any part of the
# ticket lifecycle's definition, and a status added to the terminal set on the
# ticket core's side must change this count automatically rather than by
# somebody remembering this file (`sweep.py`'s reasoning for importing
# `_RESOLVED_LIKE_STATUSES`).
from app.core.i18n import N_, tr, tr_marked
from app.modules.service_desk.errors import TicketNotFoundError
from app.modules.service_desk.lifecycle import TERMINAL_STATUSES
from app.modules.service_desk.models import Ticket, TicketQueue
from app.modules.service_desk.tickets import get_ticket
from app.modules.sla.calendar import CalendarSpec
from app.modules.sla.calendars import load_calendar_spec
from app.modules.sla.due import Target, TargetState
from app.modules.sla.errors import SlaNotFoundError
from app.modules.sla.models import SlaCalendar, SlaCalendarWindow, SlaPolicy, TicketSla

# `sweep` does not import this module, so this is not a cycle (the same note
# `settings.py` carries). Both names are reached across the private boundary
# on purpose: `_derive_deadlines` IS the formula `_recompute_row` writes with,
# and a staleness measure computed from a second copy of it would report drift
# between two implementations rather than between the configuration and the
# rows.
from app.modules.sla.sweep import _derive_deadlines, any_clock_running_clause
from app.modules.sla.tracking import _resolution_running, _response_running

__all__ = [
    "CalendarHealth",
    "TargetState",
    "calendar_health",
    "calendar_health_by_id",
    "count_stale_ticket_sla",
    "count_tickets_without_sla",
    "describe_policy",
    "policy_and_calendar_names",
    "target_state",
    "ticket_sla_state",
    "ticket_sla_summaries",
]

CalendarHealth = Literal["ok", "no_working_windows", "invalid_timezone"]


def calendar_health(*, timezone: str, window_count: int) -> CalendarHealth:
    """Why this calendar cannot produce a deadline, or `"ok"`.

    `invalid_timezone` is checked FIRST because it is the more total failure:
    `load_calendar_spec` -> `ZoneInfo(...)` raises before any window is ever
    consulted, so a calendar with both faults is unusable for that reason
    whatever its windows say. `_validate_timezone` makes this unreachable
    through any write path in this module; it is reported anyway because
    `test_sla_sweep`'s recompute fixture demonstrates the state is
    reachable on a stored row, and an unreportable fault is exactly the
    blindness this function exists to end.
    """
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return "invalid_timezone"
    if window_count == 0:
        return "no_working_windows"
    return "ok"


async def calendar_health_by_id(
    session: AsyncSession, org_id: UUID, calendars: list[SlaCalendar]
) -> dict[UUID, CalendarHealth]:
    """`calendar_health` for a whole list, in ONE extra query.

    A per-calendar `load_calendar_spec` would be N+1 on the admin list screen,
    and would also load every holiday for a decision that does not consider
    them. The window counts come back grouped instead; a calendar with no
    window rows is absent from the result and counts as 0, which is exactly
    the `no_working_windows` case.
    """
    if not calendars:
        return {}
    ids = [calendar.id for calendar in calendars]
    rows = (
        await session.execute(
            select(SlaCalendarWindow.calendar_id, func.count())
            .where(
                SlaCalendarWindow.org_id == org_id,
                SlaCalendarWindow.calendar_id.in_(ids),
            )
            .group_by(SlaCalendarWindow.calendar_id)
        )
    ).all()
    counts: dict[UUID, int] = {row[0]: row[1] for row in rows}
    return {
        calendar.id: calendar_health(
            timezone=calendar.timezone, window_count=counts.get(calendar.id, 0)
        )
        for calendar in calendars
    }


async def count_tickets_without_sla(session: AsyncSession, org_id: UUID) -> int:
    """How many NON-TERMINAL tickets in this org have no `ticket_sla` row.

    The consequence measure described in the module docstring: 0 in a healthy
    org, and growing in one whose calendar or policy configuration is broken.

    Terminal tickets (`lifecycle.TERMINAL_STATUSES` -- closed, cancelled,
    rejected) are excluded because nothing can be done about them: no
    re-triage will materialise a row on a ticket nobody will touch again, so
    counting them would turn a number that should return to 0 once an admin
    fixes the calendar into one that never can, and a number that cannot
    reach 0 stops being read.

    Both halves of the NOT EXISTS carry `org_id` (invariant #5): `ticket_sla`
    has no org-aware constraint tying it to its ticket's org, so without the
    inner predicate a foreign tenant's row could satisfy the correlation and
    hide one of this org's SLA-less tickets from its own count.
    """
    has_row = (
        select(TicketSla.id)
        .where(TicketSla.org_id == org_id, TicketSla.ticket_id == Ticket.id)
        .exists()
    )
    return (
        await session.execute(
            select(func.count())
            .select_from(Ticket)
            .where(
                Ticket.org_id == org_id,
                Ticket.status.not_in(sorted(TERMINAL_STATUSES)),
                ~has_row,
            )
        )
    ).scalar_one()


async def count_stale_ticket_sla(session: AsyncSession, org_id: UUID) -> int:
    """How many still-running SLA clocks in this org store a deadline the
    CURRENT configuration would not produce.

    The measure that catches a recompute that failed on every row and reported
    success anyway (module docstring). A row is STALE when at least one of
    these is true, asked of the configuration as it stands right now:

    * its `calendar_id` is not the one its policy names -- the exact residue
      of a `recompute_for_policy_change` that could not land, because
      `_recompute_row` adopts the policy's calendar and a failed row rolls
      back to the calendar it was promised under;
    * a running clock's stored deadline differs from the derived one;
    * or the deadline cannot be derived at all -- a missing ticket or policy,
      a calendar `calendar_health` already flags, or an
      `UnsatisfiableCalendar`/lookahead failure from the arithmetic itself.
      "Cannot be verified" is reported as stale rather than skipped: it is
      exactly the row a recompute would have counted as `failed`, and a
      health measure that quietly excludes the rows it cannot check reports 0
      most confidently when the org is most broken.

    ONLY ROWS A RECOMPUTE COULD MOVE ARE CONSIDERED (`any_clock_running_clause`
    plus the same `tracking` predicates `_recompute_row` guards on). A frozen
    or voided row is not stale, it is HISTORY -- `_recompute_row` deliberately
    leaves it alone, calendar included, so measuring it against today's
    configuration would report a permanent non-zero number for rows that are
    behaving exactly as specified.

    NO N+1. One query for the rows, one for their tickets' `created_at`, one
    for their policies, one for those policies' calendars, then one
    `load_calendar_spec` per DISTINCT healthy calendar. A calendar
    `calendar_health` already reports as broken short-circuits every row on it
    without loading a spec or walking a horizon -- which is also what keeps
    the pathological case cheap, since an unsatisfiable calendar is precisely
    the one where `add_working_minutes` would walk `MAX_LOOKAHEAD_DAYS` before
    raising, once per row.

    Every query carries `org_id` (invariant #5), and the maps are keyed on
    ids that came back from those org-scoped queries -- a `ticket_sla` row
    pointing at a foreign ticket or policy (both are plain FKs with no org
    check, `tracking._load_ticket_in_org`'s reasoning) finds no entry and is
    counted as unverifiable, never derived from another tenant's data.
    """
    rows = list(
        (
            await session.execute(
                select(TicketSla)
                .where(TicketSla.org_id == org_id, any_clock_running_clause())
                .order_by(TicketSla.id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    created_at: dict[UUID, datetime] = dict(
        (
            await session.execute(
                select(Ticket.id, Ticket.created_at).where(
                    Ticket.org_id == org_id,
                    Ticket.id.in_({row.ticket_id for row in rows}),
                )
            )
        ).all()  # type: ignore[arg-type]
    )
    policies: dict[UUID, SlaPolicy] = {
        policy.id: policy
        for policy in (
            await session.execute(
                select(SlaPolicy).where(
                    SlaPolicy.org_id == org_id,
                    SlaPolicy.id.in_({row.policy_id for row in rows}),
                )
            )
        )
        .scalars()
        .all()
    }
    calendars = list(
        (
            await session.execute(
                select(SlaCalendar).where(
                    SlaCalendar.org_id == org_id,
                    SlaCalendar.id.in_({policy.calendar_id for policy in policies.values()}),
                )
            )
        )
        .scalars()
        .all()
    )
    health = await calendar_health_by_id(session, org_id, calendars)

    specs: dict[UUID, CalendarSpec | None] = {}
    stale = 0
    for row in rows:
        policy = policies.get(row.policy_id)
        ticket_created = created_at.get(row.ticket_id)
        if policy is None or ticket_created is None:
            stale += 1
            continue
        # The configuration says the POLICY's calendar applies; a running row
        # whose own `calendar_id` disagrees is one a policy repoint never
        # reached. Checked before the derivation because it is stale whatever
        # the two calendars happen to produce -- two calendars can agree on an
        # instant and still be different answers to "which calendar is this
        # measured on".
        if row.calendar_id != policy.calendar_id:
            stale += 1
            continue
        if health.get(policy.calendar_id, "ok") != "ok":
            stale += 1
            continue
        if policy.calendar_id not in specs:
            try:
                specs[policy.calendar_id] = await load_calendar_spec(
                    session, org_id, policy.calendar_id
                )
            except Exception:  # noqa: BLE001 -- an unloadable calendar is stale, not fatal
                specs[policy.calendar_id] = None
        spec = specs[policy.calendar_id]
        if spec is None:
            stale += 1
            continue

        response_running = _response_running(row)
        resolution_running = _resolution_running(row)
        try:
            response_due, resolution_due = _derive_deadlines(
                spec,
                ticket_created,
                response_minutes=policy.response_target_minutes if response_running else None,
                resolution_minutes=(
                    policy.resolution_target_minutes if resolution_running else None
                ),
                credit=row.paused_minutes_total,
            )
        except Exception:  # noqa: BLE001 -- see the docstring: unverifiable is stale
            stale += 1
            continue
        if response_due is not None and response_due != row.response_due_at:
            stale += 1
        elif resolution_due is not None and resolution_due != row.resolution_due_at:
            stale += 1
    return stale


def target_state(row: TicketSla | None, *, target: Target) -> TargetState:
    """The single server-side answer to "what happened to this promise"
    (spec §9.1).

    PRECEDENCE, and every step of it is a decision rather than an ordering
    convenience:

    * `none` -- no `ticket_sla` row. Spec §3's legitimate state, never a
      fabricated zero or a silent infinity.
    * `void` -- the ticket reached a terminal-not-resolved status
      (`voided_at`, migration 0038). Outranks everything below because a void
      promise is neither kept nor broken.
    * `breached` -- OUTRANKS `met`, and the two genuinely co-occur: a reply
      that lands after the deadline stamps `met_at` on a row whose
      `breached_at` is already set, and (since the late-met widening) can
      stamp `breached_at` on a row that is already met. Reporting that as
      "met" would let any breach be laundered by eventually answering, which
      is the metric laundering §0.6.1 refuses elsewhere.
    * `met`, then `paused`, then `running`. `paused` is below the two stop
      columns because a paused clock that has already been met or breached is
      finished, not stopped -- and §9.1 requires a paused clock to be VISIBLY
      paused, which is why it is a state of its own rather than folded into
      `running`.
    """
    if row is None:
        return "none"
    if row.voided_at is not None:
        return "void"
    breached_at = (
        row.response_breached_at if target == "response" else row.resolution_breached_at
    )
    met_at = row.response_met_at if target == "response" else row.resolution_met_at
    if breached_at is not None:
        return "breached"
    if met_at is not None:
        return "met"
    if row.paused_at is not None:
        return "paused"
    return "running"


async def ticket_sla_state(
    session: AsyncSession, org_id: UUID, ticket_id: UUID
) -> tuple[Ticket, TicketSla | None]:
    """The ticket and its `ticket_sla` row, or `None` for a ticket that has
    none (spec §3 -- an org with no policy configured must visibly have no
    SLA, and a broken calendar degrades to the same state).

    The TICKET is looked up first and a miss is fatal, so "no such ticket in
    this org" (404) stays distinguishable from "this ticket has no SLA" (200
    with an empty state). `service_desk.tickets.get_ticket` is reused rather
    than re-implemented -- it is already the org-scoped load, and it already
    makes a cross-org id indistinguishable from a nonexistent one.

    Its `TicketNotFoundError` is re-raised as this module's `SlaNotFoundError`
    so `api._http_error` has exactly ONE error family to map. Both are 404 and
    the message is unchanged, so this narrows nothing and adds no oracle.
    """
    try:
        ticket = await get_ticket(session, org_id, ticket_id)
    except TicketNotFoundError as exc:
        raise SlaNotFoundError(str(exc)) from exc

    row = (
        await session.execute(
            select(TicketSla).where(TicketSla.org_id == org_id, TicketSla.ticket_id == ticket_id)
        )
    ).scalar_one_or_none()
    return ticket, row


async def ticket_sla_summaries(
    session: AsyncSession, org_id: UUID, ticket_ids: list[UUID]
) -> dict[UUID, TicketSla]:
    """The `ticket_sla` rows for a page of tickets, in one query, keyed by
    ticket. A ticket with no row is absent. The explicit `org_id` predicate
    matters: `ticket_sla.ticket_id` is a plain FK with no org check."""
    if not ticket_ids:
        return {}
    rows = (
        await session.execute(
            select(TicketSla).where(
                TicketSla.org_id == org_id, TicketSla.ticket_id.in_(ticket_ids)
            )
        )
    ).scalars().all()
    return {row.ticket_id: row for row in rows}


_PRIORITY_NOUNS = {
    "low": N_("Low priority tickets"),
    "normal": N_("Normal priority tickets"),
    "high": N_("High priority tickets"),
    "urgent": N_("Urgent tickets"),
}


def describe_policy(*, queue_name: str | None, priority: str | None) -> str:
    """A policy has no name column; it is named by what it matches. The console
    prints this as it arrives (`policy_name`), so it is worded in the request's
    language; it is computed on every read and never stored."""
    if priority is None:
        who = tr("Every ticket")
    elif priority in _PRIORITY_NOUNS:
        who = tr_marked(_PRIORITY_NOUNS[priority])
    else:
        who = tr("{priority} priority tickets", priority=priority.capitalize())
    if queue_name is not None:
        return tr("{who} in the {queue_name} queue", who=who, queue_name=queue_name)
    return who if priority is None else tr("{who} in any queue", who=who)


async def policy_and_calendar_names(
    session: AsyncSession, org_id: UUID, row: TicketSla
) -> tuple[str | None, str | None]:
    """The policy's description and the calendar's name for one clock, in one
    query. Every joined table is org-scoped explicitly: none of these FKs
    carries an org check."""
    found = (
        await session.execute(
            select(SlaPolicy.priority, TicketQueue.name, SlaCalendar.name)
            .select_from(SlaPolicy)
            .outerjoin(
                TicketQueue,
                (TicketQueue.id == SlaPolicy.queue_id) & (TicketQueue.org_id == org_id),
            )
            .outerjoin(
                SlaCalendar,
                (SlaCalendar.id == row.calendar_id) & (SlaCalendar.org_id == org_id),
            )
            .where(SlaPolicy.org_id == org_id, SlaPolicy.id == row.policy_id)
        )
    ).one_or_none()
    if found is None:
        return None, None
    priority, queue_name, calendar_name = found
    return describe_policy(queue_name=queue_name, priority=priority), calendar_name
