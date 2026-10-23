"""Canned responses: saved replies an operator inserts into a ticket comment.

Every mutation is audited with the title only; the body is reply text, which
the audit log has no reason to copy. Every query carries `org_id` as well as
running under RLS (invariant #5).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.models import TicketCannedResponse

# A library, not a data store: enough for every reply a small team repeats.
MAX_CANNED_RESPONSES = 500


async def list_canned_responses(
    session: AsyncSession, org_id: UUID, *, limit: int = MAX_CANNED_RESPONSES
) -> list[TicketCannedResponse]:
    stmt = (
        select(TicketCannedResponse)
        .where(TicketCannedResponse.org_id == org_id)
        .order_by(func.lower(TicketCannedResponse.title), TicketCannedResponse.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _get(session: AsyncSession, org_id: UUID, response_id: UUID) -> TicketCannedResponse:
    row = (
        await session.execute(
            select(TicketCannedResponse).where(
                TicketCannedResponse.org_id == org_id, TicketCannedResponse.id == response_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise TicketNotFoundError(
            tr(
                "canned response not found: {response_id}",
                response_id=response_id,
            )
        )
    return row


async def _reject_duplicate_title(
    session: AsyncSession, org_id: UUID, title: str, *, excluding: UUID | None = None
) -> None:
    # Checked up front so a clash is a 400; the unique index still guards races.
    stmt = select(TicketCannedResponse.id).where(
        TicketCannedResponse.org_id == org_id,
        func.lower(TicketCannedResponse.title) == title.lower(),
    )
    if excluding is not None:
        stmt = stmt.where(TicketCannedResponse.id != excluding)
    if (await session.execute(stmt)).first() is not None:
        raise TicketValidationError(
            tr(
                "a saved reply called {title} already exists",
                title=repr(title),
            )
        )


async def _record(
    session: AsyncSession, org_id: UUID, actor_id: UUID, action: str, row: TicketCannedResponse
) -> None:
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type="ticket_canned_response",
        target_id=str(row.id),
        metadata={"title": row.title},
    )
    await session.flush()


async def create_canned_response(
    session: AsyncSession, org_id: UUID, actor_id: UUID, *, title: str, body: str
) -> TicketCannedResponse:
    await _reject_duplicate_title(session, org_id, title)
    count = (
        await session.execute(
            select(func.count()).where(TicketCannedResponse.org_id == org_id)
        )
    ).scalar_one()
    if count >= MAX_CANNED_RESPONSES:
        raise TicketValidationError(
            tr(
                "this organisation already has {limit} saved replies",
                limit=MAX_CANNED_RESPONSES,
            )
        )
    row = TicketCannedResponse(org_id=org_id, title=title, body=body, created_by=actor_id)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await _record(session, org_id, actor_id, "ticket.canned_response_created", row)
    return row


async def update_canned_response(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    response_id: UUID,
    fields: dict[str, Any],
) -> TicketCannedResponse:
    if any(value is None for value in fields.values()):
        raise TicketValidationError(tr("title and body cannot be cleared"))
    row = await _get(session, org_id, response_id)
    if "title" in fields:
        await _reject_duplicate_title(session, org_id, fields["title"], excluding=row.id)
        row.title = fields["title"]
    if "body" in fields:
        row.body = fields["body"]
    row.updated_by = actor_id
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await _record(session, org_id, actor_id, "ticket.canned_response_updated", row)
    return row


async def delete_canned_response(
    session: AsyncSession, org_id: UUID, actor_id: UUID, response_id: UUID
) -> None:
    row = await _get(session, org_id, response_id)
    await _record(session, org_id, actor_id, "ticket.canned_response_deleted", row)
    await session.delete(row)
    await session.flush()
