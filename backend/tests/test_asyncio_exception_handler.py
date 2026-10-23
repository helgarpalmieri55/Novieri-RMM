"""Task 3, spec §6's first carried follow-up: `install_asyncio_exception_
handler` replaces asyncio's own loop exception handler, which `repr()`s
every context value -- including, for an unretrieved Task's exception, the
Task itself, whose `__repr__` embeds `repr(exc)` -- pre-rendering exception
text into the DEFAULT handler's own message, on every asyncio tree this
platform runs.

PINNED, per the task brief: the secret's `repr()` must be shown to leak
under the DEFAULT handler first -- an input that never carried the secret
proves nothing about the fix -- and only then shown absent under the
installed one, with the error still captured.
"""

import asyncio
import gc
import logging

from app.core.errors.asyncio_handler import install_asyncio_exception_handler
from app.core.errors.capture import drained

SECRET = "S3CRET-TASK-PAYLOAD-9f1c"  # pragma: allowlist secret


class _SecretRepr(Exception):
    """Its `repr()` -- not its `str()` -- carries the secret, matching
    `Future.__repr__`'s own `exception={self._exception!r}` (`asyncio/
    base_futures.py::_future_repr_info`), which is exactly what the default
    handler renders for the 'future' context key."""

    def __repr__(self) -> str:
        return f"<_SecretRepr leaked={SECRET}>"


async def _boom() -> None:
    raise _SecretRepr()


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def _run_unretrieved_secret_task() -> None:
    """Create a task that raises `_SecretRepr`, never await/retrieve its
    result, then force a GC pass -- `Future.__del__` (`Task.__del__` calls
    it via `super().__del__()`) is what fires `loop.call_exception_handler`
    for "exception was never retrieved", the exact path spec §6 names."""
    task = asyncio.get_running_loop().create_task(_boom())
    await asyncio.sleep(0)  # let it run to completion (and fail)
    del task
    gc.collect()


async def test_asyncios_default_handler_would_have_leaked_the_secret() -> None:
    """Proves the vector before proving the fix -- without this, a test that
    happened to never exercise the leaky path would pass for the wrong
    reason."""
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(None)  # explicit: restore asyncio's own default
    handler = _RecordingHandler()
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.addHandler(handler)
    asyncio_logger.setLevel(logging.DEBUG)
    try:
        await _run_unretrieved_secret_task()
    finally:
        asyncio_logger.removeHandler(handler)

    assert any(SECRET in m for m in handler.messages), (
        "the default asyncio handler did not leak the secret -- this input "
        "does not exercise the vector the real fix closes, so absence below "
        "would prove nothing"
    )


async def test_the_installed_handler_never_leaks_the_secret_and_still_captures() -> None:
    drained()  # start empty
    loop = asyncio.get_running_loop()
    install_asyncio_exception_handler(loop, where="api")
    handler = _RecordingHandler()
    handler_logger = logging.getLogger("app.core.errors.asyncio_handler")
    handler_logger.addHandler(handler)
    handler_logger.setLevel(logging.DEBUG)
    try:
        await _run_unretrieved_secret_task()
    finally:
        handler_logger.removeHandler(handler)
        loop.set_exception_handler(None)  # don't leak into another test

    assert not any(SECRET in m for m in handler.messages), (
        f"the secret leaked into a log message: {handler.messages}"
    )
    captured = drained()
    assert any(c.exc_type == "_SecretRepr" and c.where == "api" for c in captured), (
        "the unretrieved task's exception was not captured"
    )
