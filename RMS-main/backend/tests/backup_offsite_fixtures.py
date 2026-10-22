"""Shared seeding for the off-site copy tests: a moto-backed local store, an
Object Lock bucket, an org with a repository, points whose chunks really exist
locally, and the standalone recovery tool loaded from `scripts/`."""

import importlib.util
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import boto3
from blake3 import blake3
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import BackupPoint, BackupRun, Device, Organization, User
from app.modules.backup.gc import seal_chunk
from app.modules.backup.offsite import OffsiteSecret, create_target
from app.modules.backup.recovery import prove_recovery_key, register_recovery_key
from app.modules.backup.repository import create_repository, open_repo_key
from app.modules.backup.restore import seal_manifest
from app.modules.backup.schema import BackupManifest, FiletreeStream, ManifestEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_BUCKET = "offsite-locked"
PLAIN_BUCKET = "offsite-nolock"


def load_recover_tool() -> ModuleType:
    name = "itms_recover_tool"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / "itms_recover.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def local_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-test",
    )


def make_buckets() -> Any:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=LOCK_BUCKET, ObjectLockEnabledForBucket=True)
    s3.create_bucket(Bucket=PLAIN_BUCKET)
    return s3


def moto_client_factory(target: Any, secret: OffsiteSecret) -> Any:
    return boto3.client(
        "s3",
        region_name=target.region,
        aws_access_key_id=secret.access_key_id,
        aws_secret_access_key=secret.secret_access_key,
    )


@dataclass
class OffsiteOrg:
    org_id: UUID
    user_id: UUID
    device_id: UUID
    repository_id: UUID
    repo_key: bytes
    private_key: bytes


async def seed_org(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault: LocalVaultService, tag: str
) -> OffsiteOrg:
    slug = f"{tag}-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@x.test")
        device = Device(org_id=org.id, hostname=f"fs-{slug}", os_family="linux")
        s.add_all([user, device])
        await s.commit()
        org_id, user_id, device_id = org.id, user.id, device.id
    async with org_session(org_id, engine=app_engine) as session:
        repo = await create_repository(session, vault, org_id=org_id, actor_id=user_id,
                                       name="primary")
        repo_id = repo.id
    async with org_session(org_id, engine=app_engine) as session:
        key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)
    return OffsiteOrg(org_id, user_id, device_id, repo_id, key, b"")


async def add_proven_key(app_engine: AsyncEngine, org: OffsiteOrg) -> None:
    tool = load_recover_tool()
    private_text, public_text = tool.keygen()
    org.private_key = tool.load_private_key(private_text)
    now = datetime.now(UTC)
    async with org_session(org.org_id, engine=app_engine) as session:
        row, challenge = await register_recovery_key(
            session, org_id=org.org_id, actor_id=org.user_id, public_key_text=public_text, now=now
        )
        await prove_recovery_key(session, row, answer=tool.prove(org.private_key, challenge),
                                 actor_id=org.user_id, now=now)


async def add_target(
    app_engine: AsyncEngine, vault: LocalVaultService, org: OffsiteOrg, *,
    bucket: str = LOCK_BUCKET, state: str = "ok", retention_gfs: dict[str, int] | None = None,
    attach: bool = True,
) -> UUID:
    async with org_session(org.org_id, engine=app_engine) as session:
        target = await create_target(
            session, vault, org_id=org.org_id, actor_id=org.user_id,
            settings={"name": f"t-{uuid4().hex[:6]}", "bucket": bucket, "prefix": "itms",
                      "retention_gfs": retention_gfs or {}},
            secret=OffsiteSecret("offsite-ak", "offsite-sk"),
        )
        target.state = state
        if attach:
            from sqlalchemy import select

            from app.models import BackupRepository

            repo = (await session.execute(select(BackupRepository).where(
                BackupRepository.id == org.repository_id))).scalar_one()
            repo.offsite_target_id = target.id
        return target.id


async def add_point(
    app_engine: AsyncEngine,
    store: ObjectStore,
    org: OffsiteOrg,
    files: dict[str, list[bytes]],
    *,
    taken_at: datetime | None = None,
) -> tuple[UUID, dict[str, bytes]]:
    """A complete point whose manifest lists `files` (path -> chunk plaintexts);
    every chunk is sealed and stored locally. Returns (point id, path -> bytes)."""
    entries = []
    contents: dict[str, bytes] = {}
    for path, chunks in files.items():
        ids = []
        for plain in chunks:
            cid = blake3(plain).hexdigest()
            await store.put_backup_chunk(org.org_id, org.repository_id, cid,
                                         seal_chunk(org.repo_key, plain))
            ids.append(cid)
        data = b"".join(chunks)
        contents[path] = data
        entries.append(ManifestEntry(path=path, kind="file", mode=0o644, size=len(data),
                                     mtime=1.0, chunk_ids=ids))
    manifest = BackupManifest(schema_version=1,
                              streams=[FiletreeStream(type="filetree", entries=entries)])
    ref = await store.put_backup_object(org.org_id, org.repository_id,
                                        f"manifests/{uuid4().hex}",
                                        seal_manifest(org.repo_key, manifest))
    async with org_session(org.org_id, engine=app_engine) as session:
        run = BackupRun(org_id=org.org_id, device_id=org.device_id,
                        repository_id=org.repository_id, state="success", actor_type="system")
        session.add(run)
        await session.flush()
        point = BackupPoint(org_id=org.org_id, device_id=org.device_id,
                            repository_id=org.repository_id, backup_run_id=run.id,
                            manifest_ref=ref, size=sum(len(v) for v in contents.values()),
                            state="complete", taken_at=taken_at or datetime.now(UTC))
        session.add(point)
        await session.flush()
        return point.id, contents
