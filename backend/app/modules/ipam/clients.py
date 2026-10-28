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

The client and the site are loaded as every new row's are
(`app/services/client_sites.py`): FOR SHARE, held until commit, as the
transaction's first locks -- before `ensure_default_vrf`'s insert, the
address plan's advisory lock and the audit chain's.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.ipam.errors import IpamClientError, IpamConflictError, IpamNotFoundError
from app.services.client_sites import (
    ClientNotFound,
    NotAcceptingNewRows,
    SiteNotFound,
    client_for_new_rows,
    site_for_new_rows,
)

__all__ = ["agreed_client", "client_for_new_row", "loaded_client", "site_of_client"]


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


async def client_for_new_row(session: AsyncSession, org_id: UUID, client_id: UUID) -> UUID:
    """`client_id`, once it may take a new IPAM row: visible (404), active
    (409) and not "Sin asignar" (422). Read FOR SHARE until commit."""
    try:
        client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
    except ClientNotFound:
        raise IpamNotFoundError(tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise IpamConflictError(exc.detail) from None
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise IpamClientError(tr("IPAM records cannot be filed under Unassigned"))
    return client.id


async def site_of_client(
    session: AsyncSession, org_id: UUID, site_id: UUID, client_id: UUID | None
) -> UUID:
    """The client of `site_id`, a site a new or edited row may name: visible
    (404), not archived and of an active client (409) -- read FOR SHARE until
    commit -- and, when `client_id` is given, that client's (422)."""
    try:
        site = await site_for_new_rows(session, org_id=org_id, site_id=site_id)
    except SiteNotFound:
        raise IpamNotFoundError(tr("site {site_id} not found", site_id=site_id)) from None
    except NotAcceptingNewRows as exc:
        raise IpamConflictError(exc.detail) from None
    if client_id is not None and site.client_id != client_id:
        raise IpamClientError(
            tr("site {site_id} belongs to another client", site_id=site_id)
        )
    return site.client_id


async def loaded_client(session: AsyncSession, row: Any) -> UUID:
    """`row.client_id`, read back if an UPDATE flush expired it. An H table's
    client is `server_onupdate` (the trigger may change it), so after any
    UPDATE of the row the attribute is unloaded -- and in an async session a
    lazy load is a `MissingGreenlet`, not a query."""
    if "client_id" in sa_inspect(row).unloaded:
        await session.refresh(row, ["client_id"])
    client_id: UUID = row.client_id
    return client_id
