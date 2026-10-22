"""The nine dependency probes.

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


# --- soft probes: reported as degraded, never gating readiness -----------------

_OBJECT_STORE_SINGLETON: Any = None


async def probe_objectstore(state: Any, *, factory: Any = None) -> ProbeResult:
    """`health()` against a CACHED store (see the test for why it is cached).

    IT CALLED `bucket_exists()` UNTIL 2026-08-22, AND `ObjectStore` HAS NO SUCH
    METHOD. The attribute error was swallowed by the `except Exception` below
    and reported as `UNREACHABLE`, so this probe returned DOWN on every
    deployment that ever ran it, whatever object storage was actually doing --
    the operator's MinIO was `Up (healthy)` and the status page said
    "Unreachable ... check that it is running".

    The suite was green throughout, because the test that exercises this probe
    injects its own `_Store` fake through `factory` and that fake defines
    `bucket_exists`. The method existed in exactly two places in the
    repository: here, and in the double written to satisfy here. A fake is
    allowed to stand in for a collaborator; it is not allowed to invent the
    collaborator's interface. `test_the_probe_only_calls_methods_the_real_store_has`
    now pins the fake to the real class.
    """
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
        healthy = await _OBJECT_STORE_SINGLETON.health()
    except Exception:
        # A RAISE IS NOT A DOWN BUCKET. `health()` returns False for an
        # unreachable or missing bucket and never raises, so an exception here
        # is this process being wrong -- a missing method, a bad construction --
        # not the dependency being ill. Reporting it as MISCONFIGURED sends the
        # reader to the right place; UNREACHABLE sent them to check a server
        # that was already running.
        return ProbeResult(
            "objectstore", Status.DOWN, _ms(started), ReasonCode.MISCONFIGURED
        )
    if not healthy:
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


async def probe_keycloak_events(state: Any) -> ProbeResult:
    """Is Keycloak's credential-event forwarding configured, and running?

    THE UNCONFIGURED CASE IS `ok`, WITH `configured: false`, AND THAT IS THE
    WHOLE POINT OF THIS PROBE. An operator who has not set forwarding up has
    not broken anything, so alarming would train them to ignore it. But an
    audit feature that silently does nothing is worse than one that does not
    exist -- somebody reads the chain and believes it is complete -- so the gap
    has to be VISIBLE somewhere. Visible and alarming are different properties,
    and this is the one that was needed.

    READS A RECORD, PERFORMS NO NETWORK IO. The sweep runs in the Celery
    worker; the API process answers this. A probe that called Keycloak here
    would be measuring the API's path to Keycloak, which is not the path the
    forwarding uses, and would report `ok` for a worker that has been dead for
    a week (`app/core/health/heartbeat.py` makes the same argument).
    """
    from app.modules.keycloak_events.client import KeycloakEventsConfig
    from app.modules.keycloak_events.sweep import SWEEP_INTERVAL_S, read_last_sweep

    started = time.monotonic()
    try:
        config = KeycloakEventsConfig.from_settings(state.settings)
    except (OSError, ValueError):
        # Configured, and the secret file is missing or empty. NOT the same as
        # unconfigured: this operator believes forwarding is running.
        return ProbeResult(
            "keycloak_events", Status.DOWN, _ms(started), ReasonCode.MISCONFIGURED
        )
    if config is None:
        return ProbeResult(
            "keycloak_events", Status.OK, _ms(started), detail={"configured": False}
        )

    record = await read_last_sweep(state.redis)
    if record is None:
        return ProbeResult(
            "keycloak_events",
            Status.DEGRADED,
            _ms(started),
            ReasonCode.STALE,
            detail={"configured": True, "ever_run": False},
        )
    age_s = max(0.0, time.time() - float(record.get("ts", 0.0)))
    # Three intervals, matching `heartbeat._DEGRADED_AFTER`: one missed beat is
    # a slow worker, three is a stopped one.
    status = Status.OK if age_s < SWEEP_INTERVAL_S * 3 else Status.DEGRADED
    return ProbeResult(
        "keycloak_events",
        status,
        _ms(started),
        None if status is Status.OK else ReasonCode.STALE,
        detail={
            "configured": True,
            "ever_run": True,
            "age_s": int(age_s),
            "written": int(record.get("written", 0)),
        },
    )
