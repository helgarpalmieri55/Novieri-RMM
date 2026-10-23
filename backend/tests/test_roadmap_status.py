"""THE ROADMAP'S STATUS CLAIMS, PINNED TO THE TREE.

`docs/06-roadmap.md` lists 18 phases. Until 2026-08-14 exactly THREE of its
rows carried a "BUILT" marker, while every one of the 18 had a backend package,
live routes, tests and a frontend feature folder. Anyone reading that table to
decide what to build next was reading a document that understated its own
project by fifteen phases.

That is the same defect this codebase keeps finding in itself -- a status claim
nothing forces anyone to re-check -- and it is the reason
`test_ai_tool_surface.py` exists for the tool-property measurement. This file is
the same treatment for the roadmap.

WHAT IS PINNED, AND WHY EACH SIGNAL IS A FACT RATHER THAN A PROXY. An earlier
draft of this measurement got two numbers wrong and would have written both into
a document:

  * it counted `@router.get` DECLARATION SITES, so `reporting` -- whose routes
    are generated in a loop -- read as 3 when the router mounts 10;
  * its test heuristic matched file NAMES against the package name, so
    `security_logs` read as 0 tests when `test_security*.py` is five files.

Both are the same mistake. So: routes come from the LIVE ROUTER, attributed by
the endpoint function's defining module (matching on a URL prefix was also a
guess -- every module mounts at `/api/v1` and the segments come from the routes,
which read `inventory` as 0); and tests are files that IMPORT the package.

NO DATABASE. The app is built for route inspection only, exactly as
`tests/tool_surface.py` does it, so this runs anywhere.
"""

import pathlib
import subprocess

from fastapi.routing import APIRoute

from tests.tool_surface import build_router_only_app, flatten

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

# Roadmap phase -> (backend package, frontend feature folder).
#
# THE MAPPING IS JUDGEMENT AND LIVES HERE SO IT CAN BE ARGUED WITH rather than
# buried in a loop. Phase 18 is the AI Automation LAYER: its substrate is
# `app/core/ai/`, and its two personas are `ai_copilot` (technician) and
# `ai_portal` (end-user tier-1 resolver). `ai_copilot` is named here as its
# representative package; `ai_portal` is listed in PHASE_18_ALSO so it is not
# miscounted as unphased.
PHASES: tuple[tuple[int, str, str, str], ...] = (
    (1, "Inventory", "inventory", "inventory"),
    (2, "Security logs", "security_logs", "security"),
    (3, "Remote control", "remote", "remote"),
    (4, "Patch management", "patch", "patch"),
    (5, "Application deployment", "deploy", "deploy"),
    (6, "Backup engine", "backup", "backup"),
    (7, "Provisioning & imaging", "provisioning", "provisioning"),
    (8, "IT documentation", "documentation", "docs"),
    (9, "Change management", "change_mgmt", "change"),
    (10, "BCP test reports", "bcp", "bcp"),
    (11, "Licenses management", "licenses", "licenses"),
    (12, "Subscriptions management", "subscriptions", "subscriptions"),
    (13, "IT budget & expense", "budget", "budget"),
    (14, "Service Desk", "service_desk", "service-desk"),
    (15, "IPAM", "ipam", "ipam"),
    (16, "Endpoint health & alerting", "health", "health"),
    (17, "AI admin agent", "ai_admin", "ai-admin"),
    (18, "AI Automation layer", "ai_copilot", "ai-automation"),
)
PHASE_18_ALSO = {"ai_portal"}

# Shipped backend packages the roadmap names in NO phase. Measured 2026-08-14.
#
# This is the roadmap's OTHER staleness and the more surprising one: seven
# modules' worth of shipped surface that its phase list does not mention at all.
# `reporting`'s own spec §16.1 says so in its first line -- "There is no brief.
# No docs/modules/reporting.md, no roadmap phase." -- and it is not alone.
#
# Pinned as a SET, not a count: a new module that nobody adds to the roadmap
# turns this red naming itself, which is the whole point.
#
# `agent_distribution` (added 2026-08-18) is the one entry here that is
# unphased ON PURPOSE rather than by drift, and the roadmap says so: it is a
# PHASE 0 deliverable (`docs/06-roadmap.md`, "Platform Kernel"), the other half
# of enrollment, and the phase list this test maps covers phases 1-18 only. It
# is named in the roadmap; it is simply not in a numbered phase.
#
# `user_admin` (added 2026-08-19) is unphased for the same reason as
# `agent_distribution`, not by drift: it is PHASE 0 work. The roadmap's
# "Platform Kernel" names tenancy, RBAC and audit as Phase 0 deliverables, and
# this package is the administrative surface over exactly those — creating a
# user and assigning them roles is the kernel's own vocabulary, not a business
# module sitting on top of it. The phase list this test maps covers phases
# 1-18, so a Phase 0 deliverable has no numbered row to occupy.
#
# `agent_push` (added 2026-08-26) is unphased on the same ground as
# `agent_distribution`, and is named in the roadmap's Phase 0 list beside it:
# both answer "how does the agent reach a machine that has no agent", which is
# enrollment's other half rather than a business capability. It is the module
# invariant 2a governs, and 2a is a clause about a path to UNENROLLED machines
# -- there is no numbered phase for that, because every numbered phase assumes
# an endpoint the control plane can already reach.
#
# `keycloak_events` (added 2026-08-18) is unphased for a different and narrower
# reason: it is NOT A BUSINESS MODULE. It adds no routes, no job types and no
# frontend folder -- it is a worker sweep implementing one clause of
# `docs/modules/console-auth/spec.md` §4, which is sign-in and deployment work
# rather than a numbered phase. It lives under `app/modules/` because that is
# where a self-contained package with its own migration belongs, not because it
# is a module in the roadmap's sense. A roadmap row for it would claim a
# deliverable that does not exist.
EXPECTED_UNPHASED = frozenset(
    {
        "agent_distribution",
        "agent_push",
        # Self-update (spec-self-update.md): keeping enrolled agents current,
        # the third agent-distribution path. Phase 0 work like the two above.
        "agent_update",
        "keycloak_events",
        "email_intake",
        "notification",
        # Answers review 2026-09-23 finding 5; not a numbered roadmap phase.
        "people",
        "portal",
        "reporting",
        "site_server",
        "user_admin",
        "sla",
    }
)


def _routes_by_package() -> dict[str, int]:
    """Every mounted route, attributed to the module that DEFINED its endpoint."""
    counts: dict[str, int] = {}
    for _path, route in flatten(build_router_only_app().router):
        if not isinstance(route, APIRoute):
            continue
        module = getattr(route.endpoint, "__module__", "") or ""
        if not module.startswith("app.modules."):
            continue
        pkg = module.split(".")[2]
        counts[pkg] = counts.get(pkg, 0) + len((route.methods or set()) - {"HEAD", "OPTIONS"})
    return counts


def _tests_importing(pkg: str) -> int:
    found = subprocess.run(
        ["grep", "-rl", f"app.modules.{pkg}", str(REPO / "backend/tests")],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    return len(found)


def test_every_roadmap_phase_has_shipped_surface() -> None:
    """All 18, on all four signals.

    If this fails with a phase missing a signal, the roadmap has a row the tree
    does not back -- which is the honest direction for this test to fail in, and
    the one the old three-BUILT-markers table could never have shown.
    """
    routes = _routes_by_package()
    missing: list[str] = []
    for num, name, pkg, frontend in PHASES:
        why = []
        if not (REPO / "backend/app/modules" / pkg).is_dir():
            why.append("no backend package")
        if not routes.get(pkg):
            why.append("no mounted routes")
        if not _tests_importing(pkg):
            why.append("no tests import it")
        if not (REPO / "frontend/src/modules" / frontend).is_dir():
            why.append("no frontend folder")
        if why:
            missing.append(f"phase {num} ({name}, {pkg}): {', '.join(why)}")
    assert not missing, "roadmap phases without shipped surface:\n  " + "\n  ".join(missing)


def test_no_module_ships_without_a_roadmap_phase() -> None:
    """The roadmap's other staleness, and the one that grows silently.

    A module can be built, mounted, tested and shipped without anybody adding a
    row to `docs/06-roadmap.md` -- six have been. That is not necessarily wrong
    (some may belong inside an existing phase's scope), but it must be a
    DECISION rather than a drift, so the set is pinned: add a module and this
    goes red naming it, and you either add the roadmap row or add it here with
    a reason.
    """
    phased = {pkg for _n, _name, pkg, _fe in PHASES} | PHASE_18_ALSO
    shipped = {
        d.name
        for d in (REPO / "backend/app/modules").iterdir()
        if d.is_dir() and not d.name.startswith("__")
    }
    unphased = shipped - phased
    assert unphased == EXPECTED_UNPHASED, (
        f"newly unphased (add a roadmap row, or add here with a reason): "
        f"{sorted(unphased - EXPECTED_UNPHASED)}; "
        f"no longer unphased (update the roadmap and this set): "
        f"{sorted(EXPECTED_UNPHASED - unphased)}"
    )


def test_the_route_attribution_is_not_vacuous() -> None:
    """The guard against this file passing over nothing.

    Every assertion above rests on `_routes_by_package`. If the flattener or the
    attribution broke, it would return an empty mapping and
    `test_every_roadmap_phase_has_shipped_surface` would fail loudly -- but a
    future edit that made it return a DEFAULT would not. This pins the shape.
    """
    routes = _routes_by_package()
    assert len(routes) >= 20, f"attribution found only {len(routes)} packages"
    assert sum(routes.values()) >= 300, f"only {sum(routes.values())} routes attributed"
