"""Migration 0248 on a database of its own, stopped at 0247 (the pattern of
`tests/test_oauth_server_migration.py`; own authentication phase 4, Task 5).

upgrade drops `keycloak_event_ingest` with its policy, its index and its
unique key; downgrade puts back exactly what 0064 made -- the columns, the
FORCE RLS and its `tenant_isolation`, the key, the index and app_user's three
grants -- so the migrations job's round trip through 0064 still works.
"""

import secrets
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import PgCluster, _psql
from tests.test_remote_supervise_migration import _alembic

TABLE = "keycloak_event_ingest"


@pytest.fixture()
def db_at_0247(pg_cluster: PgCluster) -> Iterator[str]:
    dbname = f"its_kc_ledger_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    url = pg_cluster.admin_url.rsplit("/", 1)[0] + f"/{dbname}"
    try:
        _alembic(url, "upgrade", "0247")
        yield url
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def _shape(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as conn:
        present = (await conn.execute(text("SELECT to_regclass(:t)"), {"t": TABLE})).scalar_one()
        if present is None:
            return {"table": False}
        columns = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t ORDER BY column_name"
                ),
                {"t": TABLE},
            )
        ).all()
        rls = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"
                ),
                {"t": TABLE},
            )
        ).one()
        policies = set(
            (
                await conn.execute(
                    text("SELECT policyname FROM pg_policies WHERE tablename = :t"), {"t": TABLE}
                )
            ).scalars()
        )
        indexes = set(
            (
                await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": TABLE}
                )
            ).scalars()
        )
        grants = set(
            (
                await conn.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = :t AND grantee = 'app_user'"
                    ),
                    {"t": TABLE},
                )
            ).scalars()
        )
    return {
        "table": True,
        "columns": [tuple(row) for row in columns],
        "rls": tuple(rls),
        "policies": policies,
        "indexes": indexes,
        "grants": grants,
    }


async def test_0248_drops_the_ledger_and_round_trips(db_at_0247: str) -> None:
    engine = create_async_engine(db_at_0247)
    try:
        before = await _shape(engine)
        assert before["table"] is True
        assert before["policies"] == {"tenant_isolation"}
        assert before["rls"] == (True, True)
        assert {"ix_keycloak_event_age", "uq_keycloak_event_fingerprint"} <= set(
            before["indexes"]  # type: ignore[call-overload]
        )

        _alembic(db_at_0247, "upgrade", "0248")
        assert await _shape(engine) == {"table": False}

        _alembic(db_at_0247, "downgrade", "0247")
        assert await _shape(engine) == before

        _alembic(db_at_0247, "upgrade", "0248")
        assert await _shape(engine) == {"table": False}
    finally:
        await engine.dispose()
