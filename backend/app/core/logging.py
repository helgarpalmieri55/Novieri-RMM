"""Structured logging: JSON to stdout, correlation IDs, redaction at the sink.

THIS IS NOT THE AUDIT LOG. `app/core/audit.py` is immutable, hash-chained and
append-only, and its evidentiary value is that the chain verifies (invariant
#4). These lines are OPERATIONAL AND DISPOSABLE -- rotated by the log driver,
unordered across processes, carrying no integrity guarantee. Never route an
audit event through this logger.

REDACTION IS BY FIELD NAME, AND THE HOLE IS DELIBERATE. Any `extra=` field
whose NAME matches `looks_like_secret_key` is replaced with `[redacted]`.
`%`-ARGS ARE NOT COVERED: they render into the message string, where there is
no name to match. So:

    A value that could carry secret material goes in `extra=`, never a %-arg.

`%`-args stay permitted -- they are idiomatic, already used across the
codebase, and their template is static -- but they are OUTSIDE the gate.
`test_a_percent_arg_is_NOT_redacted` pins that, so the limitation cannot be
misremembered as coverage.

WHY STDLIB AND NOT structlog: uvicorn and FastAPI already log through stdlib,
so a dictConfig formatter on the ROOT logger catches their lines too. Under
structlog those need separate wiring and, until somebody does it, escape
redaction entirely.

C-1 (fix wave 7): FIELD-NAME REDACTION ALONE IS NOT ENOUGH ON A PROCESS THAT
DOES NOT OWN EVERY RECORD IT LOGS. Installing `JsonFormatter` on the Celery
worker's root logger (so `exc_info=` stops rendering a traceback, per fix wave
6) also routes Celery's OWN task-failure record through it --
`celery.app.trace.TraceInfo._log_error`, the handler for every unhandled task
exception, logs the full traceback, `repr(exc)` and the task's own args/kwargs
under field names (`traceback`, `exc`, `args`, `kwargs`) that `looks_like_
secret_key` has no reason to flag, AND embeds `exc` into the message string
itself as a `%`-arg -- outside the redaction gate BY DESIGN (see above).

FIX WAVE 8: THE FIRST FIX FOR C-1 WAS ITSELF A LEAK, AND THE SHAPE OF THE
MISTAKE MATTERS MORE THAN THE INSTANCE OF IT. That first fix,
`_CeleryTraceSanitiser`, was a DENYLIST OVER RECORD SHAPES: a `logging.Filter`
that recognised ONE known `celery.app.trace` call (`_log_error`'s -- a `data`
extra carrying a `traceback` key) and rewrote only that. Every other record on
that logger -- `LOG_RETRY`, `LOG_SUCCESS`, `LOG_IGNORED`, none of which carry a
`traceback` key -- failed the filter's guard and passed through UNCHANGED,
Celery's own message and args intact. Measured: `LOG_RETRY`'s template is
`"Task %(name)s[%(id)s] retry: %(exc)s"`, and `Retry.__str__` embeds
`safe_repr(original_exc)` -- a DB password reached worker stdout through
exactly the `%`-arg door this module already documents as outside the
name-based gate, on a record shape the filter was never built to recognise.
Deciding which records are dangerous is guesswork about a third party's
internals, and every wrong guess in that filter failed OPEN. Worse: the
filter's install line (`handler.addFilter(_CeleryTraceSanitiser())` in
`configure_logging` below) had NO test asserting `configure_logging()` itself
installed it -- every one of that filter's own tests built its own handler
and called `addFilter` directly -- so deleting that one line left the entire
suite green while the leak returned in full.

THE FIX STOPS DETECTING AND STARTS SUPPRESSING. `configure_logging` takes
`celery.app.trace` OFF PROPAGATION and gives it a DEDICATED handler,
`_CeleryTraceHandler`, that renders EVERY record delivered to it -- no shape
matching, no guessing -- from a small, fixed set of fields
(`name`/`id`/`hostname`/`description`) that Celery itself controls and that
structurally cannot carry rendered exception text or a task's raw arguments.
It never calls `record.getMessage()` or reads `record.msg`/`record.args` at
all, so Celery's own message text -- where every leak in this class has
lived -- is never rendered, whatever shape the record turns out to be. See
that class's docstring for the full account, and `_NEVER_EMIT` (widened this
same fix wave, finding 3) for the layer that still protects every OTHER
logger in the process from a field literally named `traceback`.

FIX WAVE 9: WAVE 8's SUPPRESSION WAS ONE LOGGER WIDE; THE LEAK IS TREE WIDE.
Wave 8 fixed the SHAPE of the defence (suppress, don't detect) but left its
SCOPE a guess -- `celery.app.trace` alone -- which is the same style of
guesswork finding 3 already named as the failure mode, just at a coarser
grain. `celery/worker/consumer/consumer.py` and `celery/worker/strategy.py`
are genuine `celery`-tree loggers, neither of them `celery.app.trace`, so
wave 8's handler never saw a record from either one; both reached root's
`JsonFormatter` unfiltered, which has no field name to redact a value
sitting in the MESSAGE STRING (§1.2's own documented hole). **The specific
"measured" justification originally recorded here for widening -- that a
redis-py connection error's own text embeds the broker password, unmasked
-- was FALSE. It is retracted, not softened, by fix wave 10 below; see
that section for what actually reproduces on these two loggers.** The
widening to the tree root was, and remains, the right fix regardless --
both loggers sit outside `celery.app.trace` and reach the unfiltered root
formatter either way.

THE FIX WIDENS THE ATTACHMENT POINT, NOT THE GUESSWORK. `configure_logging`
now attaches the same handler -- renamed `_CeleryHandler`, `celery.app.trace`
no longer being the whole story it renders -- to `logging.getLogger("celery")`,
the TREE ROOT every Celery-internal logger descends from
(`celery.utils.log.get_logger` explicitly reparents any logger it hands out
onto this one), with propagation turned off THERE instead of at
`celery.app.trace`. This is not a longer enumeration of "which submodules are
risky" -- it does not enumerate submodules at all. Python's own logger
hierarchy already encodes exactly the predicate this wave needs
(`record.name == "celery"` or `record.name.startswith("celery.")`): a logger
below `celery` that installs no handler of its own -- true of every
stdlib-`logging`-based Celery submodule -- propagates up until something
stops it, and that something is now `celery` itself. `celery.app.trace`'s own
dedicated wiring is REMOVED rather than left alongside this one: it is a
descendant of `celery` and reaches the identical handler by propagation, so a
second attachment point would be two names for one mechanism, drifting apart
the moment someone edits one and not the other.

THE COST, NAMED RATHER THAN HIDDEN. Every Celery-internal logger under this
tree now loses its OWN message text the same way `celery.app.trace` already
did -- "worker ready", "Connection restored", the connection-error text
itself -- because the dedicated handler never reads `msg`/`args` regardless
of which Celery logger handed it the record. What survives: the logger name,
the level, the task identity when the record carries Celery's own `data`
extra, the exception TYPE, and this kernel's correlation fields. That is a
real loss of operator signal, accepted under invariant #3 (a security
invariant wins over convenience) -- see spec §6 for the trade recorded in
full and the remedy if a specific message proves worth getting back: a
named, safe template built from known-safe fields, never the framework's own
text let through again.

FIX WAVE 10: THE REASON RECORDED FOR WAVE 9's WIDENING WAS FABRICATED; THE
WIDENING ITSELF WAS STILL RIGHT. Wave 9's stated justification for taking
`celery.worker.consumer` and `celery.worker.strategy` onto this same
suppression was that a redis-py connection error's own text embeds the
broker password, unmasked, when Celery's `CONNECTION_ERROR` handler logs
it. MEASURED AGAINST THE PINNED DEPENDENCIES (redis 8.1.0, kombu 5.6.2,
celery 5.6.3) AND IT DOES NOT REPRODUCE: pointed at a dead broker on
`redis://:S3CRETBROKERPW@127.0.0.1:16399/0`, both redis-py and kombu report
only

    Error 111 connecting to 127.0.0.1:16399. Connection refused.

-- `redis/utils.py::format_error_message` interpolates
`Connection._host_error()`, which is `f"{self.host}:{self.port}"`, never
the URL and never the userinfo. The password does not reach the
exception's own text on either driver at this measured version pin. The
two tests offered as proof (`test_celery_worker_consumer_broker_
connection_failure_never_leaks_the_password`,
`test_celery_worker_strategy_eta_conversion_failure_never_leaks_the_dsn`,
both since replaced) did not exercise redis-py at all: `_BrokerConnErr.
__str__` and `_StrategyDsnErr.__init__` hardcoded the string the prose
claimed, so both passed by construction against any dependency version and
could never falsify the premise. THIS IS RECORDED HERE, NOT QUIETLY FIXED,
because a future reader who finds the old claim in git history needs to
know it was retracted deliberately, not merely edited around.

THE REAL, REPRODUCIBLE LEAK ON THE SAME LOGGER: `celery/worker/consumer/
consumer.py` renders the RAW SERIALIZED TASK BODY into the message at four
call sites -- `on_unknown_task` (`UNKNOWN_TASK_ERROR`), the message-report
helper and `on_invalid_task` (`INVALID_TASK_ERROR`), and `on_decode_error`
(`MESSAGE_DECODE_ERROR`) -- all four via `dump_body(message, body)`.
Measured:

    dump_body(m, b'[[], {"password": "PROVISIONING-SECRET", "device": "d1"}, {}]')
      -> 'b\'[[], {"password": "PROVISIONING-SECRET", "device": "d1"}, {}]\' (61b)'

That is task KWARGS -- this platform's provisioning and remote-control
material -- rendered into the message string, exactly where §1.2 says
name-based redaction cannot reach, and it fires on the same ordinary class
of event (Celery permanently rejecting a message) the retracted claim
pointed at, not an exotic shape. `celery/worker/strategy.py`'s
`req.info(safe=True)` (`argsrepr`/`kwargsrepr`, logged at the
ETA-conversion error site) is a second, smaller instance of the same
shape. Both are already covered by the mechanism this handler already
has, unchanged by this correction: it never reads `record.getMessage()`,
`record.msg`, or `record.args` at all, so the serialized body cannot reach
output regardless of which call site rendered it into the message. THE
SUPPRESSION ITSELF WAS ALREADY CORRECT AND DOES NOT CHANGE HERE -- only the
reason recorded for it was wrong, and this section is that correction.

FIX WAVE 10 ALSO CLOSES A FAIL-OPEN ERROR PATH IN `_CeleryHandler.emit`
(see that method's own comment) AND NAMES THE POISON-MESSAGE PATH'S LOSS OF
OPERATOR SIGNAL EXPLICITLY, PARTIALLY REDUCING IT (see
`_synthesize_celery_record`'s docstring) -- both findings on this same
suppression, found while re-verifying the widening this section corrects.
"""

import contextvars
import json
import logging
import math
import re
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.secrets import looks_like_secret_key

REDACTED = "[redacted]"

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
org_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "org_id", default=None
)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_id", default=None
)
job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "job_id", default=None
)
device_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "device_id", default=None
)

_CONTEXT_VARS = {
    "request_id": request_id_var,
    "org_id": org_id_var,
    "user_id": user_id_var,
    "job_id": job_id_var,
    "device_id": device_id_var,
}

# LogRecord's own attributes. Anything NOT here arrived via `extra=` and is a
# field the caller chose, so it is emitted (after the name check). Derived from
# a real record rather than hand-listed, because the list differs by Python
# version and a hand-list silently drops new fields into the output.
_RESERVED = frozenset(
    vars(
        logging.LogRecord("x", logging.INFO, "x", 0, "x", (), None)
    )
) | {"asctime", "message", "taskName"}

# Types that cannot carry a credential and are useless as bare strings in a log
# line, so they are coerced with str() rather than reduced to a type name.
_COERCIBLE: tuple[type, ...] = (UUID, datetime, date, Decimal, Path)

# C-1 (fix wave 7), widened by fix wave 8 finding 3: field names that
# structurally carry exception text or a call's raw arguments, redacted
# UNCONDITIONALLY at any depth -- regardless of what `looks_like_secret_key`
# says -- in both key paths (see `_shape_dict`, which is now the only
# implementation of both).
#
# THIS IS DEFENCE IN DEPTH FOR `_CeleryHandler` (`configure_logging` below),
# NOT A SUBSTITUTE FOR IT. That handler renders EVERY record from the
# `celery` logger tree (fix wave 9 widened it from `celery.app.trace` alone)
# from a fixed, known-safe field set and never reads Celery's own message or
# args at all -- but this set is what protects every OTHER logger in the
# process: a caller anywhere in this codebase who names an `extra=` field
# `traceback` believing it is just another string, or a future task-tracing
# framework this platform never anticipated.
#
# CASE-FOLDED, NOT EXACT-MATCH (fix wave 8, finding 3). This set used to be
# compared with a bare `in` test against the field name as written, sitting
# in the same `or` as `looks_like_secret_key` (case-insensitive and
# word-decomposing) as if the two agreed on that. They did not: measured,
# nested as a nested-dict key, `traceback` redacted while `Traceback`,
# `TRACEBACK` and `Kwargs` all leaked verbatim -- six exact lowercase
# spellings do not "survive a future Celery version, a different
# task-tracing framework, or a caller who names a field `traceback`" (this
# set's own stated job) when any of those three sources is free to spell the
# name differently. The set is stored casefolded and compared against
# `shaped.casefold()` in `_shape_dict` below, matching `looks_like_secret_
# key`'s own case-insensitivity rather than silently disagreeing with it.
#
# WIDENED with the "obvious neighbours" the exact-match version missed
# entirely, not just case variants of the original six: `stacktrace`,
# `stack_trace`, `tb`, `exception`, `exc_info` carry exactly the same
# rendered-exception-text risk as `traceback`/`einfo`/`exc_text` always did --
# they were never excluded for a principled reason, only because nothing had
# measured them yet.
#
# MEASURED, not hypothetical: `TraceInfo._log_error` builds exactly
# `{'exc': safe_repr(eobj), 'traceback': safe_str(einfo.traceback),
# 'args': ..., 'kwargs': ..., ...}` and logs it via `extra={'data': context}`.
# Reproduced before this set existed: a DB password in `exc`/`traceback` and a
# task kwarg secret in `kwargs` both reached worker stdout.
_NEVER_EMIT: frozenset[str] = frozenset(
    name.casefold()
    for name in (
        "traceback", "exc", "exc_text", "einfo", "args", "kwargs",
        "stacktrace", "stack_trace", "tb", "exception", "exc_info",
    )
)

# Recursion cap for dict/list/tuple walking. Handles both unbounded depth AND
# self-referential structures (a cycle just keeps "getting deeper" until the
# cap trips) -- a cap is sufficient for fail-closed purposes, so no separate
# cycle-detection scheme is built.
_MAX_DEPTH = 6
_TRUNCATED = "<truncated>"

# A key that is not field-name-shaped (see `_is_field_name_shaped`'s
# docstring) is treated as data rather than a field name, and the whole entry
# is replaced with this placeholder -- the key is never written verbatim,
# because the key itself may be the secret (a DSN, an exception's str()).
# I2: this exact string is what the four Blocker-1 regression tests assert
# byte-for-byte, so it stays the placeholder for the FIRST redacted key in a
# dict/record; see `_redacted_key_slot` for the numbered slots after it.
_REDACTED_KEY = "[redacted-key]"

# Fix wave 4, change 2: `[\w.-]+` (I1's widening) turned out to be exactly the
# alphabet of a JWT -- base64url is `[A-Za-z0-9_-]`, segments joined by `.` --
# and `\w` is Unicode, not ASCII, so a homoglyph key (e.g. Cyrillic `а` in
# `pаssword`) satisfied it too. Two changes close both holes at once:
#
#   * ASCII-ONLY, an explicit character class rather than `\w`. A homoglyph
#     key now FAILS this gate and the whole entry redacts closed -- turning
#     the homoglyph finding from a plaintext VALUE leak into a redaction.
#   * LENGTH-CAPPED at `_MAX_FIELD_NAME_LEN`, derived from a measurement, not
#     guessed: every dict-literal string key, annotated attribute name and
#     keyword-argument name under `backend/app/` was swept -- 1941 distinct
#     names, 100% ASCII, longest 36 characters, p99 = 26. A cap of 40 admits
#     every real field name in this codebase with margin and rejects an
#     85-character JWT.
#
#     Fix wave 5, I-3: THE CAP ALSO APPLIES TO HOSTNAMES, and that measurement
#     cannot falsify it. §1.2 permits a hostname as a key deliberately
#     (`{"db-prod-01.internal.toeshee.corp": ...}`, 32 characters, survives)
#     -- but DNS permits 253 characters, so a real hostname over 40
#     (`db-prod-01.eu-west-1.internal.toeshee.corp`, 42) redacts CLOSED, not
#     admitted. That is a loss of operator signal, not a leak -- the failure
#     direction is the safe one -- but it means "hostnames are permitted"
#     is really "hostnames under 41 characters are permitted"
#     (`test_a_hostname_over_the_cap_redacts_closed`). The 1941-name sweep
#     above measured SOURCE-CODE identifiers (dict-literal keys, annotated
#     attributes, kwarg names); this gate judges RUNTIME dict keys arriving
#     through `extra=`, and this comment's own permitted hostname example
#     comes from that runtime population, not from source -- a source sweep
#     cannot falsify a cap that fails on runtime keys. THE FALSIFIER, STATED
#     AGAINST THE RIGHT POPULATION: an operator finding `[redacted-key]`
#     where they expected a hostname, device id, or other runtime key they
#     recognise. If that happens, the cap is what should change, against a
#     measurement of runtime keys -- not this source-identifier sweep.
#
# Still permits `-`, `.` and a leading digit -- restoring ordinary operator
# data (`user-agent`, `retry.max`, `db-01`, a numeric-string key) that the
# original `isidentifier()` rule annihilated -- and still rejects every
# character the four Blocker-1 reproduction cases rely on: `:`, `/`, `@` (a
# DSN), `=` (`password=hunter2`), a space (an exception's str()), and
# parens/quotes/commas (a stringified tuple). Used with `fullmatch` so a
# partial match (a field name with a trailing DSN, say) cannot sneak through.
_MAX_FIELD_NAME_LEN = 40
_FIELD_NAME = re.compile(rf"[A-Za-z0-9_.-]{{1,{_MAX_FIELD_NAME_LEN}}}")


def _is_field_name_shaped(key: Any) -> bool:
    """Is `key` shaped like a field name a developer chose, not data?

    Fix wave 4, change 1: a key that is not already a `str` was never a field
    name a developer typed -- a dict literal in `extra=` has string keys --
    so there is no reason to call `str()` on a non-str key at all, and doing
    so is the entire `str(exc)` leak class reopened in key position (an
    exception used as a dict key, `{TokenRejected(): "rejected"}`). A non-str
    key is therefore redacted OUTRIGHT here, unconditionally, rather than
    coerced and then shape-checked -- this closes the exception-as-key class
    COMPLETELY, not merely for the punctuation the four Blocker-1 reproduction
    cases happen to contain (see
    `test_an_exception_whose_str_is_field_name_shaped_is_still_redacted`,
    which uses a `__str__` that IS field-name-shaped -- a JWT -- to prove the
    property rather than pin one instance of it).

    `re.fullmatch` rather than `str_key.isidentifier()` -- a bound-method call
    the Critical fix used originally. `isidentifier` can be overridden by a
    `str` subclass to return `True` for text that is not identifier-shaped at
    all, silently reopening the exact key-position leak the Critical fix
    closed (M4). `re.fullmatch` reads the string's own character buffer
    directly and has no method for a subclass to override.

    Fix wave 5, I-1: `type(key) is str`, not `isinstance(key, str)` -- the
    shape gate closed the `re.fullmatch` half of the `str`-subclass attack,
    but `looks_like_secret_key` (`app/core/secrets.py`) is the OTHER half and
    is all method dispatch: `unicodedata.normalize("NFKC", key)` returns the
    input object itself when it is already normalised, so a subclass's own
    overridden `.translate`/`.lower` run in place of `str`'s, and a key like
    `class Sneaky(str): translate = lambda *a, **k: "harmless"` makes
    `looks_like_secret_key(Sneaky("password"))` return `False` -- the
    predicate never sees the real word. `isinstance` accepts that subclass;
    `type(key) is str` does not. A `str` subclass is not a field name a
    developer typed into a dict literal, so it is treated like any other
    non-`str` key here and redacted outright, before `looks_like_secret_key`
    (or anything else) ever gets a chance to call one of its methods.
    """
    return type(key) is str and _FIELD_NAME.fullmatch(key) is not None


_UNPRINTABLE = "<unprintable>"


def _shaped_type_name(cls: type) -> str | None:
    """The type's bare `__name__`, or `None` when the name itself is not
    field-name-shaped and must not reach output.

    Fix wave 5, M-1: `type.__name__` is set at class-definition time --
    `type("postgres://u:hunter2@h/db", (Exception,), {})` is legal Python
    and its `.__name__` IS that string -- so a dynamically-named class
    reaching either place this module renders a type name (`exc_type`, and
    `_safe_value`'s "everything else becomes its type name" catch-all) had
    no shape gate in the way, even though the module's stated rule is that
    the type name IS the safe rendering, precisely because `str(exc)` is
    not. An ordinary class name (`ValueError`, `_Weird`) passes through
    unchanged; each caller picks its own placeholder for the `None` case,
    since the two call sites render a type name in different shapes
    (`exc_type` writes the bare name, `_safe_value` wraps it in `<...>`).
    """
    name = cls.__name__
    return name if _is_field_name_shaped(name) else None


# Fix wave 5, I-2: `_safe_value` allowlists int/float/bool as JSON-native and
# `_COERCIBLE` (UUID/datetime/date/Decimal/Path) as "structurally cannot
# carry a credential" -- in VALUE position. Key position's blanket non-str
# rule (fix wave 4) ignored that agreement entirely and destroyed all of
# them: a `Counter` keyed by HTTP status (`{200: 41, 404: 12}`) or a fleet
# map keyed by device UUID is ordinary RMM telemetry, not an attack, and a
# UUID *object* key dying while the identical UUID *string* key survives is
# incoherent.
#
# Fix wave 6, I-3: NOT derived from `_COERCIBLE` anymore. It was, and two of
# that tuple's five members do not actually work in key position -- silently
# reopening the exact incoherence this allowlist exists to close, just for a
# smaller pair of types:
#   * `Path`: `type(Path("/x"))` is `PosixPath`, never `pathlib.Path` -- the
#     exact-type check below (M-6) can never fire for it. Measured:
#     `{Path("app.log"): "rotated"}` -> `[redacted-key]`.
#   * `datetime`: `str(datetime(2026,1,1,tzinfo=UTC))` is
#     `"2026-01-01 00:00:00+00:00"` -- a space and two colons -- which always
#     fails `_is_field_name_shaped`'s character-class gate. Measured:
#     `{datetime(...): 1}` -> `[redacted-key]`.
# Both still survive in VALUE position (they remain in `_COERCIBLE`);
# claiming they also survive here would have been a promise this gate cannot
# keep, which is the same incoherence I-2 rewrote the spec's key-position
# bullet to stop asserting. Listed explicitly, not derived, so a type added
# to `_COERCIBLE` for value-position convenience does not silently widen key
# position too -- the two positions use different type tests (`isinstance`
# vs exact-type `is`) and must be reviewed separately from here on.
_KEY_COERCIBLE: tuple[type, ...] = (int, float, bool, UUID, date, Decimal)


def _shaped_key(key: Any) -> str | None:
    """Resolve a dict/record key to the text it should be written as, or
    `None` when the whole entry must redact outright.

    Fix wave 5, I-2. Order, matching the value path: exact-type check ->
    guarded coercion -> shape gate -> secret-word predicate (the predicate is
    applied by the caller, after this returns).

      * `type(key) is str` -- the key is already text; used as-is.
      * exact-type membership in `_KEY_COERCIBLE` (`int`, `float`, `bool`,
        `UUID`, `date`, `Decimal`) -- the subset of value position's
        allowlist that actually renders field-name-shaped text; see that
        tuple's own comment for the two value-safe types (`datetime`,
        `Path`) that do NOT work here and why not. Coerced with the same
        guarded `str()` `_safe_value` uses for values (a pathological
        `__str__` that raises redacts the entry rather than propagating).
      * anything else -- including a SUBCLASS of an allowlisted type -- is
        not coerced at all and redacts outright. Exact-type (`is`), not
        `isinstance`: a hostile `int` subclass overriding `__str__` (e.g. to
        return a DSN) would otherwise smuggle arbitrary text through the
        coercion step, exactly the class of attack I-1 closed for `str`.

    Coercion alone does not make a key safe -- the result still has to pass
    `_is_field_name_shaped` afterward, exactly as an ordinary `str` key does
    (e.g. `str(float)` can render `+`/`e` for a value like `1e+20`, which the
    shape gate rejects; the entry redacts rather than admitting it).
    """
    if type(key) is str:
        candidate: str = key
    elif any(type(key) is t for t in _KEY_COERCIBLE):
        # M-6: `is`, not `in` (which dispatches to `==`). `in` on a tuple of
        # types calls `__eq__` on each candidate, and a class whose metaclass
        # overrides `__eq__` can satisfy `type(key) in _KEY_COERCIBLE` for a
        # type that was never listed -- measured: a metaclass doing exactly
        # that got `{"d": {"AKIAIOSFODNN7EXAMPLE": "v"}}` out of the OLD
        # `in` check. `any(type(key) is t for t in _KEY_COERCIBLE)` reads
        # each membership with `is`, matching the docstring above.
        try:
            candidate = str(key)
        except Exception:
            return None
    else:
        return None
    return candidate if _is_field_name_shaped(candidate) else None


def _redacted_key_slot(index: int) -> str:
    """The placeholder for the `index`-th (1-based) redacted key in one
    dict/record.

    I2: every redacted key used to write into the literal `_REDACTED_KEY`
    slot, so a dict with several non-field-name keys collapsed to one entry
    and the true count was silently lost. The first keeps `_REDACTED_KEY`
    exactly -- the four Blocker-1 tests assert that string byte-for-byte --
    and later ones get a `-2`, `-3`, ... suffix instead of overwriting it.
    No collision with real data is possible: `[redacted-key-2]` is itself not
    field-name-shaped (`[` and `]` are outside `[A-Za-z0-9_.-]`), so a literal
    key with that text would be redacted too, not mistaken for this
    placeholder.
    """
    if index <= 1:
        return _REDACTED_KEY
    return f"{_REDACTED_KEY[:-1]}-{index}]"


def _collision_slot(shaped: str, index: int) -> str:
    """The placeholder for the `index`-th (>= 2) SURVIVING key in one
    dict/record that shapes to the SAME text as one already written.

    M-8 (fix wave 6): a coerced key colliding with an already-written shaped
    key (`{200: "a", "200": "b"}`, both -> `"200"`) used to silently overwrite
    the entry it collided with -- the same cardinality loss `_redacted_key_
    slot` exists to prevent for REDACTED keys, reopened here for SURVIVING
    ones. The fix gave the second occurrence a numbered suffix (`"200-2"`).

    M-4 (fix wave 7): that suffix was itself field-name-shaped -- `-` and
    digits are inside `_FIELD_NAME`'s character class -- so it could collide
    with a THIRD, genuinely distinct literal key spelling the same text.
    Measured: `{200: "a", "200": "b", "200-2": "c"}` walked to `{"200": "a",
    "200-2": "c"}` -- "b"'s value silently dropped, the exact bug M-8 exists
    to prevent, one level up. `[` and `]` are outside `_FIELD_NAME`'s
    character class -- the same property `_REDACTED_KEY` relies on -- so no
    key that has ever passed the shape gate can equal this marker; a literal
    `"200[dup-2]"` key would itself fail the shape gate and redact, not
    collide with it.
    """
    return f"{shaped}[dup-{index}]"


def _shape_dict(
    items: Iterable[tuple[Any, Any]], value_of: Callable[[Any], Any]
) -> dict[str, Any]:
    """Build one JSON-safe dict from `items`, applying ONE key rule at every
    call site that walks a mapping -- the top-level `extra=` loop in
    `JsonFormatter.format` and the nested walk in `_safe_value` below.

    I1 (fix wave 7): these used to be two separate copies of "the same rule."
    `JsonFormatter.format`'s own comment claimed the two loops "can't drift"
    because they shared `_shaped_key` -- true of the SHAPE gate, false of the
    COLLISION handling M-8 added, which only ever landed in the nested copy.
    Measured: `{200: "from-int-key", "200": "from-str-key"}` as a record's
    TOP-LEVEL extras lost the first entry outright (`{"200": "from-str-key"}`,
    one field gone, no marker); the byte-identical dict one level down did
    not. Making this function the only implementation of both loops is what
    makes "the two can't drift" true rather than aspirational prose.

    Per key, in order:

      * Unshapeable (not a `str`, not one of `_KEY_COERCIBLE`'s allowlisted
        types, or fails `_is_field_name_shaped` -- see `_shaped_key`) redacts
        the WHOLE entry into a numbered `_redacted_key_slot`, never writing
        the (possibly secret) key text verbatim.
      * A shaped key that COLLIDES with one already written this call gets a
        numbered `_collision_slot` instead of overwriting the entry it
        collides with (M-8, hardened by M-4).
      * The value is replaced with `REDACTED` -- key and value both survive,
        only the value changes -- when the shaped key matches
        `looks_like_secret_key` OR is one of `_NEVER_EMIT`'s structurally
        unsafe names (C-1, fix wave 7). Otherwise `value_of` reduces it:
        `_safe_value` one depth deeper for a nested dict, or `_safe_value` at
        depth 0 for the top-level extras loop, where the value is not itself
        wrapped in anything this function walked to get here.
    """
    result: dict[str, Any] = {}
    redacted_count = 0
    seen_shaped: dict[str, int] = {}
    for key, value in items:
        shaped = _shaped_key(key)
        if shaped is None:
            redacted_count += 1
            result[_redacted_key_slot(redacted_count)] = REDACTED
            continue
        seen_shaped[shaped] = seen_shaped.get(shaped, 0) + 1
        slot = shaped if seen_shaped[shaped] == 1 else _collision_slot(shaped, seen_shaped[shaped])
        result[slot] = (
            REDACTED
            if looks_like_secret_key(shaped) or shaped.casefold() in _NEVER_EMIT
            else value_of(value)
        )
    return result


def _safe_value(value: Any, _depth: int = 0) -> Any:
    r"""Reduce a value to something JSON-native, failing CLOSED rather than open.

    `json.dumps(..., default=str)` -- the thing this replaces -- silently
    stringifies ANY object, including an exception, via its __str__. That is
    the exact leak class §2 exists to prevent (an exception's own text), reached
    through a field name (e.g. `err`) that `looks_like_secret_key` has no
    reason to flag. So the default here is the opposite of the stdlib one:

      * JSON-native scalars (str, int, float, bool, None) pass through as-is.
      * dict and list/tuple are walked recursively -- a secret nested one level
        down inside an `extra=` container must not escape by being wrapped.
        Depth is capped at `_MAX_DEPTH`; anything deeper (including a
        self-referential/cyclic structure, which never terminates on its own)
        renders as `_TRUNCATED` instead of recursing further.
      * A short allowlist of types that structurally cannot carry a credential
        (UUID, datetime, date, Decimal, Path) is coerced with str(), so logs
        stay useful for the common, safe case. The coercion itself is guarded:
        a pathological subclass whose __str__ raises fails closed to its type
        name rather than propagating.
      * EVERYTHING ELSE -- exceptions above all -- becomes its TYPE NAME, never
        its str(). `"<ValueError>"`, not `str(ValueError(...))`.

    DICT KEYS FOLLOW A SEPARATE, STRICTER RULE, NOT THE ONE ABOVE. A key is
    always rendered as text (JSON has no other kind of key), so "becomes its
    type name" is not by itself enough -- unlike a value, a key is written out
    even when it is redacted-away, and a key that happens to equal readable
    secret material (a DSN used as a map key, an exception's own str() text)
    is exactly as dangerous as a value would be. So: a key is resolved with
    `_shaped_key` -- exact-type check, guarded coercion, THEN
    `_is_field_name_shaped` (`type(key) is str and
    re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", key)`, not `str.isidentifier()` --
    see that function's docstring for why) -- BEFORE anything else is done
    with it, and the resulting entries (redacted-key slots for an unshapeable
    key, numbered collision slots for a shaped key that repeats, `_NEVER_EMIT`
    alongside `looks_like_secret_key` for the value) are all built by
    `_shape_dict`, which this function calls below rather than repeating any
    of that here -- see its docstring for the full key-resolution rule and
    fix wave 7, I1 for why there is exactly one implementation of it now.
    """
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        # M-2: `json.dumps`'s default `allow_nan=True` renders NaN/Infinity/
        # -Infinity as bare, UNQUOTED tokens. CPython's own `json.loads`
        # accepts them back (a documented leniency), but the JSON spec does
        # not, so spec §5 test 1's "one valid JSON object per record" claim
        # is false for any record carrying one -- a strict ingest pipeline
        # drops it. Render a non-finite float as its STRING form instead
        # ("nan", "inf", "-inf"); `allow_nan=False` on the final `json.dumps`
        # call is the backstop that makes any future path which
        # reintroduces a bare one fail loudly instead of shipping silently.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, _COERCIBLE):
        try:
            return str(value)
        except Exception:
            return f"<{type(value).__name__}>"
    if isinstance(value, dict | list | tuple):
        if _depth >= _MAX_DEPTH:
            return _TRUNCATED
        if isinstance(value, dict):
            # A secret nested one level down inside an `extra=` container
            # must not escape redaction by being wrapped -- so this recurses
            # through `_shape_dict`'s `value_of` callback rather than
            # stopping at one level, and depth is capped above by `_depth`.
            return _shape_dict(value.items(), lambda val: _safe_value(val, _depth + 1))
        return [_safe_value(item, _depth + 1) for item in value]
    type_name = _shaped_type_name(type(value))
    return f"<{type_name}>" if type_name is not None else _UNPRINTABLE


@contextmanager
def bind(**fields: str | None) -> Iterator[None]:
    """Set correlation fields for the duration of the block."""
    tokens = [
        (_CONTEXT_VARS[name], _CONTEXT_VARS[name].set(value))
        for name, value in fields.items()
        if name in _CONTEXT_VARS
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class JsonFormatter(logging.Formatter):
    """One JSON object per record, with context and redacted extras."""

    def format(self, record: logging.LogRecord) -> str:
        # Caller extras are written FIRST. The synthetic keys (ts, level,
        # logger, message) and the context keys (request_id, org_id, ...) are
        # not real LogRecord attributes, so nothing stops a caller's `extra=`
        # from colliding with one of those names -- e.g. extra={"level": "INFO"}
        # on a CRITICAL record, or extra={"org_id": "attacker-org"}. Writing
        # extras first and overwriting with the true values below means a
        # colliding caller field is simply lost, never trusted -- WHEN BOUND.
        # M1: a context name that is NOT currently bound (see _CONTEXT_VARS'
        # loop below, which only writes a key when var.get() is not None) has
        # nothing to overwrite the caller's `extra=` value with, so it simply
        # survives. Today that is every context field except `request_id`
        # (spec §3, "UNWIRED") -- `extra={"org_id": "attacker-org"}` is NOT
        # caught until something actually calls `bind(org_id=...)`.
        # M3: stdlib `logging` writes `extra=` into `record.__dict__` without
        # requiring str keys, so a non-str key can reach this TOP-LEVEL loop
        # just as it can reach a nested `extra=` dict -- same key rule as
        # `_safe_value`'s dict-key path.
        #
        # I1 (fix wave 7): "same key rule" USED TO MEAN "same `_shaped_key`
        # helper", and that was narrower than the comment implied -- the
        # collision handling M-8 added (a shaped key that renders the same
        # text as one already written gets a numbered slot instead of
        # overwriting it) only ever landed in the nested copy of this loop,
        # not here. Measured: `{200: "from-int-key", "200": "from-str-key"}`
        # at the TOP level lost the first entry outright; the identical dict
        # one level down did not. This loop now calls `_shape_dict`, the same
        # function the nested path calls, so "the two can't drift" is true of
        # the actual code, not merely of the one helper they used to share.
        #
        # ORDERING, MADE EXPLICIT RATHER THAN LEFT LOOKING ACCIDENTAL: `key in
        # _RESERVED` runs before the shape check, against the RAW key, not a
        # stringified one. Before fix wave 4, a non-str key was coerced with
        # `str()` further down regardless of whether it passed this check --
        # so a caller could pass `extra={Named(): "V"}` where `Named().__str__()
        # == "pathname"`: the raw `Named()` object is not `in _RESERVED` (that
        # set holds strings), so it survived this check, then coerced to the
        # literal string "pathname" and got written into the payload as if it
        # were a real, ordinarily-suppressed record attribute. Fix wave 4,
        # change 1 kills this as a side effect, not by reordering these two
        # checks: `_shaped_key` rejects an unresolvable key outright (see its
        # docstring) before `str()` is ever called on it in a way this check
        # could be fooled by -- it can no longer disguise itself as a reserved
        # attribute name by way of its `__str__()`.
        payload = _shape_dict(
            ((key, value) for key, value in vars(record).items() if key not in _RESERVED),
            _safe_value,
        )

        payload["ts"] = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
        payload["level"] = record.levelname
        payload["logger"] = record.name
        payload["message"] = record.getMessage()
        for name, var in _CONTEXT_VARS.items():
            value = var.get()
            # Omitted rather than null: a line full of nulls is noise in every
            # log search.
            if value is not None:
                payload[name] = value
        # Fix wave 4, change 3: `exc_type` is written below ONLY when
        # `record.exc_info` is set, but unlike the other synthetic keys above
        # (ts/level/logger/message) nothing previously discarded a caller-
        # supplied `extra={"exc_type": ...}` first -- on a record with NO
        # exception, the extras loop above would have already written the
        # caller's spoofed value into `payload["exc_type"]`, and it would
        # never get overwritten. Drop it unconditionally before the exc_info
        # check, so the key is present in the output only when there is a
        # real exception attached to this record.
        payload.pop("exc_type", None)
        if record.exc_info:
            # TYPE NAME ONLY, NEVER str(exc). An exception's own message is
            # where this project's two real credential leaks lived: a presigned
            # URL inside a url.Error, a hostname inside a DNSError.
            # M-1: the type name itself is guarded by `_shaped_type_name` --
            # `type.__name__` is developer/attacker-controlled at
            # class-definition time and reaches here with no shape gate
            # otherwise, which would make the "type name is the safe
            # rendering" rule false for a dynamically-named class.
            exc_type = record.exc_info[0]
            if exc_type is None:
                payload["exc_type"] = "unknown"
            else:
                type_name = _shaped_type_name(exc_type)
                payload["exc_type"] = type_name if type_name is not None else _UNPRINTABLE
        # No `default=str` fallback: that is the fail-OPEN path this fixes.
        # `_safe_value` already reduces every payload value to something
        # JSON-native, so this fallback is a last-resort safety net, not the
        # primary path -- and it emits a type name, matching the fail-closed
        # rule rather than reintroducing it.
        # M-2: allow_nan=False is a backstop, not the primary defence --
        # `_safe_value` already maps every non-finite float it walks through
        # to a string. It exists so a *future* value that reaches here
        # without going through `_safe_value` (there is no such path today)
        # raises loudly instead of silently emitting a bare NaN/Infinity
        # token that only a lenient (CPython) JSON parser accepts.
        return json.dumps(
            payload, default=lambda o: f"<{type(o).__name__}>", allow_nan=False
        )


# The `celery` TREE ROOT's name, exactly -- fix wave 9 widened this from
# `celery.app.trace` (fix wave 8, finding 3's own anchoring fix, which this
# comment used to describe). `configure_logging` below calls
# `logging.getLogger(_CELERY_LOGGER_NAME)` -- an exact lookup, not a string
# match -- so the anchoring property fix wave 8 established still holds at
# the wider scope: `celery.app.tracer_metrics` and `celery.app.trace` are
# both genuine DESCENDANTS of this node (Python's logger hierarchy is
# dot-separated) and both now reach this handler by ordinary propagation,
# while a logger that merely shares the letters -- `celerybeat_custom`,
# which contains no `.` after "celery" at all -- is not a descendant and is
# never touched, regardless of how much of the name string it shares. See
# `test_a_non_celery_logger_that_merely_shares_the_prefix_is_untouched`
# (`tests/test_logging_kernel.py`), the tree-level twin of the sibling-node
# proof fix wave 8 pinned for `celery.app.trace` vs `celery.app.tracer_metrics`.
_CELERY_LOGGER_NAME = "celery"

# `TraceInfo._log_error`'s `context` also carries `exc`, `traceback`, `args`,
# `kwargs` and `internal` -- exception text and the task's own raw call
# arguments, exactly what this handler exists to never render. These four are
# the ones every `celery.app.trace` call site populates from Celery-controlled
# strings that cannot carry a credential: an operator-assigned task NAME, a
# job UUID, a worker HOSTNAME, and -- restoring it is finding 5 -- Celery's
# own closed-vocabulary OUTCOME WORD (`log_policy_t.description`:
# 'ignored' / 'rejected' / 'raised expected' / 'raised unexpected').
# CORRECTED, fix wave 10 (F-5): this used to claim "`celery.worker.consumer`
# and `celery.worker.strategy` ... never populate `data` at all -- their
# calls carry no `extra=`". False for `celery.worker.strategy`, and it
# contradicted `_synthesize_celery_record`'s own docstring twenty lines
# below in the very commit that added both. Measured:
# `celery/worker/strategy.py:161` is `info(LOG_RECEIVED, context,
# extra={'data': context})`, where `context` carries `id`/`name` (plus
# `args`/`kwargs`/`eta`, none of them in this allowlist) -- that call DOES
# populate `data`, and the handler DOES render `id`/`name` from it, exactly
# like `celery.app.trace`. What is true: `celery.worker.strategy`'s OTHER
# call site (the ETA-conversion `error(...)`, no `extra=` at all) and every
# call in `celery.worker.consumer.consumer` (`on_unknown_task`,
# `on_invalid_task`, `on_decode_error` -- see F-3, `_synthesize_celery_
# record` below) carry no `extra=`, so records from THOSE render with all
# four fields at `_CELERY_FIELD_UNKNOWN`. The trade is real, it is just
# per-CALL-SITE, not per-logger: a Celery call that does not share `celery.
# app.trace`'s `extra={'data': ...}` convention gets the safe,
# unknown-heavy rendering rather than a tailored one -- FAIL CLOSED, not a
# broken feature.
_CELERY_SAFE_FIELDS: tuple[str, ...] = ("name", "id", "hostname", "description")

# Placeholder for a safe field this record's `data` did not carry -- LOG_RETRY
# and LOG_SUCCESS never populate `hostname`, LOG_RECEIVED never populates
# `description`, and a record with no usable `data` at all is missing every
# one of them. FAIL CLOSED per the brief: still emit a well-formed line with
# whatever IS present, rather than omitting the field silently (which would
# read as "this record had a hostname of nothing" instead of "this field was
# never available") or falling back to Celery's own text.
_CELERY_FIELD_UNKNOWN = "?"

# F-3 (fix wave 10): a second, optional source of TASK IDENTITY --
# `id`/`task` -- for records that carry no `data` extra at all. Celery's own
# `message.headers` mapping (protocol v2: `id`, `task`, `root_id`, ...) is
# structurally routing metadata, never credential-bearing -- the raw
# serialized message BODY is where a secret lives (`dump_body`, see the
# module docstring's "FIX WAVE 10" section), and this key is never read for
# it. See `_synthesize_celery_record` below for how it is used.
_CELERY_HEADERS_EXTRA_KEY = "headers"


def _synthesize_celery_record(record: logging.LogRecord) -> logging.LogRecord:
    """Build a FRESH `LogRecord` carrying only known-safe fields --
    `_CeleryHandler.emit` hands this, never the original, to the JSON sink.
    Renamed from `_synthesize_celery_trace_record` (fix wave 9): the function
    was never actually specific to `celery.app.trace`'s calling convention in
    a way that mattered -- it already failed closed to
    `_CELERY_FIELD_UNKNOWN` for any record missing `data` -- only the NAME
    implied a narrower scope than the code had, once this handler started
    receiving records from every Celery-internal logger, not just that one.

    NEVER READS `record.msg`, `record.args`, OR CALLS `record.getMessage()`.
    That is the entire point: every leak in this class -- fix wave 7's DB
    password inside `%(exc)s`, fix wave 8's `%(exc)s` inside `LOG_RETRY`, fix
    wave 10's `dump_body` task-body leak inside `celery.worker.consumer`'s
    poison-message templates (see the module docstring's "FIX WAVE 10"
    section -- fix wave 9 originally, and wrongly, attributed this same
    widening to a redis-py connection error that does not reproduce) --
    lived in exactly those places, and detecting-and-rewriting them (fix
    wave 7's approach) only works for record shapes someone thought to
    detect. Not reading them at all works for every shape, including ones no
    Celery logger has written yet.

    The only things read from the original record: `record.__dict__.get
    ("data")` (Celery's own calling convention puts the same dict passed as
    `%`-args here too, as `extra={'data': context}`, on the loggers that use
    it -- `celery/app/trace.py` and, via its `info()` helper, `celery/
    worker/strategy.py`'s `LOG_RECEIVED` call; others -- `strategy.py`'s
    ETA-conversion error and every call in `celery/worker/consumer/
    consumer.py` -- pass no `extra=` at all and fall through to the
    fail-closed branch below), `record.__dict__.get("headers")` (F-3, fix
    wave 10: a `message.headers`-shaped mapping, read ONLY for the
    structurally-safe `id`/`task` keys, never populated by any
    Celery-internal call site today -- see that constant's comment), and
    `record.exc_info` (kept UNCHANGED and passed through -- `JsonFormatter`
    reads only `exc_info[0].__name__` from it, never the traceback or the
    exception's own text, which is exactly what §2 permits).

    FAIL CLOSED: if `data` (and, for `id`/`name`, `headers`) is absent, not a
    dict, or missing some of the safe fields, the record is still emitted --
    with `_CELERY_FIELD_UNKNOWN` standing in for whatever is missing --
    rather than silently dropped or, worse, filled in from Celery's own
    untrusted text. This is what closes the exact gap fix wave 7's filter
    left open: that filter's guard (`"traceback" not in data: return True`)
    let `LOG_RETRY`/`LOG_SUCCESS`/`LOG_IGNORED` through completely unmodified
    because none of them carry a `traceback` key. This function does not
    have a guard to bypass -- it runs for every record `_CeleryHandler` is
    given, unconditionally, regardless of which logger in the tree it came
    from.
    """
    data = record.__dict__.get("data")
    safe: dict[str, str] = {}
    if isinstance(data, dict):
        for field in _CELERY_SAFE_FIELDS:
            value = data.get(field)
            # F-7 (fix wave 10): `type(value) is str`, not `isinstance`. A
            # `str` subclass passes `isinstance(value, str)` unchanged and
            # still dispatches ITS OWN `__format__`/`__str__` when the
            # f-string below renders it -- measured, a hostile `str`
            # subclass in `data["name"]` produced `"...logged: OWNED[OWNED]
            # on ?"` in the output message while `data` itself held the real
            # value untouched. Same subclass-dispatch class fix wave 5
            # closed in key position with `type(key) is str`
            # (`_is_field_name_shaped`'s docstring); this closes it here, in
            # VALUE position, on the one path in this module that still
            # builds a message by direct string interpolation instead of
            # going through `_safe_value`.
            if type(value) is str:
                safe[field] = value

    # F-3 (fix wave 10): the poison-message path is unattributable, and this
    # is its partial remedy. `on_unknown_task`, `on_invalid_task` and
    # `on_decode_error` (celery/worker/consumer/consumer.py) carry no `data`
    # extra at all -- see the comment on `_CELERY_SAFE_FIELDS` above -- so
    # every field above stays at `_CELERY_FIELD_UNKNOWN` at exactly the
    # moment Celery is PERMANENTLY rejecting and discarding a task: the
    # operator gets no task name, no id, nothing to correlate with the job
    # that vanished. A `message.headers`-shaped extra is picked up the same
    # way `data` is, filling in only what `data` did not already supply --
    # task IDENTITY only (`id`, and Celery's own `task` key, mapped to this
    # module's `name` slot), never the body. No Celery-internal call site
    # attaches one under this key today (spec §6/§10 name the residual as
    # the accepted loss), so this does not change what the three sites
    # above render -- it is the mechanism ready for the day a call site
    # does attach one.
    headers = record.__dict__.get(_CELERY_HEADERS_EXTRA_KEY)
    if isinstance(headers, dict):
        if "id" not in safe:
            value = headers.get("id")
            if type(value) is str:
                safe["id"] = value
        if "name" not in safe:
            value = headers.get("task")
            if type(value) is str:
                safe["name"] = value

    outcome = safe.get("description", "logged")
    name = safe.get("name", _CELERY_FIELD_UNKNOWN)
    task_id = safe.get("id", _CELERY_FIELD_UNKNOWN)
    hostname = safe.get("hostname", _CELERY_FIELD_UNKNOWN)
    # A STATIC template, matching §2's rule for every log call this platform
    # writes itself -- the only things substituted into it are `record.name`
    # (the logger's own name, assigned by Celery/this codebase, never caller
    # data) and the four `data` values above, already filtered to
    # Celery-controlled, structurally credential-free strings (or the fixed
    # `_CELERY_FIELD_UNKNOWN` marker). `record.name` replaces the fix wave
    # 8 version's hardcoded `"celery.app.trace"` literal, fix wave 9: this
    # handler now renders records from every logger in the tree, and the
    # prefix should say which one, not claim they all came from one.
    message = f"{record.name} {outcome}: {name}[{task_id}] on {hostname}"

    synthetic = logging.LogRecord(
        name=record.name,
        level=record.levelno,
        pathname=record.pathname,
        lineno=record.lineno,
        msg=message,
        args=(),
        exc_info=record.exc_info,
    )
    synthetic.data = safe
    return synthetic


class _CeleryHandler(logging.Handler):
    """Renders EVERY record from the `celery` logger tree from known-safe
    fields only. Renamed from `_CeleryTraceHandler` (fix wave 9): it is
    attached to `logging.getLogger("celery")`, the tree ROOT, not to
    `celery.app.trace` alone -- see the module docstring's "FIX WAVE 9"
    section for the leak this widening closes and why the scope, not the
    shape, was still a guess.

    THE FAILURE THIS REPLACES, NAMED PLAINLY. `_CeleryTraceSanitiser` (fix
    wave 7) was a DENYLIST OVER RECORD SHAPES: it recognised `_log_error`'s
    one specific call (a `data` extra carrying a `traceback` key) and
    rewrote only that record. Every OTHER `celery.app.trace` record --
    `LOG_RETRY`, `LOG_SUCCESS`, `LOG_IGNORED`, `LOG_RECEIVED` (via `celery/
    worker/strategy.py`) -- has no `traceback` key, so the filter's guard let
    it through UNCHANGED, `msg` and `args` intact. Measured: `LOG_RETRY`'s
    template is `"Task %(name)s[%(id)s] retry: %(exc)s"`, and
    `Retry.__str__` embeds `safe_repr(original_exc)` -- a DB password reached
    worker stdout through exactly the `%`-arg door this module's own
    docstring already names as outside the redaction gate, on a record the
    shape-matching filter was never built to recognise. Deciding which
    records are dangerous is guesswork about a third party's internals, and
    guessing wrong fails OPEN -- three of this branch's seven Criticals came
    from exactly that style of thinking, and fix wave 9's finding (the same
    guesswork one level up: which LOGGERS, not which record shapes) is a
    fourth.

    THIS HANDLER STOPS GUESSING, AT EVERY LEVEL. It does not inspect a
    record for a shape that looks dangerous -- there is no `if` that decides
    whether to act. Every record delivered here (this handler sits on
    `celery`, the logger every Celery-internal logger propagates to and
    stops at -- see `configure_logging` below -- so nothing else in that
    tree ever reaches a different handler) is rendered the identical way, by
    `_synthesize_celery_record`: from a fixed, small set of fields Celery's
    own call sites populate and that cannot carry rendered exception text or
    a task's raw call arguments. See that function's docstring for the full
    account of what is read (`data`, `exc_info`) and, more importantly, what
    never is (`msg`, `args`, `getMessage()`).

    WHY THIS BELONGS ON A DEDICATED HANDLER, NOT A FILTER ON THE SHARED ONE.
    A `logging.Filter` can only decide whether a record proceeds, or mutate
    it in place before it does -- both of those are still "look at this
    record and decide", the exact shape of reasoning that failed here twice.
    Taking the `celery` tree off propagation at its root (`configure_logging`
    below) and giving it its OWN handler means none of those loggers' records
    ever reach the shared root handler's `JsonFormatter` carrying Celery's
    own message at all -- there is no shared code path left for a future
    record shape, on a future Celery logger this wave never measured, to
    slip through unmodified the way `LOG_RETRY` did.
    """

    def __init__(self) -> None:
        super().__init__()
        # A private sink, not the process's shared root handler: this
        # handler's whole job is that records anywhere in the celery tree
        # never reach that shared handler carrying Celery's own msg/args, so
        # it needs its own StreamHandler+JsonFormatter pair to actually write
        # the safe, synthesized record out. Reusing JsonFormatter (rather
        # than a second formatter implementation) is safe here specifically
        # because by the time a record reaches it, `_synthesize_celery_
        # record` has already replaced msg/args with the static,
        # credential-free template -- JsonFormatter's own
        # `record.getMessage()` call renders THAT, never anything Celery
        # wrote.
        self._sink = logging.StreamHandler(sys.stdout)
        self._sink.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.emit(_synthesize_celery_record(record))
        except Exception:
            # Matches every other fail-closed path in this module: a defect
            # in synthesis must not crash the worker process, and stdlib
            # logging's own convention for "a handler failed" is
            # `Handler.handleError`, not letting the exception propagate.
            #
            # F-1 (fix wave 10): `handleError` MUST NEVER be given the
            # ORIGINAL `record` -- this used to pass it directly.
            # `logging.Handler.handleError`'s own CPython body ends with
            # `sys.stderr.write('Message: %r\nArguments: %s\n' %
            # (record.msg, record.args))` whenever `logging.raiseExceptions`
            # is true (the default, unchanged by this module), and it runs
            # unconditionally on whatever record it is handed -- there is no
            # opt-out short of subclassing `Handler` itself. `record.msg`
            # and `record.args` are exactly the two attributes this whole
            # handler exists to never render; handing the original record to
            # `handleError` was a second, unguarded path to the same output
            # this class's entire design is built to close off.
            #
            # Measured: a `data` value whose `.get` raises (a hostile `dict`
            # subclass, or any future Celery internal shape this handler was
            # never updated for) makes `_synthesize_celery_record` itself
            # raise -- landing here with the ORIGINAL record still in hand,
            # `args` intact, secret text and all -- and the old code wrote
            # the full `args` tuple, exception object included, straight to
            # stderr. The log driver captures stderr exactly like stdout, so
            # this was a fail-OPEN path on precisely the two fields the
            # handler's whole claim is that it never reads.
            #
            # The fix: build a placeholder record that never touches
            # `record.msg`/`record.args` at all -- not the synthetic record
            # from a (by definition, in this branch) failed synthesis
            # attempt, and not the original -- so no path through this
            # method, on any failure shape, can surface either field.
            placeholder = logging.LogRecord(
                name=record.name,
                level=record.levelno,
                pathname=record.pathname,
                lineno=record.lineno,
                msg="_CeleryHandler failed to synthesize a safe record",
                args=(),
                exc_info=None,
            )
            self.handleError(placeholder)


#: `/i/<code>` at the start of a request path: the install link's code
#: (install-link spec 2026-09-28 §1.2). Everything after the code is kept, so
#: `/i/[redacted]/install.sh` still says which route was asked for.
_INSTALL_LINK_PATH = re.compile(r"^/i/[^/?]+")


class InstallLinkPathRedactor(logging.Filter):
    """Rewrites the install link's code out of uvicorn's access line.

    THE ONE PLACE THIS MODULE REDACTS A REQUEST LINE, and why it has to. The
    rule above (`configure_logging`'s docstring) is that a secret is kept OUT
    of the request line rather than redacted out of the log -- which is how
    the WebSocket token was fixed. The install link cannot follow it: the code
    IS the URL a technician opens on a machine, by the operator's design. So
    uvicorn's access record, whose `args` are `(client, method, full_path,
    http_version, status)`, has its path argument rewritten before it is
    rendered. Installed on `uvicorn.access` (a logger filter, so it runs on
    every record created there, whatever handler later formats it).
    `tests/test_install_link_page.py` measures it against a real uvicorn,
    with a control that shows the code IS logged under any other path.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            redacted = _INSTALL_LINK_PATH.sub("/i/[redacted]", args[2])
            if redacted != args[2]:
                record.args = (*args[:2], redacted, *args[3:])
        return True


def configure_logging(level: int | str = "INFO") -> None:
    """Install the JSON formatter on the ROOT logger, once, and give the
    `celery` logger tree its own suppression-based defence.

    ROOT, deliberately: uvicorn's `uvicorn.access` and `uvicorn.error` loggers
    propagate here, so their lines get the same JSON FORMATTING as everything
    else this process logs.

    THAT IS NOT THE SAME AS "THE SAME REDACTION", AND THE DIFFERENCE IS NOT
    ACADEMIC. `JsonFormatter` only redacts `extra=` fields by name (§1.2 /
    module docstring above) -- and uvicorn's own log records carry no `extra=`
    at all. Their entire content is `%`-args rendered into the message string
    by uvicorn itself, which this same module documents as OUTSIDE the
    redaction gate. Routing them to the root handler changes their FORMAT
    (plain text -> one JSON object per line) and changes NOTHING about their
    REDACTION, because there is no field name for `looks_like_secret_key` to
    match.

    The consequence was a live gap, and it was CLOSED IN THE TRANSPORT, which
    is the only place it could be closed. `app/api/v1/ws.py` used to read the
    auth token from the WebSocket query string (browser clients cannot set
    headers), so uvicorn's own handshake line -- which writes the full request
    line, query string included -- put a live bearer token on stdout, JSON
    formatted and entirely unredacted. No change here could have fixed it:
    there is no `extra=` field to match. `ws.py` now takes the token from the
    WebSocket subprotocol header instead and refuses a query-string one, so
    the credential never enters a URL. `tests/test_ui_ws.py` measures both
    halves against a real uvicorn -- token absent from the log after a real
    handshake, and still present when a URL-borne one is used as the control.

    The general point survives the specific fix: anything uvicorn renders into
    its own message string is outside this gate, so a secret must be kept out
    of the request line rather than redacted out of the log.

    FIX WAVE 9 (widening fix wave 8's shape to the whole tree): the `celery`
    logger -- not `celery.app.trace` alone -- is taken OFF PROPAGATION and
    given `_CeleryHandler` as its own, dedicated handler -- see that class's
    docstring and the module docstring's "FIX WAVE 9" section for the leak
    this widening closes (`celery.worker.consumer`'s broker password,
    `celery.worker.strategy`'s DSN) and why the previous, narrower scope was
    still guesswork. `celery.app.trace` gets NO separate wiring of its own
    any more: it is a descendant of `celery` in Python's logger hierarchy, so
    it reaches this same handler by ordinary propagation, and a second
    attachment point would only be two places for this to drift apart.
    THE INSTALLATION ITSELF IS WHAT FIX WAVE 7 SHIPPED WITH NO TEST FOR:
    deleting either line below (`celery_logger.handlers[:] = [...]` or
    `celery_logger.propagate = False`) must fail
    `test_configure_logging_takes_celery_off_propagation_at_the_tree_root`
    (`tests/test_logging_kernel.py`) -- fix wave 7's equivalent line
    (`handler.addFilter(_CeleryTraceSanitiser())`) had no such test, and
    deleting it left 67 tests green while the leak came back in full.

    `level: int | str`, not only `str`: Celery's `setup_logging` signal
    passes an already-resolved numeric level (`celery.utils.log.mlevel`), not
    the string this function's own default implies -- `logging.Logger.
    setLevel` accepts either, so this only widens the type hint to match what
    `create_celery`'s receiver now forwards (I-4, `app/worker.py`).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, InstallLinkPathRedactor) for f in access.filters):
        access.addFilter(InstallLinkPathRedactor())

    celery_logger = logging.getLogger(_CELERY_LOGGER_NAME)
    celery_logger.handlers[:] = [_CeleryHandler()]
    celery_logger.propagate = False
