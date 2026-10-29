"""A deployment's window is checked before it is stored (final review B2
Important 1), the treatment a backup policy's got (`test_backup_policy_window`).

`POST /deploy/deployments` took any string as `window_cron`. The window sweep
reads it with `app.core.schedule.in_window`, which raises on anything that is
not five valid cron fields, and the sweep runs ONE transaction per
organisation: one bad expression rolled back every windowed deployment of
every client in the organisation, every 15 minutes, and `POST .../execute`
on it was a 500. There is no route that edits or deletes a deployment, so
nothing in the console could undo it.

Now: the create reads the expression with the parser backup uses
(`app.core.schedule.checked_window`, shared) and refuses what it refuses, a
422 with the same sentence; blank is no window. A value stored before the
check (or straight into the table) is contained to its own deployment by the
sweep, and `execute` on it is a 422 naming the problem.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, override_settings
from app.models import AuditEvent, Deployment, Device, Job, Organization, Package
from app.modules.deploy.handler import register_job_types
from tests.conftest import ApiEnv
from tests.mcp_support import (
    MCP_BASE,
    mcp_org,
    mcp_session,
    mcp_token,
    running_mcp,
    tool_error_text,
)
from tests.test_backup_policy_window import BAD, GOOD, NEVER, REFUSED, REFUSED_ES, UNREAD
from tests.test_deploy_dispatch import _FakeArtifactSigner, _FakeJobEngine

register_job_types()

API = "/api/v1/deploy"
UNREADABLE = "this deployment's window cannot be read ({window}); create it again with a valid one"
UNREADABLE_ES = (
    "la ventana de este despliegue no se puede leer ({window}); créalo de nuevo con una válida"
)


async def _target(engine: AsyncEngine, org: UUID) -> tuple[UUID, UUID]:
    """(a manager-sourced package, a device of the internal client)."""
    async with AsyncSession(engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org, client_id=org, site_id=org, hostname=f"d-{uuid4().hex[:6]}",
            os_family="windows",
        )
        name = f"Foo-{uuid4().hex[:6]}"
        package = Package(
            org_id=org, name=name, version="1.0", os_family="windows", installer_type="winget",
            package_identifier=f"Foo.{name}",
            detection_rule={"type": "package_installed", "name": name, "version": "1.0"},
        )
        s.add_all([device, package])
        await s.commit()
        return package.id, device.id


async def _written(env: ApiEnv, org: UUID) -> tuple[int, int]:
    async with AsyncSession(env.admin_engine) as s:
        deployments = (await s.execute(select(func.count()).select_from(Deployment).where(
            Deployment.org_id == org))).scalar_one()
        events = (await s.execute(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.org_id == org))).scalar_one()
    return deployments, events


def _body(package: UUID, device: UUID, cron: str | None) -> dict[str, Any]:
    return {"package_id": str(package), "action": "install", "target_type": "device",
            "target_id": str(device), "window_cron": cron}


def test_deploy_and_backup_share_one_check() -> None:
    from app.core import schedule
    from app.modules.backup import policy

    assert policy.checked_window is schedule.checked_window


@pytest.mark.parametrize("cron", [*BAD, *UNREAD, *NEVER])
async def test_the_api_refuses_a_window_the_sweep_cannot_read(
    api_env: ApiEnv, cron: str
) -> None:
    org = await api_env.create_org("dep-window-bad")
    headers = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    package, device = await _target(api_env.admin_engine, org)
    before = await _written(api_env, org)
    body = _body(package, device, cron)
    r = await api_env.client.post(f"{API}/deployments", json=body, headers=headers)
    assert (r.status_code, r.json()) == (422, {"detail": REFUSED}), r.text
    r = await api_env.client.post(f"{API}/deployments", json=body,
                                  headers={**headers, "Accept-Language": "es"})
    assert (r.status_code, r.json()) == (422, {"detail": REFUSED_ES}), r.text
    assert await _written(api_env, org) == before


@pytest.mark.parametrize(("cron", "stored"), GOOD)
async def test_the_api_stores_a_window_the_sweep_can_read(
    api_env: ApiEnv, cron: str | None, stored: str | None
) -> None:
    org = await api_env.create_org("dep-window-good")
    headers = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    package, device = await _target(api_env.admin_engine, org)
    r = await api_env.client.post(f"{API}/deployments", json=_body(package, device, cron),
                                  headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["window_cron"] == stored
    async with AsyncSession(api_env.admin_engine) as s:
        assert (await s.execute(select(Deployment.window_cron).where(
            Deployment.org_id == org))).scalar_one() == stored


@pytest.fixture()
async def mcp_env(api_env: ApiEnv) -> ApiEnv:
    api_env.settings.public_console_url = MCP_BASE
    return api_env


async def test_the_mcp_tool_meets_the_same_check(mcp_env: Any) -> None:
    org, slug = await mcp_org(mcp_env)
    sub = await mcp_env.create_user(org, "org-admin")
    package, device = await _target(mcp_env.admin_engine, org)
    token = mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env), mcp_session(mcp_env, token) as session:
        for cron in [*BAD, *UNREAD, *NEVER]:
            result = await session.call_tool(
                "deploy_deployments_create", {"body": _body(package, device, cron)}
            )
            assert tool_error_text(result) == f"invalid arguments: {REFUSED}", cron
    async with AsyncSession(mcp_env.admin_engine) as s:
        assert (await s.execute(select(func.count()).select_from(Deployment).where(
            Deployment.org_id == org))).scalar_one() == 0


async def _stored_unreadable(engine: AsyncEngine, org: UUID, window: str) -> UUID:
    """A pending deployment whose window is `window`, written straight to the
    table -- the way a value stored before the API checked it is held."""
    package, device = await _target(engine, org)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        deployment = Deployment(org_id=org, package_id=package, action="install",
                                target_type="device", target_id=device, window_cron=window,
                                state="pending")
        s.add(deployment)
        await s.commit()
        return deployment.id


async def test_executing_a_deployment_whose_window_cannot_be_read_is_a_422(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("dep-window-exec")
    headers = await api_env.headers(org, await api_env.create_user(org, "org-admin"))
    deployment = await _stored_unreadable(api_env.admin_engine, org, "0 2 * *")
    r = await api_env.client.post(f"{API}/deployments/{deployment}/execute", headers=headers)
    assert (r.status_code, r.json()) == (
        422, {"detail": UNREADABLE.format(window="0 2 * *")}
    ), r.text
    r = await api_env.client.post(f"{API}/deployments/{deployment}/execute",
                                  headers={**headers, "Accept-Language": "es"})
    assert (r.status_code, r.json()) == (
        422, {"detail": UNREADABLE_ES.format(window="0 2 * *")}
    ), r.text


async def test_one_unreadable_stored_window_stops_only_its_own_deployment(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    pg_cluster: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same sweep dispatches the organisation's other windowed deployment
    and leaves the unreadable one pending, logged once, without a traceback."""
    from app import worker

    slug = f"dep-contain-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        organization = Organization(name=slug, slug=slug)
        s.add(organization)
        await s.commit()
    org = organization.id
    unreadable = {
        window: await _stored_unreadable(admin_engine, org, window)
        for window in ("0 2 * *", "0-999999999 2 * * *")
    }
    valid = await _stored_unreadable(admin_engine, org, "* * * * *")

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
    )
    now = datetime(2026, 7, 19, 15, 30, tzinfo=UTC)
    with (
        pytest.MonkeyPatch.context() as mp,
        override_settings(settings),
        caplog.at_level(logging.WARNING),
    ):
        mp.setattr(worker, "JobEngine", lambda **_kw: _FakeJobEngine())
        mp.setattr(worker, "LazyObjectStoreSigner", lambda *_a: _FakeArtifactSigner())
        total = await worker.deploy_window_sweep_all_orgs(now)
    assert total >= 1

    async with AsyncSession(admin_engine) as s:
        states = dict((await s.execute(select(Deployment.id, Deployment.state).where(
            Deployment.org_id == org))).all())
        jobs = (await s.execute(select(func.count()).select_from(Job).where(
            Job.org_id == org))).scalar_one()
    assert states[valid] == "dispatching"
    assert {states[d] for d in unreadable.values()} == {"pending"}
    assert jobs == 1
    # One line per unreadable deployment, by type, with a constant message
    # (`tests/test_logging_discipline.py`): no stored value, no traceback.
    mine = [r for r in caplog.records
            if getattr(r, "deployment_id", None) in set(unreadable.values())]
    assert sorted(str(r.deployment_id) for r in mine) == sorted(
        str(d) for d in unreadable.values()
    )
    assert {(r.getMessage(), r.reason, r.exc_info) for r in mine} == {(
        "deploy window sweep: a deployment its own data stops was skipped; the others go on",
        "UnreadableWindow",
        None,
    )}
