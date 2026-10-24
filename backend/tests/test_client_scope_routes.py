"""Every route that takes a site or a client, against the CLIENT scope.

MSP clients spec §2.2: "un ID de un cliente fuera del alcance da 404". The org
tests beside these (`test_client_enrollment.py`, `test_client_device_groups.py`)
use another ORGANISATION's site or client, which an application-level
`org_id` filter refuses on its own. These use two clients of ONE
organisation, X and Y, and a technician who holds every code on X only --
so only the client scope (RLS, `require_permission`'s narrowed session) stands
between them and Y. Each route is shown refusing Y with a 404 and serving X.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models.core import AgentRelease
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv
from tests.install_link_fixtures import link_path
from tests.test_agent_release_api import _StubStore

WINDOWS = {"all": [{"fact": "os_family", "op": "eq", "value": "windows"}]}


@dataclass
class World:
    org: UUID
    x_client: UUID
    x_site: UUID
    y_client: UUID
    y_site: UUID
    tech: dict[str, str]
    release: UUID


async def _release(env: ApiEnv, org: UUID) -> UUID:
    """A Linux release with bytes, so an install command and a deployment token
    can be minted. The catalogue is global, so it is written on the APP engine
    under the every-client scope -- not on the admin engine."""
    async with org_session(org, engine=env.app_engine, clients=ClientScope.all()) as s:
        release = AgentRelease(
            version=f"v{uuid4().hex[:10]}",
            os="linux",
            arch="amd64",
            storage_ref="agent-releases/x",
            sha256="a" * 64,
            size_bytes=18_874_368,
        )
        s.add(release)
        await s.commit()
        release_id = release.id
    env.app.state.object_store = _StubStore()
    return release_id


@pytest.fixture()
async def world(api_env: ApiEnv) -> World:
    org = await api_env.create_org("cscope")
    x_client, x_site = await client_with_site(api_env, org, "Cliente X")
    y_client, y_site = await client_with_site(api_env, org, "Cliente Y")
    return World(
        org=org,
        x_client=x_client,
        x_site=x_site,
        y_client=y_client,
        y_site=y_site,
        tech=await everything_on(api_env, org, x_client),
        release=await _release(api_env, org),
    )


def _token_body(site: UUID) -> dict[str, Any]:
    return {"name": "t", "max_uses": 1, "expires_in_hours": 1, "site_id": str(site)}


async def _post(env: ApiEnv, w: World, path: str, body: dict[str, Any]) -> int:
    return (await env.client.post(path, json=body, headers=w.tech)).status_code


async def test_token_mint(api_env: ApiEnv, world: World) -> None:
    path = "/api/v1/enrollment-tokens"
    assert await _post(api_env, world, path, _token_body(world.y_site)) == 404
    assert await _post(api_env, world, path, _token_body(world.x_site)) == 201


async def test_install_command(api_env: ApiEnv, world: World) -> None:
    path = f"/api/v1/agent/releases/{world.release}/install-command"
    body = {"expires_in_hours": 4}
    assert await _post(api_env, world, path, body | {"site_id": str(world.y_site)}) == 404
    assert await _post(api_env, world, path, body | {"site_id": str(world.x_site)}) == 200


async def test_deployment_token(api_env: ApiEnv, world: World) -> None:
    path = f"/api/v1/agent/releases/{world.release}/deployment-token"
    body = {"max_uses": 5, "expires_in_hours": 24}
    assert await _post(api_env, world, path, body | {"site_id": str(world.y_site)}) == 404
    assert await _post(api_env, world, path, body | {"site_id": str(world.x_site)}) == 200


async def test_install_link_read(api_env: ApiEnv, world: World) -> None:
    y = await api_env.client.get(link_path(world.y_site), headers=world.tech)
    x = await api_env.client.get(link_path(world.x_site), headers=world.tech)
    assert (y.status_code, x.status_code) == (404, 200)


async def test_install_link_save(api_env: ApiEnv, world: World) -> None:
    body = {"enabled": True, "networks": ["10.50.0.0/16"]}
    y = await api_env.client.put(link_path(world.y_site), json=body, headers=world.tech)
    x = await api_env.client.put(link_path(world.x_site), json=body, headers=world.tech)
    assert (y.status_code, x.status_code) == (404, 200)


async def test_install_link_regenerate(api_env: ApiEnv, world: World) -> None:
    assert await _post(api_env, world, link_path(world.y_site, "/regenerate"), {}) == 404
    assert await _post(api_env, world, link_path(world.x_site, "/regenerate"), {}) == 200


async def test_install_link_msi(api_env: ApiEnv, world: World) -> None:
    """X's link is off, so its MSI is a 409 that says so -- the site was found.
    Y's site is not found at all."""
    body = {"code": "a" * 32, "arch": "amd64"}
    assert await _post(api_env, world, link_path(world.y_site, "/msi"), body) == 404
    assert await _post(api_env, world, link_path(world.x_site, "/msi"), body) == 409


async def test_asset_create(api_env: ApiEnv, world: World) -> None:
    body = {"description": "boxed laptop"}
    path = "/api/v1/devices"
    assert await _post(api_env, world, path, body | {"site_id": str(world.y_site)}) == 404
    assert await _post(api_env, world, path, body | {"site_id": str(world.x_site)}) == 201


async def test_group_create(api_env: ApiEnv, world: World) -> None:
    path = "/api/v1/device-groups"
    y = {"name": "servers", "client_id": str(world.y_client)}
    x = {"name": "servers", "client_id": str(world.x_client)}
    assert await _post(api_env, world, path, y) == 404
    assert await _post(api_env, world, path, x) == 201


async def test_rule_preview(api_env: ApiEnv, world: World) -> None:
    path = "/api/v1/device-groups/preview"
    y = {"rules": WINDOWS, "client_id": str(world.y_client)}
    x = {"rules": WINDOWS, "client_id": str(world.x_client)}
    assert await _post(api_env, world, path, y) == 404
    assert await _post(api_env, world, path, x) == 200
