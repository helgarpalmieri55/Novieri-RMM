"""Reconciliation: the drift predicates and the three gates
(`docs/modules/ipam/spec.md` §4.3, §4.4, §0.8, §0.11, §0.16).

**THE GATES ARE THE MODULE'S WHOLE THESIS.** Every IPAM that got ignored got
ignored because its drift list was mostly laptops that were asleep. Gate E
(something looked, recently), Gate H (something ever looked) and Gate L (the
claimant is alive) are what keep the list worth opening, so most of this file is
NEGATIVE tests -- the findings that must NOT appear.

WHAT THESE FIXTURES DELIBERATELY VARY:

* **Gate L's two cases differ only in the AGENT row.** One device's agent is
  offline past `W_obs`, the other's is online -- and the online one's
  `Device.last_seen_at` is 60 days stale on purpose. `Device.last_seen_at` is
  written only by inventory ingestion and enrollment, so it means "last
  inventory sweep", not "last contact"; an implementation reading `Device`
  marks every connected-but-not-recently-swept machine dormant. The two tests
  observe DIFFERENT `drift_class` values, in different tabs, in different
  counts.
* **Gate H's two fixtures differ only in whether a successful sweep run ever
  existed.** Same address, same state, same silence, same gates otherwise. If a
  test can pass with Gate H deleted it is testing the ordering of §3.1's table
  and not the gate.
* **Gate E's fixture has a live claimant and a documented address** -- i.e.
  everything `documented_absent` needs except evidence -- so deleting the gate
  produces findings rather than removing them.
* **`mac_mismatch`'s negative fixture carries a REAL current observation with a
  DIFFERENT documented MAC**, and differs from the positive only in the
  observation's method/confidence. An ICMP observation carries no MAC at all, so
  the predicate is unsatisfiable -- which is why `confidence` is a column.
* **The two-detection rule is tested by counting `domain_events` rows**, not by
  asserting an absence: one pass then two passes, with the count changing from 0
  to 1 and staying at 1 on the third.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import AuditEvent, Device, DomainEvent, IpamDrift
from app.modules.ipam.reconcile import (
    DORMANT_CLASS,
    DRIFT_CLASSES,
    RECONCILE_INTERVAL_SECONDS,
    acknowledge_drift,
    evaluate_org,
    list_drift,
    suppress_drift,
)
from tests.ipam_fixtures import (
    seed_address,
    seed_agent,
    seed_device,
    seed_org,
    seed_range,
    seed_subnet,
    seed_sweep_run,
    seed_vlan,
    seed_vrf,
    upsert_observation_row,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(hours=2)
ANCIENT = NOW - timedelta(days=90)
MAC_A = "a4:bb:6d:11:22:33"
MAC_B = "a4:bb:6d:44:55:66"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _Fx:
    def __init__(self, org, vrf_id: UUID, subnet_id: UUID) -> None:
        self.org = org
        self.org_id = org.org_id
        self.user_id = org.user_id
        self.vrf_id = vrf_id
        self.subnet_id = subnet_id


async def _fx(engine: AsyncEngine, tag: str, prefix: str = "10.70.0.0/24", **kw) -> _Fx:
    org = await seed_org(engine, tag)
    vrf_id = await seed_vrf(engine, org.org_id)
    subnet_id = await seed_subnet(
        engine, org.org_id, vrf_id=vrf_id, prefix=prefix, **kw
    )
    return _Fx(org, vrf_id, subnet_id)


async def _observe(engine: AsyncEngine, fx: _Fx, address: str, **kw) -> None:
    """A current observation. `last_seen_at` defaults to two hours ago -- well
    inside `W_obs` -- so a test that means "aged out" has to say so."""
    kw.setdefault("now", RECENT)
    kw.setdefault("subnet_id", fx.subnet_id)
    await upsert_observation_row(
        engine, fx.org_id, vrf_id=fx.vrf_id, address=address, **kw
    )


async def _evidence(engine: AsyncEngine, fx: _Fx, **kw) -> UUID:
    """A succeeded sweep run inside `W_obs`: Gate E and Gate H both satisfied."""
    kw.setdefault("started_at", RECENT)
    return await seed_sweep_run(engine, fx.org_id, subnet_id=fx.subnet_id, **kw)


async def _live_device(engine: AsyncEngine, fx: _Fx, **kw) -> UUID:
    device_id = await seed_device(engine, fx.org_id, **kw)
    await seed_agent(engine, fx.org_id, device_id, status="online", last_seen_at=RECENT)
    return device_id


async def _evaluate(engine: AsyncEngine, org_id: UUID, *, now: datetime = NOW):
    async with AsyncSession(engine, expire_on_commit=False) as session:
        stats = await evaluate_org(session, org_id, now=now)
        await session.commit()
        return stats


async def _drift(engine: AsyncEngine, org_id: UUID) -> dict[tuple[str, str], IpamDrift]:
    """Every stored `ipam_drift` row keyed `(address, class)` -- INCLUDING
    `dormant` and including resolved rows, so a test can tell "not detected"
    from "stamped resolved" from "deleted"."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(IpamDrift).where(IpamDrift.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    return {(str(r.address), r.drift_class): r for r in rows}


async def _classes(engine: AsyncEngine, org_id: UUID, address: str) -> set[str]:
    return {
        cls for (addr, cls) in (await _drift(engine, org_id)) if addr == address
    }


async def _events(engine: AsyncEngine, org_id: UUID) -> list[DomainEvent]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return list(
            (
                await session.execute(
                    select(DomainEvent).where(
                        DomainEvent.org_id == org_id,
                        DomainEvent.event_type == "ipam.drift_detected",
                    )
                )
            )
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------- #
# Gate L -- the claimant-liveness gate, and WHICH TABLE it reads
# --------------------------------------------------------------------------- #


async def test_a_device_whose_agent_is_offline_is_dormant_not_absent(
    admin_engine: AsyncEngine
) -> None:
    """§0.11: silence is not information.

    Everything `documented_absent` needs is present -- an assigned address, no
    current observation, and a successful sweep inside the window -- EXCEPT a
    live claimant. *If Gate L were deleted* this observes `documented_absent` in
    the drift list and in the counts: the sleeping-laptop finding that makes an
    operator stop opening the page.
    """
    fx = await _fx(admin_engine, "gatel")
    device_id = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, device_id, status="offline",
                     last_seen_at=ANCIENT)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=device_id)
    await _evidence(admin_engine, fx)

    await _evaluate(admin_engine, fx.org_id)
    assert await _classes(admin_engine, fx.org_id, "10.70.0.42") == {DORMANT_CLASS}


async def test_an_online_agent_with_an_ancient_device_row_is_absent_not_dormant(
    admin_engine: AsyncEngine
) -> None:
    """**Gate L READS `Agent`, NEVER `Device`.**

    `Device.last_seen_at` is written only by inventory ingestion
    (`app/modules/inventory/ingest.py`) and by enrollment, so it means "last
    inventory sweep", not "last contact". This fixture is the exact shape that
    distinguishes them: the agent is connected NOW and the device row has not
    been touched in sixty days.

    *If the implementation read `Device`* this observes `dormant` -- a different
    stored `drift_class`, in a different tab, excluded from the counts, for a
    machine that is online and demonstrably not at the address the plan claims.
    """
    fx = await _fx(admin_engine, "gateldev")
    device_id = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, device_id, status="online",
                     last_seen_at=RECENT)
    async with AsyncSession(admin_engine) as session:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one()
        device.last_seen_at = NOW - timedelta(days=60)
        await session.commit()
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=device_id)
    await _evidence(admin_engine, fx)

    await _evaluate(admin_engine, fx.org_id)
    rows = await _drift(admin_engine, fx.org_id)
    assert ("10.70.0.42", "documented_absent") in rows
    assert ("10.70.0.42", DORMANT_CLASS) not in rows
    # A live device contradicting the plan is HIGH confidence (§0.11's asymmetry).
    assert rows[("10.70.0.42", "documented_absent")].confidence == "high"


async def test_an_unclaimed_reservation_passes_gate_l(
    admin_engine: AsyncEngine
) -> None:
    """§4.3: Gate L is `dev(D) IS NULL` **or** `live(dev(D))`. A reserved address
    with no device claimant has no liveness to check, and treating "no claimant"
    as "not live" would make every reservation permanently dormant."""
    fx = await _fx(admin_engine, "noclaim")
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       state="reserved")
    await _evidence(admin_engine, fx)

    await _evaluate(admin_engine, fx.org_id)
    assert await _classes(admin_engine, fx.org_id, "10.70.0.42") == {
        "documented_absent"
    }


# --------------------------------------------------------------------------- #
# Gate E -- absence of evidence is not evidence of absence
# --------------------------------------------------------------------------- #


async def test_a_subnet_with_no_evidence_in_the_window_produces_no_absence_drift(
    admin_engine: AsyncEngine
) -> None:
    """§0.11. THREE assigned addresses on a segment whose only sweep ran ninety
    days ago and whose only inventory observation aged out.

    Everything else is in place: the claimants are online, the addresses are
    documented, nothing answers. *If Gate E were deleted* this observes THREE
    `documented_absent` rows for a segment nobody looked at -- and the count is
    what makes it a different observation than "one finding appeared".
    """
    fx = await _fx(admin_engine, "gatee")
    for octet in (41, 42, 43):
        device_id = await _live_device(admin_engine, fx)
        await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                           subnet_id=fx.subnet_id, address=f"10.70.0.{octet}",
                           device_id=device_id)
    await _evidence(admin_engine, fx, started_at=ANCIENT)  # Gate H yes, Gate E no

    await _evaluate(admin_engine, fx.org_id)
    rows = await _drift(admin_engine, fx.org_id)
    assert [cls for _, cls in rows] == [DORMANT_CLASS] * 3
    assert "documented_absent" not in {cls for _, cls in rows}


async def test_a_failed_sweep_run_is_not_evidence_for_either_gate(
    admin_engine: AsyncEngine
) -> None:
    """Both gates read `state='succeeded'`. A run that was refused, that found no
    agent, or that failed the §6.4 scope check LOOKED AT NOTHING -- so it is not
    evidence now (Gate E) and it is not evidence ever (Gate H).

    The fixture is the `documented_absent` fixture with the run's STATE changed
    and nothing else: a live claimant, a documented address, silence. *If the
    state predicate were dropped* this observes a `documented_absent` row
    produced by a sweep that never put a packet on the wire. With it, the
    address is simply `assigned` (§3.1 row 5) and produces no row at all --
    which is also why the assertion is on the whole table rather than on one
    class.
    """
    fx = await _fx(admin_engine, "failedrun")
    device_id = await _live_device(admin_engine, fx)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=device_id)
    await _evidence(admin_engine, fx, state="no_agent")
    await _evidence(admin_engine, fx, state="refused")
    await _evidence(admin_engine, fx, state="failed")

    await _evaluate(admin_engine, fx.org_id)
    assert await _drift(admin_engine, fx.org_id) == {}


async def test_a_current_inventory_observation_satisfies_gate_e(
    admin_engine: AsyncEngine
) -> None:
    """§6.3: an UNAUTHORIZED segment still gets useful reconciliation, because
    Gate E is satisfied by inventory and not only by sweeps. That is the whole
    reason "observe-passive" is a useful mode.

    No sweep run exists here at all; the evidence is another machine on the same
    segment reporting its own address.
    """
    fx = await _fx(admin_engine, "gateeinv")
    absent_device = await _live_device(admin_engine, fx)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=absent_device)
    neighbour = await _live_device(admin_engine, fx)
    await _observe(admin_engine, fx, "10.70.0.9", source="inventory", method="nic",
                   confidence="high", device_id=neighbour)

    await _evaluate(admin_engine, fx.org_id)
    assert "documented_absent" in await _classes(admin_engine, fx.org_id, "10.70.0.42")


# --------------------------------------------------------------------------- #
# Gate H (§0.16) -- the two fixtures differ ONLY in the sweep run's existence
# --------------------------------------------------------------------------- #


async def test_a_segment_nobody_ever_looked_at_produces_no_drift_row_at_all(
    admin_engine: AsyncEngine
) -> None:
    """§0.16: `assigned` (§3.1 row 5), not `dormant`. Nothing has ever looked, so
    there is nothing to be dormant about, and telling an operator "we cannot
    currently verify this" about a segment nobody ever configured a sweep for is
    exactly the noise §0.11 exists to eliminate.

    *Without Gate H*, every address in every never-swept subnet is dormant on day
    one and the dormant tab is the whole registry: this observes one row where
    zero belong.
    """
    fx = await _fx(admin_engine, "gateh-no")
    device_id = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, device_id, status="offline",
                     last_seen_at=ANCIENT)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=device_id)

    await _evaluate(admin_engine, fx.org_id)
    assert await _drift(admin_engine, fx.org_id) == {}


async def test_the_same_segment_after_one_successful_sweep_is_dormant(
    admin_engine: AsyncEngine
) -> None:
    """THE PAIR TO THE TEST ABOVE, differing in ONE row: a succeeded sweep run
    that has since gone stale (Gate H yes, Gate E no).

    "Something looked before, nothing can look now" is dormant; "nothing has ever
    looked" is not. If either test could pass with Gate H deleted, the pair would
    be testing §3.1's ordering rather than the gate.
    """
    fx = await _fx(admin_engine, "gateh-yes")
    device_id = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, device_id, status="offline",
                     last_seen_at=ANCIENT)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=device_id)
    await _evidence(admin_engine, fx, started_at=ANCIENT)

    await _evaluate(admin_engine, fx.org_id)
    assert await _classes(admin_engine, fx.org_id, "10.70.0.42") == {DORMANT_CLASS}


async def test_an_aged_observation_satisfies_gate_h_alone(
    admin_engine: AsyncEngine
) -> None:
    """Gate H is Gate E's queries with the window dropped -- so an observation
    that has aged out of `W_obs` still proves that something once looked."""
    fx = await _fx(admin_engine, "gateh-obs")
    device_id = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, device_id, status="offline",
                     last_seen_at=ANCIENT)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=device_id)
    await _observe(admin_engine, fx, "10.70.0.9", now=ANCIENT)

    await _evaluate(admin_engine, fx.org_id)
    assert await _classes(admin_engine, fx.org_id, "10.70.0.42") == {DORMANT_CLASS}


# --------------------------------------------------------------------------- #
# The six real classes
# --------------------------------------------------------------------------- #


async def test_documented_absent_is_a_reclaim_candidate_and_changes_nothing(
    admin_engine: AsyncEngine
) -> None:
    """§4.3: `documented_absent` is a reclaim CANDIDATE, never an automatic
    reclaim. Nothing in this module ever changes `state` by itself.

    Asserting the address row is untouched is the point: a "flag only" promise
    that only checks the drift row would pass against an implementation that
    released the address.
    """
    from app.models import IpamAddress

    fx = await _fx(admin_engine, "absent")
    device_id = await _live_device(admin_engine, fx)
    address_id = await seed_address(
        admin_engine, fx.org_id, vrf_id=fx.vrf_id, subnet_id=fx.subnet_id,
        address="10.70.0.42", device_id=device_id,
    )
    await _evidence(admin_engine, fx)

    stats = await _evaluate(admin_engine, fx.org_id)
    row = (await _drift(admin_engine, fx.org_id))[("10.70.0.42", "documented_absent")]
    assert (row.severity, row.device_id, row.subnet_id) == (
        "medium", device_id, fx.subnet_id
    )
    assert row.address_id == address_id
    assert row.consecutive_detections == 1
    assert stats.detected == 1

    async with AsyncSession(admin_engine) as session:
        after = (
            await session.execute(
                select(IpamAddress).where(IpamAddress.id == address_id)
            )
        ).scalar_one()
    assert (after.state, after.device_id, after.released_at) == (
        "assigned", device_id, None
    )


async def test_present_undocumented_fires_for_an_answering_address_nobody_filed(
    admin_engine: AsyncEngine
) -> None:
    """§4.3. An address answering inside a registered `assignable` subnet with no
    `ipam_address` row is the finding that pays for the allocator's "not
    currently observed" clause."""
    fx = await _fx(admin_engine, "undoc")
    await _observe(admin_engine, fx, "10.70.0.77", mac=MAC_A, source="sweep",
                   method="arp", confidence="medium")

    await _evaluate(admin_engine, fx.org_id)
    rows = await _drift(admin_engine, fx.org_id)
    assert ("10.70.0.77", "present_undocumented") in rows
    assert rows[("10.70.0.77", "present_undocumented")].severity == "low"
    assert rows[("10.70.0.77", "present_undocumented")].address_id is None


async def test_present_undocumented_is_suppressed_inside_a_dhcp_pool_and_an_exclusion(
    admin_engine: AsyncEngine
) -> None:
    """§4.3's two carve-outs, and a THIRD address outside both so the test
    observes the difference rather than an empty answer.

    A DHCP pool hands addresses out by design and an `excluded` range is
    documented as "not ours to plan": neither is an undocumented host. *If
    either predicate were dropped* this observes three findings where one
    belongs -- and the drift page fills with the DHCP scope every morning.
    """
    fx = await _fx(admin_engine, "carve")
    await seed_range(admin_engine, fx.org_id, subnet_id=fx.subnet_id,
                     kind="dhcp_pool", start_address="10.70.0.100",
                     end_address="10.70.0.200")
    await seed_range(admin_engine, fx.org_id, subnet_id=fx.subnet_id,
                     kind="excluded", start_address="10.70.0.240",
                     end_address="10.70.0.250")
    for address in ("10.70.0.150", "10.70.0.245", "10.70.0.77"):
        await _observe(admin_engine, fx, address)

    await _evaluate(admin_engine, fx.org_id)
    found = {
        addr for (addr, cls) in await _drift(admin_engine, fx.org_id)
        if cls == "present_undocumented"
    }
    assert found == {"10.70.0.77"}


async def test_present_undocumented_needs_a_registered_assignable_subnet(
    admin_engine: AsyncEngine
) -> None:
    """§4.2: an observation inside no registered subnet is an ORPHAN, not drift
    -- nothing is wrong with the plan, the plan is ABSENT -- and a `container`
    holds no addresses (§0.2), so neither produces a finding.

    Three observations, one of which does qualify, so the assertion is a set
    difference rather than an emptiness check.
    """
    fx = await _fx(admin_engine, "orphan")
    container = await seed_subnet(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                                  prefix="172.16.0.0/12", kind="container")
    await _observe(admin_engine, fx, "10.70.0.77")
    await _observe(admin_engine, fx, "192.0.2.9", subnet_id=None)
    await _observe(admin_engine, fx, "172.16.4.9", subnet_id=container)

    await _evaluate(admin_engine, fx.org_id)
    found = {
        addr for (addr, cls) in await _drift(admin_engine, fx.org_id)
        if cls == "present_undocumented"
    }
    assert found == {"10.70.0.77"}


async def test_mac_mismatch_fires_on_a_medium_confidence_arp_observation(
    admin_engine: AsyncEngine
) -> None:
    """§4.3. The documented MAC and the observed MAC disagree, and the evidence
    is authoritative for address<->MAC on-segment."""
    fx = await _fx(admin_engine, "macmm")
    device_id = await _live_device(admin_engine, fx)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=device_id, mac=MAC_A)
    await _observe(admin_engine, fx, "10.70.0.42", mac=MAC_B, source="sweep",
                   method="arp", confidence="medium")

    await _evaluate(admin_engine, fx.org_id)
    rows = await _drift(admin_engine, fx.org_id)
    assert ("10.70.0.42", "mac_mismatch") in rows
    assert rows[("10.70.0.42", "mac_mismatch")].severity == "medium"
    assert rows[("10.70.0.42", "mac_mismatch")].detail["documented_mac"] == MAC_A
    assert MAC_B in rows[("10.70.0.42", "mac_mismatch")].detail["observed_macs"]


async def test_an_icmp_only_observation_can_never_produce_mac_mismatch(
    admin_engine: AsyncEngine
) -> None:
    """§4.3: an ICMP observation carries no MAC, so the predicate is
    UNSATISFIABLE. This is why `confidence` is a column and not a comment.

    The fixture differs from the positive above in exactly the observation's
    method: the documented MAC is still there, the address still answers. *If
    the mac/confidence predicate were dropped* this observes a `mac_mismatch`
    row derived from an observation carrying no MAC at all -- a finding an
    operator cannot act on and cannot disprove.
    """
    fx = await _fx(admin_engine, "icmponly")
    device_id = await _live_device(admin_engine, fx)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=device_id, mac=MAC_A)
    await _observe(admin_engine, fx, "10.70.0.42", mac=None, source="sweep",
                   method="icmp", confidence="low")

    await _evaluate(admin_engine, fx.org_id)
    assert "mac_mismatch" not in await _classes(admin_engine, fx.org_id, "10.70.0.42")


async def test_claimant_mismatch_fires_when_another_machine_reports_the_address(
    admin_engine: AsyncEngine
) -> None:
    """§4.3, severity HIGH: the plan says device X owns this address and device
    Y's own inventory says it has it. Only `source='inventory'` evidence can
    say this -- a sweep sees MACs, not identity (§6.4)."""
    fx = await _fx(admin_engine, "claimant")
    documented = await _live_device(admin_engine, fx)
    intruder = await _live_device(admin_engine, fx)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=documented)
    await _observe(admin_engine, fx, "10.70.0.42", source="inventory", method="nic",
                   confidence="high", device_id=intruder, mac=MAC_B)

    await _evaluate(admin_engine, fx.org_id)
    rows = await _drift(admin_engine, fx.org_id)
    assert ("10.70.0.42", "claimant_mismatch") in rows
    assert rows[("10.70.0.42", "claimant_mismatch")].severity == "high"
    # §4.3: `conflict` includes `claimant_mismatch` by definition.
    assert ("10.70.0.42", "conflict") in rows


async def test_conflict_fires_on_two_distinct_macs_at_one_address(
    admin_engine: AsyncEngine
) -> None:
    """§4.3, severity HIGH. Two current observations with different MACs at one
    address is two machines answering to one identity."""
    fx = await _fx(admin_engine, "conflict")
    await _observe(admin_engine, fx, "10.70.0.42", mac=MAC_A, source="sweep",
                   method="arp", confidence="medium")
    await _observe(admin_engine, fx, "10.70.0.42", mac=MAC_B, source="sweep",
                   method="arp", confidence="medium")

    await _evaluate(admin_engine, fx.org_id)
    rows = await _drift(admin_engine, fx.org_id)
    assert ("10.70.0.42", "conflict") in rows
    assert rows[("10.70.0.42", "conflict")].severity == "high"
    assert sorted(rows[("10.70.0.42", "conflict")].detail["macs"]) == sorted(
        [MAC_A, MAC_B]
    )


async def test_one_mac_seen_by_two_sources_is_not_a_conflict(
    admin_engine: AsyncEngine
) -> None:
    """The negative that makes the positive meaningful: `source` is in the
    observation key on purpose (§0.15), so ONE machine seen by both ARP and
    inventory is two rows and one MAC.

    *If `conflict` counted observation ROWS rather than distinct MACs*, every
    address the fleet reports twice would be a high-severity finding.
    """
    fx = await _fx(admin_engine, "onemac")
    device_id = await _live_device(admin_engine, fx)
    await _observe(admin_engine, fx, "10.70.0.42", mac=MAC_A, source="sweep",
                   method="arp", confidence="medium")
    await _observe(admin_engine, fx, "10.70.0.42", mac=MAC_A, source="inventory",
                   method="nic", confidence="high", device_id=device_id)

    await _evaluate(admin_engine, fx.org_id)
    assert "conflict" not in await _classes(admin_engine, fx.org_id, "10.70.0.42")


# --------------------------------------------------------------------------- #
# `conflict`'s THIRD disjunct -- the cross-VRF clause, and the half of it that
# is the whole clause. §4.3 reads "≥2 `ipam_address` rows in different VRFs
# claim A **and those VRFs share a swept segment**". Every other fixture in this
# file has exactly ONE VRF, which is how a version that dropped the second half
# shipped: `10.0.0.0/24` in two unrelated routing domains is the ORDINARY reason
# VRFs exist, and reading it as a conflict puts a high-severity finding on a
# switched-off printer and -- via §3.2's last predicate -- blocks assignment at
# that address in BOTH VRFs.
# --------------------------------------------------------------------------- #


async def _two_vrfs(
    engine: AsyncEngine, tag: str, *, vlan_a: UUID | None, vlan_b: UUID | None
) -> tuple[_Fx, _Fx]:
    """One org, two VRFs, the same `10.0.0.0/24` in each, `.5` claimed in both.

    Zero observations, zero agents, every device off: the ONLY evidence in play
    is the two documented rows and one succeeded sweep of each segment ninety
    days ago (Gate H yes, Gate E no). The two fixtures below differ in exactly
    one value -- whether the two subnets carry the SAME `vlan_id`, i.e. whether
    they are the same wire.
    """
    org = await seed_org(engine, tag)
    left = await seed_vrf(engine, org.org_id, name="global")
    right = await seed_vrf(engine, org.org_id, name="customer-a")
    fx_left = _Fx(
        org,
        left,
        await seed_subnet(
            engine, org.org_id, vrf_id=left, prefix="10.0.0.0/24", vlan_id=vlan_a
        ),
    )
    fx_right = _Fx(
        org,
        right,
        await seed_subnet(
            engine, org.org_id, vrf_id=right, prefix="10.0.0.0/24", vlan_id=vlan_b
        ),
    )
    for fx in (fx_left, fx_right):
        await seed_address(
            engine, org.org_id, vrf_id=fx.vrf_id, subnet_id=fx.subnet_id,
            address="10.0.0.5", state="assigned", asset_label="printer-a",
        )
        await seed_sweep_run(
            engine, org.org_id, subnet_id=fx.subnet_id, state="succeeded",
            started_at=ANCIENT,
        )
    return fx_left, fx_right


async def test_two_unrelated_vrfs_claiming_one_address_are_not_a_conflict(
    admin_engine: AsyncEngine
) -> None:
    """§4.3's cross-VRF clause needs a SHARED SEGMENT, and these two share
    nothing but a number.

    `10.0.0.5` is documented in `global` and in `customer-a`, on two different
    wires. There are no observations, no agents and no live devices -- there is
    no evidence of any kind that anything is wrong, because nothing is.

    *If the clause were `len(vrfs) > 1`* this observes two high-severity
    `conflict` rows carrying `{'macs': [], 'claimant_mismatch': False}` -- a
    finding with an empty evidence payload, on a switched-off printer, that
    publishes `ipam.drift_detected` on the second nightly pass and locks
    `10.0.0.5` out of assignment in both VRFs until a correct row is deleted.
    """
    left, right = await _two_vrfs(admin_engine, "xvrf", vlan_a=None, vlan_b=None)

    await _evaluate(admin_engine, left.org_id)
    rows = await _drift(admin_engine, left.org_id)
    conflicts = [key for key in rows if key[1] == "conflict"]
    assert conflicts == [], [
        (key, rows[key].detail) for key in conflicts
    ]
    # The gates still speak: Gate H holds and Gate E does not, so the two
    # claims are DORMANT. A pass that produced nothing at all would pass the
    # assertion above for the wrong reason.
    assert ("10.0.0.5", DORMANT_CLASS) in rows


async def test_two_vrfs_on_one_swept_vlan_claiming_one_address_do_conflict(
    admin_engine: AsyncEngine
) -> None:
    """The positive that makes the negative meaningful, and it differs from it
    in ONE value: both subnets sit on the same `ipam_vlan` row.

    Two routing domains presenting the same address on the same broadcast
    domain is a real duplicate-IP defect, and it is the one §4.3's third
    disjunct is for. The segment has been swept (Gate H), so the module has
    actually looked at that wire.
    """
    org = await seed_org(admin_engine, "xvrfy")
    vlan_id = await seed_vlan(admin_engine, org.org_id, vid=10)
    left = await seed_vrf(admin_engine, org.org_id, name="global")
    right = await seed_vrf(admin_engine, org.org_id, name="customer-a")
    fixtures = []
    for vrf_id in (left, right):
        subnet_id = await seed_subnet(
            admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.0.0.0/24",
            vlan_id=vlan_id,
        )
        await seed_address(
            admin_engine, org.org_id, vrf_id=vrf_id, subnet_id=subnet_id,
            address="10.0.0.5", state="assigned", asset_label="printer-a",
        )
        await seed_sweep_run(
            admin_engine, org.org_id, subnet_id=subnet_id, state="succeeded",
            started_at=ANCIENT,
        )
        fixtures.append(_Fx(org, vrf_id, subnet_id))

    await _evaluate(admin_engine, org.org_id)
    rows = await _drift(admin_engine, org.org_id)
    conflicts = {key: row for key, row in rows.items() if key[1] == "conflict"}
    assert len(conflicts) == 1, conflicts
    row = next(iter(conflicts.values()))
    assert (str(row.address), row.severity) == ("10.0.0.5", "high")
    assert row.detail["vrf_count"] == 2
    assert row.detail["macs"] == []
    assert row.detail["claimant_mismatch"] is False


async def test_a_shared_vlan_nobody_ever_looked_at_is_not_a_conflict(
    admin_engine: AsyncEngine
) -> None:
    """"Share a **swept** segment" -- the adjective is load-bearing.

    Identical to the positive above except that no sweep ever succeeded and no
    observation was ever recorded inside either subnet, so Gate H is false for
    the shared VLAN. Two rows in a table that nothing has ever verified are a
    documentation question, not a high-severity duplicate-address finding.

    *If the sweptness requirement were dropped* this observes a `conflict` on
    day one, in every org that registered the same VLAN in two VRFs before
    turning sweeps on.
    """
    org = await seed_org(admin_engine, "xvrfn")
    vlan_id = await seed_vlan(admin_engine, org.org_id, vid=10)
    for name in ("global", "customer-a"):
        vrf_id = await seed_vrf(admin_engine, org.org_id, name=name)
        subnet_id = await seed_subnet(
            admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.0.0.0/24",
            vlan_id=vlan_id,
        )
        await seed_address(
            admin_engine, org.org_id, vrf_id=vrf_id, subnet_id=subnet_id,
            address="10.0.0.5", state="assigned", asset_label="printer-a",
        )

    await _evaluate(admin_engine, org.org_id)
    rows = await _drift(admin_engine, org.org_id)
    assert [key for key in rows if key[1] == "conflict"] == []
    # Nor dormant: Gate H is what separates "nothing can look now" from
    # "nothing ever looked", and nothing ever looked here.
    assert [key for key in rows if key[1] == DORMANT_CLASS] == []


async def test_device_moved_collapses_two_findings_into_one_action(
    admin_engine: AsyncEngine
) -> None:
    """§4.3, and the reason the class exists.

    A laptop's assignment is at `.42` and its current inventory observation is at
    `.57`. Left apart, the operator sees a `documented_absent` at `.42` and a
    `present_undocumented` at `.57` and has to notice they are the same machine.

    *Without the class* this observes exactly those two unrelated findings --
    which is why the assertion names all three classes rather than only the one
    that should appear.
    """
    fx = await _fx(admin_engine, "moved")
    laptop = await _live_device(admin_engine, fx)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=laptop)
    await _observe(admin_engine, fx, "10.70.0.57", source="inventory", method="nic",
                   confidence="high", device_id=laptop)
    await _evidence(admin_engine, fx)

    await _evaluate(admin_engine, fx.org_id)
    rows = await _drift(admin_engine, fx.org_id)
    assert ("10.70.0.42", "device_moved") in rows
    assert rows[("10.70.0.42", "device_moved")].detail["observed_at"] == "10.70.0.57"
    assert ("10.70.0.42", "documented_absent") not in rows
    assert ("10.70.0.57", "present_undocumented") not in rows


# --------------------------------------------------------------------------- #
# The two-detection rule (§0.8) and the stable identity
# --------------------------------------------------------------------------- #


async def _absent_fixture(admin_engine: AsyncEngine, tag: str) -> _Fx:
    fx = await _fx(admin_engine, tag)
    device_id = await _live_device(admin_engine, fx)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=device_id)
    await _evidence(admin_engine, fx)
    return fx


async def test_one_detection_publishes_nothing_and_the_second_publishes_once(
    admin_engine: AsyncEngine
) -> None:
    """§0.8: one sweep that ran while a switch was rebooting is not news.

    Counted, not asserted absent: the count goes 0 -> 1 -> 1, so *if the event
    were published on the first detection* this observes 2 `domain_events` rows
    where 1 is expected, and *if it were published every pass* it observes 3.
    """
    fx = await _absent_fixture(admin_engine, "twice")

    first = await _evaluate(admin_engine, fx.org_id)
    assert first.published == 0
    assert await _events(admin_engine, fx.org_id) == []

    second = await _evaluate(admin_engine, fx.org_id)
    assert second.published == 1
    events = await _events(admin_engine, fx.org_id)
    assert len(events) == 1
    assert events[0].payload["drift_class"] == "documented_absent"
    assert events[0].payload["address"] == "10.70.0.42"
    assert events[0].payload["subnet_prefix"] == "10.70.0.0/24"

    third = await _evaluate(admin_engine, fx.org_id)
    assert third.published == 0
    assert len(await _events(admin_engine, fx.org_id)) == 1


async def test_acknowledgement_and_first_detected_at_survive_recomputation(
    admin_engine: AsyncEngine
) -> None:
    """§0.8's stable identity is what makes the list openable twice.

    *If recomputation deleted and re-inserted* instead of upserting, this
    observes `acknowledged_at IS NULL` and a `first_detected_at` that has moved
    -- and every acknowledgement an operator made yesterday is gone this
    morning.
    """
    fx = await _absent_fixture(admin_engine, "ack")
    await _evaluate(admin_engine, fx.org_id)
    row = (await _drift(admin_engine, fx.org_id))[("10.70.0.42", "documented_absent")]
    original_id, original_first = row.id, row.first_detected_at

    async with AsyncSession(admin_engine) as session:
        await acknowledge_drift(
            session, fx.org_id, actor_id=fx.user_id, drift_id=row.id,
            note="known: the printer is being replaced",
        )
        await session.commit()

    await _evaluate(admin_engine, fx.org_id, now=NOW + timedelta(days=1))
    after = (await _drift(admin_engine, fx.org_id))[
        ("10.70.0.42", "documented_absent")
    ]
    assert after.id == original_id
    assert after.first_detected_at == original_first
    assert after.acknowledged_by == fx.user_id
    assert after.acknowledged_at is not None
    assert after.note == "known: the printer is being replaced"
    assert after.consecutive_detections == 2


async def test_an_acknowledged_item_reaching_its_second_detection_publishes_nothing(
    admin_engine: AsyncEngine
) -> None:
    """"I have seen this and it is fine" must silence the event, not merely the
    row -- otherwise acknowledging changes what the page shows and nothing an
    operator actually receives."""
    fx = await _absent_fixture(admin_engine, "ackquiet")
    await _evaluate(admin_engine, fx.org_id)
    row = (await _drift(admin_engine, fx.org_id))[("10.70.0.42", "documented_absent")]
    async with AsyncSession(admin_engine) as session:
        await acknowledge_drift(session, fx.org_id, actor_id=fx.user_id,
                                drift_id=row.id, note="known")
        await session.commit()

    stats = await _evaluate(admin_engine, fx.org_id)
    assert stats.published == 0
    assert await _events(admin_engine, fx.org_id) == []


async def test_suppression_is_compared_against_the_evaluation_time(
    admin_engine: AsyncEngine
) -> None:
    """Suppression is TIME-BOXED, and the boundary is asserted from both sides.

    Two orgs with identical fixtures, both suppressed with a note, differing
    ONLY in whether the window has expired when the second detection lands: org
    A is muted until three days out and evaluated now; org B was muted for an
    hour and is evaluated two hours later.

    *If `suppressed_until` were treated as a permanent mute* org B observes 0
    events; *if it were ignored* org A observes 1. Asserting only one side
    cannot tell those apart.
    """
    inside = await _absent_fixture(admin_engine, "suppress-in")
    expired = await _absent_fixture(admin_engine, "suppress-out")
    for fx, until in ((inside, NOW + timedelta(days=3)), (expired, NOW + timedelta(hours=1))):
        await _evaluate(admin_engine, fx.org_id)
        row = (await _drift(admin_engine, fx.org_id))[
            ("10.70.0.42", "documented_absent")
        ]
        async with AsyncSession(admin_engine) as session:
            await suppress_drift(
                session, fx.org_id, actor_id=fx.user_id, drift_id=row.id,
                until=until, note="change window CHG-41", now=NOW,
            )
            await session.commit()

    assert (await _evaluate(admin_engine, inside.org_id)).published == 0
    assert await _events(admin_engine, inside.org_id) == []

    later = await _evaluate(
        admin_engine, expired.org_id, now=NOW + timedelta(hours=2)
    )
    assert later.published == 1
    assert len(await _events(admin_engine, expired.org_id)) == 1


async def test_a_resolved_item_is_stamped_not_deleted(
    admin_engine: AsyncEngine
) -> None:
    """§0.8: persisting resolution makes "we closed 40 address-plan defects this
    quarter" an evidence export rather than an anecdote.

    *If resolution deleted the row* this observes 0 rows instead of 1 carrying
    `resolved_at` -- and the acknowledgement history goes with it.
    """
    fx = await _absent_fixture(admin_engine, "resolve")
    await _evaluate(admin_engine, fx.org_id)
    await _evaluate(admin_engine, fx.org_id)

    await _observe(admin_engine, fx, "10.70.0.42")  # it answers again
    stats = await _evaluate(admin_engine, fx.org_id, now=NOW + timedelta(minutes=1))
    assert stats.resolved == 1

    row = (await _drift(admin_engine, fx.org_id))[("10.70.0.42", "documented_absent")]
    assert row.resolved_at is not None
    assert row.consecutive_detections == 0
    assert row.first_detected_at is not None


async def test_a_re_detected_item_clears_its_resolution_and_counts_again(
    admin_engine: AsyncEngine
) -> None:
    """The other side of resolution: an item that comes back starts its
    two-detection count from zero rather than firing an event immediately off a
    stale counter."""
    fx = await _absent_fixture(admin_engine, "redetect")
    await _evaluate(admin_engine, fx.org_id)
    await _observe(admin_engine, fx, "10.70.0.42")
    await _evaluate(admin_engine, fx.org_id)
    assert (await _drift(admin_engine, fx.org_id))[
        ("10.70.0.42", "documented_absent")
    ].resolved_at is not None

    async with AsyncSession(admin_engine) as session:
        from app.models import IpamObservation

        await session.execute(
            IpamObservation.__table__.delete().where(
                IpamObservation.org_id == fx.org_id
            )
        )
        await session.commit()

    stats = await _evaluate(admin_engine, fx.org_id)
    row = (await _drift(admin_engine, fx.org_id))[("10.70.0.42", "documented_absent")]
    assert row.resolved_at is None
    assert row.consecutive_detections == 1
    assert stats.published == 0


# --------------------------------------------------------------------------- #
# `dormant` is not a drift class
# --------------------------------------------------------------------------- #


async def test_dormant_is_absent_from_the_drift_list_and_the_counts(
    admin_engine: AsyncEngine
) -> None:
    """§4.3: `dormant` is the bucket that makes the drift list READABLE. It is
    excluded from counts, from exports of "drift", and never published as an
    event -- "37 addresses we cannot currently verify" is worth knowing and is
    not the same information as "37 addresses are wrong".

    The fixture holds one of each so the assertion is a partition, not an
    emptiness check.
    """
    fx = await _fx(admin_engine, "dormlist")
    await _evidence(admin_engine, fx)
    live = await _live_device(admin_engine, fx)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=live)
    asleep = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, asleep, status="offline",
                     last_seen_at=ANCIENT)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.43",
                       device_id=asleep)

    stats = await _evaluate(admin_engine, fx.org_id)
    assert (stats.detected, stats.dormant) == (1, 1)

    async with AsyncSession(admin_engine) as session:
        listed = await list_drift(session, fx.org_id)
        dormant = await list_drift(session, fx.org_id, drift_class=DORMANT_CLASS)
    assert [str(r.address) for r in listed] == ["10.70.0.42"]
    assert [str(r.address) for r in dormant] == ["10.70.0.43"]
    assert DORMANT_CLASS not in DRIFT_CLASSES


async def test_a_dormant_item_never_publishes_an_event(
    admin_engine: AsyncEngine
) -> None:
    """Two passes, so the item reaches the count at which a real drift class
    would speak. *If `dormant` were treated as a drift class* this observes one
    `ipam.drift_detected` for a laptop that is merely switched off."""
    fx = await _fx(admin_engine, "dormquiet")
    await _evidence(admin_engine, fx, started_at=ANCIENT)
    device_id = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, device_id, status="offline",
                     last_seen_at=ANCIENT)
    await seed_address(admin_engine, fx.org_id, vrf_id=fx.vrf_id,
                       subnet_id=fx.subnet_id, address="10.70.0.42",
                       device_id=device_id)

    await _evaluate(admin_engine, fx.org_id)
    stats = await _evaluate(admin_engine, fx.org_id)
    assert stats.published == 0
    assert await _events(admin_engine, fx.org_id) == []
    assert (await _drift(admin_engine, fx.org_id))[
        ("10.70.0.42", DORMANT_CLASS)
    ].consecutive_detections == 2


# --------------------------------------------------------------------------- #
# Acknowledge / suppress: RBAC-gated at the API, AUDITED here
# --------------------------------------------------------------------------- #


async def test_acknowledge_and_suppress_are_audited_with_their_notes(
    admin_engine: AsyncEngine
) -> None:
    """INVARIANT #4. §11.3 lists both actions with the note, because "who decided
    this finding was fine, and why" is the question an auditor asks."""
    fx = await _absent_fixture(admin_engine, "auditack")
    await _evaluate(admin_engine, fx.org_id)
    rows = await _drift(admin_engine, fx.org_id)
    drift_id = rows[("10.70.0.42", "documented_absent")].id

    async with AsyncSession(admin_engine) as session:
        await acknowledge_drift(session, fx.org_id, actor_id=fx.user_id,
                                drift_id=drift_id, note="printer swap")
        await suppress_drift(session, fx.org_id, actor_id=fx.user_id,
                             drift_id=drift_id, until=NOW + timedelta(days=2),
                             note="CHG-41", now=NOW)
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == fx.org_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
    by_action = {e.action: e for e in events}
    assert by_action["ipam.drift_acknowledged"].target_type == "ipam_drift"
    assert by_action["ipam.drift_acknowledged"].meta["note"] == "printer swap"
    assert by_action["ipam.drift_suppressed"].meta["note"] == "CHG-41"
    assert by_action["ipam.drift_suppressed"].meta["drift_class"] == (
        "documented_absent"
    )


async def test_suppression_requires_a_note(admin_engine: AsyncEngine) -> None:
    """§4.4: "suppression sets `suppressed_until` with a REQUIRED note". Muting a
    finding without saying why is how a drift list becomes a list of things
    somebody once silenced.

    The stored row is asserted first: a mutation test that only observed
    `DID NOT RAISE` cannot tell a refused suppression from an accepted one.
    """
    import pytest

    from app.modules.ipam.errors import IpamValidationError

    fx = await _absent_fixture(admin_engine, "note")
    await _evaluate(admin_engine, fx.org_id)
    drift_id = (await _drift(admin_engine, fx.org_id))[
        ("10.70.0.42", "documented_absent")
    ].id

    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamValidationError):
            await suppress_drift(session, fx.org_id, actor_id=fx.user_id,
                                 drift_id=drift_id, until=NOW + timedelta(days=1),
                                 note="   ", now=NOW)
        await session.rollback()
    assert (await _drift(admin_engine, fx.org_id))[
        ("10.70.0.42", "documented_absent")
    ].suppressed_until is None


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


async def test_one_orgs_reconciliation_touches_nothing_in_another(
    admin_engine: AsyncEngine
) -> None:
    """PINNED ON THE ADMIN ENGINE (invariant #5): through `org_session` RLS
    rejects first, so this passes with every `org_id` predicate in `reconcile.py`
    deleted. Here the predicates are the only thing scoping the pass.

    Org B is given a drift-producing fixture of its own, so "org A wrote nothing
    for org B" is observable as a COUNT that stays at zero rather than as an
    absence that would hold for an implementation that wrote nothing at all.
    """
    a = await _absent_fixture(admin_engine, "org-a")
    b = await _absent_fixture(admin_engine, "org-b")

    stats = await _evaluate(admin_engine, a.org_id)
    assert stats.detected == 1
    async with AsyncSession(admin_engine) as session:
        b_rows = (
            await session.execute(
                select(func.count()).select_from(IpamDrift).where(
                    IpamDrift.org_id == b.org_id
                )
            )
        ).scalar_one()
    assert b_rows == 0

    await _evaluate(admin_engine, b.org_id)
    assert len(await _drift(admin_engine, b.org_id)) == 1


async def test_a_drift_id_from_another_org_is_not_found(
    admin_engine: AsyncEngine
) -> None:
    """404, never 403: a 403 confirms the id exists. Pinned on the admin engine
    for the same reason as above."""
    import pytest

    from app.modules.ipam.errors import IpamNotFoundError

    a = await _absent_fixture(admin_engine, "xorg-a")
    b = await _absent_fixture(admin_engine, "xorg-b")
    await _evaluate(admin_engine, a.org_id)
    victim = (await _drift(admin_engine, a.org_id))[
        ("10.70.0.42", "documented_absent")
    ].id

    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamNotFoundError):
            await acknowledge_drift(session, b.org_id, actor_id=b.user_id,
                                    drift_id=victim, note="not mine")


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


def test_the_reconcile_interval_is_daily() -> None:
    """§4.4. A project constant rather than a literal in `app/worker.py`: a test
    carrying its own copy of 86400.0 passes when the constant changes and the
    beat entry does not."""
    assert RECONCILE_INTERVAL_SECONDS == 86400.0


def test_the_six_drift_classes_are_the_spec_table() -> None:
    """§4.3's classes, pinned so adding one is a deliberate edit. `dormant` is
    NOT among them -- that is the whole distinction the module rests on."""
    assert DRIFT_CLASSES == (
        "documented_absent",
        "present_undocumented",
        "mac_mismatch",
        "claimant_mismatch",
        "device_moved",
        "conflict",
    )
    assert DORMANT_CLASS == "dormant"
