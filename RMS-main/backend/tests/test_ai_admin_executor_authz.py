"""SECURITY GATE 2, part 1: "no powers the admin doesn't have", made a test.

docs/modules/ai-admin/spec.md §0.1, §0.12, §13 tests 1-3, 7.

THE GOVERNING PRINCIPLE OF THE WHOLE MODULE is that the agent's reads pass
through whatever actually enforces authorization in this codebase -- which,
verified, is `require_permission` in route handlers and NOTHING ELSE. Services
take an `org_id` and trust their caller for role. `org_session` pins
`app.current_org` only, so RLS scopes by org and never by role: a read-only
auditor and an org-admin get identical rows from it. RLS CANNOT SUBSTITUTE FOR
THE PERMISSION CHECK, and that is why the executor re-enters the real ASGI app
instead of calling a service or issuing a query.

EVERY TEST HERE ASSERTS ON THE RETURNED VALUE, NEVER ON AN EXCEPTION. A denied
tool call comes back as a `ToolError` the model can act on. Asserting
`pytest.raises` would pass against an implementation that raised for an entirely
different reason, and would print `DID NOT RAISE` when the gate was removed --
instead of printing the rows a permissionless caller was handed.
"""

import time
from typing import Any
from uuid import uuid4

import pytest

from app.core.ai.executor_staff import ToolError, invoke_tool
from app.core.ai.tools import REGISTRY, ToolResult
from tests.ai_admin_fakes import RecordingTransport, ai_user, install_spy
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


async def _args_for(name: str, org: HealthOrg) -> dict[str, Any]:
    """Valid arguments for every registered tool, so a parametrized test over
    `REGISTRY` exercises the GATE rather than argument validation."""
    if name == "devices.health":
        return {"device_id": str(org.device_id)}
    if name == "health.alerts":
        return {}
    raise AssertionError(f"no arguments known for tool {name!r} -- add them here")


async def _seed_estate(api_env: Any) -> tuple[HealthOrg, Any]:
    """An org with a device, a profile assigned to it, and one open alert, so a
    PERMITTED call returns real data rather than an empty list. Without the
    alert, "allowed" and "silently denied" would both print `[]`."""
    org = await seed_org(api_env, "aiauth")
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    await seed_assignment(api_env, org.org_id, profile_id, device_id=org.device_id)
    alert_id = await seed_alert(
        api_env, org, profile_id=profile_id, check_id=check_id
    )
    return org, alert_id


# --------------------------------------------------------------------------- #
# §13 test 1 -- `ai.use` and nothing else.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_ai_use_alone_is_forbidden_from_every_tool(api_env: Any, name: str) -> None:
    """§0.11: `ai.use` gets the user NOTHING on its own. Every tool call is
    still gated on that user's own module permissions.

    Asserted per tool over the whole registry, so a tool added without a real
    gate cannot hide behind its siblings.
    """
    org, _ = await _seed_estate(api_env)
    _, sub = await ai_user(api_env, org.org_id, "ai.use")
    headers = await api_env.headers(org.org_id, sub)

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name=name,
        raw_args=await _args_for(name, org),
        app=api_env.app,
        bearer=headers["Authorization"],
        deadline=_deadline(),
    )

    assert isinstance(outcome, ToolError), f"{name} returned {outcome!r} to a permissionless user"
    assert outcome.kind == "forbidden"
    assert outcome.status == 403
    # THE MODEL IS TOLD THE TRUTH. The mutation that returns `[]` here instead
    # is the one with the worst real-world consequence: a confident wrong answer
    # rather than a visible failure.
    assert REGISTRY[name].permission in outcome.message


# --------------------------------------------------------------------------- #
# §13 test 2 -- one permission, not the other.
# --------------------------------------------------------------------------- #


async def test_one_permission_allows_one_tool_and_denies_the_other(api_env: Any) -> None:
    """§13 test 2, asserted AGAINST THE EXECUTOR and not against the prefilter
    (which does not exist yet -- that ordering is deliberate).

    The spec writes this as ticket.read/devices.read. The v1 registry's two
    tools are gated on `health.read` and `devices.read`, so that is the pair
    exercised here; the property is identical.
    """
    org, alert_id = await _seed_estate(api_env)
    _, sub = await ai_user(api_env, org.org_id, "ai.use", "health.read")
    bearer = (await api_env.headers(org.org_id, sub))["Authorization"]

    allowed = await invoke_tool(
        registry=REGISTRY,
        tool_name="health.alerts",
        raw_args={},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    denied = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(org.device_id)},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )

    assert isinstance(allowed, ToolResult)
    # The allowed call returns the seeded alert, so "allowed" and "silently
    # denied" print different values -- an empty list would satisfy a weaker
    # assertion here and would also satisfy a broken gate.
    assert allowed.result_count == 1
    assert allowed.payload["items"][0]["id"] == str(alert_id)

    assert isinstance(denied, ToolError)
    assert (denied.kind, denied.status) == ("forbidden", 403)


# --------------------------------------------------------------------------- #
# §13 test 3 / §0.12 -- the prefilter is not the control.
# --------------------------------------------------------------------------- #


async def test_the_gate_runs_even_when_the_tool_is_invoked_directly(api_env: Any) -> None:
    """§0.12, pinned. This call bypasses any tool filtering entirely -- it names
    the tool straight to the executor. A caller lacking the permission is still
    refused, because the refusal comes from `require_permission` inside the real
    route, not from anything this module decided to offer.

    RE-RUN THIS WHEN THE PREFILTER LANDS. If it ever needs changing to keep
    passing, the prefilter has become load-bearing and that is a defect.
    """
    org, _ = await _seed_estate(api_env)
    _, sub = await ai_user(api_env, org.org_id, "ai.use", "health.read")
    bearer = (await api_env.headers(org.org_id, sub))["Authorization"]

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(org.device_id)},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolError) and outcome.status == 403


async def test_a_holder_of_the_permission_gets_the_data(api_env: Any) -> None:
    """The other side of every test above. Without this, a gate that denied
    EVERYONE would pass the whole file."""
    org, _ = await _seed_estate(api_env)
    _, sub = await ai_user(api_env, org.org_id, "ai.use", "devices.read")
    bearer = (await api_env.headers(org.org_id, sub))["Authorization"]

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(org.device_id)},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolResult)
    assert outcome.status == 200
    assert outcome.payload["device_id"] == str(org.device_id)
    # Real content, not merely a 200: the device's own hostname and the profile
    # that was assigned to it. A gate mutation that returned an empty envelope
    # would still be a 200.
    assert outcome.payload["hostname"] == await _hostname(api_env, org)
    assert outcome.payload["has_profile"] is True


async def _hostname(api_env: Any, org: HealthOrg) -> str:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Device

    async with AsyncSession(api_env.admin_engine) as session:
        return (
            await session.execute(select(Device.hostname).where(Device.id == org.device_id))
        ).scalar_one()


# --------------------------------------------------------------------------- #
# §13 test 7 -- cross-org.
# --------------------------------------------------------------------------- #


async def test_a_device_id_from_another_org_is_404_never_another_orgs_row(
    api_env: Any, monkeypatch: Any
) -> None:
    """§13 test 7. A REAL row is seeded in org B and asked for as org A, so
    "isolated" and "the route is simply broken" print different values: the same
    caller reading their OWN device in the same test gets 200 with data.

    404 and not 403: a 403 would confirm the id exists somewhere, which is a
    cross-tenant existence oracle.
    """
    mine, _ = await _seed_estate(api_env)
    theirs, _ = await _seed_estate(api_env)
    _, sub = await ai_user(api_env, mine.org_id, "ai.use", "devices.read")
    bearer = (await api_env.headers(mine.org_id, sub))["Authorization"]

    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    own = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(mine.device_id)},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    other = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(theirs.device_id)},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )

    assert isinstance(own, ToolResult) and own.payload["device_id"] == str(mine.device_id)
    assert isinstance(other, ToolError)
    assert (other.kind, other.status) == ("not_found", 404)
    # The cross-org id really was sent -- the refusal came from the platform,
    # not from this module quietly declining to ask.
    assert str(theirs.device_id) in str(spy.requests[1].url)


async def test_an_unknown_device_id_is_404_and_not_a_crash(api_env: Any) -> None:
    """The near-miss for the test above: an id that exists nowhere must produce
    the SAME answer as an id that exists in another org, or 404-vs-error becomes
    an existence oracle by another route."""
    org, _ = await _seed_estate(api_env)
    _, sub = await ai_user(api_env, org.org_id, "ai.use", "devices.read")
    bearer = (await api_env.headers(org.org_id, sub))["Authorization"]

    outcome = await invoke_tool(
        registry=REGISTRY,
        tool_name="devices.health",
        raw_args={"device_id": str(uuid4())},
        app=api_env.app,
        bearer=bearer,
        deadline=_deadline(),
    )
    assert isinstance(outcome, ToolError) and outcome.status == 404
