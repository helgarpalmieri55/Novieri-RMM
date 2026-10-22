"""patch.scan / patch.apply result handlers — the module's kernel hookup.

Mirrors `app/modules/inventory/handler.py`: register_job_types() replaces the
kernel's bare registrations with ones carrying the ingestion handlers. Both
handlers queue a `patch_status_updated` UI event (drained and published by the
kernel only after the transaction commits, per `queue_ui_event`'s contract) so
the operator UI can refetch a device's patch state after either a scan or an
apply completes.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.dispatch import queue_ui_event
from app.jobs.registry import JobType, register
from app.models import Device, Job
from app.modules.patch.apply import ingest_apply_result
from app.modules.patch.ingest import PatchValidationError, ingest_scan
from app.modules.patch.reboot import ingest_reboot_result
from app.modules.patch.schema import (
    PatchApplyParams,
    PatchApplyResult,
    PatchRebootParams,
    PatchRebootResult,
    PatchScanDocument,
    PatchScanParams,
)


def register_job_types() -> None:
    async def handle_scan_result(
        session: AsyncSession, job: Job, result: dict[str, Any]
    ) -> None:
        try:
            doc = PatchScanDocument(**result)
        except ValidationError as exc:
            raise PatchValidationError(  # i18n-exempt: stored as the job's failure reason
                f"invalid patch scan document: {exc}"
            ) from exc
        device = (
            await session.execute(
                select(Device).where(
                    Device.id == job.target_id, Device.org_id == job.org_id
                )
            )
        ).scalar_one_or_none()
        if device is None:
            raise PatchValidationError(  # i18n-exempt: stored as the job's failure reason
                f"no such device {job.target_id}"
            )
        await ingest_scan(session, device, doc)
        queue_ui_event(
            session,
            job.org_id,
            {
                "kind": "patch_status_updated",
                "device_id": str(device.id),
                "job_id": str(job.id),
            },
        )

    async def handle_apply_result(
        session: AsyncSession, job: Job, result: dict[str, Any]
    ) -> None:
        try:
            res = PatchApplyResult(**result)
        except ValidationError as exc:
            raise PatchValidationError(  # i18n-exempt: stored as the job's failure reason
                f"invalid patch apply result: {exc}"
            ) from exc
        await ingest_apply_result(session, job, res)
        queue_ui_event(
            session,
            job.org_id,
            {
                "kind": "patch_status_updated",
                "device_id": str(job.target_id),
                "job_id": str(job.id),
            },
        )

    async def handle_reboot_result(
        session: AsyncSession, job: Job, result: dict[str, Any]
    ) -> None:
        try:
            res = PatchRebootResult(**result)
        except ValidationError as exc:
            raise PatchValidationError(  # i18n-exempt: stored as the job's failure reason
                f"invalid patch reboot result: {exc}"
            ) from exc
        await ingest_reboot_result(session, job, res, datetime.now(UTC))
        queue_ui_event(
            session,
            job.org_id,
            {
                "kind": "patch_status_updated",
                "device_id": str(job.target_id),
                "job_id": str(job.id),
            },
        )

    register(JobType("patch.scan", PatchScanParams, result_handler=handle_scan_result))
    register(JobType("patch.apply", PatchApplyParams, result_handler=handle_apply_result))
    register(JobType("patch.reboot", PatchRebootParams, result_handler=handle_reboot_result))
