"""Provisioning REST API tests (docs/modules/provisioning/spec.md §12).

Exercises server-side RBAC on every route (invariant #6) and that no response
body anywhere ever carries a plaintext credential, a `{"__sealed__": ...}`
vault ref, an enrollment token secret, or rendered answer-file bytes
(invariant #3). Uses the `api_env` in-process ASGI harness (mirrors
`tests/test_backup_api.py`).
"""

from uuid import UUID, uuid4

from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import Device, ProvisionRun, User

_DOMAIN_PASSWORD = "SuperSecretDomainPW!23"  # noqa: S105 - test fixture, not a real credential
_ADMIN_PASSWORD = "SuperSecretAdminPW!45"  # noqa: S105 - test fixture, not a real credential
_SEALED_MARKER = "__sealed__"

_POST_CONFIG = {
    "hostname": "wks-01",
    "domain_join": {
        "user": "svc-join",
        "password": _DOMAIN_PASSWORD,
        "ou": "OU=Workstations,DC=corp",
    },
    "local_admin": {"password": _ADMIN_PASSWORD},
}


def _assert_no_secrets(resp) -> None:
    assert _DOMAIN_PASSWORD not in resp.text
    assert _ADMIN_PASSWORD not in resp.text
    assert _SEALED_MARKER not in resp.text


async def _device(api_env, org_id: UUID, hostname: str = "wks-provisioning") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        dev = Device(org_id=org_id, hostname=hostname, os_family="windows")
        s.add(dev)
        await s.commit()
        return dev.id


async def _user_id(api_env, org_id: UUID, sub: str) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        return (
            await s.execute(
                select(User.id).where(User.org_id == org_id, User.external_subject == sub)
            )
        ).scalar_one()


async def _create_image(api_env, org_id: UUID, admin_headers: dict[str, str]) -> str:
    resp = await api_env.client.post(
        "/api/v1/provisioning/images",
        json={"os_family": "windows", "name": "win11-golden", "kind": "image", "params": {}},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --------------------------------------------------------------------------- #
# GET|POST /images, GET /images/{id}
# --------------------------------------------------------------------------- #


async def test_list_images_allowed_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("prov")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/provisioning/images", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    _assert_no_secrets(resp)


async def test_create_image_forbidden_for_technician_ok_for_org_admin(api_env) -> None:
    org_id = await api_env.create_org("prov")
    tech = await api_env.create_user(org_id, "technician")
    admin = await api_env.create_user(org_id, "org-admin")
    body = {"os_family": "windows", "name": "win11-golden", "kind": "image", "params": {}}

    forbidden = await api_env.client.post(
        "/api/v1/provisioning/images", json=body, headers=await api_env.headers(org_id, tech)
    )
    assert forbidden.status_code == 403

    ok = await api_env.client.post(
        "/api/v1/provisioning/images", json=body, headers=await api_env.headers(org_id, admin)
    )
    assert ok.status_code == 201, ok.text
    out = ok.json()
    assert out["name"] == "win11-golden"
    assert out["state"] == "active"
    _assert_no_secrets(ok)


async def test_get_image_ok_for_read_only_and_404_for_unknown(api_env) -> None:
    org_id = await api_env.create_org("prov")
    admin = await api_env.create_user(org_id, "org-admin")
    ro = await api_env.create_user(org_id, "read-only")
    admin_headers = await api_env.headers(org_id, admin)
    image_id = await _create_image(api_env, org_id, admin_headers)

    resp = await api_env.client.get(
        f"/api/v1/provisioning/images/{image_id}", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == image_id

    missing = await api_env.client.get(
        f"/api/v1/provisioning/images/{uuid4()}", headers=await api_env.headers(org_id, ro)
    )
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# GET|POST /profiles, PUT|GET /profiles/{id} — secret redaction
# --------------------------------------------------------------------------- #


async def test_create_profile_forbidden_for_technician_ok_for_org_admin_redacts_secrets(
    api_env,
) -> None:
    org_id = await api_env.create_org("prov")
    tech = await api_env.create_user(org_id, "technician")
    admin = await api_env.create_user(org_id, "org-admin")
    admin_headers = await api_env.headers(org_id, admin)
    image_id = await _create_image(api_env, org_id, admin_headers)

    body = {
        "name": "standard-workstation",
        "image_id": image_id,
        "post_config": _POST_CONFIG,
        "target_scope": {},
        "enabled": True,
    }

    forbidden = await api_env.client.post(
        "/api/v1/provisioning/profiles", json=body, headers=await api_env.headers(org_id, tech)
    )
    assert forbidden.status_code == 403

    ok = await api_env.client.post(
        "/api/v1/provisioning/profiles", json=body, headers=admin_headers
    )
    assert ok.status_code == 201, ok.text
    out = ok.json()
    assert out["name"] == "standard-workstation"
    assert out["has_domain_join_credential"] is True
    assert out["has_local_admin_credential"] is True
    assert "password" not in out["post_config"]["domain_join"]
    assert out["post_config"]["domain_join"]["user"] == "svc-join"
    assert "password" not in out["post_config"]["local_admin"]
    _assert_no_secrets(ok)


async def test_put_profile_ok_for_org_admin_stays_redacted(api_env) -> None:
    org_id = await api_env.create_org("prov")
    admin = await api_env.create_user(org_id, "org-admin")
    admin_headers = await api_env.headers(org_id, admin)
    image_id = await _create_image(api_env, org_id, admin_headers)

    create_resp = await api_env.client.post(
        "/api/v1/provisioning/profiles",
        json={
            "name": "standard-workstation",
            "image_id": image_id,
            "post_config": _POST_CONFIG,
            "target_scope": {},
            "enabled": True,
        },
        headers=admin_headers,
    )
    profile_id = create_resp.json()["id"]

    put_resp = await api_env.client.put(
        f"/api/v1/provisioning/profiles/{profile_id}",
        json={
            "name": "standard-workstation-v2",
            "image_id": image_id,
            "post_config": _POST_CONFIG,
            "target_scope": {"group": "engineering"},
            "enabled": False,
        },
        headers=admin_headers,
    )
    assert put_resp.status_code == 200, put_resp.text
    out = put_resp.json()
    assert out["name"] == "standard-workstation-v2"
    assert out["enabled"] is False
    assert out["has_domain_join_credential"] is True
    _assert_no_secrets(put_resp)

    unknown = await api_env.client.put(
        f"/api/v1/provisioning/profiles/{uuid4()}",
        json={"name": "x", "image_id": image_id, "post_config": {}, "target_scope": {}},
        headers=admin_headers,
    )
    assert unknown.status_code == 404


async def test_get_profile_ok_for_read_only_stays_redacted(api_env) -> None:
    org_id = await api_env.create_org("prov")
    admin = await api_env.create_user(org_id, "org-admin")
    ro = await api_env.create_user(org_id, "read-only")
    admin_headers = await api_env.headers(org_id, admin)
    image_id = await _create_image(api_env, org_id, admin_headers)

    create_resp = await api_env.client.post(
        "/api/v1/provisioning/profiles",
        json={
            "name": "standard-workstation",
            "image_id": image_id,
            "post_config": _POST_CONFIG,
            "target_scope": {},
            "enabled": True,
        },
        headers=admin_headers,
    )
    profile_id = create_resp.json()["id"]

    resp = await api_env.client.get(
        f"/api/v1/provisioning/profiles/{profile_id}", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    _assert_no_secrets(resp)

    list_resp = await api_env.client.get(
        "/api/v1/provisioning/profiles", headers=await api_env.headers(org_id, ro)
    )
    assert list_resp.status_code == 200, list_resp.text
    assert len(list_resp.json()) == 1
    _assert_no_secrets(list_resp)


# --------------------------------------------------------------------------- #
# POST /profiles/{id}/run
# --------------------------------------------------------------------------- #


async def test_start_run_forbidden_for_read_only_ok_for_technician_no_secrets(api_env) -> None:
    org_id = await api_env.create_org("prov")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    admin_headers = await api_env.headers(org_id, admin)
    image_id = await _create_image(api_env, org_id, admin_headers)

    profile_resp = await api_env.client.post(
        "/api/v1/provisioning/profiles",
        json={
            "name": "standard-workstation",
            "image_id": image_id,
            "post_config": _POST_CONFIG,
            "target_scope": {},
            "enabled": True,
        },
        headers=admin_headers,
    )
    profile_id = profile_resp.json()["id"]

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)

        forbidden = await api_env.client.post(
            f"/api/v1/provisioning/profiles/{profile_id}/run",
            json={},
            headers=await api_env.headers(org_id, ro),
        )
        assert forbidden.status_code == 403

        ok = await api_env.client.post(
            f"/api/v1/provisioning/profiles/{profile_id}/run",
            json={},
            headers=await api_env.headers(org_id, tech),
        )
    assert ok.status_code == 200, ok.text
    out = ok.json()
    assert set(out.keys()) == {"run_id", "state"}
    assert out["state"] == "awaiting_enrollment"
    _assert_no_secrets(ok)

    expected_user_id = await _user_id(api_env, org_id, tech)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        run = (
            await session.execute(
                select(ProvisionRun).where(ProvisionRun.id == UUID(out["run_id"]))
            )
        ).scalar_one()
    assert run.requested_by == expected_user_id
    assert run.actor_type == "user"


# --------------------------------------------------------------------------- #
# GET /runs, GET /runs/{id}
# --------------------------------------------------------------------------- #


async def test_list_runs_and_get_run_status_ok_for_read(api_env) -> None:
    org_id = await api_env.create_org("prov")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    admin_headers = await api_env.headers(org_id, admin)
    image_id = await _create_image(api_env, org_id, admin_headers)

    profile_resp = await api_env.client.post(
        "/api/v1/provisioning/profiles",
        json={
            "name": "standard-workstation",
            "image_id": image_id,
            "post_config": _POST_CONFIG,
            "target_scope": {},
            "enabled": True,
        },
        headers=admin_headers,
    )
    profile_id = profile_resp.json()["id"]

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        run_resp = await api_env.client.post(
            f"/api/v1/provisioning/profiles/{profile_id}/run",
            json={},
            headers=await api_env.headers(org_id, tech),
        )
    run_id = run_resp.json()["run_id"]

    list_resp = await api_env.client.get(
        "/api/v1/provisioning/runs", headers=await api_env.headers(org_id, ro)
    )
    assert list_resp.status_code == 200, list_resp.text
    runs = list_resp.json()
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    _assert_no_secrets(list_resp)

    get_resp = await api_env.client.get(
        f"/api/v1/provisioning/runs/{run_id}", headers=await api_env.headers(org_id, ro)
    )
    assert get_resp.status_code == 200, get_resp.text
    body = get_resp.json()
    assert body["id"] == run_id
    assert body["profile_id"] == profile_id
    assert body["state"] == "awaiting_enrollment"
    _assert_no_secrets(get_resp)


async def test_get_run_unknown_is_404(api_env) -> None:
    org_id = await api_env.create_org("prov")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.get(
        f"/api/v1/provisioning/runs/{uuid4()}", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# GET /devices/{id}/provisioning, GET /report
# --------------------------------------------------------------------------- #


async def test_device_provisioning_ok_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("prov")
    ro = await api_env.create_user(org_id, "read-only")
    device_id = await _device(api_env, org_id)
    resp = await api_env.client.get(
        f"/api/v1/provisioning/devices/{device_id}/provisioning",
        headers=await api_env.headers(org_id, ro),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device_id"] == str(device_id)
    assert body["profile_id"] is None
    _assert_no_secrets(resp)


async def test_report_ok_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("prov")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/provisioning/report", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success_rate"] <= 1.0
    assert "run_counts_by_state" in body
    _assert_no_secrets(resp)


async def test_report_forbidden_without_provisioning_read(api_env) -> None:
    org_id = await api_env.create_org("prov")
    nobody = await api_env.create_user(org_id)
    resp = await api_env.client.get(
        "/api/v1/provisioning/report", headers=await api_env.headers(org_id, nobody)
    )
    assert resp.status_code == 403
