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

from tests.export_console_global_routes import (
    API_PREFIX,
    CONSOLE_ROUTES_JSON,
    REGENERATE,
    console_global_routes,
    render,
)
from tests.test_global_writes import GLOBAL_READS, GLOBAL_WRITES, WRITE_METHODS


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
