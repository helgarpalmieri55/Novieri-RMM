"""`ticket_sla` lifecycle: materialise at creation, recompute on re-triage
(docs/modules/sla/spec.md §0.3, §0.4, §2.5, §3; Task 5).

FIXTURE DISCIPLINE -- what these tests deliberately VARY. This sub-project has
repeatedly shipped tests that passed while proving nothing because every
fixture held constant the one parameter the defect would vary (Task 2's
Critical hid behind a business-hours calendar; Task 3's F1 behind a single
exception class; Task 4's F1 behind default-valued create arguments). The
parameters varied here, and the mutant each one catches:

* **`created_at` is always months in the past, never "now".** A recompute
  measured from `datetime.now(UTC)` -- the single most likely wrong
  implementation of §0.3, and the one that looks RIGHT -- lands hours or
  months away from every expected value below.
* **Calendar shape varies: 24/7, Mon-Fri 09:00-17:00, and a no-windows
  calendar.** On 24/7 alone, working-time arithmetic and a naive `timedelta`
  are indistinguishable; the Mon-Fri cases are what make the calendar
  load-bearing.
* **The two targets always differ, and the two clocks are stopped
  independently** -- met vs breached, response vs resolution, one at a time
  and both together. A guard that checks only `response_met_at`, or that
  treats the pair as one clock, fails at least one of them.
* **Both re-triage axes are exercised:** a priority change AND a queue-only
  change, and the queue-only case moves the ticket onto a policy with a
  DIFFERENT calendar, so a recompute that reuses `ticket_sla.calendar_id`
  instead of the newly resolved `policy.calendar_id` is caught.
* **`policy_source` is asserted on every write path**, including that nothing
  here ever writes `escalation` (§0.4).

Tenancy tests at the bottom run on a raw BYPASSRLS `admin_engine` session, so
the service function's own `org_id` predicate is the sole guard under test --
through `org_session` RLS would mask a deleted predicate and the test would
pass either way (the discipline `test_sla_policies.py` established).
"""

import asyncio
import re
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import Ticket, TicketStatusHistory
from app.modules.sla.errors import SlaValidationError, UnsatisfiableCalendar
from app.modules.sla.models import TicketSla
from app.modules.sla.tracking import POLICY_SOURCES, recompute_for_retriage, start_tracking
from tests.test_sla_migration import _seed_calendar, _seed_policy, _seed_queue, _seed_user

LONDON = "Europe/London"

# Mon-Fri 09:00-17:00: the shape that makes working time differ from elapsed
# time. `Europe/London` is on GMT (UTC+0) for every instant used below
# (BST 2026 starts 29 March), so local wall-clock == UTC in the expectations
# and no assertion here doubles as an unintended DST test -- Task 2 owns
# those.
_MON_FRI = [(d, time(9, 0), time(17, 0)) for d in range(5)]
# 00:00-00:00 seven days a week: spec §2.2's only legal encoding of a genuine
# 24-hour day (1440 minutes; `00:00-23:59` silently loses a minute a day).
# Here it makes working time == elapsed time, so a deadline is exactly
# `created_at + target` and the arithmetic is checkable by eye.
_ALWAYS = [(d, time(0, 0), time(0, 0)) for d in range(7)]

# A Monday 09:00 UTC, five months before "now" in any run of this suite.
MONDAY_0900 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
# A Friday 16:30 UTC -- 30 minutes before a Mon-Fri calendar closes, so any
# target over 30 minutes has to cross the weekend.
FRIDAY_1630 = datetime(2026, 3, 6, 16, 30, tzinfo=UTC)


async def _seed_ticket(
    api_env,
    org_id: UUID,
    *,
    queue_id: UUID | None = None,
    priority: str = "normal",
    created_at: datetime,
) -> Ticket:
    """Creates a ticket via the admin (RLS-bypassing) engine and returns the
    detached `Ticket` ORM object the tracking services take.

    `conftest.a_ticket_for` is not used here: it returns an `ATicket`
    (`id` + `requester_id` only) and gives no control over `priority`,
    `queue_id` or -- decisively for §0.3 -- `created_at`, which every
    expectation in this module is measured from.

    `created_at` is REQUIRED (no `now()` default) so that no test in this
    module can accidentally measure a deadline from a creation instant equal
    to "now", which is precisely the confusion §0.3 turns on.

    Side effects: one `users` row (via `_seed_user`), one `ticket_category`
    row, one `ticket` row, and -- only when `queue_id` is None -- one
    `ticket_queue` row.
    """
    from app.models import TicketCategory, TicketQueue

    actor = await _seed_user(api_env, org_id, email=f"sla-ticket-{uuid4().hex[:10]}@test.local")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            if queue_id is None:
                queue = TicketQueue(
                    org_id=org_id,
                    key=f"q-{uuid4().hex[:10]}",
                    name="Tracking queue",
                    created_by=actor,
                )
                session.add(queue)
                await session.flush()
                queue_id = queue.id
            category = TicketCategory(
                org_id=org_id,
                key=f"c-{uuid4().hex[:10]}",
                name="Tracking category",
                type="incident",
                default_queue_id=queue_id,
                created_by=actor,
            )
            session.add(category)
            await session.flush()
            ticket = Ticket(
                org_id=org_id,
                number=f"T-{uuid4().hex[:10]}",
                type="incident",
                category_id=category.id,
                queue_id=queue_id,
                status="new",
                priority=priority,
                subject="tracking",
                requester_id=actor,
                origin="portal",
                created_by=actor,
                created_at=created_at,
            )
            session.add(ticket)
            await session.flush()
        return ticket


async def _retriage(
    api_env, ticket: Ticket, *, priority: str | None = None, queue_id: UUID | None = None
) -> Ticket:
    """Applies a human re-triage to the stored ticket and returns the reloaded
    row -- so the recompute reads the ticket's real state, not a locally
    mutated copy that the database never saw."""
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            row = (await session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
            if priority is not None:
                row.priority = priority
            if queue_id is not None:
                row.queue_id = queue_id
        return row


async def _row(api_env, ticket_id: UUID) -> TicketSla | None:
    """The stored `ticket_sla` row read on the BYPASSRLS admin engine with NO
    org filter -- so a test asserting "no row was written" cannot be satisfied
    by a row that merely exists in another tenant."""
    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(TicketSla).where(TicketSla.ticket_id == ticket_id))
        ).scalar_one_or_none()


async def _row_count(api_env, org_id: UUID) -> int:
    """How many `ticket_sla` rows this org owns, on the admin engine. Scoped
    to the org because the test database is shared for the whole session --
    a whole-table count would be a running total of every other test's rows.
    Paired with `_row` above (which is NOT org-scoped) it distinguishes "no
    row was written" from "a row was written under the wrong org_id"."""
    async with AsyncSession(api_env.admin_engine) as session:
        return len(
            (await session.execute(select(TicketSla.id).where(TicketSla.org_id == org_id)))
            .scalars()
            .all()
        )


async def _stop_clocks(
    api_env,
    ticket_id: UUID,
    *,
    response_met_at: datetime | None = None,
    response_breached_at: datetime | None = None,
    resolution_met_at: datetime | None = None,
    resolution_breached_at: datetime | None = None,
) -> None:
    """Stamps met/breached columns directly (Tasks 6-8 own the services that
    will write them); the admin engine keeps this setup independent of
    anything not yet built."""
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            row = (
                await session.execute(select(TicketSla).where(TicketSla.ticket_id == ticket_id))
            ).scalar_one()
            row.response_met_at = response_met_at
            row.response_breached_at = response_breached_at
            row.resolution_met_at = resolution_met_at
            row.resolution_breached_at = resolution_breached_at


# ---------------------------------------------------------------------------
# start_tracking
# ---------------------------------------------------------------------------


async def test_start_tracking_materialises_both_deadlines_from_created_at(api_env) -> None:
    """The whole row, on a 24/7 calendar where working time == elapsed time so
    both deadlines are checkable by eye.

    The two targets differ (60 vs 480) and are asserted separately: a mutant
    writing `response_target_minutes` into both columns -- or `resolution`
    into both -- passes if only one is checked. `created_at` is five months
    in the past, so a deadline measured from `now` cannot land on either
    expectation.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    policy_id = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)

    before = datetime.now(UTC)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        tracking = await start_tracking(session, org_id, ticket)
    assert tracking is not None

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.org_id == org_id
    assert stored.ticket_id == ticket.id
    assert stored.policy_id == policy_id
    assert stored.calendar_id == calendar_id
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480)
    assert stored.policy_source == "initial"
    assert stored.response_met_at is None
    assert stored.resolution_met_at is None
    assert stored.response_breached_at is None
    assert stored.resolution_breached_at is None
    assert stored.paused_at is None
    assert stored.escalated_at is None
    assert before <= stored.last_recomputed_at <= datetime.now(UTC)


async def test_start_tracking_measures_the_target_on_the_business_calendar(api_env) -> None:
    """The same code path on a Mon-Fri 09:00-17:00 calendar, where the answer
    and a `created_at + timedelta` answer differ by two and a half days.

    Filed Friday 16:30 with a 240-minute response target: 30 minutes remain on
    Friday, the weekend counts for nothing, and the remaining 210 land on
    Monday at 12:30. A holiday on the Monday pushes the resolution target
    (480 minutes) onto the Tuesday -- so this test also proves the resolved
    policy's calendar HOLIDAYS reach the arithmetic, not merely its windows.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(
        api_env,
        org_id,
        timezone=LONDON,
        windows=_MON_FRI,
        holidays=[date(2026, 3, 9)],
    )
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=240, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=FRIDAY_1630)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        tracking = await start_tracking(session, org_id, ticket)

    assert tracking is not None
    # Fri 16:30-17:00 = 30, then Monday is a holiday, so the remaining 210
    # minutes fall on Tuesday 09:00-12:30.
    assert tracking.response_due_at == datetime(2026, 3, 10, 12, 30, tzinfo=UTC)
    # 30 on Friday + Tuesday's full 480-minute day is 510, so 480 lands at
    # Tuesday 16:30.
    assert tracking.resolution_due_at == datetime(2026, 3, 10, 16, 30, tzinfo=UTC)


async def test_start_tracking_writes_no_row_when_no_policy_matches(api_env) -> None:
    """Spec §3 / §10.4: no match means NO `ticket_sla` row -- not a zero, a
    default, or a silent infinity. Asserted on the whole table via the admin
    engine, so a row written under another org_id would still fail this."""
    org_id = await api_env.create_org()
    await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        tracking = await start_tracking(session, org_id, ticket)

    assert tracking is None
    assert await _row(api_env, ticket.id) is None
    assert await _row_count(api_env, org_id) == 0


async def test_start_tracking_twice_is_an_idempotent_no_op(api_env) -> None:
    """Task 5 brief item 5: `ticket_sla.ticket_id` is UNIQUE, so a second
    creation must not raise `IntegrityError` out of the service.

    The second call happens AFTER a more specific policy has been added, so
    this proves both halves of the chosen semantics: one row, and the
    existing promise is returned UNCHANGED rather than silently recomputed
    under today's policy (an unattributed rewrite is what §0.3/§0.4 forbid --
    only `recompute_for_retriage` may rewrite, and it stamps `policy_source`
    to say so).
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    wildcard_id = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, priority="high", created_at=MONDAY_0900)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        first = await start_tracking(session, org_id, ticket)
    assert first is not None

    # A more specific policy now exists; the second call must ignore it.
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="high",
        response_minutes=15,
        resolution_minutes=90,
    )
    async with org_session(org_id, engine=api_env.app_engine) as session:
        second = await start_tracking(session, org_id, ticket)

    assert second is not None
    assert second.id == first.id
    assert await _row_count(api_env, org_id) == 1
    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.policy_id == wildcard_id
    assert stored.policy_source == "initial"
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480)


async def test_start_tracking_returns_the_existing_row_even_if_no_policy_resolves_now(
    api_env,
) -> None:
    """The other half of the idempotent no-op, and the one the previous test
    cannot see: the second call must return the EXISTING row, not `None`,
    when the policy that produced it has since been retired.

    Without this, a `start_tracking` that skips the "already tracked?" read
    and relies on the INSERT's `ON CONFLICT` alone is indistinguishable --
    it returns the same row on the happy path but reports `None` (spec §3's
    "this ticket has no SLA") for a ticket that demonstrably does have one.
    A caller acting on that `None` would treat a tracked ticket as untracked.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    policy_id = await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        first = await start_tracking(session, org_id, ticket)
    assert first is not None

    # Retire the policy (spec §3 only ever considers `active` ones), so a
    # second call would resolve nothing at all.
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            await session.execute(
                text("UPDATE sla_policy SET active = false WHERE id = :id"), {"id": policy_id}
            )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        second = await start_tracking(session, org_id, ticket)

    assert second is not None, "an already-tracked ticket must not report itself untracked"
    assert second.id == first.id
    assert second.policy_id == policy_id
    assert second.policy_source == "initial"
    assert await _row_count(api_env, org_id) == 1


async def test_concurrent_start_tracking_yields_one_row_and_no_integrity_error(api_env) -> None:
    """The real race, two sessions (the `test_sla_policies.py` discipline).
    `ON CONFLICT DO NOTHING` + re-read must make the loser return the winner's
    row -- a plain INSERT here raises `IntegrityError` out of the service on
    the UNIQUE `ticket_id`, which is the failure item 5 rules out."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)
    barrier = asyncio.Barrier(2)

    async def attempt() -> UUID | None:
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await session.execute(select(TicketSla.id))
            await asyncio.wait_for(barrier.wait(), timeout=30)
            tracking = await start_tracking(session, org_id, ticket)
            return None if tracking is None else tracking.id

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)

    assert all(not isinstance(r, BaseException) for r in results), results
    assert await _row_count(api_env, org_id) == 1
    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert set(results) == {stored.id}


async def test_start_tracking_surfaces_an_unsatisfiable_calendar_and_writes_no_row(
    api_env,
) -> None:
    """A calendar with no windows cannot deliver any target (spec §4). The
    typed error must reach the caller -- and no partially materialised row may
    survive it."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=[])
    await _seed_policy(
        api_env, org_id, calendar_id=calendar_id, response_minutes=60, resolution_minutes=480
    )
    ticket = await _seed_ticket(api_env, org_id, created_at=MONDAY_0900)

    with pytest.raises(UnsatisfiableCalendar):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await start_tracking(session, org_id, ticket)

    assert await _row_count(api_env, org_id) == 0


async def test_start_tracking_rejects_a_ticket_from_another_org(api_env) -> None:
    """`ticket_sla.ticket_id`'s FK checks that the ticket exists, never that
    it belongs to `org_id` -- so a cross-org ticket would otherwise write this
    org's SLA row against another org's ticket."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_a = await _seed_calendar(api_env, org_a, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(api_env, org_a, calendar_id=calendar_a)
    ticket_b = await _seed_ticket(api_env, org_b, created_at=MONDAY_0900)

    async with org_session(org_a, engine=api_env.app_engine) as session:
        with pytest.raises(SlaValidationError, match="does not belong to org"):
            await start_tracking(session, org_a, ticket_b)

    assert await _row_count(api_env, org_a) == 0


# ---------------------------------------------------------------------------
# recompute_for_retriage
# ---------------------------------------------------------------------------


async def test_retriage_recomputes_from_ticket_start_and_comes_out_already_breached(
    api_env,
) -> None:
    """Spec §10.5 / §0.3, the module's central and most contested rule.

    A ticket filed `normal` (480-minute response) at Monday 09:00 is
    re-triaged to `urgent` (60-minute response) long afterwards. The deadline
    is recomputed from the TICKET's start, not from the moment of the change,
    so it lands at Monday 10:00 -- in the past, i.e. ALREADY BREACHED.

    That is the intended, documented outcome, not an off-by-something: the
    work was urgent all along and nobody treated it that way, so the breach is
    information rather than an artifact. `policy_source = 'retriage'` is what
    makes it attributable as a triage-quality problem rather than a
    response-time failure. The stamping of `response_breached_at` itself is
    the sweep's job (§7, Task 8); what Task 5 owes is a due-at in the past,
    asserted here against real wall-clock now.

    The mutant this kills: recomputing from `datetime.now(UTC)`, which is the
    plausible-looking implementation §0.3 explicitly rejects (miss the target,
    bump the priority, get a fresh clock, repeat). It would put the deadline
    an hour in the FUTURE and never breach.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_MON_FRI)
    normal_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=480,
        resolution_minutes=960,
    )
    urgent_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="urgent",
        response_minutes=60,
        resolution_minutes=240,
    )
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=MONDAY_0900)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        initial = await start_tracking(session, org_id, ticket)
    assert initial is not None
    assert initial.policy_id == normal_id
    assert initial.policy_source == "initial"
    # 480 working minutes from Monday 09:00 is Monday 17:00.
    assert initial.response_due_at == datetime(2026, 3, 2, 17, 0, tzinfo=UTC)

    retriaged_ticket = await _retriage(api_env, ticket, priority="urgent")
    before_recompute = datetime.now(UTC)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        recomputed = await recompute_for_retriage(session, org_id, retriaged_ticket)
    after_recompute = datetime.now(UTC)
    assert recomputed is not None

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.policy_id == urgent_id
    assert stored.policy_source == "retriage"
    assert stored.policy_source != "escalation"
    # Measured from the TICKET's start: Monday 09:00 + 60 working minutes.
    assert stored.response_due_at == datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
    assert stored.resolution_due_at == datetime(2026, 3, 2, 13, 0, tzinfo=UTC)
    # ... which is in the past. The ticket is already breached.
    now = datetime.now(UTC)
    assert stored.response_due_at < now
    assert stored.resolution_due_at < now
    assert stored.response_breached_at is None, "stamping the breach is the sweep's job (§7)"
    # `last_recomputed_at` must actually ADVANCE on a real recompute (F3):
    # deleting the assignment in `recompute_for_retriage` left this column
    # untested in the shipped suite -- only "must not move" (the both-stopped
    # test below) was pinned, never "must move" when it should.
    assert initial.last_recomputed_at < stored.last_recomputed_at
    assert before_recompute <= stored.last_recomputed_at <= after_recompute


async def test_retriage_recomputes_into_the_future_when_not_yet_breached(api_env) -> None:
    """The converse of the previous test (F2): every fixture in this module
    files `created_at` months in the past, so "recomputed and already
    breached" is the only direction the shipped suite ever observed -- true
    for right AND wrong implementations, since any deadline derived from a
    `created_at` that old lands in the past regardless of which target or
    calendar produced it. Here `created_at` is 5 minutes ago and the new
    target is 60 minutes, so the recomputed deadline must land 55 minutes in
    the FUTURE, `policy_source = 'retriage'`, and not breached.
    """
    org_id = await api_env.create_org()
    # 24/7 so working time == elapsed time and the expected due-at is exact
    # regardless of what wall-clock day/time this suite happens to run at.
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=500,
        resolution_minutes=1000,
    )
    urgent_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="urgent",
        response_minutes=60,
        resolution_minutes=600,
    )
    created_at = datetime.now(UTC) - timedelta(minutes=5)
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=created_at)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await start_tracking(session, org_id, ticket)

    retriaged = await _retriage(api_env, ticket, priority="urgent")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        recomputed = await recompute_for_retriage(session, org_id, retriaged)
    assert recomputed is not None

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.policy_id == urgent_id
    assert stored.policy_source == "retriage"
    # created_at + 60 working minutes == created_at + 60 minutes on a 24/7
    # calendar -- 55 minutes in the FUTURE, not the past.
    assert stored.response_due_at == created_at + timedelta(minutes=60)
    now = datetime.now(UTC)
    assert stored.response_due_at > now
    assert stored.resolution_due_at > now
    assert stored.response_breached_at is None
    assert stored.resolution_breached_at is None


async def test_retriage_keeps_a_met_response_deadline_and_moves_the_running_one(
    api_env,
) -> None:
    """A met clock has stopped for good; only the still-running one moves
    (spec §7). Response is stopped by `response_met_at` here, and the
    resolution clock alone recomputes."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=60,
        resolution_minutes=480,
    )
    urgent_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="urgent",
        response_minutes=30,
        resolution_minutes=120,
    )
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=MONDAY_0900)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await start_tracking(session, org_id, ticket)
    await _stop_clocks(api_env, ticket.id, response_met_at=MONDAY_0900 + timedelta(minutes=30))

    retriaged = await _retriage(api_env, ticket, priority="urgent")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await recompute_for_retriage(session, org_id, retriaged)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60), "met clock rewritten"
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=120)
    # `policy_id` moves to `urgent` even though `urgent`'s 30-minute response
    # target never produced `response_due_at` above (that's `normal`'s
    # 60-minute target, frozen at met time). This is F4, documented and
    # accepted, not overlooked: see `recompute_for_retriage`'s "KNOWN
    # LIMITATION" docstring paragraph for why the row's single `policy_id`
    # cannot correctly describe both clocks once they diverge, and why this
    # is the less-wrong of the two available choices.
    assert stored.policy_id == urgent_id
    assert stored.policy_source == "retriage"


async def test_retriage_keeps_a_breached_response_deadline_and_moves_the_running_one(
    api_env,
) -> None:
    """F1: the `response_breached_at`-alone combination. Every fixture in the
    shipped suite paired `response` with `met_at` and `resolution` with
    `breached_at`, so `response_breached_at is None` was never independently
    proven -- deleting that guard clause left all 19 tests green. Response is
    stopped by a BREACH here (not a met), resolution keeps running."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=60,
        resolution_minutes=480,
    )
    urgent_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="urgent",
        response_minutes=30,
        resolution_minutes=120,
    )
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=MONDAY_0900)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await start_tracking(session, org_id, ticket)
    await _stop_clocks(
        api_env, ticket.id, response_breached_at=MONDAY_0900 + timedelta(minutes=30)
    )

    retriaged = await _retriage(api_env, ticket, priority="urgent")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await recompute_for_retriage(session, org_id, retriaged)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60), (
        "breached clock rewritten"
    )
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=120)
    assert stored.policy_id == urgent_id
    assert stored.policy_source == "retriage"


async def test_retriage_keeps_a_breached_resolution_deadline_and_moves_the_running_one(
    api_env,
) -> None:
    """The mirror image of the previous test, varying BOTH parameters at once:
    the stopped clock is the RESOLUTION one, and it is stopped by a BREACH
    rather than by being met.

    Task 3's finding F1 was exactly this shape -- two tests that both used one
    value of the parameter the defect varied. A guard written as
    `if tracking.response_met_at is None` passes the previous test and fails
    here.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=60,
        resolution_minutes=480,
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="urgent",
        response_minutes=30,
        resolution_minutes=120,
    )
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=MONDAY_0900)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await start_tracking(session, org_id, ticket)
    await _stop_clocks(
        api_env, ticket.id, resolution_breached_at=MONDAY_0900 + timedelta(minutes=500)
    )

    retriaged = await _retriage(api_env, ticket, priority="urgent")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await recompute_for_retriage(session, org_id, retriaged)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=30)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480), (
        "breached clock rewritten"
    )
    assert stored.policy_source == "retriage"


async def test_retriage_keeps_a_met_resolution_deadline_and_moves_the_running_one(
    api_env,
) -> None:
    """F1: the `resolution_met_at`-alone combination -- the last of the four
    column-to-clock pairings the shipped suite never constructed. The
    resolution clock is stopped by being MET (not breached) here, and the
    response clock alone recomputes."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=60,
        resolution_minutes=480,
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="urgent",
        response_minutes=30,
        resolution_minutes=120,
    )
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=MONDAY_0900)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await start_tracking(session, org_id, ticket)
    await _stop_clocks(api_env, ticket.id, resolution_met_at=MONDAY_0900 + timedelta(minutes=400))

    retriaged = await _retriage(api_env, ticket, priority="urgent")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await recompute_for_retriage(session, org_id, retriaged)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=30)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480), "met clock rewritten"
    assert stored.policy_source == "retriage"


async def test_retriage_leaves_a_row_with_both_clocks_stopped_completely_untouched(
    api_env,
) -> None:
    """Nothing is running, so nothing is recomputed -- and `policy_id` /
    `policy_source` stay put too. Re-attributing a finished promise to a
    policy that never governed it would destroy the very §0.3 distinction the
    column exists for."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    normal_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=60,
        resolution_minutes=480,
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="urgent",
        response_minutes=30,
        resolution_minutes=120,
    )
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=MONDAY_0900)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await start_tracking(session, org_id, ticket)
    await _stop_clocks(
        api_env,
        ticket.id,
        response_met_at=MONDAY_0900 + timedelta(minutes=30),
        resolution_breached_at=MONDAY_0900 + timedelta(minutes=500),
    )
    before = await _row(api_env, ticket.id)
    assert before is not None
    snapshot = (
        before.policy_id,
        before.calendar_id,
        before.policy_source,
        before.response_due_at,
        before.resolution_due_at,
        before.last_recomputed_at,
    )

    retriaged = await _retriage(api_env, ticket, priority="urgent")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        returned = await recompute_for_retriage(session, org_id, retriaged)
    assert returned is not None

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert (
        stored.policy_id,
        stored.calendar_id,
        stored.policy_source,
        stored.response_due_at,
        stored.resolution_due_at,
        stored.last_recomputed_at,
    ) == snapshot
    assert stored.policy_id == normal_id
    assert stored.policy_source == "initial"


async def test_retriage_follows_a_queue_move_onto_that_queues_own_calendar(api_env) -> None:
    """The other re-triage axis (§3: "a priority or queue change"), with the
    priority held constant so only the queue can explain the new policy.

    The destination queue's policy uses a DIFFERENT calendar -- 24/7 rather
    than Mon-Fri -- so this also catches a recompute that reuses the stored
    `ticket_sla.calendar_id` instead of the newly resolved
    `policy.calendar_id`: on the old calendar the answer would still be the
    following Monday.
    """
    org_id = await api_env.create_org()
    office_cal = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_MON_FRI)
    always_cal = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    queue_office = await _seed_queue(api_env, org_id)
    queue_noc = await _seed_queue(api_env, org_id)
    office_policy = await _seed_policy(
        api_env,
        org_id,
        calendar_id=office_cal,
        queue_id=queue_office,
        response_minutes=240,
        resolution_minutes=600,
    )
    noc_policy = await _seed_policy(
        api_env,
        org_id,
        calendar_id=always_cal,
        queue_id=queue_noc,
        response_minutes=120,
        resolution_minutes=300,
    )
    ticket = await _seed_ticket(
        api_env, org_id, queue_id=queue_office, priority="normal", created_at=FRIDAY_1630
    )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        initial = await start_tracking(session, org_id, ticket)
    assert initial is not None
    assert initial.policy_id == office_policy
    assert initial.response_due_at == datetime(2026, 3, 9, 12, 30, tzinfo=UTC)

    moved = await _retriage(api_env, ticket, queue_id=queue_noc)
    assert moved.priority == "normal"
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await recompute_for_retriage(session, org_id, moved)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.policy_id == noc_policy
    assert stored.calendar_id == always_cal
    assert stored.policy_source == "retriage"
    assert stored.response_due_at == FRIDAY_1630 + timedelta(minutes=120)
    assert stored.resolution_due_at == FRIDAY_1630 + timedelta(minutes=300)


async def test_retriage_materialises_tracking_that_did_not_exist_yet(api_env) -> None:
    """A ticket filed when nothing matched (§3: no row at all) and re-triaged
    into a priority that now has a policy. Spec §3 runs resolution on human
    re-triage, so the SLA appears -- from the ticket's start, and sourced
    `retriage` because that is WHY this policy applies."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=MONDAY_0900)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert await start_tracking(session, org_id, ticket) is None
    assert await _row_count(api_env, org_id) == 0

    urgent_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="urgent",
        response_minutes=45,
        resolution_minutes=180,
    )
    retriaged = await _retriage(api_env, ticket, priority="urgent")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        created = await recompute_for_retriage(session, org_id, retriaged)

    assert created is not None
    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.policy_id == urgent_id
    assert stored.policy_source == "retriage"
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=45)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=180)


async def test_retriage_onto_a_pending_ticket_credits_the_wait_since_it_entered_pending(
    api_env,
) -> None:
    """F4 (review sla-breach-wiring): `_materialise`'s born-paused stamp must
    be the instant the ticket ENTERED the pausing status, read from
    `ticket_status_history`, not the instant materialisation happens to run.

    This is the retriage half of the born-paused story
    (`test_sla_wiring.py::test_a_request_created_into_pending_approval_is_born_paused`
    covers the creation half, where "now" and "entered the status" are the
    same instant to within a flush and the bug is invisible). Here the ticket
    entered `pending` TWO DAYS before any policy resolved for it -- no
    `ticket_sla` row exists yet (§3: no match, no row) -- and only THEN does a
    retriage land it under a policy. Stamping `paused_at = now` at that
    retriage would credit only the instant of the retriage and silently
    discard the two days already spent blocked on someone who is not us,
    which is exactly the understated credit F4 flags.
    """
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    entered_pending_at = MONDAY_0900 + timedelta(days=2)
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=MONDAY_0900)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            row = (await session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
            row.status = "pending"
            session.add(
                TicketStatusHistory(
                    org_id=org_id,
                    ticket_id=ticket.id,
                    from_status="new",
                    to_status="pending",
                    actor_type="system",
                    actor_id=None,
                    created_at=entered_pending_at,
                )
            )

    async with org_session(org_id, engine=api_env.app_engine) as session:
        assert await start_tracking(session, org_id, ticket) is None
    assert await _row_count(api_env, org_id) == 0

    urgent_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="urgent",
        response_minutes=45,
        resolution_minutes=180,
    )
    retriaged = await _retriage(api_env, ticket, priority="urgent")
    assert retriaged.status == "pending"

    async with org_session(org_id, engine=api_env.app_engine) as session:
        created = await recompute_for_retriage(session, org_id, retriaged)

    assert created is not None
    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.policy_id == urgent_id
    assert stored.paused_at == entered_pending_at


async def test_retriage_into_an_unpoliced_level_leaves_the_promise_intact(api_env) -> None:
    """Re-triage that resolves to NO policy neither deletes nor rewrites the
    existing row: a promise already made is not withdrawn because the ticket
    moved somewhere unpoliced, and the row records what was promised."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    normal_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=60,
        resolution_minutes=480,
    )
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=MONDAY_0900)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await start_tracking(session, org_id, ticket)

    retriaged = await _retriage(api_env, ticket, priority="low")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        returned = await recompute_for_retriage(session, org_id, retriaged)
    assert returned is not None

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert await _row_count(api_env, org_id) == 1
    assert stored.policy_id == normal_id
    assert stored.policy_source == "initial"
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480)


async def test_retriage_surfaces_an_unsatisfiable_calendar_and_leaves_the_row_untouched(
    api_env,
) -> None:
    """F6: `UnsatisfiableCalendar` (spec §4) was only ever asserted out of
    `start_tracking` in the shipped suite; nothing called `recompute_for_retriage`
    against a window-less calendar. A ticket is created under a working
    calendar (both clocks running), then re-triaged into a policy whose
    calendar has no windows at all -- the typed error must propagate, and the
    row already on disk must survive unmodified rather than being partially
    overwritten."""
    org_id = await api_env.create_org()
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    normal_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=60,
        resolution_minutes=480,
    )
    empty_calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=[])
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=empty_calendar_id,
        priority="urgent",
        response_minutes=30,
        resolution_minutes=120,
    )
    ticket = await _seed_ticket(api_env, org_id, priority="normal", created_at=MONDAY_0900)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await start_tracking(session, org_id, ticket)

    retriaged = await _retriage(api_env, ticket, priority="urgent")
    with pytest.raises(UnsatisfiableCalendar):
        async with org_session(org_id, engine=api_env.app_engine) as session:
            await recompute_for_retriage(session, org_id, retriaged)

    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.policy_id == normal_id
    assert stored.policy_source == "initial"
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480)


async def test_retriage_rejects_a_ticket_from_another_org(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_a = await _seed_calendar(api_env, org_a, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(api_env, org_a, calendar_id=calendar_a)
    ticket_b = await _seed_ticket(api_env, org_b, created_at=MONDAY_0900)

    async with org_session(org_a, engine=api_env.app_engine) as session:
        with pytest.raises(SlaValidationError, match="does not belong to org"):
            await recompute_for_retriage(session, org_a, ticket_b)

    assert await _row_count(api_env, org_a) == 0


async def test_policy_sources_agree_with_the_database_check_constraint(api_env) -> None:
    """`POLICY_SOURCES` and `ck_ticket_sla_policy_source_valid` are two
    statements of one domain (spec §2.5). Read the live constraint rather than
    the model's Python copy: a value added to one and not the other is either
    a write that 500s at the database or a legal value the module refuses."""
    async with AsyncSession(api_env.admin_engine) as session:
        definition = (
            await session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_ticket_sla_policy_source_valid'"
                )
            )
        ).scalar_one()

    assert set(re.findall(r"'([a-z_]+)'", definition)) == set(POLICY_SOURCES)
    assert POLICY_SOURCES == ("initial", "retriage", "escalation")


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session (module docstring).
# RLS masks the application predicate through org_session; these tests remove
# RLS from the equation entirely, so `_load_tracking`'s `org_id` predicate is
# the only thing standing between the caller and another tenant's row.
# ---------------------------------------------------------------------------


async def _foreign_tracking_row(api_env, owner_org: UUID, ticket_id: UUID) -> UUID:
    """A `ticket_sla` row owned by `owner_org` but pointing at another org's
    ticket. The FK permits it and no application path produces it (
    `_require_same_org` rejects the cross-org ticket first); constructed
    directly on the BYPASSRLS admin session, exactly as
    `test_sla_policies.py` constructs its cross-org fixtures."""
    calendar_id = await _seed_calendar(api_env, owner_org, timezone=LONDON, windows=_ALWAYS)
    policy_id = await _seed_policy(api_env, owner_org, calendar_id=calendar_id)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            row = TicketSla(
                org_id=owner_org,
                ticket_id=ticket_id,
                policy_id=policy_id,
                calendar_id=calendar_id,
                response_due_at=MONDAY_0900 + timedelta(hours=1),
                resolution_due_at=MONDAY_0900 + timedelta(hours=8),
                policy_source="initial",
                last_recomputed_at=MONDAY_0900,
            )
            session.add(row)
            await session.flush()
            return row.id


async def test_tenancy_start_tracking_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """With the `org_id` predicate removed from `_load_tracking`, org A's
    `start_tracking` finds org B's row for this ticket and RETURNS IT -- a
    cross-tenant read of another org's promise. With the predicate, A sees
    nothing, its INSERT hits the UNIQUE `ticket_id`, and the typed error
    below is raised instead."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_a = await _seed_calendar(api_env, org_a, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(api_env, org_a, calendar_id=calendar_a)
    ticket_a = await _seed_ticket(api_env, org_a, created_at=MONDAY_0900)
    foreign_id = await _foreign_tracking_row(api_env, org_b, ticket_a.id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError, match="not visible in org"):
            await start_tracking(session, org_a, ticket_a)

    stored = await _row(api_env, ticket_a.id)
    assert stored is not None
    assert stored.id == foreign_id
    assert stored.org_id == org_b
    assert await _row_count(api_env, org_a) == 0
    assert await _row_count(api_env, org_b) == 1


async def test_tenancy_recompute_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """The same predicate on the recompute path, where removing it is worse:
    org A would not merely READ org B's row, it would rewrite B's deadlines
    and `policy_source` under A's policy."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    calendar_a = await _seed_calendar(api_env, org_a, timezone=LONDON, windows=_ALWAYS)
    await _seed_policy(
        api_env, org_a, calendar_id=calendar_a, response_minutes=15, resolution_minutes=90
    )
    ticket_a = await _seed_ticket(api_env, org_a, created_at=MONDAY_0900)
    foreign_id = await _foreign_tracking_row(api_env, org_b, ticket_a.id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError, match="not visible in org"):
            await recompute_for_retriage(session, org_a, ticket_a)

    stored = await _row(api_env, ticket_a.id)
    assert stored is not None
    assert stored.id == foreign_id
    assert stored.org_id == org_b
    assert stored.policy_source == "initial"
    assert stored.response_due_at == MONDAY_0900 + timedelta(hours=1)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(hours=8)
