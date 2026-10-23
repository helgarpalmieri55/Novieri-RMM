"""The portal's auth seam (spec §0.1, §3).

THIS FILE IS THE EXTENSION POINT. Portal users in v1 are staff already in the
org's OIDC IdP, holding a portal permission (`portal.ticket.read` /
`portal.ticket.write`) instead of -- or in addition to -- a technician one.
External end users -- a managed provider's clients, who are not in that IdP --
are a real future requirement, and adding them means writing a SECOND
implementation of `resolve_portal_principal` here. That is the whole seam: no
pluggable-auth framework is built for a case that does not yet exist, and the
tenancy design (spec §2) is deliberately independent of how a principal
authenticated.

`PortalContext` is intentionally NOT an `AuthContext`. Distinct types mean a
portal principal cannot be handed to a staff dependency by mistake, which a
shared type would make a one-line error.

Gating decision (spec §3, as amended): access is gated on HOLDING A PORTAL
PERMISSION CODE, not on `requester` role membership. The two are not the same
test: `portal.ticket.read`/`portal.ticket.write` are also granted to
`super-admin`/`org-admin` (migration `0028`), because a pre-existing invariant
test (`test_org_admin_has_all_permissions`) requires every admin to hold every
permission code -- there is no way to seed a permission without granting it to
admins too. Gating on the permission rather than on the role is therefore one
authorization concept instead of two, and it is not a hole: an admin using the
portal is still confined by the database to their own `requester_id` row (no
admin carve-out exists in the RLS policy predicate -- see migration `0028` /
`app.core.tenancy.portal_session`), so the worst an admin can do via the portal
is see their own tickets, same as any other requester.
"""

from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status

from app.api.deps import auth_context
from app.core.i18n import tr
from app.core.logging import bind
from app.core.rbac import has_permission, user_permission_codes
from app.core.tenancy import org_session


@dataclass(frozen=True)
class PortalContext:
    org_id: UUID
    requester_user_id: UUID


async def resolve_portal_principal(request: Request) -> PortalContext:
    """Resolve an authenticated portal principal.

    v1 reuses the OIDC path (`auth_context`), then requires the caller hold
    A portal permission -- `portal.ticket.read` OR `portal.ticket.write`
    (spec §3, exactly: "requiring a portal permission (`portal.ticket.read`
    or `portal.ticket.write`)") -- the minimum bar for being a "portal user"
    at all; `require_portal_permission` (below) checks the finer-grained code
    a given route actually needs. M1 fix: an EARLIER version of this check
    hard-coded `portal.ticket.read` only, so a write-only principal (holding
    `portal.ticket.write` but not `portal.ticket.read`) got a 403 here before
    ever reaching a route -- contradicting spec §3's "or". Harmless in
    practice today (the seeded `requester` role holds both), but the code and
    spec must agree. Auto-provisioning of an unknown `sub` is kept
    deliberately: an unrecognised person gets a `User` row with no roles and
    therefore no access, which is a clean 403 rather than a 500. Having a row
    is not access; the permission grant is.

    This is the ONE place in this module that uses `org_session` rather than
    `portal_session`, and it must be: the requester identity IS what this
    function is still establishing, so `portal_session` -- which requires a
    requester id up front to pin the GUC -- cannot work here. Every other read
    or write this module performs, once a `PortalContext` exists, goes through
    `portal_session`.
    """
    ctx = await auth_context(request)
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        codes = await user_permission_codes(session, ctx.user_id)
    if not (codes & {"portal.ticket.read", "portal.ticket.write"}):
        raise HTTPException(status.HTTP_403_FORBIDDEN, tr("not a portal user"))
    return PortalContext(org_id=ctx.org_id, requester_user_id=ctx.user_id)


async def bound_portal_principal(request: Request) -> AsyncIterator[PortalContext]:
    """`resolve_portal_principal`, with org_id/requester bound as CONTEXTVARS
    for the rest of the request -- the dependency portal routes use.

    THE GAP THIS CLOSES. `app.api.deps.bound_auth_context` gave every staff
    route its correlation ids, and a sweep there stops a new staff route from
    losing them again. The portal escaped both nets. `resolve_portal_principal`
    above awaits `auth_context` DIRECTLY rather than through `Depends` -- it
    must, since it is still establishing the identity `portal_session` needs --
    so it never matched the staff sweep's pattern (a `Depends` on the bare
    staff dependency, spelled out in `tests/test_bound_auth_context.py`), and
    nothing in this package called `bind()` at all. Every log line emitted
    while handling any portal route therefore carried no org_id and no
    user_id, including the AI resolver's loopback calls
    (`app.core.ai.executor_portal`), which re-enter these routes carrying the
    principal's own token.

    `user_id` is the REQUESTER's id. The kernel's contextvar is named for the
    authenticated person, and for a portal request that person is the
    requester -- the same id `portal_session` pins into
    `app.current_requester`. Binding it under a different name would give the
    platform two vocabularies for "who made this request" in one log stream.

    An ordinary `with` around the `yield` is correct here for the reason
    `bound_auth_context`'s docstring gives: everything from the yield until
    FastAPI closes this generator runs in the SAME asyncio Task as the
    binding. The request-context middleware's capture cannot use a contextvar
    and does not -- `auth_context`, reached through the resolver above, has
    already stashed `request.state.org_id`/`user_id` for it.
    """
    ctx = await resolve_portal_principal(request)
    with bind(org_id=str(ctx.org_id), user_id=str(ctx.requester_user_id)):
        yield ctx


def require_portal_permission(code: str) -> Callable[..., Coroutine[Any, Any, PortalContext]]:
    # WARNING: mirrors `app.api.deps.require_permission`'s SHAPE deliberately --
    # same inner-function name (`dependency`), same free variable (`code`) --
    # so `app.core.rbac.route_permission` can recover the gated permission
    # code from this closure exactly as it does for staff routes. See the
    # WARNING at `require_permission`'s definition for the full mechanics
    # (`__qualname__` + `__closure__`/`co_freevars` reflection).
    #
    # `route_permission` identifies a gating closure by an exact `__qualname__`
    # match, and this function's inner closure is
    # "require_portal_permission.<locals>.dependency" -- NOT
    # "require_permission.<locals>.dependency". `route_permission` was
    # therefore extended (`app/core/rbac.py`, `_PERMISSION_DEPENDENCY_QUALNAMES`)
    # to recognise both qualnames; that is the one change this module makes to
    # core RBAC, and it is additive (a second recognised shape, not a rewrite
    # of the mechanism) rather than a fork of it.
    async def dependency(
        request: Request, ctx: PortalContext = Depends(bound_portal_principal)
    ) -> PortalContext:
        # `bound_portal_principal`, not `resolve_portal_principal`: the
        # org_id/user_id contextvar binding lives THERE, once, so every portal
        # route gets it by one mechanism. Its binding is already active by the
        # time this body runs and stays active until FastAPI closes ITS
        # generator -- which happens after this dependency finishes, since
        # this one depends on it.
        async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
            allowed = await has_permission(session, ctx.requester_user_id, code)
        if not allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, tr("requires {code}", code=code))
        return ctx

    return dependency
