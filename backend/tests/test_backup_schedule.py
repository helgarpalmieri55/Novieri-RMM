"""Due-based backup scheduling (spec-server-backup.md §3.3, defect D1).

The old sweep matched the cron to the minute while running every 15 minutes, so
`0 2 * * *` fired only when a sweep happened to land on 02:00, and a NULL
schedule meant "every sweep". These tests pin the replacement: a scheduled time
that fell since the last sweep is due, once; a run already done or in progress
blocks it; NULL means on demand only; a deadline that passes unmet leaves one
`missed` row.
"""

import datetime as _dt
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.schedule import last_fire
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent, BackupPolicy, BackupRun, Device, Organization, User
from app.modules.backup.schedule import (
    MAX_RUNTIME,
    START_DEADLINE,
    decide,
)

UTC = _dt.UTC


def _t(h: int, m: int = 0, day: int = 19) -> _dt.datetime:
    return _dt.datetime(2026, 7, day, h, m, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# last_fire: pure cron math
# --------------------------------------------------------------------------- #


def test_last_fire_finds_the_exact_minute_when_now_is_later_in_the_window() -> None:
    # The D1 case: a sweep at 02:07 must still see the 02:00 fire.
    assert last_fire("0 2 * * *", _t(2, 7)) == _t(2, 0)


def test_last_fire_at_the_fire_minute_is_that_minute() -> None:
    assert last_fire("0 2 * * *", _t(2, 0)) == _t(2, 0)


def test_last_fire_before_todays_fire_is_yesterdays() -> None:
    assert last_fire("0 2 * * *", _t(1, 59)) == _t(2, 0, day=18)


def test_last_fire_ignores_seconds_in_now() -> None:
    now = _t(2, 0).replace(second=45, microsecond=10)
    assert last_fire("0 2 * * *", now) == _t(2, 0)


def test_last_fire_weekly_looks_back_across_days() -> None:
    # 2026-07-19 is a Sunday; `0 3 * * 1` is Monday 03:00 -> 2026-07-13.
    assert last_fire("0 3 * * 1", _t(12)) == _dt.datetime(2026, 7, 13, 3, 0, tzinfo=UTC)


def test_last_fire_returns_none_beyond_the_lookback() -> None:
    # Only on 1 January: nothing within eight days of mid-July.
    assert last_fire("0 0 1 1 *", _t(12)) is None


def test_last_fire_hour_range_returns_the_latest_minute() -> None:
    assert last_fire("*/15 1-3 * * *", _t(2, 52)) == _t(2, 45)


def test_last_fire_rejects_a_malformed_expression() -> None:
    with pytest.raises(ValueError):
        last_fire("0 2 * *", _t(3))


# --------------------------------------------------------------------------- #
# decide: the due rule
# --------------------------------------------------------------------------- #


def test_a_fire_inside_the_deadline_with_no_run_dispatches() -> None:
    d = decide("0 2 * * *", _t(2, 7), protected_since=_t(0, day=1), last_started_at=None,
               running=False)
    assert d.action == "dispatch"
    assert d.fire_at == _t(2, 0)


def test_a_run_started_since_the_fire_blocks_a_second_dispatch() -> None:
    d = decide("0 2 * * *", _t(2, 22), protected_since=_t(0, day=1),
               last_started_at=_t(2, 7), running=False)
    assert d.action == "none"


def test_a_run_in_progress_blocks_dispatch_even_for_a_new_fire() -> None:
    d = decide("0 2 * * *", _t(2, 7), protected_since=_t(0, day=1),
               last_started_at=_t(2, 0, day=18), running=True)
    assert d.action == "none"


def test_no_schedule_means_on_demand_only() -> None:
    d = decide(None, _t(2, 7), protected_since=_t(0, day=1), last_started_at=None,
               running=False)
    assert d.action == "none"


def test_a_passed_deadline_with_no_run_is_missed() -> None:
    now = _t(2) + START_DEADLINE + _dt.timedelta(minutes=1)
    d = decide("0 2 * * *", now, protected_since=_t(0, day=1), last_started_at=None,
               running=False)
    assert d.action == "missed"
    assert d.fire_at == _t(2, 0)


def test_a_fire_before_protection_began_is_neither_dispatched_nor_missed() -> None:
    d = decide("0 2 * * *", _t(10), protected_since=_t(9), last_started_at=None,
               running=False)
    assert d.action == "none"


def test_the_job_lifetime_covers_the_start_deadline_and_the_run() -> None:
    assert START_DEADLINE == _dt.timedelta(minutes=360)
    assert MAX_RUNTIME == _dt.timedelta(minutes=1440)


# --------------------------------------------------------------------------- #
# dispatch_due_backup against the database
# --------------------------------------------------------------------------- #


class _FakeJobEngine:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, session, **kw):
        from app.models import Job

        now = _dt.datetime.now(UTC)
        job = Job(
            org_id=kw["org_id"],
            type=kw["type"],
            target_type="device",
            target_id=kw["target_device_id"],
            params=kw["params"],
            nonce=uuid4().hex,
            issued_at=now,
            expires_at=now + _dt.timedelta(seconds=kw.get("ttl_s") or 3600),
            requested_by=kw.get("requested_by"),
        )
        session.add(job)
        await session.flush()
        job.signature = "fake-signature"
        self.created.append(kw)
        return job

    async def signed_envelope(self, job):
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": job.signature}


async def _seed(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    vault: LocalVaultService,
    *,
    window_cron: str | None,
    created_at: _dt.datetime,
) -> tuple[UUID, UUID, UUID]:
    from app.modules.backup.repository import create_repository

    slug = f"bksched-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@x.test")
        device = Device(
            org_id=org.id, hostname="srv1", os_family="linux", client_id=org.id, site_id=org.id
        )
        session.add_all([user, device])
        await session.flush()
        device.created_at = created_at
        await session.commit()
        org_id, device_id, user_id = org.id, device.id, user.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        repo = await create_repository(session, vault, org_id=org_id, actor_id=user_id,
                                       client_id=org_id,
                                       name="primary")
        policy = BackupPolicy(
            org_id=org_id, client_id=org_id,
            repository_id=repo.id,
            device_group_id=None,
            sources={"paths": ["/srv"]},
            window_cron=window_cron,
            retention_gfs={"daily": 7},
            enabled=True,
        )
        session.add(policy)
        await session.flush()
        policy.created_at = created_at
        await session.commit()
        repo_id = repo.id
    return org_id, device_id, repo_id


async def test_a_sweep_seven_minutes_after_the_fire_dispatches_once(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    from app.modules.backup.dispatch import dispatch_due_backup

    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, _repo = await _seed(
        admin_engine, app_engine, vault, window_cron="0 2 * * *", created_at=_t(0, day=1)
    )
    engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        first = await dispatch_due_backup(
            session, engine, vault, org_id=org_id, device_id=device_id, now=_t(2, 7)
        )
        second = await dispatch_due_backup(
            session, engine, vault, org_id=org_id, device_id=device_id, now=_t(2, 22)
        )
        await session.commit()

    assert len(first) == 1
    assert second == []
    kw = engine.created[0]
    assert kw["ttl_s"] == int((START_DEADLINE + MAX_RUNTIME).total_seconds())
    assert kw["params"]["not_after_start"] == "2026-07-19T08:07:00Z"


async def test_a_null_schedule_never_dispatches_from_the_sweep(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    from app.modules.backup.dispatch import dispatch_due_backup

    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, _repo = await _seed(
        admin_engine, app_engine, vault, window_cron=None, created_at=_t(0, day=1)
    )
    engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        for minute in (0, 15, 30, 45):
            assert await dispatch_due_backup(
                session, engine, vault, org_id=org_id, device_id=device_id, now=_t(2, minute)
            ) == []
    assert engine.created == []


async def test_a_run_in_progress_blocks_the_next_fire(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    from app.modules.backup.dispatch import dispatch_due_backup

    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, _repo = await _seed(
        admin_engine, app_engine, vault, window_cron="0 * * * *", created_at=_t(0, day=1)
    )
    engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert len(await dispatch_due_backup(
            session, engine, vault, org_id=org_id, device_id=device_id, now=_t(2, 5)
        )) == 1
        # 03:00 fires, but the 02:00 run is still `running`.
        assert await dispatch_due_backup(
            session, engine, vault, org_id=org_id, device_id=device_id, now=_t(3, 5)
        ) == []
        await session.commit()


async def test_a_missed_deadline_writes_one_missed_row_and_an_audit_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    from app.modules.backup.dispatch import dispatch_due_backup

    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, repo_id = await _seed(
        admin_engine, app_engine, vault, window_cron="0 2 * * *", created_at=_t(0, day=1)
    )
    engine = _FakeJobEngine()
    late = _t(9)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        assert await dispatch_due_backup(
            session, engine, vault, org_id=org_id, device_id=device_id, now=late
        ) == []
        assert await dispatch_due_backup(
            session, engine, vault, org_id=org_id, device_id=device_id,
            now=late + _dt.timedelta(minutes=15),
        ) == []
        await session.commit()

    async with AsyncSession(admin_engine) as session:
        runs = (
            await session.execute(select(BackupRun).where(BackupRun.device_id == device_id))
        ).scalars().all()
        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "backup.missed"
                )
            )
        ).scalars().all()
    assert engine.created == []
    assert len(runs) == 1
    run = runs[0]
    assert run.state == "missed"
    assert run.job_id is None
    assert run.repository_id == repo_id
    assert run.started_at == _t(2)
    assert run.error
    assert len(events) == 1


async def test_manual_run_ignores_the_schedule_but_not_a_run_in_progress(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    from app.modules.backup.dispatch import dispatch_backup

    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, _repo = await _seed(
        admin_engine, app_engine, vault, window_cron="0 2 * * *", created_at=_t(0, day=1)
    )
    engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        first = await dispatch_backup(
            session, engine, vault, org_id=org_id, device_id=device_id, now=_t(14, 3)
        )
        second = await dispatch_backup(
            session, engine, vault, org_id=org_id, device_id=device_id, now=_t(14, 4)
        )
        await session.commit()
    assert len(first) == 1
    assert second == []
