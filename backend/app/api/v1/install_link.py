"""The install link's settings (spec 2026-09-28 §1.1).

GATED `enrollment.link.manage` ON EVERY VERB, INCLUDING THE READ: the settings
say which networks may enrol machines into this organisation, which is not a
fact a technician needs.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, require_permission
from app.core.tenancy import org_session
from app.modules.agent_distribution import msi as msi_svc
from app.modules.agent_distribution.api import _object_store
from app.services import install_link as svc

router = APIRouter(prefix="/install-link", tags=["install-link"])


class InstallLinkOut(BaseModel):
    """This organisation's install link, WITHOUT the link itself.

    The full link is shown once, by the response that created its code
    (`InstallLinkSaved.url`); after that only its hash exists.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(description="On right now: switched on and not past its switch-off time")
    has_code: bool = Field(description="Whether a link has ever been generated")
    networks: list[str] = Field(description="The networks a request must come from")
    disable_at: datetime | None = Field(description="When the link switches itself off")
    hourly_limit: int = Field(description="Most machines that may join per rolling hour")
    enabled_at: datetime | None = Field(description="When it was switched on, while it is on")
    joined_since_enabled: int = Field(description="Machines that joined since it was switched on")


class InstallLinkSaved(InstallLinkOut):
    """The settings after a change, and the link if this change created one."""

    url: str | None = Field(
        description="The full link. Present only in the response that generated its code"
    )


class InstallLinkUpdate(BaseModel):
    """The settings the console's Install link panel sends.

    THE BOUNDS ARE NOT DECLARED HERE, ON PURPOSE. The panel shows a 422's
    `detail` verbatim, so every refusal an operator can cause must be one plain
    sentence -- and a Pydantic bound answers with a list of error objects that
    name wire fields. `install_link.save` checks the network count, the hourly
    limit's range and the switch-off time (its zone as well as its future) and
    raises `InstallLinkError` with a sentence, which the route returns as a
    string `detail`.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    networks: list[str] = Field(default_factory=list)
    disable_at: datetime | None = None
    hourly_limit: int = svc.DEFAULT_HOURLY_LIMIT


class InstallLinkMsiRequest(BaseModel):
    """Download the Group Policy MSI (GPO MSI spec 2026-09-30 §2.1).

    THE CODE TRAVELS IN THIS BODY, NEVER IN THE URL, which access logs keep.
    The server holds only the code's hash, so the administrator supplies the
    code -- from the link shown when it was generated -- and the server checks
    it against that hash before building anything.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=64, description="The install link's code: the part after /i/")
    arch: Literal["amd64", "arm64"] = Field(description="The Windows architecture of the MSI")


def _out(view: svc.LinkView) -> InstallLinkOut:
    return InstallLinkOut(**vars(view))


def _saved(view: svc.LinkView, url: str | None) -> InstallLinkSaved:
    return InstallLinkSaved(**vars(view), url=url)


def _base(request: Request) -> str:
    return svc.console_base(
        request.app.state.settings.public_console_url, request.headers.get("host")
    )


@router.get("", response_model=InstallLinkOut)
async def get_install_link(
    request: Request,
    ctx: AuthContext = Depends(require_permission("enrollment.link.manage")),
) -> InstallLinkOut:
    """The install link's settings: whether it is on, where it answers, its limit."""
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        return _out(await svc.view(session, ctx.org_id, datetime.now(UTC)))


@router.put("", response_model=InstallLinkSaved)
async def put_install_link(
    body: InstallLinkUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("enrollment.link.manage")),
) -> InstallLinkSaved:
    now = datetime.now(UTC)
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            minted = await svc.save(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                enabled=body.enabled,
                networks=body.networks,
                disable_at=body.disable_at,
                hourly_limit=body.hourly_limit,
                now=now,
                trusted_proxies=request.app.state.trusted_proxies,
            )
        except svc.InstallLinkError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        view = await svc.view(session, ctx.org_id, now)
    return _saved(view, svc.link_url(_base(request), minted) if minted else None)


@router.post("/regenerate", response_model=InstallLinkSaved)
async def regenerate_install_link(
    request: Request,
    ctx: AuthContext = Depends(require_permission("enrollment.link.manage")),
) -> InstallLinkSaved:
    now = datetime.now(UTC)
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        code = await svc.regenerate(session, org_id=ctx.org_id, actor_id=ctx.user_id, now=now)
        view = await svc.view(session, ctx.org_id, now)
    return _saved(view, svc.link_url(_base(request), code))


@router.post(
    "/msi",
    response_class=Response,
    responses={200: {"content": {msi_svc.MSI_MEDIA_TYPE: {}}}},
)
async def download_install_link_msi(
    body: InstallLinkMsiRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("enrollment.link.manage")),
) -> Response:
    """The MSI for Group Policy software installation: the published agent and
    this organisation's install link, built for this request and not kept.

    The console address is ITS_PUBLIC_CONSOLE_URL and nothing else -- never
    `_base(request)`'s Host fallback: the file goes to every computer in the
    domain. How long the request holds a transaction and a thread is said in
    `msi_svc`'s docstring."""
    state = request.app.state
    try:
        built = await msi_svc.msi_for_link(
            lambda: org_session(ctx.org_id, engine=state.app_engine),
            _object_store(request),
            org_id=ctx.org_id,
            actor_id=ctx.user_id,
            code=body.code,
            arch=body.arch,
            console=state.settings.public_console_url,
            public_key=state.settings.agent_release_public_key,
            now=datetime.now(UTC),
        )
    except msi_svc.MsiRefused as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        raise HTTPException(exc.status, exc.message, headers=headers) from None
    return Response(
        content=built.data,
        media_type=msi_svc.MSI_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{built.filename}"',
            "X-Content-SHA256": built.sha256,
            "Cache-Control": "no-store",
        },
    )
