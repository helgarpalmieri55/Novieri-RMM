"""Tests for the provision object storage helpers (docs/modules/provisioning/spec.md).

Mirrors `tests/test_storage_backup.py`'s fixture/style. The provision key
layout has no repository_id: `{org_id}/provision/{suffix}` (unlike backup's
`{org_id}/backup/{repository_id}/{suffix}`).
"""

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


async def test_put_get_provision_object_roundtrip(store: ObjectStore) -> None:
    org_id = uuid4()
    run_id = uuid4()
    ref = await store.put_provision_object(org_id, f"answers/{run_id}", b"answer-bytes")
    assert ref == f"s3://it-suite-test/{org_id}/provision/answers/{run_id}"
    assert await store.get_provision_object(ref) == b"answer-bytes"


async def test_delete_provision_object_removes_it(store: ObjectStore) -> None:
    org_id = uuid4()
    run_id = uuid4()
    ref = await store.put_provision_object(org_id, f"answers/{run_id}", b"data")
    await store.delete_provision_object(ref)
    with pytest.raises(Exception):  # noqa: B017 - boto3 ClientError (NoSuchKey)
        await store.get_provision_object(ref)


async def test_provision_ref_isolated_per_org(store: ObjectStore) -> None:
    org_a, org_b = uuid4(), uuid4()
    run_id = uuid4()
    ref_a = await store.put_provision_object(org_a, f"answers/{run_id}", b"a-data")
    assert str(org_a) in ref_a
    assert str(org_b) not in ref_a
    assert await store.get_provision_object(ref_a) == b"a-data"


async def test_get_provision_object_rejects_foreign_bucket_ref(store: ObjectStore) -> None:
    with pytest.raises(ValueError):
        await store.get_provision_object("s3://another-bucket/x/y")
