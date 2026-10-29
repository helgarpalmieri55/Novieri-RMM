"""A push stays inside one client (MSP clients spec, invariant 5a; CLAUDE.md 2a(a)).

The originator of a push install receives the decrypted push credential
(`jobtypes.py`), so WHICH site server is chosen is a question about who holds a
plaintext administrative secret. Inside one organization, the answer must be a
site server of the target asset's own client: a technician who may act on
every client still pushes to X's machine through X's site server, never
through Y's -- even when Y's declared segment is the more specific one, which
is exactly what `list_site_servers`' most-specific-first order would otherwise
prefer.

And the target itself must be one client's -- the push's, its credential's
(plan 2026-10-26 Task 12). When vouched-for assets of two clients answer at the
same address (two customers with the same private range), the ladder never
chooses by row id: it is asked for one client's machine, and the credential
names the client. `ambiguous_asset` cannot arise in a ladder that always
knows the client; X's credential reaches X's machine, Y's reaches Y's.

Each fixture makes the guarded and the unguarded path produce DIFFERENT
observable values: Y's site server is always the more specific match, so a
lookup that ignored the client would pick it.
"""

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.modules.agent_push import preflight, runs
from app.modules.agent_push.net import parse_target_address
from app.modules.agent_push.preflight import REFUSAL_REASONS, evaluate_targets
from tests.agent_push_fixtures import (
    PushOrg,
    seed_asset,
    seed_client,
    seed_credential,
    seed_org,
    seed_push_authorization,
    seed_push_credential_scope,
    seed_push_site_server,
)
from tests.push_run_fixtures import grant_org_admin, make_dispatcher, run_world, start_run

TARGET = "10.61.0.17"
WIDE = "10.61.0.0/16"
NARROW = "10.61.0.0/24"


@pytest.fixture(autouse=True)
def _register_push_job_types() -> None:
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


async def _authorized_org(admin_engine: AsyncEngine, tag: str) -> PushOrg:
    org = await seed_org(admin_engine, tag)
    await grant_org_admin(admin_engine, org)
    return org


async def _authorized(admin_engine: AsyncEngine, org: PushOrg, client_id: UUID) -> UUID:
    """A push credential of `client_id`, an authorization of that client over
    WIDE and the credential bound to it: every bound but the machine and the
    originator, in ONE client (Task 12: a push is its credential's client's)."""
    credential_id = await seed_credential(admin_engine, org, client_id=client_id)
    await seed_push_authorization(admin_engine, org, WIDE, client_id=client_id)
    await seed_push_credential_scope(admin_engine, org, credential_id, WIDE)
    return credential_id


async def _decide(
    app_engine: AsyncEngine,
    org: PushOrg,
    credential_id: UUID,
    clients: ClientScope,
    address: str = TARGET,
    *,
    client_id: UUID,
) -> preflight.TargetDecision:
    async with org_session(org.org_id, engine=app_engine, clients=clients) as session:
        (decision,) = await evaluate_targets(
            session,
            org.org_id,
            addresses=[parse_target_address(address)],
            credential_id=credential_id,
            client_id=client_id,
        )
    return decision


async def test_an_every_client_push_uses_the_targets_own_clients_site_server(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """X's asset; X's site server declared for the /16 and Y's for the /24.
    Without the client predicate the most specific -- Y's -- originates, and
    Y's machine is handed X's administrative credential."""
    org = await _authorized_org(admin_engine, "pcs-overlap")
    x_client, x_site = await seed_client(admin_engine, org.org_id, "X")
    y_client, y_site = await seed_client(admin_engine, org.org_id, "Y")
    credential_id = await _authorized(admin_engine, org, x_client)
    asset = await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=x_client, site_id=x_site
    )
    x_ss, x_ss_device = await seed_push_site_server(
        admin_engine, org.org_id, segments=[WIDE], client_id=x_client, site_id=x_site
    )
    await seed_push_site_server(
        admin_engine, org.org_id, segments=[NARROW], client_id=y_client, site_id=y_site
    )

    decision = await _decide(
        app_engine, org, credential_id, ClientScope.all(), client_id=x_client
    )

    assert decision.refusals == ()
    assert decision.device_id == asset
    assert (decision.site_server_id, decision.site_server_device_id) == (x_ss, x_ss_device)


async def test_another_clients_site_server_is_never_a_fallback(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Only Y has a site server on the segment: X's push is `no_site_server`,
    exactly as if no site server existed (2a(a): there is no fallback
    originator)."""
    org = await _authorized_org(admin_engine, "pcs-nofallback")
    x_client, x_site = await seed_client(admin_engine, org.org_id, "X")
    y_client, y_site = await seed_client(admin_engine, org.org_id, "Y")
    credential_id = await _authorized(admin_engine, org, x_client)
    await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=x_client, site_id=x_site
    )
    await seed_push_site_server(
        admin_engine, org.org_id, segments=[NARROW], client_id=y_client, site_id=y_site
    )

    decision = await _decide(
        app_engine, org, credential_id, ClientScope.all(), client_id=x_client
    )

    assert decision.refusals == ("no_site_server",)
    assert decision.site_server_id is None
    assert decision.site_server_device_id is None


async def test_an_address_held_by_two_clients_names_the_credential_s_clients_machine(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Two customers on the same private range: the address names two
    machines. Choosing by row id would push to whichever was created first.

    RETARGETED BY PLAN 2026-10-26 TASK 12, ON PURPOSE (2a(d) narrowed to the
    client). This test used to assert `ambiguous_asset` under every client's
    scope: the ladder could not know which client the person meant. Now it
    always does -- the push's client is its credential's -- so the same
    world answers with ONE machine per credential, and never by row id: X's
    credential reaches X's machine through X's site server, Y's credential
    Y's through Y's, whichever was recorded first, under every client's scope
    as under a narrowed one."""
    org = await _authorized_org(admin_engine, "pcs-ambiguous")
    x_client, x_site = await seed_client(admin_engine, org.org_id, "X")
    y_client, y_site = await seed_client(admin_engine, org.org_id, "Y")
    x_credential = await _authorized(admin_engine, org, x_client)
    y_credential = await _authorized(admin_engine, org, y_client)
    x_asset = await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=x_client, site_id=x_site
    )
    y_asset = await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=y_client, site_id=y_site
    )
    _x_ss, x_ss_device = await seed_push_site_server(
        admin_engine, org.org_id, segments=[NARROW], client_id=x_client, site_id=x_site
    )
    _y_ss, y_ss_device = await seed_push_site_server(
        admin_engine, org.org_id, segments=[NARROW], client_id=y_client, site_id=y_site
    )

    for credential, client, asset, ss_device in (
        (x_credential, x_client, x_asset, x_ss_device),
        (y_credential, y_client, y_asset, y_ss_device),
    ):
        for scope in (ClientScope.all(), ClientScope.only([client])):
            decision = await _decide(app_engine, org, credential, scope, client_id=client)
            assert decision.refusals == ()
            assert (decision.device_id, decision.site_server_device_id) == (asset, ss_device)

    # A person narrowed to X who names Y's client sees none of Y's rows: no
    # machine, no authorization, no originator -- refused, never X's.
    crossed = await _decide(
        app_engine, org, y_credential, ClientScope.only([x_client]), client_id=y_client
    )
    assert crossed.device_id is None and crossed.site_server_device_id is None
    assert "no_asset" in crossed.refusals or "credential_missing" in crossed.refusals


async def test_two_assets_of_one_client_at_one_address_are_not_ambiguous(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The refusal is about CLIENTS, not rows: two vouched records of the same
    client at one address (a re-imaged machine) resolve as before, inside
    that client."""
    org = await _authorized_org(admin_engine, "pcs-sameclient")
    x_client, x_site = await seed_client(admin_engine, org.org_id, "X")
    credential_id = await _authorized(admin_engine, org, x_client)
    first = await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=x_client, site_id=x_site
    )
    second = await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=x_client, site_id=x_site
    )
    await seed_push_site_server(
        admin_engine, org.org_id, segments=[NARROW], client_id=x_client, site_id=x_site
    )

    decision = await _decide(
        app_engine, org, credential_id, ClientScope.all(), client_id=x_client
    )

    assert decision.refusals == ()
    assert decision.device_id == min(first, second)


def test_ambiguous_asset_is_a_named_reason_with_a_remedy() -> None:
    assert "ambiguous_asset" in REFUSAL_REASONS
    assert len(REFUSAL_REASONS["ambiguous_asset"]) > 80


async def test_the_pacer_keeps_a_run_inside_the_targets_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The pacer works under the every-client scope, and re-runs the ladder
    before every target. A site server of another client declared for a more
    specific segment AFTER the run was confirmed must not become the
    originator: the job goes to the target client's own site server."""
    world = await run_world(admin_engine, "pcs-pacer", octet=62, count=1)
    run_id = await start_run(app_engine, world)
    y_client, y_site = await seed_client(admin_engine, world.org.org_id, "Y")
    _y_ss, y_ss_device = await seed_push_site_server(
        admin_engine,
        world.org.org_id,
        segments=[f"{world.addresses[0]}/32"],
        client_id=y_client,
        site_id=y_site,
    )

    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        envelopes = await runs.advance_run(
            session, world.org.org_id, run_id, dispatcher=make_dispatcher(tmp_path)
        )

    devices = {device for device, _envelope in envelopes}
    assert devices == {world.site_server_device_id}
    assert y_ss_device not in devices


async def test_the_pacer_pushes_the_run_s_own_machine_when_another_client_shares_its_address(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A second client's vouched asset appears at a confirmed target's address
    before the pacer reaches it.

    RETARGETED BY PLAN 2026-10-26 TASK 12, ON PURPOSE (2a(d) narrowed to the
    client). This test used to be `test_the_pacer_refuses_a_target_that_
    became_ambiguous`: under every client the pacer saw two machines and
    refused. The run is one client's now and the pacer re-runs the ladder
    inside it, so the address names ONE machine -- the run's client's, the
    one the person confirmed -- and the pacer pushes THAT machine through ITS
    client's site server, under every client's scope. What it still never
    does is choose by row id or reach the other client's machine: if the
    confirmed machine stops being the run's client's vouched asset and only
    the other client's remains at the address, the target is refused."""
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.agent_push.models import PushRunTarget

    world = await run_world(admin_engine, "pcs-pacer-amb", octet=63, count=2)
    run_id = await start_run(app_engine, world)
    y_client, y_site = await seed_client(admin_engine, world.org.org_id, "Y")
    for address in world.addresses:
        await seed_asset(
            admin_engine, world.org.org_id, primary_ip=address, client_id=y_client,
            site_id=y_site,
        )
    await seed_push_site_server(
        admin_engine, world.org.org_id, segments=[f"{world.addresses[1]}/32"],
        client_id=y_client, site_id=y_site,
    )
    # The FIRST confirmed machine is no longer vouched for (back in review):
    # at its address, only Y's machine is. A refusal takes no capacity, so the
    # second target -- whose address Y also holds -- is still dispatched.
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE devices SET review_state = 'pending' WHERE id = :d"),
            {"d": world.device_ids[0]},
        )
        await session.commit()

    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        envelopes = await runs.advance_run(
            session, world.org.org_id, run_id, dispatcher=make_dispatcher(tmp_path)
        )

    # The second target, the run's own machine, through the run's own client's
    # site server -- not Y's, declared for that very address.
    assert [device for device, _envelope in envelopes] == [world.site_server_device_id]
    assert envelopes[0][1]["job"]["params"]["target_address"] == world.addresses[1]
    async with AsyncSession(admin_engine) as session:
        targets = list(
            (
                await session.execute(
                    select(PushRunTarget)
                    .where(PushRunTarget.run_id == run_id)
                    .order_by(PushRunTarget.position)
                )
            ).scalars()
        )
    assert [t.state for t in targets] == ["refused", "in_flight"]
    assert "credential_missing" in targets[0].reasons
    assert "ambiguous_asset" not in targets[0].reasons
