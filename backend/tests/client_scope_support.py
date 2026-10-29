"""Clients, sites and per-client technicians for the client-scope tests, and
spec §2.2's client policies written once (not a test module)."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope_catalog import PHASE_1
from app.models import Client, Permission, Role, RolePermission, Site, User, UserRole
from tests.conftest import ApiEnv

#: Task 4's refusal for a write that needs a scope of every client.
EVERY_CLIENT = "only a technician with access to every client can do this"


async def client_with_site(
    env: ApiEnv,
    org: UUID,
    name: str | None = None,
    *,
    client_archived: bool = False,
    site_archived: bool = False,
) -> tuple[UUID, UUID]:
    """A customer client of `org` and its one site, as `(client_id, site_id)`."""
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        client = Client(
            org_id=org,
            name=name or f"Cliente {uuid4().hex[:6]}",
            kind="customer",
            status="archived" if client_archived else "active",
        )
        s.add(client)
        await s.flush()
        site = Site(
            org_id=org,
            client_id=client.id,
            name="Principal",
            archived_at=datetime.now(UTC) if site_archived else None,
        )
        s.add(site)
        await s.commit()
        return client.id, site.id


async def everything_on(env: ApiEnv, org: UUID, client_id: UUID) -> dict[str, str]:
    """Headers of a person holding EVERY permission code, on one client only."""
    sub = await env.create_user(org)
    async with AsyncSession(env.admin_engine) as s:
        user_id = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        role = Role(org_id=org, name=f"everything-{uuid4().hex[:8]}")
        s.add(role)
        await s.flush()
        for permission_id in (await s.execute(select(Permission.id))).scalars():
            s.add(RolePermission(role_id=role.id, permission_id=permission_id))
        s.add(UserRole(org_id=org, user_id=user_id, role_id=role.id, client_id=client_id))
        await s.commit()
    return await env.headers(org, sub)


async def make_technician(env: ApiEnv, org: UUID, user_id: UUID) -> None:
    """Give an existing user the built-in `technician` role on every client.
    A ticket's assignee holds `ticket.read` in the ticket's client (MSP
    clients plan 2026-10-26 Task 2), so a bare seeded user can no longer be
    assigned one."""
    async with AsyncSession(env.admin_engine) as s:
        role = (
            await s.execute(select(Role.id).where(Role.name == "technician", Role.org_id.is_(None)))
        ).scalar_one()
        s.add(UserRole(org_id=org, user_id=user_id, role_id=role, client_id=None))
        await s.commit()


# --------------------------------------------------------------------------- #
# Spec §2.2's client policies, for `tests/test_client_catalog.py` and
# `tests/test_client_isolation.py`.
# --------------------------------------------------------------------------- #

# THE EXPECTED POLICIES, WRITTEN ONCE. Spec §2.2 in SQL. Postgres deparses
# both these and the live policies (`deparse_policies`), so the comparison is
# exact and canonical: casts, parentheses and spelling come from the same
# deparser on both sides. Like `test_schema_drift.py`'s index comparison, a predicate
# that says the same thing in a different SHAPE fails -- a false failure,
# never a false pass.
_SCOPE = "current_setting('app.client_scope', true)"
_VISIBLE = "its_client_visible(client_id)"
# A NULL client is "every client": readable under a scope that names at
# least one client (spec §2.2: no scope, or an empty one, sees zero rows) ...
_NULL_PINNED = (
    f"(client_id IS NULL AND ({_SCOPE} = 'all' OR ({_SCOPE} = 'list' AND "
    "nullif(current_setting('app.current_clients', true), '') IS NOT NULL)))"
)
# ... and written, changed or deleted only under scope all.
_NULL_ALL = f"(client_id IS NULL AND {_SCOPE} = 'all')"

#: THE EXCEPTION, NAMED: the one O table whose `client_isolation` WITH CHECK
#: admits a NULL row under any pinned scope -- phase 1's client-less domain
#: events. `users` left it in Task 8: no narrowed path writes a staff row
#: (`PATCH /me` and the login bookkeeping run under scope all since Task 6b),
#: so a staff row is created, changed and deleted only under scope all, like
#: any other global row. Anything else here is a finding.
NULL_WRITABLE_FROM_ANY_SCOPE = frozenset({"domain_events"})

Shape = tuple[str, str | None, str | None]  # (cmd, USING, WITH CHECK) as written


def expected_client_policies(table: str) -> dict[str, Shape]:
    """Spec §2.2's client policies for `table`, by name, as written. Shared by
    `tests/test_client_catalog.py` and `tests/test_client_isolation.py`'s per-table
    sweep."""
    if table == "clients":
        return {
            "client_isolation": (
                "ALL",
                f"its_client_visible(id) AND (kind <> 'unassigned' OR {_SCOPE} = 'all')",
                f"{_SCOPE} = 'all' OR its_client_visible(id)",
            )
        }
    entry = PHASE_1[table]
    if entry.kind in {"C", "H"}:
        return {"client_isolation": ("ALL", _VISIBLE, _VISIBLE)}
    read = f"{_NULL_PINNED} OR {_VISIBLE}"
    modify = f"{_NULL_ALL} OR {_VISIBLE}"
    insert = read if table in NULL_WRITABLE_FROM_ANY_SCOPE else modify
    policies: dict[str, Shape] = {
        "client_isolation": ("ALL", read, insert),
        # `users` included since Task 8: a narrowed scope changes no column
        # of a staff row (`its_user_identity_guard` then holds a visible
        # contact's kind and external_subject; its status is its client's
        # since Task 9).
        "client_isolation_update": ("UPDATE", modify, None),
        "client_isolation_delete": ("DELETE", modify, None),
    }
    # CREATING a staff row is scope all (Task 6b, re-review M2): first-login
    # provisioning and `POST /users`, both every-client. Named on `users`
    # although `client_isolation`'s WITH CHECK now says the same. An INSERT
    # policy has no USING.
    if table == "users":
        policies["client_isolation_insert"] = ("INSERT", None, modify)
    return policies


async def deparse_policies(conn, shapes: set[Shape]) -> dict[Shape, tuple[str, str | None]]:
    """Each shape's (qual, with_check) as `pg_policies` prints it, from a
    policy built on a temporary table with the columns the predicates name.
    The caller rolls the transaction back."""
    await conn.execute(
        text("CREATE TEMP TABLE policy_probe (id uuid, client_id uuid, kind varchar(20))")
    )
    names: dict[str, Shape] = {}
    for n, (cmd, using, check) in enumerate(sorted(shapes, key=repr)):
        names[f"p{n}"] = (cmd, using, check)
        using_ = f" USING ({using})" if using is not None else ""
        with_check = f" WITH CHECK ({check})" if check is not None else ""
        await conn.execute(
            text(
                f"CREATE POLICY p{n} ON policy_probe AS RESTRICTIVE FOR {cmd}"
                f"{using_}{with_check}"
            )
        )
    rows = (
        await conn.execute(
            text(
                "SELECT policyname, qual, with_check FROM pg_policies "
                "WHERE tablename = 'policy_probe' AND schemaname LIKE 'pg_temp%'"
            )
        )
    ).all()
    return {names[r.policyname]: (r.qual, r.with_check) for r in rows}


async def internal_client(env: ApiEnv, headers: dict[str, str]) -> str:
    """The internal client of whoever `headers` sign in as: `/me`'s `org_id`,
    the id convention -- what the console sends until phase 3's selector
    (`frontend/src/lib/internalClient.ts`). For helpers that build a create
    body from headers alone."""
    me = await env.client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200, me.text
    return str(me.json()["org_id"])


async def drop_client_keys(session: AsyncSession, table: str, *constraints: str) -> None:
    """Drop `constraints` of `table` INSIDE `session`'s transaction, which the
    caller rolls back. For the org-predicate tests (plan 2026-10-26 Task 8,
    lesson 2): a row of org B under org A's client is what a query's org
    predicate alone must keep out, and the client keys (`fk_<table>_client_org`
    and the like) make such a row impossible -- which is also why, once a
    query filters by client, its org predicate can only be shown to bite with
    them gone. DDL is transactional in Postgres: nothing outlives the
    rollback."""
    for name in constraints:
        await session.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {name}"))


#: Some session other than the holder's (`:me`) is waiting for a `clients`
#: row the holder locked. Postgres shows that wait in two rows: the waiter
#: takes the row's TUPLE lock on `clients` (granted) and then waits for the
#: holder's TRANSACTION (a `transactionid` lock, not granted) -- so a filter
#: on `relation = 'clients' AND NOT granted` sees nothing. A second waiter
#: would queue on the tuple lock itself, not granted; that counts too.
WAITS_ON_A_CLIENT_ROW = text(
    "SELECT count(*) FROM pg_locks t "
    "WHERE t.locktype = 'tuple' AND t.relation = 'clients'::regclass AND t.pid <> :me "
    "AND (NOT t.granted OR EXISTS ("
    "  SELECT 1 FROM pg_locks w JOIN pg_locks h ON h.transactionid = w.transactionid "
    "  WHERE w.pid = t.pid AND w.locktype = 'transactionid' AND NOT w.granted "
    "  AND h.pid = :me AND h.locktype = 'transactionid' AND h.granted))"
)


async def wait_until_blocked(observer: AsyncSession, holder_pid: int) -> bool:
    """Whether, within ten seconds, a session waits on a `clients` row the
    holder (`holder_pid`) locked: for the lock-order tests (plan 2026-10-26
    Tasks 9 and 14)."""
    for _ in range(400):
        await asyncio.sleep(0.025)
        waiting = (
            await observer.execute(WAITS_ON_A_CLIENT_ROW, {"me": holder_pid})
        ).scalar_one()
        await observer.rollback()
        if waiting:
            return True
    return False
