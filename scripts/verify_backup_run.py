#!/usr/bin/env python3
"""Live measurement of `backup.run` against REAL object storage.

Everything about the backup write path was, until this script, solved on
paper: unit-tested against fakes, an in-process HTTP server and `moto`. None
of that can measure the thing that matters. **`moto` does not enforce S3
POST-policy conditions** — measured on this project, not assumed: it accepts
an upload whose `key` violates the signed policy and answers `204`. So a
"cross-prefix upload is rejected" test passes against a grant carrying no
conditions at all. Real MinIO enforces them, which is why this script exists
and why it will not run against anything else.

WHAT IS REAL HERE, AND WHAT IS NOT
==================================

Real: MinIO behind Traefik on `:443` (the `deploy/docker-compose.yml`
services, their own labels and their own routing rule), the operator-facing
TLS listener, the compiled Go agent binary from `agent/`, a server-signed
`backup.run` job the agent verifies in Go before executing, the mTLS gateway,
Postgres with RLS, and Redis.

NOT real, and named rather than hidden:

* **The control plane runs IN-PROCESS**, not as the `api`/`gateway`
  containers — `scripts/demo_e2e_agent.py`'s precedent. It is the same code
  from the same tree; it is not the same deployment artifact.
* **The certificate is self-signed** and trusted because it was installed
  into this host's trust store. `agent/internal/backupstore` deliberately
  uses SYSTEM trust roots, so the decision is exercised — not proven against
  a publicly-issued chain on the operator's real hostname.
* **This is Linux.** No Windows and no macOS endpoint has ever run the agent.
* **This script itself runs nowhere in CI.** CI's `agent` job DOES run
  `agent/`'s unit, schema and idle-budget suites, with a real Go toolchain, on
  every push -- that job predates this branch. This script is the live
  measurement against real MinIO, and it is manual and Linux-only: a
  regression in what it checks returns silently until somebody runs it.

WHAT THIS SCRIPT FOUND, AND WHY IT IS THE POINT
===============================================

`backup.run` does NOT complete end to end. Two independent blockers, both
found by running this and neither visible to any test in the tree:

1. **No snapshot provider exists.** `agent/cmd/agent/main.go` binds
   `backup.Options{QueryChunks, OpenStore, Logger}` and leaves
   `OpenSnapshot` nil, so `defaultOpenSnapshot` answers — and it REFUSES all
   three kinds (`vss`, `lvm`, `apfs`), deliberately and with a documented
   reason. `run.go` resolves the snapshot BEFORE it opens the store, so a
   stock binary refuses every `backup.run` before a single chunk query
   leaves the machine, on every platform. Observed here as
   `status=refused`, "backup.run: no LVM snapshot provider in this build".

2. **The dedup exchange deadlocks against the agent's transport.**
   `jobs.Runner.Handle` runs on the transport's READ goroutine and blocks
   for the whole capability — stated in its own doc comment, and deliberate,
   so that job ordering is the wire's ordering. The exchange needs the
   opposite: `backup.run` sends a `backup_chunk_query` and waits for the
   `backup_chunk_grant` ON THE SAME CONNECTION, whose only reader is the
   goroutine the run is occupying. Measured with a build that gets past (1):
   the server answers correctly and publishes the grant, the grant sits
   unread on the socket for the full 60s `chunkreply` deadline, the run
   fails, `Handle` returns, and only THEN does the agent read it and log
   `dropped a backup chunk grant that nothing was waiting for`. A blocked
   reader also sends no WebSocket pong, so the gateway closes the connection
   at the same moment.

   Every test of this exchange injects `Options.QueryChunks` and answers it
   in-process, so none of them can see it. That is this project's
   fixture-blindness trap in its purest form: the seam under test was never
   connected to the thing that makes it hard.

Blocker (2) is a design question, not a patch, and this script does not
pretend to answer it. Pass `--agent-bin` at a build that differs only in
having a snapshot provider to reproduce (2) directly.

THE RESTORE (READ) LEG BELOW HAS NEVER BEEN EXECUTED, IN ANY ENVIRONMENT
=========================================================================

Task 9 added a `backup.restore` leg after the write measurement: dispatch a
restore of the point the write leg produced into a scratch directory, compare
the restored tree against the seeded one BYTE FOR BYTE (file count and total
bytes included), sign a GET with `presign_backup_read`-style parameters and
confirm 200, retarget that same signed GET at a different key and confirm
MinIO's signature verification answers 403 -- which is grant-REUSE
resistance, NOT prefix enforcement; reads have no MinIO-side equivalent of
the write leg's `starts-with $key` policy, so the read prefix boundary is
application-level and this stack cannot test it -- and count the
`backup_read_grant` frames observed on the wire to confirm the read exchange
BATCHED (`ceil(distinct chunks / 256)` requests) rather than fell back to one
grant per object.

That leg is written for the same real stack as everything above -- it is not
a second, easier harness -- but this container has no Docker daemon
(`docker info` fails here), so the compose stack cannot come up and nothing
in THIS script, restore leg included, has run in this environment. The write
leg's own findings above (blockers 1 and 2) are prior results, carried in
this docstring from an environment where the stack *did* come up.

AMENDED 2026-08-14 -- PART OF THE READ LEG HAS NOW EXECUTED, ELSEWHERE.
The paragraph above said the read leg "has never been executed, in any
environment" and that it "has produced neither" a PASS nor a FAIL. Half of
that is now out of date, and the half that is not is the more interesting
half.

MinIO is a single static binary and needs no Docker. The read-grant
assertions that depend only on real object storage and real SigV4 were
therefore separated out into `scripts/verify_backup_read_grants.py` and RUN:
a grant is honoured and returns its own bytes; a batch yields one grant per
suffix, each resolving to its own object; the same signature retargeted at
another key is refused 403 while that key reads fine under its own grant (so
the refusal is signature verification, not a 404); a 1-second grant works and
then expires; and one traversal suffix refuses the whole batch. 7/7 PASS
against MinIO RELEASE.2025-09-07. The checks were themselves falsified by
planting a `_backup_key` that ignores its suffix, which turned two of them
red.

STILL NEVER EXECUTED, and the reason is unchanged: the byte-for-byte
restored-tree comparison, and the `ceil(distinct chunks / 256)` batching
count on the wire. Both need the agent, a snapshot provider and the gateway,
and `backup.run` still does not complete end to end -- blockers 1 and 2 are
neither reproduced nor resolved by any of this. Do not read the amendment
above as the read leg having passed; read it as the object-store half of it
having passed and the agent half being exactly where it was.

BRINGING THE STACK UP
=====================

    service postgresql start
    redis-cli ping || redis-server --daemonize yes

    # the operator's certificate for :443 (any cert this host trusts will do
    # for a measurement; see deploy/traefik/dynamic.yml for the filenames)
    #   deploy/certs/fullchain.pem
    #   deploy/certs/privkey.pem

    cd deploy && docker compose up -d && docker compose ps

Only `minio` and `traefik` are load-bearing for this script; the rest of the
stack may be up or down. MinIO publishes no host port on purpose, so the
control-plane-side endpoint defaults to the container's own address on the
Docker bridge, which the host can reach directly:

    docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
        it-suite-minio-1

RUNNING IT
==========

    scripts/build_agent.sh
    backend/.venv/bin/python scripts/verify_backup_run.py

Every step prints PASS or FAIL, the script exits non-zero if any failed, and
the closing report says which steps were driven end to end and which were
not. A step that could not be driven is a finding, not a footnote.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "backend" / "tests"))

DEFAULT_AGENT_BIN = str(REPO_ROOT / "agent" / "dist" / "its-agent")
DEFAULT_MINIO_CONTAINER = "it-suite-minio-1"
DEFAULT_PUBLIC_ENDPOINT = "https://localhost"
DEFAULT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

# The seeded tree. Four DISTINCT files, each well under the chunker's `min`
# (524288) so each is exactly one chunk, plus one BYTE-IDENTICAL copy of the
# first. So: 5 files, 4 distinct chunks, and one of the five deduplicates
# inside the run itself. A fixture where every file were distinct could not
# tell "dedup works" from "dedup never fires".
SEED_FILE_BYTES = 64 * 1024
SEED_DISTINCT_FILES = 4

CHECKS: list[tuple[str, bool, str]] = []
# What the closing report is built from: a step is only "driven" if it
# actually executed against the real stack. A step that could not be reached
# is recorded here with the reason, and is NOT silently omitted.
DRIVEN: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    return ok


def drove(name: str, yes: bool, detail: str = "") -> None:
    DRIVEN.append((name, yes, detail))


def step(msg: str) -> None:
    print(f"\n== {msg}")


def psql(sql: str, dbname: str = "postgres") -> str:
    return subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-X", "-A", "-t", "-d", dbname, "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _discover_minio_endpoint(container: str) -> str | None:
    """MinIO's address on the Docker bridge.

    The compose file publishes NO host port for MinIO — deliberately, since
    the only public route is Traefik's presign-only rule on :443. The host
    can still reach the container directly on the bridge, so an in-process
    control plane needs no override and no deviation from the compose file.
    """
    if shutil.which("docker") is None:
        return None
    proc = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            container,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    ip = proc.stdout.strip()
    return f"http://{ip}:9000" if proc.returncode == 0 and ip else None


# --------------------------------------------------------------------------- #
# The two negatives moto cannot express
# --------------------------------------------------------------------------- #


def _post_form(
    url: str,
    fields: dict[str, str],
    key: str,
    body: bytes,
    *,
    ca_bundle: str | None,
) -> int:
    """POST one S3 form, exactly as `agent/internal/backupstore` builds it.

    `key` is written FIRST and overrides whatever the grant's fields carry
    (boto3 returns the `${filename}` placeholder), and `file` is written LAST
    because S3 ignores every field after it. Returns the status code.
    """
    import httpx

    parts: list[tuple[str, tuple[str | None, bytes | str, str | None]]] = [
        ("key", (None, key, None))
    ]
    for name, value in fields.items():
        if name == "key":
            continue
        parts.append((name, (None, value, None)))
    parts.append(("file", ("object", body, "application/octet-stream")))

    verify: Any = ca_bundle if ca_bundle else True
    with httpx.Client(verify=verify, timeout=120.0, trust_env=False) as client:
        response = client.post(url, files=parts)
    return response.status_code


def _get_status(url: str, *, ca_bundle: str | None) -> int:
    """GET one URL exactly as issued, and return only the status code.

    No retry, no follow-redirects override: a signed GET is a bearer
    credential for its lifetime and this probes exactly the request an
    endpoint would make with it.
    """
    import httpx

    verify: Any = ca_bundle if ca_bundle else True
    with httpx.Client(verify=verify, timeout=30.0, trust_env=False) as client:
        response = client.get(url)
    return response.status_code


def _swap_object_key(url: str, bucket: str, new_key: str) -> str:
    """Retarget a signed GET at a DIFFERENT object, keeping its query-string
    signature untouched.

    This is the read-side analogue of the write leg's cross-prefix POST: the
    grant was signed for exactly one key, and using it against another is
    what a compromised or buggy client would do if it tried to reuse a
    read grant outside the object it was issued for. SigV4 binds the
    canonical request -- including the resource path -- into the signature,
    so retargeting it must make MinIO's own verification fail, independent
    of anything this codebase asserts about the URL it minted.
    """
    parts = urlsplit(url)
    new_path = f"/{bucket}/{new_key}"
    return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))


# --------------------------------------------------------------------------- #
# Provisioning
# --------------------------------------------------------------------------- #


def _seed_tree(root: Path) -> tuple[int, int, int]:
    """Write the source tree.

    Returns (files on disk, distinct chunks expected, total bytes on disk) —
    all three READ BACK from the filesystem rather than returned from the
    values that were written, so a step that silently wrote nothing reports
    zero instead of reporting what it intended.
    """
    root.mkdir(parents=True, exist_ok=True)
    first: bytes | None = None
    for index in range(SEED_DISTINCT_FILES):
        blob = secrets.token_bytes(SEED_FILE_BYTES)
        if first is None:
            first = blob
        (root / f"file-{index}.bin").write_bytes(blob)
    assert first is not None
    # The byte-identical copy. Its chunk is already `seen`, so it costs no
    # upload and no round trip — and its absence would make the dedup numbers
    # below unfalsifiable: an exchange that deduplicated nothing at all would
    # still satisfy every other assertion in this script.
    (root / "duplicate-of-file-0.bin").write_bytes(first)

    on_disk = sorted(p for p in root.iterdir() if p.is_file())
    distinct = {p.read_bytes() for p in on_disk}
    return len(on_disk), len(distinct), sum(p.stat().st_size for p in on_disk)


async def _wait_online(client: Any, headers: dict[str, str], *, timeout: float = 30.0) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        response = await client.get("/api/v1/devices", headers=headers)
        if response.status_code == 200:
            for device in response.json()["items"]:
                if device.get("agent_status") == "online":
                    return str(device["id"])
        await asyncio.sleep(0.4)
    raise TimeoutError("the agent did not enroll and connect in time")


# Every terminal `JobStatus`. `refused` belongs here and is easy to omit: an
# agent that DECLINES a job (no snapshot provider, no transport) lands on
# `refused`, not `failed`, so a waiter watching only succeeded/failed hangs on
# exactly the outcome this script most needs to report.
TERMINAL_JOB_STATUSES = ("succeeded", "failed", "expired", "refused")


async def _wait_job(client: Any, job_id: str, headers: dict[str, str], *, timeout: float) -> dict:
    """Poll until terminal, then RETURN THE JOB EITHER WAY.

    A timeout is not raised. A job stuck non-terminal is itself a result worth
    reporting, and raising here would abort the script before the steps that
    explain WHY — the audited refusal reason, which is the only place a
    deliberately silent exchange is legible.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    final: dict = {"status": "unknown"}
    while asyncio.get_event_loop().time() < deadline:
        final = dict((await client.get(f"/api/v1/jobs/{job_id}", headers=headers)).json())
        if final["status"] in TERMINAL_JOB_STATUSES:
            return final
        await asyncio.sleep(0.3)
    print(f"    job {job_id} was still {final.get('status')!r} after {timeout}s")
    return final


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument(
        "--agent-bin",
        default=os.environ.get("ITS_AGENT_BIN", DEFAULT_AGENT_BIN),
        help="the compiled agent binary to drive (default: agent/dist/its-agent)",
    )
    parser.add_argument(
        "--s3-endpoint",
        default=os.environ.get("ITS_VERIFY_S3_ENDPOINT", ""),
        help=(
            "where the CONTROL PLANE reaches MinIO. Default: the compose "
            f"container's address on the Docker bridge ({DEFAULT_MINIO_CONTAINER})"
        ),
    )
    parser.add_argument(
        "--s3-public-endpoint",
        default=os.environ.get("ITS_VERIFY_S3_PUBLIC_ENDPOINT", DEFAULT_PUBLIC_ENDPOINT),
        help="where an ENDPOINT reaches the same bucket, through Traefik on :443",
    )
    parser.add_argument(
        "--ca-bundle",
        default=os.environ.get("ITS_VERIFY_CA_BUNDLE", DEFAULT_CA_BUNDLE),
        help="CA bundle this script verifies the :443 listener against",
    )
    parser.add_argument(
        "--minio-container",
        default=os.environ.get("ITS_VERIFY_MINIO_CONTAINER", DEFAULT_MINIO_CONTAINER),
    )
    parser.add_argument(
        "--job-timeout",
        type=float,
        default=float(os.environ.get("ITS_VERIFY_JOB_TIMEOUT", "180")),
    )
    return parser.parse_args(argv)


async def main(argv: list[str]) -> int:
    args = parse_args(argv)

    step("step 0: preflight -- the real stack, and nothing standing in for it")
    agent_bin = Path(args.agent_bin)
    if not check("a compiled agent binary exists", agent_bin.exists(), str(agent_bin)):
        print("\nBuild it:  scripts/build_agent.sh")
        return 1

    s3_endpoint = args.s3_endpoint or _discover_minio_endpoint(args.minio_container) or ""
    if not check(
        "MinIO's control-plane endpoint resolved",
        bool(s3_endpoint),
        s3_endpoint or f"container {args.minio_container} not found",
    ):
        print("\nBring the stack up:  cd deploy && docker compose up -d")
        return 1

    import httpx

    try:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            live = client.get(f"{s3_endpoint}/minio/health/live")
        minio_ok = live.status_code == 200
    except Exception as exc:  # noqa: BLE001
        minio_ok = False
        s3_endpoint_detail = repr(exc)
    else:
        s3_endpoint_detail = f"{s3_endpoint} -> {live.status_code}"
    if not check("MinIO answers on the control-plane endpoint", minio_ok, s3_endpoint_detail):
        return 1

    ca_bundle = args.ca_bundle if Path(args.ca_bundle).exists() else None
    public = args.s3_public_endpoint.rstrip("/")
    bucket = os.environ.get("ITS_S3_BUCKET", "it-suite")
    try:
        with httpx.Client(
            verify=ca_bundle or True, timeout=15.0, trust_env=False
        ) as client:
            routed = client.get(f"{public}/{bucket}/does-not-exist")
        # 403 AccessDenied is MinIO answering an unsigned read of a bucket
        # with no policy -- i.e. the request reached MinIO through Traefik.
        # A Traefik 404, or index.html from the `web` catch-all, would not.
        traefik_ok = routed.status_code in (403, 404) and "<Error>" in routed.text
        traefik_detail = f"{routed.status_code} {routed.text[:60]!r}"
    except Exception as exc:  # noqa: BLE001
        traefik_ok = False
        traefik_detail = repr(exc)
    if not check(
        "Traefik routes :443 to MinIO, over TLS this host trusts", traefik_ok, traefik_detail
    ):
        print(
            "\nThe endpoint-facing route is what the whole measurement rests on.\n"
            "Check deploy/certs/{fullchain,privkey}.pem and `docker compose ps`."
        )
        return 1

    # Set BEFORE any app import: `dedup.gateway_object_store()` and
    # `backup/handler.py` build their ObjectStore from `get_settings()`, which
    # reads the environment, not from any Settings object this script holds.
    os.environ["ITS_S3_ENDPOINT"] = s3_endpoint
    os.environ["ITS_S3_PUBLIC_ENDPOINT"] = public
    os.environ.setdefault("ITS_S3_ACCESS_KEY", os.environ.get("MINIO_ROOT_USER", "its-minio"))
    os.environ.setdefault(
        "ITS_S3_SECRET_KEY", os.environ.get("MINIO_ROOT_PASSWORD", "verify-local-minio")
    )
    os.environ["ITS_S3_BUCKET"] = bucket

    dbname = f"its_verify_backup_{secrets.token_hex(4)}"
    psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app_user') THEN "
        "CREATE ROLE app_user LOGIN PASSWORD 'app' NOSUPERUSER NOBYPASSRLS; END IF; END $$;"
    )
    psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='its_admin') THEN "
        "CREATE ROLE its_admin LOGIN PASSWORD 'its_admin_test' NOSUPERUSER CREATEDB "
        "CREATEROLE; END IF; END $$;"
    )
    psql("ALTER ROLE its_admin BYPASSRLS")
    psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='portal_user') THEN "
        "CREATE ROLE portal_user LOGIN PASSWORD 'portal' NOSUPERUSER NOBYPASSRLS; "
        "ELSE ALTER ROLE portal_user LOGIN PASSWORD 'portal'; END IF; END $$;"
    )
    psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO app_user', dbname)
    psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO portal_user', dbname)

    try:
        return await run(args, dbname, s3_endpoint, public, bucket, ca_bundle)
    finally:
        await asyncio.sleep(0)
        psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def run(  # noqa: PLR0915, PLR0912, C901
    args: argparse.Namespace,
    dbname: str,
    s3_endpoint: str,
    public: str,
    bucket: str,
    ca_bundle: str | None,
) -> int:
    import httpx
    import redis.asyncio as aioredis
    import uvicorn
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.ca import LocalCA
    from app.core.config import Settings, override_settings
    from app.core.signing import LocalEd25519Signer
    from app.core.storage import ObjectStore
    from app.core.tenancy import org_session
    from app.core.vault import LocalVaultService
    from app.gateway.dispatch import dispatch_channel
    from app.gateway.main import GatewayServer
    from app.jobs.engine import JobEngine
    from app.main import create_app
    from app.models import (
        BackupChunk,
        BackupPoint,
        BackupPolicy,
        BackupRun,
        Organization,
        Role,
        User,
        UserRole,
    )
    from app.modules.backup.repository import create_repository
    from conftest import PgCluster, _run_migrations
    from fake_idp import ISSUER, JWKS_URL, FakeIdp, jwks_client

    tmp = Path(tempfile.mkdtemp(prefix="its-verify-backup-"))
    cluster = PgCluster(
        dbname=dbname,
        app_url=f"postgresql+asyncpg://app_user:app@127.0.0.1:5432/{dbname}",
        admin_url=f"postgresql+asyncpg://its_admin:its_admin_test@127.0.0.1:5432/{dbname}",
        portal_url=f"postgresql+asyncpg://portal_user:portal@127.0.0.1:5432/{dbname}",
        app_url_sync="",
        admin_url_sync="",
    )
    _run_migrations(cluster)

    api_port, gw_port = _free_port(), _free_port()
    idp = FakeIdp()
    settings = Settings(
        database_url=cluster.app_url,
        database_admin_url=cluster.admin_url,
        oidc_issuer=ISSUER,
        oidc_jwks_url=JWKS_URL,
        ca_dir=tmp / "ca",
        signing_key_path=tmp / "job-signing.key",
        redis_url="redis://localhost:6379/9",
        gateway_url=f"wss://localhost:{gw_port}/agent",
        s3_endpoint=s3_endpoint,
        s3_public_endpoint=public,
        s3_bucket=bucket,
        s3_access_key=os.environ["ITS_S3_ACCESS_KEY"],
        s3_secret_key=os.environ["ITS_S3_SECRET_KEY"],
    )
    # THE GATEWAY DOES NOT USE THE Settings OBJECT ABOVE. `handle_chunk_query`
    # calls `dedup.gateway_object_store()`, and `backup/handler.py` calls it
    # again to ingest the result; both build their ObjectStore from
    # `get_settings()`, i.e. from the environment. Left alone, that
    # constructor raises inside the gateway's message handler, the agent's
    # query goes unanswered, and the run fails on the deadline looking exactly
    # like a server refusal — which is what happened the first time this was
    # run. Installing these settings as the process-wide ones makes the two
    # halves the same configuration by construction rather than by two sets of
    # environment variables that must be kept in step.
    settings_override = override_settings(settings)
    settings_override.__enter__()

    app = create_app(settings, jwks_http_client=jwks_client(idp))
    app_engine = create_async_engine(cluster.app_url)
    admin_engine = create_async_engine(cluster.admin_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    await redis.flushdb()
    store = ObjectStore(settings)
    vault = LocalVaultService(tmp / "root.key")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name="Verify", slug=f"verify-{secrets.token_hex(3)}")
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject="verify-admin", email="a@verify.test")
        session.add(user)
        await session.flush()
        role = (
            await session.execute(
                select(Role).where(Role.name == "org-admin", Role.org_id.is_(None))
            )
        ).scalar_one()
        session.add(UserRole(org_id=org.id, user_id=user.id, role_id=role.id))
        await session.commit()
        org_id, user_id = org.id, user.id
    headers = {"Authorization": f"Bearer {idp.token('verify-admin', org.slug)}"}

    ca = LocalCA(settings.ca_dir)
    key_pem, cert_pem = ca.issue_server_cert(["localhost"])
    (tmp / "gw.key").write_text(key_pem)
    (tmp / "gw.crt").write_text(cert_pem)
    job_engine = JobEngine(signer=LocalEd25519Signer(settings.signing_key_path))
    gateway = GatewayServer(
        settings=settings,
        app_engine=app_engine,
        admin_engine=admin_engine,
        job_engine=job_engine,
        redis=redis,
    )
    handle = await gateway.start(
        host="localhost",
        port=gw_port,
        certfile=str(tmp / "gw.crt"),
        keyfile=str(tmp / "gw.key"),
        ca_certs=str(settings.ca_dir / "root.crt"),
    )
    uv_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
    )
    uv_task = asyncio.create_task(uv_server.serve())
    while not uv_server.started:
        await asyncio.sleep(0.05)

    agent_proc: Any = None
    agent_log = tmp / "agent.log"
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{api_port}", trust_env=False
        ) as client:
            # ------------------------------------------------------------- 1
            step("step 1: org, enrolled device, repository, seeded source tree")
            token = (
                await client.post(
                    "/api/v1/enrollment-tokens",
                    json={"name": "verify", "expires_in_hours": 1},
                    headers=headers,
                )
            ).json()["token"]

            source_root = tmp / "source"
            file_count, expected_chunks, seeded_bytes = _seed_tree(source_root)
            check(
                "source tree seeded with a known chunk count",
                (file_count, expected_chunks, seeded_bytes)
                == (
                    SEED_DISTINCT_FILES + 1,
                    SEED_DISTINCT_FILES,
                    (SEED_DISTINCT_FILES + 1) * SEED_FILE_BYTES,
                ),
                f"{file_count} files on disk, {expected_chunks} distinct contents, "
                f"{seeded_bytes} bytes (one file is an exact duplicate of another)",
            )

            agent_proc = await asyncio.create_subprocess_exec(
                str(Path(args.agent_bin)),
                env={
                    **os.environ,
                    "ITS_AGENT_ENROLL_URL": f"http://127.0.0.1:{api_port}",
                    "ITS_AGENT_ENROLL_TOKEN": token,
                    "ITS_AGENT_DATA_DIR": str(tmp / "agent-data"),
                    # localhost is already in NO_PROXY here, but an agent must
                    # reach the object store directly regardless of what proxy
                    # variables this shell happens to carry.
                    "NO_PROXY": "localhost,127.0.0.1",
                    "no_proxy": "localhost,127.0.0.1",
                },
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            device_id = UUID(await _wait_online(client, headers))
            check("the real agent binary enrolled and connected over mTLS", True, str(device_id))

            # `_SNAPSHOT_BY_OS_FAMILY` is keyed on the device's os_family, so
            # the dispatch needs one. Set explicitly rather than waiting for an
            # inventory run: this script measures storage, and a KeyError from
            # an un-inventoried device would look like a backup defect.
            async with AsyncSession(admin_engine) as session:
                await session.execute(
                    text("UPDATE devices SET os_family = 'linux' WHERE id = :d"),
                    {"d": device_id},
                )
                await session.commit()

            async with org_session(org_id, engine=app_engine) as session:
                repo = await create_repository(
                    session, vault, org_id=org_id, actor_id=user_id, name="verify-repo"
                )
                repository_id = repo.id
                storage_ref = repo.storage_ref
                session.add(
                    BackupPolicy(
                        org_id=org_id,
                        repository_id=repository_id,
                        device_group_id=None,
                        sources={"paths": [str(source_root)]},
                        window_cron=None,
                        retention_gfs={},
                        enabled=True,
                    )
                )
            check(
                "backup repository created, prefix derived",
                storage_ref == f"{org_id}/backup/{repository_id}",
                storage_ref,
            )

            # ------------------------------------------------------------- 2
            step("step 2: dispatch a real signed `backup.run`, and watch the wire")
            channel = dispatch_channel(device_id)
            pubsub = redis.pubsub()
            await pubsub.subscribe(channel)
            await asyncio.sleep(0.2)

            job = await _dispatch_and_wait(
                client,
                headers,
                redis,
                app_engine,
                job_engine,
                vault,
                org_id=org_id,
                device_id=device_id,
                timeout=args.job_timeout,
            )
            grants = await _drain_grants(pubsub)
            state = (job.get("result") or {}).get("state")
            check(
                "the job reached a terminal state",
                job["status"] in TERMINAL_JOB_STATUSES,
                f"status={job['status']} state={state}",
            )
            run_succeeded = job["status"] == "succeeded" and state == "success"
            if not run_succeeded:
                error = (job.get("result") or {}).get("error") or job.get("error")
                check(
                    "backup.run completed (state=success)",
                    False,
                    f"status={job['status']} state={state} error={str(error)[:400]}",
                )
                drove(
                    "backup.run end to end",
                    False,
                    f"status={job['status']}: {str(error)[:220]}",
                )
                if job["status"] == "refused":
                    print(
                        "\n  A REFUSAL IS THE AGENT DECLINING TO ACT, not a transient.\n"
                        "  `cmd/agent/main.go` binds backup.Options{QueryChunks, OpenStore,\n"
                        "  Logger} and leaves OpenSnapshot nil, and defaultOpenSnapshot\n"
                        "  refuses vss, lvm AND apfs. run.go resolves the snapshot BEFORE it\n"
                        "  opens the store, so a stock binary refuses every backup.run before\n"
                        "  one chunk query leaves the machine -- on every platform, not only\n"
                        "  this one. Everything below that needs a completed run will now\n"
                        "  fail, and it is that gap failing, not the storage path."
                    )
            else:
                check("backup.run completed (state=success)", True, "")
                drove("backup.run end to end", True, "real signed job, real agent binary")

            # ------------------------------------------------------------- 3
            step("step 3: the dedup exchange crossed the wire")
            # A refusal is SILENT on the wire by design, so the only place it
            # is legible is the audit log. Printed unconditionally: without
            # this, "0 grants" and "the server refused for reason X" look
            # identical from here, and the whole point of the exchange's
            # silence is that the agent cannot tell them apart either.
            async with AsyncSession(admin_engine) as session:
                refusals = (
                    (
                        await session.execute(
                            text(
                                "SELECT metadata ->> 'reason' FROM audit_events"
                                " WHERE org_id = :org AND action = 'backup.dedup_refused'"
                            ),
                            {"org": org_id},
                        )
                    )
                    .scalars()
                    .all()
                )
            print(f"    audited dedup refusals: {list(refusals) or 'none'}")
            answered = check(
                "at least one backup_chunk_query was answered by the server",
                len(grants) >= 1,
                f"{len(grants)} backup_chunk_grant frames published on {channel}",
            )
            # TWO SEPARATE FACTS, deliberately not one check. "The server
            # answered" and "the agent acted on the answer" are different
            # claims, and collapsing them is what would have hidden the
            # deadlock below: the server answers every query correctly and the
            # agent never reads a single reply.
            acted = check(
                "the agent ACTED on the grant (it uploaded and reported chunks)",
                run_succeeded,
                "the run completed" if run_succeeded else "the run did not complete",
            )
            if answered and not acted:
                print(
                    "\n  THE SERVER ANSWERED AND THE AGENT DID NOT ACT ON IT.\n"
                    "  `jobs.Runner.Handle` runs on the transport's READ goroutine and\n"
                    "  blocks for the whole capability -- stated in its own doc comment,\n"
                    "  and deliberate, so that job ordering is the wire's ordering. The\n"
                    "  dedup exchange needs the opposite: `backup.run` sends a query and\n"
                    "  waits for a reply ON THE SAME CONNECTION, whose only reader is the\n"
                    "  goroutine the run is occupying. The grant sits unread on the socket\n"
                    "  until chunkreply's 60s deadline fires and the run gives up; the\n"
                    "  agent then reads it and logs `dropped a backup chunk grant that\n"
                    "  nothing was waiting for`. Control frames are handled by the same\n"
                    "  reader, so a blocked one sends no WebSocket pong either and the\n"
                    "  gateway closes the connection at about the same moment.\n"
                    "  Every test of this exchange injected `Options.QueryChunks` and\n"
                    "  answered it in-process, so none of them could see it."
                )
            drove(
                "the dedup exchange (backup_chunk_query -> backup_chunk_grant)",
                answered and acted,
                f"{len(grants)} grants published, agent acted: {acted}",
            )
            async with AsyncSession(admin_engine) as session:
                chunk_rows = (
                    (
                        await session.execute(
                            select(BackupChunk.chunk_id).where(
                                BackupChunk.org_id == org_id,
                                BackupChunk.repository_id == repository_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            check(
                "backup_chunks rows exist for the repository",
                len(chunk_rows) == expected_chunks,
                f"{len(chunk_rows)} rows, expected {expected_chunks}",
            )

            # ------------------------------------------------------------- 4
            step("step 4: the objects landed under the repository's prefix")
            chunk_keys = _list_keys(store, f"{storage_ref}/chunks/")
            manifest_keys = _list_keys(store, f"{storage_ref}/manifests/")
            landed = check(
                "chunk objects under {org}/backup/{repo}/chunks/",
                len(chunk_keys) == expected_chunks,
                f"{len(chunk_keys)} objects, expected {expected_chunks}",
            )
            landed &= check(
                "a manifest object under {org}/backup/{repo}/manifests/",
                len(manifest_keys) == 1,
                f"{len(manifest_keys)} objects",
            )
            stray = _list_keys(store, f"{org_id}/backup/{repository_id}/")
            check(
                "nothing was written outside chunks/ and manifests/",
                len(stray) == len(chunk_keys) + len(manifest_keys),
                f"{len(stray)} objects under the prefix in total",
            )
            drove(
                "chunk and manifest objects written to real MinIO under the POST grant",
                landed,
                f"{len(chunk_keys)} chunks + {len(manifest_keys)} manifest",
            )

            # ------------------------------------------------------------- 5
            step("step 5: the BackupPoint, and its manifest_ref is fetchable")
            async with AsyncSession(admin_engine) as session:
                point = (
                    await session.execute(
                        select(BackupPoint).where(
                            BackupPoint.org_id == org_id,
                            BackupPoint.repository_id == repository_id,
                        )
                    )
                ).scalar_one_or_none()
                manifest_ref = point.manifest_ref if point is not None else ""
                point_size = point.size if point is not None else -1
                # Captured now, not dereferenced later: `point` is loaded on
                # `admin_engine`'s session, which this block closes on exit.
                # Its primitive attributes stay in memory, but the id is
                # extracted alongside its siblings for the same reason they
                # are -- so the restore leg further down never touches an
                # ORM object outside the session that loaded it.
                point_id = point.id if point is not None else None
            found_point = check(
                "a BackupPoint row exists", point is not None, str(manifest_ref)
            )
            fetched = b""
            if found_point:
                try:
                    fetched = await store.get_backup_object(manifest_ref)
                except Exception as exc:  # noqa: BLE001
                    check("get_backup_object(point.manifest_ref) returned bytes", False, repr(exc))
                else:
                    check(
                        "get_backup_object(point.manifest_ref) returned bytes",
                        len(fetched) > 0,
                        f"{len(fetched)} bytes",
                    )
                    # SEALED, not plaintext. A manifest the server could read
                    # would mean the repository key never reached the agent.
                    check(
                        "the manifest is sealed, not readable JSON",
                        not fetched.lstrip()[:1] in (b"{", b"["),
                        f"first bytes {fetched[:8]!r}",
                    )
                check(
                    "the point records the bytes actually protected",
                    point_size == seeded_bytes,
                    f"size={point_size}, seeded {seeded_bytes}",
                )
            drove(
                "BackupPoint.manifest_ref resolves to the sealed manifest",
                found_point and len(fetched) > 0,
                f"{len(fetched)} bytes",
            )

            # ------------------------------------------------------------- 6/7
            step("step 6/7: the two negatives moto cannot express, against real MinIO")
            grant = store.presign_backup_write(
                org_id, repository_id, expires_in=600, max_bytes=1024
            )
            other_repo = uuid4()
            outside_key = f"{org_id}/backup/{other_repo}/chunks/aa/{'a' * 64}"
            outside_status = _post_form(
                str(grant["url"]),
                {str(k): str(v) for k, v in dict(grant["fields"]).items()},
                outside_key,
                b"x" * 16,
                ca_bundle=ca_bundle,
            )
            check(
                "a key outside the signed prefix is refused 403 by MinIO",
                outside_status == 403,
                f"HTTP {outside_status} for {outside_key[:64]}...",
            )
            # DRIVEN unconditionally, and deliberately: `_post_form` raises if
            # it cannot reach the endpoint, so reaching this line means a real
            # POST crossed a real Traefik to a real MinIO and came back with a
            # status. Whether that status was the RIGHT one is the `check`
            # above; the two are different questions and this report answers
            # only the first.
            drove(
                "prefix enforcement (403 outside {storage_ref}/)",
                True,
                f"observed HTTP {outside_status}",
            )

            inside_key = f"{storage_ref}/chunks/bb/{'b' * 64}"
            oversize_status = _post_form(
                str(grant["url"]),
                {str(k): str(v) for k, v in dict(grant["fields"]).items()},
                inside_key,
                b"y" * 4096,
                ca_bundle=ca_bundle,
            )
            check(
                "a body over content-length-range is refused 400 by MinIO",
                oversize_status == 400,
                f"HTTP {oversize_status} for 4096 bytes against a 1024-byte cap",
            )
            drove(
                "content-length-range enforcement (400 over the cap)",
                True,
                f"observed HTTP {oversize_status}",
            )

            # A grant that is correct in both respects must still be ACCEPTED.
            # Without this the two rejections above are consistent with a route
            # that refuses everything, which is the fixture-blindness trap.
            accepted_status = _post_form(
                str(grant["url"]),
                {str(k): str(v) for k, v in dict(grant["fields"]).items()},
                f"{storage_ref}/chunks/cc/{'c' * 64}",
                b"z" * 512,
                ca_bundle=ca_bundle,
            )
            check(
                "a compliant upload under the same grant is ACCEPTED",
                accepted_status in (200, 204),
                f"HTTP {accepted_status} -- the rejections above are the policy, "
                "not a route that refuses everything",
            )
            # Written directly, so it must not be left to confuse a later step.
            await store.delete_backup_object(
                f"s3://{bucket}/{storage_ref}/chunks/cc/{'c' * 64}"
            )

            # ------------------------------------------------------------- 8
            step("step 8: a second run over UNCHANGED data must dedup")
            before_chunks = len(_list_keys(store, f"{storage_ref}/chunks/"))
            second = await _dispatch_and_wait(
                client,
                headers,
                redis,
                app_engine,
                job_engine,
                vault,
                org_id=org_id,
                device_id=device_id,
                timeout=args.job_timeout,
            )
            after_chunks = len(_list_keys(store, f"{storage_ref}/chunks/"))
            second_state = (second.get("result") or {}).get("state")
            stats = (second.get("result") or {}).get("dedup_stats") or {}
            second_ok = second["status"] == "succeeded" and second_state == "success"
            check(
                "the second run completed",
                second_ok,
                f"status={second['status']} state={second_state} "
                f"error={str((second.get('result') or {}).get('error'))[:200]}",
            )
            if second_ok:
                check(
                    "the second run uploaded no new chunks",
                    int(stats.get("chunks_uploaded", -1)) == 0,
                    f"chunks_uploaded={stats.get('chunks_uploaded')} "
                    f"chunks_deduped={stats.get('chunks_deduped')}",
                )
                check(
                    "no new chunk objects appeared in the bucket",
                    after_chunks == before_chunks,
                    f"{before_chunks} -> {after_chunks}",
                )
                check(
                    "a second manifest WAS written (the run is a new backup point)",
                    len(_list_keys(store, f"{storage_ref}/manifests/")) == 2,
                    f"{len(_list_keys(store, f'{storage_ref}/manifests/'))} manifests",
                )
            drove(
                "dedup measured over a second run of unchanged data",
                second_ok,
                f"chunks_uploaded={stats.get('chunks_uploaded')} "
                f"chunk objects {before_chunks} -> {after_chunks}",
            )

            # ------------------------------------------------------------- 9
            # THIS ENTIRE STEP HAS NEVER BEEN EXECUTED. See the module
            # docstring's "THE RESTORE (READ) LEG BELOW HAS NEVER BEEN
            # EXECUTED" section. Written for the real stack, against the
            # FIRST run's repository, device and backup point (`point_id`,
            # `storage_ref`, `device_id` above) -- not the second, dedup-only
            # run just above, which exists only to measure reuse.
            step(
                "step 9: backup.restore -- the READ leg, against real MinIO "
                "(UNEXECUTED in this environment)"
            )
            from datetime import UTC, datetime

            from app.gateway.dispatch import publish_job
            from app.modules.backup.restore import dispatch_restore

            restore_root = tmp / "restore"
            restore_root.mkdir(parents=True, exist_ok=True)

            restore_job: dict = {"status": "not_dispatched"}
            if point_id is not None:
                async with org_session(org_id, engine=app_engine) as session:
                    restore_pending = await dispatch_restore(
                        session,
                        job_engine,
                        vault,
                        org_id=org_id,
                        backup_point_id=point_id,
                        # Selecting the whole seeded source tree by naming its
                        # root: `selects()` (agent/internal/capabilities/
                        # backup/path.go) covers a manifest path `p` when
                        # `p == s or p.startswith(s + "/")`, and every seeded
                        # file's manifest path is `source_root/<name>`.
                        selection=[str(source_root)],
                        target={"path": str(restore_root)},
                        mode="restore",
                        now=datetime.now(UTC),
                        actor_id=user_id,
                        actor_type="user",
                    )
                if restore_pending:
                    r_job_id, r_target, r_envelope = restore_pending[0]
                    await publish_job(redis, r_target, r_envelope)
                    restore_job = await _wait_job(
                        client, str(r_job_id), headers, timeout=args.job_timeout
                    )

            # `backup_read_grant` frames, exactly as `_drain_grants` already
            # observes `backup_chunk_grant` ones for the write leg above --
            # same mechanism, different kind, so a restore that fell back to
            # one grant per object is visible here rather than only inferred.
            read_grant_frames = await _drain_grants(pubsub, kind="backup_read_grant")

            restore_state = (restore_job.get("result") or {}).get("state")
            restore_ok = restore_job.get("status") == "succeeded" and restore_state == "success"
            check(
                "backup.restore completed (state=success)",
                restore_ok,
                f"status={restore_job.get('status')} state={restore_state} "
                f"error={str((restore_job.get('result') or {}).get('error'))[:300]}",
            )

            # Byte-for-byte: file count, total bytes, AND per-file contents --
            # `_seed_tree`'s own three numbers are the source of truth for the
            # first two, read back from the restored tree the same way
            # `_seed_tree` reads the seeded one, rather than trusted from the
            # job result.
            restored_files = (
                sorted((p for p in restore_root.rglob("*") if p.is_file()), key=lambda p: p.name)
                if restore_root.exists()
                else []
            )
            restored_bytes_total = sum(p.stat().st_size for p in restored_files)
            seeded_files = sorted(
                (p for p in source_root.iterdir() if p.is_file()), key=lambda p: p.name
            )
            tree_matches = (
                len(restored_files) == len(seeded_files) == file_count
                and restored_bytes_total == seeded_bytes
                and all(
                    sp.name == rp.name and sp.read_bytes() == rp.read_bytes()
                    for sp, rp in zip(seeded_files, restored_files, strict=False)
                )
            )
            check(
                "the restored tree matches the seeded tree byte for byte "
                "(file count and total bytes match _seed_tree's own numbers)",
                tree_matches,
                f"{len(restored_files)} files / {restored_bytes_total} bytes restored, "
                f"expected {file_count} files / {seeded_bytes} bytes",
            )
            drove(
                "backup.restore end to end",
                restore_ok and tree_matches,
                f"status={restore_job.get('status')}",
            )

            # --------------------------------------------------------- 9a
            # The read-side counterpart of step 6/7's write negatives, and
            # per the task this leg exists for: the one assertion no
            # moto-backed unit test in this repository can make, because
            # moto does not enforce this either.
            step("step 9a: a signed GET against real MinIO -- compliant, and retargeted")
            if chunk_keys:
                real_suffix = chunk_keys[0][len(f"{storage_ref}/") :]
                read_grant = store.presign_backup_read(
                    org_id, repository_id, [real_suffix], expires_in=600
                )[0]
                compliant_status = _get_status(read_grant["url"], ca_bundle=ca_bundle)
                check(
                    "a compliant signed GET is answered 200 by MinIO",
                    compliant_status == 200,
                    f"HTTP {compliant_status} for {real_suffix}",
                )

                other_repo = uuid4()
                outside_key = f"{org_id}/backup/{other_repo}/chunks/aa/{'a' * 64}"
                retargeted_url = _swap_object_key(read_grant["url"], bucket, outside_key)
                outside_status = _get_status(retargeted_url, ca_bundle=ca_bundle)
                # WHAT THIS DOES AND DOES NOT PROVE, because the two are easy
                # to conflate and the label used to. The write leg's 403 IS
                # prefix enforcement: a POST policy carries `starts-with $key`
                # and MinIO refuses a violating key. THERE IS NO READ
                # EQUIVALENT. The bucket carries no policy, and a presigned
                # GET is signed for exactly one key, so a *validly signed* URL
                # for an out-of-prefix key would be answered 200 -- there is
                # nothing on the MinIO side for it to violate. For reads the
                # prefix boundary is entirely application-level: the control
                # plane derives the key from ids it is already authorized for
                # and refuses to SIGN anything else (answer_read_query +
                # _safe_object_suffix), which unit tests cover and this stack
                # cannot add to. What the retarget below proves is the
                # narrower, still-worth-having property that a grant cannot be
                # REUSED against another object: SigV4 binds the resource path
                # into the signature, so MinIO's own verification rejects it.
                check(
                    "the SAME signed GET, reused against a different key, fails "
                    "MinIO's signature verification with 403",
                    outside_status == 403,
                    f"HTTP {outside_status} for {outside_key[:64]}...",
                )
                drove(
                    "signed-GET reuse against real MinIO (200 for the key it "
                    "was signed for, 403 once retargeted) -- NOT prefix "
                    "enforcement, which has no read-side equivalent",
                    True,
                    f"compliant={compliant_status}, retargeted={outside_status}",
                )
            else:
                check("a compliant signed GET is answered 200 by MinIO", False,
                      "no chunk objects exist from the write leg to sign a GET against")
                check(
                    "the SAME signed GET, retargeted at a key OUTSIDE the "
                    "repository prefix, is refused 403 by MinIO",
                    False,
                    "no chunk objects exist from the write leg to sign a GET against",
                )
                drove(
                    "signed-GET prefix enforcement against real MinIO "
                    "(200 inside the repository, 403 retargeted outside it)",
                    False,
                    "no chunk objects to test against -- the write leg produced none",
                )

            # --------------------------------------------------------- 9b
            step("step 9b: the read exchange BATCHED rather than one grant per object")
            chunk_batches = sum(
                1
                for frame in read_grant_frames
                if (frame.get("grants") or [{}])[0].get("suffix", "").startswith("chunks/")
            )
            manifest_batches = len(read_grant_frames) - chunk_batches
            expected_chunk_batches = -(-expected_chunks // 256)  # ceil(chunks/256)
            check(
                "the chunk fetch was batched into ceil(distinct chunks / 256) "
                "backup_read_grant frames, not one per chunk",
                chunk_batches == expected_chunk_batches,
                f"{chunk_batches} chunk-batch frame(s) for {expected_chunks} distinct chunks "
                f"(ceil({expected_chunks}/256) = {expected_chunk_batches}); a broken fallback "
                f"to one grant per object would show {expected_chunks} here instead",
            )
            drove(
                "the read exchange ran and batched, rather than falling back "
                "to one grant per object",
                restore_ok and chunk_batches == expected_chunk_batches,
                f"{len(read_grant_frames)} backup_read_grant frames total "
                f"({chunk_batches} chunk batch(es), {manifest_batches} manifest batch(es))",
            )

            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

            async with org_session(org_id, engine=app_engine) as session:
                runs = (
                    (
                        await session.execute(
                            select(BackupRun.state).where(BackupRun.org_id == org_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            print(f"    backup_runs states: {list(runs)}")

    finally:
        if agent_proc is not None and agent_proc.returncode is None:
            agent_proc.terminate()
            try:
                out, _ = await asyncio.wait_for(agent_proc.communicate(), timeout=8)
                if out:
                    agent_log.write_bytes(out)
                    print(f"\n--- agent log ({agent_log}) ---")
                    print(out.decode(errors="replace").rstrip()[-4000:])
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
        settings_override.__exit__(None, None, None)

    # ----------------------------------------------------------------- report
    print("\n" + "=" * 72)
    print("WHAT WAS AND WAS NOT DRIVEN END TO END")
    print("=" * 72)
    for name, yes, detail in DRIVEN:
        print(f"  [{'DRIVEN    ' if yes else 'NOT DRIVEN'}] {name}" + (f" -- {detail}" if detail else ""))
    print(
        "\n  Standing limits of this measurement, which a green run does NOT remove:\n"
        "    * Linux only. No Windows or macOS endpoint has ever run the agent.\n"
        "    * No CI guards this script: CI's agent job runs agent/'s unit,\n"
        "      schema and idle-budget suites on every push, but this live\n"
        "      MinIO measurement is manual and Linux-only.\n"
        "    * The :443 certificate here is not the operator's real public\n"
        "      certificate, so backupstore's system-trust-roots decision is\n"
        "      exercised, not proven.\n"
        "    * Step 9 (backup.restore / the read leg) reuses the FIRST run's\n"
        "      point, not the dedup-measurement run in step 8.\n"
        "    * The control plane ran in-process, not as the api/gateway containers.\n"
        f"    * Agent binary: {args.agent_bin}"
    )

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n== {len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed ==")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    return 0


def _list_keys(store: Any, prefix: str) -> list[str]:
    """Every key under `prefix`, read with the control plane's own client."""
    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": store._bucket, "Prefix": prefix}  # noqa: SLF001
        if token:
            kwargs["ContinuationToken"] = token
        response = store._client.list_objects_v2(**kwargs)  # noqa: SLF001
        keys.extend(obj["Key"] for obj in response.get("Contents", []))
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")


async def _drain_grants(pubsub: Any, kind: str = "backup_chunk_grant") -> list[dict]:
    """Every `kind` frame the SERVER published for this device (default:
    `backup_chunk_grant`; the restore leg passes `backup_read_grant`).

    Read off Redis rather than instrumented into the gateway: the reply the
    agent acts on is published on this channel, so counting frames here
    observes the wire rather than a counter this script installed. It says
    what the server SENT — whether the agent ever read it is a separate
    question, and keeping the two apart is what made the defect below
    legible.

    Drains until the channel has been quiet for `quiet_for`, NOT until the
    first `None`. `get_message(ignore_subscribe_messages=True)` returns None
    for the subscribe confirmation too, so a loop that stopped at the first
    None stopped before reading anything and reported zero grants forever —
    which is exactly what it did, and it looked like evidence.
    """
    grants: list[dict] = []
    quiet_for = 1.0
    idle_since = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - idle_since < quiet_for:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.25)
        if message is None:
            continue
        idle_since = asyncio.get_event_loop().time()
        try:
            payload = json.loads(message["data"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("kind") == kind:
            grants.append(payload)
    return grants


async def _dispatch_and_wait(
    client: Any,
    headers: dict[str, str],
    redis: Any,
    app_engine: Any,
    job_engine: Any,
    vault: Any,
    *,
    org_id: UUID,
    device_id: UUID,
    timeout: float,
) -> dict:
    """One signed `backup.run`, published AFTER commit, awaited to terminal.

    Goes through `dispatch_backup` rather than `POST /api/v1/jobs`: that is
    the module's own application-security gate, and it is what writes the
    `backup_runs` row the dedup exchange resolves the query against.
    """
    from datetime import UTC, datetime

    from app.core.tenancy import org_session
    from app.gateway.dispatch import publish_job
    from app.modules.backup.dispatch import dispatch_backup

    async with org_session(org_id, engine=app_engine) as session:
        pending = await dispatch_backup(
            session,
            job_engine,
            vault,
            org_id=org_id,
            device_id=device_id,
            now=datetime.now(UTC),
        )
    if not pending:
        raise RuntimeError("dispatch_backup produced no job -- check the policy and repository")
    job_id, target, envelope = pending[0]
    await publish_job(redis, target, envelope)
    return await _wait_job(client, str(job_id), headers, timeout=timeout)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
