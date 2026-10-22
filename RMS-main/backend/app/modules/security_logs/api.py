"""Security-event search + SOC 2 access-evidence export (spec §8).
All routes require audit.read."""

import csv
import io
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import Select, func, select

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.csv_export import escape_formula
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import Device, SecurityEvent

router = APIRouter(tags=["security-logs"])


class SecurityEventOut(BaseModel):
    id: UUID
    device_id: UUID
    hostname: str
    ts: datetime
    event_type: str
    username: str
    source: str
    outcome: str


class SecurityEventDetailOut(SecurityEventOut):
    raw: dict[str, Any]


class SecurityEventsPage(BaseModel):
    items: list[SecurityEventOut]
    total: int


def _filtered_query(
    org_id: UUID,
    device_id: UUID | None,
    username: str | None,
    event_type: str | None,
    from_ts: datetime | None,
    to_ts: datetime | None,
) -> Select[tuple[SecurityEvent, str | None]]:
    """The one predicate both the search and the evidence export are built on.

    ONE BUILDER, BECAUSE A58 IS ABOUT AGREEMENT (finding A58, closeout rec. 7).
    The export used to accept only `from_ts`/`to_ts`, so an operator looking at
    a filtered screen could not produce a file of it; the console labelled the
    button honestly rather than lie about the difference. Handing both routes
    the SAME builder is what makes "the export matches the screen" a fact
    rather than two implementations that agree today.

    BOTH SIDES OF THE JOIN CARRY `org_id`, not only the events. RLS scopes both
    tables under `org_session`, but `security_events.device_id` is a bare FK
    with no org-aware constraint, so an event may legally name another org's
    device -- and this join exists to put that device's HOSTNAME in a SOC 2
    evidence file. Invariant #5 in application code and at the database, not
    either alone; `tests/test_security_api.py` pins both predicates against the
    BYPASSRLS engine, where RLS is not the thing rejecting.
    """
    query = (
        select(SecurityEvent, Device.hostname)
        .join(Device, Device.id == SecurityEvent.device_id)
        .where(SecurityEvent.org_id == org_id, Device.org_id == org_id)
    )
    if device_id:
        query = query.where(SecurityEvent.device_id == device_id)
    if username:
        query = query.where(SecurityEvent.username.ilike(f"%{username}%"))
    if event_type:
        query = query.where(SecurityEvent.event_type == event_type)
    if from_ts:
        query = query.where(SecurityEvent.ts >= from_ts)
    if to_ts:
        query = query.where(SecurityEvent.ts <= to_ts)
    return query


def _out(event: SecurityEvent, hostname: str | None) -> SecurityEventOut:
    """`hostname` is nullable on `devices` since 0061 (an asset need not have
    one), so this signature has to accept `None` even though a SecurityEvent
    only ever arrives from an agent job result -- which means the device DID
    have a hostname when it reported.

    The response field stays non-null: the device id identifies the row
    unambiguously and does not read as a machine name, whereas `""` renders as
    a blank cell an operator would take for a missing device.
    """
    return SecurityEventOut(
        id=event.id,
        device_id=event.device_id,
        hostname=hostname or str(event.device_id),
        ts=event.ts,
        event_type=event.event_type,
        username=event.username,
        source=event.source,
        outcome=event.outcome,
    )


@router.get("/security-events", response_model=SecurityEventsPage)
async def list_security_events(
    request: Request,
    ctx: AuthContext = Depends(require_permission("audit.read")),
    device_id: UUID | None = Query(default=None),
    username: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> SecurityEventsPage:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        query = _filtered_query(
            ctx.org_id, device_id, username, event_type, from_ts, to_ts
        )
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        rows = (
            await session.execute(
                query.order_by(SecurityEvent.ts.desc()).limit(limit).offset(offset)
            )
        ).all()
        return SecurityEventsPage(
            items=[_out(event, hostname) for event, hostname in rows], total=total
        )


@router.get("/security-events/{event_id}", response_model=SecurityEventDetailOut)
async def get_security_event(
    event_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("audit.read")),
) -> SecurityEventDetailOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        row = (
            await session.execute(
                select(SecurityEvent, Device.hostname)
                .join(Device, Device.id == SecurityEvent.device_id)
                .where(SecurityEvent.id == event_id)
            )
        ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such event"))
    event, hostname = row
    base = _out(event, hostname)
    return SecurityEventDetailOut(**base.model_dump(), raw=event.raw)


@router.get("/reports/access.csv", response_class=PlainTextResponse)
async def access_evidence_csv(
    request: Request,
    ctx: AuthContext = Depends(require_permission("audit.read")),
    username: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
) -> PlainTextResponse:
    """SOC 2 access evidence: who logged into what, when, over the period.

    `username` AND `event_type` ARE THE SAME FILTERS THE SEARCH TAKES (finding
    A58, closeout rec. 7). Without them an operator looking at a narrowed
    screen could not produce a file of what they were looking at -- the export
    silently widened to every principal and every event type -- and the console
    had no way to fix that from its side, so it renamed the button to say what
    the file really contained. Both routes now build on `_filtered_query`, so
    the file and the screen agree by construction.

    THE EXPORT IS AUDITED AND THE SEARCH IS NOT (invariant #4). A file of who
    signed in to what, leaving the platform, is a privileged act; this route
    recorded nothing at all until now. The event is written INSIDE the same
    transaction as the read, the shape `app/modules/reporting/export.py` uses,
    so evidence cannot leave without the record of it landing too. It carries
    the filters, the row count and the format -- never the rows: `audit_events`
    is append-only and unprunable by `app_user`, so exported data written into
    it could not be taken back. The filters are the half this route needs most:
    without them the log can say a CSV was taken and cannot say of what.
    """
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = (
            await session.execute(
                _filtered_query(
                    ctx.org_id, None, username, event_type, from_ts, to_ts
                ).order_by(SecurityEvent.ts)
            )
        ).all()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["ts", "hostname", "username", "event_type", "source", "outcome"])
        for event, hostname in rows:
            # Escaped only where the value is genuinely agent-reported free
            # text: `hostname` (device's own name), `username` (the actor
            # name off the raw OS event, `SecurityEventDoc.username` --
            # unconstrained beyond a length cap), and `source` (the
            # OS-native channel/id string, e.g. `"win:4624"` -- also just a
            # length-capped free string, per `SecurityEventDoc.source`).
            # `event_type` and `outcome` are excluded even though they sit in
            # the same row: both are `Literal`-typed on `SecurityEventDoc`
            # (`app.gateway.protocol`) -- `event_type` against
            # `SecurityEventType`, `outcome` against
            # `"success"|"failure"|"n_a"` -- so ingestion rejects anything
            # outside that fixed set (`ingest_events`'s `VALID_TYPES` check
            # for `event_type`; Pydantic's `Literal` validation for
            # `outcome`) and neither can ever begin with a dangerous
            # character. `ts` is a `datetime` the server itself isoformatted.
            # This is SOC 2 evidence explicitly meant to be opened in a
            # spreadsheet (module docstring), so the genuine free-text
            # columns must never carry an unescaped formula.
            writer.writerow(
                [
                    event.ts.isoformat(),
                    escape_formula(hostname),
                    escape_formula(event.username),
                    event.event_type,
                    escape_formula(event.source),
                    event.outcome,
                ]
            )
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="security_logs.evidence_exported",
            target_type="report",
            target_id="access-evidence",
            # THE FILTERS, THE COUNT AND THE FORMAT -- NEVER THE ROWS.
            # `from_ts`/`to_ts` are isoformatted rather than passed as
            # `datetime`: `metadata` is canonically encoded into the hash
            # chain, and a type the encoder has to guess at is a link nobody
            # can recompute later.
            metadata={
                "filters": {
                    "username": username,
                    "event_type": event_type,
                    "from_ts": from_ts.isoformat() if from_ts else None,
                    "to_ts": to_ts.isoformat() if to_ts else None,
                },
                "row_count": len(rows),
                "format": "csv",
            },
        )
    return PlainTextResponse(buffer.getvalue(), media_type="text/csv")
