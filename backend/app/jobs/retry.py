"""Re-running a job that finished badly.

Cycle 3 of `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md`. Before
this, an operator whose `app.deploy` failed at 02:00 for a laptop that was
asleep had no way to run it again from the console (finding B41's other half),
and `JobEngine.resign_for_retry` -- the only thing in the platform called
"retry" -- raises `InvalidTransition` for any job that is not `queued` or
`dispatched`, i.e. for exactly the case an operator has.

**A RETRY IS A NEW DISPATCH, NOT A REPLAY, AND THAT IS A SECURITY PROPERTY
RATHER THAN AN IMPLEMENTATION DETAIL.** Nothing here re-sends stored signed
bytes. `JobEngine.create_retry` mints a fresh row -- new `id`, new `nonce`,
fresh `issued_at`/`expires_at`, params re-validated against the type's own
model and passed through its refresher -- and signs THOSE canonical bytes. A
route that re-published an old envelope would be a replay primitive with a
permission check in front of it; this one cannot express that, and the agent's
nonce store would refuse it in any case.

**AND IT IS AUTHORIZED EXACTLY AS AN ORIGINAL DISPATCH IS.** The gate is
`dispatch_permission_for(job.type)` -- the permission the job type's OWN
dispatch route requires -- so a holder of `jobs.dispatch` cannot re-run a
`patch.apply` they could never have started. `None` means refuse, here as at
every other call site.

**THE OTHER GATE IS A RECORD, NOT A PERMISSION** (§4.4). `POST /jobs` already
refuses module-governed types because the bypass "is not a missing permission
-- it is a missing RECORD". Retry has that hole and one more: a new job id
ORPHANS the module's record. `ingest_deploy_result` looks up
`DeploymentResult.job_id == job.id` and, finding nothing, "is skipped
gracefully" -- so a kernel retry of a failed `app.deploy` would install the
package and leave the deployment showing pending forever, with nothing
anywhere going red. So a module-governed type is retryable only if it declares
a `retry_relinker`; everything else is a 403 naming the module's own re-run
route, in the same voice `MODULE_GOVERNED_REASONS` uses, so the console can
render a link rather than a dead button.

WHICH TERMINAL STATES MAY BE RETRIED (§4.2). `expired`, `failed`, `refused`
and `cancelled`. NOT `succeeded`: re-running a job that worked is a new
dispatch, not a retry, and it belongs on the route that owns the record and the
bounds. NOT `queued`/`dispatched`/`running`: those are not finished, and the
stuck-dispatch sweep already owns re-sending them.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr, tr_marked
from app.core.rbac import has_permission
from app.jobs.direct_dispatch import (
    DIRECTLY_DISPATCHABLE_JOB_TYPES,
    MODULE_GOVERNED_REASONS,
)
from app.jobs.dispatch_permissions import NO_HUMAN_DISPATCH_PATH, dispatch_permission_for
from app.jobs.engine import JobEngine
from app.jobs.registry import UnknownJobType, get_job_type
from app.models import Job, JobStatus

__all__ = [
    "RETRYABLE_STATUSES",
    "RetryConflict",
    "RetryError",
    "RetryForbidden",
    "RetryNotFound",
    "RetryResult",
    "retry_job",
    "retryable_job_types",
]

#: The four terminal states a retry may start from (design §4.2).
#:
#: `succeeded` IS ABSENT ON PURPOSE and is not an oversight: re-running work
#: that worked is a NEW dispatch, and it belongs on the route that owns the
#: record and the bounds rather than on a verb whose name says "again".
RETRYABLE_STATUSES: frozenset[JobStatus] = frozenset(
    {
        # Never confirmed to have run -- the device was off. The commonest case
        # by a long way, and the one that has no other answer at all.
        JobStatus.expired,
        # It ran and the handler raised. The operator decides.
        JobStatus.failed,
        # `queue_full` is transient by construction; the others refuse again,
        # cheaply, audibly, and on the record.
        JobStatus.refused,
        # A fresh deliberate act. The console confirms, naming the earlier
        # cancellation, so nobody re-runs a job somebody else stopped on purpose.
        JobStatus.cancelled,
    }
)


class RetryError(Exception):
    """Base for the three refusals, so a caller cannot catch one by accident."""


class RetryNotFound(RetryError):
    """No job with that id in THIS org. 404."""


class RetryForbidden(RetryError):
    """The caller may not re-run this, or this type has no kernel retry path. 403."""


class RetryConflict(RetryError):
    """The job is not in a state a retry may start from. 409, naming the state."""


@dataclass(frozen=True)
class RetryResult:
    """What the route publishes after the transaction commits."""

    job: Job
    envelope: dict[str, Any]


def retryable_job_types() -> set[str]:
    """Every registered job type the KERNEL retry route may re-run.

    TWO SOURCES AND NO THIRD. A type no module governs (`hello`,
    `inventory.collect`, `logs.ship`) has no module record to orphan, so the
    kernel route is its owning route here as it is for dispatch. A
    module-governed type qualifies only by DECLARING A RELINKER, which is the
    module saying in code that it knows how to repoint its own record.

    DENY BY DEFAULT, and `tests/test_job_retry.py` asserts this as a PARTITION
    over the live registry -- so a module registering a new job type breaks a
    test rather than picking a side by accident.
    """
    from app.jobs import registry

    out = set(DIRECTLY_DISPATCHABLE_JOB_TYPES)
    for name in registry.registered_names():
        if registry.get_job_type(name).retry_relinker is not None:
            out.add(name)
    return out


def _not_retryable_reason(job_type: str) -> str:
    """Why the kernel route will not re-run this type, naming where it can be.

    The message is the point. A refusal an operator cannot act on is a refusal
    they will look for a way around, and the way around this one is the module's
    own re-run route -- which is where the record that makes a retry meaningful
    lives.
    """
    stated = MODULE_GOVERNED_REASONS.get(job_type)
    if stated is not None:
        return (
            tr(
                "{job_type} cannot be re-run from the jobs route: a retry is a NEW job with a "
                "new id, and this module's record of the work is keyed on the OLD one -- so a "
                "retry from here would run on the endpoint and leave the module's record "
                "pointing at a job that will never speak again. Re-run it where that record "
                "lives.",
                job_type=job_type,
            )
            + " "
            + tr_marked(stated)
        )
    if job_type in NO_HUMAN_DISPATCH_PATH:
        return (
            tr(
                "{job_type} has no human dispatch route in this platform, so there is nothing "
                "to re-run it as.",
                job_type=job_type,
            )
            + " "
            + tr_marked(NO_HUMAN_DISPATCH_PATH[job_type])
        )
    return tr(
        "{job_type} is not re-runnable through the jobs route. That route may only re-run "
        "job types no module governs, plus those whose module has declared how to repoint "
        "its own record at the new job. Adding one is a reviewed change in the owning "
        "module",
        job_type=job_type,
    )


def _state_refusal(job: Job) -> str:
    if job.status is JobStatus.succeeded:
        return tr(
            "this job succeeded, and re-running work that worked is a new dispatch rather "
            "than a retry -- it belongs on the route that owns the record of what was asked "
            "for and the bounds on it, not on a verb whose name says 'again'"
        )
    return tr(
        "this job is {status} and has not finished, so there is nothing to re-run yet. A job "
        "that never reaches its device is re-sent automatically by the platform's own "
        "stuck-dispatch sweep; a job that is running will report its own outcome",
        status=job.status.value,
    )


async def retry_job(
    session: AsyncSession,
    *,
    engine: JobEngine,
    org_id: UUID,
    user_id: UUID,
    job_id: UUID,
) -> RetryResult:
    """Re-run one finished job as a NEW signed job, or raise the refusal.

    THE `org_id` PREDICATE IS EXPLICIT AND IS NOT DECORATION. Everything served
    through `org_session` runs as a NOBYPASSRLS role, so RLS would hide a
    neighbour's row anyway -- which means deleting this predicate turns no
    ordinary route test red (2026-08-21-tenancy-test-efficacy.md §0).
    `tests/test_job_retry.py` drives the real route on the BYPASSRLS engine so
    that deleting it does go red.

    NO ROW LOCK ON THE ORIGINAL, and the asymmetry with `request_cancellation`
    is deliberate. A cancel WRITES the row it read, so it races the gateway's
    delivery bookkeeping and must hold it. A retry reads the original and never
    writes it; the worst a concurrent transition could do is let a retry start
    from a status that changed a millisecond later, and both outcomes are
    states the operator asked for. Taking a lock here would put the platform's
    hottest table under a lock held across a signing call for no property
    gained.
    """
    original = (
        await session.execute(select(Job).where(Job.id == job_id, Job.org_id == org_id))
    ).scalar_one_or_none()
    if original is None:
        raise RetryNotFound(tr("no such job"))

    try:
        job_type = get_job_type(original.type)
    except UnknownJobType:
        # A type that was RETIRED after its jobs were written. Nobody is left to
        # say what re-running it would mean, so nobody may.
        raise RetryForbidden(
            tr(
                "{job_type} is no longer a registered job type in this platform, so there is "
                "nothing to re-run it as",
                job_type=original.type,
            )
        ) from None

    required = dispatch_permission_for(original.type)
    if required is None:
        stated = NO_HUMAN_DISPATCH_PATH.get(original.type)
        refusal = tr(
            "{job_type} has no human dispatch route in this platform, so there is no "
            "permission that would authorize starting one -- and re-running a job is "
            "gated on exactly that permission",
            job_type=original.type,
        )
        if stated:
            refusal += ". " + tr_marked(stated)
        raise RetryForbidden(refusal)
    # ASKED ABOUT THE JOB'S OWN CLIENT (MSP clients spec §2.1): a grant on X
    # authorizes re-running X's jobs and says nothing about Y's. Without the
    # client only every-client grants would count, and a technician granted
    # the dispatch code on X alone could not retry X's own jobs. `original`
    # was just read by the SELECT above, so `client_id` is loaded, not expired.
    if not await has_permission(session, user_id, required, client_id=original.client_id):
        raise RetryForbidden(
            tr(
                "re-running a {job_type} job requires {required}, the permission its own dispatch "
                "route requires: a retry is a new dispatch and is authorized as one",
                job_type=original.type,
                required=required,
            )
        )

    # CHECKED AFTER THE PERMISSION, so a caller who may not run this type at all
    # is told that rather than being handed a map of which types the kernel
    # route governs.
    if original.type not in DIRECTLY_DISPATCHABLE_JOB_TYPES and job_type.retry_relinker is None:
        raise RetryForbidden(_not_retryable_reason(original.type))

    if original.status not in RETRYABLE_STATUSES:
        raise RetryConflict(_state_refusal(original))

    try:
        retry = await engine.create_retry(session, original, requested_by=user_id)
    except ValueError as exc:
        # THE STORED PARAMS NO LONGER SATISFY THE TYPE'S OWN MODEL -- the type
        # gained a required field, or narrowed one, after this job was written.
        # `create` re-validates rather than trusting what is on the row, which
        # is right: signing params the current model would reject would put a
        # job on an endpoint that the platform can no longer describe. Surfaced
        # as a refusal naming the mismatch, never as a 500, because it is a
        # fact about this job and an operator can act on it.
        raise RetryConflict(
            tr(
                "this job's stored parameters no longer satisfy what {job_type} requires, so it "
                "cannot be re-run as it was written. Dispatch a fresh one instead: {exc}",
                job_type=original.type,
                exc=exc,
            )
        ) from None

    # The module repoints its own record at the new job, in THIS transaction --
    # so the new row and the repointed record commit together or not at all.
    # Without it the retried work happens on the endpoint and the module's
    # record never learns of it (§4.4).
    if job_type.retry_relinker is not None:
        await job_type.retry_relinker(session, original, retry)

    # **A NAMED HUMAN RE-RAN FLEET WORK, AND THAT IS AUDITED HERE RATHER THAN
    # LEFT TO `jobs.dispatched`.** The design (§4.1) specifies `retry_of` in
    # the dispatch event's metadata, and that is written -- but `jobs.dispatched`
    # is `actor_type="system"` and is written by the GATEWAY at delivery, which
    # for a device that is off is hours away or never. Invariant #4 requires the
    # privileged act itself on the chain, with its actor, at the moment it
    # happened; the two events answer different questions and neither replaces
    # the other.
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user_id,
        action="jobs.retry_requested",
        target_type="job",
        target_id=str(retry.id),
        metadata={
            "type": retry.type,
            "device": str(retry.target_id),
            "retry_of": str(original.id),
            "retry_of_status": original.status.value,
        },
    )
    await session.flush()
    return RetryResult(job=retry, envelope=await engine.signed_envelope(retry))
