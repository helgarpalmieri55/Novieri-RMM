"""Operator-facing error groups API (docs/superpowers/specs/
2026-08-15-error-monitoring-design.md §5).

GATED, and TWO CODES, NOT ONE: `platform.errors.read` on both GET routes,
`platform.errors.write` on resolve AND on its inverse. Reading which code
paths are failing and changing triage state are different privileges (spec
§5) -- a caller holding only the read code must get 403 from either write,
not a silent no-op. Reopening is not a lesser act than resolving, so it does
not get a lesser gate.

THE HOUSE FORM, load-bearing exactly as `app/api/v1/platform.py` documents:
`ctx: AuthContext = Depends(require_permission(...))` is a PARAMETER, never a
decorator `dependencies=[]` entry -- `app.core.rbac.route_permission` walks
the dependant tree for that exact closure shape, and `tests/
test_route_gating.py`'s whole-router sweep depends on it.

NO EXCEPTION TEXT REACHES A RESPONSE (spec §0.1 falsifier #1). Every field
below traces straight back to `PlatformErrorGroup`/`PlatformErrorEvent`
columns, and neither table HAS a message/traceback column to leak (see
`app.core.errors.capture`'s module docstring) -- this module cannot
reintroduce the leak by projecting a field that was never written.

WHO HOLDS THE CODES, AND WHY THAT SENTENCE IS LOAD-BEARING HERE. Migration
0068 grants `platform.errors.read`/`write` to `super-admin` alone -- the
cross-org platform-operator role that no org-scoped console surface can grant,
clone or assign. Until 0068 they sat on `org-admin`, and because the paragraph
below removes RLS from the picture entirely, that grant WAS the disclosure:
`platform_error_event` carries `org_id`, `user_id`, `job_id` and `device_id`
for every tenant, and any tenant's administrator could read all of them
(`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` F1). Nothing
about the paragraph below is wrong; it just means the permission is not one
control among several, it is the only one. Re-gating these routes on a code a
tenant holds re-opens the finding.

READS GO THROUGH `app.state.app_engine` DIRECTLY, not `org_session`.
`platform_error_group`/`platform_error_event` are platform-scoped, not
tenant-scoped (spec §4.1: `org_id` is a nullable CORRELATION column, not a
tenancy key) -- pinning `app.current_org` here would claim a tenant boundary
that does not exist and that these tables' RLS policy (migration 0056: one
`app_user`-only, deny-all-else policy, no tenant predicate) does not enforce.
This is the same choice `app.core.errors.store.drain_once` already made for
writes; these routes make it for reads. Authorisation is entirely by the
`platform.errors.*` permission, which is platform-wide, exactly as `platform.
status.read` is for `/platform/status`.

THE TWO WRITES DO USE `org_session`, AND THAT IS NOT A RETRACTION OF THE
PARAGRAPH ABOVE. They append to `audit_events`, which is ordinary tenant data
whose RLS check needs `app.current_org` pinned -- to the ACTING operator's
org, which is a true statement about who pressed the button, not a claim that
the group belongs to that org. `platform_error_group`'s own policy (migration
0056: `FOR ALL TO app_user USING (true)`) carries no tenant predicate, so
pinning the GUC filters nothing here; `tests/test_error_api.py::
test_a_group_is_platform_scoped_but_the_audit_row_belongs_to_the_actor` holds
that open from both sides. One session, so the row change and its audit
record commit together or not at all.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.errors.capture import dropped_count
from app.core.errors.triage import (
    ErrorGroupNotFoundError,
    resolve_group,
    unresolve_group,
)
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import PlatformErrorEvent, PlatformErrorGroup

router = APIRouter()

# Bound on how many recent events a detail response carries (spec §5: "a
# group's detail view lists recent events' correlation ids", not every
# event a long-lived group ever recorded).
_EVENT_LIMIT_DEFAULT = 50
_EVENT_LIMIT_MAX = 200


class ErrorGroupOut(BaseModel):
    """One fingerprinted failure -- `platform_error_group` (spec §4),
    verbatim. No field here can carry exception text; the row it is read
    from has none to give."""

    id: UUID = Field(description="Group id")
    fingerprint: str = Field(
        description=(
            "sha256(version|exc_type|module|lineno|qualname) at the deepest "
            "application frame, 16 hex chars"
        )
    )
    fingerprint_version: int = Field(
        description=(
            "Which grouping rule named this row: 1 = the traceback's last frame "
            "(so `module` may be a third-party library), 2 = the deepest app.* frame"
        )
    )
    exc_type: str = Field(description="Bare exception type name, e.g. 'ValueError'")
    module: str = Field(description="Dotted module the exception was raised in")
    lineno: int = Field(description="Line number of the raise site")
    qualname: str = Field(description="Qualified function/method name of the raise site")
    where: str = Field(description="Capture seam: 'api' | 'worker' | 'gateway'")
    first_seen: datetime = Field(description="When this fingerprint was first captured")
    last_seen: datetime = Field(description="When this fingerprint was most recently captured")
    event_count: int = Field(description="Total captured occurrences of this fingerprint")
    resolved_at: datetime | None = Field(
        default=None, description="When an operator marked this group resolved, if ever"
    )


class ErrorGroupListOut(BaseModel):
    """The list route's payload: groups newest-`last_seen`-first, plus the
    capture queue's running drop count (spec §3.1) -- so an operator reading
    the list also sees when the counts below it are a floor, not a total."""

    groups: list[ErrorGroupOut] = Field(description="Groups, newest last_seen first")
    dropped: int = Field(
        description="Captures dropped for queue overflow since process start (spec §3.1)"
    )


class ErrorEventOut(BaseModel):
    """One captured occurrence -- `platform_error_event` (spec §4). Carries
    only the correlation ids read from the contextvars at capture time; each
    is `None`, never `""`, when capture happened outside that context."""

    id: UUID = Field(description="Event id")
    occurred_at: datetime = Field(description="When this occurrence was captured")
    request_id: str | None = Field(
        default=None, description="Correlation id to grep the logs for this occurrence"
    )
    org_id: UUID | None = Field(default=None, description="Org in scope when this was captured")
    user_id: UUID | None = Field(default=None, description="User in scope when this was captured")
    job_id: UUID | None = Field(default=None, description="Job in scope when this was captured")
    device_id: UUID | None = Field(
        default=None, description="Device in scope when this was captured"
    )


class ErrorGroupDetailOut(BaseModel):
    """One group plus its recent events' correlation ids (spec §5) -- each
    copyable, because the operator's next move is to grep the logs for that
    `request_id`."""

    group: ErrorGroupOut = Field(description="The group itself")
    events: list[ErrorEventOut] = Field(
        description="Its most recent events, newest occurred_at first"
    )


def _group_out(row: PlatformErrorGroup) -> ErrorGroupOut:
    return ErrorGroupOut(
        id=row.id,
        fingerprint=row.fingerprint,
        fingerprint_version=row.fingerprint_version,
        exc_type=row.exc_type,
        module=row.module,
        lineno=row.lineno,
        qualname=row.qualname,
        where=row.where_,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        event_count=row.event_count,
        resolved_at=row.resolved_at,
    )


async def _get_group_or_404(session: AsyncSession, group_id: UUID) -> PlatformErrorGroup:
    group = await session.get(PlatformErrorGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("error group not found"))
    return group


@router.get("/platform/errors", response_model=ErrorGroupListOut)
async def list_error_groups(
    request: Request,
    ctx: AuthContext = Depends(require_permission("platform.errors.read")),
    limit: int = Query(default=100, ge=1, le=500, description="Maximum groups to return"),
) -> ErrorGroupListOut:
    """Error groups, newest `last_seen` first, with counts (spec §5)."""
    del ctx
    async with AsyncSession(request.app.state.app_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(PlatformErrorGroup)
                    .order_by(PlatformErrorGroup.last_seen.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return ErrorGroupListOut(groups=[_group_out(r) for r in rows], dropped=dropped_count())


@router.get("/platform/errors/{group_id}", response_model=ErrorGroupDetailOut)
async def get_error_group(
    group_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("platform.errors.read")),
    limit: int = Query(
        default=_EVENT_LIMIT_DEFAULT,
        ge=1,
        le=_EVENT_LIMIT_MAX,
        description="Maximum recent events to return",
    ),
) -> ErrorGroupDetailOut:
    """One group plus its recent events' correlation ids (spec §5)."""
    del ctx
    async with AsyncSession(request.app.state.app_engine, expire_on_commit=False) as session:
        group = await _get_group_or_404(session, group_id)
        events = (
            (
                await session.execute(
                    select(PlatformErrorEvent)
                    .where(PlatformErrorEvent.group_id == group_id)
                    .order_by(PlatformErrorEvent.occurred_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return ErrorGroupDetailOut(
        group=_group_out(group),
        events=[
            ErrorEventOut(
                id=e.id,
                occurred_at=e.occurred_at,
                request_id=e.request_id,
                org_id=e.org_id,
                user_id=e.user_id,
                job_id=e.job_id,
                device_id=e.device_id,
            )
            for e in events
        ],
    )


@router.post("/platform/errors/{group_id}/resolve", response_model=ErrorGroupOut)
async def resolve_error_group(
    group_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("platform.errors.write")),
) -> ErrorGroupOut:
    """Sets `resolved_at` (spec §5). Idempotent: resolving an already-resolved
    group simply moves the timestamp forward rather than erroring, matching
    `app/modules/health/api.py`'s alert-lifecycle transitions."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            group = await resolve_group(
                session,
                group_id=group_id,
                actor_org_id=ctx.org_id,
                actor_user_id=ctx.user_id,
            )
        except ErrorGroupNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("error group not found")) from exc
        # Read the fields INSIDE the transaction, answer AFTER it commits: a
        # 200 carrying a `resolved_at` the database then failed to keep would
        # be the console showing a write that did not happen.
        out = _group_out(group)
    return out


@router.post("/platform/errors/{group_id}/unresolve", response_model=ErrorGroupOut)
async def unresolve_error_group(
    group_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("platform.errors.write")),
) -> ErrorGroupOut:
    """Clears `resolved_at` -- the inverse of resolve, which shipped one-way
    (audit finding B76: an operator who resolved a group by mistake had no
    route back, so the console could not offer a Reopen control without
    404ing).

    Same permission code as resolve, deliberately: a group put back into the
    open list is a group that will page somebody, and undoing triage is not a
    lesser privilege than doing it. Idempotent, and audited in both the
    changed and the unchanged case -- `app.core.errors.triage` documents why
    the undo cannot be a silent blanking of `resolved_at`.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            group = await unresolve_group(
                session,
                group_id=group_id,
                actor_org_id=ctx.org_id,
                actor_user_id=ctx.user_id,
            )
        except ErrorGroupNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("error group not found")) from exc
        out = _group_out(group)
    return out
