"""Tests for per-device + fleet backup reporting and the SOC 2/BCP evidence
export (docs/modules/backup/spec.md). Mirrors `tests/test_deploy_reporting.
py`'s fixture style and `tests/test_backup_result.py`'s org/device/repository
seeding. `reporting.py` is pure read-only aggregation — these tests seed a
realistic mix of runs/points across the full state vocabulary, then
hand-compute the expected coverage/success-rate/dedup math as literals.
"""

import datetime as _dt
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import BackupPoint, BackupRepository, BackupRun, Device, Organization
from app.modules.backup.reporting import (
    backup_evidence_rows,
    device_backup_status,
    fleet_backup_report,
)


async def _seed_org(admin_engine: AsyncEngine, slug: str) -> dict[str, UUID]:
    """Seeds one org with a repository and five devices:

    - d1: a `failed` run (older) then a `success` run (newer) producing a
      `complete` point that is both verified and restore-tested.
      bytes_protected=1000, bytes_stored=400.
    - d2: a `success` run producing a `complete` point that is verified but
      NOT restore-tested. bytes_protected=2000, bytes_stored=500.
    - d3: a `success` run producing a `complete` point that is neither
      verified nor restore-tested. bytes_protected=500, bytes_stored=500
      (no dedup savings).
    - d4: only a `failed` run, no point at all (a device that has tried and
      failed).
    - d5: no runs, no points at all (a device with zero backup history).

    Hand-computed fleet numbers (5 devices):
    - devices_covered = 3 (d1, d2, d3) -> coverage_rate = 3/5 = 0.6
    - runs: success=3 (d1's success, d2, d3), failed=2 (d1's failed, d4),
      pending=0, running=0 -> success_rate = 3/5 = 0.6
    - points_needing_verification = 1 (only d3's point has verified_at=None)
    - dedup: bytes_protected_total = 1000+2000+500 = 3500;
      bytes_stored_total = 400+500+500 = 1400;
      dedup_ratio = 1400/3500 = 0.4; savings_pct = (1-0.4)*100 = 60.0
    """
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        repo = BackupRepository(
            org_id=org.id, client_id=org.id,
            name="repo-1",
            storage_ref="s3://bucket/repo-1",
            encryption_key_ref="vault://key-1",
        )
        session.add(repo)
        await session.flush()

        d1 = Device(
            org_id=org.id,
            hostname=f"{slug}-d1",
            os_family="windows",
            client_id=org.id,
            site_id=org.id,
        )
        d2 = Device(
            org_id=org.id,
            hostname=f"{slug}-d2",
            os_family="windows",
            client_id=org.id,
            site_id=org.id,
        )
        d3 = Device(
            org_id=org.id,
            hostname=f"{slug}-d3",
            os_family="windows",
            client_id=org.id,
            site_id=org.id,
        )
        d4 = Device(
            org_id=org.id,
            hostname=f"{slug}-d4",
            os_family="windows",
            client_id=org.id,
            site_id=org.id,
        )
        d5 = Device(
            org_id=org.id,
            hostname=f"{slug}-d5",
            os_family="windows",
            client_id=org.id,
            site_id=org.id,
        )
        session.add_all([d1, d2, d3, d4, d5])
        await session.flush()

        now = _dt.datetime.now(_dt.UTC)

        # d1: failed run (older), then success run (newer) with a fully
        # proven (verified + restore-tested) point.
        d1_failed_run = BackupRun(
            org_id=org.id,
            device_id=d1.id,
            repository_id=repo.id,
            state="failed",
            bytes_protected=0,
            bytes_stored=0,
            chunk_count=0,
            started_at=now - _dt.timedelta(minutes=30),
            finished_at=now - _dt.timedelta(minutes=29),
            error="disk full",
            actor_type="system",
        )
        d1_success_run = BackupRun(
            org_id=org.id,
            device_id=d1.id,
            repository_id=repo.id,
            state="success",
            bytes_protected=1000,
            bytes_stored=400,
            chunk_count=5,
            started_at=now - _dt.timedelta(minutes=10),
            finished_at=now - _dt.timedelta(minutes=9),
            actor_type="system",
        )
        session.add_all([d1_failed_run, d1_success_run])
        await session.flush()

        d1_point = BackupPoint(
            org_id=org.id,
            device_id=d1.id,
            repository_id=repo.id,
            backup_run_id=d1_success_run.id,
            taken_at=now - _dt.timedelta(minutes=9),
            manifest_ref="s3://bucket/repo-1/manifests/d1",
            size=1000,
            state="complete",
            verified_at=now - _dt.timedelta(minutes=8),
            restore_tested_at=now - _dt.timedelta(minutes=7),
        )
        session.add(d1_point)

        # d2: success run with a point that is verified but NOT
        # restore-tested.
        d2_success_run = BackupRun(
            org_id=org.id,
            device_id=d2.id,
            repository_id=repo.id,
            state="success",
            bytes_protected=2000,
            bytes_stored=500,
            chunk_count=3,
            started_at=now - _dt.timedelta(minutes=15),
            finished_at=now - _dt.timedelta(minutes=14),
            actor_type="system",
        )
        session.add(d2_success_run)
        await session.flush()

        d2_point = BackupPoint(
            org_id=org.id,
            device_id=d2.id,
            repository_id=repo.id,
            backup_run_id=d2_success_run.id,
            taken_at=now - _dt.timedelta(minutes=14),
            manifest_ref="s3://bucket/repo-1/manifests/d2",
            size=2000,
            state="complete",
            verified_at=now - _dt.timedelta(minutes=13),
            restore_tested_at=None,
        )
        session.add(d2_point)

        # d3: success run with a point that is neither verified nor
        # restore-tested.
        d3_success_run = BackupRun(
            org_id=org.id,
            device_id=d3.id,
            repository_id=repo.id,
            state="success",
            bytes_protected=500,
            bytes_stored=500,
            chunk_count=1,
            started_at=now - _dt.timedelta(minutes=20),
            finished_at=now - _dt.timedelta(minutes=19),
            actor_type="system",
        )
        session.add(d3_success_run)
        await session.flush()

        d3_point = BackupPoint(
            org_id=org.id,
            device_id=d3.id,
            repository_id=repo.id,
            backup_run_id=d3_success_run.id,
            taken_at=now - _dt.timedelta(minutes=19),
            manifest_ref="s3://bucket/repo-1/manifests/d3",
            size=500,
            state="complete",
            verified_at=None,
            restore_tested_at=None,
        )
        session.add(d3_point)

        # d4: only a failed run, no point.
        d4_failed_run = BackupRun(
            org_id=org.id,
            device_id=d4.id,
            repository_id=repo.id,
            state="failed",
            bytes_protected=0,
            bytes_stored=0,
            chunk_count=0,
            started_at=now - _dt.timedelta(minutes=5),
            finished_at=now - _dt.timedelta(minutes=4),
            error="timeout",
            actor_type="system",
        )
        session.add(d4_failed_run)

        # d5: nothing at all.

        await session.flush()
        await session.commit()

        return {
            "org_id": org.id,
            "repo_id": repo.id,
            "d1": d1.id,
            "d2": d2.id,
            "d3": d3.id,
            "d4": d4.id,
            "d5": d5.id,
            "d1_point_id": d1_point.id,
            "d1_success_run_finished_at": d1_success_run.finished_at,  # type: ignore[dict-item]
        }


async def test_device_backup_status_covered_verified_and_restore_tested(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"dbs-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        status = await device_backup_status(session, ids["org_id"], ids["d1"])

        assert status["device_id"] == ids["d1"]
        assert status["last_good_point"] is not None
        assert status["last_good_point"]["backup_point_id"] == ids["d1_point_id"]
        assert status["last_good_point"]["size"] == 1000
        assert status["last_good_point"]["repository_id"] == ids["repo_id"]
        assert status["last_run_state"] == "success"  # newer than the failed run
        assert status["last_run_finished_at"] == ids["d1_success_run_finished_at"]
        assert status["verified_at"] is not None
        assert status["restore_tested_at"] is not None
        assert status["repository"] == {"id": ids["repo_id"], "name": "repo-1"}


async def test_device_backup_status_covered_but_not_verified_or_tested(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"dbs-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        status = await device_backup_status(session, ids["org_id"], ids["d3"])

        assert status["last_good_point"] is not None
        assert status["last_good_point"]["size"] == 500
        assert status["last_run_state"] == "success"
        assert status["verified_at"] is None
        assert status["restore_tested_at"] is None


async def test_device_backup_status_uncovered_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"dbs-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        status = await device_backup_status(session, ids["org_id"], ids["d5"])

        assert status["last_good_point"] is None
        assert status["last_run_state"] is None
        assert status["last_run_finished_at"] is None
        assert status["verified_at"] is None
        assert status["restore_tested_at"] is None
        assert status["repository"] is None


async def test_device_backup_status_only_failed_runs_no_point(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"dbs-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        status = await device_backup_status(session, ids["org_id"], ids["d4"])

        assert status["last_good_point"] is None
        assert status["last_run_state"] == "failed"
        assert status["repository"] is None


async def test_fleet_backup_report_math_against_hand_computed_values(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"frpt-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_backup_report(session, ids["org_id"])

        assert report["devices_total"] == 5
        assert report["devices_covered"] == 3
        assert report["coverage_rate"] == 3 / 5

        assert report["run_counts_by_state"] == {
            "pending": 0,
            "running": 0,
            "success": 3,
            "failed": 2,
            "missed": 0,
        }
        assert report["success_rate"] == 3 / 5

        assert report["points_needing_verification"] == 1

        dedup = report["dedup"]
        assert dedup["bytes_protected_total"] == 3500
        assert dedup["bytes_stored_total"] == 1400
        assert dedup["dedup_ratio"] == 1400 / 3500
        assert dedup["savings_pct"] == 60.0


async def test_fleet_backup_report_zero_devices_guards_divide_by_zero(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    slug = f"frpt-empty-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        org_id = org.id
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_backup_report(session, org_id)
        assert report["devices_total"] == 0
        assert report["devices_covered"] == 0
        assert report["coverage_rate"] == 0.0
        assert report["success_rate"] == 0.0
        assert report["points_needing_verification"] == 0
        assert report["dedup"]["bytes_protected_total"] == 0
        assert report["dedup"]["bytes_stored_total"] == 0
        assert report["dedup"]["dedup_ratio"] == 0.0
        assert report["dedup"]["savings_pct"] == 0.0


async def test_backup_evidence_rows_one_row_per_device_including_gaps(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"evid-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        rows = await backup_evidence_rows(session, ids["org_id"])

        assert len(rows) == 5
        by_device = {r["device_id"]: r for r in rows}
        assert set(by_device) == {ids["d1"], ids["d2"], ids["d3"], ids["d4"], ids["d5"]}

        assert by_device[ids["d1"]]["covered"] is True
        assert by_device[ids["d1"]]["restore_proven"] is True
        assert by_device[ids["d1"]]["verified_at"] is not None

        assert by_device[ids["d2"]]["covered"] is True
        assert by_device[ids["d2"]]["restore_proven"] is False
        assert by_device[ids["d2"]]["verified_at"] is not None

        assert by_device[ids["d3"]]["covered"] is True
        assert by_device[ids["d3"]]["restore_proven"] is False
        assert by_device[ids["d3"]]["verified_at"] is None

        # Gap rows: no backups at all, still get a row, covered=False.
        assert by_device[ids["d4"]]["covered"] is False
        assert by_device[ids["d4"]]["restore_proven"] is False
        assert by_device[ids["d4"]]["last_backup_taken_at"] is None

        assert by_device[ids["d5"]]["covered"] is False
        assert by_device[ids["d5"]]["restore_proven"] is False

        # Deterministic ordering by hostname.
        assert [r["hostname"] for r in rows] == sorted(r["hostname"] for r in rows)


async def test_a_second_orgs_rows_never_appear_in_any_output_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids_a = await _seed_org(admin_engine, f"tenant-a-{uuid4().hex[:6]}")
    ids_b = await _seed_org(admin_engine, f"tenant-b-{uuid4().hex[:6]}")

    async with org_session(
        ids_a["org_id"], engine=app_engine, clients=ClientScope.all()
    ) as session:
        fleet_a = await fleet_backup_report(session, ids_a["org_id"])
        assert fleet_a["devices_total"] == 5  # org A's own 5, not org B's

        rows_a = await backup_evidence_rows(session, ids_a["org_id"])
        assert len(rows_a) == 5
        device_ids_a = {r["device_id"] for r in rows_a}
        assert ids_b["d1"] not in device_ids_a
        assert ids_b["d2"] not in device_ids_a
        assert ids_b["d3"] not in device_ids_a
        assert ids_b["d4"] not in device_ids_a
        assert ids_b["d5"] not in device_ids_a

        # org A's session cannot see org B's device/run/point rows even when
        # asked about org B's own device_id directly.
        status = await device_backup_status(session, ids_a["org_id"], ids_b["d1"])
        assert status["last_good_point"] is None
        assert status["last_run_state"] is None

    async with org_session(
        ids_b["org_id"], engine=app_engine, clients=ClientScope.all()
    ) as session:
        fleet_b = await fleet_backup_report(session, ids_b["org_id"])
        assert fleet_b["devices_total"] == 5  # org B's own 5, not org A's

        rows_b = await backup_evidence_rows(session, ids_b["org_id"])
        device_ids_b = {r["device_id"] for r in rows_b}
        assert ids_a["d1"] not in device_ids_b
