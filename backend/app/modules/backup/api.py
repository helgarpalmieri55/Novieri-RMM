"""Backup engine REST API (docs/modules/backup/spec.md §12).

Routes under ``/api/v1/backup``. Every route is gated server-side by
``require_permission`` (the frontend never gates security, invariant #6); no
response model carries a secret — ``repo_key``/``encryption_key_ref`` never
leave this module through a response body. The repository/manifest models
below deliberately omit ``encryption_key_ref``; the manifest route returns
``BackupManifest`` (paths/sizes/chunk ids), never chunk bytes.

``POST /devices/{id}/run``, ``POST /points/{id}/restore``, and
``POST /points/{id}/verify`` dispatch signed jobs exclusively through
``app.modules.backup.dispatch.dispatch_backup`` /
``app.modules.backup.restore.dispatch_restore`` — the module's only path from
an HTTP request to a device job (invariant #1). All three compute
``now=datetime.now(UTC)`` in the handler and attribute the dispatch audit
event to the calling user (``actor_id=ctx.user_id, actor_type="user"``).

Each build+signs the job inside the tenant transaction, but the actual
``publish_job`` (the external Redis side effect) is deferred until AFTER the
transaction commits — mirrors ``app.modules.deploy.api``'s
``execute_deployment`` idiom — so a rolled-back request never dispatches a
job to a device that doesn't durably have one.

``GET /report/export`` is a privileged action in its own right — exporting
SOC 2/BCP evidence is audited as ``backup.evidence_exported`` with
``actor_type="user"`` (invariant #4), same treatment as
``app.modules.patch.api``'s ``report_export``.
"""

import csv
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, NoResultFound

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.csv_export import escape_formula
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job
from app.models import BackupPoint, BackupPolicy, BackupRepository
from app.modules.backup.clients import BackupClientError
from app.modules.backup.dispatch import dispatch_backup
from app.modules.backup.integrity import violated_constraint
from app.modules.backup.policy import upsert_policy as upsert_backup_policy
from app.modules.backup.reporting import (
    backup_evidence_rows,
    device_backup_status,
    fleet_backup_report,
)
from app.modules.backup.repository import create_repository
from app.modules.backup.restore import browse_manifest, dispatch_restore
from app.modules.backup.schema import BackupManifest

router = APIRouter(prefix="/backup", tags=["backup"])

#: Migration 0242's per-client name key.
REPOSITORY_NAME_KEY = "uq_backup_repositories_client_name"


# --------------------------------------------------------------------------- #
# Models — none carry a secret: repositories never expose
# `encryption_key_ref`; the manifest response (`BackupManifest`) is metadata
# only (paths/sizes/chunk ids), never chunk bytes.
# --------------------------------------------------------------------------- #


class RepositoryOut(BaseModel):
    id: UUID
    # The one client whose devices back up here (plan 2026-10-26 Task 13).
    client_id: UUID
    name: str
    storage_ref: str
    state: str
    dedup_params: dict[str, Any]
    created_at: datetime
    offsite_target_id: UUID | None = None


class RepositoryCreateRequest(BaseModel):
    # Required: a repository is one client's, and nothing else in the request
    # can say which (`clients.client_for_new_repository`).
    client_id: UUID
    name: str
    dedup_params: dict[str, Any] | None = None


class PolicyOut(BaseModel):
    id: UUID
    # The group's client, or the client whose default this is. NULL only on
    # a row from before migration 0242's guard, which names no repository
    # the resolution will use.
    client_id: UUID | None
    device_group_id: UUID | None
    repository_id: UUID
    sources: dict[str, Any]
    window_cron: str | None
    retention_gfs: dict[str, Any]
    enabled: bool


class PolicyUpsertRequest(BaseModel):
    device_group_id: UUID | None = None
    # With no group, the client whose default policy this is. With a group it
    # may be left out: the group's client is the policy's, and a different
    # one is a 422.
    client_id: UUID | None = None
    repository_id: UUID
    sources: dict[str, Any] = {}
    window_cron: str | None = None
    retention_gfs: dict[str, Any] = {}
    enabled: bool = True


class RunResponse(BaseModel):
    job_ids: list[UUID]


class BackupPointOut(BaseModel):
    id: UUID
    device_id: UUID
    repository_id: UUID
    backup_run_id: UUID
    taken_at: datetime
    size: int
    state: str
    verified_at: datetime | None
    restore_tested_at: datetime | None
    restore_test_level: str | None = None
    restore_test_scope: str | None = None
    # From format-2 agents: `live` / crash_consistent false means files were
    # read while in use rather than from a snapshot.
    consistency_level: str | None = None
    crash_consistent: bool | None = None
    warnings: list[str] | None = None


class RestoreRequest(BaseModel):
    selection: list[str]
    target: dict[str, Any]


class RestoreResponse(BaseModel):
    job_ids: list[UUID]


class LastGoodPointOut(BaseModel):
    backup_point_id: UUID
    taken_at: datetime
    size: int
    repository_id: UUID


class RepositoryRefOut(BaseModel):
    id: UUID
    name: str


class DeviceBackupStatusOut(BaseModel):
    device_id: UUID
    last_good_point: LastGoodPointOut | None
    last_run_state: str | None
    last_run_finished_at: datetime | None
    verified_at: datetime | None
    restore_tested_at: datetime | None
    repository: RepositoryRefOut | None


class DedupOut(BaseModel):
    bytes_protected_total: int
    bytes_stored_total: int
    dedup_ratio: float
    savings_pct: float


class FleetBackupReportOut(BaseModel):
    devices_total: int
    devices_covered: int
    coverage_rate: float
    run_counts_by_state: dict[str, int]
    success_rate: float
    points_needing_verification: int
    dedup: DedupOut


def _repository_out(row: BackupRepository) -> RepositoryOut:
    return RepositoryOut(
        id=row.id,
        client_id=row.client_id,
        name=row.name,
        storage_ref=row.storage_ref,
        state=row.state,
        dedup_params=row.dedup_params,
        created_at=row.created_at,
        offsite_target_id=row.offsite_target_id,
    )


def _policy_out(row: BackupPolicy, client_id: UUID | None = None) -> PolicyOut:
    """`client_id`, when the caller has it: after an UPDATE flush the row's
    own is expired (its trigger may write it), and an async session cannot
    lazy-load it."""
    return PolicyOut(
        id=row.id,
        client_id=client_id if client_id is not None else row.client_id,
        device_group_id=row.device_group_id,
        repository_id=row.repository_id,
        sources=row.sources,
        window_cron=row.window_cron,
        retention_gfs=row.retention_gfs,
        enabled=row.enabled,
    )


def _point_out(row: BackupPoint) -> BackupPointOut:
    return BackupPointOut(
        id=row.id,
        device_id=row.device_id,
        repository_id=row.repository_id,
        backup_run_id=row.backup_run_id,
        taken_at=row.taken_at,
        size=row.size,
        state=row.state,
        verified_at=row.verified_at,
        restore_tested_at=row.restore_tested_at,
        restore_test_level=row.restore_test_level,
        restore_test_scope=row.restore_test_scope,
        consistency_level=row.consistency_level,
        crash_consistent=row.crash_consistent,
        warnings=row.warnings,
    )


def _object_store(request: Request) -> ObjectStore:
    """Lazily build (and cache) the object store on first use — mirrors
    ``app.modules.deploy.api._object_store``. Construction hits the object
    store to ensure the bucket exists, so it stays out of app startup;
    manifest browsing is the only backup-module path that needs it."""
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(request.app.state.settings)
        request.app.state.object_store = store
    return store


# --------------------------------------------------------------------------- #
# Repositories
# --------------------------------------------------------------------------- #


@router.get("/repositories", response_model=list[RepositoryOut])
async def list_repositories(
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.read")),
) -> list[RepositoryOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = (
            (
                await session.execute(
                    select(BackupRepository)
                    .where(BackupRepository.org_id == ctx.org_id)
                    .order_by(BackupRepository.id)
                )
            )
            .scalars()
            .all()
        )
        return [_repository_out(r) for r in rows]


@router.post("/repositories", status_code=status.HTTP_201_CREATED, response_model=RepositoryOut)
async def create_repository_route(
    body: RepositoryCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.policy.manage")),
) -> RepositoryOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            repo = await create_repository(
                session,
                state.vault,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                client_id=body.client_id,
                name=body.name,
                dedup_params=body.dedup_params,
            )
        except BackupClientError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        except IntegrityError as exc:
            # The name is taken in this client: the key is per client, so the
            # answer says nothing about another client's names.
            if violated_constraint(exc) == REPOSITORY_NAME_KEY:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    tr("a repository with that name exists in this client"),
                ) from None
            raise
        return _repository_out(repo)


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.read")),
) -> list[PolicyOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = (
            (
                await session.execute(
                    select(BackupPolicy)
                    .where(BackupPolicy.org_id == ctx.org_id)
                    .order_by(BackupPolicy.id)
                )
            )
            .scalars()
            .all()
        )
        return [_policy_out(r) for r in rows]


@router.put("/policies", response_model=PolicyOut)
async def upsert_policy(
    body: PolicyUpsertRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.policy.manage")),
) -> PolicyOut:
    """One group's policy, or one client's default (no group, `client_id`).
    The repository must be the policy's client's (`policy.upsert_policy`)."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            row, client_id = await upsert_backup_policy(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                device_group_id=body.device_group_id,
                client_id=body.client_id,
                repository_id=body.repository_id,
                sources=body.sources,
                window_cron=body.window_cron,
                retention_gfs=body.retention_gfs,
                enabled=body.enabled,
            )
        except BackupClientError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        return _policy_out(row, client_id)


# --------------------------------------------------------------------------- #
# POST /devices/{device_id}/run
# --------------------------------------------------------------------------- #


@router.post("/devices/{device_id}/run", response_model=RunResponse)
async def run_backup(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.run")),
) -> RunResponse:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        pending = await dispatch_backup(
            session,
            state.job_engine,
            state.vault,
            org_id=ctx.org_id,
            device_id=device_id,
            now=datetime.now(UTC),
            actor_id=ctx.user_id,
            actor_type="user",
        )
    # Transaction committed: safe to publish the signed jobs to the agents.
    for _job_id, device_id_, envelope in pending:
        await publish_job(state.redis, device_id_, envelope)
    return RunResponse(job_ids=[job_id for job_id, _, _ in pending])


# --------------------------------------------------------------------------- #
# Points + manifest browsing + device status tab
# --------------------------------------------------------------------------- #


@router.get("/points", response_model=list[BackupPointOut])
async def list_points(
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.read")),
) -> list[BackupPointOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = (
            (
                await session.execute(
                    select(BackupPoint)
                    .where(BackupPoint.org_id == ctx.org_id)
                    .order_by(BackupPoint.taken_at.desc(), BackupPoint.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_point_out(r) for r in rows]


@router.get("/points/{point_id}/manifest", response_model=BackupManifest)
async def get_manifest(
    point_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.read")),
) -> BackupManifest:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            return await browse_manifest(
                session,
                _object_store(request),
                state.vault,
                org_id=ctx.org_id,
                backup_point_id=point_id,
            )
        except NoResultFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such backup point")) from exc


@router.get("/devices/{device_id}/backup", response_model=DeviceBackupStatusOut)
async def device_backup_status_route(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.read")),
) -> DeviceBackupStatusOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        data = await device_backup_status(session, ctx.org_id, device_id)
        return DeviceBackupStatusOut(**data)


# --------------------------------------------------------------------------- #
# Restore + verify
# --------------------------------------------------------------------------- #


@router.post("/points/{point_id}/restore", response_model=RestoreResponse)
async def restore_point(
    point_id: UUID,
    body: RestoreRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.restore")),
) -> RestoreResponse:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        pending = await dispatch_restore(
            session,
            state.job_engine,
            state.vault,
            org_id=ctx.org_id,
            backup_point_id=point_id,
            selection=body.selection,
            target=body.target,
            mode="restore",
            now=datetime.now(UTC),
            actor_id=ctx.user_id,
            actor_type="user",
        )
    # Transaction committed: safe to publish the signed jobs to the agents.
    for _job_id, device_id_, envelope in pending:
        await publish_job(state.redis, device_id_, envelope)
    return RestoreResponse(job_ids=[job_id for job_id, _, _ in pending])


@router.post("/points/{point_id}/verify", response_model=RestoreResponse)
async def verify_point(
    point_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.restore")),
) -> RestoreResponse:
    """Prove `point_id` restorable without a caller-chosen selection/target —
    a verify covers the whole point, so both are empty: the agent restores
    every entry in the manifest into a scratch target and checksums it,
    never landing files at any caller-chosen destination (`dispatch_restore`'s
    `mode="verify"` contract)."""
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        pending = await dispatch_restore(
            session,
            state.job_engine,
            state.vault,
            org_id=ctx.org_id,
            backup_point_id=point_id,
            selection=[],
            target={},
            mode="verify",
            now=datetime.now(UTC),
            actor_id=ctx.user_id,
            actor_type="user",
        )
    # Transaction committed: safe to publish the signed jobs to the agents.
    for _job_id, device_id_, envelope in pending:
        await publish_job(state.redis, device_id_, envelope)
    return RestoreResponse(job_ids=[job_id for job_id, _, _ in pending])


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


@router.get("/report", response_model=FleetBackupReportOut)
async def report(
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.read")),
) -> FleetBackupReportOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        data = await fleet_backup_report(session, ctx.org_id)
        return FleetBackupReportOut(**data)


_EXPORT_COLUMNS = (
    "device_id",
    "hostname",
    "covered",
    "last_backup_taken_at",
    "verified_at",
    "restore_tested_at",
    "restore_proven",
)

# CSV formula-injection escaping (`app.core.csv_export.escape_formula`) is
# applied per column, not per row: `hostname` is the only column here that
# is genuinely agent-/operator-supplied free text (`app.models.core.Device.
# hostname` -- unconstrained). `device_id` is a UUID, `covered`/
# `restore_proven` are booleans, and the three `*_at` columns are timestamps
# this file itself isoformats below -- none of those can carry a formula
# payload, so escaping them would only risk corrupting a value that was
# never operator text in the first place.
_EXPORT_ESCAPED_COLUMNS: frozenset[str] = frozenset({"hostname"})


def _csv_cell(col: str, value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if col in _EXPORT_ESCAPED_COLUMNS:
        return escape_formula(value)
    return value


async def _export_csv_lines(rows: list[dict[str, Any]]) -> AsyncIterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_EXPORT_COLUMNS)
    yield buffer.getvalue()
    for row in rows:
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(_csv_cell(col, row[col]) for col in _EXPORT_COLUMNS)
        yield buffer.getvalue()


@router.get("/report/export")
async def report_export(
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.read")),
) -> StreamingResponse:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await backup_evidence_rows(session, ctx.org_id)
        # An evidence export is itself a privileged action (invariant #4):
        # someone pulled SOC 2/BCP proof of coverage out of the system.
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="backup.evidence_exported",
            target_type="organization",
            target_id=str(ctx.org_id),
            metadata={"row_count": len(rows)},
        )
    return StreamingResponse(
        _export_csv_lines(rows),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="backup-evidence.csv"'},
    )
