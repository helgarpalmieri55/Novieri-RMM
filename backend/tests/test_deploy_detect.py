"""Tests for server-side detection-rule evaluation
(docs/modules/app-deploy/spec.md).

`device_in_desired_state` decides whether an `app.deploy` job can be skipped
because inventory already shows the device in the state the action wants.
Only `package_installed` is evaluable from inventory; every other rule type
(and the empty rule) defers to the agent by returning False regardless of
action.
"""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Device, InstalledSoftware, Organization
from app.modules.deploy.detect import device_in_desired_state


async def _org_device_with_software(
    admin_engine: AsyncEngine, *, with_acme: bool
) -> tuple[UUID, UUID]:
    slug = f"dd-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(
            org_id=org.id, hostname="wks", os_family="windows", client_id=org.id, site_id=org.id
        )
        session.add(device)
        await session.flush()
        if with_acme:
            session.add(
                InstalledSoftware(
                    org_id=org.id,
                    device_id=device.id,
                    name="Acme",
                    version="1.2",
                )
            )
        await session.commit()
        return org.id, device.id


async def test_package_installed_install_skips_when_present(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _org_device_with_software(admin_engine, with_acme=True)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await device_in_desired_state(
            s,
            org_id=org_id,
            device_id=device_id,
            action="install",
            detection_rule={"type": "package_installed", "name": "Acme"},
        )
    assert result is True


async def test_package_installed_uninstall_dispatches_when_still_present(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _org_device_with_software(admin_engine, with_acme=True)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await device_in_desired_state(
            s,
            org_id=org_id,
            device_id=device_id,
            action="uninstall",
            detection_rule={"type": "package_installed", "name": "Acme"},
        )
    assert result is False


async def test_package_installed_install_dispatches_when_absent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _org_device_with_software(admin_engine, with_acme=False)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await device_in_desired_state(
            s,
            org_id=org_id,
            device_id=device_id,
            action="install",
            detection_rule={"type": "package_installed", "name": "Acme"},
        )
    assert result is False


async def test_package_installed_uninstall_skips_when_absent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _org_device_with_software(admin_engine, with_acme=False)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await device_in_desired_state(
            s,
            org_id=org_id,
            device_id=device_id,
            action="uninstall",
            detection_rule={"type": "package_installed", "name": "Acme"},
        )
    assert result is True


async def test_package_installed_version_mismatch_dispatches(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _org_device_with_software(admin_engine, with_acme=True)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await device_in_desired_state(
            s,
            org_id=org_id,
            device_id=device_id,
            action="install",
            detection_rule={"type": "package_installed", "name": "Acme", "version": "9.9"},
        )
    assert result is False


async def test_file_exists_rule_defers_to_agent(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _org_device_with_software(admin_engine, with_acme=True)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        result = await device_in_desired_state(
            s,
            org_id=org_id,
            device_id=device_id,
            action="install",
            detection_rule={"type": "file_exists", "path": "/x"},
        )
    assert result is False


async def test_empty_rule_always_dispatches(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _org_device_with_software(admin_engine, with_acme=True)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        install_result = await device_in_desired_state(
            s,
            org_id=org_id,
            device_id=device_id,
            action="install",
            detection_rule={},
        )
        uninstall_result = await device_in_desired_state(
            s,
            org_id=org_id,
            device_id=device_id,
            action="uninstall",
            detection_rule={},
        )
    assert install_result is False
    assert uninstall_result is False


async def test_org_bs_software_row_is_hidden_from_org_a_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, device_a = await _org_device_with_software(admin_engine, with_acme=False)
    org_b, device_b = await _org_device_with_software(admin_engine, with_acme=True)
    assert org_a != org_b

    # org A's device has no Acme of its own; org B's Acme row must not leak in
    # even though we deliberately query org A's device_id scoped under org_id=org_a.
    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        result = await device_in_desired_state(
            s,
            org_id=org_a,
            device_id=device_a,
            action="install",
            detection_rule={"type": "package_installed", "name": "Acme"},
        )
    assert result is False

    # Sanity: org B's own device does see it.
    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        result_b = await device_in_desired_state(
            s,
            org_id=org_b,
            device_id=device_b,
            action="install",
            detection_rule={"type": "package_installed", "name": "Acme"},
        )
    assert result_b is True
