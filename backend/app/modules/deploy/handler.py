"""`app.deploy` / `script.run` result handlers — the module's kernel hookup.

Mirrors `app/modules/patch/handler.py`: `register_job_types()` replaces the
kernel's bare registration with one carrying the ingestion handler, so the
job engine calls it (inside the job's tenant transaction) after a succeeded
`app.deploy`/`script.run` job result comes back from the agent. Queues a
`deploy_status_updated`/`script_run_updated` UI event (drained and published
by the kernel only after the transaction commits, per `queue_ui_event`'s
contract) so the operator UI can refetch a device's deployment/script state.
"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.dispatch import queue_job_ui_event
from app.jobs.registry import JobType, register
from app.models import Job
from app.modules.deploy.artifacts import redact_deploy_params, refresh_deploy_params
from app.modules.deploy.dispatch import ingest_deploy_result
from app.modules.deploy.models import DeploymentResult, ScriptRun
from app.modules.deploy.packages import DeployValidationError
from app.modules.deploy.schema import (
    AppDeployParams,
    AppDeployResult,
    ScriptRunParams,
    ScriptRunResult,
)
from app.modules.deploy.scripts import ingest_script_result


def register_job_types() -> None:
    async def handle_deploy_result(
        session: AsyncSession, job: Job, result: dict[str, Any]
    ) -> None:
        try:
            res = AppDeployResult(**result)
        except ValidationError as exc:
            raise DeployValidationError(  # i18n-exempt: stored as the job's failure reason
                f"invalid app.deploy result: {exc}"
            ) from exc
        await ingest_deploy_result(session, job, res)
        await queue_job_ui_event(
            session,
            job,
            {
                "kind": "deploy_status_updated",
                "device_id": str(job.target_id),
                "job_id": str(job.id),
            },
        )

    async def handle_script_result(
        session: AsyncSession, job: Job, result: dict[str, Any]
    ) -> None:
        try:
            res = ScriptRunResult(**result)
        except ValidationError as exc:
            raise DeployValidationError(  # i18n-exempt: stored as the job's failure reason
                f"invalid script.run result: {exc}"
            ) from exc
        await ingest_script_result(session, job, res)
        await queue_job_ui_event(
            session,
            job,
            {
                "kind": "script_run_updated",
                "device_id": str(job.target_id),
                "job_id": str(job.id),
            },
        )

    async def relink_deployment(session: AsyncSession, old: Job, new: Job) -> None:
        """Repoint this device's `DeploymentResult` at the retried job.

        **WITHOUT THIS THE RETRY WOULD INSTALL THE PACKAGE AND THE DEPLOYMENT
        WOULD SHOW PENDING FOREVER.** `ingest_deploy_result` finds its row with
        `DeploymentResult.job_id == job.id` and, finding none, "is skipped
        gracefully rather than raising" -- so a retry with a new job id runs on
        the endpoint, the agent reports success, and the module's record never
        learns of it. Nothing goes red anywhere. Declaring this hook is also
        what makes `app.deploy` retryable through the kernel route at all
        (2026-08-22-job-lifecycle-design.md §4.4): a governed type without one
        is refused with a 403 naming the module's own re-run route.

        THE ROW GOES BACK TO `pending` AND THE PER-ATTEMPT FIELDS ARE CLEARED,
        which is not information loss even though it looks like it. There is
        ONE `DeploymentResult` per (deployment, device) by unique constraint,
        so the row has always meant "the latest attempt"; leaving `failed` and
        an exit code on it while a fresh attempt is in flight would be the row
        describing a job that is no longer the one it points at. The first
        attempt's evidence is where it has always been and where a retry never
        touches it: the original `jobs` row's own `result`, plus the
        `jobs.failed` event in the hash-chained audit log.

        The parent `Deployment` rollup needs no help. It flips to
        `complete`/`failed` only when EVERY result is terminal, so a row back
        at `pending` simply holds the rollup until the retry reports -- at
        which point `ingest_deploy_result` recomputes it from all the siblings.

        EXPLICIT `org_id` PREDICATE (invariant #5), like every other query in
        this module.
        """
        result = (
            await session.execute(
                select(DeploymentResult).where(
                    DeploymentResult.org_id == old.org_id,
                    DeploymentResult.job_id == old.id,
                )
            )
        ).scalar_one_or_none()
        if result is None:
            # No record to repoint -- the row was never created, or an earlier
            # retry already moved it. The retry itself is still correct, and
            # raising here would fail a dispatch over bookkeeping.
            return
        result.job_id = new.id
        result.state = "pending"
        result.exit_code = None
        result.log_ref = None
        result.detected_after = None
        result.finished_at = None
        session.add(result)

    async def relink_script_run(session: AsyncSession, old: Job, new: Job) -> None:
        """Repoint this device's `ScriptRun` at the retried job.

        Same shape and same reason as `relink_deployment` above:
        `ingest_script_result` looks the row up by `ScriptRun.job_id` and skips
        a report it cannot match, so a retry without this would execute the
        script as SYSTEM/root on the endpoint and leave the run record pointing
        at a job that will never speak again -- which is precisely the state
        `script_runs` exists to make impossible ("what ran is identifiable
        afterwards").
        """
        run = (
            await session.execute(
                select(ScriptRun).where(
                    ScriptRun.org_id == old.org_id, ScriptRun.job_id == old.id
                )
            )
        ).scalar_one_or_none()
        if run is None:
            return
        run.job_id = new.id
        run.state = "pending"
        run.exit_code = None
        run.log_ref = None
        run.finished_at = None
        session.add(run)

    # `app.deploy` signs a BEARER CREDENTIAL into its params — the presigned
    # artifact URL — so it declares both of the kernel's params extension
    # points. Without `params_refresher` a retry re-signs a URL scoped to a
    # window that has moved on; without `params_redactor` a `jobs.read` holder
    # reads a live download grant out of `GET /jobs/{id}`.
    register(
        JobType(
            "app.deploy",
            AppDeployParams,
            result_handler=handle_deploy_result,
            params_refresher=refresh_deploy_params,
            params_redactor=redact_deploy_params,
            retry_relinker=relink_deployment,
        )
    )
    # `script.run` IS INTERRUPTIBLE AND `app.deploy` IS NOT, deliberately
    # (design §9.1). Both shell out through `exec.CommandContext`, so cancelling
    # kills the direct child -- but a stopped script is a script that did less,
    # while a stopped INSTALL is a machine with a half-written package on it,
    # and the installer's own descendants are not guaranteed to die with it
    # (`deploy/exec.go`, from measurement on this project). Granting
    # `app.deploy` the flag is a decision for the people who know what its
    # rollback looks like, in its own cycle.
    register(
        JobType(
            "script.run",
            ScriptRunParams,
            result_handler=handle_script_result,
            cancellable=True,
            retry_relinker=relink_script_run,
        )
    )
