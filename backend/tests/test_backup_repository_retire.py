"""Retiring a backup repository (phase 3 design §5.5, operator decision Q5).

`POST /backup/repositories/{id}/retire` needs `backup.policy.manage` on the
repository's client -- the route's scope is where the person holds it, so
another client's repository is a 404 like a missing one -- and is refused with
a 409 while an enabled policy names it, or once it is retired.

A retired repository takes no new backup:
- the dispatch refuses it and records why, reason `repository_retired`, in
  the `backup.dispatch_refused` shape it already uses -- once per device and
  policy version for the window, every time for a person;
- the dedup exchange grants it no writes;
- no enabled policy may name it (a stored one switched off may keep naming
  it).
Its points stay, and stay restorable: the read exchange grants reads on
`active` and `retired` repositories. There is no way back in this phase.

A retire, a policy write and an off-site link each take the repository row
first and the audit chain last, so any two of them on one repository
serialise on the row and never deadlock.
"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import BackupPolicy, BackupRepository, BackupRun
from app.modules.backup.dedup import answer_chunk_query
from app.modules.backup.dispatch import dispatch_due_backup
from app.modules.backup.read import answer_read_query
from tests.backup_dedup_fixtures import SEED_NOW
from tests.client_scope_support import client_with_site
from tests.conftest import ApiEnv
from tests.test_backup_audit_clients import _chunk_query, _read_query
from tests.test_backup_dispatch import _FakeJobEngine
from tests.test_backup_offsite import aws, vault  # noqa: F401  (fixtures)
from tests.test_backup_read_query import seed_restore
from tests.test_client_record_checks import _holder
from tests.test_client_scope_backup import (
    _LONG_AGO,
    BK,
    _admin,
    _device,
    _events,
    _group,
    _offsite_world,
    _policy,
    _repo,
    _target,
    _tomorrow,
    _written,
)

IN_USE = (
    "an enabled backup policy still uses this repository; switch it off or point it at "
    "another repository first"
)
ALREADY = "this repository is already retired"
RETIRED = "that repository is retired"

#: Some session other than the holder's (`:me`) waits for a
#: `backup_repositories` row the holder locked:
#: `client_scope_support.WAITS_ON_A_CLIENT_ROW` on this table (that comment
#: explains the two shapes the wait takes in `pg_locks`).
WAITS_ON_A_REPOSITORY_ROW = text(
    "SELECT count(*) FROM pg_locks t "
    "WHERE t.locktype = 'tuple' AND t.relation = 'backup_repositories'::regclass "
    "AND t.pid <> :me AND (NOT t.granted OR EXISTS ("
    "  SELECT 1 FROM pg_locks w JOIN pg_locks h ON h.transactionid = w.transactionid "
    "  WHERE w.pid = t.pid AND w.locktype = 'transactionid' AND NOT w.granted "
    "  AND h.pid = :me AND h.locktype = 'transactionid' AND h.granted))"
)

#: Some session other than the holder's (`:me`) holds the audit chain of
#: `:org` in this database: the per-organisation advisory lock
#: `audit_service.record` takes, keyed `hashtext('audit:' || org)` as a
#: bigint, which `pg_locks` shows split into `classid` (the high half) and
#: `objid` (the low half).
HOLDS_THE_CHAIN = text(
    "SELECT count(*) FROM pg_locks "
    "WHERE locktype = 'advisory' AND granted AND pid <> :me AND objsubid = 1 "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND ((classid::bigint << 32) | objid::bigint) = hashtext('audit:' || :org)::bigint"
)


async def _retire(env: ApiEnv, headers: dict[str, str], repository: str | UUID) -> Any:
    return await env.client.post(f"{BK}/repositories/{repository}/retire", headers=headers)


async def _state(env: ApiEnv, repository: str | UUID) -> str:
    async with AsyncSession(env.admin_engine) as s:
        return (
            await s.execute(
                select(BackupRepository.state).where(
                    BackupRepository.id == UUID(str(repository))
                )
            )
        ).scalar_one()


async def _wait_until_blocked_on_a_repository(observer: AsyncSession, holder_pid: int) -> bool:
    for _ in range(400):
        await asyncio.sleep(0.025)
        waiting = (
            await observer.execute(WAITS_ON_A_REPOSITORY_ROW, {"me": holder_pid})
        ).scalar_one()
        await observer.rollback()
        if waiting:
            return True
    return False


async def test_a_repository_is_retired_in_its_client_and_audited_there(
    api_env: ApiEnv,
) -> None:
    """`backup.policy.manage` on X retires X's repository: 200, `retired`,
    and one `backup.repository_retired` in X naming the person. Y's
    repository, where the person only reads, is the 404 a missing one gets
    and stays active. Without the code anywhere, the old 403.
    FALSIFIER: gate the route on `backup.read` (Y's repository is retired)."""
    org = await api_env.create_org("bk-retire")
    x, _ = await client_with_site(api_env, org)
    y, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    y_repo = await _repo(api_env, admin, y, "y")
    tech_id, tech = await _holder(
        api_env, org, {x: ("backup.read", "backup.policy.manage"), y: ("backup.read",)}
    )
    _, reader = await _holder(api_env, org, {None: ("backup.read",)})

    hidden = await _retire(api_env, tech, y_repo)
    missing = await _retire(api_env, tech, uuid4())
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json()) == (
        404, {"detail": "no such repository"}
    )
    assert await _state(api_env, y_repo) == "active"
    assert (await _retire(api_env, reader, x_repo)).status_code == 403

    r = await _retire(api_env, tech, x_repo)
    assert r.status_code == 200, r.text
    assert (r.json()["id"], r.json()["client_id"], r.json()["state"]) == (
        x_repo, str(x), "retired"
    )
    assert await _state(api_env, x_repo) == "retired"
    (event,) = await _events(api_env, org, "backup.repository_retired")
    assert (event.target_type, event.target_id, event.client_id, event.actor_id) == (
        "backup_repository", x_repo, x, tech_id
    )
    assert event.meta["name"] == "x"


async def test_a_repository_an_enabled_policy_uses_cannot_be_retired(api_env: ApiEnv) -> None:
    """A 409 while an enabled policy names it: the repository unchanged and
    nothing written or audited. Switched off -- same repository, as it
    stands -- the policy no longer holds it. A second retire is a 409 too
    and writes no second event.
    FALSIFIER: drop the enabled-policy check in
    `repository.py::retire_repository`."""
    org = await api_env.create_org("bk-retire-used")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    group = await _group(api_env, org, x)
    policy = {"device_group_id": str(group), "repository_id": x_repo, "window_cron": "0 2 * * *"}
    r = await api_env.client.put(f"{BK}/policies", json=policy, headers=admin)
    assert r.status_code == 200, r.text

    before = await _written(api_env, org)
    used = await _retire(api_env, admin, x_repo)
    assert (used.status_code, used.json()) == (409, {"detail": IN_USE}), used.text
    assert await _written(api_env, org) == before
    assert await _state(api_env, x_repo) == "active"

    off = await api_env.client.put(
        f"{BK}/policies", json={**policy, "enabled": False}, headers=admin
    )
    assert off.status_code == 200, off.text
    assert (await _retire(api_env, admin, x_repo)).status_code == 200
    again = await _retire(api_env, admin, x_repo)
    assert (again.status_code, again.json()) == (409, {"detail": ALREADY}), again.text
    assert len(await _events(api_env, org, "backup.repository_retired")) == 1


async def test_no_policy_may_name_a_retired_repository(api_env: ApiEnv) -> None:
    """Plan decision (Task 6): the dispatch would refuse every window of such
    a policy, so the write says so first -- a 409, nothing written. A stored
    policy that names it, switched off before the retire, may still be saved
    switched off as it stands (the one exception `upsert_policy` already
    makes); switching it on is the 409.
    FALSIFIER: drop the state check in `clients.py::policy_repository`."""
    org = await api_env.create_org("bk-retire-policy")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    kept = await _group(api_env, org, x)
    fresh = await _group(api_env, org, x)
    stored = {"device_group_id": str(kept), "repository_id": x_repo, "enabled": False}
    r = await api_env.client.put(f"{BK}/policies", json=stored, headers=admin)
    assert r.status_code == 200, r.text
    assert (await _retire(api_env, admin, x_repo)).status_code == 200

    before = await _written(api_env, org)
    new = await api_env.client.put(
        f"{BK}/policies", json={"device_group_id": str(fresh), "repository_id": x_repo},
        headers=admin,
    )
    assert (new.status_code, new.json()) == (409, {"detail": RETIRED}), new.text
    on = await api_env.client.put(f"{BK}/policies", json={**stored, "enabled": True},
                                  headers=admin)
    assert (on.status_code, on.json()) == (409, {"detail": RETIRED}), on.text
    assert await _written(api_env, org) == before

    kept_off = await api_env.client.put(
        f"{BK}/policies", json={**stored, "window_cron": "0 3 * * *"}, headers=admin
    )
    assert kept_off.status_code == 200, kept_off.text


async def test_a_policy_write_waits_for_a_retire_in_flight(api_env: ApiEnv) -> None:
    """A retire and a policy write are a check and an act each: the retire
    looks for an enabled policy and writes the state; the write looks at the
    state and writes the policy. Unserialised, both pass and an enabled
    policy lands on a retired repository. So the write reads the repository
    FOR SHARE (`policy_repository`) and the retire holds it FOR NO KEY
    UPDATE. Here the holder is a retire in flight -- the state written, not
    committed: the write waits on the row, and once the retire commits it
    answers the 409 a retired repository gets.
    FALSIFIER: drop `.with_for_update(read=True)` from `policy_repository`:
    the write reads the row as it was, never waits, and answers 200."""
    org = await api_env.create_org("bk-retire-lock")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    group = await _group(api_env, org, x)
    body = {"device_group_id": str(group), "repository_id": x_repo}
    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            text("UPDATE backup_repositories SET state = 'retired' WHERE id = :r"),
            {"r": UUID(x_repo)},
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        put = asyncio.create_task(
            api_env.client.put(f"{BK}/policies", json=body, headers=admin)
        )
        if not await _wait_until_blocked_on_a_repository(observer, me):
            put.cancel()
            pytest.fail("the policy write never waited on the repository row")
        await holder.commit()
    r = await put
    assert (r.status_code, r.json()) == (409, {"detail": RETIRED}), r.text


async def test_a_retire_waits_for_a_policy_write_in_flight(api_env: ApiEnv) -> None:
    """The mirror of the test above, the retire's side. Here the holder is a
    policy write in flight -- the repository read FOR SHARE, as
    `policy_repository` reads it, and an enabled policy naming it inserted,
    not committed. The retire waits on the row; once the write commits it
    finds that policy and answers the 409, the repository still active and
    nothing audited.
    FALSIFIER: drop `.with_for_update(key_share=True)` from
    `repository.py::retire_repository`: the retire reads the row unlocked,
    finds no enabled policy (the holder's is not committed), and waits only at
    its UPDATE -- after its check -- so it answers 200 and retires a
    repository an enabled policy names."""
    org = await api_env.create_org("bk-retire-lock-mirror")
    x, _ = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    group = await _group(api_env, org, x)
    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        await holder.execute(
            select(BackupRepository.id)
            .where(BackupRepository.id == UUID(x_repo))
            .with_for_update(read=True)
        )
        holder.add(
            BackupPolicy(org_id=org, repository_id=UUID(x_repo), device_group_id=group,
                         sources={"paths": ["/srv"]}, retention_gfs={}, enabled=True)
        )
        await holder.flush()
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        retire = asyncio.create_task(_retire(api_env, admin, x_repo))
        if not await _wait_until_blocked_on_a_repository(observer, me):
            retire.cancel()
            pytest.fail("the retire never waited on the repository row")
        await holder.commit()
    r = await retire
    assert (r.status_code, r.json()) == (409, {"detail": IN_USE}), r.text
    assert await _state(api_env, x_repo) == "active"
    assert await _events(api_env, org, "backup.repository_retired") == []


async def test_an_off_site_link_waits_for_a_retire_on_the_row_not_the_chain(
    api_env: ApiEnv,
) -> None:
    """The off-site link (`offsite.py::set_repository_target`) and the retire
    each write the repository row and append to the audit chain. The retire
    takes the row first and the chain last, and so must the link: in the
    other order the two form a cycle -- the link holding the chain and
    waiting on the row, the retire holding the row and waiting on the chain
    -- and Postgres kills one of them. Here the holder is a retire in
    flight, its row locked and the state written. The link waits on the
    ROW, holding no chain; the retire then takes the chain, audits and
    commits; the link completes. Both acts land, the retire's event first.
    FALSIFIER: read the repository without `.with_for_update(key_share=True)`
    in `set_repository_target`. Its UPDATE then reaches the database only
    when `audit_service.record` autoflushes, after the chain lock: the link
    waits on the row holding the chain, and the retire's chain lock would be
    the deadlock."""
    org, x, _y, repo, actor = await _offsite_world(api_env, "bk-retire-offsite")
    target = await _target(api_env, org, actor, repo, None)
    admin = await _admin(api_env, org)
    link_url = f"{BK}/repositories/{repo}/offsite"
    async with AsyncSession(api_env.admin_engine) as holder, AsyncSession(
        api_env.admin_engine
    ) as observer:
        # The retire's first lock and its write (`retire_repository`).
        name = (
            await holder.execute(
                select(BackupRepository.name)
                .where(BackupRepository.id == repo)
                .with_for_update(key_share=True)
            )
        ).scalar_one()
        await holder.execute(
            text("UPDATE backup_repositories SET state = 'retired' WHERE id = :r"), {"r": repo}
        )
        me = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        link = asyncio.create_task(
            api_env.client.put(link_url, json={"target_id": str(target)}, headers=admin)
        )
        if not await _wait_until_blocked_on_a_repository(observer, me):
            link.cancel()
            pytest.fail("the off-site link never waited on the repository row")
        chain = (
            await observer.execute(HOLDS_THE_CHAIN, {"me": me, "org": str(org)})
        ).scalar_one()
        await observer.rollback()
        if chain:
            await holder.rollback()
            await link
            pytest.fail("the off-site link took the audit chain before the repository row")
        # The retire's last lock, the chain: no cycle, so it is granted.
        await audit_service.record(
            holder, org_id=org, actor_type="user", actor_id=actor,
            action="backup.repository_retired", target_type="backup_repository",
            target_id=str(repo), metadata={"name": name}, client_id=x,
        )
        await holder.commit()
    r = await link
    assert (r.status_code, r.json()["offsite_target_id"]) == (200, str(target)), r.text
    events = await _events(api_env, org, "backup.repository_retired", "backup.offsite_enabled")
    assert [(e.action, e.client_id) for e in events] == [
        ("backup.repository_retired", x), ("backup.offsite_enabled", x)
    ]
    async with AsyncSession(api_env.admin_engine) as s:
        row = (
            await s.execute(select(BackupRepository).where(BackupRepository.id == repo))
        ).scalar_one()
    assert (row.state, row.offsite_target_id) == ("retired", target)


async def test_the_window_refuses_a_retired_repository_once_per_policy_version(
    api_env: ApiEnv,
) -> None:
    """No job and no key, and the refusal recorded in the device's client,
    reason `repository_retired`, once however many sweeps the fire is due
    for. The policy is planted after the retire (`_policy` writes the row
    directly): the API refuses to write it, and that is the case the
    dispatch is the net for.
    FALSIFIER: drop the state check in `dispatch.py::_dispatch_for_policy`
    (a job is created, with the repository key in its params)."""
    org = await api_env.create_org("bk-retire-window")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    assert (await _retire(api_env, admin, x_repo)).status_code == 200
    group = await _group(api_env, org, x)
    policy = await _policy(api_env, org, UUID(x_repo), group=group, window_cron="0 2 * * *",
                           created_at=_LONG_AGO)
    device = await _device(api_env, org, x, x_site, group, created_at=_LONG_AGO)
    engine = _FakeJobEngine()
    for minute in (5, 20):
        async with org_session(org, clients=ClientScope.all(), engine=api_env.app_engine) as s:
            assert await dispatch_due_backup(
                s, engine, api_env.app.state.vault, org_id=org, device_id=device,
                now=_tomorrow(2, minute),
            ) == []
    assert engine.created == []
    async with AsyncSession(api_env.admin_engine) as s:
        runs = (
            await s.execute(
                select(func.count()).select_from(BackupRun).where(BackupRun.device_id == device)
            )
        ).scalar_one()
    assert runs == 0
    (refusal,) = await _events(api_env, org, "backup.dispatch_refused")
    assert (refusal.actor_type, refusal.target_id, refusal.client_id) == (
        "system", str(device), x
    )
    assert (refusal.meta["reason"], refusal.meta["repository_id"], refusal.meta["policy_id"]) == (
        "repository_retired", x_repo, str(policy)
    )


async def test_run_now_on_a_retired_repository_is_refused_every_time(api_env: ApiEnv) -> None:
    """A person's Run now is their act, recorded each time it is refused
    (invariant 4), and dispatches nothing."""
    org = await api_env.create_org("bk-retire-runnow")
    x, x_site = await client_with_site(api_env, org)
    admin = await _admin(api_env, org)
    x_repo = await _repo(api_env, admin, x, "x")
    assert (await _retire(api_env, admin, x_repo)).status_code == 200
    group = await _group(api_env, org, x)
    await _policy(api_env, org, UUID(x_repo), group=group)
    device = await _device(api_env, org, x, x_site, group)
    for _ in range(2):
        r = await api_env.client.post(f"{BK}/devices/{device}/run", headers=admin)
        assert (r.status_code, r.json()) == (200, {"job_ids": []}), r.text
    refusals = await _events(api_env, org, "backup.dispatch_refused")
    assert [(e.actor_type, e.client_id, e.meta["reason"]) for e in refusals] == [
        ("user", x, "repository_retired")
    ] * 2


async def test_a_retired_repository_takes_no_writes_and_its_points_stay_readable(
    api_env: ApiEnv, aws: Any, vault: LocalVaultService  # noqa: F811
) -> None:
    """The exchanges, on the admin engine like their own suites. A retired
    repository gets no write grant (`dedup.py`, unchanged: only `active`
    writes) and does get the read grants of a restore (`read.py`: `active`
    and `retired`); any other state, none.
    FALSIFIER: `read.py` back to `repo.state != "active"` (the restore is
    refused)."""
    store, _s3 = aws
    base, restore, _ = await seed_restore(
        api_env.admin_engine, api_env.app_engine, vault, "bk-retire-x"
    )

    async def set_state(state: str) -> None:
        async with AsyncSession(api_env.admin_engine) as s:
            await s.execute(
                text("UPDATE backup_repositories SET state = :state WHERE id = :r"),
                {"state": state, "r": base.repository_id},
            )
            await s.commit()

    await set_state("retired")
    async with AsyncSession(api_env.admin_engine) as s:
        write = await answer_chunk_query(s, store, _chunk_query(base.job_id),
                                         org_id=base.org_id, device_id=base.device_id,
                                         now=SEED_NOW)
        read = await answer_read_query(s, store, _read_query(restore.job_id),
                                       org_id=base.org_id, device_id=base.device_id,
                                       now=SEED_NOW)
        await s.commit()
    assert write is None
    assert read is not None
    assert [g.suffix for g in read.grants] == ["chunks/ab/abcd"]

    await set_state("archived")
    async with AsyncSession(api_env.admin_engine) as s:
        assert await answer_read_query(s, store, _read_query(restore.job_id),
                                       org_id=base.org_id, device_id=base.device_id,
                                       now=SEED_NOW) is None
        await s.commit()
    refusals = await _events(api_env, base.org_id, "backup.dedup_refused",
                             "backup.read_refused")
    assert [(e.action, e.meta["reason"]) for e in refusals] == [
        ("backup.dedup_refused", "repository_not_active"),
        ("backup.read_refused", "repository_not_active"),
    ]
