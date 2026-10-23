"""Tests for repository GC (mark-and-sweep) and integrity verification
(docs/modules/backup/spec.md).

This is the load-bearing correctness test for the backup engine: a GC bug
deletes live backup data, and a verify bug reports corrupt backups as good.
Mirrors `tests/test_backup_restore.py`'s fixture style — a `moto`-backed
`ObjectStore` stands in for S3/MinIO, and chunks/manifests are sealed with the
real repository master key so the encrypt/decrypt and BLAKE3 round trips are
exercised, not stubbed.
"""

import datetime as _dt
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from blake3 import blake3
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import (
    AuditEvent,
    BackupPoint,
    BackupRun,
    Device,
    Organization,
    User,
)
from app.modules.backup.gc import (
    gc_repository,
    seal_chunk,
    verify_point,
)
from app.modules.backup.repository import create_repository, open_repo_key
from app.modules.backup.restore import seal_manifest
from app.modules.backup.schema import BackupManifest, FiletreeStream, ManifestEntry

_NOW = _dt.datetime(2026, 7, 21, 12, 0, tzinfo=_dt.UTC)


@pytest.fixture()
def object_store(tmp_path: Path):
    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-test",
    )
    with mock_aws():
        yield ObjectStore(settings)


async def _setup_base(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    vault: LocalVaultService,
) -> tuple[UUID, UUID, UUID, UUID]:
    """Create (org, user, device, repository) and return their ids
    (org_id, device_id, repository_id, user_id) with a real, vault-sealed
    repository master key."""
    slug = f"bkgc-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        session.add(user)
        await session.flush()

        device = Device(org_id=org.id, hostname="d1", os_family="linux")
        session.add(device)
        await session.flush()

        await session.commit()
        org_id, device_id, user_id = org.id, device.id, user.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(
            session, vault, org_id=org_id, actor_id=user_id, name="primary"
        )
        await session.commit()
        repo_id = repo.id

    return org_id, device_id, repo_id, user_id


async def _seed_chunk(
    object_store: ObjectStore,
    repo_key: bytes,
    org_id: UUID,
    repo_id: UUID,
    plaintext: bytes,
) -> str:
    """Seal `plaintext` and store it under its BLAKE3-256 content id. Returns
    the chunk id."""
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
    state: str,
) -> UUID:
    """Write a sealed manifest referencing `chunk_ids` (one entry per chunk),
    create its `BackupRun` + `BackupPoint(state=state)`, and return point id."""
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
        manifest_ref=ref,
        size=123,
        state=state,
    )
    session.add(point)
    await session.flush()
    return point.id


# ---------------------------------------------------------------------------
# gc_repository
# ---------------------------------------------------------------------------


async def test_gc_keeps_live_and_shared_chunks_deletes_pruned_only_and_orphan(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    """Point A (live) refs {c1,c2,c3}; point B (pruned) refs {c2,c3,c4}; c5 is
    an orphan referenced by no live manifest. GC must delete EXACTLY {c4,c5}
    and keep {c1,c2,c3} — c2/c3 are shared but still live via A, c1 is A-only,
    c4 is referenced solely by the pruned point."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, repo_id, _user_id = await _setup_base(admin_engine, app_engine, vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo_key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)

        c1 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"plaintext-1")
        c2 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"plaintext-2")
        c3 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"plaintext-3")
        c4 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"plaintext-4")
        c5 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"plaintext-5-orphan")

        await _seed_point(
            session, object_store, repo_key,
            org_id=org_id, device_id=device_id, repo_id=repo_id,
            chunk_ids=[c1, c2, c3], state="complete",
        )
        await _seed_point(
            session, object_store, repo_key,
            org_id=org_id, device_id=device_id, repo_id=repo_id,
            chunk_ids=[c2, c3, c4], state="pruned",
        )
        await session.commit()

    # sanity: all five chunks are present before GC
    before = set(await object_store.list_backup_chunks(org_id, repo_id))
    assert before == {c1, c2, c3, c4, c5}

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        deleted = await gc_repository(
            session, object_store, vault, org_id=org_id, repository_id=repo_id, now=_NOW
        )
        await session.commit()

    assert deleted == 2

    after = set(await object_store.list_backup_chunks(org_id, repo_id))
    # surviving set: exactly the live-referenced chunks
    assert after == {c1, c2, c3}
    # deleted set: pruned-only chunk + orphan
    assert (before - after) == {c4, c5}

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.gc")
            )
        ).scalar_one()
        assert event.actor_type == "system"
        assert event.actor_id is None
        assert event.target_type == "repository"
        assert event.target_id == str(repo_id)
        assert event.meta["deleted"] == 2
        assert event.meta["live_points"] == 1
        assert event.meta["referenced_chunks"] == 3
        # invariant #3: no chunk ids / data in audit metadata
        meta_str = str(event.meta)
        for cid in (c1, c2, c3, c4, c5):
            assert cid not in meta_str


async def test_gc_missing_repository_fails_closed(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, _device_id, _repo_id, _user_id = await _setup_base(admin_engine, app_engine, vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(NoResultFound):
            await gc_repository(
                session, object_store, vault, org_id=org_id, repository_id=uuid4(), now=_NOW
            )


# ---------------------------------------------------------------------------
# verify_point
# ---------------------------------------------------------------------------


async def test_verify_point_good_sets_verified_at_and_returns_true(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, repo_id, _user_id = await _setup_base(admin_engine, app_engine, vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo_key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)
        c1 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"good-1")
        c2 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"good-2")
        point_id = await _seed_point(
            session, object_store, repo_key,
            org_id=org_id, device_id=device_id, repo_id=repo_id,
            chunk_ids=[c1, c2], state="complete",
        )
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        ok = await verify_point(
            session, object_store, vault, org_id=org_id, backup_point_id=point_id, now=_NOW
        )
        await session.commit()

    assert ok is True

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        point = (
            await session.execute(select(BackupPoint).where(BackupPoint.id == point_id))
        ).scalar_one()
        assert point.verified_at is not None

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.verify")
            )
        ).scalar_one()
        assert event.actor_type == "system"
        assert event.actor_id is None
        assert event.target_type == "backup_point"
        assert event.target_id == str(point_id)
        assert event.meta["ok"] is True
        assert event.meta["checked"] == 2


async def test_verify_point_corrupt_chunk_returns_false_and_does_not_stamp(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    """Overwriting a referenced chunk object with garbage bytes (so its stored
    content no longer decrypts to something whose BLAKE3 matches its id) must
    make verify FAIL, leave `verified_at` None, and audit `{"ok": false}`."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, repo_id, _user_id = await _setup_base(admin_engine, app_engine, vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo_key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)
        c1 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"corruptible-1")
        c2 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"corruptible-2")
        point_id = await _seed_point(
            session, object_store, repo_key,
            org_id=org_id, device_id=device_id, repo_id=repo_id,
            chunk_ids=[c1, c2], state="complete",
        )
        await session.commit()

    # corrupt c1's object in place (keep its id/key, replace bytes with garbage)
    await object_store.put_backup_chunk(org_id, repo_id, c1, b"\x00garbage-not-sealed")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        ok = await verify_point(
            session, object_store, vault, org_id=org_id, backup_point_id=point_id, now=_NOW
        )
        await session.commit()

    assert ok is False

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        point = (
            await session.execute(select(BackupPoint).where(BackupPoint.id == point_id))
        ).scalar_one()
        assert point.verified_at is None

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.verify")
            )
        ).scalar_one()
        assert event.meta["ok"] is False


async def test_verify_point_missing_chunk_returns_false(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    """A referenced chunk whose object is absent must fail verification."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, repo_id, _user_id = await _setup_base(admin_engine, app_engine, vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo_key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)
        c1 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"present-1")
        c2 = await _seed_chunk(object_store, repo_key, org_id, repo_id, b"to-be-deleted-2")
        point_id = await _seed_point(
            session, object_store, repo_key,
            org_id=org_id, device_id=device_id, repo_id=repo_id,
            chunk_ids=[c1, c2], state="complete",
        )
        await session.commit()

    await object_store.delete_backup_chunk(org_id, repo_id, c2)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        ok = await verify_point(
            session, object_store, vault, org_id=org_id, backup_point_id=point_id, now=_NOW
        )
        await session.commit()

    assert ok is False

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        point = (
            await session.execute(select(BackupPoint).where(BackupPoint.id == point_id))
        ).scalar_one()
        assert point.verified_at is None


async def test_verify_point_missing_point_fails_closed(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, _device_id, _repo_id, _user_id = await _setup_base(admin_engine, app_engine, vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(NoResultFound):
            await verify_point(
                session, object_store, vault, org_id=org_id, backup_point_id=uuid4(), now=_NOW
            )


# ---------------------------------------------------------------------------
# tenancy
# ---------------------------------------------------------------------------


async def test_gc_never_touches_another_orgs_chunks(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    """Org B seeds chunks that would be pure orphans in ITS prefix. Running GC
    for org A must not list, read, or delete anything under org B's prefix —
    B's chunks all survive A's GC."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_a, device_a, repo_a, _user_a = await _setup_base(admin_engine, app_engine, vault)
    org_b, device_b, repo_b, _user_b = await _setup_base(admin_engine, app_engine, vault)

    # org A: one live point refs {a1}; a2 is an orphan in A's prefix
    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        key_a = await open_repo_key(session, vault, org_id=org_a, repository_id=repo_a)
        a1 = await _seed_chunk(object_store, key_a, org_a, repo_a, b"a-live-1")
        # a-orphan-2: referenced by no live point in A -> A's GC should delete it
        await _seed_chunk(object_store, key_a, org_a, repo_a, b"a-orphan-2")
        await _seed_point(
            session, object_store, key_a,
            org_id=org_a, device_id=device_a, repo_id=repo_a,
            chunk_ids=[a1], state="complete",
        )
        await session.commit()

    # org B: chunks referenced by NO live point in B (pure orphans in B's prefix)
    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
        key_b = await open_repo_key(session, vault, org_id=org_b, repository_id=repo_b)
        b1 = await _seed_chunk(object_store, key_b, org_b, repo_b, b"b-1")
        b2 = await _seed_chunk(object_store, key_b, org_b, repo_b, b"b-2")
        await session.commit()

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        deleted = await gc_repository(
            session, object_store, vault, org_id=org_a, repository_id=repo_a, now=_NOW
        )
        await session.commit()

    # A's GC deletes only A's orphan (a2), keeps A's live chunk (a1)
    assert deleted == 1
    after_a = set(await object_store.list_backup_chunks(org_a, repo_a))
    assert after_a == {a1}

    # org B's chunks are entirely untouched despite being orphans in B's prefix
    after_b = set(await object_store.list_backup_chunks(org_b, repo_b))
    assert after_b == {b1, b2}
