"""The pacer (design §1.4, §1.4a, §1.5; CLAUDE.md 2a(e), 2a(f) as amended
2026-09-30).

One target first, and at most 50 in flight once a sign-in has succeeded, even
under concurrent calls. Three `auth_failed` results stop the run as
`stopped_credential`, and only a person's Resume restarts it, again from one.
The whole ladder is re-run
before every target, so a withdrawn credential refuses the rest one by one
with no decrypt. The pacer never resumes and never adds a target. Every test
seeds the world on the admin engine and drives the pacer through
`org_session`, as the beat and the API do.
"""

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app as app_pkg
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Job
from app.modules.agent_push import preflight, runs
from app.modules.agent_push.errors import PushValidationError
from app.modules.agent_push.models import PushRun, PushRunTarget
from tests.agent_push_fixtures import seed_asset
from tests.ipam_fixtures import seed_agent
from tests.push_run_fixtures import (
    FakeVault,
    RunWorld,
    job_engine,
    make_dispatcher,
    run_world,
    start_run,
)

APP = Path(next(iter(app_pkg.__path__)))


@pytest.fixture(autouse=True)
def _register_push_job_types() -> None:
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


async def _advance(app_engine: AsyncEngine, world: RunWorld, run_id, dispatcher) -> list[Any]:
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        return await runs.advance_run(
            session, world.org.org_id, run_id, dispatcher=dispatcher
        )


async def _targets(admin_engine: AsyncEngine, run_id) -> list[PushRunTarget]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(PushRunTarget)
                    .where(PushRunTarget.run_id == run_id)
                    .order_by(PushRunTarget.position)
                )
            ).scalars()
        )


async def _states(admin_engine: AsyncEngine, run_id) -> list[str]:
    return [t.state for t in await _targets(admin_engine, run_id)]


async def _jobs(admin_engine: AsyncEngine, world: RunWorld) -> int:
    async with AsyncSession(admin_engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Job).where(Job.org_id == world.org.org_id)
                )
            ).scalar_one()
        )


async def _audits(admin_engine: AsyncEngine, world: RunWorld, action: str) -> list[AuditEvent]:
    async with AsyncSession(admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == world.org.org_id, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


async def _finish_job(
    admin_engine: AsyncEngine, job_id, status: str, result: dict | None = None
) -> None:
    assert status in ("succeeded", "failed", "refused", "expired", "cancelled")
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                f"UPDATE jobs SET status = '{status}', result = CAST(:r AS jsonb),"
                " finished_at = now() WHERE id = :j"
            ),
            {"r": json.dumps(result or {}), "j": job_id},
        )
        await session.commit()


async def _finish(
    admin_engine: AsyncEngine, target: PushRunTarget, outcome: str, **extra: Any
) -> None:
    """The site server reports `outcome` for this target's install job."""
    await _finish_job(
        admin_engine, target.job_id, "succeeded",
        {"target_address": str(target.address), "outcome": outcome, **extra},
    )


async def _in_flight(admin_engine: AsyncEngine, run_id) -> list[PushRunTarget]:
    return [t for t in await _targets(admin_engine, run_id) if t.state == "in_flight"]


async def _prove_sign_in(
    app_engine: AsyncEngine, admin_engine: AsyncEngine, world, run_id, dispatcher
) -> None:
    """One first (spec §1.4a): dispatch the first target and have it install,
    which opens the run to full concurrency."""
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    (first,) = await _in_flight(admin_engine, run_id)
    await _finish(admin_engine, first, "installed")


async def _run(admin_engine: AsyncEngine, run_id) -> PushRun:
    async with AsyncSession(admin_engine) as session:
        return (await session.execute(select(PushRun).where(PushRun.id == run_id))).scalar_one()


# --------------------------------------------------------------------------- #
# The ceiling
# --------------------------------------------------------------------------- #


def test_the_bounds_are_the_approved_numbers() -> None:
    from app.modules.agent_push.run_list import PUSH_RUN_MAX

    assert runs.PUSH_RUN_CONCURRENCY == 50
    assert PUSH_RUN_MAX == 1024
    assert preflight.PUSH_BATCH_MAX == 25


async def test_the_pacer_fills_to_fifty_and_no_further(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-50", octet=101, count=56)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _prove_sign_in(app_engine, admin_engine, world, run_id, dispatcher)

    fill = await _advance(app_engine, world, run_id, dispatcher)
    assert len(fill) == 50
    assert all(device == world.site_server_device_id for device, _env in fill)
    states = await _states(admin_engine, run_id)
    assert states.count("in_flight") == 50 and states.count("waiting") == 5
    # Position order: the next fifty confirmed are the fifty in flight.
    assert states[:51] == ["installed"] + ["in_flight"] * 50

    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert await _jobs(admin_engine, world) == 51


async def test_concurrent_advances_never_exceed_the_ceiling(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    """The row lock is the whole mechanism. Without `FOR UPDATE`, both calls
    count zero in flight and dispatch three each."""
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 3)
    world = await run_world(admin_engine, "rp-race", octet=102, count=8)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _prove_sign_in(app_engine, admin_engine, world, run_id, dispatcher)
    # Settle the proving install the way the gateway does (the result handler,
    # in the job's own transaction) BEFORE the race. Otherwise both passes
    # would write that same target row while reconciling, and that incidental
    # row lock -- not the run lock -- would serialize them: this test then
    # passed with `FOR UPDATE` deleted (checked 2026-09-30).
    (proving,) = await _in_flight(admin_engine, run_id)
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        job = (await session.execute(select(Job).where(Job.id == proving.job_id))).scalar_one()
        await runs.settle_target_for_job(session, job)
    assert await _in_flight(admin_engine, run_id) == []

    first, second = await asyncio.gather(
        _advance(app_engine, world, run_id, dispatcher),
        _advance(app_engine, world, run_id, dispatcher),
    )
    assert len(first) + len(second) == 3
    assert await _jobs(admin_engine, world) == 4
    assert (await _states(admin_engine, run_id)).count("in_flight") == 3


async def test_freed_capacity_is_refilled_one_for_one(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 2)
    world = await run_world(admin_engine, "rp-refill", octet=103, count=4)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _prove_sign_in(app_engine, admin_engine, world, run_id, dispatcher)
    # The proven sign-in freed its slot and opened the run: both slots fill.
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 2
    assert await _states(admin_engine, run_id) == ["installed", "in_flight", "in_flight", "waiting"]


# --------------------------------------------------------------------------- #
# The full ladder, before every target, with no decrypt on a refusal
# --------------------------------------------------------------------------- #


async def test_an_unbound_credential_refuses_the_rest_one_by_one_without_a_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 1)
    world = await run_world(admin_engine, "rp-unbind", octet=104, count=3)
    run_id = await start_run(app_engine, world)
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    await _advance(app_engine, world, run_id, dispatcher)
    assert vault.calls == 1

    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE push_credential_scope SET unbound_at = now(), unbound_by = :u"
                 " WHERE credential_id = :c"),
            {"u": world.org.user_id, "c": world.credential_id},
        )
        await session.commit()
    first = (await _targets(admin_engine, run_id))[0]
    await _finish_job(admin_engine, first.job_id, "succeeded",
                      {"target_address": str(first.address), "outcome": "installed"})

    assert await _advance(app_engine, world, run_id, dispatcher) == []
    targets = await _targets(admin_engine, run_id)
    assert [t.state for t in targets] == ["installed", "refused", "refused"]
    assert all("credential_not_scoped" in t.reasons for t in targets[1:])
    assert vault.calls == 1  # the refusals decrypted nothing
    refused = await _audits(admin_engine, world, "push.refused")
    assert [e.target_id for e in refused] == list(world.addresses[1:])
    for event in refused:
        assert event.actor_type == "system" and event.actor_id is None
        assert event.meta["run_id"] == str(run_id)
        assert event.meta["on_behalf_of"] == str(world.org.user_id)


async def test_a_deleted_credential_refuses_every_remaining_target_without_a_decrypt(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-del", octet=105, count=2)
    run_id = await start_run(app_engine, world)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM push_credential_scope WHERE credential_id = :c"),
            {"c": world.credential_id},
        )
        await session.execute(
            text("DELETE FROM credentials WHERE id = :c"), {"c": world.credential_id}
        )
        await session.commit()
    assert (await _run(admin_engine, run_id)).credential_id is None

    vault = FakeVault()
    assert await _advance(app_engine, world, run_id, make_dispatcher(tmp_path, vault=vault)) == []
    targets = await _targets(admin_engine, run_id)
    assert [t.state for t in targets] == ["refused", "refused"]
    assert all("credential_missing" in t.reasons for t in targets)
    assert vault.calls == 0 and await _jobs(admin_engine, world) == 0


async def test_a_person_who_lost_the_permission_no_longer_pushes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-perm", octet=106, count=1)
    run_id = await start_run(app_engine, world)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("DELETE FROM user_roles WHERE user_id = :u"), {"u": world.org.user_id}
        )
        await session.commit()
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    assert target.state == "refused" and "run_owner_not_permitted" in target.reasons


async def test_a_machine_that_enrolled_meanwhile_is_not_pushed_to(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-enr", octet=107, count=1)
    run_id = await start_run(app_engine, world)
    await seed_agent(admin_engine, world.org.org_id, world.device_ids[0])
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    assert target.state == "refused" and target.reasons == ["already_enrolled"]


async def test_an_address_that_now_names_another_machine_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-moved", octet=108, count=1)
    run_id = await start_run(app_engine, world)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text("UPDATE devices SET primary_ip = CAST('10.108.9.9' AS inet) WHERE id = :d"),
            {"d": world.device_ids[0]},
        )
        await session.commit()
    await seed_asset(admin_engine, world.org.org_id, primary_ip=world.addresses[0])
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    assert target.state == "refused" and "target_changed" in target.reasons
    assert await _jobs(admin_engine, world) == 0


# --------------------------------------------------------------------------- #
# The pacer never resumes, never adds a target, and settles outcomes
# --------------------------------------------------------------------------- #


async def test_the_pacer_never_resumes_a_paused_run_but_still_settles_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 1)
    world = await run_world(admin_engine, "rp-pause", octet=109, count=2)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _advance(app_engine, world, run_id, dispatcher)
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        await runs.pause_run(session, world.org.org_id, run_id=run_id, actor_id=world.org.user_id)
    first = (await _targets(admin_engine, run_id))[0]
    await _finish_job(admin_engine, first.job_id, "succeeded",
                      {"target_address": str(first.address), "outcome": "installed"})

    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert (await _run(admin_engine, run_id)).state == "paused"
    assert await _states(admin_engine, run_id) == ["installed", "waiting"]


async def test_a_paused_run_whose_last_target_settles_completes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Codex on #92 (runs.py:1057). Paused after its last target went out, a
    run had nothing waiting and nothing in flight once that target settled,
    yet stayed `paused` for ever -- holding the one-active-run slot until a
    person resumed or cancelled a finished run. Completing it is not
    resuming it: nothing is dispatched, and the pacer never writes
    `running`."""
    world = await run_world(admin_engine, "rp-pause-last", octet=111, count=1)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        await runs.pause_run(session, world.org.org_id, run_id=run_id, actor_id=world.org.user_id)
    (only,) = await _in_flight(admin_engine, run_id)
    await _finish(admin_engine, only, "installed")

    assert await _advance(app_engine, world, run_id, dispatcher) == []
    run = await _run(admin_engine, run_id)
    assert run.state == "completed"
    assert run.finished_at is not None
    assert await _states(admin_engine, run_id) == ["installed"]
    assert len(await _audits(admin_engine, world, "push.run.completed")) == 1


async def test_a_cancelled_run_dispatches_nothing_and_stays_cancelled(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-cxl", octet=110, count=2)
    run_id = await start_run(app_engine, world)
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        await runs.cancel_run(session, world.org.org_id, run_id=run_id, actor_id=world.org.user_id)
    assert await _advance(app_engine, world, run_id, make_dispatcher(tmp_path)) == []
    assert (await _run(admin_engine, run_id)).state == "cancelled"
    assert await _jobs(admin_engine, world) == 0


def test_only_create_run_constructs_a_run_target() -> None:
    """No code path adds a target to an existing run (design §1.3). The
    database trigger refuses one; this names the only constructor."""
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name == "PushRunTarget":
                        offenders.append(f"{path.relative_to(APP)}::{fn.name}")
    assert offenders == ["modules/agent_push/runs.py::create_run"]
    assert not [p for p in APP.rglob("*.py") if "INSERT INTO push_run_target" in p.read_text()]


def _names_push_run(tree: ast.AST) -> bool:
    return any(
        (isinstance(node, ast.Name) and node.id == "PushRun")
        or (isinstance(node, ast.Attribute) and node.attr == "PushRun")
        or (isinstance(node, ast.alias) and (node.asname or node.name) == "PushRun")
        for node in ast.walk(tree)
    )


def test_only_a_persons_act_makes_a_run_running() -> None:
    """CLAUDE.md 2a(e) as amended: the pacer cannot resume. The value
    `"running"` is written to a run's state in `create_run` and `resume_run`
    and nowhere else IN THE WHOLE APP (review item 8), as
    `test_only_the_safety_stop_writes_stopped_credential` scans for
    `stopped_credential`.

    `"running"` is also a state of other modules' rows (backup, deploy), so
    the scan names every way a PUSH RUN could be written, whatever the file:
    * `<anything>.state = "running"`, anywhere: an attribute assignment cannot
      be typed from the AST, so every one is a writer until a person has
      looked (none exists outside `resume_run` today);
    * `setattr(<anything>, "state", "running")`, anywhere;
    * `state="running"` in `PushRun(...)`, or in any call in a file that names
      `PushRun` (an `update(PushRun).values(...)`);
    * SQL text that updates `push_run` and names `'running'`."""
    writers: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        names_run = _names_push_run(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            where = f"{path.relative_to(APP)}::{fn.name}"
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(t, ast.Attribute) and t.attr == "state" for t in node.targets
                    )
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == "running"
                ):
                    writers.add(where)
                if not isinstance(node, ast.Call):
                    continue
                called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if (
                    called == "setattr"
                    and len(node.args) == 3
                    and all(isinstance(a, ast.Constant) for a in node.args[1:])
                    and [getattr(a, "value", None) for a in node.args[1:]] == ["state", "running"]
                ):
                    writers.add(where)
                if (called == "PushRun" or names_run) and any(
                    kw.arg == "state"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "running"
                    for kw in node.keywords
                ):
                    writers.add(where)
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        sql = arg.value.lower()
                        if "update push_run " in sql and "'running'" in sql:
                            writers.add(where)
    assert writers == {
        "modules/agent_push/runs.py::create_run",
        "modules/agent_push/runs.py::resume_run",
    }


async def test_the_install_result_settles_the_target_in_the_jobs_transaction(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-res", octet=111, count=2)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    engine = job_engine(tmp_path)
    for position, outcome in ((0, "installed"), (1, "auth_failed")):
        # One first: each target is dispatched only after the one before settled.
        await _advance(app_engine, world, run_id, dispatcher)
        target = (await _targets(admin_engine, run_id))[position]
        async with org_session(
            world.org.org_id, engine=app_engine, clients=ClientScope.all()
        ) as session:
            job = (await session.execute(select(Job).where(Job.id == target.job_id))).scalar_one()
            await engine.complete(
                session, job, state="succeeded",
                result={"target_address": str(target.address), "outcome": outcome,
                        "detail": "Permission denied" if outcome != "installed" else ""},
            )
    first, second = await _targets(admin_engine, run_id)
    assert (first.state, first.outcome, first.signed_in) == ("installed", "installed", True)
    assert (second.state, second.outcome, second.signed_in) == ("failed", "auth_failed", False)
    assert second.detail == "Permission denied"
    assert first.settled_at is not None and second.settled_at is not None


async def test_a_job_that_expired_is_settled_by_the_next_pass(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 1)
    world = await run_world(admin_engine, "rp-exp", octet=113, count=2)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _advance(app_engine, world, run_id, dispatcher)
    first = (await _targets(admin_engine, run_id))[0]
    await _finish_job(admin_engine, first.job_id, "expired")
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    first, second = await _targets(admin_engine, run_id)
    assert (first.state, first.outcome) == ("failed", "expired")
    assert second.state == "in_flight"


async def test_a_hung_install_is_lost_after_the_grace(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-lost", octet=114, count=1)
    run_id = await start_run(app_engine, world)
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            text(
                "UPDATE jobs SET status = 'running', expires_at = now() - interval '31 minutes'"
                " WHERE id = :j"
            ),
            {"j": target.job_id},
        )
        await session.commit()
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    assert (target.state, target.outcome) == ("failed", "lost")


async def test_a_run_whose_last_target_is_terminal_completes(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-done", octet=115, count=1)
    run_id = await start_run(app_engine, world)
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    (target,) = await _targets(admin_engine, run_id)
    await _finish_job(admin_engine, target.job_id, "succeeded",
                      {"target_address": str(target.address), "outcome": "installed"})
    await _advance(app_engine, world, run_id, make_dispatcher(tmp_path))
    run = await _run(admin_engine, run_id)
    assert run.state == "completed" and run.finished_at is not None
    (completed,) = await _audits(admin_engine, world, "push.run.completed")
    assert completed.actor_type == "system" and completed.meta["counts"]["installed"] == 1
    from app.models import DomainEvent

    async with AsyncSession(admin_engine) as session:
        (event,) = (
            await session.execute(
                select(DomainEvent).where(
                    DomainEvent.org_id == world.org.org_id,
                    DomainEvent.event_type == "push.run.completed",
                )
            )
        ).scalars().all()
    assert event.subject_id == run_id and event.actor_user_id is None
    assert event.payload["installed"] == 1 and event.payload["prefix"] == world.prefix


# --------------------------------------------------------------------------- #
# The lockout guard (spec §1.4a): one first, stop on sign-in failures
# --------------------------------------------------------------------------- #


def test_the_guard_limit_is_the_approved_number() -> None:
    assert runs.PUSH_RUN_AUTH_FAILURE_LIMIT == 3


@pytest.mark.parametrize(
    ("outcome", "result", "signed_in"),
    [
        ("installed", {}, True),
        ("os_mismatch", {"arch_detected": "arm64"}, True),
        # `failed` after sign-in carries what the target said it is ...
        ("failed", {"arch_detected": "amd64", "os_detected": "linux"}, True),
        # ... and before sign-in (WinRM artefact download) it cannot.
        ("failed", {"detail": "artefact download: 404"}, False),
        # Host-key and transport failures happen before any authentication.
        ("unreachable", {}, False),
        ("auth_failed", {}, False),
        ("something_new", {"arch_detected": "amd64"}, False),
    ],
)
def test_what_counts_as_getting_past_sign_in(
    outcome: str, result: dict, signed_in: bool
) -> None:
    assert runs._signed_in(outcome, result) is signed_in


async def test_one_target_first_until_a_sign_in_succeeds(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-one", octet=116, count=5)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)

    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    # Still one in flight: nothing more, though 49 slots are "free".
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    # An unreachable machine proves nothing about the credential: still one.
    (first,) = await _in_flight(admin_engine, run_id)
    await _finish(admin_engine, first, "unreachable")
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    # A sign-in that got through opens the run to its ceiling.
    (second,) = await _in_flight(admin_engine, run_id)
    await _finish(admin_engine, second, "os_mismatch", arch_detected="arm64")
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 3
    assert await _states(admin_engine, run_id) == [
        "failed", "failed", "in_flight", "in_flight", "in_flight",
    ]


async def test_three_sign_in_failures_stop_the_run_and_only_a_person_restarts_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rp-stop", octet=117, count=6)
    run_id = await start_run(app_engine, world)
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    for _ in range(3):
        assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
        (target,) = await _in_flight(admin_engine, run_id)
        await _finish(admin_engine, target, "auth_failed", detail="authentication failed")

    # The third failure stops the run before anything else is presented.
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    run = await _run(admin_engine, run_id)
    assert run.state == "stopped_credential" and run.stopped_at is not None
    assert vault.calls == 3
    assert (await _states(admin_engine, run_id)).count("waiting") == 3
    (stopped,) = await _audits(admin_engine, world, "push.run.stopped")
    assert stopped.actor_type == "system" and stopped.actor_id is None
    assert stopped.meta["on_behalf_of"] == str(world.org.user_id)
    assert stopped.meta["reason"] == "auth_failures"
    assert stopped.meta["auth_failures"] == 3
    from app.models import DomainEvent

    async with AsyncSession(admin_engine) as session:
        (event,) = (
            await session.execute(
                select(DomainEvent).where(
                    DomainEvent.org_id == world.org.org_id,
                    DomainEvent.event_type == "push.run.stopped",
                )
            )
        ).scalars().all()
    assert event.subject_id == run_id and event.actor_user_id is None
    assert event.payload["auth_failures"] == 3 and event.payload["waiting"] == 3

    # The pacer never leaves `stopped_credential` on its own.
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert (await _run(admin_engine, run_id)).state == "stopped_credential"
    assert vault.calls == 3

    # A person's Resume restarts it -- from ONE, with a fresh count.
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        await runs.resume_run(session, world.org.org_id, run_id=run_id, actor_id=world.org.user_id)
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    (target,) = await _in_flight(admin_engine, run_id)
    await _finish(admin_engine, target, "auth_failed")
    # One failure in the new window is not three: the run carries on, one at a time.
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    assert (await _run(admin_engine, run_id)).state == "running"


async def test_installs_in_flight_finish_after_a_stop_and_do_not_undo_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 4)
    world = await run_world(admin_engine, "rp-stop2", octet=118, count=8)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _prove_sign_in(app_engine, admin_engine, world, run_id, dispatcher)
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 4
    in_flight = await _in_flight(admin_engine, run_id)
    for target in in_flight[:3]:
        await _finish(admin_engine, target, "auth_failed")

    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert (await _run(admin_engine, run_id)).state == "stopped_credential"
    assert [t.address for t in await _in_flight(admin_engine, run_id)] == [in_flight[3].address]

    # The last install finishes, is recorded, and a success does not unstop it.
    await _finish(admin_engine, in_flight[3], "installed")
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert await _in_flight(admin_engine, run_id) == []
    assert (await _run(admin_engine, run_id)).state == "stopped_credential"


async def _stop_with_six_in_flight(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch, tag: str,
    octet: int,
) -> tuple[RunWorld, Any, Any, list[PushRunTarget]]:
    """The reviewer's probe, up to the Resume: concurrency 6, a proven
    sign-in, six in flight, three of them `auth_failed`, and the run stopped.
    Returns the six dispatched before the stop."""
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 6)
    world = await run_world(admin_engine, tag, octet=octet, count=11)
    run_id = await start_run(app_engine, world)
    dispatcher = make_dispatcher(tmp_path)
    await _prove_sign_in(app_engine, admin_engine, world, run_id, dispatcher)
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 6
    old = await _in_flight(admin_engine, run_id)
    for target in old[:3]:
        await _finish(admin_engine, target, "auth_failed")
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert (await _run(admin_engine, run_id)).state == "stopped_credential"
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        await runs.resume_run(session, world.org.org_id, run_id=run_id, actor_id=world.org.user_id)
    return world, run_id, dispatcher, old


async def test_an_install_sent_before_a_resume_does_not_reopen_the_run_after_it(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    """Spec §1.4a: "A Resume starts from one again". An install dispatched
    before the stop that reports `installed` after the Resume proved the
    credential in the OLD window, not this one: the next pass sends exactly
    one, not up to the ceiling."""
    world, run_id, dispatcher, old = await _stop_with_six_in_flight(
        admin_engine, app_engine, tmp_path, monkeypatch, "rp-oldproof", 141
    )
    await _finish(admin_engine, old[3], "installed")
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    # Still one: the two old installs in flight neither open nor block it.
    assert await _advance(app_engine, world, run_id, dispatcher) == []


async def test_an_old_sign_in_failure_after_a_resume_does_not_count_again(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    """An `auth_failed` from an install sent before the stop lands after the
    Resume: it belongs to the window the stop already closed, so it neither
    re-stops the run nor counts toward the next three."""
    world, run_id, dispatcher, old = await _stop_with_six_in_flight(
        admin_engine, app_engine, tmp_path, monkeypatch, "rp-oldfail", 142
    )
    # The three installs still in flight at the stop all fail after the Resume.
    for target in old[3:]:
        await _finish(admin_engine, target, "auth_failed")
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    run = await _run(admin_engine, run_id)
    assert run.state == "running" and run.stopped_at is None
    assert len(await _audits(admin_engine, world, "push.run.stopped")) == 1
    # One new failure in this window is one, not four: the run carries on.
    (fresh,) = await _in_flight(admin_engine, run_id)
    await _finish(admin_engine, fresh, "auth_failed")
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    assert (await _run(admin_engine, run_id)).state == "running"


async def test_a_third_failure_settled_mid_pass_never_frees_a_slot_for_a_fourth(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    """Review item 4. The gateway settles results in its own transaction, with
    no run lock. If the third `auth_failed` commits AFTER the pass read the
    failure count but BEFORE it counted what is in flight, a pass that reads
    them separately sees two failures and a free slot, and presents the
    password a fourth time. The guard reads both in ONE statement, so the
    settle is either wholly visible (three failures: stop) or wholly invisible
    (the target still holds its slot)."""
    world = await run_world(admin_engine, "rp-race3", octet=144, count=5)
    run_id = await start_run(app_engine, world)
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    for _ in range(2):
        assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
        (target,) = await _in_flight(admin_engine, run_id)
        await _finish(admin_engine, target, "auth_failed")
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    (third,) = await _in_flight(admin_engine, run_id)
    assert vault.calls == 3

    real = runs.auth_window
    settled: list[bool] = []

    async def settle_after_the_first_guard_read(*args: Any, **kwargs: Any) -> Any:
        read = await real(*args, **kwargs)
        if not settled:
            settled.append(True)
            # The gateway: the job's result and the target's settle, committed.
            await _finish(admin_engine, third, "auth_failed")
            async with org_session(
                world.org.org_id, engine=app_engine, clients=ClientScope.all()
            ) as session:
                job = (
                    await session.execute(select(Job).where(Job.id == third.job_id))
                ).scalar_one()
                await runs.settle_target_for_job(session, job)
        return read

    monkeypatch.setattr(runs, "auth_window", settle_after_the_first_guard_read)
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert settled and vault.calls == 3  # never presented a fourth time
    monkeypatch.setattr(runs, "auth_window", real)
    assert await _advance(app_engine, world, run_id, dispatcher) == []
    assert (await _run(admin_engine, run_id)).state == "stopped_credential"
    assert vault.calls == 3


async def test_a_failure_mid_pass_keeps_the_audit_of_every_secret_already_decrypted(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    """Review item 9. The third dispatch of a pass fails for a reason that is
    not a named refusal (the signer, after the vault checkout). The two
    targets before it were decrypted, signed and audited; rolling the whole
    pass back would erase `push.dispatched` for two secrets that WERE
    decrypted. The pass stops there instead: the two commit and are
    published, the failed target stays waiting for the next pass, and its own
    checkout is audited as `push.run.dispatch_failed`."""
    monkeypatch.setattr(runs, "PUSH_RUN_CONCURRENCY", 3)
    world = await run_world(admin_engine, "rp-midfail", octet=145, count=6)
    run_id = await start_run(app_engine, world)
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    await _prove_sign_in(app_engine, admin_engine, world, run_id, dispatcher)
    assert vault.calls == 1

    engine = dispatcher._job_engine
    real_sign = engine.signed_envelope
    signed: list[int] = []

    async def sign_twice_then_fail(job: Any) -> Any:
        signed.append(1)
        if len(signed) == 3:
            raise RuntimeError("signer unavailable")
        return await real_sign(job)

    monkeypatch.setattr(engine, "signed_envelope", sign_twice_then_fail)
    envelopes = await _advance(app_engine, world, run_id, dispatcher)

    assert len(envelopes) == 2 and vault.calls == 4
    states = await _states(admin_engine, run_id)
    assert states == ["installed", "in_flight", "in_flight", "waiting", "waiting", "waiting"]
    # Every checkout has its audit row: three dispatched (one in the first
    # pass) and the one that failed after its checkout.
    dispatched = await _audits(admin_engine, world, "push.dispatched")
    assert len(dispatched) == 3
    (failed,) = await _audits(admin_engine, world, "push.run.dispatch_failed")
    assert failed.actor_type == "system" and failed.actor_id is None
    assert failed.target_id == world.addresses[3]
    assert failed.meta["run_id"] == str(run_id)
    assert failed.meta["credential_id"] == str(world.credential_id)
    assert failed.meta["credential_checked_out"] is True
    assert failed.meta["error"] == "RuntimeError"
    assert "signer unavailable" not in json.dumps(failed.meta)
    # No job, token or batch row of the failed target outlived its savepoint.
    assert await _jobs(admin_engine, world) == 3

    # The next pass tries it again, and it goes.
    monkeypatch.setattr(engine, "signed_envelope", real_sign)
    assert len(await _advance(app_engine, world, run_id, dispatcher)) == 1
    assert (await _states(admin_engine, run_id))[3] == "in_flight"


def test_only_the_safety_stop_writes_stopped_credential() -> None:
    """CLAUDE.md 2a(e) as amended: the pacer's one act of its own is the
    safety stop. Nothing else anywhere sets `stopped_credential`."""
    writers: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                assigns = (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(t, ast.Attribute) and t.attr == "state" for t in node.targets
                    )
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == "stopped_credential"
                )
                keyword = (
                    isinstance(node, ast.keyword)
                    and node.arg == "state"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == "stopped_credential"
                )
                if assigns or keyword:
                    writers.add(f"{path.relative_to(APP)}::{fn.name}")
    assert writers == {"modules/agent_push/runs.py::_stop_if_locked_out"}


async def test_a_ticked_push_is_still_bounded_by_twenty_five_while_a_run_exists(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    world = await run_world(admin_engine, "rp-25", octet=112, count=1)
    await start_run(app_engine, world)
    with pytest.raises(PushValidationError, match="at most 25"):
        async with org_session(
            world.org.org_id, engine=app_engine, clients=ClientScope.all()
        ) as session:
            await preflight.request_push_batch(
                session,
                world.org.org_id,
                actor_id=world.org.user_id,
                credential_id=world.credential_id,
                targets=[f"10.112.1.{i}" for i in range(26)],
            )
