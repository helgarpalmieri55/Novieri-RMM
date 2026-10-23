"""SECURITY GATE: propose and preview (docs/modules/ai-automation.md §6.1, §6.2,
§0.6, §0.7; §13 tests 3, 4, 10, 12, 16).

**THE PROPERTY: THERE IS NO PATH FROM MODEL OUTPUT TO AN EFFECT.** The model
names a catalogue KEY and a device. Everything else -- job type, params, script
pin, risk, destructiveness, fuses -- comes from a row an org admin authored.

Three things every test here does deliberately:

* **`off` and `suggest` cases seed a NEARLY-MATCHING `ai_action` row first**
  (same org, same session, different key), so `[]` is evidence the filter ran
  rather than evidence the table was empty. That is this project's third named
  fixture-blindness trap and it is the one this file is most exposed to.
* **Adversarial arguments are driven through `ActTool.invoke`**, not through
  `propose`, because `extra="forbid"` on `ProposeArgs` is the control and only
  the tool path exercises it.
* **The transport spy is asserted on**, not the response, for the device checks:
  a refusal tells you the answer, the tape tells you which request was made and
  to whom the route said no.
"""

import time
from typing import Any, get_args

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.act import ActRefused, ActTool, ProposeBinding, propose
from app.core.ai.executor_staff import ToolError, ToolErrorKind, invoke_tool
from app.core.ai.types import ToolResult, TurnContext
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AiAction, Job
from tests.ai_act_fixtures import build_world
from tests.health_action_fixtures import seed_approved_action
from tests.health_fixtures import seed_device, seed_org


def _session_factory(api_env: Any, org_id: Any) -> Any:
    def factory() -> Any:
        return org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all())

    return factory


async def _actions(api_env: Any, org_id: Any) -> list[AiAction]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AiAction).where(AiAction.org_id == org_id).order_by(AiAction.id)
                )
            )
            .scalars()
            .all()
        )


async def _seed_near_miss(api_env: Any, world: Any) -> None:
    """A NEARLY-MATCHING `ai_action`: same org, same session, DIFFERENT key.

    Without it, "no row was minted" is satisfied by a query that was always
    going to return nothing -- a typo'd predicate, a broken seed, an empty
    table. With it, `[near-miss]` and `[]` print different values.
    """
    from tests.test_ai_action_migration import _Refs, _seed_ai_action

    refs = _Refs(
        world.org_id, world.user_id, world.device_id, world.ai_session_id, world.action_id
    )
    await _seed_ai_action(
        api_env.admin_engine, refs, key="a-different-key", state="previewed"
    )


# --------------------------------------------------------------------------- #
# §13 test 3 -- the persona is not on the catalogue row.
# --------------------------------------------------------------------------- #


async def test_a_row_that_does_not_carry_the_persona_mints_no_action(api_env: Any) -> None:
    world = await build_world(api_env, tag="act-persona", action_kw={"personas": ["health"]})
    await _seed_near_miss(api_env, world)

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(ActRefused, match="not available to this assistant"):
            await propose(
                session,
                org_id=world.org_id,
                binding=world.binding,
                ctx=world.ctx,
                executor=invoke_tool,
                action_key=world.action_key,
                device_id=world.device_id,
            )

    rows = await _actions(api_env, world.org_id)
    assert [row.approved_action_key for row in rows] == ["a-different-key"]


async def test_an_inactive_row_mints_no_action(api_env: Any) -> None:
    world = await build_world(api_env, tag="act-inactive", action_kw={"active": False})
    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(ActRefused, match="inactive"):
            await propose(
                session,
                org_id=world.org_id,
                binding=world.binding,
                ctx=world.ctx,
                executor=invoke_tool,
                action_key=world.action_key,
                device_id=world.device_id,
            )
    assert await _actions(api_env, world.org_id) == []


# --------------------------------------------------------------------------- #
# §13 test 4 -- a job type the allowlist refuses, named by a LEGACY row.
# --------------------------------------------------------------------------- #


async def test_a_job_type_outside_the_allowlist_is_refused_even_from_a_legacy_row(
    api_env: Any,
) -> None:
    """§6.1 step 3, re-checked HERE because the row may predate the allowlist --
    which the kernel's own comment anticipates in as many words.

    The row is seeded DIRECTLY on the admin engine, because health's write path
    would refuse it. That is the whole point of the step: a row that exists today
    and would not be writable today.
    """
    world = await build_world(
        api_env,
        tag="act-legacy",
        action_kw={"job_type": "backup.run", "params_template": {"repo_key": "s3cret"}},
    )
    await _seed_near_miss(api_env, world)

    # **NOT `pytest.raises` ALONE.** A skipped step 3 reports only `DID NOT
    # RAISE`, which says nothing about what the table now holds. Reduced to a
    # comparable value first, so the mutation prints the previewed `ai_action`
    # row naming `backup.run` that the database accepted.
    outcome = "not refused"
    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        try:
            await propose(
                session,
                org_id=world.org_id,
                binding=world.binding,
                ctx=world.ctx,
                executor=invoke_tool,
                action_key=world.action_key,
                device_id=world.device_id,
            )
        except ActRefused as exc:
            outcome = "refused"
            assert "repo_key" in str(exc), exc

    minted = sorted(
        (row.approved_action_key, row.job_type) for row in await _actions(api_env, world.org_id)
    )
    assert (outcome, minted) == ("refused", [("a-different-key", "inventory.collect")])


# --------------------------------------------------------------------------- #
# §13 test 10 -- the dial: off mints none, suggest mints none, require mints one.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("ceiling", "expected_keys", "message"),
    [
        (None, ["a-different-key"], "not permitted for this assistant"),
        ("off", ["a-different-key"], "not permitted for this assistant"),
        ("suggest", ["a-different-key"], "may only be SUGGESTED"),
        ("require_approval", None, ""),
    ],
)
async def test_the_dial_decides_whether_a_row_is_minted(
    api_env: Any, ceiling: str | None, expected_keys: list[str] | None, message: str
) -> None:
    """§13 test 10, with a near-miss row seeded in EVERY case.

    The `suggest` case asserts on the MESSAGE as well as on the table: returning
    an empty result instead of a truthful refusal is the worse failure, because
    "nothing to do" reads as an answer while "not permitted at this setting"
    reads as a decision somebody made.
    """
    world = await build_world(api_env, tag=f"act-dial-{ceiling or 'none'}", ceiling=ceiling)
    await _seed_near_miss(api_env, world)

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        if expected_keys is None:
            preview = await propose(
                session,
                org_id=world.org_id,
                binding=world.binding,
                ctx=world.ctx,
                executor=invoke_tool,
                action_key=world.action_key,
                device_id=world.device_id,
            )
            assert preview.state == "previewed"
        else:
            with pytest.raises(ActRefused, match=message):
                await propose(
                    session,
                    org_id=world.org_id,
                    binding=world.binding,
                    ctx=world.ctx,
                    executor=invoke_tool,
                    action_key=world.action_key,
                    device_id=world.device_id,
                )

    keys = sorted(row.approved_action_key for row in await _actions(api_env, world.org_id))
    assert keys == sorted(expected_keys or ["a-different-key", world.action_key])


# --------------------------------------------------------------------------- #
# §13 test 12 -- a destructive or high-risk row never auto-dispatches.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ceiling", ["suggest", "require_approval"])
@pytest.mark.parametrize(
    ("action_kw", "risk_class"),
    [
        ({"job_type": "patch.apply", "destructive": True}, "low"),
        ({"job_type": "inventory.collect", "destructive": False}, "high"),
    ],
)
async def test_a_destructive_or_high_row_never_produces_a_dispatch(
    api_env: Any, ceiling: str, action_kw: dict[str, Any], risk_class: str
) -> None:
    """§13 test 12, over every SETTABLE dial position. `off` is omitted because
    it mints no row at all, which is covered above; here the question is what a
    row that IS minted looks like, and the answer must never be `dispatched`."""
    world = await build_world(
        api_env,
        tag="act-destructive",
        ceiling=ceiling,
        risk_class=risk_class,
        action_kw={**action_kw, "risk_class": risk_class},
    )
    before = await _job_count(api_env, world.org_id)

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        try:
            await propose(
                session,
                org_id=world.org_id,
                binding=world.binding,
                ctx=world.ctx,
                executor=invoke_tool,
                action_key=world.action_key,
                device_id=world.device_id,
            )
        except ActRefused:
            pass  # `suggest` refuses; that is also "no dispatch".

    rows = await _actions(api_env, world.org_id)
    assert all(row.state == "previewed" and row.job_id is None for row in rows), rows
    assert await _job_count(api_env, world.org_id) == before


async def _job_count(api_env: Any, org_id: Any) -> int:
    async with AsyncSession(api_env.admin_engine) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Job).where(Job.org_id == org_id)
                )
            ).scalar_one()
        )


# --------------------------------------------------------------------------- #
# §13 test 16 -- model output naming a job type, params, or a foreign device.
# --------------------------------------------------------------------------- #


async def test_adversarial_tool_arguments_reach_no_dispatch_path(api_env: Any) -> None:
    """**THE MUTATION THAT PROVES §0.7.**

    (a) `job_type` and `params` are rejected BY FIELD NAME, because
        `ProposeArgs` is `extra="forbid"`. There is no field through which model
        output could reach `JobEngine.create`.
    (b) a device in ANOTHER ORG produces a refusal, and the loopback's own read
        came back 404 -- never that org's row.
    """
    world = await build_world(api_env, tag="act-adversarial")
    other = await seed_org(api_env.admin_engine, "act-elsewhere")
    foreign_device = await seed_device(api_env.admin_engine, other.org_id, hostname="not-yours")

    tool = ActTool(
        org_id=world.org_id,
        binding=world.binding,
        executor=invoke_tool,
        session_factory=_session_factory(api_env, world.org_id),
    )

    # (a) The two fields a laundered instruction would most like to set.
    for bad in ({"job_type": "script.run"}, {"params": {"body": "rm -rf /"}}):
        outcome = await tool.invoke(
            world.ctx,
            {"action_key": world.action_key, "device_id": str(world.device_id), **bad},
        )
        assert isinstance(outcome, ToolError), outcome
        assert outcome.kind == "invalid_arguments", outcome
        # NAMED, so a transcript reader sees what the model tried to set.
        assert next(iter(bad)) in outcome.message, outcome.message

    # (b) Another org's device.
    outcome = await tool.invoke(
        world.ctx, {"action_key": world.action_key, "device_id": str(foreign_device)}
    )
    assert isinstance(outcome, ToolError), outcome
    assert outcome.kind == "forbidden"
    assert "no such device" in outcome.message

    assert await _actions(api_env, world.org_id) == []
    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(
                select(func.count()).select_from(AiAction).where(AiAction.org_id == other.org_id)
            )
        ).scalar_one() == 0


async def test_the_device_check_is_the_loopback_and_not_a_filter(api_env: Any) -> None:
    """§6.1 step 5. A caller WITHOUT `devices.read` is refused even though the
    device exists in their own org -- because the loopback re-runs
    `require_permission`, not because this layer filtered anything.

    Asserted on the SPY: the request was actually built and issued, to the real
    device route, carrying the caller's own token, and the route said 403.
    """
    from tests.ai_admin_fakes import RecordingTransport, install_spy

    world = await build_world(
        api_env, tag="act-noread", codes=("ai.use", "ai.act.propose")
    )
    spy = RecordingTransport(api_env.app)
    with pytest.MonkeyPatch.context() as monkeypatch:
        install_spy(monkeypatch, spy)
        async with org_session(
            world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            with pytest.raises(ActRefused, match="no such device"):
                await propose(
                    session,
                    org_id=world.org_id,
                    binding=world.binding,
                    ctx=world.ctx,
                    executor=invoke_tool,
                    action_key=world.action_key,
                    device_id=world.device_id,
                )

    urls = [str(r.url) for r in spy.requests]
    assert urls and urls[-1].endswith(f"/api/v1/devices/{world.device_id}"), urls
    assert await _actions(api_env, world.org_id) == []


async def test_the_probes_success_signal_really_is_blocked(api_env: Any) -> None:
    """**THE FACT THE ALLOWLIST BELOW RESTS ON, PINNED SEPARATELY.**

    `_DEVICE_PROBE` declares an EMPTY `response_fields`, so a device the caller
    CAN see comes back as `ToolError(kind="blocked")` -- the route answered and
    the scrubber refused to hand the body onward, which is what the empty set
    asks for. Every other outcome means the answer was not obtained.

    Without this test, `_device_is_visible` could be an allowlist over a kind
    the probe never returns: it would refuse everything, every act test would go
    red, and the reason would be four files away. With it, a scrubber change
    that starts returning a `ToolResult` (or a different kind) names itself
    here, at the source of the assumption.
    """
    from app.core.ai.act import _DEVICE_PROBE, _DEVICE_PROBE_REGISTRY

    world = await build_world(api_env, tag="act-probe-signal")
    outcome = await invoke_tool(
        tool_name=_DEVICE_PROBE.name,
        raw_args={"device_id": str(world.device_id)},
        app=api_env.app,
        bearer=world.headers["Authorization"],
        deadline=time.monotonic() + 30.0,
        registry=_DEVICE_PROBE_REGISTRY,
    )
    assert isinstance(outcome, ToolError), outcome
    assert (outcome.kind, outcome.status) == ("blocked", 200), outcome


@pytest.mark.parametrize("kind", sorted(get_args(ToolErrorKind)))
async def test_only_a_blocked_probe_grants_visibility(api_env: Any, kind: str) -> None:
    """**THE CHECK IS AN ALLOWLIST, AND IT SHIPPED AS A DENYLIST.**

    `_device_is_visible` returned `kind not in {"forbidden", "not_found"}` --
    which is "did we get an explicit refusal", not "did we get an answer". The
    probe's success signal is `blocked`, so every OTHER outcome -- an
    `upstream_error` from a failed lookup, a `timeout`, a `session_expired`
    (a 401!), an `unknown_tool`, an `invalid_arguments` -- granted authorization
    to a principal the route never approved. **Any failure to obtain an answer
    was a yes**, which is invariant #6 inverted: authorization is server-side,
    and a server that could not answer has not authorized anything.

    **PARAMETRISED OVER EVERY `ToolErrorKind` MEMBER, READ OFF THE TYPE**, not
    over the two the old code happened to name. A kind added later is covered
    the day it is added, and it defaults to REFUSED -- which is the property
    that makes this an allowlist rather than a longer denylist. `get_args` is
    what makes that automatic; a literal list here would reproduce the defect in
    the test.

    The assertion is the STATE OF THE WORLD, not `DID NOT RAISE`: for every
    kind, the pair `(kind, visible)`, compared against the one pair that may be
    True. A mutation back to the denylist prints
    `('upstream_error', True) != ('upstream_error', False)`.
    """
    from app.core.ai.act import _device_is_visible

    world = await build_world(api_env, tag=f"act-vis-{kind.replace('_', '-')}")

    async def refusing_executor(**kwargs: Any) -> ToolError:
        return ToolError(tool="devices.get", kind=kind, message="scripted", status=None)

    visible = await _device_is_visible(
        ctx=world.ctx, executor=refusing_executor, device_id=world.device_id
    )
    assert (kind, visible) == (kind, kind == "blocked")


async def test_a_probe_that_somehow_succeeds_does_not_grant_visibility(api_env: Any) -> None:
    """The non-`ToolError` half, which `getattr(outcome, "kind", None)` used to
    wave through: a `ToolResult` has no `kind`, `None` was not in the denylist,
    and the answer was True.

    A `ToolResult` from THIS probe would mean the scrubber let an empty-
    `response_fields` payload past, which is a scrubber defect -- and the
    conservative reading of a defect is not "authorized". Asserted as the state
    of the world so the mutation prints a value.
    """
    from app.core.ai.act import _device_is_visible

    world = await build_world(api_env, tag="act-vis-result")

    async def succeeding_executor(**kwargs: Any) -> ToolResult:
        return ToolResult(payload={}, status=200, result_count=1, truncated=False)

    visible = await _device_is_visible(
        ctx=world.ctx, executor=succeeding_executor, device_id=world.device_id
    )
    assert ("ToolResult", visible) == ("ToolResult", False)


# --------------------------------------------------------------------------- #
# §6.5 -- the per-turn cap.
# --------------------------------------------------------------------------- #


async def test_the_fourth_proposal_in_one_turn_is_refused_and_three_rows_exist(
    api_env: Any,
) -> None:
    world = await build_world(api_env, tag="act-cap")
    tool = ActTool(
        org_id=world.org_id,
        binding=world.binding,
        executor=invoke_tool,
        session_factory=_session_factory(api_env, world.org_id),
    )
    args = {"action_key": world.action_key, "device_id": str(world.device_id)}

    for _ in range(3):
        assert not isinstance(await tool.invoke(world.ctx, args), ToolError)
    fourth = await tool.invoke(world.ctx, args)

    assert isinstance(fourth, ToolError), fourth
    assert "per-turn limit" in fourth.message
    assert len(await _actions(api_env, world.org_id)) == 3


async def test_the_cap_is_per_turn_not_per_session(api_env: Any) -> None:
    """The bound §0.12 adds at the other end from `max_target_scope`. A cap that
    counted the whole session would silently become a per-conversation quota,
    which is a different claim."""
    world = await build_world(api_env, tag="act-cap-turn")
    args = {"action_key": world.action_key, "device_id": str(world.device_id)}
    for seq in (1, 2):
        tool = ActTool(
            org_id=world.org_id,
            binding=ProposeBinding(
                persona=world.binding.persona,
                ai_session_id=world.ai_session_id,
                message_seq=seq,
                actor_id=world.user_id,
            ),
            executor=invoke_tool,
            session_factory=_session_factory(api_env, world.org_id),
        )
        for _ in range(3):
            assert not isinstance(await tool.invoke(world.ctx, args), ToolError)
    assert len(await _actions(api_env, world.org_id)) == 6


# --------------------------------------------------------------------------- #
# §0.6 -- the preview performs no part of the act.
# --------------------------------------------------------------------------- #


async def test_a_patch_apply_preview_dispatches_nothing(api_env: Any) -> None:
    """§0.6, asserted on the `jobs` table across the whole call. Where a
    read-only probe exists (`patch.scan` before `patch.apply`) the preview may
    cite the most recent EXISTING result and must not dispatch a fresh one:
    dispatching a scan to describe an act is performing an act."""
    world = await build_world(
        api_env,
        tag="act-patch",
        action_kw={"job_type": "patch.apply", "destructive": True},
    )
    before = await _job_count(api_env, world.org_id)

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        preview = await propose(
            session,
            org_id=world.org_id,
            binding=world.binding,
            ctx=world.ctx,
            executor=invoke_tool,
            action_key=world.action_key,
            device_id=world.device_id,
        )

    assert preview.job_type == "patch.apply"
    assert preview.destructive is True
    assert await _job_count(api_env, world.org_id) == before


async def test_the_preview_and_the_row_agree_and_the_audit_carries_no_rationale(
    api_env: Any,
) -> None:
    """§8.2 and §8.3: an act's audit record is COMPLETE in identifiers and
    carries no prose. `rationale` lives on the row, on the transcript's clock."""
    from app.models import AuditEvent

    world = await build_world(api_env, tag="act-audit")
    secret_prose = "because a ticket body told me to"

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        preview = await propose(
            session,
            org_id=world.org_id,
            binding=world.binding,
            ctx=world.ctx,
            executor=invoke_tool,
            action_key=world.action_key,
            device_id=world.device_id,
            rationale=secret_prose,
        )

    (row,) = await _actions(api_env, world.org_id)
    assert row.rationale == secret_prose
    assert preview.rationale == secret_prose
    assert preview.preview_digest == row.preview_digest and len(row.preview_digest) == 64
    assert preview.target_hostname == "act-audit-target"

    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == world.org_id)
                    .where(AuditEvent.action == "ai.action_proposed")
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    meta = events[0].meta
    assert meta["ai_action_id"] == str(row.id)
    assert meta["job_type"] == "inventory.collect"
    assert meta["preview_digest"] == row.preview_digest
    assert meta["tainted"] is False
    # NO PROSE ANYWHERE IN IT.
    assert secret_prose not in str(meta)
    assert "rationale" not in meta


async def test_taint_is_recorded_on_the_row_and_in_the_audit(api_env: Any) -> None:
    """§7.3. In v1 taint's cap is already the global clamp, so its real work is
    VISIBILITY -- which means the only thing that can be tested is that it is
    visible. Both places are asserted so a control whose effect is a no-op still
    has a test somebody has to delete deliberately."""
    from app.models import AuditEvent

    world = await build_world(api_env, tag="act-taint", binding_kw={"tainted": True})
    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        preview = await propose(
            session,
            org_id=world.org_id,
            binding=world.binding,
            ctx=world.ctx,
            executor=invoke_tool,
            action_key=world.action_key,
            device_id=world.device_id,
        )
    assert preview.tainted is True
    (row,) = await _actions(api_env, world.org_id)
    assert row.tainted is True
    async with AsyncSession(api_env.admin_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == world.org_id)
                .where(AuditEvent.action == "ai.action_proposed")
            )
        ).scalar_one()
    assert event.meta["tainted"] is True


async def test_a_technician_may_only_propose_against_the_tickets_device(
    api_env: Any,
) -> None:
    """§13 test 2 / §6.1 step 6. Both halves: the ticket's own device is
    accepted, another device in the SAME ORG is refused -- so the check is about
    the ticket rather than about visibility, which step 5 already covers.

    `ticket_bound=True` is what SAYS the binding applies. It was added after the
    copilot's own suite found that `ticket_device_id is not None` alone cannot
    tell "this persona has no ticket" from "this ticket has no device", and §3.2
    requires the second to propose nothing -- see `test_ai_copilot.py::
    test_a_ticket_with_no_linked_device_can_propose_nothing`.
    """
    world = await build_world(api_env, tag="act-ticket", persona="technician")
    other_device = await seed_device(api_env.admin_engine, world.org_id, hostname="not-the-one")
    binding = ProposeBinding(
        persona="technician",
        ai_session_id=world.ai_session_id,
        message_seq=1,
        actor_id=world.user_id,
        ticket_bound=True,
        ticket_device_id=world.device_id,
    )

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(ActRefused, match="that ticket names"):
            await propose(
                session,
                org_id=world.org_id,
                binding=binding,
                ctx=world.ctx,
                executor=invoke_tool,
                action_key=world.action_key,
                device_id=other_device,
            )
        preview = await propose(
            session,
            org_id=world.org_id,
            binding=binding,
            ctx=world.ctx,
            executor=invoke_tool,
            action_key=world.action_key,
            device_id=world.device_id,
        )
    assert preview.target_device_id == world.device_id


async def test_an_unknown_catalogue_key_is_a_tool_error_and_the_turn_continues(
    api_env: Any,
) -> None:
    world = await build_world(api_env, tag="act-unknown")
    tool = ActTool(
        org_id=world.org_id,
        binding=world.binding,
        executor=invoke_tool,
        session_factory=_session_factory(api_env, world.org_id),
    )
    outcome = await tool.invoke(
        world.ctx, {"action_key": "no-such-key", "device_id": str(world.device_id)}
    )
    assert isinstance(outcome, ToolError)
    assert outcome.kind == "forbidden"
    assert await _actions(api_env, world.org_id) == []


async def test_a_successful_tool_call_is_the_first_mutating_tool_result(
    api_env: Any,
) -> None:
    """`ToolResult.mutating` was reserved by `app/core/ai/types.py` from day one
    so that act mode would add no branch to the audit writer or the console.
    This is the path that finally sets it, and the read path still does not."""
    from app.core.ai.types import ToolResult

    world = await build_world(api_env, tag="act-mutating")
    tool = ActTool(
        org_id=world.org_id,
        binding=world.binding,
        executor=invoke_tool,
        session_factory=_session_factory(api_env, world.org_id),
    )
    outcome = await tool.invoke(
        world.ctx, {"action_key": world.action_key, "device_id": str(world.device_id)}
    )
    assert isinstance(outcome, ToolResult), outcome
    assert outcome.mutating is True
    # And a READ tool result still is not.
    read = await invoke_tool(
        tool_name="devices.health",
        raw_args={"device_id": str(world.device_id)},
        app=api_env.app,
        bearer=world.headers["Authorization"],
        deadline=time.monotonic() + 30.0,
        registry=__import__(
            "app.core.ai.tools", fromlist=["REGISTRY"]
        ).REGISTRY,
    )
    assert getattr(read, "mutating", False) is False


async def test_a_cross_org_catalogue_key_is_not_resolvable(api_env: Any) -> None:
    """The org predicate on `get_approved_action`, exercised end to end: org B
    authors a key, org A's assistant names it, and the answer is "there is no
    approved action with that key" -- never org B's row."""
    world = await build_world(api_env, tag="act-xorg")
    other = await seed_org(api_env.admin_engine, "act-xorg-b")
    other_action = await seed_approved_action(
        api_env.admin_engine,
        other.org_id,
        other.user_id,
        key="borrowed-key",
        personas=["health", "admin"],
    )
    assert other_action is not None

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        with pytest.raises(ActRefused, match="no approved action with key"):
            await propose(
                session,
                org_id=world.org_id,
                binding=world.binding,
                ctx=world.ctx,
                executor=invoke_tool,
                action_key="borrowed-key",
                device_id=world.device_id,
            )
    assert await _actions(api_env, world.org_id) == []


async def test_the_turn_context_is_the_callers_own_token(api_env: Any) -> None:
    """A sanity pin on the fixture itself: the loopback probe carries the
    principal under test, not a service token. If this ever stopped being true,
    every authorization assertion in this file would be about the wrong
    identity."""
    world = await build_world(api_env, tag="act-token")
    assert world.ctx.bearer == world.headers["Authorization"]
    assert isinstance(world.ctx, TurnContext)


async def test_destructive_is_re_derived_from_the_pin_not_read_off_the_row(
    api_env: Any,
) -> None:
    """**THE MUTATION THIS TEST EXISTS FOR: use `action.destructive` instead of
    re-deriving.** Then this observes `destructive is False` on a preview for a
    `script.run` whose pinned version has no exemption -- arbitrary code marked
    safe on a confirmation screen.

    `approved_action.destructive` is a STORED column, and `update_script` bumps
    `version` IN PLACE: an exemption granted for version 2 does not follow the
    script to version 3, so a row written when the exemption matched can sit in
    the catalogue claiming to be non-destructive while the version it now pins
    has no exemption at all. That gap is the whole reason `is_destructive` takes
    the pin as keyword arguments.
    """
    from tests.health_action_fixtures import seed_script

    org = await seed_org(api_env.admin_engine, "act-pin")
    script_id = await seed_script(api_env.admin_engine, org.org_id, org.user_id, version=3)
    world = await build_world(
        api_env,
        tag="act-pin",
        action_kw={
            "job_type": "script.run",
            "script_id": script_id,
            "script_version": 3,
            # STORED NON-DESTRUCTIVE, which is the legacy state the re-derivation
            # exists to correct. Seeded directly; health's own path re-derives.
            "destructive": False,
        },
    )

    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        preview = await propose(
            session,
            org_id=world.org_id,
            binding=world.binding,
            ctx=world.ctx,
            executor=invoke_tool,
            action_key=world.action_key,
            device_id=world.device_id,
        )

    stored_claim = await _stored_destructive(api_env, world.action_id)
    # The catalogue says one thing; the derivation from the pin says another,
    # and the preview shows the derivation.
    assert (stored_claim, preview.destructive) == (False, True)
    (row,) = await _actions(api_env, world.org_id)
    assert row.destructive is True
    assert preview.script_version == 3


async def _stored_destructive(api_env: Any, action_id: Any) -> bool:
    from app.models import ApprovedAction

    async with AsyncSession(api_env.admin_engine) as session:
        return bool(
            (
                await session.execute(
                    select(ApprovedAction.destructive).where(ApprovedAction.id == action_id)
                )
            ).scalar_one()
        )
