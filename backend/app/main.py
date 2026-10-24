"""Operator-facing API application factory."""

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.ca import get_ca
from app.core.config import Settings, get_settings
from app.core.errors.asyncio_handler import install_asyncio_exception_handler
from app.core.errors.store import DRAIN_INTERVAL_S, drain_once
from app.core.health.heartbeat import HEARTBEAT_INTERVAL_S, write_heartbeat
from app.core.i18n import tr, tr_marked
from app.core.security import JwksCache
from app.core.signing import get_signer
from app.core.vault import get_vault
from app.jobs.engine import JobEngine

logger = logging.getLogger("its.api")

# How long shutdown waits for a cancelled background loop to actually stop.
# Small: both loops sit in `asyncio.sleep` and take the cancellation on their
# next tick. This is a bound on a pathological case, not a budget.
_SHUTDOWN_TIMEOUT_S = 5.0


async def write_api_heartbeat_once(redis: Any, instance: str, version: str) -> None:
    """Write THIS API replica's heartbeat once, swallowing any failure.

    Extracted from the startup loop so a test can run exactly one iteration
    without a running loop -- the loop itself is only reachable through the
    lifespan, which most of this suite never starts. See
    `tests/test_platform_heartbeat.py`.

    THE SWALLOW IS THE POINT: a heartbeat is a report ABOUT the process, and a
    reporting failure must never be the thing that takes the process down.
    Logged with `exc_info` rather than the exception as a %-arg -- the message
    template stays static, and only the exception TYPE reaches the line.
    """
    try:
        await write_heartbeat(redis, "api", instance, version, now=time.time())
    except Exception:  # noqa: BLE001 - see the docstring: never propagate.
        logger.error("failed to write api heartbeat", exc_info=True)


async def drain_platform_errors_once(engine: AsyncEngine) -> int:
    """Drain THIS process's share of the in-process capture queue to
    Postgres once, swallowing any failure -- same seam as
    `write_api_heartbeat_once` above, extracted so a test can run exactly one
    iteration without a running loop (see `tests/test_error_drain_wiring.py`).

    THE SWALLOW IS THE POINT, again: `capture_exception` already never
    raises (spec §3 falsifier #2), and this is the other half of that
    guarantee -- a drain failure (a dead database, a bad migration) must not
    also take the API process down. Logged with `exc_info` rather than the
    exception as a %-arg, matching every other never-raise-swallow-and-log
    site in this kernel.
    """
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await drain_once(session)
    except Exception:  # noqa: BLE001 - see the docstring: never propagate.
        logger.error("failed to drain platform errors", exc_info=True)
        return 0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """The MCP server's session manager (`app/mcp/server.py`), around the
    API's background loops. The SDK refuses every `/mcp` request until its
    manager runs, and a manager runs once per instance -- so once per app."""
    async with app.state.mcp.lifespan(), _background_loops(app):
        yield


@asynccontextmanager
async def _background_loops(app: FastAPI) -> AsyncIterator[None]:
    """Start the API's two background loops, and STOP THEM AGAIN.

    Replaces three `@app.on_event("startup")` handlers. FastAPI has
    deprecated `on_event` in favour of this, but the deprecation is the
    smaller half of the reason: `on_event("startup")` had no counterpart
    here, so `heartbeat_task` and `error_drain_task` were created and never
    cancelled. On shutdown asyncio tears down a loop with two pending tasks
    still in it -- the "Task was destroyed but it is pending!" shape. A
    lifespan has somewhere to put the cancel, so the teardown below is the
    part that is new behaviour rather than a rename.

    Everything reads `app.state`, which `create_app` has fully populated by
    the time this runs -- the lifespan is entered when the server starts,
    not when the app object is built.
    """
    # PER-REPLICA HEARTBEAT, including the API's own (`app.core.health`).
    # A process that answered a status request is alive only for that one
    # replica; behind traefik with several API containers, a dead replica is
    # invisible to a request the live one served. The instance id defaults to
    # the container hostname (Docker and Kubernetes both set it uniquely per
    # replica); a random suffix is the fallback for anything that does not.
    instance = os.environ.get("HOSTNAME", uuid4().hex[:8])
    version = os.environ.get("ITS_IMAGE_TAG", "dev")

    async def _heartbeat_loop() -> None:
        while True:
            await write_api_heartbeat_once(app.state.redis, instance, version)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    async def _drain_loop() -> None:
        while True:
            await drain_platform_errors_once(app.state.app_engine)
            await asyncio.sleep(DRAIN_INTERVAL_S)

    # Replace asyncio's own loop exception handler so a task nobody awaits is
    # captured and logged through this kernel rather than through asyncio's
    # `repr()`-everything default. See `app.core.errors.asyncio_handler` for
    # the mechanism and the leak it closes.
    install_asyncio_exception_handler(asyncio.get_running_loop(), where="api")

    app.state.heartbeat_task = asyncio.create_task(_heartbeat_loop())
    app.state.error_drain_task = asyncio.create_task(_drain_loop())

    try:
        yield
    finally:
        for name in ("heartbeat_task", "error_drain_task"):
            task = getattr(app.state, name, None)
            if task is None:
                continue
            task.cancel()
            # Await the cancellation rather than firing and forgetting: an
            # un-awaited cancelled task still emits "Task exception was never
            # retrieved" on some paths, which is exactly the class of noise
            # this kernel exists to keep out of the log stream.
            #
            # BOUNDED, because an unbounded await here is a shutdown hang.
            # Both loops today take CancelledError promptly -- it is a
            # BaseException, so neither loop's `except Exception` swallows it
            # -- but a future loop that catches too broadly would turn this
            # line into a process that will not die, and the operator's only
            # symptom would be a container stuck in `Stopping`. Measured
            # while falsifying this teardown: removing the cancel and leaving
            # the await made the test suite hang for four minutes.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_SHUTDOWN_TIMEOUT_S)
            except (asyncio.CancelledError, TimeoutError):
                pass
            if not task.done():
                logger.error("background task %s did not stop on shutdown", name)


def create_app(
    settings: Settings | None = None,
    *,
    jwks_http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    from app.core.logging import configure_logging

    configure_logging()
    settings = settings or get_settings()
    app = FastAPI(title="ITMS API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    # Parsed once, here, so a bad ITS_TRUSTED_PROXY_CIDRS stops the process at
    # startup instead of 500ing the first install-link request.
    from app.core.client_address import parse_trusted_proxies

    app.state.trusted_proxies = parse_trusted_proxies(settings.trusted_proxy_cidrs)
    # Authenticate the audit chain with the process-wide HMAC key (fail closed
    # in production). Always set — None clears any key from a prior context.
    from app.core.audit import audit_service, load_audit_hmac_key

    audit_service.set_hmac_key(load_audit_hmac_key(settings))
    # THE AI PROVIDER CONFIGURATION IS READ HERE SO A BAD ONE FAILS AT STARTUP.
    #
    # Since `docs/modules/ai-admin/spec.md` §0.14 the deployment's `ITS_AI_*`
    # environment is the ONLY switch on the assistant, and it must name at most
    # one provider ("the same model for every tenant"). `configured_providers()`
    # raises `ProviderConfigError` on a misspelled variable, an unknown kind or
    # a second provider key -- and forcing that read at app construction puts
    # the failure in the operator's terminal, next to the file they edited,
    # instead of in front of an administrator at 09:00. An UNCONFIGURED
    # deployment is not an error: it returns `{}`, and the AI routes answer 503.
    from app.core.ai.providers import configured_providers

    _ai_provider = next(iter(configured_providers().values()), None)
    if _ai_provider is None:
        logger.info("ai assistant: no model provider configured; the AI routes answer 503")
    else:
        # Key, kind and model only. The base URL and the key path are
        # deliberately absent: a log line is a place secrets and internal hosts
        # go to be copied into tickets.
        logger.info(
            "ai assistant: provider %s (%s) model %s",
            _ai_provider.key,
            _ai_provider.kind,
            _ai_provider.model,
        )
    app.state.app_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    app.state.admin_engine = create_async_engine(settings.database_admin_url, pool_pre_ping=True)
    app.state.jwks = JwksCache(settings.oidc_jwks_url, client=jwks_http_client)
    app.state.ca = get_ca(settings)
    app.state.signer = get_signer(settings)
    app.state.vault = get_vault(settings)
    # Object store for large artifacts / remote-recording bytes. Built lazily on
    # first use (see app.modules.remote.api._object_store): its constructor
    # ensures the bucket exists, which would otherwise force S3 I/O at every app
    # startup — undesirable for the API process, which rarely touches storage.
    app.state.object_store = None
    app.state.job_engine = JobEngine(
        signer=app.state.signer, default_ttl_s=settings.job_default_ttl_s
    )
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    from app.api.locale_middleware import locale_middleware
    from app.api.request_context import request_context_middleware
    from app.api.security_headers import security_headers_middleware

    # The MCP channel's never-exposed guard (`app/mcp/guard.py`) is added
    # FIRST, so it is the INNERMOST of these layers: the locale is set when it
    # answers, and its refusal still carries the security headers. Its route
    # table is filled in after `modules.register_all` below.
    from app.mcp.guard import McpNeverExposedGuard

    app.add_middleware(McpNeverExposedGuard)
    app.middleware("http")(security_headers_middleware)
    app.middleware("http")(request_context_middleware)
    # Registered last, so it is the OUTERMOST layer: the request's language is
    # set before anything else (middleware, dependency, handler, exception
    # handler) runs and reset after all of it.
    app.middleware("http")(locale_middleware)
    # A job refused by the review gate is a 409, wherever it was raised: the
    # kernel route, a module route, the AI confirm path. One handler, so no
    # route has to remember to translate it.
    from fastapi.responses import JSONResponse
    from starlette.requests import Request as StarletteRequest

    from app.jobs.review_gate import DeviceAwaitingReview

    async def _awaiting_review(_request: StarletteRequest, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DeviceAwaitingReview)
        return JSONResponse(
            status_code=409,
            content={
                "detail": tr("waiting for review"),
                "device_id": str(exc.device_id),
                "job_type": exc.job_type,
            },
        )

    app.add_exception_handler(DeviceAwaitingReview, _awaiting_review)

    # A write to a global row (an O-table row with no client) under a scope
    # that is not every client: refused by `app.core.global_rows` before any
    # SQL, so the transaction and its audit event roll back. The same 403 and
    # the same words as `require_all_clients`, wherever it was raised -- a
    # route, a service, the MCP channel's in-process call (which passes a
    # 403's detail through unchanged, `app/mcp/invoke.py::error_text`).
    from app.api.deps import ALL_CLIENTS_REQUIRED
    from app.core.global_rows import GlobalRowNotWritable

    async def _global_row(_request: StarletteRequest, exc: Exception) -> JSONResponse:
        assert isinstance(exc, GlobalRowNotWritable)
        return JSONResponse(status_code=403, content={"detail": tr_marked(ALL_CLIENTS_REQUIRED)})

    app.add_exception_handler(GlobalRowNotWritable, _global_row)

    from app.api.probes import build_registry
    from app.api.probes import router as probes_router

    app.state.health_registry = build_registry()
    app.include_router(probes_router)

    from app.api.v1.audit import router as audit_router
    from app.api.v1.clients import router as clients_router
    from app.api.v1.credentials import router as credentials_router
    from app.api.v1.dashboard import router as dashboard_router
    from app.api.v1.device_groups import router as device_groups_router
    from app.api.v1.devices import router as devices_router
    from app.api.v1.enrollment import agents_router, enroll_router, tokens_router
    from app.api.v1.errors import router as errors_router
    from app.api.v1.install_link import router as install_link_router
    from app.api.v1.jobs import router as jobs_router
    from app.api.v1.me import router as me_router
    from app.api.v1.orgs import router as orgs_router
    from app.api.v1.platform import router as platform_router
    from app.api.v1.realm_settings import router as realm_settings_router
    from app.api.v1.roles import router as roles_router
    from app.api.v1.setup import router as setup_router
    from app.api.v1.ws import router as ws_router
    from app.modules.user_admin.api import router as user_admin_router

    app.include_router(tokens_router, prefix="/api/v1")
    app.include_router(agents_router, prefix="/api/v1")
    app.include_router(install_link_router, prefix="/api/v1")
    app.include_router(me_router, prefix="/api/v1")
    app.include_router(dashboard_router, prefix="/api/v1")
    app.include_router(orgs_router, prefix="/api/v1")
    app.include_router(devices_router, prefix="/api/v1")
    # `/device-groups`, NOT under `devices_router`'s `/devices` prefix: a
    # group is addressed in its own right by deploy targets, policy scopes
    # and RBAC grants, and nesting it under a device would read as "groups
    # of this device". Separate router, separate namespace -- the same
    # first-registered-wins hazard the access-control note below records.
    app.include_router(device_groups_router, prefix="/api/v1")
    # MSP clients spec §3.2: `/clients` and the `/sites/{id}` and
    # `/contacts/{id}` routes beneath them. `/sites/{id}/install-link` is
    # `install_link_router`'s, a longer path, so the two never shadow.
    app.include_router(clients_router, prefix="/api/v1")
    app.include_router(jobs_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(credentials_router, prefix="/api/v1")
    app.include_router(platform_router, prefix="/api/v1")
    app.include_router(errors_router, prefix="/api/v1")
    # Access control (spec 2026-08-19). `roles_router` owns /roles and
    # /permissions; `user_admin_router` owns the WHOLE /users namespace,
    # including effective-permissions. That split is deliberate: FastAPI
    # resolves duplicate paths first-registered-wins and raises nothing, so
    # two routers reaching into one namespace would shadow silently, pass
    # every gating test, and inflate the tool-surface count with a route no
    # request can reach.
    app.include_router(roles_router, prefix="/api/v1")
    app.include_router(user_admin_router, prefix="/api/v1")
    app.include_router(realm_settings_router, prefix="/api/v1")
    # UNAUTHENTICATED BY NECESSITY: it answers before anybody can sign in.
    # What guards it is zero-users AND a one-time install code, never the
    # absence of a token — see app/api/v1/setup.py.
    app.include_router(setup_router, prefix="/api/v1")
    app.include_router(ws_router, prefix="/api/v1")
    app.include_router(enroll_router)

    # The MCP server (`app/mcp/server.py`): the protected-resource metadata,
    # and `/mcp` behind its own OAuth gate. Public paths outside `/api/v1`;
    # the gate, not a dependency, decides who gets past them.
    from app.mcp import build_mcp

    mcp = build_mcp(app)
    app.state.mcp = mcp
    app.include_router(mcp.metadata_router)
    app.router.routes.extend(mcp.routes())
    # The console's side of it: Settings → AI connections (`app/mcp/api.py`).
    from app.mcp.api import router as mcp_admin_router

    app.include_router(mcp_admin_router, prefix="/api/v1")

    # Business modules register into the kernel here (job-type result handlers
    # bound to this app's Redis publisher + module API routes).
    import app.modules as modules
    from app.core.module_registry import build_module_registry, capture_job_type_registrations

    # `capture_job_type_registrations` monkeypatches `app.jobs.registry.
    # register` for the duration of this ONE call, single-threaded at
    # startup, then restores it -- see that function's docstring for why
    # attribution is by call site. This is also what makes the module
    # registry below free of the defect it replaced: no second app built per
    # request, no `tests` import outside the test tree, no global patched
    # while concurrent requests could be in flight.
    with capture_job_type_registrations() as job_type_counts:
        modules.register_all(app)

    # Routes the MCP channel may never reach, resolved against the router
    # actually built: route identities for `auth_context`, path matchers for
    # `McpNeverExposedGuard`. After `register_all`, because three of the ten
    # are module routes.
    from app.core.router_inspect import flatten
    from app.mcp.guard import missing_never_exposed, never_exposed_routes

    flat = flatten(app.router)
    # A never-exposed pair that names no route is a route renamed or removed
    # under the guard's feet: it would stop being refused, silently. Fail the
    # boot instead.
    missing = missing_never_exposed(flat)
    if missing:
        raise RuntimeError(  # i18n-exempt: startup config error, logged
            f"NEVER_EXPOSED names routes that do not exist: {sorted(missing)}"
        )
    app.state.mcp_never_route_ids, app.state.mcp_never_routes = never_exposed_routes(flat)

    # Route and job-type counts per module, computed once against the app
    # actually being built -- see `app.core.module_registry` and
    # `app/api/v1/platform.py`. `GET /api/v1/platform/status` only reads
    # this; it never rebuilds it.
    app.state.module_registry = build_module_registry(app, job_type_counts)

    return app
