"""A push stays inside one client (MSP clients spec, invariant 5a; CLAUDE.md 2a(a)).

The originator of a push install receives the decrypted push credential
(`jobtypes.py`), so WHICH site server is chosen is a question about who holds a
plaintext administrative secret. Inside one organization, the answer must be a
site server of the target asset's own client: a technician who may act on
every client still pushes to X's machine through X's site server, never
through Y's -- even when Y's declared segment is the more specific one, which
is exactly what `list_site_servers`' most-specific-first order would otherwise
prefer.

And the target itself must be one client's: when vouched-for assets of two
clients answer at the same address (two customers with the same private
range), the ladder cannot know which machine the person meant, and refuses
with `ambiguous_asset` rather than choosing by row id.

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


async def _authorized_org(admin_engine: AsyncEngine, tag: str) -> tuple[PushOrg, UUID]:
    org = await seed_org(admin_engine, tag)
    await grant_org_admin(admin_engine, org)
    credential_id = await seed_credential(admin_engine, org)
    await seed_push_authorization(admin_engine, org, WIDE)
    await seed_push_credential_scope(admin_engine, org, credential_id, WIDE)
    return org, credential_id


async def _decide(
    app_engine: AsyncEngine,
    org: PushOrg,
    credential_id: UUID,
    clients: ClientScope,
    address: str = TARGET,
) -> preflight.TargetDecision:
    async with org_session(org.org_id, engine=app_engine, clients=clients) as session:
        (decision,) = await evaluate_targets(
            session,
            org.org_id,
            addresses=[parse_target_address(address)],
            credential_id=credential_id,
        )
    return decision


async def test_an_every_client_push_uses_the_targets_own_clients_site_server(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """X's asset; X's site server declared for the /16 and Y's for the /24.
    Without the client predicate the most specific -- Y's -- originates, and
    Y's machine is handed X's administrative credential."""
    org, credential_id = await _authorized_org(admin_engine, "pcs-overlap")
    x_client, x_site = await seed_client(admin_engine, org.org_id, "X")
    y_client, y_site = await seed_client(admin_engine, org.org_id, "Y")
    asset = await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=x_client, site_id=x_site
    )
    x_ss, x_ss_device = await seed_push_site_server(
        admin_engine, org.org_id, segments=[WIDE], client_id=x_client, site_id=x_site
    )
    await seed_push_site_server(
        admin_engine, org.org_id, segments=[NARROW], client_id=y_client, site_id=y_site
    )

    decision = await _decide(app_engine, org, credential_id, ClientScope.all())

    assert decision.refusals == ()
    assert decision.device_id == asset
    assert (decision.site_server_id, decision.site_server_device_id) == (x_ss, x_ss_device)


async def test_another_clients_site_server_is_never_a_fallback(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Only Y has a site server on the segment: X's push is `no_site_server`,
    exactly as if no site server existed (2a(a): there is no fallback
    originator)."""
    org, credential_id = await _authorized_org(admin_engine, "pcs-nofallback")
    x_client, x_site = await seed_client(admin_engine, org.org_id, "X")
    y_client, y_site = await seed_client(admin_engine, org.org_id, "Y")
    await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=x_client, site_id=x_site
    )
    await seed_push_site_server(
        admin_engine, org.org_id, segments=[NARROW], client_id=y_client, site_id=y_site
    )

    decision = await _decide(app_engine, org, credential_id, ClientScope.all())

    assert decision.refusals == ("no_site_server",)
    assert decision.site_server_id is None
    assert decision.site_server_device_id is None


async def test_an_address_held_by_two_clients_assets_is_ambiguous(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Two customers on the same private range: the address names two
    machines. Choosing by row id would push to whichever was created first;
    the ladder refuses instead, and every site server stays unchosen."""
    org, credential_id = await _authorized_org(admin_engine, "pcs-ambiguous")
    x_client, x_site = await seed_client(admin_engine, org.org_id, "X")
    y_client, y_site = await seed_client(admin_engine, org.org_id, "Y")
    x_asset = await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=x_client, site_id=x_site
    )
    await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=y_client, site_id=y_site
    )
    _x_ss, x_ss_device = await seed_push_site_server(
        admin_engine, org.org_id, segments=[NARROW], client_id=x_client, site_id=x_site
    )
    await seed_push_site_server(
        admin_engine, org.org_id, segments=[NARROW], client_id=y_client, site_id=y_site
    )

    everyone = await _decide(app_engine, org, credential_id, ClientScope.all())
    assert everyone.refusals == ("ambiguous_asset",)
    assert everyone.device_id is None
    assert everyone.site_server_device_id is None

    # Narrowed to X (X-Client-Id), the address names one machine again: X's,
    # pushed through X's site server.
    only_x = await _decide(app_engine, org, credential_id, ClientScope.only([x_client]))
    assert only_x.refusals == ()
    assert only_x.device_id == x_asset
    assert only_x.site_server_device_id == x_ss_device


async def test_two_assets_of_one_client_at_one_address_are_not_ambiguous(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The refusal is about CLIENTS, not rows: two vouched records of the same
    client at one address (a re-imaged machine) resolve as before, inside
    that client."""
    org, credential_id = await _authorized_org(admin_engine, "pcs-sameclient")
    x_client, x_site = await seed_client(admin_engine, org.org_id, "X")
    first = await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=x_client, site_id=x_site
    )
    second = await seed_asset(
        admin_engine, org.org_id, primary_ip=TARGET, client_id=x_client, site_id=x_site
    )
    await seed_push_site_server(
        admin_engine, org.org_id, segments=[NARROW], client_id=x_client, site_id=x_site
    )

    decision = await _decide(app_engine, org, credential_id, ClientScope.all())

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


async def test_the_pacer_refuses_a_target_that_became_ambiguous(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A second client's vouched asset appears at a confirmed target's address
    before the pacer reaches it: the pacer, under every client, sees two
    machines and refuses the target by name rather than choosing one."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.agent_push.models import PushRunTarget

    world = await run_world(admin_engine, "pcs-pacer-amb", octet=63, count=1)
    run_id = await start_run(app_engine, world)
    y_client, y_site = await seed_client(admin_engine, world.org.org_id, "Y")
    await seed_asset(
        admin_engine,
        world.org.org_id,
        primary_ip=world.addresses[0],
        client_id=y_client,
        site_id=y_site,
    )

    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        envelopes = await runs.advance_run(
            session, world.org.org_id, run_id, dispatcher=make_dispatcher(tmp_path)
        )

    assert envelopes == []
    async with AsyncSession(admin_engine) as session:
        (target,) = (
            await session.execute(select(PushRunTarget).where(PushRunTarget.run_id == run_id))
        ).scalars()
    assert target.state == "refused"
    assert "ambiguous_asset" in target.reasons
