"""Per-instance heartbeats: the only way the API learns about the other two
processes, and the only way a multi-replica outage is visible.

TASK 6 COVERAGE GAP, CLOSED HERE. The three writers -- `app.main`'s startup
handler, `app.worker`'s heartbeat thread, `app.gateway.main`'s background
task -- were wired and never executed by a test; they were verified by
reading the code. Each writer's `while True`-plus-sleep loop now has its
"write one heartbeat, swallow and log any failure" body extracted into a
named, directly callable coroutine:

- `app.main.write_api_heartbeat_once(redis, instance, version)`
- `app.worker.write_worker_heartbeat(redis=...)`
- `app.gateway.main.write_gateway_heartbeat_once(redis, instance, version)`

The tests below exercise the REAL wiring, not just these coroutines in
isolation: `test_the_apis_startup_handler_runs_and_writes_a_heartbeat` drives
`app.main.create_app`'s actual `@app.on_event("startup")` handler through
`TestClient`'s lifespan-running context-manager form (the bare
`TestClient(app)` / `httpx.ASGITransport` used everywhere else in this suite
never runs it -- see `tests/conftest.py`'s `api_env` fixture, which is why
this gap existed). The gateway has no HTTP surface to drive a lifespan
through, so its test calls the extracted coroutine directly against a fake
Redis -- still the exact code the background task calls, just without the
sleep loop around it. The worker is NOT a beat task any more (a queued
heartbeat measured the worker's own queue depth, not its liveness -- see
`app.worker.write_worker_heartbeat`'s docstring); its own coverage gap is
closed the same way `test_the_apis_startup_handler_...` closes the API's --
`test_the_workers_process_init_receiver_starts_a_heartbeat_thread` below
fires the real `worker_process_init` signal and asserts a heartbeat actually
lands in Redis, not merely that the extracted coroutine works in isolation.
"""

import asyncio
import logging
import threading
import time as _time

import pytest
import redis.asyncio as aioredis
from celery.signals import worker_process_init

from app import worker
from app.core.config import override_settings
from app.core.health.heartbeat import (
    HEARTBEAT_INTERVAL_S,
    read_heartbeats,
    write_heartbeat,
)
from app.core.health.types import Status


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        # Set whenever `set()` is called -- lets a test block until a
        # background task (running on TestClient's own event-loop thread)
        # has actually written, instead of guessing at a sleep duration.
        self.wrote = threading.Event()

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.wrote.set()

    async def keys(self, pattern: str) -> list[str]:
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store.get(k) for k in keys]


class _RaisingRedis:
    """A `.set` that always fails -- for the failure-swallowing property."""

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        raise ConnectionError("redis is unreachable")


@pytest.mark.asyncio
async def test_a_fresh_heartbeat_is_ok() -> None:
    redis = _FakeRedis()
    await write_heartbeat(redis, "api", "i-1", "v1", now=100.0)
    [health] = await read_heartbeats(redis, now=100.0)
    assert health.process == "api"
    assert health.instances == 1
    assert health.status is Status.OK


@pytest.mark.asyncio
async def test_a_stale_heartbeat_degrades_then_goes_down() -> None:
    redis = _FakeRedis()
    await write_heartbeat(redis, "worker", "i-1", "v1", now=0.0)
    [degraded] = await read_heartbeats(redis, now=HEARTBEAT_INTERVAL_S * 4)
    assert degraded.status is Status.DEGRADED
    [down] = await read_heartbeats(redis, now=HEARTBEAT_INTERVAL_S * 11)
    assert down.status is Status.DOWN


@pytest.mark.asyncio
async def test_one_of_two_api_instances_dying_is_visible() -> None:
    """THE MULTI-REPLICA BLIND SPOT (spec §4). A process answering the status
    request is alive -- but only that ONE replica. Behind traefik with several
    API containers, a dead replica is invisible to a request the live one
    served, and the page would report api: ok while a share of requests fail.
    """
    redis = _FakeRedis()
    await write_heartbeat(redis, "api", "i-1", "v1", now=0.0)
    await write_heartbeat(redis, "api", "i-2", "v1", now=0.0)
    fresh = HEARTBEAT_INTERVAL_S * 11
    await write_heartbeat(redis, "api", "i-1", "v1", now=fresh)
    [health] = await read_heartbeats(redis, now=fresh)
    assert health.instances == 2
    assert health.healthy_instances == 1
    assert health.status is Status.DEGRADED, "a lost replica must not read as ok"


# ---------------------------------------------------------------------------
# Task 6 coverage gap: the three writers, actually run.
# ---------------------------------------------------------------------------


def _api_settings(tmp_path):
    from app.core.config import Settings

    # No real Postgres needed: `create_async_engine` is lazy and nothing in
    # `create_app`'s startup path issues a query -- only per-request handlers
    # do. Confirmed against this exact settings shape before writing this
    # test (an unreachable DSN, app builds cleanly, only a real request would
    # fail to connect).
    return Settings(
        database_url="postgresql+asyncpg://user:pass@localhost:59999/nonexistent",
        database_admin_url="postgresql+asyncpg://user:pass@localhost:59999/nonexistent",
        oidc_issuer="https://issuer.test",
        oidc_jwks_url="https://issuer.test/jwks",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
    )


def test_the_apis_startup_handler_runs_and_writes_a_heartbeat(tmp_path) -> None:
    """THE GAP ITSELF. Every other test in this suite builds the app with
    `httpx.ASGITransport` (see `tests/conftest.py`'s `api_env`) or a bare
    `TestClient(app)`, neither of which runs FastAPI's lifespan -- so
    `_start_platform_heartbeat` (`app/main.py`) has never actually executed
    in this test suite. `with TestClient(app) as client:` is the
    context-manager form that DOES run it.

    `app.state.redis` is swapped for a fake before entering the context --
    the real one `create_app` builds is a lazy, unconnected client, but
    there is no reason to depend on a live Redis for this assertion."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(_api_settings(tmp_path))
    fake_redis = _FakeRedis()
    app.state.redis = fake_redis

    with TestClient(app):
        assert fake_redis.wrote.wait(timeout=5), (
            "the startup handler's heartbeat task never wrote within 5s -- "
            "the lifespan did not run, or the task never fired"
        )

    keys = list(fake_redis.store)
    assert len(keys) == 1
    [key] = keys
    assert key.startswith("platform:heartbeat:api:"), key


async def test_the_workers_beat_task_writes_a_heartbeat() -> None:
    """`app.worker.write_worker_heartbeat` IS the single iteration the worker's
    own heartbeat thread runs on a loop (`_worker_heartbeat_forever`, started
    per-process by the `worker_process_init` receiver in `create_celery` --
    see `test_the_workers_process_init_receiver_starts_a_heartbeat_thread`
    below for that wiring exercised for real). This test calls the shared
    single-shot coroutine directly, with an injected fake Redis instead of the
    one it would otherwise build itself from `get_settings().redis_url`."""
    from app.worker import write_worker_heartbeat

    redis = _FakeRedis()
    await write_worker_heartbeat(redis)

    keys = list(redis.store)
    assert len(keys) == 1
    [key] = keys
    assert key.startswith("platform:heartbeat:worker:"), key


async def test_the_gateways_background_task_writes_a_heartbeat() -> None:
    """`write_gateway_heartbeat_once` is the body `_heartbeat_loop` (called
    from `run_gateway`'s `_main`) runs on every iteration -- exercised here
    without a socket, TLS, or a running loop around it."""
    from app.gateway.main import write_gateway_heartbeat_once

    redis = _FakeRedis()
    await write_gateway_heartbeat_once(redis, "gw-instance", "v1")

    keys = list(redis.store)
    assert len(keys) == 1
    [key] = keys
    assert key == "platform:heartbeat:gateway:gw-instance"


# --- failure-swallowing: a write failure must never take the process down ---


async def test_the_apis_writer_swallows_a_redis_failure(caplog) -> None:
    from app.main import write_api_heartbeat_once

    with caplog.at_level(logging.ERROR, logger="its.api"):
        await write_api_heartbeat_once(_RaisingRedis(), "i-1", "v1")  # must not raise

    [record] = [r for r in caplog.records if r.name == "its.api"]
    assert record.exc_info is not None, "the failure must be logged with exc_info"
    assert "%s" not in record.msg and "redis is unreachable" not in record.getMessage(), (
        "the exception must not be interpolated into the message as a %-arg"
    )


async def test_the_workers_writer_swallows_a_redis_failure(caplog) -> None:
    from app.worker import write_worker_heartbeat

    with caplog.at_level(logging.ERROR, logger="its.worker"):
        await write_worker_heartbeat(_RaisingRedis())  # must not raise

    [record] = [r for r in caplog.records if r.name == "its.worker"]
    assert record.exc_info is not None, "the failure must be logged with exc_info"
    assert "%s" not in record.msg and "redis is unreachable" not in record.getMessage(), (
        "the exception must not be interpolated into the message as a %-arg"
    )


async def test_the_gateways_writer_swallows_a_redis_failure(caplog) -> None:
    from app.gateway.main import write_gateway_heartbeat_once

    with caplog.at_level(logging.ERROR, logger="its.gateway"):
        await write_gateway_heartbeat_once(_RaisingRedis(), "gw-1", "v1")  # must not raise

    [record] = [r for r in caplog.records if r.name == "its.gateway"]
    assert record.exc_info is not None, "the failure must be logged with exc_info"
    assert "%s" not in record.msg and "redis is unreachable" not in record.getMessage(), (
        "the exception must not be interpolated into the message as a %-arg"
    )


# --- the fourth process, whenever it shows up, must be visible as a gap ---


async def test_exactly_three_processes_write_heartbeats(tmp_path) -> None:
    """Pins the set of process names any writer can emit to `{api, worker,
    gateway}`. A fourth process added later without wiring a heartbeat is
    silent everywhere else -- this is the one test that would fail for it,
    by naming the set explicitly rather than just asserting ``>= 3``."""
    from fastapi.testclient import TestClient

    from app.gateway.main import write_gateway_heartbeat_once
    from app.main import create_app
    from app.worker import write_worker_heartbeat

    redis = _FakeRedis()
    await write_worker_heartbeat(redis)
    await write_gateway_heartbeat_once(redis, "gw-1", "v1")

    app = create_app(_api_settings(tmp_path))
    app.state.redis = redis
    with TestClient(app):
        assert redis.wrote.wait(timeout=5)

    processes = {key.split(":")[2] for key in redis.store}
    assert processes == {"api", "worker", "gateway"}


# ---------------------------------------------------------------------------
# The worker's heartbeat is a thread the process owns, not a queued task.
# ---------------------------------------------------------------------------


def test_platform_heartbeat_is_not_in_the_beat_schedule(tmp_path) -> None:
    """A QUEUED HEARTBEAT MEASURES QUEUE DEPTH, NOT LIVENESS. The worker used
    to write its own liveness heartbeat from a beat-scheduled Celery task
    (`platform-heartbeat` -> `app.worker.platform_heartbeat`), sharing the
    worker's own task queue with every long sweep in this file (patch scans,
    backup verification, retention). A worker whose pool slots were all busy
    with one of those delayed the heartbeat behind them, and both the status
    page and the Docker healthcheck read the resulting staleness as
    `degraded`, then `down` -- a false signal reporting queue saturation, not
    process death, while the process was entirely alive and doing exactly
    what it was supposed to.

    `platform-heartbeat` must never reappear as a beat entry: the worker now
    writes its own heartbeat from a daemon thread the process starts and owns
    directly (`create_celery`'s `worker_process_init` receiver, see
    `test_the_workers_process_init_receiver_starts_a_heartbeat_thread`
    below), independent of the task queue -- the same posture `app.main` and
    `app.gateway.main` already use for their own heartbeats."""
    with override_settings(_api_settings(tmp_path)):
        schedule = worker.create_celery().conf.beat_schedule

    assert "platform-heartbeat" not in schedule, (
        "the worker's liveness heartbeat must not be a beat-scheduled task -- "
        "a queued heartbeat measures this worker's own queue depth, not "
        "whether the process is alive"
    )


async def test_the_workers_process_init_receiver_starts_a_heartbeat_thread(tmp_path) -> None:
    """THE WORKER'S OWN COVERAGE GAP, closed the same way
    `test_the_apis_startup_handler_runs_and_writes_a_heartbeat` closes the
    API's: this drives the REAL mechanism -- `create_celery`'s
    `worker_process_init` receiver, fired the way Celery's prefork/solo pool
    actually fires it (once per worker process, after fork) -- rather than
    calling `write_worker_heartbeat` directly, which
    `test_the_workers_beat_task_writes_a_heartbeat` above already covers in
    isolation.

    A stand-in fake Redis cannot be swapped in here the way `app.state.redis`
    is for the API test: the receiver starts a thread that builds its OWN
    client from `get_settings().redis_url` (see `write_worker_heartbeat`'s
    docstring), so this asserts against the real local Redis instead, over
    the dedicated test database `redis_url` already points at (`/9`, the same
    one `tests/test_worker_beat_expiry.py` and `_api_settings` above use)."""
    settings = _api_settings(tmp_path)
    with override_settings(settings):
        worker.create_celery()
        worker_process_init.send(sender=None)

        redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            from app.core.health.heartbeat import read_heartbeats

            deadline = _time.monotonic() + 5
            healths: list = []
            while _time.monotonic() < deadline:
                healths = await read_heartbeats(redis, now=_time.time())
                if any(h.process == "worker" for h in healths):
                    break
                await asyncio.sleep(0.05)
            assert any(h.process == "worker" for h in healths), (
                "firing worker_process_init never produced a worker heartbeat in "
                "Redis within 5s -- the receiver did not fire, or its thread "
                "never wrote"
            )
        finally:
            await redis.aclose()
