"""The object-store grants that let an endpoint reach storage at all
(`app/core/storage.py`).

Two capabilities need one, and they need DIFFERENT SHAPES — which is the point
of this file. `app.deploy` reads ONE object whose key the control plane knows
at dispatch time, so it gets a single-object presigned GET. `backup` reads and
writes MANY objects whose keys are content-derived and therefore unknowable
until the run is under way, so a bundle of per-object URLs cannot cover it.

WHAT THIS SUITE CAN AND CANNOT PROVE, STATED UP FRONT. The prefix condition on
a POST grant is enforced by S3/MinIO, not by this process. `moto` does not
model that enforcement: it accepts an upload whose `key` violates the signed
policy and answers 204. This was checked, not assumed. So every assertion below
is about THE SIGNED POLICY DOCUMENT — the thing this codebase actually
controls and can get wrong — and the enforcement itself is a named gap covered
by neither this suite nor CI. A test that POSTed to moto and asserted a
rejection would pass against a grant with no conditions at all, which is
precisely the fixture blindness this project keeps finding.
"""

import base64
import json
from uuid import uuid4

import pytest
from moto import mock_aws

from app.core.config import Settings
from app.core.storage import MAX_PRESIGNED_TTL_S, ObjectStore

BUCKET = "it-suite-grant-test"


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
        s3_bucket=BUCKET,
    )
    with mock_aws():
        yield ObjectStore(settings)


def _policy(post: dict) -> dict:
    """The signed policy document, decoded. This is the artefact the server
    produces and the object store enforces; asserting on it is asserting on
    what was actually signed rather than on what was passed in."""
    return json.loads(base64.b64decode(post["fields"]["policy"]))


def _key_prefix_conditions(policy: dict) -> list[list]:
    return [
        c
        for c in policy["conditions"]
        if isinstance(c, list) and len(c) == 3 and c[0] == "starts-with" and c[1] == "$key"
    ]


# --------------------------------------------------------------------------- #
# The write grant is bounded to one repository's prefix
# --------------------------------------------------------------------------- #


def test_the_write_grant_is_signed_for_this_repositorys_prefix_and_no_other(store) -> None:
    """THE ASSERTION THAT MAKES THIS A SCOPING TEST.

    A grant with no `starts-with $key` condition is a write grant over the
    WHOLE BUCKET — every tenant's backups, every package artifact, every
    ticket attachment. The policy is decoded and every key condition in it is
    required to name exactly this repository's prefix.

    THE POLICY CARRIES TWO SUCH CONDITIONS AND THAT IS NOT AN ACCIDENT. boto3
    derives one of its own from the `Key=f"{prefix}/${{filename}}"` template,
    and `presign_backup_write` states another explicitly. They were checked to
    be INDEPENDENT: removing either one alone leaves the grant correctly
    scoped (both mutations were run, both correctly still pass), and removing
    BOTH produces `['starts-with', '$key', '']` — a bucket-wide write grant —
    which is what this assertion catches. Asserting over EVERY key condition
    rather than over one of them is what makes that true.

    Falsifier: widen the condition to the org or the bucket root, or drop both
    the template prefix and the explicit condition, and `_key_prefix_conditions`
    comes back empty or naming a shorter path.
    """
    org_id, repo_id = uuid4(), uuid4()
    post = store.presign_backup_write(org_id, repo_id, expires_in=900, max_bytes=4096)

    prefix = f"{org_id}/backup/{repo_id}"
    assert store.backup_prefix(org_id, repo_id) == prefix

    conditions = _key_prefix_conditions(_policy(post))
    assert conditions, "the write grant carries no $key prefix condition: it covers the bucket"
    for cond in conditions:
        assert cond[2] == f"{prefix}/", cond
    # Not the org, not the bucket root, not the backup root.
    for wider in (f"{org_id}/", f"{org_id}/backup/", ""):
        assert all(c[2] != wider for c in conditions), wider


def test_one_orgs_write_grant_names_nothing_of_another_orgs(store) -> None:
    """Two repositories, two orgs, two grants. Neither policy may mention the
    other's prefix anywhere in it.

    Falsifier: derive the prefix from anything the caller supplies rather than
    from `org_id` + `repository_id`, and one grant's policy starts naming the
    other's objects.
    """
    org_a, repo_a = uuid4(), uuid4()
    org_b, repo_b = uuid4(), uuid4()

    grant_a = store.presign_backup_write(org_a, repo_a, expires_in=900, max_bytes=4096)
    grant_b = store.presign_backup_write(org_b, repo_b, expires_in=900, max_bytes=4096)

    text_a = json.dumps(_policy(grant_a))
    text_b = json.dumps(_policy(grant_b))

    assert str(org_a) in text_a and str(repo_a) in text_a
    assert str(org_b) not in text_a and str(repo_b) not in text_a
    assert str(org_a) not in text_b and str(repo_a) not in text_b


def test_the_write_grant_is_bounded_in_size_and_in_time(store) -> None:
    """A grant an endpoint holds is a grant a COMPROMISED endpoint holds. The
    content-length-range is what stops it filling the tenant's storage with one
    object, and the expiry is what stops it being useful tomorrow."""
    org_id, repo_id = uuid4(), uuid4()
    post = store.presign_backup_write(org_id, repo_id, expires_in=900, max_bytes=4096)
    policy = _policy(post)

    ranges = [
        c for c in policy["conditions"] if isinstance(c, list) and c[0] == "content-length-range"
    ]
    assert ranges == [["content-length-range", 0, 4096]], policy["conditions"]
    assert policy["expiration"], "an unexpiring write grant is a standing credential"

    for bad_ttl in (0, -1, MAX_PRESIGNED_TTL_S + 1):
        with pytest.raises(ValueError):
            store.presign_backup_write(org_id, repo_id, expires_in=bad_ttl, max_bytes=4096)
    with pytest.raises(ValueError):
        store.presign_backup_write(org_id, repo_id, expires_in=900, max_bytes=0)


# --------------------------------------------------------------------------- #
# The read grant is one object, and the suffix cannot escape the prefix
# --------------------------------------------------------------------------- #


def test_the_read_grant_names_one_object_under_this_repositorys_prefix(store) -> None:
    from urllib.parse import urlparse

    org_id, repo_id = uuid4(), uuid4()
    url = store.presign_backup_object(
        org_id, repo_id, "manifests/abc123", expires_in=900
    )
    path = urlparse(url).path
    assert path.endswith(f"/{org_id}/backup/{repo_id}/manifests/abc123"), path
    assert "X-Amz-Signature" in url


@pytest.mark.parametrize(
    "suffix",
    [
        "../../other-org/backup/r/chunks/aa/x",  # climbs out of the prefix
        "chunks/../../../etc/passwd",
        "/absolute/key",
        "chunks//aa/x",  # empty segment: normalises differently in proxies
        "chunks\\aa\\x",  # backslash: a Windows agent's idea of a separator
        "chunks/aa/x/",  # trailing slash: names a "directory"
        "",
        ".",
        "..",
        "chunks/aa/\x00x",
    ],
)
def test_a_suffix_that_could_leave_the_prefix_yields_no_grant(store, suffix) -> None:
    """S3 keys are opaque and the service does not normalise `..`, but every
    HTTP client, proxy and CDN between here and the bucket may — so a key
    containing one reaches a sibling tenant's prefix on SOME path. Rejected
    rather than normalised: normalising means guessing which of the two
    readings the caller meant.

    Falsifier: drop `_safe_object_suffix` and the first case signs a grant
    whose path resolves into another org's backup prefix.
    """
    with pytest.raises(ValueError):
        store.presign_backup_object(uuid4(), uuid4(), suffix, expires_in=900)


def test_a_read_grant_cannot_be_signed_for_a_listing(store) -> None:
    """A presigned `list_objects_v2` is a directory listing of a tenant's
    storage — every chunk id, every manifest name, for every device. There is
    no caller that needs one, so the helper refuses the method outright rather
    than relying on nobody passing it."""
    with pytest.raises(ValueError):
        store._presign("list_objects_v2", "some/key", expires_in=900)
    with pytest.raises(ValueError):
        store._presign("put_object", "some/key", expires_in=900)


def test_the_read_grant_is_bounded_in_time(store) -> None:
    for bad_ttl in (0, -1, MAX_PRESIGNED_TTL_S + 1):
        with pytest.raises(ValueError):
            store.presign_backup_object(uuid4(), uuid4(), "manifests/x", expires_in=bad_ttl)
