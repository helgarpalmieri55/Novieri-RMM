"""`/api/v1` clients, sites and contacts (MSP clients spec §3.2, Task 9).

EVERY ROUTE CARRIES A PERMISSION (spec §2.1), recovered from the router by
`app.core.rbac.route_permission` (`tests/test_rbac.py`):

| Route | Gate |
|---|---|
| `GET /clients`, `GET /clients/{id}` | `clients.read` |
| `POST /clients`, `POST /clients/{id}/archive` | `clients.manage` on EVERY client |
| `PATCH /clients/{id}` | `clients.manage` on that client |
| `GET /clients/{id}/sites` | `clients.read` |
| `POST /clients/{id}/sites`, `PATCH`/`DELETE /sites/{id}` | `sites.manage` on that client |
| `GET /clients/{id}/contacts` | `clients.read` |
| `POST /clients/{id}/contacts`, `PATCH /contacts/{id}` | `clients.manage` on that client |

"ON THAT CLIENT" IS RLS, NOT A CHECK HERE. `require_permission(code)` hands the
route the scope of the clients where the person holds `code`, narrowed by
`X-Client-Id`; a record of any other client does not exist for the request,
so it is a 404 -- never a 403, which would confirm the id exists (spec §3.2,
`app/modules/ipam/api.py`). "On EVERY client" is the dependency
`require_permission_on_every_client`, which refuses a narrower scope with
403 before the body is read; with it the route's own scope IS every client,
so nothing here needs `ClientScope.all()` (spec §2.3).

HANDLERS STAY THIN: open the session on the request's scope, call
`app.services.clients`, and map `ClientError` OUTSIDE the session block, so a
refusal rolls back everything the request wrote -- its audit events included.

WHAT IS NOT EDITABLE HERE, deliberately: a client's `kind` and `status` (an
archive is its own act), a site's `client_id` and `egress_networks` (the
networks decide who may enrol through the site's install link, and are written
by that link's route under `enrollment.link.manage`), and a contact's `kind`,
`client_id` and login identity. Every request model is closed
(`extra="forbid"`), so naming one of them is a 422, not a silent no-op.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.tenancy import org_session
from app.models import User
from app.services import clients as svc

router = APIRouter(tags=["clients"])

#: The optional modules a client may switch on (spec §1.4). Held equal to
#: `app.models.clients.OPTIONAL_MODULES` by `tests/test_clients_api.py`.
OptionalModule = Literal["budget", "bcp", "change"]
Domain = Annotated[str, Field(min_length=1, max_length=253)]
MAX_DOMAINS = 50


def _http(exc: svc.ClientError) -> HTTPException:
    """By status, never by matching the message's prose."""
    return HTTPException(exc.status_code, exc.detail)


def _changes(body: BaseModel) -> dict[str, object]:
    """The fields a PATCH named. A `null` is the same as leaving the field out:
    no field here can be NULL, and "clear it" is an empty string."""
    return {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class ClientOut(BaseModel):
    """One client: a company Novieri serves, or one of the two system clients."""

    id: UUID = Field(description="Client id -- the value X-Client-Id and every client_id take")
    name: str = Field(description="Unique within the organization, ignoring case")
    kind: str = Field(description="customer, internal (Novieri's own IT) or unassigned")
    status: str = Field(description="active or archived; clients are archived, never deleted")
    email_domains: list[str] = Field(
        description="Domains whose inbound mail is filed under this client; one client each"
    )
    phone: str = Field(description="Contact phone, free text")
    address: str = Field(description="Postal address, free text")
    website: str = Field(description="Website, free text")
    notes: str = Field(description="Free notes about the client")
    enabled_modules: list[str] = Field(
        description="Optional modules switched on for this client: budget, bcp, change"
    )
    device_count: int = Field(description="Devices filed under the client, rejected ones aside")
    site_count: int = Field(description="Sites of the client that are not archived")


class ClientCreate(BaseModel):
    """A new customer client. It is born with a site called Principal."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    email_domains: list[Domain] = Field(default_factory=list, max_length=MAX_DOMAINS)
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=2000)
    website: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=10_000)
    enabled_modules: list[OptionalModule] = Field(default_factory=list, max_length=10)


class ClientUpdate(BaseModel):
    """A partial edit: only the fields present are written."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    email_domains: list[Domain] | None = Field(default=None, max_length=MAX_DOMAINS)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=2000)
    website: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=10_000)
    enabled_modules: list[OptionalModule] | None = Field(default=None, max_length=10)


class SiteOut(BaseModel):
    """One site of a client: an office, one LAN."""

    id: UUID = Field(description="Site id -- what enrollment tokens and install links take")
    client_id: UUID = Field(description="The client the site belongs to; it never changes")
    name: str = Field(description="Unique within the client, ignoring case")
    address: str = Field(description="Postal address, free text")
    description: str = Field(description="Free text about the site")
    egress_networks: list[str] = Field(
        description=(
            "Public networks the office reaches the Internet from; set with the site's install link"
        )
    )
    archived_at: datetime | None = Field(
        description="When the site was archived, or null while it is in use"
    )
    device_count: int = Field(description="Devices filed under the site, rejected ones aside")


class SiteCreate(BaseModel):
    """A new site of a client."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=2000)
    description: str = Field(default="", max_length=10_000)


class SiteUpdate(BaseModel):
    """A partial edit of a site: its name, address and description."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=10_000)


class ContactOut(BaseModel):
    """A person of a client, who will use the portal (spec §1.3)."""

    id: UUID = Field(description="The contact's user id")
    client_id: UUID = Field(description="The client the contact belongs to")
    kind: str = Field(description="Always contact")
    email: str = Field(description="Email address, lower-cased")
    display_name: str = Field(description="Name shown for the contact")
    phone: str | None = Field(description="Phone, or null")
    status: str = Field(description="active or disabled")


class ContactCreate(BaseModel):
    """A new contact of a client. Contacts cannot sign in yet."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(default="", max_length=200)
    phone: str | None = Field(default=None, max_length=40)


class ContactUpdate(BaseModel):
    """A partial edit of a contact; `status` switches it off and on."""

    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    status: Literal["active", "disabled"] | None = None


def _client_out(row: svc.ClientRow) -> ClientOut:
    c = row.client
    return ClientOut(
        id=c.id,
        name=c.name,
        kind=c.kind,
        status=c.status,
        email_domains=list(c.email_domains),
        phone=c.phone,
        address=c.address,
        website=c.website,
        notes=c.notes,
        enabled_modules=list(c.enabled_modules),
        device_count=row.device_count,
        site_count=row.site_count,
    )


def _site_out(row: svc.SiteRow) -> SiteOut:
    s = row.site
    return SiteOut(
        id=s.id,
        client_id=s.client_id,
        name=s.name,
        address=s.address,
        description=s.description,
        egress_networks=[str(n) for n in s.egress_networks],
        archived_at=s.archived_at,
        device_count=row.device_count,
    )


def _contact_out(user: User) -> ContactOut:
    assert user.client_id is not None  # a contact always has one (ck_users_kind_client)
    return ContactOut(
        id=user.id,
        client_id=user.client_id,
        kind=user.kind,
        email=user.email,
        display_name=user.display_name,
        phone=user.phone,
        status=user.status,
    )


_INCLUDE_ARCHIVED = Query(default=False, description="Include archived ones, hidden by default")


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #


@router.get("/clients", response_model=list[ClientOut])
async def list_clients(
    request: Request,
    ctx: AuthContext = Depends(require_permission("clients.read")),
    include_archived: bool = _INCLUDE_ARCHIVED,
) -> list[ClientOut]:
    """The clients of the request's scope, with their device and site counts.
    `unassigned` is listed only to a technician with every client."""
    async with org_session(
        ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
    ) as session:
        rows = await svc.list_clients(session, org_id=ctx.org_id, include_archived=include_archived)
        return [_client_out(row) for row in rows]


@router.post("/clients", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
async def create_client(
    body: ClientCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("clients.manage")),
) -> ClientOut:
    """Create a customer client and its Principal site. Needs every client."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            client = await svc.create_client(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                name=body.name,
                email_domains=body.email_domains,
                phone=body.phone,
                address=body.address,
                website=body.website,
                notes=body.notes,
                enabled_modules=body.enabled_modules,
            )
            out = _client_out(await svc.get_client(session, org_id=ctx.org_id, client_id=client.id))
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


@router.get("/clients/{client_id}", response_model=ClientOut)
async def get_client(
    client_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("clients.read")),
) -> ClientOut:
    """One client of the request's scope, archived or not."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            out = _client_out(await svc.get_client(session, org_id=ctx.org_id, client_id=client_id))
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


@router.patch("/clients/{client_id}", response_model=ClientOut)
async def update_client(
    client_id: UUID,
    body: ClientUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("clients.manage")),
) -> ClientOut:
    """Edit a client's name, contact details, email domains or optional
    modules. An email domain already held by another client is a 409."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            await svc.update_client(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                client_id=client_id,
                changes=_changes(body),
            )
            out = _client_out(await svc.get_client(session, org_id=ctx.org_id, client_id=client_id))
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


@router.post("/clients/{client_id}/archive", response_model=ClientOut)
async def archive_client(
    client_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("clients.manage")),
) -> ClientOut:
    """Archive a customer client: refused while it has active devices, and for
    the internal and unassigned clients. Revokes its live enrollment tokens and
    switches off its install links. Needs every client."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            await svc.archive_client(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                client_id=client_id,
                now=datetime.now(UTC),
            )
            out = _client_out(await svc.get_client(session, org_id=ctx.org_id, client_id=client_id))
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


# --------------------------------------------------------------------------- #
# Sites
# --------------------------------------------------------------------------- #


@router.get("/clients/{client_id}/sites", response_model=list[SiteOut])
async def list_sites(
    client_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("clients.read")),
    include_archived: bool = _INCLUDE_ARCHIVED,
) -> list[SiteOut]:
    """A client's sites, with their device counts."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            rows = await svc.list_sites(
                session,
                org_id=ctx.org_id,
                client_id=client_id,
                include_archived=include_archived,
            )
            out = [_site_out(row) for row in rows]
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


@router.post(
    "/clients/{client_id}/sites", response_model=SiteOut, status_code=status.HTTP_201_CREATED
)
async def create_site(
    client_id: UUID,
    body: SiteCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("sites.manage")),
) -> SiteOut:
    """Add a site to a client. The unassigned client has none."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            row = await svc.create_site(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                client_id=client_id,
                name=body.name,
                address=body.address,
                description=body.description,
            )
            out = _site_out(row)
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


@router.patch("/sites/{site_id}", response_model=SiteOut)
async def update_site(
    site_id: UUID,
    body: SiteUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("sites.manage")),
) -> SiteOut:
    """Rename a site or edit its address and description."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            row = await svc.update_site(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                site_id=site_id,
                changes=_changes(body),
            )
            out = _site_out(row)
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


@router.delete("/sites/{site_id}", response_model=SiteOut)
async def archive_site(
    site_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("sites.manage")),
) -> SiteOut:
    """Archive a site -- never a hard delete. Refused while it holds devices,
    and for the organization's own Principal site. Revokes its live enrollment
    tokens and switches off its install link."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            row = await svc.archive_site(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                site_id=site_id,
                now=datetime.now(UTC),
            )
            out = _site_out(row)
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #


@router.get("/clients/{client_id}/contacts", response_model=list[ContactOut])
async def list_contacts(
    client_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("clients.read")),
) -> list[ContactOut]:
    """A client's contacts, disabled ones included."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            users = await svc.list_contacts(session, org_id=ctx.org_id, client_id=client_id)
            out = [_contact_out(user) for user in users]
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


@router.post(
    "/clients/{client_id}/contacts",
    response_model=ContactOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_contact(
    client_id: UUID,
    body: ContactCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("clients.manage")),
) -> ContactOut:
    """Add a contact to a client: a user of kind contact, who cannot sign in
    to the console. One email address per client."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            user = await svc.create_contact(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                client_id=client_id,
                email=body.email,
                display_name=body.display_name,
                phone=body.phone,
            )
            out = _contact_out(user)
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: UUID,
    body: ContactUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission("clients.manage")),
) -> ContactOut:
    """Edit a contact's email, name or phone, or switch it off and on."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            user = await svc.update_contact(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                contact_id=contact_id,
                changes=_changes(body),
            )
            out = _contact_out(user)
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out
