"""The push-run beat: the backstop that continues confirmed runs every 60 s
(design §1.4). It never starts, resumes or widens a run. It fills capacity
freed by jobs that ended without reaching the gateway's result handler, and
capacity the gateway cannot fill because it holds no vault.

The shared test database also holds other tests' orgs, so every assertion is
filtered to this test's own site server device.
"""

from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings, override_settings
from app.core.tenancy import org_session
from tests.conftest import PgCluster
from tests.push_run_fixtures import CONSOLE, FakeStore, FakeVault, run_world, start_run


@pytest.fixture(autouse=True)
def _register_push_job_types() -> None:
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


def _settings(pg_cluster: PgCluster, tmp_path: Path) -> Settings:
    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
        public_console_url=CONSOLE,
    )


@pytest.fixture()
def published(monkeypatch) -> list[tuple[Any, dict[str, Any]]]:
    from app import worker

    sent: list[tuple[Any, dict[str, Any]]] = []

    async def _publish(redis: Any, device_id: Any, envelope: dict[str, Any]) -> None:
        sent.append((device_id, envelope))

    monkeypatch.setattr(worker, "publish_job", _publish)
    monkeypatch.setattr(worker, "get_vault", lambda settings: FakeVault())
    monkeypatch.setattr(worker, "ObjectStore", lambda settings: FakeStore())
    return sent


async def test_the_beat_continues_a_running_run(
    admin_engine, app_engine, pg_cluster: PgCluster, tmp_path: Path, published
) -> None:
    from app import worker

    world = await run_world(admin_engine, "rt-go", octet=121, count=2)
    await start_run(app_engine, world)
    with override_settings(_settings(pg_cluster, tmp_path)):
        total = await worker.push_run_tick_all_orgs()
    mine = [e for device, e in published if device == world.site_server_device_id]
    # One first (spec §1.4a): the beat, like every caller, sends ONE target
    # until a sign-in has got through.
    assert len(mine) == 1 and total >= 1
    assert mine[0]["job"]["params"]["target_address"] == world.addresses[0]


async def test_a_cancelled_run_is_not_continued_by_the_beat(
    admin_engine, app_engine, pg_cluster: PgCluster, tmp_path: Path, published
) -> None:
    from app import worker
    from app.modules.agent_push import runs

    world = await run_world(admin_engine, "rt-cxl", octet=122, count=2)
    run_id = await start_run(app_engine, world)
    async with org_session(world.org.org_id, engine=app_engine) as session:
        await runs.cancel_run(session, world.org.org_id, run_id=run_id, actor_id=world.org.user_id)
    with override_settings(_settings(pg_cluster, tmp_path)):
        await worker.push_run_tick_all_orgs()
    assert [d for d, _e in published if d == world.site_server_device_id] == []


async def test_one_failing_org_does_not_stop_the_others(
    admin_engine, app_engine, pg_cluster: PgCluster, tmp_path: Path, published, monkeypatch
) -> None:
    from app import worker
    from app.modules.agent_push import runs

    broken = await run_world(admin_engine, "rt-bad", octet=123, count=1)
    healthy = await run_world(admin_engine, "rt-good", octet=124, count=1)
    await start_run(app_engine, broken)
    await start_run(app_engine, healthy)
    real_tick = runs.tick

    async def _flaky(session: Any, org_id: Any, **kw: Any) -> Any:
        if org_id == broken.org.org_id:
            raise RuntimeError("this org's pass fails")
        return await real_tick(session, org_id, **kw)

    monkeypatch.setattr(runs, "tick", _flaky)
    with override_settings(_settings(pg_cluster, tmp_path)):
        await worker.push_run_tick_all_orgs()
    assert [d for d, _e in published if d == healthy.site_server_device_id] == [
        healthy.site_server_device_id
    ]
    assert [d for d, _e in published if d == broken.site_server_device_id] == []


def test_the_beat_entry_runs_every_minute(pg_cluster: PgCluster, tmp_path: Path) -> None:
    from app import worker
    from app.modules.agent_push import runs

    with override_settings(_settings(pg_cluster, tmp_path)):
        entry = worker.create_celery().conf.beat_schedule["push-run-tick"]
    assert entry["task"] == "app.worker.push_run_tick"
    assert entry["schedule"] == runs.TICK_INTERVAL_S == 60.0
    assert entry["options"]["expires"] == 60.0
