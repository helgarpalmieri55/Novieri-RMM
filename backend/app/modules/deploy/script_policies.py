"""Scheduled scripts: script x device group x interval (app-deploy spec §9).

A policy names a script, a device group and how often it runs on each device.
`run_policy_sweep` (beat, 15 min) dispatches the ordinary signed `script.run`
with the script's current body to each connected device in the group whose
last run for the policy is older than the interval and, when the policy says
so, whose patch maintenance window is open. Output is kept on the run and
cleared past `keep_runs` per device. With `alert_on_failure`, a failed run
opens a health alert through the device's health profile, which decides
severity and whether a ticket opens; the next successful run clears it.

Every write is audited; every query carries `org_id` (invariant #5).
"""

from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.schedule import in_window
from app.models import Agent, Device, DeviceGroup, Script, ScriptPolicy, ScriptRun
from app.modules.deploy.packages import DeployValidationError
from app.modules.deploy.scripts import dispatch_script_to_device
from app.modules.patch.policy import resolve_patch_policy

POLICY_FAILED_CHECK = "script.policy_failed"
# A run still `running` after this long lost its job; the device is due again.
IN_FLIGHT = timedelta(hours=2)
_EDITABLE = (
    "name",
    "interval_hours",
    "in_maintenance_window",
    "alert_on_failure",
    "keep_runs",
    "enabled",
)


class ScriptPolicyNotFound(DeployValidationError):
    pass


class _JobEngineLike(Protocol):
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
    ) -> Any: ...

    async def signed_envelope(self, job: Any) -> dict[str, Any]: ...


async def _audit(
    session: AsyncSession,
    org_id: UUID,
    action: str,
    policy_id: UUID,
    metadata: dict[str, Any],
    actor_id: UUID | None = None,
) -> None:
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action=action,
        target_type="script_policy",
        target_id=str(policy_id),
        metadata=metadata,
    )


async def get_policy(session: AsyncSession, org_id: UUID, policy_id: UUID) -> ScriptPolicy:
    row = (
        await session.execute(
            select(ScriptPolicy).where(ScriptPolicy.org_id == org_id, ScriptPolicy.id == policy_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ScriptPolicyNotFound(tr("no such schedule"))
    return row


async def _require_targets(
    session: AsyncSession, org_id: UUID, script_id: UUID, device_group_id: UUID
) -> UUID:
    """The script and the group exist here; returns the GROUP's client, which
    the schedule takes (trigger `its_client_from_parent`, migration 0231)."""
    script = (
        await session.execute(
            select(Script.id).where(Script.org_id == org_id, Script.id == script_id)
        )
    ).scalar_one_or_none()
    if script is None:
        raise DeployValidationError(tr("no such script"))
    group_client = (
        await session.execute(
            select(DeviceGroup.client_id).where(
                DeviceGroup.org_id == org_id, DeviceGroup.id == device_group_id
            )
        )
    ).scalar_one_or_none()
    if group_client is None:
        raise DeployValidationError(tr("no such device group"))
    return group_client


async def _name_taken(
    session: AsyncSession,
    org_id: UUID,
    client_id: UUID,
    name: str,
    exclude: UUID | None = None,
) -> bool:
    """Is `name` another schedule's in this CLIENT?

    Per client, as `uq_script_policies_client_name` is (migration 0231, Task
    8b): the check runs under the caller's scope, which may not see another
    client's schedules -- a per-organisation rule it cannot see would be one
    only the index could enforce, with a 500, and a refusal that named
    another client's schedule to a technician who cannot read it.
    """
    query = select(ScriptPolicy.id).where(
        ScriptPolicy.org_id == org_id,
        ScriptPolicy.client_id == client_id,
        ScriptPolicy.name == name,
    )
    if exclude is not None:
        query = query.where(ScriptPolicy.id != exclude)
    return (await session.execute(query)).first() is not None


#: The schedule-name key (migration 0231): what holds when two writes race past
#: `_name_taken`. Its unique violation is the check's own refusal.
_NAME_KEY = "uq_script_policies_client_name"


def _name_exists() -> DeployValidationError:
    return DeployValidationError(tr("a schedule with that name already exists"))


async def _flush_named(session: AsyncSession) -> None:
    """Flush, answering a race lost on the name key as `_name_taken` does --
    the route's 422 -- not a 500. Only that key's: another violation in the
    same flush is not a name clash and stays the error it is."""
    try:
        await session.flush()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        if constraint == _NAME_KEY:
            raise _name_exists() from None
        raise


async def create_policy(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    script_id: UUID,
    device_group_id: UUID,
    interval_hours: int,
    in_maintenance_window: bool,
    alert_on_failure: bool,
    keep_runs: int,
    enabled: bool,
) -> ScriptPolicy:
    client_id = await _require_targets(session, org_id, script_id, device_group_id)
    if await _name_taken(session, org_id, client_id, name):
        raise _name_exists()
    row = ScriptPolicy(
        org_id=org_id,
        name=name,
        script_id=script_id,
        device_group_id=device_group_id,
        interval_hours=interval_hours,
        in_maintenance_window=in_maintenance_window,
        alert_on_failure=alert_on_failure,
        keep_runs=keep_runs,
        enabled=enabled,
        created_by=actor_id,
    )
    session.add(row)
    await _flush_named(session)
    await _audit(
        session,
        org_id,
        "script_policy.created",
        row.id,
        {
            "name": name,
            "script_id": str(script_id),
            "device_group_id": str(device_group_id),
            "interval_hours": interval_hours,
            "in_maintenance_window": in_maintenance_window,
            "alert_on_failure": alert_on_failure,
            "enabled": enabled,
        },
        actor_id,
    )
    return row


async def update_policy(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    policy_id: UUID,
    changes: dict[str, Any],
) -> ScriptPolicy:
    row = await get_policy(session, org_id, policy_id)
    changed: dict[str, Any] = {}
    if "name" in changes and changes["name"] is not None:
        # The schedule's own client: its group never changes (not editable).
        if await _name_taken(session, org_id, row.client_id, changes["name"], exclude=row.id):
            raise _name_exists()
    for field in _EDITABLE:
        value = changes.get(field)
        if value is not None and getattr(row, field) != value:
            setattr(row, field, value)
            changed[field] = value
    await _flush_named(session)
    if changed:
        await _audit(session, org_id, "script_policy.updated", row.id, changed, actor_id)
    return row


async def delete_policy(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, policy_id: UUID
) -> None:
    """Delete the schedule. Its runs stay, as runs of the script."""
    row = await get_policy(session, org_id, policy_id)
    await session.execute(
        update(ScriptRun)
        .where(ScriptRun.org_id == org_id, ScriptRun.policy_id == row.id)
        .values(policy_id=None)
    )
    await session.delete(row)
    await session.flush()
    await _audit(session, org_id, "script_policy.deleted", policy_id, {"name": row.name}, actor_id)


async def policy_summaries(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """Every schedule with its script, group and latest outcome counts."""
    rows = (
        await session.execute(
            select(ScriptPolicy, Script.name, DeviceGroup.name)
            .join(Script, Script.id == ScriptPolicy.script_id)
            .join(DeviceGroup, DeviceGroup.id == ScriptPolicy.device_group_id)
            .where(
                ScriptPolicy.org_id == org_id,
                Script.org_id == org_id,
                DeviceGroup.org_id == org_id,
            )
            .order_by(ScriptPolicy.name)
        )
    ).all()
    out: list[dict[str, Any]] = []
    for policy, script_name, group_name in rows:
        latest = (
            select(ScriptRun.device_id, ScriptRun.state, ScriptRun.created_at)
            .where(ScriptRun.org_id == org_id, ScriptRun.policy_id == policy.id)
            .order_by(ScriptRun.device_id, ScriptRun.created_at.desc())
            .distinct(ScriptRun.device_id)
            .subquery()
        )
        stats = (
            await session.execute(
                select(
                    func.count().filter(latest.c.state == "failed"),
                    func.count().filter(latest.c.state == "succeeded"),
                    func.max(latest.c.created_at),
                )
            )
        ).one()
        out.append(
            {
                "id": policy.id,
                "name": policy.name,
                "script_id": policy.script_id,
                "script_name": script_name,
                "device_group_id": policy.device_group_id,
                "device_group_name": group_name,
                "interval_hours": policy.interval_hours,
                "in_maintenance_window": policy.in_maintenance_window,
                "alert_on_failure": policy.alert_on_failure,
                "keep_runs": policy.keep_runs,
                "enabled": policy.enabled,
                "devices_failing": stats[0],
                "devices_succeeding": stats[1],
                "last_run_at": stats[2],
            }
        )
    return out


async def policy_runs(
    session: AsyncSession, org_id: UUID, policy_id: UUID, limit: int
) -> list[dict[str, Any]]:
    await get_policy(session, org_id, policy_id)
    rows = (
        await session.execute(
            select(ScriptRun, Device.hostname)
            .join(Device, Device.id == ScriptRun.device_id)
            .where(
                ScriptRun.org_id == org_id,
                Device.org_id == org_id,
                ScriptRun.policy_id == policy_id,
            )
            .order_by(ScriptRun.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": run.id,
            "device_id": run.device_id,
            "hostname": hostname,
            "script_version": run.script_version,
            "state": run.state,
            "exit_code": run.exit_code,
            "output": run.output,
            "created_at": run.created_at,
            "finished_at": run.finished_at,
        }
        for run, hostname in rows
    ]


async def run_policy_sweep(
    session: AsyncSession, job_engine: _JobEngineLike, *, org_id: UUID, now: datetime
) -> list[tuple[UUID, UUID, dict[str, Any]]]:
    """Dispatch every due run in this org. Returns pending publishes for the
    caller to send after commit."""
    policies = (
        await session.execute(
            select(ScriptPolicy).where(ScriptPolicy.org_id == org_id, ScriptPolicy.enabled)
        )
    ).scalars().all()
    pending: list[tuple[UUID, UUID, dict[str, Any]]] = []
    for policy in policies:
        script = (
            await session.execute(
                select(Script).where(Script.org_id == org_id, Script.id == policy.script_id)
            )
        ).scalar_one_or_none()
        if script is None:
            continue
        device_ids = (
            await session.execute(
                select(Device.id)
                .join(Agent, Agent.device_id == Device.id)
                .where(
                    Device.org_id == org_id,
                    Device.device_group_id == policy.device_group_id,
                    Device.os_family == script.os_family,
                    Agent.status == "online",
                    # Dynamic groups place a link-enrolled machine the moment it
                    # joins; the review gate would refuse `script.run` to it and
                    # abort every policy in this org on every tick.
                    Device.review_state == "approved",
                )
            )
        ).scalars().all()
        dispatched = 0
        for device_id in device_ids:
            if not await _due(session, org_id, policy, device_id, now):
                continue
            if policy.in_maintenance_window:
                window = (await resolve_patch_policy(session, org_id, device_id)).maintenance_window
                if not in_window(window, now):
                    continue
            pending.append(
                await dispatch_script_to_device(
                    session,
                    job_engine,
                    org_id=org_id,
                    script=script,
                    device_id=device_id,
                    target_type="group",
                    target_id=policy.device_group_id,
                    actor_id=None,
                    policy_id=policy.id,
                )
            )
            dispatched += 1
        if dispatched:
            await _audit(
                session,
                org_id,
                "script_policy.dispatched",
                policy.id,
                {
                    "dispatched_count": dispatched,
                    "script_id": str(script.id),
                    "script_version": script.version,
                },
            )
    await session.flush()
    return pending


async def _due(
    session: AsyncSession, org_id: UUID, policy: ScriptPolicy, device_id: UUID, now: datetime
) -> bool:
    last = (
        await session.execute(
            select(ScriptRun.state, ScriptRun.created_at)
            .where(
                ScriptRun.org_id == org_id,
                ScriptRun.policy_id == policy.id,
                ScriptRun.device_id == device_id,
            )
            .order_by(ScriptRun.created_at.desc())
            .limit(1)
        )
    ).first()
    if last is None:
        return True
    state, created_at = last
    if state == "running" and created_at > now - IN_FLIGHT:
        return False
    return bool(created_at <= now - timedelta(hours=policy.interval_hours))


async def after_policy_run(session: AsyncSession, run: ScriptRun, now: datetime) -> None:
    """Retention and the failure alert for a finished scheduled run."""
    policy = (
        await session.execute(
            select(ScriptPolicy).where(
                ScriptPolicy.org_id == run.org_id, ScriptPolicy.id == run.policy_id
            )
        )
    ).scalar_one_or_none()
    if policy is None:
        return
    await _trim_output(session, policy, run.device_id)
    if policy.alert_on_failure:
        failed = run.state == "failed" or (run.exit_code not in (None, 0))
        await _route_alert(session, policy, run, failed, now)


async def _trim_output(session: AsyncSession, policy: ScriptPolicy, device_id: UUID) -> None:
    keep = (
        select(ScriptRun.id)
        .where(
            ScriptRun.org_id == policy.org_id,
            ScriptRun.policy_id == policy.id,
            ScriptRun.device_id == device_id,
        )
        .order_by(ScriptRun.created_at.desc())
        .limit(policy.keep_runs)
    )
    await session.execute(
        update(ScriptRun)
        .where(
            ScriptRun.org_id == policy.org_id,
            ScriptRun.policy_id == policy.id,
            ScriptRun.device_id == device_id,
            ScriptRun.output.is_not(None),
            ScriptRun.id.not_in(keep),
        )
        .values(output=None)
    )


async def _route_alert(
    session: AsyncSession, policy: ScriptPolicy, run: ScriptRun, failed: bool, now: datetime
) -> None:
    """Open or clear this schedule's alert on the device through its health
    profile. A profile with no `script.policy_failed` check routes nothing,
    and that is recorded, so the operator can see why no ticket opened."""
    from app.modules.health.alerts import clear_alert, open_alert_for_key, publish_alert_event
    from app.modules.health.declared_actions import approved_action_id_for, declared_action_kinds
    from app.modules.health.models import HealthAlert, HealthAlertAction
    from app.modules.health.resolution import resolve_profile_for_device
    from app.modules.health.settings import get_settings

    org_id = policy.org_id
    device = (
        await session.execute(
            select(Device).where(Device.org_id == org_id, Device.id == run.device_id)
        )
    ).scalar_one_or_none()
    if device is None:
        return
    resolved = await resolve_profile_for_device(session, org_id, device)
    checks = [
        c
        for c in (resolved.checks if resolved is not None else [])
        if c.check_type == POLICY_FAILED_CHECK and c.selector in ("", "*", policy.name)
    ]
    if resolved is None or not checks:
        if failed:
            await _audit(
                session,
                org_id,
                "script_policy.alert_unrouted",
                policy.id,
                {
                    "device_id": str(device.id),
                    "reason": "the device's health profile has no scheduled-script check",
                },
            )
        return
    check = next((c for c in checks if c.selector == policy.name), checks[0])
    selector = policy.name[:200]
    settings = await get_settings(session, org_id)
    existing = await open_alert_for_key(
        session, org_id, device_id=device.id, check_id=check.id, selector=selector
    )
    observed = f"exit {run.exit_code}" if run.exit_code is not None else run.state
    if not failed:
        if existing is not None:
            await clear_alert(
                session, org_id, existing, reason="recovery", now=now, check=check,
                settings=settings,
            )
        return
    if existing is not None:
        existing.last_seen_at = now
        existing.occurrence_count += 1
        existing.observed_value = observed[:64]
        existing.updated_at = now
        await session.flush()
        return
    alert = HealthAlert(
        org_id=org_id,
        device_id=device.id,
        check_id=check.id,
        profile_id=resolved.profile.id,
        selector=selector,
        check_type=check.check_type,
        severity=check.severity,
        state="active",
        observed_value=observed[:64],
        threshold_display="the scheduled script failed",
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
                approved_action_id=approved_action_id_for(check, kind),
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    await session.flush()
    await publish_alert_event(
        session, org_id, alert, event_type="health.alert_raised", check=check, settings=settings
    )
