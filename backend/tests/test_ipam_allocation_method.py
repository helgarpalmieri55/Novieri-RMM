"""§3.2's `allocation_method`, from the NIC to the grid
(`docs/modules/ipam/spec.md` §3.2, §4.1 point 3, §14.1, §0.6).

**THE DEFECT THIS FILE CLOSES.** The agent has supplied `origin` on every address
it reports since §14.1 landed. `nic.py` read the address out of each entry and
dropped the rest, `ipam_observation` had no column for it, and §3.2's
`allocation_method` was therefore `unknown` for everything inventory-derived --
not because the fact was unavailable, but because nothing on this side retained
it.

**THE PARAMETER THIS FILE EXISTS TO VARY.** `allocation_method` was CONSTANT
(`unknown`) for every inventory-derived address in the entire suite, which means
every pre-existing test that mentions it proves nothing about this change: a
constant parameter is an untested one. Every test below varies it -- across
origins, across sources, across which of the operator and the fleet spoke, and
across time.

**WHY THE ANSWER IS DERIVED AND NOT STORED ON `ipam_address`.** §0.6 is this
module's founding decision: `state`, the claimant and `allocation_method` on
`ipam_address` are what a HUMAN asserted, and no ingestion path may write them,
because a device coming online that overwrites what an operator documented is
what "the IPAM changed my reservation by itself" means. So the fleet's report
lands in `ipam_observation.allocation_origin` and the two are combined at READ
TIME, exactly as `status` is combined from `state` and observations, with
`allocation_method_source` naming which one answered. `test_an_operator_
assertion_is_not_overwritten_by_the_fleet` is that decision as an assertion.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Device, HardwareComponent, IpamObservation
from app.modules.ipam.allocation import (
    ALLOCATION_METHOD_BY_ORIGIN,
    ALLOCATION_METHOD_SOURCES,
    ALLOCATION_METHODS,
    address_grid,
    derive_allocation_method,
)
from app.modules.ipam.errors import IpamValidationError
from app.modules.ipam.nic import ORIGINS, UNKNOWN_ORIGIN
from app.modules.ipam.observations import record_inventory_observations, upsert_observation
from tests.ipam_fixtures import seed_address, seed_org, seed_subnet, seed_vrf

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=1)


# --------------------------------------------------------------------------- #
# 1. The map itself.
# --------------------------------------------------------------------------- #


def test_the_origin_map_is_total_over_the_parsers_vocabulary() -> None:
    """Every origin `nic.py` can produce has an entry here, and no entry here is
    an origin it cannot.

    A MISSING ENTRY WOULD NOT RAISE -- `derive_allocation_method` uses `.get(...,
    "unknown")` so an unclassified origin reads as "nobody said". That is the
    right behaviour at runtime and the wrong thing to discover by accident: a
    sixth origin added to `ORIGINS` and forgotten here would silently make every
    address carrying it read as unknown, which is exactly the bug this whole
    finding is. Equality, not a subset, in both directions.
    """
    assert set(ALLOCATION_METHOD_BY_ORIGIN) == set(ORIGINS)


def test_every_mapped_method_is_a_legal_allocation_method() -> None:
    """The map's VALUES live in `ipam_address.allocation_method`'s CHECK
    vocabulary. A value outside it would be a derived answer the same column can
    never hold when an operator types it, and the grid would show a method no
    edit dialog offers."""
    assert set(ALLOCATION_METHOD_BY_ORIGIN.values()) <= set(ALLOCATION_METHODS)


def test_link_local_is_not_an_allocation_method() -> None:
    """§3.2 says it in those words. An address a NIC gave itself because DHCP
    failed was not allocated by anybody.

    Pinned on its own because it is the ONE entry that is not the identity, and
    an implementation that wrote `dict(zip(ORIGINS, ORIGINS))` would satisfy both
    tests above and fail only here.
    """
    assert ALLOCATION_METHOD_BY_ORIGIN["link_local"] == "unknown"
    assert "link_local" not in ALLOCATION_METHODS


def test_discovered_is_never_produced_from_an_observation() -> None:
    """`discovered` is a legal `allocation_method` and is what an IMPORT may
    carry (§8.1) -- an operator saying "this came off a scan of the old estate".
    Inventory evidence is not that, and mapping to it would make an operator's
    import and a device's self-report indistinguishable in the grid."""
    assert "discovered" not in set(ALLOCATION_METHOD_BY_ORIGIN.values())
    assert "discovered" in ALLOCATION_METHODS


# --------------------------------------------------------------------------- #
# 2. `derive_allocation_method` -- pure, and every branch varied.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("documented", "observed", "expected"),
    [
        # Nobody said anything at all: no row, no evidence.
        (None, [], ("unknown", "unknown")),
        # The fleet spoke and the plan did not. THE CASE §3.2 IS ABOUT.
        (None, [(NOW, "dhcp")], ("dhcp", "observed")),
        (None, [(NOW, "static")], ("static", "observed")),
        (None, [(NOW, "slaac")], ("slaac", "observed")),
        # A stored `unknown` is the COLUMN DEFAULT, not an assertion -- a row
        # created by `create_address`, `reserve`, or an import with no mapping.
        # Treating it as one would leave the observed method unreachable for
        # exactly the rows this finding is about.
        ("unknown", [(NOW, "dhcp")], ("dhcp", "observed")),
        # The operator spoke. §0.6: the fleet does not get to rewrite the plan.
        ("static", [(NOW, "dhcp")], ("static", "documented")),
        ("dhcp", [(NOW, "static")], ("dhcp", "documented")),
        ("discovered", [(NOW, "dhcp")], ("discovered", "documented")),
        # The fleet spoke and said nothing useful.
        (None, [(NOW, "link_local")], ("unknown", "unknown")),
        (None, [(NOW, UNKNOWN_ORIGIN)], ("unknown", "unknown")),
        # Defence in depth: an origin no vocabulary contains. `nic.py` normalises
        # before this is reachable, but a sweep handler or an importer calling in
        # directly is not obliged to, and a KeyError here would be an exception on
        # a read path.
        (None, [(NOW, "dhcpv6")], ("unknown", "unknown")),
        # THE NEWEST USEFUL ANSWER WINS. A machine re-imaged from a lease onto a
        # fixed address reports `static` from then on.
        (None, [(EARLIER, "dhcp"), (NOW, "static")], ("static", "observed")),
        (None, [(NOW, "static"), (EARLIER, "dhcp")], ("static", "observed")),
        # ...and a NEWER useless one does not displace an older useful one. This
        # is the sweep: it runs weekly, it always reports `unknown`, and it must
        # not erase what inventory established.
        (None, [(EARLIER, "dhcp"), (NOW, UNKNOWN_ORIGIN)], ("dhcp", "observed")),
    ],
)
def test_derive_allocation_method(
    documented: str | None,
    observed: list[tuple[datetime, str]],
    expected: tuple[str, str],
) -> None:
    """§3.2's answer, as a table. ASSERTS THE PAIR, never the method alone: an
    implementation that returned the right method with `source='documented'`
    everywhere would pass on every row if only the method were checked, and
    `source` is the field that keeps §0.6's distinction visible."""
    result = derive_allocation_method(documented=documented, observed=observed)
    assert (result.method, result.source) == expected
    assert result.source in ALLOCATION_METHOD_SOURCES


def test_the_caller_need_not_sort_the_observations() -> None:
    """Stated in the signature's docstring and asserted here, because an ordering
    contract that lives only in prose is one the second caller gets wrong. The
    same three sightings in six orders give one answer."""
    rows = [(EARLIER, "dhcp"), (NOW, "static"), (NOW - timedelta(hours=1), "slaac")]
    answers = set()
    for permutation in (
        rows,
        list(reversed(rows)),
        [rows[1], rows[0], rows[2]],
        [rows[2], rows[1], rows[0]],
    ):
        answer = derive_allocation_method(documented=None, observed=permutation)
        answers.add((answer.method, answer.source))
    assert answers == {("static", "observed")}


# --------------------------------------------------------------------------- #
# 3. Storage: the origin survives the trip from the NIC block to the table.
# --------------------------------------------------------------------------- #


def _nic_component(org_id: UUID, device_id: UUID, *entries: tuple[str, str]) -> HardwareComponent:
    return HardwareComponent(
        org_id=org_id,
        device_id=device_id,
        type="nic",
        identifier="a4:bb:6d:11:22:33",
        attributes={
            "name": "eth0",
            "addresses": [
                {
                    "address": address,
                    "family": "ipv6" if ":" in address.split("/")[0] else "ipv4",
                    "origin": origin,
                }
                for address, origin in entries
            ],
        },
    )


async def _observations(engine: AsyncEngine, org_id: UUID) -> dict[str, str]:
    """`{address: allocation_origin}` ON THE ADMIN ENGINE."""
    async with AsyncSession(engine) as session:
        rows = (
            (
                await session.execute(
                    select(IpamObservation).where(IpamObservation.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        return {str(row.address): row.allocation_origin for row in rows}


async def test_each_address_keeps_its_own_origin(admin_engine: AsyncEngine) -> None:
    """ONE NIC, THREE ADDRESSES, THREE DIFFERENT ORIGINS -- the ordinary shape of
    a dual-stack interface holding a v4 lease beside a SLAAC v6 address and a
    static alias.

    IF THE ORIGIN WERE READ PER COMPONENT rather than per entry, all three land
    with whichever came first and this prints one origin three times. That is a
    mutation a single-address fixture cannot see.
    """
    org = await seed_org(admin_engine, "alloc-per-entry")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            _nic_component(
                org.org_id,
                org.device_id,
                ("10.4.7.9/24", "dhcp"),
                ("10.4.7.10/24", "static"),
                ("2001:db8::5/64", "slaac"),
            )
        )
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await session.get(Device, org.device_id)
        assert device is not None
        assert await record_inventory_observations(session, device) == 3
        await session.commit()

    assert await _observations(admin_engine, org.org_id) == {
        "10.4.7.9": "dhcp",
        "10.4.7.10": "static",
        "2001:db8::5": "slaac",
    }


@pytest.mark.parametrize(
    ("entry_origin", "stored"),
    [
        ("dhcp", "dhcp"),
        ("static", "static"),
        ("slaac", "slaac"),
        ("link_local", "link_local"),
        ("unknown", "unknown"),
        # NOT REFUSED, DEGRADED. Dropping the address would be tightening inbound
        # (`docs/kernel/wire-protocol/README.md`), and it would do it SILENTLY,
        # because the parser is total -- the day someone ships `dhcpv6`, every
        # address carrying it would vanish from the passive stream with nothing
        # going red. The address count is asserted below for exactly this reason.
        ("dhcpv6", "unknown"),
        ("DHCP", "unknown"),
        ("", "unknown"),
    ],
)
async def test_an_origin_costs_the_address_nothing_however_odd_it_is(
    admin_engine: AsyncEngine, entry_origin: str, stored: str
) -> None:
    """The observation is recorded in EVERY case; only the origin degrades.

    ASSERTS THE ADDRESS AND THE ORIGIN TOGETHER. Asserting the origin alone
    cannot distinguish "degraded to unknown" from "no row at all", and the second
    is the failure mode that matters.
    """
    org = await seed_org(admin_engine, "alloc-odd")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(_nic_component(org.org_id, org.device_id, ("10.4.7.9/24", entry_origin)))
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await session.get(Device, org.device_id)
        assert device is not None
        await record_inventory_observations(session, device)
        await session.commit()

    assert await _observations(admin_engine, org.org_id) == {"10.4.7.9": stored}


async def test_a_sweep_observation_records_unknown_and_that_is_the_answer(
    admin_engine: AsyncEngine,
) -> None:
    """ARP and ICMP establish that an address ANSWERS, never how it was
    configured -- the same limit `confidence` records for the same evidence. A
    sweep that inferred a method would be inventing one.

    Written through `upsert_observation` with the argument OMITTED, so this also
    pins the default rather than a value a caller happened to pass.
    """
    org = await seed_org(admin_engine, "alloc-sweep")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await upsert_observation(
            session,
            org_id=org.org_id,
            vrf_id=vrf_id,
            address="10.4.7.9",
            mac="a4:bb:6d:11:22:33",
            hostname="",
            source="sweep",
            method="arp",
            device_id=None,
            confidence="medium",
        )
        await session.commit()
    assert await _observations(admin_engine, org.org_id) == {"10.4.7.9": UNKNOWN_ORIGIN}


async def test_upsert_observation_refuses_an_origin_outside_the_vocabulary(
    admin_engine: AsyncEngine,
) -> None:
    """The parser normalises; this function VALIDATES, and the two are for
    different callers. `nic.py` handles an untrusted agent, where an unrecognised
    value is noise to absorb. `upsert_observation`'s other callers -- the sweep
    result handler, the importer -- are OUR code, where an unrecognised value is
    a bug, and an `IpamValidationError` the caller can see beats both a silent
    coercion and the CHECK constraint's `IntegrityError`.

    ASSERTS THE TABLE IS STILL EMPTY, not merely that something was raised: "it
    refused" and "it refused after writing" are different facts.
    """
    org = await seed_org(admin_engine, "alloc-bad-origin")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamValidationError, match="allocation_origin"):
            await upsert_observation(
                session,
                org_id=org.org_id,
                vrf_id=vrf_id,
                address="10.4.7.9",
                mac=None,
                hostname="",
                source="sweep",
                method="icmp",
                device_id=None,
                confidence="low",
                allocation_origin="dhcpv6",
            )
        await session.rollback()
    assert await _observations(admin_engine, org.org_id) == {}


async def test_a_later_report_replaces_the_earlier_origin_on_the_same_row(
    admin_engine: AsyncEngine,
) -> None:
    """The upsert key does not carry the origin, so a machine that moves from a
    DHCP lease onto a static address is the SAME sighting with a new answer.

    IF `allocation_origin` WERE OMITTED FROM THE CONFLICT `set_` this reports
    `dhcp` forever -- §3.2's field wrong in a way nothing ever corrects, and
    invisible because the row count and every other column are identical.
    """
    org = await seed_org(admin_engine, "alloc-refresh")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    for origin in ("dhcp", "static"):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            await upsert_observation(
                session,
                org_id=org.org_id,
                vrf_id=vrf_id,
                address="10.4.7.9",
                mac="a4:bb:6d:11:22:33",
                hostname="",
                source="inventory",
                method="nic",
                device_id=org.device_id,
                confidence="high",
                allocation_origin=origin,
            )
            await session.commit()

    async with AsyncSession(admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(IpamObservation).where(IpamObservation.org_id == org.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert [(r.allocation_origin, r.sighting_count) for r in rows] == [("static", 2)]


# --------------------------------------------------------------------------- #
# 4. The grid: the answer reaches a reader.
# --------------------------------------------------------------------------- #


async def _grid(engine: AsyncEngine, org_id: UUID, subnet_id: UUID) -> dict[str, Any]:
    async with AsyncSession(engine) as session:
        entries = await address_grid(session, org_id, subnet_id, limit=16)
        return {
            entry.address: (entry.allocation_method, entry.allocation_method_source)
            for entry in entries
        }


async def test_the_grid_reports_what_the_endpoint_said_when_nobody_documented_it(
    admin_engine: AsyncEngine,
) -> None:
    """END TO END, and the whole point of the finding: an address with NO
    `ipam_address` row at all -- §0.4's ordinary case -- whose device reported it
    as a DHCP lease reads `dhcp`/`observed` in the grid.

    Before this change it read `unknown` for every address in every estate.
    """
    org = await seed_org(admin_engine, "alloc-grid-obs")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.4.7.0/29")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await upsert_observation(
            session,
            org_id=org.org_id,
            vrf_id=vrf_id,
            address="10.4.7.1",
            mac="a4:bb:6d:11:22:33",
            hostname="ws-01",
            source="inventory",
            method="nic",
            device_id=org.device_id,
            confidence="high",
            allocation_origin="dhcp",
        )
        await session.commit()

    grid = await _grid(admin_engine, org.org_id, subnet_id)
    assert grid["10.4.7.1"] == ("dhcp", "observed")
    # AND THE NEIGHBOURS STAY UNKNOWN. Without this the test passes against an
    # implementation that stamped every row in the page with the first answer it
    # found -- and `allocation_method` being constant across the page is the
    # exact shape of the defect being fixed.
    assert grid["10.4.7.2"] == ("unknown", "unknown")


async def test_an_operator_assertion_is_not_overwritten_by_the_fleet(
    admin_engine: AsyncEngine,
) -> None:
    """§0.6, IN THIS COLUMN. Two addresses, IDENTICAL evidence -- both reported
    `dhcp` by the same device in the same pass -- differing ONLY in whether an
    operator documented a method.

    That is what makes this test discriminate. With one address it would pass
    against an implementation that always preferred the observation, or one that
    always preferred the column; with two that differ in exactly one input, only
    the correct precedence gives both answers. An ingestion path that could write
    `ipam_address.allocation_method` is what "the IPAM changed my reservation by
    itself" means, and it is why the fleet's report lives on the observation.
    """
    org = await seed_org(admin_engine, "alloc-grid-doc")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.4.7.0/29")
    await seed_address(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        subnet_id=subnet_id,
        address="10.4.7.1",
        state="assigned",
        device_id=org.device_id,
        allocation_method="static",
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for address in ("10.4.7.1", "10.4.7.2"):
            await upsert_observation(
                session,
                org_id=org.org_id,
                vrf_id=vrf_id,
                address=address,
                mac="a4:bb:6d:11:22:33",
                hostname="ws-01",
                source="inventory",
                method="nic",
                device_id=org.device_id,
                confidence="high",
                allocation_origin="dhcp",
            )
        await session.commit()

    grid = await _grid(admin_engine, org.org_id, subnet_id)
    assert (grid["10.4.7.1"], grid["10.4.7.2"]) == (
        ("static", "documented"),
        ("dhcp", "observed"),
    )


async def test_a_documented_unknown_still_yields_to_the_fleet(
    admin_engine: AsyncEngine,
) -> None:
    """The complement of the test above, and the one that decides whether this
    finding is actually closed for documented addresses.

    An `ipam_address` row EXISTS -- an operator reserved it, or an import created
    it -- and its `allocation_method` is the column's default `unknown`. Nobody
    asserted anything, so the endpoint's report is the only answer there is. An
    implementation that treated "there is a row" as "an operator spoke" would
    leave §3.2's field permanently `unknown` for precisely the addresses that are
    in the plan, which is most of them.
    """
    org = await seed_org(admin_engine, "alloc-grid-default")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.4.7.0/29")
    await seed_address(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        subnet_id=subnet_id,
        address="10.4.7.1",
        state="reserved",
        allocation_method="unknown",
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await upsert_observation(
            session,
            org_id=org.org_id,
            vrf_id=vrf_id,
            address="10.4.7.1",
            mac=None,
            hostname="",
            source="inventory",
            method="nic",
            device_id=org.device_id,
            confidence="high",
            allocation_origin="slaac",
        )
        await session.commit()

    assert (await _grid(admin_engine, org.org_id, subnet_id))["10.4.7.1"] == (
        "slaac",
        "observed",
    )
