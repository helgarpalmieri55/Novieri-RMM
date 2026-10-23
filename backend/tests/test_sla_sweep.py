"""The breach sweep, auto-close, and the calendar-edit recompute
(docs/modules/sla/spec.md §7, §8, §10.11, §10.12, §10.13; Task 10).

THE PARAMETER EVERY FIXTURE IN THIS FILE DELIBERATELY VARIES. Every task in
this sub-project has shipped at least one test that proved nothing, always the
same way: a fixture held constant the parameter a defect would vary, so the
guarded and unguarded implementations produced identical observable values and
the assertion passed against the bug it existed to catch. The worst instance
was Task 9's cascade guard -- a single wildcard policy meant a recompute
re-resolved to the SAME policy and produced the SAME deadlines.

For the sweep the candidates are named, and each has a fixture that varies it:

* **Which target breached.** `test_only_the_target_whose_clock_is_still_
  running_is_breached` puts a met response BESIDE a due, unmet resolution on
  ONE row and asserts exactly one column moves. A per-ROW predicate (rather
  than per-target) breaches both or neither; a mismatched column-to-clock
  pairing -- Task 5's F1, the reason `sweep.py` keys three parallel maps off
  `target` -- breaches the wrong one.
* **Whether a row is paused / met / voided / already breached.** Each
  ineligible row is seeded ALONGSIDE an eligible one in the SAME org and swept
  in the SAME call, so "the sweep did nothing" cannot pass: the eligible row
  must be breached and the ineligible one must not. Dropping any one filter
  turns 1 breach into 2.
* **How many orgs are in play.** The isolation test seeds two orgs with
  identical due rows and sweeps ONE, pinned on the BYPASSRLS admin engine so
  the `org_id` predicate is the only guard under test. With one org in the
  fixture, an implementation with no tenancy predicate at all passes.
* **Whether escalation is enabled.** The idempotency fixture enables
  `escalate_priority`, so "one escalation" is a real 1 rather than a vacuous
  0 == 0, and asserts the priority actually moved one rung.
* **Whether the ticket is terminal, INDEPENDENTLY of whether its promise is
  voided.** The void guard is two filters -- a `voided_at IS NULL` column
  check and a joined `status NOT IN FINAL_STATUSES` -- and they overlap on
  every row real code produces, so a fixture that set both would pin neither.
  Each therefore has a fixture only IT can catch: a row voided WITHOUT a
  terminal status, and a terminal ticket WITHOUT `voided_at` (the pre-0038
  row that migration 0038 deliberately did not backfill).
* **The per-org auto-close window.** Three orgs at 3 days, 30 days and NO
  settings row (the default 7), with identically aged resolved tickets, so an
  implementation that hardcodes 7 -- or ignores the setting entirely -- fails
  on the 30-day org. A single-org fixture cannot catch either.
* **Whether a clock is running, in the recompute.** §10.13's fixture edits the
  calendar to one that produces DIFFERENT instants (Mon-Fri 09:00-17:00 ->
  09:00-13:00), and asserts the frozen deadlines equal the OLD values while
  the running ones equal the NEW ones. An edit that produced the same instants
  is the Task 9 trap exactly: a mutant that rewrites met/breached deadlines
  would be invisible.
* **Whether the batch contains a row belonging to somebody else, in the
  recompute.** Both recompute functions select on `calendar_id`/`policy_id` --
  per-row UUIDs -- so a NEIGHBOUR ORG WITH ITS OWN IDENTICALLY-SHAPED CALENDAR
  varies nothing: its rows name a different calendar and a missing `org_id`
  predicate could never reach them. The only fixture that varies the parameter
  is a `ticket_sla` row that BELONGS to another org while naming THIS org's
  calendar and policy (`_foreign_row` below) -- a shape the FKs permit,
  because they have no org concept. Pinned on the BYPASSRLS admin engine, like
  the sweep's own isolation test, because through `org_session` RLS masks the
  missing predicate and the test passes against it.

TIME IS INJECTED, not slept for: `sweep_org(..., now=SWEEP_AT)` places the
deadlines in the past, and `breach._utcnow` is frozen so the stamped instant
is exact and a re-stamp is distinguishable from a first stamp.
"""

import inspect
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings, override_settings
from app.core.tenancy import org_session

# `app.models` first, before any `app.modules.*` import -- see
# `test_sla_breach.py`'s note on ORM registry initialisation order.
from app.models import AuditEvent, Ticket, TicketStatusHistory
from app.modules.sla import breach as breach_module
from app.modules.sla import sweep as sweep_module
from app.modules.sla.models import (
    SlaCalendar,
    SlaCalendarWindow,
    SlaOrgSettings,
    SlaPolicy,
    TicketSla,
)
from app.modules.sla.sweep import (
    DEFAULT_AUTO_CLOSE_AFTER_DAYS,
    SWEEP_INTERVAL_SECONDS,
    recompute_for_calendar_change,
    recompute_for_policy_change,
    sweep_all_orgs,
    sweep_org,
)
from tests.test_sla_breach import _events, _seed_ticket, _ticket_row, _track
from tests.test_sla_migration import _seed_calendar, _seed_policy, _seed_queue, _seed_user
from tests.test_sla_tracking import _ALWAYS, LONDON, MONDAY_0900, _row, _stop_clocks

# The instant every sweep in this file runs at: a month after the tickets are
# filed, so every 60/480-minute deadline on the 24/7 fixture calendar is
# comfortably past due, and far enough from `MONDAY_0900` that a resolved-at
# ten days before it still lands AFTER the ticket was created.
SWEEP_AT = MONDAY_0900 + timedelta(days=30)
# A second, later instant used to prove a second sweep does NOT move a stamp.
LATER = SWEEP_AT + timedelta(days=1)

# Mon-Fri 09:00-17:00 (480 working minutes/day) and the edited calendar that
# replaces it in §10.13: Mon-Fri 09:00-13:00 (240/day). The two produce
# DIFFERENT instants for the same targets, which is what makes the
# running-clock guard testable at all.
_MON_FRI_9_TO_5 = [(d, time(9, 0), time(17, 0)) for d in range(5)]
_MON_FRI_9_TO_1 = [(d, time(9, 0), time(13, 0)) for d in range(5)]

# Targets chosen so both deadlines move when the windows are halved, and so
# neither lands on a window boundary where "the deadline is that instant" and
# "the next window's open" would be indistinguishable.
_RECOMPUTE_RESPONSE_MINUTES = 300  # 5 working hours
_RECOMPUTE_RESOLUTION_MINUTES = 600  # 10 working hours

# Under Mon-Fri 09:00-17:00 from Mon 2026-03-02 09:00 UTC.
_OLD_RESPONSE_DUE = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
_OLD_RESOLUTION_DUE = datetime(2026, 3, 3, 11, 0, tzinfo=UTC)
# Under Mon-Fri 09:00-13:00 from the same start.
_NEW_RESPONSE_DUE = datetime(2026, 3, 3, 10, 0, tzinfo=UTC)
_NEW_RESOLUTION_DUE = datetime(2026, 3, 4, 11, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _sweep(api_env, org_id: UUID, *, now: datetime = SWEEP_AT) -> dict[str, int]:
    """One sweep of one org through the RLS-constrained app engine, committed
    -- the production path (`sweep_all_orgs` opens exactly this)."""
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        return await sweep_org(session, org_id, now=now)


async def _sweep_on_admin_engine(
    api_env, org_id: UUID, *, now: datetime = SWEEP_AT
) -> dict[str, int]:
    """The same sweep on the BYPASSRLS admin engine, where RLS masks nothing
    and each query's own `org_id` predicate is the sole tenancy guard
    (`test_sla_policies.py`'s discipline)."""
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            return await sweep_org(session, org_id, now=now)


async def _set_columns(api_env, ticket_id: UUID, **values: Any) -> None:
    """Set `ticket_sla` columns directly on the admin engine. Used only for
    states whose OWN service is not what this file is testing (`paused_at`,
    `voided_at`, `paused_minutes_total`), the same way `_stop_clocks` sets the
    four stop columns."""
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            row = (
                await session.execute(select(TicketSla).where(TicketSla.ticket_id == ticket_id))
            ).scalar_one()
            for key, value in values.items():
                setattr(row, key, value)


async def _resolve_directly(api_env, ticket_id: UUID, *, status: str, at: datetime) -> None:
    """Put a ticket into a resolved-like status with a chosen `resolved_at`.

    Written directly rather than through `change_status` because the instant
    is the parameter under test: auto-close keys on how OLD the resolution is,
    and a real transition would stamp `resolved_at` at wall-clock now, which
    is never older than any org's window.
    """
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            ticket = (
                await session.execute(select(Ticket).where(Ticket.id == ticket_id))
            ).scalar_one()
            ticket.status = status
            ticket.resolved_at = at


async def _seed_settings(api_env, org_id: UUID, days: int) -> None:
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            session.add(
                SlaOrgSettings(
                    org_id=org_id,
                    auto_close_after_days=days,
                    created_by=await _seed_user(
                        api_env, org_id, email=f"sla-settings-{uuid4().hex[:10]}@test.local"
                    ),
                )
            )


async def _replace_windows(
    api_env, org_id: UUID, calendar_id: UUID, windows: list[tuple[int, time, time]]
) -> None:
    """The calendar edit §10.13 is about, applied directly on the admin engine
    -- `calendars.replace_windows` belongs to Task 3 and is not what this file
    is testing."""
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            await session.execute(
                delete(SlaCalendarWindow).where(SlaCalendarWindow.calendar_id == calendar_id)
            )
            for weekday, start, end in windows:
                session.add(
                    SlaCalendarWindow(
                        org_id=org_id,
                        calendar_id=calendar_id,
                        weekday=weekday,
                        start_time=start,
                        end_time=end,
                    )
                )


async def _set_timezone(api_env, calendar_id: UUID, timezone: str) -> None:
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            calendar = (
                await session.execute(select(SlaCalendar).where(SlaCalendar.id == calendar_id))
            ).scalar_one()
            calendar.timezone = timezone


async def _audit(api_env, org_id: UUID, action: str) -> list[AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def _history(api_env, ticket_id: UUID) -> list[TicketStatusHistory]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(TicketStatusHistory)
                    .where(TicketStatusHistory.ticket_id == ticket_id)
                    .order_by(TicketStatusHistory.id)
                )
            )
            .scalars()
            .all()
        )


async def _breach_org(
    api_env,
    *,
    escalate_priority: bool = False,
    escalate_to_queue_id: UUID | None = None,
    response_minutes: int = 60,
    resolution_minutes: int = 480,
) -> tuple[UUID, UUID]:
    """An org with a 24/7 calendar (working time == elapsed time, so every
    deadline is checkable by eye) and one wildcard policy. Returns
    `(org_id, calendar_id)`."""
    org_id = await api_env.create_org(f"sweep-{uuid4().hex[:8]}")
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        response_minutes=response_minutes,
        resolution_minutes=resolution_minutes,
        escalate_priority=escalate_priority,
        escalate_to_queue_id=escalate_to_queue_id,
    )
    return org_id, calendar_id


@pytest.fixture(autouse=True)
def _frozen_breach_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every breach stamp in this file lands on `SWEEP_AT`, so "stamped" and
    "re-stamped" are distinguishable in the idempotency assertions."""
    monkeypatch.setattr(breach_module, "_utcnow", lambda: SWEEP_AT)


# ---------------------------------------------------------------------------
# breach detection: which rows are selected, and which are not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("due_target", ["response", "resolution"])
async def test_only_the_target_whose_clock_is_still_running_is_breached(
    api_env, due_target: str
) -> None:
    """ONE row, ONE call, two targets in opposite states.

    The met stamp is placed BEFORE the deadline it belongs to, so it is a
    promise genuinely kept on time and must not be breached; the other clock
    is past due and untouched and must be. A predicate evaluated per ROW
    rather than per TARGET breaches both or neither, and a mismatched
    column-to-clock pairing (Task 5's F1) breaches the wrong one -- both are
    invisible to a fixture that exercises one target at a time.
    """
    met_target = "resolution" if due_target == "response" else "response"
    org_id, _ = await _breach_org(api_env)
    ticket = await _seed_ticket(api_env, org_id)
    await _track(api_env, org_id, ticket)
    # A minute after creation: before BOTH the 60- and the 480-minute
    # deadline, so this is unambiguously "kept on time".
    await _stop_clocks(
        api_env, ticket.id, **{f"{met_target}_met_at": MONDAY_0900 + timedelta(minutes=1)}
    )

    counts = await _sweep(api_env, org_id)

    assert counts == {"breached": 1, "auto_closed": 0, "failed": 0}
    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert getattr(stored, f"{due_target}_breached_at") == SWEEP_AT
    assert getattr(stored, f"{met_target}_breached_at") is None
    events = await _events(api_env, org_id, "ticket.sla_breached")
    assert [event.payload["target"] for event in events] == [due_target]


async def test_a_paused_row_past_its_deadline_is_not_breached(api_env) -> None:
    """§5/§7: `paused_at` means the clock is stopped RIGHT NOW, so a deadline
    that has drifted past while the ticket waits on a third party is not a
    missed promise.

    The unpaused ticket beside it is what makes this bite: without the
    `paused_at IS NULL` filter this org breaches TWO rows, not zero, so the
    assertion cannot be satisfied by a sweep that simply does nothing.
    `mark_breached` does not check pause itself -- it marks the row it is
    handed -- so this predicate is the only thing standing between a paused
    ticket and a breach notification.
    """
    org_id, _ = await _breach_org(api_env)
    paused = await _seed_ticket(api_env, org_id)
    running = await _seed_ticket(api_env, org_id)
    await _track(api_env, org_id, paused)
    await _track(api_env, org_id, running)
    await _set_columns(api_env, paused.id, paused_at=MONDAY_0900 + timedelta(minutes=5))

    counts = await _sweep(api_env, org_id)

    # Two targets on the one running row.
    assert counts["breached"] == 2
    assert counts["failed"] == 0
    paused_row = await _row(api_env, paused.id)
    assert paused_row is not None
    assert paused_row.response_breached_at is None
    assert paused_row.resolution_breached_at is None
    running_row = await _row(api_env, running.id)
    assert running_row is not None
    assert running_row.response_breached_at == SWEEP_AT
    assert running_row.resolution_breached_at == SWEEP_AT
    assert len(await _events(api_env, org_id, "ticket.sla_breached")) == 2


async def test_a_voided_promise_is_not_breached(api_env) -> None:
    """The `voided_at IS NULL` half of the void guard, pinned on the ONLY
    fixture that isolates it.

    `voided_at` (migration 0038) is what stops the clocks on a cancelled or
    rejected ticket -- the four stop columns stay NULL deliberately, because
    voiding must neither launder a real breach nor claim the promise was kept.
    Without it every cancelled ticket in the fleet breaches with certainty
    once its deadline passes: at a 5-10% cancellation rate, roughly 0.1-0.2
    phantom `ticket.sla_breached` events per ticket ever filed, each
    indistinguishable in reporting from a promise the team really missed.

    THE VOIDED TICKET HERE IS NOT TERMINAL, and that is the whole point of the
    fixture. The query carries a SECOND guard -- a joined
    `status NOT IN FINAL_STATUSES` -- which would exclude a cancelled ticket
    on its own, so a fixture that voided a cancelled ticket would pass with
    the column filter deleted and prove nothing about it. Voided-without-a-
    terminal-status is reachable only through data drift, which is exactly why
    it is the state that isolates the column. The terminal-without-`voided_at`
    case -- the one only the JOIN catches -- is the next test.

    Escalation is enabled, so a phantom breach would also re-prioritise a
    ticket nobody will ever work again, and the healthy row beside it is what
    turns "breached 2" into "breached 4" when the guard is removed.
    """
    org_id, _ = await _breach_org(api_env, escalate_priority=True)
    voided = await _seed_ticket(api_env, org_id, status="open")
    healthy = await _seed_ticket(api_env, org_id)
    await _track(api_env, org_id, voided)
    await _track(api_env, org_id, healthy)
    await _set_columns(api_env, voided.id, voided_at=MONDAY_0900 + timedelta(minutes=10))

    counts = await _sweep(api_env, org_id)

    assert counts["breached"] == 2  # the healthy row's two targets
    assert counts["failed"] == 0
    voided_row = await _row(api_env, voided.id)
    assert voided_row is not None
    assert voided_row.response_breached_at is None
    assert voided_row.resolution_breached_at is None
    assert voided_row.escalated_at is None
    assert (await _ticket_row(api_env, voided.id)).priority == "normal"
    events = await _events(api_env, org_id, "ticket.sla_breached")
    assert {event.payload["ticket_id"] for event in events} == {str(healthy.id)}


@pytest.mark.parametrize(
    ("terminal_status", "type_"), [("cancelled", "incident"), ("rejected", "request")]
)
async def test_a_terminal_ticket_predating_the_voided_at_column_is_not_breached(
    api_env, terminal_status: str, type_: str
) -> None:
    """The JOIN half of the void guard: a terminal ticket whose `ticket_sla`
    row has `voided_at IS NULL`.

    THIS IS NOT A HYPOTHETICAL ROW. Migration 0038 added `voided_at` and
    deliberately did not backfill it -- it refuses to fabricate a past instant
    for tickets cancelled before the column existed -- so EVERY pre-0038
    cancelled or rejected ticket in an existing deployment is in exactly this
    state. The column filter excludes none of them, and the very first sweep
    after this task ships would emit one phantom breach per historical
    cancelled ticket, each one taking the escalation path on any policy that
    enables it. That is the sweep-abort the guard exists to prevent, arriving
    on day one at production scale.

    Both statuses are exercised because they belong to different ticket types
    (`ck_ticket_status_matches_type`): an incident can reach `cancelled` and
    only a request can reach `rejected`, so an incident-only fixture cannot
    see half of `FINAL_STATUSES`.
    """
    org_id, _ = await _breach_org(api_env, escalate_priority=True)
    terminal = await _seed_ticket(api_env, org_id, status=terminal_status, type_=type_)
    healthy = await _seed_ticket(api_env, org_id)
    stored_terminal = await _track(api_env, org_id, terminal)
    await _track(api_env, org_id, healthy)
    # The pre-0038 state: terminal ticket, promise never voided.
    assert stored_terminal.voided_at is None

    counts = await _sweep(api_env, org_id)

    assert counts["breached"] == 2  # the healthy row's two targets
    assert counts["failed"] == 0
    terminal_row = await _row(api_env, terminal.id)
    assert terminal_row is not None
    assert terminal_row.response_breached_at is None
    assert terminal_row.resolution_breached_at is None
    assert terminal_row.escalated_at is None
    assert (await _ticket_row(api_env, terminal.id)).priority == "normal"
    events = await _events(api_env, org_id, "ticket.sla_breached")
    assert {event.payload["ticket_id"] for event in events} == {str(healthy.id)}


async def test_a_reply_that_landed_after_the_deadline_is_still_a_candidate(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The predicate compares `met_at` to `due_at`, not to NULL (§7's
    order-independence).

    A reply four minutes into a deadline that had already passed did not keep
    the promise, and `met_at IS NULL` alone would exclude that row forever --
    making attainment depend on whether the technician replied before or after
    the beat happened to fire. The two rows here differ ONLY in whether the
    met stamp precedes or follows the deadline, which is exactly the parameter
    the naive predicate holds constant.

    Asserted at the SELECTION boundary, on what the sweep hands to
    `breach.mark_breached`, because that is the boundary this test owns.
    `test_a_late_reply_is_breached_once_and_then_stops_re_selecting` below
    asserts the other half -- that the selected row is actually STAMPED and
    then leaves the candidate set -- and the two are deliberately separate:
    for one release the selection was widened while the stamping guard was
    not, so every late-met row was selected, handed over, silently skipped,
    and selected again 300 seconds later, forever. This test passed
    throughout. A single end-to-end test would not distinguish "never
    selected" from "selected and never stamped", which are different bugs in
    different files.
    """
    org_id, _ = await _breach_org(api_env)
    met_on_time = await _seed_ticket(api_env, org_id)
    met_late = await _seed_ticket(api_env, org_id)
    await _track(api_env, org_id, met_on_time)
    await _track(api_env, org_id, met_late)
    # Response deadline is MONDAY_0900 + 60 minutes on the 24/7 calendar.
    await _stop_clocks(
        api_env,
        met_on_time.id,
        response_met_at=MONDAY_0900 + timedelta(minutes=59),
        resolution_met_at=MONDAY_0900 + timedelta(minutes=59),
    )
    await _stop_clocks(
        api_env,
        met_late.id,
        response_met_at=MONDAY_0900 + timedelta(minutes=61),
        resolution_met_at=MONDAY_0900 + timedelta(minutes=61),
    )

    handed: list[tuple[UUID, str]] = []

    async def _record(session, org, row, *, target):  # type: ignore[no-untyped-def]
        handed.append((row.ticket_id, target))

    monkeypatch.setattr(sweep_module, "mark_breached", _record)
    await _sweep(api_env, org_id)

    # The resolution deadline is MONDAY_0900 + 480 minutes, so the 61-minute
    # stamp is EARLY for that clock and LATE only for the response one --
    # which is also why a per-row rather than per-target comparison fails
    # here.
    assert handed == [(met_late.id, "response")]


async def test_a_late_reply_is_breached_once_and_then_stops_re_selecting(api_env) -> None:
    """THE PROPERTY THAT WAS BROKEN, ASSERTED ON COUNTS ACROSS TWO SWEEPS.

    A ticket whose only reply landed after the response deadline, through the
    real sweep, with nothing monkeypatched. The first sweep must breach it;
    the second must find nothing left to do. Those two numbers together are
    the whole fix, and neither is sufficient alone:

    * `first["breached"] == 1` fails if `mark_breached` still guards on
      "is this clock running" -- the row is selected and skipped, so the
      count is 0 and no `ticket.sla_breached` event is ever published.
    * `second["breached"] == 0` fails if the row is stamped but the sweep
      keeps re-selecting it, which is the loop the guard mismatch produced
      and the reason it was survivable in production: nothing errored, the
      sweep just did the same futile work every 300 seconds forever.

    `breached` counts columns actually STAMPED rather than rows returned by
    the query (`sweep_org`'s docstring), which is exactly what makes the pair
    of counts able to tell selection and stamping apart.

    THE PARAMETER THIS FIXTURE VARIES, beside the late row: a second ticket
    whose reply landed one minute EARLY sits in the same org and is swept in
    the same call. Without it, "breached 1, then 0" is also what an
    implementation that breaches every met row on its first sight produces --
    the on-time row must survive both sweeps unbreached for the count to mean
    "the late one, and only the late one".

    ESCALATION IS ENABLED, so the `escalated_at` latch is exercised on a
    late-met row too: the ticket's priority moves one rung, once, and the
    second sweep must not move it again. With escalation off the assertion
    would be a vacuous 0 == 0.

    The second sweep runs at a LATER instant with `breach._utcnow` still
    frozen at `SWEEP_AT` (the autouse fixture), so a re-stamp would be caught
    by the count even where it could not be caught by the value.
    """
    org_id, _ = await _breach_org(api_env, escalate_priority=True)
    replied_late = await _seed_ticket(api_env, org_id, priority="normal")
    replied_on_time = await _seed_ticket(api_env, org_id, priority="normal")
    await _track(api_env, org_id, replied_late)
    await _track(api_env, org_id, replied_on_time)
    # The 24/7 fixture calendar makes working time elapsed time, so the
    # response deadline is MONDAY_0900 + 60 minutes and the resolution one
    # MONDAY_0900 + 480. Both tickets stop BOTH clocks, one minute either side
    # of the response deadline -- so for the resolution clock both stamps are
    # hundreds of minutes early and neither ticket may breach on it.
    late_met_at = MONDAY_0900 + timedelta(minutes=61)
    on_time_met_at = MONDAY_0900 + timedelta(minutes=59)
    await _stop_clocks(
        api_env, replied_late.id, response_met_at=late_met_at, resolution_met_at=late_met_at
    )
    await _stop_clocks(
        api_env,
        replied_on_time.id,
        response_met_at=on_time_met_at,
        resolution_met_at=on_time_met_at,
    )

    first = await _sweep(api_env, org_id)
    second = await _sweep(api_env, org_id, now=LATER)

    assert first == {"breached": 1, "auto_closed": 0, "failed": 0}
    assert second == {"breached": 0, "auto_closed": 0, "failed": 0}

    late_row = await _row(api_env, replied_late.id)
    assert late_row is not None
    # Met AND breached: `met_at` says when contact happened, `breached_at`
    # says the promise was broken. Neither is derivable from the other.
    assert late_row.response_met_at == late_met_at
    assert late_row.response_breached_at == SWEEP_AT
    assert late_row.resolution_breached_at is None, "the resolution reply was 419 minutes early"

    on_time_row = await _row(api_env, replied_on_time.id)
    assert on_time_row is not None
    assert on_time_row.response_breached_at is None
    assert on_time_row.resolution_breached_at is None

    events = await _events(api_env, org_id, "ticket.sla_breached")
    assert [event.payload["ticket_id"] for event in events] == [str(replied_late.id)]
    assert [event.payload["target"] for event in events] == ["response"]
    assert len(await _audit(api_env, org_id, "sla.escalated")) == 1
    assert (await _ticket_row(api_env, replied_late.id)).priority == "high"
    assert (await _ticket_row(api_env, replied_on_time.id)).priority == "normal"


async def test_the_sweep_is_idempotent(api_env) -> None:
    """§10.11: running the sweep twice produces ONE breach record per target,
    ONE event per target, and ONE escalation -- asserted on counts, never on
    the absence of an exception.

    Escalation is ENABLED and the ticket starts at `normal`, so "one
    escalation" is a real 1 and the priority visibly moves one rung; with
    escalation off the assertion would be a vacuous 0 == 0. Both clocks are
    due, which is the sharper case for the `escalated_at` latch: the two
    targets breach independently and must still escalate the ticket once
    between them.

    The second sweep runs at a LATER frozen instant, so a re-stamp would move
    the recorded value rather than merely repeating it.
    """
    org_id, _ = await _breach_org(api_env, escalate_priority=True)
    ticket = await _seed_ticket(api_env, org_id, priority="normal")
    await _track(api_env, org_id, ticket)

    first = await _sweep(api_env, org_id)
    assert first == {"breached": 2, "auto_closed": 0, "failed": 0}

    second = await _sweep(api_env, org_id, now=LATER)
    assert second == {"breached": 0, "auto_closed": 0, "failed": 0}

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.response_breached_at == SWEEP_AT
    assert stored.resolution_breached_at == SWEEP_AT
    assert stored.escalated_at == SWEEP_AT
    assert len(await _events(api_env, org_id, "ticket.sla_breached")) == 2
    assert len(await _audit(api_env, org_id, "sla.escalated")) == 1
    assert (await _ticket_row(api_env, ticket.id)).priority == "high"


# ---------------------------------------------------------------------------
# per-ticket containment (the two known raising paths, and the org's cycle)
# ---------------------------------------------------------------------------


async def test_an_off_ladder_priority_fails_one_ticket_and_not_the_cycle(api_env) -> None:
    """`ticket.priority` carries no DB CHECK (only `sla_policy.priority`
    does), so a value outside `PRIORITY_LADDER` is reachable, and
    `breach._next_priority` refuses to guess what one step above `p1` is.

    That raise happens AFTER `mark_breached` has already flushed the breach
    stamp and published the event, so containment has to be a savepoint rather
    than a `try`/`except`: the poisoned ticket must end with NO stamp and NO
    event, and the healthy ticket beside it must still be committed. Without
    the savepoint the exception unwinds `org_session`, which rolls back the
    whole org -- and since the input has not changed, the identical failure
    repeats every SWEEP_INTERVAL_SECONDS forever, so the org never breaches
    anything again.

    The poisoned ticket is seeded FIRST, and the sweep orders by `id` (uuid7,
    i.e. creation order), so the healthy row is processed after the failure
    rather than before it.
    """
    org_id, _ = await _breach_org(api_env, escalate_priority=True)
    poisoned = await _seed_ticket(api_env, org_id, priority="p1")
    healthy = await _seed_ticket(api_env, org_id, priority="normal")
    await _track(api_env, org_id, poisoned)
    await _track(api_env, org_id, healthy)

    counts = await _sweep(api_env, org_id)

    # One failure per target on the poisoned row, two stamps on the healthy.
    assert counts == {"breached": 2, "auto_closed": 0, "failed": 2}
    poisoned_row = await _row(api_env, poisoned.id)
    assert poisoned_row is not None
    assert poisoned_row.response_breached_at is None
    assert poisoned_row.resolution_breached_at is None
    healthy_row = await _row(api_env, healthy.id)
    assert healthy_row is not None
    assert healthy_row.response_breached_at == SWEEP_AT
    assert healthy_row.resolution_breached_at == SWEEP_AT
    events = await _events(api_env, org_id, "ticket.sla_breached")
    assert {event.payload["ticket_id"] for event in events} == {str(healthy.id)}


async def test_a_cross_org_escalation_queue_fails_one_ticket_and_not_the_cycle(
    api_env,
) -> None:
    """`sla_policy.escalate_to_queue_id` is a plain FK with no org check, so it
    can legally name another tenant's queue (invariant #5) and
    `refdata.resolve_queue` raises `TicketNotFoundError` -- a
    `TicketValidationError`, from a different exception hierarchy than the
    off-ladder priority's `SlaValidationError`.

    Both rows here sit under that one broken policy, so the failure is the
    org's whole breach pass; what must survive is the AUTO-CLOSE pass, which
    is in the same transaction and has nothing to do with the escalation
    queue. That is the containment this test pins: a raise in pass 1 must not
    cost pass 2, and no ticket may end up half-marked.
    """
    other_org = await api_env.create_org(f"sweep-foreign-{uuid4().hex[:8]}")
    foreign_queue_id = await _seed_queue(api_env, other_org)
    org_id, _ = await _breach_org(
        api_env, escalate_priority=True, escalate_to_queue_id=foreign_queue_id
    )
    breaching = await _seed_ticket(api_env, org_id)
    await _track(api_env, org_id, breaching)
    stale = await _seed_ticket(api_env, org_id, status="new")
    await _resolve_directly(
        api_env, stale.id, status="resolved", at=SWEEP_AT - timedelta(days=10)
    )

    counts = await _sweep(api_env, org_id)

    assert counts == {"breached": 0, "auto_closed": 1, "failed": 2}
    stored = await _row(api_env, breaching.id)
    assert stored is not None
    assert stored.response_breached_at is None
    assert stored.resolution_breached_at is None
    assert stored.escalated_at is None
    assert (await _ticket_row(api_env, breaching.id)).priority == "normal"
    assert await _events(api_env, org_id, "ticket.sla_breached") == []
    assert (await _ticket_row(api_env, stale.id)).status == "closed"


# ---------------------------------------------------------------------------
# auto-close (§10.12)
# ---------------------------------------------------------------------------


async def test_auto_close_respects_the_per_org_setting(api_env) -> None:
    """§10.12. Three orgs whose ONLY difference is the auto-close window --
    3 days, 30 days, and no `sla_org_settings` row at all (the documented
    default of 7) -- each holding a ticket resolved 10 days before the sweep.

    That is the parameter a defect holds constant: an implementation that
    hardcodes 7, or that ignores the setting and closes everything resolved,
    passes on the 3-day and default orgs and fails only on the 30-day one. A
    single-org fixture cannot distinguish any of those implementations.

    The types differ too (`resolved` on an incident, `fulfilled` on a
    request), because `_RESOLVED_LIKE_STATUSES` is imported rather than
    restated and an implementation that hardcoded `'resolved'` would close
    only half the fleet.
    """
    resolved_at = SWEEP_AT - timedelta(days=10)

    tight_org = await api_env.create_org(f"sweep-tight-{uuid4().hex[:8]}")
    await _seed_settings(api_env, tight_org, 3)
    tight_ticket = await _seed_ticket(api_env, tight_org)
    await _resolve_directly(api_env, tight_ticket.id, status="resolved", at=resolved_at)

    loose_org = await api_env.create_org(f"sweep-loose-{uuid4().hex[:8]}")
    await _seed_settings(api_env, loose_org, 30)
    loose_ticket = await _seed_ticket(api_env, loose_org)
    await _resolve_directly(api_env, loose_ticket.id, status="resolved", at=resolved_at)

    default_org = await api_env.create_org(f"sweep-default-{uuid4().hex[:8]}")
    default_ticket = await _seed_ticket(api_env, default_org, type_="request")
    await _resolve_directly(api_env, default_ticket.id, status="fulfilled", at=resolved_at)

    assert (await _sweep(api_env, tight_org))["auto_closed"] == 1
    assert (await _sweep(api_env, loose_org))["auto_closed"] == 0
    assert (await _sweep(api_env, default_org))["auto_closed"] == 1

    assert (await _ticket_row(api_env, tight_ticket.id)).status == "closed"
    assert (await _ticket_row(api_env, loose_ticket.id)).status == "resolved"
    assert (await _ticket_row(api_env, default_ticket.id)).status == "closed"
    assert DEFAULT_AUTO_CLOSE_AFTER_DAYS == 7


async def test_auto_close_acts_as_the_system_actor_and_is_audited(api_env) -> None:
    """§7/§8: the close goes through `change_status` with
    `actor_type="system"`/`actor_id=None`, and every auto-close is audited.

    Two tickets in ONE org and ONE sweep, differing only in how long they have
    been resolved -- so an implementation that closed every resolved ticket
    regardless of age fails here rather than passing on a fixture where every
    ticket is stale.

    `sla.auto_closed` carries the window that drove the decision, which is the
    only place it is recorded: `ticket.status_changed` says a machine closed
    the ticket but not under what rule, and cannot answer "was this org's
    auto-close window respected".
    """
    org_id = await api_env.create_org(f"sweep-actor-{uuid4().hex[:8]}")
    await _seed_settings(api_env, org_id, 3)
    stale = await _seed_ticket(api_env, org_id)
    fresh = await _seed_ticket(api_env, org_id)
    await _resolve_directly(api_env, stale.id, status="resolved", at=SWEEP_AT - timedelta(days=4))
    await _resolve_directly(api_env, fresh.id, status="resolved", at=SWEEP_AT - timedelta(days=2))

    counts = await _sweep(api_env, org_id)

    assert counts["auto_closed"] == 1
    assert counts["failed"] == 0
    assert (await _ticket_row(api_env, stale.id)).status == "closed"
    assert (await _ticket_row(api_env, fresh.id)).status == "resolved"

    history = await _history(api_env, stale.id)
    assert [(row.from_status, row.to_status) for row in history] == [("resolved", "closed")]
    assert history[0].actor_type == "system"
    assert history[0].actor_id is None

    status_audit = [
        event
        for event in await _audit(api_env, org_id, "ticket.status_changed")
        if event.target_id == str(stale.id)
    ]
    assert len(status_audit) == 1
    assert status_audit[0].actor_type == "system"
    assert status_audit[0].actor_id is None

    auto_close_audit = await _audit(api_env, org_id, "sla.auto_closed")
    assert len(auto_close_audit) == 1
    assert auto_close_audit[0].target_id == str(stale.id)
    assert auto_close_audit[0].actor_type == "system"
    assert auto_close_audit[0].actor_id is None
    assert auto_close_audit[0].meta["auto_close_after_days"] == 3


async def test_auto_close_is_idempotent_and_leaves_a_reopened_ticket_alone(api_env) -> None:
    """§7's second idempotence claim: a ticket this pass closed is no longer
    in a resolved-like status, so the next cycle does not return it.

    The reopened ticket beside it varies the parameter that matters for the
    `resolved_at IS NOT NULL` half: `change_status` clears `resolved_at` on
    every reopen edge, so a ticket that WAS resolved long ago and has since
    been reopened has no measurable age and must not be closed on a guess.
    An implementation that keyed only on `resolved_at < cutoff` without the
    status filter, or that treated NULL as "infinitely old", closes it.
    """
    org_id = await api_env.create_org(f"sweep-reopen-{uuid4().hex[:8]}")
    stale = await _seed_ticket(api_env, org_id)
    reopened = await _seed_ticket(api_env, org_id, status="open")
    await _resolve_directly(api_env, stale.id, status="resolved", at=SWEEP_AT - timedelta(days=10))

    assert (await _sweep(api_env, org_id))["auto_closed"] == 1
    assert (await _sweep(api_env, org_id, now=LATER))["auto_closed"] == 0

    assert len(await _audit(api_env, org_id, "sla.auto_closed")) == 1
    assert len(await _history(api_env, stale.id)) == 1
    assert (await _ticket_row(api_env, reopened.id)).status == "open"
    assert await _history(api_env, reopened.id) == []


# ---------------------------------------------------------------------------
# tenancy (§10.14) -- pinned on the BYPASSRLS admin engine, where each query's
# own `org_id` predicate is the sole guard.
# ---------------------------------------------------------------------------


async def test_one_orgs_sweep_does_not_touch_another(api_env) -> None:
    """Two orgs holding identical work -- one overdue tracked ticket and one
    long-resolved ticket each -- and only one of them is swept.

    Run on the admin engine deliberately: through `org_session`, RLS would
    mask a deleted `org_id` predicate and this test would pass against a sweep
    with no tenancy filter at all (`test_sla_policies.py`'s discipline). With
    only one org in the fixture there is nothing for a missing predicate to
    reach, which is why the untouched org is the whole point.
    """
    org_a, _ = await _breach_org(api_env)
    org_b, _ = await _breach_org(api_env)
    ticket_a = await _seed_ticket(api_env, org_a)
    ticket_b = await _seed_ticket(api_env, org_b)
    await _track(api_env, org_a, ticket_a)
    await _track(api_env, org_b, ticket_b)
    resolved_a = await _seed_ticket(api_env, org_a)
    resolved_b = await _seed_ticket(api_env, org_b)
    for ticket_id in (resolved_a.id, resolved_b.id):
        await _resolve_directly(
            api_env, ticket_id, status="resolved", at=SWEEP_AT - timedelta(days=10)
        )

    counts = await _sweep_on_admin_engine(api_env, org_a)

    assert counts == {"breached": 2, "auto_closed": 1, "failed": 0}
    row_b = await _row(api_env, ticket_b.id)
    assert row_b is not None
    assert row_b.response_breached_at is None
    assert row_b.resolution_breached_at is None
    assert await _events(api_env, org_b, "ticket.sla_breached") == []
    assert (await _ticket_row(api_env, resolved_b.id)).status == "resolved"
    assert await _audit(api_env, org_b, "sla.auto_closed") == []


async def test_another_orgs_settings_do_not_govern_this_orgs_auto_close(api_env) -> None:
    """`sla_org_settings.org_id` is UNIQUE across the whole table, so a
    settings lookup without an `org_id` predicate returns whichever row the
    planner hands back first.

    The two orgs' windows are deliberately opposite (30 days vs the default 7)
    and both tickets were resolved 10 days ago, so reading the wrong org's row
    produces the wrong answer for BOTH orgs -- the loose org would close a
    ticket it should keep and the default org would keep one it should close.
    Identical windows would make the bug invisible.
    """
    loose_org = await api_env.create_org(f"sweep-xorg-loose-{uuid4().hex[:8]}")
    await _seed_settings(api_env, loose_org, 30)
    loose_ticket = await _seed_ticket(api_env, loose_org)
    await _resolve_directly(
        api_env, loose_ticket.id, status="resolved", at=SWEEP_AT - timedelta(days=10)
    )

    default_org = await api_env.create_org(f"sweep-xorg-default-{uuid4().hex[:8]}")
    default_ticket = await _seed_ticket(api_env, default_org)
    await _resolve_directly(
        api_env, default_ticket.id, status="resolved", at=SWEEP_AT - timedelta(days=10)
    )

    assert (await _sweep_on_admin_engine(api_env, loose_org))["auto_closed"] == 0
    assert (await _sweep_on_admin_engine(api_env, default_org))["auto_closed"] == 1
    assert (await _ticket_row(api_env, loose_ticket.id)).status == "resolved"
    assert (await _ticket_row(api_env, default_ticket.id)).status == "closed"


# ---------------------------------------------------------------------------
# §10.13 -- a calendar edit recomputes running deadlines only
# ---------------------------------------------------------------------------


async def _recompute_env(api_env) -> tuple[UUID, UUID, list[Ticket]]:
    """One Mon-Fri 09:00-17:00 calendar, one policy at 300/600 working
    minutes, and THREE tracked tickets in different clock states:

    0. both clocks running,
    1. response met, resolution running,
    2. both clocks stopped (response met, resolution breached).

    The three states are the parameter §10.13's guard turns on. A fixture with
    only running clocks passes against an implementation that rewrites
    everything.
    """
    org_id = await api_env.create_org(f"sweep-recompute-{uuid4().hex[:8]}")
    calendar_id = await _seed_calendar(
        api_env, org_id, timezone=LONDON, windows=_MON_FRI_9_TO_5
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        response_minutes=_RECOMPUTE_RESPONSE_MINUTES,
        resolution_minutes=_RECOMPUTE_RESOLUTION_MINUTES,
    )
    tickets = [await _seed_ticket(api_env, org_id) for _ in range(3)]
    for ticket in tickets:
        stored = await _track(api_env, org_id, ticket)
        # The fixture is only meaningful if the OLD calendar really produced
        # these instants.
        assert stored.response_due_at == _OLD_RESPONSE_DUE
        assert stored.resolution_due_at == _OLD_RESOLUTION_DUE
    await _stop_clocks(api_env, tickets[1].id, response_met_at=MONDAY_0900 + timedelta(hours=1))
    await _stop_clocks(
        api_env,
        tickets[2].id,
        response_met_at=MONDAY_0900 + timedelta(hours=1),
        resolution_breached_at=MONDAY_0900 + timedelta(days=2),
    )
    return org_id, calendar_id, tickets


async def test_a_calendar_edit_recomputes_running_deadlines_only(api_env) -> None:
    """§10.13. Halving every working day (09:00-17:00 -> 09:00-13:00) moves
    both targets to DIFFERENT instants, which is what makes the guard
    testable: an edited calendar that produced the same deadlines would let a
    mutant that rewrites met and breached clocks pass unnoticed -- the exact
    fixture failure Task 9's cascade guard had to be rebuilt for.

    A met or breached deadline is history reporting reads. Rewriting it would
    let an admin's calendar edit retroactively turn a breach into a
    non-breach, which is the metric laundering §0.6.1 refuses elsewhere.
    """
    org_id, calendar_id, tickets = await _recompute_env(api_env)
    await _replace_windows(api_env, org_id, calendar_id, _MON_FRI_9_TO_1)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        counts = await recompute_for_calendar_change(session, org_id, calendar_id)

    assert counts == {"recomputed": 2, "skipped": 1, "failed": 0}

    both_running = await _row(api_env, tickets[0].id)
    assert both_running is not None
    assert both_running.response_due_at == _NEW_RESPONSE_DUE
    assert both_running.resolution_due_at == _NEW_RESOLUTION_DUE

    half_stopped = await _row(api_env, tickets[1].id)
    assert half_stopped is not None
    assert half_stopped.response_due_at == _OLD_RESPONSE_DUE  # met: frozen
    assert half_stopped.resolution_due_at == _NEW_RESOLUTION_DUE

    both_stopped = await _row(api_env, tickets[2].id)
    assert both_stopped is not None
    assert both_stopped.response_due_at == _OLD_RESPONSE_DUE
    assert both_stopped.resolution_due_at == _OLD_RESOLUTION_DUE


async def test_a_calendar_edit_does_not_re_attribute_the_policy(api_env) -> None:
    """The recompute loads the row's OWN policy by id and never re-resolves.

    A rival policy that a re-resolution would land on is what makes this bite
    -- with one wildcard policy in the fixture, `resolve_policy` returns the
    same row and the assertion passes against an implementation that
    re-resolves (Task 9's trap, verbatim). Re-resolving on a CALENDAR edit
    would be §0.3's unattributed recompute arriving through an unrelated door:
    a policy change landing on tickets because somebody fixed a calendar, with
    `policy_source` still reading whatever it read before.
    """
    org_id, calendar_id, tickets = await _recompute_env(api_env)
    # Tighter and MORE specific than the wildcard the tickets are under, so
    # `resolve_policy` would prefer it (§3: a priority-only policy outranks
    # the both-wildcard fallback the tickets were materialised under).
    rival_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=15,
        resolution_minutes=45,
    )
    before = await _row(api_env, tickets[0].id)
    assert before is not None
    original_policy_id = before.policy_id
    assert original_policy_id != rival_id
    await _replace_windows(api_env, org_id, calendar_id, _MON_FRI_9_TO_1)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await recompute_for_calendar_change(session, org_id, calendar_id)

    stored = await _row(api_env, tickets[0].id)
    assert stored is not None
    assert stored.policy_id == original_policy_id
    assert stored.policy_source == "initial"
    # The rival's 15/45 minutes would have produced Mon 12:00 / Mon 12:45 on
    # the edited calendar; the row's own policy produces these.
    assert stored.response_due_at == _NEW_RESPONSE_DUE
    assert stored.resolution_due_at == _NEW_RESOLUTION_DUE


async def test_a_calendar_edit_skips_a_voided_promise(api_env) -> None:
    """A cancelled ticket's promise stopped meaning anything when it was
    voided; re-deriving its deadlines is churn on a dead row. The running row
    beside it is what stops "skipped everything" from passing."""
    org_id, calendar_id, tickets = await _recompute_env(api_env)
    await _set_columns(api_env, tickets[0].id, voided_at=MONDAY_0900 + timedelta(hours=2))
    await _replace_windows(api_env, org_id, calendar_id, _MON_FRI_9_TO_1)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        counts = await recompute_for_calendar_change(session, org_id, calendar_id)

    assert counts == {"recomputed": 1, "skipped": 2, "failed": 0}
    voided = await _row(api_env, tickets[0].id)
    assert voided is not None
    assert voided.response_due_at == _OLD_RESPONSE_DUE
    assert voided.resolution_due_at == _OLD_RESOLUTION_DUE


async def test_a_row_that_cannot_be_recomputed_does_not_fail_the_others(api_env) -> None:
    """`add_working_minutes` raises `UnsatisfiableCalendar` when the budget is
    still unspent after `MAX_LOOKAHEAD_DAYS`, and the budget includes
    `paused_minutes_total` (§5's credit is folded into the `minutes`
    argument).

    So the failure is per ROW, not per calendar: two rows share one perfectly
    valid calendar and only the one with an extreme accumulator is
    unsatisfiable. That is what makes this a containment test rather than a
    calendar test -- the healthy row must still be recomputed and committed,
    which an implementation that let the exception escape the loop cannot do.
    """
    org_id, calendar_id, tickets = await _recompute_env(api_env)
    # More working minutes than any Mon-Fri calendar can deliver inside
    # MAX_LOOKAHEAD_DAYS (366 days x 480 minutes at the very most).
    await _set_columns(api_env, tickets[0].id, paused_minutes_total=500_000)
    await _replace_windows(api_env, org_id, calendar_id, _MON_FRI_9_TO_1)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        counts = await recompute_for_calendar_change(session, org_id, calendar_id)

    assert counts == {"recomputed": 1, "skipped": 1, "failed": 1}
    unsatisfiable = await _row(api_env, tickets[0].id)
    assert unsatisfiable is not None
    assert unsatisfiable.response_due_at == _OLD_RESPONSE_DUE
    healthy = await _row(api_env, tickets[1].id)
    assert healthy is not None
    assert healthy.resolution_due_at == _NEW_RESOLUTION_DUE


async def test_an_unknown_timezone_does_not_escape_the_recompute(api_env) -> None:
    """`ZoneInfo("Nowhere/Atlantis")` raises `ZoneInfoNotFoundError`, which
    subclasses **`KeyError`** -- not `SlaValidationError`, not `ValueError`.

    A handler that enumerated the SLA module's own exception types would let
    it through, and one org's admin typing a timezone wrong would abort the
    pass. `sla_calendar.timezone` is validated at the API layer and never at
    sweep time precisely because a bad value discovered here would break every
    ticket on that calendar at once; this pins that it breaks nothing else.
    The calendar is valid when the rows are materialised and broken
    afterwards, which is exactly how an admin reaches this state.
    """
    org_id, calendar_id, tickets = await _recompute_env(api_env)
    await _set_timezone(api_env, calendar_id, "Nowhere/Atlantis")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        counts = await recompute_for_calendar_change(session, org_id, calendar_id)

    assert counts == {"recomputed": 0, "skipped": 1, "failed": 2}
    for ticket in tickets:
        stored = await _row(api_env, ticket.id)
        assert stored is not None
        assert stored.response_due_at == _OLD_RESPONSE_DUE


# ---------------------------------------------------------------------------
# §7's other half -- `recompute_for_policy_change`
# ---------------------------------------------------------------------------


async def _policy_of(api_env, ticket_id: UUID) -> UUID:
    """The policy the fixture's rows were actually materialised under, read
    off a stored row rather than remembered -- `_recompute_env` seeds it
    through `_seed_policy` and discards the id."""
    row = await _row(api_env, ticket_id)
    assert row is not None
    return row.policy_id


async def _set_policy_columns(api_env, policy_id: UUID, **values: Any) -> None:
    """Edit an `sla_policy` row directly on the admin engine.

    NOT through `policies.update_policy`: that function CALLS
    `recompute_for_policy_change` itself, so driving the edit through it would
    make the function under test part of its own fixture and an assertion
    about the recompute could not distinguish "the recompute ran" from "the
    edit was applied".
    """
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            policy = (
                await session.execute(select(SlaPolicy).where(SlaPolicy.id == policy_id))
            ).scalar_one()
            for key, value in values.items():
                setattr(policy, key, value)


async def test_a_policy_edit_recomputes_running_clocks_and_adopts_its_calendar(
    api_env,
) -> None:
    """`recompute_for_policy_change`, driven directly rather than through
    `policies.update_policy` -- the only coverage it had was indirect, through
    the API, which cannot tell this function's behaviour from the route's.

    The parameter this fixture varies is the SECOND calendar. With one
    calendar the policy's `calendar_id` and the row's are the same UUID,
    adopting and not adopting produce identical deadlines, and
    `adopt_policy_calendar` is untestable -- this file's recurring fixture
    failure. Repointing the policy from Mon-Fri 09:00-17:00 to 09:00-13:00
    makes the adoption visible as BOTH a different `calendar_id` and different
    instants.

    The three clock states carry the rest: the row whose clocks have all
    stopped is SKIPPED, and a skipped row is not repointed either -- the
    adoption happens inside `_recompute_row`, below the running-clock guards,
    so history keeps the calendar it was really promised under. `policy_id`
    and `policy_source` move on no row at all (§0.4).
    """
    org_id, calendar_id, tickets = await _recompute_env(api_env)
    policy_id = await _policy_of(api_env, tickets[0].id)
    halved_calendar_id = await _seed_calendar(
        api_env, org_id, timezone=LONDON, windows=_MON_FRI_9_TO_1
    )
    await _set_policy_columns(api_env, policy_id, calendar_id=halved_calendar_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        counts = await recompute_for_policy_change(session, org_id, policy_id)

    assert counts == {"recomputed": 2, "skipped": 1, "failed": 0}

    both_running = await _row(api_env, tickets[0].id)
    assert both_running is not None
    assert both_running.calendar_id == halved_calendar_id
    assert both_running.response_due_at == _NEW_RESPONSE_DUE
    assert both_running.resolution_due_at == _NEW_RESOLUTION_DUE
    assert both_running.policy_id == policy_id
    assert both_running.policy_source == "initial"

    half_stopped = await _row(api_env, tickets[1].id)
    assert half_stopped is not None
    assert half_stopped.calendar_id == halved_calendar_id
    assert half_stopped.response_due_at == _OLD_RESPONSE_DUE  # met: frozen
    assert half_stopped.resolution_due_at == _NEW_RESOLUTION_DUE

    both_stopped = await _row(api_env, tickets[2].id)
    assert both_stopped is not None
    assert both_stopped.calendar_id == calendar_id  # skipped: not repointed
    assert both_stopped.response_due_at == _OLD_RESPONSE_DUE
    assert both_stopped.resolution_due_at == _OLD_RESOLUTION_DUE
    assert both_stopped.policy_id == policy_id
    assert both_stopped.policy_source == "initial"


# ---------------------------------------------------------------------------
# tenancy on the recompute path (§10.14, invariant #5) -- pinned on the
# BYPASSRLS admin engine, where each query's own `org_id` predicate is the
# sole guard. `sweep_org` and both recompute functions take a SESSION rather
# than opening one, so all three are callable on that engine; only the first
# two had a test that said so.
# ---------------------------------------------------------------------------


async def _foreign_row(
    api_env, *, owner_org: UUID, ticket_id: UUID, calendar_id: UUID, policy_id: UUID
) -> UUID:
    """A `ticket_sla` row OWNED by `owner_org` that names ANOTHER org's
    ticket, calendar and policy, written directly on the BYPASSRLS admin
    engine -- `test_sla_pause._foreign_row` and
    `test_sla_tracking._foreign_tracking_row`, same shape and the same reason.
    No application path produces it (`start_tracking` rejects the cross-org
    ticket before writing); the FKs permit it, which is the point.

    THIS IS THE ONLY FIXTURE THAT VARIES THE PARAMETER. Both recompute
    functions select on `calendar_id`/`policy_id`, and those are per-row
    UUIDs: a second org holding an identically-SHAPED calendar and policy has
    its own ids, so a missing `org_id` predicate could not reach its rows and
    a two-org fixture built that way passes against the mutant. Only a row
    that names THIS org's calendar and policy while belonging to somebody else
    is inside the batch the predicate has to exclude. The FKs into
    `sla_calendar.id`/`sla_policy.id` have no org concept at all -- the same
    gap `calendars.delete_calendar`'s fallback catch is written for.

    Its ticket is deliberately one of the TARGET org's, so every lookup
    `_recompute_row` performs under the target org (`_load_ticket_in_org`,
    `_load_policy_in_org`, `load_calendar_spec`) succeeds: without the
    predicate this row is not merely READ across the tenant boundary, its
    deadlines are rewritten. A ticket in the owner's own org would fail the
    row instead and turn a silent cross-tenant WRITE into a mere `failed` count
    -- a much weaker signal.

    Its deadlines are the instants the ORIGINAL calendar produced, so
    "untouched" and "recomputed" are different values rather than the same one.
    """
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            row = TicketSla(
                org_id=owner_org,
                ticket_id=ticket_id,
                policy_id=policy_id,
                calendar_id=calendar_id,
                response_due_at=_OLD_RESPONSE_DUE,
                resolution_due_at=_OLD_RESOLUTION_DUE,
                policy_source="initial",
                last_recomputed_at=MONDAY_0900,
            )
            session.add(row)
            await session.flush()
            return row.id


def _assert_foreign_row_untouched(
    row: TicketSla | None, *, foreign_id: UUID, owner_org: UUID
) -> None:
    assert row is not None
    assert row.id == foreign_id
    assert row.org_id == owner_org
    assert row.response_due_at == _OLD_RESPONSE_DUE
    assert row.resolution_due_at == _OLD_RESOLUTION_DUE
    assert row.last_recomputed_at == MONDAY_0900


async def test_tenancy_recompute_for_calendar_change_org_filter_is_load_bearing(
    api_env,
) -> None:
    """`recompute_for_calendar_change`'s `TicketSla.org_id` predicate, with
    RLS removed from the equation.

    Every other caller of this function in the suite runs under `org_session`,
    where RLS rejects the neighbour's row first and the predicate can be
    deleted with the suite still green. Here the batch contains a row another
    org owns that names THIS org's calendar, and the call runs on the
    BYPASSRLS admin engine: without the predicate that row is selected,
    passes every in-org lookup (its ticket, policy and calendar are all this
    org's), and has both of another tenant's deadlines rewritten under this
    org's calendar edit.
    """
    org_id, calendar_id, tickets = await _recompute_env(api_env)
    policy_id = await _policy_of(api_env, tickets[0].id)
    neighbour_org = await api_env.create_org(f"sweep-neighbour-{uuid4().hex[:8]}")
    # An otherwise untracked ticket of THIS org, so the foreign row can claim
    # it: `ticket_sla.ticket_id` is UNIQUE.
    unclaimed = await _seed_ticket(api_env, org_id)
    foreign_id = await _foreign_row(
        api_env,
        owner_org=neighbour_org,
        ticket_id=unclaimed.id,
        calendar_id=calendar_id,
        policy_id=policy_id,
    )
    await _replace_windows(api_env, org_id, calendar_id, _MON_FRI_9_TO_1)

    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            counts = await recompute_for_calendar_change(session, org_id, calendar_id)

    # The foreign row's clocks are both running, so a batch that included it
    # would report 3 recomputed, not 2.
    assert counts == {"recomputed": 2, "skipped": 1, "failed": 0}
    moved = await _row(api_env, tickets[0].id)
    assert moved is not None
    assert moved.response_due_at == _NEW_RESPONSE_DUE
    _assert_foreign_row_untouched(
        await _row(api_env, unclaimed.id), foreign_id=foreign_id, owner_org=neighbour_org
    )


async def test_tenancy_recompute_for_policy_change_org_filter_is_load_bearing(
    api_env,
) -> None:
    """The same predicate on §7's other half, where removing it is worse: the
    policy-change path also writes `calendar_id`, so the neighbour's row would
    not merely have its deadlines rewritten -- it would be REPOINTED at a
    calendar belonging to a tenant it cannot see, permanently measured against
    a calendar its own org does not own."""
    org_id, calendar_id, tickets = await _recompute_env(api_env)
    policy_id = await _policy_of(api_env, tickets[0].id)
    halved_calendar_id = await _seed_calendar(
        api_env, org_id, timezone=LONDON, windows=_MON_FRI_9_TO_1
    )
    neighbour_org = await api_env.create_org(f"sweep-neighbour-{uuid4().hex[:8]}")
    unclaimed = await _seed_ticket(api_env, org_id)
    foreign_id = await _foreign_row(
        api_env,
        owner_org=neighbour_org,
        ticket_id=unclaimed.id,
        calendar_id=calendar_id,
        policy_id=policy_id,
    )
    await _set_policy_columns(api_env, policy_id, calendar_id=halved_calendar_id)

    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            counts = await recompute_for_policy_change(session, org_id, policy_id)

    assert counts == {"recomputed": 2, "skipped": 1, "failed": 0}
    moved = await _row(api_env, tickets[0].id)
    assert moved is not None
    assert moved.calendar_id == halved_calendar_id
    assert moved.response_due_at == _NEW_RESPONSE_DUE

    foreign = await _row(api_env, unclaimed.id)
    _assert_foreign_row_untouched(foreign, foreign_id=foreign_id, owner_org=neighbour_org)
    assert foreign is not None
    assert foreign.calendar_id == calendar_id  # NOT repointed


# ---------------------------------------------------------------------------
# sweep_all_orgs and the beat wiring
# ---------------------------------------------------------------------------


async def test_sweep_all_orgs_visits_every_org_and_one_failure_skips_only_that_org(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three orgs, the middle one poisoned.

    `sweep_org` is replaced with a recorder so this test asserts the fan-out
    itself rather than re-testing the passes, and so it cannot mutate the rows
    of every other org in the shared test database. The org count is the
    parameter: with one org in the fixture, an implementation that returns
    after the first -- or that lets the first failure unwind the loop --
    passes.
    """
    org_ids = [await api_env.create_org(f"sweep-fanout-{uuid4().hex[:8]}") for _ in range(3)]
    visited: list[UUID] = []

    async def _fake_sweep_org(session, org, *, now=None):  # type: ignore[no-untyped-def]
        if org not in org_ids:
            return {"breached": 0, "auto_closed": 0, "failed": 0}
        visited.append(org)
        if org == org_ids[1]:
            raise RuntimeError("this org's cycle is lost, and only this one")
        return {"breached": 2, "auto_closed": 1, "failed": 0}

    monkeypatch.setattr(sweep_module, "sweep_org", _fake_sweep_org)
    total = await sweep_all_orgs(
        engine=api_env.app_engine, admin_engine=api_env.admin_engine, now=SWEEP_AT
    )

    assert set(visited) == set(org_ids)
    # 3 actions each from the two healthy orgs; the poisoned one contributes
    # nothing and stops nothing.
    assert total == 6


async def test_sla_sweep_all_orgs_drives_the_module_sweep(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing else tests the worker shell, so a rename inside it would stop
    the sweep in production with the whole suite green."""
    from app import worker

    captured: dict[str, Any] = {}

    async def _fake_sweep_all_orgs(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 11

    monkeypatch.setattr(sweep_module, "sweep_all_orgs", _fake_sweep_all_orgs)
    with override_settings(api_env.settings):
        assert await worker.sla_sweep_all_orgs() == 11
    assert set(captured) == {"engine", "admin_engine"}


def test_create_celery_registers_the_sla_sweep(pg_cluster, tmp_path: Path) -> None:
    """The beat entry's task name and the `@celery_app.task(name=...)` string
    are two strings in two places that must match exactly; a typo would stop
    the sweep silently, and the sweep is the only thing that drives this whole
    module.

    The schedule is asserted against `SWEEP_INTERVAL_SECONDS` rather than
    against a repeated literal, because that constant is what the module
    documents as its interval.
    """
    from app import worker

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
    )
    with override_settings(settings):
        celery = worker.create_celery()

    entry = celery.conf.beat_schedule["sla-sweep"]
    assert entry["task"] == "app.worker.sla_sweep"
    assert entry["schedule"] == SWEEP_INTERVAL_SECONDS == 300.0

    source = inspect.getsource(worker)
    assert '@celery_app.task(name="app.worker.sla_sweep")' in source
    assert inspect.iscoroutinefunction(worker.sla_sweep_all_orgs)
