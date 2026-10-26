"""The action worker: remediate -> open_ticket -> notify
(`docs/modules/endpoint-health-alerting/spec.md` §0.10, §7, §8).

THE ORDER IS FIXED AND NOT CONFIGURABLE, and each position is load-bearing
(§0.10): remediation is asynchronous and its job id is context the other two
want to carry ("we saw X and have already dispatched Y"); the ticket must exist
before the notification so the mail can name a number that resolves; and notify
is LAST because it is the only one with an external, irreversible side effect --
if ticket creation fails, sending nothing beats sending a mail pointing at a
ticket which does not exist.

IDEMPOTENCY IS TWO UNIQUE INDEXES PLUS ONE ROW LOCK, AND THE INDEXES ALONE ARE
NOT ENOUGH (§0.10, amended).
`uq_health_alert_open_key` gives one open alert per key and `UNIQUE (alert_id,
kind)` gives one ticket, one remediation dispatch and one notify per alert,
forever. An at-least-once evaluation -- a redelivered breach, a re-run sweep, a
summary repairing a missed edge, a worker that dies mid-action and retries --
lands on one of those two rather than producing one ticket per tick.

WHAT THOSE INDEXES CANNOT DO, stated because this docstring previously claimed
otherwise ("NOTHING HERE ADDS A SECOND, APPLICATION-LEVEL DEDUP"): a unique
index bounds the number of ROWS, not the number of WRITERS TO ONE ROW. Two
transactions that both read the SAME `health_alert_action` row and both
transition it to `dispatched` violate no index -- one row goes in, and two
signed jobs go out at the endpoint. The sweep, `approve_action` and
`dispatch_manual_remediation` all target that same row, so the indexes are
structurally incapable of separating them and the second mechanism is not
optional. It is not a second DEDUP, though, which is why it cannot disagree
with the first: the indexes decide how many rows exist, the row lock decides
who may transition one, and the state machine (`_TERMINAL_STATES`) decides what
the loser is told. Reproduced before it was fixed: two concurrent approvals of
one alert produced two jobs against one `dispatched` row, and a console click
inside the sweep's open claim did the same.

CLAIM-BEFORE-EXECUTE IS `SELECT ... FOR UPDATE`, ON EVERY PATH THAT TRANSITIONS
A ROW. The sweep adds `SKIP LOCKED` because a row another worker is already
executing is a row this pass should leave alone; the two human paths
(`_load_action_row`, feeding `approve_action` and `refuse_action`, and
`dispatch_manual_remediation`) BLOCK instead, because a console click that
silently did nothing would be worse than one that waits and is then told the
remediation already went. Both re-read the row under the lock
(`populate_existing`), so the loser sees the winner's terminal state and
refuses on it rather than acting on a stale `awaiting_approval`.

THERE IS NO `claimed_at` COLUMN AND NO STUCK-CLAIM SWEEP. `health_alert_action`
has no such column (§2.5, migration 0042) and needs none, and this is the
`drain_outbox` precedent rather than the `deliver_pending` one -- `app/worker.py`
states the distinction in its own docstring. Every step here is a plain database
read/write: `JobEngine.create` writes a row and signs it, `create_ticket` and
`change_status` write rows, and `notify` only marks a row against an outbox
event that ingestion already wrote. THE REDIS PUBLISH HAPPENS AFTER THE
TRANSACTION COMMITS, outside the claim, so nothing here holds a database
connection open across an outbound call. A worker that dies mid-action
therefore rolls its transaction back and releases the lock with it, which is
exactly the case a stuck-claim sweep exists to repair when there IS a network
call inside the claim. `notification_deliveries` needs one because
`attempt_delivery` opens a socket; this does not.

THE PUBLISH IS THE CALLER'S JOB. `execute_pending_actions` returns the signed
envelopes it created and the CALLER publishes them after its `org_session` block
exits -- `app/modules/deploy/scripts.py::run_script`'s contract, stated in its
own docstring: a job published before the caller's tenant transaction commits
could be handed to an agent with no durable server record if the commit later
failed.

A FAILED REMEDIATION DOES NOT CLEAR THE ALERT AND DOES NOT RE-FIRE. It posts a
comment on the ticket if one exists and leaves the alert open for a human.
Auto-remediation that retries itself is how a service-restart loop becomes an
outage.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.approved_actions import (
    ApprovedAction,
    fuse_breach,
    fuse_breach_worded,
    get_script_exemption,
    is_destructive,
    render_params_template,
)
from app.core.audit import audit_service
from app.core.i18n import tr, use_locale
from app.jobs.dispatch_permissions import dispatch_permission_for
from app.jobs.registry import registered_names
from app.models import Device, Job, Script, Ticket, TicketComment, TicketStatusHistory
from app.modules.health.declared_actions import ACTION_KINDS
from app.modules.health.errors import (
    HealthForbiddenError,
    HealthNotFoundError,
    HealthValidationError,
)
from app.modules.health.models import HealthAlert, HealthAlertAction, HealthCheck
from app.modules.health.settings import EffectiveHealthSettings, get_settings

__all__ = [
    "ActionOutcome",
    "approve_action",
    "auto_resolve_alert_ticket",
    "dispatch_manual_remediation",
    "execute_pending_actions",
    "refuse_action",
]

logger = logging.getLogger("its.health.actions")

# States the sweep will pick up. `awaiting_approval` is NOT among them: it is
# waiting on a human, and a sweep that "executed" it would be the autonomy
# dial's middle setting silently meaning "autonomous after 30 seconds".
_DUE_STATES: tuple[str, ...] = ("pending",)

# Terminal for this alert, forever. `UNIQUE (alert_id, kind)` means a row that
# reached one of these is never replaced by a second attempt at the same kind.
_TERMINAL_STATES: frozenset[str] = frozenset(
    {"dispatched", "done", "skipped", "blocked_rate_limit", "blocked_flapping", "expired", "dead"}
)


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


@dataclass(frozen=True)
class ActionOutcome:
    """What one sweep pass did, plus the envelopes the caller must publish.

    Counts rather than a bare success flag: "the sweep ran" is satisfied by a
    sweep that did nothing, and the fuse tests need to see 20 dispatched beside
    1 `blocked_rate_limit` in the SAME batch.
    """

    dispatched: int = 0
    ticketed: int = 0
    notified: int = 0
    blocked: int = 0
    skipped: int = 0
    awaiting_approval: int = 0
    envelopes: tuple[tuple[UUID, dict[str, Any]], ...] = ()


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# §7.1's fuses, evaluated against `jobs`
# ---------------------------------------------------------------------------


async def _fuse_breach(
    session: AsyncSession,
    org_id: UUID,
    action: ApprovedAction,
    *,
    device_id: UUID,
    now: datetime,
) -> str | None:
    """The three fuses of §7.1 clause 2, or `None` when all pass.

    ONE LINE, AND THE BODY IS THE KERNEL'S. It moved to
    `app/core/approved_actions.py::fuse_breach` -- read that docstring for why
    the fuses count against `jobs`, why two approved actions sharing a job type
    share one budget, and why every branch names the job ids it counted.

    THE MOVE WAS FORCED BY docs/modules/ai-automation.md §6.1 AND §6.5, which
    need this exact derivation at preview and again at dispatch, and by §2.3
    property 1, which makes "`app/core/ai/` imports no `app.modules.*` package"
    a test. Reimplementing it there would have produced two derivations of "is
    this fused", which is precisely the defect `is_destructive`'s docstring
    exists to prevent. This delegation is kept rather than deleting the name so
    the three call sites below, and health's own tests, read unchanged.
    """
    return await fuse_breach(session, org_id, action, device_id=device_id, now=now)


async def _fuse_breach_worded(
    session: AsyncSession,
    org_id: UUID,
    action: ApprovedAction,
    *,
    device_id: UUID,
    now: datetime,
) -> str | None:
    """`_fuse_breach` in the REQUEST's language, for a refusal that only answers a
    person. `_fuse_breach` stays English: its text lands in `last_error`."""
    return await fuse_breach_worded(session, org_id, action, device_id=device_id, now=now)


# ---------------------------------------------------------------------------
# remediate
# ---------------------------------------------------------------------------


def _require_dispatch_grant(action: ApprovedAction, permissions: Collection[str]) -> None:
    """§20's SECOND permission: the one the job type's OWN module route requires.

    `health.remediate` is the floor, not the whole check. The catalogue row this
    dispatch resolved to carries a `job_type`, and that job type has its own
    module dispatch route with its own permission -- `patch.apply` for
    `patch.apply`, `deploy.script.run` for `script.run`, `backup.run` for
    `backup.run`. Gating this path on `health.remediate` ALONE let its holder
    fire a `patch.apply` job here while being refused the same job on
    `POST /api/v1/patch/apply`: privilege escalation across a module boundary,
    and invariant #6 violated -- authorization was server-side, but it was the
    wrong authorization. `app/core/ai/act.py::confirm_action` closed exactly this
    hole on its own dispatch path (`23df039` closed it on the kernel route); this
    is the same rule, applied to health's console path, from the same map, so the
    three cannot drift.

    **`None` IS A REFUSAL, NOT A PASS**, and reading it the other way turns this
    function into the bypass it exists to close. `dispatch_permission_for`
    returns `None` in two cases -- a job type in `NO_HUMAN_DISPATCH_PATH` (all
    three of them: `health.policy.apply`, `agent.cert.renew`,
    `agent.cert.install`, each dispatched by a kernel state machine from state
    the kernel itself minted) and a job type nobody has classified at all. The
    first means "there is no route, so there is no grant to check"; the second
    means "nobody has said which grant applies". Neither is a licence to
    dispatch, and the second is the one that matters as the platform grows: a
    module registering a new job type must not have it become dispatchable
    through health by default.

    **THE STORED ROW IS NOT THE AUTHORITY, THE JOB TYPE AT DISPATCH IS.** This
    reads `action.job_type` as it stands right now, in the same transaction that
    is about to create the job, for the reason `_rederived_destructive` re-derives
    `destructive` instead of trusting the column (`4f4e05b`): what a write gate
    checked when the catalogue row was authored is not authority at dispatch
    time. An action edited from `inventory.collect` to `patch.apply` after it was
    approved dispatches as `patch.apply` and is gated as `patch.apply`.

    PURE AND SYNCHRONOUS: no session, no clock. The permission set is resolved
    once by the caller (`user_permission_codes`) and passed in, so this function
    cannot accidentally answer a different question from the route's own gate.
    """
    required = dispatch_permission_for(action.job_type)
    if required is None:
        raise HealthForbiddenError(
            tr(
                "approved action {action_key} names job type {action_job_type}, which has "
                "no human dispatch route in this platform -- there is no permission that "
                "would authorize running it, so it cannot be dispatched from here either",
                action_key=repr(action.key),
                action_job_type=repr(action.job_type),
            )
        )
    if required not in permissions:
        raise HealthForbiddenError(
            tr(
                "dispatching approved action {action_key} additionally requires {required} "
                "-- the same permission {action_job_type}'s own module route requires. "
                "health.remediate is the floor, not the whole check",
                action_key=repr(action.key),
                required=required,
                action_job_type=repr(action.job_type),
            )
        )


async def _refusal(session: AsyncSession, org_id: UUID, action: ApprovedAction) -> str | None:
    """§7.1 clause 1's three refusals, plus the amendment's fourth, worded in the
    REQUEST's language. For a person who asked; `_refuse_reason` is the same
    refusal as the English the sweep stores."""
    if not action.active:
        return tr("approved action {action_key} is inactive", action_key=repr(action.key))
    if action.job_type not in registered_names():
        return tr(
            "approved action {action_key} names job type {action_job_type}, which is no "
            "longer registered",
            action_key=repr(action.key),
            action_job_type=repr(action.job_type),
        )
    if "health" not in (action.personas or []):
        return tr(
            "approved action {action_key} does not carry the 'health' persona",
            action_key=repr(action.key),
        )
    if action.job_type == "script.run":
        # §7.1's amendment. `update_script` bumps `version` IN PLACE and the
        # approved body is not retained anywhere (§2.10), so comparing versions
        # is the only form pinning can take. Not retried: the fix is an admin
        # re-approving the new version, not a delay.
        live = (
            await session.execute(
                select(Script.version).where(
                    Script.org_id == org_id, Script.id == action.script_id
                )
            )
        ).scalar_one_or_none()
        if live is None:
            return tr(
                "approved action {action_key} pins script {script_id}, which is gone",
                action_key=repr(action.key),
                script_id=action.script_id,
            )
        if live != action.script_version:
            return tr(
                "approved action {action_key} pins script version {script_version} but the "
                "live script is version {live}; it was edited since it was approved",
                action_key=repr(action.key),
                script_version=action.script_version,
                live=live,
            )
    return None


async def _refuse_reason(
    session: AsyncSession, org_id: UUID, action: ApprovedAction
) -> str | None:
    """`_refusal` in English, for the sweep: the text lands in `last_error`, a
    database value, and a stored value never takes the request's language."""
    with use_locale("en"):
        return await _refusal(session, org_id, action)


async def _rederived_destructive(
    session: AsyncSession, org_id: UUID, action: ApprovedAction
) -> bool:
    """`is_destructive` run AGAIN, from the action row as it stands right now.

    THE STORED COLUMN IS THE GATE, AND UNTIL NOW IT WAS THE ONLY ONE. §8.3's
    rule -- a machine never dispatches a destructive action -- is enforced at
    write time by `_validate_and_derive` and as a fact by
    `ck_approved_action_autonomous_is_bounded`; both read
    `approved_action.destructive`, a STORED column. The dispatcher below then
    branched on `autonomy` alone and never read it, so the entire distance
    between an autonomous action and a signed job was one boolean nobody
    re-checked. ca7b595 made the shared script exemption re-derive that column
    on every action pinning the pair, which keeps it FRESH; keeping a value
    fresh and not trusting it alone are different defences, and this is the
    second one.

    WHAT CAN STILL MAKE THE COLUMN WRONG once it is written. The derivation's
    inputs are not all in the row: `_DESTRUCTIVE_JOB_TYPES` and
    `DESTRUCTIVE_PARAM_KEYS` are CODE. Adding a job type to the first, or a
    value to the second, changes the truth of every stored row in every tenant
    with no write to touch them and no constraint to notice -- the CHECK is
    evaluated on write, not on read. A deploy is all it takes. Re-deriving here
    costs one indexed lookup on the sweep's hot path and turns that from a
    silent fleet-wide change into a demotion an operator is told about.

    THE STORED COLUMN IS DELIBERATELY NOT AN INPUT to this answer. `autonomous`
    with `destructive = true` is unrepresentable -- the CHECK enforces it
    against the service, the ORM and direct SQL alike -- so OR-ing the stored
    value in would add a branch that cannot be reached and therefore cannot be
    tested. The gap this closes is the opposite one: a stored `false` the
    derivation no longer produces.
    """
    exemption = None
    if action.script_id is not None and action.script_version is not None:
        exemption = await get_script_exemption(
            session,
            org_id=org_id,
            script_id=action.script_id,
            script_version=action.script_version,
        )
    return is_destructive(
        action.job_type,
        action.params_template or {},
        exemption,
        script_id=action.script_id,
        script_version=action.script_version,
    )


async def _script_run_params(
    session: AsyncSession, org_id: UUID, action: ApprovedAction
) -> dict[str, Any]:
    """`ScriptRunParams` built from the PINNED `scripts` row (§8.2).

    Not from `params_template`: duplicating the pin into the template would
    create two truths that can disagree, and §8.2 has the dispatcher build the
    params from the row precisely so the body that runs is the body that was
    approved.
    """
    script = (
        await session.execute(
            select(Script).where(Script.org_id == org_id, Script.id == action.script_id)
        )
    ).scalar_one()
    return {
        "interpreter": script.interpreter,
        "body": script.body,
        "script_id": str(script.id),
        "script_version": script.version,
    }


async def _dispatch_remediation(
    session: AsyncSession,
    org_id: UUID,
    row: HealthAlertAction,
    alert: HealthAlert,
    action: ApprovedAction,
    *,
    job_engine: _JobEngineLike,
    approved_by: UUID | None,
    autonomy: str,
    now: datetime,
    superseded: ApprovedAction | None = None,
) -> tuple[UUID, dict[str, Any]]:
    """§7.1 clause 4. ONE DEVICE, ONE JOB, THROUGH THE UNMODIFIED SIGNED ENGINE.

    `target_device_id` is a single `UUID`. There is no target union and no group
    path in this signature, which is how §8.4 makes "fleet-wide is structurally
    impossible" a claim about the code rather than a convention -- there is
    nothing here to pass a group through.

    `superseded` is the approved action whose PENDING APPROVAL REQUEST this
    dispatch consumed, when a console dispatch answered a request for a
    different action (there is one `remediate` slot per alert, so answering it
    with Y discards the request for X). It is carried into the audit record
    because "a job was sent" and "the question somebody was asked was thrown
    away unanswered" are different facts, and only the first of them used to be
    written down.
    """
    if action.job_type == "script.run":
        params = await _script_run_params(session, org_id, action)
    else:
        params = render_params_template(
            action.job_type,
            action.params_template or {},
            device_id=alert.device_id,
            selector=alert.selector,
        )
    job = await job_engine.create(
        session,
        org_id=org_id,
        type=action.job_type,
        target_device_id=alert.device_id,
        params=params,
        requested_by=approved_by,
    )
    row.job_id = job.id
    row.state = "dispatched"
    row.approved_by = approved_by
    row.approved_at = now if approved_by is not None else None
    row.updated_at = now
    session.add(row)
    await session.flush()

    # §11.3: IN ADDITION TO the kernel's own `jobs.dispatched`. The kernel record
    # says a job was sent; this one says WHY A MACHINE DECIDED TO SEND IT.
    metadata: dict[str, Any] = {
        "alert_id": str(alert.id),
        "action_key": action.key,
        "job_id": str(job.id),
        "autonomy": autonomy,
        "approved_by": str(approved_by) if approved_by else None,
        "device_id": str(alert.device_id),
    }
    if superseded is not None:
        metadata["superseded_action_key"] = superseded.key
        metadata["superseded_action_id"] = str(superseded.id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if approved_by is not None else "system",
        actor_id=approved_by,
        action="health.remediation_dispatched",
        target_type="health_alert",
        target_id=str(alert.id),
        metadata=metadata,
    )
    await session.flush()
    return job.id, await job_engine.signed_envelope(job)


async def _execute_remediate(
    session: AsyncSession,
    org_id: UUID,
    row: HealthAlertAction,
    alert: HealthAlert,
    check: HealthCheck | None,
    *,
    job_engine: _JobEngineLike | None,
    settings: EffectiveHealthSettings,
    now: datetime,
) -> tuple[str, tuple[UUID, dict[str, Any]] | None]:
    """One `remediate` row. Returns `(outcome_key, envelope_or_None)`."""
    if row.approved_action_id is None:
        row.state = "skipped"
        row.last_error = "no approved action is attached to this remediate row"
        row.updated_at = now
        session.add(row)
        return "skipped", None

    action = (
        await session.execute(
            select(ApprovedAction).where(
                ApprovedAction.org_id == org_id, ApprovedAction.id == row.approved_action_id
            )
        )
    ).scalar_one_or_none()
    if action is None:
        row.state = "skipped"
        row.last_error = "the approved action no longer resolves in this org"
        row.updated_at = now
        session.add(row)
        return "skipped", None

    refusal = await _refuse_reason(session, org_id, action)
    if refusal is not None:
        row.state = "skipped"
        row.last_error = refusal[:500]
        row.updated_at = now
        session.add(row)
        return "skipped", None

    # Health is reported for a machine waiting for review (install-link spec
    # §1.5); remediation is not monitoring, and the gate would refuse it.
    review_state = (
        await session.execute(
            select(Device.review_state).where(
                Device.org_id == org_id, Device.id == alert.device_id
            )
        )
    ).scalar_one_or_none()
    if review_state not in (None, "approved"):
        row.state = "skipped"
        row.last_error = (
            "waiting for review: this computer joined through the install link and has "
            "not been approved, so no remediation is sent to it"
        )
        row.updated_at = now
        session.add(row)
        return "skipped", None

    breach = await _fuse_breach(session, org_id, action, device_id=alert.device_id, now=now)
    if breach is not None:
        # BLOCKED, NOT QUEUED (§7.1): "a remediation deferred an hour is a
        # remediation applied to a situation that has changed."
        row.state = "blocked_rate_limit"
        row.last_error = breach[:500]
        row.updated_at = now
        session.add(row)
        return "blocked", None

    if action.autonomy == "suggest":
        # Nothing is dispatched. The console offers one-click dispatch to a
        # holder of `health.remediate` (§8.3); the row records the suggestion.
        row.state = "skipped"
        row.last_error = (
            f"approved action {action.key!r} is at autonomy 'suggest'; a user holding "
            f"health.remediate must dispatch it"
        )
        row.updated_at = now
        session.add(row)
        return "skipped", None

    if action.autonomy == "require_approval":
        row.state = "awaiting_approval"
        row.expires_at = now + timedelta(seconds=settings.approval_ttl_s)
        row.updated_at = now
        session.add(row)
        await session.flush()
        # ALWAYS PUBLISHED, silent check or not (§7.3): this is not an alert
        # notification, it is a request addressed to a named human who must act,
        # and an approval request nobody is told about expires at
        # `approval_ttl_s` -- the dial's middle setting silently meaning "never".
        await _publish_approval_request(
            session, org_id, alert, action, check=check, settings=settings
        )
        return "awaiting_approval", None

    # AUTONOMOUS FROM HERE, and this is the last thing between a machine's
    # decision and a signed job. `destructive` is RE-DERIVED rather than read off
    # the row: see `_rederived_destructive` for why a stored column that was
    # correct when it was written can stop being correct without any write.
    if await _rederived_destructive(session, org_id, action):
        # DEMOTED TO `require_approval`, NOT SKIPPED. The remediation is still
        # wanted -- what changed is that it now needs a human -- and skipping it
        # would silently drop a remediation an operator configured. This is the
        # same demotion `approved_actions._rederive_from_exemption` performs on
        # the config row; the row's own `autonomy` is deliberately NOT rewritten
        # here, because a sweep that edits configuration is a sweep nobody can
        # audit.
        logger.warning(
            "health: approved action %s is stored destructive=%s but re-derives DESTRUCTIVE at "
            "dispatch; the autonomous remediation was demoted to require_approval",
            action.key,
            action.destructive,
            # I-4: the widened AST sweep now flags str(...) inside an extra=
            # dict literal the same as it always flagged str(...) as a
            # %-arg -- these two UUIDs were pre-stringified for no reason
            # `_safe_value` needed; it already coerces a UUID via `_COERCIBLE`
            # to the identical text. Passing the objects themselves keeps the
            # output byte-identical and clears the sweep.
            extra={"org_id": org_id, "approved_action_id": action.id},
        )
        row.state = "awaiting_approval"
        row.expires_at = now + timedelta(seconds=settings.approval_ttl_s)
        row.last_error = (
            f"approved action {action.key!r} is autonomous and stored "
            f"destructive={action.destructive}, but re-deriving it at dispatch says DESTRUCTIVE; "
            f"§8.3 forbids a machine dispatching a destructive action, so a human must approve "
            f"this one. The stored column is stale -- re-save the action to correct it"
        )[:500]
        row.updated_at = now
        session.add(row)
        await session.flush()
        await _publish_approval_request(
            session, org_id, alert, action, check=check, settings=settings
        )
        return "awaiting_approval", None

    if job_engine is None:
        row.state = "skipped"
        row.last_error = "no job engine is available in this process to dispatch a remediation"
        row.updated_at = now
        session.add(row)
        return "skipped", None

    envelope = await _dispatch_remediation(
        session,
        org_id,
        row,
        alert,
        action,
        job_engine=job_engine,
        approved_by=None,
        autonomy=action.autonomy,
        now=now,
    )
    return "dispatched", envelope


async def _publish_approval_request(
    session: AsyncSession,
    org_id: UUID,
    alert: HealthAlert,
    action: ApprovedAction,
    *,
    check: HealthCheck | None,
    settings: EffectiveHealthSettings,
) -> None:
    """`health.remediation_requires_approval`, with the pinned script named.

    §13 and §2.10: "nobody approves a script they cannot identify." The script
    NAME and VERSION travel; the BODY never does -- it is arbitrary code, which
    is why §8.4 makes `script.run` destructive by default in the first place.

    Published through `events.publish` directly rather than through
    `publish_alert_event`, because that helper gates on the check declaring
    `notify` and this event is deliberately NOT gated (§7.3).
    """
    from app.core import events
    from app.modules.health.alerts import alert_event_payload

    script_name: str | None = None
    if action.script_id is not None:
        script_name = (
            await session.execute(
                select(Script.name).where(
                    Script.org_id == org_id, Script.id == action.script_id
                )
            )
        ).scalar_one_or_none()

    payload = await alert_event_payload(
        session,
        org_id,
        alert,
        settings=settings,
        comparator=check.comparator if check is not None else "",
    )
    payload["remediation_action_key"] = action.key
    payload["script_name"] = script_name
    payload["script_version"] = action.script_version
    await events.publish(
        session,
        org_id=org_id,
        event_type="health.remediation_requires_approval",
        subject_type="health_alert",
        subject_id=alert.id,
        payload=payload,
        actor_user_id=None,
        # Read from the database (trigger-filled, expired after an UPDATE).
        client_id=(
            await session.execute(select(HealthAlert.client_id).where(HealthAlert.id == alert.id))
        ).scalar_one(),
    )


# ---------------------------------------------------------------------------
# open_ticket
# ---------------------------------------------------------------------------


def _ticket_description(
    alert: HealthAlert,
    check: HealthCheck | None,
    *,
    hostname: str,
    remediation: str,
    previous_number: str | None,
) -> str:
    """§7.2's description. NO RAW LOG TEXT AND NO SCRIPT BODY -- the same rule
    §9.1 applies to payloads, with the extra weight that a ticket is read by
    more people than an event is."""
    lines = [
        f"Endpoint health alert on {hostname}.",
        "",
        f"Check:        {alert.check_type}",
        f"Selector:     {alert.selector or '(none)'}",
        f"Comparator:   {check.comparator if check is not None else '(unknown)'}",
        f"Threshold:    {alert.threshold_display}",
        f"Observed:     {alert.observed_value}",
        f"Severity:     {alert.severity}",
        f"Opened at:    {alert.opened_at.isoformat()}",
    ]
    if remediation:
        lines.append(f"Remediation:  {remediation}")
    if previous_number:
        lines.append(
            f"Previous:     this condition last alerted on ticket {previous_number}; see "
            f"alert {alert.previous_alert_id}"
        )
    return "\n".join(lines)


async def _execute_open_ticket(
    session: AsyncSession,
    org_id: UUID,
    row: HealthAlertAction,
    alert: HealthAlert,
    check: HealthCheck | None,
    *,
    settings: EffectiveHealthSettings,
    now: datetime,
) -> str:
    """§7.2. NOTHING HERE ENFORCES THE ALERT-ORIGIN CONTRACT, because
    `create_ticket` already does.

    Verified rather than re-implemented: it enforces `origin='alert' =>
    type='incident'`, forces `requester_id`/`created_by` to NULL for alert
    origin regardless of what the caller passes, and refuses `source_alert_id`
    for any other origin, with `ck_ticket_human_attribution` behind it. A second
    copy of those rules here would be a second answer to who may create a
    machine-attributed ticket.
    """
    from app.modules.service_desk.errors import TicketValidationError
    from app.modules.service_desk.schema import TicketCreate
    from app.modules.service_desk.tickets import create_ticket

    if settings.default_ticket_category_id is None:
        row.state = "skipped"
        row.last_error = (
            "health_org_settings.default_ticket_category_id is not configured; an alert "
            "ticket has no category to land in"
        )
        row.updated_at = now
        session.add(row)
        return "skipped"

    hostname = (
        await session.execute(
            select(Device.hostname).where(Device.org_id == org_id, Device.id == alert.device_id)
        )
    ).scalar_one_or_none() or str(alert.device_id)

    remediation = ""
    job_id = (
        await session.execute(
            select(HealthAlertAction.job_id).where(
                HealthAlertAction.org_id == org_id,
                HealthAlertAction.alert_id == alert.id,
                HealthAlertAction.kind == "remediate",
            )
        )
    ).scalar_one_or_none()
    if job_id is not None:
        remediation = f"a remediation job has been dispatched (job {job_id})"

    previous_number: str | None = None
    if alert.previous_alert_id is not None:
        previous_number = (
            await session.execute(
                select(Ticket.number)
                .join(HealthAlert, HealthAlert.ticket_id == Ticket.id)
                .where(HealthAlert.org_id == org_id, HealthAlert.id == alert.previous_alert_id)
            )
        ).scalar_one_or_none()

    subject = f"[{alert.severity}] {alert.check_type}"
    if alert.selector:
        subject += f" {alert.selector}"
    subject += f" on {hostname}"

    try:
        # `last_error` below is stored, so a refusal is worded in English whatever the
        # language of the request that approved the action.
        with use_locale("en"):
            ticket = await create_ticket(
                session,
                org_id,
                None,
                TicketCreate(
                    type="incident",
                    category_id=settings.default_ticket_category_id,
                    queue_id=settings.default_alert_queue_id,
                    subject=subject[:200],
                    description=_ticket_description(
                        alert,
                        check,
                        hostname=hostname,
                        remediation=remediation,
                        previous_number=previous_number,
                    ),
                    device_id=alert.device_id,
                ),
                origin="alert",
                source_alert_id=alert.id,
            )
    except TicketValidationError as exc:
        row.state = "dead"
        row.last_error = str(exc)[:500]
        row.updated_at = now
        session.add(row)
        return "skipped"

    row.state = "done"
    row.ticket_id = ticket.id
    row.updated_at = now
    session.add(row)
    alert.ticket_id = ticket.id
    alert.updated_at = now
    session.add(alert)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="health.alert_ticket_opened",
        target_type="health_alert",
        target_id=str(alert.id),
        metadata={"ticket_id": str(ticket.id), "ticket_number": ticket.number},
    )
    await session.flush()
    return "ticketed"


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


async def _execute_notify(
    session: AsyncSession,
    org_id: UUID,
    row: HealthAlertAction,
    alert: HealthAlert,
    *,
    now: datetime,
) -> str:
    """§7.3: notify PUBLISHES NOTHING ITSELF.

    The alert's `health.alert_raised` event is already in the outbox from
    ingestion, in the alert's own transaction. This row exists so a check
    WITHOUT it can be silent by design; marking it `done` records that the
    outbox event for this alert exists. Publishing here would double every
    alert mail, because the outbox row is already there.
    """
    from app.models import DomainEvent

    published = (
        await session.execute(
            select(func.count())
            .select_from(DomainEvent)
            .where(
                DomainEvent.org_id == org_id,
                DomainEvent.subject_id == alert.id,
                DomainEvent.event_type == "health.alert_raised",
            )
        )
    ).scalar_one()
    row.state = "done" if published else "skipped"
    if not published:
        row.last_error = "no health.alert_raised event exists for this alert"
    row.updated_at = now
    session.add(row)
    await session.flush()
    return "notified" if published else "skipped"


# ---------------------------------------------------------------------------
# The sweep body
# ---------------------------------------------------------------------------


async def execute_pending_actions(
    session: AsyncSession,
    org_id: UUID,
    *,
    job_engine: _JobEngineLike | None = None,
    now: datetime | None = None,
    limit: int = 500,
) -> ActionOutcome:
    """Execute this org's due `health_alert_action` rows in §0.10's fixed order.

    ORDERED BY KIND FIRST, ALERT SECOND. `remediate` for every alert runs before
    `open_ticket` for any of them, so a ticket's description can name a job id
    the same pass dispatched. Ordering by alert first would make the ticket for
    alert #1 precede the remediation for alert #2, which is correct per alert
    but leaves the batch's behaviour dependent on insertion order.

    Claims with `FOR UPDATE SKIP LOCKED` -- see the module docstring for why
    that is the whole claim mechanism here and why no `claimed_at` column and no
    stuck-claim sweep are needed.
    """
    now = now or _utcnow()
    settings = await get_settings(session, org_id)

    rows = list(
        (
            await session.execute(
                select(HealthAlertAction)
                .where(
                    HealthAlertAction.org_id == org_id,
                    HealthAlertAction.state.in_(_DUE_STATES),
                )
                .order_by(HealthAlertAction.created_at, HealthAlertAction.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return ActionOutcome()

    by_kind: dict[str, list[HealthAlertAction]] = {kind: [] for kind in ACTION_KINDS}
    for row in rows:
        by_kind.setdefault(row.kind, []).append(row)

    counts = {
        "dispatched": 0,
        "ticketed": 0,
        "notified": 0,
        "blocked": 0,
        "skipped": 0,
        "awaiting_approval": 0,
    }
    envelopes: list[tuple[UUID, dict[str, Any]]] = []

    for kind in ACTION_KINDS:
        for row in by_kind.get(kind, []):
            alert = (
                await session.execute(
                    select(HealthAlert).where(
                        HealthAlert.org_id == org_id, HealthAlert.id == row.alert_id
                    )
                )
            ).scalar_one_or_none()
            if alert is None:  # pragma: no cover - FK makes this unreachable
                continue
            check = (
                await session.execute(
                    select(HealthCheck).where(
                        HealthCheck.org_id == org_id, HealthCheck.id == alert.check_id
                    )
                )
            ).scalar_one_or_none()

            if kind == "remediate":
                key, envelope = await _execute_remediate(
                    session,
                    org_id,
                    row,
                    alert,
                    check,
                    job_engine=job_engine,
                    settings=settings,
                    now=now,
                )
                if envelope is not None:
                    envelopes.append(envelope)
            elif kind == "open_ticket":
                key = await _execute_open_ticket(
                    session, org_id, row, alert, check, settings=settings, now=now
                )
            else:
                key = await _execute_notify(session, org_id, row, alert, now=now)
            counts[key] += 1

    await session.flush()
    return ActionOutcome(envelopes=tuple(envelopes), **counts)


# ---------------------------------------------------------------------------
# Approval / refusal / manual dispatch (§8.3, §10)
# ---------------------------------------------------------------------------


async def _load_action_row(
    session: AsyncSession, org_id: UUID, alert_id: UUID, action_id: UUID
) -> HealthAlertAction:
    """Load ONE `health_alert_action` row FOR UPDATE, BLOCKING.

    The lock is the point, not the load: `approve_action` and `refuse_action`
    both transition this row, and `UNIQUE (alert_id, kind)` cannot separate two
    writers to ONE row. See the module docstring. Blocking rather than
    `SKIP LOCKED` (the sweep's choice) because the loser here is a human who
    must be TOLD, and `populate_existing` so that what they are told is the
    winner's committed state and not the stale row this session read first.
    """
    row = (
        await session.execute(
            select(HealthAlertAction)
            .where(
                HealthAlertAction.org_id == org_id,
                HealthAlertAction.id == action_id,
                HealthAlertAction.alert_id == alert_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HealthNotFoundError(
            tr("health alert action {action_id} not found", action_id=action_id)
        )
    return row


async def approve_action(
    session: AsyncSession,
    org_id: UUID,
    alert_id: UUID,
    action_id: UUID,
    *,
    actor_id: UUID,
    permissions: Collection[str],
    job_engine: _JobEngineLike,
    now: datetime | None = None,
) -> tuple[UUID, dict[str, Any]]:
    """A holder of `health.remediate` approves an `awaiting_approval` row.

    Returns `(job_id, envelope)` for the caller to publish AFTER COMMIT -- the
    `deploy/scripts.py` contract. The approver is carried into
    `jobs.requested_by` and into the `health.remediation_dispatched` audit
    record, because "why a machine decided to send it" is a different fact from
    "a job was sent" and this one has a name on it.

    **`permissions` IS REQUIRED AND HAS NO DEFAULT (§20).** This route dispatches
    a job whose type carries its own module gate, so `health.remediate` alone is
    not enough -- see `_require_dispatch_grant`. A keyword with no default is
    what makes a caller that forgot it a `TypeError` at import-adjacent test
    time rather than a silent dispatch with an empty permission set.
    """
    now = now or _utcnow()
    row = await _load_action_row(session, org_id, alert_id, action_id)
    if row.kind != "remediate":
        raise HealthValidationError(
            tr(
                "action {action_id} is a {row_kind}, not a remediation",
                action_id=action_id,
                row_kind=repr(row.kind),
            )
        )
    if row.state != "awaiting_approval":
        raise HealthValidationError(
            tr(
                "action {action_id} is {row_state}; only an awaiting_approval remediation "
                "can be approved",
                action_id=action_id,
                row_state=repr(row.state),
            )
        )
    if row.expires_at is not None and now > row.expires_at:
        row.state = "expired"
        row.last_error = "the approval window closed before anyone acted"
        row.updated_at = now
        session.add(row)
        await session.flush()
        raise HealthValidationError(
            tr("action {action_id}'s approval window has expired", action_id=action_id)
        )

    alert = (
        await session.execute(
            select(HealthAlert).where(HealthAlert.org_id == org_id, HealthAlert.id == alert_id)
        )
    ).scalar_one()
    action = (
        await session.execute(
            select(ApprovedAction).where(
                ApprovedAction.org_id == org_id, ApprovedAction.id == row.approved_action_id
            )
        )
    ).scalar_one()

    # §20, AND IT IS CHECKED BEFORE ANY REFUSAL THAT WRITES. `_fuse_breach` below
    # transitions the row to `blocked_rate_limit`, and a caller who may not
    # dispatch this job type at all must not be able to burn an approval request
    # by asking. The expiry transition above is the clock's own fact, not this
    # caller's write, and the API rolls it back with the 403 in any case.
    _require_dispatch_grant(action, permissions)

    refusal = await _refusal(session, org_id, action)
    if refusal is not None:
        raise HealthValidationError(refusal)
    breach = await _fuse_breach(session, org_id, action, device_id=alert.device_id, now=now)
    if breach is not None:
        row.state = "blocked_rate_limit"
        row.last_error = breach[:500]
        row.updated_at = now
        session.add(row)
        await session.flush()
        raise HealthValidationError(
            await _fuse_breach_worded(
                session, org_id, action, device_id=alert.device_id, now=now
            )
            or breach
        )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="health.remediation_approved",
        target_type="health_alert",
        target_id=str(alert_id),
        metadata={"action_key": action.key, "action_id": str(action_id)},
    )
    return await _dispatch_remediation(
        session,
        org_id,
        row,
        alert,
        action,
        job_engine=job_engine,
        approved_by=actor_id,
        autonomy=action.autonomy,
        now=now,
    )


async def refuse_action(
    session: AsyncSession,
    org_id: UUID,
    alert_id: UUID,
    action_id: UUID,
    *,
    actor_id: UUID,
    reason: str = "",
    now: datetime | None = None,
) -> HealthAlertAction:
    """A holder of `health.remediate` refuses. The row is `skipped`, forever --
    `UNIQUE (alert_id, kind)` means there is no second attempt to make."""
    now = now or _utcnow()
    row = await _load_action_row(session, org_id, alert_id, action_id)
    if row.state != "awaiting_approval":
        raise HealthValidationError(
            tr(
                "action {action_id} is {row_state}; only an awaiting_approval remediation "
                "can be refused",
                action_id=action_id,
                row_state=repr(row.state),
            )
        )
    row.state = "skipped"
    row.approved_by = actor_id
    row.approved_at = now
    row.last_error = (f"refused by a user: {reason}" if reason else "refused by a user")[:500]
    row.updated_at = now
    session.add(row)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="health.remediation_refused",
        target_type="health_alert",
        target_id=str(alert_id),
        metadata={"action_id": str(action_id), "reason": reason},
    )
    await session.flush()
    return row


async def dispatch_manual_remediation(
    session: AsyncSession,
    org_id: UUID,
    alert_id: UUID,
    *,
    action_key: str,
    actor_id: UUID,
    permissions: Collection[str],
    job_engine: _JobEngineLike,
    now: datetime | None = None,
) -> tuple[UUID, dict[str, Any]]:
    """`POST /health/alerts/{id}/remediate` (§10, §8.3's `suggest` row).

    **THE ROUTE'S `health.remediate` IS THE FLOOR, NOT THE WHOLE CHECK (§20).**
    `body.action_key` resolves to a catalogue row carrying a `job_type`, and this
    additionally requires whatever THAT job type's own module route requires --
    `_require_dispatch_grant`, from `app/jobs/dispatch_permissions.py`, where
    `None` means refuse. `permissions` is a required keyword with no default for
    the reason `approve_action`'s is.

    THE FUSES APPLY TO A HUMAN TOO. §8.4's real fleet-wide risk is 4,000
    device-scoped jobs in one minute; a manual path exempt from the fuses is a
    manual path that reproduces it one click at a time. The autonomy dial does
    NOT apply -- the human IS the approval -- but every other refusal in §7.1
    clause 1 does, including the script-version pin.

    AND SO DOES THE APPROVAL WINDOW, when the row it lands on is an open
    approval request. This is the second dispatch path onto one `remediate`
    slot; see the `awaiting_approval` branch below for what it must not be
    allowed to be a back door around.
    """
    now = now or _utcnow()
    alert = (
        await session.execute(
            select(HealthAlert).where(HealthAlert.org_id == org_id, HealthAlert.id == alert_id)
        )
    ).scalar_one_or_none()
    if alert is None:
        raise HealthNotFoundError(tr("health alert {alert_id} not found", alert_id=alert_id))

    action = (
        await session.execute(
            select(ApprovedAction).where(
                ApprovedAction.org_id == org_id, ApprovedAction.key == action_key
            )
        )
    ).scalar_one_or_none()
    if action is None:
        raise HealthNotFoundError(
            tr("approved action {action_key} not found", action_key=repr(action_key))
        )

    # §20, FIRST OF THE REFUSALS AND BEFORE ANYTHING BELOW WRITES. The
    # `awaiting_approval` branch further down transitions an expired request to
    # `expired`, and the row INSERT happens below too; a caller who may not
    # dispatch this job type at all reaches neither.
    _require_dispatch_grant(action, permissions)

    refusal = await _refusal(session, org_id, action)
    if refusal is not None:
        raise HealthValidationError(refusal)
    breach = await _fuse_breach_worded(
        session, org_id, action, device_id=alert.device_id, now=now
    )
    if breach is not None:
        raise HealthValidationError(breach)

    # FOR UPDATE, BLOCKING -- the same claim the sweep takes, without
    # `SKIP LOCKED`. See the module docstring: `UNIQUE (alert_id, kind)` bounds
    # the number of ROWS and cannot bound the number of WRITERS to one row, and
    # this console path and the sweep target the SAME row. A row that does not
    # exist yet needs no lock: the INSERT below is itself locked by this
    # transaction until it commits, and a concurrent INSERT of the same
    # `(alert_id, kind)` blocks on the unique index and then fails it.
    row = (
        await session.execute(
            select(HealthAlertAction)
            .where(
                HealthAlertAction.org_id == org_id,
                HealthAlertAction.alert_id == alert_id,
                HealthAlertAction.kind == "remediate",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        row = HealthAlertAction(
            org_id=org_id,
            alert_id=alert_id,
            kind="remediate",
            state="pending",
            approved_action_id=action.id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
    elif row.state in _TERMINAL_STATES:
        # `UNIQUE (alert_id, kind)` is the guarantee; this is the message. One
        # remediation dispatch per alert, forever (§0.10).
        raise HealthValidationError(
            tr(
                "alert {alert_id} already has a remediation in state {row_state}; one "
                "remediation dispatch per alert, forever",
                alert_id=alert_id,
                row_state=repr(row.state),
            )
        )

    superseded: ApprovedAction | None = None
    if row.state == "awaiting_approval":
        # THE ROW IS AN OPEN APPROVAL REQUEST, and this call is answering it.
        # There is only one `remediate` slot per alert, so a console dispatch
        # naming action Y consumes the slot that action X was waiting in. Two
        # things follow, and neither used to happen here.
        #
        # 1. THE EXPIRY WINDOW APPLIES, exactly as it does in `approve_action`.
        #    Skipping it made this the back door: a request nobody answered in
        #    time was still answerable through `POST .../remediate`, which is
        #    the approval TTL silently meaning "never".
        # 2. THE SWAP IS NAMED. The dispatch was always audited, but the audit
        #    said only "Y was dispatched" -- there was no record anywhere that
        #    the pending request for X had been discarded to make room, so the
        #    named human who was asked to decide about X could not learn what
        #    happened to it. `superseded_action_key` in
        #    `health.remediation_dispatched` is that record.
        #
        # The swap itself is ALLOWED: a holder of `health.remediate` deciding
        # "not that one, this one" is a legitimate answer to an approval
        # request, and forcing them through refuse-then-dispatch would buy
        # nothing but a second click.
        #
        # SCOPED TO `awaiting_approval` DELIBERATELY. A `pending` row naming a
        # different action is not a question anybody was asked -- the sweep has
        # not reached it, no human is waiting on it, and no window is running.
        # Overwriting that one supersedes nothing.
        if row.expires_at is not None and now > row.expires_at:
            row.state = "expired"
            row.last_error = "the approval window closed before anyone acted"
            row.updated_at = now
            session.add(row)
            await session.flush()
            raise HealthValidationError(
                tr(
                    "alert {alert_id}'s pending remediation approval expired at "
                    "{expires_at}; it cannot be answered by dispatching "
                    "{action_key}",
                    alert_id=alert_id,
                    expires_at=row.expires_at.isoformat(),
                    action_key=repr(action_key),
                )
            )
        if row.approved_action_id is not None and row.approved_action_id != action.id:
            superseded = (
                await session.execute(
                    select(ApprovedAction).where(
                        ApprovedAction.org_id == org_id,
                        ApprovedAction.id == row.approved_action_id,
                    )
                )
            ).scalar_one_or_none()

    row.approved_action_id = action.id

    return await _dispatch_remediation(
        session,
        org_id,
        row,
        alert,
        action,
        job_engine=job_engine,
        approved_by=actor_id,
        autonomy=action.autonomy,
        now=now,
        superseded=superseded,
    )


# ---------------------------------------------------------------------------
# §7.4 -- ticket auto-resolution on recovery
# ---------------------------------------------------------------------------


async def _human_touched(session: AsyncSession, org_id: UUID, ticket: Ticket) -> str | None:
    """§7.4's three signals, each checked independently.

    A test with only one signal passes against an implementation that checks
    only that one, so all three are enumerated here and each has its own test:
    a `user`-actor status-history row, a `user`-authored comment, or an
    assignee. Any one of them means a human is mid-investigation, and a ticket
    that resolves itself under them vanishes from their queue.
    """
    if ticket.assigned_to is not None:
        return "the ticket has an assignee"
    touched = (
        await session.execute(
            select(func.count())
            .select_from(TicketStatusHistory)
            .where(
                TicketStatusHistory.org_id == org_id,
                TicketStatusHistory.ticket_id == ticket.id,
                TicketStatusHistory.actor_type == "user",
            )
        )
    ).scalar_one()
    if touched:
        return "a user changed the ticket's status"
    commented = (
        await session.execute(
            select(func.count())
            .select_from(TicketComment)
            .where(
                TicketComment.org_id == org_id,
                TicketComment.ticket_id == ticket.id,
                TicketComment.author_type == "user",
            )
        )
    ).scalar_one()
    if commented:
        return "a user commented on the ticket"
    return None


async def auto_resolve_alert_ticket(
    session: AsyncSession,
    org_id: UUID,
    alert: HealthAlert,
    *,
    now: datetime,
    settings: EffectiveHealthSettings,
) -> bool:
    """§7.4. Drive an UNTOUCHED alert ticket to `resolved` -- NEVER `closed`.

    Returns whether the ticket was moved, so a caller asserts on a value.

    `resolved`, NOT `closed`, adopting `docs/modules/service-desk/spec.md`
    §7.2's pinned contract verbatim: closing removes the ticket from every
    "needs a human look" view, and the interesting case -- a disk that filled,
    was cleaned by remediation, and will fill again in six hours -- is exactly
    the one where the recovery is the LEAST informative event. The SLA engine's
    existing `auto_close_after_days` then closes it on the org's own schedule.

    TWO TRANSITIONS, NOT ONE, AND THIS IS A FINDING ABOUT THE CODE. An alert
    ticket is created at `new`, and `lifecycle._TRANSITIONS["incident"]["new"]`
    is `{"open", "cancelled"}` -- `new -> resolved` is NOT a legal edge. So the
    move is `new -> open -> resolved`, both with `actor_type="system"`, which
    leaves two `system` rows in `ticket_status_history`. That is the right
    shape: §7.4's mitigation for the MTTR-laundering incentive is ATTRIBUTION,
    and both rows carry it.

    THE PERVERSE INCENTIVE, ACCEPTED AND STATED (§7.4): untouched alert tickets
    auto-resolve, which manufactures an excellent MTTR figure for work nobody
    did. The mitigation is not a softer rule -- it is that `ticket.origin='alert'`
    plus `ticket_status_history.actor_type='system'` make machine-resolved and
    human-resolved separable, and §13 REQUIRES reporting to separate them. A
    dashboard that adds them together is a defect in the report, not in this
    rule.
    """
    if not settings.auto_resolve_alert_tickets or alert.ticket_id is None:
        return False

    from app.modules.service_desk.threads import add_comment
    from app.modules.service_desk.transitions import change_status

    ticket = (
        await session.execute(
            select(Ticket).where(Ticket.org_id == org_id, Ticket.id == alert.ticket_id)
        )
    ).scalar_one_or_none()
    if ticket is None:  # pragma: no cover - FK makes this unreachable
        return False

    comment = (
        f"The endpoint health alert this ticket was opened for has cleared "
        f"(reason: {alert.cleared_reason}). Observed value at clear: "
        f"{alert.observed_value or '(none)'}."
    )
    touched = await _human_touched(session, org_id, ticket)
    if touched is not None or ticket.status in ("resolved", "closed", "cancelled"):
        await add_comment(
            session,
            org_id,
            ticket.id,
            body=(
                f"{comment} This ticket was NOT auto-resolved because "
                f"{touched or f'it is already {ticket.status}'}."
            ),
            author_id=None,
            author_type="system",
        )
        return False

    if ticket.status == "new":
        await change_status(
            session,
            org_id,
            ticket.id,
            to_status="open",
            actor_id=None,
            actor_type="system",
            reason="endpoint health alert cleared",
        )
    await change_status(
        session,
        org_id,
        ticket.id,
        to_status="resolved",
        actor_id=None,
        actor_type="system",
        reason=f"endpoint health alert cleared ({alert.cleared_reason})",
    )
    await add_comment(
        session,
        org_id,
        ticket.id,
        body=f"{comment} Auto-resolved by the endpoint health module.",
        author_id=None,
        author_type="system",
    )
    await session.flush()
    return True
