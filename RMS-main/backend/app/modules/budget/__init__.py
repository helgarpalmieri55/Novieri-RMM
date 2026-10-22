"""Budget: IT budget planning, expense tracking, and renewal-driven forecasting.

docs/modules/budget/spec.md §2 (data model) and §8 (security invariants).

register(app) is called by app.modules.register_all(): it mounts this
module's API routes only. Like bcp/change_mgmt/licenses/subscriptions, Budget
dispatches no agent jobs -- there is no register_job_types() call and no
gateway edit (spec §1: control-plane only, no worker).
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.budget.api import router

    app.include_router(router, prefix="/api/v1")
