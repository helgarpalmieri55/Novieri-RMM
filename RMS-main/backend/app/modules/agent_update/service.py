"""Rollouts and rings (spec-self-update.md §6, §7, §8).

Every mutation here is a human act, audited, on one org's rows under RLS. No
function in this file is called by the beat: the reconciler (`reconcile.py`)
only continues a ring a person started.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import Agent, Device, DeviceGroup
from app.models.core import AgentRelease
from app.modules.agent_update.models import (
    ACTIVE_ROLLOUT_STATES,
    AgentUpdateRing,
    AgentUpdateRollout,
    AgentUpdateTarget,
)
from app.modules.agent_update.semver import Version, parse_version, try_parse_version

__all__ = [
    "CAPABILITY",
    "PILOT_MAX_DEVICES",
    "FleetDevice",
    "Preview",
    "RingPreview",
    "RingSpec",
    "RolloutConflict",
    "RolloutError",
    "RolloutNotFound",
    "advance_ring",
    "cancel_rollout",
    "close_ring",
    "create_rollout",
    "fleet_versions",
    "get_rollout",
    "list_rollouts",
    "list_targets",
    "load_fleet",
    "pause_rollout",
    "preview",
    "promote_version",
    "release_in_active_rollout",
    "resume_rollout",
    "set_release_signature",
    "start_rollout",
]

CAPABILITY = "agent.update"
PILOT_MAX_DEVICES = 25
MAX_RINGS = 10
_OS = {"linux": "linux", "windows": "windows", "macos": "darwin", "darwin": "darwin"}
_ARCH = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


class RolloutError(Exception):
    """A request the rules refuse (422 unless a subclass says otherwise)."""

    status_code = 422


class RolloutNotFound(RolloutError):
    status_code = 404


class RolloutConflict(RolloutError):
    status_code = 409


@dataclass(frozen=True)
class RingSpec:
    name: str
    device_group_id: UUID | None
    is_pilot: bool = False


@dataclass(frozen=True)
class FleetDevice:
    device_id: UUID
    hostname: str | None
    os: str | None
    arch: str | None
    version: str
    capable: bool
    online: bool
    device_group_id: UUID | None


@dataclass
class RingPreview:
    ordinal: int
    name: str
    is_pilot: bool
    device_group_id: UUID | None
    total: int = 0
    eligible: int = 0
    needs_manual_update: int = 0
    no_artifact: int = 0
    already_current: int = 0
    other_skipped: int = 0


@dataclass
class Preview:
    target_version: str
    is_rollback: bool
    channel: str | None
    rings: list[RingPreview] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(UTC)


def platform_of(os_family: str | None, architecture: str | None) -> tuple[str | None, str | None]:
    return _OS.get((os_family or "").lower()), _ARCH.get((architecture or "").lower())


def update_capable(capabilities: Any) -> bool:
    """Does the agent say it runs `agent.update`? Enrollment and the gateway
    both store `{job_type: true}`."""
    if not isinstance(capabilities, dict):
        return False
    return capabilities.get(CAPABILITY) is True


async def load_fleet(session: AsyncSession) -> list[FleetDevice]:
    """Every live device with an agent in the session's org."""
    rows = (
        await session.execute(
            select(Device, Agent)
            .join(Agent, Agent.device_id == Device.id)
            .where(
                Device.status != "decommissioned",
                Agent.cert_revoked.is_(False),
                # `agent.update` is refused by the review gate while pending.
                Device.review_state == "approved",
            )
            .order_by(Device.id)
        )
    ).all()
    out = []
    for device, agent in rows:
        os_name, arch = platform_of(device.os_family, device.architecture)
        out.append(
            FleetDevice(
                device_id=device.id,
                hostname=device.hostname,
                os=os_name,
                arch=arch,
                version=agent.agent_version or "",
                capable=update_capable(agent.capabilities),
                online=agent.status == "online",
                device_group_id=device.device_group_id,
            )
        )
    return out


async def releases_for(session: AsyncSession, version: str) -> dict[tuple[str, str], AgentRelease]:
    rows = (
        await session.execute(
            select(AgentRelease).where(AgentRelease.version == version, AgentRelease.sha256 != "")
        )
    ).scalars()
    return {(r.os, r.arch): r for r in rows}


def classify(
    dev: FleetDevice,
    target: Version,
    is_rollback: bool,
    releases: dict[tuple[str, str], AgentRelease],
) -> tuple[str, str, AgentRelease | None]:
    """(target state, reason, release) for one device (spec §6.3)."""
    current = try_parse_version(dev.version)
    if current is None:
        return "needs_manual_update", "version_unknown", None
    if not dev.capable:
        return "needs_manual_update", "no_agent_update", None
    release = releases.get((dev.os or "", dev.arch or ""))
    if release is None:
        return "skipped", "no_artifact", None
    if current == target:
        return "skipped", "already_current", release
    if not is_rollback and current > target:
        return "skipped", "above_target", release
    if is_rollback and current < target:
        return "skipped", "below_target", release
    return "pending", "", release


def _validate_rings(rings: Sequence[RingSpec], is_rollback: bool) -> None:
    if not 1 <= len(rings) <= MAX_RINGS:
        raise RolloutError(tr("a rollout has 1 to {max_rings} rings", max_rings=MAX_RINGS))
    groups = [r.device_group_id for r in rings if r.device_group_id is not None]
    if len(groups) != len(set(groups)):
        raise RolloutError(tr("a device group may appear in one ring only"))
    for i, ring in enumerate(rings):
        if not ring.name.strip():
            raise RolloutError(tr("every ring needs a name"))
        if ring.is_pilot and i != 0:
            raise RolloutError(tr("only ring 0 may be the pilot ring"))
        if ring.device_group_id is None and i != len(rings) - 1:
            raise RolloutError(tr("only the last ring may take every remaining device"))
    if not is_rollback and not rings[0].is_pilot:
        # A rollback restores a version the fleet has already run (§7).
        raise RolloutError(tr("ring 0 must be the pilot ring"))


def assign_rings(
    fleet: Sequence[FleetDevice], rings: Sequence[RingSpec]
) -> list[list[FleetDevice]]:
    remaining = list(fleet)
    out: list[list[FleetDevice]] = []
    for ring in rings:
        members = [
            d
            for d in remaining
            if ring.device_group_id is None or d.device_group_id == ring.device_group_id
        ]
        taken = {d.device_id for d in members}
        remaining = [d for d in remaining if d.device_id not in taken]
        out.append(members)
    return out


def _channel(releases: dict[tuple[str, str], AgentRelease]) -> str | None:
    channels = {r.channel for r in releases.values()}
    if not channels:
        return None
    return channels.pop() if len(channels) == 1 else "mixed"


def _parse_target(version: str) -> Version:
    try:
        return parse_version(version)
    except ValueError:
        raise RolloutError(
            tr(
                "version {version} is not strict semver; agents cannot order it",
                version=repr(version),
            )
        ) from None


async def _check_groups(session: AsyncSession, rings: Sequence[RingSpec]) -> None:
    ids = {r.device_group_id for r in rings if r.device_group_id is not None}
    if not ids:
        return
    found = set(
        (await session.execute(select(DeviceGroup.id).where(DeviceGroup.id.in_(ids)))).scalars()
    )
    if found != ids:
        raise RolloutError(tr("a ring names a device group that does not exist in this org"))


async def preview(
    session: AsyncSession,
    *,
    target_version: str,
    is_rollback: bool,
    rings: Sequence[RingSpec],
) -> Preview:
    """What a rollout would do, per ring. Writes nothing."""
    _validate_rings(rings, is_rollback)
    await _check_groups(session, rings)
    target = _parse_target(target_version)
    releases = await releases_for(session, target_version)
    fleet = await load_fleet(session)
    result = Preview(
        target_version=target_version, is_rollback=is_rollback, channel=_channel(releases)
    )
    for i, (spec, members) in enumerate(zip(rings, assign_rings(fleet, rings), strict=True)):
        rp = RingPreview(i, spec.name, spec.is_pilot, spec.device_group_id, total=len(members))
        for dev in members:
            state, reason, _ = classify(dev, target, is_rollback, releases)
            if state == "pending":
                rp.eligible += 1
            elif state == "needs_manual_update":
                rp.needs_manual_update += 1
            elif reason == "no_artifact":
                rp.no_artifact += 1
            elif reason == "already_current":
                rp.already_current += 1
            else:
                rp.other_skipped += 1
        result.rings.append(rp)
    pilot = result.rings[0]
    if pilot.is_pilot and pilot.total > PILOT_MAX_DEVICES:
        raise RolloutError(
            tr(
                "the pilot ring holds {pilot_total} devices; it may hold at most "
                "{pilot_max_devices}",
                pilot_total=pilot.total,
                pilot_max_devices=PILOT_MAX_DEVICES,
            )
        )
    return result


async def _audit(
    session: AsyncSession,
    rollout: AgentUpdateRollout,
    action: str,
    *,
    actor_id: UUID | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await audit_service.record(
        session,
        org_id=rollout.org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action=f"agent.update.{action}",
        target_type="agent_update_rollout",
        target_id=str(rollout.id),
        metadata={
            "version": rollout.target_version,
            "is_rollback": rollout.is_rollback,
            **(metadata or {}),
        },
    )


async def create_rollout(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    target_version: str,
    is_rollback: bool,
    reason: str,
    rings: Sequence[RingSpec],
    batch_size: int = 10,
    failure_threshold_pct: int = 5,
    health_window_s: int = 600,
) -> AgentUpdateRollout:
    """A draft. Nothing is dispatched until a person starts it."""
    if is_rollback and not reason.strip():
        raise RolloutError(tr("a rollback rollout must name the release it supersedes in `reason`"))
    pv = await preview(session, target_version=target_version, is_rollback=is_rollback, rings=rings)
    if pv.channel is None:
        raise RolloutError(
            tr(
                "no published artefact exists for version {target_version}",
                target_version=repr(target_version),
            )
        )
    rollout = AgentUpdateRollout(
        org_id=org_id,
        target_version=target_version,
        is_rollback=is_rollback,
        state="draft",
        batch_size=batch_size,
        failure_threshold_pct=failure_threshold_pct,
        health_window_s=health_window_s,
        reason=reason.strip(),
        created_by=actor_id,
    )
    session.add(rollout)
    await session.flush()
    for i, spec in enumerate(rings):
        session.add(
            AgentUpdateRing(
                org_id=org_id,
                rollout_id=rollout.id,
                ordinal=i,
                name=spec.name.strip(),
                is_pilot=spec.is_pilot,
                device_group_id=spec.device_group_id,
            )
        )
    await session.flush()
    await _audit(
        session,
        rollout,
        "rollout_created",
        actor_id=actor_id,
        metadata={
            "ring_sizes": [r.total for r in pv.rings],
            "batch_size": batch_size,
            "failure_threshold_pct": failure_threshold_pct,
            "health_window_s": health_window_s,
            "reason": rollout.reason,
        },
    )
    return rollout


async def get_rollout(session: AsyncSession, rollout_id: UUID) -> AgentUpdateRollout:
    rollout = (
        await session.execute(select(AgentUpdateRollout).where(AgentUpdateRollout.id == rollout_id))
    ).scalar_one_or_none()
    if rollout is None:
        raise RolloutNotFound(tr("no such rollout"))
    return rollout


async def rings_of(session: AsyncSession, rollout_id: UUID) -> list[AgentUpdateRing]:
    return list(
        (
            await session.execute(
                select(AgentUpdateRing)
                .where(AgentUpdateRing.rollout_id == rollout_id)
                .order_by(AgentUpdateRing.ordinal)
            )
        ).scalars()
    )


async def _require_stable(session: AsyncSession, version: str, ring: AgentUpdateRing) -> None:
    if ring.is_pilot:
        return
    channel = _channel(await releases_for(session, version))
    if channel != "stable":
        raise RolloutConflict(
            tr(
                "ring {ring_ordinal} ({ring_name}) is not a pilot ring and version "
                "{version} is not promoted to stable (channel: {channel})",
                ring_ordinal=ring.ordinal,
                ring_name=ring.name,
                version=version,
                channel=channel or "none",
            )
        )


async def start_rollout(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, rollout_id: UUID
) -> AgentUpdateRollout:
    """Materialize targets from the preview and activate ring 0 (§6.2)."""
    rollout = await get_rollout(session, rollout_id)
    if rollout.state != "draft":
        raise RolloutConflict(
            tr("rollout is {rollout_state}, not draft", rollout_state=rollout.state)
        )
    active = (
        await session.execute(
            select(func.count())
            .select_from(AgentUpdateRollout)
            .where(AgentUpdateRollout.state.in_(ACTIVE_ROLLOUT_STATES))
        )
    ).scalar_one()
    if active:
        raise RolloutConflict(tr("this org already has an active rollout"))
    rings = await rings_of(session, rollout.id)
    specs = [RingSpec(r.name, r.device_group_id, r.is_pilot) for r in rings]
    pv = await preview(
        session, target_version=rollout.target_version, is_rollback=rollout.is_rollback, rings=specs
    )
    await _require_stable(session, rollout.target_version, rings[0])

    target = parse_version(rollout.target_version)
    releases = await releases_for(session, rollout.target_version)
    fleet = await load_fleet(session)
    for ring, members in zip(rings, assign_rings(fleet, specs), strict=True):
        for dev in members:
            state, reason, release = classify(dev, target, rollout.is_rollback, releases)
            session.add(
                AgentUpdateTarget(
                    org_id=org_id,
                    rollout_id=rollout.id,
                    ring_id=ring.id,
                    device_id=dev.device_id,
                    release_id=release.id if release is not None else None,
                    from_version=dev.version[:64],
                    state=state,
                    reason=reason,
                )
            )
    now = _now()
    rollout.state, rollout.started_by, rollout.started_at = "running", actor_id, now
    rings[0].state, rings[0].activated_by, rings[0].activated_at = "active", actor_id, now
    try:
        await session.flush()
    except IntegrityError as exc:
        raise RolloutConflict(tr("this org already has an active rollout")) from exc
    await _audit(
        session,
        rollout,
        "rollout_started",
        actor_id=actor_id,
        metadata={
            "ring_sizes": [r.total for r in pv.rings],
            "eligible": [r.eligible for r in pv.rings],
            "batch_size": rollout.batch_size,
            "failure_threshold_pct": rollout.failure_threshold_pct,
        },
    )
    return rollout


async def pause_rollout(
    session: AsyncSession,
    *,
    rollout_id: UUID,
    actor_id: UUID | None,
    reason: str = "manual",
) -> AgentUpdateRollout:
    rollout = await get_rollout(session, rollout_id)
    if rollout.state != "running":
        raise RolloutConflict(
            tr("rollout is {rollout_state}, not running", rollout_state=rollout.state)
        )
    rollout.state, rollout.pause_reason = "paused", reason
    await session.flush()
    await _audit(
        session, rollout, "rollout_paused", actor_id=actor_id, metadata={"pause_reason": reason}
    )
    return rollout


async def resume_rollout(
    session: AsyncSession, *, rollout_id: UUID, actor_id: UUID
) -> AgentUpdateRollout:
    rollout = await get_rollout(session, rollout_id)
    if rollout.state != "paused":
        raise RolloutConflict(
            tr("rollout is {rollout_state}, not paused", rollout_state=rollout.state)
        )
    previous = rollout.pause_reason
    rollout.state, rollout.pause_reason = "running", None
    await session.flush()
    await _audit(
        session,
        rollout,
        "rollout_resumed",
        actor_id=actor_id,
        metadata={"was_paused_for": previous},
    )
    return rollout


async def advance_ring(
    session: AsyncSession, *, rollout_id: UUID, actor_id: UUID
) -> AgentUpdateRing:
    """Activate the next ring. Always a person's act (operator decision)."""
    rollout = await get_rollout(session, rollout_id)
    if rollout.state != "running":
        raise RolloutConflict(
            tr(
                "rollout is {rollout_state}; only a running rollout advances",
                rollout_state=rollout.state,
            )
        )
    rings = await rings_of(session, rollout.id)
    if any(r.state == "active" for r in rings):
        raise RolloutConflict(tr("the active ring has not finished; close it or wait"))
    nxt = next((r for r in rings if r.state == "pending"), None)
    if nxt is None:
        raise RolloutConflict(tr("there is no further ring"))
    await _require_stable(session, rollout.target_version, nxt)
    nxt.state, nxt.activated_by, nxt.activated_at = "active", actor_id, _now()
    await session.flush()
    await _audit(
        session,
        rollout,
        "rollout_ring_advanced",
        actor_id=actor_id,
        metadata={"ring": nxt.ordinal, "ring_name": nxt.name},
    )
    return nxt


async def close_ring(
    session: AsyncSession, *, rollout_id: UUID, ring_id: UUID, actor_id: UUID
) -> AgentUpdateRing:
    """Skip the active ring's pending targets (typically offline devices)."""
    rollout = await get_rollout(session, rollout_id)
    ring = (
        await session.execute(
            select(AgentUpdateRing).where(
                AgentUpdateRing.id == ring_id, AgentUpdateRing.rollout_id == rollout.id
            )
        )
    ).scalar_one_or_none()
    if ring is None:
        raise RolloutNotFound(tr("no such ring in this rollout"))
    if ring.state != "active":
        raise RolloutConflict(
            tr("ring is {ring_state}; only the active ring can be closed", ring_state=ring.state)
        )
    result = await session.execute(
        update(AgentUpdateTarget)
        .where(AgentUpdateTarget.ring_id == ring.id, AgentUpdateTarget.state == "pending")
        .values(state="skipped", reason="ring_closed", updated_at=_now())
    )
    skipped = int(getattr(result, "rowcount", 0) or 0)
    await session.flush()
    await _audit(
        session,
        rollout,
        "rollout_ring_closed",
        actor_id=actor_id,
        metadata={"ring": ring.ordinal, "skipped": skipped},
    )
    return ring


async def cancel_rollout(
    session: AsyncSession, *, rollout_id: UUID, actor_id: UUID
) -> AgentUpdateRollout:
    """Stop new dispatches. Jobs in flight finish or roll back on their own."""
    rollout = await get_rollout(session, rollout_id)
    if rollout.state not in ("draft", "running", "paused"):
        raise RolloutConflict(tr("rollout is {rollout_state}", rollout_state=rollout.state))
    await session.execute(
        update(AgentUpdateTarget)
        .where(AgentUpdateTarget.rollout_id == rollout.id, AgentUpdateTarget.state == "pending")
        .values(state="skipped", reason="cancelled", updated_at=_now())
    )
    rollout.state, rollout.completed_at = "cancelled", _now()
    await session.flush()
    await _audit(session, rollout, "rollout_cancelled", actor_id=actor_id)
    return rollout


async def list_rollouts(session: AsyncSession) -> list[AgentUpdateRollout]:
    return list(
        (
            await session.execute(
                select(AgentUpdateRollout).order_by(AgentUpdateRollout.created_at.desc())
            )
        ).scalars()
    )


async def target_counts(session: AsyncSession, rollout_id: UUID) -> dict[UUID, dict[str, int]]:
    rows = (
        await session.execute(
            select(AgentUpdateTarget.ring_id, AgentUpdateTarget.state, func.count())
            .where(AgentUpdateTarget.rollout_id == rollout_id)
            .group_by(AgentUpdateTarget.ring_id, AgentUpdateTarget.state)
        )
    ).all()
    out: dict[UUID, dict[str, int]] = {}
    for ring_id, state, n in rows:
        out.setdefault(ring_id, {})[state] = n
    return out


async def list_targets(
    session: AsyncSession, rollout_id: UUID
) -> list[tuple[AgentUpdateTarget, str | None]]:
    await get_rollout(session, rollout_id)
    rows = (
        await session.execute(
            select(AgentUpdateTarget, Device.hostname)
            .join(Device, Device.id == AgentUpdateTarget.device_id)
            .where(AgentUpdateTarget.rollout_id == rollout_id)
            .order_by(AgentUpdateTarget.ring_id, Device.hostname)
        )
    ).all()
    return [(t, hostname) for t, hostname in rows]


@dataclass
class FleetVersions:
    total: int
    versions: dict[str, int]
    needs_manual_update: list[FleetDevice]
    latest_stable: str | None
    update_available: int


async def fleet_versions(session: AsyncSession) -> FleetVersions:
    fleet = await load_fleet(session)
    stable = (
        await session.execute(
            select(AgentRelease.version).where(
                AgentRelease.channel == "stable", AgentRelease.sha256 != ""
            )
        )
    ).scalars()
    parsed = [v for v in (try_parse_version(s) for s in set(stable)) if v is not None]
    latest = max(parsed) if parsed else None
    versions: dict[str, int] = {}
    manual: list[FleetDevice] = []
    behind = 0
    for dev in fleet:
        versions[dev.version or "unknown"] = versions.get(dev.version or "unknown", 0) + 1
        current = try_parse_version(dev.version)
        if current is None or not dev.capable:
            manual.append(dev)
        elif latest is not None and current < latest:
            behind += 1
    return FleetVersions(
        total=len(fleet),
        versions=versions,
        needs_manual_update=manual,
        latest_stable=str(latest) if latest is not None else None,
        update_available=behind,
    )


async def promote_version(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, version: str, channel: str
) -> int:
    """Set the channel of every (os, arch) row of a version. Platform-scoped:
    `agent_releases` is global, and the route requires `agent.release.manage`."""
    rows = list(
        (
            await session.execute(select(AgentRelease).where(AgentRelease.version == version))
        ).scalars()
    )
    if not rows:
        raise RolloutNotFound(tr("no release rows for version {version}", version=repr(version)))
    for row in rows:
        row.channel = channel
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="agent.release_promoted",
        target_type="agent_release_version",
        target_id=version,
        metadata={"version": version, "channel": channel, "rows": len(rows)},
    )
    return len(rows)


async def set_release_signature(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, release_id: UUID, signature: str
) -> AgentRelease:
    release = (
        await session.execute(select(AgentRelease).where(AgentRelease.id == release_id))
    ).scalar_one_or_none()
    if release is None:
        raise RolloutNotFound(tr("no such agent release"))
    release.release_signature = signature
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="agent.release_signature_set",
        target_type="agent_release",
        target_id=str(release.id),
        metadata={"version": release.version, "os": release.os, "arch": release.arch},
    )
    return release


async def release_in_active_rollout(admin_session: AsyncSession, release_id: UUID) -> bool:
    """Across every org (an admin session): does an active rollout still use
    this release? Unpublishing it would strand those targets (§7)."""
    hit = (
        await admin_session.execute(
            select(AgentUpdateTarget.id)
            .join(AgentUpdateRollout, AgentUpdateRollout.id == AgentUpdateTarget.rollout_id)
            .where(
                AgentUpdateTarget.release_id == release_id,
                AgentUpdateRollout.state.in_(ACTIVE_ROLLOUT_STATES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return hit is not None
