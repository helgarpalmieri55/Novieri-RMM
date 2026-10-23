"""EVERY ROUTE UNDER /api/v1 IS GATED, AND THE EXCEPTIONS ARE NAMED.

Invariant #6: "RBAC is checked on every privileged endpoint... Authorization is
server-side; the frontend never gates security."

Five modules each carried a PRIVATE copy of this assertion -- `ai_admin`,
`email_intake`, `notification`, `portal`, `sla` -- each scoped to its own
routes. Twenty other modules had none. And `remote`, the one module whose
routes do NOT report a permission through `route_permission`, was among the
twenty: it has twelve test files including `test_remote_rbac.py`, and no such
check. So the guarantee was asserted five times by modules that would pass
anyway, and zero times where it was interesting.

This is that assertion, once, over the whole router.

THE EXCEPTIONS ARE GATED -- VERIFIED, NOT ASSUMED. Before writing this
file each was read:

  * `GET /api/v1/me` -- `Depends(auth_context)`. A valid token and no
    permission code, which is correct: it returns the CALLER'S OWN identity and
    permission list. A code here would be a code every role must hold.
  * `GET /api/v1/remote/sessions` and `/remote/sessions/{session_id}` --
    `Depends(require_any_permission(*_READ_PERMS))`, a local dependency in
    `remote/api.py` that queries `has_permission` for each code and raises 403
    when none match. Genuinely enforced; `route_permission` simply cannot
    express "any of two codes", so it answers None.
  * `GET /api/v1/orgs/current` -- `Depends(bound_auth_context)`, added with the
    org-settings routes. Same shape as `GET /me` and for the same reason: it
    returns the CALLER'S OWN organization (name, slug, status, and a settings
    document that can only hold allowlisted presentation keys), so a code here
    would be a code every role must hold. The console chrome renders the org
    name on every screen, including screens an operator holding only
    `devices.read` is entitled to see. Its sibling `PATCH /api/v1/orgs/current`
    IS gated, on `orgs.manage`, and `tests/test_org_settings.py::
    test_the_two_org_routes_carry_the_intended_gates` pins that asymmetry from
    the other side -- so this entry cannot quietly grow to cover the write.

So this is a TOOLING-VISIBILITY gap, not a security hole, and the distinction
is the reason the exceptions are recorded here rather than "fixed" by widening
`route_permission` -- a kernel change that would touch every consumer,
including the AI tool surface's property 2.

WHAT WOULD MAKE THIS FILE WRONG: an entry added to the exception set without a
verified reason. The set is small and every member carries one above. A fourth
should be hard to add and easy to argue about, which is the point of naming
them here instead of tolerating a count.

NO DATABASE -- the app is built for route inspection only, as in
`tests/tool_surface.py`, so this runs anywhere.
"""

from pathlib import Path

from fastapi.routing import APIRoute

from app.core.rbac import route_permission
from tests.tool_surface import build_router_only_app, flatten

# `(method, path)` pairs that legitimately report no permission code.
UNGATED_BY_DESIGN: frozenset[tuple[str, str]] = frozenset(
    {
        # Returns the caller's own identity and permissions. Auth-gated only.
        ("GET", "/api/v1/me"),
        # The caller's OWN account profile -- display_name, phone, timezone,
        # locale, and nothing else (`ProfileUpdate` is `extra="forbid"`, and
        # `tests/test_me_profile.py` pins its field list so the write surface
        # cannot grow silently). `Depends(bound_auth_context)`. A permission
        # code here would be a code every role must hold, because it is the
        # route by which an operator sets their own timezone. Safe without one
        # for the reason `app/api/v1/me.py`'s docstring states: the target is
        # `ctx.user_id` from the validated token, never an id in the body, so
        # this writes exactly one row and it is the caller's.
        ("PATCH", "/api/v1/me"),
        # THE CALLER'S OWN DASHBOARD ARRANGEMENT -- read, write, reset.
        # `Depends(bound_auth_context)`. Same argument as the profile PATCH
        # above and stated again rather than shared with it: a code here would
        # be a code every role must hold, since this is how a person arranges
        # their own first screen. Safe without one because the target is
        # `ctx.user_id` from the validated token and there is no `user_id`
        # parameter on any of the three, so a caller reaches exactly one row
        # and it is theirs. Storing a layout grants nothing either: each widget
        # renders from a module query gated on its own, and naming one in a
        # layout does not make its data readable.
        ("GET", "/api/v1/dashboard/layout"),
        # The WRITE, and the reason is stated again rather than shared with the
        # line above: one comment covering a run of entries is a reason the
        # next person appends beneath, inheriting an explanation that was never
        # about their route. `Depends(bound_auth_context)`, target
        # `ctx.user_id`, no `user_id` parameter to tamper with, so an upsert
        # here writes exactly the caller's own row.
        ("PUT", "/api/v1/dashboard/layout"),
        # The RESET, which deletes that same one row. Same dependency, same
        # `ctx.user_id` target, idempotent on a miss -- "reset a dashboard I
        # never arranged" is a request already satisfied.
        ("DELETE", "/api/v1/dashboard/layout"),
        # Gated by `remote/api.py::require_any_permission` -- operators
        # (`remote.control`) OR auditors (`remote.recordings.read`).
        ("GET", "/api/v1/remote/sessions"),
        # Same dependency, stated again rather than shared with the line above:
        # a reason that covers a RUN of entries is a reason the next person
        # appends beneath, inheriting an explanation that was never about their
        # route. One comment per entry is the rule its guard enforces.
        ("GET", "/api/v1/remote/sessions/{session_id}"),
        # The caller's OWN organization. Auth-gated only, like `GET /me`; the
        # PATCH beside it is gated on `orgs.manage`.
        ("GET", "/api/v1/orgs/current"),
        # FIRST BOOT, and the only pair here that is ungated because no identity
        # CAN exist yet rather than because the caller's own is enough.
        # `app/api/v1/setup.py` answers "does this installation have any users?"
        # for a browser that cannot sign in until one does. One boolean, no
        # organization, no code, nothing else -- and the bit is already
        # observable, since an unclaimed platform has no sign-in that succeeds.
        ("GET", "/api/v1/setup/status"),
        # ITS SIBLING, AND THE ONLY UNGATED WRITE ON THIS SURFACE. It creates the
        # first organization and its first administrator, so requiring a
        # permission would require a user, and there is none. Two facts stand in
        # for the gate and BOTH must hold: the platform has zero `users` rows
        # (read through the BYPASSRLS admin engine -- through a tenant session
        # RLS reports every platform as empty), and the caller presents the
        # install code the installer printed, compared with
        # `hmac.compare_digest`. `tests/test_setup_api.py` drives each refusal
        # separately -- a wrong code on an EMPTY platform, and the RIGHT code on
        # a populated one -- because a check only ever exercised when the other
        # also fails is a check nobody has tested. This entry must never grow to
        # cover a route that runs after setup: the reason it is safe expires the
        # moment the platform has an owner.
        ("POST", "/api/v1/setup/claim"),
    }
)


def _api_v1_routes() -> list[tuple[str, str, APIRoute]]:
    """Every `(method, path, route)` under /api/v1, HEAD/OPTIONS excluded."""
    out: list[tuple[str, str, APIRoute]] = []
    for path, route in flatten(build_router_only_app().router):
        if not isinstance(route, APIRoute) or not path.startswith("/api/v1"):
            continue
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            out.append((method, path, route))
    return out


def test_every_api_route_is_gated_or_a_named_exception() -> None:
    ungated = {
        (method, path)
        for method, path, route in _api_v1_routes()
        if route_permission(route) is None
    }
    surprises = ungated - UNGATED_BY_DESIGN
    assert not surprises, (
        "these routes report no permission code and are not a recorded "
        "exception. Either gate them with `require_permission`, or -- if they "
        "are gated some other way -- add them to UNGATED_BY_DESIGN with the "
        f"dependency that enforces them: {sorted(surprises)}"
    )


def test_the_exception_list_has_not_rotted() -> None:
    """A denylist entry pointing at a path nothing serves denies nothing.

    The same liveness discipline `test_denylist_entries_resolve_to_real_routes`
    applies to the AI tool registry: if one of these routes is renamed or
    deleted, the exception silently becomes permission for a path that no
    longer exists, and a future route inheriting that path would be exempt
    without anybody deciding so.
    """
    live = {(method, path) for method, path, _route in _api_v1_routes()}
    stale = UNGATED_BY_DESIGN - live
    assert not stale, (
        f"recorded exceptions that no longer resolve to a route -- remove them "
        f"or fix the path: {sorted(stale)}"
    )

    # And each must still actually BE ungated: if one acquires a
    # `require_permission`, the exception is now hiding a route that no longer
    # needs hiding, and the next reader would take the list as evidence that
    # ungated routes are normal here.
    gated_now = {
        (method, path)
        for method, path, route in _api_v1_routes()
        if (method, path) in UNGATED_BY_DESIGN and route_permission(route) is not None
    }
    assert not gated_now, (
        f"these are now gated by `route_permission` and should be removed from "
        f"UNGATED_BY_DESIGN: {sorted(gated_now)}"
    )


def test_the_sweep_is_not_vacuous() -> None:
    """Without this, a broken flattener reports a perfectly gated API."""
    routes = _api_v1_routes()
    assert len(routes) > 300, f"only {len(routes)} routes found"
    gated = sum(1 for _m, _p, r in routes if route_permission(r) is not None)
    assert gated > 300, f"only {gated} routes reported a permission code"


def test_every_exception_carries_a_stated_reason() -> None:
    """An entry with no explanation is how an ungated route becomes normal.

    The two tests above keep the list HONEST -- every entry resolves to a live
    route, and none has quietly acquired a permission code. Neither asks the
    question that matters when somebody adds a line: *why is this one safe*.
    An unexplained tuple reads as precedent, and the next person appends theirs
    beside it.

    Every entry is preceded by a comment naming the dependency that protects
    it, and this makes that a rule rather than a habit. It is deliberately
    crude -- presence of a `#` line above the tuple, not a judgement about the
    prose -- because the achievable check is that somebody was made to write
    something down.

    ADDED 2026-08-19 to make a claim in `app/api/v1/me.py`'s docstring true.
    It said "its sibling test refuses any entry without a stated reason"; no
    such test existed. A comment asserting a guard that is not there is worse
    than no comment, because it is load-bearing for a reader deciding whether
    the list can be trusted.
    """
    source = Path(__file__).read_text().splitlines()
    start = next(i for i, line in enumerate(source) if "UNGATED_BY_DESIGN" in line and "=" in line)
    end = next(i for i in range(start, len(source)) if source[i].strip() == ")")

    # THE LINE IMMEDIATELY ABOVE, and no walking back. The first version of
    # this walked backwards over neighbouring entries looking for any comment,
    # which meant every entry inherited its neighbour's reason and the whole
    # test was vacuous -- stripping a comment left it green. Caught by planting
    # exactly that.
    unexplained: list[str] = []
    for i in range(start + 1, end):
        stripped = source[i].strip()
        if not stripped.startswith('("'):
            continue
        above = source[i - 1].strip()
        if not above.startswith("#"):
            unexplained.append(stripped)

    assert not unexplained, (
        "every UNGATED_BY_DESIGN entry needs a comment above it naming what "
        f"protects it; these have none: {unexplained}"
    )
