"""Tests for the signed `script.run` service (docs/modules/app-deploy/spec.md
Task 9): author/edit versioning, dispatch (body-in-signed-envelope), and
result ingestion. Mirrors `tests/test_deploy_dispatch.py` /
`tests/test_deploy_result.py`'s fixture style: a fake job engine stands in
for the kernel job engine so these tests exercise scripts.py's
target-resolution and audit logic in isolation. `run_script` no longer
publishes to Redis (see its docstring) — it returns the pending
`(job_id, device_id, envelope)` triples for the caller to publish after its
own transaction commits, so there's no fake Redis here.

`test_valid_script_result_through_job_engine_reconciles_and_queues_ui_event`
instead drives the real kernel path (`JobEngine.complete` ->
`app.modules.deploy.handler.handle_script_result` -> `ingest_script_result`),
so the handler's `ScriptRunResult(**result)` parse and `queue_ui_event` call
are actually exercised end-to-end — mirrors
`tests/test_inventory_ingest.py`'s `test_valid_result_through_job_engine_
ingests` and `tests/test_deploy_result.py`'s deploy-side counterpart.
"""

import datetime as _dt
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.core.tenancy import org_session
from app.core.wire import InstallOutcome
from app.jobs.engine import JobEngine
from app.models import AuditEvent, Device, DeviceGroup, Job, JobStatus, Organization, User
from app.modules.deploy.handler import register_job_types
from app.modules.deploy.models import Script, ScriptRun
from app.modules.deploy.packages import DeployValidationError
from app.modules.deploy.schema import ScriptRunResult
from app.modules.deploy.scripts import (
    author_script,
    edit_script,
    ingest_script_result,
    run_script,
)

# The job-type registry is populated at app-startup time (app.modules.deploy.
# register(app)), not merely on import. Register directly so this file's own
# kernel round-trip test doesn't depend on import order, mirroring
# test_deploy_worker.py's module-level call.
register_job_types()

# ---------------------------------------------------------------------------
# fakes (mirrors test_deploy_dispatch.py)
# ---------------------------------------------------------------------------


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
            nonce="n",
            issued_at=_dt.datetime.now(_dt.UTC),
            expires_at=_dt.datetime.now(_dt.UTC),
            requested_by=kw.get("requested_by"),
        )
        session.add(job)
        await session.flush()
        # Mirror the real JobEngine.create: sign at create time.
        job.signature = "fake-signature"
        session.add(job)
        await session.flush()
        self.created.append(kw)
        return job

    async def signed_envelope(self, job):
        return {"v": 1, "kind": "job", "job": {"id": str(job.id)}, "sig": job.signature}


async def _setup(admin_engine: AsyncEngine) -> tuple[UUID, UUID, UUID, UUID]:
    """Returns (org_id, device_id, group_id, user_id)."""
    slug = f"scr-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()

        user = User(
            org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com"
        )
        session.add(user)
        await session.flush()

        group = DeviceGroup(org_id=org.id, name="script-group", client_id=org.id)
        session.add(group)
        await session.flush()

        device = Device(
    org_id=org.id,
    hostname="d1",
    os_family="windows",
    device_group_id=group.id,
    client_id=org.id,
    site_id=org.id,
        )
        session.add(device)
        await session.flush()

        await session.commit()
        return org.id, device.id, group.id, user.id


# ---------------------------------------------------------------------------
# author_script / edit_script
# ---------------------------------------------------------------------------


async def test_author_script_creates_version_1_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _device_id, _group_id, user_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        script = await author_script(
            session,
            org_id=org_id,
            actor_id=user_id,
            name="disk-cleanup",
            os_family="windows",
            interpreter="powershell",
            body="Remove-Item -Recurse C:\\Temp\\*",
        )

        assert script.version == 1
        assert script.body == "Remove-Item -Recurse C:\\Temp\\*"
        assert script.created_by == user_id

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "script.authored")
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == user_id


async def test_edit_script_bumps_version_and_updates_body(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _device_id, _group_id, user_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        script = await author_script(
            session,
            org_id=org_id,
            actor_id=user_id,
            name="disk-cleanup",
            os_family="windows",
            interpreter="powershell",
            body="v1 body",
        )

        updated = await edit_script(
            session, org_id=org_id, actor_id=user_id, script_id=script.id, body="v2 body"
        )

        assert updated.id == script.id
        assert updated.version == 2
        assert updated.body == "v2 body"

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "script.edited")
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == user_id
        assert event.meta["version"] == 2


# ---------------------------------------------------------------------------
# run_script
# ---------------------------------------------------------------------------


async def test_run_script_dispatches_signed_job_with_body_in_envelope(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, _group_id, user_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        script = await author_script(
            session,
            org_id=org_id,
            actor_id=user_id,
            name="disk-cleanup",
            os_family="windows",
            interpreter="powershell",
            body="v1 body",
        )
        script = await edit_script(
            session, org_id=org_id, actor_id=user_id, script_id=script.id, body="v2 body"
        )
        assert script.version == 2

        job_engine = _FakeJobEngine()
        pending = await run_script(
            session,
            job_engine,
            org_id=org_id,
            script_id=script.id,
            target_type="device",
            target_id=device_id,
            actor_id=user_id,
            actor_type="user",
        )
        job_ids = [job_id for job_id, _device_id, _envelope in pending]

        assert len(job_ids) == 1
        assert len(job_engine.created) == 1
        created = job_engine.created[0]
        assert created["type"] == "script.run"
        assert created["target_device_id"] == device_id
        # The body-in-signed-envelope guarantee: the exact body and the
        # version snapshot at dispatch time must be inside the signed params.
        assert created["params"]["body"] == "v2 body"
        assert created["params"]["script_version"] == 2
        assert created["params"]["interpreter"] == "powershell"
        assert created["params"]["script_id"] == str(script.id)

        # run_script no longer publishes — it returns the pending
        # (job_id, device_id, envelope) triple for the caller to publish
        # after its transaction commits.
        assert len(pending) == 1
        assert pending[0][1] == device_id

        signed_job = (
            await session.execute(select(Job).where(Job.id == job_ids[0]))
        ).scalar_one()
        assert signed_job.signature is not None
        assert signed_job.params["body"] == "v2 body"

        run = (
            await session.execute(
                select(ScriptRun).where(ScriptRun.job_id == job_ids[0])
            )
        ).scalar_one()
        assert run.state == "running"
        assert run.script_version == 2
        assert run.device_id == device_id
        assert run.requested_by == user_id

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "script.dispatched")
            )
        ).scalar_one()
        assert event.actor_type == "user"
        assert event.actor_id == user_id
        assert event.meta["script_version"] == 2
        assert event.meta["dispatched_count"] == 1


async def test_run_script_group_target_resolves_devices(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, group_id, user_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        script = await author_script(
            session,
            org_id=org_id,
            actor_id=user_id,
            name="ping",
            os_family="linux",
            interpreter="bash",
            body="echo hi",
        )
        job_engine = _FakeJobEngine()
        pending = await run_script(
            session,
            job_engine,
            org_id=org_id,
            script_id=script.id,
            target_type="group",
            target_id=group_id,
            actor_id=user_id,
        )
        assert len(pending) == 1
        assert job_engine.created[0]["target_device_id"] == device_id


async def test_author_script_rejects_unknown_os_family_and_interpreter(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _device_id, _group_id, user_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(DeployValidationError):
            await author_script(
                session,
                org_id=org_id,
                actor_id=user_id,
                name="bad",
                os_family="amiga",
                interpreter="powershell",
                body="x",
            )
        with pytest.raises(DeployValidationError):
            await author_script(
                session,
                org_id=org_id,
                actor_id=user_id,
                name="bad",
                os_family="windows",
                interpreter="cobol",
                body="x",
            )


# ---------------------------------------------------------------------------
# ingest_script_result
# ---------------------------------------------------------------------------


async def _setup_dispatched_run(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> tuple[UUID, UUID, UUID, Job]:
    org_id, device_id, _group_id, user_id = await _setup(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        script = await author_script(
            session,
            org_id=org_id,
            actor_id=user_id,
            name="disk-cleanup",
            os_family="windows",
            interpreter="powershell",
            body="v1 body",
        )
        job_engine = _FakeJobEngine()
        pending = await run_script(
            session,
            job_engine,
            org_id=org_id,
            script_id=script.id,
            target_type="device",
            target_id=device_id,
            actor_id=user_id,
        )
        job_ids = [job_id for job_id, _device_id, _envelope in pending]
        job = (
            await session.execute(select(Job).where(Job.id == job_ids[0]))
        ).scalar_one()
        await session.commit()
        return org_id, device_id, script.id, job


async def test_succeeded_outcome_flips_script_run_state(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, _script_id, job = await _setup_dispatched_run(admin_engine, app_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = ScriptRunResult(
            outcome=InstallOutcome(identifier="script", state="succeeded", exit_code=0)
        )
        await ingest_script_result(session, job, result)

        run = (
            await session.execute(select(ScriptRun).where(ScriptRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "succeeded"
        assert run.exit_code == 0
        assert run.finished_at is not None

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "script.result")
            )
        ).scalar_one()
        assert event.actor_type == "system"
        assert event.actor_id is None
        assert event.meta["device_id"] == str(device_id)
        assert event.meta["state"] == "succeeded"


async def test_failed_outcome_flips_script_run_state(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _device_id, _script_id, job = await _setup_dispatched_run(admin_engine, app_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = ScriptRunResult(
            outcome=InstallOutcome(identifier="script", state="failed", exit_code=1)
        )
        await ingest_script_result(session, job, result)

        run = (
            await session.execute(select(ScriptRun).where(ScriptRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "failed"
        assert run.exit_code == 1


async def test_out_of_domain_state_coerced_to_failed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """`InstallOutcome.state` also allows "installed"/"not_applicable"
    (app.deploy's vocabulary), which is outside script.run's valid domain
    (`succeeded|failed`). It must be coerced to `failed` with the original
    value recorded in the audit metadata."""
    org_id, _device_id, _script_id, job = await _setup_dispatched_run(admin_engine, app_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        result = ScriptRunResult(
            outcome=InstallOutcome(identifier="script", state="installed", exit_code=0)
        )
        await ingest_script_result(session, job, result)

        run = (
            await session.execute(select(ScriptRun).where(ScriptRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "failed"  # coerced, not "installed" verbatim

        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "script.result")
            )
        ).scalar_one()
        assert event.meta["unexpected_outcome_state"] == "installed"


async def test_no_matching_run_row_is_a_graceful_no_op(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _device_id, _script_id, job = await _setup_dispatched_run(admin_engine, app_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        run = (
            await session.execute(select(ScriptRun).where(ScriptRun.job_id == job.id))
        ).scalar_one()
        run.job_id = None
        await session.flush()

        result = ScriptRunResult(
            outcome=InstallOutcome(identifier="script", state="succeeded", exit_code=0)
        )
        await ingest_script_result(session, job, result)  # must not raise

        events = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "script.result")
            )
        ).scalars().all()
        assert events == []


# ---------------------------------------------------------------------------
# tenancy
# ---------------------------------------------------------------------------


async def test_another_orgs_script_is_neither_runnable_nor_visible_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, device_a, _group_a, user_a = await _setup(admin_engine)
    org_b, _device_b, _group_b, user_b = await _setup(admin_engine)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as session:
        script = await author_script(
            session,
            org_id=org_a,
            actor_id=user_a,
            name="secret",
            os_family="windows",
            interpreter="powershell",
            body="A's body",
        )
        script_id = script.id
        await session.commit()

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as session:
        # org A's script is invisible under org B's RLS-scoped session.
        found = (
            await session.execute(select(Script).where(Script.id == script_id))
        ).scalar_one_or_none()
        assert found is None

        job_engine = _FakeJobEngine()
        pending = await run_script(
            session,
            job_engine,
            org_id=org_b,
            script_id=script_id,  # org A's script id, under org B's session
            target_type="device",
            target_id=device_a,
            actor_id=user_b,
        )
        assert pending == []
        assert job_engine.created == []


# ---------------------------------------------------------------------------
# kernel round-trip (Finding 2, whole-branch review): every other test in
# this file exercises ingest_script_result directly, which never runs
# app.modules.deploy.handler's ScriptRunResult(**result) parse or its
# queue_ui_event call. This test drives the real job_engine.complete() path
# (with a real JobEngine, not the fake, so run_script's own sign+dispatch
# path runs too) — mirrors test_deploy_result.py's deploy-side counterpart
# and test_inventory_ingest.py's test_valid_result_through_job_engine_ingests.
# ---------------------------------------------------------------------------


async def test_valid_script_result_through_job_engine_reconciles_and_queues_ui_event(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, device_id, _group_id, user_id = await _setup(admin_engine)
    engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        script = await author_script(
            session,
            org_id=org_id,
            actor_id=user_id,
            name="disk-cleanup",
            os_family="windows",
            interpreter="powershell",
            body="v1 body",
        )
        pending = await run_script(
            session,
            engine,
            org_id=org_id,
            script_id=script.id,
            target_type="device",
            target_id=device_id,
            actor_id=user_id,
        )
        assert len(pending) == 1
        job_id = pending[0][0]
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert job.signature is not None  # invariant #1: no unsigned job path

        await engine.mark_dispatched(session, job)
        await engine.complete(
            session,
            job,
            state="succeeded",
            result={
                "outcome": {"identifier": "script", "state": "succeeded", "exit_code": 0}
            },
        )
        assert job.status is JobStatus.succeeded

        run = (
            await session.execute(select(ScriptRun).where(ScriptRun.job_id == job.id))
        ).scalar_one()
        assert run.state == "succeeded"
        assert run.exit_code == 0

        # the handler queued a post-commit UI event on the session
        queued = session.info.get("ui_events", [])
        assert [e["kind"] for _, e in queued] == ["script_run_updated"]
        assert queued[0][1]["device_id"] == str(device_id)
        assert queued[0][1]["job_id"] == str(job.id)


async def test_the_run_script_org_predicates_hold_where_rls_is_not_doing_the_rejecting(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    The test above drives `run_script` through `org_session` on the NOBYPASSRLS
    `app_user` role, where `0002_rls.py`'s forced `tenant_isolation` policy has
    already removed org A's script and org A's device before `Script.org_id ==
    org_id` or `Device.org_id == org_id` is consulted -- so it stays green with
    all three of `run_script`'s predicates deleted. `its_admin` carries
    BYPASSRLS (`tests/conftest.py`), so here they are the only scoping there is.

    THIS IS THE HIGHEST-CONSEQUENCE PREDICATE IN THE MODULE, which is why the
    file gets an RLS-free test and not merely a docstring. `run_script` builds
    SIGNED `script.run` jobs, and the agent executes what it verifies as
    SYSTEM/root (invariant 1). A missing predicate here is not a disclosure, it
    is arbitrary code execution aimed at a machine in another customer's fleet
    -- and the signature would be perfectly valid, because the server is the
    thing that signed it.

    THREE PREDICATES, THREE DIFFERENT WAYS TO CROSS. A neighbour's script id
    (my devices run their body), a neighbour's device id (my script runs on
    their machine), and a neighbour's group id -- and the group branch has no
    schema backstop at all, because `devices.device_group_id` is a bare FK, so
    a device row may legally name a group in another org.

    Org B holds a device of its own in a group of its own, so the "no jobs"
    answers are a difference between two tenants rather than a fixture with
    nothing to dispatch: the last block dispatches for real.

    FALSIFIED, one at a time: deleting `Script.org_id == org_id` lets org B run
    org A's body; deleting `Device.org_id == org_id` from the device branch
    aims a job at org A's machine; deleting it from the group branch pulls org
    A's device out of org A's group into org B's run.
    """
    org_a, device_a, group_a, user_a = await _setup(admin_engine)
    org_b, device_b, group_b, user_b = await _setup(admin_engine)

    script_a = await author_script(
        bypassrls_session,
        org_id=org_a,
        actor_id=user_a,
        name="secret",
        os_family="windows",
        interpreter="powershell",
        body="A's body",
    )
    script_b = await author_script(
        bypassrls_session,
        org_id=org_b,
        actor_id=user_b,
        name="mine",
        os_family="windows",
        interpreter="powershell",
        body="B's body",
    )
    await bypassrls_session.commit()

    job_engine = _FakeJobEngine()

    # 1. a neighbour's SCRIPT, against my own device.
    assert await run_script(
        bypassrls_session, job_engine, org_id=org_b, script_id=script_a.id,
        target_type="device", target_id=device_b, actor_id=user_b,
    ) == []

    # 2. my own script, against a neighbour's DEVICE.
    assert await run_script(
        bypassrls_session, job_engine, org_id=org_b, script_id=script_b.id,
        target_type="device", target_id=device_a, actor_id=user_b,
    ) == []

    # 3. my own script, against a neighbour's GROUP.
    assert await run_script(
        bypassrls_session, job_engine, org_id=org_b, script_id=script_b.id,
        target_type="group", target_id=group_a, actor_id=user_b,
    ) == []

    assert job_engine.created == [], "a signed job was built across the tenant boundary"

    # Non-vacuity: the same call against org B's own group DOES dispatch.
    pending = await run_script(
        bypassrls_session, job_engine, org_id=org_b, script_id=script_b.id,
        target_type="group", target_id=group_b, actor_id=user_b,
    )
    assert len(pending) == 1
    assert [job["target_device_id"] for job in job_engine.created] == [device_b]


async def test_a_group_script_run_passes_over_a_device_waiting_for_review(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from sqlalchemy import update

    org_id, device_id, group_id, user_id = await _setup(admin_engine)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Device).where(Device.id == device_id).values(review_state="pending")
        )
        await session.commit()
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
        script = await author_script(
            session, org_id=org_id, actor_id=user_id, name="ping",
            os_family="linux", interpreter="bash", body="echo hi",
        )
        job_engine = _FakeJobEngine()
        pending = await run_script(
            session, job_engine, org_id=org_id, script_id=script.id,
            target_type="group", target_id=group_id, actor_id=user_id,
        )
    assert pending == [] and job_engine.created == []


async def test_a_single_device_script_run_on_a_device_waiting_for_review_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A device the operator NAMED is not filtered away into a silent no-op
    (which would still audit `script.dispatched`): the gate refuses it, and
    the API answers 409 "waiting for review"."""
    from sqlalchemy import update

    from app.jobs.review_gate import DeviceAwaitingReview

    org_id, device_id, _group_id, user_id = await _setup(admin_engine)
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Device).where(Device.id == device_id).values(review_state="pending")
        )
        await session.commit()
    job_engine = JobEngine(signer=LocalEd25519Signer(tmp_path / "k.key"))
    with pytest.raises(DeviceAwaitingReview):
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session:
            script = await author_script(
                session, org_id=org_id, actor_id=user_id, name="ping",
                os_family="linux", interpreter="bash", body="echo hi",
            )
            await run_script(
                session, job_engine, org_id=org_id, script_id=script.id,
                target_type="device", target_id=device_id, actor_id=user_id,
            )
