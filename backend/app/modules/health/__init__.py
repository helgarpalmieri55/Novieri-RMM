"""Endpoint health, alerting & auto-remediation
(`docs/modules/endpoint-health-alerting/spec.md`).

One new agent capability (`health.check`, built in
`agent/internal/capabilities/health/`) and one new
agent->server wire message. Remediation adds NO new action code: it dispatches
existing, registered job types through the unmodified signed-job engine.

`register(app)` registers the two job types and mounts the module's router under
`/api/v1` (spec §10). The job types are registered in all three processes that
need them (API, gateway, worker) because job types are validated where a job is
CREATED and a result handler is looked up where a result is COMPLETED, and those
are different processes: the API dispatches `health.check.now` and
`health.policy.apply`, the worker's sweeps dispatch `health.policy.apply`, and
the gateway completes both.

REGISTRATION ORDER: the job types are registered BEFORE the router is imported.
`api.py` reaches `approved_actions._validate_and_derive`, which validates a
caller-supplied `job_type` against `app.jobs.registry.registered_names()` -- a
process where the health types were not registered would refuse a
`health.policy.apply` approved action as unknown.

Importing this package must stay cheap: `app/models/__init__.py` imports
`app.modules.health.models` at process start, so anything that drags FastAPI or
Celery in belongs behind a function-local import, exactly as every sibling
module does.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.health.jobtypes import register_job_types

    register_job_types()

    from app.modules.health.api import router

    app.include_router(router, prefix="/api/v1")
