"""Move backup runs and restores out of `running` when their job is over
(spec-server-backup.md §3.3, defect D4).

The job engine calls a module's result handler only for a `succeeded` job, so
a refused, expired, failed or cancelled job never touched its `backup_runs` or
`restore_jobs` row. Those rows then stayed `running` for ever, hiding the
failure from reports and alerts and blocking the next scheduled run.

A job still `running` long after its `expires_at` is also over: the agent
cancels its own work at `expires_at`, so one that never reported has gone
away, and the job engine does not expire running jobs.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.models import BackupRun, Job, RestoreJob

_OVER = ("failed", "expired", "refused", "cancelled")
# How long past `expires_at` a running job may still report before it is
# treated as gone. The agent reports promptly after cancelling at expiry.
REPORT_GRACE = timedelta(hours=1)


def _reason(job: Job) -> str:
    status = str(job.status.value if hasattr(job.status, "value") else job.status)
    result: dict[str, Any] = job.result if isinstance(job.result, dict) else {}
    if status == "refused":
        return f"The device refused the job ({str(result.get('refused', 'no reason'))[:120]})."
    if status == "expired":
        return "The job expired before the device picked it up."
    if status == "cancelled":
        return "The job was cancelled."
    if status == "failed":
        error = result.get("error")
        if isinstance(error, str) and error:
            return error[:500]
        return "The job failed on the device."
    return "The device stopped reporting before the job finished."


def _is_over(now: datetime) -> Any:
    status = cast(Job.status, Text)
    return status.in_(_OVER) | (
        (status == "running") & (Job.expires_at < now - REPORT_GRACE)
    )


async def reconcile_stuck(session: AsyncSession, org_id: UUID, *, now: datetime) -> dict[str, int]:
    """Fail every `running` run/restore in `org_id` whose job is over.
    Returns `{runs, restores}`. Every query carries `org_id` (invariant 5)."""
    runs = (
        await session.execute(
            select(BackupRun, Job)
            .join(Job, (Job.id == BackupRun.job_id) & (Job.org_id == BackupRun.org_id))
            .where(BackupRun.org_id == org_id, BackupRun.state == "running", _is_over(now))
        )
    ).all()
    for run, job in runs:
        run.state = "failed"
        run.error = _reason(job)
        run.finished_at = now
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="backup.run_reconciled",
            target_type="device",
            target_id=str(run.device_id),
            metadata={"run_id": str(run.id), "job_id": str(job.id)},
        )

    restores = (
        await session.execute(
            select(RestoreJob, Job)
            .join(Job, (Job.id == RestoreJob.job_id) & (Job.org_id == RestoreJob.org_id))
            .where(RestoreJob.org_id == org_id, RestoreJob.state == "running", _is_over(now))
        )
    ).all()
    for restore, job in restores:
        restore.state = "failed"
        restore.error = _reason(job)
        restore.finished_at = now
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system",
            actor_id=None,
            action="backup.restore_reconciled",
            target_type="device",
            target_id=str(restore.device_id),
            metadata={"restore_job_id": str(restore.id), "job_id": str(job.id)},
        )
    await session.flush()
    return {"runs": len(runs), "restores": len(restores)}
