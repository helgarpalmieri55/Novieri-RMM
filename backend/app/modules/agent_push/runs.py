"""Push runs: a subnet push a person confirmed, continued 50 at a time
(docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §1.3-§1.5;
CLAUDE.md 2a(e), 2a(f) as amended 2026-09-30).

**A PERSON STARTS, PAUSES, RESUMES AND CANCELS. THE PACER ONLY CONTINUES.**
`create_run` writes the confirmed list once and seals it. The database trigger
`push_run_target_fixed` then refuses any further target, and only
`create_run` constructs a `PushRunTarget` (an AST test pins this). The pacer
(`advance_run`, below) never writes `state = 'running'`: only `create_run` and
`resume_run` do, and both are a person's act.

**THIS FILE SIGNS NOTHING AND DECRYPTS NOTHING.** The pacer is handed a
`RunTargetDispatcher` (`run_contract.py`), which `dispatch.RunDispatcher`
implements. `tests/test_agent_push_dispatches_nothing.py` holds this file to
that. Even which version of a secret is current reaches this file only as the
dispatcher's opaque `credential_fingerprint`.

**A RUN IS ONE CLIENT'S** (MSP clients, plan 2026-10-26 Task 12). Its client
is its credential's (`its_push_run_client_guard`, migration 0241), its
targets are that client's machines, and the pacer re-runs the ladder inside
that client before every target. One run may be active per CLIENT (operator
decision D3), each with at most `PUSH_RUN_CONCURRENCY` in flight. The beat
continues each run in a session whose scope is that run's client alone
(`app/worker.py::push_run_tick_all_orgs`), so the database refuses any row of
another; it still never starts, resumes or widens one (2a(e)).

**EVERY QUERY CARRIES AN EXPLICIT `org_id` PREDICATE**, as in the rest of the
package, in addition to RLS.
"""

import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.events import publish as publish_event
from app.core.i18n import N_, tr
from app.core.rbac import codes_held_anywhere, has_permission
from app.models import Agent, Device, Job, JobStatus
from app.modules.agent_push.credential_scope import resolve_push_credential
from app.modules.agent_push.errors import (
    PushConflictError,
    PushError,
    PushListChangedError,
    PushNotFoundError,
    PushPermissionError,
    PushValidationError,
)
from app.modules.agent_push.locks import lock_target_scope, sites_of
from app.modules.agent_push.models import (
    PUSH_RUN_ACTIVE_STATES,
    PUSH_RUN_TARGET_STATES,
    PushRun,
    PushRunTarget,
)
from app.modules.agent_push.net import parse_target_address
from app.modules.agent_push.preflight import TargetDecision, evaluate_targets
from app.modules.agent_push.run_contract import (
    Envelope,
    RunTargetDispatcher,
    RunTargetDispatchError,
)
from app.modules.agent_push.run_list import LIST_REASONS, expand_prefix

__all__ = [
    "PUSH_RUN_AUTH_FAILURE_LIMIT",
    "PUSH_RUN_CONCURRENCY",
    "RUN_REFUSAL_REASONS",
    "RUN_TARGET_LOST_AFTER",
    "SIGNED_IN_OUTCOMES",
    "TICK_INTERVAL_S",
    "advance_run",
    "GuardWindow",
    "auth_window",
    "cancel_run",
    "create_run",
    "get_run",
    "list_runs",
    "list_targets",
    "org_ids_with_live_runs",
    "pause_run",
    "resume_run",
    "run_counts",
    "settle_target_for_job",
]

logger = logging.getLogger(__name__)

#: CLAUDE.md 2a(f) as amended 2026-09-30: at most 50 of a run's targets in
#: flight at once. Changed only by a code change, like `PUSH_BATCH_MAX`.
PUSH_RUN_CONCURRENCY = 50

#: A `running` install still unresolved this long after its job's expiry is
#: lost (R15). The kernel expires only queued and dispatched jobs, and a hung
#: install must not hold one of the fifty slots for ever.
RUN_TARGET_LOST_AFTER = timedelta(minutes=30)

#: The beat's cadence (design §1.4): the backstop that fills freed capacity
#: and settles jobs that ended without a result. Read by `app/worker.py`.
TICK_INTERVAL_S = 60.0

#: THE LOCKOUT GUARD (spec §1.4a, decided by the operator 2026-09-30). A wrong or
#: expired domain password presented to fifty machines at once trips a typical
#: Active Directory lockout policy (5-10 bad attempts) within seconds. So a run
#: sends ONE target first, opens to `PUSH_RUN_CONCURRENCY` only once a sign-in
#: has got through, and stops as `stopped_credential` after this many
#: `auth_failed` results since it last started or resumed. The safety stop is
#: the pacer's one act of its own (CLAUDE.md 2a(e) as amended); only a person's
#: Resume leaves it.
PUSH_RUN_AUTH_FAILURE_LIMIT = 3

#: THE OUTCOMES THAT PROVE THE CREDENTIAL GOT PAST SIGN-IN (R19), read off the
#: agent (`agent/internal/capabilities/pushinstall/pushinstall.go`,
#: `winrm_capability.go`):
#: * `installed` and `os_mismatch` are produced only after an authenticated
#:   session ran on the target (SSH: after `Open`; WinRM: from the install
#:   script's exit code).
#: * `failed` is produced BOTH before sign-in (WinRM: the artefact download,
#:   the script build) and after it, so it counts only when the result carries
#:   `arch_detected`, which the agent learns only from inside a signed-in
#:   session (`_signed_in`).
#: * `unreachable` never counts: it is also the host-key and transport failure,
#:   before any authentication.
#: * `auth_failed` is the failure the guard counts.
SIGNED_IN_OUTCOMES = frozenset({"installed", "os_mismatch"})


def _signed_in(outcome: str, result: dict[str, Any]) -> bool:
    if outcome in SIGNED_IN_OUTCOMES:
        return True
    return outcome == "failed" and bool(str(result.get("arch_detected") or "").strip())


#: The refusals only a run reaches, each with the remedy it implies. They are
#: shown beside the machine's own address, so they are written in plain
#: language.
RUN_REFUSAL_REASONS: dict[str, str] = {
    "run_owner_not_permitted": (
        N_(
            "the person who started this run no longer holds both agent.push.run and "
            "credentials.checkout, so nothing more is pushed on their confirmation. "
            "Start a new run"
        )
    ),
    "credential_missing": (
        N_(
            "the run's credential no longer exists, is no longer a push credential, or is "
            "not a credential of this machine's client. Nothing is presented with a secret "
            "the operator has withdrawn, nor to another client's machine. Start a new run "
            "with a current credential of the machine's client"
        )
    ),
    "target_changed": (
        N_(
            "this address now belongs to a different machine record from the one that "
            "was confirmed, or the confirmed record is gone. Confirm it in a new run"
        )
    ),
    "already_enrolled": N_("the machine is running the agent now, so there is nothing to install"),
    "not_installable": LIST_REASONS["not_installable"],
    "no_release": (
        N_(
            "no published agent release exists for this machine's operating system and "
            "processor. Publish one under Agent packages"
        )
    ),
    "credential_unreadable": (
        N_(
            "the run's credential could not be read from the vault in the form a push "
            "needs. Open it under Credentials and save it again"
        )
    ),
    "site_server_awaiting_review": (
        N_(
            "the site server for this machine's segment is waiting for review, so no job "
            "may be sent to it. Approve it under Devices"
        )
    ),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _lock_client_runs(session: AsyncSession, org_id: UUID, client_id: UUID) -> None:
    """Serialize run creation per CLIENT (D3), so two confirmations for one
    client cannot both pass the "no active run" check, while another
    client's confirmation does not wait. `uq_push_run_active` is the
    backstop."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('push_run:' || :org || ':' || :client))"),
        {"org": str(org_id), "client": str(client_id)},
    )


async def create_run(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID,
    prefix: str,
    credential_id: UUID,
    list_digest: str,
    public_console_url: str | None,
    now: datetime | None = None,
) -> PushRun:
    """A person confirms the list they were shown. It becomes a sealed run.

    **THE LIST IS EXPANDED AGAIN AND ITS DIGEST COMPARED BEFORE ANY ROW IS
    WRITTEN OR ANY SECRET IS TOUCHED** (design §1.3). There is no vault
    parameter here: creating a run decrypts nothing and signs nothing.
    Refuses:
    * everything `expand_prefix` refuses, including the `credentials.checkout`
      conjunction;
    * `PushListChangedError` when the digest does not match;
    * `PushValidationError` when nothing is ready;
    * `PushConflictError` when a run OF THE CREDENTIAL'S CLIENT is already
      running, paused or stopped by the lockout guard (D3: one per client).

    The run is the credential's client's, and so is every target: the list
    was expanded inside that client (`expand_prefix`). Its sites and client
    are locked FOR SHARE before the run lock and the first audit
    (`locks.lock_target_scope`): the pacer that the API runs in this same
    transaction mints their tokens after that audit.
    """
    stamp = now or _utcnow()
    confirmed = await expand_prefix(
        session,
        org_id,
        actor_id=actor_id,
        prefix=prefix,
        credential_id=credential_id,
        public_console_url=public_console_url,
        now=stamp,
    )
    if not hmac.compare_digest(confirmed.list_digest, list_digest):
        raise PushListChangedError(
            tr(
                "the machines in this subnet changed since the list was shown, so the "
                "confirmation no longer describes them. Nothing was started; read the list "
                "again and confirm what it shows now"
            )
        )
    ready = confirmed.ready_rows
    if not ready:
        raise PushValidationError(
            tr("no machine in this subnet is ready for a push, so there is nothing to confirm")
        )
    client_id = confirmed.client_id
    await lock_target_scope(
        session, org_id, await sites_of(session, org_id, (row.device_id for row in ready))
    )
    await _lock_client_runs(session, org_id, client_id)
    active = (
        await session.execute(
            select(PushRun.id).where(
                PushRun.org_id == org_id,
                PushRun.client_id == client_id,
                PushRun.state.in_(PUSH_RUN_ACTIVE_STATES),
            )
        )
    ).first()
    if active is not None:
        raise PushConflictError(
            tr(
                "this client already has a push run that is running, paused or stopped "
                "after sign-in failures: one active run per client. Let it finish, or "
                "cancel it, first"
            )
        )

    run = PushRun(
        org_id=org_id,
        client_id=client_id,
        created_by=actor_id,
        credential_id=confirmed.credential_id,
        prefix=confirmed.prefix,
        list_digest=confirmed.list_digest,
        state="running",
        target_count=len(ready),
        created_at=stamp,
        updated_at=stamp,
        # The lockout guard's window opens now: one target first (spec §1.4a).
        auth_window_started_at=stamp,
    )
    session.add(run)
    await session.flush()
    for position, row in enumerate(ready):
        session.add(
            PushRunTarget(
                org_id=org_id,
                run_id=run.id,
                device_id=row.device_id,
                address=row.address,
                position=position,
                state="waiting",
                updated_at=stamp,
            )
        )
    await session.flush()
    # SEALED IN THE SAME TRANSACTION: from here the database refuses any new
    # target for this run (`push_run_target_fixed`).
    run.sealed_at = stamp
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="push.run.started",
        target_type="push_run",
        target_id=str(run.id),
        client_id=client_id,
        metadata={
            "prefix": confirmed.prefix,
            "credential_id": str(confirmed.credential_id),
            "credential_name": confirmed.credential_name,
            "target_count": len(ready),
            "blocked_count": len(confirmed.rows) - len(ready),
            "ambiguous_count": confirmed.ambiguous_count,
            "list_digest": confirmed.list_digest,
            "concurrency": PUSH_RUN_CONCURRENCY,
        },
    )
    return run


async def get_run(session: AsyncSession, org_id: UUID, run_id: UUID) -> PushRun:
    run = (
        await session.execute(
            select(PushRun).where(PushRun.org_id == org_id, PushRun.id == run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise PushNotFoundError(tr("push run {run_id} not found", run_id=run_id))
    return run


async def _locked_run(session: AsyncSession, org_id: UUID, run_id: UUID) -> PushRun:
    run = (
        await session.execute(
            select(PushRun)
            .where(PushRun.org_id == org_id, PushRun.id == run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise PushNotFoundError(tr("push run {run_id} not found", run_id=run_id))
    return run


async def list_runs(session: AsyncSession, org_id: UUID, *, limit: int = 50) -> list[PushRun]:
    rows = await session.execute(
        select(PushRun)
        .where(PushRun.org_id == org_id)
        .order_by(PushRun.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def run_counts(session: AsyncSession, org_id: UUID, run_id: UUID) -> dict[str, int]:
    """Per-state counts, computed on read (R8): no stored counter to drift."""
    rows = (
        await session.execute(
            select(PushRunTarget.state, func.count())
            .where(PushRunTarget.org_id == org_id, PushRunTarget.run_id == run_id)
            .group_by(PushRunTarget.state)
        )
    ).all()
    counts = {state: 0 for state in PUSH_RUN_TARGET_STATES}
    for state, number in rows:
        counts[str(state)] = int(number)
    return counts


async def list_targets(
    session: AsyncSession, org_id: UUID, run_id: UUID, *, offset: int, limit: int
) -> tuple[int, list[PushRunTarget]]:
    await get_run(session, org_id, run_id)
    total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(PushRunTarget)
                .where(PushRunTarget.org_id == org_id, PushRunTarget.run_id == run_id)
            )
        ).scalar_one()
    )
    rows = await session.execute(
        select(PushRunTarget)
        .where(PushRunTarget.org_id == org_id, PushRunTarget.run_id == run_id)
        .order_by(PushRunTarget.position)
        .offset(offset)
        .limit(limit)
    )
    return total, list(rows.scalars())


async def _audit_control(
    session: AsyncSession,
    run: PushRun,
    *,
    actor_id: UUID,
    action: str,
    extra: dict[str, Any] | None = None,
) -> None:
    await audit_service.record(
        session,
        org_id=run.org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        target_type="push_run",
        target_id=str(run.id),
        client_id=run.client_id,
        metadata={
            "prefix": str(run.prefix),
            "state": run.state,
            "counts": await run_counts(session, run.org_id, run.id),
            **(extra or {}),
        },
    )


async def pause_run(
    session: AsyncSession, org_id: UUID, *, run_id: UUID, actor_id: UUID,
    now: datetime | None = None,
) -> PushRun:
    """New dispatches stop; installs already in flight finish (design §1.5)."""
    run = await _locked_run(session, org_id, run_id)
    if run.state != "running":
        raise PushConflictError(
            tr(
                "only a running push run can be paused; this one is {run_state}",
                run_state=run.state,
            )
        )
    run.state = "paused"
    run.updated_at = now or _utcnow()
    await session.flush()
    await _audit_control(session, run, actor_id=actor_id, action="push.run.paused")
    return run


async def _next_window_start(session: AsyncSession, run: PushRun, stamp: datetime) -> datetime:
    """Where a Resume opens the lockout guard's window: `stamp`, or just after
    the latest `dispatched_at` the run already holds, whichever is later.

    The guard counts a target in the window it was DISPATCHED in. Both this
    and the pacer's dispatch run under the run's row lock, so ordering them by
    construction, rather than by two hosts' clocks agreeing, is what makes
    "sent before the Resume" and "sent after it" disjoint whatever `now` a
    caller passes."""
    latest = (
        await session.execute(
            select(func.max(PushRunTarget.dispatched_at)).where(
                PushRunTarget.org_id == run.org_id, PushRunTarget.run_id == run.id
            )
        )
    ).scalar_one()
    if latest is not None and latest >= stamp:
        return latest + timedelta(microseconds=1)
    return stamp


async def resume_run(
    session: AsyncSession, org_id: UUID, *, run_id: UUID, actor_id: UUID,
    now: datetime | None = None,
) -> PushRun:
    """A PERSON's act. Only here and in `create_run` does a run become
    `running`; the pacer cannot resume (CLAUDE.md 2a(e) as amended).

    Resumes a `paused` run, or a `stopped_credential` run the lockout guard
    stopped (spec §1.4a), typically after the credential was fixed. Either way
    the guard's window restarts: `auth_window_started_at` moves to now, so
    nothing dispatched before this moment counts any more -- neither an
    `auth_failed` nor a sign-in, even one that reports after the Resume -- and
    the run goes back to ONE target until a sign-in in this window succeeds.

    Needs `credentials.checkout` as well as `agent.push.run` -- in the run's
    client (Task 12) -- because resuming causes stored secrets to be
    decrypted and presented again. The waiting targets' sites and client are
    locked before the resume's audit: the pacer the API runs next, in this
    transaction, mints their tokens after it (`locks.lock_target_scope`)."""
    refused = PushPermissionError(
        tr(
            "resuming a push run requires the `credentials.checkout` permission as "
            "well as `agent.push.run`: it presents a stored secret to the machines "
            "still waiting"
        )
    )
    if "credentials.checkout" not in await codes_held_anywhere(session, actor_id):
        raise refused
    stamp = now or _utcnow()
    run = await _locked_run(session, org_id, run_id)
    if not await has_permission(
        session, actor_id, "credentials.checkout", client_id=run.client_id
    ):
        raise refused
    await _lock_waiting_scope(session, run)
    if run.state not in ("paused", "stopped_credential"):
        final = tr(" A cancelled run is final.") if run.state == "cancelled" else ""
        raise PushConflictError(
            tr(
                "only a paused push run, or one stopped after sign-in failures, can be "
                "resumed; this one is {run_state}.{final}",
                run_state=run.state,
                final=final,
            )
        )
    from_state = run.state
    run.state = "running"
    run.auth_window_started_at = await _next_window_start(session, run, stamp)
    run.stopped_at = None
    run.updated_at = stamp
    await session.flush()
    await _audit_control(
        session, run, actor_id=actor_id, action="push.run.resumed",
        extra={"from_state": from_state},
    )
    return run


async def cancel_run(
    session: AsyncSession, org_id: UUID, *, run_id: UUID, actor_id: UUID,
    now: datetime | None = None,
) -> PushRun:
    """Every waiting target becomes `cancelled`, and the run is final. Installs
    already in flight finish, and their outcomes are still recorded."""
    stamp = now or _utcnow()
    run = await _locked_run(session, org_id, run_id)
    if run.state not in PUSH_RUN_ACTIVE_STATES:
        raise PushConflictError(
            tr(
                "this push run is already {run_state}; cancelling is final and cannot be repeated",
                run_state=run.state,
            )
        )
    result = await session.execute(
        update(PushRunTarget)
        .where(
            PushRunTarget.org_id == org_id,
            PushRunTarget.run_id == run.id,
            PushRunTarget.state == "waiting",
        )
        .values(state="cancelled", updated_at=stamp)
        .execution_options(synchronize_session=False)
    )
    run.state = "cancelled"
    run.finished_at = stamp
    run.updated_at = stamp
    await session.flush()
    await _audit_control(
        session,
        run,
        actor_id=actor_id,
        action="push.run.cancelled",
        extra={"cancelled_waiting": int(result.rowcount or 0)},  # type: ignore[attr-defined]
    )
    return run


# --------------------------------------------------------------------------- #
# The pacer
# --------------------------------------------------------------------------- #


Settled = tuple[str, str, str, bool]


def _terminal(job: Job | None, now: datetime) -> Settled | None:
    """`(state, outcome, detail, signed_in)` for a target whose job is over,
    or `None` while it is still live. Derived from the `jobs` row alone, so
    succeeded, failed, refused, expired and cancelled are handled in one place,
    as in `agent_update/reconcile.py`. Only a `succeeded` job carries an agent
    outcome, so only it can prove a sign-in."""
    if job is None:
        return ("failed", "job_missing", "", False)
    result = job.result if isinstance(job.result, dict) else {}
    if job.status is JobStatus.succeeded:
        outcome = str(result.get("outcome") or "unknown")[:32]
        detail = str(result.get("detail") or "")[:500]
        state = "installed" if outcome == "installed" else "failed"
        return (state, outcome, detail, _signed_in(outcome, result))
    if job.status is JobStatus.failed:
        return ("failed", "job_failed", str(result.get("error") or "")[:500], False)
    if job.status is JobStatus.refused:
        return ("failed", "agent_refused", str(result.get("refused") or "")[:500], False)
    if job.status is JobStatus.expired:
        return ("failed", "expired", "", False)
    if job.status is JobStatus.cancelled:
        return ("failed", "job_cancelled", str(job.cancel_outcome or ""), False)
    if job.expires_at + RUN_TARGET_LOST_AFTER < now:
        return ("failed", "lost", "", False)
    return None


def _apply(target: PushRunTarget, settled: Settled, now: datetime) -> None:
    target.state, target.outcome, target.detail, target.signed_in = settled
    # NOT the lockout guard's clock: a result counts in the window its target
    # was DISPATCHED in (`dispatched_at`), however late it arrives.
    target.settled_at = now
    target.updated_at = now


async def _reconcile(session: AsyncSession, run: PushRun, now: datetime) -> None:
    rows = (
        await session.execute(
            select(PushRunTarget, Job)
            .outerjoin(Job, Job.id == PushRunTarget.job_id)
            .where(
                PushRunTarget.org_id == run.org_id,
                PushRunTarget.run_id == run.id,
                PushRunTarget.state == "in_flight",
            )
        )
    ).all()
    for target, job in rows:
        settled = _terminal(job, now)
        if settled is not None:
            _apply(target, settled, now)
    await session.flush()


async def settle_target_for_job(session: AsyncSession, job: Job) -> None:
    """Called by `dispatch.record_install_result`, in the gateway, inside the
    job's own transaction. It records the outcome and nothing more: the gateway
    holds no vault, so the pacer's next pass fills the freed slot (R2)."""
    target = (
        await session.execute(
            select(PushRunTarget).where(
                PushRunTarget.org_id == job.org_id,
                PushRunTarget.job_id == job.id,
                PushRunTarget.state == "in_flight",
            )
        )
    ).scalar_one_or_none()
    if target is None:
        return
    now = _utcnow()
    settled = _terminal(job, now)
    if settled is not None:
        _apply(target, settled, now)
        await session.flush()


async def _count_state(session: AsyncSession, run: PushRun, *states: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(PushRunTarget)
                .where(
                    PushRunTarget.org_id == run.org_id,
                    PushRunTarget.run_id == run.id,
                    PushRunTarget.state.in_(states),
                )
            )
        ).scalar_one()
    )


async def _run_credential(session: AsyncSession, run: PushRun) -> Any | None:
    """The run's credential, metadata only, or `None` if it is gone, no
    longer a push credential, or not of the run's client (which the database
    already forbids: `its_push_run_client_guard`). It is resolved again on
    every pass, never cached."""
    if run.credential_id is None:
        return None
    try:
        return await resolve_push_credential(
            session, run.org_id, run.credential_id, client_id=run.client_id
        )
    except PushError:
        return None


async def _owner_may_push(session: AsyncSession, user_id: UUID, client_id: UUID) -> bool:
    """The conjunction `request_push_batch` checks first (R10), in the run's
    client: the person who confirmed the run still holds both codes THERE."""
    return await has_permission(
        session, user_id, "agent.push.run", client_id=client_id
    ) and await has_permission(session, user_id, "credentials.checkout", client_id=client_id)


async def _lock_waiting_scope(session: AsyncSession, run: PushRun) -> None:
    """The sites of the run's waiting targets, then their client, FOR SHARE
    (`locks.lock_target_scope`), read in one statement."""
    sites = (
        await session.execute(
            select(Device.site_id)
            .join(PushRunTarget, PushRunTarget.device_id == Device.id)
            .where(
                Device.org_id == run.org_id,
                PushRunTarget.org_id == run.org_id,
                PushRunTarget.run_id == run.id,
                PushRunTarget.state == "waiting",
            )
            .distinct()
        )
    ).scalars()
    await lock_target_scope(session, run.org_id, list(sites))


async def _is_enrolled(session: AsyncSession, org_id: UUID, device_id: UUID) -> bool:
    row = (
        await session.execute(
            select(Agent.id).where(Agent.org_id == org_id, Agent.device_id == device_id)
        )
    ).first()
    return row is not None


async def _recheck(
    session: AsyncSession,
    run: PushRun,
    target: PushRunTarget,
    *,
    credential: Any | None,
    owner_ok: bool,
    now: datetime,
) -> tuple[TargetDecision | None, list[str]]:
    """THE WHOLE LADDER, AGAINST CURRENT DATA, FOR THIS ONE TARGET (design
    §1.4 step 1). Every failing reason is collected, never just the first."""
    reasons: list[str] = []
    if not owner_ok:
        reasons.append("run_owner_not_permitted")
    if credential is None:
        reasons.append("credential_missing")
    if target.device_id is None:
        reasons.append("target_changed")
    decision: TargetDecision | None = None
    if credential is not None:
        (decision,) = await evaluate_targets(
            session,
            run.org_id,
            addresses=[parse_target_address(str(target.address))],
            credential_id=credential.id,
            client_id=run.client_id,
            now=now,
        )
        reasons.extend(r for r in decision.refusals if r not in reasons)
        if (
            decision.device_id is not None
            and target.device_id is not None
            and decision.device_id != target.device_id
            and "target_changed" not in reasons
        ):
            reasons.append("target_changed")
    if target.device_id is not None and await _is_enrolled(session, run.org_id, target.device_id):
        reasons.append("already_enrolled")
    return decision, reasons


async def _refuse_target(
    session: AsyncSession,
    run: PushRun,
    target: PushRunTarget,
    *,
    reasons: list[str],
    extras: dict[str, str],
    credential: Any | None,
    now: datetime,
) -> None:
    """`push.refused`, one per target, exactly as a ticked push writes it, plus
    the run's context. The pacer is the actor, on behalf of the run's creator (R17)."""
    target.state = "refused"
    target.reasons = list(reasons)
    target.updated_at = now
    await audit_service.record(
        session,
        org_id=run.org_id,
        actor_type="system",
        actor_id=None,
        action="push.refused",
        target_type="push_target",
        target_id=str(target.address),
        client_id=run.client_id,
        metadata={
            "target_address": str(target.address),
            "reasons": list(reasons),
            "run_id": str(run.id),
            "on_behalf_of": str(run.created_by),
            "credential_id": None if run.credential_id is None else str(run.credential_id),
            "credential_name": getattr(credential, "name", ""),
            **extras,
        },
    )


@dataclass(frozen=True)
class GuardWindow:
    """What the lockout guard knows about one run, READ IN ONE STATEMENT.

    * `auth_failures`: `auth_failed` targets dispatched in this window.
    * `proven`: a target dispatched in this window, with the credential's
      current secret, got past sign-in.
    * `probing`: targets in flight, dispatched in this window with the current
      secret -- the ones the one-first ceiling counts.
    * `in_flight`: every target in flight, whenever dispatched -- the ones
      `PUSH_RUN_CONCURRENCY` counts.
    * `waiting`: targets not yet dispatched or refused.
    """

    auth_failures: int
    proven: bool
    probing: int
    in_flight: int
    waiting: int


async def auth_window(
    session: AsyncSession, run: PushRun, fingerprint: str | None
) -> GuardWindow:
    """The lockout guard's view of `run` (spec §1.4a), in ONE statement.

    THE WINDOW IS BY DISPATCH. Only targets DISPATCHED since the run last
    started or resumed count ("A Resume starts from one again"): an install
    sent before a stop proves nothing and fails nothing in the window a Resume
    opened, whenever its result arrives.

    A SIGN-IN PROVES ONLY THE SECRET IT PRESENTED. It counts only when the
    target's `credential_fingerprint` is `fingerprint`, the credential's
    current version, so a secret changed mid-run goes back to one first until
    the new one has got through. `fingerprint=None` (no credential) proves
    nothing. Every `auth_failed` in the window counts, whichever version
    failed: a directory counts bad attempts against the account.

    WHY ONE STATEMENT (review item 4). The gateway settles a result in its own
    transaction (`settle_target_for_job`) and takes no run lock, so a settle
    can commit at any point of a pass. A settle moves one target from
    `in_flight` to `failed` AND adds its `auth_failed` in the same row. Read
    separately, a settle landing between the failure count and the in-flight
    count shows the slot it freed but not the failure that freed it, and a
    third failure becomes a fourth presentation. A single statement reads one
    snapshot, in which that row is either still in flight (holding its slot,
    its failure not yet counted) or settled (slot free, failure counted):
    never the free slot without the failure. Every dispatch decision in a
    pass is taken from this one snapshot, so no pass ever spends a slot freed
    by the failure that should have stopped it. Concurrent passes serialize
    on the run's row lock (`advance_run`), which is what keeps the snapshot
    current for everything the pacer itself writes."""
    in_window = PushRunTarget.dispatched_at >= run.auth_window_started_at
    current_secret = PushRunTarget.credential_fingerprint == fingerprint
    in_flight = PushRunTarget.state == "in_flight"
    row = (
        await session.execute(
            select(
                func.count().filter(in_window & (PushRunTarget.outcome == "auth_failed")),
                func.count().filter(
                    in_window & current_secret & PushRunTarget.signed_in.is_(True)
                ),
                func.count().filter(in_window & current_secret & in_flight),
                func.count().filter(in_flight),
                func.count().filter(PushRunTarget.state == "waiting"),
            ).where(PushRunTarget.org_id == run.org_id, PushRunTarget.run_id == run.id)
        )
    ).one()
    return GuardWindow(
        auth_failures=int(row[0]),
        proven=int(row[1]) > 0,
        probing=int(row[2]),
        in_flight=int(row[3]),
        waiting=int(row[4]),
    )


async def _stop_if_locked_out(
    session: AsyncSession, run: PushRun, now: datetime, guard: GuardWindow
) -> bool:
    """THE SAFETY STOP (spec §1.4a; CLAUDE.md 2a(e) as amended): the pacer's
    one act of its own. After `PUSH_RUN_AUTH_FAILURE_LIMIT` `auth_failed`
    results in this window, while machines are still waiting, the run becomes
    `stopped_credential`: nothing further is dispatched, installs in flight
    finish, and only a person's Resume restarts it. Nothing waiting means
    nothing left to protect, so the run is left to complete. `guard` is the
    pass's one snapshot (`auth_window`)."""
    failures = guard.auth_failures
    if failures < PUSH_RUN_AUTH_FAILURE_LIMIT or not guard.waiting:
        return False
    run.state = "stopped_credential"
    run.stopped_at = now
    run.updated_at = now
    await session.flush()
    await audit_service.record(
        session,
        org_id=run.org_id,
        actor_type="system",
        actor_id=None,
        action="push.run.stopped",
        target_type="push_run",
        target_id=str(run.id),
        client_id=run.client_id,
        metadata={
            "reason": "auth_failures",
            "auth_failures": failures,
            "limit": PUSH_RUN_AUTH_FAILURE_LIMIT,
            "window_started_at": run.auth_window_started_at.isoformat(),
            "prefix": str(run.prefix),
            "credential_id": None if run.credential_id is None else str(run.credential_id),
            "on_behalf_of": str(run.created_by),
            "counts": await run_counts(session, run.org_id, run.id),
        },
    )
    counts = await run_counts(session, run.org_id, run.id)
    credential = await _run_credential(session, run)
    await publish_event(
        session,
        org_id=run.org_id,
        event_type="push.run.stopped",
        subject_type="push_run",
        subject_id=run.id,
        payload={
            "run_id": str(run.id),
            "prefix": str(run.prefix),
            "reason": "auth_failures",
            "auth_failures": failures,
            "credential_name": getattr(credential, "name", ""),
            "target_count": run.target_count,
            "installed": counts["installed"],
            "waiting": counts["waiting"],
            "stopped_at": now.isoformat(),
        },
        actor_user_id=None,
        client_id=run.client_id,
    )
    return True


async def _fill(
    session: AsyncSession,
    run: PushRun,
    dispatcher: RunTargetDispatcher,
    now: datetime,
    *,
    credential: Any | None,
    guard: GuardWindow,
) -> list[Envelope]:
    """ONE JOB AT A TIME, AS CAPACITY FREES (design §1.4). A refusal consumes
    no capacity, so the next waiting target is tried in its place.

    ONE FIRST (§1.4a): until a sign-in has got through in this window, at
    most ONE target dispatched in this window is in flight, so a wrong
    password reaches one machine at a time until the guard stops the run.
    Installs still in flight from before a Resume neither prove the new
    window nor hold its one slot; they do still count toward
    `PUSH_RUN_CONCURRENCY`, which bounds everything in flight at once. The same
    holds for a SECRET changed mid-run: proven only by a sign-in with it.

    The room is computed from `guard`, the pass's single snapshot, and never
    from a second read: see `auth_window` for why that keeps the three-failure
    bound tight against the gateway's settle."""
    room = PUSH_RUN_CONCURRENCY - guard.in_flight
    if not guard.proven:
        room = min(room, 1 - guard.probing)
    if room <= 0:
        return []
    waiting = list(
        (
            await session.execute(
                select(PushRunTarget)
                .where(
                    PushRunTarget.org_id == run.org_id,
                    PushRunTarget.run_id == run.id,
                    PushRunTarget.state == "waiting",
                )
                .order_by(PushRunTarget.position)
            )
        ).scalars()
    )
    if not waiting:
        return []
    owner_ok = await _owner_may_push(session, run.created_by, run.client_id)
    envelopes: list[Envelope] = []
    for target in waiting:
        if room <= 0:
            break
        decision, reasons = await _recheck(
            session, run, target, credential=credential, owner_ok=owner_ok, now=now
        )
        extras: dict[str, str] = {}
        if not reasons:
            assert decision is not None
            try:
                # ONE SAVEPOINT PER TARGET (review item 9). The targets before
                # this one were decrypted, signed and audited in this pass; a
                # failure here must not roll their audit rows back with it.
                async with session.begin_nested():
                    outcome = await dispatcher.dispatch_target(
                        session,
                        org_id=run.org_id,
                        run_id=run.id,
                        on_behalf_of=run.created_by,
                        credential=credential,
                        decision=decision,
                    )
            except Exception as exc:  # noqa: BLE001 - recorded, and the pass stops
                await _record_dispatch_failure(
                    session, run, target, exc=exc, credential=credential
                )
                break
            if outcome.dispatched:
                assert outcome.envelope is not None
                target.state = "in_flight"
                target.job_id = outcome.job_id
                target.updated_at = now
                # In THIS window, whatever clock `now` came from: the window
                # moves only under the run lock this pass holds.
                target.dispatched_at = max(now, run.auth_window_started_at)
                target.credential_fingerprint = outcome.credential_fingerprint
                envelopes.append((UUID(str(outcome.envelope["job"]["device"])), outcome.envelope))
                room -= 1
                continue
            reasons, extras = list(outcome.refusals), dict(outcome.extras)
        await _refuse_target(
            session, run, target, reasons=reasons, extras=extras, credential=credential, now=now
        )
    await session.flush()
    return envelopes


async def _record_dispatch_failure(
    session: AsyncSession,
    run: PushRun,
    target: PushRunTarget,
    *,
    exc: Exception,
    credential: Any | None,
) -> None:
    """A dispatch failed for a reason that is not a named refusal (review
    item 9). Its savepoint is gone; the pass stops HERE, so what it already
    dispatched commits and is published, and this target stays `waiting` for
    the next pass -- it is not refused, because a signer outage is not the
    machine's fault.

    If the secret had already been checked out (`RunTargetDispatchError`),
    that decrypt is audited now, outside the savepoint that was rolled back:
    invariant 4 does not depend on the dispatch having finished. The cause's
    TYPE is recorded and logged, never its message, which may quote the
    signed parameters."""
    checked_out = isinstance(exc, RunTargetDispatchError)
    error = exc.cause_type if isinstance(exc, RunTargetDispatchError) else type(exc).__name__
    logger.error(
        "push run dispatch failed; the pass stopped and keeps what it already dispatched",
        extra={"run_id": run.id, "target_address": target.address, "error": error},
    )
    await audit_service.record(
        session,
        org_id=run.org_id,
        actor_type="system",
        actor_id=None,
        action="push.run.dispatch_failed",
        target_type="push_target",
        target_id=str(target.address),
        client_id=run.client_id,
        metadata={
            "target_address": str(target.address),
            "run_id": str(run.id),
            "on_behalf_of": str(run.created_by),
            "credential_id": None if run.credential_id is None else str(run.credential_id),
            "credential_name": getattr(credential, "name", ""),
            "credential_checked_out": checked_out,
            "error": error,
        },
    )


async def _complete_if_done(session: AsyncSession, run: PushRun, now: datetime) -> None:
    if await _count_state(session, run, "waiting", "in_flight"):
        return
    run.state = "completed"
    run.finished_at = now
    run.updated_at = now
    await session.flush()
    counts = await run_counts(session, run.org_id, run.id)
    await audit_service.record(
        session,
        org_id=run.org_id,
        actor_type="system",
        actor_id=None,
        action="push.run.completed",
        target_type="push_run",
        target_id=str(run.id),
        client_id=run.client_id,
        metadata={
            "prefix": str(run.prefix),
            "counts": counts,
            "on_behalf_of": str(run.created_by),
        },
    )
    await publish_event(
        session,
        org_id=run.org_id,
        event_type="push.run.completed",
        subject_type="push_run",
        subject_id=run.id,
        payload={
            "run_id": str(run.id),
            "prefix": str(run.prefix),
            "target_count": run.target_count,
            "installed": counts["installed"],
            "failed": counts["failed"],
            "refused": counts["refused"],
            "cancelled": counts["cancelled"],
            "finished_at": now.isoformat(),
        },
        actor_user_id=None,
        client_id=run.client_id,
    )


async def advance_run(
    session: AsyncSession,
    org_id: UUID,
    run_id: UUID,
    *,
    dispatcher: RunTargetDispatcher,
    now: datetime | None = None,
) -> list[Envelope]:
    """Continue ONE confirmed run. Returns the envelopes to publish after the
    caller's transaction commits.

    1. `SELECT ... FOR UPDATE` on the run: two concurrent callers (the beat, a
       person's Start or Resume) serialize here, so in-flight never exceeds
       `PUSH_RUN_CONCURRENCY`. Then the waiting targets' sites and client FOR
       SHARE, before any audit of this pass (`locks.lock_target_scope`).
    2. Settle every in-flight target from its job row.
    3. Only while the run is `running`: stop it as `stopped_credential` if the
       lockout guard's limit is reached (§1.4a); otherwise re-run the whole
       ladder for each next waiting target, dispatch it through `dispatcher`
       (one at a time until a sign-in succeeds), or refuse and record it. A
       paused, stopped or cancelled run is never resumed here.
    4. Complete the run once no target is waiting or in flight.

    It never inserts a target, never changes the credential and never writes
    `running`. Its one act of its own is the safety stop.
    """
    stamp = now or _utcnow()
    run = (
        await session.execute(
            select(PushRun)
            .where(PushRun.org_id == org_id, PushRun.id == run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        return []
    await _lock_waiting_scope(session, run)
    await _reconcile(session, run, stamp)
    envelopes: list[Envelope] = []
    if run.state != "running":
        # Settled, never resumed: nothing is dispatched here. But a paused or
        # stopped run whose last in-flight target just settled has no work
        # left, and must not hold the one-active-run slot until a person
        # resumes or cancels a finished run (Codex on #92). Completing is not
        # resuming; `running` is still written only by a person's act.
        if run.state in ("paused", "stopped_credential"):
            await _complete_if_done(session, run, stamp)
        await session.flush()
        return envelopes
    # Resolved once per pass, never cached across passes: the version of the
    # secret this pass would present is the one the guard asks about.
    credential = await _run_credential(session, run)
    fingerprint = None if credential is None else dispatcher.credential_fingerprint(credential)
    # ONE read for the whole pass's decision: the stop and the room.
    guard = await auth_window(session, run, fingerprint)
    if not await _stop_if_locked_out(session, run, stamp, guard):
        envelopes = await _fill(
            session, run, dispatcher, stamp, credential=credential, guard=guard
        )
        await _complete_if_done(session, run, stamp)
    await session.flush()
    return envelopes


def _live_condition() -> Any:
    in_flight = exists().where(
        PushRunTarget.org_id == PushRun.org_id,
        PushRunTarget.run_id == PushRun.id,
        PushRunTarget.state == "in_flight",
    )
    return PushRun.state.in_(PUSH_RUN_ACTIVE_STATES) | in_flight


async def org_ids_with_live_runs(admin_engine: AsyncEngine) -> list[tuple[UUID, UUID, UUID]]:
    """`(org_id, run_id, client_id)` of every live run -- running, paused or
    `stopped_credential` (never resumed by the beat, only settled), or
    cancelled with installs still in flight to settle -- oldest first within
    an org, in ONE statement.

    Read on the ADMIN engine, exactly as `agent_update.reconcile._org_ids`
    is: the beat must find the runs before it can open a tenant session for
    any, and it opens one per run, scoped to that run's client
    (`app/worker.py::push_run_tick_all_orgs`). Ids only."""
    async with AsyncSession(admin_engine) as session:
        rows = await session.execute(
            select(PushRun.org_id, PushRun.id, PushRun.client_id)
            .where(_live_condition())
            .order_by(PushRun.org_id, PushRun.created_at, PushRun.id)
        )
        return [(row.org_id, row.id, row.client_id) for row in rows]
