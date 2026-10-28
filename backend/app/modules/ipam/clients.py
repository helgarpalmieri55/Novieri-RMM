"""The client of a new IPAM row (MSP clients plan 2026-10-26 Task 10).

Every IPAM row belongs to a client (migration 0240). A row takes it from its
parent when it has one -- a subnet, an address or a scan authorization from
its VRF, a VLAN from its site -- and only a row with no parent names it in the
request: a VRF always, a VLAN without a site, and a subnet, address or scan
authorization created in its client's `global` VRF. A body client that
contradicts the parent's is a 422 (`IpamClientError`); a client the caller
cannot see is a 404, as a missing one is (spec §3.2's existence oracle); an
archived one a 409; "Sin asignar" a 422 (C10). The site or VLAN a row names is
of the row's client, a 422 before the composite key would say so.

The lookups are the shared ones (`app/services/client_sites.py`); this module
maps their refusals to IPAM's errors. A row's trigger-written client is read
back with `client_sites.loaded_client`.

LOCK ORDER. The client and the site are read FOR SHARE and held until commit,
as every new row's are -- but not always first, and not always site first:

* a VRF, or a VLAN without a site: the client;
* a VLAN at a site: the site, then its client (`site_for_new_rows`'s order),
  then the client again;
* a subnet, an address or a scan authorization: the client
  (`registry.vrf_for_new_row`) -- after which, without a VRF, the client's
  `global` VRF may already have been INSERTED (`ensure_default_vrf`) -- then,
  for a subnet, its site and the client again; then the address plan's
  advisory lock, and the audit chain's last.

None of these can deadlock against `archive_site` (the site FOR NO KEY
UPDATE, then its client FOR SHARE) or `archive_client` (the client FOR NO KEY
UPDATE, its first lock). Every client lock here, and `archive_site`'s, is FOR
SHARE, which conflicts neither with another FOR SHARE nor with the KEY SHARE
the VRF insert's foreign key takes: `archive_site` never waits on an IPAM
write. `archive_client` holds nothing before the client and never locks a
site. Neither archive touches a VRF row or the advisory lock, and an IPAM
write waits for its site before it takes the audit chain. A write that meets
an archive in flight waits for it, re-reads and gets the 409.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.ipam.errors import IpamClientError, IpamConflictError, IpamNotFoundError
from app.services.client_sites import (
    ClientNotFound,
    NotAcceptingNewRows,
    SiteNotFound,
    SiteOfAnotherClient,
    client_for_new_rows,
    site_of_client,
)

__all__ = ["agreed_client", "client_for_ipam_row", "site_for_ipam_row"]


def agreed_client(derived: UUID | None, claimed: UUID | None) -> UUID:
    """The client a new row is filed under: `derived` from its parent when it
    has one, which a `claimed` one in the body must equal; else `claimed`,
    which is then required."""
    if derived is None:
        if claimed is None:
            raise IpamClientError(
                tr("name the client: a row with no VRF or site takes the one the request names")
            )
        return claimed
    if claimed is not None and claimed != derived:
        raise IpamClientError(
            tr(
                "client_id {claimed} contradicts the client of the VRF or site named",
                claimed=claimed,
            )
        )
    return derived


async def client_for_ipam_row(session: AsyncSession, org_id: UUID, client_id: UUID) -> UUID:
    """`client_id`, once it may take a new IPAM row: visible (404), active
    (409) and not "Sin asignar" (422). Read FOR SHARE until commit
    (`client_sites.client_for_new_rows`)."""
    try:
        client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
    except ClientNotFound:
        raise IpamNotFoundError(tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise IpamConflictError(exc.detail) from None
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise IpamClientError(tr("IPAM records cannot be filed under Unassigned"))
    return client.id


async def site_for_ipam_row(
    session: AsyncSession, org_id: UUID, site_id: UUID, client_id: UUID | None
) -> UUID:
    """The client of `site_id`, a site a new or edited IPAM row may name:
    visible (404), not archived and of an active client (409) -- read FOR
    SHARE until commit -- and, when `client_id` is given, that client's (422).
    `client_sites.site_of_client`, in IPAM's errors."""
    try:
        site = await site_of_client(
            session, org_id=org_id, site_id=site_id, client_id=client_id
        )
    except SiteNotFound:
        raise IpamNotFoundError(tr("site {site_id} not found", site_id=site_id)) from None
    except NotAcceptingNewRows as exc:
        raise IpamConflictError(exc.detail) from None
    except SiteOfAnotherClient as exc:
        raise IpamClientError(exc.detail) from None
    return site.client_id
