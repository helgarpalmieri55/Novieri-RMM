from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_all_clients, require_permission
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.rbac import codes_held_anywhere
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job, publish_revoke
from app.models import Agent, Device, SuggestedAssetMatch
from app.modules.ipam import candidates as candidates_svc
from app.modules.remote.capability import can_share_screen
from app.services import device_review as review_svc
from app.services.cert_renewal import cert_state
from app.services.client_sites import NotAcceptingNewRows, SiteNotFound, site_for_new_rows
from app.services.device_list import HOLDER_PERMISSION, DeviceHolder, device_page
from app.services.dns_name import MAX_DNS_NAME, InvalidDnsName, normalise_dns_name

router = APIRouter(prefix="/devices", tags=["devices"])

#: The lifecycle and type vocabularies, shared by the request models below and
#: pinned by 0061's CHECK constraints. Kept together so a value added to one
#: and not the other is visible in a single screenful rather than as a 500 from
#: a constraint an operator has never heard of.
_LIFECYCLES = ("in_service", "stock", "unmanageable", "retired")
_ASSET_TYPES = (
    "computer",
    "server",
    "network",
    "printer",
    "phone",
    "appliance",
    "other",
)
#: Fields the MACHINE owns. On a device with an agent these are overwritten by
#: the next inventory run, so an edit is refused rather than accepted and
#: silently reverted hours later, when nobody connects the two events.
#: `fqdn` and `architecture` joined them 2026-09-29: inventory writes both on
#: every run (`inventory/ingest.py`), so on a managed device they are the
#: machine's too.
_MACHINE_OWNED = frozenset({"hostname", "os_family", "serial", "fqdn", "architecture"})


class AssetFields(BaseModel):
    """What an operator types about an asset, on create and on edit.

    `extra="forbid"` so `source`, `agent_status` or `id` in the body is a 422
    rather than a silently ignored field. `source` in particular is the first
    thing an operator checks when a row looks wrong -- accepting it from the
    caller would let a manual row claim an agent wrote it.
    """

    model_config = ConfigDict(extra="forbid")

    hostname: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    primary_ip: IPvAnyAddress | None = None
    asset_type: str = Field(default="computer", pattern=f"^({'|'.join(_ASSET_TYPES)})$")
    lifecycle: str = Field(default="in_service", pattern=f"^({'|'.join(_LIFECYCLES)})$")
    owner: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    os_family: str | None = Field(default=None, pattern="^(windows|linux|macos)$")
    serial: str | None = Field(default=None, max_length=100)
    asset_tag: str | None = Field(default=None, max_length=100)
    # THE MACHINE'S FULL DNS NAME, typed by a person (design 2026-09-28 §2.3):
    # Windows push is Kerberos only and a ticket is issued for a name, never an
    # address, so a machine that has never run the agent needs one written here.
    # Never derived from reverse DNS (plan W10). An empty string clears it.
    fqdn: str | None = Field(default=None, max_length=MAX_DNS_NAME + 8)
    # THE PROCESSOR A PUSH INSTALLS FOR. Push picks the artefact from the
    # declared OS and architecture and refuses if either is unset (spec §7.2);
    # the agent verifies both on the target before installing. Only the two a
    # push accepts are offered. `null` on an update clears it (the column is
    # NOT NULL with an empty-string default, which `update_asset` maps to).
    architecture: str | None = Field(default=None, pattern="^(amd64|arm64)$")

    @field_validator("fqdn")
    @classmethod
    def _fqdn_is_a_dns_name(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        try:
            return normalise_dns_name(value)
        except InvalidDnsName as exc:
            raise ValueError(str(exc)) from None


class AssetCreateRequest(AssetFields):
    """A record an operator types in.

    `site_id` IS REQUIRED (MSP clients spec §1.5): every device belongs to a
    site, and to the site's client -- which is derived from the site, never
    sent. Not on `AssetUpdateRequest`: a device's client never changes, and
    moving one between its client's sites is not this route's.
    """

    site_id: UUID = Field(
        description="The site the asset is at; its client is the site's client"
    )


class AssetUpdateRequest(AssetFields):
    """Every field optional; only what was SENT is written.

    The two defaulted fields are re-declared as `None` so `exclude_unset` can
    tell "the operator did not mention lifecycle" from "the operator set it to
    in_service" -- without that, every PATCH would quietly reset both.
    """

    asset_type: str | None = Field(default=None, pattern=f"^({'|'.join(_ASSET_TYPES)})$")  # type: ignore[assignment]
    lifecycle: str | None = Field(default=None, pattern=f"^({'|'.join(_LIFECYCLES)})$")  # type: ignore[assignment]


class AssignedToOut(BaseModel):
    """Who a device is assigned to: a person's id and name, nothing more.

    The rest of the person -- email, department, what else they hold -- is the
    People API's to serve under `people.read`, on the page this id links to.
    """

    id: UUID
    display_name: str


class DeviceOut(BaseModel):
    id: UUID
    # NULLABLE SINCE 0061 (asset-inventory spec §2). A switch has a name but no
    # hostname in this sense and no OS family at all; a laptop still in its box
    # has told us neither yet.
    #
    # Declaring these `str` is not a harmless over-tightening: Pydantic raises
    # at SERIALIZATION time, so ONE asset row 500s the device list for the whole
    # organisation. `tests/test_asset_read_paths.py` is that failure, pinned.
    hostname: str | None
    fqdn: str | None
    os_family: str | None
    os_version: str
    architecture: str
    device_group_id: UUID | None
    status: str
    agent_status: str | None
    last_seen_at: datetime | None
    # Certificate lifecycle (docs/modules/agent/cert-renewal.md §6).
    #
    # `agents.cert_not_after` has been stored since enrollment existed and was
    # read by NOTHING -- so a device that fell off the fleet because its
    # certificate lapsed was indistinguishable, from the console, from one that
    # was revoked or simply powered off. It now drives rotation, and these two
    # fields are the operator's view of the same fact: `cert_state` is
    # ok | renewing | expiring | expired, where `renewing` means the platform is
    # already trying and `expiring` means it has been trying for a while.
    #
    # Both are NULLABLE and additive: a Device with no Agent row (never
    # enrolled) reports null, exactly as `agent_status` already does.
    cert_not_after: datetime | None = None
    cert_state: str | None = None
    #: Has this device's agent certificate been REVOKED?
    #:
    #: Additive and defaulted, because the console could not previously say it
    #: at all: `cert_state` is derived from `cert_not_after` alone, so a
    #: revoked certificate rendered as `ok` right up to its natural expiry and
    #: the Certificates screen still offered a Revoke button for it. An
    #: operator who had just revoked a machine saw nothing change.
    #:
    #: `False` for a device with no `agents` row, which has no certificate to
    #: revoke -- `cert_state is None` is what says "never enrolled", and this
    #: field says nothing about that case.
    #:
    #: The row is NEVER deleted when this goes true. `agents.cert_serial` is
    #: the only mapping from an entry in the CA's CRL back to a device and an
    #: org, and the CRL lives outside this database.
    cert_revoked: bool = False
    # Asset fields (asset-inventory spec §2). `lifecycle` is what the operator
    # INTENDS this record to be; whether it is MANAGED is `agent_status` being
    # non-null, which is a join and is deliberately never stored.
    lifecycle: str = "in_service"
    asset_type: str = "computer"
    source: str = "agent"
    #: WHO, IF ANYONE, STOOD BEHIND THIS ROW: `agent | operator | promoted |
    #: sweep`. Since 2026-08-26 a sweep records every host it finds, so the
    #: fleet list carries rows nobody chose; this is what lets the console show
    #: which is which instead of presenting them all as equally vouched-for.
    provenance: str = "agent"
    description: str | None = None
    primary_ip: str | None = None
    owner: str | None = None
    location: str | None = None
    #: WHO HOLDS THIS DEVICE (operator decision 2026-09-23: People folded into
    #: Devices). The person its `device_person` link names -- set by hand or
    #: resolved from who signs in -- as `{id, display_name}`.
    #:
    #: ADDITIVE AND NULLABLE, and null means one of three things, so read it
    #: with that in mind: nobody is assigned; the caller does not hold
    #: `people.read` (the list is gated `devices.read`, and a person's name is
    #: not a device fact); or the route did not resolve it. ONLY `GET /devices`
    #: resolves it. The device page asks `GET /people/devices/{id}`, which also
    #: says whether a person or inventory set it.
    #:
    #: Distinct from `owner`, which is free text an operator typed on the asset
    #: record and links to no one.
    assigned_to: AssignedToOut | None = None
    #: CAN THIS DEVICE'S AGENT SHARE ITS SCREEN? Whether it reported the
    #: `remote.screen.open` capability -- the fact a screen session is refused
    #: on, 409 "agent too old to share its screen", decided by the same
    #: `remote.capability.can_share_screen`. The console reads it to warn
    #: before asking rather than after the refusal.
    #:
    #: ADDITIVE AND NULLABLE: null when the device has no agent at all (there
    #: is nothing to be too old; the remedy is installing one), false when its
    #: agent predates screen share (the remedy is updating it).
    screen_capable: bool | None = None
    #: `approved | pending` (install-link spec §1.5). `pending` is a computer
    #: that joined through the install link and that nobody has approved yet:
    #: until someone does, it receives inventory, health monitoring and
    #: certificate renewal and nothing else (`app/jobs/review_gate.py`).
    #: `rejected` is served only in the `review` tab, and only while the
    #: rejection may have left the agent on the computer (`rejection_outcome`
    #: below) and nobody has said they removed it. Every other rejected row is
    #: a tombstone the list hides (plan D1).
    #: ADDITIVE AND DEFAULTED, like every field added to this model.
    review_state: str = "approved"
    #: The address it joined the install link from, as the API saw it (the
    #: rightmost `X-Forwarded-For` entry from Traefik). NULL for every other way
    #: a row comes to exist -- which is how the console tells "joined through
    #: the link" from "enrolled with a token".
    link_joined_from: str | None = None
    #: When it joined through the install link. NULL otherwise.
    link_joined_at: datetime | None = None
    #: How a rejection ended: `agent_confirmed`, `timeout`,
    #: `agent_did_not_confirm` or `agent_cannot_uninstall`; NULL for every row
    #: that was not rejected, and while a rejected agent still has time to
    #: confirm. On a served row it is one of the last three, which mean the
    #: agent may still be on the computer and has to be removed by hand
    #: (`POST /devices/{id}/review/acknowledge-removal` records that).
    #: ADDITIVE AND DEFAULTED.
    rejection_outcome: str | None = None


class StateCountsOut(BaseModel):
    """What each tab of the fleet list would show FROM THE CURRENT SEARCH.

    The tab itself is NOT applied to these -- the strip has to be able to say
    what the other tabs hold -- but the search and the provenance filter are.
    Counted in SQL beside the page (`app/services/device_list.py`), never in the
    browser: a count derived from a page of rows is correct only while the page
    is the whole set, and since a sweep began recording every host it finds it
    is not. `total` below is `state_counts[state]` BY CONSTRUCTION, so the strip
    and the table under it cannot disagree.
    """

    all: int
    online: int
    offline: int
    assets: int
    certs: int
    #: Machines waiting for review (install-link spec §1.5). ADDITIVE and
    #: defaulted: a console older than the field never reads it.
    review: int = 0


class FleetCountsOut(BaseModel):
    """The whole estate, NO filters applied. The stat tiles are these numbers.

    Deliberately not narrowed by the search: "Managed endpoints" is a claim
    about the estate, and a tile that shrank as somebody typed in a search box
    would be a worse version of the defect this whole response exists to fix.

    `managed` is a join -- `agents.device_id` is a unique FK -- and never a
    stored column (asset-inventory spec §9).
    """

    total: int
    managed: int
    online: int
    cert_attention: int


class DeviceListResponse(BaseModel):
    items: list[DeviceOut]
    #: Rows matching EVERY filter, including `state`. This is what to page
    #: through, and it equals `state_counts[state]`.
    total: int
    #: ADDITIVE, defaulted. An older caller that reads only `items`/`total`
    #: sees exactly what it saw before.
    state_counts: StateCountsOut = StateCountsOut(
        all=0, online=0, offline=0, assets=0, certs=0, review=0
    )
    fleet: FleetCountsOut = FleetCountsOut(total=0, managed=0, online=0, cert_attention=0)


def _row_to_out(
    device: Device,
    agent_status: str | None,
    cert_not_after: datetime | None = None,
    cert_revoked: bool | None = None,
    holder: DeviceHolder | None = None,
    screen_capable: bool | None = None,
) -> DeviceOut:
    return DeviceOut(
        id=device.id,
        hostname=device.hostname,
        fqdn=device.fqdn,
        os_family=device.os_family,
        os_version=device.os_version,
        architecture=device.architecture,
        device_group_id=device.device_group_id,
        status=device.status,
        agent_status=agent_status,
        last_seen_at=device.last_seen_at,
        cert_not_after=cert_not_after,
        cert_state=cert_state(cert_not_after),
        # The outer join hands back NULL for a device with no agent row; that
        # is "no certificate", not "a certificate that is not revoked", and
        # both serialize as False.
        cert_revoked=bool(cert_revoked),
        lifecycle=device.lifecycle,
        asset_type=device.asset_type,
        source=device.source,
        provenance=device.provenance,
        description=device.description,
        # `str(...)`, following this codebase's convention at every INET
        # boundary (`ipam/addresses.py`): asyncpg hands back an
        # `ipaddress.IPv4Address`, not the `str` the column is annotated as, and
        # Pydantic refuses it. The model's annotation matches ipam's for
        # consistency; the conversion belongs here, where the value leaves.
        primary_ip=str(device.primary_ip) if device.primary_ip else None,
        owner=device.owner,
        location=device.location,
        assigned_to=(
            AssignedToOut(id=holder.id, display_name=holder.display_name)
            if holder is not None
            else None
        ),
        screen_capable=screen_capable,
        review_state=device.review_state,
        # `str(...)` for the reason `primary_ip` gives above: asyncpg hands an
        # INET back as an `ipaddress` object.
        link_joined_from=str(device.link_joined_from) if device.link_joined_from else None,
        link_joined_at=device.link_joined_at,
        rejection_outcome=device.rejection_outcome,
    )


def _screen_capable(agent_id: UUID | None, capabilities: object) -> bool | None:
    """`screen_capable` for a row outer-joined to its agent: None without one."""
    return can_share_screen(capabilities) if agent_id is not None else None


@router.get("", response_model=DeviceListResponse)
async def list_devices(
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
    os_family: str | None = Query(default=None),
    device_status: str | None = Query(default=None, alias="status"),
    #: `agent | operator | promoted | sweep`. "Which of these did a person
    #: vouch for?" is the question a fleet full of automatically discovered
    #: rows has to be able to answer, and this is it as a query parameter.
    provenance: str | None = Query(default=None, pattern="^(agent|operator|promoted|sweep)$"),
    #: The console's search box, SERVER-SIDE. Hostname, FQDN, description,
    #: address, operating system and architecture -- the fields the box names.
    q: str | None = Query(default=None, max_length=128),
    #: The tab strip: `all | online | offline | assets | certs | review`.
    #: `review` is the machines that joined through the install link and wait
    #: for a person. NOT the same
    #: thing as `status`, which is `devices.status` -- this is a question about
    #: the AGENT beside the row, and `assets` means there is none.
    #:
    #: Unrecognised values fall back to `all` rather than 422ing: the tab lives
    #: in a URL people paste at each other, and a stale bookmark should render
    #: the list (`device_list.device_page`).
    state: str = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> DeviceListResponse:
    """The fleet list: one page, and every number the screen renders beside it.

    **PAGING, FILTERING AND COUNTING ARE ALL IN SQL** (`app/services/
    device_list.py`). They were in the browser, over one capped page, which was
    survivable while inventory grew by hand. Since 2026-08-26 a `net.sweep`
    records every host it finds, so a /24 adds ~200 rows in one act: past the
    page limit the tab counts were wrong, the search missed, and the provenance
    filter answered "nobody vouched for any of these" about an estate full of
    swept rows -- each of them rendering as a correct-looking empty answer.

    Three counts answering three different questions, and conflating any two is
    how a tile comes to disagree with the table under it: `total` obeys every
    filter, `state_counts` obeys all of them except the tab, `fleet` obeys none.
    `total` is not counted separately -- it IS `state_counts[state]`.

    `assigned_to` -- and the search reaching a holder's name -- is served only
    to a caller who also holds `people.read` IN THE DEVICE'S CLIENT: this route
    is gated `devices.read`, and who holds a machine is a fact about a person
    of that client (plan 2026-10-26 Task 5). Decided per row in SQL
    (`device_list._reads_people_there`); a caller holding `people.read`
    nowhere skips the holder join altogether.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        with_holder = HOLDER_PERMISSION in await codes_held_anywhere(session, ctx.user_id)
        page = await device_page(
            session,
            ctx.org_id,
            os_family=os_family,
            device_status=device_status,
            provenance=provenance,
            q=q,
            state=state,
            limit=limit,
            offset=offset,
            with_holder=with_holder,
            holder_reader=ctx.user_id,
        )
        return DeviceListResponse(
            items=[
                _row_to_out(device, agent_status, not_after, revoked, holder, capable)
                for device, agent_status, not_after, revoked, capable, holder in page.rows
            ],
            total=page.total,
            state_counts=StateCountsOut(**vars(page.state_counts)),
            fleet=FleetCountsOut(**vars(page.fleet)),
        )


class CandidateOut(BaseModel):
    """One address the fleet has seen and the inventory cannot account for."""

    address: str
    mac: str | None
    #: The manufacturer, or null. NEVER a guess -- an unknown prefix comes back
    #: null and the console shows `oui_prefix` instead (`ipam/oui.py`).
    vendor: str | None
    oui_prefix: str
    hostname: str
    first_seen_at: datetime
    last_seen_at: datetime
    sightings: int
    #: `medium` means ARP saw it (authoritative for address<->MAC on-segment);
    #: `low` means only ICMP did, which establishes that something answered and
    #: nothing else.
    confidence: str
    subnet_id: UUID | None


class CandidateListResponse(BaseModel):
    """A bounded page of discovery candidates, with the count it was cut from.

    Wrapped rather than returned as a bare list on purpose: the count is the
    half that keeps the screen honest, and a list cannot carry it.
    """

    items: list[CandidateOut]
    #: How many candidates EXIST, not how many are in `items`. A /16 sweep
    #: observes tens of thousands of addresses, and a truncated list that does
    #: not say so reads as "that is all of them".
    total: int
    truncated: bool
    #: Subnets whose most recent sweep found no agent able to run it.
    #:
    #: An empty candidate list and "nothing could look" are indistinguishable
    #: on screen and mean opposite things -- the first says the segment is
    #: clean, the second says the platform has no idea. This is the number that
    #: lets the console tell them apart (asset-inventory spec §3, falsifier 8).
    unsweepable_subnets: int


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The addresses an OPERATOR selected. Bulk promote is allowed; bulk
    #: auto-promote is not, and the difference is that this list came from a
    #: human looking at the screen.
    addresses: list[str] = Field(min_length=1, max_length=1000)
    asset_type: str = Field(default="computer", pattern=f"^({'|'.join(_ASSET_TYPES)})$")
    lifecycle: str = Field(default="in_service", pattern=f"^({'|'.join(_LIFECYCLES)})$")
    owner: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class PromoteResultOut(BaseModel):
    address: str
    created: bool
    device_id: UUID | None
    reason: str


class PromoteResponse(BaseModel):
    results: list[PromoteResultOut]


# DECLARED BEFORE `/{device_id}`, and that is not a style choice: FastAPI
# matches in declaration order, so with these below it the literal path
# "candidates" is parsed as a device UUID and the route 422s. That failure
# appears only in a browser and reads like a frontend bug --
# `tests/test_asset_promote.py` asserts the 200 for exactly that reason.
@router.get("/candidates", response_model=CandidateListResponse)
async def list_asset_candidates(
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
    limit: int = Query(default=candidates_svc.DEFAULT_LIMIT, ge=1, le=candidates_svc.MAX_LIMIT),
) -> CandidateListResponse:
    """Addresses the fleet has observed that the inventory cannot account for.

    A READ. Nothing here creates a device row: an echo reply is not an asset,
    and a sweep on a corporate LAN returns printers, phones and the coffee
    machine (`docs/modules/asset-inventory/spec.md` §3). Promotion is a
    separate, operator-driven action.

    Gated `devices.read` rather than a new code: this is the fleet list's
    other half, and anyone who may see the fleet may see what is beside it.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        page = await candidates_svc.list_candidates(session, ctx.org_id, limit=limit)
        return CandidateListResponse(
            items=[CandidateOut(**vars(candidate)) for candidate in page.items],
            total=page.total,
            truncated=page.truncated,
            unsweepable_subnets=await candidates_svc.unsweepable_subnets(session, ctx.org_id),
        )


@router.post("/candidates/promote", response_model=PromoteResponse)
async def promote_asset_candidates(
    body: PromoteRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("assets.manage")),
) -> PromoteResponse:
    """Turn selected candidates into assets.

    Per-address results rather than a batch failure: one stale entry in a
    selection must not silently discard the rest, and "already an asset" is not
    an error -- two operators looking at the same list is the normal case.

    EVERY CLIENT, IN PHASE 1. IPAM has no client yet, so a promoted machine is
    filed under the internal client and its Principal site (`candidates.py`).
    A technician limited to some clients is refused here, with the
    every-client sentence, rather than by RLS at the INSERT with a 500.
    PHASE 2: the subnet's client, and this gate goes (plan "Fuera de esta
    fase").
    """
    require_all_clients(ctx)
    overrides = body.model_dump(exclude_unset=True, exclude={"addresses"})
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        results = await candidates_svc.promote(
            session,
            ctx.org_id,
            actor_id=ctx.user_id,
            addresses=body.addresses,
            overrides=overrides,
        )
        return PromoteResponse(results=[PromoteResultOut(**vars(result)) for result in results])


class SuggestionDeviceOut(BaseModel):
    """Just enough of a device row to COMPARE it with another.

    The operator's whole job in this queue is deciding whether two records are
    one machine, so a response carrying only ids would leave them looking both
    rows up by hand for every question.
    """

    id: UUID
    hostname: str | None
    description: str | None
    primary_ip: str | None
    source: str
    lifecycle: str
    last_seen_at: datetime | None
    managed: bool


class SuggestionOut(BaseModel):
    """Two rows that might be one machine, and why the platform is unsure."""

    id: UUID
    #: `hostname | ip | multiple` -- WHY this was a suggestion rather than a
    #: link, which is not derivable later once a hostname has changed.
    basis: str
    created_at: datetime
    #: The row enrolment created.
    device: SuggestionDeviceOut
    #: The row that already existed.
    candidate: SuggestionDeviceOut


class SuggestionListResponse(BaseModel):
    """Open suggested matches for this organisation, oldest first."""

    items: list[SuggestionOut]


class ResolveSuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: `linked` merges the two records; `rejected` closes the question.
    #: Anything else is a 422 rather than a silent no-op -- these are the only
    #: two answers, and a third would leave a question open that the operator
    #: believes they answered.
    resolution: str = Field(pattern="^(linked|rejected)$")


def _suggestion_device_out(device: Device, managed: bool) -> SuggestionDeviceOut:
    return SuggestionDeviceOut(
        id=device.id,
        hostname=device.hostname,
        description=device.description,
        primary_ip=str(device.primary_ip) if device.primary_ip else None,
        source=device.source,
        lifecycle=device.lifecycle,
        last_seen_at=device.last_seen_at,
        managed=managed,
    )


# BEFORE `/{device_id}`, like `/candidates` above: declared after it, the
# literal path is parsed as a device UUID and 422s in a browser only.
@router.get("/suggestions", response_model=SuggestionListResponse)
async def list_asset_suggestions(
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
) -> SuggestionListResponse:
    """Records an enrolling agent could not safely be linked to.

    Gated `devices.read`: seeing that two records might be the same machine is
    not a wider privilege than seeing the records. Resolving them is, and that
    route is gated `assets.manage`.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = list(
            (
                await session.execute(
                    select(SuggestedAssetMatch)
                    .where(SuggestedAssetMatch.resolved_at.is_(None))
                    .order_by(SuggestedAssetMatch.created_at)
                )
            ).scalars()
        )
        if not rows:
            return SuggestionListResponse(items=[])

        wanted = {row.device_id for row in rows} | {row.candidate_device_id for row in rows}
        devices = {
            device.id: (device, agent_id is not None)
            for device, agent_id in (
                await session.execute(
                    select(Device, Agent.id)
                    .outerjoin(Agent, Agent.device_id == Device.id)
                    .where(Device.id.in_(wanted))
                )
            ).all()
        }
        items = []
        for row in rows:
            left = devices.get(row.device_id)
            right = devices.get(row.candidate_device_id)
            if left is None or right is None:
                # A device deleted out from under an open suggestion. Skipped
                # rather than 500ing the whole queue for one stale row.
                continue
            items.append(
                SuggestionOut(
                    id=row.id,
                    basis=row.basis,
                    created_at=row.created_at,
                    device=_suggestion_device_out(*left),
                    candidate=_suggestion_device_out(*right),
                )
            )
        return SuggestionListResponse(items=items)


@router.post("/suggestions/{suggestion_id}/resolve", response_model=SuggestionListResponse)
async def resolve_asset_suggestion(
    suggestion_id: UUID,
    body: ResolveSuggestionRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("assets.manage")),
) -> SuggestionListResponse:
    """Answer one question in the queue.

    `rejected` closes it and changes nothing: these are two different machines.

    `linked` is THE DESTRUCTIVE DIRECTION and the only place in this platform
    where two device rows become one. The operator keeps the record that
    already existed -- the one they curated -- so the `agents` row moves onto
    it and the row enrolment created is RETIRED, never deleted. A delete would
    take its job history, its documentation links and its audit trail's target
    with it; retirement leaves all of that answerable while removing the row
    from every list an operator works from. That is the same rule the spec
    states for assets generally: deleting a record with history is a
    destructive act on evidence.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        suggestion = await session.get(SuggestedAssetMatch, suggestion_id)
        if suggestion is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such suggestion"))
        if suggestion.resolved_at is not None:
            # Refused, not silently redone: resolving twice would move an agent
            # a second time, and a retired row coming back is how a decision
            # somebody made deliberately gets undone by a double-click.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr(
                    "this suggestion was already resolved as {resolution}",
                    resolution=suggestion.resolution,
                ),
            )

        enrolled = await session.get(Device, suggestion.device_id)
        kept = await session.get(Device, suggestion.candidate_device_id)
        if enrolled is None or kept is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, tr("one of these records no longer exists")
            )

        if body.resolution == "linked":
            agent = (
                await session.execute(select(Agent).where(Agent.device_id == enrolled.id))
            ).scalar_one_or_none()
            existing_on_kept = (
                await session.execute(select(Agent).where(Agent.device_id == kept.id))
            ).scalar_one_or_none()
            if agent is not None and existing_on_kept is not None:
                # Both rows have an agent, so they are two machines that both
                # enrolled -- linking them would orphan one identity.
                # `agents.device_id` is unique, so this would otherwise surface
                # as an IntegrityError.
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    tr("both records have an agent; they are two enrolled machines"),
                )
            if agent is not None:
                agent.device_id = kept.id
            # The kept row gains what the machine reported.
            kept.hostname = enrolled.hostname or kept.hostname
            kept.os_family = enrolled.os_family or kept.os_family
            kept.os_version = enrolled.os_version or kept.os_version
            kept.architecture = enrolled.architecture or kept.architecture
            kept.serial = enrolled.serial or kept.serial
            kept.last_seen_at = enrolled.last_seen_at or kept.last_seen_at
            enrolled.lifecycle = "retired"

        suggestion.resolution = body.resolution
        suggestion.resolved_at = datetime.now(UTC)
        suggestion.resolved_by_user_id = ctx.user_id
        await session.flush()

        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="asset.match_resolved",
            target_type="device",
            target_id=str(kept.id),
            # BOTH ROWS BY ID. Two identities became one; without this nobody
            # can say which survived, which was retired, or who decided.
            metadata={
                "resolution": body.resolution,
                "basis": suggestion.basis,
                "kept_device_id": str(kept.id),
                "retired_device_id": str(enrolled.id) if body.resolution == "linked" else "",
            },
        )
    return await list_asset_suggestions(request, ctx)


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.read")),
) -> DeviceOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        row = (
            await session.execute(
                select(
                    Device,
                    Agent.status,
                    Agent.cert_not_after,
                    Agent.cert_revoked,
                    Agent.id,
                    Agent.capabilities,
                )
                .join(Agent, isouter=True)
                .where(
                    Device.org_id == ctx.org_id,
                    Device.id == device_id,
                    # A REJECTED ROW IS A TOMBSTONE (install-link plan D1): not
                    # readable or editable by id any more than it is listed.
                    Device.review_state != "rejected",
                )
            )
        ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device"))
    device, agent_status, cert_not_after, cert_revoked, agent_id, capabilities = row
    return _row_to_out(
        device,
        agent_status,
        cert_not_after,
        cert_revoked,
        screen_capable=_screen_capable(agent_id, capabilities),
    )


async def _one_device_out(session: AsyncSession, org_id: UUID, device_id: UUID) -> DeviceOut:
    """`get_device`'s composition for one row, with the org predicate written out."""
    device, agent_status, cert_not_after, cert_revoked, agent_id, capabilities = (
        await session.execute(
            select(
                Device,
                Agent.status,
                Agent.cert_not_after,
                Agent.cert_revoked,
                Agent.id,
                Agent.capabilities,
            )
            .join(Agent, isouter=True)
            .where(Device.org_id == org_id, Device.id == device_id)
        )
    ).one()
    return _row_to_out(
        device,
        agent_status,
        cert_not_after,
        cert_revoked,
        screen_capable=_screen_capable(agent_id, capabilities),
    )


class DeviceRejectedOut(BaseModel):
    """What a Reject did (install-link spec §1.5, §4)."""

    device_id: UUID
    #: `dispatched`: the agent was told to uninstall itself; its certificate is
    #: revoked when it confirms or in 24 hours. `manual`: the agent is too old
    #: to remove itself; its certificate was revoked now and a technician has
    #: to remove it from the computer by hand.
    uninstall: Literal["dispatched", "manual"]
    job_id: UUID | None
    #: One sentence for the technician, saying which of the two happened.
    message: str


class ApproveAllIn(BaseModel):
    """The list the Approve all dialog showed (GPO MSI spec 2026-09-30 §2.5)."""

    model_config = ConfigDict(extra="forbid")

    # Bounded in the schema too, so an oversized body is refused before a
    # digest is computed over it; the service keeps its own check.
    device_ids: list[UUID] = Field(
        max_length=review_svc.APPROVE_ALL_MAX, description="The computers the dialog listed"
    )
    list_digest: str = Field(
        min_length=64,
        max_length=64,
        description="SHA-256 hex of the sorted ids, one per line, as the dialog computed it",
    )


class ApproveAllOut(BaseModel):
    """What an Approve all did."""

    bulk_id: UUID = Field(description="Carried by every device.review_approved record it wrote")
    approved: int = Field(description="Listed computers approved now")
    not_pending: int = Field(description="Listed computers that were no longer waiting")
    still_waiting: int = Field(description="Computers still waiting: they joined after the list")


@router.post("/review/approve-all", response_model=ApproveAllOut)
async def approve_all_devices(
    body: ApproveAllIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.review")),
) -> ApproveAllOut:
    """Approve every listed computer that is still waiting for review. A
    computer that joined after the list was shown is left waiting."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            outcome = await review_svc.approve_all(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                device_ids=body.device_ids,
                list_digest=body.list_digest,
                now=datetime.now(UTC),
            )
        except review_svc.ApproveAllRefused as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    return ApproveAllOut(
        bulk_id=outcome.bulk_id,
        approved=outcome.approved,
        not_pending=outcome.not_pending,
        still_waiting=outcome.still_waiting,
    )


@router.post("/{device_id}/review/approve", response_model=DeviceOut)
async def approve_device(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.review")),
) -> DeviceOut:
    """Approve a computer that joined through the install link. From now on it
    is an ordinary device and receives every job its organisation sends."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await review_svc.approve(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                device_id=device_id,
                now=datetime.now(UTC),
            )
        except review_svc.ReviewNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device")) from None
        except review_svc.NotWaitingForReview as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        return await _one_device_out(session, ctx.org_id, device_id)


@router.post("/{device_id}/review/reject", response_model=DeviceRejectedOut)
async def reject_device(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.review")),
) -> DeviceRejectedOut:
    """Reject a computer that joined through the install link: its agent is told
    to uninstall itself and its certificate is revoked (spec §1.5)."""
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            outcome = await review_svc.reject(
                session,
                state.job_engine,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                device_id=device_id,
                now=datetime.now(UTC),
            )
        except review_svc.ReviewNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device")) from None
        except review_svc.NotWaitingForReview as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    # COMMITTED. Only now is anything published: a job handed to an agent before
    # its row is durable could complete against nothing.
    if outcome.envelope is not None:
        await publish_job(state.redis, device_id, outcome.envelope)
    if outcome.revoke_serial is not None:
        await state.ca.revoke(outcome.revoke_serial)
        await publish_revoke(state.redis, device_id)
    return DeviceRejectedOut(
        device_id=device_id,
        uninstall="dispatched" if outcome.uninstall == "dispatched" else "manual",
        job_id=outcome.job.id if outcome.job is not None else None,
        message=outcome.message,
    )


@router.post("/{device_id}/review/acknowledge-removal", status_code=status.HTTP_204_NO_CONTENT)
async def acknowledge_removal(
    device_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.review")),
) -> None:
    """Say the agent of a rejected computer has been removed by hand. Only for
    a rejection that may have left it behind (the agent did not confirm, or is
    too old to remove itself); the row then leaves *Waiting for review*."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await review_svc.acknowledge_removal(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                device_id=device_id,
                now=datetime.now(UTC),
            )
        except review_svc.ReviewNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device")) from None
        except review_svc.NothingToAcknowledge as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.post("", status_code=status.HTTP_201_CREATED, response_model=DeviceOut)
async def create_asset(
    body: AssetCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("assets.manage")),
) -> DeviceOut:
    """Record an asset by hand: warehouse stock, or a device that can never run
    an agent (asset-inventory spec §2).

    The row is an ordinary `devices` row with no `agents` row beside it, which
    is what makes it appear in the fleet list and what makes every
    agent-dependent screen omit itself for it.
    """
    fields = body.model_dump(exclude_none=True, exclude={"site_id"})
    if body.primary_ip is not None:
        fields["primary_ip"] = str(body.primary_ip)
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        # LOADED IN THE CALLER'S OWN SESSION: RLS hides another organisation's
        # site and one of a client outside the caller's scope, and both are
        # the same 404. The client is the site's; an archived site or client
        # takes no new asset.
        try:
            site = await site_for_new_rows(session, org_id=ctx.org_id, site_id=body.site_id)
        except SiteNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such site")) from None
        except NotAcceptingNewRows as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, exc.detail) from None
        device = Device(
            org_id=ctx.org_id,
            client_id=site.client_id,
            site_id=site.id,
            # STAMPED, never taken from the body -- see AssetCreateRequest.
            source="manual",
            # A PERSON TYPED THIS. `provenance` is what separates it from the
            # rows a sweep now writes on its own (asset-inventory spec §3), and
            # 0078's CHECK constraint refuses the pairing if this is forgotten
            # -- a 500 at the moment of the mistake rather than an inventory
            # that quietly credits a human with a machine's guess.
            provenance="operator",
            **fields,
        )
        session.add(device)
        await session.flush()
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="asset.created",
            target_type="device",
            target_id=str(device.id),
            metadata={"asset_type": device.asset_type, "lifecycle": device.lifecycle},
        )
        return _row_to_out(device, None, None)


@router.patch("/{device_id}", response_model=DeviceOut)
async def update_asset(
    device_id: UUID,
    body: AssetUpdateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("assets.manage")),
) -> DeviceOut:
    """Edit an asset. Only the fields present in the body are written."""
    changes = body.model_dump(exclude_unset=True)
    if "primary_ip" in changes and changes["primary_ip"] is not None:
        changes["primary_ip"] = str(changes["primary_ip"])
    if "architecture" in changes and changes["architecture"] is None:
        # `devices.architecture` is NOT NULL, default "" -- "unknown" is empty.
        changes["architecture"] = ""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        row = (
            await session.execute(
                select(
                    Device,
                    Agent.status,
                    Agent.cert_not_after,
                    Agent.cert_revoked,
                    Agent.id,
                    Agent.capabilities,
                )
                .join(Agent, isouter=True)
                .where(
                    Device.org_id == ctx.org_id,
                    Device.id == device_id,
                    # A REJECTED ROW IS A TOMBSTONE (install-link plan D1): not
                    # readable or editable by id any more than it is listed.
                    Device.review_state != "rejected",
                )
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device"))
        device, agent_status, cert_not_after, cert_revoked, agent_id, capabilities = row

        machine_owned = sorted(_MACHINE_OWNED & changes.keys())
        if agent_status is not None and machine_owned:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr(
                    "{machine_owned} is reported by the agent on this device and would be "
                    "overwritten by the next inventory run",
                    machine_owned=", ".join(machine_owned),
                ),
            )

        old_fqdn = device.fqdn
        for field, value in changes.items():
            setattr(device, field, value)
        # The FIELD NAMES, not the values: an asset row can carry an owner's
        # name and a location, and the audit log is read by more people than
        # the device page is. ONE EXCEPTION, the DNS name (review I1): it
        # decides which Kerberos service a Windows push presents a domain
        # account to, so its old and new values are evidence -- and a DNS
        # name is not personal data.
        metadata: dict[str, object] = {"fields": sorted(changes)}
        if "fqdn" in changes:
            metadata["fqdn"] = {"old": old_fqdn, "new": device.fqdn}
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="asset.updated",
            target_type="device",
            target_id=str(device.id),
            metadata=metadata,
        )
        await session.flush()
        return _row_to_out(
            device,
            agent_status,
            cert_not_after,
            cert_revoked,
            screen_capable=_screen_capable(agent_id, capabilities),
        )
