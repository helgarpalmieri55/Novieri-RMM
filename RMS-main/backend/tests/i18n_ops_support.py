"""Helpers for tests/test_i18n_modules_ops.py (not a test module).

They build the worlds the per-module Spanish tests need, from the shared
fixture modules, so the test file imports no other test module."""

from typing import Any
from uuid import UUID

import pytest

from app.modules.agent_distribution.link_page import LinkRelease
from tests.agent_push_fixtures import (
    seed_asset,
    seed_credential,
    seed_org,
    seed_push_authorization,
    seed_push_credential_scope,
    seed_push_site_server,
)
from tests.agent_update_support import seed_device, seed_release, unique_version
from tests.install_link_fixtures import switch_on
from tests.msi_fixtures import StoreWith, offer, signed_release

PUSH = "/api/v1/agent-push"
MSI_URL = "/api/v1/install-link/msi"
CONSOLE = "https://itms.example.com"

#: One stable release per platform the install page can offer.
RELEASES = {
    ("windows", "amd64"): LinkRelease("1.6.2", "windows", "amd64", "b" * 64, "sig-win-amd64"),
    ("windows", "arm64"): LinkRelease("1.6.2", "windows", "arm64", "c" * 64, "sig-win-arm64"),
    ("linux", "amd64"): LinkRelease("1.6.2", "linux", "amd64", "b" * 64),
    ("darwin", "arm64"): LinkRelease("1.6.2", "darwin", "arm64", "b" * 64),
}


async def msi_world(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    """An org with the install link on and a signed 1.7.0 Windows agent on offer.
    Returns `(org_id, code, release, store, admin_headers)`."""
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", CONSOLE)
    org_id = await api_env.create_org("i18n-msi")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    release = signed_release("1.7.0")
    offer(monkeypatch, release)
    monkeypatch.setattr(api_env.app.state.settings, "agent_release_public_key", release.public_key)
    store = StoreWith({(release.version, "windows", "amd64"): release.data})
    api_env.app.state.object_store = store
    admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    return org_id, code, release, store, admin


async def push_world(api_env: Any, tag: str) -> tuple[Any, dict[str, str], UUID]:
    """Every invariant 2a bound satisfied for 10.90.0.17, built through the admin
    engine. Returns `(org, admin_headers, credential_id)`."""
    org = await seed_org(api_env, tag)
    sub = await api_env.create_user(org.org_id, "org-admin")
    headers = await api_env.headers(org.org_id, sub)
    credential_id = await seed_credential(api_env, org)
    await seed_asset(api_env, org.org_id, primary_ip="10.90.0.17")
    await seed_push_authorization(api_env, org, "10.90.0.0/24")
    await seed_push_credential_scope(api_env, org, credential_id, "10.90.0.0/24")
    await seed_push_site_server(api_env, org.org_id, segments=["10.90.0.0/24"])
    return org, headers, credential_id


async def active_rollout_release(api_env: Any) -> tuple[dict[str, str], UUID]:
    """A release that an active rollout still uses. Returns the super-admin's
    headers and the release id."""
    org_id = await api_env.create_org("i18n-rollout")
    admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    super_admin = await api_env.headers(org_id, await api_env.create_user(org_id, "super-admin"))
    version = unique_version()
    release_id = await seed_release(api_env.admin_engine, version)
    await seed_device(api_env.admin_engine, org_id)
    created = await api_env.client.post(
        "/api/v1/agent/update-rollouts",
        json={"target_version": version, "rings": [{"name": "pilot", "is_pilot": True}]},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    started = await api_env.client.post(
        f"/api/v1/agent/update-rollouts/{created.json()['id']}/start", headers=admin
    )
    assert started.status_code == 200, started.text
    return super_admin, release_id
