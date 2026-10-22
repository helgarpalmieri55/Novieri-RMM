"""Deployment + fleet reporting (docs/modules/app-deploy/spec.md).

Pure read-only aggregation over the deploy module's tables. This module
performs **no writes** and dispatches **no jobs** — it exists solely to
answer "what happened on this deployment" and "what does app-deployment
activity look like across the fleet" for the API and frontend.

State vocabularies (see `app/modules/deploy/models.py`):

- `DeploymentResult.state`: pending | running | installed | failed | not_applicable
- `Deployment.state`: pending | dispatching | complete | failed
- `ScriptRun.state`: pending | running | succeeded | failed

`fleet_deploy_report`'s success rate for a deployment is
`installed_count / total_device_result_rows`, computed as `0.0` when a
deployment has zero result rows (nothing dispatched yet) rather than raising
a division error — a deployment that is entirely still `pending`/`running`
therefore also reports `0.0`; callers distinguish "not yet done" from "done
and unsuccessful" via the deployment's own `state` field, not the rate alone.

Every query below filters on `org_id` for every table involved (Deployment,
DeploymentResult, ScriptRun) per the tenancy invariant — RLS is the backstop,
not the only line of defense.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.deploy.models import Deployment, DeploymentResult, ScriptRun

# Order matches the task brief: installed/failed/not_applicable/running/pending.
DEPLOYMENT_RESULT_STATES = ("installed", "failed", "not_applicable", "running", "pending")

DEFAULT_RECENT_LIMIT = 20


async def deployment_report(
    session: AsyncSession, org_id: UUID, deployment_id: UUID
) -> dict[str, Any]:
    """Per-deployment snapshot: one row per device result (device_id, state,
    exit_code, log_ref, finished_at) ordered deterministically by device_id,
    a rollup of counts by state, and the deployment's own
    state/action/package_id.
    """
    deployment = (
        await session.execute(
            select(Deployment).where(Deployment.org_id == org_id, Deployment.id == deployment_id)
        )
    ).scalar_one_or_none()

    result_rows = (
        await session.execute(
            select(
                DeploymentResult.device_id,
                DeploymentResult.state,
                DeploymentResult.exit_code,
                DeploymentResult.log_ref,
                DeploymentResult.finished_at,
            )
            .where(
                DeploymentResult.org_id == org_id,
                DeploymentResult.deployment_id == deployment_id,
            )
            .order_by(DeploymentResult.device_id.asc())
        )
    ).all()

    device_results = [
        {
            "device_id": row.device_id,
            "state": row.state,
            "exit_code": row.exit_code,
            "log_ref": row.log_ref,
            "finished_at": row.finished_at,
        }
        for row in result_rows
    ]

    counts_by_state = dict.fromkeys(DEPLOYMENT_RESULT_STATES, 0)
    for row in result_rows:
        counts_by_state[row.state] += 1

    return {
        "deployment_id": deployment_id,
        "state": deployment.state if deployment else None,
        "action": deployment.action if deployment else None,
        "package_id": deployment.package_id if deployment else None,
        "device_results": device_results,
        "counts_by_state": counts_by_state,
    }


async def fleet_deploy_report(
    session: AsyncSession, org_id: UUID, limit: int = DEFAULT_RECENT_LIMIT
) -> dict[str, Any]:
    """Fleet-wide deployment activity: the most recent `limit` deployments
    (each with its state/action/package_id and installed/total success
    rate), ordered by `created_at` desc, plus the most recent `limit`
    `ScriptRun` rows, also ordered by `created_at` desc. Both orderings
    tie-break on `id` desc for full determinism.
    """
    deployment_rows = (
        (
            await session.execute(
                select(Deployment)
                .where(Deployment.org_id == org_id)
                .order_by(Deployment.created_at.desc(), Deployment.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    deployment_ids = [d.id for d in deployment_rows]
    success_counts = (
        await session.execute(
            select(
                DeploymentResult.deployment_id,
                func.count().filter(DeploymentResult.state == "installed"),
                func.count(),
            )
            .where(
                DeploymentResult.org_id == org_id,
                DeploymentResult.deployment_id.in_(deployment_ids),
            )
            .group_by(DeploymentResult.deployment_id)
        )
    ).all()
    success_by_deployment = {
        dep_id: (installed, total) for dep_id, installed, total in success_counts
    }

    deployments = []
    for deployment in deployment_rows:
        installed, total = success_by_deployment.get(deployment.id, (0, 0))
        success_rate = (installed / total) if total else 0.0
        deployments.append(
            {
                "deployment_id": deployment.id,
                "state": deployment.state,
                "action": deployment.action,
                "package_id": deployment.package_id,
                "created_at": deployment.created_at,
                "device_count": total,
                "success_rate": success_rate,
            }
        )

    script_run_rows = (
        (
            await session.execute(
                select(ScriptRun)
                .where(ScriptRun.org_id == org_id)
                .order_by(ScriptRun.created_at.desc(), ScriptRun.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    script_runs = [
        {
            "script_run_id": run.id,
            "script_id": run.script_id,
            "device_id": run.device_id,
            "state": run.state,
            "exit_code": run.exit_code,
            "finished_at": run.finished_at,
            "created_at": run.created_at,
        }
        for run in script_run_rows
    ]

    return {"deployments": deployments, "script_runs": script_runs}
