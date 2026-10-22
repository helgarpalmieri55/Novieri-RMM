"""`tools/list` and `tools/call` (MCP server plan, Task 8; design §4.2, §4.3, §5.3, §7).

What is pinned here:

- `tools/list` shows a person only the tools whose route permission they hold
  (or the few open to any signed-in user), narrowed by `?areas=`;
- `tools/call` runs the route IN-PROCESS through the whole API, as the person,
  with their own token and language: the result is the API's own JSON, the
  route's own audit event says `via: "mcp"`, and the API's own `detail` is the
  error text;
- a result with a secret-looking key that nobody reviewed is BLOCKED, whole;
- the per-person rate limit and the per-call timeout;
- every call, whatever its outcome, writes `mcp.tool_called` with a hash of
  the arguments and never the arguments.

Driven by the SDK's own client (`streamable_http_client` + `ClientSession`)
over `httpx2.ASGITransport` -- the same client an AI app runs, in-process --
and, for the 2026-07-28 single-exchange wire, by raw JSON-RPC.
"""

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import anyio
import httpx2
import pytest
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import LATEST_PROTOCOL_VERSION, CallToolResult
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_permission
from app.models import AuditEvent, Organization, Permission, Role, RolePermission, User, UserRole
from tests.mcp_support import mcp_client, mcp_token, running_mcp

BASE = "https://itms.example.com"
EMPTY_ARGS_SHA = hashlib.sha256(
    json.dumps({}, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


_DEVICES_READ = Depends(require_permission("devices.read"))
#: What the probe routes did, in order, for the tests that ask whether a route
#: ran to completion or was cancelled. Cleared by the fixture.
PROBE_EVENTS: list[str] = []


def _probe_router() -> APIRouter:
    """Routes that misbehave on purpose, for the outcomes no real route can be
    made to produce on demand: a 401 after the gate admitted the token, a route
    slower than the timeout, a result carrying an unreviewed secret-looking key.
    Gated like any route, so the catalogue makes tools of them."""
    router = APIRouter()

    # A literal route registered BEFORE a parameterised sibling: Starlette
    # dispatches `/mcp-probe/order/first` to the literal one, whatever tool
    # the request was built for.
    @router.get("/mcp-probe/order/first")
    async def order_first(
        ctx: AuthContext = _DEVICES_READ,
    ) -> dict[str, str]:
        PROBE_EVENTS.append("literal sibling ran")
        return {"route": "literal"}

    @router.get("/mcp-probe/order/{item}")
    async def order_item(
        item: str,
        ctx: AuthContext = _DEVICES_READ,
    ) -> dict[str, str]:
        return {"item": item}

    @router.get("/mcp-probe/unauthorised")
    async def unauthorised(
        ctx: AuthContext = _DEVICES_READ,
    ) -> dict[str, str]:
        raise HTTPException(401, "token expired")

    @router.get("/mcp-probe/slow")
    async def slow(
        ctx: AuthContext = _DEVICES_READ,
    ) -> dict[str, str]:
        try:
            await anyio.sleep(10)
        except anyio.get_cancelled_exc_class():
            PROBE_EVENTS.append("slow read cancelled")
            raise
        return {"never": "reached"}

    @router.post("/mcp-probe/slow-write")
    async def slow_write(
        ctx: AuthContext = _DEVICES_READ,
    ) -> dict[str, bool]:
        try:
            await anyio.sleep(0.5)
        except anyio.get_cancelled_exc_class():
            PROBE_EVENTS.append("slow write cancelled")
            raise
        PROBE_EVENTS.append("slow write completed")
        return {"done": True}

    @router.get("/mcp-probe/text")
    async def text(
        ctx: AuthContext = _DEVICES_READ,
    ) -> PlainTextResponse:
        return PlainTextResponse('{"looks": "like json"}')

    @router.get("/mcp-probe/secret-detail")
    async def secret_detail(
        ctx: AuthContext = _DEVICES_READ,
    ) -> dict[str, str]:
        raise HTTPException(409, detail={"conflict": "x", "password": "hunter2"})

    @router.get("/mcp-probe/scalar")
    async def scalar(
        ctx: AuthContext = _DEVICES_READ,
    ) -> int:
        return 42

    @router.get("/mcp-probe/leaky")
    async def leaky(
        ctx: AuthContext = _DEVICES_READ,
    ) -> dict[str, Any]:
        return {"name": "printer", "nested": {"api_token": "x"}}

    @router.get("/mcp-probe/listing")
    async def listing(
        ctx: AuthContext = _DEVICES_READ,
    ) -> list[dict[str, int]]:
        return [{"a": 1}, {"a": 2}]

    @router.get("/mcp-probe/empty", status_code=204)
    async def empty(
        ctx: AuthContext = _DEVICES_READ,
    ) -> None:
        return None

    return router


@pytest.fixture()
async def mcp_env(api_env):
    """`api_env` with `ITS_PUBLIC_CONSOLE_URL` set and the probe routes added
    BEFORE the MCP server starts, so the catalogue it builds includes them."""
    api_env.settings.public_console_url = BASE
    api_env.app.include_router(_probe_router(), prefix="/api/v1")
    PROBE_EVENTS.clear()
    return api_env


async def _org(env: Any) -> tuple[UUID, str]:
    org_id = await env.create_org("mcp")
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(Organization)
            .where(Organization.id == org_id)
            .values(settings={"mcp_enabled": True})
        )
        await session.commit()
    return org_id, await env.org_slug(org_id)


async def _user_id(env: Any, sub: str) -> UUID:
    async with AsyncSession(env.admin_engine) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _grant(env: Any, org_id: UUID, sub: str, *codes: str) -> None:
    """Give an existing user an org role holding `codes` (here: `mcp.use` for
    a technician, who does not hold it by default)."""
    user_id = await _user_id(env, sub)
    async with AsyncSession(env.admin_engine) as session:
        role = Role(org_id=org_id, name=f"mcp-{uuid4().hex[:6]}")
        session.add(role)
        await session.flush()
        for code in codes:
            perm = (
                await session.execute(select(Permission.id).where(Permission.code == code))
            ).scalar_one()
            session.add(RolePermission(role_id=role.id, permission_id=perm))
        session.add(UserRole(org_id=org_id, user_id=user_id, role_id=role.id))
        await session.commit()


async def _events(env: Any, org_id: UUID, action: str) -> list[AuditEvent]:
    async with AsyncSession(env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id, AuditEvent.action == action)
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )


@asynccontextmanager
async def _session(
    env: Any, token: str, *, query: str = "", language: str | None = None
) -> AsyncIterator[ClientSession]:
    """The SDK's own client, initialised, over an in-process transport."""
    headers = {"Authorization": f"Bearer {token}"}
    if language is not None:
        headers["Accept-Language"] = language
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=env.app), base_url=BASE, headers=headers
    )
    async with (
        http,
        streamable_http_client(f"{BASE}/mcp{query}", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def _admin(env: Any) -> tuple[UUID, str, str]:
    org_id, slug = await _org(env)
    sub = await env.create_user(org_id, "org-admin")
    return org_id, sub, mcp_token(env, slug, sub)


def _error_text(result: CallToolResult) -> str:
    assert result.is_error is True, result
    assert result.structured_content is None
    assert len(result.content) == 1
    return result.content[0].text  # type: ignore[union-attr]


async def _fresh_window(
    env: Any, session: ClientSession, sub: str, *, list_first: bool = True
) -> None:
    """Start the person's rate window afresh, so a test counts only the
    requests it makes.

    EVERY ADMITTED REQUEST COUNTS, and opening a session makes two:
    `initialize`, and `notifications/initialized`, which the SDK client sends
    AFTER `initialize` returns, so it can land after anything the test does
    next. One more request on the same session is answered only after it, so
    that request is made -- a listing, which the SDK client also wants before
    its first call to learn output schemas, or a ping -- under a limit no test
    reaches, and only then is the window cleared."""
    import app.mcp.server as server

    limit = server.MCP_CALLS_PER_MINUTE
    server.MCP_CALLS_PER_MINUTE = 1_000_000
    try:
        if list_first:
            await session.list_tools()
        else:
            await session.send_ping()
    finally:
        server.MCP_CALLS_PER_MINUTE = limit
    await env.app.state.redis.delete(f"mcp:rate:user:{await _user_id(env, sub)}")


def _probe_tool(env: Any, path: str) -> str:
    catalog = env.app.state.mcp_catalog
    return next(t.name for t in catalog.values() if t.path == f"/api/v1/mcp-probe/{path}")


# --------------------------------------------------------------------------- #
# 1-3. tools/list
# --------------------------------------------------------------------------- #


async def test_an_org_admin_lists_tools_with_schemas_annotations_and_descriptions(
    mcp_env,
) -> None:
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        tools = (await session.list_tools()).tools
    names = {t.name for t in tools}
    assert "devices_list" in names
    assert "me_get" in names  # open to any signed-in user
    for tool in tools:
        if tool.name.startswith("mcp_probe"):
            continue  # the test's own probes have no description
        assert tool.input_schema["type"] == "object", tool.name
        assert tool.annotations is not None and tool.annotations.read_only_hint is not None
        assert tool.description, tool.name
    devices_list = next(t for t in tools if t.name == "devices_list")
    assert devices_list.annotations is not None
    assert devices_list.annotations.read_only_hint is True
    assert not [n for n in names if n.startswith("credentials")]
    # Every tool shown is one the catalogue holds.
    assert names <= set(mcp_env.app.state.mcp_catalog)


async def test_a_technician_does_not_see_or_call_an_org_admin_tool(mcp_env) -> None:
    org_id, slug = await _org(mcp_env)
    sub = await mcp_env.create_user(org_id, "technician")
    await _grant(mcp_env, org_id, sub, "mcp.use")
    token = mcp_token(mcp_env, slug, sub)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        catalog = mcp_env.app.state.mcp_catalog
        assert catalog["roles_create"].permission == "roles.manage"
        names = {t.name for t in (await session.list_tools()).tools}
        assert "devices_list" in names  # non-vacuity: the technician sees tools
        assert "roles_create" not in names
        result = await session.call_tool("roles_create", {"body": {"name": "x"}})
    assert "requires roles.manage" in _error_text(result)
    # Refused before the route: no role was created, and the refusal is audited.
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["tool"] == "roles_create"
    assert event.meta["status"] == 403
    assert not await _events(mcp_env, org_id, "role.created")


async def test_areas_narrow_the_list_and_what_is_callable(mcp_env) -> None:
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token, query="?areas=devices") as session:
        tools = (await session.list_tools()).tools
        catalog = mcp_env.app.state.mcp_catalog
        outside = await session.call_tool("me_get", {})
    assert tools
    assert {catalog[t.name].area for t in tools} == {"devices"}
    assert "there is no tool named me_get" in _error_text(outside)


# --------------------------------------------------------------------------- #
# 4-5. Calls run as the person, through the whole API
# --------------------------------------------------------------------------- #


async def test_a_read_call_returns_the_apis_own_json_and_is_audited(mcp_env) -> None:
    org_id, sub, token = await _admin(mcp_env)
    console = await mcp_env.client.get(
        "/api/v1/devices", headers=await mcp_env.headers(org_id, sub)
    )
    assert console.status_code == 200, console.text
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("devices_list", {})
    assert result.is_error is False
    assert result.structured_content == console.json()
    assert json.loads(result.content[0].text) == console.json()  # type: ignore[union-attr]
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.actor_type == "user"
    assert event.actor_id == await _user_id(mcp_env, sub)
    assert event.meta["tool"] == "devices_list"
    assert event.meta["app"] == "claude"
    assert event.meta["status"] == 200
    assert event.meta["args_sha256"] == EMPTY_ARGS_SHA
    assert "args" not in event.meta
    # The tool-call event is itself written on the MCP channel.
    assert event.meta["via"] == "mcp"
    assert event.meta["mcp_app"] == "claude"


async def test_a_list_payload_is_wrapped_and_a_204_is_an_empty_object(mcp_env) -> None:
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        listing = await session.call_tool(_probe_tool(mcp_env, "listing"), {})
        empty = await session.call_tool(_probe_tool(mcp_env, "empty"), {})
    assert listing.is_error is False
    assert listing.structured_content == {"items": [{"a": 1}, {"a": 2}]}
    assert json.loads(listing.content[0].text) == [{"a": 1}, {"a": 2}]  # type: ignore[union-attr]
    assert empty.is_error is False
    assert empty.structured_content == {}


async def test_a_write_call_carries_the_mcp_channel_into_the_routes_own_audit(
    mcp_env,
) -> None:
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("me_update", {"body": {"timezone": "UTC"}})
    assert result.is_error is False, result
    assert result.structured_content is not None
    assert result.structured_content["timezone"] == "UTC"
    (event,) = await _events(mcp_env, org_id, "user.profile_updated")
    assert event.meta["fields"] == ["timezone"]
    assert event.meta["via"] == "mcp"
    assert event.meta["mcp_app"] == "claude"
    (called,) = await _events(mcp_env, org_id, "mcp.tool_called")
    expected = hashlib.sha256(
        json.dumps({"body": {"timezone": "UTC"}}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert called.meta["args_sha256"] == expected
    assert "UTC" not in json.dumps(called.meta)


# --------------------------------------------------------------------------- #
# 6. The scrub blocks, whole
# --------------------------------------------------------------------------- #


async def test_an_unreviewed_secret_looking_key_blocks_the_whole_result(mcp_env) -> None:
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        tool = _probe_tool(mcp_env, "leaky")
        result = await session.call_tool(tool, {})
    text = _error_text(result)
    assert text == "blocked: the result contained a secret-looking field at nested.api_token"
    assert "printer" not in text
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["status"] == 200
    assert event.meta["blocked"] is True


# --------------------------------------------------------------------------- #
# 7. Status mapping: the API's own detail
# --------------------------------------------------------------------------- #


async def test_a_404_is_not_found_with_the_apis_detail(mcp_env) -> None:
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("devices_get", {"device_id": str(uuid4())})
    assert _error_text(result) == "not found: no such device"
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["status"] == 404


async def test_a_409_is_the_apis_detail(mcp_env) -> None:
    org_id, sub, token = await _admin(mcp_env)
    console = await mcp_env.client.post(
        "/api/v1/roles",
        json={"name": "technician"},
        headers=await mcp_env.headers(org_id, sub),
    )
    assert console.status_code == 409
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("roles_create", {"body": {"name": "technician"}})
    assert _error_text(result) == console.json()["detail"]
    assert _error_text(result).startswith("a role named 'technician' already exists")


async def test_a_422_from_the_api_is_invalid_arguments_with_its_detail(mcp_env) -> None:
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("roles_update", {"role_id": str(uuid4()), "body": {}})
    assert _error_text(result) == (
        "invalid arguments: no fields supplied; send name and/or description"
    )


async def test_arguments_that_fail_the_schema_never_reach_the_route(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.invoke as invoke_mod

    org_id, _, token = await _admin(mcp_env)
    made: list[Any] = []
    real_marked = invoke_mod._marked
    monkeypatch.setattr(invoke_mod, "_marked", lambda *a: made.append(a) or real_marked(*a))
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("devices_list", {"limit": "many", "nope": 1})
    text = _error_text(result)
    assert text.startswith("invalid arguments: ")
    assert made == []
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["status"] == 422


async def test_a_401_from_the_api_says_re_authenticate(mcp_env) -> None:
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool(_probe_tool(mcp_env, "unauthorised"), {})
    assert _error_text(result).startswith("re-authenticate")


async def test_an_unknown_tool_is_an_error_and_is_audited(mcp_env) -> None:
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("no_such_tool", {"x": 1})
    assert _error_text(result) == "there is no tool named no_such_tool"
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["tool"] == "no_such_tool"
    assert event.meta["status"] == 404


# --------------------------------------------------------------------------- #
# 8-9. Rate limit and timeout
# --------------------------------------------------------------------------- #


async def test_the_fourth_call_over_a_limit_of_three_is_refused_before_the_route(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.invoke as invoke_mod
    import app.mcp.server as server

    monkeypatch.setattr(server, "MCP_CALLS_PER_MINUTE", 3)
    org_id, sub, token = await _admin(mcp_env)
    made: list[Any] = []
    real_marked = invoke_mod._marked
    monkeypatch.setattr(invoke_mod, "_marked", lambda *a: made.append(a) or real_marked(*a))
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        await _fresh_window(mcp_env, session, sub)
        results = [await session.call_tool("me_get", {}) for _ in range(4)]
    assert [r.is_error for r in results] == [False, False, False, True]
    assert len(made) == 3  # the fourth never reached the API
    text = _error_text(results[3])
    assert "at most 3 requests per minute" in text
    assert "try again in" in text
    events = await _events(mcp_env, org_id, "mcp.tool_called")
    assert [e.meta["status"] for e in events] == [200, 200, 200, 429]


async def test_a_redis_failure_fails_open(mcp_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from redis.exceptions import RedisError

    class _Down:
        async def incr(self, key: str) -> int:
            raise RedisError("down")

    _, _, token = await _admin(mcp_env)
    monkeypatch.setattr(mcp_env.app.state, "redis", _Down())
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("me_get", {})
    assert result.is_error is False


async def test_a_call_slower_than_the_timeout_is_an_error(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.invoke as invoke_mod

    monkeypatch.setattr(invoke_mod, "TOOL_TIMEOUT_S", 0.05)
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        with anyio.fail_after(5):
            result = await session.call_tool(_probe_tool(mcp_env, "slow"), {})
    assert "took longer than" in _error_text(result)
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["status"] == 504


async def test_a_timed_out_write_is_not_cancelled_and_its_outcome_is_unknown(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.invoke as invoke_mod

    monkeypatch.setattr(invoke_mod, "TOOL_TIMEOUT_S", 0.05)
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        with anyio.fail_after(5):
            result = await session.call_tool(_probe_tool(mcp_env, "slow-write"), {})
        assert _error_text(result) == (
            "the action is still running after 0.05 seconds and its outcome is unknown; "
            "check before retrying"
        )
        assert PROBE_EVENTS == []  # the answer came before the write finished...
        with anyio.fail_after(5):
            while not PROBE_EVENTS:
                await anyio.sleep(0.02)
    # ...and the write ran to completion afterwards, uncancelled.
    assert PROBE_EVENTS == ["slow write completed"]
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["status"] == 504
    assert event.meta["outcome_unknown"] is True


async def test_a_timed_out_read_is_cancelled(mcp_env, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.mcp.invoke as invoke_mod

    monkeypatch.setattr(invoke_mod, "TOOL_TIMEOUT_S", 0.05)
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        with anyio.fail_after(5):
            result = await session.call_tool(_probe_tool(mcp_env, "slow"), {})
    assert _error_text(result) == "the action took longer than 0.05 seconds"
    assert PROBE_EVENTS == ["slow read cancelled"]
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert "outcome_unknown" not in event.meta


# --------------------------------------------------------------------------- #
# Fix round 1: path values, content type, error details, failures, limits
# --------------------------------------------------------------------------- #


def _spy_marked(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import app.mcp.invoke as invoke_mod

    made: list[Any] = []
    real = invoke_mod._marked
    monkeypatch.setattr(invoke_mod, "_marked", lambda *a: made.append(a) or real(*a))
    return made


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        # The reviewer's cases: a value that, decoded, adds a path segment and
        # reaches a never-exposed route or a sibling tool.
        ("licenses_get", {"license_id": f"{uuid4()}/key"}),
        ("ipam_subnets_set", {"subnet_id": f"{uuid4()}/sweep-settings", "body": {}}),
        ("devices_get", {"device_id": "a\\b"}),
        ("devices_get", {"device_id": ".."}),
        ("devices_get", {"device_id": "."}),
        ("devices_get", {"device_id": ""}),
    ],
)
async def test_a_path_value_that_could_change_the_path_is_refused_before_any_request(
    mcp_env, monkeypatch: pytest.MonkeyPatch, tool: str, args: dict[str, Any]
) -> None:
    made = _spy_marked(monkeypatch)
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool(tool, args)
    assert _error_text(result).startswith("invalid arguments: ")
    assert made == []
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["status"] == 422
    # Nothing reached a license key or a subnet's sweep settings.
    assert not await _events(mcp_env, org_id, "license.key_revealed")


async def test_the_wrapper_refuses_a_request_whose_path_is_not_the_tools(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: with the argument check switched off, the in-process
    wrapper still refuses a decoded path that does not match the tool's own
    template, without calling the app."""
    import app.mcp.invoke as invoke_mod

    monkeypatch.setattr(invoke_mod, "path_value_problems", lambda tool, args: [])
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("licenses_get", {"license_id": f"{uuid4()}/key"})
    assert _error_text(result).startswith("invalid arguments: ")
    assert not await _events(mcp_env, org_id, "license.key_revealed")


async def test_the_wrapper_checks_the_method_and_never_calls_the_app_on_a_mismatch() -> None:
    from app.core.mcp_channel import McpCall
    from app.mcp.catalog import build_catalog
    from app.mcp.invoke import _marked
    from tests.tool_surface import build_router_only_app

    catalog = build_catalog(build_router_only_app())
    called: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        called.append(scope)

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    async def receive() -> Any:
        return {"type": "http.request", "body": b""}

    wrapper = _marked(app, McpCall(azp="claude", tool="devices_get"), catalog["devices_get"])
    device = str(uuid4())
    base = {"type": "http", "path": f"/api/v1/devices/{device}", "headers": []}
    await wrapper({**base, "method": "DELETE"}, receive, send)
    await wrapper({**base, "method": "GET", "path": f"/api/v1/devices/{device}/x"}, receive, send)
    assert called == []
    assert wrapper.refused is True
    await wrapper({**base, "method": "GET"}, receive, send)
    assert len(called) == 1


async def test_a_2xx_that_is_not_application_json_is_blocked(mcp_env) -> None:
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool(_probe_tool(mcp_env, "text"), {})
    assert _error_text(result) == (
        "the action returned something other than JSON, which an AI app cannot receive"
    )
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["blocked"] is True


async def test_a_scalar_payload_is_wrapped_as_value(mcp_env) -> None:
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool(_probe_tool(mcp_env, "scalar"), {})
    assert result.is_error is False
    assert result.structured_content == {"value": 42}


async def test_an_error_detail_with_a_secret_looking_key_is_withheld(mcp_env) -> None:
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool(_probe_tool(mcp_env, "secret-detail"), {})
    text = _error_text(result)
    assert text == "the error's details were withheld because they contained a secret-looking field"
    assert "hunter2" not in text


async def test_an_unexpected_failure_is_audited_as_500_and_says_nothing_of_it(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.invoke as invoke_mod

    async def boom(**kwargs: Any) -> Any:
        raise RuntimeError("internal secret text")

    monkeypatch.setattr(invoke_mod, "invoke", boom)
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("me_get", {})
    text = _error_text(result)
    assert text == "the platform could not complete this action (status 500)"
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["status"] == 500


async def test_an_invalid_tool_name_is_not_stored_in_the_audit(mcp_env) -> None:
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        await session.call_tool("Robert'); DROP TABLE--", {})
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["tool"] == "<invalid>"


async def test_tools_list_counts_toward_the_limit_and_is_not_audited(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.server as server

    monkeypatch.setattr(server, "MCP_CALLS_PER_MINUTE", 2)
    org_id, sub, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        await _fresh_window(mcp_env, session, sub, list_first=False)
        await session.list_tools()
        await session.list_tools()
        with pytest.raises(MCPError) as refused:
            await session.list_tools()
        call = await session.call_tool("me_get", {})
    assert "try again in" in refused.value.error.message
    assert "at most 2" in _error_text(call)
    events = await _events(mcp_env, org_id, "mcp.tool_called")
    assert [e.meta["status"] for e in events] == [429]


async def test_only_the_first_refused_call_in_a_window_is_audited(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.server as server

    monkeypatch.setattr(server, "MCP_CALLS_PER_MINUTE", 1)
    org_id, sub, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        await _fresh_window(mcp_env, session, sub)
        results = [await session.call_tool("me_get", {}) for _ in range(4)]
    assert [r.is_error for r in results] == [False, True, True, True]
    events = await _events(mcp_env, org_id, "mcp.tool_called")
    assert [e.meta["status"] for e in events] == [200, 429]


async def test_every_authenticated_request_counts_toward_the_limit(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review 5b. The limit bounds what an AI app can make this server
    do, and every request that passes the gate has cost it the gate's reads.
    So a ping counts as a call does: two pings fill a limit of two, and the
    call after them is refused (and audited, as the first refusal)."""
    import app.mcp.server as server

    monkeypatch.setattr(server, "MCP_CALLS_PER_MINUTE", 2)
    org_id, sub, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        await _fresh_window(mcp_env, session, sub)
        await session.send_ping()
        await session.send_ping()
        call = await session.call_tool("me_get", {})
    assert "at most 2 requests per minute" in _error_text(call)
    events = await _events(mcp_env, org_id, "mcp.tool_called")
    assert [e.meta["status"] for e in events] == [429]


async def test_a_request_is_counted_exactly_once(mcp_env) -> None:
    """Counted at the door, never again in a handler: one call is one."""
    _, sub, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        await _fresh_window(mcp_env, session, sub)
        await session.call_tool("me_get", {})
        await session.list_tools()
        count = await mcp_env.app.state.redis.get(f"mcp:rate:user:{await _user_id(mcp_env, sub)}")
    assert int(count) == 2


async def test_a_rate_key_left_without_an_expiry_gets_one(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key whose EXPIRE never landed (a crash between INCR and EXPIRE) would
    otherwise never reset: the person would be locked out for good."""
    org_id, sub, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        user_id = await _user_id(mcp_env, sub)
        key = f"mcp:rate:user:{user_id}"
        redis = mcp_env.app.state.redis
        await redis.set(key, 5)  # no TTL
        await session.call_tool("me_get", {})
        ttl = await redis.ttl(key)
    assert 0 < ttl <= 60


# --------------------------------------------------------------------------- #
# Fix round 2: a tool reaches exactly its own route; late writes are bounded
# --------------------------------------------------------------------------- #


async def test_a_value_that_is_not_the_declared_format_is_refused_before_any_request(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's probe: `expense_id: "export"` would be dispatched to
    `GET /budget/expenses/export`. The format checker refuses it first."""
    made = _spy_marked(monkeypatch)
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool("budget_expenses_get", {"expense_id": "export"})
    assert _error_text(result).startswith("invalid arguments: ")
    assert made == []


async def test_the_guard_refuses_a_request_dispatched_to_another_route(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the format check off, the same probe reaches the API, and the
    never-exposed guard sees that Starlette would dispatch it to the export,
    not to the tool's own route: refused before the router, so the export
    never runs."""
    import app.mcp.invoke as invoke_mod

    monkeypatch.setattr(invoke_mod, "_FORMAT_CHECKER", None)
    org_id, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        refused = await session.call_tool("budget_expenses_get", {"expense_id": "export"})
        valid = await session.call_tool("budget_expenses_get", {"expense_id": str(uuid4())})
    assert _error_text(refused) == "this tool cannot reach that route"
    assert not await _events(mcp_env, org_id, "budget.export")
    # The tool's own route is still reached: its own 404, not the guard's 403.
    assert _error_text(valid).startswith("not found: ")


async def test_a_literal_sibling_registered_first_is_not_reached(mcp_env) -> None:
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        tool = _probe_tool(mcp_env, "order/{item}")
        refused = await session.call_tool(tool, {"item": "first"})
        valid = await session.call_tool(tool, {"item": "second"})
    assert _error_text(refused) == "this tool cannot reach that route"
    assert PROBE_EVENTS == []  # the literal route's handler never ran
    assert valid.is_error is False
    assert valid.structured_content == {"item": "second"}


async def test_a_marker_whose_route_matches_nothing_is_refused(mcp_env) -> None:
    from app.core.mcp_channel import EXTENSION_KEY

    org_id, sub, token = await _admin(mcp_env)

    def marked(route: str) -> Any:
        async def wrapper(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                scope = {
                    **scope,
                    "extensions": {EXTENSION_KEY: {"azp": "claude", "tool": "t", "route": route}},
                }
            await mcp_env.app(scope, receive, send)

        return wrapper

    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    async with running_mcp(mcp_env):  # builds the dispatch table
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=marked("GET /api/v1/no-such-route")),
            base_url="http://mcp.test",
        ) as client:
            nowhere = await client.get("/api/v1/no-such-route", headers=headers)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=marked("GET /api/v1/me")),
            base_url="http://mcp.test",
        ) as client:
            own = await client.get("/api/v1/me", headers=headers)
            wrong_method = await client.patch(
                "/api/v1/me", json={"timezone": "UTC"}, headers=headers
            )
    assert nowhere.status_code == 403
    assert nowhere.json()["detail"] == "this tool cannot reach that route"
    assert own.status_code == 200, own.text
    assert wrong_method.status_code == 403
    assert not await _events(mcp_env, org_id, "user.profile_updated")


async def test_the_route_refusal_is_in_the_requests_language(mcp_env) -> None:
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token, language="es") as session:
        tool = _probe_tool(mcp_env, "order/{item}")
        refused = await session.call_tool(tool, {"item": "first"})
    assert _error_text(refused) == "esta herramienta no puede llegar a esa ruta"


async def test_a_late_write_is_cancelled_at_the_hard_cap(
    mcp_env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import app.mcp.invoke as invoke_mod

    monkeypatch.setattr(invoke_mod, "TOOL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(invoke_mod, "WRITE_HARD_CAP_S", 0.2)
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        result = await session.call_tool(_probe_tool(mcp_env, "slow-write"), {})
        assert "outcome is unknown" in _error_text(result)
        with anyio.fail_after(5):
            while not PROBE_EVENTS:
                await anyio.sleep(0.02)
    assert PROBE_EVENTS == ["slow write cancelled"]
    assert any(
        r.getMessage() == "mcp write tool call cancelled at the hard cap" for r in caplog.records
    )


async def test_a_late_write_logs_its_final_status(
    mcp_env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    import app.mcp.invoke as invoke_mod

    caplog.set_level(logging.INFO, logger="app.mcp.invoke")
    monkeypatch.setattr(invoke_mod, "TOOL_TIMEOUT_S", 0.05)
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token) as session:
        tool = _probe_tool(mcp_env, "slow-write")
        await session.call_tool(tool, {})
        with anyio.fail_after(5):
            while not [r for r in caplog.records if "finished after" in r.getMessage()]:
                await anyio.sleep(0.02)
    (late,) = [r for r in caplog.records if "finished after" in r.getMessage()]
    assert late.levelname == "INFO"
    assert late.tool == tool  # type: ignore[attr-defined]
    assert late.status == 200  # type: ignore[attr-defined]


async def test_the_audit_row_is_written_even_when_the_handler_is_cancelled(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that disconnects cancels the handler. The `mcp.tool_called`
    row is written all the same, shielded, as 499 with an unknown outcome."""
    import app.mcp.invoke as invoke_mod
    import app.mcp.server as server
    from app.core.security import TokenClaims

    started = anyio.Event()

    async def hang(**kwargs: Any) -> Any:
        started.set()
        await anyio.sleep(10)

    monkeypatch.setattr(invoke_mod, "invoke", hang)
    org_id, sub, _ = await _admin(mcp_env)
    principal = server.McpPrincipal(
        org_id=org_id,
        user_id=await _user_id(mcp_env, sub),
        claims=TokenClaims(sub=sub, email="", name="", org_slug="", azp="claude"),
        bearer="Bearer x",
        areas=None,
        accept_language="",
    )
    ctx = type("Ctx", (), {})()
    ctx.request = type("Req", (), {"scope": {"state": {server.PRINCIPAL_KEY: principal}}})()
    params = type("Params", (), {"name": "me_get", "arguments": {}})()
    handlers = server._ToolHandlers(mcp_env.app)
    async with running_mcp(mcp_env):
        async with anyio.create_task_group() as tg:
            tg.start_soon(handlers.call_tool, ctx, params)
            with anyio.fail_after(5):
                await started.wait()
            tg.cancel_scope.cancel()
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["status"] == 499
    assert event.meta["outcome_unknown"] is True


def _direct_call(env: Any, org_id: UUID, user_id: UUID, sub: str) -> tuple[Any, Any, Any]:
    """`(handlers, ctx, params)` for calling the SDK handler directly, with a
    principal the gate would have admitted."""
    import app.mcp.server as server
    from app.core.security import TokenClaims

    principal = server.McpPrincipal(
        org_id=org_id,
        user_id=user_id,
        claims=TokenClaims(sub=sub, email="", name="", org_slug="", azp="claude"),
        bearer="Bearer x",
        areas=None,
        accept_language="",
    )
    ctx = type("Ctx", (), {})()
    ctx.request = type("Req", (), {"scope": {"state": {server.PRINCIPAL_KEY: principal}}})()
    params = type("Params", (), {"name": "me_get", "arguments": {}})()
    return server._ToolHandlers(env.app), ctx, params


async def test_a_stuck_audit_write_is_abandoned_at_its_deadline(
    mcp_env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Final review 5c. The `mcp.tool_called` write is shielded, so nothing
    outside can cancel it -- and so a database that never answers would hold
    the handler, and shutdown, for ever. It has a deadline of its own inside
    the shield, and an abandoned write is logged."""
    import logging

    import app.mcp.invoke as invoke_mod
    import app.mcp.server as server

    async def done(**kwargs: Any) -> Any:
        return invoke_mod.InvokeOutcome(200, {"ok": True}, None)

    async def stuck(self: Any, *args: Any) -> None:
        await anyio.sleep(30)

    caplog.set_level(logging.ERROR, logger="app.mcp.server")
    monkeypatch.setattr(server, "_AUDIT_DEADLINE_S", 0.05)
    monkeypatch.setattr(invoke_mod, "invoke", done)
    monkeypatch.setattr(server._ToolHandlers, "_audit", stuck)
    org_id, sub, _ = await _admin(mcp_env)
    handlers, ctx, params = _direct_call(mcp_env, org_id, await _user_id(mcp_env, sub), sub)
    async with running_mcp(mcp_env):
        with anyio.fail_after(5):
            result = await handlers.call_tool(ctx, params)
    assert result.is_error is False
    (record,) = [r for r in caplog.records if "abandoned" in r.getMessage()]
    assert record.levelname == "ERROR"
    assert record.tool == "me_get"  # type: ignore[attr-defined]
    assert record.status == 200  # type: ignore[attr-defined]


async def test_a_cancelled_call_with_a_stuck_audit_write_still_ends(
    mcp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shield keeps the client's disconnect from skipping the audit row;
    the deadline keeps the shield from outliving a database that hangs."""
    import app.mcp.invoke as invoke_mod
    import app.mcp.server as server

    started = anyio.Event()

    async def hang(**kwargs: Any) -> Any:
        started.set()
        await anyio.sleep(30)

    async def stuck(self: Any, *args: Any) -> None:
        await anyio.sleep(30)

    monkeypatch.setattr(server, "_AUDIT_DEADLINE_S", 0.05)
    monkeypatch.setattr(invoke_mod, "invoke", hang)
    monkeypatch.setattr(server._ToolHandlers, "_audit", stuck)
    org_id, sub, _ = await _admin(mcp_env)
    handlers, ctx, params = _direct_call(mcp_env, org_id, await _user_id(mcp_env, sub), sub)
    async with running_mcp(mcp_env):
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(handlers.call_tool, ctx, params)
                await started.wait()
                tg.cancel_scope.cancel()


# --------------------------------------------------------------------------- #
# 10. The person's language
# --------------------------------------------------------------------------- #


async def test_errors_are_in_the_persons_language(mcp_env) -> None:
    _, _, token = await _admin(mcp_env)
    async with running_mcp(mcp_env), _session(mcp_env, token, language="es") as session:
        missing = await session.call_tool("devices_get", {"device_id": str(uuid4())})
        unknown = await session.call_tool("no_such_tool", {})
    # The API's own Spanish detail, inside the handler's own Spanish frame.
    assert _error_text(missing) == "no encontrado: no existe ese dispositivo"
    assert _error_text(unknown) == "no existe ninguna herramienta llamada no_such_tool"


# --------------------------------------------------------------------------- #
# The 2026-07-28 single-exchange wire
# --------------------------------------------------------------------------- #


async def test_list_and_call_work_on_the_2026_wire(mcp_env) -> None:
    org_id, _, token = await _admin(mcp_env)
    meta = {
        PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {},
    }

    def headers(method: str) -> dict[str, str]:
        return {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
            "Mcp-Method": method,
        }

    async with running_mcp(mcp_env), mcp_client(mcp_env) as client:
        listed = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}},
            headers=headers("tools/list"),
        )
        called = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"_meta": meta, "name": "devices_list", "arguments": {"limit": 5}},
            },
            headers={**headers("tools/call"), "Mcp-Name": "devices_list"},
        )
    assert listed.status_code == 200, listed.text
    assert "devices_list" in {t["name"] for t in listed.json()["result"]["tools"]}
    assert called.status_code == 200, called.text
    result = called.json()["result"]
    assert result["isError"] is False, result
    assert result["structuredContent"]["items"] == []
    (event,) = await _events(mcp_env, org_id, "mcp.tool_called")
    assert event.meta["status"] == 200
