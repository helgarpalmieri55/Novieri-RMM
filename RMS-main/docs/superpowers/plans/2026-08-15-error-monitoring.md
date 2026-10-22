# Error Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Count, group and surface failures across all three processes — carrying the exception type, raise site and correlation ids, and never its text.

**Architecture:** `capture_exception(exc, *, where)` in `app/core/errors/` fingerprints by raise site, reads the correlation contextvars, and pushes onto a bounded queue that a background drain writes to `platform_error_group` / `platform_error_event`. Capture hangs off seams that already handle failure. A gated API and a UI page surface groups.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Alembic, Celery, React + TypeScript (strict).

**Spec:** `docs/superpowers/specs/2026-08-15-error-monitoring-design.md`

## Global Constraints

- **No exception text, anywhere.** Not in a record, not in an API response, not in the UI. `exc_type` is `type(exc).__name__` and must pass the same shape gate `app/core/logging.py` applies to type names — a dynamically-named class is a measured attack here, not a hypothetical.
- **`capture_exception` must never raise.** A monitoring failure must not become the outage. Swallow, log with `exc_info`, continue — the pattern the heartbeat writers already use.
- **Never log an exception as a `%`-arg.** `tests/test_logging_discipline.py` flags `str(exc)`, `repr(exc)`, f-strings and `extra=` values, and asserts an exact log-call-site count (currently 64) whose failure message requires spec line 13 of `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` to move in the same commit.
- **`app/` never imports from `tests/`.** `tests` is absent from the deployed image (`packages = ["app"]`); `tests/test_no_tests_import_in_app.py` enforces it. That defect shipped through a green 6087-test suite.
- **Gate routes with the parameter form** — `ctx: AuthContext = Depends(require_permission("..."))` from `app/api/deps.py`. `route_permission()` matches the closure qualname and `tests/test_route_gating.py` sweeps the whole router; the decorator form risks reading as ungated.
- **Both new tables are platform-scoped.** RLS enabled + forced, one `app_user` policy, no tenant policy. `org_id` is a nullable correlation column, never the tenancy key.
- Imports at the top of files. `ruff check app tests` and `mypy app` clean. `asyncio_mode = "auto"` — async tests take no marker. Alembic head is **0055**; verify before writing a migration.
- Postgres and Redis die on every container restart. Restart with `setsid nohup pg_ctlcluster 16 main start` and `setsid nohup redis-server --port 6379` before any DB-touching task.

---

## File Structure

**Create**
- `backend/app/core/errors/__init__.py` — public surface
- `backend/app/core/errors/capture.py` — `CapturedError`, `fingerprint_of`, `capture_exception`, the bounded queue and drop counter
- `backend/app/core/errors/store.py` — the drain: upsert group, insert event
- `backend/app/models/platform_error.py` — `PlatformErrorGroup`, `PlatformErrorEvent`
- `backend/migrations/versions/0056_platform_errors.py`
- `backend/app/api/v1/errors.py` — list, detail, resolve
- `backend/tests/test_error_capture.py`
- `backend/tests/test_error_api.py`
- `frontend/src/modules/error-monitor/{types.ts,api.ts,ErrorMonitorPage.tsx,ErrorMonitorPage.test.tsx}`

**Modify**
- `backend/app/api/request_context.py` — capture at the existing except
- `backend/app/worker.py` — `task_failure` signal + `_OrgCycleFailures.contain`
- `backend/app/gateway/main.py` — gateway capture + asyncio handler
- `backend/app/main.py` — asyncio exception handler, drain task
- `backend/app/services/cert_renewal.py` — audit metadata carries the type, not `str(exc)`
- `backend/app/core/rbac.py` — two new permission codes
- `backend/tests/test_tenant_column_sweep.py` — two new named exceptions
- `frontend/src/App.tsx` — route the page

---

## Task 1: Capture and fingerprinting

**Files:** Create `app/core/errors/{__init__,capture}.py`, `tests/test_error_capture.py`

**Interfaces:**
- Produces: `CapturedError` (frozen slots dataclass, fields exactly as spec §2), `fingerprint_of(exc, tb) -> str` (16 hex chars), `capture_exception(exc, *, where) -> CapturedError | None`, `drained() -> list[CapturedError]`, `dropped_count() -> int`, `QUEUE_MAX = 1000`.
- Consumes: the contextvars in `app/core/logging.py` (`request_id_var`, `org_id_var`, `user_id_var`, `job_id_var`, `device_id_var`) and its type-name shape gate.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_error_capture.py
"""Capture: what a record carries, and above all what it does not.

NO DATABASE -- capture is pure; Task 2 covers the drain.
"""

import app.core.errors.capture as capture_mod
from app.core.errors.capture import (
    QUEUE_MAX,
    capture_exception,
    drained,
    dropped_count,
    fingerprint_of,
)
from app.core.logging import org_id_var, request_id_var


class _LeakyErr(Exception):
    """Its str() is a DSN with a password -- the shape this project's two real
    leaks had."""

    def __str__(self) -> str:
        return "could not connect: postgres://its:S3CRET-PW@db.internal/itsuite"


def _raise_leaky() -> Exception:
    try:
        raise _LeakyErr()
    except _LeakyErr as exc:
        return exc


def test_a_captured_record_carries_no_exception_text() -> None:
    """THE INVARIANT (spec §0.1). An error monitor that stores tracebacks
    undoes two sub-projects of work, in a durable store, on purpose."""
    captured = capture_exception(_raise_leaky(), where="api")
    assert captured is not None
    rendered = repr(captured)
    assert "S3CRET-PW" not in rendered
    assert "postgres://" not in rendered
    assert "could not connect" not in rendered
    assert captured.exc_type == "_LeakyErr"


def test_two_raises_from_the_same_line_share_a_fingerprint() -> None:
    a = capture_exception(_raise_leaky(), where="api")
    b = capture_exception(_raise_leaky(), where="api")
    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint


def test_raises_from_different_lines_do_not_share_a_fingerprint() -> None:
    def _other() -> Exception:
        try:
            raise _LeakyErr()
        except _LeakyErr as exc:
            return exc

    a = capture_exception(_raise_leaky(), where="api")
    b = capture_exception(_other(), where="api")
    assert a is not None and b is not None
    assert a.fingerprint != b.fingerprint


def test_a_hostile_type_name_does_not_reach_the_record_verbatim() -> None:
    """Measured in sub-project 1: `type(name, (Exception,), {})` puts arbitrary
    text where a type name is expected, and the type name is the ONE string
    this design lets through."""
    Hostile = type("postgres://u:hunter2@h/db", (Exception,), {})
    try:
        raise Hostile()
    except Exception as exc:  # noqa: BLE001
        captured = capture_exception(exc, where="api")
    assert captured is not None
    assert "hunter2" not in repr(captured)


def test_correlation_ids_are_populated_from_the_contextvars() -> None:
    token_r = request_id_var.set("req-abc")
    token_o = org_id_var.set("org-1")
    try:
        captured = capture_exception(_raise_leaky(), where="api")
    finally:
        request_id_var.reset(token_r)
        org_id_var.reset(token_o)
    assert captured is not None
    assert captured.request_id == "req-abc"
    assert captured.org_id == "org-1"


def test_absent_correlation_ids_are_none_not_empty_string() -> None:
    """`""` and `None` mean different things in a filter, and only one of them
    is true outside a request."""
    captured = capture_exception(_raise_leaky(), where="worker")
    assert captured is not None
    assert captured.request_id is None
    assert captured.job_id is None


def test_capture_never_raises_even_when_recording_fails(monkeypatch) -> None:  # noqa: ANN001
    """A monitoring failure must not become the outage."""

    def _boom(_item: object) -> None:
        raise RuntimeError("queue is broken")

    monkeypatch.setattr(capture_mod, "_enqueue", _boom)
    assert capture_exception(_raise_leaky(), where="api") is None  # no raise


def test_queue_overflow_drops_and_counts() -> None:
    """A silently truncated error stream is worse than a visibly truncated
    one: the count tells an operator the numbers are floors."""
    drained()  # start empty
    before = dropped_count()
    for _ in range(QUEUE_MAX + 25):
        capture_exception(_raise_leaky(), where="api")
    assert dropped_count() - before == 25
    assert len(drained()) == QUEUE_MAX
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/pytest tests/test_error_capture.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.core.errors'`

- [ ] **Step 3: Implement `capture.py`**

Write `CapturedError` exactly as spec §2. `fingerprint_of` walks to the
**last** frame of the traceback (the raise site, not the catch site) and
hashes `f"{exc_type}|{module}|{lineno}|{qualname}"` with sha256, truncated to
16 hex characters. *(This paragraph is version 1 of the rule and is left as
the record of what this cycle built. It was SUPERSEDED on 2026-08-22 by spec
§2.2 — the deepest `app.*` frame, with the version hashed into the digest —
because the last frame is the library's when a library raises. Read §2.2, not
this, before touching `fingerprint_of`.)* The type name goes through
`app.core.logging._shaped_type_name` (or whatever that helper is actually
called — read the module) so a hostile dynamic name cannot pass through.

`capture_exception` reads the five contextvars, builds the record, calls
`_enqueue`, and wraps the whole body in `try/except Exception` that logs with
`exc_info` and returns `None`. `_enqueue` appends to a module-level
`collections.deque(maxlen=None)` guarded by a lock, incrementing
`_dropped` and returning without appending when `len(queue) >= QUEUE_MAX`.

> **Implementer note:** do not use `deque(maxlen=QUEUE_MAX)` — it silently
> evicts the OLDEST entry, which loses the first occurrence of a failure (the
> most diagnostically valuable one) and makes the drop invisible. Drop the
> NEWEST and count it.

- [ ] **Step 4: Run to verify pass, then lint, type-check and commit**

```bash
cd backend && .venv/bin/pytest tests/test_error_capture.py -q
.venv/bin/ruff check app tests && .venv/bin/mypy app
git add backend/app/core/errors backend/tests/test_error_capture.py
git commit -m "feat(errors): capture the type, the raise site and the correlation ids -- never the text"
```

---

## Task 2: Storage, migration, and the drain

**Files:** Create `app/models/platform_error.py`, `app/core/errors/store.py`, `migrations/versions/0056_platform_errors.py`; modify `tests/test_tenant_column_sweep.py`

**Interfaces:**
- Produces: `PlatformErrorGroup`, `PlatformErrorEvent`, `async drain_once(session) -> int`, `async prune_error_events(session, *, now=None) -> int`, `EVENT_RETENTION_DAYS = 30`.

- [ ] **Step 1: Write the models and migration**

Models per spec §4. The migration follows `0055_platform_health_sample.py`
exactly for RLS: `ENABLE` + `FORCE ROW LEVEL SECURITY`, one
`CREATE POLICY application_only ... FOR ALL TO app_user USING (true) WITH CHECK (true)`,
and `GRANT SELECT, INSERT, UPDATE, DELETE` to `app_user` — **`UPDATE` matters
here**, unlike the samples table, because the group's `last_seen`,
`event_count` and `resolved_at` are updated in place.

Set `down_revision` to the real current head — check `migrations/versions/`,
do not trust this plan's number.

- [ ] **Step 2: Extend the tenant sweep, with reasons**

In `tests/test_tenant_column_sweep.py`:

```python
TENANTLESS_BY_DESIGN = frozenset(
    {
        # Postgres being unreachable is not any organisation's fact.
        "platform_health_sample",
        # A code path being broken is platform information, and grouping is
        # across orgs by construction -- the whole point is "this line has
        # failed 4,000 times", which is not one org's property.
        "platform_error_group",
        # Carries `org_id` as a nullable CORRELATION column, not as a tenancy
        # key: an error at process startup belongs to no org. Access is by the
        # platform-wide `platform.errors.read` code, not by tenant scope.
        "platform_error_event",
    }
)
```

The sweep must still fail for a fourth, unnamed table — that is the property
worth having, and Task 2 is done only when a planted tenant-less model turns
it red.

- [ ] **Step 3: The drain**

`drain_once(session)`: pop everything the queue holds, group by fingerprint,
`INSERT ... ON CONFLICT (fingerprint) DO UPDATE` the group (bump `last_seen`,
add to `event_count`), then bulk-insert the events. One transaction per drain,
not per error — that is the point of the queue.

- [ ] **Step 4: Verify RLS denies, for real**

Run the migration, then connect as a role that is not `app_user` and attempt a
read of both tables. Put the actual command and its actual output in your
report. Then grant that role a raw `SELECT` and confirm it still reads zero
rows — that is what separates the RLS policy from the missing privilege, and
it is how Task 7 of the previous sub-project caught its own vacuity.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(errors): group and event tables, RLS deny-all plus one grant"
```

---

## Task 3: Wire capture into the three processes

**Files:** modify `app/api/request_context.py`, `app/worker.py`, `app/gateway/main.py`, `app/main.py`

- [ ] **Step 1: API**

In `app/api/request_context.py`'s existing `except Exception:` block — which
already logs and re-raises, and runs INSIDE the contextvars — add
`capture_exception(exc, where="api")` before the `raise`. Bind the exception
with `as exc`; the block currently uses a bare `except`.

- [ ] **Step 2: Worker**

Connect Celery's `task_failure` signal in `create_celery`, with
**`weak=False`** — a receiver held only weakly is collected the moment the
enclosing function returns, and this project has already lost a fix to exactly
that (see the `setup_logging` receiver's comment). Also call
`capture_exception(exc, where="worker")` from `_OrgCycleFailures.contain`,
which already receives every contained per-org failure.

- [ ] **Step 3: Gateway and the asyncio handler**

In `app/gateway/main.py`'s `run_gateway`, and in `app/main.py`'s startup,
install an `asyncio` exception handler via
`loop.set_exception_handler(...)` that captures and logs a **static** line.

This closes spec §6's first follow-up: `asyncio`'s default handler `repr()`s
every context value and `Future.__repr__` embeds `repr(exc)`, so exception
text the logging kernel refuses to print via `exc_info` is printed anyway,
pre-rendered into `msg`. Replacing the handler is what stops that.

Pin it: create a task that raises an exception whose `repr()` carries a
secret, never retrieve its result, force garbage collection, and assert the
secret does not appear in the captured log output while the error IS captured.

- [ ] **Step 4: Drain task**

A background task in the API (alongside the heartbeat) and a Celery beat entry
for the worker, both calling `drain_once` every few seconds. If the beat
count changes, `tests/test_worker_beat_expiry.py` may assert an entry count.

- [ ] **Step 5: Commit**

---

## Task 4: `cert_renewal`'s audit metadata

**Files:** modify `app/services/cert_renewal.py`

- [ ] **Step 1: Replace the text with the type**

`app/services/cert_renewal.py:283` currently writes:

```python
metadata={"renew_job_id": str(renew_job.id), "reason": str(exc)},
```

into the **audit** record — which is served through the audit API. Same leak
shape as the log sink, different destination. Replace `str(exc)` with
`type(exc).__name__`, keeping the key so the audit entry still names what
failed.

- [ ] **Step 2: Pin it**

A test that the audit metadata for a failed CSR signing carries the exception
type and not its text, using an exception whose `str()` is a DSN. Find the
existing cert-renewal tests and add it there.

- [ ] **Step 3: Commit**

---

## Task 5: The API

**Files:** Create `app/api/v1/errors.py`, `tests/test_error_api.py`; modify `app/core/rbac.py`, `app/main.py`, and add a migration seeding the two permission codes

Routes and gating exactly as spec §5. Two codes: `platform.errors.read` and
`platform.errors.write`. The permission migration follows
`0054_platform_status_permission.py` — seed the code, grant to `org-admin`
(`tests/test_rbac.py::test_org_admin_has_all_permissions` asserts an
org-admin's codes equal `set(PERMISSIONS)`).

Responses must contain no exception text — assert that directly, with a group
whose captured error came from a DSN-bearing raise.

---

## Task 6: The page

**Files:** Create `frontend/src/modules/error-monitor/*`; modify `frontend/src/App.tsx`

Read the real response model from `app/api/v1/errors.py` rather than trusting
this plan's TypeScript. Follow the house pattern: `ApiClient` method in
`frontend/src/lib/api.ts`, a query hook in the module's `api.ts`.

Assertions in the test: a group renders `exc_type` and `module:line`; the
resolve control calls the write endpoint; the dropped-count is displayed when
non-zero. **Assert structurally, not by regex over concatenated text** — a
`\bword\b` regex over joined DOM text is a false-green generator, measured in
the previous sub-project.

`App.routing.test.tsx` requires the shell to import the module.

---

## Task 7: End-to-end falsification

- [ ] **Step 1: Full backend suite** — `cd backend && .venv/bin/pytest tests/ -q -p no:randomly`, with Postgres and Redis running.

- [ ] **Step 2: Prove text cannot reach the store.** Raise a real exception whose `str()` is a DSN with a password through the API's capture seam, drain it to a real Postgres, and `SELECT` every column of both tables. Grep the entire result for the password. Put the actual SQL and its actual output in the commit message.

- [ ] **Step 3: Prove capture cannot break a request.** With the drain deliberately broken (patch `drain_once` to raise), confirm a request that raises still returns its 500 and the process stays up.

- [ ] **Step 4: Check the leak was possible before claiming it was closed.** Before asserting "no password in the store", confirm the raw exception you used genuinely contains the password. An input that never carried the secret cannot demonstrate the secret was removed — two of the previous sub-project's redaction checks were vacuous for exactly this reason.
