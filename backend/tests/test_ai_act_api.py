"""The admin persona's act surface, over HTTP (docs/modules/ai-automation.md
§6, §9.2, §12).

The service-layer proofs live in `test_ai_act_propose.py`,
`test_ai_act_confirm.py` and `test_ai_act_confirm_authz.py`. **THIS FILE IS ABOUT
THE ROUTES**: that the whole propose -> preview -> confirm -> dispatch path works
through the surface a human actually uses, that each gate is the one §12 names,
and that the two things a route could quietly add -- a job type in a request
body, an effect built in a handler -- are not there.

**THE ROUND TRIP ASSERTS THE JOB AND THE FIVE AUDIT EVENTS, NOT THE STATUS.**
A 200 says the handler returned; the `jobs` row and the audit chain say what
happened.
"""

import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers

from app.core.ai.providers import TextBlock, ToolCallBlock
from app.models import AiAction, AiSettings, AuditEvent, Job
from app.models.ai import AI_ACTION_REJECTION_REASONS
from app.modules.ai_admin import api as api_module
from app.modules.ai_admin.profile import ADMIN_PROFILE
from app.modules.ai_admin.schema import RejectIn
from tests.ai_act_fixtures import build_world
from tests.ai_admin_fakes import ScriptedProvider, ai_user

AI_PREFIX = "/api/v1/ai"

#: What a human needs to run `inventory.collect` by hand, per
#: `app/jobs/dispatch_permissions.py`. §9.2's second part means the confirming
#: principal must hold it too.
DISPATCH_GATE = "jobs.dispatch"


async def _enable_ai(api_env: Any, org_id: UUID) -> None:
    """Give this org an `ai_settings` row.

    **IT NO LONGER ENABLES ANYTHING, AND THE NAME IS KEPT FOR THE CALL SITES.**
    Before spec §0.14 (the operator's decision of 2026-08-25) this set
    `enabled = true` and `provider_key = 'local'`, and those two writes were the
    whole of "this org may use the assistant". Both columns are dropped by
    migration `0074`: the deployment's `ITS_AI_*` configuration is the only
    switch, and `tests/conftest.py::ai_provider_configured` is where this suite
    declares it has one. What remains is the row itself, holding the cost and
    retention dials at their defaults -- which is what a test that later edits
    one of them needs.
    """
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        row = await session.get(AiSettings, org_id)
        if row is None:
            row = AiSettings(org_id=org_id)
            session.add(row)
        await session.commit()


async def _counts(api_env: Any, org_id: UUID) -> dict[str, int]:
    async with AsyncSession(api_env.admin_engine) as session:
        jobs = (
            await session.execute(
                select(func.count()).select_from(Job).where(Job.org_id == org_id)
            )
        ).scalar_one()
        events = list(
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.org_id == org_id)
                )
            ).scalars()
        )
    counted = {"jobs": int(jobs)}
    for action in events:
        counted[action] = counted.get(action, 0) + 1
    return counted


async def _act_world(api_env: Any, tag: str, *codes: str) -> Any:
    """`build_world` plus the things an HTTP round trip needs: AI enabled, and a
    principal holding exactly `codes`."""
    world = await build_world(api_env, tag=tag, codes=codes)
    await _enable_ai(api_env, world.org_id)
    return world


# --------------------------------------------------------------------------- #
# 1. The round trip.
# --------------------------------------------------------------------------- #


async def test_propose_preview_confirm_dispatch_over_http(
    api_env: Any, monkeypatch: Any
) -> None:
    """**ONE JOB, FIVE EVENTS, AND THE MODEL NAMED ONLY A CATALOGUE KEY.**

    The model emits `actions.propose` with a key and a device -- the only act
    arguments any model may ever emit (§0.7) -- and the turn ends with a
    PREVIEW, not an effect. A human then confirms by id and digest, carrying no
    job type, no params and no target, and exactly one signed job appears with
    `requested_by` set to that human, because a human authorized it.
    """
    world = await _act_world(
        api_env,
        "acthttp",
        "ai.use",
        "ai.act.propose",
        "ai.act.confirm",
        "devices.read",
        DISPATCH_GATE,
    )
    before = await _counts(api_env, world.org_id)

    provider = ScriptedProvider(
        [
            [
                ToolCallBlock(
                    id="1",
                    name="actions.propose",
                    arguments={
                        "action_key": world.action_key,
                        "device_id": str(world.device_id),
                        "rationale": "the inventory is 40 days stale",
                    },
                )
            ],
            [TextBlock(text="I have prepared an inventory collection for a human to confirm.")],
        ]
    )
    monkeypatch.setattr(api_module, "_provider_for", lambda: provider)

    created = await api_env.client.post(
        f"{AI_PREFIX}/sessions", headers=world.headers, json={"mode": "act"}
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    turn = await api_env.client.post(
        f"{AI_PREFIX}/sessions/{session_id}/turns",
        headers=world.headers,
        json={"question": "is anything stale on that machine?"},
    )
    assert turn.status_code == 200, turn.text
    assert turn.json()["stop_reason"] == "answer"

    # THE TURN DISPATCHED NOTHING. The preview performs no part of the act.
    mid = await _counts(api_env, world.org_id)
    assert mid["jobs"] == before["jobs"], "the proposal dispatched a job"
    assert mid.get("ai.action_proposed") == 1, mid

    listed = await api_env.client.get(f"{AI_PREFIX}/actions", headers=world.headers)
    assert listed.status_code == 200, listed.text
    (row,) = listed.json()
    assert (row["state"], row["job_type"], row["job_id"]) == (
        "previewed",
        "inventory.collect",
        None,
    )
    # §6.3: the list row carries no rationale -- a one-region row cannot separate
    # the model's argument from the catalogue's facts.
    assert "rationale" not in row, row

    preview = await api_env.client.get(
        f"{AI_PREFIX}/actions/{row['id']}", headers=world.headers
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["rationale"] == "the inventory is 40 days stale"
    assert body["job_type"] == "inventory.collect"
    assert body["target_hostname"] == "acthttp-target"

    confirmed = await api_env.client.post(
        f"{AI_PREFIX}/actions/{row['id']}/confirm",
        headers=world.headers,
        json={"digest": body["preview_digest"]},
    )
    assert confirmed.status_code == 200, confirmed.text

    after = await _counts(api_env, world.org_id)
    assert after["jobs"] == before["jobs"] + 1, after
    assert after.get("ai.action_confirmed") == 1, after
    assert after.get("ai.action_dispatched") == 1, after
    assert after.get("ai.session_started") == 1, after
    assert after.get("ai.query") == 1, after

    async with AsyncSession(api_env.admin_engine) as session:
        job = (
            await session.execute(select(Job).where(Job.id == UUID(confirmed.json()["job_id"])))
        ).scalar_one()
        action = await session.get(AiAction, UUID(row["id"]))
    assert action is not None
    assert (action.state, action.confirmed_by, action.job_id) == (
        "dispatched",
        world.user_id,
        job.id,
    )
    # THE HUMAN, because a human authorized it. There is no `actor_type='ai'`.
    assert job.requested_by == world.user_id
    assert job.signature, "the kernel signed it; nothing here did"


async def test_reject_writes_the_bounded_reason_and_no_job(
    api_env: Any, monkeypatch: Any
) -> None:
    world = await _act_world(
        api_env, "actrej", "ai.use", "ai.act.propose", "devices.read", DISPATCH_GATE
    )
    before = await _counts(api_env, world.org_id)
    action_id = await _propose_over_http(api_env, monkeypatch, world)

    response = await api_env.client.post(
        f"{AI_PREFIX}/actions/{action_id}/reject",
        headers=world.headers,
        json={"reason": "wrong_device"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "rejected"

    after = await _counts(api_env, world.org_id)
    assert after["jobs"] == before["jobs"]
    assert after.get("ai.action_rejected") == 1, after

    async with AsyncSession(api_env.admin_engine) as session:
        row = await session.get(AiAction, action_id)
    assert row is not None
    assert (row.state, row.rejected_reason, row.job_id) == ("rejected", "wrong_device", None)


async def test_a_free_text_rejection_reason_is_refused_by_the_schema(
    api_env: Any, monkeypatch: Any
) -> None:
    """§8.2: a BOUNDED ENUM, not free text. The rejecting human is reading the
    model's rationale at the moment they click, and a free-text box beside it is
    where that prose gets copied onto a durable row."""
    world = await _act_world(
        api_env, "actfree", "ai.use", "ai.act.propose", "devices.read", DISPATCH_GATE
    )
    action_id = await _propose_over_http(api_env, monkeypatch, world)

    response = await api_env.client.post(
        f"{AI_PREFIX}/actions/{action_id}/reject",
        headers=world.headers,
        json={"reason": "the model said the printer is on fire"},
    )
    async with AsyncSession(api_env.admin_engine) as session:
        row = await session.get(AiAction, action_id)
    assert row is not None
    assert (response.status_code, row.state, row.rejected_reason) == (422, "previewed", None)


def test_the_rejection_enum_and_the_stored_vocabulary_cannot_drift() -> None:
    """Two literals for one vocabulary is how a reason the schema accepts becomes
    a reason the service refuses -- a 400 nobody can explain from either file."""
    literal = RejectIn.model_fields["reason"].annotation
    assert set(getattr(literal, "__args__", ())) == set(AI_ACTION_REJECTION_REASONS)


# --------------------------------------------------------------------------- #
# 2. The gates.
# --------------------------------------------------------------------------- #


async def _propose_over_http(api_env: Any, monkeypatch: Any, world: Any) -> UUID:
    """One proposal through the real turn route, returning its id."""
    provider = ScriptedProvider(
        [
            [
                ToolCallBlock(
                    id="1",
                    name="actions.propose",
                    arguments={
                        "action_key": world.action_key,
                        "device_id": str(world.device_id),
                        "rationale": "because",
                    },
                )
            ],
            [TextBlock(text="prepared")],
        ]
    )
    monkeypatch.setattr(api_module, "_provider_for", lambda: provider)
    created = await api_env.client.post(
        f"{AI_PREFIX}/sessions", headers=world.headers, json={"mode": "act"}
    )
    assert created.status_code == 201, created.text
    turn = await api_env.client.post(
        f"{AI_PREFIX}/sessions/{created.json()['id']}/turns",
        headers=world.headers,
        json={"question": "anything stale?"},
    )
    assert turn.status_code == 200, turn.text
    async with AsyncSession(api_env.admin_engine) as session:
        rows = list(
            (
                await session.execute(
                    select(AiAction.id).where(AiAction.org_id == world.org_id)
                )
            ).scalars()
        )
    assert len(rows) == 1, f"expected one proposal, got {len(rows)}"
    return rows[0]


async def test_confirming_without_the_job_types_own_dispatch_permission_is_403(
    api_env: Any, monkeypatch: Any
) -> None:
    """**§13 TEST 1, THROUGH THE ROUTE.** The caller holds `ai.act.confirm` and
    NOT `jobs.dispatch`, which is what this job type's own kernel route requires.
    403, and the `jobs` table is unchanged -- "403" and "403 but it also
    dispatched" are different facts."""
    world = await _act_world(
        api_env,
        "actgate2",
        "ai.use",
        "ai.act.propose",
        "ai.act.confirm",
        "devices.read",
        DISPATCH_GATE,
    )
    action_id = await _propose_over_http(api_env, monkeypatch, world)
    async with AsyncSession(api_env.admin_engine) as session:
        digest = (await session.get(AiAction, action_id)).preview_digest  # type: ignore[union-attr]

    # A SECOND principal: `ai.act.confirm` and `devices.read`, but NOT
    # `jobs.dispatch`. Built with `ai_user` rather than a seeded role because
    # every seeded role is a bundle and cannot express this distinction.
    _, sub = await ai_user(
        api_env, world.org_id, "ai.use", "ai.act.confirm", "devices.read"
    )
    headers = await api_env.headers(world.org_id, sub)
    before = await _counts(api_env, world.org_id)

    response = await api_env.client.post(
        f"{AI_PREFIX}/actions/{action_id}/confirm", headers=headers, json={"digest": digest}
    )

    after = await _counts(api_env, world.org_id)
    async with AsyncSession(api_env.admin_engine) as session:
        row = await session.get(AiAction, action_id)
    assert row is not None
    assert (response.status_code, after["jobs"], row.state, row.job_id) == (
        403,
        before["jobs"],
        "previewed",
        None,
    ), response.text
    assert "jobs.dispatch" in response.json()["detail"], response.text


async def test_a_stale_digest_is_a_409_and_dispatches_nothing(
    api_env: Any, monkeypatch: Any
) -> None:
    world = await _act_world(
        api_env,
        "actstale",
        "ai.use",
        "ai.act.propose",
        "ai.act.confirm",
        "devices.read",
        DISPATCH_GATE,
    )
    action_id = await _propose_over_http(api_env, monkeypatch, world)
    before = await _counts(api_env, world.org_id)

    response = await api_env.client.post(
        f"{AI_PREFIX}/actions/{action_id}/confirm",
        headers=world.headers,
        json={"digest": "b" * 64},
    )
    after = await _counts(api_env, world.org_id)
    assert (response.status_code, after["jobs"]) == (409, before["jobs"]), response.text


async def test_a_second_confirm_is_a_409_and_does_not_duplicate_the_job(
    api_env: Any, monkeypatch: Any
) -> None:
    """The state machine makes a second confirm a conflict -- §0.5 refuses an
    idempotency key deliberately, so this is the mechanism that stands in for
    one, and the assertion is the JOB COUNT."""
    world = await _act_world(
        api_env,
        "acttwice",
        "ai.use",
        "ai.act.propose",
        "ai.act.confirm",
        "devices.read",
        DISPATCH_GATE,
    )
    action_id = await _propose_over_http(api_env, monkeypatch, world)
    async with AsyncSession(api_env.admin_engine) as session:
        digest = (await session.get(AiAction, action_id)).preview_digest  # type: ignore[union-attr]

    first = await api_env.client.post(
        f"{AI_PREFIX}/actions/{action_id}/confirm",
        headers=world.headers,
        json={"digest": digest},
    )
    assert first.status_code == 200, first.text
    once = await _counts(api_env, world.org_id)

    second = await api_env.client.post(
        f"{AI_PREFIX}/actions/{action_id}/confirm",
        headers=world.headers,
        json={"digest": digest},
    )
    twice = await _counts(api_env, world.org_id)
    assert (second.status_code, twice["jobs"]) == (409, once["jobs"]), second.text


async def test_the_confirm_body_accepts_a_digest_and_nothing_else(
    api_env: Any, monkeypatch: Any
) -> None:
    """**§13 TEST 8's PROPERTY, AT THE EDGE OF THE SYSTEM.** A client that sends a
    `job_type` or a `params` blob is told THAT FIELD DOES NOT EXIST, by name, and
    nothing is dispatched. There is no request field on this surface through
    which a caller could reach `JobEngine.create`."""
    world = await _act_world(
        api_env,
        "actbody",
        "ai.use",
        "ai.act.propose",
        "ai.act.confirm",
        "devices.read",
        DISPATCH_GATE,
    )
    action_id = await _propose_over_http(api_env, monkeypatch, world)
    async with AsyncSession(api_env.admin_engine) as session:
        digest = (await session.get(AiAction, action_id)).preview_digest  # type: ignore[union-attr]
    before = await _counts(api_env, world.org_id)

    response = await api_env.client.post(
        f"{AI_PREFIX}/actions/{action_id}/confirm",
        headers=world.headers,
        json={
            "digest": digest,
            "job_type": "script.run",
            "params": {"script_id": str(uuid4())},
            "target_device_id": str(uuid4()),
        },
    )
    after = await _counts(api_env, world.org_id)
    assert (response.status_code, after["jobs"]) == (422, before["jobs"]), response.text
    named = str(response.json())
    assert "job_type" in named and "params" in named, named


async def test_a_cross_org_action_id_is_a_404_not_another_orgs_row(
    api_env: Any, monkeypatch: Any
) -> None:
    """§13 test 17. A 403 here would confirm the id exists somewhere, which is a
    cross-tenant existence oracle over `ai_action.id`."""
    mine = await _act_world(
        api_env, "actorg-a", "ai.use", "ai.act.propose", "devices.read", DISPATCH_GATE
    )
    theirs = await _act_world(
        api_env,
        "actorg-b",
        "ai.use",
        "ai.act.propose",
        "ai.act.confirm",
        "devices.read",
        DISPATCH_GATE,
    )
    action_id = await _propose_over_http(api_env, monkeypatch, theirs)

    read = await api_env.client.get(f"{AI_PREFIX}/actions/{action_id}", headers=mine.headers)
    listed = await api_env.client.get(f"{AI_PREFIX}/actions", headers=mine.headers)
    assert read.status_code == 404, read.text
    assert listed.json() == [], listed.text


@pytest.mark.parametrize(
    ("reader_codes", "expected"),
    [
        (("ai.use", "devices.read"), 200),
        # `ai.use` and NOT `devices.read`: the proposal names a device, its
        # hostname and the approved action prepared against it.
        (("ai.use",), 403),
        # `devices.read` and NOT `ai.use`: the ROUTE refuses, so the two gates
        # are distinguishable and neither is carrying the other.
        (("devices.read",), 403),
    ],
    ids=["both", "ai-use-only", "devices-read-only"],
)
async def test_reading_a_proposal_needs_devices_read_as_well_as_ai_use(
    api_env: Any, monkeypatch: Any, reader_codes: tuple[str, ...], expected: int
) -> None:
    """**A PROPOSAL IS DEVICE-IDENTIFYING DATA, SO READING ONE COSTS WHAT
    READING A DEVICE COSTS** (§12 as amended by §19.19).

    `GET /ai/actions` and `GET /ai/actions/{id}` shipped gated on `ai.use`
    alone, org-wide. `ActionOut` carries `target_device_id`, `job_type` and
    `approved_action_key`; `ActionPreviewOut` adds the HOSTNAME and the rendered
    params. A principal who cannot read a device could therefore learn that it
    exists, what it is called, and which approved action somebody prepared
    against it -- from a queue that lists every persona's proposals in the org.

    `devices.read` is exactly the right second gate because device visibility in
    this platform IS org plus that code: it is what the confirm path's loopback
    probe re-runs (`_device_is_visible`), and what `devices.health` is gated on.

    **THE THIRD CASE IS THE ONE THAT MAKES THE FIRST TWO MEAN ANYTHING.** With
    only "both -> 200" and "ai.use only -> 403", a handler that had simply
    started refusing everyone would pass. Holding `devices.read` and NOT
    `ai.use` must ALSO be 403, and the 200 case proves the pair together still
    works -- and returns the row, which is the observable the ids assert.
    """
    owner = await _act_world(
        api_env, f"actgate-{'-'.join(c.split('.')[-1] for c in reader_codes)}",
        "ai.use", "ai.act.propose", "devices.read", DISPATCH_GATE,
    )
    action_id = await _propose_over_http(api_env, monkeypatch, owner)

    _, sub = await ai_user(api_env, owner.org_id, *reader_codes)
    headers = await api_env.headers(owner.org_id, sub)

    listed = await api_env.client.get(f"{AI_PREFIX}/actions", headers=headers)
    read = await api_env.client.get(f"{AI_PREFIX}/actions/{action_id}", headers=headers)

    # The state of the world, not "it raised": the two statuses AND what the
    # list actually handed over. A 403 that still leaked the body would print
    # the id here.
    ids = [row["id"] for row in listed.json()] if listed.status_code == 200 else []
    assert (listed.status_code, read.status_code, ids) == (
        expected,
        expected,
        [str(action_id)] if expected == 200 else [],
    ), (listed.text, read.text)


# --------------------------------------------------------------------------- #
# 3. The act tool is offered only when all three conditions hold.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mode", "codes", "offered"),
    [
        ("act", ("ai.use", "ai.act.propose", "devices.read"), True),
        ("read", ("ai.use", "ai.act.propose", "devices.read"), False),
        ("act", ("ai.use", "devices.read"), False),
    ],
    ids=["act-session-with-grant", "read-session", "act-session-without-grant"],
)
async def test_the_act_tool_is_offered_only_to_an_act_session_holding_the_grant(
    api_env: Any, monkeypatch: Any, mode: str, codes: tuple[str, ...], offered: bool
) -> None:
    """**THREE CONDITIONS, THREE CASES, TWO DIFFERENT OBSERVED VALUES.**

    The assertion is on the TOOL LIST THE PROVIDER WAS HANDED. A test that only
    checked "no row was minted" would pass against a build that offers the tool
    to a read session and refuses the proposal downstream -- and offering it is
    already the failure: it tells a model downstream of hostile text that
    proposing is a thing it can try.

    The third case cannot use `POST /ai/sessions` to create its act session (that
    route requires `ai.act.propose`), which is itself the point -- the session is
    seeded directly, so this exercises the TURN route's own check rather than the
    creation route's.
    """
    world = await _act_world(api_env, f"actoffer-{mode}-{len(codes)}", *codes)
    from tests.test_ai_admin_migration import seed_session

    session_id = await seed_session(
        api_env.admin_engine, world.org_id, world.user_id, persona="admin", mode=mode
    )
    provider = ScriptedProvider([[TextBlock(text="nothing to report")]])
    monkeypatch.setattr(api_module, "_provider_for", lambda: provider)

    turn = await api_env.client.post(
        f"{AI_PREFIX}/sessions/{session_id}/turns",
        headers=world.headers,
        json={"question": "anything stale?"},
    )
    assert turn.status_code == 200, turn.text
    tools = " ".join(provider.seen[0]["tools"])
    assert ("actions.propose" in tools) is offered, tools


async def test_the_persona_profile_may_propose_and_its_tool_set_is_reads_only(
    api_env: Any,
) -> None:
    """`may_propose` is the persona's opt-in and `tools` stays `ReadTool`-only:
    the act tool is built per turn by the router (§18.9), never registered in a
    frozen profile whose one binding would outlive the turn it describes."""
    from app.core.ai.types import ReadTool

    assert ADMIN_PROFILE.may_propose is True
    assert ADMIN_PROFILE.tools
    assert all(isinstance(tool, ReadTool) for tool in ADMIN_PROFILE.tools.values())
    assert "actions.propose" not in ADMIN_PROFILE.tools


@pytest.mark.parametrize("narrowed", [False, True], ids=["unnarrowed", "narrowed"])
def test_the_turn_context_built_for_a_confirm_redacts_its_bearer(narrowed: bool) -> None:
    """The confirm path builds a `TurnContext` for its one loopback read. Its
    repr must not carry the token: `TurnContext` is per-turn and module-private,
    but the accident §0.1 refuses -- a token in a log file forever -- is one
    `logger.debug(ctx)` away. It carries the request's `X-Client-Id`, which is
    no secret (phase 3 design §5.2)."""
    client = uuid4() if narrowed else None
    raw = {"Authorization": "Bearer secret-token-value"} | (
        {} if client is None else {"X-Client-Id": str(client)}
    )

    class _Request:
        app = None
        # A real `Headers`: `requested_client` reads it with `getlist`.
        headers = Headers(raw)

    ctx = api_module._turn_context(_Request())  # type: ignore[arg-type]
    assert ctx.bearer == "Bearer secret-token-value"
    assert "secret-token-value" not in repr(ctx)
    assert ctx.deadline > time.monotonic()
    assert ctx.requested_client == client
