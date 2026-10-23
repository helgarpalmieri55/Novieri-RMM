"""Text that is written down is English, whatever language the request spoke.

A translated exception's `str()` is in the language of the request that raised
it. Code that catches such an exception and stores the message (a run's error, a
target's last_error, an audit entry) would store Spanish for the next person to
read in English, and the audit chain is never translated. `stored_in_english`
runs the whole coroutine under `use_locale("en")`, so everything it raises,
catches and writes is English; the exceptions that reach a response are raised
elsewhere, in the request's language.
"""

import functools
from collections.abc import Callable, Coroutine
from typing import Any

from app.core.i18n import use_locale


def stored_in_english[**P, R](
    fn: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with use_locale("en"):
            return await fn(*args, **kwargs)

    return wrapper
