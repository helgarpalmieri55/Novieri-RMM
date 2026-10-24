"""Application deployment REST API tests (docs/modules/app-deploy/spec.md
§12).

Exercises server-side RBAC on every route (invariant #6), the signed
`app.deploy` dispatch reaching the agent (via the live redis dispatch
channel, mirroring ``tests/test_patch_api.py``'s
``test_scan_dispatches_signed_job``), correct actor attribution on the audit
log (operator-initiated actions carry ``actor_type="user"`` — verifying
Task 5's actor threading through ``store_artifact``), and the
``deployment_report``-derived 404 for an unknown deployment id. Uses the
``api_env`` in-process ASGI harness.
"""

import asyncio
import json
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import AuditEvent, Device, Package, User

REDIS_URL = "redis://localhost:6379/9"


async def _device(api_env, org_id: UUID, hostname: str = "wks-deploy") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        dev = Device(
            org_id=org_id, hostname=hostname, os_family="windows", client_id=org_id, site_id=org_id
        )
        s.add(dev)
        await s.commit()
        return dev.id


async def _package(api_env, org_id: UUID, name: str = "Acme", version: str = "1.0") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        pkg = Package(
            org_id=org_id,
            name=name,
            version=version,
            os_family="windows",
            installer_type="msi",
        )
        s.add(pkg)
        await s.commit()
        return pkg.id


async def _user_id(api_env, org_id: UUID, sub: str) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        return (
            await s.execute(
                select(User.id).where(User.org_id == org_id, User.external_subject == sub)
            )
        ).scalar_one()


# --------------------------------------------------------------------------- #
# GET|POST /packages
# --------------------------------------------------------------------------- #


async def test_list_packages_allowed_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/deploy/packages", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_create_package_forbidden_for_technician_ok_for_org_admin(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    tech = await api_env.create_user(org_id, "technician")
    admin = await api_env.create_user(org_id, "org-admin")
    body = {
        "name": "Acme Client",
        "version": "1.0",
        "os_family": "windows",
        "installer_type": "msi",
    }

    forbidden = await api_env.client.post(
        "/api/v1/deploy/packages", json=body, headers=await api_env.headers(org_id, tech)
    )
    assert forbidden.status_code == 403

    ok = await api_env.client.post(
        "/api/v1/deploy/packages", json=body, headers=await api_env.headers(org_id, admin)
    )
    assert ok.status_code == 201, ok.text
    out = ok.json()
    assert out["name"] == "Acme Client"
    assert out["artifact_ref"] is None


async def test_delete_package_not_found(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.delete(
        f"/api/v1/deploy/packages/{uuid4()}", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# POST /packages/{id}/artifact
# --------------------------------------------------------------------------- #


async def test_upload_artifact_audits_with_user_actor(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    package_id = await _package(api_env, org_id)
    expected_user_id = await _user_id(api_env, org_id, admin)

    with mock_aws():
        store = ObjectStore(api_env.settings)
        # Pin the moto-backed store so the lazily-built one in the route
        # doesn't reconstruct against the real S3 endpoint.
        api_env.app.state.object_store = store

        resp = await api_env.client.post(
            f"/api/v1/deploy/packages/{package_id}/artifact",
            content=b"MSI-BYTES",
            headers=await api_env.headers(org_id, admin),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["artifact_ref"] is not None
        assert body["artifact_sha256"]

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "deploy.package_artifact_uploaded"
                )
            )
        ).scalar_one()
    assert event.actor_type == "user"
    assert event.actor_id == expected_user_id


async def test_upload_artifact_forbidden_for_technician(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    tech = await api_env.create_user(org_id, "technician")
    package_id = await _package(api_env, org_id)
    resp = await api_env.client.post(
        f"/api/v1/deploy/packages/{package_id}/artifact",
        content=b"MSI-BYTES",
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 403


async def test_upload_artifact_not_found(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        resp = await api_env.client.post(
            f"/api/v1/deploy/packages/{uuid4()}/artifact",
            content=b"MSI-BYTES",
            headers=await api_env.headers(org_id, admin),
        )
    assert resp.status_code == 404


async def test_upload_artifact_oversized_body_is_413(api_env, monkeypatch) -> None:
    """P2: a bare `await request.body()` would buffer an entire, unbounded
    upload in process memory before this handler ever saw a byte of it --
    any `deploy.package.manage` holder could OOM the control plane with one
    request. The real cap (`_MAX_ARTIFACT_BYTES`, 2 GiB -- deliberately much
    larger than budget's 10 MiB invoice cap, since this module ships real
    installers) is far too large to exercise directly in a test, so this
    monkeypatches it down to a small value and proves the shared
    `app.core.http.reject_oversized_content_length` fast-path rejects a body
    over it. A plain `bytes` body (as sent here) gets httpx-declared
    `Content-Length`, so this specifically exercises the HEADER fast-path,
    not the streaming guard (see the chunked variant below)."""
    import app.modules.deploy.api as deploy_api

    monkeypatch.setattr(deploy_api, "_MAX_ARTIFACT_BYTES", 1000)

    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    package_id = await _package(api_env, org_id)

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        resp = await api_env.client.post(
            f"/api/v1/deploy/packages/{package_id}/artifact",
            content=b"x" * 1001,
            headers=await api_env.headers(org_id, admin),
        )
        assert resp.status_code == 413, resp.text


async def test_upload_artifact_oversized_chunked_body_is_413_via_streaming_guard(
    api_env, monkeypatch
) -> None:
    """The OTHER half of P2's guard, and the one that actually matters
    against a hostile client: `Content-Length` is client-supplied, and a
    chunked request (`Transfer-Encoding: chunked`, no declared length at
    all) sails straight past `reject_oversized_content_length`'s fast-path --
    that check simply returns when the header is absent. This proves
    `app.core.http.read_bounded_body`'s STREAMING enforcement independently
    rejects an oversized body with no declared length, by posting an async
    generator body (which httpx sends chunked, never setting
    `Content-Length` -- see the identical technique in
    `tests/test_budget_api.py`'s chunked-attachment-upload test). Without a
    test like this, only the header fast-path is ever exercised in the
    suite (a normal `bytes` body always carries a `Content-Length`), and the
    guard that exists specifically for the untrusted/chunked case never
    runs."""
    import app.modules.deploy.api as deploy_api

    monkeypatch.setattr(deploy_api, "_MAX_ARTIFACT_BYTES", 1000)

    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    package_id = await _package(api_env, org_id)

    async def _chunks():
        yield b"x" * 600
        yield b"x" * 600  # 1200 bytes total, over the 1000-byte cap

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        resp = await api_env.client.post(
            f"/api/v1/deploy/packages/{package_id}/artifact",
            content=_chunks(),
            headers=await api_env.headers(org_id, admin),
        )
        assert resp.status_code == 413, resp.text


# --------------------------------------------------------------------------- #
# POST /deployments, POST /deployments/{id}/execute
# --------------------------------------------------------------------------- #


async def test_create_deployment_ok_for_technician(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    tech = await api_env.create_user(org_id, "technician")
    package_id = await _package(api_env, org_id)
    device_id = await _device(api_env, org_id)

    resp = await api_env.client.post(
        "/api/v1/deploy/deployments",
        json={
            "package_id": str(package_id),
            "action": "install",
            "target_type": "device",
            "target_id": str(device_id),
        },
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "pending"
    assert body["package_id"] == str(package_id)


async def test_create_deployment_forbidden_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    ro = await api_env.create_user(org_id, "read-only")
    package_id = await _package(api_env, org_id)
    device_id = await _device(api_env, org_id)
    resp = await api_env.client.post(
        "/api/v1/deploy/deployments",
        json={
            "package_id": str(package_id),
            "action": "install",
            "target_type": "device",
            "target_id": str(device_id),
        },
        headers=await api_env.headers(org_id, ro),
    )
    assert resp.status_code == 403


async def test_execute_deployment_dispatches_signed_job_and_audits_user_actor(
    api_env,
) -> None:
    org_id = await api_env.create_org("deploy")
    tech = await api_env.create_user(org_id, "technician")
    admin = await api_env.create_user(org_id, "org-admin")
    package_id = await _package(api_env, org_id)
    device_id = await _device(api_env, org_id)
    expected_user_id = await _user_id(api_env, org_id, tech)
    headers = await api_env.headers(org_id, tech)

    # An `msi` package is artifact-backed, and dispatch now presigns its
    # artifact into the signed params -- so it needs one uploaded. Executing a
    # deployment of an artifact-backed package with nothing uploaded is a 422
    # (see `test_execute_deployment_without_an_uploaded_artifact_is_422`), not
    # a signed job the agent would refuse in the field.
    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        uploaded = await api_env.client.post(
            f"/api/v1/deploy/packages/{package_id}/artifact",
            content=b"MSI-BYTES",
            headers=await api_env.headers(org_id, admin),
        )
        assert uploaded.status_code == 200, uploaded.text

    create_resp = await api_env.client.post(
        "/api/v1/deploy/deployments",
        json={
            "package_id": str(package_id),
            "action": "install",
            "target_type": "device",
            "target_id": str(device_id),
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    deployment_id = create_resp.json()["id"]

    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.psubscribe(f"dispatch:{device_id}")

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        resp = await api_env.client.post(
            f"/api/v1/deploy/deployments/{deployment_id}/execute", headers=headers
        )
    assert resp.status_code == 200, resp.text
    job_ids = resp.json()["job_ids"]
    assert len(job_ids) == 1

    published = None
    for _ in range(30):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message and message["type"] == "pmessage":
            published = json.loads(message["data"])
            break
        await asyncio.sleep(0.05)
    await pubsub.aclose()
    await redis.aclose()
    assert published is not None, "expected an app.deploy job to be published"
    assert published["job"]["id"] == job_ids[0]
    assert published["sig"]

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "deploy.dispatched")
            )
        ).scalar_one()
    assert event.actor_type == "user"
    assert event.actor_id == expected_user_id


async def test_execute_deployment_forbidden_for_technician_without_execute(api_env) -> None:
    # technician has deploy.execute per the seeded matrix; read-only does not.
    org_id = await api_env.create_org("deploy")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.post(
        f"/api/v1/deploy/deployments/{uuid4()}/execute",
        headers=await api_env.headers(org_id, ro),
    )
    assert resp.status_code == 403


async def test_get_deployment_unknown_id_is_404(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    tech = await api_env.create_user(org_id, "technician")
    resp = await api_env.client.get(
        f"/api/v1/deploy/deployments/{uuid4()}", headers=await api_env.headers(org_id, tech)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# GET|POST /scripts, POST /scripts/{id}/run
# --------------------------------------------------------------------------- #


async def test_create_script_forbidden_for_technician(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    tech = await api_env.create_user(org_id, "technician")
    resp = await api_env.client.post(
        "/api/v1/deploy/scripts",
        json={
            "name": "cleanup",
            "os_family": "windows",
            "interpreter": "powershell",
            "body": "Write-Host hi",
        },
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 403


async def test_create_script_ok_for_org_admin(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.post(
        "/api/v1/deploy/scripts",
        json={
            "name": "cleanup",
            "os_family": "windows",
            "interpreter": "powershell",
            "body": "Write-Host hi",
        },
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 1
    assert body["body"] == "Write-Host hi"


async def test_list_scripts_omits_body(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    await api_env.client.post(
        "/api/v1/deploy/scripts",
        json={
            "name": "cleanup",
            "os_family": "windows",
            "interpreter": "powershell",
            "body": "Write-Host hi",
        },
        headers=await api_env.headers(org_id, admin),
    )
    resp = await api_env.client.get(
        "/api/v1/deploy/scripts", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert "body" not in items[0]


async def test_run_script_ok_for_technician(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env, org_id)

    create_resp = await api_env.client.post(
        "/api/v1/deploy/scripts",
        json={
            "name": "cleanup",
            "os_family": "windows",
            "interpreter": "powershell",
            "body": "Write-Host hi",
        },
        headers=await api_env.headers(org_id, admin),
    )
    script_id = create_resp.json()["id"]

    resp = await api_env.client.post(
        f"/api/v1/deploy/scripts/{script_id}/run",
        json={"target_type": "device", "target_id": str(device_id)},
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["job_ids"]) == 1


async def test_get_script_returns_body_for_authorized_user(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    create_resp = await api_env.client.post(
        "/api/v1/deploy/scripts",
        json={
            "name": "cleanup",
            "os_family": "windows",
            "interpreter": "powershell",
            "body": "Write-Host hi",
        },
        headers=await api_env.headers(org_id, admin),
    )
    script_id = create_resp.json()["id"]

    resp = await api_env.client.get(
        f"/api/v1/deploy/scripts/{script_id}", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"] == "Write-Host hi"


async def test_get_script_not_found(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.get(
        f"/api/v1/deploy/scripts/{uuid4()}", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 404


async def test_get_script_forbidden_without_deploy_read(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    nobody = await api_env.create_user(org_id)
    create_resp = await api_env.client.post(
        "/api/v1/deploy/scripts",
        json={
            "name": "cleanup",
            "os_family": "windows",
            "interpreter": "powershell",
            "body": "Write-Host hi",
        },
        headers=await api_env.headers(org_id, admin),
    )
    script_id = create_resp.json()["id"]

    resp = await api_env.client.get(
        f"/api/v1/deploy/scripts/{script_id}", headers=await api_env.headers(org_id, nobody)
    )
    assert resp.status_code == 403


async def test_update_script_not_found(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.put(
        f"/api/v1/deploy/scripts/{uuid4()}",
        json={"body": "new body"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# GET /report, GET /devices/{device_id}/deployments
# --------------------------------------------------------------------------- #


async def test_report_ok_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("deploy")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/deploy/report", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "deployments" in body
    assert "script_runs" in body


async def test_device_deployment_history_requires_read_and_404_for_unknown_device(
    api_env,
) -> None:
    org_id = await api_env.create_org("deploy")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env, org_id)
    headers = await api_env.headers(org_id, tech)

    resp = await api_env.client.get(
        f"/api/v1/deploy/devices/{device_id}/deployments", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    missing = await api_env.client.get(
        f"/api/v1/deploy/devices/{uuid4()}/deployments", headers=headers
    )
    assert missing.status_code == 404


async def test_execute_deployment_without_an_uploaded_artifact_is_422(api_env) -> None:
    """FAIL CLOSED, AND SAY SO TO THE OPERATOR.

    An `msi` package with nothing uploaded has no bytes to presign and no hash
    to verify them against. The agent would refuse such a job on arrival, so
    dispatching it would spend a signed job (and a row per device) to discover
    what the control plane already knows. A 422 naming the missing upload is
    the answer an operator can act on.
    """
    org_id = await api_env.create_org("deploy")
    tech = await api_env.create_user(org_id, "technician")
    package_id = await _package(api_env, org_id)  # msi, no artifact uploaded
    device_id = await _device(api_env, org_id, hostname="no-artifact")
    headers = await api_env.headers(org_id, tech)

    created = await api_env.client.post(
        "/api/v1/deploy/deployments",
        json={
            "package_id": str(package_id),
            "action": "install",
            "target_type": "device",
            "target_id": str(device_id),
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text

    resp = await api_env.client.post(
        f"/api/v1/deploy/deployments/{created.json()['id']}/execute", headers=headers
    )
    assert resp.status_code == 422, resp.text
    assert "artifact" in resp.text

    # And no job, and no deployment_results row, was created for it.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        from app.models import DeploymentResult, Job

        assert (
            await session.execute(select(Job).where(Job.target_id == device_id))
        ).scalars().all() == []
        assert (
            await session.execute(
                select(DeploymentResult).where(DeploymentResult.device_id == device_id)
            )
        ).scalars().all() == []


async def test_a_deployment_to_a_device_waiting_for_review_is_refused_at_creation(
    api_env,
) -> None:
    from sqlalchemy import update

    org_id = await api_env.create_org("deploy")
    tech = await api_env.create_user(org_id, "technician")
    package_id = await _package(api_env, org_id)
    device_id = await _device(api_env, org_id)
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(update(Device).where(Device.id == device_id).values(review_state="pending"))
        await s.commit()

    resp = await api_env.client.post(
        "/api/v1/deploy/deployments",
        json={
            "package_id": str(package_id),
            "action": "install",
            "target_type": "device",
            "target_id": str(device_id),
        },
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json() == {
        "detail": "waiting for review",
        "device_id": str(device_id),
        "job_type": "app.deploy",
    }
