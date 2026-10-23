"""Met detection: when a response or resolution promise was actually kept
(docs/modules/sla/spec.md §0.6, §2.5, §10.9, §10.10; Task 6).

§0.6 IN ONE PARAGRAPH. The response clock stops at the **first public comment
from someone who is not the requester**, because a response SLA is a promise to
the person waiting and that is the only candidate they can perceive. First
assignment and first status change out of `new` were both rejected: the
requester has seen nothing, so the number would measure internal bookkeeping.
An internal note does not stop it either -- `ticket.internal_note_added` is a
distinct event type precisely so staff discussion cannot be mistaken for
customer contact. Resolution stops the response clock too, because a ticket
resolved outright with no reply (a duplicate, say) cannot owe a response
forever. §10.9's three negatives -- an internal note, an assignment, a status
change out of `new` -- are the whole point of the section: without them this
file would pass against an implementation that stops the clock on any activity
at all, which is exactly the rejected alternative.

FIXTURE DISCIPLINE -- what these tests deliberately VARY, and why. This
sub-project has repeatedly shipped tests that passed while proving nothing
because every fixture held constant the one parameter the defect would vary:
Task 2's Critical hid behind a business-hours calendar, Task 3's F1 behind a
single exception class, Task 4's F1 behind default-valued create arguments,
Task 5's F1 behind a constant column-to-clock pairing, and Task 7 caught the
same shape in its own first draft (every guard test stopped its clock with
`*_met_at`).

**The constant this module found in its own first draft was inherited from
`test_sla_tracking._seed_ticket`: it builds every ticket with
`requester_id == created_by == <one user>` and no assignee at all.** Under that
fixture, `author_id != ticket.created_by` and `author_id != ticket.requester_id`
are the SAME predicate, and an implementation that compared the author against
the ticket's AUTHOR (or, with an assignee added, against its assignee) would
pass every test in this file. `_seed_met_ticket` below therefore gives every
ticket **four distinct users** -- requester, creator, assignee and a bystander
who is none of the three -- and the four author-identity tests triangulate
`requester_id` specifically: the requester's own comment does NOT stop the
clock, and the creator's, the assignee's and the bystander's each DO.

The other parameters varied here, and the mutant each one catches:

* **Comment visibility**, public vs internal -- the §10.9 negative that a
  visibility-blind wiring (`add_comment` calling the hook unconditionally)
  fails.
* **Which stop column is already set, per clock.** All four combinations
  (response x met, response x breached, resolution x met, resolution x
  breached) are asserted independently, because Task 5's F1 was exactly a
  constant pairing here and §5.1 records that "already stopped" means met
  **or** breached.
* **Whether the ticket has an SLA row at all.** A ticket whose org configured
  no matching policy has no `ticket_sla` row (§3), and both hooks must be
  silent no-ops for it rather than materialising a promise nobody made.
* **Whether the ticket has a requester at all.** An `origin='alert'` ticket is
  the one shape the DB check constraint lets carry `requester_id IS NULL`, and
  a naive `author_id != ticket.requester_id` treats it differently from an
  `is`-comparison against a missing author.
* **Whether the comment has a human author at all** -- a `author_type="system"`
  public comment (the change-management adapter posts these) carries
  `author_id=None`.
* **Ticket type: incident (`resolved`) and request (`fulfilled`).** A wiring
  that hardcoded `to_status == "resolved"` passes every incident test and
  leaves every request's promise running forever.
* **`created_at` is always months in the past, never "now"** -- inherited from
  `test_sla_tracking.py`, so a due-at that this module accidentally recomputed
  could not land on the untouched expectation.

TIME IS FROZEN EXPLICITLY at every call site (`tracking._utcnow` monkeypatched),
because the assertions are on the exact stored instant -- a tolerance-based
assertion could not distinguish "stamped now" from "re-stamped now" in the
first-contact-wins tests.

Tenancy tests at the bottom run on a raw BYPASSRLS `admin_engine` session, so
each service's own `org_id` predicate is the sole guard under test -- through
`org_session` RLS would mask a deleted predicate and the test would pass either
way (the discipline `test_sla_policies.py` established).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session

# `app.models` first, before any `app.modules.*` import -- importing an SLA
# service module directly would otherwise start the ORM registry from inside
# `app.modules.service_desk.models` and hit a partially-initialised module
# (`test_sla_tracking.py`/`test_sla_pause.py` order their imports the same way
# for the same reason).
from app.models import Ticket
from app.modules.service_desk.threads import add_comment
from app.modules.service_desk.transitions import assign_ticket, change_status
from app.modules.sla import tracking
from app.modules.sla.errors import SlaValidationError
from app.modules.sla.models import TicketSla
from app.modules.sla.tracking import note_public_comment, note_resolved
from tests.test_sla_migration import _seed_calendar, _seed_policy, _seed_user
from tests.test_sla_pause import _pause_at, _resume_at
from tests.test_sla_tracking import (
    _ALWAYS,
    _MON_FRI,
    LONDON,
    MONDAY_0900,
    _row,
    _row_count,
    _stop_clocks,
)

# Monday 09:30 UTC -- 30 minutes into a 60-minute response target, so a stamp
# here is a promise KEPT and can never be confused with a breach.
MONDAY_0930 = MONDAY_0900 + timedelta(minutes=30)
# Monday 11:00 -- a later instant, used to prove first contact wins: a second
# public comment at 11:00 must not move a `response_met_at` of 09:30.
MONDAY_1100 = MONDAY_0900 + timedelta(hours=2)
# An instant BEFORE any hook in this file runs, used to pre-stamp a stop
# column so "left untouched" is distinguishable from "re-stamped".
MONDAY_0905 = MONDAY_0900 + timedelta(minutes=5)

# The wildcard policy every test in this file resolves to, on a 24/7 calendar:
# response due 10:00, resolution due 17:00, both exactly `created_at + target`
# so an accidental recompute is visible by eye.
RESPONSE_MINUTES = 60
RESOLUTION_MINUTES = 480
RESPONSE_DUE = MONDAY_0900 + timedelta(minutes=RESPONSE_MINUTES)
RESOLUTION_DUE = MONDAY_0900 + timedelta(minutes=RESOLUTION_MINUTES)


class _Actors:
    """The four distinct users every ticket in this file carries -- see the
    module docstring's fixture-discipline note. `requester` is the person the
    promise was made to; `creator` filed it on their behalf; `assignee` owns
    it; `bystander` is none of the three."""

    def __init__(self, requester: UUID | None, creator: UUID, assignee: UUID, bystander: UUID):
        self.requester = requester
        self.creator = creator
        self.assignee = assignee
        self.bystander = bystander


async def _seed_met_ticket(
    api_env,
    org_id: UUID,
    *,
    created_at: datetime = MONDAY_0900,
    ticket_type: str = "incident",
    status: str | None = None,
    with_requester: bool = True,
    priority: str = "normal",
) -> tuple[Ticket, _Actors]:
    """A ticket whose `requester_id`, `created_by` and `assigned_to` are THREE
    DIFFERENT users, plus a fourth who holds none of those roles.

    `test_sla_tracking._seed_ticket` is deliberately not reused: it sets
    `requester_id == created_by` and leaves `assigned_to` NULL, which collapses
    the three identity predicates an implementation could plausibly confuse
    into one (module docstring).

    `with_requester=False` produces the `origin='alert'` shape --
    `ck_ticket_human_attribution` permits `requester_id IS NULL` for that
    origin alone.

    `status` defaults to each lifecycle's own entry state (`new` for an
    incident, `in_progress` for a request), so `change_status` in the wiring
    tests below has a legal edge to drive.

    Side effects: four `users` rows, one `ticket_queue`, one `ticket_category`
    and one `ticket` row.
    """
    from app.models import TicketCategory, TicketQueue

    requester = await _seed_user(api_env, org_id, email=f"met-req-{uuid4().hex[:10]}@test.local")
    creator = await _seed_user(api_env, org_id, email=f"met-cre-{uuid4().hex[:10]}@test.local")
    assignee = await _seed_user(api_env, org_id, email=f"met-asg-{uuid4().hex[:10]}@test.local")
    bystander = await _seed_user(api_env, org_id, email=f"met-byst-{uuid4().hex[:10]}@test.local")

    if status is None:
        status = "new" if ticket_type == "incident" else "in_progress"

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            queue = TicketQueue(
                org_id=org_id,
                key=f"met-q-{uuid4().hex[:10]}",
                name="Met queue",
                created_by=creator,
            )
            session.add(queue)
            await session.flush()
            category = TicketCategory(
                org_id=org_id,
                key=f"met-c-{uuid4().hex[:10]}",
                name="Met category",
                type=ticket_type,
                default_queue_id=queue.id,
                created_by=creator,
            )
            session.add(category)
            await session.flush()
            ticket = Ticket(
                org_id=org_id,
                number=f"M-{uuid4().hex[:10]}",
                type=ticket_type,
                category_id=category.id,
                queue_id=queue.id,
                status=status,
                priority=priority,
                subject="met detection",
                requester_id=requester if with_requester else None,
                assigned_to=assignee,
                created_by=creator,
                origin="portal" if with_requester else "alert",
                created_at=created_at,
            )
            session.add(ticket)
            await session.flush()
        return ticket, _Actors(
            requester if with_requester else None, creator, assignee, bystander
        )


async def _org_with_policy(
    api_env,
    *,
    windows: list | None = None,
    response_minutes: int = RESPONSE_MINUTES,
    resolution_minutes: int = RESOLUTION_MINUTES,
) -> UUID:
    """A fresh org carrying one wildcard `(queue=any, priority=any)` policy, so
    every ticket seeded into it materialises a promise."""
    org_id = await api_env.create_org("met")
    calendar_id = await _seed_calendar(
        api_env, org_id, timezone=LONDON, windows=_ALWAYS if windows is None else windows
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        response_minutes=response_minutes,
        resolution_minutes=resolution_minutes,
    )
    return org_id


async def _start(api_env, org_id: UUID, ticket: Ticket) -> None:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await tracking.start_tracking(session, org_id, ticket)


async def _comment_at(
    api_env,
    monkeypatch,
    org_id: UUID,
    ticket_id: UUID,
    *,
    author_id: UUID | None,
    when: datetime,
) -> None:
    """`note_public_comment` with the clock frozen at `when`, through the
    RLS-constrained app engine (the ordinary caller's path)."""
    monkeypatch.setattr(tracking, "_utcnow", lambda: when)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await note_public_comment(session, org_id, ticket_id, author_id=author_id)


async def _resolve_at(
    api_env, monkeypatch, org_id: UUID, ticket_id: UUID, *, when: datetime
) -> None:
    """`note_resolved` with the clock frozen at `when`."""
    monkeypatch.setattr(tracking, "_utcnow", lambda: when)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await note_resolved(session, org_id, ticket_id)


def _assert_deadlines_untouched(row: TicketSla) -> None:
    """Neither hook writes a `*_due_at`: met detection records WHEN a promise
    was kept, it never restates the promise. `created_at` is months in the past
    in every fixture here, so a recompute from "now" would miss these by
    months and a recompute from `created_at` under the same policy would still
    move `last_recomputed_at`, asserted separately where it matters."""
    assert row.response_due_at == RESPONSE_DUE
    assert row.resolution_due_at == RESOLUTION_DUE


# ---------------------------------------------------------------------------
# note_public_comment -- who the author is (§0.6)
# ---------------------------------------------------------------------------


async def test_a_public_comment_from_the_assignee_stops_the_response_clock(
    api_env, monkeypatch
) -> None:
    """The ordinary case: a technician replies, the requester can see it, the
    response promise is kept at that instant. The resolution clock is
    untouched -- the two stop independently."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    await _comment_at(
        api_env, monkeypatch, org_id, ticket.id, author_id=actors.assignee, when=MONDAY_0930
    )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_0930
    assert row.response_breached_at is None
    assert row.resolution_met_at is None
    assert row.resolution_breached_at is None
    _assert_deadlines_untouched(row)


async def test_a_public_comment_from_the_requester_does_not_stop_the_response_clock(
    api_env, monkeypatch
) -> None:
    """§0.6's exclusion. The person waiting adding to their own ticket is not
    a response to them; treating it as one would let a chasing follow-up
    ("any news?") satisfy the promise made to the chaser."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    await _comment_at(
        api_env, monkeypatch, org_id, ticket.id, author_id=actors.requester, when=MONDAY_0930
    )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at is None
    _assert_deadlines_untouched(row)


async def test_a_public_comment_from_the_ticket_creator_stops_the_response_clock(
    api_env, monkeypatch
) -> None:
    """The creator is NOT the requester here (module docstring's fixture
    note): a service-desk agent filed this on the requester's behalf. Their
    public reply is contact the requester can perceive, so it stops the clock.
    An implementation that compared the author against `created_by` -- the
    collapse `test_sla_tracking._seed_ticket` would have hidden -- fails this
    test and the requester test above together."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    await _comment_at(
        api_env, monkeypatch, org_id, ticket.id, author_id=actors.creator, when=MONDAY_0930
    )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_0930


async def test_a_public_comment_from_a_bystander_stops_the_response_clock(
    api_env, monkeypatch
) -> None:
    """A fourth user who is neither requester, creator nor assignee -- a
    colleague chiming in publicly. §0.6 says "someone who is not the
    requester", not "the assignee", so this stops the clock too; an
    implementation gated on `author_id == ticket.assigned_to` fails here."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    await _comment_at(
        api_env, monkeypatch, org_id, ticket.id, author_id=actors.bystander, when=MONDAY_0930
    )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_0930


async def test_a_system_authored_public_comment_does_not_stop_the_response_clock(
    api_env, monkeypatch
) -> None:
    """`author_type="system"` carries `author_id=None` (`threads.add_comment`
    enforces the pairing, `ck_comment_author` at the DB layer). §0.6 stops the
    clock for "someone who is not the requester" -- an automated note has no
    someone behind it, and the change-management adapter posts exactly this
    shape ("Change X is now Y") on a linked ticket. Letting it stop the clock
    would mean an unrelated module's status note silently satisfies this
    ticket's response promise, which is the machine-generated version of the
    internal-bookkeeping metric §0.6 rejects."""
    org_id = await _org_with_policy(api_env)
    ticket, _actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    await _comment_at(api_env, monkeypatch, org_id, ticket.id, author_id=None, when=MONDAY_0930)

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at is None


async def test_a_public_comment_on_a_ticket_with_no_requester_stops_the_response_clock(
    api_env, monkeypatch
) -> None:
    """An `origin='alert'` ticket is the one shape that may carry
    `requester_id IS NULL` (`ck_ticket_human_attribution`). Nobody is excluded,
    so any human public comment is the first contact. This exercises that
    `requester_id IS NULL` shape end to end -- it does NOT, by itself, pin any
    particular guard against a NULL/UUID comparison: `ticket.requester_id ==
    author_id` (an `is not None` half was removed as dead code by the
    sla-task-6b review -- `author_id` is never `None` by this point, so
    `None == <uuid>` was already `False` without it) behaves identically here
    whether or not that clause exists."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id, with_requester=False)
    assert ticket.requester_id is None
    await _start(api_env, org_id, ticket)

    await _comment_at(
        api_env, monkeypatch, org_id, ticket.id, author_id=actors.bystander, when=MONDAY_0930
    )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_0930


# ---------------------------------------------------------------------------
# note_public_comment -- first contact wins, and the stopped-clock guard
# ---------------------------------------------------------------------------


async def test_a_second_public_comment_does_not_move_the_met_timestamp(
    api_env, monkeypatch
) -> None:
    """"First public comment" (§0.6) is literal: response time is measured to
    the FIRST reply, and letting a later one overwrite it would silently turn
    the metric into time-to-last-comment, growing without bound on a chatty
    ticket."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    await _comment_at(
        api_env, monkeypatch, org_id, ticket.id, author_id=actors.assignee, when=MONDAY_0930
    )
    await _comment_at(
        api_env, monkeypatch, org_id, ticket.id, author_id=actors.bystander, when=MONDAY_1100
    )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_0930


async def test_a_public_comment_does_not_stamp_a_breached_response_clock(
    api_env, monkeypatch
) -> None:
    """§5.1: a clock stops on `*_met_at` OR `*_breached_at`, and this module's
    existing guards read both. A late reply after the sweep has already
    recorded the miss must not also mark the promise met -- a row reading
    breached AND met is unreadable to reporting, and "we eventually replied"
    is what the ticket thread is for."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)
    await _stop_clocks(api_env, ticket.id, response_breached_at=MONDAY_0905)

    await _comment_at(
        api_env, monkeypatch, org_id, ticket.id, author_id=actors.assignee, when=MONDAY_1100
    )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at is None
    assert row.response_breached_at == MONDAY_0905


async def test_a_public_comment_on_a_ticket_with_no_sla_row_writes_nothing(
    api_env, monkeypatch
) -> None:
    """An org that configured no matching policy has no promise (§3) and
    therefore no `ticket_sla` row. The hook must be a silent no-op rather than
    materialising a row -- a fabricated target is worse than an absent one, and
    materialising here would attribute one to a comment rather than to
    creation or a re-triage."""
    org_id = await api_env.create_org("met-nopolicy")
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)
    assert await _row(api_env, ticket.id) is None

    await _comment_at(
        api_env, monkeypatch, org_id, ticket.id, author_id=actors.assignee, when=MONDAY_0930
    )

    assert await _row(api_env, ticket.id) is None
    assert await _row_count(api_env, org_id) == 0


# ---------------------------------------------------------------------------
# The portal invariant (sla-task-6b review, Finding 2). `portal_user` holds no
# grant on `ticket_sla` at all -- this hook is safe on the portal's connection
# only because the requester comparison, which reads `ticket` alone, runs
# BEFORE any `ticket_sla` access. Named and pinned here, through a real
# `portal_session`, so a future reorder fails a test that explains itself
# instead of three unrelated-looking portal tests.
# ---------------------------------------------------------------------------


async def test_a_portal_requesters_public_comment_never_touches_ticket_sla(
    api_env, portal_engine, monkeypatch
) -> None:
    """`portal_user` (migrations 0028/0030) holds NO grant on `ticket_sla`.
    `note_public_comment`'s docstring states the invariant this test proves:
    the requester comparison must run before any `ticket_sla` read, because
    `portal.tickets.add_my_comment` always calls this hook with `author_id ==
    requester_id` (the portal has exactly one comment path, and it forces
    both), so every portal-originated call is supposed to return at that
    comparison having never reached `ticket_sla`.

    Driven through the REAL `portal_session` connection and
    `portal.tickets.add_my_comment` -- not `note_public_comment` called
    directly -- because the point is to exercise the actual database grant,
    not a stand-in for it. Reordering the requester test after the
    `ticket_sla` SELECT turns this red with
    `asyncpg.exceptions.InsufficientPrivilegeError: permission denied for
    table ticket_sla` (sla-task-6b-review.md Finding 2, check 2) -- and unlike
    the three portal tests that incidentally catch the same reorder today,
    this one's name says why."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0930)

    from app.core.tenancy import portal_session
    from app.modules.portal.schema import PortalCommentCreate
    from app.modules.portal.tickets import add_my_comment

    async with portal_session(
        org_id, actors.requester, engine=portal_engine, clients=ClientScope.all()
    ) as s:
        await add_my_comment(
            s,
            org_id=org_id,
            requester_id=actors.requester,
            ticket_id=ticket.id,
            data=PortalCommentCreate(body="thanks, checking on this"),
        )

    # Not just "no exception" -- the requester's own comment must not stop the
    # clock either (§0.6), so a stamped `response_met_at` here would mean the
    # comparison ran but matched the wrong thing.
    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at is None


# ---------------------------------------------------------------------------
# note_resolved (§0.6's last sentence, §10.10)
# ---------------------------------------------------------------------------


async def test_note_resolved_stops_both_clocks_when_both_are_running(
    api_env, monkeypatch
) -> None:
    """§0.6: "a ticket resolved outright with no reply (a duplicate, say)
    cannot owe a response forever". Both clocks stop at the same instant, and
    neither deadline moves."""
    org_id = await _org_with_policy(api_env)
    ticket, _actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    await _resolve_at(api_env, monkeypatch, org_id, ticket.id, when=MONDAY_1100)

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_1100
    assert row.resolution_met_at == MONDAY_1100
    assert row.response_breached_at is None
    assert row.resolution_breached_at is None
    _assert_deadlines_untouched(row)


async def test_note_resolved_keeps_an_earlier_response_met_at(api_env, monkeypatch) -> None:
    """Response x met. The reply came first; resolution stops now, and the
    earlier response stamp is history that must not be overwritten -- an
    implementation that stamped both unconditionally would report every
    ticket's response time as its resolution time."""
    org_id = await _org_with_policy(api_env)
    ticket, _actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)
    await _stop_clocks(api_env, ticket.id, response_met_at=MONDAY_0905)

    await _resolve_at(api_env, monkeypatch, org_id, ticket.id, when=MONDAY_1100)

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_0905
    assert row.resolution_met_at == MONDAY_1100


async def test_note_resolved_does_not_stamp_a_breached_response_clock(
    api_env, monkeypatch
) -> None:
    """Response x breached -- the other half of the pairing Task 5's F1 left
    unproven. A resolution does not retroactively convert a missed response
    into a kept one."""
    org_id = await _org_with_policy(api_env)
    ticket, _actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)
    await _stop_clocks(api_env, ticket.id, response_breached_at=MONDAY_0905)

    await _resolve_at(api_env, monkeypatch, org_id, ticket.id, when=MONDAY_1100)

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at is None
    assert row.response_breached_at == MONDAY_0905
    assert row.resolution_met_at == MONDAY_1100


async def test_note_resolved_keeps_an_earlier_resolution_met_at(api_env, monkeypatch) -> None:
    """Resolution x met. A second resolution -- a reopen followed by a
    re-resolve, or a redelivered status hook -- must not move the original
    stamp. First stop wins on this clock exactly as it does on the other."""
    org_id = await _org_with_policy(api_env)
    ticket, _actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)
    await _stop_clocks(api_env, ticket.id, resolution_met_at=MONDAY_0905)

    await _resolve_at(api_env, monkeypatch, org_id, ticket.id, when=MONDAY_1100)

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.resolution_met_at == MONDAY_0905
    # The response clock was still running, so it stops now.
    assert row.response_met_at == MONDAY_1100


async def test_note_resolved_does_not_stamp_a_breached_resolution_clock(
    api_env, monkeypatch
) -> None:
    """Resolution x breached -- the fourth combination. A ticket resolved two
    days late is resolved, not on time, and the breach stands."""
    org_id = await _org_with_policy(api_env)
    ticket, _actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)
    await _stop_clocks(api_env, ticket.id, resolution_breached_at=MONDAY_0905)

    await _resolve_at(api_env, monkeypatch, org_id, ticket.id, when=MONDAY_1100)

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.resolution_met_at is None
    assert row.resolution_breached_at == MONDAY_0905
    assert row.response_met_at == MONDAY_1100


async def test_note_resolved_on_a_ticket_with_no_sla_row_writes_nothing(
    api_env, monkeypatch
) -> None:
    """Same §3 no-op as the comment hook: no policy, no promise, no row."""
    org_id = await api_env.create_org("met-nopolicy-res")
    ticket, _actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)
    assert await _row(api_env, ticket.id) is None

    await _resolve_at(api_env, monkeypatch, org_id, ticket.id, when=MONDAY_1100)

    assert await _row(api_env, ticket.id) is None
    assert await _row_count(api_env, org_id) == 0


# ---------------------------------------------------------------------------
# The wiring: §10.9's three negatives, driven through the real ticket core
# ---------------------------------------------------------------------------


async def _add_comment_through_core(
    api_env,
    monkeypatch,
    org_id: UUID,
    ticket_id: UUID,
    *,
    author_id: UUID,
    visibility: str,
    when: datetime,
) -> None:
    monkeypatch.setattr(tracking, "_utcnow", lambda: when)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await add_comment(
            session,
            org_id,
            ticket_id,
            body="a comment",
            author_id=author_id,
            visibility=visibility,
        )


async def test_a_public_comment_through_the_ticket_core_stops_the_response_clock(
    api_env, monkeypatch
) -> None:
    """The positive half of §10.9, through `threads.add_comment` rather than
    the service directly -- this is what proves the hook is actually wired,
    and asserts on stored state rather than on whether a function was
    called."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    await _add_comment_through_core(
        api_env,
        monkeypatch,
        org_id,
        ticket.id,
        author_id=actors.assignee,
        visibility="public",
        when=MONDAY_0930,
    )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_0930


async def test_a_requesters_public_comment_through_the_ticket_core_does_not_stop_the_response_clock(
    api_env, monkeypatch
) -> None:
    """The composition Finding 6 (sla-task-6b review) found missing: every
    other `_add_comment_through_core` call in this section passes
    `author_id=actors.assignee`, so nothing at the core-wiring level had ever
    crossed "driven through `threads.add_comment`" with "author is the
    requester". That crossing is precisely what the portal relies on --
    `add_my_comment` reaches this same `add_comment` with `author_id ==
    requester_id` -- so it is the one core-level test worth having, alongside
    (not instead of) the direct-service requester test above and the portal
    test that drives it through a real `portal_session`."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    await _add_comment_through_core(
        api_env,
        monkeypatch,
        org_id,
        ticket.id,
        author_id=actors.requester,
        visibility="public",
        when=MONDAY_0930,
    )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at is None
    _assert_deadlines_untouched(row)


async def test_negative_an_internal_note_does_not_stop_the_response_clock(
    api_env, monkeypatch
) -> None:
    """§10.9 negative 1. `ticket.internal_note_added` is a distinct event type
    precisely so staff discussion cannot be mistaken for customer contact
    (§0.6). The identical call with `visibility="public"` above stops the
    clock, so the ONLY parameter varied between the two is visibility."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    await _add_comment_through_core(
        api_env,
        monkeypatch,
        org_id,
        ticket.id,
        author_id=actors.assignee,
        visibility="internal",
        when=MONDAY_0930,
    )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at is None
    _assert_deadlines_untouched(row)


async def test_negative_assigning_the_ticket_does_not_stop_the_response_clock(
    api_env, monkeypatch
) -> None:
    """§10.9 negative 2. First assignment was the rejected alternative: the
    requester has seen nothing, and a ticket can be assigned and then sit
    untouched for a day with a green response SLA. Driven through the real
    `transitions.assign_ticket`, which really does move `assigned_to`."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0930)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        updated = await assign_ticket(
            session, org_id, actors.creator, ticket.id, {"assigned_to": actors.bystander}
        )
        assert updated.assigned_to == actors.bystander

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at is None
    assert row.resolution_met_at is None
    _assert_deadlines_untouched(row)


async def test_negative_a_status_change_out_of_new_does_not_stop_the_response_clock(
    api_env, monkeypatch
) -> None:
    """§10.9 negative 3. `new -> open` is internal bookkeeping the requester
    cannot see; once it stops the clock the number stops meaning anything to
    the customer it was promised to (§0.6)."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0930)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        updated = await change_status(
            session, org_id, ticket.id, to_status="open", actor_id=actors.creator
        )
        assert updated.status == "open"

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at is None
    assert row.resolution_met_at is None
    _assert_deadlines_untouched(row)


async def test_resolving_an_incident_through_the_ticket_core_stops_both_clocks(
    api_env, monkeypatch
) -> None:
    """§10.10, incident branch: `open -> resolved`."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id, status="open")
    await _start(api_env, org_id, ticket)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_1100)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await change_status(
            session, org_id, ticket.id, to_status="resolved", actor_id=actors.creator
        )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_1100
    assert row.resolution_met_at == MONDAY_1100
    _assert_deadlines_untouched(row)


async def test_fulfilling_a_request_through_the_ticket_core_stops_both_clocks(
    api_env, monkeypatch
) -> None:
    """§10.10, request branch: `in_progress -> fulfilled`. `fulfilled` is the
    request lifecycle's counterpart to `resolved` -- the ticket core already
    treats them as one concept for `resolved_at` and for its `ticket.resolved`
    event, and a wiring hardcoded to `"resolved"` leaves every request's
    promise running forever."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id, ticket_type="request")
    await _start(api_env, org_id, ticket)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_1100)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await change_status(
            session, org_id, ticket.id, to_status="fulfilled", actor_id=actors.creator
        )

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_1100
    assert row.resolution_met_at == MONDAY_1100


async def test_reopening_a_resolved_ticket_leaves_both_met_stamps_alone(
    api_env, monkeypatch
) -> None:
    """§2.5.1: "a clock only ever moves running -> stopped, never back". A
    reopen clears the ticket core's `resolved_at`, and must NOT un-stamp the
    SLA row: the promise WAS met at that instant, and erasing it would let a
    reopen launder a met-then-reopened ticket into a fresh, unmeasured one."""
    org_id = await _org_with_policy(api_env)
    ticket, actors = await _seed_met_ticket(api_env, org_id, status="open")
    await _start(api_env, org_id, ticket)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_1100)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await change_status(
            session, org_id, ticket.id, to_status="resolved", actor_id=actors.creator
        )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        reopened = await change_status(
            session, org_id, ticket.id, to_status="open", actor_id=actors.creator
        )
        assert reopened.resolved_at is None

    row = await _row(api_env, ticket.id)
    assert row is not None
    assert row.response_met_at == MONDAY_1100
    assert row.resolution_met_at == MONDAY_1100


# ---------------------------------------------------------------------------
# Interaction with pause (Task 7, spec §5/§5.1)
# ---------------------------------------------------------------------------


async def test_a_met_response_is_not_re_derived_when_a_pause_is_resumed(
    api_env, monkeypatch
) -> None:
    """A public comment lands DURING a pause: the response clock stops, and
    the still-running resolution clock alone receives the pause credit at
    `resume`. On a Mon-Fri 09:00-17:00 calendar the pause is measured in
    working minutes, so this also proves the met stamp did not disturb the
    accumulator.

    Deliberately on a business-hours calendar rather than the 24/7 one the
    rest of this file uses: on 24/7, working time and wall clock are
    indistinguishable and a credit applied to the wrong clock could still land
    on a plausible-looking instant."""
    org_id = await _org_with_policy(api_env, windows=_MON_FRI)
    ticket, actors = await _seed_met_ticket(api_env, org_id)
    await _start(api_env, org_id, ticket)

    # Mon-Fri 09:00-17:00, filed Monday 09:00: the 60-minute response target
    # lands at Monday 10:00 and the 480-minute resolution target consumes the
    # whole working day, landing at Monday 17:00.
    before = await _row(api_env, ticket.id)
    assert before is not None
    assert before.response_due_at == RESPONSE_DUE
    assert before.resolution_due_at == datetime(2026, 3, 2, 17, 0, tzinfo=UTC)

    await _pause_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_0905)
    await _comment_at(
        api_env, monkeypatch, org_id, ticket.id, author_id=actors.assignee, when=MONDAY_0930
    )
    await _resume_at(api_env, monkeypatch, org_id, ticket.id, MONDAY_1100)

    row = await _row(api_env, ticket.id)
    assert row is not None
    # 09:05 -> 11:00 is 115 working minutes on this calendar.
    assert row.paused_minutes_total == 115
    assert row.paused_at is None
    # The met response clock is neither extended nor re-derived (§5.1) -- it
    # still reads Monday 10:00 despite 115 minutes of credit being available.
    assert row.response_met_at == MONDAY_0930
    assert row.response_due_at == RESPONSE_DUE
    # The running resolution clock gets the whole credit, re-derived as
    # 480 + 115 working minutes from Monday 09:00: the first 480 exhaust
    # Monday's window at 17:00, and the remaining 115 resume at Tuesday 09:00
    # and land at 10:55. Note this is NOT `17:00 + 115 minutes` of wall clock
    # -- the weekend-buying arithmetic §5 forbids, in miniature.
    assert row.resolution_due_at == datetime(2026, 3, 3, 10, 55, tzinfo=UTC)


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session (module docstring)
# ---------------------------------------------------------------------------


async def _foreign_tracking_row(api_env, owner_org: UUID, ticket_id: UUID) -> UUID:
    """A `ticket_sla` row owned by `owner_org` but pointing at another org's
    ticket -- the FK permits it and no application path produces it. Same
    construction `test_sla_tracking._foreign_tracking_row` uses; duplicated
    rather than imported so this file can give the row RUNNING clocks and the
    exact deadlines its assertions read back."""
    calendar_id = await _seed_calendar(api_env, owner_org, timezone=LONDON, windows=_ALWAYS)
    policy_id = await _seed_policy(api_env, owner_org, calendar_id=calendar_id)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            row = TicketSla(
                org_id=owner_org,
                ticket_id=ticket_id,
                policy_id=policy_id,
                calendar_id=calendar_id,
                response_due_at=RESPONSE_DUE,
                resolution_due_at=RESOLUTION_DUE,
                policy_source="initial",
                last_recomputed_at=MONDAY_0900,
            )
            session.add(row)
            await session.flush()
            return row.id


async def test_tenancy_note_public_comment_tracking_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine, monkeypatch
) -> None:
    """With the `org_id` predicate removed from `_load_tracking`, org A's
    public comment finds org B's row for this ticket and stamps
    `response_met_at` on it -- a cross-tenant WRITE that marks another
    tenant's promise kept. With the predicate, A sees no row and does
    nothing."""
    org_a = await api_env.create_org("met-a")
    org_b = await api_env.create_org("met-b")
    ticket_a, actors = await _seed_met_ticket(api_env, org_a)
    foreign_id = await _foreign_tracking_row(api_env, org_b, ticket_a.id)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0930)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await note_public_comment(session, org_a, ticket_a.id, author_id=actors.assignee)
        await session.commit()

    stored = await _row(api_env, ticket_a.id)
    assert stored is not None
    assert stored.id == foreign_id
    assert stored.org_id == org_b
    assert stored.response_met_at is None
    assert await _row_count(api_env, org_a) == 0


async def test_tenancy_note_public_comment_ticket_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine, monkeypatch
) -> None:
    """The second predicate on the same call: `_load_ticket_in_org`.
    `ticket_sla.ticket_id` is a plain FK with no org check, so a row org A owns
    can legally point at org B's ticket. Without the `Ticket.org_id`
    predicate, A reads B's ticket to decide whether the author is "the
    requester" -- deciding this tenant's SLA from another tenant's data. With
    it, the impossible state is a typed error."""
    org_a = await api_env.create_org("met-c")
    org_b = await api_env.create_org("met-d")
    ticket_b, actors_b = await _seed_met_ticket(api_env, org_b)
    foreign_id = await _foreign_tracking_row(api_env, org_a, ticket_b.id)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0930)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError, match="not visible in that org"):
            await note_public_comment(session, org_a, ticket_b.id, author_id=actors_b.assignee)

    async with AsyncSession(api_env.admin_engine) as session:
        stored = (
            await session.execute(select(TicketSla).where(TicketSla.id == foreign_id))
        ).scalar_one()
        assert stored.response_met_at is None


async def test_tenancy_note_resolved_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine, monkeypatch
) -> None:
    """The same predicate on the resolution hook, where removing it stamps
    BOTH of another tenant's clocks met -- reporting a promise kept that this
    org never made and the owning org never satisfied."""
    org_a = await api_env.create_org("met-e")
    org_b = await api_env.create_org("met-f")
    ticket_a, _actors = await _seed_met_ticket(api_env, org_a)
    foreign_id = await _foreign_tracking_row(api_env, org_b, ticket_a.id)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_1100)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await note_resolved(session, org_a, ticket_a.id)
        await session.commit()

    stored = await _row(api_env, ticket_a.id)
    assert stored is not None
    assert stored.id == foreign_id
    assert stored.org_id == org_b
    assert stored.response_met_at is None
    assert stored.resolution_met_at is None
    assert await _row_count(api_env, org_a) == 0


async def test_note_public_comment_refuses_another_orgs_ticket_before_any_sla_row_exists(
    api_env, admin_engine: AsyncEngine, monkeypatch
) -> None:
    """`_load_ticket_in_org` runs FIRST on this path (see the service's
    docstring on the portal role), so a cross-org ticket id is refused even
    when no `ticket_sla` row exists anywhere -- the module never reads another
    tenant's ticket at all, rather than reading it and then discovering there
    was nothing to stamp."""
    org_a = await api_env.create_org("met-g")
    org_b = await api_env.create_org("met-h")
    ticket_b, actors_b = await _seed_met_ticket(api_env, org_b)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0930)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(SlaValidationError, match="the ticket itself is not visible"):
            await note_public_comment(session, org_a, ticket_b.id, author_id=actors_b.assignee)

    assert await _row(api_env, ticket_b.id) is None
    assert await _row_count(api_env, org_a) == 0


async def test_note_public_comment_on_an_in_org_ticket_with_no_sla_row_does_not_raise(
    api_env, admin_engine: AsyncEngine, monkeypatch
) -> None:
    """The companion that keeps the test above honest: an IN-ORG ticket with
    no `ticket_sla` row is an ordinary silent no-op, so the error above is
    genuinely the org predicate firing and not merely "anything unexpected
    raises"."""
    org_a = await api_env.create_org("met-i")
    ticket_a, actors_a = await _seed_met_ticket(api_env, org_a)
    monkeypatch.setattr(tracking, "_utcnow", lambda: MONDAY_0930)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await note_public_comment(session, org_a, ticket_a.id, author_id=actors_a.assignee)
        await session.commit()

    assert await _row(api_env, ticket_a.id) is None
    assert await _row_count(api_env, org_a) == 0
