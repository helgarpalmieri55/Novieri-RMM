"""Subscription notice-deadline and renewal alerts through `notification`
(platform review 2026-09-23)."""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import DomainEvent, Organization, Subscription, SubscriptionAlertSent, User
from app.modules.subscriptions import alerts
from app.modules.subscriptions.alerts import emit_due_alerts

TODAY = date(2026, 9, 23)


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"subalert-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _sub(
    admin_engine: AsyncEngine,
    org_id: UUID,
    user_id: UUID,
    *,
    name: str,
    renews_at: date | None,
    notice_days: int = 0,
    auto_renew: bool = False,
    status: str = "active",
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        sub = Subscription(
            org_id=org_id,
            service_name=name,
            billing_cycle="annual",
            cost_per_cycle=Decimal("100.00"),
            currency="USD",
            renews_at=renews_at,
            notice_days=notice_days,
            auto_renew=auto_renew,
            status=status,
            created_by=user_id,
        )
        s.add(sub)
        await s.commit()
        return sub.id


async def _emit(app_engine: AsyncEngine, org_id: UUID, today: date, window: int = 30) -> int:
    async with org_session(org_id, engine=app_engine) as session:
        return await emit_due_alerts(session, org_id, today=today, window_days=window)


async def _events(app_engine: AsyncEngine, org_id: UUID) -> list[DomainEvent]:
    async with org_session(org_id, engine=app_engine) as session:
        return list(
            (
                await session.execute(
                    select(DomainEvent).order_by(DomainEvent.created_at, DomainEvent.id)
                )
            ).scalars()
        )


async def test_due_notice_and_renewal_are_announced(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_with_user(admin_engine)
    # notice deadline in window (renewal is not): notice only
    long_notice = await _sub(
        admin_engine, org_id, user_id, name="Atlassian", renews_at=TODAY + timedelta(days=60),
        notice_days=45, auto_renew=True,
    )
    # both in window: two messages
    both = await _sub(
        admin_engine, org_id, user_id, name="Slack", renews_at=TODAY + timedelta(days=20),
        notice_days=10,
    )
    # no notice period: the renewal only, never a second "notice" for the same day
    plain = await _sub(
        admin_engine, org_id, user_id, name="Zoom", renews_at=TODAY + timedelta(days=5),
    )
    # outside the window / already passed / cancelled / undated: nothing
    await _sub(admin_engine, org_id, user_id, name="Far", renews_at=TODAY + timedelta(days=90))
    await _sub(admin_engine, org_id, user_id, name="Past", renews_at=TODAY - timedelta(days=1))
    await _sub(
        admin_engine, org_id, user_id, name="Gone", renews_at=TODAY + timedelta(days=3),
        status="cancelled",
    )
    await _sub(admin_engine, org_id, user_id, name="Undated", renews_at=None)

    assert await _emit(app_engine, org_id, TODAY) == 4
    got = {(e.subject_id, e.event_type) for e in await _events(app_engine, org_id)}
    assert got == {
        (long_notice, "subscription.notice_deadline_approaching"),
        (both, "subscription.notice_deadline_approaching"),
        (both, "subscription.renewal_approaching"),
        (plain, "subscription.renewal_approaching"),
    }

    notice = next(
        e for e in await _events(app_engine, org_id)
        if e.subject_id == long_notice
    )
    assert notice.subject_type == "subscription"
    assert notice.actor_user_id is None
    assert notice.payload["service_name"] == "Atlassian"
    assert notice.payload["notice_deadline"] == (TODAY + timedelta(days=15)).isoformat()
    assert notice.payload["days_to_notice"] == 15
    assert notice.payload["auto_renew"] is True
    assert notice.payload["action_required"] is False


async def test_each_deadline_is_announced_once(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_with_user(admin_engine)
    sub_id = await _sub(
        admin_engine, org_id, user_id, name="GitHub", renews_at=TODAY + timedelta(days=10)
    )
    assert await _emit(app_engine, org_id, TODAY) == 1
    assert await _emit(app_engine, org_id, TODAY) == 0
    assert await _emit(app_engine, org_id, TODAY + timedelta(days=1)) == 0

    # A recorded renewal moves the deadline: the next one is announced.
    async with AsyncSession(admin_engine) as s:
        sub = await s.get(Subscription, sub_id)
        assert sub is not None
        sub.renews_at = TODAY + timedelta(days=375)
        await s.commit()
    next_year = TODAY + timedelta(days=350)
    assert await _emit(app_engine, org_id, next_year) == 1
    assert len(await _events(app_engine, org_id)) == 2


async def test_the_window_is_configurable(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_with_user(admin_engine)
    await _sub(admin_engine, org_id, user_id, name="M365", renews_at=TODAY + timedelta(days=45))
    assert await _emit(app_engine, org_id, TODAY, window=30) == 0
    assert await _emit(app_engine, org_id, TODAY, window=60) == 1


async def test_subscriptions_are_scoped_by_the_applications_own_predicate(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """On BYPASSRLS, org A's sweep must neither read nor announce org B's
    subscription, and every row it writes is org A's."""
    org_a, user_a = await _org_with_user(admin_engine)
    org_b, user_b = await _org_with_user(admin_engine)
    await _sub(admin_engine, org_a, user_a, name="A", renews_at=TODAY + timedelta(days=5))
    await _sub(admin_engine, org_b, user_b, name="B", renews_at=TODAY + timedelta(days=5))

    assert await emit_due_alerts(bypassrls_session, org_a, today=TODAY, window_days=30) == 1
    await bypassrls_session.commit()

    events = (
        await bypassrls_session.execute(
            select(DomainEvent.org_id, DomainEvent.payload).where(
                DomainEvent.org_id.in_([org_a, org_b])
            )
        )
    ).all()
    assert [(e.org_id, e.payload["service_name"]) for e in events] == [(org_a, "A")]
    sent = (
        await bypassrls_session.execute(
            select(SubscriptionAlertSent.org_id).where(
                SubscriptionAlertSent.org_id.in_([org_a, org_b])
            )
        )
    ).scalars().all()
    assert sent == [org_a]


async def test_the_sweep_shell_uses_the_configured_window(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    pg_cluster: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import worker
    from app.core.config import Settings, override_settings

    seen: list[tuple[UUID, date, int]] = []

    async def fake_emit(_session: Any, org_id: UUID, *, today: date, window_days: int) -> int:
        seen.append((org_id, today, window_days))
        return 1

    monkeypatch.setattr(worker.subscription_alerts_module, "emit_due_alerts", fake_emit)
    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        subscription_alert_window_days=14,
    )
    from datetime import UTC, datetime

    now = datetime(2026, 9, 23, 6, 0, tzinfo=UTC)
    with override_settings(settings):
        total = await worker.subscription_alerts_all_orgs(now)
        schedule = worker.create_celery().conf.beat_schedule

    assert total == len(seen) > 0
    assert {(today, window) for _org, today, window in seen} == {(TODAY, 14)}
    entry = schedule["subscription-alerts"]
    assert entry["task"] == "app.worker.subscription_alerts"
    assert entry["schedule"] == alerts.SWEEP_INTERVAL_S
