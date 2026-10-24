"""Writing a device the client guard of migration 0231 refuses (not a test module).

SINCE 0231 THE SCHEMA REFUSES a device naming a group of another client
(`its_device_client_guard`) -- and so a device naming another ORGANISATION'S
group, whose clients are not its own. Several tenancy tests pin a service's own
`org_id` predicate with exactly that row: "their device naming my group", the
row that used to be the one nothing in the schema forbade. They are not about
the trigger. The predicate is still what keeps such a row out of a count or an
expansion wherever one exists -- older than the trigger, or written around it
-- so those tests write it around the trigger, with this.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def past_the_client_guard(session: AsyncSession) -> AsyncIterator[None]:
    """`its_device_client_guard` off for what the caller adds or changes inside
    the block, which is flushed before the trigger comes back on.

    The session must be one whose role owns `devices` (the admin engine's
    `its_admin`). Both statements are in the caller's transaction, so the DDL
    rolls back with it if anything in between fails.
    """
    await session.execute(text("ALTER TABLE devices DISABLE TRIGGER its_device_client_guard"))
    yield
    await session.flush()
    await session.execute(text("ALTER TABLE devices ENABLE TRIGGER its_device_client_guard"))
