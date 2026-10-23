"""The probe registry: concurrency, timeouts, caching, and fail-closed status.

NO DATABASE. Every probe here is a stub -- this file tests the machinery that
runs probes, not the probes themselves (Task 2/3 cover those).
"""

import asyncio
from pathlib import Path

from app.core.health.probes import (
    probe_jwks,
    probe_objectstore,
    probe_postgres_app,
    probe_redis,
    probe_vault,
)
from app.core.health.registry import ProbeRegistry
from app.core.health.types import ProbeResult, ReasonCode, Status


def _ok(name: str) -> ProbeResult:
    return ProbeResult(name=name, status=Status.OK, duration_ms=1, reason=None)


async def test_a_probe_that_hangs_is_cut_off_and_reports_timeout() -> None:
    """A status endpoint that can hang takes the page down with the dependency
    it is describing. The timeout is a RESULT, not an exception.

    THE BUDGET IS DELIBERATELY FAR LARGER THAN THE PROBE TIMEOUT, and the
    elapsed time is asserted. The first version of this test used a 1s budget
    against a 0.05s probe timeout and asserted only the reason code -- so when
    the per-probe timeout was deleted, the TOTAL BUDGET fired instead, returned
    TIMEOUT for every probe, and the test still passed. It pinned the reason
    code while testing nothing about the guard it was written for. Verified by
    deleting `timeout=self._probe_timeout_s`: the old assertions stayed green.

    With a 5s budget and a 0.05s probe timeout, only the per-probe path can
    return in under a second, so the elapsed assertion is what makes the two
    mechanisms distinguishable.
    """
    registry = ProbeRegistry(probe_timeout_s=0.05, total_budget_s=5.0, cache_ttl_s=0)

    async def hangs(_state: object) -> ProbeResult:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    registry.register("slow", hangs)
    loop = asyncio.get_running_loop()
    started = loop.time()
    results = await registry.run(object())
    elapsed = loop.time() - started
    assert [r.status for r in results] == [Status.DOWN]
    assert results[0].reason is ReasonCode.TIMEOUT
    assert elapsed < 1.0, (
        f"took {elapsed:.2f}s -- the per-probe timeout did not fire; the total "
        "budget did, which is a different guard"
    )


async def test_the_total_budget_is_a_separate_guard_from_the_per_probe_timeout() -> None:
    """The budget catches what the per-probe timeout cannot: many probes each
    finishing inside their own limit but summing past what a caller will wait.

    Pinned separately so neither guard can silently stand in for the other --
    which is exactly how the per-probe timeout went untested above.
    """
    registry = ProbeRegistry(probe_timeout_s=5.0, total_budget_s=0.1, cache_ttl_s=0)

    async def slow(_state: object) -> ProbeResult:
        await asyncio.sleep(2)
        raise AssertionError("unreachable")

    registry.register("a", slow)
    results = await registry.run(object())
    assert [r.reason for r in results] == [ReasonCode.TIMEOUT]


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


# --- Task 2: the hard probes ---------------------------------------------------


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


async def test_postgres_probe_reports_ok_when_select_1_succeeds() -> None:
    result = await probe_postgres_app(_FakeState(app_engine=_FakeEngine()))
    assert result.status is Status.OK
    assert result.reason is None


async def test_postgres_probe_reports_down_without_the_dsn() -> None:
    """The failure mode this whole design is careful about: a connection error
    whose text is the DSN, on an endpoint reachable without a token."""
    exc = OSError("could not connect to postgres://its:S3CRET@db.internal/itsuite")
    result = await probe_postgres_app(_FakeState(app_engine=_FakeEngine(exc)))
    assert result.status is Status.DOWN
    assert result.reason is ReasonCode.UNREACHABLE
    assert "S3CRET" not in repr(result)


async def test_redis_probe_pings() -> None:
    class _R:
        async def ping(self) -> bool:
            return True

    result = await probe_redis(_FakeState(redis=_R()))
    assert result.status is Status.OK


# --- Task 3: the soft probes ---------------------------------------------------


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


async def test_an_empty_jwks_cache_is_degraded_not_down() -> None:
    """An empty cache is indistinguishable from 'no token has been validated
    yet', which is the normal state of a freshly started process."""

    class _Empty:
        _keys: dict[str, object] = {}
        _last_refetch = 0.0

    result = await probe_jwks(_FakeState(jwks=_Empty()))
    assert result.status is Status.DEGRADED
    assert result.reason is ReasonCode.EMPTY_CACHE


async def test_a_sealed_vault_is_down_with_a_sealed_reason_not_an_error() -> None:
    """A sealed vault is a normal operational state with a specific remedy.
    Reporting it as UNREACHABLE would lose that."""

    class _Sealed:
        async def health(self) -> tuple[bool, ReasonCode | None]:
            return False, ReasonCode.SEALED

    result = await probe_vault(_FakeState(vault=_Sealed()))
    assert result.status is Status.DOWN
    assert result.reason is ReasonCode.SEALED


async def test_the_objectstore_is_constructed_at_most_once() -> None:
    """`ObjectStore.__init__` ensures the bucket exists -- that is S3 I/O, and
    it is why `app/main.py` builds it lazily. A probe that constructed per call
    would turn a page refresh into a bucket-ensure storm."""
    constructed = 0

    class _Store:
        def __init__(self, _settings: object) -> None:
            nonlocal constructed
            constructed += 1

        async def health(self) -> bool:
            return True

    state = _FakeState(settings=object(), object_store=None)
    for _ in range(3):
        await probe_objectstore(state, factory=_Store)
    assert constructed == 1


# --------------------------------------------------------------------------- #
# keycloak_events: the probe whose whole job is making a silence visible
# --------------------------------------------------------------------------- #


class _FakeRedis:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    async def get(self, _key: str) -> str | None:
        return self.value


def _kc_settings(tmp_path: object, *, configured: bool) -> object:
    from types import SimpleNamespace

    if not configured:
        return SimpleNamespace(keycloak_admin_url=None, keycloak_events_secret_path=None)
    secret = tmp_path / "kc.secret"  # type: ignore[operator]
    secret.write_text("not-a-real-secret")
    return SimpleNamespace(
        keycloak_admin_url="https://keycloak.test",
        keycloak_realm="it-suite",
        keycloak_events_client_id="it-suite-events",
        keycloak_events_secret_path=str(secret),
    )


async def test_an_unconfigured_deployment_is_ok_and_says_so(tmp_path: object) -> None:
    """THE POINT OF THIS PROBE. An operator who has not enabled forwarding has
    not broken anything, so alarming would train them to ignore it -- but the
    gap still has to be VISIBLE, because somebody will otherwise read the audit
    chain and believe it is complete. `ok` with `configured: false` is the
    distinction: visible, not alarming."""
    from app.core.health.probes import probe_keycloak_events

    result = await probe_keycloak_events(
        _FakeState(settings=_kc_settings(tmp_path, configured=False), redis=_FakeRedis())
    )
    assert result.status is Status.OK
    assert result.detail == {"configured": False}


async def test_a_configured_deployment_that_has_never_swept_is_degraded(
    tmp_path: object,
) -> None:
    """Configured and never run is the state an operator most needs told: they
    believe forwarding is happening. It is DEGRADED rather than DOWN because
    nothing is broken for anyone using the platform -- only the evidence trail
    is thinner than they think."""
    from app.core.health.probes import probe_keycloak_events

    result = await probe_keycloak_events(
        _FakeState(settings=_kc_settings(tmp_path, configured=True), redis=_FakeRedis(None))
    )
    assert result.status is Status.DEGRADED
    assert result.reason is ReasonCode.STALE
    assert result.detail == {"configured": True, "ever_run": False}


async def test_a_recent_sweep_is_ok_and_a_stale_one_is_degraded(tmp_path: object) -> None:
    """The failure this catches is a STOPPED WORKER, which no amount of
    Keycloak-side configuration reveals: the settings are right, the credential
    works, and nothing has run for a week."""
    import json
    import time

    from app.core.health.probes import probe_keycloak_events
    from app.modules.keycloak_events.sweep import SWEEP_INTERVAL_S

    settings = _kc_settings(tmp_path, configured=True)

    fresh = json.dumps({"ts": time.time(), "written": 2})
    result = await probe_keycloak_events(_FakeState(settings=settings, redis=_FakeRedis(fresh)))
    assert result.status is Status.OK
    assert result.reason is None
    assert result.detail is not None and result.detail["written"] == 2

    old = json.dumps({"ts": time.time() - SWEEP_INTERVAL_S * 4, "written": 0})
    result = await probe_keycloak_events(_FakeState(settings=settings, redis=_FakeRedis(old)))
    assert result.status is Status.DEGRADED
    assert result.reason is ReasonCode.STALE


async def test_a_configured_secret_that_has_gone_missing_is_down_not_unconfigured(
    tmp_path: object,
) -> None:
    """THE TWO SILENCES ARE NOT THE SAME. Degrading "configured, and the secret
    file is gone" into "not configured" would hide precisely the deployment
    whose operator believes forwarding is running."""
    from app.core.health.probes import probe_keycloak_events

    settings = _kc_settings(tmp_path, configured=True)
    (tmp_path / "kc.secret").unlink()  # type: ignore[operator]

    result = await probe_keycloak_events(_FakeState(settings=settings, redis=_FakeRedis()))
    assert result.status is Status.DOWN
    assert result.reason is ReasonCode.MISCONFIGURED


# --------------------------------------------------------------------------- #
# what the status PAGE prints, as distinct from what the probe is CALLED
# --------------------------------------------------------------------------- #


def _status_app(results: list[ProbeResult]):
    """`/platform/status` on a router-only app: no database, no auth stack.

    The permission dependency is built once, at import time, inside the route's
    default argument, so it is recovered off the route's dependant rather than
    reconstructed -- `require_permission("platform.status.read")` returns a NEW
    closure on every call, and overriding a different object than the one the
    route holds silently overrides nothing.
    """
    from fastapi import FastAPI

    from app.api.v1.platform import router as platform_router

    app = FastAPI()
    app.include_router(platform_router)
    registry = ProbeRegistry(cache_ttl_s=0)
    for r in results:

        async def _immediate(_state: object, res: ProbeResult = r) -> ProbeResult:
            return res

        registry.register(r.name, _immediate)
    app.state.health_registry = registry
    app.state.module_registry = []

    class _NoHeartbeats:
        async def keys(self, _pattern: str) -> list[str]:
            return []

    app.state.redis = _NoHeartbeats()

    # Read off the ROUTER's own route, not off `app.routes`: this FastAPI
    # wraps an included router in a single `_IncludedRouter` entry, so the
    # `APIRoute` (and the dependant the gate hangs from) is not at the app
    # level at all.
    route = next(r for r in platform_router.routes if getattr(r, "path", "") == "/platform/status")
    gate = route.dependant.dependencies[0].call  # type: ignore[attr-defined]
    assert "require_permission" in gate.__qualname__, gate.__qualname__
    app.dependency_overrides[gate] = lambda: None
    return app


async def test_the_status_route_prints_a_label_and_carries_the_key_separately() -> None:
    """THE WORD REACHED THE SCREEN FROM A PYTHON IDENTIFIER.

    `PlatformStatusPage` renders the probe name the API sends, and one probe is
    registered as `keycloak_events` -- so a buyer read the identity vendor's
    name in a table heading position, from a dict key that no scan of user-
    visible string literals could have caught.

    The key is NOT renamed: `platform_health_sample.probe` rows carry it,
    `/readyz`'s `failing` list reports it, and
    `test_platform_health_sampler.py` pins the sampled set -- renaming would
    orphan every stored sample. So the route sends both: `key`, the stable
    identifier, and `name`, the human one the page prints.
    """
    import re

    from fastapi.testclient import TestClient

    from app.api.probes import _ALL_PROBES, probe_label

    results = [_ok(key) for key in _ALL_PROBES]
    body = TestClient(_status_app(results)).get("/platform/status").json()

    by_key = {p["key"]: p for p in body["probes"]}
    assert set(by_key) == set(_ALL_PROBES), "the identifier every stored sample is keyed by"
    assert by_key["keycloak_events"]["name"] == probe_label("keycloak_events")

    printed = " | ".join(p["name"] for p in body["probes"])
    assert not re.search(r"keycloak", printed, re.IGNORECASE), printed


async def test_readyz_still_reports_the_key_because_that_is_what_it_is_for() -> None:
    """The display label is for the page, and only for the page. `/readyz` is
    read by a load balancer and by `platform_health_sample`'s writer, both of
    which key on the identifier -- a label there would break the contract this
    change exists to preserve."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.probes import HARD_PROBES
    from app.api.probes import router as probes_router

    app = FastAPI()
    registry = ProbeRegistry(cache_ttl_s=0)

    async def _down(_state: object) -> ProbeResult:
        return ProbeResult("redis", Status.DOWN, 1, ReasonCode.UNREACHABLE)

    async def _up(_state: object) -> ProbeResult:
        return ProbeResult("postgres.app", Status.OK, 1)

    registry.register("redis", _down, hard="redis" in HARD_PROBES)
    registry.register("postgres.app", _up, hard="postgres.app" in HARD_PROBES)
    app.state.health_registry = registry
    app.include_router(probes_router)

    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["failing"] == ["redis"]


# --------------------------------------------------------------------------- #
# The guard that would have caught a probe calling a method nobody implements
# --------------------------------------------------------------------------- #


async def test_the_probe_only_calls_methods_the_real_store_has() -> None:
    """A fake may stand in for a collaborator. It may not invent its interface.

    `probe_objectstore` called `bucket_exists()` for the life of the platform
    status feature, and `ObjectStore` has never had that method. The
    `AttributeError` was caught by the probe's own `except Exception` and
    reported as `UNREACHABLE`, so the status page said object storage was down
    on every deployment that ever ran it -- while the operator's MinIO sat
    `Up (healthy)` and told them to "check that it is running".

    Nothing went red, because the fake above defined `bucket_exists` too. The
    method existed in exactly two places in the repository: the probe, and the
    double written to satisfy the probe. They agreed with each other and with
    nothing else.

    So this asserts the direction that was missing: every coroutine the probe's
    double exposes must exist on the REAL class. It reads the source rather than
    the object, because importing `ObjectStore` costs a boto3 client.
    """
    import inspect
    import re

    from app.core.health import probes as probes_module

    real = (Path(probes_module.__file__).parent.parent / "storage.py").read_text()
    declared = set(re.findall(r"^    (?:async )?def (\w+)", real, re.M))

    called = set(re.findall(r"_OBJECT_STORE_SINGLETON\.(\w+)\(", inspect.getsource(probes_module)))
    assert called, "the probe calls nothing on the store -- this test has stopped measuring"

    missing = sorted(called - declared)
    assert missing == [], (
        f"probe_objectstore calls {missing} on the object store, and ObjectStore "
        "declares no such method. Every exception it raises is swallowed and "
        "reported as a dependency failure."
    )
