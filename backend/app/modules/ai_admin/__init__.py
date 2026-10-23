"""AI admin agent, read/analyze mode (docs/modules/ai-admin/spec.md).

An authenticated administrator asks natural-language questions about their own
org's estate, and the agent answers by calling a bounded, static allowlist of
this platform's own `GET` routes AS THAT ADMINISTRATOR -- with no powers the
admin does not already have, no mutation, and no path to vault material.

**Zero endpoint code.** No agent capability, no job type, no signed-job path, no
wire-protocol change. The agent in `agent/` is not touched by this module at all.

`register(app)` mounts the module's router under `/api/v1` (spec §6.1) and does
NOTHING ELSE. There is no job type to register and no result handler to bind:
this module adds no agent capability, no signed-job path and no wire-protocol
change, so the agent's idle budget is untouched by it entirely.

Importing this package must stay cheap, and `register(app)` is the only thing
that pulls the router in: the router import is function-local, exactly as every
sibling module does it. The four declarative models this package used to own now
live in `app/models/ai.py` (docs/modules/ai-automation.md §0.1, §2.1), so
`app/models/__init__.py` no longer imports anything from here at all -- a kernel
module importing a business module is what the relocation exists to end.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.ai_admin.api import router

    app.include_router(router, prefix="/api/v1")
