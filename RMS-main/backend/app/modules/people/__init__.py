"""People: one identity per person, starter and leaver checklists.

See `docs/modules/people/spec.md`. Control-plane only: routes, plus a hook
into inventory's post-ingest extension point. The hook must also be
registered in the gateway process, where inventory results complete.
"""

from fastapi import FastAPI


def register_inventory_hook() -> None:
    """Idempotent: the hook list dedupes by identity."""
    from app.modules.inventory.hooks import register_post_ingest_hook
    from app.modules.people.hooks import inventory_post_ingest

    register_post_ingest_hook(inventory_post_ingest)


def register(app: FastAPI) -> None:
    from app.modules.people.api import router

    register_inventory_hook()
    app.include_router(router, prefix="/api/v1")
