"""Licenses/SAM REST API tests (docs/modules/licenses/spec.md §10, §12).

Exercises server-side RBAC on every route (invariant #6), `LicenseValidationError`
routing to 4xx never 500 (a cross-org `product_id` -> 400, a missing vendor/
product/license/allocation -> 404), the vault-sealed key reveal split (invariant
#3: list/detail never expose the key, only the dedicated `licenses.keys.read`
reveal endpoint does, and it audits every call), the audited-export vs
non-audited-read split (invariant #4: `GET /licenses/export` writes
`license.export`; plain list/detail/compliance/review-queue/renewal reads write
nothing), and the pool-enforcement 400 on allocation. Uses the `api_env`
in-process ASGI harness (mirrors `tests/test_bcp_api.py`).
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Device
from tests.conftest import ApiEnv


async def _create_vendor(api_env: ApiEnv, headers: dict[str, str]) -> dict[str, Any]:
    resp = await api_env.client.post(
        "/api/v1/vendors", json={"name": "Acme Corp"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _create_product(
    api_env: ApiEnv, headers: dict[str, str], name: str = "Widget Editor"
) -> dict[str, Any]:
    resp = await api_env.client.post(
        "/api/v1/licenses/products",
        json={"name": name, "publisher": "Acme", "match_rules": []},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _create_license(
    api_env: ApiEnv,
    headers: dict[str, str],
    product_id: str,
    *,
    entitlements: int = 5,
    key: str | None = None,
) -> dict[str, Any]:
    resp = await api_env.client.post(
        "/api/v1/licenses",
        json={
            "product_id": product_id,
            "type": "perpetual",
            "entitlements": entitlements,
            "key": key,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _add_device(api_env: ApiEnv, org_id: UUID, hostname: str = "lic-dev-1") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id, hostname=hostname, os_family="windows", client_id=org_id, site_id=org_id
        )
        session.add(device)
        await session.commit()
        return device.id


async def _audit_count(api_env: ApiEnv, org_id: UUID, action: str) -> int:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(select(AuditEvent).where(AuditEvent.action == action))
        ).scalars().all()
        return len(rows)


# --------------------------------------------------------------------------- #
# POST /vendors, /licenses/products, /licenses -- RBAC
# --------------------------------------------------------------------------- #


async def test_create_vendor_product_license_ok_for_technician_forbidden_for_read_only(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("lic")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    tech_headers = await api_env.headers(org_id, tech)
    ro_headers = await api_env.headers(org_id, ro)

    vendor = await _create_vendor(api_env, tech_headers)
    assert vendor["name"] == "Acme Corp"

    product = await _create_product(api_env, tech_headers)
    assert product["name"] == "Widget Editor"

    license_ = await _create_license(api_env, tech_headers, product["id"])
    assert license_["entitlements"] == 5
    assert license_["has_key"] is False

    forbidden_vendor = await api_env.client.post(
        "/api/v1/vendors", json={"name": "Other Corp"}, headers=ro_headers
    )
    assert forbidden_vendor.status_code == 403

    forbidden_product = await api_env.client.post(
        "/api/v1/licenses/products",
        json={"name": "Other Product", "publisher": "", "match_rules": []},
        headers=ro_headers,
    )
    assert forbidden_product.status_code == 403

    forbidden_license = await api_env.client.post(
        "/api/v1/licenses",
        json={"product_id": product["id"], "type": "perpetual", "entitlements": 1},
        headers=ro_headers,
    )
    assert forbidden_license.status_code == 403


# --------------------------------------------------------------------------- #
# GET /licenses/{id}/key -- reveal, licenses.keys.read, audited
# --------------------------------------------------------------------------- #


async def test_reveal_key_forbidden_for_technician_ok_for_org_admin_and_audits(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("lic")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    admin_headers = await api_env.headers(org_id, admin)
    tech_headers = await api_env.headers(org_id, tech)

    product = await _create_product(api_env, admin_headers)
    license_ = await _create_license(
        api_env, admin_headers, product["id"], key="s3cr3t-key-value"
    )
    assert license_["has_key"] is True

    forbidden = await api_env.client.get(
        f"/api/v1/licenses/{license_['id']}/key", headers=tech_headers
    )
    assert forbidden.status_code == 403

    before = await _audit_count(api_env, org_id, "license.key_revealed")

    ok = await api_env.client.get(
        f"/api/v1/licenses/{license_['id']}/key", headers=admin_headers
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["key"] == "s3cr3t-key-value"

    after = await _audit_count(api_env, org_id, "license.key_revealed")
    assert after == before + 1


async def test_reveal_key_unknown_license_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("lic")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.get(
        f"/api/v1/licenses/{uuid4()}/key", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# GET /licenses/{id} -- detail never exposes key/key_sealed, only has_key.
# Detail read writes no license.key_revealed audit.
# --------------------------------------------------------------------------- #


async def test_get_license_detail_has_no_key_field_and_writes_no_key_audit(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("lic")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    product = await _create_product(api_env, headers)
    license_ = await _create_license(api_env, headers, product["id"], key="hidden-key")

    before = await _audit_count(api_env, org_id, "license.key_revealed")

    resp = await api_env.client.get(f"/api/v1/licenses/{license_['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_key"] is True
    assert "key" not in body
    assert "key_sealed" not in body

    after = await _audit_count(api_env, org_id, "license.key_revealed")
    assert after == before


# --------------------------------------------------------------------------- #
# Allocation pool enforcement
# --------------------------------------------------------------------------- #


async def test_allocate_past_pool_is_400(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("lic")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    product = await _create_product(api_env, headers)
    license_ = await _create_license(api_env, headers, product["id"], entitlements=1)
    device_id = await _add_device(api_env, org_id)

    first = await api_env.client.post(
        f"/api/v1/licenses/{license_['id']}/allocations",
        json={"target_type": "device", "target_id": str(device_id)},
        headers=headers,
    )
    assert first.status_code == 201, first.text

    second_device = await _add_device(api_env, org_id, "lic-dev-2")
    second = await api_env.client.post(
        f"/api/v1/licenses/{license_['id']}/allocations",
        json={"target_type": "device", "target_id": str(second_device)},
        headers=headers,
    )
    assert second.status_code == 400, second.text

    listing = await api_env.client.get(
        f"/api/v1/licenses/{license_['id']}/allocations", headers=headers
    )
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1

    release = await api_env.client.post(
        f"/api/v1/allocations/{first.json()['id']}/release", json={}, headers=headers
    )
    assert release.status_code == 200, release.text
    assert release.json()["released_at"] is not None

    release_again = await api_env.client.post(
        f"/api/v1/allocations/{first.json()['id']}/release", json={}, headers=headers
    )
    assert release_again.status_code == 400, release_again.text

    release_unknown = await api_env.client.post(
        f"/api/v1/allocations/{uuid4()}/release", json={}, headers=headers
    )
    assert release_unknown.status_code == 404


# --------------------------------------------------------------------------- #
# Cross-org product_id on POST /licenses -- 400 not 500 (invariant #5)
# --------------------------------------------------------------------------- #


async def test_create_license_cross_org_product_id_is_400_not_500(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("lic")
    other_org_id = await api_env.create_org("lic-other")
    tech = await api_env.create_user(org_id, "technician")
    other_tech = await api_env.create_user(other_org_id, "technician")

    other_product = await _create_product(
        api_env, await api_env.headers(other_org_id, other_tech)
    )

    resp = await api_env.client.post(
        "/api/v1/licenses",
        json={
            "product_id": other_product["id"],
            "type": "perpetual",
            "entitlements": 1,
        },
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 400, resp.text


# --------------------------------------------------------------------------- #
# Missing ids -- 404 not 500
# --------------------------------------------------------------------------- #


async def test_missing_vendor_product_license_are_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("lic")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    assert (
        await api_env.client.get(f"/api/v1/vendors/{uuid4()}", headers=headers)
    ).status_code == 404
    assert (
        await api_env.client.patch(
            f"/api/v1/vendors/{uuid4()}", json={"name": "x"}, headers=headers
        )
    ).status_code == 404

    assert (
        await api_env.client.get(f"/api/v1/licenses/products/{uuid4()}", headers=headers)
    ).status_code == 404
    assert (
        await api_env.client.patch(
            f"/api/v1/licenses/products/{uuid4()}", json={"name": "x"}, headers=headers
        )
    ).status_code == 404

    assert (
        await api_env.client.get(f"/api/v1/licenses/{uuid4()}", headers=headers)
    ).status_code == 404
    assert (
        await api_env.client.patch(
            f"/api/v1/licenses/{uuid4()}", json={"notes": "x"}, headers=headers
        )
    ).status_code == 404
    assert (
        await api_env.client.delete(f"/api/v1/licenses/{uuid4()}", headers=headers)
    ).status_code == 404


# --------------------------------------------------------------------------- #
# GET /licenses/compliance, /review-queue, /renewals -- ok for read, no 500
# --------------------------------------------------------------------------- #


async def test_compliance_review_queue_renewals_ok_for_read(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("lic")
    ro = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, ro)

    compliance = await api_env.client.get("/api/v1/licenses/compliance", headers=headers)
    assert compliance.status_code == 200, compliance.text
    assert compliance.json() == []

    review_queue = await api_env.client.get("/api/v1/licenses/review-queue", headers=headers)
    assert review_queue.status_code == 200, review_queue.text
    assert review_queue.json() == []

    renewals = await api_env.client.get("/api/v1/licenses/renewals", headers=headers)
    assert renewals.status_code == 200, renewals.text
    assert renewals.json() == []

    costs = await api_env.client.get("/api/v1/licenses/costs", headers=headers)
    assert costs.status_code == 200, costs.text
    assert costs.json() == []

    forbidden = await api_env.client.post(
        "/api/v1/licenses/review-queue/map",
        json={"name": "SomeApp", "product_id": str(uuid4())},
        headers=headers,
    )
    assert forbidden.status_code == 403


async def test_review_queue_map_and_ignore_ok_for_manage(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("lic")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    product = await _create_product(api_env, headers)

    mapped = await api_env.client.post(
        "/api/v1/licenses/review-queue/map",
        json={"name": "SomeApp", "product_id": product["id"]},
        headers=headers,
    )
    assert mapped.status_code == 200, mapped.text
    assert {"type": "exact", "pattern": "SomeApp", "publisher": None} in mapped.json()[
        "match_rules"
    ]

    ignored = await api_env.client.post(
        "/api/v1/licenses/review-queue/ignore",
        json={"name": "OtherApp"},
        headers=headers,
    )
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["name"] == "OtherApp"

    unknown_product = await api_env.client.post(
        "/api/v1/licenses/review-queue/map",
        json={"name": "SomeApp", "product_id": str(uuid4())},
        headers=headers,
    )
    assert unknown_product.status_code == 404, unknown_product.text


# --------------------------------------------------------------------------- #
# GET /licenses/export -- audited; GET /licenses (list) -- not audited.
# --------------------------------------------------------------------------- #


async def test_export_writes_audit_list_writes_none(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("lic")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    product = await _create_product(api_env, headers)
    await _create_license(api_env, headers, product["id"])

    before_export = await _audit_count(api_env, org_id, "license.export")

    listing = await api_env.client.get("/api/v1/licenses", headers=headers)
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1

    after_list = await _audit_count(api_env, org_id, "license.export")
    assert after_list == before_export, "GET /licenses must never write license.export"

    export = await api_env.client.get("/api/v1/licenses/export", headers=headers)
    assert export.status_code == 200, export.text
    assert len(export.json()) == 1

    after_export = await _audit_count(api_env, org_id, "license.export")
    assert after_export == before_export + 1


# --------------------------------------------------------------------------- #
# Route ordering: /licenses/products must resolve to the product route, not
# be captured as a /licenses/{id} path parameter.
# --------------------------------------------------------------------------- #


async def test_licenses_products_resolves_to_product_route_not_license_id(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("lic")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/licenses/products", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
