"""The client or site a NEW row is filed under (MSP clients spec §1.5).

Every path that files something new under a client or a site -- an enrollment
token, an install link, a hand-typed asset, a device group -- loads it HERE, in
the caller's own session, and takes the client from the site rather than from
the caller. Two refusals, the same everywhere:

* `SiteNotFound` / `ClientNotFound`: absent, another organisation's, or a
  client outside the caller's scope -- RLS hides the last two, and all three are
  one 404, because a 403 would confirm the id exists somewhere (spec §2.2).
* `NotAcceptingNewRows`: an archived site, or a client that is archived. A row
  filed under one would be work for a client the console no longer offers --
  a token nobody can see minting machines into it, an asset in a selector that
  hides it. A 409 with a sentence.

Reads are not refused here: an archived site's link settings and an archived
client's devices stay readable.

A row that already HAS a client and names a site -- a subnet, a VLAN, a site
server -- loads it through `site_of_client`, which adds a third refusal,
`SiteOfAnotherClient` (a 422: the site is visible, just not this row's). And a
row whose client a trigger writes (an H table, MSP clients spec §2.4) reads
it back after an UPDATE with `loaded_client`. Both serve every module that
files rows under a client's site (IPAM and site servers, migration 0240), so
neither is re-implemented per module; each module maps the exceptions to its
own errors, as it does `SiteNotFound` and `NotAcceptingNewRows`.

THE CHECK HOLDS UNTIL COMMIT (final review, T9R2 N1). The client is read FOR
SHARE, and the site with it, so the "not archived" answer cannot go stale
under a writer: an archive holds the client's row FOR NO KEY UPDATE (and
`archive_site` the site's), which FOR SHARE waits for and then re-reads -- a
contact, site, token or asset filed during an archive in flight is refused
409 once it commits, never committed into an archived client. The other way
round, the archive waits for the writer and then finds its row (a token it
revokes, a contact it switches off).

LOCK ORDER: these are the FIRST locks of the transaction that takes them --
site, then client -- and archives take the client (`archive_client`) or the
site then the client (`archive_site`) first too, before any token, link,
contact or audit lock. IPAM is the documented exception: its creates lock the
client, may insert the client's `global` VRF, and only then lock the site
(`app.modules.ipam.clients` explains why that cannot deadlock with an
archive). A path that already holds the audit chain's lock when
it gets here would invert that order; push install minting a token per target
after the previous target's audit is the one such path (a cycle there needs an
archive of a client with no active device, and resolves as one 40P01).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import Client, Site

CLIENT_ARCHIVED = "archived"


class SiteNotFound(LookupError):
    """No such site in the caller's view of this organisation: a 404."""


class ClientNotFound(LookupError):
    """No such client in the caller's view of this organisation: a 404."""


class NotAcceptingNewRows(Exception):
    """An archived site or client: a 409 whose `detail` is the sentence."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class SiteOfAnotherClient(Exception):
    """A site the caller can see, of another client than the row naming it:
    a 422 whose `detail` is the sentence (`site_of_client`)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def site_and_client_status(
    session: AsyncSession, site_id: UUID, *, org_id: UUID | None = None
) -> tuple[Site, str] | None:
    """The site as this session sees it, with its client's status -- or None.

    `org_id`, when given, mirrors RLS rather than relying on it, so a caller
    on the admin engine cannot reach another organisation's site.
    """
    query = select(Site, Client.status).join(Client, Client.id == Site.client_id)
    query = query.where(Site.id == site_id)
    if org_id is not None:
        query = query.where(Site.org_id == org_id)
    found = (await session.execute(query)).one_or_none()
    if found is None:
        return None
    return found[0], found[1]


def refuse_if_closed(site: Site, client_status: str) -> None:
    """`NotAcceptingNewRows` for an archived site, or a site of an archived
    client. Shared by every path that files or enrols something under a site
    (MSP clients Task 9: enrollment refuses both, whatever predates the
    archive), so the sentence is the same everywhere."""
    if site.archived_at is not None:
        raise NotAcceptingNewRows(tr("that site is archived"))
    if client_status == CLIENT_ARCHIVED:
        raise NotAcceptingNewRows(tr("that client is archived"))


async def site_for_new_rows(
    session: AsyncSession, *, org_id: UUID, site_id: UUID, for_update: bool = False
) -> Site:
    """The site a new row is filed under, with its client checked too --
    both LOCKED until commit (module docstring): the site FOR SHARE, or FOR NO
    KEY UPDATE when the caller changes the site row itself (`for_update`:
    editing or archiving it, its install link's networks) -- a FOR SHARE it
    then upgraded would deadlock two such callers -- and the client FOR SHARE.
    Read with `populate_existing`, so a site this session already holds is
    re-read from the locked row: what is checked is what was waited for.

    The `org_id` predicate mirrors RLS rather than relying on it, so a caller
    on the admin engine cannot file a row under another organisation's site.
    """
    query = (
        select(Site)
        .where(Site.id == site_id, Site.org_id == org_id)
        .execution_options(populate_existing=True)
    )
    query = (
        query.with_for_update(key_share=True) if for_update else query.with_for_update(read=True)
    )
    site = (await session.execute(query)).scalar_one_or_none()
    if site is None:
        raise SiteNotFound(site_id)
    client_status = (
        await session.execute(
            select(Client.status)
            .where(Client.id == site.client_id, Client.org_id == org_id)
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if client_status is None:
        raise SiteNotFound(site_id)
    refuse_if_closed(site, client_status)
    return site


async def client_for_new_rows(
    session: AsyncSession, *, org_id: UUID, client_id: UUID
) -> Client:
    """The client a new row is filed under, read FOR SHARE and held until
    commit (module docstring)."""
    client = (
        await session.execute(
            select(Client)
            .where(Client.id == client_id, Client.org_id == org_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if client is None:
        raise ClientNotFound(client_id)
    if client.status == CLIENT_ARCHIVED:
        raise NotAcceptingNewRows(tr("that client is archived"))
    return client


async def site_of_client(
    session: AsyncSession, *, org_id: UUID, site_id: UUID, client_id: UUID | None
) -> Site:
    """`site_for_new_rows` (FOR SHARE, site then client, held until commit),
    for a row that names a site: when the row already has a client,
    `client_id`, a site of another client is `SiteOfAnotherClient`. With
    `client_id` None the row takes the site's client (`site.client_id`)."""
    site = await site_for_new_rows(session, org_id=org_id, site_id=site_id)
    if client_id is not None and site.client_id != client_id:
        raise SiteOfAnotherClient(
            tr("site {site_id} belongs to another client", site_id=site_id)
        )
    return site


async def loaded_client(session: AsyncSession, row: Any) -> UUID:
    """`row.client_id`, read back if an UPDATE flush expired it. An H table's
    client is `server_onupdate` (its trigger may change it), so after any
    UPDATE of the row the attribute is unloaded -- and in an async session a
    lazy load is a `MissingGreenlet`, not a query."""
    if "client_id" in sa_inspect(row).unloaded:
        await session.refresh(row, ["client_id"])
    client_id: UUID = row.client_id
    return client_id
