"""Ticket core publishes domain events (docs/modules/notification/spec.md
§3, §6.4; docs/modules/service-desk/spec.md §1).

Task 12: the ticket core is already-shipped, already-reviewed code. This
file covers ONLY the additive change -- a `publish(...)` call inside the
transaction each service already had -- not a re-test of the underlying
service behavior, which `test_service_desk_transitions.py`,
`test_service_desk_threads.py`, and `test_service_desk_approvals.py` already
own.

Covers: `assign_ticket` and `change_status` (`transitions.py`) each publish
exactly one event, with a payload shaped exactly like
`notification.routing.resolve_recipients`'s fixed contract (every id a
`str(uuid)` or `None`, `requester_id` present); a public comment and an
internal note (`threads.add_comment`) publish DIFFERENT event types, and the
internal note's payload never contains the note's text, searched for as the
literal string written, not merely absent from an expected-keys diff; a
request landing on `pending_approval` (`tickets.create_ticket`, and --
per `lifecycle.py`'s `new -> pending_approval` request edge --
`transitions.change_status`) publishes `ticket.approval_requested`, the
NEED-TO-ACT notification, exactly once, with the negative control that a
request whose category does NOT require approval publishes no such event; a
request approval decision (`approvals.decide_approval`) publishes its OWN,
DIFFERENT event type, `ticket.approval_decided`, with the same contract
payload, never the now-repurposed `ticket.approval_requested`; a rolled
back state change -- the whole transaction, not `publish()` in isolation --
leaves no `domain_events` row at all (spec §12.8, end to end); and
`app.modules.service_desk` imports nothing from `app.modules.notification`
(spec §1 / §12.12 in reverse).
"""

import pathlib
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import DomainEvent, User
from app.modules.service_desk.approvals import decide_approval
from app.modules.service_desk.refdata import create_category, create_queue
from app.modules.service_desk.schema import TicketCreate
from app.modules.service_desk.threads import add_comment
from app.modules.service_desk.tickets import create_ticket
from app.modules.service_desk.transitions import assign_ticket, change_status

# ---------------------------------------------------------------------------
# Fixture helpers (mirrors test_service_desk_transitions.py's shapes -- not
# imported across test modules, per convention)
# ---------------------------------------------------------------------------


async def _org_with_user(api_env, slug: str = "sd-events") -> tuple[UUID, UUID]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _second_user(api_env, org_id: UUID, *role_names: str) -> UUID:
    sub = await api_env.create_user(org_id, *role_names)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _create_incident(session, org_id, actor_id, *, requester_id=None):
    queue = await create_queue(session, org_id, actor_id, key="helpdesk", name="Helpdesk")
    category = await create_category(
        session, org_id, actor_id, key="hardware", name="Hardware", type="incident"
    )
    return await create_ticket(
        session,
        org_id,
        actor_id,
        TicketCreate(
            type="incident",
            category_id=category.id,
            queue_id=queue.id,
            subject="Laptop won't boot",
            requester_id=requester_id or actor_id,
        ),
    )


async def _create_request(session, org_id, requester_id, actor_id):
    queue = await create_queue(session, org_id, actor_id, key="requests", name="Requests")
    category = await create_category(
        session,
        org_id,
        actor_id,
        key="access_request",
        name="Access Request",
        type="request",
        requires_approval=True,
    )
    return await create_ticket(
        session,
        org_id,
        actor_id,
        TicketCreate(
            type="request",
            category_id=category.id,
            queue_id=queue.id,
            subject="Need VPN access",
            requester_id=requester_id,
        ),
    )


async def _events(session, org_id) -> list[DomainEvent]:
    return list(
        (
            await session.execute(select(DomainEvent).where(DomainEvent.org_id == org_id))
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# assign_ticket -- ticket.assigned
# ---------------------------------------------------------------------------


async def test_assigning_publishes_ticket_assigned(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    assignee_id = await _second_user(api_env, org_id, "technician")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        await assign_ticket(
            session, org_id, user_id, ticket.id, {"assigned_to": assignee_id}
        )
        events = await _events(session, org_id)

    assert [e.event_type for e in events] == ["ticket.assigned"]
    payload = events[0].payload
    assert payload["queue_id"] == str(ticket.queue_id)
    assert payload["assignee_id"] == str(assignee_id)
    assert payload["requester_id"] == str(ticket.requester_id)
    assert events[0].subject_type == "ticket"
    assert events[0].subject_id == ticket.id
    assert events[0].actor_user_id == user_id


async def test_assigning_publishes_string_ids_not_bare_uuids(api_env) -> None:
    """`notification.routing.resolve_recipients` accepts `str(uuid)` (the
    real `domain_events.payload` JSONB shape) -- a bare `UUID` object is not
    JSON-serializable and would never have survived a real write."""
    org_id, user_id = await _org_with_user(api_env)
    assignee_id = await _second_user(api_env, org_id, "technician")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        await assign_ticket(
            session, org_id, user_id, ticket.id, {"assigned_to": assignee_id}
        )
        events = await _events(session, org_id)

    payload = events[0].payload
    for key in ("queue_id", "assignee_id", "requester_id"):
        assert isinstance(payload[key], str), f"{key} was not a string: {payload[key]!r}"


# ---------------------------------------------------------------------------
# change_status -- ticket.status_changed
# ---------------------------------------------------------------------------


async def test_status_change_publishes_ticket_status_changed(api_env) -> None:
    """Uses a NON-resolving transition (`open -> pending`) deliberately, so
    this test's exactly-one-event assertion stays scoped to
    `ticket.status_changed` alone -- `open -> resolved` also publishes
    `ticket.resolved` (see the dedicated `ticket.resolved` tests below, C2
    fix) and would otherwise make this assertion about a different thing."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()
        await change_status(session, org_id, ticket.id, to_status="pending", actor_id=user_id)
        events = await _events(session, org_id)

    assert [e.event_type for e in events] == ["ticket.status_changed"]
    payload = events[0].payload
    assert payload["from_status"] == "open"
    assert payload["to_status"] == "pending"
    assert payload["queue_id"] == str(ticket.queue_id)
    assert payload["requester_id"] == str(ticket.requester_id)
    assert payload["assignee_id"] is None


# ---------------------------------------------------------------------------
# add_comment -- ticket.comment_added / ticket.internal_note_added
# ---------------------------------------------------------------------------


async def test_a_public_comment_and_an_internal_note_use_different_event_types(
    api_env,
) -> None:
    """Spec §6.4 -- the write-time leak guard depends on these being distinct."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        await add_comment(
            session, org_id, ticket.id, body="visible", author_id=user_id, visibility="public"
        )
        await add_comment(
            session, org_id, ticket.id, body="staff only", author_id=user_id, visibility="internal"
        )
        events = await _events(session, org_id)

    types = {e.event_type for e in events}
    assert types == {"ticket.comment_added", "ticket.internal_note_added"}


async def test_internal_note_event_omits_the_body(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    secret = "staff only -- do not disclose"
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        await add_comment(
            session, org_id, ticket.id, body=secret, author_id=user_id, visibility="internal"
        )
        events = await _events(session, org_id)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "ticket.internal_note_added"
    # Search the payload for the ACTUAL body string written, not merely
    # confirm a "body" key is absent -- a bug that renamed the key or
    # nested the text elsewhere would still leak it past a keys-only check.
    assert secret not in str(event.payload)
    assert "body" not in event.payload


async def test_public_comment_event_carries_the_body(api_env) -> None:
    """The negative space of the test above: a PUBLIC comment's body is
    allowed to travel, so the omission above is a deliberate choice about
    internal notes specifically, not a blanket "never send body" rule that
    would make public-comment notifications useless."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        await add_comment(
            session,
            org_id,
            ticket.id,
            body="the fix is on its way",
            author_id=user_id,
            visibility="public",
        )
        events = await _events(session, org_id)

    assert events[0].payload["body"] == "the fix is on its way"


async def test_internal_note_event_carries_requester_id_for_the_routing_guard(
    api_env,
) -> None:
    """`notification.routing.resolve_recipients` reads `requester_id` on
    EVERY event type, `ticket.internal_note_added` included, to drop the
    requester from the resolved set regardless of which `recipient_kind`
    surfaced them. If this event omitted `requester_id`, that guard could
    never run for internal notes -- the one event type it exists for."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        await add_comment(
            session, org_id, ticket.id, body="staff only", author_id=user_id, visibility="internal"
        )
        events = await _events(session, org_id)

    assert events[0].payload["requester_id"] == str(ticket.requester_id)


# ---------------------------------------------------------------------------
# change_status -- ticket.resolved (whole-branch review finding C2).
# `ticket.resolved` was registered in `app.core.events.EVENT_TYPES` and
# rule-selectable (`POST /rules` returned 201 for it) since Task 7, but
# nothing published it anywhere -- spec §0.4 calls telling a requester their
# ticket was resolved "most of the product value", and it silently never
# fired.
# ---------------------------------------------------------------------------


async def test_incident_reaching_resolved_publishes_ticket_resolved(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()
        await change_status(session, org_id, ticket.id, to_status="resolved", actor_id=user_id)
        events = await _events(session, org_id)

    types = [e.event_type for e in events]
    # `ticket.status_changed` fires on every change_status call; exactly one
    # `ticket.resolved` is the additional, previously-missing publish.
    assert types.count("ticket.resolved") == 1
    assert "ticket.status_changed" in types

    event = next(e for e in events if e.event_type == "ticket.resolved")
    assert event.subject_type == "ticket"
    assert event.subject_id == ticket.id
    payload = event.payload
    assert payload["queue_id"] == str(ticket.queue_id)
    assert payload["requester_id"] == str(ticket.requester_id)
    assert payload["assignee_id"] is None
    for key in ("queue_id", "requester_id"):
        assert isinstance(payload[key], str), f"{key} was not a string: {payload[key]!r}"


async def test_request_reaching_fulfilled_publishes_ticket_resolved(api_env) -> None:
    """`fulfilled` is the request lifecycle's counterpart to incident's
    `resolved` (`transitions.py`'s own docstring, spec §3.3) -- there is a
    SINGLE `ticket.resolved` event type registered, not a second
    `ticket.fulfilled` type, so a request reaching `fulfilled` publishes the
    SAME event a resolved incident does: a rule on `ticket.resolved` routed
    to the requester notifies them either way, which is spec §0.4's whole
    point."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        # requires_approval=False so creation lands directly on in_progress,
        # from which `fulfilled` is reachable through the generic path.
        queue = await create_queue(session, org_id, user_id, key="requests-res", name="Requests")
        category = await create_category(
            session,
            org_id,
            user_id,
            key="no_approval_res",
            name="No Approval",
            type="request",
            requires_approval=False,
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="request",
                category_id=category.id,
                queue_id=queue.id,
                subject="Need a webcam",
                requester_id=user_id,
            ),
        )
        assert ticket.status == "in_progress"
        await change_status(session, org_id, ticket.id, to_status="fulfilled", actor_id=user_id)
        events = await _events(session, org_id)

    types = [e.event_type for e in events]
    assert types.count("ticket.resolved") == 1
    event = next(e for e in events if e.event_type == "ticket.resolved")
    assert event.payload["requester_id"] == str(user_id)


async def test_a_non_resolving_status_change_publishes_no_ticket_resolved(api_env) -> None:
    """NEGATIVE CONTROL: without it, a version that fires `ticket.resolved`
    on every `change_status` call regardless of `to_status` -- or one that
    only checked "did resolved_at get stamped" rather than the specific
    to_status -- would still pass the positive tests above."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        ticket.status = "open"
        await session.flush()
        # open -> pending is a legal incident transition (lifecycle.py) that
        # is NOT a resolution.
        await change_status(session, org_id, ticket.id, to_status="pending", actor_id=user_id)
        events = await _events(session, org_id)

    types = [e.event_type for e in events]
    assert "ticket.resolved" not in types
    assert "ticket.status_changed" in types


# ---------------------------------------------------------------------------
# create_ticket -- ticket.approval_requested (ENTERING pending_approval --
# the need-to-act notification nobody got before this fix)
# ---------------------------------------------------------------------------


async def test_creating_a_request_needing_approval_publishes_approval_requested(
    api_env,
) -> None:
    """Spec §4: a request whose category requires approval lands on
    `pending_approval` at CREATION -- `tickets.create_ticket` is one of the
    two entry points to that state (the other is `transitions.change_status`'s
    `new -> pending_approval` request edge, covered separately below). This
    is the notification that did not exist before this fix: the approver was
    never told a decision was waiting on them."""
    org_id, requester_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_request(session, org_id, requester_id, requester_id)
        events = await _events(session, org_id)

    assert ticket.status == "pending_approval"
    types = [e.event_type for e in events]
    # Exactly once -- `create_ticket` runs its approval-row insert and this
    # publish call exactly once per invocation, never a loop or a retry path.
    assert types.count("ticket.approval_requested") == 1

    event = next(e for e in events if e.event_type == "ticket.approval_requested")
    assert event.subject_type == "ticket"
    assert event.subject_id == ticket.id
    payload = event.payload
    assert payload["queue_id"] == str(ticket.queue_id)
    assert payload["requester_id"] == str(requester_id)
    assert payload["assignee_id"] is None


async def test_creating_a_request_not_needing_approval_publishes_no_approval_requested(
    api_env,
) -> None:
    """The negative control: without it, a version that ALWAYS publishes
    `ticket.approval_requested` on every request creation -- rather than only
    when the category actually requires approval and the ticket actually
    lands on `pending_approval` -- would pass the positive test above too."""
    org_id, requester_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await create_queue(
            session, org_id, requester_id, key="requests-noap", name="Requests"
        )
        category = await create_category(
            session,
            org_id,
            requester_id,
            key="no_approval_needed",
            name="No Approval Needed",
            type="request",
            requires_approval=False,
        )
        ticket = await create_ticket(
            session,
            org_id,
            requester_id,
            TicketCreate(
                type="request",
                category_id=category.id,
                queue_id=queue.id,
                subject="Need a monitor",
                requester_id=requester_id,
            ),
        )
        events = await _events(session, org_id)

    assert ticket.status == "in_progress"
    assert "ticket.approval_requested" not in [e.event_type for e in events]


async def test_approval_requested_event_carries_the_full_routing_payload(api_env) -> None:
    """`notification.routing.resolve_recipients` reads `queue_id`,
    `assignee_id`, `requester_id` unconditionally (routing.py's fixed
    contract, `resolve_recipients`'s docstring) -- all three populated, not
    merely present as keys, when the ticket actually carries them."""
    org_id, requester_id = await _org_with_user(api_env)
    assignee_id = await _second_user(api_env, org_id, "technician")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await create_queue(
            session, org_id, requester_id, key="requests-full", name="Requests"
        )
        category = await create_category(
            session,
            org_id,
            requester_id,
            key="access_request_full",
            name="Access Request",
            type="request",
            requires_approval=True,
        )
        ticket = await create_ticket(
            session,
            org_id,
            requester_id,
            TicketCreate(
                type="request",
                category_id=category.id,
                queue_id=queue.id,
                subject="Need admin access",
                requester_id=requester_id,
                assigned_to=assignee_id,
            ),
        )
        events = await _events(session, org_id)

    event = next(e for e in events if e.event_type == "ticket.approval_requested")
    payload = event.payload
    assert payload["queue_id"] == str(ticket.queue_id)
    assert payload["assignee_id"] == str(assignee_id)
    assert payload["requester_id"] == str(requester_id)
    for key in ("queue_id", "assignee_id", "requester_id"):
        assert isinstance(payload[key], str), f"{key} was not a string: {payload[key]!r}"


# ---------------------------------------------------------------------------
# change_status -- ticket.approval_requested (the OTHER entry to
# pending_approval, per lifecycle.py's request `new -> pending_approval`
# edge)
# ---------------------------------------------------------------------------


async def test_change_status_entering_pending_approval_publishes_approval_requested(
    api_env,
) -> None:
    """`lifecycle.py` names `new -> pending_approval` a legal `request`
    transition, so `transitions.change_status` -- the sole status mutator
    after creation -- must publish the same event on that edge too, not just
    `create_ticket`'s own, far more common entry to `pending_approval`.

    NOT reachable through any real application flow today: `initial_status`
    (`lifecycle.py`) never returns `"new"` for `ticket_type="request"` --
    a freshly created request lands directly on `pending_approval` or
    `in_progress` -- and `change_status` is the only place `status` moves
    after creation, so no legitimate caller can ever hand a `request` ticket
    a `from_status` of `"new"` to drive this edge from. This test forces
    `ticket.status = "new"` directly on the ORM object first, exactly the
    same artifice `test_service_desk_transitions.py`'s
    `test_every_legal_request_transition_moves_status_and_writes_one_history_row`
    already uses to exercise this exact edge against `lifecycle.py`'s table --
    it is what makes the edge testable at all, not a claim that any user or
    route can reach it."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        # A category with requires_approval=False, so CREATION lands on
        # `in_progress` and publishes no `ticket.approval_requested` itself
        # -- this isolates the event under test to the `change_status` call
        # below, rather than double-counting creation's own (separately
        # tested above).
        queue = await create_queue(session, org_id, user_id, key="requests-cs", name="Requests")
        category = await create_category(
            session,
            org_id,
            user_id,
            key="no_approval_cs",
            name="No Approval CS",
            type="request",
            requires_approval=False,
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="request",
                category_id=category.id,
                queue_id=queue.id,
                subject="Need a keyboard",
                requester_id=user_id,
            ),
        )
        assert ticket.status == "in_progress"

        # Force the from-state directly, as `lifecycle.py` requires for this
        # edge (see docstring above) -- mirrors
        # `test_service_desk_transitions.py`'s own artifice for the identical
        # `("new", "pending_approval")` edge.
        ticket.status = "new"
        await session.flush()

        updated = await change_status(
            session, org_id, ticket.id, to_status="pending_approval", actor_id=user_id
        )
        events = await _events(session, org_id)

    assert updated.status == "pending_approval"
    types = [e.event_type for e in events]
    # `ticket.status_changed` (every change_status call) plus exactly one
    # `ticket.approval_requested` for this specific entry.
    assert types.count("ticket.approval_requested") == 1
    assert "ticket.status_changed" in types

    event = next(e for e in events if e.event_type == "ticket.approval_requested")
    payload = event.payload
    assert payload["queue_id"] == str(ticket.queue_id)
    assert payload["requester_id"] == str(user_id)


# ---------------------------------------------------------------------------
# decide_approval -- ticket.approval_decided (the DECISION -- a distinct
# event from ticket.approval_requested, the NEED-TO-ACT notification above)
# ---------------------------------------------------------------------------


async def test_approval_decision_publishes_ticket_approval_decided_not_requested(
    api_env,
) -> None:
    """`decide_approval` decides an approval that already exists -- it must
    publish `ticket.approval_decided`, never (again) `ticket.approval_requested`,
    which fired once already, at creation, for the need-to-act notification.
    Overloading one event type for both "you need to approve this" and "your
    request was decided" is the exact defect this task fixes."""
    org_id, requester_id = await _org_with_user(api_env)
    approver_id = await _second_user(api_env, org_id, "org-admin")
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_request(session, org_id, requester_id, requester_id)
        await decide_approval(session, org_id, approver_id, ticket.id, approve=True)
        events = await _events(session, org_id)

    types = [e.event_type for e in events]
    # Creation's own approval_requested fires once; decide_approval must add
    # approval_decided, never a second approval_requested.
    assert types.count("ticket.approval_requested") == 1
    assert types.count("ticket.approval_decided") == 1

    approval_event = next(e for e in events if e.event_type == "ticket.approval_decided")
    payload = approval_event.payload
    assert payload["decision"] == "approved"
    assert payload["queue_id"] == str(ticket.queue_id)
    assert payload["requester_id"] == str(requester_id)
    assert payload["assignee_id"] is None


async def test_approval_rejection_does_not_leak_the_rejection_reason(api_env) -> None:
    """Mirrors the audit metadata convention this same file already follows
    (`metadata={"approval_id": ...}`, never the free-text `reason`) -- the
    domain event is not a second, laxer place for that text to leak into."""
    org_id, requester_id = await _org_with_user(api_env)
    approver_id = await _second_user(api_env, org_id, "org-admin")
    reason = "does not meet the access policy for this role"
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_request(session, org_id, requester_id, requester_id)
        await decide_approval(
            session, org_id, approver_id, ticket.id, approve=False, reason=reason
        )
        events = await _events(session, org_id)

    approval_event = next(e for e in events if e.event_type == "ticket.approval_decided")
    assert reason not in str(approval_event.payload)
    assert approval_event.payload["decision"] == "rejected"


# ---------------------------------------------------------------------------
# Atomicity, end to end (spec §12.8) -- not the unit-level publish() version
# ---------------------------------------------------------------------------


async def test_a_rolled_back_assignment_publishes_nothing(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    assignee_id = await _second_user(api_env, org_id, "technician")

    with pytest.raises(RuntimeError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            ticket = await _create_incident(session, org_id, user_id)
            await assign_ticket(
                session, org_id, user_id, ticket.id, {"assigned_to": assignee_id}
            )
            # The assignment and its publish() have both already run against
            # this session -- forcing the surrounding transaction to abort
            # here is what proves the event does not survive independently
            # of the state change it describes.
            raise RuntimeError("simulated failure after the state change")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        got = (
            await session.execute(select(DomainEvent).where(DomainEvent.org_id == org_id))
        ).scalars().all()
    assert got == []


# ---------------------------------------------------------------------------
# Module boundary (spec §1 / §12.12, reversed direction)
# ---------------------------------------------------------------------------


def test_service_desk_does_not_import_notification() -> None:
    """Spec §1: the ticket core must not grow a private notification path.
    `app.modules.service_desk` may depend on the kernel extension point
    (`app.core.events`) and nothing from `app.modules.notification`."""
    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "modules" / "service_desk"
    assert root.is_dir(), root
    for path in root.rglob("*.py"):
        assert "modules.notification" not in path.read_text(), path
