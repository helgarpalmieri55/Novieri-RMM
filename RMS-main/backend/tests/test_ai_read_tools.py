"""SECURITY GATE 1: the static read-tool allowlist, kept in sync with the router.

docs/modules/ai-admin/spec.md §0.2, §0.3, §0.4, §4, §7.1;
docs/modules/ai-automation.md §2.1, §19.9.

**RENAMED FROM `test_ai_admin_registry.py` BY THE ALLOWLIST'S MOVE TO
`app/core/ai/tools/` (§19.9), AND THE RENAME IS PART OF THE FIX.** The allowlist
is platform capability -- "authored by the platform", as `registry.py` has always
said -- and it is what every staff persona's tool set is built from. A file named
for one persona is a file the next reader deletes along with that persona, taking
the drift test that keeps `response_fields` honest for ALL of them. The two
assertions in here that really were about the admin PACKAGE (its `mutating=True`
scan and its approved-action independence) moved to
`tests/test_ai_admin_boundaries.py`, which is where package-scoped guards live.

THE PROPERTY THIS FILE ESTABLISHES is that **becoming an AI tool is a
deliberate, reviewed act, and the build fails if the registry drifts from the
router.** Nothing after this file may widen the allowlist without failing a test
here.

ROUTE DISCOVERY IS FROM THE FULLY BUILT APP, VIA A FLATTENER. For FastAPI
0.139.2 `app.routes` IS `app.router.routes`, and `include_router` appends a lazy
`_IncludedRouter` wrapper whose own `.path` is `None` -- so filtering either
collection by path yields an empty list forever. `_app_routes` descends through
`_IncludedRouter.original_router` and re-applies `include_context.prefix`, the
same flattener `test_email_intake_api.py` uses. THIS IS LOAD-BEARING TWICE
OVER: a flattener that silently missed `include_router`'d routes would make
"every tool resolves" vacuous, and a vacuous allowlist test is the entire
failure mode this module exists to avoid. `test_the_flattener_finds_real_routes`
pins it against an empty result.

WHY A DENYLIST AT ALL, GIVEN THE ALLOWLIST. Invariant #3's "credentials never
appear in API responses" is NOT literally true of this codebase:
`credentials.py::checkout_credential` returns a plaintext secret and
`GET /licenses/{id}/key` returns a decrypted key. Both are correct point-of-use
behaviour, which is exactly why the ALLOWLIST -- not the API's own reticence --
is what keeps vault material away from the agent. The denylist is the tripwire
for the day someone adds one of those routes back, and
`test_denylist_entries_resolve_to_real_routes` is what stops the tripwire from
quietly pointing at a path that no longer exists.
"""

from typing import Any, get_origin
from uuid import UUID

import pytest
from fastapi.routing import APIRoute

from app.core.ai.scrub import looks_like_secret_key
from app.core.ai.tools import REGISTRY, ReadTool
from app.core.ai.tools.registry import _FORBIDDEN_GATES, _NEVER_A_TOOL
from app.core.rbac import PERMISSIONS, route_permission
from tests.tool_surface import every_field_name, flatten, inner_model, route_property_failures

# --------------------------------------------------------------------------- #
# Router flattening (see the module docstring).
# --------------------------------------------------------------------------- #

# MOVED TO `tests/tool_surface.py`, ALIASED HERE rather than duplicated. The
# whole-router sweep needs the same flattener, the same `list[Model]` unwrapping
# and the same depth-flat field walk that this file's gate needs, and a property
# implemented twice is a property that will be implemented differently -- which
# is how the gate and the published measurement came to disagree in the first
# place. The private names are kept so the tests below read unchanged.
_flatten = flatten


def _matches(app: Any, method: str, path: str) -> list[APIRoute]:
    return [
        route
        for full_path, route in _flatten(app.router)
        if full_path == path
        and isinstance(route, APIRoute)
        and method in (route.methods or set())
    ]


@pytest.fixture()
def app(api_env: Any) -> Any:
    return api_env.app


async def test_the_flattener_finds_real_routes(app: Any) -> None:
    """The flattener itself, pinned against an empty result -- without this
    every assertion below passes over nothing."""
    routes = _flatten(app.router)
    api_paths = {p for p, _ in routes if p.startswith("/api/v1/")}
    assert len(api_paths) > 100, f"flattener found only {len(api_paths)} /api/v1 paths"


async def test_the_registry_is_not_empty(app: Any) -> None:
    """Every parametrized test in this module and in the executor's authz suite
    iterates `REGISTRY`. An empty registry makes all of them vacuous -- zero
    cases collected, green suite, no allowlist under test at all. This is the
    guard against that."""
    assert len(REGISTRY) >= 2


# --------------------------------------------------------------------------- #
# Spec §4.3's four sync tests.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_every_tool_resolves_to_exactly_one_route(app: Any, name: str) -> None:
    """§4.3 test 1. EXACTLY one, not at least one: an ambiguous match is a tool
    whose gate you cannot name."""
    tool = REGISTRY[name]
    found = _matches(app, tool.method, tool.path)
    assert len(found) == 1, f"{name}: {len(found)} routes match {tool.method} {tool.path}"


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_every_recorded_permission_matches_the_router(app: Any, name: str) -> None:
    """§4.3 test 2. The registry records the gate; the router owns it. A module
    that tightens OR loosens a route's gate fails here, so the registry can
    never describe a tool as needing a permission it no longer needs."""
    tool = REGISTRY[name]
    (route,) = _matches(app, tool.method, tool.path)
    assert route_permission(route) == tool.permission


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_nothing_forbidden_is_registered(name: str) -> None:
    """§4.3 test 3, all four clauses. The GET-only clause is the one that makes
    the read/act boundary something a BUILD can fail on: `ReadTool.method` is
    `Literal["GET"]`, so a mutating entry cannot even be constructed."""
    tool = REGISTRY[name]
    assert isinstance(tool, ReadTool)
    assert tool.method == "GET"
    assert (tool.method, tool.path) not in _NEVER_A_TOOL
    assert tool.permission not in _FORBIDDEN_GATES
    # A tool that calls the chat API is a recursion primitive.
    assert not tool.path.startswith("/api/v1/ai")


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_no_tool_takes_an_org(name: str) -> None:
    """§4.3 test 4. Tenancy comes from the token, never from the model (§0.9)."""
    tool = REGISTRY[name]
    banned = {"org_id", "org", "organization_id"}
    assert not (set(tool.args.model_fields) & banned)
    assert not (banned & {seg.strip("{}") for seg in tool.path.split("/")})


# --------------------------------------------------------------------------- #
# This plan's two additions: denylist liveness.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("method", "path"), sorted(_NEVER_A_TOOL))
async def test_denylist_entries_resolve_to_real_routes(
    app: Any, method: str, path: str
) -> None:
    """THE HIGHEST-VALUE TEST IN THIS FILE, and not one the spec lists.

    A denylist entry whose path has drifted denies NOTHING, and
    `test_nothing_forbidden_is_registered` then passes happily against a
    registry that has since acquired the real credential route under its new
    path. If this goes red, the denylist stopped being a tripwire -- fix the
    pair, do not delete it."""
    found = _matches(app, method, path)
    assert len(found) == 1, f"denylist entry {method} {path} matches {len(found)} routes"


@pytest.mark.parametrize("code", sorted(_FORBIDDEN_GATES))
async def test_forbidden_gates_are_real_permission_codes(code: str) -> None:
    """Same reasoning: a renamed permission turns the gate denylist into an
    empty set that forbids nothing."""
    assert code in PERMISSIONS


async def test_the_denylist_covers_the_whole_credential_and_key_surface(app: Any) -> None:
    """§0.5: the ENTIRE credential surface is excluded, including harmless
    metadata, so a successful injection cannot even enumerate credential names
    as reconnaissance. Derived from the live router rather than hand-listed, so
    a credential route added tomorrow shows up here."""
    credentialish = {
        (method, path)
        for path, route in _flatten(app.router)
        if isinstance(route, APIRoute)
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}
        if "/credentials" in path or path.endswith("/key")
    }
    registered = {(t.method, t.path) for t in REGISTRY.values()}
    assert not (credentialish & registered)
    assert credentialish <= _NEVER_A_TOOL, f"not on the denylist: {credentialish - _NEVER_A_TOOL}"


# --------------------------------------------------------------------------- #
# Spec §0.3's SIX properties, made mechanical.
# --------------------------------------------------------------------------- #

_inner_model = inner_model


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_every_registered_tool_passes_the_six_properties(app: Any, name: str) -> None:
    """§0.3, executable rather than a PR checklist item.

    §4.3 makes this a REVIEW check. It is cheap enough to run, and it is the
    only thing that turns red when someone adds an undescribed, unbounded or
    untyped route to `REGISTRY` -- which is the concrete shape of "the
    allowlist was widened". Read the failure message: it names the property.

    SIX PROPERTIES, NOT FIVE (added 2026-08-14). The sixth -- CLOSED -- is the
    one `scrub` has enforced since it shipped and no property stated: a response
    model whose payload can carry a key that is not a model field name has no
    legal `response_fields` declaration, because
    `test_declared_response_fields_match_the_live_model` pins that declaration
    equal to the model's own field names. Declaring the data keys turns that
    test red as `stale`; omitting them blocks every result at runtime, silently.
    Five reporting routes shipped passing properties 1-5 and unable to be
    registered, and nothing noticed for a phase.

    Properties 2-6 live in `tests/tool_surface.py`, shared with the whole-router
    sweep in `test_ai_tool_surface.py`, because a property implemented twice
    will be implemented differently.
    """
    tool = REGISTRY[name]
    (route,) = _matches(app, tool.method, tool.path)
    failures: list[str] = []

    # P1 method-honest and P4's `max_limit` half are REGISTRY properties -- they
    # compare the entry against the route -- so they are checked here rather
    # than in the shared predicate, which judges routes that are not tools.
    if tool.method != "GET" or "GET" not in (route.methods or set()):
        failures.append("P1 method-honest")
    if get_origin(route.response_model) is list and tool.max_limit is None:
        failures.append("P4 bounded (no max_limit recorded)")

    failures.extend(route_property_failures(route))
    assert not failures, f"{name} ({tool.method} {tool.path}) fails: {failures}"


# --------------------------------------------------------------------------- #
# The act-mode seam (§12.1, §12.2) -- reserved, and nothing more.
# --------------------------------------------------------------------------- #


async def test_tool_result_is_never_mutating_in_read_mode() -> None:
    """§12.2: `ToolResult.mutating` is reserved so act mode's audit writer and
    UI branch on a field that already exists. In read mode it is ALWAYS False,
    and the default is False so a caller that forgets it cannot claim
    otherwise."""
    from app.core.ai.types import ToolResult

    assert ToolResult(payload={}, status=200, result_count=0, truncated=False).mutating is False


# **TWO TESTS MOVED OUT WITH THE RENAME** (§19.9). They scanned
# `app/modules/ai_admin/` -- for a `mutating=True` write and for any dependency
# on the approved-action catalogue -- and both are properties of that PACKAGE,
# not of this allowlist. They are now
# `test_ai_admin_boundaries.py::test_a_tool_result_is_never_mutating_on_any_path_
# the_executor_produces` (which absorbed this file's whitespace-normalized form,
# the stronger of the two scans) and
# `test_ai_admin_boundaries.py::test_this_persona_package_never_touches_the_
# approved_action_registry`, verbatim. Neither was dropped; a file scanning a
# persona package from a kernel suite is how a scan ends up aimed at a directory
# nobody moved it with.


async def test_turn_context_never_prints_the_bearer_token() -> None:
    """§0.1: the token is never stored on a dataclass every module receives,
    never logged. `TurnContext` is per-turn and module-private, but a dataclass
    is one careless `logger.debug(ctx)` from putting a live bearer token in a
    log file forever -- so its repr redacts."""
    from app.core.ai.types import TurnContext

    ctx = TurnContext(
        app=None,
        bearer="Bearer super-secret-token-value",
        deadline=0.0,
        org_id=UUID(int=1),
        user_id=UUID(int=2),
    )
    assert "super-secret-token-value" not in repr(ctx)
    assert "super-secret-token-value" not in str(ctx)
    assert ctx.bearer == "Bearer super-secret-token-value"


# --------------------------------------------------------------------------- #
# The declared response contract (§7.3) -- SECURITY GATE 3's control.
# --------------------------------------------------------------------------- #


# Every key a serialized model can contain, AT ANY DEPTH. `scrub` walks the
# whole payload, so the contract is depth-flat: a field of a nested model is a
# key that appears in the response, and the declaration has to name it.
#
# MOVED TO `tests/tool_surface.py` and aliased here: property 6 is defined as
# "every key the response can carry is derivable from the model", so the sweep
# needs this exact walk. Two copies would let the gate and the measurement drift
# apart on what "derivable" means.
_every_field_name = every_field_name


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_declared_response_fields_match_the_live_model(app: Any, name: str) -> None:
    """THE CHECK THAT DID NOT EXIST, and whose absence is why `scrub.py` was
    reduced to guessing at key names: *the registry test checks that the ROUTE is
    allowed; nothing checks what the route newly RETURNS.*

    Now something does. The declaration in `registry.py` is a frozen constant --
    it CANNOT be computed from the model, or a field added to `AlertOut` would
    allowlist itself and walk straight into model context -- so this test is what
    keeps the constant equal to reality.

    BOTH DIRECTIONS ARE ASSERTED, and both failures are real:
      * a field in the model and not in the declaration is a route that has
        grown something nobody reviewed; the tool is blocked at runtime until
        somebody looks at it, and this names it;
      * a field in the declaration and not in the model is a stale entry, which
        is how an allowlist rots into permission for a key nothing returns.
    """
    tool = REGISTRY[name]
    (route,) = _matches(app, tool.method, tool.path)
    _, inner = _inner_model(route.response_model)
    assert inner is not None, "P3 typed is what makes this check possible"

    live = _every_field_name(inner)
    assert tool.response_fields == live, (
        f"{name}: undeclared {sorted(live - tool.response_fields)}, "
        f"stale {sorted(tool.response_fields - live)}"
    )


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_no_declared_response_field_is_secret_shaped(name: str) -> None:
    """THE OVER-BLOCK, MADE A RED BUILD RATHER THAN A SILENT DEGRADATION.

    `scrub`'s key-name pattern runs over DECLARED keys too, on purpose: a
    declaration must never be able to unblock a secret. The other side of that
    is that a legitimate field named `sort_key` or `token_expiry` would block its
    tool result in production forever, and the only symptom would be an answer
    that quietly said the data was unavailable. This turns that into a failure at
    the moment the field is declared, naming it.
    """
    tool = REGISTRY[name]
    offenders = sorted(f for f in tool.response_fields if looks_like_secret_key(f))
    assert offenders == [], (
        f"{name} declares secret-shaped field names, which `scrub` blocks at "
        f"runtime: {offenders}. Rename them in the module that owns the model, "
        f"or drop the route from the registry."
    )


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_every_tool_declares_a_response_contract(name: str) -> None:
    """An empty declaration is FAIL-CLOSED (the tool returns nothing), so this is
    not a security check -- it is a "you shipped a dead tool" check, and the
    failure mode without it is an agent that silently cannot answer."""
    assert REGISTRY[name].response_fields, f"{name} declares no response_fields"


# --------------------------------------------------------------------------- #
# Argument models (§4.4).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_args_models_are_closed(name: str) -> None:
    """§4.4: the model produces JSON that must validate against a CLOSED field
    set. Anything else is a tool error returned to the model, never an HTTP
    request."""
    tool = REGISTRY[name]
    assert tool.args.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_path_placeholders_are_typed_args(name: str) -> None:
    """A crafted path segment must fail VALIDATION before any request is built,
    which only happens if the placeholder is a typed field (`UUID`, not `str`)."""
    tool = REGISTRY[name]
    placeholders = [
        seg.strip("{}") for seg in tool.path.split("/") if seg.startswith("{")
    ]
    for placeholder in placeholders:
        field = tool.args.model_fields.get(placeholder)
        assert field is not None, f"{name}: path param {placeholder} is not an args field"
        assert field.annotation is UUID, f"{name}: {placeholder} is {field.annotation}, not UUID"
