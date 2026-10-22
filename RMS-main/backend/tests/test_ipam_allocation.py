"""Derived `status`, utilisation, next-free and the atomic allocator
(`docs/modules/ipam/spec.md` §3.1, §3.3, §0.4, §0.5, §0.16).

WHAT THESE FIXTURES DELIBERATELY VARY:

* **`derive_status` is exercised with MORE THAN ONE PREDICATE TRUE at a time.**
  The table is a chain evaluated top to bottom, first match wins (§0.16), and a
  case that satisfies exactly one row cannot detect a reordered table. Every
  precedence pair below is a case where two rows both match and the assertion
  is on which one wins.
* **Both S6 rows are asserted REACHABLE.** `assigned` was unreachable in the
  spec as first written -- `dormant` sat above it and fired on
  `¬(E ∧ L)`, which includes the never-looked-at case. A table test that only
  checked `dormant` would have passed against exactly that bug.
* **The observation fixtures are not all fresh.** `next_free`'s "no current
  observation" clause is bounded by `W_obs`, and a fixture that only seeds
  `last_seen_at = now()` cannot see a missing currency filter -- it would
  observe the same answer either way.
* **The blockers are seeded one at a time, each on its own address.** Seeded
  together, deleting any one exclusion clause still leaves every other address
  blocked by a different clause, and the test passes against the bug.
* **The concurrency test issues a WARM-UP QUERY before the barrier.** Without
  it both tasks reach the barrier with no connection checked out, serialise on
  connection acquisition, and the test passes against an unlocked
  implementation. That is the exact defect that shipped in this module's
  previous concurrency test.
* **The `org_id`-predicate tests run on the ADMIN engine**, where RLS is not the
  thing rejecting.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AuditEvent, IpamAddress, Policy
from app.modules.ipam.allocation import (
    ADDRESS_STATUSES,
    allocate,
    derive_status,
    enumeration_max_hosts,
    next_free,
    utilization,
)
from app.modules.ipam.errors import (
    IpamConflictError,
    IpamNotFoundError,
    IpamTooLargeError,
    IpamValidationError,
)
from app.modules.ipam.subnets import create_range
from tests.ipam_fixtures import (
    IpamOrg,
    seed_address,
    seed_org,
    seed_subnet,
    seed_sweep_run,
    seed_vlan,
    seed_vrf,
    upsert_observation_row,
)

# ---------------------------------------------------------------------------
# §3.1 -- the derived status, as a pure table
# ---------------------------------------------------------------------------

#: `(label, kwargs, expected)`. One case per row of §3.1, then the precedence
#: pairs. `state=None` means "no `ipam_address` row at all", which §0.4's
#: sparseness makes the common case.
_BASE = {
    "state": None,
    "in_dhcp_pool": False,
    "has_current_observation": False,
    "conflicting": False,
    "gate_e": False,
    "gate_l": True,
    "gate_h": False,
}

_CASES: list[tuple[str, dict, str]] = [
    # Row 1
    ("dhcp pool", {"in_dhcp_pool": True}, "dhcp"),
    # Row 2
    ("conflict", {"conflicting": True}, "conflicting"),
    # Row 3
    (
        "assigned and answering",
        {"state": "assigned", "has_current_observation": True, "gate_e": True, "gate_h": True},
        "in_use",
    ),
    # Row 4 -- both states that can be stale
    (
        "assigned, silent, evidence available, claimant live",
        {"state": "assigned", "gate_e": True, "gate_l": True, "gate_h": True},
        "stale",
    ),
    (
        "reserved, silent, evidence available",
        {"state": "reserved", "gate_e": True, "gate_l": True, "gate_h": True},
        "stale",
    ),
    # Row 5 -- S6's separated case: nothing has EVER looked
    (
        "assigned, silent, nothing has ever looked",
        {"state": "assigned", "gate_e": False, "gate_h": False},
        "assigned",
    ),
    # Row 6 -- something looked before, nothing can look now
    (
        "assigned, silent, evidence existed once, none now",
        {"state": "assigned", "gate_e": False, "gate_h": True},
        "dormant",
    ),
    (
        "assigned, silent, evidence now but claimant offline",
        {"state": "assigned", "gate_e": True, "gate_l": False, "gate_h": True},
        "dormant",
    ),
    (
        "reserved, silent, evidence existed once",
        {"state": "reserved", "gate_e": False, "gate_h": True},
        "dormant",
    ),
    # Row 7
    ("reserved and answering", {"state": "reserved", "has_current_observation": True}, "reserved"),
    ("reserved, nothing has ever looked", {"state": "reserved", "gate_h": False}, "reserved"),
    # Row 8
    ("no row, answering", {"has_current_observation": True}, "undocumented"),
    ("free row, answering", {"state": "free", "has_current_observation": True}, "undocumented"),
    (
        "deprecated row, answering",
        {"state": "deprecated", "has_current_observation": True},
        "undocumented",
    ),
    # Row 9
    ("no row, silent", {}, "free"),
    ("free row, silent", {"state": "free"}, "free"),
    ("deprecated row, silent", {"state": "deprecated", "gate_e": True, "gate_h": True}, "free"),
]


@pytest.mark.parametrize("label,delta,expected", _CASES, ids=[c[0] for c in _CASES])
def test_derive_status_table(label: str, delta: dict, expected: str) -> None:
    assert derive_status(**(_BASE | delta)) == expected


_PRECEDENCE: list[tuple[str, dict, str]] = [
    (
        "dhcp beats everything below it",
        {
            "in_dhcp_pool": True,
            "conflicting": True,
            "state": "assigned",
            "has_current_observation": True,
            "gate_e": True,
            "gate_h": True,
        },
        "dhcp",
    ),
    (
        "conflicting beats in_use",
        {
            "conflicting": True,
            "state": "assigned",
            "has_current_observation": True,
            "gate_e": True,
            "gate_h": True,
        },
        "conflicting",
    ),
    (
        "conflicting beats undocumented",
        {"conflicting": True, "state": None, "has_current_observation": True},
        "conflicting",
    ),
    (
        "stale beats dormant when both gates pass",
        {"state": "assigned", "gate_e": True, "gate_l": True, "gate_h": True},
        "stale",
    ),
    (
        "stale beats reserved",
        {"state": "reserved", "gate_e": True, "gate_l": True, "gate_h": True},
        "stale",
    ),
    (
        "dormant beats reserved",
        {"state": "reserved", "gate_e": False, "gate_h": True},
        "dormant",
    ),
]


@pytest.mark.parametrize("label,delta,expected", _PRECEDENCE, ids=[c[0] for c in _PRECEDENCE])
def test_derive_status_precedence(label: str, delta: dict, expected: str) -> None:
    """EVERY CASE HERE SATISFIES AT LEAST TWO ROWS OF §3.1.

    IF THE TABLE WERE REORDERED -- `conflicting` below `in_use`, `dormant`
    above `stale`, `dhcp` anywhere but first -- each of these observes the
    OTHER matching row's value. The single-predicate cases above cannot: they
    match one row whichever order the branches are written in.
    """
    assert derive_status(**(_BASE | delta)) == expected


def test_both_s6_rows_are_reachable() -> None:
    """§0.16, and the reason it was written.

    `dormant` originally fired on `¬(Gate E ∧ Gate L)`, which INCLUDES "nothing
    has ever looked at this segment" -- so the `assigned` row, documented as
    exactly that case, was unreachable and no test noticed. Gate H is the
    mechanism that separates them; the ordering is the safety net.

    IF GATE H WERE DROPPED FROM ROW 5 this observes `dormant` for the
    never-looked-at case, and `assigned` becomes a status the product never
    renders.
    """
    never_looked = _BASE | {"state": "assigned", "gate_e": False, "gate_h": False}
    looked_before = _BASE | {"state": "assigned", "gate_e": False, "gate_h": True}
    assert derive_status(**never_looked) == "assigned"
    assert derive_status(**looked_before) == "dormant"
    assert derive_status(**never_looked) != derive_status(**looked_before)


def test_derive_status_is_total_over_every_input_combination() -> None:
    """Exhaustive over the whole input space -- 6 booleans and 5 states, 320
    cases -- asserting only that the result is one of §3.1's nine values.

    A chain of `elif`s with no final `else` returns `None` for whatever
    combination nobody thought of, and `None` renders as an empty cell rather
    than as an error.
    """
    seen: set[str] = set()
    for state in (None, "free", "reserved", "assigned", "deprecated"):
        for bits in range(64):
            kwargs = {
                "state": state,
                "in_dhcp_pool": bool(bits & 1),
                "has_current_observation": bool(bits & 2),
                "conflicting": bool(bits & 4),
                "gate_e": bool(bits & 8),
                "gate_l": bool(bits & 16),
                "gate_h": bool(bits & 32),
            }
            result = derive_status(**kwargs)
            assert result in ADDRESS_STATUSES, (kwargs, result)
            seen.add(result)
    # Every one of the nine is reachable -- including `assigned` (S6).
    assert seen == set(ADDRESS_STATUSES)


def test_derive_status_refuses_a_derived_value_in_state() -> None:
    """§0.6: `in_use`, `stale` and `conflicting` are NOT values of `state`, ever.
    A caller passing one has read the split backwards, and a silent answer would
    make that readable as working code."""
    with pytest.raises(IpamValidationError):
        derive_status(**(_BASE | {"state": "in_use"}))


# ---------------------------------------------------------------------------
# Fixtures for the database-backed half
# ---------------------------------------------------------------------------


async def _fixture(engine: AsyncEngine, tag: str, prefix: str = "10.20.0.0/24", **kw):
    org = await seed_org(engine, tag)
    vrf_id = await seed_vrf(engine, org.org_id)
    subnet_id = await seed_subnet(engine, org.org_id, vrf_id=vrf_id, prefix=prefix, **kw)
    return org, vrf_id, subnet_id


async def _next_free(engine: AsyncEngine, org_id: UUID, subnet_id: UUID, count: int = 3):
    async with AsyncSession(engine) as session:
        return await next_free(session, org_id, subnet_id, count)


async def _add_address(engine: AsyncEngine, org: IpamOrg, vrf_id: UUID, subnet_id: UUID,
                       address: str, state: str) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            IpamAddress(
                org_id=org.org_id,
                vrf_id=vrf_id,
                subnet_id=subnet_id,
                address=address,
                state=state,
                device_id=org.device_id if state == "assigned" else None,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# The enumeration ceiling: ONE RULE, TWO APPLICATIONS (§0.4, §0.5)
# ---------------------------------------------------------------------------


async def test_enumeration_max_hosts_defaults_and_is_read_per_org(
    admin_engine: AsyncEngine,
) -> None:
    """TWO ORGS, TWO POLICIES, on the ADMIN engine -- one policy row cannot tell
    "this org's ceiling" from "the only ceiling there is"."""
    org_a = await seed_org(admin_engine, "ceila")
    org_b = await seed_org(admin_engine, "ceilb")
    async with AsyncSession(admin_engine) as session:
        assert await enumeration_max_hosts(session, org_a.org_id) == 65_536
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Policy(org_id=org_a.org_id, module="ipam", config={"enumeration_max_hosts": 256})
        )
        session.add(
            Policy(org_id=org_b.org_id, module="ipam", config={"enumeration_max_hosts": 4096})
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert await enumeration_max_hosts(session, org_a.org_id) == 256
        assert await enumeration_max_hosts(session, org_b.org_id) == 4096


async def test_a_slash_24_enumerates(admin_engine: AsyncEngine) -> None:
    org, _, subnet_id = await _fixture(admin_engine, "en24")
    assert await _next_free(admin_engine, org.org_id, subnet_id, 2) == ["10.20.0.1", "10.20.0.2"]


async def test_a_slash_12_is_refused_above_the_ceiling(admin_engine: AsyncEngine) -> None:
    """§0.4: a /12 is 1,048,574 hosts. The answer is not slow, it is
    arithmetically hopeless, so it is refused with a message rather than
    attempted.

    On its own this is a "DID NOT RAISE" test, which is exactly the shape this
    project has been bitten by -- so it is one half of a pair.
    `test_the_ceiling_is_the_orgs_policy_not_a_constant` is the other: the SAME
    subnet returns a list under one ceiling and raises under another, which is a
    difference in VALUES. Deleting the ceiling check makes that one observe
    `['10.30.0.1']` where it expects the refusal.
    """
    org, _, subnet_id = await _fixture(admin_engine, "en12", prefix="10.0.0.0/12")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamTooLargeError, match="too large to enumerate"):
            await next_free(session, org.org_id, subnet_id, 1)


async def test_utilisation_above_the_ceiling_is_counts_with_a_null_percentage(
    admin_engine: AsyncEngine,
) -> None:
    """NOT ZERO. NOT AN ESTIMATE. NULL.

    IF THE PERCENTAGE WERE COMPUTED ANYWAY (or defaulted to 0.0) this observes a
    number, and an operator reads "0% used" off a /12 holding a documented
    address. The counts are still real, which is why they are asserted too.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "util12", prefix="10.0.0.0/12")
    await _add_address(admin_engine, org, vrf_id, subnet_id, "10.0.0.5", "assigned")
    await _add_address(admin_engine, org, vrf_id, subnet_id, "10.0.0.6", "reserved")
    await upsert_observation_row(admin_engine, org.org_id, vrf_id=vrf_id, address="10.0.0.7")

    async with AsyncSession(admin_engine) as session:
        report = await utilization(session, org.org_id, subnet_id)
    assert report.enumerable is False
    assert report.percentage is None
    assert report.capacity is None
    assert report.free is None
    assert (report.assigned, report.reserved, report.observed) == (1, 1, 1)


async def test_ipv6_is_refused_for_enumeration_but_documented_and_observed(
    admin_engine: AsyncEngine,
) -> None:
    """§0.5's constraint is "NO ENUMERATION", not "no IPv6".

    A /64 holds 1.8x10^19 addresses; "next free" there is a category error, not
    a hard query. But the address record, the observation and the utilisation
    counts all work -- which is what makes this one rule with two applications
    rather than a family special case.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "v6", prefix="2001:db8:1::/64")
    await _add_address(admin_engine, org, vrf_id, subnet_id, "2001:db8:1::5", "assigned")
    await upsert_observation_row(
        admin_engine, org.org_id, vrf_id=vrf_id, address="2001:db8:1::9"
    )
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamTooLargeError, match="too large to enumerate"):
            await next_free(session, org.org_id, subnet_id, 1)
        report = await utilization(session, org.org_id, subnet_id)
    assert report.enumerable is False
    assert report.percentage is None
    assert (report.assigned, report.observed) == (1, 1)


async def test_the_ceiling_is_the_orgs_policy_not_a_constant(
    admin_engine: AsyncEngine,
) -> None:
    """A /22 (1022 hosts) enumerates by default and is refused under a policy of
    256. IF THE CEILING WERE HARDCODED this observes a list instead of the
    refusal."""
    org, _, subnet_id = await _fixture(admin_engine, "ceil22", prefix="10.30.0.0/22")
    assert await _next_free(admin_engine, org.org_id, subnet_id, 1) == ["10.30.0.1"]
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Policy(org_id=org.org_id, module="ipam", config={"enumeration_max_hosts": 256})
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamTooLargeError):
            await next_free(session, org.org_id, subnet_id, 1)


# ---------------------------------------------------------------------------
# next-free: RFC 3021 and every exclusion (§3.3)
# ---------------------------------------------------------------------------


async def test_network_and_broadcast_are_skipped_on_a_slash_24(
    admin_engine: AsyncEngine,
) -> None:
    """IF THE BOUNDS WERE `0 .. num_addresses - 1` this observes `10.20.0.0`
    first -- the network address, handed to a machine as a static config."""
    org, _, subnet_id = await _fixture(admin_engine, "bounds")
    first = await _next_free(admin_engine, org.org_id, subnet_id, 1)
    assert first == ["10.20.0.1"]
    async with AsyncSession(admin_engine) as session:
        report = await utilization(session, org.org_id, subnet_id)
    assert report.capacity == 254  # not 256


async def test_a_slash_31_yields_both_hosts(admin_engine: AsyncEngine) -> None:
    """RFC 3021. IF THE `/31` SPECIAL CASE WERE MISSING this observes `[]` --
    a point-to-point link that the allocator says is full."""
    org, _, subnet_id = await _fixture(admin_engine, "p2p", prefix="10.21.0.0/31")
    assert await _next_free(admin_engine, org.org_id, subnet_id, 5) == [
        "10.21.0.0",
        "10.21.0.1",
    ]


async def test_a_slash_32_yields_its_single_address(admin_engine: AsyncEngine) -> None:
    org, _, subnet_id = await _fixture(admin_engine, "host", prefix="10.22.0.7/32")
    assert await _next_free(admin_engine, org.org_id, subnet_id, 5) == ["10.22.0.7"]


async def test_each_exclusion_blocks_on_its_own(admin_engine: AsyncEngine) -> None:
    """FIVE BLOCKERS, EACH ON ITS OWN ADDRESS, asserted by which addresses come
    back rather than by a count.

    SEEDED TOGETHER ON ONE ADDRESS, deleting any single exclusion clause would
    leave that address blocked by a different clause and the test would pass
    against the bug. Here `.1` is a DHCP pool, `.2` a reserved range, `.3` an
    excluded range, `.4` an assigned row, `.5` a reserved row, `.6` a
    deprecated row and `.7` a current observation with NO ROW AT ALL -- so
    deleting any one clause observes exactly that address in the result.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "excl")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for kind, start, end in (
            ("dhcp_pool", "10.20.0.1", "10.20.0.1"),
            ("reserved", "10.20.0.2", "10.20.0.2"),
            ("excluded", "10.20.0.3", "10.20.0.3"),
        ):
            await create_range(
                session,
                org.org_id,
                org.user_id,
                subnet_id,
                kind=kind,
                start_address=start,
                end_address=end,
            )
        await session.commit()
    await _add_address(admin_engine, org, vrf_id, subnet_id, "10.20.0.4", "assigned")
    await _add_address(admin_engine, org, vrf_id, subnet_id, "10.20.0.5", "reserved")
    await _add_address(admin_engine, org, vrf_id, subnet_id, "10.20.0.6", "deprecated")
    await upsert_observation_row(
        admin_engine, org.org_id, vrf_id=vrf_id, address="10.20.0.7"
    )

    assert await _next_free(admin_engine, org.org_id, subnet_id, 2) == [
        "10.20.0.8",
        "10.20.0.9",
    ]


async def test_a_free_row_does_not_block(admin_engine: AsyncEngine) -> None:
    """§0.4: release keeps the row and sets `state='free'`. IF THE STATE FILTER
    WERE DROPPED (any row blocking) a released address could never be handed out
    again, and the address plan would run out of addresses it owns."""
    org, vrf_id, subnet_id = await _fixture(admin_engine, "freed")
    await _add_address(admin_engine, org, vrf_id, subnet_id, "10.20.0.1", "free")
    assert await _next_free(admin_engine, org.org_id, subnet_id, 1) == ["10.20.0.1"]


async def test_a_current_observation_blocks_an_address_with_no_row(
    admin_engine: AsyncEngine,
) -> None:
    """THE RECONCILIATION PAYING FOR ITSELF (§3.3).

    An address that answers on the wire is not free whatever the spreadsheet
    says, and handing it out is an outage. IF THE OBSERVATION CLAUSE WERE
    DELETED this observes `10.20.0.1` -- the machine already using it -- at the
    head of the list.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "obsblock")
    await upsert_observation_row(
        admin_engine, org.org_id, vrf_id=vrf_id, address="10.20.0.1"
    )
    assert await _next_free(admin_engine, org.org_id, subnet_id, 1) == ["10.20.0.2"]


async def test_an_aged_observation_does_not_block(admin_engine: AsyncEngine) -> None:
    """THE CURRENCY WINDOW IS LOAD-BEARING, and a fixture of only fresh
    observations cannot see it.

    IF `last_seen_at >= cutoff` WERE DROPPED from the anti-join this observes
    `10.20.0.2` -- a machine that has been gone for three months would hold its
    address forever, and the subnet would silently fill up.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "obsaged")
    await upsert_observation_row(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        address="10.20.0.1",
        now=datetime.now(UTC) - timedelta(days=90),
    )
    assert await _next_free(admin_engine, org.org_id, subnet_id, 1) == ["10.20.0.1"]


async def test_next_free_counts_are_bounded(admin_engine: AsyncEngine) -> None:
    org, _, subnet_id = await _fixture(admin_engine, "cnt")
    async with AsyncSession(admin_engine) as session:
        for bad in (0, -1, 10_000):
            with pytest.raises(IpamValidationError):
                await next_free(session, org.org_id, subnet_id, bad)


async def test_next_free_returns_what_is_left_when_fewer_than_asked(
    admin_engine: AsyncEngine,
) -> None:
    org, _, subnet_id = await _fixture(admin_engine, "short", prefix="10.23.0.0/30")
    assert await _next_free(admin_engine, org.org_id, subnet_id, 5) == [
        "10.23.0.1",
        "10.23.0.2",
    ]


# ---------------------------------------------------------------------------
# Utilisation
# ---------------------------------------------------------------------------


async def test_utilisation_agrees_with_next_free(admin_engine: AsyncEngine) -> None:
    """`free` is counted with the SAME anti-join `next_free` selects from, so
    the two can never disagree. IF UTILISATION HAD ITS OWN PREDICATE SET, a
    subnet could report 100% used and still hand out an address."""
    org, vrf_id, subnet_id = await _fixture(admin_engine, "agree", prefix="10.24.0.0/29")
    await _add_address(admin_engine, org, vrf_id, subnet_id, "10.24.0.1", "assigned")
    await upsert_observation_row(
        admin_engine, org.org_id, vrf_id=vrf_id, address="10.24.0.2"
    )
    offered = await _next_free(admin_engine, org.org_id, subnet_id, 99)
    async with AsyncSession(admin_engine) as session:
        report = await utilization(session, org.org_id, subnet_id)
    assert report.capacity == 6
    assert report.free == len(offered) == 4
    assert report.used == 2
    assert report.percentage == pytest.approx(33.33, abs=0.01)
    assert (report.assigned, report.reserved, report.observed) == (1, 0, 1)


async def test_utilisation_counts_a_dhcp_pool_as_used(admin_engine: AsyncEngine) -> None:
    """A pool is 101 addresses that are not available, expressed WITHOUT 101
    rows (§0.4). IF RANGES WERE LEFT OUT OF `used` a subnet whose whole host
    range is a DHCP pool reports 0% used."""
    org, _, subnet_id = await _fixture(admin_engine, "pool")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_range(
            session,
            org.org_id,
            org.user_id,
            subnet_id,
            kind="dhcp_pool",
            start_address="10.20.0.100",
            end_address="10.20.0.200",
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        report = await utilization(session, org.org_id, subnet_id)
    assert report.used == 101
    assert report.free == 254 - 101


async def test_a_container_subnet_has_no_utilisation_next_free_or_allocation(
    admin_engine: AsyncEngine,
) -> None:
    """§0.2: a `container` is a pure-hierarchy prefix that holds no addresses.
    Without this the first thing an operator does is ask for the next free
    address in their /8."""
    org, _, subnet_id = await _fixture(
        admin_engine, "cont", prefix="10.25.0.0/24", kind="container"
    )
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError, match="container"):
            await next_free(session, org.org_id, subnet_id, 1)
        with pytest.raises(IpamValidationError, match="container"):
            await utilization(session, org.org_id, subnet_id)
        with pytest.raises(IpamValidationError, match="container"):
            await allocate(session, org.org_id, subnet_id, actor_id=org.user_id,
                           device_id=org.device_id)


# ---------------------------------------------------------------------------
# allocate -- the non-racing path (§3.3)
# ---------------------------------------------------------------------------


async def test_allocate_takes_the_first_free_address_and_audits(
    admin_engine: AsyncEngine,
) -> None:
    org, vrf_id, subnet_id = await _fixture(admin_engine, "alloc")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await allocate(
            session, org.org_id, subnet_id, actor_id=org.user_id, device_id=org.device_id
        )
        await session.commit()
        taken = str(row.address)
        assert taken == "10.20.0.1"
        assert row.state == "assigned"
        assert row.device_id == org.device_id
        assert row.assigned_by == org.user_id
        assert row.assigned_at is not None
        assert row.origin == "manual"
        assert row.allocation_method == "static"
    # ... and it is no longer offered.
    assert await _next_free(admin_engine, org.org_id, subnet_id, 1) == ["10.20.0.2"]
    async with AsyncSession(admin_engine) as session:
        actions = (
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.org_id == org.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert "ipam.address_assigned" in actions


async def test_allocate_requires_exactly_one_claimant(admin_engine: AsyncEngine) -> None:
    """§2.6's CHECK says the same thing; this is the difference between a 400
    naming the rule and a 500 naming a constraint. BOTH and NEITHER are tested:
    a service that only rejected "neither" would let "both" through to the
    database."""
    org, _, subnet_id = await _fixture(admin_engine, "claim")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError, match="claimant"):
            await allocate(session, org.org_id, subnet_id, actor_id=org.user_id)
        with pytest.raises(IpamValidationError, match="claimant"):
            await allocate(
                session,
                org.org_id,
                subnet_id,
                actor_id=org.user_id,
                device_id=org.device_id,
                asset_label="printer-3",
            )


async def test_allocate_refuses_a_device_from_another_org(admin_engine: AsyncEngine) -> None:
    org, _, subnet_id = await _fixture(admin_engine, "allocx")
    other = await seed_org(admin_engine, "allocy")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError, match="device"):
            await allocate(
                session, org.org_id, subnet_id, actor_id=org.user_id, device_id=other.device_id
            )


async def test_allocate_refuses_a_full_subnet(admin_engine: AsyncEngine) -> None:
    org, vrf_id, subnet_id = await _fixture(admin_engine, "full", prefix="10.26.0.0/31")
    await _add_address(admin_engine, org, vrf_id, subnet_id, "10.26.0.0", "assigned")
    await _add_address(admin_engine, org, vrf_id, subnet_id, "10.26.0.1", "reserved")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamConflictError, match="no free address"):
            await allocate(
                session, org.org_id, subnet_id, actor_id=org.user_id, device_id=org.device_id
            )


async def test_two_concurrent_allocations_take_different_addresses(
    admin_engine: AsyncEngine,
) -> None:
    """§3.3's non-racing path, and it needs two REAL connections and a barrier.

    THE WARM-UP QUERY BEFORE THE BARRIER IS NOT DECORATION. `AsyncSession` opens
    lazily, so without it both tasks arrive at the barrier holding no
    connection, then serialise on acquiring one from the pool -- and the test
    passes against an implementation with no lock at all. That is the exact
    defect that shipped in this module's previous concurrency test.

    IF THE ADVISORY LOCK WERE REMOVED, both transactions read a snapshot in
    which the other's row does not exist, both pick `10.20.0.1`, and this
    observes two allocations of the SAME address (or one `IntegrityError`
    against `uq_ipam_address_org_vrf_addr`) instead of `.1` and `.2`.
    """
    org, _, subnet_id = await _fixture(admin_engine, "race")
    barrier = asyncio.Barrier(2)

    async def _take() -> str:
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            # Warm-up: force a real connection to be checked out BEFORE the
            # barrier, so both tasks are genuinely inside a transaction when
            # they race.
            await session.execute(select(IpamAddress.id).limit(1))
            await barrier.wait()
            try:
                row = await allocate(
                    session,
                    org.org_id,
                    subnet_id,
                    actor_id=org.user_id,
                    device_id=org.device_id,
                )
                address = str(row.address)
                await session.commit()
                return address
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                await session.rollback()
                return f"failed: {type(exc).__name__}: {exc}"

    outcomes = await asyncio.gather(_take(), _take())
    assert sorted(outcomes) == ["10.20.0.1", "10.20.0.2"], outcomes


async def test_allocate_skips_a_currently_observed_address(
    admin_engine: AsyncEngine,
) -> None:
    """The allocator inherits next-free's reconciliation clause. IF ALLOCATE HAD
    ITS OWN PICKER this observes an assignment written onto an address that is
    already answering."""
    org, vrf_id, subnet_id = await _fixture(admin_engine, "allocobs")
    await upsert_observation_row(
        admin_engine, org.org_id, vrf_id=vrf_id, address="10.20.0.1"
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await allocate(
            session, org.org_id, subnet_id, actor_id=org.user_id, device_id=org.device_id
        )
        await session.commit()
    assert str(row.address) == "10.20.0.2"


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


async def test_a_subnet_from_another_org_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE. Through `org_session` RLS rejects first and
    this passes with the service's own `org_id` predicate deleted."""
    org_a = await seed_org(admin_engine, "tena")
    org_b = await seed_org(admin_engine, "tenb")
    vrf_b = await seed_vrf(admin_engine, org_b.org_id)
    subnet_b = await seed_subnet(
        admin_engine, org_b.org_id, vrf_id=vrf_b, prefix="10.90.0.0/24"
    )
    async with AsyncSession(admin_engine) as session:
        for call in (
            next_free(session, org_a.org_id, subnet_b, 1),
            utilization(session, org_a.org_id, subnet_b),
            allocate(session, org_a.org_id, subnet_b, actor_id=org_a.user_id,
                     device_id=org_a.device_id),
        ):
            with pytest.raises(IpamNotFoundError):
                await call


async def test_another_orgs_rows_do_not_block_this_orgs_allocation(
    admin_engine: AsyncEngine,
) -> None:
    """The anti-join's own `org_id` predicates, pinned on the ADMIN engine.

    Both orgs hold the same prefix and org B has `10.20.0.1` assigned and
    `10.20.0.2` observed. IF EITHER `NOT EXISTS` SUBQUERY LOST ITS `org_id`
    PREDICATE this observes `10.20.0.3` for org A -- one tenant's address plan
    silently consuming another's.
    """
    org_a = await seed_org(admin_engine, "antia")
    org_b = await seed_org(admin_engine, "antib")
    vrf_a = await seed_vrf(admin_engine, org_a.org_id)
    vrf_b = await seed_vrf(admin_engine, org_b.org_id)
    subnet_a = await seed_subnet(
        admin_engine, org_a.org_id, vrf_id=vrf_a, prefix="10.20.0.0/24"
    )
    subnet_b = await seed_subnet(
        admin_engine, org_b.org_id, vrf_id=vrf_b, prefix="10.20.0.0/24"
    )
    await _add_address(admin_engine, org_b, vrf_b, subnet_b, "10.20.0.1", "assigned")
    await upsert_observation_row(
        admin_engine, org_b.org_id, vrf_id=vrf_b, address="10.20.0.2"
    )
    assert await _next_free(admin_engine, org_a.org_id, subnet_a, 2) == [
        "10.20.0.1",
        "10.20.0.2",
    ]
    assert await _next_free(admin_engine, org_b.org_id, subnet_b, 1) == ["10.20.0.3"]


# ---------------------------------------------------------------------------
# §3.1 row 2 -- the grid's `conflicting` IS §4.3's conflict predicate
#
# There is one implementation of it and both callers read it. These two tests
# assert the AGREEMENT rather than either answer on its own: a predicate that
# exists twice can be right in one place and wrong in the other, and that is the
# defect -- the drift page and the address grid answering differently about the
# same address on the same afternoon.
# ---------------------------------------------------------------------------


async def _cross_vrf_org(admin_engine: AsyncEngine, tag: str, *, shared: bool):
    """One org, two VRFs, `10.0.0.0/24` in each, `10.0.0.5` claimed in both.

    `shared` is the ONE value that differs: whether the two subnets sit on the
    same `ipam_vlan` row -- the same wire -- or on two unrelated ones. Both are
    swept (Gate H), and there are no observations, no agents and no live
    devices, so the cross-VRF disjunct is the only thing that can make either
    view say `conflicting`.
    """
    org = await seed_org(admin_engine, tag)
    vlan = await seed_vlan(admin_engine, org.org_id, vid=10)
    other_vlan = vlan if shared else await seed_vlan(admin_engine, org.org_id, vid=20)
    subnets = []
    for name, vlan_id in (("global", vlan), ("customer-a", other_vlan)):
        vrf_id = await seed_vrf(admin_engine, org.org_id, name=name)
        subnet_id = await seed_subnet(
            admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.0.0.0/24",
            vlan_id=vlan_id,
        )
        await seed_address(
            admin_engine, org.org_id, vrf_id=vrf_id, subnet_id=subnet_id,
            address="10.0.0.5", state="assigned", asset_label="printer",
        )
        await seed_sweep_run(
            admin_engine, org.org_id, subnet_id=subnet_id, state="succeeded",
            started_at=datetime.now(UTC) - timedelta(days=90),
        )
        subnets.append(subnet_id)
    return org, subnets


async def _grid_status(engine: AsyncEngine, org_id: UUID, subnet_id: UUID, address: str) -> str:
    from app.modules.ipam.allocation import address_grid

    async with AsyncSession(engine, expire_on_commit=False) as session:
        entries = await address_grid(session, org_id, subnet_id, limit=16)
    return next(entry.status for entry in entries if entry.address == address)


async def _drift_classes(engine: AsyncEngine, org_id: UUID, address: str) -> set[str]:
    from app.models import IpamDrift
    from app.modules.ipam.reconcile import evaluate_org

    async with AsyncSession(engine, expire_on_commit=False) as session:
        await evaluate_org(session, org_id)
        await session.commit()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(IpamDrift).where(
                        IpamDrift.org_id == org_id, IpamDrift.address == address
                    )
                )
            )
            .scalars()
            .all()
        )
    return {row.drift_class for row in rows}


async def test_the_grid_and_the_drift_page_agree_that_a_cross_vrf_claim_conflicts(
    admin_engine: AsyncEngine,
) -> None:
    """Both views see the same duplicate address on one swept VLAN.

    *If the grid computed its own predicate* -- `len(macs) > 1 or
    claimant_mismatch`, which is what it did -- this observes `conflict` on the
    drift page and `dormant` in the grid for `10.0.0.5`, in the same org, in the
    same second.
    """
    org, subnets = await _cross_vrf_org(admin_engine, "gridxv", shared=True)

    assert "conflict" in await _drift_classes(admin_engine, org.org_id, "10.0.0.5")
    for subnet_id in subnets:
        assert await _grid_status(admin_engine, org.org_id, subnet_id, "10.0.0.5") == (
            "conflicting"
        )


async def test_the_grid_and_the_drift_page_agree_that_unrelated_vrfs_do_not(
    admin_engine: AsyncEngine,
) -> None:
    """The negative twin, differing in ONE value: two different VLANs.

    Without it, a grid that reported `conflicting` for every address claimed in
    two VRFs would pass the test above while restoring the false positive §4.3's
    second half exists to prevent.
    """
    org, subnets = await _cross_vrf_org(admin_engine, "gridnv", shared=False)

    assert "conflict" not in await _drift_classes(admin_engine, org.org_id, "10.0.0.5")
    for subnet_id in subnets:
        # Gate H holds and Gate E does not, so §3.1 row 6 -- not row 2.
        assert await _grid_status(admin_engine, org.org_id, subnet_id, "10.0.0.5") == (
            "dormant"
        )
