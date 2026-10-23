"""IPAM's sweeper selection prefers a site server (site-server spec §6, §0.7,
§0.8).

**THE WHOLE TASK TURNS ON ONE ORDERING DECISION INSIDE ONE SQL STATEMENT, AND AN
ORDERING BUG THERE IS INVISIBLE IN ANY FIXTURE WHERE ONE CANDIDATE IS OBVIOUSLY
BETTER.** So `test_preferred_among_equals` holds both candidates online, with
IDENTICAL `last_seen_at`, and seeds the ids so the NON-site-server sorts first on
the final `d.id` tie-break. Delete the new term and that fixture returns the
other device — a different observable value, not a missing exception. Every
other test here is written the same way.

**THE ORDERING IMPLEMENTED IS §6.2's, NOT §6.1's, AND THE SPEC CONTRADICTS
ITSELF.** See `test_liveness_outranks_preference`, which is RED under §6.1's
literal ordering. That is the point of it.
"""

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.ipam.sweep import REFUSAL_COOLDOWN_DAYS, select_sweeper
from tests.ipam_fixtures import (
    seed_agent,
    seed_device,
    seed_org,
    seed_subnet,
    seed_vrf,
    upsert_observation_row,
)
from tests.site_server_fixtures import seed_site_server

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SEEN = NOW - timedelta(minutes=5)

# THE FINAL TIE-BREAK IS `d.id`, SO IT IS CONTROLLED RATHER THAN LEFT TO CHANCE.
# `_ids()` hands out a LOW/HIGH pair per call, unique across the session (the
# test database is shared, so fixed literals collide). The ordinary device
# always gets the LOW id and the site server the HIGH one -- so with the
# preference term removed, `d.id` decides and the ORDINARY device wins. Left to
# uuid7 the answer would depend on which was generated first, and the test would
# pass or fail for reasons unrelated to what it claims to check.
_counter = itertools.count(1)


def _ids() -> tuple[UUID, UUID]:
    n = next(_counter)
    return (
        UUID(f"00000000-0000-7000-8000-{n:012x}"),
        UUID(f"ffffffff-0000-7000-8000-{n:012x}"),
    )


class _Subnet:
    """The two attributes `select_sweeper` reads off an `IpamSubnet`."""

    def __init__(self, subnet_id: UUID, prefix: str) -> None:
        self.id = subnet_id
        self.prefix = prefix
        self.sweep_device_id: UUID | None = None


async def _observe(
    admin_engine: AsyncEngine, org_id: UUID, vrf_id: UUID, device_id: UUID, address: str
) -> None:
    await upsert_observation_row(
        admin_engine,
        org_id,
        vrf_id=vrf_id,
        address=address,
        source="inventory",
        method="nic",
        confidence="high",
        device_id=device_id,
        now=NOW - timedelta(hours=1),
    )


async def _two_candidates(
    admin_engine: AsyncEngine,
    tag: str,
    *,
    prefix: str = "10.20.7.0/24",
    declared: list[str] | None = None,
    site_server_online: bool = True,
    ordinary_online: bool = True,
    enabled: bool = True,
    promote: bool = True,
    observe_site_server: bool = True,
    site_server_capabilities: Any = None,
    ordinary_capabilities: Any = None,
) -> tuple[Any, UUID, UUID, _Subnet]:
    """One org, one subnet, and TWO equally-eligible devices.

    `ordinary` gets the LOW id, `site_server` the HIGH one, and both agents share
    `last_seen_at = SEEN` — so every ordering term above `d.id` is a tie except
    the one under test.
    """
    org = await seed_org(admin_engine, tag)
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix=prefix)

    low, high = _ids()
    ordinary = await seed_device(admin_engine, org.org_id, id=low, hostname="ordinary")
    site_server = await seed_device(admin_engine, org.org_id, id=high, hostname="branch")
    await seed_agent(
        admin_engine,
        org.org_id,
        ordinary,
        status="online" if ordinary_online else "offline",
        last_seen_at=SEEN,
        capabilities=ordinary_capabilities or {},
    )
    await seed_agent(
        admin_engine,
        org.org_id,
        site_server,
        status="online" if site_server_online else "offline",
        last_seen_at=SEEN,
        capabilities=site_server_capabilities or {},
    )

    host = prefix.rsplit(".", 1)[0]
    await _observe(admin_engine, org.org_id, vrf_id, ordinary, f"{host}.11")
    if observe_site_server:
        await _observe(admin_engine, org.org_id, vrf_id, site_server, f"{host}.12")
    else:
        # Observed somewhere ELSE, so the device exists and has an agent and is
        # simply not on this wire — which is what §0.8 says must exclude it.
        await _observe(admin_engine, org.org_id, vrf_id, site_server, "172.31.9.9")

    if promote:
        await seed_site_server(
            admin_engine,
            org.org_id,
            device_id=site_server,
            segments=declared or [prefix],
            enabled=enabled,
        )

    return org, ordinary, site_server, _Subnet(subnet_id, prefix)


async def _choose(engine: AsyncEngine, org_id: UUID, subnet: _Subnet) -> UUID | None:
    async with AsyncSession(engine) as session:
        return await select_sweeper(session, org_id, subnet, now=NOW)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The fixture the whole task rests on
# --------------------------------------------------------------------------- #


async def test_preferred_among_equals(admin_engine: AsyncEngine) -> None:
    """**THE ONE THAT MATTERS.**

    Both devices online, both with a current inventory observation inside the
    prefix, IDENTICAL `last_seen_at`, and the ids seeded so the ordinary device
    wins the `d.id` tie-break. Every ordering term except the new one is a tie,
    so the answer is decided by the preference and by nothing else.

    *If the site-server ORDER BY term were deleted* this returns the ORDINARY
    device — a different device id, not a missing exception. A fixture where the
    site server wins on `last_seen_at` anyway proves nothing.
    """
    org, ordinary, site_server, subnet = await _two_candidates(admin_engine, "ssp-a")
    chosen = await _choose(admin_engine, org.org_id, subnet)
    assert (chosen == site_server, chosen == ordinary) == (True, False)


async def test_liveness_outranks_preference(admin_engine: AsyncEngine) -> None:
    """**THIS TEST IS RED UNDER SPEC §6.1's LITERAL ORDERING, WHICH IS THE
    POINT.**

    §6.1 writes the preference term FIRST. §6.2's fallback table says an offline
    site server "loses the preference; an ordinary endpoint sweeps". Both cannot
    be true, and §6.2 is the one implemented: with preference first, an OFFLINE
    site server outranks an ONLINE ordinary endpoint and the sweep is dispatched
    to a machine that cannot receive it — the job sits `queued` until the 600s
    TTL expires it and the segment goes unswept for the cycle.

    IPAM's own `_designated` already settles the principle: designation "is not
    liveness", and a designated device whose agent is offline falls through. An
    inferred preference is weaker than an explicit designation, so it cannot
    outrank liveness where designation does not.
    """
    org, ordinary, site_server, subnet = await _two_candidates(
        admin_engine, "ssp-b", site_server_online=False
    )
    chosen = await _choose(admin_engine, org.org_id, subnet)
    assert (chosen == ordinary, chosen == site_server) == (True, False)


# --------------------------------------------------------------------------- #
# Preference reorders; it never adds (§0.8)
# --------------------------------------------------------------------------- #


async def test_declared_but_unobserved_is_not_a_candidate(
    admin_engine: AsyncEngine,
) -> None:
    """§0.8: a claim earns preference, only EVIDENCE earns candidacy.

    The site server is promoted and declares the prefix, but its only current
    observation is on `172.31.9.9` — it is not on this wire. It must not be
    chosen, however loudly the registry claims it serves the segment, because
    dispatching anyway emits somebody's probe from the wrong place. The ordinary
    device is here so "not chosen" is distinguishable from "nothing was chosen".
    """
    org, ordinary, site_server, subnet = await _two_candidates(
        admin_engine, "ssp-c", observe_site_server=False
    )
    chosen = await _choose(admin_engine, org.org_id, subnet)
    assert (chosen == ordinary, chosen == site_server) == (True, False)


async def test_containment_not_equality(admin_engine: AsyncEngine) -> None:
    """A branch server declared for `10.20.0.0/16` is preferred for
    `10.20.7.0/24`: it serves a branch, not one VLAN."""
    org, _ordinary, site_server, subnet = await _two_candidates(
        admin_engine, "ssp-d", declared=["10.20.0.0/16"]
    )
    assert await _choose(admin_engine, org.org_id, subnet) == site_server


async def test_a_segment_that_merely_looks_similar_does_not_match(
    admin_engine: AsyncEngine,
) -> None:
    """`10.200.0.0/16` starts with `10.20` and contains nothing in
    `10.20.7.0/24`. Native `cidr` gets this right; text comparison would not,
    which is why the column is `cidr[]`."""
    org, ordinary, site_server, subnet = await _two_candidates(
        admin_engine, "ssp-e", declared=["10.200.0.0/16"]
    )
    chosen = await _choose(admin_engine, org.org_id, subnet)
    assert (chosen == ordinary, chosen == site_server) == (True, False)


# --------------------------------------------------------------------------- #
# No site-server exemption from any existing rule
# --------------------------------------------------------------------------- #


async def test_refusal_cooldown_applies_to_site_servers_too(
    admin_engine: AsyncEngine,
) -> None:
    """**TWO PROMOTED SITE SERVERS**, one that refused a `net.sweep` INSIDE the
    seven-day cooldown and one that refused OUTSIDE it — so the test observes
    WHICH was chosen rather than "somebody was".

    There is no site-server exemption from `REFUSAL_COOLDOWN_DAYS` and there
    must not be one: a refusing device re-selected every hour forever is what
    the cooldown exists to stop, and a privileged host is not a reason to
    re-probe from a machine that said no.
    """
    org = await seed_org(admin_engine, "ssp-f")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.20.7.0/24"
    )
    low, high = _ids()
    recent = await seed_device(admin_engine, org.org_id, id=low, hostname="refused-recently")
    stale = await seed_device(admin_engine, org.org_id, id=high, hostname="refused-long-ago")
    # DISTINCT addresses per device. `uq_ipam_obs_key` is
    # `(org_id, vrf_id, address, source, mac_key)` and the upsert does NOT
    # update `device_id`, so seeding both devices at one address silently leaves
    # a single observation still owned by the FIRST -- and the second device
    # quietly stops being a candidate for a reason that has nothing to do with
    # what this test is about.
    for offset, device in enumerate((recent, stale), start=20):
        await seed_agent(
            admin_engine, org.org_id, device, status="online", last_seen_at=SEEN
        )
        await _observe(admin_engine, org.org_id, vrf_id, device, f"10.20.7.{offset}")
        await seed_site_server(
            admin_engine, org.org_id, device_id=device, segments=["10.20.7.0/24"]
        )

    await _refused_job(admin_engine, org.org_id, recent, NOW - timedelta(days=1))
    await _refused_job(
        admin_engine, org.org_id, stale, NOW - timedelta(days=REFUSAL_COOLDOWN_DAYS + 2)
    )

    chosen = await _choose(admin_engine, org.org_id, _Subnet(subnet_id, "10.20.7.0/24"))
    assert (chosen == stale, chosen == recent) == (True, False)


async def _refused_job(
    engine: AsyncEngine, org_id: UUID, device_id: UUID, finished_at: datetime
) -> None:
    async with AsyncSession(engine) as session:
        await session.execute(
            text(
                "INSERT INTO jobs (id, org_id, type, target_type, target_id, params,"
                "                  status, nonce, issued_at, expires_at, finished_at,"
                "                  attempts)"
                " VALUES (gen_random_uuid(), :org, 'net.sweep', 'device', :device,"
                "         '{}'::jsonb, 'refused', gen_random_uuid()::text, :ts, :ts,"
                "         :ts, 1)"
            ),
            {"org": org_id, "device": device_id, "ts": finished_at},
        )
        await session.commit()


async def test_capability_advertisement_is_not_the_mechanism(
    admin_engine: AsyncEngine,
) -> None:
    """Constraint 9: `agents.capabilities` is written by NOTHING in this repo.

    An empty map means "this agent has not told us" and is tolerated; a
    NON-EMPTY map that does not name `net.sweep` is taken at its word. A site
    server gets no exemption from either rule.

    Two fixtures, two different chosen devices: with `capabilities = {}` the
    site server is preferred; with capabilities naming only `patch.apply` it is
    skipped exactly as any device would be. One fixture could not tell "the
    filter is applied to site servers" from "the filter never runs".
    """
    org_a, _ord_a, site_a, subnet_a = await _two_candidates(
        admin_engine, "ssp-g", site_server_capabilities={}
    )
    org_b, ord_b, site_b, subnet_b = await _two_candidates(
        admin_engine, "ssp-h", site_server_capabilities={"jobs": ["patch.apply"]}
    )

    assert await _choose(admin_engine, org_a.org_id, subnet_a) == site_a
    chosen_b = await _choose(admin_engine, org_b.org_id, subnet_b)
    assert (chosen_b == ord_b, chosen_b == site_b) == (True, False)


async def test_a_disabled_site_server_loses_the_preference(
    admin_engine: AsyncEngine,
) -> None:
    """`enabled=false` is the demote-without-delete, and the preference subquery
    requires `s.enabled`."""
    org, ordinary, site_server, subnet = await _two_candidates(
        admin_engine, "ssp-i", enabled=False
    )
    chosen = await _choose(admin_engine, org.org_id, subnet)
    assert (chosen == ordinary, chosen == site_server) == (True, False)


async def test_a_demoted_site_server_loses_the_preference(
    admin_engine: AsyncEngine,
) -> None:
    """Separate from the disabled case: `enabled=false` and "the row is gone"
    are different states reached by different operator actions, and a subquery
    that checked only one of them would pass the other's test."""
    org, ordinary, site_server, subnet = await _two_candidates(admin_engine, "ssp-j")
    assert await _choose(admin_engine, org.org_id, subnet) == site_server

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM site_server WHERE device_id = :device"),
            {"device": site_server},
        )
        await session.commit()

    chosen = await _choose(admin_engine, org.org_id, subnet)
    assert (chosen == ordinary, chosen == site_server) == (True, False)


async def test_an_operators_designation_still_wins(admin_engine: AsyncEngine) -> None:
    """`_designated` short-circuits BEFORE the ordering, and that is correct: an
    explicit operator choice outranks an inferred preference."""
    org, ordinary, site_server, subnet = await _two_candidates(admin_engine, "ssp-k")
    subnet.sweep_device_id = ordinary
    chosen = await _choose(admin_engine, org.org_id, subnet)
    assert (chosen == ordinary, chosen == site_server) == (True, False)


async def test_no_agent_still_occurs(admin_engine: AsyncEngine) -> None:
    """A promoted site server with no observation on the wire and no other
    candidate still yields `None` — `no_agent` remains a first-class outcome and
    the preference did not quietly become a candidacy."""
    org = await seed_org(admin_engine, "ssp-l")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.20.7.0/24"
    )
    device = await seed_device(admin_engine, org.org_id, hostname="elsewhere")
    await seed_agent(admin_engine, org.org_id, device, status="online", last_seen_at=SEEN)
    await _observe(admin_engine, org.org_id, vrf_id, device, "172.31.9.9")
    await seed_site_server(
        admin_engine, org.org_id, device_id=device, segments=["10.20.7.0/24"]
    )

    assert await _choose(admin_engine, org.org_id, _Subnet(subnet_id, "10.20.7.0/24")) is None


# --------------------------------------------------------------------------- #
# Tenancy and determinism
# --------------------------------------------------------------------------- #


async def test_another_orgs_site_server_row_does_not_influence_this_org(
    admin_engine: AsyncEngine,
) -> None:
    """**PINNED ON THE ADMIN ENGINE**, where RLS is not the thing rejecting.

    The preference subquery carries `s.org_id = :org` explicitly. Through
    `org_session` RLS would hide org B's row before the predicate mattered, and
    the test would pass with the predicate deleted — the defect that has shipped
    four times in this repo.

    The fixture is built so the predicate is THE ONLY thing separating the rows:
    org B's `site_server` row names org A's OWN device id. Nothing else about
    that row distinguishes it — same device, same segment — so if `s.org_id` were
    dropped, org A's `_HIGH` device would gain the preference and be chosen.
    """
    org, ordinary, site_server, subnet = await _two_candidates(
        admin_engine, "ssp-m", promote=False
    )
    other = await seed_org(admin_engine, "ssp-n")
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "INSERT INTO site_server (id, org_id, device_id, name, segments, roles,"
                "                         enabled, notes, created_at, updated_at)"
                " VALUES (gen_random_uuid(), :org, :device, 'cross-org',"
                "         CAST(:segments AS cidr[]), CAST('{wake}' AS text[]), true, '',"
                "         now(), now())"
            ),
            # ORG B's row, naming ORG A's device. The FK to `devices` does not
            # constrain the org, so this row is insertable -- and it is exactly
            # the row a missing `s.org_id` predicate would honour.
            {"org": other.org_id, "device": site_server, "segments": ["10.20.7.0/24"]},
        )
        await session.commit()

    chosen = await _choose(admin_engine, org.org_id, subnet)
    assert (chosen == ordinary, chosen == site_server) == (True, False)


async def test_selection_is_deterministic_across_repeated_runs(
    admin_engine: AsyncEngine,
) -> None:
    """The same fixture picks the same device every time. An unordered `LIMIT 1`
    passes a single-candidate test and picks a different machine on every run in
    production."""
    org, _ordinary, site_server, subnet = await _two_candidates(admin_engine, "ssp-o")
    answers = [await _choose(admin_engine, org.org_id, subnet) for _ in range(5)]
    assert answers == [site_server] * 5
