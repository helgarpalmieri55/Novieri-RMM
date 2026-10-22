"""inventory.collect result handler — the module's kernel hookup.

register_job_type() replaces the kernel's bare registration with one carrying
the ingestion handler. The optional publisher (bound to the hosting process's
Redis) emits an `inventory_updated` UI event after a successful ingest; the
API app and the gateway each register with their own publisher.
"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.dispatch import queue_ui_event
from app.jobs.registry import InventoryCollectParams, JobType, register
from app.models import Device, Job
from app.modules.inventory.hooks import run_post_ingest_hooks
from app.modules.inventory.ingest import InventoryValidationError, ingest_inventory
from app.modules.inventory.schema import InventoryDocument


def register_job_type() -> None:
    async def handle_result(session: AsyncSession, job: Job, result: dict[str, Any]) -> None:
        try:
            doc = InventoryDocument(**result)
        except ValidationError as exc:
            raise InventoryValidationError(  # i18n-exempt: stored as the job's failure reason
                f"invalid inventory document: {exc}"
            ) from exc
        device = (
            await session.execute(select(Device).where(Device.id == job.target_id))
        ).scalar_one_or_none()
        if device is None:
            raise InventoryValidationError(  # i18n-exempt: stored as the job's failure reason
                f"no such device {job.target_id}"
            )
        stats = await ingest_inventory(session, device, doc)
        # Other modules observe the result here (hooks.py). After the ingest, so
        # a hook reads the normalized current state rather than the document;
        # before the UI event, so that event still reflects a completed ingest.
        # Each hook runs in its own SAVEPOINT and cannot fail this job.
        await run_post_ingest_hooks(session, device, doc)
        # Queued, not published: the kernel publishes after the transaction
        # commits, so UI consumers never refetch ahead of durable data.
        queue_ui_event(
            session,
            job.org_id,
            {
                "kind": "inventory_updated",
                "device_id": str(device.id),
                "job_id": str(job.id),
                "changes": stats.changes,
            },
        )

    register(JobType("inventory.collect", InventoryCollectParams, result_handler=handle_result))
