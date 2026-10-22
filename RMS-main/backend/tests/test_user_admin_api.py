"""The user-lifecycle API: `/api/v1/users`.

Spec `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2.

The five rules this surface exists to keep, and the test that would notice each
one breaking:

1. **No password ever transits ITMS.** "Reset password" is a call to Keycloak's
   `execute-actions-email` with `UPDATE_PASSWORD`; nothing here generates,
   stores, displays or returns a credential.
   `test_no_route_returns_anything_password_shaped` drives every route on the
   surface and scans the bodies, so a future route cannot quietly join without
   being covered.
2. **Delete removes the Keycloak identity only.** 89 foreign-key columns across
   21 tables reference `users.id`, audit `actor_id` among them (§0.6), so the
   ITMS row is marked `deleted` and RETAINED.
   `test_a_deleted_users_audit_history_is_still_resolvable` is the assertion
   that matters -- a row that survives but whose history no longer joins would
   pass a naive "the row is still there" test.
3. **Creation is Keycloak first, then the ITMS row**, because Keycloak's create
   returns the id that IS the `sub` claim. Both failure orderings are tested:
   a refused Keycloak create leaves NO ITMS row, and a failed ITMS write leaves
   an account with no roles -- the state of any federated user before first
   login, which `deps.py` already self-heals.
4. **The domain rule is enforced here too.** A console that refuses an address
   at sign-in but mints it in the create form is not enforcing anything.
5. **SMTP may be absent.** `deploy/keycloak/realm-it-suite.json` has no
   `smtpServer`. Creation succeeds, the enrolment mail is ATTEMPTED, and a
   failure is REPORTED in the response -- plus a resend route, without which one
   transient mail failure leaves an account nobody can ever sign into.

TENANCY. `test_the_route_predicate_scopes_users_without_rls_to_mask_it`,
`test_another_orgs_user_cannot_be_acted_on` and
`test_no_keycloak_touching_route_reaches_another_orgs_identity` all repoint the
app engine at the BYPASSRLS ADMIN engine and drive the REAL routes through it.
Through a tenant session RLS hides the other org whether or not the application
predicate is there, so those tests would pass with every predicate deleted --
which is precisely the bug they exist to catch. The third is the one that
matters most: the ITMS half of a write is tenant-scoped and the Keycloak half
cannot be, because `manage-users` is realm-wide, so the subject handed to
`kc_client` has to come off the tenant-scoped row and nowhere else.

THE RAILS ARE TESTED AT EVERY DOOR TO THE SAME END STATE, not at one of them.
`org-admin` has three (revoke, disable, delete) and so does `super-admin`, which
is refused at grant time AND at all three undos -- no console surface can grant
it back, so its removal is irreversible in a way `technician`'s is not.
`test_the_super_admin_rail_refuses_that_role_and_not_every_user` is the
non-vacuity half: a rail that refused those three calls unconditionally would
satisfy every other assertion and break the surface.

WHY THE ROUTER IS INCLUDED BY A FIXTURE. This workstream owns
`app/modules/user_admin/api.py` and not `app/main.py`; the controller wires the
router in afterwards. `users_env` includes it into the per-test app only if it
is not already there, so these tests pass both before and after that wiring
lands, and never register the router twice.

THE KEYCLOAK HALF IS A MOCK TRANSPORT, exactly as `keycloak_events` is, and
spec §6 already says what that does not prove: nothing here has run against a
live Keycloak. What it does prove is that the ORDERING, the rails, the audit and
the tenancy are what this application does.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.rbac import route_permission
from app.core.router_inspect import flatten
from app.models import AuditEvent, DeviceGroup, Role, User, UserRole
from app.modules.user_admin.api import keycloak_config
from app.modules.user_admin.api import router as users_router
from app.modules.user_admin.kc_client import KeycloakAdminConfig

#: Captured at import, before any fixture swaps the name -- see
#: `tests/test_keycloak_event_client.py` for why a factory that resolved
#: `httpx.AsyncClient` by name would recurse into itself.
_REAL_CLIENT = httpx.AsyncClient

CONFIG = KeycloakAdminConfig(
    base_url="https://keycloak.test",
    realm="it-suite",
    client_id="it-suite-provisioning",
    client_secret="not-a-real-secret",
)


class FakeKeycloak:
    """A Keycloak that records what it was asked and answers what the test says.

    Each response status is a separate knob because each of them is a DIFFERENT
    real failure with a different correct handling: a taken email (409 on
    create) must leave no ITMS row, a realm with no SMTP (500 on the mail) must
    leave the user created and say so, and a revoked credential (403 on the
    enable) must roll the ITMS status back.
    """

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.create_status = 201
        self.mail_status = 204
        self.update_status = 204
        self.delete_status = 204
        self.issued: list[str] = []
        #: Force the id the next create returns. The only reason a test needs
        #: this is to collide with a row that already exists, which is how the
        #: "ITMS half fails" ordering is exercised.
        self.next_id: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "tok-123"})
        self.seen.append(request)
        if path.endswith("/execute-actions-email"):
            return httpx.Response(self.mail_status)
        if request.method == "POST" and path.endswith("/users"):
            new_id = self.next_id or str(uuid4())
            self.next_id = None
            self.issued.append(new_id)
            return httpx.Response(
                self.create_status,
                headers={"Location": f"{CONFIG.base_url}/admin/realms/it-suite/users/{new_id}"},
            )
        if request.method == "PUT":
            return httpx.Response(self.update_status)
        if request.method == "DELETE":
            return httpx.Response(self.delete_status)
        raise AssertionError(f"unexpected call: {request.method} {path}")

    # -- readers the tests assert on ---------------------------------------- #

    def calls(self, method: str, suffix: str = "") -> list[httpx.Request]:
        return [
            r for r in self.seen if r.method == method and r.url.path.endswith(suffix)
        ]

    @property
    def mails(self) -> list[httpx.Request]:
        return self.calls("PUT", "/execute-actions-email")


@pytest.fixture()
def users_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    paths = {path for path, _route in flatten(api_env.app.router)}
    if "/api/v1/users" not in paths:
        api_env.app.include_router(users_router, prefix="/api/v1")

    kc = FakeKeycloak()

    def factory(**kw: Any) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(kc.handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    api_env.app.dependency_overrides[keycloak_config] = lambda: CONFIG
    api_env.kc = kc
    yield api_env
    api_env.app.dependency_overrides.pop(keycloak_config, None)


# --------------------------------------------------------------------------- #
# helpers -- every one reads through the ADMIN (BYPASSRLS) engine, so an
# assertion about what a request did or did not change is never itself filtered
# by the mechanism under test.
# --------------------------------------------------------------------------- #


async def _user_id(engine: AsyncEngine, sub: str) -> UUID:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _user_row(engine: AsyncEngine, user_id: UUID) -> User | None:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()


async def _user_by_email(engine: AsyncEngine, email: str) -> User | None:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()


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
    from sqlalchemy import text

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


async def _assign(
    engine: AsyncEngine,
    org_id: UUID,
    user_id: UUID,
    role_id: UUID,
    device_group_id: UUID | None = None,
) -> UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = UserRole(
            org_id=org_id,
            user_id=user_id,
            role_id=role_id,
            device_group_id=device_group_id,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _device_group(engine: AsyncEngine, org_id: UUID, name: str) -> UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        group = DeviceGroup(org_id=org_id, name=name)
        session.add(group)
        await session.commit()
        return group.id


async def _assignments(engine: AsyncEngine, user_id: UUID) -> list[UserRole]:
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.execute(select(UserRole).where(UserRole.user_id == user_id))
            ).scalars()
        )


async def _audit(engine: AsyncEngine, org_id: UUID) -> list[AuditEvent]:
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


async def _admin(env: Any, org: UUID) -> tuple[str, dict[str, str], UUID]:
    """An `org-admin` in `org`: their sub, their auth headers, and their row id."""
    sub = await env.create_user(org, "org-admin")
    headers = await env.headers(org, sub)
    return sub, headers, await _user_id(env.admin_engine, sub)


async def _headers_claiming(env: Any, org: UUID, sub: str, email: str) -> dict[str, str]:
    """Auth headers whose token asserts a particular email claim.

    Needed by the domain test and by nothing else: switching
    `console_email_domain` on makes `app/api/deps.py`'s own admission gate
    (door 1 of 3) refuse the CALLER too if their token's address does not pass.
    That is spec §3.3's lockout reproduced in miniature -- and it is why the
    preflight is a required step in `deploy/README.md` rather than a suggestion.
    """
    slug = await env.org_slug(org)
    return {"Authorization": f"Bearer {env.idp.token(sub, slug, email=email)}"}


def _listed(body: dict[str, Any], user_id: UUID) -> dict[str, Any] | None:
    for user in body["users"]:
        if user["id"] == str(user_id):
            return user
    return None


# --------------------------------------------------------------------------- #
# RBAC (invariant #6) -- derived from the router, never hand-listed
# --------------------------------------------------------------------------- #


def test_every_route_carries_the_intended_permission_gate() -> None:
    app = FastAPI()
    app.include_router(users_router, prefix="/api/v1")
    gates = {
        (method, path): route_permission(route)
        for path, route in flatten(app.router)
        if isinstance(route, APIRoute) and path.startswith("/api/v1")
        for method in sorted(route.methods or set())
    }
    assert gates == {
        ("GET", "/api/v1/users"): "users.read",
        ("GET", "/api/v1/users/{user_id}/effective-permissions"): "users.read",
        ("POST", "/api/v1/users"): "users.manage",
        ("POST", "/api/v1/users/{user_id}/roles"): "users.manage",
        ("DELETE", "/api/v1/users/{user_id}/roles/{assignment_id}"): "users.manage",
        ("POST", "/api/v1/users/{user_id}/disable"): "users.manage",
        ("POST", "/api/v1/users/{user_id}/enable"): "users.manage",
        ("POST", "/api/v1/users/{user_id}/reset-password"): "users.manage",
        ("POST", "/api/v1/users/{user_id}/resend-enrolment"): "users.manage",
        ("DELETE", "/api/v1/users/{user_id}"): "users.manage",
    }


async def test_reads_require_users_read(users_env: Any) -> None:
    org = await users_env.create_org("u-read")
    nobody = await users_env.create_user(org, "read-only")
    headers = await users_env.headers(org, nobody)
    target = await _user_id(users_env.admin_engine, nobody)

    assert (await users_env.client.get("/api/v1/users", headers=headers)).status_code == 403
    assert (
        await users_env.client.get(
            f"/api/v1/users/{target}/effective-permissions", headers=headers
        )
    ).status_code == 403


async def test_writes_require_users_manage(users_env: Any) -> None:
    """`users.read` is NOT enough to write. "Can see who has access" and "can
    grant access" are different levels of trust.

    THE TARGET IS SOMEBODY ELSE, and that is the whole point of the `victim`
    row. An earlier version addressed every route at the CALLER's own id, which
    made the disable and delete assertions unfalsifiable: `_refuse_self` answers
    403 for "you cannot disable your own account" whatever the permission gate
    does, so those two entries stayed green with the gate deleted. A target the
    caller has no rail against leaves the gate as the only thing that can refuse.
    """
    org = await users_env.create_org("u-write")
    reader_role = await _seed_role(users_env.admin_engine, org, "user-reader", ["users.read"])
    reader = await users_env.create_user(org)
    reader_id = await _user_id(users_env.admin_engine, reader)
    await _assign(users_env.admin_engine, org, reader_id, reader_role)
    headers = await users_env.headers(org, reader)
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    # A second `org-admin` exists so the victim is not the org's last one --
    # otherwise `_refuse_stranding_the_org` would answer 403 for its own reason
    # and the disable/delete assertions would be unfalsifiable again.
    await _admin(users_env, org)
    victim = await users_env.create_user(org, "technician")
    victim_id = await _user_id(users_env.admin_engine, victim)

    assert (await users_env.client.get("/api/v1/users", headers=headers)).status_code == 200
    refused = [
        await users_env.client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "x@toeshee.com", "display_name": "X", "role_ids": []},
        ),
        await users_env.client.post(
            f"/api/v1/users/{victim_id}/roles", headers=headers, json={"role_id": str(role_id)}
        ),
        await users_env.client.delete(
            f"/api/v1/users/{victim_id}/roles/{uuid4()}", headers=headers
        ),
        await users_env.client.post(f"/api/v1/users/{victim_id}/disable", headers=headers),
        await users_env.client.post(f"/api/v1/users/{victim_id}/enable", headers=headers),
        await users_env.client.post(f"/api/v1/users/{victim_id}/reset-password", headers=headers),
        await users_env.client.post(
            f"/api/v1/users/{victim_id}/resend-enrolment", headers=headers
        ),
        await users_env.client.delete(f"/api/v1/users/{victim_id}", headers=headers),
    ]
    assert [r.status_code for r in refused] == [403] * 8
    # And nothing was attempted against Keycloak on a refused request.
    assert users_env.kc.seen == []
    # Non-vacuity: the victim really was reachable -- nothing about the target
    # itself refused these, only the missing permission.
    row = await _user_row(users_env.admin_engine, victim_id)
    assert row is not None and row.status == "active"


# --------------------------------------------------------------------------- #
# tenancy (invariant #5)
# --------------------------------------------------------------------------- #


async def test_another_orgs_users_are_not_listed(users_env: Any) -> None:
    mine = await users_env.create_org("t-mine")
    theirs = await users_env.create_org("t-theirs")
    _sub, headers, _id = await _admin(users_env, mine)
    stranger = await users_env.create_user(theirs, "technician")
    stranger_id = await _user_id(users_env.admin_engine, stranger)

    body = (await users_env.client.get("/api/v1/users", headers=headers)).json()

    assert _listed(body, stranger_id) is None


async def test_the_route_predicate_scopes_users_without_rls_to_mask_it(
    users_env: Any,
) -> None:
    """RLS on `users` (migration 0002) hides another org's rows even with the
    route's own `org_id` predicate deleted, so this repoints the app engine at
    the BYPASSRLS admin engine and drives the REAL route through it. Nothing but
    the application predicate is left standing.

    Without this, `test_another_orgs_users_are_not_listed` above passes on a
    route that has no tenant filter at all."""
    mine = await users_env.create_org("pred-mine")
    theirs = await users_env.create_org("pred-theirs")
    _sub, headers, _id = await _admin(users_env, mine)
    stranger = await users_env.create_user(theirs, "technician")
    stranger_id = await _user_id(users_env.admin_engine, stranger)

    users_env.app.state.app_engine = users_env.admin_engine
    try:
        body = (await users_env.client.get("/api/v1/users", headers=headers)).json()
    finally:
        users_env.app.state.app_engine = users_env.app_engine

    assert _listed(body, stranger_id) is None


async def test_another_orgs_user_cannot_be_acted_on(users_env: Any) -> None:
    """Same reasoning, on the WRITE side and on the same BYPASSRLS engine: a
    disable route with no tenant predicate would reach across tenants, and RLS
    would hide the evidence in any test that used a tenant session."""
    mine = await users_env.create_org("act-mine")
    theirs = await users_env.create_org("act-theirs")
    _sub, headers, _id = await _admin(users_env, mine)
    stranger = await users_env.create_user(theirs, "technician")
    stranger_id = await _user_id(users_env.admin_engine, stranger)

    users_env.app.state.app_engine = users_env.admin_engine
    try:
        disable = await users_env.client.post(
            f"/api/v1/users/{stranger_id}/disable", headers=headers
        )
        perms = await users_env.client.get(
            f"/api/v1/users/{stranger_id}/effective-permissions", headers=headers
        )
    finally:
        users_env.app.state.app_engine = users_env.app_engine

    assert disable.status_code == 404, disable.text
    assert perms.status_code == 404, perms.text
    row = await _user_row(users_env.admin_engine, stranger_id)
    assert row is not None and row.status == "active"
    assert users_env.kc.seen == []


async def test_no_keycloak_touching_route_reaches_another_orgs_identity(
    users_env: Any,
) -> None:
    """THE CROSS-TENANT DESTRUCTION PATH, and why it needs its own test.

    The ITMS half of every write here is tenant-scoped. The KEYCLOAK half is
    not, and cannot be: `manage-users` is realm-wide and the admin API has no
    notion of an ITMS organization at all (spec §2.1). So the only thing
    standing between org A's administrator and org B's identity is that the
    subject handed to `kc_client` is read off the TENANT-SCOPED `users` row --
    never off the id in the request path. A route that resolved the target any
    other way would delete or disable a real person in another tenant, and the
    ITMS side of the same request would look like a clean 404.

    Driven on the BYPASSRLS ADMIN ENGINE, so RLS is not what produces the
    refusal: with the application predicate deleted this test finds org B's row
    and the assertions below fail. Through a tenant session it would pass either
    way, which is exactly the mistake it exists to catch.

    Every Keycloak-touching route is here, not a sample -- `disable`, `enable`,
    `delete`, `reset-password` and `resend-enrolment` -- because a rail on some
    of them is not a rail.
    """
    mine = await users_env.create_org("kc-mine")
    theirs = await users_env.create_org("kc-theirs")
    _sub, headers, _id = await _admin(users_env, mine)
    stranger = await users_env.create_user(theirs, "technician")
    stranger_id = await _user_id(users_env.admin_engine, stranger)
    stranger_row = await _user_row(users_env.admin_engine, stranger_id)
    assert stranger_row is not None
    stranger_subject = stranger_row.external_subject

    users_env.app.state.app_engine = users_env.admin_engine
    try:
        attempts = {
            "disable": await users_env.client.post(
                f"/api/v1/users/{stranger_id}/disable", headers=headers
            ),
            "enable": await users_env.client.post(
                f"/api/v1/users/{stranger_id}/enable", headers=headers
            ),
            "reset-password": await users_env.client.post(
                f"/api/v1/users/{stranger_id}/reset-password", headers=headers
            ),
            "resend-enrolment": await users_env.client.post(
                f"/api/v1/users/{stranger_id}/resend-enrolment", headers=headers
            ),
            "delete": await users_env.client.delete(
                f"/api/v1/users/{stranger_id}", headers=headers
            ),
        }
    finally:
        users_env.app.state.app_engine = users_env.app_engine

    assert {name: r.status_code for name, r in attempts.items()} == {
        "disable": 404,
        "enable": 404,
        "reset-password": 404,
        "resend-enrolment": 404,
        "delete": 404,
    }, {name: r.text for name, r in attempts.items()}

    # NOTHING was said to Keycloak at all -- not about this subject, not about
    # anything. Asserting the subject specifically as well, because a future
    # refactor that resolved the target correctly but still issued a call would
    # be a different bug worth naming.
    assert users_env.kc.seen == []
    assert not [r for r in users_env.kc.seen if stranger_subject in r.url.path]

    # And org B's row is exactly as it was.
    row = await _user_row(users_env.admin_engine, stranger_id)
    assert row is not None
    assert row.status == "active"
    assert row.external_subject == stranger_subject


# --------------------------------------------------------------------------- #
# GET /users
# --------------------------------------------------------------------------- #


async def test_the_list_carries_each_users_roles_and_the_assignment_id(
    users_env: Any,
) -> None:
    """`assignment_id` is what DELETE addresses. Without it the console would
    have to reconstruct which of a user's rows to remove from (role, group),
    which is exactly the ambiguity `UniqueConstraint(user_id, role_id,
    device_group_id)` allows to exist twice over."""
    org = await users_env.create_org("list")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    assignment_id = await _assign(users_env.admin_engine, org, tech_id, role_id)

    body = (await users_env.client.get("/api/v1/users", headers=headers)).json()

    listed = _listed(body, tech_id)
    assert listed is not None
    assert listed["status"] == "active"
    assert listed["roles"] == [
        {
            "assignment_id": str(assignment_id),
            "role_id": str(role_id),
            "role_name": "technician",
            "device_group_id": None,
        }
    ]


# --------------------------------------------------------------------------- #
# GET /users/{id}/effective-permissions
# --------------------------------------------------------------------------- #


async def test_effective_permissions_names_the_role_that_granted_each_code(
    users_env: Any,
) -> None:
    """Spec §1.5: an access-control UI whose result you have to simulate in your
    head is one you will get wrong. `via_role` is the whole point."""
    org = await users_env.create_org("eff")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)

    body = (
        await users_env.client.get(
            f"/api/v1/users/{tech_id}/effective-permissions", headers=headers
        )
    ).json()

    assert body["user_id"] == str(tech_id)
    granted = {(c["code"], c["via_role"], c["device_group_id"]) for c in body["codes"]}
    assert ("devices.read", "technician", None) in granted
    assert ("jobs.dispatch", "technician", None) in granted
    assert not any(c["code"] == "credentials.checkout" for c in body["codes"])


async def test_a_group_scoped_grant_reports_its_device_group(users_env: Any) -> None:
    """`user_roles.device_group_id` has meant "this grant applies only to that
    group" since the kernel shipped (`app/core/rbac.py`'s docstring), and no
    surface has ever shown it. A view that flattened it would tell an operator a
    technician has `jobs.dispatch` org-wide when they have it for one group."""
    org = await users_env.create_org("eff-scoped")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    group = await _device_group(users_env.admin_engine, org, "branch-office")
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    await _assign(users_env.admin_engine, org, tech_id, role_id, group)

    body = (
        await users_env.client.get(
            f"/api/v1/users/{tech_id}/effective-permissions", headers=headers
        )
    ).json()

    scoped = {c["device_group_id"] for c in body["codes"]}
    assert scoped == {str(group)}


# --------------------------------------------------------------------------- #
# POST /users -- rule 3 (ordering) and rule 5 (SMTP)
# --------------------------------------------------------------------------- #


async def test_create_makes_the_keycloak_identity_first_then_the_itms_row(
    users_env: Any,
) -> None:
    """RULE 3. Keycloak's create returns the new user id in `Location`, and that
    id IS the `sub` claim -- so the ITMS row can be written with the correct
    `external_subject` immediately rather than waiting for a first login to
    reconcile. The assertion IS the ordering: a row whose `external_subject`
    equals the id Keycloak issued could not have been written first."""
    org = await users_env.create_org("create")
    _sub, headers, _id = await _admin(users_env, org)

    response = await users_env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "ada@toeshee.com", "display_name": "Ada Lovelace", "role_ids": []},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    row = await _user_row(users_env.admin_engine, UUID(body["id"]))
    assert row is not None
    assert row.external_subject == users_env.kc.issued[0]
    assert row.email == "ada@toeshee.com"
    assert row.display_name == "Ada Lovelace"
    assert row.status == "active"
    assert row.org_id == org


async def test_create_assigns_the_requested_roles_and_audits(users_env: Any) -> None:
    org = await users_env.create_org("create-roles")
    _sub, headers, actor_id = await _admin(users_env, org)
    role_id = await _global_role_id(users_env.admin_engine, "technician")

    body = (
        await users_env.client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "email": "grace@toeshee.com",
                "display_name": "Grace",
                "role_ids": [str(role_id)],
            },
        )
    ).json()

    new_id = UUID(body["id"])
    assignments = await _assignments(users_env.admin_engine, new_id)
    assert [a.role_id for a in assignments] == [role_id]
    assert body["roles"][0]["role_name"] == "technician"
    created = [e for e in await _audit(users_env.admin_engine, org) if e.action == "user.created"]
    assert len(created) == 1
    assert created[0].actor_id == actor_id
    assert created[0].target_id == str(new_id)
    assert created[0].meta["email"] == "grace@toeshee.com"


async def test_create_sends_the_enrolment_email_and_reports_that_it_went(
    users_env: Any,
) -> None:
    import json

    org = await users_env.create_org("create-mail")
    _sub, headers, _id = await _admin(users_env, org)

    body = (
        await users_env.client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "ada@toeshee.com", "display_name": "Ada", "role_ids": []},
        )
    ).json()

    assert body["enrolment_email_sent"] is True
    assert body["enrolment_email_error"] is None
    assert len(users_env.kc.mails) == 1
    actions = json.loads(users_env.kc.mails[0].content)
    assert "webauthn-register-passwordless" in actions


async def test_a_created_user_whose_enrolment_email_fails_is_still_created_and_says_so(
    users_env: Any,
) -> None:
    """RULE 5, AND THE PATH THIS DEPLOYMENT IS ON TODAY.
    `deploy/keycloak/realm-it-suite.json` has no `smtpServer`, so this is what
    the operator's first attempt does. The account must EXIST -- rolling it back
    would leave a Keycloak identity with no ITMS row on the very failure mode
    the runbook expects -- and the failure must be VISIBLE, because a "user
    created" that quietly sent no mail is an account nobody can ever sign into
    with every status green."""
    org = await users_env.create_org("create-nosmtp")
    _sub, headers, _id = await _admin(users_env, org)
    users_env.kc.mail_status = 500

    response = await users_env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "ada@toeshee.com", "display_name": "Ada", "role_ids": []},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["enrolment_email_sent"] is False
    assert body["enrolment_email_error"]
    row = await _user_row(users_env.admin_engine, UUID(body["id"]))
    assert row is not None and row.status == "active"


async def test_a_refused_keycloak_create_leaves_no_itms_row(users_env: Any) -> None:
    """RULE 3, THE FIRST FAILURE ORDERING. Keycloak answers 409 when the email
    or username is taken. Writing the ITMS row anyway would point it at an
    identity somebody else owns."""
    org = await users_env.create_org("create-409")
    _sub, headers, _id = await _admin(users_env, org)
    users_env.kc.create_status = 409

    response = await users_env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "taken@toeshee.com", "display_name": "Taken", "role_ids": []},
    )

    assert response.status_code == 502, response.text
    assert await _user_by_email(users_env.admin_engine, "taken@toeshee.com") is None
    assert users_env.kc.mails == []


async def test_a_failed_itms_write_leaves_an_account_with_no_roles(users_env: Any) -> None:
    """RULE 3, THE SECOND FAILURE ORDERING, and the reason the order is this way
    round rather than the other.

    The ITMS half is made to fail by pre-seeding a row that already carries the
    `external_subject` Keycloak is about to issue, which trips
    `UniqueConstraint(org_id, external_subject)`. What is left behind is a
    Keycloak account holding no roles -- exactly the state of any federated user
    before their first login, which `app/api/deps.py` already self-heals. The
    REVERSE order would strand an ITMS row holding roles that no identity can
    ever claim, which nothing self-heals.
    """
    org = await users_env.create_org("create-dup")
    _sub, headers, _id = await _admin(users_env, org)
    collide = str(uuid4())
    users_env.kc.next_id = collide
    async with AsyncSession(users_env.admin_engine) as session:
        session.add(
            User(org_id=org, external_subject=collide, email="squatter@toeshee.com")
        )
        await session.commit()

    response = await users_env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "clash@toeshee.com", "display_name": "Clash", "role_ids": []},
    )

    # PINNED, not `>= 400`. The service catches the `IntegrityError` and answers
    # 409 deliberately -- a row already claims this subject. `>= 400` also passes
    # on the very regression this test exists to notice: the exception escaping
    # unhandled and surfacing as a 500, which is the same database failure
    # reported as a bug in this platform rather than a conflict in the request.
    assert response.status_code == 409, response.text
    # The Keycloak identity WAS created -- proof the order is Keycloak first.
    assert users_env.kc.calls("POST", "/users")
    # And the ITMS half rolled back entirely: no row, therefore no roles.
    assert await _user_by_email(users_env.admin_engine, "clash@toeshee.com") is None


async def test_create_refuses_an_email_outside_the_configured_domain(
    users_env: Any,
) -> None:
    """RULE 4. `app.core.identity.email_domain.is_allowed_domain` is the SAME
    function the three admission doors use (spec §3.2). A console that refuses
    an address at sign-in but mints it in the create form is not enforcing
    anything -- it just moves the lockout to the new colleague's first day."""
    org = await users_env.create_org("create-domain")
    sub, _headers, _id = await _admin(users_env, org)
    # The CALLER's own token has to pass the rule too, or `deps.py` refuses this
    # request at the door and the 403 would look like the create being refused.
    headers = await _headers_claiming(users_env, org, sub, "admin@toeshee.com")
    users_env.app.state.settings.console_email_domain = "toeshee.com"
    try:
        refused = await users_env.client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "mallory@eviltoeshee.com", "display_name": "M", "role_ids": []},
        )
        # A SUBDOMAIN IS ALLOWED -- non-vacuity, so a rule that refused
        # everything would not pass this test.
        allowed = await users_env.client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "ada@mail.toeshee.com", "display_name": "Ada", "role_ids": []},
        )
    finally:
        users_env.app.state.settings.console_email_domain = None

    assert refused.status_code == 422, refused.text
    assert allowed.status_code == 201, allowed.text
    # Refused BEFORE Keycloak: no identity is minted for an address the console
    # will not admit.
    assert len(users_env.kc.calls("POST", "/users")) == 1


async def test_a_whitespace_only_domain_setting_is_unrestricted_here_too(
    users_env: Any,
) -> None:
    """THE TWO SURFACES MUST READ THE SETTING THE SAME WAY.

    `ITS_CONSOLE_EMAIL_DOMAIN=` in an env file yields `""`, and a value of
    spaces yields `"   "`. `app.core.identity.domain_refusal` -- the helper all
    three admission doors call -- reads both as NO RESTRICTION, deliberately:
    read as a restriction it matches no address at all and locks out the whole
    deployment including the operator, which is the one failure spec §3.3 exists
    to prevent.

    A create form that tested the setting for bare truthiness instead would read
    the same string as a RESTRICTION and refuse every address, so the console
    would admit a person at sign-in and refuse to create them -- one setting,
    two answers. This test drives the caller through door 1 with the same
    setting in place, so the two readings are compared rather than assumed.
    """
    org = await users_env.create_org("create-blank-domain")
    sub, _headers, _id = await _admin(users_env, org)
    headers = await _headers_claiming(users_env, org, sub, "admin@elsewhere.example")
    users_env.app.state.settings.console_email_domain = "   "
    try:
        response = await users_env.client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "ada@elsewhere.example", "display_name": "Ada", "role_ids": []},
        )
    finally:
        users_env.app.state.settings.console_email_domain = None

    # 201, not 403: door 1 admitted the caller on this setting, so the create
    # form must too. A 403 here would mean the door disagreed with the form.
    assert response.status_code == 201, response.text
    assert await _user_by_email(users_env.admin_engine, "ada@elsewhere.example") is not None


async def test_create_refuses_super_admin(users_env: Any) -> None:
    """`super-admin` is cross-org platform administration (spec §1.3). An
    org-scoped surface that could grant it is a tenant-escalation path."""
    org = await users_env.create_org("create-su")
    _sub, headers, _id = await _admin(users_env, org)
    su = await _global_role_id(users_env.admin_engine, "super-admin")

    response = await users_env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "root@toeshee.com", "display_name": "Root", "role_ids": [str(su)]},
    )

    assert response.status_code == 403, response.text
    assert await _user_by_email(users_env.admin_engine, "root@toeshee.com") is None
    assert users_env.kc.seen == []


async def test_create_refuses_another_orgs_role(users_env: Any) -> None:
    org = await users_env.create_org("create-role-mine")
    theirs = await users_env.create_org("create-role-theirs")
    _sub, headers, _id = await _admin(users_env, org)
    theirs_role = await _seed_role(users_env.admin_engine, theirs, "not-yours", ["devices.read"])

    users_env.app.state.app_engine = users_env.admin_engine
    try:
        response = await users_env.client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "email": "x@toeshee.com",
                "display_name": "X",
                "role_ids": [str(theirs_role)],
            },
        )
    finally:
        users_env.app.state.app_engine = users_env.app_engine

    assert response.status_code == 404, response.text
    assert await _user_by_email(users_env.admin_engine, "x@toeshee.com") is None


async def test_lifecycle_routes_refuse_when_keycloak_is_not_configured(
    users_env: Any,
) -> None:
    """`KeycloakAdminConfig.from_settings` returns `None` on a deployment that
    has not set provisioning up, and that is a SUPPORTED state. What it must not
    be is a 500: a 503 naming the setting is what tells the operator the gap is
    configuration rather than a bug."""
    org = await users_env.create_org("no-kc")
    _sub, headers, admin_id = await _admin(users_env, org)
    users_env.app.dependency_overrides[keycloak_config] = lambda: None
    victim = await users_env.create_user(org, "technician")
    victim_id = await _user_id(users_env.admin_engine, victim)

    responses = [
        await users_env.client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "a@toeshee.com", "display_name": "A", "role_ids": []},
        ),
        await users_env.client.post(f"/api/v1/users/{victim_id}/disable", headers=headers),
        await users_env.client.post(f"/api/v1/users/{victim_id}/enable", headers=headers),
        await users_env.client.post(
            f"/api/v1/users/{victim_id}/reset-password", headers=headers
        ),
        await users_env.client.post(
            f"/api/v1/users/{victim_id}/resend-enrolment", headers=headers
        ),
        await users_env.client.delete(f"/api/v1/users/{victim_id}", headers=headers),
    ]

    assert [r.status_code for r in responses] == [503] * 6
    # Reads and role assignment need no Keycloak at all and must still work.
    assert (await users_env.client.get("/api/v1/users", headers=headers)).status_code == 200
    assert admin_id


async def test_authorization_runs_before_the_provisioning_secret_is_read(
    users_env: Any, tmp_path: Any
) -> None:
    """THE ORDER OF THE DEPENDENCIES IS A SECURITY PROPERTY, not a style point.

    `keycloak_config` reads the provisioning client secret OFF DISK. FastAPI
    resolves a route's dependencies in declaration order, so declaring it ahead
    of `require_permission` means every unauthenticated request to these six
    routes opens and reads the `manage-users` credential before anything has
    established who is asking. Nothing is returned to the caller, but the read
    happens, and an anonymous request that touches a secret is a filesystem and
    audit surface nobody asked for.

    Proved by pointing the setting at a path that DOES NOT EXIST:
    `KeycloakAdminConfig.from_settings` raises `FileNotFoundError` on a
    configured-but-missing secret, so if the read is reached at all the request
    cannot answer 401 or 403. The `pytest.raises` at the end is the non-vacuity
    half -- an authorized caller DOES reach the read and DOES blow up on it,
    which is what makes the 401s and 403s above evidence of ordering rather than
    evidence that the setting was ignored.
    """
    org = await users_env.create_org("dep-order")
    users_env.app.dependency_overrides.pop(keycloak_config, None)
    settings = users_env.app.state.settings
    settings.keycloak_admin_url = "https://keycloak.test"
    settings.keycloak_admin_secret_path = tmp_path / "absent" / "provisioning.secret"

    nobody = await users_env.create_user(org)
    unprivileged = await users_env.headers(org, nobody)
    victim = uuid4()

    async def drive(headers: dict[str, str]) -> list[int]:
        return [
            (
                await users_env.client.post(
                    "/api/v1/users",
                    headers=headers,
                    json={"email": "a@toeshee.com", "display_name": "A", "role_ids": []},
                )
            ).status_code,
            (
                await users_env.client.post(
                    f"/api/v1/users/{victim}/disable", headers=headers
                )
            ).status_code,
            (
                await users_env.client.post(
                    f"/api/v1/users/{victim}/enable", headers=headers
                )
            ).status_code,
            (
                await users_env.client.post(
                    f"/api/v1/users/{victim}/reset-password", headers=headers
                )
            ).status_code,
            (
                await users_env.client.post(
                    f"/api/v1/users/{victim}/resend-enrolment", headers=headers
                )
            ).status_code,
            (await users_env.client.delete(f"/api/v1/users/{victim}", headers=headers)).status_code,
        ]

    assert await drive({}) == [401] * 6
    assert await drive(unprivileged) == [403] * 6

    _sub, headers, _id = await _admin(users_env, org)
    with pytest.raises(FileNotFoundError):
        await users_env.client.post(f"/api/v1/users/{victim}/disable", headers=headers)


# --------------------------------------------------------------------------- #
# role assignment
# --------------------------------------------------------------------------- #


async def test_assigning_a_role_writes_the_row_and_an_audit_event(users_env: Any) -> None:
    org = await users_env.create_org("assign")
    _sub, headers, actor_id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    role_id = await _global_role_id(users_env.admin_engine, "technician")

    response = await users_env.client.post(
        f"/api/v1/users/{tech_id}/roles", headers=headers, json={"role_id": str(role_id)}
    )

    assert response.status_code == 201, response.text
    assignments = await _assignments(users_env.admin_engine, tech_id)
    assert [a.role_id for a in assignments] == [role_id]
    assert str(assignments[0].id) == response.json()["assignment_id"]
    events = [
        e for e in await _audit(users_env.admin_engine, org) if e.action == "user.role_assigned"
    ]
    assert len(events) == 1
    assert events[0].actor_id == actor_id
    assert events[0].meta["role_name"] == "technician"


async def test_a_grant_can_be_scoped_to_a_device_group(users_env: Any) -> None:
    org = await users_env.create_org("assign-scoped")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    group = await _device_group(users_env.admin_engine, org, "site-a")

    response = await users_env.client.post(
        f"/api/v1/users/{tech_id}/roles",
        headers=headers,
        json={"role_id": str(role_id), "device_group_id": str(group)},
    )

    assert response.status_code == 201, response.text
    assignments = await _assignments(users_env.admin_engine, tech_id)
    assert assignments[0].device_group_id == group


async def test_assigning_refuses_another_orgs_device_group(users_env: Any) -> None:
    """A grant scoped to a group in another tenant is either meaningless or a
    cross-tenant reference; either way it must not be written."""
    org = await users_env.create_org("scope-mine")
    theirs = await users_env.create_org("scope-theirs")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    group = await _device_group(users_env.admin_engine, theirs, "their-site")

    users_env.app.state.app_engine = users_env.admin_engine
    try:
        response = await users_env.client.post(
            f"/api/v1/users/{tech_id}/roles",
            headers=headers,
            json={"role_id": str(role_id), "device_group_id": str(group)},
        )
    finally:
        users_env.app.state.app_engine = users_env.app_engine

    assert response.status_code == 404, response.text
    assert await _assignments(users_env.admin_engine, tech_id) == []


async def test_assigning_refuses_super_admin(users_env: Any) -> None:
    org = await users_env.create_org("assign-su")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    su = await _global_role_id(users_env.admin_engine, "super-admin")

    response = await users_env.client.post(
        f"/api/v1/users/{tech_id}/roles", headers=headers, json={"role_id": str(su)}
    )

    assert response.status_code == 403, response.text
    assert await _assignments(users_env.admin_engine, tech_id) == []


async def test_super_admin_cannot_be_revoked_disabled_or_deleted(users_env: Any) -> None:
    """`super-admin` is refused at GRANT time (spec §1.3). It must be refused at
    every UNDO too, and the reason is that the two are not symmetrical.

    Granting it back is impossible from any console surface -- `_visible_role`
    refuses it, so does migration 0065's database function -- so revoking it,
    disabling its holder or deleting them is an IRREVERSIBLE act that only SQL
    against production can undo. A rail that stops an org-scoped administrator
    minting cross-org platform administration, but lets the same administrator
    destroy the platform operator's own account, is half a rail.

    All three doors to that end state are here: the revoke, the disable and the
    delete. `enable` is deliberately absent -- restoring an account takes
    nothing away and needs no rail.
    """
    org = await users_env.create_org("su-rails")
    _sub, headers, _actor = await _admin(users_env, org)
    su = await _global_role_id(users_env.admin_engine, "super-admin")
    operator = await users_env.create_user(org)
    operator_id = await _user_id(users_env.admin_engine, operator)
    assignment = await _assign(users_env.admin_engine, org, operator_id, su)

    refused = {
        "revoke": await users_env.client.delete(
            f"/api/v1/users/{operator_id}/roles/{assignment}", headers=headers
        ),
        "disable": await users_env.client.post(
            f"/api/v1/users/{operator_id}/disable", headers=headers
        ),
        "delete": await users_env.client.delete(
            f"/api/v1/users/{operator_id}", headers=headers
        ),
    }

    assert {name: r.status_code for name, r in refused.items()} == {
        "revoke": 403,
        "disable": 403,
        "delete": 403,
    }, {name: r.text for name, r in refused.items()}
    # Nothing moved: the grant is intact, the row is still active, and no
    # Keycloak identity was touched.
    assert [a.id for a in await _assignments(users_env.admin_engine, operator_id)] == [
        assignment
    ]
    row = await _user_row(users_env.admin_engine, operator_id)
    assert row is not None and row.status == "active"
    assert users_env.kc.seen == []


async def test_the_super_admin_rail_refuses_that_role_and_not_every_user(
    users_env: Any,
) -> None:
    """NON-VACUITY for the test above. A rail implemented as "refuse the revoke,
    the disable and the delete" unconditionally would pass every assertion there
    and break the entire surface, so the same three calls are made against an
    ordinary `technician` holder and must all succeed."""
    org = await users_env.create_org("su-rails-control")
    _sub, headers, _actor = await _admin(users_env, org)
    tech_role = await _global_role_id(users_env.admin_engine, "technician")
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    assignment = await _assign(users_env.admin_engine, org, tech_id, tech_role)

    revoke = await users_env.client.delete(
        f"/api/v1/users/{tech_id}/roles/{assignment}", headers=headers
    )
    disable = await users_env.client.post(
        f"/api/v1/users/{tech_id}/disable", headers=headers
    )
    enable = await users_env.client.post(f"/api/v1/users/{tech_id}/enable", headers=headers)
    delete = await users_env.client.delete(f"/api/v1/users/{tech_id}", headers=headers)

    assert [revoke.status_code, disable.status_code, enable.status_code, delete.status_code] == [
        204,
        200,
        200,
        204,
    ], (revoke.text, disable.text, enable.text, delete.text)


async def test_removing_an_assignment_deletes_the_row_and_audits(users_env: Any) -> None:
    org = await users_env.create_org("unassign")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    assignment_id = await _assign(users_env.admin_engine, org, tech_id, role_id)

    response = await users_env.client.delete(
        f"/api/v1/users/{tech_id}/roles/{assignment_id}", headers=headers
    )

    assert response.status_code == 204, response.text
    assert await _assignments(users_env.admin_engine, tech_id) == []
    events = [
        e for e in await _audit(users_env.admin_engine, org) if e.action == "user.role_removed"
    ]
    assert len(events) == 1


# --------------------------------------------------------------------------- #
# RAILS -- both are denial-of-service against the operator's own recovery path
# --------------------------------------------------------------------------- #


async def test_a_caller_cannot_disable_or_delete_themselves(users_env: Any) -> None:
    """The first mis-click must not be the unrecoverable one. Disabling yourself
    takes effect at the NEXT request -- `users.status` is checked at every
    admission door -- so there is no second chance to undo it from the console.

    A SECOND `org-admin` EXISTS, and that is the whole design of this test. Its
    first draft had only the caller, so the last-`org-admin` rail refused both
    calls and the test passed with `_refuse_self` deleted entirely -- the
    falsification run is what caught it. With a spare admin in place, the
    last-admin rail cannot fire and only the self rail is left to produce the
    403.
    """
    org = await users_env.create_org("self")
    _sub, headers, actor_id = await _admin(users_env, org)
    await users_env.create_user(org, "org-admin")

    disable = await users_env.client.post(f"/api/v1/users/{actor_id}/disable", headers=headers)
    delete = await users_env.client.delete(f"/api/v1/users/{actor_id}", headers=headers)

    assert disable.status_code == 403, disable.text
    assert delete.status_code == 403, delete.text
    row = await _user_row(users_env.admin_engine, actor_id)
    assert row is not None and row.status == "active"
    assert users_env.kc.seen == []


async def test_the_last_org_admin_cannot_be_disabled_deleted_or_stripped(
    users_env: Any,
) -> None:
    """The org's recovery path. With no `org-admin` left, nobody can grant the
    role back and the fix is SQL against production.

    All three doors are tested together because they are three ways to reach the
    same end state, and a rail on two of them is not a rail."""
    org = await users_env.create_org("last-admin")
    _sub, headers, actor_id = await _admin(users_env, org)
    # A SECOND admin does the acting, so "you cannot do this to yourself" is not
    # what produces the refusal -- otherwise this test passes with the
    # last-admin rail deleted.
    other = await users_env.create_user(org)
    manager_role = await _seed_role(
        users_env.admin_engine, org, "user-manager", ["users.manage", "users.read"]
    )
    other_id = await _user_id(users_env.admin_engine, other)
    await _assign(users_env.admin_engine, org, other_id, manager_role)
    other_headers = await users_env.headers(org, other)
    org_admin_role = await _global_role_id(users_env.admin_engine, "org-admin")
    assignment = (await _assignments(users_env.admin_engine, actor_id))[0]

    disable = await users_env.client.post(
        f"/api/v1/users/{actor_id}/disable", headers=other_headers
    )
    delete = await users_env.client.delete(f"/api/v1/users/{actor_id}", headers=other_headers)
    strip = await users_env.client.delete(
        f"/api/v1/users/{actor_id}/roles/{assignment.id}", headers=other_headers
    )

    assert disable.status_code == 403, disable.text
    assert delete.status_code == 403, delete.text
    assert strip.status_code == 403, strip.text
    row = await _user_row(users_env.admin_engine, actor_id)
    assert row is not None and row.status == "active"
    assert [a.role_id for a in await _assignments(users_env.admin_engine, actor_id)] == [
        org_admin_role
    ]
    assert users_env.kc.seen == []


async def test_a_second_org_admin_makes_the_first_removable(users_env: Any) -> None:
    """NON-VACUITY for the rail above. A guard written too broadly -- refusing
    every disable of an `org-admin` -- would pass that test while making the
    surface unusable, and this is the test that would notice."""
    org = await users_env.create_org("two-admins")
    _sub, headers, actor_id = await _admin(users_env, org)
    spare = await users_env.create_user(org, "org-admin")
    spare_id = await _user_id(users_env.admin_engine, spare)

    response = await users_env.client.post(
        f"/api/v1/users/{spare_id}/disable", headers=headers
    )

    assert response.status_code == 200, response.text
    row = await _user_row(users_env.admin_engine, spare_id)
    assert row is not None and row.status == "disabled"
    assert actor_id


# --------------------------------------------------------------------------- #
# disable / enable
# --------------------------------------------------------------------------- #


async def test_disable_writes_the_itms_status_and_turns_the_identity_off(
    users_env: Any,
) -> None:
    """BOTH halves, because neither alone is enough. Keycloak's `enabled: false`
    stops the NEXT token being issued and does nothing to the one already in the
    browser; `users.status` is what the three admission doors check, and it is
    what closes that window (spec §3.2)."""
    import json

    org = await users_env.create_org("disable")
    _sub, headers, actor_id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)

    response = await users_env.client.post(f"/api/v1/users/{tech_id}/disable", headers=headers)

    assert response.status_code == 200, response.text
    row = await _user_row(users_env.admin_engine, tech_id)
    assert row is not None and row.status == "disabled"
    put = users_env.kc.calls("PUT")
    assert len(put) == 1
    assert json.loads(put[0].content) == {"enabled": False}
    events = [e for e in await _audit(users_env.admin_engine, org) if e.action == "user.disabled"]
    assert len(events) == 1 and events[0].actor_id == actor_id


async def test_enable_restores_both_halves(users_env: Any) -> None:
    import json

    org = await users_env.create_org("enable")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)
    await users_env.client.post(f"/api/v1/users/{tech_id}/disable", headers=headers)

    response = await users_env.client.post(f"/api/v1/users/{tech_id}/enable", headers=headers)

    assert response.status_code == 200, response.text
    row = await _user_row(users_env.admin_engine, tech_id)
    assert row is not None and row.status == "active"
    assert json.loads(users_env.kc.calls("PUT")[-1].content) == {"enabled": True}


async def test_a_keycloak_failure_rolls_the_itms_status_back(users_env: Any) -> None:
    """A half-applied disable is the worst outcome available: the console shows
    "disabled" and Keycloak keeps issuing tokens, or the reverse. The ITMS write
    happens inside the transaction and the Keycloak call inside it too, so a
    raise takes both down."""
    org = await users_env.create_org("disable-fail")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)
    users_env.kc.update_status = 403

    response = await users_env.client.post(f"/api/v1/users/{tech_id}/disable", headers=headers)

    assert response.status_code == 502, response.text
    row = await _user_row(users_env.admin_engine, tech_id)
    assert row is not None and row.status == "active"
    events = await _audit(users_env.admin_engine, org)
    assert [e for e in events if e.action == "user.disabled"] == []


# --------------------------------------------------------------------------- #
# reset-password and resend-enrolment -- rule 1 and rule 5
# --------------------------------------------------------------------------- #


async def test_reset_password_asks_keycloak_to_email_update_password(
    users_env: Any,
) -> None:
    """RULE 1. The entire implementation of "reset this person's password" is
    one `execute-actions-email` call. ITMS never generates, stores, displays or
    returns a password, and there is no branch anywhere that could."""
    import json

    org = await users_env.create_org("reset")
    _sub, headers, actor_id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)

    response = await users_env.client.post(
        f"/api/v1/users/{tech_id}/reset-password", headers=headers
    )

    assert response.status_code == 200, response.text
    assert json.loads(users_env.kc.mails[0].content) == ["UPDATE_PASSWORD"]
    events = [
        e
        for e in await _audit(users_env.admin_engine, org)
        if e.action == "user.password_reset_requested"
    ]
    assert len(events) == 1 and events[0].actor_id == actor_id


async def test_a_mail_failure_on_reset_is_reported_not_swallowed(users_env: Any) -> None:
    """Unlike creation, the mail IS the whole action here -- there is no partial
    success to report -- so it is an error status rather than a flag in a 200
    body. A 200 would tell the operator a link is on its way when the realm has
    no SMTP server configured."""
    org = await users_env.create_org("reset-fail")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)
    users_env.kc.mail_status = 500

    response = await users_env.client.post(
        f"/api/v1/users/{tech_id}/reset-password", headers=headers
    )

    assert response.status_code == 502, response.text


async def test_resend_enrolment_sends_the_enrolment_actions(users_env: Any) -> None:
    """RULE 5's other half. Without this route, one transient mail failure at
    creation leaves an account nobody can ever sign into -- Keycloak 26 exposes
    no admin API that returns the action link for an operator to pass on by
    hand."""
    import json

    org = await users_env.create_org("resend")
    _sub, headers, _id = await _admin(users_env, org)
    users_env.kc.mail_status = 500
    created = (
        await users_env.client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "ada@toeshee.com", "display_name": "Ada", "role_ids": []},
        )
    ).json()
    assert created["enrolment_email_sent"] is False
    users_env.kc.mail_status = 204

    response = await users_env.client.post(
        f"/api/v1/users/{created['id']}/resend-enrolment", headers=headers
    )

    assert response.status_code == 200, response.text
    actions = json.loads(users_env.kc.mails[-1].content)
    assert "UPDATE_PASSWORD" in actions
    assert "webauthn-register-passwordless" in actions
    assert [
        e
        for e in await _audit(users_env.admin_engine, org)
        if e.action == "user.enrolment_resent"
    ]


# --------------------------------------------------------------------------- #
# DELETE -- rule 2
# --------------------------------------------------------------------------- #


async def test_delete_removes_the_identity_and_retains_the_itms_row(
    users_env: Any,
) -> None:
    """RULE 2. 89 foreign-key columns across 21 tables reference `users.id`
    (§0.6), so a hard delete either fails on the constraints or shreds the
    history that makes the audit log evidence."""
    org = await users_env.create_org("delete")
    _sub, headers, actor_id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)

    response = await users_env.client.delete(f"/api/v1/users/{tech_id}", headers=headers)

    assert response.status_code == 204, response.text
    assert len(users_env.kc.calls("DELETE")) == 1
    row = await _user_row(users_env.admin_engine, tech_id)
    assert row is not None, "the ITMS row must be RETAINED, only the identity goes"
    assert row.status == "deleted"
    events = [e for e in await _audit(users_env.admin_engine, org) if e.action == "user.deleted"]
    assert len(events) == 1 and events[0].actor_id == actor_id


async def test_a_deleted_users_audit_history_is_still_resolvable(users_env: Any) -> None:
    """THE ASSERTION THAT MATTERS, and the one a "the row is still there" test
    would miss. An audit event whose `actor_id` no longer joins to a user is an
    audit trail that has stopped being evidence: it can say something happened
    but not who did it."""
    org = await users_env.create_org("delete-audit")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)
    async with AsyncSession(users_env.admin_engine) as session:
        await audit_service.record(
            session,
            org_id=org,
            actor_type="user",
            actor_id=tech_id,
            action="jobs.dispatched",
            target_type="device",
            target_id=str(uuid4()),
            metadata={},
        )
        await session.commit()

    await users_env.client.delete(f"/api/v1/users/{tech_id}", headers=headers)

    async with AsyncSession(users_env.admin_engine) as session:
        resolved = (
            await session.execute(
                select(AuditEvent.action, User.email)
                .join(User, User.id == AuditEvent.actor_id)
                .where(AuditEvent.actor_id == tech_id, AuditEvent.action == "jobs.dispatched")
            )
        ).all()
    assert len(resolved) == 1
    assert resolved[0].email


async def test_a_failed_keycloak_delete_does_not_mark_the_row_deleted(
    users_env: Any,
) -> None:
    """A row marked `deleted` whose identity still exists is a user the console
    says is gone who can still sign in -- and `users.status` would then be the
    only thing stopping them."""
    org = await users_env.create_org("delete-fail")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)
    users_env.kc.delete_status = 500

    response = await users_env.client.delete(f"/api/v1/users/{tech_id}", headers=headers)

    assert response.status_code == 502, response.text
    row = await _user_row(users_env.admin_engine, tech_id)
    assert row is not None and row.status == "active"


# --------------------------------------------------------------------------- #
# RULE 1, across the WHOLE surface
# --------------------------------------------------------------------------- #


async def test_no_route_returns_anything_password_shaped(users_env: Any) -> None:
    """Every route on this surface, driven for real, with every response body
    scanned. Written as one sweep rather than per-route assertions so a route
    added later cannot quietly opt out: the list is derived from the router, and
    the test fails if the router grows a path this sweep does not exercise.

    THE SCAN CARRIES NO EXEMPTIONS, and keeping it that way changed the design:
    the mail routes originally echoed Keycloak's required-action names back
    (`["UPDATE_PASSWORD"]`, `["webauthn-register-passwordless", ...]`), which
    would have forced two carve-outs into this list. Neither name is a
    credential, but a sweep with exemptions is a sweep that can be widened, so
    `MailResultOut` dropped the field instead.
    """
    org = await users_env.create_org("sweep")
    _sub, headers, _id = await _admin(users_env, org)
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    create_response = await users_env.client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "ada@toeshee.com", "display_name": "Ada", "role_ids": []},
    )
    assert create_response.status_code == 201, create_response.text
    created = create_response.json()
    uid = created["id"]
    assign_response = await users_env.client.post(
        f"/api/v1/users/{uid}/roles", headers=headers, json={"role_id": str(role_id)}
    )
    assert assign_response.status_code == 201, assign_response.text
    assign = assign_response.json()

    responses = {
        "GET /users": await users_env.client.get("/api/v1/users", headers=headers),
        "GET effective": await users_env.client.get(
            f"/api/v1/users/{uid}/effective-permissions", headers=headers
        ),
        "POST reset-password": await users_env.client.post(
            f"/api/v1/users/{uid}/reset-password", headers=headers
        ),
        "POST resend-enrolment": await users_env.client.post(
            f"/api/v1/users/{uid}/resend-enrolment", headers=headers
        ),
        "POST disable": await users_env.client.post(
            f"/api/v1/users/{uid}/disable", headers=headers
        ),
        "POST enable": await users_env.client.post(
            f"/api/v1/users/{uid}/enable", headers=headers
        ),
        "DELETE role": await users_env.client.delete(
            f"/api/v1/users/{uid}/roles/{assign['assignment_id']}", headers=headers
        ),
        "DELETE user": await users_env.client.delete(f"/api/v1/users/{uid}", headers=headers),
    }

    # EVERY RESPONSE MUST HAVE SUCCEEDED BEFORE IT IS SCANNED. A route that
    # errored answers a short, needle-free body -- a FastAPI validation error or
    # a 500 detail string -- and would sail through the scan below while proving
    # nothing about the route at all. The sweep's whole claim is "these are the
    # real success bodies of every route on the surface", so the status codes
    # are part of the assertion, not context.
    for name, response in responses.items():
        assert response.status_code < 400, f"{name} failed: {response.status_code} {response.text}"

    bodies = {"POST /users": str(created), "POST role": str(assign)}
    bodies |= {name: r.text for name, r in responses.items()}
    for name, body in bodies.items():
        haystack = body.lower()
        for needle in ("password", "secret", "credential", "token", "otp"):
            assert needle not in haystack, f"{name} leaked {needle!r}: {body}"

    # Non-vacuity: the sweep above must have covered every path on the surface.
    app = FastAPI()
    app.include_router(users_router, prefix="/api/v1")
    exercised = len(bodies)
    surface = sum(
        1 for _path, route in flatten(app.router) if isinstance(route, APIRoute)
    )
    assert exercised == surface


def test_the_settings_the_surface_depends_on_exist() -> None:
    """A guard against this workstream's own assumption. `keycloak_config`
    reads three settings by name; a rename elsewhere would make
    `from_settings` silently return `None` and every lifecycle route answer 503
    on a correctly configured deployment."""
    from app.core.config import Settings

    fields = Settings.model_fields
    assert "keycloak_admin_client_id" in fields
    assert "keycloak_admin_secret_path" in fields
    assert "console_email_domain" in fields
