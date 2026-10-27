"""Tests for the Subscriptions seat-assignment service (docs/modules/
subscriptions/spec.md §4: pool-enforced seat assignment to free-form people,
with release).

Mirrors `tests/test_subscriptions_service.py`'s fixture style (throwaway org +
user seeded via `admin_engine`, service calls through an RLS-constrained
`org_session`, audit rows read back by `target_type`/`target_id`) plus the
Phase-10/11 load-bearing pattern for the org-scoped seat load.
"""

import asyncio
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Organization, User
from app.modules.subscriptions.errors import (
    SubscriptionNotFoundError,
    SubscriptionValidationError,
)
from app.modules.subscriptions.service import (
    assign_seat,
    available_seats,
    create_subscription,
    get_subscription,
    list_events,
    list_seats,
    release_seat,
)


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"seatsvc-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


def _create_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "vendor_id": None,
        "service_name": "Acme SaaS",
        "plan": "Pro",
        "category": "productivity",
        "billing_cycle": "monthly",
        "cost_per_cycle": Decimal("100.00"),
        "currency": "USD",
        "seats": 2,
        "owner_user_id": None,
        "started_at": None,
        "renews_at": None,
        "auto_renew": False,
        "notice_days": 30,
        "payment_ref": None,
        "license_ref": None,
        "status": "active",
    }
    kwargs.update(overrides)
    return kwargs


async def _audit_actions(
    app_engine: AsyncEngine, org_id: UUID, target_type: str, target_id: UUID
) -> list[str]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == target_type,
                    AuditEvent.target_id == str(target_id),
                )
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


async def _audit_meta(
    app_engine: AsyncEngine, org_id: UUID, target_type: str, target_id: UUID, action: str
) -> dict[str, Any]:
    """The `meta` payload of the single audit row for `action` on this target."""
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == target_type,
                    AuditEvent.target_id == str(target_id),
                    AuditEvent.action == action,
                )
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        assert len(events) == 1, f"expected exactly one {action} row, got {len(events)}"
        meta: dict[str, Any] = events[0].meta
        return meta


async def _make_subscription(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    org_id: UUID,
    actor_id: UUID,
    **overrides: Any,
) -> UUID:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        sub = await create_subscription(
            s, org_id=org_id, client_id=org_id, actor_id=actor_id, **_create_kwargs(**overrides)
        )
        await s.commit()
        return sub.id


# ---------------------------------------------------------------------------
# assign_seat / pool enforcement
# ---------------------------------------------------------------------------


async def test_assign_seat_up_to_pool_then_exhausted_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_id, actor_id, seats=2)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        seat1 = await assign_seat(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )
        seat2 = await assign_seat(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Bob",
            assignee_email="bob@example.com",
        )
        await s.commit()
        assert seat1.assignee_email == "alice@example.com"
        assert seat2.assignee_email == "bob@example.com"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(SubscriptionValidationError, match="no seats available"):
            await assign_seat(
                s,
                org_id=org_id,
                actor_id=actor_id,
                sub_id=sub_id,
                assignee_name="Carol",
                assignee_email="carol@example.com",
            )


async def test_assign_seat_missing_subscription_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await assign_seat(
                s,
                org_id=org_id,
                actor_id=actor_id,
                sub_id=uuid4(),
                assignee_name="Alice",
                assignee_email="alice@example.com",
            )


async def test_assign_seat_cross_org_subscription_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_a, actor_a)

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await assign_seat(
                s,
                org_id=org_b,
                actor_id=actor_b,
                sub_id=sub_id,
                assignee_name="Alice",
                assignee_email="alice@example.com",
            )


async def test_assign_seat_cross_org_subscription_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Isolate the explicit `Subscription.org_id == org_id` predicate in
    `_load_subscription` so it is the SOLE tenant guard, not RLS.

    Runs `assign_seat` directly on an RLS-bypassing `admin_engine` session
    (mirrors `tests/test_subscriptions_service.py`'s
    `test_create_subscription_cross_ref_org_filter_is_load_bearing` pattern).
    If the `org_id` predicate were removed, the foreign-org subscription
    would resolve and this test would fail (no exception raised).
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_a, actor_a)

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await assign_seat(
                s,
                org_id=org_b,
                actor_id=actor_b,
                sub_id=sub_id,
                assignee_name="Alice",
                assignee_email="alice@example.com",
            )


async def test_assign_seat_concurrent_calls_cannot_exceed_pool(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`assign_seat`'s capacity check (`active >= sub.seats`) is NOT backed
    by any unique index -- a seat pool's SIZE is not a uniqueness fact, so
    `ON CONFLICT DO NOTHING` (the fix for a true duplicate-key race, e.g.
    `budget.rates.create_rate`) would do nothing here: two concurrent
    callers can both read `active < seats` as true before either has
    inserted its `SeatAssignment`, and both would then insert, overfilling
    the pool by one.

    The fix is a `SELECT ... FOR UPDATE` on the subscription row, taken
    before the count is read (see `assign_seat`'s docstring): it serializes
    concurrent callers for the SAME subscription, so a second call blocks
    until the first commits (or rolls back), and only then re-reads the
    count.

    A bare `asyncio.gather` of two attempts does NOT reliably exercise this
    -- exactly like `test_get_settings_concurrent_first_calls_do_not_race`'s
    docstring in `test_budget_fx.py` warns, connection acquisition staggers
    the two calls enough that each one's SELECT already sees the other's
    committed row, so a plain gather passes even with the `FOR UPDATE`
    removed (verified by hand while writing this test). Proving the row
    lock actually blocks needs a deterministic technique of its own:
    session A takes the single seat and DELIBERATELY keeps its transaction
    open; session B's `assign_seat` is started concurrently as a real
    background task on its own connection, and is asserted to still be
    unfinished after a generous timeout -- i.e. genuinely blocked on A's
    row lock, not just slow. Only once that is proven does A commit,
    unblocking B, which must then see the seat already taken and raise the
    typed error rather than a raw `IntegrityError`."""
    org_id, actor_id = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_id, actor_id, seats=1)

    outcome: dict[str, str | None] = {}

    async def attempt_b() -> None:
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session_b:
            try:
                await assign_seat(
                    session_b,
                    org_id=org_id,
                    actor_id=actor_id,
                    sub_id=sub_id,
                    assignee_name="Bob",
                    assignee_email="bob@example.com",
                )
            except SubscriptionValidationError as exc:
                outcome["error"] = str(exc)
            else:
                outcome["error"] = None
                await session_b.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session_a:
        # Session A takes the pool's only seat. Its transaction (and the
        # `FOR UPDATE` lock it holds on the subscription row) stays open
        # until the explicit commit below.
        await assign_seat(
            session_a,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )

        task_b = asyncio.create_task(attempt_b())

        # Prove B is genuinely blocked on the row lock -- not merely slow --
        # by requiring it to still be unfinished after a generous timeout
        # while A's transaction is deliberately still open. `shield` stops
        # the timeout from cancelling B's task; it keeps running in the
        # background, still parked on Postgres's lock wait.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task_b), timeout=1.0)
        assert not task_b.done()

        # Release the lock: B's blocked `SELECT ... FOR UPDATE` can now
        # proceed and will see Alice's committed seat.
        await session_a.commit()

    await asyncio.wait_for(task_b, timeout=5.0)

    # B lost the race: the typed error, never a raw IntegrityError.
    assert outcome["error"] == "no seats available"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        sub = await get_subscription(s, org_id, sub_id)
        assert sub is not None
        # The pool was never overfilled: exactly one seat is active, not two.
        assert await available_seats(s, org_id, sub) == 0
        assert len(await list_seats(s, org_id, sub_id)) == 1


# ---------------------------------------------------------------------------
# available_seats
# ---------------------------------------------------------------------------


async def test_available_seats_decreases_on_assign_and_recovers_on_release(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_id, actor_id, seats=2)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        sub = await get_subscription(s, org_id, sub_id)
        assert sub is not None
        assert await available_seats(s, org_id, sub) == 2

        seat1 = await assign_seat(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )
        assert await available_seats(s, org_id, sub) == 1

        await assign_seat(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Bob",
            assignee_email="bob@example.com",
        )
        assert await available_seats(s, org_id, sub) == 0
        await s.commit()
        seat1_id = seat1.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        sub = await get_subscription(s, org_id, sub_id)
        assert sub is not None
        await release_seat(s, org_id=org_id, actor_id=actor_id, assignment_id=seat1_id)
        assert await available_seats(s, org_id, sub) == 1
        await s.commit()

    # a new assign now succeeds again, filling the freed slot
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        seat3 = await assign_seat(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Carol",
            assignee_email="carol@example.com",
        )
        await s.commit()
        assert seat3.assignee_email == "carol@example.com"


# ---------------------------------------------------------------------------
# release_seat
# ---------------------------------------------------------------------------


async def test_release_seat_twice_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_id, actor_id, seats=2)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        seat = await assign_seat(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )
        await s.commit()
        seat_id = seat.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await release_seat(s, org_id=org_id, actor_id=actor_id, assignment_id=seat_id)
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(SubscriptionValidationError, match="already released"):
            await release_seat(s, org_id=org_id, actor_id=actor_id, assignment_id=seat_id)


async def test_release_seat_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await release_seat(s, org_id=org_id, actor_id=actor_id, assignment_id=uuid4())


async def test_release_seat_cross_org_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_a, actor_a)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        seat = await assign_seat(
            s,
            org_id=org_a,
            actor_id=actor_a,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )
        await s.commit()
        seat_id = seat.id

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await release_seat(s, org_id=org_b, actor_id=actor_b, assignment_id=seat_id)


async def test_release_seat_cross_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Isolate the explicit `SeatAssignment.org_id == org_id` predicate in
    `_load_seat` so it is the SOLE tenant guard, not RLS -- runs `release_seat`
    directly on an RLS-bypassing `admin_engine` session."""
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_a, actor_a)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        seat = await assign_seat(
            s,
            org_id=org_a,
            actor_id=actor_a,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )
        await s.commit()
        seat_id = seat.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await release_seat(s, org_id=org_b, actor_id=actor_b, assignment_id=seat_id)


# ---------------------------------------------------------------------------
# events + audit
# ---------------------------------------------------------------------------


async def test_assign_and_release_record_seat_change_events_and_audit(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_id, actor_id, seats=2)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        seat = await assign_seat(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )
        await s.commit()
        seat_id = seat.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = await list_events(s, org_id, sub_id)
        types = [e.type for e in events]
        assert types == ["created", "seat_change"]
        assert events[1].details == {"action": "assigned", "email": "alice@example.com"}

    seat_actions = await _audit_actions(app_engine, org_id, "seat_assignment", seat_id)
    assert seat_actions == ["subscription.seat_assigned"]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await release_seat(s, org_id=org_id, actor_id=actor_id, assignment_id=seat_id)
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = await list_events(s, org_id, sub_id)
        types = [e.type for e in events]
        assert types == ["created", "seat_change", "seat_change"]
        assert events[2].details == {"action": "released", "email": "alice@example.com"}

    seat_actions = await _audit_actions(app_engine, org_id, "seat_assignment", seat_id)
    assert seat_actions == ["subscription.seat_assigned", "subscription.seat_released"]


async def test_seat_assign_and_release_audit_metadata_are_aligned(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`subscription.seat_released` must carry the same identifying metadata as
    `subscription.seat_assigned` (M1): a row saying only "user U released seat
    S" leaves no trace of WHICH subscription or WHOSE seat."""
    org_id, actor_id = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_id, actor_id, seats=2)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        seat = await assign_seat(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )
        await s.commit()
        seat_id = seat.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await release_seat(s, org_id=org_id, actor_id=actor_id, assignment_id=seat_id)
        await s.commit()

    expected = {"subscription_id": str(sub_id), "email": "alice@example.com"}
    assert (
        await _audit_meta(
            app_engine, org_id, "seat_assignment", seat_id, "subscription.seat_assigned"
        )
        == expected
    )
    assert (
        await _audit_meta(
            app_engine, org_id, "seat_assignment", seat_id, "subscription.seat_released"
        )
        == expected
    )


async def test_list_seats_and_available_seats_write_no_audit(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_id, actor_id, seats=2)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        seat = await assign_seat(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )
        await s.commit()
        seat_id = seat.id

    before = await _audit_actions(app_engine, org_id, "seat_assignment", seat_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        sub = await get_subscription(s, org_id, sub_id)
        assert sub is not None
        seats = await list_seats(s, org_id, sub_id)
        assert len(seats) == 1
        await available_seats(s, org_id, sub)

    after = await _audit_actions(app_engine, org_id, "seat_assignment", seat_id)
    assert after == before


async def test_list_seats_is_empty_for_another_orgs_subscription_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, _actor_b = await _org_with_user(admin_engine)
    sub_id = await _make_subscription(admin_engine, app_engine, org_a, actor_a)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        await assign_seat(
            s,
            org_id=org_a,
            actor_id=actor_a,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )
        await s.commit()

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        seats = await list_seats(s, org_b, sub_id)
        assert seats == []
