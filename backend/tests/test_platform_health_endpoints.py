"""/livez and /readyz: what gates traffic, and what an unauthenticated caller
is allowed to learn.

NO DATABASE -- the app is built for route inspection and the registry is
replaced with stubs.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.probes import HARD_PROBES, router
from app.core.health.registry import ProbeRegistry
from app.core.health.types import ProbeResult, ReasonCode, Status


def _app(results: list[ProbeResult]) -> FastAPI:
    app = FastAPI()
    registry = ProbeRegistry(cache_ttl_s=0)
    for r in results:
        registry.register(r.name, (lambda _s, res=r: _immediate(res)), hard=r.name in HARD_PROBES)
    app.state.health_registry = registry
    app.include_router(router)
    return app


async def _immediate(result: ProbeResult) -> ProbeResult:
    return result


def _ok(name: str) -> ProbeResult:
    return ProbeResult(name, Status.OK, 1)


def _down(name: str, reason: ReasonCode = ReasonCode.UNREACHABLE) -> ProbeResult:
    return ProbeResult(name, Status.DOWN, 1, reason)


def test_the_hard_set_has_exactly_this_membership() -> None:
    """Adding to this set changes what takes the platform out of rotation.
    That should require deciding, not editing."""
    assert HARD_PROBES == frozenset({"postgres.app", "redis"})


def test_livez_is_200_with_every_dependency_down() -> None:
    """Liveness means the process can answer. If it cannot serve this, it
    cannot serve the response either -- which is the signal."""
    app = _app([_down("postgres.app"), _down("redis"), _down("vault")])
    assert TestClient(app).get("/livez").status_code == 200


@pytest.mark.parametrize("failing", ["postgres.app", "redis"])
def test_readyz_is_503_when_a_hard_probe_fails(failing: str) -> None:
    results = [_ok("postgres.app"), _ok("redis"), _ok("objectstore")]
    results = [_down(r.name) if r.name == failing else r for r in results]
    response = TestClient(_app(results)).get("/readyz")
    assert response.status_code == 503
    assert response.json()["failing"] == [failing]


def test_readyz_is_200_when_only_a_soft_probe_fails() -> None:
    """THE DEGRADED-NOT-DOWN PROPERTY. A MinIO outage costs backup artifacts
    and remote recordings; taking the control plane out of rotation for it
    would remove the operator's ability to diagnose it through the very API
    they need."""
    app = _app([_ok("postgres.app"), _ok("redis"), _down("objectstore")])
    assert TestClient(app).get("/readyz").status_code == 200


def test_readyz_leaks_no_reason_codes_to_an_unauthenticated_caller() -> None:
    """Names are enough for a load balancer to act. Reasons would let an
    unauthenticated caller map which backend is currently weak."""
    app = _app([_down("postgres.app", ReasonCode.AUTH_FAILED), _ok("redis")])
    body = TestClient(app).get("/readyz").text
    assert "postgres.app" in body
    assert "auth_failed" not in body
    assert "reason" not in body


# --- Task 5: the detail route --------------------------------------------------


def test_the_status_route_reports_modules_as_registered_not_healthy() -> None:
    """§1: modules are LISTED, not probed. The page must not imply otherwise,
    so the field name says what it measures.

    The registry is built once by `create_app` (`app.core.module_registry`)
    and stored on `app.state.module_registry` -- there is no standalone
    `_module_registry()` to call anymore, so this reads it the same way the
    route does, off a router-only app exactly like the rest of this module's
    NO-DATABASE tests."""
    from tests.tool_surface import build_router_only_app

    modules = build_router_only_app().state.module_registry
    assert len(modules) > 20
    entry = modules[0]
    assert set(entry) == {"name", "routes", "job_types"}
    assert "status" not in entry, "a module entry must not claim health"
    assert "healthy" not in entry


async def test_platform_status_reads_the_startup_registry_and_builds_no_second_app(
    api_env,
) -> None:
    """D-2/D-3 regression: the route used to build a whole second app (two
    more async engines, a Redis client, JWKS cache, CA, signer and vault) on
    every request, behind a monkeypatch of `app.jobs.registry.register` that
    two overlapping requests could race on. Now the registry is computed once
    at startup and the route only reads it -- so `app.state.module_registry`
    must already be populated right after `create_app()`, and handling a
    request must not replace it with a different object."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Role, User, UserRole

    registry_before = api_env.app.state.module_registry
    assert registry_before, "create_app() must populate the registry before any request"

    org_id = await api_env.create_org("platform-status")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(org_id=org_id, external_subject="platform-status-admin", email="p@x.test")
        session.add(user)
        await session.flush()
        role_stmt = select(Role).where(Role.name == "org-admin", Role.org_id.is_(None))
        role = (await session.execute(role_stmt)).scalar_one()
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
    headers = await api_env.headers(org_id, "platform-status-admin")

    response = await api_env.client.get("/api/v1/platform/status", headers=headers)
    assert response.status_code == 200
    assert response.json()["modules"], "the route must still report the module registry"

    assert api_env.app.state.module_registry is registry_before, (
        "handling a request replaced app.state.module_registry -- the route "
        "must read the startup-built registry, not rebuild one"
    )
