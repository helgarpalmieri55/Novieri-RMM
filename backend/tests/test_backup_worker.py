"""Tests for the backup-engine worker sweeps (Task 13):

- ``backup_window_sweep_all_orgs`` -- per org, enumerate the org's devices and
  dispatch a signed ``backup.run`` for each via ``dispatch_backup`` (which
  itself resolves the device's effective policy and gates the maintenance
  window); publish per org only after that org's tenant transaction commits.
  Mirrors ``tests/test_deploy_worker.py``'s ``deploy_window_sweep_all_orgs``
  tests (monkeypatched-dispatch summation-invariant style, since the pg_cluster
  database is shared session-wide across every test file) plus a real
  closed-window case.
- ``backup_retention_gc_all_orgs`` -- the load-bearing PER-DEVICE GFS
  correctness surface: each device's points are bucketed against that device's
  OWN ``retention_gfs``; device A's extra points never starve device B. After
  marking superseded points ``pruned``, the now-orphaned chunks are collected
  by ``gc_repository`` while chunks still referenced by kept points survive.
- ``backup_verification_sweep_all_orgs`` -- verify never-verified ``complete``
  points and count how many pass.
- The Phase-4 un-masking test: the worker process must register ``backup.run``
  and ``backup.restore`` or its first window sweep raises ``UnknownJobType``.
"""

import datetime as _dt
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from blake3 import blake3
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings, override_settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.jobs.registry import UnknownJobType, get_job_type
from app.jobs.registry import _registry as _job_registry
from app.models import (
    BackupPoint,
    BackupPolicy,
    BackupRun,
    Device,
    DeviceGroup,
    Organization,
    User,
)
from app.modules.backup.gc import seal_chunk
from app.modules.backup.handler import register_job_types as register_backup_job_types
from app.modules.backup.repository import create_repository, open_repo_key
from app.modules.backup.restore import seal_manifest
from app.modules.backup.schema import BackupManifest, FiletreeStream, ManifestEntry

REDIS_URL = "redis://localhost:6379/9"

# The backup job-type registry is populated at app-startup time, not merely on
# import. Register directly so this file's own fixtures/assertions don't depend
# on import order (mirroring test_deploy_worker.py's module-level call). The
# un-masking test below explicitly pops these first, so this call does not mask
# the bug it proves.
register_backup_job_types()


def _settings(pg_cluster, tmp_path: Path) -> Settings:
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
        s3_bucket="it-suite-worker-test",
        vault_root_key_path=tmp_path / "vault-root.key",
    )


async def _make_org(admin_engine: AsyncEngine, slug_prefix: str) -> UUID:
    slug = f"{slug_prefix}-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


async def _make_windowed_device(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    vault: LocalVaultService,
    org_id: UUID,
    *,
    window_cron: str,
) -> tuple[UUID, UUID]:
    """A device in `org_id` under an enabled device-group backup policy whose
    window is `window_cron`, pointing at a real vault-sealed repository.
    Returns (device_id, repository_id)."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        group = DeviceGroup(org_id=org_id, name=f"bkw-{uuid4().hex[:6]}", client_id=org_id)
        session.add(group)
        await session.flush()
        device = Device(
    org_id=org_id,
    hostname="d1",
    os_family="linux",
    device_group_id=group.id,
    client_id=org_id,
    site_id=org_id,
        )
        session.add(device)
        await session.flush()
        user = User(
            org_id=org_id, external_subject=f"sub-{uuid4().hex[:6]}", email="a@b.c"
        )
        session.add(user)
        await session.flush()
        await session.commit()
        device_id, group_id, user_id = device.id, group.id, user.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(
            session, vault, org_id=org_id, actor_id=user_id, name="primary"
        )
        session.add(
            BackupPolicy(
                org_id=org_id,
                repository_id=repo.id,
                device_group_id=group_id,
                sources={"paths": ["/data"]},
                window_cron=window_cron,
                retention_gfs={"daily": 7},
                enabled=True,
            )
        )
        await session.commit()
        return device_id, repo.id


# ---------------------------------------------------------------------------
# backup_window_sweep_all_orgs
# ---------------------------------------------------------------------------


async def test_window_sweep_visits_all_orgs_and_sums_dispatch_backup_counts(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    """Two orgs, each with a device under an enabled windowed policy whose
    window is OPEN at `now`. Monkeypatching dispatch_due_backup isolates the
    assertion to the sweep's own fan-out wiring -- that it opens each org's
    session and calls dispatch_due_backup once per device -- without depending on
    real vault/signing across the many other orgs sitting in the shared
    pg_cluster database. Mirrors test_deploy_worker.py's summation invariant."""
    from app import worker

    calls: list[tuple[UUID, UUID]] = []

    async def fake_dispatch_backup(
        session,
        job_engine,
        vault,
        *,
        org_id,
        device_id,
        now,
    ):
        calls.append((org_id, device_id))
        return [(uuid4(), device_id, {"v": 1, "kind": "job", "job": {}, "sig": "x"})]

    published: list[UUID] = []

    async def fake_publish_job(redis, device_id, envelope):
        published.append(device_id)

    vault = LocalVaultService(tmp_path / "vault-root.key")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker, "dispatch_due_backup", fake_dispatch_backup)
        mp.setattr(worker, "publish_job", fake_publish_job)

        org_a = await _make_org(admin_engine, "bws-a")
        org_b = await _make_org(admin_engine, "bws-b")
        dev_a, _repo_a = await _make_windowed_device(
            admin_engine, app_engine, vault, org_a, window_cron="* * * * *"
        )
        dev_b, _repo_b = await _make_windowed_device(
            admin_engine, app_engine, vault, org_b, window_cron="* * * * *"
        )

        now = _dt.datetime(2026, 7, 19, 15, 30, tzinfo=_dt.UTC)
        with override_settings(_settings(pg_cluster, tmp_path)):
            total = await worker.backup_window_sweep_all_orgs(now)

    assert (org_a, dev_a) in calls
    assert (org_b, dev_b) in calls
    # one triple per device call, published post-commit
    assert total == len(calls)
    assert len(published) == len(calls)


async def test_closed_window_device_dispatches_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    """Real dispatch_due_backup path for THIS test's device: a device whose
    maintenance window is closed at `now` must be visited by the sweep but
    produce no BackupRun, since dispatch_due_backup's own due rule returns
    [] before it ever opens the repository key.

    The sweep enumerates every device in the shared session-wide pg_cluster
    database, including foreign orgs whose repositories were sealed with other
    per-test vault roots (a shared-DB test artifact -- in production all repos
    share one vault root). A delegating wrapper runs the REAL dispatch_due_backup
    for this test's org and neutralizes foreign orgs, so the real window gate is
    exercised for our device without foreign-vault decrypt failures."""
    from app import worker

    vault = LocalVaultService(tmp_path / "vault-root.key")
    org_id = await _make_org(admin_engine, "bws-closed")
    # fires only on 1 January: nothing is due within the look-back of `now`
    _device_id, _repo_id = await _make_windowed_device(
        admin_engine, app_engine, vault, org_id, window_cron="0 2 1 1 *"
    )

    real_dispatch = worker.dispatch_due_backup

    async def guarded_dispatch(
        session,
        job_engine,
        vlt,
        *,
        org_id,
        device_id,
        now,
    ):
        if org_id != globals()["_bws_closed_org"]:
            return []
        return await real_dispatch(
            session, job_engine, vlt,
            org_id=org_id, device_id=device_id, now=now,
        )

    now = _dt.datetime(2026, 7, 19, 15, 30, tzinfo=_dt.UTC)
    with pytest.MonkeyPatch.context() as mp:
        globals()["_bws_closed_org"] = org_id
        mp.setattr(worker, "dispatch_due_backup", guarded_dispatch)
        with override_settings(_settings(pg_cluster, tmp_path)):
            total = await worker.backup_window_sweep_all_orgs(now)

    assert total == 0  # our device's window is closed -> nothing dispatched
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        runs = (
            await session.execute(select(BackupRun).where(BackupRun.org_id == org_id))
        ).scalars().all()
        assert runs == []


# ---------------------------------------------------------------------------
# backup_retention_gc_all_orgs  -- per-device GFS correctness
# ---------------------------------------------------------------------------


async def _seed_chunk(
    object_store: ObjectStore,
    repo_key: bytes,
    org_id: UUID,
    repo_id: UUID,
    plaintext: bytes,
) -> str:
    cid = blake3(plaintext).hexdigest()
    await object_store.put_backup_chunk(org_id, repo_id, cid, seal_chunk(repo_key, plaintext))
    return cid


async def _seed_point(
    session: AsyncSession,
    object_store: ObjectStore,
    repo_key: bytes,
    *,
    org_id: UUID,
    device_id: UUID,
    repo_id: UUID,
    chunk_ids: list[str],
    taken_at: _dt.datetime,
    state: str = "complete",
) -> UUID:
    manifest = BackupManifest(
        schema_version=1,
        streams=[
            FiletreeStream(
                type="filetree",
                entries=[
                    ManifestEntry(
                        path=f"/data/{cid[:8]}",
                        kind="file",
                        mode=0o644,
                        size=len(cid),
                        mtime=1700000000.0,
                        chunk_ids=[cid],
                    )
                    for cid in chunk_ids
                ],
            )
        ],
    )
    ref = await object_store.put_backup_object(
        org_id, repo_id, f"manifests/{uuid4().hex}", seal_manifest(repo_key, manifest)
    )
    run = BackupRun(
        org_id=org_id,
        device_id=device_id,
        repository_id=repo_id,
        state="success",
        actor_type="system",
    )
    session.add(run)
    await session.flush()
    point = BackupPoint(
        org_id=org_id,
        device_id=device_id,
        repository_id=repo_id,
        backup_run_id=run.id,
        taken_at=taken_at,
        manifest_ref=ref,
        size=123,
        state=state,
    )
    session.add(point)
    await session.flush()
    return point.id


async def test_retention_gc_is_per_device_and_collects_orphan_chunks(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    """The load-bearing correctness test. ONE repo, ONE org-default policy
    (retention_gfs={"daily": 3}) resolved by BOTH device A and device B.

    Device A has 5 daily points (5 distinct dates); device B has 2. GFS is
    computed PER DEVICE, so A keeps its 3 newest dates and prunes its 2 oldest,
    while B (only 2 points, under the 3-day keep) is pruned NOTHING -- A's
    surplus never starves B. The chunks referenced solely by A's two pruned
    points then become orphans and are deleted by gc_repository, while chunks
    referenced by any kept point (A's newest 3, both of B's) survive.

    Each pruned A-point carries 2 distinct chunks, so the 2 pruned POINTS orphan
    4 chunks -- proving the sweep returns the pruned-point count (2), not the
    chunk-deletion count (4)."""
    from app import worker

    vault = LocalVaultService(tmp_path / "vault-root.key")

    # org + two group-less devices (both resolve the org-default policy)
    slug = f"bkret-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@b.c")
        session.add(user)
        dev_a = Device(
            org_id=org.id, hostname="a", os_family="linux", client_id=org.id, site_id=org.id
        )
        dev_b = Device(
            org_id=org.id, hostname="b", os_family="linux", client_id=org.id, site_id=org.id
        )
        session.add_all([dev_a, dev_b])
        await session.flush()
        await session.commit()
        org_id, user_id, device_a, device_b = org.id, user.id, dev_a.id, dev_b.id

    day = _dt.timedelta(days=1)
    base = _dt.datetime(2026, 7, 21, 12, 0, tzinfo=_dt.UTC)
    now = base

    with mock_aws():
        object_store = ObjectStore(_settings(pg_cluster, tmp_path))

        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            repo = await create_repository(
                session, vault, org_id=org_id, actor_id=user_id, name="primary"
            )
            repo_id = repo.id
            session.add(
                BackupPolicy(
                    org_id=org_id,
                    repository_id=repo_id,
                    device_group_id=None,  # org-default: resolved by both devices
                    sources={"paths": ["/data"]},
                    retention_gfs={"daily": 3},
                    enabled=True,
                )
            )
            await session.flush()
            repo_key = await open_repo_key(
                session, vault, org_id=org_id, repository_id=repo_id
            )

            # Device A: 5 points on 5 distinct dates (oldest .. newest).
            # The 2 oldest (to be pruned) each carry 2 unique chunks.
            a_prune_chunks: list[str] = []
            a_keep_chunks: list[str] = []
            a_points: list[UUID] = []
            for i in range(5):
                taken = base - (4 - i) * day  # i=0 oldest .. i=4 newest
                if i < 2:  # oldest two -> pruned; give each 2 chunks
                    c1 = await _seed_chunk(
                        object_store, repo_key, org_id, repo_id, f"a-{i}-x".encode()
                    )
                    c2 = await _seed_chunk(
                        object_store, repo_key, org_id, repo_id, f"a-{i}-y".encode()
                    )
                    a_prune_chunks += [c1, c2]
                    cids = [c1, c2]
                else:
                    c = await _seed_chunk(
                        object_store, repo_key, org_id, repo_id, f"a-{i}".encode()
                    )
                    a_keep_chunks.append(c)
                    cids = [c]
                a_points.append(
                    await _seed_point(
                        session, object_store, repo_key,
                        org_id=org_id, device_id=device_a, repo_id=repo_id,
                        chunk_ids=cids, taken_at=taken,
                    )
                )

            # Device B: 2 points, both under the 3-day keep -> pruned nothing.
            b_chunks: list[str] = []
            b_points: list[UUID] = []
            for i in range(2):
                taken = base - (1 - i) * day
                c = await _seed_chunk(
                    object_store, repo_key, org_id, repo_id, f"b-{i}".encode()
                )
                b_chunks.append(c)
                b_points.append(
                    await _seed_point(
                        session, object_store, repo_key,
                        org_id=org_id, device_id=device_b, repo_id=repo_id,
                        chunk_ids=[c], taken_at=taken,
                    )
                )
            await session.commit()

        before = set(await object_store.list_backup_chunks(org_id, repo_id))
        assert before == set(a_prune_chunks) | set(a_keep_chunks) | set(b_chunks)
        assert len(before) == 4 + 3 + 2  # 9 chunks

        with override_settings(_settings(pg_cluster, tmp_path)):
            # rebuild ObjectStore inside the same moto backend via the sweep
            pruned_total = await worker.backup_retention_gc_all_orgs(now)

        # Return semantics: pruned POINTS (2), not chunks deleted (4).
        assert pruned_total == 2

        # Per-device pruning: only A's 2 oldest are pruned; A keeps 3; B keeps 2.
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            states = {
                pid: (
                    await session.execute(
                        select(BackupPoint.state).where(BackupPoint.id == pid)
                    )
                ).scalar_one()
                for pid in a_points + b_points
            }
        assert states[a_points[0]] == "pruned"
        assert states[a_points[1]] == "pruned"
        assert all(states[p] == "complete" for p in a_points[2:])
        assert all(states[p] == "complete" for p in b_points)  # B never starved by A

        # GC deleted exactly the 4 orphaned (pruned-only) chunks; the rest live.
        after = set(await object_store.list_backup_chunks(org_id, repo_id))
        assert after == set(a_keep_chunks) | set(b_chunks)
        assert (before - after) == set(a_prune_chunks)


# ---------------------------------------------------------------------------
# backup_verification_sweep_all_orgs
# ---------------------------------------------------------------------------


async def test_verification_sweep_verifies_never_verified_points(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    """A `complete` point with verified_at IS NULL and intact chunks is verified
    (verified_at stamped, counted True); an already-verified point is excluded
    from the sweep's query and is neither re-checked nor re-counted (its
    original verified_at is left untouched)."""
    from app import worker

    vault = LocalVaultService(tmp_path / "vault-root.key")

    slug = f"bkver-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@b.c")
        session.add(user)
        device = Device(
            org_id=org.id, hostname="d", os_family="linux", client_id=org.id, site_id=org.id
        )
        session.add(device)
        await session.flush()
        await session.commit()
        org_id, user_id, device_id = org.id, user.id, device.id

    now = _dt.datetime(2026, 7, 21, 12, 0, tzinfo=_dt.UTC)
    already = _dt.datetime(2026, 7, 1, 8, 0, tzinfo=_dt.UTC)

    with mock_aws():
        object_store = ObjectStore(_settings(pg_cluster, tmp_path))
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            repo = await create_repository(
                session, vault, org_id=org_id, actor_id=user_id, name="primary"
            )
            repo_id = repo.id
            repo_key = await open_repo_key(
                session, vault, org_id=org_id, repository_id=repo_id
            )
            c1 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"v-good-1")
            c2 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"v-good-2")
            fresh_point = await _seed_point(
                session, object_store, repo_key,
                org_id=org_id, device_id=device_id, repo_id=repo_id,
                chunk_ids=[c1, c2], taken_at=now,
            )
            c3 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"v-old-1")
            verified_point = await _seed_point(
                session, object_store, repo_key,
                org_id=org_id, device_id=device_id, repo_id=repo_id,
                chunk_ids=[c3], taken_at=already,
            )
            # mark the second point already-verified at a distinct earlier time
            vp = (
                await session.execute(
                    select(BackupPoint).where(BackupPoint.id == verified_point)
                )
            ).scalar_one()
            vp.verified_at = already
            await session.commit()

        with override_settings(_settings(pg_cluster, tmp_path)):
            verified = await worker.backup_verification_sweep_all_orgs(now)

    # Only the never-verified point is counted. Foreign orgs' complete+
    # unverified points (sealed with other vault roots) fail to open and are
    # skipped, contributing nothing -- so exactly our one point is counted.
    assert verified == 1

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        fp = (
            await session.execute(select(BackupPoint).where(BackupPoint.id == fresh_point))
        ).scalar_one()
        assert fp.verified_at == now  # newly stamped

        vp = (
            await session.execute(
                select(BackupPoint).where(BackupPoint.id == verified_point)
            )
        ).scalar_one()
        assert vp.verified_at == already  # untouched: excluded from the sweep


# ---------------------------------------------------------------------------
# worker bootstrap registers backup job types (the Phase-4 un-masking test:
# the Celery worker+beat process never called register_job_types() for the
# backup module, so its first window sweep -- which creates a backup.run job
# via dispatch_backup -- would raise UnknownJobType. This file's own
# module-level register_backup_job_types() call above would mask that for every
# other test here, so this test explicitly deletes the registry entries first
# and drives the worker's own bootstrap path.
# ---------------------------------------------------------------------------


def test_worker_bootstrap_registers_backup_job_types() -> None:
    from app import worker

    for name in ("backup.run", "backup.restore"):
        _job_registry.pop(name, None)

    # fail-without-fix: without registration the sweep's job creation cannot
    # resolve the type -- this is exactly the crash the registration prevents.
    with pytest.raises(UnknownJobType):
        get_job_type("backup.run")
    with pytest.raises(UnknownJobType):
        get_job_type("backup.restore")

    worker.register_worker_job_types()

    assert get_job_type("backup.run").name == "backup.run"
    assert get_job_type("backup.restore").name == "backup.restore"


def test_create_celery_registers_backup_job_types(pg_cluster, tmp_path: Path) -> None:
    """create_celery() -- the real bootstrap path when the worker process builds
    its Celery app -- must also leave backup.run + backup.restore registered."""
    from app import worker

    for name in ("backup.run", "backup.restore"):
        _job_registry.pop(name, None)

    with pytest.raises(UnknownJobType):
        get_job_type("backup.run")

    with override_settings(_settings(pg_cluster, tmp_path)):
        worker.create_celery()

    assert get_job_type("backup.run").name == "backup.run"
    assert get_job_type("backup.restore").name == "backup.restore"
