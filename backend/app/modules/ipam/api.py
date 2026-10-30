"""IPAM HTTP routes (`docs/modules/ipam/spec.md` §9, §11.2, §0.13).

**EVERY ROUTE CARRIES A `require_permission(...)` DEPENDENCY**, which is what
makes RBAC completeness DERIVABLE FROM THE ROUTER (`app/core/rbac.py`'s
`route_permission`, invariant #6) rather than from a list somebody has to
remember to update. A route added without one returns `None` there and fails
`tests/test_ipam_api.py`'s completeness test by existing.

**`ipam.sweep.run` AND `ipam.scope.manage` ARE DELIBERATELY SEPARATE** (§11.2).
Running a sweep inside an already-authorized scope is routine technician work;
deciding what may be scoped at all is not. `POST /subnets/{id}/sweep` dispatches
**within already-authorized scope only** -- it calls `create_sweep`, which calls
`authorized_targets` -- and it cannot create or widen an authorization. Collapsed
into one code, anyone who can run a scan could decide what may be scanned, which
is the whole control. `tests/test_ipam_api.py` asserts that split as BEHAVIOUR,
with a technician getting 200 on the first and 403 on the second.

**HANDLERS STAY THIN**: resolve the principal, call the service, map the error.
Business logic lives in the services (`CLAUDE.md`).

**ERRORS ARE MAPPED OUTSIDE THE `org_session` BLOCK, NEVER CAUGHT INSIDE IT.**
Catching inside leaves the failed request's partial writes committed -- the
transaction is the unit of atomicity, and an `except` in the middle of it is a
promise that the half-written state is fine. Every handler therefore lets the
`IpamError` escape the `async with` and maps it afterwards.

**404, NEVER 403, FOR ANOTHER ORG'S ID.** RLS makes that the natural outcome
rather than a hand-written check: the row is not visible, so it does not exist.
A 403 would confirm the id exists, which is a cross-tenant existence oracle.

**`GET /addresses/{id}/history` IS GATED ON `ipam.read` ALONE** (§0.13). It is a
deliberate, narrow projection of the audit log for people who do not hold
`audit.read`; the general audit API keeps requiring `audit.read`. That is
recorded as a decision precisely because "IPAM read implies a slice of audit
read" should not be discovered in a review -- and it is asserted as behaviour,
at the router, because it cannot be asserted below one.

**NOTHING HERE PUBLISHES INSIDE A TENANT TRANSACTION.** `POST
/subnets/{id}/sweep` receives the signed envelope from `create_sweep` and
publishes it only after the `org_session` block has exited, so a job can never
reach an agent describing a run that rolled back.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, require_permission
from app.core.http import read_bounded_body, reject_oversized_content_length
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job
from app.modules.ipam import addresses as address_service
from app.modules.ipam import allocation as allocation_service
from app.modules.ipam import authorization as scope_service
from app.modules.ipam import importer
from app.modules.ipam import observations as observation_service
from app.modules.ipam import reconcile as reconcile_service
from app.modules.ipam import registry as registry_service
from app.modules.ipam import subnets as subnet_service
from app.modules.ipam.errors import (
    IpamAuthorizationError,
    IpamClientError,
    IpamConflictError,
    IpamError,
    IpamNotFoundError,
    IpamTooLargeError,
    IpamValidationError,
)
from app.modules.ipam.models import (
    IpamAddress,
    IpamDrift,
    IpamRange,
    IpamScanAuthorization,
    IpamSubnet,
    IpamSweepRun,
    IpamVlan,
    IpamVrf,
)
from app.modules.ipam.sweep import MIN_SWEEP_INTERVAL_HOURS, create_sweep
from app.modules.ipam.sweep import list_sweep_runs as _list_sweep_runs
from app.services.client_sites import loaded_client

router = APIRouter(prefix="/ipam", tags=["ipam"])


# --------------------------------------------------------------------------- #
# Error mapping -- BY TYPE, never by message text
# --------------------------------------------------------------------------- #


def _http_error(exc: IpamError) -> HTTPException:
    """§9's mapping. The order of these checks is load-bearing: every subclass
    below `IpamValidationError` must be tested before it."""
    if isinstance(exc, IpamAuthorizationError):
        # 403, not 404: the subnet exists and the caller may see it. What is
        # missing is a human's authorization to put packets on that wire (§0.12).
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, IpamNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, IpamTooLargeError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    if isinstance(exc, IpamClientError):
        # The row's client cannot be what the request makes it (MSP clients
        # plan 2026-10-26: a contradiction, a missing client, "Sin asignar").
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    if isinstance(exc, IpamConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, IpamValidationError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _engine(request: Request) -> Any:
    return request.app.state.app_engine


# --------------------------------------------------------------------------- #
# Request / response models.
#
# These live here and NOT in `schema.py`: that file is the AGENT WIRE CONTRACT
# (`NetSweepParams`/`NetSweepDocument`), exported to
# `docs/kernel/wire-protocol/` and built against by a separate repository.
# Mixing HTTP DTOs into it is how an `extra="forbid"` wire model acquires an
# HTTP-shaped field, and how a JSON-Schema export starts describing a REST body.
# `inventory/api.py` and `documentation/api.py` do the same.
# --------------------------------------------------------------------------- #


class _ProjectionOut(BaseModel):
    """Base for every response model built by splatting a service dataclass --
    the `**vars(...)` constructions below. **`extra="forbid"`, deliberately.**

    Pydantic's default is `extra="ignore"`, which makes that splat SILENTLY
    LOSSY: a service dataclass grows a field, the DTO does not, and the field
    stops reaching clients with nothing raised, nothing logged and no test
    failing unless one happens to assert on the response body. That is exactly
    how `HistoryEntryOut` dropped §11.3's `detail` -- claimant and allocation
    method -- for every caller of `GET /addresses/{id}/history`.

    Forbidding extras converts that silent loss into a 500 on the first request,
    which a test suite catches immediately. It is safe here precisely because
    these are RESPONSE models: the only thing ever splatted into them is a
    dataclass this repository owns, never untrusted request input, so "extra"
    can only ever mean "a service field this DTO forgot to declare".

    Request bodies deliberately do NOT inherit this -- rejecting unknown keys
    from a client is a separate API decision, not this one.
    """

    model_config = ConfigDict(extra="forbid")


#: Every IPAM row is a client's (MSP clients plan 2026-10-26 Task 10). A create
#: takes the client from its VRF, or a VLAN from its site; `client_id` is
#: required only where there is neither, and must agree where there is one.
CLIENT_DOC = (
    "The client the row belongs to. Required when no VRF (or, for a VLAN, no site) is "
    "named; otherwise it must be that VRF's or site's client"
)


class VrfCreate(BaseModel):
    client_id: UUID = Field(description="The client the VRF belongs to")
    name: str = Field(min_length=1, max_length=100)
    rd: str | None = Field(default=None, max_length=64)
    description: str = ""


class VrfOut(BaseModel):
    id: UUID
    client_id: UUID
    name: str
    rd: str | None
    description: str


class VlanCreate(BaseModel):
    vid: int = Field(ge=1, le=4094)
    site_id: UUID | None = Field(
        default=None, description="One of the client's sites (`GET /clients/{id}/sites`)"
    )
    client_id: UUID | None = Field(default=None, description=CLIENT_DOC)
    name: str = Field(default="", max_length=100)
    description: str = ""


class VlanOut(BaseModel):
    id: UUID
    client_id: UUID
    vid: int
    site_id: UUID | None
    name: str
    description: str


class SubnetCreate(BaseModel):
    prefix: str = Field(min_length=7, max_length=49)
    vrf_id: UUID | None = None
    client_id: UUID | None = Field(default=None, description=CLIENT_DOC)
    kind: str = Field(default="assignable", pattern="^(container|assignable)$")
    name: str = Field(default="", max_length=200)
    description: str = ""
    site_id: UUID | None = None
    vlan_id: UUID | None = None
    gateway: str | None = None
    dns_servers: list[str] | None = None


class SubnetUpdate(BaseModel):
    """`prefix`, `vrf_id` and `parent_id` are absent for three different reasons
    (`subnets.SUBNET_EDITABLE_FIELDS`): `parent_id` is DERIVED (§0.2) and a
    client-supplied value is exactly the state deriving it exists to make
    unrepresentable; `prefix` and `vrf_id` are the row's identity."""

    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    site_id: UUID | None = None
    vlan_id: UUID | None = None
    gateway: str | None = None
    dns_servers: list[str] | None = None


class SubnetOut(BaseModel):
    id: UUID
    client_id: UUID
    vrf_id: UUID
    prefix: str
    kind: str
    name: str
    description: str
    site_id: UUID | None
    vlan_id: UUID | None
    gateway: str | None
    dns_servers: list[str]
    parent_id: UUID | None
    sweep_enabled: bool
    sweep_device_id: UUID | None
    sweep_interval_hours: int | None
    last_swept_at: datetime | None


class RangeCreate(BaseModel):
    kind: str = Field(pattern="^(dhcp_pool|reserved|excluded)$")
    start_address: str
    end_address: str
    name: str = Field(default="", max_length=200)
    description: str = ""


class RangeOut(BaseModel):
    id: UUID
    client_id: UUID
    subnet_id: UUID
    kind: str
    start_address: str
    end_address: str
    name: str


class UtilizationOut(_ProjectionOut):
    subnet_id: UUID
    prefix: str
    enumerable: bool
    capacity: int | None
    free: int | None
    used: int | None
    percentage: float | None
    assigned: int
    reserved: int
    deprecated: int
    observed: int


class GridEntryOut(_ProjectionOut):
    address: str
    status: str
    state: str | None
    address_id: UUID | None
    device_id: UUID | None
    asset_label: str
    mac: str | None
    hostname: str
    last_seen_at: datetime | None
    range_kinds: list[str]
    #: §3.2, DERIVED at read time (`allocation.derive_allocation_method`), the
    #: same way `status` is, and stored nowhere. `allocation_method_source` is
    #: `documented` when an operator asserted it, `observed` when it came from
    #: what an endpoint reported about itself, `unknown` when nobody has said.
    #: Two fields rather than one because §0.6's whole thesis is that "a human
    #: asserted this" and "the fleet reported this" are different facts.
    allocation_method: str
    allocation_method_source: str


class AllocateRequest(BaseModel):
    device_id: UUID | None = None
    asset_label: str = ""
    hostname: str = ""
    description: str = ""
    allocation_method: str = Field(default="static", max_length=12)


class AddressCreate(BaseModel):
    """`state` is ABSENT (§0.6). Reserve and assign are separate transitions with
    separate predicates and separate audit events; a `state` field here would be
    a bypass of both."""

    address: str
    vrf_id: UUID | None = None
    client_id: UUID | None = Field(default=None, description=CLIENT_DOC)
    reserve: bool = False
    device_id: UUID | None = None
    asset_label: str = ""
    hostname: str = ""
    description: str = ""
    mac: str | None = None
    allocation_method: str = Field(default="static", max_length=12)


class AddressUpdate(BaseModel):
    hostname: str | None = None
    description: str | None = None
    asset_label: str | None = None
    allocation_method: str | None = None


class AddressOut(BaseModel):
    id: UUID
    client_id: UUID
    vrf_id: UUID
    subnet_id: UUID | None
    address: str
    state: str
    origin: str
    allocation_method: str
    device_id: UUID | None
    asset_label: str
    mac: str | None
    hostname: str
    description: str
    assigned_at: datetime | None
    released_at: datetime | None


class HistoryEntryOut(_ProjectionOut):
    """§0.13's projection row, and §11.3's payload with it.

    **`detail` IS PART OF THE CONTRACT, NOT A DEBUG EXTRA.** §11.3 specifies
    claimant + method on `ipam.address_assigned` and the previous claimant on
    `ipam.address_released`; without it the history says an address changed
    state and refuses to say who to or how, which is the one question the page
    exists to answer. It carries `AuditEvent.meta` minus `from_state`/`to_state`
    -- those are columns of this row already, and duplicating them would give
    the frontend two places to read one fact.

    Every key inside it was written by this module, in-org: `address`,
    `subnet_id` and `vrf_id` are resolved through org-scoped loads, and
    `claimant` is either an `asset_label` or a `device_id` that
    `allocation.claim_address` proved belongs to `org_id`. Widening the response
    therefore does not widen the projection: it is still one org, still one
    `target_id`, and still no field naming another tenant.
    """

    ts: datetime
    actor_type: str
    actor_id: UUID | None
    action: str
    from_state: str | None
    to_state: str | None
    detail: dict[str, Any]


class SearchOut(_ProjectionOut):
    """§9's one box. `kind` says HOW `q` WAS INTERPRETED, because an operator who
    typed a hostname that happens to parse as something else needs to see which
    question was answered -- ids and counts, exactly as `addresses.SearchResult`
    produces them, so the API cannot claim a richer answer than the service
    computed."""

    kind: str
    query: str
    subnets: list[UUID]
    addresses: list[str]
    observations: int
    devices: list[UUID]


class DriftOut(BaseModel):
    id: UUID
    client_id: UUID
    vrf_id: UUID
    subnet_id: UUID | None
    address: str
    drift_class: str
    severity: str
    confidence: str
    detail: dict[str, Any]
    device_id: UUID | None
    first_detected_at: datetime
    last_evaluated_at: datetime
    consecutive_detections: int
    resolved_at: datetime | None
    acknowledged_at: datetime | None
    suppressed_until: datetime | None
    note: str


class AcknowledgeRequest(BaseModel):
    note: str = Field(default="", max_length=2000)


class SuppressRequest(BaseModel):
    until: datetime
    note: str = Field(min_length=1, max_length=2000)


class OrphanOut(_ProjectionOut):
    prefix: str
    count: int
    last_seen_at: datetime


class ScopeCreate(BaseModel):
    prefix: str = Field(min_length=7, max_length=49)
    vrf_id: UUID | None = None
    client_id: UUID | None = Field(default=None, description=CLIENT_DOC)
    note: str = Field(default="", max_length=500)
    expires_at: datetime | None = None


class ScopeOut(BaseModel):
    id: UUID
    client_id: UUID
    vrf_id: UUID
    prefix: str
    note: str
    authorized_by: UUID
    authorized_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


class SweepSettings(BaseModel):
    """§0.10 point 6's clamp, restated as a schema bound so the refusal is a 422
    on shape and a 400 on intent -- both refuse, and neither lets a one-minute
    sweep interval through."""

    sweep_enabled: bool
    sweep_device_id: UUID | None = None
    sweep_interval_hours: int | None = None


class SweepRunOut(BaseModel):
    id: UUID
    client_id: UUID
    subnet_id: UUID
    job_id: UUID | None
    device_id: UUID | None
    state: str
    trigger: str
    target_count: int
    scope_digest: str
    hosts_found: int
    truncated: bool
    started_at: datetime
    finished_at: datetime | None
    error: str


# --------------------------------------------------------------------------- #
# Response builders -- one per shape, so no two routes can disagree
# --------------------------------------------------------------------------- #


def _vrf_out(row: IpamVrf) -> VrfOut:
    return VrfOut(
        id=row.id, client_id=row.client_id, name=row.name, rd=row.rd,
        description=row.description,
    )


def _vlan_out(row: IpamVlan) -> VlanOut:
    return VlanOut(
        id=row.id, client_id=row.client_id, vid=row.vid, site_id=row.site_id, name=row.name,
        description=row.description,
    )


def _subnet_out(row: IpamSubnet) -> SubnetOut:
    return SubnetOut(
        id=row.id,
        client_id=row.client_id,
        vrf_id=row.vrf_id,
        prefix=str(row.prefix),
        kind=row.kind,
        name=row.name,
        description=row.description,
        site_id=row.site_id,
        vlan_id=row.vlan_id,
        gateway=str(row.gateway) if row.gateway else None,
        dns_servers=list(row.dns_servers or []),
        parent_id=row.parent_id,
        sweep_enabled=row.sweep_enabled,
        sweep_device_id=row.sweep_device_id,
        sweep_interval_hours=row.sweep_interval_hours,
        last_swept_at=row.last_swept_at,
    )


def _range_out(row: IpamRange) -> RangeOut:
    return RangeOut(
        id=row.id, client_id=row.client_id, subnet_id=row.subnet_id, kind=row.kind,
        start_address=str(row.start_address), end_address=str(row.end_address),
        name=row.name,
    )


def _address_out(row: IpamAddress) -> AddressOut:
    return AddressOut(
        id=row.id,
        client_id=row.client_id,
        vrf_id=row.vrf_id,
        subnet_id=row.subnet_id,
        address=str(row.address),
        state=row.state,
        origin=row.origin,
        allocation_method=row.allocation_method,
        device_id=row.device_id,
        asset_label=row.asset_label,
        mac=row.mac,
        hostname=row.hostname,
        description=row.description,
        assigned_at=row.assigned_at,
        released_at=row.released_at,
    )


def _drift_out(row: IpamDrift) -> DriftOut:
    return DriftOut(
        id=row.id,
        client_id=row.client_id,
        vrf_id=row.vrf_id,
        subnet_id=row.subnet_id,
        address=str(row.address),
        drift_class=row.drift_class,
        severity=row.severity,
        confidence=row.confidence,
        detail=dict(row.detail or {}),
        device_id=row.device_id,
        first_detected_at=row.first_detected_at,
        last_evaluated_at=row.last_evaluated_at,
        consecutive_detections=row.consecutive_detections,
        resolved_at=row.resolved_at,
        acknowledged_at=row.acknowledged_at,
        suppressed_until=row.suppressed_until,
        note=row.note,
    )


def _scope_out(row: IpamScanAuthorization) -> ScopeOut:
    return ScopeOut(
        id=row.id,
        client_id=row.client_id,
        vrf_id=row.vrf_id,
        prefix=str(row.prefix),
        note=row.note,
        authorized_by=row.authorized_by,
        authorized_at=row.authorized_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


def _run_out(row: IpamSweepRun) -> SweepRunOut:
    return SweepRunOut(
        id=row.id,
        client_id=row.client_id,
        subnet_id=row.subnet_id,
        job_id=row.job_id,
        device_id=row.device_id,
        state=row.state,
        trigger=row.trigger,
        target_count=row.target_count,
        scope_digest=row.scope_digest,
        hosts_found=row.hosts_found,
        truncated=row.truncated,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
    )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@router.get("/vrfs", response_model=list[VrfOut])
async def list_vrfs(
    request: Request, ctx: AuthContext = Depends(require_permission("ipam.read"))
) -> list[VrfOut]:
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        rows = await registry_service.list_vrfs(session, ctx.org_id)
        return [_vrf_out(row) for row in rows]


@router.post("/vrfs", response_model=VrfOut, status_code=status.HTTP_201_CREATED)
async def create_vrf(
    body: VrfCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.manage")),
) -> VrfOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await registry_service.create_vrf(
                session, ctx.org_id, ctx.user_id, client_id=body.client_id,
                name=body.name, rd=body.rd, description=body.description,
            )
            out = _vrf_out(row)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/vlans", response_model=list[VlanOut])
async def list_vlans(
    request: Request,
    site_id: UUID | None = Query(default=None),
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> list[VlanOut]:
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        rows = await registry_service.list_vlans(session, ctx.org_id, site_id=site_id)
        return [_vlan_out(row) for row in rows]


@router.post("/vlans", response_model=VlanOut, status_code=status.HTTP_201_CREATED)
async def create_vlan(
    body: VlanCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.manage")),
) -> VlanOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await registry_service.create_vlan(
                session, ctx.org_id, ctx.user_id,
                vid=body.vid, client_id=body.client_id, site_id=body.site_id, name=body.name,
                description=body.description,
            )
            out = _vlan_out(row)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# Subnets
# --------------------------------------------------------------------------- #


@router.get("/subnets", response_model=list[SubnetOut])
async def list_subnets(
    request: Request,
    vrf: UUID | None = Query(default=None),
    site: UUID | None = Query(default=None),
    vlan: UUID | None = Query(default=None),
    contains: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=200),
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> list[SubnetOut]:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            rows = await subnet_service.list_subnets(
                session, ctx.org_id, vrf_id=vrf, site_id=site, vlan_id=vlan,
                contains=contains, q=q,
            )
            out = [_subnet_out(row) for row in rows]
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/subnets", response_model=SubnetOut, status_code=status.HTTP_201_CREATED)
async def create_subnet(
    body: SubnetCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.manage")),
) -> SubnetOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await subnet_service.create_subnet(
                session, ctx.org_id, ctx.user_id,
                prefix=body.prefix, vrf_id=body.vrf_id, client_id=body.client_id,
                kind=body.kind,
                name=body.name, description=body.description, site_id=body.site_id,
                vlan_id=body.vlan_id, gateway=body.gateway,
                dns_servers=body.dns_servers,
            )
            out = _subnet_out(row)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/subnets/{subnet_id}", response_model=SubnetOut)
async def get_subnet(
    subnet_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> SubnetOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            out = _subnet_out(
                await subnet_service.get_subnet(session, ctx.org_id, subnet_id)
            )
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.put("/subnets/{subnet_id}", response_model=SubnetOut)
async def update_subnet(
    subnet_id: UUID,
    body: SubnetUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.manage")),
) -> SubnetOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await subnet_service.update_subnet(
                session, ctx.org_id, ctx.user_id, subnet_id,
                body.model_dump(exclude_unset=True),
            )
            out = _subnet_out(row)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.delete("/subnets/{subnet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subnet(
    subnet_id: UUID,
    request: Request,
    cascade: bool = Query(default=False),
    ctx: AuthContext = Depends(require_permission("ipam.manage")),
) -> None:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            await subnet_service.delete_subnet(
                session, ctx.org_id, ctx.user_id, subnet_id, cascade=cascade
            )
    except IpamError as exc:
        raise _http_error(exc) from None


@router.get("/subnets/{subnet_id}/addresses", response_model=list[GridEntryOut])
async def subnet_addresses(
    subnet_id: UUID,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=256, ge=1, le=1024),
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> list[GridEntryOut]:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            entries = await allocation_service.address_grid(
                session, ctx.org_id, subnet_id, offset=offset, limit=limit
            )
            out = [GridEntryOut(**vars(entry)) for entry in entries]
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/subnets/{subnet_id}/utilization", response_model=UtilizationOut)
async def subnet_utilization(
    subnet_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> UtilizationOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            out = UtilizationOut(
                **vars(await allocation_service.utilization(session, ctx.org_id, subnet_id))
            )
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/subnets/{subnet_id}/next-free", response_model=list[str])
async def subnet_next_free(
    subnet_id: UUID,
    request: Request,
    count: int = Query(default=1, ge=1, le=allocation_service.MAX_NEXT_FREE),
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> list[str]:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            out = await allocation_service.next_free(session, ctx.org_id, subnet_id, count)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.post(
    "/subnets/{subnet_id}/allocate",
    response_model=AddressOut,
    status_code=status.HTTP_201_CREATED,
)
async def subnet_allocate(
    subnet_id: UUID,
    body: AllocateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.assign")),
) -> AddressOut:
    """§3.3's NON-RACING path. `next-free` is advisory -- two operators who both
    call it race for `.42`; this picks and claims in one transaction under the
    §0.3 advisory lock and returns the address it took."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await allocation_service.allocate(
                session, ctx.org_id, subnet_id, actor_id=ctx.user_id,
                device_id=body.device_id, asset_label=body.asset_label,
                hostname=body.hostname, description=body.description,
                allocation_method=body.allocation_method,
            )
            out = _address_out(row)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/subnets/{subnet_id}/ranges", response_model=list[RangeOut])
async def list_ranges(
    subnet_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> list[RangeOut]:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            rows = await subnet_service.list_ranges(session, ctx.org_id, subnet_id)
            out = [_range_out(row) for row in rows]
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.post(
    "/subnets/{subnet_id}/ranges",
    response_model=RangeOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_range(
    subnet_id: UUID,
    body: RangeCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.manage")),
) -> RangeOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await subnet_service.create_range(
                session, ctx.org_id, ctx.user_id, subnet_id,
                kind=body.kind, start_address=body.start_address,
                end_address=body.end_address, name=body.name,
                description=body.description,
            )
            out = _range_out(row)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.delete(
    "/subnets/{subnet_id}/ranges/{range_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_range(
    subnet_id: UUID,
    range_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.manage")),
) -> None:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            # The subnet is resolved first so a range id from another subnet --
            # or another org -- is a 404 about the SUBNET, not a delete that
            # quietly succeeds against a row the caller did not name.
            await subnet_service.get_subnet(session, ctx.org_id, subnet_id)
            rows = await subnet_service.list_ranges(session, ctx.org_id, subnet_id)
            if range_id not in {row.id for row in rows}:
                raise IpamNotFoundError(
                    tr("range {range_id} not found in this subnet", range_id=range_id)
                )
            await subnet_service.delete_range(session, ctx.org_id, ctx.user_id, range_id)
    except IpamError as exc:
        raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #


@router.get("/addresses/{address_id}", response_model=AddressOut)
async def get_address(
    address_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> AddressOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            out = _address_out(
                await address_service.get_address(session, ctx.org_id, address_id)
            )
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/addresses", response_model=AddressOut, status_code=status.HTTP_201_CREATED)
async def create_address(
    body: AddressCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.assign")),
) -> AddressOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            if body.reserve:
                row = await address_service.reserve(
                    session, ctx.org_id, ctx.user_id,
                    address=body.address, vrf_id=body.vrf_id, client_id=body.client_id,
                    hostname=body.hostname, description=body.description,
                )
            else:
                row = await address_service.assign(
                    session, ctx.org_id, ctx.user_id,
                    address=body.address, vrf_id=body.vrf_id, client_id=body.client_id,
                    device_id=body.device_id, asset_label=body.asset_label,
                    hostname=body.hostname, description=body.description,
                    mac=body.mac, allocation_method=body.allocation_method,
                )
            out = _address_out(row)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.put("/addresses/{address_id}", response_model=AddressOut)
async def update_address(
    address_id: UUID,
    body: AddressUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.assign")),
) -> AddressOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await address_service.update_address(
                session, ctx.org_id, ctx.user_id, address_id,
                body.model_dump(exclude_unset=True),
            )
            out = _address_out(row)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/addresses/{address_id}/release", response_model=AddressOut)
async def release_address(
    address_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.assign")),
) -> AddressOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            out = _address_out(
                await address_service.release(session, ctx.org_id, ctx.user_id, address_id)
            )
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/addresses/{address_id}/history", response_model=list[HistoryEntryOut])
async def address_history(
    address_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> list[HistoryEntryOut]:
    """§0.13: gated on `ipam.read` ALONE, deliberately.

    There is no `ipam_address_history` table -- assign, release, reserve and
    state-change all write hash-chained audit events (invariant #4), which is
    tamper-evident, and a second history table would be neither. This is a
    NARROW read of that log for people who do not hold `audit.read`; the general
    audit API keeps requiring it.
    """
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            entries = await address_service.address_history(session, ctx.org_id, address_id)
            out = [HistoryEntryOut(**vars(entry)) for entry in entries]
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/search", response_model=SearchOut)
async def search(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> SearchOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            out = SearchOut(**vars(await address_service.search(session, ctx.org_id, q)))
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# Drift and orphans
# --------------------------------------------------------------------------- #


@router.get("/drift", response_model=list[DriftOut])
async def list_drift(
    request: Request,
    drift_class: str | None = Query(default=None, alias="class", max_length=24),
    severity: str | None = Query(default=None, max_length=8),
    subnet: UUID | None = Query(default=None),
    include_resolved: bool = Query(default=False),
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> list[DriftOut]:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            rows = await reconcile_service.list_drift(
                session, ctx.org_id, drift_class=drift_class, severity=severity,
                subnet_id=subnet, include_resolved=include_resolved,
            )
            out = [_drift_out(row) for row in rows]
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/drift/{drift_id}/acknowledge", response_model=DriftOut)
async def acknowledge_drift(
    drift_id: UUID,
    body: AcknowledgeRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.assign")),
) -> DriftOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            out = _drift_out(
                await reconcile_service.acknowledge_drift(
                    session, ctx.org_id, actor_id=ctx.user_id, drift_id=drift_id,
                    note=body.note,
                )
            )
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/drift/{drift_id}/suppress", response_model=DriftOut)
async def suppress_drift(
    drift_id: UUID,
    body: SuppressRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.assign")),
) -> DriftOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            out = _drift_out(
                await reconcile_service.suppress_drift(
                    session, ctx.org_id, actor_id=ctx.user_id, drift_id=drift_id,
                    until=body.until, note=body.note,
                )
            )
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/orphans", response_model=list[OrphanOut])
async def list_orphans(
    request: Request, ctx: AuthContext = Depends(require_permission("ipam.read"))
) -> list[OrphanOut]:
    """§4.2. NOT drift: nothing is wrong with the plan, the plan is ABSENT --
    which is why this is a separate list with its own page."""
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        groups = await observation_service.orphan_groups(session, ctx.org_id)
        return [OrphanOut(**vars(group)) for group in groups]


# --------------------------------------------------------------------------- #
# Sweep governance -- THE HIGH-PRIVILEGE SURFACE (§6.1, §11.2)
# --------------------------------------------------------------------------- #


@router.get("/scan-authorizations", response_model=list[ScopeOut])
async def list_scan_authorizations(
    request: Request,
    include_revoked: bool = Query(default=False),
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> list[ScopeOut]:
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        rows = await scope_service.list_authorizations(
            session, ctx.org_id, include_revoked=include_revoked
        )
        return [_scope_out(row) for row in rows]


@router.post(
    "/scan-authorizations", response_model=ScopeOut, status_code=status.HTTP_201_CREATED
)
async def create_scan_authorization(
    body: ScopeCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.scope.manage")),
) -> ScopeOut:
    """**HIGH PRIVILEGE, AND `ipam.scope.manage` ONLY** (§11.2).

    This grant decides where this platform emits network traffic. It is
    deliberately not held by `technician`, who can still dispatch a sweep inside
    a scope somebody else authorized.
    """
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await scope_service.create_authorization(
                session, ctx.org_id, actor_id=ctx.user_id, vrf_id=body.vrf_id,
                client_id=body.client_id, prefix=body.prefix, note=body.note,
                expires_at=body.expires_at,
            )
            out = _scope_out(row)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.delete(
    "/scan-authorizations/{authorization_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_scan_authorization(
    authorization_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.scope.manage")),
) -> None:
    """Revocation STAMPS; the row stays, so "who authorized probing that segment
    in March" remains answerable."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            await scope_service.revoke_authorization(
                session, ctx.org_id, actor_id=ctx.user_id,
                authorization_id=authorization_id,
            )
    except IpamError as exc:
        raise _http_error(exc) from None


@router.post(
    "/subnets/{subnet_id}/sweep",
    response_model=SweepRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def dispatch_sweep(
    subnet_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.sweep.run")),
) -> SweepRunOut:
    """§11.2: `ipam.sweep.run`, and it dispatches WITHIN ALREADY-AUTHORIZED SCOPE
    ONLY -- it calls `create_sweep`, which calls `authorized_targets`. It cannot
    create or widen an authorization; that is `ipam.scope.manage`'s job and a
    different person's decision.

    The envelope is published only after the `org_session` block has exited: a
    job published before the transaction commits could be handed to an agent
    with no durable server record if the commit later failed.

    **AND THE REFUSAL IS RAISED AFTER THE BLOCK HAS EXITED, FOR THE MIRROR-IMAGE
    REASON.** §6.3 records `state='refused'` on the near side of the signing
    boundary precisely so an operator can see that somebody asked to probe a
    segment nobody authorized. `org_session` wraps this block in
    `session.begin()`, which rolls back on any exception leaving it -- so while
    `create_sweep` raised, that row was written and thrown away on every request
    this route ever served, and the 403 was the only trace. A refusal nobody can
    audit is indistinguishable from a request nobody made (invariant #4).

    `create_sweep` therefore RETURNS the refusal and this raises it here, one
    statement after the commit. Same discipline as `agent_push`'s
    `POST /requests`.
    """
    envelope: dict[str, Any] | None = None
    refusal: IpamAuthorizationError | None = None
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            run, envelope, refusal = await create_sweep(
                session,
                request.app.state.job_engine,
                ctx.org_id,
                subnet_id=subnet_id,
                actor_id=ctx.user_id,
                trigger="manual",
            )
            await loaded_client(session, run)  # the run's later UPDATEs expired it
            out = _run_out(run)
    except IpamError as exc:
        raise _http_error(exc) from None
    if refusal is not None:
        # THE TRANSACTION HAS COMMITTED. The `refused` run row is durable, and
        # only now may the 403 be raised.
        raise _http_error(refusal)
    if envelope is not None:
        await publish_job(
            request.app.state.redis, UUID(envelope["job"]["device"]), envelope
        )
    return out


@router.put("/subnets/{subnet_id}/sweep-settings", response_model=SubnetOut)
async def update_sweep_settings(
    subnet_id: UUID,
    body: SweepSettings,
    request: Request,
    ctx: AuthContext = Depends(require_permission("ipam.sweep.manage")),
) -> SubnetOut:
    """§0.10 point 6: `sweep_interval_hours` is CLAMPED TO >= 1 AND ANYTHING
    SHORTER IS REFUSED WITH A 400.

    A one-minute sweep interval is the exact mechanism by which an IPAM becomes
    a ping monitor. The refusal is here, in `ck_ipam_subnet_sweep_interval`, and
    in `due_subnet_ids`' `GREATEST(..., 1)` -- three layers, because the
    application check is one refactor away from being the only one.
    """
    if (
        body.sweep_interval_hours is not None
        and body.sweep_interval_hours < MIN_SWEEP_INTERVAL_HOURS
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            tr(
                "sweep_interval_hours must be at least {min_sweep_interval_hours} (§0.10: "
                "a sub-hourly sweep interval is how an address plan becomes a ping monitor)",
                min_sweep_interval_hours=MIN_SWEEP_INTERVAL_HOURS,
            ),
        )
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await subnet_service.set_sweep_settings(
                session, ctx.org_id, ctx.user_id, subnet_id,
                sweep_enabled=body.sweep_enabled,
                sweep_device_id=body.sweep_device_id,
                sweep_interval_hours=body.sweep_interval_hours,
            )
            out = _subnet_out(row)
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/subnets/{subnet_id}/sweep-runs", response_model=list[SweepRunOut])
async def list_sweep_runs(
    subnet_id: UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> list[SweepRunOut]:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            rows = await _list_sweep_runs(session, ctx.org_id, subnet_id, limit=limit)
            out = [_run_out(row) for row in rows]
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# Import / export (§8)
# --------------------------------------------------------------------------- #


class ImportRowOut(_ProjectionOut):
    line: int
    action: str
    address: str
    error: str
    detail: dict[str, Any]


class ImportPreviewOut(_ProjectionOut):
    digest: str
    total_rows: int
    creates: int
    updates: int
    conflicts: int
    errors: int
    rows: list[ImportRowOut]


class ImportResultOut(_ProjectionOut):
    digest: str
    total_rows: int
    applied: int
    errors: int
    rows: list[ImportRowOut]


class ConvertedRecordOut(_ProjectionOut):
    record_id: UUID
    name: str
    prefix: str
    subnet_id: UUID | None
    vlan_id: UUID | None
    warnings: list[str]


class SkippedRecordOut(_ProjectionOut):
    record_id: UUID
    name: str
    reason: str


class DocRecordConversionOut(BaseModel):
    dry_run: bool
    converted: list[ConvertedRecordOut]
    skipped: list[SkippedRecordOut]


def _mapping(raw: str) -> dict[str, str]:
    """The `{csv_column: ipam_field}` mapping, carried as a JSON form field
    because the body is multipart."""
    import json

    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, tr("mapping must be a JSON object: {exc}", exc=exc)
        ) from None
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            tr("mapping must be a JSON object of {{csv_column: ipam_field}}"),
        )
    return dict(parsed)


async def _read_csv(request: Request) -> bytes:
    """The CSV, as the RAW REQUEST BODY.

    **§8.1 says "multipart", and this codebase has no multipart parser.**
    `python-multipart` is not installed and adding it is a new dependency the
    plan's tech-stack line rules out; `app/core/http.py`'s docstring records that
    two other modules -- budget's expense attachment and deploy's package
    artifact -- hit the same wall and both take the raw body instead. This
    follows that shipped precedent rather than inventing a third answer, and
    every property §8.1 actually asks for survives: two steps, a digest binding
    them, a 10 MB cap, a 50,000-row cap, and a verdict per row.

    BOTH bounds are applied, in order: the declared `Content-Length` first
    (cheap, catches the common case before any DB work), then the bytes actually
    read off the wire -- a chunked request carries no `Content-Length` at all, so
    the header check alone can be bypassed by a client that simply omits it.
    """
    reject_oversized_content_length(request, importer.MAX_IMPORT_BYTES)
    return await read_bounded_body(request, importer.MAX_IMPORT_BYTES)


#: Where an import reads and writes: a VRF (`vrf_id`), whose client is the
#: import's, or a client (`client_id`), whose `global` VRF holds the sheet.
IMPORT_VRF_DOC = "The VRF the sheet is imported into; its client is the import's"
IMPORT_CLIENT_DOC = (
    "Without a VRF: the client whose global VRF the sheet is imported into -- one the "
    "caller can see, active, and not Unassigned. With one, it must be the VRF's"
)


@router.post("/import/preview", response_model=ImportPreviewOut)
async def import_preview(
    request: Request,
    mapping: str = Query(..., max_length=4000),
    vrf_id: UUID | None = Query(default=None, description=IMPORT_VRF_DOC),
    client_id: UUID | None = Query(default=None, description=IMPORT_CLIENT_DOC),
    ctx: AuthContext = Depends(require_permission("ipam.manage")),
) -> ImportPreviewOut:
    """§8.1 step one. **WRITES NOTHING.**

    The digest it returns covers the file AND the mapping, and is what `apply`
    must echo. It proves the two calls name the same inputs; it does NOT prove a
    preview happened, because no preview is persisted -- see
    `importer.apply_import` and this module's `importer` docstring for why that
    trade was taken deliberately rather than closed with a table.

    THE REQUEST NAMES WHERE IT GOES (MSP clients plan 2026-10-26 Task 11): a VRF,
    whose client is the import's -- how a technician limited to some clients
    names one of theirs -- or a client, for its `global` VRF. A VRF or client
    the caller cannot see is a 404, an archived client 409, "Sin asignar" or a
    client contradicting the VRF 422, naming neither 422. In that order: the
    body is read and bounded and the mapping parsed first (413 and 400 come
    before them), then the target is checked -- before any row, the `global`
    VRF included, is written.
    """
    content = await _read_csv(request)
    parsed = _mapping(mapping)
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            preview = await importer.preview_import(
                session, ctx.org_id, vrf_id=vrf_id, client_id=client_id, content=content,
                mapping=parsed,
            )
            out = ImportPreviewOut(
                **{**vars(preview), "rows": [ImportRowOut(**vars(r)) for r in preview.rows]}
            )
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/import/apply", response_model=ImportResultOut)
async def import_apply(
    request: Request,
    mapping: str = Query(..., max_length=4000),
    digest: str = Query(..., min_length=64, max_length=64),
    vrf_id: UUID | None = Query(default=None, description=IMPORT_VRF_DOC),
    client_id: UUID | None = Query(default=None, description=IMPORT_CLIENT_DOC),
    ctx: AuthContext = Depends(require_permission("ipam.manage")),
) -> ImportResultOut:
    """§8.1 step two: ONE transaction. A failure on the last row leaves ZERO
    rows written, because the `org_session` block is the transaction and the
    error escapes it. Into the VRF or client the request names, checked as
    `import_preview` says, after the body and mapping and before any write."""
    content = await _read_csv(request)
    parsed = _mapping(mapping)
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            result = await importer.apply_import(
                session, ctx.org_id, actor_id=ctx.user_id, vrf_id=vrf_id, client_id=client_id,
                content=content, mapping=parsed, digest=digest,
            )
            out = ImportResultOut(
                **{**vars(result), "rows": [ImportRowOut(**vars(r)) for r in result.rows]}
            )
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/import/doc-records", response_model=DocRecordConversionOut)
async def import_doc_records(
    request: Request,
    dry_run: bool = Query(default=True),
    ctx: AuthContext = Depends(require_permission("ipam.manage")),
) -> DocRecordConversionOut:
    """§8.3. **`dry_run` DEFAULTS TO TRUE**, and the default is the safe one: a
    call with no query parameter reports what WOULD convert and writes nothing.

    This is deliberately not a data migration -- a silent conversion would hit
    unparseable CIDRs and duplicate prefixes with no operator to ask.
    """
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            conversion = await importer.convert_doc_records(
                session, ctx.org_id, actor_id=ctx.user_id, dry_run=dry_run
            )
            out = DocRecordConversionOut(
                dry_run=conversion.dry_run,
                converted=[ConvertedRecordOut(**vars(row)) for row in conversion.converted],
                skipped=[SkippedRecordOut(**vars(row)) for row in conversion.skipped],
            )
    except IpamError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/export/subnets.csv", response_class=PlainTextResponse)
async def export_subnets(
    request: Request, ctx: AuthContext = Depends(require_permission("ipam.read"))
) -> PlainTextResponse:
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        body = await importer.export_subnets_csv(session, ctx.org_id)
    return PlainTextResponse(body, media_type="text/csv")


@router.get("/export/addresses.csv", response_class=PlainTextResponse)
async def export_addresses(
    request: Request, ctx: AuthContext = Depends(require_permission("ipam.read"))
) -> PlainTextResponse:
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        body = await importer.export_addresses_csv(session, ctx.org_id)
    return PlainTextResponse(body, media_type="text/csv")


@router.get("/export/drift.csv", response_class=PlainTextResponse)
async def export_drift(
    request: Request,
    include_resolved: bool = Query(default=False),
    ctx: AuthContext = Depends(require_permission("ipam.read")),
) -> PlainTextResponse:
    """§4.3: `dormant` IS EXCLUDED. It is not drift, and an export called "drift"
    that contained it would be the mixed list §0.11 says nobody opens twice."""
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        body = await importer.export_drift_csv(
            session, ctx.org_id, include_resolved=include_resolved
        )
    return PlainTextResponse(body, media_type="text/csv")
