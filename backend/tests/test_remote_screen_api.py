"""Starting and stopping screen sessions (spec-screen-share.md §5.1, §3.2-3.6).

Covers the server half that exists before any agent can share a screen: the
signed `remote.screen.open` job, the Redis registration it is bound to, the
viewer ticket kept only in api memory, and the refusals (agent too old, a
second live session, a credential, the retired guacd protocols).
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.models import Agent, AuditEvent, Device, Job, RemotePolicy, RemoteSession, User
from app.modules.remote.jobtypes import RemoteScreenOpenParams, handle_screen_result
from app.modules.remote.relay import registration_key


async def _device(api_env, org_id: UUID, *, capabilities: dict | None = None) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        dev = Device(org_id=org_id, hostname="wks-screen", os_family="windows")
        s.add(dev)
        await s.flush()
        if capabilities is not None:
            s.add(
                Agent(
                    org_id=org_id,
                    device_id=dev.id,
                    cert_fingerprint=uuid4().hex + uuid4().hex,
                    cert_serial=uuid4().hex,
                    cert_not_after=datetime.now(UTC) + timedelta(days=30),
                    enrolled_at=datetime.now(UTC),
                    capabilities=capabilities,
                )
            )
        await s.commit()
        return dev.id


SCREEN = {"remote.screen.open": True}


async def _start(api_env, headers, device_id: UUID, **extra):
    body = {"device_id": str(device_id), "reason": "INC-4821 printer", **extra}
    return await api_env.client.post("/api/v1/remote/sessions", json=body, headers=headers)


async def _tech(api_env, org_id: UUID, *roles: str) -> dict[str, str]:
    sub = await api_env.create_user(org_id, *(roles or ("technician",)))
    return await api_env.headers(org_id, sub)


async def _job_for(api_env, org_id: UUID, session_id: str) -> Job:
    async with org_session(org_id, engine=api_env.app_engine) as s:
        jobs = (
            (await s.execute(select(Job).where(Job.type == "remote.screen.open"))).scalars().all()
        )
    return next(j for j in jobs if j.params["session_id"] == session_id)


async def test_screen_is_the_default_protocol(api_env) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)

    resp = await _start(api_env, headers, device_id)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "awaiting_consent" and body["consent_mode"] == "attended"
    assert "relay_token" not in resp.text and "ticket" not in resp.text

    async with org_session(org_id, engine=api_env.app_engine) as s:
        row = (
            await s.execute(
                select(RemoteSession).where(RemoteSession.id == UUID(body["session_id"]))
            )
        ).scalar_one()
    assert row.protocol == "screen" and row.credential_id is None


async def test_signed_job_and_registration_are_bound(api_env) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)

    before = datetime.now(UTC)
    sid = (await _start(api_env, headers, device_id)).json()["session_id"]
    job = await _job_for(api_env, org_id, sid)
    params = RemoteScreenOpenParams.model_validate(job.params)

    assert params.relay_url == api_env.settings.gateway_relay_url
    assert params.consent_mode == "attended" and params.consent.timeout_s == 30
    assert params.consent.reason == "INC-4821 printer"
    connect_by = datetime.fromisoformat(params.connect_by)
    assert before + timedelta(seconds=55) < connect_by < before + timedelta(seconds=65)
    # Job lifetime covers the whole session; connect_by is what keeps it fresh.
    ttl = (job.expires_at - job.issued_at).total_seconds()
    assert ttl == pytest.approx(params.max_duration_s + 30 + 120, abs=2)

    raw = await api_env.app.state.redis.get(registration_key(UUID(sid)))
    reg = json.loads(raw)
    assert reg["device_id"] == str(device_id) and reg["org_id"] == str(org_id)
    assert reg["token_hash"] == hashlib.sha256(params.relay_token.encode()).hexdigest()
    assert params.relay_token not in raw

    ticket = api_env.app.state.remote_service.take_viewer_ticket(UUID(sid))
    assert ticket and hashlib.sha256(ticket.encode()).hexdigest() == reg["viewer_ticket_hash"]
    # One viewer per session: the ticket is handed out once.
    assert api_env.app.state.remote_service.take_viewer_ticket(UUID(sid)) is None


async def test_jobs_api_never_shows_the_relay_token(api_env) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id, "org-admin")
    device_id = await _device(api_env, org_id, capabilities=SCREEN)
    sid = (await _start(api_env, headers, device_id)).json()["session_id"]
    job = await _job_for(api_env, org_id, sid)

    resp = await api_env.client.get(f"/api/v1/jobs/{job.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert job.params["relay_token"] not in resp.text


@pytest.mark.parametrize("capabilities", [None, {}, {"inventory.collect": True}])
async def test_agent_without_the_capability_is_refused(api_env, capabilities) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id, capabilities=capabilities)

    resp = await _start(api_env, headers, device_id)
    assert resp.status_code == 409
    assert "too old" in resp.json()["detail"]


async def test_second_live_screen_session_is_refused(api_env) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)

    assert (await _start(api_env, headers, device_id)).status_code == 201
    resp = await _start(api_env, headers, device_id)
    assert resp.status_code == 409


async def test_a_session_whose_agent_never_came_does_not_block_the_device(api_env) -> None:
    """Past connect_by plus the longest consent window, an awaiting_consent
    session can never go live (its registration is gone), so the next start
    expires it instead of answering 409 for ever."""
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)
    old = (await _start(api_env, headers, device_id)).json()["session_id"]
    async with AsyncSession(api_env.admin_engine) as s:
        row = (
            await s.execute(select(RemoteSession).where(RemoteSession.id == UUID(old)))
        ).scalar_one()
        row.created_at = row.created_at - timedelta(minutes=5)
        await s.commit()

    assert (await _start(api_env, headers, device_id)).status_code == 201
    async with org_session(org_id, engine=api_env.app_engine) as s:
        row = (
            await s.execute(select(RemoteSession).where(RemoteSession.id == UUID(old)))
        ).scalar_one()
        assert row.state == "ended" and row.end_reason == "expired"
        ended = (
            (
                await s.execute(
                    select(AuditEvent).where(
                        AuditEvent.target_id == old, AuditEvent.action == "remote.session.ended"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [e.actor_type for e in ended] == ["system"]


async def test_screen_session_takes_no_credential(api_env) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)

    resp = await _start(api_env, headers, device_id, credential_id=str(uuid4()))
    assert resp.status_code == 422


@pytest.mark.parametrize(
    ("roles", "policy_allows", "expected"),
    [
        (("technician",), True, "attended"),  # lacks remote.unattended
        (("org-admin",), False, "attended"),  # policy forbids
        (("org-admin",), True, "unattended"),
    ],
)
async def test_unattended_needs_policy_and_permission(
    api_env, roles, policy_allows, expected
) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id, *roles)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)
    async with org_session(org_id, engine=api_env.app_engine) as s:
        s.add(RemotePolicy(org_id=org_id, allow_unattended=policy_allows))

    resp = await _start(api_env, headers, device_id, mode="unattended")
    assert resp.status_code == 201, resp.text
    assert resp.json()["consent_mode"] == expected
    job = await _job_for(api_env, org_id, resp.json()["session_id"])
    assert job.params["consent_mode"] == expected


async def test_policy_bounds_reach_the_signed_params(api_env) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)
    async with org_session(org_id, engine=api_env.app_engine) as s:
        s.add(
            RemotePolicy(
                org_id=org_id, consent_timeout_s=45, allow_input=False, record_sessions=False
            )
        )
    sid = (await _start(api_env, headers, device_id)).json()["session_id"]
    params = (await _job_for(api_env, org_id, sid)).params
    assert params["consent"]["timeout_s"] == 45
    assert params["allow_input"] is False and params["record"] is False


async def test_stop_removes_the_registration(api_env) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)
    sid = (await _start(api_env, headers, device_id)).json()["session_id"]

    resp = await api_env.client.post(f"/api/v1/remote/sessions/{sid}/stop", headers=headers)
    assert resp.status_code == 200, resp.text
    assert await api_env.app.state.redis.get(registration_key(UUID(sid))) is None
    assert api_env.app.state.remote_service.take_viewer_ticket(UUID(sid)) is None
    # The device is free for a new session again.
    assert (await _start(api_env, headers, device_id)).status_code == 201


@pytest.mark.parametrize("protocol", ["rdp", "vnc", "ssh"])
async def test_guacd_protocols_are_retired(api_env, protocol) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id)
    resp = await _start(api_env, headers, device_id, protocol=protocol)
    assert resp.status_code == 422
    assert "retired" in resp.json()["detail"]


async def test_result_ends_a_session_the_relay_never_ended(api_env) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)
    sid = (await _start(api_env, headers, device_id)).json()["session_id"]
    job = await _job_for(api_env, org_id, sid)

    async with org_session(org_id, engine=api_env.app_engine) as s:
        await handle_screen_result(s, job, {"outcome": "no_user_session", "duration_s": 0})
    async with org_session(org_id, engine=api_env.app_engine) as s:
        row = (
            await s.execute(select(RemoteSession).where(RemoteSession.id == UUID(sid)))
        ).scalar_one()
        assert row.state == "ended" and row.end_reason == "no_user_session"
        actions = (
            (
                await s.execute(
                    select(AuditEvent.action).where(AuditEvent.target_id == sid)
                )
            )
            .scalars()
            .all()
        )
        assert "remote.session.ended" in actions

        # A second result (or one for a session the relay already ended) is a no-op.
        await handle_screen_result(s, job, {"outcome": "bogus"})
    async with org_session(org_id, engine=api_env.app_engine) as s:
        row = (
            await s.execute(select(RemoteSession).where(RemoteSession.id == UUID(sid)))
        ).scalar_one()
        assert row.end_reason == "no_user_session"


async def test_result_for_another_device_is_ignored(api_env) -> None:
    org_id = await api_env.create_org("scr")
    headers = await _tech(api_env, org_id)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)
    sid = (await _start(api_env, headers, device_id)).json()["session_id"]
    job = await _job_for(api_env, org_id, sid)
    job.target_id = uuid4()

    async with org_session(org_id, engine=api_env.app_engine) as s:
        await handle_screen_result(s, job, {"outcome": "error"})
        row = (
            await s.execute(select(RemoteSession).where(RemoteSession.id == UUID(sid)))
        ).scalar_one()
        assert row.state == "awaiting_consent"


async def test_consent_text_names_the_technician_and_org(api_env) -> None:
    org_id = await api_env.create_org("scr")
    sub = await api_env.create_user(org_id, "technician")
    async with AsyncSession(api_env.admin_engine) as s:
        user = (await s.execute(select(User).where(User.external_subject == sub))).scalar_one()
        user.display_name = "Helga R."
        await s.commit()
    headers = await api_env.headers(org_id, sub)
    device_id = await _device(api_env, org_id, capabilities=SCREEN)
    sid = (await _start(api_env, headers, device_id)).json()["session_id"]
    consent = (await _job_for(api_env, org_id, sid)).params["consent"]
    assert consent["technician"] == "Helga R."
    assert consent["org"].startswith("scr-")
