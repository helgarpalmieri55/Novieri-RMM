"""Migration 0248 on a database of its own, stopped at 0247 (the pattern of
`tests/test_oauth_server_migration.py`; own authentication phase 4, Task 5).

upgrade drops `keycloak_event_ingest` with its policy, its index and its
unique key; downgrade puts back exactly what 0064 made, so the migrations
job's round trip through 0064 still works. "Exactly" is what `_shape` reads
from the catalogs, and it is everything a table carries here: each column
with its type, nullability, length and default; every constraint as
`pg_get_constraintdef` prints it (the primary key, the foreign key to
`organizations`, the unique key); ENABLE and FORCE RLS; each policy with its
command, roles, `USING` and `WITH CHECK`; each index as `pg_indexes` defines
it; the owner; and every grant, to every grantee.
"""

import secrets
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

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


async def _rows(conn: AsyncConnection, sql: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in (await conn.execute(text(sql), {"t": TABLE})).all()]


async def _shape(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as conn:
        present = (await conn.execute(text("SELECT to_regclass(:t)"), {"t": TABLE})).scalar_one()
        if present is None:
            return {"table": False}
        return {
            "table": True,
            "columns": await _rows(
                conn,
                "SELECT column_name, data_type, is_nullable, character_maximum_length, "
                "column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t ORDER BY column_name",
            ),
            "constraints": set(
                await _rows(
                    conn,
                    "SELECT conname, CAST(contype AS text), pg_get_constraintdef(oid) "
                    "FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)",
                )
            ),
            "rls": tuple(
                *await _rows(
                    conn,
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = CAST(:t AS regclass)",
                )
            ),
            "policies": set(
                await _rows(
                    conn,
                    "SELECT policyname, permissive, CAST(roles AS text), cmd, qual, with_check "
                    "FROM pg_policies WHERE schemaname = 'public' AND tablename = :t",
                )
            ),
            "indexes": set(
                await _rows(
                    conn,
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = :t",
                )
            ),
            "owner": await _rows(
                conn,
                "SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = :t",
            ),
            "grants": set(
                await _rows(
                    conn,
                    "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'public' AND table_name = :t",
                )
            ),
        }


async def test_0248_drops_the_ledger_and_round_trips(db_at_0247: str) -> None:
    engine = create_async_engine(db_at_0247)
    try:
        before = await _shape(engine)
        # Non-vacuity: the shape 0064 left really holds what the docstring
        # says is compared, so an empty read cannot make the equality below
        # pass.
        assert before["table"] is True
        assert before["rls"] == (True, True)
        assert before["owner"] == [("its_admin",)]
        constraints: Any = before["constraints"]
        assert {(name, kind) for name, kind, _ in constraints} >= {
            ("uq_keycloak_event_fingerprint", "u")
        }
        assert {kind for _, kind, _ in constraints} == {"p", "f", "u"}
        assert any(
            definition == "FOREIGN KEY (org_id) REFERENCES organizations(id)"
            for _, kind, definition in constraints
            if kind == "f"
        )
        policies: Any = before["policies"]
        ((name, _, _, cmd, using, check),) = policies
        assert (name, cmd) == ("tenant_isolation", "ALL")
        assert using == check and "current_setting('app.current_org'" in str(using)
        indexes: Any = before["indexes"]
        assert {name for name, _ in indexes} >= {
            "ix_keycloak_event_age",
            "uq_keycloak_event_fingerprint",
        }
        grants: Any = before["grants"]
        assert {("app_user", p) for p in ("SELECT", "INSERT", "DELETE")} <= grants
        columns: Any = before["columns"]
        defaults = {name: default for name, _, _, _, default in columns}
        assert defaults["id"] == "gen_random_uuid()" and defaults["ingested_at"] == "now()"

        _alembic(db_at_0247, "upgrade", "0248")
        assert await _shape(engine) == {"table": False}

        _alembic(db_at_0247, "downgrade", "0247")
        assert await _shape(engine) == before

        _alembic(db_at_0247, "upgrade", "0248")
        assert await _shape(engine) == {"table": False}
    finally:
        await engine.dispose()
