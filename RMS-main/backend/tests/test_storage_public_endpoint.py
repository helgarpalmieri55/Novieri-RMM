"""Where a presigned grant POINTS (`app/core/storage.py`).

`test_storage_grants.py` covers what a grant permits — one object, one
operation, bounded in time. Every one of those assertions held while the URLs
were unusable: they named `http://minio:9000`, the control plane's own route
to object storage, which resolves inside the Docker network and nowhere an
endpoint can see. A grant that is perfectly scoped at a host the fleet cannot
resolve is a `app.deploy` job that dispatches cleanly and dies at the download.

THE FIXTURE IS THE WHOLE TEST DESIGN HERE. "A presigned URL was generated"
passes against a URL pointing anywhere, and — worse — an assertion about the
public host passes trivially if the fixture's internal and public endpoints are
the same string, because then the guarded and unguarded code paths produce the
identical observable value. So:

* `INTERNAL` and `PUBLIC` below are deliberately DIFFERENT, and
  `test_the_fixture_can_tell_the_two_endpoints_apart` fails the suite if a
  later edit ever makes them equal.
* Every assertion names the host it wants AND asserts the internal host is
  absent, so a resolution that reverted to `s3_endpoint` fails on a wrong
  value rather than passing on a substring.

WHAT IS PROVED HERE VS. AGAINST A REAL STORE. There is no mock object store in
this file and none is needed: SigV4 query signing is pure computation and
touches no socket, so the URLs below are the real ones this code would hand an
agent. The single call that does need the network is the bucket check in
`ObjectStore.__init__`, stubbed by the fixture. (`moto` is not used precisely
because it only intercepts AWS-shaped URLs — it lets a request to
`http://minio:9000` out to the real network — so a moto fixture would have
forced the internal endpoint to be an AWS hostname and made the realistic
value untestable.)

That SigV4 survives the public route at all, that MinIO denies an unsigned
GET, and that an expired grant is refused are properties of MinIO and Traefik
rather than of this code; they were established against the real containers
and are recorded in `deploy/README.md`'s "Object storage on :443" section.
"""

from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.core.signing import LocalEd25519Signer
from app.core.storage import ObjectStore, PublicEndpointError, resolve_presign_endpoint
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import Deployment, Device, Job, Organization, Package
from app.modules.deploy.artifacts import artifact_url_for
from app.modules.deploy.dispatch import dispatch_deployment
from app.modules.deploy.handler import register_job_types

register_job_types()

BUCKET = "it-suite-public-test"

# The internal name is the one the compose stack really uses, and the public
# one is the operator's real hostname. They differ in scheme, host AND port, so
# no assertion below can pass by coincidence.
INTERNAL = "http://minio:9000"
INTERNAL_HOST = "minio:9000"
PUBLIC = "https://itms.toeshee.com"
PUBLIC_HOST = "itms.toeshee.com"


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket=BUCKET,
        s3_endpoint=INTERNAL,
        s3_public_endpoint=PUBLIC,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture()
def store(tmp_path):
    # `_ensure_bucket` is the one constructor step that opens a socket, and it
    # is about the INTERNAL client. Everything under test here is offline
    # signing, so it is stubbed rather than mocked out with a fake backend.
    with patch.object(ObjectStore, "_ensure_bucket", lambda self: None):
        yield ObjectStore(_settings(tmp_path))


def test_the_fixture_can_tell_the_two_endpoints_apart(tmp_path) -> None:
    """The guard on every other test in this file.

    If `INTERNAL` and `PUBLIC` were ever made equal, "the URL carries the
    public host" and "the URL carries the internal host" would be the same
    observation, and this whole suite would pass against code that never
    learned about a second endpoint at all.
    """
    settings = _settings(tmp_path)
    assert settings.s3_endpoint != settings.s3_public_endpoint
    assert INTERNAL_HOST not in PUBLIC and PUBLIC_HOST not in INTERNAL


# --------------------------------------------------------------------------- #
# Where the grant points
# --------------------------------------------------------------------------- #


def test_a_package_artifact_grant_carries_the_public_host(store) -> None:
    """`app.deploy`'s artifact URL, asserted on the HOST.

    Falsifier: sign against `s3_endpoint` and this reads `minio:9000`.
    """
    org_id, package_id = uuid4(), uuid4()
    key = f"{org_id}/package-artifacts/{package_id}"
    url = store.presign_package_artifact(
        org_id, package_id, ref=f"s3://{BUCKET}/{key}", expires_in=900
    )
    parsed = urlparse(url)
    assert parsed.netloc == PUBLIC_HOST, url
    assert parsed.scheme == "https"
    assert INTERNAL_HOST not in url
    assert "X-Amz-Signature" in url


def test_the_bucket_is_the_first_path_segment(store) -> None:
    """PATH STYLE, which is what makes one hostname and one certificate enough.

    The public route is a path prefix on the operator's existing hostname, so
    the bucket has to be the first segment of the path — that is the prefix
    Traefik matches. Virtual-host style (`<bucket>.itms.toeshee.com`) would
    need a DNS name and a certificate SAN the operator does not have, and
    botocore chooses it whenever it decides an endpoint looks like AWS.

    Falsifier: `addressing_style` drifts to virtual and the bucket moves into
    the hostname, where this assertion fails on both halves.
    """
    org_id, package_id = uuid4(), uuid4()
    key = f"{org_id}/package-artifacts/{package_id}"
    url = store.presign_package_artifact(
        org_id, package_id, ref=f"s3://{BUCKET}/{key}", expires_in=900
    )
    parsed = urlparse(url)
    assert parsed.path == f"/{BUCKET}/{key}", parsed.path
    assert BUCKET not in parsed.netloc, "the bucket must not be in the hostname"


def test_a_backup_object_grant_carries_the_public_host(store) -> None:
    org_id, repo_id = uuid4(), uuid4()
    url = store.presign_backup_object(org_id, repo_id, "manifests/m1", expires_in=900)
    parsed = urlparse(url)
    assert parsed.netloc == PUBLIC_HOST, url
    assert INTERNAL_HOST not in url
    assert parsed.path == f"/{BUCKET}/{org_id}/backup/{repo_id}/manifests/m1"


def test_a_backup_write_grant_posts_to_the_public_host(store) -> None:
    """The POST-policy upload target is a URL too, and it was signed against
    the same wrong endpoint. `fields` carry the signature; `url` carries the
    host, and an agent POSTing to `http://minio:9000` reaches nothing."""
    org_id, repo_id = uuid4(), uuid4()
    post = store.presign_backup_write(org_id, repo_id, expires_in=900, max_bytes=1024)
    parsed = urlparse(post["url"])
    assert parsed.netloc == PUBLIC_HOST, post["url"]
    assert INTERNAL_HOST not in post["url"]
    # Bucket-level POST: exactly `/{bucket}`, which is the one non-object path
    # the public route admits.
    assert parsed.path == f"/{BUCKET}"


def test_internal_reads_and_writes_still_use_the_internal_endpoint(store) -> None:
    """The public route is for endpoints. Bulk object I/O from the api and
    worker must not leave the container network — routing it through Traefik
    and back would put every backup chunk on the public entrypoint.

    Falsifier: point the one client at the public endpoint (the lazy fix) and
    this reads `itms.toeshee.com`.
    """
    assert store._client.meta.endpoint_url == INTERNAL
    assert store._signing_client.meta.endpoint_url == PUBLIC
    assert store._client is not store._signing_client


async def test_app_deploy_produces_a_url_an_endpoint_can_resolve(store) -> None:
    """Requirement stated as `app.deploy` sees it: the URL that ends up in the
    signed job params names the public host.

    Driven through `artifact_url_for` — the function `dispatch_deployment`
    calls — with a real `ObjectStore` rather than a recording stub, because a
    stub returns whatever the test told it to and would carry the public host
    even from code that signs against the internal one.
    """
    org_id = uuid4()
    package = Package(
        org_id=org_id,
        name="Acme Widget",
        version="2.0",
        os_family="windows",
        installer_type="msi",
        detection_rule={"type": "package_installed", "name": "Acme Widget"},
        uninstall_rule={},
    )
    package.id = uuid4()
    package.artifact_ref = f"s3://{BUCKET}/{org_id}/package-artifacts/{package.id}"
    package.artifact_sha256 = "b" * 64

    url = artifact_url_for(store, org_id=org_id, package=package, expires_in=900)
    assert urlparse(url).netloc == PUBLIC_HOST, url
    assert INTERNAL_HOST not in url
    assert str(package.id) in urlparse(url).path


async def test_a_dispatched_deploy_job_carries_a_resolvable_artifact_url(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, store, tmp_path
) -> None:
    """The whole path, ending at the params an agent would actually receive.

    `test_deploy_artifacts.py` drives this dispatch too, but with a store whose
    internal and public endpoints are the same — which is exactly the fixture
    that cannot tell a correct signer from one that ignores the public
    endpoint. Here they differ, so the assertion is on the host in
    `job.params["artifact_ref"]`: the string the signed envelope carries to
    the endpoint, not an intermediate return value.
    """
    slug = f"pub-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(org_id=org.id, hostname="w1", os_family="windows")
        package = Package(
            org_id=org.id,
            name="Acme Widget",
            version="2.0",
            os_family="windows",
            installer_type="msi",
            detection_rule={"type": "package_installed", "name": "Acme Widget"},
        )
        session.add_all([device, package])
        await session.flush()
        package.artifact_ref = f"s3://{BUCKET}/{org.id}/package-artifacts/{package.id}"
        package.artifact_sha256 = "b" * 64
        deployment = Deployment(
            org_id=org.id,
            package_id=package.id,
            action="install",
            target_type="device",
            target_id=device.id,
        )
        session.add(deployment)
        await session.flush()
        org_id, deployment_id, package_id = org.id, deployment.id, package.id
        await session.commit()

    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "sign.key"), default_ttl_s=900)
    async with org_session(org_id, engine=app_engine) as session:
        pending = await dispatch_deployment(
            session,
            engine,
            org_id=org_id,
            deployment_id=deployment_id,
            now=datetime.now(UTC),
            artifact_urls=store,
        )
        assert len(pending) == 1
        job = (await session.execute(select(Job).where(Job.id == pending[0][0]))).scalar_one()

    url = job.params["artifact_ref"]
    assert urlparse(url).netloc == PUBLIC_HOST, (
        f"the agent would be handed {url!r}, whose host it cannot resolve"
    )
    assert INTERNAL_HOST not in url
    assert urlparse(url).path == f"/{BUCKET}/{org_id}/package-artifacts/{package_id}"


# --------------------------------------------------------------------------- #
# The refusals. Each one is a URL that would sign cleanly and fail in the field.
# --------------------------------------------------------------------------- #


def _outcome(settings: Settings) -> tuple[str, str]:
    """`("refused", message)` or `("SIGNED AGAINST", endpoint)`.

    A refusal test written as a bare `pytest.raises` fails with "DID NOT
    RAISE", which names nothing: it says the guard is gone but not what the
    code would have done instead. Removing any guard below was checked against
    this helper, and each one then fails naming the endpoint it would have
    signed against — which is the value an operator would otherwise have had
    to recover from a 403 on an endpoint at 02:00.
    """
    try:
        return ("SIGNED AGAINST", str(resolve_presign_endpoint(settings)))
    except PublicEndpointError as exc:
        return ("refused", str(exc))


def test_production_without_a_public_endpoint_refuses_to_build_a_store(tmp_path) -> None:
    """No silent fallback to the internal name. That fallback is the defect:
    it produces a job that dispatches cleanly and fails at the download.

    Falsifier: `resolve_presign_endpoint` returns `settings.s3_endpoint` and a
    store is built that mints `http://minio:9000/...` grants.
    """
    settings = _settings(tmp_path, environment="production", s3_public_endpoint=None)
    kind, detail = _outcome(settings)
    assert kind == "refused", f"a production store would have signed grants at {detail}"
    assert "ITS_S3_PUBLIC_ENDPOINT" in detail
    # And the error names the value it refused to sign against, so the operator
    # is not left guessing which setting was wrong.
    assert INTERNAL in detail

    # The refusal is at construction, not at the first dispatch.
    with pytest.raises(PublicEndpointError):
        with patch.object(ObjectStore, "_ensure_bucket", lambda self: None):
            ObjectStore(settings)

    # Blank and whitespace are "unset", not a hostname.
    for blank in ("", "   "):
        kind, detail = _outcome(
            _settings(tmp_path, environment="production", s3_public_endpoint=blank)
        )
        assert kind == "refused", f"{blank!r} was accepted, signing at {detail}"


def test_production_refuses_a_plaintext_public_endpoint(tmp_path) -> None:
    """A presigned URL is a bearer credential for the object it names. In the
    clear it is a bearer credential for anything on the path.

    This also catches the copy-paste that survives every other check: an
    operator who sets ITS_S3_PUBLIC_ENDPOINT to the internal value.
    """
    kind, detail = _outcome(
        _settings(tmp_path, environment="production", s3_public_endpoint=INTERNAL)
    )
    assert kind == "refused", f"plaintext accepted: grants would be signed at {detail}"
    assert INTERNAL in detail


def test_a_public_endpoint_with_a_path_prefix_is_refused(tmp_path) -> None:
    """MEASURED against the real MinIO, not assumed. SigV4 signs the host and
    the full path:

    * strip the prefix at the proxy and the store recomputes the signature
      over a different path → `SignatureDoesNotMatch`;
    * keep it and S3 path-style addressing reads the first segment as the
      bucket → `https://host/objects/it-suite/key` asks for bucket `objects`.

    Both are 403/404 at the endpoint and neither is visible here, so the
    configuration is refused up front.
    """
    for bad in (
        f"{PUBLIC}/objects",
        f"{PUBLIC}/s3/",
        f"{PUBLIC}/{BUCKET}",  # the bucket is added by the signer, not the endpoint
    ):
        kind, detail = _outcome(_settings(tmp_path, s3_public_endpoint=bad))
        assert kind == "refused", (
            f"{bad!r} was accepted; every grant would be signed at {detail}, whose "
            "path either gets stripped (SignatureDoesNotMatch) or gets read as the "
            "bucket name (NoSuchBucket)"
        )
        assert "path" in detail

    # A bare trailing slash is not a path — it is normalised, not refused, so
    # a copied-with-slash URL does not fail a deployment for punctuation.
    assert resolve_presign_endpoint(
        _settings(tmp_path, s3_public_endpoint=f"{PUBLIC}/")
    ) == PUBLIC


def test_a_public_endpoint_that_is_not_an_absolute_url_is_refused(tmp_path) -> None:
    for bad in ("itms.toeshee.com", "//itms.toeshee.com", "https://", "s3://bucket"):
        kind, detail = _outcome(_settings(tmp_path, s3_public_endpoint=bad))
        assert kind == "refused", f"{bad!r} was accepted, signing at {detail}"


def test_dev_falls_back_to_the_internal_endpoint(tmp_path) -> None:
    """Explicitly documented, not incidental: a developer running MinIO on
    localhost has one address for both roles, and the whole moto-backed suite
    configures neither. Production is where the fallback is a defect, and
    production is where it raises."""
    assert (
        resolve_presign_endpoint(_settings(tmp_path, s3_public_endpoint=None)) == INTERNAL
    )
    assert (
        resolve_presign_endpoint(
            _settings(tmp_path, s3_public_endpoint=None, s3_endpoint=None)
        )
        is None
    )
