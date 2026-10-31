"""Every backup audit that has its record's client at hand names it (phase 3
design §5.5).

Phase 2 filed the repository, policy and dispatch events under their client
(`test_client_scope_backup.py`, the exact sets near its end). These are the ones
it left without, each with the client of the row it already holds: the restore
and its result, the automatic test restore, the verification, the off-site copy
and its failure, and the two reconciliations.

The refused dedup and read exchanges name the client when the agent named a
job of its own device -- a job's client is its target's (migration 0231) --
and none otherwise: there is nothing to take it from. `backup.offsite_gc`
stays without one: it sums up every target, repository and client of the
organisation.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.gateway.protocol import BackupChunkQueryMsg, BackupReadQueryMsg
from app.models import BackupPoint, BackupRun, Job, RestoreJob
from app.models.core import JobStatus
from app.modules.backup.dedup import answer_chunk_query
from app.modules.backup.gc import verify_point
from app.modules.backup.offsite import record_copy_failure
from app.modules.backup.read import answer_read_query
from app.modules.backup.reconcile import reconcile_stuck
from app.modules.backup.repository import open_repo_key
from app.modules.backup.restore import ingest_restore_result
from app.modules.backup.schema import BackupRestoreResult
from app.modules.backup.test_restore import dispatch_due_test_restores
from tests.backup_dedup_fixtures import SEED_NOW
from tests.backup_offsite_fixtures import OffsiteOrg, add_point, add_proven_key, add_target
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv
from tests.test_backup_dispatch import _FakeJobEngine
from tests.test_backup_offsite import _replicate_all, aws, vault  # noqa: F401  (fixtures)
from tests.test_backup_read_query import seed_restore
from tests.test_client_scope_backup import (
    _device,
    _events,
    _group,
    _policy,
    _sealed_point,
    _service_repo,
    _user_id,
)


def _chunk_query(job_id: UUID | str) -> BackupChunkQueryMsg:
    """A dedup query naming `job_id` and nothing to partition."""
    return BackupChunkQueryMsg(
        v=1, kind="backup_chunk_query", job_id=str(job_id), query_id="q1", check=[], stored=[]
    )


def _read_query(job_id: UUID | str) -> BackupReadQueryMsg:
    """A read query naming `job_id`, for one object of the repository."""
    return BackupReadQueryMsg(
        v=1, kind="backup_read_query", job_id=str(job_id), query_id="q1",
        suffixes=["chunks/ab/abcd"],
    )


async def test_a_restore_its_result_and_a_verification_name_the_point_s_client(
    api_env: ApiEnv, aws: Any  # noqa: F811
) -> None:
    """The automatic test restore of a point of X's device -- its dispatch
    (`backup.restore_dispatched`, `backup.test_restore_dispatched`), its
    result -- and a verification of the same point: all four in X.
    FALSIFIER: drop `client_id=` from any one of the four audits
    (`restore.py` twice, `test_restore.py`, `gc.py`)."""
    store, _s3 = aws
    org = await api_env.create_org("bk-aud-restore")
    x, x_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    vault = api_env.app.state.vault  # noqa: F811
    x_repo = await _service_repo(api_env, org, x, actor)
    group = await _group(api_env, org, x)
    await _policy(api_env, org, x_repo, group=group)
    device = await _device(api_env, org, x, x_site, group)
    point = await _sealed_point(api_env, store, vault, org, device, x_repo,
                                taken_at=datetime.now(UTC) - timedelta(hours=1), legacy=False)
    seeded = {e.id for e in await _events(api_env, org)}
    now = datetime.now(UTC)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        ((job_id, _device_id, _envelope),) = await dispatch_due_test_restores(
            s, _FakeJobEngine(), vault, store, org_id=org, now=now
        )
        job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
        await ingest_restore_result(
            s, job,
            BackupRestoreResult(state="success", restored_bytes=1, verified=True, mismatch=[]),
        )
        assert await verify_point(s, store, vault, org_id=org, backup_point_id=point, now=now)
    events = [e for e in await _events(api_env, org) if e.id not in seeded]
    assert sorted(((e.action, e.client_id) for e in events), key=repr) == sorted([
        ("backup.restore_dispatched", x),
        ("backup.test_restore_dispatched", x),
        ("backup.restore_result", x),
        ("backup.verify", x),
    ], key=repr)


async def test_the_reconciliations_name_the_device_s_client(api_env: ApiEnv) -> None:
    """A run and a restore of X's device left `running` by a failed job:
    the sweep fails both and files each in X.
    FALSIFIER: drop `client_id=` from either audit in `reconcile.py`."""
    org = await api_env.create_org("bk-aud-reconcile")
    x, x_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    device = await _device(api_env, org, x, x_site)
    now = datetime.now(UTC)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        jobs = [
            Job(org_id=org, type=kind, target_type="device", target_id=device, params={},
                nonce=uuid4().hex, issued_at=now - timedelta(hours=2),
                expires_at=now + timedelta(hours=1), status=JobStatus.failed,
                result={"state": "failed", "error": "disk"})
            for kind in ("backup.run", "backup.restore")
        ]
        s.add_all(jobs)
        await s.flush()
        done = BackupRun(org_id=org, device_id=device, repository_id=x_repo, state="success",
                         actor_type="system", started_at=now - timedelta(days=1))
        s.add(done)
        await s.flush()
        point = BackupPoint(org_id=org, device_id=device, repository_id=x_repo,
                            backup_run_id=done.id, taken_at=now - timedelta(days=1),
                            manifest_ref="m", size=1, state="complete")
        s.add(point)
        await s.flush()
        s.add_all([
            BackupRun(org_id=org, device_id=device, repository_id=x_repo, job_id=jobs[0].id,
                      state="running", actor_type="system", started_at=now - timedelta(hours=2)),
            RestoreJob(org_id=org, device_id=device, backup_point_id=point.id,
                       job_id=jobs[1].id, selection={"paths": []}, target={}, mode="restore",
                       state="running", actor_type="system"),
        ])
        await s.commit()
    seeded = {e.id for e in await _events(api_env, org)}
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        assert await reconcile_stuck(s, org, now=now) == {"runs": 1, "restores": 1}
    events = [e for e in await _events(api_env, org) if e.id not in seeded]
    assert sorted(((e.action, e.client_id) for e in events), key=repr) == sorted([
        ("backup.run_reconciled", x),
        ("backup.restore_reconciled", x),
    ], key=repr)


async def test_an_off_site_copy_and_its_failure_name_the_point_s_client(
    api_env: ApiEnv, aws: Any  # noqa: F811
) -> None:
    """A point of X's device copied off site (`backup.replicated`) and a
    failed copy of it (`backup.replication_failed`, which reads the point's
    client: a copy written there for the first time has none yet): both in X.
    FALSIFIER: drop `client_id=` from either audit in `offsite.py`."""
    store, _s3 = aws
    org = await api_env.create_org("bk-aud-offsite")
    x, x_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    vault = api_env.app.state.vault  # noqa: F811
    x_repo = await _service_repo(api_env, org, x, actor)
    device = await _device(api_env, org, x, x_site)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        key = await open_repo_key(s, vault, org_id=org, repository_id=x_repo)
    world = OffsiteOrg(org, actor, device, x_repo, key, b"")
    await add_proven_key(api_env.app_engine, world)
    target = await add_target(api_env.app_engine, vault, world)
    point, _ = await add_point(api_env.app_engine, store, world, {"/srv/a.txt": [b"alpha" * 10]})

    assert await _replicate_all(api_env.app_engine, vault, store, world, datetime.now(UTC)) == [
        True
    ]
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        await record_copy_failure(s, org_id=org, point_id=point, target_id=target,
                                  error=RuntimeError("provider unreachable"))
    events = await _events(api_env, org, "backup.replicated", "backup.replication_failed")
    assert [(e.action, e.target_id, e.client_id) for e in events] == [
        ("backup.replicated", str(point), x),
        ("backup.replication_failed", str(point), x),
    ]


async def test_a_refused_exchange_names_the_client_of_its_own_device_s_job(
    api_env: ApiEnv, aws: Any, vault: LocalVaultService  # noqa: F811
) -> None:
    """Both exchanges, on the admin engine like their own suites, refused
    three ways. A job that is no longer live, named by its own device: the
    job's client (the device's). A malformed id, and a job of ANOTHER device:
    no client -- the refusal does not take one from a job the asking device
    has no claim on.
    FALSIFIER: drop `client_id=` from either `_audit_refusal`, or the
    `Job.target_id == device_id` predicate from `dedup.py::refusal_client`."""
    store, _s3 = aws
    base, restore, devices = await seed_restore(
        api_env.admin_engine, api_env.app_engine, vault, "bk-aud-refused", device_count=2
    )
    other = devices[1]
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(
            update(Job)
            .where(Job.id.in_([base.job_id, restore.job_id]))
            .values(status=JobStatus.succeeded)
        )
        await s.commit()
    async with AsyncSession(api_env.admin_engine) as s:
        for device, job in ((base.device_id, str(base.job_id)), (base.device_id, "not-a-job"),
                            (other, str(base.job_id))):
            assert await answer_chunk_query(
                s, store, _chunk_query(job), org_id=base.org_id, device_id=device, now=SEED_NOW
            ) is None
        for device, job in ((base.device_id, str(restore.job_id)),
                            (base.device_id, "not-a-job"), (other, str(restore.job_id))):
            assert await answer_read_query(
                s, store, _read_query(job), org_id=base.org_id, device_id=device, now=SEED_NOW
            ) is None
        await s.commit()
    refusals = await _events(api_env, base.org_id, "backup.dedup_refused", "backup.read_refused")
    assert [(e.action, e.meta["reason"], e.client_id) for e in refusals] == [
        ("backup.dedup_refused", "job_not_live", base.org_id),
        ("backup.dedup_refused", "malformed_job_id", None),
        ("backup.dedup_refused", "wrong_device", None),
        ("backup.read_refused", "job_not_live", base.org_id),
        ("backup.read_refused", "malformed_job_id", None),
        ("backup.read_refused", "wrong_device", None),
    ]
