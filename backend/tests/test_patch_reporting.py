"""Tests for per-device + fleet compliance reporting and evidence export
(docs/modules/patch/spec.md). Mirrors `tests/test_patch_result.py`'s
org/device/patch fixture style. `reporting.py` is pure read-only aggregation
— these tests seed mixed missing/installed/failed/approved/not_applicable
statuses across severities and both os_families, then hand-compute the
expected compliance math.

Compliance definition under test (see `app/modules/patch/reporting.py`):
`installed` and `not_applicable` are COMPLIANT; `missing`, `approved`, and
`failed` are NON-COMPLIANT.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, DevicePatchStatus, Job, JobStatus, Organization, Patch, PatchRun
from app.modules.patch.reporting import device_report, export_rows, fleet_report


async def _seed(admin_engine: AsyncEngine, slug: str) -> dict[str, UUID]:
    """Seeds one org with:

    - d1 (windows): p1(critical)=missing, p2(important)=installed,
      p3(moderate)=failed  -> d1 is device-non-compliant (has outstanding).
    - d2 (windows): p1(critical)=installed, p2(important)=not_applicable
      -> d2 is device-compliant (zero outstanding).
    - d3 (linux): p4(critical)=approved -> d3 is device-non-compliant.

    Plus two PatchRun rows on d1 (an older one with reboot_required=False and
    a newer one with reboot_required=True) to exercise "latest run wins".

    Returns ids keyed by name for the test to reference.
    """
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        d1 = Device(org_id=org.id, hostname="d1-win", os_family="windows")
        d2 = Device(org_id=org.id, hostname="d2-win", os_family="windows")
        d3 = Device(org_id=org.id, hostname="d3-lin", os_family="linux")
        session.add_all([d1, d2, d3])
        await session.flush()

        p1 = Patch(
            org_id=org.id,
            os_family="windows",
            source="windows_update",
            identifier="KB-CRIT",
            title="Critical Update",
            severity="critical",
            priority=100,
        )
        p2 = Patch(
            org_id=org.id,
            os_family="windows",
            source="windows_update",
            identifier="KB-IMP",
            title="Important Update",
            severity="important",
            priority=None,
        )
        p3 = Patch(
            org_id=org.id,
            os_family="windows",
            source="windows_update",
            identifier="KB-MOD",
            title="Moderate Update",
            severity="moderate",
            priority=50,
        )
        p4 = Patch(
            org_id=org.id,
            os_family="linux",
            source="apt",
            identifier="pkg-crit",
            title="Critical Package",
            severity="critical",
            priority=None,
        )
        session.add_all([p1, p2, p3, p4])
        await session.flush()

        session.add_all(
            [
                DevicePatchStatus(org_id=org.id, device_id=d1.id, patch_id=p1.id, state="missing"),
                DevicePatchStatus(
                    org_id=org.id, device_id=d1.id, patch_id=p2.id, state="installed"
                ),
                DevicePatchStatus(org_id=org.id, device_id=d1.id, patch_id=p3.id, state="failed"),
                DevicePatchStatus(
                    org_id=org.id, device_id=d2.id, patch_id=p1.id, state="installed"
                ),
                DevicePatchStatus(
                    org_id=org.id, device_id=d2.id, patch_id=p2.id, state="not_applicable"
                ),
                DevicePatchStatus(org_id=org.id, device_id=d3.id, patch_id=p4.id, state="approved"),
            ]
        )

        job1 = Job(
            org_id=org.id,
            type="patch.apply",
            target_type="device",
            target_id=d1.id,
            params={},
            nonce="n1",
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
        )
        job2 = Job(
            org_id=org.id,
            type="patch.apply",
            target_type="device",
            target_id=d1.id,
            params={},
            nonce="n2",
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
        )
        session.add_all([job1, job2])
        await session.flush()

        now = datetime.now(UTC)
        run_old = PatchRun(
            org_id=org.id,
            job_id=job1.id,
            device_id=d1.id,
            reboot_required=False,
            created_at=now - timedelta(hours=1),
        )
        run_new = PatchRun(
            org_id=org.id,
            job_id=job2.id,
            device_id=d1.id,
            reboot_required=True,
            created_at=now,
        )
        session.add_all([run_old, run_new])

        await session.commit()

        return {
            "org_id": org.id,
            "d1": d1.id,
            "d2": d2.id,
            "d3": d3.id,
            "p1": p1.id,
            "p2": p2.id,
            "p3": p3.id,
            "p4": p4.id,
        }


async def test_device_report_counts_by_state_and_severity_and_pending_reboot(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed(admin_engine, "rpt-dev")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await device_report(session, ids["org_id"], ids["d1"])

        assert report["counts_by_state"] == {
            "missing": 1,
            "approved": 0,
            "installed": 1,
            "failed": 1,
            "not_applicable": 0,
        }
        # Outstanding (non-compliant) patches on d1: p1(critical)=missing,
        # p3(moderate)=failed. p2 is installed -> compliant -> excluded.
        assert report["counts_by_severity"] == {
            "critical": 1,
            "important": 0,
            "moderate": 1,
            "low": 0,
            "unknown": 0,
        }
        # Latest PatchRun (run_new, created most recently) has reboot_required=True.
        assert report["pending_reboot"] is True


async def test_device_report_compliant_device_has_no_outstanding_severity(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed(admin_engine, "rpt-dev2")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await device_report(session, ids["org_id"], ids["d2"])

        assert report["counts_by_state"] == {
            "missing": 0,
            "approved": 0,
            "installed": 1,
            "not_applicable": 1,
            "failed": 0,
        }
        assert report["counts_by_severity"] == {
            "critical": 0,
            "important": 0,
            "moderate": 0,
            "low": 0,
            "unknown": 0,
        }
        assert report["pending_reboot"] is False  # d2 has no PatchRun rows at all


async def test_fleet_report_compliant_pct_math(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed(admin_engine, "rpt-fleet")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_report(session, ids["org_id"])

        # windows: d1 non-compliant, d2 compliant -> 1/2 = 50.0%
        # linux: d3 non-compliant -> 0/1 = 0.0%
        assert report["compliant_pct_by_os_family"] == pytest.approx(
            {"windows": 50.0, "linux": 0.0}
        )

        # critical rows: d1/p1 missing (non-compliant), d2/p1 installed
        # (compliant), d3/p4 approved (non-compliant) -> 1/3 compliant.
        # important rows: d1/p2 installed, d2/p2 not_applicable -> 2/2 compliant.
        # moderate rows: d1/p3 failed -> 0/1 compliant.
        expected_severity_pct = {
            "critical": (1 / 3) * 100,
            "important": 100.0,
            "moderate": 0.0,
        }
        assert report["compliant_pct_by_severity"] == pytest.approx(expected_severity_pct)

        # Exposed (non-compliant) rows: d1/p1(critical, prio=100),
        # d1/p3(moderate, prio=50), d3/p4(critical, prio=None).
        # Order: priority DESC NULLS LAST, then severity (critical first).
        # -> p1(prio=100) has None (None sorts... > d1/p1(100), then d1/p3(50),
        # then d3/p4(None, nulls last)).
        exposed = report["exposures"]
        assert [(e["hostname"], e["identifier"]) for e in exposed] == [
            ("d1-win", "KB-CRIT"),
            ("d1-win", "KB-MOD"),
            ("d3-lin", "pkg-crit"),
        ]
        assert exposed[0]["severity"] == "critical"
        assert exposed[0]["state"] == "missing"
        assert exposed[2]["priority"] is None


async def test_export_rows_one_row_per_device_patch_pair(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed(admin_engine, "rpt-export")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        rows = await export_rows(session, ids["org_id"])

        assert len(rows) == 6  # one row per (device, patch) status pair seeded
        # Deterministic ordering: hostname then identifier.
        assert [(r["hostname"], r["identifier"]) for r in rows] == [
            ("d1-win", "KB-CRIT"),
            ("d1-win", "KB-IMP"),
            ("d1-win", "KB-MOD"),
            ("d2-win", "KB-CRIT"),
            ("d2-win", "KB-IMP"),
            ("d3-lin", "pkg-crit"),
        ]
        first = rows[0]
        assert first["hostname"] == "d1-win"
        assert first["identifier"] == "KB-CRIT"
        assert first["severity"] == "critical"
        assert first["state"] == "missing"


async def test_a_second_orgs_rows_never_appear_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids_a = await _seed(admin_engine, "rpt-tenant-a")
    ids_b = await _seed(admin_engine, "rpt-tenant-b")

    async with org_session(
        ids_a["org_id"], engine=app_engine, clients=ClientScope.all()
    ) as session:
        rows = await export_rows(session, ids_a["org_id"])
        assert all(r["device_id"] in {ids_a["d1"], ids_a["d2"], ids_a["d3"]} for r in rows)

        fleet = await fleet_report(session, ids_a["org_id"])
        exposed_device_ids = {e["device_id"] for e in fleet["exposures"]}
        assert ids_b["d1"] not in exposed_device_ids
        assert ids_b["d3"] not in exposed_device_ids

        # Fleet counts are org_a-only: d1(3 patches) + d2(2 patches) = 5 windows
        # rows, never org_b's rows even though org_b was seeded identically.
        assert len([r for r in rows if r["os_family"] == "windows"]) == 5

    async with org_session(
        ids_b["org_id"], engine=app_engine, clients=ClientScope.all()
    ) as session:
        report = await device_report(session, ids_b["org_id"], ids_a["d1"])
        # device_id belongs to org_a; org_b's RLS-scoped session must not see it.
        assert report["counts_by_state"] == {
            "missing": 0,
            "approved": 0,
            "installed": 0,
            "failed": 0,
            "not_applicable": 0,
        }


async def test_the_fleet_report_counts_devices_and_findings_separately(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """REPORTED FROM THE LIVE DEPLOYMENT: "it thinks there are 66 devices, it is
    just 1 device with 66 pending updates".

    The field was named `exposed_devices` and documented, correctly, as one row
    per non-compliant (device, patch) pair -- a findings list wearing a device
    list's name. The console then rendered `exposed_devices.length` under the
    label "Exposed devices", so a single machine with 66 missing packages was
    reported as 66 exposed machines, and "Patches in catalog" and "Exposed
    devices" showed the identical number on a one-device fleet. Nothing was
    wrong with the query; the name was wrong, and the name was believed.

    The counts are computed HERE rather than derived in the browser, because a
    count taken from a list is only correct while the list is complete. This
    endpoint returns every exposure today; the first time a fleet needs that
    capped, a browser-side `new Set(rows).size` starts under-reporting silently
    while still looking like arithmetic.

    This fixture discriminates: THREE findings across TWO devices, so counting
    rows and counting devices cannot both be right.
    """
    ids = await _seed(admin_engine, "rpt-counts")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_report(session, ids["org_id"])

        assert len(report["exposures"]) == 3, "three (device, patch) findings"
        assert report["exposed_device_count"] == 2, "across two distinct devices"

        # Critical findings: d1/p1 and d3/p4 -- two rows, two devices. Seeded
        # so a future change that makes them share a device fails loudly here
        # rather than making this assertion vacuous.
        assert report["critical_device_count"] == 2
        assert len({e["device_id"] for e in report["exposures"]}) == 2


async def test_a_device_with_many_findings_is_still_one_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The operator's case exactly, and the one the seed above cannot make on
    its own: ONE machine, MANY missing patches. Row-counting reports the
    patch count; device-counting reports one."""
    ids = await _seed(admin_engine, "rpt-one-device")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_report(session, ids["org_id"])
        only = [e for e in report["exposures"] if e["hostname"] == "d1-win"]

        assert len(only) == 2, "d1-win carries two findings in this fixture"
        assert len({e["device_id"] for e in only}) == 1


# --------------------------------------------------------------------------- #
# last_scan_at -- "nothing outstanding" and "nothing ever measured" are not
# the same fact (audit 2026-08-21, recommendation 5).
# --------------------------------------------------------------------------- #


async def _scan_job(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    *,
    nonce: str,
    status: JobStatus,
    finished_at: datetime | None,
) -> UUID:
    """A `patch.scan` job row in a terminal state. This is the ONLY durable
    record that a scan happened -- there is no `devices.last_scan_at` column
    and none is added for this, see `reporting.last_successful_scan_at`."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        job = Job(
            org_id=org_id,
            type="patch.scan",
            target_type="device",
            target_id=device_id,
            params={},
            nonce=nonce,
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status=status,
            finished_at=finished_at,
        )
        session.add(job)
        await session.commit()
        return job.id


async def test_fleet_report_reports_never_scanned_rather_than_a_clean_zero(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The defect this closes: a console tile printing 0 cannot tell a clean
    fleet from an unmeasured one. With no scan ever completed, the report says
    so in three ways at once -- no timestamp, no scanned device, and a device
    total that proves the fleet is not empty either."""
    ids = await _seed(admin_engine, "rpt-neverscanned")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_report(session, ids["org_id"])

        assert report["last_scan_at"] is None
        assert report["devices_scanned"] == 0
        assert report["devices_total"] == 3


async def test_fleet_report_last_scan_at_is_the_most_recent_scan_that_succeeded(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A scan that FAILED measured nothing, so it must not move the clock --
    otherwise the freshest number on the page is produced by the machine that
    could not answer. The failed job here is the newest of the three."""
    ids = await _seed(admin_engine, "rpt-lastscan")
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=5)
    await _scan_job(
        admin_engine,
        ids["org_id"],
        ids["d1"],
        nonce="s1",
        status=JobStatus.succeeded,
        finished_at=base,
    )
    await _scan_job(
        admin_engine,
        ids["org_id"],
        ids["d2"],
        nonce="s2",
        status=JobStatus.succeeded,
        finished_at=base + timedelta(hours=1),
    )
    await _scan_job(
        admin_engine,
        ids["org_id"],
        ids["d3"],
        nonce="s3",
        status=JobStatus.failed,
        finished_at=base + timedelta(hours=4),
    )

    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_report(session, ids["org_id"])

        assert report["last_scan_at"] == base + timedelta(hours=1)
        # d3's scan failed, so d3 has never been measured. Two of three.
        assert report["devices_scanned"] == 2
        assert report["devices_total"] == 3


async def test_fleet_report_scan_coverage_counts_devices_not_scans(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The `exposed_device_count` lesson applied to coverage: scanning one
    machine twice is one machine measured, not two. Counting rows here would
    let a single chatty device report the whole fleet as covered."""
    ids = await _seed(admin_engine, "rpt-scancoverage")
    now = datetime.now(UTC)
    await _scan_job(
        admin_engine,
        ids["org_id"],
        ids["d1"],
        nonce="c1",
        status=JobStatus.succeeded,
        finished_at=now - timedelta(hours=2),
    )
    await _scan_job(
        admin_engine,
        ids["org_id"],
        ids["d1"],
        nonce="c2",
        status=JobStatus.succeeded,
        finished_at=now - timedelta(hours=1),
    )

    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_report(session, ids["org_id"])

        assert report["devices_scanned"] == 1
        assert report["devices_total"] == 3


async def test_device_report_last_scan_at_is_that_devices_own_scan(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A scan is dispatched per device, so `last_scan_at` is a per-device
    fact first and a fleet aggregate second. d2's clean state means nothing
    until d2 itself has answered."""
    ids = await _seed(admin_engine, "rpt-devscan")
    when = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    await _scan_job(
        admin_engine,
        ids["org_id"],
        ids["d1"],
        nonce="d1s",
        status=JobStatus.succeeded,
        finished_at=when,
    )

    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        assert (await device_report(session, ids["org_id"], ids["d1"]))["last_scan_at"] == when
        assert (await device_report(session, ids["org_id"], ids["d2"]))["last_scan_at"] is None


async def test_another_orgs_scan_does_not_date_this_orgs_report(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Tenancy (invariant #5) on the new read. Only the OTHER org has ever
    completed a scan; this org's report must still say `None`, or a busy
    neighbour tenant makes an unmeasured fleet look freshly measured."""
    mine = await _seed(admin_engine, "rpt-scan-mine")
    theirs = await _seed(admin_engine, "rpt-scan-theirs")
    await _scan_job(
        admin_engine,
        theirs["org_id"],
        theirs["d1"],
        nonce="foreign",
        status=JobStatus.succeeded,
        finished_at=datetime.now(UTC),
    )

    async with org_session(mine["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_report(session, mine["org_id"])
        assert report["last_scan_at"] is None
        assert report["devices_scanned"] == 0
        assert report["devices_total"] == 3
        assert (await device_report(session, mine["org_id"], mine["d1"]))["last_scan_at"] is None

    # ...and the other org still sees its own, so the assertions above are not
    # passing merely because nothing was written.
    async with org_session(
        theirs["org_id"], engine=app_engine, clients=ClientScope.all()
    ) as session:
        assert (await fleet_report(session, theirs["org_id"]))["last_scan_at"] is not None
