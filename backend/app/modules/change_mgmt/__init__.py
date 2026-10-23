"""Change management module registration (docs/modules/change_management/
spec.md §12).

register(app) is called by app.modules.register_all(): it mounts this
module's API routes only. Unlike provisioning/backup/patch/deploy, change
management dispatches no agent jobs -- there is no register_job_types() call
and no gateway edit.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.change_mgmt.api import router

    app.include_router(router, prefix="/api/v1")
