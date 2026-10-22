"""The five properties, enforced over the whole generated reporting surface
(spec §0.10, §12.6, §12.7).

**WHY THIS FILE EXISTS SEPARATELY FROM THE MODULE THAT GENERATES THE ROUTES.**
`api.py`'s loop is the *mechanism* that supplies the five properties; this is
the *measurement* of the built application, and the two are only the same thing
if nothing else in the process can add a reporting route. Nothing can today --
which is exactly the claim §0.10 makes ("a report that reaches the router has
the five properties because there is no code path by which it could not") and
exactly the kind of claim that stops being true silently.

**THE VACUITY GUARD IS THE FIRST TEST AND IT IS NOT A FORMALITY.** FastAPI
0.139.2's `include_router` appends a lazy `_IncludedRouter` whose own `.path` is
`None`, so a naive `[r for r in app.routes if r.path.startswith(...)]` finds
ZERO reporting routes and every assertion in this file passes over an empty
list. `_flatten` below is copied from `tests/test_ai_admin_registry.py`, and
`test_the_sweep_covers_every_generated_route` asserts the EXACT count
(`2 * len(REGISTRY) + 1`) before anything else runs.

**`/api/v1/reports` IS NOT THIS MODULE'S PREFIX ALONE.**
`app/modules/security_logs/api.py:127` mounts `GET /api/v1/reports/access.csv`
-- a foreign route, gated on `security_logs.read`, registered before this module
existed. So `_report_routes` discriminates by the ENDPOINT'S DEFINING MODULE,
never by a path prefix. A `path.startswith("/api/v1/reports")` sweep folds that
route in, and the exact count above then fails for a reason that has nothing to
do with reporting -- or, worse, someone "fixes" it by loosening the count to
`>=` and the guard is gone.

**THIS SWEEP IS DELIBERATELY SCOPED TO REPORTING'S OWN ROUTES.** Do not
generalise it to "every route in the app is gated". That assertion goes red
TODAY on `GET /api/v1/remote/sessions`, `GET /api/v1/remote/sessions/{id}` (both
genuinely gated, but through `remote`'s own forked `require_any_permission`,
which `route_permission` cannot read) and on `GET /api/v1/me`. Whoever hit it
would add an allowlist, and a visible finding would become a permanently
suppressed one. The finding is filed against the remote module's own cycle
(spec §16); reporting's contribution is that it never forks the gate and never
hides one from the router.
"""

import inspect
from typing import Any

import pytest
from fastapi.routing import APIRoute
from starlette.responses import PlainTextResponse, Response, StreamingResponse

from app.core.rbac import route_permission

# IMPORTED FOR ITS SIDE EFFECT. Every parametrization below is over `REGISTRY`,
# which is populated when the DEFINITIONS package is imported -- and
# parametrization is evaluated at COLLECTION time, before any fixture builds the
# app that would have imported it. Without this line every sweep here collects
# an empty parameter set and the file reports success having asserted nothing.
from app.modules.reporting import definitions as _definitions  # noqa: F401
from app.modules.reporting import service as service_module
from app.modules.reporting.registry import _NEVER_A_TOOL, REGISTRY, export_path
from app.modules.reporting.schema import (
    CatalogueEntry,
    ReportCatalogue,
    ReportMeta,
    ReportPage,
    ReportSource,
)

_CATALOGUE_PATH = "/api/v1/reports"


# --------------------------------------------------------------------------- #
# Router flattening. COPIED FROM `tests/test_ai_admin_registry.py::_flatten`.
# --------------------------------------------------------------------------- #


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


def _report_routes(app: Any) -> dict[str, APIRoute]:
    """Routes THIS MODULE generated, discriminated by the endpoint's defining
    module (see the module docstring for why not by path)."""
    return {
        path: route
        for path, route in _flatten(app.router)
        if isinstance(route, APIRoute)
        and getattr(route.endpoint, "__module__", "").startswith("app.modules.reporting")
    }


def _matches(app: Any, method: str, path: str) -> list[APIRoute]:
    return [
        route
        for full_path, route in _flatten(app.router)
        if full_path == path
        and isinstance(route, APIRoute)
        and method in (route.methods or set())
    ]


# --------------------------------------------------------------------------- #
# The vacuity guards. BOTH RUN BEFORE ANYTHING ELSE ASSERTS OVER THE SWEEP.
# --------------------------------------------------------------------------- #


async def test_the_registry_is_not_empty() -> None:
    assert REGISTRY, "the registry is empty; every parametrized test here is vacuous"


async def test_the_sweep_covers_every_generated_route(api_env: Any) -> None:
    """EXACT, not `>=`. A `>=` hides a missing route, and a missing route is
    precisely the failure a completeness sweep exists to find.

    FALSIFY by replacing `_report_routes`'s module discriminator with
    `path.startswith("/api/v1/reports")`: `security_logs`' foreign
    `GET /api/v1/reports/access.csv` joins the set, the count is one too high,
    and the failure message names a path from another module.
    """
    routes = _report_routes(api_env.app)
    assert len(routes) == 2 * len(REGISTRY) + 1, sorted(routes)
    assert set(routes) == (
        {f"/api/v1/reports/{key}" for key in REGISTRY}
        | {export_path(key) for key in REGISTRY}
        | {_CATALOGUE_PATH}
    )
    # And the foreign route on the shared prefix is NOT in the set -- the
    # premise of the discriminator, asserted rather than assumed.
    assert "/api/v1/reports/access.csv" not in routes
    assert len(_matches(api_env.app, "GET", "/api/v1/reports/access.csv")) == 1


# --------------------------------------------------------------------------- #
# The five properties, on the report routes.
# --------------------------------------------------------------------------- #


def _report_route(api_env: Any, key: str) -> APIRoute:
    return _report_routes(api_env.app)[f"/api/v1/reports/{key}"]


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_property_1_method_honest(api_env: Any, key: str) -> None:
    """A report route is a `GET` **and it writes nothing**.

    The method alone is not the property -- `GET` in this codebase is already
    not side-effect-free (`auth_context` stamps `last_login_at` on every
    authenticated request). What makes a REPORT method-honest is that its own
    path performs no write, and §0.7 turns that into an enforceable statement:
    running a report writes no audit event. The structural half is asserted
    here, over the module the report path actually runs through; the behavioural
    half is `test_reporting_export.py::
    test_a_run_writes_nothing_and_an_export_writes_exactly_one`.

    FALSIFY by adding a `record()` call to `execute_report` "for symmetry with
    the export": this goes red on the import, and the behavioural test goes red
    on the count. Measured -- that mutation was run, and both did.
    """
    route = _report_route(api_env, key)
    assert route.methods == {"GET"}

    # Read through the imported module rather than a relative path, so this
    # cannot silently read nothing when pytest is invoked from another cwd --
    # `Path("...").read_text()` on a missing file raises, but a moved file with
    # the same name would not.
    service = inspect.getsource(service_module)
    assert "audit_service" not in service, (
        "the report execution path imports the audit service; running a report "
        "writes nothing (§0.7) and a write there puts every dashboard refresh on "
        "the audit chain's per-org advisory lock"
    )


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_property_2_gated_on_reporting_read(api_env: Any, key: str) -> None:
    """`route_permission(route) == "reporting.read"`, NOT merely "not None".

    "Not None" is satisfied by a route gated on any code at all, including one
    narrower or wider than intended. The stricter form is what makes the AI
    registry's `test_every_recorded_permission_matches_the_router` able to
    record a reporting tool's gate without reading this module's source.
    """
    assert route_permission(_report_route(api_env, key)) == "reporting.read"


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_property_3_typed_response_model(api_env: Any, key: str) -> None:
    """`ReportPage[ThisReportsRow]` -- never `dict[str, Any]`, never a bare
    `Response`/`StreamingResponse`/`PlainTextResponse`.

    The ROW TYPE is asserted, not just the envelope: `ReportPage[Any]` would
    satisfy "is a ReportPage" while describing nothing, and the point of
    property 3 is that a consumer can know the shape of a row without running
    the report.
    """
    definition = REGISTRY[key]
    model = _report_route(api_env, key).response_model
    assert isinstance(model, type), model
    assert not issubclass(model, Response), model
    assert not issubclass(model, (PlainTextResponse, StreamingResponse)), model
    assert issubclass(model, ReportPage), model
    assert model is not ReportPage, "the envelope is unparametrized and describes no row"
    assert model.model_fields["rows"].annotation == list[definition.row_model]  # type: ignore[name-defined]


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_property_4_bounded_by_a_limit_with_an_le(api_env: Any, key: str) -> None:
    """A `limit` with an `le=`, on EVERY report route regardless of response
    shape (§10.2 -- the ai-admin boundedness test only asks this of
    `list[Model]` responses, and reporting's envelope is an object, so the
    existing test would not ask).

    **THE `le` IS READ OFF THE PARAMS MODEL, NOT OFF `route.dependant.
    query_params`.** The route binds the whole params model as ONE parameter
    named `params` (asserted below, because that is the fact that makes the
    obvious implementation of this test wrong): a sweep over `query_params`
    looking for a field called `limit` finds nothing and passes vacuously.
    """
    definition = REGISTRY[key]
    route = _report_route(api_env, key)

    bound = [field.name for field in route.dependant.query_params]
    assert bound == ["params"], bound
    (field,) = route.dependant.query_params
    assert field.field_info.annotation is definition.params

    limit = definition.params.model_fields["limit"]
    les = [getattr(constraint, "le", None) for constraint in limit.metadata]
    assert definition.max_limit in les, limit.metadata
    assert definition.max_limit > 0


_DESCRIBED_MODELS = [
    ReportPage,
    ReportMeta,
    ReportSource,
    ReportCatalogue,
    CatalogueEntry,
]


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_property_5_self_describing_route(api_env: Any, key: str) -> None:
    """A REAL `summary=`, not the docstring fallback and not the name-derived
    OpenAPI default.

    The premise is asserted first: the generated handler has NO docstring, so
    FastAPI's `description or cleandoc(endpoint.__doc__ or "")` fallback has
    nothing to fall back to -- which means a non-empty `description` on this
    route can only have come from the generation loop. Without that premise
    stated, "the route has a description" is satisfied by a docstring somebody
    wrote for a different audience.
    """
    definition = REGISTRY[key]
    route = _report_route(api_env, key)

    assert route.endpoint.__doc__ is None, "the premise below rests on this"
    assert (route.summary or "").strip() == definition.summary
    assert (route.description or "").strip() == definition.description
    assert route.summary != route.name.replace("_", " ").title()
    assert len(definition.summary) > 20 and len(definition.description) > 100


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_property_5_every_row_field_is_described(key: str) -> None:
    """`Field(description=...)` on EVERY field of the row model -- not a class
    docstring, which no OpenAPI consumer and no tool registry can read per
    field."""
    fields = REGISTRY[key].row_model.model_fields
    assert fields, "an empty row model would make this vacuous"
    undescribed = [name for name, f in fields.items() if not (f.description or "").strip()]
    assert undescribed == [], undescribed


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_property_5_every_params_field_is_described(key: str) -> None:
    """The same, for the parameters -- a self-describing route describes what
    you may ask it, not only what it answers."""
    fields = REGISTRY[key].params.model_fields
    assert fields
    undescribed = [name for name, f in fields.items() if not (f.description or "").strip()]
    assert undescribed == [], undescribed


@pytest.mark.parametrize("model", _DESCRIBED_MODELS, ids=lambda m: m.__name__)
async def test_property_5_every_envelope_field_is_described(model: Any) -> None:
    """`ReportMeta`, `ReportSource`, `ReportPage` and the catalogue models.

    Measured against this repo when the module was specified: there was exactly
    ONE `Field(description=...)` in any module's `schema.py`. A generic envelope
    discharges the obligation once for every future report; this is what stops
    it being discharged and then quietly undone.
    """
    fields = model.model_fields
    assert fields
    undescribed = [name for name, f in fields.items() if not (f.description or "").strip()]
    assert undescribed == [], undescribed


async def test_the_catalogue_route_is_self_describing_too(api_env: Any) -> None:
    """The catalogue is the one route with a handler docstring, so it is the one
    place the fallback could actually fire. Asserted as DIFFERENT from the
    cleaned docstring, which is the only way to tell an explicit description
    from the fallback here."""
    route = _report_routes(api_env.app)[_CATALOGUE_PATH]
    doc = inspect.cleandoc(route.endpoint.__doc__ or "")
    assert doc, "the premise: this endpoint does have a docstring"
    assert (route.summary or "").strip()
    assert (route.description or "").strip()
    assert route.description != doc, "the description is the docstring fallback"
    assert route.response_model is ReportCatalogue
    assert route_permission(route) == "reporting.read"
    assert route.methods == {"GET"}


# --------------------------------------------------------------------------- #
# The exports: excluded from properties 1 and 3 BY DESIGN, and denylisted for it
# (§10.3).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_an_export_route_is_denylisted_rather_than_expected_to_pass(
    api_env: Any, key: str
) -> None:
    """An export is a `GET` that writes (the `report.exported` audit event) and
    answers `text/csv`, so it fails properties 1 and 3 on the honest reading.

    THE EXCLUSION IS ASSERTED AS REAL, not merely declared: the route genuinely
    has no response model and genuinely answers `PlainTextResponse`. A denylist
    entry for a route that would have passed anyway is a comment.
    """
    route = _report_routes(api_env.app)[export_path(key)]
    assert ("GET", export_path(key)) in _NEVER_A_TOOL

    assert route.response_model is None, route.response_model
    assert route.response_class is PlainTextResponse

    # The properties an export DOES keep: the same gate as its report, and the
    # same conjunction. An export is never a weaker gate than the page it
    # exports; `tests/test_reporting_export.py` proves that on the wire.
    assert route.methods == {"GET"}
    assert route_permission(route) == "reporting.read"


async def test_the_never_a_tool_list_covers_exactly_the_export_routes() -> None:
    """A new report whose export nobody denylisted is a RED TEST NAMING IT.

    `_NEVER_A_TOOL` is written out as literals rather than derived from
    `REGISTRY` on purpose: a list computed from the registry grows itself to
    cover a new report and can never be wrong, which makes it a comment rather
    than a tripwire. This is the assertion that keeps the literal honest.
    """
    assert _NEVER_A_TOOL, "an empty denylist forbids nothing"
    assert _NEVER_A_TOOL == {("GET", export_path(key)) for key in REGISTRY}


@pytest.mark.parametrize(("method", "path"), sorted(_NEVER_A_TOOL))
async def test_denylist_entries_resolve_to_real_routes(
    api_env: Any, method: str, path: str
) -> None:
    """The liveness discipline from
    `tests/test_ai_admin_registry.py::test_denylist_entries_resolve_to_real_routes`.

    A denylist entry whose path has drifted denies NOTHING, and the test above
    would then be comparing two equally-stale sets. If this goes red, the
    tripwire stopped being one -- fix the pair, do not delete it.
    """
    found = _matches(api_env.app, method, path)
    assert len(found) == 1, f"denylist entry {method} {path} matches {len(found)} routes"
    assert found[0].endpoint.__module__.startswith("app.modules.reporting")


async def test_the_denylist_is_not_vacuously_satisfied(api_env: Any) -> None:
    """`sorted(_NEVER_A_TOOL)` over an empty frozenset collects zero cases and
    the parametrized test above reports success. Asserted separately, so the
    liveness sweep can never be green by being empty."""
    assert len(_NEVER_A_TOOL) == len(REGISTRY) >= 4
