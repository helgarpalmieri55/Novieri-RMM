"""The six tool properties, implemented ONCE.

`docs/modules/ai-admin/spec.md` §0.3 defines what makes a route "clean enough
to be a tool". Two tests need that definition: the GATE, scoped to `REGISTRY`
(`test_ai_read_tools.py`), and the SWEEP, over every `GET` route
(`test_ai_tool_surface.py`). A property implemented twice is a property that
will be implemented differently, and the gate and the measurement disagreeing
is precisely the failure this module exists to prevent.

WHY PROPERTY 6 EXISTS AT ALL. Properties 1-5 measure DOCUMENTATION. `scrub.py`
demands something they never stated: a CLOSED key space. `ReadTool.response_fields`
is a frozen literal that `test_declared_response_fields_match_the_live_model`
pins equal to `every_field_name(response_model)` -- exactly, both directions. So
a model whose payload can carry a key that is not a model field name has NO
LEGAL DECLARATION: declaring the key turns that test red as `stale`, and omitting
it blocks every result at runtime, silently, forever. Five routes shipped passing
all five properties and unable to become tools, and nothing noticed.
"""

import pathlib
import tempfile
from typing import Any, get_args, get_origin

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.core.ai.scrub import looks_like_secret_key
from app.core.config import Settings
from app.core.rbac import route_permission
from app.core.router_inspect import flatten
from app.main import create_app

# `flatten` now lives in `app/core/router_inspect.py` -- `app.core.
# module_registry` needs it at application startup to build the platform
# module registry, and `app/` can never import from `tests/` (it does not
# exist in the deployed image). Re-exported here, unchanged, so every test
# that does `from tests.tool_surface import flatten` keeps working.
__all__ = [
    "api_v1_get_routes",
    "build_router_only_app",
    "every_field_name",
    "flatten",
    "inner_model",
    "model_openness",
    "route_property_failures",
]


def inner_model(response_model: Any) -> tuple[bool, type[BaseModel] | None]:
    """Verbatim from `test_ai_read_tools.py::_inner_model`."""
    if isinstance(response_model, type) and issubclass(response_model, BaseModel):
        return True, response_model
    if get_origin(response_model) is list:
        (arg,) = get_args(response_model)
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return True, arg
    return False, None


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    for arg in get_args(annotation):
        found.extend(_nested_models(arg))
    return found


def every_field_name(model: type[BaseModel], seen: set[Any] | None = None) -> set[str]:
    """Verbatim from `test_ai_read_tools.py::_every_field_name`: every key a
    serialized `model` can contain, at any depth."""
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for field_name, field in model.model_fields.items():
        names.add(field_name)
        for nested in _nested_models(field.annotation):
            names |= every_field_name(nested, seen)
    return names


def model_openness(
    model: type[BaseModel], seen: set[Any] | None = None, path: str = ""
) -> list[str]:
    """Every reason this model's key space is NOT closed. Empty means closed.

    Two failure modes, and both are fatal in production rather than cosmetic:

      * an OPEN DICT -- `dict[K, V]` at any depth. Its keys are data. NO
        CARVE-OUT FOR `dict[str, SomeModel]`: `scrub` walks KEYS, so
        `{"<hostname>": {...}}` is exactly as unregisterable as
        `{"<hostname>": 3}`. Stating the rule without an exception also gives
        this function one branch instead of two.
      * a SECRET-SHAPED NAME -- `looks_like_secret_key` is word-level, so
        `report_key` and `approved_action_key` match on `key`. That over-block
        is deliberate (see `scrub.py`); a field carrying such a name blocks its
        tool's entire result forever, so it is an openness failure here.
    """
    seen = seen if seen is not None else set()
    if model in seen:
        return []
    seen.add(model)
    reasons: list[str] = []
    for field_name, field in model.model_fields.items():
        here = f"{path}.{field_name}" if path else field_name
        if looks_like_secret_key(field_name):
            reasons.append(f"secret-shaped name `{here}`")
        # Walk the WHOLE annotation, not just its origin: the dict may be inside
        # a `list[...]`, a union, or an `Optional`.
        stack: list[Any] = [field.annotation]
        while stack:
            annotation = stack.pop()
            if get_origin(annotation) is dict:
                reasons.append(f"open dict at `{here}`")
            stack.extend(get_args(annotation))
        for nested in _nested_models(field.annotation):
            reasons.extend(model_openness(nested, seen, here))
    return reasons


def route_property_failures(route: APIRoute) -> list[str]:
    """§0.3's properties 2-6 against one route, as a list of failure labels.

    Property 1 (method-honest) is a fact about the REGISTRY entry -- it compares
    `ReadTool.method` with the route -- so it is checked by the gate, not here.
    Property 4's `max_limit` half is likewise a registry property. This function
    answers only what a ROUTE can be judged on alone, which is what lets the
    sweep run over routes that are not tools.
    """
    failures: list[str] = []
    if route_permission(route) is None:
        failures.append("P2 gated")
    typed, inner = inner_model(route.response_model)
    if not typed:
        failures.append("P3 typed")
    query_names = {p.name for p in route.dependant.query_params}
    if get_origin(route.response_model) is list and "limit" not in query_names:
        failures.append("P4 bounded")
    if not (route.summary or route.description or "").strip():
        failures.append("P5 self-describing")
    if inner is not None:
        undocumented = [n for n, f in inner.model_fields.items() if not f.description]
        if undocumented and not (inner.__doc__ or "").strip():
            failures.append("P5 self-describing")
    if inner is None or model_openness(inner):
        failures.append("P6 closed")
    return failures


def build_router_only_app() -> FastAPI:
    """An app built for ROUTE INSPECTION, with no database behind it.

    The properties are static analysis of the router -- no query runs -- so
    requiring the `api_env` fixture (and therefore a Postgres cluster) would
    make the measurement unrunnable in exactly the environments where somebody
    would want to check it. `create_async_engine` does not connect at
    construction, so the URLs below are never dialled.
    """
    tmp = pathlib.Path(tempfile.mkdtemp())
    unreachable = "postgresql+asyncpg://u:p@127.0.0.1:1/db"
    return create_app(
        Settings(
            database_url=unreachable,
            database_admin_url=unreachable,
            database_portal_url=unreachable,
            oidc_issuer="https://idp.invalid",
            oidc_jwks_url="https://idp.invalid/jwks",
            ca_dir=tmp / "ca",
            signing_key_path=tmp / "job-signing.key",
            redis_url="redis://127.0.0.1:6379/9",
        )
    )


def api_v1_get_routes(app: Any) -> list[tuple[str, APIRoute]]:
    """Every `GET` route under `/api/v1`, as `(full_path, route)`."""
    return [
        (path, route)
        for path, route in flatten(app.router)
        if isinstance(route, APIRoute)
        and "GET" in (route.methods or set())
        and path.startswith("/api/v1")
    ]
