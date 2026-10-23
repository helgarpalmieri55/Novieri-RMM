"""The portal auth seam (spec §0.1, §3).

PortalContext is deliberately NOT an AuthContext: a portal principal must not
be passable to a staff dependency by mistake.

Gating decision (spec §3, as amended by this task): access is gated on
HOLDING A PORTAL PERMISSION CODE, not on `requester` role membership --
`portal.ticket.read`/`write` are also granted to `super-admin`/`org-admin`
(migration 0028), so an admin is legitimately admitted here too, and that is
NOT asserted as a refusal below. What IS asserted: a technician (holding no
portal permission) is refused, and a `requester` is admitted.
"""

import pytest
from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.rbac import route_permission
from app.modules.portal.principal import (
    PortalContext,
    require_portal_permission,
    resolve_portal_principal,
)


def _fake_request(api_env, headers: dict[str, str]) -> Request:
    """No existing test builds a bare `Request` to exercise a dependency
    directly (checked: no precedent in `backend/tests`), so this constructs
    the minimal ASGI `http` scope `Request` needs for the code paths this
    module's dependencies touch -- `request.headers` (for the bearer token)
    and `request.app` (for `.state.{admin,app}_engine`, `.state.settings`,
    `.state.jwks`). No ASGI transport/receive channel is needed because
    nothing here reads a request body.
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "app": api_env.app,
    }
    return Request(scope)


def test_portal_context_is_not_an_auth_context() -> None:
    """Spec §3 -- distinct types, so the two surfaces cannot be crossed by a
    careless dependency signature."""
    from app.api.deps import AuthContext

    assert not issubclass(PortalContext, AuthContext)
    assert "claims" not in PortalContext.__dataclass_fields__


def test_portal_context_carries_only_org_requester_and_client_scope() -> None:
    """No claims and nothing else from the token: the org, the requester, and
    the client scope `portal_session` pins as its third GUC (MSP clients spec
    §2.3)."""
    assert sorted(PortalContext.__dataclass_fields__) == ["clients", "org_id", "requester_user_id"]


async def test_resolve_refuses_a_principal_without_a_portal_permission(api_env) -> None:
    """A technician-tier user is authenticated but is NOT a portal user. Call
    resolve_portal_principal directly -- no route needed."""
    org_id = await api_env.create_org("portal-nrole")
    sub = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, sub)
    request = _fake_request(api_env, headers)
    with pytest.raises(HTTPException) as exc:
        await resolve_portal_principal(request)
    assert exc.value.status_code == 403


async def test_resolve_admits_a_requester(api_env) -> None:
    org_id = await api_env.create_org("portal-role")
    sub = await api_env.create_user(org_id, "requester")
    headers = await api_env.headers(org_id, sub)
    request = _fake_request(api_env, headers)
    ctx = await resolve_portal_principal(request)
    assert ctx.org_id == org_id


async def test_resolve_admits_a_write_only_principal(api_env) -> None:
    """M1 -- spec §3 gates admission on `portal.ticket.read` OR
    `portal.ticket.write`. An earlier version of this check hard-coded
    `portal.ticket.read` alone, so a principal holding ONLY
    `portal.ticket.write` got a 403 here before ever reaching a route --
    contradicting the spec's "or". Build a throwaway role holding write only
    (the seeded `requester` role holds both, so it alone can't catch this)."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Permission, Role, RolePermission, User, UserRole

    org_id = await api_env.create_org("portal-write-only")
    sub = f"write-only-{org_id.hex[:8]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
            session.add(user)
            await session.flush()

            role = Role(
                org_id=None, name=f"write-only-{org_id.hex[:8]}", description="write only, for M1"
            )
            session.add(role)
            await session.flush()

            write_permission_id = (
                await session.execute(
                    select(Permission.id).where(Permission.code == "portal.ticket.write")
                )
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=write_permission_id))
            session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))

    headers = await api_env.headers(org_id, sub)
    request = _fake_request(api_env, headers)
    ctx = await resolve_portal_principal(request)
    assert ctx.org_id == org_id


# ---------------------------------------------------------------------------
# route_permission recovery -- confirms `require_portal_permission` mirrors
# `require_permission`'s shape closely enough for the router-derived RBAC
# completeness mechanism (invariant #6) to work for portal routes too. This
# builds a throwaway route, exactly as `test_rbac.py`'s canary does for
# `require_permission` -- it needs no real `/api/v1/portal` route (those are
# Task 5's).
# ---------------------------------------------------------------------------


def test_route_permission_recovers_the_code_from_a_portal_dependency() -> None:
    router = APIRouter()

    @router.get("/canary")
    async def _canary_route(ctx=Depends(require_portal_permission("portal.ticket.write"))):  # type: ignore[no-untyped-def]  # noqa: B008
        return {}

    (route,) = router.routes
    assert route_permission(route) == "portal.ticket.write"
