"""Router traversal for introspection: the platform module registry, the AI
tool surface, route-gating and roadmap-status measurements all need to walk
the LIVE router rather than trust a hand-maintained list.

Lives in `app/` -- not `tests/` -- because `app/api/v1/platform.py` needs it
at application startup to build the module registry (see
`app/core/module_registry.py`), and `app/` can never import from `tests/`:
`tests` does not exist in the deployed image (`pyproject.toml`'s
`packages = ["app"]`; `deploy/backend.Dockerfile` copies only `app`,
`migrations` and `alembic.ini`), so an import that only resolves in a checkout
with the test tree present raises `ModuleNotFoundError` at request time in
production while every test -- run from a checkout that HAS `tests/` --
stays green. `tests/tool_surface.py` re-exports `flatten` from here so the
existing tool-surface, route-gating and roadmap tests are unaffected.
"""

from typing import Any


def flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every route under `router`, recursively, as `(full_path, route)`.

    This codebase's `register()` wraps included routers, so `app.routes`
    alone finds NOTHING under `/api/v1` -- a walker that misses every route
    reports a clean sweep over an empty set.
    """
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found
