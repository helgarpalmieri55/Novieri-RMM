"""The fleet patch ROLLUPS (docs/modules/patch/view-redesign.md §3).

WHAT THESE EXIST TO STOP. `/patch` rendered `fleet_report()["exposures"]` --
one row per `(device, patch)` pair, uncapped -- above a patch catalogue that
carried no fleet count at all. One Ubuntu box with 300 upgradable packages was
600 rows on one page, and the number that would have made the list actionable
("41 of your 60 devices are missing this") was in no response.

Two aggregates replace it, and every assertion below is about a number that
CANNOT be computed in the browser from a page of rows: a distinct-device count
per patch, a severity histogram over the whole filtered set, and a per-device
worst-severity. A console that fetched 500 rows and grouped them would be the
same defect one layer down -- correct until the cap bites, then silently wrong.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Device,
    DeviceGroup,
    DevicePatchStatus,
    Job,
    JobStatus,
    Patch,
    PatchApproval,
    PatchPolicy,
    PatchRun,
)
from app.modules.patch.reporting import device_rollup, update_rollup

# --------------------------------------------------------------------------- #
# Seeding helpers -- everything goes in through the BYPASSRLS admin engine so a
# test can build two tenants side by side.
# --------------------------------------------------------------------------- #


async def _device(api_env, org_id: UUID, hostname: str, os_family: str = "windows") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        dev = Device(org_id=org_id, hostname=hostname, os_family=os_family)
        s.add(dev)
        await s.commit()
        return dev.id


async def _patch(
    api_env,
    org_id: UUID,
    identifier: str,
    severity: str = "critical",
    os_family: str = "windows",
    source: str = "windows_update",
    title: str | None = None,
) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        row = Patch(
            org_id=org_id,
            os_family=os_family,
            source=source,
            identifier=identifier,
            title=title or f"Update {identifier}",
            severity=severity,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _status(api_env, org_id: UUID, device_id: UUID, patch_id: UUID, state: str) -> None:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(
            DevicePatchStatus(
                org_id=org_id, device_id=device_id, patch_id=patch_id, state=state
            )
        )
        await s.commit()


async def _succeeded_scan(api_env, org_id: UUID, device_id: UUID, when: datetime) -> None:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(
            Job(
                org_id=org_id,
                type="patch.scan",
                target_type="device",
                target_id=device_id,
                params={},
                nonce=f"n-{uuid4().hex[:8]}",
                issued_at=when,
                expires_at=when,
                status=JobStatus.succeeded,
                finished_at=when,
            )
        )
        await s.commit()


# --------------------------------------------------------------------------- #
# GET /patch/updates -- one row per PATCH, with a fleet count on it
# --------------------------------------------------------------------------- #


async def test_updates_rollup_is_one_row_per_patch_carrying_the_devices_missing_it(
    api_env,
) -> None:
    """THE NUMBER THE OLD PAGE COULD NOT SAY. `GET /patches` returned bare
    catalogue rows, so an update missing on one machine and one missing on
    forty rendered identically. Two devices, two patches, four status rows: the
    rollup answers with TWO rows, not four."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    a = await _device(api_env, org_id, "wks-a")
    b = await _device(api_env, org_id, "wks-b")
    shared = await _patch(api_env, org_id, "KB-SHARED")
    lonely = await _patch(api_env, org_id, "KB-LONELY")
    await _status(api_env, org_id, a, shared, "missing")
    await _status(api_env, org_id, b, shared, "missing")
    await _status(api_env, org_id, a, lonely, "missing")
    await _status(api_env, org_id, b, lonely, "installed")

    resp = await api_env.client.get("/api/v1/patch/updates", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total"] == 2, "one row per PATCH, not per (device, patch) pair"
    rows = {row["identifier"]: row for row in body["items"]}
    assert rows["KB-SHARED"]["devices_missing"] == 2
    assert rows["KB-SHARED"]["devices_outstanding"] == 2
    assert rows["KB-LONELY"]["devices_missing"] == 1
    assert rows["KB-LONELY"]["devices_installed"] == 1
    assert rows["KB-LONELY"]["devices_outstanding"] == 1


async def test_updates_rollup_counts_approved_and_failed_as_outstanding(api_env) -> None:
    """`approved` is cleared-but-not-installed and `failed` is tried-and-not-
    installed; both are still a patch the machine does not have. The panel's
    one number has to agree with `reporting.NON_COMPLIANT_STATES`, which is
    what the compliance percentages are computed from."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    a = await _device(api_env, org_id, "wks-a")
    b = await _device(api_env, org_id, "wks-b")
    c = await _device(api_env, org_id, "wks-c")
    patch_id = await _patch(api_env, org_id, "KB-MIXED")
    await _status(api_env, org_id, a, patch_id, "missing")
    await _status(api_env, org_id, b, patch_id, "approved")
    await _status(api_env, org_id, c, patch_id, "failed")

    body = (await api_env.client.get("/api/v1/patch/updates", headers=headers)).json()
    row = body["items"][0]
    assert (row["devices_missing"], row["devices_approved"], row["devices_failed"]) == (1, 1, 1)
    assert row["devices_outstanding"] == 3


async def test_updates_rollup_narrows_by_severity_and_says_what_it_hid(api_env) -> None:
    """THE HALF THAT KEEPS A FILTERED COUNT FROM READING AS A TOTAL.

    The console defaults to the severities that demand a decision. That is only
    safe if the response also carries the size of the set it is NOT showing --
    otherwise "8 updates" on a machine missing 300 is a lie the operator cannot
    detect. `severity_counts` applies every filter EXCEPT severity, so
    `sum(severity_counts) == total_unfiltered` by construction.
    """
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a", os_family="linux")
    for index, severity in enumerate(
        ["critical", "important", "important", "unknown", "unknown", "unknown", "low"]
    ):
        patch_id = await _patch(
            api_env, org_id, f"pkg-{index}", severity=severity, os_family="linux", source="apt"
        )
        await _status(api_env, org_id, device_id, patch_id, "missing")

    body = (
        await api_env.client.get(
            "/api/v1/patch/updates?severity=critical&severity=important", headers=headers
        )
    ).json()

    assert body["total"] == 3
    assert [row["severity"] for row in body["items"]] == ["critical", "important", "important"]
    assert body["total_unfiltered"] == 7, "what the operator would see with the filter off"
    assert body["severity_counts"] == {"critical": 1, "important": 2, "unknown": 3, "low": 1}
    assert sum(body["severity_counts"].values()) == body["total_unfiltered"]


async def test_updates_rollup_severity_histogram_respects_the_other_filters(api_env) -> None:
    """`severity_counts` is what the console's "292 more are outside this
    filter" line is built from, so it has to be true IN CONTEXT. An OS filter
    that narrowed the table and not the histogram would offer to widen the
    severity filter onto rows the OS filter is still hiding."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    win = await _device(api_env, org_id, "win-a", os_family="windows")
    lin = await _device(api_env, org_id, "lin-a", os_family="linux")
    win_patch = await _patch(api_env, org_id, "KB-WIN", severity="unknown")
    lin_patch = await _patch(
        api_env, org_id, "pkg-lin", severity="unknown", os_family="linux", source="apt"
    )
    await _status(api_env, org_id, win, win_patch, "missing")
    await _status(api_env, org_id, lin, lin_patch, "missing")

    body = (
        await api_env.client.get("/api/v1/patch/updates?os_family=linux", headers=headers)
    ).json()
    assert body["severity_counts"] == {"unknown": 1}
    assert body["total_unfiltered"] == 1


async def test_updates_rollup_pages_instead_of_returning_the_whole_catalogue(
    api_env,
) -> None:
    """A 300-package Ubuntu box is 300 patch rows even after the aggregation --
    aggregation collapses the (device, patch) explosion, not the catalogue. So
    the route pages, and `total` states the size of the set the page came from.
    """
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a")
    for index in range(30):
        patch_id = await _patch(api_env, org_id, f"KB{index:04d}", severity="critical")
        await _status(api_env, org_id, device_id, patch_id, "missing")

    first = (await api_env.client.get("/api/v1/patch/updates", headers=headers)).json()
    assert len(first["items"]) == 25, "the default page, not the whole set"
    assert first["total"] == 30

    second = (
        await api_env.client.get("/api/v1/patch/updates?offset=25", headers=headers)
    ).json()
    assert len(second["items"]) == 5
    assert second["total"] == 30
    assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}


async def test_updates_rollup_orders_severest_first_then_by_reach(api_env) -> None:
    """The default order is the reading order: what is severest, and among
    equals what is on the most machines. An alphabetical list of 300 packages
    is the long list with extra steps."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    a = await _device(api_env, org_id, "wks-a")
    b = await _device(api_env, org_id, "wks-b")
    low = await _patch(api_env, org_id, "KB-LOW", severity="low")
    narrow = await _patch(api_env, org_id, "KB-CRIT-NARROW", severity="critical")
    wide = await _patch(api_env, org_id, "KB-CRIT-WIDE", severity="critical")
    await _status(api_env, org_id, a, low, "missing")
    await _status(api_env, org_id, b, low, "missing")
    await _status(api_env, org_id, a, narrow, "missing")
    await _status(api_env, org_id, a, wide, "missing")
    await _status(api_env, org_id, b, wide, "missing")

    body = (
        await api_env.client.get("/api/v1/patch/updates?severity=", headers=headers)
    ).json()
    assert [row["identifier"] for row in body["items"]] == [
        "KB-CRIT-WIDE",
        "KB-CRIT-NARROW",
        "KB-LOW",
    ]


async def test_updates_rollup_scoped_to_one_device_is_that_devices_own_list(
    api_env,
) -> None:
    """ONE ROUTE, THREE SURFACES. The device rollup's expansion and the device
    detail page's Patch tab are this same query with one more predicate, which
    is why neither needs a browser-side grouping of a fleet-wide page."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    a = await _device(api_env, org_id, "wks-a")
    b = await _device(api_env, org_id, "wks-b")
    mine = await _patch(api_env, org_id, "KB-MINE")
    theirs = await _patch(api_env, org_id, "KB-THEIRS")
    await _status(api_env, org_id, a, mine, "missing")
    await _status(api_env, org_id, b, theirs, "missing")

    body = (
        await api_env.client.get(
            f"/api/v1/patch/updates?device_id={a}&severity=", headers=headers
        )
    ).json()
    assert [row["identifier"] for row in body["items"]] == ["KB-MINE"]
    assert body["total"] == 1
    assert body["items"][0]["devices_missing"] == 1


async def test_updates_rollup_carries_the_rings_a_patch_is_already_cleared_for(
    api_env,
) -> None:
    """THE COLUMN THAT USED TO UNDER-REPORT. The console derived "Cleared for"
    from a capped `GET /approvals` page and had to print a disclaimer saying an
    older approval might be missing from it. Here it is a property of the row.

    A REVOKED approval is not a clearance. The Approvals panel is where the
    withdrawn ones stay on the record; this column answers what is in force.
    """
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a")
    patch_id = await _patch(api_env, org_id, "KB-CLEARED")
    await _status(api_env, org_id, device_id, patch_id, "missing")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(PatchApproval(org_id=org_id, patch_id=patch_id, ring_name="pilot"))
        s.add(PatchApproval(org_id=org_id, patch_id=patch_id, ring_name="test"))
        s.add(
            PatchApproval(
                org_id=org_id, patch_id=patch_id, ring_name="broad", revoked=True
            )
        )
        await s.commit()

    body = (await api_env.client.get("/api/v1/patch/updates", headers=headers)).json()
    assert body["items"][0]["cleared_rings"] == ["test", "pilot"], (
        "rollout order, and the revoked one is not a clearance"
    )


async def test_updates_rollup_carries_the_provenance_of_its_own_zero(api_env) -> None:
    """An empty list is the same shape on a clean fleet and on one nothing has
    scanned. Same three fields `GET /report` carries, for the same reason."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a")

    before = (await api_env.client.get("/api/v1/patch/updates", headers=headers)).json()
    assert before["items"] == []
    assert before["last_scan_at"] is None
    assert (before["devices_scanned"], before["devices_total"]) == (0, 1)

    when = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    await _succeeded_scan(api_env, org_id, device_id, when)
    after = (await api_env.client.get("/api/v1/patch/updates", headers=headers)).json()
    assert datetime.fromisoformat(after["last_scan_at"]) == when
    assert (after["devices_scanned"], after["devices_total"]) == (1, 1)


async def test_updates_rollup_hides_a_fully_installed_patch_by_default(api_env) -> None:
    """The working list is what is OUTSTANDING. An update every machine already
    has is catalogue history, and `outstanding_only=false` is where it lives."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a")
    done = await _patch(api_env, org_id, "KB-DONE")
    todo = await _patch(api_env, org_id, "KB-TODO")
    await _status(api_env, org_id, device_id, done, "installed")
    await _status(api_env, org_id, device_id, todo, "missing")

    default = (await api_env.client.get("/api/v1/patch/updates", headers=headers)).json()
    assert [row["identifier"] for row in default["items"]] == ["KB-TODO"]

    everything = (
        await api_env.client.get(
            "/api/v1/patch/updates?outstanding_only=false", headers=headers
        )
    ).json()
    assert {row["identifier"] for row in everything["items"]} == {"KB-DONE", "KB-TODO"}


async def test_updates_rollup_searches_identifier_and_title_server_side(api_env) -> None:
    """The old page filtered the FETCHED rows, so a search could not reach past
    the 500-row cap and answered "No patches match that filter" about updates
    it had never asked for."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a")
    for identifier, title in (
        ("KB5031354", "2023-10 Cumulative Update"),
        ("KB5031355", "Defender definition update"),
    ):
        patch_id = await _patch(api_env, org_id, identifier, title=title)
        await _status(api_env, org_id, device_id, patch_id, "missing")

    by_title = (
        await api_env.client.get("/api/v1/patch/updates?q=cumulative", headers=headers)
    ).json()
    assert [row["identifier"] for row in by_title["items"]] == ["KB5031354"]

    by_identifier = (
        await api_env.client.get("/api/v1/patch/updates?q=5031355", headers=headers)
    ).json()
    assert [row["identifier"] for row in by_identifier["items"]] == ["KB5031355"]


async def test_updates_rollup_requires_patch_read(api_env) -> None:
    org_id = await api_env.create_org("patch-rollup")
    nobody = await api_env.create_user(org_id)
    resp = await api_env.client.get(
        "/api/v1/patch/updates", headers=await api_env.headers(org_id, nobody)
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# GET /patch/device-rollup -- one row per DEVICE
# --------------------------------------------------------------------------- #


async def test_device_rollup_counts_devices_not_findings(api_env) -> None:
    """THE 2026-08-20 DEFECT, IN ITS OTHER DIRECTION. "it thinks there are 66
    devices, it is just 1 device with 66 pending updates". One device with
    three outstanding patches is ONE row saying three, and `total` is 1."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a")
    for index in range(3):
        patch_id = await _patch(api_env, org_id, f"KB{index}", severity="important")
        await _status(api_env, org_id, device_id, patch_id, "missing")

    body = (await api_env.client.get("/api/v1/patch/device-rollup", headers=headers)).json()
    assert body["total"] == 1
    assert body["devices_total"] == 1
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["hostname"] == "wks-a"
    assert row["outstanding"] == 3
    assert row["worst_severity"] == "important"
    assert row["counts_by_severity"]["important"] == 3


async def test_device_rollup_orders_worst_first(api_env) -> None:
    """A list of machines is only useful if the one that needs a person is at
    the top. Severest first; among equals, the one carrying the most."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    quiet = await _device(api_env, org_id, "quiet")
    loud = await _device(api_env, org_id, "loud")
    urgent = await _device(api_env, org_id, "urgent")
    low = await _patch(api_env, org_id, "KB-LOW", severity="low")
    critical = await _patch(api_env, org_id, "KB-CRIT", severity="critical")
    other_low = await _patch(api_env, org_id, "KB-LOW-2", severity="low")
    await _status(api_env, org_id, quiet, low, "missing")
    await _status(api_env, org_id, loud, low, "missing")
    await _status(api_env, org_id, loud, other_low, "missing")
    await _status(api_env, org_id, urgent, critical, "missing")

    body = (await api_env.client.get("/api/v1/patch/device-rollup", headers=headers)).json()
    assert [row["hostname"] for row in body["items"]] == ["urgent", "loud", "quiet"]
    assert [row["worst_severity"] for row in body["items"]] == ["critical", "low", "low"]


async def test_device_rollup_leaves_out_a_device_with_nothing_outstanding(
    api_env,
) -> None:
    """`total` is machines that need something; `devices_total` is the fleet.
    A count of 1 says nothing about whether that is the whole estate."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    exposed = await _device(api_env, org_id, "exposed")
    await _device(api_env, org_id, "clean")
    patch_id = await _patch(api_env, org_id, "KB-ONE")
    await _status(api_env, org_id, exposed, patch_id, "missing")

    body = (await api_env.client.get("/api/v1/patch/device-rollup", headers=headers)).json()
    assert [row["hostname"] for row in body["items"]] == ["exposed"]
    assert body["total"] == 1
    assert body["devices_total"] == 2


async def test_device_rollup_carries_each_devices_own_last_scan(api_env) -> None:
    """PER DEVICE, not the fleet maximum. A row reading "0 outstanding" on a
    machine that has never answered is not the same fact as one on a machine
    scanned ten minutes ago, and a fleet-wide maximum would date both."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    scanned = await _device(api_env, org_id, "scanned")
    never = await _device(api_env, org_id, "never")
    patch_id = await _patch(api_env, org_id, "KB-ONE")
    await _status(api_env, org_id, scanned, patch_id, "missing")
    await _status(api_env, org_id, never, patch_id, "missing")
    when = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    await _succeeded_scan(api_env, org_id, scanned, when)

    body = (await api_env.client.get("/api/v1/patch/device-rollup", headers=headers)).json()
    rows = {row["hostname"]: row for row in body["items"]}
    assert datetime.fromisoformat(rows["scanned"]["last_scan_at"]) == when
    assert rows["never"]["last_scan_at"] is None


async def test_device_rollup_pages_and_requires_patch_read(api_env) -> None:
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    patch_id = await _patch(api_env, org_id, "KB-ONE")
    for index in range(4):
        device_id = await _device(api_env, org_id, f"wks-{index:02d}")
        await _status(api_env, org_id, device_id, patch_id, "missing")

    page = (
        await api_env.client.get(
            "/api/v1/patch/device-rollup?limit=2&offset=2", headers=headers
        )
    ).json()
    assert len(page["items"]) == 2
    assert page["total"] == 4

    nobody = await api_env.create_user(org_id)
    resp = await api_env.client.get(
        "/api/v1/patch/device-rollup", headers=await api_env.headers(org_id, nobody)
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Tenancy -- on the BYPASSRLS session, where the predicate is the only scoping
# --------------------------------------------------------------------------- #


async def test_the_rollup_org_predicates_hold_where_rls_is_not_rejecting(
    api_env, bypassrls_session: AsyncSession
) -> None:
    """THE TENANCY TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    Every route above runs `org_session` on the NOBYPASSRLS `app_user` role,
    where `tenant_isolation` removes the neighbour's rows before any
    application predicate is consulted -- so deleting `WHERE org_id` from these
    aggregates turns none of them red. `its_admin` carries BYPASSRLS
    (`tests/conftest.py`), so here the predicate is the only scoping there is.

    AGGREGATES ARE THE WORST PLACE FOR A MISSING PREDICATE. A leaked ROW is
    visible; a leaked row inside `count(distinct device_id)` is a plausible
    number. "41 of your devices are missing this" would silently be counting
    another tenant's estate, and nothing on the screen would look wrong.

    FALSIFIED: dropping `DevicePatchStatus.org_id == org_id` from
    `update_rollup` puts org B's device inside org A's `devices_missing`;
    dropping `Device.org_id == org_id` from `device_rollup` puts org B's
    machine in org A's list.
    """
    org_a = await api_env.create_org("rollup-pred-a")
    org_b = await api_env.create_org("rollup-pred-b")
    device_a = await _device(api_env, org_a, "a-01")
    device_b = await _device(api_env, org_b, "b-01")
    # Same identifier in both tenants: the catalogue is per-org, so a shared
    # KB number is the ordinary case rather than a contrivance.
    patch_a = await _patch(api_env, org_a, "KB-SHARED")
    patch_b = await _patch(api_env, org_b, "KB-SHARED")
    await _status(api_env, org_a, device_a, patch_a, "missing")
    await _status(api_env, org_b, device_b, patch_b, "missing")

    page_a = await update_rollup(bypassrls_session, org_a)
    assert [row["id"] for row in page_a["items"]] == [patch_a]
    assert page_a["items"][0]["devices_missing"] == 1, "org B's device is inside the count"
    assert page_a["total"] == 1
    assert page_a["severity_counts"] == {"critical": 1}
    assert page_a["devices_total"] == 1, "the fleet size is org A's fleet"

    devices_a = await device_rollup(bypassrls_session, org_a)
    assert [row["hostname"] for row in devices_a["items"]] == ["a-01"]
    assert devices_a["total"] == 1
    assert devices_a["devices_total"] == 1

    # Non-vacuity: org B sees its own, so the predicates are scoping rather
    # than simply refusing everything.
    page_b = await update_rollup(bypassrls_session, org_b)
    assert [row["id"] for row in page_b["items"]] == [patch_b]
    devices_b = await device_rollup(bypassrls_session, org_b)
    assert [row["hostname"] for row in devices_b["items"]] == ["b-01"]


async def test_the_cleared_rings_predicate_holds_where_rls_is_not_rejecting(
    api_env, bypassrls_session: AsyncSession
) -> None:
    """`cleared_rings` is read through a second query keyed on patch ids, and a
    patch id is a uuid7 -- unguessable, which is exactly the argument that
    makes people leave the org predicate off. An approval is the gate that lets
    a patch reach a machine: showing org A that an update is "cleared for
    broad" because org B cleared it would be a clearance nobody in org A
    granted.

    FALSIFIED: dropping `PatchApproval.org_id == org_id` from
    `_cleared_rings_by_patch` puts `broad` on org A's row.
    """
    org_a = await api_env.create_org("rings-pred-a")
    org_b = await api_env.create_org("rings-pred-b")
    device_a = await _device(api_env, org_a, "a-01")
    patch_a = await _patch(api_env, org_a, "KB-SHARED")
    await _status(api_env, org_a, device_a, patch_a, "missing")
    # Org B approves org A's patch id. Impossible over HTTP -- `approve_patch`
    # refuses a patch that is not the caller's -- and written straight in here
    # precisely so the READ path is tested rather than the write path's guard.
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(PatchApproval(org_id=org_b, patch_id=patch_a, ring_name="broad"))
        await s.commit()

    page_a = await update_rollup(bypassrls_session, org_a)
    assert page_a["items"][0]["cleared_rings"] == [], (
        "org B's approval is showing as org A's clearance"
    )

    # Non-vacuity: org A's own approval of the same patch does appear.
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(PatchApproval(org_id=org_a, patch_id=patch_a, ring_name="test"))
        await s.commit()
    again = await update_rollup(bypassrls_session, org_a)
    assert again["items"][0]["cleared_rings"] == ["test"]


async def test_cleared_rings_are_in_rollout_order_the_policies_declare(
    api_env, bypassrls_session: AsyncSession
) -> None:
    """test -> pilot -> broad is a rollout, not an alphabet, and a list sorted
    any other way invites the reader to reach for the widest ring first. The
    order comes from `patch_policies.ring_order`, the same column `GET /rings`
    orders by; a ring no policy names sorts last rather than being dropped."""
    org_id = await api_env.create_org("rings-order")
    device_id = await _device(api_env, org_id, "a-01")
    patch_id = await _patch(api_env, org_id, "KB-ORDER")
    await _status(api_env, org_id, device_id, patch_id, "missing")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        group = DeviceGroup(org_id=org_id, name="pilot-group")
        s.add(group)
        await s.flush()
        s.add(PatchPolicy(org_id=org_id, device_group_id=None, ring_name="broad", ring_order=100))
        s.add(
            PatchPolicy(
                org_id=org_id, device_group_id=group.id, ring_name="pilot", ring_order=50
            )
        )
        for ring in ("broad", "pilot", "aardvark"):
            s.add(PatchApproval(org_id=org_id, patch_id=patch_id, ring_name=ring))
        await s.commit()

    page = await update_rollup(bypassrls_session, org_id)
    assert page["items"][0]["cleared_rings"] == ["pilot", "broad", "aardvark"]


# --------------------------------------------------------------------------- #
# THE APPLY OUTCOME -- did the patches the operator dispatched actually install?
#
# The module could not answer that on its own page. `DevicePatchStatus` has
# carried `state`, `applied_at` and `last_error` since it shipped, and
# `patch_runs` has been written on every dispatch and finalized on every
# result, but: the fleet rollup's per-device query returned only fleet-wide
# counts, the device rollup returned only an `outstanding` total that folds a
# failed apply into a patch nobody ever tried, `last_error` was reachable
# ONLY through the CSV export, and NO route read `patch_runs` at all. An
# operator who applied updates on one machine had no reading on the patch page
# that distinguished success from failure.
#
# Each test below fails if the field it names is dropped.
# --------------------------------------------------------------------------- #


async def _failed_status(
    api_env, org_id: UUID, device_id: UUID, patch_id: UUID, error: str
) -> None:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(
            DevicePatchStatus(
                org_id=org_id,
                device_id=device_id,
                patch_id=patch_id,
                state="failed",
                last_error=error,
            )
        )
        await s.commit()


async def test_device_scoped_updates_carry_that_devices_own_state_and_error(
    api_env,
) -> None:
    """WHY a patch is still outstanding, on the row that says it is.

    `failed` and `missing` are both "this machine does not have it", so an
    apply that errored rendered identically to one that was never dispatched.
    Scoped to a device, the row states that device's own state and the agent's
    own error text."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a")
    tried = await _patch(api_env, org_id, "KB-TRIED")
    never = await _patch(api_env, org_id, "KB-NEVER")
    await _failed_status(api_env, org_id, device_id, tried, "0x80070422 service disabled")
    await _status(api_env, org_id, device_id, never, "missing")

    body = (
        await api_env.client.get(
            f"/api/v1/patch/updates?device_id={device_id}", headers=headers
        )
    ).json()
    rows = {row["identifier"]: row for row in body["items"]}

    assert rows["KB-TRIED"]["device_state"] == "failed"
    assert rows["KB-TRIED"]["device_last_error"] == "0x80070422 service disabled"
    assert rows["KB-NEVER"]["device_state"] == "missing"
    assert rows["KB-NEVER"]["device_last_error"] is None


async def test_fleet_scoped_updates_do_not_claim_one_devices_outcome(api_env) -> None:
    """The same three fields are NULL fleet-wide, deliberately.

    Across machines with different outcomes they would be an aggregate that
    states nothing about any of them -- and a state string on a fleet row reads
    as a verdict on the fleet."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    a = await _device(api_env, org_id, "wks-a")
    b = await _device(api_env, org_id, "wks-b")
    shared = await _patch(api_env, org_id, "KB-SHARED")
    await _failed_status(api_env, org_id, a, shared, "disk full")
    await _status(api_env, org_id, b, shared, "missing")

    body = (await api_env.client.get("/api/v1/patch/updates", headers=headers)).json()
    row = next(r for r in body["items"] if r["identifier"] == "KB-SHARED")
    assert row["device_state"] is None
    assert row["device_last_error"] is None
    assert row["device_applied_at"] is None
    # The fleet counts still separate them; only the per-device fields are held.
    assert row["devices_failed"] == 1
    assert row["devices_missing"] == 1


async def test_device_rollup_separates_tried_and_failed_from_never_attempted(
    api_env,
) -> None:
    """`outstanding` counts a failed patch -- correctly, the machine still
    lacks it -- which is exactly why the failures need their own number. A row
    reading "3 outstanding" is a different call to action when two of them are
    errors from a run that already happened."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a")
    for index in range(2):
        patch_id = await _patch(api_env, org_id, f"KB-FAIL{index}")
        await _failed_status(api_env, org_id, device_id, patch_id, "install error")
    await _status(api_env, org_id, device_id, await _patch(api_env, org_id, "KB-NEW"), "missing")

    body = (await api_env.client.get("/api/v1/patch/device-rollup", headers=headers)).json()
    row = body["items"][0]
    assert row["outstanding"] == 3
    assert row["failed"] == 2


# --------------------------------------------------------------------------- #
# GET /patch/runs -- the table that was written on every apply and read by
# nothing. `DevicePatchStatus` holds the durable per-patch end state; this
# holds the EVENT, which is what an operator who just pressed a button is
# asking about. A patch that failed on Tuesday and installed on Wednesday
# reads as `installed`, with nothing on the page saying Tuesday happened.
# --------------------------------------------------------------------------- #


async def _run(
    api_env,
    org_id: UUID,
    device_id: UUID,
    *,
    results: dict | None = None,
    reboot_required: bool = False,
    finished: bool = True,
) -> UUID:
    when = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        job = Job(
            org_id=org_id,
            type="patch.apply",
            target_type="device",
            target_id=device_id,
            params={},
            nonce=f"n-{uuid4().hex[:8]}",
            issued_at=when,
            expires_at=when,
            status=JobStatus.succeeded if finished else JobStatus.dispatched,
        )
        s.add(job)
        await s.flush()
        run = PatchRun(
            org_id=org_id,
            job_id=job.id,
            device_id=device_id,
            results=results or {},
            reboot_required=reboot_required,
            finished_at=when if finished else None,
        )
        s.add(run)
        await s.commit()
        return run.id


async def test_runs_report_what_the_last_apply_installed_and_what_it_did_not(
    api_env,
) -> None:
    """THE ANSWER TO "DID IT WORK?". The counts come off the agent's own
    result document, and every failure is NAMED with its error -- a count on
    its own only tells the operator to go looking somewhere else, which is
    where this module left them (the CSV export)."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a")
    await _run(
        api_env,
        org_id,
        device_id,
        reboot_required=True,
        results={
            "results": [
                {"source": "windows_update", "identifier": "KB1", "state": "installed"},
                {"source": "windows_update", "identifier": "KB2", "state": "installed"},
                {
                    "source": "windows_update",
                    "identifier": "KB3",
                    "state": "failed",
                    "error": "0x8024402c",
                },
            ],
            "reboot_required": True,
            "applied_at": datetime.now(UTC).isoformat(),
        },
    )

    resp = await api_env.client.get(
        f"/api/v1/patch/runs?device_id={device_id}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()["items"][0]
    assert row["installed"] == 2
    assert row["failed"] == 1
    assert row["reboot_required"] is True
    assert row["finished_at"] is not None
    assert row["failures"] == [
        {"source": "windows_update", "identifier": "KB3", "error": "0x8024402c"}
    ]


async def test_a_run_still_out_is_distinguishable_from_one_that_installed_nothing(
    api_env,
) -> None:
    """Both report zero installed. `finished_at is None` is the only thing that
    separates "the agent has not answered" from "the agent answered and
    nothing took", and they mean opposite things to the person waiting."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    device_id = await _device(api_env, org_id, "wks-a")
    await _run(api_env, org_id, device_id, finished=False)

    row = (
        await api_env.client.get(
            f"/api/v1/patch/runs?device_id={device_id}", headers=headers
        )
    ).json()["items"][0]
    assert row["finished_at"] is None
    assert row["installed"] == 0
    assert row["failed"] == 0


async def test_runs_are_newest_first_and_scoped_to_the_device_asked_for(api_env) -> None:
    """An in-flight run has no `finished_at`, so ordering by it would sort the
    one the operator is waiting on to the BOTTOM. Ordered by the uuid7 id."""
    org_id = await api_env.create_org("patch-rollup")
    headers = await api_env.headers(org_id, await api_env.create_user(org_id, "auditor"))
    a = await _device(api_env, org_id, "wks-a")
    b = await _device(api_env, org_id, "wks-b")
    first = await _run(api_env, org_id, a)
    second = await _run(api_env, org_id, a, finished=False)
    await _run(api_env, org_id, b)

    items = (
        await api_env.client.get(f"/api/v1/patch/runs?device_id={a}", headers=headers)
    ).json()["items"]
    assert [row["id"] for row in items] == [str(second), str(first)]


async def test_runs_requires_patch_read(api_env) -> None:
    """Same gate as every other read on this module."""
    org_id = await api_env.create_org("patch-rollup")
    nobody = await api_env.create_user(org_id)
    resp = await api_env.client.get(
        "/api/v1/patch/runs", headers=await api_env.headers(org_id, nobody)
    )
    assert resp.status_code == 403, resp.text
