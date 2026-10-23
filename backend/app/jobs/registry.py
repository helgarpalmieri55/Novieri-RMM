"""Job-type registry: the kernel's extension point for modules.

A module registers its job types (name + params schema) here and ships the
matching agent capability. The engine refuses to create a job whose type is
not registered — a typo'd or retired job type can never reach an agent.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Job

# Invoked by JobEngine.complete() after a succeeded result, inside the job's
# tenant transaction. A raising handler fails the job (see engine).
ResultHandler = Callable[["AsyncSession", "Job", dict[str, Any]], Awaitable[None]]

# Invoked by JobEngine.resign_for_retry() AFTER the job's new `expires_at` is
# set and BEFORE the job is re-signed, inside the sweep's tenant transaction.
#
# WHY THIS EXTENSION POINT EXISTS. A retry gets a fresh nonce and a fresh
# expiry, and re-signs the SAME params. That is correct for params that are
# pure instructions, and wrong for params that carry something time-bounded:
# `app.deploy` carries a presigned artifact URL whose lifetime is the job's own
# window, so attempt 2 — dispatched at least `STUCK_DISPATCH_AFTER` later —
# would re-sign a URL that is already dead and the deployment would fail for
# every remaining attempt with no way to tell why. A refresher returns the
# params to sign instead (or None to keep them unchanged).
#
# It must NOT raise for a business reason. It runs inside the org's job-expiry
# sweep, so raising costs that org its whole cycle; a refresher that cannot mint
# fresh material returns params the AGENT will refuse, which lands the job on a
# terminal state instead of wedging the sweep.
ParamsRefresher = Callable[["AsyncSession", "Job"], Awaitable[dict[str, Any] | None]]

# Applied to `jobs.params` before they reach an API response. Pure and
# synchronous: it maps a params dict to a safe-to-read params dict.
#
# Signed params are not automatically safe to hand back to an operator. Most are
# instructions, but a job type may sign a BEARER CREDENTIAL into them — a
# presigned object-store URL, a scoped write grant — and `GET /jobs/{id}` is
# gated on `jobs.read`, which is a far weaker permission than the one that
# authorized the dispatch. A job type that signs bearer material declares a
# redactor; `app/api/v1/jobs.py` is the single read path that applies it.
ParamsRedactor = Callable[[dict[str, Any]], dict[str, Any]]

# Applied to `jobs.params` IN PLACE OF the stored value the instant a job of
# this type reaches ANY terminal state — succeeded, failed, refused, expired or
# cancelled. Pure and synchronous: it maps the stored params to the params the
# terminated row should keep. The engine reassigns `job.params` to the result,
# never mutating the JSONB under SQLAlchemy.
#
# WHY THIS EXTENSION POINT EXISTS, and why it is not the same thing as a
# redactor. A redactor hides bearer material on the way OUT of the API; the row
# is untouched and the plaintext lives for the retention period. That is right
# for a presigned URL whose window already lapsed. It is NOT enough for a
# decrypted administrative credential (`agent.push_install`, spec §6.4): signing
# needs canonical bytes and this platform's canonical bytes are the stored
# params, so the plaintext IS in the database. The durable fix is vault
# indirection for job params — a kernel change nobody has scoped. Until it
# exists, a job type that signs a decrypted credential declares a scrubber, and
# the plaintext's life in the database is bounded by the job's TTL rather than
# its retention. This is a RESIDUAL, stated plainly (spec §6.4), not a solution.
#
# DENY BY DEFAULT: a type with no scrubber keeps its params, exactly as today.
# The engine applies it at every terminal writer, so a future terminal path that
# forgets it is the failure this centralisation exists to prevent.
TerminalParamsScrubber = Callable[[dict[str, Any]], dict[str, Any]]

# Invoked by `app.jobs.retry.retry_job` with `(session, old_job, new_job)`,
# inside the retry route's tenant transaction, AFTER the new job row exists and
# BEFORE anything is published.
#
# WHY THIS EXTENSION POINT EXISTS, and why declaring it is what makes a type
# retryable at all (2026-08-22-job-lifecycle-design.md §4.4). A retry is a NEW
# job row with a NEW id, and every module's record of what it asked for is
# keyed on `jobs.id`: `ingest_deploy_result` finds its row with
# `DeploymentResult.job_id == job.id` and, finding none, "is skipped gracefully
# rather than raising". So a kernel retry of a failed `app.deploy` with no
# relinker would install the package on the endpoint and leave the deployment
# showing pending FOREVER -- the work happens, the record never learns of it,
# and nothing anywhere goes red. The bypass is not a missing permission; it is
# a missing record, exactly as `direct_dispatch.py` says of its own allowlist.
#
# A relinker repoints that record at the new job. It runs in the caller's
# transaction, so the new job row and the module's repointed record commit
# together or not at all. It MUST NOT raise for a business reason: raising
# fails the whole retry, which is the correct outcome for "this record cannot
# be repointed" and a poor one for anything else.
RetryRelinker = Callable[["AsyncSession", "Job", "Job"], Awaitable[None]]


class UnknownJobType(Exception):
    pass


class HelloParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = "ping"


class InventoryCollectParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LogsShipParams(BaseModel):
    """Dispatching logs.ship tells the agent to flush its security-event
    buffer now; the scheduled cadence comes from the org's policy."""

    model_config = ConfigDict(extra="forbid")


class AgentCertRenewParams(BaseModel):
    """`agent.cert.renew` — server → agent, leg 1 of certificate rotation
    (docs/modules/agent/cert-renewal.md §0.3).

    Carries NOTHING. The agent needs no instruction beyond "produce a CSR": the
    keypair is generated on the endpoint and the private half never leaves it,
    exactly as at enrollment. Everything about *who the certificate is for* is
    the server's own knowledge (`jobs.target_id`, `jobs.org_id`) and is never
    read from anything the agent sends — §0.5.
    """

    model_config = ConfigDict(extra="forbid")


class AgentCertRenewResult(BaseModel):
    """agent → server: the CSR over the freshly generated device key.

    The public half plus a proof of possession, and nothing else. The CSR's own
    subject is ignored by the CA, which overwrites CN and O from the job row.
    """

    model_config = ConfigDict(extra="forbid")
    csr_pem: str


class AgentCertInstallParams(BaseModel):
    """`agent.cert.install` — server → agent, leg 2 of certificate rotation.

    Carries the issued certificate, its chain, and the serial the CA reported.

    `cert_serial` is on the wire even though the AGENT IGNORES IT. It is here so
    the control plane records exactly the serial string its own CA gave back:
    `OpenBaoPkiCA` returns Vault's colon-separated form while re-deriving it
    from the DER would produce bare hex, and `enrollment.revoke_agent` feeds
    `agents.cert_serial` straight back to `CAService.revoke`. Re-deriving it
    would leave revocation working before a rotation and broken after one, on
    OpenBao deployments only — the kind of defect that surfaces in production
    and in no test.
    """

    model_config = ConfigDict(extra="forbid")
    cert_pem: str
    chain_pem: str
    cert_serial: str


class AgentCertInstallResult(BaseModel):
    """agent → server: confirmation that the endpoint swapped its identity.

    `cert_fingerprint` is checked against the fingerprint of the certificate the
    SERVER issued (`confirm_installed_cert`) — the agent's claim is never taken
    as the new identity, only as an acknowledgement of it. `not_after` is the
    agent's own reading of the leaf and is recorded, not trusted.
    """

    model_config = ConfigDict(extra="forbid")
    cert_fingerprint: str
    not_after: str


class JobCancelParams(BaseModel):
    """`job.cancel` -- server -> agent, cycle 2 of the job-lifecycle design.

    **A CANCEL THAT REACHES AN ENDPOINT IS A JOB, AND TAKES THE JOB PATH.**
    Invariant #1 has no "it is only a cancel" exemption: a mechanism that makes
    an endpoint stop what it is doing is a command to that endpoint, so it is
    signed over canonical bytes with the platform's job-signing key, verified
    by the agent against the key pinned at enrollment, bound to that device,
    nonce-checked and freshness-checked, exactly like every other job. A forged
    cancel would otherwise be a denial-of-service primitive against the whole
    fleet -- one unsigned frame per running job and the fleet stops working.

    **IT IS NOT A NEW WIRE `kind`, AND THAT IS THE COMPATIBILITY DECISION**
    (design §3.1). An unknown inbound `kind` is dropped by a deployed agent in
    SILENCE -- `wire.Decode` returns `ErrKind`, the runner logs locally and
    answers nothing -- which is the worst possible property for a verb whose
    whole difficulty is knowing what the endpoint did. An unknown job TYPE is
    refused `unknown_type` loudly and on the record, before anything runs. So
    an agent that predates this change answers a cancel in seconds with a
    `job_result` the control plane reads as "this endpoint cannot be
    interrupted", and the target job runs or expires exactly as it would have.

    `target_job_id` IS THE ONLY FIELD, AND IT IS THE SERVER'S OWN. The target
    is read back out of THESE SIGNED PARAMS when the result arrives -- never
    out of the agent's result body -- so a compromised endpoint cannot name a
    different job to close.
    """

    model_config = ConfigDict(extra="forbid")
    target_job_id: str


class JobCancelResult(BaseModel):
    """agent -> server: what the endpoint did with the job it was told to stop.

    `JobResultMsg.state` for a `job.cancel` is `succeeded` whenever the agent
    CARRIED OUT ITS INSTRUCTION -- including `unknown`, which is a true and
    useful answer ("this agent has never seen that job"). The job succeeded;
    the outcome says what it found.

    NO WIRE ENUM IS WIDENED BY THIS TYPE. `JobResultMsg.state` is untouched:
    adding `"cancelled"` to it would be safe for a new server reading an old
    agent and fatal in the direction that actually bites -- a NEWER agent
    against an older control plane, where `parse_agent_message` drops the whole
    message and the job hangs to expiry with only a `logger.warning`
    (design §3.1).

    `dropped_before_start` IS ACCEPTED AND IS NOT PRODUCED BY THIS PROJECT'S
    AGENT. See `docs/kernel/wire-protocol/README.md`: the agent's two pieces of
    cancellation state (design §3.8) cannot distinguish "queued in the
    dispatcher" from "never seen", so it answers `unknown` and the suppressed
    target reports for itself. The value stays in the schema because the server
    must keep accepting it from an agent that CAN tell the difference -- that
    is the additive direction, and narrowing an enum an agent may already send
    is a break.
    """

    model_config = ConfigDict(extra="forbid")
    target_job_id: str
    outcome: Literal[
        "interrupted", "dropped_before_start", "already_finished", "unknown"
    ]


class AgentUninstallParams(BaseModel):
    """`agent.uninstall` -- server -> agent (install-link spec 2026-09-28 §1.5).

    CARRIES NOTHING, AND THAT IS THE DESIGN. What to remove is the agent's own
    knowledge -- its service, its binary, its data directory -- so there is no
    field an operator or a compromised control plane could point at a path.
    `extra="forbid"`: a member added here is a second input to a command path
    that deletes files, and needs a review that knows it.
    """

    model_config = ConfigDict(extra="forbid")


class AgentUninstallResult(BaseModel):
    """agent -> server: the removal has been SCHEDULED, not done.

    A process cannot delete its own running service and then report, so the
    capability starts a detached helper that waits, stops the service and
    removes everything, and answers before it does (plan D7). `succeeded` on
    this job is what `device_review.complete_due_rejections` reads as "the
    agent confirmed".

    `extra="ignore"`, NOT `forbid`: this is an agent -> server message, and a
    fleet is always mixed-version -- an agent newer than this control plane
    may report more, and refusing its whole result would leave the rejection
    waiting out the 24-hour timeout. The `AgentUpdateResult` rule
    (`app/modules/agent_update/jobtypes.py`). The PARAMS stay `forbid`: that
    direction is server -> agent, into a path that deletes files.
    """

    model_config = ConfigDict(extra="ignore")
    scheduled: bool


async def _handle_job_cancel_result(
    session: "AsyncSession", job: "Job", result: dict[str, Any]
) -> None:
    """Thin trampoline into `app.jobs.cancel`.

    Imported at CALL time for the same reason `_confirm_installed_cert` is:
    `app.jobs.cancel` imports `app.jobs.engine`, which imports this module, so
    a top-level import would be a cycle.
    """
    from app.jobs.cancel import handle_cancel_result

    await handle_cancel_result(session, job, result)


async def _confirm_installed_cert(
    session: "AsyncSession", job: "Job", result: dict[str, Any]
) -> None:
    """Thin trampoline into `app.services.cert_renewal`.

    Imported at CALL time, not module import time: this module is imported by
    `app.jobs.engine`, and the service imports `app.models` (which pulls in
    every module's models). A top-level import here would make the kernel's job
    registry depend on the whole model graph.
    """
    from app.services.cert_renewal import confirm_installed_cert

    await confirm_installed_cert(session, job, result)


@dataclass(frozen=True)
class JobType:
    name: str
    params_model: type[BaseModel]
    result_handler: ResultHandler | None = None
    params_refresher: ParamsRefresher | None = None
    params_redactor: ParamsRedactor | None = None
    #: May a job of this type be INTERRUPTED once it has reached the endpoint?
    #:
    #: DENY BY DEFAULT, and it gates ONLY the `dispatched`/`running` path
    #: (2026-08-22-job-lifecycle-design.md §9.1, the operator's option (b)).
    #: Withdrawal -- a `queued` job that was never written to a socket -- is
    #: safe for every type whatever this says, because nothing ran.
    #:
    #: Interruption is not safe by default and the reason is not squeamishness:
    #: a `patch.apply` stopped mid-install leaves a half-patched machine and a
    #: `backup.restore` stopped mid-write leaves a half-written volume, either
    #: of which is worse than the job somebody wanted to stop. A type claims
    #: this flag only when its AGENT capability already honours `ctx` -- the
    #: flag is a statement about the endpoint's code, so setting it for a
    #: capability that checks no context (`inventory.collect`, `provision`)
    #: would be a claim the binary does not honour and the operator would read
    #: "Cancel requested" forever.
    cancellable: bool = False
    #: Overwrites the credential/bearer material in `jobs.params` the instant a
    #: job of this type reaches any terminal state (spec §6.4). Deny by default:
    #: a type with no scrubber keeps its params unchanged, exactly as today. See
    #: `TerminalParamsScrubber`.
    terminal_params_scrubber: TerminalParamsScrubber | None = None
    #: Repoints this module's own record at the new job when one of its jobs is
    #: retried, and DECLARING IT IS WHAT MAKES THE TYPE RETRYABLE through the
    #: kernel route (design §4.4). Deny by default: a module-governed type with
    #: no relinker is refused with a 403 naming the module's own re-run route,
    #: because a retry that orphans the module's record installs software and
    #: leaves the deployment showing pending forever.
    retry_relinker: RetryRelinker | None = None


_registry: dict[str, JobType] = {}


def register(job_type: JobType) -> None:
    _registry[job_type.name] = job_type


def get_job_type(name: str) -> JobType:
    try:
        return _registry[name]
    except KeyError:
        raise UnknownJobType(name) from None


def registered_names() -> list[str]:
    return sorted(_registry)


# Phase 0 kernel job types
register(JobType("hello", HelloParams))
register(JobType("inventory.collect", InventoryCollectParams))
# Phase 2: security-log flush trigger
register(JobType("logs.ship", LogsShipParams))
# Automatic certificate renewal (docs/modules/agent/cert-renewal.md). Two legs,
# both ordinary signed jobs — invariant #1 gets no exemption for the job type
# whose subject is the agent's own identity.
register(JobType("agent.cert.renew", AgentCertRenewParams))
register(JobType("agent.cert.install", AgentCertInstallParams, _confirm_installed_cert))
# Interruption (docs/superpowers/specs/2026-08-22-job-lifecycle-design.md §3.1,
# cycle 2). A cancel that reaches an endpoint is an ORDINARY SIGNED JOB -- same
# signature over the same canonical bytes, same verification on the endpoint,
# same audit chain. There is no second command path and invariant #1 gets no
# exemption for the verb whose subject is another job.
#
# NOT `cancellable` ITSELF. Cancelling a cancel is a request the platform
# cannot act on coherently, and `NO_HUMAN_DISPATCH_PATH` already makes it
# unreachable from any route.
register(JobType("job.cancel", JobCancelParams, _handle_job_cancel_result))

# Reject (install-link spec 2026-09-28 §1.5, §4). An ORDINARY SIGNED JOB, like
# the certificate legs above: invariant #1 has no "it is only an uninstall"
# exemption. Created only by `app/services/device_review.py::reject`, only for
# an agent that advertises the capability (1.6.0+), and admitted by the review
# gate for a `pending` or `rejected` device -- the one type a rejected machine
# may still receive.
register(JobType("agent.uninstall", AgentUninstallParams))
