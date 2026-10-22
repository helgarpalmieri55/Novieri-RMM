"""Backup engine module registration (docs/modules/backup/spec.md §12).

register(app) is called by app.modules.register_all(): it registers the
`backup.run` / `backup.restore` job types and mounts the module's API
routes. Like `patch`/`deploy`, this module has no stateful service to hold
on app.state — run/restore handlers dispatch directly via
`app.state.job_engine`, `app.state.vault`, and `app.state.redis`, which the
kernel already constructs. The gateway process registers the job types too
(see app.gateway.main) so signed jobs validate where they are created.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.backup.handler import register_job_types

    register_job_types()

    from app.modules.backup.api import router
    from app.modules.backup.offsite_api import router as offsite_router

    app.include_router(router, prefix="/api/v1")
    app.include_router(offsite_router, prefix="/api/v1")
