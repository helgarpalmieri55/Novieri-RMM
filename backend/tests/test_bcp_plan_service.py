"""Tests for the BCP/DR plan service (docs/modules/bcp/spec.md): CRUD, review
sign-off, and org-scoped scope/procedure-ref validation.

Mirrors `tests/test_change_mgmt_service.py`'s fixture style: a throwaway org +
user seeded directly via `admin_engine`, service calls run through an
RLS-constrained `org_session`, and audit rows are read back by
`target_type`/`target_id`.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, BcpPlan, Device, DeviceGroup, DocArticle, Organization, User
from app.modules.bcp.errors import BcpNotFoundError, BcpValidationError
from app.modules.bcp.service import (
    create_plan,
    get_plan,
    list_plans,
    resolve_scope_devices,
    review_plan,
    update_plan,
)


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"bcpsvc-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _seed_group(admin_engine: AsyncEngine, org_id: UUID, name: str = "Core") -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        group = DeviceGroup(org_id=org_id, name=name)
        s.add(group)
        await s.commit()
        return group.id


async def _seed_device(
    admin_engine: AsyncEngine, org_id: UUID, hostname: str, group_id: UUID | None = None
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org_id,
            hostname=hostname,
            os_family="linux",
            device_group_id=group_id,
        )
        s.add(device)
        await s.commit()
        return device.id


async def _seed_doc_article(
    admin_engine: AsyncEngine, org_id: UUID, title: str = "Runbook"
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        article = DocArticle(org_id=org_id, title=title, body="steps")
        s.add(article)
        await s.commit()
        return article.id


async def _audit_actions(app_engine: AsyncEngine, org_id: UUID, plan_id: UUID) -> list[str]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == "bcp_plan",
                    AuditEvent.target_id == str(plan_id),
                )
                # Callers assert on list ORDER, so the query must define one.
                # `id` is uuid7 and therefore chain-monotonic.
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


# ---------------------------------------------------------------------------
# create_plan
# ---------------------------------------------------------------------------


async def test_create_plan_with_valid_refs_succeeds_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    group_id = await _seed_group(admin_engine, org_id)
    device_id = await _seed_device(admin_engine, org_id, "host-1", group_id)
    doc_id = await _seed_doc_article(admin_engine, org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        plan = await create_plan(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="DR Plan",
            description="Recover the core stack",
            scope=[
                {"kind": "device", "id": str(device_id)},
                {"kind": "group", "id": str(group_id)},
            ],
            rto_minutes=60,
            rpo_minutes=15,
            procedure_refs=[str(doc_id)],
            dependencies=[],
            review_interval_days=180,
        )
        await s.commit()
        plan_id = plan.id

    assert plan.state == "active"
    assert plan.created_by == actor_id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (await s.execute(select(BcpPlan).where(BcpPlan.id == plan_id))).scalar_one()
        assert row.name == "DR Plan"
        assert row.review_interval_days == 180

    actions = await _audit_actions(app_engine, org_id, plan_id)
    assert actions == ["bcp.plan_created"]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        event = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "bcp.plan_created"
                )
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == actor_id
        assert event.meta["name"] == "DR Plan"


async def test_create_plan_scope_device_from_another_org_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    other_org_id, _ = await _org_with_user(admin_engine)
    foreign_device_id = await _seed_device(admin_engine, other_org_id, "foreign-host")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await create_plan(
                s,
                org_id=org_id,
                actor_id=actor_id,
                name="DR Plan",
                description="",
                scope=[{"kind": "device", "id": str(foreign_device_id)}],
                rto_minutes=None,
                rpo_minutes=None,
                procedure_refs=[],
                dependencies=[],
                review_interval_days=365,
            )


async def test_create_plan_scope_random_device_id_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await create_plan(
                s,
                org_id=org_id,
                actor_id=actor_id,
                name="DR Plan",
                description="",
                scope=[{"kind": "device", "id": str(uuid4())}],
                rto_minutes=None,
                rpo_minutes=None,
                procedure_refs=[],
                dependencies=[],
                review_interval_days=365,
            )


async def test_create_plan_unknown_scope_kind_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await create_plan(
                s,
                org_id=org_id,
                actor_id=actor_id,
                name="DR Plan",
                description="",
                scope=[{"kind": "site", "id": str(uuid4())}],
                rto_minutes=None,
                rpo_minutes=None,
                procedure_refs=[],
                dependencies=[],
                review_interval_days=365,
            )


async def test_create_plan_malformed_scope_uuid_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await create_plan(
                s,
                org_id=org_id,
                actor_id=actor_id,
                name="DR Plan",
                description="",
                scope=[{"kind": "device", "id": "not-a-uuid"}],
                rto_minutes=None,
                rpo_minutes=None,
                procedure_refs=[],
                dependencies=[],
                review_interval_days=365,
            )


async def test_create_plan_cross_org_procedure_ref_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    other_org_id, _ = await _org_with_user(admin_engine)
    foreign_doc_id = await _seed_doc_article(admin_engine, other_org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await create_plan(
                s,
                org_id=org_id,
                actor_id=actor_id,
                name="DR Plan",
                description="",
                scope=[],
                rto_minutes=None,
                rpo_minutes=None,
                procedure_refs=[str(foreign_doc_id)],
                dependencies=[],
                review_interval_days=365,
            )


async def test_create_plan_random_procedure_ref_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await create_plan(
                s,
                org_id=org_id,
                actor_id=actor_id,
                name="DR Plan",
                description="",
                scope=[],
                rto_minutes=None,
                rpo_minutes=None,
                procedure_refs=[str(uuid4())],
                dependencies=[],
                review_interval_days=365,
            )


# ---------------------------------------------------------------------------
# update_plan
# ---------------------------------------------------------------------------


async def test_update_plan_applies_fields_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        plan = await create_plan(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Original",
            description="Original desc",
            scope=[],
            rto_minutes=None,
            rpo_minutes=None,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=365,
        )
        await s.commit()
        plan_id = plan.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        updated = await update_plan(
            s, org_id=org_id, actor_id=actor_id, plan_id=plan_id, name="Renamed", rto_minutes=30
        )
        await s.commit()
        assert updated.name == "Renamed"
        assert updated.rto_minutes == 30
        assert updated.description == "Original desc"

    actions = await _audit_actions(app_engine, org_id, plan_id)
    assert actions == ["bcp.plan_created", "bcp.plan_updated"]


async def test_update_plan_revalidates_new_scope(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    other_org_id, _ = await _org_with_user(admin_engine)
    foreign_device_id = await _seed_device(admin_engine, other_org_id, "foreign-host")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        plan = await create_plan(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Original",
            description="",
            scope=[],
            rto_minutes=None,
            rpo_minutes=None,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=365,
        )
        await s.commit()
        plan_id = plan.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpValidationError):
            await update_plan(
                s,
                org_id=org_id,
                actor_id=actor_id,
                plan_id=plan_id,
                scope=[{"kind": "device", "id": str(foreign_device_id)}],
            )


async def test_update_plan_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpNotFoundError):
            await update_plan(
                s, org_id=org_id, actor_id=actor_id, plan_id=uuid4(), name="nope"
            )


# ---------------------------------------------------------------------------
# review_plan
# ---------------------------------------------------------------------------


async def test_review_plan_sets_reviewed_fields_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        plan = await create_plan(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Plan",
            description="",
            scope=[],
            rto_minutes=None,
            rpo_minutes=None,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=365,
        )
        await s.commit()
        plan_id = plan.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        reviewed = await review_plan(s, org_id=org_id, actor_id=actor_id, plan_id=plan_id)
        await s.commit()
        assert reviewed.last_reviewed_at is not None
        assert reviewed.approved_by == actor_id

    actions = await _audit_actions(app_engine, org_id, plan_id)
    assert actions == ["bcp.plan_created", "bcp.plan_reviewed"]


async def test_review_plan_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(BcpNotFoundError):
            await review_plan(s, org_id=org_id, actor_id=actor_id, plan_id=uuid4())


# ---------------------------------------------------------------------------
# resolve_scope_devices
# ---------------------------------------------------------------------------


async def testresolve_scope_devices_expands_group_and_includes_direct_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _actor_id = await _org_with_user(admin_engine)
    group_id = await _seed_group(admin_engine, org_id)
    member_1 = await _seed_device(admin_engine, org_id, "member-1", group_id)
    member_2 = await _seed_device(admin_engine, org_id, "member-2", group_id)
    direct_device = await _seed_device(admin_engine, org_id, "direct-1")

    other_org_id, _ = await _org_with_user(admin_engine)
    # Crafted cross-tenant pointer: an org-B device whose device_group_id is org-A's
    # group id. resolve_scope_devices MUST exclude it via the Device.org_id
    # predicate — if that predicate were removed, this device would leak in.
    other_org_leak = await _seed_device(admin_engine, other_org_id, "other-org-host", group_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        resolved = await resolve_scope_devices(
            s,
            org_id,
            [
                {"kind": "group", "id": str(group_id)},
                {"kind": "device", "id": str(direct_device)},
            ],
        )

    assert resolved == {member_1, member_2, direct_device}
    assert other_org_leak not in resolved  # org_id predicate excludes the cross-org device


# ---------------------------------------------------------------------------
# get_plan / list_plans + tenancy
# ---------------------------------------------------------------------------


async def test_get_plan_and_list_plans(admin_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        plan = await create_plan(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Plan",
            description="",
            scope=[],
            rto_minutes=None,
            rpo_minutes=None,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=365,
        )
        await s.commit()
        plan_id = plan.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        fetched = await get_plan(s, org_id, plan_id)
        assert fetched is not None
        assert fetched.id == plan_id

        all_plans = await list_plans(s, org_id)
        assert [p.id for p in all_plans] == [plan_id]


async def test_get_plan_missing_returns_none(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _actor_id = await _org_with_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await get_plan(s, org_id, uuid4())
        assert result is None


async def test_another_orgs_plan_is_hidden_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, _actor_b = await _org_with_user(admin_engine)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        plan = await create_plan(
            s,
            org_id=org_a,
            actor_id=actor_a,
            name="Plan A",
            description="",
            scope=[],
            rto_minutes=None,
            rpo_minutes=None,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=365,
        )
        await s.commit()
        plan_id = plan.id

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        fetched = await get_plan(s, org_b, plan_id)
        assert fetched is None

        all_plans = await list_plans(s, org_b)
        assert all_plans == []


async def test_update_plan_rejects_invalid_state(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        plan = await create_plan(
            s, org_id=org_id, actor_id=actor_id, name="p", description="",
            scope=[], rto_minutes=None, rpo_minutes=None, procedure_refs=[],
            dependencies=[], review_interval_days=365,
        )
        with pytest.raises(BcpValidationError):
            await update_plan(s, org_id=org_id, actor_id=actor_id, plan_id=plan.id, state="bogus")


# ---------------------------------------------------------------------------
# The org predicate, where RLS is not the thing rejecting
# ---------------------------------------------------------------------------


async def test_the_plan_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    Every other tenancy assertion here runs through `org_session` on the
    `app_user` role, where `0002_rls.py`'s forced `tenant_isolation` policy
    rejects the neighbour's rows before the service's own `WHERE org_id` is
    consulted -- so they stay green with those predicates deleted. `its_admin`
    carries BYPASSRLS (`tests/conftest.py`), so here the SQL is the only thing
    scoping anything.

    Three predicates, chosen because they fail in three different directions:

    * `_validate_scope` -- a REFUSAL. Naming org B's device in org A's scope
      must raise, or org A's plan documents a machine it does not own.
    * `list_plans` -- a READ. The whole-table select is where an org-wide
      answer hides.
    * `resolve_scope_devices` -- the one with no schema backstop.
      `devices.device_group_id` is a bare FK with no org-aware constraint, so
      org B's device row may legally name org A's group; the group expansion's
      `Device.org_id == org_id` is the ONLY thing that keeps that device out
      of org A's evidence set.

    FALSIFIED, one predicate at a time: dropping `model.org_id == org_id` from
    `_validate_scope` lets the cross-org scope be created; dropping
    `BcpPlan.org_id == org_id` from `list_plans` returns both orgs' plans;
    dropping `Device.org_id == org_id` from the group branch of
    `resolve_scope_devices` pulls org B's device into org A's set.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    group_a = await _seed_group(admin_engine, org_a, name="core-a")
    device_a = await _seed_device(admin_engine, org_a, "host-a", group_a)
    device_b = await _seed_device(admin_engine, org_b, "host-b")
    # The row nothing in the schema forbids: THEIR device naming MY group.
    async with AsyncSession(admin_engine) as s:
        theirs = (await s.execute(select(Device).where(Device.id == device_b))).scalar_one()
        theirs.device_group_id = group_a
        await s.commit()

    # 1. The refusal: org B's device may not enter org A's scope.
    with pytest.raises(BcpValidationError):
        await create_plan(
            bypassrls_session,
            org_id=org_a,
            actor_id=actor_a,
            name="cross-org scope",
            description="",
            scope=[{"kind": "device", "id": str(device_b)}],
            rto_minutes=None,
            rpo_minutes=None,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=365,
        )
    await bypassrls_session.rollback()

    # 2. The read: one plan each, and neither org sees the other's.
    plans: dict[str, UUID] = {}
    for key, org_id, actor_id, device_id in (
        ("a", org_a, actor_a, device_a),
        ("b", org_b, actor_b, device_b),
    ):
        plan = await create_plan(
            bypassrls_session,
            org_id=org_id,
            actor_id=actor_id,
            name=f"plan-{key}",
            description="",
            scope=[{"kind": "device", "id": str(device_id)}],
            rto_minutes=None,
            rpo_minutes=None,
            procedure_refs=[],
            dependencies=[],
            review_interval_days=365,
        )
        plans[key] = plan.id
    await bypassrls_session.commit()

    assert [p.id for p in await list_plans(bypassrls_session, org_a)] == [plans["a"]]
    assert [p.id for p in await list_plans(bypassrls_session, org_b)] == [plans["b"]]
    assert await get_plan(bypassrls_session, org_a, plans["b"]) is None
    assert await get_plan(bypassrls_session, org_a, plans["a"]) is not None

    # 3. The expansion with no schema backstop.
    resolved = await resolve_scope_devices(
        bypassrls_session, org_a, [{"kind": "group", "id": str(group_a)}]
    )
    assert resolved == {device_a}, (
        "a neighbour's device row naming this org's group reached this org's evidence set"
    )
