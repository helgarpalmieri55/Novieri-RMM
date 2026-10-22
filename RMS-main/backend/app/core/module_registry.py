"""The platform module registry: per-module route and job-type counts,
computed ONCE at application startup from the real app `create_app` builds.

MODULES ARE LISTED, NOT PROBED (spec §1) -- see `app/api/v1/platform.py`.
This file only measures what is REGISTERED.

WHY THIS RUNS AT STARTUP, NOT PER REQUEST. An earlier version of
`GET /api/v1/platform/status` called into `tests/tool_surface.py` to build a
SECOND app on every request, for two reasons that both stopped applying once
the counting moved here:

  * `tests` does not exist in the deployed image (`pyproject.toml`'s
    `packages = ["app"]`; `deploy/backend.Dockerfile` copies only `app`,
    `migrations` and `alembic.ini`), so that import raised
    `ModuleNotFoundError` at request time in production while every test --
    run from a checkout that HAS `tests/` -- stayed green.
  * building a second app meant a second pair of async SQLAlchemy engines, a
    second Redis client, JWKS cache, CA, signer and vault, leaked on every
    page refresh of a route the operator page polls every 15 seconds -- and
    the monkeypatch below, swapped in and restored around that build, raced
    with any overlapping request doing the same thing.

Called once from `app.main.create_app`, this needs none of that: it counts
routes and job types on the SAME app `create_app` is already assembling, and
the monkeypatch is confined to that single-threaded startup call with no
concurrent caller. The finished list is stored on `app.state.module_registry`;
`app/api/v1/platform.py` only reads it.
"""

import pkgutil
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType
from typing import TYPE_CHECKING, Any

from app.core.router_inspect import flatten

if TYPE_CHECKING:
    from fastapi import FastAPI

_MODULES_PREFIX = "app.modules."


def _owning_module(dotted: str) -> str | None:
    """`"app.modules.patch.api"` -> `"patch"`; anything outside `app.modules.*`
    (kernel routes, kernel job types) -> `None`, so it is counted against no
    module rather than misattributed to one."""
    if not dotted.startswith(_MODULES_PREFIX):
        return None
    return dotted[len(_MODULES_PREFIX) :].split(".", 1)[0]


@contextmanager
def capture_job_type_registrations() -> Iterator[Counter[str]]:
    """Monkeypatch `app.jobs.registry.register` for the duration of the
    `with` block, returning a live `Counter` of job-type registrations per
    owning module -- attributed by which module's own code CALLED
    `register(JobType(...))`, not by `JobType.params_model.__module__` or
    `.result_handler.__module__`, both of which are unreliable here:

    * `inventory.collect` is re-registered by
      `app.modules.inventory.handler.register_job_type` using
      `InventoryCollectParams`, a class defined in `app.jobs.registry` itself
      (the kernel's bare registration). Attributing by the params model's
      module would count it as kernel, not `inventory`.
    * `health.check.now` and `remote.session.open` register with no
      `result_handler` at all, so attributing by the handler's module drops
      them silently.

    The call site is the one signal that is always correct: it is the frame
    executing the `register(JobType(...))` line, inside that module's own
    `register_job_types()`/`register_job_type()`.

    Meant to wrap exactly one call -- `app.modules.register_all(app)` inside
    `create_app` -- so the patch is single-threaded and restored before any
    request can be served. It is not safe to hold open across a request.
    """
    import app.jobs.registry as job_registry

    counts: Counter[str] = Counter()
    original_register = job_registry.register

    def _tracking_register(job_type: Any) -> None:
        frame: FrameType | None = sys._getframe(1)
        while frame is not None:
            owner = _owning_module(frame.f_globals.get("__name__", ""))
            if owner is not None:
                counts[owner] += 1
                break
            frame = frame.f_back
        original_register(job_type)

    job_registry.register = _tracking_register
    try:
        yield counts
    finally:
        job_registry.register = original_register


def build_module_registry(app: "FastAPI", job_type_counts: Counter[str]) -> list[dict[str, Any]]:
    """Route and job-type counts per module, read from the app that was JUST
    built (routes) and the counts captured while it was built (job types).

    The module NAMES themselves come from `app.modules`' own subpackages
    (`pkgutil.iter_modules`), not a hand-maintained constant -- a module with
    zero counted routes or job types (neither exists today, but nothing here
    should assume it can't) still appears, with `0`.

    ROUTES are attributed by `route.endpoint.__module__` -- not by URL prefix
    (a previous audit in this repo attributed by prefix and read 0 for a
    module whose routes mount elsewhere) and not by counting decorator sites
    (the same audit read 3 routes where 10 existed).
    """
    from fastapi.routing import APIRoute

    import app.modules as modules_pkg

    names = sorted(
        name for _finder, name, is_pkg in pkgutil.iter_modules(modules_pkg.__path__) if is_pkg
    )

    route_counts: Counter[str] = Counter()
    for _path, route in flatten(app.router):
        if not isinstance(route, APIRoute):
            continue
        owner = _owning_module(getattr(route.endpoint, "__module__", "") or "")
        if owner is not None:
            route_counts[owner] += 1

    return [
        {
            "name": name,
            "routes": route_counts.get(name, 0),
            "job_types": job_type_counts.get(name, 0),
        }
        for name in names
    ]
