"""Tests for signed `app.deploy` dispatch (docs/modules/app-deploy/spec.md).
Mirrors `tests/test_patch_apply.py`'s fixture style: a fake job engine stands
in for the kernel job engine so these tests exercise dispatch.py's
target-resolution, detection-skip, and audit logic in isolation, while
`tests/test_job_engine.py` covers the real signing path. `dispatch_deployment`
no longer publishes to Redis (see its docstring) — it returns the pending
`(job_id, device_id, envelope)` triples for the caller to publish after its
own transaction commits, so there's no fake Redis here.
"""

import datetime as _dt
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Deployment,
    DeploymentResult,
    Device,
    DeviceGroup,
    InstalledSoftware,
    Organization,
    Package,
    User,
)
from app.modules.deploy.dispatch import dispatch_deployment

# ---------------------------------------------------------------------------
# dispatch_deployment
# ---------------------------------------------------------------------------


class _FakeArtifactSigner:
    """Stands in for `ObjectStore`. Records what it was asked to sign so a
    test can assert the grant's SCOPE and LIFETIME without a bucket."""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, str, int]] = []

    def presign_package_artifact(self, org_id, package_id, *, ref, expires_in) -> str:
        self.calls.append((org_id, package_id, ref, expires_in))
        return f"https://objects.test/{package_id}?sig=fake&X-Amz-Expires={expires_in}"


class _FakeJobEngine:
    # The real engine's TTL, which dispatch reads to size the artifact grant.
    default_ttl_s = 900

    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, session, **kw):
        from app.models import Job

        job = Job(
            org_id=kw["org_id"],
            type=kw["type"],
            target_type="device",
            target_id=kw["target_device_id"],
            params=kw["params"],
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
            requested_by=kw.get("requested_by"),
        )
        session.add(job)
        await session.flush()
        # Mirror the real JobEngine.create: sign at create time.
        job.signature = "fake-signature"
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job):
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": job.signature}


async def _setup(
    admin_engine: AsyncEngine, *, window_cron: str | None = None
) -> tuple[UUID, UUID, UUID, UUID, UUID, UUID]:
    """Returns (org_id, deployment_id, device_a_id, device_b_id, package_id,
    user_id) with device A already carrying the package's `InstalledSoftware`
    row (so detection skips it) and device B not. `user_id` is a real user in
    this org, usable as `actor_id` without violating `jobs.requested_by`'s FK."""
    slug = f"dep-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        user = User(
            org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com"
        )
        session.add(user)
        await session.flush()

        group = DeviceGroup(org_id=org.id, name="deploy-group", client_id=org.id)
        session.add(group)
        await session.flush()

        device_a = Device(
    org_id=org.id,
    hostname="a1",
    os_family="windows",
    device_group_id=group.id,
    client_id=org.id,
    site_id=org.id,
        )
        device_b = Device(
    org_id=org.id,
    hostname="b1",
    os_family="windows",
    device_group_id=group.id,
    client_id=org.id,
    site_id=org.id,
        )
        session.add_all([device_a, device_b])
        await session.flush()

        package = Package(
            org_id=org.id,
            name="Foo",
            version="1.0",
            os_family="windows",
            installer_type="msi",
            artifact_sha256="a" * 64,
            detection_rule={"type": "package_installed", "name": "Foo", "version": "1.0"},
        )
        session.add(package)
        await session.flush()
        # `{org_id}/package-artifacts/{package_id}` -- the layout
        # `ObjectStore.presign_package_artifact` DERIVES and checks against.
        package.artifact_ref = f"s3://it-suite/{org.id}/package-artifacts/{package.id}"
        session.add(package)
        await session.flush()

        session.add(
            InstalledSoftware(
                org_id=org.id, device_id=device_a.id, name="Foo", version="1.0"
            )
        )
        await session.flush()

        deployment = Deployment(
            org_id=org.id,
            package_id=package.id,
            action="install",
            target_type="group",
            target_id=group.id,
            window_cron=window_cron,
        )
        session.add(deployment)
        await session.flush()

        await session.commit()
        return org.id, deployment.id, device_a.id, device_b.id, package.id, user.id


async def test_group_deploy_skips_device_already_installed_and_dispatches_other(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, deployment_id, device_a, device_b, _package_id, caller_id = await _setup(
        admin_engine
    )
    now = _dt.datetime(2026, 7, 19, 3, 0)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_deployment(
            session,
            job_engine,
            org_id=org_id,
            deployment_id=deployment_id,
            now=now,
            actor_id=caller_id,
            actor_type="user",
            artifact_urls=_FakeArtifactSigner(),
        )
        job_ids = [job_id for job_id, _device_id, _envelope in pending]

        assert len(job_ids) == 1
        assert len(job_engine.created) == 1
        assert job_engine.created[0]["type"] == "app.deploy"
        assert job_engine.created[0]["target_device_id"] == device_b

        # dispatch_deployment no longer publishes — it returns the pending
        # (job_id, device_id, envelope) triple for the caller to publish
        # after its transaction commits.
        assert len(pending) == 1
        assert pending[0][1] == device_b

        result_a = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.device_id == device_a)
            )
        ).scalar_one()
        assert result_a.state == "not_applicable"
        assert result_a.job_id is None

        result_b = (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.device_id == device_b)
            )
        ).scalar_one()
        assert result_b.state == "running"
        assert result_b.job_id == job_ids[0]

        # Assert the dispatched job is signed (invariant #1: no unsigned job path).
        from app.models import Job

        signed_job = (
            await session.execute(select(Job).where(Job.id == job_ids[0]))
        ).scalar_one()
        assert signed_job.signature is not None

        deployment = (
            await session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one()
        assert deployment.state == "dispatching"

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "deploy.dispatched")
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == caller_id
        assert event.meta["dispatched_count"] == 1


async def test_closed_window_dispatches_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, deployment_id, _device_a, _device_b, _package_id, _user_id = await _setup(
        admin_engine, window_cron="0 2 * * *"
    )
    now = _dt.datetime(2026, 7, 19, 10, 0)  # outside the 02:00 window
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_deployment(
            session,
            job_engine,
            org_id=org_id,
            deployment_id=deployment_id,
            now=now,
            artifact_urls=_FakeArtifactSigner(),
        )

        assert pending == []
        assert job_engine.created == []
        results = (await session.execute(select(DeploymentResult))).scalars().all()
        assert results == []


async def test_unknown_org_deployment_dispatches_nothing_and_does_not_touch_other_org(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Tenancy: a deployment id belonging to org A must not be dispatchable
    under org B's session, and dispatching org A's own deployment must never
    touch org B's devices."""
    org_a, deployment_a, device_a, device_b, _pkg_a, _user_a = await _setup(admin_engine)
    org_b, _deployment_b, _device_a_b, _device_b_b, _pkg_b, _user_b = await _setup(admin_engine)
    now = _dt.datetime(2026, 7, 19, 3, 0)

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_deployment(
            session,
            job_engine,
            org_id=org_b,
            deployment_id=deployment_a,  # org A's deployment id, under org B's session
            now=now,
            artifact_urls=_FakeArtifactSigner(),
        )
        assert pending == []
        assert job_engine.created == []

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_deployment(
            session,
            job_engine,
            org_id=org_a,
            deployment_id=deployment_a,
            now=now,
            artifact_urls=_FakeArtifactSigner(),
        )
        assert len(pending) == 1
        assert job_engine.created[0]["target_device_id"] == device_b
        # org B's devices must never appear in org A's dispatch.
        touched_devices = {kw["target_device_id"] for kw in job_engine.created}
        assert touched_devices == {device_b}
        assert device_a not in touched_devices


async def test_a_group_deployment_passes_over_a_device_waiting_for_review(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from sqlalchemy import update

    org_id, deployment_id, _device_a, device_b, _package_id, caller_id = await _setup(admin_engine)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Device).where(Device.id == device_b).values(review_state="pending")
        )
        await session.commit()
    job_engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        pending = await dispatch_deployment(
            session,
            job_engine,
            org_id=org_id,
            deployment_id=deployment_id,
            now=_dt.datetime(2026, 7, 19, 3, 0),
            actor_id=caller_id,
            actor_type="user",
            artifact_urls=_FakeArtifactSigner(),
        )
    assert pending == []
    assert job_engine.created == []
