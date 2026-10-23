"""Breach marking and one-shot escalation (docs/modules/sla/spec.md §0.4,
§0.5, §6, §6.0, §6.0.1, §8, §10.6, §10.7; Task 9).

§10.6 IS THE FILE'S REASON TO EXIST. §0.3 recomputes both deadlines from
ticket creation on a priority change; escalation raises priority. Composed,
they loop: a breached `normal` ticket escalates to `high`, recomputes against
a shorter target it is already older than, breaches again in the same second,
escalates to `urgent`, breaches again -- three escalations and three
notifications for one ticket, terminating only at the ceiling. The queue half
is the same cascade through a second door, because `resolve_policy` keys on
`(queue, priority)` and the escalation queue has its own, tighter policy.

**WHAT MAKES THE TWO CASCADE TESTS ACTUALLY BITE.** A test that escalates a
ticket and asserts the deadlines are unchanged proves nothing unless the
policy the escalation moves the ticket ONTO has DIFFERENT targets from the one
it was under. With a single wildcard policy in the fixture, a mutant that
recomputes after escalation re-resolves to the very same policy, re-derives
the very same deadlines from the very same `created_at`, and the assertion
passes. That is exactly the failure this sub-project keeps repeating -- a
fixture holding constant the parameter the defect varies (Task 2's calendar,
Task 3's exception class, Task 4's escalation defaults, Task 5's column-to-
clock pairing, Task 6's author identity, Task 7's stop column). So
`_priority_cascade_env` seeds a `high` policy at 15/120 beside the `normal`
policy at 60/480, `_queue_cascade_env` seeds a policy on the escalation queue
at 15/120 beside the wildcard at 60/480, and BOTH cascade tests assert the
would-be recomputed instants explicitly and assert the stored ones are not
them. Removing the guard turns both red.

THE OTHER PARAMETERS THIS FILE VARIES, and the mutant each one catches:

* **Which target breached.** Every stamping, event and idempotence test is
  parametrized over `response` and `resolution`, and asserts the OTHER
  column is untouched -- Task 5's F1 was a constant column-to-clock pairing,
  and `breach.py` keys the breach column, the due column and the running
  predicate off `target` in three parallel maps precisely so a mismatched
  pair is expressible and therefore testable.
* **Which halves the policy enables** -- all four of
  (priority, queue), (priority only), (queue only), (neither). A fixture
  that always enabled both would pass an implementation that ignored the
  flags entirely; one that always enabled priority would pass an
  implementation that routed the queue move through `update_ticket` (which
  now raises) or dropped it silently, which is the Task 8 review's F1.
* **The ticket's starting priority**, every rung: `low`, `normal`, `high`
  and -- decisively -- `urgent`, where the ladder has no next step. An
  `urgent` fixture is the only one that distinguishes "escalate one step"
  from "escalate to the top", and the only one that can catch a wrap-around
  back to `low`.
* **The ticket's status**, terminal and not: `closed`, `cancelled`,
  `rejected` and the ordinary working statuses. `closed` is the sharp case --
  `assign_ticket` permits it and `update_ticket` refuses it -- so a queue-only
  escalation on a `closed` ticket is the exact shape that half-applies.
* **The ticket's type.** `rejected` is a request-only status and `resolved`
  an incident-only one (`ck_ticket_status_matches_type`), so an
  incident-only fixture cannot reach `rejected` at all.
* **`created_at` is always months in the past**, inherited from
  `test_sla_tracking.py`: a deadline this module accidentally recomputed from
  "now" could not land on an untouched expectation by luck.

TIME IS FROZEN at every call site (`breach._utcnow` monkeypatched), because
the assertions are on the exact stored instant -- a tolerance-based assertion
cannot distinguish "stamped now" from "re-stamped now" in the idempotence
tests.

Tenancy tests run on a raw BYPASSRLS `admin_engine` session, so each service's
own `org_id` predicate is the sole guard under test -- through `org_session`,
RLS would mask a deleted predicate and the test would pass either way (the
discipline `test_sla_policies.py` established).
"""

import re
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session

# `app.models` first, before any `app.modules.*` import -- importing an SLA
# service module directly would otherwise start the ORM registry from inside
# `app.modules.service_desk.models` and hit a partially-initialised module
# (`test_sla_tracking.py`/`test_sla_met.py` order their imports the same way
# for the same reason).
from app.models import AuditEvent, DomainEvent, Ticket
from app.modules.service_desk.errors import TicketNotFoundError
from app.modules.sla import breach as breach_module
from app.modules.sla.breach import PRIORITY_LADDER, mark_breached
from app.modules.sla.errors import SlaValidationError
from app.modules.sla.models import TicketSla
from app.modules.sla.tracking import start_tracking
from tests.test_sla_migration import _seed_calendar, _seed_policy, _seed_queue, _seed_user
from tests.test_sla_tracking import _ALWAYS, LONDON, MONDAY_0900, _row, _stop_clocks

# The instant every breach in this file is stamped at. Well after
# MONDAY_0900 + any target used here, and frozen so "stamped now" and
# "re-stamped now" are distinguishable.
BREACH_AT = MONDAY_0900 + timedelta(days=3)
# A second, later instant used to prove a re-mark does NOT move a stamp.
LATER = BREACH_AT + timedelta(days=1)

# The event payload contract, shared with the notification module
# (`delivery.py::_render_ticket_sla_breached`, `routing.resolve_recipients`,
# and `test_notification_delivery.py`'s pinned wire-format sample). Asserted
# as an exact set: `requester_id` must never appear (§6.1), and a key silently
# dropped is as much a contract break as a key added.
BREACH_PAYLOAD_KEYS = {
    "ticket_id",
    "ticket_number",
    "subject",
    "target",
    "due_at",
    "policy_id",
    "queue_id",
    "priority",
}

TARGETS = ("response", "resolution")

# Per-target expectations, so a test can enumerate the pairing rather than
# hardcode one half of it (see the module docstring on Task 5's F1).
_BREACH_COLUMN = {"response": "response_breached_at", "resolution": "resolution_breached_at"}
_OTHER_BREACH_COLUMN = {"response": "resolution_breached_at", "resolution": "response_breached_at"}
_DUE_COLUMN = {"response": "response_due_at", "resolution": "resolution_due_at"}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


async def _seed_ticket(
    api_env,
    org_id: UUID,
    *,
    queue_id: UUID | None = None,
    priority: str = "normal",
    status: str = "new",
    type_: str = "incident",
    created_at: datetime = MONDAY_0900,
) -> Ticket:
    """A ticket with FULL control of `priority`, `status`, `type` and
    `queue_id`, written on the BYPASSRLS admin engine.

    `test_sla_tracking._seed_ticket` is not reused: it fixes `status="new"`
    and `type="incident"`, and both are parameters this module's defects vary
    (a terminal ticket must not escalate; `rejected` is a request-only status
    that an incident fixture cannot reach at all -- `ck_ticket_status_matches_
    type`). Statuses are set directly rather than through `change_status`
    because the transition table would refuse most of the shortcuts these
    tests need, and this module's behaviour depends on the STORED status, not
    on how the ticket got there.

    Side effects: one `users` row (via `_seed_user`), one `ticket_category`
    row, one `ticket` row, and -- only when `queue_id` is None -- one
    `ticket_queue` row.
    """
    from app.models import TicketCategory, TicketQueue

    actor = await _seed_user(api_env, org_id, email=f"sla-breach-{uuid4().hex[:10]}@test.local")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            if queue_id is None:
                queue = TicketQueue(
                    org_id=org_id,
                    key=f"q-{uuid4().hex[:10]}",
                    name="Breach queue",
                    created_by=actor,
                )
                session.add(queue)
                await session.flush()
                queue_id = queue.id
            category = TicketCategory(
                org_id=org_id,
                key=f"c-{uuid4().hex[:10]}",
                name="Breach category",
                type=type_,
                default_queue_id=queue_id,
                created_by=actor,
            )
            session.add(category)
            await session.flush()
            ticket = Ticket(
                org_id=org_id,
                number=f"INC-{uuid4().hex[:8]}",
                type=type_,
                category_id=category.id,
                queue_id=queue_id,
                status=status,
                priority=priority,
                subject="breach subject",
                requester_id=actor,
                origin="portal",
                created_by=actor,
                created_at=created_at,
            )
            session.add(ticket)
            await session.flush()
        return ticket


async def _track(api_env, org_id: UUID, ticket: Ticket) -> TicketSla:
    """Materialise the ticket's `ticket_sla` row through the real service, so
    every deadline under test was produced by `tracking.start_tracking` from
    `ticket.created_at` -- never hand-written into the column this module is
    accused of rewriting."""
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        row = await start_tracking(session, org_id, ticket)
        await session.commit()
    assert row is not None
    stored = await _row(api_env, ticket.id)
    assert stored is not None
    return stored


async def _mark(
    api_env,
    org_id: UUID,
    ticket_sla: TicketSla,
    *,
    target: str,
    at: datetime = BREACH_AT,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run `mark_breached` in its own committed transaction with `_utcnow`
    frozen at `at`."""
    monkeypatch.setattr(breach_module, "_utcnow", lambda: at)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        row = (
            await session.execute(select(TicketSla).where(TicketSla.id == ticket_sla.id))
        ).scalar_one()
        await mark_breached(session, org_id, row, target=target)  # type: ignore[arg-type]
        await session.commit()


async def _ticket_row(api_env, ticket_id: UUID) -> Ticket:
    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()


async def _events(api_env, org_id: UUID, event_type: str) -> list[DomainEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(DomainEvent)
                    .where(DomainEvent.org_id == org_id, DomainEvent.event_type == event_type)
                    .order_by(DomainEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def _audit(api_env, org_id: UUID, *, action: str | None = None) -> list[AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        stmt = select(AuditEvent).where(AuditEvent.org_id == org_id)
        if action is not None:
            stmt = stmt.where(AuditEvent.action == action)
        return list((await session.execute(stmt.order_by(AuditEvent.id))).scalars().all())


class _Env:
    """What every test needs to assert against, in one object: the org, the
    ticket, the policy that produced the stored deadlines, and (when the
    fixture seeds one) the escalation queue plus the policy that would apply
    if the escalation recomputed."""

    def __init__(
        self,
        org_id: UUID,
        ticket: Ticket,
        tracking: TicketSla,
        policy_id: UUID,
        calendar_id: UUID,
        *,
        escalation_queue_id: UUID | None = None,
        rival_response_minutes: int | None = None,
        rival_resolution_minutes: int | None = None,
    ) -> None:
        self.org_id = org_id
        self.ticket = ticket
        self.tracking = tracking
        self.policy_id = policy_id
        self.calendar_id = calendar_id
        self.escalation_queue_id = escalation_queue_id
        self.rival_response_minutes = rival_response_minutes
        self.rival_resolution_minutes = rival_resolution_minutes


async def _env(
    api_env,
    *,
    priority: str = "normal",
    status: str = "new",
    type_: str = "incident",
    escalate_priority: bool = False,
    escalate_to_queue: bool = False,
    escalate_to_own_queue: bool = False,
    response_minutes: int = 60,
    resolution_minutes: int = 480,
) -> _Env:
    """The ordinary fixture: one 24/7 calendar (so working time == elapsed
    time and every deadline is checkable by eye), one WILDCARD policy, one
    tracked ticket.

    The wildcard policy is deliberate here and deliberately NOT used by the
    two cascade tests, which need a rival policy for the escalated
    (queue, priority) to resolve to -- see the module docstring.
    """
    org_id = await api_env.create_org(f"breach-{uuid4().hex[:8]}")
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    ticket = await _seed_ticket(
        api_env, org_id, priority=priority, status=status, type_=type_
    )
    escalation_queue_id: UUID | None = None
    if escalate_to_own_queue:
        escalation_queue_id = ticket.queue_id
    elif escalate_to_queue:
        escalation_queue_id = await _seed_queue(api_env, org_id)
    policy_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        response_minutes=response_minutes,
        resolution_minutes=resolution_minutes,
        escalate_priority=escalate_priority,
        escalate_to_queue_id=escalation_queue_id,
    )
    tracking = await _track(api_env, org_id, ticket)
    return _Env(
        org_id,
        ticket,
        tracking,
        policy_id,
        calendar_id,
        escalation_queue_id=escalation_queue_id,
    )


async def _priority_cascade_env(api_env) -> _Env:
    """§10.6's priority fixture. Two policies whose targets DIFFER, selected
    by priority: `normal` at 60/480 (the one the ticket is under) and `high`
    at 15/120 (the one a recompute after escalation would land on).

    Without the rival policy this fixture would hold constant the very
    parameter the cascade varies, and the loop-guard test would pass against
    a mutant that recomputes.
    """
    org_id = await api_env.create_org(f"breach-casc-{uuid4().hex[:8]}")
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    ticket = await _seed_ticket(api_env, org_id, priority="normal")
    policy_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=60,
        resolution_minutes=480,
        escalate_priority=True,
    )
    # The rival. Never resolved by this module -- only by a mutant that
    # re-resolves after raising the priority.
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="high",
        response_minutes=15,
        resolution_minutes=120,
    )
    tracking = await _track(api_env, org_id, ticket)
    assert tracking.policy_id == policy_id
    return _Env(
        org_id,
        ticket,
        tracking,
        policy_id,
        calendar_id,
        rival_response_minutes=15,
        rival_resolution_minutes=120,
    )


async def _queue_cascade_env(api_env) -> _Env:
    """§10.6's queue fixture -- §6.0.1's "same cascade through a second door".

    A wildcard policy at 60/480 governs the ticket; a policy scoped to the
    ESCALATION QUEUE at 15/120 is what `resolve_policy` would pick up after
    the move (queue-only beats the both-wildcard level, §3). `escalate_
    priority` is False, so the only thing that changes here is the queue --
    which is the whole point: a recompute-after-escalation mutant does not
    need a priority change to cascade.
    """
    org_id = await api_env.create_org(f"breach-qcasc-{uuid4().hex[:8]}")
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    ticket = await _seed_ticket(api_env, org_id, priority="normal")
    escalation_queue_id = await _seed_queue(api_env, org_id)
    policy_id = await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        response_minutes=60,
        resolution_minutes=480,
        escalate_to_queue_id=escalation_queue_id,
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        queue_id=escalation_queue_id,
        response_minutes=15,
        resolution_minutes=120,
    )
    tracking = await _track(api_env, org_id, ticket)
    assert tracking.policy_id == policy_id
    return _Env(
        org_id,
        ticket,
        tracking,
        policy_id,
        calendar_id,
        escalation_queue_id=escalation_queue_id,
        rival_response_minutes=15,
        rival_resolution_minutes=120,
    )


# ---------------------------------------------------------------------------
# stamping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", TARGETS)
async def test_mark_breached_stamps_only_that_targets_column(
    api_env, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """The breached target's column is stamped and the OTHER target's is not.

    Both are asserted, in both directions, because `breach.py` reaches its
    breach column, its due column and its running predicate through three
    parallel per-target maps: a mutant that stamps `response_breached_at`
    whatever the caller asked for passes any test that only exercises
    `response`. Neither `*_met_at` moves, and neither `*_due_at` moves --
    breach records that a promise was missed, it does not restate the promise.
    """
    env = await _env(api_env)

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert getattr(stored, _BREACH_COLUMN[target]) == BREACH_AT
    assert getattr(stored, _OTHER_BREACH_COLUMN[target]) is None
    assert stored.response_met_at is None
    assert stored.resolution_met_at is None
    assert stored.response_due_at == MONDAY_0900 + timedelta(minutes=60)
    assert stored.resolution_due_at == MONDAY_0900 + timedelta(minutes=480)
    # No escalation configured on this policy, so nothing was escalated and
    # the attribution still says the promise came from creation.
    assert stored.escalated_at is None
    assert stored.policy_source == "initial"


@pytest.mark.parametrize("target", TARGETS)
async def test_marking_the_same_target_twice_stamps_and_publishes_once(
    api_env, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """§10.7's idempotence for the stamp and the event: an at-least-once sweep
    that sees the same row twice must not move the stamp forward or fire a
    second notification. The second call is made at a LATER frozen instant, so
    "did not re-stamp" is distinguishable from "re-stamped with the same
    value"."""
    env = await _env(api_env)

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)
    await _mark(
        api_env, env.org_id, env.tracking, target=target, at=LATER, monkeypatch=monkeypatch
    )

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert getattr(stored, _BREACH_COLUMN[target]) == BREACH_AT
    assert len(await _events(api_env, env.org_id, "ticket.sla_breached")) == 1


@pytest.mark.parametrize("target", TARGETS)
async def test_a_met_clock_is_never_marked_breached(
    api_env, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """A clock stopped ON TIME is met and must never be breached.

    A reply that landed a second before the sweep ran KEPT the promise.
    Stamping a breach over it would fire a breach notification for a promise
    nobody broke. The met stamp itself is left exactly where it was.

    "On time" is the operative word and it is what
    `test_a_late_met_clock_is_breached_and_the_boundary_is_inclusive` below
    varies: this fixture's met stamp is 5 minutes in, comfortably inside both
    the 60- and the 480-minute target, so it says nothing at all about the
    late case. The two tests are a pair and neither is complete alone.
    """
    env = await _env(api_env)
    met_at = MONDAY_0900 + timedelta(minutes=5)
    await _stop_clocks(
        api_env,
        env.ticket.id,
        response_met_at=met_at if target == "response" else None,
        resolution_met_at=met_at if target == "resolution" else None,
    )

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert getattr(stored, _BREACH_COLUMN[target]) is None
    assert await _events(api_env, env.org_id, "ticket.sla_breached") == []


# ---------------------------------------------------------------------------
# a clock stopped AFTER its deadline: met AND breached
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "expect_breach"),
    [
        (timedelta(seconds=-1), False),
        (timedelta(0), False),
        (timedelta(seconds=1), True),
    ],
    ids=["a-second-early", "exactly-on-the-deadline", "a-second-late"],
)
@pytest.mark.parametrize("target", TARGETS)
async def test_a_late_met_clock_is_breached_and_the_boundary_is_inclusive(
    api_env,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    offset: timedelta,
    expect_breach: bool,
) -> None:
    """THE PARAMETER THIS FILE PREVIOUSLY HELD CONSTANT: where `met_at` sits
    relative to `due_at`.

    Every met fixture in this file and in `test_sla_sweep.py` placed the met
    stamp comfortably BEFORE the deadline (5 minutes in, 1 minute in), so
    "already met" and "met on time" produced identical observable values
    everywhere and the guard could be either one. §7's sweep was widened to
    select a row whose clock stopped after its deadline -- `met_at IS NULL OR
    met_at > due_at` -- while `mark_breached` still asked
    `tracking._response_running`, which a late-met row also fails. The row was
    therefore selected and skipped on every 300-second cycle, forever, and no
    test in the suite could see it because none of them varied this.

    THREE POINTS, NOT TWO, AND THE MIDDLE ONE IS THE POINT. `met_at ==
    due_at` is where the off-by-one lives: `met_at > due_at` and `met_at >=
    due_at` both look correct read in isolation, they differ on exactly one
    instant, and only a fixture that lands on that instant can tell them
    apart. A promise of "within 60 minutes" is KEPT by a reply at minute 60
    exactly, so the boundary is met -- which is also what makes the Python
    guard the exact complement of the sweep's strict SQL `>`.

    Parametrized over the target as well, against its OWN due column: the
    response deadline is 60 minutes out and the resolution one 480, so a
    mutant that compared either clock's `met_at` to the other's `due_at` gets
    the answer wrong on four of these six cases.

    The met stamp must survive either way. `met_at` records WHEN contact
    happened and `breached_at` records THAT the promise was broken; a late
    reply carries both, and neither is derivable from the other.
    """
    env = await _env(api_env)
    stored_before = await _row(api_env, env.ticket.id)
    assert stored_before is not None
    due_at: datetime = getattr(stored_before, _DUE_COLUMN[target])
    met_at = due_at + offset
    await _stop_clocks(api_env, env.ticket.id, **{f"{target}_met_at": met_at})

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert getattr(stored, _BREACH_COLUMN[target]) == (BREACH_AT if expect_breach else None)
    # Whatever happened to the breach column, the met stamp is untouched.
    assert getattr(stored, f"{target}_met_at") == met_at
    assert getattr(stored, _OTHER_BREACH_COLUMN[target]) is None
    events = await _events(api_env, env.org_id, "ticket.sla_breached")
    assert [event.payload["target"] for event in events] == ([target] if expect_breach else [])


async def test_lateness_is_judged_per_clock_from_one_shared_met_instant(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WHICH OF THE TWO CLOCKS IS LATE -- one row, one instant, two answers.

    Both met columns are stamped at the SAME instant, 61 minutes after the
    ticket was filed. That instant is one minute LATE for the 60-minute
    response target and 419 minutes EARLY for the 480-minute resolution one.
    So the two clocks must disagree about themselves, from identical inputs.

    This is the fixture that cannot be satisfied by a row-level predicate.
    Anything that asks "is this ROW late" -- one comparison, one answer,
    however it picks its columns -- breaches both targets or neither. So does
    a mismatched column-to-clock pairing (Task 5's F1): comparing
    `response_met_at` against `resolution_due_at` reads the response clock as
    early, and comparing `resolution_met_at` against `response_due_at` reads
    the resolution clock as late. Each of those four mutants gets at least one
    of these two assertions wrong, and none of them is visible to a fixture
    that uses a different met instant per target.
    """
    env = await _env(api_env)
    met_at = MONDAY_0900 + timedelta(minutes=61)
    await _stop_clocks(
        api_env, env.ticket.id, response_met_at=met_at, resolution_met_at=met_at
    )

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)
    await _mark(
        api_env, env.org_id, env.tracking, target="resolution", at=LATER, monkeypatch=monkeypatch
    )

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.response_breached_at == BREACH_AT, "a reply one minute late kept nothing"
    assert stored.resolution_breached_at is None, "419 minutes inside the target is not a breach"
    assert stored.response_met_at == met_at
    assert stored.resolution_met_at == met_at
    events = await _events(api_env, env.org_id, "ticket.sla_breached")
    assert [event.payload["target"] for event in events] == ["response"]


@pytest.mark.parametrize("target", TARGETS)
async def test_a_late_met_clock_that_already_breached_is_not_re_stamped(
    api_env, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """§10.7's idempotence, on the row shape that now carries BOTH stamps.

    Widening the guard from "is this clock running" to "was this promise
    broken" removes `met_at` from the idempotence argument entirely: after the
    first mark a late-met row reads met AND breached, so `breached_at IS NULL`
    is the ONLY thing left standing between an at-least-once sweep and a
    second stamp, a second `ticket.sla_breached` event and a second
    escalation. A guard written as "not met on time" without the
    already-breached half passes every other test in this file and fails only
    here.

    The second mark runs at a LATER frozen instant, so a re-stamp MOVES the
    recorded value rather than merely repeating it -- an equality assertion
    against `BREACH_AT` catches it either way.
    """
    env = await _env(api_env, escalate_priority=True)
    stored_before = await _row(api_env, env.ticket.id)
    assert stored_before is not None
    met_at: datetime = getattr(stored_before, _DUE_COLUMN[target]) + timedelta(minutes=1)
    await _stop_clocks(api_env, env.ticket.id, **{f"{target}_met_at": met_at})

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)
    await _mark(
        api_env, env.org_id, env.tracking, target=target, at=LATER, monkeypatch=monkeypatch
    )

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert getattr(stored, _BREACH_COLUMN[target]) == BREACH_AT
    assert getattr(stored, f"{target}_met_at") == met_at
    assert stored.escalated_at == BREACH_AT
    assert len(await _events(api_env, env.org_id, "ticket.sla_breached")) == 1
    assert len(await _audit(api_env, env.org_id, action="sla.escalated")) == 1
    assert (await _ticket_row(api_env, env.ticket.id)).priority == "high"


@pytest.mark.parametrize("target", TARGETS)
async def test_a_late_met_breach_publishes_and_escalates_like_any_other(
    api_env, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """A late-met breach is a BREACH, not a quiet column write.

    It is the full §6 path -- the stamp, the `ticket.sla_breached` event
    naming this target and this target's `due_at`, the priority bump, the
    `sla.escalated` audit row. Asserted because the cheap way to make the
    late-met row stop re-selecting is to stamp `breached_at` and nothing else,
    which would silence the loop and lose the notification the breach exists
    to produce.

    The event's `due_at` is asserted against the target's own due column, so
    a stamp that reached the right row through the wrong pairing still fails.
    """
    env = await _env(api_env, escalate_priority=True)
    stored_before = await _row(api_env, env.ticket.id)
    assert stored_before is not None
    due_at: datetime = getattr(stored_before, _DUE_COLUMN[target])
    await _stop_clocks(api_env, env.ticket.id, **{f"{target}_met_at": due_at + timedelta(hours=2)})

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)

    events = await _events(api_env, env.org_id, "ticket.sla_breached")
    assert len(events) == 1
    assert set(events[0].payload) == BREACH_PAYLOAD_KEYS
    assert events[0].payload["target"] == target
    assert events[0].payload["due_at"] == due_at.isoformat()
    assert (await _ticket_row(api_env, env.ticket.id)).priority == "high"
    assert len(await _audit(api_env, env.org_id, action="sla.escalated")) == 1


async def test_the_two_targets_breach_independently(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response breach does not stop the resolution clock breaching later,
    and each publishes its own event naming its own target and its own
    due-at. This is the shape a sweep actually produces on a badly-missed
    ticket."""
    env = await _env(api_env)

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)
    await _mark(
        api_env, env.org_id, env.tracking, target="resolution", at=LATER, monkeypatch=monkeypatch
    )

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.response_breached_at == BREACH_AT
    assert stored.resolution_breached_at == LATER

    events = await _events(api_env, env.org_id, "ticket.sla_breached")
    assert [e.payload["target"] for e in events] == ["response", "resolution"]
    assert events[0].payload["due_at"] == (MONDAY_0900 + timedelta(minutes=60)).isoformat()
    assert events[1].payload["due_at"] == (MONDAY_0900 + timedelta(minutes=480)).isoformat()


async def test_an_unknown_target_is_refused(api_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typed error, not a silent no-op: a caller that mistypes the target
    would otherwise have its breach quietly not recorded."""
    env = await _env(api_env)

    async with org_session(
        env.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        row = (
            await session.execute(select(TicketSla).where(TicketSla.id == env.tracking.id))
        ).scalar_one()
        with pytest.raises(SlaValidationError, match="unknown SLA target"):
            await mark_breached(session, env.org_id, row, target="reponse")  # type: ignore[arg-type]

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.response_breached_at is None
    assert stored.resolution_breached_at is None


# ---------------------------------------------------------------------------
# the event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", TARGETS)
async def test_the_breach_event_matches_the_pinned_payload_contract(
    api_env, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """The whole payload, key set asserted exactly.

    The shape is shared with the notification module -- `delivery.py`'s
    renderer reads `target`/`due_at`/`ticket_number`/`subject`,
    `routing.resolve_recipients` routes on `queue_id`, and
    `test_notification_delivery.py` pins the same key set as a wire-format
    sample. An extra key or a missing one is a contract break there, not
    here, so it is asserted here where the producer lives.
    """
    env = await _env(api_env)

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)

    events = await _events(api_env, env.org_id, "ticket.sla_breached")
    assert len(events) == 1
    event = events[0]
    assert event.subject_type == "ticket"
    assert event.subject_id == env.ticket.id
    # A sweep has no human actor -- inventing one would put a fake person in
    # the record (`publish`'s own docstring).
    assert event.actor_user_id is None
    assert set(event.payload) == BREACH_PAYLOAD_KEYS
    assert event.payload["ticket_id"] == str(env.ticket.id)
    assert event.payload["ticket_number"] == env.ticket.number
    assert event.payload["subject"] == "breach subject"
    assert event.payload["target"] == target
    stored_due = MONDAY_0900 + timedelta(minutes=60 if target == "response" else 480)
    assert event.payload["due_at"] == stored_due.isoformat()
    assert event.payload["policy_id"] == str(env.policy_id)
    assert event.payload["queue_id"] == str(env.ticket.queue_id)
    assert event.payload["priority"] == "normal"


async def test_the_breach_payload_carries_no_requester_id(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.1: the breach mail is written staff-facing, and this payload is what
    makes it structurally unroutable to the requester --
    `routing.resolve_recipients` resolves `recipient_kind="requester"` from
    `payload["requester_id"]`, and there is none.

    The ticket really does HAVE a requester, asserted here, so the absence is
    a property of the payload rather than of the fixture.
    """
    env = await _env(api_env)
    assert env.ticket.requester_id is not None

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    (event,) = await _events(api_env, env.org_id, "ticket.sla_breached")
    assert "requester_id" not in event.payload
    assert str(env.ticket.requester_id) not in str(event.payload)


async def test_the_event_describes_the_ticket_as_it_breached_not_as_escalation_left_it(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`queue_id` and `priority` in the payload are the PRE-escalation values.

    Not cosmetic: `routing.resolve_recipients` matches rules on
    `payload["queue_id"]` and expands `recipient_kind="queue_members"` from
    it, so publishing the post-escalation queue would notify the escalation
    queue's members about a breach they did not incur and leave the team that
    actually missed the deadline hearing nothing at all.
    """
    env = await _env(api_env, escalate_priority=True, escalate_to_queue=True)
    original_queue_id = env.ticket.queue_id

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == "high"
    assert ticket.queue_id == env.escalation_queue_id

    (event,) = await _events(api_env, env.org_id, "ticket.sla_breached")
    assert event.payload["priority"] == "normal"
    assert event.payload["queue_id"] == str(original_queue_id)


# ---------------------------------------------------------------------------
# §10.6 -- THE CASCADE GUARD. Both of these must fail if the guard is removed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", TARGETS)
async def test_escalating_a_breached_ticket_does_not_recompute_deadlines(
    api_env, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """§10.6/§0.4, the priority door. **This test must fail if the guard is
    removed.**

    The fixture holds a `normal` policy at 60/480 and a rival `high` policy at
    15/120, so recomputing after the bump is OBSERVABLE: it would move the
    resolution deadline from `created_at + 480m` to `created_at + 120m` and
    the response deadline from `+60m` to `+15m`. Both would-be values are
    asserted NOT to be what is stored, so the test cannot pass because the two
    policies happened to agree.

    And the recomputed deadlines are both in the past relative to the breach
    instant, which is the cascade itself: the ticket would breach again the
    moment the next sweep looked at it, escalate to `urgent`, and breach
    again.
    """
    env = await _priority_cascade_env(api_env)
    expected_response = MONDAY_0900 + timedelta(minutes=60)
    expected_resolution = MONDAY_0900 + timedelta(minutes=480)
    would_be_response = MONDAY_0900 + timedelta(minutes=env.rival_response_minutes or 0)
    would_be_resolution = MONDAY_0900 + timedelta(minutes=env.rival_resolution_minutes or 0)
    assert would_be_response != expected_response
    assert would_be_resolution != expected_resolution

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)

    # The escalation really happened -- otherwise "deadlines unchanged" is
    # trivially true and proves nothing.
    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == "high"

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.response_due_at == expected_response
    assert stored.resolution_due_at == expected_resolution
    assert stored.response_due_at != would_be_response
    assert stored.resolution_due_at != would_be_resolution
    # The attribution §0.4 requires, and the policy the deadlines came from,
    # left pointing where it was.
    assert stored.policy_source == "escalation"
    assert stored.policy_id == env.policy_id
    assert stored.calendar_id == env.calendar_id
    assert stored.escalated_at == BREACH_AT
    # One breach, one notification -- not the burst §0.4 describes.
    assert len(await _events(api_env, env.org_id, "ticket.sla_breached")) == 1


@pytest.mark.parametrize("target", TARGETS)
async def test_a_queue_move_escalation_does_not_recompute_deadlines(
    api_env, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """§10.6/§6.0.1, the second door -- the more dangerous half.

    Nothing about the priority changes here. The ticket simply moves to the
    escalation queue, and `resolve_policy` keys on `(queue, priority)`, so a
    recompute would pick up the escalation queue's own policy (15/120, a
    queue-scoped level-2 match that beats the wildcard the ticket is under)
    and produce exactly the same instant re-breach. **This test must fail if
    the guard is removed.**
    """
    env = await _queue_cascade_env(api_env)
    expected_response = MONDAY_0900 + timedelta(minutes=60)
    expected_resolution = MONDAY_0900 + timedelta(minutes=480)
    would_be_response = MONDAY_0900 + timedelta(minutes=env.rival_response_minutes or 0)
    would_be_resolution = MONDAY_0900 + timedelta(minutes=env.rival_resolution_minutes or 0)
    assert would_be_response != expected_response
    assert would_be_resolution != expected_resolution

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.queue_id == env.escalation_queue_id
    assert ticket.priority == "normal"

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.response_due_at == expected_response
    assert stored.resolution_due_at == expected_resolution
    assert stored.response_due_at != would_be_response
    assert stored.resolution_due_at != would_be_resolution
    assert stored.policy_source == "escalation"
    assert stored.policy_id == env.policy_id
    assert stored.calendar_id == env.calendar_id
    assert stored.escalated_at == BREACH_AT
    assert len(await _events(api_env, env.org_id, "ticket.sla_breached")) == 1


# ---------------------------------------------------------------------------
# escalation mechanics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "expected"),
    [("low", "normal"), ("normal", "high"), ("high", "urgent")],
)
async def test_escalation_raises_the_priority_exactly_one_step(
    api_env, monkeypatch: pytest.MonkeyPatch, start: str, expected: str
) -> None:
    """Every rung of the ladder, because a fixture pinned to one starting
    priority cannot tell "one step up" from "jump to `high`"."""
    env = await _env(api_env, priority=start, escalate_priority=True)

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == expected


async def test_urgent_does_not_escalate_past_the_ceiling(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6: `urgent` is the top. Nothing is applied, so `escalated_at` stays
    NULL and `policy_source` still reads `initial` -- the latch records that
    an escalation HAPPENED, not that one was considered. A wrap-around mutant
    would demote the fleet's most urgent work on breach, and this is the only
    fixture that can see it."""
    env = await _env(api_env, priority="urgent", escalate_priority=True)

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == "urgent"
    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.escalated_at is None
    assert stored.policy_source == "initial"
    assert await _audit(api_env, env.org_id, action="sla.escalated") == []
    # The breach itself still happened -- the ceiling suppresses escalation,
    # not the record of the missed promise.
    assert stored.response_breached_at == BREACH_AT
    assert len(await _events(api_env, env.org_id, "ticket.sla_breached")) == 1


async def test_an_urgent_ticket_still_moves_queue_when_the_policy_says_so(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling stops the PRIORITY half only. An implementation that
    returned early on `urgent` would silently drop the queue move as well,
    and no fixture that enables both halves on a non-`urgent` ticket can see
    that."""
    env = await _env(
        api_env, priority="urgent", escalate_priority=True, escalate_to_queue=True
    )

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == "urgent"
    assert ticket.queue_id == env.escalation_queue_id
    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.escalated_at == BREACH_AT
    assert stored.policy_source == "escalation"


@pytest.mark.parametrize(
    ("escalate_priority", "escalate_to_queue"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_the_policys_two_escalation_switches_are_honoured_independently(
    api_env, monkeypatch: pytest.MonkeyPatch, escalate_priority: bool, escalate_to_queue: bool
) -> None:
    """All four configurations. Task 4's F1 was precisely a fixture that left
    these two fields at their defaults on every call, so an implementation
    that ignored them entirely -- or that always did both -- passed.

    `escalated_at` is set iff SOMETHING was applied, which is also the
    `(False, False)` row's assertion: a policy with escalation switched off
    records a breach and nothing else.
    """
    env = await _env(
        api_env, escalate_priority=escalate_priority, escalate_to_queue=escalate_to_queue
    )
    original_queue_id = env.ticket.queue_id

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == ("high" if escalate_priority else "normal")
    assert ticket.queue_id == (env.escalation_queue_id if escalate_to_queue else original_queue_id)

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    applied = escalate_priority or escalate_to_queue
    assert (stored.escalated_at == BREACH_AT) is applied
    assert stored.policy_source == ("escalation" if applied else "initial")
    assert len(await _audit(api_env, env.org_id, action="sla.escalated")) == (1 if applied else 0)


async def test_the_queue_move_goes_through_assign_ticket_not_update_ticket(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.0's table, read off the immutable audit log.

    `queue_id` is absent from `TICKET_EDITABLE_FIELDS` because service-desk
    §5 gates a queue move on `ticket.assign` rather than `ticket.write` and
    files it under `ticket.assigned`; `update_ticket` now RAISES on it rather
    than silently dropping it, which before the Task 8 review made the queue
    half report success while doing nothing. A queue-only escalation must
    therefore produce a `ticket.assigned` row and NO `ticket.updated` row at
    all -- and both audit rows must be system-actor with no invented user.
    """
    env = await _env(api_env, escalate_to_queue=True)

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    assigned = await _audit(api_env, env.org_id, action="ticket.assigned")
    assert len(assigned) == 1
    assert assigned[0].actor_type == "system"
    assert assigned[0].actor_id is None
    assert assigned[0].meta["queue_id"] == str(env.escalation_queue_id)
    assert await _audit(api_env, env.org_id, action="ticket.updated") == []


async def test_the_priority_bump_goes_through_update_ticket(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of §6.0's table: a priority-only escalation produces a
    `ticket.updated` row naming `priority` as the changed field, and NO
    `ticket.assigned` row -- the ticket did not move."""
    env = await _env(api_env, escalate_priority=True)

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    updated = await _audit(api_env, env.org_id, action="ticket.updated")
    assert len(updated) == 1
    assert updated[0].actor_type == "system"
    assert updated[0].actor_id is None
    assert updated[0].meta["changed_fields"] == ["priority"]
    assert await _audit(api_env, env.org_id, action="ticket.assigned") == []


async def test_an_escalation_queue_the_ticket_is_already_in_is_not_re_applied(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A policy whose `escalate_to_queue_id` is the ticket's own queue is a
    no-op, not a self-move: `assign_ticket` would otherwise write a
    `ticket.assigned` audit row and publish a `ticket.assigned` event for a
    change that did not happen, and every sweep-triggered breach on that queue
    would look like a reassignment to anyone reading the log."""
    env = await _env(api_env, escalate_to_own_queue=True)

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.escalated_at is None
    assert stored.policy_source == "initial"
    assert await _audit(api_env, env.org_id, action="ticket.assigned") == []
    assert await _events(api_env, env.org_id, "ticket.assigned") == []


async def test_escalation_is_audited_with_the_target_and_the_old_and_new_values(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.0.1/§8, invariant #4. `ticket.updated` records only a changed-field
    NAME and `ticket.assigned` only the new queue, so neither says which SLA
    target breached nor what the value was before -- which is exactly what an
    escalation dispute needs. This row carries both."""
    env = await _env(api_env, escalate_priority=True, escalate_to_queue=True)
    original_queue_id = env.ticket.queue_id

    await _mark(api_env, env.org_id, env.tracking, target="resolution", monkeypatch=monkeypatch)

    (row,) = await _audit(api_env, env.org_id, action="sla.escalated")
    assert row.actor_type == "system"
    assert row.actor_id is None
    assert row.target_type == "ticket"
    assert row.target_id == str(env.ticket.id)
    assert row.meta == {
        "ticket_number": env.ticket.number,
        "target": "resolution",
        "policy_id": str(env.policy_id),
        "old_priority": "normal",
        "new_priority": "high",
        "old_queue_id": str(original_queue_id),
        "new_queue_id": str(env.escalation_queue_id),
    }


async def test_a_priority_only_escalation_audits_the_queue_as_unchanged(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`old_*`/`new_*` are the real before/after values and are EQUAL when
    that half did not apply, so a reader diffs the row rather than trusting a
    separate boolean to stay in agreement with them."""
    env = await _env(api_env, escalate_priority=True)

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    (row,) = await _audit(api_env, env.org_id, action="sla.escalated")
    assert row.meta["old_priority"] == "normal"
    assert row.meta["new_priority"] == "high"
    assert row.meta["old_queue_id"] == str(env.ticket.queue_id)
    assert row.meta["new_queue_id"] == str(env.ticket.queue_id)


# ---------------------------------------------------------------------------
# §10.7 -- once per ticket, not once per sweep
# ---------------------------------------------------------------------------


async def test_escalation_happens_once_when_the_same_target_is_marked_twice(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10.7. One escalation, one priority step, one audit row -- however
    often an at-least-once sweep sees the row."""
    env = await _env(api_env, escalate_priority=True)

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)
    await _mark(
        api_env, env.org_id, env.tracking, target="response", at=LATER, monkeypatch=monkeypatch
    )

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == "high"
    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.escalated_at == BREACH_AT
    assert len(await _audit(api_env, env.org_id, action="sla.escalated")) == 1


async def test_escalation_happens_once_across_the_two_targets(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harder half of §10.7, and the reason `escalated_at` lives on the
    row rather than being derived from the breach columns: the two clocks
    breach independently, so a response breach that escalated `normal -> high`
    must NOT escalate again to `urgent` when the resolution deadline passes an
    hour later. Both breaches are recorded; only one escalation is."""
    env = await _env(api_env, escalate_priority=True)

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)
    await _mark(
        api_env, env.org_id, env.tracking, target="resolution", at=LATER, monkeypatch=monkeypatch
    )

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == "high"
    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.response_breached_at == BREACH_AT
    assert stored.resolution_breached_at == LATER
    assert stored.escalated_at == BREACH_AT
    assert len(await _audit(api_env, env.org_id, action="sla.escalated")) == 1
    assert len(await _events(api_env, env.org_id, "ticket.sla_breached")) == 2


# ---------------------------------------------------------------------------
# terminal tickets -- the two services disagree about what "too late" means
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("escalate_priority", "escalate_to_queue"),
    [(True, False), (False, True), (True, True)],
)
async def test_a_closed_ticket_is_breached_and_published_but_never_escalated(
    api_env,
    monkeypatch: pytest.MonkeyPatch,
    escalate_priority: bool,
    escalate_to_queue: bool,
) -> None:
    """The decision this module makes about §6.0's closing note, tested across
    every escalation configuration, for the one `TERMINAL_STATUSES` member
    that is NOT also in `FINAL_STATUSES`.

    `closed` is the sharp case: `assign_ticket` would have ALLOWED a queue
    move on it (it only refuses `FINAL_STATUSES`), so an implementation that
    gated escalation on `FINAL_STATUSES`, or that let the two services police
    themselves, would move a closed ticket's queue here and then raise on the
    priority half in the `(closed, both)` cell -- a half-applied escalation
    whose visible result depends on which switches an admin happened to set.
    `TERMINAL_STATUSES` is the union of the two refusals and therefore the
    only set on which both halves agree.

    The breach is still stamped and still published: a missed promise on a
    ticket that later closed is real history and staff-facing information,
    and suppressing it would under-report breaches -- unlike `cancelled`/
    `rejected` below, `closed` never had its promise voided, so there is
    nothing for `mark_breached`'s F1 guard to refuse. `escalated_at` stays
    NULL because nothing was escalated.
    """
    env = await _env(
        api_env,
        status="closed",
        type_="incident",
        escalate_priority=escalate_priority,
        escalate_to_queue=escalate_to_queue,
    )
    original_queue_id = env.ticket.queue_id

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == "normal"
    assert ticket.queue_id == original_queue_id
    assert ticket.status == "closed"

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.response_breached_at == BREACH_AT
    assert stored.escalated_at is None
    assert stored.policy_source == "initial"
    assert len(await _events(api_env, env.org_id, "ticket.sla_breached")) == 1
    assert await _audit(api_env, env.org_id, action="sla.escalated") == []
    assert await _audit(api_env, env.org_id, action="ticket.updated") == []
    assert await _audit(api_env, env.org_id, action="ticket.assigned") == []


@pytest.mark.parametrize(("status", "type_"), [("cancelled", "incident"), ("rejected", "request")])
@pytest.mark.parametrize("target", TARGETS)
async def test_a_final_status_ticket_is_never_marked_breached_even_without_voided_at(
    api_env, monkeypatch: pytest.MonkeyPatch, status: str, type_: str, target: str
) -> None:
    """F1/F2 (review sla-breach-wiring): `mark_breached`'s defence-in-depth
    half. `voided_at` is NULL here -- this fixture writes the ticket directly
    into `cancelled`/`rejected` the same way the rest of this file does,
    never through `tracking.note_cancelled` -- which is exactly the shape of
    every ticket cancelled or rejected BEFORE migration 0038 added the
    column: `voided_at IS NOT NULL` alone cannot exclude it. `mark_breached`
    must still refuse the stamp, the publish and the escalation for it,
    because the ticket's own status is in `FINAL_STATUSES` regardless of
    whether `voided_at` was ever written.

    `closed` is deliberately absent from this parametrization -- see the
    control test above, which asserts the opposite for it.
    """
    env = await _env(
        api_env, status=status, type_=type_, escalate_priority=True, escalate_to_queue=True
    )
    original_queue_id = env.ticket.queue_id
    stored_before = await _row(api_env, env.ticket.id)
    assert stored_before is not None
    assert stored_before.voided_at is None

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == "normal"
    assert ticket.queue_id == original_queue_id

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert getattr(stored, _BREACH_COLUMN[target]) is None
    assert stored.escalated_at is None
    assert await _events(api_env, env.org_id, "ticket.sla_breached") == []
    assert await _audit(api_env, env.org_id, action="sla.escalated") == []


@pytest.mark.parametrize("target", TARGETS)
async def test_a_voided_promise_is_never_marked_breached_or_published(
    api_env, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """F1 (review sla-breach-wiring), the primary guard: a ticket cancelled
    through the REAL `tracking.note_cancelled` path -- `voided_at` genuinely
    stamped, exactly as `transitions.change_status` leaves it on its
    `FINAL_STATUSES` branch -- must come out of `mark_breached` neither
    stamped, nor published, nor escalated. `note_cancelled`'s own docstring
    says a voided promise is neither met nor breached; this is that guarantee
    holding all the way through the function whose whole job is to mark one.

    This is the REALISTIC shape (status genuinely `cancelled`, reached by the
    same hook `transitions.change_status` calls), so both of `mark_breached`'s
    guards agree here -- `voided_at is not None` and `ticket.status in
    FINAL_STATUSES` are both true. Removing either one alone therefore does
    NOT turn this specific test red; the test below isolates the `voided_at`
    guard on its own, and
    `test_a_final_status_ticket_is_never_marked_breached_even_without_voided_at`
    above isolates the status guard on its own. This test's job is to prove
    the REAL production path -- not a synthetic corner -- comes out right.
    """
    from app.modules.sla.tracking import note_cancelled

    env = await _env(api_env, status="cancelled", type_="incident")
    async with org_session(
        env.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        await note_cancelled(session, env.org_id, env.ticket.id)
        await session.commit()

    stored_before = await _row(api_env, env.ticket.id)
    assert stored_before is not None
    assert stored_before.voided_at is not None

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert getattr(stored, _BREACH_COLUMN[target]) is None
    assert stored.escalated_at is None
    assert stored.voided_at == stored_before.voided_at
    assert await _events(api_env, env.org_id, "ticket.sla_breached") == []
    assert await _audit(api_env, env.org_id, action="sla.escalated") == []


@pytest.mark.parametrize("target", TARGETS)
async def test_the_voided_at_guard_alone_refuses_a_breach_on_a_non_terminal_status(
    api_env, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """Isolates F1's PRIMARY guard (`tracking.voided_at is not None`) from
    `mark_breached`'s second, defence-in-depth `FINAL_STATUSES` check, so
    removing just the `voided_at` guard is provably what turns this test red
    -- the sibling test above cannot distinguish the two because a real
    cancellation sets both `voided_at` and the terminal status together.

    `voided_at` set on an otherwise NON-terminal ticket is not a state the
    ticket core can produce today (`note_cancelled` is wired only to
    `change_status`'s `FINAL_STATUSES` branch, and nothing reopens a voided
    row) -- it is constructed directly here, the same way `_stop_clocks`
    constructs states the ticket core cannot reach, precisely so this test
    exercises ONE guard at a time rather than the two that happen to move
    together on the realistic path.
    """
    env = await _env(api_env, status="new", type_="incident")
    voided_at = MONDAY_0900 + timedelta(hours=1)
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            row = (
                await session.execute(
                    select(TicketSla).where(TicketSla.id == env.tracking.id)
                )
            ).scalar_one()
            row.voided_at = voided_at

    await _mark(api_env, env.org_id, env.tracking, target=target, monkeypatch=monkeypatch)

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.status == "new"

    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert getattr(stored, _BREACH_COLUMN[target]) is None
    assert stored.escalated_at is None
    assert stored.voided_at == voided_at
    assert await _events(api_env, env.org_id, "ticket.sla_breached") == []
    assert await _audit(api_env, env.org_id, action="sla.escalated") == []


@pytest.mark.parametrize(
    ("status", "type_"),
    [("new", "incident"), ("open", "incident"), ("pending", "incident"),
     ("in_progress", "request"), ("pending_approval", "request")],
)
async def test_a_non_terminal_ticket_still_escalates(
    api_env, monkeypatch: pytest.MonkeyPatch, status: str, type_: str
) -> None:
    """The control for the test above. Without it, "a terminal ticket is not
    escalated" is equally satisfied by an implementation that never escalates
    anything -- the whole-fixture form of the constant-parameter mistake this
    file exists to avoid. Both ticket types are represented, because
    `ck_ticket_status_matches_type` keeps the two status sets apart."""
    env = await _env(
        api_env, status=status, type_=type_, escalate_priority=True, escalate_to_queue=True
    )

    await _mark(api_env, env.org_id, env.tracking, target="response", monkeypatch=monkeypatch)

    ticket = await _ticket_row(api_env, env.ticket.id)
    assert ticket.priority == "high"
    assert ticket.queue_id == env.escalation_queue_id
    stored = await _row(api_env, env.ticket.id)
    assert stored is not None
    assert stored.escalated_at == BREACH_AT


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------


async def test_priority_ladder_matches_the_policy_priority_check_constraint(
    api_env, admin_engine: AsyncEngine
) -> None:
    """The ladder and `ck_sla_policy_priority_valid` describe the same domain
    of priorities, read from the LIVE constraint rather than from a second
    copy of the list in this file. A priority added to the schema and not to
    the ladder is one `_next_priority` raise away from breaking every
    escalation on it, and the ladder is also ORDERED -- so this asserts the
    order too, low to high."""
    async with AsyncSession(admin_engine) as session:
        definition = (
            await session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_sla_policy_priority_valid'"
                )
            )
        ).scalar_one()

    # The CHECK's exact rendering varies with the server version (ANY(ARRAY[])
    # vs a chain of ORs, with or without `::text` casts), so the priorities are
    # extracted rather than string-matched as a whole.
    assert set(re.findall(r"'([a-z_]+)'", definition)) == set(PRIORITY_LADDER)
    # Ordered, low to high -- `_next_priority` indexes into it, so the order is
    # behaviour, not presentation.
    assert PRIORITY_LADDER == ("low", "normal", "high", "urgent")


async def test_an_off_ladder_priority_is_refused_rather_than_guessed(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ticket.priority` carries no DB CHECK (only `sla_policy.priority`
    does) and `update_ticket` takes a `dict[str, Any]`, so a value off the
    ladder is reachable by a service caller even though the API schema refuses
    it. There is no defensible answer to "one step above `p1`", and both
    silent alternatives are worse: treating it as the ceiling means such a
    ticket can never escalate and nothing ever says so, and guessing an index
    escalates it to a priority nobody defined."""
    env = await _env(api_env, priority="normal", escalate_priority=True)
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            row = (
                await session.execute(select(Ticket).where(Ticket.id == env.ticket.id))
            ).scalar_one()
            row.priority = "p1"

    monkeypatch.setattr(breach_module, "_utcnow", lambda: BREACH_AT)
    async with org_session(
        env.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        tracking = (
            await session.execute(select(TicketSla).where(TicketSla.id == env.tracking.id))
        ).scalar_one()
        with pytest.raises(SlaValidationError, match="PRIORITY_LADDER"):
            await mark_breached(session, env.org_id, tracking, target="response")


# ---------------------------------------------------------------------------
# cross-org references
# ---------------------------------------------------------------------------


async def test_a_cross_org_escalation_queue_is_refused_before_either_half_applies(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sla_policy.escalate_to_queue_id` is a plain FK with no org check, so
    it can legally name another tenant's queue (invariant #5).

    `assign_ticket` would refuse it -- but only at the point of the call,
    which on a combined escalation is AFTER the priority bump has already been
    applied. Resolving the queue up front collapses the last way the pair
    could half-apply into a failure that happens before anything is written,
    and the assertion below is that the priority did NOT move.
    """
    other_org = await api_env.create_org(f"breach-other-{uuid4().hex[:8]}")
    foreign_queue_id = await _seed_queue(api_env, other_org)

    org_id = await api_env.create_org(f"breach-xorg-{uuid4().hex[:8]}")
    calendar_id = await _seed_calendar(api_env, org_id, timezone=LONDON, windows=_ALWAYS)
    ticket = await _seed_ticket(api_env, org_id, priority="normal")
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        escalate_priority=True,
        escalate_to_queue_id=foreign_queue_id,
    )
    tracking = await _track(api_env, org_id, ticket)

    monkeypatch.setattr(breach_module, "_utcnow", lambda: BREACH_AT)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        row = (
            await session.execute(select(TicketSla).where(TicketSla.id == tracking.id))
        ).scalar_one()
        with pytest.raises(TicketNotFoundError):
            await mark_breached(session, org_id, row, target="response")
        await session.rollback()

    stored_ticket = await _ticket_row(api_env, ticket.id)
    assert stored_ticket.priority == "normal"
    assert stored_ticket.queue_id != foreign_queue_id
    stored = await _row(api_env, ticket.id)
    assert stored is not None
    assert stored.escalated_at is None


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session. Through
# `org_session`, RLS would mask a deleted predicate and these would pass
# either way (the discipline `test_sla_policies.py` established).
# ---------------------------------------------------------------------------


async def test_tenancy_a_ticket_sla_row_from_another_org_is_refused(
    api_env, admin_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-memory `org_id` check on the caller-supplied row. It is strictly
    stronger than the re-SELECT that follows it: an in-memory comparison
    cannot be satisfied by a row this org can see under a different id.
    Without it, org A's sweep could hand org B's row in and breach, escalate
    and re-prioritise B's ticket under A's authority."""
    org_a = await api_env.create_org(f"breach-a-{uuid4().hex[:8]}")
    env_b = await _env(api_env, escalate_priority=True)

    monkeypatch.setattr(breach_module, "_utcnow", lambda: BREACH_AT)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = (
            await session.execute(select(TicketSla).where(TicketSla.id == env_b.tracking.id))
        ).scalar_one()
        with pytest.raises(SlaValidationError, match="refusing to mark it breached"):
            await mark_breached(session, org_a, row, target="response")
        await session.rollback()

    stored = await _row(api_env, env_b.ticket.id)
    assert stored is not None
    assert stored.response_breached_at is None
    assert stored.escalated_at is None
    ticket = await _ticket_row(api_env, env_b.ticket.id)
    assert ticket.priority == "normal"


async def test_tenancy_a_row_pointing_at_another_orgs_ticket_is_refused(
    api_env, admin_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ticket_sla.ticket_id` is a plain FK with no org check, so a row this
    org owns can legally point at another org's ticket. Without
    `_load_ticket_in_org`'s predicate this module would read that foreign
    ticket's number, subject, queue and priority straight into a published
    event -- and then escalate it."""
    org_a = await api_env.create_org(f"breach-ta-{uuid4().hex[:8]}")
    org_b = await api_env.create_org(f"breach-tb-{uuid4().hex[:8]}")
    calendar_a = await _seed_calendar(api_env, org_a, timezone=LONDON, windows=_ALWAYS)
    policy_a = await _seed_policy(api_env, org_a, calendar_id=calendar_a, escalate_priority=True)
    ticket_b = await _seed_ticket(api_env, org_b, priority="normal")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            foreign = TicketSla(
                org_id=org_a,
                ticket_id=ticket_b.id,
                policy_id=policy_a,
                calendar_id=calendar_a,
                response_due_at=MONDAY_0900 + timedelta(hours=1),
                resolution_due_at=MONDAY_0900 + timedelta(hours=8),
                policy_source="initial",
                last_recomputed_at=MONDAY_0900,
            )
            session.add(foreign)
            await session.flush()
            foreign_id = foreign.id

    monkeypatch.setattr(breach_module, "_utcnow", lambda: BREACH_AT)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = (
            await session.execute(select(TicketSla).where(TicketSla.id == foreign_id))
        ).scalar_one()
        with pytest.raises(SlaValidationError, match="not visible in that org"):
            await mark_breached(session, org_a, row, target="response")
        await session.rollback()

    ticket = await _ticket_row(api_env, ticket_b.id)
    assert ticket.priority == "normal"
    assert await _events(api_env, org_a, "ticket.sla_breached") == []
    assert await _events(api_env, org_b, "ticket.sla_breached") == []


async def test_tenancy_a_row_pointing_at_another_orgs_policy_is_refused(
    api_env, admin_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third plain FK: `ticket_sla.policy_id`. Without
    `_load_policy_in_org`'s predicate, escalation would read another tenant's
    `escalate_priority` / `escalate_to_queue_id` and act on them -- moving
    this org's ticket into a queue chosen by that org's admin.

    The breach itself is a legitimate write and lands first; the refusal is
    on the escalation, and the assertion is that nothing was escalated.
    """
    org_a = await api_env.create_org(f"breach-pa-{uuid4().hex[:8]}")
    org_b = await api_env.create_org(f"breach-pb-{uuid4().hex[:8]}")
    calendar_a = await _seed_calendar(api_env, org_a, timezone=LONDON, windows=_ALWAYS)
    calendar_b = await _seed_calendar(api_env, org_b, timezone=LONDON, windows=_ALWAYS)
    policy_b = await _seed_policy(
        api_env, org_b, calendar_id=calendar_b, escalate_priority=True
    )
    ticket_a = await _seed_ticket(api_env, org_a, priority="normal")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            row = TicketSla(
                org_id=org_a,
                ticket_id=ticket_a.id,
                policy_id=policy_b,
                calendar_id=calendar_a,
                response_due_at=MONDAY_0900 + timedelta(hours=1),
                resolution_due_at=MONDAY_0900 + timedelta(hours=8),
                policy_source="initial",
                last_recomputed_at=MONDAY_0900,
            )
            session.add(row)
            await session.flush()
            row_id = row.id

    monkeypatch.setattr(breach_module, "_utcnow", lambda: BREACH_AT)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        stmt = select(TicketSla).where(TicketSla.id == row_id)
        target_row = (await session.execute(stmt)).scalar_one()
        with pytest.raises(SlaValidationError, match="is not visible in that org"):
            await mark_breached(session, org_a, target_row, target="response")
        await session.rollback()

    ticket = await _ticket_row(api_env, ticket_a.id)
    assert ticket.priority == "normal"
    stored = await _row(api_env, ticket_a.id)
    assert stored is not None
    assert stored.escalated_at is None


# ---------------------------------------------------------------------------
# interface
# ---------------------------------------------------------------------------


def test_breach_never_reaches_the_recompute_path_and_never_writes_a_deadline() -> None:
    """§0.4's guard is structural, and this asserts the structure rather than
    the behaviour the two cascade tests above assert.

    Read off the AST, not the source text: docstrings and comments in this
    module discuss `resolve_policy` and `recompute_for_retriage` by name at
    length (they are the whole subject), so a substring search over the file
    would either fail on the prose or have to be weakened until it proved
    nothing. Identifiers actually referenced by code are a different set from
    words appearing in the file.

    A re-resolve ANYWHERE in this module -- before the bump, after it, or
    after the queue move -- is the cascade, and it is far easier to add by
    accident than the explicit recompute the mutation exercise catches. The
    same goes for assigning either `*_due_at`: this module marks a promise
    missed, it never restates the promise.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(breach_module))

    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.alias):
            referenced.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                referenced.add(node.asname)
    assert "recompute_for_retriage" not in referenced
    assert "resolve_policy" not in referenced
    assert "add_working_minutes" not in referenced
    assert "load_calendar_spec" not in referenced

    written: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute):
                written.add(target.attr)
    assert "response_due_at" not in written
    assert "resolution_due_at" not in written


def test_mark_breached_signature_is_the_declared_interface() -> None:
    """The plan's declared interface, so a caller written against it (Task
    10's sweep) cannot be broken by a silent rename: keyword-only `target`,
    positional `session, org_id, ticket_sla`."""
    import inspect

    signature = inspect.signature(mark_breached)
    parameters = list(signature.parameters.values())
    assert [p.name for p in parameters] == ["session", "org_id", "ticket_sla", "target"]
    assert parameters[3].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_module_exports_only_its_declared_interface() -> None:
    assert breach_module.__all__ == ["PRIORITY_LADDER", "mark_breached"]
