"""Replaces asyncio's own event-loop exception handler.

Spec §6's first carried follow-up (docs/superpowers/specs/
2026-08-15-error-monitoring-design.md). `asyncio.BaseEventLoop.
default_exception_handler` renders EVERY value in the context dict it is
handed through `repr()` -- see its own source
(`asyncio/base_events.py::default_exception_handler`), which does exactly
`value = repr(value)` for every key but `message`/`exception`/
`source_traceback`. The context for "a Task's exception was never retrieved"
(`asyncio/futures.py::Future.__del__`, which `Task.__del__` also calls)
carries a `future` key holding the Task itself, and `Task`/`Future.__repr__`
embeds `exception={self._exception!r}` (`asyncio/base_futures.py::
_future_repr_info`) -- so exception text this project's logging kernel
refuses to print via `exc_info` (`app/core/logging.py`'s `JsonFormatter`
reads only `exc_info[0].__name__`, never `str(exc)`/`repr(exc)`) is printed
anyway, pre-rendered into the DEFAULT handler's own `msg` string, on every
asyncio tree this platform runs (the API and the gateway both are one; the
worker is not -- Celery tasks run under `asyncio.run()` per call, each loop
closed before the next, and Celery's own signal-based failure handling
(`app/worker.py`'s `task_failure` receiver) is what covers it there).

`install_asyncio_exception_handler` replaces the handler entirely rather than
wrapping or filtering it -- the same shape decision `app/core/logging.py`'s
`_CeleryHandler` made for the identical class of problem (detecting which
context shapes are dangerous is guesswork that fails open; suppressing the
framework's own rendering and rebuilding a safe, static message from known-
safe pieces does not). The replacement CAPTURES the exception (this sub-
project's `capture_exception`, so a task nobody awaits still becomes a
counted group) and logs exactly one STATIC line -- never the context dict,
never `repr()` of anything in it.
"""

import asyncio
import logging
from typing import Any

from app.core.errors.capture import capture_exception

logger = logging.getLogger(__name__)


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop, *, where: str) -> None:
    """Install the replacement handler on `loop`.

    `where` is the capture seam name (`capture_exception`'s convention --
    `"api"` | `"worker"` | `"gateway"`); callers pass their own process's
    name. Idempotent to call more than once (each call simply replaces the
    handler with an equivalent closure), so it is safe to call once per
    process startup without guarding against a second call.
    """

    def _handle(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        exc = exc if isinstance(exc, BaseException) else None
        if exc is not None:
            capture_exception(exc, where=where)
        # STATIC message, no %-arg, no f-string -- never the context dict,
        # which is exactly what asyncio's own default handler renders via
        # `repr()` per key (see module docstring). `exc_info` carries the
        # exception itself when there is one; `JsonFormatter` reads only its
        # TYPE off that, matching every other guarded call site in this
        # kernel.
        logger.error(
            "unhandled error in the asyncio event loop",
            exc_info=exc,
        )

    loop.set_exception_handler(_handle)
