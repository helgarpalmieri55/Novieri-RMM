"""Write the console's list of global routes (phase 3 design §1.2).

`GLOBAL_WRITES` and `GLOBAL_READS` (`tests/test_global_writes.py`) are the one
declaration of the routes that need a scope of every client. The console must
never send `X-Client-Id` on them, and reads them from
`frontend/src/lib/globalRoutes.json`, which this module writes:

    cd backend && .venv/bin/python -m tests.export_console_global_routes

`tests/test_console_global_routes.py` fails CI when the file and the lists
disagree. Not a pytest file (no `test_` prefix): run it, do not collect it.
"""

import json
from pathlib import Path

from tests.test_global_writes import GLOBAL_READS, GLOBAL_WRITES

API_PREFIX = "/api/v1"
CONSOLE_ROUTES_JSON = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "globalRoutes.json"
)
REGENERATE = "Regenerate it, from backend/: .venv/bin/python -m tests.export_console_global_routes"


def console_global_routes() -> list[dict[str, str]]:
    """`GLOBAL_WRITES | GLOBAL_READS` as the console names them: the path
    relative to `/api/v1` (what `ApiClient` requests), sorted by
    (path, method)."""
    routes: list[dict[str, str]] = []
    for method, path in set(GLOBAL_WRITES) | set(GLOBAL_READS):
        if not path.startswith(API_PREFIX + "/"):
            raise ValueError(f"{method} {path} is not under {API_PREFIX}")
        routes.append({"method": method, "path": path.removeprefix(API_PREFIX)})
    return sorted(routes, key=lambda route: (route["path"], route["method"]))


def render(routes: list[dict[str, str]]) -> str:
    """The file's exact bytes: a JSON array with one route per line, so a
    route added or removed is a one-line diff."""
    return "[\n" + ",\n".join(f"  {json.dumps(route)}" for route in routes) + "\n]\n"


def main() -> None:
    routes = console_global_routes()
    CONSOLE_ROUTES_JSON.write_text(render(routes), encoding="utf-8")
    print(f"wrote {len(routes)} routes to {CONSOLE_ROUTES_JSON}")


if __name__ == "__main__":
    main()
