from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import (  # noqa: F401 -- Script/ScriptRun verify importability
    Deployment,
    DeploymentResult,
    Device,
    Organization,
    Package,
    Script,
    ScriptRun,
    User,
)


async def _org_device_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="dt", slug=f"dt-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="windows")
        s.add(dev)
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, dev.id, usr.id


async def test_package_and_deployment_roundtrip(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        pkg = Package(
            org_id=org_id,
            name="acrobat",
            version="24.1",
            os_family="windows",
            installer_type="msi",
            detection_rule={"type": "registry_value", "key": "HKLM\\...", "value": "24.1"},
        )
        s.add(pkg)
        await s.flush()
        assert pkg.detection_rule == {
            "type": "registry_value",
            "key": "HKLM\\...",
            "value": "24.1",
        }
        assert pkg.uninstall_rule == {}

        dep = Deployment(
            org_id=org_id,
            package_id=pkg.id,
            action="install",
            target_type="device",
            target_id=device_id,
            requested_by=user_id,
        )
        s.add(dep)
        await s.flush()
        assert dep.state == "pending"

        res = DeploymentResult(
            org_id=org_id,
            deployment_id=dep.id,
            device_id=device_id,
        )
        s.add(res)
        await s.flush()
        assert res.state == "pending"
        assert res.job_id is None and res.exit_code is None
    async with org_session(org_id, engine=app_engine) as s:
        got_pkg = (await s.execute(select(Package).where(Package.id == pkg.id))).scalar_one()
        assert got_pkg.detection_rule["type"] == "registry_value"
        got_dep = (await s.execute(select(Deployment).where(Deployment.id == dep.id))).scalar_one()
        assert got_dep.state == "pending"
        got_res = (
            await s.execute(select(DeploymentResult).where(DeploymentResult.id == res.id))
        ).scalar_one()
        assert got_res.state == "pending"


async def test_script_and_run_roundtrip(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        script = Script(
            org_id=org_id,
            name="cleanup-temp",
            os_family="windows",
            interpreter="powershell",
            body="Remove-Item -Recurse $env:TEMP\\*",
            created_by=user_id,
        )
        s.add(script)
        await s.flush()
        assert script.version == 1

        run = ScriptRun(
            org_id=org_id,
            script_id=script.id,
            script_version=script.version,
            target_type="device",
            target_id=device_id,
            device_id=device_id,
            requested_by=user_id,
        )
        s.add(run)
        await s.flush()
        assert run.state == "pending"
        assert run.job_id is None and run.finished_at is None
    async with org_session(org_id, engine=app_engine) as s:
        got = (await s.execute(select(ScriptRun).where(ScriptRun.id == run.id))).scalar_one()
        assert got.state == "pending" and got.script_version == 1
