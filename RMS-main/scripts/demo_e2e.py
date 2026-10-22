#!/usr/bin/env python3
"""Phase 0 kernel demo: enroll a fake agent, dispatch a signed job, verify.

Boots the API and mTLS gateway in-process against the local dev services
(PostgreSQL + Redis), using the local dev CA, the local Ed25519 signer, and a
fake OIDC IdP — then walks the whole kernel loop, printing each step:

    backend/.venv/bin/python scripts/demo_e2e.py

Requires: local Postgres 16 (with a superuser 'postgres' reachable via
`sudo -u postgres psql`) and Redis on localhost:6379 — same prerequisites as
the backend test suite.
"""

import asyncio
import json
import socket
import ssl
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "backend" / "tests"))


def step(msg: str) -> None:
    print(f"\n=== {msg}")


async def main() -> None:
    import httpx
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
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.ca import LocalCA
    from app.core.config import Settings
    from app.core.signing import LocalEd25519Signer, verify_signature
    from app.gateway.main import GatewayServer
    from app.jobs.canonical import canonical_bytes
    from app.jobs.engine import JobEngine
    from app.main import create_app
    from app.models import Organization, Role, User, UserRole
    from conftest import PgCluster, _psql, _run_migrations  # test harness reuse
    from fake_idp import ISSUER, JWKS_URL, FakeIdp, jwks_client
    from sqlalchemy import select

    tmp = Path(tempfile.mkdtemp(prefix="its-demo-"))
    step("provisioning throwaway database (real Postgres, RLS enforced)")
    import secrets as pysecrets

    dbname = f"its_demo_{pysecrets.token_hex(4)}"
    _psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app_user') THEN "
        "CREATE ROLE app_user LOGIN PASSWORD 'app' NOSUPERUSER NOBYPASSRLS; END IF; END $$;"
    )
    _psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='its_admin') THEN "
        "CREATE ROLE its_admin LOGIN PASSWORD 'its_admin_test' NOSUPERUSER CREATEDB "
        "CREATEROLE; END IF; END $$;"
    )
    _psql("ALTER ROLE its_admin BYPASSRLS")
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    _psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO app_user', dbname)
    cluster = PgCluster(
        dbname=dbname,
        app_url=f"postgresql+asyncpg://app_user:app@127.0.0.1:5432/{dbname}",
        admin_url=f"postgresql+asyncpg://its_admin:its_admin_test@127.0.0.1:5432/{dbname}",
        app_url_sync="",
        admin_url_sync="",
    )
    _run_migrations(cluster)
    print(f"    database {dbname} migrated to head")

    idp = FakeIdp()
    settings = Settings(
        database_url=cluster.app_url,
        database_admin_url=cluster.admin_url,
        oidc_issuer=ISSUER,
        oidc_jwks_url=JWKS_URL,
        ca_dir=tmp / "ca",
        signing_key_path=tmp / "job-signing.key",
        redis_url="redis://localhost:6379/8",
    )
    app = create_app(settings, jwks_http_client=jwks_client(idp))
    app_engine = create_async_engine(cluster.app_url)
    admin_engine = create_async_engine(cluster.admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    await redis.flushdb()

    step("creating organization 'toeshee' and an org-admin operator")
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name="Toeshee", slug="toeshee")
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject="demo-admin", email="admin@toeshee.test")
        session.add(user)
        await session.flush()
        role = (
            await session.execute(
                select(Role).where(Role.name == "org-admin", Role.org_id.is_(None))
            )
        ).scalar_one()
        session.add(UserRole(org_id=org.id, user_id=user.id, role_id=role.id))
        await session.commit()
        org_id = org.id
    headers = {"Authorization": f"Bearer {idp.token('demo-admin', 'toeshee')}"}

    step("starting mTLS gateway")
    ca = LocalCA(settings.ca_dir)
    key_pem, cert_pem = ca.issue_server_cert(["localhost"])
    (tmp / "gw.key").write_text(key_pem)
    (tmp / "gw.crt").write_text(cert_pem)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    gateway = GatewayServer(
        settings=settings,
        app_engine=app_engine,
        admin_engine=admin_engine,
        job_engine=JobEngine(signer=LocalEd25519Signer(settings.signing_key_path)),
        redis=redis,
    )
    handle = await gateway.start(
        host="localhost",
        port=port,
        certfile=str(tmp / "gw.crt"),
        keyfile=str(tmp / "gw.key"),
        ca_certs=str(settings.ca_dir / "root.crt"),
    )
    print(f"    wss://localhost:{port}/agent (client certificate required)")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.demo"
    ) as client:
        step("operator issues an enrollment token")
        token = (
            await client.post(
                "/api/v1/enrollment-tokens",
                json={"name": "demo", "expires_in_hours": 1},
                headers=headers,
            )
        ).json()["token"]
        print(f"    token: {token[:16]}… (hash-stored, shown once)")

        step("agent generates a keypair + CSR and enrolls")
        agent_key = Ed25519PrivateKey.generate()
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "demo-agent")]))
            .sign(agent_key, None)
        )
        enrolled = (
            await client.post(
                "/enroll/v1",
                json={
                    "token": token,
                    "csr_pem": csr.public_bytes(Encoding.PEM).decode(),
                    "device_facts": {
                        "hostname": "demo-endpoint",
                        "os_family": "linux",
                        "os_version": "Ubuntu 24.04",
                        "architecture": "x86_64",
                    },
                },
            )
        ).json()
        device_id = enrolled["device_id"]
        print(f"    device {device_id} enrolled; cert + pinned signing key received")

        step("agent connects outbound over mutual TLS")
        (tmp / "agent.crt").write_text(enrolled["cert_pem"])
        (tmp / "agent.key").write_bytes(
            agent_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        )
        (tmp / "agent-ca.pem").write_text(enrolled["ca_bundle_pem"])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_cert_chain(tmp / "agent.crt", tmp / "agent.key")
        ctx.load_verify_locations(tmp / "agent-ca.pem")

        async with websockets.connect(f"wss://localhost:{port}/agent", ssl=ctx) as ws:
            await asyncio.sleep(0.4)
            step("operator dispatches a signed `hello` job from the console API")
            job = (
                await client.post(
                    "/api/v1/jobs",
                    json={
                        "type": "hello",
                        "device_id": device_id,
                        "params": {"message": "hello from the control plane"},
                    },
                    headers=headers,
                )
            ).json()
            print(f"    job {job['id']} queued and signed")

            envelope = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            payload = envelope["job"]
            step("agent verifies the job before running (signature/device/freshness)")
            assert verify_signature(
                enrolled["job_signing_public_key_pem"],
                canonical_bytes(payload),
                envelope["sig"],
            ), "signature verification failed"
            assert payload["device"] == device_id
            assert datetime.fromisoformat(payload["expires_at"]) > datetime.now(UTC)
            print("    signature OK, device identity OK, freshness OK, nonce fresh")

            await ws.send(
                json.dumps(
                    {"v": 1, "kind": "job_status", "job_id": job["id"], "state": "running"}
                )
            )
            await ws.send(
                json.dumps(
                    {
                        "v": 1,
                        "kind": "job_result",
                        "job_id": job["id"],
                        "state": "succeeded",
                        "result": {"echo": payload["params"]["message"]},
                    }
                )
            )
            await asyncio.sleep(0.5)

        step("operator sees the verified result")
        final = (await client.get(f"/api/v1/jobs/{job['id']}", headers=headers)).json()
        print(f"    status={final['status']} result={final['result']}")

        step("audit chain verification")
        verify = (await client.get("/api/v1/audit/verify", headers=headers)).json()
        print(f"    ok={verify['ok']} events={verify['count']}")
        assert final["status"] == "succeeded" and verify["ok"]

    handle.close()
    await handle.wait_closed()
    await gateway.stop()
    await redis.aclose()
    await app_engine.dispose()
    await admin_engine.dispose()
    _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    print(f"\nKernel demo complete: signed job round-trip verified end-to-end. (org {org_id})")


if __name__ == "__main__":
    asyncio.run(main())
