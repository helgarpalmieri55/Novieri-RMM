"""Subscription notice-deadline and renewal alerts, pushed rather than read.

`reporting.renewal_calendar` answers "what is coming up" when someone opens the
page; a notice deadline nobody looked at passes silently, which defeats the
point. The daily sweep here publishes into the notification outbox instead:

- `subscription.notice_deadline_approaching` when `renews_at - notice_days`
  falls in `[today, today + window]` (only when `notice_days > 0`; with no
  notice period the deadline IS the renewal and one message is enough);
- `subscription.renewal_approaching` when `renews_at` does.

Each is published once per subscription per deadline, enforced by
`subscription_alert_sent`'s unique key. Both event types have no ticket and no
requester, so they are routed through `channel_default` rules. Each names its
subscription's client (plan 2026-10-26 Task 6); the claim row is H and takes
it from the subscription by trigger.
"""

from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import publish
from app.modules.subscriptions.models import Subscription, SubscriptionAlertSent

SWEEP_INTERVAL_S = 86400.0

EVENT_NOTICE = "subscription.notice_deadline_approaching"
EVENT_RENEWAL = "subscription.renewal_approaching"


def _payload(sub: Subscription, today: date, notice_deadline: date) -> dict[str, Any]:
    assert sub.renews_at is not None
    return {
        "subscription_id": str(sub.id),
        "service_name": sub.service_name,
        "plan": sub.plan,
        "renews_at": sub.renews_at.isoformat(),
        "notice_deadline": notice_deadline.isoformat(),
        "days_to_notice": (notice_deadline - today).days,
        "days_to_renewal": (sub.renews_at - today).days,
        "auto_renew": sub.auto_renew,
        # The org must act to avoid an unwanted renewal.
        "action_required": sub.auto_renew and today >= notice_deadline,
    }


async def _claim(
    session: AsyncSession, org_id: UUID, subscription_id: UUID, kind: str, deadline: date
) -> bool:
    return (
        await session.execute(
            pg_insert(SubscriptionAlertSent)
            .values(org_id=org_id, subscription_id=subscription_id, kind=kind, deadline=deadline)
            .on_conflict_do_nothing(constraint="uq_subscription_alert_sent")
            .returning(SubscriptionAlertSent.id)
        )
    ).scalar_one_or_none() is not None


async def emit_due_alerts(
    session: AsyncSession, org_id: UUID, *, today: date, window_days: int
) -> int:
    """Publish this org's due alerts not already sent. Returns the count."""
    window_end = today + timedelta(days=window_days)
    subs = (
        await session.execute(
            select(Subscription)
            .where(
                Subscription.org_id == org_id,
                Subscription.status == "active",
                Subscription.renews_at.is_not(None),
            )
            .order_by(Subscription.renews_at, Subscription.id)
        )
    ).scalars().all()

    published = 0
    for sub in subs:
        assert sub.renews_at is not None
        notice_deadline = sub.renews_at - timedelta(days=sub.notice_days)
        due: list[tuple[str, str, date]] = []
        if sub.notice_days > 0 and today <= notice_deadline <= window_end:
            due.append(("notice", EVENT_NOTICE, notice_deadline))
        if today <= sub.renews_at <= window_end:
            due.append(("renewal", EVENT_RENEWAL, sub.renews_at))
        for kind, event_type, deadline in due:
            if not await _claim(session, org_id, sub.id, kind, deadline):
                continue
            await publish(
                session,
                org_id=org_id,
                event_type=event_type,
                subject_type="subscription",
                subject_id=sub.id,
                payload=_payload(sub, today, notice_deadline),
                actor_user_id=None,
                client_id=sub.client_id,
            )
            published += 1
    await session.flush()
    return published
