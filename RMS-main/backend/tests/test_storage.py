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


async def test_put_get_job_artifact_roundtrip(store: ObjectStore) -> None:
    org_id, job_id = uuid4(), uuid4()
    ref = await store.put_job_artifact(org_id, job_id, "inventory.json", b'{"cpu": "x"}')
    assert ref == f"s3://it-suite-test/{org_id}/job-artifacts/{job_id}/inventory.json"
    assert await store.get_job_artifact(ref) == b'{"cpu": "x"}'


async def test_health_check(store: ObjectStore) -> None:
    assert await store.health() is True


async def test_get_rejects_foreign_bucket_ref(store: ObjectStore) -> None:
    with pytest.raises(ValueError):
        await store.get_job_artifact("s3://another-bucket/x/y")


async def test_an_agent_release_is_read_back_by_its_identity(store: ObjectStore) -> None:
    """The Group Policy MSI reads the published binary by (version, os, arch);
    the key is derived, so a traversal in a version is refused, not read."""
    await store.put_agent_release(store.agent_release_key("1.7.0", "windows", "amd64"), b"MZ-1")
    assert await store.get_agent_release("1.7.0", "windows", "amd64") == b"MZ-1"
    with pytest.raises(ValueError):
        await store.get_agent_release("../../backups", "windows", "amd64")


async def test_an_agent_releases_body_is_read_off_the_event_loop(
    store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows agent is megabytes: `Body.read()` streams it from S3, so it
    runs on the executor with `get_object`, never on the event loop's thread."""
    import threading

    loop_thread = threading.current_thread()
    readers: list[threading.Thread] = []

    class Body:
        def read(self) -> bytes:
            readers.append(threading.current_thread())
            return b"MZ-2"

    monkeypatch.setattr(store._client, "get_object", lambda **_: {"Body": Body()})
    assert await store.get_agent_release("1.7.0", "windows", "amd64") == b"MZ-2"
    assert readers and readers[0] is not loop_thread
