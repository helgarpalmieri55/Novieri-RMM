"""Tests for per-run + fleet + device provisioning reporting
(docs/modules/provisioning/spec.md). Mirrors `tests/test_backup_reporting.py`'s
fixture style: `reporting.py` is pure read-only aggregation over the
provisioning module's tables, so these tests seed a realistic mix of runs
across the full state vocabulary (pending, awaiting_enrollment, enrolled,
complete, failed) across two profiles, then hand-compute the expected
success-rate/per-profile math as literals.
"""

import datetime as _dt
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, Organization, OsImage, ProvisionProfile, ProvisionRun
from app.modules.provisioning.reporting import (
    device_provisioning,
    fleet_provisioning_report,
    provision_run_status,
)


async def _seed_org(admin_engine: AsyncEngine, slug: str) -> dict[str, UUID]:
    """Seeds one org with an image, two profiles (p1, p2), two devices (d1
    bound to a completed run, d2 bound to a completed run under p2, d3 with
    no provisioning history at all), and five runs:

    - r1: profile p1, device d1, state=complete, target_ref="vm-1",
      log_ref="log-1", started_at/finished_at set.
    - r2: profile p1, device=None, state=failed, error="dhcp timeout".
    - r3: profile p2, device d2, state=complete.
    - r4: profile p2, device=None, state=awaiting_enrollment.
    - r5: profile p1, device=None, state=pending.

    Hand-computed fleet numbers:
    - run_counts_by_state = {pending: 1, awaiting_enrollment: 1, enrolled: 0,
      complete: 2, failed: 1}
    - success_rate = 2 complete / (2 complete + 1 failed) = 2/3
    - machines_provisioned = 2 (r1, r3)
    - per_profile = {p1: {complete: 1, failed: 1, total: 3},
                     p2: {complete: 1, failed: 0, total: 2}}
    """
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        image = OsImage(
            org_id=org.id,
            os_family="windows",
            name="win-image",
            kind="image",
            params={},
        )
        session.add(image)
        await session.flush()

        p1 = ProvisionProfile(
            org_id=org.id, name="profile-1", image_id=image.id, post_config={}, target_scope={}
        )
        p2 = ProvisionProfile(
            org_id=org.id, name="profile-2", image_id=image.id, post_config={}, target_scope={}
        )
        session.add_all([p1, p2])
        await session.flush()

        d1 = Device(org_id=org.id, hostname=f"{slug}-d1", os_family="windows")
        d2 = Device(org_id=org.id, hostname=f"{slug}-d2", os_family="windows")
        d3 = Device(org_id=org.id, hostname=f"{slug}-d3", os_family="windows")
        session.add_all([d1, d2, d3])
        await session.flush()

        now = _dt.datetime.now(_dt.UTC)

        r1 = ProvisionRun(
            org_id=org.id,
            profile_id=p1.id,
            device_id=d1.id,
            target_ref="vm-1",
            state="complete",
            log_ref="log-1",
            started_at=now - _dt.timedelta(minutes=10),
            finished_at=now - _dt.timedelta(minutes=5),
            actor_type="system",
        )
        r2 = ProvisionRun(
            org_id=org.id,
            profile_id=p1.id,
            device_id=None,
            state="failed",
            error="dhcp timeout",
            started_at=now - _dt.timedelta(minutes=30),
            finished_at=now - _dt.timedelta(minutes=29),
            actor_type="system",
        )
        r3 = ProvisionRun(
            org_id=org.id,
            profile_id=p2.id,
            device_id=d2.id,
            state="complete",
            started_at=now - _dt.timedelta(minutes=20),
            finished_at=now - _dt.timedelta(minutes=15),
            actor_type="system",
        )
        r4 = ProvisionRun(
            org_id=org.id,
            profile_id=p2.id,
            device_id=None,
            state="awaiting_enrollment",
            started_at=now - _dt.timedelta(minutes=2),
            actor_type="system",
        )
        r5 = ProvisionRun(
            org_id=org.id,
            profile_id=p1.id,
            device_id=None,
            state="pending",
            actor_type="system",
        )
        session.add_all([r1, r2, r3, r4, r5])
        await session.flush()
        await session.commit()

        return {
            "org_id": org.id,
            "image_id": image.id,
            "p1": p1.id,
            "p2": p2.id,
            "d1": d1.id,
            "d2": d2.id,
            "d3": d3.id,
            "r1": r1.id,
            "r1_finished_at": r1.finished_at,  # type: ignore[dict-item]
            "r2": r2.id,
        }


async def test_provision_run_status_returns_fields_for_a_run(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"prs-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        status = await provision_run_status(session, ids["org_id"], ids["r1"])

        assert status["run_id"] == ids["r1"]
        assert status["state"] == "complete"
        assert status["profile_id"] == ids["p1"]
        assert status["image_id"] == ids["image_id"]
        assert status["device_id"] == ids["d1"]
        assert status["target_ref"] == "vm-1"
        assert status["finished_at"] == ids["r1_finished_at"]
        assert status["error"] is None
        assert status["log_ref"] == "log-1"


async def test_provision_run_status_failed_run_carries_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"prs-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        status = await provision_run_status(session, ids["org_id"], ids["r2"])

        assert status["state"] == "failed"
        assert status["error"] == "dhcp timeout"
        assert status["device_id"] is None


async def test_provision_run_status_missing_run_returns_none_shape(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"prs-{uuid4().hex[:6]}")
    missing_run_id = uuid4()
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        status = await provision_run_status(session, ids["org_id"], missing_run_id)

        assert status["run_id"] == missing_run_id
        assert status["state"] is None
        assert status["profile_id"] is None
        assert status["image_id"] is None
        assert status["device_id"] is None
        assert status["target_ref"] is None
        assert status["started_at"] is None
        assert status["finished_at"] is None
        assert status["error"] is None
        assert status["log_ref"] is None


async def test_fleet_provisioning_report_math_against_hand_computed_values(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"fpr-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_provisioning_report(session, ids["org_id"])

        assert report["run_counts_by_state"] == {
            "pending": 1,
            "awaiting_enrollment": 1,
            "enrolled": 0,
            "complete": 2,
            "failed": 1,
        }
        assert report["success_rate"] == 2 / 3
        assert report["machines_provisioned"] == 2

        per_profile = report["per_profile"]
        assert per_profile[ids["p1"]] == {"complete": 1, "failed": 1, "total": 3}
        assert per_profile[ids["p2"]] == {"complete": 1, "failed": 0, "total": 2}


async def test_fleet_provisioning_report_zero_runs_guards_divide_by_zero(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    slug = f"fpr-empty-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        org_id = org.id
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_provisioning_report(session, org_id)
        assert report["run_counts_by_state"] == {
            "pending": 0,
            "awaiting_enrollment": 0,
            "enrolled": 0,
            "complete": 0,
            "failed": 0,
        }
        assert report["success_rate"] == 0.0
        assert report["machines_provisioned"] == 0
        assert report["per_profile"] == {}


async def test_device_provisioning_returns_binding_for_provisioned_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"dpv-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        result = await device_provisioning(session, ids["org_id"], ids["d1"])

        assert result["device_id"] == ids["d1"]
        assert result["profile_id"] == ids["p1"]
        assert result["image_id"] == ids["image_id"]
        assert result["enrolled_at"] == ids["r1_finished_at"]
        assert result["latest_run_state"] == "complete"


async def test_device_provisioning_no_history_returns_none_shape(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"dpv-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        result = await device_provisioning(session, ids["org_id"], ids["d3"])

        assert result["device_id"] == ids["d3"]
        assert result["profile_id"] is None
        assert result["image_id"] is None
        assert result["enrolled_at"] is None
        assert result["latest_run_state"] is None


async def test_a_second_orgs_rows_never_appear_in_any_output_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids_a = await _seed_org(admin_engine, f"tenant-a-{uuid4().hex[:6]}")
    ids_b = await _seed_org(admin_engine, f"tenant-b-{uuid4().hex[:6]}")

    async with org_session(
        ids_a["org_id"], engine=app_engine, clients=ClientScope.all()
    ) as session:
        fleet_a = await fleet_provisioning_report(session, ids_a["org_id"])
        assert fleet_a["run_counts_by_state"] == {
            "pending": 1,
            "awaiting_enrollment": 1,
            "enrolled": 0,
            "complete": 2,
            "failed": 1,
        }
        assert set(fleet_a["per_profile"].keys()) == {ids_a["p1"], ids_a["p2"]}
        assert ids_b["p1"] not in fleet_a["per_profile"]
        assert ids_b["p2"] not in fleet_a["per_profile"]

        # org A's session cannot see org B's run even when asked directly.
        status = await provision_run_status(session, ids_a["org_id"], ids_b["r1"])
        assert status["state"] is None
        assert status["profile_id"] is None

        # org A's session cannot see org B's device binding either.
        device_result = await device_provisioning(session, ids_a["org_id"], ids_b["d1"])
        assert device_result["profile_id"] is None
        assert device_result["latest_run_state"] is None

    async with org_session(
        ids_b["org_id"], engine=app_engine, clients=ClientScope.all()
    ) as session:
        fleet_b = await fleet_provisioning_report(session, ids_b["org_id"])
        assert fleet_b["machines_provisioned"] == 2  # org B's own, not org A's
        assert ids_a["p1"] not in fleet_b["per_profile"]
