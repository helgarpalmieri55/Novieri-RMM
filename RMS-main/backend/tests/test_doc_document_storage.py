"""The four `ObjectStore` methods for stored documents, and the isolation they
carry instead of RLS (`spec-attachments.md` §3, falsifiers F1c, F1d, F2a, F7b).

**AN OBJECT STORE HAS NO ROW-LEVEL SECURITY.** Invariant 5 says tenant scoping
is enforced at the database layer; the bucket is not a database layer. §3.3
names four mechanisms that replace it, and two of them are asserted in this
file: the key is entirely server-derived and always under `{org_id}/docs/`
(F1d), and BOTH storage directions refuse a ref outside that prefix (F1c). The
other two -- no key in any response, no presigned URL -- are asserted in
`test_doc_documents_api.py` (F1b) and below (F2a).

The prefix assertions should never trip in practice, because no route takes a
key and every segment is derived from ids the caller was already authorized
for. They exist because a defence-in-depth check present on only ONE direction
is worth very little, which is a thing this codebase has already written down
twice (`get_budget_attachment`, `get_ticket_attachment`).
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


# --------------------------------------------------------------------------- #
# F1d -- every key is under `{org_id}/docs/`, and no client bytes reach it.
# --------------------------------------------------------------------------- #


def test_the_key_is_entirely_server_derived(store: ObjectStore) -> None:
    org_id, document_id, version_id = uuid4(), uuid4(), uuid4()
    key = store._doc_document_key(org_id, document_id, version_id)
    assert key == f"{org_id}/docs/documents/{document_id}/{version_id}"
    assert key.startswith(f"{org_id}/docs/")


def test_every_key_starts_with_the_orgs_docs_prefix(store: ObjectStore) -> None:
    """F1d over a spread of ids rather than one, because "it happened to start
    with the org id this time" is not the property."""
    for _ in range(25):
        org_id = uuid4()
        key = store._doc_document_key(org_id, uuid4(), uuid4())
        assert key.startswith(f"{org_id}/docs/")
        assert ".." not in key


async def test_put_returns_a_ref_under_the_bucket_and_the_org(store: ObjectStore) -> None:
    org_id, document_id, version_id = uuid4(), uuid4(), uuid4()
    ref = await store.put_doc_document(org_id, document_id, version_id, b"bytes")
    assert ref == (
        f"s3://it-suite-test/{org_id}/docs/documents/{document_id}/{version_id}"
    )


# --------------------------------------------------------------------------- #
# F7b -- a round trip returns the same bytes.
# --------------------------------------------------------------------------- #


async def test_round_trip_preserves_the_bytes_and_their_digest(store: ObjectStore) -> None:
    """F7b. A digest mismatch means storage corruption or the wrong object, and
    this is the only test that can tell you which -- the stored `sha256` is
    computed from the bytes in memory before the write, so comparing it to a
    digest of what comes back compares two independent computations."""
    import hashlib

    org_id, document_id, version_id = uuid4(), uuid4(), uuid4()
    data = b"%PDF-1.7\nsigned contract\n" * 1000
    digest = hashlib.sha256(data).hexdigest()
    ref = await store.put_doc_document(org_id, document_id, version_id, data)
    fetched = await store.get_doc_document(ref, org_id)
    assert fetched == data
    assert hashlib.sha256(fetched).hexdigest() == digest


# --------------------------------------------------------------------------- #
# F1c -- BOTH directions refuse a ref outside `{org_id}/docs/`.
# --------------------------------------------------------------------------- #


async def test_get_refuses_a_ref_outside_the_bucket(store: ObjectStore) -> None:
    org_id = uuid4()
    with pytest.raises(ValueError):
        await store.get_doc_document(f"s3://another-bucket/{org_id}/docs/x/y", org_id)


async def test_delete_refuses_a_ref_outside_the_bucket(store: ObjectStore) -> None:
    org_id = uuid4()
    with pytest.raises(ValueError):
        await store.delete_doc_document(org_id, f"s3://another-bucket/{org_id}/docs/x/y")


async def test_get_refuses_another_orgs_key(store: ObjectStore) -> None:
    """The one that matters: org A's stored ref, org B's identity."""
    org_a, org_b = uuid4(), uuid4()
    document_id, version_id = uuid4(), uuid4()
    ref = await store.put_doc_document(org_a, document_id, version_id, b"org A's contract")
    assert await store.get_doc_document(ref, org_a) == b"org A's contract"
    with pytest.raises(ValueError):
        await store.get_doc_document(ref, org_b)


async def test_delete_refuses_another_orgs_key(store: ObjectStore) -> None:
    """The same assertion in the other direction. A purge that could be aimed
    at a foreign key would be a cross-tenant DESTRUCTION, which is strictly
    worse than a cross-tenant read."""
    org_a, org_b = uuid4(), uuid4()
    document_id, version_id = uuid4(), uuid4()
    ref = await store.put_doc_document(org_a, document_id, version_id, b"org A's contract")
    with pytest.raises(ValueError):
        await store.delete_doc_document(org_b, ref)
    # And the object is still there, so the refusal happened before the call.
    assert await store.get_doc_document(ref, org_a) == b"org A's contract"


async def test_get_refuses_another_prefix_within_the_same_org(store: ObjectStore) -> None:
    """`{org_id}/docs/` and not merely `{org_id}/`. The bucket is SHARED --
    backup repositories, agent binaries, job artefacts and session recordings
    all live under this same org's prefix -- so a docs route that would fetch
    `{org_id}/backup/...` on request is a docs permission reading backup bytes.
    """
    org_id = uuid4()
    with pytest.raises(ValueError):
        await store.get_doc_document(f"s3://it-suite-test/{org_id}/backup/repo/chunk", org_id)


async def test_delete_refuses_another_prefix_within_the_same_org(store: ObjectStore) -> None:
    org_id = uuid4()
    with pytest.raises(ValueError):
        await store.delete_doc_document(org_id, f"s3://it-suite-test/{org_id}/backup/r/c")


async def test_delete_removes_the_object(store: ObjectStore) -> None:
    org_id, document_id, version_id = uuid4(), uuid4(), uuid4()
    ref = await store.put_doc_document(org_id, document_id, version_id, b"gone soon")
    await store.delete_doc_document(org_id, ref)
    with pytest.raises(Exception):  # noqa: B017 -- botocore's NoSuchKey
        await store.get_doc_document(ref, org_id)


# --------------------------------------------------------------------------- #
# F2a -- no presigned URL, ever.
# --------------------------------------------------------------------------- #


def test_the_object_store_exposes_no_presign_method_for_documents() -> None:
    """F2a, and the reason it is a TEST rather than a habit (§0.3).

    `ObjectStore` has a careful presigning facility and this module must not
    use it. A presigned URL is a bearer credential: whoever holds it performs
    the operation with no token, no RBAC check and no audit event, so
    invariants 4 and 6 both fall. And in THIS deployment it is worse than
    generic: `deploy/docker-compose.yml` routes `PathPrefix('/${ITS_S3_BUCKET}/')`
    to MinIO on the same hostname and entrypoint that serves the console, so a
    presigned GET of an uploaded HTML file would be an unauthenticated,
    same-origin, script-executing URL for the whole tenant -- the exact stored
    XSS this module is built to prevent, delivered by the mitigation.

    Asserted over the attribute surface rather than by reading the source, so
    it fails on the method being ADDED rather than on any particular spelling
    of it.
    """
    presign_names = [
        name
        for name in dir(ObjectStore)
        if name.startswith("presign") and ("doc" in name or "document" in name)
    ]
    assert presign_names == []


def test_no_presign_helper_names_the_docs_prefix() -> None:
    """The other half: an existing presign method must not learn to sign a
    `{org}/docs/` key either. `_presign` is the single chokepoint every signed
    URL in this module goes through, and no caller of it is a docs path."""
    import inspect

    from app.core import storage

    source = inspect.getsource(storage)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("*"):
            continue
        if "_presign(" in stripped:
            assert "/docs/" not in stripped, stripped
            assert "doc_document" not in stripped, stripped
