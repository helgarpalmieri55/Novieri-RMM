"""Restart orchestration after patching (docs/modules/patch/spec.md §8.1).

Records come from apply results and scans; `run_reboot_sweep` dispatches a
signed `patch.reboot` only inside the device's maintenance window and only to
a connected agent; a later boot closes the record. Every step is audited.
"""

import datetime as _dt
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import (
    Agent,
    AuditEvent,
    Device,
    DeviceGroup,
    DevicePatchStatus,
    Job,
    JobStatus,
    Organization,
    Patch,
    PatchApproval,
    PatchPolicy,
    PatchReboot,
    PatchRun,
)
from app.modules.patch.apply import dispatch_apply, ingest_apply_result
from app.modules.patch.handler import register_job_types
from app.modules.patch.ingest import ingest_scan
from app.modules.patch.reboot import ingest_reboot_result, run_reboot_sweep
from app.modules.patch.schema import PatchApplyResult, PatchRebootResult, PatchScanDocument

register_job_types()

# Saturday 2026-09-26, 02:37 UTC: inside a "Saturday 02:00 to 06:00" window,
# and deliberately not on the hour.
SAT = _dt.datetime(2026, 9, 26, 2, 37, tzinfo=_dt.UTC)
WINDOW = "0 2-5 * * 6"


class _FakeJobEngine:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, session, **kw):
        job = Job(
            org_id=kw["org_id"],
            type=kw["type"],
            target_type="device",
            target_id=kw["target_device_id"],
            params=kw["params"],
            nonce=uuid4().hex,
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
        )
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job):
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": "x"}


async def _setup(
    admin_engine: AsyncEngine,
    *,
    reboot: str = "if_required",
    window: str | None = WINDOW,
    online: bool = True,
) -> tuple[UUID, UUID]:
    slug = f"prb-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        group = DeviceGroup(org_id=org.id, name="servers")
        session.add(group)
        await session.flush()
        session.add(
            PatchPolicy(
                org_id=org.id,
                device_group_id=group.id,
                ring_name="broad",
                maintenance_window=window,
                reboot=reboot,
                reboot_deferral_hours=8,
            )
        )
        device = Device(
            org_id=org.id, hostname="srv1", os_family="windows", device_group_id=group.id
        )
        session.add(device)
        await session.flush()
        session.add(
            Agent(
                org_id=org.id,
                device_id=device.id,
                cert_fingerprint=uuid4().hex,
                cert_serial=uuid4().hex[:16],
                cert_not_after=SAT + _dt.timedelta(days=365),
                enrolled_at=SAT - _dt.timedelta(days=30),
                status="online" if online else "offline",
                last_seen_at=SAT,
            )
        )
        await session.commit()
        return org.id, device.id


async def _apply_job(admin_engine: AsyncEngine, org_id: UUID, device_id: UUID) -> Job:
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        job = Job(
            org_id=org_id,
            type="patch.apply",
            target_type="device",
            target_id=device_id,
            params={},
            nonce=uuid4().hex,
            issued_at=SAT,
            expires_at=SAT,
        )
        session.add(job)
        await session.flush()
        session.add(PatchRun(org_id=org_id, job_id=job.id, device_id=device_id))
        await session.commit()
        return job


async def _rows(admin_engine: AsyncEngine, org_id: UUID) -> list[PatchReboot]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(PatchReboot)
                    .where(PatchReboot.org_id == org_id)
                    .order_by(PatchReboot.created_at)
                )
            ).scalars()
        )


async def _actions(admin_engine: AsyncEngine, org_id: UUID) -> list[str]:
    async with AsyncSession(admin_engine) as session:
        return [
            a
            for a in (
                await session.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
            if a.startswith("patch.reboot")
        ]


def _apply_result(**kw: object) -> PatchApplyResult:
    return PatchApplyResult(results=[], applied_at="2026-09-26T02:40:00Z", **kw)  # type: ignore[arg-type]


async def test_a_restart_the_agent_booked_is_recorded_as_scheduled_and_audited(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _setup(admin_engine)
    job = await _apply_job(admin_engine, org_id, device_id)
    async with org_session(org_id, engine=app_engine) as session:
        await ingest_apply_result(
            session,
            job,
            _apply_result(
                reboot_required=True,
                reboot_scheduled_for="2026-09-26T02:45:00Z",
                user_present=False,
            ),
        )
    [row] = await _rows(admin_engine, org_id)
    assert row.state == "scheduled"
    assert row.scheduled_for == _dt.datetime(2026, 9, 26, 2, 45, tzinfo=_dt.UTC)
    assert row.origin == "apply" and row.apply_job_id == job.id
    assert await _actions(admin_engine, org_id) == ["patch.reboot_scheduled"]


async def test_a_restart_nobody_booked_waits_for_the_window(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _setup(admin_engine)
    job = await _apply_job(admin_engine, org_id, device_id)
    async with org_session(org_id, engine=app_engine) as session:
        await ingest_apply_result(session, job, _apply_result(reboot_required=True))
    [row] = await _rows(admin_engine, org_id)
    assert row.state == "pending"
    assert await _actions(admin_engine, org_id) == ["patch.reboot_pending"]


async def test_the_never_policy_records_no_restart(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _setup(admin_engine, reboot="never")
    job = await _apply_job(admin_engine, org_id, device_id)
    async with org_session(org_id, engine=app_engine) as session:
        await ingest_apply_result(session, job, _apply_result(reboot_required=True))
    assert await _rows(admin_engine, org_id) == []


async def test_the_sweep_dispatches_only_inside_the_window_and_only_once(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _setup(admin_engine)
    job = await _apply_job(admin_engine, org_id, device_id)
    async with org_session(org_id, engine=app_engine) as session:
        await ingest_apply_result(session, job, _apply_result(reboot_required=True))

    engine = _FakeJobEngine()
    closed = SAT - _dt.timedelta(days=1)
    async with org_session(org_id, engine=app_engine) as session:
        assert await run_reboot_sweep(session, engine, org_id=org_id, now=closed) == []
    assert engine.created == []

    async with org_session(org_id, engine=app_engine) as session:
        pending = await run_reboot_sweep(session, engine, org_id=org_id, now=SAT)
    assert [device for device, _ in pending] == [device_id]
    [created] = engine.created
    assert created["type"] == "patch.reboot"
    assert created["params"] == {
        "only_if_required": True,
        "deferral_hours": 8,
        "not_after": "2026-09-26T06:00:00Z",
    }
    [row] = await _rows(admin_engine, org_id)
    assert row.state == "dispatched" and row.attempts == 1

    # A second pass while the job is in flight dispatches nothing.
    async with org_session(org_id, engine=app_engine) as session:
        assert await run_reboot_sweep(session, engine, org_id=org_id, now=SAT) == []
    assert len(engine.created) == 1
    assert await _actions(admin_engine, org_id) == [
        "patch.reboot_pending",
        "patch.reboot_dispatched",
    ]


async def test_forced_policy_restarts_even_when_the_os_does_not_ask(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _setup(admin_engine, reboot="forced")
    job = await _apply_job(admin_engine, org_id, device_id)
    async with org_session(org_id, engine=app_engine) as session:
        await ingest_apply_result(session, job, _apply_result(reboot_required=True))
        engine = _FakeJobEngine()
        await run_reboot_sweep(session, engine, org_id=org_id, now=SAT)
    assert engine.created[0]["params"]["only_if_required"] is False


async def test_an_offline_device_is_passed_over(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _setup(admin_engine, online=False)
    job = await _apply_job(admin_engine, org_id, device_id)
    engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine) as session:
        await ingest_apply_result(session, job, _apply_result(reboot_required=True))
        assert await run_reboot_sweep(session, engine, org_id=org_id, now=SAT) == []
    assert engine.created == []


async def test_the_reboot_result_moves_the_record(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _setup(admin_engine)
    apply_job = await _apply_job(admin_engine, org_id, device_id)
    engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine) as session:
        await ingest_apply_result(session, apply_job, _apply_result(reboot_required=True))
        await run_reboot_sweep(session, engine, org_id=org_id, now=SAT)
    [row] = await _rows(admin_engine, org_id)
    async with org_session(org_id, engine=app_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == row.job_id))).scalar_one()
        await ingest_reboot_result(
            session,
            job,
            PatchRebootResult(
                state="scheduled",
                scheduled_for="2026-09-26T02:42:00Z",
                user_present=False,
                workstation=False,
            ),
            SAT,
        )
    [row] = await _rows(admin_engine, org_id)
    assert row.state == "scheduled"
    assert row.scheduled_for == _dt.datetime(2026, 9, 26, 2, 42, tzinfo=_dt.UTC)


async def test_a_refused_restart_fails_and_a_failed_one_is_retried(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    for status, expected in ((JobStatus.failed, "pending"), (JobStatus.refused, "failed")):
        org_id, device_id = await _setup(admin_engine)
        apply_job = await _apply_job(admin_engine, org_id, device_id)
        engine = _FakeJobEngine()
        async with org_session(org_id, engine=app_engine) as session:
            await ingest_apply_result(session, apply_job, _apply_result(reboot_required=True))
            await run_reboot_sweep(session, engine, org_id=org_id, now=SAT)
        [row] = await _rows(admin_engine, org_id)
        async with AsyncSession(admin_engine) as session:
            job = (await session.execute(select(Job).where(Job.id == row.job_id))).scalar_one()
            job.status = status
            await session.commit()
        # Outside the window, so a retried row is not redispatched this pass.
        async with org_session(org_id, engine=app_engine) as session:
            await run_reboot_sweep(
                session, engine, org_id=org_id, now=SAT + _dt.timedelta(hours=6)
            )
        [row] = await _rows(admin_engine, org_id)
        assert row.state == expected, status


async def test_a_later_boot_completes_the_restart_and_a_scan_can_open_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _setup(admin_engine)
    doc = PatchScanDocument(
        os_family="windows", patches=[], scanned_at="2026-09-26T02:00:00Z", reboot_required=True
    )
    async with org_session(org_id, engine=app_engine) as session:
        device = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
        await ingest_scan(session, device, doc)
    [row] = await _rows(admin_engine, org_id)
    assert row.state == "pending" and row.origin == "scan"

    booted = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = PatchScanDocument(
        os_family="windows",
        patches=[],
        scanned_at=booted,
        reboot_required=False,
        booted_at=booted,
    )
    async with org_session(org_id, engine=app_engine) as session:
        device = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one()
        await ingest_scan(session, device, later)
    [row] = await _rows(admin_engine, org_id)
    assert row.state == "completed" and row.closed_at is not None
    assert await _actions(admin_engine, org_id) == [
        "patch.reboot_pending",
        "patch.reboot_completed",
    ]


async def test_a_scheduled_restart_is_confirmed_by_one_scan_after_its_time(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _setup(admin_engine)
    job = await _apply_job(admin_engine, org_id, device_id)
    async with org_session(org_id, engine=app_engine) as session:
        await ingest_apply_result(
            session,
            job,
            _apply_result(reboot_required=True, reboot_scheduled_for="2026-09-26T02:45:00Z"),
        )
    engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine) as session:
        assert await run_reboot_sweep(session, engine, org_id=org_id, now=SAT) == []
    later = SAT + _dt.timedelta(minutes=30)
    for _ in range(2):
        async with org_session(org_id, engine=app_engine) as session:
            await run_reboot_sweep(session, engine, org_id=org_id, now=later)
    assert [c["type"] for c in engine.created] == ["patch.scan"]


async def test_apply_dispatch_opens_mid_hour_and_bounds_the_restart_by_the_window(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id = await _setup(admin_engine)
    async with AsyncSession(admin_engine) as session:
        patch = Patch(
            org_id=org_id,
            os_family="windows",
            source="windows_update",
            identifier="KB9",
            title="CU",
            severity="critical",
        )
        session.add(patch)
        await session.flush()
        session.add(
            DevicePatchStatus(
                org_id=org_id, device_id=device_id, patch_id=patch.id, state="approved"
            )
        )
        session.add(PatchApproval(org_id=org_id, patch_id=patch.id, ring_name="broad"))
        await session.commit()
    engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine) as session:
        pending = await dispatch_apply(session, engine, org_id=org_id, now=SAT)
    assert len(pending) == 1
    params = engine.created[0]["params"]
    assert params["reboot_not_after"] == "2026-09-26T06:00:00Z"
    assert params["result_version"] == 2
