"""Patch management REST API tests (docs/modules/patch/spec.md §12).

Exercises server-side RBAC on every route (invariant #6), the signed `patch.scan`
dispatch reaching the agent (via the live redis dispatch channel, mirroring
``tests/test_api_routes.py``'s ``test_job_dispatch_creates_signed_job_and_publishes``),
and the single-org-default policy upsert (mirroring ``tests/test_remote_api.py``).
Uses the ``api_env`` in-process ASGI harness.
"""

import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Device,
    DeviceGroup,
    DevicePatchStatus,
    Job,
    Patch,
    PatchApproval,
    PatchPolicy,
    User,
)
from app.modules.patch.approve import (
    UnknownPatchError,
    approve_patch,
    approve_patches,
    list_approvals,
    revoke_approval,
)
from app.modules.patch.policy import resolve_ring_populations

REDIS_URL = "redis://localhost:6379/9"


async def _device(api_env, org_id: UUID, hostname: str = "wks-patch") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        dev = Device(
            org_id=org_id, hostname=hostname, os_family="windows", client_id=org_id, site_id=org_id
        )
        s.add(dev)
        await s.commit()
        return dev.id


async def _patch(
    api_env, org_id: UUID, identifier: str = "KB123", severity: str = "critical"
) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        p = Patch(
            org_id=org_id,
            os_family="windows",
            source="wsus",
            identifier=identifier,
            title="Some update",
            severity=severity,
        )
        s.add(p)
        await s.commit()
        return p.id


# --------------------------------------------------------------------------- #
# GET /patches
# --------------------------------------------------------------------------- #


async def test_list_patches_allowed_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("patch")
    ro = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, ro)
    resp = await api_env.client.get("/api/v1/patch/patches", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body and "total" in body


# --------------------------------------------------------------------------- #
# POST /approvals, DELETE /approvals/{id}
# --------------------------------------------------------------------------- #


async def test_create_approval_forbidden_for_technician_ok_for_org_admin(api_env) -> None:
    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")
    admin = await api_env.create_user(org_id, "org-admin")
    patch_id = await _patch(api_env, org_id)

    forbidden = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(patch_id), "ring_name": "broad"},
        headers=await api_env.headers(org_id, tech),
    )
    assert forbidden.status_code == 403

    ok = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(patch_id), "ring_name": "broad"},
        headers=await api_env.headers(org_id, admin),
    )
    assert ok.status_code in (200, 201), ok.text
    body = ok.json()
    assert body["patch_id"] == str(patch_id)
    assert body["ring_name"] == "broad"
    assert body["revoked"] is False


async def test_revoke_approval_not_found(api_env) -> None:
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.delete(
        f"/api/v1/patch/approvals/{uuid4()}", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# POST /apply
# --------------------------------------------------------------------------- #


async def test_apply_allowed_for_technician(api_env) -> None:
    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")
    resp = await api_env.client.post(
        "/api/v1/patch/apply", json={}, headers=await api_env.headers(org_id, tech)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_ids"] == []


async def test_apply_forbidden_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("patch")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.post(
        "/api/v1/patch/apply", json={}, headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 403


async def test_apply_audits_dispatching_user_not_system(api_env) -> None:
    """POST /apply is a human action; the immutable audit log must attribute
    the dispatch to the calling user, not the worker sweep's system actor."""
    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env, org_id)
    patch_id = await _patch(api_env, org_id)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        expected_user_id = (
            await s.execute(
                select(User.id).where(User.org_id == org_id, User.external_subject == tech)
            )
        ).scalar_one()

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        session.add(
            DevicePatchStatus(
                org_id=org_id, device_id=device_id, patch_id=patch_id, state="approved"
            )
        )
        session.add(PatchApproval(org_id=org_id, patch_id=patch_id, ring_name="broad"))
        await session.flush()

    resp = await api_env.client.post(
        "/api/v1/patch/apply", json={}, headers=await api_env.headers(org_id, tech)
    )
    assert resp.status_code == 200, resp.text
    job_ids = resp.json()["job_ids"]
    assert len(job_ids) >= 1

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "patch.apply_dispatched")
            )
        ).scalar_one()
    assert event.actor_type == "user"
    assert event.actor_id == expected_user_id


# --------------------------------------------------------------------------- #
# POST /scan
# --------------------------------------------------------------------------- #


async def test_scan_dispatches_signed_job(api_env) -> None:
    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env, org_id)
    headers = await api_env.headers(org_id, tech)

    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.psubscribe(f"dispatch:{device_id}")

    resp = await api_env.client.post(
        "/api/v1/patch/scan",
        json={"device_ids": [str(device_id)]},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
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
    assert published is not None, "expected a patch.scan job to be published"
    assert published["job"]["id"] == job_ids[0]
    assert published["job"]["type"] == "patch.scan"
    assert published["sig"]


async def test_an_untargeted_scan_passes_over_devices_waiting_for_review(api_env) -> None:
    """Final review I2. "Scan every device" must not 409 because the org has a
    machine in *Waiting for review* (or a rejected one being removed): the
    untargeted selection is review-aware, like the automatic sweeps (plan D4).
    A device NAMED in the request still meets the gate's 409."""
    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    approved = await _device(api_env, org_id, hostname="approved-01")
    held: dict[str, UUID] = {}
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        for state in ("pending", "rejected"):
            dev = Device(
    org_id=org_id,
    hostname=f"{state}-01",
    os_family="windows",
    review_state=state,
    client_id=org_id,
    site_id=org_id,
            )
            s.add(dev)
            await s.commit()
            held[state] = dev.id

    resp = await api_env.client.post("/api/v1/patch/scan", json={}, headers=headers)
    assert resp.status_code == 202, resp.text
    job_ids = resp.json()["job_ids"]
    assert len(job_ids) == 1
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        target = (
            await session.execute(select(Job.target_id).where(Job.id == UUID(job_ids[0])))
        ).scalar_one()
    assert target == approved

    named = await api_env.client.post(
        "/api/v1/patch/scan", json={"device_ids": [str(held["pending"])]}, headers=headers
    )
    assert named.status_code == 409, named.text


async def test_scan_forbidden_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("patch")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.post(
        "/api/v1/patch/scan", json={}, headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# GET|PUT /policies
# --------------------------------------------------------------------------- #


async def test_policies_get_requires_read(api_env) -> None:
    org_id = await api_env.create_org("patch")
    nobody = await api_env.create_user(org_id)
    resp = await api_env.client.get(
        "/api/v1/patch/policies", headers=await api_env.headers(org_id, nobody)
    )
    assert resp.status_code == 403


async def test_policies_put_requires_manage(api_env) -> None:
    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")  # patch.read, not policy.manage
    resp = await api_env.client.put(
        "/api/v1/patch/policies", json={}, headers=await api_env.headers(org_id, tech)
    )
    assert resp.status_code == 403


async def test_policies_put_upserts_single_org_default(api_env) -> None:
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    r1 = await api_env.client.put(
        "/api/v1/patch/policies",
        json={"ring_name": "fast", "scan_interval_hours": 12},
        headers=headers,
    )
    assert r1.status_code == 200, r1.text
    r2 = await api_env.client.put(
        "/api/v1/patch/policies",
        json={"ring_name": "broad", "scan_interval_hours": 48},
        headers=headers,
    )
    assert r2.status_code == 200
    # the org-default row was UPDATED in place, not duplicated
    assert r1.json()["id"] == r2.json()["id"]

    lst = await api_env.client.get("/api/v1/patch/policies", headers=headers)
    assert lst.status_code == 200
    defaults = [p for p in lst.json() if p["device_group_id"] is None]
    assert len(defaults) == 1
    assert defaults[0]["ring_name"] == "broad"
    assert defaults[0]["scan_interval_hours"] == 48


async def test_policies_put_refuses_a_reboot_mode_the_agent_cannot_name(api_env) -> None:
    """`always` was offered by the console and stored; every apply for the
    policy then failed PatchApplyParams validation inside the sweep."""
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    for body in ({"reboot": "always"}, {"reboot_deferral_hours": -1}):
        resp = await api_env.client.put("/api/v1/patch/policies", json=body, headers=headers)
        assert resp.status_code == 422, (body, resp.text)
    ok = await api_env.client.put(
        "/api/v1/patch/policies", json={"reboot": "forced"}, headers=headers
    )
    assert ok.status_code == 200


# --------------------------------------------------------------------------- #
# GET /report/export
# --------------------------------------------------------------------------- #


async def test_report_export_streams_and_audits(api_env) -> None:
    org_id = await api_env.create_org("patch")
    ro = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, ro)

    resp = await api_env.client.get("/api/v1/patch/report/export", headers=headers)
    assert resp.status_code == 200, resp.text

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        actions = {
            a.action
            for a in (
                await s.execute(select(AuditEvent).where(AuditEvent.org_id == org_id))
            ).scalars()
        }
    assert "patch.report_exported" in actions


async def test_report_export_escapes_free_text_columns_but_not_enums_or_ids(api_env) -> None:
    """`hostname`/`identifier`/`kb`/`cve`/`source`/`last_error` are all
    unconstrained free text (`app.modules.patch.schema.PatchScanDoc`:
    `identifier`/`kb`/`cve`/`source` carry no `Literal`/pattern constraint,
    and `last_error` is a free-form agent-reported error string) -- each is
    a formula-injection candidate straight off a vendor patch feed or a
    compromised device. `os_family`/`severity` are `Literal`-constrained and
    `state` is set from a fixed internal vocabulary
    (`missing`/`approved`/`installed`/`failed`), never raw agent text, so
    none of those three should be escaped."""
    import csv
    import io

    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    device_id = await _device(api_env, org_id, hostname="=cmd|' /C calc'!A1")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        patch = Patch(
            org_id=org_id,
            os_family="windows",
            source="-wsus",
            identifier="@KB000-evil",
            title="Some update",
            severity="critical",
            kb="+KB999",
            cve=["-CVE-EVIL"],
        )
        session.add(patch)
        await session.flush()
        session.add(
            DevicePatchStatus(
                org_id=org_id,
                device_id=device_id,
                patch_id=patch.id,
                state="failed",
                last_error="=cmd|/c calc",
            )
        )
        await session.commit()
        patch_id = patch.id

    resp = await api_env.client.get(
        "/api/v1/patch/report/export", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    rows = list(csv.reader(io.StringIO(resp.text)))
    header = rows[0]
    row = next(r for r in rows[1:] if r[header.index("patch_id")] == str(patch_id))

    assert row[header.index("hostname")] == "'=cmd|' /C calc'!A1"
    assert row[header.index("identifier")] == "'@KB000-evil"
    assert row[header.index("kb")] == "'+KB999"
    assert row[header.index("cve")] == "'-CVE-EVIL"
    assert row[header.index("source")] == "'-wsus"
    assert row[header.index("last_error")] == "'=cmd|/c calc"
    # Enum/id/UUID columns: never escaped.
    assert row[header.index("os_family")] == "windows"
    assert row[header.index("severity")] == "critical"
    assert row[header.index("state")] == "failed"


async def test_report_fleet_allowed_for_auditor(api_env) -> None:
    org_id = await api_env.create_org("patch")
    aud = await api_env.create_user(org_id, "auditor")
    resp = await api_env.client.get(
        "/api/v1/patch/report", headers=await api_env.headers(org_id, aud)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "compliant_pct_by_os_family" in body
    # `exposures`, not `exposed_devices`: one entry per (device, patch)
    # finding, renamed on 2026-08-20 because the old name was read as a device
    # list by the console and by everyone else.
    assert "exposures" in body
    assert "exposed_device_count" in body
    assert "critical_device_count" in body


async def test_device_status_requires_read_and_404_for_unknown_device(api_env) -> None:
    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env, org_id)
    headers = await api_env.headers(org_id, tech)

    resp = await api_env.client.get(
        f"/api/v1/patch/devices/{device_id}/status", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device_id"] == str(device_id)
    assert "counts_by_state" in body

    missing = await api_env.client.get(
        f"/api/v1/patch/devices/{uuid4()}/status", headers=headers
    )
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# GET /approvals -- the route that makes DELETE /approvals/{id} reachable
# (audit 2026-08-21, recommendation 4).
# --------------------------------------------------------------------------- #


async def test_list_approvals_takes_the_read_permission_not_the_approve_one(
    api_env,
) -> None:
    """Reading which patches are approved is read work. `technician` holds
    `patch.read` and NOT `patch.approve` (migration 0011), so it is the
    negative control: it must be able to LIST and still be refused the
    revoke below it."""
    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")
    nobody = await api_env.create_user(org_id)

    refused = await api_env.client.get(
        "/api/v1/patch/approvals", headers=await api_env.headers(org_id, nobody)
    )
    assert refused.status_code == 403

    allowed = await api_env.client.get(
        "/api/v1/patch/approvals", headers=await api_env.headers(org_id, tech)
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"items": [], "total": 0}


async def test_listing_an_approval_yields_the_id_that_revokes_it(api_env) -> None:
    """The whole point of the route. Before it existed, `DELETE
    /approvals/{id}` was unreachable over HTTP: nothing returned an approval
    id except the POST that created it, so an approval made in a previous
    session could never be revoked. This walks create -> list -> revoke using
    ONLY the id the list produced."""
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    patch_id = await _patch(api_env, org_id, identifier="KB-REVOKE")

    created = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(patch_id), "ring_name": "broad"},
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text

    listed = await api_env.client.get("/api/v1/patch/approvals", headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["patch_id"] == str(patch_id)
    assert row["ring_name"] == "broad"
    assert row["revoked"] is False

    revoked = await api_env.client.delete(
        f"/api/v1/patch/approvals/{row['id']}", headers=headers
    )
    assert revoked.status_code == 204, revoked.text

    after = await api_env.client.get("/api/v1/patch/approvals", headers=headers)
    assert after.json()["items"][0]["revoked"] is True


async def test_list_approvals_refuses_the_revoke_it_makes_reachable(api_env) -> None:
    """A read permission must not become a write one by proximity: the same
    technician who can list must still be refused the DELETE."""
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    patch_id = await _patch(api_env, org_id, identifier="KB-RBAC")

    created = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(patch_id), "ring_name": "broad"},
        headers=await api_env.headers(org_id, admin),
    )
    approval_id = created.json()["id"]

    refused = await api_env.client.delete(
        f"/api/v1/patch/approvals/{approval_id}",
        headers=await api_env.headers(org_id, tech),
    )
    assert refused.status_code == 403


async def test_list_approvals_never_shows_another_orgs_approval(api_env) -> None:
    """Tenancy (invariant #5). An approval is the gate that lets a patch reach
    a machine; leaking another tenant's approval ids would hand a caller the
    exact input `DELETE /approvals/{id}` takes. The org-admin here holds every
    patch permission there is, so nothing but the tenant scope is stopping
    them."""
    org_id = await api_env.create_org("patch-mine")
    other_org = await api_env.create_org("patch-theirs")
    mine = await api_env.create_user(org_id, "org-admin")
    theirs = await api_env.create_user(other_org, "org-admin")

    my_patch = await _patch(api_env, org_id, identifier="KB-MINE")
    their_patch = await _patch(api_env, other_org, identifier="KB-THEIRS")
    mine_created = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(my_patch), "ring_name": "broad"},
        headers=await api_env.headers(org_id, mine),
    )
    theirs_created = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(their_patch), "ring_name": "broad"},
        headers=await api_env.headers(other_org, theirs),
    )
    assert mine_created.status_code in (200, 201), mine_created.text
    assert theirs_created.status_code in (200, 201), theirs_created.text
    their_approval_id = theirs_created.json()["id"]

    listed = await api_env.client.get(
        "/api/v1/patch/approvals", headers=await api_env.headers(org_id, mine)
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    ids = {row["id"] for row in body["items"]}
    assert ids == {mine_created.json()["id"]}
    assert their_approval_id not in ids
    assert body["total"] == 1, "the count must be tenant-scoped too, not just the page"

    # ...and knowing the foreign id anyway does not revoke it.
    assert (
        await api_env.client.delete(
            f"/api/v1/patch/approvals/{their_approval_id}",
            headers=await api_env.headers(org_id, mine),
        )
    ).status_code == 404


async def test_the_approval_list_names_the_patch_it_clears(api_env) -> None:
    """`patch_identifier` ON THE ROW, and the reason it had to be.

    An approval is keyed by `patch_id` and carried nothing else, so the console
    could not print what an approval was FOR without a second request. It
    solved that by fetching a 500-row page of `GET /patch/patches` on every
    visit purely to build an id -> identifier map -- five hundred rows to
    render a handful, and silently wrong past the cap: an approval whose patch
    fell outside the page rendered as a raw uuid.

    THE JOIN MUST NOT WIDEN THE ROW. It adds ONE field. Nothing else about the
    patch -- title, severity, CVE list -- crosses onto an approval, because
    `GET /approvals` is `patch.read` for the approval and is not a second,
    unpaged catalogue route wearing a different name.
    """
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    patch_id = await _patch(api_env, org_id, identifier="KB-NAMED")

    created = await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(patch_id), "ring_name": "broad"},
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text
    # The POST answers with the same model, so it names the patch too -- a
    # field that is only populated on one of the two routes returning
    # `ApprovalOut` is a field a caller cannot rely on.
    assert created.json()["patch_identifier"] == "KB-NAMED"

    listed = await api_env.client.get("/api/v1/patch/approvals", headers=headers)
    assert listed.status_code == 200, listed.text
    row = listed.json()["items"][0]
    assert row["patch_identifier"] == "KB-NAMED"
    assert row["patch_id"] == str(patch_id)
    assert set(row) == {
        "id",
        "patch_id",
        "patch_identifier",
        "ring_name",
        "approved_by",
        "approved_at",
        "revoked",
        "revoked_by",
        "revoked_at",
    }, "the join widened the approval row"


async def test_the_bulk_approval_response_names_every_patch(api_env) -> None:
    """Same model, same field, on the route the console approves from."""
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    first = await _patch(api_env, org_id, identifier="KB-BULK-1")
    second = await _patch(api_env, org_id, identifier="KB-BULK-2")

    resp = await api_env.client.post(
        "/api/v1/patch/approvals/bulk",
        json={"patch_ids": [str(first), str(second)], "ring_name": "broad"},
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text
    assert [row["patch_identifier"] for row in resp.json()["items"]] == [
        "KB-BULK-1",
        "KB-BULK-2",
    ]


async def test_list_approvals_filters_by_ring_patch_and_revoked_state(api_env) -> None:
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    p_one = await _patch(api_env, org_id, identifier="KB-ONE")
    p_two = await _patch(api_env, org_id, identifier="KB-TWO")

    for patch_id, ring in ((p_one, "test"), (p_one, "broad"), (p_two, "broad")):
        resp = await api_env.client.post(
            "/api/v1/patch/approvals",
            json={"patch_id": str(patch_id), "ring_name": ring},
            headers=headers,
        )
        assert resp.status_code in (200, 201), resp.text

    by_ring = await api_env.client.get(
        "/api/v1/patch/approvals", params={"ring": "broad"}, headers=headers
    )
    assert by_ring.json()["total"] == 2
    assert {r["ring_name"] for r in by_ring.json()["items"]} == {"broad"}

    by_patch = await api_env.client.get(
        "/api/v1/patch/approvals", params={"patch_id": str(p_one)}, headers=headers
    )
    assert by_patch.json()["total"] == 2
    assert {r["patch_id"] for r in by_patch.json()["items"]} == {str(p_one)}

    # A revoked approval stays in the list by default -- approvals are soft
    # revoked precisely so the trail holds (spec §5) -- and `revoked=false`
    # is how a console asks for the ones still in force.
    target = by_patch.json()["items"][0]["id"]
    assert (
        await api_env.client.delete(f"/api/v1/patch/approvals/{target}", headers=headers)
    ).status_code == 204

    everything = await api_env.client.get("/api/v1/patch/approvals", headers=headers)
    assert everything.json()["total"] == 3
    active = await api_env.client.get(
        "/api/v1/patch/approvals", params={"revoked": "false"}, headers=headers
    )
    assert active.json()["total"] == 2
    assert target not in {r["id"] for r in active.json()["items"]}
    only_revoked = await api_env.client.get(
        "/api/v1/patch/approvals", params={"revoked": "true"}, headers=headers
    )
    assert [r["id"] for r in only_revoked.json()["items"]] == [target]


# --------------------------------------------------------------------------- #
# GET /report -- last_scan_at (audit 2026-08-21, recommendation 5)
# --------------------------------------------------------------------------- #


async def _succeeded_scan(api_env, org_id: UUID, device_id: UUID, when) -> None:
    from app.models import Job, JobStatus

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(
            Job(
                org_id=org_id,
                type="patch.scan",
                target_type="device",
                target_id=device_id,
                params={},
                nonce=f"n-{uuid4().hex[:8]}",
                issued_at=when,
                expires_at=when,
                status=JobStatus.succeeded,
                finished_at=when,
            )
        )
        await s.commit()


async def test_report_says_never_scanned_rather_than_printing_a_clean_zero(
    api_env,
) -> None:
    """A fleet with one device and no completed scan has zero exposures and
    zero information. The response has to carry both facts, or the console
    tile is the /reports nine-zeros defect again."""
    from datetime import timedelta

    org_id = await api_env.create_org("patch")
    aud = await api_env.create_user(org_id, "auditor")
    headers = await api_env.headers(org_id, aud)
    device_id = await _device(api_env, org_id)

    before = await api_env.client.get("/api/v1/patch/report", headers=headers)
    assert before.status_code == 200, before.text
    body = before.json()
    assert body["exposures"] == []
    assert body["last_scan_at"] is None
    assert body["devices_total"] == 1
    assert body["devices_scanned"] == 0

    when = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    await _succeeded_scan(api_env, org_id, device_id, when)

    after = await api_env.client.get("/api/v1/patch/report", headers=headers)
    body = after.json()
    assert body["exposures"] == [], "still clean -- but now it is a measured clean"
    assert datetime.fromisoformat(body["last_scan_at"]) == when
    assert body["devices_scanned"] == 1
    assert body["devices_total"] == 1


async def test_device_status_carries_that_devices_own_last_scan(api_env) -> None:
    from datetime import timedelta

    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    scanned = await _device(api_env, org_id, hostname="scanned-01")
    unscanned = await _device(api_env, org_id, hostname="unscanned-01")

    when = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
    await _succeeded_scan(api_env, org_id, scanned, when)

    one = await api_env.client.get(
        f"/api/v1/patch/devices/{scanned}/status", headers=headers
    )
    assert one.status_code == 200, one.text
    assert datetime.fromisoformat(one.json()["last_scan_at"]) == when

    other = await api_env.client.get(
        f"/api/v1/patch/devices/{unscanned}/status", headers=headers
    )
    assert other.json()["last_scan_at"] is None


async def test_the_approval_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    api_env, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    `test_list_approvals_never_shows_another_orgs_approval` above drives the
    real routes, which means `org_session` on the NOBYPASSRLS `app_user` role:
    `patch_approval` carries a forced `tenant_isolation` policy, so the
    neighbour's approval is gone before `PatchApproval.org_id == org_id` is
    consulted and that test stays green with the predicate deleted. What it
    pins is the paged envelope's `total` and the 404 -- both worth pinning,
    neither the filter. `its_admin` carries BYPASSRLS (`tests/conftest.py`), so
    here the predicate is the only scoping.

    AN APPROVAL IS THE GATE THAT LETS A PATCH REACH A MACHINE, and `revoke` is
    the destructive half: a missing predicate there does not leak an approval,
    it REVOKES another tenant's, which silently stops that tenant's patch
    rollout with nothing in their own audit trail to explain it. So the revoke
    is asserted from both sides -- refused for the neighbour's id, and the
    neighbour's row read back unchanged.

    FALSIFIED: deleting `PatchApproval.org_id == org_id` from `list_approvals`
    puts org B's approval in org A's page and its `total`; deleting it from
    `revoke_approval` turns the `False` into a revocation of org B's approval.
    """
    org_a = await api_env.create_org("patch-pred-a")
    org_b = await api_env.create_org("patch-pred-b")
    actor_a = await api_env.create_user(org_a, "org-admin")
    actor_b = await api_env.create_user(org_b, "org-admin")
    async with AsyncSession(api_env.admin_engine) as s:
        actor_a_id = (
            await s.execute(select(User.id).where(User.external_subject == actor_a))
        ).scalar_one()
        actor_b_id = (
            await s.execute(select(User.id).where(User.external_subject == actor_b))
        ).scalar_one()

    patch_a = await _patch(api_env, org_a, identifier="KB-PRED-A")
    patch_b = await _patch(api_env, org_b, identifier="KB-PRED-B")

    approvals: dict[str, UUID] = {}
    for key, org_id, actor_id, patch_id in (
        ("a", org_a, actor_a_id, patch_a),
        ("b", org_b, actor_b_id, patch_b),
    ):
        approval = await approve_patch(
            bypassrls_session,
            org_id=org_id,
            actor_id=actor_id,
            patch_id=patch_id,
            ring_name="broad",
        )
        approvals[key] = approval.id
    await bypassrls_session.commit()

    rows_a, total_a = await list_approvals(bypassrls_session, org_id=org_a)
    rows_b, total_b = await list_approvals(bypassrls_session, org_id=org_b)
    assert [r.approval.id for r in rows_a] == [approvals["a"]]
    assert [r.approval.id for r in rows_b] == [approvals["b"]]
    assert [r.patch_identifier for r in rows_a] == ["KB-PRED-A"]
    assert (total_a, total_b) == (1, 1), "the count must be tenant-scoped too, not just the page"

    assert await revoke_approval(
        bypassrls_session, org_id=org_a, actor_id=actor_a_id, approval_id=approvals["b"]
    ) is False
    await bypassrls_session.commit()

    theirs = (
        await bypassrls_session.execute(
            select(PatchApproval).where(PatchApproval.id == approvals["b"])
        )
    ).scalar_one()
    assert theirs.revoked is False, "org A revoked a neighbour tenant's approval"

    # Non-vacuity: org A revoking its OWN approval still works.
    assert await revoke_approval(
        bypassrls_session, org_id=org_a, actor_id=actor_a_id, approval_id=approvals["a"]
    ) is True


async def test_the_approval_name_join_cannot_name_a_neighbours_patch(
    api_env, bypassrls_session: AsyncSession
) -> None:
    """THE ORG PREDICATE ON THE NEW JOIN, and the only place it can fail.

    `list_approvals` now reaches into `patches` for the identifier, and under
    `org_session` that reach is already scoped -- `patches` carries a forced
    `tenant_isolation` policy, so a neighbour's row is gone before any
    predicate of ours is consulted and a route test stays green with the ON
    clause's `Patch.org_id` deleted. `its_admin` carries BYPASSRLS
    (`tests/conftest.py`), so here it is the only scoping there is.

    THE ROW THIS NEEDS IS NOT REACHABLE OVER HTTP, and that is the point.
    `patch_approvals.patch_id` is a foreign key, and PostgreSQL runs
    referential-integrity checks with row security BYPASSED -- so an approval
    naming a neighbour tenant's patch id can exist in the table even though
    `approve_patch` refuses to write one (see `UnknownPatchError`). It is
    written straight in here so the READ path is what is under test.

    WHAT MUST HAPPEN: the row stays in the page -- it is org A's approval, and
    `DELETE /approvals/{id}` still has to be able to reach it -- and its
    identifier is `None`. `None` is not "unknown formatting", it is "this org
    has no patch by that id", which is the anomaly itself becoming visible
    instead of being papered over with a neighbour's KB number.

    FALSIFIED: dropping `Patch.org_id == org_id` from the ON clause makes this
    row report `KB-LEAK-B`, org B's identifier, on org A's page. Turning the
    outer join into an inner one drops the row from the page entirely and takes
    the only handle on it with it.
    """
    org_a = await api_env.create_org("patch-join-a")
    org_b = await api_env.create_org("patch-join-b")
    patch_b = await _patch(api_env, org_b, identifier="KB-LEAK-B")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        row = PatchApproval(org_id=org_a, patch_id=patch_b, ring_name="broad")
        s.add(row)
        await s.commit()
        stray = row.id

    rows, total = await list_approvals(bypassrls_session, org_id=org_a)
    assert [r.approval.id for r in rows] == [stray], (
        "the join dropped an approval instead of failing to name it"
    )
    assert total == 1
    assert rows[0].patch_identifier is None, "org B's identifier is on org A's approval"

    # Non-vacuity: an approval whose patch IS this org's still gets named.
    patch_a = await _patch(api_env, org_a, identifier="KB-JOIN-A")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        s.add(PatchApproval(org_id=org_a, patch_id=patch_a, ring_name="test"))
        await s.commit()
    rows, _ = await list_approvals(bypassrls_session, org_id=org_a, ring_name="test")
    assert [r.patch_identifier for r in rows] == ["KB-JOIN-A"]


# --------------------------------------------------------------------------- #
# POST /approvals/bulk, GET /rings
#
# WHAT THE OPERATOR REPORTED, 2026-08-24: "there are a lot of packages and lines
# and a lot of data just for 1 device, it is super hard to approve all updates
# one by one, there must be a better way to manage it".
#
# The model was never the problem. `PatchApproval` is `(org_id, patch_id,
# ring_name)` -- there is no device column and `approve_patch` takes no device
# id, so ONE approval has always cleared a patch for a whole ring. What the
# console asked for was one POST and one confirmation dialog per catalogue row,
# which turned a fleet-wide decision into a per-row chore. These two routes are
# what let the decision be expressed once: a selection of patches, one ring, and
# a number saying how many machines that ring reaches BEFORE the click.
# --------------------------------------------------------------------------- #


async def test_bulk_approval_forbidden_for_technician_ok_for_org_admin(api_env) -> None:
    """Same gate as the single POST it generalises. `technician` holds
    `patch.read` and not `patch.approve` (migration 0011), so it is the negative
    control -- a batch route must not be a weaker door onto the same act."""
    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")
    admin = await api_env.create_user(org_id, "org-admin")
    patch_id = await _patch(api_env, org_id)

    forbidden = await api_env.client.post(
        "/api/v1/patch/approvals/bulk",
        json={"patch_ids": [str(patch_id)], "ring_name": "pilot"},
        headers=await api_env.headers(org_id, tech),
    )
    assert forbidden.status_code == 403

    ok = await api_env.client.post(
        "/api/v1/patch/approvals/bulk",
        json={"patch_ids": [str(patch_id)], "ring_name": "pilot"},
        headers=await api_env.headers(org_id, admin),
    )
    assert ok.status_code == 201, ok.text


async def test_bulk_approval_answers_one_approval_per_patch_and_counts_the_new_ones(
    api_env,
) -> None:
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    first = await _patch(api_env, org_id, identifier="KB-BULK-1")
    second = await _patch(api_env, org_id, identifier="KB-BULK-2")

    await api_env.client.post(
        "/api/v1/patch/approvals",
        json={"patch_id": str(first), "ring_name": "pilot"},
        headers=headers,
    )
    resp = await api_env.client.post(
        "/api/v1/patch/approvals/bulk",
        json={"patch_ids": [str(first), str(second)], "ring_name": "pilot"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total"] == 2 and len(body["items"]) == 2
    assert body["newly_approved"] == 1
    assert body["already_in_force"] == 1
    assert {item["patch_id"] for item in body["items"]} == {str(first), str(second)}
    assert all(item["ring_name"] == "pilot" for item in body["items"])
    assert all(item["revoked"] is False for item in body["items"])


async def test_bulk_approval_writes_one_audit_event_per_patch(api_env) -> None:
    """INVARIANT #4 through the ROUTE, not just the service: a bulk action must
    leave the log able to answer "who cleared this patch", one row at a time."""
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    ids = [
        await _patch(api_env, org_id, identifier=f"KB-AUD-{n}") for n in range(3)
    ]
    resp = await api_env.client.post(
        "/api/v1/patch/approvals/bulk",
        json={"patch_ids": [str(i) for i in ids], "ring_name": "test"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 201, resp.text

    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "patch.approved"
                )
            )
        ).scalars().all()
    assert len(events) == 3
    assert {e.target_id for e in events} == {str(i) for i in ids}


async def test_bulk_approval_404s_on_a_patch_id_from_another_org(api_env) -> None:
    """And writes nothing at all -- the legitimate ids in the same batch do not
    land either. A partially-applied batch is a clearance the operator never
    consciously granted."""
    org_id = await api_env.create_org("patch-a")
    other_org_id = await api_env.create_org("patch-b")
    admin = await api_env.create_user(org_id, "org-admin")
    mine = await _patch(api_env, org_id, identifier="KB-MINE")
    theirs = await _patch(api_env, other_org_id, identifier="KB-THEIRS")

    resp = await api_env.client.post(
        "/api/v1/patch/approvals/bulk",
        json={"patch_ids": [str(mine), str(theirs)], "ring_name": "pilot"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 404, resp.text

    async with AsyncSession(api_env.admin_engine) as session:
        approvals = (
            await session.execute(
                select(PatchApproval).where(PatchApproval.org_id == org_id)
            )
        ).scalars().all()
    assert approvals == [], "a refused batch still wrote part of itself"


async def test_bulk_approval_refuses_an_empty_selection_rather_than_answering_ok(
    api_env,
) -> None:
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.post(
        "/api/v1/patch/approvals/bulk",
        json={"patch_ids": [], "ring_name": "pilot"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 422


async def test_rings_says_how_many_devices_each_ring_reaches(api_env) -> None:
    """THE BLAST RADIUS, BEFORE THE CLICK. "Approve 42 updates for pilot" is
    unreadable without the number of machines "pilot" means, and that number is
    not derivable in the browser: it needs `patch_policies` joined to `devices`
    through `device_groups`, which is `resolve_patch_policy`'s job."""
    org_id = await api_env.create_org("patch")
    admin = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        group = DeviceGroup(org_id=org_id, name="pilot-machines", client_id=org_id)
        session.add(group)
        await session.flush()
        session.add(
            PatchPolicy(
                org_id=org_id, device_group_id=None, ring_name="broad", ring_order=100
            )
        )
        session.add(
            PatchPolicy(
                org_id=org_id, device_group_id=group.id, ring_name="pilot", ring_order=50
            )
        )
        session.add_all(
            [
                Device(org_id=org_id, hostname="p1", os_family="windows",
                       device_group_id=group.id,
                       client_id=org_id,
                       site_id=org_id),
                Device(org_id=org_id, hostname="p2", os_family="windows",
                       device_group_id=group.id,
                       client_id=org_id,
                       site_id=org_id),
                Device(
                    org_id=org_id,
                    hostname="b1",
                    os_family="linux",
                    client_id=org_id,
                    site_id=org_id,
                ),
            ]
        )
        await session.commit()

    resp = await api_env.client.get(
        "/api/v1/patch/rings", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device_total"] == 3
    assert body["total"] == len(body["items"]) == 2
    counts = {item["ring_name"]: item["device_count"] for item in body["items"]}
    assert counts == {"pilot": 2, "broad": 1}
    # Rollout order, so the console offers test before broad rather than
    # alphabetically.
    assert [item["ring_name"] for item in body["items"]] == ["pilot", "broad"]
    assert sum(counts.values()) == body["device_total"], (
        "the rings must partition the fleet or the numbers cannot be read"
    )


async def test_rings_takes_the_read_permission_not_the_approve_one(api_env) -> None:
    """Reading which ring a machine is in is read work, and the technician who
    may see it is still refused the approval it informs."""
    org_id = await api_env.create_org("patch")
    tech = await api_env.create_user(org_id, "technician")
    resp = await api_env.client.get(
        "/api/v1/patch/rings", headers=await api_env.headers(org_id, tech)
    )
    assert resp.status_code == 200, resp.text


async def test_rings_never_counts_another_orgs_devices(api_env) -> None:
    org_id = await api_env.create_org("rings-a")
    other = await api_env.create_org("rings-b")
    admin = await api_env.create_user(org_id, "org-admin")
    await _device(api_env, org_id, hostname="mine")
    await _device(api_env, other, hostname="theirs-1")
    await _device(api_env, other, hostname="theirs-2")

    resp = await api_env.client.get(
        "/api/v1/patch/rings", headers=await api_env.headers(org_id, admin)
    )
    assert resp.json()["device_total"] == 1


async def test_the_bulk_approval_and_ring_predicates_hold_where_rls_is_not_rejecting(
    api_env, bypassrls_session: AsyncSession
) -> None:
    """THE TEST HERE THAT CAN ACTUALLY FAIL, for the reason
    `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` records: the
    route tests above run through `org_session` on the NOBYPASSRLS `app_user`
    role, so Postgres removes the neighbour's rows before either predicate is
    consulted and deleting a `WHERE org_id` turns NONE of them red. `its_admin`
    carries BYPASSRLS (`tests/conftest.py`), so here the predicates are the only
    scoping there is.

    BOTH HALVES MATTER AND THEY FAIL DIFFERENTLY. Without its predicate
    `approve_patches` would accept a neighbour tenant's patch id and mint a
    clearance against it; without its predicate `resolve_ring_populations` would
    count the neighbour's machines into this org's blast radius, which is the
    number an operator reads immediately before authorising an install.

    FALSIFIED: deleting `Patch.org_id == org_id` from `_assert_patches_in_org`
    turns the `UnknownPatchError` into a written approval; deleting
    `Device.org_id == org_id` from `resolve_ring_populations` makes the
    device_ids tuple carry org B's machine.
    """
    org_a = await api_env.create_org("patch-bulk-a")
    org_b = await api_env.create_org("patch-bulk-b")
    actor_a = await api_env.create_user(org_a, "org-admin")
    async with AsyncSession(api_env.admin_engine) as s:
        actor_a_id = (
            await s.execute(select(User.id).where(User.external_subject == actor_a))
        ).scalar_one()

    patch_a = await _patch(api_env, org_a, identifier="KB-BULK-PRED-A")
    patch_b = await _patch(api_env, org_b, identifier="KB-BULK-PRED-B")
    device_a = await _device(api_env, org_a, hostname="bulk-pred-a")
    await _device(api_env, org_b, hostname="bulk-pred-b1")
    await _device(api_env, org_b, hostname="bulk-pred-b2")

    with pytest.raises(UnknownPatchError) as raised:
        await approve_patches(
            bypassrls_session,
            org_id=org_a,
            actor_id=actor_a_id,
            patch_ids=[patch_a, patch_b],
            ring_name="broad",
        )
    assert raised.value.patch_ids == [patch_b]
    await bypassrls_session.rollback()

    populations = await resolve_ring_populations(bypassrls_session, org_a)
    assert [(p.ring_name, p.device_ids) for p in populations] == [("broad", (device_a,))]

    # Non-vacuity: org B's own resolution still sees org B's two machines, so
    # the assertion above cannot pass by returning nothing at all.
    theirs = await resolve_ring_populations(bypassrls_session, org_b)
    assert sum(len(p.device_ids) for p in theirs) == 2
