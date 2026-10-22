"""Provisioning registry + run REST API (docs/modules/provisioning/spec.md §12).

Routes under ``/api/v1/provisioning``. Every route is gated server-side by
``require_permission`` (invariant #6, the frontend never gates security);
``org_id``/``actor_id`` always come from ``AuthContext``, never the request
body/path (invariant #5).

No response model carries a secret (invariant #3): a profile's
``post_config`` can hold sealed ``domain_join.password`` / ``local_admin.
password`` markers (``repository._CREDENTIAL_PATHS``) — before a
``ProfileOut`` is built, ``_redact_post_config`` strips those fields out
entirely (never echoes the ``{"__sealed__": ...}`` value) and surfaces
``has_domain_join_credential`` / ``has_local_admin_credential`` booleans
instead. ``POST /profiles/{id}/run`` returns only ``run_id``/``state`` —
never the enrollment token secret or the rendered answer-file bytes that
``start_provision_run`` produces internally.

Unlike ``app.modules.backup.api``, ``POST /profiles/{id}/run`` does NOT
publish anything after commit: the ``provision.prepare`` envelope
``start_provision_run`` signs is a site-server contract with no v1 consumer
(contract-only), and the ``provision.enroll`` post-config job is dispatched
later, out of band, by the worker once the device actually enrolls (Task 11)
— not from this HTTP handler.
"""

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import AuthContext, require_permission
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job
from app.models import OsImage, ProvisionProfile, ProvisionRun
from app.modules.provisioning.dispatch import start_provision_run
from app.modules.provisioning.reporting import (
    device_provisioning,
    fleet_provisioning_report,
    provision_run_status,
)
from app.modules.provisioning.repository import (
    _CREDENTIAL_PATHS,
    ProvisioningPermissionError,
    ProvisioningValidationError,
    _is_sealed_marker,
    create_image,
    get_image,
    get_profile,
    list_images,
    list_profiles,
    upsert_profile,
)
from app.modules.provisioning.schema import (
    DeviceProvisioningOut,
    FleetProvisioningReportOut,
    OsImageCreateRequest,
    OsImageOut,
    ProfileOut,
    ProfileUpsertRequest,
    RunOut,
    RunStartRequest,
    RunStartResponse,
)

router = APIRouter(prefix="/provisioning", tags=["provisioning"])


def _object_store(request: Request) -> ObjectStore:
    """Lazily build (and cache) the object store on first use — mirrors
    ``app.modules.backup.api._object_store``. Only the run-start path needs
    it (rendering/storing the answer file), so it stays out of app startup."""
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(request.app.state.settings)
        request.app.state.object_store = store
    return store


def _image_out(row: OsImage) -> OsImageOut:
    return OsImageOut(
        id=row.id,
        os_family=row.os_family,
        name=row.name,
        kind=row.kind,
        artifact_ref=row.artifact_ref,
        artifact_sha256=row.artifact_sha256,
        params=row.params,
        state=row.state,
        created_at=row.created_at,
    )


def _redact_post_config(post_config: dict[str, Any]) -> tuple[dict[str, Any], bool, bool]:
    """Return a deep copy of ``post_config`` with every sealed credential
    field (``_CREDENTIAL_PATHS``) removed entirely, plus booleans recording
    whether each was present. Never echoes the removed value — sealed or
    (should it somehow occur) plaintext — back to the caller (invariant #3)."""
    redacted = copy.deepcopy(post_config) if isinstance(post_config, dict) else {}
    has_domain_join_credential = False
    has_local_admin_credential = False
    for outer_key, inner_key in _CREDENTIAL_PATHS:
        outer = redacted.get(outer_key)
        if not isinstance(outer, dict) or inner_key not in outer:
            continue
        value = outer.pop(inner_key)
        if _is_sealed_marker(value) or isinstance(value, str):
            if outer_key == "domain_join":
                has_domain_join_credential = True
            elif outer_key == "local_admin":
                has_local_admin_credential = True
    return redacted, has_domain_join_credential, has_local_admin_credential


def _profile_out(row: ProvisionProfile) -> ProfileOut:
    post_config = row.post_config if isinstance(row.post_config, dict) else {}
    redacted, has_domain_join_credential, has_local_admin_credential = _redact_post_config(
        post_config
    )
    return ProfileOut(
        id=row.id,
        name=row.name,
        image_id=row.image_id,
        post_config=redacted,
        target_scope=row.target_scope,
        enabled=row.enabled,
        created_at=row.created_at,
        has_domain_join_credential=has_domain_join_credential,
        has_local_admin_credential=has_local_admin_credential,
    )


def _run_out(row: ProvisionRun) -> RunOut:
    return RunOut(
        id=row.id,
        profile_id=row.profile_id,
        device_id=row.device_id,
        target_ref=row.target_ref,
        state=row.state,
        answer_ref=row.answer_ref,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
    )


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #


@router.get("/images", response_model=list[OsImageOut])
async def list_images_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.read")),
) -> list[OsImageOut]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await list_images(session, ctx.org_id)
        return [_image_out(r) for r in rows]


@router.post("/images", status_code=status.HTTP_201_CREATED, response_model=OsImageOut)
async def create_image_route(
    body: OsImageCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.manage")),
) -> OsImageOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            image = await create_image(
                session,
                org_id=ctx.org_id,
                os_family=body.os_family,
                name=body.name,
                kind=body.kind,
                params=body.params,
                actor_id=ctx.user_id,
                actor_type="user",
            )
        except ProvisioningValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        return _image_out(image)


@router.get("/images/{image_id}", response_model=OsImageOut)
async def get_image_route(
    image_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.read")),
) -> OsImageOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        image = await get_image(session, ctx.org_id, image_id)
        if image is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such image"))
        return _image_out(image)


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


@router.get("/profiles", response_model=list[ProfileOut])
async def list_profiles_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.read")),
) -> list[ProfileOut]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = await list_profiles(session, ctx.org_id)
        return [_profile_out(r) for r in rows]


@router.post("/profiles", status_code=status.HTTP_201_CREATED, response_model=ProfileOut)
async def create_profile_route(
    body: ProfileUpsertRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.manage")),
) -> ProfileOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine) as session:
        try:
            profile = await upsert_profile(
                session,
                state.vault,
                org_id=ctx.org_id,
                name=body.name,
                image_id=body.image_id,
                post_config=body.post_config,
                target_scope=body.target_scope,
                enabled=body.enabled,
                actor_id=ctx.user_id,
                actor_type="user",
            )
        except ProvisioningValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        return _profile_out(profile)


@router.put("/profiles/{profile_id}", response_model=ProfileOut)
async def update_profile_route(
    profile_id: UUID,
    body: ProfileUpsertRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.manage")),
) -> ProfileOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine) as session:
        try:
            profile = await upsert_profile(
                session,
                state.vault,
                org_id=ctx.org_id,
                name=body.name,
                image_id=body.image_id,
                post_config=body.post_config,
                target_scope=body.target_scope,
                enabled=body.enabled,
                actor_id=ctx.user_id,
                actor_type="user",
                profile_id=profile_id,
            )
        except ProvisioningValidationError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
        return _profile_out(profile)


@router.get("/profiles/{profile_id}", response_model=ProfileOut)
async def get_profile_route(
    profile_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.read")),
) -> ProfileOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        profile = await get_profile(session, ctx.org_id, profile_id)
        if profile is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such profile"))
        return _profile_out(profile)


# --------------------------------------------------------------------------- #
# POST /profiles/{profile_id}/run — no publish (see module docstring)
# --------------------------------------------------------------------------- #


@router.post("/profiles/{profile_id}/run", response_model=RunStartResponse)
async def start_run_route(
    profile_id: UUID,
    body: RunStartRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.run")),
) -> RunStartResponse:
    state = request.app.state
    wake_envelope: dict[str, Any] | None = None
    try:
        async with org_session(ctx.org_id, engine=state.app_engine) as session:
            run, wake_envelope = await start_provision_run(
                session,
                _object_store(request),
                state.vault,
                org_id=ctx.org_id,
                profile_id=profile_id,
                target_ref=body.target_ref,
                now=datetime.now(UTC),
                actor_id=ctx.user_id,
                actor_type="user",
                wake_on_lan=body.wake_on_lan,
                job_engine=state.job_engine,
            )
            out = RunStartResponse(run_id=run.id, state=run.state)
    except ProvisioningPermissionError as exc:
        # 403, not 422: "you may not do this" is not "your request was
        # malformed". `wake_on_lan` additionally requires `site.wake`.
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    except ProvisioningValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    # `provision.prepare` still has no v1 consumer and `provision.enroll` is
    # dispatched later by the worker. What IS published here is the `site.wake`
    # envelope, and ONLY AFTER the `org_session` block has exited -- a job
    # published before the transaction commits could reach an agent describing a
    # run that then rolled back.
    if wake_envelope is not None:
        await publish_job(
            state.redis, UUID(wake_envelope["job"]["device"]), wake_envelope
        )
    return out


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


@router.get("/runs", response_model=list[RunOut])
async def list_runs_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.read")),
) -> list[RunOut]:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        rows = (
            (
                await session.execute(
                    select(ProvisionRun)
                    .where(ProvisionRun.org_id == ctx.org_id)
                    .order_by(ProvisionRun.started_at.desc(), ProvisionRun.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_run_out(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run_route(
    run_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.read")),
) -> RunOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        data = await provision_run_status(session, ctx.org_id, run_id)
        if data["state"] is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such run"))
        # `provision_run_status` doesn't surface `answer_ref` (a storage
        # pointer, not the secret bytes it points to) — this route mirrors
        # that omission rather than reaching around it.
        return RunOut(
            id=data["run_id"],
            profile_id=data["profile_id"],
            device_id=data["device_id"],
            target_ref=data["target_ref"],
            state=data["state"],
            answer_ref=None,
            started_at=data["started_at"],
            finished_at=data["finished_at"],
            error=data["error"],
        )


# --------------------------------------------------------------------------- #
# Device tab + fleet report
# --------------------------------------------------------------------------- #


@router.get("/devices/{device_id}/provisioning", response_model=DeviceProvisioningOut)
async def device_provisioning_route(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.read")),
) -> DeviceProvisioningOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        data = await device_provisioning(session, ctx.org_id, device_id)
        return DeviceProvisioningOut(**data)


@router.get("/report", response_model=FleetProvisioningReportOut)
async def report_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("provisioning.read")),
) -> FleetProvisioningReportOut:
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        data = await fleet_provisioning_report(session, ctx.org_id)
        data["per_profile"] = {str(k): v for k, v in data["per_profile"].items()}
        return FleetProvisioningReportOut(**data)
