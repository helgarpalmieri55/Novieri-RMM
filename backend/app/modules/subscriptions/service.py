"""Subscriptions service (docs/modules/subscriptions/spec.md §3 lifecycle &
change history, §6 hybrid license link, §8 invariants): CRUD for the
`subscription` entity plus its immutable business change-history trail.

Mirrors `app.modules.licenses.service`'s org-scoped-load + audit + flush
style: every query in this module carries an explicit `org_id` predicate
(invariant #5) so a subscription/vendor/user/license id from another org is
never reachable through any function here, and every mutation is followed by
`audit_service.record` + a flush (invariant #4). `subscription_event` rows are
business change-history surfaced in the UI -- distinct from the audit log --
so `_record_event` deliberately does NOT call `audit_service.record`.

A subscription belongs to ONE client (plan 2026-10-26 Task 6, migration
0236): the one its creator names, loaded like every new row's
(`client_for_new_rows`). Its seats are held by that client's people, and the
license it refers to is that client's too.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import (
    License,
    Person,
    SeatAssignment,
    Subscription,
    SubscriptionEvent,
    User,
    Vendor,
)
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.people.service import person_id_for_email
from app.modules.subscriptions.costs import advance_renewal
from app.modules.subscriptions.errors import (
    SubscriptionClientError,
    SubscriptionConflictError,
    SubscriptionNotFoundError,
    SubscriptionValidationError,
)
from app.services.client_sites import ClientNotFound, NotAcceptingNewRows, client_for_new_rows

_VALID_STATUSES = {"active", "trial", "cancelled"}
_VALID_CYCLES = {"monthly", "quarterly", "annual"}

# Fields `update_subscription` is allowed to apply as plain scalar
# assignment. `vendor_id`/`owner_user_id`/`license_ref` are also listed here
# so they get applied via the same loop once re-validated org-scoped above.
_SUBSCRIPTION_EDITABLE_FIELDS = (
    "vendor_id",
    "service_name",
    "plan",
    "category",
    "billing_cycle",
    "cost_per_cycle",
    "currency",
    "seats",
    "owner_user_id",
    "started_at",
    "renews_at",
    "auto_renew",
    "notice_days",
    "payment_ref",
    "license_ref",
    "status",
)

# The editable fields backed by a NOT NULL column on `models.Subscription`.
# `update_subscription` applies a supplied `None` (an explicit `null` in the
# PATCH body) as a clear, so a `None` for one of these would reach Postgres
# as a NOT NULL violation and surface as a 500; they are rejected up front as
# a `SubscriptionValidationError` -> 400 instead. The complement -- the six
# NULLable columns `vendor_id`, `owner_user_id`, `started_at`, `renews_at`,
# `payment_ref`, `license_ref` -- IS clearable, which is the whole point of
# distinguishing "omitted" from "explicitly null".
_NON_NULLABLE_EDITABLE_FIELDS = frozenset(
    {
        "service_name",
        "plan",
        "category",
        "billing_cycle",
        "cost_per_cycle",
        "currency",
        "seats",
        "auto_renew",
        "notice_days",
        "status",
    }
)


async def _load_subscription(
    session: AsyncSession, org_id: UUID, sub_id: UUID
) -> Subscription:
    sub = (
        await session.execute(
            select(Subscription).where(
                Subscription.org_id == org_id, Subscription.id == sub_id
            )
        )
    ).scalar_one_or_none()
    if sub is None:
        raise SubscriptionNotFoundError(tr("subscription {sub_id} not found", sub_id=sub_id))
    return sub


async def _client_for_new_subscription(
    session: AsyncSession, org_id: UUID, client_id: UUID
) -> None:
    """The client a new subscription is filed under, loaded as every new row's
    is (`client_for_new_rows`, held FOR SHARE until commit): one this session
    cannot see is a 404, never a 403 (spec §3.2's existence oracle), an
    archived one a 409. "Sin asignar" is the inbox of what has no owner, and
    nobody subscribes there (C10): 422."""
    try:
        client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
    except ClientNotFound:
        raise SubscriptionNotFoundError(tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise SubscriptionConflictError(exc.detail) from None
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise SubscriptionClientError(tr("a subscription cannot be filed under Unassigned"))


async def _validate_refs(
    session: AsyncSession,
    org_id: UUID,
    *,
    client_id: UUID,
    vendor_id: UUID | None,
    owner_user_id: UUID | None,
    license_ref: UUID | None,
) -> None:
    """Resolve each non-None cross-ref org-scoped; a cross-org/unknown id is
    indistinguishable from nonexistent and both raise
    `SubscriptionValidationError` (invariant #5).

    `license_ref` stays advisory -- no foreign key, so deleting the license
    leaves it dangling (spec §143-148) -- but it names a license of the
    subscription's own client (`client_id`): another client's is a
    `SubscriptionClientError` (422). Vendors are the organisation's catalogue,
    of no client. An owner is any user of the organisation this session can
    see (`users` is O: technicians, and the contacts of the clients in its
    scope); neither its kind nor its client is checked, so under a scope of
    several clients a contact of another client than the subscription's is
    accepted as its owner."""
    if vendor_id is not None:
        found = (
            await session.execute(
                select(Vendor.id).where(Vendor.org_id == org_id, Vendor.id == vendor_id)
            )
        ).scalar_one_or_none()
        if found is None:
            raise SubscriptionValidationError(
                tr("vendor not found: {vendor_id}", vendor_id=vendor_id)
            )

    if owner_user_id is not None:
        found = (
            await session.execute(
                select(User.id).where(User.org_id == org_id, User.id == owner_user_id)
            )
        ).scalar_one_or_none()
        if found is None:
            raise SubscriptionValidationError(
                tr("owner user not found: {owner_user_id}", owner_user_id=owner_user_id)
            )

    if license_ref is not None:
        license_client = (
            await session.execute(
                select(License.client_id).where(
                    License.org_id == org_id, License.id == license_ref
                )
            )
        ).scalar_one_or_none()
        if license_client is None:
            raise SubscriptionValidationError(
                tr("license not found: {license_ref}", license_ref=license_ref)
            )
        if license_client != client_id:
            raise SubscriptionClientError(tr("that license belongs to another client"))


async def _record_event(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    sub_id: UUID,
    type_: str,
    details: dict[str, Any],
) -> None:
    """Insert a `subscription_event` row. NO audit call here -- this is
    business change-history for the UI, not the tamper-evident audit log."""
    session.add(
        SubscriptionEvent(
            org_id=org_id,
            subscription_id=sub_id,
            type=type_,
            details=details,
            created_by=actor_id,
        )
    )
    await session.flush()


async def create_subscription(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: UUID,
    vendor_id: UUID | None,
    service_name: str,
    plan: str,
    category: str,
    billing_cycle: str,
    cost_per_cycle: Decimal,
    currency: str,
    seats: int,
    owner_user_id: UUID | None,
    started_at: date | None,
    renews_at: date | None,
    auto_renew: bool,
    notice_days: int,
    payment_ref: str | None,
    license_ref: UUID | None,
    status: str,
) -> Subscription:
    """Create a new subscription of `client_id`; record a `created` event and
    audit `subscription.created`."""
    await _client_for_new_subscription(session, org_id, client_id)
    if billing_cycle not in _VALID_CYCLES:
        raise SubscriptionValidationError(
            tr("invalid billing_cycle: {billing_cycle}", billing_cycle=billing_cycle)
        )
    if status not in _VALID_STATUSES:
        raise SubscriptionValidationError(tr("invalid status: {status}", status=status))

    await _validate_refs(
        session,
        org_id,
        client_id=client_id,
        vendor_id=vendor_id,
        owner_user_id=owner_user_id,
        license_ref=license_ref,
    )

    sub = Subscription(
        org_id=org_id,
        client_id=client_id,
        vendor_id=vendor_id,
        service_name=service_name,
        plan=plan,
        category=category,
        billing_cycle=billing_cycle,
        cost_per_cycle=cost_per_cycle,
        currency=currency,
        seats=seats,
        owner_user_id=owner_user_id,
        started_at=started_at,
        renews_at=renews_at,
        auto_renew=auto_renew,
        notice_days=notice_days,
        payment_ref=payment_ref,
        license_ref=license_ref,
        status=status,
        created_by=actor_id,
    )
    session.add(sub)
    await session.flush()

    await _record_event(
        session, org_id, actor_id, sub.id, "created", {"service_name": service_name}
    )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="subscription.created",
        target_type="subscription",
        target_id=str(sub.id),
        metadata={"service_name": service_name},
        client_id=sub.client_id,
    )
    await session.flush()
    return sub


async def update_subscription(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    sub_id: UUID,
    **fields: Any,
) -> Subscription:
    """Apply the SUPPLIED fields to a subscription, org-scoped-loaded.

    Which fields are applied is decided by key PRESENCE in `fields`, never by
    the value being non-None -- the route passes
    `model_dump(exclude_unset=True)`, so a key is present only if the caller
    actually sent it. That is what makes "field omitted" (leave it alone)
    distinguishable from "field explicitly set to null" (clear it): the six
    NULLable columns are clearable, and a `None` for one of the ten NOT NULL
    columns in `_NON_NULLABLE_EDITABLE_FIELDS` is rejected up front as a
    `SubscriptionValidationError` (-> 400) rather than reaching Postgres as a
    NOT NULL violation (-> 500).

    `vendor_id`/`owner_user_id`/`license_ref`, if supplied and not None, are
    re-validated org-scoped before being applied (a supplied `None` is a
    clear, so there is nothing to resolve). `billing_cycle`/`status`, if
    supplied and not None, are re-validated against their allow-lists.
    `seats` may not be reduced below the subscription's current ACTIVE
    seat-assignment count -- otherwise `available_seats` and the idle-seat
    report's `idle` go negative and the spend dashboard renders a negative
    cost.

    Every editable field's value is captured before the update is applied so
    that (a) a material change can be recorded as a `subscription_event`
    (price_change/seat_change/plan_change) and (b) the sorted NAMES of the
    fields that actually changed go into the `subscription.updated` audit
    metadata -- a row saying only "user U updated subscription S" leaves no
    trace of WHAT changed. Names only: sufficient to make the row useful, and
    it sidesteps any value-sensitivity question."""
    sub = await _load_subscription(session, org_id, sub_id)

    for field in _SUBSCRIPTION_EDITABLE_FIELDS:
        if field in fields and fields[field] is None and field in _NON_NULLABLE_EDITABLE_FIELDS:
            raise SubscriptionValidationError(tr("{field} cannot be null", field=field))

    vendor_id = fields.get("vendor_id")
    owner_user_id = fields.get("owner_user_id")
    license_ref = fields.get("license_ref")
    if vendor_id is not None or owner_user_id is not None or license_ref is not None:
        await _validate_refs(
            session,
            org_id,
            client_id=sub.client_id,
            vendor_id=vendor_id,
            owner_user_id=owner_user_id,
            license_ref=license_ref,
        )

    billing_cycle = fields.get("billing_cycle")
    if billing_cycle is not None and billing_cycle not in _VALID_CYCLES:
        raise SubscriptionValidationError(
            tr("invalid billing_cycle: {billing_cycle}", billing_cycle=billing_cycle)
        )

    status = fields.get("status")
    if status is not None and status not in _VALID_STATUSES:
        raise SubscriptionValidationError(tr("invalid status: {status}", status=status))

    seats = fields.get("seats")
    if seats is not None:
        active = await _active_seat_count(session, org_id, sub_id)
        if seats < active:
            raise SubscriptionValidationError(
                tr(
                    "cannot reduce seats to {seats}: {active} seats are currently assigned",
                    seats=seats,
                    active=active,
                )
            )

    old_values = {field: getattr(sub, field) for field in _SUBSCRIPTION_EDITABLE_FIELDS}
    old_cost = old_values["cost_per_cycle"]
    old_seats = old_values["seats"]
    old_plan = old_values["plan"]

    for field in _SUBSCRIPTION_EDITABLE_FIELDS:
        if field not in fields:
            continue
        setattr(sub, field, fields[field])

    await session.flush()

    changed_fields = sorted(
        field
        for field in _SUBSCRIPTION_EDITABLE_FIELDS
        if getattr(sub, field) != old_values[field]
    )

    if sub.cost_per_cycle != old_cost:
        await _record_event(
            session,
            org_id,
            actor_id,
            sub.id,
            "price_change",
            {"from": str(old_cost), "to": str(sub.cost_per_cycle), "currency": sub.currency},
        )
    if sub.seats != old_seats:
        await _record_event(
            session,
            org_id,
            actor_id,
            sub.id,
            "seat_change",
            {"from": old_seats, "to": sub.seats},
        )
    if sub.plan != old_plan:
        await _record_event(
            session,
            org_id,
            actor_id,
            sub.id,
            "plan_change",
            {"from": old_plan, "to": sub.plan},
        )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="subscription.updated",
        target_type="subscription",
        target_id=str(sub.id),
        metadata={"changed_fields": changed_fields},
        client_id=sub.client_id,
    )
    await session.flush()
    return sub


async def get_subscription(
    session: AsyncSession, org_id: UUID, sub_id: UUID
) -> Subscription | None:
    return (
        await session.execute(
            select(Subscription).where(
                Subscription.org_id == org_id, Subscription.id == sub_id
            )
        )
    ).scalar_one_or_none()


async def list_subscriptions(session: AsyncSession, org_id: UUID) -> list[Subscription]:
    return list(
        (
            await session.execute(
                select(Subscription).where(Subscription.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )


async def list_events(
    session: AsyncSession, org_id: UUID, sub_id: UUID
) -> list[SubscriptionEvent]:
    return list(
        (
            await session.execute(
                select(SubscriptionEvent)
                .where(
                    SubscriptionEvent.org_id == org_id,
                    SubscriptionEvent.subscription_id == sub_id,
                )
                .order_by(SubscriptionEvent.created_at)
            )
        )
        .scalars()
        .all()
    )


async def cancel_subscription(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, sub_id: UUID
) -> Subscription:
    """Cancel a subscription (org-scoped-load); record a `cancel` event and
    audit `subscription.cancelled`. Idempotent guard: cancelling an
    already-cancelled subscription is a `SubscriptionValidationError`."""
    sub = await _load_subscription(session, org_id, sub_id)

    if sub.status == "cancelled":
        raise SubscriptionValidationError(tr("subscription already cancelled"))

    sub.status = "cancelled"
    await session.flush()

    await _record_event(session, org_id, actor_id, sub_id, "cancel", {})

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="subscription.cancelled",
        target_type="subscription",
        target_id=str(sub_id),
        metadata={},
        client_id=sub.client_id,
    )
    await session.flush()
    return sub


async def record_renewal(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, sub_id: UUID
) -> Subscription:
    """Manually advance a subscription's `renews_at` by one `billing_cycle`
    (docs/modules/subscriptions/spec.md §3/§5: month-end clamped date
    arithmetic via `costs.advance_renewal`); record a `renewal` event and
    audit `subscription.renewed`."""
    sub = await _load_subscription(session, org_id, sub_id)

    if sub.renews_at is None:
        raise SubscriptionValidationError(tr("subscription has no renewal date"))

    new_date = advance_renewal(sub.renews_at, sub.billing_cycle)
    sub.renews_at = new_date
    await session.flush()

    await _record_event(
        session,
        org_id,
        actor_id,
        sub_id,
        "renewal",
        {"renewed_to": new_date.isoformat()},
    )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="subscription.renewed",
        target_type="subscription",
        target_id=str(sub_id),
        metadata={"renewed_to": new_date.isoformat()},
        client_id=sub.client_id,
    )
    await session.flush()
    return sub


# ---------------------------------------------------------------------------
# seat_assignment (docs/modules/subscriptions/spec.md §4): assigning seats to
# free-form people (name + email, not a `user` row), enforcing the
# subscription's `seats` pool. Mirrors `app.modules.licenses.service`'s
# allocate/release_allocation pattern: an explicit `org_id` predicate on
# every load (invariant #5) so a cross-org subscription/seat id is never
# reachable through any function here.
# ---------------------------------------------------------------------------


async def _active_seat_count(session: AsyncSession, org_id: UUID, sub_id: UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(SeatAssignment)
            .where(
                SeatAssignment.org_id == org_id,
                SeatAssignment.subscription_id == sub_id,
                SeatAssignment.released_at.is_(None),
            )
        )
    ).scalar_one()


async def assign_seat(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    sub_id: UUID,
    assignee_name: str,
    assignee_email: str,
    person_id: UUID | None = None,
) -> SeatAssignment:
    """Assign a seat of `sub_id` to a free-form person, enforcing the pool.

    `sub_id` is resolved org-scoped first (invariant #5). If the
    subscription's active-assignment count has already reached `seats`, the
    pool is exhausted and no row is created.

    The capacity check below is NOT protected by any unique index -- unlike
    a duplicate-key check, "how many active seats exist" has nothing for
    Postgres to arbitrate a conflict on, so `ON CONFLICT DO NOTHING` would
    do nothing to prevent an overfill. Two concurrent callers can both read
    `active < sub.seats` as true before either has inserted its
    `SeatAssignment`, and both would then insert, overfilling the pool by
    one. The fix is a `SELECT ... FOR UPDATE` on the subscription row
    itself: it takes a row lock for the rest of THIS transaction, so a
    second concurrent `assign_seat` call for the same `sub_id` blocks at
    this line until the first commits or rolls back, and then re-runs the
    count against the first call's now-visible row (or its absence, if the
    first rolled back)."""
    sub = await _load_subscription(session, org_id, sub_id)
    await session.execute(
        select(Subscription.id)
        .where(Subscription.org_id == org_id, Subscription.id == sub_id)
        .with_for_update()
    )

    active = await _active_seat_count(session, org_id, sub_id)
    if active >= sub.seats:
        raise SubscriptionValidationError(tr("no seats available"))

    # The seat belongs to a person (docs/modules/people/spec.md §0.4) of the
    # subscription's client: the one named, or else whoever of that client
    # holds this address -- two customers can each employ an ana@gmail.com.
    if person_id is not None:
        person_client = (
            await session.execute(
                select(Person.client_id).where(Person.org_id == org_id, Person.id == person_id)
            )
        ).scalar_one_or_none()
        if person_client is None:
            raise SubscriptionValidationError(tr("person not found"))
        if person_client != sub.client_id:
            raise SubscriptionClientError(tr("that person belongs to another client"))
    else:
        person_id = await person_id_for_email(
            session, org_id, assignee_email, client_id=sub.client_id
        )

    seat = SeatAssignment(
        org_id=org_id,
        subscription_id=sub_id,
        assignee_name=assignee_name,
        assignee_email=assignee_email,
        person_id=person_id,
        created_by=actor_id,
    )
    session.add(seat)
    await session.flush()

    await _record_event(
        session,
        org_id,
        actor_id,
        sub_id,
        "seat_change",
        {"action": "assigned", "email": assignee_email},
    )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="subscription.seat_assigned",
        target_type="seat_assignment",
        target_id=str(seat.id),
        metadata={"subscription_id": str(sub_id), "email": assignee_email},
        client_id=sub.client_id,
    )
    await session.flush()
    return seat


async def _load_seat(
    session: AsyncSession, org_id: UUID, assignment_id: UUID
) -> SeatAssignment:
    seat = (
        await session.execute(
            select(SeatAssignment).where(
                SeatAssignment.org_id == org_id, SeatAssignment.id == assignment_id
            )
        )
    ).scalar_one_or_none()
    if seat is None:
        raise SubscriptionNotFoundError(
            tr("seat assignment {assignment_id} not found", assignment_id=assignment_id)
        )
    return seat


async def release_seat(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, assignment_id: UUID
) -> SeatAssignment:
    """Release a previously granted seat, freeing a slot in the pool.

    Releasing an already-released assignment is a validation error, not a
    silent no-op -- it usually indicates a double-submit or a stale UI state
    the caller should surface."""
    seat = await _load_seat(session, org_id, assignment_id)
    if seat.released_at is not None:
        raise SubscriptionValidationError(tr("seat already released"))
    # Read before the UPDATE: it expires the trigger-kept column.
    client_id = seat.client_id

    seat.released_at = datetime.now(UTC)
    await session.flush()

    await _record_event(
        session,
        org_id,
        actor_id,
        seat.subscription_id,
        "seat_change",
        {"action": "released", "email": seat.assignee_email},
    )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="subscription.seat_released",
        target_type="seat_assignment",
        target_id=str(assignment_id),
        # Aligned with `assign_seat`'s metadata: a release row saying only
        # "user U released seat S" leaves no trace of WHICH subscription or
        # WHOSE seat without joining back to a row that may since have moved.
        metadata={
            "subscription_id": str(seat.subscription_id),
            "email": seat.assignee_email,
        },
        client_id=client_id,
    )
    await session.flush()
    return seat


async def list_seats(
    session: AsyncSession, org_id: UUID, sub_id: UUID
) -> list[SeatAssignment]:
    return list(
        (
            await session.execute(
                select(SeatAssignment).where(
                    SeatAssignment.org_id == org_id,
                    SeatAssignment.subscription_id == sub_id,
                )
            )
        )
        .scalars()
        .all()
    )


async def available_seats(
    session: AsyncSession, org_id: UUID, subscription: Subscription
) -> int:
    active = await _active_seat_count(session, org_id, subscription.id)
    return subscription.seats - active
