"""Signed `script.run` — script authoring, versioning, dispatch, and result
ingestion (docs/modules/app-deploy/spec.md).

Scripts are the org-scoped catalog of ad-hoc script bodies (`Script`); running
one against a device or group snapshots the script's `body` and `version`
into `ScriptRunParams`, the params of a signed `script.run` job (invariant
#1 — the body travels *inside* the Ed25519-signed envelope, so the agent
verifies the signature covering the body before executing it as SYSTEM/root;
no unsigned script-execution path exists). Mirrors `app.modules.deploy.
dispatch.dispatch_deployment` for the job build/sign/publish idiom and target
resolution, and `app.modules.deploy.packages.create_package` for the
author/audit shape.
"""

from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.wire import InstallOutcome
from app.models import Device, Job
from app.modules.deploy.models import Script, ScriptRun
from app.modules.deploy.packages import DeployValidationError
from app.modules.deploy.schema import ScriptRunParams, ScriptRunResult

OS_FAMILIES = {"windows", "linux", "macos"}
INTERPRETERS = {"powershell", "bash", "python"}

# `script_runs.state` domain — narrower than `InstallOutcome.state`, which
# also allows the deploy-only states "installed"/"not_applicable". Those have
# no meaning for a script run and must never be written verbatim (mirrors
# `app.modules.deploy.dispatch`'s out-of-domain coercion for deploy results).
_SCRIPT_OUTCOME_STATES = {"succeeded", "failed"}


class _JobEngineLike(Protocol):
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


async def author_script(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    os_family: str,
    interpreter: str,
    body: str,
) -> Script:
    """Create a new `Script` catalog row at `version=1`. Audits
    `script.authored` (`actor_type="user"`)."""
    if os_family not in OS_FAMILIES:
        raise DeployValidationError(tr("unknown os_family: {os_family}", os_family=repr(os_family)))
    if interpreter not in INTERPRETERS:
        raise DeployValidationError(
            tr("unknown interpreter: {interpreter}", interpreter=repr(interpreter))
        )

    script = Script(
        org_id=org_id,
        name=name,
        os_family=os_family,
        interpreter=interpreter,
        body=body,
        version=1,
        created_by=actor_id,
    )
    session.add(script)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="script.authored",
        target_type="script",
        target_id=str(script.id),
        metadata={"name": name, "os_family": os_family, "interpreter": interpreter},
    )

    return script


async def edit_script(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    script_id: UUID,
    body: str,
) -> Script:
    """Update `script_id`'s body in place, bumping `version` by 1. Bumping the
    version on the same row (rather than inserting a new row) is the simplest
    scheme given `UNIQUE(org_id, name, version)` — incrementing version keeps
    that constraint satisfied. Audits `script.edited` (`actor_type="user"`).
    """
    script = (
        await session.execute(
            select(Script).where(Script.org_id == org_id, Script.id == script_id)
        )
    ).scalar_one()

    script.version += 1
    script.body = body
    session.add(script)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="script.edited",
        target_type="script",
        target_id=str(script.id),
        metadata={"version": script.version},
    )

    return script


async def run_script(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    *,
    org_id: UUID,
    script_id: UUID,
    target_type: Literal["device", "group"],
    target_id: UUID,
    actor_id: UUID | None = None,
    actor_type: Literal["user", "agent", "system"] = "system",
) -> list[tuple[UUID, UUID, dict[str, Any]]]:
    """Resolve `target_id`'s device set (a single device, or every device in
    a group — resolved exactly like `dispatch_deployment`) and build one
    signed `script.run` job per device, snapshotting the script's `body` and
    `version` into `ScriptRunParams`. The body travels inside the signed
    envelope (invariant #1): the agent verifies the job's signature — which
    covers the params, including `body` — before executing it as SYSTEM/root.

    Does NOT publish the jobs to Redis — mirrors `dispatch_deployment`: a job
    published before the caller's tenant transaction commits could be handed
    to an agent with no durable server record if the commit later failed.
    Instead, returns the pending publishes as `(job_id, device_id, envelope)`
    triples; the CALLER must publish each one via
    `app.gateway.dispatch.publish_job` only AFTER its `org_session` block
    exits (i.e. after commit).

    The pending list is empty if the script doesn't exist in this org, or the
    target resolves to no devices. Records a single `script.dispatched` audit
    event after the loop, attributed to `actor_id`/`actor_type`.

    EVERY query (`Script`, `Device`) carries an explicit `org_id` predicate
    (invariant #5) — a script id or device/group id from another org must
    never be reachable through this path.
    """
    script = (
        await session.execute(
            select(Script).where(Script.org_id == org_id, Script.id == script_id)
        )
    ).scalar_one_or_none()
    if script is None:
        return []

    device_ids: list[UUID]
    if target_type == "device":
        found = (
            await session.execute(
                # NOT filtered by review state: a device the operator NAMED is
                # refused by the review gate (409 "waiting for review"), not
                # turned into a silent no-op that still audits a dispatch.
                select(Device.id).where(Device.org_id == org_id, Device.id == target_id)
            )
        ).scalar_one_or_none()
        device_ids = [found] if found is not None else []
    else:  # "group"
        device_ids = list(
            (
                await session.execute(
                    select(Device.id).where(
                        Device.org_id == org_id,
                        Device.device_group_id == target_id,
                        # A group run passes over members waiting for review
                        # rather than failing the whole run on the gate.
                        Device.review_state == "approved",
                    )
                )
            )
            .scalars()
            .all()
        )

    pending: list[tuple[UUID, UUID, dict[str, Any]]] = []
    for device_id in device_ids:
        pending.append(
            await dispatch_script_to_device(
                session,
                job_engine,
                org_id=org_id,
                script=script,
                device_id=device_id,
                target_type=target_type,
                target_id=target_id,
                actor_id=actor_id,
            )
        )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="script.dispatched",
        target_type="script",
        target_id=str(script_id),
        metadata={
            "dispatched_count": len(pending),
            "script_id": str(script_id),
            "script_version": script.version,
        },
    )

    await session.flush()
    return pending


async def dispatch_script_to_device(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    *,
    org_id: UUID,
    script: Script,
    device_id: UUID,
    target_type: str,
    target_id: UUID,
    actor_id: UUID | None,
    policy_id: UUID | None = None,
) -> tuple[UUID, UUID, dict[str, Any]]:
    """One signed `script.run` for one device, with its `ScriptRun` row. The
    body is snapshotted into the signed params (invariant #1). Not published."""
    params = ScriptRunParams(
        interpreter=script.interpreter,
        body=script.body,
        script_id=str(script.id),
        script_version=script.version,
        timeout_s=3600,
    )
    job = await job_engine.create(
        session,
        org_id=org_id,
        type="script.run",
        target_device_id=device_id,
        params=params.model_dump(),
        requested_by=actor_id,
    )
    envelope = await job_engine.signed_envelope(job)
    session.add(
        ScriptRun(
            org_id=org_id,
            script_id=script.id,
            script_version=script.version,
            target_type=target_type,
            target_id=target_id,
            device_id=device_id,
            job_id=job.id,
            state="running",
            requested_by=actor_id,
            policy_id=policy_id,
        )
    )
    return job.id, device_id, envelope


# What a run keeps of the script's output. The agent bounds it too; this is
# the database's own limit.
OUTPUT_CAP = 64 * 1024


async def ingest_script_result(
    session: AsyncSession, job: Job, result: ScriptRunResult
) -> None:
    """Reconcile a completed `script.run` job's result.

    A script run targets exactly one device, so `result.outcome` is applied
    directly to the `ScriptRun` row `run_script` created for this job. A
    report for a job with no matching `ScriptRun` (e.g. a stale/duplicate
    report) is skipped gracefully rather than raising — mirrors `app.modules.
    deploy.dispatch.ingest_deploy_result`.

    EVERY query carries an explicit `org_id` predicate (invariant #5).
    """
    org_id = job.org_id
    now = datetime.now(UTC)

    script_run = (
        await session.execute(
            select(ScriptRun).where(ScriptRun.org_id == org_id, ScriptRun.job_id == job.id)
        )
    ).scalar_one_or_none()
    if script_run is None:
        return  # no matching row; the agent's report must never fail the job

    outcome: InstallOutcome = result.outcome
    audit_metadata: dict[str, Any] = {"device_id": str(job.target_id)}

    if outcome.state in _SCRIPT_OUTCOME_STATES:
        script_run.state = outcome.state
    else:
        # Out-of-domain state (deploy's vocabulary, e.g. "installed"): coerce
        # to "failed" so the row always reaches a script-valid terminal state.
        # Record the original value so the operator can see why.
        script_run.state = "failed"
        audit_metadata["unexpected_outcome_state"] = outcome.state

    script_run.exit_code = outcome.exit_code
    script_run.log_ref = outcome.log_ref
    script_run.output = outcome.log[:OUTPUT_CAP] if outcome.log else None
    script_run.finished_at = now
    session.add(script_run)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="script.result",
        target_type="device",
        target_id=str(job.target_id),
        metadata={**audit_metadata, "state": script_run.state},
    )

    await session.flush()

    if script_run.policy_id is not None:
        from app.modules.deploy.script_policies import after_policy_run

        await after_policy_run(session, script_run, now)
