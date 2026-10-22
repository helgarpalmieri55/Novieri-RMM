"""§0.8's CLEAR predicate, clause by clause
(`docs/modules/endpoint-health-alerting/spec.md` §0.8, §0.7, §2.4).

SIX TESTS, ONE PER `cleared_reason`, PLUS THE CLAUSE DELIBERATELY NOT PRESENT.
The reasons are not interchangeable and the file is organised around that:
§0.8 says an alert cleared by `recovery` is a fixed problem while one cleared by
`unavailable_timeout` is a REMOVED SENSOR that must never count as a resolution
in an MTTR figure. A test that only asserted `state == "cleared"` would pass
against an implementation that stamped the same reason for all six, which is the
one bug this predicate cannot afford.

WHAT THESE FIXTURES HOLD CONSTANT: one device, one Windows OS family, one
`cpu.busy_pct` check per org, and `unavailable_clear_after_s` at its default
except in the two clause-3 tests, which vary the ALERT'S AGE on either side of
the boundary. Age is the only parameter clause 3 reads, so it is the only one
worth varying here; OS and check type are held constant on purpose because
varying them would test resolution, which has its own file.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.health.alerts import CLEARED_REASONS, clear_alert, clear_for_device_inactive
from app.modules.health.errors import HealthValidationError
from app.modules.health.ingest import ingest_report
from tests.health_fixtures import (
    seed_assignment,
    seed_check,
    seed_org,
    seed_profile,
    seed_settings,
)
from tests.test_health_ingest import _armed_org, _count, _device_row, _Fixture, _line, _msg

pytestmark = pytest.mark.usefixtures("admin_engine")


async def _open_one(admin_engine: Any, fixture: Any, *, now: datetime) -> UUID:
    """Open exactly one alert through the real ingest path and return its id.

    Through ingestion rather than by seeding a row: an alert seeded directly
    would not have a `health_flap_state` row or action rows, and three of the
    tests below depend on the clear interacting with those.
    """
    from app.models import HealthAlert

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session, fixture.org_id, device, _msg([_line(fixture.check_id)]), now=now
        )
        assert outcome.opened == 1
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        return (
            await session.execute(
                select(HealthAlert.id).where(
                    HealthAlert.org_id == fixture.org_id, HealthAlert.state != "cleared"
                )
            )
        ).scalar_one()


async def _state_of(admin_engine: Any, alert_id: UUID) -> tuple[str, str | None]:
    from app.models import HealthAlert

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlert.state, HealthAlert.cleared_reason).where(
                    HealthAlert.id == alert_id
                )
            )
        ).one()
    return row[0], row[1]


# ---------------------------------------------------------------------------
# Clause 1 -- recovery
# ---------------------------------------------------------------------------


async def test_clause_1_a_recovery_report_clears_with_reason_recovery(admin_engine) -> None:
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="recovery", state="ok", value="12.0")]),
            now=now + timedelta(minutes=2),
        )
        await session.commit()
    assert outcome.cleared == 1
    assert await _state_of(admin_engine, alert_id) == ("cleared", "recovery")


async def test_a_recovery_reporting_unavailable_clears_nothing(admin_engine) -> None:
    """§0.7: "an alert cleared because the check stopped being answerable is a
    silent loss of a real finding."

    The sensor going quiet is not the condition ending. Without this guard a
    `recovery` line is trusted purely on its `kind`, and an agent that reports
    `kind="recovery", state="unavailable"` when a service is uninstalled clears
    a real alert with `cleared_reason='recovery'` -- i.e. it launders a removed
    sensor into a fixed problem, which is exactly the attribution §0.8 makes the
    six reasons distinct to prevent.
    """
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="recovery", state="unavailable", value="")]),
            now=now + timedelta(minutes=2),
        )
        await session.commit()
    assert outcome.cleared == 0
    assert await _state_of(admin_engine, alert_id) == ("active", None)


# ---------------------------------------------------------------------------
# Clause 2 -- summary_ok, the repair path for a LOST recovery edge
# ---------------------------------------------------------------------------


async def test_clause_2_a_summary_reporting_ok_clears_with_reason_summary_ok(
    admin_engine,
) -> None:
    """§0.8: "this is the repair path for a lost recovery edge (a device that
    rebooted between breach and recovery, an at-least-once buffer that dropped
    the recovery message). WITHOUT THIS CLAUSE EVERY DROPPED RECOVERY IS A
    PERMANENT ALERT", which is failure mode (a).

    The distinguishing assertion is the REASON: a clear stamped `recovery` here
    would be indistinguishable in reporting from an edge that actually arrived.
    """
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        # NOTE: no `recovery` line ever arrives -- that is the point.
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="summary", state="ok", value="40.0")]),
            now=now + timedelta(minutes=30),
        )
        await session.commit()
    assert outcome.cleared == 1
    assert await _state_of(admin_engine, alert_id) == ("cleared", "summary_ok")


async def test_a_summary_still_reporting_breaching_clears_nothing(admin_engine) -> None:
    """The negative that gives clause 2 its meaning: a summary is a periodic
    restatement, not an edge, and the state it carries is what decides. Without
    this, `kind == "summary"` alone could be taken as "clear it" and every
    breaching fleet would go quiet at the summary interval."""
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="summary", state="breaching", value="98.0")]),
            now=now + timedelta(minutes=30),
        )
        await session.commit()
    assert outcome.cleared == 0
    assert await _state_of(admin_engine, alert_id) == ("active", None)


# ---------------------------------------------------------------------------
# Clause 3 -- unavailable_timeout, BOTH sides of the boundary
# ---------------------------------------------------------------------------


async def test_clause_3_an_unavailable_summary_does_not_clear_before_the_timeout(
    admin_engine,
) -> None:
    """The negative half, and it is the half that matters. §2.8 fixes the range
    at 1 hour to 30 days because "below an hour the removed-sensor clear races
    an agent that is merely rebooting and starts attributing REAL FINDINGS to
    `unavailable_timeout`, which §0.8 says must never be counted as a
    resolution"."""
    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, unavailable_clear_after_s=3600)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="summary", state="unavailable", value="")]),
            now=now + timedelta(seconds=3600),
        )
        await session.commit()
    assert outcome.cleared == 0, "at exactly the timeout the alert is still inside it"
    assert await _state_of(admin_engine, alert_id) == ("active", None)


async def test_clause_3_an_unavailable_summary_clears_after_the_timeout(admin_engine) -> None:
    """The positive half. §0.8: "the disk was removed, the service was
    uninstalled, the volume was unmounted. Without this clause, decommissioning
    a monitored thing leaves a permanent alert."

    `cleared_reason='unavailable_timeout'` and not `recovery`: §13 requires
    reporting to EXCLUDE this reason from any resolution count.
    """
    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, unavailable_clear_after_s=3600)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="summary", state="unavailable", value="")]),
            now=now + timedelta(seconds=3601),
        )
        await session.commit()
    assert outcome.cleared == 1
    assert await _state_of(admin_engine, alert_id) == ("cleared", "unavailable_timeout")


async def test_clause_3_measures_from_the_last_confirmed_breach_not_the_alert_age(
    admin_engine,
) -> None:
    """THE FIXTURE ABOVE OPENS THE ALERT AT `now`, WHICH IS WHY IT CANNOT SEE
    THIS. Both clause-3 tests hold `opened_at == last_seen_at`, so the two
    candidate clocks are indistinguishable in them; this one varies the alert's
    AGE independently of when it was last reconfirmed.

    A genuine breach re-observed every day for ten days, and then its FIRST EVER
    `unavailable` summary one minute later. Anchored to `opened_at` the alert is
    ten days old against a one-hour threshold and clears IMMEDIATELY -- and
    clears as `unavailable_timeout`, which §13 requires reporting to EXCLUDE
    from resolution counts, so a real ten-day finding disappears from the open
    list and from the resolved list at once. Anchored to `last_seen_at` -- the
    last time anything confirmed the condition -- the sensor has been quiet for
    one minute and the alert correctly stays open.

    `unavailable_clear_after_s` is set to its LOWER BOUND (§2.8's 1 hour) so the
    ten days cannot be mistaken for the threshold merely being generous.
    """
    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, unavailable_clear_after_s=3600)
    now = datetime.now(UTC)

    alert_id = await _open_one(admin_engine, fixture, now=now)
    for day in range(1, 11):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            device = await _device_row(session, fixture.device_id)
            await ingest_report(
                session,
                fixture.org_id,
                device,
                _msg([_line(fixture.check_id, value="98.2")]),
                now=now + timedelta(days=day),
            )
            await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="summary", state="unavailable")]),
            now=now + timedelta(days=10, minutes=1),
        )
        await session.commit()
    assert outcome.cleared == 0, (
        "a ten-day-old breach confirmed a minute ago has not been a removed "
        "sensor for an hour"
    )
    assert await _state_of(admin_engine, alert_id) == ("active", None)

    # And the clause still fires once the sensor really has been quiet that long.
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="summary", state="unavailable")]),
            now=now + timedelta(days=10, seconds=3601),
        )
        await session.commit()
    assert outcome.cleared == 1
    assert await _state_of(admin_engine, alert_id) == ("cleared", "unavailable_timeout")


async def test_the_sweep_half_of_clause_3_uses_the_same_clock(admin_engine) -> None:
    """`run_alert_sweep`'s `_clear_stale_unavailable` is the other half of the
    same clause -- for a device that stopped reporting entirely -- and if the two
    disagree the sweep simply undoes what ingestion just refused.

    Same shape as the test above, driven through the sweep: the alert is old,
    the confirmation is recent, and the CURRENT check state is `unavailable`.
    """
    from app.modules.health.sweep import run_alert_sweep

    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, unavailable_clear_after_s=3600)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        # Ten days on, the breach is reconfirmed and THEN the sensor goes quiet.
        await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, value="98.2")]),
            now=now + timedelta(days=10),
        )
        await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="summary", state="unavailable")]),
            now=now + timedelta(days=10, minutes=1),
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        swept = await run_alert_sweep(
            session, fixture.org_id, now=now + timedelta(days=10, minutes=2)
        )
        await session.commit()
    assert swept["cleared"] == 0
    assert await _state_of(admin_engine, alert_id) == ("active", None)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        swept = await run_alert_sweep(
            session, fixture.org_id, now=now + timedelta(days=10, seconds=3601)
        )
        await session.commit()
    assert swept["cleared"] == 1
    assert await _state_of(admin_engine, alert_id) == ("cleared", "unavailable_timeout")


# ---------------------------------------------------------------------------
# Clause 4 -- policy_removed (the profile-write path, already shipped in Task 5)
# ---------------------------------------------------------------------------


async def test_clause_4_retiring_a_check_clears_its_alerts_as_policy_removed(
    admin_engine,
) -> None:
    """§0.8 clause 4 / §2.4's amendment: removing a check from a profile is a
    RETIRE, and the retire path clears the check's open alerts first.

    The second assertion is the one §2.4 argues for at length: the alert row
    SURVIVES with a resolvable `check_id`. `ON DELETE RESTRICT` plus the retire
    is what makes §10's "deleting a check leaves existing alerts intact" true
    rather than aspirational -- a hard delete would have to take the history
    with it.
    """
    from app.models import HealthAlert, HealthCheck
    from app.modules.health.profiles import replace_checks

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await replace_checks(
            session, fixture.org_id, fixture.org.user_id, fixture.profile_id, []
        )
        await session.commit()

    assert await _state_of(admin_engine, alert_id) == ("cleared", "policy_removed")
    async with AsyncSession(admin_engine) as session:
        alert = (
            await session.execute(select(HealthAlert).where(HealthAlert.id == alert_id))
        ).scalar_one()
        still_there = (
            await session.execute(
                select(HealthCheck.retired_at).where(HealthCheck.id == alert.check_id)
            )
        ).scalar_one()
        assert still_there is not None, "the check row survives, retired"


# Clause 4's OTHER TWO LIMBS. §0.8 says "the check is removed from the profile,
# DISABLED, or THE DEVICE'S ASSIGNMENT CHANGES such that the check no longer
# applies". Only the first was implemented: `_retire_or_delete_checks` runs on
# the SET DIFFERENCE in `replace_checks`, so a check still present in the
# profile -- merely disabled, or merely no longer resolving to this device --
# never reached it. The alerts that produced then survived every subsequent
# report, because the check had left `agent_checks` and every line naming it was
# discarded before `kind` was read (see `ingest.py`'s docstring).

CPU_SPEC: dict[str, Any] = {
    "check_type": "cpu.busy_pct",
    "selector": "",
    "comparator": "gt",
    "threshold_num": 95,
    "for_ticks": 3,
    "severity": "warning",
    "os_family": None,
}


async def _report_for_days(admin_engine: Any, fixture: Any, start: datetime, days: int) -> None:
    """Drive every ingestion clear clause at the alert's own key, daily."""
    for day in range(days):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            device = await _device_row(session, fixture.device_id)
            await ingest_report(
                session,
                fixture.org_id,
                device,
                _msg(
                    [
                        _line(fixture.check_id, kind="recovery", state="ok", value="4"),
                        _line(fixture.check_id, kind="summary", state="ok", value="4"),
                        _line(fixture.check_id, kind="summary", state="unavailable"),
                    ]
                ),
                now=start + timedelta(days=day + 1),
            )
            await session.commit()


async def test_clause_4_disabling_a_check_clears_its_alerts_as_policy_removed(
    admin_engine,
) -> None:
    """The DISABLED limb. `enabled: false` keeps the row in the profile, so the
    set difference in `replace_checks` is empty and `_retire_or_delete_checks`
    is never called for it -- while `_check_applies` drops it from the resolved
    set immediately, which takes it out of `agent_checks` and out of every
    report that could have cleared its alert.

    THE SIXTY DAYS ARE THE POINT. Without them this reads as a test of one
    function call; with them it is the reproduction: sixty days of `recovery`,
    `summary`+`ok` and `summary`+`unavailable` lines for this exact key, all
    discarded, leaving `active None`. `for_ticks` and the threshold are written
    back unchanged, so `enabled` is the ONLY thing that differs from a no-op
    edit -- which the next test pins as genuinely a no-op.
    """
    from app.modules.health.profiles import replace_checks

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await replace_checks(
            session,
            fixture.org_id,
            fixture.org.user_id,
            fixture.profile_id,
            [{**CPU_SPEC, "enabled": False}],
        )
        await session.commit()

    await _report_for_days(admin_engine, fixture, now, 60)
    assert await _state_of(admin_engine, alert_id) == ("cleared", "policy_removed")


async def test_rewriting_a_check_unchanged_leaves_its_alert_alone(admin_engine) -> None:
    """The negative that stops the fix from becoming a worse bug than the one it
    repairs.

    `replace_checks` is a whole-set PUT (§10) and a console that GETs the set
    and PUTs it back unchanged must not clear the fleet's live alerts. The
    difference from the previous test is exactly one key, `enabled`.
    """
    from app.modules.health.profiles import replace_checks

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await replace_checks(
            session, fixture.org_id, fixture.org.user_id, fixture.profile_id, [CPU_SPEC]
        )
        await session.commit()

    assert await _state_of(admin_engine, alert_id) == ("active", None)


async def test_clause_4_reassigning_a_device_clears_the_old_profiles_alerts(
    admin_engine,
) -> None:
    """The ASSIGNMENT limb. A device-level assignment overrides the org default
    (§0.2, first match wins), so every check on the old profile stops applying
    to this device -- and, being on a profile the device no longer resolves to,
    can never be reported again.

    The assertion pairs the clear with a DISCARD COUNT from a real recovery
    line: `discarded == 1` is what proves the alert could not have cleared
    through ingestion, so `policy_removed` is the only reason available.
    """
    from app.modules.health.profiles import create_assignment

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    other = await seed_profile(admin_engine, fixture.org_id)
    await seed_check(
        admin_engine,
        fixture.org_id,
        other,
        check_type="memory.used_pct",
        comparator="gt",
        threshold_num=90,
    )
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await create_assignment(
            session,
            fixture.org_id,
            fixture.org.user_id,
            profile_id=other,
            device_id=fixture.device_id,
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="recovery", state="ok", value="4")]),
            now=now + timedelta(minutes=5),
        )
        await session.commit()
    assert (outcome.discarded, outcome.cleared) == (1, 0), (
        "the old profile's check is unreportable, which is why the clear cannot "
        "come from ingestion"
    )
    assert await _state_of(admin_engine, alert_id) == ("cleared", "policy_removed")


async def test_clause_4_unassigning_a_device_entirely_clears_its_alerts(
    admin_engine,
) -> None:
    """The same limb in the direction §5.2 calls a disarm: nothing resolves at
    all, so the applicable set is empty and every open alert on the device is
    for a rule the agent is about to be told to stop evaluating."""
    from app.models import HealthProfileAssignment
    from app.modules.health.profiles import delete_assignment

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assignment_id = (
            await session.execute(
                select(HealthProfileAssignment.id).where(
                    HealthProfileAssignment.org_id == fixture.org_id
                )
            )
        ).scalar_one()
        await delete_assignment(
            session, fixture.org_id, fixture.org.user_id, assignment_id
        )
        await session.commit()

    await _report_for_days(admin_engine, fixture, now, 7)
    assert await _state_of(admin_engine, alert_id) == ("cleared", "policy_removed")


async def test_clause_4_deactivating_a_profile_clears_its_alerts(admin_engine) -> None:
    """§3: "an INACTIVE profile is skipped, not treated as a match." Turning
    `active` off is therefore the assignment limb wearing a different switch,
    and it is the one an operator reaches for when they want a rule out of
    service without deleting anybody's configuration."""
    from app.modules.health.profiles import update_profile

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await update_profile(
            session,
            fixture.org_id,
            fixture.org.user_id,
            fixture.profile_id,
            {"active": False},
        )
        await session.commit()

    await _report_for_days(admin_engine, fixture, now, 7)
    assert await _state_of(admin_engine, alert_id) == ("cleared", "policy_removed")


# ---------------------------------------------------------------------------
# Clause 5 -- device_inactive
# ---------------------------------------------------------------------------


async def test_clause_5_a_device_leaving_active_status_clears_its_alerts(
    admin_engine,
) -> None:
    """§0.8 clause 5. `devices.status` is an ADMINISTRATIVE fact -- a
    decommissioned machine -- and is not the same as the device going quiet,
    which clears nothing (see the next test)."""
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cleared = await clear_for_device_inactive(
            session, fixture.org_id, fixture.device_id, now=now + timedelta(minutes=1)
        )
        await session.commit()
    assert cleared == 1
    assert await _state_of(admin_engine, alert_id) == ("cleared", "device_inactive")


# ---------------------------------------------------------------------------
# Clause 6 -- manual
# ---------------------------------------------------------------------------


async def test_clause_6_a_manual_close_stamps_manual_and_the_closing_user(
    admin_engine,
) -> None:
    """§0.8 clause 6. `cleared_by` is what separates a human closing an alert
    from any machine-driven clear: an alert somebody closed is not evidence that
    anything was fixed, and reporting has to be able to tell."""
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        from app.modules.health.alerts import get_alert

        alert = await get_alert(session, fixture.org_id, alert_id)
        did = await clear_alert(
            session,
            fixture.org_id,
            alert,
            reason="manual",
            now=now + timedelta(minutes=1),
            actor_id=fixture.org.user_id,
        )
        await session.commit()
    assert did is True

    async with AsyncSession(admin_engine) as session:
        alert = (
            await session.execute(select(HealthAlert).where(HealthAlert.id == alert_id))
        ).scalar_one()
    assert (alert.state, alert.cleared_reason) == ("cleared", "manual")
    assert alert.cleared_by == fixture.org.user_id


# ---------------------------------------------------------------------------
# The enum itself
# ---------------------------------------------------------------------------


async def test_an_unknown_cleared_reason_is_refused_before_postgres_sees_it(
    admin_engine,
) -> None:
    """`CLEARED_REASONS` is refused in the service AND by
    `ck_health_alert_cleared_reason`. The service check is what turns the
    plausible typo (`"recovered"` -- the word the kernel's own notification
    sample payload uses) into a typed 400 instead of a 500 from an
    `IntegrityError` naming a constraint."""
    from app.modules.health.alerts import get_alert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = await get_alert(session, fixture.org_id, alert_id)
        with pytest.raises(HealthValidationError):
            await clear_alert(session, fixture.org_id, alert, reason="recovered", now=now)
        await session.rollback()

    # Read the row back rather than trusting that the raise meant nothing
    # happened: a `pytest.raises` that prints only "DID NOT RAISE" on mutation
    # proves the exception, never the write.
    assert await _state_of(admin_engine, alert_id) == ("active", None)
    assert "device_offline" not in CLEARED_REASONS


async def test_clearing_an_already_cleared_alert_is_a_no_op_that_says_so(
    admin_engine,
) -> None:
    """Ingestion is at-least-once and every sweep may run twice. Re-stamping
    would move `cleared_at` on every redelivery and turn MTTR into a measure of
    how often the agent retried; the bool is what lets a caller count real
    work."""
    from app.models import HealthAlert
    from app.modules.health.alerts import get_alert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = await get_alert(session, fixture.org_id, alert_id)
        assert await clear_alert(session, fixture.org_id, alert, reason="recovery", now=now)
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        first_at = (
            await session.execute(
                select(HealthAlert.cleared_at).where(HealthAlert.id == alert_id)
            )
        ).scalar_one()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = await get_alert(session, fixture.org_id, alert_id)
        assert (
            await clear_alert(
                session,
                fixture.org_id,
                alert,
                reason="summary_ok",
                now=now + timedelta(hours=1),
            )
            is False
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlert.cleared_at, HealthAlert.cleared_reason).where(
                    HealthAlert.id == alert_id
                )
            )
        ).one()
    assert row[0] == first_at
    assert row[1] == "recovery"


async def test_clearing_a_muted_alert_drops_the_mute_stamp(admin_engine) -> None:
    """`ck_health_alert_muted_until_matches_state` is `(state='muted') =
    (muted_until IS NOT NULL)`. A clear that left the stamp is an
    `IntegrityError` at flush -- the constraint doing its job, and the reason
    the reset lives in `clear_alert` once instead of at each call site.

    §0.8 is explicit that acknowledging (and by the same argument, muting) does
    NOT prevent auto-clear: a muted alert whose condition recovers must still
    clear, or a mute becomes a way to make a fixed problem permanent.
    """
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = (
            await session.execute(select(HealthAlert).where(HealthAlert.id == alert_id))
        ).scalar_one()
        alert.state = "muted"
        alert.muted_until = now + timedelta(days=1)
        session.add(alert)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="recovery", state="ok")]),
            now=now + timedelta(minutes=5),
        )
        await session.commit()
    assert outcome.cleared == 1

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlert.state, HealthAlert.muted_until).where(
                    HealthAlert.id == alert_id
                )
            )
        ).one()
    assert row == ("cleared", None)


async def test_one_orgs_clear_does_not_touch_another_orgs_alerts(admin_engine) -> None:
    """Invariant #5 on the clear path, pinned on the ADMIN engine where RLS is
    not the thing doing the filtering. `clear_for_device_inactive` takes an
    `org_id` AND a `device_id`; without the `org_id` predicate the device id
    alone would still resolve, so the falsifier here is that org B's alert
    survives."""
    a = await _armed_org(admin_engine)
    b = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    a_alert = await _open_one(admin_engine, a, now=now)
    b_alert = await _open_one(admin_engine, b, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cleared = await clear_for_device_inactive(session, a.org_id, a.device_id, now=now)
        await session.commit()
    assert cleared == 1
    assert (await _state_of(admin_engine, a_alert))[0] == "cleared"
    assert (await _state_of(admin_engine, b_alert))[0] == "active"

    async with AsyncSession(admin_engine) as session:
        from app.models import HealthAlert

        assert await _count(session, HealthAlert, org_id=b.org_id) == 1
# ---------------------------------------------------------------------------
# THE CLAUSE DELIBERATELY NOT PRESENT
# ---------------------------------------------------------------------------


async def test_an_offline_device_does_not_clear_its_open_alerts(admin_engine) -> None:
    """§0.8: "an alert does NOT clear because the device went offline. Offline
    is not healthy."

    Without this test, the implementation that "recovers" everything at the
    instant a machine dies passes: a fleet crashing under memory pressure would
    read as the healthiest it has ever been. Two things are asserted together
    because either alone is satisfiable by a bug -- the existing alerts are
    STILL OPEN, and a THIRD alert (`agent.offline`) has opened alongside them.

    The offline sweep itself is Task 12's; this test drives it, so the exclusion
    is pinned against the real sweep rather than against a mock.
    """
    from app.models import Agent, HealthAlert
    from app.modules.health.sweep import run_offline_sweep

    org = await seed_org(admin_engine, "off")
    profile_id = await seed_profile(admin_engine, org.org_id)
    cpu_id = await seed_check(admin_engine, org.org_id, profile_id)
    disk_id = await seed_check(
        admin_engine,
        org.org_id,
        profile_id,
        check_type="disk.free_pct",
        selector="C:",
        comparator="lt",
        threshold_num=10,
    )
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
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, org.device_id)
        outcome = await ingest_report(
            session,
            org.org_id,
            device,
            _msg([_line(cpu_id), _line(disk_id, selector="C:", value="3.1")]),
            now=now,
        )
        assert outcome.opened == 2
        session.add(
            Agent(
                org_id=org.org_id,
                device_id=org.device_id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=now + timedelta(days=365),
                enrolled_at=now - timedelta(days=1),
                status="online",
                last_seen_at=now - timedelta(seconds=3600),
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        swept = await run_offline_sweep(session, org.org_id, now=now + timedelta(seconds=1))
        await session.commit()
    assert swept == {"opened": 1, "cleared": 0}

    async with AsyncSession(admin_engine) as session:
        rows = sorted(
            (check_type, state)
            for check_type, state in (
                await session.execute(
                    select(HealthAlert.check_type, HealthAlert.state).where(
                        HealthAlert.org_id == org.org_id
                    )
                )
            ).all()
        )
    assert rows == [
        ("agent.offline", "active"),
        ("cpu.busy_pct", "active"),
        ("disk.free_pct", "active"),
    ], "a dying machine must not read as three recoveries and one new problem"


# ---------------------------------------------------------------------------
# The clause that has to exist for the ONE server-evaluated check
# ---------------------------------------------------------------------------


async def _offline_org(admin_engine: Any, *, offline_after_s: int = 900) -> Any:
    """An org whose profile carries `agent.offline` and nothing else."""
    org = await seed_org(admin_engine, "offclr")
    profile_id = await seed_profile(admin_engine, org.org_id)
    check_id = await seed_check(
        admin_engine,
        org.org_id,
        profile_id,
        check_type="agent.offline",
        comparator="gt",
        threshold_num=0,
    )
    await seed_assignment(admin_engine, org.org_id, profile_id)
    await seed_settings(admin_engine, org.org_id, offline_after_s=offline_after_s)
    return _Fixture(org, profile_id, check_id)


async def _seed_offline_agent(admin_engine: Any, fixture: Any, *, last_seen_at: Any) -> None:
    from app.models import Agent

    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            Agent(
                org_id=fixture.org_id,
                device_id=fixture.device_id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=now + timedelta(days=365),
                enrolled_at=now - timedelta(days=1),
                status="online",
                last_seen_at=last_seen_at,
            )
        )
        await session.commit()


async def _only_alert_id(admin_engine: Any, org_id: UUID) -> UUID:
    from app.models import HealthAlert

    async with AsyncSession(admin_engine) as session:
        return (
            await session.execute(select(HealthAlert.id).where(HealthAlert.org_id == org_id))
        ).scalar_one()


async def _touch_agent(admin_engine: Any, device_id: UUID, when: datetime) -> None:
    from app.models import Agent

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
        agent.last_seen_at = when
        await session.commit()


async def test_an_agent_offline_alert_clears_when_the_device_answers_again(
    admin_engine,
) -> None:
    """§0.8's failure mode (a) for the ONE server-evaluated check.

    `agent.offline` is `evaluated_by="server"`, so `resolve_profile_for_device`
    keeps it out of `agent_checks` and `ingest_report` keys its per-line lookup
    off exactly that set -- every line naming this check is DISCARDED before
    `kind` is read. Clauses 1-3 are therefore structurally unreachable for it,
    and so is `run_alert_sweep`'s `unavailable_timeout` pass, which joins a
    `device_health_check_state` row ingestion never writes for a discarded line.
    That left `policy_removed`, `device_inactive` and a human clicking Close --
    i.e. a laptop asleep for fifteen minutes pinned an alert, its check, its
    profile and its ticket FOREVER, because `run_retention` deletes cleared
    alerts only.

    THE FALSIFIER IS THE FINAL BLOCK, not a counter. After the device returns,
    THIRTY DAYS of every periodic path this module has are driven -- the offline
    sweep, the alert sweep, and a batch carrying all three of ingestion's clear
    lines for this exact check -- and the per-day assertions pin that ingestion
    discarded all three and the alert sweep cleared nothing, so the only thing
    that can have cleared the alert is the offline sweep's own pass. Delete
    `_clear_recovered_offline` and this prints `active None` after thirty days.
    """
    from app.models import HealthAlert
    from app.modules.health.sweep import run_alert_sweep, run_offline_sweep

    fixture = await _offline_org(admin_engine)
    now = datetime.now(UTC)
    await _seed_offline_agent(
        admin_engine, fixture, last_seen_at=now - timedelta(hours=5)
    )

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        assert (await run_offline_sweep(session, fixture.org_id, now=now))["opened"] == 1
        await session.commit()
    alert_id = await _only_alert_id(admin_engine, fixture.org_id)
    assert await _state_of(admin_engine, alert_id) == ("active", None)

    # The device answers again, and then thirty days of every periodic path
    # this module has. The per-day assertions are what make the final one mean
    # something: ingestion discards all three of its clear lines because the
    # check is server-evaluated, and the alert sweep clears nothing because it
    # joins a `device_health_check_state` row that discard never wrote.
    for day in range(30):
        t = now + timedelta(days=day + 1)
        # A live device heartbeats; without this the sweep would legitimately
        # find it offline again and the test would prove nothing.
        await _touch_agent(admin_engine, fixture.device_id, t)
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            device = await _device_row(session, fixture.device_id)
            outcome = await ingest_report(
                session,
                fixture.org_id,
                device,
                _msg(
                    [
                        _line(fixture.check_id, kind="recovery", state="ok", value="0"),
                        _line(fixture.check_id, kind="summary", state="ok", value="0"),
                        _line(fixture.check_id, kind="summary", state="unavailable"),
                    ]
                ),
                now=t,
            )
            swept = await run_alert_sweep(session, fixture.org_id, now=t)
            offline = await run_offline_sweep(session, fixture.org_id, now=t)
            await session.commit()
        assert outcome.discarded == 3, "a server-evaluated check is never agent-reportable"
        assert outcome.cleared == 0
        assert swept["cleared"] == 0, "the alert sweep cannot see a check with no state row"
        assert offline["opened"] == 0

    async with AsyncSession(admin_engine) as session:
        alert = (
            await session.execute(select(HealthAlert).where(HealthAlert.id == alert_id))
        ).scalar_one()
    assert (alert.state, alert.cleared_reason) == ("cleared", "recovery"), (
        f"an offline alert must clear when the device comes back; got "
        f"{alert.state} {alert.cleared_reason} after {alert.occurrence_count} occurrences"
    )


async def test_the_offline_clear_is_attributed_recovery_and_not_unavailable_timeout(
    admin_engine,
) -> None:
    """§13 requires reporting to EXCLUDE `unavailable_timeout` from any
    resolution count, because that reason means a removed sensor.

    A machine that went quiet and came back is a REAL outage that really ended,
    so attributing it to `unavailable_timeout` would delete every agent outage
    from the MTTR figure while looking like a fix. The sweep's return value is
    asserted alongside the reason so a clear that happened but was attributed
    wrongly is distinguishable from one that never happened.
    """
    from app.modules.health.sweep import run_offline_sweep

    fixture = await _offline_org(admin_engine, offline_after_s=300)
    now = datetime.now(UTC)
    await _seed_offline_agent(admin_engine, fixture, last_seen_at=now - timedelta(hours=9))

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await run_offline_sweep(session, fixture.org_id, now=now)
        await session.commit()
    alert_id = await _only_alert_id(admin_engine, fixture.org_id)

    await _touch_agent(admin_engine, fixture.device_id, now + timedelta(days=9))
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        swept = await run_offline_sweep(
            session, fixture.org_id, now=now + timedelta(days=9, seconds=30)
        )
        await session.commit()
    assert swept == {"opened": 0, "cleared": 1}
    assert await _state_of(admin_engine, alert_id) == ("cleared", "recovery")


async def test_a_device_still_quiet_keeps_its_offline_alert_open(admin_engine) -> None:
    """The negative half, and it is the one that stops the fix from becoming the
    bug §0.8 forbids: a clear pass with no `last_seen_at` predicate would clear
    every offline alert in the fleet on its next tick, which is "offline is
    healthy" arriving through the repair.

    `last_seen_at` is moved to a point still OUTSIDE `offline_after_s`, so the
    device has reported -- just not recently enough. An implementation that
    tested `IS NOT NULL` alone passes the positive test above and fails here.
    """
    from app.modules.health.sweep import run_offline_sweep

    fixture = await _offline_org(admin_engine, offline_after_s=900)
    now = datetime.now(UTC)
    await _seed_offline_agent(admin_engine, fixture, last_seen_at=now - timedelta(hours=5))

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await run_offline_sweep(session, fixture.org_id, now=now)
        await session.commit()
    alert_id = await _only_alert_id(admin_engine, fixture.org_id)

    await _touch_agent(admin_engine, fixture.device_id, now - timedelta(seconds=901))
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        swept = await run_offline_sweep(session, fixture.org_id, now=now)
        await session.commit()
    assert swept == {"opened": 0, "cleared": 0}
    assert await _state_of(admin_engine, alert_id) == ("active", None)


