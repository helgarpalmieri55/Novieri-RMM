# AI Tool Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the sixth tool property (*closed*), make the whole five/six-property measurement executable against the live router and pinned in CI, and correct three stale published figures.

**Architecture:** One shared predicate module under `backend/tests/` implements the properties once. Two consumers use it: the existing registry-scoped **gate** (`test_ai_read_tools.py`) and a new whole-router **sweep** (`test_ai_tool_surface.py`). The sweep builds its own app via `create_app` with throwaway settings, because the properties are static analysis of the router and must not need a database. No new tool is registered; two denylist entries are added.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, pytest, SQLAlchemy 2.x (not exercised here).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-14-ai-tool-surface-design.md` (commit `707d2f1`). It is authoritative; where this plan and the spec disagree, raise it rather than choosing.
- **Measured 2026-08-14 against the live router — these exact numbers are pinned:** `188` `GET` routes under `/api/v1`; `10` pass the five properties; `3` also pass the sixth; `3` are ungated per `route_permission`.
- The three passing all six: `/api/v1/health/alerts`, `/api/v1/devices/{device_id}/health`, `/api/v1/ai/status`.
- **Property 6 has no carve-out for `dict[str, SomeModel]`.** Any `dict` at any depth is open. Verified: the counts are identical with and without a carve-out, so the stricter rule costs nothing.
- **Register no new tool.** `REGISTRY` stays at exactly two entries. `/api/v1/ai/status` passes all six and is deliberately not registered (spec §3).
- **Do not modify `app/modules/reporting/`** in any way. Its routes' unregisterability is the finding, not a bug to fix here (spec §3).
- **Do not weaken `app/core/ai/scrub.py`.** No new parameter, no exemption, no change to `_SECRET_WORDS`. The properties move; the gate does not (spec §2).
- Type hints mandatory. Run `.venv/bin/python -m mypy app tests` and `.venv/bin/python -m ruff check` before each commit.
- Backend commands run from `/home/user/Toeshee-RMS/backend` using `.venv/bin/python`.
- Never `git add -A`, `git add .`, `git commit -a`, `git checkout .`, or `git stash`. Stage named paths only.
- No model identifier and no `Co-Authored-By` trailer in commit messages.
- **Postgres is unavailable in this container.** Tests requiring the `api_env`/`app` fixture cannot run here; every test this plan adds is designed to run without it. Report honestly which tests you ran and which you could not.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/tests/tool_surface.py` | **New.** The shared predicate: router flattening, `inner_model`, `every_field_name`, `model_openness`, `route_property_failures`, `build_router_only_app`. No assertions — a library. |
| `backend/tests/test_tool_surface_predicate.py` | **New.** Unit tests for `model_openness` on constructed models (spec §6.3). |
| `backend/tests/test_ai_tool_surface.py` | **New.** The sweep: pins the four counts (§6.1) and the predicate/`scrub` agreement test (§6.4). |
| `backend/tests/test_ai_read_tools.py` | **Modify.** The gate moves onto the shared predicate and runs six properties. |
| `backend/app/core/ai/tools/registry.py` | **Modify.** Two `_NEVER_A_TOOL` entries for the act routes. |
| `docs/modules/ai-admin/spec.md` | **Modify.** Property 6 in §0.3; §17.2 re-measured. |
| `docs/modules/reporting/spec.md` | **Modify.** §0.10, §10.1, §16.2 re-measured and amended. |
| `docs/06-roadmap.md` | **Modify.** Phase 17's measurement paragraph. |

---

### Task 1: The shared predicate

**Files:**
- Create: `backend/tests/tool_surface.py`
- Test: `backend/tests/test_tool_surface_predicate.py`

**Interfaces:**
- Consumes: `app.core.ai.scrub.looks_like_secret_key`, `app.core.rbac.route_permission`, `app.core.config.Settings`, `app.main.create_app`.
- Produces, relied on by Tasks 2–4:
  - `flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]`
  - `inner_model(response_model: Any) -> tuple[bool, type[BaseModel] | None]`
  - `every_field_name(model: type[BaseModel], seen: set[Any] | None = None) -> set[str]`
  - `model_openness(model: type[BaseModel], seen: set[Any] | None = None, path: str = "") -> list[str]`
  - `route_property_failures(route: APIRoute) -> list[str]` — labels `"P2 gated"`, `"P3 typed"`, `"P4 bounded"`, `"P5 self-describing"`, `"P6 closed"`.
  - `build_router_only_app() -> FastAPI`
  - `api_v1_get_routes(app: Any) -> list[tuple[str, APIRoute]]`

- [ ] **Step 1: Write the failing unit tests**

Create `backend/tests/test_tool_surface_predicate.py`:

```python
"""Unit tests for the property predicate (spec §6.3).

The CLOSED case is the one that matters: a predicate that flags everything
would make the sweep's numbers meaningless while looking like it worked.
"""

from typing import Any

from pydantic import BaseModel, Field

from tests.tool_surface import model_openness


class Closed(BaseModel):
    """A model whose key space is finite and derivable."""

    device_id: str = Field(description="d")
    hostname: str = Field(description="h")


class Nested(BaseModel):
    """Holds a closed model."""

    inner: Closed = Field(description="i")


class OpenInt(BaseModel):
    """`dict[str, int]` -- keys are data."""

    totals: dict[str, int] = Field(description="t")


class OpenAny(BaseModel):
    """`dict[str, Any]` -- a JSON blob."""

    params_schema: dict[str, Any] = Field(description="p")


class OpenModelValued(BaseModel):
    """`dict[str, Closed]` -- typed VALUES, data KEYS. Still open."""

    by_host: dict[str, Closed] = Field(description="b")


class DeeplyOpen(BaseModel):
    """The open dict is two models down, where a shallow walk would miss it."""

    nested: Nested = Field(description="n")
    also: OpenInt = Field(description="a")


class SecretNamed(BaseModel):
    """`report_key` splits to `report` + `key`; `key` is a secret word."""

    report_key: str = Field(description="r")


class ListOfOpen(BaseModel):
    """The open dict is inside a list element."""

    rows: list[OpenInt] = Field(description="r")


def test_a_closed_model_reports_nothing() -> None:
    assert model_openness(Closed) == []
    assert model_openness(Nested) == []


def test_an_open_dict_is_reported() -> None:
    assert any("open dict" in r for r in model_openness(OpenInt))
    assert any("open dict" in r for r in model_openness(OpenAny))


def test_a_model_valued_dict_is_still_open() -> None:
    """No carve-out: `{"<hostname>": {...}}` is as unregisterable as
    `{"<hostname>": 3}`, because `scrub` walks KEYS."""
    assert any("open dict" in r for r in model_openness(OpenModelValued))


def test_openness_is_found_at_depth() -> None:
    reasons = model_openness(DeeplyOpen)
    assert any("open dict" in r for r in reasons), reasons


def test_openness_is_found_inside_a_list() -> None:
    reasons = model_openness(ListOfOpen)
    assert any("open dict" in r for r in reasons), reasons


def test_a_secret_shaped_field_name_is_reported() -> None:
    reasons = model_openness(SecretNamed)
    assert any("secret-shaped" in r and "report_key" in r for r in reasons), reasons


def test_reasons_name_the_path_so_a_failure_is_actionable() -> None:
    (reason,) = model_openness(OpenInt)
    assert "totals" in reason
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tool_surface_predicate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.tool_surface'`

- [ ] **Step 3: Write the predicate**

Create `backend/tests/tool_surface.py`:

```python
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
from app.main import create_app


def flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Verbatim from `test_ai_read_tools.py::_flatten`.

    This codebase's `register()` wraps included routers, so `app.routes` alone
    finds NOTHING under `/api/v1` -- a walker that misses every route reports a
    clean sweep over an empty set.
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
    if inner is None:
        failures.append("P6 closed")
    elif model_openness(inner):
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tool_surface_predicate.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Verify the closed case can actually fail**

Temporarily change `Closed.hostname` to `host_key: str = Field(description="h")` and re-run. Expected: `test_a_closed_model_reports_nothing` FAILS. **Restore by editing the field name back — never `git checkout`.** Re-run and confirm green.

- [ ] **Step 6: Type-check, lint, commit**

```bash
.venv/bin/python -m ruff check tests/tool_surface.py tests/test_tool_surface_predicate.py
.venv/bin/python -m mypy tests/tool_surface.py tests/test_tool_surface_predicate.py
git add tests/tool_surface.py tests/test_tool_surface_predicate.py
git commit -m "test(ai): the six tool properties, implemented once

Properties 1-5 measure documentation. scrub.py demands a closed key
space and no property said so, which is how five routes shipped passing
every stated property and unable to become tools."
```

---

### Task 2: The sweep — pin the measurement

**Files:**
- Create: `backend/tests/test_ai_tool_surface.py`

**Interfaces:**
- Consumes: everything Task 1 produces.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_ai_tool_surface.py`:

```python
"""THE MEASUREMENT, PINNED (spec §4.2).

Three documents carried three different wrong numbers -- 2 of 147, 3 of 173,
2 of 147 again -- and the drift was found only because somebody re-measured by
hand. This test is what makes the next drift a red build.

IT IS DELIBERATELY BRITTLE. Any route added anywhere turns it red, and that is
the deliverable, not a defect: the numbers below are quoted in specs and in the
roadmap as a statement about this codebase, so a number nobody is forced to
re-measure is a number that rots. When it fails, re-run it, read which property
the new route failed, and update the record here in the same commit.

NO DATABASE. See `tool_surface.build_router_only_app`.
"""

from tests.tool_surface import (
    api_v1_get_routes,
    build_router_only_app,
    route_property_failures,
)

# Measured 2026-08-14 against the live router.
EXPECTED_GET_ROUTES = 188
EXPECTED_PASSING_FIVE = 10
EXPECTED_UNGATED = 3

# The registrable set, pinned by PATH rather than count: this is the number the
# whole exercise is about, it is small, and "which routes" is the actionable
# half of "how many".
EXPECTED_PASSING_SIX = frozenset(
    {
        "/api/v1/health/alerts",
        "/api/v1/devices/{device_id}/health",
        "/api/v1/ai/status",
    }
)

# Genuinely gated, but invisible to `route_permission`: `remote/api.py` defines
# a local `require_any_permission`. Recorded as a finding about the remote
# module (reporting spec §16.3), pinned here so it cannot grow unnoticed.
EXPECTED_UNGATED_PATHS = frozenset(
    {
        "/api/v1/me",
        "/api/v1/remote/sessions",
        "/api/v1/remote/sessions/{session_id}",
    }
)


def test_the_tool_surface_measurement_is_unchanged() -> None:
    app = build_router_only_app()
    routes = api_v1_get_routes(app)
    failures = {path: route_property_failures(route) for path, route in routes}

    passing_five = {p for p, f in failures.items() if not [x for x in f if x != "P6 closed"]}
    passing_six = {p for p, f in failures.items() if not f}
    ungated = {p for p, f in failures.items() if "P2 gated" in f}

    problems: list[str] = []
    if len(routes) != EXPECTED_GET_ROUTES:
        problems.append(f"GET routes: {len(routes)}, pinned {EXPECTED_GET_ROUTES}")
    if len(passing_five) != EXPECTED_PASSING_FIVE:
        problems.append(
            f"passing five: {len(passing_five)}, pinned {EXPECTED_PASSING_FIVE}"
        )
    if passing_six != EXPECTED_PASSING_SIX:
        problems.append(
            f"passing six -- gained {sorted(passing_six - EXPECTED_PASSING_SIX)}, "
            f"lost {sorted(EXPECTED_PASSING_SIX - passing_six)}"
        )
    if ungated != EXPECTED_UNGATED_PATHS:
        problems.append(
            f"ungated -- gained {sorted(ungated - EXPECTED_UNGATED_PATHS)}, "
            f"lost {sorted(EXPECTED_UNGATED_PATHS - ungated)}"
        )

    assert not problems, (
        "The tool surface moved. Re-measure, read which property each new route "
        "failed, and update this record in the same commit:\n  "
        + "\n  ".join(problems)
    )


def test_the_flattener_finds_routes() -> None:
    """The failure this whole file is worthless without: `app.routes` alone
    finds NOTHING under `/api/v1` in this codebase, and a sweep over an empty
    set reports a clean bill of health."""
    assert len(api_v1_get_routes(build_router_only_app())) > 100


def test_passing_six_is_a_subset_of_passing_five() -> None:
    """Property 6 can only ever REMOVE routes. If this fails, the predicate is
    counting something the five-property reading does not."""
    app = build_router_only_app()
    failures = {path: route_property_failures(route) for path, route in api_v1_get_routes(app)}
    passing_five = {p for p, f in failures.items() if not [x for x in f if x != "P6 closed"]}
    passing_six = {p for p, f in failures.items() if not f}
    assert passing_six <= passing_five
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_ai_tool_surface.py -v`
Expected: PASS, 3 tests. If the counts differ from the pinned values, **do not adjust the numbers to match** — report it: the branch has changed the router since the measurement, and the controller must decide.

- [ ] **Step 3: Verify the pin can actually fail**

Add a throwaway route to any module's router, re-run, and confirm the failure message names the count that moved. **Remove the route by editing it out.** Re-run and confirm green. Paste both outputs into your report.

- [ ] **Step 4: Type-check, lint, commit**

```bash
.venv/bin/python -m ruff check tests/test_ai_tool_surface.py
.venv/bin/python -m mypy tests/test_ai_tool_surface.py
git add tests/test_ai_tool_surface.py
git commit -m "test(ai): pin the tool-surface measurement at 188/10/3

Three documents carried three different wrong numbers and the drift was
found only by re-measuring by hand. Brittle on purpose."
```

---

### Task 3: Prove the predicate agrees with `scrub`

**Files:**
- Modify: `backend/tests/test_ai_tool_surface.py` (append)

**Interfaces:**
- Consumes: Task 1's `every_field_name`, `model_openness`, `inner_model`, `api_v1_get_routes`, `build_router_only_app`; `app.core.ai.scrub.scrub`; `app.core.ai.types.ReadTool`.

This is the task that turns spec §1 from a claim into a property of the code. Without it, "these routes cannot be registered" is an assertion in a document — the exact species of unfalsifiable claim this project keeps finding.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_ai_tool_surface.py`:

```python
from typing import Any

import pytest

from app.core.ai.scrub import Blocked, scrub
from app.core.ai.types import ReadTool
from tests.tool_surface import every_field_name, inner_model, model_openness

# The seven routes that pass five properties and fail the sixth. Written out
# rather than derived, so that a route silently LEAVING this set (because
# somebody fixed it, or deleted it) is visible in a diff.
OPEN_BUT_OTHERWISE_PASSING = (
    "/api/v1/reports",
    "/api/v1/reports/fleet-posture",
    "/api/v1/reports/patch-compliance",
    "/api/v1/reports/backup-coverage",
    "/api/v1/reports/sla-attainment",
    "/api/v1/ai/actions",
    "/api/v1/ai/actions/{ai_action_id}",
)


def _tool_for(model: Any) -> ReadTool:
    """A `ReadTool` carrying the declaration the drift test would FORCE.

    `test_declared_response_fields_match_the_live_model` asserts
    `response_fields == every_field_name(response_model)` exactly, in both
    directions. So this is the only legal declaration for the model, and if it
    does not survive `scrub`, no legal declaration does.
    """
    return ReadTool(
        name="probe",
        method="GET",
        path="/api/v1/probe",
        permission="probe.read",
        args=None,  # type: ignore[arg-type]
        hint="probe",
        response_fields=frozenset(every_field_name(model)),
    )


@pytest.mark.parametrize("path", OPEN_BUT_OTHERWISE_PASSING)
def test_an_open_route_has_no_legal_declaration_that_survives_scrub(path: str) -> None:
    """SPEC §1, MADE FALSIFIABLE.

    For each route: build the forced declaration, then build the smallest
    payload the model permits which exercises the openness reason, and assert
    the REAL `scrub` blocks it. This is what makes "the five properties do not
    imply registrability" a fact about running code.
    """
    app = build_router_only_app()
    (route,) = [r for p, r in api_v1_get_routes(app) if p == path]
    _, model = inner_model(route.response_model)
    assert model is not None, "these routes pass P3 by definition"

    reasons = model_openness(model)
    assert reasons, f"{path} is closed -- it should not be in this list"

    tool = _tool_for(model)
    blocked_something = False
    for reason in reasons:
        field = reason.split("`")[1].split(".")[-1]
        if reason.startswith("secret-shaped"):
            payload: Any = {field: "x"}
        else:
            # An open dict carrying one data-derived key. `scrub` walks keys,
            # and this key is by construction not a model field name.
            payload = {field: {"a_key_the_model_cannot_name": 1}}
        if isinstance(scrub(tool, payload), Blocked):
            blocked_something = True
    assert blocked_something, (
        f"{path}: the forced declaration survived scrub for every openness "
        f"reason {reasons} -- property 6 is stricter than the gate, which "
        f"would mean the pinned count UNDERSTATES what is registrable"
    )


def test_the_two_registered_tools_are_closed() -> None:
    """The other direction: property 6 must not condemn what already works.

    Both registered tools serve results in production, so a predicate that
    called either one open would be wrong about the gate rather than about
    them."""
    from app.core.ai.tools import REGISTRY

    app = build_router_only_app()
    by_path = dict(api_v1_get_routes(app))
    for tool in REGISTRY.values():
        _, model = inner_model(by_path[tool.path].response_model)
        assert model is not None
        assert model_openness(model) == [], f"{tool.name} is open"
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_ai_tool_surface.py -v`
Expected: PASS, 3 + 7 + 1 = 11 tests.

If `test_an_open_route_has_no_legal_declaration_that_survives_scrub` fails for any route, **stop and report** — it means property 6 is stricter than `scrub`, which is the falsifier spec §7 names.

- [ ] **Step 3: Commit**

```bash
.venv/bin/python -m ruff check tests/test_ai_tool_surface.py
.venv/bin/python -m mypy tests/test_ai_tool_surface.py
git add tests/test_ai_tool_surface.py
git commit -m "test(ai): prove the seven open routes have no legal declaration

Builds the declaration the drift test forces and runs the real scrub
against it. Turns 'these cannot be registered' from a claim in a spec
into a property of the code."
```

---

### Task 4: The gate runs six properties; the act routes join the denylist

**Files:**
- Modify: `backend/tests/test_ai_read_tools.py`
- Modify: `backend/app/core/ai/tools/registry.py:197-207`

**Interfaces:**
- Consumes: Task 1's `route_property_failures`, `inner_model`, `every_field_name`, `flatten`.

- [ ] **Step 1: Add the two denylist entries**

In `backend/app/core/ai/tools/registry.py`, extend `_NEVER_A_TOOL`:

```python
        ("GET", "/api/v1/licenses/{license_id}/key"),
        # ACT MODE'S OWN SURFACE (ai-admin spec §12.3): "Read mode does not read
        # that registry at all. It does not list approved actions, does not
        # describe them to the model, and adds nothing to them."
        #
        # Until now that was a sentence in a spec.
        # `test_denylist_entries_resolve_to_real_routes` makes it a tripwire
        # that cannot rot into pointing at a path nothing serves.
        #
        # Both routes ALSO fail property 6 today (`approved_action_key` is
        # secret-shaped; `rendered_params` is an open dict), so they are doubly
        # excluded. The denylist entry is still the right mechanism: property 6
        # is a fact about today's models that a refactor could change, and §12.3
        # is a decision meant to outlive it.
        ("GET", "/api/v1/ai/actions"),
        ("GET", "/api/v1/ai/actions/{ai_action_id}"),
```

- [ ] **Step 2: Point the gate at the shared predicate and add property 6**

In `backend/tests/test_ai_read_tools.py`, replace the body of
`test_every_registered_tool_passes_the_five_properties` and rename it. Delete the now-duplicated local `_inner_model`, `_every_field_name`, `_nested_models` and `_flatten` definitions **only if nothing else in the file uses them** — check each with a grep before removing, and import the equivalents from `tests.tool_surface`.

```python
@pytest.mark.parametrize("name", sorted(REGISTRY))
async def test_every_registered_tool_passes_the_six_properties(app: Any, name: str) -> None:
    """§0.3, executable rather than a PR checklist item.

    SIX properties, not five. The sixth -- CLOSED -- is the one `scrub` has
    enforced since it shipped and no property stated: a response model whose
    payload can carry a key that is not a model field name has no legal
    `response_fields` declaration, so the route cannot become a tool however
    well documented it is. Five routes shipped passing all five and unable to
    be registered before anybody noticed.

    Implemented in `tests/tool_surface.py`, shared with the whole-router sweep,
    because a property implemented twice will be implemented differently.
    """
    tool = REGISTRY[name]
    (route,) = _matches(app, tool.method, tool.path)
    failures: list[str] = []

    # P1 method-honest is a REGISTRY property -- it compares the entry with the
    # route -- so it is checked here and not in the shared predicate.
    if tool.method != "GET" or "GET" not in (route.methods or set()):
        failures.append("P1 method-honest")
    # P4's max_limit half is likewise a registry property.
    if get_origin(route.response_model) is list and tool.max_limit is None:
        failures.append("P4 bounded (no max_limit recorded)")

    failures.extend(route_property_failures(route))
    assert not failures, f"{name} ({tool.method} {tool.path}) fails: {failures}"
```

Add the import: `from tests.tool_surface import route_property_failures`.

- [ ] **Step 3: Run the affected tests**

Postgres is unavailable here, so `test_ai_read_tools.py` cannot run in this container. Run what you can and say so plainly:

```bash
.venv/bin/python -m pytest tests/test_ai_tool_surface.py tests/test_tool_surface_predicate.py -v
.venv/bin/python -m pytest tests/test_ai_read_tools.py --collect-only -q
```

Expected: the first two pass; the third collects without import errors (this is what catches a broken import or a removed helper that was still referenced). Report exactly this, and do not claim `test_ai_read_tools.py` passes.

- [ ] **Step 4: Type-check, lint, commit**

```bash
.venv/bin/python -m ruff check tests/test_ai_read_tools.py app/core/ai/tools/registry.py
.venv/bin/python -m mypy tests/test_ai_read_tools.py app/core/ai/tools/registry.py
git add tests/test_ai_read_tools.py app/core/ai/tools/registry.py
git commit -m "test(ai): the gate runs six properties, and act routes join the denylist

The gate and the sweep now share one predicate. §12.3's 'read mode does
not read that registry at all' becomes a tested tripwire rather than a
sentence."
```

---

### Task 5: Correct the three stale measurements

**Files:**
- Modify: `docs/modules/ai-admin/spec.md` (§0.3, §17.2)
- Modify: `docs/modules/reporting/spec.md` (§0.10, §10.1, §16.2)
- Modify: `docs/06-roadmap.md` (Phase 17 measurement paragraph)

House style, followed everywhere in this repo: **quote the superseded figure and date the correction** rather than deleting it, so a reader who remembers the old number can see what happened to it.

- [ ] **Step 1: Add property 6 to `docs/modules/ai-admin/spec.md` §0.3**

Add after property 5, using the spec's own wording:

> **6. Closed.** Every key the response can carry is derivable from the response
> model, and none of them is secret-shaped. Concretely: no `dict` at any depth
> of the model, and no field name at any depth for which `looks_like_secret_key`
> is true.
>
> (Added 2026-08-14. This property was enforced by `scrub.py` from the day it
> shipped and stated nowhere. `ReadTool.response_fields` is pinned equal to
> `every_field_name(response_model)`, so a model carrying an open `dict` has no
> legal declaration: declaring the data keys turns the drift test red as stale,
> omitting them blocks every result at runtime. Five reporting routes shipped
> passing properties 1–5 and unable to be registered. There is no carve-out for
> `dict[str, SomeModel]` — `scrub` walks keys, and typed values do not make
> data-derived keys declarable.)

- [ ] **Step 2: Re-measure §17.2's table**

Replace the table with the 2026-08-14 measurement, keeping the old one quoted:

| measurement | count |
| --- | --- |
| `GET` routes under `/api/v1` | 188 |
| passing properties 1–5 | 10 |
| **also passing property 6 — i.e. registrable** | **3** |
| registered as tools | 2 |

and add the finding: the registry did not grow when reporting shipped five
qualifying routes **because none of them is registrable**, not because nobody
got round to it. State that the previously published "2 of 147" was measured
before 41 routes shipped, and that the count of *registrable* routes went from
2 to 3 in that time.

- [ ] **Step 3: Amend `docs/modules/reporting/spec.md`**

- **§0.10**: re-measure (173→188, 3→10 passing five), and add the sentence the section's argument needs to stay honest: *this module's routes pass the five properties by construction and are still not registrable* — `meta.report_key` is secret-shaped and `totals` is an open dict. §0.10's claim ("a report that reaches the router has the five properties because there is no code path by which it could not") remains true and is now visibly insufficient.
- **§10.1**: amend "each becomes a `ReadTool` entry in the ordinary, deliberate, reviewed way" — the reviewed act happened on 2026-08-14 and its answer was **no**. Record the bill for changing that answer: rename `meta.report_key` and `CatalogueEntry.key`, and replace `totals: dict[str, int]` and `params_schema: dict[str, Any]` with typed models. Note that the first is a breaking wire change and the second touches the generic envelope §0.10 rests on, so it is that module's cycle.
- **§16.2**: re-measure, and note that the failure mode it did not mention is the one that mattered.

- [ ] **Step 4: Amend `docs/06-roadmap.md` Phase 17**

Re-measure the "2 of 147" paragraph, and extend its "specific, checkable bill of work" sentence with the sixth item — a module that pays the stated bill in full today still cannot be registered if its response model carries an open `dict` or a field named `*_key`. Point at `tests/test_ai_tool_surface.py` as the thing that now keeps the number honest, replacing the sentence that points only at the registry-scoped test.

- [ ] **Step 5: Verify every number you wrote**

Do not copy the numbers from this plan. Re-run the measurement and read them off it:

```bash
.venv/bin/python -m pytest tests/test_ai_tool_surface.py -v
```

A green run means the pinned constants match the router; those constants are the numbers to quote. If you wrote a number that appears nowhere in the test, it is unverified — remove it or measure it.

- [ ] **Step 6: Commit**

```bash
git add docs/modules/ai-admin/spec.md docs/modules/reporting/spec.md docs/06-roadmap.md
git commit -m "docs: re-measure the tool surface and record the sixth property

147/2 and 173/3 were both measured before 41 routes shipped. Measured
2026-08-14: 188 GET routes, 10 pass properties 1-5, 3 also pass the
sixth. Reporting's five qualifying routes are not registrable, and that
is why the registry did not grow when they shipped."
```

---

## Self-Review

**Spec coverage.** §0.1 numbers → T2's pinned constants. §1 the finding → T3 (made falsifiable). §1.3 `key` is a secret word → T1's `SecretNamed` case, T3's secret-shaped branch. §2 property 6 → T1 (`model_openness`), T5 step 1 (the spec text). §2's no-carve-out → T1's `test_a_model_valued_dict_is_still_open`. §3 what this cycle does not do → enforced by Global Constraints (no reporting changes, no new registration). §4.1 one predicate two consumers → T1 + T4 step 2. §4.2 the sweep → T2. §4.3 no database → T1's `build_router_only_app`, verified by T2 running here. §4.4 denylist → T4 step 1. §4.5 the un-federated denylists → recorded in the spec, no code (correct: the spec says federating is not in this cycle). §5 doc corrections → T5. §6.1–6.6 testing requirements → T2, T4, T1, T3, T4 step 1, and the final full-suite run below. §7 falsifiers → T1 step 5, T2 step 3, T3's stop-and-report instruction.

**Gap found and closed during review:** §6.6 requires the full backend suite to be green with the new tests present, to check §4.3's premise that `create_app` in a test module does not pollute the process-wide audit HMAC key. No task owned it, because Postgres is unavailable in this container and it cannot be run here. It is therefore **not** a task — it is a branch-level verification that must run in CI before merge, and the controller must report it as unverified locally rather than assume it. Added to the completion criteria below.

**Placeholder scan:** none. Every step has its code or its exact command.

**Type consistency:** `route_property_failures` returns `list[str]` and is extended into a `list[str]` in T4. `model_openness` returns `list[str]` and is truth-tested in `route_property_failures`. `every_field_name` returns `set[str]`, wrapped in `frozenset` for `ReadTool.response_fields` in T3. `api_v1_get_routes` returns `list[tuple[str, APIRoute]]` and is used as such in T2, T3 and via `dict(...)` in T3's last test.

---

## Completion criteria

1. `tests/test_tool_surface_predicate.py`, `tests/test_ai_tool_surface.py` green **and run locally** — no database needed.
2. `tests/test_ai_read_tools.py` collects cleanly locally; its pass/fail is **unverified in this container** and must be confirmed in CI.
3. The full backend suite green in CI, specifically confirming §4.3's premise about `create_app`'s process-wide side effect.
4. Three documents re-measured, every quoted number traceable to a pinned constant in `test_ai_tool_surface.py`.
5. `REGISTRY` still has exactly two entries; `app/modules/reporting/` untouched; `app/core/ai/scrub.py` untouched.
