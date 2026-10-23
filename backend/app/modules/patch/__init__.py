"""Patch management module registration (docs/modules/patch/spec.md).

register(app) is called by app.modules.register_all(): it registers the
`patch.scan` / `patch.apply` job types and mounts the module's API routes.
Unlike `remote`, this module has no stateful service to hold on app.state —
scan/apply handlers dispatch directly via `app.state.job_engine` and
`app.state.redis`, which the kernel already constructs. The gateway process
registers the job types too (see app.gateway.main) so signed jobs validate
where they are created.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.patch.handler import register_job_types

    register_job_types()

    from app.modules.patch.api import router

    app.include_router(router, prefix="/api/v1")
