"""The two optional upgrade-request headers (spec-self-update.md §3.3).

Self-reported by an authenticated agent, used for display and eligibility
only. An invalid capabilities header is ignored whole, never stored.
"""

import asyncio
from pathlib import Path
from uuid import UUID

import websockets
from sqlalchemy import select

from app.core.tenancy import org_session
from app.gateway.agent_headers import (
    HEADER_CAPABILITIES,
    HEADER_VERSION,
    MAX_CAPABILITIES_HEADER,
    parse_agent_headers,
)
from app.models import Agent, AuditEvent
from tests.test_gateway_mtls import _client_ssl, _enroll_device, gateway  # noqa: F401


def test_both_headers_parse() -> None:
    report = parse_agent_headers(
        {HEADER_VERSION: "1.7.0", HEADER_CAPABILITIES: "agent.update,app.deploy,hello"}
    )
    assert report.version == "1.7.0"
    assert report.capabilities == {"agent.update": True, "app.deploy": True, "hello": True}


def test_an_old_agent_sends_neither() -> None:
    report = parse_agent_headers({})
    assert report.version is None
    assert report.capabilities is None


def test_one_invalid_capability_name_ignores_the_whole_header() -> None:
    for bad in ("agent.update,Hello", "agent.update,,hello", "agent update", "x" * 65, "1abc"):
        assert parse_agent_headers({HEADER_CAPABILITIES: bad}).capabilities is None, bad


def test_an_oversized_capabilities_header_is_ignored() -> None:
    names = ",".join(f"cap{i:05d}" for i in range(600))
    assert len(names) > MAX_CAPABILITIES_HEADER
    assert parse_agent_headers({HEADER_CAPABILITIES: names}).capabilities is None


def test_a_version_with_odd_characters_is_ignored() -> None:
    for bad in ("1.0.0 ; drop", "‮1.0.0", "v" * 51, ""):
        assert parse_agent_headers({HEADER_VERSION: bad}).version is None, bad
    assert parse_agent_headers({HEADER_VERSION: "dev"}).version == "dev"


async def test_the_gateway_records_version_and_capabilities(
    api_env,  # type: ignore[no-untyped-def]
    gateway,  # noqa: F811
    tmp_path: Path,
) -> None:
    _server, port = gateway
    enrolled = await _enroll_device(api_env, tmp_path)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    ctx = _client_ssl(tmp_path, enrolled, "hdr")

    async def recorded_version() -> str | None:
        async with org_session(org_id, engine=api_env.app_engine) as session:
            agent = (
                await session.execute(select(Agent).where(Agent.device_id == device_id))
            ).scalar_one()
            return agent.agent_version

    async def connect(version: str, caps: str) -> None:
        headers = {HEADER_VERSION: version, HEADER_CAPABILITIES: caps}
        async with websockets.connect(
            f"wss://localhost:{port}/agent", ssl=ctx, additional_headers=headers
        ):
            # Wait for the gateway's write rather than a fixed sleep: under a
            # loaded full-suite run 0.3 s was not always enough.
            for _ in range(100):
                if await recorded_version() == version:
                    break
                await asyncio.sleep(0.1)

    await connect("1.6.0", "agent.update,hello")
    await connect("1.7.0", "Not A Capability")

    async with org_session(org_id, engine=api_env.app_engine) as session:
        agent = (
            await session.execute(select(Agent).where(Agent.device_id == device_id))
        ).scalar_one()
        assert agent.agent_version == "1.7.0"
        assert agent.agent_version_reported_at is not None
        # The invalid header on the second connect left the first one's list.
        assert agent.capabilities == {"agent.update": True, "hello": True}
        changes = list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.action == "agent.version_changed")
                    .order_by(AuditEvent.ts, AuditEvent.id)
                )
            ).scalars()
        )
    assert [(e.meta["from"], e.meta["to"]) for e in changes] == [
        (None, "1.6.0"),
        ("1.6.0", "1.7.0"),
    ]
    assert all(e.actor_type == "agent" for e in changes)


async def test_a_reconnect_with_new_capabilities_replaces_the_enrolled_set(
    api_env,  # type: ignore[no-untyped-def]
    gateway,  # noqa: F811
    tmp_path: Path,
) -> None:
    """An agent that gains a capability through self-update is not refused
    until it re-enrolls: the header replaces what enrollment recorded, in the
    same column and shape. An agent that sends no header changes nothing."""
    _server, port = gateway
    enrolled = await _enroll_device(api_env, tmp_path)
    org_id, device_id = enrolled["org_id"], UUID(enrolled["device_id"])
    ctx = _client_ssl(tmp_path, enrolled, "caps")

    async def stored() -> tuple[str, dict[str, object]]:
        async with org_session(org_id, engine=api_env.app_engine) as session:
            agent = (
                await session.execute(select(Agent).where(Agent.device_id == device_id))
            ).scalar_one()
            return agent.agent_version, dict(agent.capabilities)

    async def connect(headers: dict[str, str]) -> None:
        async with websockets.connect(
            f"wss://localhost:{port}/agent", ssl=ctx, additional_headers=headers
        ):
            await asyncio.sleep(0.3)

    enrolled_state = await stored()

    # An old agent: no headers, and the enrollment record stands.
    await connect({})
    assert await stored() == enrolled_state

    await connect({HEADER_VERSION: "1.6.0", HEADER_CAPABILITIES: "agent.update,hello"})
    assert (await stored())[1] == {"agent.update": True, "hello": True}

    # After an update: one capability gained, one dropped. Replaced, not merged.
    await connect({HEADER_VERSION: "1.7.0", HEADER_CAPABILITIES: "agent.update,remote.screen"})
    assert await stored() == ("1.7.0", {"agent.update": True, "remote.screen": True})
