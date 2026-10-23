"""Migration 0053: the `backup_chunks` index, its constraints and its RLS
(`docs/modules/backup/spec.md` §5.2).

**THE TENANCY ASSERTIONS HERE ARE PINNED BY MUTATION.** For an `ALL`-command
policy PostgreSQL reuses `USING` as `WITH CHECK` when the latter is omitted, so
*deleting* the `WITH CHECK` clause falsifies nothing. The two mutations that
make these tests red are `USING (true)` (the visibility test) and
`WITH CHECK (true)` (the insert test), and both were run against this file.

**THE `chunk_id` CHECK IS A KEY-SAFETY CONTROL.** A chunk id becomes an object
key (`{storage_ref}/chunks/{id[:2]}/{id}`), so the test that matters is not "a
64-character string is accepted" but that a traversal-shaped id of the same
LENGTH is refused — a fixture using a short string would pass against a CHECK
that only measured length.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from tests.backup_dedup_fixtures import all_index_rows, chunk_id, seed_dedup_org

_INSERT = (
    "INSERT INTO backup_chunks (id, org_id, repository_id, chunk_id, created_at, updated_at)"
    " VALUES (:id, :org, :repo, :chunk, now(), now())"
)


async def _insert(
    session: AsyncSession, org_id: UUID, repository_id: UUID, chunk: str
) -> None:
    await session.execute(
        text(_INSERT),
        {"id": uuid4(), "org": org_id, "repo": repository_id, "chunk": chunk},
    )


# --------------------------------------------------------------------------- #
# Row-level security (invariant #5)
# --------------------------------------------------------------------------- #


async def test_rls_is_enabled_and_forced(admin_engine: AsyncEngine) -> None:
    """`ENABLE` alone exempts the table owner; `FORCE` is what closes that."""
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relname = 'backup_chunks'"
                )
            )
        ).one()
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True)


async def test_a_row_is_invisible_to_another_org(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path
) -> None:
    """*If the policy's `USING` were `(true)`* org B's session reads org A's
    chunk id out of this table instead of nothing — which in a deduplicating
    store is the cross-tenant existence oracle the design exists to prevent."""
    vault = LocalVaultService(tmp_path / "root.key")
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "chm-a")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "chm-b")
    secret = chunk_id(0xA1)

    async with AsyncSession(admin_engine) as session:
        await _insert(session, a.org_id, a.repository_id, secret)
        await session.commit()

    async with org_session(b.org_id, engine=app_engine) as session:
        seen = (
            (await session.execute(text("SELECT chunk_id FROM backup_chunks"))).scalars().all()
        )
    assert list(seen) == []

    async with org_session(a.org_id, engine=app_engine) as session:
        own = (
            (await session.execute(text("SELECT chunk_id FROM backup_chunks"))).scalars().all()
        )
    assert list(own) == [secret]


async def test_a_row_cannot_be_inserted_under_another_orgs_id(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path
) -> None:
    """*If the policy's `WITH CHECK` were `(true)`* this reports
    `(False, [(<org B>, <org B's repo>, '...a2')])` — org B's repository
    acquires a dedup entry written from org A's session."""
    vault = LocalVaultService(tmp_path / "root.key")
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "chm-c")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "chm-d")

    refused = False
    try:
        async with org_session(a.org_id, engine=app_engine) as session:
            await _insert(session, b.org_id, b.repository_id, chunk_id(0xA2))
    except (DBAPIError, ProgrammingError):
        refused = True

    rows = [r for r in await all_index_rows(admin_engine) if r[1] == b.repository_id]
    assert (refused, rows) == (True, [])


# --------------------------------------------------------------------------- #
# Constraints
# --------------------------------------------------------------------------- #


async def test_a_traversal_shaped_chunk_id_of_the_right_length_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path
) -> None:
    """The id is 64 characters and would pass any length-only check; it is
    rejected because it is not hex.

    This is the fixture the CHECK is actually for: `chunk_id` is concatenated
    into an object key, and `../../` inside one reaches out of the repository
    prefix on any HTTP layer that normalises a path.
    """
    vault = LocalVaultService(tmp_path / "root.key")
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "chm-e")
    traversal = "../" * 20 + "abcd"
    assert len(traversal) == 64

    refused = False
    try:
        async with AsyncSession(admin_engine) as session:
            await _insert(session, a.org_id, a.repository_id, traversal)
            await session.commit()
    except (DBAPIError, ProgrammingError):
        refused = True

    # `DID NOT RAISE` names no wrong value. Under a length-only CHECK this
    # reports `(False, ['../../../.../abcd'])` — the id that would have become
    # an object key.
    rows = [r[2] for r in await all_index_rows(admin_engine) if r[1] == a.repository_id]
    assert (refused, rows) == (True, [])


async def test_uppercase_and_short_ids_are_rejected_and_the_canonical_form_accepted(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path
) -> None:
    """Both halves in one test: a CHECK that refused everything would pass a
    test that only asserted the refusals."""
    vault = LocalVaultService(tmp_path / "root.key")
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "chm-f")
    good = chunk_id(0xB3)

    async with AsyncSession(admin_engine) as session:
        await _insert(session, a.org_id, a.repository_id, good)
        await session.commit()

    accepted: list[str] = []
    for bad in (good.upper(), good[:63], good + "0"):
        try:
            async with AsyncSession(admin_engine) as session:
                await _insert(session, a.org_id, a.repository_id, bad)
                await session.commit()
        except (DBAPIError, ProgrammingError):
            continue
        accepted.append(bad)

    rows = [r[2] for r in await all_index_rows(admin_engine) if r[1] == a.repository_id]
    assert (accepted, rows) == ([], [good])


async def test_the_same_chunk_id_is_unique_per_repository_but_not_across_them(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path
) -> None:
    """Two repositories hold the same content id independently — there is NO
    cross-repository (and therefore no cross-org) dedup, which is the spec's
    position and is enforced by the uniqueness being `(repository_id,
    chunk_id)` rather than `(chunk_id)`."""
    vault = LocalVaultService(tmp_path / "root.key")
    a, _ = await seed_dedup_org(admin_engine, app_engine, vault, "chm-g")
    b, _ = await seed_dedup_org(admin_engine, app_engine, vault, "chm-h")
    shared = chunk_id(0xC4)

    async with AsyncSession(admin_engine) as session:
        await _insert(session, a.org_id, a.repository_id, shared)
        await _insert(session, b.org_id, b.repository_id, shared)
        await session.commit()

    with pytest.raises((DBAPIError, ProgrammingError)):
        async with AsyncSession(admin_engine) as session:
            await _insert(session, a.org_id, a.repository_id, shared)
            await session.commit()

    rows = sorted(r for r in await all_index_rows(admin_engine) if r[2] == shared)
    assert rows == sorted(
        [(a.org_id, a.repository_id, shared), (b.org_id, b.repository_id, shared)]
    )
