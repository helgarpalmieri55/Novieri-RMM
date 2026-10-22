"""`app.main.lifespan` -- the API's two background loops start AND stop.

The start half was previously three `@app.on_event("startup")` handlers,
covered by `tests/test_platform_heartbeat.py`'s
`test_the_apis_startup_handler_runs_and_writes_a_heartbeat`. The STOP half
never existed: `on_event("startup")` had no counterpart, so the heartbeat and
drain tasks were created and never cancelled, and shutdown tore down a loop
with two pending tasks still in it.

That is the behaviour these tests pin. A migration from `on_event` to
`lifespan` that only renamed things would pass the first test below and fail
the second.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings


def _api_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://app_user:app@127.0.0.1:5432/postgres",
        database_admin_url="postgresql+asyncpg://its_admin:its_admin_test@127.0.0.1:5432/postgres",
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url="redis://localhost:6379/9",
    )


def test_the_lifespan_starts_both_background_tasks(tmp_path: Path) -> None:
    """Inside the context both tasks exist and are running.

    `TestClient(app)` as a CONTEXT MANAGER is what runs the lifespan -- the
    bare constructor does not, which is why these loops went unexercised by
    this suite for so long.
    """
    from app.main import create_app

    app = create_app(_api_settings(tmp_path))

    with TestClient(app):
        heartbeat = app.state.heartbeat_task
        drain = app.state.error_drain_task
        assert not heartbeat.done(), "heartbeat task is not running"
        assert not drain.done(), "error drain task is not running"


async def test_the_lifespan_cancels_both_tasks_on_shutdown(tmp_path: Path) -> None:
    """THE HALF THAT DID NOT EXIST BEFORE.

    Leaving these tasks running past shutdown is how a process ends with
    "Task was destroyed but it is pending!" -- noise from the one moment an
    operator is most likely to be reading the log.

    DRIVEN DIRECTLY, NOT THROUGH `TestClient`, AND THAT IS THE WHOLE POINT.
    An earlier version of this test used `with TestClient(app):` and asserted
    the tasks were cancelled afterwards. It passed -- and it passed with the
    lifespan's `task.cancel()` DELETED, because `TestClient` tears down the
    event loop it created on exit and the loop's own teardown cancels
    whatever is still pending. The assertion was measuring anyio, not this
    code. Found by planting the deletion; the test did not notice.

    Entering the lifespan directly leaves the surrounding loop alive after
    the context exits, so a cancelled task here was cancelled by the
    `finally` block and nothing else.
    """
    from app.main import create_app, lifespan

    app = create_app(_api_settings(tmp_path))

    async with lifespan(app):
        heartbeat = app.state.heartbeat_task
        drain = app.state.error_drain_task
        assert not heartbeat.done()
        assert not drain.done()

    # The loop is still running; only the lifespan's finally block has run.
    assert heartbeat.cancelled(), "heartbeat task was not cancelled by the lifespan"
    assert drain.cancelled(), "error drain task was not cancelled by the lifespan"


def test_create_app_registers_no_deprecated_on_event_handlers(tmp_path: Path) -> None:
    """The router keeps `on_startup`/`on_shutdown` lists that `on_event`
    appends to. Both must be empty: a handler added there later would run
    outside the lifespan's try/finally and so would never be cleaned up,
    which is the exact gap this change closed.
    """
    from app.main import create_app

    app = create_app(_api_settings(tmp_path))

    assert app.router.on_startup == []
    assert app.router.on_shutdown == []
