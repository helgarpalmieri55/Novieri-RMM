"""Signed agent self-update (docs/modules/agent-distribution/spec-self-update.md).

Release channels, org rollouts in human-advanced rings, and the reconciler
that dispatches `agent.update` in batches and pauses on failures. The job
type is refused by the kernel jobs route, approved actions and the AI layer
(§8.5); this module's routes are the only way to start one.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.agent_update.api import router
    from app.modules.agent_update.jobtypes import register_job_types

    register_job_types()
    app.include_router(router, prefix="/api/v1")
