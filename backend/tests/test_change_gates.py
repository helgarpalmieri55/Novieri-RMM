"""Change gating (docs/modules/change-mgmt/spec.md §11).

A gate on a device group (or every device) means patch approvals and
deployments reaching it need a linked change that is approved or being
implemented. It is enforced when the action is requested and again at
dispatch, with a sentence the operator can act on and a `change.gate_refused`
audit event. With no gate, an approval or deployment made without a change
gets an automatic change record so the evidence exists.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    ChangeGate,
    ChangeLink,
    ChangeRequest,
    Deployment,
    Device,
    DeviceGroup,
    DevicePatchStatus,
    Package,
    Patch,
    PatchApproval,
    PatchPolicy,
    User,
)
from app.modules.patch.apply import dispatch_apply
from tests.test_patch_reboot import _FakeJobEngine


async def _fleet(api_env) -> dict:
    org_id = await api_env.create_org("gate")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        servers = DeviceGroup(org_id=org_id, name="Servers")
        laptops = DeviceGroup(org_id=org_id, name="Laptops")
        s.add_all([servers, laptops])
        await s.flush()
        s.add_all(
            [
                PatchPolicy(org_id=org_id, device_group_id=servers.id, ring_name="broad"),
                PatchPolicy(org_id=org_id, device_group_id=laptops.id, ring_name="pilot"),
            ]
        )
        srv = Device(
            org_id=org_id, hostname="srv1", os_family="windows", device_group_id=servers.id
        )
        lap = Device(
            org_id=org_id, hostname="lap1", os_family="windows", device_group_id=laptops.id
        )
        patch = Patch(
            org_id=org_id,
            os_family="windows",
            source="windows_update",
            identifier="KB77",
            title="CU",
            severity="critical",
        )
        package = Package(
            org_id=org_id,
            name="7-Zip",
            version="24.08",
            os_family="windows",
            installer_type="winget",
            package_identifier="7zip.7zip",
        )
        s.add_all([srv, lap, patch, package])
        await s.flush()
        s.add(
            DevicePatchStatus(org_id=org_id, device_id=srv.id, patch_id=patch.id, state="missing")
        )
        user_id = (
            await s.execute(select(User.id).where(User.external_subject == admin))
        ).scalar_one()
        await s.commit()
    return {
        "org_id": org_id,
        "admin": admin,
        "tech": tech,
        "admin_id": user_id,
        "servers": servers.id,
        "laptops": laptops.id,
        "srv": srv.id,
        "patch": patch.id,
        "package": package.id,
    }


async def _gate(api_env, f: dict, group: UUID | None, **kw: bool) -> None:
    resp = await api_env.client.put(
        "/api/v1/changes/gates",
        json={"device_group_id": str(group) if group else None, **kw},
        headers=await api_env.headers(f["org_id"], f["admin"]),
    )
    assert resp.status_code == 200, resp.text


async def _change(api_env, f: dict, state: str) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        change = ChangeRequest(
            org_id=f["org_id"],
            type="normal",
            title="Patch Tuesday",
            description="",
            risk="medium",
            affected=[],
            state=state,
            created_by=f["admin_id"],
        )
        s.add(change)
        await s.commit()
        return change.id


async def _actions(api_env, org_id: UUID, action: str) -> list[AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as s:
        return list(
            (
                await s.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == action
                    )
                )
            ).scalars()
        )


async def test_with_no_gate_an_approval_gets_an_automatic_change_record(api_env) -> None:
    f = await _fleet(api_env)
    resp = await api_env.client.post(
        "/api/v1/patch/approvals/bulk",
        json={"patch_ids": [str(f["patch"])], "ring_name": "broad"},
        headers=await api_env.headers(f["org_id"], f["admin"]),
    )
    assert resp.status_code == 201, resp.text
    async with AsyncSession(api_env.admin_engine) as s:
        [change] = (
            (await s.execute(select(ChangeRequest).where(ChangeRequest.org_id == f["org_id"])))
            .scalars()
            .all()
        )
        [link] = (
            (await s.execute(select(ChangeLink).where(ChangeLink.change_id == change.id)))
            .scalars()
            .all()
        )
    assert change.origin == "auto" and change.state == "completed"
    assert "broad" in change.title
    assert link.action_type == "patch_approval"
    assert len(await _actions(api_env, f["org_id"], "change.auto_recorded")) == 1

    listed = await api_env.client.get(
        "/api/v1/changes", headers=await api_env.headers(f["org_id"], f["admin"])
    )
    assert listed.json()[0]["origin"] == "auto"


async def test_a_gated_ring_refuses_an_approval_without_a_change(api_env) -> None:
    f = await _fleet(api_env)
    await _gate(api_env, f, f["servers"], applies_to_patches=True, applies_to_deployments=False)
    resp = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(f["patch"]), "ring_name": "broad"},
        headers=await api_env.headers(f["org_id"], f["admin"]),
    )
    assert resp.status_code == 409
    assert "Servers group" in resp.json()["detail"]
    assert "approved change" in resp.json()["detail"]
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(select(PatchApproval).where(PatchApproval.org_id == f["org_id"]))
        ).first() is None
    [refused] = await _actions(api_env, f["org_id"], "change.gate_refused")
    assert refused.target_id == "broad"

    # The Laptops ring is not gated.
    ok = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(f["patch"]), "ring_name": "pilot"},
        headers=await api_env.headers(f["org_id"], f["admin"]),
    )
    assert ok.status_code == 201, ok.text


async def test_an_approved_change_authorizes_and_is_linked(api_env) -> None:
    f = await _fleet(api_env)
    await _gate(api_env, f, None, applies_to_patches=True, applies_to_deployments=True)
    draft = await _change(api_env, f, "draft")
    refused = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(f["patch"]), "ring_name": "broad", "change_id": str(draft)},
        headers=await api_env.headers(f["org_id"], f["admin"]),
    )
    assert refused.status_code == 422 and "draft" in refused.json()["detail"]
    missing = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(f["patch"]), "ring_name": "broad", "change_id": str(uuid4())},
        headers=await api_env.headers(f["org_id"], f["admin"]),
    )
    assert missing.status_code == 404

    approved = await _change(api_env, f, "approved")
    ok = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(f["patch"]), "ring_name": "broad", "change_id": str(approved)},
        headers=await api_env.headers(f["org_id"], f["admin"]),
    )
    assert ok.status_code == 201, ok.text
    async with AsyncSession(api_env.admin_engine) as s:
        [link] = (
            (await s.execute(select(ChangeLink).where(ChangeLink.change_id == approved)))
            .scalars()
            .all()
        )
    assert link.action_id == UUID(ok.json()["id"])


async def test_dispatch_withholds_a_gated_approval_whose_change_was_cancelled(api_env) -> None:
    f = await _fleet(api_env)
    approved = await _change(api_env, f, "approved")
    await _gate(api_env, f, f["servers"], applies_to_patches=True, applies_to_deployments=False)
    ok = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(f["patch"]), "ring_name": "broad", "change_id": str(approved)},
        headers=await api_env.headers(f["org_id"], f["admin"]),
    )
    assert ok.status_code == 201

    engine = _FakeJobEngine()
    async with org_session(
        f["org_id"], engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        assert (
            len(await dispatch_apply(session, engine, org_id=f["org_id"], now=datetime.now(UTC)))
            == 1
        )

    async with AsyncSession(api_env.admin_engine) as s:
        change = (
            await s.execute(select(ChangeRequest).where(ChangeRequest.id == approved))
        ).scalar_one()
        change.state = "cancelled"
        await s.commit()
    engine = _FakeJobEngine()
    async with org_session(
        f["org_id"], engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        assert (
            await dispatch_apply(session, engine, org_id=f["org_id"], now=datetime.now(UTC)) == []
        )
    [refused] = await _actions(api_env, f["org_id"], "change.gate_refused")
    assert refused.target_type == "device" and refused.target_id == str(f["srv"])
    assert refused.actor_type == "system"


async def test_deployments_are_gated_at_creation_and_at_execution(api_env) -> None:
    f = await _fleet(api_env)
    headers = await api_env.headers(f["org_id"], f["admin"])
    body = {
        "package_id": str(f["package"]),
        "action": "install",
        "target_type": "group",
        "target_id": str(f["servers"]),
    }
    # No gate: created, with an automatic change record.
    first = await api_env.client.post("/api/v1/deploy/deployments", json=body, headers=headers)
    assert first.status_code == 201, first.text
    assert len(await _actions(api_env, f["org_id"], "change.auto_recorded")) == 1

    await _gate(api_env, f, f["servers"], applies_to_patches=False, applies_to_deployments=True)
    refused = await api_env.client.post("/api/v1/deploy/deployments", json=body, headers=headers)
    assert refused.status_code == 409
    assert "Deployments to the Servers group need an approved change" in refused.json()["detail"]

    # The one created before the gate cannot be executed now either.
    execute = await api_env.client.post(
        f"/api/v1/deploy/deployments/{first.json()['id']}/execute", headers=headers
    )
    assert execute.status_code == 409
    assert len(await _actions(api_env, f["org_id"], "change.gate_refused")) == 2

    approved = await _change(api_env, f, "implementing")
    linked = await api_env.client.post(
        "/api/v1/deploy/deployments", json={**body, "change_id": str(approved)}, headers=headers
    )
    assert linked.status_code == 201, linked.text
    async with AsyncSession(api_env.admin_engine) as s:
        link = (
            await s.execute(select(ChangeLink).where(ChangeLink.change_id == approved))
        ).scalar_one()
        dep = (
            await s.execute(select(Deployment).where(Deployment.id == link.action_id))
        ).scalar_one()
    assert link.action_type == "deployment" and str(dep.id) == linked.json()["id"]


async def test_gates_are_managed_by_org_admins_only(api_env) -> None:
    f = await _fleet(api_env)
    denied = await api_env.client.put(
        "/api/v1/changes/gates",
        json={"device_group_id": str(f["servers"])},
        headers=await api_env.headers(f["org_id"], f["tech"]),
    )
    assert denied.status_code == 403
    await _gate(api_env, f, f["servers"], applies_to_patches=True, applies_to_deployments=True)
    # Setting it again replaces rather than duplicates.
    await _gate(api_env, f, f["servers"], applies_to_patches=False, applies_to_deployments=True)
    listed = await api_env.client.get(
        "/api/v1/changes/gates", headers=await api_env.headers(f["org_id"], f["tech"])
    )
    assert listed.status_code == 200
    [gate] = listed.json()
    assert gate["device_group_name"] == "Servers" and gate["applies_to_patches"] is False

    neither = await api_env.client.put(
        "/api/v1/changes/gates",
        json={"applies_to_patches": False, "applies_to_deployments": False},
        headers=await api_env.headers(f["org_id"], f["admin"]),
    )
    assert neither.status_code == 400

    gone = await api_env.client.delete(
        f"/api/v1/changes/gates/{gate['id']}",
        headers=await api_env.headers(f["org_id"], f["admin"]),
    )
    assert gone.status_code == 204
    async with AsyncSession(api_env.admin_engine) as s:
        assert (
            await s.execute(select(ChangeGate).where(ChangeGate.org_id == f["org_id"]))
        ).first() is None
    assert len(await _actions(api_env, f["org_id"], "change.gate_set")) == 2
    assert len(await _actions(api_env, f["org_id"], "change.gate_removed")) == 1
