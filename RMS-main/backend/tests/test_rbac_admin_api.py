"""The roles + permission-matrix API: `/api/v1/permissions` and `/api/v1/roles`.

Spec `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §1.

The four things this surface has to get right, and the test that would notice
each one going wrong:

1. **The matrix is written through `set_role_permissions`, never the table.**
   `deploy/postgres/grant-app-privileges.sql:59` REVOKES INSERT/UPDATE/DELETE on
   `role_permissions` from `app_user` as an anti-self-grant control, and that
   control stays. `tests/test_rbac_matrix_function.py` pins the function's own
   behaviour; here the question is only whether the ROUTES go through it, which
   they demonstrably do -- a route that reached for the table directly would get
   `permission denied` from Postgres and fail every write test below.

2. **Editing a seeded role must not reshape another tenant's.** `Role.org_id` is
   nullable and the five seeded roles leave it NULL, so `technician` is ONE ROW
   shared by every org (§0.5). `test_editing_a_seeded_role_leaves_another_orgs_
   users_untouched` is the assertion, and it reads org B **on the BYPASSRLS
   admin engine**: through a tenant session it would pass even with clone-on-
   write deleted, because RLS hides org B either way.

3. **`super-admin` is unreachable from here.** Never listed, never created,
   never edited, never deleted. It is cross-org platform administration, so an
   org-scoped console that could touch it is a tenant-escalation path.

4. **A caller cannot lock themselves out.** Removing `roles.manage`/`orgs.manage`
   from a role the caller holds is refused, and refused with the change rolled
   back rather than half-applied.

WHY THE ROUTER IS INCLUDED BY A FIXTURE. This workstream owns
`app/api/v1/roles.py` and not `app/main.py`; the controller wires the router in
afterwards. `rbac_env` includes it into the per-test app only if it is not
already there, so these tests pass both before and after that wiring lands, and
never register the router twice.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.v1.roles import router as roles_router
from app.core.rbac import PERMISSIONS, PLATFORM_OPERATOR_CODES, route_permission
from app.core.router_inspect import flatten
from app.models import AuditEvent, Role, User, UserRole

# The code set the tests below edit a role TO. Deliberately small and
# deliberately not what any seeded role already grants, so "org B is unchanged"
# and "org A got what it asked for" are different assertions.
EDITED_CODES = {"devices.read", "remote.control"}


@pytest.fixture()
def rbac_env(api_env: Any) -> Any:
    paths = {path for path, _route in flatten(api_env.app.router)}
    if "/api/v1/roles" not in paths:
        api_env.app.include_router(roles_router, prefix="/api/v1")
    return api_env


# --------------------------------------------------------------------------- #
# helpers -- every one of them reads through the ADMIN (BYPASSRLS) engine, so an
# assertion about what a request did or did not change is never itself filtered
# by the mechanism under test.
# --------------------------------------------------------------------------- #


async def _codes_of_role(engine: AsyncEngine, role_id: UUID) -> set[str]:
    async with AsyncSession(engine) as session:
        rows = await session.execute(
            text(
                "SELECT p.code FROM role_permissions rp "
                "JOIN permissions p ON p.id = rp.permission_id WHERE rp.role_id = :r"
            ),
            {"r": str(role_id)},
        )
        return set(rows.scalars())


async def _effective_codes(engine: AsyncEngine, user_id: UUID) -> set[str]:
    async with AsyncSession(engine) as session:
        rows = await session.execute(
            text(
                "SELECT p.code FROM user_roles ur "
                "JOIN role_permissions rp ON rp.role_id = ur.role_id "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE ur.user_id = :u"
            ),
            {"u": str(user_id)},
        )
        return set(rows.scalars())


async def _user_id(engine: AsyncEngine, sub: str) -> UUID:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _global_role_id(engine: AsyncEngine, name: str) -> UUID:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(
                select(Role.id).where(Role.name == name, Role.org_id.is_(None))
            )
        ).scalar_one()


async def _seed_role(
    engine: AsyncEngine, org_id: UUID | None, name: str, codes: list[str]
) -> UUID:
    """A role created OUT OF BAND, through the admin engine, so a test can set
    up a starting state without going through the routes it is testing."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        role = Role(org_id=org_id, name=name, description=f"seeded {name}")
        session.add(role)
        await session.flush()
        for code in codes:
            await session.execute(
                text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "SELECT :r, id FROM permissions WHERE code = :c"
                ),
                {"r": str(role.id), "c": code},
            )
        await session.commit()
        return role.id


async def _assign(engine: AsyncEngine, org_id: UUID, user_id: UUID, role_id: UUID) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(UserRole(org_id=org_id, user_id=user_id, role_id=role_id))
        await session.commit()


async def _audit_actions(engine: AsyncEngine, org_id: UUID) -> list[AuditEvent]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


def _role_named(body: dict[str, Any], name: str) -> dict[str, Any] | None:
    for role in body["roles"]:
        if role["name"] == name:
            return role
    return None


# --------------------------------------------------------------------------- #
# RBAC (invariant #6) -- gates, derived from the router rather than hand-listed
# --------------------------------------------------------------------------- #


def test_every_route_carries_the_intended_permission_gate() -> None:
    app = FastAPI()
    app.include_router(roles_router, prefix="/api/v1")
    gates = {
        (method, path): route_permission(route)
        for path, route in flatten(app.router)
        if isinstance(route, APIRoute) and path.startswith("/api/v1")
        for method in sorted(route.methods or set())
    }
    assert gates == {
        ("GET", "/api/v1/permissions"): "roles.read",
        ("GET", "/api/v1/roles"): "roles.read",
        ("POST", "/api/v1/roles"): "roles.manage",
        ("PATCH", "/api/v1/roles/{role_id}"): "roles.manage",
        ("PUT", "/api/v1/roles/{role_id}/permissions"): "roles.manage",
        ("DELETE", "/api/v1/roles/{role_id}"): "roles.manage",
    }


async def test_reads_require_roles_read(rbac_env: Any) -> None:
    org = await rbac_env.create_org("rbac-read")
    nobody = await rbac_env.create_user(org, "read-only")
    headers = await rbac_env.headers(org, nobody)

    assert (await rbac_env.client.get("/api/v1/permissions", headers=headers)).status_code == 403
    assert (await rbac_env.client.get("/api/v1/roles", headers=headers)).status_code == 403


async def test_writes_require_roles_manage(rbac_env: Any) -> None:
    """`roles.read` is NOT enough to write. Two codes rather than one because
    "can see what technician means" and "can redefine what it means" are
    different levels of trust."""
    org = await rbac_env.create_org("rbac-write")
    reader_role = await _seed_role(rbac_env.admin_engine, org, "matrix-reader", ["roles.read"])
    reader = await rbac_env.create_user(org)
    await _assign(rbac_env.admin_engine, org, await _user_id(rbac_env.admin_engine, reader),
                  reader_role)
    headers = await rbac_env.headers(org, reader)
    role_id = await _seed_role(rbac_env.admin_engine, org, "target", ["devices.read"])

    assert (await rbac_env.client.get("/api/v1/roles", headers=headers)).status_code == 200
    assert (
        await rbac_env.client.post(
            "/api/v1/roles", headers=headers, json={"name": "x", "description": "", "codes": []}
        )
    ).status_code == 403
    assert (
        await rbac_env.client.patch(
            f"/api/v1/roles/{role_id}", headers=headers, json={"name": "y"}
        )
    ).status_code == 403
    assert (
        await rbac_env.client.put(
            f"/api/v1/roles/{role_id}/permissions", headers=headers, json={"codes": []}
        )
    ).status_code == 403
    assert (
        await rbac_env.client.delete(f"/api/v1/roles/{role_id}", headers=headers)
    ).status_code == 403


async def test_every_route_requires_authentication(rbac_env: Any) -> None:
    role_id = uuid4()
    assert (await rbac_env.client.get("/api/v1/permissions")).status_code == 401
    assert (await rbac_env.client.get("/api/v1/roles")).status_code == 401
    assert (await rbac_env.client.post("/api/v1/roles", json={})).status_code == 401
    assert (await rbac_env.client.patch(f"/api/v1/roles/{role_id}", json={})).status_code == 401
    assert (
        await rbac_env.client.put(f"/api/v1/roles/{role_id}/permissions", json={"codes": []})
    ).status_code == 401
    assert (await rbac_env.client.delete(f"/api/v1/roles/{role_id}")).status_code == 401


# --------------------------------------------------------------------------- #
# GET /permissions -- the catalogue, served from the platform's own dict
# --------------------------------------------------------------------------- #


async def test_the_catalogue_is_the_platform_permission_dict(rbac_env: Any) -> None:
    """Served FROM `app.core.rbac.PERMISSIONS`, never a second hand-typed list.
    A code added to the platform must appear here without another edit, so the
    assertion is equality with that dict -- not "contains the ones I remembered"."""
    org = await rbac_env.create_org("cat")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)

    response = await rbac_env.client.get("/api/v1/permissions", headers=headers)

    assert response.status_code == 200, response.text
    served = response.json()["permissions"]
    # MINUS THE PLATFORM OPERATOR'S CODES (0068). This is an ORG-SCOPED matrix:
    # every code it lists is a tick an org administrator can apply to a role.
    # `platform.errors.*` is refused on write -- by `_validate_codes` and again
    # by the database function -- so listing it would render a checkbox that
    # 403s, which is the worst failure mode an access-control screen has.
    assert {p["code"] for p in served} == set(PERMISSIONS) - PLATFORM_OPERATOR_CODES
    assert {p["code"]: p["description"] for p in served} == {
        code: description
        for code, description in PERMISSIONS.items()
        if code not in PLATFORM_OPERATOR_CODES
    }
    by_code = {p["code"]: p for p in served}
    # `<module>.<action>`, split at the FIRST dot: `remote.recordings.read` is
    # the `remote` module (migration 0010 seeds it exactly that way), not a
    # `remote.recordings` one. Splitting at the last dot would invent modules.
    assert by_code["remote.recordings.read"]["module"] == "remote"
    assert by_code["remote.recordings.read"]["action"] == "recordings.read"
    assert by_code["roles.manage"]["module"] == "roles"
    assert by_code["roles.manage"]["action"] == "manage"


async def test_every_catalogued_code_is_actually_settable(rbac_env: Any) -> None:
    """A checkbox the matrix offers must stick. `set_role_permissions` raises on
    a code with no `permissions` row, so a code present in `PERMISSIONS` but
    never seeded would render as a tick that silently fails -- the worst kind of
    bug in an access-control UI. This applies the WHOLE catalogue at once."""
    org = await rbac_env.create_org("settable")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(rbac_env.admin_engine, org, "everything", [])

    grantable = set(PERMISSIONS) - PLATFORM_OPERATOR_CODES
    response = await rbac_env.client.put(
        f"/api/v1/roles/{role_id}/permissions",
        headers=headers,
        json={"codes": sorted(grantable)},
    )

    assert response.status_code == 200, response.text
    assert await _codes_of_role(rbac_env.admin_engine, role_id) == grantable


# --------------------------------------------------------------------------- #
# The platform operator's codes are not grantable from an org-scoped surface.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", sorted(PLATFORM_OPERATOR_CODES))
async def test_the_matrix_refuses_a_platform_operator_code(rbac_env: Any, code: str) -> None:
    """WITHOUT THIS, MIGRATION 0068 IS COSMETIC.

    0068 takes `platform.errors.*` off `org-admin` because the routes they gate
    read every tenant's error correlation ids with no tenant predicate anywhere
    beneath them (audit 2026-08-21-tenancy-test-efficacy F1). `org-admin` also
    holds `roles.manage` and `users.manage` -- so an administrator who wanted
    the code back could tick it onto a role and assign it to themselves,
    entirely inside the console, and the revocation would have bought nothing.

    This module's own docstring used to justify the self-grant path with "and
    `org-admin` already holds every code". That premise stopped being true in
    0068, which is exactly why the rail is needed now and was not before."""
    org = await rbac_env.create_org("plat-matrix")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(rbac_env.admin_engine, org, f"reach-{uuid4().hex[:6]}", [])

    response = await rbac_env.client.put(
        f"/api/v1/roles/{role_id}/permissions",
        headers=headers,
        json={"codes": ["devices.read", code]},
    )

    assert response.status_code == 403, response.text
    assert code in response.json()["detail"]
    # NOTHING APPLIED, not even the legitimate code it was smuggled beside. A
    # partial apply would leave the operator staring at a role that holds some
    # of what they asked for and no explanation of which half.
    assert await _codes_of_role(rbac_env.admin_engine, role_id) == set()


@pytest.mark.parametrize("code", sorted(PLATFORM_OPERATOR_CODES))
async def test_create_role_refuses_a_platform_operator_code(rbac_env: Any, code: str) -> None:
    """The other door into the same end state: not editing a role to carry the
    code, but creating one that already does."""
    org = await rbac_env.create_org("plat-create")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    name = f"smuggle-{uuid4().hex[:6]}"

    response = await rbac_env.client.post(
        "/api/v1/roles",
        headers=headers,
        json={"name": name, "description": "", "codes": [code]},
    )

    assert response.status_code == 403, response.text
    assert code in response.json()["detail"]
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert (
            await session.execute(select(Role.id).where(Role.name == name))
        ).scalar_one_or_none() is None


async def test_platform_status_read_is_still_grantable(rbac_env: Any) -> None:
    """The rail is two codes, not the `platform.` prefix. `platform.status.read`
    discloses no tenant row -- probes, heartbeats and the module registry come
    from tables with no `org_id` -- and 0068 deliberately leaves it with
    `org-admin`. A prefix rule would also break an ordinary role edit, because
    the console posts back every code a role already holds."""
    org = await rbac_env.create_org("plat-status")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(rbac_env.admin_engine, org, f"status-{uuid4().hex[:6]}", [])

    response = await rbac_env.client.put(
        f"/api/v1/roles/{role_id}/permissions",
        headers=headers,
        json={"codes": ["platform.status.read"]},
    )

    assert response.status_code == 200, response.text
    assert await _codes_of_role(rbac_env.admin_engine, role_id) == {"platform.status.read"}


async def test_agent_release_read_is_still_grantable(rbac_env: Any) -> None:
    """0070 SPLIT THE PAIR; IT DID NOT REVOKE IT.

    `agent.release.manage` publishes the bytes every endpoint in the fleet
    runs, out of a catalogue with no `org_id` and no RLS under it, so it left
    for `super-admin`. `agent.release.read` did not: 0060's docstring says it
    "is needed by anyone who installs an agent", because generating an install
    command is a read of the catalogue plus a token mint.

    So an org administrator must still be able to tick READ onto a technician's
    role -- which 0060 deliberately left ungranted so each org decides. A rail
    keyed on the `agent.release.` prefix instead of on the exact code would
    have taken that away, and this is what says so."""
    org = await rbac_env.create_org("agent-read")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(rbac_env.admin_engine, org, f"installer-{uuid4().hex[:6]}", [])

    response = await rbac_env.client.put(
        f"/api/v1/roles/{role_id}/permissions",
        headers=headers,
        json={"codes": ["agent.release.read", "devices.read"]},
    )

    assert response.status_code == 200, response.text
    assert await _codes_of_role(rbac_env.admin_engine, role_id) == {
        "agent.release.read",
        "devices.read",
    }


async def test_an_org_admin_cannot_grant_themselves_agent_release_manage(
    rbac_env: Any,
) -> None:
    """THE SELF-GRANT PATH, NAMED RATHER THAN PARAMETRISED, AND SHUT AT EVERY
    DOOR IT HAS.

    The two tests above iterate `PLATFORM_OPERATOR_CODES`, which is the right
    shape for a rail that must hold for the whole set -- but it means the day
    somebody removes `agent.release.manage` from that set, the coverage for
    this code disappears silently along with it rather than going red. This
    test names the code, so it does not.

    WHY IT IS THE HALF THAT MATTERS. Migration 0070 takes the code off
    `org-admin`; `org-admin` also holds `roles.manage` and `users.manage`. So
    an administrator who wanted to publish anyway does not need SQL or a
    database shell -- they need a new role and an assignment, both of which
    this console offers them. Without a rail, the revocation is a speed bump
    measured in clicks, and what it is protecting is arbitrary code execution
    on every OTHER tenant's endpoints, out of a catalogue with no `org_id`.

    Three doors, all of them: create a role carrying it, edit a role to carry
    it, and see it offered in the matrix at all. The third is not decoration --
    a checkbox that 403s on save is the worst failure mode an access-control
    screen has, and it is also how an administrator learns the code exists."""
    org = await rbac_env.create_org("agent-selfgrant")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    name = f"publisher-{uuid4().hex[:6]}"

    created = await rbac_env.client.post(
        "/api/v1/roles",
        headers=headers,
        json={"name": name, "description": "", "codes": ["agent.release.manage"]},
    )
    assert created.status_code == 403, created.text
    assert "agent.release.manage" in created.json()["detail"]
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert (
            await session.execute(select(Role.id).where(Role.name == name))
        ).scalar_one_or_none() is None

    role_id = await _seed_role(rbac_env.admin_engine, org, f"edit-{uuid4().hex[:6]}", [])
    edited = await rbac_env.client.put(
        f"/api/v1/roles/{role_id}/permissions",
        headers=headers,
        # Smuggled beside a code they legitimately hold, which is how this
        # would actually be attempted -- the console posts the whole set.
        json={"codes": ["agent.release.read", "agent.release.manage"]},
    )
    assert edited.status_code == 403, edited.text
    assert "agent.release.manage" in edited.json()["detail"]
    assert await _codes_of_role(rbac_env.admin_engine, role_id) == set()

    catalogue = await rbac_env.client.get("/api/v1/permissions", headers=headers)
    assert catalogue.status_code == 200, catalogue.text
    served = {p["code"] for p in catalogue.json()["permissions"]}
    assert "agent.release.manage" not in served
    # Non-vacuity: the sibling code IS offered, so this is not an empty matrix.
    assert "agent.release.read" in served


# --------------------------------------------------------------------------- #
# GET /roles
# --------------------------------------------------------------------------- #


async def test_roles_lists_global_and_own_roles_with_codes_and_counts(rbac_env: Any) -> None:
    org = await rbac_env.create_org("list")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    mine = await _seed_role(rbac_env.admin_engine, org, "night-shift", ["devices.read"])
    holder = await rbac_env.create_user(org)
    await _assign(rbac_env.admin_engine, org, await _user_id(rbac_env.admin_engine, holder), mine)

    body = (await rbac_env.client.get("/api/v1/roles", headers=headers)).json()

    technician = _role_named(body, "technician")
    assert technician is not None
    assert technician["is_global"] is True
    global_tech = await _global_role_id(rbac_env.admin_engine, "technician")
    assert set(technician["codes"]) == await _codes_of_role(rbac_env.admin_engine, global_tech)
    assert technician["codes"]  # non-vacuous: the seeded role does grant things
    assert technician["assigned_users"] == 0

    own = _role_named(body, "night-shift")
    assert own is not None
    assert own["id"] == str(mine)
    assert own["is_global"] is False
    assert own["codes"] == ["devices.read"]
    assert own["assigned_users"] == 1


async def test_a_cloned_role_is_listed_once_not_beside_the_global_row(
    rbac_env: Any,
) -> None:
    """After clone-on-write the GLOBAL `technician` row is still visible to this
    org -- RLS's `roles_select` policy is `org_id IS NULL OR org_id =
    current_org`, and cloning does not change that. A listing that returned both
    rows would show two roles called `technician`, and the operator could not
    tell which one their users hold or which one their next edit would reach.

    So the listing shadows a global role whose name the org has taken locally,
    and the surviving row is the LOCAL one -- checked by id, because "exactly one
    technician" is also true if the wrong one survived."""
    org = await rbac_env.create_org("shadow")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    global_tech = await _global_role_id(rbac_env.admin_engine, "technician")

    cloned = await rbac_env.client.patch(
        f"/api/v1/roles/{global_tech}", headers=headers, json={"description": "ours"}
    )
    assert cloned.status_code == 200, cloned.text
    clone_id = cloned.json()["id"]
    assert clone_id != str(global_tech)

    body = (await rbac_env.client.get("/api/v1/roles", headers=headers)).json()

    technicians = [r for r in body["roles"] if r["name"] == "technician"]
    assert len(technicians) == 1, technicians
    assert technicians[0]["id"] == clone_id
    assert technicians[0]["is_global"] is False


async def test_super_admin_is_never_listed(rbac_env: Any) -> None:
    org = await rbac_env.create_org("no-su")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)

    body = (await rbac_env.client.get("/api/v1/roles", headers=headers)).json()

    assert _role_named(body, "super-admin") is None


async def test_another_orgs_role_is_not_listed(rbac_env: Any) -> None:
    mine = await rbac_env.create_org("list-mine")
    theirs = await rbac_env.create_org("list-theirs")
    await _seed_role(rbac_env.admin_engine, theirs, "their-secret-role", ["devices.read"])
    admin = await rbac_env.create_user(mine, "org-admin")
    headers = await rbac_env.headers(mine, admin)

    body = (await rbac_env.client.get("/api/v1/roles", headers=headers)).json()

    assert _role_named(body, "their-secret-role") is None


async def test_the_route_predicate_scopes_roles_without_rls_to_mask_it(rbac_env: Any) -> None:
    """RLS on `roles` (migration 0002) would hide another org's rows even if the
    route's own `org_id` predicate were deleted, so this repoints the app engine
    at the BYPASSRLS admin engine and drives the REAL route through it. Nothing
    but the application predicate is left standing."""
    mine = await rbac_env.create_org("pred-mine")
    theirs = await rbac_env.create_org("pred-theirs")
    await _seed_role(rbac_env.admin_engine, theirs, "their-hidden-role", ["devices.read"])
    admin = await rbac_env.create_user(mine, "org-admin")
    headers = await rbac_env.headers(mine, admin)

    rbac_env.app.state.app_engine = rbac_env.admin_engine
    try:
        body = (await rbac_env.client.get("/api/v1/roles", headers=headers)).json()
    finally:
        rbac_env.app.state.app_engine = rbac_env.app_engine

    assert _role_named(body, "their-hidden-role") is None


# --------------------------------------------------------------------------- #
# POST /roles
# --------------------------------------------------------------------------- #


async def test_create_writes_the_role_its_codes_and_an_audit_event(rbac_env: Any) -> None:
    org = await rbac_env.create_org("create")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)

    response = await rbac_env.client.post(
        "/api/v1/roles",
        headers=headers,
        json={
            "name": "patcher",
            "description": "applies patches",
            "codes": ["devices.read", "patch.apply"],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "patcher"
    assert body["is_global"] is False
    assert body["assigned_users"] == 0
    assert set(body["codes"]) == {"devices.read", "patch.apply"}
    assert await _codes_of_role(rbac_env.admin_engine, UUID(body["id"])) == {
        "devices.read",
        "patch.apply",
    }
    events = await _audit_actions(rbac_env.admin_engine, org)
    created = [e for e in events if e.action == "role.created"]
    assert len(created) == 1
    assert created[0].target_id == body["id"]
    assert set(created[0].meta["codes_added"]) == {"devices.read", "patch.apply"}


async def test_create_refuses_the_super_admin_name(rbac_env: Any) -> None:
    org = await rbac_env.create_org("create-su")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)

    response = await rbac_env.client.post(
        "/api/v1/roles",
        headers=headers,
        json={"name": "super-admin", "description": "", "codes": []},
    )

    assert response.status_code == 403, response.text


async def test_create_refuses_a_name_that_already_resolves(rbac_env: Any) -> None:
    """Including a GLOBAL name. Two visible roles called `technician` -- one
    global, one local -- is a screen where the operator cannot tell which row
    their users hold, and it is also the collision clone-on-write would hit."""
    org = await rbac_env.create_org("create-dup")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)

    first = await rbac_env.client.post(
        "/api/v1/roles", headers=headers, json={"name": "dup", "description": "", "codes": []}
    )
    assert first.status_code == 201, first.text

    again = await rbac_env.client.post(
        "/api/v1/roles", headers=headers, json={"name": "dup", "description": "", "codes": []}
    )
    assert again.status_code == 409, again.text

    global_name = await rbac_env.client.post(
        "/api/v1/roles",
        headers=headers,
        json={"name": "technician", "description": "", "codes": []},
    )
    assert global_name.status_code == 409, global_name.text


async def test_create_refuses_an_unknown_code_and_writes_nothing(rbac_env: Any) -> None:
    org = await rbac_env.create_org("create-typo")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)

    response = await rbac_env.client.post(
        "/api/v1/roles",
        headers=headers,
        json={"name": "typo", "description": "", "codes": ["devices.read", "devices.raed"]},
    )

    assert response.status_code == 422, response.text
    assert "devices.raed" in response.text
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert (
            await session.execute(select(Role).where(Role.name == "typo", Role.org_id == org))
        ).scalar_one_or_none() is None


# --------------------------------------------------------------------------- #
# PATCH /roles/{id} -- and clone-on-write
# --------------------------------------------------------------------------- #


async def test_patch_renames_an_org_role(rbac_env: Any) -> None:
    org = await rbac_env.create_org("patch")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(rbac_env.admin_engine, org, "old-name", ["devices.read"])

    response = await rbac_env.client.patch(
        f"/api/v1/roles/{role_id}", headers=headers, json={"name": "new-name"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(role_id)
    assert body["name"] == "new-name"
    assert body["codes"] == ["devices.read"]


async def test_patching_a_global_role_clones_it_into_this_org(rbac_env: Any) -> None:
    org = await rbac_env.create_org("patch-global")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    tech_holder = await rbac_env.create_user(org, "technician")
    holder_id = await _user_id(rbac_env.admin_engine, tech_holder)
    global_tech = await _global_role_id(rbac_env.admin_engine, "technician")
    technician_codes = await _codes_of_role(rbac_env.admin_engine, global_tech)
    assert technician_codes  # non-vacuous

    response = await rbac_env.client.patch(
        f"/api/v1/roles/{global_tech}",
        headers=headers,
        json={"description": "our own technicians"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    clone_id = UUID(body["id"])
    assert clone_id != global_tech
    assert body["is_global"] is False
    assert body["name"] == "technician"
    assert body["description"] == "our own technicians"
    # The clone carries the code set it was cloned from...
    assert set(body["codes"]) == technician_codes
    assert await _codes_of_role(rbac_env.admin_engine, clone_id) == technician_codes
    # ...and this org's assignment now points at the clone, so the holder's
    # effective permissions are unchanged by the clone itself.
    async with AsyncSession(rbac_env.admin_engine) as session:
        role_ids = set(
            (
                await session.execute(
                    select(UserRole.role_id).where(UserRole.user_id == holder_id)
                )
            ).scalars()
        )
    assert role_ids == {clone_id}
    assert await _effective_codes(rbac_env.admin_engine, holder_id) == technician_codes
    # And the global row is untouched.
    assert await _codes_of_role(rbac_env.admin_engine, global_tech) == technician_codes


async def test_cannot_patch_another_orgs_role(rbac_env: Any) -> None:
    mine = await rbac_env.create_org("edit-mine")
    theirs = await rbac_env.create_org("edit-theirs")
    theirs_role = await _seed_role(rbac_env.admin_engine, theirs, "not-yours", ["devices.read"])
    admin = await rbac_env.create_user(mine, "org-admin")
    headers = await rbac_env.headers(mine, admin)

    response = await rbac_env.client.patch(
        f"/api/v1/roles/{theirs_role}", headers=headers, json={"name": "hijacked"}
    )

    assert response.status_code == 404, response.text
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert (await session.get(Role, theirs_role)).name == "not-yours"


# --------------------------------------------------------------------------- #
# PUT /roles/{id}/permissions -- the matrix write
# --------------------------------------------------------------------------- #


async def test_put_replaces_the_whole_code_set_and_reports_affected_users(
    rbac_env: Any,
) -> None:
    org = await rbac_env.create_org("put")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(
        rbac_env.admin_engine, org, "shifting", ["devices.read", "jobs.read"]
    )
    for _ in range(2):
        who = await rbac_env.create_user(org)
        await _assign(
            rbac_env.admin_engine, org, await _user_id(rbac_env.admin_engine, who), role_id
        )

    response = await rbac_env.client.put(
        f"/api/v1/roles/{role_id}/permissions",
        headers=headers,
        json={"codes": ["devices.read", "audit.read"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role_id"] == str(role_id)
    assert set(body["codes"]) == {"devices.read", "audit.read"}
    # The count is what tells the operator "edit role" is not a local edit.
    assert body["affected_users"] == 2
    # REPLACED, not accumulated: `jobs.read` is gone.
    assert await _codes_of_role(rbac_env.admin_engine, role_id) == {
        "devices.read",
        "audit.read",
    }


async def test_put_audits_the_codes_added_and_removed(rbac_env: Any) -> None:
    org = await rbac_env.create_org("put-audit")
    admin = await rbac_env.create_user(org, "org-admin")
    actor_id = await _user_id(rbac_env.admin_engine, admin)
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(
        rbac_env.admin_engine, org, "audited", ["devices.read", "jobs.read"]
    )

    await rbac_env.client.put(
        f"/api/v1/roles/{role_id}/permissions",
        headers=headers,
        json={"codes": ["devices.read", "credentials.checkout"]},
    )

    events = [
        e
        for e in await _audit_actions(rbac_env.admin_engine, org)
        if e.action == "role.permissions_set"
    ]
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "user"
    assert event.actor_id == actor_id
    assert event.target_id == str(role_id)
    assert event.meta["role_name"] == "audited"
    assert event.meta["codes_added"] == ["credentials.checkout"]
    assert event.meta["codes_removed"] == ["jobs.read"]


async def test_editing_a_seeded_role_leaves_another_orgs_users_untouched(
    rbac_env: Any,
) -> None:
    """THE ONE THAT MATTERS (spec §0.5, falsifier 2).

    `technician` is ONE ROW with `org_id IS NULL`, shared by every tenant.
    Org A edits it; org B's technician must be exactly what it was.

    BOTH assertions read the ADMIN (BYPASSRLS) engine deliberately. Through a
    tenant session org B is invisible whatever happened to it, so a test written
    that way passes with clone-on-write deleted entirely -- which is the defect
    it exists to catch.
    """
    org_a = await rbac_env.create_org("clone-a")
    org_b = await rbac_env.create_org("clone-b")
    a_tech = await rbac_env.create_user(org_a, "technician")
    b_tech = await rbac_env.create_user(org_b, "technician")
    a_tech_id = await _user_id(rbac_env.admin_engine, a_tech)
    b_tech_id = await _user_id(rbac_env.admin_engine, b_tech)
    admin = await rbac_env.create_user(org_a, "org-admin")
    headers = await rbac_env.headers(org_a, admin)
    global_tech = await _global_role_id(rbac_env.admin_engine, "technician")

    # Snapshotted BEFORE the edit, as a plain Python value the request cannot
    # reach. Asserting against it afterwards is what makes "org B is unchanged"
    # a real claim rather than a comparison of the database with itself.
    technician_codes = await _codes_of_role(rbac_env.admin_engine, global_tech)
    assert technician_codes and technician_codes != EDITED_CODES
    assert await _effective_codes(rbac_env.admin_engine, b_tech_id) == technician_codes

    response = await rbac_env.client.put(
        f"/api/v1/roles/{global_tech}/permissions",
        headers=headers,
        json={"codes": sorted(EDITED_CODES)},
    )

    assert response.status_code == 200, response.text
    clone_id = UUID(response.json()["role_id"])
    assert clone_id != global_tech
    assert response.json()["affected_users"] == 1

    # Org A got what it asked for...
    assert await _effective_codes(rbac_env.admin_engine, a_tech_id) == EDITED_CODES
    # ...and org B is UNCHANGED, still on the global row, which is unchanged.
    assert await _effective_codes(rbac_env.admin_engine, b_tech_id) == technician_codes
    assert await _codes_of_role(rbac_env.admin_engine, global_tech) == technician_codes


async def test_put_refuses_super_admin(rbac_env: Any) -> None:
    """Refused in the SERVICE layer so the operator gets a clean 4xx. The
    database function refuses it too (`test_rbac_matrix_function.py`); this is
    the belt to that braces, and the reason the response is a 403 rather than a
    500 with a Postgres exception in it."""
    org = await rbac_env.create_org("put-su")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    su = await _global_role_id(rbac_env.admin_engine, "super-admin")
    before = await _codes_of_role(rbac_env.admin_engine, su)

    response = await rbac_env.client.put(
        f"/api/v1/roles/{su}/permissions", headers=headers, json={"codes": ["devices.read"]}
    )

    assert response.status_code == 403, response.text
    assert await _codes_of_role(rbac_env.admin_engine, su) == before
    # And no clone of it was created either -- a clone named `super-admin`
    # inside an org would be exactly the tenant-escalation path §1.3 refuses.
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert (
            await session.execute(
                select(Role).where(Role.name == "super-admin", Role.org_id == org)
            )
        ).scalar_one_or_none() is None


async def test_put_refuses_an_unknown_code_and_changes_nothing(rbac_env: Any) -> None:
    org = await rbac_env.create_org("put-typo")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(rbac_env.admin_engine, org, "typo-target", ["devices.read"])

    response = await rbac_env.client.put(
        f"/api/v1/roles/{role_id}/permissions",
        headers=headers,
        json={"codes": ["jobs.read", "jobs.raed"]},
    )

    assert response.status_code == 422, response.text
    assert "jobs.raed" in response.text
    assert await _codes_of_role(rbac_env.admin_engine, role_id) == {"devices.read"}


async def test_a_caller_cannot_remove_their_own_roles_manage(rbac_env: Any) -> None:
    """Self-lockout (§1.4). Without this the first mis-click is unrecoverable
    without SQL on production.

    The caller holds the GLOBAL `org-admin` role, so this exercises the rail
    THROUGH clone-on-write: the role is cloned, the caller's assignment is
    repointed at the clone, and only then does the code set turn out to strand
    them. The whole transaction must roll back -- including the clone."""
    org = await rbac_env.create_org("lockout")
    admin = await rbac_env.create_user(org, "org-admin")
    actor_id = await _user_id(rbac_env.admin_engine, admin)
    headers = await rbac_env.headers(org, admin)
    org_admin = await _global_role_id(rbac_env.admin_engine, "org-admin")
    before = await _effective_codes(rbac_env.admin_engine, actor_id)
    assert "roles.manage" in before

    response = await rbac_env.client.put(
        f"/api/v1/roles/{org_admin}/permissions",
        headers=headers,
        json={"codes": ["devices.read"]},
    )

    assert response.status_code == 403, response.text
    assert "roles.manage" in response.text
    # Nothing applied, and no orphaned clone left behind.
    assert await _effective_codes(rbac_env.admin_engine, actor_id) == before
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert (
            await session.execute(
                select(Role).where(Role.name == "org-admin", Role.org_id == org)
            )
        ).scalar_one_or_none() is None


async def test_a_caller_cannot_remove_their_own_orgs_manage(rbac_env: Any) -> None:
    """The second guarded code. `orgs.manage` is what gates the org-settings
    write and remote-tunnel authorization; losing it is as unrecoverable as
    losing `roles.manage`."""
    org = await rbac_env.create_org("lockout-orgs")
    role_id = await _seed_role(
        rbac_env.admin_engine, org, "self-admin", ["roles.manage", "orgs.manage"]
    )
    actor = await rbac_env.create_user(org)
    actor_id = await _user_id(rbac_env.admin_engine, actor)
    await _assign(rbac_env.admin_engine, org, actor_id, role_id)
    headers = await rbac_env.headers(org, actor)

    response = await rbac_env.client.put(
        f"/api/v1/roles/{role_id}/permissions",
        headers=headers,
        json={"codes": ["roles.manage"]},
    )

    assert response.status_code == 403, response.text
    assert "orgs.manage" in response.text
    assert await _codes_of_role(rbac_env.admin_engine, role_id) == {
        "roles.manage",
        "orgs.manage",
    }


async def test_a_caller_may_edit_a_role_they_do_not_hold(rbac_env: Any) -> None:
    """NON-VACUITY for the rail above. If the lockout check were written too
    broadly -- refusing any removal of a guarded code from any role -- the two
    tests above would still pass while the surface became unusable."""
    org = await rbac_env.create_org("lockout-other")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    other = await _seed_role(rbac_env.admin_engine, org, "deputy", ["roles.manage"])

    response = await rbac_env.client.put(
        f"/api/v1/roles/{other}/permissions", headers=headers, json={"codes": ["devices.read"]}
    )

    assert response.status_code == 200, response.text
    assert await _codes_of_role(rbac_env.admin_engine, other) == {"devices.read"}


async def test_cannot_set_permissions_on_another_orgs_role(rbac_env: Any) -> None:
    mine = await rbac_env.create_org("put-mine")
    theirs = await rbac_env.create_org("put-theirs")
    theirs_role = await _seed_role(rbac_env.admin_engine, theirs, "theirs", ["devices.read"])
    admin = await rbac_env.create_user(mine, "org-admin")
    headers = await rbac_env.headers(mine, admin)

    response = await rbac_env.client.put(
        f"/api/v1/roles/{theirs_role}/permissions",
        headers=headers,
        json={"codes": ["credentials.checkout"]},
    )

    assert response.status_code == 404, response.text
    assert await _codes_of_role(rbac_env.admin_engine, theirs_role) == {"devices.read"}


async def test_setting_permissions_on_an_unknown_role_is_a_404(rbac_env: Any) -> None:
    org = await rbac_env.create_org("put-missing")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)

    response = await rbac_env.client.put(
        f"/api/v1/roles/{uuid4()}/permissions", headers=headers, json={"codes": []}
    )

    assert response.status_code == 404, response.text


async def test_the_route_predicate_refuses_another_orgs_role_without_rls_to_mask_it(
    rbac_env: Any,
) -> None:
    """The write-side twin of `test_the_route_predicate_scopes_roles_without_rls_
    to_mask_it`, and it is not redundant with the three cross-org tests above.

    `roles` carries RLS (migration 0002), so those tests pass EVEN IF the
    service's own `org_id` predicate is deleted -- verified by planting exactly
    that defect and watching all three stay green. Here `app.state.app_engine`
    is repointed at the BYPASSRLS admin engine and the REAL routes are driven
    through it, so nothing but the application predicate is left standing.
    """
    mine = await rbac_env.create_org("wpred-mine")
    theirs = await rbac_env.create_org("wpred-theirs")
    theirs_role = await _seed_role(rbac_env.admin_engine, theirs, "untouchable", ["devices.read"])
    admin = await rbac_env.create_user(mine, "org-admin")
    headers = await rbac_env.headers(mine, admin)

    rbac_env.app.state.app_engine = rbac_env.admin_engine
    try:
        patched = await rbac_env.client.patch(
            f"/api/v1/roles/{theirs_role}", headers=headers, json={"name": "hijacked"}
        )
        put = await rbac_env.client.put(
            f"/api/v1/roles/{theirs_role}/permissions",
            headers=headers,
            json={"codes": ["credentials.checkout"]},
        )
        deleted = await rbac_env.client.delete(
            f"/api/v1/roles/{theirs_role}", headers=headers
        )
    finally:
        rbac_env.app.state.app_engine = rbac_env.app_engine

    assert patched.status_code == 404, patched.text
    assert put.status_code == 404, put.text
    assert deleted.status_code == 404, deleted.text
    async with AsyncSession(rbac_env.admin_engine) as session:
        survivor = await session.get(Role, theirs_role)
        assert survivor is not None
        assert survivor.name == "untouchable"
    assert await _codes_of_role(rbac_env.admin_engine, theirs_role) == {"devices.read"}


# --------------------------------------------------------------------------- #
# DELETE /roles/{id}
# --------------------------------------------------------------------------- #


async def test_delete_removes_an_unassigned_org_role_and_audits(rbac_env: Any) -> None:
    org = await rbac_env.create_org("del")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(rbac_env.admin_engine, org, "disposable", ["devices.read"])

    response = await rbac_env.client.delete(f"/api/v1/roles/{role_id}", headers=headers)

    assert response.status_code == 204, response.text
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert await session.get(Role, role_id) is None
    assert await _codes_of_role(rbac_env.admin_engine, role_id) == set()
    events = [
        e for e in await _audit_actions(rbac_env.admin_engine, org) if e.action == "role.deleted"
    ]
    assert len(events) == 1
    assert events[0].target_id == str(role_id)
    assert events[0].meta["role_name"] == "disposable"


async def test_delete_refuses_a_global_role(rbac_env: Any) -> None:
    """Cloning to EDIT is coherent; cloning to DELETE is not. A global role is
    one row shared by every tenant, so "delete technician" from one org either
    deletes another tenant's role or does nothing at all -- there is no
    org-scoped meaning to give it, so it is refused rather than guessed at."""
    org = await rbac_env.create_org("del-global")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    global_tech = await _global_role_id(rbac_env.admin_engine, "technician")

    response = await rbac_env.client.delete(f"/api/v1/roles/{global_tech}", headers=headers)

    assert response.status_code == 409, response.text
    # THE MESSAGE IS PART OF THE ASSERTION, and not as decoration. Strip the
    # service-layer refusal and the request STILL 409s -- `set_role_permissions`
    # refuses a global role too ("must be cloned before editing"), so a test
    # asserting only the status passes with this rail deleted. Pinning which
    # refusal fired is what tells the two apart, and it is also the difference
    # between an operator reading an explanation and reading a Postgres error.
    assert "shared by every organization" in response.json()["detail"]
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert await session.get(Role, global_tech) is not None


async def test_delete_refuses_super_admin(rbac_env: Any) -> None:
    org = await rbac_env.create_org("del-su")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    su = await _global_role_id(rbac_env.admin_engine, "super-admin")

    response = await rbac_env.client.delete(f"/api/v1/roles/{su}", headers=headers)

    assert response.status_code == 403, response.text
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert await session.get(Role, su) is not None


async def test_delete_refuses_a_role_users_still_hold(rbac_env: Any) -> None:
    """Which is also what makes lockout-by-deletion impossible: a caller holds
    the role that grants them `roles.manage`, so the role is assigned, so the
    delete is refused. The count is in the message because "you cannot" without
    "because two people hold it" is an error the operator cannot act on."""
    org = await rbac_env.create_org("del-held")
    role_id = await _seed_role(rbac_env.admin_engine, org, "held", ["roles.manage"])
    actor = await rbac_env.create_user(org)
    actor_id = await _user_id(rbac_env.admin_engine, actor)
    await _assign(rbac_env.admin_engine, org, actor_id, role_id)
    headers = await rbac_env.headers(org, actor)

    response = await rbac_env.client.delete(f"/api/v1/roles/{role_id}", headers=headers)

    assert response.status_code == 409, response.text
    assert "1" in response.json()["detail"]
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert await session.get(Role, role_id) is not None
    assert await _effective_codes(rbac_env.admin_engine, actor_id) == {"roles.manage"}


async def test_cannot_delete_another_orgs_role(rbac_env: Any) -> None:
    mine = await rbac_env.create_org("del-mine")
    theirs = await rbac_env.create_org("del-theirs")
    theirs_role = await _seed_role(rbac_env.admin_engine, theirs, "safe", ["devices.read"])
    admin = await rbac_env.create_user(mine, "org-admin")
    headers = await rbac_env.headers(mine, admin)

    response = await rbac_env.client.delete(f"/api/v1/roles/{theirs_role}", headers=headers)

    assert response.status_code == 404, response.text
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert await session.get(Role, theirs_role) is not None


# --------------------------------------------------------------------------- #
# Name collisions AFTER clone-on-write
#
# Once an org has cloned `technician`, the name `technician` matches TWO rows
# visible to that org: the global one and the org's copy. Both routes that ask
# "does this name already exist here" were asking it with `scalar_one_or_none()`
# over exactly that predicate, so the second row turned a 409 into a
# `MultipleResultsFound` -- a 500 on a duplicate-name refusal, which is the
# single most ordinary thing an operator can do wrong on this screen.
# --------------------------------------------------------------------------- #


async def _clone_technician(rbac_env: Any, org: UUID, headers: dict[str, str]) -> UUID:
    """Fire clone-on-write for `technician` and return the org's copy's id."""
    global_tech = await _global_role_id(rbac_env.admin_engine, "technician")
    response = await rbac_env.client.patch(
        f"/api/v1/roles/{global_tech}", headers=headers, json={"description": "ours"}
    )
    assert response.status_code == 200, response.text
    clone_id = UUID(response.json()["id"])
    assert clone_id != global_tech
    return clone_id


async def test_create_refuses_a_cloned_name_with_409_not_a_crash(rbac_env: Any) -> None:
    """`test_create_refuses_a_name_that_already_resolves` checks the global name
    BEFORE any clone exists, so only one row matches and the ambiguity never
    arises. This one clones first, which is the state every org is in the moment
    it edits a seeded role."""
    org = await rbac_env.create_org("dup-after-clone")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    body = {"name": "technician", "description": "", "codes": []}

    # Before the clone, one row matches and the refusal says which kind it is.
    first = await rbac_env.client.post("/api/v1/roles", headers=headers, json=body)
    assert first.status_code == 409, first.text
    assert "platform-wide" in first.json()["detail"]

    clone_id = await _clone_technician(rbac_env, org, headers)

    response = await rbac_env.client.post("/api/v1/roles", headers=headers, json=body)

    assert response.status_code == 409, response.text
    # Now TWO rows match, and the refusal must name the one the org actually
    # resolves `technician` to -- its own copy. Reading "it is a platform-wide
    # role" here would send the operator looking for a row they cannot edit,
    # and it is also how a query that picked whichever row Postgres returned
    # first would show up.
    assert "platform-wide" not in response.json()["detail"]
    # And nothing was created: still exactly one local `technician`, the clone.
    async with AsyncSession(rbac_env.admin_engine) as session:
        local = list(
            (
                await session.execute(
                    select(Role.id).where(Role.name == "technician", Role.org_id == org)
                )
            ).scalars()
        )
    assert local == [clone_id]


async def test_patch_refuses_renaming_onto_a_cloned_name_with_409_not_a_crash(
    rbac_env: Any,
) -> None:
    """The same ambiguity on the rename path."""
    org = await rbac_env.create_org("rename-after-clone")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    await _clone_technician(rbac_env, org, headers)
    other = await _seed_role(rbac_env.admin_engine, org, "night-shift", ["devices.read"])

    response = await rbac_env.client.patch(
        f"/api/v1/roles/{other}", headers=headers, json={"name": "technician"}
    )

    assert response.status_code == 409, response.text
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert (await session.get(Role, other)).name == "night-shift"


# --------------------------------------------------------------------------- #
# Clone-on-write fires ONCE
# --------------------------------------------------------------------------- #


async def test_a_second_write_to_a_global_role_id_reuses_the_existing_clone(
    rbac_env: Any,
) -> None:
    """RLS's `roles_select` policy is `org_id IS NULL OR org_id = current_org`,
    so the GLOBAL `technician` row stays visible to the org after cloning and
    the console may well address it again. Cloning a second time would violate
    `UniqueConstraint("org_id", "name")` -- and if that constraint were ever
    relaxed, it would silently split the org's technicians across two rows.

    So: two writes to the same global id, one clone, and the second write landed
    on it."""
    org = await rbac_env.create_org("reclone")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    holder = await rbac_env.create_user(org, "technician")
    holder_id = await _user_id(rbac_env.admin_engine, holder)
    global_tech = await _global_role_id(rbac_env.admin_engine, "technician")

    first = await rbac_env.client.patch(
        f"/api/v1/roles/{global_tech}", headers=headers, json={"description": "first"}
    )
    assert first.status_code == 200, first.text
    second = await rbac_env.client.patch(
        f"/api/v1/roles/{global_tech}", headers=headers, json={"description": "second"}
    )
    assert second.status_code == 200, second.text

    assert second.json()["id"] == first.json()["id"]
    clone_id = UUID(first.json()["id"])
    assert clone_id != global_tech
    # Exactly ONE local `technician`, carrying the SECOND write.
    async with AsyncSession(rbac_env.admin_engine) as session:
        local = list(
            (
                await session.execute(
                    select(Role).where(Role.name == "technician", Role.org_id == org)
                )
            ).scalars()
        )
        assert [r.id for r in local] == [clone_id]
        assert local[0].description == "second"
        # The holder is on that one row, not split across two.
        assert set(
            (
                await session.execute(
                    select(UserRole.role_id).where(UserRole.user_id == holder_id)
                )
            ).scalars()
        ) == {clone_id}
    # And the clone was recorded once, not twice.
    cloned = [
        e for e in await _audit_actions(rbac_env.admin_engine, org) if e.action == "role.cloned"
    ]
    assert len(cloned) == 1


async def test_a_matrix_write_after_a_patch_reuses_the_same_clone(rbac_env: Any) -> None:
    """The same guard across the two different routes, which is how an operator
    actually hits it: rename the seeded role, then set its codes."""
    org = await rbac_env.create_org("reclone-put")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    global_tech = await _global_role_id(rbac_env.admin_engine, "technician")
    clone_id = await _clone_technician(rbac_env, org, headers)

    response = await rbac_env.client.put(
        f"/api/v1/roles/{global_tech}/permissions",
        headers=headers,
        json={"codes": sorted(EDITED_CODES)},
    )

    assert response.status_code == 200, response.text
    assert response.json()["role_id"] == str(clone_id)
    assert await _codes_of_role(rbac_env.admin_engine, clone_id) == EDITED_CODES
    async with AsyncSession(rbac_env.admin_engine) as session:
        local = list(
            (
                await session.execute(
                    select(Role.id).where(Role.name == "technician", Role.org_id == org)
                )
            ).scalars()
        )
    assert local == [clone_id]


# --------------------------------------------------------------------------- #
# Audit (invariant #4) -- the two events nothing asserted
# --------------------------------------------------------------------------- #


async def test_patch_records_a_role_updated_event(rbac_env: Any) -> None:
    """Invariant #4: every privileged action is written to the hash-chained
    audit log. A rename redefines what a role means for everyone holding it, so
    the event has to name both spellings -- "role.updated" alone does not tell
    an investigator what changed."""
    org = await rbac_env.create_org("patch-audit")
    admin = await rbac_env.create_user(org, "org-admin")
    actor_id = await _user_id(rbac_env.admin_engine, admin)
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(rbac_env.admin_engine, org, "before-name", ["devices.read"])

    response = await rbac_env.client.patch(
        f"/api/v1/roles/{role_id}",
        headers=headers,
        json={"name": "after-name", "description": "redescribed"},
    )
    assert response.status_code == 200, response.text

    events = [
        e for e in await _audit_actions(rbac_env.admin_engine, org) if e.action == "role.updated"
    ]
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "user"
    assert event.actor_id == actor_id
    assert event.target_type == "role"
    assert event.target_id == str(role_id)
    assert event.meta["name_from"] == "before-name"
    assert event.meta["name_to"] == "after-name"
    assert event.meta["description_changed"] is True


async def test_cloning_a_seeded_role_records_a_role_cloned_event(rbac_env: Any) -> None:
    """The clone is the moment the org stops sharing the platform's row, and it
    happens as a SIDE EFFECT of an ordinary edit -- the operator did not ask for
    it. An audit trail in which a new role appears with no event explaining it
    is one an investigator cannot read."""
    org = await rbac_env.create_org("clone-audit")
    admin = await rbac_env.create_user(org, "org-admin")
    actor_id = await _user_id(rbac_env.admin_engine, admin)
    headers = await rbac_env.headers(org, admin)
    global_tech = await _global_role_id(rbac_env.admin_engine, "technician")
    technician_codes = await _codes_of_role(rbac_env.admin_engine, global_tech)
    assert technician_codes  # non-vacuous

    clone_id = await _clone_technician(rbac_env, org, headers)

    events = [
        e for e in await _audit_actions(rbac_env.admin_engine, org) if e.action == "role.cloned"
    ]
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "user"
    assert event.actor_id == actor_id
    assert event.target_type == "role"
    assert event.target_id == str(clone_id)
    assert event.meta["role_name"] == "technician"
    assert event.meta["cloned_from"] == str(global_tech)
    assert set(event.meta["codes"]) == technician_codes


# --------------------------------------------------------------------------- #
# `super-admin` on the RENAME target
# --------------------------------------------------------------------------- #


async def test_patch_refuses_renaming_a_role_to_super_admin(rbac_env: Any) -> None:
    """`super-admin` is refused as a NAME, not only as a target row. Without
    this the org could mint its own `super-admin` by renaming a role it already
    owns -- and while that row grants nothing by itself, `list_roles` and the
    database function both key the rail on the NAME, so the result is a role
    this surface can no longer read, edit or delete.

    A 403, distinctly: the global `super-admin` row would also collide on the
    name and produce a 409, which is the wrong answer with the right status."""
    org = await rbac_env.create_org("rename-su")
    admin = await rbac_env.create_user(org, "org-admin")
    headers = await rbac_env.headers(org, admin)
    role_id = await _seed_role(rbac_env.admin_engine, org, "ordinary", ["devices.read"])

    response = await rbac_env.client.patch(
        f"/api/v1/roles/{role_id}", headers=headers, json={"name": "super-admin"}
    )

    assert response.status_code == 403, response.text
    assert "cross-org platform administration" in response.json()["detail"]
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert (await session.get(Role, role_id)).name == "ordinary"


# --------------------------------------------------------------------------- #
# The org may not be left with nobody who can administer it (spec §1.4)
# --------------------------------------------------------------------------- #


async def test_a_write_may_not_leave_the_org_with_no_orgs_manage_holder(
    rbac_env: Any,
) -> None:
    """The self-lockout rail guards the CALLER's own codes, and that is not the
    whole of §1.4. A caller holding `roles.manage` through some OTHER role can
    edit `org-admin` -- cloning it on the way -- and strip `orgs.manage` from
    every administrator in the org while keeping their own access to this
    screen. The org is then unadministrable and the caller sees a 200.

    So the rail is measured org-wide as well as per-caller: if `orgs.manage` was
    held by somebody in this org before the write and by nobody after it, the
    whole transaction rolls back."""
    org = await rbac_env.create_org("last-admin")
    deputy_role = await _seed_role(rbac_env.admin_engine, org, "deputy", ["roles.manage"])
    caller = await rbac_env.create_user(org)
    caller_id = await _user_id(rbac_env.admin_engine, caller)
    await _assign(rbac_env.admin_engine, org, caller_id, deputy_role)
    headers = await rbac_env.headers(org, caller)

    admin = await rbac_env.create_user(org, "org-admin")
    admin_id = await _user_id(rbac_env.admin_engine, admin)
    org_admin = await _global_role_id(rbac_env.admin_engine, "org-admin")
    admin_codes = await _effective_codes(rbac_env.admin_engine, admin_id)
    assert "orgs.manage" in admin_codes

    response = await rbac_env.client.put(
        f"/api/v1/roles/{org_admin}/permissions",
        headers=headers,
        json={"codes": ["devices.read"]},
    )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert "orgs.manage" in detail
    # Distinguishable from the self-lockout refusal, which is a different rail
    # with a different remedy -- the caller here loses nothing of their own.
    assert "your own" not in detail
    # Nothing applied, and no orphaned clone of `org-admin` left behind.
    assert await _effective_codes(rbac_env.admin_engine, admin_id) == admin_codes
    assert await _effective_codes(rbac_env.admin_engine, caller_id) == {"roles.manage"}
    async with AsyncSession(rbac_env.admin_engine) as session:
        assert (
            await session.execute(
                select(Role).where(Role.name == "org-admin", Role.org_id == org)
            )
        ).scalar_one_or_none() is None


async def test_orgs_manage_may_be_removed_while_another_holder_remains(
    rbac_env: Any,
) -> None:
    """NON-VACUITY for the rail above. Written as "the org has no holder left",
    not "nobody may ever remove `orgs.manage`" -- the broader reading would make
    a perfectly ordinary edit impossible and both rail tests would still pass."""
    org = await rbac_env.create_org("not-last-admin")
    deputy_role = await _seed_role(rbac_env.admin_engine, org, "deputy", ["roles.manage"])
    caller = await rbac_env.create_user(org)
    caller_id = await _user_id(rbac_env.admin_engine, caller)
    await _assign(rbac_env.admin_engine, org, caller_id, deputy_role)
    headers = await rbac_env.headers(org, caller)

    demoted = await _seed_role(rbac_env.admin_engine, org, "site-admin", ["orgs.manage"])
    demoted_user = await rbac_env.create_user(org)
    await _assign(
        rbac_env.admin_engine, org, await _user_id(rbac_env.admin_engine, demoted_user), demoted
    )
    # Somebody else in the org still holds it afterwards.
    keeper_role = await _seed_role(rbac_env.admin_engine, org, "keeper", ["orgs.manage"])
    keeper = await rbac_env.create_user(org)
    await _assign(
        rbac_env.admin_engine, org, await _user_id(rbac_env.admin_engine, keeper), keeper_role
    )

    response = await rbac_env.client.put(
        f"/api/v1/roles/{demoted}/permissions", headers=headers, json={"codes": ["devices.read"]}
    )

    assert response.status_code == 200, response.text
    assert await _codes_of_role(rbac_env.admin_engine, demoted) == {"devices.read"}
