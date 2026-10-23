"""Tests for the Licenses/SAM reconciliation service
(docs/modules/licenses/spec.md §4, review queue): the operator-facing surface
for installed-software that matches no product's rules -- map it onto a
product or ignore it so it stops reappearing.

Mirrors `tests/test_licenses_matching.py`'s fixture style (throwaway org/user
seeded via `admin_engine`, service calls through the RLS-constrained
`org_session`) and its `_seed_device`/`_seed_installed`/`_audit_actions`
helpers.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Device, InstalledSoftware, Organization, User
from app.modules.licenses.errors import LicenseNotFoundError
from app.modules.licenses.service import (
    _load_product,
    create_product,
    ignore_software,
    map_software,
    review_queue,
)

# ---------------------------------------------------------------------------
# seeding helpers (mirrors test_licenses_matching.py)
# ---------------------------------------------------------------------------


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"licrecon-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _seed_device(admin_engine: AsyncEngine, org_id: UUID, hostname: str) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(org_id=org_id, hostname=hostname, os_family="linux")
        s.add(device)
        await s.commit()
        return device.id


async def _seed_installed(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    name: str,
    publisher: str = "",
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        row = InstalledSoftware(
            org_id=org_id, device_id=device_id, name=name, publisher=publisher
        )
        s.add(row)
        await s.commit()
        return row.id


async def _audit_actions(
    app_engine: AsyncEngine, org_id: UUID, target_type: str, target_id: UUID | str
) -> list[str]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
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


async def _seed_product(
    app_engine: AsyncEngine, org_id: UUID, actor_id: UUID, match_rules: list[dict]
):
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        product = await create_product(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Acme Suite",
            publisher="Acme Inc",
            match_rules=match_rules,
        )
        await s.commit()
        return product


async def _seed_scenario(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> tuple[UUID, UUID, "object"]:
    """Seed org/user, a product matching 'Acme Pro', and installs:
    - Acme Pro on device1 (matched)
    - Widget on device2, device3 (unmatched, 2 devices)
    - Gadget on device4 (unmatched, 1 device)
    - OsThing on device5 (unmatched until ignored by the caller)
    """
    org_id, actor_id = await _org_with_user(admin_engine)

    device1 = await _seed_device(admin_engine, org_id, "device-1")
    device2 = await _seed_device(admin_engine, org_id, "device-2")
    device3 = await _seed_device(admin_engine, org_id, "device-3")
    device4 = await _seed_device(admin_engine, org_id, "device-4")
    device5 = await _seed_device(admin_engine, org_id, "device-5")

    await _seed_installed(admin_engine, org_id, device1, "Acme Pro")
    await _seed_installed(admin_engine, org_id, device2, "Widget")
    await _seed_installed(admin_engine, org_id, device3, "Widget")
    await _seed_installed(admin_engine, org_id, device4, "Gadget")
    await _seed_installed(admin_engine, org_id, device5, "OsThing")

    match_rules = [{"type": "exact", "pattern": "Acme Pro", "publisher": None}]
    product = await _seed_product(app_engine, org_id, actor_id, match_rules)

    return org_id, actor_id, product


# ---------------------------------------------------------------------------
# review_queue
# ---------------------------------------------------------------------------


async def test_review_queue_excludes_matched_and_ignored_ordered_by_device_count(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await ignore_software(s, org_id=org_id, actor_id=actor_id, name="OsThing")
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        queue = await review_queue(s, org_id)

    assert [(row["name"], row["device_count"]) for row in queue] == [
        ("Widget", 2),
        ("Gadget", 1),
    ]
    names = {row["name"] for row in queue}
    assert "Acme Pro" not in names
    assert "OsThing" not in names


async def test_review_queue_writes_no_audit(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events_before = (
            await s.execute(select(AuditEvent)
                .where(AuditEvent.org_id == org_id))
        ).scalars().all()
        count_before = len(events_before)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await review_queue(s, org_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events_after = (
            await s.execute(select(AuditEvent)
                .where(AuditEvent.org_id == org_id))
        ).scalars().all()
        count_after = len(events_after)

    assert count_after == count_before


async def test_review_queue_foreign_org_unmatched_install_never_appears(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)
    other_org_id, _other_actor = await _org_with_user(admin_engine)
    foreign_device = await _seed_device(admin_engine, other_org_id, "foreign-device")
    await _seed_installed(admin_engine, other_org_id, foreign_device, "ForeignApp")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        queue = await review_queue(s, org_id)

    names = {row["name"] for row in queue}
    assert "ForeignApp" not in names


async def test_review_queue_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Run on an RLS-bypassing admin session with a foreign-org unmatched
    install present, so the explicit org_id predicate in `review_queue`'s
    installed-software query is the SOLE guard, not RLS.
    """
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)
    other_org_id, _other_actor = await _org_with_user(admin_engine)
    foreign_device = await _seed_device(admin_engine, other_org_id, "foreign-device")
    await _seed_installed(admin_engine, other_org_id, foreign_device, "ForeignApp")

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        queue = await review_queue(s, org_id)

    names = {row["name"] for row in queue}
    assert "ForeignApp" not in names


# ---------------------------------------------------------------------------
# map_software
# ---------------------------------------------------------------------------


async def test_map_software_appends_exact_rule_and_leaves_queue(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)
    product_id = product.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await map_software(
            s, org_id=org_id, actor_id=actor_id, name="Widget", product_id=product_id
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        refetched = await _load_product(s, org_id, product_id)
        assert {"type": "exact", "pattern": "Widget", "publisher": None} in refetched.match_rules

        queue = await review_queue(s, org_id)

    names = {row["name"] for row in queue}
    assert "Widget" not in names
    assert "Gadget" in names


async def test_map_software_is_dedupe_safe(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)
    product_id = product.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await map_software(
            s, org_id=org_id, actor_id=actor_id, name="Widget", product_id=product_id
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await map_software(
            s, org_id=org_id, actor_id=actor_id, name="Widget", product_id=product_id
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        refetched = await _load_product(s, org_id, product_id)
        widget_rules = [
            r
            for r in refetched.match_rules
            if r == {"type": "exact", "pattern": "Widget", "publisher": None}
        ]
        assert len(widget_rules) == 1


async def test_map_software_cross_org_product_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)
    other_org_id, other_actor_id = await _org_with_user(admin_engine)

    async with org_session(other_org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseNotFoundError):
            await map_software(
                s,
                org_id=other_org_id,
                actor_id=other_actor_id,
                name="Widget",
                product_id=product.id,
            )


async def test_map_software_missing_product_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseNotFoundError):
            await map_software(
                s, org_id=org_id, actor_id=actor_id, name="Widget", product_id=uuid4()
            )


async def test_map_software_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)
    product_id = product.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await map_software(
            s, org_id=org_id, actor_id=actor_id, name="Widget", product_id=product_id
        )
        await s.commit()

    actions = await _audit_actions(app_engine, org_id, "product", product_id)
    assert "license.software_mapped" in actions


# ---------------------------------------------------------------------------
# ignore_software
# ---------------------------------------------------------------------------


async def test_ignore_software_removes_from_queue(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await ignore_software(s, org_id=org_id, actor_id=actor_id, name="Gadget")
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        queue = await review_queue(s, org_id)

    names = {row["name"] for row in queue}
    assert "Gadget" not in names
    assert "Widget" in names


async def test_ignore_software_is_idempotent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        first = await ignore_software(s, org_id=org_id, actor_id=actor_id, name="Gadget")
        await s.commit()
        first_id = first.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        second = await ignore_software(s, org_id=org_id, actor_id=actor_id, name="Gadget")
        await s.commit()
        assert second.id == first_id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        from app.models import SoftwareIgnore

        rows = (
            await s.execute(
                select(SoftwareIgnore).where(
                    SoftwareIgnore.org_id == org_id, SoftwareIgnore.name == "Gadget"
                )
            )
        ).scalars().all()
        assert len(rows) == 1


async def test_ignore_software_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id, product = await _seed_scenario(admin_engine, app_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        ignored = await ignore_software(s, org_id=org_id, actor_id=actor_id, name="Gadget")
        await s.commit()
        ignored_id = ignored.id

    actions = await _audit_actions(app_engine, org_id, "software_ignore", ignored_id)
    assert actions == ["license.software_ignored"]
