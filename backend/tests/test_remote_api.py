"""Remote-control REST API tests (docs/modules/remote-control/spec.md §11).

Exercises server-side RBAC on every route, the no-secret/no-token response
contract, the single-org-default policy upsert, and the recording read path
(403 for operators, 404 when absent, 200 + audit when present). Uses the
``api_env`` in-process ASGI harness like ``test_enrollment.py``.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from moto import mock_aws
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.models import Agent, Device, RemoteSession, User


async def _device(api_env, org_id: UUID) -> UUID:
    """A device whose agent can share its screen (the only session type)."""
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        dev = Device(
            org_id=org_id, hostname="wks-rc", os_family="windows", client_id=org_id, site_id=org_id
        )
        s.add(dev)
        await s.flush()
        s.add(
            Agent(
                org_id=org_id,
                device_id=dev.id,
                cert_fingerprint=uuid4().hex + uuid4().hex,
                cert_serial=uuid4().hex,
                cert_not_after=datetime.now(UTC) + timedelta(days=30),
                enrolled_at=datetime.now(UTC),
                capabilities={"remote.screen.open": True},
            )
        )
        await s.commit()
        return dev.id


async def _user_id(api_env, org_id: UUID) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        usr = User(org_id=org_id, external_subject=f"t-{uuid4().hex[:8]}", email="t@x.y")
        s.add(usr)
        await s.commit()
        return usr.id


# --------------------------------------------------------------------------- #
# POST /sessions
# --------------------------------------------------------------------------- #


async def test_start_session_requires_permission(api_env) -> None:
    org_id = await api_env.create_org("rc")
    ro = await api_env.create_user(org_id, "read-only")
    headers = await api_env.headers(org_id, ro)
    resp = await api_env.client.post(
        "/api/v1/remote/sessions",
        json={"device_id": str(uuid4()), "reason": "x"},
        headers=headers,
    )
    assert resp.status_code == 403


async def test_start_session_unknown_device_404(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    resp = await api_env.client.post(
        "/api/v1/remote/sessions",
        json={"device_id": str(uuid4()), "reason": "x"},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_start_session_creates_and_dispatches(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env, org_id)
    headers = await api_env.headers(org_id, tech)
    resp = await api_env.client.post(
        "/api/v1/remote/sessions",
        json={"device_id": str(device_id), "reason": "helpdesk"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "awaiting_consent"
    assert body["consent_mode"] == "attended"
    # the response never leaks the relay token or any secret material
    assert "relay_token" not in body
    assert "secret" not in body
    UUID(body["session_id"])  # a real session was created


# --------------------------------------------------------------------------- #
# GET /sessions (+ detail), read permission is remote.control OR recordings.read
# --------------------------------------------------------------------------- #


async def test_list_sessions_forbidden_for_readonly(api_env) -> None:
    org_id = await api_env.create_org("rc")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/remote/sessions", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 403


async def test_create_then_list_and_get_as_auditor(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")
    aud = await api_env.create_user(org_id, "auditor")
    device_id = await _device(api_env, org_id)
    created = await api_env.client.post(
        "/api/v1/remote/sessions",
        json={"device_id": str(device_id), "reason": "look"},
        headers=await api_env.headers(org_id, tech),
    )
    sid = created.json()["session_id"]

    aud_headers = await api_env.headers(org_id, aud)
    lst = await api_env.client.get("/api/v1/remote/sessions", headers=aud_headers)
    assert lst.status_code == 200
    payload = lst.json()
    assert payload["total"] >= 1
    assert any(item["id"] == sid for item in payload["items"])

    detail = await api_env.client.get(f"/api/v1/remote/sessions/{sid}", headers=aud_headers)
    assert detail.status_code == 200
    dbody = detail.json()
    assert dbody["state"] == "awaiting_consent"
    assert "relay_token" not in dbody and "secret" not in dbody

    missing = await api_env.client.get(
        f"/api/v1/remote/sessions/{uuid4()}", headers=aud_headers
    )
    assert missing.status_code == 404


async def test_list_filters_by_device(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    dev_a = await _device(api_env, org_id)
    dev_b = await _device(api_env, org_id)
    for dev in (dev_a, dev_b):
        await api_env.client.post(
            "/api/v1/remote/sessions",
            json={"device_id": str(dev), "reason": "r"},
            headers=headers,
        )
    resp = await api_env.client.get(
        "/api/v1/remote/sessions", params={"device_id": str(dev_a)}, headers=headers
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and all(i["device_id"] == str(dev_a) for i in items)


# --------------------------------------------------------------------------- #
# POST /sessions/{id}/stop
# --------------------------------------------------------------------------- #


async def test_stop_session(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, tech)
    device_id = await _device(api_env, org_id)
    sid = (
        await api_env.client.post(
            "/api/v1/remote/sessions",
            json={"device_id": str(device_id), "reason": "r"},
            headers=headers,
        )
    ).json()["session_id"]

    stopped = await api_env.client.post(
        f"/api/v1/remote/sessions/{sid}/stop", headers=headers
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "ended"
    assert stopped.json()["end_reason"] == "completed"

    # stopping an already-ended session is a conflict, not a silent re-end
    again = await api_env.client.post(
        f"/api/v1/remote/sessions/{sid}/stop", headers=headers
    )
    assert again.status_code == 409

    missing = await api_env.client.post(
        f"/api/v1/remote/sessions/{uuid4()}/stop", headers=headers
    )
    assert missing.status_code == 404


async def test_stop_session_requires_owner_or_admin(api_env) -> None:
    """Spec §11 (phase 3 design §5.3): stop is allowed only for the session
    OWNER or a holder of ``remote.supervise`` on its client. A second
    technician who holds ``remote.control`` but neither owns the session nor
    holds ``remote.supervise`` gets 403; the owner and an org-admin
    (``remote.supervise`` by migration 0244) succeed."""
    org_id = await api_env.create_org("rc")
    owner = await api_env.create_user(org_id, "technician")
    other = await api_env.create_user(org_id, "technician")  # remote.control, not owner
    admin = await api_env.create_user(org_id, "org-admin")  # remote.supervise (migration 0244)
    device_id = await _device(api_env, org_id)

    owner_headers = await api_env.headers(org_id, owner)

    async def _new_session() -> str:
        return (
            await api_env.client.post(
                "/api/v1/remote/sessions",
                json={"device_id": str(device_id), "reason": "r"},
                headers=owner_headers,
            )
        ).json()["session_id"]

    # A non-owner technician (no remote.supervise) may not stop another's session.
    sid_a = await _new_session()
    forbidden = await api_env.client.post(
        f"/api/v1/remote/sessions/{sid_a}/stop",
        headers=await api_env.headers(org_id, other),
    )
    assert forbidden.status_code == 403
    # ...and the 403 did not change the session state.
    still = await api_env.client.get(
        f"/api/v1/remote/sessions/{sid_a}", headers=owner_headers
    )
    assert still.json()["state"] == "awaiting_consent"

    # The owner can stop their own session.
    owned = await api_env.client.post(
        f"/api/v1/remote/sessions/{sid_a}/stop", headers=owner_headers
    )
    assert owned.status_code == 200, owned.text
    assert owned.json()["state"] == "ended"

    # An org-admin (remote.supervise, by migration 0244) can stop someone else's session.
    sid_b = await _new_session()
    by_admin = await api_env.client.post(
        f"/api/v1/remote/sessions/{sid_b}/stop",
        headers=await api_env.headers(org_id, admin),
    )
    assert by_admin.status_code == 200, by_admin.text
    assert by_admin.json()["state"] == "ended"


# --------------------------------------------------------------------------- #
# GET /sessions/{id}/recording
# --------------------------------------------------------------------------- #


async def test_recording_read_forbidden_for_technician_but_404_for_auditor(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")
    aud = await api_env.create_user(org_id, "auditor")
    sid = uuid4()
    assert (
        await api_env.client.get(
            f"/api/v1/remote/sessions/{sid}/recording",
            headers=await api_env.headers(org_id, tech),
        )
    ).status_code == 403
    assert (
        await api_env.client.get(
            f"/api/v1/remote/sessions/{sid}/recording",
            headers=await api_env.headers(org_id, aud),
        )
    ).status_code == 404


async def test_recording_download_ok_and_audited(api_env) -> None:
    from sqlalchemy import select

    from app.core.storage import ObjectStore
    from app.core.tenancy import org_session
    from app.models import AuditEvent

    org_id = await api_env.create_org("rc")
    aud = await api_env.create_user(org_id, "auditor")
    device_id = await _device(api_env, org_id)
    technician_id = await _user_id(api_env, org_id)
    sid = uuid4()

    with mock_aws():
        store = ObjectStore(api_env.settings)
        ref = await store.put_remote_recording(org_id, sid, b"GUAC-RECORDING-BYTES")
        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
            s.add(
                RemoteSession(
                    id=sid,
                    org_id=org_id,
                    device_id=device_id,
                    technician_id=technician_id,
                    protocol="rdp",
                    consent_mode="attended",
                    state="ended",
                    recording_ref=ref,
                    recording_bytes=len(b"GUAC-RECORDING-BYTES"),
                )
            )
            await s.commit()
        # pin the moto-backed store so the lazy builder doesn't reconstruct it
        api_env.app.state.object_store = store
        resp = await api_env.client.get(
            f"/api/v1/remote/sessions/{sid}/recording",
            headers=await api_env.headers(org_id, aud),
        )
    assert resp.status_code == 200, resp.text
    assert resp.content == b"GUAC-RECORDING-BYTES"

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as s:
        actions = {
            a.action
            for a in (
                await s.execute(select(AuditEvent).where(AuditEvent.org_id == org_id))
            ).scalars()
        }
    assert "remote.recording_accessed" in actions


# --------------------------------------------------------------------------- #
# GET|PUT /policies
# --------------------------------------------------------------------------- #


async def test_policies_get_requires_control(api_env) -> None:
    org_id = await api_env.create_org("rc")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/remote/policies", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 403


async def test_policies_put_requires_manage(api_env) -> None:
    org_id = await api_env.create_org("rc")
    tech = await api_env.create_user(org_id, "technician")  # has control, not policy.manage
    resp = await api_env.client.put(
        "/api/v1/remote/policies", json={}, headers=await api_env.headers(org_id, tech)
    )
    assert resp.status_code == 403


async def test_policies_put_upserts_single_org_default(api_env) -> None:
    org_id = await api_env.create_org("rc")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)

    r1 = await api_env.client.put(
        "/api/v1/remote/policies",
        json={"allow_unattended": True, "recording_retention_days": 30},
        headers=headers,
    )
    assert r1.status_code == 200, r1.text
    r2 = await api_env.client.put(
        "/api/v1/remote/policies",
        json={"allow_unattended": False, "recording_retention_days": 45},
        headers=headers,
    )
    assert r2.status_code == 200
    # the org-default row was UPDATED in place, not duplicated
    assert r1.json()["id"] == r2.json()["id"]

    lst = await api_env.client.get("/api/v1/remote/policies", headers=headers)
    defaults = [p for p in lst.json() if p["device_group_id"] is None]
    assert len(defaults) == 1
    assert defaults[0]["allow_unattended"] is False
    assert defaults[0]["recording_retention_days"] == 45
