from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import AuthContext, require_permission
from app.core.client_address import client_address
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.services import enrollment as svc
from app.services.client_sites import NotAcceptingNewRows, SiteNotFound

tokens_router = APIRouter(prefix="/enrollment-tokens", tags=["enrollment"])
agents_router = APIRouter(prefix="/agents", tags=["enrollment"])
enroll_router = APIRouter(tags=["enrollment"])


class TokenCreateRequest(BaseModel):
    """What a caller must decide before this platform mints a bootstrap
    credential.

    `max_uses` IS REQUIRED AND CAPPED, AND IT USED TO BE NEITHER. It read
    `int | None = Field(default=None, ge=1)`, and `None` means UNLIMITED at
    `services/enrollment.py::enroll` -- so the one field an operator could
    leave alone was the field that made the credential unbounded. A holder of
    such a token enrols machines into this organisation until it expires: each
    one gets a device certificate, appears in the fleet list, submits inventory
    the console believes, and becomes something an operator may later target
    with a signed job whose params can carry a secret.

    `docs/modules/agent-distribution/spec-managed-distribution.md` §3.2 called
    an unbounded token "a long-lived shared secret wearing a shorter name", and
    §3.5 recorded leaving this route alone as **found and not fixed**, because
    that module could not change a kernel default under a distribution feature.
    That was the right call there. It is not a reason to keep it.

    THERE IS NO LONGER A WAY TO SAY "UNLIMITED" ON THIS ROUTE. A body that
    omits `max_uses`, or sends `null`, is a 422 naming the field -- which is a
    refusal a human reads on the screen that asked, rather than a rejection at
    an endpoint hours later. `expires_in_hours` keeps its bounds unchanged
    (1..2160): it was already required-with-a-default and already capped, and
    tightening a shipped ceiling is a separate decision from closing an open
    one.

    `site_id` IS REQUIRED (MSP clients spec §1.5, §3.2): the token is minted
    for one site, and a machine enrolled with it lands in that site and its
    client. The client is derived from the site, never sent: a body that named
    both could name a pair that does not belong together.
    """

    name: str = Field(default="", max_length=200)
    expires_in_hours: int = Field(default=72, ge=1, le=24 * 90)
    max_uses: int = Field(
        ge=1,
        le=svc.MAX_TOKEN_USES,
        description=(
            "How many machines this token may enrol. Required, and capped at "
            f"{svc.MAX_TOKEN_USES}: there is no unlimited enrollment token."
        ),
    )
    site_id: UUID = Field(
        description=(
            "The site a machine enrolled with this token lands in; its client is the "
            "site's client"
        ),
    )


class TokenCreatedResponse(BaseModel):
    id: UUID
    token: str  # plaintext, shown exactly once
    name: str
    expires_at: datetime
    #: Never null on anything this route mints. `TokenListItem.max_uses` is
    #: still nullable, because rows issued before the bound existed are
    #: grandfathered rather than rewritten.
    max_uses: int
    #: Where a machine enrolled with this token lands (MSP clients spec §1.5).
    client_id: UUID
    site_id: UUID


class TokenListItem(BaseModel):
    """One issued bootstrap credential, WITHOUT the credential.

    The plaintext is served exactly once, by `TokenCreatedResponse`, and only
    `sha256(secret)` is stored -- so there is nothing here to mask and nothing
    to re-serve. `test_token_list_never_returns_secrets` pins that.
    """

    id: UUID
    name: str
    expires_at: datetime
    #: `null` MEANS UNLIMITED, AND ONLY A GRANDFATHERED ROW CAN SAY IT.
    #: `POST /enrollment-tokens` cannot mint one any more; `enroll()` still
    #: honours the ones already issued, because re-reading a stored NULL as a
    #: number would fail at the endpoint as an indistinguishable 403.
    #: `TokenListResponse.unlimited_count` is how the console finds them.
    max_uses: int | None
    use_count: int
    #: Where a machine enrolled with this token lands (MSP clients spec §1.5).
    client_id: UUID
    site_id: UUID
    #: Soft, and permanent. A revoked token is never deleted -- see
    #: `services/enrollment.py::list_tokens` for why the row has to outlive the
    #: credential -- so this is the flag the default listing filters on.
    revoked: bool


class TokenListResponse(BaseModel):
    """An org's enrollment tokens, with revoked ones excluded by default.

    `revoked_count` IS THE POINT OF THE ENVELOPE. Hiding a revocation is only
    acceptable while the console can say how many were hidden; a bare list
    cannot carry that number, which is the same reason `DeviceGroupListResponse`
    and `CandidateListResponse` are wrapped.

    DELIBERATELY NOT AN AI READ TOOL. See the note in
    `tests/test_ai_tool_surface.py`: this route enumerates a tenant's live
    bootstrap credentials -- their names, lifetimes and how many redemptions
    each has left. It is well-formed enough to be one, and registering it would
    hand an automated actor the map of every credential that can put a new
    machine into the fleet.
    """

    items: list[TokenListItem]
    #: Tokens matching the request -- the LIVE ones unless `include_revoked`
    #: was set -- BEFORE `limit`/`offset`. This is the only thing that tells a
    #: console a short page from the end of the list.
    total: int
    #: Revoked tokens in this org, counted whether or not they are in `items`.
    revoked_count: int
    #: STILL-REDEEMABLE tokens with NO USE LIMIT: `max_uses IS NULL`, not
    #: revoked, not expired. Nothing can mint one any more, so this number only
    #: ever falls -- but until it is zero the organisation holds credentials
    #: that enrol an unbounded number of machines, and a console that cannot
    #: say so is the screen that let them be created.
    unlimited_count: int


class EnrollRequest(BaseModel):
    """The agent's enrollment request.

    `capabilities` is a STRICTLY ADDITIVE field (C4): `agents.capabilities` has
    existed since the kernel migration and nothing has ever written it, while
    `docs/modules/ipam/spec.md` §5.3 claims "it is written at enrollment by the
    agent". This closes that gap without making advertisement a prerequisite for
    anything.

    Compatibility, both directions:

    * **Old agent -> new server.** The field defaults to `{}`, which is exactly
      the column default and exactly what is stored today. IPAM's dispatcher
      already tolerates an empty map by dispatching anyway and reading a
      `refused` job as the answer, so nothing changes for a fleet that never
      sends it.
    * **New agent -> old server.** An older `EnrollRequest` is a plain Pydantic
      model with default `extra` behaviour ("ignore"), so an unknown
      `capabilities` key is dropped and enrollment still succeeds. That is the
      direction nobody checks, and it is an outage if it is ever wrong:
      `test_enrollment.py` pins the tolerance, so a future `extra="forbid"`
      goes red there rather than in the field.

    The value is an ADVERTISEMENT, not an authorization. Nothing may grant a
    capability on the strength of it: what an agent will actually run is
    decided by the signed-job runner on the endpoint, and refusal is the answer
    for a job type it does not implement.
    """

    #: The enrollment token. Every agent before 1.6.0 sends it, and still does.
    token: str | None = None
    #: The install link's code (spec 2026-09-28 §1.4) -- ADDITIVE: older agents
    #: never send it, and a body carrying it takes the link path instead of the
    #: token path. No pattern here on purpose: a malformed code is refused by
    #: the service with the same 404 as a wrong one, not with a 422 that says
    #: which.
    link_code: str | None = Field(default=None, max_length=64)
    csr_pem: str
    device_facts: svc.DeviceFacts
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_credential(self) -> "EnrollRequest":
        if (self.token is None) == (self.link_code is None):
            raise ValueError(tr("send exactly one of token or link_code"))
        return self


class EnrollResponse(BaseModel):
    device_id: UUID
    cert_pem: str
    chain_pem: str
    ca_bundle_pem: str
    gateway_url: str
    job_signing_public_key_pem: str


@tokens_router.post("", status_code=status.HTTP_201_CREATED, response_model=TokenCreatedResponse)
async def create_token(
    body: TokenCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("enrollment.manage")),
) -> TokenCreatedResponse:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            # The site is loaded in THIS session, so RLS hides another
            # organisation's and one of a client outside the caller's scope:
            # the same 404 for both.
            issued = await svc.create_token(
                session,
                org_id=ctx.org_id,
                site_id=body.site_id,
                created_by=ctx.user_id,
                name=body.name,
                expires_in_hours=body.expires_in_hours,
                max_uses=body.max_uses,
            )
        except SiteNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such site")) from None
        except NotAcceptingNewRows as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, exc.detail) from None
    return TokenCreatedResponse(
        id=issued.id,
        token=issued.token,
        name=issued.name,
        expires_at=issued.expires_at,
        max_uses=issued.max_uses,
        client_id=issued.client_id,
        site_id=issued.site_id,
    )


@tokens_router.get("", response_model=TokenListResponse)
async def list_tokens(
    request: Request,
    ctx: AuthContext = Depends(require_permission("enrollment.manage")),
    include_revoked: bool = Query(
        default=False,
        description="Include revoked tokens, which are excluded by default",
    ),
    unlimited_only: bool = Query(
        default=False,
        description=(
            "Only tokens that are still redeemable and carry no use limit -- "
            "the grandfathered rows `unlimited_count` counts"
        ),
    ),
    limit: int = Query(default=svc.TOKEN_PAGE_DEFAULT, ge=1, le=svc.TOKEN_PAGE_MAX),
    offset: int = Query(default=0, ge=0),
) -> TokenListResponse:
    """This org's enrollment tokens, newest first, revoked ones excluded.

    THE DEFAULT CHANGED, THE ROWS DID NOT. Revoked tokens accumulated in this
    response forever, and an operator asked for a way to clear them. Nothing is
    deleted -- `services/enrollment.py::list_tokens` records why a revocation
    has to outlive the credential -- so this takes the shape
    `GET /device-groups?include_archived` already uses: hidden by default,
    counted in `revoked_count`, returned in full on request.

    AND IT IS PAGED, WHICH IT WAS NOT. Hiding revoked rows bounded nothing:
    the LIVE ones accumulate too -- one per device onboarded, one per
    provisioning run -- so this route returned every token row an org had ever
    issued, on the one page an operator sits on while onboarding a fleet.
    `limit`/`offset` with a capped `limit` and a `total` that ignores both, the
    same contract `GET /patch/patches`, `GET /devices` and `GET /audit` take.
    `revoked_count` stays an org-wide aggregate for the same reason it always
    was: it is what the "Show revoked" control offers to reveal, and a count
    computed from the page would understate what it is hiding.

    AND IT NOW COUNTS THE UNBOUNDED ONES, AND CAN LIST JUST THEM.
    `max_uses` is required and capped on `POST /enrollment-tokens` as of this
    change, so no new token is unlimited -- but the rows minted while it
    defaulted to null are still redeemable, and `enroll()` still honours them
    (deliberately: see the note on the guarded UPDATE). A grandfathered
    credential nobody can find is a grandfathered credential nobody revokes, so
    `unlimited_count` says how many exist and `unlimited_only=true` lists
    exactly those. Both use the same predicate, so the number and the list
    cannot disagree.

    GATE UNCHANGED: `enrollment.manage`, tenant-scoped. Paging is not a
    permission decision and this route's is not being reopened here.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        page = await svc.list_tokens(
            session,
            org_id=ctx.org_id,
            include_revoked=include_revoked,
            unlimited_only=unlimited_only,
            limit=limit,
            offset=offset,
        )
        return TokenListResponse(
            items=[
                TokenListItem(
                    id=r.id,
                    name=r.name,
                    expires_at=r.expires_at,
                    max_uses=r.max_uses,
                    use_count=r.use_count,
                    client_id=r.client_id,
                    site_id=r.site_id,
                    revoked=r.revoked,
                )
                for r in page.rows
            ],
            total=page.total,
            revoked_count=page.revoked_count,
            unlimited_count=page.unlimited_count,
        )


@tokens_router.post("/{token_id}/revoke")
async def revoke_token(
    token_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("enrollment.manage")),
) -> dict[str, bool]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        found = await svc.revoke_token(
            session, org_id=ctx.org_id, actor_id=ctx.user_id, token_id=token_id
        )
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such token"))
    return {"revoked": True}


@agents_router.post("/{device_id}/revoke")
async def revoke_agent(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("enrollment.manage")),
) -> dict[str, bool]:
    """Revoke an enrolled agent's certificate and drop its live connection.
    Sets the DB revocation flag (the gateway's admission gate), updates the CA
    CRL, and publishes a revoke event the gateway consumes to disconnect the
    device immediately."""
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        serial = await svc.revoke_agent(
            session, org_id=ctx.org_id, actor_id=ctx.user_id, device_id=device_id
        )
    if serial is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such agent"))
    # DB flag is committed; now update the CRL and drop any live connection.
    await state.ca.revoke(serial)
    from app.gateway.dispatch import publish_revoke

    await publish_revoke(state.redis, device_id)
    return {"revoked": True}


@enroll_router.post(
    "/enroll/v1", status_code=status.HTTP_201_CREATED, response_model=EnrollResponse
)
async def enroll_device(body: EnrollRequest, request: Request) -> EnrollResponse:
    state = request.app.state
    if body.link_code is not None:
        try:
            result = await svc.enroll_via_link(
                admin_engine=state.admin_engine,
                app_engine=state.app_engine,
                ca=state.ca,
                signer=state.signer,
                gateway_url=state.settings.gateway_url,
                link_code=body.link_code,
                source=client_address(request, state.trusted_proxies),
                csr_pem=body.csr_pem,
                facts=body.device_facts,
                capabilities=body.capabilities,
            )
        except svc.LinkNotFound:
            # FastAPI's own 404 body, so a refused link reads exactly like a
            # path that does not exist.
            raise HTTPException(  # i18n-exempt: must equal FastAPI's own 404 body
                status.HTTP_404_NOT_FOUND, "Not Found"
            ) from None
        except svc.LinkRateLimited as exc:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                tr("too many computers joined through this link in the last hour; try again later"),
                headers={"Retry-After": str(exc.retry_after_s)},
            ) from None
        return EnrollResponse(**result.__dict__)

    assert body.token is not None  # the validator guarantees exactly one
    try:
        result = await svc.enroll(
            admin_engine=state.admin_engine,
            app_engine=state.app_engine,
            ca=state.ca,
            signer=state.signer,
            gateway_url=state.settings.gateway_url,
            token=body.token,
            csr_pem=body.csr_pem,
            facts=body.device_facts,
            capabilities=body.capabilities,
        )
    except svc.EnrollmentError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, tr("enrollment rejected")) from None
    return EnrollResponse(**result.__dict__)
