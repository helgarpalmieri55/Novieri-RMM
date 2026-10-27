"""BCP/DR REST API tests (docs/modules/bcp/spec.md §9, §12).

Exercises server-side RBAC on every route (invariant #6), `BcpValidationError`
routing to 4xx never 500 (a cross-org `change_ref` -> 400, a missing plan/
test/finding -> 404), and the `bcp.report_exported` audit split (invariant
#4): the explicit export routes (`GET /tests/{id}/report`, `GET
/report/period`) write an audit event; plain list/detail reads do not. Uses
the `api_env` in-process ASGI harness (mirrors `tests/test_change_mgmt_api.py`).
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent
from tests.client_scope_support import internal_client
from tests.conftest import ApiEnv

_PLAN_BODY = {
    "name": "Core services DR plan",
    "description": "Restore core services after a site outage.",
    "scope": [],
    "rto_minutes": 240,
    "rpo_minutes": 60,
    "procedure_refs": [],
    "dependencies": [],
    "review_interval_days": 365,
}


async def _create_plan(api_env: ApiEnv, headers: dict[str, str]) -> dict[str, Any]:
    # The internal client of whoever `headers` sign in as, which uses
    # continuity (MSP clients plan 2026-10-26 Task 7).
    body = {**_PLAN_BODY, "client_id": await internal_client(api_env, headers)}
    resp = await api_env.client.post("/api/v1/bcp/plans", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _create_test(api_env: ApiEnv, plan_id: str, headers: dict[str, str]) -> dict[str, Any]:
    resp = await api_env.client.post(
        f"/api/v1/bcp/plans/{plan_id}/tests",
        json={"type": "tabletop", "notes": "quarterly tabletop"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _create_finding(api_env: ApiEnv, test_id: str, headers: dict[str, str]) -> dict[str, Any]:
    resp = await api_env.client.post(
        f"/api/v1/bcp/tests/{test_id}/findings",
        json={"description": "Runbook step 4 was stale", "severity": "medium"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _audit_count(api_env: ApiEnv, org_id: UUID, action: str) -> int:
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (
            await session.execute(select(AuditEvent).where(AuditEvent.action == action))
        ).scalars().all()
        return len(rows)


# --------------------------------------------------------------------------- #
# GET|POST /plans -- RBAC
# --------------------------------------------------------------------------- #


async def test_create_plan_ok_for_technician_forbidden_for_read_only(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")

    created = await _create_plan(api_env, await api_env.headers(org_id, tech))
    assert created["name"] == _PLAN_BODY["name"]
    assert created["state"] == "active"

    forbidden = await api_env.client.post(
        "/api/v1/bcp/plans",
        json={**_PLAN_BODY, "client_id": str(org_id)},
        headers=await api_env.headers(org_id, ro),
    )
    assert forbidden.status_code == 403


async def test_list_plans_ok_for_read_only(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    created = await _create_plan(api_env, await api_env.headers(org_id, tech))

    resp = await api_env.client.get("/api/v1/bcp/plans", headers=await api_env.headers(org_id, ro))
    assert resp.status_code == 200, resp.text
    assert any(p["id"] == created["id"] for p in resp.json())


async def test_get_plan_unknown_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        f"/api/v1/bcp/plans/{uuid4()}", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 404


async def test_update_plan_invalid_scope_kind_is_400_not_500(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    created = await _create_plan(api_env, headers)

    resp = await api_env.client.patch(
        f"/api/v1/bcp/plans/{created['id']}",
        json={"scope": [{"kind": "bogus", "id": str(uuid4())}]},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


async def test_update_plan_unknown_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    resp = await api_env.client.patch(
        f"/api/v1/bcp/plans/{uuid4()}",
        json={"name": "does not exist"},
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 404, resp.text


async def test_update_plan_can_archive_and_rejects_invalid_state(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    created = await _create_plan(api_env, headers)
    assert created["state"] == "active"

    archived = await api_env.client.patch(
        f"/api/v1/bcp/plans/{created['id']}",
        json={"state": "archived"},
        headers=headers,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["state"] == "archived"

    # An out-of-domain state is rejected by the request schema (422) before it
    # ever reaches the service's allow-list.
    bad = await api_env.client.patch(
        f"/api/v1/bcp/plans/{created['id']}",
        json={"state": "deleted"},
        headers=headers,
    )
    assert bad.status_code == 422, bad.text


# --------------------------------------------------------------------------- #
# POST /plans/{id}/review -- bcp.approve, not bcp.manage
# --------------------------------------------------------------------------- #


async def test_review_plan_forbidden_for_technician_ok_for_org_admin(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    admin_headers = await api_env.headers(org_id, admin)
    tech_headers = await api_env.headers(org_id, tech)

    created = await _create_plan(api_env, admin_headers)

    forbidden = await api_env.client.post(
        f"/api/v1/bcp/plans/{created['id']}/review", json={}, headers=tech_headers
    )
    assert forbidden.status_code == 403

    ok = await api_env.client.post(
        f"/api/v1/bcp/plans/{created['id']}/review", json={}, headers=admin_headers
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["last_reviewed_at"] is not None
    assert ok.json()["approved_by"] == str(await _user_id(api_env, org_id, admin))


async def test_review_plan_unknown_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.post(
        f"/api/v1/bcp/plans/{uuid4()}/review", json={}, headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# GET /plans/status
# --------------------------------------------------------------------------- #


async def test_plan_status_ok_for_read(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    created = await _create_plan(api_env, await api_env.headers(org_id, tech))

    resp = await api_env.client.get(
        "/api/v1/bcp/plans/status", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    row = next(r for r in body if r["id"] == created["id"])
    assert row["review_overdue"] is True
    assert row["last_test_outcome"] is None


# --------------------------------------------------------------------------- #
# GET|POST /plans/{id}/tests, GET|PATCH /tests/{id}, PUT /tests/{id}/steps
# --------------------------------------------------------------------------- #


async def test_create_and_list_plan_tests(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    plan = await _create_plan(api_env, headers)

    test = await _create_test(api_env, plan["id"], headers)
    assert test["outcome"] == "pending"
    assert test["plan_id"] == plan["id"]

    listing = await api_env.client.get(f"/api/v1/bcp/plans/{plan['id']}/tests", headers=headers)
    assert listing.status_code == 200, listing.text
    assert any(t["id"] == test["id"] for t in listing.json())


async def test_create_test_unknown_plan_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    resp = await api_env.client.post(
        f"/api/v1/bcp/plans/{uuid4()}/tests",
        json={"type": "tabletop"},
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 404


async def test_get_test_unknown_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        f"/api/v1/bcp/tests/{uuid4()}", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 404


async def test_record_test_captures_and_returns_evidence_snapshot(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    plan = await _create_plan(api_env, headers)
    test = await _create_test(api_env, plan["id"], headers)

    resp = await api_env.client.patch(
        f"/api/v1/bcp/tests/{test['id']}",
        json={
            "outcome": "pass",
            "rto_met": True,
            "rpo_met": True,
            "notes": "all good",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "pass"
    assert body["executed_at"] is not None
    assert "evidence" in body
    assert "summary" in body["evidence"]
    assert body["evidence"]["summary"] == {
        "total_n": 0,
        "covered_n": 0,
        "restore_proven_n": 0,
    }


async def test_record_test_unknown_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    resp = await api_env.client.patch(
        f"/api/v1/bcp/tests/{uuid4()}",
        json={"outcome": "pass"},
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 404


async def test_upsert_step_ok_and_unknown_test_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    plan = await _create_plan(api_env, headers)
    test = await _create_test(api_env, plan["id"], headers)

    resp = await api_env.client.put(
        f"/api/v1/bcp/tests/{test['id']}/steps",
        json={"ordinal": 1, "description": "Fail over primary site", "result": "pass"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ordinal"] == 1

    missing = await api_env.client.put(
        f"/api/v1/bcp/tests/{uuid4()}/steps",
        json={"ordinal": 1, "description": "n/a"},
        headers=headers,
    )
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# GET|POST /tests/{id}/findings, PATCH /findings/{id}
# --------------------------------------------------------------------------- #


async def test_create_and_list_findings(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    plan = await _create_plan(api_env, headers)
    test = await _create_test(api_env, plan["id"], headers)

    finding = await _create_finding(api_env, test["id"], headers)
    assert finding["remediation_state"] == "open"

    listing = await api_env.client.get(
        f"/api/v1/bcp/tests/{test['id']}/findings", headers=headers
    )
    assert listing.status_code == 200, listing.text
    assert any(f["id"] == finding["id"] for f in listing.json())


async def test_list_findings_unknown_test_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        f"/api/v1/bcp/tests/{uuid4()}/findings", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 404


async def test_update_finding_cross_org_change_ref_is_400_not_500(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    other_org_id = await api_env.create_org("bcp-other")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    plan = await _create_plan(api_env, headers)
    test = await _create_test(api_env, plan["id"], headers)
    finding = await _create_finding(api_env, test["id"], headers)

    other_tech = await api_env.create_user(other_org_id, "technician")
    other_headers = await api_env.headers(other_org_id, other_tech)
    other_change = await api_env.client.post(
        "/api/v1/changes",
        json={
            "type": "standard",
            "title": "Other org change",
            "description": "n/a",
            "risk": "low",
            "affected": [],
            "client_id": str(other_org_id),
        },
        headers=other_headers,
    )
    assert other_change.status_code == 201, other_change.text

    resp = await api_env.client.patch(
        f"/api/v1/bcp/findings/{finding['id']}",
        json={"change_ref": other_change.json()["id"]},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


async def test_update_finding_remediation_state_and_unknown_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    plan = await _create_plan(api_env, headers)
    test = await _create_test(api_env, plan["id"], headers)
    finding = await _create_finding(api_env, test["id"], headers)

    resp = await api_env.client.patch(
        f"/api/v1/bcp/findings/{finding['id']}",
        json={"remediation_state": "resolved"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["remediation_state"] == "resolved"
    assert resp.json()["resolved_at"] is not None

    missing = await api_env.client.patch(
        f"/api/v1/bcp/findings/{uuid4()}",
        json={"remediation_state": "resolved"},
        headers=headers,
    )
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# GET /tests/{id}/report, GET /report/period -- audited exports
# --------------------------------------------------------------------------- #


async def test_test_report_ok_and_writes_audit(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, tech)
    plan = await _create_plan(api_env, headers)
    test = await _create_test(api_env, plan["id"], headers)

    before = await _audit_count(api_env, org_id, "bcp.report_exported")

    resp = await api_env.client.get(
        f"/api/v1/bcp/tests/{test['id']}/report", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["test"]["id"] == test["id"]
    assert body["plan"]["id"] == plan["id"]

    after = await _audit_count(api_env, org_id, "bcp.report_exported")
    assert after == before + 1

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "bcp.report_exported")
            )
        ).scalars().first()
    assert event is not None
    assert event.target_type == "bcp_test"
    assert event.target_id == test["id"]


async def test_test_report_unknown_is_404(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        f"/api/v1/bcp/tests/{uuid4()}/report", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 404


async def test_period_report_ok_and_writes_audit(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    ro = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, ro)

    before = await _audit_count(api_env, org_id, "bcp.report_exported")

    resp = await api_env.client.get(
        "/api/v1/bcp/report/period",
        params={"from": "2026-01-01T00:00:00Z", "to": "2026-12-31T00:00:00Z"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "outcome_counts" in body
    assert "open_findings_by_severity" in body

    after = await _audit_count(api_env, org_id, "bcp.report_exported")
    assert after == before + 1


# --------------------------------------------------------------------------- #
# No-audit-on-view: GET /plans (and other plain reads) must never write
# `bcp.report_exported` -- only the explicit export routes do.
# --------------------------------------------------------------------------- #


async def test_list_plans_writes_no_audit(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("bcp")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    await _create_plan(api_env, headers)

    before = await _audit_count(api_env, org_id, "bcp.report_exported")

    resp = await api_env.client.get("/api/v1/bcp/plans", headers=headers)
    assert resp.status_code == 200, resp.text

    after = await _audit_count(api_env, org_id, "bcp.report_exported")
    assert after == before, "GET /plans must never write bcp.report_exported"


async def _user_id(api_env: ApiEnv, org_id: UUID, sub: str) -> UUID:
    from app.models import User

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
