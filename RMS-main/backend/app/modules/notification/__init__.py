"""Notification core: channels, routing rules, queue membership, deliveries,
and the webhook egress allowlist.

docs/modules/notification/spec.md. register(app) is called by
app.modules.register_all(): it mounts this module's API routes only. Like
bcp/change_mgmt/licenses/subscriptions/budget/service_desk, this module
dispatches no agent jobs -- there is no register_job_types() call and no
gateway edit (spec §1: control-plane only, no agent capability, no signed-job
path).
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.notification.api import router

    app.include_router(router, prefix="/api/v1")
