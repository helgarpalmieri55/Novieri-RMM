"""Migration 0244 (phase 3 design §5.3): `remote.supervise` goes to every
role that holds `orgs.manage` when it runs -- the global `super-admin` and
`org-admin`, and a role an organisation made itself (the copy-on-write clone
of `org-admin` a role edit makes is one) -- and to no other. Its downgrade
takes the code and every grant of it away and leaves `orgs.manage` as it was;
a second upgrade grants again.

On a throwaway database stopped at 0243 (the `test_people_migration.py`
pattern): the session database is at head, where an organisation's own role
made BEFORE the upgrade can no longer be seeded.
"""

import os
import secrets
import subprocess
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import BACKEND_DIR, PgCluster, _psql

CODE = "remote.supervise"


def _alembic(url: str, *args: str) -> None:
    subprocess.run(
        [str(BACKEND_DIR / ".venv" / "bin" / "alembic"), *args],
        cwd=BACKEND_DIR,
        env=os.environ | {"ITS_ALEMBIC_URL": url},
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def db_at_0243(pg_cluster: PgCluster) -> Iterator[str]:
    """A database of its own at 0243, owned by `its_admin`; `pg_cluster`
    guarantees the roles the migrations grant to exist."""
    dbname = f"its_supervise_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    url = pg_cluster.admin_url.rsplit("/", 1)[0] + f"/{dbname}"
    try:
        _alembic(url, "upgrade", "0243")
        yield url
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def _holders(engine: AsyncEngine, code: str) -> set[tuple[str, UUID | None]]:
    """(role name, the role's organisation) of every role granted `code`."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.name, r.org_id FROM roles r "
                "JOIN role_permissions rp ON rp.role_id = r.id "
                "JOIN permissions p ON p.id = rp.permission_id WHERE p.code = :code"
            ),
            {"code": code},
        )
        return {(row.name, row.org_id) for row in rows}


async def test_0244_grants_remote_supervise_wherever_orgs_manage_is_held(
    db_at_0243: str,
) -> None:
    org, leads, desk = uuid4(), uuid4(), uuid4()
    engine = create_async_engine(db_at_0243)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, status, settings)"
                    " VALUES (:id, 'supervise', :slug, 'active', '{}')"
                ),
                {"id": org, "slug": f"sup-{org.hex[:8]}"},
            )
            # The organisation's own administrators, and -- the control -- a
            # role of its own that drives sessions but holds no `orgs.manage`.
            for role_id, name, codes in (
                (leads, "service-desk-leads", ["orgs.manage", "remote.control"]),
                (desk, "service-desk", ["remote.control"]),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO roles (id, org_id, name, description)"
                        " VALUES (:id, :org, :name, '')"
                    ),
                    {"id": role_id, "org": org, "name": name},
                )
                await conn.execute(
                    text(
                        "INSERT INTO role_permissions (role_id, permission_id)"
                        " SELECT :role, id FROM permissions"
                        " WHERE code = ANY(CAST(:codes AS text[]))"
                    ),
                    {"role": role_id, "codes": codes},
                )
        before = await _holders(engine, "orgs.manage")
        assert before == {
            ("super-admin", None), ("org-admin", None), ("service-desk-leads", org)
        }

        _alembic(db_at_0243, "upgrade", "0244")
        assert await _holders(engine, CODE) == before
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT module, action FROM permissions WHERE code = :c"), {"c": CODE}
                )
            ).one()
        assert (row.module, row.action) == ("remote", "supervise")

        _alembic(db_at_0243, "downgrade", "0243")
        async with engine.connect() as conn:
            left = (
                await conn.execute(
                    text("SELECT count(*) FROM permissions WHERE code = :c"), {"c": CODE}
                )
            ).scalar_one()
        assert left == 0
        assert await _holders(engine, "orgs.manage") == before

        _alembic(db_at_0243, "upgrade", "0244")
        assert await _holders(engine, CODE) == before
    finally:
        await engine.dispose()
