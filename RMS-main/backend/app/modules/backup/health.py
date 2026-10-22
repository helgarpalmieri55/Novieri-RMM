"""Backup health checks (spec-server-backup.md §8.1).

Four server-evaluated checks, registered in the health module's closed
registry and evaluated here from the backup tables. Alerts, notification and
tickets are the health module's own (`open_alert_for_key`, `clear_alert`, the
`health_alert_action` pipeline), exactly as `run_offline_sweep` uses them; this
module adds no ticket code.

Only devices with an enabled backup policy are evaluated, so nothing pages
about a machine nobody asked to protect. A device whose policy goes away has
its open backup alerts cleared.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.modules.backup.models import (
    BackupPoint,
    BackupPointCopy,
    BackupRepository,
    BackupRun,
)
from app.modules.backup.policy import ResolvedBackupPolicy, resolve_backup_policy
from app.modules.health.alerts import clear_alert, open_alert_for_key, publish_alert_event
from app.modules.health.declared_actions import approved_action_id_for, declared_action_kinds
from app.modules.health.models import HealthAlert, HealthAlertAction, HealthCheck
from app.modules.health.resolution import resolve_profile_for_device
from app.modules.health.settings import EffectiveHealthSettings, get_settings

SUCCESS_AGE = "backup.success_age_h"
LAST_RUN_FAILED = "backup.last_run_failed"
OFFSITE_LAG = "backup.offsite_lag_h"
RESTORE_TEST_AGE = "backup.restore_test_age_d"
BACKUP_CHECK_TYPES = frozenset({SUCCESS_AGE, LAST_RUN_FAILED, OFFSITE_LAG, RESTORE_TEST_AGE})

_UNITS = {SUCCESS_AGE: "hours", OFFSITE_LAG: "hours", RESTORE_TEST_AGE: "days"}
_WORDS = {"gt": "more than", "gte": "at least", "lt": "less than", "lte": "at most",
          "eq": "exactly", "neq": "not"}


@dataclass(frozen=True)
class BackupFacts:
    protected_since: datetime
    last_point_at: datetime | None
    last_finished_state: str | None
    offsite_on: bool
    last_point_offsite_complete: bool | None
    last_restore_tested_at: datetime | None


def evaluate(check_type: str, facts: BackupFacts, now: datetime) -> float | bool | None:
    """The observed value, or None when the check does not apply."""
    if check_type == SUCCESS_AGE:
        since = facts.last_point_at or facts.protected_since
        return (now - since).total_seconds() / 3600
    if check_type == LAST_RUN_FAILED:
        if facts.last_finished_state is None:
            return None
        return facts.last_finished_state in ("failed", "missed")
    if check_type == OFFSITE_LAG:
        if not facts.offsite_on or facts.last_point_at is None:
            return None
        if facts.last_point_offsite_complete:
            return 0.0
        return (now - facts.last_point_at).total_seconds() / 3600
    if check_type == RESTORE_TEST_AGE:
        since = facts.last_restore_tested_at or facts.protected_since
        return (now - since).total_seconds() / 86400
    return None


def breached(check: HealthCheck, value: float | bool) -> bool:
    if isinstance(value, bool):
        return check.threshold_bool is not None and value == check.threshold_bool
    if check.threshold_num is None:
        return False
    threshold = float(Decimal(str(check.threshold_num)))
    return {
        "gt": value > threshold,
        "gte": value >= threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
        "eq": value == threshold,
        "neq": value != threshold,
    }.get(check.comparator, False)


def _observed(value: float | bool) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:.1f}"


def _threshold_display(check: HealthCheck) -> str:
    if check.threshold_num is None:
        return "last backup failed or was missed"
    number = format(Decimal(str(check.threshold_num)).normalize(), "f")
    return f"{_WORDS.get(check.comparator, check.comparator)} {number} " + _UNITS.get(
        check.check_type, ""
    )


async def collect_facts(
    session: AsyncSession, org_id: UUID, device: Device, policy: ResolvedBackupPolicy
) -> BackupFacts:
    last_point = (
        await session.execute(
            select(BackupPoint)
            .where(
                BackupPoint.org_id == org_id,
                BackupPoint.device_id == device.id,
                BackupPoint.state == "complete",
            )
            .order_by(BackupPoint.taken_at.desc(), BackupPoint.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    last_state = (
        await session.execute(
            select(BackupRun.state)
            .where(
                BackupRun.org_id == org_id,
                BackupRun.device_id == device.id,
                BackupRun.state != "running",
            )
            .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    tested = (
        await session.execute(
            select(func.max(BackupPoint.restore_tested_at)).where(
                BackupPoint.org_id == org_id, BackupPoint.device_id == device.id
            )
        )
    ).scalar_one()
    target_id = (
        await session.execute(
            select(BackupRepository.offsite_target_id).where(
                BackupRepository.org_id == org_id,
                BackupRepository.id == policy.repository_id,
            )
        )
    ).scalar_one_or_none()
    offsite_complete: bool | None = None
    if target_id is not None and last_point is not None:
        state = (
            await session.execute(
                select(BackupPointCopy.state).where(
                    BackupPointCopy.org_id == org_id,
                    BackupPointCopy.point_id == last_point.id,
                    BackupPointCopy.target_id == target_id,
                )
            )
        ).scalar_one_or_none()
        offsite_complete = state == "complete"
    since = [t for t in (policy.created_at, device.created_at) if t is not None]
    return BackupFacts(
        protected_since=max(since),
        last_point_at=last_point.taken_at if last_point is not None else None,
        last_finished_state=last_state,
        offsite_on=target_id is not None,
        last_point_offsite_complete=offsite_complete,
        last_restore_tested_at=tested,
    )


async def _open(
    session: AsyncSession,
    org_id: UUID,
    device: Device,
    check: HealthCheck,
    profile: Any,
    value: float | bool,
    settings: EffectiveHealthSettings,
    now: datetime,
) -> bool:
    existing = await open_alert_for_key(
        session, org_id, device_id=device.id, check_id=check.id, selector=""
    )
    if existing is not None:
        existing.last_seen_at = now
        existing.occurrence_count += 1
        existing.observed_value = _observed(value)[:64]
        existing.updated_at = now
        await session.flush()
        return False
    alert = HealthAlert(
        org_id=org_id,
        device_id=device.id,
        check_id=check.id,
        profile_id=profile.id,
        selector="",
        check_type=check.check_type,
        severity=check.severity,
        state="active",
        observed_value=_observed(value)[:64],
        threshold_display=_threshold_display(check)[:64],
        opened_at=now,
        opened_policy_version=profile.policy_version,
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
                approved_action_id=approved_action_id_for(check, kind),
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    await publish_alert_event(
        session, org_id, alert, event_type="health.alert_raised", check=check, settings=settings
    )
    return True


async def _clear_all(
    session: AsyncSession,
    org_id: UUID,
    device_id: UUID,
    settings: EffectiveHealthSettings,
    now: datetime,
) -> int:
    alerts = (
        await session.execute(
            select(HealthAlert).where(
                HealthAlert.org_id == org_id,
                HealthAlert.device_id == device_id,
                HealthAlert.check_type.in_(BACKUP_CHECK_TYPES),
                HealthAlert.state != "cleared",
            )
        )
    ).scalars().all()
    cleared = 0
    for alert in alerts:
        if await clear_alert(
            session, org_id, alert, reason="policy_removed", now=now, settings=settings
        ):
            cleared += 1
    return cleared


async def run_backup_health_sweep(
    session: AsyncSession, org_id: UUID, *, now: datetime
) -> dict[str, int]:
    """Evaluate the backup checks for every protected device in `org_id`.
    Returns `{opened, cleared}`. Idempotent by the open-alert key."""
    settings = await get_settings(session, org_id)
    devices = (
        await session.execute(
            select(Device).where(
                Device.org_id == org_id,
                Device.status == "active",
                Device.lifecycle == "in_service",
                Device.os_family.is_not(None),
            )
        )
    ).scalars().all()
    opened = cleared = 0
    for device in devices:
        policy = await resolve_backup_policy(session, org_id, device.id)
        if policy is None or policy.repository_id is None:
            cleared += await _clear_all(session, org_id, device.id, settings, now)
            continue
        resolved = await resolve_profile_for_device(session, org_id, device)
        checks = [
            c for c in (resolved.checks if resolved else []) if c.check_type in BACKUP_CHECK_TYPES
        ]
        if not checks or resolved is None:
            continue
        facts = await collect_facts(session, org_id, device, policy)
        for check in checks:
            value = evaluate(check.check_type, facts, now)
            if value is not None and breached(check, value):
                opened += int(
                    await _open(session, org_id, device, check, resolved.profile, value,
                                settings, now)
                )
                continue
            existing = await open_alert_for_key(
                session, org_id, device_id=device.id, check_id=check.id, selector=""
            )
            if existing is not None and await clear_alert(
                session, org_id, existing, reason="recovery", now=now, check=check,
                settings=settings,
            ):
                cleared += 1
    await session.flush()
    return {"opened": opened, "cleared": cleared}
