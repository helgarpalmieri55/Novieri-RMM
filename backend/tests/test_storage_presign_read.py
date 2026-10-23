"""`ObjectStore.presign_backup_read` — the batch READ counterpart to
`presign_backup_object` (`app/core/storage.py`).

`backup.restore` needs short-lived GET grants for many objects in one
repository at once; SigV4 query signing cannot express a prefix READ, so each
object is still signed individually, and this method is the batch wrapper an
agent's restore request goes through instead of calling `presign_backup_object`
once per suffix from the caller side.
"""

from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest
from moto import mock_aws

from app.core.config import Settings
from app.core.storage import ObjectStore

BUCKET = "it-suite-presign-read-test"


@pytest.fixture()
def object_store(tmp_path):
    """Built the same way `test_storage_grants.py::store` builds its
    `ObjectStore` — no `object_store` fixture existed in `tests/` yet, so this
    mirrors that construction rather than inventing a second one."""
    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket=BUCKET,
    )
    with mock_aws():
        yield ObjectStore(settings)


@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "chunks/../../../secrets", "/absolute/key", "chunks\\ab\\abcd", ""],
)
def test_an_unsafe_suffix_refuses_the_whole_batch(object_store: ObjectStore, bad: str) -> None:
    """One bad suffix must refuse everything, not sign the rest.

    Signing the good ones would hand an agent that is probing a partial success
    to learn from, and would put the grant before the refusal.
    """
    with pytest.raises(ValueError):
        object_store.presign_backup_read(
            uuid4(), uuid4(), ["chunks/ab/abcd", bad], expires_in=900
        )


def test_every_signed_url_addresses_a_key_under_the_repository_prefix(
    object_store: ObjectStore,
) -> None:
    org, repo = uuid4(), uuid4()
    got = object_store.presign_backup_read(
        org, repo, ["chunks/ab/abcd", "manifests/m1"], expires_in=900
    )
    assert [g["suffix"] for g in got] == ["chunks/ab/abcd", "manifests/m1"]

    prefix = object_store.backup_prefix(org, repo)
    for g in got:
        key = unquote(urlparse(g["url"]).path).lstrip("/")
        # The bucket may be in the path (path-style) or in the host
        # (virtual-host style); either way the key must start under the prefix.
        assert f"{prefix}/" in key, "a signed URL addressed a key outside the repository prefix"


def test_an_out_of_range_lifetime_is_refused(object_store: ObjectStore) -> None:
    """`_check_ttl`'s bound, reached through this method rather than assumed.

    THIS TEST PINS THE BEHAVIOUR, NOT THE EXPLICIT GUARD, and the distinction
    was measured: deleting `presign_backup_read`'s own `_check_ttl` call leaves
    it green, because `_presign` enforces the same bound before signing. That
    is defense in depth and is deliberate -- the same layering the suffix
    alphabet gets -- but it means this test alone would not notice the outer
    guard disappearing. What it does prove is that no caller of this method can
    obtain a URL outside the permitted lifetime, which is the property that
    matters.
    """
    with pytest.raises(ValueError):
        object_store.presign_backup_read(uuid4(), uuid4(), ["chunks/ab/abcd"], expires_in=0)


def test_an_empty_batch_is_refused(object_store: ObjectStore) -> None:
    with pytest.raises(ValueError):
        object_store.presign_backup_read(uuid4(), uuid4(), [], expires_in=900)


# The agent-release download must save WITH the platform's extension. The object
# key is `agent_release_suffix`'s (version, os, arch) triple and carries none,
# so without a signed Content-Disposition a Windows artefact lands as
# `its-agent-windows-amd64` -- an extensionless file Windows will not run. This
# was reported from the field: the operator downloaded the binary and could not
# install it. The name is part of the signature, so it cannot be swapped after.
@pytest.mark.parametrize(
    "os_name, arch, expected",
    [
        ("windows", "amd64", "its-agent-windows-amd64.exe"),
        ("windows", "arm64", "its-agent-windows-arm64.exe"),
        ("linux", "amd64", "its-agent-linux-amd64"),
        ("darwin", "arm64", "its-agent-darwin-arm64"),
    ],
)
def test_the_release_download_carries_its_platform_filename(
    object_store: ObjectStore, os_name: str, arch: str, expected: str
) -> None:
    url = object_store.presign_agent_release_get(
        "1.4.0", os_name, arch, expires_in=900
    )
    disposition = unquote(url)
    assert f'filename="{expected}"' in disposition
    # Linux/macOS take NO suffix: assert the windows-only `.exe` is absent there.
    if os_name != "windows":
        assert ".exe" not in disposition


def test_a_caller_may_name_the_saved_file(object_store: ObjectStore) -> None:
    """The install link signs `ITMS-Agent-Setup_<code>.exe` into the grant
    (spec 2026-09-28 §1.2); the bytes are the published artefact, untouched."""
    url = object_store.presign_agent_release_get(
        "1.6.0", "windows", "amd64", expires_in=900,
        download_filename="ITMS-Agent-Setup_" + "A" * 32 + ".exe",
    )
    assert 'filename="ITMS-Agent-Setup_' + "A" * 32 + '.exe"' in unquote(url)
