"""The off-site copy (spec-server-backup.md §6; operator decisions 2026-09-23).

moto stands in for both the local MinIO and the off-site provider. Object
Lock is real in moto: a locked version refuses deletion, which is what the
target test and these assertions rely on.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import boto3
import pytest
from blake3 import blake3
from moto import mock_aws
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import (
    AuditEvent,
    BackupChunkCopy,
    BackupPointCopy,
    BackupRepository,
    Credential,
)
from app.modules.backup.offsite import (
    LOCK_REQUIRED_MESSAGE,
    NO_RECOVERY_KEY_MESSAGE,
    NOT_TESTED_MESSAGE,
    OffsiteError,
    get_target,
    offsite_gc,
    offsite_hold,
    pending_copies,
    replicate_point,
    run_target_test,
    set_repository_target,
)
from tests.backup_offsite_fixtures import (
    LOCK_BUCKET,
    PLAIN_BUCKET,
    add_point,
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
        s3 = make_buckets()
        yield ObjectStore(local_settings(tmp_path)), s3


@pytest.fixture()
def vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


def _session_factory(app_engine: AsyncEngine, org_id: UUID) -> Any:
    return lambda: org_session(org_id, engine=app_engine)


async def _replicate_all(app_engine, vault, store, org, now, batch=256) -> list[bool]:
    async with org_session(org.org_id, engine=app_engine) as session:
        work = await pending_copies(session, org.org_id)
    return [
        await replicate_point(
            _session_factory(app_engine, org.org_id), vault=vault, local_store=store,
            client_factory=moto_client_factory, item=item, org_id=org.org_id, now=now,
            batch=batch,
        )
        for item in work
    ]


def _keys(s3: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=LOCK_BUCKET, Prefix=prefix):
        out.extend(o["Key"] for o in page.get("Contents", []))
    return out


# --------------------------------------------------------------------------- #
# Switching off-site on
# --------------------------------------------------------------------------- #


async def test_off_site_cannot_be_enabled_without_a_proven_recovery_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    org = await seed_org(admin_engine, app_engine, vault, "os-nokey")
    target_id = await add_target(app_engine, vault, org, attach=False)
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(OffsiteError, match="recovery key"):
            await set_repository_target(session, org_id=org.org_id,
                                        repository_id=org.repository_id,
                                        target_id=target_id, actor_id=org.user_id)
    assert "recovery key" in NO_RECOVERY_KEY_MESSAGE


async def test_off_site_cannot_be_enabled_on_an_untested_target(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    org = await seed_org(admin_engine, app_engine, vault, "os-untested")
    await add_proven_key(app_engine, org)
    target_id = await add_target(app_engine, vault, org, state="untested", attach=False)
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(OffsiteError) as exc:
            await set_repository_target(session, org_id=org.org_id,
                                        repository_id=org.repository_id,
                                        target_id=target_id, actor_id=org.user_id)
    assert str(exc.value) == NOT_TESTED_MESSAGE


async def test_enabling_is_audited_and_sets_the_repository_target(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    org = await seed_org(admin_engine, app_engine, vault, "os-on")
    await add_proven_key(app_engine, org)
    target_id = await add_target(app_engine, vault, org, attach=False)
    async with org_session(org.org_id, engine=app_engine) as session:
        repo = await set_repository_target(session, org_id=org.org_id,
                                           repository_id=org.repository_id,
                                           target_id=target_id, actor_id=org.user_id)
        assert repo.offsite_target_id == target_id
    async with AsyncSession(admin_engine) as s:
        actions = (await s.execute(select(AuditEvent.action).where(
            AuditEvent.org_id == org.org_id))).scalars().all()
    assert "backup.offsite_enabled" in actions


# --------------------------------------------------------------------------- #
# The target test (worker side)
# --------------------------------------------------------------------------- #


async def test_a_bucket_without_object_lock_fails_the_test_with_a_clear_message(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    org = await seed_org(admin_engine, app_engine, vault, "os-nolock")
    target_id = await add_target(app_engine, vault, org, bucket=PLAIN_BUCKET, state="testing",
                                 attach=False)
    async with org_session(org.org_id, engine=app_engine) as session:
        target = await get_target(session, org.org_id, target_id)
        assert target is not None
        result = await run_target_test(session, vault, target, moto_client_factory,
                                       now=datetime.now(UTC))
        assert target.state == "failing"
        assert target.last_error == LOCK_REQUIRED_MESSAGE
    assert result["steps"][0] == {"step": "object_lock", "ok": False,
                                  "detail": LOCK_REQUIRED_MESSAGE}


async def test_a_locked_bucket_passes_every_step_and_the_decrypt_is_audited(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    org = await seed_org(admin_engine, app_engine, vault, "os-lock")
    target_id = await add_target(app_engine, vault, org, state="testing", attach=False)
    async with org_session(org.org_id, engine=app_engine) as session:
        target = await get_target(session, org.org_id, target_id)
        assert target is not None
        result = await run_target_test(session, vault, target, moto_client_factory,
                                       now=datetime.now(UTC))
        assert target.state == "ok", result
        credential = (await session.execute(select(Credential).where(
            Credential.id == target.credential_id))).scalar_one()
    assert [s["step"] for s in result["steps"]] == [
        "object_lock", "write", "read", "lock", "delete_refused"
    ]
    assert credential.type == "s3_offsite"
    assert credential.last_used_at is not None
    async with AsyncSession(admin_engine) as s:
        accessed = (await s.execute(select(AuditEvent).where(
            AuditEvent.org_id == org.org_id, AuditEvent.action == "credential.accessed"
        ))).scalars().all()
    assert len(accessed) == 1
    assert "offsite-sk" not in json.dumps(accessed[0].meta)


# --------------------------------------------------------------------------- #
# Replication
# --------------------------------------------------------------------------- #


async def test_replication_copies_ciphertext_under_keyed_names_with_a_30_day_lock(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    store, s3 = aws
    org = await seed_org(admin_engine, app_engine, vault, "os-rep")
    await add_proven_key(app_engine, org)
    await add_target(app_engine, vault, org)
    point_id, _ = await add_point(app_engine, store, org,
                                  {"/srv/a.txt": [b"alpha" * 100, b"beta" * 100],
                                   "/srv/b.txt": [b"gamma" * 50]})
    now = datetime.now(UTC)

    assert await _replicate_all(app_engine, vault, store, org, now) == [True]

    base = f"itms/{org.repository_id}/"
    keys = _keys(s3, base)
    chunk_keys = [k for k in keys if k.startswith(base + "chunks/")]
    assert len(chunk_keys) == 3
    # The provider sees neither the plaintext hash nor the plaintext.
    for k in chunk_keys:
        body = s3.get_object(Bucket=LOCK_BUCKET, Key=k)["Body"].read()
        assert b"alpha" not in body and b"gamma" not in body
    assert not any(blake3(b"alpha" * 100).hexdigest() in k for k in keys)
    assert any(k.startswith(base + "manifests/") for k in keys)
    assert f"{base}catalog/points/{point_id}" in keys
    assert f"{base}catalog/repository.json" in keys
    assert any(k.startswith(base + "keys/repository-key.") for k in keys)

    head = s3.head_object(Bucket=LOCK_BUCKET, Key=chunk_keys[0])
    assert head["ObjectLockMode"] == "COMPLIANCE"
    days = (head["ObjectLockRetainUntilDate"] - now).total_seconds() / 86400
    assert 29.9 < days <= 30.01

    async with org_session(org.org_id, engine=app_engine) as session:
        copy = (await session.execute(select(BackupPointCopy).where(
            BackupPointCopy.point_id == point_id))).scalar_one()
    assert copy.state == "complete"
    assert copy.chunks_copied == 3

    # Idempotent: nothing pending, nothing re-copied.
    assert await _replicate_all(app_engine, vault, store, org, now) == []


async def test_replication_resumes_at_chunk_granularity(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    store, _s3 = aws
    org = await seed_org(admin_engine, app_engine, vault, "os-resume")
    await add_proven_key(app_engine, org)
    target_id = await add_target(app_engine, vault, org)
    point_id, _ = await add_point(app_engine, store, org,
                                  {"/a": [b"1" * 10, b"2" * 10, b"3" * 10]})
    async with org_session(org.org_id, engine=app_engine) as session:
        (item,) = await pending_copies(session, org.org_id)
    # A deadline already passed stops after the first committed step.
    done = await replicate_point(
        _session_factory(app_engine, org.org_id), vault=vault, local_store=store,
        client_factory=moto_client_factory, item=item, org_id=org.org_id,
        now=datetime.now(UTC), batch=1, deadline=0.0,
    )
    assert done is False
    async with org_session(org.org_id, engine=app_engine) as session:
        rows = (await session.execute(select(BackupChunkCopy).where(
            BackupChunkCopy.target_id == target_id))).scalars().all()
        copy = (await session.execute(select(BackupPointCopy).where(
            BackupPointCopy.point_id == point_id))).scalar_one()
    assert len(rows) == 1
    assert copy.state == "copying"
    assert await _replicate_all(app_engine, vault, store, org, datetime.now(UTC), batch=1) == [
        True
    ]


async def test_nothing_is_copied_to_a_bucket_that_lost_its_lock(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    store, s3 = aws
    org = await seed_org(admin_engine, app_engine, vault, "os-lost")
    await add_proven_key(app_engine, org)
    await add_target(app_engine, vault, org, bucket=PLAIN_BUCKET)
    point_id, _ = await add_point(app_engine, store, org, {"/a": [b"x" * 10]})
    assert await _replicate_all(app_engine, vault, store, org, datetime.now(UTC)) == [False]
    assert s3.list_objects_v2(Bucket=PLAIN_BUCKET).get("KeyCount", 0) == 0
    async with org_session(org.org_id, engine=app_engine) as session:
        copy = (await session.execute(select(BackupPointCopy).where(
            BackupPointCopy.point_id == point_id))).scalar_one()
    assert copy.state == "failed"
    assert copy.last_error == LOCK_REQUIRED_MESSAGE


async def test_a_shared_chunk_near_the_end_of_its_lock_is_extended(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    store, s3 = aws
    org = await seed_org(admin_engine, app_engine, vault, "os-extend")
    await add_proven_key(app_engine, org)
    target_id = await add_target(app_engine, vault, org)
    await add_point(app_engine, store, org, {"/a": [b"shared" * 10]})
    t0 = datetime.now(UTC)
    await _replicate_all(app_engine, vault, store, org, t0)
    # Pretend the first copy was made 25 days ago.
    async with AsyncSession(admin_engine) as s:
        await s.execute(text("UPDATE backup_chunk_copies SET locked_until = :t "
                             "WHERE target_id = :tid"),
                        {"t": t0 + timedelta(days=5), "tid": target_id})
        await s.commit()
    await add_point(app_engine, store, org, {"/a": [b"shared" * 10], "/b": [b"new" * 10]})
    await _replicate_all(app_engine, vault, store, org, t0)
    async with org_session(org.org_id, engine=app_engine) as session:
        rows = (await session.execute(select(BackupChunkCopy).where(
            BackupChunkCopy.target_id == target_id))).scalars().all()
    by_days = sorted(round((r.locked_until - t0).total_seconds() / 86400) for r in rows)
    assert by_days == [30, 60]
    shared = next(r for r in rows if round((r.locked_until - t0).total_seconds() / 86400) == 60)
    head = s3.head_object(Bucket=LOCK_BUCKET, Key=shared.object_name,
                          VersionId=shared.version_id)
    assert (head["ObjectLockRetainUntilDate"] - t0).days >= 59


async def test_the_recovery_tool_restores_from_the_off_site_copy_alone(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws, tmp_path: Path
) -> None:
    store, _s3 = aws
    org = await seed_org(admin_engine, app_engine, vault, "os-recover")
    await add_proven_key(app_engine, org)
    await add_target(app_engine, vault, org)
    point_id, contents = await add_point(
        app_engine, store, org, {"/srv/data/a.bin": [b"\x00\x01" * 700, b"tail"],
                                 "/srv/data/sub/b.bin": [b"bee" * 33]})
    await _replicate_all(app_engine, vault, store, org, datetime.now(UTC))

    tool = load_recover_tool()
    # The tool gets a bucket client and the private key; no database, no vault.
    reader = tool.OffsiteReader(boto3.client("s3", region_name="us-east-1"), LOCK_BUCKET, "itms")
    assert str(org.repository_id) in reader.repositories()
    repo_key = reader.repo_key(str(org.repository_id), org.private_key)
    listed = reader.points(str(org.repository_id), repo_key)
    assert [p["point_id"] for p in listed] == [str(point_id)]
    dest = tmp_path / "restored"
    count = tool.restore(reader, org.private_key, str(org.repository_id), str(point_id), dest)
    assert count == 2
    for path, data in contents.items():
        assert (dest / path.lstrip("/")).read_bytes() == data


# --------------------------------------------------------------------------- #
# Retention per copy, off-site GC, and the local hold
# --------------------------------------------------------------------------- #


async def test_off_site_retention_is_per_copy_and_gc_waits_for_the_lock(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    store, _s3 = aws
    org = await seed_org(admin_engine, app_engine, vault, "os-gc")
    await add_proven_key(app_engine, org)
    target_id = await add_target(app_engine, vault, org, retention_gfs={"daily": 1})
    now = datetime.now(UTC)
    old_id, _ = await add_point(app_engine, store, org,
                                {"/a": [b"only-old" * 5, b"both" * 5]},
                                taken_at=now - timedelta(days=3))
    new_id, _ = await add_point(app_engine, store, org, {"/a": [b"both" * 5]}, taken_at=now)
    await _replicate_all(app_engine, vault, store, org, now)

    gc = lambda session, when: offsite_gc(  # noqa: E731
        session, vault=vault, local_store=store, client_factory=moto_client_factory,
        org_id=org.org_id, now=when)
    async with org_session(org.org_id, engine=app_engine) as session:
        counts = await gc(session, now)
    assert counts == {"expired": 1, "deleted": 0}  # still locked: nothing deleted

    # Past the lock, the chunk only the expired copy used goes; the shared one stays.
    later = now + timedelta(days=31)
    async with AsyncSession(admin_engine) as s:
        await s.execute(text("UPDATE backup_chunk_copies SET locked_until = :t "
                             "WHERE target_id = :tid"), {"t": now, "tid": target_id})
        await s.commit()
    async with org_session(org.org_id, engine=app_engine) as session:
        rows_before = (await session.execute(select(BackupChunkCopy).where(
            BackupChunkCopy.target_id == target_id))).scalars().all()
    shared_id = blake3(b"both" * 5).hexdigest()
    only_old = next(r for r in rows_before if r.chunk_id != shared_id)
    # moto enforces the real 30-day lock, so record the delete instead of making it.
    with pytest.MonkeyPatch.context() as mp:
        import app.modules.backup.offsite as offsite_mod

        deleted_keys: list[str] = []

        async def fake_call(fn: Any, /, **kwargs: Any) -> Any:
            if getattr(fn, "__name__", "") == "delete_object":
                deleted_keys.append(kwargs["Key"])
                return {}
            return fn(**kwargs)

        mp.setattr(offsite_mod, "_call", fake_call)
        async with org_session(org.org_id, engine=app_engine) as session:
            counts = await gc(session, later)
    assert counts["deleted"] == 1
    assert deleted_keys == [only_old.object_name]
    async with org_session(org.org_id, engine=app_engine) as session:
        states = dict((await session.execute(select(
            BackupPointCopy.point_id, BackupPointCopy.state))).all())
    assert states[old_id] == "expired"
    assert states[new_id] == "complete"


async def test_local_gc_holds_points_whose_off_site_copy_is_not_complete(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, vault, aws
) -> None:
    store, _ = aws
    org = await seed_org(admin_engine, app_engine, vault, "os-hold")
    await add_proven_key(app_engine, org)
    await add_target(app_engine, vault, org)
    copied, _ = await add_point(app_engine, store, org, {"/a": [b"1" * 10]})
    await _replicate_all(app_engine, vault, store, org, datetime.now(UTC))
    pending, _ = await add_point(app_engine, store, org, {"/a": [b"2" * 10]})
    async with org_session(org.org_id, engine=app_engine) as session:
        held = await offsite_hold(session, org.org_id, {copied, pending})
        repo = (await session.execute(select(BackupRepository).where(
            BackupRepository.id == org.repository_id))).scalar_one()
    assert held == {pending}
    assert repo.offsite_target_id is not None
