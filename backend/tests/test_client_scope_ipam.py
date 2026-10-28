"""IPAM per client (plan 2026-10-26 Tasks 10 and 11).

Task 10: every IPAM table belongs to a client under the database's own client
isolation (migration 0240). `ipam_vrf` and `ipam_vlan` are C -- the
application writes the client -- and everything below a VRF or a subnet is H,
`ipam_subnet` included: a subnet's VRF is NOT NULL, so the subnet's client is
the VRF's. `ipam_site` is gone: a VLAN, a subnet and a site server name a
`sites` row of their own client, pinned by `(site_id, client_id) REFERENCES
sites (id, client_id)`.

A create takes its client from its parent -- the VRF, or the VLAN's site --
and only a row with no parent names one in the body; a body client that
contradicts the parent's is a 422, as is "Sin asignar". A client the caller
cannot see is a 404, an archived one a 409, and a refused create writes
nothing at all.
"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Client,
    IpamAddress,
    IpamDrift,
    IpamRange,
    IpamScanAuthorization,
    IpamSubnet,
    IpamVlan,
    IpamVrf,
    SiteServer,
)
from app.modules.ipam import registry
from app.modules.ipam.registry import ensure_default_vrf
from tests.client_scope_support import client_with_site, drop_client_keys, everything_on
from tests.conftest import ApiEnv
from tests.ipam_fixtures import seed_agent, seed_device, utcnow

I = "/api/v1/ipam"  # noqa: E741
SITE_SERVERS = "/api/v1/site-servers"

#: Every IPAM table a create may write, site servers, and the audit chain.
WRITTEN = (
    IpamVrf, IpamVlan, IpamSubnet, IpamRange, IpamAddress, IpamScanAuthorization, SiteServer,
    AuditEvent,
)


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _post(env: ApiEnv, path: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
    return await env.client.post(f"{I}{path}", json=body, headers=headers)


async def _created(env: ApiEnv, path: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
    r = await _post(env, path, body, headers)
    assert r.status_code == 201, r.text
    return r.json()


async def _counts(env: ApiEnv, org: UUID) -> dict[str, int]:
    async with AsyncSession(env.admin_engine) as s:
        return {
            model.__tablename__: (
                await s.execute(
                    select(func.count()).select_from(model).where(model.org_id == org)
                )
            ).scalar_one()
            for model in WRITTEN
        }


async def _default_vrf(env: ApiEnv, client: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(IpamVrf.id).where(IpamVrf.client_id == client, IpamVrf.name == "global")
            )
        ).scalar_one()


# --------------------------------------------------------------------------- #
# The brief's tests
# --------------------------------------------------------------------------- #


async def test_two_clients_can_both_have_10_0_0_0_24(api_env: ApiEnv) -> None:
    """The reason IPAM is per client: two networks, one prefix."""
    org = await api_env.create_org("i-overlap")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        r = await api_env.client.post(
            f"{I}/subnets", json={"prefix": "10.0.0.0/24", "client_id": str(client)},
            headers=admin,
        )
        assert r.status_code == 201, r.text
        assert r.json()["client_id"] == str(client)
    async with AsyncSession(api_env.admin_engine) as s:
        vrfs = (await s.execute(select(IpamVrf.client_id, IpamVrf.name)
                                .where(IpamVrf.org_id == org))).all()
    assert {(c, n) for c, n in vrfs if c in (x, y)} == {(x, "global"), (y, "global")}


async def test_a_subnet_takes_its_vrf_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-vrf")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    vrf = (await api_env.client.post(f"{I}/vrfs", json={"name": "LAN", "client_id": str(x)},
                                     headers=admin)).json()["id"]
    ok = await api_env.client.post(f"{I}/subnets", json={"prefix": "10.1.0.0/24", "vrf_id": vrf},
                                   headers=admin)
    assert ok.status_code == 201 and ok.json()["client_id"] == str(x)
    before = await _counts(api_env, org)
    contradiction = await api_env.client.post(
        f"{I}/subnets", json={"prefix": "10.2.0.0/24", "vrf_id": vrf, "client_id": str(y)},
        headers=admin,
    )
    assert contradiction.status_code == 422
    assert await _counts(api_env, org) == before


async def test_a_subnet_s_site_must_be_its_client_s(api_env: ApiEnv) -> None:
    """The API's 422 comes first; under it, the composite key holds even a
    write that skips the application (here: every client, no check)."""
    org = await api_env.create_org("i-site")
    x, _ = await client_with_site(api_env, org)
    _, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_vrf = (await _created(api_env, "/vrfs", {"name": "LAN", "client_id": str(x)}, admin))["id"]
    before = await _counts(api_env, org)
    r = await api_env.client.post(
        f"{I}/subnets", json={"prefix": "10.3.0.0/24", "client_id": str(x), "site_id": str(y_site)},
        headers=admin,
    )
    assert r.status_code == 422, r.text
    assert await _counts(api_env, org) == before
    with pytest.raises(DBAPIError, match="fk_ipam_subnet_site_client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(IpamSubnet(org_id=org, vrf_id=UUID(x_vrf), prefix="10.4.0.0/24", site_id=y_site))
            await s.flush()


async def test_the_old_ipam_site_routes_are_gone(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-gone")
    admin = await _admin(api_env, org)
    assert (await api_env.client.get(f"{I}/sites", headers=admin)).status_code in (404, 405)
    created = await api_env.client.post(f"{I}/sites", json={"name": "Manila"}, headers=admin)
    assert created.status_code in (404, 405)
    async with AsyncSession(api_env.admin_engine) as s:
        assert (await s.execute(text("SELECT to_regclass('public.ipam_site')"))).scalar() is None


async def test_a_technician_of_x_sees_only_x_s_subnets(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-narrow")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    y_subnet = (await api_env.client.post(
        f"{I}/subnets", json={"prefix": "10.9.0.0/24", "client_id": str(y)}, headers=admin
    )).json()["id"]
    x_tech = await everything_on(api_env, org, x)
    assert (await api_env.client.get(f"{I}/subnets/{y_subnet}", headers=x_tech)).status_code == 404


# --------------------------------------------------------------------------- #
# Every create: where its client comes from
# --------------------------------------------------------------------------- #


async def test_a_vrf_names_its_client_and_its_name_is_unique_per_client(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("i-vrfs")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    before = await _counts(api_env, org)
    missing = await _post(api_env, "/vrfs", {"name": "LAN"}, admin)
    assert missing.status_code == 422, missing.text
    assert await _counts(api_env, org) == before
    for client in (x, y):
        vrf = await _created(api_env, "/vrfs", {"name": "LAN", "client_id": str(client)}, admin)
        assert vrf["client_id"] == str(client)
    again = await _post(api_env, "/vrfs", {"name": "LAN", "client_id": str(x)}, admin)
    assert again.status_code == 409, again.text


async def test_a_vlan_takes_its_site_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-vlans")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    sited = await _created(api_env, "/vlans", {"vid": 10, "site_id": str(x_site)}, admin)
    assert (sited["client_id"], sited["site_id"]) == (str(x), str(x_site))
    before = await _counts(api_env, org)
    contradiction = await _post(
        api_env, "/vlans", {"vid": 11, "site_id": str(x_site), "client_id": str(y)}, admin
    )
    assert contradiction.status_code == 422, contradiction.text
    nameless = await _post(api_env, "/vlans", {"vid": 12}, admin)
    assert nameless.status_code == 422, nameless.text
    assert await _counts(api_env, org) == before
    # A site-less VLAN 10 is unique per client: X's and Y's coexist, and X's
    # coexists with X's VLAN 10 at its site.
    for client in (x, y):
        vlan = await _created(api_env, "/vlans", {"vid": 10, "client_id": str(client)}, admin)
        assert (vlan["client_id"], vlan["site_id"]) == (str(client), None)
    twice = await _post(api_env, "/vlans", {"vid": 10, "client_id": str(x)}, admin)
    assert twice.status_code == 409, twice.text


async def test_a_subnet_s_vlan_must_be_its_client_s(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-subvlan")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_vlan = (await _created(api_env, "/vlans", {"vid": 20, "client_id": str(x)}, admin))["id"]
    y_vlan = (await _created(api_env, "/vlans", {"vid": 20, "client_id": str(y)}, admin))["id"]
    before = await _counts(api_env, org)
    wrong = await _post(
        api_env, "/subnets", {"prefix": "10.20.0.0/24", "client_id": str(x), "vlan_id": y_vlan},
        admin,
    )
    assert wrong.status_code == 422, wrong.text
    assert await _counts(api_env, org) == before
    right = await _created(
        api_env, "/subnets", {"prefix": "10.20.0.0/24", "client_id": str(x), "vlan_id": x_vlan},
        admin,
    )
    assert (right["client_id"], right["vlan_id"]) == (str(x), x_vlan)


async def test_an_address_takes_its_vrf_s_client(api_env: ApiEnv) -> None:
    """Without a VRF, the named client's `global` VRF: two clients' 10.0.0.5
    are two rows. With one, the VRF's client, and a body that says otherwise
    is a 422."""
    org = await api_env.create_org("i-addr")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        await _created(
            api_env, "/subnets", {"prefix": "10.0.0.0/24", "client_id": str(client)}, admin
        )
    for client in (x, y):
        row = await _created(
            api_env, "/addresses",
            {"address": "10.0.0.5", "asset_label": "printer", "client_id": str(client)}, admin,
        )
        assert row["client_id"] == str(client)
        assert row["vrf_id"] == str(await _default_vrf(api_env, client))
    before = await _counts(api_env, org)
    x_vrf = str(await _default_vrf(api_env, x))
    contradiction = await _post(
        api_env, "/addresses",
        {"address": "10.0.0.6", "reserve": True, "vrf_id": x_vrf, "client_id": str(y)}, admin,
    )
    assert contradiction.status_code == 422, contradiction.text
    nameless = await _post(api_env, "/addresses", {"address": "10.0.0.7", "reserve": True}, admin)
    assert nameless.status_code == 422, nameless.text
    assert await _counts(api_env, org) == before
    by_vrf = await _created(
        api_env, "/addresses", {"address": "10.0.0.8", "reserve": True, "vrf_id": x_vrf}, admin
    )
    assert by_vrf["client_id"] == str(x)


async def test_a_scan_authorization_takes_its_vrf_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-scope")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    for client in (x, y):
        scope = await _created(
            api_env, "/scan-authorizations", {"prefix": "10.0.0.0/24", "client_id": str(client)},
            admin,
        )
        assert scope["client_id"] == str(client)
        assert scope["vrf_id"] == str(await _default_vrf(api_env, client))
    before = await _counts(api_env, org)
    x_vrf = str(await _default_vrf(api_env, x))
    contradiction = await _post(
        api_env, "/scan-authorizations",
        {"prefix": "10.1.0.0/24", "vrf_id": x_vrf, "client_id": str(y)}, admin,
    )
    assert contradiction.status_code == 422, contradiction.text
    nameless = await _post(api_env, "/scan-authorizations", {"prefix": "10.1.0.0/24"}, admin)
    assert nameless.status_code == 422, nameless.text
    assert await _counts(api_env, org) == before


async def _unassigned(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()


async def test_nothing_of_ipam_is_filed_under_unassigned(api_env: ApiEnv) -> None:
    """C10: "Sin asignar" is the inbox of what has no owner. Every create that
    names a client refuses it, 422, and writes nothing -- not even the
    client's `global` VRF a subnet would have created."""
    org = await api_env.create_org("i-unass")
    admin = await _admin(api_env, org)
    nobody = str(await _unassigned(api_env, org))
    before = await _counts(api_env, org)
    for path, body in (
        ("/vrfs", {"name": "LAN", "client_id": nobody}),
        ("/vlans", {"vid": 30, "client_id": nobody}),
        ("/subnets", {"prefix": "10.30.0.0/24", "client_id": nobody}),
        ("/addresses", {"address": "10.30.0.5", "reserve": True, "client_id": nobody}),
        ("/scan-authorizations", {"prefix": "10.30.0.0/24", "client_id": nobody}),
    ):
        r = await _post(api_env, path, body, admin)
        assert r.status_code == 422, (path, r.text)
        assert "Unassigned" in r.json()["detail"], (path, r.text)
    assert await _counts(api_env, org) == before


async def test_an_archived_client_takes_no_new_ipam_row(api_env: ApiEnv) -> None:
    """The archive is checked on the client whatever its source: named in the
    body, or derived from the VRF the row goes into."""
    org = await api_env.create_org("i-arch")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    vrf = (await _created(api_env, "/vrfs", {"name": "LAN", "client_id": str(x)}, admin))["id"]
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(text("UPDATE clients SET status = 'archived' WHERE id = :c"), {"c": x})
        await s.commit()
    before = await _counts(api_env, org)
    for path, body in (
        ("/vrfs", {"name": "WAN", "client_id": str(x)}),
        ("/vlans", {"vid": 40, "client_id": str(x)}),
        ("/subnets", {"prefix": "10.40.0.0/24", "client_id": str(x)}),
        ("/subnets", {"prefix": "10.41.0.0/24", "vrf_id": vrf}),
        ("/scan-authorizations", {"prefix": "10.41.0.0/24", "vrf_id": vrf}),
    ):
        r = await _post(api_env, path, body, admin)
        assert r.status_code == 409, (path, r.text)
    assert await _counts(api_env, org) == before


async def test_a_client_the_technician_cannot_see_is_a_404(api_env: ApiEnv) -> None:
    """Y is outside X's technician's scope: naming it is the answer a client
    that does not exist gets, never a 403 (spec §3.2's existence oracle)."""
    org = await api_env.create_org("i-hidden")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    y_vrf = (await _created(api_env, "/vrfs", {"name": "LAN", "client_id": str(y)}, admin))["id"]
    x_tech = await everything_on(api_env, org, x)
    before = await _counts(api_env, org)
    for path, body in (
        ("/vrfs", {"name": "WAN", "client_id": str(y)}),
        ("/vlans", {"vid": 50, "client_id": str(y)}),
        ("/vlans", {"vid": 50, "site_id": str(y_site)}),
        ("/subnets", {"prefix": "10.50.0.0/24", "client_id": str(y)}),
        ("/subnets", {"prefix": "10.50.0.0/24", "vrf_id": y_vrf}),
        ("/subnets", {"prefix": "10.51.0.0/24", "client_id": str(x), "site_id": str(y_site)}),
        ("/scan-authorizations", {"prefix": "10.50.0.0/24", "client_id": str(y)}),
        ("/addresses", {"address": "10.50.0.5", "reserve": True, "vrf_id": y_vrf}),
        ("/vrfs", {"name": "WAN", "client_id": str(uuid4())}),
    ):
        r = await _post(api_env, path, body, x_tech)
        assert r.status_code == 404, (path, r.text)
    assert await _counts(api_env, org) == before


# --------------------------------------------------------------------------- #
# Edits
# --------------------------------------------------------------------------- #


async def test_a_subnet_s_new_site_and_vlan_are_its_client_s(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-edit")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = await _created(
        api_env, "/subnets", {"prefix": "10.60.0.0/24", "client_id": str(x)}, admin
    )
    y_vlan = (await _created(api_env, "/vlans", {"vid": 60, "client_id": str(y)}, admin))["id"]
    x_vlan = (await _created(api_env, "/vlans", {"vid": 60, "client_id": str(x)}, admin))["id"]
    put = f"{I}/subnets/{subnet['id']}"
    before = await _counts(api_env, org)
    for body in ({"site_id": str(y_site)}, {"vlan_id": y_vlan}):
        r = await api_env.client.put(put, json=body, headers=admin)
        assert r.status_code == 422, (body, r.text)
    assert await _counts(api_env, org) == before
    async with AsyncSession(api_env.admin_engine) as s:
        row = await s.get(IpamSubnet, UUID(subnet["id"]))
        assert row is not None and (row.site_id, row.vlan_id) == (None, None)
    ok = await api_env.client.put(
        put, json={"site_id": str(x_site), "vlan_id": x_vlan}, headers=admin
    )
    assert ok.status_code == 200, ok.text
    assert (ok.json()["site_id"], ok.json()["vlan_id"], ok.json()["client_id"]) == (
        str(x_site), x_vlan, str(x),
    )


# --------------------------------------------------------------------------- #
# Site servers: `ipam_site_id` became `site_id`
# --------------------------------------------------------------------------- #


async def _server_device(env: ApiEnv, org: UUID, client: UUID, site: UUID) -> UUID:
    device = await seed_device(env, org, client_id=client, site_id=site, review_state="approved")
    await seed_agent(env, org, device)
    return device


async def test_a_site_server_s_site_is_its_device_s_client_s(api_env: ApiEnv) -> None:
    org = await api_env.create_org("i-ss")
    x, x_site = await client_with_site(api_env, org)
    _, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    device = await _server_device(api_env, org, x, x_site)
    body = {"device_id": str(device), "segments": ["10.70.0.0/24"]}
    before = await _counts(api_env, org)
    wrong = await api_env.client.post(
        SITE_SERVERS, json={**body, "site_id": str(y_site)}, headers=admin
    )
    assert wrong.status_code == 422, wrong.text
    assert await _counts(api_env, org) == before
    old = await api_env.client.post(
        SITE_SERVERS, json={**body, "ipam_site_id": str(x_site)}, headers=admin
    )
    assert old.status_code == 422, old.text  # the old field is refused, not ignored
    created = await api_env.client.post(
        SITE_SERVERS, json={**body, "site_id": str(x_site)}, headers=admin
    )
    assert created.status_code == 201, created.text
    server = created.json()
    assert (server["site_id"], server["client_id"]) == (str(x_site), str(x))
    assert "ipam_site_id" not in server
    listed = await api_env.client.get(
        SITE_SERVERS, params={"site_id": str(x_site)}, headers=admin
    )
    assert [s["id"] for s in listed.json()] == [server["id"]]
    elsewhere = await api_env.client.get(
        SITE_SERVERS, params={"site_id": str(y_site)}, headers=admin
    )
    assert elsewhere.json() == []

    patch = f"{SITE_SERVERS}/{server['id']}"
    moved = await api_env.client.patch(patch, json={"site_id": str(y_site)}, headers=admin)
    assert moved.status_code == 422, moved.text
    cleared = await api_env.client.patch(patch, json={"site_id": None}, headers=admin)
    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["site_id"], cleared.json()["client_id"]) == (None, str(x))


# --------------------------------------------------------------------------- #
# The database: composite site keys, client isolation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", ["ipam_vlan", "ipam_subnet", "site_server"])
async def test_the_site_key_pins_a_site_to_the_row_s_client(api_env: ApiEnv, table: str) -> None:
    """`fk_<table>_site_client`: under scope all, with no application in the
    way, a row of X cannot name Y's site -- on INSERT or on UPDATE."""
    org = await api_env.create_org(f"i-fk-{table[:8]}")
    x, x_site = await client_with_site(api_env, org)
    _, y_site = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        vrf = IpamVrf(org_id=org, client_id=x, name="LAN")
        s.add(vrf)
        await s.flush()
        device = await seed_device(api_env, org, client_id=x, site_id=x_site)
        rows = {
            "ipam_vlan": IpamVlan(org_id=org, client_id=x, vid=70, site_id=x_site),
            "ipam_subnet": IpamSubnet(org_id=org, vrf_id=vrf.id, prefix="10.70.0.0/24",
                                      site_id=x_site),
            "site_server": SiteServer(org_id=org, device_id=device, name="ss",
                                      segments=["10.70.0.0/24"], roles=["wake"], site_id=x_site),
        }
        s.add(rows[table])
        await s.commit()
        row_id = rows[table].id
    with pytest.raises(DBAPIError, match=f"fk_{table}_site_client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            await s.execute(
                text(f"UPDATE {table} SET site_id = :y WHERE id = :i"), {"y": y_site, "i": row_id}
            )


async def test_a_technician_of_x_lists_only_x_s_ipam(api_env: ApiEnv) -> None:
    """Every list a narrowed technician reads holds X's rows and none of Y's,
    and Y's rows answer 404 by id."""
    org = await api_env.create_org("i-lists")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    mine: dict[str, set[str]] = {}
    theirs: dict[str, str] = {}
    for client, site, bucket in ((x, x_site, "x"), (y, y_site, "y")):
        vrf = await _created(api_env, "/vrfs", {"name": "LAN", "client_id": str(client)}, admin)
        vlan = await _created(api_env, "/vlans", {"vid": 80, "site_id": str(site)}, admin)
        subnet = await _created(
            api_env, "/subnets",
            {"prefix": "10.80.0.0/24", "vrf_id": vrf["id"], "site_id": str(site)}, admin,
        )
        address = await _created(
            api_env, "/addresses",
            {"address": "10.80.0.5", "reserve": True, "vrf_id": vrf["id"]}, admin,
        )
        scope = await _created(
            api_env, "/scan-authorizations", {"prefix": "10.80.0.0/24", "vrf_id": vrf["id"]}, admin
        )
        ids = {"vrfs": vrf["id"], "vlans": vlan["id"], "subnets": subnet["id"],
               "addresses": address["id"], "scan-authorizations": scope["id"]}
        if bucket == "x":
            mine = {k: {v} for k, v in ids.items()}
        else:
            theirs = ids
    x_tech = await everything_on(api_env, org, x)
    for path in ("vrfs", "vlans", "subnets", "scan-authorizations"):
        listed = await api_env.client.get(f"{I}/{path}", headers=x_tech)
        assert listed.status_code == 200, listed.text
        assert {row["id"] for row in listed.json()} == mine[path], path
    for path in ("subnets", "addresses"):
        r = await api_env.client.get(f"{I}/{path}/{theirs[path]}", headers=x_tech)
        assert r.status_code == 404, (path, r.text)


# --------------------------------------------------------------------------- #
# `ensure_default_vrf`: one `global` VRF per client
# --------------------------------------------------------------------------- #


async def test_the_default_vrf_is_one_per_client(api_env: ApiEnv) -> None:
    """Y's `global` VRF is no answer for X: X gets its own, once, and asking
    again returns the same row."""
    org = await api_env.create_org("i-dvrf")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        decoy = IpamVrf(org_id=org, client_id=y, name="global")
        s.add(decoy)
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        first = await ensure_default_vrf(s, org, x)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        second = await ensure_default_vrf(s, org, x)
    assert first == second != decoy.id
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (await s.execute(select(IpamVrf.client_id).where(IpamVrf.org_id == org))).all()
    assert sorted(str(r.client_id) for r in rows) == sorted([str(x), str(y)])


async def test_two_concurrent_first_writes_make_one_global_vrf_per_client(
    api_env: ApiEnv,
) -> None:
    """Race-safe on the per-client key: two transactions that both miss X's
    VRF make one row (`ON CONFLICT` on `(org_id, client_id, name)`), and Y's
    first write in the same instant makes Y's."""
    org = await api_env.create_org("i-race")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    barrier = asyncio.Barrier(3)

    async def _ensure(client: UUID) -> UUID:
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            await s.execute(text("SELECT 1"))
            await barrier.wait()
            return await ensure_default_vrf(s, org, client)

    a, b, c = await asyncio.gather(_ensure(x), _ensure(x), _ensure(y))
    assert a == b != c
    async with AsyncSession(api_env.admin_engine) as s:
        count = (
            await s.execute(select(func.count()).select_from(IpamVrf).where(IpamVrf.org_id == org))
        ).scalar_one()
    assert count == 2


async def test_the_default_vrf_lookup_keeps_to_its_organisation(api_env: ApiEnv) -> None:
    """Lesson 2: the org predicate next to the client one bites on its own. A
    row of ANOTHER organisation under this org's client -- impossible while
    `fk_ipam_vrf_client_org` holds, so planted with it dropped in a
    transaction that is rolled back -- is not this org's `global` VRF."""
    org = await api_env.create_org("i-dvrf-org")
    other = await api_env.create_org("i-dvrf-other")
    async with AsyncSession(api_env.admin_engine) as s:
        await drop_client_keys(s, "ipam_vrf", "fk_ipam_vrf_client_org")
        planted = IpamVrf(org_id=other, client_id=org, name="global")
        s.add(planted)
        await s.flush()
        mine = await ensure_default_vrf(s, org, org)
        assert mine != planted.id
        owner = (await s.execute(select(IpamVrf.org_id).where(IpamVrf.id == mine))).scalar_one()
        assert owner == org
        await s.rollback()


# --------------------------------------------------------------------------- #
# Every output names its client; every audit event too
# --------------------------------------------------------------------------- #


async def _x_world(env: ApiEnv, tag: str) -> tuple[UUID, UUID, UUID, dict[str, str]]:
    org = await env.create_org(tag)
    x, x_site = await client_with_site(env, org)
    return org, x, x_site, await _admin(env, org)


async def test_every_ipam_output_names_its_client(api_env: ApiEnv) -> None:
    """Creates, reads and edits alike: an edit flushes an UPDATE, after which
    the trigger-written client has to be read back, not lazily loaded."""
    org, x, x_site, admin = await _x_world(api_env, "i-out")
    c = str(x)
    vrf = await _created(api_env, "/vrfs", {"name": "LAN", "client_id": c}, admin)
    vlan = await _created(api_env, "/vlans", {"vid": 90, "site_id": str(x_site)}, admin)
    subnet = await _created(
        api_env, "/subnets", {"prefix": "10.90.0.0/24", "vrf_id": vrf["id"]}, admin
    )
    sid = subnet["id"]
    outputs: list[tuple[str, Any]] = [("vrf", vrf), ("vlan", vlan), ("subnet", subnet)]
    for name, call in (
        ("vrfs", api_env.client.get(f"{I}/vrfs", headers=admin)),
        ("vlans", api_env.client.get(f"{I}/vlans", headers=admin)),
        ("subnets", api_env.client.get(f"{I}/subnets", headers=admin)),
    ):
        outputs.append((name, (await call).json()[0]))
    outputs.append(("get subnet", (await api_env.client.get(f"{I}/subnets/{sid}",
                                                             headers=admin)).json()))
    outputs.append(("put subnet", (await api_env.client.put(
        f"{I}/subnets/{sid}", json={"name": "users"}, headers=admin)).json()))
    outputs.append(("sweep settings", (await api_env.client.put(
        f"{I}/subnets/{sid}/sweep-settings",
        json={"sweep_enabled": True, "sweep_interval_hours": 24}, headers=admin)).json()))
    outputs.append(("range", await _created(
        api_env, f"/subnets/{sid}/ranges",
        {"kind": "reserved", "start_address": "10.90.0.200", "end_address": "10.90.0.210"},
        admin)))
    outputs.append(("ranges", (await api_env.client.get(f"{I}/subnets/{sid}/ranges",
                                                         headers=admin)).json()[0]))
    reserved = await _created(
        api_env, "/addresses", {"address": "10.90.0.5", "reserve": True, "vrf_id": vrf["id"]},
        admin,
    )
    outputs.append(("reserve", reserved))
    outputs.append(("assign", await _created(
        api_env, "/addresses", {"address": "10.90.0.5", "asset_label": "printer",
                                "vrf_id": vrf["id"]}, admin)))
    aid = reserved["id"]
    outputs.append(("get address", (await api_env.client.get(f"{I}/addresses/{aid}",
                                                              headers=admin)).json()))
    outputs.append(("put address", (await api_env.client.put(
        f"{I}/addresses/{aid}", json={"hostname": "prn"}, headers=admin)).json()))
    outputs.append(("release", (await api_env.client.post(
        f"{I}/addresses/{aid}/release", headers=admin)).json()))
    outputs.append(("allocate", await _created(
        api_env, f"/subnets/{sid}/allocate", {"asset_label": "laptop"}, admin)))
    scope = await _created(
        api_env, "/scan-authorizations", {"prefix": "10.90.0.0/24", "vrf_id": vrf["id"]}, admin
    )
    outputs.append(("scope", scope))
    outputs.append(("scopes", (await api_env.client.get(f"{I}/scan-authorizations",
                                                         headers=admin)).json()[0]))
    run = await api_env.client.post(f"{I}/subnets/{sid}/sweep", headers=admin)
    assert run.status_code == 202, run.text
    outputs.append(("sweep", run.json()))
    outputs.append(("sweep runs", (await api_env.client.get(f"{I}/subnets/{sid}/sweep-runs",
                                                             headers=admin)).json()[0]))
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        drift = IpamDrift(org_id=org, vrf_id=UUID(vrf["id"]), subnet_id=UUID(sid),
                          address="10.90.0.77", drift_class="present_undocumented",
                          severity="medium", confidence="high", first_detected_at=utcnow(),
                          last_evaluated_at=utcnow())
        s.add(drift)
        await s.commit()
    outputs.append(("drift", (await api_env.client.get(f"{I}/drift", headers=admin)).json()[0]))
    outputs.append(("acknowledge", (await api_env.client.post(
        f"{I}/drift/{drift.id}/acknowledge", json={"note": "ok"}, headers=admin)).json()))
    outputs.append(("suppress", (await api_env.client.post(
        f"{I}/drift/{drift.id}/suppress",
        json={"until": "2099-01-01T00:00:00Z", "note": "lab"}, headers=admin)).json()))
    device = await _server_device(api_env, org, x, x_site)
    server = await api_env.client.post(
        SITE_SERVERS, json={"device_id": str(device), "segments": ["10.90.0.0/24"]},
        headers=admin,
    )
    outputs.append(("site server", server.json()))
    outputs.append(("patch site server", (await api_env.client.patch(
        f"{SITE_SERVERS}/{server.json()['id']}", json={"site_id": str(x_site)},
        headers=admin)).json()))
    for name, out in outputs:
        assert isinstance(out, dict) and out.get("client_id") == c, (name, out)


#: Every IPAM and site-server audit action Task 10 touches. Each must be
#: written with the client of the row it is about.
AUDITED = {
    "ipam.vrf_created", "ipam.vrf_updated", "ipam.vrf_deleted",
    "ipam.vlan_created", "ipam.vlan_updated", "ipam.vlan_deleted",
    "ipam.subnet_created", "ipam.subnet_updated", "ipam.subnet_deleted",
    "ipam.range_created", "ipam.range_deleted", "ipam.sweep_settings_changed",
    "ipam.address_state_changed", "ipam.address_reserved", "ipam.address_assigned",
    "ipam.address_updated", "ipam.address_released",
    "ipam.scope_authorized", "ipam.scope_revoked",
    "ipam.drift_acknowledged", "ipam.drift_suppressed",
    "site.promoted", "site.updated", "site.demoted",
}


async def test_every_ipam_audit_event_names_its_client(api_env: ApiEnv) -> None:
    """Through the routes where there is one, through the service where
    there is none (VRF and VLAN edits and deletes, a documented address)."""
    org, x, x_site, admin = await _x_world(api_env, "i-audit")
    vrf = await _created(api_env, "/vrfs", {"name": "LAN", "client_id": str(x)}, admin)
    vlan = await _created(api_env, "/vlans", {"vid": 95, "site_id": str(x_site)}, admin)
    subnet = await _created(
        api_env, "/subnets", {"prefix": "10.95.0.0/24", "vrf_id": vrf["id"]}, admin
    )
    sid = subnet["id"]
    assert (await api_env.client.put(f"{I}/subnets/{sid}", json={"name": "users"},
                                     headers=admin)).status_code == 200
    assert (await api_env.client.put(
        f"{I}/subnets/{sid}/sweep-settings",
        json={"sweep_enabled": True, "sweep_interval_hours": 24}, headers=admin,
    )).status_code == 200
    rng = await _created(
        api_env, f"/subnets/{sid}/ranges",
        {"kind": "reserved", "start_address": "10.95.0.200", "end_address": "10.95.0.210"}, admin,
    )
    assert (await api_env.client.delete(f"{I}/subnets/{sid}/ranges/{rng['id']}",
                                        headers=admin)).status_code == 204
    reserved = await _created(
        api_env, "/addresses", {"address": "10.95.0.5", "reserve": True, "vrf_id": vrf["id"]},
        admin,
    )
    await _created(api_env, "/addresses", {"address": "10.95.0.5", "asset_label": "printer",
                                           "vrf_id": vrf["id"]}, admin)
    aid = reserved["id"]
    assert (await api_env.client.put(f"{I}/addresses/{aid}", json={"hostname": "prn"},
                                     headers=admin)).status_code == 200
    assert (await api_env.client.post(f"{I}/addresses/{aid}/release",
                                      headers=admin)).status_code == 200
    scope = await _created(
        api_env, "/scan-authorizations", {"prefix": "10.95.0.0/24", "vrf_id": vrf["id"]}, admin
    )
    assert (await api_env.client.delete(f"{I}/scan-authorizations/{scope['id']}",
                                        headers=admin)).status_code == 204
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        drift = IpamDrift(org_id=org, vrf_id=UUID(vrf["id"]), subnet_id=UUID(sid),
                          address="10.95.0.77", drift_class="present_undocumented",
                          severity="medium", confidence="high", first_detected_at=utcnow(),
                          last_evaluated_at=utcnow())
        s.add(drift)
        await s.commit()
    assert (await api_env.client.post(f"{I}/drift/{drift.id}/acknowledge", json={"note": "ok"},
                                      headers=admin)).status_code == 200
    assert (await api_env.client.post(
        f"{I}/drift/{drift.id}/suppress", json={"until": "2099-01-01T00:00:00Z", "note": "lab"},
        headers=admin,
    )).status_code == 200
    device = await _server_device(api_env, org, x, x_site)
    server = await api_env.client.post(
        SITE_SERVERS, json={"device_id": str(device), "segments": ["10.95.0.0/24"]},
        headers=admin,
    )
    assert server.status_code == 201, server.text
    ss = f"{SITE_SERVERS}/{server.json()['id']}"
    assert (await api_env.client.patch(ss, json={"site_id": str(x_site)},
                                       headers=admin)).status_code == 200
    assert (await api_env.client.delete(ss, headers=admin)).status_code == 204
    assert (await api_env.client.delete(f"{I}/subnets/{sid}?cascade=true",
                                        headers=admin)).status_code == 204

    from app.modules.ipam.addresses import create_address

    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        actor = (await s.execute(text("SELECT id FROM users WHERE org_id = :o LIMIT 1"),
                                 {"o": org})).scalar_one()
        await _created_subnet_for_documenting(s, org, actor, UUID(vrf["id"]))
        await create_address(s, org, actor, address="10.96.0.9", vrf_id=UUID(vrf["id"]))
        await registry.update_vrf(s, org, actor, UUID(vrf["id"]), {"description": "lan"})
        await registry.update_vlan(s, org, actor, UUID(vlan["id"]), {"name": "voice"})
        await registry.delete_vlan(s, org, actor, UUID(vlan["id"]))
        spare = await registry.create_vrf(s, org, actor, client_id=x, name="spare")
        await registry.delete_vrf(s, org, actor, spare.id)

    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.client_id).where(
                    AuditEvent.org_id == org, AuditEvent.action.in_(sorted(AUDITED))
                )
            )
        ).all()
    assert {r.action for r in rows} == AUDITED
    assert {(r.action, r.client_id) for r in rows if r.client_id != x} == set()


async def _created_subnet_for_documenting(
    s: AsyncSession, org: UUID, actor: UUID, vrf: UUID
) -> UUID:
    from app.modules.ipam.subnets import create_subnet

    subnet = await create_subnet(s, org, actor, prefix="10.96.0.0/24", vrf_id=vrf)
    return subnet.id


# --------------------------------------------------------------------------- #
# Edits that have no route, and the inventory hook
# --------------------------------------------------------------------------- #


async def test_a_rename_meets_only_its_own_client_s_names(api_env: ApiEnv) -> None:
    """`update_vrf` and `update_vlan` check a new name or number against the
    ROW'S client, as the per-client keys do: Y's "LAN" and Y's site-less VLAN
    20 are no collision for X, and never leak into X's refusal; X's own are."""
    from app.modules.ipam.errors import IpamConflictError

    org = await api_env.create_org("i-rename")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        rows = [
            IpamVrf(org_id=org, client_id=y, name="LAN"),
            IpamVrf(org_id=org, client_id=x, name="WAN"),
            IpamVrf(org_id=org, client_id=x, name="DMZ"),
            IpamVlan(org_id=org, client_id=y, vid=20),
            IpamVlan(org_id=org, client_id=x, vid=20, site_id=x_site),
            IpamVlan(org_id=org, client_id=x, vid=21),
            IpamVlan(org_id=org, client_id=x, vid=21, site_id=x_site),
        ]
        s.add_all(rows)
        await s.commit()
    _, wan, dmz, _, x_sited_20, _, x_sited_21 = (r.id for r in rows)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        renamed = await registry.update_vrf(s, org, None, wan, {"name": "LAN"})
        moved = await registry.update_vlan(s, org, None, x_sited_20, {"site_id": None})
        assert (renamed.name, moved.site_id) == ("LAN", None)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        with pytest.raises(IpamConflictError, match="already exists in this client"):
            await registry.update_vrf(s, org, None, dmz, {"name": "LAN"})
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        with pytest.raises(IpamConflictError, match="without a site in this client"):
            await registry.update_vlan(s, org, None, x_sited_21, {"site_id": None})


async def test_a_machine_s_own_addresses_go_into_its_client_s_network(api_env: ApiEnv) -> None:
    """The inventory hook files what a device reports about its NICs in its
    OWN client's `global` VRF -- never the internal client's, whatever the
    organisation's id is."""
    from app.models import Device, HardwareComponent, IpamObservation
    from app.modules.ipam.observations import record_inventory_observations

    org = await api_env.create_org("i-nic")
    x, x_site = await client_with_site(api_env, org)
    device_id = await seed_device(api_env, org, client_id=x, site_id=x_site)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(HardwareComponent(
            org_id=org, device_id=device_id, type="nic", identifier="a4:bb:6d:11:22:33",
            attributes={"addresses": [{"address": "10.1.2.42/24", "origin": "dhcp"}]},
        ))
        await s.commit()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        device = (await s.execute(select(Device).where(Device.id == device_id))).scalar_one()
        assert await record_inventory_observations(s, device) == 1
    async with AsyncSession(api_env.admin_engine) as s:
        vrf_client, observed = (
            await s.execute(
                select(IpamVrf.client_id, IpamObservation.client_id)
                .join(IpamVrf, IpamVrf.id == IpamObservation.vrf_id)
                .where(IpamObservation.org_id == org)
            )
        ).one()
    assert (vrf_client, observed) == (x, x)
