"""Phase 0 exit test (in-repo scope): the full kernel loop in one flow.

Org → admin (fake IdP) → enrollment token → CSR → enroll → mTLS connect →
LIVE dispatch via the operator API (API → Redis → gateway subscriber → agent)
→ agent-side verification exactly per docs/05-agent-spec.md → result → API
shows succeeded → audit chain verifies.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as aioredis
import websockets

from app.core.ca import LocalCA
from app.core.signing import LocalEd25519Signer, verify_signature
from app.gateway.dispatch import ui_channel
from app.gateway.main import GatewayServer
from app.jobs.canonical import canonical_bytes
from app.jobs.engine import JobEngine
from tests.test_gateway_mtls import _client_ssl, _enroll_device, _free_port

REDIS_URL = "redis://localhost:6379/9"


class FakeAgentRunner:
    """Mirrors the Go agent's signed-job runner checks (docs/05-agent-spec.md):
    signature, device identity, freshness, nonce non-replay, known type."""

    KNOWN_TYPES = {"hello", "inventory.collect"}

    def __init__(self, device_id: str, signing_pubkey_pem: str) -> None:
        self.device_id = device_id
        self.pubkey = signing_pubkey_pem
        self.seen_nonces: set[str] = set()

    def verify_and_run(self, envelope: dict[str, Any]) -> dict[str, Any]:
        job = envelope["job"]
        if not verify_signature(self.pubkey, canonical_bytes(job), envelope["sig"]):
            raise AssertionError("signature check failed")
        if job["device"] != self.device_id:
            raise AssertionError("job addressed to another device")
        now = datetime.now(UTC)
        if not (
            datetime.fromisoformat(job["issued_at"]) <= now
            <= datetime.fromisoformat(job["expires_at"])
        ):
            raise AssertionError("job not fresh")
        if job["nonce"] in self.seen_nonces:
            raise AssertionError("nonce replay")
        self.seen_nonces.add(job["nonce"])
        if job["type"] not in self.KNOWN_TYPES:
            raise AssertionError("unknown job type")
        if job["type"] == "hello":
            return {"echo": job["params"].get("message", "ping")}
        # inventory.collect: a realistic collector document
        return {
            "hostname": "e2e-agent",
            "os_family": "linux",
            "os_version": "Ubuntu 24.04.1 LTS",
            "architecture": "x86_64",
            "serial": "E2E-SN-01",
            "hardware": {
                "cpu": "AMD EPYC 7543",
                "ram_bytes": 8589934592,
                "components": [
                    {
                        "type": "disk",
                        "identifier": "nvme0n1",
                        "attributes": {"size_bytes": 256060514304},
                    }
                ],
            },
            "software": [
                {"name": "openssh-server", "version": "9.6p1"},
                {"name": "docker-ce", "version": "26.1.4"},
            ],
            "facts": {"disk_encryption": True, "firewall_enabled": True},
        }


@pytest.fixture()
async def kernel(api_env, tmp_path: Path):
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await redis.flushdb()
    ca = LocalCA(api_env.settings.ca_dir)
    key_pem, cert_pem = ca.issue_server_cert(["localhost"])
    (tmp_path / "gw.key").write_text(key_pem)
    (tmp_path / "gw.crt").write_text(cert_pem)
    port = _free_port()
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
    )
    yield port
    handle.close()
    await handle.wait_closed()
    await server.stop()
    await redis.aclose()


async def test_kernel_end_to_end(api_env, kernel, tmp_path: Path) -> None:
    port = kernel
    enrolled = await _enroll_device(api_env, tmp_path)
    org_id, device_id = enrolled["org_id"], enrolled["device_id"]
    runner = FakeAgentRunner(device_id, enrolled["job_signing_public_key_pem"])

    admin_sub = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin_sub)

    ctx = _client_ssl(tmp_path, enrolled, "e2e-agent")
    async with websockets.connect(f"wss://localhost:{port}/agent", ssl=ctx) as ws:
        # The first connection after enrolment is handed a signed
        # `inventory.collect` unprompted, drained at connect.
        first_contact = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert first_contact["job"]["type"] == "inventory.collect"
        runner.verify_and_run(first_contact)
        await asyncio.sleep(0.3)  # let the dispatch subscriber pick up the connection

        # live dispatch through the operator API
        created = await api_env.client.post(
            "/api/v1/jobs",
            json={
                "type": "hello",
                "device_id": device_id,
                "params": {"message": "kernel exit criterion"},
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]

        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        envelope = json.loads(raw)
        result = runner.verify_and_run(envelope)  # raises if any check fails

        await ws.send(
            json.dumps({"v": 1, "kind": "job_status", "job_id": job_id, "state": "running"})
        )
        await ws.send(
            json.dumps(
                {
                    "v": 1,
                    "kind": "job_result",
                    "job_id": job_id,
                    "state": "succeeded",
                    "result": result,
                }
            )
        )
        await asyncio.sleep(0.5)

        # a replay of the same envelope must be refused by the agent
        with pytest.raises(AssertionError, match="nonce replay"):
            runner.verify_and_run(envelope)

        # === Phase 1: an inventory sweep through the same signed-job loop ===
        ui = aioredis.from_url(REDIS_URL, decode_responses=True)
        pubsub = ui.pubsub()
        # Enrolled on the internal client's site (`site_id=org_id`): the
        # device's client, and so its channel, is the org's internal client.
        await pubsub.subscribe(ui_channel(org_id, org_id))

        sweep = await api_env.client.post(
            "/api/v1/jobs",
            json={"type": "inventory.collect", "device_id": device_id, "params": {}},
            headers=headers,
        )
        assert sweep.status_code == 201, sweep.text
        sweep_id = sweep.json()["id"]
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        sweep_envelope = json.loads(raw)
        sweep_result = runner.verify_and_run(sweep_envelope)
        await ws.send(
            json.dumps(
                {
                    "v": 1,
                    "kind": "job_result",
                    "job_id": sweep_id,
                    "state": "succeeded",
                    "result": sweep_result,
                }
            )
        )
        inventory_event = None
        for _ in range(50):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message:
                event = json.loads(message["data"])
                if event.get("kind") == "inventory_updated":
                    inventory_event = event
                    break
        assert inventory_event is not None
        assert inventory_event["device_id"] == device_id

        # === Phase 2: the agent ships buffered security events ===
        batch = {
            "v": 1,
            "kind": "security_events",
            "events": [
                {
                    "event_uid": "e2e-logon-1",
                    "ts": "2026-07-18T08:00:00Z",
                    "event_type": "logon_success",
                    "username": "root",
                    "source": "sshd",
                    "outcome": "success",
                },
                {
                    "event_uid": "e2e-logon-2",
                    "ts": "2026-07-18T08:05:00Z",
                    "event_type": "logon_failure",
                    "username": "intruder",
                    "source": "sshd",
                    "outcome": "failure",
                },
            ],
        }
        await ws.send(json.dumps(batch))
        ingested_event = None
        for _ in range(50):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message:
                event = json.loads(message["data"])
                if event.get("kind") == "security_events_ingested":
                    ingested_event = event
                    break
        assert ingested_event is not None and ingested_event["count"] == 2

        # at-least-once redelivery of the identical batch inserts nothing new
        await ws.send(json.dumps(batch))
        redelivered = None
        for _ in range(50):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message:
                event = json.loads(message["data"])
                if event.get("kind") == "security_events_ingested":
                    redelivered = event
                    break
        assert redelivered is not None and redelivered["count"] == 0

        await pubsub.unsubscribe()
        await ui.aclose()

    # the operator sees the verified result
    job = (await api_env.client.get(f"/api/v1/jobs/{job_id}", headers=headers)).json()
    assert job["status"] == "succeeded"
    assert job["result"] == {"echo": "kernel exit criterion"}
    assert job["attempts"] == 1  # live delivery marked dispatched exactly once

    # the sweep landed: device summary refreshed, inventory queryable
    inventory = (
        await api_env.client.get(f"/api/v1/devices/{device_id}/inventory", headers=headers)
    ).json()
    assert inventory["summary"]["hostname"] == "e2e-agent"
    assert inventory["summary"]["os_version"] == "Ubuntu 24.04.1 LTS"
    assert {s["name"] for s in inventory["software"]} == {"openssh-server", "docker-ce"}
    assert inventory["facts"]["disk_encryption"] is True

    # security events queryable through the operator API
    events = (
        await api_env.client.get("/api/v1/security-events", headers=headers)
    ).json()
    assert events["total"] == 2
    assert {e["username"] for e in events["items"]} == {"root", "intruder"}

    # and the audit chain over the whole flow is intact
    verify = await api_env.client.get("/api/v1/audit/verify", headers=headers)
    assert verify.json()["ok"] is True
    events = (await api_env.client.get("/api/v1/audit", headers=headers)).json()["items"]
    actions = {e["action"] for e in events}
    assert {
        "enrollment.token_created",
        "enrollment.device_enrolled",
        "gateway.connected",
        "jobs.dispatched",
        "jobs.succeeded",
    } <= actions
