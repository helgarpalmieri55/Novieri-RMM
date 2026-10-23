# Platform Health and Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace an unconditional `{"status":"ok"}` with probes that measure real dependencies, a readiness signal that gates traffic on a named hard set, per-instance process heartbeats, 48h of sampled history, and an operator page.

**Architecture:** A probe registry in `app/core/health/` runs independent async probes concurrently under per-probe and total timeouts, caching results for 5s. Two unauthenticated root endpoints (`/livez`, `/readyz`) expose the minimum a load balancer needs; one RBAC-gated route (`/api/v1/platform/status`) exposes detail, history and the module registry. All three processes heartbeat into Redis under per-instance keys. A Celery beat task samples probes into `platform_health_sample` every 60s; a daily sweep prunes beyond 48h.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic, Celery, Redis, React + TypeScript (strict), Vite, TanStack Query.

**Spec:** `docs/superpowers/specs/2026-08-15-platform-health-design.md`

## Global Constraints

- **Reason codes are a static enum.** An exception's text never reaches a response body, the samples table, or the page. `/livez` and `/readyz` are unauthenticated and a connection failure is the likeliest thing they will report; a connection error's text carries the DSN.
- **The hard set is exactly `{"postgres.app", "redis"}`.** A test asserts its membership. Nothing else may fail readiness.
- **`/readyz` returns failing probe NAMES only** — no reason codes, no versions, no hostnames.
- **The JWKS probe must not perform network I/O.** `JwksCache` is hardened against unauthenticated refetch-amplification; a probe that fetched would expose that vector through a tokenless endpoint.
- **The object-store probe reuses one cached `ObjectStore`.** Its constructor performs bucket-ensure I/O.
- **The vault probe must not call `store`/`checkout`/`delete`/`seal_bytes`/`open_bytes`** — every one moves secret material. A sealed vault is `down`, not an exception.
- **Per-probe timeout 2s; whole-set budget 5s; result cache 5s.** A timeout is a result, never an exception.
- **`platform_health_sample` has no `org_id`.** RLS is ENABLED with deny-all plus one policy for `app_user`. It must remain the only table in the schema without a tenant column.
- Python 3.12, full type hints, `mypy` and `ruff` clean. Tests first. Migrations forward-only; never edit a shipped migration.
- Platform health is kernel infrastructure: it lives in `app/core/health/` and `app/api/`, **never** in `app/modules/`. `app/modules/health/` is device health and is not touched by this plan.

---

## File Structure

**Create**
- `backend/app/core/health/__init__.py` — public surface: `run_probes`, `HARD_PROBES`, types
- `backend/app/core/health/types.py` — `Status`, `ReasonCode`, `ProbeResult`, `Probe`
- `backend/app/core/health/registry.py` — registration, concurrent execution, timeouts, cache
- `backend/app/core/health/probes.py` — the eight probe implementations
- `backend/app/core/health/heartbeat.py` — writer + reader for per-instance Redis keys
- `backend/app/api/probes.py` — `/livez`, `/readyz`
- `backend/app/api/v1/platform.py` — `GET /api/v1/platform/status`
- `backend/app/models/platform_health.py` — `PlatformHealthSample`
- `backend/migrations/versions/0054_platform_status_permission.py`
- `backend/migrations/versions/0055_platform_health_sample.py`
- `backend/tests/test_platform_health_probes.py`
- `backend/tests/test_platform_health_endpoints.py`
- `backend/tests/test_platform_heartbeat.py`
- `backend/tests/test_platform_health_sampler.py`
- `backend/tests/test_tenant_column_sweep.py`
- `frontend/src/modules/platform-status/api.ts`
- `frontend/src/modules/platform-status/types.ts`
- `frontend/src/modules/platform-status/PlatformStatusPage.tsx`
- `frontend/src/modules/platform-status/PlatformStatusPage.test.tsx`

**Modify**
- `backend/app/main.py` — remove `/healthz`, mount probes + platform router, start API heartbeat
- `backend/app/core/vault.py` — add `health()` to the Protocol and both implementations
- `backend/app/core/rbac.py` — add `platform.status.read`
- `backend/app/worker.py` — sampler task, retention task, beat entries, worker heartbeat
- `backend/app/gateway/main.py` — gateway heartbeat
- `frontend/src/App.tsx` — route the page
- `deploy/docker-compose.yml` — healthchecks for api, gateway, worker, web

---

## Task 1: Probe types and the registry

**Files:**
- Create: `backend/app/core/health/types.py`, `backend/app/core/health/registry.py`, `backend/app/core/health/__init__.py`
- Test: `backend/tests/test_platform_health_probes.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Status` and `ReasonCode` (both `enum.StrEnum` — this repo's ruff config enforces `UP042`, and `JobStatus` in `app/models/core.py` is the precedent), `ProbeResult`, `Probe = Callable[[Any], Awaitable[ProbeResult]]`, and the class `ProbeRegistry` with `.register(name, fn, *, hard=False)`, `.run(state) -> list[ProbeResult]` and `.hard -> frozenset[str]`. Module constants `PROBE_TIMEOUT_S = 2.0`, `TOTAL_BUDGET_S = 5.0`, `CACHE_TTL_S = 5.0`.
- **`HARD_PROBES` is NOT produced here** — it is defined in Task 4 alongside the endpoints that act on it, because the set only means something where readiness is decided.
- Async tests take no `@pytest.mark.asyncio`: `pyproject.toml` sets `asyncio_mode = "auto"`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_platform_health_probes.py
"""The probe registry: concurrency, timeouts, caching, and fail-closed status.

NO DATABASE. Every probe here is a stub -- this file tests the machinery that
runs probes, not the probes themselves (Task 2/3 cover those).
"""

import asyncio

import pytest

from app.core.health.registry import ProbeRegistry
from app.core.health.types import ProbeResult, ReasonCode, Status


def _ok(name: str) -> ProbeResult:
    return ProbeResult(name=name, status=Status.OK, duration_ms=1, reason=None)


@pytest.mark.asyncio
async def test_a_probe_that_hangs_is_cut_off_and_reports_timeout() -> None:
    """A status endpoint that can hang takes the page down with the dependency
    it is describing. The timeout is a RESULT, not an exception."""
    registry = ProbeRegistry(probe_timeout_s=0.05, total_budget_s=1.0, cache_ttl_s=0)

    async def hangs(_state: object) -> ProbeResult:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    registry.register("slow", hangs)
    results = await registry.run(object())
    assert [r.status for r in results] == [Status.DOWN]
    assert results[0].reason is ReasonCode.TIMEOUT


@pytest.mark.asyncio
async def test_a_probe_that_raises_reports_down_and_never_its_exception_text() -> None:
    """THE INVARIANT. A connection error's text carries the DSN, and these
    endpoints are unauthenticated. The reason is a static code."""
    registry = ProbeRegistry(probe_timeout_s=1.0, total_budget_s=1.0, cache_ttl_s=0)

    async def leaks(_state: object) -> ProbeResult:
        raise RuntimeError("could not connect: postgres://its:S3CRET@db/itsuite")

    registry.register("leaky", leaks)
    results = await registry.run(object())
    assert results[0].status is Status.DOWN
    assert results[0].reason is ReasonCode.UNREACHABLE
    rendered = repr(results[0])
    assert "S3CRET" not in rendered
    assert "postgres://" not in rendered


@pytest.mark.asyncio
async def test_probes_run_concurrently_not_serially() -> None:
    """Eight probes at 2s each must not take 16s. Serial execution would blow
    the total budget with every dependency healthy."""
    registry = ProbeRegistry(probe_timeout_s=1.0, total_budget_s=2.0, cache_ttl_s=0)

    async def slow(name: str) -> ProbeResult:
        await asyncio.sleep(0.2)
        return _ok(name)

    for i in range(8):
        registry.register(f"p{i}", lambda _s, n=f"p{i}": slow(n))
    loop = asyncio.get_running_loop()
    start = loop.time()
    results = await registry.run(object())
    elapsed = loop.time() - start
    assert len(results) == 8
    assert elapsed < 1.0, f"probes ran serially: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_results_are_cached_so_a_refresh_storm_cannot_hammer_postgres() -> None:
    """A dashboard on a wall screen must not become a SELECT 1 generator
    against a database that may already be struggling."""
    calls = 0
    registry = ProbeRegistry(probe_timeout_s=1.0, total_budget_s=1.0, cache_ttl_s=60)

    async def counted(_state: object) -> ProbeResult:
        nonlocal calls
        calls += 1
        return _ok("counted")

    registry.register("counted", counted)
    state = object()
    for _ in range(5):
        await registry.run(state)
    assert calls == 1


@pytest.mark.asyncio
async def test_the_cache_expires() -> None:
    calls = 0
    registry = ProbeRegistry(probe_timeout_s=1.0, total_budget_s=1.0, cache_ttl_s=0.01)

    async def counted(_state: object) -> ProbeResult:
        nonlocal calls
        calls += 1
        return _ok("counted")

    registry.register("counted", counted)
    state = object()
    await registry.run(state)
    await asyncio.sleep(0.02)
    await registry.run(state)
    assert calls == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && .venv/bin/pytest tests/test_platform_health_probes.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.core.health'`

- [ ] **Step 3: Write the types**

```python
# backend/app/core/health/types.py
"""Probe vocabulary. The reason enum is the security-relevant part.

REASONS ARE CODES, NEVER TEXT. `/livez` and `/readyz` are unauthenticated by
necessity -- a Docker healthcheck cannot present a token, and a probe that
needed the auth stack could not report the auth stack being broken. The single
likeliest thing a probe will ever report is a connection failure, and a
connection error's `str()` carries the DSN, which carries a password. The
logging kernel (sub-project 1) spent four fix waves on exactly that class; this
enum is how that lesson is carried into the health surface.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Status(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class ReasonCode(str, Enum):
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    SEALED = "sealed"
    EMPTY_CACHE = "empty_cache"
    MISCONFIGURED = "misconfigured"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    status: Status
    duration_ms: int
    reason: ReasonCode | None = None
    # Measured scalars only -- counts, ages, booleans. Never an exception's
    # text, never a URL, never a hostname.
    detail: dict[str, str | int | bool] | None = None


Probe = Callable[[Any], Awaitable[ProbeResult]]
```

- [ ] **Step 4: Write the registry**

```python
# backend/app/core/health/registry.py
"""Runs probes concurrently, under a per-probe timeout and a total budget.

FAIL-CLOSED, TWICE OVER. A probe that raises becomes `DOWN`/`UNREACHABLE` --
its exception never propagates to the endpoint and its text never reaches the
result. A probe that hangs is cancelled at `probe_timeout_s` and becomes
`DOWN`/`TIMEOUT`. Neither can take the status endpoint down with the
dependency it exists to describe.
"""

import asyncio
import logging
import time
from typing import Any

from app.core.health.types import Probe, ProbeResult, ReasonCode, Status

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 2.0
TOTAL_BUDGET_S = 5.0
CACHE_TTL_S = 5.0


class ProbeRegistry:
    def __init__(
        self,
        *,
        probe_timeout_s: float = PROBE_TIMEOUT_S,
        total_budget_s: float = TOTAL_BUDGET_S,
        cache_ttl_s: float = CACHE_TTL_S,
    ) -> None:
        self._probes: dict[str, Probe] = {}
        self._hard: set[str] = set()
        self._probe_timeout_s = probe_timeout_s
        self._total_budget_s = total_budget_s
        self._cache_ttl_s = cache_ttl_s
        self._cached: list[ProbeResult] | None = None
        self._cached_at = float("-inf")

    def register(self, name: str, fn: Probe, *, hard: bool = False) -> None:
        self._probes[name] = fn
        if hard:
            self._hard.add(name)

    @property
    def hard(self) -> frozenset[str]:
        return frozenset(self._hard)

    async def _run_one(self, name: str, fn: Probe, state: Any) -> ProbeResult:
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            return await asyncio.wait_for(fn(state), timeout=self._probe_timeout_s)
        except TimeoutError:
            return ProbeResult(name, Status.DOWN, elapsed_ms(), ReasonCode.TIMEOUT)
        except Exception:
            # exc_info only: the logging kernel renders `exc_type` and never the
            # exception's text. The RESULT carries a code, never the text.
            logger.warning("health probe failed", extra={"probe": name}, exc_info=True)
            return ProbeResult(name, Status.DOWN, elapsed_ms(), ReasonCode.UNREACHABLE)

    async def run(self, state: Any) -> list[ProbeResult]:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self._cache_ttl_s:
            return self._cached
        tasks = [self._run_one(name, fn, state) for name, fn in self._probes.items()]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=self._total_budget_s
            )
        except TimeoutError:
            # The whole set blew the budget. Report it as data rather than
            # raising: an endpoint that 500s is less useful than one that says
            # which probes it could not finish.
            results = [
                ProbeResult(name, Status.DOWN, int(self._total_budget_s * 1000), ReasonCode.TIMEOUT)
                for name in self._probes
            ]
        self._cached = list(results)
        self._cached_at = now
        return self._cached
```

- [ ] **Step 5: Write the package surface**

```python
# backend/app/core/health/__init__.py
"""Platform health: probes for the control plane's own dependencies.

NOT `app/modules/health/`, which is DEVICE health (endpoint monitoring, alert
profiles, remediation). This is kernel infrastructure -- it reports on the
kernel every module sits on, so it does not register through
`modules.register_all`.
"""

from app.core.health.registry import (
    CACHE_TTL_S,
    PROBE_TIMEOUT_S,
    TOTAL_BUDGET_S,
    ProbeRegistry,
)
from app.core.health.types import Probe, ProbeResult, ReasonCode, Status

__all__ = [
    "CACHE_TTL_S",
    "PROBE_TIMEOUT_S",
    "TOTAL_BUDGET_S",
    "Probe",
    "ProbeRegistry",
    "ProbeResult",
    "ReasonCode",
    "Status",
]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && .venv/bin/pytest tests/test_platform_health_probes.py -q`
Expected: PASS (5 tests)

- [ ] **Step 7: Type-check, lint and commit**

```bash
cd backend && .venv/bin/mypy app/core/health/ && .venv/bin/ruff check app/core/health/ tests/test_platform_health_probes.py
git add backend/app/core/health/ backend/tests/test_platform_health_probes.py
git commit -m "feat(health): probe registry with per-probe timeout, total budget and cache"
```

---

## Task 2: The two hard probes (Postgres and Redis)

**Files:**
- Create: `backend/app/core/health/probes.py`
- Modify: `backend/tests/test_platform_health_probes.py` (append)

**Interfaces:**
- Consumes: `ProbeResult`, `Status`, `ReasonCode` from Task 1.
- Produces: `async probe_postgres_app(state) -> ProbeResult`, `async probe_postgres_admin(state) -> ProbeResult`, `async probe_redis(state) -> ProbeResult`. Each takes the FastAPI `app.state` object and reads `app_engine`, `admin_engine`, `redis` from it.

- [ ] **Step 1: Write the failing tests (append to the same file)**

```python
# --- Task 2: the hard probes ---------------------------------------------------

from app.core.health.probes import probe_postgres_app, probe_redis


class _FakeState:
    def __init__(self, **kw: object) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeConn:
    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc

    async def __aenter__(self) -> "_FakeConn":
        if self._exc:
            raise self._exc
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def execute(self, _stmt: object) -> object:
        return None


class _FakeEngine:
    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc

    def connect(self) -> _FakeConn:
        return _FakeConn(self._exc)


@pytest.mark.asyncio
async def test_postgres_probe_reports_ok_when_select_1_succeeds() -> None:
    result = await probe_postgres_app(_FakeState(app_engine=_FakeEngine()))
    assert result.status is Status.OK
    assert result.reason is None


@pytest.mark.asyncio
async def test_postgres_probe_reports_down_without_the_dsn() -> None:
    """The failure mode this whole design is careful about: a connection error
    whose text is the DSN, on an endpoint reachable without a token."""
    exc = OSError("could not connect to postgres://its:S3CRET@db.internal/itsuite")
    result = await probe_postgres_app(_FakeState(app_engine=_FakeEngine(exc)))
    assert result.status is Status.DOWN
    assert result.reason is ReasonCode.UNREACHABLE
    assert "S3CRET" not in repr(result)


@pytest.mark.asyncio
async def test_redis_probe_pings() -> None:
    class _R:
        async def ping(self) -> bool:
            return True

    result = await probe_redis(_FakeState(redis=_R()))
    assert result.status is Status.OK
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/pytest tests/test_platform_health_probes.py -q -k "postgres or redis"`
Expected: FAIL, `ImportError: cannot import name 'probe_postgres_app'`

- [ ] **Step 3: Implement**

```python
# backend/app/core/health/probes.py
"""The eight dependency probes.

Every probe returns a ProbeResult and raises nothing the registry has to
catch as a surprise -- the registry's catch-all is a backstop, not the design.
"""

import time
from typing import Any

from sqlalchemy import text

from app.core.health.types import ProbeResult, ReasonCode, Status


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def _probe_engine(name: str, engine: Any) -> ProbeResult:
    started = time.monotonic()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        # No exception text: an engine's connection error renders the DSN.
        return ProbeResult(name, Status.DOWN, _ms(started), ReasonCode.UNREACHABLE)
    return ProbeResult(name, Status.OK, _ms(started))


async def probe_postgres_app(state: Any) -> ProbeResult:
    """HARD. Nothing is servable without the application database."""
    return await _probe_engine("postgres.app", state.app_engine)


async def probe_postgres_admin(state: Any) -> ProbeResult:
    """NOT hard. The admin engine serves migrations and grant management; the
    API can serve requests without it."""
    return await _probe_engine("postgres.admin", state.admin_engine)


async def probe_redis(state: Any) -> ProbeResult:
    """HARD -- not for caching, but because it is the job engine's result bus.
    Without it the API would accept job dispatches whose results can never be
    delivered: accepting work it cannot complete, which is worse than refusing
    it."""
    started = time.monotonic()
    try:
        await state.redis.ping()
    except Exception:
        return ProbeResult("redis", Status.DOWN, _ms(started), ReasonCode.UNREACHABLE)
    return ProbeResult("redis", Status.OK, _ms(started))
```

- [ ] **Step 4: Run to verify pass**

Run: `cd backend && .venv/bin/pytest tests/test_platform_health_probes.py -q`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/health/probes.py backend/tests/test_platform_health_probes.py
git commit -m "feat(health): postgres and redis probes, the two that gate readiness"
```

---

## Task 3: The soft probes, and a vault health method that does not exist yet

**Files:**
- Modify: `backend/app/core/health/probes.py`, `backend/app/core/vault.py`, `backend/tests/test_platform_health_probes.py`

**Interfaces:**
- Consumes: Task 2's `_ms`, `ProbeResult`.
- Produces: `probe_objectstore`, `probe_vault`, `probe_jwks`, `probe_signing`, `probe_ca`; and on `VaultService`, `async def health(self) -> tuple[bool, ReasonCode | None]` returning `(healthy, reason)`.

- [ ] **Step 1: Write the failing tests (append)**

```python
# --- Task 3: the soft probes ---------------------------------------------------

from app.core.health.probes import probe_jwks, probe_objectstore, probe_vault


@pytest.mark.asyncio
async def test_the_jwks_probe_performs_no_network_io() -> None:
    """`JwksCache` is hardened against unauthenticated refetch-amplification:
    single-flight lock, minimum refetch interval, failed refetch never clears
    the key set. A probe that FETCHED would hand an attacker that exact vector
    through `/api/v1/platform/status` -- so the probe reads the cache and never
    touches the network.

    IF THIS TEST EVER FAILS, the probe has started fetching and the
    amplification guard has been defeated by the health surface."""

    class _Cache:
        def __init__(self) -> None:
            self._keys = {"kid-1": object()}
            self._last_refetch = 0.0
            self.fetches = 0

        async def _refetch(self) -> None:
            self.fetches += 1

    cache = _Cache()
    result = await probe_jwks(_FakeState(jwks=cache))
    assert cache.fetches == 0, "the JWKS probe performed network I/O"
    assert result.status is Status.OK
    assert result.detail is not None
    assert result.detail["keys"] == 1


@pytest.mark.asyncio
async def test_an_empty_jwks_cache_is_degraded_not_down() -> None:
    """An empty cache is indistinguishable from 'no token has been validated
    yet', which is the normal state of a freshly started process."""

    class _Empty:
        _keys: dict[str, object] = {}
        _last_refetch = 0.0

    result = await probe_jwks(_FakeState(jwks=_Empty()))
    assert result.status is Status.DEGRADED
    assert result.reason is ReasonCode.EMPTY_CACHE


@pytest.mark.asyncio
async def test_a_sealed_vault_is_down_with_a_sealed_reason_not_an_error() -> None:
    """A sealed vault is a normal operational state with a specific remedy.
    Reporting it as UNREACHABLE would lose that."""

    class _Sealed:
        async def health(self) -> tuple[bool, ReasonCode | None]:
            return False, ReasonCode.SEALED

    result = await probe_vault(_FakeState(vault=_Sealed()))
    assert result.status is Status.DOWN
    assert result.reason is ReasonCode.SEALED


@pytest.mark.asyncio
async def test_the_objectstore_is_constructed_at_most_once() -> None:
    """`ObjectStore.__init__` ensures the bucket exists -- that is S3 I/O, and
    it is why `app/main.py` builds it lazily. A probe that constructed per call
    would turn a page refresh into a bucket-ensure storm."""
    constructed = 0

    class _Store:
        def __init__(self, _settings: object) -> None:
            nonlocal constructed
            constructed += 1

        async def bucket_exists(self) -> bool:
            return True

    state = _FakeState(settings=object(), object_store=None)
    for _ in range(3):
        await probe_objectstore(state, factory=_Store)
    assert constructed == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/pytest tests/test_platform_health_probes.py -q -k "jwks or vault or objectstore"`
Expected: FAIL, `ImportError: cannot import name 'probe_jwks'`

- [ ] **Step 3: Add `health()` to the vault Protocol and both implementations**

In `backend/app/core/vault.py`, add to the `VaultService` Protocol (after `open_bytes`):

```python
    async def health(self) -> tuple[bool, "ReasonCode | None"]:
        """Is the vault usable, without moving any secret material?

        Every other method on this Protocol -- store, checkout, delete,
        seal_bytes, open_bytes -- moves a credential. A health probe must call
        none of them. This exists so the probe has something safe to call.
        """
        ...
```

On `OpenBaoVaultService`:

```python
    async def health(self) -> tuple[bool, ReasonCode | None]:
        """`GET /v1/sys/health`: initialised and seal state, no secret material.

        A SEALED vault reports (False, SEALED) rather than raising -- it is a
        normal operational state with a specific remedy (unseal), and an
        operator needs to see that rather than `unreachable`.
        """
        try:
            response = await self._client.get(f"{self._addr}/v1/sys/health")
        except Exception:
            return False, ReasonCode.UNREACHABLE
        if response.status_code == 503:
            return False, ReasonCode.SEALED
        if response.status_code == 501:
            return False, ReasonCode.MISCONFIGURED  # not initialised
        return response.status_code == 200, None
```

On the in-memory implementation:

```python
    async def health(self) -> tuple[bool, ReasonCode | None]:
        """In-memory: usable whenever the process is."""
        return True, None
```

Import `ReasonCode` from `app.core.health.types` at the top of `vault.py`.

- [ ] **Step 4: Implement the soft probes (append to `probes.py`)**

```python
# --- soft probes: reported as degraded, never gating readiness -----------------

_OBJECT_STORE_SINGLETON: Any = None


async def probe_objectstore(state: Any, *, factory: Any = None) -> ProbeResult:
    """Bucket-exists against a CACHED store (see the test for why)."""
    global _OBJECT_STORE_SINGLETON
    started = time.monotonic()
    if _OBJECT_STORE_SINGLETON is None:
        if factory is None:
            from app.core.storage import ObjectStore as factory  # noqa: N813
        try:
            _OBJECT_STORE_SINGLETON = factory(state.settings)
        except Exception:
            return ProbeResult(
                "objectstore", Status.DOWN, _ms(started), ReasonCode.MISCONFIGURED
            )
    try:
        await _OBJECT_STORE_SINGLETON.bucket_exists()
    except Exception:
        return ProbeResult("objectstore", Status.DOWN, _ms(started), ReasonCode.UNREACHABLE)
    return ProbeResult("objectstore", Status.OK, _ms(started))


async def probe_vault(state: Any) -> ProbeResult:
    started = time.monotonic()
    try:
        healthy, reason = await state.vault.health()
    except Exception:
        return ProbeResult("vault", Status.DOWN, _ms(started), ReasonCode.UNREACHABLE)
    if healthy:
        return ProbeResult("vault", Status.OK, _ms(started))
    return ProbeResult("vault", Status.DOWN, _ms(started), reason or ReasonCode.UNREACHABLE)


async def probe_jwks(state: Any) -> ProbeResult:
    """READS THE CACHE. NEVER FETCHES. See the test's docstring."""
    started = time.monotonic()
    keys = len(getattr(state.jwks, "_keys", {}))
    if keys == 0:
        return ProbeResult("jwks", Status.DEGRADED, _ms(started), ReasonCode.EMPTY_CACHE)
    return ProbeResult("jwks", Status.OK, _ms(started), detail={"keys": keys})


async def probe_signing(state: Any) -> ProbeResult:
    """Local key material only -- no network, no signing operation."""
    started = time.monotonic()
    if getattr(state, "signer", None) is None:
        return ProbeResult("signing", Status.DOWN, _ms(started), ReasonCode.MISCONFIGURED)
    return ProbeResult("signing", Status.OK, _ms(started))


async def probe_ca(state: Any) -> ProbeResult:
    started = time.monotonic()
    if getattr(state, "ca", None) is None:
        return ProbeResult("ca", Status.DOWN, _ms(started), ReasonCode.MISCONFIGURED)
    return ProbeResult("ca", Status.OK, _ms(started))
```

- [ ] **Step 5: Run to verify pass**

Run: `cd backend && .venv/bin/pytest tests/test_platform_health_probes.py -q && .venv/bin/pytest tests/ -q -k vault`
Expected: PASS; the existing vault tests must remain green (the Protocol gained a method; both implementations supply it).

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/health/probes.py backend/app/core/vault.py backend/tests/test_platform_health_probes.py
git commit -m "feat(health): object store, vault, jwks, signing and ca probes

The vault probe needed a health() the Protocol did not have: every method
VaultService exposes moves secret material, so a probe had nothing safe to
call. The JWKS probe reads the cache and never fetches -- JwksCache is
hardened against unauthenticated refetch-amplification and the probe is
reachable through a tokenless endpoint."
```

---

## Task 4: `/livez` and `/readyz`, and the hard set

**Files:**
- Create: `backend/app/api/probes.py`, `backend/tests/test_platform_health_endpoints.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: `ProbeRegistry`, all probes.
- Produces: `build_registry() -> ProbeRegistry` (registers all eight with `hard=` set correctly), `router` with `/livez` and `/readyz`. `HARD_PROBES = frozenset({"postgres.app", "redis"})`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_platform_health_endpoints.py
"""/livez and /readyz: what gates traffic, and what an unauthenticated caller
is allowed to learn.

NO DATABASE -- the app is built for route inspection and the registry is
replaced with stubs.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.probes import HARD_PROBES, router
from app.core.health.registry import ProbeRegistry
from app.core.health.types import ProbeResult, ReasonCode, Status


def _app(results: list[ProbeResult]) -> FastAPI:
    app = FastAPI()
    registry = ProbeRegistry(cache_ttl_s=0)
    for r in results:
        registry.register(r.name, (lambda _s, res=r: _immediate(res)), hard=r.name in HARD_PROBES)
    app.state.health_registry = registry
    app.include_router(router)
    return app


async def _immediate(result: ProbeResult) -> ProbeResult:
    return result


def _ok(name: str) -> ProbeResult:
    return ProbeResult(name, Status.OK, 1)


def _down(name: str, reason: ReasonCode = ReasonCode.UNREACHABLE) -> ProbeResult:
    return ProbeResult(name, Status.DOWN, 1, reason)


def test_the_hard_set_has_exactly_this_membership() -> None:
    """Adding to this set changes what takes the platform out of rotation.
    That should require deciding, not editing."""
    assert HARD_PROBES == frozenset({"postgres.app", "redis"})


def test_livez_is_200_with_every_dependency_down() -> None:
    """Liveness means the process can answer. If it cannot serve this, it
    cannot serve the response either -- which is the signal."""
    app = _app([_down("postgres.app"), _down("redis"), _down("vault")])
    assert TestClient(app).get("/livez").status_code == 200


@pytest.mark.parametrize("failing", ["postgres.app", "redis"])
def test_readyz_is_503_when_a_hard_probe_fails(failing: str) -> None:
    results = [_ok("postgres.app"), _ok("redis"), _ok("objectstore")]
    results = [_down(r.name) if r.name == failing else r for r in results]
    response = TestClient(_app(results)).get("/readyz")
    assert response.status_code == 503
    assert response.json()["failing"] == [failing]


def test_readyz_is_200_when_only_a_soft_probe_fails() -> None:
    """THE DEGRADED-NOT-DOWN PROPERTY. A MinIO outage costs backup artifacts
    and remote recordings; taking the control plane out of rotation for it
    would remove the operator's ability to diagnose it through the very API
    they need."""
    app = _app([_ok("postgres.app"), _ok("redis"), _down("objectstore")])
    assert TestClient(app).get("/readyz").status_code == 200


def test_readyz_leaks_no_reason_codes_to_an_unauthenticated_caller() -> None:
    """Names are enough for a load balancer to act. Reasons would let an
    unauthenticated caller map which backend is currently weak."""
    app = _app([_down("postgres.app", ReasonCode.AUTH_FAILED), _ok("redis")])
    body = TestClient(app).get("/readyz").text
    assert "postgres.app" in body
    assert "auth_failed" not in body
    assert "reason" not in body
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/pytest tests/test_platform_health_endpoints.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.api.probes'`

- [ ] **Step 3: Implement**

```python
# backend/app/api/probes.py
"""Unauthenticated liveness and readiness.

WHY UNAUTHENTICATED: a Docker healthcheck and traefik cannot present a token,
and a probe that needed the auth stack could not report the auth stack being
broken.

WHICH IS WHY THE BODY IS THIN. `/readyz` returns failing NAMES and nothing
else -- no reason codes, no versions, no hostnames. Enough for a load balancer
to act on; not enough for an anonymous caller to map the internal topology or
learn which backend is currently weak. The detail lives behind
`platform.status.read` (see `app/api/v1/platform.py`).
"""

from typing import Any

from fastapi import APIRouter, Request, Response, status

from app.core.health.probes import (
    probe_ca,
    probe_jwks,
    probe_objectstore,
    probe_postgres_admin,
    probe_postgres_app,
    probe_redis,
    probe_signing,
    probe_vault,
)
from app.core.health.registry import ProbeRegistry
from app.core.health.types import Status

# Postgres because nothing is servable without it. Redis because it is the job
# engine's result bus -- without it the API accepts dispatches whose results
# can never be delivered. NOTHING ELSE. `test_the_hard_set_has_exactly_this_
# membership` pins it.
HARD_PROBES = frozenset({"postgres.app", "redis"})

router = APIRouter()


def build_registry() -> ProbeRegistry:
    registry = ProbeRegistry()
    for name, fn in (
        ("postgres.app", probe_postgres_app),
        ("redis", probe_redis),
        ("postgres.admin", probe_postgres_admin),
        ("objectstore", probe_objectstore),
        ("vault", probe_vault),
        ("jwks", probe_jwks),
        ("signing", probe_signing),
        ("ca", probe_ca),
    ):
        registry.register(name, fn, hard=name in HARD_PROBES)
    return registry


@router.get("/livez")
async def livez() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    registry: ProbeRegistry = request.app.state.health_registry
    results = await registry.run(request.app.state)
    failing = sorted(
        r.name for r in results if r.name in HARD_PROBES and r.status is not Status.OK
    )
    if failing:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "failing": failing}
    return {"status": "ready", "failing": []}
```

- [ ] **Step 4: Wire into `main.py`**

In `backend/app/main.py`, delete the `/healthz` handler entirely and add, after the middleware registration:

```python
    from app.api.probes import build_registry
    from app.api.probes import router as probes_router

    app.state.health_registry = build_registry()
    app.include_router(probes_router)
```

- [ ] **Step 5: Run to verify pass**

Run: `cd backend && .venv/bin/pytest tests/test_platform_health_endpoints.py -q`
Expected: PASS (6 tests)

- [ ] **Step 6: Confirm `/healthz` is gone and nothing referenced it**

Run: `cd backend && grep -rn "healthz" app/ tests/ ../deploy/ ../frontend/src/ || echo "no references remain"`
Expected: no references. If `deploy/docker-compose.yml` references it, Task 11 replaces that; note it and continue.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/probes.py backend/app/main.py backend/tests/test_platform_health_endpoints.py
git commit -m "feat(health): /livez and /readyz replace the unconditional /healthz

/healthz returned {status: ok} with Postgres down, Redis down and the vault
sealed. Readiness now fails on a named hard set of postgres.app and redis and
nothing else, so a MinIO outage costs backup artifacts rather than the whole
control plane. The body carries failing names only -- reasons would let an
unauthenticated caller map which backend is weak."
```

---

## Task 5: The permission, and `GET /api/v1/platform/status`

**Files:**
- Create: `backend/app/api/v1/platform.py`, `backend/migrations/versions/0054_platform_status_permission.py`
- Modify: `backend/app/core/rbac.py`, `backend/app/main.py`, `backend/tests/test_platform_health_endpoints.py`

**Interfaces:**
- Consumes: `build_registry`, `ProbeResult`.
- Produces: `router` with `GET /api/v1/platform/status` gated on `platform.status.read`; response model `PlatformStatusOut` with `overall`, `probes`, `processes`, `modules`.

- [ ] **Step 1: Add the permission code**

In `backend/app/core/rbac.py`'s `PERMISSIONS` dict, alongside `audit.read`:

```python
    "platform.status.read": "View platform health, dependency status and history",
```

- [ ] **Step 2: Write the migration**

```python
# backend/migrations/versions/0054_platform_status_permission.py
"""platform: seed `platform.status.read`

docs/superpowers/specs/2026-08-15-platform-health-design.md §5.

NO TABLES HERE. The samples table is 0055; this migration is one permission row
and its grants, kept separate so the RBAC change can be reviewed and reverted
without touching schema.

WHO GETS IT. `org-admin` is not a choice -- `tests/test_rbac.py::
test_org_admin_has_all_permissions` asserts an org-admin's codes equal
set(PERMISSIONS), so a code in the dict and not seeded here turns that red.
Platform health is infrastructure-wide and not tenant data, so no other role is
seeded: an org that wants a technician to hold it grants it deliberately.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "platform.status.read"


def upgrade() -> None:
    op.execute(
        "INSERT INTO permissions (id, code, module, action) "
        f"VALUES (gen_random_uuid(), '{_CODE}', 'platform', 'status.read')"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        f"WHERE r.name = 'org-admin' AND p.code = '{_CODE}'"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code = '{_CODE}')"
    )
    op.execute(f"DELETE FROM permissions WHERE code = '{_CODE}'")
```

- [ ] **Step 3: Write the failing test (append to the endpoints test file)**

```python
# --- Task 5: the detail route --------------------------------------------------


def test_the_status_route_reports_modules_as_registered_not_healthy() -> None:
    """§1: modules are LISTED, not probed. The page must not imply otherwise,
    so the field name says what it measures."""
    from app.api.v1.platform import _module_registry

    modules = _module_registry()
    assert len(modules) > 20
    entry = modules[0]
    assert set(entry) == {"name", "routes", "job_types"}
    assert "status" not in entry, "a module entry must not claim health"
    assert "healthy" not in entry
```

- [ ] **Step 4: Implement the route**

```python
# backend/app/api/v1/platform.py
"""Operator-facing platform status.

GATED on `platform.status.read`. This is where the detail `/readyz` withholds
lives: reason codes, durations, heartbeat ages, 48h uptime and the module
registry.

MODULES ARE LISTED, NOT PROBED (spec §1). The registry reports what is
REGISTERED -- name, route count, job-type count -- because that is what it
measures. A per-module health contributor is a later extension point, and
until it exists nothing here may imply a module is healthy.
"""

from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import AuthContext, require_permission
from app.core.health.types import Status

router = APIRouter()


class ProbeOut(BaseModel):
    name: str
    status: Status
    duration_ms: int
    reason: str | None = None
    detail: dict[str, str | int | bool] | None = None


class ModuleOut(BaseModel):
    name: str
    routes: int
    job_types: int


class PlatformStatusOut(BaseModel):
    overall: Status
    probes: list[ProbeOut]
    processes: list[dict[str, Any]]
    modules: list[ModuleOut]


def _module_registry() -> list[dict[str, Any]]:
    """Route and job-type counts per module, read from the LIVE router.

    Attribution is by `endpoint.__module__`, not by URL prefix: a previous
    audit in this repo counted decorator sites and read 3 routes where 10
    existed, and attributed by prefix and read 0 for a module whose routes
    are mounted under another module's path.
    """
    from fastapi.routing import APIRoute

    from app.main import create_app

    app = create_app.__wrapped__() if hasattr(create_app, "__wrapped__") else None
    del app  # the live router is inspected by the caller in tests; see below
    from app.modules import MODULE_NAMES  # provided by app/modules/__init__.py

    counts: Counter[str] = Counter()
    return [
        {"name": name, "routes": counts.get(name, 0), "job_types": 0}
        for name in sorted(MODULE_NAMES)
    ]


@router.get("/platform/status", response_model=PlatformStatusOut)
async def platform_status(
    request: Request,
    # THE HOUSE FORM, and it is load-bearing. `route_permission`
    # (`app/core/rbac.py`) recovers a route's code by walking the dependant
    # tree for a closure whose `__qualname__` is exactly
    # `require_permission.<locals>.dependency`. Every gated route in this repo
    # declares it as a PARAMETER; matching that keeps this route visible to
    # `test_route_gating.py`'s whole-router sweep rather than relying on the
    # decorator form happening to land in the same tree.
    ctx: AuthContext = Depends(require_permission("platform.status.read")),
) -> PlatformStatusOut:
    registry = request.app.state.health_registry
    results = await registry.run(request.app.state)
    worst = Status.OK
    for r in results:
        if r.status is Status.DOWN:
            worst = Status.DOWN
            break
        if r.status is Status.DEGRADED:
            worst = Status.DEGRADED
    return PlatformStatusOut(
        overall=worst,
        probes=[
            ProbeOut(
                name=r.name,
                status=r.status,
                duration_ms=r.duration_ms,
                reason=r.reason.value if r.reason else None,
                detail=r.detail,
            )
            for r in results
        ],
        processes=[],  # Task 6
        modules=[ModuleOut(**m) for m in _module_registry()],
    )
```

> **Implementer note:** `_module_registry` above is a sketch that will not work as written — it must read the live router the same way `backend/tests/tool_surface.py::flatten` does, attributing each `APIRoute` by `route.endpoint.__module__` and each job type by its registered handler's module. **Build it that way**, reusing `tests/tool_surface.py`'s traversal rather than re-deriving it, and delete the `create_app.__wrapped__` line. The test in Step 3 asserts the shape; make it pass honestly.

- [ ] **Step 5: Mount it in `main.py`**

```python
    from app.api.v1.platform import router as platform_router

    app.include_router(platform_router, prefix="/api/v1")
```

- [ ] **Step 6: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_platform_health_endpoints.py tests/test_route_gating.py tests/test_rbac.py -q`
Expected: PASS. `test_route_gating` must stay green — the new route is gated, so it needs no exception entry.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/platform.py backend/app/core/rbac.py backend/migrations/versions/0054_platform_status_permission.py backend/app/main.py backend/tests/test_platform_health_endpoints.py
git commit -m "feat(platform): gated status route with probe detail and a module registry"
```

---

## Task 6: Per-instance heartbeats

**Files:**
- Create: `backend/app/core/health/heartbeat.py`, `backend/tests/test_platform_heartbeat.py`
- Modify: `backend/app/main.py`, `backend/app/worker.py`, `backend/app/gateway/main.py`, `backend/app/api/v1/platform.py`

**Interfaces:**
- Consumes: `redis` from app state / a passed client.
- Produces: `HEARTBEAT_INTERVAL_S = 15.0`, `async write_heartbeat(redis, process, instance, version)`, `async read_heartbeats(redis, *, now) -> list[ProcessHealth]` where `ProcessHealth` has `process`, `instances`, `oldest_age_s`, `status`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_platform_heartbeat.py
"""Per-instance heartbeats: the only way the API learns about the other two
processes, and the only way a multi-replica outage is visible.
"""

import pytest

from app.core.health.heartbeat import (
    HEARTBEAT_INTERVAL_S,
    read_heartbeats,
    write_heartbeat,
)
from app.core.health.types import Status


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def keys(self, pattern: str) -> list[str]:
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store.get(k) for k in keys]


@pytest.mark.asyncio
async def test_a_fresh_heartbeat_is_ok() -> None:
    redis = _FakeRedis()
    await write_heartbeat(redis, "api", "i-1", "v1", now=100.0)
    [health] = await read_heartbeats(redis, now=100.0)
    assert health.process == "api"
    assert health.instances == 1
    assert health.status is Status.OK


@pytest.mark.asyncio
async def test_a_stale_heartbeat_degrades_then_goes_down() -> None:
    redis = _FakeRedis()
    await write_heartbeat(redis, "worker", "i-1", "v1", now=0.0)
    [degraded] = await read_heartbeats(redis, now=HEARTBEAT_INTERVAL_S * 4)
    assert degraded.status is Status.DEGRADED
    [down] = await read_heartbeats(redis, now=HEARTBEAT_INTERVAL_S * 11)
    assert down.status is Status.DOWN


@pytest.mark.asyncio
async def test_one_of_two_api_instances_dying_is_visible() -> None:
    """THE MULTI-REPLICA BLIND SPOT (spec §4). A process answering the status
    request is alive -- but only that ONE replica. Behind traefik with several
    API containers, a dead replica is invisible to a request the live one
    served, and the page would report api: ok while a share of requests fail.
    """
    redis = _FakeRedis()
    await write_heartbeat(redis, "api", "i-1", "v1", now=0.0)
    await write_heartbeat(redis, "api", "i-2", "v1", now=0.0)
    fresh = HEARTBEAT_INTERVAL_S * 11
    await write_heartbeat(redis, "api", "i-1", "v1", now=fresh)
    [health] = await read_heartbeats(redis, now=fresh)
    assert health.instances == 2
    assert health.healthy_instances == 1
    assert health.status is Status.DEGRADED, "a lost replica must not read as ok"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/pytest tests/test_platform_heartbeat.py -q`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# backend/app/core/health/heartbeat.py
"""Per-instance liveness for the three processes, through Redis.

The API cannot probe the other two: the gateway is a mutual-TLS WebSocket
server with no HTTP surface, and the worker is a Celery consumer. Both are
reachable only by means the API must not use. So each process writes a
heartbeat and the API reads ages.

PER-INSTANCE, INCLUDING THE API'S OWN. A process that answered your status
request is alive -- that one replica. With several API containers behind
traefik, a dead replica is invisible to a request the live one served. Keys are
per-instance so the page can report `2 registered, 1 healthy` instead of `ok`.

TTL'd at 10x the interval so a retired container disappears rather than
accumulating for ever as a phantom `down`.
"""

import json
from dataclasses import dataclass
from typing import Any

from app.core.health.types import Status

HEARTBEAT_INTERVAL_S = 15.0
_DEGRADED_AFTER = HEARTBEAT_INTERVAL_S * 3
_DOWN_AFTER = HEARTBEAT_INTERVAL_S * 10
_PREFIX = "platform:heartbeat"


@dataclass(frozen=True, slots=True)
class ProcessHealth:
    process: str
    instances: int
    healthy_instances: int
    oldest_age_s: float
    status: Status
    version: str | None = None


async def write_heartbeat(
    redis: Any, process: str, instance: str, version: str, *, now: float
) -> None:
    await redis.set(
        f"{_PREFIX}:{process}:{instance}",
        json.dumps({"ts": now, "version": version}),
        ex=int(_DOWN_AFTER),
    )


async def read_heartbeats(redis: Any, *, now: float) -> list[ProcessHealth]:
    keys = sorted(await redis.keys(f"{_PREFIX}:*"))
    if not keys:
        return []
    raw = await redis.mget(keys)
    by_process: dict[str, list[tuple[float, str]]] = {}
    for key, value in zip(keys, raw, strict=True):
        if value is None:
            continue
        process = key.split(":")[2]
        payload = json.loads(value)
        by_process.setdefault(process, []).append((float(payload["ts"]), payload["version"]))
    out: list[ProcessHealth] = []
    for process, entries in sorted(by_process.items()):
        ages = [now - ts for ts, _v in entries]
        healthy = sum(1 for age in ages if age <= _DEGRADED_AFTER)
        oldest = max(ages)
        if healthy == 0:
            status = Status.DOWN if oldest > _DOWN_AFTER else Status.DEGRADED
        elif healthy < len(entries):
            status = Status.DEGRADED
        else:
            status = Status.OK
        out.append(
            ProcessHealth(
                process=process,
                instances=len(entries),
                healthy_instances=healthy,
                oldest_age_s=oldest,
                status=status,
                version=entries[0][1],
            )
        )
    return out
```

- [ ] **Step 4: Wire the three writers**

- **API** (`app/main.py`): an `asyncio` task started on the `startup` event writing every `HEARTBEAT_INTERVAL_S`, with `instance = os.environ.get("HOSTNAME", uuid4().hex[:8])` and `version = os.environ.get("ITS_IMAGE_TAG", "dev")`.
- **Worker** (`app/worker.py`): a beat task `app.worker.platform_heartbeat` on a 15s schedule doing the same with `process="worker"`.
- **Gateway** (`app/gateway/main.py`): a background `asyncio` task in `run_gateway`'s `_main`.

Each writer must swallow and log its own errors — a heartbeat failure must never take down the process it is reporting on.

- [ ] **Step 5: Surface it in the status route**

In `app/api/v1/platform.py`, replace `processes=[]` with a call to `read_heartbeats(request.app.state.redis, now=time.time())`, serialised to dicts.

- [ ] **Step 6: Run the tests and commit**

```bash
cd backend && .venv/bin/pytest tests/test_platform_heartbeat.py tests/test_platform_health_endpoints.py -q
git add backend/app/core/health/heartbeat.py backend/app/main.py backend/app/worker.py backend/app/gateway/main.py backend/app/api/v1/platform.py backend/tests/test_platform_heartbeat.py
git commit -m "feat(health): per-instance process heartbeats through redis

Keys are per-instance and include the API's own, because a process that
answered the status request is alive only for that replica -- with several
containers behind traefik a dead one is invisible to a request the live one
served."
```

---

## Task 7: The samples table, and the first table without a tenant

**Files:**
- Create: `backend/app/models/platform_health.py`, `backend/migrations/versions/0055_platform_health_sample.py`, `backend/tests/test_tenant_column_sweep.py`

**Interfaces:**
- Produces: `PlatformHealthSample` model; table `platform_health_sample`.

- [ ] **Step 1: Write the failing sweep test**

```python
# backend/tests/test_tenant_column_sweep.py
"""EXACTLY ONE TABLE MAY LACK A TENANT COLUMN.

Invariant #5: all data access is tenant-scoped, enforced at the database layer
via RLS. `platform_health_sample` is the first and only deliberate exception --
Postgres being unreachable is not any organisation's fact, and an `org_id`
there would be a fiction every read had to filter on for no meaning.

The guarantee that matters is not "this table is fine". It is "no SECOND one
appears without somebody deciding". A sweep that names the permitted exception
makes the next one fail loudly instead of arriving unnoticed.
"""

from app.models.base import Base

TENANTLESS_BY_DESIGN = frozenset({"platform_health_sample"})

# Tables that legitimately have no tenant because they are global reference or
# identity data, not per-org facts. This set predates the sweep; it is listed
# rather than discovered so a new entry is a visible decision.
GLOBAL_BY_NATURE = frozenset({"organizations", "permissions", "roles", "role_permissions"})


def test_only_the_named_table_lacks_a_tenant_column() -> None:
    offenders = {
        table.name
        for table in Base.metadata.sorted_tables
        if "org_id" not in table.columns
        and table.name not in TENANTLESS_BY_DESIGN
        and table.name not in GLOBAL_BY_NATURE
    }
    assert not offenders, (
        "these tables have no org_id and are not a recorded exception. Either "
        "add a tenant column, or add the table to TENANTLESS_BY_DESIGN with a "
        f"written reason in the spec: {sorted(offenders)}"
    )


def test_the_exception_list_has_not_rotted() -> None:
    """A named exception pointing at a table nothing defines exempts nothing."""
    live = {t.name for t in Base.metadata.sorted_tables}
    stale = (TENANTLESS_BY_DESIGN | GLOBAL_BY_NATURE) - live
    assert not stale, f"recorded exceptions that no longer exist: {sorted(stale)}"


def test_the_sweep_is_not_vacuous() -> None:
    """Without this, a broken metadata import reports a perfectly tenant-scoped
    schema."""
    assert len(Base.metadata.sorted_tables) > 50
```

> **Implementer note:** `GLOBAL_BY_NATURE` above is a guess. Run the sweep first with an empty `GLOBAL_BY_NATURE`, read the real offender list, and populate it from what is actually there — each entry justified in a comment. Do not adjust the assertion to fit; adjust the set to the measured truth and record why each belongs.

- [ ] **Step 2: Run to see the real offender list**

Run: `cd backend && .venv/bin/pytest tests/test_tenant_column_sweep.py -q`
Expected: FAIL, listing the tables that actually lack `org_id`. Use that list.

- [ ] **Step 3: Write the model**

```python
# backend/app/models/platform_health.py
"""One probe result at one instant.

NO `org_id`, DELIBERATELY (spec §6). Postgres being unreachable is not any
organisation's fact. RLS is still ENABLED on this table with a deny-all default
and one policy for `app_user`, so it is not an RLS-shaped hole -- it is a table
whose policy is "the application, and nobody else".

`reason` stores the reason CODE, never an exception's text.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


class PlatformHealthSample(Base):
    __tablename__ = "platform_health_sample"
    __table_args__ = (Index("ix_phs_probe_sampled", "probe", "sampled_at"),)

    id: Mapped[UUID] = uuid7_pk()
    probe: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    duration_ms: Mapped[int] = mapped_column()
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sampled_at: Mapped[datetime] = mapped_column()
```

- [ ] **Step 4: Write the migration**

```python
# backend/migrations/versions/0055_platform_health_sample.py
"""platform: the health samples table

docs/superpowers/specs/2026-08-15-platform-health-design.md §6.

THE FIRST TABLE WITHOUT A TENANT COLUMN, and it is handled explicitly rather
than quietly. RLS is ENABLED and FORCED with NO tenant policy -- which in
Postgres means deny-all -- plus exactly one policy granting `app_user`. So the
table is not an RLS-exempt hole; its policy is "the application, and nobody
else". `tests/test_tenant_column_sweep.py` asserts it stays the only one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_health_sample"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("probe", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_phs_probe_sampled", _TABLE, ["probe", "sampled_at"])
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY application_only ON {_TABLE} "
        "FOR ALL TO app_user USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT, DELETE ON {_TABLE} TO app_user")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS application_only ON {_TABLE}")
    op.drop_index("ix_phs_probe_sampled", table_name=_TABLE)
    op.drop_table(_TABLE)
```

- [ ] **Step 5: Run the migration and the sweep**

```bash
cd backend && .venv/bin/alembic upgrade head
.venv/bin/pytest tests/test_tenant_column_sweep.py tests/test_schema_drift.py -q
```
Expected: PASS. If `test_schema_drift` exists and compares model metadata to the migrated schema, it must agree.

- [ ] **Step 6: Verify RLS actually denies a non-application role**

```bash
cd backend && psql "$ITS_DATABASE_ADMIN_URL" -c "SET ROLE portal_user; SELECT count(*) FROM platform_health_sample;" 2>&1 | tail -2
```
Expected: a permission-denied or zero-row result, not a successful read of data. Record the actual output in the commit message.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/platform_health.py backend/migrations/versions/0055_platform_health_sample.py backend/tests/test_tenant_column_sweep.py
git commit -m "feat(platform): health samples table, RLS deny-all plus one grant

The first table without a tenant column. RLS stays enabled and forced with no
tenant policy -- deny-all -- plus one policy for app_user, so it is not an
RLS-shaped hole. A sweep asserts it remains the ONLY such table, so the second
exception has to be argued for rather than merged."
```

---

## Task 8: The sampler and the retention sweep

**Files:**
- Modify: `backend/app/worker.py`
- Create: `backend/tests/test_platform_health_sampler.py`

**Interfaces:**
- Consumes: `build_registry`, `PlatformHealthSample`.
- Produces: tasks `app.worker.platform_health_sample` (60s) and `app.worker.platform_health_retention` (86400s); `RETENTION_HOURS = 48`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_platform_health_sampler.py
"""The sampler writes one row per probe; the sweep prunes beyond 48h."""

from datetime import UTC, datetime, timedelta

import pytest

from app.worker import RETENTION_HOURS, prune_health_samples, sample_health_once


@pytest.mark.asyncio
async def test_one_row_per_probe_is_written(db_session) -> None:  # noqa: ANN001
    written = await sample_health_once(db_session)
    assert written == 8, "one row per registered probe"


@pytest.mark.asyncio
async def test_the_sweep_deletes_beyond_the_window_and_keeps_the_rest(db_session) -> None:  # noqa: ANN001
    now = datetime.now(UTC)
    await _insert(db_session, sampled_at=now - timedelta(hours=RETENTION_HOURS + 1))
    await _insert(db_session, sampled_at=now - timedelta(hours=1))
    deleted = await prune_health_samples(db_session, now=now)
    assert deleted == 1
```

> **Implementer note:** `db_session` is whatever async session fixture this repo's other DB tests use — find it in `tests/conftest.py` and match the existing pattern rather than inventing one. Write `_insert` as a small helper in this file.

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && .venv/bin/pytest tests/test_platform_health_sampler.py -q`
Expected: FAIL, `ImportError: cannot import name 'sample_health_once'`

- [ ] **Step 3: Implement the two functions and register the tasks**

Add to `app/worker.py` (module level):

```python
RETENTION_HOURS = 48


async def sample_health_once(session: AsyncSession) -> int:
    """Write one row per probe. Called by the beat task every 60 seconds.

    SAMPLED BY THE WORKER, NOT ON REQUEST. Request-driven sampling would be
    dense while somebody watches and empty overnight -- exactly inverted from
    what a history is for -- and several API processes would each write their
    own view of the same instant.
    """
    from app.api.probes import build_registry
    ...
```

and register in `create_celery`'s beat schedule:

```python
        "platform-health-sample": {
            "task": "app.worker.platform_health_sample",
            "schedule": 60.0,
        },
        "platform-health-retention": {
            "task": "app.worker.platform_health_retention",
            "schedule": 86400.0,
        },
        "platform-heartbeat": {
            "task": "app.worker.platform_heartbeat",
            "schedule": 15.0,
        },
```

with task wrappers matching the file's existing shape (`@celery_app.task(name="app.worker.platform_health_sample")` returning a `dict[str, int]`).

- [ ] **Step 4: Run the tests, then confirm the beat schedule parses**

```bash
cd backend && .venv/bin/pytest tests/test_platform_health_sampler.py -q
.venv/bin/python -c "
from app.worker import create_celery
names = sorted(create_celery().conf.beat_schedule)
print(names)
assert 'platform-health-sample' in names and 'platform-health-retention' in names
"
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/worker.py backend/tests/test_platform_health_sampler.py
git commit -m "feat(platform): sample health every 60s, prune beyond 48h"
```

---

## Task 9: The page

**Files:**
- Create: `frontend/src/modules/platform-status/{types.ts,api.ts,PlatformStatusPage.tsx,PlatformStatusPage.test.tsx}`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: `GET /api/v1/platform/status`.
- Produces: `PlatformStatusPage`, routed at `/platform-status`.

- [ ] **Step 1: Write the types**

```typescript
// frontend/src/modules/platform-status/types.ts
export type Status = "ok" | "degraded" | "down";

export interface Probe {
  name: string;
  status: Status;
  duration_ms: number;
  reason: string | null;
  detail: Record<string, string | number | boolean> | null;
}

export interface ProcessHealth {
  process: string;
  instances: number;
  healthy_instances: number;
  oldest_age_s: number;
  status: Status;
  version: string | null;
}

/** REGISTERED, not healthy. Modules are listed, never probed (spec §1). */
export interface RegisteredModule {
  name: string;
  routes: number;
  job_types: number;
}

export interface PlatformStatus {
  overall: Status;
  probes: Probe[];
  processes: ProcessHealth[];
  modules: RegisteredModule[];
}
```

- [ ] **Step 2: Write the query hook**

```typescript
// frontend/src/modules/platform-status/api.ts
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "../../lib/api";
import type { PlatformStatus } from "./types";

/** Polls every 15s -- matching the heartbeat interval, so a lost instance
 *  becomes visible within one cycle. The server caches probe results for 5s,
 *  so polling cannot turn the page into a load generator. */
export function usePlatformStatus() {
  return useQuery<PlatformStatus>({
    queryKey: ["platform-status"],
    queryFn: () => apiGet<PlatformStatus>("/platform/status"),
    refetchInterval: 15_000,
  });
}
```

> **Implementer note:** match `apiGet`'s real name and signature from `frontend/src/lib/api.ts`; other modules' `api.ts` files show the house pattern.

- [ ] **Step 3: Write the page and its test**

The page renders three sections — dependencies, processes, modules — with the module section headed **"Registered modules"** and a one-line note that registration is not health. Write `PlatformStatusPage.test.tsx` asserting: a `down` probe renders its reason code; the module section does not render any status pill; and a process with `healthy_instances < instances` renders the shortfall.

- [ ] **Step 4: Route it**

In `frontend/src/App.tsx`, import `PlatformStatusPage` and add `<Route path="/platform-status" element={<PlatformStatusPage />} />`. `App.routing.test.tsx` requires every folder under `modules/` to be imported by the shell — a module nobody routes has shipped here before.

- [ ] **Step 5: Verify and commit**

```bash
cd frontend && npm run test -- --run && npx tsc --noEmit
git add frontend/src/modules/platform-status frontend/src/App.tsx
git commit -m "feat(ui): platform status page"
```

---

## Task 10: Compose healthchecks for the three services we build

**Files:**
- Modify: `deploy/docker-compose.yml`

- [ ] **Step 1: Add healthchecks**

`api` and `web` get an HTTP check against `/livez`; `gateway` and `worker` have no HTTP surface, so they get a heartbeat-freshness check against Redis:

```yaml
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/livez', timeout=2).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
```

For `gateway` and `worker`, check that their own heartbeat key is fresh — reusing `read_heartbeats` via a tiny `python -c`, so the healthcheck exercises the same mechanism the status page reads rather than a second, divergent one.

- [ ] **Step 2: Verify the compose file parses**

```bash
docker compose -f deploy/docker-compose.yml config >/dev/null && echo "compose OK"
```
If `docker` is unavailable in this environment, validate with `python -c "import yaml,sys; yaml.safe_load(open('deploy/docker-compose.yml'))"` and say so in the commit message.

- [ ] **Step 3: Commit**

```bash
git add deploy/docker-compose.yml
git commit -m "chore(deploy): healthchecks for api, gateway, worker and web

Only postgres, minio and openbao had one; none of the three services we
build did."
```

---

## Task 11: End-to-end verification

- [ ] **Step 1: Full backend suite**

```bash
cd backend && .venv/bin/pytest tests/ -q -p no:randomly
```
Expected: all green. Postgres and redis must be running.

- [ ] **Step 2: Falsify the readiness gate against a real dependency**

Stop Redis, then:
```bash
redis-cli shutdown nosave || true
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/readyz   # expect 503
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/livez    # expect 200
redis-server --daemonize yes
```
Record both codes in the commit message. A readiness gate that has never been observed to fail is a readiness gate nobody has tested.

- [ ] **Step 3: Falsify the redaction rule against a real connection failure**

Point `ITS_DATABASE_URL` at a dead host with a password in the DSN, hit `/api/v1/platform/status`, and grep the response for the password. Expected: absent, with `reason: "unreachable"`.

- [ ] **Step 4: Commit the evidence**

```bash
git commit --allow-empty -m "test(platform): end-to-end verification evidence

/readyz 503 with redis stopped, /livez 200 in the same moment. A DSN with a
password behind a dead host renders as reason=unreachable with the password
absent from the response body."
```
