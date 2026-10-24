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
"""

from uuid import UUID

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


async def site_for_new_rows(session: AsyncSession, *, org_id: UUID, site_id: UUID) -> Site:
    """The site a new row is filed under, with its client checked too.

    The `org_id` predicate mirrors RLS rather than relying on it, so a caller
    on the admin engine cannot file a row under another organisation's site.
    """
    found = await site_and_client_status(session, site_id, org_id=org_id)
    if found is None:
        raise SiteNotFound(site_id)
    site, client_status = found
    refuse_if_closed(site, client_status)
    return site


async def client_for_new_rows(
    session: AsyncSession, *, org_id: UUID, client_id: UUID
) -> Client:
    """The client a new row is filed under."""
    client = (
        await session.execute(
            select(Client).where(Client.id == client_id, Client.org_id == org_id)
        )
    ).scalar_one_or_none()
    if client is None:
        raise ClientNotFound(client_id)
    if client.status == CLIENT_ARCHIVED:
        raise NotAcceptingNewRows(tr("that client is archived"))
    return client
