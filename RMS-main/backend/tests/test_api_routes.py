"""Operator API routes: RBAC gates, happy paths, live dispatch publication."""

import asyncio
import json
from uuid import uuid4

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device

REDIS_URL = "redis://localhost:6379/9"


async def _add_device(api_env, org_id, hostname="api-dev-1"):
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        device = Device(org_id=org_id, hostname=hostname, os_family="windows")
        session.add(device)
        await session.commit()
        return device.id


async def test_me_returns_identity_and_permissions(api_env) -> None:
    org_id = await api_env.create_org("api")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    response = await api_env.client.get("/api/v1/me", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["org_id"] == str(org_id)
    assert set(body["permissions"]) == {
        "devices.read",
        # Migration 0170 (install link): a technician approves or rejects a
        # computer that joined through the install link.
        "devices.review",
        # Migration 0062. A technician recording the switch they are standing
        # in front of is the ordinary work of the role, and the code publishes
        # no executable and mints no credential -- unlike `agent.release.manage`,
        # which is org-admin only for exactly that reason.
        "assets.manage",
        # Migration 0095: a technician sees fleet versions and rollouts; only
        # an org-admin starts one (spec-self-update.md §8.3).
        "agent.update.read",
        "jobs.read",
        "jobs.dispatch",
        "docs.read",
        "docs.manage",
        "remote.control",
        "patch.read",
        "patch.scan",
        "patch.apply",
        "deploy.read",
        "deploy.create",
        "deploy.execute",
        "deploy.script.run",
        "backup.read",
        "backup.run",
        "backup.restore",
        "provisioning.read",
        "provisioning.run",
        "change.read",
        "change.request",
        "bcp.read",
        "bcp.manage",
        "licenses.read",
        "licenses.manage",
        "subscriptions.read",
        "subscriptions.manage",
        # People (migration 0115): technicians run new starters, not leavers.
        "people.read",
        "people.manage",
        "ticket.read",
        "ticket.write",
        "ticket.assign",
        # Endpoint health spec §11.1 (migration 0040): the technician tier gets
        # exactly the alert day-job -- see, acknowledge, mute, close. It gets
        # NEITHER `health.admin` (which decides what the whole fleet alarms on)
        # NOR `health.remediate`/`health.action.admin` (which put signed code on
        # endpoints as SYSTEM/root). That exclusion is pinned separately, and
        # behaviourally, by `test_health_kernel_registry.py::
        # test_technician_tier_roles_hold_no_high_privilege_health_grant`.
        "health.read",
        "health.alert.manage",
        # IPAM spec §11.2 (migration 0043): the technician tier documents and
        # assigns addresses, and may run a sweep INSIDE an already-authorized
        # scope. It gets NEITHER `ipam.manage` (the plan of record itself) NOR
        # `ipam.scope.manage`, which decides where this platform emits network
        # traffic -- collapsing `ipam.sweep.run` into it would let anyone who can
        # run a scan also decide what may be scanned. That exclusion is pinned
        # separately, and negatively, by `test_ipam_migration.py::
        # test_no_non_admin_role_holds_a_high_privilege_ipam_grant`.
        "ipam.read",
        "ipam.assign",
        "ipam.sweep.run",
        # AI automation spec §16 (migration 0048): the technician tier may MINT
        # proposals and nothing else. `ai.act.confirm` is admin tier and is
        # deliberately absent -- §9.2's confirm check is two-part, and a
        # technician holding it could fire `patch.apply` through the AI layer
        # while being refused it on the patch module's own route. That exclusion
        # is pinned separately, and negatively, by
        # `test_ai_autonomy_ceiling.py::test_the_grants_match_section_16`.
        # The grant is inert on its own: `ai.use` is admin-only (0045), so a
        # technician cannot open an AI session at all.
        "ai.act.propose",
        # Site servers (migration 0050). `site.read` + `site.wake` and NOT
        # `site.manage`: waking a machine inside an already-declared segment is
        # routine technician work, while deciding that a host may serve an entire
        # subnet is not. That exclusion is pinned separately, and negatively, by
        # `test_site_server_migration.py::
        # test_technician_may_wake_but_may_not_manage` -- a test that only
        # checked for presence would pass against a migration that granted
        # everything to everyone.
        "site.read",
        "site.wake",
    }


async def test_devices_list_requires_permission_and_scopes_to_org(api_env) -> None:
    org_id = await api_env.create_org("api")
    other_org = await api_env.create_org("api-other")
    await _add_device(api_env, org_id, "mine-01")
    await _add_device(api_env, other_org, "theirs-01")

    reader = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, reader)
    response = await api_env.client.get("/api/v1/devices", headers=headers)
    assert response.status_code == 200
    hostnames = {d["hostname"] for d in response.json()["items"]}
    assert "mine-01" in hostnames
    assert "theirs-01" not in hostnames

    # a user with no roles is refused
    nobody = await api_env.create_user(org_id)
    headers = await api_env.headers(org_id, nobody)
    assert (await api_env.client.get("/api/v1/devices", headers=headers)).status_code == 403


async def test_device_detail_404_for_foreign_org(api_env) -> None:
    org_id = await api_env.create_org("api")
    other_org = await api_env.create_org("api-other")
    foreign_device = await _add_device(api_env, other_org, "foreign")
    reader = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, reader)
    response = await api_env.client.get(f"/api/v1/devices/{foreign_device}", headers=headers)
    assert response.status_code == 404


async def test_job_dispatch_creates_signed_job_and_publishes(api_env) -> None:
    org_id = await api_env.create_org("api")
    device_id = await _add_device(api_env, org_id)
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)

    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.psubscribe(f"dispatch:{device_id}")

    response = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "hello", "device_id": str(device_id), "params": {"message": "yo"}},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "queued"
    assert job["type"] == "hello"

    published = None
    for _ in range(30):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message and message["type"] == "pmessage":
            published = json.loads(message["data"])
            break
        await asyncio.sleep(0.05)
    await redis.aclose()
    assert published is not None and published["job"]["id"] == job["id"]
    assert published["sig"]

    # read-only cannot dispatch
    reader = await api_env.create_user(org_id, "read-only")
    ro_headers = await api_env.headers(org_id, reader)
    refused = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "hello", "device_id": str(device_id), "params": {}},
        headers=ro_headers,
    )
    assert refused.status_code == 403

    # unknown job type is a 422
    bad = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "not.a.thing", "device_id": str(device_id), "params": {}},
        headers=headers,
    )
    assert bad.status_code == 422

    # dispatch to a nonexistent device is a 404
    missing = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "hello", "device_id": str(uuid4()), "params": {}},
        headers=headers,
    )
    assert missing.status_code == 404


async def test_job_list_and_detail(api_env) -> None:
    org_id = await api_env.create_org("api")
    device_id = await _add_device(api_env, org_id)
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    created = await api_env.client.post(
        "/api/v1/jobs",
        json={"type": "hello", "device_id": str(device_id), "params": {}},
        headers=headers,
    )
    job_id = created.json()["id"]

    listing = await api_env.client.get("/api/v1/jobs", headers=headers)
    assert listing.status_code == 200
    assert any(j["id"] == job_id for j in listing.json()["items"])

    detail = await api_env.client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["device_id"] == str(device_id)
    # signature is server-internal: never exposed through the API
    assert "signature" not in detail.json()


async def test_audit_read_and_verify(api_env) -> None:
    org_id = await api_env.create_org("api")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    # generate some audited activity
    await api_env.client.post(
        "/api/v1/enrollment-tokens", json={"name": "a", "max_uses": 1}, headers=headers
    )

    auditor = await api_env.create_user(org_id, "auditor")
    audit_headers = await api_env.headers(org_id, auditor)
    events = await api_env.client.get("/api/v1/audit", headers=audit_headers)
    assert events.status_code == 200
    actions = [e["action"] for e in events.json()["items"]]
    assert "enrollment.token_created" in actions

    verify = await api_env.client.get("/api/v1/audit/verify", headers=audit_headers)
    assert verify.status_code == 200
    assert verify.json()["ok"] is True

    # technician lacks audit.read
    tech = await api_env.create_user(org_id, "technician")
    tech_headers = await api_env.headers(org_id, tech)
    assert (await api_env.client.get("/api/v1/audit", headers=tech_headers)).status_code == 403
