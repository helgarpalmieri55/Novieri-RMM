"""Gateway integration test over real mutual TLS.

A fake agent enrolls through the real enrollment endpoint, connects to the
gateway with its issued client certificate, verifies the signed job envelope
exactly as the Go agent will (signature, device identity, freshness, nonce),
and reports a result. Unknown and revoked certificates are refused.
"""

import asyncio
import json
import socket
import ssl
from datetime import UTC, datetime
from pathlib import Path

import pytest
import redis.asyncio as aioredis
import websockets
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ca import LocalCA
from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer, verify_signature
from app.core.tenancy import org_session
from app.gateway.main import GatewayServer
from app.jobs.canonical import canonical_bytes
from app.jobs.engine import JobEngine
from app.models import Agent, Job, JobStatus

REDIS_URL = "redis://localhost:6379/9"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_key_and_csr() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent")]))
        .sign(key, None)
    )
    key_pem = key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()
    return key_pem, csr.public_bytes(Encoding.PEM).decode()


async def _enroll_device(api_env, tmp_path: Path) -> dict:
    org_id = await api_env.create_org("gw")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    token = (
        await api_env.client.post(
            "/api/v1/enrollment-tokens",
            json={
                "name": "gw-test",
                "expires_in_hours": 1,
                "max_uses": 1,
                "site_id": str(org_id),
            },
            headers=headers,
        )
    ).json()["token"]
    key_pem, csr_pem = make_key_and_csr()
    response = await api_env.client.post(
        "/enroll/v1",
        json={
            "token": token,
            "csr_pem": csr_pem,
            "device_facts": {"hostname": "gw-agent", "os_family": "linux"},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    body["key_pem"] = key_pem
    body["org_id"] = org_id
    return body


def _client_ssl(tmp_path: Path, enrolled: dict, name: str) -> ssl.SSLContext:
    cert_file = tmp_path / f"{name}.crt"
    key_file = tmp_path / f"{name}.key"
    ca_file = tmp_path / f"{name}-ca.pem"
    cert_file.write_text(enrolled["cert_pem"])
    key_file.write_text(enrolled["key_pem"])
    ca_file.write_text(enrolled["ca_bundle_pem"])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(cert_file, key_file)
    ctx.load_verify_locations(ca_file)
    return ctx


@pytest.fixture()
async def gateway(api_env, tmp_path: Path):
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await redis.flushdb()
    ca = LocalCA(api_env.settings.ca_dir)
    key_pem, cert_pem = ca.issue_server_cert(["localhost"])
    server_key = tmp_path / "gw.key"
    server_cert = tmp_path / "gw.crt"
    server_key.write_text(key_pem)
    server_cert.write_text(cert_pem)

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
        certfile=str(server_cert),
        keyfile=str(server_key),
        ca_certs=str(api_env.settings.ca_dir / "root.crt"),
    )
    yield server, port
    handle.close()
    await handle.wait_closed()
    await server.stop()
    await redis.aclose()


async def test_agent_runs_signed_job_end_to_end(api_env, gateway, tmp_path: Path) -> None:
    server, port = gateway
    enrolled = await _enroll_device(api_env, tmp_path)
    org_id = enrolled["org_id"]
    device_id = enrolled["device_id"]

    # queue a job before the agent connects: it must be drained on connect
    job_engine = JobEngine(signer=LocalEd25519Signer(api_env.settings.signing_key_path))
    from uuid import UUID

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        job = await job_engine.create(
            session,
            org_id=org_id,
            type="hello",
            target_device_id=UUID(device_id),
            params={"message": "hi from control plane"},
        )
        job_id = str(job.id)

    ctx = _client_ssl(tmp_path, enrolled, "agent-a")
    async with websockets.connect(f"wss://localhost:{port}/agent", ssl=ctx) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        envelope = json.loads(raw)
        assert envelope["kind"] == "job"
        job_payload = envelope["job"]

        # === the agent-side signed-job runner checks (docs/05-agent-spec.md) ===
        assert verify_signature(
            enrolled["job_signing_public_key_pem"],
            canonical_bytes(job_payload),
            envelope["sig"],
        )
        assert job_payload["device"] == device_id
        assert datetime.fromisoformat(job_payload["expires_at"]) > datetime.now(UTC)

        await ws.send(
            json.dumps(
                {"v": 1, "kind": "job_status", "job_id": job_payload["id"], "state": "running"}
            )
        )
        await ws.send(
            json.dumps(
                {
                    "v": 1,
                    "kind": "job_result",
                    "job_id": job_payload["id"],
                    "state": "succeeded",
                    "result": {"echo": job_payload["params"]["message"]},
                }
            )
        )
        await asyncio.sleep(0.5)  # let the server process before closing

    from uuid import UUID as _UUID

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        refreshed = (
            await session.execute(select(Job).where(Job.id == _UUID(job_id)))
        ).scalar_one()
        assert refreshed.status is JobStatus.succeeded
        assert refreshed.result == {"echo": "hi from control plane"}
        agent_row = (
            await session.execute(select(Agent).where(Agent.device_id == _UUID(device_id)))
        ).scalar_one()
        assert agent_row.last_seen_at is not None


async def test_unenrolled_certificate_refused(api_env, gateway, tmp_path: Path) -> None:
    server, port = gateway
    # certificate signed by the same CA but never enrolled (no Agent row)
    ca = LocalCA(api_env.settings.ca_dir)
    key_pem, csr_pem = make_key_and_csr()
    from uuid import uuid4

    issued = await ca.sign_csr(csr_pem, device_id=uuid4(), org_id=uuid4())
    fake = {
        "cert_pem": issued.cert_pem,
        "key_pem": key_pem,
        "ca_bundle_pem": await ca.ca_bundle_pem(),
    }
    ctx = _client_ssl(tmp_path, fake, "rogue")
    with pytest.raises(websockets.exceptions.ConnectionClosed) as excinfo:
        async with websockets.connect(f"wss://localhost:{port}/agent", ssl=ctx) as ws:
            await ws.recv()
    assert excinfo.value.rcvd.code == 4401


async def test_revoked_certificate_refused(api_env, gateway, tmp_path: Path) -> None:
    server, port = gateway
    enrolled = await _enroll_device(api_env, tmp_path)
    async with AsyncSession(api_env.admin_engine) as session:
        from uuid import UUID

        agent_row = (
            await session.execute(
                select(Agent).where(Agent.device_id == UUID(enrolled["device_id"]))
            )
        ).scalar_one()
        agent_row.cert_revoked = True
        session.add(agent_row)
        await session.commit()

    ctx = _client_ssl(tmp_path, enrolled, "revoked")
    with pytest.raises(websockets.exceptions.ConnectionClosed) as excinfo:
        async with websockets.connect(f"wss://localhost:{port}/agent", ssl=ctx) as ws:
            await ws.recv()
    assert excinfo.value.rcvd.code == 4401


async def test_first_connect_after_enrolment_delivers_one_inventory_collect(
    api_env, gateway, tmp_path: Path
) -> None:
    """The first connection queues a signed `inventory.collect` and the
    connect-time drain delivers it; a reconnect queues nothing more."""
    from uuid import UUID

    server, port = gateway
    enrolled = await _enroll_device(api_env, tmp_path)
    org_id = enrolled["org_id"]
    device_id = UUID(enrolled["device_id"])
    ctx = _client_ssl(tmp_path, enrolled, "first-connect")

    async with websockets.connect(f"wss://localhost:{port}/agent", ssl=ctx) as ws:
        envelope = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert envelope["kind"] == "job"
    assert envelope["job"]["type"] == "inventory.collect"
    assert envelope["job"]["device"] == str(device_id)
    assert verify_signature(
        enrolled["job_signing_public_key_pem"],
        canonical_bytes(envelope["job"]),
        envelope["sig"],
    )
    await asyncio.sleep(0.3)  # let the gateway record the disconnect

    async with websockets.connect(f"wss://localhost:{port}/agent", ssl=ctx) as ws:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=1)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        jobs = (
            await session.execute(
                select(Job).where(Job.type == "inventory.collect", Job.target_id == device_id)
            )
        ).scalars().all()
    assert len(jobs) == 1
