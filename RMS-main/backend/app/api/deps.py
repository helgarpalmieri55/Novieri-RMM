"""Request dependencies: authentication context and RBAC gates.

Every privileged route depends on require_permission(code), which resolves the
operator from the bearer token, pins the tenant, and evaluates RBAC inside the
tenant session — server-side, always.
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr, tr_marked
from app.core.identity import domain_refusal, log_admission_refusal, status_refusal
from app.core.identity.policy import resolve_console_domain
from app.core.logging import bind
from app.core.mcp_channel import (
    MCP_FORBIDDEN_CODES,
    McpCall,
    mcp_call_from_scope,
    mcp_resource,
)
from app.core.rbac import has_permission
from app.core.security import (
    JwksCache,
    TokenClaims,
    TokenValidationError,
    require_console_token,
    require_person_mcp_token,
    validate_token,
)
from app.core.tenancy import org_session
from app.models import Organization, User


@dataclass(frozen=True)
class AuthContext:
    org_id: UUID
    user_id: UUID
    claims: TokenClaims


def app_engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.app_engine
    return engine


def admin_engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.admin_engine
    return engine


def jwks(request: Request) -> JwksCache:
    cache: JwksCache = request.app.state.jwks
    return cache


def _mcp_call(request: Request) -> McpCall | None:
    """The MCP channel marker on this request, or None. A malformed marker is a
    401: it can only come from a defect in our own `/mcp` wrapper, and a
    request we cannot attribute is not one we authenticate."""
    try:
        return mcp_call_from_scope(request.scope)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, tr("the MCP channel marker is malformed")
        ) from exc


async def auth_context(request: Request) -> AuthContext:
    # THE CHANNEL DECIDES THE AUDIENCE (MCP server design §4). A request that
    # came in through `/mcp` carries the scope marker and is validated against
    # the MCP resource ONLY; every other request against `oidc_audience` only.
    # Neither token is accepted on the other's channel. (The channel itself is
    # bound for audit by `app.mcp.guard.McpNeverExposedGuard`, for the whole
    # request, before routing.)
    call = _mcp_call(request)
    mcp_audience = mcp_resource(request.app.state.settings)
    audience: str | None = None
    if call is not None:
        audience = mcp_audience
        if audience is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                tr("MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set."),
            )
        # The second of the two never-exposed refusals; `McpNeverExposedGuard`
        # is the first, and also covers the routes that never authenticate.
        if id(request.scope.get("route")) in request.app.state.mcp_never_route_ids:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("this action is never available to an AI app")
            )
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, tr("missing bearer token"))
    try:
        claims = await validate_token(
            authorization.removeprefix("Bearer "),
            settings=request.app.state.settings,
            jwks=request.app.state.jwks,
            audience=audience,
        )
    except TokenValidationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    # OFF THE CHANNEL, A TOKEN THAT CARRIES THE MCP AUDIENCE IS REFUSED even if
    # it also carries the console's -- by `require_console_token` below, which
    # the UI WebSocket and the remote tunnel call too: one rule, three doors.
    #
    # ON THE CHANNEL: invariant 1c(a) admits a token "carrying the
    # MCP audience and no other platform audience". A console token that also
    # names the MCP resource is a misconfigured identity provider, not a grant.
    if call is not None and request.app.state.settings.oidc_audience in claims.aud:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            tr("this token also grants the console and is not accepted over MCP"),
        )
    # THE CHANNEL ALSO DECIDES THE ORIGIN: off the channel only a token the
    # console's own client obtained, naming no MCP audience; on it only a person's,
    # never the console's and never a service account's. Before the org
    # lookup, so a refused token provisions no `users` row and writes no
    # first-seen `auth.login` -- `McpAsgi` turns this 401 into its
    # `invalid_token` challenge, and an in-process MCP call is held to it too.
    try:
        if call is None:
            require_console_token(claims, request.app.state.settings)
        else:
            require_person_mcp_token(claims, request.app.state.settings)
    except TokenValidationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    # Org lookup by slug precedes tenant context, so it uses the admin engine —
    # a read-only bootstrap query, the only pre-tenant step in the request path.
    async with AsyncSession(request.app.state.admin_engine) as session:
        org_id = (
            await session.execute(
                select(Organization.id).where(Organization.slug == claims.org_slug)
            )
        ).scalar_one_or_none()
    if org_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, tr("unknown organization"))

    async with org_session(org_id, engine=request.app.state.app_engine) as session:
        # ADMISSION, DOOR 1 OF 3 (spec §3.2). The token is genuine from here
        # on; the remaining question is whether this identity is one we admit.
        #
        # 403, NOT 401. A 401 tells the browser its credential was rejected, so
        # it goes round the sign-in loop and comes back with another token that
        # fails in exactly the same way. The credential is fine — the identity
        # is not permitted, which is precisely what 403 means.
        #
        # THE ALLOWLIST IS NOW THE ORG'S OWN SETTING (invisible-identity-layer
        # design §2.2), so it cannot be read before the org is known. That is
        # why this moved inside the tenant session: the value belongs to the
        # tenant, and reading it here means it is constrained by RLS rather
        # than by a `WHERE` clause. Resolved PER REQUEST, with no cache, so a
        # change saved in the console applies to the next request without a
        # restart — see `app.core.identity.policy`.
        #
        # WHAT DID NOT CHANGE, AND IT IS THE PROPERTY THAT MATTERS: a refused
        # identity still does not leave a provisioned `users` row behind. The
        # refusal is the FIRST thing inside this transaction, ahead of the
        # self-heal below, and it raises — so `org_session`'s `session.begin()`
        # rolls back and nothing is written. Attack traffic cannot populate the
        # tenant, and the preview's own output cannot be grown by it.
        refusal = domain_refusal(
            claims.email,
            await resolve_console_domain(session, org_id, request.app.state.settings),
        )
        if refusal is not None:
            log_admission_refusal("http", sub=claims.sub, email=claims.email, reason=refusal)
            # The audit/log reason stays the English constant; only the HTTP body is
            # translated, so a Spanish request never writes Spanish into the chain.
            raise HTTPException(status.HTTP_403_FORBIDDEN, tr_marked(refusal))

        user = (
            await session.execute(select(User).where(User.external_subject == claims.sub))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                org_id=org_id,
                external_subject=claims.sub,
                email=claims.email,
                display_name=claims.name,
                last_login_at=datetime.now(UTC),
            )
            session.add(user)
            await session.flush()
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="user",
                actor_id=user.id,
                action="auth.login",
                target_type="user",
                target_id=str(user.id),
                metadata={"first_seen": True, "email": claims.email},
            )
        else:
            # `users.status`, the second admission rule, and the reason it is
            # checked here rather than trusted to Keycloak: disabling an
            # account there stops the NEXT token being issued, it does not
            # revoke the one already in the operator's browser. Until that one
            # expires, a disabled user is fully authorized. Checked before
            # `last_login_at` is touched, so a refused request does not record
            # a login it did not grant.
            #
            # Only the EXISTING-row branch has a status to consult. A `sub`
            # with no row is self-healed above exactly as before -- that is a
            # user the platform has never seen, not a user someone disabled,
            # and inventing a refusal for it would change behaviour this
            # workstream has no mandate to change.
            status_reason = status_refusal(user.status)
            if status_reason is not None:
                log_admission_refusal(
                    "http", sub=claims.sub, email=claims.email, reason=status_reason
                )
                raise HTTPException(status.HTTP_403_FORBIDDEN, tr_marked(status_reason))

            # EMAIL IS REFRESHED FROM THE CLAIM; `display_name` IS NOT, and the
            # asymmetry is the design (spec §7.2). Keycloak owns the identity
            # fact -- the address is changed there, behind re-authentication
            # and verification -- while ITMS owns the presentation preference,
            # which the §7.1 profile form writes. Refreshing both would make
            # every sign-in silently undo that form.
            #
            # THIS IS NOT COSMETIC. `email` was written only in the `user is
            # None` branch above, so an address changed in Keycloak never
            # reached ITMS. The admission gate two dozen lines up tests
            # `claims.email` -- the live value -- while every screen, every
            # audit metadata field and the §3.3 lockout preflight read this
            # row. The security decision and the displayed fact came from
            # different sources; `deploy/README.md`'s preflight recovery step
            # ("sign the user in so ITMS refreshes the address") was untrue
            # until this existed.
            #
            # Guarded on inequality, not written unconditionally: admission
            # runs on EVERY request, not once per sign-in, so an unguarded
            # write would append an audit event per HTTP call.
            # `claims.email` GUARDED FOR TRUTHINESS, not just inequality.
            # `validate_token` yields `claims.get("email", "")`, so a token
            # carrying no address presents one as "". With the allowlist unset
            # -- the default, and the state of any deployment that has not
            # opted in -- such a token is admitted, and comparing on
            # inequality alone would overwrite a real stored address with
            # nothing. An ABSENT claim is not evidence the address was
            # removed; it is evidence this token did not carry one, which is
            # true of any IdP or client scope that omits `email`. The refresh
            # exists to propagate a CHANGE, and "" is not a change.
            if claims.email and user.email != claims.email:
                await audit_service.record(
                    session,
                    org_id=org_id,
                    actor_type="user",
                    actor_id=user.id,
                    action="auth.email_changed",
                    target_type="user",
                    target_id=str(user.id),
                    metadata={"from": user.email, "to": claims.email},
                )
                user.email = claims.email

            user.last_login_at = datetime.now(UTC)
            session.add(user)
        user_id = user.id

    # Task 3 (error monitoring): stash org_id/user_id on `request.state` --
    # a PLAIN object attribute, never a contextvar -- so `app.api.
    # request_context.request_context_middleware`'s exception handler can
    # recover them for a capture. This is NOT the contextvar binding
    # (`require_permission`'s own `dependency`, below, does that one, via
    # `bind()`) -- it exists because that middleware's `except` clause runs
    # in a DIFFERENT asyncio Task than this coroutine: Starlette's
    # `BaseHTTPMiddleware.call_next` (which registering a function with
    # `app.middleware("http")` uses) spawns the downstream ASGI app --
    # routing, every dependency including this one, the endpoint -- inside
    # its OWN anyio task-group task, and a `contextvars.Token` minted inside
    # that child task cannot be used to reset the same contextvar from the
    # parent's frame (verified: doing exactly that raised "Token ... was
    # created in a different Context"). Contextvars never propagate from a
    # child Task's context back to its parent at all -- only a plain
    # attribute on an object BOTH frames hold a reference to (this same
    # `Request`) crosses that boundary. Set unconditionally on every
    # resolution (idempotent -- a second resolution in the same request,
    # e.g. `app.modules.portal.principal.resolve_portal_principal` awaiting
    # this directly rather than through `Depends`, simply overwrites with
    # the same values).
    request.state.org_id = str(org_id)
    request.state.user_id = str(user_id)

    return AuthContext(org_id=org_id, user_id=user_id, claims=claims)


async def bound_auth_context(request: Request) -> AsyncIterator[AuthContext]:
    """`auth_context`, with org_id/user_id bound as CONTEXTVARS for the rest
    of the request -- the dependency ungated routes should use.

    THE GAP THIS CLOSES. `require_permission` binds the correlation ids
    around its `yield`, so every log line emitted while handling a
    permission-gated route carries them. Routes that authenticate but need
    no permission code depended on the bare, unbound dependency, which stashes
    `request.state.org_id`/`user_id` (so the middleware's *capture* still
    gets them) but binds nothing -- so ordinary log lines from those routes
    were missing org_id/user_id entirely, and nothing said so. Five routes
    were in that state, including `GET /me`.

    Binding here rather than at each call site means a new ungated route
    cannot silently rejoin them: `tests/test_logging_discipline.py` asserts
    no route depends on the unbound `auth_context`.

    An ordinary `with` is correct, for the reason `require_permission`'s
    comment gives at length: everything from this `yield` until FastAPI
    closes the generator runs in the SAME asyncio Task as the binding. The
    middleware's capture call is the one that cannot use a contextvar, and
    it uses `request.state` instead.
    """
    ctx = await auth_context(request)
    with bind(org_id=str(ctx.org_id), user_id=str(ctx.user_id)):
        yield ctx


def require_permission(code: str) -> Callable[..., AsyncIterator[AuthContext]]:
    # WARNING: `app.core.rbac.route_permission` recovers `code` back out of
    # this closure by reflection -- it walks a route's dependant tree for the
    # inner function below by its `__qualname__`
    # ("require_permission.<locals>.dependency"), then reads `code` out of
    # `dependency.__code__.co_freevars`/`dependency.__closure__` (`code` must
    # be a genuine free variable of `dependency`, i.e. still referenced by its
    # body). This is what makes RBAC completeness DERIVABLE from the router
    # (invariant #6) instead of a hand-maintained "route -> permission" list
    # that a later route addition could silently miss.
    #
    # Refactoring this function's SHAPE breaks that reflection silently:
    # renaming `dependency`, renaming the `code` parameter/free variable, or
    # restructuring this into something where `code` is no longer a free
    # variable of the returned closure all make `route_permission` return
    # `None` for every route in the codebase, not just this module's. A
    # completeness test (`test_every_notification_route_requires_a_permission`
    # and its ticket-core/other-module siblings) DOES catch this -- it fails
    # loudly -- but the failure surfaces inside whichever module's test file
    # happens to run the assertion, pointing at that module's router, not at
    # this file. See `tests/test_rbac.py`'s canary test for a failure that
    # instead points here.
    async def dependency(
        request: Request, ctx: AuthContext = Depends(bound_auth_context)
    ) -> AsyncIterator[AuthContext]:
        # `bound_auth_context`, not `auth_context`: the org_id/user_id
        # contextvar binding lives THERE now, once, so gated and ungated
        # routes get it by the same mechanism. This dependency used to hold
        # its own `with bind(...)` around the yield; five ungated routes
        # depending on the bare `auth_context` therefore had no binding at
        # all, which is the gap `bound_auth_context`'s docstring describes.
        # Its binding is already active by the time this body runs, and
        # stays active until FastAPI closes ITS generator -- which happens
        # after this one, since this depends on it.
        #
        # NEVER OVER MCP (MCP server design §5.1): credential and license-key
        # gates refuse the channel whatever the person holds. Checked before
        # RBAC so the answer does not reveal whether they hold it.
        if code in MCP_FORBIDDEN_CODES and mcp_call_from_scope(request.scope) is not None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, tr("this action is never available to an AI app")
            )
        async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
            allowed = await has_permission(session, ctx.user_id, code)
        if not allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, tr("requires {code}", code=code))
        yield ctx

    return dependency
