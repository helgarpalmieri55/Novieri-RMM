"""Stopping a signed job: withdrawal, and interruption.

Cycles 1 and 2 of `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md`.
The engine gained no lifecycle verbs at all until cycle 1: a job queued for a
machine that had been off for a week could not be taken back, and the console
said so rather than faking a control (finding B41).

**TWO MECHANISMS, AND THE DIFFERENCE IS WHOSE EVIDENCE IT IS.**

*Withdrawal* is a `queued` job -- one whose signed envelope was never written
to a socket. It is server-side only: the row moves straight to `cancelled`
with `cancel_outcome = "withdrawn"`, nothing reaches any endpoint, and no
envelope is signed. It is the one outcome this platform can assert on its own
authority, and §3.3's delivery-ordering fix is what makes the assertion true.

*Interruption* is a job that has already reached a device. The server has NO
authority over it whatsoever, so it asks: it creates a **signed `job.cancel`
job** for the same device naming the target, records that a human asked, and
leaves the status alone until the endpoint answers.

**INVARIANT 1 GOVERNS INTERRUPTION AND GETS NO EXEMPTION FOR IT.** A cancel
that reaches an endpoint is a command to that endpoint, so it is an ordinary
signed job: signed over canonical bytes by the same signer, verified by the
agent against the key pinned at enrollment, device-bound, nonce-checked and
freshness-checked, before anything on the endpoint acts on it. There is no
"it is only a cancel" path, because a forged cancel is a denial-of-service
primitive against the entire fleet -- one frame per running job and the fleet
stops working. Invariant 2 is untouched as well: the envelope is PUBLISHED,
and it travels down the connection the agent itself opened. Nothing here dials
an endpoint.

**WHAT THE PLATFORM STILL WILL NOT SAY.** `cancelled` is never written from
`dispatched` or `running` on a timeout, a guess, or the passage of time --
only on an endpoint's own answer (`handle_cancel_result` below, and the
target's own result through `JobEngine.complete`/`refuse`). A job whose cancel
was requested and that then reaches `expires_at` in silence is written
`expired`, not `cancelled`: nobody ever told us it stopped.

**WHY THE REFUSAL IS A 409 AND NOT A SILENT 200.** A no-op reported as a
success is the defect class this programme has spent its cycles closing -- a
console reading "Cancelled" for a job that ran to completion is worse than a
console with no cancel button, because the second one is honest. Nor is it a
404: the job exists, the caller may read it, and hiding it would answer a
different question from the one asked.

THE PERMISSION IS THE JOB TYPE'S OWN DISPATCH PERMISSION (design §9.2, settled
by the operator). Whoever may start a job may stop it. This adds no permission
code -- `e25bbe7` made adding one a deliberate decision rather than a default
-- and it forecloses the escalation in which a holder of `jobs.dispatch` stops
a `patch.apply` they could never have started. It has a consequence worth
stating rather than discovering: the three `NO_HUMAN_DISPATCH_PATH` types
(`health.policy.apply`, `agent.cert.renew`, `agent.cert.install`) are
uncancellable from the console, which is correct -- they are minted by the
kernel's own state machines from state the kernel minted, and an operator
cancelling a certificate rotation is cancelling the thing that keeps the
endpoint reachable.

`None` FROM `dispatch_permission_for` IS A REFUSAL, at this call site as at
every other. It is returned both for a type with no human dispatch route and
for one nobody has classified; neither is a licence.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr, tr_marked
from app.core.rbac import has_permission
from app.jobs.dispatch_permissions import NO_HUMAN_DISPATCH_PATH, dispatch_permission_for
from app.jobs.engine import JobEngine, record_cancelled
from app.jobs.registry import JobCancelResult, get_job_type
from app.models import Job, JobLog, JobStatus

__all__ = [
    "AGENT_CANCELLED_REFUSAL",
    "CANCEL_JOB_TYPE",
    "CancelConflict",
    "CancelError",
    "CancelForbidden",
    "CancelRequest",
    "JobNotFound",
    "handle_cancel_refusal",
    "handle_cancel_result",
    "is_agent_cancellation",
    "request_cancellation",
]

#: The job type an interruption travels as. One string, named once, because
#: four allowlists and two result paths all have to agree about it.
CANCEL_JOB_TYPE = "job.cancel"

#: The agent's own refusal reason for a target job it suppressed because a
#: `job.cancel` for it arrived FIRST (design §3.8's ordering race).
#:
#: **THIS CONSTANT IS ONE HALF OF A WIRE CONTRACT** and its other half is
#: `ReasonCancelledBeforeStart` in `agent/internal/jobs/verify.go`. It is not a
#: message: it is the token that tells `JobEngine.refuse` the difference
#: between "the endpoint never started this job" (`dropped`) and "the endpoint
#: stopped it mid-flight" (`interrupted`), which §3.6 renders as two different
#: sentences. Changing it on one side only degrades every such cancellation to
#: `interrupted` -- silently, with no test in this repository red, because both
#: sides of every test here are built from the same commit.
AGENT_CANCELLED_REFUSAL = "cancelled_before_start"

#: The shortest window a `job.cancel` is ever signed for. A cancel whose own
#: TTL is a second is a cancel that expires before the gateway can write it.
MIN_CANCEL_TTL_S = 60


def is_agent_cancellation(reason: str) -> bool:
    """Did the endpoint refuse this job BECAUSE it had been cancelled?

    The agent sends `"<reason>"` or `"<reason>: <its own sentence>"`
    (`safeReason` in `agent/internal/jobs/runner.go`), so the token is matched
    against the head and never against the whole string -- and never with a
    substring search, which a capability's own refusal text could otherwise
    trip by quoting the constant.
    """
    return reason.split(":", 1)[0].strip() == AGENT_CANCELLED_REFUSAL


@dataclass(frozen=True)
class CancelRequest:
    """What the route has to do after the transaction commits.

    `envelope` IS PUBLISHED OUTSIDE THE TRANSACTION, never inside it. A cancel
    published from inside would be a signed command on the wire for a request
    the database might still roll back -- the same rule `POST /jobs` follows,
    and the same reason.
    """

    job: Job
    #: The signed `job.cancel` envelope to publish, or `None` for a withdrawal
    #: -- which reaches no endpoint at all.
    envelope: dict[str, Any] | None


class CancelError(Exception):
    """Base for the three refusals, so a caller cannot catch one by accident."""


class JobNotFound(CancelError):
    """No job with that id in THIS org. 404."""


class CancelForbidden(CancelError):
    """The caller does not hold the job type's own dispatch permission. 403."""


class CancelConflict(CancelError):
    """The job is not in a state that can be withdrawn. 409, naming the state."""


def _finished_refusal(job: Job) -> str:
    when = _stamp(job.finished_at)
    return tr(
        "this job {outcome}{when} and cannot be cancelled. A job that has reached a terminal "
        "state is final -- its outcome is what the record says happened, and nothing may "
        "rewrite it",
        outcome=_verb(job.status),
        when=when,
    )


def _verb(status: JobStatus) -> str:
    if status is JobStatus.succeeded:
        return tr("finished successfully")
    if status is JobStatus.failed:
        return tr("failed")
    if status is JobStatus.expired:
        return tr("expired")
    if status is JobStatus.refused:
        return tr("was refused by the endpoint")
    return tr("is {status}", status=status.value)


def _stamp(moment: datetime | None) -> str:
    return tr(" at {moment}", moment=moment.isoformat()) if moment is not None else ""


def _not_cancellable_refusal(job: Job) -> str:
    """A job on a device whose TYPE may not be interrupted (design §9.1).

    A 409 WITH A REASON RATHER THAN A CANCEL THAT QUIETLY DOES NOTHING. The
    operator asked the platform to stop a running install; the honest answer is
    that stopping one halfway is worse than letting it finish, not a spinner
    that never resolves.
    """
    reached = (
        tr("has already been sent to the device")
        if job.status is JobStatus.dispatched
        else tr("is executing on the device")
    )
    return tr(
        "this job {reached}, and {job_type} may not be interrupted once it has got there: "
        "stopping one part-way leaves the endpoint in a state nobody chose -- a half-applied "
        "update, a half-written restore -- which is worse than the job you wanted to stop. "
        "Withdrawal is only offered before a job is sent. The job will run or expire at "
        "{expires_at}",
        reached=reached,
        job_type=job.type,
        expires_at=job.expires_at.isoformat(),
    )


def _cancel_ttl_s(job: Job, now: datetime) -> int:
    """How long the `job.cancel` envelope is signed for.

    THE TARGET'S OWN REMAINING WINDOW, and deliberately not the platform
    default. A cancel that outlived its target would be a signed command
    sitting in a queue for a job that has already expired -- drained on the
    next connect, answered `unknown`, and audited as though somebody had just
    asked. A cancel that dies with its target cannot do that.
    """
    remaining = int((job.expires_at - now).total_seconds())
    return max(MIN_CANCEL_TTL_S, remaining)


async def request_cancellation(
    session: AsyncSession,
    *,
    engine: JobEngine,
    org_id: UUID,
    user_id: UUID,
    job_id: UUID,
) -> CancelRequest:
    """Stop one job -- by withdrawing it, or by asking its endpoint to.

    `FOR UPDATE`, AND THE STATUS IS READ INSIDE THE LOCK. The gateway's
    delivery bookkeeping writes the same row from another process, and under
    READ COMMITTED an unlocked read would let a cancel and a delivery both see
    `queued` and both write -- producing a job the console reports as
    withdrawn and the endpoint has in hand. `app/gateway/main.py::
    _on_delivered` takes the same lock from the other side; the two together
    are what make "withdrawn" a fact rather than a hope.

    THE `org_id` PREDICATE IS EXPLICIT AND IS NOT DECORATION. Everything served
    through `org_session` runs as a NOBYPASSRLS role, so RLS would hide a
    neighbour's row anyway -- which means deleting this predicate turns no
    ordinary route test red (2026-08-21-tenancy-test-efficacy.md §0). The
    application layer is the half a reader can see, and
    `test_job_cancel.py::test_a_neighbours_job_cannot_be_cancelled_without_rls_to_mask_it`
    drives this on the BYPASSRLS engine so that deleting it does go red.

    THE AUTHORIZATION IS THE SAME ONE FOR BOTH MECHANISMS, and it is checked
    before either. Interruption is not a lesser act than withdrawal -- it is a
    signed command to an endpoint -- so it does not get a weaker gate.
    """
    job = (
        await session.execute(
            select(Job).where(Job.id == job_id, Job.org_id == org_id).with_for_update()
        )
    ).scalar_one_or_none()
    if job is None:
        raise JobNotFound(tr("no such job"))

    required = dispatch_permission_for(job.type)
    if required is None:
        stated = NO_HUMAN_DISPATCH_PATH.get(job.type)
        refusal = tr(
            "{job_type} has no human dispatch route in this platform, so there is no "
            "permission that would authorize starting one -- and cancelling a job is "
            "gated on exactly that permission",
            job_type=job.type,
        )
        if stated:
            refusal += ". " + tr_marked(stated)
        raise CancelForbidden(refusal)
    if not await has_permission(session, user_id, required):
        raise CancelForbidden(
            tr(
                "cancelling a {job_type} job requires {required}, the permission its own dispatch "
                "route requires: whoever may start a job may stop it",
                job_type=job.type,
                required=required,
            )
        )

    # Checked BEFORE the status, so a second cancel names the first request
    # rather than restating the state it produced. It matters more now than it
    # did in cycle 1: an interruption leaves the status UNCHANGED, so a
    # `running` job whose cancel is already in flight would otherwise look
    # exactly like one nobody has asked about, and a second press would mint a
    # second signed command for the same target.
    if job.cancel_requested_at is not None:
        raise CancelConflict(
            tr(
                "a cancellation for this job was already requested at {requested_at}; it is not "
                "requested twice",
                requested_at=job.cancel_requested_at.isoformat(),
            )
        )
    if job.status is JobStatus.queued:
        await engine.withdraw(session, job, requested_by=user_id)
        return CancelRequest(job=job, envelope=None)
    if job.status in (JobStatus.dispatched, JobStatus.running):
        if not get_job_type(job.type).cancellable:
            raise CancelConflict(_not_cancellable_refusal(job))
        now = datetime.now(UTC)
        cancel_job = await engine.create(
            session,
            org_id=org_id,
            type=CANCEL_JOB_TYPE,
            target_device_id=job.target_id,
            # THE TARGET IS THE SERVER'S OWN STATEMENT, SIGNED. It is read back
            # out of these params when the result arrives and never out of the
            # agent's result body, so a compromised endpoint cannot name a
            # different job to close.
            params={"target_job_id": str(job.id)},
            requested_by=user_id,
            ttl_s=_cancel_ttl_s(job, now),
        )
        await engine.record_cancel_request(
            session, job, requested_by=user_id, cancel_job_id=cancel_job.id
        )
        return CancelRequest(job=job, envelope=await engine.signed_envelope(cancel_job))
    raise CancelConflict(_finished_refusal(job))


# ---------------------------------------------------------------------------
# The endpoint's answer (design §3.5)
# ---------------------------------------------------------------------------


class CancelResultError(Exception):
    """The endpoint's `job.cancel` result is not one this server can read.

    Raised from the result handler, so `JobEngine.complete` records the CANCEL
    job as failed with the reason. The target is untouched: a result we cannot
    parse is not evidence of anything, and the request stands.
    """


#: What each reported outcome means for the TARGET job (design §3.5).
#:
#: Only `dropped_before_start` closes the target here, and the rule is one
#: sentence: **close it only when nothing else will ever speak for it.** A job
#: the endpoint says it never started has no result coming, so this is its last
#: chance to reach a terminal state. `interrupted` leaves the target alone
#: because the target sends its own result and `JobEngine.complete`/`refuse`
#: map it; `already_finished` and `unknown` leave it alone because it either
#: reached a terminal state on its own or this agent has never heard of it.
_OUTCOME_TO_TARGET: dict[str, tuple[str, bool]] = {
    "dropped_before_start": ("dropped", True),
    "interrupted": ("interrupted", False),
    "already_finished": ("too_late", False),
    "unknown": ("too_late", False),
}


async def _target_of(session: AsyncSession, cancel_job: Job) -> Job | None:
    """The job a `job.cancel` was signed to stop, or `None` if it may not act.

    THREE PREDICATES, AND EACH ONE IS A DIFFERENT ATTACK.

    * The id comes from `cancel_job.params` -- the bytes THIS SERVER SIGNED --
      so a compromised endpoint cannot redirect the outcome at another job.
    * `org_id` must match the cancel job's, so a device in one tenant can never
      reach a row in another. This is the application half of invariant #5; RLS
      is the other half and neither is a substitute for the other.
    * `target_id` must match, so a compromised device cannot close a job
      belonging to a DIFFERENT device in its own org -- the same rule
      `_load_job_for_device` applies to every other agent message.
    """
    raw = cancel_job.params.get("target_job_id")
    if not isinstance(raw, str):
        return None
    try:
        target_id = UUID(raw)
    except ValueError:
        return None
    target = (
        await session.execute(
            select(Job)
            .where(Job.id == target_id, Job.org_id == cancel_job.org_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None or target.target_id != cancel_job.target_id:
        return None
    return target


async def _audit_cancel_mismatch(session: AsyncSession, cancel_job: Job, detail: str) -> None:
    await audit_service.record(
        session,
        org_id=cancel_job.org_id,
        actor_type="agent",
        actor_id=None,
        action="jobs.cancel_target_mismatch",
        target_type="job",
        target_id=str(cancel_job.id),
        metadata={"device": str(cancel_job.target_id), "detail": detail},
    )


async def handle_cancel_result(
    session: AsyncSession, cancel_job: Job, result: dict[str, Any]
) -> None:
    """`JobType.result_handler` for `job.cancel`: read the outcome, close what may be closed.

    Runs in the gateway process, inside the tenant transaction that recorded
    the cancel job's own result, so the target's state and the audit record of
    it commit together or not at all.
    """
    try:
        answer = JobCancelResult(**result)
    except ValidationError as exc:
        raise CancelResultError(  # i18n-exempt: gateway result path, logged only
            f"invalid job.cancel result: {exc}"
        ) from exc

    target = await _target_of(session, cancel_job)
    if target is None:
        await _audit_cancel_mismatch(
            session, cancel_job, "the signed target names no job on this device in this org"
        )
        return
    if answer.target_job_id != str(target.id):
        # THE FALSIFIER FOR "CANCEL IS UNFORGEABLE" IS A RESULT CLOSING A JOB
        # NAMED ANYWHERE OTHER THAN THE SIGNED PARAMS. This is that case: the
        # endpoint answered about a different job from the one it was asked
        # about, so the answer is discarded rather than reinterpreted.
        await _audit_cancel_mismatch(
            session,
            cancel_job,
            f"result names {answer.target_job_id}, the signed params name {target.id}",
        )
        return

    outcome, closes_target = _OUTCOME_TO_TARGET[answer.outcome]
    if closes_target and target.status in (
        JobStatus.queued,
        JobStatus.dispatched,
        JobStatus.running,
    ):
        await record_cancelled(session, target, outcome=outcome, actor_type="agent")
        return
    # `requested` IS THE ONLY VALUE THIS OVERWRITES. A target that has already
    # been closed with better evidence -- its own refusal naming the agent's
    # cancellation constant, say -- must not be downgraded to `too_late` by a
    # cancel result that arrives afterwards.
    if target.cancel_outcome == "requested":
        target.cancel_outcome = outcome
        session.add(target)
        await session.flush()


async def handle_cancel_refusal(session: AsyncSession, cancel_job: Job, *, reason: str) -> None:
    """A `job.cancel` the endpoint refused. `unknown_type` is the one that matters.

    **THIS IS THE MIXED-VERSION CASE, ON THE RECORD RATHER THAN IN A LOG
    FILE** (design §3.7). An agent built before this cycle has no `job.cancel`
    capability, so its verifier refuses the type *before anything runs* and
    sends an ordinary `job_result`. That answer is the entire reason cancel is
    a job TYPE and not a new wire `kind`: an unknown `kind` is dropped in
    silence by a deployed agent, and the control plane would have to
    distinguish "old agent" from "job already finished" by timeout, i.e. by
    guessing. Here it is told, from the endpoint, in seconds.

    THE TARGET IS UNTOUCHED apart from the outcome word. It runs or expires
    exactly as it would have; the operator is told the endpoint cannot be
    interrupted rather than being left to watch "Cancel requested" forever.

    ANY OTHER REFUSAL LEAVES THE REQUEST STANDING. A `queue_full`, a
    `replayed_nonce`, an expired envelope -- none of those says anything about
    whether this endpoint understands cancellation, and writing `unsupported`
    for them would put a false sentence in front of the operator.
    """
    if not reason.split(":", 1)[0].strip() == "unknown_type":
        return
    target = await _target_of(session, cancel_job)
    if target is None:
        await _audit_cancel_mismatch(
            session, cancel_job, "the signed target names no job on this device in this org"
        )
        return
    if target.cancel_outcome == "requested":
        target.cancel_outcome = "unsupported"
        session.add(target)
    session.add(
        JobLog(
            org_id=target.org_id,
            job_id=target.id,
            ts=datetime.now(UTC),
            level="warn",
            message=(
                "This device's agent does not support being interrupted: it refused the "
                "signed job.cancel as an unknown job type. The job will run or expire "
                "as it would have."
            ),
            artifact_ref=None,
        )
    )
    await audit_service.record(
        session,
        org_id=target.org_id,
        actor_type="agent",
        actor_id=None,
        action="jobs.cancel_unsupported",
        target_type="job",
        target_id=str(target.id),
        metadata={
            "type": target.type,
            "device": str(target.target_id),
            "cancel_job_id": str(cancel_job.id),
        },
    )
    await session.flush()
