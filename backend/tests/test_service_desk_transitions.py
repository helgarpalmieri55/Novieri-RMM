"""Status transitions and assignment (docs/modules/service-desk/spec.md §3,
§5, §8.3).

Covers: every legal transition in BOTH lifecycles moves `status` and writes
exactly one new `ticket_status_history` row with the correct `from_status`
(parametrized directly off the edges named in `lifecycle.py`/spec §3.1/§3.2,
not re-derived); illegal transitions raise `TicketValidationError` and leave
the ticket completely unchanged -- status, history row count, AND the
`resolved_at`/`closed_at` stamps -- re-checked via `session.expire()` before
a re-read so the identity map cannot mask a bug (a plain re-select of an
already-loaded object proves nothing; task 8's S4 fix: asserting only status
and history count would still pass if the stamping block ran before
`assert_transition`); `resolved`/`fulfilled` stamp `resolved_at`, `closed`
stamps `closed_at`, and ANY reopen (leaving `resolved`/`fulfilled` for an
active status, or leaving `closed` at all -- both shapes, no exception,
task 8's S2 fix) clears `resolved_at`, while progression-onward or
cancellation leaves the earlier stamp alone; `actor_type="system"`/
`actor_id=None` is recorded as such in BOTH the history row and the audit
event (task 8's S5 fix: the audit event is asserted explicitly, not just the
history row) and the actor-type/actor-id pairing is validated both ways;
`assign_ticket` takes a `fields: dict` (task 8's S1 fix, matching
`tickets.update_ticket`'s presence convention -- a key's PRESENCE, not its
value, decides whether it is touched) and resolves the assignee org-scoped
(cross-org on a raw BYPASSRLS admin session, per spec §8.1's defence-in-depth
requirement -- never inside `org_session`, where RLS alone would mask the
other org's row and prove nothing about the explicit predicate), can set and
clear `assigned_to`, and moves `queue_id` only when one is supplied, leaving
it -- and a real, non-None incumbent assignee -- untouched when omitted
(task 8's S10 fix); and each of `ticket.status_changed` / `ticket.assigned`
audits exactly once per call.

Whole-branch security review, finding A1: `pending_approval -> in_progress`
and `pending_approval -> rejected` are legal per `lifecycle.py` (unchanged --
`test_service_desk_lifecycle.py` still pins that) but are now refused when
`change_status` is called the way the generic `/status` route calls it, and
succeed only with the private `_approval_decision=True` keyword
`approvals.decide_approval` alone passes. Both halves are covered explicitly
below; `_REQUEST_EDGES` above no longer includes those two edges, since the
generic path this test parametrizes no longer reaches them.
"""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_extensions import uuid7

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Ticket, TicketStatusHistory, User
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.lifecycle import can_transition
from app.modules.service_desk.refdata import create_category, create_queue
from app.modules.service_desk.schema import TicketCreate
from app.modules.service_desk.tickets import create_ticket, get_ticket
from app.modules.service_desk.transitions import assign_ticket, change_status


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Mirrors `test_service_desk_tickets.py`'s helper of the same name/shape
    -- not imported across test modules, per convention."""
    org_id = await api_env.create_org("sd-transitions")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _queue(session, org_id, user_id, key="helpdesk"):
    return await create_queue(session, org_id, user_id, key=key, name=key.title())


async def _category(
    session,
    org_id,
    user_id,
    *,
    key="hardware",
    type="incident",
    requires_approval=False,
    default_queue_id=None,
):
    return await create_category(
        session,
        org_id,
        user_id,
        key=key,
        name=key.title(),
        type=type,
        requires_approval=requires_approval,
        default_queue_id=default_queue_id,
    )


async def _create_incident(session, org_id, user_id):
    queue = await _queue(session, org_id, user_id, key="helpdesk")
    category = await _category(session, org_id, user_id, key="hardware", type="incident")
    return await create_ticket(
        session,
        org_id,
        user_id,
        TicketCreate(
            type="incident",
            category_id=category.id,
            queue_id=queue.id,
            subject="Laptop won't boot",
            requester_id=user_id,
        ),
    )


async def _create_request(session, org_id, user_id, *, requires_approval=False):
    queue = await _queue(session, org_id, user_id, key="requests")
    category = await _category(
        session,
        org_id,
        user_id,
        key="software_request",
        type="request",
        requires_approval=requires_approval,
    )
    return await create_ticket(
        session,
        org_id,
        user_id,
        TicketCreate(
            type="request",
            category_id=category.id,
            queue_id=queue.id,
            subject="Need VPN access",
            requester_id=user_id,
        ),
    )


async def _history_rows(session, org_id, ticket_id) -> list[TicketStatusHistory]:
    return list(
        (
            await session.execute(
                select(TicketStatusHistory)
                .where(
                    TicketStatusHistory.org_id == org_id,
                    TicketStatusHistory.ticket_id == ticket_id,
                )
                .order_by(TicketStatusHistory.id)
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# change_status -- every legal edge, both lifecycles (spec §3.1, §3.2)
# ---------------------------------------------------------------------------

# Mirrors lifecycle.py's `_TRANSITIONS` table exactly (not imported -- this
# suite is meant to catch the table and this list disagreeing, the same
# reason test_service_desk_lifecycle.py lists edges directly rather than
# importing the private dict).
_INCIDENT_EDGES = [
    ("new", "open"),
    ("new", "cancelled"),
    ("open", "pending"),
    ("open", "resolved"),
    ("open", "cancelled"),
    ("pending", "open"),
    ("pending", "resolved"),
    ("pending", "cancelled"),
    ("resolved", "open"),
    ("resolved", "closed"),
    ("resolved", "cancelled"),
    ("closed", "open"),
]

# `("pending_approval", "in_progress")` and `("pending_approval", "rejected")`
# are DELIBERATELY ABSENT here, even though `lifecycle.py` says both are legal
# `request` transitions (test_service_desk_lifecycle.py's
# `test_request_walks_approval_to_closed` pins that unchanged fact). A1 fix
# (whole-branch security review): those two edges are reserved for
# `approvals.decide_approval` alone -- the generic `change_status` call this
# parametrized test makes (no `_approval_decision` keyword) now refuses them.
# See `test_pending_approval_edges_are_legal_in_lifecycle_but_refused_via_generic_change_status`
# below for that refusal, proven directly against the exact edges this list
# used to (wrongly) claim were reachable through the generic path.
_REQUEST_EDGES = [
    # `("new", "pending_approval")` is legal per `lifecycle.py`'s `_TRANSITIONS`
    # table and exercised here even though it is currently UNREACHABLE through
    # any real API flow: `lifecycle.initial_status` never returns `"new"` for
    # a request (creation lands directly on `pending_approval` or
    # `in_progress`, `lifecycle.py`'s own comment says so), and `change_status`
    # is the only place `status` moves after creation, so no legitimate caller
    # can ever hand a request ticket a `from_status` of `"new"` to drive this
    # edge from. Testing it anyway deliberately MIRRORS the shipped
    # `_TRANSITIONS` table rather than a second, narrower belief about which
    # transitions this test suite considers "real" -- see
    # `test_service_desk_events.py`'s
    # `test_change_status_entering_pending_approval_publishes_approval_requested`,
    # which forces this exact edge the same way to prove `ticket.
    # approval_requested` also publishes here, not only from `create_ticket`'s
    # far more common entry to the same state.
    ("new", "pending_approval"),
    ("new", "in_progress"),
    ("new", "cancelled"),
    ("pending_approval", "cancelled"),
    ("in_progress", "fulfilled"),
    ("in_progress", "cancelled"),
    ("fulfilled", "in_progress"),
    ("fulfilled", "closed"),
    ("fulfilled", "cancelled"),
    ("closed", "in_progress"),
]


@pytest.mark.parametrize("from_status,to_status", _INCIDENT_EDGES)
async def test_every_legal_incident_transition_moves_status_and_writes_one_history_row(
    api_env, from_status: str, to_status: str
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        # Force the "from" state directly -- create_ticket only ever produces
        # "new"; every other from_status under test is reached the same way
        # test_service_desk_tickets.py forces "cancelled"/"closed" for its
        # terminal-edit tests.
        ticket.status = from_status
        await session.flush()
        before_count = len(await _history_rows(session, org_id, ticket.id))

        updated = await change_status(
            session, org_id, ticket.id, to_status=to_status, actor_id=user_id
        )

        assert updated.status == to_status
        rows = await _history_rows(session, org_id, ticket.id)
        assert len(rows) == before_count + 1
        newest = rows[-1]
        assert newest.from_status == from_status
        assert newest.to_status == to_status
        assert newest.actor_type == "user"
        assert newest.actor_id == user_id


@pytest.mark.parametrize("from_status,to_status", _REQUEST_EDGES)
async def test_every_legal_request_transition_moves_status_and_writes_one_history_row(
    api_env, from_status: str, to_status: str
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_request(session, org_id, user_id)
        ticket.status = from_status
        await session.flush()
        before_count = len(await _history_rows(session, org_id, ticket.id))

        updated = await change_status(
            session, org_id, ticket.id, to_status=to_status, actor_id=user_id
        )

        assert updated.status == to_status
        rows = await _history_rows(session, org_id, ticket.id)
        assert len(rows) == before_count + 1
        newest = rows[-1]
        assert newest.from_status == from_status
        assert newest.to_status == to_status


# ---------------------------------------------------------------------------
# change_status -- the approval-only edges are legal but reserved (A1 fix)
# ---------------------------------------------------------------------------

_APPROVAL_ONLY_EDGES = [
    ("pending_approval", "in_progress"),
    ("pending_approval", "rejected"),
]


@pytest.mark.parametrize("from_status,to_status", _APPROVAL_ONLY_EDGES)
async def test_pending_approval_edges_are_legal_in_lifecycle_but_refused_via_generic_change_status(
    api_env, from_status: str, to_status: str
) -> None:
    """Whole-branch security review, finding A1 (invariant #6, spec §4).

    `lifecycle.py` is unmodified and still says both these edges are legal
    `request` transitions -- confirmed directly below, and pinned again
    independently by `test_service_desk_lifecycle.py`'s
    `test_request_walks_approval_to_closed`. What changed is who may drive
    them THROUGH `change_status`: only `approvals.decide_approval`, via its
    private `_approval_decision=True` keyword. A plain `change_status` call
    with no such keyword -- exactly what the generic `POST
    /tickets/{id}/status` route makes, gated on `ticket.write` alone -- must
    now be refused on these two edges, or a technician holding only
    `ticket.write` could self-approve/self-reject their own
    `pending_approval` request, skipping `ticket.approve`, the self-approval
    prohibition, `approver_role_id` membership, the mandatory rejection
    reason, and the `ticket.approved`/`ticket.rejected` audit events
    entirely."""
    assert can_transition("request", from_status, to_status)

    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_request(session, org_id, user_id, requires_approval=True)
        ticket.status = from_status
        await session.flush()
        ticket_id = ticket.id
        before_count = len(await _history_rows(session, org_id, ticket_id))

        with pytest.raises(TicketValidationError):
            await change_status(session, org_id, ticket_id, to_status=to_status, actor_id=user_id)

        # Refused exactly like any other illegal transition: nothing mutated.
        session.expire(ticket)
        reloaded = await get_ticket(session, org_id, ticket_id)
        assert reloaded.status == from_status
        rows = await _history_rows(session, org_id, ticket_id)
        assert len(rows) == before_count


async def test_decide_approval_private_keyword_still_drives_the_reserved_edges(api_env) -> None:
    """The flip side of the test above: `_approval_decision=True` -- the
    keyword only `approvals.decide_approval` passes -- still lets
    `change_status` complete both reserved edges. This is `change_status`'s
    own contract, exercised directly rather than through `decide_approval`,
    so a future reader can see both halves (refused without the keyword,
    allowed with it) in one place."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_request(session, org_id, user_id, requires_approval=True)
        assert ticket.status == "pending_approval"

        updated = await change_status(
            session,
            org_id,
            ticket.id,
            to_status="in_progress",
            actor_id=user_id,
            _approval_decision=True,
        )
    assert updated.status == "in_progress"


# ---------------------------------------------------------------------------
# change_status -- illegal transitions reject and leave the ticket untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticket_type,from_status,to_status",
    [
        ("incident", "new", "closed"),
        ("incident", "new", "resolved"),
        ("incident", "cancelled", "open"),
        ("incident", "new", "pending_approval"),
        ("request", "pending_approval", "fulfilled"),
        ("request", "rejected", "in_progress"),
        ("request", "new", "resolved"),
    ],
)
async def test_illegal_transition_raises_and_leaves_ticket_unchanged(
    api_env, ticket_type: str, from_status: str, to_status: str
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = (
            await _create_incident(session, org_id, user_id)
            if ticket_type == "incident"
            else await _create_request(session, org_id, user_id)
        )
        ticket.status = from_status
        await session.flush()
        # Capture the id BEFORE expiring -- an expired instance's own `.id`
        # attribute requires an awaited reload too, and a synchronous
        # attribute access outside an awaited call raises `MissingGreenlet`
        # (the `test_budget_expenses.py` H6 pattern).
        ticket_id = ticket.id

        with pytest.raises(TicketValidationError):
            await change_status(session, org_id, ticket_id, to_status=to_status, actor_id=user_id)

        # session.expire() forces the re-select below to hit the DB instead
        # of returning the SAME identity-mapped object -- a plain re-select
        # without it would (wrongly) prove nothing (brief's explicit
        # warning; Budget lost a review round to exactly this).
        session.expire(ticket)
        reloaded = await get_ticket(session, org_id, ticket_id)
        assert reloaded.status == from_status
        # task 8's S4 fix: status and history-row count alone don't prove
        # "completely unchanged" -- reordering change_status so the
        # stamping block ran BEFORE assert_transition would still pass both
        # of those (the stamp persists via autoflush, the raise still
        # happens). Assert the stamps too.
        assert reloaded.resolved_at is None
        assert reloaded.closed_at is None

        rows = await _history_rows(session, org_id, ticket_id)
        assert len(rows) == 1  # only the creation row -- no row from the rejected call


async def test_illegal_transition_unchanged_on_a_fresh_session(
    api_env, admin_engine: AsyncEngine
) -> None:
    """The brief's other sanctioned option besides `session.expire()`: a
    second, independent session, so there is no identity map to be fooled by
    at all."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket_id = ticket.id

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(TicketValidationError):
            await change_status(session, org_id, ticket_id, to_status="closed", actor_id=user_id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as fresh:
        reloaded = (
            await fresh.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()
    assert reloaded.status == "new"


# ---------------------------------------------------------------------------
# change_status -- resolved_at / closed_at stamping and reopen-clearing
# ---------------------------------------------------------------------------


async def test_moving_to_resolved_stamps_resolved_at(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()
        assert ticket.resolved_at is None

        updated = await change_status(
            session, org_id, ticket.id, to_status="resolved", actor_id=user_id
        )
    assert updated.resolved_at is not None
    assert updated.closed_at is None


async def test_moving_to_fulfilled_stamps_resolved_at(api_env) -> None:
    """`fulfilled` is the request lifecycle's counterpart to incident's
    `resolved` -- spec §3.3 pairs the two explicitly ("the whole reason
    `closed` is distinct from `resolved`/`fulfilled`"), and both columns are
    generic on `ticket`, not per-type."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_request(session, org_id, user_id)
        ticket.status = "in_progress"
        await session.flush()

        updated = await change_status(
            session, org_id, ticket.id, to_status="fulfilled", actor_id=user_id
        )
    assert updated.resolved_at is not None


async def test_moving_to_closed_stamps_closed_at(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()
        # Go through the real `resolved` stamping path first, so
        # `resolved_at` is genuinely set before this test asserts it survives
        # the subsequent close -- forcing `status = "resolved"` directly
        # would leave `resolved_at` NULL and prove nothing.
        await change_status(session, org_id, ticket.id, to_status="resolved", actor_id=user_id)
        assert ticket.resolved_at is not None

        updated = await change_status(
            session, org_id, ticket.id, to_status="closed", actor_id=user_id
        )
    assert updated.closed_at is not None
    # Progressing onward from resolved to closed is not a reopen -- the
    # historical resolved_at stays put.
    assert updated.resolved_at is not None


async def test_reopen_from_resolved_clears_resolved_at(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()
        await change_status(session, org_id, ticket.id, to_status="resolved", actor_id=user_id)
        assert ticket.resolved_at is not None

        reopened = await change_status(
            session, org_id, ticket.id, to_status="open", actor_id=user_id
        )
    assert reopened.resolved_at is None


async def test_reopen_from_closed_clears_both_closed_at_and_resolved_at(api_env) -> None:
    """task 8's S2 fix: any reopen clears `resolved_at`, `closed -> open`
    included. An earlier revision cleared `resolved_at` only when leaving
    `resolved`/`fulfilled` directly and left it set here, which meant two
    incidents, both reopened, both now `open`, disagreed on whether
    `resolved_at IS NOT NULL` -- the exact column an SLA "average time to
    resolve" query reads. `closed_at` still clears unconditionally, as
    before."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()
        await change_status(session, org_id, ticket.id, to_status="resolved", actor_id=user_id)
        await change_status(session, org_id, ticket.id, to_status="closed", actor_id=user_id)
        assert ticket.closed_at is not None
        assert ticket.resolved_at is not None

        reopened = await change_status(
            session, org_id, ticket.id, to_status="open", actor_id=user_id
        )
    assert reopened.closed_at is None
    assert reopened.resolved_at is None


async def test_reopen_from_fulfilled_clears_resolved_at(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_request(session, org_id, user_id)
        ticket.status = "in_progress"
        await session.flush()
        await change_status(session, org_id, ticket.id, to_status="fulfilled", actor_id=user_id)
        assert ticket.resolved_at is not None

        reopened = await change_status(
            session, org_id, ticket.id, to_status="in_progress", actor_id=user_id
        )
    assert reopened.resolved_at is None


async def test_reopen_from_closed_request_clears_both_closed_at_and_resolved_at(api_env) -> None:
    """Request-lifecycle mirror of
    `test_reopen_from_closed_clears_both_closed_at_and_resolved_at` (task 8's
    S2 fix)."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_request(session, org_id, user_id)
        ticket.status = "in_progress"
        await session.flush()
        await change_status(session, org_id, ticket.id, to_status="fulfilled", actor_id=user_id)
        await change_status(session, org_id, ticket.id, to_status="closed", actor_id=user_id)

        reopened = await change_status(
            session, org_id, ticket.id, to_status="in_progress", actor_id=user_id
        )
    assert reopened.closed_at is None
    assert reopened.resolved_at is None


async def test_cancelling_a_resolved_ticket_does_not_clear_resolved_at(api_env) -> None:
    """Cancelling is a finalization, not a reopen -- `resolved_at` is a
    historical fact and must not be erased just because the ticket was later
    cancelled outright."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()
        await change_status(session, org_id, ticket.id, to_status="resolved", actor_id=user_id)

        cancelled = await change_status(
            session, org_id, ticket.id, to_status="cancelled", actor_id=user_id
        )
    assert cancelled.resolved_at is not None


# ---------------------------------------------------------------------------
# change_status -- actor_type/actor_id (Phase 15 system path, spec §3.3)
# ---------------------------------------------------------------------------


async def test_system_actor_with_null_actor_id_is_recorded(api_env) -> None:
    """task 8's S5 fix: asserts the `ticket.status_changed` AUDIT EVENT's
    actor pairing too, not just the `ticket_status_history` row. `AuditEvent`
    has no actor-pairing constraint of its own, so hardcoding
    `actor_type="user"` in the `audit_service.record` call would still pass
    a check limited to the history row -- the audit log would then attribute
    a system transition to a user with a NULL actor_id, defeating the "never
    reads as a technician's action" property (spec §3.3, §7.2) exactly where
    it matters."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()

        await change_status(
            session,
            org_id,
            ticket.id,
            to_status="resolved",
            actor_id=None,
            actor_type="system",
        )
        rows = await _history_rows(session, org_id, ticket.id)
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "ticket.status_changed",
                    )
                )
            )
            .scalars()
            .all()
        )
    newest = rows[-1]
    assert newest.actor_type == "system"
    assert newest.actor_id is None

    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "system"
    assert event.actor_id is None


async def test_system_actor_type_with_non_null_actor_id_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()

        with pytest.raises(TicketValidationError):
            await change_status(
                session,
                org_id,
                ticket.id,
                to_status="resolved",
                actor_id=user_id,
                actor_type="system",
            )


async def test_user_actor_type_with_null_actor_id_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()

        with pytest.raises(TicketValidationError):
            await change_status(
                session, org_id, ticket.id, to_status="resolved", actor_id=None, actor_type="user"
            )


async def test_unknown_actor_type_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()

        with pytest.raises(TicketValidationError):
            await change_status(
                session,
                org_id,
                ticket.id,
                to_status="resolved",
                actor_id=user_id,
                actor_type="agent",
            )


# ---------------------------------------------------------------------------
# change_status -- audit
# ---------------------------------------------------------------------------


async def test_change_status_audits_exactly_once(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()

        await change_status(session, org_id, ticket.id, to_status="resolved", actor_id=user_id)
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "ticket.status_changed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    event = events[0]
    assert event.target_type == "ticket"
    assert event.target_id == str(ticket.id)
    assert event.actor_id == user_id
    assert event.meta == {"from_status": "open", "to_status": "resolved"}


# ---------------------------------------------------------------------------
# assign_ticket -- set, clear, cross-org resolution
# ---------------------------------------------------------------------------


async def test_assign_ticket_sets_assignee(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_sub = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        other_user_id = (
            await session.execute(select(User.id).where(User.external_subject == other_sub))
        ).scalar_one()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        assigned = await assign_ticket(
            session, org_id, user_id, ticket.id, {"assigned_to": other_user_id}
        )
    assert assigned.assigned_to == other_user_id


async def test_assign_ticket_clears_assignee(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_sub = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        other_user_id = (
            await session.execute(select(User.id).where(User.external_subject == other_sub))
        ).scalar_one()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        await assign_ticket(session, org_id, user_id, ticket.id, {"assigned_to": other_user_id})

        cleared = await assign_ticket(
            session, org_id, user_id, ticket.id, {"assigned_to": None}
        )
    assert cleared.assigned_to is None


async def test_assign_ticket_leaves_queue_alone_when_queue_id_omitted(
    api_env,
) -> None:
    """`queue_id` omitted from `fields` unambiguously means "do not move the
    queue" -- presence, not value, decides (task 8's S1 fix; renamed from
    `..._moves_queue_without_touching_assignee_when_queue_id_omitted`, task
    8's S9 fix, since this test asserts the queue is NOT moved, the opposite
    of what the old name said)."""
    org_id, user_id = await _org_with_user(api_env)
    other_sub = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        other_user_id = (
            await session.execute(select(User.id).where(User.external_subject == other_sub))
        ).scalar_one()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        original_queue_id = ticket.queue_id

        updated = await assign_ticket(
            session, org_id, user_id, ticket.id, {"assigned_to": other_user_id}
        )
    assert updated.assigned_to == other_user_id
    assert updated.queue_id == original_queue_id


async def test_assign_ticket_rejects_explicit_null_queue_id(api_env) -> None:
    """B4 (whole-branch review, round B): `assign_ticket`'s "queue_id cannot
    be null" guard was only ever exercised by the OMITTED case above
    (`test_assign_ticket_leaves_queue_alone_when_queue_id_omitted`) -- an
    explicit `{"queue_id": None}` was never tried. `update_ticket`'s
    equivalent guard on `NON_NULLABLE_TICKET_FIELDS` IS tested both ways
    (`test_update_explicit_none_on_non_nullable_field_rejected_before_setattr`
    in `test_service_desk_tickets.py`); this was the skipped half of that
    pair for assignment's own `queue_id`. Without the guard, the call would
    reach `refdata.resolve_queue(session, org_id, None)`, which 404s
    (`TicketNotFoundError`) rather than raising the typed 400 `assign_ticket`
    promises for this shape."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket_id = ticket.id
        original_queue_id = ticket.queue_id

        with pytest.raises(TicketValidationError) as exc:
            await assign_ticket(session, org_id, user_id, ticket_id, {"queue_id": None})
        assert "queue_id cannot be null" in str(exc.value).lower()

        # session.expire() forces the re-select below to hit the DB instead
        # of returning the SAME identity-mapped object (the
        # test_service_desk_transitions.py convention, see
        # test_illegal_transition_raises_and_leaves_ticket_unchanged above).
        session.expire(ticket)
        reloaded = await get_ticket(session, org_id, ticket_id)
    assert reloaded.queue_id == original_queue_id


async def test_assign_ticket_moves_queue_when_supplied(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        new_queue = await _queue(session, org_id, user_id, key="infrastructure")

        updated = await assign_ticket(
            session,
            org_id,
            user_id,
            ticket.id,
            {"queue_id": new_queue.id},
        )
    assert updated.queue_id == new_queue.id


async def test_assign_ticket_moves_queue_while_a_real_assignee_is_untouched(api_env) -> None:
    """task 8's S10 fix: `test_assign_ticket_moves_queue_when_supplied` only
    ever exercised this with `assigned_to=None` already in place, so it could
    not have caught S1 (a caller moving the queue via the documented
    presence contract silently unassigning whoever held the ticket). This
    puts a REAL, non-None incumbent assignee on the ticket first, moves only
    the queue, and asserts the assignee survives untouched."""
    org_id, user_id = await _org_with_user(api_env)
    other_sub = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        other_user_id = (
            await session.execute(select(User.id).where(User.external_subject == other_sub))
        ).scalar_one()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        new_queue = await _queue(session, org_id, user_id, key="infrastructure")
        await assign_ticket(session, org_id, user_id, ticket.id, {"assigned_to": other_user_id})

        updated = await assign_ticket(
            session, org_id, user_id, ticket.id, {"queue_id": new_queue.id}
        )
    assert updated.queue_id == new_queue.id
    assert updated.assigned_to == other_user_id


async def test_assign_ticket_cross_org_assignee_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Runs on a raw BYPASSRLS admin session, not `org_session` -- inside
    `org_session`, RLS alone already hides org B's row, so this would pass
    even with the explicit `User.org_id == org_id` predicate deleted from
    `tickets.resolve_user`. Spec §8.1 requires that predicate as defence in
    depth IN ADDITION to RLS."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket = await _create_incident(session, org_a, user_a)

        with pytest.raises(TicketNotFoundError):
            await assign_ticket(session, org_a, user_a, ticket.id, {"assigned_to": user_b})


async def test_assign_ticket_cross_org_queue_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Same reasoning as the assignee case above, for `resolve_queue`."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket = await _create_incident(session, org_a, user_a)
        queue_b = await _queue(session, org_b, user_b, key="other-org-queue")

        with pytest.raises(TicketNotFoundError):
            await assign_ticket(
                session,
                org_a,
                user_a,
                ticket.id,
                {"queue_id": queue_b.id},
            )


async def test_assign_ticket_rejected_assignee_leaves_ticket_unchanged(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, user_a = await _org_with_user(api_env)
    _org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket = await _create_incident(session, org_a, user_a)
        ticket_id = ticket.id

        with pytest.raises(TicketNotFoundError):
            await assign_ticket(session, org_a, user_a, ticket_id, {"assigned_to": user_b})

        session.expire(ticket)
        reloaded = await get_ticket(session, org_a, ticket_id)
        assert reloaded.assigned_to is None


# ---------------------------------------------------------------------------
# assign_ticket -- audit
# ---------------------------------------------------------------------------


async def test_assign_ticket_audits_exactly_once(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_sub = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        other_user_id = (
            await session.execute(select(User.id).where(User.external_subject == other_sub))
        ).scalar_one()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        await assign_ticket(session, org_id, user_id, ticket.id, {"assigned_to": other_user_id})
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "ticket.assigned",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    event = events[0]
    assert event.target_type == "ticket"
    assert event.target_id == str(ticket.id)
    assert event.actor_id == user_id
    assert event.meta["assigned_to"] == str(other_user_id)


# ---------------------------------------------------------------------------
# assign_ticket -- system actor (SLA task 8 review, F1; mirrors
# `change_status`/`tickets.update_ticket`'s identical contract)
# ---------------------------------------------------------------------------


async def test_assign_ticket_system_actor_moves_queue_and_is_audited_with_no_actor_id(
    api_env,
) -> None:
    """SLA §6's queue-move escalation is system-originated and has no human
    actor. `assign_ticket` is the queue-move service (`queue_id` is not in
    `tickets.TICKET_EDITABLE_FIELDS`), so it carries the same keyword-only
    `actor_type` contract Task 8 gave `update_ticket`: `actor_type="system"`
    with `actor_id=None` must reach `audit_service.record` unchanged, never
    coerced back to a human who did nothing (invariant #4)."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        escalation_queue = await _queue(session, org_id, user_id, key="escalation")

        moved = await assign_ticket(
            session,
            org_id,
            None,
            ticket.id,
            {"queue_id": escalation_queue.id},
            actor_type="system",
        )
        assert moved.queue_id == escalation_queue.id
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "ticket.assigned",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "system"
    assert event.actor_id is None
    assert event.meta["queue_id"] == str(escalation_queue.id)


async def test_assign_ticket_rejects_unknown_actor_type(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        with pytest.raises(TicketValidationError):
            await assign_ticket(
                session,
                org_id,
                user_id,
                ticket.id,
                {"assigned_to": user_id},
                actor_type="agent",  # type: ignore[arg-type]
            )


async def test_assign_ticket_rejects_system_actor_with_actor_id(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        with pytest.raises(TicketValidationError):
            await assign_ticket(
                session,
                org_id,
                user_id,
                ticket.id,
                {"assigned_to": user_id},
                actor_type="system",
            )


async def test_assign_ticket_rejects_user_actor_type_without_actor_id(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        with pytest.raises(TicketValidationError):
            await assign_ticket(
                session,
                org_id,
                None,
                ticket.id,
                {"assigned_to": user_id},
                actor_type="user",
            )


# ---------------------------------------------------------------------------
# assign_ticket -- not gated on terminal status (deliberate, see module
# docstring for reasoning)
# ---------------------------------------------------------------------------


async def test_assign_ticket_permits_reassignment_of_a_closed_ticket(api_env) -> None:
    """Unlike `tickets.update_ticket`'s field edits, `assign_ticket`
    deliberately does NOT gate on `TERMINAL_STATUSES` -- reassigning
    ownership of a closed ticket is administrative bookkeeping, not a
    content edit. See `transitions.py`'s module docstring."""
    org_id, user_id = await _org_with_user(api_env)
    other_sub = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        other_user_id = (
            await session.execute(select(User.id).where(User.external_subject == other_sub))
        ).scalar_one()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "closed"
        await session.flush()

        assigned = await assign_ticket(
            session, org_id, user_id, ticket.id, {"assigned_to": other_user_id}
        )
    assert assigned.assigned_to == other_user_id


# ---------------------------------------------------------------------------
# get_ticket sanity (imported for the fresh-session unchanged-check pattern)
# ---------------------------------------------------------------------------


async def test_get_ticket_used_for_unchanged_check_after_rejection(api_env) -> None:
    """Belt-and-braces: `get_ticket` itself, used as the "second session"
    unchanged-check pattern the brief calls out, returns the untouched row."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket_id = ticket.id

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(TicketValidationError):
            await change_status(session, org_id, ticket_id, to_status="closed", actor_id=user_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as fresh:
        reloaded = await get_ticket(fresh, org_id, ticket_id)
    assert reloaded.status == "new"


async def test_change_status_ticket_not_found_for_nonexistent_id(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(TicketNotFoundError):
            await change_status(
                session, org_id, uuid7(), to_status="open", actor_id=user_id
            )


async def test_assign_rejects_a_finally_terminal_ticket(api_env) -> None:
    """`cancelled` and `rejected` are dead — nothing leaves them, so handing
    one to a technician is churn in the audit log for work that can never
    move. `closed` is deliberately NOT rejected here: it is reopenable, and
    reassigning ownership of a closed ticket is bookkeeping, not a content
    edit (see test_assign_ticket_permits_reassignment_of_a_closed_ticket).

    That asymmetry is the one place in this module where TERMINAL vs FINAL
    changes behavior rather than merely naming it: update_ticket gates field
    edits on TERMINAL (a closed ticket accepts no edits), assignment gates on
    FINAL.
    """
    org_id, user_id = await _org_with_user(api_env)
    # `cancelled` is reachable by an incident; `rejected` only by a request --
    # ck_ticket_status_matches_type refuses the cross pairing, so each final
    # status is exercised on a ticket type that can legally hold it.
    cases = [("cancelled", _create_incident), ("rejected", _create_request)]
    for final, make in cases:
        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
            ticket = await make(session, org_id, user_id)
            ticket.status = final
            await session.flush()

            with pytest.raises(TicketValidationError):
                await assign_ticket(
                    session, org_id, user_id, ticket.id, {"assigned_to": user_id}
                )
