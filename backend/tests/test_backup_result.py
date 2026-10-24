"""Tests for `backup.run` result ingestion (docs/modules/backup/spec.md).
Mirrors `tests/test_deploy_result.py`'s ingest-side assertions: a `BackupRun`
row dispatch created for a device job flips to a terminal state on report, a
successful report also produces a restorable `BackupPoint`, and the
ingestion is graceful (never raises) when there's no matching row for the
job.

Most tests here call `ingest_backup_result` directly to isolate its own
reconciliation logic. `test_valid_result_through_job_engine_reconciles_and_
queues_ui_event` instead drives the real kernel path (`JobEngine.complete`
-> `app.modules.backup.handler.handle_backup_result` ->
`ingest_backup_result`), so the handler's `BackupRunResult(**result)` parse
and `queue_ui_event` call are actually exercised end-to-end.

`object_store` (below) mirrors `tests/test_backup_gc.py`'s fixture: a
`moto`-backed `ObjectStore` so `_resolve_manifest_ref`'s derivation
(`Task 6`) exercises the real key-construction code, not a stub.
`ingest_backup_result` takes a FACTORY, not an instance, so most calls below
wrap the fixture as `lambda: object_store` -- the factory is only invoked on
the success path that actually needs a store.
`test_failed_result_does_not_construct_object_store` pins the other half:
a failed result must reconcile without the factory being called at all.
"""

import datetime as _dt
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.signing import LocalEd25519Signer
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import (
    AuditEvent,
    BackupPoint,
    BackupRepository,
    BackupRun,
    Device,
    Job,
    JobStatus,
    Organization,
)
from app.modules.backup import dedup
from app.modules.backup.dispatch import ingest_backup_result
from app.modules.backup.handler import register_job_types
from app.modules.backup.schema import BackupRunResult

# The job-type registry is populated at app-startup time (app.modules.backup.
# register(app), a later task), not merely on import. Register directly so
# this file's own kernel round-trip test doesn't depend on import order,
# mirroring test_deploy_result.py's module-level call.
register_job_types()

_BUCKET = "it-suite-test"


@pytest.fixture()
def object_store(tmp_path: Path):
    """Mirrors `tests/test_backup_gc.py`'s `object_store` fixture."""
    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket=_BUCKET,
    )
    with mock_aws():
        yield ObjectStore(settings)


async def _setup_job(
    admin_engine: AsyncEngine, *, state: str = "running"
) -> tuple[UUID, UUID, UUID, Job]:
    """Returns (org_id, repository_id, device_id, job) with a dispatched
    `BackupRun(state=state)` row wired to the job."""
    slug = f"bkr-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        device = Device(
            org_id=org.id, hostname="h1", os_family="windows", client_id=org.id, site_id=org.id
        )
        session.add(device)
        await session.flush()

        repo = BackupRepository(
            org_id=org.id,
            name="repo-1",
            storage_ref="s3://bucket/repo-1",
            encryption_key_ref="vault://key-1",
        )
        session.add(repo)
        await session.flush()

        job = Job(
            org_id=org.id,
            type="backup.run",
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
            BackupRun(
                org_id=org.id,
                device_id=device.id,
                repository_id=repo.id,
                job_id=job.id,
                state=state,
                actor_type="system",
                started_at=_dt.datetime.now(_dt.UTC),
            )
        )
        await session.flush()

        await session.commit()
        return org.id, repo.id, device.id, job


async def test_success_result_flips_run_and_creates_backup_point(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store: ObjectStore
) -> None:
    org_id, repo_id, device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRunResult(
            state="success",
            manifest_ref="manifests/x",
            bytes_protected=1000,
            bytes_stored=400,
            chunk_count=5,
            dedup_stats={},
        )
        await ingest_backup_result(session, lambda: object_store, job, result)

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "success"
        assert run.bytes_protected == 1000
        assert run.bytes_stored == 400
        assert run.chunk_count == 5
        assert run.finished_at is not None

        point = (
            await session.execute(
                select(BackupPoint).where(BackupPoint.backup_run_id == run.id)
            )
        ).scalar_one()
        assert point.state == "complete"
        assert point.manifest_ref == f"s3://{_BUCKET}/{org_id}/backup/{repo_id}/manifests/x"
        assert point.size == 1000
        assert point.repository_id == repo_id
        assert point.device_id == device_id

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.result")
            )
        ).scalar_one()
        assert event.actor_type == "system"
        assert event.actor_id is None
        assert event.meta["device_id"] == str(device_id)
        assert event.meta["state"] == "success"


async def test_failed_result_fails_run_with_no_backup_point(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store: ObjectStore
) -> None:
    org_id, _repo_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRunResult(
            state="failed",
            error="disk full",
            bytes_protected=0,
            bytes_stored=0,
            chunk_count=0,
        )
        await ingest_backup_result(session, lambda: object_store, job, result)

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "failed"
        assert run.error == "disk full"
        assert run.finished_at is not None

        points = (
            await session.execute(
                select(BackupPoint).where(BackupPoint.backup_run_id == run.id)
            )
        ).scalars().all()
        assert points == []


async def test_failed_result_does_not_construct_object_store(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A `state="failed"` report needs no object store at all -- constructing
    a real `ObjectStore` reaches the network (`resolve_presign_endpoint`,
    then a synchronous `head_bucket`/`create_bucket`), so a misconfigured or
    unreachable deployment must not be a precondition for reconciling a
    failure to a terminal state. No `object_store` fixture, no `moto`: the
    factory below raises if it is ever called, and this test asserts it
    is not."""
    org_id, _repo_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRunResult(
            state="failed",
            error="disk full",
            bytes_protected=0,
            bytes_stored=0,
            chunk_count=0,
        )

        def _unreachable_store() -> ObjectStore:
            raise AssertionError(
                "a failed backup.run result must not construct an object store"
            )

        await ingest_backup_result(session, _unreachable_store, job, result)

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "failed"
        assert run.finished_at is not None


async def test_out_of_domain_state_coerced_to_failed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store: ObjectStore
) -> None:
    """`BackupRunResult.state` is itself typed `Literal["success", "failed"]`,
    so this exercises defense in depth against a value that bypassed the
    wire model's own validation (mirroring the deploy module's
    out-of-domain-state coercion, the Task-8-deploy lesson): the run must
    still land on a terminal `failed` state — never raise, never wedge the
    run, never create a `BackupPoint` — with the original value recorded in
    the audit metadata."""
    org_id, _repo_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRunResult.model_construct(
            state="running",  # type: ignore[arg-type]  # deliberately out-of-domain
            manifest_ref=None,
            bytes_protected=0,
            bytes_stored=0,
            chunk_count=0,
            dedup_stats={},
            error=None,
        )
        await ingest_backup_result(session, lambda: object_store, job, result)

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "failed"  # coerced, not "running" verbatim
        assert "running" in (run.error or "")

        points = (
            await session.execute(
                select(BackupPoint).where(BackupPoint.backup_run_id == run.id)
            )
        ).scalars().all()
        assert points == []

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.result")
            )
        ).scalar_one()
        assert event.meta["unexpected_result_state"] == "running"


async def test_success_without_manifest_ref_coerced_to_failed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store: ObjectStore
) -> None:
    """`BackupRunResult.manifest_ref` is `str | None`, so a wire-valid
    `{"state": "success", "manifest_ref": null, ...}` passes
    `BackupRunResult(**result)` validation even though `BackupPoint.
    manifest_ref` is a NON-NULL column. Without a guard this would raise an
    unhandled `IntegrityError` at `session.flush()` when building the
    `BackupPoint` below — crashing the result handler. A malformed/
    incomplete agent report must instead land the run on `failed`, never
    crash or wedge the job (the Task-8-deploy coercion lesson applied to
    this success-without-manifest case)."""
    org_id, _repo_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRunResult(
            state="success",
            manifest_ref=None,
            bytes_protected=1000,
            bytes_stored=400,
            chunk_count=5,
            dedup_stats={},
        )
        await ingest_backup_result(session, lambda: object_store, job, result)  # must not raise

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "failed"  # NOT success
        assert run.error is not None and run.error != ""
        assert run.finished_at is not None

        points = (
            await session.execute(
                select(BackupPoint).where(BackupPoint.backup_run_id == run.id)
            )
        ).scalars().all()
        assert points == []  # no BackupPoint despite state="success" on the wire

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.result")
            )
        ).scalar_one()
        assert event.meta["state"] == "failed"
        assert event.meta["unexpected_result_state"] == "success_without_manifest"


async def test_success_derives_the_full_manifest_ref_from_the_suffix(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store: ObjectStore
) -> None:
    """The agent reports a suffix because it does not know the bucket. The
    server owns key construction -- the same derived-never-accepted rule
    `presign_backup_object` is built on."""
    org_id, repo_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRunResult(
            state="success",
            manifest_ref="manifests/deadbeef",
            bytes_protected=1,
            bytes_stored=1,
            chunk_count=1,
            dedup_stats={},
        )
        await ingest_backup_result(session, lambda: object_store, job, result)

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "success"

        point = (
            await session.execute(
                select(BackupPoint).where(BackupPoint.backup_run_id == run.id)
            )
        ).scalar_one()
        expected = f"s3://{_BUCKET}/{org_id}/backup/{repo_id}/manifests/deadbeef"
        assert point.manifest_ref == expected


@pytest.mark.parametrize(
    "suffix",
    [
        "../../another-org/backup/x/manifests/m",
        "/absolute/manifests/m",
        "manifests/../../escape",
        "manifests\\windows",
        "manifests//empty-segment",
        "",
    ],
)
async def test_an_unsafe_suffix_fails_the_run_and_writes_no_point(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store: ObjectStore, suffix: str
) -> None:
    """A suffix is endpoint-supplied. Rejecting it here is what stops an
    endpoint naming another tenant's key."""
    org_id, _repo_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRunResult(
            state="success",
            manifest_ref=suffix,
            bytes_protected=1,
            bytes_stored=1,
            chunk_count=1,
            dedup_stats={},
        )
        await ingest_backup_result(session, lambda: object_store, job, result)

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        points = (
            await session.execute(
                select(BackupPoint).where(BackupPoint.backup_run_id == run.id)
            )
        ).scalars().all()
        assert (run.state, points) == ("failed", [])
        assert run.error

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.result")
            )
        ).scalar_one()
        # "" is falsy, so it takes the OTHER branch (`success_without_
        # manifest`, already covered by its own dedicated test above) rather
        # than reaching `_resolve_manifest_ref` at all. Every other suffix in
        # this parametrization IS truthy and unsafe, so it exercises the
        # branch this test is actually about.
        if suffix:
            assert event.meta["unexpected_result_state"] == "unusable_manifest_ref"
        else:
            assert event.meta["unexpected_result_state"] == "success_without_manifest"


async def test_a_control_character_in_the_suffix_fails_the_run_and_writes_no_point(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store: ObjectStore
) -> None:
    """A control character is legal inside a JSON string, so it reaches
    `_resolve_manifest_ref` unfiltered by wire validation -- unlike `..` or a
    leading `/`, nothing about it looks unsafe until it lands in an S3 key.
    `_safe_object_suffix` already rejects it (`storage.py`'s
    `if any(ch < " " or ch == "\\x7f" ...)` check); this pins that an earlier,
    hand-rolled version of `_resolve_manifest_ref` omitted this check
    entirely, and a suffix like this one would have passed straight through."""
    org_id, _repo_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRunResult(
            state="success",
            manifest_ref="manifests/x\ty",
            bytes_protected=1,
            bytes_stored=1,
            chunk_count=1,
            dedup_stats={},
        )
        await ingest_backup_result(session, lambda: object_store, job, result)

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        points = (
            await session.execute(
                select(BackupPoint).where(BackupPoint.backup_run_id == run.id)
            )
        ).scalars().all()
        assert (run.state, points) == ("failed", [])
        assert run.error

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.result")
            )
        ).scalar_one()
        assert event.meta["unexpected_result_state"] == "unusable_manifest_ref"


async def test_an_absolute_ref_from_an_older_agent_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store: ObjectStore
) -> None:
    """An agent that sends a full s3:// ref is naming a bucket, which is the
    thing the endpoint is not allowed to choose."""
    org_id, _repo_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRunResult(
            state="success",
            manifest_ref="s3://someone-elses-bucket/k",
            bytes_protected=1,
            bytes_stored=1,
            chunk_count=1,
            dedup_stats={},
        )
        await ingest_backup_result(session, lambda: object_store, job, result)

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        points = (
            await session.execute(
                select(BackupPoint).where(BackupPoint.backup_run_id == run.id)
            )
        ).scalars().all()
        assert (run.state, points) == ("failed", [])

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.result")
            )
        ).scalar_one()
        assert event.meta["unexpected_result_state"] == "unusable_manifest_ref"


async def test_no_matching_run_row_is_a_graceful_no_op(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store: ObjectStore
) -> None:
    """An agent report for a job with no dispatch-created `BackupRun` row
    must never raise — the agent's report should never be able to fail the
    job over server-side bookkeeping."""
    org_id, _repo_id, _device_id, job = await _setup_job(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        # Detach the row from this job so there's nothing to match.
        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        run.job_id = None
        await session.flush()

        result = BackupRunResult(
            state="success",
            manifest_ref="s3://x",
            bytes_protected=1,
            bytes_stored=1,
            chunk_count=1,
            dedup_stats={},
        )
        await ingest_backup_result(session, lambda: object_store, job, result)  # must not raise

        events = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.result")
            )
        ).scalars().all()
        assert events == []


async def test_another_orgs_job_row_is_invisible_to_the_result_session_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store: ObjectStore
) -> None:
    """A job/run row belonging to org A must not be reachable/updatable
    through org B's session, even when handed org A's Job object directly."""
    org_a, _repo_a, _device_a, job_a = await _setup_job(admin_engine)
    org_b, _repo_b, _device_b, _job_b = await _setup_job(admin_engine)

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
        result = BackupRunResult(
            state="success",
            manifest_ref="s3://x",
            bytes_protected=1,
            bytes_stored=1,
            chunk_count=1,
            dedup_stats={},
        )
        # job_a.org_id is org A's id; the RLS-scoped session belongs to org B.
        await ingest_backup_result(session, lambda: object_store, job_a, result)

        rows = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job_a.id))
        ).scalars().all()
        assert rows == []  # org B's RLS-scoped session cannot see org A's row

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job_a.id))
        ).scalar_one()
        assert run.state == "running"  # untouched by the org B attempt


# ---------------------------------------------------------------------------
# kernel round-trip: every other test in this file calls ingest_backup_result
# directly, which never exercises app.modules.backup.handler's
# BackupRunResult(**result) parse or its queue_ui_event call. This test
# drives the real job_engine.complete() path instead, mirroring
# test_deploy_result.py's test_valid_result_through_job_engine_reconciles_
# and_queues_ui_event.
# ---------------------------------------------------------------------------


async def test_valid_result_through_job_engine_reconciles_and_queues_ui_event(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `handle_backup_result` resolves the object store via
    # `dedup.gateway_object_store()` (module-level singleton, real MinIO in
    # production) -- patched exactly as `test_backup_dedup.py` already does
    # for the chunk-query path, so this stays on the moto-backed store.
    monkeypatch.setattr(dedup, "gateway_object_store", lambda: object_store)

    org_id, repo_id, device_id, job = await _setup_job(admin_engine)
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
                "manifest_ref": "manifests/x",
                "bytes_protected": 1000,
                "bytes_stored": 400,
                "chunk_count": 5,
                "dedup_stats": {},
            },
        )
        assert job.status is JobStatus.succeeded

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "success"

        point = (
            await session.execute(
                select(BackupPoint).where(BackupPoint.backup_run_id == run.id)
            )
        ).scalar_one()
        assert point.manifest_ref == f"s3://{_BUCKET}/{org_id}/backup/{repo_id}/manifests/x"

        # the handler queued a post-commit UI event on the session
        queued = session.info.get("ui_events", [])
        assert [e["kind"] for _, e in queued] == ["backup_status_updated"]
        assert queued[0][1]["device_id"] == str(device_id)
        assert queued[0][1]["job_id"] == str(job.id)
