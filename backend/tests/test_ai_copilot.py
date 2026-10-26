"""The technician copilot (docs/modules/ai-automation.md §0.3, §3.2, §9.2, §12).

Two guarantees, and the second is the one with no other home:

1. **The copilot proposes only against `ticket.device_id`** (§13 test 2). A
   ticket that links no device gets no proposal at all, and a device the ticket
   does not name is refused -- asserted as DIFFERENT OBSERVED VALUES in one test,
   because "the proposal was refused" and "the proposal was previewed" have to
   come out of the same fixture for either to mean anything.
2. **It cannot write to a ticket by construction, not by care.** The package
   registers no route that mutates a ticket and imports no ticket write service.
   Asserted over the FLATTENED router and over the AST -- never as a string ban,
   so the docstrings that explain why the copilot does not post survive
   (constraint 10).
"""

import ast
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.ai_copilot as copilot_package
from app.core.ai.providers import TextBlock, ToolCallBlock
from app.core.ai.tools import REGISTRY as PLATFORM_READ_TOOLS
from app.core.client_scope import ClientScope
from app.models import AiAction, AiSettings, Job, Ticket, TicketComment
from app.modules.ai_copilot import api as copilot_api
from app.modules.ai_copilot.profile import COPILOT_PROFILE
from app.modules.ai_copilot.tools import COPILOT_TOOLS
from tests.ai_admin_fakes import ScriptedProvider, ai_user
from tests.health_action_fixtures import seed_approved_action
from tests.health_fixtures import seed_device, seed_org

PACKAGE_ROOT = Path(copilot_package.__file__).resolve().parent
BASE = "/api/v1/service-desk/tickets"

STAFF_CODES = ("ai.use", "ai.act.propose", "ticket.read", "devices.read", "health.read")


class _Copilot:
    def __init__(
        self,
        org_id: UUID,
        user_id: UUID,
        headers: dict[str, str],
        ticket_id: UUID,
        device_id: UUID,
        other_device_id: UUID,
        action_key: str,
    ) -> None:
        self.org_id = org_id
        self.user_id = user_id
        self.headers = headers
        self.ticket_id = ticket_id
        self.device_id = device_id
        self.other_device_id = other_device_id
        self.action_key = action_key


async def _seed_ticket(
    admin_engine: Any, org_id: UUID, *, device_id: UUID | None, user_id: UUID
) -> UUID:
    from app.models import TicketCategory, TicketQueue

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        queue = TicketQueue(
            org_id=org_id, key=f"q-{uuid4().hex[:8]}", name="Q", created_by=user_id
        )
        session.add(queue)
        await session.flush()
        category = TicketCategory(
            org_id=org_id,
            key=f"c-{uuid4().hex[:8]}",
            name="C",
            type="incident",
            default_queue_id=queue.id,
            created_by=user_id,
        )
        session.add(category)
        await session.flush()
        ticket = Ticket(
            org_id=org_id,
            client_id=org_id,
            number=f"T-{uuid4().hex[:8]}",
            type="incident",
            category_id=category.id,
            queue_id=queue.id,
            status="new",
            subject="the laptop is slow",
            requester_id=user_id,
            origin="technician",
            created_by=user_id,
            device_id=device_id,
        )
        session.add(ticket)
        await session.commit()
        return ticket.id


async def _world(
    api_env: Any,
    tag: str,
    *,
    link_device: bool = True,
    codes: tuple[str, ...] = STAFF_CODES,
    personas: tuple[str, ...] = ("health", "technician"),
) -> _Copilot:
    """A technician, a ticket, the device it links, ANOTHER device in the same
    org, and a catalogue row carrying `personas`.

    The second device is what makes §13 test 2 non-vacuous: without it, "the
    copilot refused an unrelated device" could be a refusal because the device
    does not exist rather than because the ticket does not name it.
    """
    engine = api_env.admin_engine
    org = await seed_org(engine, tag)
    device_id = await seed_device(engine, org.org_id, hostname=f"{tag}-linked")
    other_device_id = await seed_device(engine, org.org_id, hostname=f"{tag}-unrelated")
    action_id = await seed_approved_action(
        engine,
        org.org_id,
        org.user_id,
        job_type="inventory.collect",
        risk_class="low",
        destructive=False,
        autonomy="require_approval",
        personas=list(personas),
        cooldown_s=0,
        max_per_device_per_day=50,
        max_per_org_per_hour=200,
    )
    from app.models import AiAutonomyCeiling, ApprovedAction

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            AiAutonomyCeiling(
                org_id=org.org_id,
                persona="technician",
                risk_class="low",
                level="require_approval",
            )
        )
        session.add(
            AiAutonomyCeiling(
                org_id=org.org_id, persona="admin", risk_class="low", level="require_approval"
            )
        )
        settings = await session.get(AiSettings, org.org_id)
        if settings is None:
            settings = AiSettings(org_id=org.org_id)
            session.add(settings)
        await session.commit()
        action_key = (await session.get(ApprovedAction, action_id)).key  # type: ignore[union-attr]

    user_id, sub = await ai_user(api_env, org.org_id, *codes)
    headers = await api_env.headers(org.org_id, sub)
    ticket_id = await _seed_ticket(
        engine, org.org_id, device_id=device_id if link_device else None, user_id=user_id
    )
    return _Copilot(
        org.org_id, user_id, headers, ticket_id, device_id, other_device_id, action_key
    )


async def _turn(
    api_env: Any, monkeypatch: Any, world: _Copilot, *, propose_device: UUID | None
) -> Any:
    """One act-mode copilot turn whose model emits `actions.propose` against
    `propose_device` (or answers plainly when it is `None`)."""
    scripts: list[Any] = []
    if propose_device is not None:
        scripts.append(
            [
                ToolCallBlock(
                    id="1",
                    name="actions.propose",
                    arguments={
                        "action_key": world.action_key,
                        "device_id": str(propose_device),
                        "rationale": "the inventory is stale",
                    },
                )
            ]
        )
    scripts.append([TextBlock(text="Here is a draft reply you could send.")])
    provider = ScriptedProvider(scripts)
    monkeypatch.setattr(copilot_api, "_provider_for", lambda: provider)

    created = await api_env.client.post(
        f"{BASE}/{world.ticket_id}/ai/sessions", headers=world.headers, json={"mode": "act"}
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    response = await api_env.client.post(
        f"{BASE}/{world.ticket_id}/ai/sessions/{session_id}/turns",
        headers=world.headers,
        json={"question": "what should I tell them?"},
    )
    assert response.status_code == 200, response.text
    return response


async def _open_session(
    api_env: Any, monkeypatch: Any, world: _Copilot, ticket_id: UUID
) -> str:
    # The CREATE route resolves a provider too (it stores `model_id`), so the
    # seam has to be replaced before this call and not only before the turn.
    monkeypatch.setattr(copilot_api, "_provider_for", lambda: ScriptedProvider([]))
    created = await api_env.client.post(
        f"{BASE}/{ticket_id}/ai/sessions", headers=world.headers, json={"mode": "act"}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _turn_on(
    api_env: Any,
    monkeypatch: Any,
    world: _Copilot,
    *,
    session_id: str,
    ticket_id: UUID,
    propose_device: UUID | None,
) -> Any:
    """One more turn of an EXISTING session, posted under `ticket_id`.

    The point of this helper is that `session_id` and `ticket_id` are
    independent arguments -- which is the shape of the thing under test: the
    route takes both, and nothing on `ai_session` records which ticket the
    conversation started on (§5.3, §19.10).
    """
    scripts: list[Any] = []
    if propose_device is not None:
        scripts.append(
            [
                ToolCallBlock(
                    id="1",
                    name="actions.propose",
                    arguments={
                        "action_key": world.action_key,
                        "device_id": str(propose_device),
                        "rationale": "the inventory is stale",
                    },
                )
            ]
        )
    scripts.append([TextBlock(text="Here is a draft reply you could send.")])
    monkeypatch.setattr(copilot_api, "_provider_for", lambda: ScriptedProvider(scripts))
    response = await api_env.client.post(
        f"{BASE}/{ticket_id}/ai/sessions/{session_id}/turns",
        headers=world.headers,
        json={"question": "what should I tell them?"},
    )
    return response


async def _actions(api_env: Any, org_id: UUID) -> list[AiAction]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (await session.execute(select(AiAction).where(AiAction.org_id == org_id)))
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------- #
# 1. §13 test 2 -- the device bound.
# --------------------------------------------------------------------------- #


async def test_the_copilot_proposes_against_the_tickets_device_and_no_other(
    api_env: Any, monkeypatch: Any
) -> None:
    """**BOTH HALVES, ONE FIXTURE, TWO DIFFERENT OBSERVED VALUES.**

    The unrelated device is a REAL device in the SAME ORG that the technician can
    see -- so the refusal cannot be "no such device" and cannot be "you may not
    read it". The only thing that differs is whether the ticket names it.
    """
    bound = await _world(api_env, "copilot-bound")
    unbound = await _world(api_env, "copilot-unbound")

    good = await _turn(api_env, monkeypatch, bound, propose_device=bound.device_id)
    bad = await _turn(
        api_env, monkeypatch, unbound, propose_device=unbound.other_device_id
    )

    good_rows = await _actions(api_env, bound.org_id)
    bad_rows = await _actions(api_env, unbound.org_id)

    assert [row.state for row in good_rows] == ["previewed"], good_rows
    assert good_rows[0].target_device_id == bound.device_id
    assert good.json()["proposed"] == [str(good_rows[0].id)]

    assert bad_rows == [], [row.approved_action_key for row in bad_rows]
    assert bad.json()["proposed"] == []
    # The refusal is TOLD TO THE MODEL and the turn continues -- refusing the
    # whole turn would hand a hostile ticket body a denial of service.
    statuses = {call["tool"]: call["status"] for call in bad.json()["tool_calls"]}
    assert statuses == {"actions.propose": "forbidden"}, statuses


async def test_a_ticket_with_no_linked_device_can_propose_nothing(
    api_env: Any, monkeypatch: Any
) -> None:
    """§3.2: "If the ticket has no linked device, the copilot cannot propose -- it
    can only say so." The device it would otherwise reach exists and is visible,
    so `[]` is evidence the bound ran."""
    world = await _world(api_env, "copilot-nodevice", link_device=False)
    response = await _turn(api_env, monkeypatch, world, propose_device=world.device_id)

    rows = await _actions(api_env, world.org_id)
    assert rows == []
    assert response.json()["proposed"] == []
    statuses = {call["tool"]: call["status"] for call in response.json()["tool_calls"]}
    assert statuses == {"actions.propose": "forbidden"}, statuses


async def test_the_bound_is_this_turns_ticket_and_not_the_one_the_session_opened_on(
    api_env: Any, monkeypatch: Any
) -> None:
    """**§19.10's CLAIM, TURNED INTO AN OBSERVATION.**

    `ai_session` carries no `ticket_id` and §5.3 forbids adding one, so the
    ticket is re-resolved on every turn and one conversation can be carried
    across two of the technician's own tickets. §19.10 asserted that the
    security-relevant half of that -- which device may be proposed against -- is
    re-derived from THIS turn's ticket and is therefore never stale. Nothing
    tested it. A claim about per-turn re-derivation is exactly the kind that
    stays true right up until somebody hoists the resolution out of the turn
    route for a "one query, not three" tidy-up, and every other test in this
    file opens a fresh session per turn, so none of them would notice.

    One session, three turns, two tickets naming two different devices:

    1. on ticket A, propose A's device      -> previewed
    2. on ticket B, propose **A's** device  -> refused (A is the device the
       SESSION started against; B is the ticket THIS turn names)
    3. on ticket B, propose B's device      -> previewed

    Turn 2 is the whole test. If the binding were carried on the session, it
    would be the one that succeeds; if the route simply refused every continued
    session, turn 3 would fail. The assertion is the ordered list of
    `(target_device_id, state)` actually written, not that something raised.
    """
    world = await _world(api_env, "copilot-carry")
    ticket_b = await _seed_ticket(
        api_env.admin_engine,
        world.org_id,
        device_id=world.other_device_id,
        user_id=world.user_id,
    )
    session_id = await _open_session(api_env, monkeypatch, world, world.ticket_id)

    first = await _turn_on(
        api_env, monkeypatch, world,
        session_id=session_id, ticket_id=world.ticket_id, propose_device=world.device_id,
    )
    stale = await _turn_on(
        api_env, monkeypatch, world,
        session_id=session_id, ticket_id=ticket_b, propose_device=world.device_id,
    )
    fresh = await _turn_on(
        api_env, monkeypatch, world,
        session_id=session_id, ticket_id=ticket_b, propose_device=world.other_device_id,
    )

    assert (first.status_code, stale.status_code, fresh.status_code) == (200, 200, 200)
    rows = sorted(await _actions(api_env, world.org_id), key=lambda row: row.created_at)
    assert [(row.target_device_id, row.state) for row in rows] == [
        (world.device_id, "previewed"),
        (world.other_device_id, "previewed"),
    ], [(row.target_device_id, row.state, row.message_seq) for row in rows]

    # And each turn's own report agrees with the table, so the middle turn is a
    # refusal the model was told about rather than a row written somewhere else.
    assert [
        response.json()["proposed"] == [] for response in (first, stale, fresh)
    ] == [False, True, False]
    assert {call["tool"]: call["status"] for call in stale.json()["tool_calls"]} == {
        "actions.propose": "forbidden"
    }


async def test_a_second_turn_on_a_device_less_ticket_proposes_nothing(
    api_env: Any, monkeypatch: Any
) -> None:
    """**§19.1's FAIL-OPEN, RE-ASKED PER TURN.**

    `ProposeBinding.ticket_bound` exists because `ticket_device_id is not None`
    could not tell "no ticket context" from "a ticket with no device", and a
    copilot turn on a device-less ticket could therefore propose against any
    device its technician could see. That is pinned for a session's FIRST turn.

    It has to hold on a LATER turn too, and the way it could stop is specific:
    the first turn on this session was bound to a ticket that DOES name a
    device, so anything that carried `ticket_bound` or `ticket_device_id`
    forward would make this turn look bound to the wrong device -- or, worse,
    bound to nothing at all. The device it names is real, in the same org, and
    visible to the caller, so `[]` is the bound and not an authorization
    failure.
    """
    world = await _world(api_env, "copilot-carry-nodevice")
    ticket_c = await _seed_ticket(
        api_env.admin_engine, world.org_id, device_id=None, user_id=world.user_id
    )
    session_id = await _open_session(api_env, monkeypatch, world, world.ticket_id)

    first = await _turn_on(
        api_env, monkeypatch, world,
        session_id=session_id, ticket_id=world.ticket_id, propose_device=world.device_id,
    )
    second = await _turn_on(
        api_env, monkeypatch, world,
        session_id=session_id, ticket_id=ticket_c, propose_device=world.device_id,
    )

    rows = await _actions(api_env, world.org_id)
    assert (
        first.status_code,
        second.status_code,
        [row.target_device_id for row in rows],
        second.json()["proposed"],
    ) == (200, 200, [world.device_id], [])
    assert {call["tool"]: call["status"] for call in second.json()["tool_calls"]} == {
        "actions.propose": "forbidden"
    }


async def test_the_proposal_audit_names_the_ticket_that_bound_it(
    api_env: Any, monkeypatch: Any
) -> None:
    """**THE HALF OF §19.10 THAT WAS A REAL GAP** (§19.20).

    The binding is never stale, and that is provable above. What was NOT
    recoverable from any durable record is WHICH ticket authorized a copilot
    proposal's device: `ai_action` has `session_id` and `message_seq` and no
    ticket, `ai_session` has no ticket by §5.3, and the ticket is the entire
    justification for the device this persona was allowed to name. Invariant #4
    asks the audit log to record the privileged action; a proposal whose
    authorizing constraint is unrecorded is recorded only halfway.

    Fixed WITHOUT the column §5.3 forbids: `ai.action_proposed`'s metadata
    carries `ticket_id`, which is per-EVENT and per-TURN -- the same turn-scoped
    binding the check itself reads, so the two cannot disagree.

    Asserted across a session that MOVED: two proposals, two tickets, and the
    two events must name different tickets. One proposal would be satisfied by
    a constant.
    """
    from app.models import AuditEvent

    world = await _world(api_env, "copilot-audit-ticket")
    ticket_b = await _seed_ticket(
        api_env.admin_engine,
        world.org_id,
        device_id=world.other_device_id,
        user_id=world.user_id,
    )
    session_id = await _open_session(api_env, monkeypatch, world, world.ticket_id)
    await _turn_on(
        api_env, monkeypatch, world,
        session_id=session_id, ticket_id=world.ticket_id, propose_device=world.device_id,
    )
    await _turn_on(
        api_env, monkeypatch, world,
        session_id=session_id, ticket_id=ticket_b, propose_device=world.other_device_id,
    )

    async with AsyncSession(api_env.admin_engine) as session:
        events = list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == world.org_id)
                    .where(AuditEvent.action == "ai.action_proposed")
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )
    assert [
        (event.meta["ticket_id"], event.meta["target_device_id"]) for event in events
    ] == [
        (str(world.ticket_id), str(world.device_id)),
        (str(ticket_b), str(world.other_device_id)),
    ], [event.meta for event in events]
    # And no prose rode in with it.
    assert all("rationale" not in event.meta for event in events)


async def test_an_admin_proposal_records_no_ticket_rather_than_a_null_one(
    api_env: Any, monkeypatch: Any
) -> None:
    """The unbounded persona's side of the same key, and the reason it is
    ABSENT rather than `None`.

    A `"ticket_id": null` on an admin proposal reads as "this proposal was about
    a ticket and we lost it". The admin persona is correctly unbounded (§19.1),
    so the honest record is that the key does not apply -- and the copilot's
    events are then distinguishable from the admin's by the key's presence, not
    by having to know which persona is ticket-bound.
    """
    from app.core.ai.act import propose
    from app.core.ai.executor_staff import invoke_tool
    from app.core.tenancy import org_session
    from app.models import AuditEvent
    from tests.ai_act_fixtures import build_world
    from tests.test_ai_act_propose import _actions as _admin_actions

    admin_world = await build_world(api_env, tag="copilot-audit-admin")
    async with org_session(
        admin_world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        await propose(
            session,
            org_id=admin_world.org_id,
            binding=admin_world.binding,
            ctx=admin_world.ctx,
            executor=invoke_tool,
            action_key=admin_world.action_key,
            device_id=admin_world.device_id,
        )
    (row,) = await _admin_actions(api_env, admin_world.org_id)

    async with AsyncSession(api_env.admin_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == admin_world.org_id)
                .where(AuditEvent.action == "ai.action_proposed")
            )
        ).scalar_one()
    assert (event.meta["ai_action_id"], "ticket_id" in event.meta) == (str(row.id), False), (
        event.meta
    )


async def test_a_catalogue_row_without_the_technician_persona_is_refused_here_and_allowed_there(
    api_env: Any, monkeypatch: Any
) -> None:
    """**BOTH PERSONAS AGAINST THE SAME KIND OF ROW, IN ONE TEST.** Asserting only
    that the copilot was refused would pass against a build that refuses every
    proposal -- the persona filter would be untested and so would act mode."""
    from tests.ai_act_fixtures import build_world
    from tests.test_ai_act_api import _propose_over_http

    # `personas` carries `admin` but NOT `technician`.
    refused = await _world(api_env, "copilot-persona", personas=("health", "admin"))
    response = await _turn(api_env, monkeypatch, refused, propose_device=refused.device_id)
    assert await _actions(api_env, refused.org_id) == []
    statuses = {call["tool"]: call["status"] for call in response.json()["tool_calls"]}
    assert statuses == {"actions.propose": "forbidden"}, statuses

    # The same shape of row, carrying `admin`, IS proposable by the admin
    # persona -- so the refusal above is the persona filter and not act mode
    # being broken.
    admin_world = await build_world(
        api_env,
        tag="copilot-persona-admin",
        codes=("ai.use", "ai.act.propose", "devices.read", "jobs.dispatch"),
    )
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        settings = await session.get(AiSettings, admin_world.org_id)
        if settings is None:
            settings = AiSettings(org_id=admin_world.org_id)
            session.add(settings)
        await session.commit()
    action_id = await _propose_over_http(api_env, monkeypatch, admin_world)
    async with AsyncSession(api_env.admin_engine) as session:
        assert (await session.get(AiAction, action_id)) is not None


# --------------------------------------------------------------------------- #
# 2. The no-write guarantee, structurally.
# --------------------------------------------------------------------------- #


def _source_files() -> list[Path]:
    files = sorted(p for p in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)
    # A scan that found nothing would pass every assertion below.
    assert len(files) >= 5, f"the sweep of {PACKAGE_ROOT} found {len(files)} files; it is vacuous"
    return files


def _flatten(app: Any) -> list[tuple[frozenset[str], str]]:
    """FastAPI 0.139: `app.routes` holds `_IncludedRouter` placeholders whose own
    `path` is `None`, so a naive walk finds NONE of this module's routes -- and
    the assertion below would be vacuous rather than false."""
    from fastapi.routing import APIRoute

    found: list[tuple[frozenset[str], str]] = []

    def walk(router: Any, prefix: str) -> None:
        for route in getattr(router, "routes", []):
            inner = getattr(route, "original_router", None)
            if inner is not None:
                context = getattr(route, "include_context", None)
                walk(inner, prefix + getattr(context, "prefix", ""))
            elif isinstance(route, APIRoute):
                found.append((frozenset(route.methods or ()), prefix + route.path))

    walk(app.router, "")
    return found


async def test_the_package_registers_exactly_two_routes_and_neither_mutates_a_ticket(
    api_env: Any,
) -> None:
    """§0.3, as a property of the ROUTER rather than of anybody's intentions.

    The flattener is pinned against an empty result FIRST: a walk that missed
    `include_router`'d routes would make "no ticket-mutating route exists" true
    of a router it never looked at.
    """
    routes = _flatten(api_env.app)
    ours = sorted(path for _, path in routes if "/ai/sessions" in path and "tickets" in path)
    assert ours == [
        f"{BASE}/{{ticket_id}}/ai/sessions",
        f"{BASE}/{{ticket_id}}/ai/sessions/{{session_id}}/turns",
    ], ours

    # And nothing else this package could have mounted under a ticket.
    ticket_ai = [
        (sorted(methods), path)
        for methods, path in routes
        if path.startswith(f"{BASE}/{{ticket_id}}/ai")
    ]
    assert len(ticket_ai) == 2, ticket_ai
    assert all(methods == ["POST"] for methods, _ in ticket_ai), ticket_ai


def test_the_package_imports_no_ticket_write_service() -> None:
    """Matched against IMPORTS, not against a string ban -- this package's
    docstrings necessarily say `add_comment` and `change_status` while explaining
    why it does not call them, and a grep-the-source guard would be red on its
    own explanation and get deleted (constraint 10)."""
    forbidden_modules = {
        "app.modules.service_desk.threads",
        "app.modules.service_desk.transitions",
        "app.modules.service_desk.tickets",
        "app.modules.service_desk.api",
    }
    forbidden_names = {
        "add_comment",
        "change_status",
        "update_ticket",
        "create_ticket",
        "assign_ticket",
    }
    offenders: list[str] = []
    for path in _source_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.name}: import {alias.name}"
                    for alias in node.names
                    if alias.name in forbidden_modules
                )
            elif isinstance(node, ast.ImportFrom):
                if node.module in forbidden_modules:
                    offenders.append(f"{path.name}: from {node.module}")
                offenders.extend(
                    f"{path.name}: imports {alias.name}"
                    for alias in node.names
                    if alias.name in forbidden_names
                )
    assert offenders == [], "; ".join(offenders)
    # Non-vacuity: the package DOES import the ticket core's models, so the scan
    # is looking at a file that could have imported its services.
    sources = "\n".join(path.read_text() for path in _source_files())
    assert "app.modules.service_desk.models" in sources


async def test_a_draft_reply_creates_no_ticket_comment(
    api_env: Any, monkeypatch: Any
) -> None:
    """§0.3: a draft is returned to the technician's editor and never written.
    The count is taken BEFORE and AFTER, so "no comment exists" cannot be
    satisfied by a ticket that never had one."""
    world = await _world(api_env, "copilot-draft")

    async def _comments() -> int:
        async with AsyncSession(api_env.admin_engine) as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(TicketComment)
                        .where(TicketComment.ticket_id == world.ticket_id)
                    )
                ).scalar_one()
            )

    before = await _comments()
    response = await _turn(api_env, monkeypatch, world, propose_device=None)
    after = await _comments()

    assert "draft reply" in response.json()["answer"]
    assert (before, after) == (0, 0)


async def test_a_copilot_turn_dispatches_no_job(api_env: Any, monkeypatch: Any) -> None:
    """The proposal is a PREVIEW. Nothing is signed and nothing reaches a
    machine until a human with the owning module's dispatch permission
    confirms."""
    world = await _world(api_env, "copilot-nojob")

    async def _jobs() -> int:
        async with AsyncSession(api_env.admin_engine) as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(Job).where(Job.org_id == world.org_id)
                    )
                ).scalar_one()
            )

    before = await _jobs()
    await _turn(api_env, monkeypatch, world, propose_device=world.device_id)
    assert await _jobs() == before


# --------------------------------------------------------------------------- #
# 3. The tool set and the gates.
# --------------------------------------------------------------------------- #


def test_the_copilot_tool_set_is_a_non_empty_subset_of_the_platform_read_allowlist() -> None:
    """`<=` alone is satisfied by `{}`, which is why the non-emptiness assertion
    is here and not implied.

    **READ OFF THE KERNEL ALLOWLIST, NOT OFF THE ADMIN PERSONA** (§19.9). The
    subject of this assertion was always "the platform's read allowlist"; until
    the registry moved to `app/core/ai/tools/` the only way to name it was
    through `app.modules.ai_admin`, which is the finding itself showing up in
    the test suite.
    """
    assert COPILOT_TOOLS, "an empty tool set makes the subset assertion vacuous"
    assert set(COPILOT_TOOLS) <= set(PLATFORM_READ_TOOLS), sorted(
        set(COPILOT_TOOLS) - set(PLATFORM_READ_TOOLS)
    )
    for name, tool in COPILOT_TOOLS.items():
        # THE SAME OBJECT, not a copy: a second `ReadTool` with its own
        # `response_fields` is a second list the registry's drift test does not
        # check, and the day another module adds a field, this persona keeps
        # serving it while the drift test stays green against the kernel copy.
        assert tool is PLATFORM_READ_TOOLS[name], name
        assert tool.method == "GET", name


def test_the_copilot_tool_set_cannot_be_widened_at_runtime() -> None:
    with pytest.raises(TypeError):
        COPILOT_TOOLS["credentials.list"] = None  # type: ignore[index]
    assert "credentials.list" not in COPILOT_TOOLS


def test_the_profile_is_registered_with_act_and_neutralized_replay() -> None:
    """This persona reads TICKET THREADS, which carry text `email_intake` created
    from messages anyone who can reach the helpdesk mailbox can send. It is the
    persona most likely to have a laundered instruction in its own transcript, so
    `verbatim` replay here would be the §0.8 hole left open where it matters
    most."""
    from app.core.ai import PERSONAS

    assert PERSONAS["technician"] is COPILOT_PROFILE
    assert COPILOT_PROFILE.may_propose is True
    assert COPILOT_PROFILE.replay_policy == "neutralize_flagged"
    assert COPILOT_PROFILE.package == "app.modules.ai_copilot"


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        (STAFF_CODES, 201),
        # `ticket.read` but no `ai.use`: the route's own gate passes and the
        # handler's does not.
        (("ticket.read", "devices.read"), 403),
        # `ai.use` but no `ticket.read`: the ROUTE refuses.
        (("ai.use", "devices.read"), 403),
    ],
    ids=["both", "ticket-read-only", "ai-use-only"],
)
async def test_the_surface_needs_ticket_read_and_ai_use(
    api_env: Any, monkeypatch: Any, codes: tuple[str, ...], expected: int
) -> None:
    """§12's "gated on `ticket.read` plus `ai.use`", with each half missing in
    turn -- so a build that dropped either check produces a 201 where this
    expects a 403."""
    world = await _world(api_env, f"copilot-gate-{len(codes)}", codes=codes)
    monkeypatch.setattr(
        copilot_api, "_provider_for", lambda: ScriptedProvider([[TextBlock(text="x")]])
    )
    response = await api_env.client.post(
        f"{BASE}/{world.ticket_id}/ai/sessions", headers=world.headers
    )
    assert response.status_code == expected, response.text


async def test_a_ticket_in_another_org_is_a_404(api_env: Any, monkeypatch: Any) -> None:
    mine = await _world(api_env, "copilot-org-a")
    theirs = await _world(api_env, "copilot-org-b")
    monkeypatch.setattr(
        copilot_api, "_provider_for", lambda: ScriptedProvider([[TextBlock(text="x")]])
    )
    response = await api_env.client.post(
        f"{BASE}/{theirs.ticket_id}/ai/sessions", headers=mine.headers
    )
    assert response.status_code == 404, response.text


async def test_an_admin_persona_session_cannot_be_continued_on_this_surface(
    api_env: Any, monkeypatch: Any
) -> None:
    """A conversation started on another persona's surface was assembled under
    that persona's prompt, tool set and replay policy. Continuing it here would
    hand this persona a transcript built under different rules."""
    world = await _world(api_env, "copilot-crosspersona")
    from tests.test_ai_admin_migration import seed_session

    foreign = await seed_session(
        api_env.admin_engine, world.org_id, world.user_id, persona="admin", mode="act"
    )
    monkeypatch.setattr(
        copilot_api, "_provider_for", lambda: ScriptedProvider([[TextBlock(text="x")]])
    )
    response = await api_env.client.post(
        f"{BASE}/{world.ticket_id}/ai/sessions/{foreign}/turns",
        headers=world.headers,
        json={"question": "hello"},
    )
    assert response.status_code == 404, response.text
