"""Tests for per-deployment + fleet app-deploy reporting
(docs/modules/app-deploy/spec.md). Mirrors `tests/test_patch_reporting.py`'s
fixture style and `tests/test_deploy_result.py`'s org/device/package
seeding. `reporting.py` is pure read-only aggregation — these tests seed
deployment/device-result rows across the full result-state vocabulary plus
script runs, then hand-compute the expected counts and success-rate math.
"""

import datetime as _dt
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    Deployment,
    DeploymentResult,
    Device,
    DeviceGroup,
    Organization,
    Package,
)
from app.modules.deploy.models import Script, ScriptRun
from app.modules.deploy.reporting import deployment_report, fleet_deploy_report


async def _seed_org(admin_engine: AsyncEngine, slug: str) -> dict[str, UUID | list[UUID]]:
    """Seeds one org with:

    - A package and three devices (d1, d2, d3).
    - deployment_a (state="complete"): d1=installed, d2=failed,
      d3=not_applicable -> counts {installed:1, failed:1, not_applicable:1,
      running:0, pending:0}.
    - deployment_b (state="dispatching"): d1=installed, d2=installed,
      d3=running -> 2 installed of 3 rows -> success rate 2/3.
    - A script + one ScriptRun (state="succeeded") on d1.

    Returns ids keyed by name for the test to reference.
    """
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
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
        session.add_all([d1, d2, d3])
        await session.flush()

        package = Package(
            org_id=org.id, name="Acme", version="1.0", os_family="windows", installer_type="msi"
        )
        # A deployment's target exists: its client is the target's (0239).
        group = DeviceGroup(org_id=org.id, client_id=org.id, name="Acme targets")
        session.add_all([package, group])
        await session.flush()

        now = _dt.datetime.now(_dt.UTC)

        deployment_a = Deployment(
            org_id=org.id,
            package_id=package.id,
            action="install",
            target_type="group",
            target_id=group.id,
            state="complete",
            created_at=now - _dt.timedelta(minutes=10),
        )
        deployment_b = Deployment(
            org_id=org.id,
            package_id=package.id,
            action="install",
            target_type="group",
            target_id=group.id,
            state="dispatching",
            created_at=now - _dt.timedelta(minutes=5),
        )
        session.add_all([deployment_a, deployment_b])
        await session.flush()

        session.add_all(
            [
                DeploymentResult(
                    org_id=org.id,
                    deployment_id=deployment_a.id,
                    device_id=d1.id,
                    state="installed",
                    exit_code=0,
                    log_ref="s3://a/d1",
                    finished_at=now,
                ),
                DeploymentResult(
                    org_id=org.id,
                    deployment_id=deployment_a.id,
                    device_id=d2.id,
                    state="failed",
                    exit_code=1,
                    log_ref="s3://a/d2",
                    finished_at=now,
                ),
                DeploymentResult(
                    org_id=org.id,
                    deployment_id=deployment_a.id,
                    device_id=d3.id,
                    state="not_applicable",
                ),
                DeploymentResult(
                    org_id=org.id, deployment_id=deployment_b.id, device_id=d1.id, state="installed"
                ),
                DeploymentResult(
                    org_id=org.id, deployment_id=deployment_b.id, device_id=d2.id, state="installed"
                ),
                DeploymentResult(
                    org_id=org.id, deployment_id=deployment_b.id, device_id=d3.id, state="running"
                ),
            ]
        )
        await session.flush()

        script = Script(
            org_id=org.id,
            name="cleanup",
            os_family="windows",
            interpreter="powershell",
            body="echo hi",
        )
        session.add(script)
        await session.flush()

        script_run = ScriptRun(
            org_id=org.id,
            script_id=script.id,
            script_version=1,
            target_type="device",
            target_id=d1.id,
            device_id=d1.id,
            state="succeeded",
            exit_code=0,
            created_at=now,
        )
        session.add(script_run)
        await session.flush()

        await session.commit()

        return {
            "org_id": org.id,
            "d1": d1.id,
            "d2": d2.id,
            "d3": d3.id,
            "deployment_a": deployment_a.id,
            "deployment_b": deployment_b.id,
            "script_id": script.id,
            "script_run_id": script_run.id,
        }


async def test_deployment_report_per_device_rows_and_counts(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"drpt-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await deployment_report(session, ids["org_id"], ids["deployment_a"])

        assert report["state"] == "complete"
        assert report["action"] == "install"

        assert len(report["device_results"]) == 3
        # Deterministic ordering by device_id.
        assert [r["device_id"] for r in report["device_results"]] == sorted(
            [ids["d1"], ids["d2"], ids["d3"]]
        )

        by_device = {r["device_id"]: r for r in report["device_results"]}
        assert by_device[ids["d1"]]["state"] == "installed"
        assert by_device[ids["d1"]]["exit_code"] == 0
        assert by_device[ids["d1"]]["log_ref"] == "s3://a/d1"
        assert by_device[ids["d2"]]["state"] == "failed"
        assert by_device[ids["d3"]]["state"] == "not_applicable"

        assert report["counts_by_state"] == {
            "installed": 1,
            "failed": 1,
            "not_applicable": 1,
            "running": 0,
            "pending": 0,
        }


async def test_deployment_report_unknown_deployment_returns_none_fields(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"drpt-none-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await deployment_report(session, ids["org_id"], uuid4())
        assert report["state"] is None
        assert report["action"] is None
        assert report["package_id"] is None
        assert report["device_results"] == []
        assert report["counts_by_state"] == {
            "installed": 0,
            "failed": 0,
            "not_applicable": 0,
            "running": 0,
            "pending": 0,
        }


async def test_fleet_deploy_report_success_rate_math_and_script_history(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids = await _seed_org(admin_engine, f"frpt-{uuid4().hex[:6]}")
    async with org_session(ids["org_id"], engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_deploy_report(session, ids["org_id"])

        by_id = {d["deployment_id"]: d for d in report["deployments"]}
        assert set(by_id) == {ids["deployment_a"], ids["deployment_b"]}

        dep_a = by_id[ids["deployment_a"]]
        assert dep_a["state"] == "complete"
        assert dep_a["device_count"] == 3
        assert dep_a["success_rate"] == 1 / 3  # 1 installed of 3 rows

        dep_b = by_id[ids["deployment_b"]]
        assert dep_b["state"] == "dispatching"
        assert dep_b["device_count"] == 3
        assert dep_b["success_rate"] == 2 / 3  # 2 installed of 3 rows

        # Ordered by created_at desc: deployment_b (newer) before deployment_a.
        assert [d["deployment_id"] for d in report["deployments"]] == [
            ids["deployment_b"],
            ids["deployment_a"],
        ]

        assert len(report["script_runs"]) == 1
        run = report["script_runs"][0]
        assert run["script_run_id"] == ids["script_run_id"]
        assert run["script_id"] == ids["script_id"]
        assert run["device_id"] == ids["d1"]
        assert run["state"] == "succeeded"
        assert run["exit_code"] == 0


async def test_fleet_deploy_report_no_result_rows_yields_zero_success_rate(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    slug = f"frpt-empty-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        package = Package(
            org_id=org.id, name="Acme", version="1.0", os_family="windows", installer_type="msi"
        )
        # A deployment's target exists: its client is the target's (0239).
        group = DeviceGroup(org_id=org.id, client_id=org.id, name="Acme targets")
        session.add_all([package, group])
        await session.flush()

        deployment = Deployment(
            org_id=org.id,
            package_id=package.id,
            action="install",
            target_type="group",
            target_id=group.id,
            state="pending",
        )
        session.add(deployment)
        await session.flush()
        org_id, deployment_id = org.id, deployment.id
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        report = await fleet_deploy_report(session, org_id)
        dep = next(d for d in report["deployments"] if d["deployment_id"] == deployment_id)
        assert dep["device_count"] == 0
        assert dep["success_rate"] == 0.0
        assert report["script_runs"] == []


async def test_a_second_orgs_rows_never_appear_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    ids_a = await _seed_org(admin_engine, f"drpt-tenant-a-{uuid4().hex[:6]}")
    ids_b = await _seed_org(admin_engine, f"drpt-tenant-b-{uuid4().hex[:6]}")

    async with org_session(
        ids_a["org_id"], engine=app_engine, clients=ClientScope.all()
    ) as session:
        fleet = await fleet_deploy_report(session, ids_a["org_id"])
        fleet_deployment_ids = {d["deployment_id"] for d in fleet["deployments"]}
        assert ids_b["deployment_a"] not in fleet_deployment_ids
        assert ids_b["deployment_b"] not in fleet_deployment_ids

        script_run_ids = {r["script_run_id"] for r in fleet["script_runs"]}
        assert ids_b["script_run_id"] not in script_run_ids

        # org_b's deployment id, looked up under org_a's RLS-scoped session,
        # must resolve to "not found" rather than leaking org_b's rows.
        report = await deployment_report(session, ids_a["org_id"], ids_b["deployment_a"])
        assert report["state"] is None
        assert report["device_results"] == []
        assert report["counts_by_state"] == {
            "installed": 0,
            "failed": 0,
            "not_applicable": 0,
            "running": 0,
            "pending": 0,
        }

    async with org_session(
        ids_b["org_id"], engine=app_engine, clients=ClientScope.all()
    ) as session:
        fleet_b = await fleet_deploy_report(session, ids_b["org_id"])
        fleet_b_deployment_ids = {d["deployment_id"] for d in fleet_b["deployments"]}
        assert ids_a["deployment_a"] not in fleet_b_deployment_ids
        assert ids_a["deployment_b"] not in fleet_b_deployment_ids
