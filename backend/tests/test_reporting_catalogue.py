"""The catalogue route (spec §6.2, §4.1, §4.3, §11).

`GET /api/v1/reports`, gated on `reporting.read` ALONE. It exposes no org data --
the catalogue is platform code, compiled into the image -- and it exists to turn
an opaque 403 into a permissions conversation: a report you cannot run is not a
secret, it is a request you can take to whoever grants the code.

**NOTHING IS HIDDEN (§11).** A caller holding `reporting.read` and nothing else
sees EVERY report, every one of them `entitled: false`, each naming the codes it
still needs. A catalogue that silently omitted the reports you cannot run would
give the same 200 to an under-permissioned operator as to a fully entitled one
with a smaller deployment, and there would be no way to tell those apart from
the outside.

**THE TWO CALLERS IN THE ENTITLEMENT TEST DIFFER ONLY IN THEIR GRANTS.** Same
org, same seeded data, same `reporting.read`; only the source codes vary. A
fixture that also changed the org or the data would make the entitlement
computation unobservable -- it could be reading anything.
"""

from typing import Any

from fastapi.routing import APIRoute

from app.core.rbac import _walk_dependant, route_permission

# IMPORTED FOR ITS SIDE EFFECT. `REGISTRY` is populated when the DEFINITIONS
# package is imported, which otherwise happens only when `api_env` builds the
# app -- after collection. Without this line the module-level reads of
# `REGISTRY` below run against an empty mapping.
from app.modules.reporting import definitions as _definitions  # noqa: F401
from app.modules.reporting.registry import REGISTRY
from tests.reporting_fixtures import reporting_user

CATALOGUE = "/api/v1/reports"


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """COPIED FROM `tests/test_ai_admin_registry.py::_flatten`. FastAPI
    0.139.2's `include_router` appends a lazy `_IncludedRouter` whose own
    `.path` is `None`; a naive `app.routes` filter finds NOTHING and every
    assertion below would pass over an empty list."""
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


def _catalogue_route(app: Any) -> APIRoute:
    matched = [
        route
        for path, route in _flatten(app.router)
        if path == CATALOGUE
        and isinstance(route, APIRoute)
        and "GET" in (route.methods or set())
    ]
    assert len(matched) == 1, f"{len(matched)} routes claim {CATALOGUE}"
    return matched[0]


async def _entries(api_env: Any, org_id: Any, *codes: str) -> dict[str, dict[str, Any]]:
    _, sub = await reporting_user(api_env, org_id, *codes)
    response = await api_env.client.get(
        CATALOGUE, headers=await api_env.headers(org_id, sub)
    )
    assert response.status_code == 200, response.text
    return {entry["key"]: entry for entry in response.json()["reports"]}


# --------------------------------------------------------------------------- #
# The route itself (§6.2, §12.7).
# --------------------------------------------------------------------------- #


async def test_the_registry_holds_the_four_v1_reports() -> None:
    """FIRST, and the count is a LITERAL. §0.12 fixes the v1 set at four; a
    definition that failed to import would otherwise be a shorter catalogue
    nobody notices, and every assertion below would still pass."""
    assert len(REGISTRY) == 4, sorted(REGISTRY)
    assert set(REGISTRY) == {
        "fleet-posture",
        "patch-compliance",
        "backup-coverage",
        "sla-attainment",
    }


async def test_the_catalogue_is_gated_on_reporting_read_alone(api_env: Any) -> None:
    """§6.2. `route_permission` recovers `reporting.read` by reflection, and the
    route carries NO `require_report_sources` -- the catalogue is the one route
    in this module without the conjunction, because it reads no org data and
    naming the codes you lack is the whole point of it."""
    route = _catalogue_route(api_env.app)
    assert route_permission(route) == "reporting.read"

    gate_shaped = [
        dep
        for dep in _walk_dependant(route.dependant)
        if getattr(getattr(dep, "call", None), "__qualname__", "")
        == "require_permission.<locals>.dependency"
    ]
    assert len(gate_shaped) == 1, f"{len(gate_shaped)} require_permission dependencies"

    conjunction = [
        dep
        for dep in _walk_dependant(route.dependant)
        if getattr(getattr(dep, "call", None), "__qualname__", "")
        == "require_report_sources.<locals>.dependency"
    ]
    assert conjunction == [], "the catalogue must not carry the source conjunction"


async def test_the_catalogue_route_is_a_self_describing_get(api_env: Any) -> None:
    route = _catalogue_route(api_env.app)
    assert route.methods == {"GET"}
    assert (route.summary or "").strip(), "a real summary=, not the docstring fallback"
    assert (route.description or "").strip()


async def test_a_caller_without_reporting_read_is_refused(api_env: Any) -> None:
    """The gate, asserted. A caller holding every SOURCE code and not
    `reporting.read` cannot list the catalogue either."""
    org_id = await api_env.create_org("rep-cat-gate")
    every_source = sorted({code for key in REGISTRY for code in REGISTRY[key].requires})
    _, sub = await reporting_user(api_env, org_id, *every_source)
    response = await api_env.client.get(
        CATALOGUE, headers=await api_env.headers(org_id, sub)
    )
    assert response.status_code == 403, response.text
    assert "reporting.read" in response.text


# --------------------------------------------------------------------------- #
# Nothing is hidden (§11), and entitlement is per caller (§4.1).
# --------------------------------------------------------------------------- #


async def test_reporting_read_alone_sees_every_report_and_none_entitled(
    api_env: Any,
) -> None:
    """§4.3's claim -- `reporting.read` "grants NOTHING on its own" -- made
    legible instead of merely true. Every report is listed, every one refused,
    and every one says what it would take."""
    org_id = await api_env.create_org("rep-cat-bare")
    entries = await _entries(api_env, org_id, "reporting.read")

    assert set(entries) == set(REGISTRY), sorted(entries)
    for key, entry in entries.items():
        assert entry["entitled"] is False, key
        assert entry["missing_permissions"] == sorted(REGISTRY[key].requires), key
        assert entry["requires"] == sorted(REGISTRY[key].requires), key
        assert entry["summary"].strip() and entry["description"].strip(), key


async def test_entitlement_follows_the_callers_grants_and_only_those(
    api_env: Any,
) -> None:
    """THE TWO CALLERS DIFFER ONLY IN TWO GRANTS. Same org, same everything
    else. `fleet-posture` becomes entitled and the other three do not, so the
    computation is observably per-report and per-caller rather than a constant.

    `missing_permissions` is RECOMPUTED HERE from `REGISTRY`, not read back off
    the response -- a field that quietly became a copy of `requires` would still
    equal itself.
    """
    org_id = await api_env.create_org("rep-cat-partial")
    bare = await _entries(api_env, org_id, "reporting.read")
    granted = {"reporting.read", "devices.read", "health.read"}
    partial = await _entries(api_env, org_id, *sorted(granted))

    assert set(partial) == set(bare) == set(REGISTRY)
    assert partial["fleet-posture"]["entitled"] is True
    assert partial["fleet-posture"]["missing_permissions"] == []
    assert bare["fleet-posture"]["entitled"] is False

    for key, entry in partial.items():
        expected = sorted(REGISTRY[key].requires - granted)
        assert entry["missing_permissions"] == expected, key
        assert entry["entitled"] is (expected == []), key

    still_refused = {key for key, entry in partial.items() if not entry["entitled"]}
    assert still_refused == set(REGISTRY) - {"fleet-posture"}, still_refused


async def test_the_catalogue_exposes_no_org_data(api_env: Any) -> None:
    """The catalogue is platform code compiled into the image, so two callers
    holding the same codes in DIFFERENT orgs -- one of which has devices,
    tickets and backups seeded and the other of which is empty -- get byte-
    identical answers.

    An entry that leaked a count, a name or an id would differ here.
    """
    from tests.reporting_fixtures import seed_device

    populated = await api_env.create_org("rep-cat-full")
    empty = await api_env.create_org("rep-cat-empty")
    await seed_device(api_env.admin_engine, populated, hostname="something")

    codes = ("reporting.read", "devices.read", "health.read")
    first = await _entries(api_env, populated, *codes)
    second = await _entries(api_env, empty, *codes)
    assert first == second
    # The premise: the two orgs really are different. If the seeding silently
    # failed, the equality above would be vacuous.
    assert populated != empty
    assert first["fleet-posture"]["entitled"] is True
