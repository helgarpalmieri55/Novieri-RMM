"""Backup engine REST API tests (docs/modules/backup/spec.md §12).

Exercises server-side RBAC on every route (invariant #6), the signed
`backup.run` / `backup.restore` dispatch reaching the agent (via the live
redis dispatch channel, mirroring `tests/test_deploy_api.py`'s
`test_execute_deployment_dispatches_signed_job_and_audits_user_actor`),
correct actor attribution on the audit log (operator-initiated actions carry
`actor_type="user"`), and that no response body anywhere ever carries
`repo_key`/`encryption_key_ref` (invariant #3). Uses the `api_env` in-process
ASGI harness.
"""

import asyncio
import json
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import AuditEvent, BackupPoint, BackupRun, Device, User
from app.modules.backup.repository import create_repository, open_repo_key
from app.modules.backup.restore import seal_manifest
from app.modules.backup.schema import BackupManifest, FiletreeStream, ManifestEntry

REDIS_URL = "redis://localhost:6379/9"

_KNOWN_MANIFEST = BackupManifest(
    schema_version=1,
    streams=[
        FiletreeStream(
            type="filetree",
            entries=[
                ManifestEntry(
                    path="/data/file.txt",
                    kind="file",
                    mode=0o644,
                    size=123,
                    mtime=1700000000.0,
                    chunk_ids=["chunk-a"],
                ),
            ],
        )
    ],
)


async def _device(api_env, org_id: UUID, hostname: str = "wks-backup") -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        dev = Device(org_id=org_id, hostname=hostname, os_family="windows")
        s.add(dev)
        await s.commit()
        return dev.id


async def _user_id(api_env, org_id: UUID, sub: str) -> UUID:
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        return (
            await s.execute(
                select(User.id).where(User.org_id == org_id, User.external_subject == sub)
            )
        ).scalar_one()


async def _seed_point(api_env, org_id: UUID, device_id: UUID, actor_id: UUID) -> tuple[UUID, UUID]:
    """Seed a real, vault-sealed repository plus a completed `BackupRun` +
    `BackupPoint` whose `manifest_ref` is a real sealed object in (moto)
    object storage. Requires `api_env.app.state.object_store` already pinned
    to a moto-backed `ObjectStore` (see `mock_aws()` in the calling test).
    Returns `(repository_id, backup_point_id)`."""
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(
            session, api_env.app.state.vault, org_id=org_id, actor_id=actor_id,
            name=f"repo-{uuid4().hex[:8]}",
        )
        await session.commit()
        repo_id = repo.id

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        key = await open_repo_key(
            session, api_env.app.state.vault, org_id=org_id, repository_id=repo_id
        )
        blob = seal_manifest(key, _KNOWN_MANIFEST)
        manifest_ref = await api_env.app.state.object_store.put_backup_object(
            org_id, repo_id, f"manifests/{uuid4().hex}", blob
        )

        run = BackupRun(
            org_id=org_id, device_id=device_id, repository_id=repo_id,
            state="success", actor_type="system",
        )
        session.add(run)
        await session.flush()

        point = BackupPoint(
            org_id=org_id,
            device_id=device_id,
            repository_id=repo_id,
            backup_run_id=run.id,
            manifest_ref=manifest_ref,
            size=123,
            state="complete",
        )
        session.add(point)
        await session.flush()
        await session.commit()
        point_id = point.id

    return repo_id, point_id


def _assert_no_secrets(resp) -> None:
    assert "repo_key" not in resp.text
    assert "encryption_key_ref" not in resp.text


# --------------------------------------------------------------------------- #
# GET|POST /repositories
# --------------------------------------------------------------------------- #


async def test_list_repositories_allowed_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("backup")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/backup/repositories", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    _assert_no_secrets(resp)


async def test_create_repository_forbidden_for_technician_ok_for_org_admin(api_env) -> None:
    org_id = await api_env.create_org("backup")
    tech = await api_env.create_user(org_id, "technician")
    admin = await api_env.create_user(org_id, "org-admin")
    body = {"name": "primary"}

    forbidden = await api_env.client.post(
        "/api/v1/backup/repositories", json=body, headers=await api_env.headers(org_id, tech)
    )
    assert forbidden.status_code == 403

    ok = await api_env.client.post(
        "/api/v1/backup/repositories", json=body, headers=await api_env.headers(org_id, admin)
    )
    assert ok.status_code == 201, ok.text
    out = ok.json()
    assert out["name"] == "primary"
    assert out["state"] == "active"
    assert "repo_key" not in out
    assert "encryption_key_ref" not in out
    _assert_no_secrets(ok)


# --------------------------------------------------------------------------- #
# GET|PUT /policies
# --------------------------------------------------------------------------- #


async def test_list_policies_allowed_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("backup")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/backup/policies", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_put_policy_forbidden_for_technician_ok_for_org_admin(api_env) -> None:
    org_id = await api_env.create_org("backup")
    tech = await api_env.create_user(org_id, "technician")
    admin = await api_env.create_user(org_id, "org-admin")
    admin_headers = await api_env.headers(org_id, admin)

    repo_resp = await api_env.client.post(
        "/api/v1/backup/repositories", json={"name": "primary"}, headers=admin_headers
    )
    repository_id = repo_resp.json()["id"]

    body = {
        "repository_id": repository_id,
        "sources": {"paths": ["/data"]},
        "window_cron": None,
        "retention_gfs": {"daily": 7},
        "enabled": True,
    }

    forbidden = await api_env.client.put(
        "/api/v1/backup/policies", json=body, headers=await api_env.headers(org_id, tech)
    )
    assert forbidden.status_code == 403

    ok = await api_env.client.put("/api/v1/backup/policies", json=body, headers=admin_headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["repository_id"] == repository_id


async def test_put_policy_unknown_repository_is_404(api_env) -> None:
    org_id = await api_env.create_org("backup")
    admin = await api_env.create_user(org_id, "org-admin")
    resp = await api_env.client.put(
        "/api/v1/backup/policies",
        json={"repository_id": str(uuid4()), "sources": {}},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# POST /devices/{device_id}/run
# --------------------------------------------------------------------------- #


async def test_run_dispatches_signed_job_and_audits_user_actor(api_env) -> None:
    org_id = await api_env.create_org("backup")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env, org_id)
    expected_user_id = await _user_id(api_env, org_id, tech)
    admin_headers = await api_env.headers(org_id, admin)
    tech_headers = await api_env.headers(org_id, tech)

    repo_resp = await api_env.client.post(
        "/api/v1/backup/repositories", json={"name": "primary"}, headers=admin_headers
    )
    assert repo_resp.status_code == 201, repo_resp.text
    repository_id = repo_resp.json()["id"]

    policy_resp = await api_env.client.put(
        "/api/v1/backup/policies",
        json={
            "repository_id": repository_id,
            "sources": {"paths": ["/data"]},
            "window_cron": None,
            "retention_gfs": {"daily": 7},
            "enabled": True,
        },
        headers=admin_headers,
    )
    assert policy_resp.status_code == 200, policy_resp.text

    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.psubscribe(f"dispatch:{device_id}")

    resp = await api_env.client.post(
        f"/api/v1/backup/devices/{device_id}/run", headers=tech_headers
    )
    assert resp.status_code == 200, resp.text
    job_ids = resp.json()["job_ids"]
    assert len(job_ids) == 1
    _assert_no_secrets(resp)

    published = None
    for _ in range(30):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message and message["type"] == "pmessage":
            published = json.loads(message["data"])
            break
        await asyncio.sleep(0.05)
    await pubsub.aclose()
    await redis.aclose()
    assert published is not None, "expected a backup.run job to be published"
    assert published["job"]["id"] == job_ids[0]
    assert published["sig"]

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.dispatched")
            )
        ).scalar_one()
    assert event.actor_type == "user"
    assert event.actor_id == expected_user_id


async def test_run_forbidden_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("backup")
    ro = await api_env.create_user(org_id, "read-only")
    device_id = await _device(api_env, org_id)
    resp = await api_env.client.post(
        f"/api/v1/backup/devices/{device_id}/run", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# GET /points, GET /points/{id}/manifest, GET /devices/{id}/backup
# --------------------------------------------------------------------------- #


async def test_list_points_and_get_manifest_return_entries(api_env) -> None:
    org_id = await api_env.create_org("backup")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env, org_id)
    admin_id = await _user_id(api_env, org_id, admin)

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        _repo_id, point_id = await _seed_point(api_env, org_id, device_id, admin_id)

        list_resp = await api_env.client.get(
            "/api/v1/backup/points", headers=await api_env.headers(org_id, tech)
        )
        assert list_resp.status_code == 200, list_resp.text
        points = list_resp.json()
        assert len(points) == 1
        assert points[0]["id"] == str(point_id)
        _assert_no_secrets(list_resp)

        manifest_resp = await api_env.client.get(
            f"/api/v1/backup/points/{point_id}/manifest",
            headers=await api_env.headers(org_id, tech),
        )
    assert manifest_resp.status_code == 200, manifest_resp.text
    body = manifest_resp.json()
    assert body["schema_version"] == 1
    entries = body["streams"][0]["entries"]
    assert entries[0]["path"] == "/data/file.txt"
    assert entries[0]["chunk_ids"] == ["chunk-a"]
    _assert_no_secrets(manifest_resp)


async def test_get_manifest_unknown_point_is_404(api_env) -> None:
    org_id = await api_env.create_org("backup")
    admin = await api_env.create_user(org_id, "org-admin")
    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        resp = await api_env.client.get(
            f"/api/v1/backup/points/{uuid4()}/manifest",
            headers=await api_env.headers(org_id, admin),
        )
    assert resp.status_code == 404


async def test_device_backup_status_ok_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("backup")
    ro = await api_env.create_user(org_id, "read-only")
    device_id = await _device(api_env, org_id)
    resp = await api_env.client.get(
        f"/api/v1/backup/devices/{device_id}/backup", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device_id"] == str(device_id)
    assert body["last_good_point"] is None
    _assert_no_secrets(resp)


# --------------------------------------------------------------------------- #
# POST /points/{id}/restore, POST /points/{id}/verify
# --------------------------------------------------------------------------- #


async def test_restore_dispatches_signed_job_and_audits_user_actor(api_env) -> None:
    org_id = await api_env.create_org("backup")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env, org_id)
    admin_id = await _user_id(api_env, org_id, admin)
    expected_user_id = await _user_id(api_env, org_id, tech)

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        _repo_id, point_id = await _seed_point(api_env, org_id, device_id, admin_id)

        redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        pubsub = redis.pubsub()
        await pubsub.psubscribe(f"dispatch:{device_id}")

        resp = await api_env.client.post(
            f"/api/v1/backup/points/{point_id}/restore",
            json={"selection": ["/data/file.txt"], "target": {"path": "/restore/dest"}},
            headers=await api_env.headers(org_id, tech),
        )
        assert resp.status_code == 200, resp.text
        job_ids = resp.json()["job_ids"]
        assert len(job_ids) == 1
        _assert_no_secrets(resp)

        published = None
        for _ in range(30):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message and message["type"] == "pmessage":
                published = json.loads(message["data"])
                break
            await asyncio.sleep(0.05)
        await pubsub.aclose()
        await redis.aclose()
        assert published is not None, "expected a backup.restore job to be published"
        assert published["job"]["id"] == job_ids[0]

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.restore_dispatched")
            )
        ).scalar_one()
    assert event.actor_type == "user"
    assert event.actor_id == expected_user_id


async def test_restore_forbidden_for_read_only(api_env) -> None:
    org_id = await api_env.create_org("backup")
    ro = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.post(
        f"/api/v1/backup/points/{uuid4()}/restore",
        json={"selection": [], "target": {}},
        headers=await api_env.headers(org_id, ro),
    )
    assert resp.status_code == 403


async def test_verify_dispatches_signed_job(api_env) -> None:
    org_id = await api_env.create_org("backup")
    admin = await api_env.create_user(org_id, "org-admin")
    tech = await api_env.create_user(org_id, "technician")
    device_id = await _device(api_env, org_id)
    admin_id = await _user_id(api_env, org_id, admin)

    with mock_aws():
        api_env.app.state.object_store = ObjectStore(api_env.settings)
        _repo_id, point_id = await _seed_point(api_env, org_id, device_id, admin_id)

        resp = await api_env.client.post(
            f"/api/v1/backup/points/{point_id}/verify",
            headers=await api_env.headers(org_id, tech),
        )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["job_ids"]) == 1
    _assert_no_secrets(resp)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.restore_dispatched")
            )
        ).scalar_one()
    assert event.actor_type == "user"


# --------------------------------------------------------------------------- #
# GET /report, GET /report/export
# --------------------------------------------------------------------------- #


async def test_report_returns_fraction_rates(api_env) -> None:
    org_id = await api_env.create_org("backup")
    ro = await api_env.create_user(org_id, "read-only")
    await _device(api_env, org_id)
    resp = await api_env.client.get(
        "/api/v1/backup/report", headers=await api_env.headers(org_id, ro)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["coverage_rate"] <= 1.0
    assert body["success_rate"] <= 1.0
    assert "devices_total" in body
    _assert_no_secrets(resp)


async def test_report_forbidden_without_backup_read(api_env) -> None:
    org_id = await api_env.create_org("backup")
    nobody = await api_env.create_user(org_id)
    resp = await api_env.client.get(
        "/api/v1/backup/report", headers=await api_env.headers(org_id, nobody)
    )
    assert resp.status_code == 403


async def test_report_export_audits_evidence_exported(api_env) -> None:
    org_id = await api_env.create_org("backup")
    admin = await api_env.create_user(org_id, "org-admin")
    expected_user_id = await _user_id(api_env, org_id, admin)
    await _device(api_env, org_id)

    resp = await api_env.client.get(
        "/api/v1/backup/report/export", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    _assert_no_secrets(resp)
    assert "device_id" in resp.text  # CSV header row

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.evidence_exported")
            )
        ).scalar_one()
    assert event.actor_type == "user"
    assert event.actor_id == expected_user_id


async def test_report_export_escapes_hostname_but_not_device_id_or_dates(api_env) -> None:
    """`hostname` is the only genuinely free-text column in this export
    (`app.models.core.Device.hostname` -- unconstrained); `device_id` is a
    UUID and the three `*_at` columns are timestamps this file itself
    isoformats, so none of those should ever be escaped."""
    import csv
    import io

    org_id = await api_env.create_org("backup")
    admin = await api_env.create_user(org_id, "org-admin")
    device_id = await _device(api_env, org_id, hostname="=cmd|' /C calc'!A1")

    resp = await api_env.client.get(
        "/api/v1/backup/report/export", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    rows = list(csv.reader(io.StringIO(resp.text)))
    header = rows[0]
    row = next(r for r in rows[1:] if r[header.index("device_id")] == str(device_id))
    assert row[header.index("hostname")] == "'=cmd|' /C calc'!A1"
