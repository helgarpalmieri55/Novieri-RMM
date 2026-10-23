"""Service Desk REST API (docs/modules/service-desk/spec.md §9, §8, §6).

Routes are all `/service-desk`-prefixed; `app.modules.service_desk.register`
mounts this router under `/api/v1`. Every route is gated server-side by
`require_permission` (invariant #6, the frontend never gates security);
`org_id`/`actor_id` always come from `AuthContext`, never the request
body/path (invariant #5).

Like budget/licenses/subscriptions/bcp/change_mgmt, this module dispatches
no agent jobs -- `register(app)` mounts this router only, no job type
registration and no gateway edit (spec §10: control-plane only, no worker).

Every `TicketValidationError` raised by the service layer is translated to
an HTTP 4xx here -- it must never surface as a 500, and the translation is
by TYPE, never by matching exception-message text (`_http_error`, the same
shape as `app.modules.budget.api._http_error`/`app.modules.subscriptions.
api`'s). `TicketNotFoundError` (a `TicketValidationError` subclass, raised
for a missing/cross-org ticket, queue, category, or attachment) becomes a
404; every other `TicketValidationError` -- an invalid transition, a
category/type mismatch, a terminal-ticket edit/comment/attachment, a
self-approval attempt, an already-decided approval, an oversized
attachment, a bad filename -- becomes a 400.

**No double-auditing (invariant #4).** The service layer already audits
every mutation it performs (queue/category create+update, the one-off
`ticket.queues_seeded`/`ticket.categories_seeded` system events, ticket
create+update, status change, assign, approve/reject, comment add,
attachment upload). This file adds exactly ONE audited READ the service
layer has no way to audit itself, because it is a pure route-level concern:
attachment download (`ticket.attachment_downloaded`). `threads.
get_attachment` deliberately does not audit -- that is correct and covered
by that module's own tests -- so this route is the only place that event is
ever written, and it is written exactly once per request. Every other read
-- queue/category list, ticket list/detail, comment list, attachment list,
status-history list -- writes nothing (spec §8.3's no-audit-on-view rule).

**Route-ordering caution (the Phase-11/12 lesson, carried forward as this
task's requirement #4).** `GET /service-desk/attachments/{attachment_id}` --
and every other route with a leading literal path segment -- is declared
before any `/service-desk/tickets/{ticket_id}`-shaped route in this file, so
a reader (and FastAPI's route table) never has to reason about a literal
segment being captured as a path parameter. Queues and categories (no
ticket in their path at all) are declared first, then the standalone
attachment-download route, then the `tickets` collection (list/create, no
id), then every `{ticket_id}`-shaped route, with the comment/attachment
sub-resources declared last.

**Attachment upload ordering (Task 10 review, carried forward as this
task's requirement #1).** `upload_attachment_route` below reads the request
body via `read_bounded_body` ONLY AFTER the ticket has been org-scoped-
loaded and found to exist (`tickets.get_ticket`). `threads.add_attachment`
takes raw `bytes`, which makes reading the body FIRST the more natural (and
wrong) order to write: a `ticket.write` holder could otherwise buffer up to
`MAX_ATTACHMENT_BYTES` per request against a nonexistent or cross-org
ticket id before the 404 is ever raised. `reject_oversized_content_length`
(the declared `Content-Length`, a fast pre-check) and `read_bounded_body`
(the bytes actually read, which a chunked request or a lying header cannot
evade) are both used -- mirroring `app.modules.budget.api.
upload_attachment_route` exactly -- and `MAX_ATTACHMENT_BYTES` is imported
from `app.modules.service_desk.threads` (requirement #2) rather than
redeclared here, so the streaming guard and the service layer's own
defence-in-depth belt can never drift apart.

RBAC (spec §8.2, §9): queue/category/ticket/comment/attachment/status-history
reads -> `ticket.read`; ticket create/comment/attachment-upload, the generic
ticket PATCH + status transition and reply-and-resolve -> `ticket.write`; assign/reassign/move-
queue -> `ticket.assign`; approve/reject -> `ticket.approve`; queue/category
create+update and canned-response create/update/delete -> `ticket.admin`
(listing canned responses needs only `ticket.read`). Technician holds `read`/`write`/`assign`
(spec §0.6, the inverse of Budget) but NOT `approve`/`admin` -- tested by
iterating every route in `tests/test_service_desk_api.py`, mirroring
`tests/test_budget_api.py`'s `declared == covered` derivation from the
router so a route added later without a permission test fails the suite.

**`q` (spec §9's ticket-list filter) is a case-insensitive substring match
over `subject`/`description`, implemented in `tickets.list_tickets` (C1 fix,
whole-branch review round C) and forwarded here exactly like every other
filter.** It was UNDELIVERED SPEC before this fix -- declared in spec §9 and
in `types.ts`, but with no route parameter, no service-layer argument, and no
client method anywhere, invisible to mypy/ruff/the test suite. Bound at this
boundary to `MAX_SEARCH_Q` (`schema.py`), the same schema-boundary discipline
(Phase-11/12 lesson) every other bounded string in this module gets.
"""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.http import read_bounded_body, reject_oversized_content_length
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.ticket_words import status_quoted
from app.models import (
    Ticket,
    TicketAttachment,
    TicketCannedResponse,
    TicketCategory,
    TicketComment,
    TicketQueue,
    TicketStatusHistory,
)
from app.modules.service_desk.approvals import decide_approval
from app.modules.service_desk.canned import (
    MAX_CANNED_RESPONSES,
    create_canned_response,
    delete_canned_response,
    list_canned_responses,
    update_canned_response,
)
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.lifecycle import TERMINAL_STATUSES
from app.modules.service_desk.refdata import (
    create_category,
    create_queue,
    ensure_default_categories,
    ensure_default_queues,
    list_categories,
    list_queues,
    update_category,
    update_queue,
)
from app.modules.service_desk.resolve import reply_and_resolve
from app.modules.service_desk.schema import (
    MAX_SEARCH_Q,
    ApprovalDecision,
    AttachmentOut,
    CannedResponseCreate,
    CannedResponseOut,
    CannedResponseUpdate,
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
    CommentCreate,
    CommentOut,
    QueueCreate,
    QueueOut,
    QueueUpdate,
    StatusHistoryOut,
    TicketAssign,
    TicketCreate,
    TicketListResponse,
    TicketOut,
    TicketResolve,
    TicketSlaSummary,
    TicketStatusChange,
    TicketUpdate,
)
from app.modules.service_desk.threads import (
    MAX_ATTACHMENT_BYTES,
    add_attachment,
    add_comment,
    get_attachment,
    list_comments,
    sanitize_attachment_filename,
)
from app.modules.service_desk.tickets import (
    count_tickets,
    create_ticket,
    get_ticket,
    list_status_history,
    list_tickets,
    update_ticket,
)
from app.modules.service_desk.transitions import assign_ticket, change_status
from app.modules.sla.due import next_due
from app.modules.sla.models import TicketSla
from app.modules.sla.views import target_state, ticket_sla_summaries

router = APIRouter(tags=["service-desk"])


def _http_error(exc: TicketValidationError) -> HTTPException:
    """Translate a `TicketValidationError` to an HTTP 4xx -- it must never
    become a 500.

    Routing is by TYPE, never by matching exception-message prose.
    `TicketNotFoundError` -- raised for "the resource named in the URL path
    doesn't exist under this org" (a ticket, queue, category, or attachment)
    -- becomes a 404. Every other `TicketValidationError` is a 400,
    including an illegal status transition, a category/ticket type
    mismatch, an edit/comment/attachment on a terminal ticket, a
    self-approval attempt, an already-decided approval, an oversized
    attachment, and a rejected filename.
    """
    if isinstance(exc, TicketNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _object_store(request: Request) -> ObjectStore:
    """Lazily build (and cache) the object store on first use -- mirrors
    `app.modules.budget.api._object_store`/`app.modules.backup.api.
    _object_store`/`app.modules.remote.api._object_store`. Construction hits
    the object store to ensure the bucket exists, so it stays out of app
    startup; attachment upload/download is the only service-desk path that
    needs it."""
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(request.app.state.settings)
        request.app.state.object_store = store
    return store


# --------------------------------------------------------------------------- #
# Response-out builders -- no `org_id` field anywhere (invariant #5).
# --------------------------------------------------------------------------- #


def _queue_out(row: TicketQueue) -> QueueOut:
    return QueueOut(
        id=row.id,
        key=row.key,
        name=row.name,
        description=row.description,
        active=row.active,
        sort_order=row.sort_order,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _category_out(row: TicketCategory) -> CategoryOut:
    return CategoryOut(
        id=row.id,
        key=row.key,
        name=row.name,
        type=row.type,
        requires_approval=row.requires_approval,
        approver_role_id=row.approver_role_id,
        default_queue_id=row.default_queue_id,
        active=row.active,
        sort_order=row.sort_order,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _comment_out(row: TicketComment) -> CommentOut:
    return CommentOut(
        id=row.id,
        ticket_id=row.ticket_id,
        author_id=row.author_id,
        author_type=row.author_type,
        body=row.body,
        visibility=row.visibility,
        created_at=row.created_at,
    )


def _attachment_out(row: TicketAttachment) -> AttachmentOut:
    return AttachmentOut(
        id=row.id,
        ticket_id=row.ticket_id,
        filename=row.filename,
        byte_size=row.byte_size,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
    )


def _status_history_out(row: TicketStatusHistory) -> StatusHistoryOut:
    return StatusHistoryOut(
        id=row.id,
        ticket_id=row.ticket_id,
        from_status=row.from_status,
        to_status=row.to_status,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        reason=row.reason,
        created_at=row.created_at,
    )


async def _tickets_out(
    session: AsyncSession, org_id: UUID, tickets: list[Ticket]
) -> list[TicketOut]:
    """Build `TicketOut` rows for `tickets`, resolving `has_attachments` (spec
    §9's schema note: a plain boolean, never the raw `object_key` or a list
    of attachment objects) with ONE batched query over every ticket id in
    the page, rather than one query per row."""
    ticket_ids = [t.id for t in tickets]
    with_attachments: set[UUID] = set()
    if ticket_ids:
        rows = (
            await session.execute(
                select(TicketAttachment.ticket_id)
                .where(
                    TicketAttachment.org_id == org_id,
                    TicketAttachment.ticket_id.in_(ticket_ids),
                )
                .distinct()
            )
        ).scalars().all()
        with_attachments = set(rows)
    clocks = await ticket_sla_summaries(session, org_id, ticket_ids)
    return [
        TicketOut(
            id=t.id,
            number=t.number,
            type=t.type,
            category_id=t.category_id,
            queue_id=t.queue_id,
            status=t.status,
            priority=t.priority,
            subject=t.subject,
            description=t.description,
            requester_id=t.requester_id,
            assigned_to=t.assigned_to,
            device_id=t.device_id,
            origin=t.origin,
            source_alert_id=t.source_alert_id,
            resolved_at=t.resolved_at,
            closed_at=t.closed_at,
            has_attachments=t.id in with_attachments,
            created_by=t.created_by,
            created_at=t.created_at,
            updated_at=t.updated_at,
            sla=_sla_summary(clocks.get(t.id)),
        )
        for t in tickets
    ]


def _sla_summary(row: TicketSla | None) -> TicketSlaSummary | None:
    if row is None:
        return None
    response_state = target_state(row, target="response")
    resolution_state = target_state(row, target="resolution")
    due_at, due_target = next_due(row)
    return TicketSlaSummary(
        response_due_at=row.response_due_at,
        resolution_due_at=row.resolution_due_at,
        response_state=response_state,
        resolution_state=resolution_state,
        next_due_at=due_at,
        next_due_target=due_target,
        breached="breached" in (response_state, resolution_state),
        paused=row.paused_at is not None and row.voided_at is None,
    )


async def _ticket_out(session: AsyncSession, org_id: UUID, ticket: Ticket) -> TicketOut:
    """Build a single `TicketOut`. `ticket.updated_at` is `onupdate=func.now()`
    (`service_desk.models.Ticket`) -- after any UPDATE flush (`update_ticket`/
    `change_status`/`assign_ticket`) SQLAlchemy marks it expired pending a
    server round-trip for the new value, and a synchronous attribute access
    on an expired column outside an explicit `await` raises
    `MissingGreenlet` under the async engine. `session.refresh` re-populates
    every column from a fresh, properly-awaited SELECT before serialization
    -- a harmless extra round-trip for an already-fresh row (`create_ticket`
    populates defaults via INSERT...RETURNING and is never expired), and the
    fix for every row that just went through an UPDATE."""
    await session.refresh(ticket)
    return (await _tickets_out(session, org_id, [ticket]))[0]


# --------------------------------------------------------------------------- #
# GET/POST /service-desk/queues, PATCH /service-desk/queues/{queue_id}
# (spec §2 `ticket_queue`, §5, §9)
# --------------------------------------------------------------------------- #


@router.get("/service-desk/queues", response_model=list[QueueOut])
async def list_queues_route(
    request: Request,
    include_inactive: bool = Query(default=False),
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> list[QueueOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        # `refdata.list_queues` deliberately does not self-seed (it has no
        # `actor_id` to satisfy the seed's NOT NULL `created_by`) -- the
        # caller bootstraps explicitly first, exactly as its docstring
        # prescribes.
        await ensure_default_queues(session, ctx.org_id, ctx.user_id)
        rows = await list_queues(session, ctx.org_id, include_inactive=include_inactive)
        return [_queue_out(r) for r in rows]


@router.post("/service-desk/queues", status_code=status.HTTP_201_CREATED, response_model=QueueOut)
async def create_queue_route(
    body: QueueCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.admin")),
) -> QueueOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            # `sort_order` is passed straight to `create_queue` as an
            # INSERT-time value (whole-branch security review, minor
            # finding) -- a follow-up `update_queue` call here for a
            # non-default value used to write a spurious
            # `ticket.queue_updated` audit event right after
            # `ticket.queue_created` for a single create request.
            queue = await create_queue(
                session,
                ctx.org_id,
                ctx.user_id,
                key=body.key,
                name=body.name,
                description=body.description,
                sort_order=body.sort_order,
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return _queue_out(queue)


@router.patch("/service-desk/queues/{queue_id}", response_model=QueueOut)
async def update_queue_route(
    queue_id: UUID,
    body: QueueUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.admin")),
) -> QueueOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            queue = await update_queue(
                session,
                ctx.org_id,
                ctx.user_id,
                queue_id,
                body.model_dump(exclude_unset=True),
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return _queue_out(queue)


# --------------------------------------------------------------------------- #
# GET/POST /service-desk/categories, PATCH /service-desk/categories/{category_id}
# (spec §2 `ticket_category`, §4, §9)
# --------------------------------------------------------------------------- #


@router.get("/service-desk/categories", response_model=list[CategoryOut])
async def list_categories_route(
    request: Request,
    include_inactive: bool = Query(default=False),
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> list[CategoryOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        await ensure_default_categories(session, ctx.org_id, ctx.user_id)
        rows = await list_categories(session, ctx.org_id, include_inactive=include_inactive)
        return [_category_out(r) for r in rows]


@router.post(
    "/service-desk/categories", status_code=status.HTTP_201_CREATED, response_model=CategoryOut
)
async def create_category_route(
    body: CategoryCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.admin")),
) -> CategoryOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            # `sort_order` goes straight to `create_category` as an
            # INSERT-time value, for the identical reason `create_queue_route`
            # does now (whole-branch security review, minor finding) -- one
            # create request writes exactly one audit event.
            category = await create_category(
                session,
                ctx.org_id,
                ctx.user_id,
                key=body.key,
                name=body.name,
                type=body.type,
                requires_approval=body.requires_approval,
                approver_role_id=body.approver_role_id,
                default_queue_id=body.default_queue_id,
                sort_order=body.sort_order,
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return _category_out(category)


@router.patch("/service-desk/categories/{category_id}", response_model=CategoryOut)
async def update_category_route(
    category_id: UUID,
    body: CategoryUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.admin")),
) -> CategoryOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            category = await update_category(
                session,
                ctx.org_id,
                ctx.user_id,
                category_id,
                body.model_dump(exclude_unset=True),
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return _category_out(category)


# --------------------------------------------------------------------------- #
# GET /service-desk/attachments/{attachment_id} -- declared BEFORE the
# `tickets` collection and every `{ticket_id}`-shaped route (requirement #4,
# module docstring). The one route in this module that carries an explicit
# route-level audit event (requirement #3): `threads.get_attachment` itself
# does not audit -- this is the only place `ticket.attachment_downloaded` is
# ever written, exactly once per request.
# --------------------------------------------------------------------------- #


@router.get("/service-desk/attachments/{attachment_id}")
async def download_attachment_route(
    attachment_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> Response:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            attachment = await get_attachment(session, ctx.org_id, attachment_id)
        except TicketValidationError as exc:
            raise _http_error(exc) from None

        ref = attachment.object_key
        # The header is built from the sanitized STORED filename (never a
        # re-derivation from the object key, which carries no filename
        # component at all -- spec §2) -- this is where the sanitizer's
        # entire reason for existing (a safe `Content-Disposition` header)
        # actually pays off.
        filename = attachment.filename

        # requirement #3: audited HERE, exactly once, never in
        # `threads.get_attachment` (which is deliberately silent -- spec
        # §8.3, "the only audited read is an attachment download").
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="ticket.attachment_downloaded",
            target_type="ticket",
            target_id=str(attachment.ticket_id),
            metadata={"attachment_id": str(attachment.id)},
        )

    # Defence in depth: `get_ticket_attachment` itself asserts `ref` is under
    # this caller's org prefix before it will read it (mirrors
    # `budget.api.download_attachment_route`'s P1(d)).
    data = await _object_store(request).get_ticket_attachment(ref, ctx.org_id)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# GET/POST /service-desk/tickets (spec §2 `ticket`, §5, §9)
# --------------------------------------------------------------------------- #


@router.get("/service-desk/tickets", response_model=TicketListResponse)
async def list_tickets_route(
    request: Request,
    type: str | None = Query(default=None),
    status_: str | None = Query(default=None, alias="status"),
    queue_id: UUID | None = Query(default=None),
    assigned_to: UUID | None = Query(default=None),
    requester_id: UUID | None = Query(default=None),
    device_id: UUID | None = Query(default=None),
    priority: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=MAX_SEARCH_Q),
    scope: Literal["mine", "unassigned", "open", "all"] = Query(default="all"),
    sort: Literal["newest", "due"] = Query(default="newest"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> TicketListResponse:
    """One page of this org's tickets, with the count the page was cut from.

    AN ENVELOPE RATHER THAN A BARE LIST (closeout rec. 10, auditor A). This
    returned `list[TicketOut]` capped at 200, so a console holding a full page
    could not tell a complete queue from a truncated one -- `TicketListPage`
    said exactly that in a comment and refused to render "showing X of Y"
    because there was no Y. `{items, total}` is the shape `GET /jobs`,
    `GET /patch/approvals` and `GET /device-groups` already use, and `total`
    is counted from the same filtered set as the rows, before `limit`.

    `scope` narrows to open tickets that are the caller's (`mine`) or nobody's
    (`unassigned`), or to every open ticket (`open`); "mine" is always the
    authenticated user, never a value from the request. `sort=due` orders by
    the earliest outstanding SLA deadline, tickets without one last. Every
    row carries its SLA summary (`sla`), loaded in one query for the page.
    """
    if scope == "mine":
        if assigned_to is not None and assigned_to != ctx.user_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                tr("scope=mine cannot be combined with another assignee"),
            )
        assigned_to = ctx.user_id
    filters: dict[str, Any] = {
        "type": type,
        "status": status_,
        "queue_id": queue_id,
        "assigned_to": assigned_to,
        "requester_id": requester_id,
        "device_id": device_id,
        "priority": priority,
        "q": q,
        "unassigned": scope == "unassigned",
        "open_only": scope != "all",
    }
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_tickets(
            session, ctx.org_id, sort=sort, limit=limit, offset=offset, **filters
        )
        total = await count_tickets(session, ctx.org_id, **filters)
        return TicketListResponse(
            items=await _tickets_out(session, ctx.org_id, rows), total=total
        )


@router.post(
    "/service-desk/tickets", status_code=status.HTTP_201_CREATED, response_model=TicketOut
)
async def create_ticket_route(
    body: TicketCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.write")),
) -> TicketOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            # `origin` is a keyword decided by THIS ROUTE, never read off the
            # request body -- `TicketCreate` has no `origin` field at all
            # (schema.py's docstring); every ticket created through this
            # technician-facing API is `origin="technician"`.
            ticket = await create_ticket(
                session, ctx.org_id, ctx.user_id, body, origin="technician"
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return await _ticket_out(session, ctx.org_id, ticket)


# --------------------------------------------------------------------------- #
# GET/PATCH /service-desk/tickets/{ticket_id},
# GET /service-desk/tickets/{ticket_id}/history (task 13's G1 fix),
# POST /service-desk/tickets/{ticket_id}/status,
# POST /service-desk/tickets/{ticket_id}/assign,
# POST /service-desk/tickets/{ticket_id}/approve,
# POST /service-desk/tickets/{ticket_id}/reject
# --------------------------------------------------------------------------- #


@router.get("/service-desk/tickets/{ticket_id}", response_model=TicketOut)
async def get_ticket_route(
    ticket_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> TicketOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            ticket = await get_ticket(session, ctx.org_id, ticket_id)
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return await _ticket_out(session, ctx.org_id, ticket)


@router.get(
    "/service-desk/tickets/{ticket_id}/history", response_model=list[StatusHistoryOut]
)
async def list_status_history_route(
    ticket_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> list[StatusHistoryOut]:
    """Task 13's G1 fix: `schema.py`'s `StatusHistoryOut` was defined and
    schema-tested but never routed (spec §11's ticket-detail "status
    history"). Mirrors `list_comments_route` exactly -- `tickets.
    list_status_history` itself does the org-scoped ticket load (404 on a
    cross-org/missing `ticket_id`) and the oldest-first ordering; this is a
    pure read, so -- like every other list/detail route in this module,
    spec §8.3's no-audit-on-view rule -- it writes no audit event."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            rows = await list_status_history(session, ctx.org_id, ticket_id)
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return [_status_history_out(r) for r in rows]


@router.patch("/service-desk/tickets/{ticket_id}", response_model=TicketOut)
async def update_ticket_route(
    ticket_id: UUID,
    body: TicketUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.write")),
) -> TicketOut:
    # `exclude_unset=True` is load-bearing, not a micro-optimization (spec
    # §9): without it every optional key is present with a `None` default,
    # so "field omitted" and "field explicitly set to null" are
    # indistinguishable -- `update_ticket` relies on key PRESENCE to decide
    # what to touch, and rejects an explicit `null` on a non-nullable column
    # with a typed 400.
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            ticket = await update_ticket(
                session,
                ctx.org_id,
                ctx.user_id,
                ticket_id,
                body.model_dump(exclude_unset=True),
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return await _ticket_out(session, ctx.org_id, ticket)


@router.post("/service-desk/tickets/{ticket_id}/status", response_model=TicketOut)
async def change_status_route(
    ticket_id: UUID,
    body: TicketStatusChange,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.write")),
) -> TicketOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            ticket = await change_status(
                session,
                ctx.org_id,
                ticket_id,
                to_status=body.to_status,
                actor_id=ctx.user_id,
                reason=body.reason,
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return await _ticket_out(session, ctx.org_id, ticket)


@router.post("/service-desk/tickets/{ticket_id}/resolve", response_model=TicketOut)
async def resolve_ticket_route(
    ticket_id: UUID,
    body: TicketResolve,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.write")),
) -> TicketOut:
    # Raising inside `org_session` rolls the whole transaction back, so a
    # refused step leaves no reply behind (`resolve.py`).
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            ticket = await reply_and_resolve(
                session,
                ctx.org_id,
                ticket_id,
                actor_id=ctx.user_id,
                reply_body=body.reply.body if body.reply else None,
                reply_visibility=body.reply.visibility if body.reply else "public",
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return await _ticket_out(session, ctx.org_id, ticket)


@router.post("/service-desk/tickets/{ticket_id}/assign", response_model=TicketOut)
async def assign_ticket_route(
    ticket_id: UUID,
    body: TicketAssign,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.assign")),
) -> TicketOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            ticket = await assign_ticket(
                session,
                ctx.org_id,
                ctx.user_id,
                ticket_id,
                body.model_dump(exclude_unset=True),
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return await _ticket_out(session, ctx.org_id, ticket)


@router.post("/service-desk/tickets/{ticket_id}/approve", response_model=TicketOut)
async def approve_ticket_route(
    ticket_id: UUID,
    body: ApprovalDecision,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.approve")),
) -> TicketOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await decide_approval(
                session, ctx.org_id, ctx.user_id, ticket_id, approve=True, reason=body.reason
            )
            ticket = await get_ticket(session, ctx.org_id, ticket_id)
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return await _ticket_out(session, ctx.org_id, ticket)


@router.post("/service-desk/tickets/{ticket_id}/reject", response_model=TicketOut)
async def reject_ticket_route(
    ticket_id: UUID,
    body: ApprovalDecision,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.approve")),
) -> TicketOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await decide_approval(
                session, ctx.org_id, ctx.user_id, ticket_id, approve=False, reason=body.reason
            )
            ticket = await get_ticket(session, ctx.org_id, ticket_id)
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return await _ticket_out(session, ctx.org_id, ticket)


# --------------------------------------------------------------------------- #
# GET/POST /service-desk/tickets/{ticket_id}/comments (spec §2, §6, §9)
# --------------------------------------------------------------------------- #


@router.get(
    "/service-desk/tickets/{ticket_id}/comments", response_model=list[CommentOut]
)
async def list_comments_route(
    ticket_id: UUID,
    request: Request,
    include_internal: bool = Query(default=True),
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> list[CommentOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            rows = await list_comments(
                session, ctx.org_id, ticket_id, include_internal=include_internal
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return [_comment_out(r) for r in rows]


@router.post(
    "/service-desk/tickets/{ticket_id}/comments",
    status_code=status.HTTP_201_CREATED,
    response_model=CommentOut,
)
async def create_comment_route(
    ticket_id: UUID,
    body: CommentCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.write")),
) -> CommentOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            comment = await add_comment(
                session,
                ctx.org_id,
                ticket_id,
                body=body.body,
                author_id=ctx.user_id,
                author_type="user",
                visibility=body.visibility,
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return _comment_out(comment)


# --------------------------------------------------------------------------- #
# GET/POST /service-desk/tickets/{ticket_id}/attachments (spec §2, §6, §9).
#
# No multipart form dependency is installed in this project (mirrors
# `app.modules.budget.api`'s identical note) -- upload takes the raw request
# body as bytes. The object key is entirely server-derived
# (`{org_id}/service-desk/tickets/{ticket_id}/{attachment_id}`,
# `app.core.storage.ObjectStore._ticket_attachment_key`); attachment bytes
# are never returned inline in a list response -- only the dedicated
# download route above serves them, and only after an explicit audit event.
# --------------------------------------------------------------------------- #


@router.get(
    "/service-desk/tickets/{ticket_id}/attachments", response_model=list[AttachmentOut]
)
async def list_attachments_route(
    ticket_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> list[AttachmentOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await get_ticket(session, ctx.org_id, ticket_id)  # 404 if cross-org/missing
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        # `threads.py` has no dedicated list function (only `add_attachment`/
        # `get_attachment`) -- the same "no service list function, query
        # inline" shape `budget.api.list_lines_route` already uses for
        # `BudgetLine`. Explicit ORDER BY so a caller asserting list
        # equality never depends on incidental scan order.
        rows = (
            (
                await session.execute(
                    select(TicketAttachment)
                    .where(
                        TicketAttachment.org_id == ctx.org_id,
                        TicketAttachment.ticket_id == ticket_id,
                    )
                    .order_by(TicketAttachment.created_at, TicketAttachment.id)
                )
            )
            .scalars()
            .all()
        )
        return [_attachment_out(r) for r in rows]


@router.post(
    "/service-desk/tickets/{ticket_id}/attachments",
    status_code=status.HTTP_201_CREATED,
    response_model=AttachmentOut,
)
async def upload_attachment_route(
    ticket_id: UUID,
    request: Request,
    filename: str = Query(max_length=4096),
    ctx: AuthContext = Depends(require_permission("ticket.write")),
) -> AttachmentOut:
    # Cheapest checks first, before any DB/session/object-store work
    # (mirrors `budget.api.upload_attachment_route`): the declared
    # `Content-Length` fast-path rejection, then filename sanitization --
    # neither touches the database or reads a byte off the wire.
    reject_oversized_content_length(request, MAX_ATTACHMENT_BYTES)
    try:
        sanitized_filename = sanitize_attachment_filename(filename)
    except TicketValidationError as exc:
        raise _http_error(exc) from None

    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        # requirement #1: the ticket is org-scoped-loaded and validated to
        # exist BEFORE the request body is ever read. Reading first would
        # let any `ticket.write` holder buffer up to `MAX_ATTACHMENT_BYTES`
        # against a nonexistent or cross-org ticket id before the 404 is
        # raised -- the in-service 10 MiB check inside `add_attachment` is
        # only defence-in-depth on a body that has ALREADY been read; it is
        # not the guard against that.
        try:
            ticket = await get_ticket(session, ctx.org_id, ticket_id)
        except TicketValidationError as exc:
            raise _http_error(exc) from None

        # Cheap pre-check against the SAME canonical `TERMINAL_STATUSES` set
        # `threads.add_attachment` itself re-checks (spec §6, §3.3): a
        # terminal ticket never accepts a new attachment no matter what, so
        # there is no reason to read the body or construct the object store
        # (a real S3 round trip on first use) for a request already known to
        # fail. `add_attachment`'s own check remains the actual guard --
        # this is defence-in-depth/an efficiency short-circuit, not a second
        # authority on legality (mirrors `budget.api.upload_attachment_route`
        # calling `reject_if_void` before touching the object store).
        if ticket.status in TERMINAL_STATUSES:
            raise _http_error(
                TicketValidationError(
                    tr(
                        "ticket {number} is {status} and accepts no new attachments",
                        number=ticket.number,
                        status=status_quoted(ticket.status),
                    )
                )
            )

        data = await read_bounded_body(request, MAX_ATTACHMENT_BYTES)

        store = _object_store(request)
        try:
            attachment = await add_attachment(
                session,
                ctx.org_id,
                ctx.user_id,
                ticket.id,
                filename=sanitized_filename,
                data=data,
                store=store,
            )
        except TicketValidationError as exc:
            raise _http_error(exc) from None
        return _attachment_out(attachment)



# --------------------------------------------------------------------------- #
# Canned responses: read with `ticket.read` (to insert one into a reply),
# change with `ticket.admin`. The service layer audits every change.
# --------------------------------------------------------------------------- #


def _canned_out(row: TicketCannedResponse) -> CannedResponseOut:
    return CannedResponseOut(
        id=row.id,
        title=row.title,
        body=row.body,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


@router.get("/service-desk/canned-responses", response_model=list[CannedResponseOut])
async def list_canned_responses_route(
    request: Request,
    limit: int = Query(default=MAX_CANNED_RESPONSES, ge=1, le=MAX_CANNED_RESPONSES),
    ctx: AuthContext = Depends(require_permission("ticket.read")),
) -> list[CannedResponseOut]:
    """This org's saved replies, by title. Creation stops at
    `MAX_CANNED_RESPONSES`, so the default page is the whole library."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_canned_responses(session, ctx.org_id, limit=limit)
        return [_canned_out(r) for r in rows]


@router.post(
    "/service-desk/canned-responses",
    status_code=status.HTTP_201_CREATED,
    response_model=CannedResponseOut,
)
async def create_canned_response_route(
    body: CannedResponseCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.admin")),
) -> CannedResponseOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            row = await create_canned_response(
                session, ctx.org_id, ctx.user_id, title=body.title, body=body.body
            )
            return _canned_out(row)
    except TicketValidationError as exc:
        raise _http_error(exc) from None


@router.patch("/service-desk/canned-responses/{response_id}", response_model=CannedResponseOut)
async def update_canned_response_route(
    response_id: UUID,
    body: CannedResponseUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.admin")),
) -> CannedResponseOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            row = await update_canned_response(
                session, ctx.org_id, ctx.user_id, response_id, body.model_dump(exclude_unset=True)
            )
            return _canned_out(row)
    except TicketValidationError as exc:
        raise _http_error(exc) from None


@router.delete(
    "/service-desk/canned-responses/{response_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_canned_response_route(
    response_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ticket.admin")),
) -> Response:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await delete_canned_response(session, ctx.org_id, ctx.user_id, response_id)
    except TicketValidationError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
