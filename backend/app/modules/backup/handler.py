"""`backup.run` / `backup.restore` result handlers — the module's kernel
hookup.

Mirrors `app/modules/deploy/handler.py`: `register_job_types()` replaces the
kernel's bare registrations with ones carrying the ingestion handlers, so the
job engine calls them (inside the job's tenant transaction) after a
succeeded `backup.run`/`backup.restore` job result comes back from the
agent. Each queues a `backup_status_updated` UI event (drained and published
by the kernel only after the transaction commits, per `queue_ui_event`'s
contract) so the operator UI can refetch a device's backup state.
"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.dispatch import queue_ui_event
from app.jobs.registry import JobType, register
from app.models import Job
from app.modules.backup import dedup
from app.modules.backup.dispatch import ingest_backup_result
from app.modules.backup.restore import ingest_restore_result
from app.modules.backup.schema import (
    BackupRestoreParams,
    BackupRestoreResult,
    BackupRunParams,
    BackupRunResult,
)


class BackupValidationError(Exception):
    pass


def strip_repo_key(params: dict[str, Any]) -> dict[str, Any]:
    """Remove the repository encryption key from a copy of `params`.

    Both backup job types carry the repository key to the agent inside their
    signed params. Without this, `GET /api/v1/jobs/{id}` returned it to every
    `jobs.read` holder, and it stayed in `jobs.params` after the job finished
    (invariant 3). Used as the API redactor and as the terminal scrubber.
    """
    safe = dict(params)
    safe.pop("repo_key", None)
    return safe


def register_job_types() -> None:
    async def handle_backup_result(
        session: AsyncSession, job: Job, result: dict[str, Any]
    ) -> None:
        try:
            res = BackupRunResult(**result)
        except ValidationError as exc:
            raise BackupValidationError(  # i18n-exempt: stored as the job's failure reason
                f"invalid backup.run result: {exc}"
            ) from exc
        # `dedup.gateway_object_store` -- the function itself, unbound and
        # UNCALLED (not a bound import, and not `()`-invoked here) -- so tests
        # can monkeypatch `dedup.gateway_object_store` exactly as
        # `test_backup_dedup.py` already does for the chunk-query path, and so
        # a `state="failed"` result never constructs a real `ObjectStore` at
        # all: `ingest_backup_result` only calls this factory on the success
        # path that actually needs a store.
        await ingest_backup_result(session, dedup.gateway_object_store, job, res)
        queue_ui_event(
            session,
            job.org_id,
            {
                "kind": "backup_status_updated",
                "device_id": str(job.target_id),
                "job_id": str(job.id),
            },
        )

    async def handle_restore_result(
        session: AsyncSession, job: Job, result: dict[str, Any]
    ) -> None:
        try:
            res = BackupRestoreResult(**result)
        except ValidationError as exc:
            raise BackupValidationError(  # i18n-exempt: stored as the job's failure reason
                f"invalid backup.restore result: {exc}"
            ) from exc
        await ingest_restore_result(session, job, res)
        queue_ui_event(
            session,
            job.org_id,
            {
                "kind": "backup_status_updated",
                "device_id": str(job.target_id),
                "job_id": str(job.id),
            },
        )

    # `cancellable` (2026-08-22-job-lifecycle-design.md §9.1): the agent's
    # backup loop checks `ctx.Err()`, and a run stopped part-way leaves a
    # PARTIAL REPOSITORY the module reconciles rather than a corrupted one --
    # which is the property that makes the flag safe here and not on
    # `backup.restore`, where the half-written thing is the operator's data.
    register(
        JobType(
            "backup.run",
            BackupRunParams,
            result_handler=handle_backup_result,
            cancellable=True,
            params_redactor=strip_repo_key,
            terminal_params_scrubber=strip_repo_key,
        )
    )
    register(
        JobType(
            "backup.restore",
            BackupRestoreParams,
            result_handler=handle_restore_result,
            params_redactor=strip_repo_key,
            terminal_params_scrubber=strip_repo_key,
        )
    )
