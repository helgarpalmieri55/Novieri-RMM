"""The action worker: order, fuses, the autonomy dial, approvals, idempotency
(`docs/modules/endpoint-health-alerting/spec.md` §0.10, §7, §8).

WHAT THESE FIXTURES HOLD CONSTANT, and what each test deliberately varies:

  - `inventory.collect` is the default seeded `job_type` because it is the one
    registered type that is NOT in `_DESTRUCTIVE_JOB_TYPES` and needs no script
    pin, so an action can be `autonomous` without arguing with the CHECK. The
    tests that care about destructiveness vary it (`script.run`,
    `backup.restore`) rather than leaning on the default.
  - `cooldown_s` defaults to 0 in the seed helper, so the fuse tests that mean
    to exercise the per-hour fuse are not silently satisfied by the cooldown --
    three fuses that all trip at once are indistinguishable from one.
  - Every `now` is injected. The fuse windows (1 hour, 24 hours) are compared
    against `jobs.created_at`, which is set by the DATABASE, so the tests that
    pre-load the fuse insert `Job` rows with explicit `created_at` values rather
    than hoping the clock cooperates.
  - The org has a ticket queue AND category everywhere except the one test that
    asserts `open_ticket` skips without them.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.signing import LocalEd25519Signer
from app.jobs.engine import JobEngine
from app.modules.health.actions import (
    approve_action,
    auto_resolve_alert_ticket,
    dispatch_manual_remediation,
    execute_pending_actions,
    refuse_action,
)
from app.modules.health.errors import HealthNotFoundError, HealthValidationError
from tests.health_action_fixtures import (
    seed_action_row,
    seed_approved_action,
    seed_script,
    seed_ticketing,
)
from tests.health_fixtures import seed_settings
from tests.test_health_clear_predicate import _open_one
from tests.test_health_ingest import _armed_org, _count, _device_row, _line, _msg

pytestmark = pytest.mark.usefixtures("admin_engine")

# §20's SECOND permission, for the tests whose subject is NOT that gate.
#
# `dispatch_manual_remediation` and `approve_action` require the permission the
# resolved job type's own module dispatch route requires, ON TOP of the route's
# `health.remediate` -- see `app/jobs/dispatch_permissions.py` and
# `tests/test_health_remediation_authz.py`, which is where that gate is actually
# tested. Every call below that is about ordering, fuses, locking or idempotency
# passes this set so it exercises what it says it does.
#
# SPELLED OUT, NEVER DERIVED FROM `dispatch_permission_for`. A fixture that
# asked the implementation which grant it wanted would hold for any answer it
# gave, including "none" -- the map would then be free to widen and every test
# here would follow it silently. These four are the codes the four job types
# this file seeds are gated on, typed by hand from the map.
DISPATCH_GRANTS: tuple[str, ...] = (
    "jobs.dispatch",       # inventory.collect
    "patch.apply",         # patch.apply
    "patch.scan",          # patch.scan
    "deploy.script.run",   # script.run
)


@pytest.fixture(autouse=True)
def _job_types_registered() -> None:
    """§7.1 clause 1 refuses an action whose `job_type` is no longer registered,
    so the registry must be populated in this process or EVERY remediation test
    would be refused for the wrong reason -- and would still look "correctly
    refused" from the outside. `register_worker_job_types()` is the same call
    the worker makes, so a job type these tests can dispatch is exactly one a
    sweep could."""
    from app.worker import register_worker_job_types

    register_worker_job_types()


def _engine(tmp_path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "sign.key"))


async def _armed_with_ticketing(admin_engine):
    fixture = await _armed_org(admin_engine)
    refs = await seed_ticketing(admin_engine, fixture.org_id, fixture.org.user_id)
    await seed_settings(
        admin_engine,
        fixture.org_id,
        default_alert_queue_id=refs.queue_id,
        default_ticket_category_id=refs.category_id,
    )
    return fixture, refs


# ---------------------------------------------------------------------------
# §0.10's fixed order
# ---------------------------------------------------------------------------


async def test_the_three_actions_run_in_the_order_remediate_ticket_notify(
    admin_engine, tmp_path
) -> None:
    """§0.10's order is not configurable, and each position is load-bearing.

    The OBSERVABLE that proves the order is the ticket DESCRIPTION: it names the
    remediation's job id, which is only possible if `remediate` ran first in the
    same pass. Asserting "three rows are done" would pass against any order at
    all.
    """
    from app.models import HealthAlertAction, Ticket

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy="autonomous"
    )
    await seed_action_row(
        admin_engine,
        fixture.org_id,
        alert_id,
        "remediate",
        approved_action_id=action_id,
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    assert (outcome.dispatched, outcome.ticketed, outcome.notified) == (1, 1, 1)
    assert len(outcome.envelopes) == 1

    async with AsyncSession(admin_engine) as session:
        job_id = (
            await session.execute(
                select(HealthAlertAction.job_id).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "remediate",
                )
            )
        ).scalar_one()
        description = (
            await session.execute(
                select(Ticket.description).where(Ticket.source_alert_id == alert_id)
            )
        ).scalar_one()
    assert str(job_id) in description, (
        "the ticket must be able to name the job the same pass dispatched"
    )


async def test_the_dispatched_job_is_signed_and_targets_exactly_one_device(
    admin_engine, tmp_path
) -> None:
    """Invariant #1 and §8.4's structural claim, in one assertion each.

    `signature` is non-NULL because it went through `JobEngine.create`, and
    `target_type` is `device` with `target_id` the alert's own device -- there
    is no target union in this module's dispatcher signature to pass a group
    through.
    """
    from app.models import Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy="autonomous"
    )
    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.org_id == fixture.org_id))
        ).scalar_one()
    assert job.signature is not None and len(job.signature) > 0
    assert job.type == "inventory.collect"
    assert job.target_type == "device"
    assert job.target_id == fixture.device_id


async def test_a_remediation_dispatch_is_audited_beyond_the_kernels_own_record(
    admin_engine, tmp_path
) -> None:
    """§11.3: `health.remediation_dispatched` is written IN ADDITION TO the
    kernel's `jobs.dispatched`, "because the kernel record says a job was sent
    and this record says WHY A MACHINE DECIDED TO SEND IT".

    Both are asserted, because a test that only checked ours would pass against
    an implementation that bypassed `JobEngine` entirely.
    """
    from app.models import AuditEvent

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy="autonomous"
    )
    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        rows = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == fixture.org_id,
                        AuditEvent.action == "health.remediation_dispatched",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    meta = rows[0].meta
    assert set(meta) >= {"alert_id", "action_key", "job_id", "autonomy", "approved_by"}
    assert meta["autonomy"] == "autonomous"
    assert meta["approved_by"] is None, "a machine decided; nobody approved it"


# ---------------------------------------------------------------------------
# Idempotency -- carried by the indexes, not by application dedup
# ---------------------------------------------------------------------------


async def test_a_second_sweep_pass_creates_no_second_ticket_and_no_second_job(
    admin_engine, tmp_path
) -> None:
    """§0.10: "an at-least-once evaluation ... produces ONE ticket, not one per
    tick."

    ASSERTED ON COUNTS, never on an exception. The mechanism is `UNIQUE
    (alert_id, kind)` plus the fact that a row that reached a terminal state is
    no longer `pending`, so the second pass selects nothing -- which is why the
    third assertion (the outcome of pass two is all zeros) matters as much as
    the row counts.
    """
    from app.models import Job, Ticket

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy="autonomous"
    )
    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )

    for _pass in range(2):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            outcome = await execute_pending_actions(
                session, fixture.org_id, job_engine=_engine(tmp_path), now=now
            )
            await session.commit()

    assert (outcome.dispatched, outcome.ticketed, outcome.notified) == (0, 0, 0)
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, Job, org_id=fixture.org_id) == 1
        assert await _count(session, Ticket, org_id=fixture.org_id) == 1


async def test_the_unique_index_is_what_forbids_a_second_action_row(admin_engine) -> None:
    """The plan says not to add application-level dedup on top of the indexes,
    so this asserts the INDEX carries it: a second `(alert_id, 'remediate')` row
    inserted directly on the admin engine -- no service, no validation -- raises.

    GUARD OBSERVABILITY: without `uq_health_alert_action_alert_kind` this
    observes TWO stored rows, which is one remediation per sweep tick forever.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models import HealthAlertAction

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    await seed_action_row(admin_engine, fixture.org_id, alert_id, "remediate")

    async with AsyncSession(admin_engine) as session:
        session.add(
            HealthAlertAction(
                org_id=fixture.org_id, alert_id=alert_id, kind="remediate", state="pending"
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    # Positive control: a DIFFERENT kind for the same alert is legal, so the
    # rejection above is the (alert_id, kind) pair and not the alert.
    async with AsyncSession(admin_engine) as session:
        session.add(
            HealthAlertAction(
                org_id=fixture.org_id, alert_id=alert_id, kind="open_ticket", state="pending"
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        kinds = sorted(
            (
                await session.execute(
                    select(HealthAlertAction.kind).where(
                        HealthAlertAction.alert_id == alert_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert kinds == ["notify", "open_ticket", "remediate"]


# ---------------------------------------------------------------------------
# §7.1's fuses -- exercised as a BATCH, which is what §8.4 is about
# ---------------------------------------------------------------------------


async def test_twenty_one_alerts_in_one_hour_give_twenty_dispatches_and_one_block(
    admin_engine, tmp_path
) -> None:
    """§8.4's real fleet-wide risk, as a test: "4,000 individual device-scoped
    jobs fired in the same minute because an NTP jump, a DNS outage, or a bad
    monitoring assumption tripped one check across the whole estate at once."

    RUN AS ONE BATCH, not as 21 single calls -- the per-org fuse is the only one
    that can see the batch, and a per-call test would be satisfied by a fuse
    that counted nothing. `max_per_device_per_day` is raised out of the way and
    `cooldown_s` is 0, so the ONLY fuse that can trip is the per-org one; three
    fuses tripping together would be indistinguishable from one.

    GUARD OBSERVABILITY: delete the per-org fuse and this observes 21 dispatched
    and 0 blocked -- i.e. the estate-wide event arriving on the endpoints.
    """
    from app.models import Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    action_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        autonomy="autonomous",
        cooldown_s=0,
        max_per_device_per_day=100,
        max_per_org_per_hour=20,
    )

    # 21 devices, one alert each: device-scoped jobs, estate-wide event.
    from app.models import HealthAlert
    from tests.health_fixtures import seed_device

    for index in range(21):
        device_id = await seed_device(admin_engine, fixture.org_id)
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            device = await _device_row(session, device_id)
            from app.modules.health.ingest import ingest_report

            await ingest_report(
                session,
                fixture.org_id,
                device,
                _msg([_line(fixture.check_id, value=f"9{index % 10}.0")]),
                now=now,
            )
            await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert_ids = list(
            (
                await session.execute(
                    select(HealthAlert.id).where(HealthAlert.org_id == fixture.org_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(alert_ids) == 21
        await session.commit()
    for alert_id in alert_ids:
        await seed_action_row(
            admin_engine,
            fixture.org_id,
            alert_id,
            "remediate",
            approved_action_id=action_id,
        )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    assert outcome.dispatched == 20
    assert outcome.blocked == 1
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, Job, org_id=fixture.org_id) == 20


async def test_a_blocked_remediation_names_the_fuse_its_window_and_the_job_ids(
    admin_engine, tmp_path
) -> None:
    """§7.1's amendment, required in v1: "`last_error` names the fuse that
    tripped, its window, and the JOB IDS counted against it -- because the fuse
    counts `jobs` by `type`, so what consumed the budget may be another approved
    action sharing `script.run` or a technician's manual run, and a bare 'rate
    limited' leaves an operator with no way to tell which."

    BLOCKED, NOT QUEUED (§7.1): the row is terminal, not deferred. "A
    remediation deferred an hour is a remediation applied to a situation that
    has changed."
    """
    from app.models import HealthAlertAction, Job, JobStatus

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        autonomy="autonomous",
        cooldown_s=0,
        max_per_device_per_day=1,
        max_per_org_per_hour=100,
    )

    # A job of the same type on the same device, an hour ago -- exactly the
    # "somebody else consumed the budget" case the amendment is about.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        prior = Job(
            org_id=fixture.org_id,
            type="inventory.collect",
            target_type="device",
            target_id=fixture.device_id,
            params={},
            nonce=uuid4().hex,
            issued_at=now - timedelta(hours=2),
            expires_at=now,
            status=JobStatus.succeeded,
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
        )
        session.add(prior)
        await session.commit()
        prior_id = prior.id

    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert outcome.blocked == 1

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "remediate",
                )
            )
        ).scalar_one()
    assert row.state == "blocked_rate_limit"
    assert "max_per_device_per_day" in (row.last_error or "")
    assert "24h" in (row.last_error or "")
    # THE JOB ID, which is the half of the amendment this test is named for.
    # This assertion read `str(prior_id) not in (row.last_error or "") or True`
    # -- unconditionally true, with a comment ("per-device fuse counts, not
    # lists") conceding the very thing the test name promises. The per-device
    # branch really did return a bare count; both the fuse and the assertion
    # were wrong, and the tautology is why nobody found out.
    assert str(prior_id) in (row.last_error or ""), (
        f"§7.1's amendment: the per-device fuse must name the job ids counted against it, "
        f"and it said {row.last_error!r}"
    )
    # And the ticket and the notification were NOT blocked with it: §0.10's
    # "the actions are independently retryable and independently terminal".
    assert (outcome.ticketed, outcome.notified) == (1, 1)


async def test_the_cooldown_fuse_names_the_job_that_consumed_it(
    admin_engine, tmp_path
) -> None:
    """The cooldown branch specifically, because it is the one that CAN name job
    ids -- and §7.1's amendment asks for them by name."""
    from app.models import HealthAlertAction, Job, JobStatus

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        autonomy="autonomous",
        cooldown_s=3600,
        max_per_device_per_day=100,
        max_per_org_per_hour=100,
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        prior = Job(
            org_id=fixture.org_id,
            type="inventory.collect",
            target_type="device",
            target_id=fixture.device_id,
            params={},
            nonce=uuid4().hex,
            issued_at=now - timedelta(minutes=5),
            expires_at=now + timedelta(hours=1),
            status=JobStatus.succeeded,
            created_at=now - timedelta(minutes=5),
            updated_at=now - timedelta(minutes=5),
        )
        session.add(prior)
        await session.commit()
        prior_id = prior.id

    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "remediate",
                )
            )
        ).scalar_one()
    assert row.state == "blocked_rate_limit"
    assert str(prior_id) in (row.last_error or ""), (
        "an operator who cannot see WHAT consumed the budget cannot tell which case they are in"
    )


# ---------------------------------------------------------------------------
# §8.3's autonomy dial
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("autonomy", "expected_state", "expected_jobs"),
    [
        ("suggest", "skipped", 0),
        ("require_approval", "awaiting_approval", 0),
        ("autonomous", "dispatched", 1),
    ],
)
async def test_the_autonomy_dial_decides_whether_anything_reaches_the_device(
    admin_engine, tmp_path, autonomy: str, expected_state: str, expected_jobs: int
) -> None:
    """All three settings, because the dial is only meaningful as a contrast.

    A test with `autonomous` alone passes against an implementation that ignores
    the column entirely -- which is the failure that turns `require_approval`
    into "autonomous after 30 seconds".
    """
    from app.models import HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy=autonomy
    )
    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        state = (
            await session.execute(
                select(HealthAlertAction.state).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "remediate",
                )
            )
        ).scalar_one()
        jobs = await _count(session, Job, org_id=fixture.org_id)
    assert state == expected_state
    assert jobs == expected_jobs


async def test_a_stale_destructive_column_does_not_get_an_autonomous_dispatch(
    admin_engine, tmp_path
) -> None:
    """§8.3's gate re-derived AT DISPATCH, not read off the row.

    THE ROW IS THE HOLE. `approved_action.destructive` is a STORED column;
    `_validate_and_derive` writes it and `ck_approved_action_autonomous_is_bounded`
    reads it, and until now the dispatcher branched on `autonomy` and never
    looked at it at all. So the entire distance between an autonomous action and
    an arbitrary-code `script.run` signed as SYSTEM/root was one boolean nobody
    re-checked. ca7b595 keeps that boolean FRESH across a shared script
    exemption; this asserts the dispatcher no longer TRUSTS it alone, which is a
    different defence -- the derivation's other inputs
    (`_DESTRUCTIVE_JOB_TYPES`, `DESTRUCTIVE_PARAM_KEYS`) are CODE, and a deploy
    can change the truth of every stored row in every tenant with no write to
    touch them and no CHECK evaluated to notice.

    SEEDED DIRECTLY, WHICH IS THE ONLY WAY THIS ROW CAN EXIST. `script.run` with
    a valid pin and NO exemption derives destructive; the service would refuse
    to store it as `autonomous`, and the CHECK would refuse a stored
    `destructive = true` beside it. `destructive = false` is exactly the stale
    value the CHECK cannot see, and it passes the constraint.

    THE ASSERTIONS ARE THE ROW AND THE JOB TABLE. Before the fix this is
    `state='dispatched'` with a signed `script.run` carrying
    `Restart-Service -Name Spooler` on the wire; after it, the remediation
    survives as an approval request addressed to a human.
    """
    from app.models import DomainEvent, HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    script_id = await seed_script(
        admin_engine, fixture.org_id, fixture.org.user_id, name="Restart Print Spooler"
    )
    action_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        job_type="script.run",
        script_id=script_id,
        script_version=1,
        # THE LIE. No exemption exists for (script_id, 1), so the derivation
        # says destructive; the stored column says otherwise and the CHECK
        # cannot tell.
        destructive=False,
        risk_class="low",
        autonomy="autonomous",
    )
    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "remediate",
                )
            )
        ).scalar_one()
        jobs = list(
            (
                await session.execute(select(Job).where(Job.org_id == fixture.org_id))
            ).scalars().all()
        )
        events = list(
            (
                await session.execute(
                    select(DomainEvent.event_type).where(
                        DomainEvent.org_id == fixture.org_id,
                        DomainEvent.event_type == "health.remediation_requires_approval",
                    )
                )
            ).scalars().all()
        )

    assert [(job.type, job.params.get("body")) for job in jobs] == [], (
        "a machine dispatched a destructive action: the stored `destructive` column was the "
        "only thing in the way and it was stale"
    )
    assert row.state == "awaiting_approval"
    assert row.job_id is None
    assert "re-deriving it at dispatch says DESTRUCTIVE" in (row.last_error or "")
    assert row.expires_at is not None, "a demoted row must expire like any approval request"
    assert (outcome.dispatched, outcome.awaiting_approval) == (0, 1)
    # The human it now needs is actually told: a demotion nobody hears about is
    # `autonomous` silently becoming "never".
    assert events == ["health.remediation_requires_approval"]


async def test_a_correctly_stored_non_destructive_action_still_dispatches(
    admin_engine, tmp_path
) -> None:
    """THE CONTRAST, and the reason the check above is not simply "never
    dispatch".

    An EXEMPTED `script.run` derives non-destructive, so the same row shape with
    a real exemption behind it must still reach the device. Without this, a
    re-derivation that returned `True` unconditionally would pass every
    assertion in the test above and would have silently disabled autonomous
    remediation for the whole platform.
    """
    from app.models import ApprovedActionScriptExemption, HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    script_id = await seed_script(admin_engine, fixture.org_id, fixture.org.user_id)
    action_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        job_type="script.run",
        script_id=script_id,
        script_version=1,
        destructive=False,
        risk_class="low",
        autonomy="autonomous",
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            ApprovedActionScriptExemption(
                org_id=fixture.org_id,
                script_id=script_id,
                script_version=1,
                reason="reviewed",
                granted_by=fixture.org.user_id,
                granted_at=now,
            )
        )
        await session.commit()
    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "remediate",
                )
            )
        ).scalar_one()
        jobs = list(
            (
                await session.execute(select(Job).where(Job.org_id == fixture.org_id))
            ).scalars().all()
        )
    assert [job.type for job in jobs] == ["script.run"], (
        f"the exempted action did not dispatch; row said {row.state!r} "
        f"({row.last_error!r})"
    )
    assert row.state == "dispatched"
    assert (outcome.dispatched, outcome.awaiting_approval) == (1, 0)


async def test_require_approval_publishes_its_request_even_for_a_silent_check(
    admin_engine, tmp_path
) -> None:
    """§7.3: `health.remediation_requires_approval` is ALWAYS published, silent
    check or not. "It is not an alert notification -- it is a request addressed
    to a named human who must act", and an approval request nobody is told about
    expires at `approval_ttl_s`, i.e. the dial's middle setting silently meaning
    "never".

    The payload names the PINNED SCRIPT (§13, §2.10: "nobody approves a script
    they cannot identify"), and never the body -- arbitrary code is why §8.4
    makes `script.run` destructive by default in the first place.
    """
    from app.models import DomainEvent, HealthAlert

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    script_id = await seed_script(
        admin_engine, fixture.org_id, fixture.org.user_id, name="Restart Print Spooler"
    )
    action_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        job_type="script.run",
        script_id=script_id,
        script_version=1,
        destructive=True,
        risk_class="medium",
        autonomy="require_approval",
    )
    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        event = (
            await session.execute(
                select(DomainEvent).where(
                    DomainEvent.org_id == fixture.org_id,
                    DomainEvent.event_type == "health.remediation_requires_approval",
                )
            )
        ).scalar_one()
        alert_client = (
            await session.execute(select(HealthAlert.client_id).where(HealthAlert.id == alert_id))
        ).scalar_one()
    # Phase 2 Task 1: the event names the alert's client.
    assert event.client_id == alert_client
    assert event.payload["script_name"] == "Restart Print Spooler"
    assert event.payload["script_version"] == 1
    assert "Restart-Service" not in str(event.payload), "the script BODY never travels"
    assert "notify" not in event.payload, "§9.1: the flag was removed, do not re-add it"


async def test_approving_dispatches_the_job_and_names_the_approver(
    admin_engine, tmp_path
) -> None:
    from app.models import AuditEvent, HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy="require_approval"
    )
    row_id = await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        job_id, envelope = await approve_action(
            session,
            fixture.org_id,
            alert_id,
            row_id,
            actor_id=fixture.org.user_id,
            permissions=DISPATCH_GRANTS,
            job_engine=_engine(tmp_path),
            now=now + timedelta(minutes=1),
        )
        await session.commit()

    assert envelope["kind"] == "job"
    assert envelope["sig"]
    async with AsyncSession(admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        row = (
            await session.execute(
                select(HealthAlertAction).where(HealthAlertAction.id == row_id)
            )
        ).scalar_one()
        audits = list(
            (
                await session.execute(
                    select(AuditEvent.action).where(
                        AuditEvent.org_id == fixture.org_id,
                        AuditEvent.target_id == str(alert_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert job.requested_by == fixture.org.user_id
    assert job.signature is not None
    assert row.state == "dispatched"
    assert row.approved_by == fixture.org.user_id
    assert "health.remediation_approved" in audits
    assert "health.remediation_dispatched" in audits


async def test_refusing_is_terminal_and_dispatches_nothing(admin_engine, tmp_path) -> None:
    from app.models import AuditEvent, HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy="require_approval"
    )
    row_id = await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await refuse_action(
            session,
            fixture.org_id,
            alert_id,
            row_id,
            actor_id=fixture.org.user_id,
            reason="the disk is full for a reason",
        )
        await session.commit()

    # A second sweep must not resurrect it: `UNIQUE (alert_id, kind)` means
    # there is no second attempt to make.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session,
            fixture.org_id,
            job_engine=_engine(tmp_path),
            now=now + timedelta(minutes=5),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(HealthAlertAction.id == row_id)
            )
        ).scalar_one()
        assert row.state == "skipped"
        assert "the disk is full for a reason" in (row.last_error or "")
        assert await _count(session, Job, org_id=fixture.org_id) == 0
        audits = list(
            (
                await session.execute(
                    select(AuditEvent.action).where(
                        AuditEvent.org_id == fixture.org_id,
                        AuditEvent.action == "health.remediation_refused",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert audits == ["health.remediation_refused"]


# ---------------------------------------------------------------------------
# §7.1 clause 1's refusals
# ---------------------------------------------------------------------------


async def test_an_edited_script_refuses_the_remediation(admin_engine, tmp_path) -> None:
    """§7.1's amendment: "refuse when `approved_action.script_version` does not
    equal the live `scripts.version` -- the script was edited since it was
    approved". §2.10 records why that comparison is the only form pinning can
    take: `update_script` bumps the version IN PLACE and the approved body no
    longer exists anywhere.

    NOT RETRIED, because the fix is an admin re-approving the new version, not a
    delay -- so the row is terminal and `last_error` names BOTH versions.
    """
    from app.models import HealthAlertAction, Job, Script

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    script_id = await seed_script(admin_engine, fixture.org_id, fixture.org.user_id)
    action_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        job_type="script.run",
        script_id=script_id,
        script_version=1,
        destructive=True,
        autonomy="require_approval",
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        script = (
            await session.execute(select(Script).where(Script.id == script_id))
        ).scalar_one()
        script.version = 2
        script.body = "Remove-Item -Recurse -Force C:\\"
        session.add(script)
        await session.commit()

    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert outcome.dispatched == 0

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "remediate",
                )
            )
        ).scalar_one()
        assert row.state == "skipped"
        assert "version 1" in (row.last_error or "")
        assert "version 2" in (row.last_error or "")
        assert await _count(session, Job, org_id=fixture.org_id) == 0


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"active": False}, "inactive"),
        ({"personas": ["ai_admin"]}, "persona"),
    ],
)
async def test_clause_one_refusals(admin_engine, tmp_path, overrides, needle) -> None:
    """The other two refusals of §7.1 clause 1, each with its own fixture. A
    single "it refused" test cannot tell which clause did the refusing, and a
    clause that never fires is a clause that can be deleted unnoticed."""
    from app.models import HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy="autonomous", **overrides
    )
    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "remediate",
                )
            )
        ).scalar_one()
        assert row.state == "skipped"
        assert needle in (row.last_error or "")
        assert await _count(session, Job, org_id=fixture.org_id) == 0


async def test_a_dead_remediation_does_not_block_the_ticket_or_the_notification(
    admin_engine, tmp_path
) -> None:
    """§0.10: "a `dead` remediation does not block the ticket or the
    notification: the actions are independently retryable and independently
    terminal."

    A worker that abandoned the whole alert on the first failed action would
    leave a real breach with no ticket and nobody told -- the failure mode this
    independence exists to prevent.
    """
    from app.models import HealthAlertAction, Ticket

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    # An orphaned remediate row: no approved action at all.
    await seed_action_row(admin_engine, fixture.org_id, alert_id, "remediate")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    assert outcome.skipped == 1
    assert (outcome.ticketed, outcome.notified) == (1, 1)
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, Ticket, org_id=fixture.org_id) == 1
        states = {
            kind: state
            for kind, state in (
                await session.execute(
                    select(HealthAlertAction.kind, HealthAlertAction.state).where(
                        HealthAlertAction.alert_id == alert_id
                    )
                )
            ).all()
        }
    assert states == {"remediate": "skipped", "open_ticket": "done", "notify": "done"}


# ---------------------------------------------------------------------------
# §7.2 -- the alert-origin ticket contract, ASSERTED not re-implemented
# ---------------------------------------------------------------------------


async def test_the_alert_ticket_is_machine_attributed_by_the_ticket_core(
    admin_engine, tmp_path
) -> None:
    """§7.2 / §15: `create_ticket` already enforces `origin='alert' =>
    type='incident'`, already forces `requester_id`/`created_by` to NULL for
    alert origin, and already refuses `source_alert_id` for other origins, with
    `ck_ticket_human_attribution` behind it. "Nothing here needs building."

    So this asserts those behaviours rather than re-implementing them. A second
    copy of the rules in this module would be a second answer to who may create
    a machine-attributed ticket.
    """
    from app.models import Ticket

    fixture, refs = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        ticket = (
            await session.execute(select(Ticket).where(Ticket.source_alert_id == alert_id))
        ).scalar_one()
    assert ticket.origin == "alert"
    assert ticket.type == "incident"
    assert ticket.requester_id is None
    assert ticket.created_by is None
    assert ticket.queue_id == refs.queue_id
    assert ticket.device_id == fixture.device_id
    assert "Restart-Service" not in ticket.description
    assert "cpu.busy_pct" in ticket.description


async def test_open_ticket_is_skipped_when_the_org_configured_no_category(
    admin_engine, tmp_path
) -> None:
    """The unconfigured org. `TicketCreate.category_id` is a REQUIRED UUID and
    `create_ticket` raises without a resolvable queue, so an alert ticket has
    nowhere to land -- the row is skipped with a reason rather than raising
    inside a sweep that still has other orgs to serve."""
    from app.models import HealthAlertAction, Ticket

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    await seed_action_row(admin_engine, fixture.org_id, alert_id, "open_ticket")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    assert outcome.ticketed == 0
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "open_ticket",
                )
            )
        ).scalar_one()
        assert row.state == "skipped"
        assert "default_ticket_category_id" in (row.last_error or "")
        assert await _count(session, Ticket, org_id=fixture.org_id) == 0


# ---------------------------------------------------------------------------
# §7.4 -- auto-resolution, and each of the three "touched" signals
# ---------------------------------------------------------------------------


async def _alert_with_ticket(admin_engine, tmp_path, fixture):
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    from app.models import HealthAlert

    async with AsyncSession(admin_engine) as session:
        ticket_id = (
            await session.execute(
                select(HealthAlert.ticket_id).where(HealthAlert.id == alert_id)
            )
        ).scalar_one()
    return alert_id, ticket_id, now


async def test_an_untouched_alert_ticket_auto_resolves_and_is_never_closed(
    admin_engine, tmp_path
) -> None:
    """§7.4 / service-desk §7.2's pinned contract: `resolved`, NEVER `closed`.

    "Closing removes the ticket from every 'needs a human look' view, and the
    interesting case -- a disk that filled, was cleaned by remediation, and will
    fill again in six hours -- is exactly the one where the recovery is the
    LEAST informative event."

    The attribution assertion is the mitigation §7.4 accepts for the
    MTTR-laundering incentive: every status change carries `actor_type='system'`
    and `actor_id IS NULL`, so a report CAN separate machine-resolved from
    human-resolved. §13 requires it to.
    """
    from app.models import Ticket, TicketStatusHistory
    from app.modules.health.ingest import ingest_report

    fixture, _ = await _armed_with_ticketing(admin_engine)
    alert_id, ticket_id, now = await _alert_with_ticket(admin_engine, tmp_path, fixture)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="recovery", state="ok")]),
            now=now + timedelta(minutes=10),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        ticket = (
            await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()
        history = list(
            (
                await session.execute(
                    select(TicketStatusHistory)
                    .where(TicketStatusHistory.ticket_id == ticket_id)
                    .order_by(TicketStatusHistory.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert ticket.status == "resolved", "resolved, never closed"
    # THREE ROWS, NOT TWO, AND THE FIRST IS `create_ticket`'S OWN. Verified
    # against the code rather than assumed: creating an alert-origin ticket
    # writes a `('system', 'new')` history row, and the auto-resolution then
    # walks `new -> open -> resolved` because
    # `lifecycle._TRANSITIONS["incident"]["new"]` is `{"open", "cancelled"}` --
    # `new -> resolved` is NOT a legal edge. Two `change_status` calls, not one.
    assert [h.to_status for h in history] == ["new", "open", "resolved"]
    assert {h.actor_type for h in history} == {"system"}, (
        "§7.4's mitigation for the MTTR-laundering incentive is ATTRIBUTION: every row "
        "on a machine-resolved alert ticket must be separable from a human's"
    )
    assert {h.actor_id for h in history} == {None}


@pytest.mark.parametrize("signal", ["status", "comment", "assignee"])
async def test_a_human_touched_ticket_gets_a_comment_and_nothing_else(
    admin_engine, tmp_path, signal: str
) -> None:
    """§7.4's three "touched" signals, EACH TESTED INDEPENDENTLY.

    The plan is explicit about why: "a test with only one signal passes against
    an implementation that checks only that one." A technician mid-investigation
    whose ticket resolves itself under them loses it from their queue, and the
    recovery may be THEIR doing or may be the symptom vanishing while the cause
    remains.
    """
    from app.models import Ticket, TicketComment
    from app.modules.health.ingest import ingest_report
    from app.modules.service_desk.threads import add_comment
    from app.modules.service_desk.transitions import change_status

    fixture, _ = await _armed_with_ticketing(admin_engine)
    alert_id, ticket_id, now = await _alert_with_ticket(admin_engine, tmp_path, fixture)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        if signal == "status":
            await change_status(
                session,
                fixture.org_id,
                ticket_id,
                to_status="open",
                actor_id=fixture.org.user_id,
                actor_type="user",
            )
        elif signal == "comment":
            await add_comment(
                session,
                fixture.org_id,
                ticket_id,
                body="looking at this now",
                author_id=fixture.org.user_id,
                author_type="user",
            )
        else:
            ticket = (
                await session.execute(select(Ticket).where(Ticket.id == ticket_id))
            ).scalar_one()
            ticket.assigned_to = fixture.org.user_id
            session.add(ticket)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="recovery", state="ok")]),
            now=now + timedelta(minutes=10),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        ticket = (
            await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()
        system_comments = list(
            (
                await session.execute(
                    select(TicketComment.body).where(
                        TicketComment.ticket_id == ticket_id,
                        TicketComment.author_type == "system",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert ticket.status != "resolved", f"a {signal}-touched ticket must not auto-resolve"
    assert any("NOT auto-resolved" in body for body in system_comments)


async def test_auto_resolution_is_off_when_the_org_turned_it_off(
    admin_engine, tmp_path
) -> None:
    """`health_org_settings.auto_resolve_alert_tickets` is a real switch, not
    documentation. A single-value fixture would make the flag unreadable."""
    from app.models import Ticket
    from app.modules.health.ingest import ingest_report

    fixture, refs = await _armed_with_ticketing(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        from app.modules.health.settings import update_settings

        await update_settings(
            session,
            fixture.org_id,
            fixture.org.user_id,
            {"auto_resolve_alert_tickets": False},
        )
        await session.commit()

    alert_id, ticket_id, now = await _alert_with_ticket(admin_engine, tmp_path, fixture)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="recovery", state="ok")]),
            now=now + timedelta(minutes=10),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        status = (
            await session.execute(select(Ticket.status).where(Ticket.id == ticket_id))
        ).scalar_one()
    assert status == "new", "the org opted out; nothing may move its tickets"


# ---------------------------------------------------------------------------
# Manual dispatch (§10's `POST /alerts/{id}/remediate`)
# ---------------------------------------------------------------------------


async def test_a_manual_remediation_is_still_subject_to_the_fuses(
    admin_engine, tmp_path
) -> None:
    """§8.4's fleet-wide risk does not care who clicked. A manual path exempt
    from the fuses is a manual path that reproduces the estate-wide event one
    click at a time."""
    from app.models import Job, JobStatus

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        autonomy="suggest",
        cooldown_s=0,
        max_per_device_per_day=1,
    )
    async with AsyncSession(admin_engine) as session:
        key = (
            await session.execute(
                select(__import__("app.models", fromlist=["ApprovedAction"]).ApprovedAction.key)
                .where(
                    __import__("app.models", fromlist=["ApprovedAction"]).ApprovedAction.id
                    == action_id
                )
            )
        ).scalar_one()
        session.add(
            Job(
                org_id=fixture.org_id,
                type="inventory.collect",
                target_type="device",
                target_id=fixture.device_id,
                params={},
                nonce=uuid4().hex,
                issued_at=now - timedelta(hours=2),
                expires_at=now,
                status=JobStatus.succeeded,
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=2),
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError, match="max_per_device_per_day"):
            await dispatch_manual_remediation(
                session,
                fixture.org_id,
                alert_id,
                action_key=key,
                actor_id=fixture.org.user_id,
                permissions=DISPATCH_GRANTS,
                job_engine=_engine(tmp_path),
                now=now,
            )
        await session.rollback()

    async with AsyncSession(admin_engine) as session:
        assert await _count(session, Job, org_id=fixture.org_id) == 1


async def test_a_manual_remediation_dispatches_a_suggested_action(
    admin_engine, tmp_path
) -> None:
    """§8.3's `suggest` row: "the console offers one-click dispatch by a user
    holding `health.remediate`". The human IS the approval, so the dial does not
    apply -- but the audit record names them."""
    from app.models import ApprovedAction, AuditEvent, HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy="suggest"
    )
    async with AsyncSession(admin_engine) as session:
        key = (
            await session.execute(
                select(ApprovedAction.key).where(ApprovedAction.id == action_id)
            )
        ).scalar_one()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        job_id, envelope = await dispatch_manual_remediation(
            session,
            fixture.org_id,
            alert_id,
            action_key=key,
            actor_id=fixture.org.user_id,
            permissions=DISPATCH_GRANTS,
            job_engine=_engine(tmp_path),
            now=now,
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        row = (
            await session.execute(
                select(HealthAlertAction).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "remediate",
                )
            )
        ).scalar_one()
        audits = list(
            (
                await session.execute(
                    select(AuditEvent.action).where(
                        AuditEvent.org_id == fixture.org_id,
                        AuditEvent.action == "health.remediation_dispatched",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert job.requested_by == fixture.org.user_id
    assert job.signature is not None
    assert envelope["sig"]
    assert row.state == "dispatched"
    assert audits == ["health.remediation_dispatched"]


async def test_a_second_manual_remediation_for_one_alert_is_refused(
    admin_engine, tmp_path
) -> None:
    """`UNIQUE (alert_id, kind)`: one remediation dispatch per alert, FOREVER
    (§0.10). Asserted as a job COUNT, so an implementation that caught the
    IntegrityError and retried would still fail."""
    from app.models import ApprovedAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy="suggest", cooldown_s=0
    )
    async with AsyncSession(admin_engine) as session:
        key = (
            await session.execute(
                select(ApprovedAction.key).where(ApprovedAction.id == action_id)
            )
        ).scalar_one()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await dispatch_manual_remediation(
            session,
            fixture.org_id,
            alert_id,
            action_key=key,
            actor_id=fixture.org.user_id,
            permissions=DISPATCH_GRANTS,
            job_engine=_engine(tmp_path),
            now=now,
        )
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError, match="forever"):
            await dispatch_manual_remediation(
                session,
                fixture.org_id,
                alert_id,
                action_key=key,
                actor_id=fixture.org.user_id,
                permissions=DISPATCH_GRANTS,
                job_engine=_engine(tmp_path),
                now=now + timedelta(minutes=1),
            )
        await session.rollback()

    async with AsyncSession(admin_engine) as session:
        assert await _count(session, Job, org_id=fixture.org_id) == 1


# ---------------------------------------------------------------------------
# The ONE approval slot, and the console path that lands on it
#
# There is one `remediate` row per alert, so `POST /health/alerts/{id}/remediate`
# naming action Y consumes the slot action X's approval request is sitting in.
# Both tests below vary THE ACTION NAMED against the action requested, because a
# test that dispatched the same action the request names cannot tell "the slot
# was answered" from "the slot was overwritten".
# ---------------------------------------------------------------------------


async def _key_of(admin_engine, action_id: UUID) -> str:
    from app.models import ApprovedAction

    async with AsyncSession(admin_engine) as session:
        return (
            await session.execute(
                select(ApprovedAction.key).where(ApprovedAction.id == action_id)
            )
        ).scalar_one()


async def test_a_console_dispatch_answering_a_request_for_another_action_records_the_swap(
    admin_engine, tmp_path
) -> None:
    """A holder of `health.remediate` is asked to approve X and dispatches Y.

    ALLOWED -- "not that one, this one" is a legitimate answer, and the same
    permission covers both. NOT SILENT: the pending request for X is discarded
    to make room, and until this landed nothing anywhere recorded that. The
    `health.remediation_dispatched` record said only that Y went, so the named
    human who was asked about X had no way to learn what became of the question.

    The observable is `superseded_action_key` in that audit record, and the
    dispatched JOB TYPE beside it -- the two actions deliberately carry
    different job types, so a test that asserted only "a job was dispatched"
    could not tell which action actually ran.

    GUARD OBSERVABILITY: delete the `superseded` lookup in
    `dispatch_manual_remediation` and this fails naming the metadata keys the
    record does carry.
    """
    from app.models import AuditEvent, HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id, row_id, requested_id = await _awaiting_approval_row(
        admin_engine, fixture, tmp_path, now
    )
    requested_key = await _key_of(admin_engine, requested_id)

    substitute_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        job_type="patch.scan",
        autonomy="suggest",
        cooldown_s=0,
        max_per_device_per_day=10,
        max_per_org_per_hour=50,
    )
    substitute_key = await _key_of(admin_engine, substitute_id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        job_id, _envelope = await dispatch_manual_remediation(
            session,
            fixture.org_id,
            alert_id,
            action_key=substitute_key,
            actor_id=fixture.org.user_id,
            permissions=DISPATCH_GRANTS,
            job_engine=_engine(tmp_path),
            now=now + timedelta(minutes=1),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        row = (
            await session.execute(
                select(HealthAlertAction).where(HealthAlertAction.id == row_id)
            )
        ).scalar_one()
        dispatched = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == fixture.org_id,
                    AuditEvent.action == "health.remediation_dispatched",
                )
            )
        ).scalar_one()

    assert job.type == "patch.scan", "the action NAMED is the action that ran"
    assert row.state == "dispatched"
    assert row.approved_action_id == substitute_id
    assert dispatched.meta.get("action_key") == substitute_key
    assert dispatched.meta.get("superseded_action_key") == requested_key, (
        f"the discarded approval request for {requested_key!r} is unrecorded; the dispatch "
        f"record says {dispatched.meta!r}"
    )
    assert dispatched.meta.get("superseded_action_id") == str(requested_id)


async def test_a_console_dispatch_cannot_answer_an_expired_approval_request(
    admin_engine, tmp_path
) -> None:
    """The console path is not a back door around `approval_ttl_s`.

    `approve_action` expires the row and refuses once the window has closed. The
    console path landed on the SAME row and did not, so a request nobody
    answered in time was still answerable through `POST .../remediate` -- the
    approval TTL silently meaning "never", which is the exact failure §7.1's
    dial exists to prevent.

    Asserted as a JOB COUNT of zero and the row's stored state, not merely as a
    raised exception: an implementation that raised AFTER dispatching would pass
    an exception-only test.

    GUARD OBSERVABILITY: delete the `expires_at` check from
    `dispatch_manual_remediation`'s `awaiting_approval` branch and this reports
    a dispatched job against an expired request.
    """
    from app.models import HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id, row_id, _requested = await _awaiting_approval_row(
        admin_engine, fixture, tmp_path, now
    )
    substitute_key = await _key_of(
        admin_engine,
        await seed_approved_action(
            admin_engine,
            fixture.org_id,
            fixture.org.user_id,
            job_type="patch.scan",
            autonomy="suggest",
            cooldown_s=0,
            max_per_device_per_day=10,
            max_per_org_per_hour=50,
        ),
    )

    async with AsyncSession(admin_engine) as session:
        expires_at = (
            await session.execute(
                select(HealthAlertAction.expires_at).where(HealthAlertAction.id == row_id)
            )
        ).scalar_one()
    assert expires_at is not None, "the sweep stamped no approval window to expire"
    too_late = expires_at + timedelta(seconds=1)

    # Caught rather than `pytest.raises`d, so that the JOB COUNT is the
    # assertion that reports first: "it refused" and "it refused after
    # dispatching" are different outcomes and only one of them is a fix.
    refusal: str | None = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        try:
            await dispatch_manual_remediation(
                session,
                fixture.org_id,
                alert_id,
                action_key=substitute_key,
                actor_id=fixture.org.user_id,
                permissions=DISPATCH_GRANTS,
                job_engine=_engine(tmp_path),
                now=too_late,
            )
        except HealthValidationError as exc:
            refusal = str(exc)
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(HealthAlertAction.id == row_id)
            )
        ).scalar_one()
        jobs = await _count(session, Job, org_id=fixture.org_id)
    assert jobs == 0, (
        f"an expired approval request was answered with a dispatch: {jobs} job(s) at the "
        f"endpoint, row state {row.state!r}"
    )
    assert refusal is not None and "expired" in refusal
    assert row.state == "expired"
    assert "window closed" in (row.last_error or "")


# ---------------------------------------------------------------------------
# The row lock: two writers, one `health_alert_action` row
#
# `UNIQUE (alert_id, kind)` bounds the number of ROWS and cannot bound the
# number of WRITERS to one row, so neither test below can be replaced by an
# index. Both are deterministic interleaves rather than `asyncio.gather`, on
# `test_service_desk_numbering.py::test_concurrent_allocation_never_duplicates`'s
# pattern: the losing session's own `execute` is wrapped so that the instant its
# SELECT of `health_alert_action` returns -- the read half of a read-then-act
# bug -- a competing writer runs against the SAME row.
#
# THE HOOK IS IDENTICAL IN BOTH DIRECTIONS: it keys on the table being read, not
# on whether the statement carries `FOR UPDATE`, so it fires against the fixed
# code exactly as it fires against the broken code. What differs is only what
# the competing writer is able to do once it fires, and the observable is a JOB
# COUNT -- the thing an endpoint actually sees.
# ---------------------------------------------------------------------------


def _reads_the_action_row(statement: object) -> bool:
    from sqlalchemy import Select

    return isinstance(statement, Select) and "health_alert_action" in str(statement)


async def _awaiting_approval_row(admin_engine, fixture, tmp_path, now, **action_kw):
    """One alert with a `remediate` row parked in `awaiting_approval`.

    Returns `(alert_id, row_id, action_id)`. Driven through the real sweep
    rather than seeded in that state: `expires_at` is stamped by
    `_execute_remediate` from `approval_ttl_s`, and a row seeded straight into
    `awaiting_approval` would carry whatever the test felt like putting there.
    """
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        autonomy="require_approval",
        cooldown_s=0,
        max_per_device_per_day=10,
        max_per_org_per_hour=50,
        **action_kw,
    )
    row_id = await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert outcome.awaiting_approval == 1
    return alert_id, row_id, action_id


async def test_two_approvals_racing_on_one_alert_dispatch_exactly_one_job(
    admin_engine, tmp_path
) -> None:
    """Two holders of `health.remediate` click Approve on the same alert.

    Both requests read the SAME `health_alert_action` row, so `UNIQUE (alert_id,
    kind)` is structurally incapable of separating them: one row goes in and,
    without a row lock, TWO signed jobs go out at the endpoint. Reproduced
    exactly that way before the fix -- "jobs dispatched for one alert: 2 |
    action rows: 1 dispatched".

    The competing approval runs under `lock_timeout` rather than being left to
    block forever, because the fixed path BLOCKS by design (the loser is a human
    who must be told, not a row to be skipped). The timeout is what lets this
    test terminate in both directions; the job COUNT is what makes it falsify.

    GUARD OBSERVABILITY: drop `.with_for_update()` from `_load_action_row` and
    this reports 2 jobs against 1 row.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.models import HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id, row_id, _ = await _awaiting_approval_row(admin_engine, fixture, tmp_path, now)

    injected = False
    competitor: list[str] = []

    async with AsyncSession(admin_engine, expire_on_commit=False) as loser:
        original_execute = loser.execute

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal injected
            result = await original_execute(statement, *args, **kwargs)
            if not injected and _reads_the_action_row(statement):
                injected = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as rival:
                    await rival.execute(text("SET LOCAL lock_timeout = '1500ms'"))
                    try:
                        await approve_action(
                            rival,
                            fixture.org_id,
                            alert_id,
                            row_id,
                            actor_id=fixture.org.user_id,
                            permissions=DISPATCH_GRANTS,
                            job_engine=_engine(tmp_path),
                            now=now + timedelta(minutes=1),
                        )
                        await rival.commit()
                        competitor.append("dispatched")
                    except DBAPIError:
                        await rival.rollback()
                        competitor.append("locked out")
            return result

        loser.execute = interleaving_execute  # type: ignore[method-assign]
        await approve_action(
            loser,
            fixture.org_id,
            alert_id,
            row_id,
            actor_id=fixture.org.user_id,
            permissions=DISPATCH_GRANTS,
            job_engine=_engine(tmp_path),
            now=now + timedelta(minutes=1),
        )
        await loser.commit()

    assert injected, "the interleave never fired; this test proved nothing"
    async with AsyncSession(admin_engine) as session:
        jobs = list(
            (await session.execute(select(Job).where(Job.org_id == fixture.org_id)))
            .scalars()
            .all()
        )
        rows = list(
            (
                await session.execute(
                    select(HealthAlertAction).where(
                        HealthAlertAction.alert_id == alert_id,
                        HealthAlertAction.kind == "remediate",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(jobs) == 1, (
        f"two approvals of one alert dispatched {len(jobs)} jobs at the endpoint against "
        f"{len(rows)} action row(s) in state {[r.state for r in rows]}"
    )
    assert competitor == ["locked out"]
    assert [r.state for r in rows] == ["dispatched"]


async def test_a_console_dispatch_inside_the_sweeps_open_claim_adds_no_second_job(
    admin_engine, tmp_path
) -> None:
    """`POST /health/alerts/{id}/remediate` clicked while the sweep is mid-claim.

    The sweep claims with `FOR UPDATE SKIP LOCKED`; the console path must
    participate in that claim or the two of them dispatch the same remediation
    twice. Reproduced before the fix -- "jobs for one alert: 2".

    NO TIMEOUT IS NEEDED HERE, and that asymmetry is the proof: once the console
    holds the row, the sweep's `SKIP LOCKED` steps over it and returns
    `dispatched == 0`. A sweep that instead dispatches is a sweep the console
    never locked out.

    GUARD OBSERVABILITY: drop `.with_for_update()` from the row read in
    `dispatch_manual_remediation` and this reports 2 jobs, with the sweep
    claiming and dispatching inside the console's own read.
    """
    from app.models import ApprovedAction, HealthAlertAction, Job

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine,
        fixture.org_id,
        fixture.org.user_id,
        autonomy="autonomous",
        cooldown_s=0,
        max_per_device_per_day=10,
        max_per_org_per_hour=50,
    )
    await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )
    async with AsyncSession(admin_engine) as session:
        key = (
            await session.execute(
                select(ApprovedAction.key).where(ApprovedAction.id == action_id)
            )
        ).scalar_one()

    injected = False
    sweep_dispatched: list[int] = []

    async with AsyncSession(admin_engine, expire_on_commit=False) as console:
        original_execute = console.execute

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal injected
            result = await original_execute(statement, *args, **kwargs)
            if not injected and _reads_the_action_row(statement):
                injected = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as sweep:
                    outcome = await execute_pending_actions(
                        sweep, fixture.org_id, job_engine=_engine(tmp_path), now=now
                    )
                    await sweep.commit()
                    sweep_dispatched.append(outcome.dispatched)
            return result

        console.execute = interleaving_execute  # type: ignore[method-assign]
        await dispatch_manual_remediation(
            console,
            fixture.org_id,
            alert_id,
            action_key=key,
            actor_id=fixture.org.user_id,
            permissions=DISPATCH_GRANTS,
            job_engine=_engine(tmp_path),
            now=now,
        )
        await console.commit()

    assert injected, "the interleave never fired; this test proved nothing"
    async with AsyncSession(admin_engine) as session:
        jobs = list(
            (await session.execute(select(Job).where(Job.org_id == fixture.org_id)))
            .scalars()
            .all()
        )
        rows = list(
            (
                await session.execute(
                    select(HealthAlertAction).where(
                        HealthAlertAction.alert_id == alert_id,
                        HealthAlertAction.kind == "remediate",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(jobs) == 1, (
        f"a console click inside the sweep's claim dispatched {len(jobs)} jobs against "
        f"{len(rows)} action row(s); the sweep reported dispatched={sweep_dispatched}"
    )
    assert sweep_dispatched == [0], "the sweep did not skip the row the console had claimed"
    assert [r.state for r in rows] == ["dispatched"]


# ---------------------------------------------------------------------------
# Tenancy, on the ADMIN engine
# ---------------------------------------------------------------------------


async def test_one_orgs_action_sweep_does_not_execute_anothers(
    admin_engine, tmp_path
) -> None:
    """Invariant #5, where RLS is not the thing filtering.

    GUARD OBSERVABILITY: delete `HealthAlertAction.org_id == org_id` from the
    claim query and this observes org B's pending rows EXECUTED under org A's
    sweep -- i.e. a job created in org A targeting org B's device.
    """
    from app.models import HealthAlertAction, Job

    a, _ = await _armed_with_ticketing(admin_engine)
    b, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    await _open_one(admin_engine, a, now=now)
    b_alert = await _open_one(admin_engine, b, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, a.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert outcome.ticketed == 1

    async with AsyncSession(admin_engine) as session:
        b_states = sorted(
            (
                await session.execute(
                    select(HealthAlertAction.state).where(
                        HealthAlertAction.alert_id == b_alert
                    )
                )
            )
            .scalars()
            .all()
        )
        assert b_states == ["pending", "pending"], "org B's rows must be untouched"
        assert await _count(session, Job, org_id=b.org_id) == 0


async def test_another_orgs_action_row_is_a_404(admin_engine, tmp_path) -> None:
    a, _ = await _armed_with_ticketing(admin_engine)
    b, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    b_alert = await _open_one(admin_engine, b, now=now)
    row_id = await seed_action_row(admin_engine, b.org_id, b_alert, "remediate")

    async with AsyncSession(admin_engine) as session:
        with pytest.raises(HealthNotFoundError):
            await refuse_action(
                session, a.org_id, b_alert, row_id, actor_id=a.org.user_id
            )


async def test_auto_resolution_never_touches_another_orgs_ticket(
    admin_engine, tmp_path
) -> None:
    """`auto_resolve_alert_ticket` takes an `org_id` and an alert. The alert's
    `ticket_id` is a plain FK with no org check of its own, so without the
    explicit predicate a cross-org pairing would resolve another tenant's
    ticket. Pinned on the admin engine."""
    from app.models import HealthAlert, Ticket

    a, _ = await _armed_with_ticketing(admin_engine)
    b, _ = await _armed_with_ticketing(admin_engine)
    a_alert, a_ticket, now = await _alert_with_ticket(admin_engine, tmp_path, a)
    b_alert, b_ticket, _ = await _alert_with_ticket(admin_engine, tmp_path, b)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = (
            await session.execute(select(HealthAlert).where(HealthAlert.id == a_alert))
        ).scalar_one()
        alert.ticket_id = b_ticket
        alert.state = "cleared"
        alert.cleared_at = now
        alert.cleared_reason = "recovery"
        session.add(alert)
        await session.flush()
        from app.modules.health.settings import get_settings

        moved = await auto_resolve_alert_ticket(
            session, a.org_id, alert, now=now, settings=await get_settings(session, a.org_id)
        )
        await session.rollback()
    assert moved is False

    async with AsyncSession(admin_engine) as session:
        status = (
            await session.execute(select(Ticket.status).where(Ticket.id == b_ticket))
        ).scalar_one()
    assert status == "new", "org B's ticket must be untouched"


async def test_the_action_counts_are_returned_rather_than_only_logged(
    admin_engine, tmp_path
) -> None:
    """A sweep that returns nothing can only be asserted as "it did not raise",
    which every broken implementation also satisfies. The counts are the
    contract."""
    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        empty = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()
    assert (empty.dispatched, empty.ticketed, empty.notified, empty.blocked) == (0, 0, 0, 0)
    assert empty.envelopes == ()


# ---------------------------------------------------------------------------
# The org predicates that a UUID lookup cannot pin
#
# THE GENERAL SHAPE, because it caught four predicates in this module at once:
# a test that deletes `X.org_id == org_id` from a `WHERE org_id = :o AND id =
# :uuid` lookup and then asserts "org A got a 404" PROVES NOTHING. UUIDs never
# collide across tenants, so the surviving `id` predicate answers identically
# with or without the org clause, and every such mutation survives a green
# suite. Only two shapes actually pin one:
#
#   1. Key on a NON-UNIQUE identifier -- `ApprovedAction.key` is unique per org,
#      not globally, so two orgs can hold the same one.
#   2. Seed a GENUINE CROSS-TENANT REFERENCE -- an org A row whose foreign key
#      points at an org B row. Nothing in the schema forbids it, the tenancy
#      predicate is the only thing that refuses to follow it, and without the
#      predicate the query returns THE OTHER TENANT'S ROW rather than nothing.
#
# Every test below is one of those two. The observable is never "an exception
# was raised" alone -- it is the job type dispatched, or the script body
# returned, or the alert state of the tenant that was not asked about.
# ---------------------------------------------------------------------------


async def test_a_manual_remediation_cannot_reach_another_orgs_action_by_key(
    admin_engine, tmp_path
) -> None:
    """`dispatch_manual_remediation` resolves by `ApprovedAction.key`, which is
    the ONLY non-globally-unique identifier this module dispatches on -- and
    therefore the only place its org predicate is pinnable at all.

    Both halves of the failure are asserted, because they are different bugs:

    * A key ONLY THE OTHER TENANT HOLDS must be a 404 here. Without the org
      predicate the query returns org B's action and dispatches ITS job type,
      ITS params and ITS pinned script at org A's device -- so the falsifier is
      the JOB TYPE, not the exception. The two orgs' actions deliberately carry
      different job types; with the same type a leak and a clean dispatch look
      identical.
    * A key BOTH TENANTS HOLD must resolve to the caller's own. Without the
      predicate this is `MultipleResultsFound` out of `scalar_one_or_none` --
      an unhandled 500 on a route, from two tenants merely choosing the same
      word.

    GUARD OBSERVABILITY: delete `ApprovedAction.org_id == org_id` from the
    action lookup and BOTH halves fail.
    """
    from app.models import Job

    a, _ = await _armed_with_ticketing(admin_engine)
    b, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    a_alert = await _open_one(admin_engine, a, now=now)

    shared_key = f"restart-{uuid4().hex[:8]}"
    b_only_key = f"btheirs-{uuid4().hex[:8]}"
    await seed_approved_action(
        admin_engine,
        b.org_id,
        b.org.user_id,
        key=b_only_key,
        job_type="patch.apply",
        params_template={"patches": [], "reboot": "never"},
        autonomy="suggest",
        cooldown_s=0,
    )
    await seed_approved_action(
        admin_engine,
        b.org_id,
        b.org.user_id,
        key=shared_key,
        job_type="patch.apply",
        params_template={"patches": [], "reboot": "never"},
        autonomy="suggest",
        cooldown_s=0,
    )
    await seed_approved_action(
        admin_engine,
        a.org_id,
        a.org.user_id,
        key=shared_key,
        job_type="inventory.collect",
        autonomy="suggest",
        cooldown_s=0,
    )

    # Half one: a key only org B holds. Caught rather than `pytest.raises`d so
    # that the DISPATCHED JOB is what reports first -- "it raised" and "it
    # dispatched org B's action" are the two outcomes that must be told apart,
    # and only one of them is visible at an endpoint.
    refusal: Exception | None = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        try:
            await dispatch_manual_remediation(
                session,
                a.org_id,
                a_alert,
                action_key=b_only_key,
                actor_id=a.org.user_id,
                permissions=DISPATCH_GRANTS,
                job_engine=_engine(tmp_path),
                now=now,
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - the TYPE is asserted below
            refusal = exc
            await session.rollback()

    async with AsyncSession(admin_engine) as session:
        leaked = list(
            (await session.execute(select(Job).where(Job.org_id == a.org_id))).scalars().all()
        )
    assert leaked == [], (
        f"org B's action was dispatched at org A's device: {[j.type for j in leaked]}"
    )
    assert isinstance(refusal, HealthNotFoundError), refusal

    # Half two: a key BOTH orgs hold resolves to the caller's own.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        job_id, _envelope = await dispatch_manual_remediation(
            session,
            a.org_id,
            a_alert,
            action_key=shared_key,
            actor_id=a.org.user_id,
            permissions=DISPATCH_GRANTS,
            job_engine=_engine(tmp_path),
            now=now,
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert await _count(session, Job, org_id=b.org_id) == 0
    assert job.type == "inventory.collect", (
        f"a key both orgs hold resolved to the wrong tenant's action: {job.type}"
    )
    assert job.org_id == a.org_id
    assert job.target_id == a.device_id


async def test_the_sweep_will_not_follow_an_action_row_into_another_org(
    admin_engine, tmp_path
) -> None:
    """`_execute_remediate` resolves `approved_action_id` -- a globally unique
    UUID, so no key collision is available to pin its org predicate with. The
    pin is a GENUINE CROSS-TENANT REFERENCE instead: an org A `health_alert_action`
    row whose `approved_action_id` points at an org B action.

    Nothing in the schema forbids that row (the FK names `approved_actions.id`
    and says nothing about tenancy), so `ApprovedAction.org_id == org_id` is the
    only thing that refuses to follow it. Without the predicate the sweep
    resolves org B's action and dispatches ITS job type at org A's device --
    which is why the falsifier is the job type and the outcome key, not a 404.

    GUARD OBSERVABILITY: delete `ApprovedAction.org_id == org_id` from
    `_execute_remediate` and this reports a dispatched `patch.apply`.
    """
    from app.models import HealthAlertAction, Job

    a, _ = await _armed_with_ticketing(admin_engine)
    b, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    a_alert = await _open_one(admin_engine, a, now=now)

    b_action = await seed_approved_action(
        admin_engine,
        b.org_id,
        b.org.user_id,
        job_type="patch.apply",
        params_template={"patches": [], "reboot": "never"},
        autonomy="autonomous",
        cooldown_s=0,
    )
    row_id = await seed_action_row(
        admin_engine, a.org_id, a_alert, "remediate", approved_action_id=b_action
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, a.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(HealthAlertAction.id == row_id)
            )
        ).scalar_one()
        jobs = list(
            (await session.execute(select(Job).where(Job.org_id == a.org_id))).scalars().all()
        )
    assert [job.type for job in jobs] == [], (
        f"the sweep followed an org A row into org B's registry and dispatched "
        f"{[job.type for job in jobs]}"
    )
    assert (outcome.dispatched, outcome.skipped) == (0, 1)
    assert row.state == "skipped"
    assert "no longer resolves in this org" in (row.last_error or "")


async def test_script_run_params_will_not_read_another_orgs_script_body(
    admin_engine,
) -> None:
    """`_script_run_params` is called DIRECTLY, and that is the point.

    Its `Script.org_id` predicate is unreachable through every public path:
    `_refuse_reason` runs first on all three callers and checks the SAME
    `(org_id, script_id)` key, so a cross-tenant pin is refused there ("pins
    script X, which is gone") and the dispatcher never runs. The predicate is
    therefore defence in depth -- real, load-bearing if that ordering ever
    changes, and impossible to falsify through the API.

    Asserted at the function instead, on a genuine cross-tenant reference: an
    org A action pinning an org B script. The observable is org B's SCRIPT BODY
    -- arbitrary code, which §8.4 is emphatic about -- so a mutation that drops
    the predicate returns it here rather than raising.

    GUARD OBSERVABILITY: delete `Script.org_id == org_id` and this returns
    `{"body": "Remove-Item -Recurse ..."}` instead of raising.
    """
    from sqlalchemy.exc import NoResultFound

    from app.core.approved_actions import ApprovedAction
    from app.modules.health.actions import _script_run_params

    a, _ = await _armed_with_ticketing(admin_engine)
    b, _ = await _armed_with_ticketing(admin_engine)

    b_script = await seed_script(
        admin_engine,
        b.org_id,
        b.org.user_id,
        name="Theirs",
        body="Remove-Item -Recurse -Force C:\\ProgramData\\Theirs",
    )
    a_action_id = await seed_approved_action(
        admin_engine,
        a.org_id,
        a.org.user_id,
        job_type="script.run",
        script_id=b_script,
        script_version=1,
        destructive=True,
        risk_class="high",
        autonomy="require_approval",
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        action = (
            await session.execute(
                select(ApprovedAction).where(ApprovedAction.id == a_action_id)
            )
        ).scalar_one()
        leaked: dict | None = None
        try:
            leaked = await _script_run_params(session, a.org_id, action)
        except NoResultFound:
            pass
        await session.rollback()
    assert leaked is None, (
        f"another org's script body was built into a signed job's params: {leaked}"
    )


# ---------------------------------------------------------------------------
# `notify`'s `skipped` branch, which is the whole reason the function exists
# ---------------------------------------------------------------------------


async def test_notify_is_skipped_when_no_alert_raised_event_exists(
    admin_engine, tmp_path
) -> None:
    """§7.3: notify PUBLISHES NOTHING. It marks the row against the outbox event
    ingestion already wrote, so the only thing it actually decides is whether
    that event is there -- and until this landed, no test ever reached the
    branch where it is not. `_execute_notify` forced to `row.state = "done"`
    unconditionally survived the full suite.

    The missing event is constructed by DELETING the outbox row, which is the
    real shape of the failure this branch exists for: the alert committed and
    its `health.alert_raised` did not survive to the outbox, so marking the row
    `done` would record a notification nobody will ever be sent.

    GUARD OBSERVABILITY: force `row.state = "done"` in `_execute_notify` and
    this reports `notified=1` against an outbox with no event in it.
    """
    from sqlalchemy import delete

    from app.models import DomainEvent, HealthAlertAction

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        removed = await session.execute(
            delete(DomainEvent).where(
                DomainEvent.org_id == fixture.org_id,
                DomainEvent.subject_id == alert_id,
                DomainEvent.event_type == "health.alert_raised",
            )
        )
        await session.commit()
    assert removed.rowcount == 1, "ingestion wrote no health.alert_raised to remove"

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        outcome = await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlertAction).where(
                    HealthAlertAction.alert_id == alert_id,
                    HealthAlertAction.kind == "notify",
                )
            )
        ).scalar_one()
    assert outcome.notified == 0, (
        f"notify claimed to have notified against an empty outbox: {outcome}"
    )
    assert row.state == "skipped"
    assert "no health.alert_raised event exists" in (row.last_error or "")


async def test_a_remediation_is_skipped_for_a_device_waiting_for_review(
    admin_engine, tmp_path
) -> None:
    """Health is REPORTED for a pending machine (spec §1.5), so alerts open on
    it -- but remediation is not monitoring, and a machine-fired approved
    action must be passed over rather than stall the action sweep on the gate."""
    from sqlalchemy import update

    from app.models import Device, Job
    from app.modules.health.models import HealthAlertAction

    fixture, _ = await _armed_with_ticketing(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    action_id = await seed_approved_action(
        admin_engine, fixture.org_id, fixture.org.user_id, autonomy="autonomous",
        job_type="patch.scan",
    )
    row_id = await seed_action_row(
        admin_engine, fixture.org_id, alert_id, "remediate", approved_action_id=action_id
    )
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Device).where(Device.id == fixture.device_id).values(review_state="pending")
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await execute_pending_actions(
            session, fixture.org_id, job_engine=_engine(tmp_path), now=now
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        jobs = (
            (await session.execute(select(Job).where(Job.org_id == fixture.org_id)))
            .scalars()
            .all()
        )
        row = await session.get(HealthAlertAction, row_id)
    assert jobs == []
    assert row is not None and row.state == "skipped"
    assert "waiting for review" in (row.last_error or "")
