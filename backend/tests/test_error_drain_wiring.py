"""Task 3, API side: `app.main.drain_platform_errors_once` -- the
`write_api_heartbeat_once`-style seam so a test can run one drain iteration
without a running loop.
"""

import logging

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.errors.capture import capture_exception, drained
from app.main import drain_platform_errors_once
from app.models import PlatformErrorGroup


async def test_drain_platform_errors_once_writes_the_capture_queue(
    app_engine: AsyncEngine,
) -> None:
    drained()  # start empty
    try:
        raise ValueError("boom: api drain fixture")
    except ValueError as exc:
        captured = capture_exception(exc, where="api")
    assert captured is not None

    written = await drain_platform_errors_once(app_engine)
    assert written == 1

    # BY FINGERPRINT, NOT BY `exc_type`. Every test file in this suite shares
    # one database, and several of them capture a `ValueError` -- from their
    # own raise sites, so their own fingerprints, so their own group rows.
    # Selecting on `exc_type == "ValueError"` therefore matched this test's
    # row and everyone else's, and `scalar_one()` raised
    # `MultipleResultsFound` as soon as any other `test_error_*` file ran
    # first. Under pytest's default RANDOM ordering -- which CI uses, and
    # which `-p no:randomly` hides -- that is an order-dependent failure in
    # two separate drain tests.
    #
    # The fingerprint is this capture's own identity, so the assertion is
    # about this test's row and nobody else's. Shared-database contamination
    # is fixed by asking a precise question, not by trying to empty a
    # database other tests are concurrently filling.
    async with AsyncSession(app_engine) as session:
        group = (
            await session.execute(
                select(PlatformErrorGroup).where(
                    PlatformErrorGroup.fingerprint == captured.fingerprint
                )
            )
        ).scalar_one()
    assert group.where_ == "api"
    assert group.exc_type == "ValueError"


async def test_drain_platform_errors_once_returns_zero_for_an_empty_queue(
    app_engine: AsyncEngine,
) -> None:
    drained()  # start empty
    assert await drain_platform_errors_once(app_engine) == 0


async def test_a_drain_failure_is_swallowed_and_logged(
    app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """THE SWALLOW IS THE POINT (matches `write_api_heartbeat_once`): a
    drain failure -- a dead database, a bad migration -- must not take the
    API process down."""
    import app.main as main_mod

    def _boom(_session: object) -> None:
        raise RuntimeError("db is down")

    monkeypatch.setattr(main_mod, "drain_once", _boom)
    with caplog.at_level(logging.ERROR, logger="its.api"):
        result = await drain_platform_errors_once(app_engine)
    assert result == 0
    assert any("failed to drain platform errors" in r.getMessage() for r in caplog.records)
