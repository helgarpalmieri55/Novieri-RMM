"""How an `app.deploy` job names the thing it is about to install.

Two questions, one answer per installer type, and the whole point of this
module is that the two answers cannot disagree.

* An **artifact-backed** package (`msi`, `exe`, `pkg`, `dmg`, `deb`, `rpm`,
  `script`) is delivered as bytes. `artifact_ref` names them and
  `artifact_sha256` authorises them.
* A **manager-sourced** package (`winget`, `choco`) is delivered by a package
  manager already on the endpoint. There are no bytes and no hash; what the
  agent needs is the identifier the manager knows the package by.

## Why `artifact_ref` is now an `https://` URL

`packages.artifact_ref` is an `s3://<bucket>/<key>` URI — a SERVER-SIDE
address. The agent cannot resolve one and must not be able to: an
object-store credential resident on every endpoint in the fleet is invariant
#3 inverted, and it would give one compromised endpoint read of every
tenant's artifacts. So the control plane resolves it instead, at dispatch
time, into a presigned `https://` URL good for one GET of one object for the
life of the job (`ObjectStore.presign_package_artifact`).

That URL travels inside the **signed** params — the Ed25519 signature the
agent verifies covers it exactly as it covers the hash — and the download is
still not the authorization: `stage` on the agent produces an executable path
only when the fetched bytes hash to the `artifact_sha256` in those same
signed params.

### The lifetime, and why it is exactly the job's

The URL is minted for `job_engine.default_ttl_s` seconds and the job is
created with that SAME `ttl_s`, so the two windows are one window by
construction rather than by two settings agreeing.

* **Shorter than the job would be an outage.** A job the agent may still
  legitimately execute would fail at the download with an HTTP 403 nobody can
  diagnose from the endpoint.
* **Longer than the job is a standing grant with nothing to spend it on.** An
  expired job is refused by the agent's freshness check, skipped by
  `queued_for_device`, and swept to `expired` — so every second of URL past
  `expires_at` is bearer credential and no capability.

A slow download is not a reason to widen it: SigV4 query expiry is evaluated
when the request is RECEIVED, not for the duration of the transfer, so a
download that starts one second before expiry completes however long it takes
(the agent caps it at 30 minutes of its own accord).

### Retries

`resign_for_retry` gives an attempt a whole fresh window, and the first retry
is at least `STUCK_DISPATCH_AFTER` (10 min) after dispatch. Re-signing the
original params would re-sign a URL scoped to a window that has moved, so
`refresh_deploy_params` — registered as the job type's `params_refresher` —
mints a new one against the new `expires_at`. Without it, attempts 2 and 3 of
`ITS_JOB_MAX_ATTEMPTS` would fail on an expired URL for the rest of the job's
life.
"""

import math
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.i18n import tr
from app.core.storage import ObjectStore
from app.models import Deployment, DeploymentResult, Job, Package
from app.modules.deploy.packages import DeployValidationError
from app.modules.deploy.schema import AppDeployParams

# THE PARTITION, AND IT IS THE SAME OBJECT THE AGENT PARTITIONS ON.
# `agent/internal/capabilities/deploy/install.go` splits the installer_type
# enum into `artifactBacked` and `managerSourced` and chooses its branch on
# THAT ALONE — never on whether an artifact happens to be present — so that a
# nulled-out `artifact_ref` cannot reach a path that skips the hash check. The
# server's half has to be the same split or the two sides disagree about what
# a job means; `tests/test_deploy_artifacts.py` asserts this partition against
# `packages.INSTALLER_TYPES` in both directions.
ARTIFACT_BACKED_INSTALLER_TYPES: frozenset[str] = frozenset(
    {"msi", "exe", "pkg", "dmg", "deb", "rpm", "script"}
)
MANAGER_SOURCED_INSTALLER_TYPES: frozenset[str] = frozenset({"winget", "choco"})


def is_artifact_backed(installer_type: str) -> bool:
    return installer_type in ARTIFACT_BACKED_INSTALLER_TYPES


def is_manager_sourced(installer_type: str) -> bool:
    return installer_type in MANAGER_SOURCED_INSTALLER_TYPES


class ArtifactUrlSigner(Protocol):
    """The one capability dispatch needs from object storage.

    A Protocol rather than `ObjectStore` itself so the dispatch path can be
    driven with a stub that records what it was asked to sign, and so nothing
    below can reach a second object-store method by accident.
    """

    def presign_package_artifact(
        self, org_id: UUID, package_id: UUID, *, ref: str, expires_in: int
    ) -> str: ...


class LazyObjectStoreSigner:
    """An `ArtifactUrlSigner` that does not open an object-store connection
    until something actually needs a URL.

    `ObjectStore.__init__` calls `head_bucket` (and creates the bucket if it
    is absent), so constructing one eagerly would make the deploy window sweep
    depend on object storage even on the nights when every deployment it finds
    is manager-sourced or already in the desired state. Held for the length of
    one sweep and then dropped.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._store: ObjectStore | None = None

    def _open(self) -> ObjectStore:
        if self._store is None:
            self._store = ObjectStore(self._settings or get_settings())
        return self._store

    def presign_package_artifact(
        self, org_id: UUID, package_id: UUID, *, ref: str, expires_in: int
    ) -> str:
        return self._open().presign_package_artifact(
            org_id, package_id, ref=ref, expires_in=expires_in
        )


def manager_package_identifier(package: Package) -> str | None:
    """The identifier `winget`/`choco` are asked to install, or None.

    `packages.package_identifier` is the explicit answer. When it is unset —
    every row cataloged before migration 0052 — this falls back to
    `detection_rule.name`, which is EXACTLY what the agent reads today when
    `AppDeployParams` carries no identifier at all
    (`install.go::packageIdentifier`). So a control plane that starts sending
    the field sends the same value the fleet was already deriving, and the
    upgrade changes nothing for a catalog nobody has revisited.

    Returns None rather than a guess when there is nothing to send: the agent
    refuses a manager-sourced deployment with no usable identifier, and a
    refusal naming the gap is worth more than an install of whatever a fuzzy
    search resolves to.
    """
    explicit = (package.package_identifier or "").strip()
    if explicit:
        return explicit
    fallback = package.detection_rule.get("name") if package.detection_rule else None
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return None


def build_deploy_params(
    *, action: str, package: Package, artifact_url: str | None
) -> AppDeployParams:
    """Assemble `AppDeployParams` for one package, and make the two halves of
    the partition unrepresentable together.

    An artifact-backed job carries `artifact_ref` + `artifact_sha256` and NO
    `package_identifier`; a manager-sourced job carries `package_identifier`
    and NO artifact. That is not a stylistic choice: the agent refuses either
    mismatch outright (`parseDeployParams`) and re-checks in `stage` rather
    than trusting its caller, so a server that emitted one would produce a
    fleet-wide refusal. Building both shapes in one function is what stops the
    two rules from being enforced in two places and drifting.
    """
    installer_type = package.installer_type
    if is_manager_sourced(installer_type):
        if artifact_url is not None:
            raise DeployValidationError(
                tr(
                    "{installer_type} is manager-sourced and must carry no artifact",
                    installer_type=repr(installer_type),
                )
            )
        return AppDeployParams(
            action=action,
            installer_type=installer_type,
            artifact_ref=None,
            artifact_sha256=None,
            package_identifier=manager_package_identifier(package),
            install_args=package.install_args,
            detection_rule=package.detection_rule,
            uninstall_rule=package.uninstall_rule,
            deferral_hours=24,
        )
    if not is_artifact_backed(installer_type):
        raise DeployValidationError(
            tr("unknown installer_type: {installer_type}", installer_type=repr(installer_type))
        )
    return AppDeployParams(
        action=action,
        installer_type=installer_type,
        artifact_ref=artifact_url,
        artifact_sha256=package.artifact_sha256,
        # NOT `manager_package_identifier(package)`. An artifact-backed
        # deployment installs a file, and the agent names it by installer type
        # and action rather than by anything job-supplied; sending an
        # identifier here would put a string nothing consumes inside the
        # signature and invite a future reader to consume it.
        package_identifier=None,
        install_args=package.install_args,
        detection_rule=package.detection_rule,
        uninstall_rule=package.uninstall_rule,
        deferral_hours=24,
    )


def artifact_url_for(
    signer: ArtifactUrlSigner, *, org_id: UUID, package: Package, expires_in: int
) -> str:
    """Presign this package's artifact, or refuse to dispatch.

    Fail-closed on every incomplete row: an artifact-backed package with no
    `artifact_ref` or no `artifact_sha256` has nothing the agent could verify,
    and dispatching it would spend a job to learn that.
    """
    if not package.artifact_ref:
        raise DeployValidationError(
            tr(
                "package {package_id} is {installer_type} but has no uploaded artifact",
                package_id=package.id,
                installer_type=repr(package.installer_type),
            )
        )
    if not package.artifact_sha256:
        raise DeployValidationError(
            tr(
                "package {package_id} has an artifact but no artifact_sha256 to verify it against",
                package_id=package.id,
            )
        )
    return signer.presign_package_artifact(
        org_id, package.id, ref=package.artifact_ref, expires_in=expires_in
    )


def redact_deploy_params(params: dict[str, Any]) -> dict[str, Any]:
    """`app.deploy` params as a `jobs.read` holder may see them.

    `artifact_ref` is a presigned URL: a bearer credential for the download of
    one installer until it expires. `jobs.read` is a reporting permission —
    weaker than `deploy.execute`, which is what authorized the dispatch — so
    it is replaced by a fixed marker rather than shortened, prefixed or
    fingerprinted. A "safe prefix" of a signed URL is still the bucket, the
    key and the tenant id.

    Registered as the job type's `params_redactor`, so `app/api/v1/jobs.py`
    applies it on every read path (`GET /jobs`, `GET /jobs/{id}`, and the
    `JobOut` returned by `POST /jobs`).
    """
    if not params.get("artifact_ref"):
        return params
    return {**params, "artifact_ref": REDACTED_ARTIFACT_REF}


REDACTED_ARTIFACT_REF = "[redacted: short-lived artifact download URL]"


async def package_for_job(session: AsyncSession, job: Job) -> Package | None:
    """The `Package` an `app.deploy` job was dispatched for, or None.

    Walks `job -> DeploymentResult -> Deployment -> Package`, and every one of
    those three hops carries an explicit `job.org_id` predicate (invariant #5).

    SEPARATE FROM `refresh_deploy_params` ON PURPOSE. Folded into the
    refresher, the only observable of a missing predicate is "the refresh
    produced no URL" — and `presign_package_artifact` ALSO produces no URL for
    a foreign package, because it derives the key from `org_id` rather than
    reading it out of the ref. The two guards would mask each other, and a
    cross-org test would pass against a query with no tenancy predicate at
    all. Exposed here so `tests/test_deploy_artifacts.py` can assert on the
    ROW this query returns, where nothing else is in the way.
    """
    org_id = job.org_id
    return (
        await session.execute(
            select(Package)
            .join(Deployment, Deployment.package_id == Package.id)
            .join(DeploymentResult, DeploymentResult.deployment_id == Deployment.id)
            .where(
                DeploymentResult.org_id == org_id,
                DeploymentResult.job_id == job.id,
                Deployment.org_id == org_id,
                Package.org_id == org_id,
            )
        )
    ).scalar_one_or_none()


async def refresh_deploy_params(session: AsyncSession, job: Job) -> dict[str, Any] | None:
    """Mint a fresh artifact URL for a retry (`ParamsRefresher`).

    Runs inside `resign_for_retry`, after the new `expires_at` is written and
    before the envelope is signed, so the URL is scoped to the window this
    attempt actually gets.

    NEVER RAISES FOR A BUSINESS REASON. It runs inside the org's job-expiry
    sweep, where an exception costs that org its whole cycle. A package that
    has been deleted, or whose artifact is gone, yields params with
    `artifact_ref: null`, which the agent refuses on arrival — a terminal
    state with a reason, rather than a sweep that fails forever.

    EVERY query carries an explicit `org_id` predicate (invariant #5): the job
    row names the org, and the deployment result, deployment and package are
    each re-fetched under it.
    """
    params = dict(job.params or {})
    installer_type = params.get("installer_type")
    if not isinstance(installer_type, str) or not is_artifact_backed(installer_type):
        return None  # manager-sourced: nothing in these params is time-bounded

    org_id = job.org_id
    row = await package_for_job(session, job)

    remaining = math.ceil((job.expires_at - datetime.now(UTC)).total_seconds())
    if row is None or remaining < 1:
        return {**params, "artifact_ref": None}
    try:
        params["artifact_ref"] = artifact_url_for(
            LazyObjectStoreSigner(), org_id=org_id, package=row, expires_in=remaining
        )
    except Exception:  # noqa: BLE001 - contained deliberately, see below
        # Object storage unreachable, a row pointing outside this org's
        # prefix, a TTL the store refuses: every one of them means "no fresh
        # grant". The job goes back out with no artifact and the agent refuses
        # it, which is terminal and legible. Swallowing here is what keeps one
        # broken package from wedging an org's entire expiry sweep.
        params["artifact_ref"] = None
    return params
