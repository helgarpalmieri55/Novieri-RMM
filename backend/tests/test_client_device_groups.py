"""MSP clients spec §1.5: a device group organises ONE client's devices, and an
asset typed by hand belongs to its site's client."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Client, Device, DeviceGroup, InventorySnapshot, Organization, Site
from app.modules.inventory.rules import evaluate_rules
from app.modules.inventory.schema import InventoryDocument
from app.services.device_groups import reevaluate_org_membership
from tests.test_inventory_schema import FULL_DOC

GROUPS = "/api/v1/device-groups"
WINDOWS = {"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]}


async def _customer(admin_engine: AsyncEngine, org_id: UUID) -> tuple[UUID, UUID]:
    """A customer client and its one site."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        client = Client(org_id=org_id, name=f"Cliente {uuid4().hex[:6]}", kind="customer")
        s.add(client)
        await s.flush()
        site = Site(org_id=org_id, client_id=client.id, name="Bogotá")
        s.add(site)
        await s.commit()
        return client.id, site.id


async def _admin(env: Any, org_id: UUID) -> dict[str, str]:
    return await env.headers(org_id, await env.create_user(org_id, "org-admin"))


async def test_a_group_needs_a_client(api_env) -> None:
    org = await api_env.create_org("cdg")
    response = await api_env.client.post(
        GROUPS, headers=await _admin(api_env, org), json={"name": "servers"}
    )
    assert response.status_code == 422, response.text
    assert "client_id" in response.text


async def test_a_group_is_created_in_the_client_it_names(api_env) -> None:
    org = await api_env.create_org("cdg")
    client_id, _ = await _customer(api_env.admin_engine, org)
    response = await api_env.client.post(
        GROUPS,
        headers=await _admin(api_env, org),
        json={"name": "servers", "client_id": str(client_id)},
    )
    assert response.status_code == 201, response.text
    async with AsyncSession(api_env.admin_engine) as s:
        group = await s.get(DeviceGroup, UUID(response.json()["id"]))
        assert group is not None and group.client_id == client_id


async def test_a_client_of_another_organization_is_404(api_env) -> None:
    org = await api_env.create_org("cdg")
    other = await api_env.create_org("cdg-other")
    their_client, _ = await _customer(api_env.admin_engine, other)
    response = await api_env.client.post(
        GROUPS,
        headers=await _admin(api_env, org),
        json={"name": "servers", "client_id": str(their_client)},
    )
    assert response.status_code == 404, response.text


async def test_a_parent_of_another_client_is_refused_with_a_sentence(api_env) -> None:
    org = await api_env.create_org("cdg")
    client_id, _ = await _customer(api_env.admin_engine, org)
    headers = await _admin(api_env, org)
    internal = await api_env.client.post(
        GROUPS, headers=headers, json={"name": "internal-parent", "client_id": str(org)}
    )
    assert internal.status_code == 201, internal.text

    child = await api_env.client.post(
        GROUPS,
        headers=headers,
        json={
            "name": "customer-child",
            "client_id": str(client_id),
            "parent_group_id": internal.json()["id"],
        },
    )
    assert child.status_code == 422, child.text
    assert child.json()["detail"] == "the parent group belongs to another client"

    # The same parent, for a group of ITS client, is fine: the refusal is about
    # the client, not about the parent.
    same = await api_env.client.post(
        GROUPS,
        headers=headers,
        json={
            "name": "internal-child",
            "client_id": str(org),
            "parent_group_id": internal.json()["id"],
        },
    )
    assert same.status_code == 201, same.text


async def test_a_device_of_another_client_is_refused_per_id_not_as_a_500(api_env) -> None:
    org = await api_env.create_org("cdg")
    client_id, site_id = await _customer(api_env.admin_engine, org)
    headers = await _admin(api_env, org)
    group = await api_env.client.post(
        GROUPS, headers=headers, json={"name": "internal", "client_id": str(org)}
    )
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        theirs = Device(org_id=org, client_id=client_id, site_id=site_id, hostname="c1")
        mine = Device(org_id=org, client_id=org, site_id=org, hostname="i1")
        s.add_all([theirs, mine])
        await s.commit()

    response = await api_env.client.post(
        f"{GROUPS}/{group.json()['id']}/members",
        headers=headers,
        json={"device_ids": [str(theirs.id), str(mine.id)]},
    )
    assert response.status_code == 200, response.text
    assert [(r["moved"], r["reason"]) for r in response.json()["results"]] == [
        (False, "belongs to another client"),
        (True, "added"),
    ]


async def _org(admin_engine: AsyncEngine) -> UUID:
    name = f"cdg-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=name, slug=name)
        s.add(org)
        await s.commit()
        return org.id


async def _inventoried(
    admin_engine: AsyncEngine, org_id: UUID, client_id: UUID, site_id: UUID, hostname: str
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org_id,
            client_id=client_id,
            site_id=site_id,
            hostname=hostname,
            os_family="windows",
        )
        s.add(device)
        await s.flush()
        s.add(
            InventorySnapshot(
                org_id=org_id,
                device_id=device.id,
                taken_at=datetime.now(UTC),
                raw={**FULL_DOC, "hostname": hostname},
            )
        )
        await s.commit()
        return device.id


async def test_a_rule_claims_only_devices_of_its_own_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The sweep and the ingest path: a customer's rule that matches an
    internal machine does not claim it. Without the client predicate it would
    win -- and the device's client guard would then refuse the whole write."""
    org = await _org(admin_engine)
    client_id, site_id = await _customer(admin_engine, org)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        theirs = DeviceGroup(org_id=org, client_id=client_id, name="their-windows", rules=WINDOWS)
        s.add(theirs)
        await s.commit()
    internal = await _inventoried(admin_engine, org, org, org, "wks-internal")
    customer = await _inventoried(admin_engine, org, client_id, site_id, "wks-customer")

    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as session:
        result = await reevaluate_org_membership(session, org_id=org)
    assert result.devices_moved == 1

    async with AsyncSession(admin_engine) as s:
        placed = dict(
            (await s.execute(select(Device.id, Device.device_group_id))).tuples().all()
        )
    assert placed[internal] is None
    assert placed[customer] == theirs.id

    # The ingest path agrees.
    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as session:
        device = (await session.execute(select(Device).where(Device.id == internal))).scalar_one()
        claimed = await evaluate_rules(
            session, device, InventoryDocument(**FULL_DOC), org_id=org
        )
    assert claimed is None


async def test_an_asset_needs_a_site_and_takes_its_client(api_env) -> None:
    org = await api_env.create_org("cdg")
    client_id, site_id = await _customer(api_env.admin_engine, org)
    headers = await _admin(api_env, org)

    missing = await api_env.client.post(
        "/api/v1/devices", json={"description": "boxed laptop"}, headers=headers
    )
    assert missing.status_code == 422, missing.text

    created = await api_env.client.post(
        "/api/v1/devices",
        json={"description": "boxed laptop", "site_id": str(site_id)},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    async with AsyncSession(api_env.admin_engine) as s:
        device = await s.get(Device, UUID(created.json()["id"]))
        assert device is not None
        assert (device.client_id, device.site_id) == (client_id, site_id)

    other = await api_env.create_org("cdg-other")
    _, their_site = await _customer(api_env.admin_engine, other)
    refused = await api_env.client.post(
        "/api/v1/devices",
        json={"description": "boxed laptop", "site_id": str(their_site)},
        headers=headers,
    )
    assert refused.status_code == 404, refused.text
