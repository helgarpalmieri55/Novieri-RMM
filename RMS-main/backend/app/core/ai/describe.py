"""Tool descriptions are GENERATED, not written (spec §4.1).

A tool's model-visible description is assembled mechanically from three things
the route already owns: its prose (`summary`, falling back to the endpoint
docstring, which is what FastAPI puts in `description`), the `args` model's JSON
Schema, and the `response_model`'s JSON Schema trimmed to field names, types and
descriptions.

WHY NOT HAND-WRITTEN PROSE: it drifts from the API silently and **no test can
catch prose drift**. A generated description is worse writing and it is right.
`ReadTool.hint` is the escape hatch -- ONE short sentence, ADDITIVE to the
generated schema and never a replacement for it. A tool needing more than one
sentence of hint is a signal the module's API is not clean enough (§0.3), and
the fix belongs in that module.

This module emits no Python reprs. A dumped `FieldInfo` in a tool description is
a leak of implementation detail into model context and reads as noise to the
model; `test_ai_admin_describe.py` asserts on both.
"""

from collections.abc import Mapping
from typing import Any, get_args, get_origin

from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.core.ai.types import ReadTool, ToolSpec

_MAX_LISTED_FIELDS = 60


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """`(full path, route)` for every real route reachable from `router`.

    FastAPI 0.139.2's `include_router` appends a single lazy `_IncludedRouter`
    wrapper whose own `.path` is `None` rather than flattening, and
    `app.routes` IS `app.router.routes` -- so filtering either collection by a
    path prefix yields an empty list forever. Descending through
    `.original_router` and re-applying `.include_context.prefix` is what yields
    the real `APIRoute` objects under their real, fully-prefixed paths.

    This is the same walk `tests/test_ai_admin_registry.py` uses to prove every
    registry entry resolves. It lives here as well because the ORCHESTRATOR
    needs it at runtime: a tool's description is generated from its live route
    (§4.1), so a tool whose route cannot be found must be a loud failure rather
    than a tool offered to the model with an empty description.
    """
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


def find_route(app: Any, tool: ReadTool) -> APIRoute:
    """The live `APIRoute` a tool names. Raises if it is missing or ambiguous.

    Loud on purpose: an allowlist entry whose path has drifted is a registry
    defect, and the visible failure belongs at description time rather than as
    a silently under-described tool the model then misuses.
    """
    matches = [
        route
        for path, route in _flatten(app.router)
        if path == tool.path
        and isinstance(route, APIRoute)
        and tool.method in (route.methods or ())
    ]
    if len(matches) != 1:
        raise LookupError(  # i18n-exempt: startup registry check, logged
            f"tool {tool.name!r} resolves to {len(matches)} routes for "
            f"{tool.method} {tool.path}; the registry and the router disagree"
        )
    return matches[0]


def tool_specs(app: Any, names: Any, *, registry: Mapping[str, ReadTool]) -> list[ToolSpec]:
    """`ToolSpec`s for `names`, in registry order, each generated from the live
    route. Names not in the registry are ignored -- the prefilter passes tool
    names, and a name that is not a tool is not a tool.

    THE REGISTRY IS A PARAMETER, NOT AN IMPORT. This module is kernel code and
    must not know which persona is asking (§0.1); the registry it describes is
    the calling persona's own `PersonaProfile.tools`.
    """
    wanted = set(names)
    return [
        describe_tool(tool, find_route(app, tool))
        for name, tool in registry.items()
        if name in wanted
    ]


def route_prose(route: APIRoute) -> str:
    """The route's own one-line summary.

    FastAPI stores an explicit `summary=` on `route.summary` and falls back to
    the endpoint docstring for `route.description`. This repo currently sets
    `summary=` on NO route, so the docstring is where the prose actually lives;
    taking the first paragraph of whichever exists keeps the description a
    sentence rather than a page.
    """
    text = (route.summary or route.description or "").strip()
    if not text:
        return ""
    first, _, _ = text.partition("\n\n")
    return " ".join(first.split())


def _response_model(route: APIRoute) -> tuple[type[BaseModel] | None, bool]:
    model = route.response_model
    if isinstance(model, type) and issubclass(model, BaseModel):
        return model, False
    if get_origin(model) is list:
        (arg,) = get_args(model)
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg, True
    return None, False


def _type_name(annotation: Any) -> str:
    name = getattr(annotation, "__name__", None)
    return name if isinstance(name, str) else str(annotation).replace("typing.", "")


def response_field_lines(route: APIRoute) -> list[str]:
    """`name (type)` or `name (type): description`, one per response field."""
    model, _ = _response_model(route)
    if model is None:
        return []
    lines = []
    for field_name, field in list(model.model_fields.items())[:_MAX_LISTED_FIELDS]:
        line = f"- {field_name} ({_type_name(field.annotation)})"
        if field.description:
            line += f": {field.description.strip()}"
        lines.append(line)
    return lines


def describe_tool(tool: ReadTool, route: APIRoute) -> ToolSpec:
    """The `ToolSpec` handed to the provider for one allowlisted route."""
    model, is_list = _response_model(route)
    parts: list[str] = []

    prose = route_prose(route)
    if prose:
        parts.append(prose)
    if tool.hint:
        # Additive, always after the generated prose so it can never read as a
        # replacement for it.
        parts.append(tool.hint)

    shape = "a list of" if is_list else "one"
    model_name = model.__name__ if model is not None else "object"
    returns = [f"Returns {shape} {model_name} with fields:"]
    returns.extend(response_field_lines(route))
    parts.append("\n".join(returns))

    if tool.max_limit is not None:
        parts.append(
            f"At most {tool.max_limit} items are returned; a larger `limit` is "
            f"clamped and the result reports `truncated`."
        )

    return ToolSpec(
        name=tool.name,
        description="\n\n".join(parts),
        input_schema=tool.args.model_json_schema(),
    )
