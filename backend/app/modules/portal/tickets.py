"""Portal-scoped ticket services (docs/modules/portal/spec.md §5, §6).

**This module adds no ticket business logic.** Every write delegates to the
ticket core: `create_my_ticket` calls `service_desk.tickets.create_ticket`;
`add_my_comment` calls `service_desk.threads.add_comment` with `visibility`
forced to `"public"`; `add_my_attachment` calls
`service_desk.threads.add_attachment`. A second implementation of "how a
ticket is created" is exactly how the two surfaces would drift, and the
ticket core's own tests are what pin that behaviour (spec §6.1).

**Every function takes the caller's `portal_session`-opened session plus
explicit `org_id` and `requester_id`, and every read carries both predicates
in its query IN ADDITION TO the RLS policy** -- belt and braces, the same
discipline `service_desk` applies with an explicit `org_id` predicate on
every query even though `tenant_isolation` RLS already restricts `app_user`
to one org (spec §8.1). Here it is one layer narrower: not just "this org",
but "this org AND this requester's own row".

**Writes run natively on the caller's own `portal_session` connection --
there is no second, elevated connection.** An earlier version of this module
opened a second `org_session`-scoped connection (as `app_user`) to perform
the delegated ticket-core write, because `portal_user`'s grants at the time
did not cover everything `create_ticket`/`add_comment`/`add_attachment`
touch. That worked, but it meant portal WRITES were enforced by application
code verifying ownership before elevating, not by the database -- exactly
what spec §0.2 rejected when it chose a separate `portal_user` role in the
first place ("this project has already shipped a tenancy predicate that
passed its test for the wrong reason"). Migration `0030` closes the grant
gap (`docs/modules/portal/spec.md` §2.1's table, and that migration's own
docstring, have the empirically-determined detail: `users` SELECT with a
restrictive self-row policy, `audit_events` SELECT+INSERT, `domain_events`
INSERT, and INSERT on `ticket_status_history`/`ticket_approval` alongside
their existing SELECT), so every write below now runs on `session` --
the caller's own `portal_user` connection -- with no elevation at all. The
database itself, via `portal_user`'s policies, is what refuses a
cross-requester write now; the explicit ownership checks below
(`_load_my_ticket`) remain as belt-and-braces documentation of intent, not as
the only thing standing between a requester and someone else's ticket.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.models import (
    Ticket,
    TicketApproval,
    TicketAttachment,
    TicketCategory,
    TicketComment,
    TicketStatusHistory,
    User,
)
from app.modules.portal.errors import PortalNotFoundError
from app.modules.portal.schema import (
    PortalApprovalOut,
    PortalAttachmentOut,
    PortalCategoryOut,
    PortalCommentCreate,
    PortalCommentOut,
    PortalStatusHistoryOut,
    PortalTicketCreate,
    PortalTicketDetail,
    PortalTicketSummary,
)
from app.modules.service_desk import threads as sd_threads
from app.modules.service_desk import tickets as sd_tickets
from app.modules.service_desk.refdata import resolve_category
from app.modules.service_desk.schema import TicketCreate


def _summary(ticket: Ticket) -> PortalTicketSummary:
    return PortalTicketSummary(
        id=ticket.id,
        number=ticket.number,
        type=ticket.type,
        status=ticket.status,
        priority=ticket.priority,
        subject=ticket.subject,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


def _comment_out(comment: TicketComment) -> PortalCommentOut:
    return PortalCommentOut(id=comment.id, body=comment.body, created_at=comment.created_at)


def _attachment_out(attachment: TicketAttachment) -> PortalAttachmentOut:
    return PortalAttachmentOut(
        id=attachment.id,
        filename=attachment.filename,
        byte_size=attachment.byte_size,
        created_at=attachment.created_at,
    )


def _history_out(history: TicketStatusHistory) -> PortalStatusHistoryOut:
    # Deliberately NOT `reason=history.reason` -- see PortalStatusHistoryOut's
    # own docstring and spec §5: staff-authored, unbounded free text with no
    # visibility column must never reach the requester verbatim.
    return PortalStatusHistoryOut(
        from_status=history.from_status,
        to_status=history.to_status,
        created_at=history.created_at,
    )


def _approval_out(approval: TicketApproval) -> PortalApprovalOut:
    return PortalApprovalOut(
        state=approval.state, reason=approval.reason, decided_at=approval.decided_at
    )


async def _load_my_ticket(
    session: AsyncSession, org_id: UUID, requester_id: UUID, ticket_id: UUID
) -> Ticket:
    """Org- AND requester-scoped load, on top of RLS (belt and braces, spec
    §8.1's discipline one layer narrower). A ticket that is absent, or that
    belongs to a different requester in the SAME org, is deliberately
    indistinguishable -- both raise `PortalNotFoundError`, never a 403 (spec
    §5): a 403 would confirm the id exists, a cross-requester existence
    oracle. This is the query-level guard; the RLS policy is the structural
    one -- neither alone is trusted (module docstring)."""
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


# ---------------------------------------------------------------------------
# Reads -- entirely on the caller's portal_session-opened connection.
# ---------------------------------------------------------------------------


async def list_my_tickets(
    session: AsyncSession,
    *,
    org_id: UUID,
    requester_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[PortalTicketSummary]:
    """"My tickets" (spec §7): newest first, tie-broken on `id` -- mirrors
    `service_desk.tickets.list_tickets`'s own explicit ordering, so a caller
    asserting list order depends on that, never on incidental scan order."""
    stmt = (
        select(Ticket)
        .where(Ticket.org_id == org_id, Ticket.requester_id == requester_id)
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .limit(limit)
        .offset(offset)
    )
    tickets = (await session.execute(stmt)).scalars().all()
    return [_summary(t) for t in tickets]


async def get_my_ticket(
    session: AsyncSession, *, org_id: UUID, requester_id: UUID, ticket_id: UUID
) -> PortalTicketDetail:
    """Ticket detail: the ticket, its PUBLIC comments, its status history,
    and its approval outcome when one exists (spec §0.3, §5).

    The `visibility == "public"` filter here is the query-level half of spec
    §2.3's belt-and-braces guarantee -- the RLS policy on `ticket_comment`
    already makes an internal note structurally unreadable by `portal_user`
    at all, so this filter can never be the only thing standing between a
    requester and staff-only discussion, but it is still asserted explicitly
    rather than trusted to the policy alone (the same discipline every
    `service_desk` query already applies to `org_id`).
    """
    ticket = await _load_my_ticket(session, org_id, requester_id, ticket_id)

    comments = (
        await session.execute(
            select(TicketComment)
            .where(
                TicketComment.org_id == org_id,
                TicketComment.ticket_id == ticket.id,
                TicketComment.visibility == "public",
            )
            .order_by(TicketComment.created_at, TicketComment.id)
        )
    ).scalars().all()

    history = (
        await session.execute(
            select(TicketStatusHistory)
            .where(
                TicketStatusHistory.org_id == org_id,
                TicketStatusHistory.ticket_id == ticket.id,
            )
            .order_by(TicketStatusHistory.created_at, TicketStatusHistory.id)
        )
    ).scalars().all()

    approval = (
        await session.execute(
            select(TicketApproval).where(
                TicketApproval.org_id == org_id, TicketApproval.ticket_id == ticket.id
            )
        )
    ).scalar_one_or_none()

    # I1/I2: attachments are scoped to the requester's OWN uploads, exactly
    # like `download_my_attachment_route` (`api.py`) -- `ticket_attachment`
    # has no visibility column, so a staff-uploaded file on this ticket must
    # never appear here even though the ticket itself is the requester's.
    attachments = (
        await session.execute(
            select(TicketAttachment)
            .where(
                TicketAttachment.org_id == org_id,
                TicketAttachment.ticket_id == ticket.id,
                TicketAttachment.uploaded_by == requester_id,
            )
            .order_by(TicketAttachment.created_at, TicketAttachment.id)
        )
    ).scalars().all()

    return PortalTicketDetail(
        id=ticket.id,
        number=ticket.number,
        type=ticket.type,
        status=ticket.status,
        priority=ticket.priority,
        subject=ticket.subject,
        description=ticket.description,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        comments=[_comment_out(c) for c in comments],
        status_history=[_history_out(h) for h in history],
        attachments=[_attachment_out(a) for a in attachments],
        approval=_approval_out(approval) if approval is not None else None,
    )


async def list_categories(session: AsyncSession, *, org_id: UUID) -> list[PortalCategoryOut]:
    """Active categories a requester may file under (spec §2.1: `portal_user`
    has SELECT on `ticket_category`, org only, `active = true` -- no
    requester predicate, since categories are org refdata, not
    requester-owned)."""
    stmt = (
        select(TicketCategory)
        .where(TicketCategory.org_id == org_id, TicketCategory.active.is_(True))
        .order_by(TicketCategory.sort_order, TicketCategory.name)
    )
    categories = (await session.execute(stmt)).scalars().all()
    return [
        PortalCategoryOut(id=c.id, key=c.key, name=c.name, type=c.type) for c in categories
    ]


# ---------------------------------------------------------------------------
# Writes -- entirely on the caller's portal_session-opened connection. The
# explicit ownership check (`_load_my_ticket`) remains as belt-and-braces
# documentation of intent; the database's own policies (migration `0030`) are
# what actually refuse a cross-requester write now.
# ---------------------------------------------------------------------------


async def create_my_ticket(
    session: AsyncSession,
    *,
    org_id: UUID,
    requester_id: UUID,
    data: PortalTicketCreate,
) -> PortalTicketDetail:
    """File a ticket as the caller (spec §0.3, §6.2).

    `requester_id` is this function's own parameter -- ultimately
    `PortalContext.requester_user_id` -- never anything read off `data`
    (`PortalTicketCreate` carries no `requester_id` field at all, so there is
    nothing for a request body to smuggle). `type` is resolved from the
    chosen category's own `type`, via the SAME `service_desk.refdata.
    resolve_category` the ticket core itself uses to validate a category
    reference org-scoped -- not re-derived here, and not asked of the
    caller, so a requester can never submit a `type` that mismatches their
    chosen category (`create_ticket` would otherwise reject that
    combination anyway; resolving it here means it is never even offered).

    `origin="portal"` (spec §2, `TicketOrigin`) -- distinct from the
    technician-facing route's `origin="technician"`, so a ticket's origin
    column always names which surface actually created it.

    THE CLIENT IS THE REQUESTER'S (MSP clients plan 2026-10-26 Task 2, C5):
    a contact files under its own client; a staff member -- the only portal
    user until contacts sign in (project 2) -- under the internal client,
    whose id is the organisation's. The portal has no selector, and
    `portal_user` cannot read `clients`, so nothing else is asked. A client
    outside the session's scope (`its_client_visible`, the predicate the
    INSERT's own `client_isolation` policy would apply) is a 404 like every
    other thing the portal cannot see -- never the policy violation a 500
    would be.
    """
    category = await resolve_category(session, org_id, data.category_id)
    me = (
        await session.execute(
            # `portal_user` reads its own `users` row and no other -- and none
            # at all under an empty client scope, which is the 404 below too.
            select(User.kind, User.client_id).where(
                User.org_id == org_id, User.id == requester_id
            )
        )
    ).one_or_none()
    if me is None:
        raise PortalNotFoundError(tr("no such client"))
    client_id = me.client_id if me.kind == "contact" and me.client_id is not None else org_id
    if not (await session.execute(select(func.its_client_visible(client_id)))).scalar():
        raise PortalNotFoundError(tr("no such client"))
    ticket = await sd_tickets.create_ticket(
        session,
        org_id,
        requester_id,
        TicketCreate(
            type=category.type,
            category_id=category.id,
            subject=data.subject,
            description=data.description,
            priority=data.priority,
            requester_id=requester_id,
        ),
        origin="portal",
        client_id=client_id,
    )
    return await get_my_ticket(
        session, org_id=org_id, requester_id=requester_id, ticket_id=ticket.id
    )


async def add_my_comment(
    session: AsyncSession,
    *,
    org_id: UUID,
    requester_id: UUID,
    ticket_id: UUID,
    data: PortalCommentCreate,
) -> PortalCommentOut:
    """Reply on my own ticket (spec §0.3, §2.2, §6.2).

    `visibility` is forced to `"public"` here -- `PortalCommentCreate` has no
    such field for a caller to set in the first place, and even if it did,
    this is the second, independent guard: the database's `WITH CHECK
    (visibility = 'public' AND <parent is mine>)` (migration `0028`) is the
    structural one, this is the application-layer belt to its RLS brace.
    `author_id=requester_id`, `author_type="user"` -- a requester can never
    post a `system`-attributed comment.
    """
    await _load_my_ticket(session, org_id, requester_id, ticket_id)

    comment = await sd_threads.add_comment(
        session,
        org_id,
        ticket_id,
        body=data.body,
        author_id=requester_id,
        author_type="user",
        visibility="public",
    )
    return _comment_out(comment)


async def add_my_attachment(
    session: AsyncSession,
    *,
    org_id: UUID,
    requester_id: UUID,
    ticket_id: UUID,
    filename: str,
    data: bytes,
    store: ObjectStore,
) -> PortalAttachmentOut:
    """Attach a file to my own ticket (spec §0.3, §6.3).

    Ownership is verified BEFORE anything about the upload is touched (spec
    §6.3: the ticket core's own hardened route already verifies scope before
    buffering any body; this is that same discipline one layer narrower --
    verified as MINE, not just as existing in this org). The size cap,
    filename sanitisation, and server-derived object key are entirely
    `service_desk.threads.add_attachment`'s -- not re-derived here (spec
    §6.3: "the portal reuses that ordering exactly").
    """
    await _load_my_ticket(session, org_id, requester_id, ticket_id)

    attachment = await sd_threads.add_attachment(
        session,
        org_id,
        requester_id,
        ticket_id,
        filename=filename,
        data=data,
        store=store,
    )
    return _attachment_out(attachment)
