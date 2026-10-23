"""Tests for restore-side backup dispatch and manifest browsing (docs/modules/
backup/spec.md).

Mirrors `tests/test_backup_dispatch.py`'s fixture style: a fake job engine
stands in for the kernel job engine, and a `moto`-backed `ObjectStore` stands
in for S3/MinIO. The manifest object a `BackupPoint` points at is sealed with
`seal_manifest` (the same codec `browse_manifest` uses via `open_manifest`) so
these tests exercise the real encrypt/decrypt round trip, not a stub.
"""

import base64
import datetime as _dt
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

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
    RestoreJob,
    User,
)
from app.modules.backup.repository import create_repository, open_repo_key
from app.modules.backup.restore import browse_manifest, dispatch_restore, seal_manifest
from app.modules.backup.schema import BackupManifest, FiletreeStream, ManifestEntry

_KNOWN_MANIFEST = BackupManifest(
    schema_version=1,
    streams=[
        FiletreeStream(
            type="filetree",
            entries=[
                ManifestEntry(
                    path="/data/file.txt",
                    kind="file",
                    mode=0o644,
                    size=123,
                    mtime=1700000000.0,
                    chunk_ids=["chunk-a", "chunk-b"],
                ),
                ManifestEntry(
                    path="/data/dir",
                    kind="dir",
                    mode=0o755,
                    size=0,
                    mtime=1700000000.0,
                ),
            ],
        )
    ],
)


class _FakeJobEngine:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, session, **kw):
        from app.models import Job

        job = Job(
            org_id=kw["org_id"],
            type=kw["type"],
            target_type="device",
            target_id=kw["target_device_id"],
            params=kw["params"],
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
            requested_by=kw.get("requested_by"),
        )
        session.add(job)
        await session.flush()
        # Mirror the real JobEngine.create: sign at create time.
        job.signature = "fake-signature"
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job):
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": job.signature}


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


async def _setup(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    vault: LocalVaultService,
    object_store: ObjectStore,
) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    """Returns (org_id, device_id, repository_id, user_id, backup_point_id):
    a real, vault-sealed repository and a completed `BackupPoint` whose
    `manifest_ref` is a sealed, known filetree manifest in object storage."""
    slug = f"bkrestore-{uuid4().hex[:6]}"
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

    async with org_session(org_id, engine=app_engine) as session:
        repo = await create_repository(
            session, vault, org_id=org_id, actor_id=user_id, name="primary"
        )
        await session.commit()
        repo_id = repo.id

    async with org_session(org_id, engine=app_engine) as session:
        key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)
        blob = seal_manifest(key, _KNOWN_MANIFEST)
        manifest_ref = await object_store.put_backup_object(
            org_id, repo_id, f"manifests/{uuid4().hex}", blob
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
            manifest_ref=manifest_ref,
            size=123,
            state="complete",
        )
        session.add(point)
        await session.flush()
        await session.commit()
        point_id = point.id

    return org_id, device_id, repo_id, user_id, point_id


# ---------------------------------------------------------------------------
# browse_manifest
# ---------------------------------------------------------------------------


async def test_browse_manifest_round_trips_known_filetree(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, _device_id, _repo_id, _user_id, point_id = await _setup(
        admin_engine, app_engine, vault, object_store
    )

    async with org_session(org_id, engine=app_engine) as session:
        manifest = await browse_manifest(
            session, object_store, vault, org_id=org_id, backup_point_id=point_id
        )

    assert manifest == _KNOWN_MANIFEST
    assert manifest.schema_version == 1
    entries = manifest.streams[0].entries
    assert [e.path for e in entries] == ["/data/file.txt", "/data/dir"]
    assert entries[0].chunk_ids == ["chunk-a", "chunk-b"]


async def test_browse_manifest_raises_not_found_for_another_orgs_point_under_rls(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    _org_a, _device_a, _repo_a, _user_a, point_a = await _setup(
        admin_engine, app_engine, vault, object_store
    )
    org_b, _device_b, _repo_b, _user_b, _point_b = await _setup(
        admin_engine, app_engine, vault, object_store
    )

    async with org_session(org_b, engine=app_engine) as session:
        with pytest.raises(NoResultFound):
            await browse_manifest(
                session, object_store, vault, org_id=org_b, backup_point_id=point_a
            )


# ---------------------------------------------------------------------------
# dispatch_restore
# ---------------------------------------------------------------------------


async def test_dispatch_restore_creates_signed_job_and_restore_job_row(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, repo_id, user_id, point_id = await _setup(
        admin_engine, app_engine, vault, object_store
    )
    now = _dt.datetime(2026, 7, 21, 12, 0)

    async with org_session(org_id, engine=app_engine) as session:
        raw_key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)

    async with org_session(org_id, engine=app_engine) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_restore(
            session,
            job_engine,
            vault,
            org_id=org_id,
            backup_point_id=point_id,
            selection=["/data/file.txt"],
            target={"path": "/restore/dest"},
            mode="restore",
            now=now,
            actor_id=user_id,
            actor_type="user",
        )

        assert len(pending) == 1
        job_id, dispatched_device_id, envelope = pending[0]
        assert dispatched_device_id == device_id
        assert envelope["sig"] == "fake-signature"

        assert len(job_engine.created) == 1
        assert job_engine.created[0]["type"] == "backup.restore"
        assert job_engine.created[0]["target_device_id"] == device_id
        assert job_engine.created[0]["requested_by"] == user_id
        # Long enough for a start within the hour plus a full run (D3). The
        # restore wire defines no start deadline; the read exchange derives it.
        assert job_engine.created[0]["ttl_s"] == 25 * 3600
        assert "not_after_start" not in job_engine.created[0]["params"]

        from app.models import Job

        signed_job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert signed_job.signature is not None
        assert signed_job.params["repo_key"] == base64.b64encode(raw_key).decode()
        assert signed_job.params["mode"] == "restore"
        assert signed_job.params["manifest_ref"]
        assert signed_job.params["selection"] == ["/data/file.txt"]
        assert signed_job.params["target"] == {"path": "/restore/dest"}

        restore_job = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job_id))
        ).scalar_one()
        assert restore_job.state == "running"
        assert restore_job.device_id == device_id
        assert restore_job.backup_point_id == point_id
        assert restore_job.mode == "restore"
        assert restore_job.selection == {"paths": ["/data/file.txt"]}
        assert restore_job.target == {"path": "/restore/dest"}
        assert restore_job.actor_type == "user"
        assert restore_job.requested_by == user_id

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.restore_dispatched")
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == user_id
        meta_str = str(event.meta)
        assert raw_key.hex() not in meta_str
        assert base64.b64encode(raw_key).decode() not in meta_str
        assert "repo_key" not in event.meta


async def test_dispatch_restore_verify_mode_sets_restore_job_mode(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, _repo_id, user_id, point_id = await _setup(
        admin_engine, app_engine, vault, object_store
    )
    now = _dt.datetime(2026, 7, 21, 12, 0)

    async with org_session(org_id, engine=app_engine) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_restore(
            session,
            job_engine,
            vault,
            org_id=org_id,
            backup_point_id=point_id,
            selection=["/data/file.txt"],
            target={"path": "/restore/dest"},
            mode="verify",
            now=now,
            actor_id=user_id,
            actor_type="user",
        )

        assert len(pending) == 1
        job_id, _device_id, _envelope = pending[0]

        restore_job = (
            await session.execute(select(RestoreJob).where(RestoreJob.job_id == job_id))
        ).scalar_one()
        assert restore_job.mode == "verify"


async def test_dispatch_restore_finds_no_point_for_another_orgs_id_under_rls(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    """A backup point belonging to org A must not be dispatchable under org
    B's session — the point lookup is itself org-scoped."""
    vault = LocalVaultService(tmp_path / "root.key")
    _org_a, _device_a, _repo_a, _user_a, point_a = await _setup(
        admin_engine, app_engine, vault, object_store
    )
    org_b, _device_b, _repo_b, user_b, _point_b = await _setup(
        admin_engine, app_engine, vault, object_store
    )
    now = _dt.datetime(2026, 7, 21, 12, 0)

    async with org_session(org_b, engine=app_engine) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_restore(
            session,
            job_engine,
            vault,
            org_id=org_b,
            backup_point_id=point_a,  # org A's point id, under org B's session
            selection=["/data/file.txt"],
            target={"path": "/restore/dest"},
            mode="restore",
            now=now,
            actor_id=user_b,
            actor_type="user",
        )
        assert pending == []
        assert job_engine.created == []
        restore_jobs = (await session.execute(select(RestoreJob))).scalars().all()
        assert restore_jobs == []


async def test_dispatch_restore_does_not_publish(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    object_store: ObjectStore,
) -> None:
    """dispatch_restore must return triples for the caller to publish
    post-commit rather than publishing itself — mirrors dispatch_backup."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, _repo_id, user_id, point_id = await _setup(
        admin_engine, app_engine, vault, object_store
    )
    now = _dt.datetime(2026, 7, 21, 12, 0)

    async with org_session(org_id, engine=app_engine) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_restore(
            session,
            job_engine,
            vault,
            org_id=org_id,
            backup_point_id=point_id,
            selection=["/data/file.txt"],
            target={"path": "/restore/dest"},
            mode="restore",
            now=now,
            actor_id=user_id,
        )
        assert isinstance(pending, list)
        assert len(pending) == 1
        job_id, returned_device_id, envelope = pending[0]
        assert isinstance(job_id, UUID)
        assert returned_device_id == device_id
        assert isinstance(envelope, dict)
