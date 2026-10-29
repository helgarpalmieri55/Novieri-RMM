"""Optional modules per client (MSP clients spec §1.4, §3.2; plan
2026-10-26 Task 7). Budget, BCP and change management are on or off per
client (`clients.enabled_modules`). A WRITE for a client without the module
is a 404 -- the module does not exist for that client, the same answer as a
client outside the scope. Reads are not guarded: what a client recorded while
it had the module stays readable after the module is turned off (spec §3.2).

Called in the SERVICE, after the row's client is known -- from the body on a
create, from the loaded parent on every other write -- because a FastAPI
dependency runs before the parent row is read (research-5 §I).

`require_module` READS THE CLIENT FOR SHARE, held until commit, like
`client_sites.client_for_new_rows`: the "module on" it answers cannot go stale
under a write that switches the module off (`update_client` holds the row FOR
NO KEY UPDATE, which waits for this and the other way round). And it is the
FIRST lock of the write that calls it -- before any row of the module and
before the audit chain's -- which is the order an archive takes them in
(`client_sites` LOCK ORDER). So call it before writing anything.
`module_enabled` is the plain read, for paths that only decide (a gate, an
automatic change) and may run inside a sweep.
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import Client

Module = Literal["budget", "bcp", "change"]


class ModuleNotEnabled(LookupError):
    """A 404 whose `detail` is the sentence."""

    def __init__(self, module: Module) -> None:
        super().__init__(module)
        self.detail = tr("this client does not use that module")


async def _modules(
    session: AsyncSession, org_id: UUID, client_id: UUID, *, lock: bool
) -> list[str] | None:
    query = select(Client.enabled_modules).where(Client.id == client_id, Client.org_id == org_id)
    if lock:
        query = query.with_for_update(read=True)
    return (await session.execute(query)).scalar_one_or_none()


async def module_enabled(
    session: AsyncSession, *, org_id: UUID, client_id: UUID, module: Module
) -> bool:
    modules = await _modules(session, org_id, client_id, lock=False)
    return modules is not None and module in modules


async def require_module(
    session: AsyncSession, *, org_id: UUID, client_id: UUID, module: Module
) -> None:
    modules = await _modules(session, org_id, client_id, lock=True)
    if modules is None or module not in modules:
        raise ModuleNotEnabled(module)
