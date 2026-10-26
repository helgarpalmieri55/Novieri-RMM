"""Ticket core: create, get, list, update (spec §2, §3, §5, §7.2, §8).

Covers: creation resolves `category_id`/`queue_id`/`requester_id`/
`assigned_to`/`device_id` org-scoped, raising `TicketNotFoundError` for a
cross-org id -- on ALL FIVE references, mutation-verified for `device_id`,
and on a raw BYPASSRLS admin session (never `org_session`, where RLS alone
would mask the cross-org row and prove nothing about the explicit `org_id`
predicate, per spec §8.1's defence-in-depth requirement); the queue defaults
from `ticket_category.default_queue_id` and an explicit `queue_id` overrides
it, with neither resolving raising `TicketValidationError`; `initial_status`
decides the starting status (never re-derived here); creation writes a
`ticket_status_history` row with `from_status=None`; the Phase 15
`origin="alert"` contract (null `requester_id`/`created_by`, forced even when
non-None values are supplied) and its inverse (every other origin requires
both); `origin="alert"` is rejected for a non-incident `type`, and a
`source_alert_id` is rejected for any origin other than `"alert"`;
`update_ticket`'s PATCH semantics (omitted vs. explicit-null, non-nullable
rejected before any `setattr`); `status` is never editable through
`update_ticket`; a terminal ticket rejects edits, distinguishing `closed`
(edits gated, but reopenable) from `cancelled`/`rejected` (finally terminal);
a PATCHed `category_id` re-enforces the same type/category match
`create_ticket` enforces, so the guard cannot be bypassed on one of the two
write paths; cross-org `category_id`/`device_id` on the update path, also
on a raw admin session; audit metadata carries field names only, never
subject/description bodies; and a load-bearing, mutation-verified cross-org
test on `list_tickets`.

As of task 8, `assigned_to` and `queue_id` are no longer in
`TICKET_EDITABLE_FIELDS` -- both moved to `transitions.assign_ticket` (spec
§5, §8.3: they need `ticket.assign` and audit as `ticket.assigned`, not
`ticket.write`/`ticket.updated`). Their PATCH-path coverage here (set/clear,
cross-org rejection) moved to `test_service_desk_transitions.py` accordingly;
`device_id` now stands in for the "clears a nullable field" PATCH-semantics
case.
"""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_extensions import uuid7

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Device, Ticket, TicketStatusHistory, User
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.refdata import create_category, create_queue
from app.modules.service_desk.schema import TicketCreate
from app.modules.service_desk.tickets import (
    NON_NULLABLE_TICKET_FIELDS,
    TICKET_EDITABLE_FIELDS,
    create_ticket,
    get_ticket,
    list_status_history,
    list_tickets,
    update_ticket,
)
from app.modules.service_desk.transitions import change_status


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Create a throwaway org plus an `org-admin` user in it; return
    `(org_id, user_id)`. Mirrors `test_service_desk_refdata.py`'s helper of
    the same name/shape -- not imported across test modules, per
    convention."""
    org_id = await api_env.create_org("sd-tickets")
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


# ---------------------------------------------------------------------------
# create_ticket -- queue/category resolution
# ---------------------------------------------------------------------------


async def test_create_incident_defaults_queue_from_category(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=category.id,
                subject="Laptop won't boot",
                requester_id=user_id,
                client_id=org_id,
            ),
        )
    assert ticket.queue_id == queue.id
    assert ticket.category_id == category.id
    assert ticket.status == "new"
    assert ticket.org_id == org_id


async def test_explicit_queue_id_overrides_category_default(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        default_queue = await _queue(session, org_id, user_id, key="helpdesk")
        override_queue = await _queue(session, org_id, user_id, key="infrastructure")
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=default_queue.id
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=category.id,
                queue_id=override_queue.id,
                subject="Network down",
                requester_id=user_id,
                client_id=org_id,
            ),
        )
    assert ticket.queue_id == override_queue.id


async def test_create_fails_when_neither_queue_resolves(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        category = await _category(session, org_id, user_id, type="incident")
        with pytest.raises(TicketValidationError):
            await create_ticket(
                session,
                org_id,
                user_id,
                TicketCreate(
                    type="incident",
                    category_id=category.id,
                    subject="No queue anywhere",
                    requester_id=user_id,
                    client_id=org_id,
                ),
            )


async def test_create_rejects_type_mismatch_with_category(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="request", default_queue_id=queue.id
        )
        with pytest.raises(TicketValidationError):
            await create_ticket(
                session,
                org_id,
                user_id,
                TicketCreate(
                    type="incident",
                    category_id=category.id,
                    subject="Mismatched type",
                    requester_id=user_id,
                    client_id=org_id,
                ),
            )


# ---------------------------------------------------------------------------
# create_ticket -- initial status (lifecycle.initial_status, never re-derived)
# ---------------------------------------------------------------------------


async def test_incident_starts_new(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident", category_id=category.id, subject="Broken", requester_id=user_id,
                client_id=org_id,
            ),
        )
    assert ticket.status == "new"


async def test_request_requiring_approval_starts_pending_approval(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session,
            org_id,
            user_id,
            key="access_request",
            type="request",
            requires_approval=True,
            default_queue_id=queue.id,
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="request",
                category_id=category.id,
                subject="Need VPN access",
                requester_id=user_id,
                client_id=org_id,
            ),
        )
    assert ticket.status == "pending_approval"


async def test_request_not_requiring_approval_starts_in_progress(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session,
            org_id,
            user_id,
            key="software_request",
            type="request",
            requires_approval=False,
            default_queue_id=queue.id,
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="request",
                category_id=category.id,
                subject="Install VS Code",
                requester_id=user_id,
                client_id=org_id,
            ),
        )
    assert ticket.status == "in_progress"


# ---------------------------------------------------------------------------
# create_ticket -- status history
# ---------------------------------------------------------------------------


async def test_create_writes_status_history_row_with_null_from_status(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident", category_id=category.id, subject="Broken", requester_id=user_id,
                client_id=org_id,
            ),
        )
        rows = (
            (
                await session.execute(
                    select(TicketStatusHistory).where(
                        TicketStatusHistory.org_id == org_id,
                        TicketStatusHistory.ticket_id == ticket.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].from_status is None
    assert rows[0].to_status == "new"
    assert rows[0].actor_type == "user"
    assert rows[0].actor_id == user_id


# ---------------------------------------------------------------------------
# create_ticket -- the Phase 15 contract (origin="alert")
# ---------------------------------------------------------------------------


async def test_alert_origin_succeeds_with_null_requester_and_created_by(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        ticket = await create_ticket(
            session,
            org_id,
            None,
            TicketCreate(
                type="incident", category_id=category.id, subject="Alert breach", client_id=org_id
            ),
            origin="alert",
            source_alert_id=uuid7(),
        )
    assert ticket.origin == "alert"
    assert ticket.requester_id is None
    assert ticket.created_by is None
    assert ticket.source_alert_id is not None


async def test_alert_origin_status_history_is_system_actor(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        ticket = await create_ticket(
            session,
            org_id,
            None,
            TicketCreate(
                type="incident", category_id=category.id, subject="Alert breach", client_id=org_id
            ),
            origin="alert",
        )
        history = (
            await session.execute(
                select(TicketStatusHistory).where(TicketStatusHistory.ticket_id == ticket.id)
            )
        ).scalar_one()
    assert history.actor_type == "system"
    assert history.actor_id is None


async def test_integration_origin_is_system_actor_but_keeps_human_attribution(api_env) -> None:
    """Whole-branch security review, finding A3.

    `origin="integration"` (used by `service_desk.adapters.push_change`) maps
    the CREATE action's attribution to `actor_type="system"`, same as
    `"alert"` -- but, unlike `"alert"`, it is NOT exempt from human
    attribution: `requester_id`/`created_by` still resolve to and store a
    real user, since the ticket genuinely has a human behind it (e.g. the
    change's author). Only the `ticket_status_history` row and the
    `ticket.created` audit event's `actor_type`/`actor_id` differ from the
    ordinary `"technician"` case."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=category.id,
                subject="Change triggered this",
                requester_id=user_id,
                client_id=org_id,
            ),
            origin="integration",
        )

        assert ticket.origin == "integration"
        assert ticket.requester_id == user_id
        assert ticket.created_by == user_id

        history = (
            await session.execute(
                select(TicketStatusHistory).where(TicketStatusHistory.ticket_id == ticket.id)
            )
        ).scalar_one()
        assert history.actor_type == "system"
        assert history.actor_id is None

        created_event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.created",
                    AuditEvent.target_id == str(ticket.id),
                )
            )
        ).scalar_one()
        assert created_event.actor_type == "system"
        assert created_event.actor_id is None


async def test_non_alert_origin_with_null_requester_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        with pytest.raises(TicketValidationError):
            await create_ticket(
                session,
                org_id,
                user_id,
                TicketCreate(
                    type="incident",
                    category_id=category.id,
                    subject="No requester",
                    requester_id=None,
                    client_id=org_id,
                ),
                origin="technician",
            )


async def test_non_alert_origin_with_null_actor_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        with pytest.raises(TicketValidationError):
            await create_ticket(
                session,
                org_id,
                None,
                TicketCreate(
                    type="incident",
                    category_id=category.id,
                    subject="No actor",
                    requester_id=user_id,
                    client_id=org_id,
                ),
                origin="technician",
            )


async def test_alert_origin_discards_actor_and_requester_even_if_supplied(api_env) -> None:
    """The module docstring's "forced regardless of what data.requester_id/
    actor_id carry" claim, exercised directly: pass origin="alert" WITH a
    non-None actor_id AND a non-None data.requester_id, and confirm both are
    discarded rather than smuggled through."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,  # non-None actor_id
            TicketCreate(
                type="incident",
                category_id=category.id,
                subject="Alert with smuggled attribution",
                requester_id=user_id,  # non-None requester_id
                client_id=org_id,
            ),
            origin="alert",
        )
    assert ticket.requester_id is None
    assert ticket.created_by is None


async def test_alert_origin_rejects_non_incident_type(api_env) -> None:
    """Spec §7.2: alert-opened tickets are `type="incident"`. `origin="alert"`
    paired with `type="request"` must be rejected up front, not silently
    accepted with a relaxed human-attribution rule on a request ticket."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session,
            org_id,
            user_id,
            key="software_request",
            type="request",
            requires_approval=False,
            default_queue_id=queue.id,
        )
        with pytest.raises(TicketValidationError):
            await create_ticket(
                session,
                org_id,
                None,
                TicketCreate(
                    type="request", category_id=category.id, subject="Alert on a request",
                    client_id=org_id,
                ),
                origin="alert",
            )


async def test_source_alert_id_rejected_for_non_alert_origin(api_env) -> None:
    """Spec §2: `source_alert_id` is NULL for every ticket until Phase 15.
    A non-None `source_alert_id` paired with any origin other than `"alert"`
    must be rejected, not persisted."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        with pytest.raises(TicketValidationError):
            await create_ticket(
                session,
                org_id,
                user_id,
                TicketCreate(
                    type="incident",
                    category_id=category.id,
                    subject="Not an alert",
                    requester_id=user_id,
                    client_id=org_id,
                ),
                origin="technician",
                source_alert_id=uuid7(),
            )


# ---------------------------------------------------------------------------
# create_ticket -- audit
# ---------------------------------------------------------------------------


async def test_create_audits_exactly_once_with_no_subject_leak(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=category.id,
                subject="Highly sensitive subject line",
                description="Highly sensitive description body",
                requester_id=user_id,
                client_id=org_id,
            ),
        )
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "ticket.created",
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
    metadata_str = str(event.meta)
    assert "Highly sensitive subject line" not in metadata_str
    assert "Highly sensitive description body" not in metadata_str


# ---------------------------------------------------------------------------
# get_ticket
# ---------------------------------------------------------------------------


async def test_get_ticket_returns_the_created_ticket(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        created = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident", category_id=category.id, subject="Broken", requester_id=user_id,
                client_id=org_id,
            ),
        )
        fetched = await get_ticket(session, org_id, created.id)
    assert fetched.id == created.id


async def test_get_ticket_raises_not_found_for_nonexistent_id(api_env) -> None:
    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(TicketNotFoundError):
            await get_ticket(session, org_id, uuid7())


# ---------------------------------------------------------------------------
# list_tickets -- filters and ordering
# ---------------------------------------------------------------------------


async def test_list_tickets_filters_by_type_status_queue_priority(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue_a = await _queue(session, org_id, user_id, key="helpdesk")
        queue_b = await _queue(session, org_id, user_id, key="infrastructure")
        incident_cat = await _category(
            session, org_id, user_id, key="hardware", type="incident", default_queue_id=queue_a.id
        )
        request_cat = await _category(
            session,
            org_id,
            user_id,
            key="software_request",
            type="request",
            requires_approval=False,
            default_queue_id=queue_b.id,
        )
        incident = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=incident_cat.id,
                subject="Incident",
                priority="high",
                requester_id=user_id,
                client_id=org_id,
            ),
        )
        request = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="request",
                category_id=request_cat.id,
                subject="Request",
                requester_id=user_id,
                client_id=org_id,
            ),
        )

        by_type = await list_tickets(session, org_id, type="incident")
        assert {t.id for t in by_type} == {incident.id}

        by_status = await list_tickets(session, org_id, status="in_progress")
        assert {t.id for t in by_status} == {request.id}

        by_queue = await list_tickets(session, org_id, queue_id=queue_a.id)
        assert {t.id for t in by_queue} == {incident.id}

        by_priority = await list_tickets(session, org_id, priority="high")
        assert {t.id for t in by_priority} == {incident.id}


# C1 fix (whole-branch review round C): `q` was UNDELIVERED SPEC -- declared
# in spec §9 with no service-layer argument, no route parameter, and no
# client method at all. Case-insensitive substring match over `subject` OR
# `description`, matched separately so a hit against either column counts.
async def test_list_tickets_filters_by_q_case_insensitive_over_subject_and_description(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        subject_hit = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=category.id,
                subject="Printer jam on 3rd floor",
                description="No detail.",
                requester_id=user_id,
                client_id=org_id,
            ),
        )
        description_hit = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=category.id,
                subject="Escalation",
                description="The PRINTER on the 3rd floor is jammed again.",
                requester_id=user_id,
                client_id=org_id,
            ),
        )
        no_hit = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=category.id,
                subject="Laptop won't boot",
                description="Black screen.",
                requester_id=user_id,
                client_id=org_id,
            ),
        )

        # Case-insensitive: the query is lowercase, "Printer"/"PRINTER" are not.
        by_q = await list_tickets(session, org_id, q="printer")
        assert {t.id for t in by_q} == {subject_hit.id, description_hit.id}
        assert no_hit.id not in {t.id for t in by_q}

        # A substring that matches nothing returns an empty list, not every row.
        assert await list_tickets(session, org_id, q="nonexistent-term-xyz") == []


async def test_list_tickets_filters_by_assigned_to_requester_and_device(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    other_sub = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        other_user_id = (
            await session.execute(select(User.id).where(User.external_subject == other_sub))
        ).scalar_one()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        device = Device(
            org_id=org_id, hostname="host-1", os_family="linux", client_id=org_id, site_id=org_id
        )
        session.add(device)
        await session.flush()

        assigned = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=category.id,
                subject="Assigned",
                requester_id=user_id,
                assigned_to=other_user_id,
                device_id=device.id,
            ),
        )
        unassigned = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=category.id,
                subject="Unassigned",
                requester_id=other_user_id,
                client_id=org_id,
            ),
        )

        by_assignee = await list_tickets(session, org_id, assigned_to=other_user_id)
        assert {t.id for t in by_assignee} == {assigned.id}

        by_requester = await list_tickets(session, org_id, requester_id=other_user_id)
        assert {t.id for t in by_requester} == {unassigned.id}

        by_device = await list_tickets(session, org_id, device_id=device.id)
        assert {t.id for t in by_device} == {assigned.id}


async def test_list_tickets_is_ordered_newest_first(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        first = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident", category_id=category.id, subject="First", requester_id=user_id,
                client_id=org_id,
            ),
        )
        second = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident", category_id=category.id, subject="Second", requester_id=user_id,
                client_id=org_id,
            ),
        )
        third = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident", category_id=category.id, subject="Third", requester_id=user_id,
                client_id=org_id,
            ),
        )
        rows = await list_tickets(session, org_id)
    ids = [t.id for t in rows]
    assert ids.index(third.id) < ids.index(second.id) < ids.index(first.id)


async def test_list_tickets_respects_limit_and_offset(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        for i in range(5):
            await create_ticket(
                session,
                org_id,
                user_id,
                TicketCreate(
                    type="incident",
                    category_id=category.id,
                    subject=f"Ticket {i}",
                    requester_id=user_id,
                    client_id=org_id,
                ),
            )
        page_1 = await list_tickets(session, org_id, limit=2, offset=0)
        page_2 = await list_tickets(session, org_id, limit=2, offset=2)
    assert len(page_1) == 2
    assert len(page_2) == 2
    assert {t.id for t in page_1}.isdisjoint({t.id for t in page_2})


# ---------------------------------------------------------------------------
# update_ticket -- PATCH semantics
# ---------------------------------------------------------------------------


async def _create_basic_ticket(session, org_id, user_id):
    queue = await _queue(session, org_id, user_id)
    category = await _category(session, org_id, user_id, type="incident", default_queue_id=queue.id)
    ticket = await create_ticket(
        session,
        org_id,
        user_id,
        TicketCreate(
            type="incident",
            category_id=category.id,
            subject="Original subject",
            description="Original description",
            requester_id=user_id,
            client_id=org_id,
        ),
    )
    return ticket, category, queue


async def test_update_omitted_key_leaves_field_untouched(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        updated = await update_ticket(session, org_id, user_id, ticket.id, {"priority": "high"})
    assert updated.priority == "high"
    assert updated.subject == "Original subject"
    assert updated.description == "Original description"


async def test_update_explicit_none_on_nullable_field_clears_it(api_env) -> None:
    """`device_id` is the only nullable field left in `TICKET_EDITABLE_FIELDS`
    as of task 8 -- `assigned_to` moved to `transitions.assign_ticket`
    (`ticket.assign`, spec §5), so it can no longer exercise this PATCH
    semantics case here."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        device = Device(
            org_id=org_id, hostname="host-1", os_family="linux", client_id=org_id, site_id=org_id
        )
        session.add(device)
        await session.flush()

        assigned = await update_ticket(
            session, org_id, user_id, ticket.id, {"device_id": device.id}
        )
        assert assigned.device_id == device.id

        cleared = await update_ticket(session, org_id, user_id, ticket.id, {"device_id": None})
    assert cleared.device_id is None


async def test_update_explicit_none_on_non_nullable_field_rejected_before_setattr(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        with pytest.raises(TicketValidationError):
            await update_ticket(
                session,
                org_id,
                user_id,
                ticket.id,
                {"priority": "urgent", "subject": None},
            )
        # The rejection must happen BEFORE any setattr: `priority` must not
        # have been half-applied even though it appeared earlier in the dict.
        reloaded = await get_ticket(session, org_id, ticket.id)
    assert reloaded.priority == "normal"
    assert reloaded.subject == "Original subject"


async def test_status_is_not_editable_through_update_ticket(api_env) -> None:
    """`status` moves only through `transitions.change_status`.

    This used to assert the key was *silently dropped* while the rest of the
    PATCH applied. That is the behaviour the SLA task 8 review's F1 rejected
    (for `queue_id`, on the identical mechanism): a caller that asks to close
    a ticket and bump its priority, and is told nothing, reasonably believes
    both happened. Now the whole call is refused, naming `change_status`, and
    the editable half is NOT applied -- see
    `test_update_ticket_refusal_does_not_apply_the_editable_half`."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        ticket_id = ticket.id
        with pytest.raises(TicketValidationError) as excinfo:
            await update_ticket(
                session, org_id, user_id, ticket_id, {"status": "open", "priority": "high"}
            )
        assert "status" in str(excinfo.value)
        assert "change_status" in str(excinfo.value)
        reloaded = await get_ticket(session, org_id, ticket_id)
    assert reloaded.status == "new"
    assert reloaded.priority == "normal"


async def test_terminal_ticket_rejects_edits(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        # This test targets `update_ticket`'s own terminal-edit guard in
        # isolation, not the full `transitions.change_status` path -- forcing
        # the status directly is deliberate here.
        ticket.status = "cancelled"
        await session.flush()

        with pytest.raises(TicketValidationError):
            await update_ticket(session, org_id, user_id, ticket.id, {"priority": "high"})


async def test_closed_ticket_rejects_edits(api_env) -> None:
    """`closed` is in `TERMINAL_STATUSES` (edits gated) but NOT in
    `FINAL_STATUSES` (nothing ever leaves) -- spec §3.3 distinguishes the two
    on purpose, since `closed` is deliberately reopenable via
    `transitions.change_status` even though its fields may not be edited
    directly. `test_terminal_ticket_rejects_edits` above forces `cancelled`,
    which is in BOTH sets, so it alone cannot catch `_reject_if_terminal`
    being gated on `FINAL_STATUSES` instead of `TERMINAL_STATUSES` -- that
    swap would leave every `closed` ticket silently editable while this
    suite stayed green. This case, forcing `closed` specifically, is the one
    that would catch it (mutation-verified: see
    `.git/sdd/task-7-report.md`)."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        ticket.status = "closed"
        await session.flush()

        with pytest.raises(TicketValidationError):
            await update_ticket(session, org_id, user_id, ticket.id, {"priority": "high"})


async def test_update_category_id_enforces_type_match(api_env) -> None:
    """R1: `create_ticket` rejects `data.type != category.type` up front
    (`test_create_rejects_type_mismatch_with_category` above), but until this
    test existed `update_ticket` applied a PATCHed `category_id` with no such
    check -- a caller could create an incident under an `incident` category
    (passing the create-time guard), then `PATCH {"category_id": <a
    request-type category>}` and end up with a `type="incident"` ticket
    carrying a `type="request"` category, unreachable by the create path's
    guard and uncaught by any DB constraint."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)  # type="incident"
        request_queue = await _queue(session, org_id, user_id, key="requests")
        request_category = await _category(
            session,
            org_id,
            user_id,
            key="software_request",
            type="request",
            requires_approval=False,
            default_queue_id=request_queue.id,
        )
        with pytest.raises(TicketValidationError):
            await update_ticket(
                session, org_id, user_id, ticket.id, {"category_id": request_category.id}
            )
        # The rejection must leave the ticket's own category untouched.
        reloaded = await get_ticket(session, org_id, ticket.id)
    assert reloaded.category_id != request_category.id


async def test_update_cross_org_category_id_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Runs on a raw BYPASSRLS admin session, not `org_session` -- inside
    `org_session`, RLS alone already hides org B's row, so this would pass
    even with the explicit `Ticket.org_id`/`TicketCategory.org_id` predicates
    deleted. Spec §8.1 requires those predicates as defence in depth IN
    ADDITION to RLS; only a raw admin session, where RLS is bypassed and org
    B's category is fully visible, actually exercises them."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_a, user_a)
        cat_b = await _category(session, org_b, user_b, type="incident")

        with pytest.raises(TicketNotFoundError):
            await update_ticket(session, org_a, user_a, ticket.id, {"category_id": cat_b.id})


async def test_update_cross_org_device_id_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Same reasoning as the category_id case above: a raw admin session so
    the explicit `Device.org_id == org_id` predicate in `_resolve_device` is
    what is actually under test, not RLS.

    (`assigned_to`'s equivalent cross-org update-path test moved to
    `test_service_desk_transitions.py`'s `assign_ticket` coverage as of task
    8 -- `assigned_to` is no longer editable through `update_ticket`, see
    `TICKET_EDITABLE_FIELDS`.)"""
    org_a, user_a = await _org_with_user(api_env)
    org_b, _user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_a, user_a)
        device_b = Device(
            org_id=org_b, hostname="org-b-host", os_family="linux", client_id=org_b, site_id=org_b
        )
        session.add(device_b)
        await session.flush()

        with pytest.raises(TicketNotFoundError):
            await update_ticket(session, org_a, user_a, ticket.id, {"device_id": device_b.id})


# ---------------------------------------------------------------------------
# create_ticket -- cross-org resolution (spec §8.1: the explicit `org_id`
# predicate on every resolver is defence in depth IN ADDITION TO RLS, not a
# substitute for it). All five run on a raw BYPASSRLS `AsyncSession
# (admin_engine)`, never `org_session` -- inside `org_session`, RLS already
# hides the other org's row, so a test written there would pass even if the
# explicit predicate were deleted from the resolver, proving RLS works and
# nothing about the predicate. Brief Step 1 required cross-org rejection on
# creation for all five references; only the two UPDATE-path tests above
# existed before this fix, so `_resolve_device` and the requester branch had
# no cross-org coverage on ANY path.
# ---------------------------------------------------------------------------


async def test_create_cross_org_category_id_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue_a = await _queue(session, org_a, user_a)
        category_b = await _category(session, org_b, user_b, type="incident")

        with pytest.raises(TicketNotFoundError):
            await create_ticket(
                session,
                org_a,
                user_a,
                TicketCreate(
                    type="incident",
                    category_id=category_b.id,
                    queue_id=queue_a.id,
                    subject="Cross-org category",
                    requester_id=user_a,
                    client_id=org_a,
                ),
            )


async def test_create_cross_org_queue_id_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue_b = await _queue(session, org_b, user_b)
        # No default_queue_id on category_a -- this test must exercise the
        # explicit-queue_id branch of create_ticket's resolution, not the
        # category-default branch.
        category_a = await _category(session, org_a, user_a, type="incident")

        with pytest.raises(TicketNotFoundError):
            await create_ticket(
                session,
                org_a,
                user_a,
                TicketCreate(
                    type="incident",
                    category_id=category_a.id,
                    queue_id=queue_b.id,
                    subject="Cross-org queue",
                    requester_id=user_a,
                    client_id=org_a,
                ),
            )


async def test_create_cross_org_device_id_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Mutation-verified (see `.git/sdd/task-7-report.md`): deleting
    `Device.org_id == org_id` from `_resolve_device` currently breaks nothing
    else in the suite, since no other test on any path exercised a cross-org
    `device_id`."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue_a = await _queue(session, org_a, user_a)
        category_a = await _category(
            session, org_a, user_a, type="incident", default_queue_id=queue_a.id
        )
        device_b = Device(
            org_id=org_b, hostname="org-b-host", os_family="linux", client_id=org_b, site_id=org_b
        )
        session.add(device_b)
        await session.flush()

        with pytest.raises(TicketNotFoundError):
            await create_ticket(
                session,
                org_a,
                user_a,
                TicketCreate(
                    type="incident",
                    category_id=category_a.id,
                    subject="Cross-org device",
                    requester_id=user_a,
                    device_id=device_b.id,
                ),
            )


async def test_create_cross_org_requester_id_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue_a = await _queue(session, org_a, user_a)
        category_a = await _category(
            session, org_a, user_a, type="incident", default_queue_id=queue_a.id
        )

        with pytest.raises(TicketNotFoundError):
            await create_ticket(
                session,
                org_a,
                user_a,
                TicketCreate(
                    type="incident",
                    category_id=category_a.id,
                    subject="Cross-org requester",
                    requester_id=user_b,
                    client_id=org_a,
                ),
            )


async def test_create_cross_org_assigned_to_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue_a = await _queue(session, org_a, user_a)
        category_a = await _category(
            session, org_a, user_a, type="incident", default_queue_id=queue_a.id
        )

        with pytest.raises(TicketNotFoundError):
            await create_ticket(
                session,
                org_a,
                user_a,
                TicketCreate(
                    type="incident",
                    category_id=category_a.id,
                    subject="Cross-org assignee",
                    requester_id=user_a,
                    assigned_to=user_b,
                    client_id=org_a,
                ),
            )


# ---------------------------------------------------------------------------
# update_ticket -- audit
# ---------------------------------------------------------------------------


async def test_update_audits_exactly_once_with_field_names_only(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        await update_ticket(
            session,
            org_id,
            user_id,
            ticket.id,
            {"subject": "Updated sensitive subject", "priority": "high"},
        )
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "ticket.updated",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    event = events[0]
    # B2 (whole-branch review, round B): attribution was never asserted here
    # -- `actor_type="user"` -> `"system"` and `actor_id=<actor>` -> `None`
    # both survived with the suite green, since only `target_type`/`target_id`
    # /`meta` were checked. `ticket.created`'s system-attributed side is
    # already pinned thoroughly (e.g. `test_alert_origin_status_history_is_
    # system_actor`); this is the user side of the same event family.
    assert event.actor_type == "user"
    assert event.actor_id == user_id
    assert event.meta == {"changed_fields": ["priority", "subject"]}
    assert "Updated sensitive subject" not in str(event.meta)


# ---------------------------------------------------------------------------
# update_ticket -- system actor (task 8, spec §1)
# ---------------------------------------------------------------------------


async def test_update_ticket_system_actor_is_audited_with_no_actor_id(api_env) -> None:
    """Mirrors `change_status`'s system-actor path: `actor_type="system"`
    with `actor_id=None` must reach `audit_service.record` unchanged --
    never silently coerced back to `"user"` (that would make an audit row
    claim a human actor did something no human did)."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        updated = await update_ticket(
            session,
            org_id,
            None,
            ticket.id,
            {"priority": "high"},
            actor_type="system",
        )
        assert updated.priority == "high"
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "ticket.updated",
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
    assert event.meta == {"changed_fields": ["priority"]}


async def test_update_ticket_rejects_unknown_actor_type(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        with pytest.raises(TicketValidationError):
            await update_ticket(
                session,
                org_id,
                user_id,
                ticket.id,
                {"priority": "high"},
                actor_type="agent",  # type: ignore[arg-type]
            )


async def test_update_ticket_rejects_system_actor_with_actor_id(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        with pytest.raises(TicketValidationError):
            await update_ticket(
                session,
                org_id,
                user_id,
                ticket.id,
                {"priority": "high"},
                actor_type="system",
            )


async def test_update_ticket_rejects_user_actor_type_without_actor_id(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        with pytest.raises(TicketValidationError):
            await update_ticket(
                session,
                org_id,
                None,
                ticket.id,
                {"priority": "high"},
                actor_type="user",
            )


# ---------------------------------------------------------------------------
# update_ticket -- non-editable and unknown keys are REFUSED, never dropped
# (SLA task 8 review, F1)
# ---------------------------------------------------------------------------


async def test_update_ticket_refuses_queue_id_rather_than_silently_dropping_it(
    api_env,
) -> None:
    """`queue_id` is deliberately absent from `TICKET_EDITABLE_FIELDS`
    (commit `eb6b129`): spec §5 gates queue moves on `ticket.assign`, not
    `ticket.write`, and §8.3 files them under `ticket.assigned`.
    `transitions.assign_ticket` is the write path.

    Before this fix the key was DISCARDED -- `update_ticket` returned
    normally, the ticket had not moved, and the `ticket.updated` audit row
    said nothing about it. That is exactly the failure mode `eb6b129`
    removed from the HTTP layer ("a silent no-op is worse than an error,
    because it is an error the client cannot see") and it survived one layer
    down, where SLA task 9's queue-move escalation was about to call it.
    The refusal names the service to call instead so a caller cannot be left
    guessing."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        other_queue = await create_queue(session, org_id, user_id, key="esc", name="Escalation")
        ticket_id = ticket.id
        original_queue_id = ticket.queue_id

        with pytest.raises(TicketValidationError) as excinfo:
            await update_ticket(
                session,
                org_id,
                user_id,
                ticket_id,
                {"queue_id": other_queue.id},
            )
        assert "queue_id" in str(excinfo.value)
        assert "assign_ticket" in str(excinfo.value)

        # The refusal runs before anything is loaded or mutated: the ticket
        # is untouched and no audit row claims an update happened.
        reloaded = await get_ticket(session, org_id, ticket_id)
        assert reloaded.queue_id == original_queue_id
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "ticket.updated",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert events == []


@pytest.mark.parametrize(
    ("field", "value", "hint"),
    [
        # Moved to `transitions.assign_ticket` (spec §5, §8.3).
        ("assigned_to", None, "assign_ticket"),
        # Moved to `transitions.change_status` (spec §3.3, §9).
        ("status", "closed", "change_status"),
        # Immutable after creation -- no service accepts it, so no redirect.
        ("type", "request", None),
        # A plain typo: caught, not applied, not ignored.
        ("prioirty", "high", None),
    ],
)
async def test_update_ticket_refuses_every_non_editable_key(
    api_env, field: str, value: object, hint: str | None
) -> None:
    """Mirrors `sla.policies.update_policy`'s F6 fix: a key that is not in
    the whitelist is a typed 400 naming it, not a discarded key and a 200
    that reports success. The whitelist still decides what may be written,
    so this widens nothing -- it only stops the caller being lied to. For
    the two keys that moved to a dedicated service, the message names that
    service."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)

        with pytest.raises(TicketValidationError) as excinfo:
            await update_ticket(session, org_id, user_id, ticket.id, {field: value})
        assert field in str(excinfo.value)
        if hint is not None:
            assert hint in str(excinfo.value)


async def test_update_ticket_refusal_does_not_apply_the_editable_half(api_env) -> None:
    """A mixed PATCH (one editable key, one not) is refused WHOLE. Applying
    the editable half and dropping the rest is the same silent partial
    success in a smaller costume -- and is precisely the shape SLA task 9's
    combined priority+queue escalation would have taken."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _cat, _q = await _create_basic_ticket(session, org_id, user_id)
        other_queue = await create_queue(session, org_id, user_id, key="esc", name="Escalation")
        ticket_id = ticket.id
        original_priority = ticket.priority

        with pytest.raises(TicketValidationError):
            await update_ticket(
                session,
                org_id,
                user_id,
                ticket_id,
                {"priority": "urgent", "queue_id": other_queue.id},
            )
        reloaded = await get_ticket(session, org_id, ticket_id)
        assert reloaded.priority == original_priority
        assert reloaded.queue_id != other_queue.id


async def test_non_nullable_ticket_fields_matches_column_nullability() -> None:
    """Regression guard mirroring `test_budget_expenses.py`'s equivalent:
    `NON_NULLABLE_TICKET_FIELDS` must exactly match the editable fields whose
    DB column is actually NOT NULL -- a drift here means either a nullable
    field wrongly rejects `null`, or a NOT NULL field lets one through to
    become an unguarded 500 instead of a typed `TicketValidationError`."""
    actually_non_nullable = {
        field
        for field in TICKET_EDITABLE_FIELDS
        if not Ticket.__table__.columns[field].nullable
    }
    assert NON_NULLABLE_TICKET_FIELDS == actually_non_nullable


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session, mutation-verified
# ---------------------------------------------------------------------------


async def test_tenancy_list_tickets_never_returns_another_orgs_ticket(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never see org B's ticket in `list_tickets`, even on a raw
    BYPASSRLS admin session where org B's row is fully visible.
    `Ticket.org_id == org_id` in `list_tickets`'s query is the SOLE guard --
    mutation-verified by deleting that predicate, running this ONE test in
    isolation, and confirming it fails with a real assertion about org B's
    data (see `.git/sdd/task-7-report.md` for the verbatim output).

    Seeds an org-A ticket too, and asserts the result set equals exactly
    `{ticket_a.id}` -- an `x not in rows` / `all(...)` pair (the previous
    shape of this test) also passes when `list_tickets` returns `[]`, which
    proves nothing. An equality assertion against a non-empty expected set is
    the only version that can actually fail on a leak."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue_a = await _queue(session, org_a, user_a)
        category_a = await _category(
            session, org_a, user_a, type="incident", default_queue_id=queue_a.id
        )
        ticket_a = await create_ticket(
            session,
            org_a,
            user_a,
            TicketCreate(
                type="incident",
                category_id=category_a.id,
                subject="Org A's ticket",
                requester_id=user_a,
                client_id=org_a,
            ),
        )

        queue_b = await _queue(session, org_b, user_b)
        category_b = await _category(
            session, org_b, user_b, type="incident", default_queue_id=queue_b.id
        )
        ticket_b = await create_ticket(
            session,
            org_b,
            user_b,
            TicketCreate(
                type="incident",
                category_id=category_b.id,
                subject="Org B's ticket",
                requester_id=user_b,
                client_id=org_b,
            ),
        )
        await session.commit()
        ticket_a_id = ticket_a.id
        ticket_b_id = ticket_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = await list_tickets(session, org_a)

    assert {t.id for t in rows} == {ticket_a_id}, (
        f"org A's list_tickets did not return exactly org A's ticket "
        f"(ticket_b_id={ticket_b_id})"
    )


# ---------------------------------------------------------------------------
# list_status_history (task 13's G1 fix -- schema.py's StatusHistoryOut was
# defined and schema-tested but api.py mounted no route returning it; the
# data itself has always been written correctly by create_ticket/
# change_status, so these tests exercise the read side directly)
# ---------------------------------------------------------------------------


async def test_list_status_history_oldest_first_one_row_per_transition(
    api_env, admin_engine: AsyncEngine
) -> None:
    """`list_status_history` orders oldest first, tie-broken on `id`, and
    every call to `create_ticket`/`transitions.change_status` writes exactly
    one row (spec §2 `ticket_status_history`: "every transition, including
    the initial creation")."""
    org_id, user_id = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue = await _queue(session, org_id, user_id)
        category = await _category(
            session, org_id, user_id, type="incident", default_queue_id=queue.id
        )
        ticket = await create_ticket(
            session,
            org_id,
            user_id,
            TicketCreate(
                type="incident",
                category_id=category.id,
                subject="History test",
                requester_id=user_id,
                client_id=org_id,
            ),
        )
        await session.commit()
        ticket_id = ticket.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await change_status(session, org_id, ticket_id, to_status="open", actor_id=user_id)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await change_status(session, org_id, ticket_id, to_status="resolved", actor_id=user_id)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = await list_status_history(session, org_id, ticket_id)

    assert [(r.from_status, r.to_status) for r in rows] == [
        (None, "new"),
        ("new", "open"),
        ("open", "resolved"),
    ]


async def test_tenancy_list_status_history_cross_org_ticket_id_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    """A cross-org `ticket_id` raises `TicketNotFoundError` -- tested on a
    raw BYPASSRLS admin session, never inside `org_session`, where RLS alone
    would enforce isolation and mask a missing/broken `org_id` predicate in
    `list_status_history`'s own `get_ticket` call (spec §8.1's
    defence-in-depth requirement, the same reasoning
    `test_tenancy_list_tickets_never_returns_another_orgs_ticket` above
    documents). Org A and org B each get a ticket with its own history row,
    created in the SAME raw session so both are visible to it regardless of
    org -- `list_status_history(session, org_a, ticket_b_id)` must still
    404, proving the guard is the explicit predicate, not incidental RLS."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue_a = await _queue(session, org_a, user_a)
        category_a = await _category(
            session, org_a, user_a, type="incident", default_queue_id=queue_a.id
        )
        ticket_a = await create_ticket(
            session,
            org_a,
            user_a,
            TicketCreate(
                type="incident", category_id=category_a.id, subject="Org A's ticket",
                requester_id=user_a,
                client_id=org_a,
            ),
        )

        queue_b = await _queue(session, org_b, user_b)
        category_b = await _category(
            session, org_b, user_b, type="incident", default_queue_id=queue_b.id
        )
        ticket_b = await create_ticket(
            session,
            org_b,
            user_b,
            TicketCreate(
                type="incident", category_id=category_b.id, subject="Org B's ticket",
                requester_id=user_b,
                client_id=org_b,
            ),
        )
        await session.commit()
        ticket_a_id = ticket_a.id
        ticket_b_id = ticket_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        # Org A's own ticket resolves fine...
        rows = await list_status_history(session, org_a, ticket_a_id)
        assert [(r.from_status, r.to_status) for r in rows] == [(None, "new")]

        # ...but org B's ticket id, requested as org A, is indistinguishable
        # from nonexistent (invariant #5) -- not a leaked empty list, a
        # typed 404-mapped error.
        with pytest.raises(TicketNotFoundError):
            await list_status_history(session, org_a, ticket_b_id)


async def test_list_status_history_org_id_predicate_is_exercised(
    api_env, admin_engine: AsyncEngine
) -> None:
    """B1 (whole-branch review, round B): `list_status_history`'s own query
    carries an explicit `TicketStatusHistory.org_id == org_id` predicate IN
    ADDITION to `get_ticket`'s upstream org-scoped load -- and until this
    test existed, deleting that predicate left all 41 tests in this file
    green. `test_tenancy_list_status_history_cross_org_ticket_id_not_found`
    above proves nothing about it specifically: a cross-org `ticket_id` is
    caught by `get_ticket` one line earlier, before the history query ever
    runs (task 9's lesson, restated in this function's own docstring: "mirrors
    threads.list_comments exactly" -- it mirrored the query, not the test).

    Copied verbatim (construction only) from `test_service_desk_threads.
    test_list_comments_org_id_predicate_is_exercised`: on a raw admin session,
    stamp a status-history row with org_b's `org_id` while pointing it at
    org_a's own ticket -- a shape no correct caller (`create_ticket`,
    `transitions.change_status`) ever produces, but the query itself does not
    know that. `get_ticket(org_a, ticket_a.id)` succeeds, since the ticket
    really does belong to org_a, so the upstream guard is out of the picture
    entirely; only `list_status_history`'s own `org_id` predicate stands
    between this foreign-stamped row and org_a's history (mutation-verified,
    see `.git/sdd/fix-B-report.md`)."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue_a = await _queue(session, org_a, user_a)
        category_a = await _category(
            session, org_a, user_a, type="incident", default_queue_id=queue_a.id
        )
        ticket_a = await create_ticket(
            session,
            org_a,
            user_a,
            TicketCreate(
                type="incident", category_id=category_a.id, subject="Org A's ticket",
                requester_id=user_a,
                client_id=org_a,
            ),
        )

        # A shape no correct caller ever produces -- a status-history row
        # whose `ticket_id` points at org_a's ticket but whose `org_id` is
        # org_b's. No FK/CHECK ties the two together, so this insert
        # succeeds; it exists purely to isolate the query's own predicate.
        foreign = TicketStatusHistory(
            org_id=org_b,
            ticket_id=ticket_a.id,
            from_status=None,
            to_status="cancelled",
            actor_type="user",
            actor_id=user_b,
            reason="must never surface under org_a's history",
        )
        session.add(foreign)
        await session.flush()

        rows = await list_status_history(session, org_a, ticket_a.id)

    assert [(r.from_status, r.to_status) for r in rows] == [(None, "new")]
