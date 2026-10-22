"""The worker shells for the off-site copy: a requested test runs in the
worker (the only process that decrypts the target credential), then the
replication sweep copies a point end to end, and a point not yet copied is
held by local retention."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from moto import mock_aws
from sqlalchemy import select

from app.core.config import Settings, override_settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import BackupOffsiteTarget, BackupPointCopy
from tests.backup_offsite_fixtures import (
    add_point,
    add_proven_key,
    add_target,
    make_buckets,
    moto_client_factory,
    seed_org,
)

REDIS_URL = "redis://localhost:6379/9"


def _settings(pg_cluster: Any, tmp_path: Path) -> Settings:
    return Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url=REDIS_URL,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-offsite-worker",
        vault_root_key_path=tmp_path / "vault-root.key",
    )


async def test_the_worker_tests_the_target_then_replicates_a_point(
    admin_engine, app_engine, pg_cluster, tmp_path: Path
) -> None:
    from app import worker
    from app.modules.backup import offsite

    settings = _settings(pg_cluster, tmp_path)
    vault = LocalVaultService(tmp_path / "vault-root.key")
    with mock_aws(), pytest.MonkeyPatch.context() as mp:
        # Every target in the shared test database resolves to moto, whatever
        # endpoint another test gave it.
        mp.setattr(offsite, "build_client", moto_client_factory)
        make_buckets()
        store = ObjectStore(settings)
        org = await seed_org(admin_engine, app_engine, vault, "osw")
        await add_proven_key(app_engine, org)
        target_id = await add_target(app_engine, vault, org, state="testing", attach=False)
        async with org_session(org.org_id, engine=app_engine) as session:
            target = (await session.execute(select(BackupOffsiteTarget).where(
                BackupOffsiteTarget.id == target_id))).scalar_one()
            target.test_requested_at = datetime.now(UTC)

        with override_settings(settings):
            await worker.backup_offsite_test_all_orgs(datetime.now(UTC))

        async with org_session(org.org_id, engine=app_engine) as session:
            target = (await session.execute(select(BackupOffsiteTarget).where(
                BackupOffsiteTarget.id == target_id))).scalar_one()
            assert target.state == "ok", target.last_test
            assert target.test_requested_at is None
            from app.modules.backup.offsite import set_repository_target

            await set_repository_target(session, org_id=org.org_id,
                                        repository_id=org.repository_id,
                                        target_id=target_id, actor_id=org.user_id)
        point_id, _ = await add_point(app_engine, store, org, {"/etc/hosts": [b"127.0.0.1"]})

        with override_settings(settings):
            await worker.backup_replication_all_orgs(datetime.now(UTC))

        async with org_session(org.org_id, engine=app_engine) as session:
            copy = (await session.execute(select(BackupPointCopy).where(
                BackupPointCopy.point_id == point_id))).scalar_one()
    assert copy.state == "complete"


def test_the_off_site_sweeps_run_on_their_own_queue(pg_cluster, tmp_path: Path) -> None:
    from app import worker

    with override_settings(_settings(pg_cluster, tmp_path)):
        schedule = worker.create_celery().conf.beat_schedule
    for name in ("backup-replication", "backup-offsite-test", "backup-offsite-gc"):
        assert schedule[name]["options"]["queue"] == worker.BACKUP_REPLICATION_QUEUE
    assert "queue" not in schedule["backup-reconcile"].get("options", {})
