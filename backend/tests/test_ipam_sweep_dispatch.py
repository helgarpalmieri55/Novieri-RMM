"""`net.sweep` dispatch: the authorized target set on the near side of the
signing boundary (`docs/modules/ipam/spec.md` §5.2, §5.3, §6.2, §6.3, §11.3).

**SECURITY-CRITICAL.** This is the first code in the module that can put packets
on somebody else's wire. Every fixture below is built so a mutation that WIDENS
what gets probed, or that lets an unauthorized target reach `JobEngine.create`,
fails loudly.

WHAT THESE FIXTURES DELIBERATELY VARY -- fixture blindness is this repository's
defining defect, so each guard is paired with a fixture in which the guarded and
the unguarded path produce DIFFERENT OBSERVABLE VALUES:

* **The "not authorized" case seeds a REVOKED authorization for the exact
  prefix.** A test that seeds nothing and asserts a refusal passes against a
  function that refuses everything, and against one that never looks. The
  revoked row is what makes the refusal evidence: delete the `revoked_at`
  predicate and this fixture dispatches a real sweep of 254 hosts.
* **The IPv6 and `container` cases seed an ACTIVE authorization covering the
  subnet.** `authorized_targets` returns `[]` for both -- a safe deny default,
  and the WRONG ERROR. With the authorization present, a dispatcher that leans
  on that `[]` tells the operator "not authorized" when the truth is "not
  enumerable" (§0.5), and these tests observe `IpamAuthorizationError` where
  `IpamTooLargeError`/`IpamValidationError` belongs.
* **The `no_agent` case seeds a device that IS online and DOES have a current
  inventory observation -- just outside the subnet.** Falling back to "any
  device in the org" then dispatches a probe of somebody's segment from an
  unrelated machine, which is a different observable value, not a missing
  exception.
* **The refusal-cooldown case seeds TWO refused devices**, one inside the
  7-day window and one outside it, so the test observes WHICH device was
  chosen. A single refused device would let "the cooldown works" and "no device
  is ever eligible" produce the same answer.
* **The supernet case asserts the exact `/24` string.** The dangerous defect is
  not an empty target list, it is a wide one: the `max_targets` cap bounds how
  many packets a widened scope emits, never where they go.
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.modules.ipam.sweep as sweep_module
from app.core.signing import LocalEd25519Signer
from app.jobs.engine import JobEngine
from app.models import AuditEvent, IpamScanAuthorization, IpamSweepRun, Job, JobStatus
from app.modules.ipam.errors import (
    IpamAuthorizationError,
    IpamNotFoundError,
    IpamTooLargeError,
    IpamValidationError,
)
from app.modules.ipam.handler import register_job_types
from app.modules.ipam.net import scope_digest
from app.modules.ipam.schema import NetSweepParams
from app.modules.ipam.subnets import get_subnet
from app.modules.ipam.sweep import (
    REFUSAL_COOLDOWN_DAYS,
    SWEEP_DISPATCH_INTERVAL_S,
    SWEEP_TTL_S,
    SweepDispatch,
    create_sweep,
    due_subnet_ids,
    select_sweeper,
)
from tests.ipam_fixtures import (
    seed_agent,
    seed_device,
    seed_org,
    seed_subnet,
    seed_vrf,
    upsert_observation_row,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _Fixture:
    def __init__(self, org: Any, vrf_id: UUID, subnet_id: UUID) -> None:
        self.org = org
        self.org_id = org.org_id
        self.user_id = org.user_id
        self.vrf_id = vrf_id
        self.subnet_id = subnet_id


async def _fixture(
    engine: AsyncEngine, tag: str, prefix: str = "10.70.0.0/24", **kw: Any
) -> _Fixture:
    # Registered per fixture, not once at import: the three registration tests
    # at the bottom of this file POP `net.sweep` out of the kernel registry to
    # prove each process re-adds it, and a dispatch test that assumed a
    # module-import side effect would then fail on ordering rather than on
    # anything real.
    register_job_types()
    org = await seed_org(engine, tag)
    vrf_id = await seed_vrf(engine, org.org_id)
    subnet_id = await seed_subnet(
        engine, org.org_id, vrf_id=vrf_id, prefix=prefix, **kw
    )
    return _Fixture(org, vrf_id, subnet_id)


async def _authorize(
    engine: AsyncEngine,
    fx: _Fixture,
    prefix: str,
    *,
    revoked: bool = False,
) -> UUID:
    """An authorization row, seeded directly. Written on the admin engine rather
    than through `create_authorization` so a dispatch test is never set up by the
    service whose predicates it depends on."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = IpamScanAuthorization(
            org_id=fx.org_id,
            vrf_id=fx.vrf_id,
            prefix=prefix,
            authorized_by=fx.user_id,
            authorized_at=NOW - timedelta(days=1),
            revoked_at=NOW - timedelta(hours=1) if revoked else None,
            revoked_by=fx.user_id if revoked else None,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _sweeper(
    engine: AsyncEngine,
    fx: _Fixture,
    address: str,
    *,
    status: str = "online",
    last_seen_at: datetime | None = None,
) -> UUID:
    """A device with a CURRENT inventory observation at `address` and an agent.

    Both halves matter: the auto-selection query joins `agents` for liveness and
    `ipam_observation` for "is this machine on that segment", so a device missing
    either is not a candidate.
    """
    device_id = await seed_device(engine, fx.org_id)
    await seed_agent(
        engine,
        fx.org_id,
        device_id,
        status=status,
        last_seen_at=last_seen_at or NOW - timedelta(minutes=1),
    )
    await upsert_observation_row(
        engine,
        fx.org_id,
        vrf_id=fx.vrf_id,
        address=address,
        source="inventory",
        method="nic",
        confidence="high",
        device_id=device_id,
        now=NOW - timedelta(hours=1),
    )
    return device_id


def _engine_for(tmp_path: Path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "job-signing.key"))


async def _dispatch(
    engine: AsyncEngine, fx: _Fixture, tmp_path: Path, **kw: Any
) -> SweepDispatch:
    """Call the service and COMMIT, which is what every real caller does.

    **THIS HELPER NO LONGER SWALLOWS AND RE-RAISES A REFUSAL, BECAUSE A REFUSAL
    IS NO LONGER RAISED.** It used to `except IpamError: commit; raise`, on the
    stated grounds that "the API path differs deliberately -- it lets the error
    escape the `org_session` block so a 403 leaves nothing behind". That was the
    bug, written down as a design: the API path left the refusal's own evidence
    behind too, on every request it ever served. `create_sweep` now returns the
    refusal (`SweepDispatch.refusal`) and the caller raises it after committing.

    The three errors still raised -- container, IPv6, cross-org -- add nothing
    to the database, so there is nothing here to commit for them.
    """
    async with AsyncSession(engine, expire_on_commit=False) as session:
        dispatch = await create_sweep(
            session,
            _engine_for(tmp_path),
            fx.org_id,
            subnet_id=fx.subnet_id,
            actor_id=fx.user_id,
            trigger=kw.pop("trigger", "manual"),
            now=kw.pop("now", NOW),
            **kw,
        )
        await session.commit()
        return dispatch


async def _jobs(engine: AsyncEngine, org_id: UUID) -> list[Job]:
    async with AsyncSession(engine) as session:
        return list(
            (await session.execute(select(Job).where(Job.org_id == org_id)))
            .scalars()
            .all()
        )


async def _job_count(engine: AsyncEngine, org_id: UUID) -> int:
    async with AsyncSession(engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Job).where(Job.org_id == org_id)
                )
            ).scalar_one()
        )


async def _runs(engine: AsyncEngine, org_id: UUID) -> list[IpamSweepRun]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(IpamSweepRun).where(IpamSweepRun.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )


async def _audit_actions(engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------- #
# The refusal is on the NEAR side of the signing boundary (§6.3)
# --------------------------------------------------------------------------- #


async def test_an_unauthorized_subnet_refuses_before_any_job_row_exists(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.3: no job row, no canonical payload, NO SIGNATURE, for a target
    nobody authorized.

    THE FIXTURE HOLDS A REVOKED AUTHORIZATION FOR THIS EXACT PREFIX, so `[]` is
    the product of a predicate rather than of an empty table. *If the refusal
    moved to after `JobEngine.create`* this observes one `jobs` row -- a signed
    envelope for a segment nobody authorized. *If the `revoked_at` predicate were
    dropped* it observes a dispatched run and a job for `10.70.0.0/24`.

    The refusal is RETURNED now, not raised, so the assertion is on
    `SweepDispatch.refusal` -- and on `envelope is None`, which is the property
    that says nothing was signed regardless of what the exception says.
    """
    fx = await _fixture(admin_engine, "unauth")
    await _authorize(admin_engine, fx, "10.70.0.0/24", revoked=True)
    await _sweeper(admin_engine, fx, "10.70.0.9")

    dispatch = await _dispatch(admin_engine, fx, tmp_path)
    assert isinstance(dispatch.refusal, IpamAuthorizationError)
    assert dispatch.envelope is None

    assert await _job_count(admin_engine, fx.org_id) == 0
    runs = await _runs(admin_engine, fx.org_id)
    assert [(r.state, r.error) for r in runs] == [("refused", "not authorized")]
    assert runs[0].job_id is None
    assert runs[0].scope_digest == ""


async def test_the_refused_run_reaches_the_database_through_the_route(
    api_env,
) -> None:
    """**THE ROUTE, NOT THE SERVICE** -- and that swap is the whole test.

    This test used to call `create_sweep` directly through a helper that
    COMMITTED THE SESSION ITSELF, which the route does not. `create_sweep` added
    the `refused` row and raised inside the caller's `org_session` block;
    `session.begin()` rolls back on any exception leaving it, so on the real
    HTTP path the row was written and immediately discarded. The test passed the
    whole time, because it had already committed. **The record of a refusal is
    the entire point of refusing on the near side of the signing boundary** --
    a refusal nobody can audit is indistinguishable from a request nobody made
    (invariant #4, §2.10).

    *If the refusal were raised inside the block again*, this observes zero
    `ipam_sweep_run` rows behind an unchanged 403.
    """
    from sqlalchemy.ext.asyncio import AsyncSession as _Session

    from app.modules.ipam.handler import register_job_types as _register

    _register()
    org_id = await api_env.create_org("sweep-refusal")
    vrf_id = await seed_vrf(api_env, org_id)
    # A REAL, ACTIVE, WRONG SCOPE -- 192.168.5.0/24 is authorized and the subnet
    # is 10.70.0.0/24 -- so `[]` is the product of the intersection rather than
    # of an empty authorization table.
    subnet_id = await seed_subnet(api_env, org_id, vrf_id=vrf_id, prefix="10.70.0.0/24")
    headers = await api_env.headers(
        org_id, await api_env.create_user(org_id, "technician")
    )

    refused = await api_env.client.post(
        f"/api/v1/ipam/subnets/{subnet_id}/sweep", headers=headers
    )
    assert refused.status_code == 403, refused.text

    async with _Session(api_env.admin_engine) as session:
        runs = list(
            (
                await session.execute(
                    select(IpamSweepRun).where(IpamSweepRun.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    assert [(r.state, r.trigger, r.target_count, r.job_id) for r in runs] == [
        ("refused", "manual", 0, None)
    ]


# --------------------------------------------------------------------------- #
# The intersected target set and its digest (§6.2)
# --------------------------------------------------------------------------- #


async def test_the_signed_params_carry_the_intersected_targets_not_the_supernet(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§0.12: the intersection with `S.prefix` is taken FIRST, so an
    authorization broader than the subnet cannot widen anything.

    *If the dispatcher passed `[str(subnet.prefix)]`* -- or the authorization's
    own prefix -- this observes `['10.0.0.0/8']` in the SIGNED params: sixteen
    million addresses probed on the strength of one `/24` existing. The
    `max_targets` cap does not save that case; it bounds how many packets a
    widened scope emits, never where they go.
    """
    fx = await _fixture(admin_engine, "inter")
    await _authorize(admin_engine, fx, "10.0.0.0/8")
    await _sweeper(admin_engine, fx, "10.70.0.9")

    run, envelope, _refusal = await _dispatch(admin_engine, fx, tmp_path)
    assert envelope is not None
    params = envelope["job"]["params"]
    assert params["targets"] == ["10.70.0.0/24"]
    assert params["scope_digest"] == scope_digest(["10.70.0.0/24"])
    assert run.scope_digest == params["scope_digest"]
    assert run.target_count == 256


async def test_the_dispatcher_and_the_result_handler_share_one_digest_function(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """`net.scope_digest` HAS EXACTLY ONE IMPLEMENTATION and both sides call it.

    Two digests that drift is how an authorized-scope check silently stops
    matching what was authorized -- both sides agree, and neither is checking
    anything. This asserts the dispatcher's stored value against the shared
    function; `test_ipam_sweep_result.py` asserts the handler recomputes with the
    same one over the job's own signed `targets`.

    *If the dispatcher computed its own sha256* over, say, a comma-joined
    string, this observes a different 64-character hex value.
    """
    fx = await _fixture(admin_engine, "digest")
    await _authorize(admin_engine, fx, "10.70.0.0/25")
    await _sweeper(admin_engine, fx, "10.70.0.9")

    _run, envelope, _refusal = await _dispatch(admin_engine, fx, tmp_path)
    assert envelope is not None
    targets = envelope["job"]["params"]["targets"]
    assert targets == ["10.70.0.0/25"]
    assert envelope["job"]["params"]["scope_digest"] == scope_digest(targets)
    assert scope_digest(targets) != scope_digest(["10.70.0.0/24"])


async def test_an_excluded_range_is_subtracted_from_the_signed_targets(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§6.2's holes reach the signed params, not just the service's return value.

    *If the dispatcher re-derived the targets from `subnet.prefix`* after calling
    `authorized_targets`, this observes `10.70.0.150` inside the target set.
    """
    from app.modules.ipam.models import IpamRange

    fx = await _fixture(admin_engine, "excl")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    await _sweeper(admin_engine, fx, "10.70.0.9")
    async with AsyncSession(admin_engine) as session:
        session.add(
            IpamRange(
                org_id=fx.org_id,
                subnet_id=fx.subnet_id,
                kind="excluded",
                start_address="10.70.0.100",
                end_address="10.70.0.200",
            )
        )
        await session.commit()

    _run, envelope, _refusal = await _dispatch(admin_engine, fx, tmp_path)
    assert envelope is not None
    targets = envelope["job"]["params"]["targets"]
    from ipaddress import ip_address, ip_network

    covered = any(ip_address("10.70.0.150") in ip_network(t) for t in targets)
    assert not covered, f"an excluded address reached the signed targets: {targets}"
    assert any(ip_address("10.70.0.9") in ip_network(t) for t in targets)


# --------------------------------------------------------------------------- #
# The signed job itself (§5.2, invariant #1)
# --------------------------------------------------------------------------- #


async def test_the_ttl_is_six_hundred_seconds_not_the_kernel_default(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§5.2/§6.3: the TTL bounds how long a REVOKED authorization can still be
    executed by an in-flight signed job.

    *If `ttl_s=` were dropped* this observes 3600 -- an hour during which a
    revocation an operator made deliberately does not stop the probe.
    """
    fx = await _fixture(admin_engine, "ttl")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    await _sweeper(admin_engine, fx, "10.70.0.9")
    await _dispatch(admin_engine, fx, tmp_path)

    job = (await _jobs(admin_engine, fx.org_id))[0]
    assert (job.expires_at - job.issued_at) == timedelta(seconds=600)
    assert SWEEP_TTL_S == 600


async def test_every_dispatched_sweep_carries_a_signature(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """INVARIANT #1, asserted rather than assumed: no unsigned command path
    exists, not for testing, not for a quick internal tool."""
    fx = await _fixture(admin_engine, "sig")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    await _sweeper(admin_engine, fx, "10.70.0.9")
    _run, envelope, _refusal = await _dispatch(admin_engine, fx, tmp_path)

    job = (await _jobs(admin_engine, fx.org_id))[0]
    assert job.signature, "a net.sweep job left the dispatcher unsigned"
    assert envelope is not None
    assert envelope["sig"] == job.signature
    assert envelope["job"]["type"] == "net.sweep"


async def test_the_params_validate_against_the_registered_job_type(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """`JobEngine.create` looks the type up in the kernel registry and validates
    the params through its model, so a `net.sweep` that dispatches at all proves
    the job type is registered in THIS process. The round-trip through
    `NetSweepParams` pins the stored shape as well."""
    fx = await _fixture(admin_engine, "ptype")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    await _sweeper(admin_engine, fx, "10.70.0.9")
    await _dispatch(admin_engine, fx, tmp_path)

    job = (await _jobs(admin_engine, fx.org_id))[0]
    params = NetSweepParams(**job.params)
    assert params.subnet_id == str(fx.subnet_id)
    assert params.max_targets == 4096
    assert params.rate_pps == 50
    assert params.budget_s == 300
    assert params.probe_timeout_ms == 500


async def test_the_dispatch_is_audited_with_the_scope_it_signed(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """INVARIANT #4, §11.3's `ipam.sweep_dispatched` row: run id, job id, target
    count, `scope_digest`, trigger -- mirroring `patch.scan_dispatched`.

    The digest is in the audit record because it is the only value that ties the
    evidence to what was actually signed.
    """
    fx = await _fixture(admin_engine, "audit")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    device_id = await _sweeper(admin_engine, fx, "10.70.0.9")
    run, _envelope, _refusal = await _dispatch(admin_engine, fx, tmp_path)

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == fx.org_id,
                    AuditEvent.action == "ipam.sweep_dispatched",
                )
            )
        ).scalar_one()
    assert row.target_type == "device"
    assert row.target_id == str(device_id)
    assert row.meta["sweep_run_id"] == str(run.id)
    assert row.meta["scope_digest"] == scope_digest(["10.70.0.0/24"])
    assert row.meta["target_count"] == 256
    assert row.meta["trigger"] == "manual"


# --------------------------------------------------------------------------- #
# The two refusals that are NOT "not authorized" (§0.5, §0.2)
# --------------------------------------------------------------------------- #


async def test_an_ipv6_subnet_is_refused_as_unenumerable_not_as_unauthorized(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§0.5: an ICMP sweep of a /64 is a thought experiment, not a hard query.

    THE FIXTURE AUTHORIZES THE PREFIX. `authorized_targets` returns `[]` for
    every IPv6 subnet -- a safe deny default and the wrong ERROR -- so a
    dispatcher that leans on that emptiness tells the operator "not authorized"
    about a scope a human explicitly authorized. *If the family check were
    removed* this observes `IpamAuthorizationError`, and the operator spends the
    afternoon re-granting a scope that was never the problem.
    """
    fx = await _fixture(admin_engine, "v6", prefix="2001:db8:1::/64")
    await _authorize(admin_engine, fx, "2001:db8::/32")
    await _sweeper(admin_engine, fx, "2001:db8:1::9")

    with pytest.raises(IpamTooLargeError) as excinfo:
        await _dispatch(admin_engine, fx, tmp_path)
    assert "enumerate" in str(excinfo.value)
    assert await _job_count(admin_engine, fx.org_id) == 0
    assert [r.state for r in await _runs(admin_engine, fx.org_id)] == []


async def test_a_container_subnet_is_refused_as_a_container_not_as_unauthorized(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§0.2: a `container` exists so the tree reads and holds no addresses.

    Same shape as the IPv6 case and for the same reason: the authorization is
    present and active, so *if the `kind` check were removed* this observes
    `IpamAuthorizationError` -- a refusal that names the wrong cause.
    """
    fx = await _fixture(admin_engine, "cont", prefix="10.0.0.0/8", kind="container")
    await _authorize(admin_engine, fx, "10.0.0.0/8")
    await _sweeper(admin_engine, fx, "10.0.0.9")

    with pytest.raises(IpamValidationError) as excinfo:
        await _dispatch(admin_engine, fx, tmp_path)
    assert "container" in str(excinfo.value)
    assert not isinstance(excinfo.value, IpamAuthorizationError)
    assert await _job_count(admin_engine, fx.org_id) == 0


async def test_a_cross_org_subnet_id_is_not_found_on_the_admin_engine(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """PINNED ON THE ADMIN ENGINE (invariant #5). Through `org_session` RLS
    rejects first and this passes with the service's own `org_id` predicate
    deleted; here the predicate is the only thing rejecting.

    404 and never 403: a 403 confirms the id exists, which is a cross-tenant
    existence oracle.
    """
    victim = await _fixture(admin_engine, "victim")
    await _authorize(admin_engine, victim, "10.70.0.0/24")
    attacker = await _fixture(admin_engine, "attacker", prefix="10.71.0.0/24")

    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IpamNotFoundError):
            await create_sweep(
                session,
                _engine_for(tmp_path),
                attacker.org_id,
                subnet_id=victim.subnet_id,
                actor_id=attacker.user_id,
                trigger="manual",
                now=NOW,
            )
    assert await _job_count(admin_engine, victim.org_id) == 0
    assert await _runs(admin_engine, victim.org_id) == []


# --------------------------------------------------------------------------- #
# Who sweeps (§5.3)
# --------------------------------------------------------------------------- #


async def test_no_observing_device_records_no_agent_and_dispatches_nothing(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§5.3 step 3: `no_agent` is a FIRST-CLASS OUTCOME, not an error swallowed
    in a log -- the subnet page shows it and Gate E reads it.

    THE FIXTURE HAS AN ONLINE DEVICE WITH A CURRENT OBSERVATION, in a different
    segment. *If auto-selection fell back to "any device in the org"* this
    observes a signed job dispatched to a machine that is not on that wire --
    which is both useless and a probe emitted from the wrong place.
    """
    fx = await _fixture(admin_engine, "noagent")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    elsewhere = await _sweeper(admin_engine, fx, "192.168.44.9")

    run, envelope, _refusal = await _dispatch(admin_engine, fx, tmp_path)
    assert envelope is None
    assert run.state == "no_agent"
    assert run.device_id is None
    assert run.job_id is None
    assert await _job_count(admin_engine, fx.org_id) == 0
    assert elsewhere is not None  # the device exists; it was simply not eligible


async def test_the_designated_sweeper_is_preferred_and_recorded(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§5.3 step 1. The chosen device is written to `ipam_sweep_run.device_id`
    so "who probed this segment" is answerable after the fact."""
    fx = await _fixture(admin_engine, "desig")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    auto = await _sweeper(admin_engine, fx, "10.70.0.9")
    designated = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, designated, status="online")
    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        subnet.sweep_device_id = designated
        await session.commit()

    run, envelope, _refusal = await _dispatch(admin_engine, fx, tmp_path)
    assert envelope is not None
    assert run.device_id == designated, "the designated sweeper was not used"
    assert run.device_id != auto
    assert UUID(envelope["job"]["device"]) == designated


async def test_a_designated_sweeper_whose_agent_is_offline_falls_through(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§5.3 step 1's conditions are conjunctive: designation alone is not
    liveness. *If the online check were dropped* this observes the job addressed
    to a machine that cannot receive it, and a subnet that silently stops being
    swept while its page shows a dispatched run."""
    fx = await _fixture(admin_engine, "offdes")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    auto = await _sweeper(admin_engine, fx, "10.70.0.9")
    designated = await seed_device(admin_engine, fx.org_id)
    await seed_agent(
        admin_engine,
        fx.org_id,
        designated,
        status="offline",
        last_seen_at=NOW - timedelta(days=30),
    )
    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        subnet.sweep_device_id = designated
        await session.commit()

    run, envelope, _refusal = await _dispatch(admin_engine, fx, tmp_path)
    assert envelope is not None
    assert run.device_id == auto


async def test_auto_selection_is_deterministic_online_then_recency_then_id(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§5.3 step 2's exact ordering, with three candidates that differ on each
    key in turn. An unordered `LIMIT 1` passes a single-candidate test and picks
    a different machine on every run here.

    The offline candidate is the MOST RECENTLY SEEN, so a query that ordered by
    recency alone would choose it -- that is what makes the `status='online'`
    term observable.
    """
    fx = await _fixture(admin_engine, "order")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    await _sweeper(
        admin_engine, fx, "10.70.0.7", status="offline", last_seen_at=NOW
    )
    stale_online = await _sweeper(
        admin_engine, fx, "10.70.0.8", last_seen_at=NOW - timedelta(hours=6)
    )
    fresh_online = await _sweeper(
        admin_engine, fx, "10.70.0.9", last_seen_at=NOW - timedelta(minutes=1)
    )

    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        chosen = await select_sweeper(session, fx.org_id, subnet, now=NOW)
    assert chosen == fresh_online
    assert chosen != stale_online


async def test_a_device_whose_sweep_was_refused_is_skipped_for_seven_days(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§5.3: `agents.capabilities` is a shipped column nothing in this repository
    populates, so refusal -- not advertisement -- is the load-bearing fallback.

    TWO REFUSED DEVICES, one inside the window and one outside it, so the test
    observes WHICH was chosen rather than "something was chosen". *If the
    cooldown were dropped* this observes the recently refused device, which
    refuses again, forever, every hour. *If the cooldown had no upper bound* it
    observes `None` and the segment is never swept again.
    """
    fx = await _fixture(admin_engine, "refuse")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    recent = await _sweeper(
        admin_engine, fx, "10.70.0.8", last_seen_at=NOW - timedelta(seconds=1)
    )
    old = await _sweeper(
        admin_engine, fx, "10.70.0.9", last_seen_at=NOW - timedelta(hours=5)
    )
    await _refused_job(admin_engine, fx, recent, finished_at=NOW - timedelta(days=1))
    await _refused_job(admin_engine, fx, old, finished_at=NOW - timedelta(days=8))

    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        chosen = await select_sweeper(session, fx.org_id, subnet, now=NOW)
    assert chosen == old, "the device that refused a sweep yesterday was chosen"


async def test_a_designated_sweeper_inside_the_refusal_cooldown_falls_through(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """**THE COOLDOWN ON THE DESIGNATED PATH, WHICH NOTHING EXERCISED.**

    The test above drives auto-selection, where the cooldown lives in the SQL's
    `NOT EXISTS`. §5.3 step 1 has its own copy in `_designated`, and deleting it
    left 29 tests passing -- because every designated-sweeper fixture in this
    file uses a device that never refused anything.

    What that guard is for: a designated device is chosen by the OPERATOR, so
    nothing else will ever take it out of rotation. One that refuses `net.sweep`
    -- the wrong OS, the capability compiled out, a policy on the endpoint --
    would be handed the same job every hour, forever, refusing every time, while
    the subnet page showed a dispatch on each beat and the segment was never
    actually probed. Auto-selection's cooldown does not help: designation short-
    circuits it.

    THE AUTO CANDIDATE IS PRESENT AND ELIGIBLE, so this observes WHICH device
    was chosen rather than `None`. *If the check were deleted* it observes the
    refusing designated device, which is the forever-loop above.
    """
    fx = await _fixture(admin_engine, "descool")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    auto = await _sweeper(admin_engine, fx, "10.70.0.9")
    designated = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, designated, status="online")
    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        subnet.sweep_device_id = designated
        await session.commit()
    await _refused_job(admin_engine, fx, designated, finished_at=NOW - timedelta(days=1))

    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        chosen = await select_sweeper(session, fx.org_id, subnet, now=NOW)
    assert chosen == auto, (
        "the designated device that refused a sweep yesterday was chosen again"
    )
    assert chosen != designated


async def test_the_designated_cooldown_has_an_upper_bound_like_the_auto_one(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The other side of the same window, and the reason the test above cannot
    be satisfied by banning a designated device permanently.

    A refusal eight days old is outside `REFUSAL_COOLDOWN_DAYS`, so the
    operator's choice is honoured again. There is an eligible auto candidate
    here too, so *if the cooldown had no upper bound* this observes that
    candidate -- a subnet silently swept from a machine the operator did not
    pick, forever, because of one refusal a week ago.
    """
    fx = await _fixture(admin_engine, "descool2")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    auto = await _sweeper(admin_engine, fx, "10.70.0.9")
    designated = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, designated, status="online")
    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        subnet.sweep_device_id = designated
        await session.commit()
    await _refused_job(
        admin_engine, fx, designated,
        finished_at=NOW - timedelta(days=REFUSAL_COOLDOWN_DAYS + 1),
    )

    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        chosen = await select_sweeper(session, fx.org_id, subnet, now=NOW)
    assert chosen == designated
    assert chosen != auto


async def _refused_job(
    engine: AsyncEngine, fx: _Fixture, device_id: UUID, *, finished_at: datetime
) -> None:
    async with AsyncSession(engine) as session:
        session.add(
            Job(
                org_id=fx.org_id,
                type="net.sweep",
                target_type="device",
                target_id=device_id,
                params={},
                nonce=uuid4().hex,
                issued_at=finished_at - timedelta(minutes=1),
                expires_at=finished_at + timedelta(minutes=9),
                status=JobStatus.refused,
                finished_at=finished_at,
            )
        )
        await session.commit()


async def test_a_refused_job_of_another_type_does_not_ban_a_device(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The cooldown is about `net.sweep` refusals specifically. A device that
    refused a `patch.apply` last night has said nothing about whether it can
    sweep, and banning it would quietly shrink the eligible fleet.

    *If the `type` predicate were dropped* this observes `None`.
    """
    fx = await _fixture(admin_engine, "othertype")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    device_id = await _sweeper(admin_engine, fx, "10.70.0.9")
    async with AsyncSession(admin_engine) as session:
        session.add(
            Job(
                org_id=fx.org_id,
                type="patch.apply",
                target_type="device",
                target_id=device_id,
                params={},
                nonce=uuid4().hex,
                issued_at=NOW - timedelta(days=1),
                expires_at=NOW - timedelta(hours=23),
                status=JobStatus.refused,
                finished_at=NOW - timedelta(days=1),
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        assert await select_sweeper(session, fx.org_id, subnet, now=NOW) == device_id


async def test_an_agent_advertising_other_capabilities_is_not_eligible(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """§5.3: capability advertisement is the FAST PATH. An empty map is tolerated
    (nothing in this repository populates the column), but an agent that has
    said what it can do and did not say `net.sweep` is taken at its word.

    TWO CANDIDATES, so the answer is a choice: *if a non-empty map were treated
    as "tolerate anyway"* this observes the advertising-but-not-sweeping device.
    """
    fx = await _fixture(admin_engine, "caps")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    speaks = await _sweeper(
        admin_engine, fx, "10.70.0.8", last_seen_at=NOW - timedelta(seconds=1)
    )
    silent = await _sweeper(
        admin_engine, fx, "10.70.0.9", last_seen_at=NOW - timedelta(hours=2)
    )
    async with AsyncSession(admin_engine) as session:
        from app.models import Agent

        agent = (
            await session.execute(select(Agent).where(Agent.device_id == speaks))
        ).scalar_one()
        agent.capabilities = {"jobs": ["inventory.collect", "patch.scan"]}
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        chosen = await select_sweeper(session, fx.org_id, subnet, now=NOW)
    assert chosen == silent


async def test_an_agent_advertising_net_sweep_is_eligible(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The other half of the previous test: a populated map that DOES name
    `net.sweep` must not be read as "not empty, therefore refuse". Without this,
    the capability check is indistinguishable from "never trust the column"."""
    fx = await _fixture(admin_engine, "caps2")
    await _authorize(admin_engine, fx, "10.70.0.0/24")
    device_id = await _sweeper(admin_engine, fx, "10.70.0.9")
    async with AsyncSession(admin_engine) as session:
        from app.models import Agent

        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
        agent.capabilities = {"jobs": ["net.sweep"]}
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        assert await select_sweeper(session, fx.org_id, subnet, now=NOW) == device_id


async def test_a_device_in_another_org_is_never_selected_on_the_admin_engine(
    admin_engine: AsyncEngine
) -> None:
    """PINNED ON THE ADMIN ENGINE. Org B's device is given org A's `vrf_id` for
    its observation -- the foreign keys are org-blind, so that row is
    constructible -- which is the only shape in which the selector's own `org_id`
    predicate is observable at all.

    *If the `org_id` predicate were deleted* this observes another tenant's
    machine chosen to probe this tenant's segment.

    AND ORG A'S CLIENT (MSP clients plan 2026-10-26 Task 11, lesson 2). The
    selector keeps to the subnet's client too, so B's device under B's own
    client would be excluded by that predicate alone and the org one would go
    untested. A device of org B under org A's internal client is impossible
    while `fk_devices_client_org` holds, so it is planted with that key
    dropped, in a transaction that is rolled back, and selected inside it.
    """
    from app.models import Agent, Device, IpamObservation
    from tests.client_scope_support import drop_client_keys

    a = await _fixture(admin_engine, "sel-a")
    b = await seed_org(admin_engine, "sel-b")
    async with AsyncSession(admin_engine) as session:
        await drop_client_keys(session, "devices", "fk_devices_client_org")
        foreign = Device(
            org_id=b.org_id,
            client_id=a.org_id,  # org A's internal client, and its Principal site
            site_id=a.org_id,
            hostname="foreign",
            os_family="linux",
        )
        session.add(foreign)
        await session.flush()
        session.add(
            Agent(
                org_id=b.org_id,
                device_id=foreign.id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=NOW + timedelta(days=365),
                enrolled_at=NOW,
                status="online",
                last_seen_at=NOW - timedelta(minutes=1),
            )
        )
        session.add(
            IpamObservation(
                org_id=b.org_id,
                vrf_id=a.vrf_id,  # org-blind FK: org B's row, org A's VRF
                address="10.70.0.9",
                source="inventory",
                method="nic",
                confidence="high",
                device_id=foreign.id,
                first_seen_at=NOW - timedelta(hours=1),
                last_seen_at=NOW - timedelta(hours=1),
                sighting_count=1,
            )
        )
        await session.flush()
        subnet = await get_subnet(session, a.org_id, a.subnet_id)
        chosen = await select_sweeper(session, a.org_id, subnet, now=NOW)
        await session.rollback()
    assert chosen is None


async def test_an_aged_out_observation_does_not_make_a_device_a_candidate(
    admin_engine: AsyncEngine
) -> None:
    """§5.3 step 2 says CURRENT inventory observations. A machine that was on
    that segment in March is not on it today.

    Two devices, one current and one aged past `W_obs`, so the test observes the
    choice rather than an absence.
    """
    fx = await _fixture(admin_engine, "aged")
    stale_device = await seed_device(admin_engine, fx.org_id)
    await seed_agent(admin_engine, fx.org_id, stale_device, status="online")
    await upsert_observation_row(
        admin_engine,
        fx.org_id,
        vrf_id=fx.vrf_id,
        address="10.70.0.8",
        source="inventory",
        method="nic",
        confidence="high",
        device_id=stale_device,
        now=NOW - timedelta(days=90),
    )
    fresh = await _sweeper(admin_engine, fx, "10.70.0.9")

    async with AsyncSession(admin_engine) as session:
        subnet = await get_subnet(session, fx.org_id, fx.subnet_id)
        assert await select_sweeper(session, fx.org_id, subnet, now=NOW) == fresh


# --------------------------------------------------------------------------- #
# The dispatch sweep's due-subnet query (§5.3, §0.10 point 6)
# --------------------------------------------------------------------------- #


async def test_only_enabled_intervalled_and_overdue_subnets_are_due(
    admin_engine: AsyncEngine
) -> None:
    """Four near-identical subnets differing in ONE field each -- the constant
    parameter across fixtures is what makes a due-query test vacuous.

    `sweep_interval_hours IS NULL` means on-demand only (§2.4), and a disabled
    subnet is not due however overdue it looks.
    """
    org = await seed_org(admin_engine, "due")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    common = {"sweep_enabled": True, "sweep_interval_hours": 168}
    overdue = await seed_subnet(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        prefix="10.1.0.0/24",
        last_swept_at=NOW - timedelta(days=8),
        **common,
    )
    never = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.2.0.0/24", **common
    )
    recent = await seed_subnet(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        prefix="10.3.0.0/24",
        last_swept_at=NOW - timedelta(hours=2),
        **common,
    )
    on_demand = await seed_subnet(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        prefix="10.4.0.0/24",
        sweep_enabled=True,
        sweep_interval_hours=None,
        last_swept_at=NOW - timedelta(days=400),
    )
    disabled = await seed_subnet(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        prefix="10.5.0.0/24",
        sweep_enabled=False,
        sweep_interval_hours=1,
        last_swept_at=NOW - timedelta(days=400),
    )

    async with AsyncSession(admin_engine) as session:
        due = await due_subnet_ids(session, org.org_id, now=NOW)
    assert set(due) == {overdue, never}
    assert recent not in due
    assert on_demand not in due
    assert disabled not in due


async def test_the_dispatch_interval_is_hourly() -> None:
    """§5.3's beat cadence, as a project constant rather than a literal repeated
    in `app/worker.py` -- a test carrying its own copy of 3600.0 passes when the
    constant changes and the beat entry does not."""
    assert SWEEP_DISPATCH_INTERVAL_S == 3600.0


# --------------------------------------------------------------------------- #
# Registration in EVERY process that needs it (§14.2 was wrong about this)
# --------------------------------------------------------------------------- #


def test_the_beat_entry_and_the_worker_job_type_are_registered(
    pg_cluster, tmp_path: Path
) -> None:
    """The beat entry AND the job type, in the worker process.

    `app/worker.py`'s `ipam_sweep_dispatch` calls `JobEngine.create`, which asks
    the kernel registry for `net.sweep`; the worker never imports the API's
    registration path, so without `register_worker_job_types` the FIRST
    scheduled sweep in production raises `UnknownJobType` while every in-process
    test passes.
    """
    from app.core.config import Settings, override_settings
    from app.jobs import registry as job_registry
    from app.jobs.registry import UnknownJobType, get_job_type, registered_names
    from app.worker import create_celery

    job_registry._registry.pop("net.sweep", None)  # noqa: SLF001
    with pytest.raises(UnknownJobType):
        get_job_type("net.sweep")

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
    )
    with override_settings(settings):
        schedule = create_celery().conf.beat_schedule

    assert "net.sweep" in registered_names()
    entry = schedule["ipam-sweep-dispatch"]
    assert entry["task"] == "app.worker.ipam_sweep_dispatch"
    assert entry["schedule"] == SWEEP_DISPATCH_INTERVAL_S
    # `worker._with_expiry` gives EVERY beat entry an `options.expires` so an
    # overrunning sweep cannot backlog without bound. Asserted per-key rather
    # than as a whole-dict equality, which would break again the next time a
    # cross-cutting option is added; the property itself is pinned over the
    # whole schedule in tests/test_worker_beat_expiry.py.
    assert entry["options"]["expires"] == SWEEP_DISPATCH_INTERVAL_S


def test_the_gateway_process_registers_the_net_sweep_job_type() -> None:
    """AGENT RESULTS COMPLETE IN THE GATEWAY PROCESS
    (`app/gateway/dispatch.py` -> `JobEngine.complete`), and `app/gateway/main.py`
    registers each module's job types for itself rather than calling
    `register_all`. A result handler registered only in `ipam.register(app)` is
    DEAD IN PRODUCTION while every test in this file passes -- §14.2's "kernel:
    none required" did not account for it, and the previous builder found the
    same hole for the inventory hook.

    *If the gateway's registration were removed* this observes `net.sweep`
    missing from the registry after the gateway's own bootstrap ran.
    """
    from app.gateway.main import register_module_job_types
    from app.jobs import registry as job_registry
    from app.jobs.registry import registered_names

    job_registry._registry.pop("net.sweep", None)  # noqa: SLF001
    assert "net.sweep" not in registered_names()
    register_module_job_types()
    assert "net.sweep" in registered_names()


def test_registering_the_module_registers_the_job_type() -> None:
    """The API process's path: `ipam.register(app)`. Dispatch happens here too
    (`POST /subnets/{id}/sweep`), so all three processes need it."""
    from fastapi import FastAPI

    from app.jobs import registry as job_registry
    from app.jobs.registry import registered_names
    from app.modules import ipam

    job_registry._registry.pop("net.sweep", None)  # noqa: SLF001
    ipam.register(FastAPI())
    assert "net.sweep" in registered_names()


# --------------------------------------------------------------------------- #
# The sequencing control, NARROWED (was: "no file here can dispatch yet")
# --------------------------------------------------------------------------- #


def _code_of(path: Path) -> str:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0].value.value = ""
    return ast.unparse(tree)


def test_create_sweep_calls_authorized_targets_before_it_creates_a_job() -> None:
    """§6.3 AS AN ORDERING, not only as an outcome.

    The behavioural test above proves no `jobs` row exists for one unauthorized
    fixture. This proves the ORDER in the source, which is the property that
    holds for fixtures nobody wrote: `authorized_targets` is called, and it is
    called before anything that can produce a signature.

    *If the authorization call were moved below `job_engine.create`* this
    observes the reversed indices and names both call sites.
    """
    source = Path(sweep_module.__file__).read_text()
    tree = ast.parse(source)
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_sweep"
    )
    calls: list[tuple[int, str]] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in ("authorized_targets", "create", "signed_envelope"):
            calls.append((node.lineno, str(name)))
    calls.sort()
    assert calls, "create_sweep calls neither authorized_targets nor JobEngine"
    assert calls[0][1] == "authorized_targets", (
        f"the first guarded call in create_sweep is {calls[0][1]!r}: "
        "the authorization check must precede anything that can be signed"
    )
    assert {"create", "signed_envelope"} <= {name for _, name in calls}


# --------------------------------------------------------------------------- #
# The hourly beat, end to end through `app/worker.py`
# --------------------------------------------------------------------------- #


async def test_the_hourly_sweep_dispatches_due_subnets_and_publishes_after_commit(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    """The real worker path, not a stand-in for it -- `ipam_sweep_dispatch_org`
    is what actually runs in production and it is the only place `create_sweep`
    is called on a timer.

    THREE SUBNETS, DIFFERING ONLY IN WHAT SHOULD HAPPEN TO THEM: one due and
    authorized (dispatches), one due and unauthorized (records `refused` and
    does not stop the pass), one not yet due (untouched). A fixture with a
    single subnet cannot tell "the loop works" from "the loop stops at the first
    exception".

    The publish happens after the tenant transaction commits, so the envelope
    the agent receives can never describe a run that rolled back; asserting on
    the RETURN COUNT plus the committed rows is what pins that ordering here.
    """
    import redis.asyncio as aioredis

    from app.core.config import Settings, override_settings
    from app.worker import ipam_sweep_dispatch_org

    org = await seed_org(admin_engine, "beat")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    fx = _Fixture(org, vrf_id, uuid4())
    register_job_types()

    due_ok = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.80.0.0/24",
        sweep_enabled=True, sweep_interval_hours=168,
        last_swept_at=NOW - timedelta(days=8),
    )
    due_unauthorized = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.81.0.0/24",
        sweep_enabled=True, sweep_interval_hours=168,
        last_swept_at=NOW - timedelta(days=8),
    )
    not_due = await seed_subnet(
        admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.82.0.0/24",
        sweep_enabled=True, sweep_interval_hours=168,
        last_swept_at=NOW - timedelta(hours=1),
    )
    await _authorize(admin_engine, fx, "10.80.0.0/24")
    await _authorize(admin_engine, fx, "10.82.0.0/24")  # authorized but NOT due
    device_id = await _sweeper(admin_engine, fx, "10.80.0.9")
    await _sweeper(admin_engine, fx, "10.81.0.9")
    await _sweeper(admin_engine, fx, "10.82.0.9")

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
    )
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        with override_settings(settings):
            dispatched = await ipam_sweep_dispatch_org(
                org.org_id,
                _engine_for(tmp_path),
                app_engine,
                redis,
                now=NOW,
            )
    finally:
        await redis.aclose()

    assert dispatched == 1
    runs = {r.subnet_id: r for r in await _runs(admin_engine, org.org_id)}
    assert runs[due_ok].state == "dispatched"
    assert runs[due_ok].device_id == device_id
    assert runs[due_ok].trigger == "scheduled"
    assert (runs[due_unauthorized].state, runs[due_unauthorized].error) == (
        "refused",
        "not authorized",
    )
    assert not_due not in runs
    jobs = await _jobs(admin_engine, org.org_id)
    assert [j.type for j in jobs] == ["net.sweep"]
    assert jobs[0].target_id == device_id
    assert jobs[0].signature
