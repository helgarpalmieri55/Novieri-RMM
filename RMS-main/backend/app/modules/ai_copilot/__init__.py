"""The technician copilot: a persona over the shared AI layer, inside a ticket.

docs/modules/ai-automation.md §0.3, §3.2, §9.2, §12.

**A NEW PACKAGE, NOT AN ADDITION TO `service_desk`.** `service_desk` is the
ticket core -- the module every other service-desk feature builds on -- and
making it import `app.core.ai` would put an AI dependency underneath SLA,
email intake, the portal and reporting. This package depends on the ticket core;
the ticket core does not know it exists.

**IT DRAFTS; IT NEVER POSTS** (§0.3). It may read the ticket, its thread and the
technician's own estate, and it may PROPOSE an action bounded to the ticket's
linked device. It may not post a comment, set an assignment, change a status or
priority, resolve, or close -- and that is enforced by construction rather than
by care: this package registers two routes, both of which only read or run a
turn, and it imports no ticket write service. `tests/test_ai_copilot.py` asserts
both, over the flattened router and over the AST.

Two reasons the copilot may not post, either sufficient. The SLA engine's
response clock stops at "the first public comment from someone who is not the
requester" (`docs/modules/sla/spec.md` §0.6), and §0.6.1 already had to carve out
machine-generated public notes -- a copilot that can post is a metric-laundering
machine with a natural-language interface. And a posted comment is
customer-visible output attributed to a named human who did not write it.

`register(app)` mounts the router and does nothing else: no job type, no result
handler, no signed-job path, no wire-protocol change, so the agent's idle budget
is untouched.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from app.modules.ai_copilot.api import router

    app.include_router(router, prefix="/api/v1")
