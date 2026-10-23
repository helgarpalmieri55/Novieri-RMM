"""The signed-job engine (docs/kernel/spec.md §9).

Owns the job lifecycle. Every envelope leaving this engine is signed over the
canonical bytes of {id, type, device, params, issued_at, expires_at, nonce};
retries are re-signed with a fresh nonce and expiry — nonces are single-use by
design and the agent rejects replays.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.sweep import settle_action_for_job
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.signing import SigningService
from app.jobs.canonical import canonical_bytes, rfc3339
from app.jobs.registry import UnknownJobType, get_job_type
from app.jobs.review_gate import DeviceAwaitingReview, refused
from app.models import Device, Job, JobStatus

__all__ = [
    "DeviceAwaitingReview",
    "InvalidTransition",
    "JobEngine",
    "UnknownJobType",
    "record_cancel_request",
    "record_cancelled",
]


class InvalidTransition(Exception):
    pass


# queued -> running/succeeded/failed is deliberately legal: an agent's report is
# authoritative and may arrive before the gateway's delivery bookkeeping
# (mark_dispatched) commits. Terminal states never transition.
_ALLOWED: dict[JobStatus, set[JobStatus]] = {
    JobStatus.queued: {
        JobStatus.dispatched,
        JobStatus.running,
        JobStatus.succeeded,
        JobStatus.failed,
        JobStatus.expired,
        JobStatus.refused,
    },
    JobStatus.dispatched: {
        JobStatus.running,
        JobStatus.succeeded,
        JobStatus.failed,
        JobStatus.expired,
        JobStatus.refused,
    },
    JobStatus.running: {JobStatus.succeeded, JobStatus.failed, JobStatus.refused},
    JobStatus.succeeded: set(),
    JobStatus.failed: set(),
    JobStatus.expired: set(),
    JobStatus.refused: set(),
    JobStatus.cancelled: set(),
}

# THE THREE WAYS INTO `cancelled`, WRITTEN HERE RATHER THAN BURIED IN THE SET
# LITERALS ABOVE, because each rests on a different kind of evidence and the
# difference is the whole design (2026-08-22-job-lifecycle-design.md §3.2).
#
#   queued     -> cancelled   WITHDRAWAL, on the server's own authority -- the
#                             only authority it has. `queued` means the signed
#                             envelope was never written to a socket, so
#                             nothing on any endpoint can contradict the word.
#   dispatched -> cancelled   THE ENDPOINT SAID it discarded the job before
#                             starting it. Never written on a timeout or a
#                             guess: only on an agent's own answer.
#   running    -> cancelled   THE ENDPOINT SAID it stopped mid-execution.
#
# There is deliberately NO `cancelling` state. A job that is running while a
# cancel is in flight IS STILL RUNNING, and replacing that with a pending badge
# would delete a true fact to display a hoped-for one. The request lives in
# `cancel_requested_at`, beside the status, never instead of it.
_ALLOWED[JobStatus.queued].add(JobStatus.cancelled)
_ALLOWED[JobStatus.dispatched].add(JobStatus.cancelled)
_ALLOWED[JobStatus.running].add(JobStatus.cancelled)


def _dispatch_metadata(job: Job) -> dict[str, Any]:
    """`jobs.dispatched`'s metadata, plus the lineage when there is one.

    `retry_of` IS PRESENT ONLY ON A RETRY (design §4.1), so the chain shows two
    acts and their relationship rather than two unrelated dispatches. It is
    omitted -- not written as null -- for an ordinary dispatch: a key that is
    always there and usually empty reads as a field somebody forgot to fill in.
    """
    metadata: dict[str, Any] = {
        "type": job.type,
        "device": str(job.target_id),
        "attempt": job.attempts,
    }
    if job.retry_of_job_id is not None:
        metadata["retry_of"] = str(job.retry_of_job_id)
    return metadata


def _transition(job: Job, to: JobStatus) -> None:
    """The one guard on every status write. Module level so the writers that
    are not engine methods (`record_cancelled`) cannot route around it."""
    if to not in _ALLOWED[job.status]:
        raise InvalidTransition(  # i18n-exempt: state-machine guard, unhandled 500
            f"{job.status.value} -> {to.value}"
        )
    job.status = to

DEFAULT_TTL_S = 3600


class JobEngine:
    def __init__(self, signer: SigningService, *, default_ttl_s: int = DEFAULT_TTL_S) -> None:
        self.signer = signer
        self._default_ttl_s = default_ttl_s

    @property
    def default_ttl_s(self) -> int:
        """The TTL `create` applies when a caller names none.

        Read by dispatchers that must mint something whose lifetime has to
        match the job's — `app.deploy`'s presigned artifact URL derives its
        expiry from this and then passes the SAME value as `ttl_s`, so the two
        windows cannot drift apart via configuration.
        """
        return self._default_ttl_s

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
    ) -> Job:
        job_type = get_job_type(type)
        # THE REVIEW GATE (install-link spec §1.5). Before params are validated
        # and before anything is written or signed: a refused job leaves no
        # row, no signature and no audit event behind it.
        review_state = (
            await session.execute(
                select(Device.review_state).where(
                    Device.id == target_device_id, Device.org_id == org_id
                )
            )
        ).scalar_one_or_none()
        if refused(review_state, type):
            raise DeviceAwaitingReview(target_device_id, type, review_state or "")
        try:
            validated = job_type.params_model(**params)
        except ValidationError as exc:
            raise ValueError(
                tr("invalid params for {job_type}: {exc}", job_type=type, exc=exc)
            ) from exc

        now = datetime.now(UTC).replace(microsecond=0)
        job = Job(
            org_id=org_id,
            type=type,
            target_type="device",
            target_id=target_device_id,
            params=validated.model_dump(),
            nonce=str(uuid4()),
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_s or self._default_ttl_s),
            requested_by=requested_by,
        )
        session.add(job)
        await session.flush()
        job.signature = await self._sign(job)
        session.add(job)
        await session.flush()
        return job

    def _payload(self, job: Job) -> dict[str, Any]:
        return {
            "id": str(job.id),
            "type": job.type,
            "device": str(job.target_id),
            "params": job.params,
            "issued_at": rfc3339(job.issued_at),
            "expires_at": rfc3339(job.expires_at),
            "nonce": job.nonce,
        }

    async def _sign(self, job: Job) -> str:
        return await self.signer.sign(canonical_bytes(self._payload(job)))

    async def signed_envelope(self, job: Job) -> dict[str, Any]:
        signature = job.signature or await self._sign(job)
        return {"v": 1, "kind": "job", "job": self._payload(job), "sig": signature}

    def _transition(self, job: Job, to: JobStatus) -> None:
        _transition(job, to)

    async def mark_dispatched(self, session: AsyncSession, job: Job) -> None:
        self._transition(job, JobStatus.dispatched)
        job.attempts += 1
        session.add(job)
        await audit_service.record(
            session,
            org_id=job.org_id,
            actor_type="system",
            actor_id=None,
            action="jobs.dispatched",
            target_type="job",
            target_id=str(job.id),
            metadata=_dispatch_metadata(job),
        )
        await session.flush()

    async def mark_running(self, session: AsyncSession, job: Job) -> None:
        self._transition(job, JobStatus.running)
        job.started_at = datetime.now(UTC)
        session.add(job)
        await session.flush()

    async def complete(
        self,
        session: AsyncSession,
        job: Job,
        *,
        state: Literal["succeeded", "failed"],
        result: dict[str, Any] | None = None,
    ) -> None:
        """Record the endpoint's own answer for this job.

        A `failed` RESULT ON A JOB SOMEBODY CANCELLED IS A CANCELLATION, not a
        failure (design §3.5). The capability stopped because it was asked to;
        recording that as `failed` would put an alert-worthy state on work the
        platform carried out correctly, and would hide the one fact the
        operator asked about. The agent's result body is stored VERBATIM either
        way, so nothing the endpoint said is lost.

        A `succeeded` RESULT IS RECORDED `succeeded`, cancel or no cancel. The
        job finished before the interrupt landed, and this is exactly the case
        the design exists to tell the truth about: "the cancel arrived too
        late; the job completed" is a different sentence from "cancelled", and
        a console printing the second would be lying about a machine that did
        the work.
        """
        if state == "failed" and job.cancel_requested_at is not None:
            await record_cancelled(
                session, job, outcome="interrupted", actor_type="agent", result=result
            )
            return
        self._transition(job, JobStatus(state))
        job.result = result
        job.finished_at = datetime.now(UTC)
        session.add(job)

        # Module extension point: a succeeded result runs the job type's
        # handler inside this same tenant transaction. A raising handler
        # converts the outcome to failed — modules never crash the gateway.
        if state == "succeeded":
            handler = get_job_type(job.type).result_handler
            if handler is not None:
                try:
                    await handler(session, job, result or {})
                except Exception as exc:  # noqa: BLE001 - contained by design
                    job.status = JobStatus.failed
                    job.result = {"error": str(exc)}
                    state = "failed"
                    session.add(job)
        await audit_service.record(
            session,
            org_id=job.org_id,
            actor_type="agent",
            actor_id=None,
            action=f"jobs.{state}",
            target_type="job",
            target_id=str(job.id),
            metadata={"type": job.type, "device": str(job.target_id)},
        )
        await self._settle_ai_action(session, job, state)
        scrub_terminal_params(job)
        await session.flush()

    async def _settle_ai_action(
        self,
        session: AsyncSession,
        job: Job,
        state: Literal["succeeded", "failed", "refused", "expired", "cancelled"],
    ) -> None:
        """Follow an AI-proposed job to its terminal state
        (docs/modules/ai-automation.md §5.2).

        **THERE IS NO SINGLE "RESULT PATH" IN THIS ENGINE, AND THAT IS A FINDING
        RATHER THAN AN OVERSIGHT.** §5.2 says `settled` is "set by the same result
        path that already updates `jobs`, so this layer runs no poller", and
        assumes one such path. There are three terminal writers here --
        `complete` (succeeded/failed), `refuse` and `expire_due` -- reached from
        two different processes (the gateway and the expiry beat). Hooking only
        `complete` would leave a refused or expired job's `ai_action` sitting in
        `dispatched` forever, which is a lie about a signed job's outcome on the
        confirmation history. So all three call THIS one body, and the AI layer
        still runs no poller.

        It lives in the engine rather than at the two call sites because the call
        sites are `app/gateway/dispatch.py` and `app/worker.py`, and a
        transition that has to be mirrored by every future caller is a
        transition that will eventually not be.
        """
        await settle_action_for_job(
            session, org_id=job.org_id, job_id=job.id, terminal_state=state
        )

    async def refuse(self, session: AsyncSession, job: Job, *, reason: str) -> None:
        """The endpoint declined this job. `reason` is the agent's own word.

        TWO CANCELLATION RULES RIDE THIS PATH, and both live here rather than
        at the gateway call site, because a transition every future caller has
        to remember is a transition somebody will eventually not.

        1. A REFUSAL ON A JOB SOMEBODY CANCELLED IS A CANCELLATION (§3.5), and
           the outcome distinguishes the two things that can have happened: the
           agent's own cancellation constant means the job NEVER STARTED
           (`dropped`); anything else means it was stopped mid-flight
           (`interrupted`). §3.6 shows the operator a different sentence for
           each, so collapsing them would delete the distinction these columns
           exist to carry.

        2. A `job.cancel` REFUSED `unknown_type` IS THE MIXED-VERSION CASE, on
           the record rather than in a log file (§3.7). An agent built before
           this cycle has no `job.cancel` capability, so its verifier refuses
           the type before anything runs -- the loudest, fastest answer
           available, and the whole reason cancel is a job type rather than a
           new wire kind. The TARGET is marked `unsupported` and the console
           says the endpoint is too old to be interrupted.
        """
        from app.jobs.cancel import (
            CANCEL_JOB_TYPE,
            handle_cancel_refusal,
            is_agent_cancellation,
        )

        if job.cancel_requested_at is not None and job.status is not JobStatus.cancelled:
            await record_cancelled(
                session,
                job,
                outcome="dropped" if is_agent_cancellation(reason) else "interrupted",
                actor_type="agent",
                result={"refused": reason},
            )
            return

        self._transition(job, JobStatus.refused)
        job.finished_at = datetime.now(UTC)
        job.result = {"refused": reason}
        session.add(job)
        await audit_service.record(
            session,
            org_id=job.org_id,
            actor_type="agent",
            actor_id=None,
            action="jobs.refused",
            target_type="job",
            target_id=str(job.id),
            metadata={"type": job.type, "device": str(job.target_id), "reason": reason},
        )
        await self._settle_ai_action(session, job, "refused")
        scrub_terminal_params(job)
        if job.type == CANCEL_JOB_TYPE:
            await handle_cancel_refusal(session, job, reason=reason)
        await session.flush()

    async def record_cancel_request(
        self,
        session: AsyncSession,
        job: Job,
        *,
        requested_by: UUID | None,
        cancel_job_id: UUID,
    ) -> None:
        """`record_cancel_request`, for callers that already hold an engine."""
        await record_cancel_request(
            session, job, requested_by=requested_by, cancel_job_id=cancel_job_id
        )

    async def record_cancelled(
        self,
        session: AsyncSession,
        job: Job,
        *,
        outcome: str,
        actor_type: Literal["system", "agent"],
        result: dict[str, Any] | None = None,
    ) -> None:
        """`record_cancelled`, for callers that already hold an engine."""
        await record_cancelled(
            session, job, outcome=outcome, actor_type=actor_type, result=result
        )

    async def withdraw(
        self, session: AsyncSession, job: Job, *, requested_by: UUID | None
    ) -> None:
        """Take a QUEUED job to `cancelled` on the server's own authority.

        WHY THIS IS SAFE FOR A QUEUED JOB AND FOR NO OTHER. `queued` means the
        signed envelope was never written to a socket -- the property
        `DispatchSubscriber` and `queued_for_device` were reordered and locked
        to guarantee (2026-08-22-job-lifecycle-design.md §3.3). Nothing on any
        endpoint knows this job exists, so nothing on any endpoint can
        contradict the word `cancelled`. `_transition` refuses every other
        source state, which is what keeps that sentence true as the platform
        grows a second path into this column.

        NO COMMAND REACHES AN ENDPOINT. Invariant 1 is untouched by this cycle:
        the whole act is a row change plus two audit appends, in the caller's
        transaction, and no envelope is signed or published.

        TWO AUDIT EVENTS, ANSWERING DIFFERENT QUESTIONS. `jobs.cancel_requested`
        records that a NAMED HUMAN asked, with `mechanism` saying whether the
        platform stopped the job itself or merely passed the request on -- the
        field that stops "we asked" and "we stopped it" reading alike in the
        chain. `jobs.cancelled` records the terminal state where it is written,
        with `actor_type="system"`, because for a withdrawal the server is what
        acted. Cycle 2 will write the same second event with
        `actor_type="agent"` when an endpoint confirms, which is exactly why
        the request and the outcome are not one event.

        Both appends and the row change are in ONE session, so the state and
        its record commit together or not at all (invariant #4).

        THE SOURCE STATE IS CHECKED HERE AND NOT LEFT TO `_transition`, and
        that became load-bearing the moment interruption landed. `_ALLOWED` now
        permits `dispatched -> cancelled` and `running -> cancelled`, because an
        endpoint's own answer may write them -- but `withdraw` writes
        `cancel_outcome = "withdrawn"`, which is the sentence "this never
        reached a device", and that sentence is only true from `queued`.
        Without this guard the method would happily stamp it on a job the fleet
        already has in hand.
        """
        if job.status is not JobStatus.queued:
            raise InvalidTransition(  # i18n-exempt: state-machine guard, unhandled 500
                f"withdrawal is only honest for a queued job; this one is "
                f"{job.status.value}"
            )
        self._transition(job, JobStatus.cancelled)
        now = datetime.now(UTC)
        job.cancel_requested_at = now
        job.cancel_requested_by = requested_by
        job.cancel_outcome = "withdrawn"
        job.finished_at = now
        session.add(job)
        await audit_service.record(
            session,
            org_id=job.org_id,
            actor_type="user",
            actor_id=requested_by,
            action="jobs.cancel_requested",
            target_type="job",
            target_id=str(job.id),
            metadata={
                "type": job.type,
                "device": str(job.target_id),
                "from_status": JobStatus.queued.value,
                "mechanism": "withdrawn",
            },
        )
        await audit_service.record(
            session,
            org_id=job.org_id,
            actor_type="system",
            actor_id=None,
            action="jobs.cancelled",
            target_type="job",
            target_id=str(job.id),
            metadata={
                "type": job.type,
                "device": str(job.target_id),
                "cancel_outcome": "withdrawn",
            },
        )
        await self._settle_ai_action(session, job, "cancelled")
        scrub_terminal_params(job)
        await session.flush()

    async def expire_due(self, session: AsyncSession) -> int:
        """Flip overdue queued/dispatched jobs to expired. Runs inside a tenant
        or admin session; audits each expiry."""
        now = datetime.now(UTC)
        due = (
            (
                await session.execute(
                    select(Job).where(
                        Job.status.in_([JobStatus.queued, JobStatus.dispatched]),
                        Job.expires_at < now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in due:
            self._transition(job, JobStatus.expired)
            job.finished_at = now
            session.add(job)
            await audit_service.record(
                session,
                org_id=job.org_id,
                actor_type="system",
                actor_id=None,
                action="jobs.expired",
                target_type="job",
                target_id=str(job.id),
                metadata={"type": job.type},
            )
            await self._settle_ai_action(session, job, "expired")
            scrub_terminal_params(job)
        await session.flush()
        return len(due)

    async def queued_for_device(self, session: AsyncSession, device_id: UUID) -> list[Job]:
        """This device's undelivered queue, LOCKED for the caller's transaction.

        The only caller is `GatewayServer._drain_queued`, which marks each row
        `dispatched` and then writes the envelopes to the reconnecting agent.
        Read without a lock, a withdrawal that committed between this SELECT
        and that UPDATE was silently overwritten and the job went to the
        endpoint anyway -- the mirror of the race `_on_delivered` closes
        (2026-08-22-job-lifecycle-design.md §3.3).

        `FOR UPDATE` is safe here precisely because the transaction is short
        and touches one device's rows: it opens no socket (the writes happen
        after it commits) and no other writer holds these rows for longer than
        its own single-row update.
        """
        now = datetime.now(UTC)
        return list(
            (
                await session.execute(
                    select(Job)
                    .where(
                        Job.target_id == device_id,
                        Job.status == JobStatus.queued,
                        Job.expires_at > now,
                    )
                    .order_by(Job.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )

    async def create_retry(
        self, session: AsyncSession, original: Job, *, requested_by: UUID | None
    ) -> Job:
        """A NEW signed job that re-runs `original`. The original is not touched.

        **THIS IS A DISPATCH, NOT A REPLAY, AND THE DIFFERENCE IS STRUCTURAL.**
        Nothing here reads the original's stored signature or its stored bytes.
        A fresh row is created through the same `create` every other dispatch
        goes through -- new `id`, new `nonce`, new `issued_at`/`expires_at`,
        params re-validated against the type's own model -- and signed over
        those new canonical bytes. Re-sending the original envelope would be a
        replay primitive; the agent's nonce store would refuse it anyway, and
        the point is that this path cannot express it.

        THE PARAMS REFRESHER RUNS, AND IT HAS TO. `app.deploy` signs a presigned
        artifact URL into its params whose lifetime is the job's own window, so
        a retry that re-signed the stored params would carry a dead URL and
        fail for a reason nothing in the response explains. The refresher runs
        AFTER `expires_at` is set -- so it sizes fresh material against the
        window this attempt actually gets -- and the envelope is re-signed over
        the refreshed params, because a signature over params the database will
        not store is a signature over a lie.

        THE ORIGINAL IS READ AND NEVER WRITTEN. Its `result` is the failure body
        the operator is retrying FROM, and its terminal status is what makes its
        outcome final; `retry_of_job_id` on the NEW row is the only link.
        """
        retry = await self.create(
            session,
            org_id=original.org_id,
            type=original.type,
            target_device_id=original.target_id,
            params=original.params,
            requested_by=requested_by,
        )
        retry.retry_of_job_id = original.id

        refresher = get_job_type(original.type).params_refresher
        if refresher is not None:
            refreshed = await refresher(session, retry)
            if refreshed is not None:
                # Reassigned, never mutated in place: SQLAlchemy does not see a
                # JSONB dict edited under it.
                retry.params = refreshed
                retry.signature = await self._sign(retry)
        session.add(retry)
        await session.flush()
        return retry

    async def resign_for_retry(self, session: AsyncSession, job: Job) -> None:
        """Fresh nonce + expiry for a redispatch. Never reuses a nonce.

        A job type may also need fresh PARAMS. The new expiry is written
        BEFORE the type's `params_refresher` runs, so a refresher minting
        something time-bounded (a presigned artifact URL) sizes it against the
        window this attempt actually gets, and the re-signed envelope covers
        the refreshed params. See `ParamsRefresher` for why a refresher must
        not raise.
        """
        if job.status not in (JobStatus.queued, JobStatus.dispatched):
            raise InvalidTransition(  # i18n-exempt: state-machine guard, unhandled 500
                f"cannot re-sign a {job.status.value} job"
            )
        now = datetime.now(UTC).replace(microsecond=0)
        job.nonce = str(uuid4())
        job.issued_at = now
        job.expires_at = now + timedelta(seconds=self._default_ttl_s)
        job.attempts += 1
        job.status = JobStatus.queued

        refresher = get_job_type(job.type).params_refresher
        if refresher is not None:
            refreshed = await refresher(session, job)
            if refreshed is not None:
                # Reassigned, never mutated in place: SQLAlchemy does not see a
                # JSONB dict edited under it, and a job re-signed over params
                # the database will not store is a signature over a lie.
                job.params = refreshed

        job.signature = await self._sign(job)
        session.add(job)
        await session.flush()


# ---------------------------------------------------------------------------
# Interruption's two writers (2026-08-22-job-lifecycle-design.md §3.2, §3.5)
# ---------------------------------------------------------------------------
#
# MODULE LEVEL, NOT ENGINE METHODS, and the reason is a call site rather than a
# preference: `job.cancel`'s `JobType.result_handler` is invoked by
# `JobEngine.complete` with `(session, job, result)` and no engine, and the
# kernel's handler signature may not grow a parameter for one job type. Neither
# function needs a signing key -- between them they write one row and two audit
# appends -- so requiring an engine would mean constructing a signer that never
# signs. `JobEngine` keeps thin delegating methods for callers that hold one.


async def record_cancel_request(
    session: AsyncSession,
    job: Job,
    *,
    requested_by: UUID | None,
    cancel_job_id: UUID,
) -> None:
    """A human asked for a job ALREADY ON A DEVICE to stop. Status unchanged.

    THE STATUS IS NOT TOUCHED, AND THAT IS THE POINT (design §3.2). The job is
    still dispatched, or still running, and it stays so until the endpoint says
    otherwise. Writing `cancelled` here -- or inventing a `cancelling` state to
    sit in front of it -- would be the platform asserting something only the
    endpoint can know, which is the exact defect class ("Cancelled" when it
    means "asked") this design exists to make unwritable.

    `mechanism` IS THE LOAD-BEARING AUDIT FIELD. `withdrawn` says the platform
    stopped this itself; `agent_asked` says it merely passed the request on,
    and names the signed job it passed it on with. In the chain those two must
    never read alike, because one is an act and the other is a request.
    """
    job.cancel_requested_at = datetime.now(UTC)
    job.cancel_requested_by = requested_by
    job.cancel_outcome = "requested"
    session.add(job)
    await audit_service.record(
        session,
        org_id=job.org_id,
        actor_type="user",
        actor_id=requested_by,
        action="jobs.cancel_requested",
        target_type="job",
        target_id=str(job.id),
        metadata={
            "type": job.type,
            "device": str(job.target_id),
            "from_status": job.status.value,
            "mechanism": "agent_asked",
            "cancel_job_id": str(cancel_job_id),
        },
    )
    await session.flush()


def scrub_terminal_params(job: Job) -> None:
    """Apply the job type's `terminal_params_scrubber`, if it declares one.

    **CALLED AT EVERY TERMINAL WRITER IN THIS ENGINE** — `complete`, `refuse`,
    `withdraw`, `expire_due` and this module's `record_cancelled` — because a
    credential that must not survive `succeeded` must equally not survive
    `failed`, `refused`, `expired` or `cancelled`. A scrubber hooked to only one
    of them would leave the plaintext in a `jobs.params` row for the retention
    period through the others, which is precisely the exposure `agent.push_install`
    exists to bound (spec §6.4). Centralised here so a future terminal path that
    forgets it is the failure this function's placement exists to prevent.

    Reassigns `job.params`, never mutates it, so SQLAlchemy sees the JSONB
    change. Deny by default: a type with no scrubber is untouched.

    An `UnknownJobType` (a row of a type since retired) is swallowed rather than
    raised: scrubbing a type the registry no longer knows is a no-op, and a
    terminal transition must never fail because a scrubber lookup missed.
    """
    try:
        scrubber = get_job_type(job.type).terminal_params_scrubber
    except UnknownJobType:
        return
    if scrubber is None:
        return
    job.params = scrubber(dict(job.params or {}))


async def record_cancelled(
    session: AsyncSession,
    job: Job,
    *,
    outcome: str,
    actor_type: Literal["system", "agent"],
    result: dict[str, Any] | None = None,
) -> None:
    """Write the terminal `cancelled` state, wherever the evidence came from.

    `_transition` REFUSES ANY SOURCE STATE THAT IS NOT ONE OF THE THREE the
    design allows, which is what keeps "cancelled means somebody has evidence
    it stopped" true as the platform grows callers.

    `actor_type` IS NOT COSMETIC: `"system"` means the server withdrew the job
    on its own authority; `"agent"` means an endpoint reported. An investigator
    reading the chain must be able to tell those apart without knowing which
    code path wrote the row.
    """
    _transition(job, JobStatus.cancelled)
    now = datetime.now(UTC)
    job.cancel_outcome = outcome
    job.finished_at = now
    if result is not None:
        # Stored VERBATIM. The job's OUTCOME is being reinterpreted; what the
        # endpoint actually said is evidence, and evidence is not ours to edit.
        job.result = result
    session.add(job)
    await audit_service.record(
        session,
        org_id=job.org_id,
        actor_type=actor_type,
        actor_id=None,
        action="jobs.cancelled",
        target_type="job",
        target_id=str(job.id),
        metadata={
            "type": job.type,
            "device": str(job.target_id),
            "cancel_outcome": outcome,
        },
    )
    await settle_action_for_job(
        session, org_id=job.org_id, job_id=job.id, terminal_state="cancelled"
    )
    scrub_terminal_params(job)
    await session.flush()
