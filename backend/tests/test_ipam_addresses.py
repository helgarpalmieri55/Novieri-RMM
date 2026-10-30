"""The assignment workflow and the audit-history projection
(`docs/modules/ipam/spec.md` §3.2, §0.6, §0.13, §11.3).

WHAT THESE FIXTURES DELIBERATELY VARY:

* **Every transition is tested from a DIFFERENT starting state**, and each
  refusal case is paired with the permitted case that differs from it in
  exactly one input. A refusal test with no permitted twin cannot tell "this
  transition is refused" from "every transition is refused".
* **The `no ingestion path writes state` test seeds a claimant that is NOT the
  reporting device.** With the same device on both sides, an implementation
  that re-pointed `device_id` at the reporter would write the value that is
  already there and the test would pass against the bug.
* **The history test seeds a SECOND address's events.** With one address in the
  table, "filters to this address" and "returns everything" are the same list.
* **The MAC search seeds the same MAC in TWO subnets**, because the query it
  exists to answer -- "where did this printer go" -- is precisely the one a
  subnet-scoped implementation gets wrong while passing a single-subnet test.
* **Every `org_id`-predicate test runs on the ADMIN engine**, where RLS is not
  the thing rejecting.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AuditEvent, Device, IpamAddress, IpamObservation
from app.modules.ipam.addresses import (
    address_history,
    assign,
    create_address,
    deprecate,
    get_address,
    release,
    reserve,
    search,
    update_address,
)
from app.modules.ipam.errors import (
    IpamConflictError,
    IpamNotFoundError,
    IpamValidationError,
)
from app.modules.ipam.observations import record_inventory_observations, upsert_observation
from app.modules.ipam.subnets import create_range
from tests.ipam_fixtures import (
    seed_device,
    seed_org,
    seed_subnet,
    seed_vrf,
)


async def _fixture(engine: AsyncEngine, tag: str, prefix: str = "10.60.0.0/24", **kw):
    org = await seed_org(engine, tag)
    vrf_id = await seed_vrf(engine, org.org_id)
    subnet_id = await seed_subnet(engine, org.org_id, vrf_id=vrf_id, prefix=prefix, **kw)
    return org, vrf_id, subnet_id


async def _state_of(engine: AsyncEngine, org_id: UUID, address: str) -> tuple:
    async with AsyncSession(engine) as session:
        row = (
            await session.execute(
                select(IpamAddress).where(
                    IpamAddress.org_id == org_id, IpamAddress.address == address
                )
            )
        ).scalar_one()
        return (row.state, row.device_id, row.asset_label, row.mac, row.released_at)


async def _actions(engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# §3.2's transition table -- each row, positive and negative
# ---------------------------------------------------------------------------


async def test_absent_to_reserved(admin_engine: AsyncEngine) -> None:
    org, _, _ = await _fixture(admin_engine, "res")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await reserve(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            description="core switch",
        )
        await session.commit()
    assert row.state == "reserved"
    assert await _state_of(admin_engine, org.org_id, "10.60.0.5") == (
        "reserved",
        None,
        "",
        None,
        None,
    )
    assert "ipam.address_reserved" in await _actions(admin_engine, org.org_id)


async def test_reserving_an_address_outside_every_subnet_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """§3.2's first predicate. The PERMITTED twin is the test above, which
    differs only in the address -- so this cannot pass by refusing everything."""
    org, _, _ = await _fixture(admin_engine, "resout")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError, match="no registered subnet"):
            await reserve(
                session, org.org_id, org.user_id, client_id=org.org_id, address="192.0.2.5"
            )


async def test_reserving_inside_a_container_subnet_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    org, _, _ = await _fixture(admin_engine, "rescont", kind="container")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError, match="container"):
            await reserve(
                session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.5"
            )


async def test_the_network_and_broadcast_addresses_are_refused(
    admin_engine: AsyncEngine,
) -> None:
    """IF THE ENDPOINT PREDICATE WERE DELETED this reserves `10.60.0.0` and
    `10.60.0.255` -- and provisioning hands a machine the broadcast address as
    its static config. `.5` in the same fixture succeeds, so the refusal is not
    blanket."""
    org, _, _ = await _fixture(admin_engine, "edges")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for edge in ("10.60.0.0", "10.60.0.255"):
            with pytest.raises(IpamValidationError, match="network or broadcast"):
                await reserve(session, org.org_id, org.user_id, client_id=org.org_id, address=edge)
        await reserve(session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.5")
        await session.commit()


async def test_a_slash_31_has_no_forbidden_endpoints(admin_engine: AsyncEngine) -> None:
    """RFC 3021 again, and the reason `is_network_or_broadcast` is one function
    rather than a repeated inline comparison."""
    org, _, _ = await _fixture(admin_engine, "p2p", prefix="10.61.0.0/31")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for address in ("10.61.0.0", "10.61.0.1"):
            await reserve(session, org.org_id, org.user_id, client_id=org.org_id, address=address)
        await session.commit()


async def test_absent_to_assigned_with_a_device(admin_engine: AsyncEngine) -> None:
    org, _, _ = await _fixture(admin_engine, "asg")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
            hostname="wks-042",
        )
        await session.commit()
    assert row.state == "assigned"
    assert row.assigned_by == org.user_id
    assert row.allocation_method == "static"
    assert "ipam.address_assigned" in await _actions(admin_engine, org.org_id)


async def test_free_to_assigned(admin_engine: AsyncEngine) -> None:
    org, _, _ = await _fixture(admin_engine, "f2a")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_address(
            session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.5"
        )
        await session.commit()
    assert (await _state_of(admin_engine, org.org_id, "10.60.0.5"))[0] == "free"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            asset_label="printer-3",
        )
        await session.commit()
    state, device_id, label, _, _ = await _state_of(admin_engine, org.org_id, "10.60.0.5")
    assert (state, device_id, label) == ("assigned", None, "printer-3")


async def test_reserved_to_assigned(admin_engine: AsyncEngine) -> None:
    org, _, _ = await _fixture(admin_engine, "r2a")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await reserve(session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.5")
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await session.commit()
    assert (await _state_of(admin_engine, org.org_id, "10.60.0.5"))[0] == "assigned"


async def test_assigned_to_assigned_is_refused(admin_engine: AsyncEngine) -> None:
    """Not in §3.2's table. IF IT WERE PERMITTED, re-assigning would silently
    re-point a claimant with no release in the audit log -- the history would
    show a machine's address moving with nothing recording that the previous
    holder lost it."""
    org, _, _ = await _fixture(admin_engine, "a2a")
    other = await seed_device(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamConflictError, match="already assigned"):
            await assign(
                session,
                org.org_id,
                org.user_id,
                client_id=org.org_id,
                address="10.60.0.5",
                device_id=other,
            )
    # ... and the original claimant is untouched.
    assert (await _state_of(admin_engine, org.org_id, "10.60.0.5"))[1] == org.device_id


async def test_assigned_to_free_clears_the_claimant_and_keeps_the_row(
    admin_engine: AsyncEngine,
) -> None:
    """§0.4: sparse means "no row until first mentioned", NOT "rows evaporate".

    IF RELEASE DELETED THE ROW this observes no row at all -- and the address's
    `first_seen` history, its description and the audit target it hangs on would
    go with it.
    """
    org, _, _ = await _fixture(admin_engine, "rel")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
            mac="a4:bb:6d:11:22:33",
        )
        await session.commit()
        address_id = row.id
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await release(session, org.org_id, org.user_id, address_id)
        await session.commit()
    state, device_id, label, mac, released_at = await _state_of(
        admin_engine, org.org_id, "10.60.0.5"
    )
    assert state == "free"
    assert device_id is None
    assert label == ""
    assert mac is None
    assert released_at is not None
    assert "ipam.address_released" in await _actions(admin_engine, org.org_id)


async def test_release_records_the_previous_claimant(admin_engine: AsyncEngine) -> None:
    """§11.3: `ipam.address_released` carries the PREVIOUS claimant. Recorded
    after the clear, it would carry `None` -- and the audit log would say an
    address was released from nobody."""
    org, _, _ = await _fixture(admin_engine, "relaudit")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await release(session, org.org_id, org.user_id, row.id)
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "ipam.address_released",
                )
            )
        ).scalar_one()
    assert event.meta["previous_claimant"] == str(org.device_id)
    assert event.meta["from_state"] == "assigned"


async def test_assigned_to_reserved_clears_the_claimant(admin_engine: AsyncEngine) -> None:
    org, _, _ = await _fixture(admin_engine, "a2r")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await reserve(session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.5")
        await session.commit()
    assert await _state_of(admin_engine, org.org_id, "10.60.0.5") == (
        "reserved",
        None,
        "",
        None,
        None,
    )


async def test_reserved_to_free(admin_engine: AsyncEngine) -> None:
    org, _, _ = await _fixture(admin_engine, "r2f")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await reserve(
            session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.5"
        )
        await release(session, org.org_id, org.user_id, row.id)
        await session.commit()
    assert (await _state_of(admin_engine, org.org_id, "10.60.0.5"))[0] == "free"


async def test_any_to_deprecated(admin_engine: AsyncEngine) -> None:
    """"We know this is gone, stop telling us" -- excluded from next-free and
    exempt from absence drift."""
    org, _, _ = await _fixture(admin_engine, "dep")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await deprecate(session, org.org_id, org.user_id, row.id)
        await session.commit()
    state, device_id, _, _, _ = await _state_of(admin_engine, org.org_id, "10.60.0.5")
    assert state == "deprecated"
    assert device_id is None
    assert "ipam.address_state_changed" in await _actions(admin_engine, org.org_id)


async def test_deprecated_cannot_be_assigned_directly(admin_engine: AsyncEngine) -> None:
    """§3.2 permits `assigned` only from absent, `free` or `reserved`. Releasing
    first is one deliberate step and one audit event; assigning straight out of
    `deprecated` would be neither."""
    org, _, _ = await _fixture(admin_engine, "dep2a")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await create_address(
            session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.5"
        )
        await deprecate(session, org.org_id, org.user_id, row.id)
        await session.commit()
        address_id = row.id
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="deprecated"):
            await assign(
                session,
                org.org_id,
                org.user_id,
                client_id=org.org_id,
                address="10.60.0.5",
                device_id=org.device_id,
            )
        # ... but releasing it first is the documented route, and it works.
        await release(session, org.org_id, org.user_id, address_id)
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await session.commit()
    assert (await _state_of(admin_engine, org.org_id, "10.60.0.5"))[0] == "assigned"


# ---------------------------------------------------------------------------
# The claimant rule and the DHCP-pool refusal
# ---------------------------------------------------------------------------


async def test_both_claimants_and_neither_claimant_are_refused_at_the_service(
    admin_engine: AsyncEngine,
) -> None:
    """§2.6's CHECK says the same thing. This is the difference between a 400
    naming the rule and a 500 naming a constraint -- and BOTH failure shapes are
    tested, because a service that only rejected "neither" would pass "both"
    through to the database."""
    org, _, _ = await _fixture(admin_engine, "claim")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError, match="claimant"):
            await assign(
                session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.5"
            )
        with pytest.raises(IpamValidationError, match="claimant"):
            await assign(
                session,
                org.org_id,
                org.user_id,
                client_id=org.org_id,
                address="10.60.0.5",
                device_id=org.device_id,
                asset_label="printer-3",
            )


async def test_a_device_from_another_org_is_refused(admin_engine: AsyncEngine) -> None:
    """The FK is org-blind and referential integrity runs as the referenced
    table's owner, so RLS does not close this either."""
    org, _, _ = await _fixture(admin_engine, "xorgdev")
    other = await seed_org(admin_engine, "xorgdev2")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError, match="device"):
            await assign(
                session,
                org.org_id,
                org.user_id,
                client_id=org.org_id,
                address="10.60.0.5",
                device_id=other.device_id,
            )


async def test_a_static_assignment_inside_a_dhcp_pool_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """§3.2's 409, and the reason it exists: modelling a DHCP reservation as a
    static row inside a dynamic pool creates a record that is TRUE IN THE IPAM
    AND FALSE ON THE DHCP SERVER.

    The paired permitted case is `.5`, one address outside the pool, so the
    refusal is provably about the pool and not about the subnet.
    """
    org, _, subnet_id = await _fixture(admin_engine, "pool")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_range(
            session,
            org.org_id,
            org.user_id,
            subnet_id,
            kind="dhcp_pool",
            start_address="10.60.0.100",
            end_address="10.60.0.200",
        )
        await session.commit()
    refusals: list[str] = []
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for attempt in (
            assign(
                session,
                org.org_id,
                org.user_id,
                client_id=org.org_id,
                address="10.60.0.150",
                device_id=org.device_id,
            ),
            reserve(
                session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.150"
            ),
        ):
            try:
                await attempt
            except IpamConflictError as exc:
                refusals.append(str(exc))
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await session.commit()

    # THE PRIMARY ASSERTION IS THE STORED ROW, not the exception. With the guard
    # deleted this observes an `assigned` row at `.150` -- a record that is true
    # in the IPAM and false on the DHCP server -- where "DID NOT RAISE" would
    # only have told us an exception was missing.
    async with AsyncSession(admin_engine) as session:
        stored = (
            (
                await session.execute(
                    select(IpamAddress.address, IpamAddress.state).where(
                        IpamAddress.org_id == org.org_id
                    )
                )
            )
            .all()
        )
    assert [(str(r.address), r.state) for r in stored] == [("10.60.0.5", "assigned")]
    assert len(refusals) == 2
    assert all("dhcp_pool" in refusal for refusal in refusals)


async def test_a_reserved_range_does_not_block_assignment(admin_engine: AsyncEngine) -> None:
    """Only `dhcp_pool` blocks. A `reserved` range is exactly the carve-out
    §3.2 tells the operator to make, so blocking it would make the guidance
    impossible to follow. IF THE RANGE CHECK MATCHED ANY KIND this observes a
    409 on the very address the error message recommends."""
    org, _, subnet_id = await _fixture(admin_engine, "resrange")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_range(
            session,
            org.org_id,
            org.user_id,
            subnet_id,
            kind="reserved",
            start_address="10.60.0.10",
            end_address="10.60.0.20",
        )
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.15",
            device_id=org.device_id,
        )
        await session.commit()
    assert (await _state_of(admin_engine, org.org_id, "10.60.0.15"))[0] == "assigned"


# ---------------------------------------------------------------------------
# §0.6's split -- THE test that stops "the IPAM changed my reservation by itself"
# ---------------------------------------------------------------------------


async def test_no_ingestion_path_can_change_state(admin_engine: AsyncEngine) -> None:
    """BOTH ingestion paths, against an address documented to a DIFFERENT device.

    (a) an inventory ingest reporting the address from `other_device`, and
    (b) a sweep-shaped observation at the same address with a different MAC.

    `state`, `device_id` and `assigned_by` must be byte-for-byte unchanged while
    `ipam_observation` rows appear. IF §0.6's SPLIT WERE DELETED this observes
    `state` flipped or `device_id` re-pointed at the reporting machine -- a
    different VALUE, not a different exception.

    THE CLAIMANT IS DELIBERATELY NOT THE REPORTING DEVICE. With the same device
    on both sides, an implementation that re-pointed `device_id` would write the
    value already there and this test would pass against the bug.
    """
    org, vrf_id, subnet_id = await _fixture(admin_engine, "split")
    reporter = await seed_device(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.42",
            device_id=org.device_id,
            mac="a4:bb:6d:11:22:33",
        )
        await session.commit()
    before = await _state_of(admin_engine, org.org_id, "10.60.0.42")

    # (a) inventory
    from app.models import HardwareComponent

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            HardwareComponent(
                org_id=org.org_id,
                device_id=reporter,
                type="nic",
                identifier="de:ad:be:ef:00:01",
                # §14.1's block: the only address key `nic.py` reads. Seeded as
                # `{"ip": ...}` this NIC would contribute NO observation, and
                # part (a) of this test -- inventory ingestion being unable to
                # change a documented address -- would be asserting that a code
                # path which never ran left the row alone.
                attributes={
                    "addresses": [
                        {"address": "10.60.0.42/24", "family": "ipv4", "origin": "dhcp"}
                    ]
                },
            )
        )
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = (
            await session.execute(select(Device).where(Device.id == reporter))
        ).scalar_one()
        await record_inventory_observations(session, device)
        await session.commit()

    # (b) sweep
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await upsert_observation(
            session,
            org_id=org.org_id,
            vrf_id=vrf_id,
            address="10.60.0.42",
            mac="de:ad:be:ef:00:02",
            hostname="",
            source="sweep",
            method="arp",
            device_id=None,
            confidence="medium",
        )
        await session.commit()

    assert await _state_of(admin_engine, org.org_id, "10.60.0.42") == before
    async with AsyncSession(admin_engine) as session:
        observations = (
            (
                await session.execute(
                    select(IpamObservation).where(IpamObservation.org_id == org.org_id)
                )
            )
            .scalars()
            .all()
        )
        row = (
            await session.execute(
                select(IpamAddress).where(
                    IpamAddress.org_id == org.org_id, IpamAddress.address == "10.60.0.42"
                )
            )
        ).scalar_one()
    assert len(observations) == 2
    assert row.assigned_by == org.user_id
    assert row.subnet_id == subnet_id


# ---------------------------------------------------------------------------
# §0.13 -- the narrow audit-history projection
# ---------------------------------------------------------------------------


async def test_address_history_returns_only_this_addresss_events(
    admin_engine: AsyncEngine,
) -> None:
    """A SECOND ADDRESS'S EVENTS ARE SEEDED. With one address in the table,
    "filters to this address" and "returns every ipam_address event in the org"
    are the same list, and the `target_id` predicate could be deleted with
    nothing going red."""
    org, _, _ = await _fixture(admin_engine, "hist")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        first = await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await session.commit()
        first_id = first.id
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        second = await reserve(
            session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.6"
        )
        await session.commit()
        second_id = second.id
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await release(session, org.org_id, org.user_id, first_id)
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        history = await address_history(session, org.org_id, first_id)
        other = await address_history(session, org.org_id, second_id)

    assert [entry.action for entry in history] == [
        "ipam.address_assigned",
        "ipam.address_released",
    ]
    assert [entry.action for entry in other] == ["ipam.address_reserved"]
    assert history[0].from_state is None
    assert history[0].to_state == "assigned"
    assert history[1].from_state == "assigned"
    assert history[1].to_state == "free"
    assert all(entry.actor_id == org.user_id for entry in history)
    assert all(entry.actor_type == "user" for entry in history)


async def test_address_history_is_org_scoped_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE. The audit table is org-scoped by RLS through
    `org_session`, so a cross-org read driven that way passes with the
    projection's own `org_id` predicate deleted."""
    org_a, _, _ = await _fixture(admin_engine, "hista")
    org_b, _, _ = await _fixture(admin_engine, "histb", prefix="10.62.0.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row_b = await assign(
            session,
            org_b.org_id,
            org_b.user_id,
            client_id=org_b.org_id,
            address="10.62.0.5",
            device_id=org_b.device_id,
        )
        await session.commit()
        b_id = row_b.id
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamNotFoundError):
            await address_history(session, org_a.org_id, b_id)


async def test_address_history_detail_names_only_this_org_and_this_address(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE, and it is `detail` that is being pinned.

    `detail` is `AuditEvent.meta` minus the two state columns, so widening the
    API response to carry it widens what a holder of `ipam.read` ALONE can read
    out of the audit log (§0.13). What must remain true is that every identifier
    inside it belongs to this org and to this address.

    **BOTH ORGS USE THE SAME PREFIX AND THE SAME ADDRESS.** With different
    prefixes, `10.60.x` vs `10.62.x` separates the two histories on their own
    and the `org_id` predicate could be deleted with nothing going red -- the
    literal `address` string in `detail` would even look correct. Here the two
    orgs' events are textually identical apart from their ids, so only the
    tenant predicate can tell them apart. Driven on the ADMIN engine, where RLS
    is not the thing rejecting.
    """
    org_a, vrf_a, subnet_a = await _fixture(admin_engine, "detaila")
    org_b, vrf_b, subnet_b = await _fixture(admin_engine, "detailb")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        a_id = (
            await assign(
                session,
                org_a.org_id,
                org_a.user_id,
                client_id=org_a.org_id,
                address="10.60.0.5",
                device_id=org_a.device_id,
            )
        ).id
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        # Same address text, same prefix, different org.
        await assign(
            session,
            org_b.org_id,
            org_b.user_id,
            client_id=org_b.org_id,
            address="10.60.0.5",
            device_id=org_b.device_id,
        )
        # ...and a SECOND address in org A, so "filtered to one address" is not
        # satisfied by there being only one.
        await assign(
            session,
            org_a.org_id,
            org_a.user_id,
            client_id=org_a.org_id,
            address="10.60.0.6",
            device_id=org_a.device_id,
            allocation_method="dhcp",
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        history = await address_history(session, org_a.org_id, a_id)

    assert [entry.action for entry in history] == ["ipam.address_assigned"]
    detail = history[0].detail
    # §11.3's payload for `ipam.address_assigned`: claimant and method.
    assert detail["claimant"] == str(org_a.device_id)
    assert detail["claimant_type"] == "device"
    assert detail["allocation_method"] == "static"
    assert detail["address"] == "10.60.0.5"
    assert detail["vrf_id"] == str(vrf_a)
    assert detail["subnet_id"] == str(subnet_a)
    # The projection's own columns are NOT duplicated into `detail`.
    assert "from_state" not in detail and "to_state" not in detail

    # Nothing belonging to the other tenant -- or to org A's other address --
    # appears anywhere in the payload, at any depth.
    rendered = repr(detail)
    for foreign in (
        str(org_b.org_id), str(org_b.device_id), str(org_b.user_id),
        str(vrf_b), str(subnet_b), "10.60.0.6",
    ):
        assert foreign not in rendered, f"{foreign} leaked into detail: {detail}"


async def test_address_history_of_an_unknown_id_is_not_found(
    admin_engine: AsyncEngine,
) -> None:
    org, _, _ = await _fixture(admin_engine, "histmiss")
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamNotFoundError):
            await address_history(session, org.org_id, uuid4())


# ---------------------------------------------------------------------------
# Search (§9)
# ---------------------------------------------------------------------------


async def test_search_by_mac_finds_every_address_across_subnets(
    admin_engine: AsyncEngine,
) -> None:
    """"WHERE DID THIS PRINTER GO." THE SAME MAC IS SEEDED IN TWO SUBNETS,
    because a subnet-scoped implementation answers the single-subnet version of
    this question perfectly and the real one not at all.

    IF THE SEARCH JOINED ONLY `ipam_address.mac` this observes one address --
    the documented one -- and misses the address the printer is actually
    answering at, which is the entire finding.
    """
    org = await seed_org(admin_engine, "mac")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.63.0.0/24")
    await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.64.0.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.63.0.5",
            asset_label="printer-3",
            mac="A4-BB-6D-11-22-33",
        )
        await upsert_observation(
            session,
            org_id=org.org_id,
            vrf_id=vrf_id,
            address="10.64.0.9",
            mac="a4bb.6d11.2233",
            hostname="",
            source="sweep",
            method="arp",
            device_id=None,
            confidence="medium",
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        result = await search(session, org.org_id, "a4:bb:6d:11:22:33")
    assert result.kind == "mac"
    assert sorted(result.addresses) == ["10.63.0.5", "10.64.0.9"]


async def test_search_by_ip_returns_the_subnet_the_record_and_the_observations(
    admin_engine: AsyncEngine,
) -> None:
    org, vrf_id, subnet_id = await _fixture(admin_engine, "sip")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await upsert_observation(
            session,
            org_id=org.org_id,
            vrf_id=vrf_id,
            address="10.60.0.5",
            mac=None,
            hostname="",
            source="sweep",
            method="icmp",
            device_id=None,
            confidence="low",
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        result = await search(session, org.org_id, "10.60.0.5")
    assert result.kind == "address"
    assert result.subnets == [subnet_id]
    assert result.addresses == ["10.60.0.5"]
    assert result.observations == 1


async def test_search_by_cidr_returns_everything_inside_it(
    admin_engine: AsyncEngine,
) -> None:
    """A CIDR search is a containment query, not a text prefix match: in `text`,
    `10.65.70.5` matches `LIKE '10.65.7%'` (§0.1)."""
    org = await seed_org(admin_engine, "scidr")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.65.7.0/24")
    await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.65.70.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await reserve(session, org.org_id, org.user_id, client_id=org.org_id, address="10.65.7.5")
        await reserve(session, org.org_id, org.user_id, client_id=org.org_id, address="10.65.70.5")
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        result = await search(session, org.org_id, "10.65.7.0/24")
    assert result.kind == "cidr"
    assert result.addresses == ["10.65.7.5"]


async def test_search_by_hostname_and_device_name(admin_engine: AsyncEngine) -> None:
    org, _, _ = await _fixture(admin_engine, "sname")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
            hostname="mail-relay-01",
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        by_hostname = await search(session, org.org_id, "mail-relay")
        device = (
            await session.execute(select(Device).where(Device.id == org.device_id))
        ).scalar_one()
        by_device = await search(session, org.org_id, device.hostname)
    assert by_hostname.kind == "text"
    assert by_hostname.addresses == ["10.60.0.5"]
    assert by_device.addresses == ["10.60.0.5"]


async def test_search_is_org_scoped_on_the_admin_engine(admin_engine: AsyncEngine) -> None:
    """PINNED ON THE ADMIN ENGINE. IF `search`'s `org_id` PREDICATE WERE
    DELETED this observes org B's address in org A's results -- one tenant's
    address plan readable from another's search box."""
    org_a, _, _ = await _fixture(admin_engine, "sxa")
    org_b, _, _ = await _fixture(admin_engine, "sxb", prefix="10.60.0.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org_b.org_id,
            org_b.user_id,
            client_id=org_b.org_id,
            address="10.60.0.5",
            device_id=org_b.device_id,
            mac="a4:bb:6d:11:22:33",
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert (await search(session, org_a.org_id, "10.60.0.5")).addresses == []
        assert (await search(session, org_a.org_id, "a4:bb:6d:11:22:33")).addresses == []
        assert (await search(session, org_b.org_id, "10.60.0.5")).addresses == ["10.60.0.5"]


# ---------------------------------------------------------------------------
# Descriptive edits and lookups
# ---------------------------------------------------------------------------


async def test_update_address_edits_description_but_never_state(
    admin_engine: AsyncEngine,
) -> None:
    """`state` is not an editable field. IF IT WERE, the transition table would
    have a bypass and "no ingestion path writes state" would be one careless
    `fields` dict away from being false for operators too."""
    org, _, _ = await _fixture(admin_engine, "upd")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await reserve(
            session, org.org_id, org.user_id, client_id=org.org_id, address="10.60.0.5"
        )
        await session.commit()
        address_id = row.id
    refusal = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await update_address(
            session, org.org_id, org.user_id, address_id, {"description": "core uplink"}
        )
        try:
            # `free` and not `assigned`, deliberately: `assigned` would trip
            # `ck_ipam_address_one_claimant` and the DATABASE would be what
            # refused, which is not the guard under test. `free` is a state the
            # CHECK happily accepts, so if the field were editable the row would
            # simply be released -- with no `ipam.address_released` event and no
            # `released_at` -- and that is the value this asserts on.
            await update_address(
                session, org.org_id, org.user_id, address_id, {"state": "free"}
            )
        except IpamValidationError as exc:
            refusal = str(exc)
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        row = await get_address(session, org.org_id, address_id)
    assert row.description == "core uplink"
    assert row.state == "reserved"
    assert row.released_at is None
    assert refusal is not None and "state" in refusal
    assert "ipam.address_released" not in await _actions(admin_engine, org.org_id)


async def test_an_address_id_from_another_org_is_not_found(
    admin_engine: AsyncEngine,
) -> None:
    org_a, _, _ = await _fixture(admin_engine, "getxa")
    org_b, _, _ = await _fixture(admin_engine, "getxb", prefix="10.66.0.0/24")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await reserve(
            session, org_b.org_id, org_b.user_id, client_id=org_b.org_id, address="10.66.0.5"
        )
        await session.commit()
        b_id = row.id
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamNotFoundError):
            await get_address(session, org_a.org_id, b_id)
        with pytest.raises(IpamNotFoundError):
            await release(session, org_a.org_id, org_a.user_id, b_id)


async def test_an_ipv6_address_is_documented_and_assigned(admin_engine: AsyncEngine) -> None:
    """§0.5: no ENUMERATION, not "no IPv6". The workflow is family-blind."""
    org, _, _ = await _fixture(admin_engine, "v6", prefix="2001:db8:7::/64")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="2001:db8:7::42",
            device_id=org.device_id,
        )
        await session.commit()
    assert row.state == "assigned"
    assert str(row.address) == "2001:db8:7::42"


async def test_an_unresolved_conflict_finding_blocks_assignment(
    admin_engine: AsyncEngine,
) -> None:
    """§3.2's last predicate. A RESOLVED finding on the same address does NOT
    block -- seeded alongside, because a check on "is there a drift row" rather
    than "is there an UNRESOLVED one" passes every single-row test."""
    from app.models import IpamDrift

    org, vrf_id, subnet_id = await _fixture(admin_engine, "conf")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            IpamDrift(
                org_id=org.org_id,
                vrf_id=vrf_id,
                subnet_id=subnet_id,
                address="10.60.0.9",
                drift_class="conflict",
                severity="high",
                confidence="high",
                resolved_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        session.add(
            IpamDrift(
                org_id=org.org_id,
                vrf_id=vrf_id,
                subnet_id=subnet_id,
                address="10.60.0.5",
                drift_class="conflict",
                severity="high",
                confidence="high",
            )
        )
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="conflict"):
            await assign(
                session,
                org.org_id,
                org.user_id,
                client_id=org.org_id,
                address="10.60.0.5",
                device_id=org.device_id,
            )
        # The resolved finding does not block.
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.9",
            device_id=org.device_id,
        )
        await session.commit()
    assert (await _state_of(admin_engine, org.org_id, "10.60.0.9"))[0] == "assigned"


async def test_an_acknowledged_conflict_no_longer_blocks_assignment(
    admin_engine: AsyncEngine,
) -> None:
    """An operator who has judged a finding must have a way to proceed.

    `acknowledge` is "I have seen this and it is fine": it requires `ipam.assign`
    at the API and is audited (§11.3). It writes `acknowledged_at` and nothing
    else -- in particular it does NOT write `resolved_at`, because §0.8's whole
    promise is that an acknowledgement survives every nightly recompute and a
    recompute would immediately re-open a row it had stamped resolved.

    *If the block read `resolved_at IS NULL` alone* -- which it did -- the only
    route past a conflict is to DELETE one of the rows that produced it and wait
    up to 24h for the next pass. That is the remedy a false positive should
    never require, and it is a worse record than an acknowledgement: the
    judgement disappears with the row.
    """
    from app.models import IpamDrift
    from app.modules.ipam.reconcile import acknowledge_drift

    org, vrf_id, subnet_id = await _fixture(admin_engine, "ackconf")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        finding = IpamDrift(
            org_id=org.org_id,
            vrf_id=vrf_id,
            subnet_id=subnet_id,
            address="10.60.0.5",
            drift_class="conflict",
            severity="high",
            confidence="high",
        )
        session.add(finding)
        await session.commit()
        drift_id = finding.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(IpamConflictError, match="conflict"):
            await assign(
                session,
                org.org_id,
                org.user_id,
                client_id=org.org_id,
                address="10.60.0.5",
                device_id=org.device_id,
            )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await acknowledge_drift(
            session, org.org_id, actor_id=org.user_id, drift_id=drift_id,
            note="two correct claims in two routing domains",
        )
        await session.commit()
        # The acknowledgement is what changed, and it did NOT resolve the row:
        # the finding is still open and still on the drift page.
        assert row.acknowledged_at is not None
        assert row.resolved_at is None

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await session.commit()
    assert (await _state_of(admin_engine, org.org_id, "10.60.0.5"))[0] == "assigned"
    assert "ipam.drift_acknowledged" in await _actions(admin_engine, org.org_id)


async def test_suppression_lifts_the_assignment_block_only_while_it_lasts(
    admin_engine: AsyncEngine,
) -> None:
    """Suppression is TIME-BOXED, and the block honours the box.

    Two findings on two addresses, differing in exactly one value: one is
    suppressed until tomorrow, the other's suppression expired yesterday. An
    implementation that read `suppressed_until IS NOT NULL` -- ignoring the
    timestamp -- would let both through, which is how a finding silenced once
    becomes a finding silenced forever.
    """
    from app.models import IpamDrift

    org, vrf_id, subnet_id = await _fixture(admin_engine, "supconf")
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for address, until in (
            ("10.60.0.5", now + timedelta(days=1)),
            ("10.60.0.9", now - timedelta(days=1)),
        ):
            session.add(
                IpamDrift(
                    org_id=org.org_id,
                    vrf_id=vrf_id,
                    subnet_id=subnet_id,
                    address=address,
                    drift_class="conflict",
                    severity="high",
                    confidence="high",
                    suppressed_until=until,
                    note="under investigation",
                )
            )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        with pytest.raises(IpamConflictError, match="conflict"):
            await assign(
                session,
                org.org_id,
                org.user_id,
                client_id=org.org_id,
                address="10.60.0.9",
                device_id=org.device_id,
            )
        await session.commit()
    assert (await _state_of(admin_engine, org.org_id, "10.60.0.5"))[0] == "assigned"


async def test_the_actor_is_recorded_and_is_never_an_agent(
    admin_engine: AsyncEngine,
) -> None:
    """§3.2's last row. Every audit event this module writes for a state change
    carries `actor_type='user'`; there is no call site that could pass anything
    else, and that is the property asserted."""
    org, _, _ = await _fixture(admin_engine, "actor")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        row = await assign(
            session,
            org.org_id,
            org.user_id,
            client_id=org.org_id,
            address="10.60.0.5",
            device_id=org.device_id,
        )
        await release(session, org.org_id, org.user_id, row.id)
        await deprecate(session, org.org_id, org.user_id, row.id)
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org.org_id,
                        AuditEvent.target_type == "ipam_address",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows
    assert {r.actor_type for r in rows} == {"user"}
