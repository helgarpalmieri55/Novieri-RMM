"""Service Desk: ticket core -- queues, categories, tickets, comments,
attachments, approvals, status history.

docs/modules/service-desk/spec.md. register(app) is called by
app.modules.register_all(): it mounts this module's API routes only. Like
bcp/change_mgmt/licenses/subscriptions/budget, Service Desk dispatches no
agent jobs -- there is no register_job_types() call and no gateway edit
(spec §10: control-plane only, no worker).
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.service_desk.api import router

    app.include_router(router, prefix="/api/v1")
