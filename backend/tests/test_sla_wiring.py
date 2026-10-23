"""The SLA engine wired into the ticket lifecycle, and the promise voided on a
terminal-not-resolved ticket (docs/modules/sla/spec.md §0.3, §0.4, §3, §5,
§6.0; migration 0038).

WHAT THIS FILE EXISTS FOR. `start_tracking`, `pause`, `resume` and
`recompute_for_retriage` had no production caller. Every one of them was
exercised only by tests that called it directly, so the whole SLA engine was
inert on every real ticket while `tests/test_sla_*.py` stayed green -- the same
shape as the earlier sub-project whose token-minting function had no caller and
whose reply path was dead behind a passing suite. Every test below drives the
TICKET CORE (`create_ticket`, `change_status`, `update_ticket`,
`assign_ticket`) and asserts on `ticket_sla`. Not one of them calls a tracking
service directly; a deleted call site turns this file red and nothing else.

FIXTURE DISCIPLINE -- what these tests deliberately VARY, and the mutant each
variation catches. This sub-project has repeatedly shipped tests that proved
nothing because a fixture held constant the one parameter the defect would
vary; the most recent was a cascade-guard test whose org held a single
WILDCARD policy, so the recompute it was meant to forbid would have re-resolved
to the SAME policy and produced the SAME deadlines -- the assertion passed
against the bug it existed to catch.

* **Two rival policies, never one wildcard.** `_two_priority_policies` and
  `_two_queue_policies` give the org a 480-minute policy for the ticket's
  starting (queue, priority) and a 60-minute policy for the one it moves to.
  A recompute is therefore visible as a 420-minute swing in
  `resolution_due_at`, and "did not recompute" is a genuinely different
  observation from "recomputed". Under a single wildcard policy every
  no-recompute assertion in this file would pass with the guard deleted.
* **Actor: human vs system.** Every recompute test has a system-actor twin on
  the identical fixture, because `actor_type` is the ONLY thing distinguishing
  a technician re-prioritising a ticket from §0.4's breach escalation doing
  the same write with the same field and the same value.
* **Ticket origin: technician / portal / email / integration / alert.**
  Materialisation must not depend on which surface filed the ticket (§1's
  boundary note). `alert` additionally has NO human requester at all, and
  `portal` runs on a different database role -- the two origins most likely to
  be broken by a wiring that works for the console.
* **Whether a policy resolves at all.** §3's "no match -> no row" is asserted
  next to its control, so "no row" can never pass merely because nothing ever
  materialises.
* **Which status the ticket ENTERS, and how it got there.** A request whose
  category requires approval is CREATED into `pending_approval` and never
  transitions into it, so a pause wired only to `change_status` misses it
  entirely; an incident reaches `pending` only by transition. Both are here.
* **Which terminal status.** `cancelled` (both types) and `rejected`
  (request-only) must void; `closed` -- in the wider `TERMINAL_STATUSES` but
  not in `FINAL_STATUSES` -- must NOT, and is asserted as the control.

TIME. Every org here runs a 24/7 calendar (`_ALWAYS`), so working minutes and
wall-clock minutes coincide and a deadline is checkable by eye. That is a
DELIBERATE simplification for a wiring test and not a gap: the working-time
arithmetic itself, on business-hours and midnight-wrapping calendars and across
both DST transitions, is `test_sla_calendar.py`'s and `test_sla_pause.py`'s
subject. What is under test here is whether the hook fires at all, with which
actor, and against which row. Assertions are written as DIFFERENCES between two
observed deadlines rather than absolute instants, so nothing here depends on
the sub-minute rounding of `add_working_minutes`.
"""

import ast
import logging
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session, portal_session

# `app.models` before any `app.modules.*` import -- importing a service module
# first would start the ORM registry from inside
# `app.modules.service_desk.models` and hit a partially-initialised module
# (`test_sla_tracking.py`/`test_sla_pause.py` order their imports the same way
# for the same reason).
from app.models import Ticket, TicketCategory
from app.modules.service_desk.lifecycle import FINAL_STATUSES
from app.modules.service_desk.schema import TicketCreate, TicketOrigin
from app.modules.service_desk.tickets import create_ticket, update_ticket
from app.modules.service_desk.transitions import assign_ticket, change_status
from app.modules.sla import tracking
from app.modules.sla.models import TicketSla
from tests.test_sla_migration import _seed_calendar, _seed_policy, _seed_queue, _seed_user

# A 24/7 calendar: `00:00-00:00` is migration 0035's midnight-at-end-of-day
# sentinel, i.e. 1440 minutes, NOT a zero-length window (spec §2.2).
_ALWAYS = [(d, time(0, 0), time(0, 0)) for d in range(7)]

# The two targets every rival-policy fixture uses. Far enough apart that a
# recompute is unmistakable and cannot be confused with clock jitter.
_SLOW_RESOLUTION = 480
_FAST_RESOLUTION = 60
_SWING = timedelta(minutes=_SLOW_RESOLUTION - _FAST_RESOLUTION)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_category(
    api_env,
    org_id: UUID,
    *,
    queue_id: UUID,
    type_: str = "incident",
    requires_approval: bool = False,
) -> UUID:
    """One `ticket_category` row pointing at `queue_id`, via the admin
    (RLS-bypassing) engine. `requires_approval=True` is what makes
    `lifecycle.initial_status` land a request directly on `pending_approval`
    -- the creation-time entry into a pausing status that no transition can
    reproduce."""
    actor = await _seed_user(api_env, org_id, email=f"wire-cat-{uuid4().hex[:10]}@test.local")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            category = TicketCategory(
                org_id=org_id,
                key=f"wire-c-{uuid4().hex[:10]}",
                name="Wiring category",
                type=type_,
                requires_approval=requires_approval,
                default_queue_id=queue_id,
                created_by=actor,
            )
            session.add(category)
            await session.flush()
            return category.id


async def _create(
    api_env,
    org_id: UUID,
    *,
    category_id: UUID,
    requester_id: UUID | None,
    actor_id: UUID | None,
    type_: str = "incident",
    priority: str = "normal",
    queue_id: UUID | None = None,
    origin: TicketOrigin = "technician",
) -> UUID:
    """`tickets.create_ticket` through the RLS-constrained app engine -- the
    ordinary caller's path, and the only creation path in this file. Returns
    the ticket id."""
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await create_ticket(
            session,
            org_id,
            actor_id,
            TicketCreate(
                type=type_,
                category_id=category_id,
                queue_id=queue_id,
                subject="wiring",
                priority=priority,
                requester_id=requester_id,
            ),
            origin=origin,
        )
        return ticket.id


async def _change(
    api_env,
    org_id: UUID,
    ticket_id: UUID,
    *,
    to_status: str,
    actor_id: UUID | None,
    actor_type: str = "user",
    approval_decision: bool = False,
) -> None:
    """`transitions.change_status` through the app engine.

    `approval_decision` forwards `change_status`'s private
    `_approval_decision` keyword -- the one `approvals.decide_approval` alone
    passes. It is needed here only to reach `pending_approval -> rejected`,
    which is legal per `lifecycle.py` but reserved to that service
    (`_APPROVAL_ONLY_EDGES`); driving `decide_approval` itself would drag an
    approver role, a role membership and a permission grant into a test about
    where the SLA hook fires. `rejected` is the second member of
    `FINAL_STATUSES` and is unreachable any other way, so it is worth the
    private keyword rather than leaving half the set untested.
    """
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await change_status(
            session,
            org_id,
            ticket_id,
            to_status=to_status,
            actor_id=actor_id,
            actor_type=actor_type,
            _approval_decision=approval_decision,
        )


async def _patch(
    api_env,
    org_id: UUID,
    ticket_id: UUID,
    fields: dict,
    *,
    actor_id: UUID | None,
    actor_type: str = "user",
) -> None:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await update_ticket(
            session,
            org_id,
            actor_id,
            ticket_id,
            fields,
            actor_type=actor_type,  # type: ignore[arg-type]
        )


async def _assign(
    api_env,
    org_id: UUID,
    ticket_id: UUID,
    fields: dict,
    *,
    actor_id: UUID | None,
    actor_type: str = "user",
) -> None:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await assign_ticket(
            session,
            org_id,
            actor_id,
            ticket_id,
            fields,
            actor_type=actor_type,  # type: ignore[arg-type]
        )


async def _row(api_env, ticket_id: UUID) -> TicketSla | None:
    """The `ticket_sla` row read on the BYPASSRLS admin engine -- so what the
    assertions see is what is actually stored, not what one tenant context is
    allowed to see."""
    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(TicketSla).where(TicketSla.ticket_id == ticket_id))
        ).scalar_one_or_none()


async def _ticket(api_env, ticket_id: UUID) -> Ticket:
    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()


async def _wildcard_org(api_env, *, windows: list | None = None, resolution: int = 480):
    """An org with one 24/7 calendar and ONE wildcard `(any queue, any
    priority)` policy, plus a queue, an incident category and a user.

    Used only where the test is about whether a hook fires at all. Every test
    about whether a recompute HAPPENED uses `_two_priority_policies` or
    `_two_queue_policies` instead -- a wildcard org cannot tell a recompute
    from a no-op, because re-resolution lands on the same policy and reproduces
    the same deadlines.
    """
    org_id = await api_env.create_org(f"wire-{uuid4().hex[:8]}")
    calendar_id = await _seed_calendar(
        api_env, org_id, timezone="Europe/London", windows=_ALWAYS if windows is None else windows
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        response_minutes=60,
        resolution_minutes=resolution,
    )
    queue_id = await _seed_queue(api_env, org_id)
    user_id = await _seed_user(api_env, org_id, email=f"wire-u-{uuid4().hex[:10]}@test.local")
    return org_id, queue_id, user_id


async def _two_priority_policies(api_env):
    """An org whose `normal` and `urgent` policies have DIFFERENT resolution
    targets (480 vs 60), and NO wildcard fallback.

    This is the fixture the cascade-guard review said was missing. With a
    single wildcard policy, a re-triage from `normal` to `urgent` re-resolves
    to the same policy and writes back the same deadline, so
    `assert due_at == before` passes whether the guard is present or not. Here
    a recompute moves `resolution_due_at` 420 minutes earlier and the two
    outcomes are impossible to confuse.
    """
    org_id = await api_env.create_org(f"wire-{uuid4().hex[:8]}")
    calendar_id = await _seed_calendar(api_env, org_id, timezone="Europe/London", windows=_ALWAYS)
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="normal",
        response_minutes=120,
        resolution_minutes=_SLOW_RESOLUTION,
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        priority="urgent",
        response_minutes=15,
        resolution_minutes=_FAST_RESOLUTION,
    )
    queue_id = await _seed_queue(api_env, org_id)
    user_id = await _seed_user(api_env, org_id, email=f"wire-u-{uuid4().hex[:10]}@test.local")
    return org_id, queue_id, user_id


async def _two_queue_policies(api_env):
    """The queue-axis twin of `_two_priority_policies`: the ticket's home queue
    resolves to 480 minutes and the escalation queue to 60, with no wildcard.

    §6.0.1 calls the queue move the MORE dangerous half of the cascade,
    precisely because re-resolving after it picks up the escalation queue's own
    (tighter) policy. That is only observable if the two queues actually carry
    different policies.
    """
    org_id = await api_env.create_org(f"wire-{uuid4().hex[:8]}")
    calendar_id = await _seed_calendar(api_env, org_id, timezone="Europe/London", windows=_ALWAYS)
    home_queue_id = await _seed_queue(api_env, org_id)
    other_queue_id = await _seed_queue(api_env, org_id)
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        queue_id=home_queue_id,
        response_minutes=120,
        resolution_minutes=_SLOW_RESOLUTION,
    )
    await _seed_policy(
        api_env,
        org_id,
        calendar_id=calendar_id,
        queue_id=other_queue_id,
        response_minutes=15,
        resolution_minutes=_FAST_RESOLUTION,
    )
    user_id = await _seed_user(api_env, org_id, email=f"wire-u-{uuid4().hex[:10]}@test.local")
    return org_id, home_queue_id, other_queue_id, user_id


# ---------------------------------------------------------------------------
# The meta-guard: every hook has a production caller, in the right function
# ---------------------------------------------------------------------------

# Mirrors `tracking.py`'s "WHERE THE CALLERS ARE" paragraph exactly, one entry
# per call site -- `recompute_for_retriage` has two (a human priority change
# and a human queue change), everything else has one. This is what makes the
# meta-guard below check PLACEMENT, not just presence (review sla-breach-
# wiring, F5): the old form of this test greped the whole concatenated
# package, so `note_cancelled` called from the wrong function -- `assign_
# ticket`, say, instead of `change_status`'s `FINAL_STATUSES` branch -- would
# still satisfy `f"await {hook}(" in sources` and this test would stay green
# over a genuinely broken wire.
_EXPECTED_HOOK_CALL_SITES: dict[str, tuple[tuple[str, str], ...]] = {
    "start_tracking": (("tickets.py", "create_ticket"),),
    "recompute_for_retriage": (
        ("tickets.py", "update_ticket"),
        ("transitions.py", "assign_ticket"),
    ),
    "pause": (("transitions.py", "change_status"),),
    "resume": (("transitions.py", "change_status"),),
    "note_cancelled": (("transitions.py", "change_status"),),
    "note_resolved": (("transitions.py", "change_status"),),
    "note_public_comment": (("threads.py", "add_comment"),),
}


def _function_source(path: Path, function_name: str) -> str:
    """The exact source text of the top-level `def`/`async def
    <function_name>` in `path`, found via `ast` rather than a line-range
    guess or a whole-file grep -- so a call site anywhere else in the file,
    including in a DIFFERENT function, is not mistaken for one inside this
    one. Walks the whole tree (not just top-level `body`) so a hook called
    from a nested helper defined inside `function_name` still counts, which a
    `node.name == function_name` check restricted to `tree.body` would miss.
    """
    source = path.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ):
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f"{path.name}::{function_name} has no source segment"
            return segment
    raise AssertionError(f"{path.name} defines no function named {function_name!r}")


def test_every_tracking_hook_is_called_from_the_ticket_core() -> None:
    """THE DEFECT THIS WHOLE TASK EXISTS FOR, pinned as a test -- and pinned to
    the SPECIFIC function each hook is documented to be called from, not
    merely to the file (review sla-breach-wiring, F5 tightens this from a
    whole-package substring search).

    `start_tracking`, `pause`, `resume` and `recompute_for_retriage` shipped
    with no production caller at all: they were reachable only from
    `tests/test_sla_*.py`, so the SLA engine did nothing on any real ticket
    while its own suite was entirely green. Every behavioural test below would
    also fail if a call site were deleted, but each would fail for a reason
    that reads like an SLA arithmetic bug; this one names the actual cause and
    fails first -- `ast.walk` finds no call at all inside the declared
    function's source when the real call site is removed, exactly as the old
    whole-package substring check did.

    Deliberately a source scan rather than a mock/spy: the property being
    asserted is "a call site exists in this exact function", which is a fact
    about the shipped code, not about one execution path through it.
    """
    core = Path(__file__).resolve().parent.parent / "app" / "modules" / "service_desk"
    for hook, sites in _EXPECTED_HOOK_CALL_SITES.items():
        for filename, function_name in sites:
            source = _function_source(core / filename, function_name)
            assert f"await {hook}(" in source, (
                f"sla.tracking.{hook} has no caller inside {filename}::{function_name} "
                f"-- the SLA engine is inert on real tickets for that hook, exactly as "
                f"it was before Task 8"
            )


# ---------------------------------------------------------------------------
# Creation -> start_tracking (§3, §1's origin-independence)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["technician", "portal", "email", "integration", "alert"])
async def test_creating_a_ticket_materialises_its_sla_whatever_the_origin(
    api_env, origin: str
) -> None:
    """§1: "an SLA record follows from that automatically because §3's
    resolution runs at ticket creation regardless of origin."

    Origin is varied because it is the parameter a wiring defect is most likely
    to be conditional on, and two of these origins are genuinely different
    code paths rather than a different string: `alert` has NO requester and no
    author (`ck_ticket_human_attribution`'s only exemption), and `portal`
    normally runs on a different database role (driven through the real
    `portal_session` in its own test below). A `create_ticket` that materialised
    only for the technician console would pass a single-origin test and leave
    every portal-, email- and alert-filed ticket without a promise.
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)

    ticket_id = await _create(
        api_env,
        org_id,
        category_id=category_id,
        requester_id=None if origin == "alert" else user_id,
        actor_id=None if origin == "alert" else user_id,
        origin=origin,  # type: ignore[arg-type]
    )

    row = await _row(api_env, ticket_id)
    assert row is not None, f"origin={origin!r} got no ticket_sla row"
    assert row.policy_source == "initial"
    ticket = await _ticket(api_env, ticket_id)
    # 480 working minutes on a 24/7 calendar is 480 wall-clock minutes.
    assert row.resolution_due_at - ticket.created_at == timedelta(minutes=480)
    assert row.response_due_at - ticket.created_at == timedelta(minutes=60)
    assert row.voided_at is None
    assert row.paused_at is None


async def test_creating_a_ticket_where_no_policy_resolves_creates_no_row(api_env) -> None:
    """§3: "If nothing matches, no `ticket_sla` row is created" -- not a zero,
    not a default, not a silent infinity.

    The control for this is the test above, on an org that DOES carry a policy:
    without it, "no row" would be satisfied just as well by a `create_ticket`
    that never materialises anything, which is the state this whole task is
    fixing.
    """
    org_id = await api_env.create_org(f"wire-{uuid4().hex[:8]}")
    queue_id = await _seed_queue(api_env, org_id)
    user_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)

    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )

    assert await _ticket(api_env, ticket_id) is not None
    assert await _row(api_env, ticket_id) is None


async def test_an_unsatisfiable_calendar_does_not_stop_a_ticket_being_filed(
    api_env, caplog
) -> None:
    """THE DECISION: an SLA failure may not fail a ticket create.

    A policy pointed at a calendar with NO working windows makes
    `add_working_minutes` raise `UnsatisfiableCalendar` (spec §4's bounded
    iteration). Propagated, that single admin mistake means nobody in the
    organisation can file a ticket at all -- not the portal, not email intake,
    not the console. A customer locked out of raising an incident is strictly
    worse than a ticket without an SLA row, and the latter is not even a new
    state: §3 already declares "no policy -> no row" legitimate and visible,
    and a later human re-triage materialises the row once the calendar is
    fixed.

    Two halves, and the second is what makes the first mean anything:

    1. The create succeeds and leaves NO `ticket_sla` row.
    2. It is not silent -- an ERROR naming the ticket is logged. A swallowed
       exception with no trace would reintroduce, one layer down, the very
       "SLA quietly does nothing" failure this file exists to prevent.

    The org that DOES have a working calendar (`_wildcard_org` above) is the
    control: without it this test would also pass against an implementation
    that never materialised anything for anyone.
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env, windows=[])
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)

    with caplog.at_level(logging.ERROR, logger="its.sla.tracking"):
        ticket_id = await _create(
            api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
        )

    assert await _ticket(api_env, ticket_id) is not None
    assert await _row(api_env, ticket_id) is None
    assert any(
        str(ticket_id) in record.getMessage() and "start_tracking" in record.getMessage()
        for record in caplog.records
    ), "an SLA materialisation that could not happen must not be silent"


async def test_a_tenancy_failure_in_the_sla_hook_is_not_swallowed(api_env) -> None:
    """The other side of the tolerance decision: the catch is
    `UnsatisfiableCalendar` and NOT `SlaValidationError`.

    `_require_same_org`'s cross-org refusal is an `SlaValidationError`, and
    invariant #5 failures must stay loud rather than being logged away as if
    they were an admin's calendar typo. Asserted by calling `start_tracking`
    inside the tolerance context with a ticket from another org: if the catch
    were widened to the base class, this raise would be swallowed and the test
    would fail.
    """
    from app.modules.sla.errors import SlaValidationError

    org_a, queue_a, user_a = await _wildcard_org(api_env)
    org_b, _queue_b, _user_b = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_a, queue_id=queue_a)
    ticket_id = await _create(
        api_env, org_a, category_id=category_id, requester_id=user_a, actor_id=user_a
    )
    ticket = await _ticket(api_env, ticket_id)

    with pytest.raises(SlaValidationError):
        async with tracking.sla_misconfiguration_is_not_fatal("start_tracking", ticket_id):
            async with org_session(
                org_b, engine=api_env.app_engine, clients=ClientScope.all()
            ) as session:
                await tracking.start_tracking(session, org_b, ticket)


async def test_a_bare_value_error_is_not_swallowed() -> None:
    """F3 (review sla-breach-wiring): the tuple was `(UnsatisfiableCalendar,
    ValueError)`; it is narrowed to `UnsatisfiableCalendar` alone.

    The three `ValueError`s `calendar.py` can raise (naive-datetime,
    span-exceeds-`MAX_LOOKAHEAD_DAYS`, negative-minutes) are all programming-
    error guards, not admin misconfiguration -- every legitimate cause of each
    is already refused earlier, at write time, by something else (a stored
    calendar's timezone is validated when it is saved, `ticket.created_at` is
    a DB `TIMESTAMPTZ` and never naive, `sla_policy`'s target-minute columns
    carry `CHECK ... > 0`). Tolerating a bare `ValueError` here means a
    genuine arithmetic bug in this module degrades an entire org to "no SLA
    rows, one ERROR log line" instead of failing loudly -- the very failure
    class this whole task exists to fix, one exception type over
    (`tracking.py`'s docstring). No ticket-core call site is needed to prove
    this: the wrapper's job is defined entirely by what it catches, so this
    raises directly inside it.
    """
    with pytest.raises(ValueError, match="boom"):
        async with tracking.sla_misconfiguration_is_not_fatal("start_tracking", uuid4()):
            raise ValueError("boom")


# ---------------------------------------------------------------------------
# The portal: creation-time materialisation on the requester's own role
# ---------------------------------------------------------------------------


async def test_a_portal_filed_ticket_materialises_its_sla_on_the_portal_connection(
    api_env, portal_engine
) -> None:
    """Migration 0038's reason for existing, driven through the REAL
    `portal_session` connection rather than a stand-in for it.

    `portal.tickets.create_my_ticket` delegates to `create_ticket` on the
    requester's own `portal_user` connection (migration 0030 removed the
    second, elevated one deliberately), so creation-time materialisation
    INSERTs into `ticket_sla` as `portal_user`. Unlike the read path -- where
    `note_public_comment` orders its requester comparison before any
    `ticket_sla` access and never touches the table -- an INSERT cannot be
    dodged by ordering. Without 0038's grant this raises
    `asyncpg.exceptions.InsufficientPrivilegeError: permission denied for
    table ticket_sla`, and it raises on the SELECT before the INSERT is even
    attempted, which is why SELECT is granted too.
    """
    from app.modules.portal.schema import PortalTicketCreate
    from app.modules.portal.tickets import create_my_ticket

    org_id, queue_id, _user_id = await _wildcard_org(api_env)
    requester_id = await _seed_user(api_env, org_id, email=f"pw-{uuid4().hex[:10]}@test.local")
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)

    async with portal_session(
        org_id, requester_id, engine=portal_engine, clients=ClientScope.all()
    ) as session:
        detail = await create_my_ticket(
            session,
            org_id=org_id,
            requester_id=requester_id,
            data=PortalTicketCreate(category_id=category_id, subject="my laptop", description=""),
        )

    row = await _row(api_env, detail.id)
    assert row is not None
    assert row.policy_source == "initial"


async def test_a_requester_cannot_read_another_requesters_sla_row(
    api_env, portal_engine
) -> None:
    """THE ASSERTION THAT MAKES 0038'S GRANT SAFE.

    `tenant_isolation` is PERMISSIVE with `roles={public}`, so a bare
    `GRANT SELECT ON ticket_sla TO portal_user` would let any requester read
    EVERY SLA row in the org -- every colleague's deadlines, every breach,
    every escalation. That is the blanket access invariant #5 forbids, and it
    was correctly rejected once already on the read path.

    Two requesters in ONE org, each with a ticket, is what makes this
    behavioural: an org-scoped-only test would pass with the restrictive
    policy deleted, because both rows are in the same org. Here the second
    requester must see exactly one row -- their own.
    """
    from app.modules.portal.schema import PortalTicketCreate
    from app.modules.portal.tickets import create_my_ticket

    org_id, queue_id, _user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    mine = await _seed_user(api_env, org_id, email=f"pw-a-{uuid4().hex[:10]}@test.local")
    theirs = await _seed_user(api_env, org_id, email=f"pw-b-{uuid4().hex[:10]}@test.local")

    ids = []
    for requester_id in (mine, theirs):
        async with portal_session(
            org_id, requester_id, engine=portal_engine, clients=ClientScope.all()
        ) as session:
            detail = await create_my_ticket(
                session,
                org_id=org_id,
                requester_id=requester_id,
                data=PortalTicketCreate(category_id=category_id, subject="hi", description=""),
            )
            ids.append(detail.id)

    # Both rows really exist in the org...
    assert await _row(api_env, ids[0]) is not None
    assert await _row(api_env, ids[1]) is not None

    # ...and each requester sees exactly one of them.
    async with portal_session(
        org_id, mine, engine=portal_engine, clients=ClientScope.all()
    ) as session:
        visible = (await session.execute(select(TicketSla.ticket_id))).scalars().all()
    assert list(visible) == [ids[0]]


async def test_a_requester_cannot_insert_an_sla_row_onto_another_requesters_ticket(
    api_env, portal_engine
) -> None:
    """The write half of the same policy. `portal_isolation` on `ticket_sla`
    defines `USING` and no separate `WITH CHECK`, and PostgreSQL then uses the
    `USING` expression for the write-side check too -- the same property
    migration 0030 established empirically for
    `ticket_status_history`/`ticket_approval`. Asserted rather than assumed,
    because if it did NOT hold, the INSERT grant would let any requester
    fabricate a promise on any ticket in the org."""
    from sqlalchemy.exc import ProgrammingError

    org_id, queue_id, staff_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    victim = await _seed_user(api_env, org_id, email=f"pw-v-{uuid4().hex[:10]}@test.local")
    attacker = await _seed_user(api_env, org_id, email=f"pw-x-{uuid4().hex[:10]}@test.local")

    # The victim's ticket, filed by staff on the ordinary connection. Its
    # `ticket_sla` row already exists, so the attacker's INSERT is refused by
    # the policy rather than by the unique constraint -- the row is deleted
    # first to remove that alternative explanation.
    victim_ticket = await _create(
        api_env, org_id, category_id=category_id, requester_id=victim, actor_id=staff_id
    )
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            await session.execute(
                text("DELETE FROM ticket_sla WHERE ticket_id = :t"), {"t": victim_ticket}
            )
    assert await _row(api_env, victim_ticket) is None

    policy_id, calendar_id = await _any_policy(api_env, org_id)
    with pytest.raises(ProgrammingError) as caught:
        async with portal_session(
            org_id, attacker, engine=portal_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                TicketSla(
                    org_id=org_id,
                    ticket_id=victim_ticket,
                    policy_id=policy_id,
                    calendar_id=calendar_id,
                    response_due_at=datetime.now(UTC),
                    resolution_due_at=datetime.now(UTC),
                    policy_source="initial",
                )
            )
    # The POLICY refused it, not the unique constraint (the row was deleted
    # above) and not the grant (INSERT is held).
    assert "row-level security policy" in str(caught.value)
    assert await _row(api_env, victim_ticket) is None


async def _any_policy(api_env, org_id: UUID) -> tuple[UUID, UUID]:
    from app.models import SlaPolicy

    async with AsyncSession(api_env.admin_engine) as session:
        policy = (
            await session.execute(select(SlaPolicy).where(SlaPolicy.org_id == org_id))
        ).scalars().first()
        assert policy is not None
        return policy.id, policy.calendar_id


# ---------------------------------------------------------------------------
# Pause and resume (§5)
# ---------------------------------------------------------------------------


async def test_a_request_created_into_pending_approval_is_born_paused(api_env) -> None:
    """The status a ticket ENTERS is a fixture parameter, and this is the case
    a `change_status`-only wiring cannot reach at all.

    `lifecycle.initial_status` puts a request whose category requires approval
    directly on `pending_approval`, and `_TRANSITIONS` gives requests no other
    door into that status (`new -> pending_approval` exists, but creation never
    returns `new` for a request). So the ticket never *transitions* into a
    pausing status: a `pause` wired only to `change_status` would leave the
    clock running for the whole time the ticket is blocked on an approver, and
    the eventual `pending_approval -> in_progress` would call `resume` on a row
    with `paused_at IS NULL` -- a documented no-op. The entire approval wait
    would be charged to the service desk.

    `_materialise` therefore writes `paused_at` in the INSERT (not via a
    follow-up `pause`, which would be an UPDATE `portal_user` deliberately
    does not hold -- see migration 0038).
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(
        api_env, org_id, queue_id=queue_id, type_="request", requires_approval=True
    )

    ticket_id = await _create(
        api_env,
        org_id,
        category_id=category_id,
        requester_id=user_id,
        actor_id=user_id,
        type_="request",
    )

    assert (await _ticket(api_env, ticket_id)).status == "pending_approval"
    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.paused_at is not None


async def test_a_ticket_created_into_an_ordinary_status_is_not_born_paused(api_env) -> None:
    """The control for the test above, and the reason it proves anything: on
    the SAME fixture shape, an incident created into `new` must have
    `paused_at IS NULL`. Without this, "born paused" would be equally satisfied
    by materialising every row paused, which would stop the clock on every
    ticket in the system and make the whole engine silently do nothing --
    a larger version of the defect this task is fixing."""
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)

    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )

    assert (await _ticket(api_env, ticket_id)).status == "new"
    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.paused_at is None


@pytest.mark.parametrize(
    ("type_", "warm_up", "pausing_status"),
    [("incident", ["open"], "pending"), ("request", [], "pending_approval")],
)
async def test_entering_a_pausing_status_stops_the_clock(
    api_env, type_: str, warm_up: list[str], pausing_status: str
) -> None:
    """§5, through `change_status`. Both branches of `PAUSING_STATUSES` are
    driven, because `pending` is incident-only and `pending_approval` is
    request-only (`ck_ticket_status_matches_type`) -- a wiring that hard-coded
    one string would pass a single-type test and silently never pause the other
    half of the ticket population.

    The request case is set up by moving a non-approval request back to `new`
    on the admin engine, which is the only way to reach the legal
    `new -> pending_approval` edge from a ticket the app created; the
    creation-time entry into the same status has its own test above.
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id, type_=type_)
    ticket_id = await _create(
        api_env,
        org_id,
        category_id=category_id,
        requester_id=user_id,
        actor_id=user_id,
        type_=type_,
    )
    started = await _row(api_env, ticket_id)
    assert started is not None
    assert started.paused_at is None

    if type_ == "request":
        async with AsyncSession(api_env.admin_engine) as session:
            async with session.begin():
                await session.execute(
                    text("UPDATE ticket SET status = 'new' WHERE id = :t"), {"t": ticket_id}
                )
    for status in warm_up:
        await _change(api_env, org_id, ticket_id, to_status=status, actor_id=user_id)

    await _change(api_env, org_id, ticket_id, to_status=pausing_status, actor_id=user_id)

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.paused_at is not None


async def test_an_ordinary_transition_does_not_stop_the_clock(api_env) -> None:
    """The control: `new -> open` is not a pausing edge and must leave
    `paused_at` NULL. A `change_status` that paused on every transition would
    satisfy the test above and stop the clock on the entire fleet."""
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )

    await _change(api_env, org_id, ticket_id, to_status="open", actor_id=user_id)

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.paused_at is None


async def test_leaving_a_pausing_status_credits_the_pause_to_the_deadlines(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5's round trip through `change_status`: `open -> pending -> open`
    clears `paused_at`, accumulates the pause into `paused_minutes_total`, and
    re-derives every still-running deadline by exactly that many working
    minutes.

    Time is frozen explicitly at each hook, because the credit is
    `working_minutes_between(paused_at, now)` and a test that let `now` be the
    real clock could only assert a tolerance -- which is how a wall-clock
    implementation slips through. On this 24/7 calendar 90 wall-clock minutes
    are 90 working minutes; that the two DIVERGE on a business-hours calendar
    is `test_sla_pause.py`'s subject, not this file's.
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )
    before = await _row(api_env, ticket_id)
    assert before is not None
    await _change(api_env, org_id, ticket_id, to_status="open", actor_id=user_id)

    paused_at = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(tracking, "_utcnow", lambda: paused_at)
    await _change(api_env, org_id, ticket_id, to_status="pending", actor_id=user_id)
    monkeypatch.setattr(tracking, "_utcnow", lambda: paused_at + timedelta(minutes=90))
    await _change(api_env, org_id, ticket_id, to_status="open", actor_id=user_id)

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.paused_at is None
    assert row.paused_minutes_total == 90
    assert row.resolution_due_at - before.resolution_due_at == timedelta(minutes=90)
    assert row.response_due_at - before.response_due_at == timedelta(minutes=90)


async def test_resume_runs_before_resolution_stops_the_clock(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORDERING IS BEHAVIOUR on the `pending -> resolved` edge, which
    `lifecycle.py` added on purpose ("the third party comes back and the fix
    lands directly from there").

    `resume` must run BEFORE `note_resolved` inside `change_status`. Resuming
    first credits the pause to the still-running deadlines and only then stops
    the clocks, so a ticket blocked for 90 minutes and fixed the moment the
    third party replied reads as met against its extended deadline. In the
    other order `note_resolved` stops both clocks first, `resume`'s
    running-clock guard then declines to re-derive anything, and the credit is
    discarded at the only moment it mattered.

    The assertion distinguishes the two orders directly: `resolution_due_at`
    moves by the pause under the correct order and does not move at all under
    the wrong one. `paused_minutes_total` is stamped either way, which is why
    it is NOT what this test asserts on.
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )
    before = await _row(api_env, ticket_id)
    assert before is not None
    await _change(api_env, org_id, ticket_id, to_status="open", actor_id=user_id)

    paused_at = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(tracking, "_utcnow", lambda: paused_at)
    await _change(api_env, org_id, ticket_id, to_status="pending", actor_id=user_id)
    monkeypatch.setattr(tracking, "_utcnow", lambda: paused_at + timedelta(minutes=90))
    await _change(api_env, org_id, ticket_id, to_status="resolved", actor_id=user_id)

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.paused_at is None
    assert row.resolution_met_at is not None, "note_resolved did not run at all"
    assert row.resolution_due_at - before.resolution_due_at == timedelta(minutes=90), (
        "the pause credit was discarded -- note_resolved stopped the clocks before "
        "resume could re-derive the deadlines"
    )


# ---------------------------------------------------------------------------
# Re-triage (§0.3) and the escalation loop guard (§0.4)
# ---------------------------------------------------------------------------


async def test_a_human_priority_change_recomputes_against_the_new_policy(api_env) -> None:
    """§0.3 through `update_ticket`. The org carries two rival policies, so a
    recompute is visible as a 420-minute swing rather than as a no-op that
    looks identical to the guarded path."""
    org_id, queue_id, user_id = await _two_priority_policies(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env,
        org_id,
        category_id=category_id,
        requester_id=user_id,
        actor_id=user_id,
        priority="normal",
    )
    before = await _row(api_env, ticket_id)
    assert before is not None
    assert before.policy_source == "initial"

    await _patch(api_env, org_id, ticket_id, {"priority": "urgent"}, actor_id=user_id)

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.policy_source == "retriage"
    assert before.resolution_due_at - row.resolution_due_at == _SWING


async def test_a_system_priority_change_does_not_recompute(api_env) -> None:
    """§0.4's LOOP GUARD, on the fixture that can actually see it.

    `sla.breach._escalate` reaches `update_ticket` with `actor_type="system"`
    and `{"priority": ...}` -- the identical field and, for a `normal -> high`
    bump, an identical-shaped value to a technician's re-triage. `actor_type`
    is the ONLY thing at this call site that tells the two apart. If the guard
    were removed, the escalation would recompute from `created_at` against the
    tighter policy, breach again in the same second, escalate again, and fire a
    burst of notifications until `urgent` capped it.

    The org carries a real `urgent` policy with a DIFFERENT target, which is
    the whole point: under a single wildcard policy the recompute would
    re-resolve to the same policy and write back the same deadline, and this
    assertion would pass with the guard deleted -- exactly the mistake the
    cascade-guard review found.
    """
    org_id, queue_id, user_id = await _two_priority_policies(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env,
        org_id,
        category_id=category_id,
        requester_id=user_id,
        actor_id=user_id,
        priority="normal",
    )
    before = await _row(api_env, ticket_id)
    assert before is not None

    await _patch(api_env, org_id, ticket_id, {"priority": "urgent"}, actor_id=None,
                 actor_type="system")

    assert (await _ticket(api_env, ticket_id)).priority == "urgent"
    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.policy_source == "initial", "a system priority bump restamped policy_source"
    assert row.resolution_due_at == before.resolution_due_at
    assert row.response_due_at == before.response_due_at


async def test_a_patch_that_does_not_change_the_priority_does_not_recompute(api_env) -> None:
    """Keyed on the priority having actually CHANGED, not on the key being
    present. Re-sending the priority a ticket already has, or editing only the
    subject, changes no `(queue, priority)` pair and so changes no promise;
    recomputing there would restamp `policy_source`/`last_recomputed_at` for an
    edit nobody made."""
    org_id, queue_id, user_id = await _two_priority_policies(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env,
        org_id,
        category_id=category_id,
        requester_id=user_id,
        actor_id=user_id,
        priority="normal",
    )
    before = await _row(api_env, ticket_id)
    assert before is not None

    await _patch(
        api_env, org_id, ticket_id, {"priority": "normal", "subject": "edited"},
        actor_id=user_id,
    )

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.policy_source == "initial"
    assert row.last_recomputed_at == before.last_recomputed_at


async def test_a_human_queue_move_recomputes_against_the_new_queues_policy(api_env) -> None:
    """The queue half of §3's "a priority or queue change with a human actor",
    which lives on `assign_ticket` because `queue_id` is refused by
    `update_ticket` (§6.0). Two queue-scoped policies, so the move is visible."""
    org_id, home_queue, other_queue, user_id = await _two_queue_policies(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=home_queue)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )
    before = await _row(api_env, ticket_id)
    assert before is not None

    await _assign(api_env, org_id, ticket_id, {"queue_id": other_queue}, actor_id=user_id)

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.policy_source == "retriage"
    assert before.resolution_due_at - row.resolution_due_at == _SWING


async def test_a_system_queue_move_does_not_recompute(api_env) -> None:
    """§6.0.1: the queue move is the MORE dangerous half of the cascade,
    because re-resolving after it picks up the ESCALATION QUEUE's own policy --
    "§0.4's cascade arriving by a second door". `sla.breach._escalate` reaches
    `assign_ticket` with `actor_type="system"` on exactly this path, and the
    escalation queue here carries a genuinely tighter policy, so a missing
    guard changes the deadline by 420 minutes rather than by nothing."""
    org_id, home_queue, other_queue, user_id = await _two_queue_policies(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=home_queue)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )
    before = await _row(api_env, ticket_id)
    assert before is not None

    await _assign(api_env, org_id, ticket_id, {"queue_id": other_queue}, actor_id=None,
                  actor_type="system")

    assert (await _ticket(api_env, ticket_id)).queue_id == other_queue
    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.policy_source == "initial"
    assert row.resolution_due_at == before.resolution_due_at


async def test_an_assignment_that_does_not_move_the_queue_does_not_recompute(api_env) -> None:
    """`assign_ticket` also assigns PEOPLE, and `resolve_policy` keys on
    `(queue, priority)` -- not on the assignee. Handing a ticket to a colleague
    must not restamp the promise, and re-sending the queue the ticket is
    already in must not either."""
    org_id, home_queue, _other_queue, user_id = await _two_queue_policies(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=home_queue)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )
    before = await _row(api_env, ticket_id)
    assert before is not None
    assignee = await _seed_user(api_env, org_id, email=f"wire-a-{uuid4().hex[:10]}@test.local")

    await _assign(
        api_env,
        org_id,
        ticket_id,
        {"assigned_to": assignee, "queue_id": home_queue},
        actor_id=user_id,
    )

    assert (await _ticket(api_env, ticket_id)).assigned_to == assignee
    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.policy_source == "initial"
    assert row.last_recomputed_at == before.last_recomputed_at


# ---------------------------------------------------------------------------
# Voiding a terminal-not-resolved promise (§6.0, migration 0038)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_", "requires_approval", "final_status"),
    [
        ("incident", False, "cancelled"),
        ("request", False, "cancelled"),
        ("request", True, "rejected"),
    ],
)
async def test_a_terminal_not_resolved_ticket_has_its_promise_voided(
    api_env, type_: str, requires_approval: bool, final_status: str
) -> None:
    """Both members of `FINAL_STATUSES` and both ticket types.

    Without this, both clocks keep running on a ticket nobody will ever work
    again, so EVERY cancelled or rejected ticket breaches with certainty once
    past its deadline -- roughly 0.1-0.2 phantom `ticket.sla_breached` events
    per ticket filed at a 5-10% cancellation rate, each indistinguishable in
    reporting from a promise the team really missed. §6's escalation then
    RAISES on such a ticket (it refuses `TERMINAL_STATUSES`), which inside a
    per-org sweep transaction rolls back the cycle's real breaches and retries
    the identical failure every interval, forever.

    **Neither met nor breached.** `response_met_at`/`resolution_met_at` are
    asserted still NULL, because met-stamping on cancellation would claim the
    promise was KEPT and make "cancel it" a one-click way to inflate SLA
    attainment.

    `rejected` is the third case and is deliberately included even though it
    costs the private `_approval_decision` keyword to reach: it is the second
    member of `FINAL_STATUSES`, it is reachable ONLY from `pending_approval`,
    and a hook that tested `to_status == "cancelled"` instead of set
    membership would pass the first two cases and leave every rejected request
    breaching forever. That case also arrives on a row that was BORN PAUSED
    (its category requires approval), so it exercises resume-then-void in one
    transition.
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(
        api_env, org_id, queue_id=queue_id, type_=type_, requires_approval=requires_approval
    )
    ticket_id = await _create(
        api_env,
        org_id,
        category_id=category_id,
        requester_id=user_id,
        actor_id=user_id,
        type_=type_,
    )
    started = await _row(api_env, ticket_id)
    assert started is not None
    assert started.voided_at is None

    await _change(
        api_env,
        org_id,
        ticket_id,
        to_status=final_status,
        actor_id=user_id,
        approval_decision=final_status == "rejected",
    )

    assert (await _ticket(api_env, ticket_id)).status in FINAL_STATUSES
    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.voided_at is not None, f"{final_status!r} left the promise running"
    assert row.response_met_at is None, "cancelling met-stamped the response promise"
    assert row.resolution_met_at is None, "cancelling met-stamped the resolution promise"
    assert row.response_breached_at is None
    assert row.resolution_breached_at is None


async def test_closing_a_ticket_does_not_void_its_promise(api_env) -> None:
    """THE CONTROL, and the reason `FINAL_STATUSES` and not `TERMINAL_STATUSES`.

    `closed` is in the wider `TERMINAL_STATUSES` and is deliberately
    REOPENABLE. A closed ticket's promise was DISCHARGED -- it passed through
    `resolved`/`fulfilled`, where `note_resolved` stopped both clocks -- not
    voided, and voiding it would additionally leave a legally-reopened ticket
    carrying a dead promise with no path back. A hook gated on
    `TERMINAL_STATUSES` passes every assertion in the test above and fails
    here, which is the only place the two sets differ.
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )

    await _change(api_env, org_id, ticket_id, to_status="open", actor_id=user_id)
    await _change(api_env, org_id, ticket_id, to_status="resolved", actor_id=user_id)
    await _change(api_env, org_id, ticket_id, to_status="closed", actor_id=user_id)

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.voided_at is None
    # Discharged, not voided: `note_resolved` stopped both clocks on the way.
    assert row.response_met_at is not None
    assert row.resolution_met_at is not None


async def test_voiding_does_not_erase_a_breach_that_already_happened(api_env) -> None:
    """The mirror of the met-stamping hazard. If voiding cleared or masked a
    recorded breach, "cancel it" would become a way to ERASE a missed promise
    from reporting -- the same laundering in the opposite direction.

    `note_cancelled` writes only `voided_at`, so a clock that had already
    stopped keeps its stamp: the row reads "breached, and later voided", which
    is the truth. The breach is stamped directly here rather than through
    `sla.breach`, so this test is independent of that module.
    """
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )
    breached_at = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    async with AsyncSession(api_env.admin_engine) as session:
        async with session.begin():
            await session.execute(
                text("UPDATE ticket_sla SET response_breached_at = :b WHERE ticket_id = :t"),
                {"b": breached_at, "t": ticket_id},
            )

    await _change(api_env, org_id, ticket_id, to_status="cancelled", actor_id=user_id)

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.voided_at is not None
    assert row.response_breached_at == breached_at
    assert row.response_met_at is None


async def test_cancelling_a_ticket_with_no_sla_row_is_a_no_op(api_env) -> None:
    """§3's no-policy org must remain able to cancel a ticket. A
    `note_cancelled` that assumed a row exists would turn "this org configured
    no SLA" into "this org cannot close anything"."""
    org_id = await api_env.create_org(f"wire-{uuid4().hex[:8]}")
    queue_id = await _seed_queue(api_env, org_id)
    user_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )

    await _change(api_env, org_id, ticket_id, to_status="cancelled", actor_id=user_id)

    assert (await _ticket(api_env, ticket_id)).status == "cancelled"
    assert await _row(api_env, ticket_id) is None


async def test_a_ticket_cancelled_out_of_pending_closes_its_pause_then_voids(
    api_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pending -> cancelled` runs BOTH hooks in one transition, and their
    order is asserted through their effects: `resume` clears `paused_at` and
    banks the credit, then `note_cancelled` voids. A void that ran first would
    leave the pause open forever on a row nothing will ever touch again."""
    org_id, queue_id, user_id = await _wildcard_org(api_env)
    category_id = await _seed_category(api_env, org_id, queue_id=queue_id)
    ticket_id = await _create(
        api_env, org_id, category_id=category_id, requester_id=user_id, actor_id=user_id
    )
    await _change(api_env, org_id, ticket_id, to_status="open", actor_id=user_id)

    paused_at = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(tracking, "_utcnow", lambda: paused_at)
    await _change(api_env, org_id, ticket_id, to_status="pending", actor_id=user_id)
    monkeypatch.setattr(tracking, "_utcnow", lambda: paused_at + timedelta(minutes=45))
    await _change(api_env, org_id, ticket_id, to_status="cancelled", actor_id=user_id)

    row = await _row(api_env, ticket_id)
    assert row is not None
    assert row.paused_at is None
    assert row.paused_minutes_total == 45
    assert row.voided_at == paused_at + timedelta(minutes=45)
