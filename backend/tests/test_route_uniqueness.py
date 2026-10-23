"""No two routes may claim the same method and path.

THE FAILURE THIS EXISTS FOR IS INVISIBLE EVERYWHERE ELSE. FastAPI resolves
duplicate paths by FIRST REGISTERED WINS and raises nothing -- not at import,
not at startup, not on the request. Two routers reaching into one namespace
from different files therefore produce a fully green tree with a shadowed
endpoint behind it, and every other guard in this suite actively conceals it:

* `test_route_gating` walks `app.routes` and finds BOTH routes carrying a
  permission code, so it passes;
* `test_ai_tool_surface` counts GET routes off the same flattened list, so its
  pinned total absorbs a route no request will ever reach -- and whoever
  re-measures transcribes that number as the new truth, making the one file
  whose purpose is keeping that measurement honest quietly false.

It surfaces in production as a button that 404s or silently writes nothing.

This cycle came within one design decision of shipping exactly that: two
workstreams independently planned `/api/v1/users`, from different files, and
nothing in the repository would have said so. The namespace was assigned to one
owner before the code was written; this test is what keeps that decision from
having to be remembered.
"""

from collections import Counter

from fastapi.routing import APIRoute

from tests.tool_surface import build_router_only_app, flatten


def test_no_method_and_path_is_claimed_twice() -> None:
    claims: Counter[tuple[str, str]] = Counter()
    for _path, route in flatten(build_router_only_app().router):
        if not isinstance(route, APIRoute):
            continue
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}:
            claims[(method, route.path)] += 1

    assert claims, "no routes found at all -- this test is measuring nothing"
    shadowed = {k: v for k, v in claims.items() if v > 1}
    assert shadowed == {}, (
        "these method+path pairs are registered more than once; FastAPI serves "
        f"the first and silently ignores the rest: {sorted(shadowed)}"
    )
