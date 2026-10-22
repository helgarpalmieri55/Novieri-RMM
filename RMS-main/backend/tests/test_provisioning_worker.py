"""Tests for the provisioning worker sweeps (Task 11):

- ``provision_enroll_dispatch_sweep_all_orgs`` -- per org, find runs stuck in
  `enrolled` (device bound by `bind_provisioned_device` inside the real
  `/enroll/v1` flow) that have never had a `provision.enroll` post-config job
  dispatched (`job_id IS NULL`), and dispatch one via `dispatch_provision_enroll`
  -- mirroring `deploy_window_sweep_all_orgs` / `backup_window_sweep_all_orgs`'s
  publish-after-commit idiom: the pending `(job_id, device_id, envelope)`
  triples are accumulated INSIDE the org's `org_session` block and published
  only after that block exits (i.e. after the org's transaction commits).
- ``provision_timeout_sweep_all_orgs`` -- per org, fail stale
  `awaiting_enrollment` runs past the 24h deadline (secret-bearing
  `answer_ref` deleted per invariant #3) and clean up any lingering
  `answer_ref` still hanging off an already-terminal (`complete`/`failed`)
  run.
- The Phase-4 un-masking test: the worker process must register
  `provision.enroll` or the enroll-dispatch sweep's first `job_engine.create`
  call would raise `UnknownJobType`.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, override_settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.jobs.registry import UnknownJobType, get_job_type
from app.jobs.registry import _registry as _job_registry
from app.models import Device, Job, Organization, ProvisionRun, User
from app.modules.provisioning.handler import register_job_types as register_provisioning_job_types
from app.modules.provisioning.repository import create_image, upsert_profile

REDIS_URL = "redis://localhost:6379/9"

# The provisioning job-type registry is populated at app-startup time, not
# merely on import. Register directly so this file's own fixtures/assertions
# don't depend on import order (mirroring test_backup_worker.py's module-level
# call). The un-masking tests below explicitly pop this first, so this call
# does not mask the bug they prove.
register_provisioning_job_types()


def _settings(pg_cluster, tmp_path: Path) -> Settings:
    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url=REDIS_URL,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-provworker-test",
        vault_root_key_path=tmp_path / "vault-root.key",
    )


async def _make_org_and_user(admin_engine: AsyncEngine, slug_prefix: str) -> tuple[UUID, UUID]:
    slug = f"{slug_prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        session.add(user)
        await session.commit()
        return org.id, user.id


async def _make_device(admin_engine: AsyncEngine, org_id: UUID, hostname: str) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=hostname, os_family="windows")
        session.add(device)
        await session.commit()
        return device.id


async def _make_profile(
    app_engine: AsyncEngine, vault: LocalVaultService, org_id: UUID, actor_id: UUID
) -> UUID:
    async with org_session(org_id, engine=app_engine) as session:
        image = await create_image(
            session,
            org_id=org_id,
            os_family="windows",
            name="worker-test image",
            kind="image",
            params={},
            actor_id=actor_id,
            actor_type="user",
        )
        profile = await upsert_profile(
            session,
            vault,
            org_id=org_id,
            name="worker-test profile",
            image_id=image.id,
            post_config={"hostname": "WK-TEST"},
            target_scope={},
            enabled=True,
            actor_id=actor_id,
            actor_type="user",
        )
        await session.commit()
        return profile.id


async def _make_enrolled_run(
    app_engine: AsyncEngine,
    org_id: UUID,
    profile_id: UUID,
    device_id: UUID,
    actor_id: UUID,
    *,
    job_id: UUID | None = None,
) -> UUID:
    async with org_session(org_id, engine=app_engine) as session:
        run = ProvisionRun(
            org_id=org_id,
            profile_id=profile_id,
            device_id=device_id,
            job_id=job_id,
            state="enrolled",
            actor_type="user",
            requested_by=actor_id,
            started_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        session.add(run)
        await session.commit()
        return run.id


async def _make_awaiting_run(
    app_engine: AsyncEngine,
    org_id: UUID,
    profile_id: UUID,
    actor_id: UUID,
    *,
    started_at: datetime,
    answer_ref: str | None,
) -> UUID:
    async with org_session(org_id, engine=app_engine) as session:
        run = ProvisionRun(
            org_id=org_id,
            profile_id=profile_id,
            state="awaiting_enrollment",
            actor_type="user",
            requested_by=actor_id,
            started_at=started_at,
            answer_ref=answer_ref,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _make_terminal_run(
    app_engine: AsyncEngine,
    org_id: UUID,
    profile_id: UUID,
    actor_id: UUID,
    *,
    state: str,
    answer_ref: str | None,
) -> UUID:
    async with org_session(org_id, engine=app_engine) as session:
        run = ProvisionRun(
            org_id=org_id,
            profile_id=profile_id,
            state=state,
            actor_type="user",
            requested_by=actor_id,
            started_at=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
            finished_at=datetime(2026, 7, 1, 1, 0, tzinfo=UTC),
            answer_ref=answer_ref,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _make_stub_job(app_engine: AsyncEngine, org_id: UUID, device_id: UUID) -> UUID:
    """A bare, already-dispatched `provision.enroll` job row -- stands in for
    a job a previous sweep run created, so a run carrying this id as its
    `job_id` must not be re-dispatched."""
    async with org_session(org_id, engine=app_engine) as session:
        job = Job(
            org_id=org_id,
            type="provision.enroll",
            target_type="device",
            target_id=device_id,
            params={},
            nonce=uuid4().hex,
            issued_at=datetime(2026, 7, 20, tzinfo=UTC),
            expires_at=datetime(2026, 7, 21, tzinfo=UTC),
        )
        session.add(job)
        await session.commit()
        return job.id


# ---------------------------------------------------------------------------
# provision_enroll_dispatch_sweep_all_orgs
# ---------------------------------------------------------------------------


async def test_enroll_dispatch_sweep_dispatches_one_job_per_enrolled_run_and_publishes_post_commit(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    from app import worker

    vault = LocalVaultService(tmp_path / "vault-root.key")

    org_a, actor_a = await _make_org_and_user(admin_engine, "penrd-a")
    org_b, actor_b = await _make_org_and_user(admin_engine, "penrd-b")

    profile_a = await _make_profile(app_engine, vault, org_a, actor_a)
    profile_b = await _make_profile(app_engine, vault, org_b, actor_b)

    device_a = await _make_device(admin_engine, org_a, "wk-a")
    device_b = await _make_device(admin_engine, org_b, "wk-b")

    run_a = await _make_enrolled_run(app_engine, org_a, profile_a, device_a, actor_a)
    run_b = await _make_enrolled_run(app_engine, org_b, profile_b, device_b, actor_b)

    # Already-dispatched enrolled run (job_id set) -- must NOT be re-dispatched.
    already_device = await _make_device(admin_engine, org_a, "wk-a-already")
    already_job_id = await _make_stub_job(app_engine, org_a, already_device)
    already_run = await _make_enrolled_run(
        app_engine, org_a, profile_a, already_device, actor_a, job_id=already_job_id
    )

    # Not-yet-enrolled run (still awaiting_enrollment) -- must NOT be dispatched.
    awaiting_run = await _make_awaiting_run(
        app_engine,
        org_a,
        profile_a,
        actor_a,
        started_at=datetime(2026, 7, 23, 0, 0, tzinfo=UTC),
        answer_ref=None,
    )

    published_device_ids: list[UUID] = []
    run_by_device = {device_a: (org_a, run_a), device_b: (org_b, run_b)}

    async def fake_publish_job(redis, device_id, envelope):
        # The publish must happen AFTER the org's tenant transaction committed
        # -- re-querying in a brand-new session proves the run's job_id is
        # already durable at the moment publish_job is called.
        org_id, run_id = run_by_device[device_id]
        async with org_session(org_id, engine=app_engine) as check_session:
            job_id = (
                await check_session.execute(
                    select(ProvisionRun.job_id).where(ProvisionRun.id == run_id)
                )
            ).scalar_one()
        assert job_id is not None
        published_device_ids.append(device_id)

    # The sweep enumerates every org in the shared session-wide pg_cluster
    # database, including foreign orgs left over by other provisioning test
    # files' own `enrolled`/`job_id IS NULL` runs. A delegating wrapper runs
    # the REAL dispatch_provision_enroll for this test's two orgs and
    # neutralizes every other org, so `total`/`published_device_ids` reflect
    # only our own runs (mirrors test_backup_worker.py's closed-window guard).
    real_dispatch = worker.dispatch_provision_enroll
    known_orgs = {org_a, org_b}

    async def guarded_dispatch(session, job_engine, *, org_id, run_id, now, **kw):
        if org_id not in known_orgs:
            return []
        return await real_dispatch(session, job_engine, org_id=org_id, run_id=run_id, now=now, **kw)

    now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "publish_job", fake_publish_job)
        mp.setattr(worker, "dispatch_provision_enroll", guarded_dispatch)
        with override_settings(_settings(pg_cluster, tmp_path)):
            total = await worker.provision_enroll_dispatch_sweep_all_orgs(now)

    assert total == 2
    assert set(published_device_ids) == {device_a, device_b}

    async with org_session(org_a, engine=app_engine) as session:
        reloaded_a = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.id == run_a))
        ).scalar_one()
        assert reloaded_a.job_id is not None
        job_a = (
            await session.execute(select(Job).where(Job.id == reloaded_a.job_id))
        ).scalar_one()
        assert job_a.type == "provision.enroll"
        assert job_a.target_id == device_a

        # already-dispatched run untouched: same job_id, no second job created
        already = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.id == already_run))
        ).scalar_one()
        assert already.job_id == already_job_id
        jobs_for_already_device = (
            (await session.execute(select(Job).where(Job.target_id == already_device))).scalars()
        ).all()
        assert len(jobs_for_already_device) == 1

        # not-yet-enrolled run left completely alone
        awaiting = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.id == awaiting_run))
        ).scalar_one()
        assert awaiting.job_id is None
        assert awaiting.state == "awaiting_enrollment"

    async with org_session(org_b, engine=app_engine) as session:
        reloaded_b = (
            await session.execute(select(ProvisionRun).where(ProvisionRun.id == run_b))
        ).scalar_one()
        assert reloaded_b.job_id is not None


# ---------------------------------------------------------------------------
# provision_timeout_sweep_all_orgs
# ---------------------------------------------------------------------------


async def test_timeout_sweep_fails_stale_runs_and_cleans_up_terminal_answer_refs(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    from app import worker

    org_id, actor_id = await _make_org_and_user(admin_engine, "ptmo")
    vault = LocalVaultService(tmp_path / "vault-root.key")
    profile_id = await _make_profile(app_engine, vault, org_id, actor_id)

    now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    stale_started = now - timedelta(hours=25)
    fresh_started = now - timedelta(hours=1)

    with mock_aws():
        object_store = ObjectStore(_settings(pg_cluster, tmp_path))

        stale_answer_ref = await object_store.put_provision_object(
            org_id, "answers/stale-run", b"stale-answer-bytes"
        )
        terminal_answer_ref = await object_store.put_provision_object(
            org_id, "answers/terminal-run", b"terminal-answer-bytes"
        )

        stale_run = await _make_awaiting_run(
            app_engine,
            org_id,
            profile_id,
            actor_id,
            started_at=stale_started,
            answer_ref=stale_answer_ref,
        )
        fresh_run = await _make_awaiting_run(
            app_engine,
            org_id,
            profile_id,
            actor_id,
            started_at=fresh_started,
            answer_ref=None,
        )
        terminal_run = await _make_terminal_run(
            app_engine,
            org_id,
            profile_id,
            actor_id,
            state="complete",
            answer_ref=terminal_answer_ref,
        )

        with override_settings(_settings(pg_cluster, tmp_path)):
            failed = await worker.provision_timeout_sweep_all_orgs(now)

        # Only the stale awaiting_enrollment run counts as newly "failed" --
        # the terminal cleanup pass doesn't change any run's state.
        assert failed == 1

        async with org_session(org_id, engine=app_engine) as session:
            stale = (
                await session.execute(select(ProvisionRun).where(ProvisionRun.id == stale_run))
            ).scalar_one()
            assert stale.state == "failed"
            assert stale.error == "enrollment timed out"
            assert stale.answer_ref is None
            assert stale.finished_at == now

            fresh = (
                await session.execute(select(ProvisionRun).where(ProvisionRun.id == fresh_run))
            ).scalar_one()
            assert fresh.state == "awaiting_enrollment"  # untouched: not yet stale
            assert fresh.answer_ref is None

            terminal = (
                await session.execute(select(ProvisionRun).where(ProvisionRun.id == terminal_run))
            ).scalar_one()
            assert terminal.state == "complete"  # not reset -- already terminal
            assert terminal.answer_ref is None  # lingering answer object cleaned up

        with pytest.raises(Exception):  # noqa: B017 - boto3 ClientError (NoSuchKey)
            await object_store.get_provision_object(stale_answer_ref)
        with pytest.raises(Exception):  # noqa: B017 - boto3 ClientError (NoSuchKey)
            await object_store.get_provision_object(terminal_answer_ref)


# ---------------------------------------------------------------------------
# worker bootstrap registers provisioning job types (the Phase-4 un-masking
# test: the Celery worker+beat process never called register_job_types() for
# the provisioning module, so the enroll-dispatch sweep's first
# job_engine.create("provision.enroll", ...) call would raise
# UnknownJobType. This file's own module-level register_provisioning_job_
# types() call above would mask that for every other test here, so this test
# explicitly deletes the registry entry first and drives the worker's own
# bootstrap path.
# ---------------------------------------------------------------------------


def test_worker_bootstrap_registers_provisioning_job_types() -> None:
    from app import worker

    _job_registry.pop("provision.enroll", None)

    # fail-without-fix: without registration provision.enroll cannot resolve
    # -- this is exactly the crash the registration prevents.
    with pytest.raises(UnknownJobType):
        get_job_type("provision.enroll")

    worker.register_worker_job_types()

    assert get_job_type("provision.enroll").name == "provision.enroll"


def test_create_celery_registers_provisioning_job_types(pg_cluster, tmp_path: Path) -> None:
    """create_celery() -- the real bootstrap path when the worker process
    builds its Celery app -- must also leave provision.enroll registered."""
    from app import worker

    _job_registry.pop("provision.enroll", None)

    with pytest.raises(UnknownJobType):
        get_job_type("provision.enroll")

    with override_settings(_settings(pg_cluster, tmp_path)):
        worker.create_celery()

    assert get_job_type("provision.enroll").name == "provision.enroll"
