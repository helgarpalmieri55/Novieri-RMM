"""Capture and fingerprinting -- the seam every later error-monitoring task
builds on (docs/superpowers/specs/2026-08-15-error-monitoring-design.md, §2-3).

THE INVARIANT (spec §0.1): a captured record carries no exception text. Not
the message, not the traceback, not the locals. `CapturedError` below has no
field that could hold any of them. This platform spent two sub-projects (the
logging kernel and its redaction gate) keeping exception text out of the log
stream; an error monitor that stored tracebacks would undo both, in a
durable store, on purpose.

Grouping is by `(exc_type, module, lineno, qualname)` at the site the failure
BELONGS to, not the catch site. Every `capture_exception` call in this
codebase happens at one of a handful of shared catch seams (the request
middleware, the Celery `task_failure` signal, the gateway's exception paths)
-- a catch-site fingerprint would fold every distinct failure funnelled
through one handler into a single, meaningless group (spec §2.1).

WHICH FRAME THAT IS CHANGED ONCE, AND THE FINGERPRINT IS VERSIONED FOR IT
(recommendation 9 of docs/superpowers/audits/2026-08-21-ux-remediation-
closeout.md, finding B79). Version 1 took the traceback's LAST frame, which is
the raise site and is correct whenever this codebase raises. It is wrong
whenever application code calls a library and the LIBRARY raises: the last
frame is then `httpx`/`asyncpg`/`sqlalchemy`, so the group is named after the
library, the console shows a third-party path and line number for a bug in
this codebase, and every distinct failure funnelled through one library
collapses toward that library's few raise lines -- the same collapse a
catch-site fingerprint would cause, one layer down. Version 2 prefers the
DEEPEST application frame and falls back to the raise site when there is none
(`_raise_site`).

THERE IS NO BACKFILL AND THERE CANNOT BE ONE. `platform_error_group` stores
only the DERIVED site -- `exc_type`, `module`, `lineno`, `qualname` -- and
never the traceback (spec §0.1: a captured record carries no exception text,
and a traceback is exception text). For a group already named after a library,
the application frame it should have been named after is simply gone. So
existing rows keep `fingerprint_version = 1` and age out as their sites stop
failing; new captures record version 2. The two can never collide because the
version is part of the HASHED INPUT, not merely a column beside it -- see
`fingerprint_of`.
"""

import hashlib
import logging
import threading
from collections import deque
from dataclasses import dataclass
from types import TracebackType

# `_shaped_type_name` is private to `app.core.logging`, but it IS the shape
# gate spec §2 names explicitly: `type.__name__` is set at class-definition
# time, so `type("postgres://u:pw@h/db", (Exception,), {})` is legal Python
# and its `.__name__` really is that string -- a dynamically-named class
# reaching a rendered type name has no shape gate in the way unless something
# puts one there. `app.core.logging` already built and tested that gate for
# the identical problem (`JsonFormatter`'s `exc_type` field). Reusing it here
# -- rather than re-deriving the same character-class rule -- is deliberate:
# there is exactly one implementation of "is this type name safe to write
# out", and it does not drift if the logging kernel's rule ever changes.
from app.core.logging import (
    _shaped_type_name,
    device_id_var,
    job_id_var,
    org_id_var,
    request_id_var,
    user_id_var,
)

logger = logging.getLogger(__name__)

# Bound on the in-process capture queue (spec §3.1). Not a `deque(maxlen=...)`
# -- see `_enqueue`'s docstring for why that would be the wrong failure mode.
QUEUE_MAX = 1000

# Written in place of a type's own `__name__` when that name fails the shape
# gate. Matches the placeholder `app.core.logging.JsonFormatter` already
# writes for the same failure -- the type name is not merely unavailable
# here, it is REFUSED, and both call sites say so the same way.
_UNSHAPED_TYPE = "<unprintable>"

# Which rule produced a fingerprint. 1: the traceback's last frame. 2: the
# deepest application frame, falling back to the last frame. Bumping this
# changes the digest of every subsequent capture (it is hashed, not merely
# recorded), which is the point -- an old group and a new one for the same
# failure are two rows an operator can tell apart, rather than one row whose
# meaning silently changed under it.
FINGERPRINT_VERSION = 2

# What counts as application code. DERIVED FROM THIS MODULE'S OWN NAME, not
# spelled out: `app.core.errors.capture` -> `app`, which is the distribution's
# only package (`pyproject.toml`'s `packages = ["app"]`, and
# `deploy/backend.Dockerfile` copies `app`, `migrations` and `alembic.ini`
# and nothing else). A literal `"app."` here would be a third place holding
# the same fact and the one place nothing would update if the package were
# ever renamed; this expression cannot go stale, because a renamed package
# renames this module too.
_APP_ROOT = __name__.split(".", 1)[0]


def _is_app_module(module_name: str) -> bool:
    """`app`, or anything beneath it. The dot matters: a bare
    `startswith(_APP_ROOT)` would also claim `application_insights`, a package
    this codebase does not own."""
    return module_name == _APP_ROOT or module_name.startswith(f"{_APP_ROOT}.")


@dataclass(frozen=True, slots=True)
class CapturedError:
    """One captured failure -- spec §2, verbatim.

    No `message`. No `traceback`. No `locals`. No `args`. `exc_type` is a
    bare type name (`"ValueError"`), never `str(exc)`; `module`/`lineno`/
    `qualname` name the deepest APPLICATION frame -- the raise site itself
    whenever this codebase raised, and never the catch seam (`_raise_site`);
    `where` is the seam that caught it (`"api"` | `"worker"` | `"gateway"`);
    the rest are the
    correlation ids read from the contextvars `app.core.logging` installed,
    each `None` -- not `""` -- when unbound, because the two mean different
    things to a filter (spec's absent-correlation-ids test).
    """

    fingerprint: str
    # Which rule produced `fingerprint` -- see `FINGERPRINT_VERSION`. Carried
    # on the record rather than assumed at the drain, so a row can always be
    # read back as "this is what the rule of the day said", not as "this is
    # what today's rule would say".
    fingerprint_version: int
    exc_type: str
    module: str
    lineno: int
    qualname: str
    where: str
    request_id: str | None
    org_id: str | None
    user_id: str | None
    job_id: str | None
    device_id: str | None


@dataclass(frozen=True, slots=True)
class _RaiseSite:
    """Where an exception was raised, not where it was caught."""

    module: str
    lineno: int
    qualname: str


_UNKNOWN_SITE = _RaiseSite(module="", lineno=0, qualname="")


def _site_of(entry: TracebackType) -> _RaiseSite:
    return _RaiseSite(
        module=str(entry.tb_frame.f_globals.get("__name__", "")),
        lineno=entry.tb_lineno,
        qualname=str(entry.tb_frame.f_code.co_qualname),
    )


def _raise_site(tb: TracebackType | None) -> _RaiseSite:
    """The DEEPEST application frame of `tb`, or its last frame if it has none.

    DIRECTION FIRST, because it is the easy thing to get backwards. A
    traceback's frames run outward from where the exception was raised to
    where it is being inspected: `tb.tb_frame` is the CATCH site and
    `tb.tb_next` walks one step CLOSER TO THE RAISE with each hop. "Deepest
    application frame" therefore means the LAST `app.*` frame in `tb_next`
    order -- the application frame nearest the raise, the one that called
    whatever actually blew up. Taking the FIRST one met walking from `tb`
    would take the application frame nearest the CATCH, which on the API seam
    is the route handler, and would name every failure beneath it the same
    thing.

    THE FIRST FRAME IS NEVER A CANDIDATE, and that exclusion is the whole
    reason this rule does not quietly become catch-site grouping. Every
    capture in this codebase runs at a seam that is itself `app.*` code
    (`app/api/request_context.py`, `app/worker.py`, `app/gateway/main.py`), so
    a rule that merely preferred application frames would answer "the seam"
    for exactly the case this change is about: a library raising with no
    application code between it and the handler that caught it. That is the
    collapse spec §2.1 rejected, so the search starts one frame in. A raise
    inside the seam function itself still lands there, via the fallback --
    correctly, because then the seam really is the raise site.

    WHEN THERE IS NO APPLICATION FRAME AT ALL -- a failure raised entirely
    inside a library from a library-scheduled callback -- the answer is the
    last frame, i.e. version 1's behaviour. A library's own bug is still a
    failure this platform has to show, and the raise site is the only true
    thing left to say about it.
    """
    entries: list[TracebackType] = []
    entry: TracebackType | None = tb
    while entry is not None:
        entries.append(entry)
        entry = entry.tb_next
    if not entries:
        return _UNKNOWN_SITE
    for candidate in reversed(entries[1:]):
        if _is_app_module(str(candidate.tb_frame.f_globals.get("__name__", ""))):
            return _site_of(candidate)
    return _site_of(entries[-1])


def _exc_type_name(exc: BaseException) -> str:
    """`type(exc).__name__`, through the logging kernel's shape gate.

    `type(name, (Exception,), {})` puts arbitrary text where a type name is
    expected, and the type name is the ONE string this design lets through
    (spec §2) -- so it does not get to skip the gate just because it looks
    structural rather than free text.
    """
    name = _shaped_type_name(type(exc))
    return name if name is not None else _UNSHAPED_TYPE


def fingerprint_of(exc: BaseException, tb: TracebackType | None) -> str:
    """sha256 of `v2|type|module|lineno|qualname` at `_raise_site`, truncated
    to 16 hex characters.

    Deliberately excludes the message (spec §2.1): messages interpolate data
    (`"device 41f2 not found"`), so message-grouping would produce one group
    per device and defeat the purpose of grouping at all. Two different
    `ValueError`s raised from the same line are, by design, one group.

    THE VERSION IS IN THE HASHED INPUT, NOT ONLY IN A COLUMN, and that is the
    deliberate half of this. `platform_error_group.fingerprint` is UNIQUE and
    the drain upserts `ON CONFLICT (fingerprint)`; the alternative considered
    was a composite unique on `(fingerprint_version, fingerprint)`, which
    would have made every reader of the table responsible for remembering to
    carry the version -- the drain's own in-Python batch grouping, the API's
    lookups, any later query joining on a fingerprint -- and would have failed
    OPEN on the day one of them forgot, silently merging two eras of a rule.
    Prefixing the input instead puts version 2's digests in an input space
    version 1's cannot occupy, so the disjointness is a property of the digest
    rather than of a key some query might drop half of, and the existing
    unique index, upsert and routes are untouched.

    The two spaces really are disjoint, not merely unlikely to overlap: a
    version-1 input is four `|`-separated fields whose THIRD is `lineno` and
    therefore all digits, while a version-2 input's third field is a module
    name, and no Python module name is all digits (an identifier cannot begin
    with one). So even the adversarial case -- an exception class literally
    named `v2` -- cannot produce a version-1 string that a version-2 string
    equals.
    """
    site = _raise_site(tb)
    type_name = _exc_type_name(exc)
    digest = hashlib.sha256(
        f"v{FINGERPRINT_VERSION}|{type_name}|{site.module}|{site.lineno}|{site.qualname}".encode()
    ).hexdigest()
    return digest[:16]


_lock = threading.Lock()
_queue: deque[CapturedError] = deque()
_dropped = 0


def _enqueue(item: CapturedError) -> None:
    """Append to the bounded queue, or drop the arrival and count it.

    NOT `collections.deque(maxlen=QUEUE_MAX)`. That silently evicts the
    OLDEST entry on overflow -- losing the FIRST occurrence of a failure,
    which is the most diagnostically valuable record (first-seen timing,
    the earliest correlation id), and making the drop invisible: nothing
    changes about the deque's length or contents to signal it happened. This
    queue is unbounded in storage and enforces `QUEUE_MAX` itself, dropping
    the NEWEST arrival instead and incrementing `_dropped` so
    `dropped_count()` tells an operator the totals are floors (spec §3.1).
    """
    global _dropped
    with _lock:
        if len(_queue) >= QUEUE_MAX:
            _dropped += 1
            return
        _queue.append(item)


def drained() -> list[CapturedError]:
    """Empty the queue and return everything it held, oldest first.

    Task 2 covers the actual drain-to-Postgres loop; this only hands back
    what is currently queued and clears it, atomically with respect to
    `_enqueue`.
    """
    with _lock:
        items = list(_queue)
        _queue.clear()
        return items


def dropped_count() -> int:
    """How many arrivals have been dropped for overflow so far."""
    with _lock:
        return _dropped


def capture_exception(exc: BaseException, *, where: str) -> CapturedError | None:
    """The one capture entry point (spec §3).

    Reads the raise site off `exc.__traceback__` and the five correlation
    contextvars, builds a `CapturedError`, and enqueues it. Must be called
    while still inside the correlation context (e.g. from the request
    middleware, not after it has unwound) for the ids to be populated.

    Never raises. A monitoring failure must not become the outage (spec §3 /
    falsifier #2): the whole body is guarded, a failure is logged with
    `exc_info` -- type only, exactly as every other fail-closed path in this
    kernel -- and `None` is returned instead of propagating.
    """
    try:
        tb = exc.__traceback__
        site = _raise_site(tb)
        captured = CapturedError(
            fingerprint=fingerprint_of(exc, tb),
            fingerprint_version=FINGERPRINT_VERSION,
            exc_type=_exc_type_name(exc),
            module=site.module,
            lineno=site.lineno,
            qualname=site.qualname,
            where=where,
            request_id=request_id_var.get(),
            org_id=org_id_var.get(),
            user_id=user_id_var.get(),
            job_id=job_id_var.get(),
            device_id=device_id_var.get(),
        )
        _enqueue(captured)
        return captured
    except Exception:
        logger.error("capture_exception failed", exc_info=True)
        return None
