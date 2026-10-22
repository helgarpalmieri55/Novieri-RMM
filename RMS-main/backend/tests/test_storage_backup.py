from uuid import uuid4

import pytest
from moto import mock_aws

from app.core.config import Settings
from app.core.storage import ObjectStore


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


async def test_put_get_backup_chunk_roundtrip(store: ObjectStore) -> None:
    org_id, repository_id = uuid4(), uuid4()
    chunk_id = "abcdef0123456789"
    suffix = f"chunks/{chunk_id[:2]}/{chunk_id}"
    ref = await store.put_backup_object(org_id, repository_id, suffix, b"chunk-bytes")
    assert ref == f"s3://it-suite-test/{org_id}/backup/{repository_id}/{suffix}"
    assert await store.get_backup_object(ref) == b"chunk-bytes"


async def test_list_backup_chunks_includes_id(store: ObjectStore) -> None:
    org_id, repository_id = uuid4(), uuid4()
    chunk_id = "deadbeef00112233"
    suffix = f"chunks/{chunk_id[:2]}/{chunk_id}"
    await store.put_backup_object(org_id, repository_id, suffix, b"data")
    chunk_ids = await store.list_backup_chunks(org_id, repository_id)
    assert chunk_ids == [chunk_id]


async def test_delete_backup_object_removes_it(store: ObjectStore) -> None:
    org_id, repository_id = uuid4(), uuid4()
    chunk_id = "cafef00d00112233"
    suffix = f"chunks/{chunk_id[:2]}/{chunk_id}"
    ref = await store.put_backup_object(org_id, repository_id, suffix, b"data")
    await store.delete_backup_object(ref)
    with pytest.raises(Exception):  # noqa: B017 - boto3 ClientError (NoSuchKey)
        await store.get_backup_object(ref)


async def test_backup_ref_isolated_per_org(store: ObjectStore) -> None:
    org_a, org_b = uuid4(), uuid4()
    repository_id = uuid4()
    chunk_id = "0011223344556677"
    suffix = f"chunks/{chunk_id[:2]}/{chunk_id}"
    ref_a = await store.put_backup_object(org_a, repository_id, suffix, b"a-data")
    # org_b never wrote this chunk: its own listing/prefix must not see org_a's object.
    chunk_ids_b = await store.list_backup_chunks(org_b, repository_id)
    assert chunk_ids_b == []
    chunk_ids_a = await store.list_backup_chunks(org_a, repository_id)
    assert chunk_ids_a == [chunk_id]
    assert str(org_a) in ref_a


async def test_get_backup_object_rejects_foreign_bucket_ref(store: ObjectStore) -> None:
    with pytest.raises(ValueError):
        await store.get_backup_object("s3://another-bucket/x/y")
