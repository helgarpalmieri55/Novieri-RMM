from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AuthContext, require_permission
from app.core.i18n import tr, tr_marked
from app.core.tenancy import org_session
from app.gateway.dispatch import publish_job
from app.jobs.attestation import SIGNATURE_ALGORITHM, signature_digest
from app.jobs.cancel import (
    CancelConflict,
    CancelForbidden,
    JobNotFound,
    request_cancellation,
)
from app.jobs.direct_dispatch import module_governed_reason
from app.jobs.registry import UnknownJobType, get_job_type
from app.jobs.retry import (
    RetryConflict,
    RetryForbidden,
    RetryNotFound,
    retry_job,
)
from app.models import Device, Job, JobLog

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobCreateRequest(BaseModel):
    type: str
    device_id: UUID
    params: dict[str, Any] = Field(default_factory=dict)


class JobLogOut(BaseModel):
    ts: datetime
    level: str
    message: str
    artifact_ref: str | None


class JobOut(BaseModel):
    """One job as a `jobs.read` holder may see it.

    THE THREE SIGNATURE FIELDS ARE PROOF, NOT MATERIAL (finding B43). They say
    that a server signature is on record, which algorithm verifies it, and
    give it a stable one-way name; the signature and the nonce themselves stay
    inside the process, because the envelope an agent acts on is
    `payload + sig` and `jobs.read` is a far weaker permission than the one
    that authorized the dispatch. The full reasoning, and the key-id gap this
    does not paper over, are in `app/jobs/attestation.py`.
    """

    id: UUID
    type: str
    device_id: UUID
    status: str
    attempts: int
    params: dict[str, Any]
    result: dict[str, Any] | None
    issued_at: datetime
    expires_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    #: A server signature is on record for this job.
    signed: bool
    #: The algorithm that verifies it — `None` when nothing is on record.
    signature_algorithm: str | None
    #: SHA-256 of the signature. A name for it, never the signature.
    signature_digest: str | None
    #: When a human asked for this job to stop, or `None`. The CONSOLE NEEDS
    #: THIS TO TELL THE TRUTH: from cycle 2 on, a job can carry a cancel
    #: request and still be running, and "asked at 14:02, not yet confirmed"
    #: is a different sentence from "cancelled".
    cancel_requested_at: datetime | None = None
    #: What became of that request: `requested` (asked, the endpoint has not
    #: answered), `withdrawn` (never sent to the device), `dropped` (the
    #: endpoint discarded it before starting), `interrupted` (stopped
    #: mid-flight), `too_late` (it had already finished), `unsupported` (this
    #: endpoint's agent is too old to be interrupted).
    #:
    #: DELIBERATELY A BARE STRING rather than an enum on the wire. A console
    #: pinned to today's set would reject tomorrow's response rather than
    #: render it, and `jobState.ts` is written to show the bare fact for a
    #: value it does not recognise instead of inventing a story for it.
    cancel_outcome: str | None = None
    #: The job this one re-runs, or `None`. A retry is a NEW job -- new id, new
    #: nonce, a signature over different bytes -- and this is the only link
    #: back. The console needs it to say "retry of ..." rather than showing two
    #: unrelated rows; it is a JOB id, not a user id, so it discloses nothing
    #: `jobs.read` does not already cover.
    retry_of_job_id: UUID | None = None
    logs: list[JobLogOut] | None = None

    # `cancel_requested_by` IS ABSENT AND STAYS ABSENT. The actor lives in the
    # hash-chained audit log (`jobs.cancel_requested`), which is where an
    # investigator looks and where it cannot be edited. `jobs.read` is a far
    # weaker permission than the one that authorized the cancel, and this
    # model has never disclosed a user id.


class JobListResponse(BaseModel):
    """One page of jobs, and how many matched before it was cut.

    `total` counts the filtered set, not the page: the console's KPI tile and
    its table read the same response, and a total that agreed with a truncated
    page would hide exactly the backlog the tile exists to show.
    """

    items: list[JobOut]
    total: int


def _readable_params(job: Job) -> dict[str, Any]:
    """`jobs.params` as a `jobs.read` holder may see them.

    Signed params are not automatically safe to echo. A job type that signs a
    BEARER CREDENTIAL into its params — `app.deploy`'s presigned artifact URL,
    a scoped object-store write grant — declares a `params_redactor`, and this
    is the single place it is applied. `jobs.read` is a far weaker permission
    than the one that authorized the dispatch, so a route that returned the
    raw params would hand every reader a live grant.

    FAIL CLOSED ON AN UNREGISTERED TYPE. `create` refuses an unknown type, so
    the only way to get one here is a job type that was RETIRED after its jobs
    were written — exactly the case where nobody is left to say whether its
    params are safe to show.
    """
    try:
        job_type = get_job_type(job.type)
    except UnknownJobType:
        return {}
    if job_type.params_redactor is None:
        return job.params
    return job_type.params_redactor(job.params)


def _job_out(job: Job, logs: list[JobLog] | None = None) -> JobOut:
    return JobOut(
        id=job.id,
        type=job.type,
        device_id=job.target_id,
        status=job.status.value,
        attempts=job.attempts,
        params=_readable_params(job),
        result=job.result,
        issued_at=job.issued_at,
        expires_at=job.expires_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        signed=job.signature is not None,
        signature_algorithm=SIGNATURE_ALGORITHM if job.signature else None,
        signature_digest=signature_digest(job.signature),
        cancel_requested_at=job.cancel_requested_at,
        cancel_outcome=job.cancel_outcome,
        retry_of_job_id=job.retry_of_job_id,
        logs=(
            [
                JobLogOut(
                    ts=entry.ts,
                    level=entry.level,
                    message=entry.message,
                    artifact_ref=entry.artifact_ref,
                )
                for entry in logs
            ]
            if logs is not None
            else None
        ),
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=JobOut)
async def dispatch_job(
    body: JobCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("jobs.dispatch")),
) -> JobOut:
    """Create and publish a signed job for one device.

    `jobs.dispatch` IS NOT A LICENCE TO REACH EVERY REGISTERED JOB TYPE
    (`app/jobs/direct_dispatch.py`). This route takes a type and a params blob
    from an operator, so a module-governed type created here would reach the
    agent's capability handler while skipping the authorization, the record and
    the bounds that live on the module's own endpoint -- a `net.sweep` with no
    `ipam_scan_authorization` behind it, a `script.run` with a body nothing can
    identify afterwards. Those types are refused with a 403 naming the route
    that owns them; the deny-by-default allowlist is what keeps a newly
    registered job type from arriving here by accident.

    ORDERED SO THE TWO REFUSALS STAY DISTINGUISHABLE: an unregistered type is a
    422 about the request, a governed one is a 403 about the route.
    """
    try:
        get_job_type(body.type)
    except UnknownJobType:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            tr("unknown job type {job_type}", job_type=repr(body.type)),
        ) from None
    governed = module_governed_reason(body.type)
    if governed is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, tr_marked(governed))

    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        device = (
            await session.execute(select(Device).where(Device.id == body.device_id))
        ).scalar_one_or_none()
        if device is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such device"))
        try:
            job = await state.job_engine.create(
                session,
                org_id=ctx.org_id,
                type=body.type,
                target_device_id=body.device_id,
                params=body.params,
                requested_by=ctx.user_id,
            )
        except UnknownJobType:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                tr("unknown job type {job_type}", job_type=repr(body.type)),
            ) from None
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        envelope = await state.job_engine.signed_envelope(job)
        out = _job_out(job)
    await publish_job(state.redis, body.device_id, envelope)
    return out


@router.get("", response_model=JobListResponse)
async def list_jobs(
    request: Request,
    ctx: AuthContext = Depends(require_permission("jobs.read")),
    device_id: UUID | None = Query(default=None),
    job_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    order: Literal["newest", "oldest"] = Query(default="newest"),
) -> JobListResponse:
    """One page of this org's jobs, in a stated order (finding B46).

    `order` EXISTS SO A SORT CANNOT LIE ABOUT A TRUNCATED PAGE. The console
    took the default `limit` and showed the true total beside it, so sorting
    the rows it held by "oldest first" would have re-sorted the newest hundred
    and labelled the result oldest. The ordering has to happen where the
    LIMIT does.

    THE KEY IS `id`, AND `issued_at` IS DISQUALIFIED RATHER THAN OVERLOOKED.
    `jobs.id` is a uuid7 — issue-ordered, unique, never rewritten — so it is a
    total order needing no tiebreak, and `newest` is byte-for-byte the
    ordering this route has always had. `issued_at` is restamped by
    `JobEngine.resign_for_retry` on every retry, and a sort key that moves
    under an offset page makes that page show a row twice or skip one.

    An unrecognised value is a 422 rather than a quiet fall back to the
    default: a caller told "oldest" and handed "newest" with a 200 would
    reproduce the deception in a different place.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        query = select(Job)
        if device_id:
            query = query.where(Job.target_id == device_id)
        if job_status:
            query = query.where(Job.status == job_status)
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar_one()
        sequence = Job.id.asc() if order == "oldest" else Job.id.desc()
        rows = (
            (await session.execute(query.order_by(sequence).limit(limit).offset(offset)))
            .scalars()
            .all()
        )
        return JobListResponse(items=[_job_out(j) for j in rows], total=total)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("jobs.read")),
    include: str | None = Query(default=None),
) -> JobOut:
    """One job, with `?include=logs` for its log lines.

    Carries the same signature attestation as the list — `signed`, the
    algorithm and the digest, never the signature (see `JobOut`). This is the
    response the console's job detail panel renders, and B43's complaint was
    that it had nothing about a signature to render.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        job = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one_or_none()
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, tr("no such job"))
        logs: list[JobLog] | None = None
        if include == "logs":
            logs = list(
                (
                    await session.execute(
                        select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.id)
                    )
                )
                .scalars()
                .all()
            )
        return _job_out(job, logs)


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("jobs.read")),
) -> JobOut:
    """Stop a job: withdraw it, or ask its endpoint to interrupt it.

    Cycles 1 and 2 of
    `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md`. The engine had
    no lifecycle verbs at all: a job queued for a machine that has been off for
    a week could not be taken back (finding B41), and one already running on
    the wrong device could not be stopped. Everything this route cannot do is
    refused with a 409 that names the state -- never a silent 200, because a
    no-op reported as a success is the defect class this programme has spent
    its cycles closing.

    **WHAT REACHES AN ENDPOINT, AND UNDER WHAT SIGNATURE.** A `queued` job is
    withdrawn server-side and nothing is sent anywhere. A `dispatched` or
    `running` job of a type that declares itself `cancellable` gets a **signed
    `job.cancel` job** -- an ordinary envelope, signed over its canonical bytes
    by the same signer as every other job, which the agent verifies against the
    key pinned at enrollment before acting on it (invariant 1: there is no
    unsigned command path, and a cancel is a command). It is PUBLISHED after
    this transaction commits and travels down the connection the agent itself
    opened; the server dials no endpoint (invariant 2).

    **THE STATUS DOES NOT CHANGE ON THE INTERRUPTION PATH.** The response still
    says `dispatched` or `running`, with `cancel_requested_at` set. That is the
    truth: the platform has asked, and only the endpoint can say what happened.

    **`jobs.read` IS THE FLOOR, NOT THE GATE, AND THE DIFFERENCE IS THE WHOLE
    AUTHORIZATION DECISION.** The gate is `dispatch_permission_for(job.type)`
    -- the permission the job type's OWN dispatch route requires -- checked
    server-side in `app/jobs/cancel.py`, which is where it belongs because it
    cannot be known until the job is loaded and its type read. `jobs.read` sits
    on the dependency because acting on a job you may not see is not a
    capability anybody asked for, and because a route reporting no permission
    code at all is invisible to `route_permission` and therefore to
    `tests/test_route_gating.py`. Read this as "at minimum `jobs.read`, and
    then the type's own dispatch grant"; a caller holding only `jobs.read` gets
    a 403 from the service, asserted in `tests/test_job_cancel.py`.

    THE THREE REFUSALS ARE DIFFERENT FACTS AND STAY DISTINGUISHABLE: 404 the
    job is not this org's, 403 the caller could not have started this job type,
    409 the job is not in a state that can be withdrawn.
    """
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            outcome = await request_cancellation(
                session,
                engine=state.job_engine,
                org_id=ctx.org_id,
                user_id=ctx.user_id,
                job_id=job_id,
            )
        except JobNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
        except CancelForbidden as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
        except CancelConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        out = _job_out(outcome.job)
        envelope = outcome.envelope
        device_id = outcome.job.target_id
    # AFTER THE COMMIT, like `POST /jobs`. A signed cancel on the wire for a
    # request the database might still roll back would be a command nobody
    # authorized, arriving for a job the record says nobody asked to stop.
    if envelope is not None:
        await publish_job(state.redis, device_id, envelope)
    return out


@router.post("/{job_id}/retry", status_code=status.HTTP_201_CREATED, response_model=JobOut)
async def retry_job_route(
    job_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("jobs.read")),
) -> JobOut:
    """Re-run a finished job as a NEW signed job.

    Cycle 3 of `docs/superpowers/specs/2026-08-22-job-lifecycle-design.md`. An
    operator whose deployment failed at 02:00 for a laptop that was asleep had
    no way to run it again; the only thing in the platform called "retry" was
    the stuck-dispatch sweep, which refuses any job that reached a terminal
    state -- i.e. exactly the case an operator has.

    **THIS IS A DISPATCH, AND IT IS 201 BECAUSE IT CREATES A JOB.** The
    response is the NEW job, with a new id, a new nonce and a signature over
    different canonical bytes. Nothing re-sends the original's stored signed
    bytes -- that would be a replay primitive with a permission check in front
    of it -- and the original row is never written to, so the failure body the
    operator retried FROM survives.

    **`jobs.read` IS THE FLOOR, NOT THE GATE.** The gate is
    `dispatch_permission_for(job.type)`, checked server-side in
    `app/jobs/retry.py`, because it cannot be known until the job is loaded and
    its type read: a holder of `jobs.dispatch` must not be able to re-run a
    `patch.apply` they could never have started. `jobs.read` sits on the
    dependency because acting on a job you may not see is not a capability
    anybody asked for, and because a route reporting no permission code at all
    is invisible to `route_permission` and therefore to
    `tests/test_route_gating.py`.

    THE THREE REFUSALS ARE DIFFERENT FACTS AND STAY DISTINGUISHABLE: 404 the
    job is not this org's; 403 either the caller could not have started this
    job type, or the kernel route cannot re-run it without orphaning the
    module's own record (the message names where it can be re-run); 409 the job
    has not finished, or it succeeded and re-running it is a new dispatch
    rather than a retry.
    """
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            outcome = await retry_job(
                session,
                engine=state.job_engine,
                org_id=ctx.org_id,
                user_id=ctx.user_id,
                job_id=job_id,
            )
        except RetryNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
        except RetryForbidden as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
        except RetryConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        out = _job_out(outcome.job)
        envelope = outcome.envelope
        device_id = outcome.job.target_id
    # AFTER THE COMMIT, like `POST /jobs` and the cancel route. A signed job on
    # the wire for a row the database might still roll back is a command nobody
    # authorized.
    await publish_job(state.redis, device_id, envelope)
    return out
