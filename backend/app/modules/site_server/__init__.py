"""Site servers (`docs/modules/site-server/spec.md`).

A site server IS a `Device` an operator has promoted (§0.1) — not a new job
target, not a new connection identity, not a new certificate profile. That
choice is what makes this a module rather than a kernel fork, and
`tests/test_site_server_kernel_boundary.py` asserts it rather than trusting it.

`register(app)` is called by `app.modules.register_all()` in the API process.
**THE JOB TYPE MUST ALSO BE REGISTERED IN THE GATEWAY PROCESS**
(`app/gateway/main.py::register_module_job_types`), which is where agent results
actually complete and which never imports `register_all`. A handler registered
only here is DEAD IN PRODUCTION while every in-process test passes.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.site_server.api import router
    from app.modules.site_server.handler import register_job_types

    register_job_types()
    app.include_router(router, prefix="/api/v1")
