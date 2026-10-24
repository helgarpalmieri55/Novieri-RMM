"""MSP clients spec §1.5: a device group organises ONE client's devices, and an
asset typed by hand belongs to its site's client."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    Client,
    Device,
    DeviceGroup,
    InventorySnapshot,
    Organization,
    Site,
    User,
)
from app.modules.inventory.rules import evaluate_rules
from app.modules.inventory.schema import InventoryDocument
from app.services.device_groups import (
    DeviceGroupError,
    create_device_group,
    reevaluate_org_membership,
)
from tests.client_scope_support import EVERY_CLIENT, client_with_site, everything_on
from tests.test_agent_release_api import _StubStore
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


# --------------------------------------------------------------------------- #
# Review I1: the preview runs over the group's client, as the sweep does.
# --------------------------------------------------------------------------- #

PREVIEW = f"{GROUPS}/preview"


async def test_the_preview_counts_only_the_groups_clients_devices(api_env) -> None:
    """The preview claims to run the sweep's matcher, and the sweep only lets a
    group claim its own client's devices: an internal machine matching the rule
    is not in a customer group's count, saved or not."""
    org = await api_env.create_org("cdg")
    client_id, site_id = await _customer(api_env.admin_engine, org)
    await _inventoried(api_env.admin_engine, org, org, org, "wks-internal")
    customer = await _inventoried(api_env.admin_engine, org, client_id, site_id, "wks-customer")
    headers = await _admin(api_env, org)

    unsaved = await api_env.client.post(
        PREVIEW, headers=headers, json={"rules": WINDOWS, "client_id": str(client_id)}
    )
    assert unsaved.status_code == 200, unsaved.text
    assert unsaved.json()["matched"] == 1
    assert [m["device_id"] for m in unsaved.json()["sample"]] == [str(customer)]

    group = await api_env.client.post(
        GROUPS, headers=headers, json={"name": "c", "client_id": str(client_id)}
    )
    saved = await api_env.client.post(
        PREVIEW, headers=headers, json={"rules": WINDOWS, "group_id": group.json()["id"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["matched"] == 1

    # The internal client's preview sees the internal machine only.
    internal = await api_env.client.post(
        PREVIEW, headers=headers, json={"rules": WINDOWS, "client_id": str(org)}
    )
    assert internal.json()["matched"] == 1


async def test_a_preview_names_its_client(api_env) -> None:
    org = await api_env.create_org("cdg")
    headers = await _admin(api_env, org)
    nameless = await api_env.client.post(PREVIEW, headers=headers, json={"rules": WINDOWS})
    assert nameless.status_code == 422, nameless.text

    other = await api_env.create_org("cdg-other")
    their_client, _ = await _customer(api_env.admin_engine, other)
    theirs = await api_env.client.post(
        PREVIEW, headers=headers, json={"rules": WINDOWS, "client_id": str(their_client)}
    )
    assert theirs.status_code == 404, theirs.text


# --------------------------------------------------------------------------- #
# Review I2: a group's name is unique within its client.
# --------------------------------------------------------------------------- #


async def test_two_clients_may_each_have_a_group_of_the_same_name(api_env) -> None:
    org = await api_env.create_org("cdg")
    x, _ = await _customer(api_env.admin_engine, org)
    y, _ = await _customer(api_env.admin_engine, org)
    headers = await _admin(api_env, org)
    for client in (x, y):
        created = await api_env.client.post(
            GROUPS, headers=headers, json={"name": "Servers", "client_id": str(client)}
        )
        assert created.status_code == 201, created.text

    again = await api_env.client.post(
        GROUPS, headers=headers, json={"name": "Servers", "client_id": str(x)}
    )
    assert again.status_code == 409, again.text
    assert "already exists" in again.json()["detail"]


async def test_a_name_another_client_uses_is_free_for_a_technician_who_cannot_see_it(
    api_env,
) -> None:
    """The name check runs under the technician's scope, so it cannot see
    client Y's group. Under a per-org index the INSERT then died on the unique
    violation: a 500 where an every-client admin got a 409 -- an oracle for Y's
    group names. Per client, it is simply free."""
    org = await api_env.create_org("cdg")
    x, _ = await _customer(api_env.admin_engine, org)
    y, _ = await _customer(api_env.admin_engine, org)
    admin = await _admin(api_env, org)
    taken = await api_env.client.post(
        GROUPS, headers=admin, json={"name": "Servers", "client_id": str(y)}
    )
    assert taken.status_code == 201, taken.text

    tech = await everything_on(api_env, org, x)
    created = await api_env.client.post(
        GROUPS, headers=tech, json={"name": "Servers", "client_id": str(x)}
    )
    assert created.status_code == 201, created.text


async def test_a_rename_is_checked_within_the_groups_client(api_env) -> None:
    org = await api_env.create_org("cdg")
    x, _ = await _customer(api_env.admin_engine, org)
    y, _ = await _customer(api_env.admin_engine, org)
    headers = await _admin(api_env, org)
    await api_env.client.post(
        GROUPS, headers=headers, json={"name": "Servers", "client_id": str(y)}
    )
    mine = await api_env.client.post(
        GROUPS, headers=headers, json={"name": "Desktops", "client_id": str(x)}
    )
    other = await api_env.client.post(
        GROUPS, headers=headers, json={"name": "Laptops", "client_id": str(x)}
    )
    renamed = await api_env.client.patch(
        f"{GROUPS}/{mine.json()['id']}", headers=headers, json={"name": "Servers"}
    )
    assert renamed.status_code == 200, renamed.text
    clash = await api_env.client.patch(
        f"{GROUPS}/{other.json()['id']}", headers=headers, json={"name": "Servers"}
    )
    assert clash.status_code == 409, clash.text


# --------------------------------------------------------------------------- #
# Review M4: nothing new lands in an archived client.
# --------------------------------------------------------------------------- #


async def test_a_group_is_not_created_for_an_archived_client(api_env) -> None:
    org = await api_env.create_org("cdg")
    client_id, _ = await client_with_site(api_env, org, client_archived=True)
    response = await api_env.client.post(
        GROUPS,
        headers=await _admin(api_env, org),
        json={"name": "servers", "client_id": str(client_id)},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "that client is archived"


async def test_an_asset_is_not_created_at_a_site_of_an_archived_client(api_env) -> None:
    org = await api_env.create_org("cdg")
    _, site_id = await client_with_site(api_env, org, client_archived=True)
    response = await api_env.client.post(
        "/api/v1/devices",
        headers=await _admin(api_env, org),
        json={"description": "boxed laptop", "site_id": str(site_id)},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "that client is archived"


# --------------------------------------------------------------------------- #
# Review I5: the phase-1 internal-client fallbacks need every client.
# --------------------------------------------------------------------------- #


async def test_promoting_a_discovered_machine_needs_every_client(api_env) -> None:
    """Phase 1 files a promoted machine under the internal client (IPAM has no
    client yet), so a technician limited to one client is refused up front with
    the every-client sentence -- not a 500 from the row RLS would refuse."""
    org = await api_env.create_org("cdg")
    x, _ = await _customer(api_env.admin_engine, org)
    body = {"addresses": ["10.77.0.5"]}
    path = "/api/v1/devices/candidates/promote"
    narrowed = await api_env.client.post(
        path, headers=await everything_on(api_env, org, x), json=body
    )
    assert narrowed.status_code == 403, narrowed.text
    assert narrowed.json()["detail"] == EVERY_CLIENT

    everyone = await api_env.client.post(path, headers=await _admin(api_env, org), json=body)
    assert everyone.status_code == 200, everyone.text


async def test_starting_a_provisioning_run_needs_every_client(api_env) -> None:
    """Imaging is hidden in phase 1 and provisions into the internal client: a
    technician limited to one client is refused before any profile is read."""
    org = await api_env.create_org("cdg")
    x, _ = await _customer(api_env.admin_engine, org)
    path = f"/api/v1/provisioning/profiles/{uuid4()}/run"
    # A stub store: the route builds one before it reads the profile.
    api_env.app.state.object_store = _StubStore()
    narrowed = await api_env.client.post(
        path, headers=await everything_on(api_env, org, x), json={}
    )
    assert narrowed.status_code == 403, narrowed.text
    assert narrowed.json()["detail"] == EVERY_CLIENT

    # Every client: past the gate, to the profile that does not exist.
    everyone = await api_env.client.post(path, headers=await _admin(api_env, org), json={})
    assert everyone.status_code == 422, everyone.text


async def _until_its_insert_waits_on(app_engine: AsyncEngine, blocker: int) -> None:
    """Return once a backend is blocked by `blocker` IN an INSERT into
    `device_groups`: the racing create has passed the name check -- which saw
    no uncommitted row -- and waits on the live-name index for `blocker`'s
    transaction. Polled from the app role, which may read its own backends'
    query text in `pg_stat_activity`. A wait anywhere else fails the test
    rather than passing it through the name check's 409."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    async with app_engine.connect() as conn:
        while True:
            waiting = (
                await conn.execute(
                    text(
                        "SELECT query FROM pg_stat_activity "
                        "WHERE CAST(:b AS integer) = ANY (pg_blocking_pids(pid))"
                    ),
                    {"b": blocker},
                )
            ).scalars().all()
            if waiting:
                assert all(
                    q.lstrip().upper().startswith("INSERT INTO DEVICE_GROUPS") for q in waiting
                ), waiting
                return
            assert loop.time() < deadline, "the racing create never reached the index"
            await asyncio.sleep(0.02)
            await conn.rollback()  # a fresh snapshot of pg_stat_activity


async def test_two_creates_racing_for_one_name_end_in_a_409_not_a_500(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """I2's backstop. Neither create sees the other's uncommitted row, so both
    pass the name check; the second INSERT waits on the live-name index and,
    once the first commits, fails on it. That unique violation is the same 409
    the check gives, not an IntegrityError out of the route.

    DETERMINISTIC: the first transaction stays open until the second is SEEN
    waiting on it in its INSERT (`pg_blocking_pids`), so the 409 always comes
    from the 23505 mapping in `_flush_guarded` -- never from the name check,
    which is what a slow second task used to reach after the first had
    committed."""
    org = await _org(admin_engine)
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        actor = User(org_id=org, external_subject=f"sub-{uuid4().hex[:8]}", email="r@test.local")
        s.add(actor)
        await s.commit()
        actor_id = actor.id

    async def create() -> DeviceGroup:
        async with org_session(org, engine=app_engine, clients=ClientScope.all()) as session:
            return await create_device_group(
                session, org_id=org, client_id=org, actor_id=actor_id, name="Servers"
            )

    async with org_session(org, engine=app_engine, clients=ClientScope.all()) as session:
        await create_device_group(
            session, org_id=org, client_id=org, actor_id=actor_id, name="Servers"
        )
        first = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        racing = asyncio.create_task(create())
        await _until_its_insert_waits_on(app_engine, first)
        assert not racing.done()
    with pytest.raises(DeviceGroupError) as refused:
        await racing
    assert refused.value.status_code == 409
