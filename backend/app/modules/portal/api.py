"""Portal REST API (docs/modules/portal/spec.md §5, §9).

The routes under `/api/v1/portal`: seven for tickets, two for help articles.
This module adds no ticket business logic (spec §1, §6.1) -- every handler
translates an already-typed error and does nothing else; the actual work is
`app.modules.portal.tickets`'s six service functions (and `help.py`'s two),
called on the session opened by `require_portal_permission` (via
`PortalContext`) through `portal_session`.

**`portal_session` is the ONLY way any route in this file reaches the
database** -- `tests/test_portal_api.py::test_no_portal_route_opens_an_org_
session` enforces this structurally by scanning this package's source. A
route that opened `org_session` instead would connect as `app_user` and see
the whole org (spec §0.2, §3).

**RBAC (spec §4, §5): every route is gated by `require_portal_permission`,
never by the bare principal resolver** -- reads take
`portal.ticket.read`, writes take `portal.ticket.write`. Completeness is
derived from the router, not a hand-maintained list
(`tests/test_portal_api.py::test_every_portal_route_requires_a_permission`):
`app.core.rbac.route_permission` was extended (task 3, `principal.py`'s
docstring) to recover the gated code from `require_portal_permission`'s
closure exactly as it does for `require_permission`.

**Not-mine is 404, never 403 (spec §5).** `PortalNotFoundError` -- raised by
every one of `tickets.py`'s functions for a ticket that is absent OR belongs
to a different requester in the same org -- is translated to 404 here and
nowhere else translates it. A 403 would confirm the id exists, which is a
cross-requester existence oracle (the same discipline the ticket core
already applies to `default_queue_id`). `tickets.py`'s delegated
service-desk write path can also raise a service-desk `TicketValidationError`
(e.g. `resolve_category` for an unknown category, or a terminal-ticket
comment/attachment rejected deep inside `threads.add_comment`/
`add_attachment`) -- `TicketNotFoundError` (that hierarchy's own 404 case,
e.g. an unrecognized `category_id`) becomes 404 too; every other
`TicketValidationError` becomes 400. Routing is by TYPE, mirroring
`service_desk.api._http_error` -- never by matching exception-message text.

**Route ordering (spec §5, the Phase-11/12 lesson carried into the ticket
core and now here): literal segments before `{id}` parameters.**
`/portal/categories` (no id at all) is declared first, then the `tickets`
collection (list/create, no id), then every `{ticket_id}`-shaped route, with
the comment/attachment sub-resources declared last -- identical ordering
discipline to `service_desk.api`.

**Attachment upload (spec §6.3, task brief requirement #5): the ticket
core's ordering is reused EXACTLY.** `upload_my_attachment_route` below
rejects an oversized declared `Content-Length` FIRST (before touching the
database at all), then opens the caller's `portal_session` and verifies the
ticket is in scope, and only THEN reads the bounded body --
mirroring `app.modules.service_desk.api.upload_attachment_route` line for
line. Reading the body first would let any `portal.ticket.write` holder
buffer up to `MAX_ATTACHMENT_BYTES` against a ticket that is not theirs
before the 404 is ever raised -- exactly the same concern
`service_desk.api`'s own docstring names for a cross-org ticket id, one
layer narrower (cross-requester, not just cross-org). The ownership check
here (`_load_my_ticket_row`) is intentionally a second, narrower one on top
of RLS -- `tickets.add_my_attachment` performs its own ownership check again
before delegating to `threads.add_attachment`, which performs a THIRD
(org-scoped) check internally. None of the three is trusted alone (spec
§0.2's belt-and-braces discipline); this route's own check exists solely so
the 404 can be raised before any byte of the body is read.

**Attachment download (`GET /portal/tickets/{id}/attachments/{attachment_id}`)
is a pure read and writes no audit event (spec §8: "No audit on reads").**
This differs from `service_desk.api.download_attachment_route`, which
audits `ticket.attachment_downloaded` -- that is a staff-surface decision
this module does not inherit; spec §8 is explicit that only ticket
creation/comment/attachment WRITES are audited here, and the ticket core's
own write functions already write those events.

**Attachment download is scoped to `uploaded_by = requester GUC`, not just
to the parent ticket (I1 fix).** `ticket_attachment` has no visibility
column -- unlike `ticket_comment`, whose `visibility = 'public'` RLS
predicate makes an internal note structurally unreadable regardless of the
query -- so a staff-uploaded file on the requester's OWN ticket would
otherwise be indistinguishable from the requester's own upload and served
right back to them. `download_my_attachment_route`'s query below adds an
explicit `TicketAttachment.uploaded_by == ctx.requester_user_id` predicate:
a requester may retrieve only what they themselves attached. Staff-to-
requester file sharing (deliberately attaching something FOR the requester
to see) is a real product need but needs its own visibility column and a
deliberate design, matching `ticket_comment`'s -- deferred, spec §11, not
invented here.

**The engine (`_portal_engine`): built lazily, on first request, and cached
on `app.state` -- mirrors `service_desk.api._object_store`'s pattern
exactly, and for the identical reason `app.core.db.get_portal_engine`'s own
docstring gives: `database_portal_url` defaults to `""` so that the many
pre-existing test modules building `Settings` directly for unrelated modules
don't all need it set, so this module must not force that URL to resolve at
APP-CREATION time (`app.modules.register_all` runs for every `api_env`-backed
test in the suite, portal or not) -- only an actual portal request may. It is
built from `request.app.state.settings.database_portal_url` rather than by
calling `app.core.db.get_portal_engine()` itself, because that helper's
engine is a MODULE-GLOBAL singleton keyed off `app.core.config.get_settings()`
-- the process-wide cached `Settings` -- and every `api_env`-backed test
builds its OWN throwaway `Settings` (a fresh Postgres cluster per test
module) without ever calling `get_settings()`/`override_settings`. Using the
global would silently point this module's portal connections at the wrong
database (or none) in every test that isn't the very first to touch it in
the process. `app.state.app_engine`/`app.state.admin_engine` avoid this
identical trap by being built explicitly in `app.main.create_app` from the
per-app `Settings`; this module reaches the same guarantee without touching
`app.main` by deferring construction to first use instead, exactly as
`_object_store` already does for the object store.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.http import read_bounded_body, reject_oversized_content_length
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.core.tenancy import portal_session
from app.core.ticket_words import status_quoted
from app.models import Ticket, TicketAttachment
from app.modules.portal.errors import PortalNotFoundError, PortalValidationError
from app.modules.portal.help import get_help_article, suggest_help
from app.modules.portal.principal import PortalContext, require_portal_permission
from app.modules.portal.schema import (
    PortalAttachmentOut,
    PortalCategoryOut,
    PortalCommentCreate,
    PortalCommentOut,
    PortalHelpArticle,
    PortalHelpSuggestion,
    PortalTicketCreate,
    PortalTicketDetail,
    PortalTicketSummary,
)
from app.modules.portal.tickets import (
    add_my_attachment,
    add_my_comment,
    create_my_ticket,
    get_my_ticket,
    list_categories,
    list_my_tickets,
)
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.lifecycle import TERMINAL_STATUSES
from app.modules.service_desk.threads import MAX_ATTACHMENT_BYTES

router = APIRouter(tags=["portal"])


def _http_error(exc: PortalValidationError | TicketValidationError) -> HTTPException:
    """Translate a service-layer error to an HTTP 4xx -- never a 500.

    Routing is by TYPE, mirroring `service_desk.api._http_error` exactly.
    `PortalNotFoundError` (this module's own not-mine-or-absent case) and
    `TicketNotFoundError` (the ticket core's org-scoped case, reachable via
    `create_my_ticket`'s `resolve_category` call) both become 404; every
    other validation error -- from either hierarchy -- becomes 400.
    """
    if isinstance(exc, (PortalNotFoundError, TicketNotFoundError)):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _portal_engine(request: Request) -> AsyncEngine:
    """Lazily build (and cache) this app's `portal_user`-role engine on first
    use. See the module docstring for why this is deferred to first request
    and built from `request.app.state.settings` rather than the module-global
    `app.core.db.get_portal_engine()`."""
    engine = getattr(request.app.state, "portal_engine", None)
    if engine is None:
        url = request.app.state.settings.database_portal_url
        if not url:
            raise RuntimeError(  # i18n-exempt: server misconfiguration, a logged 500
                "ITS_DATABASE_PORTAL_URL is not set. The end-user portal needs "
                "its own RLS-constrained connection as the `portal_user` role "
                "(see deploy/postgres/init-app-user.sh); staff credentials must "
                "not be reused, because portal_user's row-scoped policies do "
                "not apply to app_user."
            )
        engine = create_async_engine(url, pool_pre_ping=True)
        request.app.state.portal_engine = engine
    return engine


def _object_store(request: Request) -> ObjectStore:
    """Lazily build (and cache) the object store on first use -- shared with
    every other module that touches it (`app.state.object_store` starts
    `None`, `app.main.create_app`), mirrors `service_desk.api._object_store`
    exactly."""
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(request.app.state.settings)
        request.app.state.object_store = store
    return store


async def _load_my_ticket_row(
    session: AsyncSession, org_id: UUID, requester_id: UUID, ticket_id: UUID
) -> Ticket:
    """Route-level ownership check, on the caller's own `portal_session`
    connection -- NOT ticket business logic (mirrors `service_desk.api.
    list_attachments_route`'s "no service function for this narrow shape,
    query inline" precedent). Needed only by `upload_my_attachment_route`
    (to verify scope BEFORE reading the request body -- see module
    docstring) and `download_my_attachment_route` (to scope the attachment
    lookup to the caller's own ticket). Every OTHER route already gets this
    check for free from `tickets.py`'s own `_load_my_ticket`, reached via
    `get_my_ticket`/`add_my_comment`/`add_my_attachment`."""
    ticket = (
        await session.execute(
            select(Ticket).where(
                Ticket.org_id == org_id,
                Ticket.id == ticket_id,
                Ticket.requester_id == requester_id,
            )
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise PortalNotFoundError(tr("ticket {ticket_id} not found", ticket_id=ticket_id))
    return ticket


# --------------------------------------------------------------------------- #
# GET /portal/categories (spec §2.1, §5)
# --------------------------------------------------------------------------- #


@router.get("/portal/categories", response_model=list[PortalCategoryOut])
async def list_categories_route(
    request: Request,
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.read")),
) -> list[PortalCategoryOut]:
    async with portal_session(
        ctx.org_id, ctx.requester_user_id, engine=_portal_engine(request), clients=ctx.clients
    ) as session:
        return await list_categories(session, org_id=ctx.org_id)


# --------------------------------------------------------------------------- #
# GET /portal/help/... -- help articles staff marked visible in the portal.
# Same read permission as the rest of the portal; the database limits
# `portal_user` to flagged, unarchived articles (migration 0160).
# --------------------------------------------------------------------------- #


@router.get("/portal/help/suggest", response_model=list[PortalHelpSuggestion])
async def suggest_help_route(
    request: Request,
    q: str = Query(min_length=1, max_length=300),
    limit: int = Query(default=5, ge=1, le=10),
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.read")),
) -> list[PortalHelpSuggestion]:
    async with portal_session(
        ctx.org_id, ctx.requester_user_id, engine=_portal_engine(request), clients=ctx.clients
    ) as session:
        return await suggest_help(session, org_id=ctx.org_id, text=q, limit=limit)


@router.get("/portal/help/articles/{article_id}", response_model=PortalHelpArticle)
async def get_help_article_route(
    article_id: UUID,
    request: Request,
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.read")),
) -> PortalHelpArticle:
    async with portal_session(
        ctx.org_id, ctx.requester_user_id, engine=_portal_engine(request), clients=ctx.clients
    ) as session:
        try:
            return await get_help_article(session, org_id=ctx.org_id, article_id=article_id)
        except PortalNotFoundError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET/POST /portal/tickets (spec §2, §5, §6.2)
# --------------------------------------------------------------------------- #


@router.get("/portal/tickets", response_model=list[PortalTicketSummary])
async def list_my_tickets_route(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.read")),
) -> list[PortalTicketSummary]:
    async with portal_session(
        ctx.org_id, ctx.requester_user_id, engine=_portal_engine(request), clients=ctx.clients
    ) as session:
        return await list_my_tickets(
            session,
            org_id=ctx.org_id,
            requester_id=ctx.requester_user_id,
            limit=limit,
            offset=offset,
        )


@router.post(
    "/portal/tickets", status_code=status.HTTP_201_CREATED, response_model=PortalTicketDetail
)
async def create_my_ticket_route(
    body: PortalTicketCreate,
    request: Request,
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.write")),
) -> PortalTicketDetail:
    async with portal_session(
        ctx.org_id, ctx.requester_user_id, engine=_portal_engine(request), clients=ctx.clients
    ) as session:
        try:
            return await create_my_ticket(
                session, org_id=ctx.org_id, requester_id=ctx.requester_user_id, data=body
            )
        except (PortalValidationError, TicketValidationError) as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET /portal/tickets/{ticket_id} (spec §0.3, §5)
# --------------------------------------------------------------------------- #


@router.get("/portal/tickets/{ticket_id}", response_model=PortalTicketDetail)
async def get_my_ticket_route(
    ticket_id: UUID,
    request: Request,
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.read")),
) -> PortalTicketDetail:
    async with portal_session(
        ctx.org_id, ctx.requester_user_id, engine=_portal_engine(request), clients=ctx.clients
    ) as session:
        try:
            return await get_my_ticket(
                session,
                org_id=ctx.org_id,
                requester_id=ctx.requester_user_id,
                ticket_id=ticket_id,
            )
        except PortalNotFoundError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# POST /portal/tickets/{ticket_id}/comments (spec §0.3, §2.2, §6.2)
# --------------------------------------------------------------------------- #


@router.post(
    "/portal/tickets/{ticket_id}/comments",
    status_code=status.HTTP_201_CREATED,
    response_model=PortalCommentOut,
)
async def add_my_comment_route(
    ticket_id: UUID,
    body: PortalCommentCreate,
    request: Request,
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.write")),
) -> PortalCommentOut:
    async with portal_session(
        ctx.org_id, ctx.requester_user_id, engine=_portal_engine(request), clients=ctx.clients
    ) as session:
        try:
            return await add_my_comment(
                session,
                org_id=ctx.org_id,
                requester_id=ctx.requester_user_id,
                ticket_id=ticket_id,
                data=body,
            )
        except (PortalValidationError, TicketValidationError) as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# POST /portal/tickets/{ticket_id}/attachments,
# GET  /portal/tickets/{ticket_id}/attachments/{attachment_id}
# (spec §0.3, §6.3)
# --------------------------------------------------------------------------- #


@router.post(
    "/portal/tickets/{ticket_id}/attachments",
    status_code=status.HTTP_201_CREATED,
    response_model=PortalAttachmentOut,
)
async def upload_my_attachment_route(
    ticket_id: UUID,
    request: Request,
    filename: str = Query(max_length=4096),
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.write")),
) -> PortalAttachmentOut:
    # Requirement #5 (task brief), mirroring `service_desk.api.
    # upload_attachment_route` exactly: cheapest check first, before any
    # DB/session work at all.
    reject_oversized_content_length(request, MAX_ATTACHMENT_BYTES)

    async with portal_session(
        ctx.org_id, ctx.requester_user_id, engine=_portal_engine(request), clients=ctx.clients
    ) as session:
        # The ticket is verified to be MINE before the request body is ever
        # read (module docstring): reading first would let any
        # `portal.ticket.write` holder buffer up to `MAX_ATTACHMENT_BYTES`
        # against a ticket that is not theirs before the 404 is raised.
        try:
            ticket = await _load_my_ticket_row(
                session, ctx.org_id, ctx.requester_user_id, ticket_id
            )
        except PortalNotFoundError as exc:
            raise _http_error(exc) from None

        # Cheap pre-check against the ticket core's own canonical
        # `TERMINAL_STATUSES` set (mirrors `service_desk.api.
        # upload_attachment_route`'s identical short-circuit): a terminal
        # ticket never accepts a new attachment, so there is no reason to
        # read the body or touch the object store for a request already
        # known to fail. `threads.add_attachment`'s own check (reached via
        # `add_my_attachment`) remains the actual guard.
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
            return await add_my_attachment(
                session,
                org_id=ctx.org_id,
                requester_id=ctx.requester_user_id,
                ticket_id=ticket.id,
                filename=filename,
                data=data,
                store=store,
            )
        except (PortalValidationError, TicketValidationError) as exc:
            raise _http_error(exc) from None


@router.get("/portal/tickets/{ticket_id}/attachments/{attachment_id}")
async def download_my_attachment_route(
    ticket_id: UUID,
    attachment_id: UUID,
    request: Request,
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.read")),
) -> Response:
    async with portal_session(
        ctx.org_id, ctx.requester_user_id, engine=_portal_engine(request), clients=ctx.clients
    ) as session:
        try:
            ticket = await _load_my_ticket_row(
                session, ctx.org_id, ctx.requester_user_id, ticket_id
            )
        except PortalNotFoundError as exc:
            raise _http_error(exc) from None

        attachment = (
            await session.execute(
                select(TicketAttachment).where(
                    TicketAttachment.org_id == ctx.org_id,
                    TicketAttachment.id == attachment_id,
                    TicketAttachment.ticket_id == ticket.id,
                    # I1 fix: author-scoped, not just parent-ticket-scoped.
                    # `ticket_attachment` has no visibility column (unlike
                    # `ticket_comment`), so a staff-uploaded attachment on the
                    # requester's OWN ticket is otherwise indistinguishable
                    # from one the requester uploaded themselves -- see the
                    # module docstring's note on this route. A requester may
                    # retrieve only what they themselves attached; the same
                    # "attachment not found" shape as a wholly absent
                    # attachment, so this adds no cross-requester oracle.
                    TicketAttachment.uploaded_by == ctx.requester_user_id,
                )
            )
        ).scalar_one_or_none()
        if attachment is None:
            raise _http_error(
                PortalNotFoundError(
                    tr("attachment {attachment_id} not found", attachment_id=attachment_id)
                )
            )

        ref = attachment.object_key
        filename = attachment.filename

    # No audit event here (spec §8: "No audit on reads") -- unlike
    # `service_desk.api.download_attachment_route`, which audits
    # `ticket.attachment_downloaded` for the staff surface. Defence in
    # depth: `get_ticket_attachment` itself asserts `ref` is under this
    # caller's org prefix before it will read it.
    data = await _object_store(request).get_ticket_attachment(ref, ctx.org_id)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
