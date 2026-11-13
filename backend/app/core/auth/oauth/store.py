"""Where the OAuth server's rows are read and written (design §5, §6.1-§6.2;
plan D3).

THE ORGANIZATION OF A REQUEST WITH NO PERSON IS THE DEPLOYMENT'S. A
registration, a token request and a revocation arrive with no cookie and no
person; the only organization they can belong to is the one this
installation signs people in to, `ITS_DEFAULT_ORG_SLUG` -- the rule
`flows.login` follows (phase 1). Its id is read through
`lookup.org_of_slug`, migration 0246's SECURITY DEFINER `its_auth_org`, on
the application engine: design §6.2, "El único camino es una función
SECURITY DEFINER". No admin engine anywhere in this package
(`tests/test_oauth_schema.py`). A slug that names no organization is None,
and the routes answer 503.

EVERY CLIENT, AND ONLY HERE. A technician's grant rows are global rows (O
under `users`, client NULL) that only an every-client scope may write
(migration 0231), and they are read by a hash before anybody's scope exists.
`oauth_session` is the one function that opens that scope over them
(`ALL_SCOPE_ALLOWLIST`), and the files that call it are listed
(`tests/test_client_catalog.py::OAUTH_SESSION_USERS`); a route file never
does.

THE LOCK ORDER IS THE CALLER'S, AND THE PERSON'S ROW COMES FIRST (own
authentication phase 2's rule, `guards.lock_user_row`; operator decision D2
for phase 3). Nothing here takes a lock: `oauth_session` pins the
organization and the scope, and `deployment_org` reads on a connection of
its own. A transaction that writes a person's codes, families or refresh
tokens takes, in order, their `users` row FOR NO KEY UPDATE
(`guards.lock_user_row`), their advisory lock (`factors.lock_person`) when it
needs one, then their rows, and the organization's audit chain last. A row
found by its hash before that (whose code? whose token?) is read without a
lock, and read again after the person's row is held, before it is written.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.auth.lookup import org_of_slug
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session


async def deployment_org(app: Any) -> UUID | None:
    slug = app.state.settings.default_org_slug
    if not slug:
        return None
    return await org_of_slug(app.state.app_engine, slug)


@asynccontextmanager
async def oauth_session(org_id: UUID, engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One transaction in `org_id` over every client (module docstring). It
    takes no lock: a caller that writes a person's rows locks that person's
    `users` row first, inside it (THE LOCK ORDER, above)."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        yield session
