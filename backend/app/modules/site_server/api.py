"""Site-server HTTP routes (spec §11, §10.2).

**EVERY ROUTE CARRIES A `require_permission(...)` DEPENDENCY**, which is what
makes RBAC completeness DERIVABLE FROM THE ROUTER (`app/core/rbac.py`'s
`route_permission`, invariant #6) rather than from a list somebody has to
remember to update. A route added without one returns `None` there and fails
`tests/test_site_server_api.py`'s completeness test BY EXISTING.

**`site.manage` AND `site.wake` ARE DELIBERATELY SEPARATE, AND THIS IS WHERE
THAT BECOMES REAL** (§10.2). Waking a machine inside an existing configuration
is routine technician work; deciding that a host may serve an entire subnet is
not. Collapsed into one code, anyone who can wake a machine could also decide
what a site server is — and what a site server declares is exactly what the
control plane later computes a broadcast address from. The separation is tested
in BOTH directions, because one direction alone passes against a router that
gates everything on a single code.

**HANDLERS STAY THIN**: resolve the principal, call the service, map the error.

**ERRORS ARE MAPPED OUTSIDE THE `org_session` BLOCK, NEVER CAUGHT INSIDE IT.**
Catching inside leaves the failed request's partial writes committed — the
transaction is the unit of atomicity, and an `except` in the middle of it is a
promise that the half-written state is fine.

**404, NEVER 403, FOR ANOTHER ORG'S ID.** A 403 confirms the id exists, which is
a cross-tenant existence oracle. That holds for the site server AND for the
`device_id` in a promote body.

**THE WAKE ENVELOPE IS PUBLISHED ONLY AFTER THE `org_session` BLOCK HAS EXITED.**
A job published before the transaction commits could be handed to an agent
describing state that then rolled back — and this job puts a datagram on a LAN.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job
from app.models import Agent
from app.modules.site_server import registry as registry_service
from app.modules.site_server.dispatch import wake_via_site_server
from app.modules.site_server.errors import (
    SiteServerClientError,
    SiteServerConflictError,
    SiteServerError,
    SiteServerNotFoundError,
    SiteServerValidationError,
)
from app.modules.site_server.models import SiteServer
from app.modules.site_server.net import segment_strings
from app.modules.site_server.schema import (
    SiteServerCreate,
    SiteServerOut,
    SiteServerUpdate,
    SiteWakeRequest,
)
from app.modules.site_server.segments import segment_check

router = APIRouter(prefix="/site-servers", tags=["site-servers"])


def _http_error(exc: SiteServerError) -> HTTPException:
    """By TYPE, never by message text. `SiteServerNotFoundError` and
    `SiteServerClientError` are checked first because they are SUBCLASSES of
    the validation error."""
    if isinstance(exc, SiteServerConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, SiteServerNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, SiteServerClientError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    if isinstance(exc, SiteServerValidationError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _engine(request: Request) -> Any:
    return request.app.state.app_engine


async def _agent_state(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> tuple[str | None, Any]:
    row = (
        await session.execute(
            select(Agent.status, Agent.last_seen_at).where(
                Agent.org_id == org_id, Agent.device_id == device_id
            )
        )
    ).first()
    return (None, None) if row is None else (row.status, row.last_seen_at)


async def _out(session: AsyncSession, org_id: UUID, row: SiteServer) -> SiteServerOut:
    status_value, last_seen = await _agent_state(session, org_id, row.device_id)
    return SiteServerOut(
        id=row.id,
        client_id=row.client_id,
        device_id=row.device_id,
        name=row.name,
        # `segments` comes back from asyncpg as `IPv4Network` objects, not
        # strings; the response model would coerce them via `str()` anyway, but
        # doing it here keeps ONE place in the codebase that knows.
        segments=segment_strings(row.segments),
        roles=list(row.roles),
        enabled=row.enabled,
        notes=row.notes,
        site_id=row.site_id,
        agent_status=status_value,
        agent_last_seen_at=last_seen,
    )


@router.get("", response_model=list[SiteServerOut])
async def list_site_servers_route(
    request: Request,
    enabled: bool | None = Query(default=None),
    site_id: UUID | None = Query(default=None),
    segment: str | None = Query(default=None),
    ctx: AuthContext = Depends(require_permission("site.read")),
) -> list[SiteServerOut]:
    """`segment` filters by CONTAINMENT: asking who serves `10.20.7.0/24` finds
    the branch server declared for `10.20.0.0/16`."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            rows = await registry_service.list_site_servers(
                session,
                ctx.org_id,
                enabled=enabled,
                site_id=site_id,
                segment=segment,
            )
            return [await _out(session, ctx.org_id, row) for row in rows]
    except SiteServerError as exc:
        raise _http_error(exc) from None


@router.post("", response_model=SiteServerOut, status_code=status.HTTP_201_CREATED)
async def promote_route(
    body: SiteServerCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("site.manage")),
) -> SiteServerOut:
    """§3.3. A `device_id` from another org is a 404 about the device — it must
    not confirm that the id exists somewhere else."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await registry_service.promote(
                session,
                ctx.org_id,
                device_id=body.device_id,
                segments=body.segments,
                roles=body.roles,
                name=body.name,
                site_id=body.site_id,
                notes=body.notes,
                actor_id=ctx.user_id,
            )
            return await _out(session, ctx.org_id, row)
    except SiteServerError as exc:
        raise _http_error(exc) from None


@router.get("/{site_server_id}", response_model=SiteServerOut)
async def get_site_server_route(
    site_server_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("site.read")),
) -> SiteServerOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await registry_service.get_site_server(session, ctx.org_id, site_server_id)
            return await _out(session, ctx.org_id, row)
    except SiteServerError as exc:
        raise _http_error(exc) from None


@router.patch("/{site_server_id}", response_model=SiteServerOut)
async def update_site_server_route(
    site_server_id: UUID,
    body: SiteServerUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("site.manage")),
) -> SiteServerOut:
    changes = body.model_dump(exclude_unset=True)
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await registry_service.update_site_server(
                session,
                ctx.org_id,
                site_server_id,
                changes=changes,
                actor_id=ctx.user_id,
            )
            return await _out(session, ctx.org_id, row)
    except SiteServerError as exc:
        raise _http_error(exc) from None


@router.delete("/{site_server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def demote_route(
    site_server_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("site.manage")),
    reason: str = Query(default=""),
) -> None:
    """Demote. **There is no lease to revoke in v1**, because v1 binds no
    socket and issues no `site.serve` lease (§0.3, §7.3)."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            await registry_service.demote(
                session, ctx.org_id, site_server_id, reason=reason, actor_id=ctx.user_id
            )
    except SiteServerError as exc:
        raise _http_error(exc) from None


@router.get("/{site_server_id}/segment-check")
async def segment_check_route(
    site_server_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("site.read")),
) -> dict[str, Any]:
    """§11. Degrades to `evidence: "unavailable"` where IPAM is not installed —
    explicitly, because "no mismatches" and "we could not look" are opposite
    answers and must not render the same."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            check = await segment_check(session, ctx.org_id, site_server_id)
    except SiteServerError as exc:
        raise _http_error(exc) from None
    return {
        "site_server_id": str(check.site_server_id),
        "device_id": str(check.device_id),
        "evidence": check.evidence,
        "matched": check.matched,
        "declared_unobserved": check.declared_unobserved,
        "observed_undeclared": check.observed_undeclared,
    }


@router.post("/{site_server_id}/wake", status_code=status.HTTP_202_ACCEPTED)
async def wake_route(
    site_server_id: UUID,
    body: SiteWakeRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("site.wake")),
) -> dict[str, Any]:
    """`site.wake`, gated on `site.wake` and NOT on `site.manage`.

    The envelope is published only after the `org_session` block has exited: a
    job published before the transaction commits could be handed to an agent
    describing state that rolled back, and this particular job puts a datagram
    on somebody's LAN.
    """
    envelope: dict[str, Any] | None = None
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            job, envelope = await wake_via_site_server(
                session,
                request.app.state.job_engine,
                org_id=ctx.org_id,
                site_server_id=site_server_id,
                target_mac=body.target_mac,
                segment=body.segment,
                actor_id=ctx.user_id,
            )
            out = {
                "job_id": str(job.id),
                "device_id": str(job.target_id),
                "broadcast": job.params["broadcast"],
                "target_mac": job.params["target_mac"],
                "expires_at": job.expires_at.isoformat(),
            }
    except SiteServerError as exc:
        raise _http_error(exc) from None
    if envelope is not None:
        await publish_job(
            request.app.state.redis, UUID(envelope["job"]["device"]), envelope
        )
    return out
