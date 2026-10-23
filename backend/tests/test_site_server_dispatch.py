"""`wake_via_site_server`: the task that puts a packet on somebody else's LAN
(spec §0.2, §0.9, §0.10, §5.2).

**THE PROPERTY UNDER TEST IS THAT THE CONTROL PLANE, NOT THE ENDPOINT, DECIDES
WHERE THE DATAGRAM GOES.** Every test below is written so the guarded and the
unguarded path produce different OBSERVABLE VALUES: a different broadcast
address, a different `jobs` row count, a different stored TTL. "An exception was
raised" appears nowhere as the sole evidence.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.modules.site_server.dispatch import wake_via_site_server
from app.modules.site_server.errors import (
    SiteServerNotFoundError,
    SiteServerValidationError,
)
from app.modules.site_server.handler import WAKE_TTL_S, register_job_types
from tests.ipam_fixtures import seed_agent, seed_device, seed_org
from tests.site_server_fixtures import seed_site_server

register_job_types()


def _engine_for(tmp_path: Path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "job-signing.key"))


class _RecordingRedis:
    """Records publishes so "nothing was published" is an OBSERVATION.

    `publish_job` is never called by the module under test, and asserting an
    empty list here is different evidence from "no exception was raised": a
    dispatcher that published inside the transaction would raise nothing at all.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, channel: str, message: Any) -> None:
        self.published.append((channel, message))


async def _jobs(engine: AsyncEngine, org_id: UUID) -> list[Any]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    text(
                        "SELECT id, type, target_type, target_id, params, signature,"
                        "       issued_at, expires_at FROM jobs"
                        " WHERE org_id = :org ORDER BY issued_at, id"
                    ),
                    {"org": org_id},
                )
            ).all()
        )


async def _audit_meta(engine: AsyncEngine, org_id: UUID, action: str) -> dict[str, Any]:
    async with AsyncSession(engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT metadata FROM audit_events WHERE org_id = :org"
                    "   AND action = :action ORDER BY ts DESC, id DESC LIMIT 1"
                ),
                {"org": org_id, "action": action},
            )
        ).scalar_one()
    return dict(row)


# --------------------------------------------------------------------------- #
# The broadcast is computed server-side, from the most specific segment
# --------------------------------------------------------------------------- #


async def test_the_broadcast_comes_from_the_most_specific_declared_segment(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**TWO OVERLAPPING SEGMENTS, OR THIS TEST CANNOT FAIL.**

    The site server declares `10.20.0.0/16` AND `10.20.7.0/24`. A machine on the
    /24 must be woken at `10.20.7.255`; `10.20.255.255` would be broadcast to
    the whole /16, which is both louder than authorized and, on most routed
    networks, does not reach the host at all. With only one segment declared,
    "picked the most specific" and "picked the only one" are the same answer and
    the assertion is decoration.
    """
    org = await seed_org(admin_engine, "ssd-a")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine,
        org.org_id,
        device_id=org.device_id,
        segments=["10.20.0.0/16", "10.20.7.0/24"],
    )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job, envelope = await wake_via_site_server(
            session,
            _engine_for(tmp_path),
            org_id=org.org_id,
            site_server_id=site_server_id,
            target_mac="A4-BB-6D-11-22-33",
            segment="10.20.7.31",
            actor_id=org.user_id,
        )
        signed_params = dict(envelope["job"]["params"])

    rows = await _jobs(admin_engine, org.org_id)
    assert [(r.type, r.params["broadcast"], r.params["target_mac"]) for r in rows] == [
        ("site.wake", "10.20.7.255", "a4:bb:6d:11:22:33")
    ]
    # The SIGNED envelope carries the same address as the stored row: the
    # signature covers what the agent will act on, not a parallel value.
    assert signed_params["broadcast"] == "10.20.7.255"

    meta = await _audit_meta(admin_engine, org.org_id, "site.wake_dispatched")
    assert (meta["broadcast"], meta["segment"], meta["target_mac"]) == (
        "10.20.7.255",
        "10.20.7.0/24",
        "a4:bb:6d:11:22:33",
    )


async def test_a_named_prefix_wins_over_a_wider_declared_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A caller may name `10.20.7.0/24` on a server declared only for
    `10.20.0.0/16`: the server serves a branch, and the caller is saying which
    wire inside it. The broadcast comes from the NAMED prefix, because that is
    the wire the machine is claimed to be on."""
    org = await seed_org(admin_engine, "ssd-b")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.0.0/16"]
    )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await wake_via_site_server(
            session,
            _engine_for(tmp_path),
            org_id=org.org_id,
            site_server_id=site_server_id,
            target_mac="a4:bb:6d:11:22:33",
            segment="10.20.7.0/24",
        )

    rows = await _jobs(admin_engine, org.org_id)
    assert [r.params["broadcast"] for r in rows] == ["10.20.7.255"]


async def test_a_sole_declared_segment_needs_no_segment_argument(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org = await seed_org(admin_engine, "ssd-c")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.7.0/24"]
    )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await wake_via_site_server(
            session,
            _engine_for(tmp_path),
            org_id=org.org_id,
            site_server_id=site_server_id,
            target_mac="a4:bb:6d:11:22:33",
        )

    rows = await _jobs(admin_engine, org.org_id)
    assert [r.params["broadcast"] for r in rows] == ["10.20.7.255"]


# --------------------------------------------------------------------------- #
# Refusals — asserted as a job COUNT, never as an exception
# --------------------------------------------------------------------------- #


async def test_a_segment_the_server_does_not_declare_creates_no_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """ASSERT THE `jobs` ROW COUNT. The failure being guarded against is a
    SIGNED JOB reaching a LAN nobody authorized, and "an exception was raised"
    cannot see one — an implementation that raised AFTER `JobEngine.create` would
    pass a `pytest.raises` and still have produced the signature."""
    org = await seed_org(admin_engine, "ssd-d")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.0.0/16"]
    )

    refused = False
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        try:
            await wake_via_site_server(
                session,
                _engine_for(tmp_path),
                org_id=org.org_id,
                site_server_id=site_server_id,
                target_mac="a4:bb:6d:11:22:33",
                segment="10.90.0.0/16",
            )
        except SiteServerValidationError:
            refused = True

    assert (refused, await _jobs(admin_engine, org.org_id)) == (True, [])


async def test_an_address_outside_every_declared_segment_creates_no_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org = await seed_org(admin_engine, "ssd-e")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.0.0/16"]
    )

    refused = False
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        try:
            await wake_via_site_server(
                session,
                _engine_for(tmp_path),
                org_id=org.org_id,
                site_server_id=site_server_id,
                target_mac="a4:bb:6d:11:22:33",
                # `10.200.7.9` starts with `10.20` and is nowhere near
                # `10.20.0.0/16` — the prefix-match bug, as a fixture.
                segment="10.200.7.9",
            )
        except SiteServerValidationError:
            refused = True

    assert (refused, await _jobs(admin_engine, org.org_id)) == (True, [])


async def test_several_declared_segments_and_no_choice_creates_no_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A MAC carries no topology. With two segments declared and none named, the
    module has no basis for choosing, and "the first one" would put a datagram
    on a wire the caller never mentioned."""
    org = await seed_org(admin_engine, "ssd-f")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine,
        org.org_id,
        device_id=org.device_id,
        segments=["10.20.0.0/16", "10.30.0.0/16"],
    )

    refused = False
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        try:
            await wake_via_site_server(
                session,
                _engine_for(tmp_path),
                org_id=org.org_id,
                site_server_id=site_server_id,
                target_mac="a4:bb:6d:11:22:33",
            )
        except SiteServerValidationError:
            refused = True

    assert (refused, await _jobs(admin_engine, org.org_id)) == (True, [])


async def test_a_malformed_mac_creates_no_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org = await seed_org(admin_engine, "ssd-g")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.7.0/24"]
    )

    refused = False
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        try:
            await wake_via_site_server(
                session,
                _engine_for(tmp_path),
                org_id=org.org_id,
                site_server_id=site_server_id,
                target_mac="nope",
            )
        except SiteServerValidationError:
            refused = True

    assert (refused, await _jobs(admin_engine, org.org_id)) == (True, [])


async def test_a_disabled_site_server_creates_no_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """`enabled=False` is a demote-without-delete. A row that still exists but
    has been switched off must not still be able to emit packets."""
    org = await seed_org(admin_engine, "ssd-h")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine,
        org.org_id,
        device_id=org.device_id,
        segments=["10.20.7.0/24"],
        enabled=False,
    )

    refused = False
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        try:
            await wake_via_site_server(
                session,
                _engine_for(tmp_path),
                org_id=org.org_id,
                site_server_id=site_server_id,
                target_mac="a4:bb:6d:11:22:33",
            )
        except SiteServerValidationError:
            refused = True

    assert (refused, await _jobs(admin_engine, org.org_id)) == (True, [])


# --------------------------------------------------------------------------- #
# The stored job: TTL, target, signature
# --------------------------------------------------------------------------- #


async def test_the_stored_ttl_is_three_hundred_seconds(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**ASSERTED ON THE STORED ROW**, as `expires_at - issued_at`.

    A dispatcher that omitted `ttl_s` would be observed here as 3600, which is
    the kernel default and the exact bug §0.10 names: an offline site server's
    queued wake drains on reconnect, so a 3600s window is an hour in which a
    wake nobody wants any more can still fire. Comparing against `WAKE_TTL_S`
    rather than a literal keeps the dispatcher and this test from drifting.
    """
    org = await seed_org(admin_engine, "ssd-i")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.7.0/24"]
    )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await wake_via_site_server(
            session,
            _engine_for(tmp_path),
            org_id=org.org_id,
            site_server_id=site_server_id,
            target_mac="a4:bb:6d:11:22:33",
        )

    rows = await _jobs(admin_engine, org.org_id)
    assert [r.expires_at - r.issued_at for r in rows] == [timedelta(seconds=WAKE_TTL_S)]
    assert timedelta(seconds=WAKE_TTL_S) == timedelta(minutes=5)


async def test_the_job_targets_the_site_server_and_is_signed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**THE FIXTURE HOLDS TWO DEVICES**, so `target_id` naming the site server
    is evidence rather than arithmetic on a one-element set.

    The signed payload's `device` field is the SITE SERVER's device id. The
    machine being woken is a parameter and has no `devices` row at all in this
    fixture — which is the normal case, since waking bare metal for provisioning
    is the primary use.
    """
    org = await seed_org(admin_engine, "ssd-j")
    ordinary = await seed_device(admin_engine, org.org_id, hostname="ordinary")
    await seed_agent(admin_engine, org.org_id, ordinary)
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.7.0/24"]
    )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job, envelope = await wake_via_site_server(
            session,
            _engine_for(tmp_path),
            org_id=org.org_id,
            site_server_id=site_server_id,
            target_mac="a4:bb:6d:11:22:33",
        )
        payload_device = envelope["job"]["device"]
        signature = envelope["sig"]

    rows = await _jobs(admin_engine, org.org_id)
    assert [(r.target_type, r.target_id) for r in rows] == [("device", org.device_id)]
    assert rows[0].signature is not None
    assert (payload_device, signature is not None) == (str(org.device_id), True)
    assert payload_device != str(ordinary)


async def test_nothing_is_published_by_this_function(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The CALLER publishes, after the tenant transaction commits.

    A job published inside the transaction could be handed to an agent
    describing state that then rolled back. Asserted as "the fake Redis received
    nothing", which is a different observation from "no exception was raised" —
    a dispatcher that published would raise nothing at all.
    """
    org = await seed_org(admin_engine, "ssd-k")
    await seed_agent(admin_engine, org.org_id, org.device_id)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.7.0/24"]
    )
    redis = _RecordingRedis()

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        _job, envelope = await wake_via_site_server(
            session,
            _engine_for(tmp_path),
            org_id=org.org_id,
            site_server_id=site_server_id,
            target_mac="a4:bb:6d:11:22:33",
        )

    assert redis.published == []
    # The envelope IS returned, so the caller has something to publish. An
    # implementation that published internally and returned nothing would pass
    # the assertion above and fail this one.
    assert envelope["kind"] == "job"


# --------------------------------------------------------------------------- #
# Tenancy and the dead capability column
# --------------------------------------------------------------------------- #


async def test_a_site_server_from_another_org_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """PINNED ON THE ADMIN ENGINE. *If `get_site_server`'s `SiteServer.org_id`
    predicate were deleted*, org A dispatches a wake through org B's site
    server, and org B's `jobs` table gains a row it never asked for — which is
    what the second half of the tuple observes."""
    org_a = await seed_org(admin_engine, "ssd-l")
    org_b = await seed_org(admin_engine, "ssd-m")
    await seed_agent(admin_engine, org_b.org_id, org_b.device_id)
    foreign = await seed_site_server(
        admin_engine, org_b.org_id, device_id=org_b.device_id, segments=["10.20.7.0/24"]
    )

    refused = False
    async with AsyncSession(admin_engine) as session:
        try:
            await wake_via_site_server(
                session,
                _engine_for(tmp_path),
                org_id=org_a.org_id,
                site_server_id=foreign,
                target_mac="a4:bb:6d:11:22:33",
            )
            await session.commit()
        except SiteServerNotFoundError:
            refused = True
            await session.rollback()

    assert (
        refused,
        await _jobs(admin_engine, org_a.org_id),
        await _jobs(admin_engine, org_b.org_id),
    ) == (True, [], [])


@pytest.mark.parametrize(
    "capabilities",
    [
        {},
        {"jobs": ["patch.apply"]},
        {"patch.apply": True},
    ],
)
async def test_capability_advertisement_is_never_consulted(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, capabilities: Any
) -> None:
    """**BOTH the empty map AND a map naming only another job type dispatch.**

    `agents.capabilities` is written by NOTHING in this repository — `DeviceFacts`
    has five fields and none is a capability, and no agent->server message kind
    carries them (spec §13.1). Advertisement is not a degraded path, it is a
    dead one, so there is no capability gate here and there must not be one.

    A single fixture cannot tell "no gate" from "a gate that happens to pass",
    which is why all three shapes are parametrised and all three must produce a
    job. Refusal — a real `JobStatus` — is the working mechanism instead.
    """
    org = await seed_org(admin_engine, "ssd-n")
    await seed_agent(admin_engine, org.org_id, org.device_id, capabilities=capabilities)
    site_server_id = await seed_site_server(
        admin_engine, org.org_id, device_id=org.device_id, segments=["10.20.7.0/24"]
    )

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await wake_via_site_server(
            session,
            _engine_for(tmp_path),
            org_id=org.org_id,
            site_server_id=site_server_id,
            target_mac="a4:bb:6d:11:22:33",
        )

    assert [r.params["broadcast"] for r in await _jobs(admin_engine, org.org_id)] == [
        "10.20.7.255"
    ]


async def test_an_unknown_site_server_id_is_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org = await seed_org(admin_engine, "ssd-o")
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(SiteServerNotFoundError):
            await wake_via_site_server(
                session,
                _engine_for(tmp_path),
                org_id=org.org_id,
                site_server_id=uuid4(),
                target_mac="a4:bb:6d:11:22:33",
            )
    assert await _jobs(admin_engine, org.org_id) == []
