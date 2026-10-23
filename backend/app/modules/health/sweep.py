"""The five health sweeps
(`docs/modules/endpoint-health-alerting/spec.md` §12, §6, §0.6, §0.12).

FIVE BEAT ENTRIES, FIVE FUNCTIONS, AND THE LOGIC IS HERE RATHER THAN IN
`app/worker.py`. `worker.py` is a thin scheduling shell for every module in this
repo; a sweep whose body lives there is a sweep that can only be tested through
Celery.

EVERY ONE OF THEM IS IDEMPOTENT UNDER A SECOND RUN, and that is a property of
the LATCHES rather than of the schedule:

  - `escalated_at` latches escalation. Stamped once, never cleared -- an alert
    returning from `muted` to `active` is NOT re-escalated. A sweep that
    re-fires on every pass turns one problem into an hourly mail, which is the
    same reasoning `docs/modules/sla/spec.md` §2.5 gives for its own
    `escalated_at`.
  - `muted_until` latches the mute. Expiry NULLs it in the same statement that
    moves the state back to `active`, so the re-notify happens exactly once --
    `ck_health_alert_muted_until_matches_state` would refuse any other pairing.
  - `uq_health_alert_open_key` latches the offline alert. The second offline
    pass finds the alert already open and bumps `occurrence_count`; it cannot
    mint a second one.
  - `device_health_policy_ack.policy_version` latches the reconcile. A device
    whose ack matches its effective profile's version gets ZERO jobs, which the
    test asserts as a row count and not as the absence of an exception.

INTERVALS ARE PROJECT CONSTANTS, NOT PER-ORG CONFIGURATION. The SLA sweep's
reasoning applies unchanged: detection granularity is an operational property of
this deployment, and a per-org interval lets one org's setting delay another
org's escalation. What IS per-org is the threshold each sweep compares against
(`escalate_after_s`, `offline_after_s`, `alert_retention_days`), which is the
right split -- how often we look is ours, what counts as late is theirs.

ESCALATION SKIPS A CHECK THAT DECLARES NO `notify` ACTION AND DOES NOT STAMP
`escalated_at` (§7.3's amendment): "an alert not worth a mail when it opened
does not become worth one by ageing." See
`app/modules/health/declared_actions.py` for why that declaration is currently
interim.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import Agent, Device, Job, Organization
from app.modules.health.actions import ActionOutcome, execute_pending_actions
from app.modules.health.alerts import clear_alert, open_alert_for_key, publish_alert_event
from app.modules.health.baseline import ensure_baseline
from app.modules.health.declared_actions import approved_action_id_for, declared_action_kinds
from app.modules.health.models import (
    DeviceHealthPolicyAck,
    HealthAlert,
    HealthAlertAction,
    HealthCheck,
    HealthFlapState,
)
from app.modules.health.policy_push import push_policy
from app.modules.health.resolution import resolve_profile_for_device
from app.modules.health.settings import EffectiveHealthSettings, get_settings

__all__ = [
    "ACTION_INTERVAL_S",
    "ALERT_SWEEP_INTERVAL_S",
    "OFFLINE_SWEEP_INTERVAL_S",
    "POLICY_RECONCILE_INTERVAL_S",
    "RETENTION_INTERVAL_S",
    "run_alert_actions",
    "run_alert_sweep",
    "run_offline_sweep",
    "run_policy_reconcile",
    "run_retention",
    "sweep_actions_all_orgs",
    "sweep_alerts_all_orgs",
    "sweep_baseline_all_orgs",
    "sweep_offline_all_orgs",
    "sweep_policy_reconcile_all_orgs",
    "sweep_retention_all_orgs",
]

logger = logging.getLogger("its.health.sweep")

# §12's table, as the beat entries carry them.
ACTION_INTERVAL_S = 30.0
ALERT_SWEEP_INTERVAL_S = 60.0
OFFLINE_SWEEP_INTERVAL_S = 60.0
POLICY_RECONCILE_INTERVAL_S = 900.0
RETENTION_INTERVAL_S = 86400.0

# §0.6's server-evaluated check type. Named once: three copies of the string is
# three chances for one of them to drift from the registry.
OFFLINE_CHECK_TYPE = "agent.offline"


# `(job_id, envelope)`, the shape `push_policy` and the action worker both
# return. Named so the two shells below cannot disagree about it.
Envelope = tuple[UUID, dict[str, Any]]
PublishEnvelope = Callable[[UUID, dict[str, Any]], Awaitable[None]]
SweepBody = Callable[[AsyncSession, UUID], Awaitable[tuple[int, tuple[Envelope, ...]]]]


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


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# 1. health-alert-actions (30 s)
# ---------------------------------------------------------------------------


async def run_alert_actions(
    session: AsyncSession,
    org_id: UUID,
    *,
    job_engine: _JobEngineLike | None = None,
    now: datetime | None = None,
) -> ActionOutcome:
    """Execute this org's due action rows (§7, Task 11).

    A one-line delegation on purpose: the ordering, the fuses, the autonomy dial
    and the claim all live in `actions.py` where they are tested directly, and a
    sweep that re-implemented any of them would be a second answer to "may this
    run without a human".
    """
    return await execute_pending_actions(
        session, org_id, job_engine=job_engine, now=now or _utcnow()
    )


# ---------------------------------------------------------------------------
# 2. health-alert-sweep (60 s)
# ---------------------------------------------------------------------------


async def _expire_mutes(
    session: AsyncSession, org_id: UUID, settings: EffectiveHealthSettings, now: datetime
) -> int:
    """A lapsed mute returns the alert to `active` and re-notifies ONCE (§0.8).

    `muted_until` is NULLed in the same statement, which is both what
    `ck_health_alert_muted_until_matches_state` requires and what makes the
    re-notify exactly-once: the second pass finds no expired mutes because there
    is no stamp left to expire.
    """
    alerts = list(
        (
            await session.execute(
                select(HealthAlert)
                .where(
                    HealthAlert.org_id == org_id,
                    HealthAlert.state == "muted",
                    HealthAlert.muted_until.is_not(None),
                    HealthAlert.muted_until <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for alert in alerts:
        alert.state = "active"
        alert.muted_until = None
        alert.updated_at = now
        session.add(alert)
        await session.flush()
        check = (
            await session.execute(
                select(HealthCheck).where(
                    HealthCheck.org_id == org_id, HealthCheck.id == alert.check_id
                )
            )
        ).scalar_one_or_none()
        await publish_alert_event(
            session,
            org_id,
            alert,
            event_type="health.alert_raised",
            check=check,
            settings=settings,
        )
    return len(alerts)


async def _escalate(
    session: AsyncSession, org_id: UUID, settings: EffectiveHealthSettings, now: datetime
) -> int:
    """§6's escalation: ONCE per alert, `active` only, older than
    `escalate_after_s`, and only where the check declares `notify`.

    `acknowledged` and `muted` are excluded because both mean a human has
    already engaged -- escalating past someone who took ownership is how an
    escalation path teaches people not to acknowledge.
    """
    cutoff = now - timedelta(seconds=settings.escalate_after_s)
    alerts = list(
        (
            await session.execute(
                select(HealthAlert)
                .where(
                    HealthAlert.org_id == org_id,
                    HealthAlert.state == "active",
                    HealthAlert.escalated_at.is_(None),
                    HealthAlert.opened_at < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    escalated = 0
    for alert in alerts:
        check = (
            await session.execute(
                select(HealthCheck).where(
                    HealthCheck.org_id == org_id, HealthCheck.id == alert.check_id
                )
            )
        ).scalar_one_or_none()
        if check is None or "notify" not in declared_action_kinds(check, settings):
            # §7.3's amendment: skipped ENTIRELY, and `escalated_at` is NOT
            # stamped. An alert not worth a mail when it opened does not become
            # worth one by ageing.
            continue
        alert.escalated_at = now
        alert.updated_at = now
        session.add(alert)
        await session.flush()
        await publish_alert_event(
            session,
            org_id,
            alert,
            event_type="health.alert_escalated",
            check=check,
            settings=settings,
        )
        escalated += 1
    return escalated


async def _expire_approvals(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    """§8.3: "unapproved after `approval_ttl_s` -> `expired`."

    Expired, not silently retried: the dial's middle setting means a named human
    must act, and a request that quietly becomes a dispatch when nobody does is
    `autonomous` wearing `require_approval`'s label.
    """
    rows = list(
        (
            await session.execute(
                select(HealthAlertAction)
                .where(
                    HealthAlertAction.org_id == org_id,
                    HealthAlertAction.state == "awaiting_approval",
                    HealthAlertAction.expires_at.is_not(None),
                    HealthAlertAction.expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.state = "expired"
        row.last_error = "the approval window closed before anyone acted"
        row.updated_at = now
        session.add(row)
    await session.flush()
    return len(rows)


async def _clear_stale_unavailable(
    session: AsyncSession, org_id: UUID, settings: EffectiveHealthSettings, now: datetime
) -> int:
    """§0.8 clause 3, driven by the sweep as well as by ingestion.

    Ingestion clears on a `summary` line, which requires the device to still be
    reporting. A device that stopped reporting entirely would otherwise hold its
    `unavailable` alerts forever -- so the sweep clears an alert whose CURRENT
    check state is `unavailable` and which has not been reconfirmed for longer
    than the timeout. It reads `device_health_check_state`, the one row per key
    ingestion already maintains, rather than inventing a second source of "is
    this answerable".

    THE AGE IS `last_seen_at`, NOT `opened_at`, and the two halves of clause 3
    must agree or the sweep undoes what ingestion refused. `last_seen_at` is the
    last time the condition was CONFIRMED still breaching; `opened_at` is the
    alert's total age, which made a long-lived real breach clear as
    `unavailable_timeout` the moment its check first became unanswerable. See
    `ingest._clear_for_line` for the full reasoning and §0.8 clause 3 for the
    corrected wording.
    """
    from app.modules.health.models import DeviceHealthCheckState

    cutoff = now - timedelta(seconds=settings.unavailable_clear_after_s)
    alerts = list(
        (
            await session.execute(
                select(HealthAlert)
                .join(
                    DeviceHealthCheckState,
                    (DeviceHealthCheckState.org_id == HealthAlert.org_id)
                    & (DeviceHealthCheckState.device_id == HealthAlert.device_id)
                    & (DeviceHealthCheckState.check_id == HealthAlert.check_id)
                    & (DeviceHealthCheckState.selector == HealthAlert.selector),
                )
                .where(
                    HealthAlert.org_id == org_id,
                    HealthAlert.state != "cleared",
                    HealthAlert.last_seen_at < cutoff,
                    DeviceHealthCheckState.state == "unavailable",
                )
            )
        )
        .scalars()
        .all()
    )
    cleared = 0
    for alert in alerts:
        if await clear_alert(
            session,
            org_id,
            alert,
            reason="unavailable_timeout",
            now=now,
            settings=settings,
        ):
            cleared += 1
    return cleared


async def run_alert_sweep(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> dict[str, int]:
    """§12's `health-alert-sweep`: mute expiry, escalation, `unavailable_timeout`
    clears, approval expiry.

    Returns counts rather than nothing, so a test asserts on work DONE. "The
    sweep ran without raising" is satisfied by a sweep that selected zero rows,
    which is what every broken version of this function also does.
    """
    now = now or _utcnow()
    settings = await get_settings(session, org_id)
    return {
        "unmuted": await _expire_mutes(session, org_id, settings, now),
        "escalated": await _escalate(session, org_id, settings, now),
        "cleared": await _clear_stale_unavailable(session, org_id, settings, now),
        "approvals_expired": await _expire_approvals(session, org_id, now),
    }


# ---------------------------------------------------------------------------
# 3. health-offline-sweep (60 s)
# ---------------------------------------------------------------------------


async def _clear_recovered_offline(
    session: AsyncSession,
    org_id: UUID,
    settings: EffectiveHealthSettings,
    *,
    cutoff: datetime,
    now: datetime,
) -> int:
    """§0.8 clause 1 FOR THE ONE SERVER-EVALUATED CHECK, which is the only place
    it can live.

    `agent.offline` is evaluated by the server (§0.6), so `resolve_profile_for_
    device` puts it in `checks` and NOT in `agent_checks` -- and `ingest_report`
    keys its per-line lookup off `agent_checks`, deliberately, because an agent
    reporting on whether it is offline is reporting on something only silence
    can answer. The consequence is that EVERY line naming this check is
    discarded, so ingestion's three clear clauses (`recovery`, `summary_ok`,
    `unavailable_timeout`) are all unreachable for it, and so is the alert
    sweep's `_clear_stale_unavailable`, which joins a `device_health_check_state`
    row ingestion never writes for a discarded line. Before this pass the ONLY
    ways an offline alert could clear were the check leaving the profile, the
    device leaving `active`, or a human clicking Close -- so a laptop asleep for
    fifteen minutes pinned an alert, its check, its profile and its ticket
    permanently, since `run_retention` deletes CLEARED alerts only.

    THE REASON IS `recovery`, NOT `unavailable_timeout`. The device answering
    again is the condition ending, which is a fixed problem and belongs in an
    MTTR figure; `unavailable_timeout` means a REMOVED SENSOR and §13 requires
    reporting to exclude it. Attributing a returning machine to the latter would
    hide every real outage from the resolution count.

    NOT THE SAME THING AS "OFFLINE CLEARS ALERTS", which stays forbidden: this
    clears the `agent.offline` alert and nothing else, and only on the edge back
    INSIDE `offline_after_s`. A device whose `agents` row has been removed
    entirely is not reachable from here -- it leaves `devices.status='active'`
    on decommission, which is §0.8 clause 5's path.
    """
    alerts = list(
        (
            await session.execute(
                select(HealthAlert)
                .join(
                    Agent,
                    (Agent.org_id == HealthAlert.org_id)
                    & (Agent.device_id == HealthAlert.device_id),
                )
                .where(
                    HealthAlert.org_id == org_id,
                    HealthAlert.check_type == OFFLINE_CHECK_TYPE,
                    HealthAlert.state != "cleared",
                    Agent.last_seen_at.is_not(None),
                    Agent.last_seen_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    cleared = 0
    for alert in alerts:
        check = (
            await session.execute(
                select(HealthCheck).where(
                    HealthCheck.org_id == org_id, HealthCheck.id == alert.check_id
                )
            )
        ).scalar_one_or_none()
        if await clear_alert(
            session,
            org_id,
            alert,
            reason="recovery",
            now=now,
            check=check,
            settings=settings,
        ):
            cleared += 1
    return cleared


async def run_offline_sweep(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> dict[str, int]:
    """§0.6's server-side `agent.offline` check. Returns `{opened, cleared}`.

    THE ONE SERVER-EVALUATED CHECK, because an agent that is offline cannot
    report that it is offline. It is excluded from every policy push and
    evaluated here instead, against `agents.last_seen_at`.

    IT CLEARS EXACTLY ONE THING: ITS OWN ALERT, AND ONLY ON THE WAY BACK. §0.8's
    "an alert does not clear because the device went offline. Offline is not
    healthy" is untouched -- a machine crashing under memory pressure must not
    recover every one of its alerts at the instant it dies, and
    `test_health_clear_predicate.py` still pins that behaviourally. What
    `_clear_recovered_offline` handles is the opposite edge, `last_seen_at` back
    INSIDE `offline_after_s`, which is the only clear path this check has: see
    that function's docstring for why none of ingestion's three clauses can ever
    fire for a server-evaluated type.

    IDEMPOTENT BY `uq_health_alert_open_key`: a second pass finds the alert open
    and bumps `occurrence_count` rather than minting a second one. The clear half
    is idempotent by `clear_alert` returning `False` for an already-cleared
    alert, which is the same latch every other sweep leans on.

    Counts are RETURNED rather than logged because "the sweep ran without
    raising" is satisfied by a sweep that selected zero rows.
    """
    now = now or _utcnow()
    settings = await get_settings(session, org_id)
    cutoff = now - timedelta(seconds=settings.offline_after_s)

    cleared = await _clear_recovered_offline(
        session, org_id, settings, cutoff=cutoff, now=now
    )

    rows = (
        await session.execute(
            select(Device, Agent.last_seen_at)
            .join(Agent, Agent.device_id == Device.id)
            .where(
                Device.org_id == org_id,
                Agent.org_id == org_id,
                Device.status == "active",
                # ONLY AN `in_service` RECORD CAN BE OFFLINE. A machine back in
                # the warehouse with its agent still installed is not down, a
                # switch was never expected to check in, and a retired record is
                # not a to-do item -- but all three have a stale `last_seen_at`
                # for ever, so without this they alert on every sweep until
                # somebody unboxes them.
                #
                # The cost is not one noisy alert: an alert an operator learns
                # to ignore is worse than no alert, because the next real one is
                # ignored the same way (asset-inventory spec §5).
                Device.lifecycle == "in_service",
                (Agent.last_seen_at.is_(None)) | (Agent.last_seen_at < cutoff),
            )
        )
    ).all()

    opened = 0
    for device, last_seen_at in rows:
        resolved = await resolve_profile_for_device(session, org_id, device)
        if resolved is None:
            continue
        offline_checks = [c for c in resolved.checks if c.check_type == OFFLINE_CHECK_TYPE]
        if not offline_checks:
            # An org that did not put `agent.offline` in its profile did not ask
            # to be told. Not a default: §3's "no fabricated default" applies to
            # the server-evaluated check exactly as to the pushed ones.
            continue
        check = offline_checks[0]
        existing = await open_alert_for_key(
            session, org_id, device_id=device.id, check_id=check.id, selector=""
        )
        seen = last_seen_at.isoformat() if last_seen_at is not None else "never"
        if existing is not None:
            existing.last_seen_at = now
            existing.occurrence_count += 1
            existing.observed_value = seen[:64]
            existing.updated_at = now
            session.add(existing)
            await session.flush()
            continue
        alert = HealthAlert(
            org_id=org_id,
            device_id=device.id,
            check_id=check.id,
            profile_id=resolved.profile.id,
            selector="",
            check_type=OFFLINE_CHECK_TYPE,
            severity=check.severity,
            state="active",
            observed_value=seen[:64],
            threshold_display=f"no contact for {settings.offline_after_s}s",
            opened_at=now,
            opened_policy_version=resolved.profile.policy_version,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(alert)
        await session.flush()
        for kind in declared_action_kinds(check, settings):
            session.add(
                HealthAlertAction(
                    org_id=org_id,
                    alert_id=alert.id,
                    kind=kind,
                    state="pending",
                    # As in `ingest.py`: a `remediate` row carries the approved
                    # action its check names, or it can only be skipped.
                    approved_action_id=approved_action_id_for(check, kind),
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        await publish_alert_event(
            session,
            org_id,
            alert,
            event_type="health.alert_raised",
            check=check,
            settings=settings,
        )
        opened += 1
    await session.flush()
    return {"opened": opened, "cleared": cleared}


# ---------------------------------------------------------------------------
# 4. health-policy-reconcile (900 s)
# ---------------------------------------------------------------------------


async def run_policy_reconcile(
    session: AsyncSession,
    org_id: UUID,
    *,
    job_engine: _JobEngineLike,
    now: datetime | None = None,
) -> list[tuple[UUID, dict[str, Any]]]:
    """§12: push `health.policy.apply` where the acknowledged version is behind
    the device's effective profile, AND where no ack row exists at all -- to a
    device whose agent is connected, and not when the same push is already
    waiting for it.

    Returns the `(job_id, envelope)` pairs for the CALLER to publish after its
    transaction commits -- `push_policy` deliberately does not publish, for the
    reason `deploy/scripts.py::run_script` states in its own docstring.

    A DEVICE WHOSE ACK MATCHES GETS ZERO JOBS. That is the whole point of the
    ack table: §0.4's arithmetic is 5,000 devices times a 5-minute tick = 1.4M
    job rows a day if the reconcile pushed unconditionally.
    """
    now = now or _utcnow()
    devices = list(
        (
            await session.execute(
                select(Device).where(
                    Device.org_id == org_id,
                    Device.status == "active",
                    # Pending machines ARE monitored (install-link spec §1.5); a
                    # rejected one may receive only `agent.uninstall`, and the
                    # gate would abort this org's reconcile on it.
                    Device.review_state != "rejected",
                )
            )
        )
        .scalars()
        .all()
    )
    acks = {
        device_id: version
        for device_id, version in (
            await session.execute(
                select(
                    DeviceHealthPolicyAck.device_id, DeviceHealthPolicyAck.policy_version
                ).where(DeviceHealthPolicyAck.org_id == org_id)
            )
        ).all()
    }

    # Only a device whose agent is connected now. Nothing else can receive the
    # job: an asset row has no agent at all, and an offline agent would let
    # one expire unanswered every hour for as long as it stays away (operator
    # report, 2026-09-24). The agent keeps the policy it last installed, and
    # the first pass after it reconnects pushes whatever changed meanwhile.
    connected = set(
        (
            await session.execute(
                select(Agent.device_id).where(
                    Agent.org_id == org_id,
                    Agent.status == "online",
                    Agent.cert_revoked.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )

    pushes: list[tuple[UUID, dict[str, Any]]] = []
    for device in devices:
        if device.id not in connected:
            continue
        resolved = await resolve_profile_for_device(session, org_id, device)
        acknowledged = acks.get(device.id)
        if resolved is not None and acknowledged == resolved.profile.policy_version:
            continue
        if resolved is None and acknowledged is None:
            # Never armed and nothing resolves: `push_policy` returns None
            # anyway, and skipping the call saves a resolve per device per
            # cycle on a fleet nobody configured.
            continue
        pushed = await push_policy(session, job_engine, org_id, device, unless_waiting=True)
        if pushed is not None:
            pushes.append(pushed)
    await session.flush()
    return pushes


# ---------------------------------------------------------------------------
# 5. health-retention (86400 s)
# ---------------------------------------------------------------------------


async def run_retention(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> dict[str, int]:
    """§12 / §0.12: delete `health_alert` past `alert_retention_days`, prune
    `health_flap_state` for keys with no alert in a flap window, then hard-delete
    retired `health_check` rows no alert still references.

    THE THIRD STEP IS WHY THE RETIRE MECHANISM IS NOT A LEAK. §2.4's amendment
    accepts a soft delete and says so plainly ("soft deletes leak into every
    query that forgot them"); this is the sweep that carries the OPPOSITE
    predicate and stops retired rows being immortal.

    Deletes CLEARED alerts only. An alert that is 400 days old and still open is
    a finding, not a record -- deleting it would be the module quietly forgetting
    a problem nobody fixed.
    """
    now = now or _utcnow()
    settings = await get_settings(session, org_id)
    cutoff = now - timedelta(days=settings.alert_retention_days)

    doomed = list(
        (
            await session.execute(
                select(HealthAlert.id).where(
                    HealthAlert.org_id == org_id,
                    HealthAlert.state == "cleared",
                    HealthAlert.cleared_at.is_not(None),
                    HealthAlert.cleared_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    if doomed:
        # `health_alert_action` first: its FK to `health_alert` has no CASCADE
        # (§2.5), deliberately, so that deleting an alert is an explicit act
        # rather than something a schema default does on someone's behalf.
        await session.execute(
            delete(HealthAlertAction).where(
                HealthAlertAction.org_id == org_id, HealthAlertAction.alert_id.in_(doomed)
            )
        )
        # `previous_alert_id` is a self-FK: a surviving alert pointing at a
        # deleted predecessor would be a dangling reference, so the link is cut
        # rather than the survivor deleted.
        survivors = list(
            (
                await session.execute(
                    select(HealthAlert)
                    .where(
                        HealthAlert.org_id == org_id,
                        HealthAlert.previous_alert_id.in_(doomed),
                        HealthAlert.id.notin_(doomed),
                    )
                )
            )
            .scalars()
            .all()
        )
        for survivor in survivors:
            survivor.previous_alert_id = None
            session.add(survivor)
        await session.flush()
        await session.execute(
            delete(HealthAlert).where(
                HealthAlert.org_id == org_id, HealthAlert.id.in_(doomed)
            )
        )
        await session.flush()

    flap_cutoff = now - timedelta(seconds=settings.flap_window_s)
    pruned = (
        await session.execute(
            delete(HealthFlapState)
            .where(
                HealthFlapState.org_id == org_id,
                HealthFlapState.updated_at < flap_cutoff,
                ~select(HealthAlert.id)
                .where(
                    HealthAlert.org_id == HealthFlapState.org_id,
                    HealthAlert.device_id == HealthFlapState.device_id,
                    HealthAlert.check_id == HealthFlapState.check_id,
                    HealthAlert.selector == HealthFlapState.selector,
                    HealthAlert.state != "cleared",
                )
                .exists(),
            )
            .returning(HealthFlapState.id)
        )
    ).all()

    # THREE TABLES REFERENCE `health_check`, NOT ONE, and §12 only names the
    # first. Found by the test rather than by reading: `device_health_check_state`
    # and `health_flap_state` both carry a `check_id` FK, so deleting a retired
    # check with either row still present raises `ForeignKeyViolationError` and
    # takes the whole org's retention cycle down with it. Only `health_alert`
    # blocks the collection (§2.4: the alert must keep a resolvable `check_id`);
    # the other two are PER-KEY CURRENT STATE for a check that no longer exists,
    # so they are deleted alongside it rather than being allowed to make a
    # retired row immortal by accident.
    collectable = list(
        (
            await session.execute(
                select(HealthCheck.id).where(
                    HealthCheck.org_id == org_id,
                    HealthCheck.retired_at.is_not(None),
                    ~select(HealthAlert.id)
                    .where(
                        HealthAlert.org_id == HealthCheck.org_id,
                        HealthAlert.check_id == HealthCheck.id,
                    )
                    .exists(),
                )
            )
        )
        .scalars()
        .all()
    )
    retired: list[UUID] = []
    if collectable:
        from app.modules.health.models import DeviceHealthCheckState

        await session.execute(
            delete(DeviceHealthCheckState).where(
                DeviceHealthCheckState.org_id == org_id,
                DeviceHealthCheckState.check_id.in_(collectable),
            )
        )
        await session.execute(
            delete(HealthFlapState).where(
                HealthFlapState.org_id == org_id,
                HealthFlapState.check_id.in_(collectable),
            )
        )
        retired = list(
            (
                await session.execute(
                    delete(HealthCheck)
                    .where(HealthCheck.org_id == org_id, HealthCheck.id.in_(collectable))
                    .returning(HealthCheck.id)
                )
            )
            .scalars()
            .all()
        )
    await session.flush()

    return {
        "alerts_deleted": len(doomed),
        "flap_rows_pruned": len(pruned),
        "retired_checks_deleted": len(retired),
    }


# ---------------------------------------------------------------------------
# The `*_all_orgs` shells (`app/worker.py`'s established shape)
# ---------------------------------------------------------------------------


async def _org_ids(admin_engine: AsyncEngine) -> list[UUID]:
    async with AsyncSession(admin_engine) as session:
        return list((await session.execute(select(Organization.id))).scalars().all())


async def _for_each_org(
    *,
    engine: AsyncEngine,
    admin_engine: AsyncEngine,
    name: str,
    body: SweepBody,
    publish: PublishEnvelope | None = None,
) -> int:
    """ONE ORG PER TRANSACTION, EVERY ORG WRAPPED, PUBLISH AFTER COMMIT.

    The tenant GUC is `SET LOCAL`, so a transaction cannot span two tenants
    without un-pinning the first -- one org is the largest unit that can commit.
    One unsweepable org must never skip every org queued behind it: on a fleet
    where org #2 of 300 has a broken row, the alternative is that 298 orgs are
    never swept again and the only symptom is a log line.

    And the envelopes a body produced are published only once its
    `org_session` block has EXITED. `app/modules/deploy/scripts.py::run_script`
    states the reason in its own docstring: a job published before the caller's
    tenant transaction commits could be handed to an agent with no durable
    server record if the commit later failed.
    """
    total = 0
    for org_id in await _org_ids(admin_engine):
        try:
            async with org_session(org_id, engine=engine) as session:
                count, envelopes = await body(session, org_id)
        except Exception:  # noqa: BLE001
            logger.error(
                "health.%s: sweeping org %s failed; its cycle is skipped and every other "
                "org is still swept",
                name,
                org_id,
                exc_info=True,
            )
            continue
        # transaction committed
        if publish is not None:
            for job_id, envelope in envelopes:
                await publish(job_id, envelope)
        total += count
    return total


async def sweep_actions_all_orgs(
    *,
    engine: AsyncEngine,
    admin_engine: AsyncEngine,
    job_engine: _JobEngineLike | None = None,
    publish: PublishEnvelope | None = None,
) -> int:
    """Execute pending action rows for every org.

    `publish` is injected rather than imported. This module must not import
    `app.gateway.dispatch.publish_job` at all: `tests/test_health_egress.py`
    asserts that no file under `app/modules/health/` names it, which is spec
    §8.1's single-egress property made structural. The worker owns the Redis
    handle and passes the publisher in, and it is called only AFTER each org's
    transaction has committed.
    """

    async def body(session: AsyncSession, org_id: UUID) -> tuple[int, tuple[Envelope, ...]]:
        outcome = await run_alert_actions(session, org_id, job_engine=job_engine)
        return (
            outcome.dispatched + outcome.ticketed + outcome.notified,
            outcome.envelopes,
        )

    return await _for_each_org(
        engine=engine,
        admin_engine=admin_engine,
        name="alert_actions",
        body=body,
        publish=publish,
    )


async def sweep_alerts_all_orgs(*, engine: AsyncEngine, admin_engine: AsyncEngine) -> int:
    async def body(session: AsyncSession, org_id: UUID) -> tuple[int, tuple[Envelope, ...]]:
        return sum((await run_alert_sweep(session, org_id)).values()), ()

    return await _for_each_org(
        engine=engine, admin_engine=admin_engine, name="alert_sweep", body=body
    )


async def sweep_offline_all_orgs(*, engine: AsyncEngine, admin_engine: AsyncEngine) -> int:
    async def body(session: AsyncSession, org_id: UUID) -> tuple[int, tuple[Envelope, ...]]:
        return sum((await run_offline_sweep(session, org_id)).values()), ()

    return await _for_each_org(
        engine=engine, admin_engine=admin_engine, name="offline_sweep", body=body
    )


async def sweep_policy_reconcile_all_orgs(
    *,
    engine: AsyncEngine,
    admin_engine: AsyncEngine,
    job_engine: _JobEngineLike,
    publish: PublishEnvelope | None = None,
) -> int:
    async def body(session: AsyncSession, org_id: UUID) -> tuple[int, tuple[Envelope, ...]]:
        pushes = await run_policy_reconcile(session, org_id, job_engine=job_engine)
        return len(pushes), tuple(pushes)

    return await _for_each_org(
        engine=engine,
        admin_engine=admin_engine,
        name="policy_reconcile",
        body=body,
        publish=publish,
    )


async def sweep_retention_all_orgs(*, engine: AsyncEngine, admin_engine: AsyncEngine) -> int:
    async def body(session: AsyncSession, org_id: UUID) -> tuple[int, tuple[Envelope, ...]]:
        return sum((await run_retention(session, org_id)).values()), ()

    return await _for_each_org(
        engine=engine, admin_engine=admin_engine, name="retention", body=body
    )


async def sweep_baseline_all_orgs(*, engine: AsyncEngine, admin_engine: AsyncEngine) -> int:
    """Seed the default baseline for any org not yet decided (`baseline.py`).
    Returns the number of orgs seeded this pass."""

    async def body(session: AsyncSession, org_id: UUID) -> tuple[int, tuple[Envelope, ...]]:
        return (1 if await ensure_baseline(session, org_id) == "seeded" else 0), ()

    return await _for_each_org(
        engine=engine, admin_engine=admin_engine, name="baseline", body=body
    )
