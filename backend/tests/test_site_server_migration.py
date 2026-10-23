"""Migration 0050: the `site_server` table, its constraints, RLS, and the three
permissions (`docs/modules/site-server/spec.md` §2.1, §10.1, §10.2, §15).

**THE TENANCY ASSERTIONS HERE ARE PINNED BY MUTATION, NOT BY READING THE
MIGRATION.** For an `ALL`-command policy PostgreSQL reuses `USING` as
`WITH CHECK` when the latter is omitted, so *deleting* the `WITH CHECK` clause
falsifies nothing at all. The two mutations that actually make these tests red
are `WITH CHECK (true)` (the insert test) and `USING (true)` (the visibility
test), and both were run against this file before it was committed.

**THE `roles` CHECK IS DELIBERATELY WIDER THAN v1.** `cache` and `pxe` are
admitted by the database and refused by the service (Task 2), because that
vocabulary is the whole of the forward seam: the cache slice must not need a
migration to widen a shipped CHECK constraint on a table that already has rows.
`test_roles_cache_is_accepted_at_the_database_layer` pins that split so a later
author does not "tighten" the CHECK and silently move the gate.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from tests.ipam_fixtures import seed_device, seed_org
from tests.site_server_fixtures import seed_site_server, site_server_rows

_INSERT = (
    "INSERT INTO site_server"
    " (id, org_id, device_id, name, segments, roles, enabled, notes,"
    "  created_at, updated_at)"
    " VALUES (:id, :org, :device, 'sv', CAST(:segments AS cidr[]),"
    "         CAST(:roles AS text[]), true, '', now(), now())"
)


async def _insert(
    session: AsyncSession,
    org_id: UUID,
    device_id: UUID,
    *,
    segments: list[str],
    roles: list[str],
) -> None:
    await session.execute(
        text(_INSERT),
        {
            "id": uuid4(),
            "org": org_id,
            "device": device_id,
            "segments": segments,
            "roles": roles,
        },
    )


# --------------------------------------------------------------------------- #
# Row-level security (invariant #5)
# --------------------------------------------------------------------------- #


async def test_rls_is_enabled_and_forced(admin_engine: AsyncEngine) -> None:
    """`ENABLE` alone exempts the table owner; `FORCE` is what closes that.

    Asserted as the pair of flags rather than "a policy exists", because a
    policy on a table with `relforcerowsecurity = false` is enforced against
    nobody who matters.
    """
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relname = 'site_server'"
                )
            )
        ).one()
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True)


async def test_a_row_is_invisible_to_another_org(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """*If the policy's `USING` were `(true)`* org B's session sees org A's
    device id in this list instead of nothing."""
    org_a = await seed_org(admin_engine, "ssm-a")
    org_b = await seed_org(admin_engine, "ssm-b")
    await seed_site_server(
        admin_engine, org_a.org_id, device_id=org_a.device_id, segments=["10.20.0.0/16"]
    )

    async with org_session(org_b.org_id, engine=app_engine) as session:
        seen = (
            await session.execute(text("SELECT device_id FROM site_server"))
        ).scalars().all()
    assert list(seen) == []

    async with org_session(org_a.org_id, engine=app_engine) as session:
        own = (await session.execute(text("SELECT device_id FROM site_server"))).scalars().all()
    assert list(own) == [org_a.device_id]


async def test_a_row_cannot_be_inserted_under_another_orgs_id(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """*If the policy's `WITH CHECK` were `(true)`* org B acquires a site server
    it never asked for, and this asserts THAT rather than "an exception was
    raised".

    The assertion is a tuple of observed facts — whether the write was refused
    AND what org B's table actually holds afterwards. `DID NOT RAISE` is not
    evidence: it names no wrong value, so it cannot distinguish "the policy is
    doing its job" from "the insert failed for an unrelated reason". Under the
    mutation this reports `(False, [(<org B's device>, ['10.9.0.0/16'], True)])`.
    """
    org_a = await seed_org(admin_engine, "ssm-c")
    org_b = await seed_org(admin_engine, "ssm-d")

    refused = False
    try:
        async with org_session(org_a.org_id, engine=app_engine) as session:
            await _insert(
                session, org_b.org_id, org_b.device_id, segments=["10.9.0.0/16"], roles=["wake"]
            )
    except (DBAPIError, ProgrammingError):
        refused = True

    assert (refused, await site_server_rows(admin_engine, org_b.org_id)) == (True, [])


# --------------------------------------------------------------------------- #
# Constraints (spec §2.1)
# --------------------------------------------------------------------------- #


async def test_zero_segments_is_rejected(admin_engine: AsyncEngine) -> None:
    """A site server that serves no segment serves nothing. That is a
    configuration mistake, not a valid row."""
    org = await seed_org(admin_engine, "ssm-e")
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with AsyncSession(admin_engine) as session:
            await _insert(session, org.org_id, org.device_id, segments=[], roles=["wake"])
            await session.commit()
    assert await site_server_rows(admin_engine, org.org_id) == []


async def test_thirty_two_segments_accepted_and_thirty_three_rejected(
    admin_engine: AsyncEngine,
) -> None:
    """Both halves in one test: a CHECK that refused every cardinality would
    pass a test that only asserts the refusal."""
    org = await seed_org(admin_engine, "ssm-f")
    other = await seed_device(admin_engine, org.org_id)

    thirty_two = [f"10.{i}.0.0/16" for i in range(32)]
    async with AsyncSession(admin_engine) as session:
        await _insert(session, org.org_id, org.device_id, segments=thirty_two, roles=["wake"])
        await session.commit()

    with pytest.raises((DBAPIError, ProgrammingError)):
        async with AsyncSession(admin_engine) as session:
            await _insert(
                session,
                org.org_id,
                other,
                segments=thirty_two + ["10.32.0.0/16"],
                roles=["wake"],
            )
            await session.commit()

    stored = await site_server_rows(admin_engine, org.org_id)
    assert [(d, len(s)) for d, s, _ in stored] == [(org.device_id, 32)]


async def test_empty_roles_is_rejected(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "ssm-g")
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with AsyncSession(admin_engine) as session:
            await _insert(session, org.org_id, org.device_id, segments=["10.0.0.0/16"], roles=[])
            await session.commit()
    assert await site_server_rows(admin_engine, org.org_id) == []


async def test_unknown_role_is_rejected(admin_engine: AsyncEngine) -> None:
    """`relay` is a real deferred idea (spec §16) and is NOT in the vocabulary.
    The CHECK admits exactly `wake`, `cache`, `pxe`."""
    org = await seed_org(admin_engine, "ssm-h")
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with AsyncSession(admin_engine) as session:
            await _insert(
                session, org.org_id, org.device_id, segments=["10.0.0.0/16"], roles=["relay"]
            )
            await session.commit()
    assert await site_server_rows(admin_engine, org.org_id) == []


async def test_roles_cache_is_accepted_at_the_database_layer(admin_engine: AsyncEngine) -> None:
    """`cache` is storable and **refused by the service** (Task 2), on purpose.

    The CHECK is deliberately wider than v1 honours: the `roles` vocabulary is
    the whole of the forward seam for the cache and PXE slices (spec §7, §8), so
    keeping `cache` and `pxe` admissible here is what stops that slice needing a
    migration to widen a shipped CHECK on a populated table. The *refusal* is a
    service rule with the §4.4 gate named in its error message, which is one
    predicate to relax in one place when the invariant amendment is accepted.
    """
    org = await seed_org(admin_engine, "ssm-i")
    async with AsyncSession(admin_engine) as session:
        await _insert(
            session, org.org_id, org.device_id, segments=["10.0.0.0/16"], roles=["wake", "cache"]
        )
        await session.commit()
    assert [d for d, _, _ in await site_server_rows(admin_engine, org.org_id)] == [org.device_id]


async def test_a_device_may_be_promoted_at_most_once(admin_engine: AsyncEngine) -> None:
    """`UNIQUE (device_id)`, not `(org_id, device_id)`: `devices.id` is already
    globally unique and the narrower key is the honest one (`agents.device_id`
    does the same)."""
    org = await seed_org(admin_engine, "ssm-j")
    await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.1.0.0/16"]
    )
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with AsyncSession(admin_engine) as session:
            await _insert(
                session, org.org_id, org.device_id, segments=["10.2.0.0/16"], roles=["wake"]
            )
            await session.commit()
    assert await site_server_rows(admin_engine, org.org_id) == [
        (org.device_id, ["10.1.0.0/16"], True)
    ]


async def test_segments_is_a_real_cidr_array(admin_engine: AsyncEngine) -> None:
    """Containment, not string comparison (§2.1). `10.20.7.0/24 <<= 10.20.0.0/16`
    is true and `10.200.7.0/24 <<= 10.20.0.0/16` is false; in `text` the second
    is a prefix match and would be wrong."""
    org = await seed_org(admin_engine, "ssm-k")
    await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.0.0/16"]
    )
    async with AsyncSession(admin_engine) as session:
        answers = (
            await session.execute(
                text(
                    "SELECT CAST(:inside AS cidr) <<= segments[1] AS inside,"
                    "       CAST(:outside AS cidr) <<= segments[1] AS outside"
                    " FROM site_server WHERE org_id = :org"
                ),
                {"inside": "10.20.7.0/24", "outside": "10.200.7.0/24", "org": org.org_id},
            )
        ).one()
    assert (answers.inside, answers.outside) == (True, False)


async def test_the_org_enabled_index_exists(admin_engine: AsyncEngine) -> None:
    async with AsyncSession(admin_engine) as session:
        names = (
            await session.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'site_server'")
            )
        ).scalars().all()
    assert "ix_site_server_org_enabled" in set(names)


# --------------------------------------------------------------------------- #
# Permissions and role grants (spec §10.2)
# --------------------------------------------------------------------------- #

_CODES = ("site.read", "site.manage", "site.wake")


async def _grants(engine: AsyncEngine, role: str) -> set[str]:
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT p.code FROM role_permissions rp"
                    " JOIN roles r ON r.id = rp.role_id"
                    " JOIN permissions p ON p.id = rp.permission_id"
                    " WHERE r.name = :role AND r.org_id IS NULL AND p.module = 'site'"
                ),
                {"role": role},
            )
        ).scalars().all()
    return set(rows)


async def test_the_three_permissions_are_seeded(admin_engine: AsyncEngine) -> None:
    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                text("SELECT code, action FROM permissions WHERE module = 'site' ORDER BY code")
            )
        ).all()
    assert [(r.code, r.action) for r in rows] == [
        ("site.manage", "manage"),
        ("site.read", "read"),
        ("site.wake", "wake"),
    ]


@pytest.mark.parametrize("role", ["super-admin", "org-admin"])
async def test_admin_roles_hold_all_three(admin_engine: AsyncEngine, role: str) -> None:
    assert await _grants(admin_engine, role) == set(_CODES)


async def test_technician_may_wake_but_may_not_manage(admin_engine: AsyncEngine) -> None:
    """The NEGATIVE half is the assertion that bites.

    A test that only checks `site.wake` is present passes against a migration
    that granted everything to everyone — and "everything to everyone" is
    exactly what the two codes exist to prevent: deciding a host may serve an
    entire subnet is not the same act as waking a machine inside an existing
    configuration (spec §10.2).
    """
    assert await _grants(admin_engine, "technician") == {"site.read", "site.wake"}


@pytest.mark.parametrize("role", ["auditor", "read-only"])
async def test_read_roles_hold_only_site_read(admin_engine: AsyncEngine, role: str) -> None:
    assert await _grants(admin_engine, role) == {"site.read"}


async def test_the_model_and_the_table_agree_on_the_column_set(
    admin_engine: AsyncEngine,
) -> None:
    """`test_schema_drift.py` compares types; this pins the column NAMES, which
    is what a hand-written migration and a hand-written model most easily
    disagree about."""
    from app.modules.site_server.models import SiteServer

    async with AsyncSession(admin_engine) as session:
        live = (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'site_server'"
                )
            )
        ).scalars().all()
    declared: set[Any] = {c.name for c in SiteServer.__table__.columns}
    assert declared == set(live)
