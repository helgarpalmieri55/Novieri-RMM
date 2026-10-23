"""`job_type -> permission` is DATA, and it agrees with the prose it replaces.

docs/modules/ai-automation.md §0.11, §10.2.

FIVE PROPERTIES, AND THE FOURTH IS THE ONE THAT EARNS THE FILE.

1. The two maps PARTITION the live job-type registry. A module registering a
   new job type breaks a test rather than picking a side by accident.
2. Every mapped value is a real permission code. A renamed permission otherwise
   turns the map into a lie that reads fine.
3. Every mapped value actually GATES A LIVE ROUTE. Without this, a plausible
   code that gates nothing passes 1 and 2 -- and the check §9.2 builds on it
   would be asking about a permission no route requires.
4. The map AGREES WITH `MODULE_GOVERNED_REASONS`' English, entry by entry.
   That prose is where the mapping lived before this module existed, and it is
   still what an operator reads in a 403. Two truths that can disagree is the
   defect `is_destructive`'s docstring exists to prevent; this test is what
   makes them one truth. It also asserts, EXPLICITLY, which entries do not
   parse -- so "the parse found nothing" cannot masquerade as agreement.
5. Non-vacuity floors, because tests 1-4 are all satisfiable by empty maps the
   moment the registry fixture breaks.
"""

import re
from typing import Any

import pytest
from fastapi.routing import APIRoute

from app.core.rbac import PERMISSIONS, route_permission
from app.jobs.direct_dispatch import DIRECTLY_DISPATCHABLE_JOB_TYPES, MODULE_GOVERNED_REASONS
from app.jobs.dispatch_permissions import (
    DISPATCH_PERMISSIONS,
    NO_HUMAN_DISPATCH_PATH,
    dispatch_permission_for,
)

#: The trailing "(some.permission)" every `MODULE_GOVERNED_REASONS` message
#: that names a route ends with. Anchored to the END of the message on purpose:
#: several messages mention a permission mid-sentence ("assign a health profile
#: (health.admin)"), and matching those would make this test agree with the
#: wrong half of the prose.
_TRAILING_CODE = re.compile(r"\(([a-z_]+(?:\.[a-z_]+)+)\)\s*$")

#: The four job types whose message names NO route, because none exists. Named
#: as a literal so a message that stops parsing shows up as a disagreement here
#: rather than as a silently smaller set of checked entries.
_EXPECTED_UNPARSED = {
    "health.policy.apply",
    "agent.cert.renew",
    "agent.cert.install",
    # Interruption's own job type (2026-08-22-job-lifecycle-design.md §5). The
    # cancel ROUTE is gated on the TARGET job type's dispatch permission, so
    # `job.cancel` itself has no route and no permission of its own -- which is
    # exactly what puts it in this set.
    "job.cancel",
    # Dispatched only by the patch module's restart sweep, inside the window.
    "patch.reboot",
}


def _live_registry() -> set[str]:
    """Every job type an agent can be asked to run, in one process.

    COPIED FROM `tests/test_direct_dispatch.py::_live_registry`. The registry is
    process-global, so `registered_names()` otherwise depends on which modules
    an earlier test happened to import, and a partition assertion that only
    holds when the file runs alone is the silence it exists to break.
    """
    from app.jobs import registry
    from app.modules.remote.jobtypes import register_job_type as register_remote_job_type
    from app.worker import register_worker_job_types

    register_worker_job_types()
    register_remote_job_type()
    return set(registry.registered_names())


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """`(full path, route)` for every real route reachable from `router`.

    COPIED FROM `tests/test_email_intake_api.py::_flatten`. `include_router` in
    this FastAPI appends a lazy `_IncludedRouter` wrapper whose own `.path` is
    `None` rather than flattening, so `app.routes` contains NONE of the routes
    this test is about. A flattener that silently missed them would make test 3
    vacuous.
    """
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + (getattr(route, "path", "") or ""), route))
    return found


def _gated_permissions(app: Any) -> set[str]:
    """Every permission code some live route is actually gated on.

    Recovered by `route_permission`'s own reflection over the dependency
    closure -- the same mechanism every module's RBAC-completeness test uses --
    so a route gated through `require_permission` is found wherever it was
    registered, and a hand-maintained list of routes cannot go stale here.
    """
    found = {
        code
        for _, route in _flatten(app.router)
        if isinstance(route, APIRoute) and (code := route_permission(route))
    }
    # A flattener that found nothing would make the test below pass for every
    # value, including a code that gates nothing at all.
    assert len(found) >= 40, f"the route walk recovered only {len(found)} gates; it is vacuous"
    return found


# --------------------------------------------------------------------------- #
# 1. The partition, against the live registry.
# --------------------------------------------------------------------------- #


def test_the_two_maps_partition_the_live_job_type_registry() -> None:
    registered = _live_registry()
    decided = set(DISPATCH_PERMISSIONS) | set(NO_HUMAN_DISPATCH_PATH)

    assert registered - decided == set(), (
        "these registered job types have no dispatch-permission decision. Deny-by-default "
        "already refuses them at the AI confirm gate, but a job type nobody classified is a "
        "job type nobody decided about -- put it in one map or the other"
    )
    assert decided - registered == set(), "these named job types are not registered"
    assert set(DISPATCH_PERMISSIONS) & set(NO_HUMAN_DISPATCH_PATH) == set()


def test_the_maps_are_not_vacuous() -> None:
    """An empty map satisfies every assertion above the moment `_live_registry`
    stops finding anything."""
    assert len(DISPATCH_PERMISSIONS) >= 10, len(DISPATCH_PERMISSIONS)
    assert NO_HUMAN_DISPATCH_PATH
    assert all(reason.strip() for reason in NO_HUMAN_DISPATCH_PATH.values())


# --------------------------------------------------------------------------- #
# 2-3. Every value is a real permission, gating a real route.
# --------------------------------------------------------------------------- #


def test_every_value_is_a_registered_permission_code() -> None:
    unknown = sorted(set(DISPATCH_PERMISSIONS.values()) - set(PERMISSIONS))
    assert unknown == [], f"not permission codes: {unknown}"


async def test_every_value_gates_at_least_one_live_route(api_env: Any) -> None:
    """A code that exists in `PERMISSIONS` but gates nothing would pass test 2
    and still make §9.2's second check meaningless.

    One test over the whole map rather than one per job type: the application
    is built once, and the failure message names every disagreement instead of
    the first.
    """
    gated = _gated_permissions(api_env.app)
    ungated = [
        f"{job_type} -> {code}"
        for job_type, code in sorted(DISPATCH_PERMISSIONS.items())
        if code not in gated
    ]
    assert ungated == [], (
        f"these map entries name a permission that gates no route in this application: "
        f"{ungated}; the check §9.2 builds on them would be asking about a permission "
        f"nobody needs"
    )


# --------------------------------------------------------------------------- #
# 4. The map agrees with the prose it replaces.
# --------------------------------------------------------------------------- #


def test_the_map_agrees_with_direct_dispatch_prose() -> None:
    """THE HIGHEST-VALUE TEST IN THIS FILE.

    `MODULE_GOVERNED_REASONS`' messages are what an operator is shown when the
    kernel route refuses their job type, and each one that has a route ends by
    naming that route and its permission. This binds the new data to that
    English, and the failure message names both sides.
    """
    from app.jobs.direct_dispatch import RESERVED_JOB_TYPES

    parsed: dict[str, str] = {}
    unparsed: set[str] = set()
    # RESERVED TYPES ARE OUTSIDE THIS COMPARISON, and deliberately outside BOTH
    # maps in `app/jobs/dispatch_permissions.py`. This module answers "which
    # permission would a human need to dispatch this by hand", and its `None`
    # is a REFUSAL at every call site. A job type that is not registered has no
    # dispatch to confirm.
    #
    # THE TWO PUSH TYPES ARE NO LONGER RESERVED (cycle B2 registered them), so
    # they now flow through this comparison like every other module type: they
    # have a human route -- `agent.push.run` -- and `DISPATCH_PERMISSIONS` names
    # it. That does NOT make them AI-confirmable: `CLAUDE.md` invariant 2a clause
    # (e) forbids any automated actor and `NON_APPROVABLE_REASONS` refuses the
    # type upstream, so an AI can never PROPOSE one and `confirm_action` is never
    # reached for it. The map answers the human-route question; the approvable
    # gate answers the machine-actor one, and they are different questions.
    for job_type, message in MODULE_GOVERNED_REASONS.items():
        if job_type in RESERVED_JOB_TYPES:
            continue
        match = _TRAILING_CODE.search(message)
        if match is None:
            unparsed.add(job_type)
        else:
            parsed[job_type] = match.group(1)

    # Asserted explicitly, so a message that stops parsing cannot shrink the
    # checked set into silent agreement.
    assert unparsed == _EXPECTED_UNPARSED, unparsed
    assert len(parsed) == len(MODULE_GOVERNED_REASONS) - len(_EXPECTED_UNPARSED) - len(
        RESERVED_JOB_TYPES
    )

    disagreements = [
        f"{job_type}: direct_dispatch says {code!r}, the map says "
        f"{DISPATCH_PERMISSIONS.get(job_type)!r}"
        for job_type, code in sorted(parsed.items())
        if DISPATCH_PERMISSIONS.get(job_type) != code
    ]
    assert disagreements == [], "; ".join(disagreements)


def test_the_unparsed_entries_are_exactly_the_no_route_entries() -> None:
    """The other half of the same claim: a message that names no permission
    must be a job type with no human dispatch path, not a job type somebody
    forgot to finish the sentence for."""
    assert _EXPECTED_UNPARSED == set(NO_HUMAN_DISPATCH_PATH)
    for job_type in _EXPECTED_UNPARSED:
        assert "no route for it by design" in MODULE_GOVERNED_REASONS[job_type], job_type


def test_the_kernel_route_types_are_mapped_to_the_kernel_gate() -> None:
    """`DIRECTLY_DISPATCHABLE_JOB_TYPES` have no owning module, so the kernel
    route IS the owning route and `jobs.dispatch` IS the permission."""
    for job_type in DIRECTLY_DISPATCHABLE_JOB_TYPES:
        assert DISPATCH_PERMISSIONS[job_type] == "jobs.dispatch", job_type


# --------------------------------------------------------------------------- #
# 5. Deny by default.
# --------------------------------------------------------------------------- #


def test_dispatch_permission_for_returns_none_for_an_unclassified_job_type() -> None:
    assert dispatch_permission_for("nonexistent.job.type") is None
    assert dispatch_permission_for("") is None


@pytest.mark.parametrize("job_type", sorted(NO_HUMAN_DISPATCH_PATH))
def test_dispatch_permission_for_returns_none_for_a_no_route_job_type(job_type: str) -> None:
    """`None` here means "there is no route, so there is no grant to check",
    which every caller must treat as a refusal -- never as "no permission
    needed"."""
    assert dispatch_permission_for(job_type) is None


@pytest.mark.parametrize("job_type", sorted(DISPATCH_PERMISSIONS))
def test_dispatch_permission_for_returns_the_mapped_code(job_type: str) -> None:
    assert dispatch_permission_for(job_type) == DISPATCH_PERMISSIONS[job_type]
