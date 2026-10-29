"""The review gate (install-link spec §1.5): the single place every job is born
refuses, for a device waiting for review, every type outside the allow-list."""

import ipaddress
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs import registry
from app.jobs.engine import JobEngine
from app.jobs.registry import HelloParams, JobType
from app.jobs.review_gate import (
    ALLOWED_WHILE_PENDING,
    ALLOWED_WHILE_REJECTED,
    DeviceAwaitingReview,
    refused,
)
from app.models import Device, Job
from tests.ipam_fixtures import seed_agent, seed_device, seed_org


def _live_registry() -> set[str]:
    from app.modules.remote.jobtypes import register_job_type as register_remote_job_type
    from app.worker import register_worker_job_types

    register_worker_job_types()
    register_remote_job_type()
    return set(registry.registered_names())


@pytest.fixture()
def engine(tmp_path: Path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "sign.key"))


async def _device(admin_engine: AsyncEngine, review_state: str) -> tuple[UUID, UUID]:
    org = await seed_org(admin_engine, "gate")
    device_id = await seed_device(admin_engine, org.org_id, review_state=review_state)
    return org.org_id, device_id


def test_the_allow_lists_are_exactly_the_spec() -> None:
    assert ALLOWED_WHILE_PENDING == {
        "inventory.collect",
        "health.policy.apply",
        "health.check.now",
        "agent.cert.renew",
        "agent.cert.install",
        "agent.uninstall",
        # Review fix M5: cancelling only reduces what runs.
        "job.cancel",
    }
    assert ALLOWED_WHILE_REJECTED == {"agent.uninstall"}


def test_an_approved_device_or_an_unknown_one_is_never_refused() -> None:
    for job_type in _live_registry():
        assert refused("approved", job_type) is False
        assert refused(None, job_type) is False


def test_an_unrecognised_state_is_held_back_like_rejected() -> None:
    assert refused("quarantined", "inventory.collect") is True
    assert refused("quarantined", "agent.uninstall") is False


async def test_every_registered_type_outside_the_list_is_refused_before_signing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine: JobEngine
) -> None:
    """ENUMERATES THE LIVE REGISTRY, so a job type registered tomorrow is
    refused for a pending device without anybody remembering to add it here.
    `params={}` is deliberate: the gate runs BEFORE params validation, so a
    type whose params are required still raises the gate's error."""
    org_id, device_id = await _device(admin_engine, "pending")
    refused_types = sorted(_live_registry() - ALLOWED_WHILE_PENDING)
    assert len(refused_types) >= 15, refused_types
    for job_type in refused_types:
        with pytest.raises(DeviceAwaitingReview) as info:
            async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
                await engine.create(
                    session, org_id=org_id, type=job_type, target_device_id=device_id, params={}
                )
        assert info.value.device_id == device_id
        assert info.value.job_type == job_type
        assert str(device_id) in str(info.value) and job_type in str(info.value)
    async with AsyncSession(admin_engine) as session:
        written = (
            (await session.execute(select(Job).where(Job.target_id == device_id))).scalars().all()
        )
    assert written == []


async def test_a_type_registered_later_is_refused_by_default(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine: JobEngine
) -> None:
    registry.register(JobType("test.review.brand_new", HelloParams))
    try:
        org_id, device_id = await _device(admin_engine, "pending")
        with pytest.raises(DeviceAwaitingReview):
            async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
                await engine.create(
                    session,
                    org_id=org_id,
                    type="test.review.brand_new",
                    target_device_id=device_id,
                    params={},
                )
    finally:
        registry._registry.pop("test.review.brand_new", None)


async def test_an_allowed_type_is_created_and_signed_for_a_pending_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine: JobEngine
) -> None:
    org_id, device_id = await _device(admin_engine, "pending")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await engine.create(
            session, org_id=org_id, type="inventory.collect", target_device_id=device_id, params={}
        )
    assert job.signature


async def test_approving_unblocks_and_rejecting_leaves_only_the_uninstall(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine: JobEngine
) -> None:
    org_id, device_id = await _device(admin_engine, "pending")
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Device).where(Device.id == device_id).values(review_state="approved")
        )
        await session.commit()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await engine.create(
            session, org_id=org_id, type="hello", target_device_id=device_id, params={}
        )
    assert job.signature

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Device).where(Device.id == device_id).values(review_state="rejected")
        )
        await session.commit()
    with pytest.raises(DeviceAwaitingReview):
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            await engine.create(
                session,
                org_id=org_id,
                type="inventory.collect",
                target_device_id=device_id,
                params={},
            )


async def test_the_api_answers_409_naming_the_device_and_the_type(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "gate")
    device_id = await seed_device(api_env.admin_engine, org.org_id, review_state="pending")
    await seed_agent(api_env.admin_engine, org.org_id, device_id)
    headers = await api_env.headers(org.org_id, await api_env.create_user(org.org_id, "org-admin"))
    response = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "hello", "device_id": str(device_id), "params": {}},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": "waiting for review",
        "device_id": str(device_id),
        "job_type": "hello",
    }


@pytest.mark.parametrize("review_state", ["pending", "rejected"])
async def test_promoting_an_unapproved_device_to_site_server_is_refused(
    api_env, review_state: str
) -> None:
    org = await seed_org(api_env.admin_engine, "gate")
    device_id = await seed_device(api_env.admin_engine, org.org_id, review_state=review_state)
    await seed_agent(api_env.admin_engine, org.org_id, device_id)
    headers = await api_env.headers(org.org_id, await api_env.create_user(org.org_id, "org-admin"))
    response = await api_env.client.post(
        "/api/v1/site-servers",
        json={"device_id": str(device_id), "segments": ["10.20.0.0/16"], "roles": ["wake"]},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"].startswith("waiting for review")


async def test_a_pending_row_is_not_a_vouched_push_target(admin_engine: AsyncEngine) -> None:
    from app.modules.agent_push.preflight import _asset_for

    org = await seed_org(admin_engine, "gate")
    device_id = await seed_device(
        admin_engine, org.org_id, review_state="pending", primary_ip="10.9.9.9"
    )
    address = ipaddress.ip_address("10.9.9.9")
    # The device is the internal client's, whose id is the organisation's:
    # a push is asked for one client's machine (Task 12).
    client_id = org.org_id
    async with AsyncSession(admin_engine) as session:
        assert await _asset_for(session, org.org_id, address, client_id) is None
        await session.execute(
            update(Device).where(Device.id == device_id).values(review_state="approved")
        )
        await session.flush()
        assert await _asset_for(session, org.org_id, address, client_id) == device_id


async def test_a_running_job_on_a_pending_device_can_still_be_cancelled(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, engine: JobEngine
) -> None:
    """`job.cancel` is itself a job born in `JobEngine.create`; refusing it
    would leave an allowed job on a pending machine impossible to stop."""
    org_id, device_id = await _device(admin_engine, "pending")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        running = await engine.create(
            session, org_id=org_id, type="inventory.collect", target_device_id=device_id, params={}
        )
        cancel = await engine.create(
            session,
            org_id=org_id,
            type="job.cancel",
            target_device_id=device_id,
            params={"target_job_id": str(running.id)},
        )
    assert cancel.signature
