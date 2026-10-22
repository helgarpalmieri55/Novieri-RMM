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

from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Request, Response, status

from app.core.health.backup_status import (
    probe_backup,
    probe_backup_offsite,
    probe_restore_drill,
)
from app.core.health.probes import (
    probe_ca,
    probe_jwks,
    probe_keycloak_events,
    probe_objectstore,
    probe_postgres_admin,
    probe_postgres_app,
    probe_redis,
    probe_signing,
    probe_vault,
)
from app.core.health.registry import ProbeRegistry
from app.core.health.types import Probe, Status
from app.core.i18n import N_, tr_marked

# Postgres because nothing is servable without it. Redis because it is the job
# engine's result bus -- without it the API accepts dispatches whose results
# can never be delivered. NOTHING ELSE. `test_the_hard_set_has_exactly_this_
# membership` pins it.
HARD_PROBES = frozenset({"postgres.app", "redis"})

# The single source of truth for "which probes exist and what runs
# them" -- `build_registry()` iterates this, and `WORKER_SAMPLED_PROBES`
# below is derived from its keys rather than a second hand-typed list that
# could drift.
_ALL_PROBES: dict[str, Probe] = {
    "postgres.app": probe_postgres_app,
    "redis": probe_redis,
    "postgres.admin": probe_postgres_admin,
    "objectstore": probe_objectstore,
    "vault": probe_vault,
    "jwks": probe_jwks,
    "signing": probe_signing,
    "ca": probe_ca,
    # SOFT, and `ok` when unconfigured. Forwarding Keycloak's credential events
    # is optional; a deployment that has not enabled it is not unhealthy. What
    # this probe exists to prevent is the OTHER silence -- configured, not
    # running, and nobody told.
    "keycloak_events": probe_keycloak_events,
    # SOFT, and `ok` when the host has no backup timer. Read from files the
    # host scripts write (app/core/health/backup_status.py).
    "backup.nightly": probe_backup,
    "backup.offsite": probe_backup_offsite,
    "backup.drill": probe_restore_drill,
}

# The probes a Celery worker can meaningfully SAMPLE into history -- every
# probe except `jwks`. Only the API process ever constructs a `JwksCache`
# (`app/main.py`'s startup); a worker's state has no `jwks` attribute at all,
# so running `probe_jwks` there would not be measuring the IdP being down --
# it would be measuring an attribute this process never had a reason to hold,
# and `probe_jwks`'s unconditional `state.jwks._keys` read turns that absence
# into an `AttributeError`, which the registry's catch-all reports as
# `Status.DOWN` / `ReasonCode.UNREACHABLE`. See `app.worker.sample_health_once`
# and `test_jwks_is_excluded_because_a_worker_reading_would_be_a_false_down`.
WORKER_SAMPLED_PROBES = frozenset(_ALL_PROBES) - {"jwks"}

# WHAT THE STATUS PAGE PRINTS -- a SECOND string, deliberately, and never the
# key.
#
# The keys above are identifiers and they are load-bearing as identifiers:
# `platform_health_sample.probe` rows are written with them
# (`app.worker.sample_health_once`), `/readyz` reports them to a load balancer,
# and `WORKER_SAMPLED_PROBES` is derived from them. Renaming one silently
# orphans every stored sample, so none of them changes.
#
# But `/api/v1/platform/status` sends the name straight to
# `PlatformStatusPage`, which prints it in a table -- so `keycloak_events` put
# the identity vendor's name on an operator's screen, from a Python dict key.
# A buyer does not know what Keycloak is and must never need to
# (`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §1.3,
# and the buyer test in §0). No scan of user-visible string LITERALS could ever
# have found it; `tests/test_no_vendor_names_backend.py` checks these labels
# directly for that reason.
#
# Every probe gets one, not just the offending one: a mapping with a single
# entry is a special case waiting to be deleted, and "postgres.app" was never
# a sentence a buyer should have to parse either.
PROBE_LABELS: dict[str, str] = {
    "postgres.app": N_("Application database"),
    "redis": N_("Job result bus"),
    "postgres.admin": N_("Database administration connection"),
    "objectstore": N_("Object storage"),
    "vault": N_("Secret vault"),
    "jwks": N_("Sign-in key cache"),
    "signing": N_("Job signing key"),
    "ca": N_("Certificate authority"),
    "keycloak_events": N_("Sign-in event forwarding"),
    "backup.nightly": N_("Nightly platform backup"),
    "backup.offsite": N_("Off-host backup copy"),
    "backup.drill": N_("Restore drill"),
}


def probe_label(name: str) -> str:
    """The human name for a probe key, falling back to the key itself.

    A fallback rather than a `KeyError`: a probe registered without a label is
    a cosmetic gap, and taking the status page down over one would be a worse
    answer than showing an identifier for a release.
    `test_no_vendor_names_backend.py` is what stops the gap lasting -- it
    requires a label for every registered probe.
    """
    label = PROBE_LABELS.get(name)
    return tr_marked(label) if label is not None else name


router = APIRouter()


def build_registry(names: Iterable[str] | None = None) -> ProbeRegistry:
    """Build a registry over `names` (default: every probe). Passing
    `WORKER_SAMPLED_PROBES` builds the narrower registry the health sampler
    runs; the live `/readyz` and `/api/v1/platform/status` routes call this
    with no argument and keep seeing every one."""
    registry = ProbeRegistry()
    for name in _ALL_PROBES if names is None else names:
        registry.register(name, _ALL_PROBES[name], hard=name in HARD_PROBES)
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
