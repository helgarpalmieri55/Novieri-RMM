"""BCP/DR module registration (docs/modules/bcp/spec.md §9).

register(app) is called by app.modules.register_all(): it mounts this
module's API routes only. Like change_mgmt, BCP/DR dispatches no agent jobs
-- there is no register_job_types() call and no gateway edit.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.bcp.api import router

    app.include_router(router, prefix="/api/v1")
