"""Tenant context: the only way to obtain a DB session for tenant-scoped work.

org_session() opens a transaction and pins the RLS tenant GUC **and the client
scope** (`app.client_scope`, `app.current_clients`; MSP clients spec §2.2) with
SET LOCAL, so every statement in the block is constrained to that organization,
and to the clients the caller may see, by the database itself. admin_session()
is the explicit, audited cross-org path. portal_session() is the row-scoped
path for end-user-portal requests -- it connects as `portal_user` and pins the
org, requester and client-scope GUCs.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core import db
from app.core.client_scope import ClientScope


@asynccontextmanager
async def org_session(
    org_id: UUID, *, clients: ClientScope, engine: AsyncEngine | None = None
) -> AsyncIterator[AsyncSession]:
    engine = engine or db.get_engine()
    scope, ids = clients.guc_values()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.current_org', :org, true), "
                    "set_config('app.client_scope', :scope, true), "
                    "set_config('app.current_clients', :ids, true)"
                ),
                {"org": str(org_id), "scope": scope, "ids": ids},
            )
            yield session


@asynccontextmanager
async def admin_session(*, engine: AsyncEngine | None = None) -> AsyncIterator[AsyncSession]:
    """Cross-org session (RLS-bypassing role). Callers MUST write an audit event
    for anything they change through this path."""
    engine = engine or db.get_admin_engine()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            yield session


@asynccontextmanager
async def portal_session(
    org_id: UUID,
    requester_id: UUID,
    *,
    clients: ClientScope,
    engine: AsyncEngine | None = None,
) -> AsyncIterator[AsyncSession]:
    """The ONLY way a portal request may reach the database.

    Connects as `portal_user` and pins BOTH tenant GUCs with SET LOCAL, so the
    database itself constrains every statement in the block to one
    organization AND one requester.

    Both GUCs are load-bearing. `portal_user`'s policies compare against each
    of them; if either is unset, `current_setting(..., true)` returns NULL, the
    comparison is NULL rather than true, and no row matches. A bug that forgets
    the requester context therefore yields an EMPTY portal, never another
    person's tickets.

    THE CLIENT GUCs ARE THE THIRD PIN (MSP clients spec §2.3): a contact is
    fixed to their own client, so a ticket that somehow carried their
    requester id under another client still does not show.

    A portal route that used `org_session` instead would connect as `app_user`
    and see the whole org -- `tests/test_portal_api.py` enforces that no portal
    route does this.
    """
    engine = engine or db.get_portal_engine()
    scope, ids = clients.guc_values()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.current_org', :org, true), "
                    "set_config('app.current_requester', :req, true), "
                    "set_config('app.client_scope', :scope, true), "
                    "set_config('app.current_clients', :ids, true)"
                ),
                {"org": str(org_id), "req": str(requester_id), "scope": scope, "ids": ids},
            )
            yield session
