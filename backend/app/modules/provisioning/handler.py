"""`provision.enroll` result handler -- the module's kernel hookup.

Mirrors `app/modules/backup/handler.py`: `register_job_types()` replaces the
kernel's bare registration with one carrying the ingestion handler, so the
job engine calls it (inside the job's tenant transaction) after a succeeded
`provision.enroll` job result comes back from the agent. It queues a
`provisioning_status_updated` UI event (drained and published by the kernel
only after the transaction commits, per `queue_ui_event`'s contract) so the
operator UI can refetch a device's provisioning state.
"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.dispatch import queue_job_ui_event
from app.jobs.registry import JobType, register
from app.models import Job
from app.modules.provisioning.enroll import ingest_provision_result
from app.modules.provisioning.repository import ProvisioningValidationError
from app.modules.provisioning.schema import ProvisionEnrollParams, ProvisionEnrollResult


def register_job_types() -> None:
    async def handle_provision_result(
        session: AsyncSession, job: Job, result: dict[str, Any]
    ) -> None:
        try:
            res = ProvisionEnrollResult(**result)
        except ValidationError as exc:
            raise ProvisioningValidationError(  # i18n-exempt: stored as the job's failure reason
                f"invalid provision.enroll result: {exc}"
            ) from exc
        await ingest_provision_result(session, job, res)
        await queue_job_ui_event(
            session,
            job,
            {
                "kind": "provisioning_status_updated",
                "device_id": str(job.target_id),
                "job_id": str(job.id),
            },
        )

    register(
        JobType("provision.enroll", ProvisionEnrollParams, result_handler=handle_provision_result)
    )
