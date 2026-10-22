"""Documentation module registration: API routes only (no job types)."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.documentation.api import router
    from app.modules.documentation.documents_api import router as documents_router

    app.include_router(router, prefix="/api/v1")
    # A SECOND ROUTER IN THE SAME PACKAGE, not a second module: stored
    # documents (spec-attachments.md §10) share the `/docs` prefix, the two
    # `docs.*` permission codes and the `/docs` console screen with the
    # articles and records above. Every one of its paths sits under the
    # literal `/docs/documents`, so it shadows nothing already registered and
    # nothing already registered shadows it -- checked in
    # `tests/test_doc_documents_api.py`, not assumed, because
    # `service_desk/api.py` had to fix a real shadowing bug of this kind.
    app.include_router(documents_router, prefix="/api/v1")
