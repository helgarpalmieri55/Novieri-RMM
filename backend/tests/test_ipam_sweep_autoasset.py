"""A sweep records what it finds as assets (asset-inventory spec §3, IPAM §6.6).

**THE OPERATOR RULED ON 2026-08-26 AND THIS IS THE OTHER SIDE OF THAT RULING.**
Until that date `net.sweep` wrote `ipam_observation` rows and stopped: an
address became a `devices` row only when an operator selected it on the
Discovered screen. `test_asset_promote.py`'s first test asserted exactly that,
and it now asserts the inverse. The argument the old design was making -- a
ping sweep answers for printers, phones, a colleague's laptop on the wrong VLAN
and the coffee machine, and rows built from that look identical to rows a human
entered -- was put to the operator with its consequences and the operator chose
to record everything.

**SO THE ARGUMENT IS ANSWERED BY `devices.provenance`, NOT BY REFUSING TO
WRITE.** Every row now says how it came to exist -- `agent`, `operator`,
`promoted`, `sweep` -- so "which of these did a person vouch for?" is a WHERE
clause rather than an unanswerable question. That column is what these tests
pin hardest: an auto-created row that claimed `promoted`, or that could not be
told apart from a typed-in one, would give the operator the inventory the old
design warned about.

**WHAT THESE TESTS DELIBERATELY EXERCISE THROUGH THE REAL PATH.** Observations
are written by completing a signed `net.sweep` job through `JobEngine.complete`,
never by inserting rows. Inserting an observation is not a sweep, and a test
that did so would pass with the whole auto-creation path deleted.
"""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.models import AuditEvent, Device, JobStatus
from tests.client_scope_support import drop_client_keys
from tests.ipam_fixtures import (
    complete_sweep,
    dispatch_sweep,
    seed_agent,
    seed_device,
    seed_org,
    seed_subnet,
    seed_vrf,
    sweep_document,
    sweep_host,
    upsert_observation_row,
)


async def _sited_subnet(engine: AsyncEngine, org_id: UUID, prefix: str) -> Any:
    """A subnet of the internal client at its Principal site (both carry the
    organisation's id), loaded: what `auto_create_from_sweep` files under."""
    from app.modules.ipam.models import IpamSubnet

    vrf_id = await seed_vrf(engine, org_id)
    subnet_id = await seed_subnet(engine, org_id, vrf_id=vrf_id, prefix=prefix, site_id=org_id)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await session.get(IpamSubnet, subnet_id)


async def _devices(engine: AsyncEngine, org_id: UUID) -> list[Device]:
    """Every asset row in ONE org, address order.

    Scoped, and on the admin engine: an unscoped count passes alone and reports
    another test's fixture when the file runs, which is a test defect shaped
    exactly like a tenancy bug.
    """
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(Device)
                    .where(Device.org_id == org_id)
                    .order_by(Device.primary_ip.is_(None), Device.primary_ip)
                )
            )
            .scalars()
            .all()
        )


async def _found(engine: AsyncEngine, org_id: UUID) -> list[Device]:
    """The rows a sweep created, i.e. the ones nobody vouched for."""
    return [d for d in await _devices(engine, org_id) if d.provenance == "sweep"]


THREE = [
    sweep_host("10.70.0.10", mac="a4:bb:6d:11:22:33", hostname="printer-2"),
    sweep_host("10.70.0.11"),
    sweep_host("10.70.0.12", mac="A4-BB-6D-44-55-66"),
]


async def _swept(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    tag: str,
    hosts: list[dict[str, Any]] | None = None,
) -> tuple[Any, Any]:
    """An org whose one agent has just completed a sweep of its own LAN."""
    org = await seed_org(admin_engine, tag)
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await seed_agent(admin_engine, org.org_id, org.device_id)
    fx = await dispatch_sweep(
        admin_engine, org.org_id, vrf_id=vrf_id, device_id=org.device_id
    )
    job = await complete_sweep(
        app_engine,
        admin_engine,
        fx,
        sweep_document(fx, THREE if hosts is None else hosts),
        tmp_path,
    )
    assert job.status is JobStatus.succeeded, job.result
    return org, fx


# --------------------------------------------------------------------------- #
# The ruling itself
# --------------------------------------------------------------------------- #


async def test_a_sweep_records_every_host_it_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """THE OPERATOR'S DECISION, stated as a value: three hosts answered, three
    asset rows exist, and nobody clicked anything."""
    org, _ = await _swept(admin_engine, app_engine, tmp_path, "auto")

    found = await _found(admin_engine, org.org_id)
    assert [str(d.primary_ip) for d in found] == [
        "10.70.0.10",
        "10.70.0.11",
        "10.70.0.12",
    ]
    # `source` still says HOW the row arrived; `provenance` says who, if
    # anyone, stood behind it. Both, because they answer different questions.
    assert [d.source for d in found] == ["discovery"] * 3
    assert [d.provenance for d in found] == ["sweep"] * 3


async def test_a_swept_row_is_not_dressed_up_as_a_promoted_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """THE FALSIFIER FOR THE WHOLE PROVENANCE DESIGN.

    If an automatically created row is indistinguishable from one a human
    promoted, the objection the old design was making stands in full: the
    coffee machine and the domain controller become the same kind of row for
    ever. The inventory must be able to answer "which of these did a person
    vouch for?" from the data, not from memory.
    """
    org, _ = await _swept(admin_engine, app_engine, tmp_path, "vouch")
    typed = await seed_device(
        admin_engine, org.org_id, source="manual", provenance="operator",
        primary_ip="10.70.0.99",
    )

    async with AsyncSession(admin_engine) as session:
        vouched = (
            await session.execute(
                select(func.count())
                .select_from(Device)
                .where(Device.org_id == org.org_id, Device.provenance != "sweep")
            )
        ).scalar_one()
        unvouched = (
            await session.execute(
                select(func.count())
                .select_from(Device)
                .where(Device.org_id == org.org_id, Device.provenance == "sweep")
            )
        ).scalar_one()
    assert unvouched == 3
    # The seeded agent's device and the typed-in one.
    assert vouched == 2
    assert typed is not None


async def test_the_asset_type_a_sweep_assigns_claims_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """An echo reply does not say `computer`. `other` is the vocabulary's only
    honest answer, and a wrong default here is a lie repeated on every screen
    that groups by type."""
    org, _ = await _swept(admin_engine, app_engine, tmp_path, "type")
    assert [d.asset_type for d in await _found(admin_engine, org.org_id)] == ["other"] * 3
    assert [d.lifecycle for d in await _found(admin_engine, org.org_id)] == [
        "in_service"
    ] * 3


# --------------------------------------------------------------------------- #
# What the row is called
# --------------------------------------------------------------------------- #


async def test_a_row_is_named_by_reverse_dns_or_by_nothing_at_all(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """NO INVENTED HOSTNAME. A sweep frequently has no name for an address, and
    a fabricated one (`unknown-10.70.0.11`, `device-3`) is a string that looks
    exactly like a real hostname to every screen, export and match rule that
    reads the column afterwards. `hostname` stays NULL and the console falls
    back to the address (`DevicesPage.assetLabel`)."""
    org, _ = await _swept(admin_engine, app_engine, tmp_path, "name")
    by_address = {str(d.primary_ip): d for d in await _found(admin_engine, org.org_id)}

    assert by_address["10.70.0.10"].hostname == "printer-2"
    assert by_address["10.70.0.11"].hostname is None
    assert by_address["10.70.0.12"].hostname is None


# --------------------------------------------------------------------------- #
# Identity: what stops the second sweep doubling the inventory
# --------------------------------------------------------------------------- #


async def test_a_second_sweep_of_the_same_segment_adds_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The sweep runs hourly. Without an identity key this is the defect that
    fills the inventory with copies inside a day."""
    org, fx = await _swept(admin_engine, app_engine, tmp_path, "again")
    second = await dispatch_sweep(
        admin_engine,
        org.org_id,
        vrf_id=fx.vrf_id,
        device_id=org.device_id,
        subnet_id=fx.subnet_id,
    )
    job = await complete_sweep(
        app_engine, admin_engine, second, sweep_document(second, THREE), tmp_path
    )
    assert job.status is JobStatus.succeeded, job.result
    assert len(await _found(admin_engine, org.org_id)) == 3


async def test_a_new_dhcp_lease_moves_the_row_it_already_has(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """THE MAC IS THE IDENTITY KEY WHERE THERE IS ONE, and the address is not.

    A laptop that takes a new lease overnight is the same machine. Keyed on
    address it becomes a second row every time DHCP moves it; keyed on MAC it
    is one row whose address is corrected. The correction is applied ONLY to a
    row the platform created itself -- a human's or an agent's row is never
    overwritten by a ping.
    """
    org, fx = await _swept(
        admin_engine, app_engine, tmp_path, "lease",
        [sweep_host("10.70.0.40", mac="a4:bb:6d:99:88:77")],
    )
    [before] = await _found(admin_engine, org.org_id)
    assert str(before.primary_ip) == "10.70.0.40"

    second = await dispatch_sweep(
        admin_engine, org.org_id, vrf_id=fx.vrf_id, device_id=org.device_id,
        subnet_id=fx.subnet_id,
    )
    await complete_sweep(
        app_engine,
        admin_engine,
        second,
        sweep_document(second, [sweep_host("10.70.0.41", mac="a4:bb:6d:99:88:77")]),
        tmp_path,
    )

    found = await _found(admin_engine, org.org_id)
    assert len(found) == 1, [str(d.primary_ip) for d in found]
    assert found[0].id == before.id
    assert str(found[0].primary_ip) == "10.70.0.41"


async def test_a_row_a_person_owns_is_never_readdressed_by_a_ping(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The other half of the rule above, and the one with teeth: an operator's
    record moving because something answered at another address would be the
    platform editing a human's data on ICMP evidence."""
    org = await seed_org(admin_engine, "owned")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    typed = await seed_device(
        admin_engine,
        org.org_id,
        source="manual",
        provenance="operator",
        primary_ip="10.70.0.50",
        nics={"items": [{"identifier": "a4:bb:6d:12:12:12", "mac": "a4:bb:6d:12:12:12"}]},
    )
    fx = await dispatch_sweep(
        admin_engine, org.org_id, vrf_id=vrf_id, device_id=org.device_id
    )
    await complete_sweep(
        app_engine,
        admin_engine,
        fx,
        sweep_document(fx, [sweep_host("10.70.0.51", mac="a4:bb:6d:12:12:12")]),
        tmp_path,
    )

    async with AsyncSession(admin_engine) as session:
        row = (await session.execute(select(Device).where(Device.id == typed))).scalar_one()
    assert str(row.primary_ip) == "10.70.0.50"
    assert row.provenance == "operator"
    # And no second row was created for it either: the MAC said it is known.
    assert await _found(admin_engine, org.org_id) == []


async def test_a_managed_machine_answering_a_ping_gets_no_second_row(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """THE ICMP-ONLY CASE, which the MAC check cannot cover.

    A managed machine reports its own NICs, and those observations carry a
    `device_id` (a sweep's never do -- IPAM §6.4). That is the only evidence
    tying a bare address with no MAC to a machine the platform already runs, so
    it is consulted before creating anything.
    """
    org = await seed_org(admin_engine, "managed")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await seed_agent(admin_engine, org.org_id, org.device_id)
    await upsert_observation_row(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        address="10.70.0.60",
        source="inventory",
        method="nic",
        confidence="high",
        device_id=org.device_id,
    )
    fx = await dispatch_sweep(
        admin_engine, org.org_id, vrf_id=vrf_id, device_id=org.device_id
    )
    await complete_sweep(
        app_engine,
        admin_engine,
        fx,
        sweep_document(fx, [sweep_host("10.70.0.60"), sweep_host("10.70.0.61")]),
        tmp_path,
    )

    found = await _found(admin_engine, org.org_id)
    assert [str(d.primary_ip) for d in found] == ["10.70.0.61"]


async def test_an_address_an_operator_typed_in_is_not_recorded_again(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A switch somebody entered by hand answers every sweep for ever."""
    org = await seed_org(admin_engine, "typed")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    await seed_device(
        admin_engine, org.org_id, source="manual", provenance="operator",
        primary_ip="10.70.0.70", hostname="core-sw",
    )
    fx = await dispatch_sweep(
        admin_engine, org.org_id, vrf_id=vrf_id, device_id=org.device_id
    )
    await complete_sweep(
        app_engine,
        admin_engine,
        fx,
        sweep_document(fx, [sweep_host("10.70.0.70", hostname="core-sw")]),
        tmp_path,
    )
    assert await _found(admin_engine, org.org_id) == []


async def test_an_arp_sighting_keeps_the_mac_that_lets_enrolment_join_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """WHAT HAPPENS WHEN A LATER INVENTORY REPORT CONTRADICTS THE GUESS.

    It is CORRECTED, not duplicated -- but only because the row carries the MAC
    the matcher keys on. `resolve_enrolment_target` links an enrolling agent to
    exactly one agentless row on a physical MAC (spec §4 rule 2), so the agent
    lands on the record the sweep created and overwrites its guesses with
    reported facts. Without the MAC on the row, enrolment would create a second
    record for a machine the platform already had.
    """
    from app.core.tenancy import org_session
    from app.services.matching import resolve_enrolment_target

    org, _ = await _swept(
        admin_engine, app_engine, tmp_path, "join",
        [sweep_host("10.70.0.80", mac="00:1b:21:aa:bb:cc")],
    )
    [row] = await _found(admin_engine, org.org_id)
    assert row.nics == {
        "items": [{"identifier": "00:1b:21:aa:bb:cc", "mac": "00:1b:21:aa:bb:cc"}]
    }

    class _Facts:
        hostname = "reception-pc"
        macs = ["00:1B:21:AA:BB:CC"]

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        outcome = await resolve_enrolment_target(session, org.org_id, _Facts())
    assert outcome.device_id == row.id
    assert outcome.matched_on == "mac"


# --------------------------------------------------------------------------- #
# Audit (invariant 4) and tenancy (invariant 5)
# --------------------------------------------------------------------------- #


async def test_every_automatic_creation_is_audited_to_the_system(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A bulk automatic creation is still a privileged action, and the actor is
    the SYSTEM -- not the user who scheduled the sweep, and not the agent that
    ran it. Attributing this to a person would put a human's name against rows
    no human chose, which is the one thing the audit log must never say.

    The evidence goes in the event, not just the outcome: `confidence` records
    whether ARP or only ICMP saw this, which is what an auditor needs to judge
    what the row is worth.
    """
    org, fx = await _swept(admin_engine, app_engine, tmp_path, "audit")

    async with AsyncSession(admin_engine) as session:
        rows = list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.org_id == org.org_id,
                        AuditEvent.action == "asset.auto_created",
                    )
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 3, [r.action for r in rows]
    assert {r.actor_type for r in rows} == {"system"}
    assert {r.actor_id for r in rows} == {None}
    assert [r.meta["address"] for r in rows] == [
        "10.70.0.10",
        "10.70.0.11",
        "10.70.0.12",
    ]
    assert rows[0].meta["confidence"] == "medium"
    assert rows[1].meta["confidence"] == "low"
    assert rows[0].meta["mac"] == "a4:bb:6d:11:22:33"
    assert rows[0].meta["sweep_run_id"] == str(fx.run_id)
    assert rows[0].meta["provenance"] == "sweep"


async def test_a_rejected_sweep_creates_no_assets(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.4 rejects the WHOLE result, and asset creation is downstream of that.

    An agent reporting outside its authorization is the case this matters for:
    a handler that created rows from the good half of a compromised report
    would let one controlled agent write inventory for addresses nobody
    authorized it to touch.
    """
    org = await seed_org(admin_engine, "reject")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    fx = await dispatch_sweep(
        admin_engine, org.org_id, vrf_id=vrf_id, device_id=org.device_id
    )
    job = await complete_sweep(
        app_engine,
        admin_engine,
        fx,
        sweep_document(
            fx,
            [sweep_host("10.70.0.10", mac="a4:bb:6d:11:22:33"), sweep_host("10.99.99.99")],
        ),
        tmp_path,
    )
    assert job.status is JobStatus.failed
    assert await _found(admin_engine, org.org_id) == []


async def test_another_orgs_inventory_neither_suppresses_nor_receives_a_row(
    admin_engine: AsyncEngine
) -> None:
    """TENANCY, ON THE BYPASSRLS ADMIN ENGINE, WHICH IS THE ONLY WAY IT CAN
    FAIL.

    `org_session` runs NOBYPASSRLS, so a route test stays green with the
    `org_id` predicate deleted -- the database hides the other org either way.
    This calls the service on the ADMIN engine, where both orgs are visible,
    so the application predicate is the only thing separating them.

    TWO DIRECTIONS, and the first is the one a leak would show up in. Org B
    holds a device at the same address AND with the same MAC. If the
    already-known lookups are not tenant-scoped, org A's sweep concludes the
    machine is already recorded and writes NOTHING -- a silent, permanent hole
    in A's inventory caused by B's data. Proved falsifiable: with `org_id`
    removed from the two lookup predicates, this test fails on the first
    assertion.

    ORG B'S DEVICE CARRIES ORG A'S CLIENT (MSP clients plan 2026-10-26 Task
    11, lesson 2). The lookups are the swept subnet's client's too, and B's
    own client would be excluded by that predicate alone, leaving the org one
    untested. A row of B under A's internal client is impossible while
    `fk_devices_client_org` holds, so it is planted with that key dropped, in
    a transaction that is rolled back -- and everything is asserted inside it.
    """
    from app.modules.ipam.candidates import SweepSighting, auto_create_from_sweep

    org_a = await seed_org(admin_engine, "ten-a")
    org_b = await seed_org(admin_engine, "ten-b")
    subnet = await _sited_subnet(admin_engine, org_a.org_id, "10.70.0.0/24")
    mac = "a4:bb:6d:ab:cd:ef"

    async with AsyncSession(admin_engine) as session:
        await drop_client_keys(session, "devices", "fk_devices_client_org")
        planted = Device(
            org_id=org_b.org_id,
            client_id=org_a.org_id,  # org A's internal client
            site_id=org_a.org_id,
            source="manual",
            provenance="operator",
            primary_ip="10.70.0.90",
            nics={"items": [{"identifier": mac, "mac": mac}]},
        )
        session.add(planted)
        await session.flush()
        results = await auto_create_from_sweep(
            session,
            org_a.org_id,
            subnet=subnet,
            sightings=[
                SweepSighting(
                    address="10.70.0.90",
                    mac=mac,
                    hostname="",
                    confidence="medium",
                )
            ],
            sweep_run_id=uuid4(),
        )
        await session.flush()
        assert [r.created for r in results] == [True], results
        in_a = (
            await session.execute(
                select(Device.id, Device.primary_ip).where(
                    Device.org_id == org_a.org_id, Device.provenance == "sweep"
                )
            )
        ).all()
        assert [(r.id, str(r.primary_ip)) for r in in_a] == [
            (results[0].device_id, "10.70.0.90")
        ]
        # And nothing was written into B: its own seeded device and the
        # planted one, untouched.
        in_b = (
            await session.execute(
                select(func.count())
                .select_from(Device)
                .where(Device.org_id == org_b.org_id)
            )
        ).scalar_one()
        assert in_b == 2, "a row was created in the wrong org"
        await session.rollback()


class _LockCountingSession:
    """A session proxy that counts `pg_advisory_xact_lock` statements.

    Counted off the SQL that is actually sent, so an implementation that inlines
    the lock or reaches it through some other helper is counted the same.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.locks = 0

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "pg_advisory_xact_lock" in str(statement):
            self.locks += 1
        return await self._session.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


async def test_a_whole_sweep_appends_its_audit_events_under_one_lock(
    admin_engine: AsyncEngine,
) -> None:
    """**256 AUDITED CREATIONS, ONE PER-ORG ADVISORY LOCK, AND THE CHAIN STILL
    VERIFIES.**

    `audit_service.record` takes that lock, reads the org's head hash and
    flushes -- three round trips -- and this function calls it once per host a
    sweep found. A /24 therefore paid it 254 times inside a single transaction
    and a /16 far more; 4096 rows measured 14.7s, of which the audit path was
    almost all. The lock cannot be dropped: the chain is hash-linked, and two
    transactions appending to one org concurrently would read the same head and
    fork it. So it is taken ONCE and the batch is linked from the head read
    under it (`AuditService.record_many`).

    THREE ASSERTIONS, AND EACH FALSIFIES SOMETHING DIFFERENT. *If the per-row
    `record` came back* the count is 256, and nothing else in this file changes.
    *If the batch skipped the lock entirely* it is 0, and the chain is
    unserialised. *If the batch mislinked* `verify` reports `ok=False` with the
    offending id -- which is the property `GET /api/v1/audit/verify` serves and
    the only one an operator can see.

    A FULL /24's WORTH, not three rows: the cost this is about is per row, and a
    three-row fixture cannot tell one lock from three.
    """
    from app.core.audit import audit_service
    from app.modules.ipam.candidates import SweepSighting, auto_create_from_sweep

    org = await seed_org(admin_engine, "onelock")
    subnet = await _sited_subnet(admin_engine, org.org_id, "10.90.0.0/24")
    sightings = [
        SweepSighting(address=f"10.90.0.{n}", mac=None, hostname="", confidence="low")
        for n in range(256)
    ]
    async with AsyncSession(admin_engine) as session:
        counter = _LockCountingSession(session)
        results = await auto_create_from_sweep(
            counter,
            org.org_id,
            subnet=subnet,
            sightings=sightings,
            sweep_run_id=uuid4(),
        )
        await session.commit()

    assert sum(1 for r in results if r.created) == 256
    assert counter.locks == 1

    async with AsyncSession(admin_engine) as session:
        verified = await audit_service.verify(session, org.org_id)
        events = (
            await session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "asset.auto_created",
                )
            )
        ).scalar_one()
    assert verified.ok and verified.broken_at is None
    assert events == 256
