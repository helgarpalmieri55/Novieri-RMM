"""Deterministic race tests: wait until one backend is SEEN waiting on another.

A race test that sleeps for a fixed time and hopes the other transaction got
where it needed to be passes on a fast machine and proves nothing on a slow
one -- or fails at random. These helpers poll `pg_stat_activity` with
`pg_blocking_pids` until a backend is blocked by the transaction the test is
holding open, and hand back what it is waiting in, so the test can also say
WHERE it waits (the lock order is often the thing under test).

Polled from the APP role, which may read the query text of its own role's
backends -- the API's, in these tests.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

__all__ = ["backend_pid", "normalised", "until_blocked_by"]


async def backend_pid(session: AsyncSession) -> int:
    """The server process id of the connection `session` is using."""
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def until_blocked_by(
    app_engine: AsyncEngine, blocker: int, *, timeout_s: float = 30.0
) -> list[str]:
    """Return the query text of every backend blocked by `blocker`, as soon as
    there is at least one. Fails the test after `timeout_s`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    async with app_engine.connect() as conn:
        while True:
            waiting = list(
                (
                    await conn.execute(
                        text(
                            "SELECT query FROM pg_stat_activity "
                            "WHERE CAST(:b AS integer) = ANY (pg_blocking_pids(pid))"
                        ),
                        {"b": blocker},
                    )
                )
                .scalars()
                .all()
            )
            if waiting:
                return [str(query) for query in waiting]
            assert loop.time() < deadline, f"nothing ever waited on backend {blocker}"
            await asyncio.sleep(0.02)
            await conn.rollback()  # a fresh snapshot of pg_stat_activity


def normalised(query: str) -> str:
    """A query's text, upper-cased with its whitespace collapsed, for matching."""
    return " ".join(query.upper().split())
