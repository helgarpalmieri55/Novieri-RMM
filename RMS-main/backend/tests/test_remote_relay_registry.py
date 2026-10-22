"""Redis-backed relay registration (spec-screen-share.md §2.2, §3.2).

The registration is written by the api after commit and consumed by the
gateway on agent attach. GETDEL is what makes the relay token one-shot, and
the TTL is what bounds it to connect_by.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import redis.asyncio as aioredis

from app.modules.remote.relay import (
    RelayRegistration,
    RelayRegistry,
    mint_relay_token,
    registration_key,
    stop_channel,
)

REDIS_URL = os.environ.get("ITS_TEST_REDIS_URL", "redis://localhost:6379/9")


@pytest.fixture()
async def redis():
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


def _registration(connect_in_s: float = 60) -> RelayRegistration:
    _token, token_hash = mint_relay_token()
    _ticket, ticket_hash = mint_relay_token()
    return RelayRegistration(
        session_id=uuid4(),
        org_id=uuid4(),
        device_id=uuid4(),
        token_hash=token_hash,
        viewer_ticket_hash=ticket_hash,
        connect_by=datetime.now(UTC) + timedelta(seconds=connect_in_s),
        consent_mode="attended",
        allow_input=True,
        max_duration_s=600,
        consent_timeout_s=30,
    )


async def test_consume_is_one_shot(redis) -> None:
    registry = RelayRegistry(redis)
    reg = _registration()
    await registry.register(reg)

    assert await registry.peek(reg.session_id) == reg
    assert await registry.consume(reg.session_id) == reg
    assert await registry.consume(reg.session_id) is None
    assert await registry.peek(reg.session_id) is None


async def test_ttl_follows_connect_by(redis) -> None:
    registry = RelayRegistry(redis)
    reg = _registration(connect_in_s=30)
    await registry.register(reg)
    ttl = await redis.pttl(registration_key(reg.session_id))
    assert 25_000 < ttl <= 30_000


async def test_registration_already_past_connect_by_is_not_written(redis) -> None:
    registry = RelayRegistry(redis)
    reg = _registration(connect_in_s=-1)
    await registry.register(reg)
    assert await registry.peek(reg.session_id) is None


async def test_only_hashes_are_stored(redis) -> None:
    registry = RelayRegistry(redis)
    reg = _registration()
    await registry.register(reg)
    stored = json.loads(await redis.get(registration_key(reg.session_id)))
    assert set(stored) >= {"token_hash", "viewer_ticket_hash"}
    assert "token" not in stored and "ticket" not in stored


async def test_malformed_entry_reads_as_absent(redis) -> None:
    registry = RelayRegistry(redis)
    sid = uuid4()
    await redis.set(registration_key(sid), "{not json", ex=10)
    assert await registry.consume(sid) is None


async def test_stop_discards_and_publishes(redis) -> None:
    registry = RelayRegistry(redis)
    reg = _registration()
    await registry.register(reg)

    pubsub = redis.pubsub()
    await pubsub.subscribe(stop_channel(reg.session_id))
    await pubsub.get_message(timeout=1)  # subscribe confirmation
    await registry.stop(reg.session_id)

    assert await registry.peek(reg.session_id) is None
    message = None
    for _ in range(20):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message:
            break
        await asyncio.sleep(0.05)
    assert message is not None and message["channel"] == stop_channel(reg.session_id)
    await pubsub.aclose()
