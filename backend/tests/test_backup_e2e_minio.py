"""Backup end to end against two real MinIO servers (CI job `backup-e2e`).

    server dispatch -> test-double agent upload -> replication to a second
    MinIO -> restore read, compared byte for byte.

Skipped unless `ITS_E2E_LOCAL_S3` and `ITS_E2E_OFFSITE_S3` name running MinIO
servers. moto cannot stand in here: it does not enforce POST-policy
conditions and its Object Lock is a model, so this is where the grant scoping
and the lock refusal meet a real object store.

The agent is a double, and named as one: it verifies the server's signature
on the job before acting (invariant 1), then talks to the same server
functions the gateway routes its messages to, and moves bytes over plain HTTP
with the grants it is given, as the Go agent does. What it proves is the
server side; the Go agent's own E2E belongs to the agent work.
"""

import base64
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import boto3
import httpx
import pytest
from blake3 import blake3
from botocore.config import Config as BotoConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, override_settings
from app.core.signing import LocalEd25519Signer, verify_signature
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.gateway.protocol import BackupChunkQueryMsg, BackupReadQueryMsg
from app.jobs.canonical import canonical_bytes
from app.jobs.engine import JobEngine
from app.models import BackupPoint, BackupPolicy, Device, Job, Organization, User
from app.modules.backup.dedup import answer_chunk_query
from app.modules.backup.dispatch import dispatch_backup
from app.modules.backup.gc import open_chunk, seal_chunk
from app.modules.backup.handler import register_job_types
from app.modules.backup.offsite import (
    OffsiteSecret,
    build_client,
    create_target,
    get_target,
    pending_copies,
    replicate_point,
    run_target_test,
    set_repository_target,
)
from app.modules.backup.read import answer_read_query
from app.modules.backup.recovery import prove_recovery_key, register_recovery_key
from app.modules.backup.repository import create_repository
from app.modules.backup.restore import dispatch_restore, open_manifest, seal_manifest
from app.modules.backup.schema import (
    BackupManifest,
    BackupRunResult,
    FiletreeStream,
    ManifestEntry,
)

LOCAL = os.environ.get("ITS_E2E_LOCAL_S3")
OFFSITE = os.environ.get("ITS_E2E_OFFSITE_S3")
ACCESS = os.environ.get("ITS_E2E_S3_ACCESS_KEY", "minioadmin")
SECRET = os.environ.get("ITS_E2E_S3_SECRET_KEY", "minioadmin")
TOOL = Path(__file__).resolve().parents[2] / "scripts" / "itms_recover.py"
CHUNK = 1 << 20

pytestmark = pytest.mark.skipif(
    not (LOCAL and OFFSITE), reason="needs ITS_E2E_LOCAL_S3 and ITS_E2E_OFFSITE_S3 (MinIO)"
)


def _s3(endpoint: str) -> Any:
    return boto3.client(
        "s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id=ACCESS,
        aws_secret_access_key=SECRET,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _tool(*args: str, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(
        [sys.executable, str(TOOL), *args], check=True, capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )
    return out.stdout.strip()


async def _seed(admin_engine: AsyncEngine, app_engine: AsyncEngine, vault: LocalVaultService
                ) -> tuple[UUID, UUID, UUID, UUID]:
    slug = f"e2e-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@x.test")
        device = Device(org_id=org.id, hostname="fs-e2e", os_family="linux")
        s.add_all([user, device])
        await s.commit()
        org_id, user_id, device_id = org.id, user.id, device.id
    async with org_session(org_id, engine=app_engine) as session:
        repo = await create_repository(session, vault, org_id=org_id, actor_id=user_id,
                                       name="primary")
        session.add(BackupPolicy(org_id=org_id, repository_id=repo.id, sources={"paths": ["/srv"]},
                                 window_cron=None, retention_gfs={}, enabled=True))
        repo_id = repo.id
    return org_id, user_id, device_id, repo_id


async def test_dispatch_upload_replicate_and_restore_compare_bytes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    assert LOCAL and OFFSITE
    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i", oidc_jwks_url="https://i/certs", ca_dir=tmp_path,
        s3_endpoint=LOCAL, s3_access_key=ACCESS, s3_secret_key=SECRET,
        s3_bucket="itms-e2e-local", signing_key_path=tmp_path / "sign.key",
        vault_root_key_path=tmp_path / "vault.key",
    )
    register_job_types()
    store = ObjectStore(settings)
    vault = LocalVaultService(tmp_path / "vault.key")
    signer = LocalEd25519Signer(tmp_path / "sign.key")
    engine = JobEngine(signer=signer)
    org_id, user_id, device_id, repo_id = await _seed(admin_engine, app_engine, vault)
    now = datetime.now(UTC)

    # ---- 1. The server dispatches a signed backup.run ------------------------
    async with org_session(org_id, engine=app_engine) as session:
        ((job_id, _dev, envelope),) = await dispatch_backup(
            session, engine, vault, org_id=org_id, device_id=device_id, now=now,
            actor_id=user_id, actor_type="user",
        )

    # ---- 2. The agent double verifies it, then uploads through its grant -----
    public_pem = await signer.public_key_pem()
    assert verify_signature(public_pem, canonical_bytes(envelope["job"]), envelope["sig"])
    tampered = {**envelope["job"], "params": {**envelope["job"]["params"], "sources": {}}}
    assert not verify_signature(public_pem, canonical_bytes(tampered), envelope["sig"])
    params = envelope["job"]["params"]
    repo_key = base64.b64decode(params["repo_key"])

    shared = os.urandom(CHUNK)
    source = {
        "/srv/data/big.bin": shared + os.urandom(CHUNK + 12345),
        "/srv/data/copy.bin": shared,  # deduplicates against big.bin's first chunk
        "/srv/notes.txt": b"off-site copies are locked for 30 days\n" * 200,
    }
    entries: list[ManifestEntry] = []
    sealed: dict[str, bytes] = {}
    for path, data in source.items():
        ids = []
        for off in range(0, len(data), CHUNK):
            plain = data[off : off + CHUNK]
            cid = blake3(plain).hexdigest()
            sealed.setdefault(cid, seal_chunk(repo_key, plain))
            ids.append(cid)
        entries.append(ManifestEntry(path=path, kind="file", mode=0o644, size=len(data),
                                     mtime=1.0, chunk_ids=ids))

    async with org_session(org_id, engine=app_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await engine.mark_running(session, job)
    async with org_session(org_id, engine=app_engine) as session:
        grant = await answer_chunk_query(
            session, store,
            BackupChunkQueryMsg(v=1, kind="backup_chunk_query", job_id=str(job_id),
                                query_id="q1", check=list(sealed), stored=[]),
            org_id=org_id, device_id=device_id,
        )
    assert grant is not None
    assert sorted(grant.new) == sorted(sealed)

    manifest_suffix = f"manifests/{uuid4().hex}"
    manifest = BackupManifest(schema_version=1,
                              streams=[FiletreeStream(type="filetree", entries=entries)])
    uploads = {f"chunks/{cid[:2]}/{cid}": sealed[cid] for cid in grant.new}
    uploads[manifest_suffix] = seal_manifest(repo_key, manifest)
    async with httpx.AsyncClient(timeout=60) as http:
        for suffix, body in uploads.items():
            resp = await http.post(
                grant.upload.url,
                data={**grant.upload.fields, "key": grant.upload.key_prefix + suffix},
                files={"file": ("blob", body)},
            )
            assert resp.status_code in (200, 204), resp.text
        # The grant is scoped to this repository: a write beside it is refused
        # by MinIO itself.
        outside = await http.post(
            grant.upload.url,
            data={**grant.upload.fields, "key": f"{org_id}/backup/{uuid4()}/chunks/xx/evil"},
            files={"file": ("blob", b"x")},
        )
        assert outside.status_code == 403

    async with org_session(org_id, engine=app_engine) as session:
        await answer_chunk_query(
            session, store,
            BackupChunkQueryMsg(v=1, kind="backup_chunk_query", job_id=str(job_id),
                                query_id="q2", check=[], stored=list(grant.new)),
            org_id=org_id, device_id=device_id,
        )
    result = BackupRunResult(state="success", manifest_ref=manifest_suffix,
                             bytes_protected=sum(len(v) for v in source.values()),
                             bytes_stored=sum(len(v) for v in sealed.values()),
                             chunk_count=len(sealed))
    with override_settings(settings):
        async with org_session(org_id, engine=app_engine) as session:
            job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
            await engine.complete(session, job, state="succeeded", result=result.model_dump())
    async with org_session(org_id, engine=app_engine) as session:
        point = (await session.execute(select(BackupPoint).where(
            BackupPoint.org_id == org_id))).scalar_one()
    assert point.state == "complete"

    # ---- 3. Off-site: recovery key, tested locked bucket, replication --------
    offsite_bucket = f"itms-offsite-{uuid4().hex[:8]}"
    _s3(OFFSITE).create_bucket(Bucket=offsite_bucket, ObjectLockEnabledForBucket=True)
    key_file = tmp_path / "recovery.key"
    public_text = _tool("keygen", "--out", str(key_file))
    async with org_session(org_id, engine=app_engine) as session:
        row, challenge = await register_recovery_key(
            session, org_id=org_id, actor_id=user_id, public_key_text=public_text, now=now)
        answer = _tool("prove", "--key", str(key_file), "--challenge", challenge)
        await prove_recovery_key(session, row, answer=answer, actor_id=user_id, now=now)
        target = await create_target(
            session, vault, org_id=org_id, actor_id=user_id,
            settings={"name": "second minio", "endpoint": OFFSITE, "bucket": offsite_bucket,
                      "prefix": "itms", "immutable_days": 1},
            secret=OffsiteSecret(ACCESS, SECRET),
        )
        target_id = target.id
    async with org_session(org_id, engine=app_engine) as session:
        target = await get_target(session, org_id, target_id)
        assert target is not None
        tested = await run_target_test(session, vault, target, build_client, now=now)
        assert target.state == "ok", tested
        await set_repository_target(session, org_id=org_id, repository_id=repo_id,
                                    target_id=target_id, actor_id=user_id)
    async with org_session(org_id, engine=app_engine) as session:
        (item,) = await pending_copies(session, org_id)
    assert await replicate_point(
        lambda: org_session(org_id, engine=app_engine), vault=vault, local_store=store,
        client_factory=build_client, item=item, org_id=org_id, now=now,
    )
    # A replicated object is locked: MinIO refuses to delete its version.
    offsite_s3 = _s3(OFFSITE)
    listed = offsite_s3.list_object_versions(
        Bucket=offsite_bucket, Prefix=f"itms/{repo_id}/chunks/"
    )
    version = listed["Versions"][0]
    with pytest.raises(Exception):  # noqa: B017 - AccessDenied from the object lock
        offsite_s3.delete_object(Bucket=offsite_bucket, Key=version["Key"],
                                 VersionId=version["VersionId"])

    # ---- 4. Restore read through the server's read exchange -----------------
    async with org_session(org_id, engine=app_engine) as session:
        ((restore_job_id, _d, renvelope),) = await dispatch_restore(
            session, engine, vault, org_id=org_id, backup_point_id=point.id,
            selection=list(source), target={"path": "/restore"}, mode="restore", now=now,
            actor_id=user_id, actor_type="user",
        )
    assert verify_signature(public_pem, canonical_bytes(renvelope["job"]), renvelope["sig"])
    async with org_session(org_id, engine=app_engine) as session:
        rjob = (await session.execute(select(Job).where(Job.id == restore_job_id))).scalar_one()
        await engine.mark_running(session, rjob)
    rkey = base64.b64decode(renvelope["job"]["params"]["repo_key"])

    async def fetch(suffixes: list[str]) -> dict[str, bytes]:
        async with org_session(org_id, engine=app_engine) as session:
            reply = await answer_read_query(
                session, store,
                BackupReadQueryMsg(v=1, kind="backup_read_query", job_id=str(restore_job_id),
                                   query_id=uuid4().hex[:16], suffixes=suffixes),
                org_id=org_id, device_id=device_id,
            )
        assert reply is not None
        async with httpx.AsyncClient(timeout=60) as http:
            return {g.suffix: (await http.get(g.url)).raise_for_status().content
                    for g in reply.grants}

    got_manifest = open_manifest(rkey, (await fetch([manifest_suffix]))[manifest_suffix])
    for entry in got_manifest.streams[0].entries:
        blobs = await fetch([f"chunks/{c[:2]}/{c}" for c in entry.chunk_ids])
        data = b"".join(open_chunk(rkey, blobs[f"chunks/{c[:2]}/{c}"]) for c in entry.chunk_ids)
        assert data == source[entry.path]

    # ---- 5. And from the off-site copy alone, with only the bucket key -------
    dest = tmp_path / "from-offsite"
    _tool("restore", "--key", str(key_file), "--bucket", offsite_bucket, "--endpoint", OFFSITE,
          "--prefix", "itms", "--repository", str(repo_id), "--point", str(point.id),
          "--dest", str(dest),
          env={"AWS_ACCESS_KEY_ID": ACCESS, "AWS_SECRET_ACCESS_KEY": SECRET})
    for path, data in source.items():
        assert (dest / path.lstrip("/")).read_bytes() == data
