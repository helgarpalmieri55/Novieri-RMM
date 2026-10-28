"""Signed `app.deploy` dispatch (docs/modules/app-deploy/spec.md).

Mirrors `app.modules.patch.apply.dispatch_apply`: this is the module's
application-security gate for *deployments* — the only path by which an
install/update/uninstall of a cataloged `Package` becomes a device job. Every
device job is built via the job engine, signed, and published; no unsigned
path exists (invariant #1).

Unlike patch's per-org sweep, a deployment dispatch is scoped to one
`Deployment` row (`deployment_id`) resolved to its target device set (a
single device, or every device in a group) at call time. Before building any
job, the server asks `device_in_desired_state` (see `app.modules.deploy.
detect`) whether the device already reflects the deployment's `action` per
the package's detection rule; a device already there gets a `not_applicable`
`DeploymentResult` and no job, rather than a redundant install/uninstall.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.schedule import in_window
from app.models import Deployment, DeploymentResult, Device, Job, Package
from app.modules.change_mgmt.gates import (
    deployment_gate,
    has_authorizing_change,
    record_gate_refusal,
)
from app.modules.deploy.artifacts import (
    ArtifactUrlSigner,
    LazyObjectStoreSigner,
    artifact_url_for,
    build_deploy_params,
    is_artifact_backed,
)
from app.modules.deploy.detect import device_in_desired_state
from app.modules.deploy.schema import AppDeployResult

# NAMES THE DEPLOYMENT, NEVER THE PARAMS. `artifact_ref` is a presigned URL and
# therefore a bearer credential; a log line built from the params dict is the
# ordinary way one ends up in a log aggregator with a 90-day retention, long
# outliving the grant's 900 seconds. What is logged here is what an operator
# needs to correlate a dispatch — ids and the installer type — and nothing that
# would let a reader of the log perform the download.
logger = logging.getLogger("its.deploy.dispatch")


class _JobEngineLike(Protocol):
    @property
    def default_ttl_s(self) -> int: ...

    async def create(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        type: str,
        target_device_id: UUID,
        params: dict[str, Any],
        requested_by: UUID | None = None,
        ttl_s: int | None = None,
    ) -> Any: ...

    async def signed_envelope(self, job: Any) -> dict[str, Any]: ...


async def deployment_refusal(
    session: AsyncSession, org_id: UUID, deployment: Deployment, *, actor_id: UUID | None
) -> str | None:
    """Why a change gate stops this deployment, or None. The gates are the
    deployment's own client's (`client_id`, its target's: migration 0239). A
    refusal is audited `change.gate_refused`, with that client."""
    client_id = deployment.client_id
    reason = await deployment_gate(
        session, org_id, deployment.target_type, deployment.target_id, client_id=client_id
    )
    if reason is None or await has_authorizing_change(
        session, org_id, "deployment", deployment.id
    ):
        return None
    await record_gate_refusal(
        session,
        org_id=org_id,
        actor_id=actor_id,
        target_type="deployment",
        target_id=str(deployment.id),
        reason=reason,
        client_id=client_id,
    )
    return reason


async def dispatch_deployment(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    *,
    org_id: UUID,
    deployment_id: UUID,
    now: datetime,
    actor_id: UUID | None = None,
    actor_type: Literal["user", "agent", "system"] = "system",
    artifact_urls: ArtifactUrlSigner | None = None,
) -> list[tuple[UUID, UUID, dict[str, Any]]]:
    """Resolve `deployment_id`'s target device set and build one signed
    `app.deploy` job per device that isn't already in the desired state.

    Does NOT publish the jobs to Redis — publishing a signed job before the
    caller's tenant transaction commits would hand an agent a job with no
    durable server record if the commit later failed (an unaudited
    privileged fleet execution). Instead, returns the pending publishes as
    `(job_id, device_id, envelope)` triples; the CALLER must publish each one
    via `app.gateway.dispatch.publish_job` only AFTER its `org_session` block
    exits (i.e. after commit) — mirrors `app.modules.patch.api`'s `POST
    /scan` idiom.

    The pending list is empty if the deployment doesn't exist in this org,
    its maintenance window is closed, or every target device is already in
    the desired state. `actor_id`/`actor_type` attribute the single
    `deploy.dispatched` audit event recorded after the loop — a
    human-initiated dispatch passes `actor_id=<user>, actor_type="user"`; the
    worker's window sweep passes `actor_type="system"` (the default).

    An artifact-backed package's `artifact_ref` is resolved ONCE per dispatch
    into a presigned `https://` URL (`app.modules.deploy.artifacts`) — the
    agent cannot fetch an `s3://` URI and must hold no object-store
    credential (invariant #3). The URL's lifetime is `job_engine.
    default_ttl_s`, and the SAME value is passed as the job's `ttl_s`, so the
    grant and the job it authorises expire together by construction. It goes
    into the SIGNED params, so it is covered by the job signature; it is not
    written to the audit metadata, not logged, and redacted out of every API
    read path. `artifact_urls` is the signer; `None` opens one lazily, so a
    sweep that never dispatches an artifact-backed package never opens an
    object-store connection.

    EVERY query (`Deployment`, `Package`, `Device`) carries an explicit
    `org_id` predicate (invariant #5) — a deployment id, package id, or
    device id from another org must never be reachable through this path.
    THE PRESIGNED URL IS SUBJECT TO THE SAME RULE AND DOES NOT TRUST THIS
    FUNCTION FOR IT: `presign_package_artifact` DERIVES the object key from
    `org_id` and `package.id` and only checks the stored ref against it, so a
    `packages` row somehow naming another tenant's object yields no URL
    rather than a signed grant over it.

    And the devices are the DEPLOYMENT'S CLIENT'S (MSP clients plan
    2026-10-26 Task 9). The window sweep runs this under scope all, where RLS
    hides no client, so a device of another client -- the one named, or a
    member of the group -- never gets this deployment's job. The database
    already keeps a deployment and its target, and a group and its members,
    in one client; this predicate does not lean on that.
    """
    deployment = (
        await session.execute(
            select(Deployment).where(
                Deployment.org_id == org_id, Deployment.id == deployment_id
            )
        )
    ).scalar_one_or_none()
    if deployment is None:
        return []
    # Read now: the state update below expires it (written by trigger).
    client_id = deployment.client_id

    if deployment.window_cron is not None and not in_window(deployment.window_cron, now):
        return []

    if await deployment_refusal(session, org_id, deployment, actor_id=actor_id) is not None:
        return []

    package = (
        await session.execute(
            select(Package).where(
                Package.id == deployment.package_id, Package.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if package is None:
        return []

    device_ids: list[UUID]
    if deployment.target_type == "device":
        found = (
            await session.execute(
                # NOT filtered by review state: a single named target waiting
                # for review is refused at creation (`api.create_deployment`)
                # and by the review gate here, never silently skipped.
                select(Device.id).where(
                    Device.org_id == org_id,
                    Device.client_id == client_id,
                    Device.id == deployment.target_id,
                )
            )
        ).scalar_one_or_none()
        device_ids = [found] if found is not None else []
    else:  # "group"
        device_ids = list(
            (
                await session.execute(
                    select(Device.id).where(
                        Device.org_id == org_id,
                        Device.client_id == client_id,
                        Device.device_group_id == deployment.target_id,
                        # A group deployment passes over members waiting for
                        # review rather than failing on the gate.
                        Device.review_state == "approved",
                    )
                )
            )
            .scalars()
            .all()
        )

    # One grant per DISPATCH, not per device: the artifact is the same object
    # for every device in the group, and minting N identical-scope URLs would
    # be N bearer credentials to leak instead of one. The lifetime is the job
    # TTL, and the same value is handed to `create` below.
    ttl_s = job_engine.default_ttl_s
    artifact_url: str | None = None
    if device_ids and is_artifact_backed(package.installer_type):
        artifact_url = artifact_url_for(
            artifact_urls or LazyObjectStoreSigner(),
            org_id=org_id,
            package=package,
            expires_in=ttl_s,
        )

    pending: list[tuple[UUID, UUID, dict[str, Any]]] = []
    for device_id in device_ids:
        already_done = await device_in_desired_state(
            session,
            org_id=org_id,
            device_id=device_id,
            action=deployment.action,
            detection_rule=package.detection_rule,
        )
        if already_done:
            session.add(
                DeploymentResult(
                    org_id=org_id,
                    deployment_id=deployment_id,
                    device_id=device_id,
                    state="not_applicable",
                )
            )
            continue

        params = build_deploy_params(
            action=deployment.action, package=package, artifact_url=artifact_url
        )
        job = await job_engine.create(
            session,
            org_id=org_id,
            type="app.deploy",
            target_device_id=device_id,
            params=params.model_dump(),
            requested_by=actor_id,
            ttl_s=ttl_s,
        )
        envelope = await job_engine.signed_envelope(job)
        pending.append((job.id, device_id, envelope))
        logger.info(
            "app.deploy dispatched: deployment=%s device=%s package=%s installer_type=%s job=%s",
            deployment_id,
            device_id,
            package.id,
            package.installer_type,
            job.id,
        )

        session.add(
            DeploymentResult(
                org_id=org_id,
                deployment_id=deployment_id,
                device_id=device_id,
                job_id=job.id,
                state="running",
            )
        )

    deployment.state = "dispatching"

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="deploy.dispatched",
        target_type="deployment",
        target_id=str(deployment_id),
        metadata={"dispatched_count": len(pending), "deployment_id": str(deployment_id)},
        client_id=client_id,
    )

    await session.flush()
    return pending


_TERMINAL_STATES = {"installed", "failed", "not_applicable"}

# `deployment_results.state` domain (see docs/modules/app-deploy/spec.md). Note
# this is narrower than `InstallOutcome.state` (app/core/wire.py), which also
# allows "succeeded" for script.run's vocabulary — a value that has no meaning
# for a deployment result and must never be written to this row verbatim (see
# Finding 1 below).
_DEPLOY_OUTCOME_STATES = {"installed", "failed", "not_applicable"}


async def ingest_deploy_result(session: AsyncSession, job: Job, result: AppDeployResult) -> None:
    """Reconcile a completed `app.deploy` job's result (docs/modules/app-deploy/
    spec.md).

    A deploy job targets exactly one device and one package, so `result.
    outcomes` normally carries a single `InstallOutcome`; the first (only)
    outcome is applied to the `DeploymentResult` row dispatch created for this
    job. An agent report for a job with no matching `DeploymentResult` (e.g.
    the row was never created, or this is a stale/duplicate report) is
    skipped gracefully rather than raising — the agent's report must never be
    able to fail the job over server-side bookkeeping.

    After updating the device's row, rolls the parent `Deployment.state` to
    `"complete"` (or `"failed"` if any device's result is `failed`) once every
    `DeploymentResult` for that deployment is terminal. Finishes with a single
    `deploy.result` audit event, system-attributed since this runs in the
    kernel's result handler, not on behalf of any user.

    EVERY query carries an explicit `org_id` predicate (invariant #5).
    """
    org_id = job.org_id
    device_id = job.target_id
    now = datetime.now(UTC)

    deployment_result = (
        await session.execute(
            select(DeploymentResult).where(
                DeploymentResult.org_id == org_id,
                DeploymentResult.job_id == job.id,
            )
        )
    ).scalar_one_or_none()
    if deployment_result is None:
        return  # no matching row; the agent's report must never fail the job
    # Its device's, which dispatch holds to the deployment's. Read now: the
    # updates below expire it (written by trigger).
    client_id = deployment_result.client_id

    audit_metadata: dict[str, Any] = {"device_id": str(device_id)}

    if not result.outcomes:
        # Schema-allowed but protocol-violating: an agent report with no
        # outcomes at all. Land on a terminal "failed" state (never wedge the
        # rollup) and surface why, rather than raising IndexError.
        deployment_result.state = "failed"
        deployment_result.exit_code = None
        deployment_result.log_ref = "agent returned no outcomes"
        deployment_result.detected_after = None
        deployment_result.finished_at = now
        audit_metadata["note"] = "agent returned no outcomes"
    else:
        outcome = result.outcomes[0]
        if outcome.state in _DEPLOY_OUTCOME_STATES:
            deployment_result.state = outcome.state
        else:
            # Out-of-domain state (e.g. "succeeded", script.run's vocabulary):
            # coerce to "failed" so the row always reaches a deploy-valid
            # terminal state and the rollup never wedges. Record the original
            # value so the operator can see why.
            deployment_result.state = "failed"
            audit_metadata["unexpected_outcome_state"] = outcome.state
        deployment_result.exit_code = outcome.exit_code
        deployment_result.log_ref = outcome.log_ref
        deployment_result.detected_after = outcome.detected_after
        deployment_result.finished_at = now

    deployment = (
        await session.execute(
            select(Deployment).where(
                Deployment.org_id == org_id,
                Deployment.id == deployment_result.deployment_id,
            )
        )
    ).scalar_one_or_none()
    if deployment is not None:
        sibling_results = (
            (
                await session.execute(
                    select(DeploymentResult).where(
                        DeploymentResult.org_id == org_id,
                        DeploymentResult.deployment_id == deployment.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if all(r.state in _TERMINAL_STATES for r in sibling_results):
            deployment.state = (
                "failed"
                if any(r.state == "failed" for r in sibling_results)
                else "complete"
            )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="deploy.result",
        target_type="device",
        target_id=str(device_id),
        metadata={**audit_metadata, "state": deployment_result.state},
        client_id=client_id,
    )

    await session.flush()
