"""Alert state transitions: acknowledge, mute, manual close, `previous_alert_id`
(`docs/modules/endpoint-health-alerting/spec.md` §0.8, §2.4, §11.3).

ACKNOWLEDGE IS DEFINED BY WHAT IT DOES NOT DO, so most of this file is
negatives. §0.8: a named user taking ownership "does NOT stop the check, does
NOT prevent auto-clear, and does NOT cancel a remediation already in flight".
Each of those three is a separate test here, because an implementation that got
one wrong would pass a test that only asserted `state == "acknowledged"` -- and
the wrong one (suppressing auto-clear) turns every fixed problem into an owned
one forever, which is §0.8's failure mode (a) arriving through the front door.

WHAT THESE FIXTURES HOLD CONSTANT: one org, one Windows device, one
`cpu.busy_pct` check, and an alert opened through the real ingest path at a
`now` the test controls. The parameters that VARY are the ones each transition
reads -- the starting state (every legal and illegal one is enumerated), the
mute horizon on both sides of the 30-day bound, and the gap between a clear and
a re-open on both sides of `flap_window_s`. Anything a transition does not read
is held constant deliberately, so a failure here names the transition rather
than the fixture.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.health.alerts import (
    MAX_MUTE_DAYS,
    acknowledge,
    close,
    get_alert,
    link_previous_alert,
    list_alerts,
    mute,
    summarise_open_alerts,
)
from app.modules.health.errors import HealthNotFoundError, HealthValidationError
from app.modules.health.ingest import ingest_report
from tests.health_fixtures import seed_settings
from tests.test_health_clear_predicate import _open_one, _state_of
from tests.test_health_ingest import _armed_org, _device_row, _line, _msg

pytestmark = pytest.mark.usefixtures("admin_engine")


# ---------------------------------------------------------------------------
# Acknowledge -- and its three negatives
# ---------------------------------------------------------------------------


async def test_acknowledge_moves_active_to_acknowledged_and_names_the_user(
    admin_engine,
) -> None:
    from app.models import AuditEvent, HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = await acknowledge(
            session,
            fixture.org_id,
            alert_id,
            actor_id=fixture.org.user_id,
            now=now + timedelta(minutes=1),
        )
        assert alert.state == "acknowledged"
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(select(HealthAlert).where(HealthAlert.id == alert_id))
        ).scalar_one()
        assert stored.state == "acknowledged"
        assert stored.acknowledged_by == fixture.org.user_id
        assert stored.acknowledged_at is not None
        actions = list(
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
        assert "health.alert_acknowledged" in actions, "invariant #4: privileged, audited"


async def test_acknowledge_does_not_stop_the_check(admin_engine) -> None:
    """§0.8's first negative. The check keeps running: its row stays `enabled`
    and un-retired, and the very next report still updates
    `device_health_check_state`. An acknowledge that disarmed the check would
    make "I am looking at this" mean "stop telling me anything about it"."""
    from app.models import DeviceHealthCheckState, HealthCheck

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await acknowledge(session, fixture.org_id, alert_id, actor_id=fixture.org.user_id)
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, value="99.9")]),
            now=now + timedelta(minutes=5),
        )
        await session.commit()
    assert outcome.applied == 1

    async with AsyncSession(admin_engine) as session:
        check = (
            await session.execute(
                select(HealthCheck).where(HealthCheck.id == fixture.check_id)
            )
        ).scalar_one()
        assert check.enabled is True
        assert check.retired_at is None
        state = (
            await session.execute(
                select(DeviceHealthCheckState).where(
                    DeviceHealthCheckState.org_id == fixture.org_id
                )
            )
        ).scalar_one()
        assert state.last_value == "99.9", "the check kept reporting"


async def test_acknowledge_does_not_prevent_auto_clear(admin_engine) -> None:
    """§0.8's second negative, and the most dangerous one to get wrong.

    GUARD OBSERVABILITY: if acknowledge were allowed to suppress auto-clear,
    this observes `state='acknowledged'` where it expects `'cleared'` -- i.e. a
    fixed problem showing as an owned one forever.
    """
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await acknowledge(session, fixture.org_id, alert_id, actor_id=fixture.org.user_id)
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
    assert await _state_of(admin_engine, alert_id) == ("cleared", "recovery")


async def test_acknowledge_does_not_cancel_a_remediation_already_in_flight(
    admin_engine,
) -> None:
    """§0.8's third negative. The job is on the wire; the server cannot recall
    it and must not pretend it did. A design that marked the action row
    `skipped` on acknowledge would leave the console claiming nothing ran while
    the device was already running it."""
    from app.models import HealthAlertAction

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            HealthAlertAction(
                org_id=fixture.org_id,
                alert_id=alert_id,
                kind="remediate",
                state="dispatched",
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await acknowledge(session, fixture.org_id, alert_id, actor_id=fixture.org.user_id)
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
    assert state == "dispatched"


@pytest.mark.parametrize("start_state", ["acknowledged", "muted", "cleared"])
async def test_acknowledge_refuses_every_state_but_active(
    admin_engine, start_state: str
) -> None:
    """Every illegal edge, enumerated. A test that only tried `cleared ->
    acknowledged` would pass against an implementation that allowed
    `muted -> acknowledged`, which silently un-mutes an alert somebody
    deliberately silenced.

    THE STORED ROW IS ASSERTED FIRST, AND THE RAISE SECOND. `pytest.raises`
    around the call would fail with "DID NOT RAISE" on mutation, which proves
    the exception and never the write -- and this project has already shipped
    one guard that printed exactly that and nothing else. So the call is made
    inside a `try`, the row is read back, and the row assertion is what fails
    when the guard goes: `acknowledged_by` is populated on an alert that was not
    `active`.
    """
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = (
            await session.execute(select(HealthAlert).where(HealthAlert.id == alert_id))
        ).scalar_one()
        alert.state = start_state
        if start_state == "muted":
            alert.muted_until = now + timedelta(days=1)
        if start_state == "cleared":
            alert.cleared_at = now
            alert.cleared_reason = "recovery"
        session.add(alert)
        await session.commit()

    raised: Exception | None = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        try:
            await acknowledge(
                session, fixture.org_id, alert_id, actor_id=fixture.org.user_id
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - the type is asserted below
            raised = exc
            await session.rollback()

    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(
                select(HealthAlert.state, HealthAlert.acknowledged_by).where(
                    HealthAlert.id == alert_id
                )
            )
        ).one()
    assert stored == (start_state, None), (
        f"acknowledging a {start_state!r} alert wrote to the row; it must change nothing"
    )
    assert isinstance(raised, HealthValidationError), (
        f"expected a typed HealthValidationError, got {raised!r}"
    )


# ---------------------------------------------------------------------------
# Mute -- bounded at 30 days by the service AND by the database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("start_state", ["active", "acknowledged"])
async def test_mute_is_legal_from_active_and_from_acknowledged(
    admin_engine, start_state: str
) -> None:
    from app.models import AuditEvent, HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)
    if start_state == "acknowledged":
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            await acknowledge(
                session, fixture.org_id, alert_id, actor_id=fixture.org.user_id
            )
            await session.commit()

    horizon = now + timedelta(days=7)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await mute(
            session,
            fixture.org_id,
            alert_id,
            actor_id=fixture.org.user_id,
            muted_until=horizon,
            now=now,
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(select(HealthAlert).where(HealthAlert.id == alert_id))
        ).scalar_one()
        assert stored.state == "muted"
        assert stored.muted_by == fixture.org.user_id
        assert stored.muted_until is not None
        actions = list(
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.target_id == str(alert_id))
                )
            )
            .scalars()
            .all()
        )
        assert "health.alert_muted" in actions


async def test_a_mute_of_thirty_days_is_accepted_and_thirty_one_is_refused(
    admin_engine,
) -> None:
    """BOTH SIDES OF THE BOUND. §0.8 fixes it at 30 days because "unbounded
    mutes are how a fleet accumulates permanently silent alarms that everyone
    believes are still armed". A test with only the 31-day case passes against
    a bound of one day, which would be equally wrong in the other direction."""
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        alert = await get_alert(session, fixture.org_id, alert_id)
        opened_at = alert.opened_at
        with pytest.raises(HealthValidationError):
            await mute(
                session,
                fixture.org_id,
                alert_id,
                actor_id=fixture.org.user_id,
                muted_until=opened_at + timedelta(days=MAX_MUTE_DAYS + 1),
                now=now,
            )
        await session.rollback()

    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(
                select(HealthAlert.state, HealthAlert.muted_until).where(
                    HealthAlert.id == alert_id
                )
            )
        ).one()
    assert stored == ("active", None), "the refused mute must not have been stored"

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await mute(
            session,
            fixture.org_id,
            alert_id,
            actor_id=fixture.org.user_id,
            muted_until=opened_at + timedelta(days=MAX_MUTE_DAYS),
            now=now,
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(
                select(HealthAlert.state).where(HealthAlert.id == alert_id)
            )
        ).scalar_one() == "muted"


async def test_the_thirty_day_mute_bound_is_enforced_by_postgres_not_only_by_the_service(
    admin_engine,
) -> None:
    """`ck_health_alert_mute_bounded`, on the ADMIN engine with raw SQL: no
    Pydantic model, no service call, nothing between the statement and the
    database. The service check is the message; the CHECK is the fact, and it is
    what survives a second caller, a worker and a direct SQL fixup.

    GUARD OBSERVABILITY: without the constraint this observes a STORED row with
    a 31-day mute rather than an `IntegrityError`, which is why the positive
    control below runs in the same test.
    """
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine) as session:
        opened_at = (
            await session.execute(
                select(HealthAlert.opened_at).where(HealthAlert.id == alert_id)
            )
        ).scalar_one()

    async with AsyncSession(admin_engine) as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "UPDATE health_alert SET state = 'muted', muted_until = :until "
                    "WHERE id = :id"
                ),
                {"until": opened_at + timedelta(days=31), "id": alert_id},
            )
        await session.rollback()

    # Positive control: the identical statement one day inside the bound
    # succeeds, so the rejection above is the BOUND and not the statement.
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "UPDATE health_alert SET state = 'muted', muted_until = :until WHERE id = :id"
            ),
            {"until": opened_at + timedelta(days=29), "id": alert_id},
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        assert (
            await session.execute(
                select(HealthAlert.state).where(HealthAlert.id == alert_id)
            )
        ).scalar_one() == "muted"


async def test_a_mute_in_the_past_is_refused(admin_engine) -> None:
    """A mute that has already expired is not a mute; it is an alert that
    silently returns to `active` on the next sweep pass having notified nobody
    that it was ever silenced."""
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError):
            await mute(
                session,
                fixture.org_id,
                alert_id,
                actor_id=fixture.org.user_id,
                muted_until=now - timedelta(hours=1),
                now=now,
            )
        await session.rollback()
    assert await _state_of(admin_engine, alert_id) == ("active", None)


# ---------------------------------------------------------------------------
# Manual close
# ---------------------------------------------------------------------------


async def test_manual_close_clears_with_reason_manual_and_names_the_closer(
    admin_engine,
) -> None:
    from app.models import AuditEvent, HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await close(
            session,
            fixture.org_id,
            alert_id,
            actor_id=fixture.org.user_id,
            now=now + timedelta(minutes=3),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(select(HealthAlert).where(HealthAlert.id == alert_id))
        ).scalar_one()
        assert (stored.state, stored.cleared_reason) == ("cleared", "manual")
        assert stored.cleared_by == fixture.org.user_id
        actions = list(
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.target_id == str(alert_id))
                )
            )
            .scalars()
            .all()
        )
        assert "health.alert_closed" in actions


async def test_closing_an_already_cleared_alert_is_refused(admin_engine) -> None:
    """Distinct from `clear_alert`'s silent no-op: a MACHINE re-clearing is an
    at-least-once redelivery and must be idempotent, while a HUMAN closing an
    alert that is already closed is acting on a stale console and deserves to be
    told."""
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await close(session, fixture.org_id, alert_id, actor_id=fixture.org.user_id, now=now)
        await session.commit()
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(HealthValidationError):
            await close(
                session, fixture.org_id, alert_id, actor_id=fixture.org.user_id, now=now
            )
        await session.rollback()
    assert await _state_of(admin_engine, alert_id) == ("cleared", "manual")


async def test_a_muted_alert_can_be_closed_by_a_human(admin_engine) -> None:
    """`muted -> cleared` is legal and is where
    `ck_health_alert_muted_until_matches_state` bites: the close must drop the
    stamp in the same statement or the flush fails."""
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        await mute(
            session,
            fixture.org_id,
            alert_id,
            actor_id=fixture.org.user_id,
            muted_until=now + timedelta(days=1),
            now=now,
        )
        await close(
            session,
            fixture.org_id,
            alert_id,
            actor_id=fixture.org.user_id,
            now=now + timedelta(minutes=1),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlert.state, HealthAlert.muted_until).where(
                    HealthAlert.id == alert_id
                )
            )
        ).one()
    assert row == ("cleared", None)


# ---------------------------------------------------------------------------
# `previous_alert_id`, directly
# ---------------------------------------------------------------------------


async def test_link_previous_alert_finds_the_most_recent_clear_inside_the_window(
    admin_engine,
) -> None:
    """Two cleared alerts for one key: the link must be the LATER one. A test
    with a single predecessor cannot see an `ORDER BY cleared_at` that runs the
    wrong way, and naming the wrong predecessor on a ticket is worse than naming
    none -- it asserts a continuity that is not there."""
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)

    ids = []
    for cycle in range(2):
        alert_id = await _open_one(
            admin_engine, fixture, now=now + timedelta(seconds=cycle * 60)
        )
        ids.append(alert_id)
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            device = await _device_row(session, fixture.device_id)
            await ingest_report(
                session,
                fixture.org_id,
                device,
                _msg([_line(fixture.check_id, kind="recovery", state="ok")]),
                now=now + timedelta(seconds=cycle * 60 + 10),
            )
            await session.commit()

    async with AsyncSession(admin_engine) as session:
        linked = await link_previous_alert(
            session,
            fixture.org_id,
            device_id=fixture.device_id,
            check_id=fixture.check_id,
            selector="",
            now=now + timedelta(seconds=120),
            window_s=3600,
        )
    assert linked == ids[1]


async def test_link_previous_alert_returns_none_outside_the_window(admin_engine) -> None:
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    await _open_one(admin_engine, fixture, now=now)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="recovery", state="ok")]),
            now=now + timedelta(seconds=10),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        linked = await link_previous_alert(
            session,
            fixture.org_id,
            device_id=fixture.device_id,
            check_id=fixture.check_id,
            selector="",
            now=now + timedelta(seconds=4000),
            window_s=3600,
        )
    assert linked is None


# ---------------------------------------------------------------------------
# Tenancy: 404, never 403 -- pinned on the ADMIN engine
# ---------------------------------------------------------------------------


async def test_another_orgs_alert_is_a_404_not_a_403(admin_engine) -> None:
    """`errors.py` states the reason: a 403 confirms the id exists, which is a
    cross-tenant existence oracle over a table keyed by device.

    ON THE ADMIN ENGINE, where RLS is not the thing doing the rejecting. Driven
    through `org_session` this passes with the `org_id` predicate deleted,
    because RLS refuses first -- the defect that shipped four times in the SLA
    sub-project.

    GUARD OBSERVABILITY, AND IT IS A VALUE RATHER THAN A RAISE. Deleting
    `HealthAlert.org_id == org_id` from `get_alert` makes it RETURN org B's
    alert to org A, so the assertion below is on the `org_id` of what came back
    -- `pytest.raises` alone would print "DID NOT RAISE" and tell a reader
    nothing about which tenant's row leaked.
    """
    a = await _armed_org(admin_engine)
    b = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    await _open_one(admin_engine, a, now=now)
    b_alert = await _open_one(admin_engine, b, now=now)

    leaked = None
    raised: Exception | None = None
    async with AsyncSession(admin_engine) as session:
        try:
            leaked = await get_alert(session, a.org_id, b_alert)
        except Exception as exc:  # noqa: BLE001 - the type is asserted below
            raised = exc
        rows = await list_alerts(session, a.org_id)

    assert leaked is None, (
        f"get_alert returned an alert owned by org {getattr(leaked, 'org_id', None)} to "
        f"org {a.org_id}"
    )
    assert [r.org_id for r in rows] == [a.org_id]
    assert isinstance(raised, HealthNotFoundError), (
        f"another org's alert must be a 404, not a 403 and not a leak; got {raised!r}"
    )


async def test_acknowledging_another_orgs_alert_is_a_404_and_writes_nothing(
    admin_engine,
) -> None:
    from app.models import HealthAlert

    a = await _armed_org(admin_engine)
    b = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    b_alert = await _open_one(admin_engine, b, now=now)

    raised: Exception | None = None
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        try:
            await acknowledge(session, a.org_id, b_alert, actor_id=a.org.user_id)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - the type is asserted below
            raised = exc
            await session.rollback()

    async with AsyncSession(admin_engine) as session:
        stored = (
            await session.execute(
                select(HealthAlert.state, HealthAlert.acknowledged_by).where(
                    HealthAlert.id == b_alert
                )
            )
        ).one()
    # The row FIRST: without the predicate this reads org B's alert stamped with
    # org A's user, which is a cross-tenant write and not merely a missing 404.
    assert stored == ("active", None), "org A acknowledged org B's alert"
    assert isinstance(raised, HealthNotFoundError), f"expected a 404; got {raised!r}"


async def test_an_unknown_alert_id_is_a_404(admin_engine) -> None:
    fixture = await _armed_org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        with pytest.raises(HealthNotFoundError):
            await get_alert(session, fixture.org_id, uuid4())


async def test_list_alerts_filters_do_not_leak_across_devices(admin_engine) -> None:
    """The console filters (§13). A filter that ignored `device_id` would show
    a technician another machine's alerts on a device page, which is the one
    view where "these are this machine's problems" has to be exactly true."""
    from tests.health_fixtures import seed_device

    fixture = await _armed_org(admin_engine)
    other = await seed_device(admin_engine, fixture.org_id)
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for device_id in (fixture.device_id, other):
            device = await _device_row(session, device_id)
            await ingest_report(
                session, fixture.org_id, device, _msg([_line(fixture.check_id)]), now=now
            )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        rows = await list_alerts(session, fixture.org_id, device_id=other)
    assert [r.device_id for r in rows] == [other]


async def test_settings_are_read_per_org_when_linking_a_predecessor(admin_engine) -> None:
    """Two orgs with different `flap_window_s`: the same 400-second gap links a
    predecessor in the org whose window is an hour and links nothing in the org
    whose window is five minutes.

    A single-org fixture cannot see a hardcoded window -- this is the same trap
    the SLA engine hit, where every "met" fixture was stamped before its
    deadline and made "met" and "met on time" observationally identical.
    """
    from app.models import HealthAlert

    wide = await _armed_org(admin_engine)
    narrow = await _armed_org(admin_engine)
    await seed_settings(admin_engine, wide.org_id, flap_window_s=3600)
    await seed_settings(admin_engine, narrow.org_id, flap_window_s=300)

    now = datetime.now(UTC)
    results = {}
    for fixture in (wide, narrow):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            device = await _device_row(session, fixture.device_id)
            await ingest_report(
                session, fixture.org_id, device, _msg([_line(fixture.check_id)]), now=now
            )
            await ingest_report(
                session,
                fixture.org_id,
                device,
                _msg([_line(fixture.check_id, kind="recovery", state="ok")]),
                now=now + timedelta(seconds=10),
            )
            await ingest_report(
                session,
                fixture.org_id,
                device,
                _msg([_line(fixture.check_id)]),
                now=now + timedelta(seconds=400),
            )
            await session.commit()
        async with AsyncSession(admin_engine) as session:
            reopened = (
                await session.execute(
                    select(HealthAlert.previous_alert_id).where(
                        HealthAlert.org_id == fixture.org_id, HealthAlert.state != "cleared"
                    )
                )
            ).scalar_one()
        results[fixture.org_id] = reopened

    assert results[wide.org_id] is not None
    assert results[narrow.org_id] is None


# ---------------------------------------------------------------------------
# Two predicates the ordinary tenancy style cannot pin
#
# A cross-org test that passes the CALLER'S OWN device id proves nothing about
# an `org_id` predicate: the device id alone already excludes the other tenant,
# because device ids never collide. The mutation survives. Both tests below
# therefore key on the OTHER tenant's identifier, which is the only input that
# makes the org clause the thing doing the work.
# ---------------------------------------------------------------------------


async def test_clearing_for_an_inactive_device_will_not_follow_another_orgs_device_id(
    admin_engine,
) -> None:
    """`clear_for_device_inactive` filters on `(org_id, device_id)`, and its
    existing cross-org test passes org A's OWN device id -- so dropping
    `HealthAlert.org_id == org_id` still clears nothing of org B's and the
    mutation survives (`tests/test_health_clear_predicate.py::
    test_one_orgs_clear_does_not_touch_another_orgs_alerts` says otherwise in
    its docstring; it is wrong about which clause it is pinning).

    This passes org A's `org_id` with org B's `device_id` -- the shape a stale
    id, a copy-pasted console link or a confused-deputy caller actually
    produces. With the predicate nothing resolves and nothing clears; without
    it, org B's open alert is cleared by an org A caller.

    GUARD OBSERVABILITY: delete `HealthAlert.org_id == org_id` from the query
    and this reports org B's alert `cleared` under org A's org_id.
    """
    from app.modules.health.alerts import clear_for_device_inactive

    a = await _armed_org(admin_engine)
    b = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    a_alert = await _open_one(admin_engine, a, now=now)
    b_alert = await _open_one(admin_engine, b, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cleared = await clear_for_device_inactive(session, a.org_id, b.device_id, now=now)
        await session.commit()

    b_state = (await _state_of(admin_engine, b_alert))[0]
    assert b_state == "active", (
        f"an org A caller cleared org B's alert by naming org B's device: state {b_state!r}"
    )
    assert cleared == 0
    assert (await _state_of(admin_engine, a_alert))[0] == "active"


async def test_publishing_an_alert_event_for_a_missing_check_reports_that_it_published_nothing(
    admin_engine,
) -> None:
    """`publish_alert_event` returns WHETHER IT PUBLISHED, and `check is None`
    is the branch that returns `False` without touching the outbox.

    That is not a hypothetical input: `clear_alert` resolves the check itself
    and passes whatever it finds, so an alert whose check row no longer resolves
    in the org arrives here as `None`. The whole reason the function returns a
    bool is stated in its own docstring -- "so a caller can assert on a value
    rather than on the absence of an exception" -- and until this landed,
    flipping that `return False` to `return True` survived the full suite,
    because every existing assertion was about the outbox rather than the answer.

    Both are asserted here, and the RETURN VALUE first: a `True` that published
    nothing is a caller told a notification went out when none did.

    GUARD OBSERVABILITY: change `if check is None: return False` to `return True`
    and this fails on the returned value with an empty outbox beside it.
    """
    from app.models import DomainEvent
    from app.modules.health.alerts import publish_alert_event
    from app.modules.health.settings import get_settings

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    alert_id = await _open_one(admin_engine, fixture, now=now)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        from app.models import HealthAlert

        alert = (
            await session.execute(select(HealthAlert).where(HealthAlert.id == alert_id))
        ).scalar_one()
        before = len(
            (
                await session.execute(
                    select(DomainEvent.id).where(DomainEvent.org_id == fixture.org_id)
                )
            )
            .scalars()
            .all()
        )
        published = await publish_alert_event(
            session,
            fixture.org_id,
            alert,
            event_type="health.alert_cleared",
            check=None,
            settings=await get_settings(session, fixture.org_id),
        )
        after = len(
            (
                await session.execute(
                    select(DomainEvent.id).where(DomainEvent.org_id == fixture.org_id)
                )
            )
            .scalars()
            .all()
        )
        await session.rollback()

    assert published is False, (
        f"publish_alert_event answered {published!r} for an alert whose check does not "
        f"resolve; the outbox went from {before} to {after} events"
    )
    assert after == before


# ---------------------------------------------------------------------------
# The badge's count, which was a floor pretending to be a total
# ---------------------------------------------------------------------------
#
# THE DEFECT. The health module exposed the alert LIST and nothing that
# summarised it, and its `state` filter was single-valued -- so "open" (active
# OR acknowledged OR muted, §0.8) could not even be expressed server-side. The
# shell badge therefore read ONE PAGE of `GET /health/alerts` and filtered it in
# the browser, which on a fleet with more than a page of open alerts is a floor.
# The console was honest about it and rendered "at least 40 needing attention";
# a floor is still not what an operator reads a badge as.
#
# WHAT THESE HOLD CONSTANT AND WHAT THEY VARY. One org shape throughout; what
# varies is the parameter each assertion is about -- the STATE mix (so counting
# `active` alone fails), the SEVERITY mix (so an alphabetical `worst` fails),
# the VOLUME (so a page-bounded implementation fails), and WHOSE org the rows
# are in (so a missing tenant predicate fails, on the engine where it can).


async def _seed_alerts(engine, org, spec: list[tuple[str, str]]) -> None:
    """One alert per `(state, severity)` pair, in one transaction.

    ONE PROFILE, ONE CHECK, A DEVICE PER ALERT -- and each of those three is
    forced by a constraint rather than chosen:

    * `uq_health_alert_open_key` is unique on `(org_id, device_id, check_id,
      selector)` wherever the state is not `cleared`, so the alerts have to
      differ in one of those four;
    * `uq_health_check_profile_type_selector_os` is unique on `(profile_id,
      check_type, selector, os_family)`, so a check per alert has to vary the
      selector too; and
    * §0.11's trigger caps a profile at 32 live checks, which the
      more-than-a-page fixture below would blow straight through.

    So the device is what varies. An alert carries its OWN severity column
    (§2.4), which is why every alert here can sit on one `warning` check and
    still be counted as the severity the spec names.
    """
    from app.models import Device, HealthAlert, HealthCheck, HealthProfile

    now = datetime.now(UTC)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        profile = HealthProfile(org_id=org.org_id, name=f"p-{uuid4().hex[:8]}", description="")
        session.add(profile)
        await session.flush()
        check = HealthCheck(
            org_id=org.org_id,
            profile_id=profile.id,
            check_type="cpu.busy_pct",
            selector="",
            comparator="gt",
            threshold_num=95,
            threshold_bool=None,
            for_ticks=3,
            severity="warning",
            os_family=None,
        )
        session.add(check)
        await session.flush()
        for state, severity in spec:
            device = Device(
                org_id=org.org_id, hostname=f"host-{uuid4().hex[:10]}", os_family="windows"
            )
            session.add(device)
            await session.flush()
            extra: dict[str, object] = {}
            if state == "cleared":
                extra = {"cleared_at": now, "cleared_reason": "recovery"}
            elif state == "muted":
                extra = {"muted_until": now + timedelta(days=1)}
            session.add(
                HealthAlert(
                    org_id=org.org_id,
                    device_id=device.id,
                    check_id=check.id,
                    profile_id=profile.id,
                    selector="",
                    check_type="cpu.busy_pct",
                    severity=severity,
                    state=state,
                    observed_value="97.4",
                    threshold_display="> 95",
                    opened_at=now,
                    opened_policy_version=1,
                    last_seen_at=now,
                    **extra,
                )
            )
        await session.commit()


async def test_the_summary_counts_every_open_alert_and_not_only_the_active_ones(
    admin_engine,
) -> None:
    """`open` is the module's own definition (§0.8): acknowledged is OWNED and
    still open, muted is SILENCED and still open, only `cleared` is over. A
    summary that counted `state = 'active'` would go quiet on a fleet somebody
    had merely acknowledged -- §0.8's failure mode (a), arriving through the
    badge."""
    from tests.health_fixtures import seed_org

    org = await seed_org(admin_engine, "hsum")
    await _seed_alerts(
        admin_engine,
        org,
        [
            ("active", "critical"),
            ("active", "warning"),
            ("acknowledged", "warning"),
            ("muted", "info"),
            ("cleared", "critical"),
        ],
    )

    async with AsyncSession(admin_engine) as session:
        summary = await summarise_open_alerts(session, org.org_id)

    assert summary.open == 4
    assert summary.by_state == {"active": 2, "acknowledged": 1, "muted": 1}
    assert summary.by_severity == {"info": 1, "warning": 2, "critical": 1}
    assert summary.worst_severity == "critical"


async def test_the_summary_of_a_quiet_org_is_a_measured_zero(admin_engine) -> None:
    """Every bucket present and zero, so a caller cannot confuse "none of that
    severity are open" with "this server does not report that severity".
    `worst_severity` is the one field that legitimately has no value when
    nothing is open, and it is `None` rather than `"info"`."""
    from tests.health_fixtures import seed_org

    org = await seed_org(admin_engine, "hsumq")

    async with AsyncSession(admin_engine) as session:
        summary = await summarise_open_alerts(session, org.org_id)

    assert summary.open == 0
    assert summary.by_state == {"active": 0, "acknowledged": 0, "muted": 0}
    assert summary.by_severity == {"info": 0, "warning": 0, "critical": 0}
    assert summary.worst_severity is None


async def test_the_summary_is_not_bounded_by_the_alert_lists_page(admin_engine) -> None:
    """THE FALSIFIER FOR THE FLOOR. `list_alerts` defaults to 200 rows and its
    route caps it at 500, which is what made the browser-side count a floor. A
    summary that loaded rows and counted them in Python inherits exactly that
    bound one layer down, so this seeds MORE THAN A PAGE and asserts the total
    while asserting that the page really is still a page."""
    from tests.health_fixtures import seed_org

    over_a_page = 205
    org = await seed_org(admin_engine, "hsump")
    await _seed_alerts(admin_engine, org, [("active", "warning")] * over_a_page)

    async with AsyncSession(admin_engine) as session:
        summary = await summarise_open_alerts(session, org.org_id)
        page = await list_alerts(session, org.org_id)

    assert len(page) == 200
    assert summary.open == over_a_page


async def test_the_summary_counts_only_this_orgs_alerts_on_the_admin_engine(
    admin_engine,
) -> None:
    """INVARIANT #5, ASSERTED WHERE IT CAN ACTUALLY FAIL.

    Every route in this module runs through `org_session`, whose role is
    NOBYPASSRLS: RLS rejects another org's rows before an application predicate
    is consulted, so deleting `HealthAlert.org_id == org_id` from the service
    turns NO route test red. This runs on the ADMIN (BYPASSRLS) engine, where
    RLS is not the thing filtering and the predicate is the only thing left.

    GUARD OBSERVABILITY, verified by making the edit rather than by assuming
    it: delete the `org_id` predicate from `summarise_open_alerts`, and this
    fails on the first assertion with `ours.open` counting every open alert in
    the database rather than this org's one (212 in the run that verified it,
    since the module's other fixtures are still there) and
    `ours.worst_severity == 'critical'` from another tenant's rows.

    BOTH ORGS ARE ASSERTED, because a summary that always answered zero would
    satisfy the first half on its own.
    """
    from tests.health_fixtures import seed_org

    mine = await seed_org(admin_engine, "hsuma")
    theirs = await seed_org(admin_engine, "hsumb")
    await _seed_alerts(admin_engine, mine, [("active", "warning")])
    await _seed_alerts(
        admin_engine, theirs, [("active", "critical"), ("acknowledged", "critical")]
    )

    async with AsyncSession(admin_engine) as session:
        ours = await summarise_open_alerts(session, mine.org_id)
        yours = await summarise_open_alerts(session, theirs.org_id)

    assert ours.open == 1
    assert ours.by_severity == {"info": 0, "warning": 1, "critical": 0}
    assert ours.worst_severity == "warning"
    assert yours.open == 2
    assert yours.worst_severity == "critical"


async def test_the_summary_ranks_severity_by_seriousness_and_not_alphabetically(
    admin_engine,
) -> None:
    """`critical > warning > info`. Sorted as strings that order reads
    `warning > info > critical`, which puts the calmest word on top -- so the
    fixture below is one where the two answers differ."""
    from tests.health_fixtures import seed_org

    org = await seed_org(admin_engine, "hsumw")
    await _seed_alerts(admin_engine, org, [("active", "info"), ("active", "critical")])

    async with AsyncSession(admin_engine) as session:
        summary = await summarise_open_alerts(session, org.org_id)

    assert summary.worst_severity == "critical"


async def test_the_summary_aggregates_in_the_database_rather_than_in_python(
    admin_engine,
) -> None:
    """THE STRUCTURAL HALF, because every behavioural test above passes equally
    against a `for` loop over every alert in the org -- which is the defect the
    badge had, moved one layer down.

    ONE statement, and it aggregates: at most one row per `(state, severity)`
    pair comes back, nine of them, whatever the size of the fleet.
    """
    import re

    from tests.health_fixtures import seed_org

    org = await seed_org(admin_engine, "hsumg")
    statements: list[str] = []

    async with AsyncSession(admin_engine) as session:
        original = session.execute

        async def record(statement, *args, **kw):
            statements.append(str(statement))
            return await original(statement, *args, **kw)

        session.execute = record  # type: ignore[method-assign]
        await summarise_open_alerts(session, org.org_id)

    assert len(statements) == 1, f"one aggregate query, not {len(statements)}: {statements}"
    emitted = " ".join(statements[0].split())
    assert re.search(r"count\(", emitted, re.I), f"no aggregate: {emitted}"
    assert re.search(r"GROUP BY", emitted, re.I), f"no GROUP BY: {emitted}"
