"""Tests for signed `patch.apply` dispatch (docs/modules/patch/spec.md). This
is the module's application-security gate: dispatch must be re-checked
against the un-revoked `patch_approval` for the device's ring, never merely
inferred from the `device_patch_status` snapshot.

`dispatch_apply` no longer publishes to Redis (see its docstring) — it
returns the pending `(job_id, device_id, envelope)` triples for the caller to
publish after its own transaction commits, so there's no fake Redis here.
"""

import datetime as _dt
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Device,
    DeviceGroup,
    DevicePatchStatus,
    Organization,
    Patch,
    PatchApproval,
    PatchPolicy,
    PatchRun,
)
from app.modules.patch.apply import dispatch_apply

# ---------------------------------------------------------------------------
# dispatch_apply
#
# The maintenance-window matcher itself (`window_open`) now lives in
# app.core.schedule — see tests/test_schedule.py — shared by patch and
# deploy. This file keeps only the patch-apply dispatch-gate tests.
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
        )
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job):
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": "x"}


async def _setup(
    admin_engine: AsyncEngine, *, maintenance_window: str | None
) -> tuple[UUID, UUID, UUID]:
    """Returns (org_id, device_id, patch_id) with a `test` ring policy carrying
    the given maintenance window, and a Patch row in the org catalog."""
    slug = f"pap-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        group = DeviceGroup(org_id=org.id, name="test-ring-group")
        session.add(group)
        await session.flush()

        session.add(
            PatchPolicy(
                org_id=org.id,
                device_group_id=group.id,
                ring_name="test",
                maintenance_window=maintenance_window,
                reboot="if_required",
                reboot_deferral_hours=24,
            )
        )

        device = Device(
            org_id=org.id, hostname="h1", os_family="windows", device_group_id=group.id
        )
        session.add(device)
        await session.flush()

        patch = Patch(
            org_id=org.id,
            os_family="windows",
            source="windows_update",
            identifier="KB1",
            title="Cum Update",
            severity="critical",
        )
        session.add(patch)
        await session.flush()

        await session.commit()
        return org.id, device.id, patch.id


async def test_approved_status_with_valid_approval_and_open_window_dispatches_one_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, patch_id = await _setup(admin_engine, maintenance_window=None)
    now = _dt.datetime(2026, 7, 19, 3, 0)
    async with org_session(org_id, engine=app_engine) as session:
        session.add(
            DevicePatchStatus(
                org_id=org_id, device_id=device_id, patch_id=patch_id, state="approved"
            )
        )
        session.add(PatchApproval(org_id=org_id, patch_id=patch_id, ring_name="test"))
        await session.flush()

        job_engine = _FakeJobEngine()
        pending = await dispatch_apply(session, job_engine, org_id=org_id, now=now)
        job_ids = [job_id for job_id, _device_id, _envelope in pending]

        assert len(job_ids) == 1
        assert len(job_engine.created) == 1
        assert job_engine.created[0]["type"] == "patch.apply"
        assert job_engine.created[0]["target_device_id"] == device_id
        assert job_engine.created[0]["params"]["patches"] == [
            {"source": "windows_update", "identifier": "KB1"}
        ]

        # dispatch_apply no longer publishes — it returns the pending
        # (job_id, device_id, envelope) triple for the caller to publish
        # after its transaction commits.
        assert len(pending) == 1
        assert pending[0][1] == device_id

        run = (
            await session.execute(
                select(PatchRun).where(PatchRun.device_id == device_id)
            )
        ).scalar_one()
        assert run.job_id == job_ids[0]
        assert run.org_id == org_id

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "patch.apply_dispatched")
            )
        ).scalar_one()
        assert event.target_type == "device"
        assert event.target_id == str(device_id)
        assert event.meta["patch_count"] == 1
        # Default caller (the worker's window-apply sweep) is unattributed system.
        assert event.actor_type == "system"
        assert event.actor_id is None


async def test_missing_status_with_ring_approval_is_dispatched(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Approval-centric behavior: a device rescanned AFTER its ring was
    approved may sit at 'missing' but the ring's approval still gates it in —
    it must NOT be skipped just because the status snapshot lags."""
    org_id, device_id, patch_id = await _setup(admin_engine, maintenance_window=None)
    now = _dt.datetime(2026, 7, 19, 3, 0)
    async with org_session(org_id, engine=app_engine) as session:
        session.add(
            DevicePatchStatus(
                org_id=org_id, device_id=device_id, patch_id=patch_id, state="missing"
            )
        )
        session.add(PatchApproval(org_id=org_id, patch_id=patch_id, ring_name="test"))
        await session.flush()

        job_engine = _FakeJobEngine()
        pending = await dispatch_apply(session, job_engine, org_id=org_id, now=now)

        assert len(pending) == 1


async def test_closed_window_dispatches_nothing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, patch_id = await _setup(
        admin_engine, maintenance_window="0 2 * * *"
    )
    now = _dt.datetime(2026, 7, 19, 10, 0)  # outside the 02:00 window
    async with org_session(org_id, engine=app_engine) as session:
        session.add(
            DevicePatchStatus(
                org_id=org_id, device_id=device_id, patch_id=patch_id, state="approved"
            )
        )
        session.add(PatchApproval(org_id=org_id, patch_id=patch_id, ring_name="test"))
        await session.flush()

        job_engine = _FakeJobEngine()
        pending = await dispatch_apply(session, job_engine, org_id=org_id, now=now)

        assert pending == []
        assert job_engine.created == []
        runs = (await session.execute(select(PatchRun))).scalars().all()
        assert runs == []


async def test_revoked_approval_is_not_dispatched(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Negative security-gate test: an 'approved' status snapshot is NOT
    sufficient on its own — a revoked approval for the ring must block
    dispatch even though the status row still reads 'approved'."""
    org_id, device_id, patch_id = await _setup(admin_engine, maintenance_window=None)
    now = _dt.datetime(2026, 7, 19, 3, 0)
    async with org_session(org_id, engine=app_engine) as session:
        session.add(
            DevicePatchStatus(
                org_id=org_id, device_id=device_id, patch_id=patch_id, state="approved"
            )
        )
        session.add(
            PatchApproval(
                org_id=org_id, patch_id=patch_id, ring_name="test", revoked=True
            )
        )
        await session.flush()

        job_engine = _FakeJobEngine()
        pending = await dispatch_apply(session, job_engine, org_id=org_id, now=now)

        assert pending == []
        assert job_engine.created == []


async def test_installed_status_is_never_dispatched(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, patch_id = await _setup(admin_engine, maintenance_window=None)
    now = _dt.datetime(2026, 7, 19, 3, 0)
    async with org_session(org_id, engine=app_engine) as session:
        session.add(
            DevicePatchStatus(
                org_id=org_id, device_id=device_id, patch_id=patch_id, state="installed"
            )
        )
        session.add(PatchApproval(org_id=org_id, patch_id=patch_id, ring_name="test"))
        await session.flush()

        job_engine = _FakeJobEngine()
        pending = await dispatch_apply(session, job_engine, org_id=org_id, now=now)

        assert pending == []
        assert job_engine.created == []


async def test_multiple_eligible_patches_batched_into_one_job(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A device with two distinct eligible patches must get exactly one
    `patch.apply` job carrying both patches, proving the per-device
    batching described in the module docstring."""
    org_id, device_id, patch_id = await _setup(admin_engine, maintenance_window=None)
    now = _dt.datetime(2026, 7, 19, 3, 0)
    async with org_session(org_id, engine=app_engine) as session:
        patch2 = Patch(
            org_id=org_id,
            os_family="windows",
            source="windows_update",
            identifier="KB2",
            title="Another Update",
            severity="important",
        )
        session.add(patch2)
        await session.flush()

        session.add(
            DevicePatchStatus(
                org_id=org_id, device_id=device_id, patch_id=patch_id, state="approved"
            )
        )
        session.add(
            DevicePatchStatus(
                org_id=org_id, device_id=device_id, patch_id=patch2.id, state="approved"
            )
        )
        session.add(PatchApproval(org_id=org_id, patch_id=patch_id, ring_name="test"))
        session.add(PatchApproval(org_id=org_id, patch_id=patch2.id, ring_name="test"))
        await session.flush()

        job_engine = _FakeJobEngine()
        pending = await dispatch_apply(session, job_engine, org_id=org_id, now=now)

        assert len(pending) == 1
        assert len(job_engine.created) == 1

        patches = job_engine.created[0]["params"]["patches"]
        assert len(patches) == 2
        identifiers = {p["identifier"] for p in patches}
        assert identifiers == {"KB1", "KB2"}
