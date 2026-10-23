"""Screen-share relay, end to end over real mutual TLS (spec-screen-share.md §7, phase 0).

A fake agent enrolls through the real enrollment endpoint and dials the
gateway's ``/relay`` path with its issued certificate; a fake viewer (the
api's side, or the browser through the real ``/screen`` route) attaches to
the internal listener. Between them: consent, frames, input, the end, and
every refusal the spec names, each with its audit row.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import redis.asyncio as aioredis
import websockets
from moto import mock_aws
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ca import LocalCA
from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.signing import LocalEd25519Signer, verify_signature
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.gateway.main import GatewayServer
from app.jobs.canonical import canonical_bytes
from app.jobs.engine import JobEngine
from app.models import Agent, AuditEvent, Job, RemoteSession
from app.modules.remote import rsp
from app.modules.remote.relay import registration_key
from tests.test_gateway_mtls import _client_ssl, _enroll_device

REDIS_URL = "redis://localhost:6379/9"
TIMEOUT = 10


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
async def stack(api_env, tmp_path: Path):
    """A gateway with its internal relay listener, sharing Redis with the api."""
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    ca = LocalCA(api_env.settings.ca_dir)
    key_pem, cert_pem = ca.issue_server_cert(["localhost"])
    (tmp_path / "gw.key").write_text(key_pem)
    (tmp_path / "gw.crt").write_text(cert_pem)
    port, internal = _free_port(), _free_port()
    server = GatewayServer(
        settings=api_env.settings,
        app_engine=api_env.app_engine,
        admin_engine=api_env.admin_engine,
        job_engine=JobEngine(signer=LocalEd25519Signer(api_env.settings.signing_key_path)),
        redis=redis,
    )
    handle = await server.start(
        host="localhost",
        port=port,
        certfile=str(tmp_path / "gw.crt"),
        keyfile=str(tmp_path / "gw.key"),
        ca_certs=str(api_env.settings.ca_dir / "root.crt"),
        relay_internal_host="127.0.0.1",
        relay_internal_port=internal,
    )
    api_env.settings.relay_internal_url = f"ws://127.0.0.1:{internal}/relay-internal"
    await asyncio.sleep(0.1)  # let the stop listener subscribe
    yield Stack(api_env, server, port, internal, tmp_path)
    handle.close()
    await handle.wait_closed()
    await server.stop()
    await redis.aclose()


class Stack:
    def __init__(self, api_env, server, port: int, internal: int, tmp_path: Path) -> None:
        self.api_env = api_env
        self.server = server
        self.port = port
        self.internal = internal
        self.tmp_path = tmp_path
        self._n = 0

    async def device(self) -> dict:
        """Enroll a device that advertises remote.screen.open."""
        enrolled = await _enroll_device(self.api_env, self.tmp_path)
        async with AsyncSession(self.api_env.admin_engine) as s:
            await s.execute(
                update(Agent)
                .where(Agent.device_id == UUID(enrolled["device_id"]))
                .values(capabilities={"remote.screen.open": True})
            )
            await s.commit()
        self._n += 1
        enrolled["ssl"] = _client_ssl(self.tmp_path, enrolled, f"agent-{self._n}")
        return enrolled

    async def start(self, enrolled: dict, **extra) -> tuple[str, dict]:
        """Start a screen session as an org-admin; return (session_id, params)."""
        org_id = enrolled["org_id"]
        sub = await self.api_env.create_user(org_id, "org-admin")
        self.headers = await self.api_env.headers(org_id, sub)
        self.sub = sub
        resp = await self.api_env.client.post(
            "/api/v1/remote/sessions",
            json={"device_id": enrolled["device_id"], "reason": "INC-1 relay", **extra},
            headers=self.headers,
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["session_id"]
        async with org_session(
            org_id, engine=self.api_env.app_engine, clients=ClientScope.all()
        ) as s:
            jobs = (
                (await s.execute(select(Job).where(Job.type == "remote.screen.open")))
                .scalars()
                .all()
            )
        job = next(j for j in jobs if j.params["session_id"] == sid)
        return sid, dict(job.params)

    def ticket(self, sid: str) -> str:
        ticket = self.api_env.app.state.remote_service.take_viewer_ticket(UUID(sid))
        assert ticket
        return ticket

    def agent(self, enrolled: dict, *, subprotocol: bool = True):
        return websockets.connect(
            f"wss://localhost:{self.port}/relay",
            ssl=enrolled["ssl"],
            subprotocols=[rsp.SUBPROTOCOL] if subprotocol else None,
        )

    def viewer(self):
        return websockets.connect(
            f"ws://127.0.0.1:{self.internal}/relay-internal", subprotocols=[rsp.SUBPROTOCOL]
        )

    async def row(self, enrolled: dict, sid: str) -> RemoteSession:
        async with org_session(
            enrolled["org_id"], engine=self.api_env.app_engine, clients=ClientScope.all()
        ) as s:
            return (
                await s.execute(select(RemoteSession).where(RemoteSession.id == UUID(sid)))
            ).scalar_one()

    async def audits(self, enrolled: dict, **match) -> list[AuditEvent]:
        async with org_session(
            enrolled["org_id"], engine=self.api_env.app_engine, clients=ClientScope.all()
        ) as s:
            rows = (
                (await s.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars().all()
            )
        return [r for r in rows if all(getattr(r, k) == v for k, v in match.items())]


async def _auth_agent(ws, sid: str, token: str) -> None:
    await ws.send(rsp.auth_frame(session_id=sid, secret_field="token", secret=token))
    ok = rsp.decode(await asyncio.wait_for(ws.recv(), TIMEOUT))
    assert ok.type == rsp.AUTH_OK


async def _auth_viewer(ws, sid: str, ticket: str) -> None:
    await ws.send(rsp.auth_frame(session_id=sid, secret_field="ticket", secret=ticket))
    ok = rsp.decode(await asyncio.wait_for(ws.recv(), TIMEOUT))
    assert ok.type == rsp.AUTH_OK


async def _recv_type(ws) -> int:
    return rsp.decode(await asyncio.wait_for(ws.recv(), TIMEOUT)).type


async def _closed_with(ws) -> int | None:
    with pytest.raises(websockets.exceptions.ConnectionClosed):
        await asyncio.wait_for(ws.recv(), TIMEOUT)
    return ws.close_code


async def _eventually(predicate, timeout: float = TIMEOUT) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.05)


def _state(name: str, seq: int, **extra) -> bytes:
    return rsp.state_frame(name, seq, **extra)


# --------------------------------------------------------------------------- #
# the whole path
# --------------------------------------------------------------------------- #


async def test_signed_job_to_frames_to_end_through_the_browser_route(stack) -> None:
    """Signed job -> agent leg -> consent -> frames both ways -> end, with the
    browser on the real /screen route and a recording uploaded multipart."""
    import uvicorn

    api_env = stack.api_env
    enrolled = await stack.device()
    org_id = enrolled["org_id"]
    sid, _ = await stack.start(enrolled)

    # The agent's control channel delivers the signed job; verify it the way
    # the Go runner does before using anything in it.
    async with websockets.connect(
        f"wss://localhost:{stack.port}/agent", ssl=enrolled["ssl"]
    ) as control:
        envelope = json.loads(await asyncio.wait_for(control.recv(), TIMEOUT))
    job = envelope["job"]
    assert job["type"] == "remote.screen.open"
    assert verify_signature(
        enrolled["job_signing_public_key_pem"], canonical_bytes(job), envelope["sig"]
    )
    params = job["params"]

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(
            Settings(
                database_url="postgresql+asyncpg://x:x@l/x",
                database_admin_url="postgresql+asyncpg://x:x@l/x",
                oidc_issuer="https://i",
                oidc_jwks_url="https://i/certs",
                ca_dir=stack.tmp_path,
                s3_access_key="test",
                s3_secret_key="test",
                s3_bucket="it-suite-test",
            )
        )
        http_port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(api_env.app, host="127.0.0.1", port=http_port, log_level="warning")
        )
        serve_task = asyncio.create_task(server.serve())
        await _eventually(lambda: asyncio.sleep(0, server.started))
        try:
            token = api_env.idp.token(
                stack.sub, await api_env.org_slug(org_id), email=f"{stack.sub}@test.local"
            )
            browser = await websockets.connect(
                f"ws://127.0.0.1:{http_port}/api/v1/remote/sessions/{sid}/screen",
                subprotocols=["bearer", token],  # type: ignore[list-item]
            )
            async with stack.agent(enrolled) as agent:
                await _auth_agent(agent, sid, params["relay_token"])
                await agent.send(_state("consent_pending", 1, timeout_s=30))
                assert await _recv_type(browser) == rsp.STATE
                # Media before consent never reaches the viewer.
                await agent.send(rsp.encode(rsp.TILE, 2, b"early"))
                await agent.send(_state("consent_granted", 3))
                assert await _recv_type(browser) == rsp.STATE

                for i, t in enumerate((rsp.HELLO, rsp.FRAME_BEGIN, rsp.TILE, rsp.FRAME_END)):
                    await agent.send(rsp.encode(t, 10 + i, b"{}" if t == rsp.HELLO else b"x"))
                got = [await _recv_type(browser) for _ in range(4)]
                assert got == [rsp.HELLO, rsp.FRAME_BEGIN, rsp.TILE, rsp.FRAME_END]
                assert (await stack.row(enrolled, sid)).state == "active"

                # Wrong direction is dropped in both directions.
                await agent.send(rsp.encode(rsp.POINTER, 20, b"\x00\x01\x00\x01\x00"))
                await browser.send(rsp.encode(rsp.TILE, 1, b"x"))
                await browser.send(rsp.encode(rsp.ACK, 2, (13).to_bytes(4, "big")))
                assert await _recv_type(agent) == rsp.ACK
                await agent.send(rsp.encode(rsp.CURSOR_POS, 21, b"\x00\x01\x00\x01\x01"))
                assert await _recv_type(browser) == rsp.CURSOR_POS

                # The technician goes view-only: input stops at the gateway.
                await browser.send(rsp.encode(rsp.CONTROL, 3, b"\x00"))
                assert await _recv_type(agent) == rsp.CONTROL
                await browser.send(rsp.encode(rsp.POINTER, 4, b"\x00\x05\x00\x05\x01"))
                await browser.send(rsp.encode(rsp.REFRESH, 5))
                assert await _recv_type(agent) == rsp.REFRESH

                # The endpoint user presses End.
                await agent.send(_state("ended", 30, reason="ended_by_user"))
                assert await _recv_type(browser) == rsp.STATE
            await _closed_with(browser)

            async def finished() -> bool:
                row = await stack.row(enrolled, sid)
                return row.state == "ended" and row.recording_ref is not None

            await _eventually(finished)
            row = await stack.row(enrolled, sid)
            assert row.end_reason == "ended_by_user" and row.input_enabled is False
            assert row.recording_ref.endswith(f"{sid}.rsp")
            data = await api_env.app.state.object_store.get_remote_recording(row.recording_ref)
            recorded = [rsp.decode(f).type for _, f in rsp.read_recording(data)]
            assert rsp.TILE in recorded and rsp.HELLO in recorded
            assert recorded.count(rsp.TILE) == 1  # the early tile was never forwarded
        finally:
            server.should_exit = True
            await serve_task

    actions = [a.action for a in await stack.audits(enrolled, target_id=sid)]
    for expected in (
        "remote.session.consent_prompted",
        "remote.session.consent_granted",
        "remote.session.started",
        "remote.session.input_disabled",
        "remote.session.ended_by_user",
        "remote.session.ended",
    ):
        assert expected in actions, (expected, actions)
    assert actions.count("remote.session.ended") == 1
    # No API response carries the relay token.
    assert params["relay_token"] not in json.dumps(
        (await api_env.client.get(f"/api/v1/remote/sessions/{sid}", headers=stack.headers)).json()
    )


# --------------------------------------------------------------------------- #
# refusals, each with its audit row
# --------------------------------------------------------------------------- #


async def test_token_is_single_use(stack) -> None:
    enrolled = await stack.device()
    sid, params = await stack.start(enrolled)
    async with stack.agent(enrolled) as agent:
        await _auth_agent(agent, sid, params["relay_token"])
    async with stack.agent(enrolled) as again:
        await again.send(
            rsp.auth_frame(session_id=sid, secret_field="token", secret=params["relay_token"])
        )
        assert await _closed_with(again) == rsp.CLOSE_REFUSED
    rejected = await stack.audits(enrolled, action="remote.relay.token_rejected", target_id=sid)
    assert [a.meta["reason"] for a in rejected] == ["no_registration"]


async def test_wrong_device_is_refused(stack) -> None:
    victim = await stack.device()
    sid, params = await stack.start(victim)
    intruder = await stack.device()
    async with stack.agent(intruder) as agent:
        await agent.send(
            rsp.auth_frame(session_id=sid, secret_field="token", secret=params["relay_token"])
        )
        assert await _closed_with(agent) == rsp.CLOSE_REFUSED
    [event] = await stack.audits(
        victim, action="remote.relay.identity_mismatch", target_id=sid
    )
    assert event.meta["reason"] == "device_mismatch"
    assert event.meta["device_id"] == intruder["device_id"]


async def test_bad_token_is_refused(stack) -> None:
    enrolled = await stack.device()
    sid, _ = await stack.start(enrolled)
    async with stack.agent(enrolled) as agent:
        await agent.send(rsp.auth_frame(session_id=sid, secret_field="token", secret="nope"))
        assert await _closed_with(agent) == rsp.CLOSE_REFUSED
    [event] = await stack.audits(enrolled, action="remote.relay.token_rejected", target_id=sid)
    assert event.meta["reason"] == "bad_token"


async def test_expired_connect_by_is_refused(stack) -> None:
    enrolled = await stack.device()
    sid, params = await stack.start(enrolled)
    # Keep the key alive past connect_by, so the gateway's own check is what
    # refuses, not the TTL.
    redis = stack.api_env.app.state.redis
    reg = json.loads(await redis.get(registration_key(UUID(sid))))
    reg["connect_by"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await redis.set(registration_key(UUID(sid)), json.dumps(reg), ex=60)

    async with stack.agent(enrolled) as agent:
        await agent.send(
            rsp.auth_frame(session_id=sid, secret_field="token", secret=params["relay_token"])
        )
        assert await _closed_with(agent) == rsp.CLOSE_REFUSED
    [event] = await stack.audits(enrolled, action="remote.relay.token_rejected", target_id=sid)
    assert event.meta["reason"] == "expired"


async def test_revoked_certificate_is_refused(stack) -> None:
    enrolled = await stack.device()
    sid, params = await stack.start(enrolled)
    async with AsyncSession(stack.api_env.admin_engine) as s:
        await s.execute(
            update(Agent)
            .where(Agent.device_id == UUID(enrolled["device_id"]))
            .values(cert_revoked=True)
        )
        await s.commit()
    async with stack.agent(enrolled) as agent:
        assert await _closed_with(agent) == rsp.CLOSE_UNAUTHORIZED
    [event] = await stack.audits(enrolled, action="remote.relay.identity_mismatch")
    assert event.meta["reason"] == "cert_revoked"
    # The registration was not touched: nothing was presented.
    assert await stack.api_env.app.state.redis.get(registration_key(UUID(sid)))


async def test_relay_requires_the_subprotocol(stack) -> None:
    enrolled = await stack.device()
    async with stack.agent(enrolled, subprotocol=False) as agent:
        assert await _closed_with(agent) == rsp.CLOSE_PROTOCOL


async def test_agent_channel_still_accepts_clients_offering_no_subprotocol(stack) -> None:
    """Deployed agents offer no subprotocol on /agent; the relay's negotiation
    must not turn that into an HTTP 400."""
    enrolled = await stack.device()
    async with websockets.connect(
        f"wss://localhost:{stack.port}/agent", ssl=enrolled["ssl"]
    ) as control:
        assert control.subprotocol is None
        await control.send(json.dumps({"v": 1, "kind": "heartbeat"}))
        await asyncio.sleep(0.2)
        assert control.close_code is None


async def test_viewer_with_a_wrong_ticket_is_refused(stack) -> None:
    enrolled = await stack.device()
    sid, _ = await stack.start(enrolled)
    async with stack.viewer() as viewer:
        await viewer.send(rsp.auth_frame(session_id=sid, secret_field="ticket", secret="nope"))
        assert await _closed_with(viewer) == rsp.CLOSE_REFUSED
    [event] = await stack.audits(enrolled, action="remote.relay.token_rejected", target_id=sid)
    assert event.meta == {"reason": "bad_ticket", "leg": "viewer"}


# --------------------------------------------------------------------------- #
# consent outcomes and stops
# --------------------------------------------------------------------------- #


async def test_consent_denied_ends_the_session(stack) -> None:
    enrolled = await stack.device()
    sid, params = await stack.start(enrolled)
    async with stack.viewer() as viewer, stack.agent(enrolled) as agent:
        await _auth_viewer(viewer, sid, stack.ticket(sid))
        await _auth_agent(agent, sid, params["relay_token"])
        await agent.send(_state("consent_pending", 1))
        await agent.send(_state("consent_denied", 2))
        assert await _recv_type(viewer) == rsp.STATE
        assert await _recv_type(viewer) == rsp.STATE
        assert await _closed_with(viewer) == rsp.CLOSE_ENDED
        await _closed_with(agent)
    row = await stack.row(enrolled, sid)
    assert row.state == "ended" and row.end_reason == "denied_consent"
    actions = [a.action for a in await stack.audits(enrolled, target_id=sid)]
    assert "remote.session.denied" in actions and "remote.session.started" not in actions


async def test_consent_timeout_ends_the_session(stack) -> None:
    enrolled = await stack.device()
    sid, params = await stack.start(enrolled)
    async with stack.viewer() as viewer, stack.agent(enrolled) as agent:
        await _auth_viewer(viewer, sid, stack.ticket(sid))
        await _auth_agent(agent, sid, params["relay_token"])
        await agent.send(_state("consent_timeout", 1))
        assert await _recv_type(viewer) == rsp.STATE
        await _closed_with(viewer)
    row = await stack.row(enrolled, sid)
    assert row.end_reason == "consent_timeout"
    actions = [a.action for a in await stack.audits(enrolled, target_id=sid)]
    assert "remote.session.consent_timeout" in actions and "remote.session.ended" in actions


async def test_unattended_needs_no_consent_frames(stack) -> None:
    enrolled = await stack.device()
    async with org_session(
        enrolled["org_id"], engine=stack.api_env.app_engine, clients=ClientScope.all()
    ) as s:
        from app.models import RemotePolicy

        s.add(RemotePolicy(org_id=enrolled["org_id"], allow_unattended=True))
    sid, params = await stack.start(enrolled, mode="unattended")
    assert params["consent_mode"] == "unattended"
    async with stack.viewer() as viewer, stack.agent(enrolled) as agent:
        await _auth_viewer(viewer, sid, stack.ticket(sid))
        await _auth_agent(agent, sid, params["relay_token"])
        await agent.send(rsp.encode(rsp.HELLO, 1, b"{}"))
        assert await _recv_type(viewer) == rsp.HELLO
        assert (await stack.row(enrolled, sid)).state == "active"


async def test_stop_from_the_api_tears_down_both_legs(stack) -> None:
    enrolled = await stack.device()
    sid, params = await stack.start(enrolled)
    async with stack.viewer() as viewer, stack.agent(enrolled) as agent:
        await _auth_viewer(viewer, sid, stack.ticket(sid))
        await _auth_agent(agent, sid, params["relay_token"])
        resp = await stack.api_env.client.post(
            f"/api/v1/remote/sessions/{sid}/stop", headers=stack.headers
        )
        assert resp.status_code == 200, resp.text
        await _closed_with(agent)
        await _closed_with(viewer)
    row = await stack.row(enrolled, sid)
    assert row.end_reason == "completed"
    ended = await stack.audits(enrolled, action="remote.session.ended", target_id=sid)
    assert len(ended) == 1


async def test_agent_that_never_connects_expires_the_viewer(stack, monkeypatch) -> None:
    enrolled = await stack.device()
    monkeypatch.setattr(stack.server.relay, "_agent_grace_s", 0.0)
    sid, _ = await stack.start(enrolled)
    redis = stack.api_env.app.state.redis
    reg = json.loads(await redis.get(registration_key(UUID(sid))))
    reg["connect_by"] = (datetime.now(UTC) + timedelta(seconds=0.5)).isoformat()
    await redis.set(registration_key(UUID(sid)), json.dumps(reg), ex=60)

    async with stack.viewer() as viewer:
        await _auth_viewer(viewer, sid, stack.ticket(sid))
        assert await _closed_with(viewer) == rsp.CLOSE_TIMEOUT
    row = await stack.row(enrolled, sid)
    assert row.state == "ended" and row.end_reason == "expired"


async def test_gateway_bounds_the_consent_window_itself(stack, monkeypatch) -> None:
    """An agent that never reports a consent outcome does not hold the
    session open: the gateway ends it as consent_timeout."""
    monkeypatch.setattr(stack.server.relay, "_consent_grace_s", 0.0)
    enrolled = await stack.device()
    async with org_session(
        enrolled["org_id"], engine=stack.api_env.app_engine, clients=ClientScope.all()
    ) as s:
        from app.models import RemotePolicy

        s.add(RemotePolicy(org_id=enrolled["org_id"], consent_timeout_s=15))
    sid, params = await stack.start(enrolled)
    redis = stack.api_env.app.state.redis
    reg = json.loads(await redis.get(registration_key(UUID(sid))))
    reg["consent_timeout_s"] = 1  # below the policy floor, to keep the test short
    await redis.set(registration_key(UUID(sid)), json.dumps(reg), ex=60)

    async with stack.viewer() as viewer, stack.agent(enrolled) as agent:
        await _auth_viewer(viewer, sid, stack.ticket(sid))
        await _auth_agent(agent, sid, params["relay_token"])
        await agent.send(_state("consent_pending", 1))
        assert await _recv_type(viewer) == rsp.STATE
        assert await _closed_with(viewer) == rsp.CLOSE_ENDED
        await _closed_with(agent)
    row = await stack.row(enrolled, sid)
    assert row.end_reason == "consent_timeout"
    [event] = await stack.audits(
        enrolled, action="remote.session.consent_timeout", target_id=sid
    )
    assert event.meta["source"] == "gateway"


async def test_user_stopped_control_cannot_be_taken_back(stack) -> None:
    enrolled = await stack.device()
    sid, params = await stack.start(enrolled)
    async with stack.viewer() as viewer, stack.agent(enrolled) as agent:
        await _auth_viewer(viewer, sid, stack.ticket(sid))
        await _auth_agent(agent, sid, params["relay_token"])
        await agent.send(_state("consent_granted", 1))
        await agent.send(_state("control_revoked_by_user", 2))
        assert await _recv_type(viewer) == rsp.STATE
        assert await _recv_type(viewer) == rsp.STATE
        # Asking for control back and sending input are both dropped.
        await viewer.send(rsp.encode(rsp.CONTROL, 1, b"\x01"))
        await viewer.send(rsp.encode(rsp.KEY, 2, b"\x01KeyA"))
        await viewer.send(rsp.encode(rsp.REFRESH, 3))
        assert await _recv_type(agent) == rsp.REFRESH
    # STATE is forwarded before the gateway writes its audit row.
    async def audited() -> bool:
        return bool(
            await stack.audits(enrolled, action="remote.session.input_disabled", target_id=sid)
        )

    await _eventually(audited)
    [event] = await stack.audits(
        enrolled, action="remote.session.input_disabled", target_id=sid
    )
    assert event.meta["by"] == "endpoint_user"
    assert (await stack.row(enrolled, sid)).input_enabled is False


# --------------------------------------------------------------------------- #
# the Go test double (agent/internal/rsp/fakeagent)
# --------------------------------------------------------------------------- #

AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"


@pytest.fixture(scope="module")
def go_fake_agent(tmp_path_factory) -> Path:
    """Build the Go double of the agent's relay leg from this tree."""
    go = shutil.which("go")
    if go is None:
        # CI sets this so the check cannot quietly turn into a skip there.
        if os.environ.get("ITS_TEST_REQUIRE_GO") == "1":
            pytest.fail("ITS_TEST_REQUIRE_GO=1 but no Go toolchain on PATH")
        pytest.skip("Go toolchain not installed; the Go relay double cannot run")
    out = tmp_path_factory.mktemp("go") / "fakeagent"
    subprocess.run(
        [go, "build", "-o", str(out), "./internal/rsp/fakeagent"],
        cwd=AGENT_DIR,
        check=True,
        timeout=600,
    )
    return out


async def _run_go_agent(
    binary: Path, stack: Stack, n: int, sid: str, token: str, script: str
) -> asyncio.subprocess.Process:
    name = f"agent-{n}"
    return await asyncio.create_subprocess_exec(
        str(binary),
        f"-url=wss://localhost:{stack.port}/relay",
        f"-cert={stack.tmp_path / f'{name}.crt'}",
        f"-key={stack.tmp_path / f'{name}.key'}",
        f"-ca={stack.tmp_path / f'{name}-ca.pem'}",
        f"-session={sid}",
        f"-token={token}",
        f"-script={script}",
        "-timeout=20s",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _events(proc: asyncio.subprocess.Process) -> list[dict]:
    out, err = await asyncio.wait_for(proc.communicate(), 30)
    assert proc.returncode == 0, (out.decode(), err.decode())
    return [json.loads(line) for line in out.decode().splitlines() if line.strip()]


async def test_go_agent_double_runs_a_session_through_the_relay(stack, go_fake_agent) -> None:
    enrolled = await stack.device()
    sid, params = await stack.start(enrolled)
    async with stack.viewer() as viewer:
        await _auth_viewer(viewer, sid, stack.ticket(sid))
        proc = await _run_go_agent(
            go_fake_agent, stack, stack._n, sid, params["relay_token"], "grant"
        )
        got = [rsp.decode(await asyncio.wait_for(viewer.recv(), TIMEOUT)) for _ in range(6)]
        assert [f.type for f in got] == [
            rsp.STATE,
            rsp.STATE,
            rsp.HELLO,
            rsp.FRAME_BEGIN,
            rsp.TILE,
            rsp.FRAME_END,
        ]
        assert [rsp.parse_state(f).state for f in got[:2]] == ["consent_pending", "consent_granted"]
        frame_end = got[-1].seq

        await viewer.send(rsp.encode(rsp.ACK, 1, frame_end.to_bytes(4, "big")))
        await viewer.send(rsp.encode(rsp.POINTER, 2, b"\x00\x64\x00\xc8\x01"))
        echo = rsp.decode(await asyncio.wait_for(viewer.recv(), TIMEOUT))
        assert echo.type == rsp.CURSOR_POS and echo.body[:4] == b"\x00\x64\x00\xc8"
        await viewer.send(rsp.encode(rsp.TEXT, 3, b"bye"))
        ended = rsp.parse_state(rsp.decode(await asyncio.wait_for(viewer.recv(), TIMEOUT)))
        assert ended == rsp.State("ended", "ended_by_user")
        assert await _closed_with(viewer) == rsp.CLOSE_ENDED

    events = await _events(proc)
    kinds = [e["event"] for e in events]
    assert kinds[:2] == ["auth_ok", "frame_sent"] and kinds[-1] == "ended"
    assert {"event": "ack", "frame_seq": frame_end} in events
    assert {"event": "pointer", "x": 100, "y": 200, "buttons": 1} in events

    row = await stack.row(enrolled, sid)
    assert row.state == "ended" and row.end_reason == "ended_by_user"
    actions = [a.action for a in await stack.audits(enrolled, target_id=sid)]
    assert "remote.session.started" in actions and "remote.session.ended_by_user" in actions


async def test_go_agent_double_is_refused_a_used_token(stack, go_fake_agent) -> None:
    enrolled = await stack.device()
    sid, params = await stack.start(enrolled)
    async with stack.viewer() as viewer:
        await _auth_viewer(viewer, sid, stack.ticket(sid))
        first = await _run_go_agent(
            go_fake_agent, stack, stack._n, sid, params["relay_token"], "deny"
        )
        assert [e["event"] for e in await _events(first)] == ["auth_ok", "closed"]
    second = await _run_go_agent(
        go_fake_agent, stack, stack._n, sid, params["relay_token"], "grant"
    )
    assert await _events(second) == [{"event": "refused", "code": rsp.CLOSE_REFUSED}]
    assert (await stack.row(enrolled, sid)).end_reason == "denied_consent"


# --------------------------------------------------------------------------- #
# the real capability (agent/internal/capabilities/remote) through the gateway
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def go_relay_agent(tmp_path_factory) -> Path:
    """Build the double that runs the real capability's network side."""
    go = shutil.which("go")
    if go is None:
        if os.environ.get("ITS_TEST_REQUIRE_GO") == "1":
            pytest.fail("ITS_TEST_REQUIRE_GO=1 but no Go toolchain on PATH")
        pytest.skip("Go toolchain not installed; the Go capability cannot run")
    out = tmp_path_factory.mktemp("go") / "relayagent"
    subprocess.run(
        [go, "build", "-o", str(out), "./internal/capabilities/remote/relayagent"],
        cwd=AGENT_DIR,
        check=True,
        timeout=600,
    )
    return out


async def _until(ws, frame_type: int) -> rsp.Frame:
    while True:
        f = rsp.decode(await asyncio.wait_for(ws.recv(), TIMEOUT))
        if f.type == frame_type:
            return f


async def test_the_real_capability_runs_a_session_through_the_gateway(
    stack, go_relay_agent
) -> None:
    """Signed job over the agent channel -> the Go runner verifies it -> the
    real remote.screen.open handler dials /relay over mTLS with the token from
    the signed params, launches a helper over inherited pipes, and streams
    consent, HELLO, tiles and the cursor -> the technician leaves -> the job
    result reports the outcome."""
    api_env = stack.api_env
    enrolled = await stack.device()
    api_env.app.state.remote_service._relay_url = f"wss://localhost:{stack.port}/relay"
    sid, params = await stack.start(enrolled)
    assert params["relay_url"] == f"wss://localhost:{stack.port}/relay"
    name = f"agent-{stack._n}"
    signing = stack.tmp_path / f"{name}-signing.pem"
    signing.write_text(enrolled["job_signing_public_key_pem"])

    async with stack.viewer() as viewer:
        await _auth_viewer(viewer, sid, stack.ticket(sid))
        proc = await asyncio.create_subprocess_exec(
            str(go_relay_agent),
            f"-gateway=wss://localhost:{stack.port}/agent",
            f"-cert={stack.tmp_path / f'{name}.crt'}",
            f"-key={stack.tmp_path / f'{name}.key'}",
            f"-ca={stack.tmp_path / f'{name}-ca.pem'}",
            f"-device={enrolled['device_id']}",
            f"-signing-key={signing}",
            "-timeout=40s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        states = [
            rsp.parse_state(rsp.decode(await asyncio.wait_for(viewer.recv(), TIMEOUT)))
            for _ in range(2)
        ]
        assert [s.state for s in states if s] == ["consent_pending", "consent_granted"]
        hello = await _until(viewer, rsp.HELLO)
        assert json.loads(hello.body)["monitors"][0]["width"] == 640

        await viewer.send(rsp.encode(rsp.REFRESH, 1))
        tiles = 0
        while True:
            f = rsp.decode(await asyncio.wait_for(viewer.recv(), TIMEOUT))
            tiles += f.type == rsp.TILE
            if f.type == rsp.FRAME_END:
                break
        assert tiles > 0
        await viewer.send(rsp.encode(rsp.ACK, 2, f.seq.to_bytes(4, "big")))

        # A click travels to the helper and comes back as the cursor.
        await viewer.send(rsp.encode(rsp.POINTER, 3, b"\x00\x1e\x00\x28\x01"))
        pos = await _until(viewer, rsp.CURSOR_POS)
        assert pos.body[:4] == b"\x00\x1e\x00\x28"
        # The technician ends the session by leaving.

    events = await _events(proc)
    assert events[0]["event"] == "connected"
    [result] = [e for e in events if e["event"] == "result"]
    assert result["state"] == "succeeded", result
    assert result["result"]["outcome"] == "ended", result

    async def finished() -> bool:
        async with org_session(
            enrolled["org_id"], engine=api_env.app_engine, clients=ClientScope.all()
        ) as s:
            job = (
                await s.execute(select(Job).where(Job.type == "remote.screen.open"))
            ).scalar_one()
        return str(job.status.value) == "succeeded"

    await _eventually(finished)
    row = await stack.row(enrolled, sid)
    assert row.state == "ended" and row.end_reason == "completed"
    actions = [a.action for a in await stack.audits(enrolled, target_id=sid)]
    for expected in (
        "remote.session.consent_prompted",
        "remote.session.consent_granted",
        "remote.session.started",
        "remote.session.ended",
    ):
        assert expected in actions, (expected, actions)
