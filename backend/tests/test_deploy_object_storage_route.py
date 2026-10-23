"""The public object-storage route, asserted against `deploy/docker-compose.yml`.

Everything this file checks fails ONLY IN THE FIELD if it regresses. Add a
`stripPrefix` middleware and the backend suite stays green while every
presigned URL in the fleet starts coming back `SignatureDoesNotMatch`; point
the loadbalancer at 9001 and the MinIO admin console is on the public
entrypoint. Neither is reachable from a unit test of the application, and both
are one careless label away, so the labels themselves are the thing under test.

This is the same argument `test_deploy_env_documentation.py` makes about
`.env.example`: a deployment fact that only prose defends will rot back.

WHAT THIS CANNOT PROVE. That Traefik parses the rule the way it reads, that
MinIO refuses an unsigned GET, and that a presigned URL survives the route are
properties of Traefik and MinIO. They were measured against the real
containers using the rule as `docker compose config` renders it, and the
results are recorded in `deploy/README.md`, "Object storage on :443". A test
here that asserted them would be asserting against nothing.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO / "deploy" / "docker-compose.yml"
ENV_EXAMPLE = REPO / "deploy" / ".env.example"


@pytest.fixture(scope="module")
def minio() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["minio"]


@pytest.fixture(scope="module")
def labels(minio) -> dict[str, str]:
    return {k: str(v) for k, v in minio.get("labels", {}).items()}


def test_object_storage_is_routed_on_the_operator_entrypoint(labels) -> None:
    """Without this router MinIO has no published port and no route at all,
    which is the defect: every presigned URL named a host no endpoint can
    resolve."""
    assert labels.get("traefik.enable") == "true"
    assert labels["traefik.http.routers.minio.entrypoints"] == "websecure"
    assert labels["traefik.http.routers.minio.tls"] == "true"


def test_the_route_points_at_the_s3_api_port_and_not_the_console(labels, minio) -> None:
    """The container exposes 9000 (S3 API) and 9001 (admin console). Traefik
    cannot choose between two exposed ports, so the port is pinned — and it is
    pinned to the one that is not an admin UI over the root credential.

    Falsifier: 9001 here puts the MinIO console on :443.
    """
    assert labels["traefik.http.services.minio.loadbalancer.server.port"] == "9000"
    command = minio["command"]
    command = command if isinstance(command, str) else " ".join(map(str, command))
    # The console is on its own port precisely so that pinning 9000 excludes it.
    assert "--console-address :9001" in command, command
    # And nothing is published directly: the console stays inside the network
    # even for someone who can reach this host on an arbitrary port.
    assert not minio.get("ports"), f"minio must publish no host port: {minio.get('ports')}"


def test_nothing_rewrites_the_path(labels) -> None:
    """THE ONE THAT WOULD FAIL SILENTLY IN THE FIELD.

    SigV4 signs the host and the full path. A stripPrefix makes MinIO
    recompute the signature over a path that is not the one that was signed,
    so every grant the platform issues comes back `SignatureDoesNotMatch` —
    measured, not assumed. The route works precisely because the prefix IS the
    bucket name, which makes the public URL ordinary S3 path-style addressing
    with nothing to rewrite.
    """
    for key, value in labels.items():
        assert "stripprefix" not in key.lower(), f"{key} rewrites the path"
        assert "stripprefix" not in value.lower(), f"{key}={value} rewrites the path"
        assert "replacepath" not in key.lower(), f"{key} rewrites the path"
    assert "middlewares" not in " ".join(labels).lower()


def test_the_route_admits_object_reads_and_the_upload_post_and_nothing_else(
    labels,
) -> None:
    """Presign-only, expressed as the four things the rule has to say.

    A rule that dropped the method clauses would route PUT and DELETE, which is
    what turns a leaked storage credential from "reads the fleet's artifacts"
    into "deletes the fleet's backups". A rule that dropped `!Path` would route
    bucket listing.
    """
    rule = labels["traefik.http.routers.minio.rule"]
    assert "\n" not in rule, (
        "the rule is a multi-line YAML scalar — every continuation line must sit "
        f"at the same indent as the first: {rule!r}"
    )
    bucket = "${ITS_S3_BUCKET:-it-suite}"
    assert f"PathPrefix(`/{bucket}/`)" in rule
    # The trailing slash matters twice: it keeps `/it-suiteXYZ` out (PathPrefix
    # is a raw string match, not segment-aware) and it is what `!Path` then
    # subtracts to keep listing out.
    assert f"!Path(`/{bucket}/`)" in rule
    assert "Method(`GET`)" in rule and "Method(`HEAD`)" in rule
    assert f"Path(`/{bucket}`) && Method(`POST`)" in rule
    for never in ("Method(`PUT`)", "Method(`DELETE`)"):
        assert never not in rule, f"{never} must not be routed on the public entrypoint"


def test_the_router_and_the_bucket_cannot_drift_apart(labels) -> None:
    """The prefix is not a name someone chose; it is the bucket. Hard-coding
    `it-suite` here would route nothing for a deployment that set
    ITS_S3_BUCKET, and the failure would be a 404 on every artifact download.
    """
    rule = labels["traefik.http.routers.minio.rule"]
    assert "${ITS_S3_BUCKET" in rule, "the rule must interpolate the configured bucket"
    assert "ITS_S3_BUCKET=" in ENV_EXAMPLE.read_text()


def test_the_api_and_worker_keep_using_the_internal_endpoint(labels) -> None:
    """Bulk object I/O must not leave the Docker network. If a future edit
    "simplifies" this by pointing the services at the public URL, every backup
    chunk starts transiting Traefik and the operator entrypoint."""
    compose = yaml.safe_load(COMPOSE.read_text())
    for service in ("api", "worker"):
        env = compose["services"][service].get("environment", {})
        assert env["ITS_S3_ENDPOINT"] == "http://minio:9000", service
        # The public one is the operator's own hostname, so it belongs in .env
        # and must not be pinned to an internal value here.
        assert "ITS_S3_PUBLIC_ENDPOINT" not in env, service


def test_the_public_endpoint_is_documented_for_the_operator() -> None:
    """It is required in production and its absence stops the api from
    touching object storage at all, so `.env.example` is not courtesy."""
    text = ENV_EXAMPLE.read_text()
    assert "ITS_S3_PUBLIC_ENDPOINT=" in text
    readme = (REPO / "deploy" / "README.md").read_text()
    assert "Object storage on :443" in readme, "storage.py points operators at this"
