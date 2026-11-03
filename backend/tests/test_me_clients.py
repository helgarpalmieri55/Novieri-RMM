"""`GET /me` and the per-client map (MSP clients phase 3 design §1.3).

Until phase 3, `/me` answered "what may I do on EVERY client": a technician
whose grants were all per client got `permissions: []`, and the console showed
them the Panel and nothing else. It now answers three questions:

- `clients`: every client the person holds a role in, archived ones left out,
  each with the codes they hold THERE. `unassigned` only for a person with a
  grant on every client, as `GET /clients` lists it. Not narrowed by
  `X-Client-Id`: it is the selector's list, and the selector must still offer
  the other clients while one is chosen.
- `permissions`: the codes held in at least one client of the request's scope
  -- `clients`, narrowed by `X-Client-Id` -- which is `require_permission`'s rule.
- `permissions_everywhere`: the codes held on every client (an all-clients
  grant), `require_permission_on_every_client`'s rule. Never narrowed.

None of it needs `clients.read`: it is the person's own grants, named.

Role names: the seeded `technician` holds `jobs.dispatch` and `clients.read`
among others; `read-only` holds only read codes (`devices.read`,
`clients.read`, ...), so neither `jobs.dispatch` nor `clients.manage`. The
tests read each role's codes from the database rather than restate them.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Permission, Role, RolePermission, User, UserRole

CUSTOMERS = ("Acme", "Bravo", "Charlie")


async def _world(env: Any) -> tuple[UUID, dict[str, UUID]]:
    """An org (`me-…`, so its internal client sorts after the customers) with
    customers Acme, Bravo (modules `change` and `budget`) and Charlie."""
    org = await env.create_org("me")
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        made = {}
        for name in CUSTOMERS:
            modules = ["change", "budget"] if name == "Bravo" else []
            row = Client(org_id=org, name=name, kind="customer", enabled_modules=modules)
            s.add(row)
            await s.flush()
            made[name] = row.id
        await s.commit()
    return org, made


async def _builtin(env: Any, name: str) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(select(Role.id).where(Role.name == name, Role.org_id.is_(None)))
        ).scalar_one()


async def _custom(env: Any, org: UUID, *codes: str) -> UUID:
    """An organisation's own role holding exactly `codes` -- none is a role too."""
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        role = Role(org_id=org, name=f"custom-{uuid4().hex[:8]}")
        s.add(role)
        await s.flush()
        for permission_id in (
            await s.execute(select(Permission.id).where(Permission.code.in_(codes)))
        ).scalars():
            s.add(RolePermission(role_id=role.id, permission_id=permission_id))
        await s.commit()
        return role.id


async def _codes_of(env: Any, role_name: str) -> set[str]:
    async with AsyncSession(env.admin_engine) as s:
        return set(
            (
                await s.execute(
                    select(Permission.code)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .join(Role, Role.id == RolePermission.role_id)
                    .where(Role.name == role_name, Role.org_id.is_(None))
                )
            ).scalars()
        )


async def _person(env: Any, org: UUID, grants: list[tuple[UUID, UUID | None]]) -> dict[str, str]:
    """Headers of a new person holding `grants`: [(role id, client id or None)]."""
    sub = await env.create_user(org)
    async with AsyncSession(env.admin_engine) as s:
        user_id = (
            await s.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
        for role_id, client_id in grants:
            s.add(UserRole(org_id=org, user_id=user_id, role_id=role_id, client_id=client_id))
        await s.commit()
    return await env.headers(org, sub)


async def _unassigned(env: Any, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(Client.id).where(Client.org_id == org, Client.kind == "unassigned")
            )
        ).scalar_one()


async def _archive(env: Any, client_id: UUID) -> None:
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(update(Client).where(Client.id == client_id).values(status="archived"))
        await s.commit()


async def _me(env: Any, headers: dict[str, str]) -> dict[str, Any]:
    response = await env.client.get("/api/v1/me", headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_a_per_client_technician_gets_each_clients_codes(api_env: Any) -> None:
    """The defect this phase fixes: `permissions` was `[]` for them. And the
    list is exactly their clients -- not Charlie, not the internal client,
    not `unassigned` -- because it is read under THEIR scope, where RLS
    decides it as it does for `GET /clients`."""
    env = api_env
    org, c = await _world(env)
    tech, read_only = await _builtin(env, "technician"), await _builtin(env, "read-only")
    headers = await _person(env, org, [(tech, c["Acme"]), (read_only, c["Bravo"])])
    technician_codes, read_only_codes = (
        await _codes_of(env, "technician"),
        await _codes_of(env, "read-only"),
    )

    me = await _me(env, headers)

    assert me["clients"] == [
        {
            "id": str(c["Acme"]),
            "name": "Acme",
            "kind": "customer",
            "enabled_modules": [],
            "permissions": sorted(technician_codes),
        },
        {
            "id": str(c["Bravo"]),
            "name": "Bravo",
            "kind": "customer",
            "enabled_modules": ["budget", "change"],
            "permissions": sorted(read_only_codes),
        },
    ]
    assert me["permissions"] == sorted(technician_codes | read_only_codes)
    assert me["permissions_everywhere"] == []


async def test_x_client_id_narrows_permissions_and_nothing_else(api_env: Any) -> None:
    env = api_env
    org, c = await _world(env)
    tech, read_only = await _builtin(env, "technician"), await _builtin(env, "read-only")
    headers = await _person(env, org, [(tech, c["Acme"]), (read_only, c["Bravo"])])
    whole = await _me(env, headers)

    narrowed = await _me(env, {**headers, "X-Client-Id": str(c["Bravo"])})

    assert narrowed["permissions"] == sorted(await _codes_of(env, "read-only"))
    assert "jobs.dispatch" not in narrowed["permissions"]
    # The selector's list does not follow the selection.
    assert narrowed["clients"] == whole["clients"]
    assert narrowed["permissions_everywhere"] == []


async def test_a_header_outside_the_map_gives_no_permissions(api_env: Any) -> None:
    """Narrowed, never widened: a client they hold nothing on, the internal
    client, an id that is no client -- each is the empty scope, so no code."""
    env = api_env
    org, c = await _world(env)
    tech = await _builtin(env, "technician")
    headers = await _person(env, org, [(tech, c["Acme"])])

    for outside in (c["Charlie"], org, uuid4()):
        me = await _me(env, {**headers, "X-Client-Id": str(outside)})
        assert me["permissions"] == [], outside
        assert [x["id"] for x in me["clients"]] == [str(c["Acme"])]


async def test_an_every_client_grant_counts_in_every_client(api_env: Any) -> None:
    """An all-clients grant lists every client, `unassigned` last, and counts
    in each; a client grant adds its codes to its own client only. For this
    person `permissions_everywhere` is what `/me` used to call `permissions`."""
    env = api_env
    org, c = await _world(env)
    tech = await _builtin(env, "technician")
    extra = await _custom(env, org, "clients.manage", "sites.manage")
    headers = await _person(env, org, [(tech, None), (extra, c["Bravo"])])
    technician_codes = await _codes_of(env, "technician")
    unassigned = await _unassigned(env, org)

    me = await _me(env, headers)

    assert [(x["id"], x["kind"]) for x in me["clients"]] == [
        (str(c["Acme"]), "customer"),
        (str(c["Bravo"]), "customer"),
        (str(c["Charlie"]), "customer"),
        (str(org), "internal"),
        (str(unassigned), "unassigned"),
    ]
    held = {x["id"]: set(x["permissions"]) for x in me["clients"]}
    assert held[str(c["Bravo"])] == technician_codes | {"clients.manage", "sites.manage"}
    for other in (c["Acme"], c["Charlie"], org, unassigned):
        assert held[str(other)] == technician_codes, other
    assert me["permissions"] == sorted(technician_codes | {"clients.manage", "sites.manage"})
    assert me["permissions_everywhere"] == sorted(technician_codes)

    narrowed = await _me(env, {**headers, "X-Client-Id": str(c["Acme"])})
    assert narrowed["permissions"] == sorted(technician_codes)
    assert narrowed["permissions_everywhere"] == sorted(technician_codes)


async def test_an_archived_client_is_left_out(api_env: Any) -> None:
    env = api_env
    org, c = await _world(env)
    tech, read_only = await _builtin(env, "technician"), await _builtin(env, "read-only")
    limited = await _person(env, org, [(tech, c["Acme"]), (read_only, c["Bravo"])])
    everywhere = await _person(env, org, [(tech, None)])

    await _archive(env, c["Acme"])

    me = await _me(env, limited)
    assert [x["id"] for x in me["clients"]] == [str(c["Bravo"])]
    # Acme's codes went with it.
    assert me["permissions"] == sorted(await _codes_of(env, "read-only"))
    assert str(c["Acme"]) not in {x["id"] for x in (await _me(env, everywhere))["clients"]}


async def test_the_map_needs_no_clients_read(api_env: Any) -> None:
    """The names come from the person's own grants, not from `GET /clients`:
    a role without `clients.read` still names its client, and a role with no
    code at all is still a role in its client."""
    env = api_env
    org, c = await _world(env)
    viewer, nothing = await _custom(env, org, "devices.read"), await _custom(env, org)
    headers = await _person(env, org, [(viewer, c["Acme"]), (nothing, c["Charlie"])])
    assert (await env.client.get("/api/v1/clients", headers=headers)).status_code == 403

    me = await _me(env, headers)

    assert me["clients"] == [
        {
            "id": str(c["Acme"]),
            "name": "Acme",
            "kind": "customer",
            "enabled_modules": [],
            "permissions": ["devices.read"],
        },
        {
            "id": str(c["Charlie"]),
            "name": "Charlie",
            "kind": "customer",
            "enabled_modules": [],
            "permissions": [],
        },
    ]
    assert me["permissions"] == ["devices.read"]


async def test_a_person_with_no_role_has_an_empty_map(api_env: Any) -> None:
    env = api_env
    org, _ = await _world(env)
    me = await _me(env, await _person(env, org, []))
    assert (me["clients"], me["permissions"], me["permissions_everywhere"]) == ([], [], [])


async def test_patch_me_answers_with_the_same_map(api_env: Any) -> None:
    """The PATCH answers with the whole of `/me`, so the console does not
    re-fetch -- narrowed by the same header."""
    env = api_env
    org, c = await _world(env)
    tech, read_only = await _builtin(env, "technician"), await _builtin(env, "read-only")
    headers = await _person(env, org, [(tech, c["Acme"]), (read_only, c["Bravo"])])

    response = await env.client.patch(
        "/api/v1/me", headers={**headers, "X-Client-Id": str(c["Bravo"])}, json={"phone": "0101"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["phone"] == "0101"
    assert body["clients"] == (await _me(env, headers))["clients"]
    assert body["permissions"] == sorted(await _codes_of(env, "read-only"))
    assert body["permissions_everywhere"] == []
