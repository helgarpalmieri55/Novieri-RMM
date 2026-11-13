"""Migration 0247 on a database of its own, stopped at 0246 (the pattern of
`tests/test_auth_core_migration.py`; plan 2026-11-06, Task 5).

upgrade -> downgrade -> upgrade leaves nothing behind: the four tables, their
`tenant_isolation` and `client_isolation*` policies, and the three
`its_client_to_oauth_*` triggers that live on `users`, not on the tables they
serve -- the one thing CI's `downgrade base` cannot see, because it drops
`users` too.
"""

import secrets
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import PgCluster, _psql
from tests.test_remote_supervise_migration import _alembic

TABLES = ("oauth_clients", "oauth_refresh_families", "oauth_refresh_tokens", "oauth_codes")
PERSON_TABLES = TABLES[1:]
POLICIES = {(t, "tenant_isolation") for t in TABLES} | {
    (t, p)
    for t in PERSON_TABLES
    for p in ("client_isolation", "client_isolation_update", "client_isolation_delete")
}


@pytest.fixture()
def db_at_0246(pg_cluster: PgCluster) -> Iterator[str]:
    dbname = f"its_oauth_server_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    url = pg_cluster.admin_url.rsplit("/", 1)[0] + f"/{dbname}"
    try:
        _alembic(url, "upgrade", "0246")
        yield url
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def _schema(engine: AsyncEngine) -> dict[str, set[object]]:
    async with engine.connect() as conn:
        tables = {
            t
            for t in TABLES
            if (await conn.execute(text("SELECT to_regclass(:t)"), {"t": t})).scalar_one()
        }
        policies = {
            (row.tablename, row.policyname)
            for row in (
                await conn.execute(
                    text(
                        "SELECT tablename, policyname FROM pg_policies "
                        "WHERE tablename = ANY(:names)"
                    ),
                    {"names": list(TABLES)},
                )
            ).all()
        }
        triggers = set(
            (
                await conn.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'users'::regclass "
                        "AND tgname LIKE 'its\\_client\\_to\\_oauth\\_%'"
                    )
                )
            ).scalars()
        )
    return {"tables": tables, "policies": policies, "triggers": triggers}


async def test_0247_round_trips(db_at_0246: str) -> None:
    engine = create_async_engine(db_at_0246)
    present = {
        "tables": set(TABLES),
        "policies": POLICIES,
        "triggers": {f"its_client_to_{t}" for t in PERSON_TABLES},
    }
    absent: dict[str, set[object]] = {"tables": set(), "policies": set(), "triggers": set()}
    try:
        _alembic(db_at_0246, "upgrade", "0247")
        assert await _schema(engine) == present
        _alembic(db_at_0246, "downgrade", "0246")
        assert await _schema(engine) == absent
        _alembic(db_at_0246, "upgrade", "0247")
        assert await _schema(engine) == present
    finally:
        await engine.dispose()
