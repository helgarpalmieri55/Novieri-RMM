"""A run's life: confirmed, paused, resumed, cancelled -- each a person's act,
RBAC-checked and audited (design §1.3, §1.5; CLAUDE.md 2a(e) as amended).
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, EnrollmentToken, Job, User
from app.modules.agent_push import run_list, runs
from app.modules.agent_push.errors import (
    PushConflictError,
    PushListChangedError,
    PushNotFoundError,
    PushPermissionError,
)
from app.modules.agent_push.models import PushRun, PushRunTarget
from tests.push_run_fixtures import CONSOLE, RunWorld, run_world, start_run


async def _audits(admin_engine: AsyncEngine, org_id, action: str) -> list[AuditEvent]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == action
                    )
                )
            ).scalars()
        )


async def _count(admin_engine: AsyncEngine, model, org_id) -> int:
    async with AsyncSession(admin_engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(model.org_id == org_id)
                )
            ).scalar_one()
        )


async def _create(app_engine: AsyncEngine, world: RunWorld, digest: str):
    async with org_session(world.org.org_id, engine=app_engine) as session:
        return await runs.create_run(
            session,
            world.org.org_id,
            actor_id=world.org.user_id,
            prefix=world.prefix,
            credential_id=world.credential_id,
            list_digest=digest,
            public_console_url=CONSOLE,
        )


async def _control(app_engine: AsyncEngine, world: RunWorld, verb: str, run_id, actor=None):
    fn = {"pause": runs.pause_run, "resume": runs.resume_run, "cancel": runs.cancel_run}[verb]
    async with org_session(world.org.org_id, engine=app_engine) as session:
        return await fn(
            session, world.org.org_id, run_id=run_id, actor_id=actor or world.org.user_id
        )


async def test_a_confirmed_list_becomes_a_sealed_run_and_nothing_is_dispatched(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-new", octet=91, count=2)
    run_id = await start_run(app_engine, world)

    async with AsyncSession(admin_engine) as session:
        run = (await session.execute(select(PushRun).where(PushRun.id == run_id))).scalar_one()
        targets = list(
            (
                await session.execute(
                    select(PushRunTarget)
                    .where(PushRunTarget.run_id == run_id)
                    .order_by(PushRunTarget.position)
                )
            ).scalars()
        )
    assert run.state == "running" and run.sealed_at is not None
    assert run.target_count == 2 and str(run.prefix) == world.prefix
    assert [str(t.address) for t in targets] == list(world.addresses)
    assert [t.position for t in targets] == [0, 1]
    assert {t.state for t in targets} == {"waiting"}
    assert [t.device_id for t in targets] == list(world.device_ids)

    (started,) = await _audits(admin_engine, world.org.org_id, "push.run.started")
    assert started.actor_type == "user" and started.actor_id == world.org.user_id
    assert started.meta["target_count"] == 2
    assert started.meta["list_digest"] == run.list_digest
    # Creating a run signs nothing and mints nothing: the pacer does that.
    assert await _count(admin_engine, Job, world.org.org_id) == 0
    assert await _count(admin_engine, EnrollmentToken, world.org.org_id) == 0


async def test_a_changed_list_is_refused_before_anything_is_written(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-chg", octet=92, count=2)
    async with org_session(world.org.org_id, engine=app_engine) as session:
        shown = await run_list.expand_prefix(
            session,
            world.org.org_id,
            actor_id=world.org.user_id,
            prefix=world.prefix,
            credential_id=world.credential_id,
            public_console_url=CONSOLE,
        )
    # Between showing and confirming, one machine stops being ready.
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM push_host_key WHERE org_id = :o AND address = CAST(:a AS inet)"),
            {"o": world.org.org_id, "a": world.addresses[1]},
        )
        await session.commit()

    with pytest.raises(PushListChangedError):
        await _create(app_engine, world, shown.list_digest)
    with pytest.raises(PushListChangedError):
        await _create(app_engine, world, "0" * 64)
    assert await _count(admin_engine, PushRun, world.org.org_id) == 0
    assert await _count(admin_engine, AuditEvent, world.org.org_id) == 0


async def test_one_active_run_per_organization(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-one", octet=93, count=1)
    first = await start_run(app_engine, world)
    with pytest.raises(PushConflictError, match="one active run"):
        await start_run(app_engine, world)
    await _control(app_engine, world, "pause", first)
    with pytest.raises(PushConflictError, match="one active run"):
        await start_run(app_engine, world)
    await _control(app_engine, world, "cancel", first)
    await start_run(app_engine, world)


async def test_pause_resume_and_cancel_are_audited_and_cancel_is_final(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-ctl", octet=94, count=2)
    run_id = await start_run(app_engine, world)

    assert (await _control(app_engine, world, "pause", run_id)).state == "paused"
    assert (await _control(app_engine, world, "resume", run_id)).state == "running"
    cancelled = await _control(app_engine, world, "cancel", run_id)
    assert cancelled.state == "cancelled" and cancelled.finished_at is not None

    for action in ("push.run.paused", "push.run.resumed", "push.run.cancelled"):
        (event,) = await _audits(admin_engine, world.org.org_id, action)
        assert event.actor_type == "user" and event.actor_id == world.org.user_id
        assert event.target_id == str(run_id)
    (cancel_event,) = await _audits(admin_engine, world.org.org_id, "push.run.cancelled")
    assert cancel_event.meta["cancelled_waiting"] == 2

    async with AsyncSession(admin_engine) as session:
        states = set(
            (
                await session.execute(
                    select(PushRunTarget.state).where(PushRunTarget.run_id == run_id)
                )
            ).scalars()
        )
    assert states == {"cancelled"}
    for verb in ("resume", "pause", "cancel"):
        with pytest.raises(PushConflictError):
            await _control(app_engine, world, verb, run_id)


async def test_resume_from_a_safety_stop_is_a_persons_act_and_restarts_the_window(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Spec §1.4a: a `stopped_credential` run is still active, cannot be
    paused, and only a person's Resume restarts it -- with a fresh window, so the
    pacer starts again from one target."""
    world = await run_world(admin_engine, "rc-stop", octet=99, count=1)
    run_id = await start_run(app_engine, world)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "UPDATE push_run SET state = 'stopped_credential', stopped_at = now(),"
                " auth_window_started_at = now() - interval '1 hour' WHERE id = :r"
            ),
            {"r": run_id},
        )
        await session.commit()
    with pytest.raises(PushConflictError, match="one active run"):
        await start_run(app_engine, world)
    with pytest.raises(PushConflictError):
        await _control(app_engine, world, "pause", run_id)

    before = datetime.now(UTC)
    resumed = await _control(app_engine, world, "resume", run_id)
    assert resumed.state == "running" and resumed.stopped_at is None
    assert resumed.auth_window_started_at >= before
    (event,) = await _audits(admin_engine, world.org.org_id, "push.run.resumed")
    assert event.actor_id == world.org.user_id
    assert event.meta["from_state"] == "stopped_credential"


async def test_resume_requires_credentials_checkout(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-res", octet=95, count=1)
    run_id = await start_run(app_engine, world)
    await _control(app_engine, world, "pause", run_id)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        stranger = User(
            org_id=world.org.org_id,
            external_subject=f"sub-{uuid4().hex[:8]}",
            email=f"{uuid4().hex[:8]}@test.local",
        )
        session.add(stranger)
        await session.commit()
    with pytest.raises(PushPermissionError, match="credentials.checkout"):
        await _control(app_engine, world, "resume", run_id, actor=stranger.id)


async def test_another_orgs_run_is_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    owner = await run_world(admin_engine, "rc-own", octet=96, count=1)
    other = await run_world(admin_engine, "rc-oth", octet=97, count=1)
    run_id = await start_run(app_engine, owner)
    with pytest.raises(PushNotFoundError):
        await _control(app_engine, other, "pause", run_id)
    with pytest.raises(PushNotFoundError):
        async with org_session(other.org.org_id, engine=app_engine) as session:
            await runs.get_run(session, other.org.org_id, run_id)


async def test_counts_and_pages_are_read_in_position_order(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rc-page", octet=98, count=3)
    run_id = await start_run(app_engine, world)
    async with org_session(world.org.org_id, engine=app_engine) as session:
        counts = await runs.run_counts(session, world.org.org_id, run_id)
        total, page = await runs.list_targets(session, world.org.org_id, run_id, offset=1, limit=1)
    assert counts == {
        "waiting": 3, "in_flight": 0, "installed": 0, "failed": 0, "refused": 0, "cancelled": 0,
    }
    assert total == 3 and [t.position for t in page] == [1]
