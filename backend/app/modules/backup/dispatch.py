"""Signed `backup.run` dispatch (docs/modules/backup/spec.md).

Mirrors `app.modules.deploy.dispatch.dispatch_deployment`: this is the
module's application-security gate for *backup runs* — the only path by
which a device's resolved backup policy becomes a signed `backup.run` job.
Every device job is built via the job engine, signed, and (by the caller,
post-commit) published; no unsigned path exists (invariant #1).

A dispatch is scoped to one `(org_id, device_id)` pair: resolve the device's
effective policy, decide whether a run is due (`schedule.py`), then open the target
repository's master key from the vault and hand it to the agent only inside
the signed job params — the repository key must never appear anywhere else
(not in the audit log, not in a log line, not in an API response).
"""

import base64
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.storage import ObjectStore, _safe_object_suffix
from app.core.stored_english import stored_in_english
from app.core.vault import VaultService
from app.models import BackupPoint, BackupRepository, BackupRun, Device, Job
from app.modules.backup.policy import ResolvedBackupPolicy, resolve_backup_policy
from app.modules.backup.repository import open_repo_key
from app.modules.backup.schedule import BACKUP_JOB_TTL, START_DEADLINE, decide, rfc3339
from app.modules.backup.schema import BackupRunParams, BackupRunResult

_SNAPSHOT_BY_OS_FAMILY: dict[str, Literal["vss", "lvm", "apfs"]] = {
    "windows": "vss",
    "linux": "lvm",
    "macos": "apfs",
}


class _JobEngineLike(Protocol):
    async def create(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        type: str,
        target_device_id: UUID,
        params: dict[str, Any],
        requested_by: UUID | None = None,
        ttl_s: int | None = None,
    ) -> Any: ...

    async def signed_envelope(self, job: Any) -> dict[str, Any]: ...


async def _run_in_progress(session: AsyncSession, org_id: UUID, device_id: UUID) -> bool:
    return (
        await session.execute(
            select(BackupRun.id)
            .where(
                BackupRun.org_id == org_id,
                BackupRun.device_id == device_id,
                BackupRun.state == "running",
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def dispatch_backup(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    vault: VaultService,
    *,
    org_id: UUID,
    device_id: UUID,
    now: datetime,
    actor_id: UUID | None = None,
    actor_type: Literal["user", "agent", "system"] = "system",
) -> list[tuple[UUID, UUID, dict[str, Any]]]:
    """Build one signed `backup.run` job for `device_id`'s effective policy, now.

    The on-demand path: the schedule is not consulted (the sweep calls
    `dispatch_due_backup`, which is). Nothing is dispatched while a run for the
    device is still `running`, so "Run now" twice is one backup.

    Does NOT publish: returns `[(job_id, device_id, envelope)]` for the caller
    to publish after its tenant transaction commits, since the job carries a
    live repository key and must not reach an agent without a durable record.
    Empty when there is no enabled policy, no repository, no OS family, or a
    run in progress.

    The repository key is opened from the vault only to place it in the signed
    params (redacted on read, scrubbed at any terminal state); never audited or
    logged (invariant 3). Every query carries `org_id` (invariant 5).
    """
    policy = await resolve_backup_policy(session, org_id, device_id)
    if policy is None:
        return []
    if await _run_in_progress(session, org_id, device_id):
        return []
    return await _dispatch_for_policy(
        session,
        job_engine,
        vault,
        policy=policy,
        org_id=org_id,
        device_id=device_id,
        now=now,
        actor_id=actor_id,
        actor_type=actor_type,
    )


async def dispatch_due_backup(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    vault: VaultService,
    *,
    org_id: UUID,
    device_id: UUID,
    now: datetime,
) -> list[tuple[UUID, UUID, dict[str, Any]]]:
    """The sweep's path: dispatch when a scheduled time is due, record a
    `missed` run when its start deadline passed unmet (`schedule.decide`).
    System-attributed. Same publish contract as `dispatch_backup`."""
    # A scheduled backup is never due on a device waiting for review: the gate
    # would refuse `backup.run` and abort the sweep for the whole org.
    review_state = (
        await session.execute(
            select(Device.review_state).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    if review_state != "approved":
        return []
    policy = await resolve_backup_policy(session, org_id, device_id)
    if policy is None or policy.window_cron is None:
        return []
    device = (
        await session.execute(
            select(Device).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    if device is None:
        return []

    last_started_at = (
        await session.execute(
            select(func.max(BackupRun.started_at)).where(
                BackupRun.org_id == org_id, BackupRun.device_id == device_id
            )
        )
    ).scalar_one()
    since = [t for t in (policy.created_at, device.created_at) if t is not None]
    decision = decide(
        policy.window_cron,
        now,
        protected_since=max(since) if since else now,
        last_started_at=last_started_at,
        running=await _run_in_progress(session, org_id, device_id),
    )
    if decision.action == "dispatch":
        return await _dispatch_for_policy(
            session,
            job_engine,
            vault,
            policy=policy,
            org_id=org_id,
            device_id=device_id,
            now=now,
            actor_id=None,
            actor_type="system",
        )
    if decision.action == "missed" and decision.fire_at is not None:
        await _record_missed(
            session,
            org_id=org_id,
            device_id=device_id,
            repository_id=policy.repository_id,
            fire_at=decision.fire_at,
            now=now,
        )
    return []


async def _record_missed(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_id: UUID,
    repository_id: UUID | None,
    fire_at: datetime,
    now: datetime,
) -> None:
    """One `missed` row per fire. `started_at = fire_at` is what makes it count
    as "acted on" for that fire, so it is never written twice or retried."""
    if repository_id is None:
        return
    session.add(
        BackupRun(
            org_id=org_id,
            device_id=device_id,
            repository_id=repository_id,
            job_id=None,
            state="missed",
            actor_type="system",
            started_at=fire_at,
            finished_at=now,
            error=(
                "The scheduled backup did not start before its deadline "
                "(device offline, or the scheduler was not running)."
            ),
        )
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="backup.missed",
        target_type="device",
        target_id=str(device_id),
        metadata={"device_id": str(device_id), "scheduled_for": rfc3339(fire_at)},
    )
    await session.flush()


async def _dispatch_for_policy(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    vault: VaultService,
    *,
    policy: ResolvedBackupPolicy,
    org_id: UUID,
    device_id: UUID,
    now: datetime,
    actor_id: UUID | None,
    actor_type: Literal["user", "agent", "system"],
) -> list[tuple[UUID, UUID, dict[str, Any]]]:
    repo = (
        await session.execute(
            select(BackupRepository).where(
                BackupRepository.org_id == org_id,
                BackupRepository.id == policy.repository_id,
            )
        )
    ).scalar_one_or_none()
    if repo is None:
        return []

    device = (
        await session.execute(
            select(Device).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    if device is None:
        return []
    if device.os_family is None:
        # An asset row (a switch, hardware in its box): nothing to back up.
        return []

    key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo.id)

    params = BackupRunParams(
        repository_id=str(repo.id),
        storage_ref=repo.storage_ref,
        sources=policy.sources,
        dedup_params=repo.dedup_params,
        repo_key=base64.b64encode(key).decode(),
        snapshot=_SNAPSHOT_BY_OS_FAMILY[device.os_family],
        not_after_start=rfc3339(now + START_DEADLINE),
    )

    job = await job_engine.create(
        session,
        org_id=org_id,
        type="backup.run",
        target_device_id=device_id,
        params=params.model_dump(),
        requested_by=actor_id,
        ttl_s=int(BACKUP_JOB_TTL.total_seconds()),
    )
    envelope = await job_engine.signed_envelope(job)

    session.add(
        BackupRun(
            org_id=org_id,
            device_id=device_id,
            repository_id=repo.id,
            job_id=job.id,
            state="running",
            actor_type=actor_type,
            requested_by=actor_id,
            started_at=now,
        )
    )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="backup.dispatched",
        target_type="device",
        target_id=str(device_id),
        metadata={
            "repository_id": str(repo.id),
            "device_id": str(device_id),
            "job_id": str(job.id),
        },
    )

    await session.flush()
    return [(job.id, device_id, envelope)]


# `backup_runs.state` domain a completed run may land on (docs/modules/backup/
# spec.md). `BackupRunResult.state` is itself typed `Literal["success",
# "failed"]`, so in the normal (validated) path only those two values ever
# reach this function; the explicit membership check below is defense in
# depth against a value that bypasses the wire model's validation (e.g. a
# handler that skips `BackupRunResult(**result)`), mirroring
# `ingest_deploy_result`'s out-of-domain coercion (the Task-8-deploy lesson):
# an unrecognized state must still land on a terminal `failed` state — never
# raise, never wedge the run — with the original value recorded in the audit
# log so the operator can see why.
_VALID_RUN_RESULT_STATES = {"success", "failed"}


def _resolve_manifest_ref(
    object_store: ObjectStore, org_id: UUID, repository_id: UUID, reported: str
) -> str:
    """Turn the agent's reported SUFFIX into the full object reference.

    The agent does not know the bucket and must not: a POST grant carries a
    key prefix, not a bucket name, and an endpoint that constructed a full
    reference would be naming a bucket the server then trusts. So the agent
    reports `manifests/{name}` and the key is derived here, from ids this
    process is already authorized for -- the same derived-never-accepted rule
    `presign_backup_object` is built on.

    Validation is DELEGATED to `_safe_object_suffix` -- the same validator
    `presign_backup_object` already uses -- rather than reimplemented here, so
    the two paths cannot drift out of sync. An earlier version of this
    function hand-rolled an equivalent set of checks and it was weaker for it:
    it omitted `_safe_object_suffix`'s control-character check, so a
    `manifest_ref` like `"manifests/x\\ty"` (a control character is legal
    inside a JSON string) would have passed straight through into an S3 key.

    A full `s3://...` reference -- an endpoint naming a bucket, which this
    function exists to refuse -- is also caught here, not by a bespoke
    prefix check but because `s3://bucket/key` contains a `//` immediately
    after the scheme, which `_safe_object_suffix` already rejects (confirmed
    by `test_an_absolute_ref_from_an_older_agent_is_refused`); verified
    directly rather than assumed.

    Raises ValueError on anything that could escape the repository's prefix.
    """
    safe_suffix = _safe_object_suffix(reported)
    return object_store.backup_object_ref(org_id, repository_id, safe_suffix)


@stored_in_english
async def ingest_backup_result(
    session: AsyncSession,
    object_store_factory: Callable[[], ObjectStore],
    job: Job,
    result: BackupRunResult,
) -> None:
    """Reconcile a completed `backup.run` job's result (docs/modules/backup/
    spec.md).

    `object_store_factory` is called AT MOST ONCE, and only inside the
    `state == "success" and manifest_ref` branch below. Constructing a real
    `ObjectStore` reaches the network (`resolve_presign_endpoint`, then a
    synchronous `head_bucket`/`create_bucket`), and a `"failed"` report --
    the common case when storage itself is the thing that is down -- needs
    no object store at all. Accepting a factory rather than an already-built
    instance is what keeps this handler able to reconcile that report to a
    terminal state instead of raising, or blocking the event loop, before it
    is even entered.

    Finds the `BackupRun` row `dispatch_backup` created for this job. An
    agent report for a job with no matching row (e.g. the row was never
    created, or this is a stale/duplicate report) is skipped gracefully
    rather than raising — the agent's report must never be able to fail the
    job over server-side bookkeeping.

    `result.state == "success"` AND a truthy `result.manifest_ref` derives the
    full object reference from the agent-reported SUFFIX via
    `_resolve_manifest_ref` (the agent does not know the bucket, so it cannot
    report a full reference itself). Deriving successfully flips the run to
    `success`, records the byte/chunk counters, and creates the `BackupPoint`
    that makes this run's data restorable. Any other case — `"failed"`, an
    out-of-domain value that bypassed wire validation, a `"success"` report
    with a missing/empty `manifest_ref` (schema-legal since
    `BackupRunResult.manifest_ref` is `str | None`, but not persistable since
    `BackupPoint.manifest_ref` is a NON-NULL column), or a `manifest_ref` that
    `_resolve_manifest_ref` refuses (a bucket-naming absolute ref, a
    traversal, or any other unsafe suffix) — lands the run on `failed` with an
    `error` message, and no `BackupPoint` is created. A malformed/incomplete
    or actively unsafe agent report must never crash or wedge this handler;
    it must always resolve to a terminal state (the Task-8-deploy coercion
    lesson, applied here to the success-without-manifest and unsafe-manifest
    cases too).

    Finishes with a single `backup.result` audit event, system-attributed
    since this runs in the kernel's result handler, not on behalf of any
    user. No secret/key data (e.g. the repository key) is ever written to
    the audit metadata.

    EVERY query carries an explicit `org_id` predicate (invariant #5).
    """
    org_id = job.org_id
    device_id = job.target_id
    now = datetime.now(UTC)

    run = (
        await session.execute(
            select(BackupRun).where(
                BackupRun.org_id == org_id,
                BackupRun.job_id == job.id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return  # no matching row; the agent's report must never fail the job

    audit_metadata: dict[str, Any] = {"device_id": str(device_id)}
    # Format-2 agents report these on failure too.
    run.warnings = result.warnings
    if result.consistency is not None:
        run.consistency = result.consistency.model_dump()

    if result.state == "success" and result.manifest_ref:
        try:
            manifest_ref = _resolve_manifest_ref(
                object_store_factory(), run.org_id, run.repository_id, result.manifest_ref
            )
        except ValueError as exc:
            run.state = "failed"
            run.error = f"agent reported an unusable manifest_ref: {exc}"
            audit_metadata["unexpected_result_state"] = "unusable_manifest_ref"
        else:
            run.state = "success"
            run.bytes_protected = result.bytes_protected
            run.bytes_stored = result.bytes_stored
            run.chunk_count = result.chunk_count
            consistency = (
                result.consistency.model_dump() if result.consistency is not None else None
            )

            session.add(
                BackupPoint(
                    org_id=org_id,
                    device_id=device_id,
                    repository_id=run.repository_id,
                    backup_run_id=run.id,
                    taken_at=now,
                    manifest_ref=manifest_ref,
                    size=result.bytes_protected,
                    dedup_stats=result.dedup_stats,
                    state="complete",
                    consistency=consistency,
                    consistency_level=(
                        result.consistency.level if result.consistency is not None else None
                    ),
                    crash_consistent=(
                        result.consistency.crash_consistent
                        if result.consistency is not None
                        else None
                    ),
                    warnings=result.warnings,
                )
            )
        run.finished_at = now
    else:
        run.state = "failed"
        if result.state == "success" and not result.manifest_ref:
            run.error = "success result missing manifest_ref"
            audit_metadata["unexpected_result_state"] = "success_without_manifest"
        else:
            run.error = result.error or f"unexpected result state {result.state!r}"
            if result.state not in _VALID_RUN_RESULT_STATES:
                audit_metadata["unexpected_result_state"] = result.state
        run.finished_at = now

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="backup.result",
        target_type="device",
        target_id=str(device_id),
        metadata={**audit_metadata, "state": run.state},
    )

    await session.flush()
