"""Tests for the Licenses/SAM vendor service (docs/modules/licenses/spec.md):
CRUD for the shared vendor entity used by product/license/allocation.

Mirrors `tests/test_bcp_plan_service.py`'s fixture style: a throwaway org +
user seeded directly via `admin_engine`, service calls run through an
RLS-constrained `org_session`, and audit rows are read back by
`target_type`/`target_id`.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, Organization, User
from app.modules.licenses.errors import LicenseNotFoundError
from app.modules.licenses.service import (
    create_vendor,
    get_vendor,
    list_vendors,
    update_vendor,
)


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"licvendor-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


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


# ---------------------------------------------------------------------------
# create_vendor
# ---------------------------------------------------------------------------


async def test_create_vendor_succeeds_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        vendor = await create_vendor(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Acme Corp",
            website="https://acme.example",
            contacts=[{"name": "Jane", "email": "jane@acme.example"}],
        )
        await s.commit()
        vendor_id = vendor.id

    assert vendor.name == "Acme Corp"
    assert vendor.created_by == actor_id

    async with org_session(org_id, engine=app_engine) as s:
        fetched = await get_vendor(s, org_id, vendor_id)
        assert fetched is not None
        assert fetched.name == "Acme Corp"

        all_vendors = await list_vendors(s, org_id)
        assert [v.id for v in all_vendors] == [vendor_id]

    actions = await _audit_actions(app_engine, org_id, "vendor", vendor_id)
    assert actions == ["license.vendor_created"]

    async with org_session(org_id, engine=app_engine) as s:
        event = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "license.vendor_created",
                )
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == actor_id
        assert event.meta["name"] == "Acme Corp"


# ---------------------------------------------------------------------------
# update_vendor
# ---------------------------------------------------------------------------


async def test_update_vendor_applies_fields_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        vendor = await create_vendor(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Original",
            website="https://original.example",
            contacts=[],
        )
        await s.commit()
        vendor_id = vendor.id

    async with org_session(org_id, engine=app_engine) as s:
        updated = await update_vendor(
            s,
            org_id=org_id,
            actor_id=actor_id,
            vendor_id=vendor_id,
            website="https://updated.example",
        )
        await s.commit()
        assert updated.website == "https://updated.example"
        assert updated.name == "Original"  # None field left unchanged

    actions = await _audit_actions(app_engine, org_id, "vendor", vendor_id)
    assert actions == ["license.vendor_created", "license.vendor_updated"]


async def test_update_vendor_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(LicenseNotFoundError):
            await update_vendor(
                s, org_id=org_id, actor_id=actor_id, vendor_id=uuid4(), name="nope"
            )


# ---------------------------------------------------------------------------
# get_vendor / list_vendors -- no audit
# ---------------------------------------------------------------------------


async def test_get_and_list_vendor_write_no_audit(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine) as s:
        vendor = await create_vendor(
            s, org_id=org_id, actor_id=actor_id, name="Vendor", website=None, contacts=[]
        )
        await s.commit()
        vendor_id = vendor.id

    before = await _audit_actions(app_engine, org_id, "vendor", vendor_id)

    async with org_session(org_id, engine=app_engine) as s:
        await get_vendor(s, org_id, vendor_id)
        await list_vendors(s, org_id)

    after = await _audit_actions(app_engine, org_id, "vendor", vendor_id)
    assert after == before


async def test_get_vendor_missing_returns_none(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _actor_id = await _org_with_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        result = await get_vendor(s, org_id, uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# tenancy
# ---------------------------------------------------------------------------


async def test_another_orgs_vendor_is_hidden_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    async with org_session(org_a, engine=app_engine) as s:
        vendor = await create_vendor(
            s, org_id=org_a, actor_id=actor_a, name="Vendor A", website=None, contacts=[]
        )
        await s.commit()
        vendor_id = vendor.id

    async with org_session(org_b, engine=app_engine) as s:
        fetched = await get_vendor(s, org_b, vendor_id)
        assert fetched is None

        all_vendors = await list_vendors(s, org_b)
        assert all_vendors == []

        with pytest.raises(LicenseNotFoundError):
            await update_vendor(
                s, org_id=org_b, actor_id=actor_b, vendor_id=vendor_id, name="hijacked"
            )


async def test_the_vendor_org_predicate_holds_where_rls_is_not_doing_the_rejecting(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    The test above drives the same three calls through `org_session` on the
    NOBYPASSRLS `app_user` role, where Postgres has already dropped org A's
    vendor before `Vendor.org_id == org_id` is consulted -- it stays green with
    every one of those predicates deleted. `its_admin` carries BYPASSRLS
    (`tests/conftest.py`), so here the application filter is the only scoping
    there is.

    All three call shapes, because they are three separate predicates: the
    single-row `get_vendor`, the whole-table `list_vendors`, and `_load_vendor`,
    which every write resolves through -- and the write is the one that
    RENAMES a neighbour's row rather than merely reading it.

    FALSIFIED: deleting `Vendor.org_id == org_id` from `get_vendor` answers
    with org B's vendor; from `list_vendors` returns both orgs' rows; from
    `_load_vendor` lets `update_vendor` rename a vendor org A does not own.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    vendors: dict[str, UUID] = {}
    for key, org_id, actor_id in (("a", org_a, actor_a), ("b", org_b, actor_b)):
        vendor = await create_vendor(
            bypassrls_session,
            org_id=org_id,
            actor_id=actor_id,
            name=f"Vendor {key.upper()}",
            website=None,
            contacts=[],
        )
        vendors[key] = vendor.id
    await bypassrls_session.commit()

    assert await get_vendor(bypassrls_session, org_a, vendors["b"]) is None
    assert await get_vendor(bypassrls_session, org_a, vendors["a"]) is not None
    assert [v.id for v in await list_vendors(bypassrls_session, org_a)] == [vendors["a"]]
    assert [v.id for v in await list_vendors(bypassrls_session, org_b)] == [vendors["b"]]

    with pytest.raises(LicenseNotFoundError):
        await update_vendor(
            bypassrls_session,
            org_id=org_a,
            actor_id=actor_a,
            vendor_id=vendors["b"],
            name="hijacked",
        )
    await bypassrls_session.rollback()
    theirs = await get_vendor(bypassrls_session, org_b, vendors["b"])
    assert theirs is not None and theirs.name == "Vendor B", (
        "org A's update reached across the tenant boundary"
    )
