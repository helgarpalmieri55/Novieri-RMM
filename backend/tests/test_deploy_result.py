"""Tests for `app.deploy` result ingestion (docs/modules/app-deploy/spec.md).
Mirrors `tests/test_patch_apply.py`'s ingest-side assertions: a
`DeploymentResult` row dispatch created for a device job flips to a terminal
state on report, the parent `Deployment.state` rolls up once every device is
terminal, and the ingestion is graceful (never raises) when there's no
matching row for the job.

Most tests here call `ingest_deploy_result` directly to isolate its own
reconciliation logic. `test_valid_result_through_job_engine_reconciles_and_
queues_ui_event` instead drives the real kernel path
(`JobEngine.complete` -> `app.modules.deploy.handler.handle_deploy_result` ->
`ingest_deploy_result`), so the handler's `AppDeployResult(**result)` parse
and `queue_ui_event` call are actually exercised end-to-end — mirrors
`tests/test_inventory_ingest.py`'s `test_valid_result_through_job_engine_
ingests`.
"""

import datetime as _dt
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.core.wire import InstallOutcome
from app.jobs.engine import JobEngine
from app.models import (
    AuditEvent,
    Deployment,
    DeploymentResult,
    Device,
    Job,
    JobStatus,
    Organization,
    Package,
)
from app.modules.deploy.dispatch import ingest_deploy_result
from app.modules.deploy.handler import register_job_types
from app.modules.deploy.schema import AppDeployResult

# The job-type registry is populated at app-startup time (app.modules.deploy.
# register(app)), not merely on import. Register directly so this file's own
# kernel round-trip test doesn't depend on import order, mirroring
# test_deploy_worker.py's module-level call.
register_job_types()


async def _setup_job(
    admin_engine: AsyncEngine, *, state: str = "running"
) -> tuple[UUID, UUID, UUID, Job]:
    """Returns (org_id, deployment_id, device_id, job) with a dispatched
    `DeploymentResult(state=state)` row wired to the job."""
    slug = f"depr-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        device = Device(
            org_id=org.id, hostname="h1", os_family="windows", client_id=org.id, site_id=org.id
        )
        session.add(device)
        await session.flush()

        package = Package(
            org_id=org.id,
            name="Acme",
            version="1.2",
            os_family="windows",
            installer_type="msi",
        )
        session.add(package)
        await session.flush()

        deployment = Deployment(
            org_id=org.id,
            package_id=package.id,
            action="install",
            target_type="device",
            target_id=device.id,
        )
        session.add(deployment)
        await session.flush()

        job = Job(
            org_id=org.id,
            type="app.deploy",
            target_type="device",
            target_id=device.id,
            params={},
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
            signature="fake-signature",
        )
        session.add(job)
        await session.flush()

        session.add(
            DeploymentResult(
                org_id=org.id,
                deployment_id=deployment.id,
                device_id=device.id,
                job_id=job.id,
                state=state,
            )
        )
        await session.flush()

        await session.commit()
        return org.id, deployment.id, device.id, job


async def test_installed_outcome_flips_result_and_completes_deployment(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, deployment_id, device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = AppDeployResult(
            outcomes=[
                InstallOutcome(
                    identifier="Acme-1.2", state="installed", exit_code=0, log_ref="s3://x"
                )
            ]
        )
        await ingest_deploy_result(session, job, result)

        dep_result = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.job_id == job.id)
            )
        ).scalar_one()
        assert dep_result.state == "installed"
        assert dep_result.exit_code == 0
        assert dep_result.log_ref == "s3://x"
        assert dep_result.finished_at is not None

        deployment = (
            await session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        assert deployment.state == "complete"

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "deploy.result")
            )
        ).scalar_one()
        assert event.actor_type == "system"
        assert event.actor_id is None
        assert event.meta["device_id"] == str(device_id)
        assert event.meta["state"] == "installed"


async def test_failed_outcome_fails_deployment(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, deployment_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = AppDeployResult(
            outcomes=[
                InstallOutcome(identifier="Acme-1.2", state="failed", exit_code=1)
            ]
        )
        await ingest_deploy_result(session, job, result)

        dep_result = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.job_id == job.id)
            )
        ).scalar_one()
        assert dep_result.state == "failed"
        assert dep_result.exit_code == 1

        deployment = (
            await session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        assert deployment.state == "failed"


async def test_not_all_terminal_leaves_deployment_state_unchanged(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A deployment with more than one device result must not roll up until
    every device's result is terminal."""
    org_id, deployment_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        deployment = (
            await session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        other_device = Device(
            org_id=org_id, hostname="h2", os_family="windows", client_id=org_id, site_id=org_id
        )
        session.add(other_device)
        await session.flush()
        session.add(
            DeploymentResult(
                org_id=org_id,
                deployment_id=deployment_id,
                device_id=other_device.id,
                state="running",  # still in flight — no job report yet
            )
        )
        await session.flush()

        result = AppDeployResult(
            outcomes=[InstallOutcome(identifier="Acme-1.2", state="installed", exit_code=0)]
        )
        await ingest_deploy_result(session, job, result)

        deployment = (
            await session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        assert deployment.state == "pending"  # unchanged — one device still running


async def test_not_applicable_outcome_completes_deployment(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A `not_applicable` outcome (device already in desired state, reported
    by the agent rather than short-circuited at dispatch time) must flip the
    result row to `not_applicable` and, as the only device, roll the
    deployment to `complete`."""
    org_id, deployment_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = AppDeployResult(
            outcomes=[InstallOutcome(identifier="Acme-1.2", state="not_applicable")]
        )
        await ingest_deploy_result(session, job, result)

        dep_result = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.job_id == job.id)
            )
        ).scalar_one()
        assert dep_result.state == "not_applicable"

        deployment = (
            await session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        assert deployment.state == "complete"


async def test_out_of_domain_state_coerced_to_failed_and_rolls_up(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`InstallOutcome.state` also allows "succeeded" (script.run's
    vocabulary), which is outside the deploy-valid domain
    (`installed|failed|not_applicable`). Writing it verbatim would leave the
    result row permanently non-terminal and wedge the rollup forever. It must
    instead be coerced to `failed` — a terminal state — with the original
    value recorded in the audit metadata so the operator can see why."""
    org_id, deployment_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = AppDeployResult(
            outcomes=[InstallOutcome(identifier="Acme-1.2", state="succeeded", exit_code=0)]
        )
        await ingest_deploy_result(session, job, result)

        dep_result = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.job_id == job.id)
            )
        ).scalar_one()
        assert dep_result.state == "failed"  # coerced, not "succeeded" verbatim

        deployment = (
            await session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        assert deployment.state == "failed"  # rolled up to terminal — no wedge

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "deploy.result")
            )
        ).scalar_one()
        assert event.meta["unexpected_outcome_state"] == "succeeded"


async def test_empty_outcomes_marks_failed_without_raising(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """An empty `outcomes` list is schema-legal but protocol-violating.
    `outcome = result.outcomes[0]` would raise IndexError; the ingestion must
    instead land the result row on a terminal `failed` state with a clear
    note, and still roll up the deployment — no exception, no wedge."""
    org_id, deployment_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = AppDeployResult(outcomes=[])
        await ingest_deploy_result(session, job, result)  # must not raise

        dep_result = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.job_id == job.id)
            )
        ).scalar_one()
        assert dep_result.state == "failed"
        assert dep_result.log_ref == "agent returned no outcomes"

        deployment = (
            await session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        assert deployment.state == "failed"

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "deploy.result")
            )
        ).scalar_one()
        assert event.meta["note"] == "agent returned no outcomes"


async def test_no_matching_result_row_is_a_graceful_no_op(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """An agent report for a job with no dispatch-created DeploymentResult
    row must never raise — the agent's report should never be able to fail
    the job over server-side bookkeeping."""
    org_id, _deployment_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        # Detach the row from this job so there's nothing to match.
        dep_result = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.job_id == job.id)
            )
        ).scalar_one()
        dep_result.job_id = None
        await session.flush()

        result = AppDeployResult(
            outcomes=[InstallOutcome(identifier="Acme-1.2", state="installed", exit_code=0)]
        )
        await ingest_deploy_result(session, job, result)  # must not raise

        events = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "deploy.result")
            )
        ).scalars().all()
        assert events == []


async def test_another_orgs_job_row_is_invisible_to_the_result_session_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A job/result row belonging to org A must not be reachable/updatable
    through org B's session, even when handed org A's Job object directly."""
    org_a, _deployment_a, _device_a, job_a = await _setup_job(admin_engine)
    org_b, _deployment_b, _device_b, _job_b = await _setup_job(admin_engine)

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
        result = AppDeployResult(
            outcomes=[InstallOutcome(identifier="Acme-1.2", state="installed", exit_code=0)]
        )
        # job_a.org_id is org A's id; the RLS-scoped session belongs to org B.
        await ingest_deploy_result(session, job_a, result)

        rows = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.job_id == job_a.id)
            )
        ).scalars().all()
        assert rows == []  # org B's RLS-scoped session cannot see org A's row

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        dep_result = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.job_id == job_a.id)
            )
        ).scalar_one()
        assert dep_result.state == "running"  # untouched by the org B attempt


# ---------------------------------------------------------------------------
# kernel round-trip (Finding 2, whole-branch review): every other test in
# this file calls ingest_deploy_result directly, which never exercises
# app.modules.deploy.handler's AppDeployResult(**result) parse or its
# queue_ui_event call. This test drives the real job_engine.complete() path
# instead, mirroring test_inventory_ingest.py's
# test_valid_result_through_job_engine_ingests.
# ---------------------------------------------------------------------------


async def test_valid_result_through_job_engine_reconciles_and_queues_ui_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, deployment_id, device_id, job = await _setup_job(admin_engine)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
        await engine.mark_dispatched(session, job)
        await engine.complete(
            session,
            job,
            state="succeeded",
            result={
                "outcomes": [
                    {"identifier": "Acme-1.2", "state": "installed", "exit_code": 0}
                ]
            },
        )
        assert job.status is JobStatus.succeeded

        dep_result = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.job_id == job.id)
            )
        ).scalar_one()
        assert dep_result.state == "installed"
        assert dep_result.exit_code == 0

        deployment = (
            await session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        assert deployment.state == "complete"

        # the handler queued a post-commit UI event on the session
        queued = session.info.get("ui_events", [])
        assert [e["kind"] for _, e in queued] == ["deploy_status_updated"]
        assert queued[0][1]["device_id"] == str(device_id)
        assert queued[0][1]["job_id"] == str(job.id)
