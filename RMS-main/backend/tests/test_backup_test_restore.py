"""Scheduled test restores (spec-server-backup.md §7.4; operator decision
2026-09-23: files monthly on a sample)."""

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from moto import mock_aws
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent, BackupPolicy, RestoreJob
from app.modules.backup.schema import BackupManifest, FiletreeStream, ManifestEntry
from app.modules.backup.test_restore import (
    SAMPLE_BYTES,
    TEST_RESTORE_EVERY,
    dispatch_due_test_restores,
    sample_selection,
)
from tests.backup_offsite_fixtures import add_point, local_settings, seed_org

NOW = datetime.now(UTC)


def _manifest(n: int, size: int) -> BackupManifest:
    return BackupManifest(schema_version=1, streams=[FiletreeStream(type="filetree", entries=[
        ManifestEntry(path=f"/d/f{i}", kind="file", mode=0o644, size=size, mtime=1.0)
        for i in range(n)
    ] + [ManifestEntry(path="/d", kind="dir", mode=0o755, size=0, mtime=1.0)])])


def test_the_sample_is_at_most_200_files() -> None:
    chosen = sample_selection(_manifest(1000, 10), rng=random.Random(1))
    assert len(chosen) == 200
    assert "/d" not in chosen


def test_the_sample_is_capped_at_5_gib() -> None:
    chosen = sample_selection(_manifest(10, 2 * 1024**3), rng=random.Random(1))
    assert len(chosen) == 2
    assert SAMPLE_BYTES == 5 * 1024**3


class _FakeJobEngine:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create(self, session: Any, **kw: Any) -> Any:
        from app.models import Job

        job = Job(org_id=kw["org_id"], type=kw["type"], target_type="device",
                  target_id=kw["target_device_id"], params=kw["params"], nonce=uuid4().hex,
                  issued_at=NOW, expires_at=NOW + timedelta(seconds=kw["ttl_s"]))
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job: Any) -> dict[str, Any]:
        return {"job": {"id": str(job.id)}}


@pytest.fixture()
def store(tmp_path: Path):
    with mock_aws():
        yield ObjectStore(local_settings(tmp_path))


async def _protected(admin_engine: AsyncEngine, app_engine: AsyncEngine, vault: Any) -> Any:
    org = await seed_org(admin_engine, app_engine, vault, "tr")
    async with AsyncSession(admin_engine) as s:
        s.add(BackupPolicy(org_id=org.org_id, repository_id=org.repository_id,
                           sources={"paths": ["/"]}, window_cron=None, retention_gfs={},
                           enabled=True))
        await s.commit()
    return org


async def _run(app_engine: AsyncEngine, vault: Any, store: ObjectStore, org: Any,
               engine: _FakeJobEngine, now: datetime) -> list[Any]:
    async with org_session(org.org_id, engine=app_engine) as session:
        return await dispatch_due_test_restores(session, engine, vault, store,
                                                org_id=org.org_id, now=now,
                                                rng=random.Random(7))


async def test_an_untested_device_gets_a_sampled_verify_restore(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, store: ObjectStore, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "k")
    org = await _protected(admin_engine, app_engine, vault)
    await add_point(app_engine, store, org, {"/srv/a": [b"a"], "/srv/b": [b"b"]})
    engine = _FakeJobEngine()

    pending = await _run(app_engine, vault, store, org, engine, NOW)

    assert len(pending) == 1
    params = engine.created[0]["params"]
    assert params["mode"] == "verify"
    assert params["target"] == {}
    assert sorted(params["selection"]) == ["/srv/a", "/srv/b"]
    assert engine.created[0]["requested_by"] is None
    async with AsyncSession(admin_engine) as s:
        actions = (await s.execute(select(AuditEvent.action).where(
            AuditEvent.org_id == org.org_id))).scalars().all()
    assert "backup.test_restore_dispatched" in actions
    # A running test blocks a second one.
    assert await _run(app_engine, vault, store, org, engine, NOW) == []


async def test_a_recently_tested_device_is_left_alone(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, store: ObjectStore, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "k")
    org = await _protected(admin_engine, app_engine, vault)
    point_id, _ = await add_point(app_engine, store, org, {"/srv/a": [b"a"]})
    async with AsyncSession(admin_engine) as s:
        await s.execute(text("UPDATE backup_points SET restore_tested_at = :t WHERE id = :p"),
                        {"t": NOW - TEST_RESTORE_EVERY + timedelta(days=1), "p": point_id})
        await s.commit()
    assert await _run(app_engine, vault, store, org, _FakeJobEngine(), NOW) == []
    later = NOW + timedelta(days=2)
    assert len(await _run(app_engine, vault, store, org, _FakeJobEngine(), later)) == 1


async def test_a_point_larger_than_the_sample_cap_is_not_test_restored(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, store: ObjectStore, tmp_path: Path
) -> None:
    # A released agent verifies the whole point; do not fill the server's disk.
    vault = LocalVaultService(tmp_path / "k")
    org = await _protected(admin_engine, app_engine, vault)
    point_id, _ = await add_point(app_engine, store, org, {"/srv/a": [b"a"]})
    async with AsyncSession(admin_engine) as s:
        await s.execute(text("UPDATE backup_points SET size = :n WHERE id = :p"),
                        {"n": SAMPLE_BYTES + 1, "p": point_id})
        await s.commit()
    assert await _run(app_engine, vault, store, org, _FakeJobEngine(), NOW) == []
    async with AsyncSession(admin_engine) as s:
        assert (await s.execute(select(RestoreJob).where(
            RestoreJob.org_id == org.org_id))).scalars().all() == []
