"""Licenses/SAM: vendor, product, license, allocation, snapshot, ignore.

docs/modules/licenses/spec.md §2 (data model) and §9 (security invariants).

register(app) is called by app.modules.register_all(): it mounts this
module's API routes only. Like bcp/change_mgmt, Licenses/SAM dispatches no
agent jobs -- there is no register_job_types() call and no gateway edit.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.licenses.api import router

    app.include_router(router, prefix="/api/v1")
