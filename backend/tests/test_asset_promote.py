"""Promoting a discovery candidate to an asset (spec §3).

**THE FIRST TEST IN THIS FILE WAS INVERTED ON 2026-08-26, NOT DELETED.** It
used to observe three addresses, run nothing else, and assert that the device
table was EMPTY -- §6.1's falsifier for "bulk promote is allowed; bulk
auto-promote is not". The operator was shown that reasoning, and its
consequence (a sweep that finds forty machines leaves forty rows of work for a
person), and ruled the other way: a sweep records everything it finds. So the
same three addresses now assert three asset rows. A deleted test is a guarantee
nobody can see leaving; an inverted one shows exactly what changed and when.

WHAT THE OLD ARGUMENT WAS, because it was not wrong about the risk. A ping
sweep on a corporate LAN answers for printers, phones, a colleague's laptop on
the wrong VLAN and the coffee machine, and rows built from that used to be
indistinguishable from rows somebody entered by hand. `devices.provenance` is
what makes them distinguishable now -- `promoted` when a person picked it off
the Discovered screen, `sweep` when the platform recorded it on its own
evidence -- and `test_ipam_sweep_autoasset.py` is where that column is pinned.

**PROMOTE IS STILL HERE AND IS NOT A DISABLED-BY-DEFAULT REMNANT.** It is how
an operator says they vouch for a row, and it is still the only path that takes
their asset type, lifecycle, owner and location.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Device
from tests.conftest import ApiEnv
from tests.ipam_fixtures import (
    complete_sweep,
    dispatch_sweep,
    seed_device,
    seed_vrf,
    sweep_document,
    sweep_host,
    upsert_observation_row,
)


async def _observe(api_env: ApiEnv, org_id: UUID, address: str, **kw: Any) -> None:
    vrf_id = kw.pop("vrf_id", None) or await seed_vrf(api_env, org_id, name=f"v-{uuid4().hex[:6]}")
    await upsert_observation_row(
        api_env,
        org_id,
        vrf_id=vrf_id,
        address=address,
        source=kw.pop("source", "sweep"),
        method=kw.pop("method", "arp"),
        confidence=kw.pop("confidence", "medium"),
        **kw,
    )


async def _device_count(api_env: ApiEnv, org_id: UUID) -> int:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        return int(
            (await session.execute(select(func.count()).select_from(Device))).scalar_one()
        )


async def test_a_sweep_alone_creates_the_devices_it_found(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """§6.1's falsifier, INVERTED on the operator's ruling of 2026-08-26.

    THE SAME FIXTURE, THE OPPOSITE ASSERTION. Three addresses answer one sweep
    and nobody clicks anything; before that date this asserted
    `_device_count == 0` and named "a sweep-completion hook or a schedule
    appearing later" as what it would catch. That hook now exists, on purpose,
    and this is its guarantee written the right way round.

    RUN THROUGH `JobEngine.complete`, not by inserting observations. Rows in
    `ipam_observation` are not a sweep, and a version of this test that wrote
    them directly would pass with the entire auto-creation path deleted -- the
    exact failure mode the original was designed to avoid.

    AND THE DISCOVERED LIST IS EMPTY AFTERWARDS, which is the other half of
    what changed: an address the platform has recorded is no longer something
    the inventory "cannot account for". A candidate list that still showed
    these three would be offering an operator work that has already been done.
    """
    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    vrf_id = await seed_vrf(api_env, org_id)
    agent_device = await seed_device(api_env, org_id)
    fx = await dispatch_sweep(
        api_env,
        org_id,
        vrf_id=vrf_id,
        device_id=agent_device,
        prefix="172.22.104.0/24",
    )
    job = await complete_sweep(
        api_env.app_engine,
        api_env,
        fx,
        sweep_document(
            fx, [sweep_host(f"172.22.104.{last}") for last in (11, 12, 13)]
        ),
        tmp_path,
    )
    assert job.status.value == "succeeded", job.result

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = list(
            (await session.execute(select(Device).order_by(Device.primary_ip)))
            .scalars()
            .all()
        )
    swept = [row for row in rows if row.provenance == "sweep"]
    assert [str(row.primary_ip) for row in swept] == [
        "172.22.104.11",
        "172.22.104.12",
        "172.22.104.13",
    ]
    assert await _device_count(api_env, org_id) == 4  # the three, and the agent's own

    listed = await api_env.client.get(
        "/api/v1/devices/candidates", headers=await api_env.headers(org_id, admin)
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []


async def test_the_candidates_route_is_not_shadowed_by_the_device_route(
    api_env: ApiEnv,
) -> None:
    """`/devices/candidates` against `/devices/{device_id}`.

    Declared in the wrong order, FastAPI parses the literal path as a UUID and
    the route 422s -- a failure that appears only in a browser and reads like a
    frontend bug.
    """
    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.get(
        "/api/v1/devices/candidates", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text


async def test_promoting_requires_assets_manage(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("promo")
    reader = await api_env.create_user(org_id, "read-only")
    await _observe(api_env, org_id, "172.22.104.21")
    resp = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"addresses": ["172.22.104.21"]},
        headers=await api_env.headers(org_id, reader),
    )
    assert resp.status_code == 403, resp.text
    assert await _device_count(api_env, org_id) == 0


async def test_a_promoted_candidate_is_marked_discovery(api_env: ApiEnv) -> None:
    """`source='discovery'`, never `manual`. How a row came to exist is the
    first thing an operator checks when it looks wrong, and "a human typed
    this" and "a sweep saw this and somebody clicked" are different claims."""
    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    await _observe(api_env, org_id, "172.22.104.22", mac="00:50:56:aa:bb:cc")

    resp = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"addresses": ["172.22.104.22"]},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 200, resp.text
    [result] = resp.json()["results"]
    assert result["created"] is True

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        device = (await session.execute(select(Device))).scalar_one()
    assert device.source == "discovery"
    # AND A PERSON STOOD BEHIND IT. Since the sweep began recording rows on its
    # own, `source='discovery'` alone no longer says that -- `provenance` is
    # the field that separates "somebody picked this" from "the platform saw
    # something answer".
    assert device.provenance == "promoted"
    assert str(device.primary_ip) == "172.22.104.22"
    # No agent, so no lie about being managed.
    assert device.lifecycle == "in_service"


async def test_promotion_carries_what_the_operator_supplied_on_top(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    await _observe(api_env, org_id, "172.22.104.23")

    resp = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={
            "addresses": ["172.22.104.23"],
            "asset_type": "printer",
            "lifecycle": "unmanageable",
            "location": "HQ floor 2",
        },
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 200, resp.text
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        device = (await session.execute(select(Device))).scalar_one()
    assert device.asset_type == "printer"
    assert device.lifecycle == "unmanageable"
    assert device.location == "HQ floor 2"


async def test_promoting_the_same_address_twice_does_not_duplicate(
    api_env: ApiEnv,
) -> None:
    """Two operators looking at the same list, or one double-click. The second
    call must not produce a second row -- and must not 500 either, because the
    operator cannot tell those two outcomes apart from the screen."""
    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    await _observe(api_env, org_id, "172.22.104.24")

    first = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"addresses": ["172.22.104.24"]},
        headers=headers,
    )
    second = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"addresses": ["172.22.104.24"]},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["results"][0]["created"] is True
    assert second.json()["results"][0]["created"] is False
    assert await _device_count(api_env, org_id) == 1


async def test_an_address_nobody_observed_is_refused_per_address(
    api_env: ApiEnv,
) -> None:
    """The row must come from an OBSERVATION, not from the request body. A
    client can post any address; promoting one nobody has seen would let the
    console write arbitrary inventory rows through a route whose whole premise
    is that the fleet found them.

    Reported per address rather than failing the batch, so one stale entry in a
    selection does not silently discard the rest.
    """
    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    await _observe(api_env, org_id, "172.22.104.25")

    resp = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"addresses": ["172.22.104.25", "10.99.99.99"]},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 200, resp.text
    results = {r["address"]: r for r in resp.json()["results"]}
    assert results["172.22.104.25"]["created"] is True
    assert results["10.99.99.99"]["created"] is False
    assert "not a candidate" in results["10.99.99.99"]["reason"]
    assert await _device_count(api_env, org_id) == 1


async def test_promotion_is_audited_naming_the_address(api_env: ApiEnv) -> None:
    """Invariant 4. A row appearing in the inventory changes what the platform
    believes the estate contains, and every later sweep and report reads it."""
    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    await _observe(api_env, org_id, "172.22.104.26")

    await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"addresses": ["172.22.104.26"]},
        headers=await api_env.headers(org_id, admin),
    )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = list(
            (
                await session.execute(
                    select(AuditEvent.action, AuditEvent.meta).order_by(AuditEvent.id)
                )
            ).all()
        )
    promoted = [meta for action, meta in rows if action == "asset.promoted"]
    assert promoted, [action for action, _ in rows]
    assert promoted[0]["address"] == "172.22.104.26"


async def test_an_address_observed_in_another_org_is_not_promotable_under_rls(
    api_env: ApiEnv,
) -> None:
    """An address observed in org A cannot be promoted by org B.

    THE OBSERVATION IS THE ABSENCE OF A ROW, NOT THE PREDICATE. The promote
    call goes through the route on the app engine, so RLS hides org A's
    observation from org B before any application filter runs -- this is green
    with that filter deleted. The admin-engine query afterwards is what makes
    the "created: false" answer worth anything: it counts the asset rows in
    BOTH orgs directly, so a promotion that quietly wrote somewhere would show
    up instead of hiding behind a tenant-scoped read."""
    org_a = await api_env.create_org("promo-a")
    org_b = await api_env.create_org("promo-b")
    admin_b = await api_env.create_user(org_b, "org-admin")
    await _observe(api_env, org_a, "172.22.104.27")

    resp = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"addresses": ["172.22.104.27"]},
        headers=await api_env.headers(org_b, admin_b),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["created"] is False

    # SCOPED TO ORG B, not a global count: the admin engine bypasses RLS and
    # therefore sees every org in the test database, including devices other
    # tests in this file created. A bare count passes alone and fails in the
    # file -- which is a test defect that looks exactly like a tenancy bug.
    async with AsyncSession(api_env.admin_engine) as session:
        in_org_b = (
            await session.execute(
                select(func.count()).select_from(Device).where(Device.org_id == org_b)
            )
        ).scalar_one()
        in_org_a = (
            await session.execute(
                select(func.count()).select_from(Device).where(Device.org_id == org_a)
            )
        ).scalar_one()
    assert in_org_b == 0, "org B promoted an address it has never observed"
    # And org A's observation was not promoted into A either -- nobody asked.
    assert in_org_a == 0


async def test_the_response_says_when_no_agent_could_have_swept(
    api_env: ApiEnv,
) -> None:
    """Falsifier 8, and the reason an empty list is not an answer.

    "Nothing was found" and "nothing could look" are indistinguishable on
    screen and mean opposite things: the first says the segment is clean, the
    second says the platform has no idea what is on it. IPAM already records
    `state='no_agent'` as a first-class sweep outcome; this asserts the asset
    side reads it, so the console can say which of the two it is showing.
    """
    from app.models import IpamSubnet, IpamSweepRun

    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    vrf_id = await seed_vrf(api_env, org_id)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        subnet = IpamSubnet(org_id=org_id, vrf_id=vrf_id, prefix="172.22.104.0/24")
        session.add(subnet)
        await session.flush()
        session.add(
            IpamSweepRun(
                org_id=org_id,
                subnet_id=subnet.id,
                state="no_agent",
                trigger="scheduled",
                target_count=0,
                scope_digest="",
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()

    resp = await api_env.client.get(
        "/api/v1/devices/candidates", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["unsweepable_subnets"] == 1, body


async def test_a_subnet_that_has_since_been_swept_is_not_reported_unsweepable(
    api_env: ApiEnv,
) -> None:
    """Non-vacuity, and a correctness case: the MOST RECENT run per subnet,
    not "any run ever". A subnet that had no agent last month and has one now
    is swept, and reporting it as unsweepable is the same lie pointed the other
    way."""
    from app.models import IpamSubnet, IpamSweepRun

    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    vrf_id = await seed_vrf(api_env, org_id)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        subnet = IpamSubnet(org_id=org_id, vrf_id=vrf_id, prefix="172.22.105.0/24")
        session.add(subnet)
        await session.flush()
        session.add(
            IpamSweepRun(
                org_id=org_id,
                subnet_id=subnet.id,
                state="no_agent",
                trigger="scheduled",
                target_count=0,
                scope_digest="",
                started_at=datetime.now(UTC) - timedelta(days=30),
            )
        )
        session.add(
            IpamSweepRun(
                org_id=org_id,
                subnet_id=subnet.id,
                state="succeeded",
                trigger="scheduled",
                target_count=254,
                scope_digest="",
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()

    resp = await api_env.client.get(
        "/api/v1/devices/candidates", headers=await api_env.headers(org_id, admin)
    )
    assert resp.json()["unsweepable_subnets"] == 0, resp.json()


async def test_a_promoted_row_carries_the_mac_that_identified_it(
    api_env: ApiEnv,
) -> None:
    """WITHOUT THIS, PART 3'S WHOLE PREMISE FAILS for the case it was designed
    for.

    The point of discovery → promote → install-agent is that the agent joins
    the record already there. That match is on a physical MAC — and a promoted
    row that stored only its address and hostname has no MAC for the agent to
    match against, so enrolment would create a second row for the machine an
    operator had just curated.

    Stored in the `nics` shape inventory already writes, so the matcher has one
    reader rather than two.
    """
    from app.services.enrollment import DeviceFacts
    from app.services.matching import resolve_enrolment_target

    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    await _observe(api_env, org_id, "172.22.104.31", mac="00:1b:21:aa:bb:cc")

    resp = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"addresses": ["172.22.104.31"]},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 200, resp.text
    device_id = UUID(resp.json()["results"][0]["device_id"])

    # The agent installs and enrols a week later, reporting the same address.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        outcome = await resolve_enrolment_target(
            session,
            org_id,
            DeviceFacts(
                hostname="printer-3", os_family="linux", macs=["00:1b:21:aa:bb:cc"]
            ),
        )
    assert outcome.device_id == device_id, (
        "the promoted row was not matchable: enrolment would have created a "
        "second row for a machine the operator had just curated"
    )


async def test_a_candidate_with_no_mac_promotes_without_inventing_one(
    api_env: ApiEnv,
) -> None:
    """An ICMP-only sighting carries no MAC. The row is still worth creating —
    the operator recognised the address — but `nics` must stay empty rather
    than gain a fabricated entry that the matcher would later act on."""
    org_id = await api_env.create_org("promo")
    admin = await api_env.create_user(org_id, "org-admin")
    await _observe(api_env, org_id, "172.22.104.32", mac=None, method="icmp",
                   confidence="low")

    resp = await api_env.client.post(
        "/api/v1/devices/candidates/promote",
        json={"addresses": ["172.22.104.32"]},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 200, resp.text
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        device = (
            await session.execute(
                select(Device).where(Device.primary_ip == "172.22.104.32")
            )
        ).scalar_one()
    assert not (device.nics or {}).get("items"), device.nics
