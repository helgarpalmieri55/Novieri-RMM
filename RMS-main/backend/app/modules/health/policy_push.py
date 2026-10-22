"""Building and dispatching the signed policy job
(`docs/modules/endpoint-health-alerting/spec.md` §5.2, §0.4, §2.11).

THREE OUTCOMES, AND THEY ARE THREE DIFFERENT THINGS. `push_policy` is the one
place this distinction lives, and collapsing any two of them is a real failure:

  1. a profile resolves -> push its checks. The list may legitimately be EMPTY
     (every check filtered out by OS), and that is still a policy: a Linux
     device under a Windows-only profile must be told to run nothing.
  2. nothing resolves AND the device is still running a policy -> push
     `checks=[]` at `DISARM_POLICY_VERSION`. §5.2: "A disarm must be an explicit
     instruction, not the absence of one." Skipping this push leaves the device
     evaluating its last policy FOREVER, and reporting breaches for thresholds
     nobody can see in the console.
  3. nothing resolves AND the device was never armed -> `None`, no job. There is
     nothing to disarm, and pushing anything would be inventing a policy for a
     machine nobody configured. §0.4's arithmetic (5,000 devices x a 5-minute
     tick = 1.4M job rows a day) is the reason a push per sweep per device is
     not acceptable.

`push_policy` DOES NOT PUBLISH. It returns `(job_id, envelope)` and the CALLER
publishes via `app.gateway.dispatch.publish_job` only after its `org_session`
block exits. `app/modules/deploy/scripts.py::run_script` states the reason in
its own docstring: a job published before the caller's tenant transaction
commits could be handed to an agent with no durable server record if the commit
later failed.

THE "NEVER LOWER" RULE ON THE ACK IS SCOPED TO ONE PROFILE. §5.2 says a result
that arrives after the next summary must be a no-op rather than a regression, so
a lower `policy_version` for the SAME profile never wins. A different
`profile_id` -- including NULL, which §2.11 defines as "acknowledged a disarm"
-- is a different policy rather than an older copy of the same one, and always
wins. Without that scoping a device disarmed after running a version-9 profile
reads forever as still on version 9, and §12 never re-pushes it.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, Job, JobStatus
from app.modules.health.jobtypes import (
    CHECK_NOW_JOB_TYPE,
    POLICY_APPLY_JOB_TYPE,
    HealthCheckSpec,
    HealthPolicyApplyParams,
)
from app.modules.health.models import DeviceHealthPolicyAck
from app.modules.health.registry import CHECK_TYPES, render_threshold
from app.modules.health.resolution import ResolvedProfile, resolve_profile_for_device

__all__ = [
    "DISARM_INTERVALS",
    "DISARM_POLICY_VERSION",
    "build_disarm_params",
    "build_policy_params",
    "push_check_now",
    "push_policy",
    "record_policy_ack",
    "record_policy_ack_from_summary",
]

logger = logging.getLogger("its.health.policy_push")

# §2.11: "A missing row reads as `policy_version = 0`, i.e. never
# acknowledged." A disarm is therefore version 0 with `profile_id = NULL`, which
# is the same pair a device that has never been armed presents -- deliberately,
# because "running nothing" and "never told to run anything" are the same
# INSTALLED state, and it is the presence of the ack ROW that distinguishes
# "told" from "never told".
DISARM_POLICY_VERSION = 0

# A disarmed agent has no checks, so these govern nothing except the timer the
# agent parks. They are the documented defaults rather than a previous
# profile's, because the previous profile is exactly what is being withdrawn.
DISARM_INTERVALS = {
    "tick_interval_s": 300,
    "posture_interval_s": 3600,
    "summary_interval_s": 1800,
}


class _JobEngineLike(Protocol):
    async def create(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        type: str,
        target_device_id: UUID,
        params: dict[str, Any],
        requested_by: UUID | None = ...,
        ttl_s: int | None = ...,
    ) -> Job: ...

    async def signed_envelope(self, job: Job) -> dict[str, Any]: ...


def build_policy_params(resolved: ResolvedProfile) -> HealthPolicyApplyParams:
    """The device's resolved check set as `health.policy.apply` params.

    Driven by `resolved.agent_checks`, which already excludes every
    server-evaluated type (§0.6) and every check this OS cannot answer (§3).
    `cadence` and the threshold rendering both come from the REGISTRY: the row
    has no cadence column, and only the registry knows that an `enum` check's
    `threshold_bool` means the member name the agent will report.
    """
    checks = [
        HealthCheckSpec(
            check_id=str(check.id),
            check_type=check.check_type,
            selector=check.selector,
            comparator=check.comparator,
            threshold=render_threshold(
                CHECK_TYPES[check.check_type],
                threshold_num=check.threshold_num,
                threshold_bool=check.threshold_bool,
            ),
            for_ticks=check.for_ticks,
            cadence=CHECK_TYPES[check.check_type].cadence,
        )
        for check in resolved.agent_checks
    ]
    return HealthPolicyApplyParams(
        policy_version=resolved.profile.policy_version,
        tick_interval_s=resolved.profile.tick_interval_s,
        posture_interval_s=resolved.profile.posture_interval_s,
        summary_interval_s=resolved.profile.summary_interval_s,
        checks=checks,
    )


def build_disarm_params() -> HealthPolicyApplyParams:
    """"Run nothing", as an instruction rather than as silence (§5.2)."""
    return HealthPolicyApplyParams(
        policy_version=DISARM_POLICY_VERSION, checks=[], **DISARM_INTERVALS
    )


async def _current_ack(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> DeviceHealthPolicyAck | None:
    return (
        await session.execute(
            select(DeviceHealthPolicyAck).where(
                DeviceHealthPolicyAck.org_id == org_id,
                DeviceHealthPolicyAck.device_id == device_id,
            )
        )
    ).scalar_one_or_none()


_WAITING = (JobStatus.queued, JobStatus.dispatched, JobStatus.running)


async def _identical_push_waiting(
    session: AsyncSession, org_id: UUID, device_id: UUID, params: dict[str, Any]
) -> bool:
    waiting = (
        await session.execute(
            select(Job.params).where(
                Job.org_id == org_id,
                Job.target_id == device_id,
                Job.type == POLICY_APPLY_JOB_TYPE,
                Job.status.in_(_WAITING),
            )
        )
    ).scalars()
    return any(existing == params for existing in waiting)


async def push_policy(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    org_id: UUID,
    device: Device,
    *,
    requested_by: UUID | None = None,
    unless_waiting: bool = False,
) -> tuple[UUID, dict[str, Any]] | None:
    """Create (but do NOT publish) this device's `health.policy.apply` job.

    Returns `(job_id, envelope)` for the caller to publish after commit, or
    `None` when there is nothing to say -- see the module docstring's three
    outcomes.

    `unless_waiting` also returns `None` when a job with exactly these params
    is still waiting for this device (queued, dispatched or running). The
    reconcile passes it: a device that has not answered yet would otherwise
    collect an identical job every pass, each expiring unanswered (operator
    report, 2026-09-24). A waiting job with DIFFERENT params does not count --
    it would install a policy that is already out of date.
    """
    if device.org_id != org_id:
        logger.error(
            "health: refusing to push policy for device %s (org %s) as org %s",
            device.id,
            device.org_id,
            org_id,
        )
        return None

    resolved = await resolve_profile_for_device(session, org_id, device)
    if resolved is not None:
        params = build_policy_params(resolved)
    else:
        ack = await _current_ack(session, org_id, device.id)
        if ack is None or (
            ack.profile_id is None and ack.policy_version == DISARM_POLICY_VERSION
        ):
            # Never armed, or already disarmed and acknowledged. Nothing to say.
            return None
        logger.info(
            "health: device %s lost its effective profile; pushing an explicit disarm",
            device.id,
        )
        params = build_disarm_params()

    if unless_waiting and await _identical_push_waiting(
        session, org_id, device.id, params.model_dump()
    ):
        return None

    job = await job_engine.create(
        session,
        org_id=org_id,
        type=POLICY_APPLY_JOB_TYPE,
        target_device_id=device.id,
        params=params.model_dump(),
        requested_by=requested_by,
    )
    return job.id, await job_engine.signed_envelope(job)


async def push_check_now(
    session: AsyncSession,
    job_engine: _JobEngineLike,
    org_id: UUID,
    device: Device,
    *,
    requested_by: UUID | None = None,
) -> tuple[UUID, dict[str, Any]]:
    """Create (but do NOT publish) a `health.check.now` job (§5.3).

    Unconditional: unlike the policy push there is no state to compare against,
    and "sample now" is meaningful even for a device running an empty policy --
    the summary it produces is what proves the policy is installed.
    """
    job = await job_engine.create(
        session,
        org_id=org_id,
        type=CHECK_NOW_JOB_TYPE,
        target_device_id=device.id,
        params={},
        requested_by=requested_by,
    )
    return job.id, await job_engine.signed_envelope(job)


async def record_policy_ack(session: AsyncSession, job: Job, result: dict[str, Any]) -> None:
    """`health.policy.apply`'s result handler: stamp `device_health_policy_ack`.

    Runs inside the job's tenant transaction (`JobEngine.complete`), and A RAISE
    HERE FAILS THE JOB. A device deleted between dispatch and result must not
    turn an acknowledged policy into a failed one, so an unresolvable device is
    logged and ignored rather than raised.

    The upsert is keyed on the `device_id` primary key -- one row per device,
    never a history (§2.11). See the module docstring for why "never lower" is
    scoped to one profile.
    """
    org_id = job.org_id
    device_id = job.target_id
    device = (
        await session.execute(
            select(Device.id).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    if device is None:
        logger.warning(
            "health: policy ack for job %s names device %s, which does not resolve in "
            "org %s; ignoring",
            job.id,
            device_id,
            org_id,
        )
        return

    params = job.params or {}
    acknowledged_version = int(params.get("policy_version", DISARM_POLICY_VERSION))
    profile_id = await _profile_id_for_version(session, org_id, device_id, acknowledged_version)
    await _upsert_ack(
        session,
        org_id,
        device_id,
        profile_id=profile_id,
        policy_version=acknowledged_version,
        now=datetime.now(UTC),
    )


async def record_policy_ack_from_summary(
    session: AsyncSession,
    org_id: UUID,
    device: Device,
    *,
    policy_version: int,
    now: datetime,
) -> None:
    """THE SECOND ACK WRITER: every `health_report` that carries a `summary`.

    §2.11 and `models.DeviceHealthPolicyAck` both describe two writers -- the
    result handler for PROMPTNESS and the summary for CORRECTNESS, "the channel
    that survives a lost result". Only the first existed, so the docstring
    asserted a recovery path the code did not have, and the consequence is not
    slowness but PERMANENCE: §5.2's own known limit says a device whose policy
    result was lost converges when "the device acknowledges via the result
    handler OR ITS NEXT SUMMARY", and with no summary writer there is no second
    chance. The ack row never appears, `run_policy_reconcile`'s "never armed,
    nothing to withdraw" branch keeps skipping it, and the endpoint evaluates a
    deleted profile forever while the reconcile pushes nothing forever.

    THE VERSION IS THE MESSAGE'S, NOT THE LINE'S, and it is applied
    INDEPENDENTLY OF WHETHER ANY LINE APPLIED. `policy_version` is a property of
    the report (§2.11) and says what the AGENT believes it is running; the
    reason the summary is the reliable channel is exactly the case where every
    line in it is discarded because the checks were removed from under it.
    Gating this on `applied > 0` would switch the repair off in the only
    situation that needs it.

    IT ADDS NO ROWS. One row per device, UPSERTed on the PK -- §0.12's
    "cardinality is the fleet and CONSTANT IN TIME" is unchanged, which is why
    this is a writer and not a `policy_version` history table.
    """
    if policy_version < 0:  # pragma: no cover - the wire model already bounds it
        return
    profile_id = await _profile_id_for_version(
        session, org_id, device.id, policy_version, device_row=device
    )
    await _upsert_ack(
        session,
        org_id,
        device.id,
        profile_id=profile_id,
        policy_version=policy_version,
        now=now,
    )


async def _upsert_ack(
    session: AsyncSession,
    org_id: UUID,
    device_id: UUID,
    *,
    profile_id: UUID | None,
    policy_version: int,
    now: datetime,
) -> None:
    """The one statement both writers share (§2.11).

    Keyed on the `device_id` primary key -- one row per device, never a history.
    """
    table = DeviceHealthPolicyAck.__table__
    statement = pg_insert(DeviceHealthPolicyAck).values(
        org_id=org_id,
        device_id=device_id,
        profile_id=profile_id,
        policy_version=policy_version,
        acknowledged_at=now,
        updated_at=now,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["device_id"],
            set_={
                # Scoped "never lower": a lower version for the SAME profile is a
                # late result and must not regress; a different profile (or a
                # disarm, `profile_id IS NULL`) is a different policy and wins.
                "policy_version": _scoped_version(table, statement),
                "profile_id": statement.excluded.profile_id,
                "acknowledged_at": statement.excluded.acknowledged_at,
                "updated_at": statement.excluded.updated_at,
            },
        )
    )
    await session.flush()


def _scoped_version(table: Any, statement: Any) -> Any:
    """`CASE WHEN stored.profile_id IS DISTINCT FROM excluded.profile_id
             THEN excluded.policy_version
             WHEN stored.profile_id IS NULL AND stored.policy_version <> 0
             THEN excluded.policy_version
             ELSE greatest(stored.policy_version, excluded.policy_version) END`

    THE MIDDLE CLAUSE EXISTS BECAUSE THE SUMMARY WRITER CAN STORE AN UNKNOWN
    POLICY. `_profile_id_for_version` answers with the profile the device
    RESOLVES TO NOW, so a device reporting "I am running version 7" after its
    assignment was deleted stores `(profile_id NULL, version 7)` -- a pair the
    result handler alone could never produce, since it only writes NULL for a
    disarm at version 0. Without this clause the disarm that reconcile then
    pushes acknowledges as `(NULL, 0)`, matches on `NULL IS NOT DISTINCT FROM
    NULL`, and loses `greatest(7, 0)` -- so the ack stays at 7 and every
    subsequent reconcile re-pushes the same disarm forever.

    `(NULL, non-zero)` means "installed, policy unidentifiable", which is
    strictly less trustworthy than any later statement about the same device, so
    it yields. The "never lower" guarantee it gives up is only over that one
    state; `(profile, version)` pairs are unaffected, which is what §5.2's rule
    is actually protecting.
    """
    from sqlalchemy import case, func

    return case(
        (
            table.c.profile_id.is_distinct_from(statement.excluded.profile_id),
            statement.excluded.policy_version,
        ),
        (
            table.c.profile_id.is_(None) & (table.c.policy_version != DISARM_POLICY_VERSION),
            statement.excluded.policy_version,
        ),
        else_=func.greatest(table.c.policy_version, statement.excluded.policy_version),
    )


async def _profile_id_for_version(
    session: AsyncSession,
    org_id: UUID,
    device_id: UUID,
    acknowledged_version: int,
    *,
    device_row: Device | None = None,
) -> UUID | None:
    """Which profile the acknowledged version belongs to.

    Resolved fresh rather than carried in the job params: the params are the
    POLICY, and adding a `profile_id` to them would put a server-side
    bookkeeping id inside a signed payload an agent parses. A disarm
    (`policy_version = 0`) is `NULL` by definition (§2.11), and a device whose
    profile has moved on since dispatch records the profile it currently
    resolves to -- which is the one the next reconciliation compares against.
    """
    if acknowledged_version == DISARM_POLICY_VERSION:
        return None
    if device_row is None:
        device_row = (
            await session.execute(
                select(Device).where(Device.org_id == org_id, Device.id == device_id)
            )
        ).scalar_one_or_none()
    if device_row is None:
        return None
    resolved = await resolve_profile_for_device(session, org_id, device_row)
    return None if resolved is None else resolved.profile.id
