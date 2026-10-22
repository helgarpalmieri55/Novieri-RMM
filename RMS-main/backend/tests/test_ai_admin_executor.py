"""SECURITY GATE 2, part 2: what the executor sends, and what it never sends.

docs/modules/ai-admin/spec.md §4.4, §10.2, §10.5, §13 test 4.

EVERY ASSERTION HERE IS ON THE RECORDED REQUEST, NOT ON THE RESPONSE. §13 test 4
is about what went OUT: the exact method, the exact URL, the complete header
set, and -- for the malformed cases -- that NOTHING went out at all. A test that
asserted on the response could not tell "the executor refused to build the
request" from "the executor built it and the route rejected it", and those two
worlds differ by exactly the bug this file exists to catch.
"""

import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest

from app.core.ai.executor_staff import MAX_TOOL_CALL_SECONDS, ToolError, invoke_tool
from app.core.ai.tools import REGISTRY, ToolResult
from tests.ai_admin_fakes import (
    RecordingTransport,
    StaticStatusTransport,
    ai_user,
    install_spy,
)
from tests.health_fixtures import (
    HealthOrg,
    seed_alert,
    seed_assignment,
    seed_check,
    seed_org,
    seed_profile,
)


def _deadline() -> float:
    return time.monotonic() + 30.0


async def _estate(api_env: Any, alerts: int = 1) -> tuple[HealthOrg, str]:
    org = await seed_org(api_env, "aiexec")
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)
    for index in range(alerts):
        await seed_alert(
            api_env,
            org,
            profile_id=profile_id,
            check_id=check_id,
            selector=f"s{index}",
            opened_policy_version=1,
        )
    _, sub = await ai_user(api_env, org.org_id, "ai.use", "devices.read", "health.read")
    bearer = (await api_env.headers(org.org_id, sub))["Authorization"]
    return org, bearer


# --------------------------------------------------------------------------- #
# The request the executor builds (§4.4).
# --------------------------------------------------------------------------- #


async def test_the_request_is_a_get_to_the_tools_own_path_with_one_header(
    api_env: Any, monkeypatch: Any
) -> None:
    """The whole of §4.4 in one recorded request: the method, the path built
    from the tool's template, and the COMPLETE header set the executor added."""
    org, bearer = await _estate(api_env)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(org.device_id)},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )

    request = spy.only
    assert request.method == "GET"
    assert urlparse(str(request.url)).path == f"/api/v1/devices/{org.device_id}/health"
    assert request.headers["authorization"] == bearer
    # Only httpx's own transport headers survive beside it -- nothing this
    # module or the model contributed.
    added = set(request.headers) - {"host", "accept", "accept-encoding", "connection", "user-agent"}
    assert added == {"authorization"}


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param({"Authorization": "Bearer other"}, id="header-injection"),
        pytest.param({"authorization": "Bearer other"}, id="header-injection-lowercase"),
        pytest.param({"__path": "../credentials"}, id="path-override"),
        pytest.param({"url": "http://attacker.test/"}, id="url-override"),
        pytest.param({"device_id": "../../credentials"}, id="path-traversal"),
        pytest.param({"device_id": {"nested": "dict"}}, id="nested-dict"),
        pytest.param({"device_id": ["a", "list"]}, id="list-for-scalar"),
        pytest.param({"device_id": None}, id="null"),
    ],
)
async def test_no_model_output_can_change_the_request(
    api_env: Any, monkeypatch: Any, hostile: dict[str, Any]
) -> None:
    """§13 test 4. Each of these is something a persuaded model might emit.

    Every one must be refused by the CLOSED args model before a URL exists, so
    the spy records ZERO requests. Asserting zero is the point: an executor that
    dropped unknown fields and issued the call anyway would return a perfectly
    ordinary 200 and pass any assertion made on the response.
    """
    org, bearer = await _estate(api_env)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(org.device_id)} | hostile
        if "device_id" not in hostile
        else hostile,
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )

    assert isinstance(outcome, ToolError), f"{hostile!r} produced {outcome!r}"
    assert outcome.kind == "invalid_arguments"
    assert spy.requests == [], f"{hostile!r} reached the transport as {spy.requests!r}"


async def test_an_extra_query_parameter_is_refused_not_forwarded(
    api_env: Any, monkeypatch: Any
) -> None:
    """A field the args model does not declare must not become a query string.
    Separate from the case above because `limit` IS declared on the sibling
    tool, so "unknown field" and "wrong tool's field" are different mistakes."""
    org, bearer = await _estate(api_env)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(org.device_id), "limit": 5},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolError) and outcome.kind == "invalid_arguments"
    assert spy.requests == []


async def test_an_unknown_tool_name_never_becomes_a_request(
    api_env: Any, monkeypatch: Any
) -> None:
    """§8.3, step 3 of the named attack: a model that invents `credentials.list`
    gets a tool error, and NO HTTP REQUEST IS MADE. The spy's emptiness is the
    whole assertion -- the error message alone would also be produced by an
    executor that asked the platform first and got a 404."""
    org, bearer = await _estate(api_env)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="credentials.list",
        raw_args={},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolError) and outcome.kind == "unknown_tool"
    assert spy.requests == []


async def test_a_validation_failure_names_the_field(api_env: Any, monkeypatch: Any) -> None:
    """§10.5: the error tells the model exactly what was wrong so it can correct
    itself -- and the arguments are not repaired for it."""
    org, bearer = await _estate(api_env)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": "not-a-uuid"},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolError) and outcome.kind == "invalid_arguments"
    assert "device_id" in outcome.message
    assert spy.requests == []


# --------------------------------------------------------------------------- #
# The clamp (§10.2).
# --------------------------------------------------------------------------- #


async def test_an_enormous_limit_is_clamped_on_the_wire(api_env: Any, monkeypatch: Any) -> None:
    """§10.2. Asserted on the SPY'S RECORDED URL, not on the response: with only
    three alerts seeded, a response-side assertion would be satisfied by a
    request that asked for 100,000 rows and happened to get three."""
    org, bearer = await _estate(api_env, alerts=3)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={"limit": 100_000},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )

    query = parse_qs(urlparse(str(spy.only.url)).query)
    assert query["limit"] == [str(REGISTRY["health.alerts"].max_limit)]


async def test_a_small_limit_is_respected(api_env: Any, monkeypatch: Any) -> None:
    """The clamp is a CEILING, not a constant. A `min()` written as an
    assignment would pass the test above and fail here."""
    org, bearer = await _estate(api_env, alerts=3)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={"limit": 2},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    query = parse_qs(urlparse(str(spy.only.url)).query)
    assert query["limit"] == ["2"]
    assert isinstance(outcome, ToolResult) and outcome.result_count == 2


async def test_an_omitted_limit_is_still_clamped(api_env: Any, monkeypatch: Any) -> None:
    """The model does not have to ask for a limit to get one. Without this, a
    model that simply omits `limit` receives the route's own default of 200."""
    org, bearer = await _estate(api_env, alerts=1)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    query = parse_qs(urlparse(str(spy.only.url)).query)
    assert query["limit"] == [str(REGISTRY["health.alerts"].max_limit)]


# --------------------------------------------------------------------------- #
# Honest truncation (§10.2).
# --------------------------------------------------------------------------- #


async def test_a_full_page_reports_itself_truncated(api_env: Any, monkeypatch: Any) -> None:
    """§10.2: the model must not be able to present a page as the whole answer.
    Seeded with more rows than the ceiling asked for, so `truncated` is True
    for a real reason and not by construction."""
    org, bearer = await _estate(api_env, alerts=3)
    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={"limit": 2},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolResult)
    assert outcome.payload["returned"] == 2
    assert outcome.payload["truncated"] is True
    assert outcome.truncated is True


async def test_a_short_page_does_not_claim_truncation(api_env: Any) -> None:
    """The other half. A `truncated=True` constant would pass the test above."""
    org, bearer = await _estate(api_env, alerts=1)
    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={"limit": 10},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolResult)
    assert (outcome.payload["returned"], outcome.payload["truncated"]) == (1, False)


async def test_a_single_object_response_is_not_wrapped_in_items(api_env: Any) -> None:
    """A detail route returns the object itself. Wrapping it would change the
    shape the tool description promised the model."""
    org, bearer = await _estate(api_env)
    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(org.device_id)},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolResult)
    assert "items" not in outcome.payload
    assert (outcome.result_count, outcome.truncated) == (1, False)


# --------------------------------------------------------------------------- #
# Response mapping (§0.1, §10.4).
# --------------------------------------------------------------------------- #


async def test_a_401_is_distinguishable_and_is_not_retried(
    api_env: Any, monkeypatch: Any
) -> None:
    """§0.1: token expiry mid-conversation ends the turn with "your session
    expired, sign in and ask again". The conversation row survives and IT IS NOT
    RETRIED WITH A STALE TOKEN -- asserted on the spy's request count, because a
    retry would return the same error and be invisible in the outcome."""
    org, bearer = await _estate(api_env)
    spy = StaticStatusTransport(401)
    install_spy(monkeypatch, spy)

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolError)
    assert (outcome.kind, outcome.status) == ("session_expired", 401)
    assert len(spy.requests) == 1


async def test_a_5xx_fails_that_call_and_is_not_retried(api_env: Any, monkeypatch: Any) -> None:
    """§10.1: a single tool call failing does not end the turn."""
    org, bearer = await _estate(api_env)
    spy = StaticStatusTransport(503)
    install_spy(monkeypatch, spy)

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolError)
    assert (outcome.kind, outcome.status) == ("upstream_error", 503)
    assert len(spy.requests) == 1


async def test_a_timeout_becomes_a_tool_error(api_env: Any, monkeypatch: Any) -> None:
    org, bearer = await _estate(api_env)

    class _Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

    install_spy(monkeypatch, _Slow())
    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolError) and outcome.kind == "timeout"


async def test_an_expired_turn_deadline_makes_no_request(api_env: Any, monkeypatch: Any) -> None:
    """The whole-turn deadline bounds the LAST call too. Without this a turn
    that has already run out of time still pays for one more round trip."""
    org, bearer = await _estate(api_env)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={},
        app=api_env.app,
        bearer=bearer,
        deadline=time.monotonic() - 1.0,
    )
    assert isinstance(outcome, ToolError) and outcome.kind == "timeout"
    assert spy.requests == []


async def test_the_per_call_ceiling_is_ten_seconds() -> None:
    """§10.1's table, pinned so a later edit to the constant is a decision."""
    assert MAX_TOOL_CALL_SECONDS == 10.0


async def test_results_are_never_mutating(api_env: Any) -> None:
    """§12.2, on a real result rather than on a constructor default."""
    org, bearer = await _estate(api_env)
    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolResult) and outcome.mutating is False


async def test_a_uuid_argument_cannot_smuggle_a_path_segment(
    api_env: Any, monkeypatch: Any
) -> None:
    """Belt and braces for the traversal case: even a VALID uuid is
    percent-encoded into exactly one path segment, so the built path always has
    the shape the template promised."""
    org, bearer = await _estate(api_env)
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)
    device_id = uuid4()

    await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(device_id)},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    path = urlparse(str(spy.only.url)).path
    assert path.split("/") == ["", "api", "v1", "devices", str(device_id), "health"]


# --------------------------------------------------------------------------- #
# The executor is TOTAL: nothing a tool call can do escapes it (F3).
#
# `ASGITransport` RE-RAISES APPLICATION EXCEPTIONS. That is the fact these two
# tests exist for: the loopback is not a socket, so an exception raised inside
# the target route does not become a 500 response -- it propagates straight back
# through `client.get`, out of `invoke_tool`, out of `run_turn`, and out of the
# turn route, which then answers 500 having persisted NOTHING and audited
# NOTHING. Since a tool call's arguments are MODEL OUTPUT, and model output is
# influenced by retrieved text, that is a route to an unaudited read plus a
# per-turn denial of service.
# --------------------------------------------------------------------------- #


async def test_an_application_exception_from_the_loopback_becomes_a_tool_error(
    api_env: Any, monkeypatch: Any
) -> None:
    """The REAL case, through the real stack: a `str | None` argument carrying a
    NUL byte reaches Postgres, which rejects the byte sequence.

    Asserted as a returned value rather than with `pytest.raises`, because the
    bug's signature is an exception ESCAPING -- so the test has to name what
    comes back instead.
    """
    org, bearer = await _estate(api_env)

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={"state": "\x00open"},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )

    assert isinstance(outcome, ToolError), f"the exception escaped: {outcome!r}"
    assert outcome.kind == "upstream_error"
    assert outcome.tool == "health.alerts"
    # The model is told a lookup failed and NOTHING about the server's internals
    # -- no SQL, no stack, no connection string.
    assert "\x00" not in outcome.message
    assert "select" not in outcome.message.lower()


async def test_a_two_hundred_that_is_not_json_becomes_a_tool_error(
    api_env: Any, monkeypatch: Any
) -> None:
    """`response.json()` is the other unguarded line: a 200 carrying a proxy's
    HTML error page raises `JSONDecodeError` from OUTSIDE every `try`."""
    org, bearer = await _estate(api_env)

    class _Html(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>", request=request)

    install_spy(monkeypatch, _Html())
    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )

    assert isinstance(outcome, ToolError), f"the exception escaped: {outcome!r}"
    assert outcome.kind == "upstream_error"
    assert "not json" not in outcome.message, "the unreadable body is not quoted back"
