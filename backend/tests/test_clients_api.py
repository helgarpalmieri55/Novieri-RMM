"""The API for clients, sites and contacts (MSP clients spec §1, §2.1, §3.2, §4; Task 9).

A client outside the request's scope is a 404, never a 403 (spec §3.2): RLS
hides it. Creating and archiving a client need `clients.manage` on EVERY
client (`require_permission_on_every_client`); editing one, and managing its
sites and contacts, need the code on that client.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
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
    env = api_env
    org, h = await _admin(env)
    await _device(env, org, org, org)
    assert (await env.client.delete(f"/api/v1/sites/{org}", headers=h)).status_code == 409


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
    principal = await _audits(env, org, "site.created")
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

    assert [e.target_id for e in await _audits(env, org, "site.updated")] == [site["id"]]
    assert site["id"] in [e.target_id for e in await _audits(env, org, "site.created")]


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
    assert [e.target_id for e in await _audits(env, org, "site.archived")] == [branch]
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
    # The same person may be a contact of two clients: the key is per client.
    elsewhere = await env.client.post(
        f"/api/v1/clients/{y}/contacts", json={"email": "a@x.co"}, headers=h
    )
    assert elsewhere.status_code == 201, elsewhere.text
