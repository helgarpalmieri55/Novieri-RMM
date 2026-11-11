"""Request dependencies: authentication context and RBAC gates.

Every privileged route depends on require_permission(code), which resolves the
operator -- from the console's session cookie, or on the MCP channel from its
bearer token -- pins the tenant, and evaluates RBAC inside the tenant session —
server-side, always.

The same evaluation yields the request's CLIENT SCOPE (MSP clients spec
§2.1-2.2): the clients where the person holds the route's code, narrowed --
never widened -- by `X-Client-Id`. `AuthContext.clients` carries it to every
`org_session`, where RLS enforces it. A contact (`users.kind = 'contact'`) is
refused here; the portal admits one through `admit(..., contacts=True)`, and so
do the person's own account routes (`bound_account_context`, phase 2 D26).
"""

import dataclasses
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.auth import loopback
from app.core.auth.refusals import AUTH_REQUIRED, PORTAL_ONLY, STEP_UP_REQUIRED, AuthRefusal
from app.core.auth.sessions import SessionPrincipal, resolve, step_up_fresh
from app.core.client_scope import ClientScope, parse_client_header
from app.core.i18n import N_, tr, tr_marked
from app.core.identity import (
    CONTACT_KIND,
    contact_refusal,
    domain_refusal,
    log_admission_refusal,
    status_refusal,
)
from app.core.identity.door import person_scope
from app.core.identity.policy import resolve_console_domain
from app.core.logging import bind
from app.core.mcp_channel import (
    MCP_FORBIDDEN_CODES,
    McpCall,
    mcp_call_from_scope,
    mcp_resource,
)
from app.core.rbac import clients_holding
from app.core.security import (
    JwksCache,
    TokenClaims,
    TokenValidationError,
    require_person_mcp_token,
    validate_token,
)
from app.core.tenancy import org_session
from app.models import Organization, User


@dataclass(frozen=True)
class AuthContext:
    org_id: UUID
    user_id: UUID
    # HOW THE PERSON GOT IN -- exactly one of the two (P2 design §6.5). The
    # console's own session (`principal`, the `__Host-itms_session` cookie), or
    # a bearer token's claims (`claims`: the MCP channel only, since own
    # authentication phase 1 Task 13 removed the console's Keycloak token).
    # Read the person's address through `email`, which works for both (D14).
    claims: TokenClaims | None
    # The request's client scope (MSP clients spec §2.2), already narrowed by
    # X-Client-Id. `bound_auth_context`: every client the person holds any
    # role in. `require_permission(code)`: every client they hold `code` in.
    # Routes pass it to `org_session(..., clients=ctx.clients)`, and RLS does
    # the rest: a record of a client outside it does not exist for the request.
    clients: ClientScope
    principal: SessionPrincipal | None = None

    @property
    def email(self) -> str:
        """The person's address: the `users` row's for a session, the token's
        claim for an MCP bearer (what that path has always checked)."""
        if self.principal is not None:
            return self.principal.email
        return "" if self.claims is None else self.claims.email


@dataclass(frozen=True)
class Admission:
    """A person door 1 admitted, before the console and the portal part ways.

    `clients` is the person's OWN scope -- a contact's one client (only the
    portal admits a contact), or every client a technician holds any role in
    (spec §2.2) -- and `requested_client` the client X-Client-Id names. A
    door gates on the first and hands the route `clients.narrow(requested)`.
    """

    org_id: UUID
    user_id: UUID
    claims: TokenClaims | None
    clients: ClientScope
    requested_client: UUID | None
    principal: SessionPrincipal | None = None


#: The 403 for global configuration -- an O-table row with no client, which
#: only an all-clients scope may write (spec §2.2) -- reached with a scope that
#: is not "every client". Shared by every such route, so it reads the same.
ALL_CLIENTS_REQUIRED = N_("only a technician with access to every client can do this")


def require_all_clients(ctx: AuthContext) -> None:
    """Refuse a write to Novieri-global configuration unless the request's
    scope is every client (spec §2.2: "Un técnico limitado puede ver la
    configuración global, pero no cambiarla"). A request narrowed by
    X-Client-Id is not every client either, whatever the person holds."""
    if not ctx.clients.is_all:
        raise HTTPException(status.HTTP_403_FORBIDDEN, tr_marked(ALL_CLIENTS_REQUIRED))


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


def requested_client(request: Request) -> UUID | None:
    """The client `X-Client-Id` names, or None (spec §2.2, §3.1). Anything but
    one UUID is a 400: the header narrows a scope, so a value that cannot be
    read is never quietly treated as absent -- that would be the un-narrowed
    scope the caller asked not to use. The header sent TWICE is a 400 too,
    whatever the copies say, rather than whichever one a parser keeps: the
    console WebSocket refuses `?client=` twice (4400) by the same rule."""
    values = request.headers.getlist("X-Client-Id")
    try:
        if len(values) > 1:
            raise ValueError("more than one X-Client-Id")  # i18n-exempt: caught, the 400 below
        return parse_client_header(values[0] if values else None)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, tr("X-Client-Id must be one client id")
        ) from exc


async def auth_context(request: Request) -> AuthContext:
    """Door 1 for the console and MCP: an admitted technician, and the clients
    they hold any role in, narrowed by X-Client-Id. A contact is refused."""
    person = await admit(request, contacts=False)
    # NARROWED, NEVER WIDENED (spec §2.2): `[that client] ∩ allowed`, which is
    # the empty scope -- zero rows -- for a client outside it.
    return AuthContext(
        org_id=person.org_id,
        user_id=person.user_id,
        claims=person.claims,
        clients=person.clients.narrow(person.requested_client),
        principal=person.principal,
    )


async def _session_principal(request: Request) -> SessionPrincipal | None:
    """The session that admits this request off the MCP channel, or None:
    `admit` then refuses it, since the console's bearer is gone (own
    authentication phase 1, Task 13).

    The assistant's internal token first (autenticación propia design §5.6): a
    request that SAYS it is the assistant's own call (`X-ITMS-Loopback`) is
    admitted by `loopback.verify_loopback` or refused there, and never falls
    through to the cookie. Otherwise the session cookie (`resolve`).

    The principal stays on `request.state`, a plain attribute as `org_id` and
    `user_id` are, so an assistant route -- the portal's included, whose
    `PortalContext` carries no principal -- can mint the next turn's token from
    it (`loopback.loopback_authorization`) without a credential on any
    context object."""
    if loopback.claims_loopback(request):
        principal: SessionPrincipal | None = await loopback.verify_loopback(request)
        request.state.via_loopback = True
    else:
        principal = await resolve(request)
        request.state.via_loopback = False
    request.state.session_principal = principal
    return principal


async def admit(request: Request, *, contacts: bool) -> Admission:
    """Authenticate the request and admit the person behind it (spec §3.2):
    off the MCP channel by the console's session (`_admit_session`), on it by
    the MCP OAuth token.

    `contacts` is the one difference between the console and the portal: the
    console refuses a contact with 403 (spec §1.3), the portal admits one
    (`app.modules.portal.principal`). Everything else -- the channel, the
    origin, the org, the domain rule, `users.status`, the self-heal -- is the
    same door for both.
    """
    # THE CHANNEL DECIDES THE CREDENTIAL (MCP server design §4; own
    # authentication phase 1). A request that came in through `/mcp` carries
    # the scope marker and is admitted only by a token validated against the
    # MCP resource; every other request only by the console's session. Neither
    # is accepted on the other's channel. (The channel itself is bound for
    # audit by `app.mcp.guard.McpNeverExposedGuard`, for the whole request,
    # before routing.)
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
    # THE CONSOLE'S OWN SESSION (P2 design §6.5), and never on the MCP
    # channel: a cookie the browser holds is not a grant an AI app was given
    # (invariant 1c(a)). Nor is the assistant's internal token, which
    # `_session_principal` takes first and `verify_loopback` refuses on an MCP
    # call anyway.
    if call is None:
        # Off the MCP channel only the session admits (own authentication
        # phase 1, Task 13): the console no longer sends a bearer, and the door
        # no longer reads one.
        principal = await _session_principal(request)
        if principal is None:
            raise AuthRefusal(
                status.HTTP_401_UNAUTHORIZED, code=AUTH_REQUIRED, detail=tr("sign in to continue")
            )
        return await _admit_session(request, principal, contacts=contacts)
    if loopback.claims_loopback(request):
        # The two in-process channels never meet (1c(a): on MCP, only OAuth
        # makes a session).
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, tr("the assistant's internal token was refused")
        )

    # ON THE MCP CHANNEL ONLY (MCP server design §4; own authentication phase 1
    # keeps MCP on its OAuth tokens until phase 3): the MCP resource is the
    # only audience, a token that also grants the console is refused, and so is
    # a token the console's own client obtained or a service account's.
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise AuthRefusal(
            status.HTTP_401_UNAUTHORIZED, code=AUTH_REQUIRED, detail=tr("missing bearer token")
        )
    try:
        claims = await validate_token(
            authorization.removeprefix("Bearer "),
            settings=request.app.state.settings,
            jwks=request.app.state.jwks,
            audience=audience,
        )
    except TokenValidationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    # Invariant 1c(a) admits a token "carrying the MCP audience and no other
    # platform audience". A token that also names the console's audience is a
    # misconfigured identity provider, not a grant. (Off the channel no bearer
    # is read at all, so an MCP grant is never replayable against the console.)
    if request.app.state.settings.oidc_audience in claims.aud:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            tr("this token also grants the console and is not accepted over MCP"),
        )
    # THE ORIGIN: only a person's token, never one the console's own client
    # obtained and never a service account's. Before the org lookup, so a
    # refused token provisions no `users` row and writes no first-seen
    # `auth.login` -- `McpAsgi` turns this 401 into its `invalid_token`
    # challenge, and an in-process MCP call is held to it too.
    try:
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

    # Read before the tenant session: a malformed header is the caller's
    # mistake, answered before anything is read or written on their behalf.
    requested = requested_client(request)

    # EVERY CLIENT, AND ONLY FOR THIS (spec §2.2, "Resolver quién es el
    # técnico"): the person's row and grants are read before their scope
    # exists, because the scope is computed from exactly these rows. The rest
    # of the request runs under the scope this computes.
    async with org_session(
        org_id, engine=request.app.state.app_engine, clients=ClientScope.all()
    ) as session:
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

        # A CONTACT NEVER ENTERS THE CONSOLE (spec §1.3), whatever roles it
        # holds -- refused before its grants are read. Raised inside this
        # transaction, so the `last_login_at` above is rolled back with it: a
        # refused request records no login. A self-healed row is always staff.
        refusal = None if contacts else contact_refusal(user.kind)
        if refusal is not None:
            log_admission_refusal("http", sub=claims.sub, email=claims.email, reason=refusal)
            raise HTTPException(status.HTTP_403_FORBIDDEN, tr_marked(refusal))
        if user.kind == CONTACT_KIND:
            # A contact sees its own client and nothing else; no grant widens
            # that (a grant is about what it may DO there). A contact with no
            # client cannot exist (`ck_users_kind_client`); were one to, it
            # would see nothing rather than fall through to a staff scope.
            scope = ClientScope.only([] if user.client_id is None else [user.client_id])
        else:
            scope = await clients_holding(session, user.id, None)
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

    return Admission(
        org_id=org_id,
        user_id=user_id,
        claims=claims,
        clients=scope,
        requested_client=requested,
    )


async def _admit_session(
    request: Request, principal: SessionPrincipal, *, contacts: bool
) -> Admission:
    """Door 1 for a console session: `admit`'s rules, read from the person's
    own row instead of a token (P2 design §6.5).

    A session that has not finished its second factor is not signed in: 401,
    and the sign-in routes (Task 7) are the only ones that accept it. Then, in
    the order the MCP bearer path uses: the console's email-domain rule (staff
    only -- it says who may use the CONSOLE; a contact, from phase 2, uses the
    portal), `users.status`, a contact refused unless `contacts` -- with 403
    `portal_only`, not logged (phase 2, Task 4) --, and the person's scope.
    Nothing is written and nothing is self-provisioned: a session exists only
    for a person the platform already has."""
    if not principal.factor_done:
        raise AuthRefusal(
            status.HTTP_401_UNAUTHORIZED,
            code=AUTH_REQUIRED,
            detail=tr("finish signing in: confirm your second factor first"),
        )
    requested = requested_client(request)
    settings = request.app.state.settings
    async with org_session(
        principal.org_id, engine=request.app.state.app_engine, clients=ClientScope.all()
    ) as session:
        user = (
            await session.execute(select(User).where(User.id == principal.user_id))
        ).scalar_one_or_none()
        if user is None:
            raise AuthRefusal(
                status.HTTP_401_UNAUTHORIZED,
                code=AUTH_REQUIRED,
                detail=tr("your session has ended; sign in again"),
            )
        who = f"user:{user.id}"
        domain_reason = (
            None
            if user.kind == CONTACT_KIND
            else domain_refusal(
                user.email, await resolve_console_domain(session, principal.org_id, settings)
            )
        )
        for reason in (domain_reason, status_refusal(user.status)):
            if reason is not None:
                log_admission_refusal("session", sub=who, email=user.email, reason=reason)
                raise HTTPException(status.HTTP_403_FORBIDDEN, tr_marked(reason))
        if not contacts:
            reason = contact_refusal(user.kind)
            if reason is not None:
                # Not logged (plan 2026-11-06, Task 4, decision 7): a contact at
                # the console's door is expected since phase 2 -- the console
                # asks `/me` on every page -- and the console answers it by
                # going to the portal (D42).
                raise AuthRefusal(
                    status.HTTP_403_FORBIDDEN, code=PORTAL_ONLY, detail=tr_marked(reason)
                )
        if user.kind == CONTACT_KIND:
            scope = ClientScope.only([] if user.client_id is None else [user.client_id])
        else:
            scope = await clients_holding(session, user.id, None)
    # The capture middleware's copy, as `admit` stashes it (its comment says why).
    request.state.org_id = str(principal.org_id)
    request.state.user_id = str(principal.user_id)
    return Admission(
        org_id=principal.org_id,
        user_id=principal.user_id,
        claims=None,
        clients=scope,
        requested_client=requested,
        principal=principal,
    )


def require_step_up(ctx: AuthContext, *, now: datetime | None = None) -> None:
    """Refuse unless a second factor was verified in this session in the last
    10 minutes (P2 design §2.5): `403 {"code": "step_up_required"}`, which the
    console answers by asking for it (`/auth/step-up/*`, Task 7).

    A session on a remembered device starts without one, so the vault keeps
    asking there (design §2.6, falsifier 5).

    A request with no session -- only MCP enters without one since own
    authentication phase 1 -- is refused too. The MCP channel never performs
    these acts: the routes that need the confirmation as a whole are no tools
    (the never-exposed table, `NOT_FOR_MCP`, `MCP_FORBIDDEN_CODES`), and on
    the two tools that need it only in part (a client's Microsoft tenants,
    switching MCP on) this refusal is the bound (plan phase 2, Task 14)."""
    if ctx.principal is None or not step_up_fresh(ctx.principal, now or datetime.now(UTC)):
        raise AuthRefusal(
            status.HTTP_403_FORBIDDEN,
            code=STEP_UP_REQUIRED,
            detail=tr("confirm your second factor again to do this"),
        )


def require_permission_with_step_up(
    code: str,
) -> Callable[..., Coroutine[Any, Any, AuthContext]]:
    """`require_permission(code)`, then `require_step_up`: a dependency, so the
    refusal comes before the body is read. `route_permission` still recovers
    `code` by walking into `gate`, as for `require_permission_on_every_client`."""
    gate = require_permission(code)

    async def with_step_up(ctx: AuthContext = Depends(gate)) -> AuthContext:
        require_step_up(ctx)
        return ctx

    return with_step_up


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


async def bound_account_context(request: Request) -> AsyncIterator[AuthContext]:
    """The routes of a person's OWN account -- My security: `/me/security`,
    `/me/password`, `/me/passkeys/*`, `/me/totp`, `/me/recovery-codes`,
    `/me/trusted-devices/*`, `/me/sessions/*` -- for a technician or a client's
    contact (own authentication phase 2, plan decision D26). The portal's
    door (`admit(contacts=True)`), and only for a browser session: a token is
    refused. These routes touch only the caller's own auth rows
    (`app.core.auth.account`), never a client's data; `GET /me` and the rest
    of the console keep `bound_auth_context`, which refuses a contact."""
    person = await admit(request, contacts=True)
    if person.principal is None:
        raise AuthRefusal(
            status.HTTP_401_UNAUTHORIZED, code=AUTH_REQUIRED, detail=tr("sign in to continue")
        )
    ctx = AuthContext(
        org_id=person.org_id,
        user_id=person.user_id,
        claims=person.claims,
        clients=person.clients.narrow(person.requested_client),
        principal=person.principal,
    )
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
        #
        # THE ROUTE'S CODE DECIDES THE SCOPE (spec §2.1-2.2). Holding `code`
        # in SOME client passes the gate -- the same rule as an org-wide grant
        # always was -- and the request then sees only the clients where the
        # person holds it: technician on X and read-only on Z, a route that
        # needs `jobs.dispatch` runs with scope [X], and Z's records are a 404
        # by RLS rather than by a per-record check somebody could forget.
        # Intersected with `ctx.clients`, which already carries the
        # X-Client-Id narrowing, so the header narrows this scope too and
        # nothing here can widen it.
        holding = await person_scope(
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            code=code,
            engine=request.app.state.app_engine,
        )
        if holding.is_empty:
            raise HTTPException(status.HTTP_403_FORBIDDEN, tr("requires {code}", code=code))
        yield dataclasses.replace(ctx, clients=ctx.clients.intersect(holding))

    return dependency


async def narrowed_to_every(
    request: Request, ctx: AuthContext, codes: Iterable[str]
) -> AuthContext:
    """`ctx`, its clients narrowed to those where the person holds EVERY one
    of `codes` (plan 2026-10-26 Task 14, T4-C2 items 2-3).

    For a route whose rows need more than its gate's one code -- a report,
    gated on `reporting.read`, reads the rows of each module it draws from
    (`devices.read`, `health.read`, ...). Holding a source code on X is no
    grant on Y's rows, so the request's RLS scope is the clients holding all
    of them: each code's clients (`person_scope`, as `require_permission`
    reads them) intersected in turn with `ctx.clients`, which already
    carries the X-Client-Id narrowing -- never wider than it. Codes held on
    DIFFERENT clients only intersect to the empty scope, which RLS answers
    with zero rows: no widening, and no error either."""
    scope = ctx.clients
    for code in sorted(set(codes)):
        scope = scope.intersect(
            await person_scope(
                org_id=ctx.org_id,
                user_id=ctx.user_id,
                code=code,
                engine=request.app.state.app_engine,
            )
        )
    return dataclasses.replace(ctx, clients=scope)


async def narrowed_to_any(
    request: Request, ctx: AuthContext, codes: Iterable[str]
) -> AuthContext | None:
    """`ctx`, its clients narrowed to those where the person holds AT LEAST
    ONE of `codes` -- or None when they hold none of them on any client (the
    caller's 403, as `require_permission` answers for one code).

    For the read routes that serve two audiences on two codes (remote
    sessions: an operator's `remote.control`, an auditor's
    `remote.recordings.read`; plan 2026-10-26 Task 14, T4-C2 item 13): the
    union of each code's clients, intersected with `ctx.clients`, so never
    wider than the request's own header-narrowed scope."""
    held = [
        await person_scope(
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            code=code,
            engine=request.app.state.app_engine,
        )
        for code in sorted(set(codes))
    ]
    every = next((scope for scope in held if scope.is_all), None)
    union = every if every is not None else ClientScope.only(
        frozenset().union(*(scope.ids for scope in held))
    )
    if union.is_empty:
        return None
    return dataclasses.replace(ctx, clients=ctx.clients.intersect(union))


def require_permission_on_every_client(
    code: str,
) -> Callable[..., Coroutine[Any, Any, AuthContext]]:
    """`require_permission(code)`, held with a scope of EVERY client.

    For the routes that write Novieri-global configuration (MSP clients spec
    §1.4: technicians and roles, incoming mail, ...): one row serves every
    client, so only an all-clients scope may change it (spec §2.2, "un técnico
    limitado puede ver la configuración global, pero no cambiarla"). Holding
    `code` on one client passes `require_permission`; it does not pass this.
    Neither does a request X-Client-Id narrowed, whatever the person holds.

    A DEPENDENCY, not a check in the handler, so the refusal comes before the
    request body is validated: a per-client technician learns nothing from a
    422 about a body they could never have sent. `route_permission` still
    recovers `code`, by walking into `gate` -- the one
    `require_permission.<locals>.dependency` this wraps.
    """
    gate = require_permission(code)

    async def on_every_client(ctx: AuthContext = Depends(gate)) -> AuthContext:
        require_all_clients(ctx)
        return ctx

    return on_every_client


def require_permission_on_every_client_with_step_up(
    code: str,
) -> Callable[..., Coroutine[Any, Any, AuthContext]]:
    """`require_permission_on_every_client(code)`, then `require_step_up`.

    For the global writes the operator put behind a recent second factor on
    2026-11-08 and 2026-11-09 (design §2.5, decisions S1 and S5): the
    organization's sign-in settings, who gets a technician's access and what a
    role grants. (S3, switching MCP on, is asked in `orgs.py` itself.) A
    DEPENDENCY, like the two it joins, so both refusals come before the body
    is read -- the scope first: a per-client technician is told the act is not
    theirs, never asked for a factor that could not make it so
    (`tests/test_step_up_widened.py` pins the order). `route_permission` still
    recovers `code` through both wrappers, and `tests/test_global_writes.py`
    still finds `on_every_client` under this one."""
    gate = require_permission_on_every_client(code)

    async def on_every_client_with_step_up(ctx: AuthContext = Depends(gate)) -> AuthContext:
        require_step_up(ctx)
        return ctx

    return on_every_client_with_step_up
