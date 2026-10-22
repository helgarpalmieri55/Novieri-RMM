#!/usr/bin/env python3
"""Phase 0 kernel demo, driven by the REAL compiled Go agent.

Unlike scripts/demo_e2e.py (which uses an inline fake Python agent), this boots
the API on a real TCP port and the mTLS gateway, then runs the agent binary
built from `agent/` in this repository, end to end:

    the agent enrolls, connects outbound over mutual TLS, and runs
    server-SIGNED `hello` and `inventory.collect` jobs — verifying the
    signature/device/freshness itself in Go before executing.

This is the cross-language proof: the Go agent's canonical JSON and Ed25519
verification agree with the Python signer over the wire. It is worth running
precisely because neither test suite can produce it — `pytest` exercises the
Python signer against a Python reader, and `go test` exercises the Go verifier
against Go-built fixtures.

Usage:
    scripts/build_agent.sh                       # -> agent/dist/its-agent
    backend/.venv/bin/python scripts/demo_e2e_agent.py

Requires: local Postgres 16 (superuser 'postgres' via `sudo -u postgres psql`),
Redis on localhost:6379, and a compiled agent binary. That binary defaults to
`agent/dist/its-agent` relative to the repo root — the agent is IN THIS TREE
under `agent/` (`CLAUDE.md` commit `a32cead`), so there is nothing to fetch from
elsewhere. Override with ITS_AGENT_BIN to test a binary built for a tag or a
different platform.
"""

import asyncio
import os
import socket
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "backend" / "tests"))

AGENT_BIN = os.environ.get("ITS_AGENT_BIN", str(REPO_ROOT / "agent" / "dist" / "its-agent"))


def step(msg: str) -> None:
    print(f"\n=== {msg}")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _wait_job(client, job_id: str, headers, *, timeout: float = 15.0) -> dict:
    import asyncio as _a

    deadline = _a.get_event_loop().time() + timeout
    while _a.get_event_loop().time() < deadline:
        final = (await client.get(f"/api/v1/jobs/{job_id}", headers=headers)).json()
        if final["status"] in ("succeeded", "failed"):
            return final
        await _a.sleep(0.25)
    raise TimeoutError(f"job {job_id} did not complete within {timeout}s")


async def main() -> None:
    import httpx
    import redis.asyncio as aioredis
    import uvicorn
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.ca import LocalCA
    from app.core.config import Settings
    from app.core.signing import LocalEd25519Signer
    from app.gateway.main import GatewayServer
    from app.jobs.engine import JobEngine
    from app.main import create_app
    from app.models import Organization, Role, User, UserRole
    from conftest import PgCluster, _psql, _run_migrations
    from fake_idp import ISSUER, JWKS_URL, FakeIdp, jwks_client

    if not Path(AGENT_BIN).exists():
        sys.exit(
            f"agent binary not found at {AGENT_BIN}.\n"
            "Build it from this repository:  scripts/build_agent.sh\n"
            "(or point ITS_AGENT_BIN at one you built elsewhere)"
        )

    tmp = Path(tempfile.mkdtemp(prefix="its-agent-demo-"))
    step("provisioning throwaway database (real Postgres, RLS enforced)")
    import secrets as pysecrets

    dbname = f"its_agent_demo_{pysecrets.token_hex(4)}"
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
    _psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='portal_user') THEN "
        "CREATE ROLE portal_user LOGIN PASSWORD 'portal' NOSUPERUSER NOBYPASSRLS; "
        "ELSE ALTER ROLE portal_user LOGIN PASSWORD 'portal'; END IF; END $$;"
    )
    _psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO portal_user', dbname)
    cluster = PgCluster(
        dbname=dbname,
        app_url=f"postgresql+asyncpg://app_user:app@127.0.0.1:5432/{dbname}",
        admin_url=f"postgresql+asyncpg://its_admin:its_admin_test@127.0.0.1:5432/{dbname}",
        # PgCluster gained `portal_url` with the end-user portal (migration
        # 0028) and this script was not updated, so it had been failing at
        # startup with a TypeError -- before reaching a single line of agent
        # code. Found while running Task 10 step 5.
        portal_url=f"postgresql+asyncpg://portal_user:portal@127.0.0.1:5432/{dbname}",
        app_url_sync="",
        admin_url_sync="",
    )
    _run_migrations(cluster)
    print(f"    database {dbname} migrated to head")

    api_port = _free_port()
    gw_port = _free_port()

    idp = FakeIdp()
    settings = Settings(
        database_url=cluster.app_url,
        database_admin_url=cluster.admin_url,
        oidc_issuer=ISSUER,
        oidc_jwks_url=JWKS_URL,
        ca_dir=tmp / "ca",
        signing_key_path=tmp / "job-signing.key",
        redis_url="redis://localhost:6379/9",
        # the enrollment response hands this to the agent as its gateway URL
        gateway_url=f"wss://localhost:{gw_port}/agent",
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
    gateway = GatewayServer(
        settings=settings,
        app_engine=app_engine,
        admin_engine=admin_engine,
        job_engine=JobEngine(signer=LocalEd25519Signer(settings.signing_key_path)),
        redis=redis,
    )
    handle = await gateway.start(
        host="localhost",
        port=gw_port,
        certfile=str(tmp / "gw.crt"),
        keyfile=str(tmp / "gw.key"),
        ca_certs=str(settings.ca_dir / "root.crt"),
    )
    print(f"    wss://localhost:{gw_port}/agent (client certificate required)")

    step("serving operator API on a real TCP port (so the Go agent can enroll)")
    uv_config = uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
    uv_server = uvicorn.Server(uv_config)
    uv_task = asyncio.create_task(uv_server.serve())
    while not uv_server.started:
        await asyncio.sleep(0.05)
    print(f"    http://127.0.0.1:{api_port}")

    agent_proc = None
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}") as client:
            step("operator issues an enrollment token")
            token = (
                await client.post(
                    "/api/v1/enrollment-tokens",
                    json={"name": "demo", "expires_in_hours": 1},
                    headers=headers,
                )
            ).json()["token"]
            print(f"    token: {token[:16]}… (hash-stored, shown once)")

            step("launching the REAL Go agent (enroll -> mTLS connect -> serve)")
            data_dir = tmp / "agent-data"
            env = {
                **os.environ,
                "ITS_AGENT_ENROLL_URL": f"http://127.0.0.1:{api_port}",
                "ITS_AGENT_ENROLL_TOKEN": token,
                "ITS_AGENT_DATA_DIR": str(data_dir),
            }
            agent_proc = await asyncio.create_subprocess_exec(
                AGENT_BIN,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            # wait for the device to come online (agent enrolled + connected)
            device_id = await _wait_online(client, headers)
            print(f"    device {device_id} enrolled and online")

            step("operator dispatches a signed `hello` job")
            hello_job = (
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
            final = await _wait_job(client, hello_job["id"], headers)
            print(f"    hello: status={final['status']} result={final['result']}")
            assert final["status"] == "succeeded", final
            assert final["result"].get("greeting") == "hello from the control plane", final

            step("operator dispatches a signed `inventory.collect` job")
            inv_job = (
                await client.post(
                    "/api/v1/jobs",
                    json={"type": "inventory.collect", "device_id": device_id, "params": {}},
                    headers=headers,
                )
            ).json()
            final = await _wait_job(client, inv_job["id"], headers)
            print(f"    inventory: status={final['status']}")
            assert final["status"] == "succeeded", final

            step("operator reads the ingested inventory")
            dev = (await client.get(f"/api/v1/devices/{device_id}", headers=headers)).json()
            print(f"    device hostname={dev.get('hostname')} os_family={dev.get('os_family')}")

            step("audit chain verification")
            verify = (await client.get("/api/v1/audit/verify", headers=headers)).json()
            print(f"    ok={verify['ok']} events={verify['count']}")
            assert verify["ok"]

        print(
            f"\n✅ Go-agent kernel demo complete: real binary enrolled, connected over "
            f"mTLS, and ran signed hello + inventory jobs end-to-end. (org {org_id})"
        )
    finally:
        if agent_proc is not None and agent_proc.returncode is None:
            agent_proc.terminate()
            try:
                out, _ = await asyncio.wait_for(agent_proc.communicate(), timeout=5)
                if out:
                    print("\n--- agent log ---")
                    print(out.decode(errors="replace").rstrip())
            except asyncio.TimeoutError:
                agent_proc.kill()
        uv_server.should_exit = True
        await uv_task
        handle.close()
        await handle.wait_closed()
        await gateway.stop()
        await redis.aclose()
        await app_engine.dispose()
        await admin_engine.dispose()
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def _wait_online(client, headers, *, timeout: float = 20.0) -> str:
    """Poll the device list until the agent has enrolled and connected."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get("/api/v1/devices", headers=headers)
        if resp.status_code == 200:
            for dev in resp.json()["items"]:
                if dev.get("agent_status") == "online":
                    return dev["id"]
        await asyncio.sleep(0.4)
    raise TimeoutError("agent did not enroll/connect in time")


if __name__ == "__main__":
    asyncio.run(main())
