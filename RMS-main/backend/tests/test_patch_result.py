"""Apply-result ingestion + `patch.scan`/`patch.apply` job-type registration
(docs/modules/patch/spec.md). Mirrors `tests/test_result_handler.py`'s
handler-invocation shape and `tests/test_patch_apply.py`'s org/device/patch
fixture setup.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.jobs.registry import get_job_type
from app.models import (
    AuditEvent,
    Device,
    DeviceGroup,
    DevicePatchStatus,
    Job,
    Organization,
    Patch,
    PatchRun,
)
from app.modules.patch.apply import ingest_apply_result
from app.modules.patch.handler import register_job_types
from app.modules.patch.schema import PatchApplyResult


async def _setup(
    admin_engine: AsyncEngine, *, status_state: str = "approved"
) -> tuple[UUID, UUID, UUID, UUID]:
    """Returns (org_id, device_id, patch_id, job_id) with a Patch, a
    DevicePatchStatus in `status_state`, and a PatchRun for a fresh Job —
    the state Task 7's dispatch leaves behind before results come back."""
    slug = f"pr-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        group = DeviceGroup(org_id=org.id, name="g")
        session.add(group)
        await session.flush()

        device = Device(
            org_id=org.id, hostname="h1", os_family="windows", device_group_id=group.id
        )
        session.add(device)
        await session.flush()

        patch = Patch(
            org_id=org.id,
            os_family="windows",
            source="windows_update",
            identifier="KB1",
            title="Cum Update",
            severity="critical",
        )
        session.add(patch)
        await session.flush()

        session.add(
            DevicePatchStatus(
                org_id=org.id, device_id=device.id, patch_id=patch.id, state=status_state
            )
        )

        job = Job(
            org_id=org.id,
            type="patch.apply",
            target_type="device",
            target_id=device.id,
            params={},
            nonce="n",
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
        )
        session.add(job)
        await session.flush()

        session.add(PatchRun(org_id=org.id, job_id=job.id, device_id=device.id))

        await session.commit()
        return org.id, device.id, patch.id, job.id


async def test_installed_outcome_marks_status_installed_and_finishes_run(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, patch_id, job_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        result = PatchApplyResult(
            results=[
                {"source": "windows_update", "identifier": "KB1", "state": "installed"},
            ],
            reboot_required=True,
            applied_at=datetime.now(UTC).isoformat(),
        )

        await ingest_apply_result(session, job, result)

        status = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.patch_id == patch_id)
            )
        ).scalar_one()
        assert status.state == "installed"
        assert status.applied_at is not None
        assert status.last_error is None

        run = (
            await session.execute(select(PatchRun).where(PatchRun.job_id == job_id))
        ).scalar_one()
        assert run.reboot_required is True
        assert run.finished_at is not None
        assert run.results["reboot_required"] is True
        assert run.results["results"][0]["state"] == "installed"

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "patch.applied")
            )
        ).scalar_one()
        assert event.target_type == "device"
        assert event.target_id == str(device_id)
        assert event.meta == {
            "installed": 1,
            "failed": 0,
            "reboot_required": True,
            "reboot_scheduled_for": None,
        }


async def test_failed_outcome_marks_status_failed_with_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, patch_id, job_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        result = PatchApplyResult(
            results=[
                {
                    "source": "windows_update",
                    "identifier": "KB1",
                    "state": "failed",
                    "error": "exit code 1603",
                },
            ],
            reboot_required=False,
            applied_at=datetime.now(UTC).isoformat(),
        )

        await ingest_apply_result(session, job, result)

        status = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.patch_id == patch_id)
            )
        ).scalar_one()
        assert status.state == "failed"
        assert status.last_error == "exit code 1603"

        run = (
            await session.execute(select(PatchRun).where(PatchRun.job_id == job_id))
        ).scalar_one()
        assert run.finished_at is not None
        assert run.reboot_required is False

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "patch.applied")
            )
        ).scalar_one()
        assert event.meta == {
            "installed": 0,
            "failed": 1,
            "reboot_required": False,
            "reboot_scheduled_for": None,
        }


async def test_outcome_for_patch_not_in_catalog_is_skipped_gracefully(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """An outcome referencing a (source, identifier) not in this org's catalog
    must not blow up ingestion — it's skipped, but the run still finishes and
    the audit still records the outcome counts."""
    org_id, device_id, patch_id, job_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        result = PatchApplyResult(
            results=[
                {"source": "windows_update", "identifier": "KB-UNKNOWN", "state": "installed"},
            ],
            reboot_required=False,
            applied_at=datetime.now(UTC).isoformat(),
        )

        await ingest_apply_result(session, job, result)

        status = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.patch_id == patch_id)
            )
        ).scalar_one()
        assert status.state == "approved"  # untouched

        run = (
            await session.execute(select(PatchRun).where(PatchRun.job_id == job_id))
        ).scalar_one()
        assert run.finished_at is not None


async def test_outcome_with_no_status_row_is_skipped_gracefully(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """An outcome for a patch that IS in the catalog but has no
    DevicePatchStatus row for this device (e.g. it was never approved or
    dispatched to it) must not blow up ingestion — it's skipped, but the run
    still finishes and no spurious status row is created."""
    org_id, device_id, patch_id, job_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine) as session:
        other_patch = Patch(
            org_id=org_id,
            os_family="windows",
            source="windows_update",
            identifier="KB2",
            title="Another Update",
            severity="important",
        )
        session.add(other_patch)
        await session.flush()

        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        result = PatchApplyResult(
            results=[
                {"source": "windows_update", "identifier": "KB2", "state": "installed"},
            ],
            reboot_required=False,
            applied_at=datetime.now(UTC).isoformat(),
        )

        await ingest_apply_result(session, job, result)

        statuses = (
            await session.execute(
                select(DevicePatchStatus).where(DevicePatchStatus.patch_id == other_patch.id)
            )
        ).scalars().all()
        assert statuses == []  # no spurious status row created

        run = (
            await session.execute(select(PatchRun).where(PatchRun.job_id == job_id))
        ).scalar_one()
        assert run.finished_at is not None


def test_register_job_types_registers_both_with_result_handlers() -> None:
    register_job_types()
    assert get_job_type("patch.scan").result_handler is not None
    assert get_job_type("patch.apply").result_handler is not None
