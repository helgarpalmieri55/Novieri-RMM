"""JobType.result_handler — the kernel's module extension point."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.jobs.registry import JobType, get_job_type, register
from app.models import Device, Job, JobStatus, Organization


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _org_device(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"rh-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(org_id=org.id, hostname="rh1", os_family="linux")
        session.add(device)
        await session.commit()
        return org.id, device.id


@pytest.fixture()
def job_engine(tmp_path: Path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))


async def test_handler_invoked_on_success(admin_engine, app_engine, job_engine) -> None:
    calls: list[tuple[Job, dict[str, Any]]] = []

    async def handler(session: AsyncSession, job: Job, result: dict[str, Any]) -> None:
        calls.append((job, result))

    register(JobType("test.handled", _NoParams, result_handler=handler))
    org_id, device_id = await _org_device(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await job_engine.create(
            session, org_id=org_id, type="test.handled", target_device_id=device_id, params={}
        )
        await job_engine.mark_dispatched(session, job)
        await job_engine.complete(session, job, state="succeeded", result={"x": 1})
    assert len(calls) == 1
    assert calls[0][0].id == job.id
    assert calls[0][1] == {"x": 1}
    assert job.status is JobStatus.succeeded


async def test_handler_not_invoked_on_failure(admin_engine, app_engine, job_engine) -> None:
    calls: list[dict[str, Any]] = []

    async def handler(session: AsyncSession, job: Job, result: dict[str, Any]) -> None:
        calls.append(result)

    register(JobType("test.handled2", _NoParams, result_handler=handler))
    org_id, device_id = await _org_device(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await job_engine.create(
            session, org_id=org_id, type="test.handled2", target_device_id=device_id, params={}
        )
        await job_engine.mark_dispatched(session, job)
        await job_engine.complete(session, job, state="failed", result={"boom": True})
    assert calls == []


async def test_raising_handler_fails_job_without_escaping(
    admin_engine, app_engine, job_engine
) -> None:
    async def handler(session: AsyncSession, job: Job, result: dict[str, Any]) -> None:
        raise ValueError("document rejected")

    register(JobType("test.raises", _NoParams, result_handler=handler))
    org_id, device_id = await _org_device(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = await job_engine.create(
            session, org_id=org_id, type="test.raises", target_device_id=device_id, params={}
        )
        await job_engine.mark_dispatched(session, job)
        await job_engine.complete(session, job, state="succeeded", result={"x": 1})
    assert job.status is JobStatus.failed
    assert job.result is not None
    assert "document rejected" in job.result["error"]


def test_bare_registration_keeps_none_handler() -> None:
    assert get_job_type("hello").result_handler is None
