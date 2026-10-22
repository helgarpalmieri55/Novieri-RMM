# Logging Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the platform structured JSON logs with correlation IDs and name-based secret redaction, enforced by tests rather than by review habit.

**Architecture:** One shared secret-name predicate promoted out of the AI package into the kernel, so the AI gate and the log sink cannot drift. A stdlib `dictConfig` installs a `JsonFormatter` on the ROOT logger — which is what lets uvicorn's own lines through the same redaction. Correlation IDs live in `contextvars`, set by an HTTP middleware that follows the existing `security_headers_middleware` pattern.

**Tech Stack:** Python 3.12, stdlib `logging` (no new dependency), `contextvars`, FastAPI/Starlette middleware, `ast` for the discipline test.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` (commit `0d31d81`). Authoritative — raise disagreements rather than resolving them.
- **No new dependency.** stdlib `logging`, not `structlog`. Spec §4.
- **These logs are NOT the audit log.** Do not touch `app/core/audit.py`. Do not route any audit event through the logger. Spec §0.1.
- **`scrub.py`'s existing tests must pass UNTOUCHED.** That is what proves the promotion was a move, not a rewrite. Do not edit `backend/tests/test_ai_admin_scrub.py`. Spec §1.1.
- **Redaction is by field NAME only.** `%`-args are outside the gate and that is deliberate and pinned by a test. Do not "improve" this by scanning message text. Spec §1.2.
- Type hints mandatory. `ruff check app tests` and `mypy app` must pass — those are the exact commands CI runs.
- Backend commands run from `/home/user/Toeshee-RMS/backend` using `.venv/bin/python`.
- **Postgres is unavailable in this container.** Tests needing the `api_env` fixture cannot run here. Every test in this plan is designed to run without it except where noted in Task 4; report honestly which you ran.
- Never `git add -A`, `git add .`, `git commit -a`, `git checkout .`, or `git stash`. Stage named paths only.
- No model identifier and no `Co-Authored-By` trailer in commits.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/core/secrets.py` | **New.** The secret-name predicate and its data, moved verbatim from `scrub.py`. Zero imports from `app.core.ai`. |
| `backend/app/core/ai/scrub.py` | **Modify.** Delete the moved machinery; import and re-export `looks_like_secret_key` so every existing import path keeps working. |
| `backend/app/core/logging.py` | **New.** `JsonFormatter`, the contextvars, `bind()`, `configure_logging()`. No FastAPI import — this is importable by workers and scripts. |
| `backend/app/api/request_context.py` | **New.** `request_context_middleware`, matching `security_headers.py`'s shape. |
| `backend/app/main.py` | **Modify.** Call `configure_logging()` first thing in `create_app`; register the middleware beside `security_headers_middleware`. |
| `backend/tests/test_logging_kernel.py` | **New.** Spec §5 tests 1–3, 7, 9, 11. |
| `backend/tests/test_logging_discipline.py` | **New.** Spec §5 tests 4–6 (the AST sweep). |

---

### Task 1: Promote the secret predicate to the kernel

**Files:**
- Create: `backend/app/core/secrets.py`
- Modify: `backend/app/core/ai/scrub.py`

**Interfaces:**
- Produces, used by Tasks 2 and 3: `looks_like_secret_key(key: str) -> bool`, importable from **both** `app.core.secrets` and `app.core.ai.scrub`.

**Read first:** `backend/app/core/ai/scrub.py` lines 110–265. The machinery to move is `_INVISIBLE`, `_WORD`, `_FLAT_WORD`, `_SECRET_WORDS`, `_SECRET_WORD_PAIRS`, `_decompositions`, and `looks_like_secret_key`, plus their `re`/`unicodedata` imports. **Move the doc comments with them verbatim** — they record measurements (35 of 48 names missed by an underscore-only matcher; `sEcReT` defeating the case-split) that must not be lost.

- [ ] **Step 1: Create `app/core/secrets.py`**

Move the seven names listed above into a new module. Header:

```python
"""Secret-name detection, shared by every gate that must not leak one.

PROMOTED OUT OF `app/core/ai/scrub.py` (2026-08-14) so the AI tool gate and the
LOG SINK use one predicate rather than two that drift. `scrub.py` re-exports it,
so every existing import path still resolves.

NOTHING HERE IMPORTS `app.core.ai`. That is the point of the move: a kernel
module the logger can depend on without dragging the AI layer into every
process that writes a log line.
"""
```

Copy the bodies verbatim. Do not "clean up" the regexes or the word sets.

- [ ] **Step 2: Re-export from `scrub.py`**

Delete the moved definitions and add, near the other imports:

```python
from app.core.secrets import looks_like_secret_key

__all__ = ["Blocked", "Ok", "ScrubOutcome", "looks_like_secret_key", "scrub"]
```

Leave a note where the code used to be:

```python
# `looks_like_secret_key` and its word sets MOVED to `app/core/secrets.py`
# (2026-08-14) and are re-exported above. The log sink needs the same predicate
# and must not import the AI package to get it. Re-exported rather than
# relocated-and-updated-everywhere so the four test modules importing it from
# here keep working -- which is how the move is verified to be lossless.
```

- [ ] **Step 3: Prove the move was lossless**

```bash
.venv/bin/python -m pytest tests/test_ai_admin_scrub.py -q
.venv/bin/python -m pytest tests/test_ai_tool_surface.py tests/test_tool_surface_predicate.py -q
```
Expected: all pass, with **no edits to any test file**. If a test needed changing, the move was not lossless — stop and report.

- [ ] **Step 4: Confirm both import paths resolve**

```bash
.venv/bin/python -c "
from app.core.secrets import looks_like_secret_key as a
from app.core.ai.scrub import looks_like_secret_key as b
assert a is b, 'the re-export is a copy, not the same object'
assert a('api_key') and a('apiKey') and a('sEcReT') and not a('hostname')
print('both paths resolve to one object; predicate behaves')
"
```

- [ ] **Step 5: Verify no import cycle**

```bash
.venv/bin/python -c "import app.core.secrets; print('secrets imports clean')"
grep -n "app.core.ai" app/core/secrets.py || echo "no AI imports: correct"
```

- [ ] **Step 6: Lint, type-check, commit**

```bash
.venv/bin/python -m ruff check app tests && .venv/bin/python -m mypy app
git add app/core/secrets.py app/core/ai/scrub.py
git commit -m "refactor(core): promote the secret-name predicate to the kernel

The log sink needs the same matcher the AI tool gate uses, and must not
import the AI package to get it. Re-exported from scrub.py so all four
test modules importing it from there keep working untouched -- which is
what proves the move was lossless rather than a rewrite."
```

---

### Task 2: The JSON formatter and its redaction

**Files:**
- Create: `backend/app/core/logging.py`
- Test: `backend/tests/test_logging_kernel.py`

**Interfaces:**
- Consumes: `app.core.secrets.looks_like_secret_key`.
- Produces, used by Tasks 3 and 4:
  - `JsonFormatter()` — a `logging.Formatter` whose `format()` returns one JSON object per record
  - `request_id_var`, `org_id_var`, `user_id_var`, `job_id_var`, `device_id_var` — `ContextVar[str | None]`, all defaulting to `None`
  - `bind(**fields: str | None)` — a context manager setting the vars it names and restoring them on exit
  - `REDACTED = "[redacted]"`
  - `configure_logging(level: str = "INFO") -> None`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_logging_kernel.py`:

```python
"""The logging kernel: structure, redaction, correlation.

Spec: docs/superpowers/specs/2026-08-14-logging-kernel-design.md §5.
No database -- every test here drives the formatter and contextvars directly.
"""

import asyncio
import json
import logging

from app.core.logging import (
    REDACTED,
    JsonFormatter,
    bind,
    request_id_var,
)


def _record(msg: str = "hello", args: object = (), **extra: object) -> logging.LogRecord:
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_logging_kernel.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'app.core.logging'`.

- [ ] **Step 3: Implement `app/core/logging.py`**

```python
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
"""

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

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
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name, var in _CONTEXT_VARS.items():
            value = var.get()
            # Omitted rather than null: a line full of nulls is noise in every
            # log search.
            if value is not None:
                payload[name] = value
        for key, value in vars(record).items():
            if key in _RESERVED:
                continue
            payload[key] = REDACTED if looks_like_secret_key(key) else value
        if record.exc_info:
            # TYPE NAME ONLY, NEVER str(exc). An exception's own message is
            # where this project's two real credential leaks lived: a presigned
            # URL inside a url.Error, a hostname inside a DNSError.
            exc_type = record.exc_info[0]
            payload["exc_type"] = exc_type.__name__ if exc_type else "unknown"
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the ROOT logger, once.

    ROOT, deliberately: uvicorn's `uvicorn.access` and `uvicorn.error` loggers
    propagate here, so their lines get the same formatting and the same
    redaction. A per-logger handler would leave framework output unredacted.
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_logging_kernel.py -v`
Expected: 8 passed.

- [ ] **Step 5: Prove the redaction test can fail**

In `JsonFormatter.format`, change the extras loop body to `payload[key] = value`. Re-run: `test_a_secret_named_field_is_redacted` must FAIL. **Restore by editing the line back — never `git checkout`.** Re-run, confirm green, and paste both outputs into your report.

- [ ] **Step 6: Prove the over-redaction test can fail**

Change the same line to `payload[key] = REDACTED`. Re-run: `test_an_ordinary_field_survives_intact` must FAIL while test 2 still passes — that is the pair working as designed. **Restore by editing.** Paste both outputs.

- [ ] **Step 7: Lint, type-check, commit**

```bash
.venv/bin/python -m ruff check app tests && .venv/bin/python -m mypy app
git add app/core/logging.py tests/test_logging_kernel.py
git commit -m "feat(core): JSON log formatter with name-based redaction

Redacts any extra= field whose name matches the kernel's secret
predicate, and carries correlation contextvars onto every line.

%-args are NOT covered -- they render into the message where there is no
name to match -- and a test pins that gap so it cannot be misremembered
as coverage. Exceptions are logged by TYPE, never str(exc): an
exception's own message is where this project's two real credential
leaks lived."
```

---

### Task 3: Request context middleware, wired in

**Files:**
- Create: `backend/app/api/request_context.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_logging_kernel.py` (append)

**Interfaces:**
- Consumes: Task 2's `request_id_var`, `configure_logging`.
- Produces: `request_context_middleware(request, call_next) -> Response`.

**Read first:** `backend/app/api/security_headers.py` — match its shape exactly (a module-level async function, registered with `app.middleware("http")(...)`). `backend/app/main.py:45-47` is the registration site.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_logging_kernel.py`:

```python
def test_the_middleware_sets_a_request_id_and_echoes_it() -> None:
    """§5 test 7, middleware half. Uses a bare Starlette app rather than the
    real one so it needs no database."""
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from app.api.request_context import request_context_middleware

    seen: dict[str, str | None] = {}

    async def handler(_request: object) -> JSONResponse:
        seen["inside"] = request_id_var.get()
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/x", handler)])
    app.middleware("http")(request_context_middleware)

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
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from app.api.request_context import request_context_middleware

    async def handler(_request: object) -> JSONResponse:
        return JSONResponse({"id": request_id_var.get()})

    app = Starlette(routes=[Route("/x", handler)])
    app.middleware("http")(request_context_middleware)

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get("/x", headers={"X-Request-ID": "forged-by-caller"})

    response = asyncio.run(drive())
    assert response.json()["id"] != "forged-by-caller"
    assert response.headers["X-Request-ID"] != "forged-by-caller"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_logging_kernel.py -k middleware -q`
Expected: `ModuleNotFoundError: No module named 'app.api.request_context'`.

- [ ] **Step 3: Implement the middleware**

Create `backend/app/api/request_context.py`:

```python
"""Per-request correlation context.

Mirrors `security_headers.py`: a module-level async middleware registered in
`create_app`.

THE REQUEST ID IS ALWAYS MINTED HERE AND AN INBOUND `X-Request-ID` IS IGNORED
(spec §3). Honouring a caller-supplied id would let one org collide with
another's, or inject chosen values into an operator's log search. The minted id
is echoed on the response, so a client can still correlate what it sent.
"""

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import request_id_var

_HEADER = "X-Request-ID"


async def request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = uuid.uuid4().hex
    token = request_id_var.set(request_id)
    started = time.monotonic()
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers[_HEADER] = request_id
    # Duration in the response is useful and free; the access LINE is uvicorn's
    # job and already passes through the same formatter (see configure_logging).
    response.headers["X-Response-Time-Ms"] = f"{(time.monotonic() - started) * 1000:.1f}"
    return response
```

- [ ] **Step 4: Wire it into `create_app`**

In `backend/app/main.py`, add `configure_logging()` as the **first statement** of `create_app` (before `Settings` resolution, so configuration errors are themselves logged in JSON):

```python
def create_app(
    settings: Settings | None = None,
    *,
    jwks_http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    from app.core.logging import configure_logging

    configure_logging()
    settings = settings or get_settings()
```

And register the middleware beside the existing one:

```python
    from app.api.request_context import request_context_middleware
    from app.api.security_headers import security_headers_middleware

    app.middleware("http")(security_headers_middleware)
    app.middleware("http")(request_context_middleware)
```

- [ ] **Step 5: Run the tests**

```bash
.venv/bin/python -m pytest tests/test_logging_kernel.py -q
.venv/bin/python -m pytest tests/test_ai_tool_surface.py tests/test_roadmap_status.py tests/test_route_gating.py -q
```
Expected: all pass. The second command matters because those three build the real app via `create_app` — they are the cheapest check that the wiring did not break startup.

- [ ] **Step 6: Verify uvicorn's loggers actually route through the formatter**

§5 test 8, and spec §7 names this as a falsifier of the whole library choice:

```bash
.venv/bin/python -c "
import logging, io, json, sys
sys.path.insert(0, '.')
from app.core.logging import configure_logging, JsonFormatter
configure_logging()
root = logging.getLogger()
assert len(root.handlers) == 1, root.handlers
assert isinstance(root.handlers[0].formatter, JsonFormatter)
for name in ('uvicorn', 'uvicorn.access', 'uvicorn.error'):
    lg = logging.getLogger(name)
    assert lg.propagate, f'{name} does not propagate to root'
    assert not lg.handlers, f'{name} keeps its own handler and escapes redaction'
buf = io.StringIO()
root.handlers[0].stream = buf
logging.getLogger('uvicorn.access').info('req done', extra={'api_key': 'leak'})
line = buf.getvalue().strip()
assert json.loads(line)['api_key'] == '[redacted]', line
print('uvicorn lines route through the formatter AND are redacted')
"
```
If this fails, **stop and report** — spec §7 says the library choice must be revisited.

- [ ] **Step 7: Lint, type-check, commit**

```bash
.venv/bin/python -m ruff check app tests && .venv/bin/python -m mypy app
git add app/api/request_context.py app/main.py tests/test_logging_kernel.py
git commit -m "feat(api): per-request correlation context, wired into create_app

Mints a server-side request_id and ignores any inbound X-Request-ID --
a caller-chosen id could collide with another org's or poison an
operator's log search. The minted id is echoed so clients can still
correlate.

configure_logging() runs first in create_app so uvicorn's own loggers
propagate to the root handler and get the same redaction."
```

---

### Task 4: The AST discipline sweep

**Files:**
- Create: `backend/tests/test_logging_discipline.py`

**Interfaces:**
- Consumes: nothing from earlier tasks — it reads source text.

- [ ] **Step 1: Write the test**

```python
"""LOG CALLS CARRY STATIC TEMPLATES. Enforced by AST, not grep.

Spec §2. An interpolated log message captures whatever is in scope, which is
how a credential reaches a log line without anyone choosing to put it there.
This project's two real leaks were both an exception's own text: a presigned
URL inside a url.Error, a hostname inside a DNSError -- each one `str(exc)`
away from an innocent-looking log call.

GREP CANNOT DO THIS. It misses calls split across lines -- which is how most of
this codebase's log sites are written -- and it flags matching text inside
comments and docstrings. The AST distinguishes all of that.

MEASURED BEFORE WRITING: all existing sites already comply -- zero f-strings,
zero .format() on a message, zero str(exc) in a log call. This test therefore
LOCKS IN practice the codebase already follows. It should be green on its first
run; if it is not, that is a finding rather than a migration.
"""

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
_LEVELS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})


def _is_log_call(node: ast.Call) -> bool:
    """`<something>.debug(...)` etc. Matched on the ATTRIBUTE name rather than
    the receiver, so `logger`, `log`, `self._log` and `logging.getLogger(x)`
    are all covered without a naming convention."""
    return isinstance(node.func, ast.Attribute) and node.func.attr in _LEVELS


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_log_call(node):
                continue
            where = f"{path.relative_to(APP.parent)}:{node.lineno}"
            if node.args:
                message = node.args[0]
                if not (isinstance(message, ast.Constant) and isinstance(message.value, str)):
                    found.append(f"{where}: message is not a string literal")
            for arg in node.args:
                if isinstance(arg, ast.JoinedStr):
                    found.append(f"{where}: f-string in a log call")
                if (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Name)
                    and arg.func.id == "str"
                ):
                    found.append(f"{where}: str(...) in a log call")
    return found


def test_every_log_call_uses_a_static_template() -> None:
    problems = _violations()
    assert not problems, (
        "log calls must carry a constant message and put values in extra=; "
        "exceptions are logged by type, never str(exc):\n  " + "\n  ".join(problems)
    )


def test_the_sweep_actually_finds_log_calls() -> None:
    """Without this, a broken matcher reports a perfectly disciplined codebase.
    Measured 2026-08-14: 62 call sites."""
    total = 0
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        total += sum(
            1 for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_log_call(n)
        )
    assert total >= 50, f"the matcher found only {total} log calls"
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_logging_discipline.py -q`
Expected: **2 passed**, immediately. If `test_every_log_call_uses_a_static_template` fails, spec §2.2's measurement was wrong — **report the violations, do not fix them silently**; the sub-project's size changes.

- [ ] **Step 3: Prove it catches each violation**

Append to any file under `app/core/` in turn, running the test after each and removing it before the next. Paste all three outputs.

```python
# (a) interpolated message
logger.info(f"planted {1}")
# (b) str() in a log call
logger.info("planted %s", str(Exception("x")))
# (c) .format() on the message
logger.info("planted {}".format(1))
```

Expected: (a) reports both "message is not a string literal" and "f-string in a log call"; (b) reports "str(...) in a log call"; (c) reports "message is not a string literal". **Remove each by editing.** Then confirm `git diff --stat` for that file is empty.

- [ ] **Step 4: Lint and commit**

```bash
.venv/bin/python -m ruff check tests/test_logging_discipline.py
git add tests/test_logging_discipline.py
git commit -m "test(logging): enforce static log templates by AST

An interpolated log message captures whatever is in scope, which is how
a credential reaches a log line without anyone choosing to put it there.
Both of this project's real leaks were an exception's own text, one
str(exc) away from an innocent log call.

AST rather than grep: grep misses multi-line calls -- how most sites
here are written -- and flags text in comments. All 62 existing sites
already comply, so this locks in practice rather than forcing a
migration."
```

---

## Self-Review

**Spec coverage.** §0.1 not-the-audit-log → Task 2's module docstring + Global Constraints. §1.1 promotion → Task 1, verified by Step 3's untouched-tests run. §1.2 name redaction + the `%`-arg hole → Task 2 Steps 1/3, tests 2, 3, 11. §1.3 rationale → Task 2 docstring. §2 static templates → Task 4. §2.1 AST not grep → Task 4 Step 1. §2.2 the measurement → Task 4 Step 2's expectation and Step 1's docstring. §3 correlation, no inbound trust → Task 3, both middleware tests. §4 stdout + stdlib + root logger → Task 2's `configure_logging`, Task 3 Step 6. §5 tests 1–11 → tests 1,2,3 (Task 2), 4,5,6 (Task 4), 7 (Tasks 2+3), 8 (Task 3 Step 6), 9 (Task 2), 10 (Task 1 Step 3), 11 (Task 2). §6 exclusions → nothing built for them. §7 falsifiers → Task 3 Step 6 stops on the uvicorn one; Task 4 Step 2 stops on the measurement one.

**Gap found and closed during review:** spec §5 test 8 (uvicorn lines through the formatter) had no owning step — it is the *deciding* reason for the library choice, so leaving it implicit was the worst place for a gap. It is now Task 3 Step 6, with an explicit stop-and-report.

**Second gap:** §3's contextvar-propagation falsifier (spec §7) is covered by Task 3 Step 1's `seen["inside"]` assertion — the handler reads the var the middleware set, which is exactly the middleware→route propagation §7 doubts.

**Placeholder scan:** none. Every step carries its code or its exact command.

**Type consistency:** `looks_like_secret_key(key: str) -> bool` used identically in Tasks 1 and 2. `request_id_var: ContextVar[str | None]` set in Task 3, read in Task 2's formatter and both Task 3 tests. `REDACTED` defined in Task 2, asserted in Task 2's tests and Task 3 Step 6. `bind(**fields: str | None)` defined and used only in Task 2.

---

## Completion criteria

1. `tests/test_logging_kernel.py` and `tests/test_logging_discipline.py` green, run locally — neither needs a database.
2. `tests/test_ai_admin_scrub.py` green **with no edits**, proving the promotion was lossless.
3. The three `create_app`-building suites (`test_ai_tool_surface`, `test_roadmap_status`, `test_route_gating`) still green.
4. Task 3 Step 6 passes: uvicorn's loggers propagate to the root handler and their `extra=` fields are redacted.
5. `ruff check app tests` and `mypy app` clean.
6. Every falsifier in Tasks 2 and 4 demonstrated, then restored, with `git diff --stat` empty for the planted files.
7. The full backend suite is **unverifiable in this container** (no Postgres superuser) — say so rather than implying it ran; CI must confirm.
