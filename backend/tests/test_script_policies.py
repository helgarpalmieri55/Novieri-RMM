"""Scheduled script policies (docs/modules/app-deploy/spec.md §9).

A policy runs a script on every connected device in a group, no more often than
its interval and, when asked, only inside each device's patch maintenance
window, through the ordinary signed `script.run`. Output is kept on the run and
cleared past `keep_runs`; a failed run can raise a health alert, which the
device's health profile routes to a ticket.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Agent, AuditEvent, Job, PatchPolicy, Script, ScriptPolicy, ScriptRun
from app.modules.deploy.handler import register_job_types
from app.modules.deploy.schema import ScriptRunResult
from app.modules.deploy.script_policies import create_policy, run_policy_sweep
from app.modules.deploy.scripts import ingest_script_result
from app.modules.health.models import HealthAlert, HealthAlertAction
from tests.health_action_fixtures import seed_ticketing
from tests.health_fixtures import (
    seed_assignment,
    seed_check,
    seed_org,
    seed_profile,
    seed_settings,
)
from tests.test_patch_reboot import _FakeJobEngine

register_job_types()

NOW = datetime.now(UTC)


async def _setup(
    admin_engine: AsyncEngine, *, window: str | None = None, online: bool = True
) -> tuple[Any, UUID]:
    org = await seed_org(admin_engine, "spol")
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        script = Script(
            org_id=org.org_id,
            name="Clear temp",
            os_family="windows",
            interpreter="powershell",
            body="Remove-Item $env:TEMP\\* -Recurse",
            version=3,
            created_by=org.user_id,
        )
        s.add(script)
        s.add(
            Agent(
                org_id=org.org_id,
                device_id=org.device_id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=NOW + timedelta(days=365),
                enrolled_at=NOW - timedelta(days=30),
                status="online" if online else "offline",
                last_seen_at=NOW,
            )
        )
        if window is not None:
            s.add(
                PatchPolicy(
                    org_id=org.org_id, device_group_id=org.group_id, maintenance_window=window
                )
            )
        await s.commit()
        return org, script.id


async def _policy(app_engine: AsyncEngine, org: Any, script_id: UUID, **kw: Any) -> UUID:
    values: dict[str, Any] = {
        "name": "Nightly temp clean",
        "interval_hours": 24,
        "in_maintenance_window": False,
        "alert_on_failure": False,
        "keep_runs": 30,
        "enabled": True,
    }
    values.update(kw)
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        row = await create_policy(
            session,
            org_id=org.org_id,
            actor_id=org.user_id,
            script_id=script_id,
            device_group_id=org.group_id,
            **values,
        )
        return row.id


async def _sweep(app_engine: AsyncEngine, org_id: UUID, now: datetime) -> _FakeJobEngine:
    engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await run_policy_sweep(session, engine, org_id=org_id, now=now)
    return engine


async def _runs(admin_engine: AsyncEngine, org_id: UUID) -> list[ScriptRun]:
    async with AsyncSession(admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(ScriptRun)
                    .where(ScriptRun.org_id == org_id)
                    .order_by(ScriptRun.created_at)
                )
            ).scalars()
        )


async def _finish(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, run: ScriptRun, *, exit_code: int
) -> None:
    async with org_session(run.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == run.job_id))).scalar_one()
        await ingest_script_result(
            session,
            job,
            ScriptRunResult(
                outcome={  # type: ignore[arg-type]
                    "identifier": "x",
                    "state": "succeeded" if exit_code == 0 else "failed",
                    "exit_code": exit_code,
                    "log": f"removed 12 files (exit {exit_code})",
                }
            ),
        )


async def _age_runs(admin_engine: AsyncEngine, org_id: UUID, hours: int) -> None:
    async with AsyncSession(admin_engine) as s:
        await s.execute(
            text(
                "UPDATE script_runs SET created_at = created_at - make_interval(hours => :h) "
                "WHERE org_id = :o"
            ),
            {"h": hours, "o": org_id},
        )
        await s.commit()


async def test_the_sweep_runs_the_signed_script_on_the_group_once_per_interval(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, script_id = await _setup(admin_engine)
    policy_id = await _policy(app_engine, org, script_id)

    first = await _sweep(app_engine, org.org_id, NOW)
    [created] = first.created
    assert created["type"] == "script.run"
    assert created["target_device_id"] == org.device_id
    assert created["params"]["body"] == "Remove-Item $env:TEMP\\* -Recurse"
    assert created["params"]["script_version"] == 3
    [run] = await _runs(admin_engine, org.org_id)
    assert run.policy_id == policy_id and run.target_type == "group"

    # In flight, then finished but inside the interval: nothing.
    assert (await _sweep(app_engine, org.org_id, NOW)).created == []
    await _finish(admin_engine, app_engine, run, exit_code=0)
    assert (await _sweep(app_engine, org.org_id, NOW)).created == []

    # A day later it is due again.
    await _age_runs(admin_engine, org.org_id, 25)
    assert len((await _sweep(app_engine, org.org_id, NOW)).created) == 1

    async with AsyncSession(admin_engine) as s:
        actions = (
            await s.execute(
                select(AuditEvent.action).where(
                    AuditEvent.org_id == org.org_id, AuditEvent.target_id == str(policy_id)
                )
            )
        ).scalars().all()
    assert actions.count("script_policy.dispatched") == 2
    assert "script_policy.created" in actions


async def test_the_window_offline_devices_and_other_platforms_are_respected(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    sat_3am = datetime(2026, 9, 26, 3, 20, tzinfo=UTC)
    org, script_id = await _setup(admin_engine, window="0 2-5 * * 6")
    await _policy(app_engine, org, script_id, in_maintenance_window=True)
    assert (await _sweep(app_engine, org.org_id, sat_3am - timedelta(days=1))).created == []
    assert len((await _sweep(app_engine, org.org_id, sat_3am)).created) == 1

    offline, offline_script = await _setup(admin_engine, online=False)
    await _policy(app_engine, offline, offline_script)
    assert (await _sweep(app_engine, offline.org_id, NOW)).created == []

    mac, mac_script = await _setup(admin_engine)
    async with AsyncSession(admin_engine) as s:
        await s.execute(
            text("UPDATE scripts SET os_family = 'macos' WHERE id = :i"), {"i": mac_script}
        )
        await s.commit()
    await _policy(app_engine, mac, mac_script)
    assert (await _sweep(app_engine, mac.org_id, NOW)).created == []


async def test_output_is_kept_and_cleared_past_keep_runs(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, script_id = await _setup(admin_engine)
    await _policy(app_engine, org, script_id, keep_runs=2, interval_hours=1)
    for _ in range(3):
        await _sweep(app_engine, org.org_id, NOW)
        run = (await _runs(admin_engine, org.org_id))[-1]
        await _finish(admin_engine, app_engine, run, exit_code=0)
        await _age_runs(admin_engine, org.org_id, 2)
    outputs = [r.output for r in await _runs(admin_engine, org.org_id)]
    assert outputs == [None, "removed 12 files (exit 0)", "removed 12 files (exit 0)"]


async def test_a_failed_run_raises_an_alert_the_profile_routes_and_success_clears_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, script_id = await _setup(admin_engine)
    profile_id = await seed_profile(admin_engine, org.org_id)
    await seed_assignment(admin_engine, org.org_id, profile_id)
    await seed_check(
        admin_engine,
        org.org_id,
        profile_id,
        check_type="script.policy_failed",
        comparator="is",
        threshold_num=None,
        threshold_bool=True,
        severity="critical",
        for_ticks=1,
    )
    refs = await seed_ticketing(admin_engine, org.org_id, org.user_id)
    await seed_settings(
        admin_engine,
        org.org_id,
        default_alert_queue_id=refs.queue_id,
        default_ticket_category_id=refs.category_id,
    )
    await _policy(app_engine, org, script_id, alert_on_failure=True, interval_hours=1)

    await _sweep(app_engine, org.org_id, NOW)
    [run] = await _runs(admin_engine, org.org_id)
    await _finish(admin_engine, app_engine, run, exit_code=2)
    async with AsyncSession(admin_engine) as s:
        [alert] = (
            await s.execute(select(HealthAlert).where(HealthAlert.org_id == org.org_id))
        ).scalars().all()
        kinds = sorted(
            (
                await s.execute(
                    select(HealthAlertAction.kind).where(HealthAlertAction.alert_id == alert.id)
                )
            ).scalars()
        )
    assert alert.check_type == "script.policy_failed"
    assert alert.selector == "Nightly temp clean"
    assert alert.observed_value == "exit 2"
    assert "open_ticket" in kinds

    await _age_runs(admin_engine, org.org_id, 2)
    await _sweep(app_engine, org.org_id, NOW)
    second = (await _runs(admin_engine, org.org_id))[-1]
    await _finish(admin_engine, app_engine, second, exit_code=0)
    async with AsyncSession(admin_engine) as s:
        alert = (
            await s.execute(select(HealthAlert).where(HealthAlert.id == alert.id))
        ).scalar_one()
    assert alert.state == "cleared" and alert.cleared_reason == "recovery"


async def test_a_failure_with_no_check_to_route_it_is_recorded(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, script_id = await _setup(admin_engine)
    policy_id = await _policy(app_engine, org, script_id, alert_on_failure=True)
    await _sweep(app_engine, org.org_id, NOW)
    [run] = await _runs(admin_engine, org.org_id)
    await _finish(admin_engine, app_engine, run, exit_code=1)
    async with AsyncSession(admin_engine) as s:
        actions = (
            await s.execute(
                select(AuditEvent.action).where(AuditEvent.target_id == str(policy_id))
            )
        ).scalars().all()
        alerts = (
            await s.execute(select(HealthAlert).where(HealthAlert.org_id == org.org_id))
        ).scalars().all()
    assert "script_policy.alert_unrouted" in actions
    assert alerts == []


async def test_the_api_is_gated_and_validates(api_env) -> None:
    org_id = await api_env.create_org("spol")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        from app.models import DeviceGroup

        group = DeviceGroup(org_id=org_id, name="laptops", client_id=org_id)
        script = Script(
            org_id=org_id, name="s", os_family="windows", interpreter="powershell", body="exit 0"
        )
        s.add_all([group, script])
        await s.commit()
    body = {"name": "Weekly", "script_id": str(script.id), "device_group_id": str(group.id)}

    denied = await api_env.client.post(
        "/api/v1/deploy/script-policies", json=body, headers=await api_env.headers(org_id, tech)
    )
    assert denied.status_code == 403
    created = await api_env.client.post(
        "/api/v1/deploy/script-policies", json=body, headers=await api_env.headers(org_id, admin)
    )
    assert created.status_code == 201, created.text
    policy = created.json()
    assert policy["script_name"] == "s" and policy["device_group_name"] == "laptops"
    assert policy["interval_hours"] == 24 and policy["in_maintenance_window"] is True

    dup = await api_env.client.post(
        "/api/v1/deploy/script-policies", json=body, headers=await api_env.headers(org_id, admin)
    )
    assert dup.status_code == 422
    bad = await api_env.client.post(
        "/api/v1/deploy/script-policies",
        json={**body, "name": "x", "script_id": str(uuid4())},
        headers=await api_env.headers(org_id, admin),
    )
    assert bad.status_code == 422

    listed = await api_env.client.get(
        "/api/v1/deploy/script-policies", headers=await api_env.headers(org_id, ro)
    )
    assert listed.status_code == 200 and len(listed.json()) == 1
    runs = await api_env.client.get(
        f"/api/v1/deploy/script-policies/{policy['id']}/runs",
        headers=await api_env.headers(org_id, ro),
    )
    assert runs.status_code == 200 and runs.json() == []

    patched = await api_env.client.patch(
        f"/api/v1/deploy/script-policies/{policy['id']}",
        json={"enabled": False, "interval_hours": 168},
        headers=await api_env.headers(org_id, admin),
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False and patched.json()["interval_hours"] == 168

    gone = await api_env.client.delete(
        f"/api/v1/deploy/script-policies/{policy['id']}",
        headers=await api_env.headers(org_id, admin),
    )
    assert gone.status_code == 204
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(select(ScriptPolicy).where(ScriptPolicy.org_id == org_id))
        ).first() is None


async def test_a_group_member_waiting_for_review_is_passed_over(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Dynamic groups place a link-enrolled machine the moment it joins; the
    gate would refuse `script.run` and abort every policy in the org."""
    from sqlalchemy import update

    from app.models import Device

    org, script_id = await _setup(admin_engine)
    await _policy(app_engine, org, script_id)
    async with AsyncSession(admin_engine) as s:
        await s.execute(
            update(Device).where(Device.id == org.device_id).values(review_state="pending")
        )
        await s.commit()
    engine = await _sweep(app_engine, org.org_id, NOW)
    assert engine.created == []
