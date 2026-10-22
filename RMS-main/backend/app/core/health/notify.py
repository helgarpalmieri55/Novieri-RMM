"""Platform probe failures, routed to a person through the notification outbox.

The worker samples every probe once a minute into `platform_health_sample`.
This reads those samples, decides per probe whether it is settled `healthy`
or `unhealthy`, and publishes `platform.probe_unhealthy` /
`platform.probe_recovered` into each org's outbox on a change. Delivery is the
notification module's: an org that wants these adds a `channel_default` rule
for the event type on an email or webhook channel.

DE-DUPLICATION, three layers:

- a verdict needs `CONSECUTIVE` samples in agreement, so a single slow probe
  run changes nothing;
- an event is published only on a state CHANGE, recorded per org in
  `platform_probe_notice`;
- a probe that goes unhealthy again within `RENOTIFY_COOLDOWN` of the last
  unhealthy notice changes state silently, and its recovery is silent too
  (`episode_notified`), so a flapping probe produces at most one pair of
  messages per cooldown.

The payload is the probe name, its status and its reason CODE -- the same
vocabulary `/readyz` exposes -- never exception text.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import publish
from app.core.health.types import Status
from app.models import PlatformHealthSample, PlatformProbeNotice

NOTIFY_INTERVAL_S = 60.0
CONSECUTIVE = 3
# Samples older than this say nothing about now: the sampler stopped.
STALE_AFTER = timedelta(minutes=5)
RENOTIFY_COOLDOWN = timedelta(hours=1)

EVENT_UNHEALTHY = "platform.probe_unhealthy"
EVENT_RECOVERED = "platform.probe_recovered"


@dataclass(frozen=True)
class ProbeVerdict:
    probe: str
    state: str  # healthy | unhealthy
    status: str  # ok | degraded | down (the worst in the window)
    reason: str | None
    since: datetime  # the oldest sample of the agreeing window


def probe_subject_id(probe: str) -> UUID:
    """A stable subject id for a probe; the outbox requires one."""
    return uuid5(NAMESPACE_URL, f"itms:platform-probe:{probe}")


async def probe_verdicts(session: AsyncSession, *, now: datetime) -> dict[str, ProbeVerdict]:
    """Settled verdicts only: a probe whose last `CONSECUTIVE` samples are all
    ok is healthy, all not-ok is unhealthy; mixed, too few or stale is absent."""
    rows = (
        await session.execute(
            select(
                PlatformHealthSample.probe,
                PlatformHealthSample.status,
                PlatformHealthSample.reason,
                PlatformHealthSample.sampled_at,
            )
            .where(PlatformHealthSample.sampled_at >= now - STALE_AFTER * 3)
            .order_by(PlatformHealthSample.probe, PlatformHealthSample.sampled_at.desc())
        )
    ).all()
    by_probe: dict[str, list[Any]] = {}
    for row in rows:
        window = by_probe.setdefault(row.probe, [])
        if len(window) < CONSECUTIVE:
            window.append(row)

    verdicts: dict[str, ProbeVerdict] = {}
    for probe, window in by_probe.items():
        if len(window) < CONSECUTIVE or now - window[0].sampled_at > STALE_AFTER:
            continue
        statuses = {r.status for r in window}
        if statuses == {Status.OK.value}:
            state, status, reason = "healthy", Status.OK.value, None
        elif Status.OK.value not in statuses:
            state = "unhealthy"
            status = Status.DOWN.value if Status.DOWN.value in statuses else Status.DEGRADED.value
            reason = window[0].reason
        else:
            continue
        verdicts[probe] = ProbeVerdict(probe, state, status, reason, window[-1].sampled_at)
    return verdicts


async def notify_org(
    session: AsyncSession,
    org_id: UUID,
    verdicts: dict[str, ProbeVerdict],
    *,
    now: datetime,
) -> int:
    """Apply the verdicts to this org's notice state; publish on a change.
    Returns the number of events published."""
    notices = {
        n.probe: n
        for n in (
            await session.execute(
                select(PlatformProbeNotice).where(PlatformProbeNotice.org_id == org_id)
            )
        ).scalars()
    }
    published = 0
    for probe in sorted(verdicts):
        verdict = verdicts[probe]
        notice = notices.get(probe)
        if notice is None:
            # A probe first seen healthy is recorded silently; first seen
            # unhealthy is a change from the assumed healthy baseline.
            notice = PlatformProbeNotice(
                org_id=org_id, probe=probe, state="healthy", changed_at=now,
                episode_notified=False,
            )
            session.add(notice)
        if notice.state == verdict.state:
            continue

        if verdict.state == "unhealthy":
            last = notice.last_unhealthy_notified_at
            announce = last is None or now - last >= RENOTIFY_COOLDOWN
            if announce:
                await _publish(session, org_id, EVENT_UNHEALTHY, verdict, {
                    "consecutive_samples": CONSECUTIVE,
                })
                notice.last_unhealthy_notified_at = now
                published += 1
            notice.episode_notified = announce
        else:
            if notice.episode_notified:
                await _publish(session, org_id, EVENT_RECOVERED, verdict, {
                    "unhealthy_since": notice.changed_at.isoformat(),
                })
                published += 1
            notice.episode_notified = False
        notice.state = verdict.state
        notice.changed_at = verdict.since
    await session.flush()
    return published


async def _publish(
    session: AsyncSession,
    org_id: UUID,
    event_type: str,
    verdict: ProbeVerdict,
    extra: dict[str, Any],
) -> None:
    await publish(
        session,
        org_id=org_id,
        event_type=event_type,
        subject_type="platform_probe",
        subject_id=probe_subject_id(verdict.probe),
        payload={
            "probe": verdict.probe,
            "status": verdict.status,
            "reason": verdict.reason,
            "since": verdict.since.isoformat(),
            **extra,
        },
        actor_user_id=None,
    )
