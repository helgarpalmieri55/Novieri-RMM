"""Spec §5 test 10 and §1.5: a device lands in its link's client and site."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Device, Site
from tests.install_link_fixtures import FACTS as LINK_FACTS
from tests.install_link_fixtures import make_csr_pem, peer_client

FACTS = {
    "hostname": "wks-cli",
    "os_family": "linux",
    "os_version": "Ubuntu 24.04",
    "architecture": "x86_64",
}

#: A manufacturer-assigned unicast address: the one shape enrollment links on.
PHYSICAL = "00:1b:21:aa:bb:cc"


async def _customer_with_site(env, org_id):
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        c = Client(org_id=org_id, name="Cliente X", kind="customer")
        s.add(c)
        await s.flush()
        site = Site(org_id=org_id, client_id=c.id, name="Bogotá")
        s.add(site)
        await s.commit()
        return c.id, site.id


async def test_a_token_is_minted_for_a_site_and_carries_its_client(api_env) -> None:
    env = api_env
    org = await env.create_org("enr")
    sub = await env.create_user(org, "org-admin")
    client_id, site_id = await _customer_with_site(env, org)
    r = await env.client.post(
        "/api/v1/enrollment-tokens",
        json={"name": "x", "max_uses": 1, "expires_in_hours": 1, "site_id": str(site_id)},
        headers=await env.headers(org, sub),
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(client_id)
    assert r.json()["site_id"] == str(site_id)


async def test_a_token_without_a_site_is_refused(api_env) -> None:
    env = api_env
    org = await env.create_org("enr")
    sub = await env.create_user(org, "org-admin")
    r = await env.client.post(
        "/api/v1/enrollment-tokens",
        json={"name": "x", "max_uses": 1, "expires_in_hours": 1},
        headers=await env.headers(org, sub),
    )
    assert r.status_code == 422


async def test_a_token_for_another_organizations_site_is_404(api_env) -> None:
    """RLS makes a site the caller cannot see absent, and absent is a 404 --
    never a token minted for a client the caller does not hold."""
    env = api_env
    org = await env.create_org("enr")
    other = await env.create_org("enr-other")
    sub = await env.create_user(org, "org-admin")
    _, their_site = await _customer_with_site(env, other)
    r = await env.client.post(
        "/api/v1/enrollment-tokens",
        json={"name": "x", "max_uses": 1, "expires_in_hours": 1, "site_id": str(their_site)},
        headers=await env.headers(org, sub),
    )
    assert r.status_code == 404, r.text


async def _enroll_with_token(env, org, sub, site_id: UUID, facts: dict) -> UUID:
    token = await env.client.post(
        "/api/v1/enrollment-tokens",
        json={"name": "x", "max_uses": 1, "expires_in_hours": 1, "site_id": str(site_id)},
        headers=await env.headers(org, sub),
    )
    assert token.status_code == 201, token.text
    enrolled = await env.client.post(
        "/enroll/v1",
        json={"token": token.json()["token"], "csr_pem": make_csr_pem(), "device_facts": facts},
    )
    assert enrolled.status_code == 201, enrolled.text
    return UUID(enrolled.json()["device_id"])


async def test_an_agent_enrolled_with_the_token_lands_in_its_client_and_site(api_env) -> None:
    env = api_env
    org = await env.create_org("enr")
    sub = await env.create_user(org, "org-admin")
    client_id, site_id = await _customer_with_site(env, org)
    device_id = await _enroll_with_token(env, org, sub, site_id, FACTS)
    async with AsyncSession(env.admin_engine) as s:
        device = (await s.execute(select(Device).where(Device.id == device_id))).scalar_one()
    assert device.client_id == client_id
    assert device.site_id == site_id


async def test_enrollment_never_links_an_asset_of_another_client(api_env) -> None:
    """Spec §1.5: the enrollment session sees only the token's client. An
    agentless record of the INTERNAL client carrying the very MAC the agent
    reports would be linked under an every-client scope; under the token's
    scope it does not exist, so the agent gets a new row in its own client."""
    env = api_env
    org = await env.create_org("enr")
    sub = await env.create_user(org, "org-admin")
    client_id, site_id = await _customer_with_site(env, org)
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        internal_asset = Device(
            org_id=org,
            client_id=org,
            site_id=org,
            source="manual",
            provenance="operator",
            nics={"items": [{"identifier": PHYSICAL, "mac": PHYSICAL}]},
        )
        s.add(internal_asset)
        await s.commit()
        internal_id = internal_asset.id

    device_id = await _enroll_with_token(env, org, sub, site_id, FACTS | {"macs": [PHYSICAL]})

    assert device_id != internal_id
    async with AsyncSession(env.admin_engine) as s:
        device = (await s.execute(select(Device).where(Device.id == device_id))).scalar_one()
        untouched = (
            await s.execute(select(Device).where(Device.id == internal_id))
        ).scalar_one()
    assert device.client_id == client_id and device.site_id == site_id
    assert untouched.client_id == org and untouched.hostname is None


async def test_the_organization_level_install_link_route_is_gone(api_env) -> None:
    env = api_env
    org = await env.create_org("enr")
    headers = await env.headers(org, await env.create_user(org, "org-admin"))
    assert (await env.client.get("/api/v1/install-link", headers=headers)).status_code == 404
    put = await env.client.put(
        "/api/v1/install-link", json={"enabled": False}, headers=headers
    )
    assert put.status_code == 404


async def test_a_machine_joining_through_a_sites_link_lands_in_that_site(api_env) -> None:
    """Spec §1.5 and §3.2: one link per site, its networks are the site's
    egress networks, and a machine that joins through it belongs to the
    site's client."""
    env = api_env
    org = await env.create_org("enr")
    headers = await env.headers(org, await env.create_user(org, "org-admin"))
    client_id, site_id = await _customer_with_site(env, org)
    saved = await env.client.put(
        f"/api/v1/sites/{site_id}/install-link",
        json={"enabled": True, "networks": ["10.50.0.0/16"], "hourly_limit": 50},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    code = saved.json()["url"].rsplit("/i/", 1)[1]
    async with AsyncSession(env.admin_engine) as s:
        site = (await s.execute(select(Site).where(Site.id == site_id))).scalar_one()
    assert [str(n) for n in site.egress_networks] == ["10.50.0.0/16"]
    # The Principal site's link is another link, and still off.
    principal = await env.client.get(f"/api/v1/sites/{org}/install-link", headers=headers)
    assert principal.status_code == 200 and principal.json()["has_code"] is False

    async with peer_client(env, "10.50.0.9") as peer:
        joined = await peer.post(
            "/enroll/v1",
            json={"link_code": code, "csr_pem": make_csr_pem(), "device_facts": LINK_FACTS},
        )
    assert joined.status_code == 201, joined.text
    async with AsyncSession(env.admin_engine) as s:
        device = (
            await s.execute(select(Device).where(Device.id == UUID(joined.json()["device_id"])))
        ).scalar_one()
    assert device.client_id == client_id and device.site_id == site_id
