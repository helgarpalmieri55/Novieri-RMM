"""Clients, sites and contacts (MSP clients spec §1, §2.1, §3.2, §4; Task 9).

Business rules live here; `app/api/v1/clients.py` maps `ClientError` to HTTP
outside the session block, so a refusal rolls the whole write back.

EVERY FUNCTION TAKES A SESSION ALREADY SCOPED BY THE CALLER'S `ClientScope`.
A client outside it is simply not found -- RLS hides it (migration 0231's
`client_isolation` on `clients`, `sites` and `users`) -- and the answer is the
same 404 as for an id that does not exist (spec §3.2: a 403 would be an
existence oracle). The `org_id` predicates mirror RLS rather than relying on
it, so a caller on the admin engine cannot reach another organisation either.

WHO MAY DO WHAT is decided at the route (spec §2.1): creating and archiving a
client need `clients.manage` on every client; editing one, and its contacts,
`clients.manage` on that client; its sites, `sites.manage` on that client.

ARCHIVING IS THE ONLY END (spec §1.1: "No se borran clientes, solo se
archivan"). A site is archived too, never deleted (addendum decision 3):
enrollment tokens and install links name it by foreign key, and their history
is the audit trail of how machines got into it. Archiving either revokes the
live enrollment tokens and switches off the install links beneath it, in the
same transaction, each audited -- and `enroll`, `live_link_for` and the MSI
refuse an archived site or client whatever predates the archive.

A CONTACT IS A `users` ROW OF KIND `contact` (spec §1.3) that cannot sign in
yet: how a contact authenticates is project 2. Its `external_subject` -- NOT
NULL and unique per organisation -- holds a placeholder no identity provider
issues (`contact-pending:<uuid>`), never the email: an email there would make
the organisation-wide key an oracle across clients (Task 8b), and would go
stale the first time the address is edited, which a narrowed scope may do but
may not rewrite `external_subject` to follow (`its_user_identity_guard`).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import String, case, cast, func, select, type_coerce
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.identity import CONTACT_KIND
from app.models import Client, Device, Site, User
from app.models.clients import (
    CLIENT_KIND_CUSTOMER,
    CLIENT_KIND_INTERNAL,
    CLIENT_KIND_UNASSIGNED,
    OPTIONAL_MODULES,
)
from app.services import enrollment as enrollment_svc
from app.services import install_link as link_svc
from app.services.client_sites import (
    CLIENT_ARCHIVED,
    ClientNotFound,
    NotAcceptingNewRows,
    SiteNotFound,
    client_for_new_rows,
    site_for_new_rows,
)

#: The site every customer and internal client is born with (spec §1.2).
#: Data, like the name migration 0230's trigger gives the internal client's.
PRINCIPAL_SITE = "Principal"

CONTACT_STATUSES = ("active", "disabled")
#: The placeholder prefix of a contact's `external_subject` (module docstring).
PENDING_SUBJECT = "contact-pending:"

#: What `clients` and `sites` say when their unique keys refuse a write. A
#: domain is held by `its_client_domains_unique` (migration 0232).
_NAME_KEY = "uq_clients_org_name"
_DOMAIN_KEY = "uq_clients_email_domain"
_SITE_NAME_KEY = "uq_sites_client_name"
#: A contact's address held by another user (`its_contact_email_unique`,
#: migration 0232).
_CONTACT_EMAIL_KEY = "uq_users_contact_email"

#: One DNS label; a domain is two or more, the last not all digits.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"^(?=.{{1,253}}$)(?:{_LABEL}\.)+(?!\d+$){_LABEL}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ClientError(Exception):
    """A refusal with the status the route answers and a sentence already
    translated (`tr`). Mapped BY STATUS, never by matching the prose."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ClientRow:
    client: Client
    #: Devices filed under the client, tombstones of rejected machines aside
    #: -- the number the fleet list shows for it.
    device_count: int
    #: Its sites that are not archived.
    site_count: int


@dataclass(frozen=True)
class SiteRow:
    site: Site
    #: Devices filed under the site, tombstones aside.
    device_count: int


def _constraint(exc: IntegrityError) -> str | None:
    name = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
    return name if isinstance(name, str) else None


def _shown_device() -> Any:
    """Every device row but a rejected one: a rejected row is a tombstone the
    fleet list never shows (`app/services/device_list.py::_shown`)."""
    return Device.review_state != "rejected"


def _active_device() -> Any:
    """A device a client cannot be archived with (spec §4: "Primero se retiran
    los equipos"): neither retired by a person (`lifecycle`) nor decommissioned
    by a completed rejection (`status`)."""
    return (Device.lifecycle != "retired") & (Device.status != "decommissioned")


def normalise_domains(domains: Sequence[str]) -> list[str]:
    """Lower-cased, de-duplicated and sorted; anything that is not a domain is
    a 422 naming it (the comparison of spec §3.3 is exact and case-free)."""
    out: set[str] = set()
    for raw in domains:
        domain = raw.strip().lower().rstrip(".")
        if not domain:
            continue
        if not _DOMAIN.match(domain):
            raise ClientError(422, tr("{domain} is not an email domain", domain=repr(raw.strip())))
        out.add(domain)
    return sorted(out)


def _modules(modules: Sequence[str]) -> list[str]:
    unknown = sorted(set(modules) - set(OPTIONAL_MODULES))
    if unknown:
        raise ClientError(
            422,
            tr(
                "{modules} is not an optional module; the optional modules are {allowed}",
                modules=", ".join(unknown),
                allowed=", ".join(OPTIONAL_MODULES),
            ),
        )
    return sorted(set(modules))


def _client_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise ClientError(422, tr("a client needs a name"))
    return name


def _site_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise ClientError(422, tr("a site needs a name"))
    return name


def _email(raw: str) -> str:
    email = raw.strip().lower()
    if not _EMAIL.match(email):
        raise ClientError(422, tr("{email} is not an email address", email=repr(raw.strip())))
    return email


async def _domain_taken(
    session: AsyncSession, org_id: UUID, domains: Sequence[str], exclude: UUID | None
) -> bool:
    """Whether a client THIS SESSION CAN SEE already holds one of `domains`.
    A client outside the scope is caught by the database instead (migration
    0232), with the same 409."""
    if not domains:
        return False
    # `Client.email_domains` is the generic ARRAY; `&&` is PostgreSQL's.
    as_pg = PG_ARRAY(String(253))
    held = type_coerce(Client.email_domains, as_pg)
    query = select(Client.id).where(
        Client.org_id == org_id, held.overlap(cast(list(domains), as_pg))
    )
    if exclude is not None:
        query = query.where(Client.id != exclude)
    return (await session.execute(query.limit(1))).scalar_one_or_none() is not None


def _domain_refusal() -> ClientError:
    return ClientError(409, tr("an email domain already belongs to another client"))


async def _flush_client(session: AsyncSession) -> None:
    """Flush a client write, answering its unique keys with a 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        constraint = _constraint(exc)
        if constraint == _DOMAIN_KEY:
            raise _domain_refusal() from None
        if constraint == _NAME_KEY:
            raise ClientError(409, tr("a client with that name already exists")) from None
        raise


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #


async def list_clients(
    session: AsyncSession,
    *,
    org_id: UUID,
    include_archived: bool = False,
    client_id: UUID | None = None,
) -> list[ClientRow]:
    """The clients this session can see, with their counts: the internal
    client first, then customers by name, `unassigned` last (visible only to
    an every-client scope, migration 0231). Archived ones only on request.

    The counts are two grouped subqueries joined once, never a count per row.
    """
    devices = (
        select(Device.client_id, func.count().label("n"))
        .where(Device.org_id == org_id, _shown_device())
        .group_by(Device.client_id)
        .subquery()
    )
    sites = (
        select(Site.client_id, func.count().label("n"))
        .where(Site.org_id == org_id, Site.archived_at.is_(None))
        .group_by(Site.client_id)
        .subquery()
    )
    order = case(
        (Client.kind == CLIENT_KIND_INTERNAL, 0),
        (Client.kind == CLIENT_KIND_UNASSIGNED, 2),
        else_=1,
    )
    query = (
        select(Client, func.coalesce(devices.c.n, 0), func.coalesce(sites.c.n, 0))
        .outerjoin(devices, devices.c.client_id == Client.id)
        .outerjoin(sites, sites.c.client_id == Client.id)
        .where(Client.org_id == org_id)
        .order_by(order, func.lower(Client.name), Client.id)
    )
    if client_id is not None:
        query = query.where(Client.id == client_id)
    elif not include_archived:
        query = query.where(Client.status != CLIENT_ARCHIVED)
    rows = (await session.execute(query)).all()
    return [ClientRow(client=c, device_count=int(d), site_count=int(s)) for c, d, s in rows]


async def get_client(session: AsyncSession, *, org_id: UUID, client_id: UUID) -> ClientRow:
    """One client, archived or not; 404 when this session cannot see it."""
    rows = await list_clients(session, org_id=org_id, client_id=client_id)
    if not rows:
        raise ClientError(404, tr("no such client"))
    return rows[0]


async def _client(session: AsyncSession, org_id: UUID, client_id: UUID) -> Client:
    client = (
        await session.execute(select(Client).where(Client.id == client_id, Client.org_id == org_id))
    ).scalar_one_or_none()
    if client is None:
        raise ClientError(404, tr("no such client"))
    return client


async def create_client(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    email_domains: Sequence[str] = (),
    phone: str = "",
    address: str = "",
    website: str = "",
    notes: str = "",
    enabled_modules: Sequence[str] = (),
) -> Client:
    """A customer client and its "Principal" site, in one transaction (spec
    §1.2, §3.2). Every-client callers only (the route's gate), so the domain
    and name checks see every client."""
    name = _client_name(name)
    domains = normalise_domains(email_domains)
    modules = _modules(enabled_modules)
    if await _domain_taken(session, org_id, domains, None):
        raise _domain_refusal()
    client = Client(
        org_id=org_id,
        name=name,
        kind=CLIENT_KIND_CUSTOMER,
        status="active",
        email_domains=domains,
        phone=phone.strip(),
        address=address.strip(),
        website=website.strip(),
        notes=notes,
        enabled_modules=modules,
    )
    session.add(client)
    await _flush_client(session)
    site = Site(org_id=org_id, client_id=client.id, name=PRINCIPAL_SITE)
    session.add(site)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="client.created",
        target_type="client",
        target_id=str(client.id),
        metadata={
            "name": client.name,
            "email_domains": domains,
            "enabled_modules": modules,
            "principal_site_id": str(site.id),
        },
    )
    await _audit_site(session, org_id, actor_id, "client.site_created", site, {"name": site.name})
    return client


#: The fields `update_client` writes; anything else is the API's 422.
CLIENT_FIELDS = (
    "name",
    "email_domains",
    "phone",
    "address",
    "website",
    "notes",
    "enabled_modules",
)


async def update_client(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: UUID,
    changes: Mapping[str, Any],
) -> Client:
    """A partial edit of one client (spec §3.2: name, contact details, domains,
    optional modules). Its kind and status are not fields: an archive is its
    own act. An archived client is not edited (409)."""
    unknown = set(changes) - set(CLIENT_FIELDS)
    if unknown:
        raise ValueError(  # i18n-exempt: caller contract, unhandled 500
            f"update_client cannot write {sorted(unknown)}"
        )
    client = await _client(session, org_id, client_id)
    if client.status == CLIENT_ARCHIVED:
        raise ClientError(409, tr("that client is archived"))
    wanted: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "name":
            wanted[key] = _client_name(value)
        elif key == "email_domains":
            wanted[key] = normalise_domains(value)
        elif key == "enabled_modules":
            wanted[key] = _modules(value)
        elif key == "notes":
            wanted[key] = value
        else:
            wanted[key] = value.strip()
    # THE UNASSIGNED CLIENT (review 1, M6): unknown senders land there because
    # no client's domain matched (spec §3.3), so a domain of its own would be a
    # contradiction, and it serves nobody, so an optional module there means
    # nothing. An empty list is what it holds already, and passes.
    if client.kind == CLIENT_KIND_UNASSIGNED:
        if wanted.get("email_domains"):
            raise ClientError(422, tr("the unassigned client takes no email domains"))
        if wanted.get("enabled_modules"):
            raise ClientError(422, tr("the unassigned client has no optional modules"))
    changed = {
        key: {"before": getattr(client, key), "after": value}
        for key, value in wanted.items()
        if getattr(client, key) != value
    }
    if "email_domains" in changed and await _domain_taken(
        session, org_id, wanted["email_domains"], client_id
    ):
        raise _domain_refusal()
    if not changed:
        return client
    for key, diff in changed.items():
        setattr(client, key, diff["after"])
    await _flush_client(session)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="client.updated",
        target_type="client",
        target_id=str(client_id),
        metadata={"changed": changed},
    )
    return client


async def archive_client(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, client_id: UUID, now: datetime
) -> Client:
    """Archive a customer client (spec §3.2, §4): 409 for the internal and
    unassigned clients and while any of its devices is active; otherwise its
    live enrollment tokens are revoked and its install links switched off in
    the same transaction, each audited."""
    # FOR NO KEY UPDATE, not FOR UPDATE: a device written meanwhile takes a KEY
    # SHARE lock on its client through its foreign key, which FOR UPDATE would
    # block -- and with the token lock below that is a deadlock with an
    # enrollment in flight. NO KEY UPDATE still serialises two archives.
    client = (
        await session.execute(
            select(Client)
            .where(Client.id == client_id, Client.org_id == org_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    if client is None:
        raise ClientError(404, tr("no such client"))
    if client.kind != CLIENT_KIND_CUSTOMER:
        raise ClientError(409, tr("the internal and unassigned clients cannot be archived"))
    if client.status == CLIENT_ARCHIVED:
        raise ClientError(409, tr("that client is archived"))
    # THE LOCKS BEFORE THE COUNT (review 1, concern 5): an enrollment in flight
    # holds its token's (or its link's) row lock until its device commits, so
    # taking them first makes the count below see that device -- or makes the
    # enrollment find its token revoked and its link off.
    tokens = await enrollment_svc.lock_live_tokens(
        session, org_id=org_id, now=now, client_id=client_id
    )
    links = await link_svc.lock_links(session, org_id=org_id, client_id=client_id)
    # ITS CONTACTS TOO, AND BEFORE ANY AUDIT WRITE (final review, T9R2 N1).
    # The audit chain's lock is taken by the first event below and held to
    # commit; a contact row locked after it would wait on a PATCH that, holding
    # that row, waits on the chain -- a 40P01. `update_contact` reads the
    # client FOR SHARE first, so it cannot even be holding the row by now.
    contacts = await _lock_contacts(session, org_id, client_id)
    active = (
        await session.execute(
            select(func.count())
            .select_from(Device)
            .where(Device.org_id == org_id, Device.client_id == client_id, _active_device())
        )
    ).scalar_one()
    if active:
        raise ClientError(
            409,
            tr(
                "this client has {count} active devices; retire them before archiving it",
                count=active,
            ),
        )
    client.status = CLIENT_ARCHIVED
    await session.flush()
    revoked = await enrollment_svc.revoke_tokens(
        session, org_id=org_id, actor_id=actor_id, reason="client_archived", tokens=tokens
    )
    switched = await link_svc.switch_off(
        session, actor_id=actor_id, reason="client_archived", now=now, links=links
    )
    disabled = await _disable_contacts(session, org_id, actor_id, client_id, contacts)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="client.archived",
        target_type="client",
        target_id=str(client_id),
        metadata={
            "name": client.name,
            "tokens_revoked": revoked,
            "links_switched_off": switched,
            "contacts_disabled": disabled,
        },
    )
    return client


async def _lock_contacts(session: AsyncSession, org_id: UUID, client_id: UUID) -> list[User]:
    """Every contact of the client, locked FOR NO KEY UPDATE in id order --
    NO KEY UPDATE for the reason the client's own lock gives: a row written
    meanwhile that refers to the contact takes KEY SHARE on it, which this
    does not block. Re-read from the locked rows (`populate_existing`)."""
    return list(
        (
            await session.execute(
                select(User)
                .where(
                    User.org_id == org_id,
                    User.client_id == client_id,
                    User.kind == CONTACT_KIND,
                )
                .order_by(User.id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def _disable_contacts(
    session: AsyncSession, org_id: UUID, actor_id: UUID, client_id: UUID, contacts: list[User]
) -> int:
    """Switch off an archived client's active contacts (review 1, concern 3),
    each audited `contact.updated` with `reason="client_archived"` -- the
    code a person's own switch-off records, so the contact's history reads as
    one list. Its people keep no way in through the portal (project 2) to a
    client nobody serves. `contacts` are `_lock_contacts`' rows."""
    count = 0
    for user in contacts:
        if user.status != "active":
            continue
        user.status = "disabled"
        await session.flush()
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="contact.updated",
            target_type="user",
            target_id=str(user.id),
            metadata={
                "client_id": str(client_id),
                "changed": {"status": {"before": "active", "after": "disabled"}},
                "reason": "client_archived",
            },
        )
        count += 1
    return count


# --------------------------------------------------------------------------- #
# Sites
# --------------------------------------------------------------------------- #


async def _audit_site(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    action: str,
    site: Site,
    metadata: dict[str, Any],
) -> None:
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type="site",
        target_id=str(site.id),
        metadata={"client_id": str(site.client_id), **metadata},
    )


async def _site_rows(session: AsyncSession, org_id: UUID, *criteria: Any) -> list[SiteRow]:
    devices = (
        select(Device.site_id, func.count().label("n"))
        .where(Device.org_id == org_id, _shown_device())
        .group_by(Device.site_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Site, func.coalesce(devices.c.n, 0))
            .outerjoin(devices, devices.c.site_id == Site.id)
            .where(Site.org_id == org_id, *criteria)
            .order_by(func.lower(Site.name), Site.id)
        )
    ).all()
    return [SiteRow(site=site, device_count=int(n)) for site, n in rows]


async def list_sites(
    session: AsyncSession, *, org_id: UUID, client_id: UUID, include_archived: bool = False
) -> list[SiteRow]:
    """A visible client's sites, archived ones only on request."""
    await _client(session, org_id, client_id)
    criteria: list[Any] = [Site.client_id == client_id]
    if not include_archived:
        criteria.append(Site.archived_at.is_(None))
    return await _site_rows(session, org_id, *criteria)


async def _site_row(session: AsyncSession, org_id: UUID, site_id: UUID) -> SiteRow:
    rows = await _site_rows(session, org_id, Site.id == site_id)
    if not rows:
        raise ClientError(404, tr("no such site"))
    return rows[0]


async def _site_name_taken(
    session: AsyncSession, client_id: UUID, name: str, exclude: UUID | None
) -> bool:
    query = select(Site.id).where(
        Site.client_id == client_id, func.lower(Site.name) == name.lower()
    )
    if exclude is not None:
        query = query.where(Site.id != exclude)
    return (await session.execute(query.limit(1))).scalar_one_or_none() is not None


def _site_name_refusal() -> ClientError:
    return ClientError(409, tr("that client already has a site with that name"))


async def _flush_site(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        if _constraint(exc) == _SITE_NAME_KEY:
            raise _site_name_refusal() from None
        raise


async def create_site(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: UUID,
    name: str,
    address: str = "",
    description: str = "",
) -> SiteRow:
    """A new site of a visible, active client. The unassigned client has none
    (spec §1.1). Its egress networks start empty: they are the install link's
    to set (`PUT /sites/{id}/install-link`, `enrollment.link.manage`)."""
    try:
        client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
    except ClientNotFound:
        raise ClientError(404, tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise ClientError(409, exc.detail) from None
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise ClientError(422, tr("the unassigned client cannot have sites"))
    name = _site_name(name)
    if await _site_name_taken(session, client_id, name, None):
        raise _site_name_refusal()
    site = Site(
        org_id=org_id,
        client_id=client_id,
        name=name,
        address=address.strip(),
        description=description,
    )
    session.add(site)
    await _flush_site(session)
    await _audit_site(session, org_id, actor_id, "client.site_created", site, {"name": name})
    return SiteRow(site=site, device_count=0)


#: The fields `update_site` writes. NOT `egress_networks`: they decide which
#: machines may enrol through the site's install link, and are written by the
#: link's own route under `enrollment.link.manage` -- `sites.manage` writing
#: them would widen the link without that code. Nor `client_id`: a site never
#: changes client (`fk_<table>_site_client`, migration 0231).
SITE_FIELDS = ("name", "address", "description")


async def update_site(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    site_id: UUID,
    changes: Mapping[str, Any],
) -> SiteRow:
    """A partial edit of one site. An archived site, or one of an archived
    client, is not edited (409)."""
    unknown = set(changes) - set(SITE_FIELDS)
    if unknown:
        raise ValueError(  # i18n-exempt: caller contract, unhandled 500
            f"update_site cannot write {sorted(unknown)}"
        )
    try:
        # FOR NO KEY UPDATE: this writes the site's row (`client_sites`).
        site = await site_for_new_rows(session, org_id=org_id, site_id=site_id, for_update=True)
    except SiteNotFound:
        raise ClientError(404, tr("no such site")) from None
    except NotAcceptingNewRows as exc:
        raise ClientError(409, exc.detail) from None
    wanted: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "name":
            wanted[key] = _site_name(value)
        elif key == "address":
            wanted[key] = value.strip()
        else:
            wanted[key] = value
    changed = {
        key: {"before": getattr(site, key), "after": value}
        for key, value in wanted.items()
        if getattr(site, key) != value
    }
    if "name" in changed and await _site_name_taken(
        session, site.client_id, wanted["name"], site.id
    ):
        raise _site_name_refusal()
    if changed:
        for key, diff in changed.items():
            setattr(site, key, diff["after"])
        await _flush_site(session)
        await _audit_site(
            session, org_id, actor_id, "client.site_updated", site, {"changed": changed}
        )
    return await _site_row(session, org_id, site_id)


async def archive_site(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, site_id: UUID, now: datetime
) -> SiteRow:
    """`DELETE /sites/{id}` (addendum decision 3): archive, never delete.

    409 while the site holds a device (spec §1.2, §4) -- any the fleet list
    shows, retired ones included: archived, they would sit in a site no
    selector offers. 409 for the internal client's "Principal" site, whose id
    is the organisation's: the phase-1 fallbacks (IPAM, provisioning) file rows
    under it. Otherwise its live tokens are revoked and its install link
    switched off in the same transaction, each audited.
    """
    # THE SITE'S ROW FIRST, AND ITS STATE READ FROM THE LOCKED ROW (final
    # review, T9R2 N4): FOR NO KEY UPDATE (a device's foreign key takes KEY
    # SHARE), then its client FOR SHARE. A second DELETE racing this one waits
    # here and then re-reads `archived_at` -- a 409, not a second archive with
    # a second audit event. Then its tokens and its link, BEFORE the count, as
    # in `archive_client` (review 1, concern 5).
    try:
        site = await site_for_new_rows(session, org_id=org_id, site_id=site_id, for_update=True)
    except SiteNotFound:
        raise ClientError(404, tr("no such site")) from None
    except NotAcceptingNewRows as exc:
        raise ClientError(409, exc.detail) from None
    if site.id == site.org_id:
        raise ClientError(
            409, tr("the Principal site of the organization's own client cannot be archived")
        )
    tokens = await enrollment_svc.lock_live_tokens(session, org_id=org_id, now=now, site_id=site_id)
    links = await link_svc.lock_links(session, org_id=org_id, site_id=site_id)
    devices = (
        await session.execute(
            select(func.count())
            .select_from(Device)
            .where(Device.org_id == org_id, Device.site_id == site_id, _shown_device())
        )
    ).scalar_one()
    if devices:
        raise ClientError(
            409,
            tr(
                "this site has {count} devices; a site with devices cannot be archived",
                count=devices,
            ),
        )
    site.archived_at = now
    await session.flush()
    revoked = await enrollment_svc.revoke_tokens(
        session, org_id=org_id, actor_id=actor_id, reason="site_archived", tokens=tokens
    )
    switched = await link_svc.switch_off(
        session, actor_id=actor_id, reason="site_archived", now=now, links=links
    )
    await _audit_site(
        session,
        org_id,
        actor_id,
        "client.site_archived",
        site,
        {"name": site.name, "tokens_revoked": revoked, "links_switched_off": switched},
    )
    return SiteRow(site=site, device_count=0)


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #


async def list_contacts(session: AsyncSession, *, org_id: UUID, client_id: UUID) -> list[User]:
    """A visible client's contacts, disabled ones included."""
    await _client(session, org_id, client_id)
    rows = await session.execute(
        select(User)
        .where(User.org_id == org_id, User.client_id == client_id, User.kind == CONTACT_KIND)
        .order_by(func.lower(User.display_name), func.lower(User.email), User.id)
    )
    return list(rows.scalars())


async def _contact_email_taken(
    session: AsyncSession, org_id: UUID, email: str, exclude: UUID | None
) -> bool:
    """Whether a user THIS SESSION CAN SEE -- staff, or a contact of a client in
    scope -- already has the address (review 1, I1). One address is one
    person: inbound mail files a message under its sender's client (spec §3.3)
    and quarantines a sender two users share. A contact of a client outside
    the scope is caught by the database instead (`its_contact_email_unique`,
    migration 0232), with the same 409."""
    query = select(User.id).where(User.org_id == org_id, func.lower(User.email) == email)
    if exclude is not None:
        query = query.where(User.id != exclude)
    return (await session.execute(query.limit(1))).scalar_one_or_none() is not None


def _contact_refusal() -> ClientError:
    return ClientError(
        409, tr("that email address already belongs to another user of the organization")
    )


async def _flush_contact(session: AsyncSession) -> None:
    """Flush a contact write, answering `its_contact_email_unique` with the 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        if _constraint(exc) == _CONTACT_EMAIL_KEY:
            raise _contact_refusal() from None
        raise


async def create_contact(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: UUID,
    email: str,
    display_name: str = "",
    phone: str | None = None,
) -> User:
    """A contact of a visible, active client (spec §1.3). The unassigned
    client has none: it is where unknown senders land (spec §3.3)."""
    try:
        client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
    except ClientNotFound:
        raise ClientError(404, tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise ClientError(409, exc.detail) from None
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise ClientError(422, tr("the unassigned client cannot have contacts"))
    address = _email(email)
    if await _contact_email_taken(session, org_id, address, None):
        raise _contact_refusal()
    user = User(
        org_id=org_id,
        kind=CONTACT_KIND,
        client_id=client_id,
        email=address,
        display_name=display_name.strip(),
        phone=(phone or "").strip() or None,
        status="active",
        external_subject=f"{PENDING_SUBJECT}{uuid4()}",
    )
    session.add(user)
    await _flush_contact(session)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="contact.created",
        target_type="user",
        target_id=str(user.id),
        metadata={"client_id": str(client_id), "email": address},
    )
    return user


#: The fields `update_contact` writes. Never `kind`, `client_id` or
#: `external_subject` (addendum decision 1).
CONTACT_FIELDS = ("email", "display_name", "phone", "status")


async def update_contact(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    contact_id: UUID,
    changes: Mapping[str, Any],
) -> User:
    """Edit or switch off a contact of a visible client. A staff row is not a
    contact, and is 404 here whatever the scope sees. A contact of an
    archived client is not edited (409, review 1 M4), as `create_contact`
    refuses one: re-enabling it would undo part of the archive."""
    unknown = set(changes) - set(CONTACT_FIELDS)
    if unknown:
        raise ValueError(  # i18n-exempt: caller contract, unhandled 500
            f"update_contact cannot write {sorted(unknown)}"
        )
    owner = (
        await session.execute(
            select(User.client_id).where(
                User.id == contact_id, User.org_id == org_id, User.kind == CONTACT_KIND
            )
        )
    ).scalar_one_or_none()
    if owner is None:
        raise ClientError(404, tr("no such contact"))
    # ITS CLIENT FOR SHARE, BEFORE THE CONTACT'S ROW (final review, T9R2 N1):
    # an archive holds the client FOR NO KEY UPDATE, so this waits for one in
    # flight and then reads "archived" (409) -- rather than reading "active",
    # writing the contact, and re-enabling a contact of an archived client
    # once the archive commits. And with the client first, this never holds
    # the contact's row while the archive waits for it.
    client_status = (
        await session.execute(
            select(Client.status)
            .where(Client.id == owner, Client.org_id == org_id)
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if client_status is None:
        raise ClientError(404, tr("no such contact"))
    if client_status == CLIENT_ARCHIVED:
        raise ClientError(409, tr("that client is archived"))
    user = (
        await session.execute(
            select(User)
            .where(User.id == contact_id, User.org_id == org_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert user.client_id is not None  # a contact always has its client
    wanted: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "email":
            wanted[key] = _email(value)
        elif key == "display_name":
            wanted[key] = value.strip()
        elif key == "phone":
            wanted[key] = (value or "").strip() or None
        else:
            if value not in CONTACT_STATUSES:
                raise ValueError(  # i18n-exempt: the API's schema refuses it first
                    f"unknown contact status {value!r}"
                )
            wanted[key] = value
    changed = {
        key: {"before": getattr(user, key), "after": value}
        for key, value in wanted.items()
        if getattr(user, key) != value
    }
    if "email" in changed and await _contact_email_taken(session, org_id, wanted["email"], user.id):
        raise _contact_refusal()
    if changed:
        for key, diff in changed.items():
            setattr(user, key, diff["after"])
        await _flush_contact(session)
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="contact.updated",
            target_type="user",
            target_id=str(user.id),
            metadata={"client_id": str(user.client_id), "changed": changed},
        )
    return user
