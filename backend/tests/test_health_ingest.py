"""Report ingestion and §0.8's OPEN predicate
(`docs/modules/endpoint-health-alerting/spec.md` §6, §0.7, §0.8, §0.12).

WHAT THESE FIXTURES HOLD CONSTANT, stated because this project's defining defect
is a guard satisfied by a constant parameter rather than by the guard:

  - every seeded device is `os_family="windows"` and every seeded check is
    `cpu.busy_pct` with `os_family=NULL`, so NOTHING here can see an OS-filter
    bug. That is `test_health_resolution.py`'s job and it varies both.
  - every batch carries `policy_version=1` EXCEPT the four clause-2 tests, which
    vary it deliberately against a stamped ack. A file that used one version
    everywhere would make clause 2 observationally identical to "no clause 2".
  - `now` is injected everywhere, so a test that passes because the machine was
    slow is not possible.

Every assertion below is on a VALUE that was stored or returned -- a row count,
a column, an outcome field -- never on "no exception was raised". Both forms
pass against a function that does nothing; only the first fails against one.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.gateway.protocol import HealthReportMsg, parse_agent_message
from app.modules.health.ingest import ingest_report
from tests.health_fixtures import (
    HealthOrg,
    seed_assignment,
    seed_check,
    seed_device,
    seed_org,
    seed_profile,
    seed_settings,
)

pytestmark = pytest.mark.usefixtures("admin_engine")


def _msg(lines: list[dict[str, Any]], *, policy_version: int = 1) -> HealthReportMsg:
    """A parsed `health_report`, built through `parse_agent_message` rather than
    by constructing the model directly -- the wire model is the security
    boundary and a test that bypasses it can assert on a shape the parser would
    have refused."""
    import json

    msg = parse_agent_message(
        json.dumps(
            {"v": 1, "kind": "health_report", "policy_version": policy_version, "reports": lines}
        )
    )
    assert isinstance(msg, HealthReportMsg)
    return msg


def _line(check_id: UUID | str, **kw: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "check_id": str(check_id),
        "selector": "",
        "kind": "breach",
        "state": "breaching",
        "value": "97.4",
        "observed_at": "2026-08-02T09:14:00Z",
    }
    line.update(kw)
    return line


class _Fixture:
    def __init__(self, org: HealthOrg, profile_id: UUID, check_id: UUID) -> None:
        self.org = org
        self.profile_id = profile_id
        self.check_id = check_id

    @property
    def org_id(self) -> UUID:
        return self.org.org_id

    @property
    def device_id(self) -> UUID:
        return self.org.device_id


async def _armed_org(engine: Any, **check_kw: Any) -> _Fixture:
    """An org whose single device resolves to a one-check profile via the org
    default. The org default rather than a device-level assignment on purpose:
    it is the level a second device in the same org also resolves through, which
    the cross-device tests need."""
    org = await seed_org(engine, "ing")
    profile_id = await seed_profile(engine, org.org_id)
    check_id = await seed_check(engine, org.org_id, profile_id, **check_kw)
    await seed_assignment(engine, org.org_id, profile_id)
    return _Fixture(org, profile_id, check_id)


async def _device_row(session: AsyncSession, device_id: UUID) -> Any:
    from app.models import Device

    return (
        await session.execute(select(Device).where(Device.id == device_id))
    ).scalar_one()


async def _count(session: AsyncSession, model: Any, **filters: Any) -> int:
    statement = select(func.count()).select_from(model)
    for column, value in filters.items():
        statement = statement.where(getattr(model, column) == value)
    return int((await session.execute(statement)).scalar_one())


# ---------------------------------------------------------------------------
# §0.8 clause 1 -- the check must be in the CURRENTLY EFFECTIVE profile
# ---------------------------------------------------------------------------


async def test_a_breach_for_an_unknown_check_is_discarded_and_the_rest_of_the_batch_applies(
    admin_engine,
) -> None:
    """§6 step 1: "one stale line must not reject a whole batch that also
    carries a recovery".

    The load-bearing half is the SECOND assertion. A test with only the stale
    line passes against an implementation that raises and rolls the batch back;
    putting a real recovery behind it is what distinguishes "discarded and
    continued" from "the whole batch died".

    GUARD OBSERVABILITY: delete the `by_id` lookup and this observes a STORED
    alert row for a check nobody configured, with `discarded == 0`.
    """
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    stale = uuid4()
    now = datetime.now(UTC)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        # Open a real alert first so the recovery in the mixed batch has
        # something to close.
        first = await ingest_report(
            session, fixture.org_id, device, _msg([_line(fixture.check_id)]), now=now
        )
        assert first.opened == 1
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg(
                [
                    _line(stale, selector="C:"),
                    _line(fixture.check_id, kind="recovery", state="ok", value="12.0"),
                ]
            ),
            now=now + timedelta(minutes=5),
        )
        await session.commit()

    assert outcome.discarded == 1
    assert outcome.discarded_keys == ((str(stale), "C:"),)
    assert outcome.applied == 1
    assert outcome.cleared == 1

    async with AsyncSession(admin_engine) as session:
        alerts = list(
            (
                await session.execute(
                    select(HealthAlert).where(HealthAlert.org_id == fixture.org_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1, "the stale line must not have minted a second alert"
        assert alerts[0].state == "cleared"
        assert alerts[0].cleared_reason == "recovery"


async def test_a_device_with_no_effective_profile_discards_every_line(admin_engine) -> None:
    """§3 returns `None` and §0.8 clause 1 then matches nothing. The console
    shows "no health profile", never "healthy" -- and an alert opened for a
    device nobody configured would be exactly the fabricated default §3
    rejects."""
    from app.models import DeviceHealthCheckState, HealthAlert

    org = await seed_org(admin_engine, "noprof")
    profile_id = await seed_profile(admin_engine, org.org_id)
    check_id = await seed_check(admin_engine, org.org_id, profile_id)
    # deliberately NO assignment

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, org.device_id)
        outcome = await ingest_report(session, org.org_id, device, _msg([_line(check_id)]))
        await session.commit()

    assert (outcome.applied, outcome.discarded, outcome.opened) == (0, 1, 0)
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=org.org_id) == 0
        assert await _count(session, DeviceHealthCheckState, org_id=org.org_id) == 0


async def test_an_agent_reporting_a_server_evaluated_check_is_discarded(admin_engine) -> None:
    """§0.6: `agent.offline` is the ONE server-evaluated check, and an agent
    that is offline cannot report that it is offline. It is excluded from every
    policy push, so a report naming it is a report about something the device
    was never asked and cannot observe."""
    from app.models import HealthAlert

    org = await seed_org(admin_engine, "srv")
    profile_id = await seed_profile(admin_engine, org.org_id)
    offline_id = await seed_check(
        admin_engine,
        org.org_id,
        profile_id,
        check_type="agent.offline",
        comparator="gt",
        threshold_num=0,
    )
    await seed_assignment(admin_engine, org.org_id, profile_id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, org.device_id)
        outcome = await ingest_report(session, org.org_id, device, _msg([_line(offline_id)]))
        await session.commit()

    assert (outcome.discarded, outcome.opened) == (1, 0)
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=org.org_id) == 0


# ---------------------------------------------------------------------------
# §0.8 clause 2 -- the policy_version tolerance, pinned from BOTH sides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported_version", "expected_alerts", "label"),
    [
        (9, 1, "exactly acknowledged - 1 is ACCEPTED"),
        (8, 0, "acknowledged - 2 is discarded"),
        (10, 1, "the acknowledged version is accepted"),
        (11, 1, "a newer version is accepted"),
    ],
)
async def test_the_policy_version_tolerance_is_exactly_one(
    admin_engine, reported_version: int, expected_alerts: int, label: str
) -> None:
    """§0.8 clause 2: "the report's `policy_version` is >= the device's last
    ACKNOWLEDGED version minus one (one version of tolerance, so a breach in
    flight across a policy push is not lost)".

    FOUR CASES BECAUSE ONE CANNOT SEE THE BOUNDARY. A test that only fed version
    9 passes against `>= acknowledged`, against `>= acknowledged - 1` and
    against no clause at all; the discriminating pair is 9 (accepted) beside 8
    (discarded).

    GUARD OBSERVABILITY: delete the tolerance and the `acknowledged - 1` case
    observes ZERO alerts where it expects one.
    """
    from app.models import DeviceHealthPolicyAck, HealthAlert

    fixture = await _armed_org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        session.add(
            DeviceHealthPolicyAck(
                org_id=fixture.org_id,
                device_id=fixture.device_id,
                profile_id=fixture.profile_id,
                policy_version=10,
            )
        )
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id)], policy_version=reported_version),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        stored = await _count(session, HealthAlert, org_id=fixture.org_id)
    assert stored == expected_alerts, label
    assert outcome.opened == expected_alerts
    assert outcome.discarded == (0 if expected_alerts else 1)


async def test_a_device_that_never_acknowledged_a_policy_keeps_its_breaches(
    admin_engine,
) -> None:
    """§2.11: "a missing row reads as `policy_version = 0`", which makes clause
    2 vacuously true -- deliberately, so a device that has never acknowledged a
    policy does not have its breaches discarded. Failing the other way would
    silence exactly the fleet nobody has finished configuring."""
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id)], policy_version=0),
        )
        await session.commit()
    assert outcome.opened == 1
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=fixture.org_id) == 1


# ---------------------------------------------------------------------------
# §0.8 clause 3 -- dedup, by index and by the ordinary UPDATE path
# ---------------------------------------------------------------------------


async def test_five_identical_breaches_produce_one_alert_and_one_action_row_each(
    admin_engine,
) -> None:
    """§0.10's idempotency guarantee, asserted on COUNTS and on the counter.

    The row counts alone do not falsify much: the plan says so explicitly --
    deleting the in-code duplicate check leaves `uq_health_alert_open_key` to
    refuse the insert, and a path that caught the resulting `IntegrityError`
    would also produce one row. The assertions that discriminate are
    `occurrence_count == 5` and `last_seen_at` having ADVANCED, neither of which
    an exception-catching path produces.
    """
    from app.models import HealthAlert, HealthAlertAction

    fixture = await _armed_org(admin_engine)
    start = datetime.now(UTC)
    for tick in range(5):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            device = await _device_row(session, fixture.device_id)
            await ingest_report(
                session,
                fixture.org_id,
                device,
                _msg([_line(fixture.check_id, value=f"9{tick}.0")]),
                now=start + timedelta(minutes=tick),
            )
            await session.commit()

    async with AsyncSession(admin_engine) as session:
        alerts = list(
            (
                await session.execute(
                    select(HealthAlert).where(HealthAlert.org_id == fixture.org_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.occurrence_count == 5
        assert alert.last_seen_at > alert.opened_at
        assert alert.last_seen_at.replace(tzinfo=UTC) == (start + timedelta(minutes=4)).replace(
            microsecond=alert.last_seen_at.microsecond
        )
        assert alert.observed_value == "94.0", "the latest value, not the first"

        kinds = list(
            (
                await session.execute(
                    select(HealthAlertAction.kind).where(
                        HealthAlertAction.alert_id == alert.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert kinds == ["notify"], "one row per declared kind, forever"


async def test_a_second_alert_opens_only_after_the_first_has_cleared(admin_engine) -> None:
    """The other half of `uq_health_alert_open_key`'s partial predicate. A key
    that has ever alarmed must be able to alarm again -- `WHERE state <>
    'cleared'` is what makes the index a dedup guarantee rather than a permanent
    ban."""
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
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
            now=now + timedelta(minutes=1),
        )
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id)]),
            now=now + timedelta(minutes=2),
        )
        await session.commit()
    assert outcome.opened == 1
    async with AsyncSession(admin_engine) as session:
        states = sorted(
            (
                await session.execute(
                    select(HealthAlert.state).where(HealthAlert.org_id == fixture.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert states == ["active", "cleared"]


async def test_a_reopen_inside_the_flap_window_links_its_predecessor(admin_engine) -> None:
    """§2.4: `previous_alert_id` "links to the last cleared alert for the same
    key within `flap_window_s`, so a re-opened alert's ticket can name its
    predecessor" (§0.10's chronic-flapper case)."""
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
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
            now=now + timedelta(minutes=1),
        )
        await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id)]),
            now=now + timedelta(minutes=2),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        rows = list(
            (
                await session.execute(
                    select(HealthAlert)
                    .where(HealthAlert.org_id == fixture.org_id)
                    .order_by(HealthAlert.opened_at)
                )
            )
            .scalars()
            .all()
        )
    assert rows[0].previous_alert_id is None
    assert rows[1].previous_alert_id == rows[0].id


async def test_a_reopen_outside_the_flap_window_links_nothing(admin_engine) -> None:
    """The negative that gives the window its meaning. A link to an alert from
    four months ago is noise wearing the shape of history; with only the
    positive test, deleting the `cleared_at >= cutoff` predicate changes
    nothing observable."""
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, flap_window_s=300)
    now = datetime.now(UTC)
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
        rows = list(
            (
                await session.execute(
                    select(HealthAlert)
                    .where(HealthAlert.org_id == fixture.org_id)
                    .order_by(HealthAlert.opened_at)
                )
            )
            .scalars()
            .all()
        )
    assert rows[1].previous_alert_id is None


# ---------------------------------------------------------------------------
# §0.8 clause 4 -- flap suppression removes actions, never evidence
# ---------------------------------------------------------------------------


async def test_a_suppressed_key_still_writes_its_alert_with_blocked_actions(
    admin_engine,
) -> None:
    """§0.9: "the alert record is still written. Suppression removes the
    ACTIONS -- the mail, the ticket, the remediation job -- not the evidence."

    Three separate observables, because a design that suppressed the RECORD and
    a design that suppressed the ACTIONS are indistinguishable if you only count
    alerts: the alert row exists, `flapping` is true with a count on it, and the
    action rows are `blocked_flapping` rather than absent.
    """
    from app.models import DomainEvent, HealthAlert, HealthAlertAction

    fixture = await _armed_org(admin_engine)
    await seed_settings(admin_engine, fixture.org_id, flap_threshold=2, flap_window_s=3600)
    now = datetime.now(UTC)

    # open/clear three times: opens 1 and 2 are inside the threshold, open 3 is
    # the one past it.
    for cycle in range(3):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            device = await _device_row(session, fixture.device_id)
            await ingest_report(
                session,
                fixture.org_id,
                device,
                _msg([_line(fixture.check_id)]),
                now=now + timedelta(seconds=cycle * 20),
            )
            if cycle < 2:
                await ingest_report(
                    session,
                    fixture.org_id,
                    device,
                    _msg([_line(fixture.check_id, kind="recovery", state="ok")]),
                    now=now + timedelta(seconds=cycle * 20 + 5),
                )
            await session.commit()

    async with AsyncSession(admin_engine) as session:
        latest = (
            await session.execute(
                select(HealthAlert)
                .where(HealthAlert.org_id == fixture.org_id, HealthAlert.state != "cleared")
            )
        ).scalar_one()
        assert latest.flapping is True
        assert latest.flap_count == 3

        states = list(
            (
                await session.execute(
                    select(HealthAlertAction.state).where(
                        HealthAlertAction.alert_id == latest.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert states == ["blocked_flapping"]

        raised = list(
            (
                await session.execute(
                    select(DomainEvent.subject_id).where(
                        DomainEvent.org_id == fixture.org_id,
                        DomainEvent.event_type == "health.alert_raised",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert latest.id not in raised, "a suppressed open fires no actions, mail included"


# ---------------------------------------------------------------------------
# §0.7 -- `unavailable` is a third state
# ---------------------------------------------------------------------------


async def test_unavailable_never_opens_an_alert(admin_engine) -> None:
    """§0.7: "a 'disk encryption is off' alert fired because the check could not
    run is worse than no check: it trains operators to ignore posture alerts."

    The second half is what makes this more than a smoke test -- `unavailable`
    is still VISIBLE, as a first-class state in `device_health_check_state`. A
    design that dropped the line entirely would pass the alert assertion and
    fail this one.

    GUARD OBSERVABILITY: delete the exclusion and this observes a stored alert
    row.
    """
    from app.models import DeviceHealthCheckState, HealthAlert

    fixture = await _armed_org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, state="unavailable", value="")]),
        )
        await session.commit()

    assert outcome.opened == 0
    assert outcome.applied == 1, "applied, not discarded: the line was understood"
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=fixture.org_id) == 0
        row = (
            await session.execute(
                select(DeviceHealthCheckState).where(
                    DeviceHealthCheckState.org_id == fixture.org_id
                )
            )
        ).scalar_one()
        assert row.state == "unavailable"


# ---------------------------------------------------------------------------
# §0.12 -- the monitoring boundary, as a property
# ---------------------------------------------------------------------------


async def test_no_health_table_grows_with_time_for_a_stable_device(admin_engine) -> None:
    """§0.12 / §14: 100 summaries for one stable device, every row count
    unchanged.

    This is the property test for "what was this device's CPU last Tuesday?"
    being unanswerable from this module's schema. It is not a performance test:
    a single INSERT that slipped into the summary path -- a history row, a
    sample, a per-batch counter -- fails it on the second iteration, which is
    exactly the change §0.12 exists to prevent.
    """
    from app.models import (
        DeviceHealthCheckState,
        DeviceHealthPolicyAck,
        HealthAlert,
        HealthAlertAction,
        HealthFlapState,
    )

    tables = (
        HealthAlert,
        HealthAlertAction,
        DeviceHealthCheckState,
        HealthFlapState,
        DeviceHealthPolicyAck,
    )
    fixture = await _armed_org(admin_engine)
    start = datetime.now(UTC)

    async def counts() -> dict[str, int]:
        async with AsyncSession(admin_engine) as session:
            return {
                model.__tablename__: await _count(session, model, org_id=fixture.org_id)
                for model in tables
            }

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="summary", state="ok", value="41.0")]),
            now=start,
        )
        await session.commit()

    before = await counts()
    for tick in range(1, 101):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            device = await _device_row(session, fixture.device_id)
            await ingest_report(
                session,
                fixture.org_id,
                device,
                _msg(
                    [
                        _line(
                            fixture.check_id,
                            kind="summary",
                            state="ok",
                            value=f"{40 + tick % 7}.0",
                        )
                    ]
                ),
                now=start + timedelta(minutes=30 * tick),
            )
            await session.commit()
    after = await counts()
    assert after == before, f"a health table grew over 100 summaries: {before} -> {after}"
    assert after["device_health_check_state"] == 1


# ---------------------------------------------------------------------------
# Tenancy (invariant #5), pinned on the ADMIN engine
# ---------------------------------------------------------------------------


async def test_ingest_refuses_a_device_from_another_org_on_the_admin_engine(
    admin_engine,
) -> None:
    """Invariant #5, pinned where RLS is NOT the thing doing the rejecting.

    The org here comes from the certificate-bound connection and the device row
    from the database; a caller that pairs them wrongly must write nothing. Run
    through `org_session` this test passes even with the predicate deleted,
    because RLS refuses first -- which is precisely the defect that shipped four
    times in the SLA sub-project.

    GUARD OBSERVABILITY: delete the `device.org_id != org_id` check and this
    observes an alert row STORED UNDER ORG A for org B's device, and
    `outcome.opened == 1`.
    """
    from app.models import HealthAlert

    a = await _armed_org(admin_engine)
    b = await _armed_org(admin_engine)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        b_device = await _device_row(session, b.device_id)
        outcome = await ingest_report(
            session, a.org_id, b_device, _msg([_line(b.check_id)])
        )
        await session.commit()

    assert outcome == type(outcome)()
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=a.org_id) == 0
        assert await _count(session, HealthAlert, org_id=b.org_id) == 0


async def test_two_orgs_with_the_same_check_shape_do_not_share_an_alert(admin_engine) -> None:
    """Two orgs, each with its own device and its own `cpu.busy_pct` check.
    Each opens one alert and neither sees the other's -- read on the admin
    engine, so the separation is the application's `org_id` predicates rather
    than RLS."""
    from app.models import HealthAlert

    a = await _armed_org(admin_engine)
    b = await _armed_org(admin_engine)
    for fixture in (a, b):
        async with AsyncSession(admin_engine, expire_on_commit=False) as session:
            device = await _device_row(session, fixture.device_id)
            await ingest_report(
                session, fixture.org_id, device, _msg([_line(fixture.check_id)])
            )
            await session.commit()

    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=a.org_id) == 1
        assert await _count(session, HealthAlert, org_id=b.org_id) == 1


# ---------------------------------------------------------------------------
# The gateway branch (Task 2's interim is gone)
# ---------------------------------------------------------------------------


async def test_the_gateway_branch_ingests_and_publishes_after_commit(
    admin_engine, app_engine, tmp_path
) -> None:
    """`handle_agent_message` now INGESTS a `health_report` instead of dropping
    it, and publishes the counts on the UI channel only after the tenant
    transaction has committed.

    The published event carries `applied`/`discarded` because §0.8's amendment
    puts the discard count there rather than in a table -- "a per-batch counter
    table is a time series wearing an ops-metrics hat". Asserting the payload is
    what makes that decision observable.
    """
    import json as _json

    import redis.asyncio as aioredis

    from app.core.signing import LocalEd25519Signer
    from app.gateway.dispatch import handle_agent_message
    from app.jobs.engine import JobEngine
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    redis = aioredis.from_url("redis://localhost:6379/9", decode_responses=True)
    await redis.flushdb()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"ui:{fixture.org_id}")

    msg = _msg([_line(fixture.check_id), _line(uuid4(), selector="X:")])
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    await handle_agent_message(
        app_engine,
        engine,
        redis,
        msg,
        device_id=fixture.device_id,
        org_id=fixture.org_id,
    )

    async with AsyncSession(admin_engine) as session:
        alert = (
            await session.execute(
                select(HealthAlert).where(HealthAlert.org_id == fixture.org_id)
            )
        ).scalar_one()
        assert alert.state == "active"
        assert alert.check_id == fixture.check_id

    published = None
    for _ in range(30):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message:
            published = message
            break
    await pubsub.unsubscribe()
    await redis.aclose()
    assert published is not None
    assert _json.loads(published["data"]) == {
        "kind": "health_reports_ingested",
        "device_id": str(fixture.device_id),
        "applied": 1,
        "discarded": 1,
    }


async def test_a_report_on_the_wrong_org_binding_is_audited_as_an_identity_mismatch(
    admin_engine, app_engine, tmp_path
) -> None:
    """The existing `_handle_security_events` tenancy path, asserted rather than
    re-implemented: a device that does not resolve inside the connection's org
    is audited `gateway.identity_mismatch` and ingests nothing. Identity comes
    from the certificate binding (invariant #2)."""
    import redis.asyncio as aioredis

    from app.core.signing import LocalEd25519Signer
    from app.core.tenancy import org_session
    from app.gateway.dispatch import handle_agent_message
    from app.jobs.engine import JobEngine
    from app.models import AuditEvent, HealthAlert

    a = await _armed_org(admin_engine)
    b = await _armed_org(admin_engine)
    redis = aioredis.from_url("redis://localhost:6379/9", decode_responses=True)

    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    await handle_agent_message(
        app_engine,
        engine,
        redis,
        _msg([_line(b.check_id)]),
        device_id=b.device_id,
        org_id=a.org_id,
    )
    await redis.aclose()

    async with org_session(a.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        mismatches = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == a.org_id,
                        AuditEvent.action == "gateway.identity_mismatch",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(mismatches) == 1
        assert mismatches[0].meta["message_kind"] == "health_report"

    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=a.org_id) == 0
        assert await _count(session, HealthAlert, org_id=b.org_id) == 0


async def test_two_devices_in_one_org_open_independent_alerts(admin_engine) -> None:
    """The dedup key is `(org_id, device_id, check_id, selector)`. One check
    breaching on two devices is two incidents, not one -- a test with a single
    device cannot tell `device_id` from a constant."""
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    second = await seed_device(admin_engine, fixture.org_id)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        for device_id in (fixture.device_id, second):
            device = await _device_row(session, device_id)
            await ingest_report(
                session, fixture.org_id, device, _msg([_line(fixture.check_id)])
            )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        devices = sorted(
            str(d)
            for d in (
                await session.execute(
                    select(HealthAlert.device_id).where(HealthAlert.org_id == fixture.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert devices == sorted([str(fixture.device_id), str(second)])


async def test_one_selector_does_not_close_another_selectors_alert(admin_engine) -> None:
    """`selector` is the fourth component of the dedup key. `disk.free_pct` on
    `C:` and on `D:` are two alerts, and a recovery on one must not clear the
    other -- a fixture with an empty selector everywhere cannot see a dropped
    `selector` predicate."""
    from app.models import HealthAlert

    fixture = await _armed_org(
        admin_engine, check_type="disk.free_pct", comparator="lt", threshold_num=10
    )
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg(
                [
                    _line(fixture.check_id, selector="C:", value="3.1"),
                    _line(fixture.check_id, selector="D:", value="4.2"),
                ]
            ),
            now=now,
        )
        await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, selector="C:", kind="recovery", state="ok")]),
            now=now + timedelta(minutes=1),
        )
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        rows = {
            selector: state
            for selector, state in (
                await session.execute(
                    select(HealthAlert.selector, HealthAlert.state).where(
                        HealthAlert.org_id == fixture.org_id
                    )
                )
            ).all()
        }
    assert rows == {"C:": "cleared", "D:": "active"}


# ---------------------------------------------------------------------------
# `kind` vs `state`: the wire carries two INDEPENDENT enums (§0.7)
# ---------------------------------------------------------------------------
#
# `gateway/protocol.py` validates `kind` and `state` separately and never
# against each other, so `{"kind": "recovery", "state": "breaching"}` parses
# cleanly and reaches ingestion. The module already knew a state could
# contradict a kind -- that is the whole reason an `unavailable` recovery clears
# nothing -- but it handled ONE of the two contradictions, and the other cleared
# real alerts.


async def _state_row(admin_engine: Any, org_id: UUID) -> tuple[str, str]:
    from app.models import DeviceHealthCheckState

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(DeviceHealthCheckState.state, DeviceHealthCheckState.last_value).where(
                    DeviceHealthCheckState.org_id == org_id
                )
            )
        ).one()
    return row[0], row[1]


async def test_a_recovery_reporting_breaching_does_not_clear_the_alert(admin_engine) -> None:
    """The finding, and its three halves are asserted together because each
    alone is satisfiable by a different wrong implementation.

    1. `cleared == 0` and the alert still `active`: the line did not launder a
       live breach into a fixed problem.
    2. `contradictory == 1`: it was REFUSED rather than never looked at. An
       implementation that dropped the line before `_upsert_check_state` would
       satisfy (1) and fail (3).
    3. `device_health_check_state` still reads `breaching 99.0`: the observation
       was kept. THIS IS WHAT MADE THE BUG COMPOUND -- the state row is written
       from the same line and trusts it as data, so after the bogus clear the
       console showed a breaching check with no alert, and the next real edge
       re-opened it. The pair oscillated open/cleared at the tick interval,
       writing a `health.alert_raised` and a `health.alert_cleared` each cycle.
    """
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        assert (
            await ingest_report(
                session, fixture.org_id, device, _msg([_line(fixture.check_id)]), now=now
            )
        ).opened == 1
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="recovery", state="breaching", value="99.0")]),
            now=now + timedelta(minutes=2),
        )
        await session.commit()

    assert (outcome.cleared, outcome.contradictory) == (0, 1)
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(HealthAlert.state, HealthAlert.cleared_reason).where(
                    HealthAlert.org_id == fixture.org_id
                )
            )
        ).one()
    assert row == ("active", None)
    assert await _state_row(admin_engine, fixture.org_id) == ("breaching", "99.0")


async def test_a_breach_reporting_ok_opens_nothing(admin_engine) -> None:
    """The same contradiction on the OPEN side, and it is worse than the clear
    side because it has no exit: an alert opened for a condition that was never
    breaching will never receive a `recovery` for it, so nothing but clause 4,
    5 or 6 can ever close it -- §0.8's failure mode (a) manufactured from a
    single malformed line.

    Only `unavailable` was excluded here before, on §0.7's grounds; `ok` went
    straight to `_open_alert`.
    """
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        outcome = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="breach", state="ok", value="3.0")]),
            now=now,
        )
        await session.commit()

    assert (outcome.opened, outcome.contradictory) == (0, 1)
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=fixture.org_id) == 0
    assert await _state_row(admin_engine, fixture.org_id) == ("ok", "3.0")


async def test_a_summary_carries_all_three_states_legitimately(admin_engine) -> None:
    """The negative that bounds the guard. A `summary` is a periodic
    RESTATEMENT, not an edge, so none of its three states contradicts it -- and
    clauses 2 and 3 both depend on that. A guard written as "state must equal
    the kind's state" would break `summary_ok` and `unavailable_timeout`
    together, i.e. would take out the repair path for a lost recovery while
    fixing an unrelated line.
    """
    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        assert (
            await ingest_report(
                session, fixture.org_id, device, _msg([_line(fixture.check_id)]), now=now
            )
        ).opened == 1
        await session.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        breaching = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="summary", state="breaching", value="98")]),
            now=now + timedelta(minutes=30),
        )
        ok = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="summary", state="ok", value="12")]),
            now=now + timedelta(minutes=60),
        )
        await session.commit()

    assert (breaching.contradictory, breaching.cleared) == (0, 0)
    assert (ok.contradictory, ok.cleared) == (0, 1)


async def test_unavailable_is_the_third_state_and_never_counts_as_a_contradiction(
    admin_engine,
) -> None:
    """The boundary between the two rules, and it is the one a careless guard
    erases.

    §0.7 makes `unavailable` a real third state with its own correct handling:
    an `unavailable` breach opens nothing, an `unavailable` recovery clears
    nothing, and only clause 3's AGED summary clears, under its own reason. A
    guard written as "state must match kind" would fold both edges into the
    protocol-violation path -- logging a WARNING about a well-behaved agent and
    filing a genuine removed-sensor signal as an agent bug.

    `contradictory == 0` in both directions is the assertion; the alert's state
    is asserted alongside it so "not counted" cannot pass by also "not handled".
    """
    from app.models import HealthAlert

    fixture = await _armed_org(admin_engine)
    now = datetime.now(UTC)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        breach = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="breach", state="unavailable", value="")]),
            now=now,
        )
        await session.commit()
    assert (breach.opened, breach.contradictory, breach.applied) == (0, 0, 1)
    async with AsyncSession(admin_engine) as session:
        assert await _count(session, HealthAlert, org_id=fixture.org_id) == 0

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = await _device_row(session, fixture.device_id)
        assert (
            await ingest_report(
                session, fixture.org_id, device, _msg([_line(fixture.check_id)]), now=now
            )
        ).opened == 1
        recovery = await ingest_report(
            session,
            fixture.org_id,
            device,
            _msg([_line(fixture.check_id, kind="recovery", state="unavailable", value="")]),
            now=now + timedelta(minutes=1),
        )
        await session.commit()
    assert (recovery.cleared, recovery.contradictory) == (0, 0)
    async with AsyncSession(admin_engine) as session:
        state = (
            await session.execute(
                select(HealthAlert.state).where(HealthAlert.org_id == fixture.org_id)
            )
        ).scalar_one()
    assert state == "active", "the sensor going quiet is not the problem going away"
