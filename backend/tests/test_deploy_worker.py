"""Tests for the deploy-management worker sweep (Task 12):

- ``deploy_window_sweep_all_orgs`` -- window-apply: per org, find scheduled
  (``window_cron IS NOT NULL``), not-yet-dispatched (``state == "pending"``)
  deployments and dispatch them via `dispatch_deployment`, which itself
  enforces the maintenance-window gate. Mirrors
  ``tests/test_patch_worker.py``'s ``test_apply_sweep_visits_all_orgs_and_
  sums_dispatch_apply_counts`` (summation-invariant style, since the
  pg_cluster database is shared session-wide across every test file) and its
  worker-bootstrap un-masking tests (Finding 1 from Phase 4's whole-branch
  review: the Celery worker process never registered a module's job types,
  so its beat sweeps raised `UnknownJobType` in production, and a test that
  pre-registered them masked it).
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings, override_settings
from app.core.tenancy import org_session
from app.jobs.registry import UnknownJobType, get_job_type
from app.jobs.registry import _registry as _job_registry
from app.models import Deployment, DeploymentResult, Device, Organization, Package
from app.modules.deploy.handler import register_job_types

REDIS_URL = "redis://localhost:6379/9"

# The job-type registry is populated at app-startup time (app.modules.deploy.
# register(app)), not merely on import. Register directly so this file's own
# fixtures/assertions don't depend on import order, mirroring
# test_patch_worker.py's module-level call.
register_job_types()


async def _make_org(admin_engine: AsyncEngine, slug_prefix: str) -> UUID:
    slug = f"{slug_prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _make_windowed_deployment(
    admin_engine: AsyncEngine, org_id: UUID, *, window_cron: str
) -> UUID:
    """A pending deployment targeting a single device that doesn't already
    have the package installed, so a real (unmocked) dispatch would produce
    exactly one dispatched job when its window is open."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname="d1", os_family="windows")
        session.add(device)
        await session.flush()

        package = Package(
            org_id=org_id,
            name="Foo",
            version="1.0",
            os_family="windows",
            installer_type="msi",
            artifact_ref="s3://artifacts/foo-1.0.msi",
            artifact_sha256="a" * 64,
            detection_rule={"type": "package_installed", "name": "Foo", "version": "1.0"},
        )
        session.add(package)
        await session.flush()

        deployment = Deployment(
            org_id=org_id,
            package_id=package.id,
            action="install",
            target_type="device",
            target_id=device.id,
            window_cron=window_cron,
        )
        session.add(deployment)
        await session.flush()
        await session.commit()
        return deployment.id


# ---------------------------------------------------------------------------
# deploy_window_sweep_all_orgs (window-apply)
# ---------------------------------------------------------------------------


async def test_window_sweep_visits_all_orgs_and_sums_dispatch_deployment_counts(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    """Mirrors test_patch_worker.py's
    test_apply_sweep_visits_all_orgs_and_sums_dispatch_apply_counts: the
    pg_cluster database is shared session-wide, so other test files' orgs
    (including a leftover closed-window pending deployment from
    test_deploy_dispatch.py) are already sitting in it. Monkeypatching
    dispatch_deployment isolates the assertion to the sweep's own SQL
    wiring -- that it finds each org's pending+windowed deployment and calls
    dispatch_deployment for it -- without depending on real window/detection
    evaluation or on other test files' data."""
    from app import worker

    calls: list[tuple[UUID, UUID]] = []

    async def fake_dispatch_deployment(
        session,
        job_engine,
        *,
        org_id,
        deployment_id,
        now,
        actor_id=None,
        actor_type="system",
        artifact_urls=None,
    ):
        calls.append((org_id, deployment_id))
        assert actor_type == "system"
        assert actor_id is None
        # ONE signer for the whole sweep, threaded through every org: minting a
        # fresh object-store client per deployment would open a connection per
        # row on a nightly fan-out.
        assert artifact_urls is not None
        # fixed count (1) per deployment — the pending (job_id, device_id,
        # envelope) triple the sweep now accumulates and publishes post-commit
        return [(uuid4(), uuid4(), {"v": 1, "kind": "job", "job": {}, "sig": "x"})]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "dispatch_deployment", fake_dispatch_deployment)

        org_a = await _make_org(admin_engine, "dws-a")
        org_b = await _make_org(admin_engine, "dws-b")
        dep_a = await _make_windowed_deployment(admin_engine, org_a, window_cron="* * * * *")
        dep_b = await _make_windowed_deployment(admin_engine, org_b, window_cron="* * * * *")

        now = datetime(2026, 7, 19, 15, 30, tzinfo=UTC)
        settings = Settings(
            database_url=pg_cluster.app_url,
            database_admin_url=pg_cluster.admin_url,
            oidc_issuer="https://kc.example/realms/it-suite",
            oidc_jwks_url="https://kc.example/realms/it-suite/certs",
            ca_dir=tmp_path / "ca",
            signing_key_path=tmp_path / "job-signing.key",
            redis_url=REDIS_URL,
        )
        with override_settings(settings):
            total = await worker.deploy_window_sweep_all_orgs(now)

    visited = {org_id: dep_id for org_id, dep_id in calls}
    assert visited.get(org_a) == dep_a
    assert visited.get(org_b) == dep_b
    assert total == sum(1 for _ in calls)


async def test_closed_window_pending_deployment_dispatches_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    """Real (unmocked) dispatch_deployment path: a pending deployment whose
    window is closed at `now` must be visited by the sweep's per-org query
    (it matches state=="pending" AND window_cron IS NOT NULL) but must not
    produce a dispatched job or a DeploymentResult row, since dispatch_
    deployment's own window_open gate rejects it. Assertions are scoped to
    this test's own org so they hold regardless of what else the shared
    pg_cluster database contains."""
    from app import worker

    org_id = await _make_org(admin_engine, "dws-closed")
    # Mirrors test_deploy_dispatch.py's own closed-window fixture: a window
    # open only at 02:00, evaluated against a `now` well outside it.
    await _make_windowed_deployment(admin_engine, org_id, window_cron="0 2 * * *")

    now = datetime(2026, 7, 19, 15, 30, tzinfo=UTC)
    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url=REDIS_URL,
    )
    with override_settings(settings):
        await worker.deploy_window_sweep_all_orgs(now)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        results = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.org_id == org_id)
            )
        ).scalars().all()
        assert results == []

        deployment = (
            await session.execute(select(Deployment).where(Deployment.org_id == org_id))
        ).scalar_one()
        assert deployment.state == "pending"  # untouched: never reached dispatch


# ---------------------------------------------------------------------------
# worker bootstrap registers deploy job types (mirrors test_patch_worker.py's
# un-masking tests for Finding 1: the Celery worker+beat process never called
# register_job_types() for a module, so the first sweep to fire in production
# would raise UnknownJobType. This file's own module-level
# `register_job_types()` call above would mask that bug for every other test
# in this file, so this test explicitly deletes the registry entries first
# and drives the worker's own bootstrap path (`register_worker_job_types`/
# `create_celery`) rather than relying on the import-time call.
# ---------------------------------------------------------------------------


def test_worker_bootstrap_registers_deploy_job_types() -> None:
    from app import worker

    for name in ("app.deploy", "script.run"):
        _job_registry.pop(name, None)

    with pytest.raises(UnknownJobType):
        get_job_type("app.deploy")

    worker.register_worker_job_types()

    assert get_job_type("app.deploy").name == "app.deploy"
    assert get_job_type("script.run").name == "script.run"


def test_create_celery_registers_deploy_job_types(pg_cluster, tmp_path: Path) -> None:
    """create_celery() -- what actually runs when the worker process builds
    its Celery app -- must also leave the registry populated, since that's
    the real bootstrap path in production (not register_worker_job_types()
    called directly)."""
    from app import worker

    for name in ("app.deploy", "script.run"):
        _job_registry.pop(name, None)

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url=REDIS_URL,
    )
    with override_settings(settings):
        worker.create_celery()

    assert get_job_type("app.deploy").name == "app.deploy"
    assert get_job_type("script.run").name == "script.run"
