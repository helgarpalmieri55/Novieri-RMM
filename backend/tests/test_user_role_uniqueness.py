"""A user holds a role once, and the DATABASE is what says so.

THE DEFECT THESE TESTS PIN. `user_roles` shipped with
`UNIQUE (user_id, role_id, device_group_id)` over a NULLABLE
`device_group_id`. Two NULLs are never equal in Postgres, so a unique key
containing a NULL can never collide -- and every ORG-WIDE grant carries
`device_group_id IS NULL`. The rule was therefore enforced for group-scoped
grants and inert for org-wide ones, which is the wrong way round: the operator
ran the same `INSERT` three times, held `super-admin` three times, and saw
three identical rows in the console. Migration 0073 adds the partial unique
index `(user_id, role_id) WHERE device_group_id IS NULL` that the constraint
could not express.

MIGRATION 0230 MOVED THE SCOPE FROM A DEVICE GROUP TO A CLIENT. The column is
`client_id` now (MSP clients spec §2.1; NULL is "every client"), with
`UNIQUE (user_id, role_id, client_id)` and the partial index over
`WHERE client_id IS NULL`, and the argument above holds unchanged for it. The
tests that run at head are therefore written against `client_id`; the two that
migrate a database through 0072 -> 0073 at the bottom of this file still speak
`device_group_id`, because that is the schema 0073 acted on.

WHY EVERY ASSERTION BELOW IS A RAW `INSERT` ON `admin_engine`. The rule has to
hold against SQL, not merely against `assign_role`: the duplicates in
production were written by hand, and a service-layer check is not a constraint.
Raw `text()` statements put nothing between the test and the index -- delete
the index and these go red, which a test driven through the API would also do
but for a reason that could be satisfied by a Python `if`.

`admin_engine` is the BYPASSRLS role, and that is not the tenancy shortcut the
`bypassrls_session` docstring warns about: unique-index enforcement happens
below RLS regardless of the connecting role, and no assertion here is about an
`org_id` predicate. It is used because these tests seed and read across the
whole table.
"""

import os
import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

BACKEND_DIR = Path(__file__).resolve().parent.parent

_INSERT_GRANT = text(
    "INSERT INTO user_roles (id, org_id, user_id, role_id, client_id) "
    "VALUES (:id, :org_id, :user_id, :role_id, :client_id)"
)


async def _seed_org_and_user(engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    """One organization, one user in it, and the global `super-admin` role id."""
    org_id, user_id = uuid4(), uuid4()
    slug = f"uniq-{uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organizations (id, name, slug, status, settings) "
                "VALUES (:id, :name, :slug, 'active', '{}')"
            ),
            {"id": org_id, "name": slug, "slug": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, org_id, external_subject, email, display_name, status) "
                "VALUES (:id, :org_id, :sub, :email, :name, 'active')"
            ),
            {
                "id": user_id,
                "org_id": org_id,
                "sub": f"sub-{slug}",
                "email": f"{slug}@test.local",
                "name": slug,
            },
        )
        role_id = (
            await conn.execute(
                text("SELECT id FROM roles WHERE name = 'super-admin' AND org_id IS NULL")
            )
        ).scalar_one()
    return org_id, user_id, role_id


async def _seed_client(engine: AsyncEngine, org_id: UUID) -> UUID:
    """A customer client of the org, besides the two system clients every
    organization is born with (the internal one has `id = org_id`)."""
    client_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO clients (id, org_id, name, kind) "
                "VALUES (:id, :org_id, :name, 'customer')"
            ),
            {"id": client_id, "org_id": org_id, "name": f"client-{uuid4().hex[:8]}"},
        )
    return client_id


async def _grant(
    engine: AsyncEngine,
    org_id: UUID,
    user_id: UUID,
    role_id: UUID,
    client_id: UUID | None = None,
) -> UUID:
    grant_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            _INSERT_GRANT,
            {
                "id": grant_id,
                "org_id": org_id,
                "user_id": user_id,
                "role_id": role_id,
                "client_id": client_id,
            },
        )
    return grant_id


# --------------------------------------------------------------------------- #
# the regression
# --------------------------------------------------------------------------- #


async def test_a_second_identical_org_wide_grant_is_refused_by_the_database(
    admin_engine: AsyncEngine,
) -> None:
    """THE REGRESSION. Before migration 0073 this second INSERT succeeded, and so
    did a third -- which is how the operator came to hold `super-admin` three
    times. Nothing in the application refuses it; the index has to."""
    org_id, user_id, role_id = await _seed_org_and_user(admin_engine)
    await _grant(admin_engine, org_id, user_id, role_id)

    with pytest.raises(IntegrityError):
        await _grant(admin_engine, org_id, user_id, role_id)

    async with admin_engine.connect() as conn:
        held = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM user_roles "
                    "WHERE user_id = :user_id AND role_id = :role_id "
                    "AND client_id IS NULL"
                ),
                {"user_id": user_id, "role_id": role_id},
            )
        ).scalar_one()
    assert held == 1


async def test_a_second_identical_client_scoped_grant_is_still_refused(
    admin_engine: AsyncEngine,
) -> None:
    """The half of the rule that always worked, kept honest.

    The partial index does not replace the table constraint, it completes
    it -- so the client-scoped duplicate the constraint already catches must
    still be caught. If a later change drops the constraint on the theory that
    the index covers the table, this goes red.
    """
    org_id, user_id, role_id = await _seed_org_and_user(admin_engine)
    client_id = await _seed_client(admin_engine, org_id)
    await _grant(admin_engine, org_id, user_id, role_id, client_id)

    with pytest.raises(IntegrityError):
        await _grant(admin_engine, org_id, user_id, role_id, client_id)


# --------------------------------------------------------------------------- #
# what the index must NOT refuse
# --------------------------------------------------------------------------- #


async def test_the_same_role_org_wide_and_scoped_to_a_client_both_stand(
    admin_engine: AsyncEngine,
) -> None:
    """Two DIFFERENT grants, not a duplicate: one covers every client, one
    covers a single client. The partial index only ranges over rows with a NULL
    client, so the scoped row is outside it entirely."""
    org_id, user_id, role_id = await _seed_org_and_user(admin_engine)
    client_id = await _seed_client(admin_engine, org_id)

    await _grant(admin_engine, org_id, user_id, role_id)
    await _grant(admin_engine, org_id, user_id, role_id, client_id)

    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT count(*) FROM user_roles WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
        ).scalar_one()
    assert rows == 2


async def test_the_same_role_on_two_different_clients_both_stand(
    admin_engine: AsyncEngine,
) -> None:
    org_id, user_id, role_id = await _seed_org_and_user(admin_engine)
    first = await _seed_client(admin_engine, org_id)
    second = await _seed_client(admin_engine, org_id)

    await _grant(admin_engine, org_id, user_id, role_id, first)
    await _grant(admin_engine, org_id, user_id, role_id, second)

    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT count(*) FROM user_roles WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
        ).scalar_one()
    assert rows == 2


async def test_two_different_users_may_each_hold_the_role_org_wide(
    admin_engine: AsyncEngine,
) -> None:
    """`user_id` is in the key, so the index constrains one user at a time.

    This is also the test that would catch an index narrowed to `(role_id)`
    alone, which would let the first `super-admin` grant in the deployment
    block every later one.
    """
    org_a, user_a, role_id = await _seed_org_and_user(admin_engine)
    org_b, user_b, _same_role = await _seed_org_and_user(admin_engine)

    await _grant(admin_engine, org_a, user_a, role_id)
    await _grant(admin_engine, org_b, user_b, role_id)

    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM user_roles "
                    "WHERE role_id = :role_id AND user_id IN (:a, :b)"
                ),
                {"role_id": role_id, "a": user_a, "b": user_b},
            )
        ).scalar_one()
    assert rows == 2


# --------------------------------------------------------------------------- #
# the index itself
# --------------------------------------------------------------------------- #


async def test_the_partial_unique_index_exists_with_the_right_predicate(
    admin_engine: AsyncEngine,
) -> None:
    """`test_schema_drift.py` compares indexes model-to-database; this pins the
    shape itself, so an index that drifted in BOTH places still goes red."""
    async with admin_engine.connect() as conn:
        definition = (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'user_roles' AND indexname = :name"
                ),
                {"name": "uq_user_roles_user_role_org_wide"},
            )
        ).scalar_one_or_none()
    assert definition is not None, "migration 0230's partial unique index is missing"
    assert "UNIQUE INDEX" in definition
    assert "(user_id, role_id)" in definition
    assert "WHERE (client_id IS NULL)" in definition


# --------------------------------------------------------------------------- #
# the dedupe the upgrade has to do first
# --------------------------------------------------------------------------- #


def _psql(sql: str, dbname: str) -> str:
    result = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
         "-d", dbname, "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _alembic(revision: str, dbname: str) -> None:
    host = os.environ.get("PGHOST", "127.0.0.1")
    url = f"postgresql+asyncpg://its_admin:its_admin_test@{host}:5432/{dbname}"
    subprocess.run(
        [str(BACKEND_DIR / ".venv" / "bin" / "alembic"), "upgrade", revision],
        cwd=BACKEND_DIR,
        env=os.environ | {"ITS_ALEMBIC_URL": url},
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def db_at_0072(pg_cluster: object) -> Iterator[str]:
    """A throwaway database stopped one revision BEFORE the fix.

    ITS OWN DATABASE, not the session one. The dedupe can only be exercised on
    a database that still ALLOWS the duplicates -- which is every deployed
    database, and is exactly the state the operator's host is in. The session
    fixture is already at head, where seeding them is impossible; that is the
    point of the fix.

    `pg_cluster` is requested only to guarantee the three roles exist before
    this creates a database owned by `its_admin`.
    """
    dbname = f"its_dedupe_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin', "postgres")
    try:
        _alembic("0072", dbname)
        yield dbname
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)', "postgres")


def test_the_upgrade_dedupes_org_wide_grants_and_leaves_the_earliest(
    db_at_0072: str,
) -> None:
    """Seed the operator's exact state, then run the migration over it.

    THE FAILURE THIS GUARDS IS SILENCE. A dedupe written as a self-join on
    `device_group_id = device_group_id` matches no org-wide row at all, deletes
    nothing, and then `CREATE UNIQUE INDEX` dies -- on the operator's host, mid
    `alembic upgrade`, not here. So this asserts the survivor count AND which
    row survived AND that the index now exists; a dedupe that did nothing fails
    all three.

    THE SCOPED ROW IS THE CONTROL. A grant of the same role to the same user,
    scoped to a group, is not a duplicate of the org-wide one and must be
    untouched -- a dedupe that partitioned without the `IS NULL` predicate
    would eat it.
    """
    org_id, user_id, group_id = uuid4(), uuid4(), uuid4()
    _psql(
        "INSERT INTO organizations (id, name, slug, status, settings) "
        f"VALUES ('{org_id}', 'dedupe', 'dedupe', 'active', '{{}}');"
        "INSERT INTO users (id, org_id, external_subject, email, display_name, status) "
        f"VALUES ('{user_id}', '{org_id}', 'sub-dedupe', 'd@test.local', 'Dedupe', 'active');"
        "INSERT INTO device_groups (id, org_id, name, description) "
        f"VALUES ('{group_id}', '{org_id}', 'dedupe-group', '');",
        db_at_0072,
    )
    # Three identical org-wide grants -- accepted at 0072, which IS the bug.
    ids = sorted(str(uuid4()) for _ in range(3))
    for grant_id in ids:
        _psql(
            "INSERT INTO user_roles (id, org_id, user_id, role_id) SELECT "
            f"'{grant_id}', '{org_id}', '{user_id}', id FROM roles "
            "WHERE name = 'super-admin' AND org_id IS NULL;",
            db_at_0072,
        )
    scoped_id = str(uuid4())
    _psql(
        "INSERT INTO user_roles (id, org_id, user_id, role_id, device_group_id) SELECT "
        f"'{scoped_id}', '{org_id}', '{user_id}', id, '{group_id}' FROM roles "
        "WHERE name = 'super-admin' AND org_id IS NULL;",
        db_at_0072,
    )
    before = _psql(
        f"SELECT count(*) FROM user_roles WHERE user_id = '{user_id}' "
        "AND device_group_id IS NULL",
        db_at_0072,
    )
    assert before == "3", "0072 was supposed to accept the duplicates; it did not"

    _alembic("0073", db_at_0072)

    survivors = _psql(
        f"SELECT id FROM user_roles WHERE user_id = '{user_id}' "
        "AND device_group_id IS NULL ORDER BY id",
        db_at_0072,
    ).splitlines()
    assert survivors == [ids[0]], "the EARLIEST grant (lowest uuid7 id) must be the survivor"

    scoped = _psql(
        f"SELECT id FROM user_roles WHERE user_id = '{user_id}' "
        "AND device_group_id IS NOT NULL",
        db_at_0072,
    ).splitlines()
    assert scoped == [scoped_id], "the group-scoped grant is not a duplicate and must remain"

    index = _psql(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'user_roles' "
        "AND indexname = 'uq_user_roles_user_role_org_wide'",
        db_at_0072,
    )
    assert "WHERE (device_group_id IS NULL)" in index

    # And the state the whole migration exists to reach: the fourth INSERT is
    # now refused by the database on the very host that held three.
    with pytest.raises(subprocess.CalledProcessError) as refused:
        _psql(
            "INSERT INTO user_roles (id, org_id, user_id, role_id) SELECT "
            f"'{uuid4()}', '{org_id}', '{user_id}', id FROM roles "
            "WHERE name = 'super-admin' AND org_id IS NULL;",
            db_at_0072,
        )
    assert "uq_user_roles_user_role_org_wide" in refused.value.stderr


def test_the_downgrade_drops_the_index_and_the_upgrade_puts_it_back(
    db_at_0072: str,
) -> None:
    """CI runs `upgrade head -> downgrade base -> upgrade head` on an EMPTY
    database. This runs 0073's own round trip over a database holding rows,
    because a downgrade that only works on an empty schema is a downgrade
    nobody has thought about (0072's docstring, same argument).

    It does NOT assert the deleted duplicates come back. They do not, and the
    migration's docstring says so.
    """
    _alembic("0073", db_at_0072)
    assert _psql(
        "SELECT count(*) FROM pg_indexes WHERE tablename = 'user_roles' "
        "AND indexname = 'uq_user_roles_user_role_org_wide'",
        db_at_0072,
    ) == "1"

    host = os.environ.get("PGHOST", "127.0.0.1")
    subprocess.run(
        [str(BACKEND_DIR / ".venv" / "bin" / "alembic"), "downgrade", "0072"],
        cwd=BACKEND_DIR,
        env=os.environ
        | {
            "ITS_ALEMBIC_URL": (
                f"postgresql+asyncpg://its_admin:its_admin_test@{host}:5432/{db_at_0072}"
            )
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert _psql(
        "SELECT count(*) FROM pg_indexes WHERE tablename = 'user_roles' "
        "AND indexname = 'uq_user_roles_user_role_org_wide'",
        db_at_0072,
    ) == "0"

    _alembic("0073", db_at_0072)
    assert _psql(
        "SELECT count(*) FROM pg_indexes WHERE tablename = 'user_roles' "
        "AND indexname = 'uq_user_roles_user_role_org_wide'",
        db_at_0072,
    ) == "1"
