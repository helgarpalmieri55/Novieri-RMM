"""Off-site copy and recovery-key routes (spec-server-backup.md §6, §11).

Under `/api/v1/backup`, every route RBAC-gated server-side (invariant 6). No
response carries the off-site secret, the credential id behind it, a
repository key, or anything derived from a private key (invariant 3): the
secret is accepted once, sealed in the vault, and only the replication worker
ever decrypts it. Testing a target therefore happens in the worker; this API
only asks for it.
"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.modules.backup.integrity import violated_constraint
from app.modules.backup.models import (
    BackupOffsiteTarget,
    BackupPoint,
    BackupPointCopy,
    BackupRecoveryKey,
)
from app.modules.backup.offsite import (
    TARGET_NAME_KEY,
    OffsiteClientError,
    OffsiteError,
    OffsiteSecret,
    create_target,
    get_target,
    request_test,
    set_repository_target,
    target_name_taken,
    update_target,
)
from app.modules.backup.recovery import (
    RecoveryKeyError,
    get_recovery_key,
    list_recovery_keys,
    prove_recovery_key,
    register_recovery_key,
    reissue_challenge,
)

router = APIRouter(prefix="/backup", tags=["backup"])

_GFS_KEYS = frozenset({"daily", "weekly", "monthly", "yearly"})


def _check_gfs(value: dict[str, int]) -> dict[str, int]:
    if set(value) - _GFS_KEYS or any(not isinstance(v, int) or v < 0 for v in value.values()):
        raise ValueError(tr("retention takes daily, weekly, monthly and yearly counts"))
    return value


class _TargetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    endpoint: str | None = Field(default=None, max_length=500)
    region: str = Field(default="us-east-1", min_length=1, max_length=64)
    bucket: str = Field(min_length=3, max_length=255)
    prefix: str = Field(default="", max_length=255)
    addressing: Literal["path", "virtual"] = "path"
    lock_mode: Literal["compliance", "governance"] = "compliance"
    immutable_days: int = Field(default=30, ge=1, le=90)
    retention_gfs: dict[str, int] = Field(default_factory=dict)
    max_mbps: int | None = Field(default=None, gt=0)

    @field_validator("retention_gfs")
    @classmethod
    def _gfs(cls, value: dict[str, int]) -> dict[str, int]:
        return _check_gfs(value)

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        if not value.startswith(("https://", "http://")):
            raise ValueError(
                tr("the address starts with https:// (or http:// on a private network)")
            )
        return value.strip()


class TargetCreate(_TargetSettings):
    access_key_id: str = Field(min_length=1, max_length=256)
    secret_access_key: str = Field(min_length=1, max_length=512)


class TargetUpdate(_TargetSettings):
    # Both or neither: leave blank to keep the stored key.
    access_key_id: str | None = Field(default=None, max_length=256)
    secret_access_key: str | None = Field(default=None, max_length=512)


class TargetTestStep(BaseModel):
    step: str
    ok: bool
    detail: str = ""


class TargetOut(BaseModel):
    id: UUID
    name: str
    endpoint: str | None
    region: str
    bucket: str
    prefix: str
    addressing: str
    lock_mode: str
    immutable_days: int
    retention_gfs: dict[str, int]
    max_mbps: int | None
    state: str
    test_requested_at: datetime | None
    tested_at: datetime | None
    test_steps: list[TargetTestStep]
    last_ok_at: datetime | None
    last_error: str | None


class RepositoryOffsiteRequest(BaseModel):
    target_id: UUID | None


class RepositoryOffsiteOut(BaseModel):
    repository_id: UUID
    offsite_target_id: UUID | None


class RecoveryKeyCreate(BaseModel):
    public_key: str = Field(min_length=1, max_length=200)
    # The console only submits once the operator confirms the private key is
    # stored; the server never sees it, so this is the only record of that.
    stored_confirmed: bool


class RecoveryKeyOut(BaseModel):
    id: UUID
    fingerprint: str
    public_key: str
    proven_at: datetime | None
    challenge_expires_at: datetime | None
    created_at: datetime
    retired_at: datetime | None


class RecoveryKeyChallengeOut(BaseModel):
    key: RecoveryKeyOut
    challenge: str


class RecoveryKeyProve(BaseModel):
    answer: str = Field(min_length=1, max_length=200)


class PointCopyOut(BaseModel):
    target_id: UUID
    state: str
    copied_at: datetime | None
    bytes: int
    locked_until: datetime | None
    last_error: str | None


def _target_out(t: BackupOffsiteTarget) -> TargetOut:
    last: dict[str, Any] = t.last_test or {}
    tested_at = last.get("at")
    return TargetOut(
        id=t.id,
        name=t.name,
        endpoint=t.endpoint,
        region=t.region,
        bucket=t.bucket,
        prefix=t.prefix,
        addressing=t.addressing,
        lock_mode=t.lock_mode,
        immutable_days=t.immutable_days,
        retention_gfs=t.retention_gfs or {},
        max_mbps=t.max_mbps,
        state=t.state,
        test_requested_at=t.test_requested_at,
        tested_at=datetime.fromisoformat(tested_at) if isinstance(tested_at, str) else None,
        test_steps=[TargetTestStep(**s) for s in last.get("steps", [])],
        last_ok_at=t.last_ok_at,
        last_error=t.last_error,
    )


def _key_out(k: BackupRecoveryKey) -> RecoveryKeyOut:
    return RecoveryKeyOut(
        id=k.id,
        fingerprint=k.fingerprint,
        public_key=k.public_key,
        proven_at=k.proven_at,
        challenge_expires_at=k.challenge_expires_at,
        created_at=k.created_at,
        retired_at=k.retired_at,
    )


def _settings(body: _TargetSettings) -> dict[str, Any]:
    return body.model_dump(exclude={"access_key_id", "secret_access_key"})


def _name_exists() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, tr("An off-site target with that name exists."))


def _name_clash(exc: IntegrityError) -> bool:
    """A unique violation on the target-name key: two writes raced past the
    check (or a rename met a name), and the answer is the check's 409."""
    return violated_constraint(exc) == TARGET_NAME_KEY


# --------------------------------------------------------------------------- #
# Off-site targets
# --------------------------------------------------------------------------- #


@router.get("/offsite-targets", response_model=list[TargetOut])
async def list_targets(
    request: Request, ctx: AuthContext = Depends(require_permission("backup.read"))
) -> list[TargetOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = (
            await session.execute(
                select(BackupOffsiteTarget)
                .where(BackupOffsiteTarget.org_id == ctx.org_id)
                .order_by(BackupOffsiteTarget.name)
            )
        ).scalars().all()
        return [_target_out(r) for r in rows]


@router.post("/offsite-targets", status_code=status.HTTP_201_CREATED, response_model=TargetOut)
async def create_target_route(
    body: TargetCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.offsite.manage")),
) -> TargetOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        # A new target is global (phase 1 writes no client): the global names.
        if await target_name_taken(session, ctx.org_id, None, body.name):
            raise _name_exists()
        try:
            target = await create_target(
                session,
                state.vault,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                settings=_settings(body),
                secret=OffsiteSecret(body.access_key_id, body.secret_access_key),
            )
        except IntegrityError as exc:
            if _name_clash(exc):
                raise _name_exists() from None
            raise
        await session.refresh(target)
        return _target_out(target)


@router.put("/offsite-targets/{target_id}", response_model=TargetOut)
async def update_target_route(
    target_id: UUID,
    body: TargetUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.offsite.manage")),
) -> TargetOut:
    if (body.access_key_id is None) != (body.secret_access_key is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            tr("Give both the access key and the secret, or neither to keep the stored ones."),
        )
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        target = await get_target(session, ctx.org_id, target_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such off-site target"))
        secret = (
            OffsiteSecret(body.access_key_id, body.secret_access_key)
            if body.access_key_id is not None and body.secret_access_key is not None
            else None
        )
        try:
            await update_target(
                session, state.vault, target, actor_id=ctx.user_id, settings=_settings(body),
                secret=secret,
            )
        except IntegrityError as exc:
            if _name_clash(exc):
                raise _name_exists() from None
            raise
        await session.refresh(target)
        return _target_out(target)


@router.post(
    "/offsite-targets/{target_id}/test",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TargetOut,
)
async def test_target_route(
    target_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.offsite.manage")),
) -> TargetOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        target = await get_target(session, ctx.org_id, target_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such off-site target"))
        await request_test(session, target, actor_id=ctx.user_id, now=datetime.now(UTC))
        return _target_out(target)


@router.put("/repositories/{repository_id}/offsite", response_model=RepositoryOffsiteOut)
async def set_repository_offsite(
    repository_id: UUID,
    body: RepositoryOffsiteRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.offsite.manage")),
) -> RepositoryOffsiteOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            repo = await set_repository_target(
                session,
                org_id=ctx.org_id,
                repository_id=repository_id,
                target_id=body.target_id,
                actor_id=ctx.user_id,
            )
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except OffsiteClientError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        except OffsiteError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return RepositoryOffsiteOut(repository_id=repo.id, offsite_target_id=repo.offsite_target_id)


@router.get("/points/{point_id}/copies", response_model=list[PointCopyOut])
async def list_point_copies(
    point_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.read")),
) -> list[PointCopyOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        point = (
            await session.execute(
                select(BackupPoint.id).where(
                    BackupPoint.org_id == ctx.org_id, BackupPoint.id == point_id
                )
            )
        ).scalar_one_or_none()
        if point is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such backup point"))
        rows = (
            await session.execute(
                select(BackupPointCopy).where(
                    BackupPointCopy.org_id == ctx.org_id, BackupPointCopy.point_id == point_id
                )
            )
        ).scalars().all()
        return [
            PointCopyOut(
                target_id=r.target_id,
                state=r.state,
                copied_at=r.copied_at,
                bytes=r.bytes,
                locked_until=r.locked_until,
                last_error=r.last_error,
            )
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# Recovery keys
# --------------------------------------------------------------------------- #


@router.get("/recovery-keys", response_model=list[RecoveryKeyOut])
async def list_keys(
    request: Request, ctx: AuthContext = Depends(require_permission("backup.read"))
) -> list[RecoveryKeyOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return [_key_out(k) for k in await list_recovery_keys(session, ctx.org_id)]


@router.post(
    "/recovery-keys",
    status_code=status.HTTP_201_CREATED,
    response_model=RecoveryKeyChallengeOut,
)
async def register_key(
    body: RecoveryKeyCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.recovery.manage")),
) -> RecoveryKeyChallengeOut:
    if not body.stored_confirmed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            tr("Confirm the private key is stored safely before adding the recovery key."),
        )
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            row, challenge = await register_recovery_key(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                public_key_text=body.public_key,
                now=datetime.now(UTC),
            )
        except RecoveryKeyError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="backup.recovery_key_storage_confirmed",
            target_type="backup_recovery_key",
            target_id=str(row.id),
            metadata={"fingerprint": row.fingerprint},
        )
        await session.refresh(row)
        return RecoveryKeyChallengeOut(key=_key_out(row), challenge=challenge)


async def _key_or_404(session: Any, org_id: UUID, key_id: UUID) -> BackupRecoveryKey:
    row = await get_recovery_key(session, org_id, key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such recovery key"))
    return row


@router.post("/recovery-keys/{key_id}/challenge", response_model=RecoveryKeyChallengeOut)
async def new_challenge(
    key_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.recovery.manage")),
) -> RecoveryKeyChallengeOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        row = await _key_or_404(session, ctx.org_id, key_id)
        try:
            challenge = await reissue_challenge(session, row, now=datetime.now(UTC))
        except RecoveryKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return RecoveryKeyChallengeOut(key=_key_out(row), challenge=challenge)


@router.post("/recovery-keys/{key_id}/prove", response_model=RecoveryKeyOut)
async def prove_key(
    key_id: UUID,
    body: RecoveryKeyProve,
    request: Request,
    ctx: AuthContext = Depends(require_permission("backup.recovery.manage")),
) -> RecoveryKeyOut:
    # The failure is audited inside `prove_recovery_key`; commit it before
    # answering 422, so a wrong answer leaves its record.
    error: str | None = None
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        row = await _key_or_404(session, ctx.org_id, key_id)
        try:
            await prove_recovery_key(
                session, row, answer=body.answer, actor_id=ctx.user_id, now=datetime.now(UTC)
            )
        except RecoveryKeyError as exc:
            error = str(exc)
        out = _key_out(row)
    if error is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, error)
    return out
