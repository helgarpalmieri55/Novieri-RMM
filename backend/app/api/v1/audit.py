from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.tenancy import org_session
from app.models import AuditEvent
from app.services.user_names import resolve_display_names

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditEventOut(BaseModel):
    """One appended event, plus the names its two identifiers stand for.

    THE NAMES ARE JOINED AT READ TIME AND ARE NOT PART OF THE RECORD (finding
    B64). `AuditService.record` hashes `_canonical_fields`, which is built from
    the columns; nothing here is stored, so nothing here can move a link in the
    chain. The ids stay exactly where they were, because they are what the log
    actually holds and what survives a rename — the name is the directory's
    CURRENT answer for that id, which is a different fact.
    """

    id: UUID
    ts: datetime
    actor_type: str
    actor_id: UUID | None
    #: The directory's current name for `actor_id`. `None` for a non-user
    #: actor, a deleted user, or one who has no name recorded.
    actor_display_name: str | None
    action: str
    target_type: str
    target_id: str
    #: As `actor_display_name`, and only when `target_type` is `"user"` —
    #: the one target type whose `target_id` names a person.
    target_display_name: str | None
    metadata: dict[str, Any]
    hash: str


class AuditListResponse(BaseModel):
    """One page of appended events, and how many matched before the cut.

    `total` counts the whole filtered set. An audit log whose page size is
    mistaken for its length is the one list in the platform where a reader
    would draw the wrong conclusion from it — that nothing else happened.
    """

    items: list[AuditEventOut]
    total: int


class AuditVerifyResponse(BaseModel):
    ok: bool
    count: int
    broken_at: UUID | None


def _as_uuid(value: str) -> UUID | None:
    """`target_id` is a free `str` — a hostname, a job id, a permission code —
    so a `"user"` target carrying something unparseable is not a 500. The log
    is append-only: history cannot be corrected, only survived."""
    try:
        return UUID(value)
    except ValueError:
        return None


def _user_ids_on(events: Sequence[AuditEvent]) -> set[UUID]:
    """Every id on this page that names a person, gathered before a single
    name is looked up — one statement for the page rather than one per row."""
    ids: set[UUID] = set()
    for event in events:
        if event.actor_type == "user" and event.actor_id is not None:
            ids.add(event.actor_id)
        if event.target_type == "user":
            target = _as_uuid(event.target_id)
            if target is not None:
                ids.add(target)
    return ids


def _target_name(names: dict[UUID, str], event: AuditEvent) -> str | None:
    if event.target_type != "user":
        return None
    target = _as_uuid(event.target_id)
    return names.get(target) if target is not None else None


@router.get("", response_model=AuditListResponse)
async def list_events(
    request: Request,
    ctx: AuthContext = Depends(require_permission("audit.read")),
    action: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AuditListResponse:
    """This org's audit events, newest first, with their actors named.

    THE NAMES ARE A JOIN, NOT A COLUMN (finding B64). Every row is identified
    by id in the table and in the hash chain, which is what makes the chain
    verifiable at all; `actor_display_name` and `target_display_name` are
    resolved from `users` at read time by `app.services.user_names`, in one
    statement for the whole page. Nothing is written, so the append-only log
    and its links are untouched — `GET /audit/verify` is unaffected by
    anything this route does.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        query = select(AuditEvent)
        if action:
            query = query.where(AuditEvent.action == action)
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    query.order_by(AuditEvent.id.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        names = await resolve_display_names(
            session, org_id=ctx.org_id, user_ids=_user_ids_on(rows)
        )
        return AuditListResponse(
            items=[
                AuditEventOut(
                    id=e.id,
                    ts=e.ts,
                    actor_type=e.actor_type,
                    actor_id=e.actor_id,
                    actor_display_name=(
                        names.get(e.actor_id)
                        if e.actor_type == "user" and e.actor_id is not None
                        else None
                    ),
                    action=e.action,
                    target_type=e.target_type,
                    target_id=e.target_id,
                    target_display_name=_target_name(names, e),
                    metadata=e.meta,
                    hash=e.hash,
                )
                for e in rows
            ],
            total=total,
        )


@router.get("/verify", response_model=AuditVerifyResponse)
async def verify_chain(
    request: Request,
    ctx: AuthContext = Depends(require_permission("audit.read")),
) -> AuditVerifyResponse:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        result = await audit_service.verify(session, ctx.org_id)
    return AuditVerifyResponse(ok=result.ok, count=result.count, broken_at=result.broken_at)
