"""Reference data: ticket queues and categories (spec §2, §5).

Covers: `ensure_default_queues` seeds exactly the three `DEFAULT_QUEUES` and
is idempotent by construction; `ensure_default_categories` seeds exactly the
nine `DEFAULT_CATEGORIES` with the right `type`/`default_queue_id`/
`requires_approval`, calling `ensure_default_queues` first so a fresh org
gets a usable desk from the category seed alone; both seeds emit exactly one
system-attributed event and nothing on a second call; a duplicate `key`
(queue or category) is rejected as a typed `TicketValidationError`,
sequentially and under a deterministic concurrent interleave;
`resolve_queue`/`resolve_category` raise `TicketNotFoundError` for a
cross-org id, load-bearing on a raw BYPASSRLS admin session; a category with
`type="incident"` and `requires_approval=True` is rejected at the service
layer; `list_queues`/`list_categories` exclude inactive by default, include
it with `include_inactive=True`, and are explicitly ordered by
`sort_order, name`.
"""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, Role, TicketCategory, TicketQueue, User
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.refdata import (
    DEFAULT_CATEGORIES,
    DEFAULT_QUEUES,
    create_category,
    create_queue,
    ensure_default_categories,
    ensure_default_queues,
    list_categories,
    list_queues,
    resolve_category,
    resolve_queue,
    resolve_role,
    update_category,
    update_queue,
)


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Create a throwaway org plus an `org-admin` user in it; return
    `(org_id, user_id)`. Mirrors `test_budget_refdata.py`'s helper of the
    same name/shape -- not imported across test modules, per convention."""
    org_id = await api_env.create_org("sd-refdata")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


# ---------------------------------------------------------------------------
# ticket_queue seeding
# ---------------------------------------------------------------------------


async def test_ensure_default_queues_creates_exactly_three(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        queues = await ensure_default_queues(session, org_id, user_id)
    assert {q.key for q in queues} == {key for key, _name in DEFAULT_QUEUES}
    assert {q.name for q in queues} == {name for _key, name in DEFAULT_QUEUES}
    assert all(q.org_id == org_id for q in queues)


async def test_ensure_default_queues_is_idempotent_by_construction(api_env) -> None:
    """Running it twice must not duplicate: the org's existing keys are
    selected first, so a second call inserts nothing."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        first = await ensure_default_queues(session, org_id, user_id)
        second = await ensure_default_queues(session, org_id, user_id)
    assert len(first) == 3
    assert len(second) == 3
    assert {q.id for q in first} == {q.id for q in second}

    async with org_session(org_id, engine=api_env.app_engine) as session:
        rows = (
            await session.execute(select(TicketQueue).where(TicketQueue.org_id == org_id))
        ).scalars().all()
    assert len(rows) == 3


async def test_seed_emits_one_system_event_and_nothing_on_second_call(api_env) -> None:
    """The seed is a system bootstrap, not an operator action: the first
    call for a fresh org must write exactly ONE `ticket.queues_seeded`,
    system-attributed (`actor_type="system"`, `actor_id=None`), naming the
    three keys it inserted. A second call, which inserts nothing, must write
    nothing at all -- not even a second `ticket.queues_seeded`. Per-row
    user-attributed events on a lazy seed were a real Budget defect: a
    read-only user opening a page became, in the immutable audit log, the
    creator of the org's reference data."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await ensure_default_queues(session, org_id, user_id)

        seeded_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.queues_seeded",
                )
            )
        ).scalars().all()

    assert len(seeded_events) == 1
    event = seeded_events[0]
    assert event.actor_type == "system"
    assert event.actor_id is None
    assert set(event.meta["keys"]) == {key for key, _name in DEFAULT_QUEUES}

    async with org_session(org_id, engine=api_env.app_engine) as session:
        await ensure_default_queues(session, org_id, user_id)
        seeded_events_after = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.queues_seeded",
                )
            )
        ).scalars().all()
    assert len(seeded_events_after) == 1


async def test_seed_backfills_only_the_missing_queues(api_env) -> None:
    """If an org already has some (but not all) default queues, a later call
    adds only what is missing and audits only the keys it actually
    inserted."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        pre_existing = TicketQueue(
            org_id=org_id, key="helpdesk", name="Custom Helpdesk Name", created_by=user_id
        )
        session.add(pre_existing)
        await session.flush()
        pre_existing_id = pre_existing.id

        queues = await ensure_default_queues(session, org_id, user_id)

        seeded_event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.queues_seeded",
                )
            )
        ).scalar_one()

    assert len(queues) == 3
    by_key = {q.key: q for q in queues}
    assert by_key["helpdesk"].id == pre_existing_id
    assert by_key["helpdesk"].name == "Custom Helpdesk Name"
    assert set(seeded_event.meta["keys"]) == {"infrastructure", "security"}


# ---------------------------------------------------------------------------
# ticket_queue CRUD
# ---------------------------------------------------------------------------


async def test_duplicate_queue_key_in_one_org_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await create_queue(session, org_id, user_id, key="ops", name="Ops")
        with pytest.raises(TicketValidationError):
            await create_queue(session, org_id, user_id, key="ops", name="Ops 2")


async def test_create_queue_concurrent_duplicate_raises_typed_error_not_integrity_error(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Two concurrent `create_queue` calls for the same `(org_id, key)` can
    both miss the in-memory pre-check before either has written. The fix
    mirrors `ensure_default_queues`'s own conflict-safe write: the actual
    insert goes through `ON CONFLICT DO NOTHING` against
    `uq_ticket_queue_org_key` -- but this is a single-caller CREATE, not a
    get-or-create, so the race loser must raise the SAME typed
    `TicketValidationError` the pre-check raises, never a raw
    `IntegrityError`.

    Deterministic interleave, following `test_budget_refdata.py`'s
    concurrent-duplicate test: session A's pre-check SELECT against
    `ticket_queue` is intercepted, and right after it returns (a miss --
    nothing exists yet), session B inserts and commits the EXACT row session
    A is about to try to create."""
    org_id, user_id = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "ticket_queue.id" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    winner = TicketQueue(
                        org_id=org_id, key="ops", name="Ops (winner)", created_by=user_id
                    )
                    session_b.add(winner)
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        with pytest.raises(TicketValidationError, match="queue key already exists"):
            await create_queue(session_a, org_id, user_id, key="ops", name="Ops")
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(TicketQueue).where(
                        TicketQueue.org_id == org_id, TicketQueue.key == "ops"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].name == "Ops (winner)"


async def test_same_queue_key_in_two_orgs_is_fine(api_env) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_a, engine=api_env.app_engine) as session:
        queue_a = await create_queue(session, org_a, user_a, key="ops", name="Ops")
    async with org_session(org_b, engine=api_env.app_engine) as session:
        queue_b = await create_queue(session, org_b, user_b, key="ops", name="Ops")
    assert queue_a.key == queue_b.key == "ops"
    assert queue_a.id != queue_b.id


async def test_create_queue_audits(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        queue = await create_queue(
            session, org_id, user_id, key="ops", name="Ops", description="Operations"
        )
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "ticket.queue_created"
                )
            )
        ).scalar_one()
    assert event.target_type == "ticket_queue"
    assert event.target_id == str(queue.id)
    assert event.actor_type == "user"
    assert event.actor_id == user_id
    assert queue.description == "Operations"


async def test_create_queue_with_nonzero_sort_order_writes_exactly_one_audit_event(
    api_env,
) -> None:
    """Whole-branch security review, minor finding: `sort_order` is an
    INSERT-time value on `create_queue` itself now, not a follow-up
    `update_queue` call applied by the route -- a single create request must
    write exactly one audit event, never `ticket.queue_created` immediately
    followed by a spurious `ticket.queue_updated`."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        queue = await create_queue(session, org_id, user_id, key="ops", name="Ops", sort_order=5)

        created_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "ticket.queue_created"
                )
            )
        ).scalars().all()
        updated_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "ticket.queue_updated"
                )
            )
        ).scalars().all()
    assert queue.sort_order == 5
    assert len(created_events) == 1
    assert updated_events == []


async def test_resolve_queue_raises_not_found_for_cross_org_id(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Load-bearing on a raw BYPASSRLS admin session (spec §8, invariant
    #5): org B's row is fully visible on this connection, so
    `resolve_queue`'s own `org_id` predicate is the SOLE guard."""
    org_a, _user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue_b = TicketQueue(org_id=org_b, key="ops", name="Org B Ops", created_by=user_b)
        session.add(queue_b)
        await session.commit()
        queue_b_id = queue_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(TicketNotFoundError):
            await resolve_queue(session, org_a, queue_b_id)


async def test_resolve_queue_raises_not_found_for_nonexistent_id(api_env) -> None:
    from uuid_extensions import uuid7

    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        with pytest.raises(TicketNotFoundError):
            await resolve_queue(session, org_id, uuid7())


async def test_update_queue_reports_only_changed_field_names(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        queue = await create_queue(session, org_id, user_id, key="ops", name="Ops")
        await update_queue(
            session, org_id, user_id, queue.id, {"name": "Ops", "sort_order": 5}
        )
        event = (
            await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "ticket.queue_updated"
                )
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    # B2 (whole-branch review, round B): attribution was never asserted here.
    assert event.actor_type == "user"
    assert event.actor_id == user_id
    assert event.meta["changed_fields"] == ["sort_order"]


async def test_update_queue_rejects_null_name(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        queue = await create_queue(session, org_id, user_id, key="ops", name="Ops")
        with pytest.raises(TicketValidationError):
            await update_queue(session, org_id, user_id, queue.id, {"name": None})


async def test_update_queue_never_touches_key(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        queue = await create_queue(session, org_id, user_id, key="ops", name="Ops")
        updated = await update_queue(session, org_id, user_id, queue.id, {"key": "renamed"})
    assert updated.key == "ops"


async def test_list_queues_excludes_inactive_by_default_and_is_ordered(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        z_queue = await create_queue(session, org_id, user_id, key="zulu", name="Zulu")
        a_queue = await create_queue(session, org_id, user_id, key="alpha", name="Alpha")
        await update_queue(session, org_id, user_id, z_queue.id, {"active": False})

        active_only = await list_queues(session, org_id)
        with_inactive = await list_queues(session, org_id, include_inactive=True)

    assert z_queue.id not in {q.id for q in active_only}
    assert a_queue.id in {q.id for q in active_only}
    assert z_queue.id in {q.id for q in with_inactive}
    # ordered by (sort_order, name): both queues share sort_order=0, so name
    # decides -- Alpha before Zulu.
    names_with_inactive = [q.name for q in with_inactive]
    assert names_with_inactive.index("Alpha") < names_with_inactive.index("Zulu")


async def test_list_queues_does_not_seed(api_env) -> None:
    """`list_queues` takes no `actor_id` and must not invoke the lazy seed
    itself (it has no actor to attribute a fallback `created_by` to) -- an
    org that never called `ensure_default_queues` simply has no queues."""
    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        queues = await list_queues(org_id=org_id, session=session)
    assert queues == []


async def test_list_categories_does_not_seed(api_env) -> None:
    """The category-side counterpart. `list_categories` takes no `actor_id`
    either, so it must not self-seed for the same reason `list_queues` does
    not: `created_by` is NOT NULL and there is no actor to attribute.

    Task 11's routes call `ensure_default_categories` explicitly with the real
    actor from `AuthContext`. Both halves of that rule need a test, or the next
    person to add self-seeding only trips one of them.
    """
    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        categories = await list_categories(org_id=org_id, session=session)
    assert categories == []


# ---------------------------------------------------------------------------
# ticket_category
# ---------------------------------------------------------------------------


async def test_ensure_default_categories_creates_all_nine_with_correct_fields(api_env) -> None:
    """The nine defaults land with the exact `type`/`requires_approval`/
    `default_queue_id` the spec's table pins -- not re-derived, copied."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        categories = await ensure_default_categories(session, org_id, user_id)
        queues = {
            q.key: q.id
            for q in (
                await session.execute(select(TicketQueue).where(TicketQueue.org_id == org_id))
            )
            .scalars()
            .all()
        }

    assert {c.key for c in categories} == {key for key, *_ in DEFAULT_CATEGORIES}
    by_key = {c.key: c for c in categories}
    for key, name, type_, requires_approval, queue_key in DEFAULT_CATEGORIES:
        cat = by_key[key]
        assert cat.name == name
        assert cat.type == type_
        assert cat.requires_approval is requires_approval
        assert cat.default_queue_id == queues[queue_key]
        assert cat.org_id == org_id


async def test_ensure_default_categories_is_idempotent_by_construction(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        first = await ensure_default_categories(session, org_id, user_id)
        second = await ensure_default_categories(session, org_id, user_id)
    assert len(first) == 9
    assert len(second) == 9
    assert {c.id for c in first} == {c.id for c in second}

    async with org_session(org_id, engine=api_env.app_engine) as session:
        rows = (
            await session.execute(select(TicketCategory).where(TicketCategory.org_id == org_id))
        ).scalars().all()
    assert len(rows) == 9


async def test_ensure_default_categories_emits_one_system_event_and_nothing_on_second_call(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await ensure_default_categories(session, org_id, user_id)

        seeded_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.categories_seeded",
                )
            )
        ).scalars().all()

    assert len(seeded_events) == 1
    event = seeded_events[0]
    assert event.actor_type == "system"
    assert event.actor_id is None
    assert set(event.meta["keys"]) == {key for key, *_ in DEFAULT_CATEGORIES}

    async with org_session(org_id, engine=api_env.app_engine) as session:
        await ensure_default_categories(session, org_id, user_id)
        seeded_events_after = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.categories_seeded",
                )
            )
        ).scalars().all()
    assert len(seeded_events_after) == 1


async def test_ensure_default_categories_alone_on_a_fresh_org_seeds_both(api_env) -> None:
    """The whole point of the ordering fix: a brand-new org that calls ONLY
    the category seed (never `ensure_default_queues` directly) must still
    end up with a usable desk -- both the three queues and the nine
    categories, each category wired to the correct queue by key."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        categories = await ensure_default_categories(session, org_id, user_id)

        queue_rows = (
            await session.execute(select(TicketQueue).where(TicketQueue.org_id == org_id))
        ).scalars().all()

        queues_seeded_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "ticket.queues_seeded"
                )
            )
        ).scalars().all()

    assert {q.key for q in queue_rows} == {key for key, _name in DEFAULT_QUEUES}
    assert len(queues_seeded_events) == 1

    queue_key_by_id = {q.id: q.key for q in queue_rows}
    by_key = {c.key: c for c in categories}
    for key, _name, _type, _requires_approval, queue_key in DEFAULT_CATEGORIES:
        cat = by_key[key]
        assert queue_key_by_id[cat.default_queue_id] == queue_key


async def test_ensure_default_categories_second_call_does_not_reseed_queues(api_env) -> None:
    """Calling the category seed when the org already has its queues (the
    common case, e.g. a second ticket-creation request) must only pay the
    cheap existence check on the queue side -- no second
    `ticket.queues_seeded` event."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await ensure_default_queues(session, org_id, user_id)
        await ensure_default_categories(session, org_id, user_id)

        queues_seeded_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "ticket.queues_seeded"
                )
            )
        ).scalars().all()
    assert len(queues_seeded_events) == 1


async def test_incident_category_with_requires_approval_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        with pytest.raises(TicketValidationError):
            await create_category(
                session,
                org_id,
                user_id,
                key="password_reset",
                name="Password reset",
                type="incident",
                requires_approval=True,
            )


async def test_incident_category_without_requires_approval_is_fine(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        category = await create_category(
            session,
            org_id,
            user_id,
            key="outage",
            name="Outage",
            type="incident",
            requires_approval=False,
        )
    assert category.type == "incident"
    assert category.requires_approval is False


async def test_request_category_with_requires_approval_is_fine(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        category = await create_category(
            session,
            org_id,
            user_id,
            key="new_laptop",
            name="New laptop",
            type="request",
            requires_approval=True,
        )
    assert category.type == "request"
    assert category.requires_approval is True


async def test_update_category_to_requires_approval_true_on_incident_is_rejected(
    api_env,
) -> None:
    """The create-time guard must not be bypassable through `update_category`
    -- flipping an existing incident category's `requires_approval` to True
    is rejected the same way."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        category = await create_category(
            session,
            org_id,
            user_id,
            key="outage",
            name="Outage",
            type="incident",
            requires_approval=False,
        )
        with pytest.raises(TicketValidationError):
            await update_category(
                session, org_id, user_id, category.id, {"requires_approval": True}
            )


async def test_duplicate_category_key_in_one_org_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        await create_category(
            session, org_id, user_id, key="hardware", name="Hardware", type="request"
        )
        with pytest.raises(TicketValidationError):
            await create_category(
                session, org_id, user_id, key="hardware", name="Hardware 2", type="request"
            )


async def test_create_category_concurrent_duplicate_raises_typed_error_not_integrity_error(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Same shape as the queue equivalent above: session A's pre-check
    SELECT against `ticket_category` is intercepted, and right after it
    returns (a miss), session B inserts and commits the EXACT row session A
    is about to try to create. The race loser must raise the SAME typed
    `TicketValidationError`, never a raw `IntegrityError`."""
    org_id, user_id = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "ticket_category.id" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    winner = TicketCategory(
                        org_id=org_id,
                        key="hardware",
                        name="Hardware (winner)",
                        type="request",
                        created_by=user_id,
                    )
                    session_b.add(winner)
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        with pytest.raises(TicketValidationError, match="category key already exists"):
            await create_category(
                session_a, org_id, user_id, key="hardware", name="Hardware", type="request"
            )
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(TicketCategory).where(
                        TicketCategory.org_id == org_id, TicketCategory.key == "hardware"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].name == "Hardware (winner)"


async def test_same_category_key_in_two_orgs_is_fine(api_env) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_a, engine=api_env.app_engine) as session:
        cat_a = await create_category(
            session, org_a, user_a, key="hardware", name="Hardware", type="request"
        )
    async with org_session(org_b, engine=api_env.app_engine) as session:
        cat_b = await create_category(
            session, org_b, user_b, key="hardware", name="Hardware", type="request"
        )
    assert cat_a.key == cat_b.key == "hardware"
    assert cat_a.id != cat_b.id


async def test_create_category_audits(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        category = await create_category(
            session, org_id, user_id, key="hardware", name="Hardware", type="request"
        )
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.category_created",
                )
            )
        ).scalar_one()
    assert event.target_type == "ticket_category"
    assert event.target_id == str(category.id)
    assert event.actor_type == "user"
    assert event.actor_id == user_id


async def test_create_category_with_nonzero_sort_order_writes_exactly_one_audit_event(
    api_env,
) -> None:
    """The category-side counterpart of the identical queue fix above."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        category = await create_category(
            session, org_id, user_id, key="hardware", name="Hardware", type="request",
            sort_order=5,
        )

        created_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.category_created",
                )
            )
        ).scalars().all()
        updated_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.category_updated",
                )
            )
        ).scalars().all()
    assert category.sort_order == 5
    assert len(created_events) == 1
    assert updated_events == []


async def test_resolve_category_raises_not_found_for_cross_org_id(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Load-bearing on a raw BYPASSRLS admin session (spec §8, invariant
    #5)."""
    org_a, _user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_b = TicketCategory(
            org_id=org_b, key="hardware", name="Org B Hardware", type="request",
            created_by=user_b,
        )
        session.add(cat_b)
        await session.commit()
        cat_b_id = cat_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(TicketNotFoundError):
            await resolve_category(session, org_a, cat_b_id)


async def test_resolve_category_raises_not_found_for_nonexistent_id(api_env) -> None:
    from uuid_extensions import uuid7

    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        with pytest.raises(TicketNotFoundError):
            await resolve_category(session, org_id, uuid7())


# ---------------------------------------------------------------------------
# resolve_role / cross-org default_queue_id & approver_role_id (finding A2)
#
# `create_category`/`update_category` were the ONLY two unresolved cross-refs
# in this module: every other one (`resolve_queue`, `resolve_category`,
# `tickets.resolve_user`/`_resolve_device`) was already org-scoped.
# `Role.org_id` is nullable (global roles alongside org-scoped ones,
# migrations/versions/0003_rbac_seed.py), so the plain FK to `roles.id`
# cannot rule out a role scoped to a DIFFERENT org, and Postgres FK checks
# always bypass row security -- RLS gives no protection either. Every test
# below runs on a raw `AsyncSession(admin_engine)`, never inside
# `org_session`: RLS alone would mask org B's row and prove nothing about the
# explicit `org_id` predicate `resolve_queue`/`resolve_role` add.
# ---------------------------------------------------------------------------


async def test_resolve_role_accepts_a_global_role(api_env, admin_engine: AsyncEngine) -> None:
    """A NULL `Role.org_id` (the seeded platform roles) is usable by every
    org -- `resolve_role` must not reject it as if it were a foreign org's
    row."""
    org_id, _user_id = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        technician_role_id = (
            await session.execute(
                select(Role.id).where(Role.name == "technician", Role.org_id.is_(None))
            )
        ).scalar_one()
        role = await resolve_role(session, org_id, technician_role_id)
    assert role.id == technician_role_id


async def test_resolve_role_raises_not_found_for_cross_org_id(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, _user_a = await _org_with_user(api_env)
    org_b, _user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        role_b = Role(org_id=org_b, name=f"org-b-approver-{org_b.hex[:8]}")
        session.add(role_b)
        await session.commit()
        role_b_id = role_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(TicketNotFoundError):
            await resolve_role(session, org_a, role_b_id)


async def test_create_category_rejects_cross_org_default_queue_id(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Verified by exploit (whole-branch security review): before the fix,
    org B's queue id landed straight on org A's category (a cross-tenant
    existence oracle -- a real foreign id 201'd, a nonexistent one 500'd on
    an unhandled FK violation). Now both raise the identical
    `TicketNotFoundError` `resolve_queue` already raises everywhere else."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue_b = TicketQueue(org_id=org_b, key="ops", name="Org B Ops", created_by=user_b)
        session.add(queue_b)
        await session.commit()
        queue_b_id = queue_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(TicketNotFoundError):
            await create_category(
                session,
                org_a,
                user_a,
                key="hardware",
                name="Hardware",
                type="request",
                default_queue_id=queue_b_id,
            )
        # Nothing was written -- the cross-ref is resolved BEFORE the insert.
        rows = (
            await session.execute(
                select(TicketCategory).where(
                    TicketCategory.org_id == org_a, TicketCategory.key == "hardware"
                )
            )
        ).scalars().all()
    assert rows == []


async def test_create_category_rejects_cross_org_approver_role_id(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Verified by exploit: before the fix, a cross-org `approver_role_id`
    could never be satisfied by anyone in this org -- `lifecycle.py` gives
    `pending_approval` no exit but `decide_approval`, so every request in
    that category was stuck forever."""
    org_a, user_a = await _org_with_user(api_env)
    org_b, _user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        role_b = Role(org_id=org_b, name=f"org-b-approver-{org_b.hex[:8]}")
        session.add(role_b)
        await session.commit()
        role_b_id = role_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(TicketNotFoundError):
            await create_category(
                session,
                org_a,
                user_a,
                key="access_request",
                name="Access request",
                type="request",
                requires_approval=True,
                approver_role_id=role_b_id,
            )
        rows = (
            await session.execute(
                select(TicketCategory).where(
                    TicketCategory.org_id == org_a, TicketCategory.key == "access_request"
                )
            )
        ).scalars().all()
    assert rows == []


async def test_update_category_rejects_cross_org_default_queue_id(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        category_a = TicketCategory(
            org_id=org_a, key="hardware", name="Hardware", type="request", created_by=user_a
        )
        queue_b = TicketQueue(org_id=org_b, key="ops", name="Org B Ops", created_by=user_b)
        session.add_all([category_a, queue_b])
        await session.commit()
        category_a_id = category_a.id
        queue_b_id = queue_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(TicketNotFoundError):
            await update_category(
                session, org_a, user_a, category_a_id, {"default_queue_id": queue_b_id}
            )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        reloaded = (
            await session.execute(
                select(TicketCategory).where(TicketCategory.id == category_a_id)
            )
        ).scalar_one()
    assert reloaded.default_queue_id is None


async def test_update_category_rejects_cross_org_approver_role_id(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, _user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        category_a = TicketCategory(
            org_id=org_a,
            key="access_request",
            name="Access request",
            type="request",
            requires_approval=True,
            created_by=user_a,
        )
        role_b = Role(org_id=org_b, name=f"org-b-approver-{org_b.hex[:8]}")
        session.add_all([category_a, role_b])
        await session.commit()
        category_a_id = category_a.id
        role_b_id = role_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(TicketNotFoundError):
            await update_category(
                session, org_a, user_a, category_a_id, {"approver_role_id": role_b_id}
            )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        reloaded = (
            await session.execute(
                select(TicketCategory).where(TicketCategory.id == category_a_id)
            )
        ).scalar_one()
    assert reloaded.approver_role_id is None


async def test_update_category_never_touches_key_or_type(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        category = await create_category(
            session, org_id, user_id, key="hardware", name="Hardware", type="request"
        )
        updated = await update_category(
            session, org_id, user_id, category.id, {"key": "renamed", "type": "incident"}
        )
    assert updated.key == "hardware"
    assert updated.type == "request"


async def test_update_category_rejects_null_name(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        category = await create_category(
            session, org_id, user_id, key="hardware", name="Hardware", type="request"
        )
        with pytest.raises(TicketValidationError):
            await update_category(session, org_id, user_id, category.id, {"name": None})


async def test_update_category_reports_only_changed_field_names(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        category = await create_category(
            session, org_id, user_id, key="hardware", name="Hardware", type="request"
        )
        await update_category(
            session, org_id, user_id, category.id, {"name": "Hardware", "sort_order": 5}
        )
        event = (
            await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "ticket.category_updated",
                )
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    # B2 (whole-branch review, round B): attribution was never asserted here.
    assert event.actor_type == "user"
    assert event.actor_id == user_id
    assert event.meta["changed_fields"] == ["sort_order"]


async def test_list_categories_excludes_inactive_by_default_and_is_ordered(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        z_cat = await create_category(
            session, org_id, user_id, key="zulu", name="Zulu", type="request"
        )
        a_cat = await create_category(
            session, org_id, user_id, key="alpha", name="Alpha", type="request"
        )
        await update_category(session, org_id, user_id, z_cat.id, {"active": False})

        active_only = await list_categories(session, org_id)
        with_inactive = await list_categories(session, org_id, include_inactive=True)

    assert z_cat.id not in {c.id for c in active_only}
    assert a_cat.id in {c.id for c in active_only}
    assert z_cat.id in {c.id for c in with_inactive}
    names_with_inactive = [c.name for c in with_inactive]
    assert names_with_inactive.index("Alpha") < names_with_inactive.index("Zulu")
