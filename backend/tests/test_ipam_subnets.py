"""Subnets, the derived hierarchy, and the serialised overlap rule
(`docs/modules/ipam/spec.md` §2.4, §2.5, §0.2, §0.3, §11.3).

WHAT THESE FIXTURES DELIBERATELY VARY:

* **`containing_subnet` is never asked a question with one candidate answer.**
  Every fixture for it holds BOTH a wide prefix and a narrow one containing the
  address, so "returns the most specific" and "returns whatever it found first"
  give different answers. With a single candidate the two are indistinguishable
  and the `ORDER BY masklen DESC` could be deleted with nothing going red.
* **The re-parenting test asserts the rows that must NOT move**, not only the
  ones that must. A `recompute_parents` that re-derived the WHOLE table would
  pass a test that only checks the children of the new supernet -- and would
  quietly be O(table) on every subnet write.
* **The overlap test uses two REAL connections and a barrier.** The check
  passes trivially when run twice in sequence: the race is the entire subject,
  and a sequential test cannot see the missing lock.
* **The `org_id`-predicate tests run on the ADMIN engine.** Through
  `org_session` they pass with the service's own predicate deleted, because RLS
  rejects first.
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AuditEvent, IpamAddress, IpamSubnet
from app.modules.ipam.errors import (
    IpamConflictError,
    IpamNotFoundError,
    IpamValidationError,
)
from app.modules.ipam.registry import ensure_default_vrf
from app.modules.ipam.subnets import (
    containing_subnet,
    create_range,
    create_subnet,
    delete_range,
    delete_subnet,
    get_subnet,
    list_ranges,
    list_subnets,
    subnet_tree,
    update_subnet,
)
from tests.ipam_fixtures import IpamOrg, seed_org, seed_subnet, seed_vrf


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


async def _parents(engine: AsyncEngine, org_id: UUID) -> dict[str, UUID | None]:
    """`{prefix: parent_id}` for the whole org -- so a test can assert what did
    NOT move as easily as what did."""
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                select(IpamSubnet.prefix, IpamSubnet.parent_id).where(
                    IpamSubnet.org_id == org_id
                )
            )
        ).all()
    return {str(r.prefix): r.parent_id for r in rows}


async def _fixture(admin_engine: AsyncEngine, tag: str) -> tuple[IpamOrg, UUID]:
    org = await seed_org(admin_engine, tag)
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    return org, vrf_id


# ---------------------------------------------------------------------------
# Containment (§0.1, §0.2)
# ---------------------------------------------------------------------------


async def test_containing_subnet_returns_the_most_specific_match(
    admin_engine: AsyncEngine,
) -> None:
    """TWO candidates, on purpose. An estate legitimately registers a /8 as a
    `container` and a /24 inside it as `assignable`, and an observation at
    10.4.7.9 belongs to the /24.

    IF THE `ORDER BY masklen DESC` WERE DELETED this returns the /8 -- every
    observation in the estate files under the container, every subnet page is
    empty, and no test with a single candidate can tell.
    """
    org, vrf_id = await _fixture(admin_engine, "contain")
    wide = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.0.0.0/8", kind="container"
    )
    narrow = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.4.7.0/24"
    )
    async with AsyncSession(admin_engine) as session:
        found = await containing_subnet(session, org.org_id, vrf_id, "10.4.7.9")
        assert found is not None
        assert found.id == narrow
        assert found.id != wide

        outside = await containing_subnet(session, org.org_id, vrf_id, "192.0.2.1")
    # An address inside no registered subnet is an ORPHAN (§4.2), not an error.
    assert outside is None


async def test_containment_works_for_ipv6(admin_engine: AsyncEngine) -> None:
    """§0.5: IPv6 is STORED, documented and reconciled in v1 -- what is refused
    is ENUMERATION, not the family. Containment is the same operator with no
    second code path, which is what §0.1 bought."""
    org, vrf_id = await _fixture(admin_engine, "v6")
    wide = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="2001:db8::/32", kind="container"
    )
    narrow = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="2001:db8:1::/64"
    )
    async with AsyncSession(admin_engine) as session:
        found = await containing_subnet(session, org.org_id, vrf_id, "2001:db8:1::42")
        assert found is not None and found.id == narrow
        # A v4 address must not match a v6 prefix, and vice versa.
        assert await containing_subnet(session, org.org_id, vrf_id, "10.0.0.1") is None
        assert wide is not None


async def test_text_near_misses_do_not_match(admin_engine: AsyncEngine) -> None:
    """§0.1's motivating example, asserted rather than described. In `text`,
    `10.4.70.0/24` matches `LIKE '10.4.7%'` and `10.4.7.9` sorts before
    `10.4.7.10`. In `cidr` neither is true, and this test is what says so."""
    org, vrf_id = await _fixture(admin_engine, "nearmiss")
    await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.4.7.0/24")
    async with AsyncSession(admin_engine) as session:
        assert await containing_subnet(session, org.org_id, vrf_id, "10.4.70.1") is None
        assert await containing_subnet(session, org.org_id, vrf_id, "10.4.7.10") is not None


async def test_the_gist_index_serves_the_containment_query(
    admin_engine: AsyncEngine,
) -> None:
    """§13: the GiST index is USED for containment, asserted from the plan.

    Seeded with enough rows to make the index attractive -- on a three-row table
    a sequential scan is genuinely cheaper and the planner is right to take it,
    so a small fixture proves the index is unnecessary rather than that it
    works. The query is the bare containment predicate, without the
    `(org_id, vrf_id)` equality that `uq_ipam_subnet_org_vrf_prefix` can serve
    on its own: this test is about `ix_ipam_subnet_prefix_gist` existing and
    being usable, and mixing the two indexes into one plan tests whichever the
    planner costed lower on the day.
    """
    from sqlalchemy import text as sqltext

    org, vrf_id = await _fixture(admin_engine, "gistplan")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add_all(
            [
                IpamSubnet(
                    org_id=org.org_id,
                    vrf_id=vrf_id,
                    prefix=f"10.{block // 256}.{block % 256}.0/24",
                    kind="assignable",
                )
                for block in range(4000)
            ]
        )
        await session.commit()
    async with admin_engine.connect() as conn:
        await conn.execute(sqltext("ANALYZE ipam_subnet"))
        plan = "\n".join(
            r[0]
            for r in (
                await conn.execute(
                    sqltext(
                        "EXPLAIN SELECT id FROM ipam_subnet"
                        " WHERE prefix >>= CAST('10.5.9.7' AS inet)"
                    )
                )
            ).all()
        )
    assert "ix_ipam_subnet_prefix_gist" in plan, plan


# ---------------------------------------------------------------------------
# §0.2 -- the derived parent
# ---------------------------------------------------------------------------


async def test_a_new_subnet_gets_the_smallest_containing_prefix_as_its_parent(
    admin_engine: AsyncEngine,
) -> None:
    org, vrf_id = await _fixture(admin_engine, "parent")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        eight = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.0.0.0/8", kind="container",
            vrf_id=vrf_id,
        )
        sixteen = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.1.0.0/16", kind="container",
            vrf_id=vrf_id,
        )
        leaf = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.1.2.0/24", vrf_id=vrf_id
        )
        await session.commit()
        eight_id, sixteen_id, leaf_id = eight.id, sixteen.id, leaf.id

    parents = await _parents(admin_engine, org.org_id)
    assert parents["10.0.0.0/8"] is None
    assert parents["10.1.0.0/16"] == eight_id
    # THE SMALLEST containing prefix, not the first or the widest.
    assert parents["10.1.2.0/24"] == sixteen_id
    assert leaf_id != sixteen_id


async def test_inserting_a_supernet_reparents_exactly_its_new_children(
    admin_engine: AsyncEngine,
) -> None:
    """§0.2's accepted consequence: a subnet write is NOT a single-row write.

    THE ASSERTIONS ON THE ROWS THAT MUST NOT MOVE ARE HALF THE TEST. Checking
    only 10.1.1.0/24 passes against a derivation that re-points every row in the
    VRF at the newest prefix, or that nulls the ones it does not understand;
    asserting the whole map is what makes those visible.

    STATED HONESTLY, because a guard test must not look stronger than it is:
    this does NOT falsify the `s.prefix <<= :prefix` BOUND on the UPDATE.
    Recomputing every row in the VRF produces identical values -- the bound is a
    COST property (O(rows inside the new prefix) rather than O(table) on every
    subnet write), and no assertion about stored state can see it. What it DOES
    falsify is the derivation itself: with `ORDER BY masklen(p.prefix) DESC`
    flipped to `ASC`, 10.1.1.0/24 parents to 10.0.0.0/8, and this printed
    exactly that when the mutation was run.
    """
    org, vrf_id = await _fixture(admin_engine, "reparent")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        eight = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.0.0.0/8", kind="container",
            vrf_id=vrf_id,
        )
        await create_subnet(session, org.org_id, org.user_id, prefix="10.1.1.0/24", vrf_id=vrf_id)
        await create_subnet(session, org.org_id, org.user_id, prefix="10.2.2.0/24", vrf_id=vrf_id)
        await create_subnet(
            session, org.org_id, org.user_id, prefix="192.168.5.0/24", vrf_id=vrf_id
        )
        await session.commit()
        eight_id = eight.id

    before = await _parents(admin_engine, org.org_id)
    assert before["10.1.1.0/24"] == eight_id
    assert before["10.2.2.0/24"] == eight_id
    assert before["192.168.5.0/24"] is None

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        sixteen = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.1.0.0/16", kind="container",
            vrf_id=vrf_id,
        )
        await session.commit()
        sixteen_id = sixteen.id

    after = await _parents(admin_engine, org.org_id)
    assert after == {
        "10.0.0.0/8": None,
        "10.1.0.0/16": eight_id,
        "10.1.1.0/24": sixteen_id,  # moved
        "10.2.2.0/24": eight_id,  # NOT moved
        "192.168.5.0/24": None,  # NOT moved
    }


async def test_deleting_a_container_reattaches_its_children(
    admin_engine: AsyncEngine,
) -> None:
    """The mirror of the test above. `parent_id` is a CACHE, so a delete that
    leaves it pointing at a dead row -- or that the foreign key refuses --
    is the same defect from the other end."""
    org, vrf_id = await _fixture(admin_engine, "unparent")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        eight = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.0.0.0/8", kind="container",
            vrf_id=vrf_id,
        )
        sixteen = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.1.0.0/16", kind="container",
            vrf_id=vrf_id,
        )
        await create_subnet(session, org.org_id, org.user_id, prefix="10.1.2.0/24", vrf_id=vrf_id)
        await session.commit()
        eight_id, sixteen_id = eight.id, sixteen.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await delete_subnet(session, org.org_id, org.user_id, sixteen_id)
        await session.commit()

    parents = await _parents(admin_engine, org.org_id)
    assert parents == {"10.0.0.0/8": None, "10.1.2.0/24": eight_id}


async def test_parent_id_is_not_a_field_a_client_can_set(
    admin_engine: AsyncEngine,
) -> None:
    """§0.2. `create_subnet` has no `parent_id` parameter at all, and the update
    path REFUSES it rather than ignoring it -- silently dropping it returns
    success for a request that did not do what it said."""
    org, vrf_id = await _fixture(admin_engine, "noparent")
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.7.0.0/24"
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamValidationError, match="unknown or server-owned"):
            await update_subnet(
                session, org.org_id, org.user_id, subnet_id, {"parent_id": uuid4()}
            )
        with pytest.raises(IpamValidationError, match="unknown or server-owned"):
            await update_subnet(
                session, org.org_id, org.user_id, subnet_id, {"prefix": "10.8.0.0/24"}
            )


async def test_subnet_tree_nests_by_the_derived_parent(admin_engine: AsyncEngine) -> None:
    org, vrf_id = await _fixture(admin_engine, "tree")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_subnet(
            session, org.org_id, org.user_id, prefix="10.0.0.0/8", kind="container",
            vrf_id=vrf_id,
        )
        await create_subnet(session, org.org_id, org.user_id, prefix="10.1.2.0/24", vrf_id=vrf_id)
        await create_subnet(
            session, org.org_id, org.user_id, prefix="192.168.5.0/24", vrf_id=vrf_id
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        roots = await subnet_tree(session, org.org_id, vrf_id=vrf_id)
    assert [str(node["subnet"].prefix) for node in roots] == ["10.0.0.0/8", "192.168.5.0/24"]
    assert [str(child["subnet"].prefix) for child in roots[0]["children"]] == ["10.1.2.0/24"]
    assert roots[1]["children"] == []


# ---------------------------------------------------------------------------
# §0.3 -- overlap, under the advisory lock
# ---------------------------------------------------------------------------


async def test_an_assignable_subnet_inside_another_assignable_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """At most one subnet that HOLDS ADDRESSES may cover an address. Two of them
    make "which subnet holds 10.40.1.5" have two answers, and utilisation,
    next-free, the sweep target set and orphan detection all ask exactly that."""
    org, vrf_id = await _fixture(admin_engine, "ov1")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_subnet(session, org.org_id, org.user_id, prefix="10.40.0.0/16", vrf_id=vrf_id)
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="overlaps the assignable subnet"):
            await create_subnet(
                session, org.org_id, org.user_id, prefix="10.40.1.0/24", vrf_id=vrf_id
            )


async def test_an_assignable_subnet_inside_a_container_is_the_hierarchy(
    admin_engine: AsyncEngine,
) -> None:
    """THE OTHER HALF, and the one a blanket "no overlap at all" rule breaks.
    A supernet MUST overlap its children -- that IS the hierarchy (§0.3) -- and
    `container` is the kind that exists to say "this prefix is here so the tree
    reads, it holds no addresses"."""
    org, vrf_id = await _fixture(admin_engine, "ov2")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_subnet(
            session, org.org_id, org.user_id, prefix="10.41.0.0/16", kind="container",
            vrf_id=vrf_id,
        )
        await create_subnet(session, org.org_id, org.user_id, prefix="10.41.1.0/24", vrf_id=vrf_id)
        await create_subnet(session, org.org_id, org.user_id, prefix="10.41.2.0/24", vrf_id=vrf_id)
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert len(await list_subnets(session, org.org_id, vrf_id=vrf_id)) == 3


async def test_promoting_a_container_that_would_then_overlap_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """The update path takes the same lock and the same check. A `kind` edit
    that skipped it is how the rule is bypassed without anybody ever writing an
    overlapping prefix."""
    org, vrf_id = await _fixture(admin_engine, "ov3")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        container = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.42.0.0/16", kind="container",
            vrf_id=vrf_id,
        )
        await create_subnet(session, org.org_id, org.user_id, prefix="10.42.1.0/24", vrf_id=vrf_id)
        await session.commit()
        container_id = container.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="overlaps the assignable subnet"):
            await update_subnet(
                session, org.org_id, org.user_id, container_id, {"kind": "assignable"}
            )


async def test_the_same_prefix_in_one_vrf_is_refused_readably(
    admin_engine: AsyncEngine,
) -> None:
    """Uniqueness stays the database's job (§0.3), but the service turns the
    `IntegrityError` into a 409 that names the prefix rather than a 500 that
    names an index."""
    org, vrf_id = await _fixture(admin_engine, "dupe")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_subnet(session, org.org_id, org.user_id, prefix="10.43.0.0/24", vrf_id=vrf_id)
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="already registered"):
            await create_subnet(
                session, org.org_id, org.user_id, prefix="10.43.0.0/24", vrf_id=vrf_id
            )


async def test_two_concurrent_overlapping_inserts_serialise_and_the_second_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """THE §0.3 TEST, and it needs two real connections and a barrier.

    IF THE `pg_advisory_xact_lock` WERE REMOVED, each transaction reads a
    snapshot in which the other's row does not exist yet, both overlap checks
    pass, both insert, and the org is left holding TWO assignable subnets
    covering 10.44.1.5 -- with nothing raised and nothing logged. This test then
    observes two successes and two rows instead of one of each.

    A sequential version of this test passes against the unlocked
    implementation, which is why it is written this way.
    """
    org, vrf_id = await _fixture(admin_engine, "race")
    barrier = asyncio.Barrier(2)

    async def _create(prefix: str) -> str:
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            await barrier.wait()
            try:
                await create_subnet(
                    session, org.org_id, org.user_id, prefix=prefix, vrf_id=vrf_id
                )
                await session.commit()
            except IpamConflictError as exc:
                await session.rollback()
                return f"refused: {exc}"
            return "created"

    outcomes = await asyncio.gather(_create("10.44.0.0/16"), _create("10.44.1.0/24"))
    async with AsyncSession(admin_engine) as session:
        stored = [str(s.prefix) for s in await list_subnets(session, org.org_id)]

    assert sorted(o.split(":")[0] for o in outcomes) == ["created", "refused"], (
        f"{outcomes} left {stored}"
    )
    assert len(stored) == 1, stored


# ---------------------------------------------------------------------------
# Validation the database cannot do
# ---------------------------------------------------------------------------


async def test_a_gateway_outside_its_prefix_gets_a_readable_refusal(
    admin_engine: AsyncEngine,
) -> None:
    """Belt and braces with `ck_ipam_subnet_gateway_in`. The CHECK is what
    actually holds; this is the difference between a 400 naming the gateway and
    a 500 naming a constraint."""
    org, vrf_id = await _fixture(admin_engine, "gw")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamValidationError, match="is not inside"):
            await create_subnet(
                session,
                org.org_id,
                org.user_id,
                prefix="10.45.0.0/24",
                gateway="192.168.1.1",
                vrf_id=vrf_id,
            )
        with pytest.raises(IpamValidationError, match="not a valid address"):
            await create_subnet(
                session,
                org.org_id,
                org.user_id,
                prefix="10.46.0.0/24",
                gateway="the router",
                vrf_id=vrf_id,
            )


async def test_a_prefix_with_host_bits_is_normalised_and_the_audit_records_both(
    admin_engine: AsyncEngine,
) -> None:
    """Postgres's `cidr` refuses `10.47.0.1/24` outright ("bits set to right of
    mask"), which is a message about masks rather than about the operator's
    subnet. This normalises like `documentation`'s validator and §8.3's
    converter do -- and records the string that was TYPED alongside the one that
    was STORED, so the audit log does not quietly claim the operator wrote the
    normalised form."""
    org, vrf_id = await _fixture(admin_engine, "hostbits")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        subnet = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.47.0.1/24", vrf_id=vrf_id
        )
        await session.commit()
        assert str(subnet.prefix) == "10.47.0.0/24"

    async with AsyncSession(admin_engine) as session:
        metadata = (
            await session.execute(
                select(AuditEvent.meta).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "ipam.subnet_created",
                )
            )
        ).scalar_one()
    assert metadata["prefix"] == "10.47.0.0/24"
    assert metadata["requested_prefix"] == "10.47.0.1/24"


async def test_create_subnet_creates_the_default_vrf_when_none_is_given(
    admin_engine: AsyncEngine,
) -> None:
    """§0.7's lazy `global` row, exercised through its real caller: the named
    client's, one per client since migration 0240."""
    org = await seed_org(admin_engine, "lazyvrf")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        subnet = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.48.0.0/24", client_id=org.org_id
        )
        await session.commit()
        vrf_id = subnet.vrf_id
    async with AsyncSession(admin_engine) as session:
        assert await ensure_default_vrf(session, org.org_id, org.org_id) == vrf_id


# ---------------------------------------------------------------------------
# Ranges (§2.5)
# ---------------------------------------------------------------------------


async def test_a_range_that_spills_outside_its_subnet_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """A CHECK cannot reach the parent row (§2.5), so this rule lives in the
    service and NOWHERE else -- which is exactly why it needs its own test."""
    org, vrf_id = await _fixture(admin_engine, "rng1")
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.50.0.0/24"
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamValidationError, match="outside"):
            await create_range(
                session,
                org.org_id,
                org.user_id,
                subnet_id,
                kind="dhcp_pool",
                start_address="10.50.0.100",
                end_address="10.50.1.200",
            )
        with pytest.raises(IpamValidationError, match="is after"):
            await create_range(
                session,
                org.org_id,
                org.user_id,
                subnet_id,
                kind="dhcp_pool",
                start_address="10.50.0.200",
                end_address="10.50.0.100",
            )
    assert await _audit_actions(admin_engine, org.org_id) == []


async def test_two_overlapping_ranges_in_one_subnet_are_refused(
    admin_engine: AsyncEngine,
) -> None:
    """Two overlapping ranges make "is this address in a DHCP pool" ambiguous,
    and §3.2 refuses a static assignment on the strength of that answer.

    Three cases, because a naive `start BETWEEN` check passes the first two: an
    overlap on the left, on the right, and a range that STRADDLES the existing
    one entirely.
    """
    org, vrf_id = await _fixture(admin_engine, "rng2")
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.51.0.0/24"
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_range(
            session,
            org.org_id,
            org.user_id,
            subnet_id,
            kind="dhcp_pool",
            start_address="10.51.0.100",
            end_address="10.51.0.200",
        )
        await session.commit()

    for start, end in (
        ("10.51.0.50", "10.51.0.120"),
        ("10.51.0.150", "10.51.0.250"),
        ("10.51.0.10", "10.51.0.250"),
    ):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            with pytest.raises(IpamConflictError, match="overlaps the existing range"):
                await create_range(
                    session,
                    org.org_id,
                    org.user_id,
                    subnet_id,
                    kind="reserved",
                    start_address=start,
                    end_address=end,
                )

    # ... and an ADJACENT, non-overlapping range is accepted. Without this the
    # test above passes against a check that refuses everything.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_range(
            session,
            org.org_id,
            org.user_id,
            subnet_id,
            kind="reserved",
            start_address="10.51.0.201",
            end_address="10.51.0.220",
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert len(await list_ranges(session, org.org_id, subnet_id)) == 2


async def test_range_writes_are_audited(admin_engine: AsyncEngine) -> None:
    org, vrf_id = await _fixture(admin_engine, "rng3")
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.52.0.0/24"
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await create_range(
            session,
            org.org_id,
            org.user_id,
            subnet_id,
            kind="dhcp_pool",
            start_address="10.52.0.100",
            end_address="10.52.0.200",
        )
        await delete_range(session, org.org_id, org.user_id, row.id)
        await session.commit()
    assert await _audit_actions(admin_engine, org.org_id) == [
        "ipam.range_created",
        "ipam.range_deleted",
    ]


# ---------------------------------------------------------------------------
# Delete, cascade, and §0.4's "rows do not evaporate"
# ---------------------------------------------------------------------------


async def test_deleting_a_subnet_holding_addresses_is_refused_by_name(
    admin_engine: AsyncEngine,
) -> None:
    org, vrf_id = await _fixture(admin_engine, "del1")
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.53.0.0/24"
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            IpamAddress(
                org_id=org.org_id,
                subnet_id=subnet_id,
                vrf_id=vrf_id,
                address="10.53.0.5",
                state="reserved",
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="1 address"):
            await delete_subnet(session, org.org_id, org.user_id, subnet_id)


async def test_cascade_keeps_the_address_row_and_nulls_its_subnet(
    admin_engine: AsyncEngine,
) -> None:
    """§0.4: sparse means "no row until first mentioned", NOT "rows evaporate".

    IF CASCADE DELETED THE ADDRESSES, everything documented about them --
    claimant, MAC, hostname, the fact that somebody reserved it -- would vanish
    with a subnet somebody re-registered a minute later. Nulled instead, the
    address becomes an ORPHAN (§4.2), which is a finding on a page.
    """
    org, vrf_id = await _fixture(admin_engine, "del2")
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.54.0.0/24"
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            IpamAddress(
                org_id=org.org_id,
                subnet_id=subnet_id,
                vrf_id=vrf_id,
                address="10.54.0.5",
                state="assigned",
                asset_label="the printer",
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await delete_subnet(session, org.org_id, org.user_id, subnet_id, cascade=True)
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                select(IpamAddress).where(IpamAddress.org_id == org.org_id)
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].subnet_id is None
    assert rows[0].asset_label == "the printer"
    assert rows[0].state == "assigned"


async def test_subnet_writes_are_audited_exactly_once_each(
    admin_engine: AsyncEngine,
) -> None:
    """§11.3, asserted as an exact ordered sequence -- an action that fires
    TWICE is as much a defect as one that never fires."""
    org, vrf_id = await _fixture(admin_engine, "audit")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        subnet = await create_subnet(
            session, org.org_id, org.user_id, prefix="10.55.0.0/24", vrf_id=vrf_id
        )
        await update_subnet(
            session, org.org_id, org.user_id, subnet.id, {"name": "Manila floor 2"}
        )
        await delete_subnet(session, org.org_id, org.user_id, subnet.id)
        await session.commit()
    assert await _audit_actions(admin_engine, org.org_id) == [
        "ipam.subnet_created",
        "ipam.subnet_updated",
        "ipam.subnet_deleted",
    ]


# ---------------------------------------------------------------------------
# The application `org_id` predicate, pinned on the ADMIN engine
# ---------------------------------------------------------------------------


async def test_a_subnet_from_another_org_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE (invariant #5). Through `org_session` this
    passes with `IpamSubnet.org_id == org_id` deleted, because RLS rejects the
    row first; here the predicate is the only thing left to reject."""
    a, a_vrf = await _fixture(admin_engine, "pin-a")
    b, b_vrf = await _fixture(admin_engine, "pin-b")
    foreign = await seed_subnet(
        admin_engine, b.org_id, vrf_id=b_vrf, prefix="10.56.0.0/24"
    )
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamNotFoundError):
            await get_subnet(session, a.org_id, foreign)
        with pytest.raises(IpamNotFoundError):
            await list_ranges(session, a.org_id, foreign)
    assert a_vrf != b_vrf


async def test_list_and_containment_are_org_scoped_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """The same pin for the read paths, including the CONTAINMENT one -- which
    is the query most likely to be written as pure address arithmetic with the
    tenant predicate forgotten, because the arithmetic looks like the whole
    question."""
    a, a_vrf = await _fixture(admin_engine, "pinl-a")
    b, b_vrf = await _fixture(admin_engine, "pinl-b")
    await seed_subnet(admin_engine, b.org_id, vrf_id=b_vrf, prefix="10.57.0.0/24")
    async with AsyncSession(admin_engine) as session:
        assert await list_subnets(session, a.org_id) == []
        assert await containing_subnet(session, a.org_id, a_vrf, "10.57.0.5") is None
        assert await containing_subnet(session, b.org_id, b_vrf, "10.57.0.5") is not None


async def test_deleting_another_orgs_subnet_is_not_found(admin_engine: AsyncEngine) -> None:
    """404 and never 403: a 403 confirms the id exists, which is a cross-tenant
    existence oracle."""
    a, _ = await _fixture(admin_engine, "pind-a")
    b, b_vrf = await _fixture(admin_engine, "pind-b")
    foreign = await seed_subnet(
        admin_engine, b.org_id, vrf_id=b_vrf, prefix="10.58.0.0/24"
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamNotFoundError):
            await delete_subnet(session, a.org_id, a.user_id, foreign)
    async with AsyncSession(admin_engine) as session:
        assert len(await list_subnets(session, b.org_id)) == 1


async def test_overlap_is_scoped_to_one_vrf(admin_engine: AsyncEngine) -> None:
    """Two routing domains legitimately both hold 10.0.0.0/8 (§2.4's unique key
    is keyed on `vrf_id` for exactly this). An overlap check that forgot the VRF
    predicate would refuse the second customer's entire address plan."""
    org, first_vrf = await _fixture(admin_engine, "vrfscope")
    second_vrf = await seed_vrf(admin_engine, org.org_id, name="customer-b")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_subnet(
            session, org.org_id, org.user_id, prefix="10.60.0.0/16", vrf_id=first_vrf
        )
        await create_subnet(
            session, org.org_id, org.user_id, prefix="10.60.1.0/24", vrf_id=second_vrf
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert len(await list_subnets(session, org.org_id)) == 2
