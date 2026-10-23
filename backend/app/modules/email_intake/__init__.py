"""Email intake (`docs/modules/email-intake/spec.md`).

Control-plane only: no agent capability, no new job type, no signed-job path.
Inbound mail is fetched by the control plane over an outbound IMAP connection
(spec §0.1) and turned into ticket-core writes.

`register(app)` mounts this module's API routes only, mirroring
`service_desk`/`notification`/`portal`: no worker capability exists for this
module to register. The poll sweep is driven by `app.worker`, not from here.

The import is inside the function, as every sibling module does: importing
`api` at package-import time would drag FastAPI, the ticket core and the IMAP
transport into any process that merely touches
`app.modules.email_intake.models`.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.email_intake.api import router

    app.include_router(router, prefix="/api/v1")
