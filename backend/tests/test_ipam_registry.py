"""VRF / site / VLAN registry (`docs/modules/ipam/spec.md` §2.1-§2.3, §0.7,
§0.15, §11.3).

WHAT THESE FIXTURES DELIBERATELY VARY, because that is where this repo's
recurring defect lives:

* **`ensure_default_vrf` is never tested against an org whose only VRF is the
  one it creates.** Every such test seeds a DIFFERENT VRF first, so an
  implementation that returns "the org's first VRF" -- or "any VRF" -- returns
  the wrong one and the assertion prints it. With one candidate the test cannot
  tell "found `global`" from "found whatever was there".
* **The concurrency test uses two REAL connections and a barrier**, not two
  sequential calls. Check-then-insert passes a sequential test perfectly; it is
  only wrong when two callers interleave, which is precisely the first-subnet-
  write-of-an-org case this function exists for.
* **The `org_id`-predicate tests run on the ADMIN engine.** RLS masks an
  application predicate: driven through `org_session` these pass with the
  service's own `org_id` filter deleted, because RLS rejects first. On the admin
  engine RLS is not the thing rejecting, so the predicate is the only thing left
  to reject -- which is what makes deleting it observable.
* **The VLAN tests exercise BOTH partial indexes through the SERVICE**, not by
  raw insert. A partial unique index can be present and still be inferred
  wrongly: `ON CONFLICT ON CONSTRAINT` against one raises
  `InvalidObjectDefinition` at RUNTIME, and only a test that actually executes
  the upsert can see it.
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sqltext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, IpamVlan, IpamVrf
from app.modules.ipam.errors import (
    IpamConflictError,
    IpamNotFoundError,
    IpamValidationError,
)
from app.modules.ipam.registry import (
    DEFAULT_VRF_NAME,
    create_site,
    create_vlan,
    create_vrf,
    delete_site,
    delete_vlan,
    delete_vrf,
    ensure_default_vrf,
    get_site,
    get_vlan,
    get_vrf,
    list_sites,
    list_vlans,
    list_vrfs,
    update_site,
    update_vlan,
    update_vrf,
)
from tests.ipam_fixtures import IpamOrg, seed_org, seed_site, seed_subnet, seed_vrf


async def _audit_actions(engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with AsyncSession(engine) as session:
        return [
            r.action
            for r in (
                await session.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            ).all()
        ]


async def _audit_metadata(engine: AsyncEngine, org_id: UUID, action: str) -> dict:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(
                select(AuditEvent.meta).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == action
                )
            )
        ).scalar_one()


async def _count(engine: AsyncEngine, model: type, org_id: UUID) -> int:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(
                select(func.count()).select_from(model).where(model.org_id == org_id)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# §0.7 -- the lazily created `global` VRF
# ---------------------------------------------------------------------------


async def test_ensure_default_vrf_creates_the_global_row(admin_engine: AsyncEngine) -> None:
    """It must return the VRF NAMED `global`, not merely "a VRF this org has".

    The org is seeded with an unrelated VRF first, so an implementation that
    selects the org's first (or only) VRF returns `customer-a` and this prints
    it. With `global` as the only candidate the assertion cannot fail.
    """
    org = await seed_org(admin_engine, "vrfdef")
    decoy = await seed_vrf(admin_engine, org.org_id, name="customer-a")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        vrf_id = await ensure_default_vrf(session, org.org_id)
        await session.commit()

    assert vrf_id != decoy
    async with AsyncSession(admin_engine) as session:
        name = (
            await session.execute(select(IpamVrf.name).where(IpamVrf.id == vrf_id))
        ).scalar_one()
    assert name == DEFAULT_VRF_NAME
    assert await _count(admin_engine, IpamVrf, org.org_id) == 2


async def test_ensure_default_vrf_is_idempotent(admin_engine: AsyncEngine) -> None:
    """Second call returns the SAME id and creates no second row."""
    org = await seed_org(admin_engine, "vrfidem")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        first = await ensure_default_vrf(session, org.org_id)
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        second = await ensure_default_vrf(session, org.org_id)
        await session.commit()
    assert first == second
    assert await _count(admin_engine, IpamVrf, org.org_id) == 1


async def test_two_concurrent_first_writes_produce_one_global_vrf(
    admin_engine: AsyncEngine,
) -> None:
    """THE CHECK-THEN-INSERT TEST, and it needs two real connections.

    `ensure_default_vrf` runs on the first subnet write of an org, which is
    exactly when two operators create their first two subnets in the same
    minute. IF THE GUARD WERE DELETED -- `SELECT ... if None: INSERT` instead of
    `ON CONFLICT DO NOTHING` + `SELECT` -- both callers see no row, both insert,
    and the loser raises `UniqueViolationError` out of a function whose entire
    job is to be boring. This test then fails with that exception rather than
    with an assertion, which is still a different observable outcome than the
    two equal ids it asserts.

    A sequential version of this test passes against the broken implementation.

    THE `SELECT 1` BEFORE THE BARRIER IS LOAD-BEARING, and it was found by
    running the mutation rather than by reasoning. A `SQLAlchemy` session
    acquires its connection lazily, on first execute -- so with the barrier as
    the first statement, both tasks reach it holding NOTHING, and the
    connection handshake inside each one's first `execute` serialises them
    enough that the second task's SELECT lands after the first task's INSERT.
    The broken implementation then passes: task B sees the row and does not
    insert. Warming the connection first is what makes both tasks arrive at the
    barrier with an open connection and interleave at the SELECT, which is the
    real race. Verified: with the warm-up, check-then-insert raises
    `UniqueViolationError` here; without it, check-then-insert is green.
    """
    org = await seed_org(admin_engine, "vrfrace")
    barrier = asyncio.Barrier(2)

    async def _ensure() -> UUID:
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            await session.execute(sqltext("SELECT 1"))
            await barrier.wait()
            vrf_id = await ensure_default_vrf(session, org.org_id)
            await session.commit()
            return vrf_id

    first, second = await asyncio.gather(_ensure(), _ensure())
    assert first == second
    assert await _count(admin_engine, IpamVrf, org.org_id) == 1


async def test_ensure_default_vrf_works_inside_a_tenant_session(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """It runs under RLS in real life: the row it inserts must satisfy the
    `WITH CHECK` predicate, which it does only because `org_id` comes from the
    argument and not from anywhere else."""
    org = await seed_org(admin_engine, "vrfrls")
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        vrf_id = await ensure_default_vrf(session, org.org_id)
        await session.commit()
    assert await _count(admin_engine, IpamVrf, org.org_id) == 1
    assert vrf_id is not None


# ---------------------------------------------------------------------------
# VRF CRUD
# ---------------------------------------------------------------------------


async def test_create_vrf_audits_and_refuses_a_duplicate_name(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "vrfcrud")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        vrf = await create_vrf(
            session, org.org_id, org.user_id, name="customer-a", rd="65000:1"
        )
        await session.commit()
        assert vrf.rd == "65000:1"

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="already exists"):
            await create_vrf(session, org.org_id, org.user_id, name="customer-a")

    assert await _audit_actions(admin_engine, org.org_id) == ["ipam.vrf_created"]
    assert (await _audit_metadata(admin_engine, org.org_id, "ipam.vrf_created"))["rd"] == (
        "65000:1"
    )


async def test_update_vrf_refuses_an_unknown_field_and_audits_from_and_to(
    admin_engine: AsyncEngine,
) -> None:
    """A key outside the editable set is REFUSED, not ignored: silently dropping
    it returns success for a typo'd body having changed nothing."""
    org = await seed_org(admin_engine, "vrfupd")
    vrf_id = await seed_vrf(admin_engine, org.org_id, name="customer-a")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamValidationError, match="unknown or server-owned"):
            await update_vrf(session, org.org_id, org.user_id, vrf_id, {"org_id": uuid4()})

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await update_vrf(session, org.org_id, org.user_id, vrf_id, {"name": "customer-b"})
        await session.commit()

    metadata = await _audit_metadata(admin_engine, org.org_id, "ipam.vrf_updated")
    assert metadata["from"] == {"name": "customer-a"}
    assert metadata["to"] == {"name": "customer-b"}


async def test_deleting_a_vrf_a_subnet_still_uses_is_refused_by_name(
    admin_engine: AsyncEngine,
) -> None:
    """The refusal NAMES the referents. Letting the foreign key raise instead
    produces a 500 whose text names a constraint, which tells an operator
    nothing about which subnets they have to move first."""
    org = await seed_org(admin_engine, "vrfdel")
    vrf_id = await seed_vrf(admin_engine, org.org_id, name="customer-a")
    await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.30.0.0/24")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="1 subnet"):
            await delete_vrf(session, org.org_id, org.user_id, vrf_id)

    assert await _count(admin_engine, IpamVrf, org.org_id) == 1


async def test_deleting_an_unused_vrf_succeeds_and_audits(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "vrfdel2")
    vrf_id = await seed_vrf(admin_engine, org.org_id, name="customer-a")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await delete_vrf(session, org.org_id, org.user_id, vrf_id)
        await session.commit()
    assert await _count(admin_engine, IpamVrf, org.org_id) == 0
    assert await _audit_actions(admin_engine, org.org_id) == ["ipam.vrf_deleted"]


# ---------------------------------------------------------------------------
# Site CRUD
# ---------------------------------------------------------------------------


async def test_create_site_refuses_a_duplicate_name_and_audits(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "sitecrud")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_site(session, org.org_id, org.user_id, name="Manila")
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="already exists"):
            await create_site(session, org.org_id, org.user_id, name="Manila")
    assert await _audit_actions(admin_engine, org.org_id) == ["ipam.site_created"]


async def test_a_doc_record_from_another_org_is_refused(admin_engine: AsyncEngine) -> None:
    """§2.2's optional link, validated IN THIS ORG.

    The foreign key is org-blind, so without this check the database happily
    joins one tenant's site to another tenant's document -- and RLS does not
    save it either, because referential-integrity checks run as the referenced
    table's owner. IF THE GUARD WERE DELETED this test creates the site and the
    stored `doc_record_id` points across the tenant boundary.
    """
    from app.models import DocRecord

    a = await seed_org(admin_engine, "docs-a")
    b = await seed_org(admin_engine, "docs-b")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        record = DocRecord(
            org_id=b.org_id, record_type="location", name="Their office", fields={}
        )
        session.add(record)
        await session.commit()
        foreign_record_id = record.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamValidationError, match="not found"):
            await create_site(
                session, a.org_id, a.user_id, name="Manila", doc_record_id=foreign_record_id
            )
    assert await list_sites_count(admin_engine, a.org_id) == 0


async def list_sites_count(engine: AsyncEngine, org_id: UUID) -> int:
    async with AsyncSession(engine) as session:
        return len(await list_sites(session, org_id))


async def test_deleting_a_site_a_vlan_still_uses_is_refused(admin_engine: AsyncEngine) -> None:
    """A site is referenced from TWO tables (§2.3, §2.4). A refusal that only
    looks at `ipam_subnet` deletes a site out from under its VLANs and leaves
    them org-wide -- which silently moves them into the OTHER partial unique
    index, where they can then collide."""
    org = await seed_org(admin_engine, "sitedel")
    site_id = await seed_site(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_vlan(session, org.org_id, org.user_id, vid=10, site_id=site_id)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="1 VLAN"):
            await delete_site(session, org.org_id, org.user_id, site_id)


async def test_updating_a_site_name_to_an_existing_one_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "siteupd")
    await seed_site(admin_engine, org.org_id, name="Manila")
    second = await seed_site(admin_engine, org.org_id, name="Cebu")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="already exists"):
            await update_site(session, org.org_id, org.user_id, second, {"name": "Manila"})


# ---------------------------------------------------------------------------
# §0.15 / §2.3 -- VLANs through the service, against BOTH partial indexes
# ---------------------------------------------------------------------------


async def test_a_second_org_wide_vlan_10_is_refused_by_the_service(
    admin_engine: AsyncEngine,
) -> None:
    """S2 through the SERVICE, not by raw insert.

    A partial unique index can be present and still be inferred wrongly. IF THE
    INFERENCE WERE WRONG here -- `index_elements=["org_id","site_id","vid"]` for
    the org-wide case, or `ON CONFLICT ON CONSTRAINT` -- Postgres raises
    `InvalidObjectDefinition`/`InvalidColumnReference` at runtime instead of
    raising `IpamConflictError`, and only a test that executes the statement can
    tell the difference.
    """
    org = await seed_org(admin_engine, "vlanglob")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_vlan(session, org.org_id, org.user_id, vid=10, name="voice")
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="VLAN 10 already exists org-wide"):
            await create_vlan(session, org.org_id, org.user_id, vid=10, name="voice-again")

    assert await _count(admin_engine, IpamVlan, org.org_id) == 1


async def test_vlan_10_at_two_sites_and_once_org_wide_all_coexist(
    admin_engine: AsyncEngine,
) -> None:
    """THE OTHER HALF, and the one a single org-wide unique index fails. VLAN 10
    legitimately exists at every site, once each, AND once org-wide -- the two
    partial indexes are total between them and neither is a superset of the
    other."""
    org = await seed_org(admin_engine, "vlansites")
    manila = await seed_site(admin_engine, org.org_id, name="Manila")
    cebu = await seed_site(admin_engine, org.org_id, name="Cebu")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_vlan(session, org.org_id, org.user_id, vid=10, site_id=manila)
        await create_vlan(session, org.org_id, org.user_id, vid=10, site_id=cebu)
        await create_vlan(session, org.org_id, org.user_id, vid=10)
        await session.commit()
    assert await _count(admin_engine, IpamVlan, org.org_id) == 3


async def test_vlan_10_twice_at_one_site_is_refused_by_the_service(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "vlansite2")
    site_id = await seed_site(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_vlan(session, org.org_id, org.user_id, vid=10, site_id=site_id)
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="already exists at site"):
            await create_vlan(session, org.org_id, org.user_id, vid=10, site_id=site_id)


async def test_moving_a_vlan_org_wide_onto_an_existing_one_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """The update path crosses BETWEEN the two partial indexes, which is where a
    duplicate check written against "the index this row currently sits under"
    silently lets a collision through."""
    org = await seed_org(admin_engine, "vlanmove")
    site_id = await seed_site(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_vlan(session, org.org_id, org.user_id, vid=10)
        sited = await create_vlan(session, org.org_id, org.user_id, vid=10, site_id=site_id)
        await session.commit()
        sited_id = sited.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="org-wide"):
            await update_vlan(session, org.org_id, org.user_id, sited_id, {"site_id": None})


@pytest.mark.parametrize("vid", [0, 4095, "10", True])
async def test_an_illegal_vid_is_refused_before_postgres_sees_it(
    admin_engine: AsyncEngine, vid: object
) -> None:
    """A typed 400 rather than a `ProgrammingError` surfacing as a 500. The
    CHECK constraint is still there; this is the readable error in front of it.
    `True` is in the list because `isinstance(True, int)` is `True` in Python
    and `vid=True` would otherwise store VLAN 1."""
    org = await seed_org(admin_engine, f"vid-{vid}")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamValidationError, match="vid must be"):
            await create_vlan(session, org.org_id, org.user_id, vid=vid)  # type: ignore[arg-type]


async def test_a_vlan_at_a_site_from_another_org_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    a = await seed_org(admin_engine, "vlanx-a")
    b = await seed_org(admin_engine, "vlanx-b")
    foreign_site = await seed_site(admin_engine, b.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamNotFoundError):
            await create_vlan(session, a.org_id, a.user_id, vid=10, site_id=foreign_site)


async def test_deleting_a_vlan_a_subnet_still_uses_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "vlandel")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        vlan = await create_vlan(session, org.org_id, org.user_id, vid=10)
        await session.commit()
        vlan_id = vlan.id
    await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.31.0.0/24", vlan_id=vlan_id
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="1 subnet"):
            await delete_vlan(session, org.org_id, org.user_id, vlan_id)


async def test_vlan_and_site_writes_audit_every_action(admin_engine: AsyncEngine) -> None:
    """§11.3's `ipam.vlan_*` / `ipam.site_*` rows, asserted as an exact ordered
    sequence rather than as membership -- an action that fires TWICE is as much
    a defect as one that never fires."""
    org = await seed_org(admin_engine, "audits")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        site = await create_site(session, org.org_id, org.user_id, name="Manila")
        vlan = await create_vlan(session, org.org_id, org.user_id, vid=10, site_id=site.id)
        await update_vlan(session, org.org_id, org.user_id, vlan.id, {"name": "voice"})
        await delete_vlan(session, org.org_id, org.user_id, vlan.id)
        await update_site(session, org.org_id, org.user_id, site.id, {"name": "Cebu"})
        await delete_site(session, org.org_id, org.user_id, site.id)
        await session.commit()
    assert await _audit_actions(admin_engine, org.org_id) == [
        "ipam.site_created",
        "ipam.vlan_created",
        "ipam.vlan_updated",
        "ipam.vlan_deleted",
        "ipam.site_updated",
        "ipam.site_deleted",
    ]


# ---------------------------------------------------------------------------
# The application `org_id` predicate, pinned on the ADMIN engine
# ---------------------------------------------------------------------------


async def _two_orgs(admin_engine: AsyncEngine, tag: str) -> tuple[IpamOrg, IpamOrg]:
    return await seed_org(admin_engine, f"{tag}-a"), await seed_org(admin_engine, f"{tag}-b")


async def test_get_vrf_from_another_org_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE ON PURPOSE (invariant #5, and the defect that
    shipped four times in the previous sub-project).

    Through `org_session` this test passes with `IpamVrf.org_id == org_id`
    DELETED from the query, because RLS rejects the row before the predicate is
    consulted -- so it proves nothing about the service. Here RLS is not the
    thing rejecting: deleting the predicate returns org B's VRF to org A and
    this fails on the returned row instead of raising.
    """
    a, b = await _two_orgs(admin_engine, "pin-vrf")
    foreign = await seed_vrf(admin_engine, b.org_id, name="theirs")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamNotFoundError):
            await get_vrf(session, a.org_id, foreign)


async def test_get_site_from_another_org_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    a, b = await _two_orgs(admin_engine, "pin-site")
    foreign = await seed_site(admin_engine, b.org_id)
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamNotFoundError):
            await get_site(session, a.org_id, foreign)


async def test_get_vlan_from_another_org_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    a, b = await _two_orgs(admin_engine, "pin-vlan")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        foreign = await create_vlan(session, b.org_id, b.user_id, vid=10)
        await session.commit()
        foreign_id = foreign.id
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamNotFoundError):
            await get_vlan(session, a.org_id, foreign_id)


async def test_the_list_queries_are_org_scoped_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """The same pin for the list paths. On the admin engine every other org's
    rows are visible, so a `list_*` without its `org_id` predicate returns the
    whole table -- which through `org_session` is indistinguishable from a
    correct one."""
    a, b = await _two_orgs(admin_engine, "pin-list")
    await seed_vrf(admin_engine, b.org_id, name="theirs")
    await seed_site(admin_engine, b.org_id, name="Their office")
    mine = await seed_vrf(admin_engine, a.org_id, name="mine")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_vlan(session, b.org_id, b.user_id, vid=99)
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        assert [v.id for v in await list_vrfs(session, a.org_id)] == [mine]
        assert await list_sites(session, a.org_id) == []
        assert await list_vlans(session, a.org_id) == []


async def test_deleting_another_orgs_vrf_is_not_found(admin_engine: AsyncEngine) -> None:
    """404 and never 403 for another org's id: a 403 confirms the id exists,
    which is a cross-tenant existence oracle."""
    a, b = await _two_orgs(admin_engine, "pin-del")
    foreign = await seed_vrf(admin_engine, b.org_id, name="theirs")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamNotFoundError):
            await delete_vrf(session, a.org_id, a.user_id, foreign)
    assert await _count(admin_engine, IpamVrf, b.org_id) == 1
