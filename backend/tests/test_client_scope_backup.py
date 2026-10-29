"""A backup repository serves ONE client (plan 2026-10-26 Task 13, C1).
Its master key travels in the signed params of every device that uses it
and chunks are deduplicated per repository: shared, a device of Y would hold
X's key and could probe X's content (research-5 §E1).

So (migration 0242):

- `backup_repositories` is C: a repository is filed under the client the
  request names (`client_for_new_rows`, held FOR SHARE as the write's first
  lock), never Unassigned; `backup_chunks` and `backup_chunk_copies` are H
  from it, and all three are under the client policy.
- A policy names only a repository of its own client
  (`its_backup_policy_repository_guard`), and a policy for every client
  (no client, no group) names none.
- A run and a point name a repository of their own client: `(repository_id,
  client_id) REFERENCES backup_repositories (id, client_id)`, NOT VALID so
  history is not re-judged, checked on every new row.
- The device's client's default policy (`client_id = <client>`, no group)
  replaces the organisation default; the window, which runs under scope all,
  never hands a device a repository of another client, and records why.
- Off-site: a target and the recovery keys a repository key is wrapped to
  are global or the repository's client's.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from blake3 import blake3
from moto import mock_aws
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import override_settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import (
    AuditEvent,
    BackupChunk,
    BackupPoint,
    BackupPolicy,
    BackupRecoveryKey,
    BackupRepository,
    BackupRun,
    Device,
    DeviceGroup,
    Job,
    RestoreJob,
    User,
)
from app.models.core import JobStatus
from app.modules.backup.clients import BackupClientError, client_for_policy, policy_repository
from app.modules.backup.dispatch import (
    dispatch_backup,
    dispatch_due_backup,
    ingest_backup_result,
    refusal_lookup,
)
from app.modules.backup.gc import gc_repository, seal_chunk, verify_point
from app.modules.backup.health import SUCCESS_AGE, run_backup_health_sweep
from app.modules.backup.offsite import (
    NO_RECOVERY_KEY_MESSAGE,
    OffsiteClientError,
    OffsiteError,
    get_target,
    prepare_repository,
    set_repository_target,
)
from app.modules.backup.policy import resolve_backup_policy
from app.modules.backup.reconcile import reconcile_stuck
from app.modules.backup.repository import create_repository, open_repo_key
from app.modules.backup.restore import ingest_restore_result, seal_manifest
from app.modules.backup.schema import (
    BackupManifest,
    BackupRestoreResult,
    BackupRunResult,
    FiletreeStream,
    ManifestEntry,
)
from app.modules.health.models import HealthAlert
from app.modules.reporting.definitions.backup_coverage import BackupCoverageParams
from app.modules.reporting.registry import REGISTRY
from app.modules.reporting.service import execute_report
from tests.backup_offsite_fixtures import (
    LOCK_BUCKET,
    OffsiteOrg,
    add_proven_key,
    add_target,
    local_settings,
    make_buckets,
)
from tests.client_scope_support import client_with_site, drop_client_keys, everything_on
from tests.conftest import ApiEnv, _psql
from tests.health_fixtures import seed_assignment, seed_check, seed_profile
from tests.test_backup_dispatch import _FakeJobEngine
from tests.test_backup_worker import _settings as _worker_settings

BK = "/api/v1/backup"
GUARD = "backup policy names another client's repository"
EVERY_CLIENT = "a policy for every client cannot name a repository; give each client its own"
OTHER_CLIENT_REPO = "that repository belongs to another client"
RUN_FK = "fk_backup_runs_repository_client"
POINT_FK = "fk_backup_points_repository_client"

#: Some session other than the holder's (`:me`) is waiting for a `clients`
#: row the holder locked (`tests/test_client_scope_deployments.py`, whose
#: comment explains the two shapes the wait takes in `pg_locks`).
WAITS_ON_A_CLIENT_ROW = text(
    "SELECT count(*) FROM pg_locks t "
    "WHERE t.locktype = 'tuple' AND t.relation = 'clients'::regclass AND t.pid <> :me "
    "AND (NOT t.granted OR EXISTS ("
    "  SELECT 1 FROM pg_locks w JOIN pg_locks h ON h.transactionid = w.transactionid "
    "  WHERE w.pid = t.pid AND w.locktype = 'transactionid' AND NOT w.granted "
    "  AND h.pid = :me AND h.locktype = 'transactionid' AND h.granted))"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _admin(env: ApiEnv, org: UUID) -> dict[str, str]:
    return await env.headers(org, await env.create_user(org, "org-admin"))


async def _repo(env: ApiEnv, admin: dict[str, str], client: UUID, name: str) -> str:
    r = await env.client.post(f"{BK}/repositories", json={"name": name, "client_id": str(client)},
                              headers=admin)
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == str(client)
    return str(r.json()["id"])


async def _user_id(env: ApiEnv, org: UUID) -> UUID:
    sub = await env.create_user(org, "org-admin")
    async with AsyncSession(env.admin_engine) as s:
        return (await s.execute(select(User.id).where(User.external_subject == sub))).scalar_one()


async def _group(env: ApiEnv, org: UUID, client: UUID) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        g = DeviceGroup(org_id=org, client_id=client, name=f"g-{uuid4().hex[:6]}")
        s.add(g)
        await s.commit()
        return g.id


async def _device(
    env: ApiEnv, org: UUID, client: UUID, site: UUID, group: UUID | None = None,
    *, created_at: datetime | None = None,
) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        d = Device(org_id=org, client_id=client, site_id=site, hostname=f"h-{uuid4().hex[:6]}",
                   os_family="linux", device_group_id=group)
        s.add(d)
        await s.flush()
        if created_at is not None:
            d.created_at = created_at
        await s.commit()
        return d.id


async def _service_repo(env: ApiEnv, org: UUID, client: UUID, actor: UUID) -> UUID:
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        repo = await create_repository(s, env.app.state.vault, org_id=org, actor_id=actor,
                                       client_id=client, name=f"r-{uuid4().hex[:6]}")
        return repo.id


async def _forced_policy(
    env: ApiEnv, org: UUID, repository: UUID, *, group: UUID | None = None,
    client: UUID | None = None, window_cron: str | None = None,
    created_at: datetime | None = None, retention_gfs: dict[str, int] | None = None,
) -> UUID:
    """A policy written with the guard switched off, inside one committed
    transaction (DDL is transactional, so no other session ever sees the
    trigger disabled): a row the guard would refuse, as 0242 can leave one
    behind (a group policy of a customer client naming a repository that
    went to the internal client) or a careless hand could plant one."""
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        await s.execute(text(
            "ALTER TABLE backup_policies DISABLE TRIGGER its_zz_backup_policy_repository_guard"
        ))
        policy = BackupPolicy(org_id=org, repository_id=repository, device_group_id=group,
                              client_id=client, sources={"paths": ["/srv"]},
                              window_cron=window_cron, retention_gfs=retention_gfs or {},
                              enabled=True)
        s.add(policy)
        await s.flush()
        if created_at is not None:
            policy.created_at = created_at
        await s.execute(text(
            "ALTER TABLE backup_policies ENABLE TRIGGER its_zz_backup_policy_repository_guard"
        ))
        await s.commit()
        return policy.id


async def _policy(
    env: ApiEnv, org: UUID, repository: UUID, *, group: UUID | None = None,
    client: UUID | None = None, window_cron: str | None = None,
    created_at: datetime | None = None,
) -> UUID:
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        policy = BackupPolicy(org_id=org, repository_id=repository, device_group_id=group,
                              client_id=client, sources={"paths": ["/srv"]},
                              window_cron=window_cron, retention_gfs={}, enabled=True)
        s.add(policy)
        await s.flush()
        if created_at is not None:
            policy.created_at = created_at
        await s.commit()
        return policy.id


async def _count(env: ApiEnv, model: Any, *where: Any) -> int:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(select(func.count()).select_from(model).where(*where))
        ).scalar_one()


async def _written(env: ApiEnv, org: UUID) -> tuple[int, int, int]:
    """(repositories, policies, audit events) of `org`: what a refused write
    must leave as it found it, in every table it touches."""
    return (
        await _count(env, BackupRepository, BackupRepository.org_id == org),
        await _count(env, BackupPolicy, BackupPolicy.org_id == org),
        await _count(env, AuditEvent, AuditEvent.org_id == org),
    )


async def _events(env: ApiEnv, org: UUID, *actions: str) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as s:
        query = select(AuditEvent).where(AuditEvent.org_id == org)
        if actions:
            query = query.where(AuditEvent.action.in_(actions))
        return list((await s.execute(query.order_by(AuditEvent.id))).scalars())


async def _wait_until_blocked(observer: AsyncSession, holder_pid: int) -> bool:
    for _ in range(400):
        await asyncio.sleep(0.025)
        waiting = (
            await observer.execute(WAITS_ON_A_CLIENT_ROW, {"me": holder_pid})
        ).scalar_one()
        await observer.rollback()
        if waiting:
            return True
    return False


async def _unassigned(env: ApiEnv, org: UUID) -> UUID:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                text("SELECT id FROM clients WHERE org_id = :o AND kind = 'unassigned'"),
                {"o": org},
            )
        ).scalar_one()


def _tomorrow(hour: int, minute: int = 0) -> datetime:
    day = (datetime.now(UTC) + timedelta(days=1)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


_LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The brief's tests
# --------------------------------------------------------------------------- #


async def test_a_repository_needs_a_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("bk-need")
    admin = await _admin(api_env, org)
    before = await _written(api_env, org)
    r = await api_env.client.post(f"{BK}/repositories", json={"name": "r"}, headers=admin)
    assert r.status_code == 422
    assert await _written(api_env, org) == before


async def test_two_clients_may_name_their_repositories_alike(api_env: ApiEnv) -> None:
    org = await api_env.create_org("bk-names")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    await _repo(api_env, admin, x, "principal")
    await _repo(api_env, admin, y, "principal")
    # ... and one client may not name two alike: a 409 with a sentence, not
    # the unique key's 500, and nothing written.
    before = await _written(api_env, org)
    r = await api_env.client.post(f"{BK}/repositories",
                                  json={"name": "principal", "client_id": str(x)}, headers=admin)
    assert (r.status_code, r.json()) == (
        409, {"detail": "a repository with that name exists in this client"}
    ), r.text
    assert await _written(api_env, org) == before


async def test_a_policy_cannot_name_another_client_s_repository(api_env: ApiEnv) -> None:
    org = await api_env.create_org("bk-policy")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    y_repo = await _repo(api_env, admin, y, "y")
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        group = DeviceGroup(org_id=org, client_id=x, name="Servidores")
        s.add(group)
        await s.flush()
        group_id = group.id
    before = await _written(api_env, org)
    r = await api_env.client.put(
        f"{BK}/policies",
        json={"device_group_id": str(group_id), "repository_id": y_repo,
              "window_cron": "0 2 * * *"},
        headers=admin,
    )
    assert (r.status_code, r.json()) == (422, {"detail": OTHER_CLIENT_REPO}), r.text
    assert await _written(api_env, org) == before
    with pytest.raises(DBAPIError, match=GUARD):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(BackupPolicy(org_id=org, device_group_id=group_id, repository_id=UUID(y_repo),
                               window_cron="0 2 * * *"))
            await s.flush()


async def test_a_default_policy_for_every_client_cannot_name_a_repository(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("bk-global")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    repo = await _repo(api_env, admin, x, "x")
    before = await _written(api_env, org)
    r = await api_env.client.put(
        f"{BK}/policies", json={"repository_id": repo, "window_cron": "0 2 * * *"},
        headers=admin,
    )
    assert (r.status_code, r.json()) == (422, {"detail": EVERY_CLIENT}), r.text
    assert await _written(api_env, org) == before
    # The database says the same: a row of no client and no group naming a
    # repository -- any repository, every one has a client -- is refused.
    with pytest.raises(DBAPIError, match=GUARD):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            s.add(BackupPolicy(org_id=org, repository_id=UUID(repo), window_cron="0 2 * * *"))
            await s.flush()


async def test_a_policy_cannot_name_a_repository_its_session_cannot_see(
    api_env: ApiEnv,
) -> None:
    """The guard reads the repository through the session's RLS: one it
    cannot see -- another organisation's, which the bare foreign key on
    `repository_id` allows -- reads as no client and is refused, for a
    client's policy and for one of every client alike."""
    a = await api_env.create_org("bk-guard-a")
    b = await api_env.create_org("bk-guard-b")
    b_repo = await _service_repo(api_env, b, b, await _user_id(api_env, b))
    for client in (a, None):
        with pytest.raises(DBAPIError, match=GUARD):
            async with org_session(a, clients=ClientScope.all(), engine=api_env.app_engine) as s:
                s.add(BackupPolicy(org_id=a, client_id=client, repository_id=b_repo))
                await s.flush()


# --------------------------------------------------------------------------- #
# Repositories: filed under the client the request names
# --------------------------------------------------------------------------- #


async def test_a_repository_is_filed_under_its_client_and_audited_there(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("bk-filed")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    repo = await _repo(api_env, admin, x, "x")
    async with AsyncSession(api_env.admin_engine) as s:
        row = (
            await s.execute(select(BackupRepository).where(BackupRepository.id == UUID(repo)))
        ).scalar_one()
    assert row.client_id == x
    (event,) = await _events(api_env, org, "backup.repository_created")
    assert (event.target_id, event.client_id) == (repo, x)
    # Invariant 3: the sealed key reference is in no audit metadata, nor in
    # the answer.
    assert row.encryption_key_ref not in str(event.meta)
    listed = await api_env.client.get(f"{BK}/repositories", headers=admin)
    assert [r["client_id"] for r in listed.json()] == [str(x)]
    assert "encryption_key_ref" not in listed.text


async def test_a_repository_is_refused_where_its_client_cannot_take_one(
    api_env: ApiEnv,
) -> None:
    """404 for a client the caller cannot see -- another organisation's, or
    outside a narrowed technician's scope -- never a 403 (spec §3.2); 409 for
    an archived client; 422 for Unassigned (C10). Nothing written for any."""
    org = await api_env.create_org("bk-refused")
    other = await api_env.create_org("bk-refused-other")
    foreign, _ = await client_with_site(api_env, other)
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    archived, _ = await client_with_site(api_env, org, client_archived=True)
    unassigned = await _unassigned(api_env, org)
    admin = await _admin(api_env, org)
    narrowed = await everything_on(api_env, org, x)
    cases = [
        (admin, foreign, 404, "no such client"),
        (narrowed, y, 404, "no such client"),
        (admin, archived, 409, "that client is archived"),
        (admin, unassigned, 422, "a backup repository cannot be filed under Unassigned"),
    ]
    for headers, client, status, detail in cases:
        before = await _written(api_env, org)
        r = await api_env.client.post(
            f"{BK}/repositories", json={"name": "r", "client_id": str(client)}, headers=headers
        )
        assert (r.status_code, r.json()) == (status, {"detail": detail}), (client, r.text)
        assert await _written(api_env, org) == before
    # The positive twin: the same narrowed technician, in their own client.
    await _repo(api_env, narrowed, x, "mine")


async def test_a_narrowed_technician_sees_only_their_client_s_repositories(
    api_env: ApiEnv,
) -> None:
    org = await api_env.create_org("bk-list")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "same")
    await _repo(api_env, admin, y, "same")
    narrowed = await everything_on(api_env, org, x)
    r = await api_env.client.get(f"{BK}/repositories", headers=narrowed)
    assert [row["id"] for row in r.json()] == [x_repo]


async def test_a_repository_create_takes_its_client_before_the_audit_chain(
    api_env: ApiEnv,
) -> None:
    """The create reads its client FOR SHARE as its FIRST lock: an archive
    holds the client and takes the audit chain's lock next, so a create that
    audited first and read the client after would deadlock with it (40P01).
    With the client held, the create waits BEFORE the audit lock; the holder
    takes that lock and commits; the create then goes through."""
    org = await api_env.create_org("bk-lock-repo")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    body = {"name": "r", "client_id": str(x)}
    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text("SELECT id FROM clients WHERE id = :x FOR NO KEY UPDATE"), {"x": x}
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        create = asyncio.create_task(
            api_env.client.post(f"{BK}/repositories", json=body, headers=admin)
        )
        if not await _wait_until_blocked(observer, me):
            create.cancel()
            pytest.fail("the create never waited on the client lock")
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :o))"), {"o": str(org)}
        )
        await holder.commit()
    r = await create
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------- #
# Policies: of one client, naming that client's repository
# --------------------------------------------------------------------------- #


async def test_a_group_policy_is_its_group_s_client_s(api_env: ApiEnv) -> None:
    org = await api_env.create_org("bk-gpol")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    group = await _group(api_env, org, x)
    r = await api_env.client.put(
        f"{BK}/policies",
        json={"device_group_id": str(group), "repository_id": x_repo, "enabled": True},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["client_id"], r.json()["device_group_id"]) == (str(x), str(group))
    (event,) = await _events(api_env, org, "backup.policy_changed")
    assert event.client_id == x
    # A body client that contradicts the group's is a 422, and nothing moves.
    before = await _written(api_env, org)
    r = await api_env.client.put(
        f"{BK}/policies",
        json={"device_group_id": str(group), "client_id": str(y), "repository_id": x_repo},
        headers=admin,
    )
    assert (r.status_code, r.json()) == (
        422, {"detail": "that device group belongs to another client"}
    ), r.text
    assert await _written(api_env, org) == before
    # Naming the group's own client is no contradiction.
    r = await api_env.client.put(
        f"{BK}/policies",
        json={"device_group_id": str(group), "client_id": str(x), "repository_id": x_repo},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    assert await _count(api_env, BackupPolicy, BackupPolicy.org_id == org) == 1


async def test_a_client_s_default_policy_is_one_row_per_client(api_env: ApiEnv) -> None:
    """No group and a client: that client's default. A second PUT for the
    client replaces it; another client's default is a row of its own."""
    org = await api_env.create_org("bk-default")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    y_repo = await _repo(api_env, admin, y, "y")
    first = await api_env.client.put(
        f"{BK}/policies", json={"client_id": str(x), "repository_id": x_repo,
                                "window_cron": "0 1 * * *"}, headers=admin,
    )
    again = await api_env.client.put(
        f"{BK}/policies", json={"client_id": str(x), "repository_id": x_repo,
                                "window_cron": "0 3 * * *"}, headers=admin,
    )
    other = await api_env.client.put(
        f"{BK}/policies", json={"client_id": str(y), "repository_id": y_repo}, headers=admin,
    )
    assert [p.status_code for p in (first, again, other)] == [200, 200, 200]
    assert first.json()["id"] == again.json()["id"] != other.json()["id"]
    assert again.json()["window_cron"] == "0 3 * * *"
    assert (again.json()["client_id"], other.json()["client_id"]) == (str(x), str(y))
    assert again.json()["device_group_id"] is None
    listed = await api_env.client.get(f"{BK}/policies", headers=admin)
    assert {(p["client_id"], p["repository_id"]) for p in listed.json()} == {
        (str(x), x_repo), (str(y), y_repo)
    }
    events = await _events(api_env, org, "backup.policy_changed")
    assert [e.client_id for e in events] == [x, x, y]
    # And X's default cannot name Y's repository.
    before = await _written(api_env, org)
    r = await api_env.client.put(
        f"{BK}/policies", json={"client_id": str(x), "repository_id": y_repo}, headers=admin,
    )
    assert (r.status_code, r.json()) == (422, {"detail": OTHER_CLIENT_REPO}), r.text
    assert await _written(api_env, org) == before


async def test_a_narrowed_technician_writes_policies_only_in_their_client(
    api_env: ApiEnv,
) -> None:
    """Y's repository, Y's group and Y itself are invisible to a technician
    of X: each is the 404 of a thing that does not exist. The positive twin:
    the same technician writes X's group policy and X's default."""
    org = await api_env.create_org("bk-narrow")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    y_repo = await _repo(api_env, admin, y, "y")
    x_group = await _group(api_env, org, x)
    y_group = await _group(api_env, org, y)
    narrowed = await everything_on(api_env, org, x)
    cases = [
        ({"device_group_id": str(x_group), "repository_id": y_repo}, "no such repository"),
        ({"device_group_id": str(y_group), "repository_id": x_repo}, "no such device group"),
        ({"client_id": str(y), "repository_id": x_repo}, "no such client"),
    ]
    for body, detail in cases:
        before = await _written(api_env, org)
        r = await api_env.client.put(f"{BK}/policies", json=body, headers=narrowed)
        assert (r.status_code, r.json()) == (404, {"detail": detail}), (body, r.text)
        assert await _written(api_env, org) == before
    for body in ({"device_group_id": str(x_group), "repository_id": x_repo},
                 {"client_id": str(x), "repository_id": x_repo}):
        r = await api_env.client.put(f"{BK}/policies", json=body, headers=narrowed)
        assert r.status_code == 200, r.text
        assert r.json()["client_id"] == str(x)
    listed = await api_env.client.get(f"{BK}/policies", headers=narrowed)
    assert {p["client_id"] for p in listed.json()} == {str(x)}


async def test_a_policy_is_refused_where_its_client_cannot_take_one(api_env: ApiEnv) -> None:
    """The policy's client is held like a new row's (`client_for_new_rows`):
    an archived client's is a 409, Unassigned's a 422, another
    organisation's client the 404 of one that does not exist."""
    org = await api_env.create_org("bk-pol-closed")
    other = await api_env.create_org("bk-pol-closed-b")
    foreign, _ = await client_with_site(api_env, other)
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(text("UPDATE clients SET status = 'archived' WHERE id = :x"), {"x": x})
        await s.commit()
    unassigned = await _unassigned(api_env, org)
    cases = [
        ({"client_id": str(x), "repository_id": x_repo}, 409, "that client is archived"),
        ({"client_id": str(unassigned), "repository_id": x_repo}, 422,
         "a backup policy cannot be filed under Unassigned"),
        ({"client_id": str(foreign), "repository_id": x_repo}, 404, "no such client"),
    ]
    for body, status, detail in cases:
        before = await _written(api_env, org)
        r = await api_env.client.put(f"{BK}/policies", json=body, headers=admin)
        assert (r.status_code, r.json()) == (status, {"detail": detail}), (body, r.text)
        assert await _written(api_env, org) == before


async def test_a_legacy_policy_can_be_stopped_without_a_repository_of_its_own(
    api_env: ApiEnv,
) -> None:
    """Review of 2240971, Important 1: a policy 0242 leaves naming another
    client's repository can be switched off as it stands -- `enabled=false`
    and its stored repository -- so a person can stop the refusals before
    Y has a repository of its own. The guard allows it (it fires only on
    UPDATE OF the repository, the client and the group); the write keeps the
    pairing it found, is audited in the policy's client, and the positive
    twin is the same body for a group-less default."""
    org = await api_env.create_org("bk-stop")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    group = await _group(api_env, org, y)
    grouped = await _forced_policy(api_env, org, x_repo, group=group, window_cron="0 2 * * *")
    default = await _forced_policy(api_env, org, x_repo, client=y, window_cron="0 2 * * *")

    for policy, scope in ((grouped, {"device_group_id": str(group)}),
                          (default, {"client_id": str(y)})):
        body = {**scope, "repository_id": str(x_repo), "sources": {"paths": ["/srv"]},
                "window_cron": "0 2 * * *", "enabled": False}
        r = await api_env.client.put(f"{BK}/policies", json=body, headers=admin)
        assert r.status_code == 200, r.text
        assert (r.json()["id"], r.json()["client_id"], r.json()["enabled"]) == (
            str(policy), str(y), False
        )
        async with AsyncSession(api_env.admin_engine) as s:
            stored = (await s.execute(select(BackupPolicy).where(
                BackupPolicy.id == policy))).scalar_one()
            assert (stored.enabled, stored.repository_id, stored.client_id) == (False, x_repo, y)
    changed = await _events(api_env, org, "backup.policy_changed")
    assert [(e.target_id, e.client_id, e.meta["created"]) for e in changed] == [
        (str(grouped), y, False), (str(default), y, False),
    ]


async def test_a_legacy_policy_is_never_switched_on_or_repointed_across_clients(
    api_env: ApiEnv,
) -> None:
    """The stop is that and nothing wider: switching the same policy on, or
    naming a third repository of another client while switching it off, is
    the 422 of any policy naming another client's repository -- and so is a
    NEW disabled policy naming one (there is no stored pairing to keep).
    Nothing is written anywhere, the audit log included."""
    org = await api_env.create_org("bk-stop-only")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    x_other = await _service_repo(api_env, org, x, actor)
    group = await _group(api_env, org, y)
    fresh_group = await _group(api_env, org, y)
    policy = await _forced_policy(api_env, org, x_repo, group=group, window_cron="0 2 * * *")
    async with AsyncSession(api_env.admin_engine) as s:
        await s.execute(text("UPDATE backup_policies SET enabled = false WHERE id = :p"),
                        {"p": policy})
        await s.commit()
    cases = [
        {"device_group_id": str(group), "repository_id": str(x_repo), "enabled": True},
        {"device_group_id": str(group), "repository_id": str(x_other), "enabled": False},
        {"device_group_id": str(fresh_group), "repository_id": str(x_repo), "enabled": False},
    ]
    for body in cases:
        before = await _written(api_env, org)
        r = await api_env.client.put(f"{BK}/policies", json=body, headers=admin)
        assert (r.status_code, r.json()) == (422, {"detail": OTHER_CLIENT_REPO}), (body, r.text)
        assert await _written(api_env, org) == before
    async with AsyncSession(api_env.admin_engine) as s:
        stored = (await s.execute(select(BackupPolicy).where(
            BackupPolicy.id == policy))).scalar_one()
        assert (stored.enabled, stored.repository_id) == (False, x_repo)


async def test_a_policy_write_takes_its_client_before_the_audit_chain(
    api_env: ApiEnv,
) -> None:
    """The upsert reads the policy's client FOR SHARE as its FIRST lock, as
    the repository create does (above)."""
    org = await api_env.create_org("bk-lock-pol")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    group = await _group(api_env, org, x)
    body = {"device_group_id": str(group), "repository_id": x_repo}
    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text("SELECT id FROM clients WHERE id = :x FOR NO KEY UPDATE"), {"x": x}
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        put = asyncio.create_task(
            api_env.client.put(f"{BK}/policies", json=body, headers=admin)
        )
        if not await _wait_until_blocked(observer, me):
            put.cancel()
            pytest.fail("the policy write never waited on the client lock")
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :o))"), {"o": str(org)}
        )
        await holder.commit()
    r = await put
    assert r.status_code == 200, r.text


async def test_the_guard_sees_the_client_its_group_gives(api_env: ApiEnv) -> None:
    """`its_zz_backup_policy_repository_guard` fires after
    `its_client_from_parent` (Postgres fires BEFORE triggers by name), so it
    judges the client the new group gives the row, not the one it had. And
    it fires on the columns that can change the pairing -- not on a window
    edit of a row it would refuse, which 0242 may have left behind."""
    org = await api_env.create_org("bk-guard")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    y_repo = await _service_repo(api_env, org, y, actor)
    x_group = await _group(api_env, org, x)
    y_group = await _group(api_env, org, y)
    policy = await _policy(api_env, org, x_repo, group=x_group)

    for column, value in (("device_group_id", y_group), ("repository_id", y_repo)):
        with pytest.raises(DBAPIError, match=GUARD):
            async with AsyncSession(api_env.admin_engine) as s, s.begin():
                await s.execute(
                    text(f"UPDATE backup_policies SET {column} = :v WHERE id = :p"),
                    {"v": value, "p": policy},
                )
    # Both at once is a consistent pair: Y's group, Y's repository.
    async with AsyncSession(api_env.admin_engine) as s, s.begin():
        await s.execute(
            text("UPDATE backup_policies SET device_group_id = :g, repository_id = :r "
                 "WHERE id = :p"),
            {"g": y_group, "r": y_repo, "p": policy},
        )
    async with AsyncSession(api_env.admin_engine) as s:
        assert (await s.execute(
            select(BackupPolicy.client_id).where(BackupPolicy.id == policy)
        )).scalar_one() == y

    legacy = await _forced_policy(api_env, org, x_repo, group=y_group)
    async with AsyncSession(api_env.admin_engine) as s, s.begin():
        await s.execute(
            text("UPDATE backup_policies SET window_cron = '0 4 * * *' WHERE id = :p"),
            {"p": legacy},
        )


# --------------------------------------------------------------------------- #
# Resolution and dispatch: the window runs under scope all
# --------------------------------------------------------------------------- #


async def test_a_device_resolves_its_own_client_s_default_policy(api_env: ApiEnv) -> None:
    """Group policy, then the device's client's default (`client_id =
    device.client_id`, no group), then a global one -- which has no
    repository. Under scope all, where RLS hides no client, and on the admin
    engine, where it hides nothing at all: the code's own predicate."""
    org = await api_env.create_org("bk-resolve")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    z, z_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    y_repo = await _service_repo(api_env, org, y, actor)
    # Y's default first, so "the first default of the org" would be Y's.
    await _policy(api_env, org, y_repo, client=y)
    await _policy(api_env, org, x_repo, client=x)
    x_device = await _device(api_env, org, x, x_site)
    y_device = await _device(api_env, org, y, y_site)
    z_device = await _device(api_env, org, z, z_site)

    def session_factories() -> list[Callable[[], Any]]:
        return [
            lambda: org_session(org, clients=ClientScope.all(), engine=api_env.app_engine),
            lambda: AsyncSession(api_env.admin_engine),
        ]

    for make in session_factories():
        async with make() as s:
            got = {
                device: await resolve_backup_policy(s, org, device)
                for device in (x_device, y_device, z_device)
            }
        assert got[x_device] is not None and got[x_device].repository_id == x_repo
        assert got[y_device] is not None and got[y_device].repository_id == y_repo
        assert got[z_device] is None

    # A global row -- only a forced one can exist -- is a schedule and a
    # retention for whoever has no other, and hands out no repository.
    await _forced_policy(api_env, org, x_repo, window_cron="0 5 * * *")
    for make in session_factories():
        async with make() as s:
            resolved = await resolve_backup_policy(s, org, z_device)
            x_still = await resolve_backup_policy(s, org, x_device)
        assert resolved is not None
        assert (resolved.repository_id, resolved.window_cron) == (None, "0 5 * * *")
        assert x_still is not None and x_still.repository_id == x_repo


async def test_a_global_policy_never_sends_a_repository_key(api_env: ApiEnv) -> None:
    org = await api_env.create_org("bk-global-key")
    x, x_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    await _forced_policy(api_env, org, x_repo)
    device = await _device(api_env, org, x, x_site)
    engine = _FakeJobEngine()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        pending = await dispatch_backup(s, engine, api_env.app.state.vault, org_id=org,
                                        device_id=device, now=datetime.now(UTC))
    assert (pending, engine.created) == ([], [])


async def test_the_window_never_hands_a_device_another_client_s_repository(
    api_env: ApiEnv,
) -> None:
    """Spec §5 test 4 for the dispatch: the window sweep runs under scope
    all. A device of Y under a policy that names X's repository -- forced in
    with the guard off, as 0242 can leave one -- gets no job and no key, and
    the refusal is recorded in Y; once the start deadline passes, no
    `missed` row is written either (it could only name X's repository, which
    the composite key refuses: that would abort the sweep for the whole
    organisation). In the same session, a device of Y under Y's own
    repository is dispatched."""
    org = await api_env.create_org("bk-window")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    vault = api_env.app.state.vault
    x_repo = await _service_repo(api_env, org, x, actor)
    y_repo = await _service_repo(api_env, org, y, actor)
    bad_group = await _group(api_env, org, y)
    good_group = await _group(api_env, org, y)
    await _forced_policy(api_env, org, x_repo, group=bad_group, window_cron="0 2 * * *",
                         created_at=_LONG_AGO)
    await _policy(api_env, org, y_repo, group=good_group, window_cron="0 2 * * *",
                  created_at=_LONG_AGO)
    refused = await _device(api_env, org, y, y_site, bad_group, created_at=_LONG_AGO)
    served = await _device(api_env, org, y, y_site, good_group, created_at=_LONG_AGO)

    engine = _FakeJobEngine()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        none = await dispatch_due_backup(s, engine, vault, org_id=org, device_id=refused,
                                         now=_tomorrow(2, 5))
        one = await dispatch_due_backup(s, engine, vault, org_id=org, device_id=served,
                                        now=_tomorrow(2, 5))
        late = await dispatch_due_backup(s, engine, vault, org_id=org, device_id=refused,
                                         now=_tomorrow(9))
    assert (none, late) == ([], [])
    assert [p[1] for p in one] == [served]
    assert [c["target_device_id"] for c in engine.created] == [served]
    assert engine.created[0]["params"]["repository_id"] == str(y_repo)
    assert await _count(api_env, BackupRun, BackupRun.device_id == refused) == 0
    runs = await _count(api_env, BackupRun, BackupRun.device_id == served,
                        BackupRun.repository_id == y_repo)
    assert runs == 1

    (refusal,) = await _events(api_env, org, "backup.dispatch_refused")
    assert (refusal.target_id, refusal.client_id) == (str(refused), y)
    assert refusal.meta["reason"] == "repository_of_another_client"
    assert refusal.meta["repository_id"] == str(x_repo)
    (dispatched,) = await _events(api_env, org, "backup.dispatched")
    assert (dispatched.target_id, dispatched.client_id) == (str(served), y)
    assert not await _events(api_env, org, "backup.missed")


async def test_run_now_refuses_another_client_s_repository_too(api_env: ApiEnv) -> None:
    """The on-demand path is the same `_dispatch_for_policy`: an empty job
    list, and the refusal recorded -- EVERY time. A person's Run now is
    their act, recorded each time it is refused (invariant 4); only the
    window's own repetition is recorded once (below)."""
    org = await api_env.create_org("bk-runnow")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    group = await _group(api_env, org, y)
    await _forced_policy(api_env, org, x_repo, group=group)
    device = await _device(api_env, org, y, y_site, group)
    for _ in range(2):
        r = await api_env.client.post(f"{BK}/devices/{device}/run", headers=admin)
        assert (r.status_code, r.json()) == (200, {"job_ids": []}), r.text
    refusals = await _events(api_env, org, "backup.dispatch_refused")
    assert [(e.actor_type, e.client_id) for e in refusals] == [("user", y), ("user", y)]


async def test_a_missed_fire_is_recorded_in_the_device_s_client(api_env: ApiEnv) -> None:
    org = await api_env.create_org("bk-missed")
    y, y_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    y_repo = await _service_repo(api_env, org, y, actor)
    group = await _group(api_env, org, y)
    await _policy(api_env, org, y_repo, group=group, window_cron="0 2 * * *",
                  created_at=_LONG_AGO)
    device = await _device(api_env, org, y, y_site, group, created_at=_LONG_AGO)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        await dispatch_due_backup(s, _FakeJobEngine(), api_env.app.state.vault, org_id=org,
                                  device_id=device, now=_tomorrow(9))
    (missed,) = await _events(api_env, org, "backup.missed")
    assert (missed.target_id, missed.client_id) == (str(device), y)
    assert await _count(api_env, BackupRun, BackupRun.device_id == device,
                        BackupRun.state == "missed") == 1


REFUSAL_LOOKUP = "'backup.dispatch_refused'"


async def _refused_world(
    env: ApiEnv, tag: str
) -> tuple[UUID, UUID, UUID, UUID, list[UUID], UUID]:
    """(org, Y, X's repository, the legacy policy, the two devices of Y under
    it, a device of Y under Y's own repository): a group policy of Y naming
    X's repository, as 0242 leaves one, its window at 02:00, everything
    protected since long ago."""
    org = await env.create_org(tag)
    x, _ = await client_with_site(env, org)
    y, y_site = await client_with_site(env, org)
    actor = await _user_id(env, org)
    x_repo = await _service_repo(env, org, x, actor)
    y_repo = await _service_repo(env, org, y, actor)
    bad_group = await _group(env, org, y)
    good_group = await _group(env, org, y)
    policy = await _forced_policy(env, org, x_repo, group=bad_group, window_cron="0 2 * * *",
                                  created_at=_LONG_AGO)
    await _policy(env, org, y_repo, group=good_group, window_cron="0 2 * * *",
                  created_at=_LONG_AGO)
    refused = [await _device(env, org, y, y_site, bad_group, created_at=_LONG_AGO)
               for _ in range(2)]
    served = await _device(env, org, y, y_site, good_group, created_at=_LONG_AGO)
    return org, y, x_repo, policy, refused, served


async def _sweep(env: ApiEnv, org: UUID, devices: list[UUID], now: datetime) -> list[str]:
    """One window sweep over `devices` as the worker runs it -- scope all, one
    transaction -- returning every statement it sent."""
    seen: list[str] = []

    def tick(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        seen.append(statement)

    engine = env.app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", tick)
    try:
        async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
            for device in devices:
                await dispatch_due_backup(s, _FakeJobEngine(), env.app.state.vault,
                                          org_id=org, device_id=device, now=now)
    finally:
        event.remove(engine, "before_cursor_execute", tick)
    return seen


async def test_the_window_records_a_refusal_once_per_device_and_policy_version(
    api_env: ApiEnv,
) -> None:
    """Review of 2240971, Important 1: the sweep runs every 15 minutes and a
    fire stays due for six hours, so recording every refusal wrote up to 24
    rows per fire per device into the hash-chained log. Three sweeps inside
    one fire write ONE refusal per device. Each sweep asks once per refused
    device whether it has already recorded one -- one indexed lookup, no
    fan-out -- and never asks for a device it dispatches."""
    org, _y, x_repo, policy, refused, served = await _refused_world(api_env, "bk-once")
    for minute in (5, 20, 35):
        seen = await _sweep(api_env, org, [*refused, served], _tomorrow(2, minute))
        assert len([q for q in seen if REFUSAL_LOOKUP in q]) == len(refused), minute
    refusals = await _events(api_env, org, "backup.dispatch_refused")
    assert sorted(e.target_id for e in refusals) == sorted(str(d) for d in refused)
    assert {(e.actor_type, e.meta["repository_id"], e.meta["policy_id"]) for e in refusals} == {
        ("system", str(x_repo), str(policy))
    }
    for device in refused:
        assert await _count(api_env, BackupRun, BackupRun.device_id == device) == 0
    assert await _count(api_env, BackupRun, BackupRun.device_id == served) == 1


async def test_a_refusal_is_recorded_again_once_its_policy_changes(api_env: ApiEnv) -> None:
    """The record is per policy VERSION: once the policy is saved again (its
    `updated_at` moves) the next sweep records one new refusal per device,
    and the sweeps after it none."""
    org, _y, _x_repo, policy, refused, _served = await _refused_world(api_env, "bk-again")
    await _sweep(api_env, org, refused, _tomorrow(2, 5))
    async with AsyncSession(api_env.admin_engine) as s:
        row = (await s.execute(select(BackupPolicy).where(BackupPolicy.id == policy))
               ).scalar_one()
        first_version = row.updated_at
        row.sources = {"paths": ["/srv", "/home"]}
        await s.commit()
    for minute in (20, 35):
        await _sweep(api_env, org, refused, _tomorrow(2, minute))
    refusals = await _events(api_env, org, "backup.dispatch_refused")
    assert sorted(e.target_id for e in refusals) == sorted(str(d) for d in refused * 2)
    versions = [e.meta["policy_updated_at"] for e in refusals]
    assert versions[:2] == [first_version.astimezone(UTC).isoformat()] * 2
    assert versions[2] == versions[3] != versions[0]


async def test_a_refused_device_still_reads_as_not_backed_up(api_env: ApiEnv) -> None:
    """Recording the refusal once hides nothing else: the health check still
    alerts on a device that has not been backed up for days, and the
    coverage report still lists it unprotected."""
    org, _y, _x_repo, _policy_id, refused, _served = await _refused_world(api_env, "bk-seen")
    for minute in (5, 20):
        await _sweep(api_env, org, refused, _tomorrow(2, minute))
    profile = await seed_profile(api_env.admin_engine, org)
    await seed_assignment(api_env.admin_engine, org, profile)
    await seed_check(api_env.admin_engine, org, profile, check_type=SUCCESS_AGE,
                     comparator="gt", threshold_num=50, severity="critical", for_ticks=1)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        await run_backup_health_sweep(s, org, now=_tomorrow(3))
    async with AsyncSession(api_env.admin_engine) as s:
        alerted = set((await s.execute(select(HealthAlert.device_id).where(
            HealthAlert.org_id == org, HealthAlert.check_type == SUCCESS_AGE,
            HealthAlert.state != "cleared",
        ))).scalars())
    assert set(refused) <= alerted
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        page = await execute_report(s, REGISTRY["backup-coverage"], BackupCoverageParams(),
                                    org_id=org)
    by_id = {row.device_id: row for row in page.rows}
    for device in refused:
        assert (by_id[device].policy_present, by_id[device].coverage) == (True, "unprotected")


def _lookup_sql() -> str:
    """The window's "already refused?" question as PostgreSQL receives it,
    every bound parameter a `$n` -- a prepared statement's shape."""
    import re

    from sqlalchemy.dialects import postgresql

    compiled = str(
        refusal_lookup(
            org_id=uuid4(), device_id=uuid4(), client_id=uuid4(), repository_id=uuid4(),
            policy_id=uuid4(), policy_version="2026-01-01T00:00:00+00:00",
        ).compile(dialect=postgresql.dialect())
    )
    names: list[str] = []

    def positional(match: "re.Match[str]") -> str:
        names.append(match.group(1))
        return f"${len(names)}"

    return re.sub(r"%\((\w+)\)s", positional, compiled)


async def test_the_refusal_lookup_is_one_index_probe_even_in_a_generic_plan(
    api_env: ApiEnv, pg_cluster: Any
) -> None:
    """The lookup states the action as an inline literal, so the planner
    proves `ix_audit_backup_dispatch_refused`'s predicate even in the generic
    plan asyncpg's prepared statements reach after a few runs (the 0220
    precedent, `tests/test_mcp_admin_api.py`)."""
    sql = _lookup_sql()
    assert "audit_events.action = 'backup.dispatch_refused'" in sql
    plan = _psql("SET enable_seqscan = off; EXPLAIN (GENERIC_PLAN) " + sql, pg_cluster.dbname)
    assert "ix_audit_backup_dispatch_refused" in plan, plan


async def test_another_org_s_policy_planted_under_this_client_resolves_nothing(
    api_env: ApiEnv,
) -> None:
    """THE RESOLUTION'S ORG PREDICATES (lesson 2). Org B's default and global
    policies planted under ORG A's client -- the keys that make such rows
    impossible dropped, the guard off, rolled back -- on the admin session,
    where only each lookup's `org_id` keeps them out."""
    a = await api_env.create_org("bk-res-a")
    b = await api_env.create_org("bk-res-b")
    x, x_site = await client_with_site(api_env, a)
    b_actor = await _user_id(api_env, b)
    b_repo = await _service_repo(api_env, b, b, b_actor)
    device = await _device(api_env, a, x, x_site)
    async with AsyncSession(api_env.admin_engine) as s:
        await drop_client_keys(s, "backup_policies", "fk_backup_policies_client_org")
        await s.execute(text(
            "ALTER TABLE backup_policies DISABLE TRIGGER its_zz_backup_policy_repository_guard"
        ))
        s.add_all([
            BackupPolicy(org_id=b, client_id=x, repository_id=b_repo, enabled=True),
            BackupPolicy(org_id=b, client_id=None, repository_id=b_repo, enabled=True),
        ])
        await s.flush()
        resolved = await resolve_backup_policy(s, a, device)
        await s.rollback()
    assert resolved is None


async def test_a_client_s_default_wins_over_an_older_policy_for_every_client(
    api_env: ApiEnv,
) -> None:
    """The client's default comes first whatever the row ids say: a policy
    for every client written before it (a smaller uuid7) must not win."""
    org = await api_env.create_org("bk-order")
    x, x_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    older = await _forced_policy(api_env, org, x_repo, window_cron="0 5 * * *")
    newer = await _policy(api_env, org, x_repo, client=x, window_cron="0 1 * * *")
    assert str(older) < str(newer)
    device = await _device(api_env, org, x, x_site)
    async with AsyncSession(api_env.admin_engine) as s:
        resolved = await resolve_backup_policy(s, org, device)
    assert resolved is not None
    assert (resolved.id, resolved.repository_id) == (newer, x_repo)


async def test_another_org_s_device_resolves_nothing_here(api_env: ApiEnv) -> None:
    """The device lookup's own org predicate: org B's device, resolved under
    org A -- which has a policy for every client that would otherwise
    apply -- resolves nothing. On the admin engine, where RLS is not doing
    the rejecting."""
    a = await api_env.create_org("bk-dev-a")
    b = await api_env.create_org("bk-dev-b")
    a_repo = await _service_repo(api_env, a, a, await _user_id(api_env, a))
    await _forced_policy(api_env, a, a_repo)
    b_device = await _device(api_env, b, b, b)
    async with AsyncSession(api_env.admin_engine) as s:
        assert await resolve_backup_policy(s, a, b_device) is None


async def test_a_policy_s_client_lookups_keep_to_their_org(api_env: ApiEnv) -> None:
    """THE ORG PREDICATES of `client_for_policy`, `policy_repository` and
    the repository create's client, on the admin engine (no RLS): org B's
    group, org B's repository and org B's client are, for org A, things that
    do not exist -- the 404 each gets when absent, not the 422 or the other
    404 the row would earn if it were found."""
    a = await api_env.create_org("bk-orgs-a")
    b = await api_env.create_org("bk-orgs-b")
    a_actor = await _user_id(api_env, a)
    b_repo = await _service_repo(api_env, b, b, await _user_id(api_env, b))
    b_group = await _group(api_env, b, b)
    cases: list[tuple[Callable[[AsyncSession], Any], str]] = [
        (lambda s: client_for_policy(s, org_id=a, device_group_id=b_group, client_id=None),
         "no such device group"),
        (lambda s: policy_repository(s, org_id=a, client_id=a, repository_id=b_repo),
         "no such repository"),
        (lambda s: client_for_policy(s, org_id=a, device_group_id=None, client_id=b),
         "no such client"),
    ]
    for call, detail in cases:
        async with AsyncSession(api_env.admin_engine) as s:
            with pytest.raises(BackupClientError) as refused:
                await call(s)
            await s.rollback()
        assert (refused.value.status_code, refused.value.detail) == (404, detail), detail
    async with AsyncSession(api_env.admin_engine) as s:
        with pytest.raises(BackupClientError) as refused:
            await create_repository(s, api_env.app.state.vault, org_id=a, actor_id=a_actor,
                                    client_id=b, name="r")
        await s.rollback()
    assert (refused.value.status_code, refused.value.detail) == (404, "no such client")


# --------------------------------------------------------------------------- #
# Runs and points: the composite key, NOT VALID on purpose
# --------------------------------------------------------------------------- #


async def test_a_new_run_or_point_names_a_repository_of_its_own_client(
    api_env: ApiEnv,
) -> None:
    """NOT VALID skips the rows that predate 0242, never a new one: a run of
    Y's device naming X's repository is refused by the key, and so is a
    point; Y's own repository is fine."""
    org = await api_env.create_org("bk-fk")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    y_repo = await _service_repo(api_env, org, y, actor)
    device = await _device(api_env, org, y, y_site)
    with pytest.raises(IntegrityError, match=RUN_FK):
        async with AsyncSession(api_env.admin_engine) as s, s.begin():
            s.add(BackupRun(org_id=org, device_id=device, repository_id=x_repo,
                            state="success", actor_type="system"))
            await s.flush()
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s, s.begin():
        run = BackupRun(org_id=org, device_id=device, repository_id=y_repo,
                        state="success", actor_type="system")
        s.add(run)
        await s.flush()
    with pytest.raises(IntegrityError, match=POINT_FK):
        async with AsyncSession(api_env.admin_engine) as s, s.begin():
            s.add(BackupPoint(org_id=org, device_id=device, repository_id=x_repo,
                              backup_run_id=run.id, manifest_ref="m", size=1,
                              state="complete"))
            await s.flush()
    async with AsyncSession(api_env.admin_engine) as s, s.begin():
        s.add(BackupPoint(org_id=org, device_id=device, repository_id=y_repo,
                          backup_run_id=run.id, manifest_ref="m", size=1, state="complete"))
        await s.flush()


async def test_the_composite_keys_are_not_valid_so_history_is_not_rejudged(
    api_env: ApiEnv,
) -> None:
    """Why NOT VALID (0242's docstring): before 0242 a device of a customer
    client could back up into an organisation repository, which 0242 files
    under the internal client. Such a row cannot satisfy the key, and a
    validated key would abort the migration on it. Shown here: with one
    such row present -- planted with the key dropped, rolled back -- the key
    can be added NOT VALID, and cannot be validated."""
    org = await api_env.create_org("bk-notvalid")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    device = await _device(api_env, org, y, y_site)
    async with AsyncSession(api_env.admin_engine) as s:
        validated = dict((await s.execute(text(
            "SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY(:n)"
        ), {"n": [RUN_FK, POINT_FK]})).all())
        assert validated == {RUN_FK: False, POINT_FK: False}

        await drop_client_keys(s, "backup_runs", RUN_FK)
        s.add(BackupRun(org_id=org, device_id=device, repository_id=x_repo,
                        state="success", actor_type="system"))
        await s.flush()
        await s.execute(text(
            f"ALTER TABLE backup_runs ADD CONSTRAINT {RUN_FK} FOREIGN KEY "
            "(repository_id, client_id) REFERENCES backup_repositories (id, client_id) NOT VALID"
        ))
        await s.execute(text("SAVEPOINT validate"))
        with pytest.raises(DBAPIError, match=RUN_FK):
            await s.execute(text(f"ALTER TABLE backup_runs VALIDATE CONSTRAINT {RUN_FK}"))
        await s.rollback()


async def test_a_run_from_before_0242_lands_failed_rather_than_on_another_client(
    api_env: ApiEnv,
) -> None:
    """A run dispatched before 0242 whose device and repository are now of
    different clients finishes after it: its point could only name the other
    client's repository, which the key refuses -- an exception inside the
    job's result handler. It lands `failed` instead, with no point, and the
    object store is never even opened.

    The run is planted the way history holds it: committed in an earlier
    transaction (the key dropped and added back NOT VALID inside it), so the
    result's UPDATE of its state is not re-judged -- Postgres re-checks a
    row's unchanged key only when the same transaction inserted or updated
    it."""
    org = await api_env.create_org("bk-legacy-run")
    x, _ = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _service_repo(api_env, org, x, actor)
    device = await _device(api_env, org, y, y_site)
    opened: list[bool] = []

    def factory() -> Any:
        opened.append(True)
        raise AssertionError("the object store must not be opened")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        await drop_client_keys(s, "backup_runs", RUN_FK)
        now = datetime.now(UTC)
        job = Job(org_id=org, type="backup.run", target_type="device", target_id=device,
                  params={}, nonce=uuid4().hex, issued_at=now,
                  expires_at=now + timedelta(hours=1), status=JobStatus.running)
        s.add(job)
        await s.flush()
        run = BackupRun(org_id=org, device_id=device, repository_id=x_repo, job_id=job.id,
                        state="running", actor_type="system")
        s.add(run)
        await s.flush()
        await s.execute(text(
            f"ALTER TABLE backup_runs ADD CONSTRAINT {RUN_FK} FOREIGN KEY "
            "(repository_id, client_id) REFERENCES backup_repositories (id, client_id) NOT VALID"
        ))
        await s.commit()
    try:
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            # A format-2 report, warnings and consistency too: everything the
            # handler writes to the run must reach it in one UPDATE, or the
            # second one re-checks the key this run cannot satisfy.
            await ingest_backup_result(
                s, factory, job,
                BackupRunResult(
                    state="success", manifest_ref="manifests/m", bytes_protected=1,
                    bytes_stored=1, chunk_count=1, warnings=["files changed during read"],
                    consistency={"level": "live", "crash_consistent": False},
                ),
            )
        async with AsyncSession(api_env.admin_engine) as s:
            state, error, warnings = (await s.execute(
                select(BackupRun.state, BackupRun.error, BackupRun.warnings).where(
                    BackupRun.id == run.id)
            )).one()
        points = await _count(api_env, BackupPoint, BackupPoint.backup_run_id == run.id)
        (result,) = await _events(api_env, org, "backup.result")
    finally:
        # Not left for the suite's sweeps to meet.
        async with AsyncSession(api_env.admin_engine) as s:
            await s.execute(text("DELETE FROM backup_runs WHERE id = :r"), {"r": run.id})
            await s.commit()
    assert (state, points, opened) == ("failed", 0, [])
    assert error == "the run's repository belongs to another client"
    assert warnings == ["files changed during read"]
    assert result.client_id == y
    assert result.meta["unexpected_result_state"] == "repository_of_another_client"


LEGACY_WRITERS = ["retention", "verify_point", "restore_verify", "reconcile_stuck",
                  "failed_result"]


async def _legacy_history(
    env: ApiEnv, store: ObjectStore, vault: LocalVaultService, now: datetime
) -> dict[str, Any]:
    """A device of Y with a run and two points in X's repository, committed
    the way history holds them: written before 0242, so they break both
    composite keys (dropped and added back NOT VALID inside the planting
    transaction). The run is still running on a job that expired hours ago;
    each point has a sealed manifest and chunk in the store; the newer one
    has a verify-mode restore in flight; Y's policy still names X's
    repository and keeps one day."""
    org = await env.create_org("bk-history")
    x, _ = await client_with_site(env, org)
    y, y_site = await client_with_site(env, org)
    actor = await _user_id(env, org)
    async with org_session(org, clients=ClientScope.all(), engine=env.app_engine) as s:
        repo = (await create_repository(s, vault, org_id=org, actor_id=actor, client_id=x,
                                        name="x")).id
        key = await open_repo_key(s, vault, org_id=org, repository_id=repo)
    group = await _group(env, org, y)
    await _forced_policy(env, org, repo, group=group, retention_gfs={"daily": 1})
    device = await _device(env, org, y, y_site, group)
    refs: dict[str, str] = {}
    for name in ("old", "new"):
        plaintext = f"legacy-{name}".encode()
        chunk = blake3(plaintext).hexdigest()
        await store.put_backup_chunk(org, repo, chunk, seal_chunk(key, plaintext))
        manifest = BackupManifest(schema_version=1, streams=[FiletreeStream(
            type="filetree", entries=[ManifestEntry(
                path=f"/srv/{name}", kind="file", mode=0o644, size=len(plaintext),
                mtime=1700000000.0, chunk_ids=[chunk])])])
        refs[name] = await store.put_backup_object(org, repo, f"manifests/{uuid4().hex}",
                                                   seal_manifest(key, manifest))
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as s:
        await drop_client_keys(s, "backup_runs", RUN_FK)
        await drop_client_keys(s, "backup_points", POINT_FK)
        job = Job(org_id=org, type="backup.run", target_type="device", target_id=device,
                  params={}, nonce=uuid4().hex, issued_at=now - timedelta(hours=4),
                  expires_at=now - timedelta(hours=2), status=JobStatus.running)
        restore = Job(org_id=org, type="backup.restore", target_type="device", target_id=device,
                      params={}, nonce=uuid4().hex, issued_at=now,
                      expires_at=now + timedelta(hours=1), status=JobStatus.running)
        s.add_all([job, restore])
        await s.flush()
        run = BackupRun(org_id=org, device_id=device, repository_id=repo, job_id=job.id,
                        state="running", actor_type="system", started_at=now - timedelta(hours=4))
        s.add(run)
        await s.flush()
        points = {
            name: BackupPoint(org_id=org, device_id=device, repository_id=repo,
                              backup_run_id=run.id, taken_at=now - age, manifest_ref=refs[name],
                              size=1, state="complete")
            for name, age in (("old", timedelta(days=3)), ("new", timedelta(hours=1)))
        }
        s.add_all(points.values())
        await s.flush()
        s.add(RestoreJob(org_id=org, device_id=device, backup_point_id=points["new"].id,
                         job_id=restore.id, mode="verify", state="running", actor_type="system"))
        for table, key_name in (("backup_runs", RUN_FK), ("backup_points", POINT_FK)):
            await s.execute(text(
                f"ALTER TABLE {table} ADD CONSTRAINT {key_name} FOREIGN KEY "
                "(repository_id, client_id) REFERENCES backup_repositories (id, client_id) "
                "NOT VALID"
            ))
        await s.commit()
        assert (run.client_id, points["new"].client_id) == (y, y)
    return {"org": org, "repo": repo, "run": run.id, "job": job, "restore": restore,
            "old": points["old"].id, "new": points["new"].id}


@pytest.mark.parametrize("writer", LEGACY_WRITERS)
async def test_every_writer_writes_a_row_from_before_0242_once(
    api_env: ApiEnv, pg_cluster: Any, tmp_path: Path, writer: str
) -> None:
    """Review of 2240971, Important 2. A run or point from before 0242 can
    break its composite key, and Postgres re-checks an unchanged key on an
    UPDATE of a row the same transaction already wrote: a writer that
    UPDATEs such a row twice in one transaction fails on it. This drives
    every current writer of `backup_runs` / `backup_points` through one --
    the retention sweep (with the GC after it), `verify_point`, a
    verify-mode `ingest_restore_result`, `reconcile_stuck` and a failure
    result -- and each must land its change. A writer that grows a second
    UPDATE (a flush between two of its attribute writes) turns its case
    red (the mutants in the Task 13 report)."""
    from app import worker

    settings = _worker_settings(pg_cluster, tmp_path)
    vault = LocalVaultService(tmp_path / "vault-root.key")
    now = datetime.now(UTC)
    with mock_aws():
        store = ObjectStore(settings)
        history = await _legacy_history(api_env, store, vault, now)
        org = history["org"]
        try:
            if writer == "retention":
                with override_settings(settings):
                    await worker.backup_retention_gc_all_orgs(now)
            else:
                async with org_session(org, clients=ClientScope.all(),
                                       engine=api_env.app_engine) as s:
                    if writer == "verify_point":
                        assert await verify_point(s, store, vault, org_id=org,
                                                  backup_point_id=history["new"], now=now)
                    elif writer == "restore_verify":
                        await ingest_restore_result(s, history["restore"], BackupRestoreResult(
                            state="success", restored_bytes=1, verified=True, mismatch=[]))
                    elif writer == "reconcile_stuck":
                        assert (await reconcile_stuck(s, org, now=now))["runs"] == 1
                    else:
                        await ingest_backup_result(
                            s, lambda: None, history["job"],  # type: ignore[arg-type, return-value]
                            BackupRunResult(state="failed", bytes_protected=0, bytes_stored=0,
                                            chunk_count=0, error="disk",
                                            warnings=["files changed during read"],
                                            consistency={"level": "live",
                                                         "crash_consistent": False}))
            async with AsyncSession(api_env.admin_engine) as s:
                run_state = (await s.execute(select(BackupRun.state).where(
                    BackupRun.id == history["run"]))).scalar_one()
                points = {
                    name: (await s.execute(select(BackupPoint).where(
                        BackupPoint.id == history[name]))).scalar_one()
                    for name in ("old", "new")
                }
        finally:
            async with AsyncSession(api_env.admin_engine) as s:
                await s.execute(text("DELETE FROM restore_jobs WHERE org_id = :o"), {"o": org})
                await s.execute(text("DELETE FROM backup_points WHERE org_id = :o"), {"o": org})
                await s.execute(text("DELETE FROM backup_runs WHERE org_id = :o"), {"o": org})
                await s.commit()
    landed = {
        "retention": (points["old"].state, points["new"].state) == ("pruned", "complete"),
        "verify_point": points["new"].verified_at is not None,
        "restore_verify": points["new"].restore_tested_at is not None,
        "reconcile_stuck": run_state == "failed",
        "failed_result": run_state == "failed",
    }
    assert landed[writer], (writer, run_state, {n: p.state for n, p in points.items()})


# --------------------------------------------------------------------------- #
# Off-site: a target and the keys of the repository's client, or global
# --------------------------------------------------------------------------- #


@pytest.fixture()
def aws(tmp_path: Path) -> Any:
    with mock_aws():
        s3 = make_buckets()
        yield ObjectStore(local_settings(tmp_path)), s3


async def _offsite_world(env: ApiEnv, tag: str) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    """(org, X, Y, X's repository, actor), with a proven GLOBAL recovery key."""
    org = await env.create_org(tag)
    x, _ = await client_with_site(env, org)
    y, _ = await client_with_site(env, org)
    actor = await _user_id(env, org)
    repo = await _service_repo(env, org, x, actor)
    await add_proven_key(env.app_engine, OffsiteOrg(org, actor, actor, repo, b"", b""))
    return org, x, y, repo, actor


async def _target(env: ApiEnv, org: UUID, actor: UUID, repo: UUID, client: UUID | None) -> UUID:
    target = await add_target(env.app_engine, env.app.state.vault,
                              OffsiteOrg(org, actor, actor, repo, b"", b""), attach=False)
    if client is not None:
        async with AsyncSession(env.admin_engine) as s:
            await s.execute(
                text("UPDATE backup_offsite_targets SET client_id = :c WHERE id = :t"),
                {"c": client, "t": target},
            )
            await s.commit()
    return target


async def test_a_repository_copies_off_site_only_to_a_global_or_its_own_target(
    api_env: ApiEnv, aws: Any
) -> None:
    org, x, y, repo, actor = await _offsite_world(api_env, "bk-os-target")
    y_target = await _target(api_env, org, actor, repo, y)
    x_target = await _target(api_env, org, actor, repo, x)
    global_target = await _target(api_env, org, actor, repo, None)
    admin = await _admin(api_env, org)
    narrowed = await everything_on(api_env, org, x)

    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        with pytest.raises(OffsiteClientError, match="belongs to another client"):
            await set_repository_target(s, org_id=org, repository_id=repo, target_id=y_target,
                                        actor_id=actor)
    url = f"{BK}/repositories/{repo}/offsite"
    before = await _count(api_env, AuditEvent, AuditEvent.org_id == org)
    r = await api_env.client.put(url, json={"target_id": str(y_target)}, headers=admin)
    assert (r.status_code, r.json()) == (
        422, {"detail": "that off-site target belongs to another client"}
    ), r.text
    r = await api_env.client.put(url, json={"target_id": str(y_target)}, headers=narrowed)
    assert (r.status_code, r.json()) == (404, {"detail": "no such off-site target"}), r.text
    assert await _count(api_env, AuditEvent, AuditEvent.org_id == org) == before
    assert await _count(api_env, BackupRepository, BackupRepository.id == repo,
                        BackupRepository.offsite_target_id.is_(None)) == 1

    for target in (x_target, global_target):
        r = await api_env.client.put(url, json={"target_id": str(target)}, headers=narrowed)
        assert (r.status_code, r.json()["offsite_target_id"]) == (200, str(target)), r.text
    r = await api_env.client.put(url, json={"target_id": None}, headers=narrowed)
    assert r.status_code == 200, r.text
    events = await _events(api_env, org, "backup.offsite_enabled", "backup.offsite_disabled")
    assert [(e.action, e.client_id) for e in events] == [
        ("backup.offsite_enabled", x), ("backup.offsite_enabled", x),
        ("backup.offsite_disabled", x),
    ]


async def _client_key(env: ApiEnv, org: UUID, actor: UUID, repo: UUID, client: UUID) -> str:
    """A proven recovery key of `client`, by fingerprint."""
    holder = OffsiteOrg(org, actor, actor, repo, b"", b"")
    async with AsyncSession(env.admin_engine) as s:
        known = set((await s.execute(
            select(BackupRecoveryKey.id).where(BackupRecoveryKey.org_id == org)
        )).scalars())
    await add_proven_key(env.app_engine, holder)
    async with AsyncSession(env.admin_engine) as s:
        new = (await s.execute(
            select(BackupRecoveryKey).where(BackupRecoveryKey.org_id == org,
                                            BackupRecoveryKey.id.not_in(known))
        )).scalar_one()
        found = new.fingerprint
        await s.execute(text("UPDATE backup_recovery_keys SET client_id = :c WHERE id = :k"),
                        {"c": client, "k": new.id})
        await s.commit()
    return found


async def test_a_repository_needs_a_recovery_key_of_its_own_client_or_global(
    api_env: ApiEnv, aws: Any
) -> None:
    """Only Y holds a proven key: X's repository cannot go off-site -- its key
    would be wrapped to nothing X's operators may open. Once X has a key of
    its own, it can."""
    org = await api_env.create_org("bk-os-keys")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    repo = await _service_repo(api_env, org, x, actor)
    await _client_key(api_env, org, actor, repo, y)
    target = await _target(api_env, org, actor, repo, None)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        with pytest.raises(OffsiteError) as refused:
            await set_repository_target(s, org_id=org, repository_id=repo, target_id=target,
                                        actor_id=actor)
    assert str(refused.value) == NO_RECOVERY_KEY_MESSAGE
    await _client_key(api_env, org, actor, repo, x)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        done = await set_repository_target(s, org_id=org, repository_id=repo, target_id=target,
                                           actor_id=actor)
        assert done.offsite_target_id == target


async def test_a_repository_key_is_wrapped_only_to_global_and_its_own_client_s_keys(
    api_env: ApiEnv, aws: Any
) -> None:
    """`prepare_repository` writes one wrapped copy of the repository key per
    recovery key: a key of another client would let that client's operators
    open this client's off-site copy."""
    _store, s3 = aws
    org, x, y, repo_id, actor = await _offsite_world(api_env, "bk-os-wrap")
    async with AsyncSession(api_env.admin_engine) as s:
        (global_fp,) = (await s.execute(
            select(BackupRecoveryKey.fingerprint).where(BackupRecoveryKey.org_id == org)
        )).scalars()
    x_fp = await _client_key(api_env, org, actor, repo_id, x)
    y_fp = await _client_key(api_env, org, actor, repo_id, y)
    target_id = await _target(api_env, org, actor, repo_id, None)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        target = await get_target(s, org, target_id)
        repo = (await s.execute(
            select(BackupRepository).where(BackupRepository.id == repo_id)
        )).scalar_one()
        key = await open_repo_key(s, api_env.app.state.vault, org_id=org, repository_id=repo_id)
        assert target is not None
        await prepare_repository(s, s3, target, repo, key, now=datetime.now(UTC))
    base = f"itms/{repo_id}/"
    listed = s3.list_objects_v2(Bucket=LOCK_BUCKET, Prefix=base + "keys/")
    names = {o["Key"].removeprefix(base + "keys/") for o in listed.get("Contents", [])}
    assert names == {f"repository-key.{fp}.json" for fp in (global_fp, x_fp)}
    catalog = json.loads(
        s3.get_object(Bucket=LOCK_BUCKET, Key=base + "catalog/repository.json")["Body"].read()
    )
    assert catalog["key_fingerprints"] == sorted([global_fp, x_fp])
    assert y_fp not in json.dumps(catalog)


async def test_a_repository_s_gc_is_audited_in_its_client(api_env: ApiEnv, aws: Any) -> None:
    store, _s3 = aws
    org = await api_env.create_org("bk-gc")
    x, _ = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    repo = await _service_repo(api_env, org, x, actor)
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        await gc_repository(s, store, api_env.app.state.vault, org_id=org, repository_id=repo,
                            now=datetime.now(UTC))
    (event,) = await _events(api_env, org, "backup.gc")
    assert (event.target_id, event.client_id) == (str(repo), x)


# --------------------------------------------------------------------------- #
# Every audit this task touches names its client: the exact set
# --------------------------------------------------------------------------- #


async def test_every_repository_and_policy_event_names_its_client(
    api_env: ApiEnv, aws: Any
) -> None:
    """One organisation, X and Y, every repository, policy and off-site act
    of the API: the exact (action, client) set of its audit log. The rows
    that are global (the recovery key, the off-site target and its
    credential) carry none; every repository and policy event carries its
    row's client."""
    org = await api_env.create_org("bk-audit")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    actor = await _user_id(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    y_repo = await _repo(api_env, admin, y, "y")
    y_group = await _group(api_env, org, y)
    for body in ({"client_id": str(x), "repository_id": x_repo},
                 {"device_group_id": str(y_group), "repository_id": y_repo}):
        r = await api_env.client.put(f"{BK}/policies", json=body, headers=admin)
        assert r.status_code == 200, r.text
    await add_proven_key(api_env.app_engine, OffsiteOrg(org, actor, actor, UUID(y_repo), b"",
                                                        b""))
    target = await _target(api_env, org, actor, UUID(y_repo), None)
    for target_id in (str(target), None):
        r = await api_env.client.put(f"{BK}/repositories/{y_repo}/offsite",
                                     json={"target_id": target_id}, headers=admin)
        assert r.status_code == 200, r.text
    events = await _events(api_env, org)
    # A LIST, so an event written twice is a failure too.
    assert sorted(((e.action, e.client_id) for e in events), key=repr) == sorted([
        ("backup.repository_created", x),
        ("backup.repository_created", y),
        ("backup.policy_changed", x),
        ("backup.policy_changed", y),
        ("backup.offsite_enabled", y),
        ("backup.offsite_disabled", y),
        ("backup.recovery_key_registered", None),
        ("backup.recovery_key_proven", None),
        ("credential.created", None),
        ("backup.offsite_target_created", None),
    ], key=repr)


async def test_every_dispatch_event_names_the_device_s_client(api_env: ApiEnv) -> None:
    """The dispatch side, the exact set: a dispatch, a refusal, a missed
    fire and a result, each in the device's client."""
    org = await api_env.create_org("bk-audit-dispatch")
    x, x_site = await client_with_site(api_env, org)
    y, y_site = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    vault = api_env.app.state.vault
    x_repo = await _service_repo(api_env, org, x, actor)
    x_group = await _group(api_env, org, x)
    y_group = await _group(api_env, org, y)
    await _policy(api_env, org, x_repo, group=x_group, window_cron="0 2 * * *",
                  created_at=_LONG_AGO)
    await _forced_policy(api_env, org, x_repo, group=y_group)
    x_device = await _device(api_env, org, x, x_site, x_group, created_at=_LONG_AGO)
    y_device = await _device(api_env, org, y, y_site, y_group, created_at=_LONG_AGO)
    # The seeding's own events (the repositories') are not this test's.
    seeded = {e.id for e in await _events(api_env, org)}

    engine = _FakeJobEngine()
    async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
        await dispatch_due_backup(s, engine, vault, org_id=org, device_id=x_device,
                                  now=_tomorrow(9))
        (pending,) = await dispatch_backup(s, engine, vault, org_id=org, device_id=x_device,
                                           now=datetime.now(UTC))
        await dispatch_backup(s, engine, vault, org_id=org, device_id=y_device,
                              now=datetime.now(UTC))
        job = (await s.execute(select(Job).where(Job.id == pending[0]))).scalar_one()
        await ingest_backup_result(
            s, lambda: None, job,  # type: ignore[arg-type, return-value]
            BackupRunResult(state="failed", bytes_protected=0, bytes_stored=0, chunk_count=0,
                            error="disk"),
        )
    events = [e for e in await _events(api_env, org) if e.id not in seeded]
    assert sorted(((e.action, e.client_id) for e in events), key=repr) == sorted([
            ("backup.missed", x),
            ("backup.dispatched", x),
            ("backup.dispatch_refused", y),
            ("backup.result", x),
        ])


# --------------------------------------------------------------------------- #
# The H children: a chunk follows its repository
# --------------------------------------------------------------------------- #


async def test_a_chunk_is_its_repository_s_client_s(api_env: ApiEnv) -> None:
    org = await api_env.create_org("bk-chunk")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    actor = await _user_id(api_env, org)
    repo = await _service_repo(api_env, org, x, actor)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        chunk = BackupChunk(org_id=org, repository_id=repo, chunk_id="a" * 64)
        s.add(chunk)
        await s.commit()
        assert chunk.client_id == x
    async with org_session(org, clients=ClientScope.only([y]), engine=api_env.app_engine) as s:
        assert (await s.execute(select(BackupChunk.id).where(
            BackupChunk.repository_id == repo))).all() == []
    async with org_session(org, clients=ClientScope.only([x]), engine=api_env.app_engine) as s:
        assert (await s.execute(select(BackupChunk.id).where(
            BackupChunk.repository_id == repo))).scalars().all() == [chunk.id]
