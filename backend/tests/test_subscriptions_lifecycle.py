"""Tests for the Subscriptions lifecycle actions (docs/modules/subscriptions/
spec.md §3): `cancel_subscription` + `record_renewal`.

Mirrors `tests/test_subscriptions_service.py`'s fixture style (throwaway org +
user seeded via `admin_engine`, service calls through an RLS-constrained
`org_session`, audit rows read back by `target_type`/`target_id` via the
`_audit_actions` helper) and reuses its `_org_with_user`/`_create_kwargs`
seeding pattern.
"""

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, Organization, User
from app.modules.subscriptions.errors import (
    SubscriptionNotFoundError,
    SubscriptionValidationError,
)
from app.modules.subscriptions.models import Subscription
from app.modules.subscriptions.service import (
    cancel_subscription,
    create_subscription,
    list_events,
    record_renewal,
)


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"sublc-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


def _create_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "vendor_id": None,
        "service_name": "Acme SaaS",
        "plan": "Pro",
        "category": "productivity",
        "billing_cycle": "monthly",
        "cost_per_cycle": Decimal("100.00"),
        "currency": "USD",
        "seats": 10,
        "owner_user_id": None,
        "started_at": None,
        "renews_at": None,
        "auto_renew": False,
        "notice_days": 30,
        "payment_ref": None,
        "license_ref": None,
        "status": "active",
    }
    kwargs.update(overrides)
    return kwargs


async def _audit_actions(
    app_engine: AsyncEngine, org_id: UUID, target_type: str, target_id: UUID
) -> list[str]:
    async with org_session(org_id, engine=app_engine) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == target_type,
                    AuditEvent.target_id == str(target_id),
                )
                # Callers assert on list ORDER, so the query must define one.
                # Without this, Postgres may return rows in any order it likes
                # and the plan shifts as audit_events grows -- an assertion
                # that passed for two phases can flip on an unrelated test
                # adding rows. `id` is uuid7, so it is chain-monotonic.
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


# ---------------------------------------------------------------------------
# cancel_subscription
# ---------------------------------------------------------------------------


async def test_cancel_subscription_sets_status_records_event_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_id, actor_id=actor_id, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        cancelled = await cancel_subscription(
            s, org_id=org_id, actor_id=actor_id, sub_id=sub_id
        )
        await s.commit()
        assert cancelled.status == "cancelled"

    async with org_session(org_id, engine=app_engine) as s:
        events = await list_events(s, org_id, sub_id)
        types = [e.type for e in events]
        assert types == ["created", "cancel"]
        assert events[1].details == {}

    actions = await _audit_actions(app_engine, org_id, "subscription", sub_id)
    assert actions == ["subscription.created", "subscription.cancelled"]


async def test_cancel_subscription_already_cancelled_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_id, actor_id=actor_id, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        await cancel_subscription(s, org_id=org_id, actor_id=actor_id, sub_id=sub_id)
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await cancel_subscription(s, org_id=org_id, actor_id=actor_id, sub_id=sub_id)


async def test_cancel_subscription_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await cancel_subscription(s, org_id=org_id, actor_id=actor_id, sub_id=uuid4())


async def test_cancel_subscription_cross_org_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    async with org_session(org_a, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_a, actor_id=actor_a, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_b, engine=app_engine) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await cancel_subscription(s, org_id=org_b, actor_id=actor_b, sub_id=sub_id)


# ---------------------------------------------------------------------------
# record_renewal
# ---------------------------------------------------------------------------


async def test_record_renewal_advances_renews_at_records_event_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s,
            org_id=org_id,
            actor_id=actor_id,
            **_create_kwargs(billing_cycle="monthly", renews_at=date(2026, 1, 31)),
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        renewed = await record_renewal(s, org_id=org_id, actor_id=actor_id, sub_id=sub_id)
        await s.commit()
        assert renewed.renews_at == date(2026, 2, 28)

    async with org_session(org_id, engine=app_engine) as s:
        events = await list_events(s, org_id, sub_id)
        types = [e.type for e in events]
        assert types == ["created", "renewal"]
        assert events[1].details == {"renewed_to": "2026-02-28"}

    actions = await _audit_actions(app_engine, org_id, "subscription", sub_id)
    assert actions == ["subscription.created", "subscription.renewed"]


async def test_record_renewal_no_renewal_date_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs(renews_at=None)
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await record_renewal(s, org_id=org_id, actor_id=actor_id, sub_id=sub_id)


async def test_record_renewal_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await record_renewal(s, org_id=org_id, actor_id=actor_id, sub_id=uuid4())


async def test_record_renewal_cross_org_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    async with org_session(org_a, engine=app_engine) as s:
        sub = await create_subscription(
            s,
            org_id=org_a,
            actor_id=actor_a,
            **_create_kwargs(renews_at=date(2026, 1, 31)),
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_b, engine=app_engine) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await record_renewal(s, org_id=org_b, actor_id=actor_b, sub_id=sub_id)


async def test_the_lifecycle_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    Both cross-org tests above run through `org_session` on the NOBYPASSRLS
    `app_user` role, where `0002_rls.py`'s forced `tenant_isolation` policy has
    already removed the neighbour's subscription before `_load_subscription`'s
    `Subscription.org_id == org_id` is consulted -- they raise
    `SubscriptionNotFoundError` with that predicate deleted just the same, and
    what they pin is the ERROR TYPE rather than the filter. This is the audit's
    own worked example: deleting that predicate left every test in
    `test_subscriptions_service.py` green and was caught only by a
    seat-assignment test in a different file, which is coverage by luck of the
    call graph. `its_admin` carries BYPASSRLS (`tests/conftest.py`), so here
    the predicate is the only scoping.

    BOTH DIRECTIONS ARE WRITES, which is why this file gets the test rather
    than the read-only one next door. `cancel_subscription` ends a service a
    tenant is paying for and `record_renewal` moves the date the billing
    reports are computed from; a miss is not a disclosure, it is one customer's
    administrator cancelling another customer's subscription.

    The neighbour's row is read back at the end, so the test cannot pass by
    raising for some reason unrelated to tenancy while the write landed anyway.

    FALSIFIED: deleting `Subscription.org_id == org_id` from
    `_load_subscription` turns both `pytest.raises` blocks into successful
    writes on org B's row.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    subs: dict[str, UUID] = {}
    for key, org_id, actor_id in (("a", org_a, actor_a), ("b", org_b, actor_b)):
        sub = await create_subscription(
            bypassrls_session,
            org_id=org_id,
            actor_id=actor_id,
            **_create_kwargs(service_name=f"service-{key}", renews_at=date(2026, 6, 1)),
        )
        subs[key] = sub.id
    await bypassrls_session.commit()

    with pytest.raises(SubscriptionNotFoundError):
        await cancel_subscription(
            bypassrls_session, org_id=org_a, actor_id=actor_a, sub_id=subs["b"]
        )
    await bypassrls_session.rollback()

    with pytest.raises(SubscriptionNotFoundError):
        await record_renewal(
            bypassrls_session, org_id=org_a, actor_id=actor_a, sub_id=subs["b"]
        )
    await bypassrls_session.rollback()

    theirs = (
        await bypassrls_session.execute(
            select(Subscription).where(Subscription.id == subs["b"])
        )
    ).scalar_one()
    assert (theirs.status, theirs.renews_at) == ("active", date(2026, 6, 1)), (
        "org A's cancel or renewal reached across the tenant boundary"
    )

    # Non-vacuity: the same two calls against org A's OWN subscription work.
    await cancel_subscription(
        bypassrls_session, org_id=org_a, actor_id=actor_a, sub_id=subs["a"]
    )
    await bypassrls_session.commit()
