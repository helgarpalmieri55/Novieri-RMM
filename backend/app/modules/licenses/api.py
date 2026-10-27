"""Licenses/SAM REST API (docs/modules/licenses/spec.md §10).

Routes are a mix of top-level ``/vendors`` paths and ``/licenses``-prefixed
paths (no single router prefix); ``app.modules.licenses.register`` mounts
this router under ``/api/v1``. Every route is gated server-side by
``require_permission`` (invariant #6, the frontend never gates security);
``org_id``/``actor_id`` always come from ``AuthContext``, never the request
body/path (invariant #5).

Like ``bcp``/``change_mgmt``, this module dispatches no agent jobs --
``register(app)`` mounts this router only, there is no job-type registration
and no gateway edit.

Every ``LicenseValidationError`` raised by the service layer is translated to
an HTTP 4xx here -- it must never surface as a 500, and the translation is by
TYPE, not by matching exception-message text. ``LicenseNotFoundError`` (a
``LicenseValidationError`` subclass, raised for a missing vendor/product/
license/allocation, or a client the caller cannot see) becomes a 404;
``LicenseConflictError`` (an archived client) a 409; ``LicenseClientError``
(another client's allocation target, "Sin asignar" as a client) a 422; every
other ``LicenseValidationError`` (an invalid match rule, a cross-org/
nonexistent ``product_id``/``vendor_id``, an unknown allocation target, an
exhausted license pool, an already-released allocation) becomes a 400.

A license is one client's (plan 2026-10-26 Task 6): the reports return one
row per client -- compliance per (client, product), costs per (client,
product, currency) -- for the clients the caller's scope reaches.

``GET /licenses/{id}/key`` is the one deliberately sensitive route: it is
gated by the distinct ``licenses.keys.read`` permission (least-privilege,
narrower than ``licenses.manage``) and calls ``reveal_key``, which itself
writes the ``license.key_revealed`` audit event (invariant #4) -- the route
does not audit a second time. ``GET /licenses/export`` is the module's other
audited read: it writes ``license.export`` before returning, mirroring
``change_mgmt``'s evidence-export precedent. Every other read here (list,
detail, compliance, review-queue, renewals, costs) records no audit event at
all -- merely viewing must never be indistinguishable from exporting/
revealing in the audit log (the Phase-9 no-audit-on-view lesson); the service
layer's own mutations (create/update/delete/allocate/release/map/ignore)
already audit themselves, so routes never double-audit those either.

Route registration order matters: the static ``/licenses/...`` segments
(``products``, ``compliance``, ``review-queue`` (+ its ``/map``/``/ignore``
sub-paths), ``renewals``, ``costs``, ``export``) are declared before
``GET|PATCH|DELETE /licenses/{license_id}`` and its sub-routes, so FastAPI
never tries to parse one of those literal segments as a license id.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import License, LicenseAllocation, SoftwareProduct, Vendor
from app.modules.licenses.errors import (
    LicenseClientError,
    LicenseConflictError,
    LicenseNotFoundError,
    LicenseValidationError,
)
from app.modules.licenses.reporting import (
    compliance_summary,
    cost_view,
    export_rows,
    renewal_calendar,
    reported_clients,
)
from app.modules.licenses.schema import (
    AllocationCreate,
    AllocationOut,
    ComplianceRow,
    CostRow,
    LicenseCreate,
    LicenseKeyOut,
    LicenseOut,
    LicenseUpdate,
    ProductCreate,
    ProductOut,
    ProductUpdate,
    RenewalRow,
    ReviewIgnoreOut,
    ReviewIgnoreRequest,
    ReviewMapRequest,
    ReviewQueueRow,
    VendorCreate,
    VendorOut,
    VendorUpdate,
)
from app.modules.licenses.service import (
    allocate,
    create_license,
    create_product,
    create_vendor,
    delete_license,
    get_license,
    get_product,
    get_vendor,
    ignore_software,
    list_allocations,
    list_licenses,
    list_products,
    list_vendors,
    map_software,
    release_allocation,
    reveal_key,
    review_queue,
    update_license,
    update_product,
    update_vendor,
)

router = APIRouter(tags=["licenses"])


def _http_error(exc: LicenseValidationError) -> HTTPException:
    """Translate a `LicenseValidationError` to an HTTP 4xx -- it must never
    become a 500.

    Routing is by TYPE, never by matching exception-message prose (a wording
    tweak in the service layer must never silently mis-route a status code).
    `LicenseNotFoundError` -- raised for "the resource named in the URL path
    doesn't exist under this org" (a vendor, a product, a license, an
    allocation), and for a client this caller cannot see -- becomes a 404.
    `LicenseConflictError` (an archived client) is a 409, and
    `LicenseClientError` (one client's row naming another's) a 422. Every
    other `LicenseValidationError` is a 400 -- including an invalid match
    rule, a cross-org/nonexistent `product_id`/`vendor_id`, an unknown
    allocation target, an exhausted license pool, and an already-released
    allocation.
    """
    if isinstance(exc, LicenseNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, LicenseConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, LicenseClientError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _vendor_out(row: Vendor) -> VendorOut:
    return VendorOut(
        id=row.id,
        name=row.name,
        website=row.website,
        contacts=row.contacts,
        created_at=row.created_at,
    )


def _product_out(row: SoftwareProduct) -> ProductOut:
    return ProductOut(
        id=row.id,
        name=row.name,
        publisher=row.publisher,
        match_rules=list(row.match_rules),
        created_at=row.created_at,
    )


def _license_out(row: License) -> LicenseOut:
    # `has_key` only -- `key`/`key_sealed` never leave this module through a
    # list/detail response (invariant #3, spec §5).
    return LicenseOut(
        id=row.id,
        client_id=row.client_id,
        product_id=row.product_id,
        vendor_id=row.vendor_id,
        type=row.type,
        entitlements=row.entitlements,
        has_key=row.key_sealed is not None,
        cost=row.cost,
        currency=row.currency,
        po_ref=row.po_ref,
        purchased_at=row.purchased_at,
        maintenance_expires_at=row.maintenance_expires_at,
        renews_at=row.renews_at,
        agreement_ref=row.agreement_ref,
        notes=row.notes,
        created_at=row.created_at,
    )


def _allocation_out(row: LicenseAllocation) -> AllocationOut:
    return AllocationOut(
        id=row.id,
        license_id=row.license_id,
        target_type=row.target_type,
        target_id=row.target_id,
        person_id=row.person_id,
        allocated_at=row.allocated_at,
        released_at=row.released_at,
    )


# --------------------------------------------------------------------------- #
# GET|POST /vendors, GET|PATCH /vendors/{vendor_id}
# --------------------------------------------------------------------------- #


@router.get("/vendors", response_model=list[VendorOut])
async def list_vendors_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> list[VendorOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_vendors(session, ctx.org_id)
        return [_vendor_out(r) for r in rows]


@router.post("/vendors", status_code=status.HTTP_201_CREATED, response_model=VendorOut)
async def create_vendor_route(
    body: VendorCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("licenses.manage")),
) -> VendorOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        vendor = await create_vendor(
            session,
            org_id=ctx.org_id,
            actor_id=ctx.user_id,
            name=body.name,
            website=body.website,
            contacts=body.contacts,
        )
        return _vendor_out(vendor)


@router.get("/vendors/{vendor_id}", response_model=VendorOut)
async def get_vendor_route(
    vendor_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> VendorOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        vendor = await get_vendor(session, ctx.org_id, vendor_id)
        if vendor is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such vendor"))
        return _vendor_out(vendor)


@router.patch("/vendors/{vendor_id}", response_model=VendorOut)
async def update_vendor_route(
    vendor_id: UUID,
    body: VendorUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("licenses.manage")),
) -> VendorOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            vendor = await update_vendor(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                vendor_id=vendor_id,
                **body.model_dump(),
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None
        return _vendor_out(vendor)


# --------------------------------------------------------------------------- #
# GET|POST /licenses/products -- static, declared before /licenses/{id}.
# --------------------------------------------------------------------------- #


@router.get("/licenses/products", response_model=list[ProductOut])
async def list_products_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> list[ProductOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_products(session, ctx.org_id)
        return [_product_out(r) for r in rows]


@router.post(
    "/licenses/products", status_code=status.HTTP_201_CREATED, response_model=ProductOut
)
async def create_product_route(
    body: ProductCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("licenses.manage")),
) -> ProductOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            product = await create_product(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                name=body.name,
                publisher=body.publisher,
                match_rules=[r.model_dump() for r in body.match_rules],
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None
        return _product_out(product)


# --------------------------------------------------------------------------- #
# GET /licenses/compliance, /review-queue (+/map, /ignore), /renewals,
# /costs, /export -- static, declared before /licenses/{id}.
# --------------------------------------------------------------------------- #


@router.get("/licenses/compliance", response_model=list[ComplianceRow])
async def compliance_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> list[ComplianceRow]:
    """One row per (client, product), for every active client the caller's
    scope reaches: a client's position is its own licenses against its own
    computers, never summed with another's."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows: list[ComplianceRow] = []
        for client_id in await reported_clients(session, ctx.org_id):
            summary = await compliance_summary(session, ctx.org_id, client_id=client_id)
            rows.extend(ComplianceRow(**row) for row in summary)
        return rows


@router.get("/licenses/review-queue", response_model=list[ReviewQueueRow])
async def review_queue_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> list[ReviewQueueRow]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await review_queue(session, ctx.org_id)
        return [ReviewQueueRow(**row) for row in rows]


@router.post("/licenses/review-queue/map", response_model=ProductOut)
async def review_queue_map_route(
    body: ReviewMapRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("licenses.manage")),
) -> ProductOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            product = await map_software(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                name=body.name,
                product_id=body.product_id,
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None
        return _product_out(product)


@router.post("/licenses/review-queue/ignore", response_model=ReviewIgnoreOut)
async def review_queue_ignore_route(
    body: ReviewIgnoreRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.manage")),
) -> ReviewIgnoreOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            ignore = await ignore_software(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                name=body.name,
                client_id=body.client_id,
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None
        return ReviewIgnoreOut(id=ignore.id, name=ignore.name, client_id=ignore.client_id)


@router.get("/licenses/renewals", response_model=list[RenewalRow])
async def renewals_route(
    request: Request,
    within_days: int = Query(default=30),
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> list[RenewalRow]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await renewal_calendar(
            session, ctx.org_id, now=datetime.now(UTC), within_days=within_days
        )
        return [RenewalRow(**row) for row in rows]


@router.get("/licenses/costs", response_model=list[CostRow])
async def costs_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> list[CostRow]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await cost_view(session, ctx.org_id)
        return [CostRow(**row) for row in rows]


@router.get("/licenses/export")
async def export_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> list[dict[str, Any]]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await export_rows(session, ctx.org_id)

        # An explicit, audited export (invariant #4) -- the ONLY report route
        # in this module that audits; plain list/detail/compliance/renewal/
        # cost reads write nothing (the Phase-9 no-audit-on-view lesson).
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="license.export",
            target_type="license_export",
            target_id=str(ctx.org_id),
            metadata={"row_count": len(rows)},
        )

        return rows


# --------------------------------------------------------------------------- #
# GET|PATCH /licenses/products/{product_id}
# --------------------------------------------------------------------------- #


@router.get("/licenses/products/{product_id}", response_model=ProductOut)
async def get_product_route(
    product_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> ProductOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        product = await get_product(session, ctx.org_id, product_id)
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such product"))
        return _product_out(product)


@router.patch("/licenses/products/{product_id}", response_model=ProductOut)
async def update_product_route(
    product_id: UUID,
    body: ProductUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("licenses.manage")),
) -> ProductOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            product = await update_product(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                product_id=product_id,
                **body.model_dump(),
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None
        return _product_out(product)


# --------------------------------------------------------------------------- #
# GET|POST /licenses, GET|PATCH|DELETE /licenses/{license_id}
# --------------------------------------------------------------------------- #


@router.get("/licenses", response_model=list[LicenseOut])
async def list_licenses_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> list[LicenseOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_licenses(session, ctx.org_id)
        return [_license_out(r) for r in rows]


@router.post("/licenses", status_code=status.HTTP_201_CREATED, response_model=LicenseOut)
async def create_license_route(
    body: LicenseCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.manage")),
) -> LicenseOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            license_ = await create_license(
                session,
                state.vault,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                client_id=body.client_id,
                product_id=body.product_id,
                vendor_id=body.vendor_id,
                type_=body.type,
                entitlements=body.entitlements,
                key=body.key,
                cost=body.cost,
                currency=body.currency,
                po_ref=body.po_ref,
                purchased_at=body.purchased_at,
                maintenance_expires_at=body.maintenance_expires_at,
                renews_at=body.renews_at,
                agreement_ref=body.agreement_ref,
                notes=body.notes,
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None
        return _license_out(license_)


@router.get("/licenses/{license_id}", response_model=LicenseOut)
async def get_license_route(
    license_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> LicenseOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        license_ = await get_license(session, ctx.org_id, license_id)
        if license_ is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such license"))
        return _license_out(license_)


@router.patch("/licenses/{license_id}", response_model=LicenseOut)
async def update_license_route(
    license_id: UUID,
    body: LicenseUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.manage")),
) -> LicenseOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            license_ = await update_license(
                session,
                state.vault,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                license_id=license_id,
                **body.model_dump(),
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None
        return _license_out(license_)


@router.delete("/licenses/{license_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_license_route(
    license_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.manage")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await delete_license(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, license_id=license_id
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET /licenses/{license_id}/key -- reveal, licenses.keys.read, audited by
# the service layer's `reveal_key` (the route never audits a second time).
# --------------------------------------------------------------------------- #


@router.get("/licenses/{license_id}/key", response_model=LicenseKeyOut)
async def reveal_license_key_route(
    license_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.keys.read")),
) -> LicenseKeyOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            key = await reveal_key(
                session,
                state.vault,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                license_id=license_id,
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None
        return LicenseKeyOut(key=key)


# --------------------------------------------------------------------------- #
# POST|GET /licenses/{license_id}/allocations, POST /allocations/{id}/release
# --------------------------------------------------------------------------- #


@router.post(
    "/licenses/{license_id}/allocations",
    status_code=status.HTTP_201_CREATED,
    response_model=AllocationOut,
)
async def allocate_route(
    license_id: UUID,
    body: AllocationCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.manage")),
) -> AllocationOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            allocation = await allocate(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                license_id=license_id,
                target_type=body.target_type,
                target_id=body.target_id,
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None
        return _allocation_out(allocation)


@router.get("/licenses/{license_id}/allocations", response_model=list[AllocationOut])
async def list_allocations_route(
    license_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.read")),
) -> list[AllocationOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        license_ = await get_license(session, ctx.org_id, license_id)
        if license_ is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such license"))
        rows = await list_allocations(session, ctx.org_id, license_id)
        return [_allocation_out(r) for r in rows]


@router.post("/allocations/{allocation_id}/release", response_model=AllocationOut)
async def release_allocation_route(
    allocation_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("licenses.manage")),
) -> AllocationOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            allocation = await release_allocation(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, allocation_id=allocation_id
            )
        except LicenseValidationError as exc:
            raise _http_error(exc) from None
        return _allocation_out(allocation)
