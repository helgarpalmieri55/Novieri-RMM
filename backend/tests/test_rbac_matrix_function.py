"""`set_role_permissions`: the ONLY way the permission matrix is written.

WHY A FUNCTION AND NOT A GRANT. `deploy/postgres/grant-app-privileges.sql`
REVOKES INSERT/UPDATE/DELETE on `role_permissions` from `app_user`, and says in
its own rationale why: without it, `app_user` could write the RBAC catalog
directly, which is "a way to self-grant permissions that bypasses the API's own
RBAC checks entirely". A permission-matrix UI still has to write that table, so
the write goes through a SECURITY DEFINER function owned by `its_admin` -- the
REVOKE stays, and the only path in validates first.

EVERY TEST HERE RUNS AS `app_user`, through `app_engine`. That is the whole
point: run them as the admin role and they prove nothing, because the admin
role could write the table directly anyway.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session

pytestmark = pytest.mark.asyncio


async def _seed_org(engine: AsyncEngine) -> UUID:
    from app.models import Organization

    slug = f"fn-{uuid4().hex[:8]}"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _seed_role(engine: AsyncEngine, org_id: UUID | None, name: str) -> UUID:
    from app.models import Role

    async with AsyncSession(engine, expire_on_commit=False) as session:
        role = Role(org_id=org_id, name=name, description="")
        session.add(role)
        await session.commit()
        return role.id


async def _codes_of(engine: AsyncEngine, role_id: UUID) -> set[str]:
    async with AsyncSession(engine) as session:
        rows = await session.execute(
            text(
                "SELECT p.code FROM role_permissions rp "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE rp.role_id = :r"
            ),
            {"r": str(role_id)},
        )
        return set(rows.scalars())


async def _apply(session: AsyncSession, role_id: UUID, codes: list[str]) -> int:
    result = await session.execute(
        text("SELECT set_role_permissions(:r, :c)"),
        {"r": str(role_id), "c": codes},
    )
    return int(result.scalar_one())


async def test_app_user_still_cannot_write_role_permissions_directly(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """NON-VACUITY, and the assumption every other test here rests on. If the
    REVOKE were ever lifted, the function would still work and every test below
    would still pass -- while the control it exists to preserve was gone. This
    is the one that notices."""
    org_id = await _seed_org(admin_engine)
    role_id = await _seed_role(admin_engine, org_id, "direct-write")

    with pytest.raises(Exception) as caught:
        async with org_session(org_id, engine=app_engine) as session:
            await session.execute(
                text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "SELECT :r, id FROM permissions WHERE code = 'devices.read'"
                ),
                {"r": str(role_id)},
            )
    assert "permission denied" in str(caught.value).lower()


async def test_the_function_applies_a_whole_code_set_as_app_user(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id = await _seed_org(admin_engine)
    role_id = await _seed_role(admin_engine, org_id, "matrix-apply")

    async with org_session(org_id, engine=app_engine) as session:
        written = await _apply(session, role_id, ["devices.read", "jobs.read"])

    assert written == 2
    assert await _codes_of(admin_engine, role_id) == {"devices.read", "jobs.read"}


async def test_applying_replaces_rather_than_accumulates(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The matrix is a SET, not a log. A UI that unticks a box must remove the
    grant; an additive function would make un-granting impossible through the
    only write path there is."""
    org_id = await _seed_org(admin_engine)
    role_id = await _seed_role(admin_engine, org_id, "matrix-replace")

    async with org_session(org_id, engine=app_engine) as session:
        await _apply(session, role_id, ["devices.read", "jobs.read", "audit.read"])
        await _apply(session, role_id, ["devices.read"])

    assert await _codes_of(admin_engine, role_id) == {"devices.read"}


async def test_an_empty_set_clears_the_role(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id = await _seed_org(admin_engine)
    role_id = await _seed_role(admin_engine, org_id, "matrix-clear")

    async with org_session(org_id, engine=app_engine) as session:
        await _apply(session, role_id, ["devices.read"])
        cleared = await _apply(session, role_id, [])

    assert cleared == 0
    assert await _codes_of(admin_engine, role_id) == set()


async def test_super_admin_is_refused_in_the_database(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE RAIL THAT MUST HOLD BELOW THE SERVICE LAYER. `super-admin` is
    cross-org platform administration; an org-scoped console able to reshape it
    is a tenant-escalation path. Enforced here rather than only in Python so it
    survives a caller that bypasses the service layer entirely."""
    org_id = await _seed_org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        role_id = (
            await session.execute(
                text("SELECT id FROM roles WHERE name = 'super-admin' AND org_id IS NULL")
            )
        ).scalar_one()

    with pytest.raises(Exception) as caught:
        async with org_session(org_id, engine=app_engine) as session:
            await _apply(session, role_id, ["devices.read"])
    assert "super-admin is not editable" in str(caught.value)


async def test_a_platform_operator_code_is_refused_in_the_database(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """ADDED BY 0068, AND IT IS WHAT MAKES 0068 MEAN ANYTHING.

    0068 takes `platform.errors.*` off `org-admin` because those codes read
    every tenant's error correlation ids across the tenant boundary (audit
    2026-08-21-tenancy-test-efficacy F1). `org-admin` also holds `roles.manage`
    and `users.manage`, so without this rail the revocation is undone by
    creating a role carrying the code and assigning it -- two console calls, no
    SQL, no audit trail anyone would read as an escalation.

    Refused by CODE, not by role name: the escalation path is a role nobody has
    named yet. And refused HERE, below the service layer, for the same reason
    `super-admin` is -- so it survives a caller that bypasses
    `app/core/rbac_admin.py` entirely."""
    org_id = await _seed_org(admin_engine)
    role_id = await _seed_role(admin_engine, org_id, f"self-grant-{uuid4().hex[:6]}")

    with pytest.raises(Exception) as caught:
        async with org_session(org_id, engine=app_engine) as session:
            await _apply(session, role_id, ["devices.read", "platform.errors.read"])
    assert "platform.errors.read" in str(caught.value)
    assert "not grantable here" in str(caught.value)

    # Nothing written -- not even the legitimate code that shared the payload.
    assert await _codes_of(admin_engine, role_id) == set()


async def test_an_ordinary_code_set_still_applies_after_the_operator_rail(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The rail must refuse two codes, not every code beginning with
    `platform.`. `platform.status.read` is deliberately still grantable: it
    reads probe results and process heartbeats from tables with no `org_id`,
    an org may legitimately hold it, and the console posts back every code a
    role holds on an ordinary matrix edit -- so a prefix rule would turn a
    routine role edit into a 403."""
    org_id = await _seed_org(admin_engine)
    role_id = await _seed_role(admin_engine, org_id, f"ordinary-{uuid4().hex[:6]}")

    async with org_session(org_id, engine=app_engine) as session:
        written = await _apply(session, role_id, ["devices.read", "platform.status.read"])
    assert written == 2
    assert await _codes_of(admin_engine, role_id) == {"devices.read", "platform.status.read"}


async def test_agent_release_manage_is_refused_in_the_database(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """ADDED BY 0070, AND IT IS THE HALF THAT MAKES 0070 MORE THAN COSMETIC.

    0070 takes `agent.release.manage` off `org-admin`. `agent_releases` is one
    catalogue shared by every tenant -- no `org_id`, so no RLS beneath it -- and
    that code publishes the bytes every tenant's endpoints execute. `org-admin`
    also holds `roles.manage` and `users.manage`, so without a rail the
    revocation is undone from the console: create a role carrying the code,
    assign it to yourself, publish. Two calls, no SQL.

    Refused HERE, below the service layer, so it survives a caller who never
    comes through `app/core/rbac_admin.py`.

    Paired deliberately with the assertion below rather than left alone: this
    one would pass against a function that refused every code."""
    org_id = await _seed_org(admin_engine)
    role_id = await _seed_role(admin_engine, org_id, f"publisher-{uuid4().hex[:6]}")

    with pytest.raises(Exception) as caught:
        async with org_session(org_id, engine=app_engine) as session:
            await _apply(session, role_id, ["devices.read", "agent.release.manage"])
    assert "agent.release.manage" in str(caught.value)
    assert "not grantable here" in str(caught.value)

    # Nothing written -- not even the legitimate code it travelled beside.
    assert await _codes_of(admin_engine, role_id) == set()


async def test_agent_release_read_still_applies_after_the_operator_rail(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The non-vacuity partner. The rail is keyed on exact codes, not on the
    `agent.release.` prefix: 0060 left `agent.release.read` for each org to
    grant a technician explicitly, because generating an install command is a
    catalogue read plus a token mint, and 0070 did not disturb that."""
    org_id = await _seed_org(admin_engine)
    role_id = await _seed_role(admin_engine, org_id, f"installer-{uuid4().hex[:6]}")

    async with org_session(org_id, engine=app_engine) as session:
        written = await _apply(session, role_id, ["devices.read", "agent.release.read"])
    assert written == 2
    assert await _codes_of(admin_engine, role_id) == {"devices.read", "agent.release.read"}


async def test_a_global_role_is_refused_because_it_is_shared_by_every_tenant(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`Role.org_id` is nullable and the five seeded roles leave it NULL, so
    `technician` is ONE ROW shared by every org. Editing it in place would
    reshape another tenant's role. The service layer clones first; being handed
    a global role here means that step was skipped, and quietly editing it would
    be a cross-tenant write that no test in the calling module would notice."""
    org_id = await _seed_org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        role_id = (
            await session.execute(
                text("SELECT id FROM roles WHERE name = 'technician' AND org_id IS NULL")
            )
        ).scalar_one()

    with pytest.raises(Exception) as caught:
        async with org_session(org_id, engine=app_engine) as session:
            await _apply(session, role_id, ["devices.read"])
    assert "must be cloned before editing" in str(caught.value)


async def test_an_unknown_code_raises_rather_than_silently_writing_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`INSERT ... SELECT` on a misspelled code inserts zero rows and reports
    success. That reaches the operator as a permission which will not stick and
    no explanation anywhere -- the worst kind of bug in an access-control UI,
    because the screen says one thing and the database another."""
    org_id = await _seed_org(admin_engine)
    role_id = await _seed_role(admin_engine, org_id, "matrix-typo")

    with pytest.raises(Exception) as caught:
        async with org_session(org_id, engine=app_engine) as session:
            await _apply(session, role_id, ["devices.read", "devices.raed"])
    assert "devices.raed" in str(caught.value)

    # AND NOTHING WAS WRITTEN. The function raises, so the whole statement rolls
    # back -- a partial apply would leave the role holding some of what was
    # asked for, which is worse than refusing.
    assert await _codes_of(admin_engine, role_id) == set()


async def test_an_unknown_role_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id = await _seed_org(admin_engine)
    with pytest.raises(Exception) as caught:
        async with org_session(org_id, engine=app_engine) as session:
            await _apply(session, uuid4(), ["devices.read"])
    assert "no such role" in str(caught.value)
