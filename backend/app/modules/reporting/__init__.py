"""Reporting & Analytics -- the cross-module read layer
(`docs/modules/reporting/spec.md`).

A fixed catalogue of code-defined, bounded, typed, self-describing cross-module
reports, authorized by the CONJUNCTION of every source module's read permission,
so that an operator can never see through a report what the module APIs would
refuse them directly (§0.4).

**THIS MODULE OWNS NO FACTS.** No table, no model, no entry in
`app/models/__init__.py`, no migration DDL, no Celery task, no beat entry, no
job type, no event type, no agent capability, no wire-protocol change. It stores
no measurement and computes no value a module could not compute about itself. It
owns *how a cross-module question is asked, who is allowed to ask it, and what
the answer is allowed to contain* -- and nothing else. If a change here starts
adding any of the above, re-read §0.2/§0.3/§0.11: that is the thing the spec
refused.

Consequently `register(app)` mounts the module's router under `/api/v1` and does
NOTHING ELSE. There is no job type to register and no result handler to bind:
the agent in `agent/` is untouched, and the agent idle budget is untouched by this
module entirely.

Importing this package must stay cheap: the router import is function-local,
exactly as every sibling module does it. It matters more here than elsewhere --
importing `app.modules.reporting.api` builds every report route AND runs
`verify_registry()` over every definition, so a module-level import would put
that work in every process that merely wanted the error types.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.reporting.api import router

    app.include_router(router, prefix="/api/v1")
