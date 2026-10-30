"""The lock order of a push that mints enrollment tokens (research-4 §I5;
plan 2026-10-26 Task 12).

Minting a target's token files it under the target's site, which locks that
site and its client FOR SHARE (`app/services/client_sites.py`, LOCK ORDER).
A push mints one per target, and the audit chain's lock is taken by the
FIRST audit event of the transaction -- the previous target's
`push.dispatched`, a pacer's `push.refused`, a run's `push.run.started` or
`push.run.resumed`. Taken one target at a time, the second target's site and
client would come AFTER the chain, while `archive_site` (the site, then its
client, then the chain) and `archive_client` (the client, then the chain)
take them before it: a cycle Postgres resolves as one 40P01.

`lock_target_scope` takes them all first, sites then clients, each set
SORTED, FOR SHARE -- the order `archive_site` takes them in -- so a push that
meets an archive waits for it before it holds the chain. The mint takes the
same rows again later, which a lock this transaction holds never waits for.

What it cannot cover: a target device moved to another site AFTER this read
and before its mint would lock that new site late. Devices move only inside
their client, and the window is one transaction; such a cycle would still
resolve as one 40P01, never as a wrong push.
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Device, Site

__all__ = ["lock_target_scope", "sites_of"]


async def sites_of(session: AsyncSession, org_id: UUID, device_ids: Iterable[UUID]) -> set[UUID]:
    """The sites `device_ids` are filed under, in one statement."""
    ids = sorted(set(device_ids))
    if not ids:
        return set()
    return set(
        (
            await session.execute(
                select(Device.site_id).where(Device.org_id == org_id, Device.id.in_(ids))
            )
        ).scalars()
    )


async def lock_target_scope(session: AsyncSession, org_id: UUID, site_ids: Iterable[UUID]) -> None:
    """Every site, then every client, the batch will mint tokens under, FOR
    SHARE and SORTED, before the first audit write: `archive_site` takes the
    site then the client, and `archive_client` the client then the audit
    chain (`app/services/client_sites.py` LOCK ORDER). Taken one target at a
    time, after the previous target's audit, they would invert that order."""
    ids = sorted(set(site_ids))
    if not ids:
        return
    rows = (
        await session.execute(
            select(Site.id, Site.client_id)
            .where(Site.org_id == org_id, Site.id.in_(ids))
            .order_by(Site.id)
            .with_for_update(read=True)
        )
    ).all()
    clients = sorted({client for _, client in rows})
    if not clients:
        return
    await session.execute(
        select(Client.id)
        .where(Client.org_id == org_id, Client.id.in_(clients))
        .order_by(Client.id)
        .with_for_update(read=True)
    )
