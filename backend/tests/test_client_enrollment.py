"""Spec §5 test 10 and §1.5: a device lands in its link's client and site."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Client, Device, InstallLink, Site
from tests.client_scope_support import client_with_site
from tests.install_link_fixtures import FACTS as LINK_FACTS
from tests.install_link_fixtures import link_path, make_csr_pem, peer_client, switch_on

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


# --------------------------------------------------------------------------- #
# Review fixes (Task 7 review 1).
# --------------------------------------------------------------------------- #


async def test_the_enrolment_audit_names_the_client_and_the_site(api_env) -> None:
    """M6: `enrollment.device_enrolled` says where the machine landed."""
    env = api_env
    org = await env.create_org("enr")
    sub = await env.create_user(org, "org-admin")
    client_id, site_id = await _customer_with_site(env, org)
    device_id = await _enroll_with_token(env, org, sub, site_id, FACTS)
    async with AsyncSession(env.admin_engine) as s:
        audit = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org,
                    AuditEvent.action == "enrollment.device_enrolled",
                    AuditEvent.target_id == str(device_id),
                )
            )
        ).scalar_one()
    assert audit.meta["client_id"] == str(client_id)
    assert audit.meta["site_id"] == str(site_id)


async def test_the_token_list_names_each_tokens_client_and_site(api_env) -> None:
    """M1: the list says where each token's machines land, not only the mint."""
    env = api_env
    org = await env.create_org("enr")
    headers = await env.headers(org, await env.create_user(org, "org-admin"))
    client_id, site_id = await _customer_with_site(env, org)
    for site in (site_id, org):
        minted = await env.client.post(
            "/api/v1/enrollment-tokens",
            json={"name": "x", "max_uses": 1, "expires_in_hours": 1, "site_id": str(site)},
            headers=headers,
        )
        assert minted.status_code == 201, minted.text
    items = (await env.client.get("/api/v1/enrollment-tokens", headers=headers)).json()["items"]
    assert sorted((i["client_id"], i["site_id"]) for i in items) == sorted(
        [(str(client_id), str(site_id)), (str(org), str(org))]
    )


async def test_a_token_is_not_minted_for_an_archived_site(api_env) -> None:
    """M4: nothing new lands in an archived site."""
    env = api_env
    org = await env.create_org("enr")
    _, site_id = await client_with_site(env, org, site_archived=True)
    r = await env.client.post(
        "/api/v1/enrollment-tokens",
        json={"name": "x", "max_uses": 1, "expires_in_hours": 1, "site_id": str(site_id)},
        headers=await env.headers(org, await env.create_user(org, "org-admin")),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "that site is archived"


async def test_an_archived_sites_link_is_not_saved(api_env) -> None:
    """M4: switching on a link would let machines join an archived site."""
    env = api_env
    org = await env.create_org("enr")
    _, site_id = await client_with_site(env, org, site_archived=True)
    r = await env.client.put(
        link_path(site_id),
        json={"enabled": True, "networks": ["10.50.0.0/16"]},
        headers=await env.headers(org, await env.create_user(org, "org-admin")),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "that site is archived"


async def _join(env, code: str, source: str):
    async with peer_client(env, source) as peer:
        return await peer.post(
            "/enroll/v1",
            json={"link_code": code, "csr_pem": make_csr_pem(), "device_facts": LINK_FACTS},
        )


async def test_the_sites_networks_are_what_a_join_is_checked_against(api_env) -> None:
    """I4 and spec §5 test 10's second half. `save` writes the networks to the
    site AND keeps a copy on the link row (0170's CHECK wants one), so a test
    that sets both alike cannot tell which one is read. Here they differ: the
    site says 10.60/16, the stale copy says 10.50/16. Only the site counts."""
    env = api_env
    org = await env.create_org("enr")
    code = await switch_on(env, org, ["10.50.0.0/16"])
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(Site).where(Site.id == org).values(egress_networks=["10.60.0.0/16"])
        )
        await s.commit()
    async with AsyncSession(env.admin_engine) as s:
        copy = (
            await s.execute(select(InstallLink.networks).where(InstallLink.site_id == org))
        ).scalar_one()
    assert [str(n) for n in copy] == ["10.50.0.0/16"]

    assert (await _join(env, code, "10.50.0.9")).status_code == 404
    assert (await _join(env, code, "10.60.0.9")).status_code == 201


async def test_switching_a_link_off_keeps_the_sites_networks(api_env) -> None:
    """M2: `networks` is optional on the link's PUT, and absent means unchanged
    -- the networks are the site's, and switching its link off is not an
    instruction to forget where the office is."""
    env = api_env
    org = await env.create_org("enr")
    await switch_on(env, org, ["10.50.0.0/16", "10.51.0.0/16"])
    headers = await env.headers(org, await env.create_user(org, "org-admin"))
    off = await env.client.put(link_path(org), json={"enabled": False}, headers=headers)
    assert off.status_code == 200, off.text
    assert off.json()["networks"] == ["10.50.0.0/16", "10.51.0.0/16"]
    async with AsyncSession(env.admin_engine) as s:
        site = (await s.execute(select(Site).where(Site.id == org))).scalar_one()
    assert [str(n) for n in site.egress_networks] == ["10.50.0.0/16", "10.51.0.0/16"]

    # And back on with no list: the site's networks are still there to use.
    on = await env.client.put(link_path(org), json={"enabled": True}, headers=headers)
    assert on.status_code == 200, on.text
    assert on.json()["enabled"] is True


async def test_the_hourly_limit_counts_each_sites_own_joins(api_env) -> None:
    """M5: one link per site, so one limit per site. The Principal site's link
    used up does not refuse a machine joining another site's link.

    BOTH SITES ARE THE INTERNAL CLIENT'S, deliberately: a link enrollment's
    session sees its own client only, so with two clients RLS would keep the
    counts apart whatever the query said. Within one client only the query's
    own site predicate does."""
    env = api_env
    org = await env.create_org("enr")
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        branch_site = Site(org_id=org, client_id=org, name="Sucursal")
        s.add(branch_site)
        await s.commit()
        site_id = branch_site.id
    principal = await switch_on(env, org, ["10.50.0.0/16"], hourly_limit=1)
    headers = await env.headers(org, await env.create_user(org, "org-admin"))
    other = await env.client.put(
        link_path(site_id),
        json={"enabled": True, "networks": ["10.60.0.0/16"], "hourly_limit": 1},
        headers=headers,
    )
    assert other.status_code == 200, other.text
    branch = other.json()["url"].rsplit("/i/", 1)[1]

    assert (await _join(env, principal, "10.50.0.9")).status_code == 201
    assert (await _join(env, principal, "10.50.0.10")).status_code == 429
    assert (await _join(env, branch, "10.60.0.9")).status_code == 201
    assert (await _join(env, branch, "10.60.0.10")).status_code == 429


async def test_the_switch_off_beat_records_every_due_link_of_an_organisation(
    api_env,
) -> None:
    """M5: with a link per site an organisation can have several links past
    their switch-off at once; each is switched off and audited on its own."""
    from app.core.client_scope import ClientScope
    from app.core.tenancy import org_session
    from app.services import install_link as svc

    env = api_env
    org = await env.create_org("enr")
    _, site_id = await _customer_with_site(env, org)
    when = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    await switch_on(env, org, ["10.50.0.0/16"], disable_at=when)
    headers = await env.headers(org, await env.create_user(org, "org-admin"))
    saved = await env.client.put(
        link_path(site_id),
        json={"enabled": True, "networks": ["10.60.0.0/16"], "disable_at": when},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    passed = datetime.now(UTC) - timedelta(minutes=1)
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(InstallLink).where(InstallLink.org_id == org).values(disable_at=passed)
        )
        await s.commit()

    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as session:
        assert await svc.switch_off_due(session, org, datetime.now(UTC)) == 2

    async with AsyncSession(env.admin_engine) as s:
        links = (
            await s.execute(select(InstallLink.enabled).where(InstallLink.org_id == org))
        ).scalars().all()
        audited = (
            await s.execute(
                select(AuditEvent.target_id).where(
                    AuditEvent.org_id == org, AuditEvent.action == "enrollment.link_disabled"
                )
            )
        ).scalars().all()
    assert links == [False, False]
    assert sorted(audited) == sorted([str(org), str(site_id)])
