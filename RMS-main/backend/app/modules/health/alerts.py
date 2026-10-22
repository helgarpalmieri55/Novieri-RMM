"""The alert lifecycle: what opens an alert, what clears it, and who may say so
(`docs/modules/endpoint-health-alerting/spec.md` §0.8, §2.4, §9.1).

FOUR STATES AND SIX CLEAR REASONS, AND *WHICH* ONE FIRED MATTERS. §0.8 is
explicit: an alert cleared by `recovery` is a fixed problem, an alert cleared by
`unavailable_timeout` is a REMOVED SENSOR and must not be counted as a
resolution in any MTTR figure. `cleared_reason` is therefore never a
convenience string -- it is the attribution reporting is required to separate on
(§13), and `clear_alert` refuses a reason outside the enum before Postgres has
to.

THE CLAUSE DELIBERATELY NOT PRESENT: an alert does not clear because the device
went offline. Offline is not healthy. Without that exclusion a machine that
crashes under memory pressure "recovers" every one of its alerts at the instant
it dies, and the fleet looks healthiest at exactly the moment it is not. There
is no `device_offline` member of the enum, on purpose, and
`tests/test_health_clear_predicate.py` pins the absence behaviourally rather
than by inspecting the enum.

CLEARING IS IDEMPOTENT AND SAYS SO IN ITS RETURN VALUE. `clear_alert` returns
`False` for an alert that is already cleared rather than raising or re-stamping:
ingestion is at-least-once (a redelivered recovery, a summary repairing a lost
edge) and every sweep may run twice. A caller that needs to know whether THIS
call did the work reads the bool; one that does not can ignore it. Re-stamping
would move `cleared_at` on every redelivery and turn MTTR into a measure of how
often the agent retried.

`muted_until` IS CLEARED ALONGSIDE THE STATE, because
`ck_health_alert_muted_until_matches_state` reads `(state = 'muted') =
(muted_until IS NOT NULL)`. Clearing a muted alert without nulling it is an
`IntegrityError` at flush -- which is the constraint doing its job, and is why
the reset lives here once instead of at each of the five call sites.

EVENTS ARE PUBLISHED ONLY FOR A CHECK THAT DECLARES `notify` (§7.3). See
`app/modules/health/declared_actions.py` for why that declaration is currently
interim and what it costs.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import Device, Ticket
from app.modules.health.declared_actions import declared_action_kinds
from app.modules.health.errors import HealthNotFoundError, HealthValidationError
from app.modules.health.models import HealthAlert, HealthAlertAction, HealthCheck
from app.modules.health.registry import CHECK_TYPES, SEVERITIES, render_threshold
from app.modules.health.settings import EffectiveHealthSettings, get_settings

__all__ = [
    "ALERT_STATES",
    "CLEARED_REASONS",
    "MAX_MUTE_DAYS",
    "OPEN_ALERT_STATES",
    "AlertSummary",
    "acknowledge",
    "alert_event_payload",
    "clear_alert",
    "clear_for_device_inactive",
    "close",
    "get_alert",
    "link_previous_alert",
    "list_alerts",
    "mute",
    "open_alert_for_key",
    "publish_alert_event",
    "summarise_open_alerts",
]

logger = logging.getLogger("its.health.alerts")

ALERT_STATES: tuple[str, ...] = ("active", "acknowledged", "muted", "cleared")

# WHAT "OPEN" MEANS, IN ONE PLACE ON THIS SIDE OF THE WIRE (§0.8). An
# acknowledged alert is OWNED and still open; a muted one is SILENCED and still
# open; only `cleared` is over. The console's `healthState.ts::isAlertOpen` says
# exactly this and is the browser's copy of it -- two homes for one rule, which
# is tolerable only because both are derived from ALERT_STATES here and pinned
# by tests on both sides. Every query below expresses it as `state <> 'cleared'`
# rather than `state IN (...)`, because that is the predicate carried by
# `uq_health_alert_open_key` and `ix_health_alert_open_opened_at`: the two
# partial indexes Postgres can only use if the predicate matches theirs.
OPEN_ALERT_STATES: tuple[str, ...] = tuple(s for s in ALERT_STATES if s != "cleared")

# Ascending seriousness, and the reason this is a tuple rather than `max()` over
# the strings: sorted alphabetically the order is warning > info > critical,
# which puts the calmest word on top of a summary meant to say how bad it is.
SEVERITY_ORDER: tuple[str, ...] = ("info", "warning", "critical")

# The registry owns which severities exist; this owns which is worse. Ranking a
# set that has drifted from the registry's would silently drop the new one out
# of `worst_severity` -- the field whose whole job is to name the worst thing
# open -- so the two are pinned to each other at import, the same way
# `schema.py` pins its `Severity` literal.
assert set(SEVERITY_ORDER) == set(SEVERITIES), (
    f"SEVERITY_ORDER {SEVERITY_ORDER} does not rank exactly the registry's {SEVERITIES}"
)

# §0.8's six, restated here because SQL's CHECK cannot produce a typed 400 and
# a caller passing "recovered" (the plausible typo -- the notification sample
# payload in the kernel's own test file carries exactly that word) would
# otherwise get an `IntegrityError` naming a constraint instead of the reason.
CLEARED_REASONS: tuple[str, ...] = (
    "recovery",
    "summary_ok",
    "unavailable_timeout",
    "policy_removed",
    "device_inactive",
    "manual",
)

# §0.8: "a mute is time-boxed on purpose: unbounded mutes are how a fleet
# accumulates permanently silent alarms that everyone believes are still armed."
# The database CHECK is the authority; this is the message.
MAX_MUTE_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Reads. Every one carries an explicit `org_id` predicate in addition to RLS
# (invariant #5): a sweep and a test both run on the admin engine, where RLS is
# not the thing doing the filtering.
# ---------------------------------------------------------------------------


async def get_alert(session: AsyncSession, org_id: UUID, alert_id: UUID) -> HealthAlert:
    """One alert, or `HealthNotFoundError` -- 404 and never 403 (see
    `errors.py`: a 403 confirms the id exists, which is a cross-tenant
    existence oracle)."""
    alert = (
        await session.execute(
            select(HealthAlert).where(HealthAlert.org_id == org_id, HealthAlert.id == alert_id)
        )
    ).scalar_one_or_none()
    if alert is None:
        raise HealthNotFoundError(tr("health alert {alert_id} not found", alert_id=alert_id))
    return alert


async def list_alerts(
    session: AsyncSession,
    org_id: UUID,
    *,
    state: str | None = None,
    severity: str | None = None,
    device_id: UUID | None = None,
    limit: int = 200,
) -> list[HealthAlert]:
    """The console list (§13). Newest first, bounded."""
    statement = select(HealthAlert).where(HealthAlert.org_id == org_id)
    if state is not None:
        statement = statement.where(HealthAlert.state == state)
    if severity is not None:
        statement = statement.where(HealthAlert.severity == severity)
    if device_id is not None:
        statement = statement.where(HealthAlert.device_id == device_id)
    statement = statement.order_by(HealthAlert.opened_at.desc()).limit(limit)
    return list((await session.execute(statement)).scalars().all())


@dataclass(frozen=True)
class AlertSummary:
    """What is open across one org, right now, and how bad it is.

    `open` is a TOTAL, not a page. That distinction is the whole reason this
    type exists: the shell's health glyph used to read one page of
    `list_alerts` and count it in the browser, so on a fleet with more than a
    page of open alerts the badge under-reported and had to say "at least N".

    EVERY BUCKET IS PRESENT AND ZERO rather than absent. A caller that has to
    tell "none of that severity are open" from "this server does not report
    that severity" cannot do it from a dict with a missing key, and the second
    reading is the one that quietly becomes a green light.

    `worst_severity` IS THE ONE NULLABLE FIELD, and `None` means nothing is
    open -- not "unknown". A caller that could not take the reading at all
    never gets an `AlertSummary`; it gets an error.
    """

    open: int
    by_state: dict[str, int]
    by_severity: dict[str, int]
    worst_severity: str | None


async def summarise_open_alerts(session: AsyncSession, org_id: UUID) -> AlertSummary:
    """The org's open-alert totals, counted BY POSTGRES.

    ONE `GROUP BY` STATEMENT, and the shape is the point: it returns at most
    one row per `(state, severity)` pair -- nine, ever -- whatever the size of
    the fleet. Loading the alerts and counting them in Python would answer the
    same numbers on a test fixture and reintroduce the defect this closes on a
    real one, which is why `tests/test_health_alerts.py` asserts the emitted
    SQL as well as the counts.

    `state <> 'cleared'` rather than `state IN ('active', 'acknowledged',
    'muted')`: §0.8's definition of open either way, but that is also the
    predicate on `ix_health_alert_open_opened_at`, so it is the form Postgres
    can serve from the partial index.

    THE `org_id` PREDICATE IS NOT DECORATION (invariant #5). Callers reach this
    through `org_session`, whose role is NOBYPASSRLS, so RLS would reject
    another org's rows even if this line were deleted -- and every route test
    would stay green while the sweep, the worker and any admin-engine caller
    counted the whole database. `tests/test_health_alerts.py` exercises this on
    the BYPASSRLS engine for exactly that reason.
    """
    rows = (
        await session.execute(
            select(HealthAlert.state, HealthAlert.severity, func.count().label("n"))
            .where(HealthAlert.org_id == org_id, HealthAlert.state != "cleared")
            .group_by(HealthAlert.state, HealthAlert.severity)
        )
    ).all()

    by_state = dict.fromkeys(OPEN_ALERT_STATES, 0)
    by_severity = dict.fromkeys(SEVERITY_ORDER, 0)
    total = 0
    for state, severity, count in rows:
        total += count
        # A state or severity outside the enums cannot exist -- both are CHECK
        # constraints -- but a row that somehow carried one must not be dropped
        # from the total, because the total is what an operator reads.
        if state in by_state:
            by_state[state] += count
        if severity in by_severity:
            by_severity[severity] += count

    worst = next(
        (name for name in reversed(SEVERITY_ORDER) if by_severity[name] > 0), None
    )
    return AlertSummary(
        open=total, by_state=by_state, by_severity=by_severity, worst_severity=worst
    )


async def open_alert_for_key(
    session: AsyncSession,
    org_id: UUID,
    *,
    device_id: UUID,
    check_id: UUID,
    selector: str,
) -> HealthAlert | None:
    """The non-cleared alert for §0.8's dedup key, if one exists.

    §0.8 clause 3 is enforced by `uq_health_alert_open_key` AND checked here, so
    the ordinary duplicate-breach path is an UPDATE rather than a caught
    `IntegrityError` -- which matters because an `IntegrityError` aborts the
    whole ingest transaction, taking a recovery in the same batch with it.
    """
    return (
        await session.execute(
            select(HealthAlert).where(
                HealthAlert.org_id == org_id,
                HealthAlert.device_id == device_id,
                HealthAlert.check_id == check_id,
                HealthAlert.selector == selector,
                HealthAlert.state != "cleared",
            )
        )
    ).scalar_one_or_none()


async def link_previous_alert(
    session: AsyncSession,
    org_id: UUID,
    *,
    device_id: UUID,
    check_id: UUID,
    selector: str,
    now: datetime,
    window_s: int,
) -> UUID | None:
    """The most recently cleared alert for this key, if it cleared inside
    `flap_window_s` (§2.4, §0.10).

    WINDOW-BOUNDED ON PURPOSE. A re-opened alert's ticket naming its predecessor
    is what makes a chronic flapper legible; a link to an alert from four months
    ago is noise wearing the same shape. The window is the org's own
    `flap_window_s`, so the same tuning that decides "this key is oscillating"
    decides "this open is a continuation".
    """
    cutoff = now - timedelta(seconds=window_s)
    return (
        await session.execute(
            select(HealthAlert.id)
            .where(
                HealthAlert.org_id == org_id,
                HealthAlert.device_id == device_id,
                HealthAlert.check_id == check_id,
                HealthAlert.selector == selector,
                HealthAlert.state == "cleared",
                HealthAlert.cleared_at.is_not(None),
                HealthAlert.cleared_at >= cutoff,
            )
            .order_by(HealthAlert.cleared_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# The §9.1 payload, built in one place
# ---------------------------------------------------------------------------


async def alert_event_payload(
    session: AsyncSession,
    org_id: UUID,
    alert: HealthAlert,
    *,
    settings: EffectiveHealthSettings,
    comparator: str = "",
    threshold: str = "",
) -> dict[str, Any]:
    """§9.1's payload contract, every key present.

    `queue_id` IS NON-NULL WHERE THE ORG MADE IT POSSIBLE, and that is a
    discovered constraint rather than decoration: `routing.resolve_recipients`
    narrows rule selection to `queue_id IS NULL` rules when a payload carries no
    queue, and `queue_members` then expands against a NULL queue and returns
    `[]`. A queue-less health event is routable only to `channel_default` -- one
    webhook for the whole org, which is not an on-call rota. So the queue is the
    alert ticket's if one was opened, else
    `health_org_settings.default_alert_queue_id`.

    `assignee_id`/`requester_id` are present-and-null so the "every id present"
    convention holds and a reader never has to tell "absent" from "null".
    `"notify"` is deliberately ABSENT (§7.3's amendment): a field no consumer
    can act on is a field a future reader will assume works.
    """
    hostname = (
        await session.execute(
            select(Device.hostname).where(Device.org_id == org_id, Device.id == alert.device_id)
        )
    ).scalar_one_or_none() or ""

    ticket_number: str | None = None
    queue_id: UUID | None = settings.default_alert_queue_id
    if alert.ticket_id is not None:
        row = (
            await session.execute(
                select(Ticket.number, Ticket.queue_id).where(
                    Ticket.org_id == org_id, Ticket.id == alert.ticket_id
                )
            )
        ).one_or_none()
        if row is not None:
            ticket_number = row[0]
            queue_id = row[1]

    remediation = (
        await session.execute(
            select(HealthAlertAction.job_id).where(
                HealthAlertAction.org_id == org_id,
                HealthAlertAction.alert_id == alert.id,
                HealthAlertAction.kind == "remediate",
            )
        )
    ).scalar_one_or_none()

    return {
        "alert_id": str(alert.id),
        "device_id": str(alert.device_id),
        "hostname": hostname,
        "check_type": alert.check_type,
        "selector": alert.selector,
        "severity": alert.severity,
        "state": alert.state,
        "observed_value": alert.observed_value,
        "threshold": threshold or alert.threshold_display,
        "comparator": comparator,
        "opened_at": alert.opened_at.isoformat(),
        "cleared_at": alert.cleared_at.isoformat() if alert.cleared_at else None,
        "cleared_reason": alert.cleared_reason,
        "flapping": alert.flapping,
        "ticket_id": str(alert.ticket_id) if alert.ticket_id else None,
        "ticket_number": ticket_number,
        "remediation_job_id": str(remediation) if remediation else None,
        "remediation_action_key": None,
        "queue_id": str(queue_id) if queue_id else None,
        "assignee_id": None,
        "requester_id": None,
    }


async def publish_alert_event(
    session: AsyncSession,
    org_id: UUID,
    alert: HealthAlert,
    *,
    event_type: str,
    check: HealthCheck | None,
    settings: EffectiveHealthSettings,
    actor_user_id: UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Publish one `health.*` event for `alert`, IF its check declares `notify`.

    Returns whether anything was published, so a caller can assert on a value
    rather than on the absence of an exception. §7.3: "silence is the absence of
    an event, not a flag on one" -- `routing.resolve_recipients` cannot read a
    payload key, so a `"notify": false` marker would have decided nothing.
    """
    if check is None:
        return False
    if "notify" not in declared_action_kinds(check, settings):
        return False
    comparator = check.comparator
    entry = CHECK_TYPES.get(check.check_type)
    threshold = ""
    if entry is not None:
        try:
            threshold = render_threshold(
                entry, threshold_num=check.threshold_num, threshold_bool=check.threshold_bool
            )
        except ValueError:  # pragma: no cover - a row the registry cannot render
            threshold = alert.threshold_display
    payload = await alert_event_payload(
        session, org_id, alert, settings=settings, comparator=comparator, threshold=threshold
    )
    if extra:
        payload.update(extra)
    await events.publish(
        session,
        org_id=org_id,
        event_type=event_type,
        subject_type="health_alert",
        subject_id=alert.id,
        payload=payload,
        actor_user_id=actor_user_id,
    )
    return True


# ---------------------------------------------------------------------------
# Clearing (§0.8's six clauses converge here)
# ---------------------------------------------------------------------------


async def clear_alert(
    session: AsyncSession,
    org_id: UUID,
    alert: HealthAlert,
    *,
    reason: str,
    now: datetime,
    actor_id: UUID | None = None,
    check: HealthCheck | None = None,
    settings: EffectiveHealthSettings | None = None,
) -> bool:
    """Move `alert` to `cleared` with `reason`. Returns whether this call did it.

    Every one of §0.8's six clauses lands here, so the state transition, the
    `muted_until` reset, the outbox publish and (from §7.4, wired by the action
    layer) the ticket's auto-resolution have exactly one implementation. Six
    call sites each doing four things is how one of them comes to do three.
    """
    if reason not in CLEARED_REASONS:
        raise HealthValidationError(
            tr(
                "cleared_reason must be one of {cleared_reasons}; got {reason}",
                cleared_reasons=list(CLEARED_REASONS),
                reason=repr(reason),
            )
        )
    if alert.state == "cleared":
        return False

    alert.state = "cleared"
    alert.cleared_at = now
    alert.cleared_reason = reason
    alert.cleared_by = actor_id
    # `ck_health_alert_muted_until_matches_state` is `(state='muted') =
    # (muted_until IS NOT NULL)`: a muted alert that clears must drop the stamp
    # in the same statement or the flush fails.
    alert.muted_until = None
    alert.updated_at = now
    session.add(alert)
    await session.flush()

    if settings is None:
        settings = await get_settings(session, org_id)
    if check is None:
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
        event_type="health.alert_cleared",
        check=check,
        settings=settings,
        actor_user_id=actor_id,
    )
    await _auto_resolve_ticket(session, org_id, alert, now=now, settings=settings)
    await session.flush()
    return True


async def _auto_resolve_ticket(
    session: AsyncSession,
    org_id: UUID,
    alert: HealthAlert,
    *,
    now: datetime,
    settings: EffectiveHealthSettings,
) -> None:
    """§7.4's hook, resolved with a FUNCTION-LOCAL import.

    `actions.py` imports `alerts.py` for `publish_alert_event`, so a module-level
    import here would be a cycle. A callback registry would be worse than the
    cycle: it would make "does clearing an alert resolve its ticket?" depend on
    which modules a given process happened to import, and the answer to that
    must not vary between the API, the gateway and the worker.
    """
    from app.modules.health.actions import auto_resolve_alert_ticket

    await auto_resolve_alert_ticket(session, org_id, alert, now=now, settings=settings)


async def clear_for_device_inactive(
    session: AsyncSession, org_id: UUID, device_id: UUID, *, now: datetime | None = None
) -> int:
    """§0.8 clause 5: the device left `devices.status = 'active'`.

    Distinct from going OFFLINE, which clears nothing (see the module
    docstring). `status` is an administrative fact -- a decommissioned or
    retired device -- so its open alerts are about a machine nobody is
    responsible for any more; `agents.last_seen_at` going quiet is the opposite,
    a machine nobody can reach.
    """
    now = now or _utcnow()
    alerts = list(
        (
            await session.execute(
                select(HealthAlert).where(
                    HealthAlert.org_id == org_id,
                    HealthAlert.device_id == device_id,
                    HealthAlert.state != "cleared",
                )
            )
        )
        .scalars()
        .all()
    )
    settings = await get_settings(session, org_id)
    cleared = 0
    for alert in alerts:
        if await clear_alert(
            session, org_id, alert, reason="device_inactive", now=now, settings=settings
        ):
            cleared += 1
    return cleared


# ---------------------------------------------------------------------------
# The human transitions (§0.8, §11.3)
# ---------------------------------------------------------------------------


async def acknowledge(
    session: AsyncSession,
    org_id: UUID,
    alert_id: UUID,
    *,
    actor_id: UUID,
    now: datetime | None = None,
) -> HealthAlert:
    """`active -> acknowledged`: a named user takes ownership (§0.8).

    WHAT ACKNOWLEDGE DOES NOT DO IS THE WHOLE CONTENT OF THE STATE. It does not
    stop the check, it does not prevent auto-clear, and it does not cancel a
    remediation already in flight. It suppresses re-notification and escalation
    (§12's sweep skips `acknowledged`) and nothing else. An acknowledge that
    also stopped auto-clear would leave a fixed problem showing as an owned one
    forever, which is §0.8's failure mode (a) arriving through the front door.
    """
    now = now or _utcnow()
    alert = await get_alert(session, org_id, alert_id)
    if alert.state != "active":
        raise HealthValidationError(
            tr(
                "alert {alert_id} is {alert_state}; only an active alert can be acknowledged",
                alert_id=alert_id,
                alert_state=repr(alert.state),
            )
        )
    alert.acknowledged_at = now
    alert.acknowledged_by = actor_id
    alert.state = "acknowledged"
    alert.updated_at = now
    session.add(alert)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="health.alert_acknowledged",
        target_type="health_alert",
        target_id=str(alert.id),
        metadata={"check_type": alert.check_type, "selector": alert.selector},
    )
    await session.flush()
    return alert


async def mute(
    session: AsyncSession,
    org_id: UUID,
    alert_id: UUID,
    *,
    actor_id: UUID,
    muted_until: datetime,
    now: datetime | None = None,
) -> HealthAlert:
    """`active|acknowledged -> muted`, bounded to 30 days (§0.8).

    The bound is refused HERE with a typed 400 and refused AGAIN by
    `ck_health_alert_mute_bounded`. Neither is redundant: without the service
    check a 31-day mute is a 500 from an `IntegrityError` that names no bound,
    and without the CHECK the bound survives no second caller and no direct SQL.
    """
    now = now or _utcnow()
    alert = await get_alert(session, org_id, alert_id)
    if alert.state not in ("active", "acknowledged"):
        raise HealthValidationError(
            tr(
                "alert {alert_id} is {alert_state}; only an active or acknowledged alert "
                "can be muted",
                alert_id=alert_id,
                alert_state=repr(alert.state),
            )
        )
    if muted_until.tzinfo is None:
        muted_until = muted_until.replace(tzinfo=UTC)
    if muted_until <= now:
        raise HealthValidationError(tr("muted_until must be in the future"))
    limit = alert.opened_at + timedelta(days=MAX_MUTE_DAYS)
    if muted_until > limit:
        raise HealthValidationError(
            tr(
                "muted_until must be at most {max_mute_days} days after the alert opened "
                "({limit})",
                max_mute_days=MAX_MUTE_DAYS,
                limit=limit.isoformat(),
            )
        )
    alert.state = "muted"
    alert.muted_until = muted_until
    alert.muted_by = actor_id
    alert.updated_at = now
    session.add(alert)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="health.alert_muted",
        target_type="health_alert",
        target_id=str(alert.id),
        metadata={"muted_until": muted_until.isoformat()},
    )
    await session.flush()
    return alert


async def close(
    session: AsyncSession,
    org_id: UUID,
    alert_id: UUID,
    *,
    actor_id: UUID,
    now: datetime | None = None,
) -> HealthAlert:
    """§0.8 clause 6: a user with `health.alert.manage` closes the alert.

    `cleared_reason='manual'` and `cleared_by` are what separate this from every
    machine-driven clear in reporting -- an alert a human closed is not evidence
    that anything was fixed.
    """
    now = now or _utcnow()
    alert = await get_alert(session, org_id, alert_id)
    if alert.state == "cleared":
        raise HealthValidationError(tr("alert {alert_id} is already cleared", alert_id=alert_id))
    await clear_alert(session, org_id, alert, reason="manual", now=now, actor_id=actor_id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="health.alert_closed",
        target_type="health_alert",
        target_id=str(alert.id),
        metadata={"check_type": alert.check_type, "selector": alert.selector},
    )
    await session.flush()
    return alert
