"""The runner: the generated routes, the conjunction, and bounded execution
(spec §0.4, §0.5, §0.6, §4.1, §6.1, §6.4, §12.1-§12.5, §12.7, §12.12).

THIS FILE ALSO CARRIES §12.1/§12.2/§12.3'S SWEEP OVER `REGISTRY`, which
`tests/test_reporting_sources.py` deliberately does not: the verifier ships
BEFORE the first report (that ordering is a security property -- sequencing it
afterwards means shipping one report on an unverified declaration), so at that
commit the registry is empty and every parametrized case over it would collect
ZERO cases and report success. The sweep lives where its first non-vacuous case
does, and `test_the_registry_is_not_empty` runs first.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.client_scope import ClientScope
from app.core.rbac import _walk_dependant, route_permission
from app.core.tenancy import org_session

# IMPORTED FOR ITS SIDE EFFECT, AND IT IS LOAD-BEARING FOR EVERY PARAMETRIZED
# TEST IN THIS FILE. `REGISTRY` is populated when the DEFINITIONS package is
# imported, and `@pytest.mark.parametrize("key", sorted(REGISTRY))` is evaluated
# at COLLECTION time -- before any fixture runs, so before `api_env` builds the
# app that would have imported it. Without this line, collecting this module on
# its own gives every sweep an empty parameter set and pytest reports thirteen
# tests as SKIPPED ("got empty parameter set"), which is a green run asserting
# nothing. MEASURED, not hypothetical: that is exactly what happened the first
# time this file was run alone, and it passed as part of a larger run only
# because `test_reporting_fleet_posture.py` collects alphabetically earlier and
# imported the definitions as a side effect of its own imports. A test file
# whose non-vacuity depends on another file's collection order is not a test.
from app.modules.reporting import definitions as _definitions  # noqa: F401
from app.modules.reporting.errors import ReportTimeout
from app.modules.reporting.registry import (
    _NEVER_A_COLUMN,
    REGISTRY,
    TABLE_MODULE,
    TABLE_PERMISSIONS,
    export_path,
)
from app.modules.reporting.service import (
    REPORT_STATEMENT_TIMEOUT_MS,
    report_sources,
    run_bounded,
)
from app.modules.reporting.sources import verify_registry
from tests.reporting_fixtures import (
    report_query,
    reporting_user,
    seed_device,
    seed_reportable_org,
    uncovered_required_params,
)

# --------------------------------------------------------------------------- #
# Router flattening. COPIED FROM `tests/test_ai_admin_registry.py::_flatten`,
# and it is load-bearing: for FastAPI 0.139.2 `app.routes` IS
# `app.router.routes`, and `include_router` appends a lazy `_IncludedRouter`
# whose own `.path` is `None`. A naive `app.routes` filter finds NOTHING, so
# every assertion over "reporting's routes" would pass over an empty list.
# `test_the_flattener_finds_the_report_routes` pins it against that.
# --------------------------------------------------------------------------- #


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


def _report_routes(app: Any) -> dict[str, APIRoute]:
    """Routes THIS MODULE generated.

    Discriminated by the endpoint's defining module, NOT by a
    `path.startswith("/api/v1/reports/")` filter -- because that prefix is
    already shared. `app/modules/security_logs/api.py:127` mounts
    `GET /api/v1/reports/access.csv`, which predates this module and is not one
    of ours. Filtering by path would have folded a foreign route into every
    assertion below about "reporting's routes".
    """
    return {
        path: route
        for path, route in _flatten(app.router)
        if isinstance(route, APIRoute)
        and getattr(route.endpoint, "__module__", "").startswith("app.modules.reporting")
    }


def _matching(app: Any, path: str) -> list[APIRoute]:
    return [
        route
        for full_path, route in _flatten(app.router)
        if full_path == path and isinstance(route, APIRoute) and "GET" in (route.methods or set())
    ]


# --------------------------------------------------------------------------- #
# §12.1 / §12.2 / §12.3 over the real registry.
# --------------------------------------------------------------------------- #


async def test_the_registry_is_not_empty() -> None:
    """FIRST. `@pytest.mark.parametrize("key", sorted(REGISTRY))` over an empty
    registry collects zero cases and reports success -- a green suite asserting
    nothing about any real report."""
    assert REGISTRY, "the registry is empty; every parametrized test below is vacuous"


async def test_the_whole_registry_verifies() -> None:
    """§12.2. Also runs at application import (`definitions/__init__.py`); this
    is the version that names the file when it fails."""
    verify_registry()


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_requires_is_the_union_of_the_map_over_sources(key: str) -> None:
    """§12.1. RECOMPUTED FROM THE MAP HERE, not read back off the object: a
    derived field that quietly became a hand-written one would still equal
    itself."""
    definition = REGISTRY[key]
    assert definition.requires == frozenset(
        TABLE_PERMISSIONS[table] for table in definition.sources
    )
    # §0.12: every v1 report spans at least two modules, so the conjunction is
    # exercised on every run rather than being an edge case.
    assert len(definition.requires) >= 2, definition.requires


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_no_export_column_names_a_denylisted_column(key: str) -> None:
    """§12.3's second half, over the column TAIL -- so
    `agents.cert_fingerprint` forbids an export column named
    `cert_fingerprint` however it was aliased."""
    definition = REGISTRY[key]
    tails = {entry.split(".", 1)[1] for entry in _NEVER_A_COLUMN}
    assert tails, "the column denylist is empty; this assertion is vacuous"
    assert not (set(definition.export_columns) & tails)
    assert not (set(definition.row_model.model_fields) & tails)


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_export_columns_are_row_model_fields(key: str) -> None:
    definition = REGISTRY[key]
    fields = set(definition.row_model.model_fields)
    assert set(definition.export_columns) <= fields, set(definition.export_columns) - fields
    assert definition.export_free_text <= set(definition.export_columns)


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_no_params_model_declares_an_org(key: str) -> None:
    """§12.5's second half. Tenancy comes from the token, never from the model.
    A report that accepted an org would be a cross-org read path with a
    friendly name, and §1.2 blocks cross-org roll-up outright."""
    banned = {"org_id", "org", "organization_id"}
    assert not (set(REGISTRY[key].params.model_fields) & banned)
    assert "{" not in REGISTRY[key].key


async def test_the_source_metadata_maps_cover_every_mapped_table() -> None:
    """`meta.sources` is built from `TABLE_MODULE` and the per-module recency
    text. A table in `TABLE_PERMISSIONS` and not in `TABLE_MODULE` is a
    `KeyError` on somebody's first request, at the point where the report is
    otherwise about to succeed."""
    assert set(TABLE_MODULE) == set(TABLE_PERMISSIONS), set(TABLE_MODULE).symmetric_difference(
        set(TABLE_PERMISSIONS)
    )


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_meta_sources_are_derived_and_described(key: str) -> None:
    definition = REGISTRY[key]
    sources = report_sources(definition)
    assert sources
    assert {s.permission for s in sources} == set(definition.requires)
    for source in sources:
        assert source.recency.strip()
        assert source.module.strip()


# --------------------------------------------------------------------------- #
# The generated routes (§6.1, §12.7).
# --------------------------------------------------------------------------- #


async def test_the_flattener_finds_the_report_routes(api_env: Any) -> None:
    """Pinned against an empty result. Without this every assertion below
    passes over nothing -- and it is not hypothetical: a naive
    `[r for r in app.routes if ...]` finds zero reporting routes on FastAPI
    0.139.2."""
    routes = _report_routes(api_env.app)
    # TWO routes per definition -- the report and its CSV export -- PLUS the
    # catalogue at the bare prefix. The count is exact rather than `>=`: a `>=`
    # hides a missing route, which is the failure this whole file is guarding
    # against.
    assert len(routes) == 2 * len(REGISTRY) + 1, sorted(routes)
    assert set(routes) == (
        {f"/api/v1/reports/{key}" for key in REGISTRY}
        | {export_path(key) for key in REGISTRY}
        | {"/api/v1/reports"}
    )


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_a_report_path_is_claimed_by_exactly_one_route(api_env: Any, key: str) -> None:
    """THE `/api/v1/reports` PREFIX IS NOT THIS MODULE'S ALONE, and the spec
    does not say so.

    `app/modules/security_logs/api.py:127` already mounts
    `GET /api/v1/reports/access.csv` -- a `PlainTextResponse` CSV gated on
    `security_logs.read`, registered before reporting. Spec §6 says "Prefix
    `/api/v1/reports`" as though the namespace were free; it is shared, and the
    finding is recorded rather than worked around.

    Nothing collides today (`register()` already refuses a key containing `/`),
    but a future definition keyed `access.csv` would mount a SECOND route at an
    existing path, and FastAPI would silently serve whichever registered first
    -- with a gate and a response type from the other module. This is the
    assertion that makes that a red test rather than a mystery.
    """
    matched = _matching(api_env.app, f"/api/v1/reports/{key}")
    assert len(matched) == 1, f"{len(matched)} routes claim /api/v1/reports/{key}"


async def test_the_shared_prefix_finding_is_real(api_env: Any) -> None:
    """The premise of the test above, pinned. If `security_logs` ever moves its
    export, this goes red and the warning above can be relaxed -- rather than
    quietly describing a hazard that no longer exists."""
    foreign = _matching(api_env.app, "/api/v1/reports/access.csv")
    assert len(foreign) == 1
    assert not foreign[0].endpoint.__module__.startswith("app.modules.reporting")


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_route_permission_returns_reporting_read(api_env: Any, key: str) -> None:
    """§12.7, and BOTH HALVES.

    The first assertion is that the conjunction dependency did not SHADOW the
    kernel gate. The second is the one that goes red if someone expresses the
    conjunction as a second `require_permission`: `route_permission` returns the
    FIRST match it finds while walking the dependant tree, so with two
    `require_permission`-shaped dependencies the first assertion would keep
    passing, order-dependently, which is exactly what §0.5 exists to prevent.
    """
    route = _report_routes(api_env.app)[f"/api/v1/reports/{key}"]
    assert route_permission(route) == "reporting.read"

    gate_shaped = [
        dep
        for dep in _walk_dependant(route.dependant)
        if getattr(getattr(dep, "call", None), "__qualname__", "")
        == "require_permission.<locals>.dependency"
    ]
    assert len(gate_shaped) == 1, f"{len(gate_shaped)} require_permission dependencies"

    # And the conjunction IS there, under its own qualname -- deliberately not
    # one `route_permission` recognises.
    conjunction = [
        dep
        for dep in _walk_dependant(route.dependant)
        if getattr(getattr(dep, "call", None), "__qualname__", "")
        == "require_report_sources.<locals>.dependency"
    ]
    assert len(conjunction) == 1


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_the_route_is_a_get_and_self_describing(api_env: Any, key: str) -> None:
    route = _report_routes(api_env.app)[f"/api/v1/reports/{key}"]
    assert route.methods == {"GET"}
    # A REAL `summary=`, not the docstring fallback -- property 5 as written.
    assert (route.summary or "").strip() == REGISTRY[key].summary
    assert (route.description or "").strip() == REGISTRY[key].description


# --------------------------------------------------------------------------- #
# The conjunction (§0.4, §4.1, §12.4).
# --------------------------------------------------------------------------- #


async def _seeded_org(api_env: Any) -> UUID:
    """An org seeded so EVERY report in the registry has NON-EMPTY rows.

    Fixture-blindness guard: a report that returned nothing for everyone would
    satisfy both halves of every conjunction test below -- 200 with no rows and
    403 are equally "not a leak", and only one of them is the behaviour under
    test. `sla-attainment` is what proved this was not theoretical: its rows are
    per (queue, priority), so two seeded devices left it empty and every
    conjunction case for it asserted nothing.
    """
    return await seed_reportable_org(api_env)


async def test_every_required_query_parameter_is_supplied_by_the_fixture() -> None:
    """FIRST, like `test_the_registry_is_not_empty`, and for the same reason.

    `sla-attainment` is the first report with REQUIRED params (§5.4's closed
    window), so every sweep below that expects a 200 has to supply them. This
    asserts the mapping matches the DEFINITIONS rather than the reports somebody
    remembered -- a future report with a required parameter fails HERE, naming
    it, instead of surfacing as an unexplained 422 in six unrelated tests.
    """
    assert REGISTRY, "vacuous otherwise"
    assert uncovered_required_params() == {}
    # And the premise: at least one report really does require something, so
    # this is not asserting over an empty question.
    assert report_query("sla-attainment"), "sla-attainment has required params"


_CONJUNCTION_CASES = sorted(
    (key, code) for key in REGISTRY for code in sorted(REGISTRY[key].requires)
)


@pytest.mark.parametrize(("key", "withheld"), _CONJUNCTION_CASES)
async def test_withholding_exactly_one_source_code_is_a_403(
    api_env: Any, key: str, withheld: str
) -> None:
    """§12.4, one parametrised case per code, so no report can accidentally
    under-require.

    THE TWO CALLERS DIFFER ONLY IN THAT ONE GRANT. Same org, same seeded data,
    same `reporting.read`; the ONLY variable is `withheld`. And the entitled
    caller must come back with NON-EMPTY rows -- otherwise a report that returns
    nothing for everybody passes both halves and this test is decoration.
    """
    definition = REGISTRY[key]
    org_id = await _seeded_org(api_env)
    path = f"/api/v1/reports/{key}{report_query(key)}"

    all_codes = ["reporting.read", *sorted(definition.requires)]
    _, entitled = await reporting_user(api_env, org_id, *all_codes)
    ok = await api_env.client.get(path, headers=await api_env.headers(org_id, entitled))
    assert ok.status_code == 200, ok.text
    assert ok.json()["rows"], "the entitled caller saw nothing; the 403 below proves nothing"

    partial = [code for code in all_codes if code != withheld]
    _, denied = await reporting_user(api_env, org_id, *partial)
    refused = await api_env.client.get(path, headers=await api_env.headers(org_id, denied))
    assert refused.status_code == 403, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "report_permission_denied"
    # The MISSING CODE ITSELF, not merely "a 403 happened".
    assert detail["missing"] == [withheld], detail


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_reporting_read_alone_is_a_403(api_env: Any, key: str) -> None:
    """§4.3's claim -- "a user holding this alone gets a 403 from every report"
    -- asserted rather than assumed.

    DELIBERATELY REQUESTED WITHOUT THE REQUIRED PARAMS. §4.1 requires the gate to
    complete BEFORE the handler body, and FastAPI resolves route-level
    `dependencies=[...]` before validating the endpoint's own query parameters --
    so `sla-attainment` answers 403 here rather than the 422 it would give an
    entitled caller. A caller who may not run a report must not be able to learn
    its parameter schema by probing it.
    """
    definition = REGISTRY[key]
    org_id = await _seeded_org(api_env)
    _, sub = await reporting_user(api_env, org_id, "reporting.read")
    response = await api_env.client.get(
        f"/api/v1/reports/{key}", headers=await api_env.headers(org_id, sub)
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["missing"] == sorted(definition.requires)


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_a_caller_without_reporting_read_is_a_403(api_env: Any, key: str) -> None:
    """The kernel half of the pair: holding every SOURCE code and not
    `reporting.read` is also refused, by `require_permission`. Both codes are
    necessary; neither is sufficient."""
    definition = REGISTRY[key]
    org_id = await _seeded_org(api_env)
    _, sub = await reporting_user(api_env, org_id, *sorted(definition.requires))
    response = await api_env.client.get(
        f"/api/v1/reports/{key}", headers=await api_env.headers(org_id, sub)
    )
    assert response.status_code == 403, response.text
    assert "reporting.read" in response.text


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_a_group_scoped_grant_is_a_403(api_env: Any, key: str) -> None:
    """§0.6, §12.4. A fleet aggregate computed for someone entitled to part of
    the fleet is either wrong (the denominator includes devices they may not
    see) or a leak (it tells them how many there are).

    THE CONTROL USER HOLDS THE SAME CODES ORG-WIDE, in the same org, over the
    same data -- so the ONLY observable difference between the 200 and the 403
    is the scoping. A test that only asserted the group-scoped 403 could not
    tell "group scoping is refused" from "these codes never work".
    """
    from app.models import DeviceGroup

    definition = REGISTRY[key]
    org_id = await _seeded_org(api_env)
    codes = ["reporting.read", *sorted(definition.requires)]
    path = f"/api/v1/reports/{key}{report_query(key)}"

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        group = DeviceGroup(org_id=org_id, name=f"g-{uuid4().hex[:8]}")
        session.add(group)
        await session.commit()
        group_id = group.id
    await seed_device(api_env.admin_engine, org_id, hostname="gamma", device_group_id=group_id)

    _, org_wide = await reporting_user(api_env, org_id, *codes)
    allowed = await api_env.client.get(path, headers=await api_env.headers(org_id, org_wide))
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["rows"]

    _, scoped = await reporting_user(api_env, org_id, *codes, device_group_id=group_id)
    refused = await api_env.client.get(path, headers=await api_env.headers(org_id, scoped))
    assert refused.status_code == 403, refused.text


# --------------------------------------------------------------------------- #
# Bounded execution (§6.4, §12.12).
# --------------------------------------------------------------------------- #


async def test_the_statement_timeout_applies(api_env: Any) -> None:
    """FALSIFY BY DELETING THE `SET LOCAL` LINE in `run_bounded`: the sleep
    completes, `run_bounded` returns a Result, and this fails on "DID NOT
    RAISE ReportTimeout" -- a different observable value, not a silent pass.

    The 2s sleep against a 50ms ceiling is a 40x margin, so this cannot flake
    on a slow container in the direction that matters.
    """
    org_id = await api_env.create_org("rep-timeout")
    with pytest.raises(ReportTimeout) as exc:
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await run_bounded(
                session,
                select(func.pg_sleep(2)),
                timeout_ms=50,
                report_key="probe",
            )
    assert exc.value.report_key == "probe"
    assert "statement timeout" in str(exc.value)


async def test_a_non_timeout_database_error_is_not_reported_as_a_timeout(
    api_env: Any,
) -> None:
    """`run_bounded` catches on SQLSTATE 57014 and NOTHING ELSE. A blanket
    `except DBAPIError -> ReportTimeout` would tell an operator to narrow their
    window while the real defect sat in the SQL."""
    org_id = await api_env.create_org("rep-error")
    with pytest.raises(Exception) as exc:  # noqa: PT011
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await run_bounded(session, select(func.no_such_function_at_all()))
    assert not isinstance(exc.value, ReportTimeout)


async def test_the_statement_timeout_does_not_leak_to_the_next_checkout(
    pg_cluster: Any,
) -> None:
    """`SET LOCAL`, not `SET` (§6.4).

    THE POOL IS PINNED TO ONE CONNECTION (`pool_size=1, max_overflow=0`) so the
    reuse is DETERMINISTIC. Without that the pool may hand back a different
    connection and this test passes against a plain `SET` -- which is the exact
    bug it exists to catch. FALSIFY by changing `SET LOCAL` to `SET`: the second
    `SHOW` returns `50ms` instead of the server default.
    """
    engine = create_async_engine(pg_cluster.app_url, pool_size=1, max_overflow=0)
    try:
        org_id = uuid4()
        async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
            await run_bounded(session, select(func.now()), timeout_ms=50)
            inside = (await session.execute(text("SHOW statement_timeout"))).scalar_one()
        assert inside == "50ms", inside

        async with AsyncSession(engine) as session:
            after = (await session.execute(text("SHOW statement_timeout"))).scalar_one()
        assert after != "50ms", after
    finally:
        await engine.dispose()


async def test_the_default_timeout_is_a_module_constant_not_a_request_value() -> None:
    """PostgreSQL does not accept a bind parameter in `SET`, so the value is
    interpolated. It must therefore never be reachable from a request: it is an
    `int` module constant, and no params model declares a field that could
    reach it."""
    assert isinstance(REPORT_STATEMENT_TIMEOUT_MS, int)
    assert REPORT_STATEMENT_TIMEOUT_MS > 0
    for key in REGISTRY:
        assert "timeout" not in " ".join(REGISTRY[key].params.model_fields)


# --------------------------------------------------------------------------- #
# Params validation (§6.5).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_an_unknown_query_parameter_is_a_422(api_env: Any, key: str) -> None:
    """`extra="forbid"`. A misspelled `os_familly=windows` that silently
    returned the whole fleet is a wrong number nobody would question."""
    definition = REGISTRY[key]
    org_id = await _seeded_org(api_env)
    _, sub = await reporting_user(
        api_env, org_id, "reporting.read", *sorted(definition.requires)
    )
    response = await api_env.client.get(
        f"/api/v1/reports/{key}{report_query(key, definitely_not_a_parameter=1)}",
        headers=await api_env.headers(org_id, sub),
    )
    assert response.status_code == 422, response.text
    assert "definitely_not_a_parameter" in response.text


@pytest.mark.parametrize("key", sorted(REGISTRY))
async def test_limit_is_bounded_by_the_definitions_max_limit(api_env: Any, key: str) -> None:
    """Property 4 (§0.10), observed on the wire rather than by reading the
    model: `limit` carries an `le=` equal to `definition.max_limit`, and one
    over it is a 422 rather than a clamp."""
    definition = REGISTRY[key]
    field = definition.params.model_fields["limit"]
    les = [getattr(m, "le", None) for m in field.metadata]
    assert definition.max_limit in les, field.metadata

    org_id = await _seeded_org(api_env)
    _, sub = await reporting_user(
        api_env, org_id, "reporting.read", *sorted(definition.requires)
    )
    headers = await api_env.headers(org_id, sub)
    over = await api_env.client.get(
        f"/api/v1/reports/{key}{report_query(key, limit=definition.max_limit + 1)}",
        headers=headers,
    )
    assert over.status_code == 422, over.text
    at = await api_env.client.get(
        f"/api/v1/reports/{key}{report_query(key, limit=definition.max_limit)}",
        headers=headers,
    )
    assert at.status_code == 200, at.text
    assert at.json()["meta"]["limit"] == definition.max_limit
