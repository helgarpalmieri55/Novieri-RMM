"""The shared Redis counter: `app/core/rate_limit.py`.

Design `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §1.1(3):
the per-account and per-factor limits live in the database
(`app/core/auth/failures.py`); the per-address ones live here. Each test names
the line of `rate_limit.py` it protects -- delete that line and it goes red.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.requests import Request

from app.core import rate_limit
from app.core.client_address import parse_trusted_proxies

REDIS_URL = os.environ.get("ITS_TEST_REDIS_URL", "redis://localhost:6379/9")


@pytest.fixture()
async def redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


def _key() -> str:
    return f"test:{uuid4().hex}"


async def test_the_limit_counts_every_hit_and_refuses_the_one_over(redis: aioredis.Redis) -> None:
    """`return count <= limit`: the request that trips the limit is itself counted
    and refused, not let through "for free"."""
    key = _key()

    results = [await rate_limit.hit(redis, key, limit=3, window_s=60) for _ in range(4)]

    assert results == [True, True, True, False]


async def test_the_window_starts_at_the_first_hit(redis: aioredis.Redis) -> None:
    """The `EXPIRE` after the first `INCR`: without it the counter never resets."""
    key = _key()

    await rate_limit.hit(redis, key, limit=3, window_s=60)

    assert 0 < await redis.ttl(rate_limit.KEY_PREFIX + key) <= 60


async def test_a_later_hit_does_not_push_the_window_out(redis: aioredis.Redis) -> None:
    """A FIXED window: `EXPIRE` runs only when the key has no TTL. A client that
    keeps knocking must not keep its own window open."""
    key = _key()
    await rate_limit.hit(redis, key, limit=3, window_s=60)
    await redis.expire(rate_limit.KEY_PREFIX + key, 10)

    await rate_limit.hit(redis, key, limit=3, window_s=60)

    assert 0 < await redis.ttl(rate_limit.KEY_PREFIX + key) <= 10


async def test_a_key_that_lost_its_ttl_gets_one(redis: aioredis.Redis) -> None:
    """The repair `mcp/server.py::_bump` taught: a crash between `INCR` and
    `EXPIRE` must not leave a counter that locks a person out for good."""
    key = _key()
    await redis.set(rate_limit.KEY_PREFIX + key, 7)  # no TTL at all

    assert await rate_limit.hit(redis, key, limit=100, window_s=60)
    assert 0 < await redis.ttl(rate_limit.KEY_PREFIX + key) <= 60


async def test_a_new_window_starts_when_the_old_one_expires(redis: aioredis.Redis) -> None:
    key = _key()
    assert await rate_limit.hit(redis, key, limit=1, window_s=60)
    assert not await rate_limit.hit(redis, key, limit=1, window_s=60)

    await redis.pexpire(rate_limit.KEY_PREFIX + key, 50)
    await asyncio.sleep(0.2)

    assert await rate_limit.hit(redis, key, limit=1, window_s=60)


async def test_keys_are_counted_apart_and_namespaced(redis: aioredis.Redis) -> None:
    """`KEY_PREFIX`: a caller's key never collides with another subsystem's."""
    first, second = _key(), _key()

    assert await rate_limit.hit(redis, first, limit=1, window_s=60)
    assert await rate_limit.hit(redis, second, limit=1, window_s=60)
    assert await redis.get(first) is None
    assert await redis.get(rate_limit.KEY_PREFIX + first) == "1"


@pytest.mark.parametrize(("limit", "window_s"), [(0, 60), (5, 0)])
async def test_a_limit_of_nothing_is_a_programming_error(
    redis: aioredis.Redis, limit: int, window_s: int
) -> None:
    with pytest.raises(ValueError):
        await rate_limit.hit(redis, _key(), limit=limit, window_s=window_s)


class _BrokenRedis:
    """Every command raises, as a Redis outage does."""

    async def incr(self, key: str) -> int:
        raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def ttl(self, key: str) -> int:
        raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def expire(self, key: str, seconds: int) -> bool:
        raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")


async def test_it_fails_open_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """Redis down must not take sign-in down: the per-factor counters in the
    database still hold. But a limit that stopped counting is logged, with a
    static message and never the Redis error's text (a Redis URL can carry a
    password)."""
    broken: Any = _BrokenRedis()
    with caplog.at_level(logging.WARNING, logger="app.core.rate_limit"):
        allowed = await rate_limit.hit(broken, _key(), limit=1, window_s=60)

    assert allowed is True
    (record,) = [r for r in caplog.records if r.name == "app.core.rate_limit"]
    assert record.getMessage() == "rate limit unavailable; redis error, request allowed"


def _request(peer: str | None, forwarded: str | None, trusted: str) -> Request:
    app = FastAPI()
    app.state.trusted_proxies = parse_trusted_proxies(trusted)
    scope: dict[str, object] = {
        "type": "http",
        "app": app,
        "method": "POST",
        "path": "/",
        "headers": [(b"x-forwarded-for", forwarded.encode())] if forwarded else [],
    }
    if peer is not None:
        scope["client"] = (peer, 40000)
    return Request(scope)


def test_the_address_key_trusts_forwarding_only_from_a_trusted_proxy() -> None:
    """`client_address` (`app/core/client_address.py:76`): the rightmost
    X-Forwarded-For entry, and only when the peer is a trusted proxy. A client
    that sends its own header must not choose its own bucket."""
    via_proxy = _request("10.0.0.5", "198.51.100.1, 203.0.113.9", "10.0.0.0/8")
    direct = _request("198.51.100.7", "203.0.113.9", "10.0.0.0/8")
    nobody = _request(None, None, "10.0.0.0/8")

    assert rate_limit.ip_key(via_proxy, "login") == "login:ip:203.0.113.9"
    assert rate_limit.ip_key(direct, "login") == "login:ip:198.51.100.7"
    assert rate_limit.ip_key(nobody, "login") == "login:ip:unknown"
