"""Security-logs module registration. Ingestion is driven by the gateway's
security_events message branch (not a job result handler); this hook mounts
the module's API routes."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.security_logs.api import router

    app.include_router(router, prefix="/api/v1")
