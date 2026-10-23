from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    Organization,
    SeatAssignment,
    Subscription,
    SubscriptionEvent,
    User,
)


async def _org_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="subscriptions", slug=f"subscriptions-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, usr.id


async def test_subscriptions_models_roundtrip(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        # Test Subscription
        sub = Subscription(
            org_id=org_id,
            service_name="Acme SaaS",
            billing_cycle="monthly",
            cost_per_cycle=Decimal("9.99"),
            currency="USD",
            created_by=user_id,
        )
        s.add(sub)
        await s.flush()
        assert sub.vendor_id is None
        assert sub.plan == ""
        assert sub.category == ""
        assert sub.cost_per_cycle == Decimal("9.99")
        assert isinstance(sub.cost_per_cycle, Decimal)
        assert sub.seats == 0
        assert sub.owner_user_id is None
        assert sub.started_at is None
        assert sub.renews_at is None
        assert sub.auto_renew is False
        assert sub.notice_days == 0
        assert sub.payment_ref is None
        assert sub.license_ref is None
        assert sub.status == "active"

        # Test Subscription with more fields set
        sub2 = Subscription(
            org_id=org_id,
            service_name="Acme Pro",
            plan="pro",
            category="productivity",
            billing_cycle="annual",
            cost_per_cycle=Decimal("1234.56"),
            currency="USD",
            seats=25,
            owner_user_id=user_id,
            started_at=date(2026, 1, 1),
            renews_at=date(2027, 1, 1),
            auto_renew=True,
            notice_days=30,
            payment_ref="PAY-1",
            license_ref=uuid4(),
            status="active",
            created_by=user_id,
        )
        s.add(sub2)
        await s.flush()
        assert sub2.owner_user_id == user_id
        assert sub2.auto_renew is True

        # Test SeatAssignment
        seat = SeatAssignment(
            org_id=org_id,
            subscription_id=sub.id,
            assignee_name="Jane Doe",
            assignee_email="jane@example.com",
            created_by=user_id,
        )
        s.add(seat)
        await s.flush()
        assert seat.released_at is None

        # Test SubscriptionEvent
        event = SubscriptionEvent(
            org_id=org_id,
            subscription_id=sub.id,
            type="created",
            created_by=user_id,
        )
        s.add(event)
        await s.flush()
        assert event.details == {}

    # Verify round-trip with fresh session
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        got_sub = (
            await s.execute(select(Subscription).where(Subscription.id == sub.id))
        ).scalar_one()
        assert got_sub.service_name == "Acme SaaS"
        assert got_sub.cost_per_cycle == Decimal("9.99")
        assert isinstance(got_sub.cost_per_cycle, Decimal)
        assert got_sub.currency == "USD"
        assert got_sub.plan == ""
        assert got_sub.category == ""
        assert got_sub.seats == 0
        assert got_sub.owner_user_id is None
        assert got_sub.started_at is None
        assert got_sub.renews_at is None
        assert got_sub.auto_renew is False
        assert got_sub.notice_days == 0
        assert got_sub.payment_ref is None
        assert got_sub.license_ref is None
        assert got_sub.status == "active"

        got_sub2 = (
            await s.execute(select(Subscription).where(Subscription.id == sub2.id))
        ).scalar_one()
        assert got_sub2.license_ref == sub2.license_ref
        assert got_sub2.started_at == date(2026, 1, 1)
        assert got_sub2.renews_at == date(2027, 1, 1)
        assert got_sub2.auto_renew is True

        got_seat = (
            await s.execute(select(SeatAssignment).where(SeatAssignment.id == seat.id))
        ).scalar_one()
        assert got_seat.assignee_name == "Jane Doe"
        assert got_seat.assignee_email == "jane@example.com"
        assert got_seat.released_at is None

        got_event = (
            await s.execute(
                select(SubscriptionEvent).where(SubscriptionEvent.id == event.id)
            )
        ).scalar_one()
        assert got_event.type == "created"
        assert got_event.details == {}
