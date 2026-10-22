"""Agent self-update rollouts: RBAC, rules and audit (spec-self-update.md §6-§8).

Authorization first: a 403 for each code on each route, and a rollback needs
both codes. Then the rollout rules: ring 0 is a pilot of at most 25 devices,
non-pilot rings need a `stable` release, one active rollout per org, and the
three kernel allowlists all name `agent.update`.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.approved_actions import NON_APPROVABLE_REASONS, non_approvable_reason
from app.core.storage import ObjectStore, agent_release_suffix
from app.core.tenancy import org_session
from app.jobs.direct_dispatch import MODULE_GOVERNED_REASONS
from app.jobs.dispatch_permissions import DISPATCH_PERMISSIONS
from app.models import AuditEvent, Permission, Role, RolePermission, UserRole
from app.models.core import User
from app.modules.agent_update.models import AgentUpdateRing, AgentUpdateTarget
from tests.agent_update_support import seed_device, seed_group, seed_release, unique_version
from tests.conftest import ApiEnv

BASE = "/api/v1/agent"


def _body(version: str, **extra: Any) -> dict[str, Any]:
    return {"target_version": version, "rings": [{"name": "pilot", "is_pilot": True}], **extra}


async def _org_with_admin(api_env: ApiEnv) -> tuple[UUID, dict[str, str]]:
    org_id = await api_env.create_org("upd")
    admin = await api_env.create_user(org_id, "org-admin")
    return org_id, await api_env.headers(org_id, admin)


async def _user_with_codes(api_env: ApiEnv, org_id: UUID, *codes: str) -> dict[str, str]:
    """A user holding exactly `codes`, through an org-scoped role."""
    sub = f"sub-{uuid4().hex[:10]}"
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        role = Role(org_id=org_id, name=f"only-{uuid4().hex[:6]}")
        user = User(org_id=org_id, external_subject=sub, email=f"{sub}@test.local")
        s.add_all([role, user])
        await s.flush()
        for code in codes:
            perm = (await s.execute(select(Permission).where(Permission.code == code))).scalar_one()
            s.add(RolePermission(role_id=role.id, permission_id=perm.id))
        s.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await s.commit()
    return await api_env.headers(org_id, sub)


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------

_RID = "00000000-0000-7000-8000-000000000001"
_ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/fleet-versions", "agent.update.read"),
    ("GET", "/update-rollouts", "agent.update.read"),
    ("GET", f"/update-rollouts/{_RID}", "agent.update.read"),
    ("GET", f"/update-rollouts/{_RID}/targets", "agent.update.read"),
    ("POST", "/update-rollouts/preview", "agent.update.rollout"),
    ("POST", "/update-rollouts", "agent.update.rollout"),
    ("POST", f"/update-rollouts/{_RID}/start", "agent.update.rollout"),
    ("POST", f"/update-rollouts/{_RID}/pause", "agent.update.rollout"),
    ("POST", f"/update-rollouts/{_RID}/resume", "agent.update.rollout"),
    ("POST", f"/update-rollouts/{_RID}/advance", "agent.update.rollout"),
    ("POST", f"/update-rollouts/{_RID}/rings/{_RID}/close", "agent.update.rollout"),
    ("POST", f"/update-rollouts/{_RID}/cancel", "agent.update.rollout"),
    ("POST", "/releases/versions/1.0.0/promote", "agent.release.manage"),
    ("PUT", f"/releases/{_RID}/release-signature", "agent.release.manage"),
]


async def test_each_route_refuses_a_caller_without_its_code(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("upd")
    every_other = [
        "agent.update.read",
        "agent.update.rollout",
        "agent.update.rollback",
        "agent.release.read",
        "jobs.dispatch",
    ]
    for method, path, code in _ROUTES:
        headers = await _user_with_codes(api_env, org_id, *[c for c in every_other if c != code])
        body = _body("1.0.0") if method != "GET" else None
        if path.endswith("release-signature"):
            body = {"release_signature": "A" * 86 + "=="}
        if path.endswith("promote"):
            body = {"channel": "stable"}
        resp = await api_env.client.request(method, BASE + path, json=body, headers=headers)
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code} {resp.text}"
        assert code in resp.text


async def test_roles_hold_what_the_spec_seeds(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("upd")
    tech = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    assert (await api_env.client.get(f"{BASE}/update-rollouts", headers=tech)).status_code == 200
    resp = await api_env.client.post(
        f"{BASE}/update-rollouts/preview", json=_body("1.0.0"), headers=tech
    )
    assert resp.status_code == 403
    # org-admin may roll out but may not promote: that writes the global catalogue.
    resp = await api_env.client.post(
        f"{BASE}/releases/versions/1.0.0/promote", json={"channel": "stable"}, headers=admin
    )
    assert resp.status_code == 403


async def test_a_rollback_rollout_needs_both_codes(api_env: ApiEnv) -> None:
    org_id, _ = await _org_with_admin(api_env)
    version = unique_version(1)
    await seed_release(api_env.admin_engine, version, channel="stable")
    await seed_device(api_env.admin_engine, org_id, version="3.0.0")
    body = _body(version, is_rollback=True, reason="supersedes 3.0.0")

    rollout_only = await _user_with_codes(api_env, org_id, "agent.update.rollout")
    resp = await api_env.client.post(f"{BASE}/update-rollouts", json=body, headers=rollout_only)
    assert resp.status_code == 403 and "agent.update.rollback" in resp.text

    both = await _user_with_codes(api_env, org_id, "agent.update.rollout", "agent.update.rollback")
    resp = await api_env.client.post(f"{BASE}/update-rollouts", json=body, headers=both)
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_rollback"] is True


async def test_a_rollback_must_name_what_it_supersedes(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    version = unique_version(1)
    await seed_release(api_env.admin_engine, version, channel="stable")
    resp = await api_env.client.post(
        f"{BASE}/update-rollouts", json=_body(version, is_rollback=True), headers=headers
    )
    assert resp.status_code == 422
    assert "supersedes" in resp.text


def test_the_three_allowlists_name_agent_update() -> None:
    assert "agent.update" in NON_APPROVABLE_REASONS
    assert non_approvable_reason("agent.update") is not None
    assert "/api/v1/agent/update-rollouts" in MODULE_GOVERNED_REASONS["agent.update"]
    assert DISPATCH_PERMISSIONS["agent.update"] == "agent.update.rollout"


async def test_the_kernel_jobs_route_refuses_agent_update(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    device_id = await seed_device(api_env.admin_engine, org_id)
    resp = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "agent.update", "device_id": str(device_id), "params": {}},
        headers=headers,
    )
    assert resp.status_code in (400, 403, 422), resp.text
    assert "update-rollouts" in resp.text


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


async def test_preview_classifies_devices_and_writes_nothing(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    version = unique_version()
    await seed_release(api_env.admin_engine, version)
    group = await seed_group(api_env.admin_engine, org_id, "pilot")
    await seed_device(api_env.admin_engine, org_id, group_id=group)
    await seed_device(api_env.admin_engine, org_id, group_id=group, capable=False)
    await seed_device(api_env.admin_engine, org_id, group_id=group, version="")
    await seed_device(api_env.admin_engine, org_id, group_id=group, os_family="windows")
    await seed_device(api_env.admin_engine, org_id, group_id=group, version=version)
    await seed_device(api_env.admin_engine, org_id)

    body = {
        "target_version": version,
        "rings": [
            {"name": "pilot", "is_pilot": True, "device_group_id": str(group)},
            {"name": "everyone"},
        ],
    }
    resp = await api_env.client.post(f"{BASE}/update-rollouts/preview", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    pilot, rest = resp.json()["rings"]
    assert (pilot["total"], pilot["eligible"], pilot["needs_manual_update"]) == (5, 1, 2)
    assert (pilot["no_artifact"], pilot["already_current"]) == (1, 1)
    assert (rest["total"], rest["eligible"]) == (1, 1)
    assert resp.json()["channel"] == "pilot"
    listed = await api_env.client.get(f"{BASE}/update-rollouts", headers=headers)
    assert listed.json() == []


async def test_ring_zero_must_be_a_pilot_of_at_most_25(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    version = unique_version()
    await seed_release(api_env.admin_engine, version)
    resp = await api_env.client.post(
        f"{BASE}/update-rollouts/preview",
        json={"target_version": version, "rings": [{"name": "all"}]},
        headers=headers,
    )
    assert resp.status_code == 422 and "pilot" in resp.text

    for _ in range(26):
        await seed_device(api_env.admin_engine, org_id)
    resp = await api_env.client.post(
        f"{BASE}/update-rollouts/preview", json=_body(version), headers=headers
    )
    assert resp.status_code == 422 and "at most 25" in resp.text


async def test_only_the_last_ring_takes_every_remaining_device(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    version = unique_version()
    await seed_release(api_env.admin_engine, version)
    resp = await api_env.client.post(
        f"{BASE}/update-rollouts/preview",
        json={
            "target_version": version,
            "rings": [{"name": "pilot", "is_pilot": True}, {"name": "rest"}],
        },
        headers=headers,
    )
    assert resp.status_code == 422 and "last ring" in resp.text


async def _started(
    api_env: ApiEnv, headers: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    created = await api_env.client.post(f"{BASE}/update-rollouts", json=body, headers=headers)
    assert created.status_code == 201, created.text
    started = await api_env.client.post(
        f"{BASE}/update-rollouts/{created.json()['id']}/start", headers=headers
    )
    assert started.status_code == 200, started.text
    result: dict[str, Any] = started.json()
    return result


async def test_start_materializes_targets_and_activates_the_pilot(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    version = unique_version()
    await seed_release(api_env.admin_engine, version)
    group = await seed_group(api_env.admin_engine, org_id, "pilot")
    eligible = await seed_device(api_env.admin_engine, org_id, group_id=group)
    await seed_device(api_env.admin_engine, org_id, group_id=group, capable=False)
    body = {
        "target_version": version,
        "rings": [{"name": "pilot", "is_pilot": True, "device_group_id": str(group)}],
    }
    out = await _started(api_env, headers, body)
    assert out["state"] == "running"
    assert out["rings"][0]["state"] == "active"
    assert out["rings"][0]["counts"] == {"pending": 1, "needs_manual_update": 1}

    targets = await api_env.client.get(
        f"{BASE}/update-rollouts/{out['id']}/targets", headers=headers
    )
    by_device = {t["device_id"]: t for t in targets.json()}
    assert by_device[str(eligible)]["state"] == "pending"
    assert by_device[str(eligible)]["from_version"] == "1.0.0"

    async with org_session(org_id, engine=api_env.app_engine) as s:
        actions = [
            e.action
            for e in (
                await s.execute(
                    select(AuditEvent)
                    .where(AuditEvent.action.like("agent.update.%"))
                    .order_by(AuditEvent.ts, AuditEvent.id)
                )
            ).scalars()
        ]
    assert actions == ["agent.update.rollout_created", "agent.update.rollout_started"]


async def test_one_active_rollout_per_org(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    version = unique_version()
    await seed_release(api_env.admin_engine, version)
    await seed_device(api_env.admin_engine, org_id)
    await _started(api_env, headers, _body(version))
    second = await api_env.client.post(
        f"{BASE}/update-rollouts", json=_body(version), headers=headers
    )
    assert second.status_code == 201
    resp = await api_env.client.post(
        f"{BASE}/update-rollouts/{second.json()['id']}/start", headers=headers
    )
    assert resp.status_code == 409


async def test_a_non_pilot_ring_needs_a_stable_release(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    super_admin = await api_env.headers(org_id, await api_env.create_user(org_id, "super-admin"))
    version = unique_version()
    await seed_release(api_env.admin_engine, version)
    group = await seed_group(api_env.admin_engine, org_id, "pilot")
    await seed_device(api_env.admin_engine, org_id, group_id=group, online=False)
    await seed_device(api_env.admin_engine, org_id, online=False)
    body = {
        "target_version": version,
        "rings": [
            {"name": "pilot", "is_pilot": True, "device_group_id": str(group)},
            {"name": "everyone"},
        ],
    }
    out = await _started(api_env, headers, body)
    pilot_id = out["rings"][0]["id"]
    # Offline pilot devices: close the ring so it can finish.
    closed = await api_env.client.post(
        f"{BASE}/update-rollouts/{out['id']}/rings/{pilot_id}/close", headers=headers
    )
    assert closed.status_code == 200, closed.text
    async with org_session(org_id, engine=api_env.app_engine) as s:
        ring = await s.get_one(AgentUpdateRing, UUID(pilot_id))
        ring.state = "done"

    advance = await api_env.client.post(
        f"{BASE}/update-rollouts/{out['id']}/advance", headers=headers
    )
    assert advance.status_code == 409 and "stable" in advance.text

    promoted = await api_env.client.post(
        f"{BASE}/releases/versions/{version}/promote",
        json={"channel": "stable"},
        headers=super_admin,
    )
    assert promoted.status_code == 200 and promoted.json()["rows"] == 1
    advance = await api_env.client.post(
        f"{BASE}/update-rollouts/{out['id']}/advance", headers=headers
    )
    assert advance.status_code == 200, advance.text
    assert [r["state"] for r in advance.json()["rings"]] == ["done", "active"]


async def test_pause_resume_cancel(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    version = unique_version()
    await seed_release(api_env.admin_engine, version)
    await seed_device(api_env.admin_engine, org_id)
    out = await _started(api_env, headers, _body(version))
    rid = out["id"]
    assert (
        await api_env.client.post(f"{BASE}/update-rollouts/{rid}/pause", headers=headers)
    ).json()["state"] == "paused"
    assert (
        await api_env.client.post(f"{BASE}/update-rollouts/{rid}/advance", headers=headers)
    ).status_code == 409
    assert (
        await api_env.client.post(f"{BASE}/update-rollouts/{rid}/resume", headers=headers)
    ).json()["state"] == "running"
    cancelled = await api_env.client.post(f"{BASE}/update-rollouts/{rid}/cancel", headers=headers)
    assert cancelled.json()["state"] == "cancelled"
    async with org_session(org_id, engine=api_env.app_engine) as s:
        states = set(
            (
                await s.execute(
                    select(AgentUpdateTarget.state).where(AgentUpdateTarget.rollout_id == UUID(rid))
                )
            ).scalars()
        )
    assert states == {"skipped"}


async def test_rollouts_are_tenant_scoped(api_env: ApiEnv) -> None:
    org_a, headers_a = await _org_with_admin(api_env)
    _org_b, headers_b = await _org_with_admin(api_env)
    version = unique_version()
    await seed_release(api_env.admin_engine, version)
    await seed_device(api_env.admin_engine, org_a)
    out = await _started(api_env, headers_a, _body(version))
    assert (
        await api_env.client.get(f"{BASE}/update-rollouts/{out['id']}", headers=headers_b)
    ).status_code == 404
    assert (await api_env.client.get(f"{BASE}/update-rollouts", headers=headers_b)).json() == []


async def test_a_release_in_an_active_rollout_cannot_be_unpublished(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    super_admin = await api_env.headers(org_id, await api_env.create_user(org_id, "super-admin"))
    version = unique_version()
    release_id = await seed_release(api_env.admin_engine, version)
    await seed_device(api_env.admin_engine, org_id)
    await _started(api_env, headers, _body(version))
    resp = await api_env.client.delete(f"/api/v1/agent/releases/{release_id}", headers=super_admin)
    assert resp.status_code == 409


class _RecordingStore(ObjectStore):
    """An object store that remembers which agent artefacts were deleted.

    A subclass, because the route asserts it holds an `ObjectStore`; the parent
    constructor is skipped since it would reach for a real bucket."""

    def __init__(self) -> None:  # noqa: D107 -- deliberately not calling super()
        self.deleted: list[str] = []

    async def delete_agent_release(self, key: str) -> None:
        self.deleted.append(key)


async def _release_ids(api_env: ApiEnv, version: str) -> list[UUID]:
    from app.models.core import AgentRelease

    async with AsyncSession(api_env.admin_engine) as s:
        return list(
            (
                await s.execute(select(AgentRelease.id).where(AgentRelease.version == version))
            ).scalars()
        )


async def test_a_version_used_by_a_past_rollout_is_removed_whole_with_its_files(
    api_env: ApiEnv,
) -> None:
    """Codex review on #87: a completed or cancelled rollout's targets kept a
    foreign key to the release, so the delete died on it -- after earlier
    platforms of the version had already gone. Now the version goes in one
    transaction, the past targets let go (keeping their version strings), and
    the stored files are deleted after the commit."""
    org_id, headers = await _org_with_admin(api_env)
    super_admin = await api_env.headers(org_id, await api_env.create_user(org_id, "super-admin"))
    version = unique_version()
    await seed_release(api_env.admin_engine, version)
    await seed_release(api_env.admin_engine, version, os_name="windows")
    await seed_device(api_env.admin_engine, org_id)
    rollout = await _started(api_env, headers, _body(version))
    cancel = await api_env.client.post(
        f"{BASE}/update-rollouts/{rollout['id']}/cancel", headers=headers
    )
    assert cancel.status_code == 200
    store = _RecordingStore()
    api_env.app.state.object_store = store
    try:
        resp = await api_env.client.delete(
            f"{BASE}/releases/versions/{version}", headers=super_admin
        )
    finally:
        api_env.app.state.object_store = None

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["removed"], body["files_not_removed"]) == (2, [])
    assert body["history_cleared"] >= 1
    assert await _release_ids(api_env, version) == []
    assert sorted(store.deleted) == sorted(
        [
            agent_release_suffix(version, "linux", "amd64"),
            agent_release_suffix(version, "windows", "amd64"),
        ]
    )
    async with AsyncSession(api_env.admin_engine) as s:
        targets = list(
            (
                await s.execute(
                    select(AgentUpdateTarget).where(
                        AgentUpdateTarget.rollout_id == UUID(rollout["id"])
                    )
                )
            ).scalars()
        )
    assert targets and all(t.release_id is None for t in targets)


async def test_a_version_in_an_active_rollout_is_refused_and_nothing_is_removed(
    api_env: ApiEnv,
) -> None:
    org_id, headers = await _org_with_admin(api_env)
    super_admin = await api_env.headers(org_id, await api_env.create_user(org_id, "super-admin"))
    version = unique_version()
    await seed_release(api_env.admin_engine, version, os_name="darwin", arch="arm64")
    await seed_release(api_env.admin_engine, version)
    await seed_device(api_env.admin_engine, org_id)
    await _started(api_env, headers, _body(version))
    store = _RecordingStore()
    api_env.app.state.object_store = store
    try:
        resp = await api_env.client.delete(
            f"{BASE}/releases/versions/{version}", headers=super_admin
        )
    finally:
        api_env.app.state.object_store = None

    assert resp.status_code == 409
    assert len(await _release_ids(api_env, version)) == 2
    assert store.deleted == []


async def test_removing_a_version_needs_the_release_permission(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    version = unique_version()
    await seed_release(api_env.admin_engine, version)
    resp = await api_env.client.delete(f"{BASE}/releases/versions/{version}", headers=headers)
    assert resp.status_code == 403
    assert len(await _release_ids(api_env, version)) == 1


async def test_fleet_versions_lists_devices_that_need_a_manual_update(api_env: ApiEnv) -> None:
    org_id, headers = await _org_with_admin(api_env)
    await seed_device(api_env.admin_engine, org_id, version="1.0.0")
    old = await seed_device(api_env.admin_engine, org_id, version="0.9.0", capable=False)
    unknown = await seed_device(api_env.admin_engine, org_id, version="")
    resp = await api_env.client.get(f"{BASE}/fleet-versions", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    manual = {d["device_id"]: d["reason"] for d in body["needs_manual_update"]}
    assert manual == {str(old): "no_agent_update", str(unknown): "version_unknown"}


async def test_the_release_signature_is_recorded_and_reported(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("upd")
    super_admin = await api_env.headers(org_id, await api_env.create_user(org_id, "super-admin"))
    release_id = await seed_release(api_env.admin_engine, unique_version())
    sig = "Q" * 86 + "=="
    resp = await api_env.client.put(
        f"{BASE}/releases/{release_id}/release-signature",
        json={"release_signature": sig},
        headers=super_admin,
    )
    assert resp.status_code == 204, resp.text
    listed = await api_env.client.get(f"{BASE}/releases", headers=super_admin)
    row = next(r for r in listed.json() if r["id"] == str(release_id))
    assert row["release_signed"] is True and row["channel"] == "pilot"
    bad = await api_env.client.put(
        f"{BASE}/releases/{release_id}/release-signature",
        json={"release_signature": "not base64"},
        headers=super_admin,
    )
    assert bad.status_code == 422


@pytest.mark.parametrize("version", ["abc1234", "1.2", "1.2.3+build"])
async def test_a_non_semver_version_cannot_be_rolled_out(api_env: ApiEnv, version: str) -> None:
    _org, headers = await _org_with_admin(api_env)
    resp = await api_env.client.post(
        f"{BASE}/update-rollouts/preview", json=_body(version), headers=headers
    )
    assert resp.status_code == 422 and "semver" in resp.text
