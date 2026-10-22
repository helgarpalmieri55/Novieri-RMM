"""Tests for the Subscriptions service (docs/modules/subscriptions/spec.md
§3 lifecycle & change history, §6 hybrid license link, §8 invariants).

Mirrors `tests/test_licenses_service.py`'s fixture style (throwaway org + user
seeded via `admin_engine`, service calls through an RLS-constrained
`org_session`, audit rows read back by `target_type`/`target_id`) plus the
Phase-10/11 load-bearing pattern: one cross-ref resolve is re-run directly on
an RLS-bypassing `admin_engine` session to prove the explicit `org_id`
predicate -- not RLS -- is what rejects a foreign-org id.
"""

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, License, Organization, User, Vendor
from app.modules.subscriptions.errors import (
    SubscriptionNotFoundError,
    SubscriptionValidationError,
)
from app.modules.subscriptions.service import (
    assign_seat,
    create_subscription,
    get_subscription,
    list_events,
    list_subscriptions,
    release_seat,
    update_subscription,
)


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"subsvc-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _vendor(admin_engine: AsyncEngine, org_id: UUID, actor_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        vendor = Vendor(
            org_id=org_id, name="Vendor", website=None, contacts=[], created_by=actor_id
        )
        s.add(vendor)
        await s.commit()
        return vendor.id


async def _license(admin_engine: AsyncEngine, org_id: UUID, actor_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        from app.models import SoftwareProduct

        product = SoftwareProduct(
            org_id=org_id, name="Product", publisher="Pub", match_rules=[], created_by=actor_id
        )
        s.add(product)
        await s.flush()
        license_ = License(
            org_id=org_id,
            product_id=product.id,
            vendor_id=None,
            type="perpetual",
            entitlements=1,
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
            created_by=actor_id,
        )
        s.add(license_)
        await s.commit()
        return license_.id


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
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


async def _audit_meta(
    app_engine: AsyncEngine, org_id: UUID, target_type: str, target_id: UUID, action: str
) -> dict[str, Any]:
    """The `meta` payload of the single audit row for `action` on this target."""
    async with org_session(org_id, engine=app_engine) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == target_type,
                    AuditEvent.target_id == str(target_id),
                    AuditEvent.action == action,
                )
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        assert len(events) == 1, f"expected exactly one {action} row, got {len(events)}"
        meta: dict[str, Any] = events[0].meta
        return meta


# ---------------------------------------------------------------------------
# create_subscription
# ---------------------------------------------------------------------------


async def test_create_subscription_records_created_event_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs()
        )
        await s.commit()
        sub_id = sub.id

    assert sub.service_name == "Acme SaaS"

    async with org_session(org_id, engine=app_engine) as s:
        events = await list_events(s, org_id, sub_id)
        assert len(events) == 1
        assert events[0].type == "created"
        assert events[0].details == {"service_name": "Acme SaaS"}

    actions = await _audit_actions(app_engine, org_id, "subscription", sub_id)
    assert actions == ["subscription.created"]

    async with org_session(org_id, engine=app_engine) as s:
        fetched = await get_subscription(s, org_id, sub_id)
        assert fetched is not None
        assert fetched.id == sub_id

        all_subs = await list_subscriptions(s, org_id)
        assert [row.id for row in all_subs] == [sub_id]


async def test_create_subscription_invalid_billing_cycle_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await create_subscription(
                s,
                org_id=org_id,
                actor_id=actor_id,
                **_create_kwargs(billing_cycle="weekly"),
            )


async def test_create_subscription_invalid_status_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await create_subscription(
                s,
                org_id=org_id,
                actor_id=actor_id,
                **_create_kwargs(status="expired"),
            )


async def test_create_subscription_cross_org_vendor_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    vendor_b = await _vendor(admin_engine, org_b, actor_b)

    async with org_session(org_a, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await create_subscription(
                s,
                org_id=org_a,
                actor_id=actor_a,
                **_create_kwargs(vendor_id=vendor_b),
            )


async def test_create_subscription_cross_org_owner_user_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    async with org_session(org_a, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await create_subscription(
                s,
                org_id=org_a,
                actor_id=actor_a,
                **_create_kwargs(owner_user_id=actor_b),
            )


async def test_create_subscription_cross_org_license_ref_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    license_b = await _license(admin_engine, org_b, actor_b)

    async with org_session(org_a, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await create_subscription(
                s,
                org_id=org_a,
                actor_id=actor_a,
                **_create_kwargs(license_ref=license_b),
            )


async def test_create_subscription_valid_license_ref_succeeds(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    license_id = await _license(admin_engine, org_id, actor_id)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s,
            org_id=org_id,
            actor_id=actor_id,
            **_create_kwargs(license_ref=license_id),
        )
        await s.commit()
        assert sub.license_ref == license_id


async def test_create_subscription_cross_ref_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Isolate the explicit `Vendor.org_id == org_id` predicate in
    `_validate_refs` so it is the SOLE tenant guard on the cross-ref resolve.

    Runs `create_subscription` directly on an RLS-bypassing `admin_engine`
    session (mirrors `tests/test_licenses_allocation.py`'s
    `test_allocate_target_resolve_org_filter_is_load_bearing` /
    `test_evidence_source_queries_org_filter_is_load_bearing` pattern),
    removing RLS as a backstop. If the `Vendor.org_id == org_id` predicate
    were deleted, the foreign-org vendor would resolve and this test would
    fail (no exception raised) -- mutation-verified by temporarily deleting
    that predicate locally and confirming this test fails.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    vendor_b = await _vendor(admin_engine, org_b, actor_b)

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        with pytest.raises(SubscriptionValidationError):
            await create_subscription(
                s,
                org_id=org_a,
                actor_id=actor_a,
                **_create_kwargs(vendor_id=vendor_b),
            )


async def test_create_subscription_owner_user_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Isolate the explicit `User.org_id == org_id` predicate in
    `_validate_refs` so it is the SOLE tenant guard on the owner resolve.

    `test_create_subscription_cross_org_owner_user_raises_validation_error`
    runs under `org_session(..., engine=app_engine)`, i.e. the non-BYPASSRLS
    `app_user` role -- RLS alone already hides org B's user row on that
    connection, so that test cannot prove the application-level predicate
    does anything. Here `create_subscription` runs on a raw
    `AsyncSession(admin_engine)` (`its_admin` is BYPASSRLS -- see
    `tests/conftest.py`), so org B's user row is fully visible and only
    `_validate_refs`'s own `User.org_id` predicate can reject it.

    Mutation-verified: deleting the `User.org_id == org_id` predicate from
    `_validate_refs` makes this test fail (the foreign owner resolves, no
    exception is raised); restoring it makes the test pass again. See
    `.git/sdd/phase12-fix-backend-report.md` for the observed failure output.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    _org_b, actor_b = await _org_with_user(admin_engine)

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        with pytest.raises(SubscriptionValidationError):
            await create_subscription(
                s,
                org_id=org_a,
                actor_id=actor_a,
                **_create_kwargs(owner_user_id=actor_b),
            )


async def test_create_subscription_license_ref_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Isolate the explicit `License.org_id == org_id` predicate in
    `_validate_refs` so it is the SOLE tenant guard on the license resolve.

    Same reasoning as the owner-user case above:
    `test_create_subscription_cross_org_license_ref_raises_validation_error`
    runs under RLS and so is hollow; this one runs on a raw
    `AsyncSession(admin_engine)` where org B's license row is fully visible.

    Mutation-verified: deleting the `License.org_id == org_id` predicate from
    `_validate_refs` makes this test fail (the foreign license resolves, no
    exception is raised); restoring it makes the test pass again. See
    `.git/sdd/phase12-fix-backend-report.md` for the observed failure output.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    license_b = await _license(admin_engine, org_b, actor_b)

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        with pytest.raises(SubscriptionValidationError):
            await create_subscription(
                s,
                org_id=org_a,
                actor_id=actor_a,
                **_create_kwargs(license_ref=license_b),
            )


# ---------------------------------------------------------------------------
# update_subscription
# ---------------------------------------------------------------------------


async def test_update_subscription_cost_change_records_price_change_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs(cost_per_cycle=Decimal("100.00"))
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        updated = await update_subscription(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            cost_per_cycle=Decimal("150.00"),
        )
        await s.commit()
        assert updated.cost_per_cycle == Decimal("150.00")

    async with org_session(org_id, engine=app_engine) as s:
        events = await list_events(s, org_id, sub_id)
        types = [e.type for e in events]
        assert types == ["created", "price_change"]
        price_event = events[1]
        assert price_event.details == {"from": "100.00", "to": "150.00", "currency": "USD"}

    actions = await _audit_actions(app_engine, org_id, "subscription", sub_id)
    assert actions == ["subscription.created", "subscription.updated"]


async def test_update_subscription_unchanged_field_records_no_extra_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs(cost_per_cycle=Decimal("100.00"))
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_subscription(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            cost_per_cycle=Decimal("100.00"),
            payment_ref="INV-123",
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        events = await list_events(s, org_id, sub_id)
        types = [e.type for e in events]
        assert types == ["created"]


async def test_update_subscription_seats_change_records_seat_change_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs(seats=10)
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_subscription(
            s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, seats=15
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        events = await list_events(s, org_id, sub_id)
        types = [e.type for e in events]
        assert types == ["created", "seat_change"]
        assert events[1].details == {"from": 10, "to": 15}


async def test_update_subscription_plan_change_records_plan_change_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs(plan="Pro")
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_subscription(
            s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, plan="Enterprise"
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        events = await list_events(s, org_id, sub_id)
        types = [e.type for e in events]
        assert types == ["created", "plan_change"]
        assert events[1].details == {"from": "Pro", "to": "Enterprise"}


async def test_update_subscription_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionNotFoundError):
            await update_subscription(
                s, org_id=org_id, actor_id=actor_id, sub_id=uuid4(), service_name="x"
            )


async def test_update_subscription_invalid_billing_cycle_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_id, actor_id=actor_id, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await update_subscription(
                s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, billing_cycle="biweekly"
            )


async def test_update_subscription_invalid_status_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_id, actor_id=actor_id, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await update_subscription(
                s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, status="expired"
            )


async def test_update_subscription_cross_org_vendor_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    vendor_b = await _vendor(admin_engine, org_b, actor_b)

    async with org_session(org_a, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_a, actor_id=actor_a, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_a, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await update_subscription(
                s, org_id=org_a, actor_id=actor_a, sub_id=sub_id, vendor_id=vendor_b
            )


async def test_update_subscription_cross_org_owner_user_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    async with org_session(org_a, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_a, actor_id=actor_a, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_a, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await update_subscription(
                s, org_id=org_a, actor_id=actor_a, sub_id=sub_id, owner_user_id=actor_b
            )


async def test_update_subscription_cross_org_license_ref_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    license_b = await _license(admin_engine, org_b, actor_b)

    async with org_session(org_a, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_a, actor_id=actor_a, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_a, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await update_subscription(
                s, org_id=org_a, actor_id=actor_a, sub_id=sub_id, license_ref=license_b
            )


# ---------------------------------------------------------------------------
# update_subscription -- explicit null (I2): "omitted" and "explicitly null"
# must be distinguishable. A supplied `None` clears a nullable column; a
# supplied `None` on a NOT NULL column is a 400-class validation error, never
# an IntegrityError/500.
# ---------------------------------------------------------------------------


async def test_update_subscription_explicit_null_clears_nullable_field(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s,
            org_id=org_id,
            actor_id=actor_id,
            **_create_kwargs(renews_at=date(2026, 6, 1), payment_ref="INV-1"),
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        updated = await update_subscription(
            s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, renews_at=None
        )
        await s.commit()
        assert updated.renews_at is None

    async with org_session(org_id, engine=app_engine) as s:
        fetched = await get_subscription(s, org_id, sub_id)
        assert fetched is not None
        assert fetched.renews_at is None
        # an omitted field is untouched -- clearing one nullable column must
        # not blank the others.
        assert fetched.payment_ref == "INV-1"


async def test_update_subscription_explicit_null_clears_every_nullable_column(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """All six nullable columns must be clearable, not just `renews_at`."""
    org_id, actor_id = await _org_with_user(admin_engine)
    vendor_id = await _vendor(admin_engine, org_id, actor_id)
    license_id = await _license(admin_engine, org_id, actor_id)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s,
            org_id=org_id,
            actor_id=actor_id,
            **_create_kwargs(
                vendor_id=vendor_id,
                owner_user_id=actor_id,
                started_at=date(2025, 1, 1),
                renews_at=date(2026, 6, 1),
                payment_ref="INV-1",
                license_ref=license_id,
            ),
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_subscription(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            vendor_id=None,
            owner_user_id=None,
            started_at=None,
            renews_at=None,
            payment_ref=None,
            license_ref=None,
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        fetched = await get_subscription(s, org_id, sub_id)
        assert fetched is not None
        assert fetched.vendor_id is None
        assert fetched.owner_user_id is None
        assert fetched.started_at is None
        assert fetched.renews_at is None
        assert fetched.payment_ref is None
        assert fetched.license_ref is None


@pytest.mark.parametrize(
    ("field", "sentinel"),
    [
        ("service_name", None),
        ("billing_cycle", None),
        ("cost_per_cycle", None),
        ("currency", None),
        ("seats", None),
        ("status", None),
        ("auto_renew", None),
        ("notice_days", None),
        ("plan", None),
        ("category", None),
    ],
)
async def test_update_subscription_explicit_null_on_non_nullable_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, field: str, sentinel: None
) -> None:
    """An explicit `null` on a NOT NULL column is a validation error (-> 400),
    never an `IntegrityError` surfacing as a 500."""
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_id, actor_id=actor_id, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await update_subscription(
                s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, **{field: sentinel}
            )


async def test_update_subscription_omitted_field_is_untouched(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s,
            org_id=org_id,
            actor_id=actor_id,
            **_create_kwargs(service_name="Acme SaaS", plan="Pro", payment_ref="INV-9"),
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_subscription(
            s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, plan="Enterprise"
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        fetched = await get_subscription(s, org_id, sub_id)
        assert fetched is not None
        assert fetched.plan == "Enterprise"
        assert fetched.service_name == "Acme SaaS"
        assert fetched.payment_ref == "INV-9"


# ---------------------------------------------------------------------------
# update_subscription -- audit metadata (I6/M1): the `subscription.updated`
# row must say WHAT changed, not merely that something did.
# ---------------------------------------------------------------------------


async def test_update_subscription_audit_metadata_lists_changed_field_names(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_id, actor_id=actor_id, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_subscription(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            status="trial",
            payment_ref="INV-77",
            auto_renew=True,
        )
        await s.commit()

    meta = await _audit_meta(
        app_engine, org_id, "subscription", sub_id, "subscription.updated"
    )
    # sorted field NAMES only -- names are enough to make the row useful and
    # sidestep any value-sensitivity question.
    assert meta["changed_fields"] == ["auto_renew", "payment_ref", "status"]


async def test_update_subscription_audit_metadata_records_a_cleared_field(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A field cleared to `null` is a real change and must appear by name."""
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs(renews_at=date(2026, 6, 1))
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_subscription(
            s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, renews_at=None
        )
        await s.commit()

    meta = await _audit_meta(
        app_engine, org_id, "subscription", sub_id, "subscription.updated"
    )
    assert meta["changed_fields"] == ["renews_at"]


async def test_update_subscription_audit_metadata_empty_when_nothing_changed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs(plan="Pro")
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_subscription(
            s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, plan="Pro"
        )
        await s.commit()

    meta = await _audit_meta(
        app_engine, org_id, "subscription", sub_id, "subscription.updated"
    )
    assert meta["changed_fields"] == []


# ---------------------------------------------------------------------------
# update_subscription -- seat-count floor (M3): `seats` may never be reduced
# below the number of currently active assignments, which would make
# `idle`/`available_seats` negative.
# ---------------------------------------------------------------------------


async def test_update_subscription_seats_below_active_count_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs(seats=7)
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        for n in range(7):
            await assign_seat(
                s,
                org_id=org_id,
                actor_id=actor_id,
                sub_id=sub_id,
                assignee_name=f"Person {n}",
                assignee_email=f"person{n}@example.com",
            )
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(SubscriptionValidationError):
            await update_subscription(
                s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, seats=2
            )

    async with org_session(org_id, engine=app_engine) as s:
        fetched = await get_subscription(s, org_id, sub_id)
        assert fetched is not None
        assert fetched.seats == 7


async def test_update_subscription_seats_down_to_active_count_is_allowed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The floor is inclusive: reducing to exactly the active count is fine."""
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs(seats=5)
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        for n in range(2):
            await assign_seat(
                s,
                org_id=org_id,
                actor_id=actor_id,
                sub_id=sub_id,
                assignee_name=f"Person {n}",
                assignee_email=f"person{n}@example.com",
            )
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        updated = await update_subscription(
            s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, seats=2
        )
        await s.commit()
        assert updated.seats == 2


async def test_update_subscription_seats_reduction_ignores_released_seats(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Only ACTIVE (unreleased) assignments set the floor."""
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(
            s, org_id=org_id, actor_id=actor_id, **_create_kwargs(seats=5)
        )
        await s.commit()
        sub_id = sub.id

    async with org_session(org_id, engine=app_engine) as s:
        seat = await assign_seat(
            s,
            org_id=org_id,
            actor_id=actor_id,
            sub_id=sub_id,
            assignee_name="Alice",
            assignee_email="alice@example.com",
        )
        await s.commit()
        seat_id = seat.id

    async with org_session(org_id, engine=app_engine) as s:
        await release_seat(s, org_id=org_id, actor_id=actor_id, assignment_id=seat_id)
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        updated = await update_subscription(
            s, org_id=org_id, actor_id=actor_id, sub_id=sub_id, seats=0
        )
        await s.commit()
        assert updated.seats == 0


# ---------------------------------------------------------------------------
# get_subscription / list_subscriptions / list_events -- no audit, tenancy
# ---------------------------------------------------------------------------


async def test_get_list_events_write_no_audit(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_id, actor_id=actor_id, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    before = await _audit_actions(app_engine, org_id, "subscription", sub_id)

    async with org_session(org_id, engine=app_engine) as s:
        await get_subscription(s, org_id, sub_id)
        await list_subscriptions(s, org_id)
        await list_events(s, org_id, sub_id)

    after = await _audit_actions(app_engine, org_id, "subscription", sub_id)
    assert after == before


async def test_get_subscription_missing_returns_none(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _actor_id = await _org_with_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        result = await get_subscription(s, org_id, uuid4())
        assert result is None


async def test_another_orgs_subscription_is_hidden_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    async with org_session(org_a, engine=app_engine) as s:
        sub = await create_subscription(s, org_id=org_a, actor_id=actor_a, **_create_kwargs())
        await s.commit()
        sub_id = sub.id

    async with org_session(org_b, engine=app_engine) as s:
        fetched = await get_subscription(s, org_b, sub_id)
        assert fetched is None

        all_subs = await list_subscriptions(s, org_b)
        assert all_subs == []

        events = await list_events(s, org_b, sub_id)
        assert events == []

        with pytest.raises(SubscriptionNotFoundError):
            await update_subscription(
                s, org_id=org_b, actor_id=actor_b, sub_id=sub_id, service_name="hijacked"
            )
