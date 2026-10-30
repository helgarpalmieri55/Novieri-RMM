"""Tests for signed `backup.run` dispatch (docs/modules/backup/spec.md).

Mirrors `tests/test_deploy_dispatch.py`'s fixture style: a fake job engine
stands in for the kernel job engine so these tests exercise dispatch.py's
policy-resolution, window-gate, key-opening, and audit logic in isolation.
`dispatch_backup` does not publish to Redis — it returns the pending
`(job_id, device_id, envelope)` triples for the caller to publish after its
own transaction commits (see the module docstring), so there's no fake Redis
here.
"""

import base64
import datetime as _dt
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import (
    AuditEvent,
    BackupPolicy,
    BackupRun,
    Device,
    DeviceGroup,
    Organization,
    User,
)
from app.modules.backup.dispatch import dispatch_backup, dispatch_due_backup
from app.modules.backup.repository import create_repository, open_repo_key

# ---------------------------------------------------------------------------
# dispatch_backup
# ---------------------------------------------------------------------------


class _FakeJobEngine:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, session, **kw):
        from app.models import Job

        job = Job(
            org_id=kw["org_id"],
            type=kw["type"],
            target_type="device",
            target_id=kw["target_device_id"],
            params=kw["params"],
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
            requested_by=kw.get("requested_by"),
        )
        session.add(job)
        await session.flush()
        # Mirror the real JobEngine.create: sign at create time.
        job.signature = "fake-signature"
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job):
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": job.signature}


async def _setup(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    vault: LocalVaultService,
    *,
    window_cron: str | None = None,
    enabled: bool = True,
    os_family: str = "windows",
) -> tuple[UUID, UUID, UUID, UUID]:
    """Returns (org_id, device_id, repository_id, user_id) with an enabled
    device-group backup policy pointing at a real, vault-sealed repository.
    `user_id` is a real user in this org, usable as `actor_id` without
    violating `backup_runs.requested_by`'s FK."""
    slug = f"bkdisp-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        user = User(
            org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com"
        )
        session.add(user)
        await session.flush()

        group = DeviceGroup(org_id=org.id, name="backup-group", client_id=org.id)
        session.add(group)
        await session.flush()

        device = Device(
    org_id=org.id,
    hostname="d1",
    os_family=os_family,
    device_group_id=group.id,
    client_id=org.id,
    site_id=org.id,
        )
        session.add(device)
        await session.flush()

        await session.commit()
        org_id, device_id, user_id = org.id, device.id, user.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(
            session, vault, org_id=org_id, actor_id=user_id, client_id=org_id, name="primary"
        )
        session.add(
            BackupPolicy(
                org_id=org_id,
                repository_id=repo.id,
                device_group_id=group.id,
                sources={"paths": ["/data"]},
                window_cron=window_cron,
                retention_gfs={"daily": 7},
                enabled=enabled,
            )
        )
        await session.commit()
        repo_id = repo.id

    return org_id, device_id, repo_id, user_id


async def test_open_window_dispatches_signed_job_and_backup_run_row(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, repo_id, user_id = await _setup(
        admin_engine, app_engine, vault, window_cron="0 3 * * *"
    )
    now = _dt.datetime(2026, 7, 19, 3, 0)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        raw_key = await open_repo_key(session, vault, org_id=org_id, repository_id=repo_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_backup(
            session,
            job_engine,
            vault,
            org_id=org_id,
            device_id=device_id,
            now=now,
            actor_id=user_id,
            actor_type="user",
        )

        assert len(pending) == 1
        job_id, dispatched_device_id, envelope = pending[0]
        assert dispatched_device_id == device_id
        assert envelope["sig"] == "fake-signature"

        assert len(job_engine.created) == 1
        assert job_engine.created[0]["type"] == "backup.run"
        assert job_engine.created[0]["target_device_id"] == device_id
        assert job_engine.created[0]["requested_by"] == user_id

        from app.models import Job

        signed_job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert signed_job.signature is not None
        assert signed_job.params["repo_key"] == base64.b64encode(raw_key).decode()
        assert signed_job.params["snapshot"] == "vss"  # windows -> vss

        run = (
            await session.execute(select(BackupRun).where(BackupRun.job_id == job_id))
        ).scalar_one()
        assert run.state == "running"
        assert run.device_id == device_id
        assert run.repository_id == repo_id
        assert run.actor_type == "user"
        assert run.requested_by == user_id

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "backup.dispatched")
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == user_id
        meta_str = str(event.meta)
        assert raw_key.hex() not in meta_str
        assert base64.b64encode(raw_key).decode() not in meta_str
        assert "repo_key" not in event.meta


async def test_a_schedule_with_nothing_due_dispatches_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, _repo_id, _user_id = await _setup(
        admin_engine, app_engine, vault, window_cron="0 2 1 1 *"
    )
    now = _dt.datetime(2026, 7, 19, 10, 0, tzinfo=_dt.UTC)  # no fire in the look-back

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_due_backup(
            session, job_engine, vault, org_id=org_id, device_id=device_id, now=now
        )

        assert pending == []
        assert job_engine.created == []
        runs = (await session.execute(select(BackupRun))).scalars().all()
        assert runs == []


async def test_no_policy_dispatches_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = LocalVaultService(tmp_path / "root.key")
    slug = f"bknopolicy-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        device = Device(
            org_id=org.id, hostname="d1", os_family="windows", client_id=org.id, site_id=org.id
        )
        session.add(device)
        await session.commit()
        org_id, device_id = org.id, device.id

    now = _dt.datetime(2026, 7, 19, 3, 0)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_backup(
            session, job_engine, vault, org_id=org_id, device_id=device_id, now=now
        )
        assert pending == []
        assert job_engine.created == []


async def test_another_orgs_device_is_not_dispatched_because_rls_hides_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A device id belonging to org A must not be dispatchable under org B's
    session — the policy resolution (Device lookup) is itself org-scoped."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_a, device_a, _repo_a, _user_a = await _setup(admin_engine, app_engine, vault)
    org_b, _device_b, _repo_b, _user_b = await _setup(admin_engine, app_engine, vault)
    now = _dt.datetime(2026, 7, 19, 3, 0)

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_backup(
            session,
            job_engine,
            vault,
            org_id=org_b,
            device_id=device_a,  # org A's device id, under org B's session
            now=now,
        )
        assert pending == []
        assert job_engine.created == []
        runs = (await session.execute(select(BackupRun))).scalars().all()
        assert runs == []


async def test_dispatch_does_not_publish(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """dispatch_backup must return triples for the caller to publish
    post-commit rather than publishing itself — mirrors dispatch_deployment.
    There's no fake Redis wired into this test at all: if the function tried
    to publish, it would raise (no publish_job import is exercised)."""
    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, _repo_id, _user_id = await _setup(admin_engine, app_engine, vault)
    now = _dt.datetime(2026, 7, 19, 3, 0)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        job_engine = _FakeJobEngine()
        pending = await dispatch_backup(
            session, job_engine, vault, org_id=org_id, device_id=device_id, now=now
        )
        assert isinstance(pending, list)
        assert len(pending) == 1
        job_id, returned_device_id, envelope = pending[0]
        assert isinstance(job_id, UUID)
        assert returned_device_id == device_id
        assert isinstance(envelope, dict)
