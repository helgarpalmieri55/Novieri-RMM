"""Inventory-derived observations (`docs/modules/ipam/spec.md` §4.1, §4.2, §0.6,
§0.15).

WHAT THESE FIXTURES DELIBERATELY VARY, stated because holding one of these
constant is how each guard below stops being observable:

* **The upsert key is exercised one column at a time.** Every other test in this
  file writes `source='inventory'` in `vrf` A at one address, so on its own the
  suite could not tell whether the key were `(org, vrf, address, source,
  mac_key)` or just `(org, address)`. `test_each_key_column_is_load_bearing`
  varies exactly one of the five per case and asserts TWO rows, against a
  control that varies none and asserts one row with `sighting_count = 2`.
* **The currency window is exercised with an AGED row, not only fresh ones.** A
  fixture that only ever seeds `last_seen_at = now()` cannot see a missing
  `last_seen_at` filter in `current_observations`.
* **The TTL test seeds TWO orgs with DIFFERENT policies.** One policy row makes
  "reads this org's policy" and "reads the only policy there is"
  indistinguishable.
* **Every `org_id`-predicate test runs on the ADMIN engine.** Through
  `org_session` RLS rejects first, so those tests pass with the service's own
  predicate deleted.
* **The "not `Device.nics`" test gives the two sources DIFFERENT addresses.**
  Seeded identically, reading the jsonb convenience copy and reading
  `hardware_components` produce the same rows and the distinction is invisible.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import (
    Device,
    HardwareComponent,
    IpamAddress,
    IpamObservation,
    IpamVrf,
    Policy,
)
from app.modules.ipam.observations import (
    current_observations,
    observation_ttl_days,
    orphan_groups,
    record_inventory_observations,
    upsert_observation,
)
from tests.ipam_fixtures import (
    IpamOrg,
    seed_agent,
    seed_device,
    seed_org,
    seed_subnet,
    seed_vrf,
    upsert_observation_row,
)


def _block(*addresses: str) -> dict:
    """§14.1's `attributes.addresses[]` -- the ONE address key a NIC carries.

    Every fixture in this file used to seed `{"ip": "10.1.2.42"}` instead. That
    key was removed from both the agent and the parser (OQ-2, closed
    2026-08-05), and leaving the fixtures alone was not an option: the tests
    that assert a ROW COUNT would have gone red, and the ones that assert ZERO
    rows would have gone green for the wrong reason -- observing that nothing
    was read rather than that the discard rule fired.

    Prefixes are carried because the block always carries them and
    `parse_address` strips them; a fixture without one would exercise a shape
    the agent never sends.
    """
    return {
        "addresses": [
            {
                "address": a,
                "family": "ipv6" if ":" in a.split("/")[0] else "ipv4",
                "origin": "dhcp",
            }
            for a in addresses
        ]
    }


async def _nics(engine: AsyncEngine, org: IpamOrg, *components: dict) -> None:
    """`hardware_components` rows of `type='nic'`, exactly as inventory's step 4
    leaves them (normalized, wholesale-replaced current state)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        for component in components:
            session.add(
                HardwareComponent(
                    org_id=org.org_id,
                    device_id=org.device_id,
                    type=component.get("type", "nic"),
                    identifier=component.get("identifier", uuid4().hex[:12]),
                    attributes=component.get("attributes", {}),
                )
            )
        await session.commit()


async def _record(engine: AsyncEngine, org: IpamOrg, device_id: UUID | None = None) -> int:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        device = (
            await session.execute(
                select(Device).where(Device.id == (device_id or org.device_id))
            )
        ).scalar_one()
        written = await record_inventory_observations(session, device)
        await session.commit()
        return written


async def _rows(engine: AsyncEngine, org_id: UUID) -> list[IpamObservation]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(IpamObservation)
                    .where(IpamObservation.org_id == org_id)
                    .order_by(IpamObservation.address)
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# §14.1's NIC shape, end to end through the service (§4.1)
# ---------------------------------------------------------------------------


async def test_a_nic_produces_one_high_confidence_observation(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "obs")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.1.2.0/24"
    )
    await _nics(
        admin_engine, org, {"identifier": "a4:bb:6d:11:22:33", "attributes": _block("10.1.2.42/24")}
    )

    assert await _record(admin_engine, org) == 1

    (row,) = await _rows(admin_engine, org.org_id)
    assert str(row.address) == "10.1.2.42"
    assert row.source == "inventory"
    assert row.method == "nic"
    # §2.7: the OS reported about itself, over mTLS, as the result of a signed
    # job. That is what `high` means, and `mac_mismatch` (§4.3) reads it.
    assert row.confidence == "high"
    assert row.device_id == org.device_id
    assert row.vrf_id == vrf_id
    assert row.subnet_id == subnet_id  # derived by containment, not reported
    assert str(row.mac) == "a4:bb:6d:11:22:33"
    assert row.sighting_count == 1


async def test_an_identifier_that_is_not_a_mac_stores_mac_null(
    admin_engine: AsyncEngine,
) -> None:
    """ASSERTS THE STORED ROW, not the absence of an exception.

    IF THE `parse_mac` GUARD WERE DELETED in `nic.py` (`identifier` written
    straight into `mac`) the write is refused --
    `IpamValidationError: 'Ethernet0' is not a valid MAC address` from
    `upsert_observation`'s own re-validation, and a `DataError` from `macaddr`
    if that were removed too -- so the observable difference is a stored row
    with `mac IS NULL` versus NO ROW AT ALL. A test phrased as "does not raise"
    sees neither, and would pass against an implementation that stored
    `'Ethernet0'` in `hostname` and lost the address.
    """
    org = await seed_org(admin_engine, "obsnomac")
    await seed_vrf(admin_engine, org.org_id)
    await _nics(admin_engine, org, {"identifier": "Ethernet0", "attributes": _block("10.9.9.9/24")})

    assert await _record(admin_engine, org) == 1
    (row,) = await _rows(admin_engine, org.org_id)
    assert row.mac is None
    assert str(row.address) == "10.9.9.9"


async def test_undocumentable_addresses_produce_zero_rows(admin_engine: AsyncEngine) -> None:
    """IF `is_documentable` WERE DROPPED this stores three rows, not zero --
    counted, not "no error"."""
    org = await seed_org(admin_engine, "obsdisc")
    await seed_vrf(admin_engine, org.org_id)
    await _nics(
        admin_engine,
        org,
        {"attributes": _block("127.0.0.1/8")},
        {"attributes": _block("169.254.3.4/16")},
        {"attributes": _block("fe80::1%4/64")},
    )
    assert await _record(admin_engine, org) == 0
    assert await _rows(admin_engine, org.org_id) == []


async def test_a_malformed_nic_block_writes_nothing_and_does_not_raise(
    admin_engine: AsyncEngine,
) -> None:
    """The service inherits `nic.py`'s totality. Asserted as a row count.

    THE MALFORMED VALUES ARE INSIDE THE BLOCK, which is the only key read. Left
    as `{"ip": 167838250}` these fixtures would still store zero rows and this
    test would still be green -- but it would be green because nothing looks at
    `ip`, so it would no longer observe the totality it is named for. Removing
    a KEY is not removing the TOLERANCE, and this is where the difference shows.
    """
    org = await seed_org(admin_engine, "obsbad")
    await seed_vrf(admin_engine, org.org_id)
    await _nics(
        admin_engine,
        org,
        {"attributes": {"addresses": [{"address": 167838250}]}},
        {"attributes": {"addresses": [{"address": "x" * 4096}]}},
        {"attributes": {"addresses": "10.1.2.42/24"}},
        {"attributes": {"addresses": [None, 42, {}]}},
        {"attributes": {}},
    )
    assert await _record(admin_engine, org) == 0
    assert await _rows(admin_engine, org.org_id) == []


async def test_hardware_components_is_the_source_not_the_device_nics_copy(
    admin_engine: AsyncEngine,
) -> None:
    """§4.1: `Device.nics` is a flattened jsonb CONVENIENCE COPY;
    `hardware_components` is the typed, indexed, uniquely keyed current state.

    THE TWO SOURCES CARRY DIFFERENT ADDRESSES ON PURPOSE. Seeded identically,
    reading either produces the same row and the choice is invisible. IF THE
    SERVICE READ `Device.nics` this observes an observation at `10.55.55.55`
    and none at `10.1.2.42`.

    THE DECOY IS IN THE SHAPE `ingest._components_of_type` REALLY WRITES --
    `{"identifier": ..., **attributes}` -- so it carries §14.1's block, not the
    removed `ip` key. That matters: a decoy the parser could not read under any
    mutation is not a decoy, and after `ip` stopped being read a flat
    `{"identifier": ..., "ip": ...}` was exactly that.
    """
    org = await seed_org(admin_engine, "obssrc")
    await seed_vrf(admin_engine, org.org_id)
    await _nics(
        admin_engine, org, {"identifier": "a4:bb:6d:11:22:33", "attributes": _block("10.1.2.42/24")}
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = (
            await session.execute(select(Device).where(Device.id == org.device_id))
        ).scalar_one()
        device.nics = {
            "items": [{"identifier": "de:ad:be:ef:00:01", **_block("10.55.55.55/24")}]
        }
        await session.commit()

    await _record(admin_engine, org)
    assert [str(r.address) for r in await _rows(admin_engine, org.org_id)] == ["10.1.2.42"]


async def test_agent_last_ip_is_never_an_observation_source(
    admin_engine: AsyncEngine,
) -> None:
    """§4.1: `Agent.last_ip` is the TCP peer address the gateway saw
    (`app/gateway/main.py:243`), which behind NAT belongs to a firewall.

    IF IT WERE INGESTED this observes one observation at `203.0.113.9` -- the
    wrong machine documented at the wrong address -- instead of none.
    """
    org = await seed_org(admin_engine, "obsnat")
    await seed_vrf(admin_engine, org.org_id)
    await seed_agent(
        admin_engine, org.org_id, org.device_id, status="online", last_ip="203.0.113.9"
    )

    assert await _record(admin_engine, org) == 0
    assert await _rows(admin_engine, org.org_id) == []


async def test_no_nic_data_creates_no_default_vrf(admin_engine: AsyncEngine) -> None:
    """§0.7's `global` row is created lazily ON FIRST USE. A device with nothing
    to document is not a use, and creating the row anyway would mean every
    `inventory.collect` in the estate wrote an IPAM row."""
    org = await seed_org(admin_engine, "obsvrf")
    assert await _record(admin_engine, org) == 0
    async with AsyncSession(admin_engine) as session:
        count = (
            await session.execute(select(IpamVrf.id).where(IpamVrf.org_id == org.org_id))
        ).all()
    assert count == []


async def test_the_default_vrf_is_created_on_the_first_observation(
    admin_engine: AsyncEngine,
) -> None:
    org = await seed_org(admin_engine, "obsvrf2")
    await _nics(admin_engine, org, {"attributes": _block("10.1.2.42/24")})
    assert await _record(admin_engine, org) == 1
    async with AsyncSession(admin_engine) as session:
        names = (
            (await session.execute(select(IpamVrf.name).where(IpamVrf.org_id == org.org_id)))
            .scalars()
            .all()
        )
    assert list(names) == ["global"]


# ---------------------------------------------------------------------------
# The upsert key (§0.15, §2.7)
# ---------------------------------------------------------------------------


async def test_the_same_nic_ingested_twice_is_one_row(admin_engine: AsyncEngine) -> None:
    """§0.10 point 4: a CURRENT-STATE table, not a time series.

    IF THE UPSERT WERE A PLAIN INSERT (or `ON CONFLICT DO NOTHING`) this
    observes TWO rows, each with `sighting_count = 1` -- executed and confirmed.
    A key naming `mac` rather than the generated `mac_key` cannot be exercised
    from here at all: migration `0043` created `uq_ipam_obs_key` on `mac_key`,
    so the mutated statement is refused outright with "there is no unique or
    exclusion constraint matching the ON CONFLICT specification" and no row
    lands. The broken key is unrepresentable in this schema, which is the point
    of §0.15 having moved the guarantee into the DDL.
    """
    org = await seed_org(admin_engine, "obstwice")
    await seed_vrf(admin_engine, org.org_id)
    await _nics(
        admin_engine, org, {"identifier": "a4:bb:6d:11:22:33", "attributes": _block("10.1.2.42/24")}
    )
    await _record(admin_engine, org)
    await _record(admin_engine, org)

    rows = await _rows(admin_engine, org.org_id)
    assert len(rows) == 1
    assert rows[0].sighting_count == 2
    assert rows[0].last_seen_at > rows[0].first_seen_at


async def test_icmp_only_observations_upsert(admin_engine: AsyncEngine) -> None:
    """§0.15's serious one, through the SERVICE rather than the fixture: an
    ICMP-only observation has `mac IS NULL`, NULLs do not collide, and the
    obvious key would append a row per host per sweep forever.

    IF THE UPSERT WERE DEGRADED TO A PLAIN INSERT this observes TWO rows each
    with `sighting_count = 1` -- different values, not a different exception
    (executed). Naming `mac` in `index_elements` instead of `mac_key` is refused
    by Postgres before any row lands, because `uq_ipam_obs_key` is on `mac_key`:
    §0.15's fix is in the DDL, so the defect it prevents cannot be reintroduced
    from the service alone.
    """
    org = await seed_org(admin_engine, "obsicmp")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for _ in range(2):
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
            )
        await session.commit()

    rows = await _rows(admin_engine, org.org_id)
    assert len(rows) == 1
    assert rows[0].sighting_count == 2
    assert rows[0].mac is None


async def test_each_key_column_is_load_bearing(admin_engine: AsyncEngine) -> None:
    """FIVE COLUMNS, VARIED ONE AT A TIME, against a control that varies none.

    Every other test in this file writes one address, one source, one VRF and
    one MAC, so none of them can tell `(org, vrf, address, source, mac_key)`
    from `(org, address)`. Here each case changes EXACTLY ONE component of the
    key and asserts two rows; the control changes nothing and asserts one row
    with `sighting_count = 2`. Degrade the upsert to a plain insert and the
    CONTROL case observes two rows instead of one (executed). Drop a column
    from `index_elements` and Postgres refuses the statement outright -- the
    five columns are the index's, not this file's choice.
    """
    org = await seed_org(admin_engine, "obskey")
    vrf_a = await seed_vrf(admin_engine, org.org_id)
    vrf_b = await seed_vrf(admin_engine, org.org_id, name="dmz")
    base = {
        "vrf_id": vrf_a,
        "address": "10.4.7.9",
        "mac": "a4:bb:6d:11:22:33",
        "source": "sweep",
        "method": "arp",
        "confidence": "medium",
    }
    variants = {
        "control": {},
        "vrf_id": {"vrf_id": vrf_b},
        "address": {"address": "10.4.7.10"},
        "mac_key": {"mac": "a4:bb:6d:11:22:99"},
        "source": {"source": "import", "method": "manual", "confidence": "medium"},
    }
    for label, delta in variants.items():
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            for values in (base, base | delta):
                await upsert_observation(
                    session,
                    org_id=org.org_id,
                    hostname="",
                    device_id=None,
                    **values,  # type: ignore[arg-type]
                )
            await session.commit()
        rows = await _rows(admin_engine, org.org_id)
        expected = 1 if label == "control" else 2
        assert len(rows) == expected, f"{label}: expected {expected} rows, got {len(rows)}"
        if label == "control":
            assert rows[0].sighting_count == 2
        # Clear between cases so each one starts from an empty table.
        async with AsyncSession(admin_engine) as session:
            await session.execute(
                delete(IpamObservation).where(IpamObservation.org_id == org.org_id)
            )
            await session.commit()


async def test_a_re_registered_subnet_re_files_an_existing_observation(
    admin_engine: AsyncEngine,
) -> None:
    """An orphan stops being one when its subnet is finally registered.

    `subnet_id` is re-derived on every upsert and written in the conflict
    branch too. IF THE CONFLICT BRANCH OMITTED IT the address would stay an
    orphan forever -- the "24 addresses in 10.7.3.x and no subnet registered"
    finding would never clear after the operator acted on it.
    """
    org = await seed_org(admin_engine, "obsrefile")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await _nics(admin_engine, org, {"attributes": _block("10.7.3.11/24")})
    await _record(admin_engine, org)
    assert (await _rows(admin_engine, org.org_id))[0].subnet_id is None

    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.7.3.0/24"
    )
    await _record(admin_engine, org)
    rows = await _rows(admin_engine, org.org_id)
    assert len(rows) == 1
    assert rows[0].subnet_id == subnet_id


# ---------------------------------------------------------------------------
# §0.6's split: ingestion writes observations and nothing else
# ---------------------------------------------------------------------------


async def test_ingestion_cannot_change_a_documented_address(
    admin_engine: AsyncEngine,
) -> None:
    """§0.6/§6.4: the agent's report can CREATE an observation and can never
    change `ipam_address.state`, `device_id` or `assigned_by`.

    IF THE SPLIT WERE DELETED (ingestion writing the address row) this observes
    `state` flipped or `device_id` re-pointed at the reporting device. This is
    the test that stops "the IPAM changed my reservation by itself".
    """
    org = await seed_org(admin_engine, "obssplit")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.1.2.0/24")
    other_device = await seed_device(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            IpamAddress(
                org_id=org.org_id,
                vrf_id=vrf_id,
                address="10.1.2.42",
                state="assigned",
                device_id=other_device,
                assigned_by=org.user_id,
                assigned_at=datetime.now(UTC),
            )
        )
        await session.commit()

    await _nics(
        admin_engine, org, {"identifier": "a4:bb:6d:11:22:33", "attributes": _block("10.1.2.42/24")}
    )
    await _record(admin_engine, org)

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(IpamAddress).where(
                    IpamAddress.org_id == org.org_id, IpamAddress.address == "10.1.2.42"
                )
            )
        ).scalar_one()
        assert row.state == "assigned"
        assert row.device_id == other_device
        assert row.assigned_by == org.user_id
        assert row.mac is None
    # ... and the evidence still landed.
    assert len(await _rows(admin_engine, org.org_id)) == 1


# ---------------------------------------------------------------------------
# Currency, TTL and orphans (§4.2)
# ---------------------------------------------------------------------------


async def test_observation_ttl_defaults_to_fourteen_days(admin_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "obsttl")
    async with AsyncSession(admin_engine) as session:
        assert await observation_ttl_days(session, org.org_id) == 14


async def test_observation_ttl_is_read_per_org(admin_engine: AsyncEngine) -> None:
    """TWO ORGS, TWO DIFFERENT POLICIES, on the ADMIN engine.

    With one policy row, "reads this org's policy" and "reads the only policy
    there is" are indistinguishable; through `org_session` RLS would reject the
    other org's row before the service's own `org_id` predicate was consulted.
    IF THAT PREDICATE WERE DELETED this observes org B's `3` for org A.
    """
    org_a = await seed_org(admin_engine, "obsttla")
    org_b = await seed_org(admin_engine, "obsttlb")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Policy(org_id=org_a.org_id, module="ipam", config={"observation_ttl_days": 30})
        )
        session.add(
            Policy(org_id=org_b.org_id, module="ipam", config={"observation_ttl_days": 3})
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert await observation_ttl_days(session, org_a.org_id) == 30
        assert await observation_ttl_days(session, org_b.org_id) == 3


async def test_a_nonsense_ttl_policy_falls_back_to_the_default(
    admin_engine: AsyncEngine,
) -> None:
    """The policy is operator-editable JSONB. A `0` there would make every
    observation instantly non-current, which silently disables the allocator's
    reconciliation clause and every absence gate -- so it is clamped, not
    trusted."""
    org = await seed_org(admin_engine, "obsttlbad")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Policy(org_id=org.org_id, module="ipam", config={"observation_ttl_days": "soon"})
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert await observation_ttl_days(session, org.org_id) == 14


async def test_current_observations_excludes_aged_rows(admin_engine: AsyncEngine) -> None:
    """THE FIXTURE SEEDS ONE FRESH AND ONE AGED ROW.

    A fixture with only fresh rows cannot see a missing `last_seen_at` filter.
    IF THE WINDOW PREDICATE WERE DELETED this observes `10.4.7.10` in the result
    -- and everything downstream (the allocator's "not currently observed"
    clause, every §4.3 absence gate) would be reading a three-month-old sighting
    as present.
    """
    org = await seed_org(admin_engine, "obscur")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    now = datetime.now(UTC)
    await upsert_observation_row(
        admin_engine, org.org_id, vrf_id=vrf_id, address="10.4.7.9", now=now
    )
    await upsert_observation_row(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        address="10.4.7.10",
        now=now - timedelta(days=90),
    )
    async with AsyncSession(admin_engine) as session:
        found = await current_observations(
            session, org.org_id, ["10.4.7.9", "10.4.7.10", "10.4.7.11"]
        )
    assert sorted(found) == ["10.4.7.9"]
    assert len(found["10.4.7.9"]) == 1


async def test_current_observations_honours_the_orgs_ttl(admin_engine: AsyncEngine) -> None:
    """A 90-day-old sighting is current under a 365-day policy. The window is
    read, not hardcoded."""
    org = await seed_org(admin_engine, "obscurttl")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Policy(org_id=org.org_id, module="ipam", config={"observation_ttl_days": 365})
        )
        await session.commit()
    await upsert_observation_row(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        address="10.4.7.10",
        now=datetime.now(UTC) - timedelta(days=90),
    )
    async with AsyncSession(admin_engine) as session:
        assert list(await current_observations(session, org.org_id, ["10.4.7.10"])) == [
            "10.4.7.10"
        ]


async def test_current_observations_is_org_scoped_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE. Through `org_session` this passes with the
    service's `org_id` predicate deleted, because RLS rejects first -- the
    defect class that shipped four times in the previous sub-project."""
    org_a = await seed_org(admin_engine, "obsxa")
    org_b = await seed_org(admin_engine, "obsxb")
    vrf_a = await seed_vrf(admin_engine, org_a.org_id)
    vrf_b = await seed_vrf(admin_engine, org_b.org_id)
    await upsert_observation_row(admin_engine, org_a.org_id, vrf_id=vrf_a, address="10.4.7.9")
    await upsert_observation_row(admin_engine, org_b.org_id, vrf_id=vrf_b, address="10.4.7.9")
    async with AsyncSession(admin_engine) as session:
        found = await current_observations(session, org_a.org_id, ["10.4.7.9"])
    assert len(found["10.4.7.9"]) == 1
    assert found["10.4.7.9"][0].org_id == org_a.org_id


async def test_orphans_group_by_inferred_prefix(admin_engine: AsyncEngine) -> None:
    """§4.2: "24 addresses in 10.7.3.x and no subnet registered" is a finding,
    not drift -- nothing is wrong with the plan, the plan is ABSENT.

    IF THE `subnet_id IS NULL` PREDICATE WERE DELETED the documented
    `10.1.2.42` appears in the groups too, and the one-click "register this
    subnet" would offer to register a subnet that exists.
    """
    org = await seed_org(admin_engine, "obsorph")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.1.2.0/24")
    await _nics(
        admin_engine,
        org,
        {"attributes": _block("10.1.2.42/24")},
        {"attributes": _block("10.7.3.11/24")},
        {"attributes": _block("10.7.3.12/24")},
        {"attributes": _block("2001:db8:1::5/64")},
    )
    await _record(admin_engine, org)

    async with AsyncSession(admin_engine) as session:
        groups = await orphan_groups(session, org.org_id)
    assert [(g.prefix, g.count) for g in groups] == [
        ("10.7.3.0/24", 2),
        ("2001:db8:1::/64", 1),
    ]


async def test_orphan_groups_ignore_aged_observations(admin_engine: AsyncEngine) -> None:
    """An orphan nobody has seen in three months is not a subnet worth
    registering today. Same window as everything else."""
    org = await seed_org(admin_engine, "obsorphage")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await upsert_observation_row(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        address="10.7.3.11",
        now=datetime.now(UTC) - timedelta(days=90),
    )
    async with AsyncSession(admin_engine) as session:
        assert await orphan_groups(session, org.org_id) == []


async def test_orphan_groups_are_org_scoped_on_the_admin_engine(
    admin_engine: AsyncEngine,
) -> None:
    org_a = await seed_org(admin_engine, "obsoa")
    org_b = await seed_org(admin_engine, "obsob")
    vrf_a = await seed_vrf(admin_engine, org_a.org_id)
    vrf_b = await seed_vrf(admin_engine, org_b.org_id)
    await upsert_observation_row(admin_engine, org_a.org_id, vrf_id=vrf_a, address="10.7.3.11")
    await upsert_observation_row(admin_engine, org_b.org_id, vrf_id=vrf_b, address="10.8.4.11")
    async with AsyncSession(admin_engine) as session:
        groups = await orphan_groups(session, org_a.org_id)
    assert [g.prefix for g in groups] == ["10.7.3.0/24"]
