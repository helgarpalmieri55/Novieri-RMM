"""Manifest v2 on the server (spec-server-backup.md §5.2).

A v2 manifest names PAGES, and pages are chunks. GC, verification and
replication decide what is alive from manifests, so a page id they did not
count would be deleted by the next GC and the point would be unreadable. These
tests build a v2 point the way the agent writes one (NDJSON entry pages, an
entry whose chunk list moved into an id page) and prove every page survives.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from blake3 import blake3
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import BackupChunkCopy, BackupPoint, BackupRun
from app.modules.backup.gc import gc_repository, seal_chunk, verify_point
from app.modules.backup.manifest import ManifestError, referenced_chunk_ids
from app.modules.backup.restore import browse_manifest
from tests.backup_offsite_fixtures import (
    add_proven_key,
    add_target,
    load_recover_tool,
    local_settings,
    make_buckets,
    moto_client_factory,
    seed_org,
)


@pytest.fixture()
def aws(tmp_path: Path):
    with mock_aws():
        make_buckets()
        yield ObjectStore(local_settings(tmp_path))


async def _put(store: ObjectStore, org: Any, plain: bytes) -> str:
    cid = blake3(plain).hexdigest()
    sealed = seal_chunk(org.repo_key, plain)
    await store.put_backup_chunk(org.org_id, org.repository_id, cid, sealed)
    return cid


async def _seal_doc(org: Any, doc: dict[str, Any]) -> bytes:
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(org.repo_key).encrypt(nonce, json.dumps(doc).encode(), None)


async def _v2_point(app_engine: AsyncEngine, store: ObjectStore, org: Any,
                    *, schema_version: int = 2) -> tuple[UUID, dict[str, set[str]]]:
    """A v2 point: two small files inline, one large file whose chunk list is in
    an id page. Returns (point id, {"data", "pages"} chunk ids)."""
    small = [await _put(store, org, b"alpha" * 10), await _put(store, org, b"beta" * 10)]
    big = [await _put(store, org, f"big-{i}".encode() * 50) for i in range(3)]
    id_page = await _put(store, org, "".join(f"{c}\n" for c in big).encode())
    entries = [
        {"path": "/srv", "kind": "dir", "mode": 0o755, "size": 0, "mtime": 1.0, "chunk_ids": []},
        {"path": "/srv/a", "kind": "file", "mode": 0o644, "size": 50, "mtime": 1.0,
         "chunk_ids": [small[0]], "uid": 0, "gid": 0, "flags": ["changed_during_read"]},
        {"path": "/srv/b", "kind": "file", "mode": 0o644, "size": 40, "mtime": 1.0,
         "chunk_ids": [small[1]], "sd": "O:BAG:BA"},
        {"path": "/srv/big", "kind": "file", "mode": 0o644, "size": 750, "mtime": 1.0,
         "chunk_ids": [], "chunk_pages": [id_page]},
    ]
    page1 = await _put(store, org, "".join(json.dumps(e) + "\n" for e in entries[:2]).encode())
    page2 = await _put(store, org, "".join(json.dumps(e) + "\n" for e in entries[2:]).encode())
    doc = {
        "schema_version": schema_version,
        "header": {"workload_kind": "files", "host_os": "linux",
                   "consistency": {"level": "live", "crash_consistent": False},
                   "page_index": [{"stream": 0, "page": 0, "first": "/srv", "last": "/srv/a",
                                   "entries": 2}]},
        "streams": [{"type": "filetree", "name": "files", "entry_count": 4,
                     "pages": [page1, page2]}],
    }
    ref = await store.put_backup_object(org.org_id, org.repository_id,
                                        f"manifests/{uuid4().hex}", await _seal_doc(org, doc))
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        run = BackupRun(org_id=org.org_id, device_id=org.device_id,
                        repository_id=org.repository_id, state="success", actor_type="system")
        session.add(run)
        await session.flush()
        point = BackupPoint(org_id=org.org_id, device_id=org.device_id,
                            repository_id=org.repository_id, backup_run_id=run.id,
                            manifest_ref=ref, size=390, state="complete",
                            taken_at=datetime.now(UTC))
        session.add(point)
        await session.flush()
        point_id = point.id
    return point_id, {"data": set(small) | set(big), "pages": {page1, page2, id_page}}


async def test_gc_keeps_every_v2_page_and_deletes_only_the_orphan(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, aws: ObjectStore, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "k")
    org = await seed_org(admin_engine, app_engine, vault, "v2gc")
    _point, ids = await _v2_point(app_engine, aws, org)
    orphan = await _put(aws, org, b"nothing references me")

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        deleted = await gc_repository(session, aws, vault, org_id=org.org_id,
                                      repository_id=org.repository_id, now=datetime.now(UTC))

    remaining = set(await aws.list_backup_chunks(org.org_id, org.repository_id))
    assert deleted == 1
    assert orphan not in remaining
    assert ids["pages"] <= remaining, "a manifest page or id page was garbage-collected"
    assert ids["data"] <= remaining


async def test_verification_checks_v2_pages_and_the_chunks_they_list(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, aws: ObjectStore, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "k")
    org = await seed_org(admin_engine, app_engine, vault, "v2ver")
    point_id, ids = await _v2_point(app_engine, aws, org)
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert await verify_point(session, aws, vault, org_id=org.org_id,
                                  backup_point_id=point_id, now=datetime.now(UTC))
    # Every data chunk is checked, including those listed only in an id page.
    big_chunk = sorted(ids["data"])[0]
    await aws.delete_backup_chunk(org.org_id, org.repository_id, big_chunk)
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert not await verify_point(session, aws, vault, org_id=org.org_id,
                                      backup_point_id=point_id, now=datetime.now(UTC))


async def test_browse_flattens_v2_pages_into_entries(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, aws: ObjectStore, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "k")
    org = await seed_org(admin_engine, app_engine, vault, "v2br")
    point_id, _ids = await _v2_point(app_engine, aws, org)
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        manifest = await browse_manifest(session, aws, vault, org_id=org.org_id,
                                         backup_point_id=point_id)
    by_path = {e.path: e for e in manifest.streams[0].entries}
    assert set(by_path) == {"/srv", "/srv/a", "/srv/b", "/srv/big"}
    assert len(by_path["/srv/big"].chunk_ids) == 3


async def test_an_unknown_schema_version_refuses_rather_than_guessing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, aws: ObjectStore, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "k")
    org = await seed_org(admin_engine, app_engine, vault, "v2unk")
    _point, ids = await _v2_point(app_engine, aws, org, schema_version=3)
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(ManifestError):
            await gc_repository(session, aws, vault, org_id=org.org_id,
                                repository_id=org.repository_id, now=datetime.now(UTC))
    remaining = set(await aws.list_backup_chunks(org.org_id, org.repository_id))
    assert ids["pages"] | ids["data"] <= remaining


async def test_a_page_that_does_not_hash_to_its_id_is_refused() -> None:
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = os.urandom(32)

    def seal(b: bytes) -> bytes:
        n = os.urandom(12)
        return n + AESGCM(key).encrypt(n, b, None)

    page_id = "ab" * 32  # not the hash of what is stored under it
    doc = {"schema_version": 2, "streams": [{"type": "filetree", "pages": [page_id]}]}

    async def fetch(_cid: str) -> bytes:
        return seal(b'{"path": "/x", "kind": "file"}\n')

    with pytest.raises(ManifestError):
        await referenced_chunk_ids(key, seal(json.dumps(doc).encode()), fetch)


async def test_replication_copies_v2_pages_and_the_tool_restores_them(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, aws: ObjectStore, tmp_path: Path
) -> None:
    import boto3

    from app.modules.backup.offsite import pending_copies, replicate_point

    vault = LocalVaultService(tmp_path / "k")
    org = await seed_org(admin_engine, app_engine, vault, "v2rep")
    await add_proven_key(app_engine, org)
    target_id = await add_target(app_engine, vault, org)
    point_id, ids = await _v2_point(app_engine, aws, org)
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        (item,) = await pending_copies(session, org.org_id)
    assert await replicate_point(
        lambda: org_session(
            org.org_id, engine=app_engine, clients=ClientScope.all()
        ), vault=vault, local_store=aws,
        client_factory=moto_client_factory, item=item, org_id=org.org_id, now=datetime.now(UTC),
    )
    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        copied = set((await session.execute(select(BackupChunkCopy.chunk_id).where(
            BackupChunkCopy.target_id == target_id))).scalars().all())
    assert copied == ids["pages"] | ids["data"]

    tool = load_recover_tool()
    reader = tool.OffsiteReader(boto3.client("s3", region_name="us-east-1"),
                                "offsite-locked", "itms")
    dest = tmp_path / "out"
    assert tool.restore(reader, org.private_key, str(org.repository_id), str(point_id), dest) == 3
    assert (dest / "srv/big").read_bytes() == b"".join(
        f"big-{i}".encode() * 50 for i in range(3))
