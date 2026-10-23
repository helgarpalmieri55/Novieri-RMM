"""Agent distribution: the artefact catalogue and the install command.

docs/modules/agent-distribution/spec.md. The module exists because the operator
declined a push install: nothing here connects to an endpoint, and
`tests/test_agent_distribution_opens_no_connection.py` is the guard on that.
"""


from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Mount this module's API routes.

    No `register_job_types()` and no gateway edit: this module dispatches no
    agent jobs. It publishes artefacts and mints an install command; getting
    the agent onto the machine is the operator's step, by design (spec §1).
    """
    from app.modules.agent_distribution.api import router
    from app.modules.agent_distribution.link_page import router as link_router

    app.include_router(router, prefix="/api/v1")
    # The install link's public routes (spec 2026-09-28 §1.2) have NO prefix:
    # the link is `https://<console>/i/<code>`, and Traefik sends `/i/` here.
    app.include_router(link_router)
