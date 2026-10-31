"""The staff assistant follows the console's client selector (phase 3 design
§5.2, plan 2026-10-31 Task 4).

A turn started with `X-Client-Id` runs every loopback call -- the read tools
and the act tool's device probe -- with the same header, so the inner route
runs under the turn's own scope: never wider, because the inner route passes
the same door with the same token and the header only narrows. Without it the
inner route re-admitted the person un-narrowed (bounded by their grants, but
wider than the scope the turn ran under). The portal executor takes no
selection and refuses one.

EVERY FORWARDING ASSERTION IS ON THE RECORDED REQUEST (`RecordingTransport`)
as well as on what came back, so "the header was sent" and "the route
narrowed by it" are both seen.
"""

import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.executor_portal import invoke_portal_tool
from app.core.ai.executor_staff import ToolError, invoke_tool
from app.core.ai.providers import TextBlock, ToolCallBlock
from app.core.ai.tools import REGISTRY, ToolResult
from app.core.ai.types import TurnContext
from app.models import AiSettings, Device
from app.modules.ai_admin import api as admin_api
from app.modules.ai_copilot import api as copilot_api
from app.modules.ai_portal.profile import RESOLVER_PROFILE
from tests.ai_admin_fakes import (
    RecordingTransport,
    ScriptedProvider,
    ai_user,
    install_portal_spy,
    install_spy,
)
from tests.client_scope_support import client_with_site
from tests.health_fixtures import HealthOrg, seed_alert, seed_check, seed_org, seed_profile
from tests.test_ai_copilot import BASE, _world

AI = "/api/v1/ai"


def _deadline() -> float:
    return time.monotonic() + 30.0


async def _two_clients(api_env: Any, tag: str) -> tuple[HealthOrg, UUID, UUID, str]:
    """An org whose internal client and a client X each hold one machine with
    one open alert, and a person holding `ai.use` and `health.read` on every
    client. Returns `(org, x, x_device, bearer)`."""
    org = await seed_org(api_env, tag)
    x, x_site = await client_with_site(api_env, org.org_id)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as s:
        device = Device(
            org_id=org.org_id, client_id=x, site_id=x_site, hostname=f"{tag}-x",
            os_family="windows",
        )
        s.add(device)
        if await s.get(AiSettings, org.org_id) is None:
            s.add(AiSettings(org_id=org.org_id))
        await s.commit()
    profile_id = await seed_profile(api_env, org.org_id)
    check_id = await seed_check(api_env, org.org_id, profile_id)
    for device_id in (org.device_id, device.id):
        await seed_alert(
            api_env, org, profile_id=profile_id, check_id=check_id, device_id=device_id
        )
    _, sub = await ai_user(api_env, org.org_id, "ai.use", "health.read")
    bearer = (await api_env.headers(org.org_id, sub))["Authorization"]
    return org, x, device.id, bearer


def _alerted_devices(outcome: Any) -> set[str]:
    assert isinstance(outcome, ToolResult), outcome
    return {item["device_id"] for item in outcome.payload["items"]}


async def test_the_executor_forwards_the_turn_s_client_and_the_route_sees_only_it(
    api_env: Any, monkeypatch: Any
) -> None:
    """Narrowed to X: the request carries `X-Client-Id: X` and the alerts
    route answers with X's machine only. Not narrowed: no such header, and
    both machines."""
    org, x, x_device, bearer = await _two_clients(api_env, "ai-narrow-exec")
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    narrowed = await invoke_tool(
        registry=REGISTRY, tool_name="health.alerts", raw_args={}, app=api_env.app,
        bearer=bearer, deadline=_deadline(), requested_client=x,
    )
    everything = await invoke_tool(
        registry=REGISTRY, tool_name="health.alerts", raw_args={}, app=api_env.app,
        bearer=bearer, deadline=_deadline(),
    )

    assert [request.headers.get("x-client-id") for request in spy.requests] == [str(x), None]
    assert _alerted_devices(narrowed) == {str(x_device)}
    assert _alerted_devices(everything) == {str(x_device), str(org.device_id)}


async def test_an_admin_turn_narrowed_by_the_selector_reads_only_that_client(
    api_env: Any, monkeypatch: Any
) -> None:
    """End to end through `POST /ai/sessions/{id}/turns`: the route puts the
    request's `X-Client-Id` on the turn, the read tool's loopback carries it,
    and the turn read one alert -- X's -- where the same turn un-narrowed
    reads two."""
    org, x, _, bearer = await _two_clients(api_env, "ai-narrow-turn")
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)

    async def turn(headers: dict[str, str]) -> Any:
        monkeypatch.setattr(
            admin_api,
            "_provider_for",
            lambda: ScriptedProvider(
                [
                    [ToolCallBlock(id="c0", name="health.alerts", arguments={})],
                    [TextBlock(text="Two machines are alerting.")],
                ]
            ),
        )
        created = await api_env.client.post(f"{AI}/sessions", headers=headers)
        assert created.status_code == 201, created.text
        r = await api_env.client.post(
            f"{AI}/sessions/{created.json()['id']}/turns",
            headers=headers,
            json={"question": "what is alerting?"},
        )
        assert r.status_code == 200, r.text
        return r.json()["tool_calls"][0]

    narrowed = await turn({"Authorization": bearer, "X-Client-Id": str(x)})
    assert [request.headers.get("x-client-id") for request in spy.requests] == [str(x)]
    assert (narrowed["status"], narrowed["result_count"]) == ("ok", 1)

    everything = await turn({"Authorization": bearer})
    assert spy.requests[-1].headers.get("x-client-id") is None
    assert (everything["status"], everything["result_count"]) == ("ok", 2)


async def test_a_copilot_turn_forwards_the_selector_s_client(
    api_env: Any, monkeypatch: Any
) -> None:
    """The copilot's route puts the request's `X-Client-Id` on the turn too:
    a ticket of the internal client, worked with the internal client chosen
    in the selector, reads its alerts under that client."""
    world = await _world(api_env, "ai-narrow-copilot")
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)
    headers = {**world.headers, "X-Client-Id": str(world.org_id)}
    monkeypatch.setattr(
        copilot_api,
        "_provider_for",
        lambda: ScriptedProvider(
            [
                [ToolCallBlock(id="c0", name="health.alerts", arguments={})],
                [TextBlock(text="Nothing is alerting.")],
            ]
        ),
    )
    created = await api_env.client.post(
        f"{BASE}/{world.ticket_id}/ai/sessions", headers=headers, json={"mode": "read"}
    )
    assert created.status_code == 201, created.text
    r = await api_env.client.post(
        f"{BASE}/{world.ticket_id}/ai/sessions/{created.json()['id']}/turns",
        headers=headers,
        json={"question": "why is it slow?"},
    )
    assert r.status_code == 200, r.text
    assert [request.headers.get("x-client-id") for request in spy.requests] == [
        str(world.org_id)
    ]


async def test_the_device_probe_carries_the_turn_s_client(api_env: Any) -> None:
    """`act._device_is_visible` passes the turn's selection to the executor,
    as every loopback call of the turn does."""
    from app.core.ai.act import _device_is_visible

    seen: list[dict[str, Any]] = []

    async def recording_executor(**kwargs: Any) -> ToolError:
        seen.append(kwargs)
        return ToolError(tool="devices.health", kind="blocked", message="scripted")

    x = uuid4()
    ctx = TurnContext(app=api_env.app, bearer="Bearer t", deadline=_deadline(),
                      requested_client=x)
    assert await _device_is_visible(ctx=ctx, executor=recording_executor, device_id=uuid4())
    assert [call["requested_client"] for call in seen] == [x]


async def test_the_confirm_probe_runs_under_the_confirm_s_client(
    api_env: Any, monkeypatch: Any
) -> None:
    """`POST /ai/actions/{id}/confirm` narrowed by the selector: its device
    probe carries the same `X-Client-Id` (`ai_admin.api._turn_context`)."""
    from app.core.ai.act import propose
    from app.core.client_scope import ClientScope
    from app.core.tenancy import org_session
    from tests.ai_act_fixtures import build_world

    world = await build_world(
        api_env, tag="ai-narrow-confirm",
        codes=("ai.use", "ai.act.propose", "ai.act.confirm", "devices.read", "jobs.dispatch"),
    )
    async with org_session(
        world.org_id, engine=api_env.app_engine, clients=ClientScope.all()
    ) as s:
        preview = await propose(
            s, org_id=world.org_id, binding=world.binding, ctx=world.ctx,
            executor=invoke_tool, action_key=world.action_key, device_id=world.device_id,
        )
    spy = RecordingTransport(api_env.app)
    install_spy(monkeypatch, spy)
    r = await api_env.client.post(
        f"{AI}/actions/{preview.ai_action_id}/confirm",
        json={"digest": preview.preview_digest},
        headers={**world.headers, "X-Client-Id": str(world.org_id)},
    )
    assert r.status_code == 200, r.text
    assert [request.headers.get("x-client-id") for request in spy.requests] == [
        str(world.org_id)
    ]


async def test_the_portal_executor_refuses_a_client_selection_and_sends_nothing(
    api_env: Any, monkeypatch: Any
) -> None:
    """A portal turn is never narrowed (the design leaves the portal as it
    is), so a selection reaching its executor is refused before a URL exists
    -- dropping it silently would answer wider than asked."""
    spy = RecordingTransport(api_env.app)
    install_portal_spy(monkeypatch, spy)
    outcome = await invoke_portal_tool(
        tool_name="portal.categories",
        raw_args={},
        app=api_env.app,
        bearer="Bearer t",
        deadline=_deadline(),
        registry=RESOLVER_PROFILE.tools,
        requested_client=uuid4(),
    )
    assert isinstance(outcome, ToolError)
    assert (outcome.kind, spy.requests) == ("forbidden", [])
