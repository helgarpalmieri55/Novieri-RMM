"""Generated tool descriptions (spec §4.1).

The property: a description is ASSEMBLED FROM THE ROUTE, so it cannot drift from
the API without a test seeing it. Hand-written prose can, and no test can catch
prose drift -- which is the whole reason these are generated.
"""

from typing import Any

import pytest
from fastapi.routing import APIRoute

from app.core.ai.describe import describe_tool, response_field_lines, route_prose
from app.core.ai.tools import REGISTRY
from tests.test_ai_read_tools import _flatten


@pytest.fixture()
def app(api_env: Any) -> Any:
    return api_env.app


def _route(app: Any, method: str, path: str) -> APIRoute:
    (route,) = [
        r
        for p, r in _flatten(app.router)
        if p == path and isinstance(r, APIRoute) and method in (r.methods or set())
    ]
    return route


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_description_carries_the_routes_own_prose_and_every_field(
    app: Any, name: str
) -> None:
    """§4.1's two halves in one assertion: the route's prose, and every field of
    the response model. A generator that dropped the schema would still produce
    a plausible-looking sentence, so the field names are what pin it."""
    tool = REGISTRY[name]
    route = _route(app, tool.method, tool.path)
    spec = describe_tool(tool, route)

    prose = route_prose(route)
    assert prose, f"{name}: the route carries no summary or docstring"
    assert prose in spec.description

    lines = response_field_lines(route)
    assert lines, f"{name}: no response fields were rendered"
    for line in lines:
        assert line in spec.description


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_description_is_prose_and_schema_not_a_python_repr(app: Any, name: str) -> None:
    """A dumped `FieldInfo` in model context is implementation detail leaking
    into the prompt, and reads as noise to the model."""
    tool = REGISTRY[name]
    spec = describe_tool(tool, _route(app, tool.method, tool.path))
    for marker in ("Field(", "FieldInfo(", "PydanticUndefined", "<class '", "annotation="):
        assert marker not in spec.description, f"{name}: description contains {marker!r}"


async def test_a_hint_is_additive_never_a_replacement(app: Any) -> None:
    """§4.1: `hint` is ADDITIVE to the generated schema. A description that is
    the hint ALONE is the hand-written prose this design rejected, arriving
    through the escape hatch."""
    tool = REGISTRY["health.alerts"]
    assert tool.hint, "this test needs a tool that carries a hint"
    route = _route(app, tool.method, tool.path)
    spec = describe_tool(tool, route)

    assert tool.hint in spec.description
    # ... and the generated halves survive alongside it.
    assert route_prose(route) in spec.description
    assert response_field_lines(route)[0] in spec.description


async def test_a_tool_without_a_hint_still_describes_itself(app: Any) -> None:
    """The hint is optional; dropping it must not empty the description."""
    from dataclasses import replace

    tool = replace(REGISTRY["health.alerts"], hint="")
    spec = describe_tool(tool, _route(app, tool.method, tool.path))
    assert route_prose(_route(app, tool.method, tool.path)) in spec.description
    assert "Returns a list of AlertOut with fields:" in spec.description


async def test_the_clamp_is_stated_to_the_model(app: Any) -> None:
    """§10.2: the model must know the result is bounded, or it will present 50
    rows as the whole answer."""
    tool = REGISTRY["health.alerts"]
    spec = describe_tool(tool, _route(app, tool.method, tool.path))
    assert f"At most {tool.max_limit} items" in spec.description


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_input_schema_is_the_closed_args_schema(app: Any, name: str) -> None:
    tool = REGISTRY[name]
    spec = describe_tool(tool, _route(app, tool.method, tool.path))
    assert spec.input_schema == tool.args.model_json_schema()
    assert spec.input_schema.get("additionalProperties") is False
