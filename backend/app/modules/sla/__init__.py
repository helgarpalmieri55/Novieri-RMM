"""SLA engine (`docs/modules/sla/spec.md`).

Control-plane only: no agent capability, no new job type, no signed-job path
(spec §1). The module owns *when a promise is due and whether it was kept*,
and nothing else about a ticket's lifecycle.

`register(app)` mounts this module's API routes only, mirroring
`service_desk`/`notification`/`portal`/`email_intake`: there is no worker
capability to register. The breach/auto-close sweep is driven by
`app.worker`'s beat schedule, not from here.

The import is inside the function, as every sibling module does: importing
`api` at package-import time would drag FastAPI and the whole ticket core into
any process that merely touches `app.modules.sla.models` -- which
`app.models` does, at import.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.sla.api import router

    app.include_router(router, prefix="/api/v1")
