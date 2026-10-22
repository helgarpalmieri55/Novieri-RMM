"""Inventory module registration (docs/modules/inventory/spec.md §3).

register(app) is called by app.modules.register_all(): it swaps the kernel's
bare inventory.collect registration for one carrying the ingestion result
handler, and mounts the module's API routes. UI events are queued on the
session and published post-commit by the kernel. The gateway process registers
the handler too (see app.gateway.main) — results arrive there.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.inventory.handler import register_job_type

    register_job_type()

    from app.modules.inventory.api import router

    app.include_router(router, prefix="/api/v1")
