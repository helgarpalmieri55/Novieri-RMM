"""Application deployment module registration (docs/modules/app-deploy/spec.md
§12).

register(app) is called by app.modules.register_all(): it registers the
`app.deploy` / `script.run` job types and mounts the module's API routes.
Like `patch`, this module has no stateful service to hold on app.state —
execute/run handlers dispatch directly via `app.state.job_engine` and
`app.state.redis`, which the kernel already constructs. The gateway process
registers the job types too (see app.gateway.main) so signed jobs validate
where they are created.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.deploy.handler import register_job_types

    register_job_types()

    from app.modules.deploy.api import router

    app.include_router(router, prefix="/api/v1")
