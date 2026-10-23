"""Push-install HTTP routes (spec §8, §14; `CLAUDE.md` invariant 2a (e), (f)).

**EVERY ROUTE CARRIES A `require_permission(...)` DEPENDENCY**, which is what
makes RBAC completeness DERIVABLE FROM THE ROUTER (`app/core/rbac.py`'s
`route_permission`, invariant #6) rather than from a list somebody has to
remember to update. A route added here without one returns `None` there and
fails `tests/test_route_gating.py` BY EXISTING.

**`agent.push.run` AND `agent.push.scope.manage` ARE DELIBERATELY SEPARATE,
AND THIS IS WHERE THAT BECOMES REAL.** Pushing inside an already-authorized
prefix and deciding what may be authorized at all are different decisions by
different people; collapsed into one code, anyone who can push could also
decide where pushes may go, which is the entire control. `ipam`'s
`sweep.run`/`scope.manage` split is the same shape one privilege level down.

**`agent.push.run` IS NOT THE WHOLE GATE**, and the second half is checked in
the SERVICE rather than here: a push additionally requires
`credentials.checkout`. A route dependency can express one code; the
conjunction is a rule about the call. See `preflight.request_push_batch`.

**THE HOST-KEY ROUTES ARE `agent.push.run`, NOT `scope.manage`** (spec §4.4).
Probing a host, confirming its fingerprint and pushing to it are all part of
RUNNING a push -- the operator who does one does all three -- so they share the
run code. Deciding WHERE pushes may go at all is the separate `scope.manage`
decision, and it keeps the authorization and credential-scope routes.

**THERE IS NO READ CODE.** Spec §8 names exactly two codes, and inventing a
third in the implementation would be diverging from an approved spec quietly.
So the AUTHORIZATION and CREDENTIAL-SCOPE list routes are gated on
`agent.push.scope.manage`: a holder of `agent.push.run` alone cannot browse
them, and finds out why a target was refused from the refusal itself, which
names the failing bound and the permission needed to fix it.

**ONE READ OF AUTHORIZATIONS IS A RUN ROUTE, AND IT CARRIES THE RUN
CONJUNCTION.** `GET /runs/prefixes` (design 2026-09-30 §1.1) shows the org's
ACTIVE authorized prefixes -- the only subnets a run may be built from --
to a holder of `agent.push.run` who ALSO holds `credentials.checkout`,
checked in `run_list.authorized_prefixes`. That is the same conjunction every
use of that list requires (preview, start, resume), so it shows nothing to
anyone who could not act on it. It shows only the prefix, its size and its
expiry, never the note, the author or revoked rows. If a read code proves
needed, adding `agent.push.read` is a decision to be made deliberately, in a
reviewed change.

**404, NEVER 403, FOR ANOTHER ORG'S ID.** A 403 confirms the id exists, which
is a cross-tenant existence oracle -- and over `credentials` it would be an
oracle on the vault's key space.

**ERRORS ARE MAPPED OUTSIDE THE `org_session` BLOCK, NEVER CAUGHT INSIDE IT.**
Catching inside leaves the failed request's partial writes committed; the
transaction is the unit of atomicity, and an `except` in the middle of it is a
promise that the half-written state is fine. That matters especially for
`POST /requests`, whose refusal path WRITES the `push.refused` audit rows and
then raises: the raise must escape the block so the rows commit with it.

**THE PUSH ROUTE DISPATCHES SIGNED JOBS, AND IT DOES SO THROUGH `dispatch.py`
ONLY.** Cycle B2 is the dispatch: `POST /requests` runs the ladder, signs a job
per target through `dispatch_push_batch`, and publishes the envelopes AFTER the
transaction commits (a job describing rolled-back state must never reach an
agent). This module is still the only path that dispatches these two job types;
both kernel gates refuse them everywhere else (clause (e)). The reshaped
`tests/test_agent_push_dispatches_nothing.py` asserts that the signing lives in
`dispatch.py` and nowhere else, and that NO file here ever connects to a target.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.core.i18n import tr, tr_marked
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job
from app.models import Credential
from app.modules.agent_push import authorization as authorization_service
from app.modules.agent_push import credential_scope as scope_service
from app.modules.agent_push import dispatch as dispatch_service
from app.modules.agent_push import host_key as host_key_service
from app.modules.agent_push import preflight as preflight_service
from app.modules.agent_push import run_list as run_list_service
from app.modules.agent_push import runs as runs_service
from app.modules.agent_push.errors import (
    PushAuthorizationError,
    PushConflictError,
    PushDispatchUnavailableError,
    PushError,
    PushListChangedError,
    PushNotFoundError,
    PushPermissionError,
)
from app.modules.agent_push.models import (
    PushAuthorization,
    PushCredentialScope,
    PushHostKey,
    PushRun,
    PushRunTarget,
)
from app.modules.agent_push.net import parse_prefix, prefix_size
from app.modules.agent_push.run_contract import Envelope
from app.modules.agent_push.schema import (
    AuthorizationCreate,
    AuthorizationOut,
    CredentialScopeBind,
    CredentialScopeOut,
    HostKeyConfirm,
    HostKeyOut,
    PushDispatchOut,
    PushDispatchTargetOut,
    PushRequestCreate,
    RunCountsOut,
    RunCreate,
    RunListRowOut,
    RunOut,
    RunPrefixOut,
    RunPreviewOut,
    RunTargetOut,
    RunTargetPageOut,
)

router = APIRouter(prefix="/agent-push", tags=["agent-push"])


def _reason_help(*tables: dict[str, str]) -> dict[str, str]:
    """Each refusal code's remedy, worded in the request's language.

    The tables hold English prose marked `N_()`; the codes (the keys) are what
    the console maps to its own words, and this text is the longer remedy it
    shows beside them, so it is translated here, where it reaches a response."""
    return {code: tr_marked(text) for table in tables for code, text in table.items()}


def _http_error(exc: PushError) -> HTTPException:
    """By TYPE, never by message text.

    `PushNotFoundError` is checked FIRST because it is a SUBCLASS of
    `PushValidationError`; reversing the order turns every 404 into a 400 and
    tells an operator their input was malformed when it was simply not theirs.
    """
    if isinstance(exc, PushNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, PushPermissionError):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, PushListChangedError):
        # A STRUCTURED 409, so the console tells "the list changed, show it
        # again" from "a run is already active" without parsing prose.
        return HTTPException(
            status.HTTP_409_CONFLICT, {"code": "list_changed", "message": str(exc)}
        )
    if isinstance(exc, PushConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, PushAuthorizationError):
        # 422 AND A STRUCTURED BODY. "Refused" that does not say WHICH bound
        # failed for WHICH address is a refusal nobody can act on (spec §9),
        # and a flat string would make the console re-parse prose to render it.
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "message": str(exc),
                "targets": [
                    {"address": address, "reasons": reasons}
                    for address, reasons in sorted(exc.targets.items())
                ],
                "reason_help": _reason_help(
                    preflight_service.REFUSAL_REASONS,
                    dispatch_service.DISPATCH_REFUSAL_REASONS,
                ),
            },
        )
    if isinstance(exc, PushDispatchUnavailableError):
        # 501, and it is the honest code: the request was well-formed, fully
        # authorized, and this platform has not implemented the act. A 4xx
        # would tell the operator they did something wrong; a 200 would tell
        # them something happened.
        return HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _engine(request: Request) -> Any:
    return request.app.state.app_engine


def _object_store(request: Request) -> ObjectStore:
    """Lazily build (and cache) the object store, mirroring `agent_distribution`.

    Construction touches object storage, so it stays out of app startup: an
    object store that is down should fail the routes that need it, not the whole
    API. A push needs it to presign the artefact URL the install job carries.
    """
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(request.app.state.settings)
        request.app.state.object_store = store
    assert isinstance(store, ObjectStore)
    return store


def _host_key_out(row: PushHostKey) -> HostKeyOut:
    installable = (
        row.confirmed_fingerprint is not None
        and row.confirmed_fingerprint == row.observed_fingerprint
    )
    readiness = row.readiness or {}
    return HostKeyOut(
        address=str(row.address),
        transport=row.transport,
        observed_fingerprint=row.observed_fingerprint,
        observed_key_type=row.observed_key_type,
        observed_at=row.observed_at,
        confirmed_fingerprint=row.confirmed_fingerprint,
        confirmed_at=row.confirmed_at,
        confirmed_by=row.confirmed_by,
        installable=installable,
        winrm_https=bool(readiness.get("winrm_https", False)),
        winrm_http=bool(readiness.get("winrm_http", False)),
        kdc_reachable=bool(readiness.get("kdc_reachable", False)),
    )


def _authorization_out(row: PushAuthorization) -> AuthorizationOut:
    return AuthorizationOut(
        id=row.id,
        prefix=str(row.prefix),
        address_count=prefix_size(parse_prefix(str(row.prefix))),
        note=row.note,
        authorized_by=row.authorized_by,
        authorized_at=row.authorized_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
    )


def _scope_out(row: PushCredentialScope, name: str) -> CredentialScopeOut:
    return CredentialScopeOut(
        id=row.id,
        credential_id=row.credential_id,
        credential_name=name,
        prefix=str(row.prefix),
        address_count=prefix_size(parse_prefix(str(row.prefix))),
        note=row.note,
        bound_by=row.bound_by,
        bound_at=row.bound_at,
        unbound_at=row.unbound_at,
        unbound_by=row.unbound_by,
    )


async def _credential_names(
    session: AsyncSession, org_id: UUID, ids: set[UUID]
) -> dict[UUID, str]:
    """Labels for the credentials a scope list names, in one query.

    The explicit `org_id` predicate is belt-and-braces beside RLS, and it is
    the belt that is observable: through `org_session` Postgres filters first,
    so deleting it turns no route test red.
    """
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Credential.id, Credential.name).where(
                Credential.org_id == org_id, Credential.id.in_(ids)
            )
        )
    ).all()
    return {row.id: row.name for row in rows}


# --------------------------------------------------------------------------- #
# Push authorizations -- where credentialed pushes may go at all
# --------------------------------------------------------------------------- #


@router.get("/authorizations", response_model=list[AuthorizationOut])
async def list_push_authorizations(
    request: Request,
    include_revoked: bool = Query(
        default=False,
        description="Include revoked grants. Rows are never hard-deleted.",
    ),
    ctx: AuthContext = Depends(require_permission("agent.push.scope.manage")),
) -> list[AuthorizationOut]:
    """Every prefix this organization has authorized for credentialed pushes.

    Expired grants ARE listed (and marked), because an operator looking at this
    list needs to see the grant that lapsed last night as much as the one that
    never existed.
    """
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        rows = await authorization_service.list_authorizations(
            session, ctx.org_id, include_revoked=include_revoked
        )
        return [_authorization_out(row) for row in rows]


@router.post(
    "/authorizations",
    response_model=AuthorizationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_push_authorization(
    body: AuthorizationCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.scope.manage")),
) -> AuthorizationOut:
    """**HIGH PRIVILEGE.** This grant decides where this platform may present
    an administrative credential to a machine it has never managed and cannot
    authenticate.

    Deliberately not held by `technician`, and deliberately not implied by an
    IPAM scan authorization over the same prefix: authorizing an ICMP echo is
    not authorizing an administrative login.
    """
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await authorization_service.create_authorization(
                session,
                ctx.org_id,
                actor_id=ctx.user_id,
                prefix=body.prefix,
                note=body.note,
                expires_at=body.expires_at,
            )
            out = _authorization_out(row)
    except PushError as exc:
        raise _http_error(exc) from None
    return out


@router.delete(
    "/authorizations/{authorization_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_push_authorization(
    authorization_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.scope.manage")),
) -> None:
    """Revocation STAMPS; the row stays, so "who authorized presenting
    credentials on that segment in March" remains answerable."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            await authorization_service.revoke_authorization(
                session,
                ctx.org_id,
                actor_id=ctx.user_id,
                authorization_id=authorization_id,
            )
    except PushError as exc:
        raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# Credential scopes -- where one stored secret may ever be presented
# --------------------------------------------------------------------------- #


@router.get("/credential-scopes", response_model=list[CredentialScopeOut])
async def list_push_credential_scopes(
    request: Request,
    credential_id: UUID | None = Query(
        default=None, description="Narrow to one credential."
    ),
    include_unbound: bool = Query(
        default=False, description="Include unbound scopes. Rows survive unbinding."
    ),
    ctx: AuthContext = Depends(require_permission("agent.push.scope.manage")),
) -> list[CredentialScopeOut]:
    """Which stored credentials may be presented where.

    Names each credential; carries no secret, no `vault_ref` and no ciphertext.
    """
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        rows = await scope_service.list_scopes(
            session,
            ctx.org_id,
            credential_id=credential_id,
            include_unbound=include_unbound,
        )
        names = await _credential_names(
            session, ctx.org_id, {row.credential_id for row in rows}
        )
        return [_scope_out(row, names.get(row.credential_id, "")) for row in rows]


@router.post(
    "/credential-scopes",
    response_model=CredentialScopeOut,
    status_code=status.HTTP_201_CREATED,
)
async def bind_push_credential_scope(
    body: CredentialScopeBind,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.scope.manage")),
) -> CredentialScopeOut:
    """**HIGH PRIVILEGE.** Widening where a secret may be presented is one of
    the four independent bounds on what a single mistaken click can do.

    Refuses any credential whose type is not `push_install`: the credential is
    dedicated on purpose, because reuse means one theft costs every subsystem
    at once (spec §6.2).
    """
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await scope_service.bind_scope(
                session,
                ctx.org_id,
                actor_id=ctx.user_id,
                credential_id=body.credential_id,
                prefix=body.prefix,
                note=body.note,
            )
            names = await _credential_names(session, ctx.org_id, {row.credential_id})
            out = _scope_out(row, names.get(row.credential_id, ""))
    except PushError as exc:
        raise _http_error(exc) from None
    return out


@router.delete(
    "/credential-scopes/{scope_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def unbind_push_credential_scope(
    scope_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.scope.manage")),
) -> None:
    """Unbinding STAMPS; the row stays, so "which addresses was this credential
    bound to when it was used" survives somebody narrowing the scope."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            await scope_service.unbind_scope(
                session, ctx.org_id, actor_id=ctx.user_id, scope_id=scope_id
            )
    except PushError as exc:
        raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# The push request itself -- which, in cycle B1, always refuses
# --------------------------------------------------------------------------- #


@router.post(
    "/requests", response_model=PushDispatchOut, status_code=status.HTTP_202_ACCEPTED
)
async def request_push_install(
    body: PushRequestCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> PushDispatchOut:
    """Ask for a push probe or a push install against explicitly named addresses.

    Runs the complete refusal ladder -- promoted asset a person vouched for, an
    active push authorization, a credential scope, and an enabled site server on
    the target's segment, with no fallback originator -- and, for an install,
    the additional host-key bound (§7.3). If every target clears every bound the
    jobs are SIGNED and dispatched through the chosen site server; otherwise the
    request is refused and NOTHING is created (all-or-nothing, §5.2).

    Additionally requires `credentials.checkout`, checked in the service: a push
    presents a stored secret, and the permission to decrypt one is not optional.

    Outcomes and status codes, deliberately distinct: 202 (dispatched, with the
    batch and its job ids -- never a secret), 403 (the caller may not use a
    stored secret), 422 (one or more targets failed a bound, every failing bound
    named per address). A caller must be able to tell "done" from "you may not"
    from "these machines may not".

    **THE ENVELOPES ARE PUBLISHED ONLY AFTER THE TRANSACTION COMMITS**, exactly
    as `site.wake` does and for the sharper version of the same reason: a job
    handed to an agent describing state that rolled back would be a push nobody
    authorized, and this one presents an administrative credential. The refusal
    is likewise raised only after the `push.refused` rows commit, or the
    operator would be told "refused" while the audit chain said nothing happened.
    """
    result: dispatch_service.PushDispatchResult | None = None
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            result = await dispatch_service.dispatch_push_batch(
                session,
                request.app.state.job_engine,
                # A LAZY FACTORY, not a built store. The object store is only
                # needed to presign the install artefact, and it is built only
                # after the ladder and the `credentials.checkout` check have
                # passed -- so an S3 that is down (or a caller who may not use a
                # stored secret) fails cleanly rather than as a 500 from bucket
                # construction. A probe never touches it.
                (lambda: _object_store(request)) if body.mode == "install" else None,
                request.app.state.vault,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                credential_id=body.credential_id,
                targets=body.targets,
                kind=body.mode,
                # Where a pushed computer enrolls: server configuration, never
                # the presigned release URL's origin (plan W2, superseded).
                public_console_url=request.app.state.settings.public_console_url,
            )
    except PushError as exc:
        raise _http_error(exc) from None
    assert result is not None
    if result.refused:
        raise _http_error(
            PushAuthorizationError(
                tr(
                    "this push was refused and nothing was created. Validation is "
                    "all-or-nothing: an operator must not discover mid-batch that some of "
                    "their targets were outside the authorization or lacked a confirmed host "
                    "key"
                ),
                result.refused,
            )
        )
    for envelope in result.envelopes:
        await publish_job(
            request.app.state.redis, UUID(envelope["job"]["device"]), envelope
        )
    assert result.batch_id is not None
    return PushDispatchOut(
        batch_id=result.batch_id,
        kind=result.kind,
        targets=[
            PushDispatchTargetOut(
                address=t.address,
                job_id=t.job_id,
                site_server_device_id=t.site_server_device_id,
            )
            for t in result.dispatched
        ],
    )


# --------------------------------------------------------------------------- #
# Host keys -- the crux: a probe observes, a human confirms, an install verifies
# --------------------------------------------------------------------------- #


@router.get("/host-keys", response_model=list[HostKeyOut])
async def list_push_host_keys(
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> list[HostKeyOut]:
    """Every target host key this org has observed, and whether it is confirmed.

    Gated on `agent.push.run`, not `scope.manage`: confirming a host key is part
    of running a push (§4.4), and the operator who probes and pushes is the one
    who must see and confirm the fingerprint. Carries public key material only.
    """
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        rows = await host_key_service.list_host_keys(session, ctx.org_id)
        return [_host_key_out(row) for row in rows]


@router.post(
    "/host-keys/confirm",
    response_model=HostKeyOut,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_push_host_key(
    body: HostKeyConfirm,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> HostKeyOut:
    """**HIGH CONSEQUENCE.** Confirming a fingerprint makes it the trust anchor
    every later push to this host is verified against.

    On first contact the platform cannot authenticate the target (§7.3); this
    is the human step that replaces trust-on-first-use with a fingerprint the
    operator compared out-of-band. The confirmed key must equal what the last
    probe observed -- confirming anything else is refused.
    """
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            row = await host_key_service.confirm_host_key(
                session,
                ctx.org_id,
                actor_id=ctx.user_id,
                address=body.address,
                fingerprint=body.fingerprint,
            )
            out = _host_key_out(row)
    except PushError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# Push runs -- a subnet builds the list; a person confirms it; the pacer
# continues it (design 2026-09-30; CLAUDE.md 2a(e), 2a(f) as amended).
#
# DECLARATION ORDER MATTERS: `/runs/prefixes` and `/runs/preview` come before
# `/runs/{run_id}`, or "prefixes" would be parsed as a run id.
# --------------------------------------------------------------------------- #


def _run_dispatcher(request: Request) -> dispatch_service.RunDispatcher:
    return dispatch_service.RunDispatcher(
        request.app.state.job_engine,
        lambda: _object_store(request),
        request.app.state.vault,
        public_console_url=request.app.state.settings.public_console_url,
    )


def _run_reason_help() -> dict[str, str]:
    return _reason_help(
        preflight_service.REFUSAL_REASONS,
        dispatch_service.DISPATCH_REFUSAL_REASONS,
        run_list_service.LIST_REASONS,
        runs_service.RUN_REFUSAL_REASONS,
    )


async def _run_out(session: AsyncSession, org_id: UUID, run: PushRun) -> RunOut:
    counts = await runs_service.run_counts(session, org_id, run.id)
    credential = (
        (
            await session.execute(
                select(Credential).where(
                    Credential.org_id == org_id, Credential.id == run.credential_id
                )
            )
        ).scalar_one_or_none()
        if run.credential_id is not None
        else None
    )
    # `signed_in` is shown for the secret as it is NOW (spec §1.4a): a sign-in
    # with a version since replaced is not reported as proof. The fingerprint
    # itself is compared here and never put in the response.
    fingerprint = (
        None if credential is None else dispatch_service.credential_fingerprint(credential)
    )
    guard = await runs_service.auth_window(session, run, fingerprint)
    names = {credential.id: credential.name} if credential is not None else {}
    return RunOut(
        id=run.id,
        prefix=str(run.prefix),
        credential_id=run.credential_id,
        credential_name=names.get(run.credential_id, "") if run.credential_id else "",
        created_by=run.created_by,
        created_at=run.created_at,
        state=run.state,
        target_count=run.target_count,
        concurrency=runs_service.PUSH_RUN_CONCURRENCY,
        list_digest=run.list_digest,
        finished_at=run.finished_at,
        counts=RunCountsOut(**counts),
        stopped_at=run.stopped_at,
        auth_failures=guard.auth_failures,
        auth_failure_limit=runs_service.PUSH_RUN_AUTH_FAILURE_LIMIT,
        signed_in=guard.proven,
    )


def _target_out(row: PushRunTarget) -> RunTargetOut:
    return RunTargetOut(
        position=row.position,
        device_id=row.device_id,
        address=str(row.address),
        state=row.state,
        job_id=row.job_id,
        outcome=row.outcome,
        detail=row.detail,
        reasons=list(row.reasons or []),
        updated_at=row.updated_at,
    )


async def _publish_after_commit(request: Request, envelopes: list[Envelope]) -> None:
    for device_id, envelope in envelopes:
        await publish_job(request.app.state.redis, device_id, envelope)


@router.get("/runs/prefixes", response_model=list[RunPrefixOut])
async def list_push_run_prefixes(
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> list[RunPrefixOut]:
    """The subnets a run may be built from: this org's ACTIVE push
    authorizations, and nothing else. None can be typed (design §1.1).
    `credentials.checkout` is required too, checked in the service, as for
    every use of this list."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            rows = await run_list_service.authorized_prefixes(
                session, ctx.org_id, actor_id=ctx.user_id
            )
            out = [
                RunPrefixOut(
                    prefix=str(row.prefix),
                    address_count=prefix_size(parse_prefix(str(row.prefix))),
                    expires_at=row.expires_at,
                )
                for row in rows
            ]
    except PushError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/runs/preview", response_model=RunPreviewOut)
async def preview_push_run(
    request: Request,
    prefix: str = Query(min_length=1, max_length=64),
    credential_id: UUID = Query(),
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunPreviewOut:
    """Expand an authorized subnet into the named list a person confirms.
    Reads only: nothing is written, decrypted or signed."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            listed = await run_list_service.expand_prefix(
                session,
                ctx.org_id,
                actor_id=ctx.user_id,
                prefix=prefix,
                credential_id=credential_id,
                public_console_url=request.app.state.settings.public_console_url,
            )
    except PushError as exc:
        raise _http_error(exc) from None
    ready = len(listed.ready_rows)
    return RunPreviewOut(
        prefix=listed.prefix,
        credential_id=listed.credential_id,
        credential_name=listed.credential_name,
        list_digest=listed.list_digest,
        ready_count=ready,
        blocked_count=len(listed.rows) - ready,
        unvouched_count=listed.unvouched_count,
        awaiting_review_count=listed.awaiting_review_count,
        run_max=run_list_service.PUSH_RUN_MAX,
        concurrency=runs_service.PUSH_RUN_CONCURRENCY,
        rows=[
            RunListRowOut(
                device_id=row.device_id,
                address=row.address,
                hostname=row.hostname,
                os_family=row.os_family,
                ready=row.ready,
                reasons=list(row.reasons),
            )
            for row in listed.rows
        ],
        reason_help=_run_reason_help(),
    )


@router.post("/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def start_push_run(
    body: RunCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunOut:
    """**A PERSON CONFIRMS THE LIST.** The server expands the subnet again and
    refuses (409 `list_changed`) unless the digest matches, before any row is
    written or any secret touched. The pacer then dispatches the first targets
    in this same transaction, and their envelopes are published only after it
    commits."""
    envelopes: list[Envelope] = []
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            run = await runs_service.create_run(
                session,
                ctx.org_id,
                actor_id=ctx.user_id,
                prefix=body.prefix,
                credential_id=body.credential_id,
                list_digest=body.list_digest,
                public_console_url=request.app.state.settings.public_console_url,
            )
            envelopes = await runs_service.advance_run(
                session, ctx.org_id, run.id, dispatcher=_run_dispatcher(request)
            )
            out = await _run_out(session, ctx.org_id, run)
    except PushError as exc:
        raise _http_error(exc) from None
    await _publish_after_commit(request, envelopes)
    return out


@router.get("/runs", response_model=list[RunOut])
async def list_push_runs(
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> list[RunOut]:
    async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
        rows = await runs_service.list_runs(session, ctx.org_id)
        return [await _run_out(session, ctx.org_id, row) for row in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_push_run(
    run_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            run = await runs_service.get_run(session, ctx.org_id, run_id)
            out = await _run_out(session, ctx.org_id, run)
    except PushError as exc:
        raise _http_error(exc) from None
    return out


@router.get("/runs/{run_id}/targets", response_model=RunTargetPageOut)
async def list_push_run_targets(
    run_id: UUID,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunTargetPageOut:
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            total, rows = await runs_service.list_targets(
                session, ctx.org_id, run_id, offset=offset, limit=limit
            )
            out = RunTargetPageOut(
                total=total,
                items=[_target_out(row) for row in rows],
                reason_help=_run_reason_help(),
            )
    except PushError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/runs/{run_id}/pause", response_model=RunOut)
async def pause_push_run(
    run_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunOut:
    """A person's act. New dispatches stop; installs in flight finish."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            run = await runs_service.pause_run(
                session, ctx.org_id, run_id=run_id, actor_id=ctx.user_id
            )
            out = await _run_out(session, ctx.org_id, run)
    except PushError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/runs/{run_id}/resume", response_model=RunOut)
async def resume_push_run(
    run_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunOut:
    """A person's act, and the only way a paused run, or one the lockout guard
    stopped (`stopped_credential`, spec §1.4a), runs again. Dispatch restarts
    in this request -- from ONE target, because the guard's window restarts --
    each target re-checked as it goes."""
    envelopes: list[Envelope] = []
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            run = await runs_service.resume_run(
                session, ctx.org_id, run_id=run_id, actor_id=ctx.user_id
            )
            envelopes = await runs_service.advance_run(
                session, ctx.org_id, run.id, dispatcher=_run_dispatcher(request)
            )
            out = await _run_out(session, ctx.org_id, run)
    except PushError as exc:
        raise _http_error(exc) from None
    await _publish_after_commit(request, envelopes)
    return out


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_push_run(
    run_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.push.run")),
) -> RunOut:
    """A person's act, and final. Waiting targets are cancelled; installs in
    flight finish and are still recorded."""
    try:
        async with org_session(ctx.org_id, engine=_engine(request), clients=ctx.clients) as session:
            run = await runs_service.cancel_run(
                session, ctx.org_id, run_id=run_id, actor_id=ctx.user_id
            )
            out = await _run_out(session, ctx.org_id, run)
    except PushError as exc:
        raise _http_error(exc) from None
    return out
