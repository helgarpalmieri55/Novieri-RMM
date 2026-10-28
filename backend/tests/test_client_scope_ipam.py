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

Task 11: what a sweep finds stays inside the subnet's client. A sweep is run
only by a machine of that client (`select_sweeper`, `set_sweep_settings`);
candidates and the "already known" lookups are per client, so the same
address in X and Y is two candidates; a promotion names its client and the
machine takes the client and the site of its subnet -- a subnet without a site
is refused (decision C7); an import names its client; and every audit event
and domain event of these paths carries the client.
"""

import asyncio
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Client,
    Device,
    DocRecord,
    DomainEvent,
    IpamAddress,
    IpamDrift,
    IpamObservation,
    IpamRange,
    IpamScanAuthorization,
    IpamSubnet,
    IpamVlan,
    IpamVrf,
    Site,
    SiteServer,
)
from app.modules.ipam import registry
from app.modules.ipam.candidates import SweepSighting, auto_create_from_sweep, list_candidates
from app.modules.ipam.registry import ensure_default_vrf
from app.modules.ipam.sweep import select_sweeper
from tests.client_scope_support import client_with_site, drop_client_keys, everything_on
from tests.conftest import ApiEnv
from tests.ipam_fixtures import (
    complete_sweep,
    dispatch_sweep,
    seed_agent,
    seed_device,
    sweep_document,
    sweep_host,
    upsert_observation_row,
    utcnow,
)
from tests.site_server_fixtures import seed_site_server

I = "/api/v1/ipam"  # noqa: E741
SITE_SERVERS = "/api/v1/site-servers"

#: Every IPAM table a create may write, site servers, the machines a
#: promotion or a sweep files (Task 11), and the audit chain.
WRITTEN = (
    IpamVrf, IpamVlan, IpamSubnet, IpamRange, IpamAddress, IpamScanAuthorization, SiteServer,
    Device, AuditEvent,
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
    from app.models import Device, HardwareComponent
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


# --------------------------------------------------------------------------- #
# Review fix round (task-10-review.md M1-M3)
# --------------------------------------------------------------------------- #


async def test_a_technician_of_x_creates_ipam_in_x(api_env: ApiEnv) -> None:
    """The positive twin of the 404 test above (M2): a technician narrowed to
    X files every kind of IPAM row in X -- each a 201, each row X's. A
    refusal of narrowed sessions across the board would pass every refusal
    test and fail this one."""
    org = await api_env.create_org("i-narrow-ok")
    x, x_site = await client_with_site(api_env, org)
    await client_with_site(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    c = str(x)
    vrf = await _created(api_env, "/vrfs", {"name": "LAN", "client_id": c}, x_tech)
    made: list[tuple[str, Any]] = [
        ("vrf", vrf),
        ("vlan", await _created(api_env, "/vlans", {"vid": 30, "client_id": c}, x_tech)),
        ("sited vlan", await _created(
            api_env, "/vlans", {"vid": 31, "site_id": str(x_site)}, x_tech)),
        ("subnet", await _created(
            api_env, "/subnets", {"prefix": "10.30.0.0/24", "vrf_id": vrf["id"]}, x_tech)),
        ("global subnet", await _created(
            api_env, "/subnets", {"prefix": "10.31.0.0/24", "client_id": c}, x_tech)),
        ("address", await _created(
            api_env, "/addresses",
            {"address": "10.30.0.5", "reserve": True, "vrf_id": vrf["id"]}, x_tech)),
        ("scope", await _created(
            api_env, "/scan-authorizations",
            {"prefix": "10.30.0.0/24", "vrf_id": vrf["id"]}, x_tech)),
    ]
    for name, out in made:
        assert out["client_id"] == c, (name, out)
    models = {
        "vrf": IpamVrf, "vlan": IpamVlan, "sited vlan": IpamVlan, "subnet": IpamSubnet,
        "global subnet": IpamSubnet, "address": IpamAddress, "scope": IpamScanAuthorization,
    }
    async with AsyncSession(api_env.admin_engine) as s:
        for name, out in made:
            model = models[name]
            stored = (
                await s.execute(select(model.client_id).where(model.id == UUID(out["id"])))
            ).scalar_one()
            assert stored == x, name


async def test_the_database_keeps_a_subnet_s_vlan_in_its_client(api_env: ApiEnv) -> None:
    """`fk_ipam_subnet_vlan_client` (M3): under scope all, with no application
    in the way, a subnet of X cannot name Y's VLAN -- on INSERT or on UPDATE
    -- while X's own VLAN is accepted (the positive control)."""
    org = await api_env.create_org("i-fk-vlan")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        vrf = IpamVrf(org_id=org, client_id=x, name="LAN")
        x_vlan = IpamVlan(org_id=org, client_id=x, vid=40)
        y_vlan = IpamVlan(org_id=org, client_id=y, vid=40)
        s.add_all([vrf, x_vlan, y_vlan])
        await s.flush()
        subnet = IpamSubnet(org_id=org, vrf_id=vrf.id, prefix="10.40.0.0/24", vlan_id=x_vlan.id)
        s.add(subnet)
        await s.commit()
    with pytest.raises(DBAPIError, match="fk_ipam_subnet_vlan_client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(IpamSubnet(org_id=org, vrf_id=vrf.id, prefix="10.41.0.0/24",
                             vlan_id=y_vlan.id))
            await s.flush()
    with pytest.raises(DBAPIError, match="fk_ipam_subnet_vlan_client"):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            await s.execute(
                text("UPDATE ipam_subnet SET vlan_id = :v WHERE id = :i"),
                {"v": y_vlan.id, "i": subnet.id},
            )
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        s.add(IpamSubnet(org_id=org, vrf_id=vrf.id, prefix="10.42.0.0/24", vlan_id=x_vlan.id))
        await s.flush()


def _migration_0240() -> ModuleType:
    """0240 itself, so the fold under test is the migration's own text."""
    path = Path(__file__).resolve().parent.parent / "migrations" / "versions"
    spec = importlib.util.spec_from_file_location(
        "migration_0240", path / "0240_client_scope_ipam.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_migration_folds_colliding_vlans_into_one(api_env: ApiEnv) -> None:
    """0240's VLAN fold (M1), run as the migration runs it: its own statements
    (`VLAN_FOLD`, imported, not copied), in a transaction that is rolled back.

    The fold runs before the client columns exist, so it runs here on
    temporary copies of `ipam_vlan` and `ipam_subnet` (`pg_temp` comes first
    on the search path, so the unqualified names in the statements resolve to
    them): the head schema's client keys are not what it ran against, and
    the rest of this database's VLANs are not its business. The copies have
    the real columns (`LIKE ... INCLUDING DEFAULTS`).

    Per organisation and number, the survivor is the site-less VLAN, else the
    oldest, else (same age) the lowest id; subnets naming another are
    repointed to it; the others are deleted. A number that does not repeat,
    and another organisation's VLAN of the same number, are untouched."""
    fold = _migration_0240().VLAN_FOLD
    a, b = uuid4(), uuid4()
    t0 = utcnow()

    def at(minutes: int) -> Any:
        return t0 + timedelta(minutes=minutes)

    manila, cebu, davao = uuid4(), uuid4(), uuid4()
    # name: (org, vid, site, created_at, id) -- two ids fixed, to decide a tie.
    low, high = UUID(int=1), UUID(int=2)
    vlans = {
        "10-manila": (a, 10, manila, at(0), uuid4()),
        "10-cebu": (a, 10, cebu, at(1), uuid4()),
        "10-global": (a, 10, None, at(2), uuid4()),  # newest, but site-less: survives
        "20-cebu": (a, 20, cebu, at(5), uuid4()),  # inserted first, but newer
        "20-manila": (a, 20, manila, at(4), uuid4()),  # oldest: survives
        "30-cebu": (a, 30, cebu, at(0), uuid4()),  # alone: untouched
        "40-davao-high": (a, 40, davao, at(0), high),
        "40-cebu-low": (a, 40, cebu, at(0), low),  # same age, lower id: survives
        "b-10-manila": (b, 10, manila, at(9), uuid4()),  # another org: untouched
    }
    subnets = {  # subnet -> the VLAN it names before the fold
        "s-10-cebu": "10-cebu",
        "s-10-manila": "10-manila",
        "s-10-global": "10-global",
        "s-20-cebu": "20-cebu",
        "s-30": "30-cebu",
        "s-40": "40-davao-high",
        "s-b": "b-10-manila",
        "s-none": None,
    }
    async with AsyncSession(api_env.admin_engine) as s:
        for table in ("ipam_vlan", "ipam_subnet"):
            await s.execute(text(
                f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            ))
        vlan_id = {}
        for name, (org, vid, site, created, row_id) in vlans.items():
            vlan_id[name] = row_id
            await s.execute(
                text(
                    "INSERT INTO ipam_vlan (id, org_id, client_id, site_id, vid, name, "
                    "description, created_at, updated_at) "
                    "VALUES (:i, :o, :o, :s, :v, :n, '', :c, :c)"
                ),
                {"i": row_id, "o": org, "s": site, "v": vid, "n": name, "c": created},
            )
        subnet_id = {}
        for n, (name, vlan) in enumerate(subnets.items()):
            subnet_id[name] = uuid4()
            org = b if name == "s-b" else a
            await s.execute(
                text(
                    "INSERT INTO ipam_subnet (id, org_id, client_id, vrf_id, prefix, vlan_id, "
                    "created_at, updated_at) VALUES (:i, :o, :o, :o, :p, :v, :c, :c)"
                ),
                {"i": subnet_id[name], "o": org, "p": f"10.{n}.0.0/24",
                 "v": vlan_id[vlan] if vlan else None, "c": t0},
            )
        assert len(fold) == 2
        for statement in fold:
            await s.execute(text(statement))
        left = set((await s.execute(text("SELECT name FROM ipam_vlan"))).scalars())
        named = {
            r.id: r.vlan_id
            for r in (await s.execute(text("SELECT id, vlan_id FROM ipam_subnet"))).all()
        }
        await s.rollback()

    assert left == {"10-global", "20-manila", "30-cebu", "40-cebu-low", "b-10-manila"}
    by_name = {name: named[sid] for name, sid in subnet_id.items()}
    assert by_name == {
        "s-10-cebu": vlan_id["10-global"],
        "s-10-manila": vlan_id["10-global"],
        "s-10-global": vlan_id["10-global"],
        "s-20-cebu": vlan_id["20-manila"],
        "s-30": vlan_id["30-cebu"],
        "s-40": vlan_id["40-cebu-low"],
        "s-b": vlan_id["b-10-manila"],
        "s-none": None,
    }


# =========================================================================== #
# Task 11: sweeps, candidates and promotion stay inside the subnet's client
# =========================================================================== #

PROMOTE = "/api/v1/devices/candidates/promote"
#: Decision C7, the sentence a candidate on a subnet without a site is refused with.
NO_SITE = "assign the subnet to a site before adding its machines"
OTHER_CLIENT = "that machine belongs to another client"


async def _subnet(
    env: ApiEnv, headers: dict[str, str], prefix: str, client: UUID, site: UUID | None = None
) -> dict[str, Any]:
    """A subnet of `client` in its `global` VRF, at `site` when one is named."""
    body: dict[str, Any] = {"prefix": prefix, "client_id": str(client)}
    if site is not None:
        body["site_id"] = str(site)
    return await _created(env, "/subnets", body, headers)


async def _device_seen_in(
    env: ApiEnv, org: UUID, client: UUID, site: UUID, vrf: UUID, address: str,
    *, seen: datetime | None = None,
) -> UUID:
    """An approved machine of `client` with an online agent, whose own
    inventory reports `address` in `vrf`: what makes it a sweeper there."""
    device = await seed_device(env, org, client_id=client, site_id=site, review_state="approved")
    await seed_agent(env, org, device, status="online", last_seen_at=seen)
    await upsert_observation_row(
        env, org, vrf_id=vrf, address=address, source="inventory", method="nic",
        confidence="high", device_id=device,
    )
    return device


async def _swept(
    env: ApiEnv, org: UUID, subnet: dict[str, Any], address: str, mac: str | None = None
) -> None:
    """A sweep's sighting of `address` in `subnet` -- no device id, ever."""
    await upsert_observation_row(
        env, org, vrf_id=UUID(subnet["vrf_id"]), address=address, subnet_id=UUID(subnet["id"]),
        source="sweep", method="arp" if mac else "icmp",
        confidence="medium" if mac else "low", mac=mac,
    )


async def _sweeper_for(env: ApiEnv, org: UUID, subnet_id: str) -> UUID | None:
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        subnet = await s.get(IpamSubnet, UUID(subnet_id))
        assert subnet is not None
        return await select_sweeper(s, org, subnet)


# --------------------------------------------------------------------------- #
# Who sweeps: a machine of the subnet's client, and only one
# --------------------------------------------------------------------------- #


async def test_a_sweep_never_borrows_another_client_s_machine(api_env: ApiEnv) -> None:
    """research-3 §D: `select_sweeper` chose any approved device of the ORG
    whose inventory showed an address inside the prefix. Y's machine sits on
    Y's 10.0.0.0/24, which is not X's 10.0.0.0/24: the signed target list,
    the wire the probe leaves from and the results are all X's."""
    org = await api_env.create_org("i-sweeper")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnets = {client: await _subnet(api_env, admin, "10.0.0.0/24", client) for client in (x, y)}
    theirs = await _device_seen_in(
        api_env, org, y, y_site, UUID(subnets[y]["vrf_id"]), "10.0.0.7"
    )
    assert await _sweeper_for(api_env, org, subnets[x]["id"]) is None  # `no_agent`, never Y's
    # The positive twin: X's own machine on X's wire is chosen for X, and
    # Y's still sweeps Y.
    mine = await _device_seen_in(api_env, org, x, x_site, UUID(subnets[x]["vrf_id"]), "10.0.0.8")
    assert await _sweeper_for(api_env, org, subnets[x]["id"]) == mine
    assert await _sweeper_for(api_env, org, subnets[y]["id"]) == theirs


async def test_a_designated_machine_of_another_client_is_ignored(api_env: ApiEnv) -> None:
    """`_designated`: a designation naming Y's machine -- written before this
    check existed, or straight into the table -- is ignored like one whose
    agent cannot sweep, and auto-selection runs within X. X's own designated
    machine is still preferred over the auto-selected one."""
    org = await api_env.create_org("i-desig")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = await _subnet(api_env, admin, "10.1.0.0/24", x)
    auto = await _device_seen_in(api_env, org, x, x_site, UUID(subnet["vrf_id"]), "10.1.0.9")
    foreign = await seed_device(api_env, org, client_id=y, site_id=y_site)
    await seed_agent(api_env, org, foreign, status="online")
    designated = await seed_device(api_env, org, client_id=x, site_id=x_site)
    await seed_agent(api_env, org, designated, status="online")

    for named, expected in ((foreign, auto), (designated, designated)):
        async with AsyncSession(api_env.admin_engine) as s:
            await s.execute(
                text("UPDATE ipam_subnet SET sweep_device_id = :d WHERE id = :i"),
                {"d": named, "i": subnet["id"]},
            )
            await s.commit()
        assert await _sweeper_for(api_env, org, subnet["id"]) == expected, named


async def test_a_site_server_row_of_another_client_earns_no_preference(api_env: ApiEnv) -> None:
    """The preference term's own `s.client_id` predicate. A site server is H
    from its device, so the database already keeps its client the device's;
    the predicate is shown to hold on its own by planting the one row that
    trigger makes impossible -- X's machine's site-server row carrying Y's
    client -- in a transaction that is rolled back (lesson 2).

    Two X machines answer; `recent` was seen last, so it wins on recency
    unless `server`'s site-server row earns the preference."""
    org = await api_env.create_org("i-ss-pref")
    x, x_site = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = await _subnet(api_env, admin, "10.2.0.0/24", x)
    vrf = UUID(subnet["vrf_id"])
    server = await _device_seen_in(
        api_env, org, x, x_site, vrf, "10.2.0.8", seen=utcnow() - timedelta(hours=2)
    )
    recent = await _device_seen_in(api_env, org, x, x_site, vrf, "10.2.0.9", seen=utcnow())
    row = await seed_site_server(api_env, org, device_id=server, segments=["10.2.0.0/16"])
    assert await _sweeper_for(api_env, org, subnet["id"]) == server  # the positive control

    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(text("ALTER TABLE site_server DISABLE TRIGGER its_client_from_parent"))
        await s.execute(
            text("UPDATE site_server SET client_id = :y WHERE id = :i"), {"y": y, "i": row}
        )
        x_subnet = await s.get(IpamSubnet, UUID(subnet["id"]))
        assert x_subnet is not None
        chosen = await select_sweeper(s, org, x_subnet)
        await s.rollback()
    assert chosen == recent


async def test_sweep_settings_name_a_machine_of_the_subnet_s_client(api_env: ApiEnv) -> None:
    """`set_sweep_settings`: the designated sweeper is a machine of the
    subnet's client -- 422 for Y's, and nothing changes; X's is accepted."""
    org = await api_env.create_org("i-settings")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = (await _subnet(api_env, admin, "10.5.0.0/24", x))["id"]
    foreign = await seed_device(api_env, org, client_id=y, site_id=y_site)
    mine = await seed_device(api_env, org, client_id=x, site_id=x_site)
    put = f"{I}/subnets/{subnet}/sweep-settings"
    before = await _counts(api_env, org)
    r = await api_env.client.put(
        put, json={"sweep_enabled": True, "sweep_interval_hours": 24,
                   "sweep_device_id": str(foreign)}, headers=admin,
    )
    assert r.status_code == 422, r.text
    assert OTHER_CLIENT in r.json()["detail"]
    assert await _counts(api_env, org) == before
    async with AsyncSession(api_env.admin_engine) as s:
        row = await s.get(IpamSubnet, UUID(subnet))
        assert row is not None and (row.sweep_enabled, row.sweep_device_id) == (False, None)
    ok = await api_env.client.put(
        put, json={"sweep_enabled": True, "sweep_interval_hours": 24,
                   "sweep_device_id": str(mine)}, headers=admin,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["sweep_device_id"] == str(mine)


# --------------------------------------------------------------------------- #
# Candidates: per client
# --------------------------------------------------------------------------- #


async def _candidates(env: ApiEnv, org: UUID) -> set[tuple[str, UUID]]:
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        return {(c.address, c.client_id) for c in (await list_candidates(s, org)).items}


async def test_the_same_address_in_two_clients_is_two_candidates(api_env: ApiEnv) -> None:
    """Grouped by (client, address): X's 10.0.0.9 and Y's 10.0.0.9 are two
    machines, each a candidate of its own client with its own subnet -- and
    a technician of X lists X's, naming its client."""
    org = await api_env.create_org("i-cand2")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnets = {
        x: await _subnet(api_env, admin, "10.0.0.0/24", x, x_site),
        y: await _subnet(api_env, admin, "10.0.0.0/24", y, y_site),
    }
    for client in (x, y):
        await _swept(api_env, org, subnets[client], "10.0.0.9")
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        found = {(c.client_id, c.subnet_id) for c in (await list_candidates(s, org)).items}
    assert found == {(c, UUID(subnets[c]["id"])) for c in (x, y)}
    x_tech = await everything_on(api_env, org, x)
    listed = await api_env.client.get("/api/v1/devices/candidates", headers=x_tech)
    assert listed.status_code == 200, listed.text
    assert [(i["address"], i["client_id"]) for i in listed.json()["items"]] == [
        ("10.0.0.9", str(x))
    ]


async def test_another_client_s_machines_hide_no_candidate(api_env: ApiEnv) -> None:
    """The three exclusions -- an address an agent claimed, a MAC an agent
    claimed, an address an asset row holds -- are within the candidate's
    client. X's machines at 10.0.0.9/10 and with MAC m say nothing about
    what answered on Y's network; the same rows hide X's own sightings."""
    org = await api_env.create_org("i-cand-hide")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    mac = "a4:bb:6d:00:00:01"
    x_subnet = await _subnet(api_env, admin, "10.0.0.0/24", x, x_site)
    y_subnet = await _subnet(api_env, admin, "10.0.0.0/24", y, y_site)
    x_vrf = UUID(x_subnet["vrf_id"])
    await seed_device(api_env, org, client_id=x, site_id=x_site, source="manual",
                      provenance="operator", primary_ip="10.0.0.9")
    agent_machine = await seed_device(api_env, org, client_id=x, site_id=x_site)
    for address, seen_mac in (("10.0.0.10", None), ("10.0.0.30", mac)):
        await upsert_observation_row(
            api_env, org, vrf_id=x_vrf, address=address, source="inventory", method="nic",
            confidence="high", device_id=agent_machine, mac=seen_mac,
        )
    for address, seen_mac in (("10.0.0.9", None), ("10.0.0.10", None), ("10.0.0.11", mac)):
        await _swept(api_env, org, y_subnet, address, seen_mac)
        await _swept(api_env, org, x_subnet, address, seen_mac)
    assert await _candidates(api_env, org) == {
        ("10.0.0.9", y), ("10.0.0.10", y), ("10.0.0.11", y),
    }


# --------------------------------------------------------------------------- #
# What a sweep records: in the subnet's client and site, or a candidate
# --------------------------------------------------------------------------- #


async def _subnet_row(env: ApiEnv, org: UUID, subnet_id: str) -> IpamSubnet:
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        row = await s.get(IpamSubnet, UUID(subnet_id))
        assert row is not None
        return row


async def _record(
    env: ApiEnv, org: UUID, subnet_id: str, sightings: list[SweepSighting]
) -> list[Any]:
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        subnet = await s.get(IpamSubnet, UUID(subnet_id))
        assert subnet is not None
        return await auto_create_from_sweep(
            s, org, subnet=subnet, sightings=sightings, sweep_run_id=uuid4()
        )


async def test_a_sweep_records_a_machine_another_client_already_has(api_env: ApiEnv) -> None:
    """`_known_inventory` is the subnet's client's, in all four of its
    lookups: an address an agent claimed, a MAC an agent claimed, an asset
    row's address and an asset row's MAC. X's knowing each is no reason to
    leave Y's sighting unrecorded, nor to answer with X's device. On X's own
    subnet every one of them is "already recorded"."""
    org = await api_env.create_org("i-known")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    asset_mac, agent_mac = "a4:bb:6d:00:00:02", "a4:bb:6d:00:00:04"
    x_subnet = await _subnet(api_env, admin, "10.0.0.0/24", x, x_site)
    y_subnet = await _subnet(api_env, admin, "10.0.0.0/24", y, y_site)
    x_asset = await seed_device(
        api_env, org, client_id=x, site_id=x_site, source="manual", provenance="operator",
        primary_ip="10.0.0.9", nics={"items": [{"identifier": asset_mac, "mac": asset_mac}]},
    )
    x_agent = await seed_device(api_env, org, client_id=x, site_id=x_site)
    for address, mac in (("10.0.0.10", None), ("10.0.0.30", agent_mac)):
        await upsert_observation_row(
            api_env, org, vrf_id=UUID(x_subnet["vrf_id"]), address=address,
            source="inventory", method="nic", confidence="high", device_id=x_agent, mac=mac,
        )

    def seen(address: str, mac: str | None = None) -> SweepSighting:
        return SweepSighting(address=address, mac=mac, hostname="",
                             confidence="medium" if mac else "low")

    sightings = [
        seen("10.0.0.9"),               # X's asset row's address
        seen("10.0.0.20", asset_mac),   # X's asset row's MAC
        seen("10.0.0.10"),              # an address X's agent claimed
        seen("10.0.0.21", agent_mac),   # a MAC X's agent claimed
    ]
    mine = await _record(api_env, org, x_subnet["id"], sightings)
    assert [(r.created, r.action) for r in mine] == [(False, "already recorded")] * 4
    assert mine[0].device_id == x_asset and mine[1].device_id == x_asset
    theirs = await _record(api_env, org, y_subnet["id"], sightings)
    assert [r.created for r in theirs] == [True] * 4, theirs
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (await s.execute(
            select(Device.client_id, Device.site_id, Device.provenance).where(
                Device.id.in_([r.device_id for r in theirs]))
        )).all()
        audited = (await s.execute(
            select(AuditEvent.client_id, AuditEvent.target_id).where(
                AuditEvent.org_id == org, AuditEvent.action == "asset.auto_created")
        )).all()
    assert [tuple(r) for r in rows] == [(y, y_site, "sweep")] * 4
    assert sorted(tuple(r) for r in audited) == sorted((y, str(r.device_id)) for r in theirs)


async def test_a_sweep_of_a_subnet_without_a_site_records_candidates(api_env: ApiEnv) -> None:
    """C7, on the automatic path: a machine needs a site and a subnet without
    one names none, so nothing is created -- the result says `candidate`,
    and the sighting stays on the Discovered list. The same for a subnet
    whose site is archived. Nothing is written, audit included."""
    org = await api_env.create_org("i-auto-nosite")
    x, x_site = await client_with_site(api_env, org)
    _, gone = await client_with_site(api_env, org, site_archived=True)
    admin = await _admin(api_env, org)
    siteless = await _subnet(api_env, admin, "10.3.0.0/24", x)
    async with AsyncSession(api_env.admin_engine) as s:
        archived_client = (await s.execute(select(Site.client_id).where(Site.id == gone))).one()[0]
    archived = await _subnet(api_env, admin, "10.4.0.0/24", archived_client)
    async with AsyncSession(api_env.admin_engine) as s:  # at the site before it was archived
        await s.execute(text("UPDATE ipam_subnet SET site_id = :s WHERE id = :i"),
                        {"s": gone, "i": archived["id"]})
        await s.commit()
    before = await _counts(api_env, org)
    for subnet, address in ((siteless, "10.3.0.9"), (archived, "10.4.0.9")):
        results = await _record(api_env, org, subnet["id"], [
            SweepSighting(address=address, mac=None, hostname="", confidence="low")
        ])
        assert [(r.created, r.device_id, r.action) for r in results] == [
            (False, None, "candidate")
        ], subnet
    assert await _counts(api_env, org) == before
    # The positive twin: the same sighting on a subnet at an active site is a machine.
    sited = await _subnet(api_env, admin, "10.5.0.0/24", x, x_site)
    [made] = await _record(api_env, org, sited["id"], [
        SweepSighting(address="10.5.0.9", mac=None, hostname="", confidence="low")
    ])
    assert made.created is True


async def test_a_completed_sweep_files_its_machines_under_the_subnet_s_client(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """Through the real result handler: Y's sweep of Y's subnet at Y's site
    writes Y's machines at that site. A sweep of a site-less subnet writes
    none, and what it saw is a candidate of its client."""
    org = await api_env.create_org("i-handler")
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    sited = await _subnet(api_env, admin, "10.6.0.0/24", y, y_site)
    siteless = await _subnet(api_env, admin, "10.7.0.0/24", y)
    sweeper = await seed_device(api_env, org, client_id=y, site_id=y_site)
    for subnet, address in ((sited, "10.6.0.20"), (siteless, "10.7.0.20")):
        fx = await dispatch_sweep(
            api_env, org, vrf_id=UUID(subnet["vrf_id"]), device_id=sweeper,
            prefix=subnet["prefix"], subnet_id=UUID(subnet["id"]),
        )
        job = await complete_sweep(
            api_env.app_engine, api_env, fx, sweep_document(fx, [sweep_host(address)]), tmp_path
        )
        assert job.status.value == "succeeded", job.result
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (await s.execute(
            select(Device.client_id, Device.site_id, Device.primary_ip).where(
                Device.org_id == org, Device.provenance == "sweep")
        )).all()
    assert [(c, site, str(ip)) for c, site, ip in rows] == [(y, y_site, "10.6.0.20")]
    assert await _candidates(api_env, org) == {("10.7.0.20", y)}


# --------------------------------------------------------------------------- #
# Promotion: names its client; the machine takes the subnet's client and site
# --------------------------------------------------------------------------- #


async def test_a_promoted_address_becomes_a_device_of_the_subnet_s_client_and_site(
    api_env: ApiEnv,
) -> None:
    """A technician narrowed to X promotes X's candidate (the stopgap
    `require_all_clients` is gone): the machine is X's, at the subnet's site,
    vouched for, and audited under X."""
    org = await api_env.create_org("i-promote")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = await _subnet(api_env, admin, "10.6.0.0/24", x, x_site)
    await _swept(api_env, org, subnet, "10.6.0.9", "aa:bb:cc:dd:ee:ff")
    x_tech = await everything_on(api_env, org, x)
    r = await api_env.client.post(
        PROMOTE, json={"client_id": str(x), "addresses": ["10.6.0.9"]}, headers=x_tech
    )
    assert r.status_code == 200, r.text
    [result] = r.json()["results"]
    assert result["created"] is True, result
    async with AsyncSession(api_env.admin_engine) as s:
        client, site, provenance = (await s.execute(
            select(Device.client_id, Device.site_id, Device.provenance).where(
                Device.org_id == org, Device.id == UUID(result["device_id"]))
        )).one()
        audited = (await s.execute(
            select(AuditEvent.client_id).where(
                AuditEvent.org_id == org, AuditEvent.action == "asset.promoted")
        )).scalars().all()
    assert (client, site, provenance) == (x, x_site, "promoted")
    assert audited == [x]


async def test_a_subnet_without_a_site_cannot_promote(api_env: ApiEnv) -> None:
    """C7: a device needs a site, and a client has no marked default. The
    candidate is refused by name, with the sentence that says what to do,
    and nothing is written -- no device, no audit."""
    org = await api_env.create_org("i-nosite")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = await _subnet(api_env, admin, "10.7.0.0/24", x)
    await _swept(api_env, org, subnet, "10.7.0.9")
    before = await _counts(api_env, org)
    r = await api_env.client.post(
        PROMOTE, json={"client_id": str(x), "addresses": ["10.7.0.9"]}, headers=admin
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"] == [
        {"address": "10.7.0.9", "created": False, "device_id": None, "reason": NO_SITE}
    ]
    assert await _counts(api_env, org) == before


async def test_a_candidate_at_an_archived_site_is_refused(api_env: ApiEnv) -> None:
    """The subnet's site takes no new machine once archived (as a hand-typed
    asset's would not): refused per candidate, nothing written."""
    org = await api_env.create_org("i-archsite")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = await _subnet(api_env, admin, "10.8.0.0/24", x, x_site)
    await _swept(api_env, org, subnet, "10.8.0.9")
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(text("UPDATE sites SET archived_at = now() WHERE id = :s"), {"s": x_site})
        await s.commit()
    before = await _counts(api_env, org)
    r = await api_env.client.post(
        PROMOTE, json={"client_id": str(x), "addresses": ["10.8.0.9"]}, headers=admin
    )
    assert r.status_code == 200, r.text
    [result] = r.json()["results"]
    assert (result["created"], result["reason"]) == (False, "that site is archived")
    assert await _counts(api_env, org) == before


async def test_a_promotion_cannot_reach_another_client(api_env: ApiEnv) -> None:
    """A technician of X naming Y is told Y does not exist (404), and naming
    X for Y's address is told it is not X's candidate. An every-client admin
    naming X for Y's address gets the same answer: the client a promotion
    names is the one its candidates are taken from, and nothing lands in Y."""
    org = await api_env.create_org("i-promote-y")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _subnet(api_env, admin, "10.9.0.0/24", x, x_site)
    y_subnet = await _subnet(api_env, admin, "10.9.0.0/24", y, y_site)
    await _swept(api_env, org, y_subnet, "10.9.0.9")
    x_tech = await everything_on(api_env, org, x)
    before = await _counts(api_env, org)
    hidden = await api_env.client.post(
        PROMOTE, json={"client_id": str(y), "addresses": ["10.9.0.9"]}, headers=x_tech
    )
    assert hidden.status_code == 404, hidden.text
    for headers in (x_tech, admin):
        r = await api_env.client.post(
            PROMOTE, json={"client_id": str(x), "addresses": ["10.9.0.9"]}, headers=headers
        )
        assert r.status_code == 200, r.text
        [result] = r.json()["results"]
        assert (result["created"], result["device_id"]) == (False, None), result
        assert "not a candidate" in result["reason"]
    assert await _counts(api_env, org) == before
    # The positive twin: Y's address, promoted in Y, is Y's machine.
    ok = await api_env.client.post(
        PROMOTE, json={"client_id": str(y), "addresses": ["10.9.0.9"]}, headers=admin
    )
    assert ok.json()["results"][0]["created"] is True, ok.text


async def test_an_asset_of_another_client_does_not_make_an_address_known(
    api_env: ApiEnv,
) -> None:
    """"Already an asset" is within the client: X's machine at 10.0.0.9 is
    not Y's 10.0.0.9, and promoting Y's must not answer with X's device id.
    In X, the same address IS already X's asset."""
    org = await api_env.create_org("i-promote-known")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_machine = await seed_device(api_env, org, client_id=x, site_id=x_site, source="manual",
                                  provenance="operator", primary_ip="10.0.0.9")
    y_subnet = await _subnet(api_env, admin, "10.0.0.0/24", y, y_site)
    await _swept(api_env, org, y_subnet, "10.0.0.9")
    known = await api_env.client.post(
        PROMOTE, json={"client_id": str(x), "addresses": ["10.0.0.9"]}, headers=admin
    )
    assert known.json()["results"] == [{
        "address": "10.0.0.9", "created": False, "device_id": str(x_machine),
        "reason": "already an asset",
    }]
    r = await api_env.client.post(
        PROMOTE, json={"client_id": str(y), "addresses": ["10.0.0.9"]}, headers=admin
    )
    [result] = r.json()["results"]
    assert result["created"] is True and result["device_id"] != str(x_machine), result


async def test_a_promotion_names_a_client_that_may_take_machines(api_env: ApiEnv) -> None:
    """The named client is loaded like every create's: unknown is 404,
    archived 409, "Sin asignar" 422 (C10), and a body without one is 422.
    Each refusal writes nothing."""
    org = await api_env.create_org("i-promote-client")
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    admin = await _admin(api_env, org)
    nobody = await _unassigned(api_env, org)
    before = await _counts(api_env, org)
    for body, status in (
        ({"client_id": str(uuid4()), "addresses": ["10.0.0.9"]}, 404),
        ({"client_id": str(archived), "addresses": ["10.0.0.9"]}, 409),
        ({"client_id": str(nobody), "addresses": ["10.0.0.9"]}, 422),
        ({"addresses": ["10.0.0.9"]}, 422),
    ):
        r = await api_env.client.post(PROMOTE, json=body, headers=admin)
        assert r.status_code == status, (body, r.text)
    assert await _counts(api_env, org) == before


async def test_a_promotion_reads_subnets_and_sites_once(api_env: ApiEnv) -> None:
    """No fan-out: however many candidates, on however many subnets and
    sites, one statement reads their subnets and one their sites."""
    org = await api_env.create_org("i-promote-fan")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        other = Site(org_id=org, client_id=x, name="Sucursal")
        s.add(other)
        await s.commit()
    addresses = []
    for n, site in enumerate((x_site, other.id, x_site)):
        subnet = await _subnet(api_env, admin, f"10.{20 + n}.0.0/24", x, site)
        for host in (5, 6):
            await _swept(api_env, org, subnet, f"10.{20 + n}.0.{host}")
            addresses.append(f"10.{20 + n}.0.{host}")
    seen: list[str] = []

    def tick(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        seen.append(statement)

    engine = api_env.app.state.app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", tick)
    try:
        r = await api_env.client.post(
            PROMOTE, json={"client_id": str(x), "addresses": addresses}, headers=admin
        )
    finally:
        event.remove(engine, "before_cursor_execute", tick)
    assert [res["created"] for res in r.json()["results"]] == [True] * 6, r.text
    from_subnets = [q for q in seen if "FROM ipam_subnet" in q]
    from_sites = [q for q in seen if "FROM sites" in q]
    assert (len(from_subnets), len(from_sites)) == (1, 1), (from_subnets, from_sites)


# --------------------------------------------------------------------------- #
# Claiming an address: by a machine of the address's client
# --------------------------------------------------------------------------- #


async def test_an_address_is_claimed_only_by_a_machine_of_its_client(api_env: ApiEnv) -> None:
    """`claim_address` (both `assign` and `allocate` land there): Y's machine
    cannot hold X's address -- 422 for an admin who sees it, "not found" for
    a technician of X who does not -- and nothing is written. X's machine
    can, for the narrowed technician too."""
    org = await api_env.create_org("i-claim")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = await _subnet(api_env, admin, "10.10.0.0/24", x)
    foreign = await seed_device(api_env, org, client_id=y, site_id=y_site)
    mine = await seed_device(api_env, org, client_id=x, site_id=x_site)
    x_tech = await everything_on(api_env, org, x)
    assign = {"address": "10.10.0.5", "vrf_id": subnet["vrf_id"]}
    allocate = f"/subnets/{subnet['id']}/allocate"
    before = await _counts(api_env, org)
    for path, body in (("/addresses", assign), (allocate, {})):
        r = await _post(api_env, path, {**body, "device_id": str(foreign)}, admin)
        assert r.status_code == 422, (path, r.text)
        assert OTHER_CLIENT in r.json()["detail"], (path, r.text)
        hidden = await _post(api_env, path, {**body, "device_id": str(foreign)}, x_tech)
        assert hidden.status_code == 400, (path, hidden.text)
        assert "not found" in hidden.json()["detail"], (path, hidden.text)
    assert await _counts(api_env, org) == before
    assigned = await _created(api_env, "/addresses", {**assign, "device_id": str(mine)}, x_tech)
    allocated = await _created(api_env, allocate, {"device_id": str(mine)}, x_tech)
    assert {assigned["device_id"], allocated["device_id"]} == {str(mine)}


# --------------------------------------------------------------------------- #
# Import: names its client; written rows are found in its own VRF
# --------------------------------------------------------------------------- #

IMPORT_MAPPING = {"IP": "address", "Owner": "asset_label"}
IMPORT_CSV = b"IP,Owner\n10.70.0.10,Printer A\n10.70.0.11,\n"


async def _import(
    env: ApiEnv, headers: dict[str, str], client: UUID | None, *, apply: bool = False,
) -> Any:
    from app.modules.ipam.importer import preview_digest

    params: dict[str, str] = {"mapping": json.dumps(IMPORT_MAPPING)}
    if client is not None:
        params["client_id"] = str(client)
    if apply:
        params["digest"] = preview_digest(IMPORT_CSV, IMPORT_MAPPING)
    return await env.client.post(
        f"{I}/import/{'apply' if apply else 'preview'}", params=params, content=IMPORT_CSV,
        headers={**headers, "Content-Type": "text/csv"},
    )


async def test_an_import_names_its_client(api_env: ApiEnv) -> None:
    """research-3 §I2 / Task 10 review (b): an import filed everything under
    the internal client, and a technician narrowed to X met an RLS refusal
    -- a 500 -- creating that client's `global` VRF. Now the import names its
    client: a technician of X previews and applies into X's network; Y is a
    404, an archived client 409, "Sin asignar" 422, no client 422 -- and a
    refused import writes nothing, the VRF included."""
    org = await api_env.create_org("i-import")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    admin = await _admin(api_env, org)
    x_tech = await everything_on(api_env, org, x)
    nobody = await _unassigned(api_env, org)
    before = await _counts(api_env, org)
    for apply in (False, True):
        for headers, client, status in (
            (x_tech, y, 404), (x_tech, org, 404), (admin, archived, 409),
            (admin, nobody, 422), (admin, None, 422),
        ):
            r = await _import(api_env, headers, client, apply=apply)
            assert r.status_code == status, (apply, client, r.text)
    assert await _counts(api_env, org) == before

    await _subnet(api_env, admin, "10.70.0.0/24", x)
    preview = await _import(api_env, x_tech, x)
    assert preview.status_code == 200, preview.text
    assert preview.json()["creates"] == 2
    applied = await _import(api_env, x_tech, x, apply=True)
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] == 2
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (await s.execute(
            select(IpamAddress.client_id, IpamAddress.origin).where(IpamAddress.org_id == org)
        )).all()
        audited = (await s.execute(
            select(AuditEvent.client_id).where(
                AuditEvent.org_id == org, AuditEvent.action == "ipam.import_applied")
        )).scalars().all()
    assert sorted(rows) == [(x, "imported"), (x, "imported")]
    assert audited == [x]


async def test_an_import_marks_its_own_rows_when_others_hold_the_address(
    api_env: ApiEnv,
) -> None:
    """The row an import wrote is the one in ITS client's VRF (research-3
    §I3): looked up by address alone, Y's 10.70.0.10 -- or X's own in
    another VRF -- made the lookup ambiguous, a 500, or marked the wrong row
    `imported`."""
    org = await api_env.create_org("i-import-vrf")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    lan = await _created(api_env, "/vrfs", {"name": "LAN", "client_id": str(x)}, admin)
    await _subnet(api_env, admin, "10.70.0.0/24", x)
    await _subnet(api_env, admin, "10.70.0.0/24", y)
    await _created(api_env, "/subnets", {"prefix": "10.70.0.0/24", "vrf_id": lan["id"]}, admin)
    for body in (
        {"address": "10.70.0.10", "reserve": True, "client_id": str(y)},
        {"address": "10.70.0.10", "reserve": True, "vrf_id": lan["id"]},
    ):
        await _created(api_env, "/addresses", body, admin)
    applied = await _import(api_env, admin, x, apply=True)
    assert applied.status_code == 200, applied.text
    x_global = await _default_vrf(api_env, x)
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (await s.execute(
            select(IpamAddress.vrf_id, IpamAddress.origin).where(
                IpamAddress.org_id == org, IpamAddress.address == "10.70.0.10")
        )).all()
    assert sorted((str(v), o) for v, o in rows) == sorted([
        (str(x_global), "imported"), (lan["id"], "manual"),
        (str(await _default_vrf(api_env, y)), "manual"),
    ])


async def test_a_documented_network_is_filed_under_its_record_s_client(
    api_env: ApiEnv,
) -> None:
    """`convert_doc_records` (Task 10 review M10b): a network record of X
    becomes a subnet of X, in X's `global` VRF; a record with no client is
    the internal client's. Each conversion is audited under the client it
    filed into, the dry run as much as the real one."""
    org = await api_env.create_org("i-docnet")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    async with AsyncSession(api_env.admin_engine) as s:
        s.add_all([
            DocRecord(org_id=org, client_id=x, record_type="network", name="x-lan",
                      fields={"cidr": "10.71.0.0/24"}),
            DocRecord(org_id=org, record_type="network", name="hq-lan",
                      fields={"cidr": "10.72.0.0/24"}),
        ])
        await s.commit()
    for dry_run in ("true", "false"):
        r = await api_env.client.post(
            f"{I}/import/doc-records", params={"dry_run": dry_run}, headers=admin
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["converted"]) == 2, r.text
    async with AsyncSession(api_env.admin_engine) as s:
        subnets = (await s.execute(
            select(IpamSubnet.prefix, IpamSubnet.client_id, IpamVrf.client_id, IpamVrf.name)
            .join(IpamVrf, IpamVrf.id == IpamSubnet.vrf_id).where(IpamSubnet.org_id == org)
        )).all()
        audited = (await s.execute(
            select(AuditEvent.action, AuditEvent.client_id).where(
                AuditEvent.org_id == org,
                AuditEvent.action.in_(["ipam.import_previewed", "ipam.import_applied"]),
            )
        )).all()
    assert sorted((str(p), c, v, n) for p, c, v, n in subnets) == [
        ("10.71.0.0/24", x, x, "global"), ("10.72.0.0/24", org, org, "global"),
    ]
    assert sorted(audited, key=str) == sorted([
        ("ipam.import_previewed", x), ("ipam.import_previewed", org),
        ("ipam.import_applied", x), ("ipam.import_applied", org),
    ], key=str)


# --------------------------------------------------------------------------- #
# Events, the fleet list's client filter, and every audit of these paths
# --------------------------------------------------------------------------- #


async def test_drift_is_published_under_its_client(api_env: ApiEnv) -> None:
    """`ipam.drift_detected` is a domain event about X's drift, so it is X's
    (it was published with no client)."""
    from app.modules.ipam.reconcile import evaluate_org

    org = await api_env.create_org("i-drift-ev")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    subnet = await _subnet(api_env, admin, "10.73.0.0/24", x)
    await _swept(api_env, org, subnet, "10.73.0.9")
    for _ in range(2):  # published on the second consecutive detection
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            await evaluate_org(s, org)
    async with AsyncSession(api_env.admin_engine) as s:
        clients = (await s.execute(
            select(DomainEvent.client_id).where(
                DomainEvent.org_id == org, DomainEvent.event_type == "ipam.drift_detected")
        )).scalars().all()
    assert clients == [x]


async def test_the_fleet_list_filters_by_client(api_env: ApiEnv) -> None:
    """`GET /devices?client_id=`: what the sweeper picker lists -- the
    subnet's client's machines and no one else's."""
    org = await api_env.create_org("i-fleet")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    mine = await seed_device(api_env, org, client_id=x, site_id=x_site)
    await seed_device(api_env, org, client_id=y, site_id=y_site)
    r = await api_env.client.get("/api/v1/devices", params={"client_id": str(x)}, headers=admin)
    assert r.status_code == 200, r.text
    assert [d["id"] for d in r.json()["items"]] == [str(mine)]
    assert r.json()["total"] == 1
    everyone = await api_env.client.get("/api/v1/devices", headers=admin)
    assert everyone.json()["total"] >= 2


#: Every audit action of a sweep, a promotion and an import (Task 11). Each
#: must be written with the client of the subnet, the job or the import.
AUDITED_TASK_11 = {
    "ipam.sweep_dispatched", "ipam.sweep_rejected", "ipam.sweep_out_of_scope",
    "ipam.import_applied", "ipam.import_previewed",
    "asset.promoted", "asset.auto_created", "asset.auto_readdressed",
}


async def test_every_sweep_promotion_and_import_audit_names_its_client(
    api_env: ApiEnv, tmp_path: Path
) -> None:
    """Every one through its real path, in a customer client X: a dispatched
    sweep, a rejected one, a completed one that creates and then re-addresses
    a machine, a promotion, a CSV import and a documented network."""
    org, x, x_site, admin = await _x_world(api_env, "i-audit-11")
    subnet = await _subnet(api_env, admin, "10.74.0.0/24", x, x_site)
    sid, vrf = subnet["id"], UUID(subnet["vrf_id"])
    await _created(api_env, "/scan-authorizations", {"prefix": "10.74.0.0/24", "vrf_id": str(vrf)},
                   admin)
    sweeper = await _device_seen_in(api_env, org, x, x_site, vrf, "10.74.0.2")
    assert (await api_env.client.post(f"{I}/subnets/{sid}/sweep", headers=admin)).status_code == 202

    mac = "a4:bb:6d:00:00:03"
    for host, expect in (("10.74.0.50", "succeeded"), ("10.74.0.51", "succeeded"),
                         ("10.99.0.1", "failed")):
        fx = await dispatch_sweep(api_env, org, vrf_id=vrf, device_id=sweeper,
                                  prefix="10.74.0.0/24", subnet_id=UUID(sid))
        job = await complete_sweep(
            api_env.app_engine, api_env, fx, sweep_document(fx, [sweep_host(host, mac=mac)]),
            tmp_path,
        )
        assert job.status.value == expect, (host, job.result)

    await _swept(api_env, org, subnet, "10.74.0.60")
    await _subnet(api_env, admin, "10.70.0.0/24", x)  # where the CSV's rows go
    promoted = await api_env.client.post(
        PROMOTE, json={"client_id": str(x), "addresses": ["10.74.0.60"]}, headers=admin
    )
    assert promoted.json()["results"][0]["created"] is True, promoted.text
    assert (await _import(api_env, admin, x, apply=True)).status_code == 200
    async with AsyncSession(api_env.admin_engine) as s:
        s.add(DocRecord(org_id=org, client_id=x, record_type="network", name="x-net",
                        fields={"cidr": "10.75.0.0/24"}))
        await s.commit()
    assert (await api_env.client.post(f"{I}/import/doc-records", headers=admin)).status_code == 200

    async with AsyncSession(api_env.admin_engine) as s:
        rows = (await s.execute(
            select(AuditEvent.action, AuditEvent.client_id).where(
                AuditEvent.org_id == org, AuditEvent.action.in_(sorted(AUDITED_TASK_11)))
        )).all()
    assert {r.action for r in rows} == AUDITED_TASK_11
    assert {(r.action, r.client_id) for r in rows if r.client_id != x} == set()
