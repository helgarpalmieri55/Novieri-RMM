"""Platform probe failures routed through `notification` (review 2026-09-23)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import override_settings
from app.core.health import notify
from app.core.health.notify import ProbeVerdict, notify_org, probe_verdicts
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import (
    DomainEvent,
    NotificationDelivery,
    Organization,
    PlatformHealthSample,
    PlatformProbeNotice,
    User,
)
from app.modules.notification.channels import create_channel
from app.modules.notification.rules import create_rule
from app.modules.notification.schema import ChannelCreate, RuleCreate

# Far from the wall clock, so samples other tests write in this shared
# database can never fall inside these windows.
T0 = datetime(2031, 1, 1, 12, 0, tzinfo=UTC)


async def _org(admin_engine: AsyncEngine) -> UUID:
    slug = f"pn-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id


def _verdict(probe: str, state: str, at: datetime) -> dict[str, ProbeVerdict]:
    status = "ok" if state == "healthy" else "down"
    reason = None if state == "healthy" else "unreachable"
    return {probe: ProbeVerdict(probe, state, status, reason, at)}


async def _apply(app_engine: AsyncEngine, org_id: UUID, verdicts: Any, now: datetime) -> int:
    async with org_session(org_id, engine=app_engine) as session:
        return await notify_org(session, org_id, verdicts, now=now)


async def _events(app_engine: AsyncEngine, org_id: UUID) -> list[DomainEvent]:
    async with org_session(org_id, engine=app_engine) as session:
        return list(
            (
                await session.execute(select(DomainEvent).order_by(DomainEvent.created_at))
            ).scalars()
        )


async def _samples(admin_engine: AsyncEngine, probe: str, statuses: list[str]) -> None:
    """Oldest first, one a minute, the newest at T0."""
    async with AsyncSession(admin_engine) as session:
        for i, status in enumerate(statuses):
            session.add(
                PlatformHealthSample(
                    probe=probe,
                    status=status,
                    duration_ms=5,
                    reason=None if status == "ok" else "timeout",
                    sampled_at=T0 - timedelta(minutes=len(statuses) - 1 - i),
                )
            )
        await session.commit()


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


async def test_a_verdict_needs_consecutive_agreeing_fresh_samples(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    tag = uuid4().hex[:6]
    names = [f"{n}-{tag}" for n in ("down", "ok", "mix", "short", "deg")]
    down, ok, mixed, short, degraded = names
    try:
        await _samples(admin_engine, down, ["ok", "down", "down", "down"])
        await _samples(admin_engine, ok, ["down", "ok", "ok", "ok"])
        await _samples(admin_engine, mixed, ["down", "down", "ok", "down"])
        await _samples(admin_engine, short, ["down", "down"])
        await _samples(admin_engine, degraded, ["degraded", "degraded", "degraded"])

        async with AsyncSession(app_engine) as session:
            verdicts = await probe_verdicts(session, now=T0 + timedelta(seconds=30))
            stale = await probe_verdicts(session, now=T0 + timedelta(minutes=10))
    finally:
        # The samples table is platform-wide and shared by the whole session;
        # other files assert on exactly what it holds.
        async with AsyncSession(admin_engine) as session:
            await session.execute(
                delete(PlatformHealthSample).where(PlatformHealthSample.probe.in_(names))
            )
            await session.commit()

    assert verdicts[down].state == "unhealthy"
    assert (verdicts[down].status, verdicts[down].reason) == ("down", "timeout")
    assert verdicts[down].since == T0 - timedelta(minutes=2)
    assert verdicts[ok].state == "healthy"
    assert verdicts[degraded].status == "degraded"
    assert mixed not in verdicts
    assert short not in verdicts
    assert down not in stale, "a sampler that stopped says nothing about now"


# ---------------------------------------------------------------------------
# de-duplication
# ---------------------------------------------------------------------------


async def test_a_change_is_published_once_and_recovery_once(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id = await _org(admin_engine)
    p = "database"
    assert await _apply(app_engine, org_id, _verdict(p, "healthy", T0), T0) == 0
    t1 = T0 + timedelta(minutes=5)
    assert await _apply(app_engine, org_id, _verdict(p, "unhealthy", t1), t1) == 1
    t2 = t1 + timedelta(minutes=1)
    assert await _apply(app_engine, org_id, _verdict(p, "unhealthy", t1), t2) == 0
    t3 = t2 + timedelta(minutes=10)
    assert await _apply(app_engine, org_id, _verdict(p, "healthy", t3), t3) == 1

    events = await _events(app_engine, org_id)
    assert [e.event_type for e in events] == [
        "platform.probe_unhealthy",
        "platform.probe_recovered",
    ]
    raised, recovered = events
    assert raised.payload == {
        "probe": p,
        "status": "down",
        "reason": "unreachable",
        "since": t1.isoformat(),
        "consecutive_samples": notify.CONSECUTIVE,
    }
    assert raised.subject_type == "platform_probe"
    assert raised.subject_id == notify.probe_subject_id(p) == recovered.subject_id
    assert raised.actor_user_id is None
    assert recovered.payload["unhealthy_since"] == t1.isoformat()


async def test_a_flapping_probe_is_silent_within_the_cooldown(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id = await _org(admin_engine)
    p = "redis"
    t = T0
    published = []
    # down, up, down, up, down... every few minutes for under an hour
    for i in range(10):
        state = "unhealthy" if i % 2 == 0 else "healthy"
        published.append(await _apply(app_engine, org_id, _verdict(p, state, t), t))
        t += timedelta(minutes=4)
    assert published == [1, 1] + [0] * 8

    # past the cooldown, a new episode is announced again
    t = T0 + notify.RENOTIFY_COOLDOWN + timedelta(minutes=1)
    assert await _apply(app_engine, org_id, _verdict(p, "unhealthy", t), t) == 1
    t += timedelta(minutes=5)
    assert await _apply(app_engine, org_id, _verdict(p, "healthy", t), t) == 1


async def test_a_probe_first_seen_unhealthy_is_announced(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id = await _org(admin_engine)
    assert await _apply(app_engine, org_id, _verdict("vault", "unhealthy", T0), T0) == 1


# ---------------------------------------------------------------------------
# tenancy
# ---------------------------------------------------------------------------


async def test_notice_state_is_read_through_the_applications_own_predicate(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """On BYPASSRLS: org B already announced this probe; org A must still be
    told, and nothing may land in org B."""
    org_a = await _org(admin_engine)
    org_b = await _org(admin_engine)
    bypassrls_session.add(
        PlatformProbeNotice(
            org_id=org_b, probe="gateway", state="unhealthy", changed_at=T0,
            last_unhealthy_notified_at=T0, episode_notified=True,
        )
    )
    await bypassrls_session.commit()

    verdict = _verdict("gateway", "unhealthy", T0)
    assert await notify_org(bypassrls_session, org_a, verdict, now=T0) == 1
    await bypassrls_session.commit()

    orgs = (
        await bypassrls_session.execute(
            select(DomainEvent.org_id).where(DomainEvent.org_id.in_([org_a, org_b]))
        )
    ).scalars().all()
    assert orgs == [org_a]
    b_notice = (
        await bypassrls_session.execute(
            select(PlatformProbeNotice).where(PlatformProbeNotice.org_id == org_b)
        )
    ).scalar_one()
    assert b_notice.last_unhealthy_notified_at == T0


# ---------------------------------------------------------------------------
# end to end: a rule on a webhook channel gets a delivery
# ---------------------------------------------------------------------------


@pytest.fixture()
def _settings(api_env: Any):
    with override_settings(api_env.settings):
        yield


async def test_an_org_rule_routes_the_event_to_its_channel(
    api_env: Any, tmp_path: Path, _settings: None
) -> None:
    from app.worker import drain_outbox

    org_id = await api_env.create_org("pn")
    sub = await api_env.create_user(org_id, "org-admin")
    vault = LocalVaultService(tmp_path / "root.key")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        channel = await create_channel(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=ChannelCreate(
                kind="webhook", name="pager", config={"url": "https://hooks.example.com/p"}
            ),
            secret=None,
            vault=vault,
        )
        await create_rule(
            session,
            org_id=org_id,
            actor_id=user_id,
            data=RuleCreate(
                event_type="platform.probe_unhealthy",
                channel_id=channel.id,
                recipient_kind="channel_default",
            ),
        )
        await session.commit()

    await _apply(api_env.app_engine, org_id, _verdict("vault", "unhealthy", T0), T0)
    assert await drain_outbox(org_id, api_env.app_engine) == 1

    async with AsyncSession(api_env.admin_engine) as session:
        deliveries = (
            await session.execute(
                select(NotificationDelivery).where(NotificationDelivery.org_id == org_id)
            )
        ).scalars().all()
    assert [(d.channel_id, d.recipient) for d in deliveries] == [
        (channel.id, "https://hooks.example.com/p")
    ]


def test_the_notify_task_is_scheduled(pg_cluster: Any, tmp_path: Path) -> None:
    from app.core.config import Settings
    from app.worker import create_celery

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
    )
    with override_settings(settings):
        entry = create_celery().conf.beat_schedule["platform-health-notify"]
    assert entry["task"] == "app.worker.platform_health_notify"
    assert entry["schedule"] == notify.NOTIFY_INTERVAL_S
