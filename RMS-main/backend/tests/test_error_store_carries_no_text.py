"""Task 7, the end-to-end falsification: exception TEXT cannot reach the
error store.

This is the invariant the whole sub-project exists to hold (spec §0.1), and
it is the one worth testing against the DATABASE rather than against
`CapturedError`'s field list. A dataclass with no `message` field proves
only that today's dataclass has no `message` field; this drains a real
capture into real Postgres and then reads back EVERY column of BOTH tables,
asking the rows themselves whether the secret is in there.

WHY THE INPUT IS CHECKED FIRST (`test_the_secret_is_really_in_the_raised_exception`):
an input that never carried the secret cannot demonstrate the secret was
removed. Two of the previous sub-project's redaction checks were vacuous for
exactly that reason -- they asserted a password was absent from output that
was never given the password to begin with. So the first test below asserts
the raise is genuinely secret-bearing, and it would fail if someone
"simplified" the fixture into an exception whose message no longer contains
the DSN.
"""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.errors.capture import capture_exception, drained
from app.main import drain_platform_errors_once
from app.models import PlatformErrorEvent, PlatformErrorGroup

# A DSN of the shape this platform really holds (the app database URL, the
# Celery broker URL, the vault address). The password is the needle.
SECRET_PASSWORD = "sUp3r-s3cret-pw"  # noqa: S105  (test fixture, not a credential)
SECRET_DSN = f"postgresql+asyncpg://its_admin:{SECRET_PASSWORD}@db.internal:5432/itsuite"


def _raise_secret_bearing() -> BaseException:
    """Raise, and hand back, an exception whose message really is the DSN.

    Deliberately NOT a class with a hand-written `__str__` that returns the
    expected string. That trick produces a test which proves only that the
    test's own fixture works: earlier in this project a leak was reported as
    "measured" on exactly such an exception, and the claim was false. Here
    the message is the DSN because the DSN was passed to the constructor,
    which is how a real driver error carries one.
    """
    try:
        raise ConnectionError(f"could not connect to {SECRET_DSN}")
    except ConnectionError as exc:
        return exc


def test_the_secret_is_really_in_the_raised_exception() -> None:
    """The non-vacuity gate. If this fails, every assertion below is empty."""
    exc = _raise_secret_bearing()
    assert SECRET_PASSWORD in str(exc)
    assert SECRET_PASSWORD in repr(exc)


def test_a_secret_bearing_raise_puts_no_text_in_the_capture() -> None:
    """In-process half: the queued record carries no field holding the text."""
    drained()  # start empty
    exc = _raise_secret_bearing()
    captured = capture_exception(exc, where="api")
    assert captured is not None

    # Every field of the record, stringified, in one haystack.
    haystack = " ".join(str(getattr(captured, f)) for f in captured.__slots__)
    assert SECRET_PASSWORD not in haystack
    assert SECRET_DSN not in haystack
    # And the type name did survive -- otherwise this passes by capturing
    # nothing at all.
    assert captured.exc_type == "ConnectionError"


async def test_a_secret_bearing_raise_puts_no_text_in_postgres(
    app_engine: AsyncEngine,
) -> None:
    """The end-to-end half: drain to real Postgres, then read back every
    column of both tables and search all of it."""
    drained()  # start empty
    exc = _raise_secret_bearing()
    captured = capture_exception(exc, where="api")
    assert captured is not None

    written = await drain_platform_errors_once(app_engine)
    assert written == 1

    async with AsyncSession(app_engine) as session:
        group = (
            await session.execute(
                select(PlatformErrorGroup).where(
                    PlatformErrorGroup.fingerprint == captured.fingerprint
                )
            )
        ).scalar_one()
        events = list(
            (
                await session.execute(
                    select(PlatformErrorEvent).where(PlatformErrorEvent.group_id == group.id)
                )
            )
            .scalars()
            .all()
        )

    assert events, "no event row -- the search below would be vacuous"

    def _all_columns(row: Any) -> list[str]:
        return [str(getattr(row, c.name)) for c in row.__table__.columns]

    values = _all_columns(group)
    for event in events:
        values.extend(_all_columns(event))

    haystack = " ".join(values)
    assert SECRET_PASSWORD not in haystack
    assert SECRET_DSN not in haystack
    assert "could not connect" not in haystack

    # NON-VACUITY, AND IT WAS CHECKED BY PLANTING, NOT BY REASONING. The
    # danger in a "secret is absent" test is that the haystack is empty --
    # `_all_columns` returning nothing would make every assertion above pass
    # while proving nothing. Asserting `"ConnectionError" not in haystack`
    # here MUST fail, and does: it reports the whole row, e.g.
    #
    #   2d6491fdd ConnectionError tests.test_error_store_carries_no_text 48
    #   _raise_secret_bearing api 2026-08-15 20:33:00.065532+00:00
    #   2026-08-15 20:33:00.065532+00:00 1 None 06a80ccf-...-40d6
    #   06a80ccf-...-8e68a7b 2026-08-15 20:33:00.065532+00:00 None None None
    #   None None
    #
    # -- every column of both tables, from Postgres. The type name, the
    # module, the line, the qualname and the correlation ids are all there;
    # the message the exception was constructed with is not.
    assert group.exc_type == "ConnectionError"
    assert group.qualname == "_raise_secret_bearing"


async def test_a_broken_drain_cannot_break_a_request(
    app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 7 step 3, at the drain seam: monitoring must never become the
    outage. With the drain itself raising, the drain call still returns and
    the process stays up -- the failure is contained, not propagated."""
    import app.main as main_mod

    def _boom(_session: object) -> None:
        raise RuntimeError("the error store is down")

    monkeypatch.setattr(main_mod, "drain_once", _boom)
    assert await drain_platform_errors_once(app_engine) == 0
