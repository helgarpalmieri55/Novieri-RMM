"""`dispatch.RunDispatcher`: the pacer's only way to sign a job and decrypt a
credential (design §1.4; CLAUDE.md invariants 1, 3, 4; 2a(e) as amended).

Each test removes exactly one bound and shows it is a NAMED refusal returned
before the vault is touched, never an exception and never a job.
"""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, EnrollmentToken, Job
from app.modules.agent_push import credential_scope, preflight
from app.modules.agent_push.jobtypes import INSTALL_JOB_TYPE
from app.modules.agent_push.models import PushBatchTarget
from app.modules.agent_push.net import parse_target_address
from tests.push_run_fixtures import FakeVault, RunWorld, make_dispatcher, run_world


@pytest.fixture(autouse=True)
def _register_push_job_types() -> None:
    from app.modules.agent_push.jobtypes import register_job_types

    register_job_types()


async def _dispatch_one(app_engine: AsyncEngine, world: RunWorld, dispatcher, run_id):
    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        credential = await credential_scope.resolve_push_credential(
            session, world.org.org_id, world.credential_id
        )
        (decision,) = await preflight.evaluate_targets(
            session,
            world.org.org_id,
            addresses=[parse_target_address(world.addresses[0])],
            credential_id=world.credential_id,
        )
        return await dispatcher.dispatch_target(
            session,
            org_id=world.org.org_id,
            run_id=run_id,
            on_behalf_of=world.org.user_id,
            credential=credential,
            decision=decision,
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


async def test_a_ready_target_becomes_one_signed_job_on_behalf_of_the_run(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rd-ok", octet=71, count=1)
    vault = FakeVault()
    run_id = uuid4()
    outcome = await _dispatch_one(
        app_engine, world, make_dispatcher(tmp_path, vault=vault), run_id
    )

    assert outcome.dispatched and outcome.refusals == ()
    assert vault.calls == 1
    assert outcome.envelope is not None
    assert outcome.envelope["job"]["device"] == str(world.site_server_device_id)
    async with AsyncSession(admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == outcome.job_id))).scalar_one()
        token = (
            await session.execute(
                select(EnrollmentToken).where(EnrollmentToken.org_id == world.org.org_id)
            )
        ).scalar_one()
        grouping = (
            await session.execute(
                select(PushBatchTarget).where(PushBatchTarget.job_id == outcome.job_id)
            )
        ).scalar_one()
        dispatched = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == world.org.org_id,
                    AuditEvent.action == "push.dispatched",
                )
            )
        ).scalar_one()
    assert job.type == INSTALL_JOB_TYPE and job.signature
    assert job.requested_by == world.org.user_id
    assert token.name == f"push:{run_id}:{world.addresses[0]}" and token.max_uses == 1
    assert grouping.batch_id == run_id
    # The pacer acted, on behalf of the person who confirmed the run (R17).
    assert dispatched.actor_type == "system" and dispatched.actor_id is None
    assert dispatched.meta["run_id"] == str(run_id)
    assert dispatched.meta["on_behalf_of"] == str(world.org.user_id)
    assert "hunter2" not in str(dispatched.meta)


async def test_an_unconfirmed_host_key_is_refused_before_the_vault(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rd-hk", octet=72, count=1, confirmed_keys=False)
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    outcome = await _dispatch_one(app_engine, world, dispatcher, uuid4())
    assert outcome.refusals == ("host_key_unconfirmed",) and not outcome.dispatched
    assert vault.calls == 0
    assert await _count(admin_engine, Job, world.org.org_id) == 0
    # The dispatcher records nothing itself: the pacer writes `push.refused`.
    assert await _count(admin_engine, AuditEvent, world.org.org_id) == 0


async def test_an_undeclared_processor_is_a_named_refusal_not_an_exception(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rd-arch", octet=73, count=1, architecture="")
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    outcome = await _dispatch_one(app_engine, world, dispatcher, uuid4())
    assert outcome.refusals == ("not_installable",)
    assert vault.calls == 0


async def test_no_published_release_is_a_named_refusal(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path, monkeypatch
) -> None:
    from app.modules.agent_push import dispatch as dispatch_module
    from app.modules.agent_push.errors import PushValidationError

    world = await run_world(admin_engine, "rd-rel", octet=74, count=1)

    async def _no_release(session, os_name, arch):
        raise PushValidationError(f"no published agent release exists for {os_name}/{arch}")

    monkeypatch.setattr(dispatch_module, "_resolve_release", _no_release)
    vault = FakeVault()
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    outcome = await _dispatch_one(app_engine, world, dispatcher, uuid4())
    assert outcome.refusals == ("no_release",)
    assert outcome.extras == {"os": "linux", "arch": "amd64"}
    assert vault.calls == 0


async def test_an_unreadable_secret_is_refused_and_nothing_is_signed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    world = await run_world(admin_engine, "rd-sec", octet=75, count=1)
    vault = FakeVault(plaintext="not json")
    dispatcher = make_dispatcher(tmp_path, vault=vault)
    outcome = await _dispatch_one(app_engine, world, dispatcher, uuid4())
    assert outcome.refusals == ("credential_unreadable",)
    assert vault.calls == 1
    assert await _count(admin_engine, Job, world.org.org_id) == 0
    assert await _count(admin_engine, EnrollmentToken, world.org.org_id) == 0
