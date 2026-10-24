"""Migration 0170: install_links, devices.review_state, and two permission codes."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, InstallLink, Organization


async def _org(admin_engine: AsyncEngine) -> Organization:
    slug = f"il-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org


async def test_install_links_is_forced_rls_with_a_tenant_policy(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'install_links'"
                )
            )
        ).one()
        policies = (
            await conn.execute(
                text("SELECT policyname FROM pg_policies WHERE tablename = 'install_links'")
            )
        ).scalars().all()
    assert tuple(row) == (True, True)
    # The tenant policy, and since migration 0231 the restrictive client one
    # (MSP clients spec §2.2) -- in whatever order `pg_policies` lists them.
    assert sorted(policies) == ["client_isolation", "tenant_isolation"]


async def test_one_org_never_reads_another_orgs_link(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    a, b = await _org(admin_engine), await _org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        session.add_all([InstallLink(
            org_id=a.id, client_id=a.id, site_id=a.id
        ), InstallLink(
            org_id=b.id, client_id=b.id, site_id=b.id
        )])
        await session.commit()
    async with org_session(b.id, engine=app_engine, clients=ClientScope.all()) as session:
        seen = (await session.execute(select(InstallLink.org_id))).scalars().all()
    assert seen == [b.id]


async def test_the_link_cannot_be_on_without_a_network(admin_engine: AsyncEngine) -> None:
    org = await _org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        session.add(InstallLink(
            org_id=org.id,
            enabled=True,
            code_hash="a" * 64,
            networks=[],
            client_id=org.id,
            site_id=org.id,
        ))
        with pytest.raises(IntegrityError, match="ck_install_links_on_needs_code_and_network"):
            await session.commit()


async def test_the_hourly_limit_is_bounded(admin_engine: AsyncEngine) -> None:
    org = await _org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        session.add(InstallLink(org_id=org.id, hourly_limit=501, client_id=org.id, site_id=org.id))
        with pytest.raises(IntegrityError, match="ck_install_links_hourly_limit"):
            await session.commit()


async def test_every_existing_and_new_device_is_approved_by_default(
    admin_engine: AsyncEngine,
) -> None:
    org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org.id, hostname="h", os_family="linux", client_id=org.id, site_id=org.id
        )
        session.add(device)
        await session.commit()
        await session.refresh(device)
    assert device.review_state == "approved"
    assert device.link_joined_from is None and device.rejected_at is None


async def test_a_rejection_outcome_is_a_closed_vocabulary(admin_engine: AsyncEngine) -> None:
    org = await _org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        session.add(
            Device(
                org_id=org.id,
                hostname="h",
                review_state="rejected",
                rejection_outcome="meh",
                client_id=org.id,
                site_id=org.id,
            )
        )
        with pytest.raises(IntegrityError, match="ck_devices_rejection_outcome"):
            await session.commit()


async def test_a_manual_removal_is_acknowledged_only_on_a_rejection_that_left_the_agent(
    admin_engine: AsyncEngine,
) -> None:
    """`removal_acknowledged_at` means "a person removed the agent by hand";
    on any other row it would be a claim about something that did not happen."""
    org = await _org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        session.add(
            Device(
                org_id=org.id,
                hostname="h",
                review_state="rejected",
                rejection_outcome="agent_confirmed",
                removal_acknowledged_at=datetime.now(UTC),
                client_id=org.id,
                site_id=org.id,
            )
        )
        with pytest.raises(IntegrityError, match="ck_devices_removal_acknowledged"):
            await session.commit()
    async with AsyncSession(admin_engine) as session:
        session.add(
            Device(
                org_id=org.id,
                hostname="h",
                review_state="rejected",
                rejection_outcome="timeout",
                removal_acknowledged_at=datetime.now(UTC),
                client_id=org.id,
                site_id=org.id,
            )
        )
        await session.commit()


async def test_review_state_is_a_closed_vocabulary(admin_engine: AsyncEngine) -> None:
    org = await _org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        session.add(Device(
            org_id=org.id, hostname="h", review_state="maybe", client_id=org.id, site_id=org.id
        ))
        with pytest.raises(IntegrityError, match="ck_devices_review_state"):
            await session.commit()


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("super-admin", {"enrollment.link.manage", "devices.review"}),
        ("org-admin", {"enrollment.link.manage", "devices.review"}),
        ("technician", {"devices.review"}),
        ("auditor", set()),
        ("read-only", set()),
    ],
)
async def test_the_codes_are_seeded_to_the_tiers_the_spec_names(
    admin_engine: AsyncEngine, role: str, expected: set[str]
) -> None:
    async with admin_engine.connect() as conn:
        held = set(
            (
                await conn.execute(
                    text(
                        "SELECT p.code FROM permissions p "
                        "JOIN role_permissions rp ON rp.permission_id = p.id "
                        "JOIN roles r ON r.id = rp.role_id "
                        "WHERE r.name = :role AND r.org_id IS NULL "
                        "AND p.code IN ('enrollment.link.manage', 'devices.review')"
                    ),
                    {"role": role},
                )
            ).scalars()
        )
    assert held == expected
