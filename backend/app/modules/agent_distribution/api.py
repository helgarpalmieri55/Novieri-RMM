"""Agent release routes.

WHY THE AUTHORIZATION CHECKS MATTER MORE HERE THAN ANYWHERE ELSE IN THIS TREE.
`agent_releases` carries no `org_id` (`docs/modules/agent-distribution/spec.md`
§4.2), so unlike every tenant table there is no RLS policy underneath these
routes failing closed when an application-level check is forgotten. The
`require_permission` on each handler is the whole control. `tests/
test_agent_release_api.py` leads with those cases for that reason, and
`tests/test_agent_release_model.py` pins the missing `org_id` as deliberate so
nobody relaxes a check on the strength of tenancy that is not there.

AND SINCE MIGRATION 0070 THE WRITE CODE IS THE PLATFORM OPERATOR'S, NOT A
TENANT'S. Follow the paragraph above to its end: with no `org_id` on the table,
`agent.release.manage` is not scoped by tenant at all, so whoever holds it
publishes for EVERY tenant at once -- the bytes every endpoint on the
deployment downloads and executes. 0060 seeded it to `org-admin`, which on a
multi-tenant deployment meant any customer's own administrator could replace
every other customer's agent binary. 0070 moves it to `super-admin` and adds it
to `app/core/rbac.PLATFORM_OPERATOR_CODES`, so no org-scoped console surface
can grant it back. `agent.release.read` deliberately stays with `org-admin`:
listing the catalogue and minting an install command is what installing an
agent IS, and it writes nothing.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.audit import audit_service
from app.core.http import read_bounded_body, reject_oversized_content_length
from app.core.i18n import N_, tr, tr_marked
from app.core.storage import ObjectStore, agent_release_suffix
from app.core.tenancy import org_session
from app.models.core import AgentRelease
from app.modules.agent_distribution import managed
from app.modules.agent_distribution import service as svc
from app.modules.agent_distribution.schemas import (
    DeploymentIntegrityOut,
    DeploymentPackageOut,
    DeploymentStepOut,
    DeploymentTokenOut,
    DeploymentTokenRequest,
    InstallCommandOut,
    InstallCommandRequest,
    ReleaseCreateRequest,
    ReleaseOut,
    VersionUnpublishedOut,
)
from app.services import enrollment as enrollment_svc

router = APIRouter(prefix="/agent", tags=["agent-distribution"])

#: The largest artefact this accepts. `build.sh` emits a static Go binary in the
#: low tens of MB; 128 MiB leaves room for a future bundle without letting a
#: `agent.release.manage` holder buffer something unbounded in the API process.
_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024


def _object_store(request: Request) -> ObjectStore:
    """Lazily build (and cache) the object store, mirroring the deploy module.

    Construction touches object storage to ensure the bucket exists, so it stays
    out of app startup: an object store that is down should fail the routes that
    need it, not the whole API.
    """
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(request.app.state.settings)
        request.app.state.object_store = store
    assert isinstance(store, ObjectStore)
    return store


def _out(release: AgentRelease) -> ReleaseOut:
    return ReleaseOut(
        id=release.id,
        version=release.version,
        os=release.os,
        arch=release.arch,
        size_bytes=release.size_bytes,
        sha256=release.sha256,
        signed=release.signed,
        notes=release.notes,
        published_at=release.published_at,
        # An empty digest means the bytes never arrived. The console needs to
        # tell those rows apart: a release with no artefact cannot produce an
        # install command, and rendering it as though it could is the "looks
        # finished and lies" failure the Devices page was built to avoid.
        has_artifact=bool(release.sha256),
        channel=release.channel,
        release_signed=bool(release.release_signature),
    )


@router.get("/releases", response_model=list[ReleaseOut])
async def list_releases_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("agent.release.read")),
) -> list[ReleaseOut]:
    """Every published agent artefact, newest first.

    The catalogue is GLOBAL, not per-tenant (`agent_releases` carries no
    `org_id`, spec §4.2): the bytes are identical for every org, so a per-org
    copy would multiply storage for no isolation. `tests/
    test_agent_release_api.py::test_the_catalogue_is_shared_across_orgs_by_design`
    pins what follows from that.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return [_out(release) for release in await svc.list_releases(session)]


@router.post(
    "/releases", status_code=status.HTTP_201_CREATED, response_model=ReleaseOut
)
async def create_release_route(
    body: ReleaseCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("agent.release.manage")),
) -> ReleaseOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            release = await svc.create_release(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                version=body.version,
                os_name=body.os,
                arch=body.arch,
                signed=body.signed,
                notes=body.notes,
            )
        except ValueError as exc:
            # `agent_release_suffix` refused the version. A 422 naming it beats
            # a 500: the operator typed this and can fix it.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except IntegrityError as exc:
            # The (version, os, arch) uniqueness constraint. Republishing the
            # same triple is a thing an operator does by accident -- re-running
            # a publish script, or two people publishing the same build -- and
            # it deserves an answer that says so rather than a 500 carrying a
            # database traceback.
            #
            # NOTE the constraint is GLOBAL: in a multi-tenant deployment this
            # 409 can be caused by ANOTHER tenant having published that version
            # (see tests/test_agent_release_api.py::
            # test_the_catalogue_is_shared_across_orgs_by_design), which the
            # message cannot say without leaking that tenant's activity.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr("a release already exists for this version, OS and architecture"),
            ) from exc
        return _out(release)


@router.post("/releases/{release_id}/artifact", response_model=ReleaseOut)
async def upload_artifact_route(
    release_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("agent.release.manage")),
) -> ReleaseOut:
    # Cheapest check first, before any database or object-store work. The
    # Content-Length is client-supplied and may be absent or wrong, so the bytes
    # actually read are bounded as well -- a bare `await request.body()` lets
    # any holder of this permission buffer an unbounded request in the API
    # process before the handler sees a byte.
    reject_oversized_content_length(request, _MAX_ARTIFACT_BYTES)
    data = await read_bounded_body(request, _MAX_ARTIFACT_BYTES)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, tr("artifact body is empty"))
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            release = await svc.store_artifact(
                session,
                _object_store(request),
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                release_id=release_id,
                data=data,
            )
        except NoResultFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such agent release")) from exc
        return _out(release)


_IN_USE = N_(
    "an active agent update rollout still uses this release; complete or cancel it first"
)


async def _unpublish(
    request: Request, ctx: AuthContext, release_ids: list[UUID]
) -> tuple[svc.UnpublishResult, list[str]]:
    """Rows first, in one transaction; files after it commits.

    The order is the point: a commit that fails leaves every row and every file
    as it was, and a file that cannot be deleted afterwards is reported rather
    than resurrecting a row for it."""
    async with AsyncSession(request.app.state.admin_engine) as admin_session:
        try:
            result = await svc.unpublish(
                admin_session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                release_ids=release_ids,
            )
        except svc.ReleaseInUse as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, tr_marked(_IN_USE)) from exc
        except NoResultFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such agent release")) from exc
        await admin_session.commit()

    keys = [agent_release_suffix(r.version, r.os, r.arch) for r in result.releases]
    left: list[str] = []
    try:
        store = _object_store(request)
    except Exception:  # noqa: BLE001 -- storage down: the rows are gone, the files are reported
        return result, keys
    for key in keys:
        try:
            await store.delete_agent_release(key)
        except Exception:  # noqa: BLE001 -- reported, and the rows are already gone
            left.append(key)
    return result, left


@router.delete("/releases/{release_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_release_route(
    release_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("agent.release.manage")),
) -> None:
    await _unpublish(request, ctx, [release_id])


@router.delete("/releases/versions/{version}", response_model=VersionUnpublishedOut)
async def delete_version_route(
    version: str,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("agent.release.manage")),
) -> VersionUnpublishedOut:
    """Every platform's package of one version, all or none.

    Replaces a loop of per-package deletes in the console, which could stop
    half way through a version when one package was refused."""
    async with AsyncSession(request.app.state.admin_engine) as admin_session:
        ids = list(
            (
                await admin_session.execute(
                    select(AgentRelease.id).where(AgentRelease.version == version)
                )
            ).scalars()
        )
    if not ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no release has that version"))
    result, left = await _unpublish(request, ctx, ids)
    return VersionUnpublishedOut(
        version=version,
        removed=len(result.releases),
        history_cleared=result.history_cleared,
        files_not_removed=left,
    )


@router.post("/releases/{release_id}/install-command", response_model=InstallCommandOut)
async def install_command_route(
    release_id: UUID,
    body: InstallCommandRequest,
    request: Request,
    # `enrollment.manage` IS THE DECLARED CODE, not `agent.release.read`, and
    # the choice is deliberate. This route mints an enrollment token, which
    # mints a device identity inside the tenant -- the strictly more privileged
    # half of what it does. Declaring the weaker code and checking the stronger
    # one elsewhere would report the weaker one to `route_permission`'s
    # completeness sweep, which reads a route's gate by reflection and finds
    # exactly one dependency.
    #
    # A holder of `enrollment.manage` can already enrol a device by hand, so
    # this grants no capability they lacked; it only makes it convenient.
    ctx: AuthContext = Depends(require_permission("enrollment.manage")),
) -> InstallCommandOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            release = await svc.get_release(session, release_id)
        except NoResultFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such agent release")) from exc
        if not release.sha256:
            # No bytes were ever uploaded. Handing out a token and a URL for an
            # object that does not exist produces a 404 on the endpoint, at the
            # far end of a copy/paste, where it is most expensive to diagnose.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr(
                    "this release has no artifact yet; upload one before generating an install "
                    "command"
                ),
            )
        issued = await enrollment_svc.create_token(
            session,
            org_id=ctx.org_id,
            created_by=ctx.user_id,
            name=f"agent install: {release.version} {release.os}/{release.arch}",
            expires_in_hours=body.expires_in_hours,
            # SINGLE USE. The token is a literal in a command an operator
            # pastes, so it reaches their shell history; a token already spent
            # by the time that matters is a weak credential by design.
            max_uses=1,
        )
        url = _object_store(request).presign_agent_release_get(
            release.version,
            release.os,
            release.arch,
            expires_in=svc.DOWNLOAD_TTL_S,
        )
        # The control-plane base the agent enrolls against. Derived from the
        # request the operator is already authenticated on -- the console and
        # the enrollment API are the same host in every supported deployment
        # (`core/storage.py`, the Traefik router). It is a NON-SECRET argument
        # the installer records in the service's environment, and the operator
        # can see and correct it in the command; the previous command passed no
        # URL at all, which left the token with nowhere to be spent.
        enroll_url = str(request.base_url).rstrip("/")
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="agent.install_command_generated",
            target_type="agent_release",
            target_id=str(release.id),
            # The token ID, never the token. Pairing this with the
            # `enrollment.token_created` event the mint above writes is what
            # lets an auditor answer "who was handed the ability to enrol a
            # device, and for which artefact".
            metadata={"token_id": str(issued.id), "version": release.version},
        )
        return InstallCommandOut(
            release=_out(release),
            token=issued.token,
            token_expires_at=issued.expires_at,
            download_url=url,
            download_expires_in_s=svc.DOWNLOAD_TTL_S,
            sha256=release.sha256,
            command=svc.install_command(
                os_name=release.os,
                download_url=url,
                sha256=release.sha256,
                token=issued.token,
                enroll_url=enroll_url,
            ),
        )


# --------------------------------------------------------------------------
# Managed distribution -- GPO and Intune.
# `docs/modules/agent-distribution/spec-managed-distribution.md`
#
# TWO ROUTES, TWO PERMISSIONS, AND THAT SPLIT IS THE DESIGN. A GPO software
# distribution point is read by every machine account in the domain by
# construction, so a deployment package carrying a credential would be a
# permanent fleet-wide enrolment secret sitting on a share -- the exact thing
# `agent/packaging/README.md` forbids ("One image build and the whole fleet
# shares one token"). The package route therefore mints nothing and the token
# route serves no artefact, and neither can be made to do the other's job by a
# caller. `tests/test_agent_managed_deployment.py` leads with the negative.
# --------------------------------------------------------------------------


@router.get(
    "/releases/{release_id}/deployment-package", response_model=DeploymentPackageOut
)
async def deployment_package_route(
    release_id: UUID,
    request: Request,
    channel: str = Query(default="gpo"),
    # `agent.release.read`, NOT `enrollment.manage`, and that is the half of the
    # split that has to be checked rather than asserted: this response is inert,
    # so an operator who may not mint credentials may still collect the artefact
    # and the staging procedure. Gating it on the stronger code would make the
    # separation cosmetic.
    ctx: AuthContext = Depends(require_permission("agent.release.read")),
) -> DeploymentPackageOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            release = await svc.get_release(session, release_id)
        except NoResultFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such agent release")) from exc
        if not release.sha256:
            # Same reasoning as the install command: staging an object that does
            # not exist fails at the far end of a copy, on somebody's file
            # share, where it is most expensive to diagnose.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr(
                    "this release has no artifact yet; upload one before building a deployment "
                    "package"
                ),
            )
        url = _object_store(request).presign_agent_release_get(
            release.version,
            release.os,
            release.arch,
            expires_in=svc.DOWNLOAD_TTL_S,
        )
        try:
            package = managed.deployment_package(
                channel=channel,
                os_name=release.os,
                arch=release.arch,
                sha256=release.sha256,
                download_url=url,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="agent.deployment_package_generated",
            target_type="agent_release",
            target_id=str(release.id),
            # No token id here, because there is no token. That absence is the
            # audit trail's own record of the split above.
            metadata={"version": release.version, "channel": channel},
        )
        return DeploymentPackageOut(
            release=_out(release),
            channel=package.channel,
            download_url=url,
            download_expires_in_s=svc.DOWNLOAD_TTL_S,
            sha256=release.sha256,
            staging=[
                DeploymentStepOut(title=s.title, detail=s.detail, command=s.command)
                for s in package.staging
            ],
            install_command=package.install_command,
            enrol_command=package.enrol_command,
            integrity=DeploymentIntegrityOut(
                sha256=package.integrity.sha256,
                verify_command=package.integrity.verify_command,
                verified_at_install=package.integrity.verified_at_install,
                notes=list(package.integrity.notes),
            ),
        )


@router.post(
    "/releases/{release_id}/deployment-token", response_model=DeploymentTokenOut
)
async def deployment_token_route(
    release_id: UUID,
    body: DeploymentTokenRequest,
    request: Request,
    # `enrollment.manage` for the same reason the install command takes it: this
    # mints a credential that mints device identities inside the tenant, which
    # is the strictly more privileged half. Here it is more privileged again --
    # the token is MULTI-USE by necessity (a fleet cannot race for one use), so
    # what is being handed over is the ability to enrol up to `max_uses`
    # machines rather than one.
    ctx: AuthContext = Depends(require_permission("enrollment.manage")),
) -> DeploymentTokenOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            release = await svc.get_release(session, release_id)
        except NoResultFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such agent release")) from exc
        if body.channel not in managed.CHANNELS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                tr("unknown distribution channel {body_channel}", body_channel=repr(body.channel)),
            )
        issued = await enrollment_svc.create_token(
            session,
            org_id=ctx.org_id,
            created_by=ctx.user_id,
            name=(
                f"managed deployment ({body.channel}): {release.version} "
                f"{release.os}/{release.arch}"
                + (f" -- {body.note}" if body.note else "")
            ),
            expires_in_hours=body.expires_in_hours,
            # FINITE, ALWAYS, and it came from the caller rather than from a
            # default here. `max_uses=None` on this table means unlimited; a
            # default in this position would be this module choosing the blast
            # radius on the operator's behalf, and choosing it once for every
            # rollout regardless of size.
            max_uses=body.max_uses,
        )
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="agent.deployment_token_generated",
            target_type="agent_release",
            target_id=str(release.id),
            # The BOUNDS are in the log, not just the fact. "Who was handed the
            # ability to enrol devices" is a question with a number in it once
            # the credential is multi-use, and an auditor who cannot see the
            # number cannot answer it.
            metadata={
                "token_id": str(issued.id),
                "version": release.version,
                "channel": body.channel,
                "max_uses": body.max_uses,
                "expires_in_hours": body.expires_in_hours,
            },
        )
        return DeploymentTokenOut(
            token=issued.token,
            token_id=issued.id,
            token_expires_at=issued.expires_at,
            max_uses=body.max_uses,
            channel=body.channel,
            blast_radius=list(
                managed.blast_radius(
                    channel=body.channel,
                    max_uses=body.max_uses,
                    hours=body.expires_in_hours,
                )
            ),
        )
