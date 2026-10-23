"""Change management REST API tests (docs/modules/change_management/spec.md
§11-12).

Exercises server-side RBAC on every route (invariant #6), the SoD guard
surfacing as a 4xx never a 500 (invariant #6 + the state machine's own
`ChangeValidationError`), tenant scoping on links (invariant #5), and the
`change.evidence_exported` audit write (invariant #4). Uses the `api_env`
in-process ASGI harness (mirrors `tests/test_provisioning_api.py`).
"""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, Patch, PatchApproval, User

_CHANGE_BODY = {
    "type": "standard",
    "title": "Upgrade firewall firmware",
    "description": "Apply vendor firmware patch to core firewall.",
    "risk": "medium",
    "affected": [{"kind": "device", "id": "fw-01"}],
    "rollback_plan": "Reflash previous firmware image.",
}


async def _user_id(api_env, org_id: UUID, sub: str) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        return (
            await s.execute(
                select(User.id).where(User.org_id == org_id, User.external_subject == sub)
            )
        ).scalar_one()


async def _create_change(api_env, org_id: UUID, headers: dict[str, str]) -> dict:
    resp = await api_env.client.post(
        "/api/v1/changes", json=_CHANGE_BODY, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _submit(api_env, org_id: UUID, change_id: str, headers: dict[str, str]) -> dict:
    resp = await api_env.client.post(
        f"/api/v1/changes/{change_id}/submit", json={}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _patch_approval_target(api_env, org_id: UUID) -> UUID:
    """A minimal org-scoped `PatchApproval` row to advisory-link against."""
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        patch = Patch(
            org_id=org_id,
            os_family="windows",
            source="wsus",
            identifier="KB0000001",
            title="Test patch",
            severity="important",
        )
        s.add(patch)
        await s.flush()
        approval = PatchApproval(org_id=org_id, patch_id=patch.id, ring_name="ring0")
        s.add(approval)
        await s.commit()
        return approval.id


# --------------------------------------------------------------------------- #
# GET|POST /changes, GET /changes/{id}, PATCH /changes/{id}
# --------------------------------------------------------------------------- #


async def test_create_change_ok_for_technician(api_env) -> None:
    org_id = await api_env.create_org("chg")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    out = await _create_change(api_env, org_id, headers)
    assert out["title"] == _CHANGE_BODY["title"]
    assert out["state"] == "draft"


async def test_create_change_forbidden_without_request_permission(api_env) -> None:
    org_id = await api_env.create_org("chg")
    nobody = await api_env.create_user(org_id)
    resp = await api_env.client.post(
        "/api/v1/changes", json=_CHANGE_BODY, headers=await api_env.headers(org_id, nobody)
    )
    assert resp.status_code == 403


async def test_list_and_get_change_ok_for_read(api_env) -> None:
    org_id = await api_env.create_org("chg")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    tech_headers = await api_env.headers(org_id, tech)
    ro_headers = await api_env.headers(org_id, ro)

    created = await _create_change(api_env, org_id, tech_headers)

    list_resp = await api_env.client.get("/api/v1/changes", headers=ro_headers)
    assert list_resp.status_code == 200, list_resp.text
    assert any(c["id"] == created["id"] for c in list_resp.json())

    get_resp = await api_env.client.get(f"/api/v1/changes/{created['id']}", headers=ro_headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == created["id"]


async def test_get_change_unknown_is_404(api_env) -> None:
    org_id = await api_env.create_org("chg")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        f"/api/v1/changes/{uuid4()}", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 404


async def test_patch_change_unknown_is_404(api_env) -> None:
    org_id = await api_env.create_org("chg")
    tech = await api_env.create_user(org_id, "technician")
    resp = await api_env.client.patch(
        f"/api/v1/changes/{uuid4()}",
        json={"title": "does not exist"},
        headers=await api_env.headers(org_id, tech),
    )
    assert resp.status_code == 404, resp.text


async def test_submit_change_unknown_is_404(api_env) -> None:
    org_id = await api_env.create_org("chg")
    tech = await api_env.create_user(org_id, "technician")
    resp = await api_env.client.post(
        f"/api/v1/changes/{uuid4()}/submit", json={}, headers=await api_env.headers(org_id, tech)
    )
    assert resp.status_code == 404, resp.text


async def test_patch_draft_ok_non_draft_400(api_env) -> None:
    org_id = await api_env.create_org("chg")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    created = await _create_change(api_env, org_id, headers)

    patch_resp = await api_env.client.patch(
        f"/api/v1/changes/{created['id']}",
        json={"title": "Upgrade firewall firmware (v2)"},
        headers=headers,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["title"] == "Upgrade firewall firmware (v2)"

    await _submit(api_env, org_id, created["id"], headers)

    non_draft_patch = await api_env.client.patch(
        f"/api/v1/changes/{created['id']}",
        json={"title": "should not apply"},
        headers=headers,
    )
    assert non_draft_patch.status_code == 400, non_draft_patch.text


# --------------------------------------------------------------------------- #
# POST /changes/{id}/approve, /reject -- RBAC + SoD
# --------------------------------------------------------------------------- #


async def test_approve_forbidden_for_technician_ok_for_org_admin_when_not_self(
    api_env,
) -> None:
    org_id = await api_env.create_org("chg")
    creator = await api_env.create_user(org_id, "org-admin")
    approver = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    creator_headers = await api_env.headers(org_id, creator)

    created = await _create_change(api_env, org_id, creator_headers)
    await _submit(api_env, org_id, created["id"], creator_headers)

    forbidden = await api_env.client.post(
        f"/api/v1/changes/{created['id']}/approve",
        json={"comment": "lgtm"},
        headers=await api_env.headers(org_id, tech),
    )
    assert forbidden.status_code == 403

    ok = await api_env.client.post(
        f"/api/v1/changes/{created['id']}/approve",
        json={"comment": "lgtm"},
        headers=await api_env.headers(org_id, approver),
    )
    assert ok.status_code in (200, 201), ok.text
    body = ok.json()
    assert body["decision"] == "approved"
    expected_approver = await _user_id(api_env, org_id, approver)
    assert body["approver_id"] == str(expected_approver)


async def test_self_approval_is_400_not_500(api_env) -> None:
    org_id = await api_env.create_org("chg")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    created = await _create_change(api_env, org_id, headers)
    await _submit(api_env, org_id, created["id"], headers)

    self_approve = await api_env.client.post(
        f"/api/v1/changes/{created['id']}/approve",
        json={"comment": "self sign-off"},
        headers=headers,
    )
    assert self_approve.status_code == 400, self_approve.text


async def test_reject_ok_for_org_admin_when_not_self(api_env) -> None:
    org_id = await api_env.create_org("chg")
    creator = await api_env.create_user(org_id, "org-admin")
    approver = await api_env.create_user(org_id, "org-admin")
    creator_headers = await api_env.headers(org_id, creator)

    created = await _create_change(api_env, org_id, creator_headers)
    await _submit(api_env, org_id, created["id"], creator_headers)

    resp = await api_env.client.post(
        f"/api/v1/changes/{created['id']}/reject",
        json={"comment": "not ready"},
        headers=await api_env.headers(org_id, approver),
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["decision"] == "rejected"


# --------------------------------------------------------------------------- #
# POST /changes/{id}/transition -- illegal transition is 400, never 500
# --------------------------------------------------------------------------- #


async def test_transition_illegal_action_is_400_not_500(api_env) -> None:
    org_id = await api_env.create_org("chg")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    created = await _create_change(api_env, org_id, headers)

    # `begin` is illegal from `draft` (must be `approved` first).
    resp = await api_env.client.post(
        f"/api/v1/changes/{created['id']}/transition",
        json={"action": "begin"},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


# --------------------------------------------------------------------------- #
# GET|POST /changes/{id}/links, DELETE .../links/{link_id} -- tenant scoping
# --------------------------------------------------------------------------- #


async def test_list_links_unknown_change_is_404(api_env) -> None:
    org_id = await api_env.create_org("chg")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        f"/api/v1/changes/{uuid4()}/links", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 404, resp.text


async def test_links_manage_permission_and_cross_org_target_is_400(api_env) -> None:
    org_id = await api_env.create_org("chg")
    other_org_id = await api_env.create_org("chg-other")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    admin_headers = await api_env.headers(org_id, admin)

    created = await _create_change(api_env, org_id, admin_headers)
    own_target = await _patch_approval_target(api_env, org_id)
    other_org_target = await _patch_approval_target(api_env, other_org_id)

    forbidden = await api_env.client.post(
        f"/api/v1/changes/{created['id']}/links",
        json={"action_type": "patch_approval", "action_id": str(own_target)},
        headers=await api_env.headers(org_id, tech),
    )
    assert forbidden.status_code == 403

    ok = await api_env.client.post(
        f"/api/v1/changes/{created['id']}/links",
        json={"action_type": "patch_approval", "action_id": str(own_target)},
        headers=admin_headers,
    )
    assert ok.status_code == 201, ok.text
    link = ok.json()
    assert link["action_id"] == str(own_target)

    cross_org = await api_env.client.post(
        f"/api/v1/changes/{created['id']}/links",
        json={"action_type": "patch_approval", "action_id": str(other_org_target)},
        headers=admin_headers,
    )
    assert cross_org.status_code == 400, cross_org.text

    list_resp = await api_env.client.get(
        f"/api/v1/changes/{created['id']}/links",
        headers=await api_env.headers(org_id, await api_env.create_user(org_id, "read-only")),
    )
    assert list_resp.status_code == 200, list_resp.text
    assert len(list_resp.json()) == 1

    delete_resp = await api_env.client.delete(
        f"/api/v1/changes/{created['id']}/links/{link['id']}",
        headers=admin_headers,
    )
    assert delete_resp.status_code == 204, delete_resp.text

    delete_missing = await api_env.client.delete(
        f"/api/v1/changes/{created['id']}/links/{uuid4()}",
        headers=admin_headers,
    )
    assert delete_missing.status_code == 404, delete_missing.text


# --------------------------------------------------------------------------- #
# GET /changes/calendar, GET /changes/evidence, GET /changes/{id}/evidence
# --------------------------------------------------------------------------- #


async def test_calendar_ok_for_read(api_env) -> None:
    org_id = await api_env.create_org("chg")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/changes/calendar",
        params={"from": "2026-01-01T00:00:00Z", "to": "2026-12-31T00:00:00Z"},
        headers=await api_env.headers(org_id, ro),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "entries" in body
    assert "conflicts" in body


async def test_fleet_evidence_ok_for_read(api_env) -> None:
    org_id = await api_env.create_org("chg")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    await _create_change(api_env, org_id, await api_env.headers(org_id, tech))

    resp = await api_env.client.get(
        "/api/v1/changes/evidence", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


async def test_change_evidence_writes_audit_and_404s_unknown(api_env) -> None:
    org_id = await api_env.create_org("chg")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    tech_headers = await api_env.headers(org_id, tech)
    expected_user_id = await _user_id(api_env, org_id, ro)

    created = await _create_change(api_env, org_id, tech_headers)

    resp = await api_env.client.get(
        f"/api/v1/changes/{created['id']}/evidence",
        headers=await api_env.headers(org_id, ro),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["change"]["id"] == created["id"]
    assert body["approvals"] == []
    assert body["links"] == []

    async with org_session(org_id, engine=api_env.app_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "change.evidence_exported")
            )
        ).scalar_one()
    assert event.actor_type == "user"
    assert event.actor_id == expected_user_id
    assert event.target_type == "change"
    assert event.target_id == created["id"]

    missing = await api_env.client.get(
        f"/api/v1/changes/{uuid4()}/evidence",
        headers=await api_env.headers(org_id, ro),
    )
    assert missing.status_code == 404


async def test_evidence_forbidden_without_change_read(api_env) -> None:
    org_id = await api_env.create_org("chg")
    nobody = await api_env.create_user(org_id)
    resp = await api_env.client.get(
        "/api/v1/changes/evidence", headers=await api_env.headers(org_id, nobody)
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# GET /changes/{change_id}/approvals -- dedicated non-auditing read used for
# display; unlike /evidence, it must never write `change.evidence_exported`.
# --------------------------------------------------------------------------- #


async def test_list_approvals_returns_history_without_audit(api_env) -> None:
    org_id = await api_env.create_org("chg")
    tech = await api_env.create_user(org_id, "technician")
    approver_1 = await api_env.create_user(org_id, "org-admin")
    approver_2 = await api_env.create_user(org_id, "org-admin")
    tech_headers = await api_env.headers(org_id, tech)

    # Emergency change: `begin` fast-paths straight to `implementing`, so
    # both approve/reject decisions recorded afterward are retrospective and
    # do not move the run state -- letting us record two decisions on one
    # change, in order, without a state-machine reset in between.
    created = await api_env.client.post(
        "/api/v1/changes",
        json={**_CHANGE_BODY, "type": "emergency"},
        headers=tech_headers,
    )
    assert created.status_code == 201, created.text
    change_id = created.json()["id"]
    await _submit(api_env, org_id, change_id, tech_headers)
    begin = await api_env.client.post(
        f"/api/v1/changes/{change_id}/transition",
        json={"action": "begin"},
        headers=tech_headers,
    )
    assert begin.status_code == 200, begin.text

    first = await api_env.client.post(
        f"/api/v1/changes/{change_id}/approve",
        json={"comment": "first pass"},
        headers=await api_env.headers(org_id, approver_1),
    )
    assert first.status_code == 200, first.text
    second = await api_env.client.post(
        f"/api/v1/changes/{change_id}/reject",
        json={"comment": "second look"},
        headers=await api_env.headers(org_id, approver_2),
    )
    assert second.status_code == 200, second.text

    async def _evidence_export_count() -> int:
        async with org_session(org_id, engine=api_env.app_engine) as session:
            rows = (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "change.evidence_exported")
                )
            ).scalars().all()
            return len(rows)

    before = await _evidence_export_count()

    resp = await api_env.client.get(
        f"/api/v1/changes/{change_id}/approvals",
        headers=tech_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    assert body[0]["comment"] == "first pass"
    assert body[0]["decision"] == "approved"
    assert body[1]["comment"] == "second look"
    assert body[1]["decision"] == "rejected"
    assert body[0]["decided_at"] <= body[1]["decided_at"]

    after = await _evidence_export_count()
    assert after == before, "GET /approvals must never write change.evidence_exported"


async def test_list_approvals_unknown_change_is_404(api_env) -> None:
    org_id = await api_env.create_org("chg")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        f"/api/v1/changes/{uuid4()}/approvals",
        headers=await api_env.headers(org_id, ro),
    )
    assert resp.status_code == 404, resp.text
