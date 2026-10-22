"""Provisioning module registration (docs/modules/provisioning/spec.md §12).

register(app) is called by app.modules.register_all(): it registers the
`provision.enroll` job type and mounts the module's API routes. Like
`backup`/`patch`/`deploy`, this module has no stateful service to hold on
app.state — the run-start route dispatches directly via
`app.state.job_engine.signer`, `app.state.vault`, and a lazily-built object
store, which the kernel already constructs. The gateway process registers the
job type too (see app.gateway.main) so signed jobs validate where they are
created.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.provisioning.handler import register_job_types

    register_job_types()

    from app.modules.provisioning.api import router

    app.include_router(router, prefix="/api/v1")
