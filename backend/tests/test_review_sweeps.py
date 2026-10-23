"""A device waiting for review never stalls an automatic dispatcher (plan D4).

Each test puts an approved and a pending device side by side where the sweep
would pick both, and asserts the approved one is still served and the pending
one is passed over -- rather than the whole org's cycle dying on the gate.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import redis.asyncio as aioredis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import Device
from tests.ipam_fixtures import (
    seed_agent,
    seed_device,
    seed_org,
    seed_subnet,
    seed_vrf,
    upsert_observation_row,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class _RefusingEngine:
    """A job engine a test expects never to be asked for anything."""

    default_ttl_s = 900

    async def create(self, session: Any, **kw: Any) -> Any:
        raise AssertionError(f"no job should be created, got {kw['type']}")


async def _pair(
    admin_engine: AsyncEngine, tag: str, *, held: str = "pending", **device_kw: Any
) -> tuple[UUID, UUID, UUID]:
    """An approved device and one in review state `held`, both online."""
    org = await seed_org(admin_engine, tag)
    approved = await seed_device(admin_engine, org.org_id, **device_kw)
    pending = await seed_device(admin_engine, org.org_id, review_state=held, **device_kw)
    for device_id in (approved, pending):
        await seed_agent(admin_engine, org.org_id, device_id, status="online", last_seen_at=NOW)
    return org.org_id, approved, pending


async def test_the_patch_scan_sweep_scans_the_approved_and_passes_over_the_pending(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import worker
    from app.modules.patch.handler import register_job_types

    register_job_types()
    org_id, approved, pending = await _pair(admin_engine, "rs-scan")
    published: list[UUID] = []

    async def fake_publish_job(_redis: Any, device_id: UUID, _envelope: Any) -> None:
        published.append(device_id)

    monkeypatch.setattr(worker, "publish_job", fake_publish_job)
    redis = aioredis.from_url("redis://localhost:6379/9", decode_responses=True)
    try:
        count = await worker.patch_scan_sweep_org(
            org_id,
            JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key")),
            app_engine,
            redis,
            now=NOW,
        )
    finally:
        await redis.aclose()
    assert count == 1 and published == [approved]
    assert pending not in published


async def test_logs_ship_is_not_due_on_a_pending_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from app.modules.security_logs.schedule import due_device_ids

    org_id, approved, pending = await _pair(admin_engine, "rs-logs", os_family="windows")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        due = await due_device_ids(session, org_id, now=NOW, interval_s=3600)
    assert due == [approved]


async def test_the_reboot_sweep_does_not_count_a_pending_device_as_online(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from app.modules.patch.models import PatchReboot
    from app.modules.patch.reboot import run_reboot_sweep

    org_id, _approved, pending = await _pair(admin_engine, "rs-reboot")
    async with AsyncSession(admin_engine) as session:
        session.add(
            PatchReboot(
                org_id=org_id, device_id=pending, origin="operator", state="pending", attempts=0
            )
        )
        await session.commit()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        sent = await run_reboot_sweep(session, _RefusingEngine(), org_id=org_id, now=NOW)
    assert sent == []


async def test_a_pending_device_is_never_chosen_to_sweep_a_subnet(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from app.modules.ipam.sweep import select_sweeper

    org = await seed_org(admin_engine, "rs-ipam")
    vrf_id = await seed_vrf(admin_engine, org.org_id)
    subnet_id = await seed_subnet(admin_engine, org.org_id, vrf_id=vrf_id, prefix="10.44.0.0/24")
    pending = await seed_device(admin_engine, org.org_id, review_state="pending")
    await seed_agent(admin_engine, org.org_id, pending, status="online", last_seen_at=NOW)
    await upsert_observation_row(
        admin_engine,
        org.org_id,
        vrf_id=vrf_id,
        address="10.44.0.7",
        source="inventory",
        method="nic",
        confidence="high",
        device_id=pending,
        now=NOW - timedelta(hours=1),
    )

    class _Subnet:
        id = subnet_id
        prefix = "10.44.0.0/24"
        sweep_device_id: UUID | None = pending  # designated, too

    async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
        chosen = await select_sweeper(session, org.org_id, _Subnet(), now=NOW)  # type: ignore[arg-type]
    assert chosen is None


async def test_a_pending_device_is_not_in_the_self_update_fleet(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from app.modules.agent_update.service import load_fleet

    org_id, approved, pending = await _pair(admin_engine, "rs-upd")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        fleet = await load_fleet(session)
    ids = {d.device_id for d in fleet}
    assert approved in ids and pending not in ids


async def _backup_setup(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, review_state: str
) -> tuple[UUID, UUID, Any]:
    """A real repository and an every-minute backup policy over the device's
    group, so `dispatch_due_backup` WOULD dispatch -- the review state is the
    only thing that can stop it."""
    from app.core.vault import LocalVaultService
    from tests.test_backup_dispatch import _setup

    vault = LocalVaultService(tmp_path / "root.key")
    org_id, device_id, _repo_id, _user_id = await _setup(
        admin_engine, app_engine, vault, window_cron="* * * * *"
    )
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Device).where(Device.id == device_id).values(review_state=review_state)
        )
        await session.commit()
    return org_id, device_id, vault


@pytest.mark.parametrize(
    ("review_state", "expected"), [("approved", 1), ("pending", 0), ("rejected", 0)]
)
async def test_a_scheduled_backup_is_due_only_on_an_approved_device(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    tmp_path: Path,
    review_state: str,
    expected: int,
) -> None:
    from app.modules.backup.dispatch import dispatch_due_backup
    from tests.test_backup_dispatch import _FakeJobEngine

    org_id, device_id, vault = await _backup_setup(admin_engine, app_engine, tmp_path, review_state)
    job_engine = _FakeJobEngine()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        sent = await dispatch_due_backup(
            session,
            job_engine,
            vault,
            org_id=org_id,
            device_id=device_id,
            now=datetime.now(UTC) + timedelta(minutes=2),
        )
    assert len(sent) == expected
    assert len(job_engine.created) == expected


async def test_the_inventory_schedule_passes_over_a_rejected_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Pending machines DO get inventory (spec §1.5); a rejected one is only
    allowed `agent.uninstall`, so selecting it would abort the org's pass."""
    from app.modules.inventory.schedule import due_device_ids

    org_id, approved, rejected = await _pair(admin_engine, "rs-inv", held="rejected")
    pending = await seed_device(admin_engine, org_id, review_state="pending")
    await seed_agent(admin_engine, org_id, pending, status="online", last_seen_at=NOW)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        due = set(await due_device_ids(session, org_id, now=NOW))
    assert approved in due and pending in due
    assert rejected not in due


async def test_the_health_policy_push_passes_over_a_rejected_device(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    from app.modules.health.jobtypes import register_job_types
    from app.modules.health.sweep import run_policy_reconcile
    from tests.health_fixtures import seed_assignment, seed_check, seed_profile

    register_job_types()
    org_id, approved, rejected = await _pair(admin_engine, "rs-hp", held="rejected")
    profile_id = await seed_profile(admin_engine, org_id)
    await seed_check(admin_engine, org_id, profile_id, check_type="cpu.busy_pct")
    for device_id in (approved, rejected):
        await seed_assignment(admin_engine, org_id, profile_id, device_id=device_id)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        pushes = await run_policy_reconcile(session, org_id, job_engine=engine, now=NOW)
    assert len(pushes) == 1


async def test_an_operator_restart_of_a_pending_device_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from app.modules.patch.reboot import RestartUnavailable, request_operator_restart

    org = await seed_org(admin_engine, "rs-rst")
    pending = await seed_device(admin_engine, org.org_id, review_state="pending")
    await seed_agent(
        admin_engine,
        org.org_id,
        pending,
        status="online",
        last_seen_at=NOW,
        capabilities={"patch.reboot": True},
    )
    with pytest.raises(RestartUnavailable, match="waiting for review"):
        async with org_session(org.org_id, engine=app_engine, clients=ClientScope.all()) as session:
            await request_operator_restart(
                session, org_id=org.org_id, device_id=pending, actor_id=org.user_id
            )
