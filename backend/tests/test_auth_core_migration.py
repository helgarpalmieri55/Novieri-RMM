"""Migration 0245 on a database of its own, stopped at 0244 (the
`test_remote_supervise_migration.py` pattern): the session database is at
head, where two technicians sharing an address can no longer be seeded.

- The upgrade refuses, BY NAME, an organization where two technicians share an
  address, instead of dying on the unique index (plan decision D2).
- upgrade -> downgrade -> upgrade leaves nothing behind: the eleven tables, the
  two functions, the staff index and the nine `its_client_to_*` triggers that
  live on `users`, not on the tables they serve.
"""

import secrets
import subprocess
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import PgCluster, _psql
from tests.test_remote_supervise_migration import _alembic

TABLES = (
    "auth_passwords",
    "auth_totp",
    "auth_passkeys",
    "auth_recovery_codes",
    "auth_trusted_devices",
    "auth_sessions",
    "auth_failures",
    "auth_action_tokens",
    "auth_webauthn_challenges",
    "auth_org_settings",
    "account_mail_settings",
)
FUNCTIONS = ("its_auth_principal(text,text)", "its_auth_session(bytea)")


@pytest.fixture()
def db_at_0244(pg_cluster: PgCluster) -> Iterator[str]:
    dbname = f"its_auth_core_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    url = pg_cluster.admin_url.rsplit("/", 1)[0] + f"/{dbname}"
    try:
        _alembic(url, "upgrade", "0244")
        yield url
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def _schema(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as conn:
        tables = {
            t
            for t in TABLES
            if (await conn.execute(text("SELECT to_regclass(:t)"), {"t": t})).scalar_one()
        }
        functions = {
            f
            for f in FUNCTIONS
            if (await conn.execute(text("SELECT to_regprocedure(:f)"), {"f": f})).scalar_one()
        }
        index = (
            await conn.execute(text("SELECT to_regclass('ux_users_org_lower_email_staff')"))
        ).scalar_one()
        triggers = set(
            (
                await conn.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'users'::regclass "
                        "AND tgname LIKE 'its\\_client\\_to\\_auth\\_%'"
                    )
                )
            ).scalars()
        )
    return {"tables": tables, "functions": functions, "index": index, "triggers": triggers}


async def _technician(engine: AsyncEngine, org: object, email: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, org_id, external_subject, email, display_name, status) "
                "VALUES (:id, :org, :sub, :email, '', 'active')"
            ),
            {"id": uuid4(), "org": org, "sub": uuid4().hex, "email": email},
        )


async def test_0245_refuses_two_technicians_sharing_an_address(db_at_0244: str) -> None:
    org = uuid4()
    engine = create_async_engine(db_at_0244)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, status, settings) "
                    "VALUES (:id, 'shared', :slug, 'active', '{}')"
                ),
                {"id": org, "slug": f"shared-{org.hex[:8]}"},
            )
        await _technician(engine, org, "ana@acme.test")
        await _technician(engine, org, "ANA@acme.test")

        with pytest.raises(subprocess.CalledProcessError) as refused:
            _alembic(db_at_0244, "upgrade", "0245")
        assert "1 staff address(es) are shared by two technicians" in refused.value.stderr

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE email = 'ANA@acme.test'"))
        _alembic(db_at_0244, "upgrade", "0245")
        assert (await _schema(engine))["index"] is not None
    finally:
        await engine.dispose()


async def test_0245_round_trips(db_at_0244: str) -> None:
    engine = create_async_engine(db_at_0244)
    present = {
        "tables": set(TABLES),
        "functions": set(FUNCTIONS),
        "index": "ux_users_org_lower_email_staff",
        "triggers": {
            f"its_client_to_{t}"
            for t in TABLES
            if t not in ("auth_org_settings", "account_mail_settings")
        },
    }
    absent = {"tables": set(), "functions": set(), "index": None, "triggers": set()}
    try:
        _alembic(db_at_0244, "upgrade", "0245")
        assert await _schema(engine) == present
        _alembic(db_at_0244, "downgrade", "0244")
        assert await _schema(engine) == absent
        _alembic(db_at_0244, "upgrade", "0245")
        assert await _schema(engine) == present
    finally:
        await engine.dispose()
