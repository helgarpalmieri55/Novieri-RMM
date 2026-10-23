"""The rollout reconciler against real Postgres (spec-self-update.md §6.3, §6.4).

Every target state is derived from the `jobs` row; these tests set that row
the way the gateway would and run one tick.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.jobs.engine import JobEngine
from app.models import AuditEvent, Job, JobStatus
from app.modules.agent_update import reconcile
from app.modules.agent_update import service as svc
from app.modules.agent_update.models import (
    AgentUpdateRing,
    AgentUpdateRollout,
    AgentUpdateTarget,
)
from tests.agent_update_support import (
    SHA,
    job_for,
    seed_device,
    seed_group,
    seed_release,
    set_job,
    set_online,
    unique_version,
)
from tests.conftest import ApiEnv

SIGNATURE = "S" * 86 + "=="


class _Presign:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []

    def __call__(self, version: str, os_name: str, arch: str, expires_in: int) -> str:
        self.calls.append((version, os_name, arch, expires_in))
        return (
            f"https://objects.test/agent-releases/{version}/{os_name}-{arch}?X-Amz-Signature=secret"
        )


class Rig:
    def __init__(self, api_env: ApiEnv) -> None:
        self.env = api_env
        self.admin: AsyncEngine = api_env.admin_engine
        self.engine = JobEngine(signer=LocalEd25519Signer(api_env.settings.signing_key_path))
        self.presign = _Presign()
        self.org_id: UUID
        self.user_id: UUID
        self.version = unique_version()

    async def setup(self) -> None:
        self.org_id = await self.env.create_org("recon")
        sub = await self.env.create_user(self.org_id, "org-admin")
        from app.models.core import User

        async with AsyncSession(self.admin) as s:
            self.user_id = (
                await s.execute(select(User.id).where(User.external_subject == sub))
            ).scalar_one()
        await seed_release(self.admin, self.version, signature=SIGNATURE)

    async def start(
        self,
        rings: list[svc.RingSpec] | None = None,
        *,
        batch_size: int = 10,
        threshold: int = 5,
        is_rollback: bool = False,
    ) -> UUID:
        rings = rings or [svc.RingSpec("pilot", None, True)]
        async with org_session(self.org_id, engine=self.env.app_engine) as s:
            rollout = await svc.create_rollout(
                s,
                org_id=self.org_id,
                actor_id=self.user_id,
                target_version=self.version,
                is_rollback=is_rollback,
                reason="supersedes a bad release" if is_rollback else "",
                rings=rings,
                batch_size=batch_size,
                failure_threshold_pct=threshold,
            )
            await svc.start_rollout(
                s, org_id=self.org_id, actor_id=self.user_id, rollout_id=rollout.id
            )
            return rollout.id

    async def tick(self, now: datetime | None = None) -> list[tuple[UUID, dict[str, Any]]]:
        async with org_session(self.org_id, engine=self.env.app_engine) as s:
            return await reconcile.tick(
                s, self.org_id, job_engine=self.engine, presign=self.presign, now=now
            )

    async def targets(self, rollout_id: UUID) -> dict[UUID, AgentUpdateTarget]:
        async with org_session(self.org_id, engine=self.env.app_engine) as s:
            rows = (
                await s.execute(
                    select(AgentUpdateTarget).where(AgentUpdateTarget.rollout_id == rollout_id)
                )
            ).scalars()
            return {t.device_id: t for t in rows}

    async def rollout(self, rollout_id: UUID) -> AgentUpdateRollout:
        async with org_session(self.org_id, engine=self.env.app_engine) as s:
            return await svc.get_rollout(s, rollout_id)


@pytest.fixture()
async def rig(api_env: ApiEnv) -> Rig:
    r = Rig(api_env)
    await r.setup()
    return r


async def test_dispatch_goes_only_to_online_devices_within_the_batch(rig: Rig) -> None:
    online = [await seed_device(rig.admin, rig.org_id) for _ in range(3)]
    offline = await seed_device(rig.admin, rig.org_id, online=False)
    rollout_id = await rig.start(batch_size=2)

    envelopes = await rig.tick()
    assert len(envelopes) == 2
    targets = await rig.targets(rollout_id)
    dispatched = {d for d, t in targets.items() if t.state == "dispatched"}
    assert len(dispatched) == 2 and dispatched <= set(online)
    assert targets[offline].state == "pending"

    # A second tick adds nothing while the batch is full.
    assert await rig.tick() == []

    job = await job_for(rig.admin, targets[next(iter(dispatched))].job_id)  # type: ignore[arg-type]
    assert job.type == "agent.update"
    ttl = reconcile.job_ttl_s(600)
    assert job.expires_at - job.issued_at == timedelta(seconds=ttl)
    p = job.params
    assert (p["artifact_sha256"], p["artifact_size"], p["release_signature"]) == (
        SHA,
        1234,
        SIGNATURE,
    )
    assert (p["target_version"], p["os"], p["arch"], p["rollback"]) == (
        rig.version,
        "linux",
        "amd64",
        False,
    )
    assert rig.presign.calls[0] == (rig.version, "linux", "amd64", ttl + 300)
    assert job.requested_by == rig.user_id


async def test_a_device_with_a_job_already_running_waits(rig: Rig) -> None:
    device = await seed_device(rig.admin, rig.org_id)
    async with org_session(rig.org_id, engine=rig.env.app_engine) as s:
        other = await rig.engine.create(
            s, org_id=rig.org_id, type="hello", target_device_id=device, params={}
        )
    await rig.start()
    assert await rig.tick() == []
    await set_job(rig.admin, other.id, JobStatus.succeeded)
    assert len(await rig.tick()) == 1


async def _dispatched(rig: Rig, **kw: Any) -> tuple[UUID, UUID, UUID]:
    device = await seed_device(rig.admin, rig.org_id, **kw)
    rollout_id = await rig.start()
    await rig.tick()
    job_id = (await rig.targets(rollout_id))[device].job_id
    assert job_id is not None
    return rollout_id, device, job_id


async def test_success_needs_the_post_update_hello(rig: Rig) -> None:
    rollout_id, device, job_id = await _dispatched(rig)
    await set_job(rig.admin, job_id, JobStatus.succeeded, {"outcome": "updated"})
    envelopes = await rig.tick()
    target = (await rig.targets(rollout_id))[device]
    assert target.state == "verifying"
    assert [e["job"]["type"] for _, e in envelopes] == ["hello"]
    assert target.probe_job_id is not None

    await set_job(rig.admin, target.probe_job_id, JobStatus.succeeded, {"greeting": "x"})
    await rig.tick()
    assert (await rig.targets(rollout_id))[device].state == "succeeded"
    # The only ring resolved, so the rollout completed.
    rollout = await rig.rollout(rollout_id)
    assert rollout.state == "completed"


async def test_a_failed_hello_is_a_degraded_failure(rig: Rig) -> None:
    rollout_id, device, job_id = await _dispatched(rig)
    await set_job(rig.admin, job_id, JobStatus.succeeded, {"outcome": "updated"})
    await rig.tick()
    probe = (await rig.targets(rollout_id))[device].probe_job_id
    assert probe is not None
    await set_job(rig.admin, probe, JobStatus.expired, None, attempts=1)
    await rig.tick()
    target = (await rig.targets(rollout_id))[device]
    assert (target.state, target.reason) == ("failed", "degraded")


@pytest.mark.parametrize(
    ("status", "result", "attempts", "state", "reason"),
    [
        (JobStatus.succeeded, {"outcome": "already_current"}, 1, "succeeded", "already_current"),
        (
            JobStatus.failed,
            {"outcome": "rolled_back", "reason": "no_confirmation"},
            1,
            "rolled_back",
            "no_confirmation",
        ),
        (
            JobStatus.failed,
            {"outcome": "aborted", "reason": "selftest_failed"},
            1,
            "failed",
            "selftest_failed",
        ),
        (JobStatus.failed, {"error": "hash_mismatch"}, 1, "failed", "hash_mismatch"),
        (
            JobStatus.refused,
            {"refused": "unknown_type: no capability"},
            1,
            "needs_manual_update",
            "unknown_type",
        ),
        (
            JobStatus.refused,
            {"refused": "downgrade_not_permitted"},
            1,
            "refused",
            "downgrade_not_permitted",
        ),
        (JobStatus.refused, {"refused": "busy"}, 1, "pending", "busy"),
        (JobStatus.expired, None, 0, "pending", "not_delivered"),
        (JobStatus.expired, None, 1, "lost", "expired"),
    ],
)
async def test_job_outcomes_map_to_target_states(
    rig: Rig,
    status: JobStatus,
    result: dict[str, Any] | None,
    attempts: int,
    state: str,
    reason: str,
) -> None:
    rollout_id, device, job_id = await _dispatched(rig)
    # Offline, so a target returned to `pending` is not redispatched this tick.
    await set_online(rig.admin, device, False)
    await set_job(rig.admin, job_id, status, result, attempts=attempts)
    await rig.tick()
    target = (await rig.targets(rollout_id))[device]
    assert (target.state, target.reason) == (state, reason)


async def test_a_running_job_past_its_expiry_is_lost(rig: Rig) -> None:
    rollout_id, device, job_id = await _dispatched(rig)
    await set_job(
        rig.admin,
        job_id,
        JobStatus.running,
        None,
        attempts=1,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await rig.tick()
    assert (await rig.targets(rollout_id))[device].state == "lost"


async def test_busy_backs_off_then_becomes_refused(rig: Rig) -> None:
    rollout_id, device, job_id = await _dispatched(rig)
    now = datetime.now(UTC)
    for attempt in range(3):
        await set_job(rig.admin, job_id, JobStatus.refused, {"refused": "busy"}, attempts=1)
        await rig.tick(now)
        target = (await rig.targets(rollout_id))[device]
        if attempt == 2:
            assert (target.state, target.reason) == ("refused", "busy")
            return
        assert target.state == "pending" and target.next_attempt_at is not None
        # Not before the backoff has passed.
        assert await rig.tick(now + timedelta(minutes=1)) == []
        now = now + reconcile.RETRY_BACKOFF + timedelta(seconds=1)
        assert len(await rig.tick(now)) == 1
        job_id = (await rig.targets(rollout_id))[device].job_id  # type: ignore[assignment]


async def test_the_pilot_pauses_on_its_first_failure(rig: Rig) -> None:
    rollout_id, _device, job_id = await _dispatched(rig)
    await seed_device(rig.admin, rig.org_id)  # not in the materialized rollout
    await set_job(
        rig.admin, job_id, JobStatus.failed, {"outcome": "rolled_back", "reason": "crash_loop"}
    )
    await rig.tick()
    rollout = await rig.rollout(rollout_id)
    assert (rollout.state, rollout.pause_reason) == ("paused", "failure_threshold")
    async with org_session(rig.org_id, engine=rig.env.app_engine) as s:
        paused = (
            await s.execute(
                select(AuditEvent).where(AuditEvent.action == "agent.update.rollout_paused")
            )
        ).scalar_one()
    assert paused.actor_type == "system" and paused.meta["pause_reason"] == "failure_threshold"


async def test_not_delivered_is_not_a_failure(rig: Rig) -> None:
    rollout_id, _device, job_id = await _dispatched(rig)
    await set_job(rig.admin, job_id, JobStatus.expired, None, attempts=0)
    await rig.tick()
    assert (await rig.rollout(rollout_id)).state == "running"


async def _ring_with(
    rig: Rig, states: list[str], *, is_pilot: bool, threshold: int
) -> tuple[AgentUpdateRollout, AgentUpdateRing]:
    """A running rollout whose active ring holds targets in `states`, in
    resolution order (the last is the most recent)."""
    async with org_session(rig.org_id, engine=rig.env.app_engine) as s:
        rollout = AgentUpdateRollout(
            org_id=rig.org_id,
            target_version=rig.version,
            # Not active: several of these share one org, and the threshold
            # reads only the ring.
            state="draft",
            failure_threshold_pct=threshold,
            created_by=rig.user_id,
        )
        s.add(rollout)
        await s.flush()
        ring = AgentUpdateRing(
            org_id=rig.org_id,
            rollout_id=rollout.id,
            ordinal=0,
            name="r",
            is_pilot=is_pilot,
            state="active",
        )
        s.add(ring)
        await s.flush()
        base = datetime.now(UTC)
        for i, state in enumerate(states):
            device = await seed_device(rig.admin, rig.org_id)
            s.add(
                AgentUpdateTarget(
                    org_id=rig.org_id,
                    rollout_id=rollout.id,
                    ring_id=ring.id,
                    device_id=device,
                    state=state,
                    updated_at=base + timedelta(seconds=i),
                )
            )
        await s.flush()
        return rollout, ring


async def _breached(rig: Rig, rollout: AgentUpdateRollout, ring: AgentUpdateRing) -> bool:
    async with org_session(rig.org_id, engine=rig.env.app_engine) as s:
        return await reconcile.threshold_breached(s, rollout, ring)


async def test_threshold_is_floor_of_the_percentage(rig: Rig) -> None:
    # 40 targets at 5%: floor(2.0) = 2 failures tolerated, the third pauses.
    ok = ["failed", "succeeded", "failed"] + ["succeeded"] * 17 + ["pending"] * 20
    rollout, ring = await _ring_with(rig, ok, is_pilot=False, threshold=5)
    assert not await _breached(rig, rollout, ring)
    over = (
        ["failed", "succeeded", "lost", "succeeded", "refused"]
        + ["succeeded"] * 15
        + ["pending"] * 20
    )
    rollout, ring = await _ring_with(rig, over, is_pilot=False, threshold=5)
    assert await _breached(rig, rollout, ring)


async def test_three_failures_in_a_row_pause_under_the_threshold(rig: Rig) -> None:
    states = ["succeeded"] * 10 + ["failed", "rolled_back", "lost"] + ["pending"] * 87
    rollout, ring = await _ring_with(rig, states, is_pilot=False, threshold=50)
    assert await _breached(rig, rollout, ring)
    states = ["succeeded"] * 10 + ["failed", "succeeded", "lost"] + ["pending"] * 87
    rollout, ring = await _ring_with(rig, states, is_pilot=False, threshold=50)
    assert not await _breached(rig, rollout, ring)


async def test_manual_update_devices_do_not_count(rig: Rig) -> None:
    states = ["needs_manual_update"] * 5 + ["skipped"] * 5 + ["succeeded"] * 30
    rollout, ring = await _ring_with(rig, states, is_pilot=True, threshold=5)
    assert not await _breached(rig, rollout, ring)


async def test_a_ring_is_never_advanced_by_the_reconciler(rig: Rig) -> None:
    group = await seed_group(rig.admin, rig.org_id, "pilot")
    pilot_device = await seed_device(rig.admin, rig.org_id, group_id=group)
    await seed_device(rig.admin, rig.org_id)
    async with AsyncSession(rig.admin) as s:
        from app.models.core import AgentRelease

        row = (
            await s.execute(select(AgentRelease).where(AgentRelease.version == rig.version))
        ).scalar_one()
        row.channel = "stable"
        await s.commit()
    rollout_id = await rig.start([svc.RingSpec("pilot", group, True), svc.RingSpec("rest", None)])
    await rig.tick()
    job_id = (await rig.targets(rollout_id))[pilot_device].job_id
    assert job_id is not None
    await set_job(rig.admin, job_id, JobStatus.succeeded, {"outcome": "already_current"})
    await rig.tick()
    await rig.tick()
    async with org_session(rig.org_id, engine=rig.env.app_engine) as s:
        states = [r.state for r in await svc.rings_of(s, rollout_id)]
    assert states == ["done", "pending"]
    assert (await rig.rollout(rollout_id)).state == "running"


async def test_the_api_and_audit_never_show_the_presigned_query(rig: Rig) -> None:
    await seed_device(rig.admin, rig.org_id)
    rollout_id = await rig.start()
    await rig.tick()
    job_id = next(iter((await rig.targets(rollout_id)).values())).job_id
    sub = await rig.env.create_user(rig.org_id, "org-admin")
    resp = await rig.env.client.get(
        f"/api/v1/jobs/{job_id}", headers=await rig.env.headers(rig.org_id, sub)
    )
    assert resp.status_code == 200, resp.text
    assert "X-Amz-Signature" not in resp.text
    assert resp.json()["params"]["artifact_url"].startswith("https://objects.test/agent-releases/")
    async with AsyncSession(rig.admin) as s:
        metas = [
            str(e.meta)
            for e in (
                await s.execute(select(AuditEvent).where(AuditEvent.org_id == rig.org_id))
            ).scalars()
        ]
    assert metas and not any("X-Amz-Signature" in m for m in metas)
    # The signed params the agent receives do carry it.
    job = await job_for(rig.admin, job_id)  # type: ignore[arg-type]
    assert "X-Amz-Signature" in job.params["artifact_url"]


async def test_tick_all_orgs_publishes_after_commit(rig: Rig) -> None:
    await seed_device(rig.admin, rig.org_id)
    await rig.start()
    published: list[UUID] = []

    async def publish(job_id: UUID, envelope: dict[str, Any]) -> None:
        async with AsyncSession(rig.admin) as s:
            # Visible to another connection: the org transaction committed.
            assert (await s.execute(select(Job.id).where(Job.id == job_id))).scalar_one()
        published.append(job_id)

    n = await reconcile.tick_all_orgs(
        engine=rig.env.app_engine,
        admin_engine=rig.admin,
        job_engine=rig.engine,
        presign=rig.presign,
        publish=publish,
    )
    assert n >= 1 and published


async def test_an_offline_device_is_picked_up_when_it_returns(rig: Rig) -> None:
    device = await seed_device(rig.admin, rig.org_id, online=False)
    rollout_id = await rig.start()
    assert await rig.tick() == []
    await set_online(rig.admin, device, True)
    assert len(await rig.tick()) == 1
    assert (await rig.targets(rollout_id))[device].state == "dispatched"
