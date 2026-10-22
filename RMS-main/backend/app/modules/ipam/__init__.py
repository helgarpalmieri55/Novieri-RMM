"""IP Address Management (Phase 15). See `docs/modules/ipam/spec.md`.

`register(app)` is called by `app.modules.register_all()`. Today it registers
IPAM's post-ingest hook into inventory's extension point (§0.17) and the
`net.sweep` job type (§5.1); the router lands with the task that creates it.

**THE HOOK AND THE JOB TYPE MUST ALSO BE REGISTERED IN THE GATEWAY PROCESS**,
which is where agent results actually complete (`app/gateway/dispatch.py` calls
`JobEngine.complete`, and `app/gateway/main.py` registers each module's job types
for itself rather than calling `register_all`) -- **and the `net.sweep` job type
in the WORKER process too**, because `app/worker.py`'s hourly dispatch calls
`JobEngine.create`, which refuses an unregistered type.
`register_inventory_hook` is separated from `register(app)` for exactly that
reason: the gateway has no `FastAPI` app to hand it. Spec §14.2's "kernel: none
required" did not account for either -- registering only in the API app would
leave the passive observation stream dead in production and make the first
scheduled sweep raise `UnknownJobType`, while every test passed.

THE DEPENDENCY ARROW POINTS ipam -> inventory AND NEVER THE REVERSE. IPAM
imports inventory's hook module; inventory imports nothing from IPAM.
"""

from fastapi import FastAPI


def register_inventory_hook() -> None:
    """Register IPAM's observation hook into inventory's post-ingest point.

    Idempotent (`register_post_ingest_hook` dedupes by identity), so calling it
    from both the API app and the gateway -- and repeatedly across a test
    session -- runs the hook once per ingest.
    """
    from app.modules.inventory.hooks import register_post_ingest_hook
    from app.modules.ipam.observations import inventory_post_ingest_hook

    register_post_ingest_hook(inventory_post_ingest_hook)


def register(app: FastAPI) -> None:
    from app.modules.ipam.api import router
    from app.modules.ipam.handler import register_job_types

    register_inventory_hook()
    register_job_types()
    app.include_router(router, prefix="/api/v1")
