"""Spooled recordings upload multipart (spec-screen-share.md §3.9).

An 8-hour tile stream can reach hundreds of MB, so the api never holds a
recording in memory: it spools to disk and uploads in parts.
"""

from pathlib import Path
from uuid import uuid4

import pytest
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.storage import S3_MIN_PART_BYTES, ObjectStore
from app.core.tenancy import org_session
from app.models import AuditEvent, Device, Organization, RemoteSession, User
from app.modules.remote.recording import finalize_recording_file


@pytest.fixture()
def store(tmp_path):
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


def _file(tmp_path: Path, size: int) -> tuple[Path, bytes]:
    data = bytes(range(256)) * (size // 256) + b"\x07" * (size % 256)
    path = tmp_path / "rec.rsp"
    path.write_bytes(data)
    return path, data


async def test_large_file_goes_up_in_parts(store: ObjectStore, tmp_path: Path) -> None:
    path, data = _file(tmp_path, 2 * S3_MIN_PART_BYTES + 123)
    calls: list[int] = []
    upload_part = store._client.upload_part

    def counting(**kw):
        calls.append(kw["PartNumber"])
        return upload_part(**kw)

    store._client.upload_part = counting  # type: ignore[method-assign]
    org, sid = uuid4(), uuid4()
    ref = await store.put_remote_recording_stream(
        org, sid, path, part_size=S3_MIN_PART_BYTES
    )
    assert calls == [1, 2, 3]
    assert ref.endswith(f"{org}/remote-recordings/{sid}.rsp")
    assert await store.get_remote_recording(ref) == data


async def test_small_file_is_a_single_put(store: ObjectStore, tmp_path: Path) -> None:
    path, data = _file(tmp_path, 1000)
    ref = await store.put_remote_recording_stream(uuid4(), uuid4(), path)
    assert await store.get_remote_recording(ref) == data


async def test_failed_upload_is_aborted(store: ObjectStore, tmp_path: Path) -> None:
    path, _ = _file(tmp_path, 2 * S3_MIN_PART_BYTES)
    aborted: list[str] = []
    abort = store._client.abort_multipart_upload

    def failing(**kw):
        raise RuntimeError("network gone")

    def recording_abort(**kw):
        aborted.append(kw["UploadId"])
        return abort(**kw)

    store._client.upload_part = failing  # type: ignore[method-assign]
    store._client.abort_multipart_upload = recording_abort  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await store.put_remote_recording_stream(
            uuid4(), uuid4(), path, part_size=S3_MIN_PART_BYTES
        )
    assert len(aborted) == 1


async def test_part_size_below_the_s3_minimum_is_refused(
    store: ObjectStore, tmp_path: Path
) -> None:
    path, _ = _file(tmp_path, 10)
    with pytest.raises(ValueError):
        await store.put_remote_recording_stream(uuid4(), uuid4(), path, part_size=1024)


async def test_truncated_recording_is_flagged_and_audited(
    store: ObjectStore, tmp_path: Path, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="rc", slug=f"rc-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        dev = Device(org_id=org.id, hostname="h", os_family="windows")
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add_all([dev, usr])
        await s.commit()
    path, _ = _file(tmp_path, 4096)
    async with org_session(org.id, engine=app_engine, clients=ClientScope.all()) as s:
        row = RemoteSession(
            org_id=org.id, device_id=dev.id, technician_id=usr.id,
            protocol="screen", consent_mode="attended", state="ended",
        )
        s.add(row)
        await s.flush()
        ref, size = await finalize_recording_file(
            s, store, org_id=org.id, session_id=row.id, path=path, truncated=True
        )
        sid = row.id
    async with org_session(org.id, engine=app_engine, clients=ClientScope.all()) as s:
        got = (await s.execute(select(RemoteSession).where(RemoteSession.id == sid))).scalar_one()
        assert got.recording_ref == ref and got.recording_bytes == size == 4096
        assert got.recording_truncated is True
        actions = (
            (await s.execute(select(AuditEvent.action).where(AuditEvent.target_id == str(sid))))
            .scalars()
            .all()
        )
    assert actions == ["remote.recording.truncated"]
