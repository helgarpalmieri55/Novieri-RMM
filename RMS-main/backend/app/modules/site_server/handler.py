"""The `site.wake` job type and its REJECTING result handler (spec §5.1, §5.3).

**THE AGENT'S REPORT IS UNTRUSTED INPUT.** This handler runs inside the job's
tenant transaction (`JobEngine.complete`'s module extension point) and compares
the reported `broadcast` against the one the SERVER signed into `job.params`. A
mismatch raises `SiteWakeRejected`, which flips the job to `failed`. A wake that
went somewhere other than the address the control plane authorized is not a
successful wake, whatever the agent says about it, and the check costs one
string comparison.

**THE COMPARISON IS AGAINST THE JOB'S OWN SIGNED PARAMS, NEVER AGAINST THE
CURRENT REGISTRY.** Re-reading `site_server.segments` here would let a segment
edited *after* dispatch retroactively bless a broadcast that was out of scope
when it was sent — the same rule IPAM's §6.4 applies to `params.targets`.

**THIS JOB TYPE IS REGISTERED IN THREE PROCESSES AND THAT IS NOT OPTIONAL.**

* **API** — `site_server.register(app)`, via `app/modules/__init__.py`. Both
  dispatch sites live here (this module's wake route and provisioning's
  run-start), and `JobEngine.create` refuses an unregistered type.
* **GATEWAY** — `app/gateway/main.py::register_module_job_types`. Results
  complete here (`app/gateway/dispatch.py` calls `JobEngine.complete`, which
  looks the handler up by name), and the gateway never imports
  `app.modules.register_all`. A handler registered only in `register(app)` is
  DEAD IN PRODUCTION while every in-process test passes — the exact hole
  `register_module_job_types`' own docstring records IPAM falling into.
* **WORKER** — `app/worker.py::register_worker_job_types`. **NOT because a
  worker path creates a `site.wake`** — none does in v1; the only two creators
  are in the API process. It is there because that function is what three
  DENY-BY-DEFAULT PARTITIONS use as their definition of "the live job-type
  registry" (`tests/test_direct_dispatch.py`,
  `tests/test_dispatch_permissions.py`, `tests/test_approved_actions_
  destructive.py`, each of which calls it and then asserts its map covers
  exactly what is registered). A job type absent there cannot be named in those
  maps — while the process-global registry means an earlier test importing this
  module makes it appear anyway, so the partition would hold or fail depending
  on test ORDERING.

**THIS PARAGRAPH WAS WRONG AND NOTHING NOTICED.** It used to deny the third
registration outright, while `app/worker.py` performed it and carried a comment
explaining why it must. Two files, one fact, in contradiction for as long as
nobody read both — because neither sentence was load-bearing on anything that
ran. So all three registrations are now ASSERTED, one test per process, in
`tests/test_site_server_jobtype.py`, the same shape as this module's other
structural guards. A described fact drifts; an asserted one goes red.
"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.registry import JobType, register
from app.models import Job
from app.modules.site_server.errors import SiteWakeRejected
from app.modules.site_server.schema import SiteWakeParams, SiteWakeResult

__all__ = ["WAKE_TTL_S", "handle_wake_result", "register_job_types"]

#: §0.10. NOT the kernel's 3600s default.
#:
#: A job for an offline device stays `queued` and is drained when that device
#: next connects (`GatewayServer._drain_queued`). A site server that reconnects
#: at 04:00 would then broadcast a wake nobody wants any more, an hour after the
#: maintenance window it belonged to. A LATE WAKE IS A WRONG WAKE. The operator
#: sees `expired` and retries, which is the correct failure. A module constant
#: rather than a literal at the call site, exactly as `SWEEP_TTL_S` is, so the
#: dispatcher and the test cannot drift apart.
WAKE_TTL_S = 300


async def handle_wake_result(session: AsyncSession, job: Job, result: dict[str, Any]) -> None:
    """Check the agent's report against the signed params, or reject it whole.

    Writes nothing on success. There is no state to record: `sent` is not
    evidence that anything woke (§0.9), and the job's own `succeeded` status
    plus the kernel's `jobs.succeeded` audit row already say everything true
    that can be said. A `site_wake_run` table would be a log of what the agent
    claimed, presented as a log of what happened.

    Raising here fails the job. `JobEngine.complete` CATCHES the exception
    rather than rolling the transaction back, so nothing this handler wrote
    before a raise would be lost — there is nothing.
    """
    try:
        reported = SiteWakeResult(**result)
    except ValidationError as exc:
        raise SiteWakeRejected(  # i18n-exempt: stored as the job's failure reason
            f"malformed site.wake result for job {job.id}: {exc}"
        ) from None

    signed = SiteWakeParams(**(job.params or {}))
    if reported.broadcast != signed.broadcast:
        raise SiteWakeRejected(  # i18n-exempt: stored as the job's failure reason
            f"site.wake result for job {job.id} reports broadcast "
            f"{reported.broadcast!r} but the signed params authorized "
            f"{signed.broadcast!r}: a packet sent somewhere else is not this wake"
        )


def register_job_types() -> None:
    """One job type, one params model, one result handler.

    Called from `site_server.register(app)` (API process) and from
    `app/gateway/main.py::register_module_job_types` (gateway process, where
    results actually complete). `register()` is an idempotent dict assignment,
    so calling it from both is always safe.
    """
    register(JobType("site.wake", SiteWakeParams, result_handler=handle_wake_result))
