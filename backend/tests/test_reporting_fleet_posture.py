"""`fleet-posture` (spec §5.1, §2, §6.3, §6.4, §12.5, §12.10, §12.11).

**TWO ORGS, WITH DELIBERATELY OVERLAPPING DATA.** A report is a wide join with
no per-row route in front of it, so "provably org-scoped" is not a formality
here. Both orgs get the SAME hostnames, the SAME `os_family`, and alerts of the
SAME severity opened inside the SAME window -- because two orgs whose rows
differ in every column make a missing org predicate INVISIBLE: the assertion
passes because the other org's rows would not have matched the filters anyway.

**AND THE APPLICATION PREDICATE IS PINNED ON THE ADMIN ENGINE.** A cross-org
test driven only through `org_session` passes even when the service's own
`org_id ==` predicate has been deleted, because RLS rejects first. §6.4 requires
an explicit `Device.org_id == org_id` in ADDITION to RLS, and the only place
that requirement is observable is where RLS is not the thing doing the
rejecting. Falsified during the build by deleting the predicate: the admin-engine
test went red and the `org_session` test stayed green.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.client_scope import ClientScope
from app.core.tenancy import admin_session, org_session
from app.modules.reporting.definitions.fleet_posture import (
    _SEVERITY_LADDER,
    FleetPostureParams,
)
from app.modules.reporting.registry import REGISTRY
from app.modules.reporting.service import execute_report
from tests.reporting_fixtures import (
    AlertAnchor,
    seed_agent,
    seed_alert,
    seed_alert_anchor,
    seed_device,
)

KEY = "fleet-posture"
CODES = ("reporting.read", "devices.read", "health.read")


def _definition() -> Any:
    return REGISTRY[KEY]


async def _run(api_env: Any, org_id: UUID, **params: Any) -> Any:
    """The report through `org_session` on the RLS-constrained app engine --
    the way a request runs. The API-level path is exercised separately; this is
    for the cases that assert on typed rows rather than on JSON."""
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        return await execute_report(
            session, _definition(), FleetPostureParams(**params), org_id=org_id
        )


# --------------------------------------------------------------------------- #
# The severity ladder is a pinned copy of the health module's.
# --------------------------------------------------------------------------- #


async def test_the_severity_ladder_matches_the_health_module() -> None:
    """`fleet_posture` restates the ladder rather than importing it (§1.3:
    reporting reads tables, not module code). A restatement without a drift
    alarm is a guess -- this is the alarm."""
    from app.modules.health.registry import SEVERITIES

    assert _SEVERITY_LADDER == SEVERITIES


# --------------------------------------------------------------------------- #
# Tenancy (§12.5, Global Constraint 4 and 5).
# --------------------------------------------------------------------------- #


async def _seed_two_overlapping_orgs(
    api_env: Any,
) -> tuple[UUID, UUID, dict[str, UUID], dict[str, UUID]]:
    """Org A and org B, seeded IDENTICALLY except for the org.

    Same hostnames, same OS family and version, same agent recency, alerts of
    the same severity opened in the same window. The ONLY thing that can
    exclude org B's rows from org A's report is an org predicate.
    """
    org_a = await api_env.create_org("rep-a")
    org_b = await api_env.create_org("rep-b")
    ids_a: dict[str, UUID] = {}
    ids_b: dict[str, UUID] = {}
    for org_id, ids in ((org_a, ids_a), (org_b, ids_b)):
        anchor: AlertAnchor = await seed_alert_anchor(api_env.admin_engine, org_id)
        for hostname in ("shared-host-1", "shared-host-2"):
            device = await seed_device(api_env.admin_engine, org_id, hostname=hostname)
            ids[hostname] = device.device_id
            await seed_agent(
                api_env.admin_engine, org_id, device.device_id, last_seen_days_ago=0.5
            )
            await seed_alert(
                api_env.admin_engine,
                org_id,
                device.device_id,
                anchor,
                severity="critical",
                opened_days_ago=2,
            )
    return org_a, org_b, ids_a, ids_b


async def test_a_report_returns_only_the_callers_org(api_env: Any) -> None:
    """§12.5, through the API, exactly as a request runs."""
    org_a, _org_b, ids_a, ids_b = await _seed_two_overlapping_orgs(api_env)
    from tests.reporting_fixtures import reporting_user

    _, sub = await reporting_user(api_env, org_a, *CODES)
    response = await api_env.client.get(
        f"/api/v1/reports/{KEY}", headers=await api_env.headers(org_a, sub)
    )
    assert response.status_code == 200, response.text
    returned = {row["device_id"] for row in response.json()["rows"]}
    assert returned == {str(v) for v in ids_a.values()}
    assert not (returned & {str(v) for v in ids_b.values()})
    assert response.json()["totals"]["devices"] == 2


async def test_the_org_predicate_holds_where_rls_is_not_doing_the_rejecting(
    api_env: Any, admin_engine: AsyncEngine
) -> None:
    """GLOBAL CONSTRAINT 4, and the reason this test exists at all.

    `admin_session` connects as the RLS-BYPASSING role. If `fleet_posture`'s
    `Device.org_id == org_id` predicate were deleted, the test above would still
    be green -- RLS would reject org B's rows before the application ever saw
    them -- and this one would return four devices instead of two. That contrast
    is the whole point: this is the only assertion in the file that can see the
    predicate.
    """
    org_a, _org_b, ids_a, ids_b = await _seed_two_overlapping_orgs(api_env)

    async with admin_session(engine=admin_engine) as session:
        page = await execute_report(
            session, _definition(), FleetPostureParams(), org_id=org_a
        )

    returned = {row.device_id for row in page.rows}
    assert returned == set(ids_a.values()), returned
    assert not (returned & set(ids_b.values()))
    assert page.meta.total == 2
    assert page.totals["devices"] == 2


# --------------------------------------------------------------------------- #
# §2's no-inference rule (§5.1's honesty requirement).
# --------------------------------------------------------------------------- #


async def test_a_device_with_no_agent_is_present_and_stale(api_env: Any) -> None:
    """A device that has never enrolled is `stale`, `never_enrolled`, and IN THE
    REPORT.

    Falsify by making the agent join inner: the device vanishes, `totals.devices`
    reads 1 instead of 2, and `stale_devices` reads 0 instead of 1 -- observably
    different numbers, not a test that "the query ran". A fleet report whose
    denominator quietly excludes the machines nobody has heard from is the worst
    number it could produce.
    """
    org_id = await api_env.create_org("rep-noagent")
    enrolled = await seed_device(api_env.admin_engine, org_id, hostname="aaa-enrolled")
    await seed_agent(api_env.admin_engine, org_id, enrolled.device_id, last_seen_days_ago=0.1)
    never = await seed_device(api_env.admin_engine, org_id, hostname="bbb-never")

    page = await _run(api_env, org_id)
    by_id = {row.device_id: row for row in page.rows}
    assert set(by_id) == {enrolled.device_id, never.device_id}

    assert by_id[never.device_id].stale is True
    assert by_id[never.device_id].agent_status == "never_enrolled"
    assert by_id[never.device_id].last_seen_at is None
    assert by_id[enrolled.device_id].stale is False
    assert by_id[enrolled.device_id].agent_status == "online"

    assert page.totals == {
        "devices": 2,
        "stale_devices": 1,
        "devices_with_open_alerts": 0,
    }


async def test_an_agent_outside_the_window_is_stale_and_the_window_is_a_parameter(
    api_env: Any,
) -> None:
    """`stale_after_days` is observable: the SAME device reads stale at 7 days
    and fresh at 30. A single-value test could not tell the predicate from a
    constant."""
    org_id = await api_env.create_org("rep-window")
    device = await seed_device(api_env.admin_engine, org_id, hostname="drifting")
    await seed_agent(api_env.admin_engine, org_id, device.device_id, last_seen_days_ago=10)

    default_window = await _run(api_env, org_id)
    assert default_window.rows[0].stale is True
    assert default_window.totals["stale_devices"] == 1

    wide_window = await _run(api_env, org_id, stale_after_days=30)
    assert wide_window.rows[0].stale is False
    assert wide_window.totals["stale_devices"] == 0


async def test_open_alerts_are_counted_by_severity_and_cleared_ones_are_not(
    api_env: Any,
) -> None:
    """An acknowledged or muted alert is still OPEN. Acknowledging is "I have
    seen it" and muting is "not now"; neither is "it stopped happening", and
    counting them as closed would let an org silence its own posture report by
    clicking acknowledge."""
    org_id = await api_env.create_org("rep-alerts")
    device = await seed_device(api_env.admin_engine, org_id, hostname="noisy")
    await seed_agent(api_env.admin_engine, org_id, device.device_id, last_seen_days_ago=0.1)
    anchor = await seed_alert_anchor(api_env.admin_engine, org_id)

    await seed_alert(
        api_env.admin_engine, org_id, device.device_id, anchor,
        severity="critical", state="active", selector="c1",
    )
    await seed_alert(
        api_env.admin_engine, org_id, device.device_id, anchor,
        severity="warning", state="acknowledged", selector="w1",
    )
    await seed_alert(
        api_env.admin_engine, org_id, device.device_id, anchor,
        severity="warning", state="muted", selector="w2",
    )
    await seed_alert(
        api_env.admin_engine, org_id, device.device_id, anchor,
        severity="critical", state="cleared", selector="c2",
    )

    page = await _run(api_env, org_id)
    (row,) = page.rows
    assert row.open_alerts_critical == 1, "the cleared critical must not count"
    assert row.open_alerts_warning == 2, "acknowledged and muted are still open"
    assert row.newest_alert_opened_at is not None
    assert page.totals["devices_with_open_alerts"] == 1


async def test_severity_at_least_filters_the_counts_but_not_the_device_list(
    api_env: Any,
) -> None:
    """§5.1: `severity_at_least` does not filter the fleet. A device with no
    qualifying alert still appears, with zero counts -- otherwise the denominator
    changes with the filter and the report answers a different question than the
    one it is labelled with."""
    org_id = await api_env.create_org("rep-sev")
    quiet = await seed_device(api_env.admin_engine, org_id, hostname="aaa-quiet")
    loud = await seed_device(api_env.admin_engine, org_id, hostname="bbb-loud")
    anchor = await seed_alert_anchor(api_env.admin_engine, org_id)
    await seed_alert(
        api_env.admin_engine, org_id, loud.device_id, anchor, severity="warning"
    )

    at_warning = await _run(api_env, org_id, severity_at_least="warning")
    assert len(at_warning.rows) == 2
    assert at_warning.totals["devices_with_open_alerts"] == 1

    at_critical = await _run(api_env, org_id, severity_at_least="critical")
    assert len(at_critical.rows) == 2, "the device list must not shrink"
    assert at_critical.totals["devices_with_open_alerts"] == 0
    assert {row.open_alerts_warning for row in at_critical.rows} == {0}
    assert quiet.device_id in {row.device_id for row in at_critical.rows}


async def test_the_os_family_filter_narrows_the_match_and_the_totals(
    api_env: Any,
) -> None:
    org_id = await api_env.create_org("rep-os")
    await seed_device(api_env.admin_engine, org_id, hostname="w1", os_family="windows")
    await seed_device(api_env.admin_engine, org_id, hostname="l1", os_family="linux")

    everything = await _run(api_env, org_id)
    assert everything.totals["devices"] == 2

    linux_only = await _run(api_env, org_id, os_family="linux")
    assert linux_only.totals["devices"] == 1
    assert [row.os_family for row in linux_only.rows] == ["linux"]
    assert linux_only.meta.total == 1


# --------------------------------------------------------------------------- #
# The envelope (§6.3, §12.10, §12.11).
# --------------------------------------------------------------------------- #


async def test_totals_are_computed_over_the_whole_match_not_the_page(
    api_env: Any,
) -> None:
    """§12.10. FIVE devices whose stale/alert values DIFFER, so a `totals`
    accidentally computed over the page produces a DIFFERENT number rather than
    the same one by coincidence: page 0 holds two stale devices and page 2 holds
    none, so a page-scoped `stale_devices` would read 2 and then 0."""
    org_id = await api_env.create_org("rep-totals")
    anchor = await seed_alert_anchor(api_env.admin_engine, org_id)
    for index, (hostname, stale, alert) in enumerate(
        [
            ("a-stale", True, True),
            ("b-stale", True, False),
            ("c-fresh", False, True),
            ("d-fresh", False, False),
            ("e-fresh", False, False),
        ]
    ):
        device = await seed_device(api_env.admin_engine, org_id, hostname=hostname)
        await seed_agent(
            api_env.admin_engine,
            org_id,
            device.device_id,
            last_seen_days_ago=30 if stale else 0.1,
        )
        if alert:
            await seed_alert(
                api_env.admin_engine,
                org_id,
                device.device_id,
                anchor,
                severity="critical",
                selector=f"s{index}",
            )

    expected = {"devices": 5, "stale_devices": 2, "devices_with_open_alerts": 2}
    first = await _run(api_env, org_id, limit=2, offset=0)
    third = await _run(api_env, org_id, limit=2, offset=4)

    assert first.totals == expected, first.totals
    assert third.totals == expected, third.totals
    assert first.meta.total == 5 and third.meta.total == 5
    # The premise: the two pages really do hold different rows.
    assert len(first.rows) == 2 and len(third.rows) == 1
    assert {r.stale for r in first.rows} == {True}
    assert {r.stale for r in third.rows} == {False}


async def test_ordering_is_stable_across_a_tie_on_the_sort_key(api_env: Any) -> None:
    """§12.11. FOUR devices SHARING ONE HOSTNAME -- `ix_devices_org_hostname` is
    not unique and two machines imaged from the same template routinely collide.

    A DELIBERATE TIE IS REQUIRED: without one, an unstable sort is unobservable
    because the sort key already totally orders the rows. Paged in size-1 pages,
    every id must appear exactly once; drop the `Device.id` tiebreaker and
    PostgreSQL is free to return the tied rows in any order per statement, so
    some id appears twice and another not at all.
    """
    org_id = await api_env.create_org("rep-tie")
    expected: set[UUID] = set()
    for _ in range(4):
        device = await seed_device(api_env.admin_engine, org_id, hostname="identical")
        expected.add(device.device_id)

    seen: list[UUID] = []
    for offset in range(4):
        page = await _run(api_env, org_id, limit=1, offset=offset)
        assert len(page.rows) == 1, page.rows
        seen.append(page.rows[0].device_id)

    assert len(seen) == len(set(seen)), seen
    assert set(seen) == expected


async def test_the_meta_envelope_is_populated_and_reproducible_is_asserted(
    api_env: Any,
) -> None:
    """`reproducible` is `True` for `fleet-posture` -- its sources are
    current-state, not a mutable historical record -- and the VALUE is asserted
    rather than defaulted. `sla-attainment` is the report where it is `False`,
    and having both cases in the suite is the only thing that catches a
    `reproducible` hardcoded either way."""
    org_id = await api_env.create_org("rep-meta")
    await seed_device(api_env.admin_engine, org_id, hostname="only")
    from tests.reporting_fixtures import reporting_user

    _, sub = await reporting_user(api_env, org_id, *CODES)
    before = datetime.now(UTC)
    response = await api_env.client.get(
        f"/api/v1/reports/{KEY}?limit=10&offset=0",
        headers=await api_env.headers(org_id, sub),
    )
    assert response.status_code == 200, response.text
    meta = response.json()["meta"]

    assert meta["report_key"] == KEY
    assert meta["reproducible"] is True
    assert meta["total"] == 1
    assert meta["limit"] == 10
    assert meta["offset"] == 0
    assert meta["window_from"] is None and meta["window_to"] is None
    assert datetime.fromisoformat(meta["generated_at"]) >= before

    modules = {source["module"]: source for source in meta["sources"]}
    assert set(modules) == {"devices", "health"}
    assert modules["devices"]["permission"] == "devices.read"
    assert modules["health"]["permission"] == "health.read"
    assert modules["health"]["recency"].strip()


async def test_the_row_model_carries_a_description_on_every_field() -> None:
    """Property 5 as written, not through the class-docstring fallback. There
    was exactly ONE `Field(description=...)` in any module's `schema.py` before
    this module; the loop cannot supply per-field prose, so the definition
    must."""
    undocumented = [
        name
        for name, field in _definition().row_model.model_fields.items()
        if not (field.description or "").strip()
    ]
    assert not undocumented, undocumented
