"""The five sweeps and their beat entries
(`docs/modules/endpoint-health-alerting/spec.md` §12, §6, §0.6, §0.12).

EVERY SWEEP IS RUN TWICE IN ITS OWN TEST, and the second pass is what carries
the assertion. "The sweep ran" is satisfied by a sweep that selected zero rows;
"the sweep ran twice and produced one escalation, one re-notify and one offline
alert" is not. §12's idempotency is a property of the LATCHES (`escalated_at`,
`muted_until`, `uq_health_alert_open_key`, the ack version), so each test
asserts the count the latch bounds rather than the absence of an exception.

WHAT THESE FIXTURES HOLD CONSTANT: one Windows device per org, `cpu.busy_pct`
with `os_family=NULL`, and `now` injected everywhere -- the sweeps compare
against `escalate_after_s`/`offline_after_s`/`alert_retention_days`, so what
each test VARIES is the age of the row against exactly one of those thresholds,
on both sides where a boundary exists. The org tunables are set explicitly
rather than left at their defaults wherever a test depends on one, because a
test that passed only because 3600 happened to be the default would keep passing
if the sweep read the wrong setting entirely.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, override_settings
from app.core.signing import LocalEd25519Signer
from app.jobs.engine import JobEngine
from app.modules.health.sweep import (
    ACTION_INTERVAL_S,
    ALERT_SWEEP_INTERVAL_S,
    OFFLINE_SWEEP_INTERVAL_S,
    POLICY_RECONCILE_INTERVAL_S,
    RETENTION_INTERVAL_S,
    run_alert_sweep,
    run_offline_sweep,
    run_policy_reconcile,
    run_retention,
)
from tests.health_fixtures import (
    seed_assignment,
    seed_check,
    seed_device,
    seed_org,
    seed_profile,
    seed_settings,
)
from tests.test_health_clear_predicate import _open_one, _state_of
from tests.test_health_ingest import _armed_org, _count, _device_row, _line, _msg

pytestmark = pytest.mark.usefixtures("admin_engine")


def _engine(tmp_path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "sweep.key"))


def _settings(pg_cluster, tmp_path) -> Settings:
    """The minimum `create_celery()` needs. Built here rather than read from the
    environment so the beat-schedule test cannot pass or fail depending on how
    the machine running it is configured."""
    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
        vault_root_key_path=tmp_path / "vault-root.key",
    )


@pytest.fixture(autouse=True)
def _job_types_registered() -> None:
    """The reconcile sweep dispatches `health.policy.apply`, so the registry has
    to be populated in this process -- which is §12's whole reason for
    `register_worker_job_types()` carrying this module's call: without it the
    FIRST SWEEP raises `UnknownJobType`."""
    from app.worker import register_worker_job_types

    register_worker_job_types()


async def _seed_agent(admin_engine, org_id, device_id, *, last_seen_at, status="online"):
    from app.models import Agent

    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Agent(
                org_id=org_id,
                device_id=device_id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=now + timedelta(days=365),
                enrolled_at=now - timedelta(days=1),
                status=status,
                last_seen_at=last_seen_at,
            )
        )
        await session.commit()


async def _events(admin_engine, org_id, event_type):
    from app.models import DomainEvent

    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(DomainEvent.subject_id).where(
                        DomainEvent.org_id == org_id, DomainEvent.event_type == event_type
                    )
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# health-alert-sweep: escalation
# ---------------------------------------------------------------------------


async def test_escalation_fires_exactly_once_across_two_passes(admin_engine) -> None:
    """§6 / §2.4's amendment: "stamped once, never cleared".

    GUARD OBSERVABILITY: if `escalated_at` is not stamped, the twice-run test
    observes TWO `health.alert_escalated` rows in `domain_events` where it
    expects one -- one problem becoming an hourly mail.
    """
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, escalate_after_s=300)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    later = now + timedelta(seconds=400)
    counts = []
    for _pass in range(2):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            counts.append((await run_alert_sweep(session, fixture.org_id, now=later))["escalated"])
            await session.commit()

    assert counts == [1, 0]
    assert await _events(admin_engine, fixture.org_id, "health.alert_escalated") == [alert_id]
    async with AsyncSession(admin_engine) as session:
        stamped = (
            await session.execute(
                select(HealthAlert.escalated_at).where(HealthAlert.id == alert_id)
            )
        ).scalar_one()
    assert stamped is not None


async def test_escalation_does_not_fire_before_escalate_after_s(admin_engine) -> None:
    """The boundary's other side. A test with only the "it escalated" case
    passes against a sweep with no age predicate at all, which would escalate
    every alert on the first pass after it opened."""
    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, escalate_after_s=3600)
    now = datetime.now(UTC)
    await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        counts = await run_alert_sweep(
            session, fixture.org_id, now=now + timedelta(seconds=3599)
        )
        await session.commit()
    assert counts["escalated"] == 0
    assert await _events(admin_engine, fixture.org_id, "health.alert_escalated") == []


@pytest.mark.parametrize("state", ["acknowledged", "muted"])
async def test_escalation_skips_acknowledged_and_muted_alerts(admin_engine, state) -> None:
    """§6: escalation is for an alert that is `active`, "never `acknowledged`,
    never `muted`". Both negatives, separately -- a test covering only one
    passes against an implementation that skips only that one.

    Escalating past someone who took ownership is how an escalation path teaches
    people not to acknowledge.
    """
    from app.models import HealthAlert
    from app.modules.health.alerts import acknowledge, mute

    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, escalate_after_s=300)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        if state == "acknowledged":
            await acknowledge(
                session, fixture.org_id, alert_id, actor_id=fixture.org.user_id, now=now
            )
        else:
            await mute(
                session,
                fixture.org_id,
                alert_id,
                actor_id=fixture.org.user_id,
                muted_until=now + timedelta(days=2),
                now=now,
            )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        counts = await run_alert_sweep(
            session, fixture.org_id, now=now + timedelta(seconds=4000)
        )
        await session.commit()
    assert counts["escalated"] == 0
    assert await _events(admin_engine, fixture.org_id, "health.alert_escalated") == []
    async with AsyncSession(admin_engine) as session:
        stamped = (
            await session.execute(
                select(HealthAlert.escalated_at).where(HealthAlert.id == alert_id)
            )
        ).scalar_one()
    assert stamped is None, "a skipped alert must not be stamped, or it can never escalate"


# ---------------------------------------------------------------------------
# health-alert-sweep: mute expiry
# ---------------------------------------------------------------------------


async def test_mute_expiry_returns_to_active_and_re_notifies_exactly_once(
    admin_engine,
) -> None:
    """§0.8: "on expiry the sweep returns the alert to `active` and re-notifies
    ONCE."

    Asserted as an OUTBOX ROW COUNT across two passes. The exactly-once property
    comes from `muted_until` being NULLed in the same statement that moves the
    state, so the second pass finds no expired mute -- there is no stamp left to
    expire.
    """
    from app.models import HealthAlert
    from app.modules.health.alerts import mute

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await mute(
            session,
            fixture.org_id,
            alert_id,
            actor_id=fixture.org.user_id,
            muted_until=now + timedelta(hours=1),
            now=now,
        )
        await session.commit()

    before = len(await _events(admin_engine, fixture.org_id, "health.alert_raised"))
    later = now + timedelta(hours=2)
    counts = []
    for _pass in range(2):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            counts.append((await run_alert_sweep(session, fixture.org_id, now=later))["unmuted"])
            await session.commit()

    assert counts == [1, 0]
    after = await _events(admin_engine, fixture.org_id, "health.alert_raised")
    assert len(after) == before + 1, "exactly one re-notify, not one per pass"
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlert.state, HealthAlert.muted_until).where(
                    HealthAlert.id == alert_id
                )
            )
        ).one()
    assert row == ("active", None)


async def test_an_unexpired_mute_is_left_alone(admin_engine) -> None:
    from app.modules.health.alerts import mute

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await mute(
            session,
            fixture.org_id,
            alert_id,
            actor_id=fixture.org.user_id,
            muted_until=now + timedelta(days=5),
            now=now,
        )
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        counts = await run_alert_sweep(session, fixture.org_id, now=now + timedelta(days=1))
        await session.commit()
    assert counts["unmuted"] == 0
    assert (await _state_of(admin_engine, alert_id))[0] == "muted"


# ---------------------------------------------------------------------------
# health-alert-sweep: approval expiry
# ---------------------------------------------------------------------------


async def test_an_unapproved_remediation_expires_rather_than_dispatching(
    admin_engine,
) -> None:
    """§8.3: "unapproved after `approval_ttl_s` -> `expired`."

    Expired, NOT quietly dispatched: a request that becomes a dispatch when
    nobody acts is `autonomous` wearing `require_approval`'s label.
    """
    from app.models import HealthAlertAction, Job
    from tests.health_action_fixtures import seed_action_row

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    row_id = await seed_action_row(
        admin_engine,
        fixture.org_id,
        alert_id,
        "remediate",
        state="awaiting_approval",
        expires_at=now + timedelta(hours=1),
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        counts = await run_alert_sweep(session, fixture.org_id, now=now + timedelta(hours=2))
        await session.commit()
    assert counts["approvals_expired"] == 1

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(HealthAlertAction.id == row_id)
            )
        ).scalar_one()
        assert row.state == "expired"
        assert await _count(session, Job, org_id=fixture.org_id) == 0


# ---------------------------------------------------------------------------
# health-offline-sweep
# ---------------------------------------------------------------------------


async def test_the_offline_sweep_opens_one_alert_across_two_passes(admin_engine) -> None:
    """Idempotent by `uq_health_alert_open_key`: the second pass finds the alert
    open and bumps `occurrence_count` rather than minting a second one."""
    from app.models import HealthAlert

    org = await seed_org(admin_engine, "off2")
    profile_id = await seed_profile(admin_engine, org.org_id)
    await seed_check(
        admin_engine,
        org.org_id,
        profile_id,
        check_type="agent.offline",
        comparator="gt",
        threshold_num=0,
    )
    await seed_assignment(admin_engine, org.org_id, profile_id)
    await seed_settings(admin_engine, org.org_id, offline_after_s=300)
    now = datetime.now(UTC)
    await _seed_agent(
        admin_engine, org.org_id, org.device_id, last_seen_at=now - timedelta(hours=1)
    )

    opened = []
    for _pass in range(2):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            opened.append(await run_offline_sweep(session, org.org_id, now=now))
            await session.commit()
    assert opened == [
        {"opened": 1, "cleared": 0},
        {"opened": 0, "cleared": 0},
    ], "the second pass must find the alert already open, not mint a second one"

    async with AsyncSession(admin_engine) as session:
        alert = (
            await session.execute(
                select(HealthAlert).where(HealthAlert.org_id == org.org_id)
            )
        ).scalar_one()
    assert alert.check_type == "agent.offline"
    assert alert.occurrence_count == 2, "the second pass bumped the counter, not the row count"


async def test_a_device_seen_inside_offline_after_s_opens_nothing(admin_engine) -> None:
    """The negative. Without it, a sweep with no `last_seen_at` predicate at all
    would open an offline alert for every enrolled device in the fleet on its
    first pass."""
    from app.models import HealthAlert

    org = await seed_org(admin_engine, "off3")
    profile_id = await seed_profile(admin_engine, org.org_id)
    await seed_check(
        admin_engine,
        org.org_id,
        profile_id,
        check_type="agent.offline",
        comparator="gt",
        threshold_num=0,
    )
    await seed_assignment(admin_engine, org.org_id, profile_id)
    await seed_settings(admin_engine, org.org_id, offline_after_s=900)
    now = datetime.now(UTC)
    await _seed_agent(
        admin_engine, org.org_id, org.device_id, last_seen_at=now - timedelta(seconds=60)
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert await run_offline_sweep(session, org.org_id, now=now) == {
            "opened": 0,
            "cleared": 0,
        }
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=org.org_id) == 0


async def test_an_org_without_an_agent_offline_check_gets_no_offline_alert(
    admin_engine,
) -> None:
    """§3's "no fabricated default" applies to the server-evaluated check
    exactly as to the pushed ones: an org that did not put `agent.offline` in
    its profile did not ask to be told."""
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)  # cpu.busy_pct only
    now = datetime.now(UTC)
    await _seed_agent(
        admin_engine, fixture.org_id, fixture.device_id, last_seen_at=now - timedelta(days=1)
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert await run_offline_sweep(session, fixture.org_id, now=now) == {
            "opened": 0,
            "cleared": 0,
        }
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=fixture.org_id) == 0


# ---------------------------------------------------------------------------
# health-policy-reconcile
# ---------------------------------------------------------------------------


async def test_a_device_whose_ack_matches_gets_zero_jobs(admin_engine, tmp_path) -> None:
    """§12 / §14: "a device whose ack matches gets zero jobs (assert a ROW
    COUNT, not the absence of an exception)".

    §0.4's arithmetic is why: 5,000 devices times a 5-minute tick is 1.4M job
    rows a day if the reconcile pushed unconditionally.
    """
    from app.models import DeviceHealthPolicyAck, HealthProfile, Job

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        version = (
            await session.execute(
                select(HealthProfile.policy_version).where(
                    HealthProfile.id == fixture.profile_id
                )
            )
        ).scalar_one()
        session.add(
            DeviceHealthPolicyAck(
                org_id=fixture.org_id,
                device_id=fixture.device_id,
                profile_id=fixture.profile_id,
                policy_version=version,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        pushes = await run_policy_reconcile(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert pushes == []
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, Job, org_id=fixture.org_id) == 0


async def test_a_device_with_no_ack_row_is_re_pushed(admin_engine, tmp_path) -> None:
    """§12: "and where no ack row exists at all (which reads as version 0)".

    §2.11 makes the missing row read as 0 deliberately -- a device that has never
    acknowledged a policy must not be assumed converged. The job is SIGNED,
    asserted here as well, because a reconcile that bypassed `JobEngine.create`
    would put an unsigned instruction on an endpoint (invariant #1).
    """
    from app.models import Job

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    await _seed_agent(admin_engine, fixture.org_id, fixture.device_id, last_seen_at=now)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        pushes = await run_policy_reconcile(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert len(pushes) == 1

    async with AsyncSession(admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.org_id == fixture.org_id))
        ).scalar_one()
    assert job.type == "health.policy.apply"
    assert job.signature is not None
    assert job.target_id == fixture.device_id


async def test_a_stale_ack_is_re_pushed_and_a_fresh_one_is_not(
    admin_engine, tmp_path
) -> None:
    """Both sides in one test, because "stale" only means something against
    "current". A fixture with a single ack version cannot see a comparison that
    always returns true."""
    from app.models import DeviceHealthPolicyAck, HealthProfile, Job

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    await _seed_agent(admin_engine, fixture.org_id, fixture.device_id, last_seen_at=now)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        version = (
            await session.execute(
                select(HealthProfile.policy_version).where(
                    HealthProfile.id == fixture.profile_id
                )
            )
        ).scalar_one()
        session.add(
            DeviceHealthPolicyAck(
                org_id=fixture.org_id,
                device_id=fixture.device_id,
                profile_id=fixture.profile_id,
                policy_version=version - 1,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        stale_pushes = await run_policy_reconcile(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert len(stale_pushes) == 1

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ack = (
            await session.execute(
                select(DeviceHealthPolicyAck).where(
                    DeviceHealthPolicyAck.device_id == fixture.device_id
                )
            )
        ).scalar_one()
        ack.policy_version = version
        session.add(ack)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        fresh_pushes = await run_policy_reconcile(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert fresh_pushes == []
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, Job, org_id=fixture.org_id) == 1


async def test_an_identical_waiting_push_is_not_pushed_again(admin_engine, tmp_path) -> None:
    """A device that has not answered yet gets ONE job, not one per pass.

    WHY (operator report, 2026-09-24): Job history showed a fresh
    `health.policy.apply` for the same device every 15 minutes, each one
    expiring unanswered an hour later. The reconcile compared only the ack and
    never looked at what it had already queued, so every pass while a device
    was unreachable added another identical row. The waiting job is delivered
    when the agent reconnects (`GatewayServer._drain_queued`), so a second copy
    says nothing the first does not.

    GUARD OBSERVABILITY: drop the pending-job check and the second pass pushes,
    making the row count 2.
    """
    from app.models import Job

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    await _seed_agent(admin_engine, fixture.org_id, fixture.device_id, last_seen_at=now)
    for _ in range(2):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            await run_policy_reconcile(
                session, fixture.org_id, job_engine=_engine(tmp_path), now=now
            )
            await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, Job, org_id=fixture.org_id) == 1


async def test_a_waiting_push_of_an_older_policy_does_not_block_the_new_one(
    admin_engine, tmp_path
) -> None:
    """Only an IDENTICAL waiting job suppresses a push. One carrying an older
    version would install a policy that is already out of date, and the device
    must still be told the current one."""
    from app.models import HealthProfile, Job

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    await _seed_agent(admin_engine, fixture.org_id, fixture.device_id, last_seen_at=now)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await run_policy_reconcile(session, fixture.org_id, job_engine=_engine(tmp_path), now=now)
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        profile = (
            await session.execute(
                select(HealthProfile).where(HealthProfile.id == fixture.profile_id)
            )
        ).scalar_one()
        profile.policy_version += 1
        session.add(profile)
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        pushes = await run_policy_reconcile(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert len(pushes) == 1
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, Job, org_id=fixture.org_id) == 2


@pytest.mark.parametrize("agent", ["none", "offline"])
async def test_a_device_without_a_connected_agent_gets_no_push(
    admin_engine, tmp_path, agent
) -> None:
    """No connected agent, no job: nothing could deliver it. An asset row a
    sweep recorded has no agent at all, and an offline one would let a job
    expire unanswered every hour for as long as it stays away. The agent keeps
    the policy it last installed, and the first pass after it reconnects
    pushes whatever changed.

    GUARD OBSERVABILITY: drop the agent check and both cases push one job.
    """
    from app.models import Job

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    if agent == "offline":
        await _seed_agent(
            admin_engine,
            fixture.org_id,
            fixture.device_id,
            last_seen_at=now - timedelta(hours=2),
            status="offline",
        )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        pushes = await run_policy_reconcile(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert pushes == []
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, Job, org_id=fixture.org_id) == 0


# ---------------------------------------------------------------------------
# health-retention
# ---------------------------------------------------------------------------


async def test_retention_deletes_past_the_boundary_and_not_at_it(admin_engine) -> None:
    """Both sides of `alert_retention_days`, and the action rows cascade.

    A test with only the "past the boundary" case passes against a sweep with an
    off-by-one that deletes a day early, which on a 90-day minimum is a day of
    incident history nobody can get back.
    """
    from app.models import HealthAlert, HealthAlertAction
    from app.modules.health.alerts import clear_alert, get_alert

    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, alert_retention_days=90)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = await get_alert(session, fixture.org_id, alert_id)
        await clear_alert(session, fixture.org_id, alert, reason="recovery", now=now)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        at_boundary = await run_retention(
            session, fixture.org_id, now=now + timedelta(days=90)
        )
        await session.commit()
    assert at_boundary["alerts_deleted"] == 0
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=fixture.org_id) == 1

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        past = await run_retention(
            session, fixture.org_id, now=now + timedelta(days=90, seconds=1)
        )
        await session.commit()
    assert past["alerts_deleted"] == 1
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=fixture.org_id) == 0
        assert await _count(session, HealthAlertAction, org_id=fixture.org_id) == 0


async def test_retention_never_deletes_an_alert_that_is_still_open(admin_engine) -> None:
    """An alert that is 400 days old and STILL OPEN is a finding, not a record.
    Deleting it would be the module quietly forgetting a problem nobody fixed --
    §0.8's failure mode (a) with a retention policy as its cover."""
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, alert_retention_days=90)
    now = datetime.now(UTC)
    await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        counts = await run_retention(session, fixture.org_id, now=now + timedelta(days=500))
        await session.commit()
    assert counts["alerts_deleted"] == 0
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=fixture.org_id) == 1


async def test_a_retired_check_is_collected_only_once_its_last_alert_is_gone(
    admin_engine,
) -> None:
    """§12's third step in the order §12 states it: delete the alerts past
    `alert_retention_days`, THEN hard-delete retired checks no alert still
    references. §2.4's amendment accepts a soft delete and says plainly that
    "soft deletes leak into every query that forgot them"; this sweep is the one
    place carrying the OPPOSITE predicate, and without it retired rows are
    immortal.

    BOTH HALVES, AND THE ORDER BETWEEN THEM IS THE TEST. Before the retention
    boundary the check must SURVIVE -- its alert still references it, and
    `ON DELETE RESTRICT` would refuse the delete anyway, so a sweep that tried
    would be a crash rather than a collection. After the boundary the alert goes
    and the check follows in the same pass.

    A FINDING ABOUT THE CODE, recorded rather than worked around: a check that
    NEVER alarmed is not retired at all. `replace_checks` hard-deletes it
    outright (its own docstring: "or hard-deleted if it never alarmed"), because
    RESTRICT does not fire for a check with no alerts. So the only rows this
    sweep can ever collect are checks that alarmed, were retired, and whose
    alerts have since aged out -- which is exactly the population §2.4's cost
    paragraph is about.
    """
    from app.models import HealthAlert, HealthCheck
    from app.modules.health.profiles import replace_checks

    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, alert_retention_days=90)
    never_alarmed_id = await seed_check(
        admin_engine,
        fixture.org_id,
        fixture.profile_id,
        check_type="memory.used_pct",
        comparator="gt",
        threshold_num=90,
    )
    now = datetime.now(UTC)
    alerted_id = fixture.check_id
    await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await replace_checks(
            session, fixture.org_id, fixture.org.user_id, fixture.profile_id, []
        )
        await session.commit()

    async def check_ids() -> list[str]:
        async with AsyncSession(admin_engine) as session:
            return sorted(
                str(i)
                for i in (
                    await session.execute(
                        select(HealthCheck.id).where(HealthCheck.org_id == fixture.org_id)
                    )
                )
                .scalars()
                .all()
            )

    surviving = await check_ids()
    assert str(never_alarmed_id) not in surviving, (
        "a check that never alarmed is hard-deleted by the retire path itself, not retired"
    )
    assert str(alerted_id) in surviving

    # Before the retention boundary: the alert still holds the reference.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        early = await run_retention(session, fixture.org_id, now=now + timedelta(days=1))
        await session.commit()
    # The flap row IS pruned here and that is correct, not incidental: the
    # alert was cleared `policy_removed` by the retire, so its key has no open
    # alert and the window is dead weight. Asserted rather than ignored, so a
    # change to the prune predicate shows up in this test too.
    assert early["alerts_deleted"] == 0
    assert early["retired_checks_deleted"] == 0
    assert str(alerted_id) in await check_ids()

    # Past it: the alert goes, and the retired check follows in the same pass.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        late = await run_retention(session, fixture.org_id, now=now + timedelta(days=91))
        await session.commit()
    assert late["alerts_deleted"] == 1
    assert late["retired_checks_deleted"] == 1
    assert await check_ids() == []
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=fixture.org_id) == 0


async def test_retention_prunes_a_flap_row_only_when_no_alert_is_open(admin_engine) -> None:
    """§12: "prune `health_flap_state` for keys with no alert in a flap window."

    Both halves again: a key with an OPEN alert keeps its window (pruning it
    would reset the flap count for a device that is actively oscillating, which
    is the one case the count exists for), and a quiet key is collected.
    """
    from app.models import HealthFlapState
    from app.modules.health.alerts import clear_alert, get_alert

    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, flap_window_s=3600)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        kept = await run_retention(session, fixture.org_id, now=now + timedelta(days=1))
        await session.commit()
    assert kept["flap_rows_pruned"] == 0
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthFlapState, org_id=fixture.org_id) == 1

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = await get_alert(session, fixture.org_id, alert_id)
        await clear_alert(session, fixture.org_id, alert, reason="recovery", now=now)
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        pruned = await run_retention(session, fixture.org_id, now=now + timedelta(days=1))
        await session.commit()
    assert pruned["flap_rows_pruned"] == 1
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthFlapState, org_id=fixture.org_id) == 0


# ---------------------------------------------------------------------------
# Tenancy, on the ADMIN engine
# ---------------------------------------------------------------------------


async def test_one_orgs_sweep_does_not_touch_anothers(admin_engine, tmp_path) -> None:
    """Invariant #5 across all four row-touching sweeps, pinned where RLS is not
    the thing filtering.

    GUARD OBSERVABILITY, AND A FINDING ABOUT WHICH PREDICATE IS LOAD-BEARING
    WHERE. Deleting `org_id` from RETENTION's alert query alone is enough to
    delete org B's aged alert with org A's cycle, and this test observes that
    directly -- org B's row count goes to zero.

    Deleting it from the ESCALATION query alone is NOT enough, verified rather
    than assumed: `_escalate` then selects org B's alert, but its per-alert
    `HealthCheck` lookup carries its own `org_id` predicate, resolves `None` for
    a foreign check, and skips the alert. Two independent predicates, and it
    takes removing both to cross a tenant boundary. Recorded because a reader
    who deletes one and sees this test stay green must know why.
    """
    from app.models import HealthAlert, Job
    from app.modules.health.alerts import clear_alert, get_alert

    a = await _armed_org(admin_engine)
    b = await _armed_org(admin_engine)
    await seed_settings(admin_engine, a.org_id, escalate_after_s=300, alert_retention_days=90)
    await seed_settings(admin_engine, b.org_id, escalate_after_s=300, alert_retention_days=90)
    now = datetime.now(UTC)
    await _open_one(admin_engine, a, now=now)
    b_alert = await _open_one(admin_engine, b, now=now)

    # A SECOND alert for org B, cleared long ago -- the row org A's retention
    # pass would delete if its query were not org-scoped.
    b_old_device = await seed_device(admin_engine, b.org_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, b_old_device)
        from app.modules.health.ingest import ingest_report

        await ingest_report(
            session, b.org_id, device, _msg([_line(b.check_id)]), now=now
        )
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        aged = (
            await session.execute(
                select(HealthAlert.id).where(
                    HealthAlert.org_id == b.org_id, HealthAlert.device_id == b_old_device
                )
            )
        ).scalar_one()
        alert = await get_alert(session, b.org_id, aged)
        await clear_alert(session, b.org_id, alert, reason="recovery", now=now)
        await session.commit()

    later = now + timedelta(days=200)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        counts = await run_alert_sweep(session, a.org_id, now=later)
        await run_policy_reconcile(session, a.org_id, job_engine=_engine(tmp_path), now=later)
        retention = await run_retention(session, a.org_id, now=later)
        await session.commit()
    assert counts["escalated"] == 1
    assert retention["alerts_deleted"] == 0, "org A has no cleared alert to delete"

    assert await _events(admin_engine, b.org_id, "health.alert_escalated") == []
    async with AsyncSession(admin_engine) as session:
        stamped = (
            await session.execute(
                select(HealthAlert.escalated_at).where(HealthAlert.id == b_alert)
            )
        ).scalar_one()
        assert stamped is None
        assert await _count(session, Job, org_id=b.org_id) == 0
        assert await _count(session, HealthAlert, org_id=b.org_id) == 2, (
            "org A's retention pass deleted an alert belonging to org B"
        )


# ---------------------------------------------------------------------------
# The beat entries and `register_worker_job_types`
# ---------------------------------------------------------------------------


def test_the_five_beat_entries_are_registered_with_the_right_intervals(
    pg_cluster, tmp_path
) -> None:
    """§12's table, pinned. The intervals are project constants imported from
    `sweep.py` rather than repeated as literals here -- a test carrying its own
    copy of 60.0 passes when the constant changes and the beat entry does not.
    """
    from app.worker import create_celery

    with override_settings(_settings(pg_cluster, tmp_path)):
        schedule = create_celery().conf.beat_schedule
    expected = {
        "health-alert-actions": ("app.worker.health_alert_actions", ACTION_INTERVAL_S),
        "health-alert-sweep": ("app.worker.health_alert_sweep", ALERT_SWEEP_INTERVAL_S),
        "health-offline-sweep": ("app.worker.health_offline_sweep", OFFLINE_SWEEP_INTERVAL_S),
        "health-policy-reconcile": (
            "app.worker.health_policy_reconcile",
            POLICY_RECONCILE_INTERVAL_S,
        ),
        "health-retention": ("app.worker.health_retention", RETENTION_INTERVAL_S),
    }
    for name, (task, interval) in expected.items():
        assert name in schedule, f"beat entry {name!r} is missing"
        assert schedule[name]["task"] == task
        assert schedule[name]["schedule"] == interval


def test_create_celery_registers_the_health_job_types(pg_cluster, tmp_path) -> None:
    """§12: "without it the FIRST SWEEP would raise `UnknownJobType`."

    The worker process dispatches `health.policy.apply` (reconcile) and
    remediations without ever importing the API's registration path, so this is
    the only thing that puts them in the registry there.
    """
    from app.jobs import registry as job_registry
    from app.jobs.registry import UnknownJobType, get_job_type, registered_names
    from app.worker import create_celery

    # Unregister first, so the assertion below is about `create_celery` and not
    # about whatever an earlier test in this process happened to import. The
    # `pytest.raises` is the positive control for that removal.
    for name in ("health.policy.apply", "health.check.now"):
        job_registry._registry.pop(name, None)  # noqa: SLF001
    with pytest.raises(UnknownJobType):
        get_job_type("health.policy.apply")

    with override_settings(_settings(pg_cluster, tmp_path)):
        create_celery()
    names = registered_names()
    assert "health.policy.apply" in names
    assert "health.check.now" in names


def test_the_intervals_are_the_spec_table() -> None:
    """§12's five numbers, asserted once so a change is a deliberate edit rather
    than a drift nobody notices."""
    assert ACTION_INTERVAL_S == 30.0
    assert ALERT_SWEEP_INTERVAL_S == 60.0
    assert OFFLINE_SWEEP_INTERVAL_S == 60.0
    assert POLICY_RECONCILE_INTERVAL_S == 900.0
    assert RETENTION_INTERVAL_S == 86400.0
