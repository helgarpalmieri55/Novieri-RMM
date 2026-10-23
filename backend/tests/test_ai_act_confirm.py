"""Confirm dispatches the stored preview, or refuses it (§6.4, §6.5; §13 tests
5-9, 21).

**THE CONFIRM REQUEST CARRIES AN ID AND A DIGEST AND NOTHING ELSE.** No job
type, no params, no target. Everything dispatched is read back off the stored
`ai_action` row, and the digest is recomputed by the server from that row.

THE THREE REFUSALS ARE DISTINGUISHED ON PURPOSE, because an operator acts on
them differently:

* **409** -- the request is well formed and THE WORLD MOVED (catalogue edited,
  dial lowered, script version bumped, fuse now breached, already confirmed).
  Not a 400: nothing about the request is wrong.
* **410** -- the preview's clock ran out. The row is left alone here and the
  expiry sweep moves it; see the expiry test for why, and for the plan/spec
  disagreement it resolves.
* **403** -- the principal may not do this (`test_ai_act_confirm_authz.py`).

Every test below asserts the `jobs` table as well as the exception, for the
reason that file's header gives: "refused" and "refused but it also dispatched"
are different facts.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.act import (
    ActConflict,
    ActExpired,
    ActForbidden,
    confirm_action,
    propose,
    reject_action,
)
from app.core.ai.errors import AiValidationError
from app.core.ai.executor_staff import invoke_tool
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AiAction, ApprovedAction, AuditEvent, Job
from tests.ai_act_fixtures import build_world

CONFIRMER = ("ai.act.confirm", "ai.act.propose", "devices.read", "jobs.dispatch")


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
    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        return await propose(
            session,
            org_id=world.org_id,
            binding=world.binding,
            ctx=world.ctx,
            executor=invoke_tool,
            action_key=world.action_key,
            device_id=world.device_id,
        )


async def _confirm(
    api_env: Any,
    world: Any,
    ai_action_id: Any,
    digest: str,
    *,
    permissions: tuple[str, ...] = CONFIRMER,
    now: datetime | None = None,
) -> Any:
    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        return await confirm_action(
            session,
            org_id=world.org_id,
            ai_action_id=ai_action_id,
            digest=digest,
            actor_id=world.user_id,
            permissions=permissions,
            job_engine=api_env.app.state.job_engine,
            ctx=world.ctx,
            executor=invoke_tool,
            now=now,
        )


async def _row(api_env: Any, ai_action_id: Any) -> AiAction:
    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(AiAction).where(AiAction.id == ai_action_id))
        ).scalar_one()




async def _attempt(api_env: Any, world: Any, ai_action_id: Any, digest: str, **kw: Any) -> tuple:
    """`(outcome, job count, state, job_id)` for one confirm attempt.

    **REDUCED TO A COMPARABLE TUPLE RATHER THAN WRAPPED IN `pytest.raises`.**
    A `pytest.raises` that fails prints `DID NOT RAISE` and stops -- so the job
    count and the row state, which are the facts that distinguish "refused" from
    "refused but it also dispatched", never get asserted at all. Every mutation
    on this path was run against both forms; only this one prints the created
    `Job`.
    """
    outcome = "dispatched"
    job_id = None
    try:
        result = await _confirm(api_env, world, ai_action_id, digest, **kw)
        job_id = result.job_id
    except ActForbidden as exc:
        outcome = f"forbidden: {exc}"
    except ActExpired:
        outcome = "expired"
    except ActConflict as exc:
        outcome = f"conflict: {exc}"
    row = await _row(api_env, ai_action_id)
    return outcome, await _job_count(api_env, world.org_id), row.state, job_id



# --------------------------------------------------------------------------- #
# §13 test 5 -- a digest that does not match.
# --------------------------------------------------------------------------- #


async def test_a_digest_that_does_not_match_is_a_409_and_no_job(api_env: Any) -> None:
    world = await build_world(api_env, tag="conf-digest")
    preview = await _preview(api_env, world)

    outcome, jobs, state, job_id = await _attempt(
        api_env, world, preview.ai_action_id, "f" * 64
    )
    assert (outcome.startswith("conflict"), jobs, state, job_id) == (True, 0, "previewed", None)


# --------------------------------------------------------------------------- #
# §13 test 6 -- editing the catalogue between preview and confirm.
# --------------------------------------------------------------------------- #


async def test_editing_the_catalogue_row_between_preview_and_confirm_is_a_409(
    api_env: Any,
) -> None:
    """The digest the human was shown is still the one they send; what changed
    is the row it described. The message NAMES the catalogue, because "409" on
    its own tells an operator nothing about which of the four things moved."""
    world = await build_world(api_env, tag="conf-edit")
    preview = await _preview(api_env, world)
    before = await _job_count(api_env, world.org_id)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        action = await session.get(ApprovedAction, world.action_id)
        assert action is not None
        action.description = "an administrator edited this"
        action.updated_at = datetime.now(UTC) + timedelta(seconds=1)
        await session.commit()

    outcome, jobs, state, job_id = await _attempt(
        api_env, world, preview.ai_action_id, preview.preview_digest
    )
    assert (jobs, state, job_id) == (before, "previewed", None), outcome
    assert "catalogue" in outcome, outcome


async def test_a_recomputed_digest_does_not_re_validate_an_edited_catalogue_row(
    api_env: Any,
) -> None:
    """**THE DIGEST GATE IS SERVER-AUTHORITATIVE, AND IT SHIPPED CLIENT-SIDE.**

    `confirm_action` compared its recomputation against the digest the CALLER
    sent, and never read `row.preview_digest` at all. So the invariant §6.4
    names -- "the row has not changed since the server issued the preview" --
    was enforced by a value the client supplies: anybody able to compute the
    current digest could confirm an act whose catalogue row had been edited
    after the human saw it, and the server would agree with itself.

    This is not hypothetical. `test_a_fuse_that_passed_at_preview_and_is_
    breached_at_confirm_refuses` used to do exactly this on purpose, to get past
    a gate it was not testing -- a test in this suite demonstrating the bypass
    working.

    The world here: the catalogue row is edited AFTER the preview, and the
    caller supplies a digest recomputed over the edited row -- the strongest
    position a client can be in. `(outcome, jobs, state, job_id)` must still be
    the refusal, and the message must still NAME the catalogue, because that is
    what moved.

    **THE MESSAGE ASSERTION IS PINNING THE CHECK ORDER, DELIBERATELY.** Both
    conditions are true here, and with both checks present either order gives
    the same verdict -- so the only thing left to get wrong is which fact the
    operator is told. Reversing the two in `confirm_action` makes this print the
    digest-bookkeeping message instead of the catalogue one, which is the less
    useful half.
    """
    from app.core.ai.act import compute_preview_digest

    world = await build_world(api_env, tag="conf-recompute")
    preview = await _preview(api_env, world)
    before = await _job_count(api_env, world.org_id)

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        action = await session.get(ApprovedAction, world.action_id)
        assert action is not None
        action.description = "an administrator edited this after the human looked"
        action.updated_at = datetime.now(UTC) + timedelta(seconds=1)
        await session.commit()

    async with AsyncSession(api_env.admin_engine) as session:
        action = await session.get(ApprovedAction, world.action_id)
        row = await session.get(AiAction, preview.ai_action_id)
        assert action is not None and row is not None
        recomputed = compute_preview_digest(row, action)
        stored = row.preview_digest

    # The premise: the caller's digest is the CURRENT one, and it differs from
    # what the server issued. Without this, the test could pass because the two
    # happened to be equal and nothing was being bypassed.
    assert recomputed != stored, (recomputed, stored)

    outcome, jobs, state, job_id = await _attempt(
        api_env, world, preview.ai_action_id, recomputed
    )
    assert (jobs, state, job_id) == (before, "previewed", None), outcome
    assert "catalogue" in outcome, outcome


async def test_a_confirmation_carrying_someone_elses_valid_digest_is_refused(
    api_env: Any,
) -> None:
    """The other half of the same pair, and the one a lone recompute check would
    miss: nothing about the world moved, and the digest is a REAL digest -- of a
    different action, in the same org, previewed in the same way.

    §6.4: "the digest without the row confirms an act nobody previewed". Sending
    action A's digest with action B's id is a confirmation of a preview no human
    was shown for B, so it is refused even though B itself is confirmable -- and
    the tuple proves B stayed `previewed` with no job, rather than merely that
    something raised.
    """
    world = await build_world(api_env, tag="conf-swap")
    first = await _preview(api_env, world)
    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        second = await propose(
            session,
            org_id=world.org_id,
            binding=replace(world.binding, message_seq=world.binding.message_seq + 1),
            ctx=world.ctx,
            executor=invoke_tool,
            action_key=world.action_key,
            device_id=world.device_id,
        )
    before = await _job_count(api_env, world.org_id)
    assert first.preview_digest != second.preview_digest

    outcome, jobs, state, job_id = await _attempt(
        api_env, world, second.ai_action_id, first.preview_digest
    )
    assert (jobs, state, job_id) == (before, "previewed", None), outcome
    assert "conflict" in outcome, outcome


async def test_a_catalogue_row_with_a_proposal_against_it_cannot_be_deleted(
    api_env: Any,
) -> None:
    """**A FINDING ABOUT §5.1, RECORDED RATHER THAN WORKED AROUND.**

    §5.1 denormalizes `approved_action_key` and `job_type` onto `ai_action` "so
    the record survives a catalogue delete". It cannot, because
    `approved_action_id` is a NOT NULL foreign key with no `ON DELETE` clause,
    which Postgres treats as NO ACTION: the DELETE is REFUSED while any
    `ai_action` references the row. The denormalization is therefore not doing
    what its own comment claims today.

    That is the SAFE direction and this build does not change it -- an
    `ai_action` whose catalogue row vanished would be a dispatch record naming a
    policy nobody can look up -- but it means the surviving behaviour is
    "refused", not "the record survives", and the confirm path's
    `no longer exists` branch is unreachable through an ordinary delete. Both
    are asserted so a future `ON DELETE SET NULL` (which the NOT NULL column
    also forbids) has to argue with a test.
    """
    world = await build_world(api_env, tag="conf-deleted")
    preview = await _preview(api_env, world)

    refused = False
    try:
        async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
            await session.execute(
                ApprovedAction.__table__.delete().where(ApprovedAction.id == world.action_id)
            )
            await session.commit()
    except IntegrityError:
        refused = True

    row = await _row(api_env, preview.ai_action_id)
    assert (refused, row.approved_action_key, row.job_type) == (
        True,
        world.action_key,
        "inventory.collect",
    )
    # And the confirm still works, because nothing actually changed.
    outcome = await _confirm(api_env, world, preview.ai_action_id, preview.preview_digest)
    assert outcome.job_id is not None


# --------------------------------------------------------------------------- #
# §13 test 7 -- lowering the dial between preview and confirm.
# --------------------------------------------------------------------------- #


async def test_lowering_the_dial_between_preview_and_confirm_refuses_the_dispatch(
    api_env: Any,
) -> None:
    """§4.2's second evaluation, and it is NOT redundant: the dial is org state
    an admin may change at any moment, and a confirmation is a decision about
    NOW. The digest still matches -- nothing on the row or the catalogue moved --
    so this refusal comes from the re-evaluation and from nowhere else."""
    world = await build_world(api_env, tag="conf-dial")
    preview = await _preview(api_env, world)
    before = await _job_count(api_env, world.org_id)

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        from app.core.ai.autonomy import set_ceiling

        await set_ceiling(
            session,
            org_id=world.org_id,
            persona="admin",
            risk_class="low",
            level="suggest",
            actor_id=world.user_id,
        )

    with pytest.raises(ActConflict, match="autonomy setting"):
        await _confirm(api_env, world, preview.ai_action_id, preview.preview_digest)

    assert await _job_count(api_env, world.org_id) == before
    assert (await _row(api_env, preview.ai_action_id)).state == "previewed"


# --------------------------------------------------------------------------- #
# §13 test 9 -- expiry.
# --------------------------------------------------------------------------- #


async def test_a_passed_expiry_is_a_410_and_no_job(api_env: Any) -> None:
    """410, not 409: this preview was valid and its clock ran out.

    **RECORDED DISAGREEMENT BETWEEN THE PLAN AND THE SPEC, RESOLVED IN THE
    SPEC'S FAVOUR.** The plan says the row "becomes `expired`" on this path;
    §6.4 says "410, and THE SWEEP marks it `expired`". The spec is right and the
    plan could not have worked: `confirm_action` raises, its caller's tenant
    transaction rolls back, and a `state = "expired"` written here would be
    rolled back with it -- leaving a test that asserted `expired` failing, and a
    reader believing in a transition that never happens. So the state is
    asserted as UNCHANGED here and the sweep (Task 9) is what moves it; the
    clock is re-checked on every confirm, so the row is not confirmable
    meanwhile, which is asserted below.
    """
    world = await build_world(api_env, tag="conf-expiry")
    preview = await _preview(api_env, world)
    before = await _job_count(api_env, world.org_id)

    later = datetime.now(UTC) + timedelta(seconds=world.binding.action_expiry_s + 60)
    with pytest.raises(ActExpired):
        await _confirm(
            api_env, world, preview.ai_action_id, preview.preview_digest, now=later
        )

    assert await _job_count(api_env, world.org_id) == before
    assert (await _row(api_env, preview.ai_action_id)).state == "previewed"

    # And it stays unconfirmable: a second attempt past the clock is a 410 too,
    # so "still previewed" is not "still usable".
    with pytest.raises(ActExpired):
        await _confirm(
            api_env, world, preview.ai_action_id, preview.preview_digest, now=later
        )
    assert await _job_count(api_env, world.org_id) == before


async def test_one_second_inside_the_window_still_confirms(api_env: Any) -> None:
    """The other half of the boundary. Without it, an expiry check written the
    wrong way round -- or one that always fired -- would pass the test above."""
    world = await build_world(api_env, tag="conf-inside")
    preview = await _preview(api_env, world)

    just_inside = preview.expires_at - timedelta(seconds=1)
    outcome = await _confirm(
        api_env, world, preview.ai_action_id, preview.preview_digest, now=just_inside
    )
    assert outcome.job_id is not None
    assert (await _row(api_env, preview.ai_action_id)).state == "dispatched"


# --------------------------------------------------------------------------- #
# A second confirm, and the fuse gate.
# --------------------------------------------------------------------------- #


async def test_a_second_confirm_is_a_409_and_the_first_job_is_not_duplicated(
    api_env: Any,
) -> None:
    """§5.2: a second confirm is a conflict BY THE STATE MACHINE, not by an
    idempotency key -- which is why `ai_action` carries none (§0.5). The COUNT is
    the assertion; a state machine that let the row through twice would produce
    two signed jobs against one preview."""
    world = await build_world(api_env, tag="conf-twice")
    preview = await _preview(api_env, world)

    first = await _confirm(api_env, world, preview.ai_action_id, preview.preview_digest)
    after_first = await _job_count(api_env, world.org_id)

    with pytest.raises(ActConflict, match="dispatched"):
        await _confirm(api_env, world, preview.ai_action_id, preview.preview_digest)

    assert await _job_count(api_env, world.org_id) == after_first == 1
    row = await _row(api_env, preview.ai_action_id)
    assert (row.state, row.job_id) == ("dispatched", first.job_id)


async def test_a_fuse_that_passed_at_preview_and_is_breached_at_confirm_refuses(
    api_env: Any,
) -> None:
    """**THE PREVIEW'S FUSE READ IS INFORMATIONAL; THIS ONE IS THE GATE** (§6.5
    step 3).

    The action's cooldown is 0 at preview -- so the preview's verdict is `None`,
    asserted -- and the catalogue row's fuses are then tightened and a job of the
    same type is seeded on the same device.

    **THE FUSE IS ARMED BEFORE THE PREVIEW AND TRIPPED AFTER IT, WITHOUT
    TOUCHING THE CATALOGUE ROW -- WHICH IS THE ONLY WAY TO ISOLATE THIS GATE.**

    Two earlier shapes of this test were wrong, each for its own reason, and
    both are worth keeping written down:

    1. It tightened `cooldown_s` after the preview and sent
       `preview.preview_digest`, claiming "nothing about the digest's ten fields
       changes, because `cooldown_s` is not one of them".
       `approved_action_updated_at` IS one of them and `ApprovedAction.updated_at`
       carries `onupdate`, so tightening the fuses moves it. It passed alone only
       because `rfc3339` truncated that field to whole seconds and the sequence
       fitted inside one; under load it straddled a boundary and answered with
       the CATALOGUE-EDITED conflict instead (§19.15).
    2. It then RECOMPUTED the digest over the edited row to get past that gate.
       That worked, and it should not have: §19.18 found that recomputing was a
       client-side bypass of the very check §6.4 says is server-authoritative.
       A test getting past a gate by the same route an attacker would is a test
       that would have kept the defect green.

    So the fuse is now part of the world from the start (`cooldown_s=3600` at
    build time), the preview is taken with no prior job -- verdict `None`,
    asserted, so the fuse genuinely was open -- and a job is seeded afterwards
    to trip it. The catalogue row is never written to, the digest never moves,
    and the only thing left that can refuse is the second fuse read.
    """
    from tests.test_approved_actions_fuses import _seed_job

    world = await build_world(
        api_env, tag="conf-fuse", action_kw={"cooldown_s": 3600}
    )
    preview = await _preview(api_env, world)
    assert preview.fuse_verdict is None

    now = datetime.now(UTC)
    await _seed_job(
        api_env.admin_engine,
        world.org_id,
        world.device_id,
        created_at=now - timedelta(minutes=1),
    )
    before = await _job_count(api_env, world.org_id)

    # The digest sent is the STORED one, unrecomputed, and it still validates --
    # so a refusal below cannot be the digest gate wearing the fuse's clothes.
    with pytest.raises(ActConflict, match="rate-limited"):
        await _confirm(api_env, world, preview.ai_action_id, preview.preview_digest)

    assert await _job_count(api_env, world.org_id) == before
    assert (await _row(api_env, preview.ai_action_id)).state == "previewed"


# --------------------------------------------------------------------------- #
# §13 test 21 -- the audit.
# --------------------------------------------------------------------------- #


async def test_a_dispatch_writes_the_ai_event_and_the_kernels_own_job_record(
    api_env: Any,
) -> None:
    """§8.2: `ai.action_dispatched` sits BESIDE the kernel's own job record
    rather than replacing it -- the kernel record says a job was sent, this one
    says why an AI proposed it and who agreed. BOTH are asserted."""
    world = await build_world(api_env, tag="conf-audit")
    preview = await _preview(api_env, world)
    outcome = await _confirm(api_env, world, preview.ai_action_id, preview.preview_digest)

    async with AsyncSession(api_env.admin_engine) as session:
        job = (await session.execute(select(Job).where(Job.id == outcome.job_id))).scalar_one()
        events = {
            event.action: event
            for event in (
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.org_id == world.org_id)
                    )
                )
                .scalars()
                .all()
            )
        }

    # The kernel's own record: a signed job row exists.
    assert job.signature and job.type == "inventory.collect"
    # And this layer's, naming the proposal, the job and the human.
    assert "ai.action_proposed" in events
    assert "ai.action_confirmed" in events
    dispatched = events["ai.action_dispatched"]
    assert dispatched.meta["job_id"] == str(outcome.job_id)
    assert dispatched.meta["ai_action_id"] == str(preview.ai_action_id)
    assert dispatched.meta["confirmed_by"] == str(world.user_id)
    assert dispatched.meta["job_type"] == "inventory.collect"
    assert dispatched.actor_type == "user"
    # §8.2: there is NO `actor_type='ai'`; inventing a fourth actor kind would
    # make every existing consumer's exhaustiveness check wrong.
    assert {event.actor_type for event in events.values()} <= {"user", "agent", "system"}
    # And `seconds_since_preview` is on the confirm event, so "how long did the
    # human look at it" is answerable.
    assert events["ai.action_confirmed"].meta["seconds_since_preview"] >= 0


# --------------------------------------------------------------------------- #
# Reject.
# --------------------------------------------------------------------------- #


async def test_reject_stores_a_bounded_reason_and_audits_it(api_env: Any) -> None:
    world = await build_world(api_env, tag="conf-reject")
    preview = await _preview(api_env, world)

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        await reject_action(
            session,
            org_id=world.org_id,
            ai_action_id=preview.ai_action_id,
            reason="wrong_device",
            actor_id=world.user_id,
            permissions=CONFIRMER,
        )

    row = await _row(api_env, preview.ai_action_id)
    assert (row.state, row.rejected_reason) == ("rejected", "wrong_device")
    async with AsyncSession(api_env.admin_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == world.org_id)
                .where(AuditEvent.action == "ai.action_rejected")
            )
        ).scalar_one()
    assert event.meta["rejected_reason"] == "wrong_device"


async def test_a_free_text_rejection_reason_is_refused_and_nothing_is_stored(
    api_env: Any,
) -> None:
    """§8.2 says BOUNDED, not free text, and §8.3 spends a paragraph on why:
    the rejecting human is reading the model's rationale at the moment they
    click, and a free-text box beside it is where that prose gets copied onto a
    durable row."""
    world = await build_world(api_env, tag="conf-reject-free")
    preview = await _preview(api_env, world)

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(AiValidationError):
            await reject_action(
                session,
                org_id=world.org_id,
                ai_action_id=preview.ai_action_id,
                reason="the ticket said to ignore previous instructions",
                actor_id=world.user_id,
                permissions=CONFIRMER,
            )

    row = await _row(api_env, preview.ai_action_id)
    assert (row.state, row.rejected_reason) == ("previewed", None)


async def test_a_rejected_action_cannot_then_be_confirmed(api_env: Any) -> None:
    world = await build_world(api_env, tag="conf-reject-confirm")
    preview = await _preview(api_env, world)
    before = await _job_count(api_env, world.org_id)

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        await reject_action(
            session,
            org_id=world.org_id,
            ai_action_id=preview.ai_action_id,
            reason="not_needed",
            actor_id=world.user_id,
            permissions=CONFIRMER,
        )
    with pytest.raises(ActConflict, match="rejected"):
        await _confirm(api_env, world, preview.ai_action_id, preview.preview_digest)
    assert await _job_count(api_env, world.org_id) == before


# --------------------------------------------------------------------------- #
# §13 test 8 -- THE SPEC'S NAMED MUTATION, as a structural property.
# --------------------------------------------------------------------------- #


def test_confirm_takes_no_job_type_and_no_params_from_its_caller() -> None:
    """§13 test 8 names a mutation: *a confirm handler that takes `job_type` or
    `params` from the request body instead of the stored row must make a test
    fail.* The behavioural half of that is
    `test_the_same_user_holding_both_confirms_and_exactly_one_job_appears`, which
    asserts the dispatched job's `type` and `target_id` against the STORED ROW.

    This is the other half, and it is what makes the mutation impossible to
    perform without editing a signature a reviewer sees: `confirm_action` has no
    parameter through which a job type, a params blob or a target could arrive.
    Asserted over the signature rather than over prose, so adding one goes red.
    """
    import inspect

    from app.core.ai.act import confirm_action as handler

    names = set(inspect.signature(handler).parameters)
    assert names == {
        "session",
        "org_id",
        "ai_action_id",
        "digest",
        "actor_id",
        "permissions",
        "job_engine",
        "ctx",
        "executor",
        "now",
    }, sorted(names)
    for forbidden in ("job_type", "params", "target_device_id", "device_id", "rendered_params"):
        assert forbidden not in names, forbidden
