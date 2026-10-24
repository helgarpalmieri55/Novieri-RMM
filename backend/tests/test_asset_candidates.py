"""What the fleet has seen and the inventory does not know (spec §3).

THE ONE FAILURE THAT MAKES THIS SCREEN USELESS is listing the managed fleet as
candidates, and it is the DEFAULT outcome of the obvious query.
`ipam_observation.device_id` is set only for `source='inventory'` -- a device
reporting its own NICs -- and never for a sweep, deliberately (ipam spec §6.4:
a sweep asserting identity would be a compromised agent's cheapest way to
re-point an assignment). So a managed workstation that answers a ping has TWO
observation rows, one with a device id and one without, and `WHERE device_id IS
NULL` returns the whole estate.

The rules are therefore about the ADDRESS and the MAC across all of an org's
observations, never about one row. Each has a test here, and each fixture is
the shape that breaks the naive version.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device
from app.modules.ipam.candidates import list_candidates
from tests.conftest import ApiEnv
from tests.ipam_fixtures import seed_vrf, upsert_observation_row


async def _managed_device(api_env: ApiEnv, org_id: UUID) -> UUID:
    """A device with an `agents` row: what "the inventory knows this" means."""
    from app.models import Agent

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        device = Device(
            org_id=org_id, hostname="ws-01", os_family="linux", client_id=org_id, site_id=org_id
        )
        session.add(device)
        await session.flush()
        device_id = device.id
        session.add(
            Agent(
                org_id=org_id,
                device_id=device_id,
                cert_fingerprint=f"fp-{device_id.hex[:16]}",
                cert_serial="01",
                cert_not_after=datetime.now(UTC) + timedelta(days=365),
                enrolled_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return device_id


async def test_a_swept_address_nobody_owns_is_a_candidate(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    await upsert_observation_row(
        api_env,
        org_id,
        vrf_id=vrf_id,
        address="172.22.104.55",
        mac="00:50:56:aa:bb:cc",
        source="sweep",
        method="arp",
        confidence="medium",
    )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        found = (await list_candidates(session, org_id)).items
    assert [c.address for c in found] == ["172.22.104.55"]
    # The vendor is the single most useful hint for "what is this" when an
    # operator is triaging fifty rows.
    assert found[0].vendor == "VMware"
    assert found[0].oui_prefix == "00:50:56"


async def test_a_managed_device_is_not_a_candidate_even_though_the_sweep_saw_it(
    api_env: ApiEnv,
) -> None:
    """THE TEST THAT MATTERS. Two rows for one machine: the inventory row
    carrying the device id, and the sweep row that cannot carry one."""
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    device_id = await _managed_device(api_env, org_id)
    await upsert_observation_row(
        api_env,
        org_id,
        vrf_id=vrf_id,
        address="172.22.104.10",
        mac="00:1b:21:aa:bb:cc",
        source="inventory",
        method="nic",
        confidence="high",
        device_id=device_id,
    )
    await upsert_observation_row(
        api_env,
        org_id,
        vrf_id=vrf_id,
        address="172.22.104.10",
        mac="00:1b:21:aa:bb:cc",
        source="sweep",
        method="arp",
        confidence="medium",
    )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        found = (await list_candidates(session, org_id)).items
    assert found == [], [c.address for c in found]


async def test_a_managed_device_seen_only_by_ICMP_is_not_a_candidate_either(
    api_env: ApiEnv,
) -> None:
    """THE CASE THE MAC RULE CANNOT COVER, and the reason rule 2 exists.

    An ICMP observation carries NO MAC -- ping establishes that an address
    answers, and that is all it means (ipam spec, `IpamObservation.confidence`).
    So for a managed machine found by a ping sweep there is nothing for the MAC
    exclusion to match on, and only the ADDRESS exclusion keeps it off this
    list. Found by deleting that exclusion and watching every test still pass:
    the fixture above shares a MAC between its two rows, so the MAC rule was
    quietly covering for the missing one.
    """
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    device_id = await _managed_device(api_env, org_id)
    await upsert_observation_row(
        api_env,
        org_id,
        vrf_id=vrf_id,
        address="172.22.104.10",
        mac="00:1b:21:aa:bb:cc",
        source="inventory",
        method="nic",
        confidence="high",
        device_id=device_id,
    )
    await upsert_observation_row(
        api_env,
        org_id,
        vrf_id=vrf_id,
        address="172.22.104.10",
        mac=None,
        source="sweep",
        method="icmp",
        confidence="low",
    )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        found = (await list_candidates(session, org_id)).items
    assert found == [], [c.address for c in found]


async def test_a_machine_that_moved_address_is_not_a_candidate_at_its_new_one(
    api_env: ApiEnv,
) -> None:
    """Rule 3: the MAC is known even though this ADDRESS has never carried a
    device id. DHCP makes an address a lease, not an identity, so a laptop that
    picked up a new one must not reappear as something unknown."""
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    device_id = await _managed_device(api_env, org_id)
    await upsert_observation_row(
        api_env,
        org_id,
        vrf_id=vrf_id,
        address="172.22.104.10",
        mac="00:1b:21:aa:bb:cc",
        source="inventory",
        method="nic",
        confidence="high",
        device_id=device_id,
    )
    await upsert_observation_row(
        api_env,
        org_id,
        vrf_id=vrf_id,
        address="172.22.104.99",
        mac="00:1b:21:aa:bb:cc",
        source="sweep",
        method="arp",
        confidence="medium",
    )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        found = (await list_candidates(session, org_id)).items
    assert found == [], [c.address for c in found]


async def test_an_asset_an_operator_already_typed_in_is_not_a_candidate(
    api_env: ApiEnv,
) -> None:
    """Rule 4. The switch was entered by hand in part 1; the sweep then found
    it. Offering it as a candidate invites a duplicate row for the one record
    whose entire point is that a human curated it."""
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        session.add(
            Device(
                org_id=org_id,
                hostname=None,
                os_family=None,
                source="manual",
                # 0078 pairs the two: a `manual` row is one a person entered.
                provenance="operator",
                asset_type="network",
                lifecycle="unmanageable",
                primary_ip="172.22.104.1",
                client_id=org_id,
                site_id=org_id,
            )
        )
        await session.commit()
    await upsert_observation_row(
        api_env,
        org_id,
        vrf_id=vrf_id,
        address="172.22.104.1",
        mac="00:00:0c:11:22:33",
        source="sweep",
        method="icmp",
    )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        found = (await list_candidates(session, org_id)).items
    assert found == [], [c.address for c in found]


async def test_something_nobody_has_seen_for_months_is_not_a_candidate(
    api_env: ApiEnv,
) -> None:
    """Currency, using the same `W_obs` window every other "is this true now"
    question in IPAM uses. An address last seen in March is not a machine to go
    and find today, and a list that carried it would grow without bound."""
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    await upsert_observation_row(
        api_env,
        org_id,
        vrf_id=vrf_id,
        address="172.22.104.77",
        source="sweep",
        method="icmp",
        now=datetime.now(UTC) - timedelta(days=400),
    )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        found = (await list_candidates(session, org_id)).items
    assert found == [], [c.address for c in found]


async def test_another_orgs_candidates_are_hidden_by_rls(api_env: ApiEnv) -> None:
    """Falsifier 9. Org B must see nothing of org A's segment, and the sweep
    that found it was authorized by org A alone."""
    org_a = await api_env.create_org("cand-a")
    org_b = await api_env.create_org("cand-b")
    vrf_a = await seed_vrf(api_env, org_a)
    await upsert_observation_row(
        api_env, org_a, vrf_id=vrf_a, address="172.22.104.55", source="sweep", method="icmp"
    )
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        page_b = await list_candidates(session, org_b)
    assert page_b.items == []
    assert page_b.total == 0

    # Non-vacuity: org A DOES see it, so the empty result above is scoping
    # rather than a query that returns nothing for anybody.
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        page = await list_candidates(session, org_a)
        assert [c.address for c in page.items] == ["172.22.104.55"]


async def test_the_list_is_bounded_and_says_when_it_was_cut(api_env: ApiEnv) -> None:
    """A /16 sweep observes tens of thousands of addresses. An unbounded list
    is a memory event in the API process and an unusable screen -- and a
    truncated one that does not SAY it was truncated reads as "that is all of
    them", which is the same lie as a fabricated column."""
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    for last in range(1, 8):
        await upsert_observation_row(
            api_env,
            org_id,
            vrf_id=vrf_id,
            address=f"172.22.104.{last}",
            source="sweep",
            method="icmp",
        )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        page = await list_candidates(session, org_id, limit=3)
    assert len(page.items) == 3
    # The COUNT is separate from the page, so the console can say "3 of 7"
    # rather than leaving an operator to assume three is all there is.
    assert page.total == 7
    assert page.truncated is True
