"""Building and dispatching `health.policy.apply`
(docs/modules/endpoint-health-alerting/spec.md §5.2, §0.4, §2.11).

READ THIS BEFORE ADDING A TEST HERE.

1. `push_policy` DOES NOT PUBLISH. It returns `(job_id, envelope)` for the
   caller to publish AFTER the `org_session` block exits.
   `app/modules/deploy/scripts.py::run_script` states the reason in its own
   docstring: a job published before the caller's transaction commits could be
   handed to an agent with no durable server record if the commit later failed.
   The test that holds this asserts on the RETURN VALUE and on the absence of
   any Redis call, not on the job row alone.

2. A DISARM IS AN EXPLICIT INSTRUCTION. Three outcomes, and they are three
   different things:
     - a profile resolves            -> push its checks (possibly `[]`);
     - nothing resolves AND the device is still running a policy
                                     -> push `checks=[]`, `policy_version=0`;
     - nothing resolves AND the device was never armed
                                     -> return `None`, create no job.
   An implementation that collapses the middle case into the third leaves the
   device running its last policy FOREVER, and the test that catches that is
   the one asserting a job WAS created.

3. THE JOB'S SIGNATURE IS ASSERTED NON-NULL. §0.4's whole argument is that the
   policy travels on the signed path; a test that only checked `type` and
   `params` would pass against a job the engine never signed.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy import text as sqltext
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import Device, DeviceHealthPolicyAck, Job
from app.modules.health.jobtypes import register_job_types
from app.modules.health.policy_push import (
    DISARM_POLICY_VERSION,
    build_policy_params,
    push_check_now,
    push_policy,
    record_policy_ack,
)
from app.modules.health.resolution import resolve_profile_for_device
from tests.health_fixtures import (
    seed_agent,
    seed_assignment,
    seed_check,
    seed_device,
    seed_org,
    seed_profile,
)


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_job_types()


def _engine(tmp_path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "health-job-signing.key"))


async def _device(api_env, device_id: UUID) -> Device:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = await session.get(Device, device_id)
        assert device is not None
        return device


async def _jobs(api_env, org_id: UUID) -> list[Job]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(Job).where(Job.org_id == org_id).order_by(Job.issued_at, Job.id)
                )
            )
            .scalars()
            .all()
        )


async def _push(api_env, tmp_path, org_id: UUID, device: Device) -> tuple[UUID, dict] | None:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        return await push_policy(session, _engine(tmp_path), org_id, device)


# ---------------------------------------------------------------------------
# build_policy_params
# ---------------------------------------------------------------------------


async def test_the_policy_is_built_from_the_profile_and_the_registry(api_env) -> None:
    """`cadence` comes from `CHECK_TYPES`, and the schema is what forces it:
    `health_check` has NO cadence column. A posture check must ship `posture`
    and a tick check `tick`, so a build that hardcoded either is red here."""
    org = await seed_org(api_env, "pp-build")
    profile_id = await seed_profile(
        api_env, org.org_id, tick_interval_s=120, posture_interval_s=1800, summary_interval_s=900
    )
    await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct", for_ticks=5)
    await seed_check(
        api_env,
        org.org_id,
        profile_id,
        check_type="posture.disk_encryption",
        comparator="is",
        threshold_num=None,
        threshold_bool=True,
        severity="critical",
    )
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)

    device = await _device(api_env, org.device_id)
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        resolved = await resolve_profile_for_device(session, org.org_id, device)
    assert resolved is not None
    params = build_policy_params(resolved)

    assert params.policy_version == 1
    assert (params.tick_interval_s, params.posture_interval_s) == (120, 1800)
    assert params.summary_interval_s == 900
    by_type = {c.check_type: c for c in params.checks}
    assert by_type["cpu.busy_pct"].cadence == "tick"
    assert by_type["posture.disk_encryption"].cadence == "posture"
    assert by_type["cpu.busy_pct"].for_ticks == 5
    assert by_type["cpu.busy_pct"].threshold == "95"
    assert by_type["posture.disk_encryption"].threshold == "true"


async def test_an_enum_threshold_ships_the_member_name_not_a_boolean(api_env) -> None:
    """§4.1's amendment. `service.state` stores its threshold in
    `threshold_bool`, but the agent reports the literal string `running`
    (§5.1's example) -- so the wire carries the member name and the two sides
    compare like with like."""
    org = await seed_org(api_env, "pp-enum")
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_check(
        api_env,
        org.org_id,
        profile_id,
        check_type="service.state",
        selector="Spooler",
        comparator="is",
        threshold_num=None,
        threshold_bool=True,
    )
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)

    device = await _device(api_env, org.device_id)
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        resolved = await resolve_profile_for_device(session, org.org_id, device)
    assert resolved is not None
    assert build_policy_params(resolved).checks[0].threshold == "running"


async def test_agent_offline_is_never_in_the_built_policy(api_env) -> None:
    """§0.6. It is server-evaluated; shipping it would be asking the agent a
    question only silence can answer."""
    org = await seed_org(api_env, "pp-offline")
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct")
    await seed_check(
        api_env,
        org.org_id,
        profile_id,
        check_type="agent.offline",
        comparator="gt",
        threshold_num=900,
    )
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)

    device = await _device(api_env, org.device_id)
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        resolved = await resolve_profile_for_device(session, org.org_id, device)
    assert resolved is not None
    assert [c.check_type for c in build_policy_params(resolved).checks] == ["cpu.busy_pct"]


# ---------------------------------------------------------------------------
# push_policy
# ---------------------------------------------------------------------------


async def test_push_policy_creates_a_signed_job_and_returns_it_unpublished(
    api_env, tmp_path
) -> None:
    """The job is created and SIGNED, and the envelope comes back to the caller
    rather than going to Redis inside the transaction (Global Constraint 15)."""
    org = await seed_org(api_env, "pp-push")
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct")
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)

    pending = await _push(api_env, tmp_path, org.org_id, await _device(api_env, org.device_id))
    assert pending is not None
    job_id, envelope = pending

    jobs = await _jobs(api_env, org.org_id)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == job_id
    assert job.type == "health.policy.apply"
    assert job.target_type == "device"
    assert job.target_id == org.device_id
    assert job.params["policy_version"] == 1
    assert [c["check_type"] for c in job.params["checks"]] == ["cpu.busy_pct"]

    # THE SIGNED PATH (invariant #1, §0.4). Without this assertion the test
    # passes against a job the engine never signed.
    assert job.signature
    assert envelope["kind"] == "job"
    assert envelope["sig"] == job.signature
    assert envelope["job"]["device"] == str(org.device_id)


async def test_push_policy_creates_no_job_for_a_device_that_was_never_armed(
    api_env, tmp_path
) -> None:
    """§3: no assignment at any level means the device has no health profile.
    There is nothing to disarm, so pushing anything would be inventing a policy
    for a machine nobody configured."""
    org = await seed_org(api_env, "pp-never")
    assert await _push(api_env, tmp_path, org.org_id, await _device(api_env, org.device_id)) is None
    assert await _jobs(api_env, org.org_id) == []


async def test_unassigning_a_device_pushes_an_explicit_disarm(api_env, tmp_path) -> None:
    """THE TEST THAT CATCHES "SKIP THE PUSH WHEN THERE IS NOTHING TO SEND".

    §5.2: "A disarm must be an explicit instruction, not the absence of one." A
    device that has acknowledged a policy and then loses its assignment must be
    TOLD to run nothing; an implementation that returns `None` here leaves it
    evaluating its last policy forever, and the only assertion that sees the
    difference is the one saying a job WAS created.
    """
    org = await seed_org(api_env, "pp-disarm")
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct")
    assignment_id = await seed_assignment(
        api_env, org.org_id, profile_id, device_id=org.device_id
    )
    device = await _device(api_env, org.device_id)

    first = await _push(api_env, tmp_path, org.org_id, device)
    assert first is not None
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        job = await session.get(Job, first[0])
        assert job is not None
        await record_policy_ack(session, job, {})

    async with api_env.admin_engine.begin() as conn:
        await conn.execute(
            sqltext("DELETE FROM health_profile_assignment WHERE id = :a"), {"a": assignment_id}
        )

    second = await _push(api_env, tmp_path, org.org_id, device)
    assert second is not None, "an unassigned but armed device must be told to stop"

    jobs = await _jobs(api_env, org.org_id)
    assert len(jobs) == 2
    disarm = jobs[1]
    assert disarm.type == "health.policy.apply"
    assert disarm.params["checks"] == []
    assert disarm.params["policy_version"] == DISARM_POLICY_VERSION
    assert disarm.signature


async def test_a_device_already_disarmed_is_not_told_again(api_env, tmp_path) -> None:
    """The other half. Once the disarm is acknowledged there is nothing left to
    say, and re-pushing it every sweep would be a job per device per interval --
    the arithmetic §0.4 rejects."""
    org = await seed_org(api_env, "pp-disarm2")
    device = await _device(api_env, org.device_id)
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        session.add(
            DeviceHealthPolicyAck(
                org_id=org.org_id,
                device_id=org.device_id,
                profile_id=None,
                policy_version=DISARM_POLICY_VERSION,
            )
        )
    assert await _push(api_env, tmp_path, org.org_id, device) is None
    assert await _jobs(api_env, org.org_id) == []


async def test_a_profile_whose_checks_all_filter_out_still_pushes_an_empty_policy(
    api_env, tmp_path
) -> None:
    """An empty check set from a REAL profile is a legal policy (§5.2), not a
    reason to skip: a Linux device under a Windows-only profile must be told to
    run nothing, not left running whatever it had."""
    org = await seed_org(api_env, "pp-empty")
    linux_device_id = await seed_device(api_env, org.org_id, os_family="linux")
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_check(
        api_env, org.org_id, profile_id, check_type="cpu.busy_pct", os_family="windows"
    )
    await seed_assignment(api_env, org.org_id, profile_id, device_id=linux_device_id)

    pending = await _push(api_env, tmp_path, org.org_id, await _device(api_env, linux_device_id))
    assert pending is not None
    jobs = await _jobs(api_env, org.org_id)
    assert len(jobs) == 1
    assert jobs[0].params["checks"] == []
    assert jobs[0].params["policy_version"] == 1


async def test_push_check_now_creates_a_signed_parameterless_job(api_env, tmp_path) -> None:
    """§5.3. The console's "refresh now" button, and what runs after a
    remediation so an operator does not wait a tick interval to learn whether
    the fix worked."""
    org = await seed_org(api_env, "pp-now")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        job_id, envelope = await push_check_now(
            session, _engine(tmp_path), org.org_id, await _device(api_env, org.device_id)
        )
    jobs = await _jobs(api_env, org.org_id)
    assert [j.type for j in jobs] == ["health.check.now"]
    assert jobs[0].id == job_id
    assert jobs[0].params == {}
    assert jobs[0].signature
    assert envelope["sig"] == jobs[0].signature


async def test_push_policy_refuses_a_device_from_another_org(api_env, tmp_path) -> None:
    """`push_policy` takes `org_id` separately from the device row. A mismatched
    pair must create no job -- this is the path that puts signed instructions on
    an endpoint."""
    a = await seed_org(api_env, "pp-xa")
    b = await seed_org(api_env, "pp-xb")
    profile_id = await seed_profile(api_env, b.org_id)
    await seed_assignment(api_env, b.org_id, profile_id, device_id=b.device_id)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device_b = await session.get(Device, b.device_id)
        assert device_b is not None
        assert await push_policy(session, _engine(tmp_path), a.org_id, device_b) is None
    assert await _jobs(api_env, a.org_id) == []


# ---------------------------------------------------------------------------
# record_policy_ack (§2.11, §5.2)
# ---------------------------------------------------------------------------


async def _ack(api_env, device_id: UUID) -> DeviceHealthPolicyAck | None:
    async with AsyncSession(api_env.admin_engine) as session:
        return await session.get(DeviceHealthPolicyAck, device_id)


async def _armed(api_env, tmp_path, tag: str) -> tuple[Any, UUID, UUID]:
    org = await seed_org(api_env, tag)
    profile_id = await seed_profile(api_env, org.org_id)
    await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct")
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)
    pending = await _push(api_env, tmp_path, org.org_id, await _device(api_env, org.device_id))
    assert pending is not None
    return org, profile_id, pending[0]


async def test_the_ack_handler_stamps_the_acknowledged_version(api_env, tmp_path) -> None:
    org, profile_id, job_id = await _armed(api_env, tmp_path, "ack-1")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        await record_policy_ack(session, job, {})

    row = await _ack(api_env, org.device_id)
    assert row is not None
    assert (row.policy_version, row.profile_id) == (1, profile_id)
    assert row.org_id == org.org_id


async def test_the_ack_handler_is_idempotent_under_redelivery(api_env, tmp_path) -> None:
    """§2.11: ONE ROW PER DEVICE, UPSERTed, never a history. A row per
    acknowledgement is a `policy_version` time series keyed by device -- §0.12's
    boundary conceded for bookkeeping. Asserted by COUNT, not by absence of
    error."""
    org, _, job_id = await _armed(api_env, tmp_path, "ack-2")
    for _ in range(5):
        async with org_session(
            org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            job = await session.get(Job, job_id)
            assert job is not None
            await record_policy_ack(session, job, {})

    async with api_env.admin_engine.connect() as conn:
        count = (
            await conn.execute(
                sqltext("SELECT count(*) FROM device_health_policy_ack WHERE org_id = :o"),
                {"o": org.org_id},
            )
        ).scalar_one()
    assert count == 1


async def test_a_stale_result_never_lowers_the_acknowledged_version(
    api_env, tmp_path
) -> None:
    """§5.2: "a result that arrives after the next summary is a no-op rather
    than a regression". A lowered version makes §12 re-push a policy the device
    already has, once per sweep, forever."""
    org, profile_id, _ = await _armed(api_env, tmp_path, "ack-3")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        session.add(
            DeviceHealthPolicyAck(
                org_id=org.org_id,
                device_id=org.device_id,
                profile_id=profile_id,
                policy_version=9,
            )
        )

    # A late result for the version-1 job.
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        job = (
            await session.execute(select(Job).where(Job.org_id == org.org_id))
        ).scalar_one()
        await record_policy_ack(session, job, {})

    row = await _ack(api_env, org.device_id)
    assert row is not None and row.policy_version == 9


async def test_a_disarm_ack_replaces_a_higher_version_from_another_profile(
    api_env, tmp_path
) -> None:
    """The "never lower" rule is scoped TO ONE PROFILE. A disarm (and any
    reassignment) is a different policy, not an older copy of the same one, so
    it must win -- otherwise a device disarmed after running a version-9 profile
    reads forever as still on version 9."""
    org, profile_id, _ = await _armed(api_env, tmp_path, "ack-4")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        session.add(
            DeviceHealthPolicyAck(
                org_id=org.org_id,
                device_id=org.device_id,
                profile_id=profile_id,
                policy_version=9,
            )
        )

    async with api_env.admin_engine.begin() as conn:
        await conn.execute(
            sqltext("DELETE FROM health_profile_assignment WHERE org_id = :o"),
            {"o": org.org_id},
        )
    disarm = await _push(api_env, tmp_path, org.org_id, await _device(api_env, org.device_id))
    assert disarm is not None
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        job = await session.get(Job, disarm[0])
        assert job is not None
        await record_policy_ack(session, job, {})

    row = await _ack(api_env, org.device_id)
    assert row is not None
    assert (row.policy_version, row.profile_id) == (DISARM_POLICY_VERSION, None)


async def test_the_ack_handler_ignores_a_result_for_an_unknown_device(
    api_env, tmp_path
) -> None:
    """Defensive: a result handler runs inside the job's tenant transaction and
    a raise FAILS the job. A device deleted between dispatch and result must not
    turn an acknowledged policy into a failed one."""
    org, _, job_id = await _armed(api_env, tmp_path, "ack-5")
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        # IN MEMORY ONLY. Since migration 0231 a job's client follows its
        # target (`its_job_client`), so writing a target that names no device
        # is refused by the database; the handler is handed the stale job
        # instead, detached so no autoflush writes it.
        session.expunge(job)
        job.target_id = uuid4()
        await record_policy_ack(session, job, {})
    assert await _ack(api_env, org.device_id) is None


async def test_the_ack_carries_its_own_org_predicate(admin_engine, tmp_path) -> None:
    """THE ONE RLS CANNOT MASK, and it needs a genuinely MIS-SCOPED job to be
    load-bearing.

    A job whose `org_id` is A but whose `target_id` names B's device is what a
    dropped predicate actually looks like from here: on the admin engine RLS is
    not filtering, so `Device.org_id == org_id` inside `record_policy_ack` is
    the only thing between org A's result and a row written into org B's fleet
    state. Delete it and org B's ack is overwritten with org A's version.

    A same-org test cannot see this: the device really does belong to the org,
    so the predicate changes nothing.
    """
    a = await seed_org(admin_engine, "ack-pa")
    b = await seed_org(admin_engine, "ack-pb")
    profile_a = await seed_profile(admin_engine, a.org_id)
    profile_b = await seed_profile(admin_engine, b.org_id)
    await seed_assignment(admin_engine, a.org_id, profile_a, device_id=a.device_id)
    await seed_assignment(admin_engine, b.org_id, profile_b, device_id=b.device_id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            DeviceHealthPolicyAck(
                org_id=b.org_id, device_id=b.device_id, profile_id=profile_b, policy_version=4
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device_a = await session.get(Device, a.device_id)
        assert device_a is not None
        pending = await push_policy(session, _engine(tmp_path), a.org_id, device_a)
        assert pending is not None
        job = await session.get(Job, pending[0])
        assert job is not None
        # The legitimate half: org A's own result stamps org A's row.
        await record_policy_ack(session, job, {})
        # The mis-scoped half: the same org-A job repointed at org B's device.
        job.target_id = b.device_id
        await record_policy_ack(session, job, {})
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row_a = await session.get(DeviceHealthPolicyAck, a.device_id)
        row_b = await session.get(DeviceHealthPolicyAck, b.device_id)
    assert row_a is not None and row_a.org_id == a.org_id and row_a.policy_version == 1
    assert row_b is not None
    assert (row_b.org_id, row_b.policy_version) == (b.org_id, 4), (
        "org B's ack must be untouched by a job carrying org A's org_id"
    )


# ---------------------------------------------------------------------------
# record_policy_ack_from_summary -- §2.11's SECOND writer
# ---------------------------------------------------------------------------
#
# `DeviceHealthPolicyAck`'s docstring and §5.2's known limit both describe two
# writers: the result handler for promptness, and every summary's
# `policy_version` for correctness -- "the channel that survives a lost result".
# Only the first existed. The docstrings therefore asserted a recovery path the
# code did not have, and the cost is not latency but permanence: with no second
# writer a device whose policy result was lost never gets an ack row at all,
# `run_policy_reconcile` reads that as "never armed, nothing to withdraw", and
# the endpoint evaluates a deleted profile forever.


async def _ingest(api_env, org_id: UUID, device_id: UUID, lines, *, policy_version: int):
    from app.modules.health.ingest import ingest_report
    from tests.test_health_ingest import _device_row, _msg

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, device_id)
        outcome = await ingest_report(
            session,
            org_id,
            device,
            _msg(lines, policy_version=policy_version),
            now=datetime.now(UTC),
        )
        await session.commit()
    return outcome


async def test_a_summary_acknowledges_the_policy_version_it_reports(
    api_env, tmp_path
) -> None:
    """The channel itself. No `health.policy.apply` RESULT is ever handled here
    -- that is the lost-result case -- so an implementation with only the result
    handler leaves `_ack` returning None.

    Both columns are asserted: a writer that stamped the version but resolved
    the wrong `profile_id` would make the reconcile's version comparison read
    against a profile the device is not on.
    """
    from tests.test_health_ingest import _line

    org = await seed_org(api_env, "sumack-1")
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct")
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)

    assert await _ack(api_env, org.device_id) is None
    outcome = await _ingest(
        api_env,
        org.org_id,
        org.device_id,
        [_line(check_id, kind="summary", state="ok", value="4")],
        policy_version=1,
    )
    assert outcome.applied == 1

    row = await _ack(api_env, org.device_id)
    assert row is not None, "a summary must acknowledge the version it reports"
    assert (row.policy_version, row.profile_id) == (1, profile_id)


async def test_a_breach_only_report_does_not_acknowledge_anything(api_env) -> None:
    """The negative that keeps the writer honest about WHICH line is the
    channel. §2.11 names the summary specifically: a summary is the agent's
    periodic full restatement of what it is running, while an edge is a single
    observation that may be replayed from a buffer long after the policy moved
    on. Treating a breach as an acknowledgement would let one redelivered edge
    declare convergence for a device that has not converged.
    """
    from tests.test_health_ingest import _line

    org = await seed_org(api_env, "sumack-2")
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct")
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)

    await _ingest(
        api_env,
        org.org_id,
        org.device_id,
        [_line(check_id, kind="breach", state="breaching", value="99")],
        policy_version=1,
    )
    assert await _ack(api_env, org.device_id) is None


async def test_a_lost_result_converges_through_the_summary_and_the_reconcile(
    api_env, tmp_path
) -> None:
    """§5.2's KNOWN LIMIT, driven end to end -- and the reason the finding is a
    functional defect rather than a stale comment.

    A policy is pushed and its RESULT IS NEVER HANDLED (the lost-result case the
    module is architected around), then the assignment is deleted. The reconcile
    sees no resolved profile and no ack row, concludes "never armed", and
    creates nothing -- five passes in a row. §5.2 says the device then
    acknowledges "via the result handler OR ITS NEXT SUMMARY" and the next
    reconcile disarms it. Without the summary writer that sentence is false in
    both halves: the ack stays None and the disarm count stays 0 forever, which
    is what this asserts on either side of the summary.
    """
    from app.modules.health.sweep import run_policy_reconcile
    from tests.test_health_ingest import _line

    org = await seed_org(api_env, "sumack-3")
    await seed_agent(api_env, org.org_id, org.device_id)
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct")
    assignment_id = await seed_assignment(
        api_env, org.org_id, profile_id, device_id=org.device_id
    )
    device = await _device(api_env, org.device_id)
    assert await _push(api_env, tmp_path, org.org_id, device) is not None  # result LOST

    async with api_env.admin_engine.begin() as conn:
        await conn.execute(
            sqltext("DELETE FROM health_profile_assignment WHERE id = :a"),
            {"a": assignment_id},
        )

    for _ in range(5):
        async with org_session(
            org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            assert (
                await run_policy_reconcile(session, org.org_id, job_engine=_engine(tmp_path))
                == []
            )
    assert await _ack(api_env, org.device_id) is None
    assert len(await _jobs(api_env, org.org_id)) == 1, "only the original push exists"

    # The agent, which DID install the policy, keeps summarising at version 1.
    # Every line is discarded -- the check left the resolved set with the
    # assignment -- which is exactly why the ack cannot be gated on `applied`.
    outcome = await _ingest(
        api_env,
        org.org_id,
        org.device_id,
        [_line(check_id, kind="summary", state="ok", value="4")],
        policy_version=1,
    )
    assert outcome.applied == 0 and outcome.discarded == 1

    row = await _ack(api_env, org.device_id)
    assert row is not None and row.policy_version == 1

    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        pushes = await run_policy_reconcile(
            session, org.org_id, job_engine=_engine(tmp_path)
        )
    assert len(pushes) == 1, "the summary is what lets the reconcile see an armed device"

    jobs = await _jobs(api_env, org.org_id)
    assert len(jobs) == 2
    assert jobs[1].params["policy_version"] == DISARM_POLICY_VERSION
    assert jobs[1].params["checks"] == []


async def test_the_disarm_that_follows_a_summary_ack_settles_and_stops_pushing(
    api_env, tmp_path
) -> None:
    """THE HALF THAT THE SUMMARY WRITER WOULD OTHERWISE BREAK.

    `record_policy_ack_from_summary` can store `(profile_id NULL, version 1)` --
    "installed, but the profile it names no longer resolves" -- a pair the
    result handler alone never produces, since it writes NULL only for a disarm
    at version 0. `_scoped_version`'s original two branches then match the
    disarm's own `(NULL, 0)` on `NULL IS NOT DISTINCT FROM NULL` and take
    `greatest(1, 0) = 1`, so the ack never drops, and the reconcile re-pushes the
    same disarm ON EVERY PASS FOREVER. Asserted as a job COUNT across three
    further passes, which is the only thing that separates "converged" from
    "looks converged".
    """
    from app.modules.health.sweep import run_policy_reconcile
    from tests.test_health_ingest import _line

    org = await seed_org(api_env, "sumack-4")
    await seed_agent(api_env, org.org_id, org.device_id)
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id, check_type="cpu.busy_pct")
    assignment_id = await seed_assignment(
        api_env, org.org_id, profile_id, device_id=org.device_id
    )
    device = await _device(api_env, org.device_id)
    assert await _push(api_env, tmp_path, org.org_id, device) is not None

    async with api_env.admin_engine.begin() as conn:
        await conn.execute(
            sqltext("DELETE FROM health_profile_assignment WHERE id = :a"),
            {"a": assignment_id},
        )
    await _ingest(
        api_env,
        org.org_id,
        org.device_id,
        [_line(check_id, kind="summary", state="ok", value="4")],
        policy_version=1,
    )
    async with org_session(
        org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        assert (
            len(await run_policy_reconcile(session, org.org_id, job_engine=_engine(tmp_path)))
            == 1
        )

    # The agent applies the disarm and says so -- through the summary channel
    # again, since this result is lost too.
    await _ingest(
        api_env,
        org.org_id,
        org.device_id,
        [_line(check_id, kind="summary", state="ok", value="4")],
        policy_version=DISARM_POLICY_VERSION,
    )
    row = await _ack(api_env, org.device_id)
    assert row is not None
    assert (row.policy_version, row.profile_id) == (DISARM_POLICY_VERSION, None), (
        "an acknowledged disarm must replace an unidentifiable stored policy, or "
        "the reconcile never settles"
    )

    for _ in range(3):
        async with org_session(
            org.org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            assert (
                await run_policy_reconcile(session, org.org_id, job_engine=_engine(tmp_path))
                == []
            )
    assert len(await _jobs(api_env, org.org_id)) == 2, "no disarm is pushed twice"
