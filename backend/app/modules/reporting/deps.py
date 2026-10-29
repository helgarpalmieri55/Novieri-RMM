"""The source conjunction, as a route dependency (spec §0.4, §0.5, §4.1).

    403 unless  reporting.read ∈ granted        <- the kernel's require_permission
    403 unless  definition.requires ⊆ granted   <- this file

**WHY THIS IS NOT A SECOND `require_permission`.** `app/core/rbac.py::
route_permission` recovers a route's gate by reflection over
`require_permission`'s closure, and that is what makes RBAC completeness
DERIVABLE FROM THE ROUTER (invariant #6) rather than a hand-maintained list. It
returns ONE code, and it returns the FIRST matching dependency it finds while
walking the dependant tree. Two `require_permission` closures on one route would
therefore make its answer order-dependent, and every router-derived RBAC test in
the repo would be asserting something no reader could predict.

So the inner closure below is named `dependency` inside a factory called
`require_report_sources`, giving it the qualname
`require_report_sources.<locals>.dependency` -- DELIBERATELY NOT in
`_PERMISSION_DEPENDENCY_QUALNAMES`. Do not add it there. And the free variable
is named `key`, NOT `code`: `route_permission` reads `code` out of the closure's
cells by name, so a free variable called `code` here would be a second candidate
gate hiding behind a qualname check.

`route_permission` consequently returns `reporting.read` for every report route,
deterministically, and `tests/test_reporting_runner.py` asserts BOTH halves --
the code it returns, and that exactly ONE `require_permission`-shaped dependency
exists on the route. The second assertion is the one that goes red if someone
expresses the conjunction as a second `require_permission`; the first alone
would keep passing, order-dependently, which is precisely what §0.5 exists to
prevent.

**THIS IS ALSO WHAT REPORTING DOES INSTEAD OF FORKING THE GATE.**
`app/modules/remote/api.py` defines a local `require_any_permission` for its two
read routes; both are genuinely gated and both return `None` from
`route_permission`, so they read as UNGATED to every router-derived check. That
is a finding about the remote module and belongs in its cycle. Reporting's rule:
never fork `require_permission`, never hide a gate from the router.

**PER CLIENT, BY RLS (MSP clients plan 2026-10-26 Task 14, T4-C2).** The gate
counts a code held on ANY client (`codes_held_anywhere`), and the report then
runs over the clients where the person holds `reporting.read` AND every source
code (`app.api.deps.narrowed_to_every`): the dependency hands the handler that
narrowed context. Phase 1 counted all-clients grants only, because §0.6's
reason -- "a fleet aggregate computed for someone entitled to part of the fleet
is either wrong (the denominator includes devices they may not see) or a leak
(it tells them how many there are)" -- had no other answer then. It has one
now: the aggregate is computed over exactly the part they are entitled to, by
the database, so the denominator is theirs and nothing else is counted.

**IT RUNS BEFORE THE HANDLER OPENS A DATA SESSION (§4.1).** FastAPI resolves
every dependency -- the route-level `reporting.read` gate first, this one as
the handler's `ctx` -- before the endpoint body, so no report query runs for an
unauthorized caller and a timing difference cannot report on data the caller
may not see.
"""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from app.api.deps import AuthContext, bound_auth_context, narrowed_to_every
from app.core.rbac import codes_held_anywhere
from app.core.tenancy import org_session
from app.modules.reporting.registry import REGISTRY


def require_report_sources(key: str) -> Callable[..., Coroutine[Any, Any, AuthContext]]:
    # `Annotated[..., Depends(...)]` rather than a `Depends()` DEFAULT: the
    # default form is the idiomatic FastAPI pattern and every `api.py` in this
    # repo carries a per-file `B008` ignore for it. This file is not an `api.py`
    # and does not need one -- the annotated form is equivalent to FastAPI and
    # avoids widening the lint config for one closure.
    async def dependency(
        request: Request, ctx: Annotated[AuthContext, Depends(bound_auth_context)]
    ) -> AuthContext:
        definition = REGISTRY[key]
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            granted = await codes_held_anywhere(session, ctx.user_id)
        missing = sorted(definition.requires - granted)
        if missing:
            # The missing codes are NAMED. A report an operator cannot run is a
            # permissions conversation; an opaque denial produces a support
            # ticket instead. This leaks nothing -- `requires` is derived from a
            # definition compiled into the image, and the catalogue route tells
            # the same caller the same thing.
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "report_permission_denied",
                    "report_key": key,
                    "requires": sorted(definition.requires),
                    "missing": missing,
                },
            )
        # The report's own scope: where the person holds all of them.
        return await narrowed_to_every(
            request, ctx, {"reporting.read", *definition.requires}
        )

    return dependency
