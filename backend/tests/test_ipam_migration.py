"""The ten IPAM tables against the schema `alembic upgrade head` actually
produced (docs/modules/ipam/spec.md §2.1-§2.10, §11.1, §11.2).

READ THIS BEFORE ADDING A TEST HERE.

1. THE RLS TESTS ARE BEHAVIOURAL. A test asserting `relrowsecurity` is true
   passes against a policy whose predicate was deleted, so the enable/force
   check is a smoke check only. For an `ALL`-command policy Postgres reuses
   `USING` as `WITH CHECK` when the latter is omitted, so DELETING `WITH CHECK`
   falsifies nothing -- the mutations that falsify these are `WITH CHECK (true)`
   for the insert tests and `USING (true)` for the read tests. Both were run;
   both outputs are in the task report.

2. THE TYPE TESTS ARE STRUCTURAL ON PURPOSE. A `text` column passes every
   behavioural test in this suite until the first `10.4.70.0/24` sorts wrong or
   the first `A4-BB-6D-11-22-33` fails to equal `a4:bb:6d:11:22:33`. Native
   `inet`/`cidr`/`macaddr` (§0.1) cannot be asserted behaviourally without
   writing the very containment queries the rest of the module is made of, so
   `udt_name` is pinned directly.

3. THE OBSERVATION TESTS ASSERT ROW COUNTS AND COUNTERS, NEVER "no exception".
   §0.15's defect FAILS NOTHING: keyed on the nullable `mac`, two ICMP-only
   upserts of one address produce TWO rows each with `sighting_count = 1`
   instead of ONE row with `sighting_count = 2`. Nothing raises, nothing is
   logged, and §2.7 quietly becomes the probe time series §0.10 point 4 exists
   to forbid. Every test here is written so the two worlds print different
   values.

4. WHAT THESE FIXTURES DELIBERATELY VARY. The observation key is five columns
   (`org_id, vrf_id, address, source, mac_key`) and a fixture that holds four of
   them constant cannot see the fifth being dropped. There is therefore one test
   per column of that key: two orgs, two VRFs, two sources, two MACs, and the
   NULL-MAC pair. A single "it upserts" test with everything else constant would
   pass against a key of `(address)` alone.

5. EVERY CHECK AND UNIQUENESS TEST REDUCES BOTH WORLDS TO A COMPARABLE STRING
   via `_add_outcome`, rather than asserting `pytest.raises`. A `pytest.raises`
   test that fails prints only "DID NOT RAISE"; this one prints the rows the
   database accepted, which is what a deleted constraint actually produces.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.events import EVENT_TYPES
from app.core.rbac import PERMISSIONS
from app.core.tenancy import org_session
from tests.ipam_fixtures import (
    IpamOrg,
    observations_at,
    seed_org,
    seed_site,
    seed_subnet,
    seed_vrf,
    upsert_observation_row,
    utcnow,
)

# §11.1's correction, restated as a list so a builder never recounts looking for
# a table that does not exist: there are TEN, not eleven. §0.13 rules out an
# eleventh by putting assignment history in the audit log.
TABLES = (
    "ipam_vrf",
    "ipam_site",
    "ipam_vlan",
    "ipam_subnet",
    "ipam_range",
    "ipam_address",
    "ipam_sweep_run",
    "ipam_observation",
    "ipam_drift",
    "ipam_scan_authorization",
)

IPAM_PERMISSIONS = (
    "ipam.read",
    "ipam.assign",
    "ipam.manage",
    "ipam.sweep.run",
    "ipam.sweep.manage",
    "ipam.scope.manage",
)


class _Fx:
    """An org plus one of every referenced row, so the tenancy tests have a
    legal row to try to plant in another tenant."""

    def __init__(self, org: IpamOrg, vrf_id: UUID, site_id: UUID, subnet_id: UUID) -> None:
        self.org = org
        self.org_id = org.org_id
        self.user_id = org.user_id
        self.device_id = org.device_id
        self.vrf_id = vrf_id
        self.site_id = site_id
        self.subnet_id = subnet_id


async def _fixture(admin_engine: AsyncEngine, tag: str) -> _Fx:
    org = await seed_org(admin_engine, tag)
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    site_id = await seed_site(admin_engine, org.org_id)
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.9.0.0/24"
    )
    return _Fx(org, vrf_id, site_id, subnet_id)


def _row(table: str, fx: _Fx) -> Any:
    """One legal ORM row for `table`, owned by `fx`'s org."""
    from app.models import (
        IpamAddress,
        IpamDrift,
        IpamObservation,
        IpamRange,
        IpamScanAuthorization,
        IpamSite,
        IpamSubnet,
        IpamSweepRun,
        IpamVlan,
        IpamVrf,
    )

    now = utcnow()
    unique = uuid4().int % 200 + 2
    if table == "ipam_vrf":
        return IpamVrf(org_id=fx.org_id, name=f"vrf-{uuid4().hex[:8]}")
    if table == "ipam_site":
        return IpamSite(org_id=fx.org_id, name=f"site-{uuid4().hex[:8]}")
    if table == "ipam_vlan":
        return IpamVlan(org_id=fx.org_id, site_id=fx.site_id, vid=unique)
    if table == "ipam_subnet":
        return IpamSubnet(
            org_id=fx.org_id, vrf_id=fx.vrf_id, prefix=f"10.{unique}.0.0/24", kind="assignable"
        )
    if table == "ipam_range":
        return IpamRange(
            org_id=fx.org_id,
            subnet_id=fx.subnet_id,
            kind="dhcp_pool",
            start_address="10.9.0.100",
            end_address="10.9.0.200",
        )
    if table == "ipam_address":
        return IpamAddress(
            org_id=fx.org_id,
            subnet_id=fx.subnet_id,
            vrf_id=fx.vrf_id,
            address=f"10.9.0.{unique}",
            state="reserved",
        )
    if table == "ipam_sweep_run":
        return IpamSweepRun(
            org_id=fx.org_id,
            subnet_id=fx.subnet_id,
            state="no_agent",
            trigger="manual",
            started_at=now,
        )
    if table == "ipam_observation":
        return IpamObservation(
            org_id=fx.org_id,
            vrf_id=fx.vrf_id,
            subnet_id=fx.subnet_id,
            address=f"10.9.0.{unique}",
            source="sweep",
            method="icmp",
            confidence="low",
            first_seen_at=now,
            last_seen_at=now,
        )
    if table == "ipam_drift":
        return IpamDrift(
            org_id=fx.org_id,
            vrf_id=fx.vrf_id,
            subnet_id=fx.subnet_id,
            address=f"10.9.0.{unique}",
            drift_class="documented_absent",
            severity="medium",
            confidence="high",
            first_detected_at=now,
            last_evaluated_at=now,
        )
    if table == "ipam_scan_authorization":
        return IpamScanAuthorization(
            org_id=fx.org_id,
            vrf_id=fx.vrf_id,
            prefix=f"10.{unique}.0.0/24",
            authorized_by=fx.user_id,
            authorized_at=now,
        )
    raise AssertionError(f"no row factory for {table}")


# ---------------------------------------------------------------------------
# Tenancy (invariant #5)
# ---------------------------------------------------------------------------


def test_the_module_defines_exactly_ten_org_scoped_tables() -> None:
    """§11.1 as first written said "all eleven tables"; §2 defines ten, and the
    spec now says so. Pinned here so the RLS parametrizations below cannot
    silently cover nine."""
    assert len(TABLES) == 10
    assert len(set(TABLES)) == 10


@pytest.mark.parametrize("table", TABLES)
async def test_rls_is_enabled_and_forced(admin_engine: AsyncEngine, table: str) -> None:
    """A smoke check. FORCE matters separately from ENABLE: without it the table
    owner bypasses the policy, and migrations run as the owner."""
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                sqltext(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True)


@pytest.mark.parametrize("table", TABLES)
async def test_cross_org_read_returns_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, table: str
) -> None:
    """Falsified by `USING (true)`, which returns org A's row to org B.

    Deleting the `WITH CHECK` clause falsifies NOTHING here -- Postgres reuses
    `USING` as `WITH CHECK` for an `ALL` policy when the latter is omitted.
    """
    a = await _fixture(admin_engine, f"ro-a-{table[5:11]}")
    b = await _fixture(admin_engine, f"ro-b-{table[5:11]}")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(_row(table, a))
        await session.commit()

    query = sqltext(f"SELECT org_id FROM {table}")  # noqa: S608 -- table is from TABLES
    async with org_session(b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        seen_by_b = {r.org_id for r in (await session.execute(query)).all()}
    async with org_session(a.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        seen_by_a = {r.org_id for r in (await session.execute(query)).all()}

    assert a.org_id not in seen_by_b
    assert seen_by_a == {a.org_id}


@pytest.mark.parametrize("table", TABLES)
async def test_cross_org_insert_is_rejected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, table: str
) -> None:
    """Falsified by `WITH CHECK (true)`, which lets org B write into org A's
    address plan -- and, for `ipam_scan_authorization`, decide where org A's
    fleet emits packets.

    The outcome is reduced to a string rather than left as `pytest.raises`: a
    bare `raises` prints "DID NOT RAISE" when it fails, which says nothing about
    what the database did. This prints the row org B managed to plant.
    """
    a = await _fixture(admin_engine, f"wi-a-{table[5:11]}")
    b = await _fixture(admin_engine, f"wi-b-{table[5:11]}")
    planted = _row(table, a)
    try:
        async with org_session(b.org_id, engine=app_engine, clients=ClientScope.all()) as session:
            session.add(planted)
            await session.flush()
    except (DBAPIError, ProgrammingError) as exc:
        outcome = "refused: " + str(getattr(exc, "orig", exc)).splitlines()[0]
    else:
        async with admin_engine.connect() as conn:
            rows = (
                await conn.execute(
                    sqltext(f"SELECT org_id FROM {table} WHERE org_id = :o"),  # noqa: S608
                    {"o": a.org_id},
                )
            ).all()
        outcome = (
            f"ACCEPTED: org B wrote into {table}; org A ({a.org_id}) now holds "
            f"{len(rows)} row(s) it did not write"
        )
    assert outcome.startswith("refused: "), outcome
    assert "row-level security policy" in outcome, outcome


# ---------------------------------------------------------------------------
# §0.1 -- native network types, pinned structurally
# ---------------------------------------------------------------------------


_NATIVE_TYPES = [
    ("ipam_subnet", "prefix", "cidr"),
    ("ipam_scan_authorization", "prefix", "cidr"),
    ("ipam_subnet", "gateway", "inet"),
    ("ipam_address", "address", "inet"),
    ("ipam_observation", "address", "inet"),
    ("ipam_drift", "address", "inet"),
    ("ipam_range", "start_address", "inet"),
    ("ipam_range", "end_address", "inet"),
    ("ipam_address", "mac", "macaddr"),
    ("ipam_observation", "mac", "macaddr"),
]


@pytest.mark.parametrize(("table", "column", "udt"), _NATIVE_TYPES)
async def test_network_columns_are_native_not_text(
    admin_engine: AsyncEngine, table: str, column: str, udt: str
) -> None:
    """§0.1. In `text` these columns pass every behavioural test in this file
    and are wrong at every boundary: `10.4.7.9` sorts before `10.4.7.10`, and
    `10.4.70.0/24` matches `LIKE '10.4.7%'`."""
    async with admin_engine.connect() as conn:
        actual = (
            await conn.execute(
                sqltext(
                    "SELECT udt_name FROM information_schema.columns"
                    " WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            )
        ).scalar_one()
    assert actual == udt


_GIST_INDEXES = [
    ("ix_ipam_subnet_prefix_gist", "prefix"),
    ("ix_ipam_address_gist", "address"),
    ("ix_ipam_obs_gist", "address"),
    ("ix_ipam_scope_gist", "prefix"),
]


@pytest.mark.parametrize(("index", "column"), _GIST_INDEXES)
async def test_gist_index_exists_with_the_inet_ops_opclass(
    admin_engine: AsyncEngine, index: str, column: str
) -> None:
    """The index that serves containment (`>>=`, `<<=`) and overlap (`&&`) --
    parent derivation, "which subnet holds this observation", and §0.3's sibling
    check. `inet_ops` is NOT the default GiST opclass for these types, so an
    index created without naming it does not exist at all; asserting `USING
    gist` without the opclass would not catch that."""
    async with admin_engine.connect() as conn:
        definition = (
            await conn.execute(
                sqltext("SELECT indexdef FROM pg_indexes WHERE indexname = :i"), {"i": index}
            )
        ).scalar_one()
    assert "USING gist" in definition, definition
    assert f"{column} inet_ops" in definition, definition


async def test_the_vlan_uniqueness_pair_is_partial_indexes_not_constraints(
    admin_engine: AsyncEngine,
) -> None:
    """§0.15's accepted failure mode, pinned: neither VLAN index is a named
    constraint, so `ON CONFLICT ON CONSTRAINT` fails at RUNTIME against them and
    every upsert must use `index_elements=` + `index_where=`.

    Asserted as "has a predicate AND has no `pg_constraint` row". If a later
    edit turned either into a plain `UniqueConstraint`, this goes red here
    rather than at runtime inside a service nobody has upserted through yet.
    """
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sqltext(
                    "SELECT i.relname AS name, ix.indpred IS NOT NULL AS partial,"
                    " c.conname AS constraint_name"
                    " FROM pg_index ix JOIN pg_class i ON i.oid = ix.indexrelid"
                    " LEFT JOIN pg_constraint c ON c.conindid = ix.indexrelid"
                    " WHERE i.relname IN ('uq_ipam_vlan_org_site_vid',"
                    " 'uq_ipam_vlan_org_vid_global')"
                )
            )
        ).all()
    assert {r.name for r in rows} == {
        "uq_ipam_vlan_org_site_vid",
        "uq_ipam_vlan_org_vid_global",
    }
    assert all(r.partial for r in rows), rows
    assert all(r.constraint_name is None for r in rows), rows


# ---------------------------------------------------------------------------
# Outcome-as-a-string, so a deleted CHECK or index prints the rows it let through
# ---------------------------------------------------------------------------


def _refusal(message: str) -> str:
    """The constraint or index name Postgres blamed, out of its error text."""
    for token in message.replace('"', " ").replace("'", " ").split():
        if token.startswith(("ck_", "uq_")):
            return f"refused by {token}"
    return f"refused: {message.splitlines()[0]}"


async def _add_outcome(
    admin_engine: AsyncEngine, row: Any, *, readback: str, params: dict[str, Any]
) -> str:
    """Commit `row` and reduce the outcome to a STRING: the constraint that
    refused it, or `ACCEPTED:` plus what `readback` then finds in the table."""
    try:
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            session.add(row)
            await session.commit()
    except (IntegrityError, DBAPIError, ProgrammingError) as exc:
        return _refusal(str(getattr(exc, "orig", exc)))
    async with admin_engine.connect() as conn:
        stored = (await conn.execute(sqltext(readback), params)).all()
    return f"ACCEPTED: {[tuple(r) for r in stored]}"


_SUBNETS_READBACK = "SELECT prefix::text, kind, gateway::text FROM ipam_subnet WHERE org_id = :o"


async def test_an_unknown_subnet_kind_is_refused(admin_engine: AsyncEngine) -> None:
    """§0.2's `container`/`assignable` split is what stops an operator asking
    for the next free address in their /8. A third value would be a subnet
    neither branch knows what to do with."""
    from app.models import IpamSubnet

    fx = await _fixture(admin_engine, "kind")
    row = IpamSubnet(org_id=fx.org_id, vrf_id=fx.vrf_id, prefix="10.20.0.0/24", kind="router")
    outcome = await _add_outcome(
        admin_engine, row, readback=_SUBNETS_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "refused by ck_ipam_subnet_kind"


async def test_a_gateway_outside_its_own_prefix_is_refused(admin_engine: AsyncEngine) -> None:
    """A gateway outside its own subnet is a typo, and it is the typo that makes
    provisioning hand out a broken static config. Without the CHECK this stores
    `10.20.0.0/24` with a gateway of `192.168.1.1` and nothing complains until a
    device cannot route."""
    from app.models import IpamSubnet

    fx = await _fixture(admin_engine, "gw")
    row = IpamSubnet(
        org_id=fx.org_id, vrf_id=fx.vrf_id, prefix="10.20.0.0/24", gateway="192.168.1.1"
    )
    outcome = await _add_outcome(
        admin_engine, row, readback=_SUBNETS_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "refused by ck_ipam_subnet_gateway_in"


async def test_a_gateway_inside_its_prefix_is_accepted(admin_engine: AsyncEngine) -> None:
    """The other half. A CHECK of `gateway IS NULL` alone would pass the test
    above while refusing every legitimate gateway ever recorded."""
    from app.models import IpamSubnet

    fx = await _fixture(admin_engine, "gwok")
    row = IpamSubnet(
        org_id=fx.org_id, vrf_id=fx.vrf_id, prefix="10.21.0.0/24", gateway="10.21.0.1"
    )
    outcome = await _add_outcome(
        admin_engine, row, readback=_SUBNETS_READBACK, params={"o": fx.org_id}
    )
    assert outcome.startswith("ACCEPTED:"), outcome
    # `inet` renders a host address with its masklen: `10.21.0.1/32`.
    assert "'10.21.0.0/24', 'assignable', '10.21.0.1/32'" in outcome, outcome


async def test_a_sweep_interval_under_one_hour_is_refused(admin_engine: AsyncEngine) -> None:
    """§0.10 point 6 in SQL. A one-minute sweep interval is the exact mechanism
    by which an IPAM becomes a ping monitor; the application clamp is one
    refactor away from being the only one."""
    from app.models import IpamSubnet

    fx = await _fixture(admin_engine, "int")
    row = IpamSubnet(
        org_id=fx.org_id, vrf_id=fx.vrf_id, prefix="10.22.0.0/24", sweep_interval_hours=0
    )
    outcome = await _add_outcome(
        admin_engine, row, readback=_SUBNETS_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "refused by ck_ipam_subnet_sweep_interval"


_VLAN_READBACK = "SELECT vid, site_id FROM ipam_vlan WHERE org_id = :o"


@pytest.mark.parametrize("vid", [0, 4095])
async def test_a_vlan_id_outside_1_4094_is_refused(admin_engine: AsyncEngine, vid: int) -> None:
    """Both ends. A CHECK of `vid > 0` alone passes the `vid = 0` case and
    stores VLAN 4095, which no switch will accept."""
    from app.models import IpamVlan

    fx = await _fixture(admin_engine, f"vid{vid}")
    row = IpamVlan(org_id=fx.org_id, site_id=fx.site_id, vid=vid)
    outcome = await _add_outcome(
        admin_engine, row, readback=_VLAN_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "refused by ck_ipam_vlan_vid_range"


async def test_a_reversed_range_is_refused(admin_engine: AsyncEngine) -> None:
    """`start > end` is a range covering nothing, which every containment query
    silently agrees with."""
    from app.models import IpamRange

    fx = await _fixture(admin_engine, "rng")
    row = IpamRange(
        org_id=fx.org_id,
        subnet_id=fx.subnet_id,
        kind="dhcp_pool",
        start_address="10.9.0.200",
        end_address="10.9.0.100",
    )
    outcome = await _add_outcome(
        admin_engine,
        row,
        readback="SELECT start_address::text, end_address::text FROM ipam_range WHERE org_id = :o",
        params={"o": fx.org_id},
    )
    assert outcome == "refused by ck_ipam_range_ordered"


_ADDRESS_READBACK = (
    "SELECT address::text, state, device_id, asset_label FROM ipam_address WHERE org_id = :o"
)


async def test_a_derived_status_cannot_be_stored_in_state(admin_engine: AsyncEngine) -> None:
    """§0.6, and this is the CHECK the whole split rests on. `in_use`, `stale`
    and `conflicting` are computed at read time from (state, ranges,
    observations, claimant liveness). If `in_use` were storable here, an agent
    reporting an address would have to write this column and a device coming
    online would silently overwrite what an operator documented -- which is what
    "the IPAM changed my reservation by itself" means."""
    from app.models import IpamAddress

    fx = await _fixture(admin_engine, "state")
    row = IpamAddress(
        org_id=fx.org_id,
        subnet_id=fx.subnet_id,
        vrf_id=fx.vrf_id,
        address="10.9.0.5",
        state="in_use",
    )
    outcome = await _add_outcome(
        admin_engine, row, readback=_ADDRESS_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "refused by ck_ipam_address_state"


_OBSERVATION_READBACK = (
    "SELECT address::text, source, allocation_origin FROM ipam_observation WHERE org_id = :o"
)


async def test_an_allocation_origin_outside_the_enum_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    """Migration 0051's CHECK. §14.1's five values and nothing else.

    THIS CONSTRAINT IS HALF OF A GUARANTEE AND NOT A BELT-AND-BRACES.
    `nic.py` normalises an unrecognised origin to `unknown` BEFORE it gets here,
    and the reason it must is this constraint: on the `inventory.collect` result
    path an `IntegrityError` is a DATABASE error, which poisons the enclosing
    transaction whether or not the Python exception is caught, and no `except` in
    that file can contain it. The parser keeps the common case away from here;
    this keeps our own code -- the sweep handler, the importer -- honest.
    """
    from app.models import IpamObservation

    fx = await _fixture(admin_engine, "origin")
    row = IpamObservation(
        org_id=fx.org_id,
        vrf_id=fx.vrf_id,
        subnet_id=fx.subnet_id,
        address="10.9.0.40",
        source="inventory",
        method="nic",
        confidence="high",
        allocation_origin="dhcpv6",
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )
    outcome = await _add_outcome(
        admin_engine, row, readback=_OBSERVATION_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "refused by ck_ipam_obs_allocation_origin"


async def test_the_check_admits_exactly_the_parsers_vocabulary(
    admin_engine: AsyncEngine,
) -> None:
    """THE OTHER HALF, and the one that catches the drift that actually happens.

    A CHECK that refuses `dhcpv6` also passes if it refuses `slaac` -- and a
    constraint narrower than `nic.ORIGINS` turns a legitimate NIC report into the
    uncontainable failure above. Driven off `ORIGINS` itself so a sixth value
    added to the parser without a migration fails HERE rather than in the field.
    """
    from app.models import IpamObservation
    from app.modules.ipam.nic import ORIGINS

    fx = await _fixture(admin_engine, "originok")
    for index, origin in enumerate(sorted(ORIGINS), start=50):
        row = IpamObservation(
            org_id=fx.org_id,
            vrf_id=fx.vrf_id,
            subnet_id=fx.subnet_id,
            address=f"10.9.0.{index}",
            source="inventory",
            method="nic",
            confidence="high",
            allocation_origin=origin,
            first_seen_at=utcnow(),
            last_seen_at=utcnow(),
        )
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            session.add(row)
            await session.commit()

    async with admin_engine.connect() as conn:
        stored = (
            await conn.execute(
                sqltext(
                    "SELECT allocation_origin FROM ipam_observation WHERE org_id = :o"
                    " ORDER BY allocation_origin"
                ),
                {"o": fx.org_id},
            )
        ).scalars().all()
    assert list(stored) == sorted(ORIGINS)


async def test_an_observation_defaults_to_an_unknown_allocation_origin(
    admin_engine: AsyncEngine,
) -> None:
    """The server default, asserted on a row that never mentions the column --
    which is every row written before migration 0051 and every sweep observation
    forever. `unknown` is the true answer for both, not a placeholder: ARP and
    ICMP see that an address answers, never how it was configured."""
    from app.models import IpamObservation

    fx = await _fixture(admin_engine, "origindef")
    row = IpamObservation(
        org_id=fx.org_id,
        vrf_id=fx.vrf_id,
        subnet_id=fx.subnet_id,
        address="10.9.0.41",
        source="sweep",
        method="icmp",
        confidence="low",
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )
    outcome = await _add_outcome(
        admin_engine, row, readback=_OBSERVATION_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "ACCEPTED: [('10.9.0.41/32', 'sweep', 'unknown')]"


async def test_an_assignment_with_two_claimants_is_refused(admin_engine: AsyncEngine) -> None:
    """An assignment has EXACTLY ONE claimant. With both set, "who has .42" has
    two answers and the reconciliation predicates in §4.3 read the wrong one."""
    from app.models import IpamAddress

    fx = await _fixture(admin_engine, "both")
    row = IpamAddress(
        org_id=fx.org_id,
        subnet_id=fx.subnet_id,
        vrf_id=fx.vrf_id,
        address="10.9.0.6",
        state="assigned",
        device_id=fx.device_id,
        asset_label="the printer",
    )
    outcome = await _add_outcome(
        admin_engine, row, readback=_ADDRESS_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "refused by ck_ipam_address_one_claimant"


async def test_an_assignment_with_no_claimant_is_refused(admin_engine: AsyncEngine) -> None:
    """THE OTHER HALF, and the one a `NOT (a AND b)` CHECK would let through.
    "Assigned to nothing" is how an address is held forever by nobody."""
    from app.models import IpamAddress

    fx = await _fixture(admin_engine, "neither")
    row = IpamAddress(
        org_id=fx.org_id,
        subnet_id=fx.subnet_id,
        vrf_id=fx.vrf_id,
        address="10.9.0.7",
        state="assigned",
    )
    outcome = await _add_outcome(
        admin_engine, row, readback=_ADDRESS_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "refused by ck_ipam_address_one_claimant"


async def test_a_reserved_address_needs_no_claimant(admin_engine: AsyncEngine) -> None:
    """The claimant rule is scoped to `state='assigned'` (§2.6). A CHECK without
    that qualifier passes both tests above and makes reserving an address --
    "hold .1 for the gateway we have not bought yet" -- impossible."""
    from app.models import IpamAddress

    fx = await _fixture(admin_engine, "resv")
    row = IpamAddress(
        org_id=fx.org_id,
        subnet_id=fx.subnet_id,
        vrf_id=fx.vrf_id,
        address="10.9.0.8",
        state="reserved",
    )
    outcome = await _add_outcome(
        admin_engine, row, readback=_ADDRESS_READBACK, params={"o": fx.org_id}
    )
    assert outcome.startswith("ACCEPTED:"), outcome


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------


async def test_the_same_prefix_twice_in_one_vrf_is_refused(admin_engine: AsyncEngine) -> None:
    from app.models import IpamSubnet

    fx = await _fixture(admin_engine, "dupsub")
    duplicate = IpamSubnet(org_id=fx.org_id, vrf_id=fx.vrf_id, prefix="10.9.0.0/24")
    outcome = await _add_outcome(
        admin_engine, duplicate, readback=_SUBNETS_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "refused by uq_ipam_subnet_org_vrf_prefix"


async def test_the_same_prefix_in_a_second_vrf_is_accepted(admin_engine: AsyncEngine) -> None:
    """THE OTHER HALF, and the reason `vrf_id` is in the key at all: two routing
    domains legitimately both hold 10.9.0.0/24. A key of `(org_id, prefix)`
    passes the test above and makes overlapping RFC1918 estates -- the ordinary
    case for an MSP -- unrepresentable."""
    from app.models import IpamSubnet

    fx = await _fixture(admin_engine, "twovrf")
    other_vrf = await seed_vrf(admin_engine, fx.org_id, name="customer-a")
    row = IpamSubnet(org_id=fx.org_id, vrf_id=other_vrf, prefix="10.9.0.0/24")
    outcome = await _add_outcome(
        admin_engine, row, readback=_SUBNETS_READBACK, params={"o": fx.org_id}
    )
    assert outcome.startswith("ACCEPTED:"), outcome


async def test_the_same_address_twice_in_one_vrf_is_refused(admin_engine: AsyncEngine) -> None:
    """One row per address per routing domain (§2.6). A duplicate CLAIM is a §4
    conflict, computed from observations -- never a second row."""
    from app.models import IpamAddress

    fx = await _fixture(admin_engine, "dupaddr")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            IpamAddress(
                org_id=fx.org_id, subnet_id=fx.subnet_id, vrf_id=fx.vrf_id, address="10.9.0.9"
            )
        )
        await session.commit()
    duplicate = IpamAddress(
        org_id=fx.org_id, subnet_id=fx.subnet_id, vrf_id=fx.vrf_id, address="10.9.0.9"
    )
    outcome = await _add_outcome(
        admin_engine, duplicate, readback=_ADDRESS_READBACK, params={"o": fx.org_id}
    )
    assert outcome == "refused by uq_ipam_address_org_vrf_addr"


_SCOPE_READBACK = (
    "SELECT prefix::text, revoked_at FROM ipam_scan_authorization WHERE org_id = :o"
)


async def _scope_row(fx: _Fx, prefix: str, *, revoked: bool = False) -> Any:
    from app.models import IpamScanAuthorization

    now = utcnow()
    return IpamScanAuthorization(
        org_id=fx.org_id,
        vrf_id=fx.vrf_id,
        prefix=prefix,
        authorized_by=fx.user_id,
        authorized_at=now,
        revoked_at=now if revoked else None,
        revoked_by=fx.user_id if revoked else None,
    )


async def test_two_active_authorizations_for_one_prefix_are_refused(
    admin_engine: AsyncEngine,
) -> None:
    fx = await _fixture(admin_engine, "scope1")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(await _scope_row(fx, "10.9.0.0/24"))
        await session.commit()
    outcome = await _add_outcome(
        admin_engine,
        await _scope_row(fx, "10.9.0.0/24"),
        readback=_SCOPE_READBACK,
        params={"o": fx.org_id},
    )
    assert outcome == "refused by uq_ipam_scope_active"


async def test_a_revoked_authorization_does_not_block_a_new_one(
    admin_engine: AsyncEngine,
) -> None:
    """THE OTHER HALF, and the one a TOTAL unique index fails. Rows are never
    hard-deleted -- revocation stamps `revoked_at` so the record of what was
    authorized when survives as evidence -- so without `WHERE revoked_at IS
    NULL` a prefix authorized and later revoked could never be authorized again.
    This pair is what proves the index is partial rather than total."""
    fx = await _fixture(admin_engine, "scope2")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(await _scope_row(fx, "10.9.0.0/24", revoked=True))
        await session.commit()
    outcome = await _add_outcome(
        admin_engine,
        await _scope_row(fx, "10.9.0.0/24"),
        readback=_SCOPE_READBACK,
        params={"o": fx.org_id},
    )
    assert outcome.startswith("ACCEPTED:"), outcome
    assert outcome.count("10.9.0.0/24") == 2, outcome


# ---------------------------------------------------------------------------
# §0.15 -- the NULL-collision defect, tested through the UPSERT PATH
# ---------------------------------------------------------------------------


async def test_two_icmp_only_observations_upsert_into_one_row(
    admin_engine: AsyncEngine,
) -> None:
    """THE §0.15 TEST. Two ICMP-only observations of one address, upserted in
    sequence, must yield exactly ONE row with `sighting_count = 2`.

    IF THE GUARD WERE DELETED -- the key inferred on the nullable `mac` instead
    of the generated `mac_key` -- this test observes TWO rows, each with
    `sighting_count = 1`. Different VALUES, not merely a different exception:
    with `mac IS NULL` on both rows, NULLs do not collide, no conflict is ever
    detected, nothing raises, and the weekly sweep of a /24 appends a row per
    responding host per sweep forever. §2.7 would stop being the current-state
    table §0.10 point 4 rests on and become the probe time series that point
    exists to forbid.
    """
    fx = await _fixture(admin_engine, "s1")
    for _ in range(2):
        await upsert_observation_row(
            admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.9.0.42"
        )
    assert await observations_at(admin_engine, fx.org_id, "10.9.0.42") == [("sweep", None, 2)]


async def test_icmp_and_arp_evidence_of_one_address_stay_two_rows(
    admin_engine: AsyncEngine,
) -> None:
    """`source` is in the key ON PURPOSE, and a fix for the test above that
    collapses these two is a different bug. ICMP evidence is `confidence='low'`
    ("something answered; that is all it means") and ARP evidence is
    `confidence='medium'` (authoritative for address<->MAC on-segment) --
    merging them destroys the distinction §4.3's `mac_mismatch` depends on."""
    fx = await _fixture(admin_engine, "s1src")
    await upsert_observation_row(admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.9.0.43")
    await upsert_observation_row(
        admin_engine,
        fx.org_id,
        vrf_id=fx.vrf_id,
        address="10.9.0.43",
        method="arp",
        confidence="medium",
        mac="a4:bb:6d:11:22:33",
    )
    assert await observations_at(admin_engine, fx.org_id, "10.9.0.43") == [
        ("sweep", "a4:bb:6d:11:22:33", 1),
        ("sweep", None, 1),
    ]


async def test_a_changed_mac_lands_as_a_second_row_so_mismatch_can_see_both(
    admin_engine: AsyncEngine,
) -> None:
    """§0.15's ACCEPTED failure mode, asserted rather than assumed. When the MAC
    observed at an address changes -- a NIC replaced, a new host at a recycled
    address -- the new evidence lands as a SECOND row because `mac_key` differs.
    That is intended: §4.3's `mac_mismatch` and `conflict` predicates both read
    the set of current observations at an address and need BOTH MACs to be able
    to say anything. The bounded cost is one row per distinct MAC ever seen per
    source, not one row per probe."""
    fx = await _fixture(admin_engine, "s1mac")
    for mac in ("a4:bb:6d:11:22:33", "00:11:22:33:44:55"):
        await upsert_observation_row(
            admin_engine,
            fx.org_id,
            vrf_id=fx.vrf_id,
            address="10.9.0.44",
            method="arp",
            confidence="medium",
            mac=mac,
        )
    assert await observations_at(admin_engine, fx.org_id, "10.9.0.44") == [
        ("sweep", "00:11:22:33:44:55", 1),
        ("sweep", "a4:bb:6d:11:22:33", 1),
    ]


async def test_one_address_in_two_vrfs_stays_two_observations(
    admin_engine: AsyncEngine,
) -> None:
    """`vrf_id` is in the key: 10.9.0.42 in two routing domains is two facts,
    not one. Without this test every observation fixture in this file shares one
    VRF, and dropping `vrf_id` from the key would pass everything."""
    fx = await _fixture(admin_engine, "s1vrf")
    other_vrf = await seed_vrf(admin_engine, fx.org_id, name="customer-b")
    for vrf in (fx.vrf_id, other_vrf):
        await upsert_observation_row(
            admin_engine, fx.org_id, vrf_id=vrf, address="10.9.0.45"
        )
    rows = await observations_at(admin_engine, fx.org_id, "10.9.0.45")
    assert rows == [("sweep", None, 1), ("sweep", None, 1)]


async def test_one_address_in_two_orgs_stays_two_observations(
    admin_engine: AsyncEngine,
) -> None:
    """`org_id` is in the key, and RFC1918 guarantees two tenants hold the same
    address. Without this test every fixture here is single-org and a key
    missing `org_id` would merge two customers' evidence into one row."""
    a = await _fixture(admin_engine, "s1org-a")
    b = await _fixture(admin_engine, "s1org-b")
    for fx in (a, b):
        await upsert_observation_row(
            admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.9.0.46"
        )
    assert await observations_at(admin_engine, a.org_id, "10.9.0.46") == [("sweep", None, 1)]
    assert await observations_at(admin_engine, b.org_id, "10.9.0.46") == [("sweep", None, 1)]


async def test_mac_key_is_generated_by_the_database(admin_engine: AsyncEngine) -> None:
    """§0.15: GENERATED, not defaulted at write time. A stored generated column
    cannot be bypassed by a write path that forgets it, or by a second ingestion
    path added later -- which is the whole point of moving the guarantee into the
    schema rather than into a service.

    Both halves are asserted: the column IS generated, and its value tracks
    `mac` (a generated column declared over the wrong expression would still
    report `is_generated = ALWAYS`)."""
    fx = await _fixture(admin_engine, "genc")
    await upsert_observation_row(
        admin_engine,
        fx.org_id,
        vrf_id=fx.vrf_id,
        address="10.9.0.47",
        method="arp",
        confidence="medium",
        mac="A4-BB-6D-11-22-33",
    )
    await upsert_observation_row(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, address="10.9.0.48"
    )
    async with admin_engine.connect() as conn:
        is_generated = (
            await conn.execute(
                sqltext(
                    "SELECT is_generated FROM information_schema.columns"
                    " WHERE table_name = 'ipam_observation' AND column_name = 'mac_key'"
                )
            )
        ).scalar_one()
        keys = sorted(
            r.mac_key
            for r in (
                await conn.execute(
                    sqltext("SELECT mac_key FROM ipam_observation WHERE org_id = :o"),
                    {"o": fx.org_id},
                )
            ).all()
        )
    assert is_generated == "ALWAYS"
    # `macaddr` normalises the Windows form on the way in (§0.1), and `mac_key`
    # carries the normalised text -- not whatever the agent formatted.
    assert keys == ["", "a4:bb:6d:11:22:33"]


async def test_writing_mac_key_directly_is_refused_by_postgres(
    admin_engine: AsyncEngine,
) -> None:
    """The property that makes §0.15 unforgettable rather than remembered: a
    write path that TRIES to set `mac_key` -- including one that "helpfully"
    computes `coalesce(mac, '')` itself -- is a hard Postgres error, not a
    silently divergent value. The plan's instruction to "set `mac_key` in the
    same expression that sets `mac`" is this error; the spec amendment
    supersedes it."""
    from app.models import IpamObservation

    fx = await _fixture(admin_engine, "genw")
    now = utcnow()
    row = IpamObservation(
        org_id=fx.org_id,
        vrf_id=fx.vrf_id,
        address="10.9.0.49",
        source="sweep",
        method="icmp",
        confidence="low",
        first_seen_at=now,
        last_seen_at=now,
    )
    row.mac_key = "whatever-i-like"
    outcome = await _add_outcome(
        admin_engine,
        row,
        readback="SELECT mac_key FROM ipam_observation WHERE org_id = :o",
        params={"o": fx.org_id},
    )
    assert outcome.startswith("refused:"), outcome
    assert "cannot insert a non-DEFAULT value into column" in outcome, outcome
    assert "mac_key" in outcome, outcome


# ---------------------------------------------------------------------------
# §0.15 / §2.3 -- the VLAN partial index PAIR
# ---------------------------------------------------------------------------


async def test_a_second_org_wide_vlan_10_is_refused(admin_engine: AsyncEngine) -> None:
    """S2. `UNIQUE (org_id, site_id, vid)` alone permits TWO org-wide VLAN 10
    rows, because `(org, NULL, 10)` does not collide with itself -- exactly the
    duplicate the constraint exists to prevent. Deleting
    `uq_ipam_vlan_org_vid_global` makes this print two rows."""
    from app.models import IpamVlan

    fx = await _fixture(admin_engine, "vlanglob")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(IpamVlan(org_id=fx.org_id, vid=10, name="voice"))
        await session.commit()
    outcome = await _add_outcome(
        admin_engine,
        IpamVlan(org_id=fx.org_id, vid=10, name="voice-again"),
        readback=_VLAN_READBACK,
        params={"o": fx.org_id},
    )
    assert outcome == "refused by uq_ipam_vlan_org_vid_global"


async def test_vlan_10_at_two_different_sites_is_accepted(admin_engine: AsyncEngine) -> None:
    """The site-scoped half. VLAN 10 legitimately exists at every site, once
    each -- a single org-wide `UNIQUE (org_id, vid)` would refuse the second
    site, which is the opposite failure and just as wrong."""
    from app.models import IpamVlan

    fx = await _fixture(admin_engine, "vlansite")
    second_site = await seed_site(admin_engine, fx.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(IpamVlan(org_id=fx.org_id, site_id=fx.site_id, vid=10))
        await session.commit()
    outcome = await _add_outcome(
        admin_engine,
        IpamVlan(org_id=fx.org_id, site_id=second_site, vid=10),
        readback=_VLAN_READBACK,
        params={"o": fx.org_id},
    )
    assert outcome.startswith("ACCEPTED:"), outcome
    assert outcome.count("10,") == 2, outcome


async def test_vlan_10_twice_at_one_site_is_refused(admin_engine: AsyncEngine) -> None:
    """And the site-scoped index actually holds. Without it, "VLAN 10 at
    Manila" is storable twice and every site-scoped lookup has two answers."""
    from app.models import IpamVlan

    fx = await _fixture(admin_engine, "vlandup")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(IpamVlan(org_id=fx.org_id, site_id=fx.site_id, vid=10))
        await session.commit()
    outcome = await _add_outcome(
        admin_engine,
        IpamVlan(org_id=fx.org_id, site_id=fx.site_id, vid=10),
        readback=_VLAN_READBACK,
        params={"o": fx.org_id},
    )
    assert outcome == "refused by uq_ipam_vlan_org_site_vid"


# ---------------------------------------------------------------------------
# RBAC (§11.2) and the event registry (§10)
# ---------------------------------------------------------------------------


async def _codes_for_role(admin_engine: AsyncEngine, role: str) -> set[str]:
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sqltext(
                    "SELECT p.code FROM roles r"
                    " JOIN role_permissions rp ON rp.role_id = r.id"
                    " JOIN permissions p ON p.id = rp.permission_id"
                    " WHERE r.name = :r AND r.org_id IS NULL AND p.module = 'ipam'"
                ),
                {"r": role},
            )
        ).all()
    return {r.code for r in rows}


async def test_the_six_permission_codes_are_seeded_and_described(
    admin_engine: AsyncEngine,
) -> None:
    """Seeded in the migration AND described in `app/core/rbac.py` -- the
    `permissions` table is `(id, code, module, action)` with no description
    column, so the two must be kept in step by a test rather than by one place
    holding both."""
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                sqltext("SELECT code, module, action FROM permissions WHERE module = 'ipam'")
            )
        ).all()
    assert {r.code for r in rows} == set(IPAM_PERMISSIONS)
    assert {(r.module, r.action) for r in rows} == {
        ("ipam", code.split(".", 1)[1]) for code in IPAM_PERMISSIONS
    }
    for code in IPAM_PERMISSIONS:
        assert code in PERMISSIONS, f"{code} is seeded but has no description in rbac.py"


@pytest.mark.parametrize("role", ["super-admin", "org-admin"])
async def test_admin_roles_hold_all_six(admin_engine: AsyncEngine, role: str) -> None:
    assert await _codes_for_role(admin_engine, role) == set(IPAM_PERMISSIONS)


async def test_technician_holds_exactly_read_assign_and_sweep_run(
    admin_engine: AsyncEngine,
) -> None:
    """§11.2's split, asserted as an EQUALITY rather than as three membership
    checks -- the whole control is what a technician does NOT hold."""
    assert await _codes_for_role(admin_engine, "technician") == {
        "ipam.read",
        "ipam.assign",
        "ipam.sweep.run",
    }


@pytest.mark.parametrize("role", ["auditor", "read-only"])
async def test_the_read_tier_holds_exactly_ipam_read(
    admin_engine: AsyncEngine, role: str
) -> None:
    assert await _codes_for_role(admin_engine, role) == {"ipam.read"}


@pytest.mark.parametrize("role", ["technician", "auditor", "read-only"])
@pytest.mark.parametrize("code", ["ipam.scope.manage", "ipam.manage"])
async def test_no_non_admin_role_holds_a_high_privilege_ipam_grant(
    admin_engine: AsyncEngine, role: str, code: str
) -> None:
    """THE §11.2 CONTROL, asserted as an explicit negative.

    `ipam.scope.manage` decides where this platform emits network traffic --
    the same class of grant as `notification.admin`. `ipam.sweep.run` and
    `ipam.scope.manage` are deliberately separate: running a sweep inside an
    already-authorized scope is routine technician work, and deciding what may
    be scoped at all is not. Collapsing them would let anyone who can run a scan
    also decide what may be scanned, which is the whole control -- and a
    role-grant test that only asserts what a role HAS cannot see that.
    """
    assert code not in await _codes_for_role(admin_engine, role)


def test_both_ipam_event_types_are_registered() -> None:
    """`publish` raises `UnknownEventType` for anything unregistered, so an
    absent entry here is a runtime failure in the reconcile sweep. The
    renderer and pinned-wire-sample obligations that come with registration are
    enforced by `test_notification_delivery.py`'s two structural tests."""
    assert "ipam.drift_detected" in EVENT_TYPES
    assert "ipam.sweep_rejected" in EVENT_TYPES


def test_neither_ipam_event_type_is_staff_only() -> None:
    """§10, stated as a test because the absence of an entry is otherwise
    indistinguishable from an oversight. `STAFF_ONLY_EVENT_TYPES` exists to keep
    a notification away from a TICKET'S EXTERNAL REQUESTER; an IPAM event has no
    ticket and no requester, so membership would add a `notification_rules`
    CHECK constraint guarding a pairing that cannot occur."""
    from app.core.events import STAFF_ONLY_EVENT_TYPES

    assert "ipam.drift_detected" not in STAFF_ONLY_EVENT_TYPES
    assert "ipam.sweep_rejected" not in STAFF_ONLY_EVENT_TYPES
