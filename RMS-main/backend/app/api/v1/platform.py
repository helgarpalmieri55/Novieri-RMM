"""Operator-facing platform status.

GATED on `platform.status.read`. This is where the detail `/readyz` withholds
lives: reason codes, durations, heartbeat ages, 48h uptime and the module
registry.

MODULES ARE LISTED, NOT PROBED (spec §1). The registry reports what is
REGISTERED -- name, route count, job-type count -- because that is what it
measures. A per-module health contributor is a later extension point, and
until it exists nothing here may imply a module is healthy.

THE REGISTRY IS COMPUTED ONCE, AT STARTUP -- not here. `app.main.create_app`
builds it (`app.core.module_registry.build_module_registry`) against the one
real app it is already constructing, and stores it on
`app.state.module_registry`. This route only reads that. It used to call into
`tests/tool_surface.py` to build a SECOND app on every request -- `tests` does
not exist in the deployed image, so that raised `ModuleNotFoundError` at
request time in production, leaked a second set of engines/Redis
client/vault/signer/CA per request on a route this page polls every 15s, and
monkeypatched a global with no protection against two overlapping requests.
See `app.core.module_registry` for the full reasoning this replaced, moved
there with the code.
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import AuthContext, require_permission
from app.api.probes import probe_label
from app.core.health.heartbeat import read_heartbeats
from app.core.health.types import Status

router = APIRouter()


class ProbeOut(BaseModel):
    """One dependency probe, with its identifier and its printed name kept
    apart -- and they ARE different things.

    `key` is the identifier and it never changes: `platform_health_sample.probe`
    rows are written with it, `/readyz` reports it to a load balancer, and
    `app.api.probes.WORKER_SAMPLED_PROBES` is derived from it, so renaming one
    orphans every stored sample.

    `name` is what `PlatformStatusPage` PRINTS. It used to be the identifier,
    which is how `keycloak_events` came to name the identity vendor in a table
    on an operator's screen -- from a Python dict key, so the frontend's
    user-visible-string guard could never have seen it and neither could a
    backend equivalent that only read literals
    (`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md`
    §1.3). `app.api.probes.PROBE_LABELS` is the source, and
    `tests/test_no_vendor_names_backend.py` holds it clean.
    """

    key: str
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
    """Full operator-facing platform status: probe detail `/readyz` withholds,
    per-process detail (Task 6), and the module registry."""

    overall: Status
    probes: list[ProbeOut]
    processes: list[dict[str, Any]]
    modules: list[ModuleOut]


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
    """The detail `/readyz` withholds: every probe's reason code and duration,
    per-process detail (Task 6), and the module registry (names, route
    counts, job-type counts -- registered, not health-checked)."""
    del ctx
    registry = request.app.state.health_registry
    results = await registry.run(request.app.state)
    worst = Status.OK
    for r in results:
        if r.status is Status.DOWN:
            worst = Status.DOWN
            break
        if r.status is Status.DEGRADED:
            worst = Status.DEGRADED
    # Task 6: per-instance process heartbeats through Redis -- the only way
    # this route learns about the gateway and the worker, and about its own
    # replicas. See `app.core.health.heartbeat`.
    processes = await read_heartbeats(request.app.state.redis, now=time.time())
    return PlatformStatusOut(
        overall=worst,
        probes=[
            ProbeOut(
                key=r.name,
                name=probe_label(r.name),
                status=r.status,
                duration_ms=r.duration_ms,
                reason=r.reason.value if r.reason else None,
                detail=r.detail,
            )
            for r in results
        ],
        processes=[
            {
                "process": p.process,
                "instances": p.instances,
                "healthy_instances": p.healthy_instances,
                "oldest_age_s": p.oldest_age_s,
                "status": p.status.value,
                "version": p.version,
            }
            for p in processes
        ],
        modules=[ModuleOut(**m) for m in request.app.state.module_registry],
    )
