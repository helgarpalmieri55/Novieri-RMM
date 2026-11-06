"""The user-lifecycle API: `/api/v1/users`.

Spec `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2;
P2 phase 1 design §7 since Task 8.

The rules this surface keeps, and the test that would notice each breaking:

1. **No password ever transits this surface.** A technician chooses their own
   through a one-time link; `test_no_route_returns_anything_password_shaped`
   drives every route and scans the bodies, so a route added later cannot
   quietly join without being covered.
2. **Delete keeps the row.** 89 foreign-key columns across 21 tables reference
   `users.id`, audit `actor_id` among them (§0.6), so the row is marked
   `deleted` and RETAINED. `test_a_deleted_users_audit_history_is_still_resolvable`
   is the assertion that matters.
3. **No identity provider is called** (design §1.3, D5). A fake Keycloak stays
   installed on `httpx` so that any call would be SEEN: every `kc.seen == []`
   below holds the rule. An invited technician's `external_subject` is
   `itms-local:<uuid>`.
4. **The domain rule is enforced here too.** A console that refuses an address
   at sign-in but invites it is not enforcing anything.
5. **The invitation link is shown once, and only when no mail carried it.**

TENANCY. `test_the_route_predicate_scopes_users_without_rls_to_mask_it`,
`test_another_orgs_user_cannot_be_acted_on` and
`test_no_lifecycle_route_reaches_another_orgs_user` repoint the app engine at
the BYPASSRLS ADMIN engine and drive the REAL routes through it: through a
tenant session RLS hides the other org whether or not the application
predicate is there.

THE RAILS ARE TESTED AT EVERY DOOR TO THE SAME END STATE, not at one of them.
`org-admin` has three (revoke, disable, delete) and so does `super-admin`.
`test_the_super_admin_rail_refuses_that_role_and_not_every_user` is the
non-vacuity half.

WHY THE ROUTER IS INCLUDED BY A FIXTURE. `users_env` includes it into the
per-test app only if it is not already there, so these tests pass both before
and after `app/main.py` wires it, and never register it twice.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.config import Settings
from app.core.rbac import route_permission
from app.core.router_inspect import flatten
from app.models import AuditEvent, Client, Role, User, UserRole
from app.modules.user_admin import service as user_admin_service
from app.modules.user_admin.api import router as users_router
from app.modules.user_admin.kc_client import KeycloakAdminConfig
from tests.auth_support import CONSOLE, capture_mail, console_ready, signed_in
from tests.session_support import console_headers

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

    # Rule 3: nothing on this surface calls an identity provider. The fake
    # stays on `httpx` so a call would be SEEN, and `kc.seen == []` holds it.
    kc = FakeKeycloak()

    def factory(**kw: Any) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(kc.handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    console_ready(api_env, monkeypatch)
    api_env.kc = kc
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _invite(
    env: Any,
    headers: dict[str, str],
    email: str = "ada@toeshee.com",
    display_name: str = "Ada",
    grants: list[dict[str, str]] | None = None,
) -> Any:
    return await env.client.post(
        "/api/v1/users/invite",
        headers=headers,
        json={"email": email, "display_name": display_name, "grants": grants or []},
    )


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
    client_id: UUID | None = None,
) -> UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = UserRole(
            org_id=org_id,
            user_id=user_id,
            role_id=role_id,
            client_id=client_id,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _client(
    engine: AsyncEngine, org_id: UUID, name: str, *, status: str = "active"
) -> UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name=name, kind="customer", status=status)
        session.add(client)
        await session.commit()
        return client.id


async def _unassigned_client(engine: AsyncEngine, org_id: UUID) -> UUID:
    """The org's `unassigned` system client (migration 0230's trigger)."""
    async with AsyncSession(engine) as session:
        return (
            await session.execute(
                select(Client.id).where(Client.org_id == org_id, Client.kind == "unassigned")
            )
        ).scalar_one()


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
    """The console's session for a person whose row holds `email`.

    Needed by the domain test and by nothing else: switching
    `console_email_domain` on makes `app/api/deps.py`'s own admission gate
    (door 1 of 3) refuse the CALLER too if their address does not pass.
    That is spec §3.3's lockout reproduced in miniature -- and it is why the
    preflight is a required step in `deploy/README.md` rather than a suggestion.
    Since own authentication phase 1 the gate (and the lockout preflight)
    read the row's address, which is the one the session carries, so the
    address is written there before the session is made.
    """
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(update(User).where(User.external_subject == sub).values(email=email))
        await session.commit()
    return await env.headers(org, sub)


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
        ("POST", "/api/v1/users/invite"): "users.manage",
        ("POST", "/api/v1/users/{user_id}/invite/resend"): "users.manage",
        ("DELETE", "/api/v1/users/{user_id}/invite"): "users.manage",
        ("POST", "/api/v1/users/{user_id}/roles"): "users.manage",
        ("DELETE", "/api/v1/users/{user_id}/roles/{assignment_id}"): "users.manage",
        ("POST", "/api/v1/users/{user_id}/disable"): "users.manage",
        ("POST", "/api/v1/users/{user_id}/enable"): "users.manage",
        ("POST", "/api/v1/users/{user_id}/factors/reset"): "users.manage",
        ("DELETE", "/api/v1/users/{user_id}/sessions"): "users.manage",
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
        await _invite(users_env, headers, email="x@toeshee.com", display_name="X"),
        await users_env.client.post(
            f"/api/v1/users/{victim_id}/invite/resend", headers=headers
        ),
        await users_env.client.delete(f"/api/v1/users/{victim_id}/invite", headers=headers),
        await users_env.client.post(
            f"/api/v1/users/{victim_id}/roles", headers=headers, json={"role_id": str(role_id)}
        ),
        await users_env.client.delete(
            f"/api/v1/users/{victim_id}/roles/{uuid4()}", headers=headers
        ),
        await users_env.client.post(f"/api/v1/users/{victim_id}/disable", headers=headers),
        await users_env.client.post(f"/api/v1/users/{victim_id}/enable", headers=headers),
        await users_env.client.post(f"/api/v1/users/{victim_id}/factors/reset", headers=headers),
        await users_env.client.delete(f"/api/v1/users/{victim_id}/sessions", headers=headers),
        await users_env.client.delete(f"/api/v1/users/{victim_id}", headers=headers),
    ]
    assert [r.status_code for r in refused] == [403] * 10
    # And nothing reached an identity provider.
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


async def test_no_lifecycle_route_reaches_another_orgs_user(users_env: Any) -> None:
    """THE CROSS-TENANT PATH, every write that addresses a user by id: the
    target is read off the TENANT-SCOPED row and nowhere else, so another
    org's id is a clean 404 and its row is untouched. Driven on the BYPASSRLS
    ADMIN ENGINE, so RLS is not what produces the refusal. `factors/reset`
    is here too: a token has no session to confirm and passes the step-up
    until Task 13, so it reaches the target read like the rest."""
    mine = await users_env.create_org("x-mine")
    theirs = await users_env.create_org("x-theirs")
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
            "invite/resend": await users_env.client.post(
                f"/api/v1/users/{stranger_id}/invite/resend", headers=headers
            ),
            "invite cancel": await users_env.client.delete(
                f"/api/v1/users/{stranger_id}/invite", headers=headers
            ),
            "factors/reset": await users_env.client.post(
                f"/api/v1/users/{stranger_id}/factors/reset", headers=headers
            ),
            "sessions": await users_env.client.delete(
                f"/api/v1/users/{stranger_id}/sessions", headers=headers
            ),
            "delete": await users_env.client.delete(
                f"/api/v1/users/{stranger_id}", headers=headers
            ),
        }
    finally:
        users_env.app.state.app_engine = users_env.app_engine

    assert {name: r.status_code for name, r in attempts.items()} == {
        name: 404 for name in attempts
    }, {name: r.text for name, r in attempts.items()}
    assert users_env.kc.seen == []
    # And org B's row is exactly as it was.
    row = await _user_row(users_env.admin_engine, stranger_id)
    assert row is not None and row.status == "active"
    assert row.external_subject == stranger_subject


# --------------------------------------------------------------------------- #
# GET /users
# --------------------------------------------------------------------------- #


async def test_the_list_carries_each_users_roles_and_the_assignment_id(
    users_env: Any,
) -> None:
    """`assignment_id` is what DELETE addresses. Without it the console would
    have to reconstruct which of a user's rows to remove from (role, client),
    which is exactly the ambiguity `UniqueConstraint(user_id, role_id,
    client_id)` allows to exist twice over."""
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
            "client_id": None,
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
    granted = {(c["code"], c["via_role"], c["client_id"]) for c in body["codes"]}
    assert ("devices.read", "technician", None) in granted
    assert ("jobs.dispatch", "technician", None) in granted
    assert not any(c["code"] == "credentials.checkout" for c in body["codes"])


async def test_a_client_scoped_grant_reports_its_client(users_env: Any) -> None:
    """`user_roles.client_id` means "this grant applies only to that client"
    (MSP clients spec §2.1). A view that flattened it would tell an operator a
    technician has `jobs.dispatch` on every client when they have it for one."""
    org = await users_env.create_org("eff-scoped")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    client = await _client(users_env.admin_engine, org, "branch-office")
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    await _assign(users_env.admin_engine, org, tech_id, role_id, client)

    body = (
        await users_env.client.get(
            f"/api/v1/users/{tech_id}/effective-permissions", headers=headers
        )
    ).json()

    scoped = {c["client_id"] for c in body["codes"]}
    assert scoped == {str(client)}


# --------------------------------------------------------------------------- #
# POST /users/invite -- rules 1, 3, 4 and 5
# --------------------------------------------------------------------------- #


async def test_an_invite_makes_the_local_row_and_no_identity_anywhere_else(
    users_env: Any,
) -> None:
    """RULE 3 and D5: the row exists, carries a subject no provider issued,
    and nothing was asked of Keycloak -- which is also why this person cannot
    reach MCP until phase 3."""
    org = await users_env.create_org("invite")
    _sub, headers, _id = await _admin(users_env, org)

    response = await _invite(users_env, headers, display_name="Ada Lovelace")

    assert response.status_code == 201, response.text
    row = await _user_row(users_env.admin_engine, UUID(response.json()["user_id"]))
    assert row is not None
    assert row.external_subject.startswith("itms-local:")
    assert (row.email, row.display_name, row.status, row.org_id) == (
        "ada@toeshee.com", "Ada Lovelace", "active", org,
    )
    assert users_env.kc.seen == []


async def test_an_invite_grants_the_requested_roles_per_client_and_audits(
    users_env: Any,
) -> None:
    org = await users_env.create_org("invite-roles")
    _sub, headers, actor_id = await _admin(users_env, org)
    tech = await _global_role_id(users_env.admin_engine, "technician")
    reader = await _global_role_id(users_env.admin_engine, "read-only")
    branch = await _client(users_env.admin_engine, org, "branch-office")

    body = (
        await _invite(
            users_env,
            headers,
            email="grace@toeshee.com",
            grants=[{"role_id": str(tech)}, {"role_id": str(reader), "client_id": str(branch)}],
        )
    ).json()

    new_id = UUID(body["user_id"])
    held = {(a.role_id, a.client_id) for a in await _assignments(users_env.admin_engine, new_id)}
    assert held == {(tech, None), (reader, branch)}
    events = await _audit(users_env.admin_engine, org)
    created = [e for e in events if e.action == "user.created"]
    invited = [e for e in events if e.action == "user.invited"]
    assert len(created) == 1 and created[0].actor_id == actor_id
    assert created[0].target_id == str(new_id)
    assert created[0].meta["email"] == "grace@toeshee.com"
    assert [e.target_id for e in invited] == [str(new_id)]


async def test_an_invite_mails_the_link_and_does_not_show_it(users_env: Any) -> None:
    org = await users_env.create_org("invite-mail")
    _sub, headers, _id = await _admin(users_env, org)

    body = (await _invite(users_env, headers)).json()

    assert body["invite_link"] is None
    assert [m.to for m in users_env.mail.sent] == ["ada@toeshee.com"]
    assert "/invite#" in users_env.mail.sent[0].text


async def test_an_invite_nobody_could_mail_shows_its_link_once(users_env: Any) -> None:
    """RULE 5 and design §7: without account mail the administrator sees the
    link -- in this answer and nowhere else -- and the account exists."""
    org = await users_env.create_org("invite-nomail")
    _sub, headers, _id = await _admin(users_env, org)
    users_env.mail.configured = False

    response = await _invite(users_env, headers)

    assert response.status_code == 201, response.text
    link = response.json()["invite_link"]
    assert link and link.startswith(f"{CONSOLE}/invite#")
    row = await _user_row(users_env.admin_engine, UUID(response.json()["user_id"]))
    assert row is not None and row.status == "active"


async def test_an_address_already_in_use_is_refused_before_anything_is_written(
    users_env: Any,
) -> None:
    org = await users_env.create_org("invite-409")
    _sub, headers, _id = await _admin(users_env, org)
    await _invite(users_env, headers, email="taken@toeshee.com")
    users_env.mail.sent.clear()

    response = await _invite(users_env, headers, email="taken@toeshee.com")

    assert response.status_code == 409, response.text
    assert users_env.mail.sent == []


async def test_a_failed_write_leaves_nothing_behind(users_env: Any) -> None:
    """The address in another case passes the service's exact-match check and
    trips `ux_users_org_lower_email_staff` (D2): the IntegrityError is a 409
    -- PINNED, not `>= 400`: a 500 would be the regression -- and the whole
    transaction rolls back: no row, no invitation, no mail."""
    org = await users_env.create_org("invite-dup")
    _sub, headers, _id = await _admin(users_env, org)
    async with AsyncSession(users_env.admin_engine) as session:
        session.add(
            User(org_id=org, external_subject=f"sub-{uuid4().hex[:8]}", email="Clash@toeshee.com")
        )
        await session.commit()

    response = await _invite(users_env, headers, email="clash@toeshee.com")

    assert response.status_code == 409, response.text
    assert await _user_by_email(users_env.admin_engine, "clash@toeshee.com") is None
    assert users_env.mail.sent == []


async def test_an_invite_refuses_an_email_outside_the_configured_domain(
    users_env: Any,
) -> None:
    """RULE 4, with the SAME helper the doors use (spec §3.2)."""
    org = await users_env.create_org("invite-domain")
    sub, _headers, _id = await _admin(users_env, org)
    # The CALLER's own address has to pass the rule too, or `deps.py` refuses this
    # request at the door and the 403 would look like the invite being refused.
    headers = await _headers_claiming(users_env, org, sub, "admin@toeshee.com")
    users_env.app.state.settings.console_email_domain = "toeshee.com"
    try:
        refused = await _invite(users_env, headers, email="mallory@eviltoeshee.com")
        # A SUBDOMAIN IS ALLOWED -- non-vacuity.
        allowed = await _invite(users_env, headers, email="ada@mail.toeshee.com")
    finally:
        users_env.app.state.settings.console_email_domain = None

    assert refused.status_code == 422, refused.text
    assert allowed.status_code == 201, allowed.text
    # Refused before anything was written: only the allowed one got a mail.
    assert [m.to for m in users_env.mail.sent] == ["ada@mail.toeshee.com"]


async def test_a_whitespace_only_domain_setting_is_unrestricted_here_too(
    users_env: Any,
) -> None:
    """THE TWO SURFACES MUST READ THE SETTING THE SAME WAY: `domain_refusal`
    reads "" and "   " as no restriction, at the door and here."""
    org = await users_env.create_org("invite-blank-domain")
    sub, _headers, _id = await _admin(users_env, org)
    headers = await _headers_claiming(users_env, org, sub, "admin@elsewhere.example")
    users_env.app.state.settings.console_email_domain = "   "
    try:
        response = await _invite(users_env, headers, email="ada@elsewhere.example")
    finally:
        users_env.app.state.settings.console_email_domain = None

    # 201, not 403: door 1 admitted the caller on this setting, so the invite
    # must too. A 403 here would mean the door disagreed with the form.
    assert response.status_code == 201, response.text
    assert await _user_by_email(users_env.admin_engine, "ada@elsewhere.example") is not None


async def test_an_invite_refuses_super_admin(users_env: Any) -> None:
    """`super-admin` is cross-org platform administration (spec §1.3). An
    org-scoped surface that could grant it is a tenant-escalation path."""
    org = await users_env.create_org("invite-su")
    _sub, headers, _id = await _admin(users_env, org)
    su = await _global_role_id(users_env.admin_engine, "super-admin")

    response = await _invite(
        users_env, headers, email="root@toeshee.com", grants=[{"role_id": str(su)}]
    )

    assert response.status_code == 403, response.text
    assert await _user_by_email(users_env.admin_engine, "root@toeshee.com") is None
    assert users_env.kc.seen == [] and users_env.mail.sent == []


async def test_an_invite_refuses_another_orgs_role(users_env: Any) -> None:
    org = await users_env.create_org("invite-role-mine")
    theirs = await users_env.create_org("invite-role-theirs")
    _sub, headers, _id = await _admin(users_env, org)
    theirs_role = await _seed_role(users_env.admin_engine, theirs, "not-yours", ["devices.read"])

    users_env.app.state.app_engine = users_env.admin_engine
    try:
        response = await _invite(
            users_env, headers, email="x@toeshee.com", grants=[{"role_id": str(theirs_role)}]
        )
    finally:
        users_env.app.state.app_engine = users_env.app_engine

    assert response.status_code == 404, response.text
    assert await _user_by_email(users_env.admin_engine, "x@toeshee.com") is None


async def test_the_lifecycle_needs_no_identity_provider_configuration(users_env: Any) -> None:
    """Rule 3, from the deployment's side: with no `ITS_KEYCLOAK_ADMIN_*` at all
    (the test settings set none) every lifecycle write works, where it used to
    be a 503."""
    assert users_env.settings.keycloak_admin_url is None
    org = await users_env.create_org("no-kc")
    _sub, headers, _admin_id = await _admin(users_env, org)
    await users_env.create_user(org, "org-admin")
    victim = await users_env.create_user(org, "technician")
    victim_id = await _user_id(users_env.admin_engine, victim)

    responses = [
        await _invite(users_env, headers),
        await users_env.client.post(f"/api/v1/users/{victim_id}/disable", headers=headers),
        await users_env.client.post(f"/api/v1/users/{victim_id}/enable", headers=headers),
        await users_env.client.delete(f"/api/v1/users/{victim_id}/sessions", headers=headers),
        await users_env.client.delete(f"/api/v1/users/{victim_id}", headers=headers),
    ]

    assert [r.status_code for r in responses] == [201, 200, 200, 204, 204]
    assert users_env.kc.seen == []


async def test_authorization_runs_before_anything_else(users_env: Any) -> None:
    """No credential, then the wrong one: 401 and 403 on every write that
    takes a user id, before the handler reads anything. Non-vacuity: an
    authorized caller DOES reach the handler, which answers 404 for an id
    that is nobody."""
    org = await users_env.create_org("dep-order")
    nobody = await users_env.create_user(org)
    unprivileged = await users_env.headers(org, nobody)
    victim = uuid4()

    async def drive(headers: dict[str, str]) -> list[int]:
        return [
            (await _invite(users_env, headers, email="a@toeshee.com")).status_code,
            (
                await users_env.client.post(f"/api/v1/users/{victim}/disable", headers=headers)
            ).status_code,
            (
                await users_env.client.post(f"/api/v1/users/{victim}/enable", headers=headers)
            ).status_code,
            (
                await users_env.client.post(
                    f"/api/v1/users/{victim}/invite/resend", headers=headers
                )
            ).status_code,
            (
                await users_env.client.delete(f"/api/v1/users/{victim}/invite", headers=headers)
            ).status_code,
            (
                await users_env.client.post(
                    f"/api/v1/users/{victim}/factors/reset", headers=headers
                )
            ).status_code,
            (
                await users_env.client.delete(f"/api/v1/users/{victim}/sessions", headers=headers)
            ).status_code,
            (await users_env.client.delete(f"/api/v1/users/{victim}", headers=headers)).status_code,
        ]

    assert await drive({}) == [401] * 8
    assert await drive(unprivileged) == [403] * 8
    assert users_env.mail.sent == []
    _sub, headers, _id = await _admin(users_env, org)
    reached = await users_env.client.post(f"/api/v1/users/{victim}/disable", headers=headers)
    assert reached.status_code == 404


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


async def test_a_grant_can_be_scoped_to_a_client(users_env: Any) -> None:
    org = await users_env.create_org("assign-scoped")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    client = await _client(users_env.admin_engine, org, "site-a")

    response = await users_env.client.post(
        f"/api/v1/users/{tech_id}/roles",
        headers=headers,
        json={"role_id": str(role_id), "client_id": str(client)},
    )

    assert response.status_code == 201, response.text
    assert response.json()["client_id"] == str(client)
    assignments = await _assignments(users_env.admin_engine, tech_id)
    assert assignments[0].client_id == client
    (event,) = [
        e for e in await _audit(users_env.admin_engine, org) if e.action == "user.role_assigned"
    ]
    assert event.meta["client_id"] == str(client)


async def test_assigning_refuses_another_orgs_client(users_env: Any) -> None:
    """A grant scoped to a client of another tenant is either meaningless or a
    cross-tenant reference; either way it must not be written -- and the answer
    is the one an id that exists nowhere gets."""
    org = await users_env.create_org("scope-mine")
    theirs = await users_env.create_org("scope-theirs")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    client = await _client(users_env.admin_engine, theirs, "their-site")

    users_env.app.state.app_engine = users_env.admin_engine
    try:
        responses = [
            await users_env.client.post(
                f"/api/v1/users/{tech_id}/roles",
                headers=headers,
                json={"role_id": str(role_id), "client_id": str(target)},
            )
            for target in (client, uuid4())
        ]
    finally:
        users_env.app.state.app_engine = users_env.app_engine

    for response in responses:
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "unknown client"
    assert await _assignments(users_env.admin_engine, tech_id) == []


async def test_assigning_refuses_the_unassigned_and_an_archived_client(users_env: Any) -> None:
    """Spec §2.2: the unassigned client is visible only to an every-client
    scope, so no grant may name it. An archived client is a scope no selector
    shows, so a grant on it is one nobody could audit."""
    org = await users_env.create_org("scope-system")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    unassigned = await _unassigned_client(users_env.admin_engine, org)
    archived = await _client(users_env.admin_engine, org, "gone", status="archived")

    async def assign(client: UUID) -> Any:
        return await users_env.client.post(
            f"/api/v1/users/{tech_id}/roles",
            headers=headers,
            json={"role_id": str(role_id), "client_id": str(client)},
        )

    refused = await assign(unassigned)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "a role cannot be granted on the unassigned client"
    gone = await assign(archived)
    assert gone.status_code == 409, gone.text
    assert gone.json()["detail"] == "that client is archived"
    assert await _assignments(users_env.admin_engine, tech_id) == []


async def test_a_device_group_no_longer_scopes_a_grant(users_env: Any) -> None:
    """Spec §2.1: "los grupos de equipos dejan de dar permisos". The old field
    is not quietly ignored -- which would turn a grant meant for one group into
    one for every client -- it is refused."""
    org = await users_env.create_org("scope-group")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org)
    tech_id = await _user_id(users_env.admin_engine, tech)
    role_id = await _global_role_id(users_env.admin_engine, "technician")

    response = await users_env.client.post(
        f"/api/v1/users/{tech_id}/roles",
        headers=headers,
        json={"role_id": str(role_id), "device_group_id": str(uuid4())},
    )

    assert response.status_code == 422, response.text
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


async def test_an_org_admin_on_one_client_does_not_keep_the_org_administrable(
    users_env: Any,
) -> None:
    """Found in review. Managing users needs `users.manage` on EVERY client
    (`require_permission_on_every_client`), so an `org-admin` grant on one
    client cannot grant anything back. Counting it as "another active
    org-admin" let the last every-client one strip, disable or delete themself
    and strand the organization. Only an every-client grant counts."""
    engine = users_env.admin_engine
    org = await users_env.create_org("last-admin-scoped")
    _sub, headers, actor_id = await _admin(users_env, org)
    org_admin_role = await _global_role_id(engine, "org-admin")
    scoped = await users_env.create_user(org)
    scoped_id = await _user_id(engine, scoped)
    await _assign(engine, org, scoped_id, org_admin_role, await _client(engine, org, "x"))
    # A SECOND every-client manager does the disable and the delete, so the
    # refusal is the last-admin rail and not "you cannot do this to yourself".
    other = await users_env.create_user(org)
    manager_role = await _seed_role(engine, org, "user-manager", ["users.manage", "users.read"])
    await _assign(engine, org, await _user_id(engine, other), manager_role)
    other_headers = await users_env.headers(org, other)
    (assignment,) = await _assignments(engine, actor_id)

    strip = await users_env.client.delete(
        f"/api/v1/users/{actor_id}/roles/{assignment.id}", headers=headers
    )
    disable = await users_env.client.post(
        f"/api/v1/users/{actor_id}/disable", headers=other_headers
    )
    delete = await users_env.client.delete(f"/api/v1/users/{actor_id}", headers=other_headers)

    assert strip.status_code == 403, strip.text
    assert disable.status_code == 403, disable.text
    assert delete.status_code == 403, delete.text
    row = await _user_row(engine, actor_id)
    assert row is not None and row.status == "active"
    assert [a.id for a in await _assignments(engine, actor_id)] == [assignment.id]


async def test_an_org_admin_grant_on_one_client_does_not_cover_stripping_the_every_client_one(
    users_env: Any,
) -> None:
    """The variant: the last every-client org-admin ALSO holds org-admin on one
    client. That grant is "another org-admin row of theirs", so the rail used
    to assume they kept the role and skip itself -- and the org was stranded."""
    engine = users_env.admin_engine
    org = await users_env.create_org("last-admin-both")
    _sub, headers, actor_id = await _admin(users_env, org)
    (every_client,) = await _assignments(engine, actor_id)
    org_admin_role = await _global_role_id(engine, "org-admin")
    on_x = await _assign(
        engine, org, actor_id, org_admin_role, await _client(engine, org, "x")
    )

    strip = await users_env.client.delete(
        f"/api/v1/users/{actor_id}/roles/{every_client.id}", headers=headers
    )
    assert strip.status_code == 403, strip.text
    # NON-VACUITY: the one-client grant itself is removable -- the every-client
    # grant is what keeps the organization administrable.
    spare = await users_env.client.delete(
        f"/api/v1/users/{actor_id}/roles/{on_x}", headers=headers
    )
    assert spare.status_code == 204, spare.text
    assert [a.id for a in await _assignments(engine, actor_id)] == [every_client.id]


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


async def test_disable_writes_the_status_and_ends_the_persons_sessions(
    users_env: Any,
) -> None:
    """`users.status` stops the next admission; the sessions end now (design
    §2.4) -- the cookie already in the person's browser stops working, and it
    stays dead after a re-enable, which `users.status` alone would not do."""
    org = await users_env.create_org("disable")
    _sub, headers, actor_id = await _admin(users_env, org)
    member = await signed_in(users_env, org, "technician")

    response = await users_env.client.post(
        f"/api/v1/users/{member.user_id}/disable", headers=headers
    )

    assert response.status_code == 200, response.text
    row = await _user_row(users_env.admin_engine, member.user_id)
    assert row is not None and row.status == "disabled"
    assert (await users_env.client.get("/api/v1/me", headers=member.headers())).status_code == 401
    await users_env.client.post(f"/api/v1/users/{member.user_id}/enable", headers=headers)
    assert (await users_env.client.get("/api/v1/me", headers=member.headers())).status_code == 401
    events = [e for e in await _audit(users_env.admin_engine, org) if e.action == "user.disabled"]
    assert len(events) == 1 and events[0].actor_id == actor_id
    assert users_env.kc.seen == []


async def test_enable_restores_the_status(users_env: Any) -> None:
    org = await users_env.create_org("enable")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)
    await users_env.client.post(f"/api/v1/users/{tech_id}/disable", headers=headers)

    response = await users_env.client.post(f"/api/v1/users/{tech_id}/enable", headers=headers)

    assert response.status_code == 200, response.text
    row = await _user_row(users_env.admin_engine, tech_id)
    assert row is not None and row.status == "active"
    assert users_env.kc.seen == []


async def test_a_failure_inside_disable_rolls_the_status_back(
    users_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-applied disable -- "disabled" with live sessions, or the reverse
    -- is the worst outcome: the sessions end inside the same transaction as
    the status, so a failure there takes both down."""
    org = await users_env.create_org("disable-fail")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the session store did not answer")

    monkeypatch.setattr(user_admin_service, "end_all_for_user", broken)
    with pytest.raises(RuntimeError):
        await users_env.client.post(f"/api/v1/users/{tech_id}/disable", headers=headers)

    row = await _user_row(users_env.admin_engine, tech_id)
    assert row is not None and row.status == "active"
    events = await _audit(users_env.admin_engine, org)
    assert [e for e in events if e.action == "user.disabled"] == []


# --------------------------------------------------------------------------- #
# the invitation link again -- rule 5
# --------------------------------------------------------------------------- #


async def test_an_administrator_no_longer_resets_a_password_by_mail(users_env: Any) -> None:
    """Plan decision 2: the person resets their own (design §2.8); the old
    route is gone, and nothing is audited for it."""
    org = await users_env.create_org("reset")
    _sub, headers, _actor = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)

    response = await users_env.client.post(
        f"/api/v1/users/{tech_id}/reset-password", headers=headers
    )

    assert response.status_code == 404
    events = await _audit(users_env.admin_engine, org)
    assert [e for e in events if e.action == "user.password_reset_requested"] == []
    assert users_env.kc.seen == [] and users_env.mail.sent == []


async def test_a_resend_whose_mail_fails_shows_the_new_link(users_env: Any) -> None:
    org = await users_env.create_org("resend-fail")
    _sub, headers, _id = await _admin(users_env, org)
    created = (await _invite(users_env, headers)).json()
    users_env.mail.failing = True

    response = await users_env.client.post(
        f"/api/v1/users/{created['user_id']}/invite/resend", headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["invite_link"].startswith(f"{CONSOLE}/invite#")


async def test_resend_replaces_the_invitation_link(users_env: Any) -> None:
    """RULE 5's other half: one transient mail failure must not leave an account
    nobody can sign into -- and the old link must stop working."""
    org = await users_env.create_org("resend")
    _sub, headers, _id = await _admin(users_env, org)
    users_env.mail.configured = False
    created = (await _invite(users_env, headers)).json()
    first = created["invite_link"]

    response = await users_env.client.post(
        f"/api/v1/users/{created['user_id']}/invite/resend", headers=headers
    )

    assert response.status_code == 200, response.text
    second = response.json()["invite_link"]
    assert second and second != first
    old = await users_env.client.post(
        "/api/v1/auth/invite/details",
        headers=console_headers(),
        json={"token": first.split("#", 1)[1]},
    )
    assert old.status_code == 410
    assert [
        e for e in await _audit(users_env.admin_engine, org) if e.action == "user.invite_resent"
    ]


# --------------------------------------------------------------------------- #
# DELETE -- rule 2
# --------------------------------------------------------------------------- #


async def test_delete_retains_the_itms_row(
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
    assert users_env.kc.seen == []
    row = await _user_row(users_env.admin_engine, tech_id)
    assert row is not None, "the ITMS row must be RETAINED"
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


async def test_a_failure_inside_delete_does_not_mark_the_row_deleted(
    users_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row marked `deleted` whose sessions still work is a user the console
    says is gone who can still act: the session end is inside the transaction."""
    org = await users_env.create_org("delete-fail")
    _sub, headers, _id = await _admin(users_env, org)
    tech = await users_env.create_user(org, "technician")
    tech_id = await _user_id(users_env.admin_engine, tech)

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the session store did not answer")

    monkeypatch.setattr(user_admin_service, "end_all_for_user", broken)
    with pytest.raises(RuntimeError):
        await users_env.client.delete(f"/api/v1/users/{tech_id}", headers=headers)

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
    the old mail routes originally echoed Keycloak's required-action names back
    (`["UPDATE_PASSWORD"]`, ...), which would have forced carve-outs into this
    list; the field was dropped instead. An invitation's link is the one secret
    the surface returns, and only when no mail carried it -- here the mail
    works, so no body carries one.

    Driven by an administrator signed in with the console cookie and a fresh
    second factor, so the factor reset succeeds like the rest.
    """
    org = await users_env.create_org("sweep")
    admin = await signed_in(users_env, org, "org-admin")
    headers = admin.headers()
    role_id = await _global_role_id(users_env.admin_engine, "technician")
    first = await _invite(users_env, headers)
    second = await _invite(users_env, headers, email="bea@toeshee.com", display_name="Bea")
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    uid, other = first.json()["user_id"], second.json()["user_id"]
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
        "POST invite/resend": await users_env.client.post(
            f"/api/v1/users/{uid}/invite/resend", headers=headers
        ),
        "POST factors/reset": await users_env.client.post(
            f"/api/v1/users/{uid}/factors/reset", headers=headers
        ),
        "DELETE sessions": await users_env.client.delete(
            f"/api/v1/users/{uid}/sessions", headers=headers
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
        "DELETE invite": await users_env.client.delete(
            f"/api/v1/users/{other}/invite", headers=headers
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

    bodies = {"POST /users/invite": first.text + second.text, "POST role": str(assign)}
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
    """A guard against this surface's own assumption: an invitation link is
    made from `public_console_url` and the domain rule reads
    `console_email_domain`. A rename elsewhere would make every invitation a
    503, or switch the rule off."""
    fields = Settings.model_fields
    assert "public_console_url" in fields
    assert "console_email_domain" in fields


# --------------------------------------------------------------------------- #
# MSP clients Task 9 review (concern 1): user administration is STAFF only
# --------------------------------------------------------------------------- #


async def test_a_contact_is_not_a_user_this_surface_administers(users_env: Any) -> None:
    """A contact (`users.kind = 'contact'`, spec §1.3) is managed from its
    client's page (`/api/v1/contacts/{id}`), never here: it is not listed, and
    every read and write addressed to its id is a 404 that reaches no identity
    provider and not the row."""
    org = await users_env.create_org("ua-contact")
    _sub, headers, _admin_id = await _admin(users_env, org)
    technician = await _global_role_id(users_env.admin_engine, "technician")
    async with AsyncSession(users_env.admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org, name="Cliente X", kind="customer")
        session.add(client)
        await session.flush()
        contact = User(
            org_id=org,
            kind="contact",
            client_id=client.id,
            email="ana@x.test",
            external_subject=f"contact-pending:{uuid4()}",
        )
        session.add(contact)
        await session.flush()
        grant = UserRole(org_id=org, user_id=contact.id, role_id=technician)
        session.add(grant)
        await session.commit()

    listed = await users_env.client.get("/api/v1/users", headers=headers)
    assert listed.status_code == 200, listed.text
    assert _listed(listed.json(), contact.id) is None

    calls = [
        ("GET", f"/api/v1/users/{contact.id}/effective-permissions", None),
        ("POST", f"/api/v1/users/{contact.id}/roles", {"role_id": str(technician)}),
        ("DELETE", f"/api/v1/users/{contact.id}/roles/{grant.id}", None),
        ("POST", f"/api/v1/users/{contact.id}/disable", None),
        ("POST", f"/api/v1/users/{contact.id}/enable", None),
        ("POST", f"/api/v1/users/{contact.id}/invite/resend", None),
        ("DELETE", f"/api/v1/users/{contact.id}/invite", None),
        ("POST", f"/api/v1/users/{contact.id}/factors/reset", None),
        ("DELETE", f"/api/v1/users/{contact.id}/sessions", None),
        ("DELETE", f"/api/v1/users/{contact.id}", None),
    ]
    answers = []
    for method, path, body in calls:
        response = await users_env.client.request(method, path, json=body, headers=headers)
        answers.append((method, path, response.status_code))
    assert [a for a in answers if a[2] != 404] == []
    assert users_env.kc.seen == []
    row = await _user_row(users_env.admin_engine, contact.id)
    assert row is not None and (row.kind, row.status) == ("contact", "active")
    assert [a.id for a in await _assignments(users_env.admin_engine, contact.id)] == [grant.id]
