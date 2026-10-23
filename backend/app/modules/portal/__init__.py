"""End-user portal (`docs/modules/portal/spec.md`).

Control-plane only: no agent capability, no new job type, no signed-job path.
This module adds no business logic — it is a second, tightly-scoped surface
onto the ticket core's existing services (`app.modules.service_desk`).

`register(app)` mounts this module's API routes only, mirroring `service_desk`/
`notification` (control-plane only, no job type registration, no gateway
edit): no worker capability exists for this module to register.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.portal.api import router

    app.include_router(router, prefix="/api/v1")
