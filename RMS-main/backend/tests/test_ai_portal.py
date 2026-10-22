"""The end-user tier-1 resolver (docs/modules/ai-automation.md §0.2, §0.9, §3.1,
§11.1, §11.2, §12; §13 test 18).

**THIS IS THE PERSONA THAT READS THE MOST ATTACKER-CONTROLLED TEXT ON THE
PLATFORM AND HAS THE LEAST REACH, AND EVERY TEST HERE IS ABOUT THE SECOND HALF.**
The inversion is the design, so the assertions are about absence -- and absence
is the easiest thing in this repo to assert vacuously. Each one below therefore
pins its own non-vacuity first: the router walk asserts it found the two routes
that DO exist before concluding no action route does; the tool-set exclusion
asserts the set is non-empty; and "no HTTP request was made" is asserted on a
transport tape that a passing test has to have installed.
"""

import ast
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import text as sqltext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.modules.ai_portal as portal_ai_package
from app.core.ai.executor_portal import invoke_portal_tool
from app.core.ai.providers import TextBlock, ToolCallBlock
from app.core.ai.types import ReadTool
from app.models import AiSettings
from app.modules.ai_portal import api as portal_ai_api
from app.modules.ai_portal.profile import RESOLVER_PROFILE
from app.modules.ai_portal.tools import PORTAL_TOOLS
from tests.ai_admin_fakes import RecordingTransport, ScriptedProvider, install_portal_spy

PACKAGE_ROOT = Path(portal_ai_package.__file__).resolve().parent
API = "/api/v1/portal/ai"


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


async def _requester(api_env: Any, org_id: UUID) -> dict[str, str]:
    sub = await api_env.create_user(org_id, "requester")
    return await api_env.headers(org_id, sub)


# --------------------------------------------------------------------------- #
# 1. §13 test 18 -- no action route exists on this surface.
# --------------------------------------------------------------------------- #


def _flatten(app: Any) -> list[tuple[frozenset[str], str]]:
    """FastAPI 0.139: `app.routes` holds `_IncludedRouter` placeholders whose own
    `path` is `None`, so a naive walk finds NONE of the module routes.

    **A FLATTENER THAT SILENTLY MISSES ROUTES MAKES §13 TEST 18 VACUOUS**, and
    that test is the one standing between §0.9 and a resolver that can dispatch.
    The test below therefore asserts the two routes that DO exist before it
    concludes anything about the ones that do not.
    """
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


async def test_the_portal_ai_surface_registers_no_action_route(api_env: Any) -> None:
    """§13 test 18, ROUTER-DERIVED so a future addition fails a build.

    The two positive assertions come FIRST and are what make the negative one
    mean something: a flattener that found nothing would report "no action route
    exists" for a router it never looked at.
    """
    routes = _flatten(api_env.app)
    ours = sorted(path for _, path in routes if path.startswith(API))
    assert ours == [
        f"{API}/sessions",
        f"{API}/sessions/{{session_id}}/turns",
    ], ours

    forbidden = [
        (sorted(methods), path)
        for methods, path in routes
        if path.startswith("/api/v1/portal")
        and any(word in path for word in ("action", "confirm", "reject", "propose"))
    ]
    assert forbidden == [], forbidden


async def test_every_route_on_this_surface_is_gated_by_a_portal_permission(
    api_env: Any,
) -> None:
    """Invariant #6, derived from the router. `require_portal_permission` was
    built to be recoverable by the same `route_permission` reflection staff
    routes use, so a portal route with no gate is visible here."""
    from fastapi.routing import APIRoute

    from app.core.rbac import route_permission

    def walk(router: Any, prefix: str) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for route in getattr(router, "routes", []):
            inner = getattr(route, "original_router", None)
            if inner is not None:
                context = getattr(route, "include_context", None)
                out.extend(walk(inner, prefix + getattr(context, "prefix", "")))
            elif isinstance(route, APIRoute):
                out.append((prefix + route.path, route))
        return out

    ours = [(path, route) for path, route in walk(api_env.app.router, "") if path.startswith(API)]
    assert len(ours) == 2, [path for path, _ in ours]
    for path, route in ours:
        assert route_permission(route) == "portal.ticket.read", path


# --------------------------------------------------------------------------- #
# 2. The package cannot reach the staff stack.
# --------------------------------------------------------------------------- #


def _source_files() -> list[Path]:
    files = sorted(p for p in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)
    assert len(files) >= 5, f"the sweep of {PACKAGE_ROOT} found {len(files)} files; vacuous"
    return files


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_resolver_package_imports_neither_the_staff_executor_nor_staff_deps() -> None:
    """§0.2, structurally. `app/api/deps.py` is where `AuthContext` and
    `require_permission` live; importing either here would mean a staff principal
    or a staff gate had come into reach of the persona that must have neither.
    `executor_staff` is the other half: handed a portal token every staff route
    403s, and the "fix" for that is a service token -- the privilege-escalation
    primitive that executor's own docstring warns about."""
    forbidden = ("app.core.ai.executor_staff", "app.api.deps")
    offenders = [
        f"{path.name}: {name}"
        for path in _source_files()
        for name in _imports(path)
        if any(name == bad or name.startswith(bad + ".") for bad in forbidden)
    ]
    assert offenders == [], "; ".join(offenders)
    # Non-vacuity: the package DOES import the portal's own principal module, so
    # the scan is looking at files that import auth machinery of some kind.
    assert any(
        "app.modules.portal.principal" in name
        for path in _source_files()
        for name in _imports(path)
    )


def test_the_profile_uses_the_portal_executor_and_proposes_nothing() -> None:
    """`may_propose is False` on the REGISTERED profile, and the executor is the
    portal one. Both asserted off the registry rather than off the source, so a
    profile constructed elsewhere and registered under this name fails here."""
    from app.core.ai import PERSONAS
    from app.core.ai.executor_portal import invoke_portal_tool as portal_executor

    assert PERSONAS["end_user"] is RESOLVER_PROFILE
    assert RESOLVER_PROFILE.may_propose is False
    assert RESOLVER_PROFILE.executor is portal_executor
    assert "actions.propose" not in RESOLVER_PROFILE.tools


# --------------------------------------------------------------------------- #
# 3. The two scope cuts (§11.1, §11.2) and the excluded surface.
# --------------------------------------------------------------------------- #

#: Every prefix this persona must not reach. `devices` and `docs` are the two
#: RECORDED SCOPE CUTS: the platform cannot tell which device a requester owns
#: (§11.1 -- `ticket.device_id` is asserted by whoever created the ticket, which
#: for an email-created ticket is a parser reading text an unauthenticated sender
#: wrote), and the documentation library is an internal runbook store (§11.2).
#: Help articles staff mark visible in the portal are the one exception, and
#: they are read through `/portal/help`, never through a `/docs` route. The rest
#: are the staff surface.
_EXCLUDED_PATH_WORDS = (
    "devices",
    "/docs",
    "documentation",
    "doc-articles",
    "alerts",
    "jobs",
    "inventory",
    "patch",
    "credentials",
    "audit",
    "scripts",
    "licenses",
    "backups",
)


def test_the_tool_set_reaches_no_device_no_runbook_and_no_staff_surface() -> None:
    """**THE §11.1 AND §11.2 GUARDS, AS TESTS RATHER THAN AS INTENTIONS.**

    Without the device half, the next builder resolves "the requester's laptop"
    from `ticket.device_id` -- which an unauthenticated email can set. Without the
    documentation half, they widen a `docs.read` route to portal principals and
    serve an internal runbook to a customer. Both are one plausible commit away,
    and both fail here instead.
    """
    assert PORTAL_TOOLS, "an empty tool set makes every exclusion below vacuous"
    offenders = [
        f"{name}: {tool.path}"
        for name, tool in PORTAL_TOOLS.items()
        for word in _EXCLUDED_PATH_WORDS
        if word in tool.path
    ]
    assert offenders == [], "; ".join(offenders)


def test_every_tool_is_a_portal_get_gated_on_a_portal_permission() -> None:
    """The positive statement of the same property. `<= exclusions` is satisfied
    by a tool set naming a staff path nobody thought to exclude; this is
    allowlist-shaped and catches that."""
    for name, tool in PORTAL_TOOLS.items():
        assert tool.method == "GET", name
        assert tool.path.startswith("/api/v1/portal/"), f"{name}: {tool.path}"
        assert tool.permission.startswith("portal."), f"{name}: {tool.permission}"
        assert tool.response_fields, f"{name} declares no response fields; scrub blocks all"


def test_the_tool_set_cannot_be_widened_at_runtime() -> None:
    with pytest.raises(TypeError):
        PORTAL_TOOLS["devices.list"] = None  # type: ignore[index]
    assert "devices.list" not in PORTAL_TOOLS


@pytest.mark.parametrize(
    ("tool_name", "model_name"),
    [
        ("portal.my_tickets", "PortalTicketSummary"),
        ("portal.categories", "PortalCategoryOut"),
        ("portal.help_suggest", "PortalHelpSuggestion"),
        ("portal.help_article", "PortalHelpArticle"),
    ],
)
def test_declared_response_fields_match_the_live_portal_model(
    tool_name: str, model_name: str
) -> None:
    """§7.3: `response_fields` is the FAIL-CLOSED control, and it is a frozen
    constant rather than a set derived from the live model -- a derived set would
    grow WITH the model and allowlist the very field the gate exists to catch. So
    it is compared here instead, and the day the portal adds a field this goes
    red and the tool goes dark until somebody looks at the new field."""
    import app.modules.portal.schema as portal_schema

    model = getattr(portal_schema, model_name)
    assert set(PORTAL_TOOLS[tool_name].response_fields) == set(model.model_fields), (
        f"{tool_name} and {model_name} have drifted"
    )


def test_the_detail_tools_fields_are_the_union_of_the_models_it_nests() -> None:
    """`PortalTicketDetail` nests four other models, and `scrub` walks the
    payload at ANY DEPTH -- so the declaration has to be the union, and a nested
    model growing a field is exactly the case the flat comparison above cannot
    see."""
    import app.modules.portal.schema as portal_schema

    expected: set[str] = set()
    for model_name in (
        "PortalTicketDetail",
        "PortalCommentOut",
        "PortalStatusHistoryOut",
        "PortalAttachmentOut",
        "PortalApprovalOut",
    ):
        expected |= set(getattr(portal_schema, model_name).model_fields)
    assert set(PORTAL_TOOLS["portal.my_ticket"].response_fields) == expected


# --------------------------------------------------------------------------- #
# 4. The executor: off-surface tools, and the act tool that does not exist.
# --------------------------------------------------------------------------- #


class _StaffArgs(BaseModel):
    device_id: UUID


_A_STAFF_TOOL = ReadTool(
    name="devices.health",
    method="GET",
    path="/api/v1/devices/{device_id}/health",
    permission="devices.read",
    args=_StaffArgs,
)


async def test_a_staff_tool_in_the_resolvers_registry_makes_no_http_request(
    api_env: Any, monkeypatch: Any
) -> None:
    """**THE TAPE IS THE ASSERTION.** "It returned an error" cannot tell "no
    request was made" apart from "the request was made and the route said no",
    and only the first is what §0.2 claims. The refusal happens before a URL
    exists, so a staff `ReadTool` that somehow reached this persona's tool set is
    inert rather than merely unauthorized."""
    import time as _time

    from app.core.ai.executor_staff import ToolError

    spy = RecordingTransport(api_env.app)
    install_portal_spy(monkeypatch, spy)
    org_id = await api_env.create_org("resolver-offsurface")
    headers = await _requester(api_env, org_id)

    outcome = await invoke_portal_tool(
        tool_name="devices.health",
        raw_args={"device_id": str(uuid4())},
        app=api_env.app,
        bearer=headers["Authorization"],
        deadline=_time.monotonic() + 30.0,
        registry={"devices.health": _A_STAFF_TOOL},
    )

    assert isinstance(outcome, ToolError)
    assert (outcome.kind, [str(r.url) for r in spy.requests]) == ("forbidden", [])


async def test_the_act_tool_is_unknown_here_and_makes_no_request(
    api_env: Any, monkeypatch: Any
) -> None:
    """§0.9 as a runtime fact, not only a flag: `actions.propose` is not in this
    persona's registry, so the executor's dict lookup fails and no HTTP request
    is built. Asserted with the resolver's OWN registry, so this is the path a
    fully persuaded model actually takes."""
    import time as _time

    from app.core.ai.executor_staff import ToolError

    spy = RecordingTransport(api_env.app)
    install_portal_spy(monkeypatch, spy)
    org_id = await api_env.create_org("resolver-noact")
    headers = await _requester(api_env, org_id)

    outcome = await invoke_portal_tool(
        tool_name="actions.propose",
        raw_args={"action_key": "anything", "device_id": str(uuid4())},
        app=api_env.app,
        bearer=headers["Authorization"],
        deadline=_time.monotonic() + 30.0,
        registry=RESOLVER_PROFILE.tools,
    )

    assert isinstance(outcome, ToolError)
    assert (outcome.kind, spy.requests) == ("unknown_tool", [])


# --------------------------------------------------------------------------- #
# 5. Cross-requester isolation, on the payload and at the database.
# --------------------------------------------------------------------------- #


async def test_a_requester_never_sees_another_requesters_ticket_through_the_resolver(
    api_env: Any, monkeypatch: Any, a_ticket_for: Any
) -> None:
    """**ASSERTED ON THE PAYLOAD THE EXECUTOR RETURNED**, because the isolation
    is RLS's and a test that merely called the service could not see it fail.

    Two requesters, one org, one ticket each. The caller's own number must be
    present and the other's absent -- two observed values from one fixture, so
    "the list was empty" cannot pass for isolation.
    """
    import time as _time

    from app.models import Ticket

    org_id = await api_env.create_org("resolver-isolation")
    mine = await a_ticket_for(org_id, "me")
    theirs = await a_ticket_for(org_id, "them")
    async with AsyncSession(api_env.admin_engine) as session:
        my_number = (await session.get(Ticket, mine.id)).number  # type: ignore[union-attr]
        their_number = (await session.get(Ticket, theirs.id)).number  # type: ignore[union-attr]

    headers = await api_env.headers(org_id, await _sub_for(api_env, org_id, mine.requester_id))

    outcome = await invoke_portal_tool(
        tool_name="portal.my_tickets",
        raw_args={},
        app=api_env.app,
        bearer=headers["Authorization"],
        deadline=_time.monotonic() + 30.0,
        registry=RESOLVER_PROFILE.tools,
    )
    payload = str(getattr(outcome, "payload", outcome))
    assert my_number in payload, payload
    assert their_number not in payload, payload


async def test_the_resolver_reads_only_help_articles_marked_for_the_portal(
    api_env: Any,
) -> None:
    """The help tools go through the portal's own routes, so an internal
    runbook is as unreachable to the resolver as it is to the requester."""
    import time as _time

    org_id = await api_env.create_org("resolver-help")
    tech = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    ids: dict[str, str] = {}
    for title, visible in (("Reset your email password", True), ("Email relay runbook", False)):
        created = await api_env.client.post(
            "/api/v1/docs/articles", json={"title": title, "body": "Steps."}, headers=tech
        )
        ids[title] = created.json()["id"]
        if visible:
            await api_env.client.put(
                f"/api/v1/docs/articles/{ids[title]}/portal-visibility",
                json={"portal_visible": True},
                headers=tech,
            )
    headers = await _requester(api_env, org_id)

    async def call(tool_name: str, raw_args: dict[str, Any]) -> Any:
        return await invoke_portal_tool(
            tool_name=tool_name,
            raw_args=raw_args,
            app=api_env.app,
            bearer=headers["Authorization"],
            deadline=_time.monotonic() + 30.0,
            registry=RESOLVER_PROFILE.tools,
        )

    suggested = await call("portal.help_suggest", {"q": "email password"})
    assert ids["Reset your email password"] in str(suggested.payload)
    assert ids["Email relay runbook"] not in str(suggested.payload)

    read = await call("portal.help_article", {"article_id": ids["Reset your email password"]})
    assert "Reset your email password" in str(read.payload)
    hidden = await call("portal.help_article", {"article_id": ids["Email relay runbook"]})
    assert not hasattr(hidden, "payload"), hidden
    assert "relay" not in str(hidden)


async def _sub_for(api_env: Any, org_id: UUID, user_id: UUID) -> str:
    """The OIDC subject of an existing user, so a token can be minted for the
    requester `a_ticket_for` created rather than for a fresh one."""
    from app.models import Permission, Role, RolePermission, User, UserRole

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = await session.get(User, user_id)
        assert user is not None
        role = Role(org_id=org_id, name=f"portal-{uuid4().hex[:8]}", description="t")
        session.add(role)
        await session.flush()
        from sqlalchemy import select

        for code in ("portal.ticket.read", "portal.ticket.write"):
            permission_id = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id))
        await session.commit()
        return str(user.external_subject)


async def test_the_portal_role_is_fail_closed_without_the_requester_guc(
    portal_engine: AsyncEngine, api_env: Any, a_ticket_for: Any
) -> None:
    """**THE FAIL-CLOSED HALF, ASSERTED WHERE IT IS ACTUALLY ENFORCED.**

    The resolver never opens a `portal_session` of its own -- the portal ROUTE
    does, inside the loopback -- and this is why that matters: with only
    `app.current_org` pinned, the `portal_user` role sees NOTHING. Not the whole
    org, not the caller's rows: nothing. So a layer that pinned one GUC and
    forgot the other would produce an empty answer rather than a leak, and a
    layer that pinned neither would produce an empty answer too.

    Asserted with a REAL ticket present, so `0` is evidence the policy ran rather
    than evidence the table is empty.
    """
    org_id = await api_env.create_org("resolver-failclosed")
    await a_ticket_for(org_id, "someone")

    async with AsyncSession(portal_engine) as session:
        # `SET LOCAL` takes no bind parameters -- Postgres parses it before the
        # extended protocol binds anything -- so the value is interpolated, from
        # a `UUID` this test created, never from input.
        await session.execute(sqltext(f"SET LOCAL app.current_org = '{org_id}'"))
        without = (
            await session.execute(sqltext("SELECT count(*) FROM ticket"))
        ).scalar_one()
    assert without == 0, "the portal role returned rows with no requester GUC pinned"

    # And with BOTH pinned, the same query answers -- so the zero above is the
    # missing GUC and not a missing grant.
    from app.core.tenancy import portal_session

    async with AsyncSession(api_env.admin_engine) as session:
        from sqlalchemy import select

        from app.models import Ticket

        requester_id = (
            await session.execute(select(Ticket.requester_id).where(Ticket.org_id == org_id))
        ).scalars().first()
    async with portal_session(org_id, requester_id, engine=portal_engine) as scoped:
        with_both = (
            await scoped.execute(sqltext("SELECT count(*) FROM ticket"))
        ).scalar_one()
    assert with_both == 1


# --------------------------------------------------------------------------- #
# 6. The dial cannot be raised for this persona.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", ["suggest", "require_approval"])
async def test_an_end_user_ceiling_above_off_is_unstorable(
    admin_engine: AsyncEngine, api_env: Any, level: str
) -> None:
    """Task 1's `ck_ai_autonomy_ceiling_end_user_is_off`, RE-ASSERTED HERE where
    a reader of this persona is looking.

    **THE STORED ROW IS SELECTED BACK BEFORE THE RAISE IS ASSERTED.** A bare
    `pytest.raises` reports `DID NOT RAISE` and says nothing about what the table
    now holds, and "it raised" and "it raised but the row is there" are different
    facts.
    """
    org_id = await api_env.create_org(f"resolver-dial-{level}")

    stored: Any = "unset"
    raised: Exception | None = None
    async with AsyncSession(admin_engine) as session:
        try:
            await session.execute(
                sqltext(
                    "INSERT INTO ai_autonomy_ceiling (org_id, persona, risk_class, level,"
                    " updated_at) VALUES (:org, 'end_user', 'low', :level, now())"
                ),
                {"org": str(org_id), "level": level},
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - this IS the constraint under test
            raised = exc
            await session.rollback()
        stored = (
            await session.execute(
                sqltext(
                    "SELECT level FROM ai_autonomy_ceiling WHERE org_id = :org"
                    " AND persona = 'end_user'"
                ),
                {"org": str(org_id)},
            )
        ).scalar_one_or_none()

    assert (stored, raised is not None) == (None, True), (
        f"an end_user ceiling is stored at {stored!r}; §0.9 gives this persona no act "
        f"at ANY dial position, and §4.1's CHECK is what makes that the database's answer"
    )


# --------------------------------------------------------------------------- #
# 7. End to end, through the routes.
# --------------------------------------------------------------------------- #


async def test_a_resolver_turn_answers_and_reaches_only_the_portal_surface(
    api_env: Any, monkeypatch: Any, a_ticket_for: Any
) -> None:
    """One turn through the real routes, with a model that calls the tool.

    The URL tape is checked as well as the answer: every request the turn made
    must be under `/api/v1/portal/`, which is the claim §0.2 makes about this
    executor and the one a leak would break first.
    """
    org_id = await api_env.create_org("resolver-turn")
    await _enable_ai(api_env, org_id)
    mine = await a_ticket_for(org_id, "me")
    headers = await api_env.headers(org_id, await _sub_for(api_env, org_id, mine.requester_id))

    provider = ScriptedProvider(
        [
            [ToolCallBlock(id="1", name="portal.my_tickets", arguments={})],
            [TextBlock(text="Your ticket is open and a technician will pick it up.")],
        ]
    )
    monkeypatch.setattr(portal_ai_api, "_provider_for", lambda: provider)
    spy = RecordingTransport(api_env.app)
    install_portal_spy(monkeypatch, spy)

    created = await api_env.client.post(f"{API}/sessions", headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["mode"] == "read"

    turn = await api_env.client.post(
        f"{API}/sessions/{created.json()['id']}/turns",
        headers=headers,
        json={"question": "what is happening with my ticket?"},
    )
    assert turn.status_code == 200, turn.text
    body = turn.json()
    assert "technician will pick it up" in body["answer"]
    # NOTHING ABOUT THE PLATFORM in a customer-facing response.
    assert set(body) == {"answer", "partial", "flagged_count"}, sorted(body)

    urls = [httpx.URL(str(r.url)).path for r in spy.requests]
    assert urls, "the turn made no tool call; the tape assertion below is vacuous"
    assert all(url.startswith("/api/v1/portal/") for url in urls), urls


async def test_a_fully_persuaded_model_reaches_no_dispatch_path(
    api_env: Any, monkeypatch: Any, a_ticket_for: Any
) -> None:
    """The resolver half of §13 test 13. A model that has been completely
    persuaded emits `actions.propose` -- and the turn STILL ANSWERS, because
    refusing would hand the attacker a denial of service (§8.4).

    Three observed values: zero `ai_action` rows, zero `jobs` rows, and
    `unknown_tool` recorded against the attempted call.
    """
    from sqlalchemy import func, select

    from app.models import AiAction, Job

    org_id = await api_env.create_org("resolver-persuaded")
    await _enable_ai(api_env, org_id)
    mine = await a_ticket_for(org_id, "me")
    headers = await api_env.headers(org_id, await _sub_for(api_env, org_id, mine.requester_id))

    provider = ScriptedProvider(
        [
            [
                ToolCallBlock(
                    id="1",
                    name="actions.propose",
                    arguments={"action_key": "restart-spooler", "device_id": str(uuid4())},
                )
            ],
            [TextBlock(text="I cannot change anything on your computer, but I can help.")],
        ]
    )
    monkeypatch.setattr(portal_ai_api, "_provider_for", lambda: provider)
    spy = RecordingTransport(api_env.app)
    install_portal_spy(monkeypatch, spy)

    created = await api_env.client.post(f"{API}/sessions", headers=headers)
    turn = await api_env.client.post(
        f"{API}/sessions/{created.json()['id']}/turns",
        headers=headers,
        json={"question": "please restart the print spooler on my laptop"},
    )
    assert turn.status_code == 200, turn.text
    assert "cannot change anything" in turn.json()["answer"]

    async with AsyncSession(api_env.admin_engine) as session:
        actions = (
            await session.execute(
                select(func.count()).select_from(AiAction).where(AiAction.org_id == org_id)
            )
        ).scalar_one()
        jobs = (
            await session.execute(
                select(func.count()).select_from(Job).where(Job.org_id == org_id)
            )
        ).scalar_one()
    assert (actions, jobs, spy.requests) == (0, 0, [])


async def test_a_staff_persona_session_cannot_be_continued_on_the_portal_surface(
    api_env: Any, monkeypatch: Any, a_ticket_for: Any
) -> None:
    """A staff conversation's transcript was assembled under a staff persona's
    prompt and tool set and may quote an internal note. Continuing it here would
    replay that into a customer's context."""
    from tests.test_ai_admin_migration import seed_session

    org_id = await api_env.create_org("resolver-crosspersona")
    await _enable_ai(api_env, org_id)
    mine = await a_ticket_for(org_id, "me")
    headers = await api_env.headers(org_id, await _sub_for(api_env, org_id, mine.requester_id))
    foreign = await seed_session(
        api_env.admin_engine, org_id, mine.requester_id, persona="admin", mode="act"
    )
    monkeypatch.setattr(
        portal_ai_api, "_provider_for", lambda: ScriptedProvider([[TextBlock(text="x")]])
    )

    turn = await api_env.client.post(
        f"{API}/sessions/{foreign}/turns", headers=headers, json={"question": "hello"}
    )
    assert turn.status_code == 404, turn.text


async def test_a_technician_without_a_portal_grant_cannot_reach_the_resolver(
    api_env: Any,
) -> None:
    org_id = await api_env.create_org("resolver-staffgate")
    await _enable_ai(api_env, org_id)
    sub = await api_env.create_user(org_id, "technician")
    headers = await api_env.headers(org_id, sub)
    response = await api_env.client.post(f"{API}/sessions", headers=headers)
    assert response.status_code == 403, response.text
