"""The default "Workstation baseline" health profile (platform review 2026-09-23)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    Agent,
    AuditEvent,
    Device,
    HealthAlert,
    HealthBaselineSeed,
    HealthCheck,
    HealthOrgSettings,
    HealthProfile,
    HealthProfileAssignment,
)
from app.modules.health import baseline
from app.modules.health import sweep as health_sweep
from app.modules.health.resolution import resolve_profile_for_device
from app.modules.health.sweep import run_offline_sweep
from tests.health_fixtures import seed_device, seed_org, seed_profile, seed_settings


async def _profiles(app_engine: AsyncEngine, org_id: UUID) -> list[HealthProfile]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        return list(
            (
                await session.execute(
                    select(HealthProfile).where(HealthProfile.org_id == org_id)
                )
            ).scalars()
        )


async def _seed(app_engine: AsyncEngine, org_id: UUID) -> str:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        return await baseline.ensure_baseline(session, org_id)


async def test_a_new_org_gets_the_baseline_as_its_default(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "bl")
    assert await _seed(app_engine, org.org_id) == "seeded"

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        profiles = list((await session.execute(select(HealthProfile))).scalars())
        assert [p.name for p in profiles] == [baseline.BASELINE_NAME]
        profile = profiles[0]
        checks = list(
            (
                await session.execute(
                    select(HealthCheck).where(HealthCheck.profile_id == profile.id)
                )
            ).scalars()
        )
        assignments = list((await session.execute(select(HealthProfileAssignment))).scalars())
        settings_row = (await session.execute(select(HealthOrgSettings))).scalar_one()
        marker = (await session.execute(select(HealthBaselineSeed))).scalar_one()
        actions = [
            a.action for a in (await session.execute(select(AuditEvent))).scalars()
        ]

    by_key = {(c.check_type, c.selector, c.os_family): c for c in checks}
    assert set(by_key) == {
        ("disk.free_pct", "*", None),
        ("agent.offline", "", None),
        ("posture.disk_encryption", "", "windows"),
        ("posture.disk_encryption", "", "macos"),
        ("posture.firewall_enabled", "", "windows"),
        ("posture.firewall_enabled", "", "macos"),
        ("process.present", "EPWD.exe", "windows"),
        # Server-evaluated: migrations 0091 and 0121 only reach profiles that
        # already existed, so the baseline carries these itself.
        ("backup.success_age_h", "", None),
        ("backup.last_run_failed", "", None),
        ("backup.offsite_lag_h", "", None),
        ("backup.restore_test_age_d", "", None),
        ("script.policy_failed", "", None),
    }
    disk = by_key[("disk.free_pct", "*", None)]
    assert (disk.comparator, float(disk.threshold_num or 0)) == ("lt", 10.0)
    harmony = by_key[("process.present", "EPWD.exe", "windows")]
    assert harmony.comparator == "is" and harmony.threshold_bool is True
    for check in checks:
        critical = check.severity == "critical"
        # Critical opens a ticket; the warnings do not.
        assert check.declares_open_ticket is critical
        assert check.declares_notify is True
        assert check.remediate_approved_action_id is None
    assert by_key[("agent.offline", "", None)].severity == "warning"
    assert by_key[("backup.last_run_failed", "", None)].severity == "critical"
    assert by_key[("script.policy_failed", "", None)].severity == "warning"

    # Assigned as the org default: every device resolves to it.
    assert len(assignments) == 1
    assert assignments[0].device_id is None and assignments[0].device_group_id is None
    assert assignments[0].profile_id == profile.id

    assert settings_row.offline_after_s == 1800
    assert (marker.outcome, marker.profile_id) == ("seeded", profile.id)
    assert "health.baseline_seeded" in actions
    assert "health.profile_created" in actions


async def test_checks_resolve_per_os(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "bl")
    linux = await seed_device(admin_engine, org.org_id, os_family="linux")
    mac = await seed_device(admin_engine, org.org_id, os_family="macos")
    await _seed(app_engine, org.org_id)

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        got = {}
        for device_id in (org.device_id, linux, mac):
            device = await session.get(Device, device_id)
            assert device is not None
            resolved = await resolve_profile_for_device(session, org.org_id, device)
            assert resolved is not None
            got[device.os_family] = {c.check_type for c in resolved.agent_checks}

    assert got["windows"] == {
        "disk.free_pct",
        "posture.disk_encryption",
        "posture.firewall_enabled",
        "process.present",
    }
    assert got["macos"] == {"disk.free_pct", "posture.disk_encryption", "posture.firewall_enabled"}
    assert got["linux"] == {"disk.free_pct"}


async def test_the_seed_runs_once(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org = await seed_org(admin_engine, "bl")
    assert await _seed(app_engine, org.org_id) == "seeded"
    assert await _seed(app_engine, org.org_id) == "already"
    assert len(await _profiles(app_engine, org.org_id)) == 1


async def test_a_deleted_baseline_is_not_recreated(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "bl")
    await _seed(app_engine, org.org_id)
    from app.modules.health.profiles import delete_assignment, delete_profile

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        profile = (await session.execute(select(HealthProfile))).scalar_one()
        assignment = (await session.execute(select(HealthProfileAssignment))).scalar_one()
        await delete_assignment(session, org.org_id, None, assignment.id)
        await delete_profile(session, org.org_id, None, profile.id)

    assert await _seed(app_engine, org.org_id) == "already"
    assert await _profiles(app_engine, org.org_id) == []


async def test_an_org_that_has_profiles_is_skipped_and_untouched(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "bl")
    own = await seed_profile(admin_engine, org.org_id, name="Servers")
    await seed_settings(admin_engine, org.org_id, offline_after_s=600)
    before = {p.id: p.policy_version for p in await _profiles(app_engine, org.org_id)}

    assert await _seed(app_engine, org.org_id) == "skipped"
    # ...and it stays skipped, even after the org's own profile is gone.
    assert await _seed(app_engine, org.org_id) == "already"

    after = {p.id: p.policy_version for p in await _profiles(app_engine, org.org_id)}
    assert after == before == {own: 1}
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert list((await session.execute(select(HealthProfileAssignment))).scalars()) == []
        settings_row = (await session.execute(select(HealthOrgSettings))).scalar_one()
        marker = (await session.execute(select(HealthBaselineSeed))).scalar_one()
    assert settings_row.offline_after_s == 600
    assert (marker.outcome, marker.profile_id) == ("skipped", None)


async def test_existing_org_settings_are_not_overwritten(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "bl")
    await seed_settings(admin_engine, org.org_id, offline_after_s=3600)
    assert await _seed(app_engine, org.org_id) == "seeded"
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        settings_row = (await session.execute(select(HealthOrgSettings))).scalar_one()
    assert settings_row.offline_after_s == 3600


async def test_the_profile_count_is_scoped_by_the_applications_own_predicate(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """On BYPASSRLS, another org's profiles must not make this org look
    configured, and this org's seed must not land anywhere else."""
    org_a = await seed_org(admin_engine, "bl")
    org_b = await seed_org(admin_engine, "bl")
    await seed_profile(admin_engine, org_b.org_id, name="B's own")

    assert await baseline.ensure_baseline(bypassrls_session, org_a.org_id) == "seeded"
    await bypassrls_session.commit()

    rows = (
        await bypassrls_session.execute(
            select(HealthProfile.org_id, HealthProfile.name).where(
                HealthProfile.org_id.in_([org_a.org_id, org_b.org_id])
            )
        )
    ).all()
    assert sorted((r.org_id == org_a.org_id, r.name) for r in rows) == [
        (False, "B's own"),
        (True, baseline.BASELINE_NAME),
    ]


async def test_the_reconcile_seeds_undecided_orgs_only(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = await seed_org(admin_engine, "bl")
    configured = await seed_org(admin_engine, "bl")
    await seed_profile(admin_engine, configured.org_id, name="Mine")

    async def only_these(_admin_engine: Any) -> list[UUID]:
        return [fresh.org_id, configured.org_id]

    monkeypatch.setattr(health_sweep, "_org_ids", only_these)
    assert await health_sweep.sweep_baseline_all_orgs(
        engine=app_engine, admin_engine=admin_engine
    ) == 1
    assert await health_sweep.sweep_baseline_all_orgs(
        engine=app_engine, admin_engine=admin_engine
    ) == 0
    assert [p.name for p in await _profiles(app_engine, fresh.org_id)] == [
        baseline.BASELINE_NAME
    ]
    assert [p.name for p in await _profiles(app_engine, configured.org_id)] == ["Mine"]


async def test_the_baseline_raises_an_offline_alert_after_thirty_minutes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "bl")
    await _seed(app_engine, org.org_id)
    now = datetime.now(UTC)
    quiet = await seed_device(admin_engine, org.org_id)
    for device_id, seen in ((org.device_id, now - timedelta(minutes=40)),
                            (quiet, now - timedelta(minutes=20))):
        async with AsyncSession(admin_engine) as session:
            session.add(
                Agent(
                    org_id=org.org_id,
                    device_id=device_id,
                    cert_fingerprint=uuid4().hex,
                    cert_serial=uuid4().hex[:16],
                    cert_not_after=now + timedelta(days=365),
                    enrolled_at=now - timedelta(days=1),
                    status="offline",
                    last_seen_at=seen,
                )
            )
            await session.commit()

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        await run_offline_sweep(session, org.org_id, now=now)
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        alerts = list((await session.execute(select(HealthAlert))).scalars())
    assert [(a.device_id, a.check_type, a.severity) for a in alerts] == [
        (org.device_id, "agent.offline", "warning")
    ]


def test_the_reconcile_is_scheduled(pg_cluster: Any, tmp_path: Any) -> None:
    from app.core.config import Settings, override_settings
    from app.worker import create_celery

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
    )
    with override_settings(settings):
        entry = create_celery().conf.beat_schedule["health-baseline-reconcile"]
    assert entry["task"] == "app.worker.health_baseline_reconcile"
    assert entry["schedule"] == baseline.RECONCILE_INTERVAL_S
