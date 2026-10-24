"""Spec §2.1-2.2: per-client grants and the request scope they produce.

A technician's grants are per client (`user_roles.client_id`, NULL = every
client). A request's scope is the clients where the person holds the route's
code (`require_permission(code)`), or any role at all (an ungated route),
narrowed -- never widened -- by `X-Client-Id`. Every door computes it: the
HTTP dependency, the portal principal, the console WebSocket and the remote
tunnel. A contact (`users.kind = 'contact'`) never enters the console.

Role names: the seeded `technician` holds `devices.read`, `jobs.dispatch` and
`remote.control`; `read-only` holds `devices.read` and neither of the others.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, bound_auth_context, require_permission
from app.core.client_scope import ClientScope
from app.core.identity.door import read_door_identity
from app.core.rbac import clients_holding, has_permission
from app.core.security import TokenClaims
from app.core.tenancy import org_session
from app.models import Client, Role, User, UserRole
from app.modules.portal.principal import require_portal_permission, resolve_portal_principal
from app.modules.remote.tunnel import TunnelAuthError, authorize_tunnel

Grants = list[tuple[str, str | None]]


async def _seed(admin_engine: Any, env: Any, grants: Grants) -> tuple[UUID, UUID, dict[str, UUID]]:
    """An org with customers X and Z; `grants` is [(role name, 'X'|'Z'|None)]."""
    org_id = await env.create_org("rbac")
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        clients = {}
        for name in ("X", "Z"):
            c = Client(org_id=org_id, name=name, kind="customer")
            s.add(c)
            await s.flush()
            clients[name] = c.id
        user = User(org_id=org_id, external_subject=f"sub-{org_id}", email="ana@test.local")
        s.add(user)
        await s.flush()
        for role_name, client in grants:
            role = (
                await s.execute(select(Role).where(Role.name == role_name, Role.org_id.is_(None)))
            ).scalar_one()
            s.add(
                UserRole(
                    org_id=org_id,
                    user_id=user.id,
                    role_id=role.id,
                    client_id=clients[client] if client else None,
                )
            )
        await s.commit()
    return org_id, user.id, clients


async def _headers(env: Any, org_id: UUID, **extra: str) -> dict[str, str]:
    """`_seed`'s user signs in: its sub is `sub-<org id>`."""
    slug = await env.org_slug(org_id)
    token = env.idp.token(f"sub-{org_id}", slug, email="ana@test.local")
    return {"Authorization": f"Bearer {token}", **extra}


def _as_json(scope: ClientScope) -> dict[str, Any]:
    return {"all": scope.is_all, "ids": sorted(str(i) for i in scope.ids)}


def _ids(*ids: UUID) -> dict[str, Any]:
    return {"all": False, "ids": sorted(str(i) for i in ids)}


ALL = {"all": True, "ids": []}


def _probe_router() -> APIRouter:
    """Routes that answer with the scope their request was given."""
    router = APIRouter()

    @router.get("/scope-probe/any")
    async def any_role(ctx: AuthContext = Depends(bound_auth_context)) -> dict[str, Any]:  # noqa: B008
        return _as_json(ctx.clients)

    @router.get("/scope-probe/read")
    async def read(
        ctx: AuthContext = Depends(require_permission("devices.read")),  # noqa: B008
    ) -> dict[str, Any]:
        return _as_json(ctx.clients)

    @router.get("/scope-probe/dispatch")
    async def dispatch(
        ctx: AuthContext = Depends(require_permission("jobs.dispatch")),  # noqa: B008
    ) -> dict[str, Any]:
        return _as_json(ctx.clients)

    return router


@pytest.fixture()
async def probe_env(api_env: Any) -> Any:
    api_env.app.include_router(_probe_router(), prefix="/api/v1")
    return api_env


def _fake_request(env: Any, headers: dict[str, str]) -> Request:
    """The minimal ASGI scope `resolve_portal_principal` reads (as in
    `tests/test_portal_principal.py`)."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "app": env.app,
    }
    return Request(scope)


async def _make_contact(env: Any, org_id: UUID, user_id: UUID, client_id: UUID) -> None:
    async with AsyncSession(env.admin_engine) as s:
        user = await s.get(User, user_id)
        assert user is not None
        user.kind, user.client_id = "contact", client_id
        await s.commit()


# --------------------------------------------------------------------------- #
# rbac: what a grant on a client counts for
# --------------------------------------------------------------------------- #


async def test_a_client_grant_counts_only_on_that_client(api_env: Any) -> None:
    env = api_env
    org, user, c = await _seed(env.admin_engine, env, [("technician", "X")])
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        assert await has_permission(s, user, "devices.read", client_id=c["X"])
        assert not await has_permission(s, user, "devices.read", client_id=c["Z"])
        assert not await has_permission(s, user, "devices.read")  # no all-clients grant


async def test_clients_holding_follows_the_permission(api_env: Any) -> None:
    env = api_env
    org, user, c = await _seed(env.admin_engine, env, [("technician", "X"), ("read-only", "Z")])
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        assert await clients_holding(s, user, "devices.read") == ClientScope.only([c["X"], c["Z"]])
        assert await clients_holding(s, user, "jobs.dispatch") == ClientScope.only([c["X"]])
        assert await clients_holding(s, user, None) == ClientScope.only([c["X"], c["Z"]])


async def test_an_all_clients_grant_is_all(api_env: Any) -> None:
    env = api_env
    org, user, _ = await _seed(env.admin_engine, env, [("technician", None)])
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        assert await clients_holding(s, user, "devices.read") == ClientScope.all()
        assert await clients_holding(s, user, None) == ClientScope.all()


async def test_no_grant_is_the_empty_scope_and_an_unknown_code_is_a_programming_error(
    api_env: Any,
) -> None:
    env = api_env
    org, user, _ = await _seed(env.admin_engine, env, [])
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        assert await clients_holding(s, user, None) == ClientScope.only([])
        assert await clients_holding(s, user, "devices.read") == ClientScope.only([])
        with pytest.raises(ValueError):
            await clients_holding(s, user, "no.such.code")


# --------------------------------------------------------------------------- #
# Door 1: the HTTP request's scope
# --------------------------------------------------------------------------- #


async def test_the_request_scope_is_the_clients_holding_the_routes_code(probe_env: Any) -> None:
    """Ana is technician on X and read-only on Z: a route that needs
    `jobs.dispatch` sees X only; Z does not exist for that request."""
    env = probe_env
    org, _, c = await _seed(env.admin_engine, env, [("technician", "X"), ("read-only", "Z")])
    h = await _headers(env, org)
    assert (await env.client.get("/api/v1/scope-probe/any", headers=h)).json() == _ids(
        c["X"], c["Z"]
    )
    assert (await env.client.get("/api/v1/scope-probe/read", headers=h)).json() == _ids(
        c["X"], c["Z"]
    )
    assert (await env.client.get("/api/v1/scope-probe/dispatch", headers=h)).json() == _ids(
        c["X"]
    )


async def test_holding_the_code_in_no_client_is_403(probe_env: Any) -> None:
    env = probe_env
    org, _, _ = await _seed(env.admin_engine, env, [("read-only", "Z")])
    r = await env.client.get("/api/v1/scope-probe/dispatch", headers=await _headers(env, org))
    assert r.status_code == 403
    assert r.json()["detail"] == "requires jobs.dispatch"


async def test_an_all_clients_grant_gives_an_all_scope(probe_env: Any) -> None:
    env = probe_env
    org, _, c = await _seed(env.admin_engine, env, [("technician", None)])
    h = await _headers(env, org)
    assert (await env.client.get("/api/v1/scope-probe/dispatch", headers=h)).json() == ALL
    narrowed = await _headers(env, org, **{"X-Client-Id": str(c["Z"])})
    assert (await env.client.get("/api/v1/scope-probe/dispatch", headers=narrowed)).json() == (
        _ids(c["Z"])
    )


async def test_x_client_id_narrows_and_never_widens(probe_env: Any) -> None:
    env = probe_env
    org, _, c = await _seed(env.admin_engine, env, [("technician", "X"), ("read-only", "Z")])

    async def probe(path: str, client: UUID) -> Any:
        h = await _headers(env, org, **{"X-Client-Id": str(client)})
        r = await env.client.get(f"/api/v1/scope-probe/{path}", headers=h)
        assert r.status_code == 200, r.text
        return r.json()

    assert await probe("any", c["Z"]) == _ids(c["Z"])
    assert await probe("dispatch", c["X"]) == _ids(c["X"])
    # Z is in the person's scope, but not where they hold `jobs.dispatch`:
    # the intersection is empty, never the un-narrowed scope.
    assert await probe("dispatch", c["Z"]) == _ids()
    # A client they hold nothing on (or that does not exist) is empty too.
    assert await probe("any", uuid4()) == _ids()
    assert await probe("read", org) == _ids()  # the internal client: no grant there


@pytest.mark.parametrize("value", ["nope", "", f"{uuid4()},{uuid4()}"])
async def test_a_malformed_x_client_id_is_400(probe_env: Any, value: str) -> None:
    env = probe_env
    org, _, _ = await _seed(env.admin_engine, env, [("technician", None)])
    h = await _headers(env, org, **{"X-Client-Id": value})
    r = await env.client.get("/api/v1/me", headers=h)
    assert r.status_code == 400
    assert r.json()["detail"] == "X-Client-Id must be one client id"


async def test_a_contact_is_refused_at_the_console(api_env: Any) -> None:
    env = api_env
    org = await env.create_org("contact")
    sub = await env.create_user(org, "technician")
    async with AsyncSession(env.admin_engine) as s:
        user = (await s.execute(select(User).where(User.external_subject == sub))).scalar_one()
        user.kind, user.client_id = "contact", org  # the internal client (id convention)
        await s.commit()
    r = await env.client.get("/api/v1/me", headers=await env.headers(org, sub))
    assert r.status_code == 403
    assert r.json()["detail"] == "this account can only use the portal"
    # Refused inside the admission transaction: it records no login.
    async with AsyncSession(env.admin_engine) as s:
        user = (await s.execute(select(User).where(User.external_subject == sub))).scalar_one()
        assert user.last_login_at is None


# Global configuration (a per-client grant is not a grant on every client) is
# `tests/test_global_writes.py`, against its one declared list of routes.


# --------------------------------------------------------------------------- #
# Doors 2 and 3: the console WebSocket and the remote tunnel
# --------------------------------------------------------------------------- #


async def test_the_door_identity_carries_the_persons_scope(api_env: Any) -> None:
    env = api_env
    org, user, c = await _seed(env.admin_engine, env, [("technician", "X"), ("read-only", "Z")])
    slug = await env.org_slug(org)

    async def identity(sub: str, code: str | None) -> Any:
        claims = TokenClaims(sub=sub, email="ana@test.local", name="Ana", org_slug=slug)
        return await read_door_identity(
            door="test",
            org_id=org,
            claims=claims,
            settings=env.settings,
            engine=env.app_engine,
            code=code,
        )

    ws = await identity(f"sub-{org}", None)
    assert (ws.refusal, ws.user_id) == (None, user)
    assert ws.clients == ClientScope.only([c["X"], c["Z"]])
    tunnel = await identity(f"sub-{org}", "remote.control")
    assert tunnel.clients == ClientScope.only([c["X"]])
    unknown = await identity("nobody-at-all", None)
    assert (unknown.user_id, unknown.clients) == (None, ClientScope.only([]))


async def test_a_contact_is_refused_at_the_websocket_and_tunnel_doors(api_env: Any) -> None:
    env = api_env
    org, user, c = await _seed(env.admin_engine, env, [("technician", None)])
    await _make_contact(env, org, user, c["X"])
    slug = await env.org_slug(org)
    claims = TokenClaims(sub=f"sub-{org}", email="ana@test.local", name="Ana", org_slug=slug)
    identity = await read_door_identity(
        door="test", org_id=org, claims=claims, settings=env.settings, engine=env.app_engine
    )
    assert identity.refusal == "this account can only use the portal"
    assert identity.clients == ClientScope.only([])


async def _tunnel(env: Any, org: UUID) -> tuple[UUID, UUID, ClientScope]:
    slug = await env.org_slug(org)
    return await authorize_tunnel(
        token=env.idp.token(f"sub-{org}", slug, email="ana@test.local"),
        session_id=uuid4(),
        settings=env.settings,
        jwks=env.app.state.jwks,
        admin_engine=env.admin_engine,
        app_engine=env.app_engine,
    )


async def test_the_tunnel_admits_remote_control_held_on_one_client(api_env: Any) -> None:
    """`remote.control` on X alone passes the tunnel's permission check (the
    session lookup then answers: there is no such session) ..."""
    env = api_env
    org, _, _ = await _seed(env.admin_engine, env, [("technician", "X")])
    with pytest.raises(TunnelAuthError) as exc:
        await _tunnel(env, org)
    assert exc.value.code == 4404


async def test_the_tunnel_refuses_remote_control_held_nowhere(api_env: Any) -> None:
    """... and holding it on no client is the 4403 it always was."""
    env = api_env
    org, _, _ = await _seed(env.admin_engine, env, [("read-only", "X")])
    with pytest.raises(TunnelAuthError) as exc:
        await _tunnel(env, org)
    assert exc.value.code == 4403


# --------------------------------------------------------------------------- #
# The portal
# --------------------------------------------------------------------------- #


async def test_a_contact_reaches_the_portal_with_its_own_client_only(api_env: Any) -> None:
    """Even an all-clients grant cannot widen a contact past its client."""
    env = api_env
    org, user, c = await _seed(env.admin_engine, env, [("requester", None)])
    await _make_contact(env, org, user, c["X"])
    pctx = await resolve_portal_principal(_fake_request(env, await _headers(env, org)))
    assert pctx.requester_user_id == user
    assert pctx.clients == ClientScope.only([c["X"]])
    gate = require_portal_permission("portal.ticket.read")
    request = _fake_request(env, await _headers(env, org))
    assert (await gate(request, pctx)).clients == ClientScope.only([c["X"]])


async def test_a_contact_without_a_portal_grant_on_its_client_is_refused(api_env: Any) -> None:
    env = api_env
    org, user, c = await _seed(env.admin_engine, env, [("requester", "Z")])
    await _make_contact(env, org, user, c["X"])
    with pytest.raises(HTTPException) as exc:
        await resolve_portal_principal(_fake_request(env, await _headers(env, org)))
    assert exc.value.status_code == 403


async def test_a_technician_at_the_portal_has_the_clients_of_their_roles(api_env: Any) -> None:
    env = api_env
    org, _, c = await _seed(env.admin_engine, env, [("requester", "X"), ("read-only", "Z")])
    request = _fake_request(env, await _headers(env, org))
    pctx = await resolve_portal_principal(request)
    assert pctx.clients == ClientScope.only([c["X"], c["Z"]])
    gated = await require_portal_permission("portal.ticket.read")(request, pctx)
    assert gated.clients == ClientScope.only([c["X"]])
