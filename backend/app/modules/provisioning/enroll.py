"""Enrollment correlation + signed `provision.enroll` post-config dispatch
(docs/modules/provisioning/spec.md §7/§9).

This module is the reverse edge between provisioning and CORE enrollment
(`app.services.enrollment`): when the freshly-imaged machine boots and calls
the real `/enroll/v1` path with the single-use token `start_provision_run`
issued, `bind_provisioned_device` correlates that consumed token back to its
`ProvisionRun` and advances it from `awaiting_enrollment` to `enrolled`. For
an ordinary (non-provisioning) enrollment token, no matching run exists and
this is a strict no-op — `enrollment.py` calls it unconditionally, guarded
only by the org-scoped lookup finding nothing.

`dispatch_provision_enroll` then builds and signs the `provision.enroll` job
that tells the just-enrolled agent to apply its post-config (hostname,
domain join, etc.) and verify health. Per invariant #1, the job is created
and signed exclusively via `job_engine.create` + `job_engine.signed_envelope`
— no unsigned path exists. Per invariant #3, the post_config placed in the
job params has the sealed credential fields (`domain_join.password`,
`local_admin.password` — `app.modules.provisioning.repository._CREDENTIAL_PATHS`)
stripped out entirely: no plaintext credential and no `{"__sealed__": ...}`
reference ever rides in this job or in its audit metadata. The credentials
already reached the device via the answer file at run-start (Task 6); the
post-config verify job has no need for them.

Mirrors `app.modules.backup.dispatch.dispatch_backup`'s dispatch shape: does
NOT publish to Redis. Returns the pending publish as a single-element list
of `(job_id, device_id, envelope)` triples (empty list if the run isn't in
the expected `enrolled` state, fail-closed) for the caller to publish only
after its own tenant transaction commits.
"""

import copy
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.models import Job, ProvisionRun
from app.modules.provisioning.repository import _CREDENTIAL_PATHS, get_image, get_profile
from app.modules.provisioning.schema import ProvisionEnrollParams, ProvisionEnrollResult


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


async def bind_provisioned_device(
    session: AsyncSession,
    *,
    org_id: UUID,
    enrollment_token_id: UUID,
    device_id: UUID,
    now: datetime,  # noqa: ARG001 - accepted for interface/audit symmetry only; no
    # ProvisionRun column records an enrollment timestamp, so `now` is never read.
) -> ProvisionRun | None:
    """Correlate a just-consumed enrollment token back to its `ProvisionRun`,
    if one exists, and advance it `awaiting_enrollment -> enrolled`.

    Org-scoped-finds the run in state `awaiting_enrollment` whose
    `enrollment_token_id` matches. If none exists — an ordinary agent
    enrollment, not a provisioning one — returns `None` and touches nothing:
    no state change, no audit event. This is what makes the caller's hook in
    `app.services.enrollment.enroll` a strict no-op for normal enrollments.

    EVERY query carries an explicit `org_id` predicate (invariant #5) — a
    token id from another org can never bind that org's run.
    """
    run = (
        await session.execute(
            select(ProvisionRun).where(
                ProvisionRun.org_id == org_id,
                ProvisionRun.enrollment_token_id == enrollment_token_id,
                ProvisionRun.state == "awaiting_enrollment",
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return None

    run.device_id = device_id
    run.state = "enrolled"
    session.add(run)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="provisioning.enrolled",
        target_type="device",
        target_id=str(device_id),
        metadata={"provision_run_id": str(run.id), "device_id": str(device_id)},
    )

    await session.flush()
    return run


def _non_secret_post_config(post_config: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy `post_config` with every sealed credential field
    (`_CREDENTIAL_PATHS`) removed entirely, so no credential — plaintext or a
    `{"__sealed__": ...}` reference — ever reaches `ProvisionEnrollParams`."""
    stripped = copy.deepcopy(post_config) if isinstance(post_config, dict) else {}
    for outer_key, inner_key in _CREDENTIAL_PATHS:
        outer = stripped.get(outer_key)
        if isinstance(outer, dict) and inner_key in outer:
            del outer[inner_key]
    return stripped


async def dispatch_provision_enroll(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    *,
    org_id: UUID,
    run_id: UUID,
    now: datetime,  # noqa: ARG001 - accepted for interface symmetry only (mirrors
    # dispatch_backup); nothing here reads wall-clock time.
    actor_id: UUID | None = None,
    actor_type: Literal["user", "agent", "system"] = "system",
) -> list[tuple[UUID, UUID, dict[str, Any]]]:
    """Build and dispatch the signed `provision.enroll` job for `run_id`'s
    freshly-enrolled device.

    Org-scoped-loads the run (must be `enrolled` with a non-null `device_id`;
    otherwise fail-closed — returns `[]` rather than dispatching against a
    run that never bound a device) plus its profile and image. Builds
    `ProvisionEnrollParams` with a NON-secret `post_config` (sealed credential
    fields stripped, see `_non_secret_post_config`), creates the job via
    `job_engine.create` + `job_engine.signed_envelope` (invariant #1 — no
    unsigned path), records `run.job_id`, and audits
    `provisioning.enroll_dispatched` with no credential/sealed-ref material
    in the metadata (invariant #3).

    Does NOT publish the job — mirrors `dispatch_backup`: publishing before
    the caller's transaction commits would hand the agent a job with no
    durable server record if the commit later failed. Returns the pending
    publish as a single-element list of `(job_id, device_id, envelope)`
    triples for the caller to publish after commit (Task 10).

    EVERY query (`ProvisionRun`, profile, image) carries an explicit `org_id`
    predicate (invariant #5).
    """
    run = (
        await session.execute(
            select(ProvisionRun).where(
                ProvisionRun.org_id == org_id,
                ProvisionRun.id == run_id,
                ProvisionRun.state == "enrolled",
            )
        )
    ).scalar_one_or_none()
    if run is None or run.device_id is None:
        return []

    profile = await get_profile(session, org_id, run.profile_id)
    if profile is None:
        return []
    image = await get_image(session, org_id, profile.image_id)
    if image is None:
        return []

    post_config = profile.post_config if isinstance(profile.post_config, dict) else {}
    hostname = post_config.get("hostname") or ""

    params = ProvisionEnrollParams(
        provision_run_id=str(run.id),
        os_family=image.os_family,
        hostname=hostname,
        post_config=_non_secret_post_config(post_config),
        expected_agent_version=None,
    )

    job = await job_engine.create(
        session,
        org_id=org_id,
        type="provision.enroll",
        target_device_id=run.device_id,
        params=params.model_dump(),
        requested_by=actor_id,
    )
    envelope = await job_engine.signed_envelope(job)

    run.job_id = job.id
    session.add(run)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="provisioning.enroll_dispatched",
        target_type="device",
        target_id=str(run.device_id),
        metadata={
            "provision_run_id": str(run.id),
            "device_id": str(run.device_id),
            "job_id": str(job.id),
        },
    )

    await session.flush()
    return [(job.id, run.device_id, envelope)]


# `provision_run.state` domain a completed `provision.enroll` job may land on
# (docs/modules/provisioning/spec.md). `ProvisionEnrollResult.state` is itself
# typed `Literal["success", "failed"]`, so in the normal (validated) path only
# those two values ever reach this function; the explicit membership check
# below is defense in depth against a value that bypassed the wire model's
# validation (e.g. a handler that skips `ProvisionEnrollResult(**result)`),
# mirroring `ingest_backup_result`'s out-of-domain coercion: an unrecognized
# state must still land on a terminal `failed` state -- never raise, never
# wedge the run -- with the original value recorded in the audit log so the
# operator can see why.
_VALID_PROVISION_RESULT_STATES = {"success", "failed"}


async def ingest_provision_result(
    session: AsyncSession, job: Job, result: ProvisionEnrollResult
) -> None:
    """Reconcile a completed `provision.enroll` job's result (docs/modules/
    provisioning/spec.md).

    Finds the `ProvisionRun` row `dispatch_provision_enroll` created for this
    job. An agent report for a job with no matching row (e.g. the row was
    never created, or this is a stale/duplicate report) is skipped
    gracefully rather than raising -- the agent's report must never be able
    to fail the job over server-side bookkeeping.

    `result.state == "success"` flips the run to `complete`. Any other case
    -- `"failed"`, or an out-of-domain value that bypassed wire validation --
    lands the run on `failed` with an `error` message; when the state isn't
    one of `_VALID_PROVISION_RESULT_STATES`, the original value is also
    recorded in the audit metadata. A malformed/incomplete agent report must
    never crash or wedge this handler; it must always resolve to a terminal
    state.

    Finishes with a single `provisioning.result` audit event, system-
    attributed since this runs in the kernel's result handler, not on behalf
    of any user. No secret data is ever written to the audit metadata --
    only booleans/ids.

    EVERY query carries an explicit `org_id` predicate (invariant #5).
    """
    org_id = job.org_id
    now = datetime.now(UTC)

    run = (
        await session.execute(
            select(ProvisionRun).where(
                ProvisionRun.org_id == org_id,
                ProvisionRun.job_id == job.id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return  # no matching row; the agent's report must never fail the job

    audit_metadata: dict[str, Any] = {}

    if result.state == "success":
        run.state = "complete"
        run.finished_at = now
    else:
        run.state = "failed"
        run.error = result.error or f"unexpected result state {result.state!r}"
        run.finished_at = now
        if result.state not in _VALID_PROVISION_RESULT_STATES:
            audit_metadata["unexpected_result_state"] = result.state

    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="provisioning.result",
        target_type="device",
        target_id=str(job.target_id),
        metadata={
            **audit_metadata,
            "provision_run_id": str(run.id),
            "state": run.state,
            "hostname_set": result.hostname_set,
            "domain_joined": result.domain_joined,
            "agent_healthy": result.agent_healthy,
        },
    )

    await session.flush()
