"""SECURITY GATE: §9.2's two-part confirm check (docs/modules/ai-automation.md
§0.11, §9.2; §13 test 1).

**THE GOVERNING PRINCIPLE OF THIS WHOLE LAYER, AS TESTS.** Confirming an
AI-proposed action requires `ai.act.confirm` AND the permission the owning
module's own dispatch route requires AND that the confirming principal can read
the target device. Without the second, a technician holding only
`ai.act.confirm` could fire `patch.apply` through this layer while being refused
it on `POST /api/v1/patch/apply` -- privilege escalation, invariant #6 violated,
and the exact negation of §0.11.

**EVERY TEST HERE ASSERTS THE `jobs` TABLE, NOT ONLY THE STATUS.** "403" and
"403 but it also dispatched" are different facts, and only the second is a
breach. A test that stops at the exception cannot tell them apart.
"""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.act import ActForbidden, confirm_action, propose
from app.core.ai.executor_staff import invoke_tool
from app.core.tenancy import org_session
from app.models import AiAction, Job
from tests.ai_act_fixtures import build_world

# What a human needs to run these job types BY HAND, per
# `app/jobs/dispatch_permissions.py`. Named here so the test reads as the claim
# it is making rather than as a string.
PATCH_APPLY_GATE = "patch.apply"


async def _job_count(api_env: Any, org_id: Any) -> int:
    async with AsyncSession(api_env.admin_engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Job).where(Job.org_id == org_id)
                )
            ).scalar_one()
        )


async def _preview(api_env: Any, world: Any) -> Any:
    async with org_session(world.org_id, engine=api_env.app_engine) as session:
        return await propose(
            session,
            org_id=world.org_id,
            binding=world.binding,
            ctx=world.ctx,
            executor=invoke_tool,
            action_key=world.action_key,
            device_id=world.device_id,
        )


async def _confirm(api_env: Any, world: Any, preview: Any, *, permissions: tuple[str, ...]) -> Any:
    async with org_session(world.org_id, engine=api_env.app_engine) as session:
        return await confirm_action(
            session,
            org_id=world.org_id,
            ai_action_id=preview.ai_action_id,
            digest=preview.preview_digest,
            actor_id=world.user_id,
            permissions=permissions,
            job_engine=api_env.app.state.job_engine,
            ctx=world.ctx,
            executor=invoke_tool,
        )


async def _patch_world(api_env: Any, tag: str, *, codes: tuple[str, ...]) -> Any:
    """A world whose catalogue row is `patch.apply` -- the job type §0.11 names,
    and one whose own module route is gated on something a technician may well
    not hold."""
    return await build_world(
        api_env,
        tag=tag,
        codes=codes,
        action_kw={
            "job_type": "patch.apply",
            "destructive": True,
            # The real `PatchApplyParams` shape -- the engine validates params
            # against the registered job type, so a plausible-looking blob
            # would fail at `JobEngine.create` rather than at the gate under
            # test.
            "params_template": {
                "patches": [{"kb": "KB5000001"}],
                "reboot": "never",
            },
        },
    )


# --------------------------------------------------------------------------- #
# §13 test 1 -- ai.act.confirm alone is not enough.
# --------------------------------------------------------------------------- #


async def test_ai_act_confirm_without_the_job_types_own_gate_is_refused_and_no_job_is_created(
    api_env: Any,
) -> None:
    """**THE GOVERNING PRINCIPLE.** The caller holds `ai.act.confirm` and NOT
    `patch.apply`, so they may not confirm a `patch.apply` action here -- because
    they could not run one by hand.

    The job COUNT is the assertion, not the exception: deleting the
    `dispatch_permission_for` check makes this print a created `Job` row, which
    is invariant #6 violated in one observable value.
    """
    world = await _patch_world(
        api_env, "authz-1", codes=("ai.use", "ai.act.propose", "ai.act.confirm", "devices.read")
    )
    preview = await _preview(api_env, world)

    # **NOT `pytest.raises`.** Deleting the `dispatch_permission_for` check makes
    # a `pytest.raises` form print `DID NOT RAISE` and stop, so the job count --
    # the fact that matters -- would never be asserted. Reduced to a comparable
    # tuple, the mutation prints `('dispatched', 1, 'dispatched', UUID(...))`:
    # a `patch.apply` job created by a caller who cannot run one by hand.
    outcome, jobs, state, job_id = "dispatched", None, None, None
    try:
        result = await _confirm(
            api_env, world, preview, permissions=("ai.act.confirm", "devices.read")
        )
        job_id = result.job_id
    except ActForbidden as exc:
        outcome = f"forbidden: {exc}"
    jobs = await _job_count(api_env, world.org_id)
    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(select(AiAction).where(AiAction.id == preview.ai_action_id))
        ).scalar_one()
        state = row.state

    assert (outcome.startswith("forbidden"), jobs, state, job_id, row.confirmed_by) == (
        True,
        0,
        "previewed",
        None,
        None,
    ), outcome
    assert PATCH_APPLY_GATE in outcome


async def test_the_same_user_holding_both_confirms_and_exactly_one_job_appears(
    api_env: Any,
) -> None:
    """The other half. Without it, "403" is equally consistent with a confirm
    path that refuses everybody -- and `requested_by` is asserted because §6.5
    says the human is recorded as the requester, since a human authorized it and
    this platform has no `actor_type='ai'`."""
    world = await _patch_world(
        api_env,
        "authz-2",
        codes=(
            "ai.use",
            "ai.act.propose",
            "ai.act.confirm",
            "devices.read",
            PATCH_APPLY_GATE,
        ),
    )
    preview = await _preview(api_env, world)
    before = await _job_count(api_env, world.org_id)

    outcome = await _confirm(
        api_env,
        world,
        preview,
        permissions=("ai.act.confirm", "devices.read", PATCH_APPLY_GATE),
    )

    assert await _job_count(api_env, world.org_id) == before + 1
    async with AsyncSession(api_env.admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == outcome.job_id))).scalar_one()
        row = (
            await session.execute(select(AiAction).where(AiAction.id == preview.ai_action_id))
        ).scalar_one()
    assert job.type == "patch.apply"
    assert job.target_id == world.device_id
    assert job.requested_by == world.user_id
    assert (row.state, row.job_id, row.confirmed_by) == ("dispatched", job.id, world.user_id)
    # And the envelope came back for the CALLER to publish after its
    # transaction commits -- never published from inside it.
    assert outcome.envelope["job"]["id"] == str(job.id)
    assert outcome.envelope["sig"]


async def test_ai_act_confirm_is_itself_required(api_env: Any) -> None:
    """The FIRST part of the two-part check, asserted separately so a refusal
    cannot be attributed to the wrong half. Holding the module's own gate and
    not `ai.act.confirm` is refused too."""
    world = await _patch_world(
        api_env, "authz-3", codes=("ai.use", "ai.act.propose", "devices.read", PATCH_APPLY_GATE)
    )
    preview = await _preview(api_env, world)
    before = await _job_count(api_env, world.org_id)

    with pytest.raises(ActForbidden, match="ai.act.confirm"):
        await _confirm(
            api_env, world, preview, permissions=("devices.read", PATCH_APPLY_GATE)
        )
    assert await _job_count(api_env, world.org_id) == before


# --------------------------------------------------------------------------- #
# 3. A job type with no human dispatch path is not confirmable.
# --------------------------------------------------------------------------- #


async def test_a_job_type_with_no_human_dispatch_route_is_refused(api_env: Any) -> None:
    """`dispatch_permission_for` returning `None` is a REFUSAL, not a pass.

    `health.policy.apply` is dispatched by health's own reconciliation from
    state the kernel minted; there is no route a human can call, so there is no
    permission that would authorize it and the question §9.2 asks has no answer.
    Seeded directly, because `non_approvable_reason` refuses it at propose --
    which is the point: this is the second line, and it must hold on its own.
    """
    from tests.test_ai_action_migration import _Refs, _seed_ai_action

    world = await build_world(api_env, tag="authz-noroute")
    refs = _Refs(
        world.org_id, world.user_id, world.device_id, world.ai_session_id, world.action_id
    )
    action_id = await _seed_ai_action(
        api_env.admin_engine, refs, key="policy-push", state="previewed"
    )
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        row = await session.get(AiAction, action_id)
        assert row is not None
        row.job_type = "health.policy.apply"
        # The digest is computed FROM THE STORED ROW, so editing the row means
        # recomputing it -- otherwise this test would be exercising the digest
        # mismatch (a 409) rather than the gate it is about (a 403).
        from app.core.ai.act import compute_preview_digest
        from app.models import ApprovedAction

        action = await session.get(ApprovedAction, world.action_id)
        assert action is not None
        row.preview_digest = compute_preview_digest(row, action)
        await session.commit()

    before = await _job_count(api_env, world.org_id)
    async with org_session(world.org_id, engine=api_env.app_engine) as session:
        stored = await session.get(AiAction, action_id)
        assert stored is not None
        with pytest.raises(ActForbidden, match="no human dispatch route"):
            await confirm_action(
                session,
                org_id=world.org_id,
                ai_action_id=action_id,
                digest=stored.preview_digest,
                actor_id=world.user_id,
                permissions=(
                    "ai.act.confirm",
                    "devices.read",
                    "health.admin",
                    "jobs.dispatch",
                ),
                job_engine=api_env.app.state.job_engine,
                ctx=world.ctx,
                executor=invoke_tool,
            )
    assert await _job_count(api_env, world.org_id) == before


# --------------------------------------------------------------------------- #
# 4. The device check, through the loopback, at confirm as well as at preview.
# --------------------------------------------------------------------------- #


async def test_a_confirmer_who_cannot_read_the_device_is_refused(api_env: Any) -> None:
    """§9.2's third clause. The proposing principal could see the device; the
    CONFIRMING principal is a different human and is checked again, through the
    same loopback -- asserted on the spy, so "the request was made and the route
    said 403" is distinguishable from "no request was made"."""
    from tests.ai_admin_fakes import RecordingTransport, ai_user, install_spy

    world = await build_world(api_env, tag="authz-device")
    preview = await _preview(api_env, world)
    before = await _job_count(api_env, world.org_id)

    # A SECOND principal in the same org, holding the confirm grants but NOT
    # `devices.read`.
    confirmer_id, sub = await ai_user(
        api_env, world.org_id, "ai.act.confirm", "jobs.dispatch"
    )
    headers = await api_env.headers(world.org_id, sub)
    ctx = type(world.ctx)(
        app=world.ctx.app,
        bearer=headers["Authorization"],
        deadline=world.ctx.deadline,
        org_id=world.org_id,
        user_id=confirmer_id,
    )

    spy = RecordingTransport(api_env.app)
    with pytest.MonkeyPatch.context() as monkeypatch:
        install_spy(monkeypatch, spy)
        async with org_session(world.org_id, engine=api_env.app_engine) as session:
            with pytest.raises(ActForbidden, match="no such device"):
                await confirm_action(
                    session,
                    org_id=world.org_id,
                    ai_action_id=preview.ai_action_id,
                    digest=preview.preview_digest,
                    actor_id=confirmer_id,
                    permissions=("ai.act.confirm", "jobs.dispatch"),
                    job_engine=api_env.app.state.job_engine,
                    ctx=ctx,
                    executor=invoke_tool,
                )

    urls = [str(r.url) for r in spy.requests]
    assert urls and urls[-1].endswith(f"/api/v1/devices/{world.device_id}"), urls
    assert await _job_count(api_env, world.org_id) == before


async def test_a_cross_org_ai_action_is_a_404_never_another_orgs_row(api_env: Any) -> None:
    """§13 test 17. A 403 would confirm the id exists somewhere, which is a
    cross-tenant existence oracle; 404 is indistinguishable from "never
    existed"."""
    from app.core.ai.errors import AiNotFoundError

    world = await build_world(api_env, tag="authz-xorg-a")
    other = await build_world(api_env, tag="authz-xorg-b")
    theirs = await _preview(api_env, other)

    async with org_session(world.org_id, engine=api_env.app_engine) as session:
        with pytest.raises(AiNotFoundError):
            await confirm_action(
                session,
                org_id=world.org_id,
                ai_action_id=theirs.ai_action_id,
                digest=theirs.preview_digest,
                actor_id=world.user_id,
                permissions=("ai.act.confirm", "devices.read", "jobs.dispatch"),
                job_engine=api_env.app.state.job_engine,
                ctx=world.ctx,
                executor=invoke_tool,
            )
        # And an id that never existed is the same answer, from the same place.
        with pytest.raises(AiNotFoundError):
            await confirm_action(
                session,
                org_id=world.org_id,
                ai_action_id=uuid4(),
                digest=theirs.preview_digest,
                actor_id=world.user_id,
                permissions=("ai.act.confirm", "devices.read", "jobs.dispatch"),
                job_engine=api_env.app.state.job_engine,
                ctx=world.ctx,
                executor=invoke_tool,
            )

    async with AsyncSession(api_env.admin_engine) as session:
        row = (
            await session.execute(select(AiAction).where(AiAction.id == theirs.ai_action_id))
        ).scalar_one()
    assert row.state == "previewed"
    assert await _job_count(api_env, other.org_id) == 0
