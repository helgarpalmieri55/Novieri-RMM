"""The test harness's own resource discipline, because Postgres counts.

WHY THIS FILE EXISTS. The full backend suite failed with

    asyncpg.exceptions.TooManyConnectionsError: remaining connection slots are
    reserved for roles with the SUPERUSER attribute

in `tests/test_realm_settings_api.py`, on two different tests on two different
runs. Neither of them leaked anything. That is the worst shape a resource leak
takes: it accuses whichever test happened to be running when the last of
Postgres's 100 slots went, and the accused test is innocent.

The actual leak was in the harness. `app.main.create_app()` builds two engines
of its own -- `app.state.app_engine` and `app.state.admin_engine` -- and the
`api_env` fixture in `tests/conftest.py`, which 137 test files take, disposed
neither. A pooled engine holds its checked-in connections open on purpose;
they are released when the engine is disposed, and otherwise only when CPython
finalises the engine and asyncpg's `__del__` terminates the socket. Hence the
intermittency: the suite passed or failed on garbage-collection timing.

The first test below proves the mechanism -- that an undisposed engine really
does hold a server-side backend, and that disposal really does return it. The
second is a structural check on the fixture itself, and it is a weaker thing:
a test cannot observe its own fixture's teardown, so this reads the source.
"""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CONFTEST = Path(__file__).resolve().parent / "conftest.py"

#: Enough apps that the assertion cannot pass on noise from another test
#: sharing this cluster, small enough to be nowhere near the 100-slot ceiling.
APPS = 4


async def _backends(engine, dbname: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = :d"),
                {"d": dbname},
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_an_app_holds_postgres_backends_until_it_is_disposed(
    pg_cluster, admin_engine, tmp_path
) -> None:
    """THE MECHANISM, and the falsifier for the fixture change: delete the
    disposal loop below and the final assertion fails -- the backends the four
    apps opened are still there. That is exactly what `api_env` was doing, once
    per test, across the whole suite."""
    from app.core.config import Settings
    from app.main import create_app
    from tests.fake_idp import ISSUER, JWKS_URL

    before = await _backends(admin_engine, pg_cluster.dbname)

    apps = [
        create_app(
            Settings(
                database_url=pg_cluster.app_url,
                database_admin_url=pg_cluster.admin_url,
                database_portal_url=pg_cluster.portal_url,
                oidc_issuer=ISSUER,
                oidc_jwks_url=JWKS_URL,
                ca_dir=tmp_path / f"ca-{i}",
                signing_key_path=tmp_path / f"job-signing-{i}.key",
            )
        )
        for i in range(APPS)
    ]
    # One statement each, which is what serving one request costs. The
    # connection goes back to the pool afterwards -- checked in, still open.
    for app in apps:
        async with AsyncSession(app.state.app_engine) as session:
            await session.execute(text("SELECT 1"))

    during = await _backends(admin_engine, pg_cluster.dbname)
    assert during >= before + APPS, (
        "an engine that served a statement must be holding a backend; if this "
        "fails the rest of the test proves nothing"
    )

    for app in apps:
        await app.state.app_engine.dispose()
        await app.state.admin_engine.dispose()

    assert await _backends(admin_engine, pg_cluster.dbname) <= before


def test_the_api_env_fixture_disposes_the_engines_create_app_built() -> None:
    """WHAT THIS CANNOT DO is watch the teardown run -- a test finishes before
    its own fixtures unwind. So it reads the fixture's source and requires both
    engines `create_app` owns to be named in a `finally:` block that disposes
    them: under `finally` because a failing test must release them too. Weaker
    than a behavioural assertion, and it still catches the deletion that caused
    the outage."""
    source = CONFTEST.read_text()
    start = source.index("async def api_env(")
    # Cut at the blank-line gap that ends the fixture, not at the next
    # decorator: everything between the two would otherwise count as
    # teardown, and a later definition mentioning these names would make
    # the assertions below pass for the wrong reason.
    fixture = source[start : source.index("\n\n\n", start)]

    assert "finally:" in fixture, "api_env releases nothing on the failure path"
    teardown = fixture[fixture.index("finally:") :]

    assert "dispose()" in teardown
    for owned in ("app.state.app_engine", "app.state.admin_engine"):
        assert owned in teardown, f"{owned} is not released when api_env unwinds"
