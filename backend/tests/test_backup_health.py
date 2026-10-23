"""Backup health checks (spec-server-backup.md §8.1).

Failed and missed backups raise health alerts, and a critical one queues the
health module's own `open_ticket` action, so a ticket is opened by the existing
service-desk path. Only protected devices are evaluated.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import BackupPoint, BackupPolicy, BackupRepository, BackupRun
from app.modules.backup.health import run_backup_health_sweep
from app.modules.health.models import HealthAlert, HealthAlertAction
from app.modules.health.registry import CHECK_TYPES, agent_check_type_names
from tests.health_action_fixtures import seed_ticketing
from tests.health_fixtures import (
    seed_assignment,
    seed_check,
    seed_org,
    seed_profile,
    seed_settings,
)

NOW = datetime.now(UTC)


async def _protected_org(admin_engine: AsyncEngine, *, ticketing: bool = True) -> Any:
    org = await seed_org(admin_engine, "bkh")
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        repo = BackupRepository(org_id=org.org_id, name="r", storage_ref="x",
                                encryption_key_ref="x", dedup_params={}, state="active")
        s.add(repo)
        await s.flush()
        policy = BackupPolicy(org_id=org.org_id, repository_id=repo.id, sources={"paths": ["/"]},
                              window_cron="0 2 * * *", retention_gfs={}, enabled=True)
        s.add(policy)
        await s.commit()
        repo_id = repo.id
    # Protected for a week already.
    async with AsyncSession(admin_engine) as s:
        await s.execute(text("UPDATE backup_policies SET created_at = :t WHERE org_id = :o"),
                        {"t": NOW - timedelta(days=7), "o": org.org_id})
        await s.execute(text("UPDATE devices SET created_at = :t WHERE org_id = :o"),
                        {"t": NOW - timedelta(days=7), "o": org.org_id})
        await s.commit()
    profile_id = await seed_profile(admin_engine, org.org_id)
    await seed_assignment(admin_engine, org.org_id, profile_id)
    checks = {
        "backup.success_age_h": await seed_check(
            admin_engine, org.org_id, profile_id, check_type="backup.success_age_h",
            comparator="gt", threshold_num=50, severity="critical", for_ticks=1),
        "backup.last_run_failed": await seed_check(
            admin_engine, org.org_id, profile_id, check_type="backup.last_run_failed",
            comparator="is", threshold_num=None, threshold_bool=True, severity="critical",
            for_ticks=1),
        "backup.restore_test_age_d": await seed_check(
            admin_engine, org.org_id, profile_id, check_type="backup.restore_test_age_d",
            comparator="gt", threshold_num=35, severity="warning", for_ticks=1,
            declares_open_ticket=False),
    }
    if ticketing:
        refs = await seed_ticketing(admin_engine, org.org_id, org.user_id)
        await seed_settings(admin_engine, org.org_id, default_alert_queue_id=refs.queue_id,
                            default_ticket_category_id=refs.category_id)
    return org, repo_id, checks


async def _add_run(admin_engine: AsyncEngine, org: Any, repo_id: UUID, state: str,
                   started: datetime) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        run = BackupRun(org_id=org.org_id, device_id=org.device_id, repository_id=repo_id,
                        state=state, actor_type="system", started_at=started)
        s.add(run)
        await s.commit()
        return run.id


async def _alerts(admin_engine: AsyncEngine, org_id: UUID) -> dict[str, HealthAlert]:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        rows = (await s.execute(select(HealthAlert).where(HealthAlert.org_id == org_id))
                ).scalars().all()
    return {r.check_type: r for r in rows}


async def _sweep(app_engine: AsyncEngine, org_id: UUID, now: datetime) -> dict[str, int]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        return await run_backup_health_sweep(session, org_id, now=now)


def test_the_backup_checks_are_server_evaluated_and_never_pushed() -> None:
    for name in ("backup.success_age_h", "backup.last_run_failed", "backup.offsite_lag_h",
                 "backup.restore_test_age_d"):
        assert CHECK_TYPES[name].evaluated_by == "server"
        assert name not in agent_check_type_names()


async def test_a_missed_backup_raises_a_critical_alert_that_opens_a_ticket(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, repo_id, _checks = await _protected_org(admin_engine)
    await _add_run(admin_engine, org, repo_id, "missed", NOW - timedelta(hours=10))

    counts = await _sweep(app_engine, org.org_id, NOW)

    alerts = await _alerts(admin_engine, org.org_id)
    failed = alerts["backup.last_run_failed"]
    assert failed.severity == "critical"
    assert failed.state == "active"
    # Never backed up in a week: the age check fires too.
    assert "backup.success_age_h" in alerts
    assert counts["opened"] >= 2
    async with AsyncSession(admin_engine) as s:
        kinds = set((await s.execute(select(HealthAlertAction.kind).where(
            HealthAlertAction.alert_id == failed.id))).scalars().all())
    assert "open_ticket" in kinds


async def test_a_second_sweep_does_not_open_a_second_alert(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, repo_id, _ = await _protected_org(admin_engine, ticketing=False)
    await _add_run(admin_engine, org, repo_id, "failed", NOW - timedelta(hours=1))
    await _sweep(app_engine, org.org_id, NOW)
    again = await _sweep(app_engine, org.org_id, NOW + timedelta(minutes=15))
    assert again["opened"] == 0
    assert (await _alerts(admin_engine, org.org_id))["backup.last_run_failed"].occurrence_count == 2


async def test_a_successful_backup_clears_the_alerts(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, repo_id, _ = await _protected_org(admin_engine, ticketing=False)
    await _add_run(admin_engine, org, repo_id, "failed", NOW - timedelta(hours=5))
    await _sweep(app_engine, org.org_id, NOW)
    run_id = await _add_run(admin_engine, org, repo_id, "success", NOW - timedelta(hours=1))
    async with AsyncSession(admin_engine) as s:
        s.add(BackupPoint(org_id=org.org_id, device_id=org.device_id, repository_id=repo_id,
                          backup_run_id=run_id, manifest_ref="m", size=1, state="complete",
                          taken_at=NOW - timedelta(hours=1), restore_tested_at=NOW))
        await s.commit()

    counts = await _sweep(app_engine, org.org_id, NOW)

    alerts = await _alerts(admin_engine, org.org_id)
    assert alerts["backup.last_run_failed"].state == "cleared"
    assert alerts["backup.last_run_failed"].cleared_reason == "recovery"
    assert alerts["backup.success_age_h"].state == "cleared"
    assert counts["cleared"] >= 2


async def test_an_untested_restore_raises_a_warning_after_35_days(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, repo_id, _ = await _protected_org(admin_engine, ticketing=False)
    run_id = await _add_run(admin_engine, org, repo_id, "success", NOW - timedelta(hours=1))
    async with AsyncSession(admin_engine) as s:
        s.add(BackupPoint(org_id=org.org_id, device_id=org.device_id, repository_id=repo_id,
                          backup_run_id=run_id, manifest_ref="m", size=1, state="complete",
                          taken_at=NOW - timedelta(hours=1),
                          restore_tested_at=NOW - timedelta(days=40)))
        await s.commit()
    await _sweep(app_engine, org.org_id, NOW)
    alerts = await _alerts(admin_engine, org.org_id)
    assert set(alerts) == {"backup.restore_test_age_d"}
    assert alerts["backup.restore_test_age_d"].severity == "warning"


async def test_a_device_nobody_asked_to_protect_is_never_evaluated(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org, repo_id, _ = await _protected_org(admin_engine, ticketing=False)
    await _add_run(admin_engine, org, repo_id, "failed", NOW - timedelta(hours=1))
    async with AsyncSession(admin_engine) as s:
        await s.execute(text("UPDATE backup_policies SET enabled = false WHERE org_id = :o"),
                        {"o": org.org_id})
        await s.commit()
    await _sweep(app_engine, org.org_id, NOW)
    assert await _alerts(admin_engine, org.org_id) == {}
