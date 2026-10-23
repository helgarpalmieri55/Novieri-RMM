"""Live end-to-end smoke test for the site-server module (plan Task 11).

REAL PostgreSQL, REAL Redis, REAL services. No mocks anywhere except the
object store (moto), which this module does not touch.

Run from `backend/`:
    .venv/bin/python <this file>

Exits non-zero on the first failed check and prints every check it ran, so
"the script passed" is never inferred from silence.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

BACKEND = Path(__file__).resolve()
sys.path.insert(0, "/home/user/Toeshee-RMS/backend")

import redis.asyncio as aioredis  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def psql(sql: str, dbname: str = "postgres") -> str:
    return subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-X", "-A", "-t", "-d", dbname, "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


async def main() -> int:
    dbname = f"smoke_site_{uuid4().hex[:8]}"
    admin_password = "its_admin_test"
    psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'its_admin') THEN "
        f"CREATE ROLE its_admin LOGIN PASSWORD '{admin_password}' NOSUPERUSER "
        "CREATEDB CREATEROLE; END IF; END $$;"
    )
    psql("ALTER ROLE its_admin BYPASSRLS")
    psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN "
        "CREATE ROLE app_user LOGIN PASSWORD 'app' NOSUPERUSER NOBYPASSRLS; END IF; END $$;"
    )
    psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    psql(f'GRANT CONNECT ON DATABASE "{dbname}" TO app_user', dbname)

    host = os.environ.get("PGHOST", "127.0.0.1")
    admin_url = f"postgresql+asyncpg://its_admin:{admin_password}@{host}:5432/{dbname}"
    app_url = f"postgresql+asyncpg://app_user:app@{host}:5432/{dbname}"

    print(f"\n== migrating {dbname} to head ==")
    subprocess.run(
        ["/home/user/Toeshee-RMS/backend/.venv/bin/alembic", "upgrade", "head"],
        cwd="/home/user/Toeshee-RMS/backend",
        env=os.environ | {"ITS_ALEMBIC_URL": admin_url},
        check=True,
        capture_output=True,
        text=True,
    )
    psql(
        "GRANT USAGE ON SCHEMA public TO app_user;"
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;"
        "REVOKE UPDATE, DELETE ON audit_events FROM app_user;"
        "REVOKE INSERT, UPDATE, DELETE ON permissions, role_permissions FROM app_user;"
        "REVOKE UPDATE, DELETE ON security_events FROM app_user;",
        dbname,
    )

    try:
        return await run(admin_url, app_url)
    finally:
        await asyncio.sleep(0)
        psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def run(admin_url: str, app_url: str) -> int:  # noqa: PLR0915
    from app.core.audit import audit_service
    from app.core.signing import LocalEd25519Signer
    from app.core.tenancy import org_session
    from app.gateway.dispatch import publish_job
    from app.jobs.engine import JobEngine
    from app.models import Agent, Device, JobStatus, Organization, User
    from app.modules.ipam.sweep import select_sweeper
    from app.modules.provisioning.repository import ProvisioningValidationError
    from app.modules.site_server import registry as site_registry
    from app.modules.site_server.dispatch import wake_via_site_server
    from app.modules.site_server.handler import register_job_types
    from app.modules.site_server.segments import segment_check

    register_job_types()
    admin_engine = create_async_engine(admin_url)
    app_engine = create_async_engine(app_url)
    signer = LocalEd25519Signer(Path("/tmp/smoke-site-signing.key"))
    job_engine = JobEngine(signer=signer)
    redis = aioredis.from_url(
        os.environ.get("ITS_TEST_REDIS_URL", "redis://localhost:6379/9"),
        decode_responses=True,
    )
    now = datetime.now(UTC)

    # ---------------------------------------------------------------- step 1
    print("\n== step 1: org, two devices with agents, subnet, observations ==")
    slug = f"smoke-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@t.local")
        session.add(user)
        # EXPLICIT IDS, LOW FOR THE ORDINARY DEVICE. `d.id` is the final
        # ORDER BY tie-break, and both agents share `last_seen_at`, so with
        # uuid7-generated ids the promoted device won the tie-break ANYWAY and
        # step 5's "disabled loses the preference" check could not fail. It
        # reported a FALSE FAILURE on the first run for exactly that reason --
        # the fixture, not the code. Seeding the ordinary device LOW makes the
        # preference the only thing that can put the promoted device first.
        promoted = Device(
            id=UUID("ffffffff-0000-7000-8000-000000000001"),
            org_id=org.id,
            hostname="branch-01",
            os_family="linux",
        )
        ordinary = Device(
            id=UUID("00000000-0000-7000-8000-000000000002"),
            org_id=org.id,
            hostname="desk-01",
            os_family="linux",
        )
        session.add_all([promoted, ordinary])
        await session.flush()
        for device in (promoted, ordinary):
            session.add(
                Agent(
                    org_id=org.id,
                    device_id=device.id,
                    cert_fingerprint=uuid4().hex,
                    cert_serial=uuid4().hex[:16],
                    cert_not_after=now + timedelta(days=365),
                    enrolled_at=now,
                    status="online",
                    last_seen_at=now - timedelta(minutes=5),
                )
            )
        await session.commit()
        org_id, user_id = org.id, user.id
        promoted_id, ordinary_id = promoted.id, ordinary.id

    async with AsyncSession(admin_engine) as session:
        vrf_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO ipam_vrf (id, org_id, name, description, created_at, updated_at)"
                " VALUES (:id, :org, 'global', '', now(), now())"
            ),
            {"id": vrf_id, "org": org_id},
        )
        subnet_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO ipam_subnet (id, org_id, vrf_id, prefix, kind, name,"
                "  description, dns_servers, sweep_enabled, created_at, updated_at)"
                " VALUES (:id, :org, :vrf, '10.20.7.0/24', 'assignable', 'lan', '',"
                "         '[]'::jsonb, true, now(), now())"
            ),
            {"id": subnet_id, "org": org_id, "vrf": vrf_id},
        )
        for device_id, address in ((promoted_id, "10.20.7.11"), (ordinary_id, "10.20.7.12")):
            await session.execute(
                text(
                    "INSERT INTO ipam_observation (id, org_id, vrf_id, address, source,"
                    "  method, device_id, confidence, hostname, first_seen_at, last_seen_at,"
                    "  sighting_count)"
                    " VALUES (gen_random_uuid(), :org, :vrf, CAST(:addr AS inet), 'inventory',"
                    "         'nic', :device, 'high', '', :ts, :ts, 1)"
                ),
                {"org": org_id, "vrf": vrf_id, "addr": address, "device": device_id, "ts": now},
            )
        await session.commit()
    check("both devices observed on 10.20.7.0/24", True)

    class _Subnet:
        id = subnet_id
        prefix = "10.20.7.0/24"
        sweep_device_id: UUID | None = None

    # ---------------------------------------------------------------- step 2
    print("\n== step 2: promote, declaring 10.20.0.0/16 ==")
    async with org_session(org_id, engine=app_engine) as session:
        row = await site_registry.promote(
            session,
            org_id,
            device_id=promoted_id,
            segments=["10.20.0.0/16"],
            actor_id=user_id,
        )
        site_server_id = row.id

    async with AsyncSession(admin_engine) as session:
        actions = (
            await session.execute(
                text(
                    "SELECT action FROM audit_events WHERE org_id = :org"
                    "   AND action LIKE 'site.%'"
                ),
                {"org": org_id},
            )
        ).scalars().all()
    check("site.promoted on the audit chain", list(actions) == ["site.promoted"], str(list(actions)))

    async with org_session(org_id, engine=app_engine) as session:
        verified = await audit_service.verify(session, org_id)
    check("audit chain still verifies", verified.ok, f"{verified.count} events")

    # ---------------------------------------------------------------- step 3
    print("\n== step 3: wake -> exactly one signed job, published after commit ==")
    channel = f"dispatch:{promoted_id}"
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    await asyncio.sleep(0.2)

    async with org_session(org_id, engine=app_engine) as session:
        job, envelope = await wake_via_site_server(
            session,
            job_engine,
            org_id=org_id,
            site_server_id=site_server_id,
            target_mac="A4-BB-6D-11-22-33",
            actor_id=user_id,
        )
        job_id = job.id
        # THE PUBLISH HAS NOT HAPPENED YET. This is the property the whole
        # "publish after commit" rule exists for.
        pre = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.4)
    check("nothing published while the transaction was open", pre is None, str(pre))

    await publish_job(redis, promoted_id, envelope)
    post = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
    check("envelope reached Redis after commit", post is not None)
    await pubsub.unsubscribe(channel)
    await pubsub.aclose()

    async with AsyncSession(admin_engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, type, target_type, target_id, params, signature,"
                    "       expires_at - issued_at AS ttl FROM jobs WHERE org_id = :org"
                ),
                {"org": org_id},
            )
        ).all()
    check("exactly one job row", len(rows) == 1, f"{len(rows)}")
    row = rows[0]
    check("target_type == 'device'", row.target_type == "device", row.target_type)
    check("target_id == the promoted device", row.target_id == promoted_id)
    check("signature is non-NULL", row.signature is not None)
    check("ttl == 300s", row.ttl == timedelta(seconds=300), str(row.ttl))
    check(
        "params.broadcast == 10.20.255.255",
        row.params["broadcast"] == "10.20.255.255",
        row.params["broadcast"],
    )

    # ---------------------------------------------------------------- step 4
    print("\n== step 4: complete with a matching, then a differing broadcast ==")
    async with org_session(org_id, engine=app_engine) as session:
        from app.models import Job

        job_row = await session.get(Job, job_id)
        assert job_row is not None
        await job_engine.complete(
            session,
            job_row,
            state="succeeded",
            result={
                "sent": True,
                "broadcast": "10.20.255.255",
                "sent_at": now.isoformat(),
            },
        )
    async with AsyncSession(admin_engine) as session:
        status = (
            await session.execute(
                text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}
            )
        ).scalar_one()
    check("matching broadcast -> succeeded", status == JobStatus.succeeded.value, str(status))

    async with org_session(org_id, engine=app_engine) as session:
        second, second_envelope = await wake_via_site_server(
            session,
            job_engine,
            org_id=org_id,
            site_server_id=site_server_id,
            target_mac="a4:bb:6d:11:22:44",
            actor_id=user_id,
        )
        second_id = second.id
    del second_envelope
    async with org_session(org_id, engine=app_engine) as session:
        from app.models import Job

        job_row = await session.get(Job, second_id)
        assert job_row is not None
        await job_engine.complete(
            session,
            job_row,
            state="succeeded",
            result={"sent": True, "broadcast": "10.99.99.255", "sent_at": now.isoformat()},
        )
    async with AsyncSession(admin_engine) as session:
        status2 = (
            await session.execute(
                text("SELECT status, result FROM jobs WHERE id = :id"), {"id": second_id}
            )
        ).one()
    check(
        "differing broadcast -> failed",
        status2.status == JobStatus.failed.value,
        f"{status2.status} / {status2.result}",
    )
    check(
        "the failure names the wrong address",
        "10.99.99.255" in str(status2.result or ""),
        str(status2.result or ""),
    )

    # ---------------------------------------------------------------- step 5
    print("\n== step 5: select_sweeper -- preferred, then offline, then disabled ==")
    async with AsyncSession(admin_engine) as session:
        chosen = await select_sweeper(session, org_id, _Subnet(), now=now)  # type: ignore[arg-type]
    check("site server preferred", chosen == promoted_id, str(chosen))

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE agents SET status = 'offline' WHERE device_id = :d"),
            {"d": promoted_id},
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        chosen_offline = await select_sweeper(session, org_id, _Subnet(), now=now)  # type: ignore[arg-type]
    check(
        "offline site server loses to an online endpoint",
        chosen_offline == ordinary_id,
        str(chosen_offline),
    )

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE agents SET status = 'online' WHERE device_id = :d"),
            {"d": promoted_id},
        )
        await session.execute(
            text("UPDATE site_server SET enabled = false WHERE id = :id"),
            {"id": site_server_id},
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        chosen_disabled = await select_sweeper(session, org_id, _Subnet(), now=now)  # type: ignore[arg-type]
    check(
        "disabled site server loses the preference",
        chosen_disabled == ordinary_id,
        str(chosen_disabled),
    )
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE site_server SET enabled = true WHERE id = :id"),
            {"id": site_server_id},
        )
        await session.commit()

    # ---------------------------------------------------------------- step 6
    print("\n== step 6: segment check ==")
    async with org_session(org_id, engine=app_engine) as session:
        result = await segment_check(session, org_id, site_server_id, now=now)
    check(
        "10.20.0.0/16 is matched, with real evidence",
        (result.evidence, result.matched, result.observed_undeclared)
        == ("ipam", ["10.20.0.0/16"], []),
        f"{result.evidence} {result.matched} {result.observed_undeclared}",
    )

    # ---------------------------------------------------------------- step 7
    print("\n== step 7: provisioning run with wake_on_lan ==")
    from moto import mock_aws

    from app.core.config import Settings
    from app.core.storage import ObjectStore
    from app.core.vault import LocalVaultService
    from app.modules.provisioning.dispatch import start_provision_run
    from app.modules.provisioning.repository import create_image, upsert_profile
    from app.modules.provisioning.schema import ProvisionWakeRequest

    async with AsyncSession(admin_engine) as session:
        role_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO roles (id, org_id, name, description)"
                " VALUES (:id, :org, :n, '')"
            ),
            {"id": role_id, "org": org_id, "n": f"smoke-{uuid4().hex[:6]}"},
        )
        for code in ("provisioning.run", "site.wake"):
            await session.execute(
                text(
                    "INSERT INTO role_permissions (role_id, permission_id)"
                    " SELECT :r, id FROM permissions WHERE code = :c"
                ),
                {"r": role_id, "c": code},
            )
        await session.execute(
            text(
                "INSERT INTO user_roles (id, org_id, user_id, role_id)"
                " VALUES (gen_random_uuid(), :org, :u, :r)"
            ),
            {"org": org_id, "u": user_id, "r": role_id},
        )
        await session.commit()

    vault = LocalVaultService(Path("/tmp/smoke-site-root.key"))
    settings = Settings(
        database_url=app_url,
        database_admin_url=admin_url,
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=Path("/tmp/smoke-site-ca"),
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-smoke",
    )

    with mock_aws():
        store = ObjectStore(settings)
        async with org_session(org_id, engine=app_engine) as session:
            image = await create_image(
                session,
                org_id=org_id,
                os_family="windows",
                name="win",
                kind="image",
                params={},
                actor_id=user_id,
                actor_type="user",
            )
            profile = await upsert_profile(
                session,
                vault,
                org_id=org_id,
                name="win profile",
                image_id=image.id,
                post_config={"hostname": "WK-1"},
                target_scope={},
                enabled=True,
                actor_id=user_id,
                actor_type="user",
            )
            profile_id = profile.id
            await session.commit()

        async with org_session(org_id, engine=app_engine) as session:
            run, wake_envelope = await start_provision_run(
                session,
                store,
                vault,
                org_id=org_id,
                profile_id=profile_id,
                target_ref=None,
                now=now,
                actor_id=user_id,
                actor_type="user",
                wake_on_lan=ProvisionWakeRequest(target_mac="a4:bb:6d:11:22:55"),
                job_engine=job_engine,
            )
            run_state = run.state
        check("run started", run_state == "awaiting_enrollment", run_state)
        check("a wake envelope came back with it", wake_envelope is not None)

        async with AsyncSession(admin_engine) as session:
            wake_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM jobs WHERE org_id = :org AND type = 'site.wake'"
                    ),
                    {"org": org_id},
                )
            ).scalar_one()
            run_count = (
                await session.execute(
                    text("SELECT count(*) FROM provision_run WHERE org_id = :org"),
                    {"org": org_id},
                )
            ).scalar_one()
        check("three wake jobs now exist (2 + this one)", wake_count == 3, str(wake_count))
        check("one provisioning run", run_count == 1, str(run_count))

        # demote, then repeat: 400-equivalent AND no run
        async with AsyncSession(admin_engine) as session:
            await session.execute(
                text("DELETE FROM site_server WHERE org_id = :org"), {"org": org_id}
            )
            await session.commit()

        refused = False
        try:
            async with org_session(org_id, engine=app_engine) as session:
                await start_provision_run(
                    session,
                    store,
                    vault,
                    org_id=org_id,
                    profile_id=profile_id,
                    target_ref=None,
                    now=now,
                    actor_id=user_id,
                    actor_type="user",
                    wake_on_lan=ProvisionWakeRequest(target_mac="a4:bb:6d:11:22:66"),
                    job_engine=job_engine,
                )
        except ProvisioningValidationError:
            refused = True

        async with AsyncSession(admin_engine) as session:
            run_count_after = (
                await session.execute(
                    text("SELECT count(*) FROM provision_run WHERE org_id = :org"),
                    {"org": org_id},
                )
            ).scalar_one()
        check(
            "with the site server demoted: refused AND no second run",
            (refused, run_count_after) == (True, 1),
            f"refused={refused} runs={run_count_after}",
        )

    await redis.aclose()
    await admin_engine.dispose()
    await app_engine.dispose()

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n== {len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed ==")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
