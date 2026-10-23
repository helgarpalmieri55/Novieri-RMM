"""Subscriptions REST API (docs/modules/subscriptions/spec.md §9).

Routes are all `/subscriptions`-prefixed (plus the `/seats/{id}/release`
sibling of the seat-assignment sub-resource, mirroring `licenses/api.py`'s
`/allocations/{id}/release`); ``app.modules.subscriptions.register`` mounts
this router under ``/api/v1``. Every route is gated server-side by
``require_permission`` (invariant #6, the frontend never gates security);
``org_id``/``actor_id`` always come from ``AuthContext``, never the request
body/path (invariant #5).

Like ``licenses``/``bcp``/``change_mgmt``, this module dispatches no agent
jobs -- ``register(app)`` mounts this router only, there is no job-type
registration and no gateway edit (spec §10: control-plane only, no worker).

Every ``SubscriptionValidationError`` raised by the service layer is
translated to an HTTP 4xx here -- it must never surface as a 500, and the
translation is by TYPE, not by matching exception-message text.
``SubscriptionNotFoundError`` (a ``SubscriptionValidationError`` subclass,
raised for a missing subscription/seat-assignment) becomes a 404; every other
``SubscriptionValidationError`` (an invalid billing_cycle/status, a
cross-org/nonexistent ``vendor_id``/``owner_user_id``/``license_ref``, an
exhausted seat pool, an already-cancelled subscription, an already-released
seat, a renewal with no ``renews_at``) becomes a 400.

``GET /subscriptions/export`` is this module's one audited read: it writes
``subscription.export`` before returning, mirroring ``licenses``'s
``license.export`` precedent. Every other read here (list, detail, events,
seats list, spend, renewals, idle-seats) records no audit event at all --
merely viewing must never be indistinguishable from exporting (the Phase-9
no-audit-on-view lesson); the service layer's own mutations
(create/update/cancel/renew/assign/release) already audit themselves, so
routes never double-audit those either.

Route registration order matters: the static ``/subscriptions/...`` report
segments (``spend``, ``renewals``, ``idle-seats``, ``export``) are declared
before ``GET|PATCH /subscriptions/{subscription_id}``, so FastAPI never tries
to parse one of those literal segments as a subscription id.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import SeatAssignment, Subscription, SubscriptionEvent
from app.modules.subscriptions.errors import (
    SubscriptionNotFoundError,
    SubscriptionValidationError,
)
from app.modules.subscriptions.reporting import (
    export_rows,
    idle_seats,
    renewal_calendar,
    spend_summary,
)
from app.modules.subscriptions.schema import (
    EventOut,
    IdleSeatRow,
    RenewalRow,
    SeatAssignCreate,
    SeatAssignmentOut,
    SpendRow,
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionUpdate,
)
from app.modules.subscriptions.service import (
    assign_seat,
    cancel_subscription,
    create_subscription,
    get_subscription,
    list_events,
    list_seats,
    list_subscriptions,
    record_renewal,
    release_seat,
    update_subscription,
)

router = APIRouter(tags=["subscriptions"])


def _http_error(exc: SubscriptionValidationError) -> HTTPException:
    """Translate a `SubscriptionValidationError` to an HTTP 4xx -- it must
    never become a 500.

    Routing is by TYPE, never by matching exception-message prose (a wording
    tweak in the service layer must never silently mis-route a status code).
    `SubscriptionNotFoundError` -- raised for "the resource named in the URL
    path doesn't exist under this org" (a subscription, a seat assignment) --
    becomes a 404. Every other `SubscriptionValidationError` is a 400 --
    including an invalid billing_cycle/status, a cross-org/nonexistent
    `vendor_id`/`owner_user_id`/`license_ref`, an exhausted seat pool, an
    already-cancelled subscription, an already-released seat, and a renewal
    attempted with no `renews_at`.
    """
    if isinstance(exc, SubscriptionNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _subscription_out(row: Subscription) -> SubscriptionOut:
    # No `org_id` field here -- security invariant #5: response models never
    # leak the tenant id.
    return SubscriptionOut(
        id=row.id,
        vendor_id=row.vendor_id,
        service_name=row.service_name,
        plan=row.plan,
        category=row.category,
        billing_cycle=row.billing_cycle,
        cost_per_cycle=row.cost_per_cycle,
        currency=row.currency,
        seats=row.seats,
        owner_user_id=row.owner_user_id,
        started_at=row.started_at,
        renews_at=row.renews_at,
        auto_renew=row.auto_renew,
        notice_days=row.notice_days,
        payment_ref=row.payment_ref,
        license_ref=row.license_ref,
        status=row.status,
        created_at=row.created_at,
    )


def _seat_out(row: SeatAssignment) -> SeatAssignmentOut:
    return SeatAssignmentOut(
        id=row.id,
        subscription_id=row.subscription_id,
        assignee_name=row.assignee_name,
        assignee_email=row.assignee_email,
        person_id=row.person_id,
        assigned_at=row.assigned_at,
        released_at=row.released_at,
    )


def _event_out(row: SubscriptionEvent) -> EventOut:
    return EventOut(
        id=row.id,
        subscription_id=row.subscription_id,
        type=row.type,
        details=row.details,
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------- #
# GET /subscriptions/{spend,renewals,idle-seats,export} -- static, declared
# before GET|PATCH /subscriptions/{subscription_id}.
# --------------------------------------------------------------------------- #


@router.get("/subscriptions/spend", response_model=dict[str, list[SpendRow]])
async def spend_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.read")),
) -> dict[str, list[dict[str, Any]]]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        return await spend_summary(session, ctx.org_id)


@router.get("/subscriptions/renewals", response_model=list[RenewalRow])
async def renewals_route(
    request: Request,
    within_days: int = Query(default=30, ge=1, le=365),
    ctx: AuthContext = Depends(require_permission("subscriptions.read")),
) -> list[dict[str, Any]]:
    """Spec §7's `?within_days=N` look-ahead window (default 30).

    Bounded `[1, 365]` so an out-of-range value is a clean 422 rather than a
    silently absurd window; mirrors `licenses/api.py::renewals_route`.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        return await renewal_calendar(
            session, ctx.org_id, now=datetime.now(UTC), within_days=within_days
        )


@router.get("/subscriptions/idle-seats", response_model=list[IdleSeatRow])
async def idle_seats_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.read")),
) -> list[dict[str, Any]]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        return await idle_seats(session, ctx.org_id)


@router.get("/subscriptions/export")
async def export_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.read")),
) -> list[dict[str, Any]]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await export_rows(session, ctx.org_id)

        # An explicit, audited export (invariant #4) -- the ONLY report route
        # in this module that audits; plain list/detail/events/seats/spend/
        # renewal/idle-seat reads write nothing (the Phase-9 no-audit-on-view
        # lesson).
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="subscription.export",
            target_type="subscription_export",
            target_id=str(ctx.org_id),
            metadata={"row_count": len(rows)},
        )

        return rows


# --------------------------------------------------------------------------- #
# GET|POST /subscriptions
# --------------------------------------------------------------------------- #


@router.get("/subscriptions", response_model=list[SubscriptionOut])
async def list_subscriptions_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.read")),
) -> list[SubscriptionOut]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await list_subscriptions(session, ctx.org_id)
        return [_subscription_out(r) for r in rows]


@router.post(
    "/subscriptions", status_code=status.HTTP_201_CREATED, response_model=SubscriptionOut
)
async def create_subscription_route(
    body: SubscriptionCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.manage")),
) -> SubscriptionOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            sub = await create_subscription(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                vendor_id=body.vendor_id,
                service_name=body.service_name,
                plan=body.plan,
                category=body.category,
                billing_cycle=body.billing_cycle,
                cost_per_cycle=body.cost_per_cycle,
                currency=body.currency,
                seats=body.seats,
                owner_user_id=body.owner_user_id,
                started_at=body.started_at,
                renews_at=body.renews_at,
                auto_renew=body.auto_renew,
                notice_days=body.notice_days,
                payment_ref=body.payment_ref,
                license_ref=body.license_ref,
                status=body.status,
            )
        except SubscriptionValidationError as exc:
            raise _http_error(exc) from None
        return _subscription_out(sub)


# --------------------------------------------------------------------------- #
# GET|PATCH /subscriptions/{subscription_id}
# --------------------------------------------------------------------------- #


@router.get("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
async def get_subscription_route(
    subscription_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.read")),
) -> SubscriptionOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        sub = await get_subscription(session, ctx.org_id, subscription_id)
        if sub is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such subscription"))
        return _subscription_out(sub)


@router.patch("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
async def update_subscription_route(
    subscription_id: UUID,
    body: SubscriptionUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.manage")),
) -> SubscriptionOut:
    # `exclude_unset=True` is load-bearing, not a micro-optimization: without
    # it every optional key is present with a `None` default, so "field
    # omitted" and "field explicitly set to null" are indistinguishable and
    # clearing a nullable column (`renews_at`, `vendor_id`, ...) is
    # unreachable. The service layer relies on key PRESENCE to decide what to
    # apply, and rejects an explicit `null` on a NOT NULL column with
    # `SubscriptionValidationError` -> 400 (never an IntegrityError -> 500).
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            sub = await update_subscription(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                sub_id=subscription_id,
                **body.model_dump(exclude_unset=True),
            )
        except SubscriptionValidationError as exc:
            raise _http_error(exc) from None
        return _subscription_out(sub)


# --------------------------------------------------------------------------- #
# POST /subscriptions/{subscription_id}/cancel, /renew
# --------------------------------------------------------------------------- #


@router.post("/subscriptions/{subscription_id}/cancel", response_model=SubscriptionOut)
async def cancel_subscription_route(
    subscription_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.manage")),
) -> SubscriptionOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            sub = await cancel_subscription(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, sub_id=subscription_id
            )
        except SubscriptionValidationError as exc:
            raise _http_error(exc) from None
        return _subscription_out(sub)


@router.post("/subscriptions/{subscription_id}/renew", response_model=SubscriptionOut)
async def renew_subscription_route(
    subscription_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.manage")),
) -> SubscriptionOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            sub = await record_renewal(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, sub_id=subscription_id
            )
        except SubscriptionValidationError as exc:
            raise _http_error(exc) from None
        return _subscription_out(sub)


# --------------------------------------------------------------------------- #
# GET /subscriptions/{subscription_id}/events
# --------------------------------------------------------------------------- #


@router.get("/subscriptions/{subscription_id}/events", response_model=list[EventOut])
async def list_events_route(
    subscription_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.read")),
) -> list[EventOut]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        sub = await get_subscription(session, ctx.org_id, subscription_id)
        if sub is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such subscription"))
        rows = await list_events(session, ctx.org_id, subscription_id)
        return [_event_out(r) for r in rows]


# --------------------------------------------------------------------------- #
# GET|POST /subscriptions/{subscription_id}/seats, POST /seats/{id}/release
# --------------------------------------------------------------------------- #


@router.get("/subscriptions/{subscription_id}/seats", response_model=list[SeatAssignmentOut])
async def list_seats_route(
    subscription_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.read")),
) -> list[SeatAssignmentOut]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        sub = await get_subscription(session, ctx.org_id, subscription_id)
        if sub is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such subscription"))
        rows = await list_seats(session, ctx.org_id, subscription_id)
        return [_seat_out(r) for r in rows]


@router.post(
    "/subscriptions/{subscription_id}/seats",
    status_code=status.HTTP_201_CREATED,
    response_model=SeatAssignmentOut,
)
async def assign_seat_route(
    subscription_id: UUID,
    body: SeatAssignCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.manage")),
) -> SeatAssignmentOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            seat = await assign_seat(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                sub_id=subscription_id,
                assignee_name=body.assignee_name,
                assignee_email=body.assignee_email,
                person_id=body.person_id,
            )
        except SubscriptionValidationError as exc:
            raise _http_error(exc) from None
        return _seat_out(seat)


@router.post("/seats/{seat_id}/release", response_model=SeatAssignmentOut)
async def release_seat_route(
    seat_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("subscriptions.manage")),
) -> SeatAssignmentOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            seat = await release_seat(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, assignment_id=seat_id
            )
        except SubscriptionValidationError as exc:
            raise _http_error(exc) from None
        return _seat_out(seat)
