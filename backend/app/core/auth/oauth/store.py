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
tokens takes, in this order:

1. the person's `users` row FOR NO KEY UPDATE (`guards.lock_user_row`);
2. their advisory lock (`factors.lock_person`), when it takes one;
3. THE AI APP'S `oauth_clients` ROW, always by `clients.hold`: FOR KEY
   SHARE, re-read, the idle condition re-checked, nothing written. Before
   any grant row, because an idle app's purge (Task 6's `purge_idle`, Task
   13's retention) deletes the app row first and its grant rows after, by
   cascade: a writer that held a grant row and then waited for the app row
   would close a cycle with it (40P01). A metadata document's refresh of
   the row, if the transaction writes one, comes after the hold, never in
   its place: alone it would skip the re-check of a row the purge may have
   taken;
4. the person's codes, families and refresh tokens;
5. the app's use, `clients.touch`, after the grant rows and after any access
   token is signed (plan Task 7, decision 9), flushed before the chain;
6. the organization's audit chain, last.

A row found by its hash before step 1 (whose code? whose token?) is read
without a lock, and read again after step 3, before it is written: the purge
may have taken it meanwhile, and a write to a row gone is a stale-row 500.

REVOCATIONS TAKE NO APP ROW. A writer whose only statement on grant rows is
one UPDATE of families -- `grants.revoke_family`, `revoke_all_for_user`,
`revoke_app` (Tasks 7 and 10) -- with nothing after it but the audit chain,
skips steps 3 and 5: the account acts revoke a person's grants across every
app, and `revoke_app`'s "everyone" holds no person's row. An idle app's
purge, already holding the app row, may then wait IN ITS CASCADE for such a
writer's family rows. No cycle closes: after its UPDATE that writer waits
only for the chain, which the purge takes only after its cascade (the
revocation test in `tests/test_oauth_schema.py`). A transaction that holds
several apps takes them in id order and touches none of them.
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
    takes no lock: a caller that writes a person's rows takes, inside it, that
    person's `users` row first, the app's row (`clients.hold`) before any
    grant row, and the use (`clients.touch`) last before the chain -- or, a
    revocation, no app row at all (THE LOCK ORDER and its carve-out, above)."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        yield session
