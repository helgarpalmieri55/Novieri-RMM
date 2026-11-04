"""The console's list of global routes is the server's (phase 3 design §1.2).

`frontend/src/lib/globalRoutes.json` names the routes the console never sends
`X-Client-Id` on: with the header, a scope of every client is impossible, so
`require_permission_on_every_client` answers 403 to the very person allowed
to make the change. Which routes those are is declared once, in
`GLOBAL_WRITES` and `GLOBAL_READS` (`tests/test_global_writes.py`), and
checked there against the router in both directions. The console's file is
GENERATED from them (`tests/export_console_global_routes.py`), never typed,
and this file fails CI when the two disagree -- so a new global route that
nobody regenerated for stops CI, not a technician in production.

Two checks, because they fail for different reasons: the routes themselves
(a route missing or extra: the message names it), then the bytes (the file
was edited by hand, or reordered).
"""

import json
import re
from typing import Any

from app.core.router_inspect import flatten
from tests.export_console_global_routes import (
    API_PREFIX,
    CONSOLE_ROUTES_JSON,
    REGENERATE,
    console_global_routes,
    render,
)
from tests.test_global_writes import GLOBAL_READS, GLOBAL_WRITES, WRITE_METHODS

#: A `{param}` segment, as `frontend/src/lib/globalRoutes.ts` reads one.
_PARAM = re.compile(r"^\{[^/{}]+\}$")


def _key(route: dict[str, str]) -> tuple[str, str]:
    return route["method"], route["path"]


def test_the_console_list_is_exactly_the_global_routes() -> None:
    on_disk = json.loads(CONSOLE_ROUTES_JSON.read_text(encoding="utf-8"))
    expected = console_global_routes()
    missing = sorted(set(map(_key, expected)) - set(map(_key, on_disk)))
    extra = sorted(set(map(_key, on_disk)) - set(map(_key, expected)))
    assert not missing and not extra, (
        f"{CONSOLE_ROUTES_JSON.name} is not GLOBAL_WRITES | GLOBAL_READS "
        f"(tests/test_global_writes.py). Missing: {missing}. Extra: {extra}. {REGENERATE}"
    )


def test_the_console_list_is_the_generated_file() -> None:
    assert CONSOLE_ROUTES_JSON.read_text(encoding="utf-8") == render(console_global_routes()), (
        f"{CONSOLE_ROUTES_JSON.name} differs from what the export writes "
        f"(order or format). {REGENERATE}"
    )


def test_the_export_is_every_declared_route_relative_to_the_api() -> None:
    """Non-vacuity: one entry per declared route, none lost to a collision,
    each under `/api/v1` and named without it -- the path `ApiClient`
    requests, which is what the console compares."""
    routes = console_global_routes()
    declared = set(GLOBAL_WRITES) | set(GLOBAL_READS)
    assert len(routes) == len(declared) == len(GLOBAL_WRITES) + len(GLOBAL_READS)
    assert {(r["method"], API_PREFIX + r["path"]) for r in routes} == declared
    assert all(r["path"].startswith("/") and not r["path"].startswith(API_PREFIX) for r in routes)
    assert {r["method"] for r in routes} <= WRITE_METHODS | {"GET"}
    assert routes == sorted(routes, key=lambda r: (r["path"], r["method"]))


def _may_meet(template: str, route_path: str) -> bool:
    """Could one request path match both the console's global `template`
    and the router's `route_path`? The console's rule (`globalRoutes.ts`):
    same number of segments, a `{param}` is one non-empty segment. A router
    `{param}` takes any value, so it can also meet a literal of the template."""
    wanted, served = template.split("/"), route_path.split("/")
    return len(wanted) == len(served) and all(
        bool(_PARAM.match(w)) or bool(_PARAM.match(s)) or w == s
        for w, s in zip(wanted, served, strict=True)
    )


def test_no_other_route_looks_global_to_the_console(api_env: Any) -> None:
    """The console drops `X-Client-Id` on any request that matches a global
    template, and the server then runs it under the person's whole scope
    instead of the client they chose. So no route outside the list may be
    reachable by a path a global template also matches (final review A4,
    F3T2 item 2: none today, out of 561).
    FALSIFIER: a route such as `DELETE /agent/releases/stale` beside the
    global `DELETE /agent/releases/{release_id}`, or a global template whose
    literal a non-global route's `{param}` can fill."""
    templates = [(r["method"], r["path"]) for r in console_global_routes()]
    served = sorted(
        (method, path.removeprefix(API_PREFIX))
        for path, route in flatten(api_env.app.router)
        if path.startswith(API_PREFIX + "/")
        for method in (getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}
    )
    # Non-vacuity: the walk found the router, and the rule finds every
    # global route under its own template.
    assert len(served) > len(templates) > 0
    assert set(templates) <= set(served)
    looks_global = [
        f"{method} {path} meets {template}"
        for method, path in served
        if (method, path) not in set(templates)
        for t_method, template in templates
        if t_method == method and _may_meet(template, path)
    ]
    assert looks_global == []
