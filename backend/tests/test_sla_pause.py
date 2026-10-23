"""Pause and resume: stopping the clock while we are blocked on someone else
(docs/modules/sla/spec.md §5, §0.3, §2.5, §4).

FIXTURE DISCIPLINE -- what these tests deliberately VARY. This sub-project has
repeatedly shipped tests that passed while proving nothing because every
fixture held constant the one parameter the defect would vary: Task 2's
Critical hid behind a business-hours calendar (it only fires on 24/7 and
night-shift shapes); Task 3's F1 behind a single exception class; Task 4's F1
behind default-valued create arguments; Task 5's F1 behind a constant
column-to-clock PAIRING -- response only ever stopped by `met`, resolution only
ever by `breached` -- which left two of its four guards unproven.

**The constant this module found in its own first draft was the same pairing
in a new disguise: every guard test stopped a clock with `*_met_at`, because
§5's own sentence only names "a deadline already met".** A `resume` that
checked `response_met_at is None` and forgot `response_breached_at` would have
passed the whole file. The four combinations (response x met, response x
breached, resolution x met, resolution x breached) are now each asserted, and
each of the three guard tests below sits on a DIFFERENT calendar shape so the
guard and the arithmetic cannot be proven on one calendar and assumed on the
rest.

The other parameters varied here, and the mutant each one catches:

* **Calendar shape: 24/7, Mon-Fri 09:00-17:00, and a 22:00-06:00 night
  shift.** On 24/7 alone, working time and wall clock are indistinguishable --
  a `resume` measuring the pause with `end - start` passes every 24/7 test in
  this file. The night shift is the shape that hid Task 2's Critical: its
  working day wraps midnight, so an implementation that is right on Mon-Fri by
  accident (because its day always starts after midnight and ends before it)
  is wrong here.
* **Which clock is paused**, and whether the other one is running, met, or
  breached (above).
* **Open vs completed pause.** A recompute that lands DURING a pause must not
  credit it (the pause's length is not known yet) and must not clear
  `paused_at`; the same pause must then be credited exactly once at `resume`.
  A completed pause must survive that same recompute.
* **Whether the recompute happens before or after the pause.** Both orderings
  must reach the same deadlines; an implementation that folds the accumulator
  in one order only passes half of them.
* **`created_at` is always months in the past, never "now"** -- inherited from
  `test_sla_tracking.py`; a deadline measured from `datetime.now(UTC)` cannot
  land on any expectation here.

TIME IS FROZEN EXPLICITLY. `tracking._utcnow` is monkeypatched at each call
site, because the pause credit is `working_minutes_between(paused_at, now)` and
a test that let `now` be the real clock could only assert on a tolerance --
which is exactly how a wall-clock implementation would slip through on a 24/7
calendar.

Tenancy tests at the bottom run on a raw BYPASSRLS `admin_engine` session, so
each service's own `org_id` predicate is the sole guard under test -- through
`org_session` RLS would mask a deleted predicate and the test would pass either
way (the discipline `test_sla_policies.py` established).
"""

import logging
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session

# `app.models` first, before any `app.modules.*` import: importing an SLA
# service module directly would otherwise start the ORM registry from inside
# `app.modules.service_desk.models` and hit a partially-initialised module
# (`test_sla_tracking.py` orders its imports the same way for the same reason).
from app.models import Ticket
from app.modules.service_desk.lifecycle import INCIDENT_STATUSES, REQUEST_STATUSES
from app.modules.sla import tracking
from app.modules.sla.calendar import MAX_LOOKAHEAD_DAYS
from app.modules.sla.errors import SlaValidationError
from app.modules.sla.models import TicketSla
from app.modules.sla.tracking import PAUSING_STATUSES, pause, recompute_for_retriage, resume
from tests.test_sla_migration import _seed_calendar, _seed_policy, _seed_queue
from tests.test_sla_tracking import (
    _ALWAYS,
    _MON_FRI,
    LONDON,
    MONDAY_0900,
    _retriage,
    _row,
    _seed_ticket,
    _stop_clocks,
)

# The two F2 tests at the bottom drive the real ticket core rather than calling
# `resume`, because "the ticket can still be moved" is the property they are
# about. These helpers already exist in the wiring suite for exactly that; they
# are imported rather than re-written so the two files cannot drift apart on
# what "create a ticket and change its status" means.
#
# DIRECTION MATTERS: `test_sla_wiring` imports only from `test_sla_migration`,
# so this edge points leaf-ward and the graph stays acyclic. It must stay that
# way -- `test_sla_api` imports `_foreign_row` from THIS file, so a future
# import of `test_sla_pause` (or `test_sla_api`) from `test_sla_wiring` would
# close a cycle and break three files at once, in none of which the change was
# made.
from tests.test_sla_wiring import (  # noqa: E402 -- must follow `app.models` (above)
    _change,
    _create,
    _seed_category,
    _wildcard_org,
)

# A 22:00-06:00 shift, seven days a week: the calendar shape whose working day
# WRAPS MIDNIGHT. Stored as two windows per weekday because spec §2.2 forbids a
# window that wraps (`ck_sla_calendar_window_start_before_end`), with
# `end_time = '00:00:00'` meaning midnight-at-end-of-day (migration 0035).
_NIGHT_SHIFT = [(d, time(22, 0), time(0, 0)) for d in range(7)] + [
    (d, time(0, 0), time(6, 0)) for d in range(7)
]

# Friday 2026-03-06 09:00 UTC. `Europe/London` is on GMT for every instant in
# this module (BST 2026 starts 29 March), so local wall-clock == UTC in every
# expectation and no assertion here doubles as an unintended DST test -- Task 2
# owns those.
FRIDAY_0900 = datetime(2026, 3, 6, 9, 0, tzinfo=UTC)
# Friday 16:00 -- one working hour before a Mon-Fri calendar closes for the
# weekend. §5's load-bearing instant.
FRIDAY_1600 = datetime(2026, 3, 6, 16, 0, tzinfo=UTC)
# The following Monday 09:00 -- 65 hours of wall clock after FRIDAY_1600, and
# 60 working minutes.
NEXT_MONDAY_0900 = datetime(2026, 3, 9, 9, 0, tzinfo=UTC)
# Monday 22:00 -- the start of a night shift.
MONDAY_2200 = datetime(2026, 3, 2, 22, 0, tzinfo=UTC)


async def _pause_at(api_env, monkeypatch, org_id: UUID, ticket_id: UUID, when: datetime) -> None:
    """`pause` with the clock frozen at `when`, through the RLS-constrained app
    engine (the ordinary caller's path)."""
    monkeypatch.setattr(tracking, "_utcnow", lambda: when)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await pause(session, org_id, ticket_id)


async def _resume_at(api_env, monkeypatch, org_id: UUID, ticket_id: UUID, when: datetime) -> None:
    """`resume` with the clock frozen at `when`."""
    monkeypatch.setattr(tracking, "_utcnow", lambda: when)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await resume(session, org_id, ticket_id)


async def _start(api_env, org_id: UUID, ticket: Ticket) -> None:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await tracking.start_tracking(session, org_id, ticket)


# ---------------------------------------------------------------------------
# PAUSING_STATUSES
# ---------------------------------------------------------------------------


def test_pausing_statuses_are_the_two_blocked_on_someone_else_states() -> None:
    """§5 names exactly two: `pending` (incident branch) and
    `pending_approval` (request branch).

    Each is also checked against the ticket core's own status sets, so a typo
    ("pending_approvals") cannot sit here looking plausible while matching no
    status any ticket can ever hold -- which would silently mean the clock
    never pauses for requests at all.
    """
    assert PAUSING_STATUSES == frozenset({"pending", "pending_approval"})
    assert "pending" in INCIDENT_STATUSES
    assert "pending_approval" in REQUEST_STATUSES
    # And neither leaks into the other branch: an incident cannot be
    # `pending_approval`, a request cannot be `pending`.
    assert "pending_approval" not in INCIDENT_STATUSES
    assert "pending" not in REQUEST_STATUSES


# ---------------------------------------------------------------------------
# pause
# ---------------------------------------------------------------------------


async def test_pause_stamps_paused_at_and_moves_no_deadline(api_env, monkeypatch) -> None:
    """Pausing records WHEN we stopped and nothing else. How long the pause
    lasts is unknown at this point, so any deadline that moved here would be
    guesswork."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)
    await _start(api_env, org_id, ticket)

    paused_at = MONDAY_0900 + timedelta(hours=3)
    await _pause_at(api_env, monkeypatch, org_id, ticket.id, paused_at)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_at == paused_at
    assert stored.paused_minutes_total == 0
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480)


async def test_a_second_pause_does_not_restamp_and_so_does_not_shorten_the_credit(
    api_env, monkeypatch
) -> None:
    """A double pause is a no-op, and the ASSERTION THAT MATTERS is the credit,
    not the stamp.

    Re-stamping `paused_at` would move the pause's start forward to now and
    throw away everything accrued since it really began -- a redelivered status
    hook would quietly halve what the ticket is owed. So this pauses at 12:00,
    pauses again at 13:00, and resumes at 14:00: the correct credit is the full
    120 minutes, and a re-stamping implementation gives 60.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)
    await _start(api_env, org_id, ticket)

    first = MONDAY_0900 + timedelta(hours=3)
    await _pause_at(api_env, monkeypatch, org_id, ticket.id, first)
    await _pause_at(api_env, monkeypatch, org_id, ticket.id, first + timedelta(hours=1))

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_at == first  # not re-stamped

    await _resume_at(api_env, monkeypatch, org_id, ticket.id, first + timedelta(hours=2))

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_minutes_total == 120
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480 + 120)


async def test_pause_and_resume_are_no_ops_for_a_ticket_with_no_sla_row(
    api_env, monkeypatch
) -> None:
    """A ticket created when no policy matched has no promise to stop (§3).
    Neither call errors, and neither invents a row."""
    org_id = await api_env.create_org()
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)
    await _start(api_env, org_id, ticket)
    assert await _row(api_env, ticket.id) is None

    await _pause_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900)
    await _resume_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900)

    assert await _row(api_env, ticket.id) is None


async def test_resume_without_an_open_pause_changes_nothing(api_env, monkeypatch) -> None:
    """`resume` on a ticket that is not paused must not credit an imaginary
    pause. Without the `paused_at is None` early return, a `None` would either
    raise or -- worse, if defaulted to `created_at` -- hand back the ticket's
    entire age as pause credit."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)
    await _start(api_env, org_id, ticket)

    await _resume_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(days=30))

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_at is None
    assert stored.paused_minutes_total == 0
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480)


# ---------------------------------------------------------------------------
# Pause is measured in WORKING time (spec §5) -- the load-bearing property
# ---------------------------------------------------------------------------


async def test_a_weekend_pause_extends_by_the_friday_hour_only(api_env, monkeypatch) -> None:
    """§5's load-bearing case. A pause from Friday 16:00 to Monday 09:00 on a
    Mon-Fri 09:00-17:00 calendar extends the deadlines by **60 minutes** -- the
    one working hour left on the Friday -- not by the 65 hours of wall clock
    that elapsed.

    The weekend was never time we owed anyone, so giving it back would be
    inventing time; §5 states the consequence plainly, that measuring pause in
    wall-clock while measuring the target in working time lets a Friday-
    afternoon pause buy two free days.

    Ticket filed Friday 09:00, targets 60 / 480 -> due Friday 10:00 and Friday
    17:00. With 60 minutes of credit: response = 120 working minutes from
    Friday 09:00 = Friday 11:00; resolution = 540 = Friday 17:00 plus one hour
    on the Monday = Monday 10:00.

    THE WALL-CLOCK MUTANT: replacing `working_minutes_between(spec, paused_at,
    now)` with `int((now - paused_at).total_seconds() // 60)` credits 3900
    minutes instead of 60 and lands both deadlines days away -- this test goes
    red, as does the night-shift one below.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_MON_FRI)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=FRIDAY_0900)
    await _start(api_env, org_id, ticket)

    await _pause_at(api_env, monkeypatch, org_id, ticket.id, FRIDAY_1600)
    await _resume_at(api_env, monkeypatch, org_id, ticket.id, NEXT_MONDAY_0900)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_at is None
    assert stored.paused_minutes_total == 60
    assert stored.response_due_at == datetime(2026, 3, 6, 11, 0, tzinfo=UTC)
    assert stored.resolution_due_at == datetime(2026, 3, 9, 10, 0, tzinfo=UTC)


async def test_pause_credit_is_working_time_on_a_night_shift_calendar(
    api_env, monkeypatch
) -> None:
    """The same property on the calendar shape whose working day WRAPS
    MIDNIGHT -- 22:00-06:00, seven days a week.

    This is the shape Task 2's Critical hid from, and it is not redundant with
    the Mon-Fri case: an implementation whose day walk assumes work starts
    after local midnight and ends before the next one is right on Mon-Fri by
    accident and wrong here.

    Ticket filed Monday 22:00 (shift start), targets 60 / 480 -> Monday 23:00
    and Tuesday 06:00 (two hours of Monday night plus six of Tuesday morning).
    Paused Tuesday 05:30, resumed Tuesday 22:30: 17 hours of wall clock, of
    which 60 minutes are working time (05:30-06:00 and 22:00-22:30). With that
    credit the response is 120 working minutes from Monday 22:00 = Tuesday
    00:00, and the resolution is 540 = Tuesday 23:00.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_NIGHT_SHIFT)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_2200)
    await _start(api_env, org_id, ticket)

    await _pause_at(
        api_env, monkeypatch, org_id, ticket.id, datetime(2026, 3, 3, 5, 30, tzinfo=UTC)
    )
    await _resume_at(
        api_env, monkeypatch, org_id, ticket.id, datetime(2026, 3, 3, 22, 30, tzinfo=UTC)
    )

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_minutes_total == 60
    assert stored.response_due_at == datetime(2026, 3, 3, 0, 0, tzinfo=UTC)
    assert stored.resolution_due_at == datetime(2026, 3, 3, 23, 0, tzinfo=UTC)


async def test_on_a_24_7_calendar_pause_credit_equals_elapsed_time(api_env, monkeypatch) -> None:
    """The control for the two tests above: on a genuine 24/7 calendar working
    time and wall clock ARE the same, so a 120-minute pause extends by 120.

    Kept deliberately. It is the test that makes the other two mean something:
    without it, the Mon-Fri and night-shift expectations could be satisfied by
    an implementation that simply under-credits everything.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)
    await _start(api_env, org_id, ticket)

    await _pause_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=3))
    await _resume_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=5))

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_minutes_total == 120
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=180)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=600)


async def test_two_completed_pauses_accumulate(api_env, monkeypatch) -> None:
    """The accumulator is a running total, not a last-pause slot. Two pauses of
    60 and 120 minutes leave 180, and the deadline reflects both."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)
    await _start(api_env, org_id, ticket)

    await _pause_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=1))
    await _resume_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=2))
    await _pause_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=4))
    await _resume_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=6))

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_minutes_total == 180
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480 + 180)


# ---------------------------------------------------------------------------
# The running-clock guard.
#
# Four combinations exist -- {response, resolution} x {met, breached} -- and
# Task 5's F1 was proof that testing a constant PAIRING leaves half of them
# unproven. All four are asserted across the three tests below, on three
# different calendar shapes.
# ---------------------------------------------------------------------------


async def test_a_met_response_is_not_extended_while_a_running_resolution_is(
    api_env, monkeypatch
) -> None:
    """Combination 1 of 4: response stopped by `response_met_at`, resolution
    running. Mon-Fri calendar.

    A met clock has stopped for good; its `due_at` is now history that
    reporting reads, and extending it would rewrite what was promised after the
    fact. The two clocks stop independently, so exactly one of the two moves.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_MON_FRI)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=FRIDAY_0900)
    await _start(api_env, org_id, ticket)
    await _stop_clocks(api_env, ticket.id, response_met_at=FRIDAY_0900 + timedelta(minutes=30))

    await _pause_at(api_env, monkeypatch, org_id, ticket.id, FRIDAY_1600)
    await _resume_at(api_env, monkeypatch, org_id, ticket.id, NEXT_MONDAY_0900)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.response_due_at == datetime(2026, 3, 6, 10, 0, tzinfo=UTC)  # untouched
    assert stored.resolution_due_at == datetime(2026, 3, 9, 10, 0, tzinfo=UTC)  # +60
    assert stored.paused_minutes_total == 60
    assert stored.paused_at is None


async def test_a_breached_resolution_is_not_extended_while_a_running_response_is(
    api_env, monkeypatch
) -> None:
    """Combination 2 of 4: resolution stopped by `resolution_breached_at`,
    response running. 24/7 calendar.

    The mirror image of the test above in BOTH axes -- the other clock, stopped
    by the other column. A guard written as `if tracking.response_met_at is
    None` (§5's sentence names only "a deadline already met") passes that test
    and fails this one.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)
    await _start(api_env, org_id, ticket)
    await _stop_clocks(
        api_env, ticket.id, resolution_breached_at=MONDAY_0900 + timedelta(minutes=600)
    )

    await _pause_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=3))
    await _resume_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=5))

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=180)  # +120
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480)  # untouched
    assert stored.paused_minutes_total == 120


async def test_both_clocks_stopped_extends_neither_but_still_records_the_pause(
    api_env, monkeypatch
) -> None:
    """Combinations 3 and 4 of 4: response stopped by `response_breached_at`
    AND resolution stopped by `resolution_met_at` -- the two pairings the first
    two tests did not use. Night-shift calendar.

    Nothing is owed, so neither deadline moves. `paused_at` is still cleared
    and the pause is still recorded: `paused_at` means "in a pausing status
    right now" (the sweep's `paused_at IS NULL` filter reads it that way), and
    leaving it set would freeze a settled ticket in a state no later resume
    would clear.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_NIGHT_SHIFT)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_2200)
    await _start(api_env, org_id, ticket)
    await _stop_clocks(
        api_env,
        ticket.id,
        response_breached_at=MONDAY_2200 + timedelta(hours=2),
        resolution_met_at=MONDAY_2200 + timedelta(hours=3),
    )

    await _pause_at(
        api_env, monkeypatch, org_id, ticket.id, datetime(2026, 3, 3, 5, 30, tzinfo=UTC)
    )
    await _resume_at(
        api_env, monkeypatch, org_id, ticket.id, datetime(2026, 3, 3, 22, 30, tzinfo=UTC)
    )

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.response_due_at == datetime(2026, 3, 2, 23, 0, tzinfo=UTC)  # untouched
    assert stored.resolution_due_at == datetime(2026, 3, 3, 6, 0, tzinfo=UTC)  # untouched
    assert stored.paused_at is None
    assert stored.paused_minutes_total == 60


# ---------------------------------------------------------------------------
# A PAUSE MUST ALWAYS BE ESCAPABLE (review F2).
#
# Everything above pauses for hours or a weekend. Two failure modes live only
# past those fixtures, and both end in the same place: a ticket that can never
# leave the pausing status. `resume` runs from `change_status` on EVERY exit
# from `pending`/`pending_approval`, and every status move goes through
# `change_status` -- so a ticket that cannot resume cannot be resolved, closed
# or even CANCELLED, with no in-application recovery.
#
# Both tests below drive the real `change_status` rather than calling `resume`,
# because "the ticket can still be moved" is the property under test and only
# the ticket core can demonstrate it. The pause is opened by the ordinary path
# and then `tracking._utcnow` is frozen 400 days ahead of it -- a test that
# actually waited 400 days is not a test, and freezing forward from the real
# stamp keeps the fixture from depending on today's date.
# ---------------------------------------------------------------------------

_LONG_PAUSE_DAYS = 400


async def _empty_the_calendar(admin_engine: AsyncEngine, calendar_id: UUID) -> None:
    """Delete every window from a calendar, which is all it takes for
    `add_working_minutes` to raise `UnsatisfiableCalendar` against it -- one
    mis-saved admin edit, per `sla_misconfiguration_is_not_fatal`'s docstring."""
    async with AsyncSession(admin_engine) as session:
        async with session.begin():
            await session.execute(
                text("DELETE FROM sla_calendar_window WHERE calendar_id = :c"), {"c": calendar_id}
            )


async def test_a_pause_longer_than_the_lookahead_horizon_can_still_be_resumed(
    api_env, admin_engine, monkeypatch, caplog
) -> None:
    """A ticket parked in `pending` for 400 days -- "waiting on the vendor RMA"
    -- must still be able to leave that status.

    THE DEFECT THIS FALSIFIES. `resume` measured the open pause with
    `working_minutes_between(paused_at, now)`, which raises **`ValueError`** on
    a span wider than `MAX_LOOKAHEAD_DAYS = 366`. `ValueError` is deliberately
    not caught by `sla_misconfiguration_is_not_fatal`, so it escaped
    `change_status` and rolled back the transaction: the ticket could not be
    resumed, resolved, closed or cancelled, ever, by any application path. 365
    days worked, 366 worked, 367 did not.

    The bound is right where it is written -- a DEADLINE further out than the
    horizon really is a corrupt timestamp -- and wrong here, because the span
    is real elapsed wall time in a pausing status and thirteen months of it is
    an ordinary MSP fact.

    WHAT IS ASSERTED. That the status moved; that the pause closed; that the
    credit is the working time of the clamped window and not zero (a "fix" that
    swallowed the exception and credited nothing would pass a status-only
    assertion); and that the clamp actually fired, read off the warning it is
    required to log -- otherwise this test would still pass on a day the fixture
    happened to shrink under the horizon.
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )
    await _change(api_env, org_id, ticket_id, to_status="open", actor_id=user_id)
    await _change(api_env, org_id, ticket_id, to_status="pending", actor_id=user_id)

    paused = await _row(api_env, ticket_id)
    assert paused is not None
    assert paused.paused_at is not None
    now = paused.paused_at + timedelta(days=_LONG_PAUSE_DAYS)
    monkeypatch.setattr(tracking, "_utcnow", lambda: now)

    with caplog.at_level(logging.WARNING, logger="its.sla.tracking"):
        await _change(api_env, org_id, ticket_id, to_status="open", actor_id=user_id)

    async with AsyncSession(admin_engine) as session:
        status = (
            await session.execute(text("SELECT status FROM ticket WHERE id = :t"), {"t": ticket_id})
        ).scalar_one()
    assert status == "open"

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.paused_at is None
    # 24/7 calendar, so the clamped window's working minutes are its elapsed
    # minutes: `MAX_LOOKAHEAD_DAYS - 1` days, not the full 400 and not zero.
    assert row.paused_minutes_total == (MAX_LOOKAHEAD_DAYS - 1) * 24 * 60
    assert any("more than MAX_LOOKAHEAD_DAYS" in record.message for record in caplog.records)


async def test_a_pause_is_closed_even_when_the_deadlines_cannot_be_re_derived(
    api_env, admin_engine, monkeypatch, caplog
) -> None:
    """`resume` re-derives each running deadline with `add_working_minutes`,
    which raises `UnsatisfiableCalendar` if the row's calendar cannot deliver
    the target -- an admin emptying the calendar is enough, and that exception
    IS swallowed by `sla_misconfiguration_is_not_fatal`, so the status move
    succeeds either way.

    THE DEFECT THIS FALSIFIES (F2b). With `paused_at = None` written AFTER the
    re-derivation, the swallowed exception left the status moved and the row
    still paused. Nothing recovers from that: the sweep's `paused_at IS NULL`
    filter excludes the row forever so it can never breach, `views.target_state`
    renders "paused" on an open ticket, and `count_tickets_without_sla` does not
    count it either -- an invisible promise, frozen, on a ticket that is not
    paused. Closing the pause first degrades the same failure to "the deadlines
    did not move, and there is a log line saying so".

    The deadlines are asserted UNCHANGED on purpose: this is not a claim that
    the re-derivation somehow worked, it is a claim about which half survives
    when it does not.
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )
    await _change(api_env, org_id, ticket_id, to_status="open", actor_id=user_id)
    await _change(api_env, org_id, ticket_id, to_status="pending", actor_id=user_id)

    paused = await _row(api_env, ticket_id)
    assert paused is not None
    assert paused.paused_at is not None
    await _empty_the_calendar(admin_engine, paused.calendar_id)

    now = paused.paused_at + timedelta(hours=2)
    monkeypatch.setattr(tracking, "_utcnow", lambda: now)
    with caplog.at_level(logging.ERROR, logger="its.sla.tracking"):
        await _change(api_env, org_id, ticket_id, to_status="open", actor_id=user_id)

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.paused_at is None, "the pause must close even when the re-derivation fails"
    # The empty calendar credits no working time, and neither deadline moved.
    assert row.paused_minutes_total == 0
    assert row.response_due_at == paused.response_due_at
    assert row.resolution_due_at == paused.resolution_due_at
    assert any("resume" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# The gap this task closes: recompute must not destroy completed pause credit.
#
# `recompute_for_retriage` re-derives both deadlines from `ticket.created_at`
# (§0.3). Before `paused_minutes_total` existed, the ONLY record of a completed
# pause was the offset already inside `due_at` -- which is exactly what the
# recompute overwrites.
# ---------------------------------------------------------------------------


async def _two_queues_one_policy_each(
    api_env, org_id: UUID, calendar_id: UUID, *, response: int, resolution: int
) -> tuple[UUID, UUID]:
    """Two queues in one org, each with its own queue-scoped policy on the same
    calendar. Targets are passed in so a caller can make the two policies
    IDENTICAL apart from their queue -- the state in which a re-triage changes
    nothing at all about the promise, and must therefore change no deadline."""
    queue_a = await _seed_queue(api_env, org_id)
    queue_b = await _seed_queue(api_env, org_id)
    for queue_id in (queue_a, queue_b):
        await _seed_policy(
            api_env,
            org_id,
            calendar_id=calendar_id,
            queue_id=queue_id,
            response_minutes=response,
            resolution_minutes=resolution,
        )
    return queue_a, queue_b


async def test_a_retriage_onto_identical_targets_preserves_completed_pause_credit(
    api_env, monkeypatch
) -> None:
    """The review's own numbers, on a 24/7 calendar.

    Filed Monday 09:00, 480-minute resolution target -> due 17:00. A 120-minute
    completed pause -> 19:00. Re-triaged onto a policy with **identical**
    targets (a pure queue move; nothing about the promise changes) -> it must
    stay at **19:00**.

    Before `paused_minutes_total`, it fell back to 17:00: two hours of credit
    destroyed by a recompute that changed nothing. Deleting the `+ credit` from
    `recompute_for_retriage` reproduces exactly that and turns this red.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    queue_a, queue_b = await _two_queues_one_policy_each(
        api_env, org_id, calendar_id, response=60, resolution=480
    )
    ticket = await _seed_ticket(api_env, org_id, queue_id=queue_a, created_at=MONDAY_0900)
    await _start(api_env, org_id, ticket)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.resolution_due_at == datetime(2026, 3, 2, 17, 0, tzinfo=UTC)

    await _pause_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=1))
    await _resume_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=3))

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.resolution_due_at == datetime(2026, 3, 2, 19, 0, tzinfo=UTC)
    assert stored.paused_minutes_total == 120

    moved = await _retriage(api_env, ticket, queue_id=queue_b)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await recompute_for_retriage(session, org_id, moved)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.policy_source == "retriage"
    assert stored.resolution_due_at == datetime(2026, 3, 2, 19, 0, tzinfo=UTC)  # NOT 17:00
    assert stored.response_due_at == datetime(2026, 3, 2, 12, 0, tzinfo=UTC)  # 60 + 120
    assert stored.paused_minutes_total == 120  # the recompute consumes, never clears


async def test_on_business_hours_the_lost_credit_would_be_two_working_days(
    api_env, monkeypatch
) -> None:
    """The same defect on a Mon-Fri calendar, where it is far worse than two
    hours: the credit is measured in working minutes, so dropping it moves the
    deadline back by whole WORKING DAYS.

    Filed Monday 09:00, 480-minute resolution -> Monday 17:00. Paused Monday
    09:00 to Wednesday 09:00: 960 working minutes (two 8-hour days). Resolution
    becomes 1440 working minutes from Monday 09:00 = **Wednesday 17:00**.

    Without the credit, the re-triage re-derives it back to Monday 17:00 -- two
    working days in the past at the instant of a correct routing action. The
    sweep would then stamp `resolution_breached_at`, publish
    `ticket.sla_breached`, escalate and notify staff, for a promise that was
    never actually missed.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_MON_FRI)
    queue_a, queue_b = await _two_queues_one_policy_each(
        api_env, org_id, calendar_id, response=60, resolution=480
    )
    ticket = await _seed_ticket(api_env, org_id, queue_id=queue_a, created_at=MONDAY_0900)
    await _start(api_env, org_id, ticket)

    await _pause_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900)
    await _resume_at(
        api_env, monkeypatch, org_id, ticket.id, datetime(2026, 3, 4, 9, 0, tzinfo=UTC)
    )

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_minutes_total == 960
    assert stored.resolution_due_at == datetime(2026, 3, 4, 17, 0, tzinfo=UTC)

    moved = await _retriage(api_env, ticket, queue_id=queue_b)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await recompute_for_retriage(session, org_id, moved)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    # Wednesday 17:00, not Monday 17:00.
    assert stored.resolution_due_at == datetime(2026, 3, 4, 17, 0, tzinfo=UTC)
    assert stored.response_due_at == datetime(2026, 3, 4, 10, 0, tzinfo=UTC)  # 60 + 960


async def test_the_credit_is_spent_in_working_time_not_wall_clock(api_env, monkeypatch) -> None:
    """The trap that mirrors §5's own warning, one level up: the accumulator
    must be folded into the `minutes` ARGUMENT of `add_working_minutes`, never
    added to its RESULT as a `timedelta`.

    Mon-Fri calendar, filed Friday 09:00, 480-minute resolution -> Friday
    17:00. A 60-minute pause taken on the Friday afternoon. Folded into the
    target, 540 working minutes from Friday 09:00 lands on **Monday 10:00**.
    Added to the result as `timedelta(minutes=60)`, it lands on Friday 18:00 --
    an hour that is not a working hour at all, and the same free-weekend
    arithmetic §5 forbids for the pause measurement itself.

    A recompute is run afterwards to prove the same of that path: the deadline
    must not move at all when the policy is unchanged.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_MON_FRI)
    queue_a, queue_b = await _two_queues_one_policy_each(
        api_env, org_id, calendar_id, response=60, resolution=480
    )
    ticket = await _seed_ticket(api_env, org_id, queue_id=queue_a, created_at=FRIDAY_0900)
    await _start(api_env, org_id, ticket)

    await _pause_at(api_env, monkeypatch, org_id, ticket.id, FRIDAY_1600)
    await _resume_at(api_env, monkeypatch, org_id, ticket.id, NEXT_MONDAY_0900)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.resolution_due_at == datetime(2026, 3, 9, 10, 0, tzinfo=UTC)
    assert stored.resolution_due_at != datetime(2026, 3, 6, 18, 0, tzinfo=UTC)

    moved = await _retriage(api_env, ticket, queue_id=queue_b)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await recompute_for_retriage(session, org_id, moved)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.resolution_due_at == datetime(2026, 3, 9, 10, 0, tzinfo=UTC)


async def test_an_open_pause_is_credited_at_resume_and_never_by_the_recompute(
    api_env, monkeypatch
) -> None:
    """OPEN vs COMPLETED pause -- the parameter a fixture that always resumed
    before recomputing would hold constant.

    A recompute landing DURING a pause must credit nothing (the pause's length
    is not yet known) and must not clear `paused_at`. The same pause must then
    be credited exactly once at `resume`, and against the NEW policy's target,
    because that is the promise the ticket is now under.

    24/7 calendar, filed Monday 09:00 under a 480-minute resolution -> 17:00.
    Paused at 12:00. Re-triaged at that moment onto a 600-minute policy ->
    19:00, still paused, still zero credit. Resumed at 14:00 -> 120 minutes of
    credit -> 600 + 120 = 21:00. An implementation that credited the open pause
    twice would land at 23:00; one that lost it would stay at 19:00.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    queue_a = await _seed_queue(api_env, org_id)
    queue_b = await _seed_queue(api_env, org_id)
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        queue_id=queue_a,
        response_minutes=60,
        resolution_minutes=480,
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        queue_id=queue_b,
        response_minutes=90,
        resolution_minutes=600,
    )
    ticket = await _seed_ticket(api_env, org_id, queue_id=queue_a, created_at=MONDAY_0900)
    await _start(api_env, org_id, ticket)

    paused_at = MONDAY_0900 + timedelta(hours=3)
    await _pause_at(api_env, monkeypatch, org_id, ticket.id, paused_at)

    moved = await _retriage(api_env, ticket, queue_id=queue_b)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await recompute_for_retriage(session, org_id, moved)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_at == paused_at  # the recompute does not close the pause
    assert stored.paused_minutes_total == 0  # nothing to credit yet
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=600)

    await _resume_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0900 + timedelta(hours=5))

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.paused_at is None
    assert stored.paused_minutes_total == 120
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=720)
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=210)


async def test_recompute_before_and_after_a_pause_reach_the_same_deadlines(
    api_env, monkeypatch
) -> None:
    """ORDERING -- the other parameter a single-scenario fixture would hold
    constant. Two identical tickets, the same re-triage and the same pause,
    applied in opposite orders, must end at the same deadlines.

    Both orderings are ordinary: a ticket is re-triaged and later goes
    `pending`, or goes `pending` and is later re-triaged. An implementation
    that folds the accumulator in only one of the two paths -- or that mutates
    `due_at` on one path and re-derives on the other -- passes half of this
    test and fails the other half.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_MON_FRI)
    queue_a = await _seed_queue(api_env, org_id)
    queue_b = await _seed_queue(api_env, org_id)
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        queue_id=queue_a,
        response_minutes=60,
        resolution_minutes=480,
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        queue_id=queue_b,
        response_minutes=120,
        resolution_minutes=960,
    )

    pause_from = FRIDAY_1600
    pause_to = NEXT_MONDAY_0900

    # Ticket 1: pause, resume, THEN re-triage.
    first = await _seed_ticket(api_env, org_id, queue_id=queue_a, created_at=FRIDAY_0900)
    await _start(api_env, org_id, first)
    await _pause_at(api_env, monkeypatch, org_id, first.id, pause_from)
    await _resume_at(api_env, monkeypatch, org_id, first.id, pause_to)
    moved_first = await _retriage(api_env, first, queue_id=queue_b)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await recompute_for_retriage(session, org_id, moved_first)

    # Ticket 2: re-triage FIRST, then pause and resume.
    second = await _seed_ticket(api_env, org_id, queue_id=queue_a, created_at=FRIDAY_0900)
    await _start(api_env, org_id, second)
    moved_second = await _retriage(api_env, second, queue_id=queue_b)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await recompute_for_retriage(session, org_id, moved_second)
    await _pause_at(api_env, monkeypatch, org_id, second.id, pause_from)
    await _resume_at(api_env, monkeypatch, org_id, second.id, pause_to)

    row_first = await _row(api_env, first.id)
    row_second = await _row(api_env, second.id)
    assert row_first is not None and row_second is not None
    assert row_first.paused_minutes_total == 60
    assert row_second.paused_minutes_total == 60
    assert row_first.response_due_at == row_second.response_due_at
    assert row_first.resolution_due_at == row_second.resolution_due_at
    # And the shared answer is the one derived from the ticket's start under
    # the NEW policy plus the credit -- asserted absolutely, so that two
    # identically wrong answers cannot satisfy the equality above.
    assert row_first.response_due_at == datetime(2026, 3, 6, 12, 0, tzinfo=UTC)  # 120 + 60
    assert row_first.resolution_due_at == datetime(2026, 3, 10, 10, 0, tzinfo=UTC)  # 960 + 60


# ---------------------------------------------------------------------------
# Schema pin for migration 0037
# ---------------------------------------------------------------------------


async def test_paused_minutes_total_is_not_null_and_defaults_to_zero(admin_engine) -> None:
    """Migration 0037 at the DATABASE, not merely in the model.

    NOT NULL with a zero default is what lets `resume` and
    `recompute_for_retriage` read the accumulator without a `None` branch; a
    nullable column would need a coercion, and the only safe coercion is 0, so
    the nullability would buy nothing and could swallow a write bug silently.
    The CHECK is the other half: working time is never negative, so a pause can
    never buy time back (`working_minutes_between` floors at zero for the same
    reason).
    """
    async with AsyncSession(admin_engine) as session:
        column = (
            await session.execute(
                text(
                    "SELECT is_nullable, column_default, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'ticket_sla' "
                    "AND column_name = 'paused_minutes_total'"
                )
            )
        ).one()
        check = (
            await session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_ticket_sla_paused_minutes_non_negative'"
                )
            )
        ).scalar_one()

    assert column.is_nullable == "NO"
    assert column.column_default == "0"
    assert column.data_type == "integer"
    assert "paused_minutes_total >= 0" in check


# ---------------------------------------------------------------------------
# Tenancy -- load-bearing, on a raw BYPASSRLS admin session (module docstring).
#
# `pause`/`resume` take a ticket_id, not a `Ticket`, so there is no in-memory
# `_require_same_org` here: these predicates ARE the tenancy guard. Three of
# them are pinned below, one per query the pause path issues that this task
# added or newly reached.
# ---------------------------------------------------------------------------


async def _foreign_row(
    api_env,
    *,
    owner_org: UUID,
    ticket_id: UUID,
    calendar_id: UUID,
    policy_id: UUID,
    paused_at: datetime | None = None,
) -> UUID:
    """A `ticket_sla` row built directly on the BYPASSRLS admin session, so its
    `org_id`, `ticket_id` and `policy_id` can point wherever a test needs.

    None of these shapes is reachable through the application (`start_tracking`
    rejects a cross-org ticket before writing), which is the point: the FKs
    permit them, so only the services' own `org_id` predicates stand between a
    caller and another tenant's data.
    """
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            row = TicketSla(
                org_id=owner_org,
                ticket_id=ticket_id,
                policy_id=policy_id,
                calendar_id=calendar_id,
                response_due_at=MONDAY_0900 + timedelta(minutes=60),
                resolution_due_at=MONDAY_0900 + timedelta(minutes=480),
                paused_at=paused_at,
                policy_source="initial",
                last_recomputed_at=MONDAY_0900,
            )
            session.add(row)
            await session.flush()
            return row.id


async def test_tenancy_pause_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine, monkeypatch
) -> None:
    """`_load_tracking`'s `org_id` predicate on the pause path. Org B owns the
    `ticket_sla` row for this ticket; org A calls `pause`. With the predicate A
    finds nothing and does nothing. Without it, A stamps `paused_at` on B's
    row -- stopping another tenant's SLA clock, which also removes that row
    from the sweep's breach pass (§7 skips `paused_at IS NOT NULL`)."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_b = await _seed_calendar(api_env, org_b, timezone=LONDON, windows=_ALWAYS)
    policy_b = await _seed_policy(api_env, org_b, calendar_id=calendar_b)
    ticket_a = await _seed_ticket(api_env, org_a, created_at=MONDAY_0900)
    foreign_id = await _foreign_row(
        api_env,
        owner_org=org_b,
        ticket_id=ticket_a.id,
        calendar_id=calendar_b,
        policy_id=policy_b,
    )

    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0900 + timedelta(hours=1))
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await pause(session, org_a, ticket_a.id)
        await session.commit()

    stored = await _row(api_env, ticket_a.id)
    assert stored is not None
    assert stored.id == foreign_id
    assert stored.org_id == org_b
    assert stored.paused_at is None


async def test_tenancy_resume_tracking_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine, monkeypatch
) -> None:
    """The same predicate on the resume path, where removing it is worse: org A
    would not merely read org B's row, it would consume B's open pause --
    clearing `paused_at` and rewriting both of B's deadlines."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_b = await _seed_calendar(api_env, org_b, timezone=LONDON, windows=_ALWAYS)
    policy_b = await _seed_policy(api_env, org_b, calendar_id=calendar_b)
    ticket_a = await _seed_ticket(api_env, org_a, created_at=MONDAY_0900)
    paused_at = MONDAY_0900 + timedelta(hours=1)
    foreign_id = await _foreign_row(
        api_env,
        owner_org=org_b,
        ticket_id=ticket_a.id,
        calendar_id=calendar_b,
        policy_id=policy_b,
        paused_at=paused_at,
    )

    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0900 + timedelta(hours=3))
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await resume(session, org_a, ticket_a.id)
        await session.commit()

    stored = await _row(api_env, ticket_a.id)
    assert stored is not None
    assert stored.id == foreign_id
    assert stored.paused_at == paused_at  # B's pause is still open
    assert stored.paused_minutes_total == 0
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480)


async def test_tenancy_resume_ticket_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine, monkeypatch
) -> None:
    """`_load_ticket_in_org`'s predicate. Org A owns a `ticket_sla` row that
    points at org B's TICKET -- legal at the FK, which does not check org
    membership. Resume must refuse rather than re-derive A's deadlines from B's
    `created_at`. Without the predicate the ticket is found and the re-derive
    proceeds against another tenant's data."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_a = await _seed_calendar(api_env, org_a, timezone=LONDON, windows=_ALWAYS)
    policy_a = await _seed_policy(api_env, org_a, calendar_id=calendar_a)
    ticket_b = await _seed_ticket(api_env, org_b, created_at=MONDAY_0900 - timedelta(days=90))
    paused_at = MONDAY_0900 + timedelta(hours=1)
    await _foreign_row(
        api_env,
        owner_org=org_a,
        ticket_id=ticket_b.id,
        calendar_id=calendar_a,
        policy_id=policy_a,
        paused_at=paused_at,
    )

    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0900 + timedelta(hours=3))
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError, match="the ticket itself is not visible"):
            await resume(session, org_a, ticket_b.id)

    stored = await _row(api_env, ticket_b.id)
    assert stored is not None
    assert stored.paused_at == paused_at
    assert stored.paused_minutes_total == 0


async def test_tenancy_resume_policy_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine, monkeypatch
) -> None:
    """`_load_policy_in_org`'s predicate. Org A owns the row and the ticket,
    but the row's `policy_id` points at org B's POLICY -- again legal at the
    FK. Resume must refuse rather than re-derive A's deadlines against B's
    targets, which is how one tenant's promise silently becomes another's."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_a = await _seed_calendar(api_env, org_a, timezone=LONDON, windows=_ALWAYS)
    calendar_b = await _seed_calendar(api_env, org_b, timezone=LONDON, windows=_ALWAYS)
    policy_b = await _seed_policy(
        api_env, org_b, calendar_id=calendar_b, response_minutes=5, resolution_minutes=15
    )
    ticket_a = await _seed_ticket(api_env, org_a, created_at=MONDAY_0900)
    paused_at = MONDAY_0900 + timedelta(hours=1)
    await _foreign_row(
        api_env,
        owner_org=org_a,
        ticket_id=ticket_a.id,
        calendar_id=calendar_a,
        policy_id=policy_b,
        paused_at=paused_at,
    )

    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0900 + timedelta(hours=3))
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError, match="is not visible in that org"):
            await resume(session, org_a, ticket_a.id)

    stored = await _row(api_env, ticket_a.id)
    assert stored is not None
    assert stored.paused_at == paused_at
    assert stored.paused_minutes_total == 0
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60)


async def test_a_pause_on_one_org_does_not_touch_another_orgs_identical_ticket(
    api_env, monkeypatch
) -> None:
    """The ordinary-path counterpart to the constructed cross-org rows above:
    two orgs, each with its own ticket and its own SLA row, and a pause in one
    leaves the other untouched. Cheap, and it catches a predicate that filters
    on `ticket_id` alone in the common case where the ids differ."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    for org in (org_a, org_b):
        calendar_id = await _seed_calendar(api_env, org, timezone=LONDON, windows=_ALWAYS)
        await _seed_policy(api_env, org, calendar_id=calendar_id)

    ticket_a = await _seed_ticket(api_env, org_a, created_at=MONDAY_0900)
    ticket_b = await _seed_ticket(api_env, org_b, created_at=MONDAY_0900)
    await _start(api_env, org_a, ticket_a)
    await _start(api_env, org_b, ticket_b)

    await _pause_at(api_env, monkeypatch, org_a, ticket_a.id, MONDAY_0900 + timedelta(hours=1))
    await _resume_at(api_env, monkeypatch, org_a, ticket_a.id, MONDAY_0900 + timedelta(hours=2))

    row_a = await _row(api_env, ticket_a.id)
    row_b = await _row(api_env, ticket_b.id)
    assert row_a is not None and row_b is not None
    assert row_a.paused_minutes_total == 60
    assert row_b.paused_minutes_total == 0
    assert row_b.paused_at is None
    assert row_b.response_due_at == MONDAY_0900 + timedelta(minutes=60)
    assert row_b.resolution_due_at == MONDAY_0900 + timedelta(minutes=480)
