"""One shared request counter in Redis: a fixed window per key.

Design `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §1.1
(3) and §5.4: the per-account and per-factor limits live in the database
(`app/core/auth/failures.py`); the per-address ones live here, because an
address is not a person and has no row to count on.

WHERE IT COMES FROM. Lifted from `app/mcp/server.py::_bump`, the best of the
three ad-hoc counters in this tree: `INCR`, then `TTL`, and a key with no TTL at
all gets one on the spot. The other two
(`notification/api.py::_enforce_test_send_rate_limit`, `ai_admin/api.py::_bump`)
set the expiry only when the count is 1, so a lost `EXPIRE` -- a crash or a
dropped connection between the two commands -- leaves a counter that never
resets. They are not moved here in this change; MCP keeps its own copy until
phase 3 rewrites that door.

A FIXED WINDOW. It starts at the first hit and ends `window_s` later whatever
happens in between; `EXPIRE` is never pushed out, so a client that keeps
knocking does not keep its own window open.

IT FAILS OPEN, and logs that it did. Redis down must not take sign-in down with
it: every limit counted here sits on top of one that needs no Redis (the
per-factor counters in the database, RBAC, the audit chain). A caller that must
fail CLOSED (`email_intake`'s connection test) keeps its own counter.

KEYS ARE NAMESPACED under `KEY_PREFIX`, so a caller's key can never collide with
another subsystem's Redis key.
"""

import logging

from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.client_address import client_address

__all__ = ["KEY_PREFIX", "hit", "ip_key"]

logger = logging.getLogger(__name__)

KEY_PREFIX = "ratelimit:"


async def hit(redis: Redis, key: str, *, limit: int, window_s: int) -> bool:
    """Count one request against `key`. True while the window holds at most
    `limit` requests, this one included; False for the one over and every one
    after it until the window ends.

    `redis` is the caller's client (`request.app.state.redis`). On a Redis
    error the request is ALLOWED and a warning is logged -- see the module
    docstring for why this limiter fails open.
    """
    if limit < 1 or window_s < 1:
        raise ValueError(  # i18n-exempt: programming error, never reaches a person
            "a rate limit needs a limit and a window of at least 1"
        )
    name = KEY_PREFIX + key
    try:
        count = int(await redis.incr(name))
        if int(await redis.ttl(name)) < 0:
            await redis.expire(name, window_s)
    except RedisError:
        logger.warning("rate limit unavailable; redis error, request allowed", exc_info=True)
        return True
    return count <= limit


def ip_key(request: Request, bucket: str) -> str:
    """`<bucket>:ip:<address>` for a per-address limit.

    The address is `client_address` (`app/core/client_address.py:76`): the
    rightmost X-Forwarded-For entry, and only from `ITS_TRUSTED_PROXY_CIDRS`.
    A request whose address cannot be established shares the `unknown`
    bucket with every other such request -- the conservative direction.
    """
    address = client_address(request, request.app.state.trusted_proxies)
    return f"{bucket}:ip:{address if address is not None else 'unknown'}"
