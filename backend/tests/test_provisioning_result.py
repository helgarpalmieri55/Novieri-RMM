"""Tests for `provision.enroll` result ingestion
(docs/modules/provisioning/spec.md). Mirrors `tests/test_backup_result.py`'s
ingest-side assertions: a `ProvisionRun` row bound to a device job flips to a
terminal state on report, and the ingestion is graceful (never raises) when
there's no matching row for the job.

Most tests here call `ingest_provision_result` directly to isolate its own
reconciliation logic. `test_valid_result_through_job_engine_reconciles_and_
queues_ui_event` instead drives the real kernel path (`JobEngine.complete` ->
`app.modules.provisioning.handler.handle_provision_result` ->
`ingest_provision_result`), so the handler's `ProvisionEnrollResult(**result)`
parse and `queue_ui_event` call are actually exercised end-to-end.
"""

import datetime as _dt
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import (
    AuditEvent,
    Device,
    Job,
    JobStatus,
    Organization,
    OsImage,
    ProvisionProfile,
    ProvisionRun,
)
from app.modules.provisioning.enroll import ingest_provision_result
from app.modules.provisioning.handler import register_job_types
from app.modules.provisioning.schema import ProvisionEnrollResult

# The job-type registry is populated at app-startup time (a later task), not
# merely on import. Register directly so this file's own kernel round-trip
# test doesn't depend on import order, mirroring test_backup_result.py's
# module-level call.
register_job_types()


async def _setup_job(
    admin_engine: AsyncEngine, *, state: str = "enrolled"
) -> tuple[UUID, UUID, UUID, Job]:
    """Returns (org_id, profile_id, device_id, job) with a `ProvisionRun(
    state=state)` row wired to the job."""
    slug = f"prv-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        device = Device(
            org_id=org.id, hostname="h1", os_family="windows", client_id=org.id, site_id=org.id
        )
        session.add(device)
        await session.flush()

        image = OsImage(
            org_id=org.id,
            os_family="windows",
            name="win-image",
            kind="image",
            params={},
        )
        session.add(image)
        await session.flush()

        profile = ProvisionProfile(
            org_id=org.id,
            name="profile-1",
            image_id=image.id,
            post_config={},
            target_scope={},
        )
        session.add(profile)
        await session.flush()

        job = Job(
            org_id=org.id,
            type="provision.enroll",
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
            ProvisionRun(
                org_id=org.id,
                profile_id=profile.id,
                device_id=device.id,
                job_id=job.id,
                state=state,
                actor_type="system",
                started_at=_dt.datetime.now(_dt.UTC),
            )
        )
        await session.flush()

        await session.commit()
        return org.id, profile.id, device.id, job


async def test_success_result_flips_run_to_complete(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _profile_id, device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = ProvisionEnrollResult(
            state="success",
            hostname_set=True,
            domain_joined=True,
            agent_healthy=True,
        )
        await ingest_provision_result(session, job, result)

        run = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "complete"
        assert run.finished_at is not None

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "provisioning.result")
            )
        ).scalar_one()
        assert event.actor_type == "system"
        assert event.actor_id is None
        assert event.meta["state"] == "complete"
        assert event.meta["provision_run_id"] == str(run.id)
        assert event.meta["hostname_set"] is True
        assert event.meta["domain_joined"] is True
        assert event.meta["agent_healthy"] is True


async def test_failed_result_fails_run_with_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _profile_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = ProvisionEnrollResult(
            state="failed",
            hostname_set=False,
            agent_healthy=False,
            error="domain join failed",
        )
        await ingest_provision_result(session, job, result)

        run = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "failed"
        assert run.error == "domain join failed"
        assert run.finished_at is not None

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "provisioning.result")
            )
        ).scalar_one()
        assert event.meta["state"] == "failed"
        assert "unexpected_result_state" not in event.meta


async def test_out_of_domain_state_coerced_to_failed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`ProvisionEnrollResult.state` is itself typed `Literal["success",
    "failed"]`, so this exercises defense in depth against a value that
    bypassed the wire model's own validation (mirroring the backup module's
    out-of-domain-state coercion): the run must still land on a terminal
    `failed` state -- never raise, never wedge the run -- with the original
    value recorded in the audit metadata."""
    org_id, _profile_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = ProvisionEnrollResult.model_construct(
            state="running",  # type: ignore[arg-type]  # deliberately out-of-domain
            hostname_set=False,
            domain_joined=None,
            agent_healthy=False,
            error=None,
        )
        await ingest_provision_result(session, job, result)  # must not raise

        run = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "failed"  # coerced, not "running" verbatim
        assert "running" in (run.error or "")
        assert run.finished_at is not None

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "provisioning.result")
            )
        ).scalar_one()
        assert event.meta["unexpected_result_state"] == "running"
        assert event.meta["state"] == "failed"


async def test_no_matching_run_row_is_a_graceful_no_op(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """An agent report for a job with no matching `ProvisionRun` row must
    never raise -- the agent's report should never be able to fail the job
    over server-side bookkeeping."""
    org_id, _profile_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        # Detach the row from this job so there's nothing to match.
        run = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.job_id == job.id))
        ).scalar_one()
        run.job_id = None
        await session.flush()

        result = ProvisionEnrollResult(
            state="success",
            hostname_set=True,
            agent_healthy=True,
        )
        await ingest_provision_result(session, job, result)  # must not raise

        events = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "provisioning.result")
            )
        ).scalars().all()
        assert events == []


async def test_provision_enroll_job_type_registered() -> None:
    from app.jobs.registry import _registry

    assert "provision.enroll" in _registry


# ---------------------------------------------------------------------------
# kernel round-trip: every other test in this file calls
# ingest_provision_result directly, which never exercises app.modules.
# provisioning.handler's ProvisionEnrollResult(**result) parse or its
# queue_ui_event call. This test drives the real job_engine.complete() path
# instead, mirroring test_backup_result.py's
# test_valid_result_through_job_engine_reconciles_and_queues_ui_event.
# ---------------------------------------------------------------------------


async def test_valid_result_through_job_engine_reconciles_and_queues_ui_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, _profile_id, device_id, job = await _setup_job(admin_engine)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
        await engine.mark_dispatched(session, job)
        await engine.complete(
            session,
            job,
            state="succeeded",
            result={
                "state": "success",
                "hostname_set": True,
                "domain_joined": True,
                "agent_healthy": True,
            },
        )
        assert job.status is JobStatus.succeeded

        run = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "complete"

        # the handler queued a post-commit UI event on the session
        queued = session.info.get("ui_events", [])
        assert [e["kind"] for _, e in queued] == ["provisioning_status_updated"]
        assert queued[0][1]["device_id"] == str(device_id)
        assert queued[0][1]["job_id"] == str(job.id)
