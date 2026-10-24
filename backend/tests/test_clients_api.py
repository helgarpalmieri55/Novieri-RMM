"""The API for clients, sites and contacts (MSP clients spec §1, §2.1, §3.2, §4; Task 9).

A client outside the request's scope is a 404, never a 403 (spec §3.2): RLS
hides it. Creating and archiving a client need `clients.manage` on EVERY
client (`require_permission_on_every_client`); editing one, and managing its
sites and contacts, need the code on that client.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditEvent,
    Client,
    Device,
    EnrollmentToken,
    InstallLink,
    Permission,
    Role,
    RolePermission,
    Site,
    User,
    UserRole,
)
from tests.client_scope_support import EVERY_CLIENT
from tests.install_link_fixtures import FACTS as LINK_FACTS
from tests.install_link_fixtures import link_path, make_csr_pem, peer_client
from tests.msi_fixtures import StoreWith

TOKEN_FACTS = {
    "hostname": "wks-cli",
    "os_family": "linux",
    "os_version": "Ubuntu 24.04",
    "architecture": "x86_64",
}
NETWORK = "10.50.0.0/16"
INSIDE = "10.50.0.9"


async def _admin(env):
    org = await env.create_org("cli")
    sub = await env.create_user(org, "org-admin")
    return org, await env.headers(org, sub)


async def _granted_on(env, org: UUID, client_id: UUID | str, role: str = "org-admin"):
    """Headers of a person holding a built-in role on ONE client."""
    sub = await env.create_user(org)
    async with AsyncSession(env.admin_engine) as s:
        user = (await s.execute(select(User).where(User.external_subject == sub))).scalar_one()
        role_row = (
            await s.execute(select(Role).where(Role.name == role, Role.org_id.is_(None)))
        ).scalar_one()
        s.add(
            UserRole(
                org_id=org, user_id=user.id, role_id=role_row.id, client_id=UUID(str(client_id))
            )
        )
        await s.commit()
    return await env.headers(org, sub)


async def _create(env, headers, name: str, **extra: Any) -> str:
    r = await env.client.post("/api/v1/clients", json={"name": name, **extra}, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _principal(env, headers, client_id: str) -> str:
    sites = (await env.client.get(f"/api/v1/clients/{client_id}/sites", headers=headers)).json()
    return str(next(s["id"] for s in sites if s["name"] == "Principal"))


async def _audits(env, org: UUID, action: str) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def _mint_token(env, headers, site_id: str) -> tuple[str, str]:
    r = await env.client.post(
        "/api/v1/enrollment-tokens",
        json={"name": "x", "max_uses": 5, "expires_in_hours": 1, "site_id": site_id},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"]), str(r.json()["token"])


async def _switch_on(env, headers, site_id: str) -> str:
    r = await env.client.put(
        link_path(UUID(site_id)),
        json={"enabled": True, "networks": [NETWORK], "hourly_limit": 50},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return str(r.json()["url"]).rsplit("/i/", 1)[1]


async def _enroll_with(env, token: str) -> int:
    r = await env.client.post(
        "/enroll/v1",
        json={"token": token, "csr_pem": make_csr_pem(), "device_facts": TOKEN_FACTS},
    )
    return r.status_code


async def _join(env, code: str) -> int:
    async with peer_client(env, INSIDE) as peer:
        r = await peer.post(
            "/enroll/v1",
            json={"link_code": code, "csr_pem": make_csr_pem(), "device_facts": LINK_FACTS},
        )
    return r.status_code


async def _device(env, org: UUID, client_id: UUID | str, site_id: UUID | str, **extra: Any) -> None:
    async with AsyncSession(env.admin_engine) as s:
        s.add(
            Device(
                org_id=org,
                client_id=UUID(str(client_id)),
                site_id=UUID(str(site_id)),
                hostname="pc1",
                **extra,
            )
        )
        await s.commit()


# --------------------------------------------------------------------------- #
# The brief's tests
# --------------------------------------------------------------------------- #


async def test_create_list_and_principal_site(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    r = await env.client.post("/api/v1/clients", json={"name": "Acme"}, headers=h)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    sites = (await env.client.get(f"/api/v1/clients/{cid}/sites", headers=h)).json()
    assert [s["name"] for s in sites] == ["Principal"]
    names = {c["name"] for c in (await env.client.get("/api/v1/clients", headers=h)).json()}
    assert {"Acme", "Sin asignar"} <= names


async def test_duplicate_email_domain_is_409(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    assert (
        await env.client.post(
            "/api/v1/clients", json={"name": "A", "email_domains": ["a.co"]}, headers=h
        )
    ).status_code == 201
    r = await env.client.post(
        "/api/v1/clients", json={"name": "B", "email_domains": ["A.co"]}, headers=h
    )
    assert r.status_code == 409


async def test_cannot_archive_system_clients(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    assert (await env.client.post(f"/api/v1/clients/{org}/archive", headers=h)).status_code == 409
    async with AsyncSession(env.admin_engine) as s:
        unassigned = (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()
    r = await env.client.post(f"/api/v1/clients/{unassigned}/archive", headers=h)
    assert r.status_code == 409


async def test_cannot_delete_a_site_with_devices(api_env) -> None:
    """A CUSTOMER's site holding a device (review M5): the internal client's
    Principal site is refused on its own rule, which has its own test
    (`test_the_internal_clients_principal_site_is_never_archived`), so it
    cannot stand in for this one."""
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Acme")
    site = await _principal(env, h, cid)
    await _device(env, org, cid, site)
    assert (await env.client.delete(f"/api/v1/sites/{site}", headers=h)).status_code == 409


async def test_a_restricted_technician_gets_404_for_another_client(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    y = await _create(env, h, "Y")
    hx = await _granted_on(env, org, x)
    assert (await env.client.get(f"/api/v1/clients/{y}", headers=hx)).status_code == 404
    assert (await env.client.get(f"/api/v1/clients/{x}", headers=hx)).status_code == 200
    listed = {c["id"] for c in (await env.client.get("/api/v1/clients", headers=hx)).json()}
    assert listed == {x}


async def test_x_client_id_narrows_and_never_widens(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    narrowed = (await env.client.get("/api/v1/clients", headers={**h, "X-Client-Id": x})).json()
    assert [c["id"] for c in narrowed] == [x]
    bad = await env.client.get("/api/v1/clients", headers={**h, "X-Client-Id": "nope"})
    assert bad.status_code == 400


async def test_contacts_are_users_of_kind_contact(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    r = await env.client.post(
        f"/api/v1/clients/{x}/contacts",
        json={"email": "ana@x.co", "display_name": "Ana"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "contact"


def test_the_apis_optional_modules_are_the_models() -> None:
    """The request schema's closed list and the model's are one list (spec §1.4)."""
    from typing import get_args

    from app.api.v1.clients import OptionalModule
    from app.models.clients import OPTIONAL_MODULES

    assert get_args(OptionalModule) == OPTIONAL_MODULES


# --------------------------------------------------------------------------- #
# Permissions (spec §2.1)
# --------------------------------------------------------------------------- #


async def test_the_new_codes_are_seeded_to_the_built_in_roles(api_env) -> None:
    """`org-admin` and `super-admin` hold all three; every other built-in staff
    role that reads devices reads clients too, and manages nothing."""
    async with AsyncSession(api_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(Role.name, Permission.code)
                .join(RolePermission, RolePermission.role_id == Role.id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(
                    Role.org_id.is_(None),
                    Permission.code.in_(["clients.read", "clients.manage", "sites.manage"]),
                )
            )
        ).all()
    grants: dict[str, set[str]] = {}
    for role, code in rows:
        grants.setdefault(role, set()).add(code)
    every = {"clients.read", "clients.manage", "sites.manage"}
    assert grants == {
        "super-admin": every,
        "org-admin": every,
        "technician": {"clients.read"},
        "auditor": {"clients.read"},
        "read-only": {"clients.read"},
    }


async def test_a_technician_reads_clients_and_manages_none(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    tech = await env.headers(org, await env.create_user(org, "technician"))
    assert (await env.client.get(f"/api/v1/clients/{x}", headers=tech)).status_code == 200
    assert (await env.client.get(f"/api/v1/clients/{x}/sites", headers=tech)).status_code == 200
    refused = [
        await env.client.post("/api/v1/clients", json={"name": "Z"}, headers=tech),
        await env.client.patch(f"/api/v1/clients/{x}", json={"notes": "n"}, headers=tech),
        await env.client.post(f"/api/v1/clients/{x}/sites", json={"name": "S"}, headers=tech),
        await env.client.post(
            f"/api/v1/clients/{x}/contacts", json={"email": "a@x.co"}, headers=tech
        ),
    ]
    assert [r.status_code for r in refused] == [403, 403, 403, 403]


async def test_a_per_client_technician_edits_their_client_and_nothing_else(api_env) -> None:
    """Addendum: `clients.manage` on X only. PATCH X is theirs; Y does not
    exist for them; creating and archiving need every client."""
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    y = await _create(env, h, "Y")
    hx = await _granted_on(env, org, x)

    edited = await env.client.patch(
        f"/api/v1/clients/{x}", json={"name": "X renamed", "phone": "+57 1"}, headers=hx
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["name"], edited.json()["phone"]) == ("X renamed", "+57 1")

    assert (
        await env.client.patch(f"/api/v1/clients/{y}", json={"notes": "n"}, headers=hx)
    ).status_code == 404
    assert (await env.client.get(f"/api/v1/clients/{y}", headers=hx)).status_code == 404
    assert (await env.client.get(f"/api/v1/clients/{y}/sites", headers=hx)).status_code == 404
    assert (await env.client.get(f"/api/v1/clients/{y}/contacts", headers=hx)).status_code == 404

    created = await env.client.post("/api/v1/clients", json={"name": "Z"}, headers=hx)
    assert created.status_code == 403
    assert created.json()["detail"] == EVERY_CLIENT
    archived = await env.client.post(f"/api/v1/clients/{x}/archive", headers=hx)
    assert archived.status_code == 403
    assert archived.json()["detail"] == EVERY_CLIENT

    # Narrowing never widens: the every-client admin narrowed to X cannot create.
    narrowed = await env.client.post(
        "/api/v1/clients", json={"name": "Z"}, headers={**h, "X-Client-Id": x}
    )
    assert narrowed.status_code == 403
    assert narrowed.json()["detail"] == EVERY_CLIENT


async def test_a_client_header_outside_the_scope_lists_nothing_and_404s(api_env) -> None:
    """Spec §5 test 6, on the clients API."""
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    y = await _create(env, h, "Y")
    hx = await _granted_on(env, org, x)
    outside = {**hx, "X-Client-Id": y}
    assert (await env.client.get("/api/v1/clients", headers=outside)).json() == []
    assert (await env.client.get(f"/api/v1/clients/{y}", headers=outside)).status_code == 404
    assert (await env.client.get(f"/api/v1/clients/{x}", headers=outside)).status_code == 404


async def test_the_unassigned_client_is_listed_only_for_every_client(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    every = {c["kind"] for c in (await env.client.get("/api/v1/clients", headers=h)).json()}
    assert every == {"internal", "unassigned", "customer"}
    narrowed = await env.client.get("/api/v1/clients", headers={**h, "X-Client-Id": x})
    assert {c["kind"] for c in narrowed.json()} == {"customer"}


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #


async def test_a_client_carries_its_fields_and_counts(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    r = await env.client.post(
        "/api/v1/clients",
        json={
            "name": "  Acme  ",
            "email_domains": [" Acme.CO ", "acme.co", "mail.acme.co"],
            "phone": "+57 1 555",
            "address": "Calle 1",
            "website": "https://acme.co",
            "notes": "VIP",
            "enabled_modules": ["budget", "bcp"],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    cid = body["id"]
    assert body == {
        "id": cid,
        "name": "Acme",
        "kind": "customer",
        "status": "active",
        "email_domains": ["acme.co", "mail.acme.co"],
        "phone": "+57 1 555",
        "address": "Calle 1",
        "website": "https://acme.co",
        "notes": "VIP",
        "enabled_modules": ["bcp", "budget"],
        "device_count": 0,
        "site_count": 1,
    }
    principal = await _principal(env, h, cid)
    await _device(env, org, cid, principal)
    got = (await env.client.get(f"/api/v1/clients/{cid}", headers=h)).json()
    assert (got["device_count"], got["site_count"]) == (1, 1)


async def test_a_client_is_validated(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    for body in (
        {"name": ""},
        {"name": "A", "enabled_modules": ["imaging"]},
        {"name": "A", "email_domains": ["not a domain"]},
        {"name": "A", "email_domains": ["@acme.co"]},
        {"name": "A", "kind": "internal"},
        {"name": "A", "status": "archived"},
    ):
        r = await env.client.post("/api/v1/clients", json=body, headers=h)
        assert r.status_code == 422, (body, r.text)


async def test_a_client_name_is_unique_in_the_organization(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    await _create(env, h, "Acme")
    dup = await env.client.post("/api/v1/clients", json={"name": "ACME"}, headers=h)
    assert dup.status_code == 409, dup.text
    other = await _create(env, h, "Other")
    renamed = await env.client.patch(f"/api/v1/clients/{other}", json={"name": "acme"}, headers=h)
    assert renamed.status_code == 409, renamed.text


async def test_a_domain_another_client_holds_is_409_even_where_it_is_invisible(api_env) -> None:
    """Spec §1.1/§4: a domain belongs to one client of the organization. A
    technician limited to X cannot see Y's domains, so the application's check
    misses them; the database refuses the duplicate all the same."""
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X", email_domains=["x.co"])
    await _create(env, h, "Y", email_domains=["y.co"])
    hx = await _granted_on(env, org, x)
    taken = await env.client.patch(
        f"/api/v1/clients/{x}", json={"email_domains": ["x.co", "Y.co"]}, headers=hx
    )
    assert taken.status_code == 409, taken.text
    own = await env.client.patch(
        f"/api/v1/clients/{x}", json={"email_domains": ["x.co", "x2.co"]}, headers=hx
    )
    assert own.status_code == 200, own.text
    assert own.json()["email_domains"] == ["x.co", "x2.co"]


async def test_client_writes_are_audited(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Acme")
    r = await env.client.patch(
        f"/api/v1/clients/{cid}", json={"notes": "n", "enabled_modules": ["change"]}, headers=h
    )
    assert r.status_code == 200, r.text
    assert (await env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)).status_code == 200

    created = await _audits(env, org, "client.created")
    assert [(e.target_type, e.target_id) for e in created] == [("client", cid)]
    principal = await _audits(env, org, "client.site_created")
    assert [e.meta["client_id"] for e in principal] == [cid]
    updated = await _audits(env, org, "client.updated")
    assert [(e.target_id, sorted(e.meta["changed"])) for e in updated] == [
        (cid, ["enabled_modules", "notes"])
    ]
    assert [e.target_id for e in await _audits(env, org, "client.archived")] == [cid]


async def test_archived_clients_are_listed_only_on_request(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Gone")
    archived = await env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    default = {c["id"] for c in (await env.client.get("/api/v1/clients", headers=h)).json()}
    assert cid not in default
    every = await env.client.get("/api/v1/clients?include_archived=true", headers=h)
    assert cid in {c["id"] for c in every.json()}
    # Still readable, no longer editable; archiving twice is a conflict.
    assert (await env.client.get(f"/api/v1/clients/{cid}", headers=h)).status_code == 200
    edit = await env.client.patch(f"/api/v1/clients/{cid}", json={"notes": "n"}, headers=h)
    assert edit.status_code == 409
    assert (await env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)).status_code == 409


async def test_a_client_with_active_devices_is_not_archived(api_env) -> None:
    """Spec §4: 409 while it has active devices. Retired first, then archived."""
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Busy")
    site = await _principal(env, h, cid)
    await _device(env, org, cid, site)
    refused = await env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)
    assert refused.status_code == 409, refused.text
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(Device).where(Device.client_id == UUID(cid)).values(lifecycle="retired")
        )
        await s.commit()
    assert (await env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)).status_code == 200


async def test_archiving_a_client_revokes_its_tokens_and_switches_off_its_links(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Leaving")
    site = await _principal(env, h, cid)
    token_id, token = await _mint_token(env, h, site)
    code = await _switch_on(env, h, site)
    # Another client's live token and link are not touched.
    other = await _create(env, h, "Staying")
    other_site = await _principal(env, h, other)
    other_token_id, _ = await _mint_token(env, h, other_site)
    await _switch_on(env, h, other_site)

    assert (await env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)).status_code == 200

    async with AsyncSession(env.admin_engine) as s:
        revoked = dict((await s.execute(select(EnrollmentToken.id, EnrollmentToken.revoked))).all())
        links = dict(
            (
                await s.execute(
                    select(InstallLink.site_id, InstallLink.enabled).where(
                        InstallLink.org_id == org
                    )
                )
            ).all()
        )
    assert revoked[UUID(token_id)] is True
    assert revoked[UUID(other_token_id)] is False
    assert links == {UUID(site): False, UUID(other_site): True}
    token_audit = await _audits(env, org, "enrollment.token_revoked")
    assert [(e.target_id, e.meta["reason"]) for e in token_audit] == [(token_id, "client_archived")]
    link_audit = await _audits(env, org, "enrollment.link_disabled")
    assert [(e.target_id, e.meta["reason"]) for e in link_audit] == [(site, "client_archived")]

    assert await _enroll_with(env, token) == 403
    assert await _join(env, code) == 404


async def test_enrollment_refuses_an_archived_client_even_with_a_live_token_or_link(
    api_env, monkeypatch
) -> None:
    """Addendum decision 4: a token or link that predates the archive -- here
    the client is archived behind the API's back, so both stay live -- enrols
    nothing, and the MSI is not built for it."""
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Leaving")
    site = await _principal(env, h, cid)
    _, token = await _mint_token(env, h, site)
    code = await _switch_on(env, h, site)
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(update(Client).where(Client.id == UUID(cid)).values(status="archived"))
        await s.commit()

    assert await _enroll_with(env, token) == 403
    assert await _join(env, code) == 404
    monkeypatch.setattr(env.app.state.settings, "public_console_url", "https://itms.example.com")
    env.app.state.object_store = StoreWith({})
    msi = await env.client.post(
        link_path(UUID(site), "/msi"), json={"code": code, "arch": "amd64"}, headers=h
    )
    assert msi.status_code == 409, msi.text
    assert msi.json()["detail"] == "that client is archived"


async def test_enrollment_refuses_an_archived_site_even_with_a_live_token_or_link(
    api_env,
) -> None:
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Moving")
    site = await _principal(env, h, cid)
    _, token = await _mint_token(env, h, site)
    code = await _switch_on(env, h, site)
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(Site).where(Site.id == UUID(site)).values(archived_at=Site.created_at)
        )
        await s.commit()
    assert await _enroll_with(env, token) == 403
    assert await _join(env, code) == 404


# --------------------------------------------------------------------------- #
# Sites
# --------------------------------------------------------------------------- #


async def test_sites_are_created_edited_and_validated(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Acme")
    r = await env.client.post(
        f"/api/v1/clients/{cid}/sites",
        json={"name": "Medellín", "address": "Calle 2", "description": "branch"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    site = r.json()
    assert (site["client_id"], site["name"], site["egress_networks"], site["archived_at"]) == (
        cid,
        "Medellín",
        [],
        None,
    )
    dup = await env.client.post(
        f"/api/v1/clients/{cid}/sites", json={"name": "medellín"}, headers=h
    )
    assert dup.status_code == 409, dup.text

    edited = await env.client.patch(
        f"/api/v1/sites/{site['id']}", json={"name": "Medellín Centro"}, headers=h
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["name"] == "Medellín Centro"
    # The egress networks decide who may enrol through the site's install
    # link: they are written there, under `enrollment.link.manage`, never here.
    nets = await env.client.patch(
        f"/api/v1/sites/{site['id']}", json={"egress_networks": ["0.0.0.0/1"]}, headers=h
    )
    assert nets.status_code == 422
    moved = await env.client.patch(
        f"/api/v1/sites/{site['id']}", json={"client_id": str(org)}, headers=h
    )
    assert moved.status_code == 422

    assert [e.target_id for e in await _audits(env, org, "client.site_updated")] == [site["id"]]
    assert site["id"] in [e.target_id for e in await _audits(env, org, "client.site_created")]


async def test_the_unassigned_and_archived_clients_take_no_site(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    async with AsyncSession(env.admin_engine) as s:
        unassigned = (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()
    r = await env.client.post(f"/api/v1/clients/{unassigned}/sites", json={"name": "S"}, headers=h)
    assert r.status_code == 422, r.text
    gone = await _create(env, h, "Gone")
    assert (await env.client.post(f"/api/v1/clients/{gone}/archive", headers=h)).status_code == 200
    r = await env.client.post(f"/api/v1/clients/{gone}/sites", json={"name": "S"}, headers=h)
    assert r.status_code == 409, r.text


async def test_archiving_a_site_hides_it_and_disables_its_token_and_link(api_env) -> None:
    """Addendum decision 3: DELETE archives -- tokens and install links name
    the site, so a hard delete would destroy their history."""
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Acme")
    principal = await _principal(env, h, cid)
    branch = (
        await env.client.post(f"/api/v1/clients/{cid}/sites", json={"name": "Sucursal"}, headers=h)
    ).json()["id"]
    token_id, token = await _mint_token(env, h, branch)
    code = await _switch_on(env, h, branch)
    principal_token, _ = await _mint_token(env, h, principal)

    gone = await env.client.delete(f"/api/v1/sites/{branch}", headers=h)
    assert gone.status_code == 200, gone.text
    assert gone.json()["archived_at"] is not None

    listed = (await env.client.get(f"/api/v1/clients/{cid}/sites", headers=h)).json()
    assert [s["id"] for s in listed] == [principal]
    every = await env.client.get(f"/api/v1/clients/{cid}/sites?include_archived=true", headers=h)
    assert {s["id"] for s in every.json()} == {principal, branch}
    assert (await env.client.get(f"/api/v1/clients/{cid}", headers=h)).json()["site_count"] == 1

    async with AsyncSession(env.admin_engine) as s:
        revoked = dict((await s.execute(select(EnrollmentToken.id, EnrollmentToken.revoked))).all())
        link = (
            await s.execute(select(InstallLink.enabled).where(InstallLink.site_id == UUID(branch)))
        ).scalar_one()
    assert (revoked[UUID(token_id)], revoked[UUID(principal_token)], link) == (True, False, False)
    assert [e.target_id for e in await _audits(env, org, "client.site_archived")] == [branch]
    assert [
        (e.target_id, e.meta["reason"]) for e in await _audits(env, org, "enrollment.token_revoked")
    ] == [(token_id, "site_archived")]
    assert [
        (e.target_id, e.meta["reason"]) for e in await _audits(env, org, "enrollment.link_disabled")
    ] == [(branch, "site_archived")]
    assert await _enroll_with(env, token) == 403
    assert await _join(env, code) == 404

    again = await env.client.delete(f"/api/v1/sites/{branch}", headers=h)
    assert again.status_code == 409
    edit = await env.client.patch(f"/api/v1/sites/{branch}", json={"name": "Z"}, headers=h)
    assert edit.status_code == 409


async def test_a_site_with_devices_is_not_archived(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Acme")
    branch = (
        await env.client.post(f"/api/v1/clients/{cid}/sites", json={"name": "Sucursal"}, headers=h)
    ).json()["id"]
    await _device(env, org, cid, branch, lifecycle="retired")
    r = await env.client.delete(f"/api/v1/sites/{branch}", headers=h)
    assert r.status_code == 409, r.text


async def test_the_internal_clients_principal_site_is_never_archived(api_env) -> None:
    """Its id is the organization's, and the phase-1 fallbacks (IPAM,
    provisioning) file rows under it."""
    env = api_env
    org, h = await _admin(env)
    r = await env.client.delete(f"/api/v1/sites/{org}", headers=h)
    assert r.status_code == 409, r.text
    # A customer's Principal is an ordinary site.
    cid = await _create(env, h, "Acme")
    principal = await _principal(env, h, cid)
    assert (await env.client.delete(f"/api/v1/sites/{principal}", headers=h)).status_code == 200


async def test_a_per_client_technician_manages_only_their_clients_sites(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    y = await _create(env, h, "Y")
    y_site = await _principal(env, h, y)
    hx = await _granted_on(env, org, x)
    mine = await env.client.post(f"/api/v1/clients/{x}/sites", json={"name": "S"}, headers=hx)
    assert mine.status_code == 201, mine.text
    assert (
        await env.client.patch(
            f"/api/v1/sites/{mine.json()['id']}", json={"name": "S2"}, headers=hx
        )
    ).status_code == 200
    assert (
        await env.client.post(f"/api/v1/clients/{y}/sites", json={"name": "S"}, headers=hx)
    ).status_code == 404
    assert (
        await env.client.patch(f"/api/v1/sites/{y_site}", json={"name": "Z"}, headers=hx)
    ).status_code == 404
    assert (await env.client.delete(f"/api/v1/sites/{y_site}", headers=hx)).status_code == 404
    assert (
        await env.client.delete(f"/api/v1/sites/{mine.json()['id']}", headers=hx)
    ).status_code == 200


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #


async def test_a_contact_is_listed_edited_and_disabled_by_its_clients_technician(api_env) -> None:
    """Addendum decision 1: X's technician deactivates X's contact."""
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    hx = await _granted_on(env, org, x)
    created = await env.client.post(
        f"/api/v1/clients/{x}/contacts",
        json={"email": " Ana@X.co ", "display_name": "Ana", "phone": "+57 300"},
        headers=hx,
    )
    assert created.status_code == 201, created.text
    contact = created.json()
    assert {k: contact[k] for k in ("client_id", "email", "display_name", "phone", "status")} == {
        "client_id": x,
        "email": "ana@x.co",
        "display_name": "Ana",
        "phone": "+57 300",
        "status": "active",
    }
    listed = (await env.client.get(f"/api/v1/clients/{x}/contacts", headers=hx)).json()
    assert [c["id"] for c in listed] == [contact["id"]]

    disabled = await env.client.patch(
        f"/api/v1/contacts/{contact['id']}",
        json={"status": "disabled", "display_name": "Ana María"},
        headers=hx,
    )
    assert disabled.status_code == 200, disabled.text
    assert (disabled.json()["status"], disabled.json()["display_name"]) == ("disabled", "Ana María")
    async with AsyncSession(env.admin_engine) as s:
        row = (await s.execute(select(User).where(User.id == UUID(contact["id"])))).scalar_one()
    assert (row.kind, row.client_id, row.status) == ("contact", UUID(x), "disabled")
    # No login identity yet (spec §1.3): a placeholder no token's `sub` matches.
    assert row.external_subject.startswith("contact-pending:")

    assert [e.target_id for e in await _audits(env, org, "contact.created")] == [contact["id"]]
    updates = await _audits(env, org, "contact.updated")
    assert [(e.target_id, sorted(e.meta["changed"])) for e in updates] == [
        (contact["id"], ["display_name", "status"])
    ]


async def test_a_contact_patch_never_touches_identity(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    contact = (
        await env.client.post(f"/api/v1/clients/{x}/contacts", json={"email": "a@x.co"}, headers=h)
    ).json()
    for body in (
        {"kind": "staff"},
        {"client_id": str(org)},
        {"external_subject": "someone"},
        {"status": "deleted"},
        {"email": "not-an-address"},
    ):
        r = await env.client.patch(f"/api/v1/contacts/{contact['id']}", json=body, headers=h)
        assert r.status_code == 422, (body, r.text)


async def test_another_clients_contact_and_a_staff_row_are_not_contacts(api_env) -> None:
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    y = await _create(env, h, "Y")
    y_contact = (
        await env.client.post(f"/api/v1/clients/{y}/contacts", json={"email": "b@y.co"}, headers=h)
    ).json()["id"]
    hx = await _granted_on(env, org, x)
    r = await env.client.patch(
        f"/api/v1/contacts/{y_contact}", json={"status": "disabled"}, headers=hx
    )
    assert r.status_code == 404
    assert (
        await env.client.post(f"/api/v1/clients/{y}/contacts", json={"email": "c@y.co"}, headers=hx)
    ).status_code == 404
    async with AsyncSession(env.admin_engine) as s:
        staff = (
            (await s.execute(select(User.id).where(User.org_id == org, User.kind == "staff")))
            .scalars()
            .first()
        )
    r = await env.client.patch(f"/api/v1/contacts/{staff}", json={"display_name": "x"}, headers=h)
    assert r.status_code == 404


async def test_contacts_are_refused_on_the_unassigned_client_and_duplicated_emails(
    api_env,
) -> None:
    env = api_env
    org, h = await _admin(env)
    async with AsyncSession(env.admin_engine) as s:
        unassigned = (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()
    r = await env.client.post(
        f"/api/v1/clients/{unassigned}/contacts", json={"email": "a@x.co"}, headers=h
    )
    assert r.status_code == 422, r.text
    x = await _create(env, h, "X")
    y = await _create(env, h, "Y")
    first = await env.client.post(
        f"/api/v1/clients/{x}/contacts", json={"email": "a@x.co"}, headers=h
    )
    assert first.status_code == 201
    dup = await env.client.post(
        f"/api/v1/clients/{x}/contacts", json={"email": "A@x.co"}, headers=h
    )
    assert dup.status_code == 409, dup.text
    # A DELIBERATE RULE CHANGE (Task 9 review 1, I1), not a weakening: the
    # same address may no longer be a contact of two clients. Inbound mail
    # (spec §3.3 step 1) files a message under its sender's client, and an
    # address held twice quarantines every message from it.
    elsewhere = await env.client.post(
        f"/api/v1/clients/{y}/contacts", json={"email": "a@x.co"}, headers=h
    )
    assert elsewhere.status_code == 409, elsewhere.text


# --------------------------------------------------------------------------- #
# Task 9 review 1
# --------------------------------------------------------------------------- #

ADDRESS_TAKEN = "that email address already belongs to another user of the organization"


async def test_a_contact_cannot_take_a_staff_members_email(api_env) -> None:
    """I1: a contact's address may not be any other user's in the
    organisation -- a technician's included, whose mail would otherwise be
    quarantined as ambiguous (`email_intake/identity.py`)."""
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    staff = await env.create_user(org, "technician")
    hx = await _granted_on(env, org, x)
    for headers in (h, hx):
        r = await env.client.post(
            f"/api/v1/clients/{x}/contacts", json={"email": f"{staff}@TEST.local"}, headers=headers
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == ADDRESS_TAKEN


async def test_a_contact_cannot_take_an_unseen_clients_contacts_email(api_env) -> None:
    """I1: a technician limited to X cannot see Y's contacts, so the
    application's check misses them; the database (`its_contact_email_unique`,
    migration 0232) refuses the address all the same, on create and on edit."""
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    y = await _create(env, h, "Y")
    theirs = await env.client.post(
        f"/api/v1/clients/{y}/contacts", json={"email": "bea@y.co"}, headers=h
    )
    assert theirs.status_code == 201, theirs.text
    hx = await _granted_on(env, org, x)
    created = await env.client.post(
        f"/api/v1/clients/{x}/contacts", json={"email": "BEA@y.co"}, headers=hx
    )
    assert created.status_code == 409, created.text
    assert created.json()["detail"] == ADDRESS_TAKEN
    mine = await env.client.post(
        f"/api/v1/clients/{x}/contacts", json={"email": "cam@x.co"}, headers=hx
    )
    assert mine.status_code == 201, mine.text
    edited = await env.client.patch(
        f"/api/v1/contacts/{mine.json()['id']}", json={"email": "bea@y.co"}, headers=hx
    )
    assert edited.status_code == 409, edited.text
    assert edited.json()["detail"] == ADDRESS_TAKEN


async def test_a_technician_signing_in_with_a_contacts_address_is_never_refused(
    api_env,
) -> None:
    """I1 is ONE-DIRECTIONAL: it holds a contact's address against every other
    user, never a staff row against a contact's. First-login provisioning
    (`app/api/deps.py::admit`) writes whatever address the identity provider
    asserts, and a sign-in must not fail on a contact's data."""
    env = api_env
    org, h = await _admin(env)
    x = await _create(env, h, "X")
    contact = await env.client.post(
        f"/api/v1/clients/{x}/contacts", json={"email": "dual@x.co"}, headers=h
    )
    assert contact.status_code == 201, contact.text
    slug = await env.org_slug(org)
    token = env.idp.token("sub-new-technician", slug, email="dual@x.co")
    me = await env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    async with AsyncSession(env.admin_engine) as s:
        kinds = sorted(
            (
                await s.execute(
                    select(User.kind).where(User.org_id == org, User.email == "dual@x.co")
                )
            )
            .scalars()
            .all()
        )
    assert kinds == ["contact", "staff"]


async def test_the_uniqueness_triggers_fire_after_rls(api_env) -> None:
    """M2: `its_client_domains_unique` and `its_contact_email_unique` are
    SECURITY DEFINER and read every row of an organisation. Fired BEFORE the
    row's RLS check, a raw INSERT naming ANOTHER organisation would answer
    "unique violation" or "row-level security" depending on that
    organisation's data -- an oracle across tenants. AFTER triggers never run
    for a row RLS refused."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.core.client_scope import ClientScope
    from app.core.tenancy import org_session

    env = api_env
    victim, hv = await _admin(env)
    await _create(env, hv, "Victim Co", email_domains=["victim.co"])
    victim_staff = await env.create_user(victim, "technician")
    attacker = await env.create_org("cli-attacker")
    probes = [
        (
            "INSERT INTO clients (id, org_id, name, kind, email_domains) "
            "VALUES (gen_random_uuid(), :o, 'probe', 'customer', ARRAY['victim.co'])",
            {"o": victim},
        ),
        (
            "INSERT INTO users (id, org_id, external_subject, email, kind, client_id) "
            "VALUES (gen_random_uuid(), :o, 'probe', :e, 'contact', :o)",
            {"o": victim, "e": f"{victim_staff}@test.local"},
        ),
    ]
    for sql, params in probes:
        try:
            async with org_session(
                attacker, clients=ClientScope.all(), engine=env.app_engine
            ) as session:
                await session.execute(text(sql), params)
        except DBAPIError as exc:
            assert "row-level security" in str(exc), str(exc)
        else:
            raise AssertionError(f"{sql} was not refused")
    async with env.admin_engine.connect() as conn:
        timing = dict(
            (
                await conn.execute(
                    text(
                        "SELECT tgname, (tgtype & 2) = 0 FROM pg_trigger "
                        "WHERE tgname IN ('its_client_domains_unique', 'its_contact_email_unique')"
                    )
                )
            ).all()
        )
    assert timing == {"its_client_domains_unique": True, "its_contact_email_unique": True}


async def test_an_archived_clients_contact_is_not_edited(api_env) -> None:
    """M4: as `create_contact` refuses an archived client, so does the edit --
    re-enabling a contact of a client nobody serves any more included."""
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Gone")
    contact = await env.client.post(
        f"/api/v1/clients/{cid}/contacts", json={"email": "eva@gone.co"}, headers=h
    )
    assert contact.status_code == 201, contact.text
    assert (await env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)).status_code == 200
    for body in ({"status": "active"}, {"display_name": "Eva"}):
        r = await env.client.patch(f"/api/v1/contacts/{contact.json()['id']}", json=body, headers=h)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "that client is archived"


async def test_the_unassigned_client_takes_no_domains_and_no_modules(api_env) -> None:
    """M6: unknown senders land on the unassigned client because no domain
    matched (spec §3.3), and it serves nobody, so an optional module there
    means nothing. Its name may still change."""
    env = api_env
    org, h = await _admin(env)
    async with AsyncSession(env.admin_engine) as s:
        unassigned = (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()
    domains = await env.client.patch(
        f"/api/v1/clients/{unassigned}", json={"email_domains": ["x.co"]}, headers=h
    )
    assert domains.status_code == 422, domains.text
    assert domains.json()["detail"] == "the unassigned client takes no email domains"
    modules = await env.client.patch(
        f"/api/v1/clients/{unassigned}", json={"enabled_modules": ["budget"]}, headers=h
    )
    assert modules.status_code == 422, modules.text
    assert modules.json()["detail"] == "the unassigned client has no optional modules"
    renamed = await env.client.patch(
        f"/api/v1/clients/{unassigned}",
        json={"name": "Sin cliente", "email_domains": [], "enabled_modules": []},
        headers=h,
    )
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["email_domains"], renamed.json()["enabled_modules"]) == ([], [])


async def test_archiving_a_client_switches_off_its_contacts(api_env) -> None:
    """Concern 3: an archived client's people do not keep access through the
    portal (project 2). Each active contact is disabled in the archive's own
    transaction and audited; one already disabled is left as it was."""
    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Leaving")
    on = (
        await env.client.post(
            f"/api/v1/clients/{cid}/contacts", json={"email": "on@l.co"}, headers=h
        )
    ).json()["id"]
    off = (
        await env.client.post(
            f"/api/v1/clients/{cid}/contacts", json={"email": "off@l.co"}, headers=h
        )
    ).json()["id"]
    assert (
        await env.client.patch(f"/api/v1/contacts/{off}", json={"status": "disabled"}, headers=h)
    ).status_code == 200
    other = await _create(env, h, "Staying")
    kept = (
        await env.client.post(
            f"/api/v1/clients/{other}/contacts", json={"email": "kept@s.co"}, headers=h
        )
    ).json()["id"]

    assert (await env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)).status_code == 200

    async with AsyncSession(env.admin_engine) as s:
        statuses = dict(
            (
                await s.execute(
                    select(User.id, User.status).where(
                        User.id.in_([UUID(on), UUID(off), UUID(kept)])
                    )
                )
            ).all()
        )
    assert statuses == {UUID(on): "disabled", UUID(off): "disabled", UUID(kept): "active"}
    archived = [e for e in await _audits(env, org, "contact.updated") if e.meta.get("reason")]
    assert [(e.target_id, e.meta["reason"], e.meta["changed"]) for e in archived] == [
        (on, "client_archived", {"status": {"before": "active", "after": "disabled"}})
    ]


async def _archive_racing_an_enrollment(env, *, site_archive: bool) -> int:
    """Concern 5. An enrollment that has consumed its token but not yet
    written its device holds the token's row lock. The archive must take that
    lock BEFORE it counts devices, so it waits, then counts the new device
    and refuses -- rather than counting zero, archiving, and leaving a machine
    in a client or site nobody serves.

    DETERMINISTIC (final review, T9R2 N3): the enrollment's transaction stays
    open until the archive is SEEN waiting on it, in the token lock
    (`pg_blocking_pids`), rather than for a fixed second that a slow runner
    might not give the archive. Returns the archive's HTTP status once the
    enrollment has committed."""
    import asyncio

    from sqlalchemy import update as sql_update

    from tests.race_helpers import backend_pid, normalised, until_blocked_by

    org, h = await _admin(env)
    cid = await _create(env, h, "Racing")
    site = await _principal(env, h, cid)
    if site_archive:
        site = (
            await env.client.post(
                f"/api/v1/clients/{cid}/sites", json={"name": "Sucursal"}, headers=h
            )
        ).json()["id"]
    token_id, _ = await _mint_token(env, h, site)
    async with AsyncSession(env.admin_engine) as enrolling:
        await enrolling.execute(
            sql_update(EnrollmentToken)
            .where(EnrollmentToken.id == UUID(token_id))
            .values(use_count=EnrollmentToken.use_count + 1)
        )
        url = f"/api/v1/sites/{site}" if site_archive else f"/api/v1/clients/{cid}/archive"
        method = "DELETE" if site_archive else "POST"
        archive = asyncio.create_task(env.client.request(method, url, headers=h))
        waiting = await until_blocked_by(env.app_engine, await backend_pid(enrolling))
        assert not archive.done()
        assert all(
            "FROM ENROLLMENT_TOKENS" in normalised(q) and "FOR UPDATE" in normalised(q)
            for q in waiting
        ), waiting
        enrolling.add(Device(org_id=org, client_id=UUID(cid), site_id=UUID(site), hostname="racer"))
        await enrolling.commit()
    response = await asyncio.wait_for(archive, timeout=30)
    return response.status_code


async def test_archiving_a_client_waits_for_an_enrollment_in_flight(api_env) -> None:
    assert await _archive_racing_an_enrollment(api_env, site_archive=False) == 409


async def test_archiving_a_site_waits_for_an_enrollment_in_flight(api_env) -> None:
    assert await _archive_racing_an_enrollment(api_env, site_archive=True) == 409



# --------------------------------------------------------------------------- #
# Archive races (final review, T9R2 N1/N4): the client's row, then the site's,
# is the one lock every writer of a new row and every archive meets first.
# Each test holds one side's transaction open until the other is SEEN waiting
# on it (`tests/race_helpers.py`), then commits, so the interleaving is the
# one named -- never whichever a scheduler happened to pick.
# --------------------------------------------------------------------------- #


async def _actor_id(env, org: UUID) -> UUID:
    sub = await env.create_user(org, "org-admin")
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _held_archive(env, org: UUID, cid: str):
    """Archive `cid` through the service in a transaction the caller keeps
    open: `async with _held_archive(...) as pid:` -- it commits on exit."""
    import contextlib
    from datetime import UTC, datetime

    from app.core.client_scope import ClientScope
    from app.core.tenancy import org_session
    from app.services.clients import archive_client
    from tests.race_helpers import backend_pid

    actor = await _actor_id(env, org)

    @contextlib.asynccontextmanager
    async def held():
        async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as session:
            await archive_client(
                session, org_id=org, actor_id=actor, client_id=UUID(cid), now=datetime.now(UTC)
            )
            yield await backend_pid(session)

    return held()


async def _contact(env, h, cid: str, email: str) -> str:
    r = await env.client.post(f"/api/v1/clients/{cid}/contacts", json={"email": email}, headers=h)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _status(env, user_id: str) -> str:
    async with AsyncSession(env.admin_engine) as s:
        return str(
            (await s.execute(select(User.status).where(User.id == UUID(user_id)))).scalar_one()
        )


async def test_a_contact_patched_during_an_archive_is_refused_not_re_enabled(api_env) -> None:
    """T9R2 N1. A switched-off contact; the archive is in flight and has not
    committed; a PATCH switching the contact back on waits on the CLIENT's
    row (it reads its client FOR SHARE before touching the contact), then
    sees the archive and is a 409. Without that lock the PATCH read "active",
    wrote the contact -- which the archive, having nothing to switch off, had
    not touched -- waited only on the audit chain, and committed an active
    contact of an archived client."""
    import asyncio

    from tests.race_helpers import normalised, until_blocked_by

    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Leaving")
    contact = await _contact(env, h, cid, "racer@leaving.co")
    off = await env.client.patch(
        f"/api/v1/contacts/{contact}", json={"status": "disabled"}, headers=h
    )
    assert off.status_code == 200, off.text

    async with await _held_archive(env, org, cid) as archiver:
        patch = asyncio.create_task(
            env.client.patch(
                f"/api/v1/contacts/{contact}", json={"status": "active"}, headers=h
            )
        )
        waiting = await until_blocked_by(env.app_engine, archiver)
        assert not patch.done()
        assert all(
            "FROM CLIENTS" in normalised(q) and "FOR SHARE" in normalised(q) for q in waiting
        ), waiting
    response = await asyncio.wait_for(patch, timeout=30)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "that client is archived"
    assert await _status(env, contact) == "disabled"


async def test_an_archive_waits_for_a_contact_patch_in_flight(api_env) -> None:
    """The other order. A PATCH holding its transaction open holds its
    client's row FOR SHARE; the archive waits on that row -- before any token,
    contact or audit lock, so no cycle can form (the 40P01 of T9R2 N1) -- and
    once the PATCH commits it archives and disables the contact."""
    import asyncio

    from app.core.client_scope import ClientScope
    from app.core.tenancy import org_session
    from app.services.clients import update_contact
    from tests.race_helpers import backend_pid, normalised, until_blocked_by

    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Leaving")
    contact = await _contact(env, h, cid, "editing@leaving.co")
    actor = await _actor_id(env, org)

    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as session:
        await update_contact(
            session,
            org_id=org,
            actor_id=actor,
            contact_id=UUID(contact),
            changes={"display_name": "Renamed mid-archive"},
        )
        archive = asyncio.create_task(
            env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)
        )
        waiting = await until_blocked_by(env.app_engine, await backend_pid(session))
        assert not archive.done()
        assert all(
            "FROM CLIENTS" in normalised(q) and "FOR NO KEY UPDATE" in normalised(q)
            for q in waiting
        ), waiting
    response = await asyncio.wait_for(archive, timeout=30)
    assert response.status_code == 200, response.text
    assert await _status(env, contact) == "disabled"


async def test_new_rows_filed_during_an_archive_are_refused(api_env) -> None:
    """T9R2 N1 and its sibling: every path that files a new row under a
    client reads that client FOR SHARE (`client_sites`), which waits for an
    archive in flight and then sees it. Without the lock each read "active",
    waited only on the audit chain, and committed a contact, a site, an
    enrollment token or a hand-typed asset into an archived client."""
    import asyncio

    from tests.race_helpers import normalised, until_blocked_by

    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Leaving")
    site = await _principal(env, h, cid)
    racers = {
        "contact": ("POST", f"/api/v1/clients/{cid}/contacts", {"email": "late@leaving.co"}),
        "site": ("POST", f"/api/v1/clients/{cid}/sites", {"name": "Late"}),
        "token": (
            "POST",
            "/api/v1/enrollment-tokens",
            {"name": "late", "max_uses": 1, "expires_in_hours": 1, "site_id": site},
        ),
        "asset": ("POST", "/api/v1/devices", {"description": "boxed laptop", "site_id": site}),
    }
    async with await _held_archive(env, org, cid) as archiver:
        tasks = {
            name: asyncio.create_task(env.client.request(method, url, json=body, headers=h))
            for name, (method, url, body) in racers.items()
        }
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while True:
            waiting = await until_blocked_by(env.app_engine, archiver)
            if len(waiting) == len(racers):
                break
            assert loop.time() < deadline, waiting
            await asyncio.sleep(0.02)
        assert not any(task.done() for task in tasks.values())
        assert all(
            "FROM CLIENTS" in normalised(q) and "FOR SHARE" in normalised(q) for q in waiting
        ), waiting
    for name, task in tasks.items():
        response = await asyncio.wait_for(task, timeout=30)
        assert response.status_code == 409, (name, response.text)
        assert response.json()["detail"] == "that client is archived", name
    async with AsyncSession(env.admin_engine) as s:
        late = (
            await s.execute(
                select(func.count()).select_from(User).where(User.email == "late@leaving.co")
            )
        ).scalar_one()
        tokens = (
            await s.execute(
                select(func.count())
                .select_from(EnrollmentToken)
                .where(EnrollmentToken.site_id == UUID(site), EnrollmentToken.revoked.is_(False))
            )
        ).scalar_one()
    assert (late, tokens) == (0, 0)


async def test_a_token_minted_before_an_archive_is_revoked_by_it(api_env) -> None:
    """The enrollment side's lock order, and no deadlock: a token mint in
    flight holds its client FOR SHARE, the archive waits on that row (not on
    the token, which it cannot see yet), and once the mint commits the
    archive finds the token live and revokes it."""
    import asyncio

    from app.core.client_scope import ClientScope
    from app.core.tenancy import org_session
    from app.services.enrollment import create_token
    from tests.race_helpers import backend_pid, normalised, until_blocked_by

    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Leaving")
    site = await _principal(env, h, cid)
    actor = await _actor_id(env, org)

    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as session:
        issued = await create_token(
            session,
            org_id=org,
            site_id=UUID(site),
            created_by=actor,
            name="in flight",
            expires_in_hours=1,
            max_uses=1,
        )
        archive = asyncio.create_task(
            env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)
        )
        waiting = await until_blocked_by(env.app_engine, await backend_pid(session))
        assert all("FROM CLIENTS" in normalised(q) for q in waiting), waiting
    response = await asyncio.wait_for(archive, timeout=30)
    assert response.status_code == 200, response.text
    async with AsyncSession(env.admin_engine) as s:
        revoked = (
            await s.execute(
                select(EnrollmentToken.revoked).where(EnrollmentToken.id == issued.id)
            )
        ).scalar_one()
    assert revoked is True


async def test_two_site_archives_racing_end_in_one_200_and_one_409(api_env) -> None:
    """T9R2 N4. The second DELETE waits on the site's row and RE-READS it once
    the first commits: a 409, and one `client.site_archived` event -- not a
    second archive of an archived site with a second audit event."""
    import asyncio
    from datetime import UTC, datetime

    from app.core.client_scope import ClientScope
    from app.core.tenancy import org_session
    from app.services.clients import archive_site
    from tests.race_helpers import backend_pid, normalised, until_blocked_by

    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Branching")
    site = (
        await env.client.post(f"/api/v1/clients/{cid}/sites", json={"name": "Sucursal"}, headers=h)
    ).json()["id"]
    actor = await _actor_id(env, org)

    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as session:
        await archive_site(
            session, org_id=org, actor_id=actor, site_id=UUID(site), now=datetime.now(UTC)
        )
        second = asyncio.create_task(env.client.delete(f"/api/v1/sites/{site}", headers=h))
        waiting = await until_blocked_by(env.app_engine, await backend_pid(session))
        assert not second.done()
        assert all(
            "FROM SITES" in normalised(q) and "FOR NO KEY UPDATE" in normalised(q)
            for q in waiting
        ), waiting
    response = await asyncio.wait_for(second, timeout=30)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "that site is archived"
    archived = [
        e for e in await _audits(env, org, "client.site_archived") if e.target_id == site
    ]
    assert len(archived) == 1


async def test_an_archive_locks_its_contacts_before_the_audit_chain(api_env) -> None:
    """T9R2 N1, the deadlock itself. A writer may hold a contact's row without
    the client's lock -- the portal's admission writes `last_login_at` on
    every request -- and then append to the audit chain. The archive must
    wait for that row BEFORE it takes the chain's lock (its first event is the
    token revocation): it locks the client's contacts right after the tokens
    and links. Locked after the chain, as it was, this interleaving is a cycle
    -- the writer waits on the chain, the archive on the row -- and Postgres
    kills one side with 40P01."""
    import asyncio

    from sqlalchemy import text

    from app.core.client_scope import ClientScope
    from app.core.tenancy import org_session
    from tests.race_helpers import backend_pid, normalised, until_blocked_by

    env = api_env
    org, h = await _admin(env)
    cid = await _create(env, h, "Leaving")
    await _mint_token(env, h, await _principal(env, h, cid))  # revoked, and audited, first
    contact = await _contact(env, h, cid, "signed-in@leaving.co")

    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as session:
        await session.execute(
            update(User).where(User.id == UUID(contact)).values(last_login_at=func.now())
        )
        archive = asyncio.create_task(
            env.client.post(f"/api/v1/clients/{cid}/archive", headers=h)
        )
        waiting = await until_blocked_by(env.app_engine, await backend_pid(session))
        assert not archive.done()
        assert all(
            "FROM USERS" in normalised(q) and "FOR NO KEY UPDATE" in normalised(q)
            for q in waiting
        ), waiting
        # The writer's own audit event: the chain is free, because the archive
        # has not reached it.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :org))"), {"org": str(org)}
        )
    response = await asyncio.wait_for(archive, timeout=30)
    assert response.status_code == 200, response.text
    assert await _status(env, contact) == "disabled"
