"""Application deployment REST API (docs/modules/app-deploy/spec.md §12).

Routes under ``/api/v1/deploy``. Every route is gated server-side by
``require_permission`` (the frontend never gates security, invariant #6); no
response model carries a secret (the module has none — installer artifacts
are package_id-keyed bytes with a sha256 integrity check, not credentials,
and script bodies are not secrets either, though list responses omit them to
keep payloads light).

``POST /deployments/{id}/execute`` and ``POST /scripts/{id}/run`` dispatch
signed jobs exclusively through ``app.modules.deploy.dispatch.
dispatch_deployment`` / ``app.modules.deploy.scripts.run_script`` — the
module's only path from an HTTP request to a device job (invariant #1). Both
compute ``now=datetime.now(UTC)`` in the handler and attribute the dispatch
audit event to the calling user (``actor_id=ctx.user_id,
actor_type="user"``), matching Task 5's actor-threading for
``store_artifact`` below.

Both build+sign the jobs inside the tenant transaction, but the actual
``publish_job`` (the external Redis side effect) is deferred until AFTER the
transaction commits — mirrors ``app.modules.patch.api``'s ``POST /scan`` /
``POST /apply`` idiom — so a rolled-back request never dispatches a job to a
device that doesn't durably have one.
"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.audit import audit_service
from app.core.http import read_bounded_body, reject_oversized_content_length
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job
from app.jobs.review_gate import DeviceAwaitingReview, refused
from app.models import Deployment, DeploymentResult, Device, DeviceGroup, Package, Script
from app.modules.change_mgmt.gates import (
    authorizing_change,
    deployment_gate,
    gate_said,
    link_actions,
    record_auto_change,
    record_gate_refusal,
)
from app.modules.change_mgmt.state import ChangeNotFoundError, ChangeValidationError
from app.modules.deploy.dispatch import deployment_refusal, dispatch_deployment
from app.modules.deploy.packages import (
    DeployValidationError,
    create_package,
    delete_package,
    store_artifact,
)
from app.modules.deploy.reporting import deployment_report, fleet_deploy_report
from app.modules.deploy.script_policies import (
    ScriptPolicyNotFound,
    create_policy,
    delete_policy,
    get_policy,
    policy_runs,
    policy_summaries,
    update_policy,
)
from app.modules.deploy.scripts import author_script, edit_script, run_script
from app.services.client_sites import ClientNotFound, NotAcceptingNewRows, client_for_new_rows

#: The job a deployment becomes, for the review gate's refusal at creation.
DEPLOY_JOB_TYPE = "app.deploy"

#: What `its_deployment_client` (migration 0239) raises with, as every check
#: constraint does: a target it cannot resolve, or a client that is not the
#: target's. `deployments` has no CHECK constraint of its own.
_CHECK_VIOLATION = "23514"

router = APIRouter(prefix="/deploy", tags=["deploy"])


# --------------------------------------------------------------------------- #
# Models — none carry a secret (installer artifacts and script bodies are
# not credentials); list responses stay metadata-only where noted.
# --------------------------------------------------------------------------- #


class PackageOut(BaseModel):
    id: UUID
    name: str
    version: str
    os_family: str
    installer_type: str
    artifact_ref: str | None
    artifact_sha256: str | None
    package_identifier: str | None
    detection_rule: dict[str, Any]
    uninstall_rule: dict[str, Any]
    install_args: str | None
    created_at: datetime


class PackageCreateRequest(BaseModel):
    name: str
    version: str
    os_family: str
    installer_type: str
    # The identifier winget/choco install by, e.g. "Mozilla.Firefox". Optional
    # and additive: omitted, `manager_package_identifier` falls back to
    # `detection_rule["name"]`, which is what the fleet already derives.
    package_identifier: str | None = None
    detection_rule: dict[str, Any] = {}
    uninstall_rule: dict[str, Any] = {}
    install_args: str | None = None


class DeploymentOut(BaseModel):
    id: UUID
    #: The target's client, written by the database (migration 0239).
    client_id: UUID
    package_id: UUID
    action: str
    target_type: str
    target_id: UUID
    window_cron: str | None
    requested_by: UUID | None
    state: str
    created_at: datetime


class DeploymentCreateRequest(BaseModel):
    package_id: UUID
    action: Literal["install", "update", "uninstall"]
    target_type: Literal["device", "group"]
    target_id: UUID
    window_cron: str | None = None
    #: An approved change this deployment implements. Required where a change
    #: gate covers the target; otherwise one is recorded for it.
    change_id: UUID | None = None


class DeploymentExecuteResponse(BaseModel):
    job_ids: list[UUID]


class DeviceResultOut(BaseModel):
    device_id: UUID
    state: str
    exit_code: int | None
    log_ref: str | None
    finished_at: datetime | None


class DeploymentDetailOut(BaseModel):
    deployment_id: UUID
    #: The target's client, written by the database (migration 0239).
    client_id: UUID
    state: str
    action: str
    package_id: UUID
    device_results: list[DeviceResultOut]
    counts_by_state: dict[str, int]


class DeviceDeploymentHistoryOut(BaseModel):
    deployment_id: UUID
    #: The deployment's client: the device's (migration 0239).
    client_id: UUID
    package_id: UUID
    action: str
    state: str
    exit_code: int | None
    log_ref: str | None
    finished_at: datetime | None
    created_at: datetime


class ScriptListItemOut(BaseModel):
    """Metadata only — the script body is omitted from list responses."""

    id: UUID
    name: str
    os_family: str
    interpreter: str
    version: int
    created_by: UUID | None
    created_at: datetime


class ScriptOut(ScriptListItemOut):
    """Full script, including body — returned on author/edit, where the
    caller just supplied (or needs to confirm) the body."""

    body: str


class ScriptCreateRequest(BaseModel):
    name: str
    os_family: str
    interpreter: str
    body: str


class ScriptUpdateRequest(BaseModel):
    body: str


class ScriptRunRequest(BaseModel):
    target_type: Literal["device", "group"]
    target_id: UUID


class ScriptRunResponse(BaseModel):
    job_ids: list[UUID]


class ScriptPolicyOut(BaseModel):
    """A scheduled script: which script, on which group, how often."""

    id: UUID
    name: str
    script_id: UUID
    script_name: str
    device_group_id: UUID
    device_group_name: str
    interval_hours: int
    in_maintenance_window: bool
    alert_on_failure: bool
    keep_runs: int
    enabled: bool
    #: Devices whose latest run for this schedule failed / succeeded.
    devices_failing: int
    devices_succeeding: int
    last_run_at: datetime | None


class ScriptPolicyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    script_id: UUID
    device_group_id: UUID
    interval_hours: int = Field(default=24, ge=1, le=720)
    in_maintenance_window: bool = True
    alert_on_failure: bool = False
    keep_runs: int = Field(default=30, ge=1, le=500)
    enabled: bool = True


class ScriptPolicyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    interval_hours: int | None = Field(default=None, ge=1, le=720)
    in_maintenance_window: bool | None = None
    alert_on_failure: bool | None = None
    keep_runs: int | None = Field(default=None, ge=1, le=500)
    enabled: bool | None = None


class ScriptPolicyRunOut(BaseModel):
    """One run of a scheduled script on one device, with what it printed."""

    id: UUID
    device_id: UUID
    hostname: str | None
    script_version: int
    state: str
    exit_code: int | None
    #: Cleared once the run is older than the schedule's keep_runs.
    output: str | None
    created_at: datetime
    finished_at: datetime | None


class ExposedDeploymentOut(BaseModel):
    deployment_id: UUID
    #: The target's client, written by the database (migration 0239).
    client_id: UUID
    state: str
    action: str
    package_id: UUID
    created_at: datetime
    device_count: int
    success_rate: float


class ExposedScriptRunOut(BaseModel):
    script_run_id: UUID
    script_id: UUID
    device_id: UUID
    state: str
    exit_code: int | None
    finished_at: datetime | None
    created_at: datetime


class FleetDeployReportOut(BaseModel):
    deployments: list[ExposedDeploymentOut]
    script_runs: list[ExposedScriptRunOut]


def _package_out(row: Package) -> PackageOut:
    return PackageOut(
        id=row.id,
        name=row.name,
        version=row.version,
        os_family=row.os_family,
        installer_type=row.installer_type,
        artifact_ref=row.artifact_ref,
        artifact_sha256=row.artifact_sha256,
        package_identifier=row.package_identifier,
        detection_rule=row.detection_rule,
        uninstall_rule=row.uninstall_rule,
        install_args=row.install_args,
        created_at=row.created_at,
    )


def _deployment_out(row: Deployment) -> DeploymentOut:
    return DeploymentOut(
        id=row.id,
        client_id=row.client_id,
        package_id=row.package_id,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        window_cron=row.window_cron,
        requested_by=row.requested_by,
        state=row.state,
        created_at=row.created_at,
    )


def _script_list_item_out(row: Script) -> ScriptListItemOut:
    return ScriptListItemOut(
        id=row.id,
        name=row.name,
        os_family=row.os_family,
        interpreter=row.interpreter,
        version=row.version,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _script_out(row: Script) -> ScriptOut:
    return ScriptOut(
        id=row.id,
        name=row.name,
        os_family=row.os_family,
        interpreter=row.interpreter,
        version=row.version,
        created_by=row.created_by,
        created_at=row.created_at,
        body=row.body,
    )


def _object_store(request: Request) -> ObjectStore:
    """Lazily build (and cache) the object store on first use — mirrors
    ``app.modules.remote.api._object_store``. Construction hits the object
    store to ensure the bucket exists, so it stays out of app startup;
    artifact upload is the only deploy-module path that needs it."""
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(request.app.state.settings)
        request.app.state.object_store = store
    return store


class _AppStateArtifactSigner:
    """`ArtifactUrlSigner` backed by the app-state ``ObjectStore``, resolved
    on first use.

    ``_object_store`` constructs (and head_buckets) on first call, so binding
    it eagerly would make ``POST /deployments/{id}/execute`` depend on object
    storage even for a manager-sourced deployment, which has no artifact at
    all. This defers that to the moment a URL is actually needed while still
    reusing the cached store when one is.
    """

    def __init__(self, request: Request) -> None:
        self._request = request

    def presign_package_artifact(
        self, org_id: UUID, package_id: UUID, *, ref: str, expires_in: int
    ) -> str:
        return _object_store(self._request).presign_package_artifact(
            org_id, package_id, ref=ref, expires_in=expires_in
        )


# --------------------------------------------------------------------------- #
# Packages
# --------------------------------------------------------------------------- #


@router.get("/packages", response_model=list[PackageOut])
async def list_packages(
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.read")),
) -> list[PackageOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = (
            (
                await session.execute(
                    select(Package).where(Package.org_id == ctx.org_id).order_by(Package.id)
                )
            )
            .scalars()
            .all()
        )
        return [_package_out(r) for r in rows]


@router.post("/packages", status_code=status.HTTP_201_CREATED, response_model=PackageOut)
async def create_package_route(
    body: PackageCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("deploy.package.manage")),
) -> PackageOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            package = await create_package(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                name=body.name,
                version=body.version,
                os_family=body.os_family,
                installer_type=body.installer_type,
                package_identifier=body.package_identifier,
                detection_rule=body.detection_rule,
                uninstall_rule=body.uninstall_rule,
                install_args=body.install_args,
            )
        except DeployValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        return _package_out(package)


# An ample cap for an installer artifact -- deliberately much larger than
# budget's 10 MiB invoice-attachment cap (`app.modules.budget.api.
# _MAX_ATTACHMENT_BYTES`): packages shipped through this module are real
# installers (MSI/EXE/PKG/DMG/DEB/RPM), and enterprise installers routinely
# run into the hundreds of MB to low GB (Office, Adobe Creative Cloud, large
# engineering/CAD suites) -- imposing an invoice-sized cap here would reject
# ordinary, legitimate packages outright. 2 GiB comfortably covers a single
# real-world installer while still bounding the worst case to a known,
# finite amount instead of "however much memory happens to be free". This
# endpoint is also `deploy.package.manage`-gated -- a rare, admin-only,
# low-concurrency action -- unlike budget's attachment upload, which any
# `expense.record` holder can trigger routinely; the higher cap is
# affordable precisely because of who can reach it and how often.
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


@router.post("/packages/{package_id}/artifact", response_model=PackageOut)
async def upload_package_artifact(
    package_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("deploy.package.manage")),
) -> PackageOut:
    # Cheapest check first, before any DB/object-store work (mirrors
    # `app.modules.budget.api.upload_attachment_route`). `Content-Length` is
    # client-supplied and may be absent (chunked transfer) or wrong, so the
    # bytes actually read are ALSO bounded below via `read_bounded_body` --
    # a bare `await request.body()` would otherwise buffer an entire,
    # unbounded request in process memory before this handler ever saw a
    # byte of it, letting any `deploy.package.manage` holder OOM the control
    # plane with one request.
    reject_oversized_content_length(request, _MAX_ARTIFACT_BYTES)
    data = await read_bounded_body(request, _MAX_ARTIFACT_BYTES)
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            package = await store_artifact(
                session,
                _object_store(request),
                org_id=ctx.org_id,
                package_id=package_id,
                data=data,
                actor_id=ctx.user_id,
                actor_type="user",
            )
        except NoResultFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such package")) from exc
        return _package_out(package)


@router.delete("/packages/{package_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_package_route(
    package_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("deploy.package.manage")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        deleted = await delete_package(
            session, org_id=ctx.org_id, actor_id=ctx.user_id, package_id=package_id
        )
        if not deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such package"))


# --------------------------------------------------------------------------- #
# Deployments
# --------------------------------------------------------------------------- #


@router.get("/deployments", response_model=list[DeploymentOut])
async def list_deployments(
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.read")),
) -> list[DeploymentOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = (
            (
                await session.execute(
                    select(Deployment)
                    .where(Deployment.org_id == ctx.org_id)
                    .order_by(Deployment.created_at.desc(), Deployment.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_deployment_out(r) for r in rows]


async def _deployment_target(
    session: AsyncSession, org_id: UUID, target_type: str, target_id: UUID
) -> tuple[UUID, str | None] | None:
    """The target's client and, for a device, its review state -- read in the
    request's own session, so a target of a client outside its scope is
    absent, exactly like one that does not exist."""
    if target_type == "device":
        row = (
            await session.execute(
                select(Device.client_id, Device.review_state).where(
                    Device.org_id == org_id, Device.id == target_id
                )
            )
        ).one_or_none()
        return None if row is None else (row[0], row[1])
    client = (
        await session.execute(
            select(DeviceGroup.client_id).where(
                DeviceGroup.org_id == org_id, DeviceGroup.id == target_id
            )
        )
    ).scalar_one_or_none()
    return None if client is None else (client, None)


@router.post("/deployments", status_code=status.HTTP_201_CREATED, response_model=DeploymentOut)
async def create_deployment(
    body: DeploymentCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.create")),
) -> DeploymentOut:
    refusal: str | None = None
    out: DeploymentOut | None = None
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        package = (
            await session.execute(
                select(Package).where(
                    Package.org_id == ctx.org_id, Package.id == body.package_id
                )
            )
        ).scalar_one_or_none()
        if package is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such package"))

        # The deployment is its target's client's (MSP clients plan 2026-10-26
        # Task 9): the database writes it from the target, and the request
        # resolves the target first, in its own scope. Another client's
        # device or group is the 404 a missing one is -- it was not, and the
        # window sweep later dispatched it under scope all (research-3 §E5).
        target = await _deployment_target(session, ctx.org_id, body.target_type, body.target_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("deployment target not found"))
        target_client, review_state = target
        # A deployment is a new row of that client, and an archived client
        # takes none (a 409). Its row, read FOR SHARE and held to commit, is
        # this create's FIRST lock (`client_sites`); the authorizing change's
        # module guard takes the same lock again. Unassigned is not refused:
        # a device waiting for its client may still need software.
        try:
            await client_for_new_rows(session, org_id=ctx.org_id, client_id=target_client)
        except ClientNotFound:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, tr("deployment target not found")
            ) from None
        except NotAcceptingNewRows as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, exc.detail) from None
        # A single named device waiting for review is refused NOW, with the
        # review gate's own 409, rather than accepted as a deployment that
        # can never dispatch (install-link spec §1.5).
        if body.target_type == "device" and refused(review_state, DEPLOY_JOB_TYPE):
            raise DeviceAwaitingReview(body.target_id, DEPLOY_JOB_TYPE, review_state or "")

        try:
            # The change must be the target's client's, which must use change
            # management -- this write's first lock (`authorizing_change`).
            change = (
                await authorizing_change(
                    session, ctx.org_id, body.change_id, client_id=target_client
                )
                if body.change_id is not None
                else None
            )
        except ChangeNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such change")) from None
        except ChangeValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        if change is None:
            refusal = await deployment_gate(
                session, ctx.org_id, body.target_type, body.target_id, client_id=target_client
            )
        if refusal is not None:
            await record_gate_refusal(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                target_type=body.target_type,
                target_id=str(body.target_id),
                reason=refusal,
                metadata={"package_id": str(body.package_id), "action": body.action},
                client_id=target_client,
            )
        else:
            # No client_id: `its_deployment_client` writes it from the target
            # and the INSERT reads it back.
            deployment = Deployment(
                org_id=ctx.org_id,
                package_id=body.package_id,
                action=body.action,
                target_type=body.target_type,
                target_id=body.target_id,
                window_cron=body.window_cron,
                requested_by=ctx.user_id,
                state="pending",
            )
            session.add(deployment)
            try:
                await session.flush()
            except IntegrityError as exc:
                # The trigger reads the target again and could not resolve it:
                # it went between the read above and this insert. Nothing was
                # written; the person may simply try again.
                if getattr(exc.orig, "sqlstate", None) != _CHECK_VIOLATION:
                    raise
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    tr("the deployment target changed while it was being created; try again"),
                ) from None
            client_id = deployment.client_id

            await audit_service.record(
                session,
                org_id=ctx.org_id,
                actor_type="user",
                actor_id=ctx.user_id,
                action="deploy.deployment_created",
                target_type="deployment",
                target_id=str(deployment.id),
                metadata={
                    "package_id": str(body.package_id),
                    "action": body.action,
                    "target_type": body.target_type,
                },
                client_id=client_id,
            )
            if change is not None:
                await link_actions(
                    session,
                    org_id=ctx.org_id,
                    actor_id=ctx.user_id,
                    change=change,
                    action_type="deployment",
                    action_ids=[deployment.id],
                )
            else:
                await record_auto_change(
                    session,
                    org_id=ctx.org_id,
                    client_id=client_id,
                    actor_id=ctx.user_id,
                    title=f"Deployment: {body.action} {package.name} {package.version}",
                    description=(
                        "Recorded automatically: a deployment was created in the deployment "
                        "console without a change request."
                    ),
                    affected=[{"kind": body.target_type, "id": str(body.target_id)}],
                    action_type="deployment",
                    action_ids=[deployment.id],
                )
            out = _deployment_out(deployment)
    if refusal is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, gate_said(refusal))
    assert out is not None
    return out


@router.post("/deployments/{deployment_id}/execute", response_model=DeploymentExecuteResponse)
async def execute_deployment(
    deployment_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.execute")),
) -> DeploymentExecuteResponse:
    state = request.app.state
    refusal: str | None = None
    pending: list[tuple[UUID, UUID, dict[str, Any]]] = []
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        deployment = (
            await session.execute(
                select(Deployment).where(
                    Deployment.org_id == ctx.org_id, Deployment.id == deployment_id
                )
            )
        ).scalar_one_or_none()
        if deployment is not None:
            refusal = await deployment_refusal(
                session, ctx.org_id, deployment, actor_id=ctx.user_id
            )
        try:
            if refusal is None:
                pending = await dispatch_deployment(
                    session,
                    state.job_engine,
                    org_id=ctx.org_id,
                    deployment_id=deployment_id,
                    now=datetime.now(UTC),
                    actor_id=ctx.user_id,
                    actor_type="user",
                    artifact_urls=_AppStateArtifactSigner(request),
                )
        except DeployValidationError as exc:
            # An artifact-backed package with no uploaded artifact (or no
            # recorded hash) is refused HERE rather than dispatched. The agent
            # would refuse it anyway, but a 422 naming the missing upload is
            # an answer the operator can act on, where a fleet of `refused`
            # job rows is an answer they have to go looking for.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    if refusal is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, gate_said(refusal))
    # Transaction committed: safe to publish the signed jobs to the agents.
    for _job_id, device_id, envelope in pending:
        await publish_job(state.redis, device_id, envelope)
    return DeploymentExecuteResponse(job_ids=[job_id for job_id, _, _ in pending])


@router.get("/deployments/{deployment_id}", response_model=DeploymentDetailOut)
async def get_deployment(
    deployment_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.read")),
) -> DeploymentDetailOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        report = await deployment_report(session, ctx.org_id, deployment_id)
        if report["state"] is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such deployment"))
        return DeploymentDetailOut(**report)


# --------------------------------------------------------------------------- #
# Scripts
# --------------------------------------------------------------------------- #


@router.get("/scripts", response_model=list[ScriptListItemOut])
async def list_scripts(
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.read")),
) -> list[ScriptListItemOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = (
            (
                await session.execute(
                    select(Script).where(Script.org_id == ctx.org_id).order_by(Script.id)
                )
            )
            .scalars()
            .all()
        )
        return [_script_list_item_out(r) for r in rows]


@router.post("/scripts", status_code=status.HTTP_201_CREATED, response_model=ScriptOut)
async def create_script(
    body: ScriptCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("deploy.script.manage")),
) -> ScriptOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            script = await author_script(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                name=body.name,
                os_family=body.os_family,
                interpreter=body.interpreter,
                body=body.body,
            )
        except DeployValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        return _script_out(script)


@router.put("/scripts/{script_id}", response_model=ScriptOut)
async def update_script(
    script_id: UUID,
    body: ScriptUpdateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("deploy.script.manage")),
) -> ScriptOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            script = await edit_script(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                script_id=script_id,
                body=body.body,
            )
        except NoResultFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such script")) from exc
        return _script_out(script)


@router.get("/scripts/{script_id}", response_model=ScriptOut)
async def get_script(
    script_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.read")),
) -> ScriptOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        script = (
            await session.execute(
                select(Script).where(Script.org_id == ctx.org_id, Script.id == script_id)
            )
        ).scalar_one_or_none()
        if script is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such script"))
        return _script_out(script)


@router.post("/scripts/{script_id}/run", response_model=ScriptRunResponse)
async def run_script_route(
    script_id: UUID,
    body: ScriptRunRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.script.run")),
) -> ScriptRunResponse:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        pending = await run_script(
            session,
            state.job_engine,
            org_id=ctx.org_id,
            script_id=script_id,
            target_type=body.target_type,
            target_id=body.target_id,
            actor_id=ctx.user_id,
            actor_type="user",
        )
    # Transaction committed: safe to publish the signed jobs to the agents.
    for _job_id, device_id, envelope in pending:
        await publish_job(state.redis, device_id, envelope)
    return ScriptRunResponse(job_ids=[job_id for job_id, _, _ in pending])


# --------------------------------------------------------------------------- #
# Scheduled scripts
# --------------------------------------------------------------------------- #


def _policy_error(exc: DeployValidationError) -> HTTPException:
    if isinstance(exc, ScriptPolicyNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


async def _policy_out(session: Any, org_id: UUID, policy_id: UUID) -> ScriptPolicyOut:
    for row in await policy_summaries(session, org_id):
        if row["id"] == policy_id:
            return ScriptPolicyOut(**row)
    raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such schedule"))


@router.get("/script-policies", response_model=list[ScriptPolicyOut])
async def list_script_policies(
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.read")),
) -> list[ScriptPolicyOut]:
    """Every scheduled script in this organization."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return [ScriptPolicyOut(**row) for row in await policy_summaries(session, ctx.org_id)]


@router.post(
    "/script-policies", status_code=status.HTTP_201_CREATED, response_model=ScriptPolicyOut
)
async def create_script_policy(
    body: ScriptPolicyCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.script.schedule")),
) -> ScriptPolicyOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            row = await create_policy(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, **body.model_dump()
            )
        except DeployValidationError as exc:
            raise _policy_error(exc) from None
        return await _policy_out(session, ctx.org_id, row.id)


@router.patch("/script-policies/{policy_id}", response_model=ScriptPolicyOut)
async def update_script_policy(
    policy_id: UUID,
    body: ScriptPolicyUpdateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.script.schedule")),
) -> ScriptPolicyOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await update_policy(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                policy_id=policy_id,
                changes=body.model_dump(exclude_unset=True),
            )
        except DeployValidationError as exc:
            raise _policy_error(exc) from None
        return await _policy_out(session, ctx.org_id, policy_id)


@router.delete("/script-policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_script_policy(
    policy_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.script.schedule")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await delete_policy(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, policy_id=policy_id
            )
        except DeployValidationError as exc:
            raise _policy_error(exc) from None


@router.get("/script-policies/{policy_id}/runs", response_model=list[ScriptPolicyRunOut])
async def list_script_policy_runs(
    policy_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.read")),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ScriptPolicyRunOut]:
    """The schedule's latest runs, newest first, with their output."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await get_policy(session, ctx.org_id, policy_id)
        except DeployValidationError as exc:
            raise _policy_error(exc) from None
        rows = await policy_runs(session, ctx.org_id, policy_id, limit)
    return [ScriptPolicyRunOut(**row) for row in rows]


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


@router.get("/report", response_model=FleetDeployReportOut)
async def report(
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.read")),
) -> FleetDeployReportOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        data = await fleet_deploy_report(session, ctx.org_id)
        return FleetDeployReportOut(**data)


@router.get(
    "/devices/{device_id}/deployments", response_model=list[DeviceDeploymentHistoryOut]
)
async def device_deployment_history(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("deploy.read")),
) -> list[DeviceDeploymentHistoryOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        device = (
            await session.execute(
                select(Device.id).where(Device.org_id == ctx.org_id, Device.id == device_id)
            )
        ).scalar_one_or_none()
        if device is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device"))

        rows = (
            await session.execute(
                select(
                    DeploymentResult.deployment_id,
                    Deployment.client_id,
                    Deployment.package_id,
                    Deployment.action,
                    DeploymentResult.state,
                    DeploymentResult.exit_code,
                    DeploymentResult.log_ref,
                    DeploymentResult.finished_at,
                    DeploymentResult.created_at,
                )
                .join(Deployment, Deployment.id == DeploymentResult.deployment_id)
                .where(
                    DeploymentResult.org_id == ctx.org_id,
                    DeploymentResult.device_id == device_id,
                    Deployment.org_id == ctx.org_id,
                )
                .order_by(DeploymentResult.created_at.desc(), DeploymentResult.deployment_id.desc())
            )
        ).all()
        return [
            DeviceDeploymentHistoryOut(
                deployment_id=r.deployment_id,
                client_id=r.client_id,
                package_id=r.package_id,
                action=r.action,
                state=r.state,
                exit_code=r.exit_code,
                log_ref=r.log_ref,
                finished_at=r.finished_at,
                created_at=r.created_at,
            )
            for r in rows
        ]
