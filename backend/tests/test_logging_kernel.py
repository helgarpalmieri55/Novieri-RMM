"""The logging kernel: structure, redaction, correlation.

Spec: docs/superpowers/specs/2026-08-14-logging-kernel-design.md §5.
No database -- every test here drives the formatter and contextvars directly.
"""

import ast
import asyncio
import io
import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, redirect_stderr
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.logging import (
    REDACTED,
    JsonFormatter,
    bind,
    org_id_var,
    request_id_var,
)
from app.modules.sla.errors import UnsatisfiableCalendar


def _record(
    msg: str = "hello",
    args: tuple[object, ...] | Mapping[str, object] = (),
    /,
    # POSITIONAL-ONLY, deliberately: `**extra` carries caller-chosen field
    # names, and several tests pass one literally called `args` (or splat a
    # dict that could contain it). Without the `/` those would bind to this
    # parameter instead of landing in `extra`, and the test would quietly
    # assert nothing about the field it meant to.
    **extra: object,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_the_formatter_emits_one_valid_json_object() -> None:
    """§5 test 1."""
    line = JsonFormatter().format(_record())
    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["ts"], "every line needs a timestamp"


def test_no_bare_nan_or_infinity_reaches_the_output() -> None:
    """M-2: §5 test 1 claims "one valid JSON object per record" -- but
    `json.dumps` defaults to `allow_nan=True`, so a `NaN`/`Infinity` value
    renders as the bare (unquoted) tokens `NaN`/`Infinity`/`-Infinity`, which
    CPython's own `json.loads` accepts (a documented leniency) but the JSON
    spec and a strict ingest pipeline do not -- so the claim is false for any
    record carrying a non-finite float. Non-finite floats are mapped to their
    STRING form instead, and `allow_nan=False` is passed as a backstop so any
    future path that reintroduces one fails loudly instead of silently."""
    import math

    payload = json.loads(
        JsonFormatter().format(
            _record(nan=math.nan, pos_inf=math.inf, neg_inf=-math.inf, ok=1.5)
        )
    )
    assert payload["nan"] == "nan"
    assert payload["pos_inf"] == "inf"
    assert payload["neg_inf"] == "-inf"
    assert payload["ok"] == 1.5

    line = JsonFormatter().format(_record(nan=math.nan))
    assert "NaN" not in line, "a bare, unquoted NaN token reached the output"


def test_a_secret_named_field_is_redacted() -> None:
    """§5 test 2. THE INVARIANT: credentials are never written to logs."""
    for name in ("password", "api_key", "token", "secret", "apiKey", "tokens"):
        payload = json.loads(JsonFormatter().format(_record(**{name: "hunter2"})))
        assert payload[name] == REDACTED, f"{name} was not redacted"
        assert "hunter2" not in json.dumps(payload), f"{name}'s VALUE survived"


def test_an_ordinary_field_survives_intact() -> None:
    """§5 test 3. A redactor that flags everything passes test 2 while
    destroying every log line -- this is the test that catches it."""
    payload = json.loads(JsonFormatter().format(_record(hostname="db-01", count=3)))
    assert payload["hostname"] == "db-01"
    assert payload["count"] == 3


def test_a_percent_arg_is_NOT_redacted() -> None:
    """§5 test 11 -- THIS ASSERTS A GAP, DELIBERATELY.

    `%`-args render into the message string, where a NAME-based redactor has no
    name to match. Spec §1.2 states this and sets the rule that secrets go in
    `extra=`. Pinned executably because prose limitations drift.

    IF THIS TEST EVER FAILS, redaction has been extended to cover `%`-args --
    which is good news, and means spec §1.2 must be rewritten rather than this
    test deleted.
    """
    line = JsonFormatter().format(_record("connecting to %s", ("postgres://u:pw@h/db",)))
    assert "postgres://u:pw@h/db" in line, (
        "a %-arg is now redacted -- rewrite spec §1.2 and this test's docstring"
    )


def test_context_fields_appear_on_every_line() -> None:
    """§5 test 7, formatter half."""
    token = request_id_var.set("req-abc")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "req-abc"


def test_absent_context_fields_are_omitted_not_null() -> None:
    """A line full of nulls is noise in every log search."""
    payload = json.loads(JsonFormatter().format(_record()))
    assert "job_id" not in payload
    assert "org_id" not in payload


def test_bind_sets_and_restores() -> None:
    with bind(job_id="job-1"):
        assert json.loads(JsonFormatter().format(_record()))["job_id"] == "job-1"
    assert "job_id" not in json.loads(JsonFormatter().format(_record()))


def test_contextvars_do_not_bleed_between_concurrent_tasks() -> None:
    """§5 test 9. The failure a thread-local would have, and the reason the
    spec insists on contextvars: two requests sharing a worker would see each
    other's ids. Interleaved with sleeps so the tasks genuinely overlap."""
    seen: dict[str, str] = {}

    async def one(name: str, value: str) -> None:
        token = request_id_var.set(value)
        try:
            await asyncio.sleep(0.01)
            seen[name] = json.loads(JsonFormatter().format(_record()))["request_id"]
        finally:
            request_id_var.reset(token)

    async def both() -> None:
        await asyncio.gather(one("a", "req-a"), one("b", "req-b"))

    asyncio.run(both())
    assert seen == {"a": "req-a", "b": "req-b"}


# --- Blocker 3: §5 test 8, landed as a real test -------------------------------


def test_configure_logging_installs_one_root_handler_and_uvicorn_propagates() -> None:
    """§5 test 8. Previously existed only as a one-off shell command; this is
    the property §4's whole stdlib-vs-structlog choice rests on.

    I4: the old version of this test never applied uvicorn's OWN dictConfig
    first, so it could not fail for the order-dependent reason spec §7 names
    -- uvicorn installs its handlers when it starts, and if
    `configure_logging()` ran *before* that, uvicorn's config would win and
    the JSON formatter would be silently defeated. Applying uvicorn's real
    `LOGGING_CONFIG` here (never hand-copied -- it would drift from whatever
    version is installed) then calling `configure_logging()` reproduces the
    actual startup order and lets this test fail the way §7 predicts.

    THIS ASSERTS ROUTING, NOT REDACTION (see blocker 2 / the module docstring
    of `configure_logging`). uvicorn's records reaching the same formatter
    only proves they get the same JSON *shape* -- their content is `%`-args
    the formatter never sees as `extra=` fields, so nothing here claims they
    are redacted.
    """
    uvicorn = pytest.importorskip("uvicorn")
    import logging.config

    from app.core.logging import configure_logging

    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    configure_logging()
    try:
        root = logging.getLogger()
        assert len(root.handlers) == 1, "configure_logging must install exactly one handler"
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            framework_logger = logging.getLogger(name)
            assert framework_logger.handlers == [], (
                f"{name} must have no handler of its own -- it must propagate to root"
            )
            assert framework_logger.propagate is True
    finally:
        # Leave global logging state as this test found it, so later tests
        # (and other test modules) aren't affected by this process-wide
        # mutation -- including the uvicorn loggers this test's own setup
        # configures, which the pre-fix version of this test never touched.
        root = logging.getLogger()
        root.handlers[:] = []
        root.setLevel(logging.WARNING)
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            framework_logger = logging.getLogger(name)
            framework_logger.handlers[:] = []
            framework_logger.propagate = True
            framework_logger.setLevel(logging.NOTSET)


# --- Finding 1: redaction must fail CLOSED for object values -----------------


class _LeakyErr(Exception):
    """An exception whose __str__ leaks a connection string -- exactly the
    shape of this project's two real credential leaks."""

    def __str__(self) -> str:
        return "postgres://user:SECRETPW@db/prod"


def test_an_exception_passed_as_extra_is_not_stringified() -> None:
    """A field name that is NOT secret-shaped (e.g. `err`) must still not leak
    an exception's own text. `json.dumps(default=str)` fails this: it silently
    stringifies the object with no redaction at all."""
    line = JsonFormatter().format(_record(err=_LeakyErr()))
    assert "SECRETPW" not in line, "the exception's str() leaked into the log line"
    payload = json.loads(line)
    assert payload["err"] == "<_LeakyErr>"


# --- C-1: `exc_info=exc` is only safe in a process that installs this ---------
# --- formatter -- pinned through a real handler, not the formatter alone -----


def test_exc_info_through_the_configured_kernel_never_leaks_the_exceptions_text() -> None:
    """C-1: fix wave 5 rewrote four exception-log sites to `exc_info=exc` on
    the premise -- written into a production comment at `app/worker.py` and a
    test docstring at `tests/test_worker_sweep_containment.py` -- that
    `JsonFormatter` only ever emits `exc_type` for an exception, never its
    message or a traceback. That is true only of a process that actually
    installs `JsonFormatter` on the logger doing the logging.

    Reproduced here through a real `logging.StreamHandler`, not by calling
    `JsonFormatter().format()` directly: `app/gateway/main.py` used to install
    `logging.basicConfig` (the stdlib default `Formatter`) and Celery installs
    its own equivalent on worker start, and BOTH call `formatException`,
    which prints the exception's own `str()` in the traceback tail. Measured
    under a handler carrying the stdlib default `Formatter` instead of
    `JsonFormatter` below (swap the two to see it): `WARNING:...\\nLeakyErr:
    postgres://user:SECRETPW@db/prod`, `SECRETPW` present. Through a handler
    carrying `JsonFormatter` it must not be -- this is the property the four
    rewritten sites actually rely on, and only holds where `configure_logging`
    is reachable (see `test_configure_logging_is_wired_into_every_process_
    entrypoint` for the wiring half)."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.c1.exc-info-through-a-real-handler")
    logger.propagate = False
    logger.handlers[:] = [handler]
    logger.setLevel(logging.WARNING)
    try:
        logger.warning("db unavailable", exc_info=_LeakyErr())
    finally:
        logger.handlers[:] = []

    output = stream.getvalue()
    assert "SECRETPW" not in output, "the exception's own text reached output through exc_info"
    payload = json.loads(output)
    assert payload["exc_type"] == "_LeakyErr"


def test_configure_logging_is_wired_into_every_process_entrypoint() -> None:
    """C-1: a WIRING check, not proof the running processes are configured --
    it can see that the source CALLS `configure_logging`, not that
    `run_gateway()` or `create_celery()` ever actually execute in production.

    It exists because the kernel silently covered exactly one of the three
    processes that log (`app/main.py`) for an entire phase: `app/gateway/
    main.py` called `logging.basicConfig` and Celery installs its own
    formatter on worker start, and nothing caught either one -- fix wave 5's
    `exc_info=exc` rewrite shipped and was reviewed on the unstated assumption
    that all three processes shared one formatter.

    Reads `app/main.py`, `app/gateway/main.py` and `app/worker.py` from disk
    with `ast`, in the style of `tests/test_logging_discipline.py`, rather
    than importing and running any of them -- doing that would start a
    gateway server or a Celery worker."""

    def calls_name(tree: ast.AST, name: str) -> bool:
        return any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == name)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            )
            for node in ast.walk(tree)
        )

    backend = Path(__file__).resolve().parent.parent
    for relative in ("app/main.py", "app/gateway/main.py", "app/worker.py"):
        tree = ast.parse((backend / relative).read_text(), filename=relative)
        assert calls_name(tree, "configure_logging"), f"{relative} never calls configure_logging()"

    gateway_tree = ast.parse((backend / "app/gateway/main.py").read_text())
    assert not calls_name(gateway_tree, "basicConfig"), (
        "app/gateway/main.py must no longer call logging.basicConfig -- it installs "
        "the stdlib default Formatter, which prints exc_info's traceback tail "
        "(str(exc)) instead of JsonFormatter's exc_type-only rendering"
    )


def test_an_arbitrary_object_in_extra_renders_as_a_type_name() -> None:
    """Any non-JSON-native, non-allowlisted object must become its type name,
    never its str()."""

    class _Weird:
        def __str__(self) -> str:
            return "do-not-log-me"

    payload = json.loads(JsonFormatter().format(_record(thing=_Weird())))
    assert payload["thing"] == "<_Weird>"
    assert "do-not-log-me" not in json.dumps(payload)


def test_uuid_and_datetime_still_render_usefully() -> None:
    """The fix must not make ordinary, safe values useless in logs."""
    the_id = uuid4()
    the_time = datetime(2026, 1, 1, tzinfo=UTC)
    payload = json.loads(JsonFormatter().format(_record(request_uuid=the_id, seen_at=the_time)))
    assert payload["request_uuid"] == str(the_id)
    assert payload["seen_at"] == str(the_time)


def test_a_secret_key_nested_in_a_dict_extra_is_redacted() -> None:
    """A secret one level down inside an `extra=` container must not escape
    redaction by being wrapped in a dict."""
    config = {"api_key": "hunter2", "host": "db-01"}
    payload = json.loads(JsonFormatter().format(_record(config=config)))
    assert payload["config"]["api_key"] == REDACTED
    assert payload["config"]["host"] == "db-01"
    assert "hunter2" not in json.dumps(payload)


# --- Finding 2: caller extras must not spoof reserved/context keys -----------


def test_extra_cannot_spoof_the_level_field() -> None:
    """A CRITICAL record must render as CRITICAL even if the caller's `extra=`
    tries to overwrite `level`."""
    record = _record(level="INFO")
    record.levelname = "CRITICAL"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "CRITICAL"


def test_extra_cannot_spoof_a_bound_context_field() -> None:
    """WHEN BOUND: a genuine `org_id` set via `bind()` must win over a
    caller-supplied `extra={"org_id": ...}` -- otherwise a log line can
    misattribute an action to the wrong org.

    M1: this guarantee only holds for a context field that is actually bound.
    Of the five context fields, only `request_id` has a live production call
    site (the request middleware); `org_id`, `user_id`, `job_id` and
    `device_id` are wired in the sense that `bind()` and this formatter both
    support them, but nothing in this codebase calls `bind(org_id=...)` yet
    (spec §3, "UNWIRED"). An UNBOUND context name passed through `extra=` is
    simply omitted from the overwrite step below and survives untouched --
    `extra={"org_id": "attacker-org"}` is written and never caught until the
    day something actually binds `org_id`."""
    token = org_id_var.set("real-org-123")
    try:
        payload = json.loads(JsonFormatter().format(_record(org_id="attacker-org-999")))
    finally:
        org_id_var.reset(token)
    assert payload["org_id"] == "real-org-123"


# --- _safe_value robustness (fix wave 2) --------------------------------------


def test_a_non_string_dict_key_is_coerced_and_does_not_raise() -> None:
    """FINDING A: a tuple (or other non-str, non-allowlisted) dict key must
    not crash json.dumps.

    UPDATED for blocker 1 (dict-KEY fail-open), then fix wave 4, then fix
    wave 5 (I-2), then fix wave 6 (I-3): a tuple is not one of
    `_KEY_COERCIBLE`'s allowlisted types -- int/float/bool/UUID/date/Decimal,
    which DO survive as coerced key text (see the tests below) -- so it is
    never passed to `str()` at all. It is treated as DATA rather than a field
    name and the whole entry is redacted rather than written verbatim, the
    same rule that stops a DSN or an exception's str() from leaking through
    the key position below. `format()` still must not raise.

    `datetime` and `Path` are NOT in that list, and the earlier version of
    this docstring wrongly said they were. Measured: `type(Path("/x"))` is
    `PosixPath`, so exact-type membership never fires for it, and
    `str(datetime(...))` contains a space and colons, so it always fails the
    shape gate. Both therefore redact in key position while surviving in
    VALUE position -- pinned by `test_a_datetime_dict_key_still_redacts_closed`
    and `test_a_path_dict_key_still_redacts_closed` so it is measured rather
    than asserted in prose, which is how it came to be wrong here."""
    line = JsonFormatter().format(_record(config={("t", "k"): "v"}))
    payload = json.loads(line)
    assert payload["config"] == {"[redacted-key]": REDACTED}


def test_a_non_str_key_redacts_regardless_of_what_its_str_would_have_returned() -> None:
    """FINDING A, redaction half -- renamed in fix wave 5 (M-3): the old name
    (`..._a_secret_named_non_string_dict_key_is_still_redacted`) claimed this
    tests secret-NAMING, but it never did and does even less so now --
    `_PasswordKey` is not one of `_KEY_COERCIBLE`'s allowlisted types (fix
    wave 5, I-2), so `_shaped_key` redacts it outright before
    `looks_like_secret_key` is ever called, whether or not its `__str__()`
    happens to look secret-shaped. Originally (UPDATED for fix wave 4, change
    1): a non-str key used to be coerced with `str()` and THEN checked
    against `looks_like_secret_key`, so this test's `_PasswordKey` landed on
    the literal `"password"` key; fix wave 4 stopped coercing an arbitrary
    non-str key at all, redacting it outright instead. What this test
    actually pins: a non-str, non-allowlisted key redacts via the numbered
    `[redacted-key]` placeholder regardless of what its `__str__()` would
    have returned, secret-shaped or not."""

    class _PasswordKey:
        def __str__(self) -> str:
            return "password"

    line = JsonFormatter().format(_record(config={_PasswordKey(): "hunter2"}))
    assert "hunter2" not in line
    payload = json.loads(line)
    assert payload["config"] == {"[redacted-key]": REDACTED}


# --- Blocker 1: dict-KEY path was fail-open -----------------------------------


def test_an_exception_used_as_a_dict_key_is_not_stringified() -> None:
    """Key-half twin of `test_an_exception_passed_as_extra_is_not_stringified`.

    Reproduced in the final pre-merge review: `extra={"m": {LeakyErr(): "ok"}}`
    used to write `LeakyErr().__str__()` -- a live connection string -- into
    the KEY position verbatim, even though the exact same object as a VALUE
    would have rendered as its type name. `str(LeakyErr())` is not
    identifier-shaped, so it is now caught by the same redact-non-identifier-
    keys rule that catches a raw DSN used as a key (below): the whole entry
    is redacted rather than the secret text ever reaching output.
    """
    line = JsonFormatter().format(_record(m={_LeakyErr(): "ok"}))
    assert "SECRETPW" not in line, "the exception's str() leaked through the key position"
    payload = json.loads(line)
    assert payload["m"] == {"[redacted-key]": REDACTED}


def test_a_string_dict_key_carrying_a_dsn_is_redacted() -> None:
    """Reproduced in the final pre-merge review:
    `extra={"results": {"postgres://u:hunter2@h/db": "ok"}}` used to write the
    DSN straight through, because the key path was never subjected to the
    value-position rules. A DSN is not identifier-shaped, so it is now caught."""
    line = JsonFormatter().format(
        _record(results={"postgres://u:hunter2@h/db": "ok"})
    )
    assert "hunter2" not in line, "the DSN key leaked verbatim"
    payload = json.loads(line)
    assert payload["results"] == {"[redacted-key]": REDACTED}


def test_a_secret_shaped_key_no_longer_leaves_the_key_itself_exposed() -> None:
    """Reproduced in the final pre-merge review, the 'perverse' case:
    `extra={"d": {"password=hunter2": 1}}` used to redact the VALUE (1, which
    carried no secret) while printing the KEY verbatim -- which is exactly
    where the secret was. `"password=hunter2"` is not identifier-shaped, so
    the whole entry is now redacted instead."""
    line = JsonFormatter().format(_record(d={"password=hunter2": 1}))
    assert "hunter2" not in line, "the secret-carrying key leaked verbatim"
    payload = json.loads(line)
    assert payload["d"] == {"[redacted-key]": REDACTED}


def test_a_dict_key_shaped_like_a_header_survives_intact() -> None:
    """I1: the Critical fix's `str.isidentifier()` rule was stricter than a
    field name needs to be -- ordinary operator data like an HTTP header name
    was being annihilated alongside the DSNs and exception text it was built
    to catch. `[\\w.-]+` is a wider, still-strict shape test that lets a
    hyphenated header name through without reopening any of the Critical
    fix's four reproduction cases (see the tests above, still redacted)."""
    line = JsonFormatter().format(
        _record(headers={"user-agent": "curl/8.5", "x-forwarded-for": "10.0.0.9"})
    )
    payload = json.loads(line)
    assert payload["headers"] == {
        "user-agent": "curl/8.5",
        "x-forwarded-for": "10.0.0.9",
    }


def test_a_dotted_config_key_survives_intact() -> None:
    """I1: `.` is field-name-shaped (dotted-config keys like `retry.max` are
    ordinary operator data) and must not be redacted."""
    payload = json.loads(JsonFormatter().format(_record(config={"retry.max": 3})))
    assert payload["config"] == {"retry.max": 3}


def test_a_hyphenated_device_id_key_survives_intact() -> None:
    """I1: `"db-01"` already survives as a VALUE
    (`test_an_ordinary_field_survives_intact`); this is the same string in KEY
    position, which the Critical fix's `isidentifier()` rule wrongly
    annihilated because `-` is not identifier-shaped."""
    payload = json.loads(JsonFormatter().format(_record(hosts={"db-01": "online"})))
    assert payload["hosts"] == {"db-01": "online"}


def test_a_numeric_string_key_survives_intact() -> None:
    """I1: `isidentifier()` rejects a leading digit; `[\\w.-]+` does not. A
    numeric-string key (e.g. an array-like index serialized as a dict) is
    ordinary data, not a secret."""
    payload = json.loads(JsonFormatter().format(_record(counts={"42": "ok"})))
    assert payload["counts"] == {"42": "ok"}


def test_multiple_redacted_keys_in_one_dict_get_distinct_numbered_slots() -> None:
    """I2: every redacted key used to write into the same `[redacted-key]`
    slot, so a dict with several non-field-name keys silently collapsed to
    one entry and the cardinality was lost. The first redacted key keeps the
    exact placeholder the Blocker-1 tests above assert byte-for-byte; later
    ones in the same dict get a numbered slot instead of overwriting it."""
    line = JsonFormatter().format(
        _record(
            d={
                "password=hunter2": 1,
                "postgres://u:pw@h/db": 2,
                "boom SECRETPW": 3,
            }
        )
    )
    payload = json.loads(line)
    assert payload["d"] == {
        "[redacted-key]": REDACTED,
        "[redacted-key-2]": REDACTED,
        "[redacted-key-3]": REDACTED,
    }


def test_a_coerced_key_colliding_with_an_equal_str_key_gets_a_numbered_slot() -> None:
    """M-8: `{200: "a", "200": "b"}` -- an int key and a str key that shape to
    the SAME text -- used to let the second entry silently overwrite the
    first: `{"200": "b"}`, one value gone with no marker. That is the
    cardinality loss `_redacted_key_slot` exists to prevent for redacted
    keys, reopened for surviving ones. A colliding shaped key now gets a
    numbered slot instead of erasing the entry it collides with.

    UPDATED, fix wave 7 (M-4): the slot's marker changed from `"200-2"` to
    `"200[dup-2]"` -- see `test_a_collision_slot_does_not_overwrite_a_real_
    key_of_the_same_name` for why `"200-2"` itself was not safe to use."""
    line = JsonFormatter().format(_record(d={200: "a", "200": "b"}))
    payload = json.loads(line)
    assert payload["d"] == {"200": "a", "200[dup-2]": "b"}, (
        "a coerced key that collides with an existing shaped key must not "
        "silently overwrite it"
    )


def test_a_collision_slot_does_not_overwrite_a_real_key_of_the_same_name() -> None:
    """M-4: `_collision_slot`'s old marker (`f"{shaped}-{index}"`, e.g.
    `"200-2"`) was itself field-name-shaped -- `-` and digits are inside
    `_FIELD_NAME`'s character class -- so it could collide with a THIRD,
    genuinely distinct literal key that happens to spell the same text.
    Measured before this fix: `{200: "a", "200": "b", "200-2": "c"}` walked
    to `{"200": "a", "200-2": "c"}` -- "b"'s value silently dropped, the
    exact cardinality loss M-8 exists to prevent, one level up. The marker is
    now bracketed (`"200[dup-2]"`), and `[`/`]` are outside `_FIELD_NAME`'s
    character class, so no key that has ever passed the shape gate can equal
    it -- all three values must survive."""
    line = JsonFormatter().format(_record(d={200: "a", "200": "b", "200-2": "c"}))
    payload = json.loads(line)
    assert payload["d"] == {"200": "a", "200[dup-2]": "b", "200-2": "c"}, (
        "the synthetic collision slot must not overwrite a real key that "
        "happens to spell the same text"
    )


# --- Fix wave 4: the key-shape gate was exactly the alphabet of a JWT --------

# base64url segments joined by `.`, 85 characters -- this platform's own
# bearer-token shape (`app/core/security.py`). Module-level so every test
# below that needs "a JWT" uses the identical, measured-length string.
_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9."
    "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
)


def test_a_jwt_in_key_position_is_redacted() -> None:
    """Fix wave 4 Critical: `[\\w.-]+` (I1's widening) is exactly the alphabet
    of a JWT -- base64url segments joined by `.` -- and `\\w` is Unicode, not
    ASCII. A key in that shape went from redacted to VERBATIM when the shape
    gate widened, and this control plane issues and validates JWTs of its own
    (invariant #3). The new gate is ASCII-only and capped at
    `_MAX_FIELD_NAME_LEN` (40) characters; this JWT is 85."""
    line = JsonFormatter().format(_record(by_sess={_JWT: "ok"}))
    assert _JWT not in line, "a JWT in key position leaked verbatim"
    payload = json.loads(line)
    assert payload["by_sess"] == {"[redacted-key]": REDACTED}


class _JwtStrErr(Exception):
    """An exception whose __str__ is FIELD-NAME-SHAPED (a JWT), unlike
    `_LeakyErr` above whose __str__ happens to contain `:`, `/` and `@`.
    `test_an_exception_used_as_a_dict_key_is_not_stringified` (frozen) only
    pins that ONE instance -- it passes today whether or not a non-str key is
    ever stringified, because the punctuation in `_LeakyErr`'s text would have
    failed the shape gate either way. This class is the one that actually
    proves the property: if the key path still called `str()` on a non-str
    key before shape-checking it, THIS exception's `__str__()` would sail
    through the (ASCII, ≤40-char) shape gate and land in output verbatim,
    because a JWT satisfies that shape."""

    def __str__(self) -> str:
        return _JWT


def test_an_exception_whose_str_is_field_name_shaped_is_still_redacted() -> None:
    """Property-level twin of `test_an_exception_used_as_a_dict_key_is_not_
    stringified`. Fix wave 4, change 1: a non-str key is never coerced with
    `str()` at all -- it is redacted outright, unconditionally -- so it does
    not matter that this exception's `__str__()` happens to be shape-passing;
    `str()` is never even called on it. This is what closes the
    exception-as-key leak class COMPLETELY rather than relying on the
    resulting text's punctuation, the way the frozen test above incidentally
    does."""
    line = JsonFormatter().format(_record(failures={_JwtStrErr(): "rejected"}))
    assert _JWT not in line, (
        "a field-name-shaped exception str() leaked through the key position"
    )
    payload = json.loads(line)
    assert payload["failures"] == {"[redacted-key]": REDACTED}


# --- Fix wave 5: I-1, a `str` subclass defeats the secret-word predicate -----


class _Sneaky(str):
    """A `str` subclass whose `translate` is overridden -- exactly the method
    `looks_like_secret_key` calls as part of NFKC normalisation. `unicodedata
    .normalize("NFKC", key)` returns the input object ITSELF when it is
    already normalised, so this subclass's own `.translate` runs instead of
    `str`'s, and `looks_like_secret_key` becomes all method dispatch with no
    way to tell a lying subclass from an honest one."""

    def translate(self, *args: object, **kwargs: object) -> str:
        return "harmless"


def test_a_str_subclass_key_is_redacted_regardless_of_its_own_methods() -> None:
    """I-1: the shape gate's old `isinstance(key, str)` accepts a subclass, so
    a hostile `str` subclass whose overridden `translate`/`lower` defeat
    `looks_like_secret_key` sailed through as an ordinary field name and its
    value reached output in plaintext -- `looks_like_secret_key(_Sneaky
    ("password"))` returned `False`. `type(key) is str` rejects the subclass
    outright: it is not a field name a developer typed in a dict literal, so
    it is redacted like any other non-str key, closing the hole without
    touching `looks_like_secret_key` itself."""
    line = JsonFormatter().format(_record(config={_Sneaky("password"): "hunter2"}))
    assert "hunter2" not in line, "the str-subclass key's value leaked in plaintext"
    payload = json.loads(line)
    assert payload["config"] == {"[redacted-key]": REDACTED}


# --- Fix wave 5: I-2, key position must agree with value position's --------
# --- "structurally cannot carry a credential" allowlist ---------------------


def test_an_int_dict_key_survives_intact() -> None:
    """I-2: value position allowlists `int` as JSON-native; key position's
    blanket non-str rule annihilated it anyway, so a `Counter` keyed by HTTP
    status (`{200: 41, 404: 12}`) -- ordinary RMM telemetry -- collapsed to
    `{"[redacted-key]": "[redacted]", "[redacted-key-2]": "[redacted]"}`. An
    `int` key structurally cannot carry a credential, exactly like an `int`
    value, so it is now coerced and kept."""
    line = JsonFormatter().format(_record(by_status={200: 41, 404: 12}))
    payload = json.loads(line)
    assert payload["by_status"] == {"200": 41, "404": 12}


def test_a_uuid_dict_key_survives_intact() -> None:
    """I-2: a fleet map keyed by device UUID (`{UUID(...): "online"}`) is
    ordinary RMM telemetry. `_COERCIBLE` already allowlists `UUID` as a value
    type ("structurally cannot carry a credential"); key position now agrees
    -- the same UUID surviving as a string key
    (`test_a_hyphenated_device_id_key_survives_intact`-style) and as a UUID
    OBJECT key must not be incoherent."""
    device_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    line = JsonFormatter().format(_record(devices={device_id: "online"}))
    payload = json.loads(line)
    assert payload["devices"] == {str(device_id): "online"}


def test_a_datetime_dict_key_still_redacts_closed() -> None:
    """I-3: `datetime` is on `_COERCIBLE` and survives in VALUE position (see
    `test_uuid_and_datetime_still_render_usefully`), but it does NOT survive
    in key position, and two test docstrings used to assert in prose that it
    did. Measured: `str(datetime(2026, 1, 1, tzinfo=UTC))` is
    `"2026-01-01 00:00:00+00:00"` -- a space and two colons -- which always
    fails the field-name shape gate, so `_KEY_COERCIBLE` does not list it
    (see that tuple's own comment). This pins the exclusion executably rather
    than leaving it assumed."""
    line = JsonFormatter().format(_record(seen={datetime(2026, 1, 1, tzinfo=UTC): "ok"}))
    payload = json.loads(line)
    assert payload["seen"] == {"[redacted-key]": REDACTED}


def test_a_path_dict_key_still_redacts_closed() -> None:
    """I-3: `Path` is on `_COERCIBLE` and survives in VALUE position, but it
    does NOT survive in key position: `type(Path("/x"))` is `PosixPath` (or
    `WindowsPath`), never `pathlib.Path` itself, so the exact-type membership
    check in `_KEY_COERCIBLE` can never fire for it -- measured, not merely
    reasoned about. This pins the exclusion executably rather than leaving it
    assumed."""
    line = JsonFormatter().format(_record(rotated={Path("app.log"): "rotated"}))
    payload = json.loads(line)
    assert payload["rotated"] == {"[redacted-key]": REDACTED}


def test_a_hostile_int_subclass_key_is_redacted_not_coerced() -> None:
    """I-2: exact-type checks throughout, not `isinstance` -- an `int`
    subclass overriding `__str__` could otherwise smuggle secret text through
    the key-coercion path the same way I-1's `str` subclass smuggled it
    through the shape gate. `type(key) is int` rejects the subclass; only a
    literal `int` is coerced."""

    class EvilInt(int):
        def __str__(self) -> str:
            return "postgres://u:pw@h/db"

    line = JsonFormatter().format(_record(config={EvilInt(200): "ok"}))
    assert "postgres://u:pw@h/db" not in line, "the int subclass's __str__ leaked via a key"
    payload = json.loads(line)
    assert payload["config"] == {"[redacted-key]": REDACTED}


def test_a_key_at_the_40_character_cap_survives_and_41_is_redacted() -> None:
    """Fix wave 4: the length cap is a MEASURED number (every dict-literal
    string key, annotated attribute name and keyword-argument name under
    `backend/app/`: 1941 distinct names, 100% ASCII, longest 36 characters,
    p99 = 26), not a guess -- pin both sides of the boundary so it cannot
    silently drift to an off-by-one."""
    at_cap = "a" * 40
    over_cap = "a" * 41
    payload = json.loads(
        JsonFormatter().format(_record(config={at_cap: "ok", over_cap: "ok"}))
    )
    assert payload["config"][at_cap] == "ok"
    assert payload["config"]["[redacted-key]"] == REDACTED
    assert over_cap not in json.dumps(payload)


def test_a_hostname_over_the_cap_redacts_closed() -> None:
    """I-3: spec §1.2 says "a hostname used as a key is deliberately
    permitted" -- true only under the 40-character cap, which its own example
    (32 characters) happens to satisfy. `_MAX_FIELD_NAME_LEN` is NOT raised to
    fix this (that would re-admit the token shapes the cap exists to reject);
    a real hostname over the cap redacts closed instead, losing operator
    signal rather than leaking anything -- the safe failure direction. Pinned
    so the limitation is executable, not merely described."""
    under_cap = "db-prod-01.internal.toeshee.corp"  # 32 chars, spec's own example
    over_cap = "db-prod-01.eu-west-1.internal.toeshee.corp"  # 42 chars
    assert len(under_cap) <= 40
    assert len(over_cap) > 40
    payload = json.loads(
        JsonFormatter().format(_record(hosts={under_cap: "ok", over_cap: "fail"}))
    )
    assert payload["hosts"][under_cap] == "ok"
    assert payload["hosts"]["[redacted-key]"] == REDACTED
    assert over_cap not in json.dumps(payload)


def test_a_homoglyph_key_is_redacted_closed() -> None:
    """Fix wave 4: the old shape gate (`\\w`) is Unicode and admits Cyrillic,
    and `looks_like_secret_key` deliberately does NOT fold homoglyphs (see its
    docstring in `app/core/secrets.py`) -- it relies on a declared-contract
    check that the log sink does not have. The new shape gate is an explicit
    ASCII character class, not `\\w`, so a homoglyph key now FAILS the shape
    gate itself and the whole entry redacts CLOSED. This turns what used to be
    a plaintext VALUE leak (`looks_like_secret_key` never even seeing the real
    word) into a redaction -- the fail-closed behaviour this kernel exists
    for."""
    line = JsonFormatter().format(_record(cfg={"pаssword": "hunter2"}))  # Cyrillic а
    assert "hunter2" not in line, "the homoglyph key's value leaked in plaintext"
    payload = json.loads(line)
    assert payload["cfg"] == {"[redacted-key]": REDACTED}


def test_exc_type_from_extra_does_not_appear_without_a_real_exception() -> None:
    """Fix wave 4, change 3: `exc_type` is a synthetic key like `ts`/`level`/
    `logger`/`message`, but unlike those it was only ever WRITTEN when
    `record.exc_info` is set -- nothing DISCARDED a caller-supplied
    `extra={"exc_type": ...}` first, so on a clean record the caller's value
    simply survived untouched, unlike a real synthetic key. `logger.info("ok",
    extra={"exc_type": "PaymentSucceeded"})` must not let a caller spoof the
    exception-type field on a record with no exception."""
    payload = json.loads(JsonFormatter().format(_record(exc_type="PaymentSucceeded")))
    assert "exc_type" not in payload


def test_an_opaque_token_in_key_POSITION_is_NOT_redacted() -> None:
    """I3 -- THIS ASSERTS A GAP, DELIBERATELY. Key-position twin of
    `test_a_percent_arg_is_NOT_redacted`.

    Redaction is by NAME: `looks_like_secret_key` (app/core/secrets.py)
    matches secret *words* after normalisation, not entropy. A SHORT, ASCII,
    field-name-shaped opaque secret -- an AWS access key ID, a `ghp_...` PAT,
    a `sk_live_...` key -- has no name for that predicate to match when it
    sits in KEY position, and the ASCII/length-capped shape gate (fix wave 4)
    still accepts its shape as an ordinary field name. So it survives
    verbatim. This is not a regression -- it is the same class of gap as the
    `%`-arg hole (spec §1.2), just not previously documented or pinned. A
    value that could carry secret material belongs in `extra=` under a
    field name the developer CHOSE, never as a dict key.

    FIX WAVE 4 CLOSED THE LONG HALF OF THIS GAP: a JWT (85 characters) and any
    other base64/opaque blob over `_MAX_FIELD_NAME_LEN` (40) characters no
    longer sails through the shape gate -- see
    `test_a_jwt_in_key_position_is_redacted`. Only the SHORT, sub-40-character
    opaque token remains open; this test now pins both the surviving gap and
    the newly closed part in one place.

    IF THIS TEST EVER FAILS ON THE SHORT-TOKEN ASSERTION, key-position
    redaction has grown an entropy (or similar) heuristic -- which is good
    news, and means spec §1.2 must be rewritten rather than this test deleted.
    """
    short_token = "AKIAIOSFODNN7EXAMPLE"
    line = JsonFormatter().format(_record(by_sess={short_token: "ok", _JWT: "ok"}))
    assert short_token in line, (
        "an opaque token in key position is now redacted -- rewrite spec §1.2 "
        "and this test's docstring"
    )
    assert _JWT not in line, (
        "the long/JWT form leaked -- fix wave 4's length cap regressed"
    )
    payload = json.loads(line)
    assert payload["by_sess"][short_token] == "ok"
    assert payload["by_sess"]["[redacted-key]"] == REDACTED


def test_a_non_string_top_level_extra_key_does_not_crash_the_formatter() -> None:
    """M3: stdlib `logging` writes `extra=` straight into `record.__dict__`
    without validating that its keys are strings, so a non-str key can reach
    the TOP LEVEL of a record, not only nested inside an `extra=` dict.
    Before this fix, `looks_like_secret_key(key)` (and/or `json.dumps`) was
    handed that key directly and raised, losing the record entirely. The same
    `_shaped_key` rule `_safe_value` applies to nested dict keys applies here
    too, via one shared helper rather than a second copy of the rule -- a
    tuple is not one of `_KEY_COERCIBLE`'s allowlisted types (fix wave 5,
    I-2), so it still redacts outright here exactly as it does nested."""
    record = _record()
    # `record.__dict__` is typed `dict[str, Any]`, so mypy is right to refuse
    # this -- and refusing it is the point: stdlib `logging` performs exactly
    # this write for `extra={<non-str>: ...}`, which is the input the
    # formatter must survive. The suppression marks a deliberate violation,
    # not an inconvenience.
    record.__dict__[("t", "k")] = "v"  # type: ignore[index]
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["[redacted-key]"] == REDACTED


def test_a_dict_key_whose_str_raises_does_not_propagate() -> None:
    """A key whose __str__ raises must fail closed like everything else in
    `_safe_value`, not propagate out of `format()`."""

    class _BrokenKey:
        def __str__(self) -> str:
            raise ValueError("boom")

    line = JsonFormatter().format(_record(config={_BrokenKey(): "v"}))
    payload = json.loads(line)
    # Fix wave 4: a non-str key is redacted outright and `str()` is never
    # called on it at all, so `_BrokenKey.__str__` raising is never even
    # reached -- the point of this test is that format() didn't raise either
    # way.
    assert payload["config"] == {"[redacted-key]": REDACTED}


def test_a_self_referential_dict_does_not_recurse_forever() -> None:
    """FINDING B: a cyclic structure in `extra=` must not raise RecursionError
    -- the depth cap renders it as '<truncated>' instead."""
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    line = JsonFormatter().format(_record(loop=cyclic))
    payload = json.loads(line)
    assert "<truncated>" in json.dumps(payload)


def test_structures_deeper_than_max_depth_are_truncated() -> None:
    """FINDING B: a legitimately deep (non-cyclic) structure must also be
    capped, not just cycles -- the depth limit is unconditional."""
    from app.core.logging import _MAX_DEPTH

    deep: dict[str, object] = {}
    cursor = deep
    for _ in range(_MAX_DEPTH + 5):
        nxt: dict[str, object] = {}
        cursor["nested"] = nxt
        cursor = nxt
    cursor["leaf"] = "unreachable"

    line = JsonFormatter().format(_record(deep=deep))
    payload = json.loads(line)
    assert "<truncated>" in json.dumps(payload)
    assert "unreachable" not in line


def test_a_raising_str_on_an_allowlisted_type_renders_as_type_name() -> None:
    """FINDING C: an allowlisted type (e.g. a UUID/Path subclass) whose
    __str__ raises must not propagate -- it must fail closed to a type
    name, same as everything else in _safe_value."""

    class _BrokenPath(Path):
        def __str__(self) -> str:
            raise ValueError("boom")

    broken = _BrokenPath("x")
    line = JsonFormatter().format(_record(thing=broken))
    payload = json.loads(line)
    assert payload["thing"] == "<_BrokenPath>"


# --- Fix wave 5: M-1, a dynamically-named class defeats the type-name rule --


def test_a_dynamically_named_exception_type_does_not_leak_via_exc_type() -> None:
    """M-1: `exc_type` is written as `exc_info[0].__name__` with no shape gate
    in the way -- but `type.__name__` is controlled at class-definition time,
    and `type("postgres://u:hunter2@h/db", (Exception,), {})` is legal Python
    whose `.__name__` IS that string verbatim. The module's stated rule is
    that the TYPE NAME is the safe rendering (never `str(exc)`); this is the
    case where the type name itself would be the leak, so it goes through the
    same shape gate a field name does and falls back to a constant when it
    fails."""
    evil = type("postgres://u:hunter2@h/db", (Exception,), {})
    record = _record()
    record.exc_info = (evil, evil(), None)
    line = JsonFormatter().format(record)
    assert "postgres://u:hunter2@h/db" not in line, "the dynamic class name leaked via exc_type"
    payload = json.loads(line)
    assert payload["exc_type"] == "<unprintable>"


def test_an_arbitrary_object_with_a_hostile_type_name_renders_as_a_placeholder() -> None:
    """M-1, `_safe_value`'s fallback half: the same `type(...).__name__`
    rendering is the module's fail-closed answer for ANY non-JSON-native,
    non-allowlisted object (`test_an_arbitrary_object_in_extra_renders_as_a_
    type_name`) -- so a class whose NAME is itself secret-shaped text must
    not reach output there either."""
    evil = type("postgres://u:hunter2@h/db", (), {})
    line = JsonFormatter().format(_record(thing=evil()))
    assert "postgres://u:hunter2@h/db" not in line, "the hostile type name leaked as a value"
    payload = json.loads(line)
    assert payload["thing"] == "<unprintable>"


# --- Request context middleware -----------------------------------------------


def test_the_middleware_sets_a_request_id_and_echoes_it() -> None:
    """§5 test 7, middleware half. Uses a bare Starlette app rather than the
    real one so it needs no database.

    Registers via `add_middleware(BaseHTTPMiddleware, dispatch=...)` rather
    than the `app.middleware("http")(...)` decorator: the installed Starlette
    dropped that decorator from the base `Starlette` class (it survives only
    on `FastAPI`, which wraps this same call) -- functionally identical
    registration, just spelled for the installed version.
    """
    import httpx
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from app.api.request_context import request_context_middleware

    seen: dict[str, str | None] = {}

    async def handler(_request: object) -> JSONResponse:
        seen["inside"] = request_id_var.get()
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/x", handler)])
    app.add_middleware(BaseHTTPMiddleware, dispatch=request_context_middleware)

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get("/x")

    response = asyncio.run(drive())
    assert response.status_code == 200
    assert seen["inside"], "the handler saw no request_id"
    assert response.headers["X-Request-ID"] == seen["inside"]


def test_an_inbound_request_id_is_ignored() -> None:
    """Spec §3: the kernel NEVER trusts a caller-supplied id. A caller could
    collide with another org's or poison an operator's log search."""
    import httpx
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from app.api.request_context import request_context_middleware

    async def handler(_request: object) -> JSONResponse:
        return JSONResponse({"id": request_id_var.get()})

    app = Starlette(routes=[Route("/x", handler)])
    app.add_middleware(BaseHTTPMiddleware, dispatch=request_context_middleware)

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get("/x", headers={"X-Request-ID": "forged-by-caller"})

    response = asyncio.run(drive())
    assert response.json()["id"] != "forged-by-caller"
    assert response.headers["X-Request-ID"] != "forged-by-caller"


def test_an_unhandled_exception_has_no_request_id_header() -> None:
    """THIS ASSERTS A GAP, DELIBERATELY.

    `response.headers[_HEADER] = request_id` in `request_context_middleware`
    runs only after `call_next` returns. When a handler raises something no
    exception handler catches, that line never executes and the 500 that
    Starlette's `ServerErrorMiddleware` eventually builds carries no
    `X-Request-ID`. The contextvar itself is unaffected -- it is set before
    `call_next` and reset in a `finally`, so log lines emitted while handling
    the failing request DO carry the request id; only the response header is
    absent, which is a client convenience, not the log correlation guarantee.
    See the module docstring of `app/api/request_context.py` for why this is
    pinned rather than fixed.

    IF THIS TEST EVER FAILS, the gap has been closed -- rewrite this test and
    that module docstring rather than deleting either.
    """
    import httpx
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.routing import Route

    from app.api.request_context import request_context_middleware

    async def handler(_request: object) -> None:
        raise RuntimeError("boom")

    app = Starlette(routes=[Route("/x", handler)])
    app.add_middleware(BaseHTTPMiddleware, dispatch=request_context_middleware)

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get("/x")

    response = asyncio.run(drive())
    assert response.status_code == 500
    assert "X-Request-ID" not in response.headers
    assert request_id_var.get() is None, "the contextvar leaked past the failing request"


# --- Fix wave 8: `celery.app.trace` off propagation, dedicated handler ----
# --- that SYNTHESISES every line -- Celery's own text is never rendered ---


class _DbErr(Exception):
    """An exception whose repr/str carry a live DSN -- exactly what
    `safe_repr(eobj)` renders into Celery's `context['exc']`."""

    def __init__(self) -> None:
        super().__init__("postgres://its:S3CRET-DB-PASSWORD@db/prod")

    def __repr__(self) -> str:
        return f"_DbErr({self.args[0]!r})"


@contextmanager
def _configured_logging_capturing_stdout() -> Iterator[io.StringIO]:
    """Run the REAL `configure_logging()`, with `sys.stdout` swapped for a
    buffer before the call so the `StreamHandler` it builds binds to the
    buffer -- every test below proves the defence as `configure_logging()`
    actually wires it, not as a hand-built handler would. That gap (an
    installation nobody tested) is exactly what let fix wave 7's filter be
    deleted with the whole suite green -- see
    `test_configure_logging_takes_celery_off_propagation_at_the_tree_root`,
    which exists to catch it this time.

    Saves and restores every piece of global `logging` state
    `configure_logging()` touches (root, the three uvicorn loggers, and --
    fix wave 9 widened the attachment point from `celery.app.trace` to the
    `celery` tree root, so this is what is saved and restored now -- the
    `celery` logger) so this test module hands later tests -- in this file
    and others -- back the state it found, rather than F-8's mistake of
    hardcoding "empty and WARNING" in a `finally`.

    Patches the real `sys.stdout`, not `app.core.logging`'s imported
    reference to it, so `configure_logging`'s own `StreamHandler(sys.stdout)`
    call -- which looks the attribute up on the one shared `sys` module
    object at call time -- binds to the buffer regardless of which module's
    `import sys` reads it."""
    from app.core.logging import configure_logging

    root = logging.getLogger()
    prev_root_handlers = root.handlers[:]
    prev_root_level = root.level
    uvicorn_names = ("uvicorn", "uvicorn.access", "uvicorn.error")
    prev_uvicorn = [
        (logging.getLogger(name).handlers[:], logging.getLogger(name).propagate)
        for name in uvicorn_names
    ]
    celery_logger = logging.getLogger("celery")
    prev_celery_handlers = celery_logger.handlers[:]
    prev_celery_propagate = celery_logger.propagate
    prev_stdout = sys.stdout

    stream = io.StringIO()
    sys.stdout = stream
    try:
        configure_logging()
        yield stream
    finally:
        sys.stdout = prev_stdout
        root.handlers[:] = prev_root_handlers
        root.setLevel(prev_root_level)
        for name, (handlers, propagate) in zip(uvicorn_names, prev_uvicorn, strict=True):
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.propagate = propagate
        celery_logger.handlers[:] = prev_celery_handlers
        celery_logger.propagate = prev_celery_propagate


def test_configure_logging_takes_celery_off_propagation_at_the_tree_root() -> None:
    """The Critical fix wave 9 exists to fix, widened from fix wave 8's own
    reverse-proof test (previously named `test_configure_logging_takes_
    celery_app_trace_off_propagation`): fix wave 7's defence
    (`_CeleryTraceSanitiser`) had an install line
    (`handler.addFilter(_CeleryTraceSanitiser())`) that NOTHING asserted
    `configure_logging()` actually ran -- deleting it left 67 tests green and
    put the DB password back on worker stdout, because every one of that
    filter's own tests built its OWN handler and called `addFilter` itself.

    This test must fail if EITHER line `configure_logging()` uses to install
    the wave-9 defence is deleted: `celery_logger.handlers[:] = [...]` (the
    dedicated handler on the TREE ROOT, not `celery.app.trace` alone any
    more) or `celery_logger.propagate = False` (without it, a `celery`-tree
    record ALSO reaches root's `JsonFormatter` with Celery's own message and
    args still attached, alongside the dedicated handler's safe rendering).

    `celery.app.trace` itself is asserted UNCHANGED -- default propagation,
    no handler of its own -- because fix wave 9 removed its dedicated wiring
    entirely: it is a descendant of `celery` and now reaches the same
    handler purely by propagation, which is the point of the widening."""
    from app.core.logging import _CeleryHandler

    with _configured_logging_capturing_stdout():
        celery_logger = logging.getLogger("celery")
        assert celery_logger.propagate is False, (
            "celery must be off propagation -- otherwise a celery-tree record still "
            "reaches root's handler with Celery's own message/args attached"
        )
        assert len(celery_logger.handlers) == 1, (
            "celery must carry exactly the dedicated handler"
        )
        assert isinstance(celery_logger.handlers[0], _CeleryHandler), (
            "celery's handler is not the dedicated one -- it would fall through to "
            "whatever else is installed, rendering Celery's own text"
        )

        trace_logger = logging.getLogger("celery.app.trace")
        assert trace_logger.handlers == [], (
            "celery.app.trace must not get its own wiring any more -- it reaches "
            "the tree-root handler by propagation, and a second attachment point "
            "would just be two places for this to drift apart"
        )
        assert trace_logger.propagate is True, (
            "celery.app.trace must still propagate -- that is how it reaches the "
            "celery-root handler now that it has none of its own"
        )


def test_celery_task_failure_record_through_the_kernel_never_leaks_the_exceptions_text() -> None:
    """Reproduces `celery.app.trace.TraceInfo._log_error`'s EXACT call shape
    -- positional `context` as the sole `%`-arg AND `extra={'data': context}`
    -- through the REAL `configure_logging()`, not a hand-built handler. A DB
    password sits in `exc`/`traceback`; a task kwarg secret sits in
    `kwargs`. The dedicated handler never reads `record.msg`/`record.args`/
    `record.getMessage()` at all, so neither can reach output regardless of
    what Celery put there.

    `name`/`id`/`hostname` and `exc_type` must survive -- suppression reduces
    the record, it does not blind the operator."""
    exc = _DbErr()
    context = {
        "hostname": "worker@prod-1",
        "id": "task-abc-123",
        "name": "app.worker.patch_scan_sweep_all_orgs",
        "exc": repr(exc),
        "traceback": f"Traceback (most recent call last):\n{exc!r}",
        "args": "()",
        "kwargs": "{'password': 'hunter2'}",
        "description": "raised unexpected",
        "internal": False,
    }
    with _configured_logging_capturing_stdout() as stream:
        logging.getLogger("celery.app.trace").log(
            logging.ERROR,
            "Task %(name)s[%(id)s] %(description)s: %(exc)s",
            context,
            exc_info=(type(exc), exc, None),
            extra={"data": context},
        )

    output = stream.getvalue()
    assert "S3CRET-DB-PASSWORD" not in output, "the DSN in exc/traceback leaked"
    assert "hunter2" not in output, "the task kwarg secret leaked"
    payload = json.loads(output)
    assert payload["exc_type"] == "_DbErr", "the exception TYPE (never its text) must survive"
    assert payload["data"] == {
        "name": "app.worker.patch_scan_sweep_all_orgs",
        "id": "task-abc-123",
        "hostname": "worker@prod-1",
        "description": "raised unexpected",
    }


def test_celery_retry_record_through_the_kernel_never_leaks_the_dsn() -> None:
    """The leak the old shape-matching filter missed entirely: `LOG_RETRY`'s
    `context` (`celery/app/trace.py::TraceInfo.handle_retry`) carries no
    `traceback` key, so the old filter's guard (`"traceback" not in data`)
    left it completely unmatched and Celery's own message --
    `"Task %(name)s[%(id)s] retry: %(exc)s"`, `%(exc)s` being
    `str(Retry.exc)` -- rendered verbatim. This handler never reads that
    message at all, so the DSN cannot reach output regardless of which
    Celery record shape carries it."""
    context = {
        "id": "task-retry-1",
        "name": "app.worker.provision_enroll_dispatch",
        "exc": "postgres://its:S3CRET-DB-PASSWORD@db/prod",
    }
    with _configured_logging_capturing_stdout() as stream:
        logging.getLogger("celery.app.trace").info(
            "Task %(name)s[%(id)s] retry: %(exc)s", context, extra={"data": context}
        )

    output = stream.getvalue()
    assert "S3CRET-DB-PASSWORD" not in output, "the DSN inside %(exc)s leaked through LOG_RETRY"
    payload = json.loads(output)
    assert payload["data"]["name"] == "app.worker.provision_enroll_dispatch"
    assert payload["data"]["id"] == "task-retry-1"


def test_celery_success_record_through_the_kernel_never_leaks_the_return_value() -> None:
    """`LOG_SUCCESS`'s `context` (`TraceInfo`/`worker/strategy.py`) has no
    `traceback` key either, so the old filter never matched it -- a task
    returning a credential-shaped value (a provisioning token, say) would
    render straight into `%(return_value)s`. Suppression closes this without
    caring what the return value contains, because it never reads it."""
    context = {
        "id": "task-success-1",
        "name": "app.worker.patch_apply",
        "return_value": "AKIAIOSFODNN7EXAMPLE:S3CRET-DB-PASSWORD",
        "runtime": 0.5,
    }
    with _configured_logging_capturing_stdout() as stream:
        logging.getLogger("celery.app.trace").info(
            "Task %(name)s[%(id)s] succeeded in %(runtime)ss: %(return_value)s",
            context,
            extra={"data": context},
        )

    output = stream.getvalue()
    assert "S3CRET-DB-PASSWORD" not in output, "the credential-shaped return value leaked"
    payload = json.loads(output)
    assert payload["data"]["name"] == "app.worker.patch_apply"
    assert payload["data"]["id"] == "task-success-1"


def test_a_celery_trace_record_with_no_data_at_all_still_emits_a_well_formed_line() -> None:
    """Replaces `test_a_celery_app_trace_record_without_the_data_marker_is_
    left_alone`, deleted by this wave: that test pinned the VULNERABLE
    behaviour -- it asserted a `celery.app.trace` record renders its
    `%`-args VERBATIM, using a call shape (`extra=` absent) Celery itself
    never emits on this logger. This asserts the opposite and correct thing:
    even with no `data` extra at all, the line is still well-formed and
    Celery's own message text is absent -- the fail-closed default the old
    filter lacked (it would have let this exact shape through unmodified)."""
    with _configured_logging_capturing_stdout() as stream:
        logging.getLogger("celery.app.trace").warning("some future celery message shape")

    output = stream.getvalue()
    assert "some future celery message shape" not in output, (
        "Celery's own message text must never reach output, even absent a data extra"
    )
    payload = json.loads(output)
    assert payload["logger"] == "celery.app.trace"
    assert payload["level"] == "WARNING"


# --- Fix wave 9: widened from `celery.app.trace` alone to the whole `celery` -
# --- tree -- `celery.worker.consumer`'s broker password, `celery.worker.  ---
# --- strategy`'s DSN, and the anchoring proof restated at the wider scope ---


def test_a_celery_submodule_logger_untouched_by_wave_8_is_now_also_suppressed() -> None:
    """Fix wave 9 changed this on purpose, and it is worth pinning explicitly
    so nobody "restores" the old behaviour believing it was a regression.
    `celery.app.tracer_metrics` used to be fix wave 8's own proof of
    anchoring (previously `test_a_neighbouring_logger_is_untouched`): it is
    NOT `celery.app.trace`, so wave 8's single-logger handler never touched
    it. But it genuinely IS `record.name.startswith("celery.")` -- a real
    descendant of the `celery` tree, not merely a string that shares a
    prefix -- and this wave's brief is explicit: widen to every logger for
    which that predicate is true. Keeping it exempt would have been the
    OPPOSITE of "widen wave 8's suppression to the whole Celery tree"; it is
    an ordinary Celery module logger, so it now goes through the same
    dedicated handler as `celery.app.trace` and `celery.worker.consumer`,
    losing its own message text along with them -- see
    `test_a_non_celery_logger_that_merely_shares_the_prefix_is_untouched`
    directly below for the logger that is STILL exempt, and why."""
    with _configured_logging_capturing_stdout() as stream:
        logging.getLogger("celery.app.tracer_metrics").info(
            "export %(name)s[%(id)s] on %(hostname)s",
            {"name": "export", "id": None, "hostname": None},
        )

    payload = json.loads(stream.getvalue())
    # F-6 (fix wave 10): this used to assert `payload["message"] !=
    # "export export[None] on None"` -- a negative match on one exact
    # literal, green for any PARTIAL reintroduction of Celery's own
    # rendered text (e.g. the outcome word changing, or the template
    # gaining a trailing character). Asserting the framework's distinctive
    # substring is absent, like every other suppression test in this file,
    # catches that instead of only the byte-identical original string.
    assert "export[None] on None" not in payload["message"], (
        "celery.app.tracer_metrics's own message text must not survive -- it is a "
        "genuine celery-tree descendant, not merely a shared prefix"
    )
    assert payload["logger"] == "celery.app.tracer_metrics"


def test_a_non_celery_logger_that_merely_shares_the_prefix_is_untouched() -> None:
    """Finding 3 (fix wave 8) fixed an UNANCHORED prefix match once already
    (`celery.app.tracer_metrics` sharing text with `celery.app.trace`); the
    brief for this wave names the risk of the SAME bug reappearing at the
    tree level explicitly: `startswith("celery")` WITHOUT the dot would
    swallow `celerybeat_custom`, a logger that shares the letters "celery"
    but is not a dotted descendant of it at all. This handler is attached by
    an EXACT lookup, `logging.getLogger("celery")`, plus ordinary Python
    logger-hierarchy propagation -- never a string comparison against the
    record's name -- so `celerybeat_custom` is a completely separate
    top-level logger to it and is never touched, unlike a bare `startswith`
    check would leave it."""
    with _configured_logging_capturing_stdout() as stream:
        logging.getLogger("celerybeat_custom").info("custom beat tick %s", "ok")

    payload = json.loads(stream.getvalue())
    assert payload["message"] == "custom beat tick ok", (
        "a logger that merely shares the string 'celery' must keep its own message"
    )
    assert payload["logger"] == "celerybeat_custom"


class _FakeConsumerMessage:
    """Minimal stand-in for the `kombu.Message` Celery's consumer passes to
    `dump_body` -- only `.body` is read (see `dump_body`'s own source,
    imported below rather than transcribed, exactly what this wave requires:
    no test may assert a property using a hand-written string standing in
    for a dependency's behaviour)."""

    def __init__(self, body: bytes) -> None:
        self.body = body


def test_celery_worker_consumer_invalid_task_never_leaks_the_task_body() -> None:
    """C-1, retracted and corrected (fix wave 10): replaces
    `test_celery_worker_consumer_broker_connection_failure_never_leaks_the_
    password`, deleted by this wave. That test's premise -- a redis-py
    connection error's own text embeds the broker password, unmasked --
    does NOT reproduce against the pinned dependencies (redis 8.1.0, kombu
    5.6.2, celery 5.6.3); see the module docstring's "FIX WAVE 10" section
    for the measurement. `_BrokerConnErr.__str__` hardcoded the string the
    retracted prose claimed, so the old test passed by construction against
    any dependency version and could never falsify the premise.

    The real, reproducible leak on this same logger: `on_invalid_task`'s
    `error(INVALID_TASK_ERROR, exc, dump_body(message, body),
    exc_info=True)` -- `dump_body`, imported from Celery rather than
    hand-written, returns the RAW SERIALIZED TASK BODY. Task kwargs are this
    platform's provisioning and remote-control material, and this handler
    never reads `msg`/`args`/`getMessage()` regardless of which celery
    logger a record came from, so the body cannot reach output no matter
    which of the four `dump_body` call sites rendered it into the message.

    Imported LOCALLY, not at module level: importing `celery.worker.
    consumer.consumer` pulls in `celery.app.trace` as a side effect, and
    `celery.utils.log.get_logger` (via `kombu.log.get_logger`) attaches a
    `NullHandler` to any logger that has none yet -- at COLLECTION time, a
    module-level import would attach one to `celery.app.trace` before
    `test_configure_logging_takes_celery_off_propagation_at_the_tree_root`
    (above) ever runs, breaking its `trace_logger.handlers == []`
    assertion for a reason that has nothing to do with what it tests."""
    from celery.worker.consumer.consumer import INVALID_TASK_ERROR, dump_body

    body = b'[[], {"password": "PROVISIONING-SECRET-1", "device": "d1"}, {}]'
    message = _FakeConsumerMessage(body)
    exc = ValueError("Received invalid task message")
    with _configured_logging_capturing_stdout() as stream:
        logging.getLogger("celery.worker.consumer.consumer").error(
            INVALID_TASK_ERROR, exc, dump_body(message, body), exc_info=True
        )

    output = stream.getvalue()
    assert "PROVISIONING-SECRET-1" not in output, "the task body's credential leaked"
    payload = json.loads(output)
    assert payload["logger"] == "celery.worker.consumer.consumer"
    assert payload["level"] == "ERROR"
    # F-3: `on_invalid_task` carries no `data`/`headers` extra in Celery's
    # real call shape, reproduced above -- task identity is the accepted
    # loss spec §6/§10 name explicitly, not a silent drop.
    assert payload["data"] == {}


def test_celery_worker_consumer_unknown_task_never_leaks_the_task_body() -> None:
    """C-1, retracted and corrected (fix wave 10): replaces
    `test_celery_worker_strategy_eta_conversion_failure_never_leaks_the_dsn`,
    deleted for the same reason as the test above -- see its docstring and
    the module docstring's "FIX WAVE 10" section. `_StrategyDsnErr` hardcoded
    a DSN into its own `__str__`, proving nothing about any dependency's
    real behaviour.

    Reproduces `on_unknown_task`'s exact call shape:
    `error(UNKNOWN_TASK_ERROR, exc, dump_body(message, body),
    message.headers, message.delivery_info, exc_info=True)` -- four
    `%`-args, no `extra=`. Built from the real `dump_body` and
    `UNKNOWN_TASK_ERROR` template imported from Celery, not a transcription
    of either, so this test tracks the dependency instead of a claim about
    it.

    Imported LOCALLY -- see the docstring of the test above for why a
    module-level import of this Celery submodule is unsafe here."""
    from celery.worker.consumer.consumer import UNKNOWN_TASK_ERROR, dump_body

    body = b'[[], {"api_key": "PROVISIONING-SECRET-2"}, {}]'
    message = _FakeConsumerMessage(body)
    exc = KeyError("no such task")
    with _configured_logging_capturing_stdout() as stream:
        logging.getLogger("celery.worker.consumer.consumer").error(
            UNKNOWN_TASK_ERROR,
            exc,
            dump_body(message, body),
            {"id": "task-1", "task": "no.such.task"},
            "delivery-info",
            exc_info=True,
        )

    output = stream.getvalue()
    assert "PROVISIONING-SECRET-2" not in output, "the task body's credential leaked"
    payload = json.loads(output)
    assert payload["logger"] == "celery.worker.consumer.consumer"
    assert payload["level"] == "ERROR"


def test_a_headers_shaped_extra_recovers_task_identity_on_the_poison_message_path() -> None:
    """F-3 (fix wave 10): `on_unknown_task`/`on_invalid_task`/`on_decode_
    error` (`celery/worker/consumer/consumer.py`) discard a task
    PERMANENTLY and carry no `data` extra at all -- spec §6/§10 now name
    this explicitly as an accepted, but reduced, loss (see the two tests
    above for the accepted-loss default that still applies to Celery's real
    call shape). `_synthesize_celery_record` also reads a `message.
    headers`-shaped extra the same way it already reads `data`; this proves
    the reduction fires when a record does carry one, even though no
    Celery-internal call site does today."""
    with _configured_logging_capturing_stdout() as stream:
        logging.getLogger("celery.worker.consumer.consumer").error(
            "Received invalid task message: %s\nThe message has been ignored.",
            extra={"headers": {"id": "task-99", "task": "app.worker.provision_device"}},
        )

    payload = json.loads(stream.getvalue())
    assert payload["data"]["id"] == "task-99"
    assert payload["data"]["name"] == "app.worker.provision_device"


class _HostileCelerySynthesisData(dict[str, object]):
    """A `data` extra that IS a `dict` (passes `_synthesize_celery_record`'s
    `isinstance` guard) but whose `.get` raises -- the trigger for the
    fail-open path F-1 closes. Any future Celery-internal shape this
    handler was never updated for could raise the same way; this is the
    minimal reproduction, not a claim about a specific Celery version."""

    def get(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("synthesis boom")


class _StderrOnlySecretExc(Exception):
    """An exception whose `repr()` carries a distinctive secret -- stands in
    for whatever object a caller passed as a `%`-arg. Its `str()`/`repr()`
    text must never be readable by `handleError`'s default stderr write."""

    def __repr__(self) -> str:
        return "_StderrOnlySecretExc('STDERR-ONLY-SECRET-999')"


def test_celery_handler_synthesis_failure_never_writes_msg_or_args_to_either_stream() -> None:
    """F-1 (fix wave 10): `_CeleryHandler.emit`'s `except Exception:
    self.handleError(record)` used to hand `handleError` the ORIGINAL
    record. `logging.Handler.handleError`'s own CPython body ends with
    `sys.stderr.write('Message: %r\\nArguments: %s\\n' % (record.msg,
    record.args))` whenever `logging.raiseExceptions` is true -- the
    default, unchanged here -- exactly the two attributes this handler's
    entire design exists to never render.

    Measured: a `data` value whose `.get` raises makes `_synthesize_celery_
    record` itself raise, landing in `emit`'s `except` block with the
    ORIGINAL record still in hand. Before the fix, the full `args` tuple --
    the exception object embedded in it, secret text and all -- reached
    stderr via `handleError`'s default write, which the log driver captures
    exactly like stdout. This asserts BOTH streams stay clean, using the
    real `configure_logging()` wiring, not a hand-built handler."""
    stderr_stream = io.StringIO()
    with _configured_logging_capturing_stdout() as stdout_stream:
        with redirect_stderr(stderr_stream):
            logging.getLogger("celery.worker.consumer.consumer").error(
                "task failed: %s",
                _StderrOnlySecretExc(),
                extra={"data": _HostileCelerySynthesisData(name="worker.task")},
            )

    assert "STDERR-ONLY-SECRET-999" not in stdout_stream.getvalue(), (
        "the secret reached stdout"
    )
    assert "STDERR-ONLY-SECRET-999" not in stderr_stream.getvalue(), (
        "handleError's default stderr write leaked record.msg/record.args -- "
        "the exact fail-open path F-1 exists to close"
    )


def test_a_plain_celery_root_record_with_no_data_is_still_well_formed() -> None:
    """C-1 (fix wave 9): a bare `celery` record -- no `data` extra, no
    exception -- must still produce a well-formed line under the widened
    tree-root defence, exactly as `celery.app.trace` already did without a
    `data` extra (`test_a_celery_trace_record_with_no_data_at_all_still_
    emits_a_well_formed_line`, above). `"worker ready."` is Celery's own
    startup banner (`Logging.setup_logging_subsystem`'s "ready." message) --
    ordinary framework prose this handler never reads."""
    with _configured_logging_capturing_stdout() as stream:
        logging.getLogger("celery").info("worker ready.")

    output = stream.getvalue()
    assert "worker ready." not in output, "Celery's own message text must never reach output"
    payload = json.loads(output)
    assert payload["logger"] == "celery"
    assert payload["level"] == "INFO"


# --- C-1, layer 2 (fix wave 7) / Finding 3 (fix wave 8): `_NEVER_EMIT` -----


def test_never_emit_bites_at_the_top_level() -> None:
    """`_NEVER_EMIT` fields are not LogRecord-reserved (only `args`/
    `exc_text` are -- see `_RESERVED`), so a caller CAN pass one directly as
    a top-level `extra=` field without colliding with a real record
    attribute. `traceback`/`exc`/`kwargs`/`einfo` must redact here exactly as
    they do nested, regardless of `looks_like_secret_key` (none of these
    NAMES reads as a secret to it -- that predicate is not what is closing
    this hole)."""
    payload = json.loads(
        JsonFormatter().format(
            _record(
                traceback="postgres://u:hunter2@h/db",
                exc="LeakyErr: hunter2",
                kwargs="{'password': 'hunter2'}",
                einfo="hunter2",
            )
        )
    )
    for name in ("traceback", "exc", "kwargs", "einfo"):
        assert payload[name] == REDACTED, f"{name} was not redacted at the top level"
    assert "hunter2" not in json.dumps(payload)


def test_never_emit_bites_nested() -> None:
    """Nested twin of the test above -- `_NEVER_EMIT` matters MOST here,
    because a caller elsewhere in the codebase (not just Celery) logging
    `extra={"data": {...}}` with a `traceback`/`exc`/`kwargs` member one
    level down must still have it redacted, regardless of source."""
    payload = json.loads(
        JsonFormatter().format(
            _record(d={"traceback": "hunter2", "kwargs": "hunter2", "host": "db-01"})
        )
    )
    assert payload["d"]["traceback"] == REDACTED
    assert payload["d"]["kwargs"] == REDACTED
    assert payload["d"]["host"] == "db-01", "an ordinary field must still survive"
    assert "hunter2" not in json.dumps(payload)


def test_never_emit_is_case_insensitive() -> None:
    """Finding 3: `_NEVER_EMIT` used to be an exact-match frozenset sitting in
    the same `or` as `looks_like_secret_key`, which is case-insensitive and
    word-decomposing -- so `traceback` redacted while `Traceback`,
    `TRACEBACK` and `Kwargs` all leaked verbatim. Its stated job
    (`logging.py`'s `_NEVER_EMIT` comment) is to survive "a future Celery
    version, a different task-tracing framework, or a caller who names a
    field `traceback`" -- six exact lowercase spellings do not do that.
    Case-folding the membership test closes it without touching the set's
    meaning: `traceback`, `Traceback` and `TRACEBACK` are the same field
    name to a reader, and now to this gate too."""
    payload = json.loads(
        JsonFormatter().format(
            _record(Traceback="hunter2", TRACEBACK="hunter2", Kwargs="hunter2")
        )
    )
    for name in ("Traceback", "TRACEBACK", "Kwargs"):
        assert payload[name] == REDACTED, f"{name} was not redacted"
    assert "hunter2" not in json.dumps(payload)


def test_never_emit_widened_neighbours_are_covered() -> None:
    """Finding 3's fix also widens the set itself with the "obvious
    neighbours" the brief names -- `stacktrace`, `stack_trace`, `tb`,
    `exception`. Each is exactly as capable of carrying rendered exception
    text as the six names `_NEVER_EMIT` already covered, and none of them
    reads as a secret to `looks_like_secret_key` either.

    `exc_info` (also named in the brief) is pinned separately, NESTED, in
    the test below: it is a genuine `LogRecord` attribute (`_RESERVED`
    already excludes it from ever reaching this gate at the TOP level -- see
    `test_never_emit_bites_at_the_top_level`'s twin for `exc_text`/`args`,
    and spec F-7's correction), so the only position where a field literally
    named `exc_info` can appear as caller-chosen `extra=` data is one level
    down inside a dict."""
    payload = json.loads(
        JsonFormatter().format(
            _record(
                stacktrace="hunter2",
                stack_trace="hunter2",
                tb="hunter2",
                exception="hunter2",
            )
        )
    )
    for name in ("stacktrace", "stack_trace", "tb", "exception"):
        assert payload[name] == REDACTED, f"{name} was not redacted"
    assert "hunter2" not in json.dumps(payload)


def test_never_emit_covers_exc_info_nested() -> None:
    """`exc_info` half of the test above -- see its docstring for why this
    one must be nested rather than top-level."""
    payload = json.loads(
        JsonFormatter().format(_record(d={"exc_info": "hunter2", "host": "db-01"}))
    )
    assert payload["d"]["exc_info"] == REDACTED
    assert payload["d"]["host"] == "db-01"
    assert "hunter2" not in json.dumps(payload)


def test_a_top_level_extra_field_that_collides_with_a_shaped_key_gets_a_slot() -> None:
    """I1: the top-level extras loop used to have NO collision handling at
    all -- a plain `payload[shaped] = ...` -- while the nested path (M-8) did.
    Measured: `{200: "from-int-key", "200": "from-str-key"}` as a record's
    OWN attributes lost the first entry outright. Both loops now call the
    same `_shape_dict`, so this must behave exactly like the nested case."""
    record = _record(**{"200": "from-str-key"})
    # Deliberately illegal per the type system, and reachable in practice --
    # see the tuple-key test above for why this suppression is intent.
    record.__dict__[200] = "from-int-key"  # type: ignore[index]
    payload = json.loads(JsonFormatter().format(record))
    assert payload["200"] == "from-str-key", "the first-seen shaped key must survive"
    assert payload["200[dup-2]"] == "from-int-key", (
        "the colliding key must get a numbered slot, not silently vanish"
    )


# --- I-2: an exception constructor with required kwargs must still --------
# --- round-trip through pickle/deepcopy/Celery's own exception handling ---


def test_unsatisfiable_calendar_round_trips_through_pickle_and_celery() -> None:
    """I-2: `BaseException.__reduce__` defaults to `(cls, self.args, ...)`,
    and `self.args` is just `("bad calendar",)` -- the four required
    keyword-only attributes (`timezone`, `requested_minutes`, `start`,
    `outstanding_minutes`) are missing from that reconstruction call.
    Measured before this fix: `pickle.loads(pickle.dumps(exc))` and
    `copy.deepcopy(exc)` both raised `TypeError: __init__() missing 4
    required keyword-only arguments`, and -- the one that actually matters --
    `celery.utils.serialization.get_pickleable_exception(exc)` did NOT
    error: it silently substituted the nearest picklable base
    (`SlaValidationError`), so the exception TYPE was lost. §2 only lets a
    log line carry the type; losing it here is losing the only thing that
    was safe to keep."""
    import copy
    import pickle

    from celery.utils.serialization import get_pickleable_exception

    from app.modules.sla.errors import UnsatisfiableCalendar

    original = UnsatisfiableCalendar(
        "bad calendar",
        timezone="UTC",
        requested_minutes=30,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        outstanding_minutes=10,
    )

    def _assert_round_tripped(rebuilt: UnsatisfiableCalendar) -> None:
        assert type(rebuilt) is UnsatisfiableCalendar
        assert str(rebuilt) == "bad calendar"
        assert rebuilt.timezone == "UTC"
        assert rebuilt.requested_minutes == 30
        assert rebuilt.start == datetime(2026, 1, 1, tzinfo=UTC)
        assert rebuilt.outstanding_minutes == 10

    _assert_round_tripped(pickle.loads(pickle.dumps(original)))
    _assert_round_tripped(copy.deepcopy(original))
    _assert_round_tripped(get_pickleable_exception(original))


# Module level, deliberately: pickle resolves a class by `__module__` +
# `__qualname__`, and a class defined inside a test FUNCTION has a
# `<locals>` in its qualname that pickle cannot look up -- a local class
# would make `test_a_subclass_round_trips_as_itself_not_the_base` pass for
# the wrong reason (pickling the class reference would itself fail loudly,
# not silently downgrade).
class _NarrowerUnsatisfiableCalendar(UnsatisfiableCalendar):
    """A subclass with no state of its own -- exists only to prove
    `__reduce__` preserves `type(self)` rather than hardcoding the base."""


def test_a_subclass_round_trips_as_itself_not_the_base() -> None:
    """Finding 4.1: `__reduce__` used to return the module-level rebuilder
    with no reference to `type(self)` at all, so ANY subclass -- pickled,
    deepcopy'd, or handed to Celery's own exception machinery -- silently
    reconstructed as the plain base `UnsatisfiableCalendar`, one level below
    where I-2 (fix wave 7) already fixed this for the base class itself.
    Before this fix, `type(Narrower(...))` was `UnsatisfiableCalendar` after
    every one of the three round trips below -- succeeding quietly, which is
    worse than I-2's original `TypeError`: a caller checking
    `isinstance(exc, Narrower)` after a round trip would get `False` with no
    signal anything was lost. `_assert_round_tripped` in the test above
    would not have caught this -- `type(rebuilt) is UnsatisfiableCalendar`
    is exactly what a downgraded subclass also satisfies."""
    import copy
    import pickle

    from celery.utils.serialization import get_pickleable_exception

    original = _NarrowerUnsatisfiableCalendar(
        "narrow calendar",
        timezone="UTC",
        requested_minutes=15,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        outstanding_minutes=5,
    )

    for rebuilt in (
        pickle.loads(pickle.dumps(original)),
        copy.deepcopy(original),
        get_pickleable_exception(original),
    ):
        assert type(rebuilt) is _NarrowerUnsatisfiableCalendar, (
            f"a subclass downgraded to {type(rebuilt)} across a round trip -- "
            "__reduce__ must carry type(self) through, not just the base class"
        )
        assert rebuilt.timezone == "UTC"


def test_an_attribute_set_after_construction_survives_a_round_trip() -> None:
    """Finding 4.3: `BaseException.__reduce__`'s default form is a 3-tuple --
    `(cls, self.args, self.__dict__)` -- and the override this exception
    carries used to return only 2 elements, dropping the state slot
    entirely. Measured before this fix: an attribute assigned to the
    instance AFTER construction (not one of the four constructor keywords)
    was silently absent from the rebuilt object. Pickle applies the third
    element to the reconstructed object's `__dict__` after calling the
    rebuild callable, so restoring it must not depend on the rebuild
    function itself knowing about attributes it was never told to accept."""
    import copy
    import pickle

    original = UnsatisfiableCalendar(
        "bad calendar",
        timezone="UTC",
        requested_minutes=30,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        outstanding_minutes=10,
    )
    original.diagnostic_note = "attached after __init__, not a constructor argument"  # type: ignore[attr-defined]

    for rebuilt in (pickle.loads(pickle.dumps(original)), copy.deepcopy(original)):
        # `pickle.loads` returns `Any`; `copy.deepcopy` is generic and keeps
        # `UnsatisfiableCalendar`, so the loop variable's type is a union of
        # the two -- mypy flags the attribute access on the non-`Any` half as
        # `union-attr`, not `attr-defined`, even though it is the same
        # deliberate "attribute mypy has no static declaration for" case as
        # the assignment two lines up.
        assert rebuilt.diagnostic_note == (  # type: ignore[union-attr]
            "attached after __init__, not a constructor argument"
        ), "an attribute set after construction did not survive the round trip"


def test_unsatisfiable_calendar_round_trips_through_the_backends_own_serializer() -> None:
    """Finding 4.2: `get_pickleable_exception` (pinned above) is only called
    on Celery's PICKLE-family result serializers. `create_celery()`
    (`app/worker.py`) uses the default `result_serializer='json'`, and on
    that path `Backend.prepare_exception`/`exception_to_python` never call
    `get_pickleable_exception` at all -- they serialize `exc.args` (just
    `("bad calendar",)`, the four keyword-only attributes are never part of
    `args`) and reconstruct with `cls(*exc_message)`, i.e.
    `UnsatisfiableCalendar("bad calendar")` with no keywords. Measured before
    this fix: that call raised `TypeError` for the four missing required
    keyword-only arguments, `exception_to_python` caught it and returned a
    bare `Exception(f'{cls}({exc_msg})')` -- the type lost on the path
    production actually uses, the pickle-only pin above notwithstanding.
    Giving the four keyword-only parameters defaults is what makes
    `cls(*exc_message)` succeed; this is pinned against Celery's REAL
    `Backend.prepare_exception`/`exception_to_python`, not by asserting the
    defaults exist in isolation, because a default that does not actually
    make this call succeed would be a false fix."""
    from celery import Celery

    from app.modules.sla.errors import UnsatisfiableCalendar

    app = Celery("test-fixwave8", broker="redis://localhost:6379/9")
    assert app.backend.serializer == "json", (
        "this test must exercise the JSON path -- the one create_celery() actually uses"
    )

    original = UnsatisfiableCalendar(
        "bad calendar",
        timezone="UTC",
        requested_minutes=30,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        outstanding_minutes=10,
    )
    prepared = app.backend.prepare_exception(original)
    rebuilt = app.backend.exception_to_python(prepared)

    assert type(rebuilt) is UnsatisfiableCalendar, (
        f"the exception TYPE was lost on the JSON-serializer path -- got {type(rebuilt)}, "
        f"str: {rebuilt!s}"
    )
    assert str(rebuilt) == "bad calendar"


# --- I-1 (fix wave 9): the constructor defaults must not fabricate operator --
# --- data, and this is what actually proves it -- the log line, not just  ---
# --- the constructor in isolation ------------------------------------------


def test_unsatisfiable_calendar_defaults_are_none_not_fake_measurements() -> None:
    """I-1: fix wave 8 defaulted the four keyword-only parameters to
    `""`/`0`/an epoch `datetime`/`0` so Celery's JSON-serializer path
    (`cls(*exc_message)`, i.e. no keywords at all) could reconstruct the
    exception without raising -- but those defaults are INDISTINGUISHABLE
    from real measurements on the resulting object. Measured:
    `UnsatisfiableCalendar("forgot everything")` constructs cleanly with
    `requested_minutes=0`, `timezone=""` -- an operator reading
    `requested_minutes: 0` off a log line has no way to tell that from a
    genuine "asked for zero working minutes" measurement. `None` cannot be
    confused with a real value the working-time arithmetic in `calendar.py`
    would ever produce, so a raise site that forgets an argument (impossible
    today -- every real raise site names all four -- but this is what a
    RECONSTRUCTED exception looks like) now produces a visibly ABSENT value."""
    reconstructed = UnsatisfiableCalendar("forgot everything")
    assert reconstructed.timezone is None
    assert reconstructed.requested_minutes is None
    assert reconstructed.start is None
    assert reconstructed.outstanding_minutes is None


@contextmanager
def _capturing_sla_tracking_logger() -> Iterator[io.StringIO]:
    """Swap `its.sla.tracking`'s handler for one writing `JsonFormatter`
    output into a buffer, saving and restoring its prior handlers/propagate
    exactly as `_configured_logging_capturing_stdout` does for the process
    root -- this logger is module-global state too, and later tests must not
    inherit a buffer-bound handler from this one."""
    sla_logger = logging.getLogger("its.sla.tracking")
    prev_handlers = sla_logger.handlers[:]
    prev_propagate = sla_logger.propagate

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    sla_logger.handlers[:] = [handler]
    sla_logger.propagate = False
    try:
        yield stream
    finally:
        sla_logger.handlers[:] = prev_handlers
        sla_logger.propagate = prev_propagate


def test_sla_misconfiguration_log_line_omits_absent_fields_on_a_reconstructed_exception() -> None:
    """I-1: a RECONSTRUCTED `UnsatisfiableCalendar` (all four keyword-only
    attributes at their `None` default, exactly what Celery's JSON-serializer
    path produces) must log a line with all four fields ABSENT -- not
    present carrying `null`, `0`, `""`, or any other value indistinguishable
    from a genuine measurement. `sla_misconfiguration_is_not_fatal`
    (`app/modules/sla/tracking.py`) is the ONLY call site that reads these
    attributes onto a log line, so this is what actually proves I-1's fix
    rather than just the constructor defaults in isolation (the test above)."""
    # Importing `app.modules.sla.tracking` directly, before anything has
    # imported `app.models` as a whole, triggers a circular partial-init
    # through `app.modules.service_desk.models` -> `app.models.base` ->
    # `app.models`'s own `__init__` re-importing the still-mid-import
    # `service_desk.models` -- exactly what `tests/test_sla_tracking.py`
    # avoids by importing `app.models` first. Same fix here.
    import app.models  # noqa: F401
    from app.modules.sla.tracking import sla_misconfiguration_is_not_fatal

    async def drive() -> None:
        async with sla_misconfiguration_is_not_fatal("start_tracking", uuid4()):
            raise UnsatisfiableCalendar("forgot everything")

    with _capturing_sla_tracking_logger() as stream:
        asyncio.run(drive())

    payload = json.loads(stream.getvalue())
    fields = ("calendar_timezone", "requested_minutes", "calendar_start", "outstanding_minutes")
    for field in fields:
        assert field not in payload, (
            f"{field} was present (as {payload.get(field)!r}) on a reconstructed exception "
            "-- it must be OMITTED, not logged with a fake value"
        )


def test_sla_misconfiguration_log_line_carries_all_four_fields_when_properly_raised() -> None:
    """I-1 twin: a raise site that DOES name all four keyword-only attributes
    -- every real raise site in this codebase (`app/modules/sla/
    calendar.py`) -- must still see all four survive onto the log line
    unchanged; the fix omits ABSENT values, it must not turn a genuine
    zero/empty measurement into an absent one."""
    # Importing `app.modules.sla.tracking` directly, before anything has
    # imported `app.models` as a whole, triggers a circular partial-init
    # through `app.modules.service_desk.models` -> `app.models.base` ->
    # `app.models`'s own `__init__` re-importing the still-mid-import
    # `service_desk.models` -- exactly what `tests/test_sla_tracking.py`
    # avoids by importing `app.models` first. Same fix here.
    import app.models  # noqa: F401
    from app.modules.sla.tracking import sla_misconfiguration_is_not_fatal

    start = datetime(2026, 1, 1, tzinfo=UTC)

    async def drive() -> None:
        async with sla_misconfiguration_is_not_fatal("start_tracking", uuid4()):
            raise UnsatisfiableCalendar(
                "bad calendar",
                timezone="UTC",
                requested_minutes=30,
                start=start,
                outstanding_minutes=10,
            )

    with _capturing_sla_tracking_logger() as stream:
        asyncio.run(drive())

    payload = json.loads(stream.getvalue())
    assert payload["calendar_timezone"] == "UTC"
    assert payload["requested_minutes"] == 30
    assert payload["calendar_start"] == str(start)
    assert payload["outstanding_minutes"] == 10


def test_the_access_log_filter_redacts_an_install_link_code() -> None:
    """uvicorn renders the request line from %-args, outside every `extra=`
    redaction; the install link's code is IN the path by design, so the
    filter rewrites the path argument itself."""
    from app.core.logging import InstallLinkPathRedactor

    code = "AbCdEfGhIjKlMnOpQrStUvWxYz012-_9"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.50.0.9:5000", "GET", f"/i/{code}/install.sh", "1.1", 200),
        None,
    )
    assert InstallLinkPathRedactor().filter(record) is True
    rendered = record.getMessage()
    assert code not in rendered
    assert '"GET /i/[redacted]/install.sh HTTP/1.1" 200' in rendered

    other = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.50.0.9:5000", "GET", "/api/v1/devices", "1.1", 200), None,
    )
    InstallLinkPathRedactor().filter(other)
    assert "/api/v1/devices" in other.getMessage()

    sig = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.50.0.9:5000", "GET", f"/i/{code}/windows.sig?arch=arm64", "1.1", 200), None,
    )
    InstallLinkPathRedactor().filter(sig)
    assert code not in sig.getMessage()
    assert "/i/[redacted]/windows.sig?arch=arm64" in sig.getMessage()


def test_configure_logging_installs_the_access_log_redactor_once() -> None:
    from app.core.logging import InstallLinkPathRedactor, configure_logging

    root = logging.getLogger()
    access = logging.getLogger("uvicorn.access")
    before = (list(root.handlers), root.level, list(access.filters))
    access.filters[:] = [f for f in access.filters if not isinstance(f, InstallLinkPathRedactor)]
    try:
        configure_logging()
        configure_logging()
        installed = [f for f in access.filters if isinstance(f, InstallLinkPathRedactor)]
        assert len(installed) == 1
    finally:
        root.handlers[:] = before[0]
        root.setLevel(before[1])
        access.filters[:] = before[2]

