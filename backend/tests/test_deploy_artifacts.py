"""`app.deploy`'s artifact grant and package identifier
(docs/modules/app-deploy/spec.md §5.2, §6.1).

Two server-side contract gaps the agent found and refused to paper over:

1. `artifact_ref` used to be the `s3://` URI off the `packages` row. The agent
   has no S3 route and must hold no object-store credential (invariant #3), so
   the control plane presigns it at dispatch time instead.
2. `AppDeployParams` carried no package identifier, so a `winget`/`choco`
   deployment could only be named by `detection_rule.name`.

WHAT THESE TESTS ARE BUILT TO AVOID. A guard test whose fixture makes the
guarded and unguarded paths produce the same observable value proves nothing.
Three shapes of that trap are live here and each is dodged explicitly:

* "a URL is generated" is not "the URL expires with the job". Every lifetime
  assertion below reads `X-Amz-Date` + `X-Amz-Expires` out of the URL that was
  actually signed and compares the resulting instant to `job.expires_at`.
* "it is not logged" passes trivially against a fixture that logs nothing. The
  redaction tests plant a CANARY inside the URL and then assert the canary is
  absent from every log record, every audit row and every API body — the same
  way the agent tested its own script-body redaction.
* a cross-org test run through `org_session` passes against code with no
  `org_id` predicate at all, because RLS answers first. The tenancy tests here
  run on the ADMIN engine, where RLS is not in the way and a missing predicate
  is the only thing left to fail.
"""

import datetime as _dt
import json
import logging
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings, override_settings
from app.core.signing import LocalEd25519Signer
from app.core.storage import MAX_PRESIGNED_TTL_S, ObjectStore
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import (
    AuditEvent,
    Deployment,
    DeploymentResult,
    Device,
    Job,
    Organization,
    Package,
)
from app.modules.deploy.artifacts import (
    ARTIFACT_BACKED_INSTALLER_TYPES,
    MANAGER_SOURCED_INSTALLER_TYPES,
    REDACTED_ARTIFACT_REF,
    build_deploy_params,
    manager_package_identifier,
    package_for_job,
    redact_deploy_params,
    refresh_deploy_params,
)
from app.modules.deploy.dispatch import dispatch_deployment
from app.modules.deploy.handler import register_job_types
from app.modules.deploy.packages import INSTALLER_TYPES, DeployValidationError

register_job_types()

# A string that exists nowhere else in this repository. Planted inside a fake
# presigned URL so "the URL did not leak" is an assertion about a value that
# would be trivially visible if it had, rather than about the absence of a
# thing the fixture never produced.
CANARY = "CANARY-4f1b9d3a-artifact-url-must-not-leak"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _RecordingSigner:
    """An `ArtifactUrlSigner` that records every request and returns a URL
    carrying the CANARY."""

    def __init__(self, tag: str = "one") -> None:
        self.calls: list[tuple[UUID, UUID, str, int]] = []
        self.tag = tag

    def presign_package_artifact(
        self, org_id: UUID, package_id: UUID, *, ref: str, expires_in: int
    ) -> str:
        self.calls.append((org_id, package_id, ref, expires_in))
        return f"https://objects.test/{package_id}?{CANARY}={self.tag}&X-Amz-Expires={expires_in}"


def _s3_settings(tmp_path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-presign-test",
    )


@pytest.fixture()
def object_store(tmp_path):
    with mock_aws():
        yield ObjectStore(_s3_settings(tmp_path))


@pytest.fixture()
def job_engine(tmp_path):
    """The REAL engine, with a 900 s TTL that is not the 3600 s default.

    A fixture on the default would make "the URL lives as long as the job" and
    "the URL lives 3600 s" the same observation, and a `dispatch_deployment`
    that ignored `default_ttl_s` entirely would pass.
    """
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "sign.key"), default_ttl_s=900)


async def _fixture(
    admin_engine: AsyncEngine,
    *,
    installer_type: str = "msi",
    with_artifact: bool = True,
    package_identifier: str | None = None,
    detection_rule: dict | None = None,
    bucket: str = "it-suite-presign-test",
) -> tuple[UUID, UUID, UUID, UUID]:
    """(org_id, deployment_id, device_id, package_id) with one device that is
    NOT already in the desired state, so a dispatch produces exactly one job."""
    slug = f"art-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        device = Device(
            org_id=org.id, hostname="w1", os_family="windows", client_id=org.id, site_id=org.id
        )
        session.add(device)
        await session.flush()

        package = Package(
            org_id=org.id,
            name="Acme Widget",
            version="2.0",
            os_family="windows",
            installer_type=installer_type,
            package_identifier=package_identifier,
            detection_rule=(
                detection_rule
                if detection_rule is not None
                else {"type": "package_installed", "name": "Acme Widget"}
            ),
        )
        if with_artifact:
            package.artifact_ref = f"s3://{bucket}/{org.id}/package-artifacts/"
            package.artifact_sha256 = "b" * 64
        session.add(package)
        await session.flush()
        if with_artifact:
            # Completed only now that the package id exists — the key layout is
            # `{org_id}/package-artifacts/{package_id}`.
            package.artifact_ref = f"s3://{bucket}/{org.id}/package-artifacts/{package.id}"
            session.add(package)
            await session.flush()

        deployment = Deployment(
            org_id=org.id,
            package_id=package.id,
            action="install",
            target_type="device",
            target_id=device.id,
        )
        session.add(deployment)
        await session.flush()
        await session.commit()
        return org.id, deployment.id, device.id, package.id


def _url_expiry(url: str) -> _dt.datetime:
    """The instant a presigned URL stops working, read out of the URL itself.

    `X-Amz-Date` + `X-Amz-Expires` is the signed statement about lifetime; a
    test that trusted the value it passed in would still pass if the signing
    call dropped `ExpiresIn` on the floor.
    """
    q = parse_qs(urlparse(url).query)
    signed_at = _dt.datetime.strptime(q["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=_dt.UTC
    )
    return signed_at + _dt.timedelta(seconds=int(q["X-Amz-Expires"][0]))


# --------------------------------------------------------------------------- #
# The partition. The server and the agent must split the enum the same way.
# --------------------------------------------------------------------------- #


def test_the_installer_type_enum_is_partitioned_exactly_once() -> None:
    """`install.go` chooses "download and hash-verify" vs "ask a package
    manager" from `installer_type` ALONE, using two maps. A type in neither is
    refused in the field while this suite stays green; a type in both makes the
    branch depend on iteration order. The server's copy has to be the same
    split or the two sides disagree about what a job means.
    """
    assert ARTIFACT_BACKED_INSTALLER_TYPES & MANAGER_SOURCED_INSTALLER_TYPES == set()
    assert ARTIFACT_BACKED_INSTALLER_TYPES | MANAGER_SOURCED_INSTALLER_TYPES == INSTALLER_TYPES
    assert ARTIFACT_BACKED_INSTALLER_TYPES and MANAGER_SOURCED_INSTALLER_TYPES


def test_manager_sourced_params_carry_no_artifact_and_artifact_backed_no_identifier() -> None:
    """The agent refuses BOTH mismatches (`parseDeployParams`) and re-checks in
    `stage`. This is the server half of that rule, and it lives in one function
    so the two directions cannot be enforced in two places and drift."""
    winget = Package(
        org_id=uuid4(),
        name="Firefox",
        version="1",
        os_family="windows",
        installer_type="winget",
        package_identifier="Mozilla.Firefox",
        detection_rule={"type": "package_installed", "name": "Mozilla Firefox"},
        uninstall_rule={},
    )
    params = build_deploy_params(action="install", package=winget, artifact_url=None)
    assert params.package_identifier == "Mozilla.Firefox"
    assert params.artifact_ref is None
    assert params.artifact_sha256 is None

    # And attaching an artifact to it is not merely unused — it is refused.
    with pytest.raises(DeployValidationError):
        build_deploy_params(
            action="install", package=winget, artifact_url="https://objects.test/x"
        )

    msi = Package(
        org_id=uuid4(),
        name="Acme",
        version="1",
        os_family="windows",
        installer_type="msi",
        artifact_sha256="c" * 64,
        package_identifier="Acme.Widget",  # set, and deliberately NOT forwarded
        detection_rule={"type": "package_installed", "name": "Acme"},
        uninstall_rule={},
    )
    out = build_deploy_params(
        action="install", package=msi, artifact_url="https://objects.test/y"
    )
    assert out.artifact_ref == "https://objects.test/y"
    assert out.artifact_sha256 == "c" * 64
    assert out.package_identifier is None


def test_identifier_falls_back_to_the_value_the_agent_already_derives() -> None:
    """The compatibility argument in one assertion: with no explicit column
    value the server sends exactly `detection_rule["name"]`, which is what
    `install.go::packageIdentifier` reads on its own. An old agent ignoring the
    new member and a new agent reading it therefore compute the same string."""
    pkg = Package(
        org_id=uuid4(),
        name="Google Chrome",  # the DISPLAY name, which is not the winget id
        version="1",
        os_family="windows",
        installer_type="winget",
        detection_rule={"type": "package_installed", "name": "Google.Chrome"},
        uninstall_rule={},
    )
    assert manager_package_identifier(pkg) == "Google.Chrome"

    pkg.package_identifier = "Google.Chrome.Beta"
    assert manager_package_identifier(pkg) == "Google.Chrome.Beta"

    pkg.package_identifier = None
    pkg.detection_rule = {}
    assert manager_package_identifier(pkg) is None


# --------------------------------------------------------------------------- #
# The grant: one object, and only this org's
# --------------------------------------------------------------------------- #


def test_the_grant_names_exactly_one_object_and_not_a_prefix(object_store) -> None:
    org_id, package_id = uuid4(), uuid4()
    key = f"{org_id}/package-artifacts/{package_id}"
    url = object_store.presign_package_artifact(
        org_id, package_id, ref=f"s3://it-suite-presign-test/{key}", expires_in=900
    )
    path = urlparse(url).path
    assert path.endswith(f"/{key}"), path
    # Not the bucket, not the prefix: the signed resource is one key.
    assert f"/{org_id}/package-artifacts" != path
    assert "X-Amz-Signature" in url


def test_a_ref_naming_another_orgs_object_yields_no_url(object_store) -> None:
    """THE KEY IS DERIVED, NOT PARSED. A `packages` row whose `artifact_ref`
    was made to point into another tenant's prefix must produce a ValueError
    and no signature — not a signed grant over whatever it names.

    Falsifier: if `presign_package_artifact` ever read the key out of `ref`,
    this returns a working URL for `victim`'s object.
    """
    victim, attacker, package_id = uuid4(), uuid4(), uuid4()
    victim_ref = f"s3://it-suite-presign-test/{victim}/package-artifacts/{package_id}"
    with pytest.raises(ValueError):
        object_store.presign_package_artifact(
            attacker, package_id, ref=victim_ref, expires_in=900
        )


def test_a_ref_naming_a_prefix_or_another_bucket_yields_no_url(object_store) -> None:
    org_id, package_id = uuid4(), uuid4()
    for bad in (
        f"s3://it-suite-presign-test/{org_id}/package-artifacts/",
        f"s3://someone-elses-bucket/{org_id}/package-artifacts/{package_id}",
        f"s3://it-suite-presign-test/{org_id}/package-artifacts/{package_id}/../../etc",
    ):
        with pytest.raises(ValueError):
            object_store.presign_package_artifact(
                org_id, package_id, ref=bad, expires_in=900
            )


def test_a_lifetime_outside_the_cap_is_refused(object_store) -> None:
    org_id, package_id = uuid4(), uuid4()
    ref = f"s3://it-suite-presign-test/{org_id}/package-artifacts/{package_id}"
    for bad_ttl in (0, -1, MAX_PRESIGNED_TTL_S + 1):
        with pytest.raises(ValueError):
            object_store.presign_package_artifact(
                org_id, package_id, ref=ref, expires_in=bad_ttl
            )


# --------------------------------------------------------------------------- #
# The lifetime, measured against the job it authorises
# --------------------------------------------------------------------------- #


async def test_the_url_expires_when_the_job_does(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, job_engine
) -> None:
    """THE ASSERTION THAT MAKES THIS A LIFETIME TEST.

    A test that only checked "a URL was produced" would pass against a
    dispatcher that presigned for a week. This reads `X-Amz-Date` +
    `X-Amz-Expires` back out of the URL that was actually signed and compares
    the instant to `job.expires_at`.

    The engine's TTL here is 900 s, not the 3600 s default, so a dispatcher
    that hard-coded either number fails.
    """
    org_id, deployment_id, _device_id, package_id = await _fixture(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        pending = await dispatch_deployment(
            session,
            job_engine,
            org_id=org_id,
            deployment_id=deployment_id,
            now=_dt.datetime.now(_dt.UTC),
            artifact_urls=object_store,
        )
        assert len(pending) == 1
        job = (
            await session.execute(select(Job).where(Job.id == pending[0][0]))
        ).scalar_one()

    url = job.params["artifact_ref"]
    assert url.startswith("https://"), "the agent has no s3:// route"
    assert str(package_id) in urlparse(url).path

    drift = abs((_url_expiry(url) - job.expires_at).total_seconds())
    assert drift <= 2, (
        f"the artifact URL expires at {_url_expiry(url)} and the job it authorises at "
        f"{job.expires_at} ({drift}s apart). Longer is a standing grant on an expired "
        f"job; shorter is a download that 403s inside a job the agent may still run."
    )
    # And the window really is the engine's, not a constant that happens to
    # match: 900 s, not 3600 s.
    assert 890 <= (job.expires_at - job.issued_at).total_seconds() <= 910


async def test_one_grant_per_dispatch_not_one_per_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, job_engine
) -> None:
    """Every device in a group installs the same object. Minting one URL per
    device would be N bearer credentials where one suffices."""
    slug = f"grp-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        from app.models import DeviceGroup

        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        group = DeviceGroup(org_id=org.id, name="g", client_id=org.id)
        session.add(group)
        await session.flush()
        for host in ("g1", "g2", "g3"):
            session.add(
                Device(
                    org_id=org.id,
                    hostname=host,
                    os_family="windows",
                    device_group_id=group.id,
                    client_id=org.id,
                    site_id=org.id,
                )
            )
        package = Package(
            org_id=org.id,
            name="Acme",
            version="1",
            os_family="windows",
            installer_type="msi",
            artifact_sha256="d" * 64,
            detection_rule={},
        )
        session.add(package)
        await session.flush()
        package.artifact_ref = f"s3://b/{org.id}/package-artifacts/{package.id}"
        deployment = Deployment(
            org_id=org.id,
            package_id=package.id,
            action="install",
            target_type="group",
            target_id=group.id,
        )
        session.add(deployment)
        await session.flush()
        await session.commit()
        org_id, deployment_id = org.id, deployment.id

    signer = _RecordingSigner()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        pending = await dispatch_deployment(
            session,
            job_engine,
            org_id=org_id,
            deployment_id=deployment_id,
            now=_dt.datetime.now(_dt.UTC),
            artifact_urls=signer,
        )
    assert len(pending) == 3
    assert len(signer.calls) == 1, signer.calls
    assert signer.calls[0][3] == job_engine.default_ttl_s


async def test_a_manager_sourced_deployment_asks_for_no_grant_at_all(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, job_engine
) -> None:
    org_id, deployment_id, _device_id, _package_id = await _fixture(
        admin_engine,
        installer_type="winget",
        with_artifact=False,
        package_identifier="Mozilla.Firefox",
    )
    signer = _RecordingSigner()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        pending = await dispatch_deployment(
            session,
            job_engine,
            org_id=org_id,
            deployment_id=deployment_id,
            now=_dt.datetime.now(_dt.UTC),
            artifact_urls=signer,
        )
        job = (
            await session.execute(select(Job).where(Job.id == pending[0][0]))
        ).scalar_one()
    assert signer.calls == []
    assert job.params["artifact_ref"] is None
    assert job.params["artifact_sha256"] is None
    assert job.params["package_identifier"] == "Mozilla.Firefox"


async def test_an_artifact_backed_package_with_no_upload_is_refused_before_dispatch(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, job_engine
) -> None:
    """Fail closed. Dispatching it would spend a signed job to learn what the
    server already knows."""
    org_id, deployment_id, _device_id, _package_id = await _fixture(
        admin_engine, installer_type="msi", with_artifact=False
    )
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(DeployValidationError):
            await dispatch_deployment(
                session,
                job_engine,
                org_id=org_id,
                deployment_id=deployment_id,
                now=_dt.datetime.now(_dt.UTC),
                artifact_urls=_RecordingSigner(),
            )


# --------------------------------------------------------------------------- #
# The URL is a bearer credential: it must not leak
# --------------------------------------------------------------------------- #


async def test_the_url_reaches_no_log_record_and_no_audit_row(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, job_engine, caplog
) -> None:
    """THE CANARY TEST.

    "Not logged" is trivially true of a fixture that logs nothing, so the URL
    handed to dispatch carries a string that exists nowhere else in this
    repository. Every log record captured across the whole dispatch, and every
    audit row it writes, is then searched for that string.

    Falsifier: any `logger.info("dispatching %s", params)` anywhere on this
    path, or an audit `metadata` that echoes the params.
    """
    org_id, deployment_id, _device_id, _package_id = await _fixture(admin_engine)
    signer = _RecordingSigner()

    with caplog.at_level(logging.DEBUG):
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            pending = await dispatch_deployment(
                session,
                job_engine,
                org_id=org_id,
                deployment_id=deployment_id,
                now=_dt.datetime.now(_dt.UTC),
                artifact_urls=signer,
            )
            job = (
                await session.execute(select(Job).where(Job.id == pending[0][0]))
            ).scalar_one()
            events = (
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.org_id == org_id)
                    )
                )
                .scalars()
                .all()
            )

    # The fixture is not vacuous: the URL really did reach the signed params.
    assert CANARY in job.params["artifact_ref"]
    assert caplog.records, "no log records were captured; this test would be vacuous"

    for record in caplog.records:
        assert CANARY not in record.getMessage(), record.name
        assert CANARY not in str(record.args or ""), record.name
    assert events, "no audit rows were written; this test would be vacuous"
    for event in events:
        assert CANARY not in json.dumps(event.meta or {}), event.action


def test_redaction_replaces_the_url_rather_than_shortening_it() -> None:
    """A "safe prefix" of a signed URL is still the endpoint, the bucket, the
    tenant id and the object id. It is replaced whole."""
    params = {
        "action": "install",
        "installer_type": "msi",
        "artifact_ref": f"https://objects.test/x?{CANARY}=1&X-Amz-Signature=deadbeef",
        "artifact_sha256": "e" * 64,
    }
    out = redact_deploy_params(params)
    assert CANARY not in json.dumps(out)
    assert "X-Amz-Signature" not in json.dumps(out)
    assert "objects.test" not in json.dumps(out)
    # Everything else is untouched — redaction is not a blanket.
    assert out["artifact_sha256"] == "e" * 64
    assert out["installer_type"] == "msi"
    # The input is not mutated: `job.params` is a live ORM attribute.
    assert CANARY in params["artifact_ref"]

    # A manager-sourced job has nothing to redact and is returned unchanged.
    manager = {"action": "install", "installer_type": "winget", "artifact_ref": None}
    assert redact_deploy_params(manager) == manager


async def test_the_url_never_reaches_the_jobs_api(api_env) -> None:
    """END TO END, THROUGH THE REAL ROUTES, AS THE WEAKEST CALLER WHO CAN SEE
    THE JOB.

    Upload an artifact, execute a deployment, then read the job back as a
    `read-only` user (who holds `jobs.read` and none of `deploy.execute`,
    `deploy.package.manage`). The signed params in the database really do carry
    a live presigned URL — asserted directly against the row so this is not a
    test of a fixture that never produced one — and none of `GET /jobs`,
    `GET /jobs/{id}` may show it.

    Falsifier: drop `params_redactor` from the `app.deploy` registration and
    `X-Amz-Signature` appears in both bodies.
    """
    from app.models import Deployment as _Deployment

    org_id = await api_env.create_org("deploy-redact")
    admin = await api_env.create_user(org_id, "org-admin")
    ro = await api_env.create_user(org_id, "read-only")
    admin_headers = await api_env.headers(org_id, admin)
    ro_headers = await api_env.headers(org_id, ro)

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)

        created = await api_env.client.post(
            "/api/v1/deploy/packages",
            json={
                "name": "Acme",
                "version": "1.0",
                "os_family": "windows",
                "installer_type": "msi",
                "detection_rule": {},
            },
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text
        package_id = created.json()["id"]

        uploaded = await api_env.client.post(
            f"/api/v1/deploy/packages/{package_id}/artifact",
            content=b"MSI-BYTES",
            headers=admin_headers,
        )
        assert uploaded.status_code == 200, uploaded.text

        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
            device = Device(
                org_id=org_id,
                hostname="redact-1",
                os_family="windows",
                client_id=org_id,
                site_id=org_id,
            )
            s.add(device)
            await s.commit()
            device_id = device.id

        deployment = await api_env.client.post(
            "/api/v1/deploy/deployments",
            json={
                "package_id": package_id,
                "action": "install",
                "target_type": "device",
                "target_id": str(device_id),
            },
            headers=admin_headers,
        )
        assert deployment.status_code == 201, deployment.text
        executed = await api_env.client.post(
            f"/api/v1/deploy/deployments/{deployment.json()['id']}/execute",
            headers=admin_headers,
        )
        assert executed.status_code == 200, executed.text
        job_id = executed.json()["job_ids"][0]

    # The fixture is not vacuous: a live grant really is in the signed params.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        row = (await session.execute(select(Job).where(Job.id == UUID(job_id)))).scalar_one()
    assert row.params["artifact_ref"].startswith("https://")
    assert "X-Amz-Signature" in row.params["artifact_ref"]

    detail = await api_env.client.get(f"/api/v1/jobs/{job_id}", headers=ro_headers)
    assert detail.status_code == 200, detail.text
    assert "X-Amz-Signature" not in detail.text
    assert "amazonaws" not in detail.text
    body = detail.json()
    assert body["params"]["artifact_ref"] == REDACTED_ARTIFACT_REF
    # Redaction is targeted, not a blanket: the hash the operator needs to
    # correlate the deployment is still there.
    assert body["params"]["artifact_sha256"]

    listed = await api_env.client.get(
        f"/api/v1/jobs?device_id={device_id}", headers=ro_headers
    )
    assert listed.status_code == 200, listed.text
    assert "X-Amz-Signature" not in listed.text

    # And the deployment itself is untouched by any of this.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        dep = (
            await session.execute(select(_Deployment).where(_Deployment.org_id == org_id))
        ).scalar_one()
        assert dep.state == "dispatching"


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #


async def test_a_retry_gets_a_fresh_url_sized_to_its_new_window(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, object_store, job_engine, tmp_path
) -> None:
    """`resign_for_retry` hands an attempt a whole fresh window, and the first
    retry is at least `STUCK_DISPATCH_AFTER` (10 min) after dispatch. Re-signing
    the original params would re-sign a URL scoped to a window that has moved.

    THE FIXTURE IS BUILT SO THE TWO PATHS CANNOT COINCIDE. SigV4 signing is
    deterministic to the second, so "the URL changed" is not an observable when
    dispatch and retry happen in the same second and ask for the same window —
    a refresher that never ran would produce a byte-identical string and the
    test would pass either way. The retry therefore runs on an engine with a
    4000 s TTL against a job dispatched with a 900 s one, so the signed
    `X-Amz-Expires` says which window the URL was minted for.

    Falsifier: `params_refresher` not registered, or registered and ignored —
    then the retry's URL still says `X-Amz-Expires=900` while `job.expires_at`
    has moved 4000 s out, and the grant dies ~52 minutes before the job it
    authorises.
    """
    org_id, deployment_id, _device_id, _package_id = await _fixture(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        pending = await dispatch_deployment(
            session,
            job_engine,  # 900 s
            org_id=org_id,
            deployment_id=deployment_id,
            now=_dt.datetime.now(_dt.UTC),
            artifact_urls=object_store,
        )
        job_id = pending[0][0]

    retry_engine = JobEngine(
        signer=LocalEd25519Signer(tmp_path / "sign.key"), default_ttl_s=4000
    )
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        first_url, first_sig = job.params["artifact_ref"], job.signature
        assert int(parse_qs(urlparse(first_url).query)["X-Amz-Expires"][0]) == 900

        # Pretend the job has been sitting dispatched: push its window back so
        # the original grant is stale, exactly as a real retry finds it.
        job.expires_at = _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=5)
        session.add(job)
        await session.flush()

        # The refresher runs in the worker, where it resolves its own store
        # from `get_settings()` — there is no signer to inject through
        # `resign_for_retry`. `override_settings` is the seam that gives it
        # the same moto-backed bucket the dispatch used.
        with mock_aws(), override_settings(_s3_settings(tmp_path)):
            await retry_engine.resign_for_retry(session, job)

        second_url = job.params["artifact_ref"]
        assert second_url is not None
        assert second_url != first_url, "the retry re-signed the original grant"
        assert job.signature != first_sig
        assert job.attempts == 1

        window = int(parse_qs(urlparse(second_url).query)["X-Amz-Expires"][0])
        assert 3990 <= window <= 4000, (
            f"the refreshed grant is good for {window}s but the retry's own window is "
            f"{(job.expires_at - job.issued_at).total_seconds()}s"
        )
        drift = abs((_url_expiry(second_url) - job.expires_at).total_seconds())
        assert drift <= 5, (
            f"the refreshed URL expires {drift}s away from the retry's own "
            f"expires_at; a retry whose grant died before its job is the failure "
            f"this refresher exists to prevent"
        )
        assert _url_expiry(second_url) > _dt.datetime.now(_dt.UTC)


async def test_a_manager_sourced_job_is_re_signed_with_its_params_untouched(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, job_engine
) -> None:
    """Nothing in a manager-sourced job's params is time-bounded, so the
    refresher must leave them alone rather than blanking a field."""
    org_id, deployment_id, _d, _p = await _fixture(
        admin_engine,
        installer_type="choco",
        with_artifact=False,
        package_identifier="googlechrome",
    )
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        pending = await dispatch_deployment(
            session,
            job_engine,
            org_id=org_id,
            deployment_id=deployment_id,
            now=_dt.datetime.now(_dt.UTC),
            artifact_urls=_RecordingSigner(),
        )
        job = (
            await session.execute(select(Job).where(Job.id == pending[0][0]))
        ).scalar_one()
        before = dict(job.params)
        await job_engine.resign_for_retry(session, job)
        assert job.params == before


async def test_a_retry_whose_package_is_gone_lands_terminal_instead_of_wedging(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, job_engine
) -> None:
    """The refresher runs inside the org's job-expiry sweep, where raising
    costs that org every other job in the cycle. A package that has been
    deleted yields `artifact_ref: null`, which the agent refuses on arrival —
    terminal and legible."""
    org_id, deployment_id, _d, package_id = await _fixture(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        pending = await dispatch_deployment(
            session,
            job_engine,
            org_id=org_id,
            deployment_id=deployment_id,
            now=_dt.datetime.now(_dt.UTC),
            artifact_urls=_RecordingSigner(),
        )
        job_id = pending[0][0]

    async with AsyncSession(admin_engine) as session:
        pkg = (
            await session.execute(select(Package).where(Package.id == package_id))
        ).scalar_one()
        pkg.artifact_ref = None
        pkg.artifact_sha256 = None
        session.add(pkg)
        await session.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await job_engine.resign_for_retry(session, job)  # must not raise
        assert job.params["artifact_ref"] is None
        assert job.status.value == "queued"


# --------------------------------------------------------------------------- #
# Tenancy, on the ADMIN engine
# --------------------------------------------------------------------------- #


async def test_the_refresher_will_not_cross_orgs_without_rls_to_stop_it(
    admin_engine: AsyncEngine,
) -> None:
    """PINNED ON THE ADMIN ENGINE ON PURPOSE.

    Run through `org_session`, this passes even against a `refresh_deploy_params`
    with no `org_id` predicate anywhere in it, because RLS filters the rows
    before the query's WHERE clause ever matters — the test would be measuring
    Postgres, not the code. Here RLS is out of the way, so the only thing that
    can keep org B's package out of org A's job is the predicate itself.

    AND IT ASSERTS ON THE ROW THE QUERY RETURNS, NOT ON THE URL. Asserting
    only "no URL came back" would pass against a query with no tenancy
    predicate at all, because `presign_package_artifact` would ALSO refuse org
    B's package — it derives the key from `org_id` instead of reading it out
    of the stored ref. Two guards masking each other is exactly the fixture
    blindness this file exists to avoid, so `package_for_job` is checked
    directly and the end-to-end refusal is checked underneath it.

    Falsifier: drop `Package.org_id == org_id` (or the deployment's) from
    `package_for_job` and it hands back org B's package row.
    """
    org_a, _dep_a, _dev_a, _pkg_a = await _fixture(admin_engine)
    org_b, dep_b, dev_b, pkg_b = await _fixture(admin_engine)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        # A job that BELONGS to org A but whose bookkeeping row points at org
        # B's deployment. Only an explicit predicate rejects this.
        device_a = (
            await session.execute(select(Device).where(Device.org_id == org_a))
        ).scalars().first()
        assert device_a is not None
        now = _dt.datetime.now(_dt.UTC)
        job = Job(
            org_id=org_a,
            type="app.deploy",
            target_type="device",
            target_id=device_a.id,
            params={
                "action": "install",
                "installer_type": "msi",
                "artifact_ref": "https://stale.example/x",
                "artifact_sha256": "b" * 64,
            },
            nonce="n",
            issued_at=now,
            expires_at=now + _dt.timedelta(seconds=900),
        )
        session.add(job)
        await session.flush()
        session.add(
            DeploymentResult(
                org_id=org_b,
                deployment_id=dep_b,
                device_id=dev_b,
                job_id=job.id,
                state="running",
            )
        )
        await session.commit()

        # THE PRIMARY ASSERTION: the lookup itself, with RLS out of the way.
        found = await package_for_job(session, job)
        assert found is None, (
            f"org A's job resolved to package {getattr(found, 'id', None)}, which belongs "
            f"to org B ({pkg_b}). The join reached across a tenant boundary and only RLS "
            f"would have stopped it in production — invariant #5 requires the predicate."
        )

        # And the whole path fails closed on top of it.
        refreshed = await refresh_deploy_params(session, job)

    assert refreshed is not None
    assert refreshed["artifact_ref"] is None, (
        "org A's job was refreshed from org B's package: the refresher reached "
        "across a tenant boundary with RLS out of the way"
    )
