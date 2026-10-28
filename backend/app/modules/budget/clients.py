"""The client a budget row belongs to (MSP clients plan 2026-10-26 Task 8).

Settings, categories, cost centers, budgets and their lines, and expenses are
ONE client's (migration 0238), and budget is an optional module per client
(spec §1.4). So:

- A NEW row names its client, loaded like every new row's
  (`client_for_new_budget_rows`): one this session cannot see is a 404, never
  a 403 (spec §3.2's existence oracle); an archived one a 409; "Sin asignar"
  a 422 (C10); and then the module.
- Every other WRITE takes its client from the row it changes and calls
  `require_module` with it BEFORE anything else -- the client read FOR SHARE
  is the write's first lock, before the audit chain's, which is the order an
  archive takes them in (Task 7).
- A READ that is one client's by nature -- the settings, the forecast, the
  exports, each in that client's base currency -- names a client this session
  can see (`visible_client`), else the same 404 as a client that does not
  exist.
- What a row POINTS AT is of the row's client (`same_client`): another
  client's category, cost center or linked record is a 422. A missing one is
  the 404 it always was, and a row this session cannot see is missing.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import Client
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.budget.errors import (
    BudgetClientError,
    BudgetConflictError,
    BudgetNotFoundError,
)
from app.services.client_modules import require_module
from app.services.client_sites import ClientNotFound, NotAcceptingNewRows, client_for_new_rows


async def client_for_new_budget_rows(session: AsyncSession, org_id: UUID, client_id: UUID) -> None:
    """The client new budget rows are filed under (module docstring), held
    FOR SHARE until commit -- and it must use budget."""
    try:
        client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
    except ClientNotFound:
        raise BudgetNotFoundError(tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise BudgetConflictError(exc.detail) from None
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise BudgetClientError(tr("nothing is budgeted under Unassigned"))
    await require_budget(session, org_id, client_id)


async def require_budget(session: AsyncSession, org_id: UUID, client_id: UUID) -> None:
    """`ModuleNotEnabled` (404) unless `client_id` uses budget. Call it before
    anything is written (module docstring)."""
    await require_module(session, org_id=org_id, client_id=client_id, module="budget")


async def visible_client(session: AsyncSession, org_id: UUID, client_id: UUID) -> None:
    """A client this session can see, archived or not, else the 404 of one
    that does not exist. The `org_id` predicate mirrors RLS rather than
    relying on it."""
    found = (
        await session.execute(
            select(Client.id).where(Client.id == client_id, Client.org_id == org_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise BudgetNotFoundError(tr("no such client"))


def same_client(row_client: UUID, client_id: UUID, refusal: str) -> None:
    """`refusal` (a 422) unless the referenced row is of `client_id`."""
    if row_client != client_id:
        raise BudgetClientError(refusal)
