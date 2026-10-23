"""`CatalogueEntry.params_schema` -- the published params contract (spec §17.11).

**THE DEFECT THIS CLOSES.** The catalogue advertised a report's `summary`,
`description` and `requires` and NOT the shape of its parameters, so a client
could not know what to send, could not validate before sending, and had to
hardcode a params bar out of knowledge the server already held. Two copies of a
contract is how a client and a server come to disagree about it, and `ReportView`
carried the second copy in the form of a hardcoded `from`/`to`/`limit`/`offset`
form.

**THE SCHEMA IS DERIVED, NOT WRITTEN TWICE.** `ReportDefinition.params_json_schema()`
returns `self.params.model_json_schema()`, and `self.params` is the very object
`api.py` binds as the route's query model.
`test_the_published_schema_is_the_model_the_route_validates_with` closes that
loop by reading the annotation off the LIVE route rather than
trusting the sentence -- if the loop ever bound a different model, the published
schema would be describing something that does not validate, which is worse than
publishing nothing.

**PUBLISHING DESCRIBES THE GATE, IT DOES NOT WIDEN IT.** The two directions are
asserted against the live server, per report, per parameter:

* every name the schema publishes is a name the route ACCEPTS (not a 422), so
  the schema cannot be narrower than the gate and mislead a client into omitting
  a filter it could have used; and
* a name the schema does NOT publish is a 422, so `additionalProperties: false`
  is a report of `extra="forbid"` and not decoration.

**FIXTURE BLINDNESS IS THE THING GUARDED AGAINST HERE.** The four v1 reports have
four DIFFERENT params shapes -- a required aliased window, a Literal with a
default, two integers with different ranges, and a report with none of those --
and `_EXPECTED` below is written out BY HAND from reading the definitions, never
computed from the model under test. A test that computed its expectation from
`definition.params` would pass against any bug that hit both sides, and a test
that only asserted "a schema is present" passes against a schema that is wrong.
`test_the_four_published_schemas_are_pairwise_different` is the specific guard
against a loop that published one report's schema for all four.
"""

from typing import Any
from uuid import uuid4

from fastapi.routing import APIRoute

# IMPORTED FOR ITS SIDE EFFECT: `REGISTRY` is populated by importing the
# definitions package, which otherwise happens only when `api_env` builds the
# app -- after collection, so the module-level reads below would see nothing.
from app.modules.reporting import definitions as _definitions  # noqa: F401
from app.modules.reporting.registry import REGISTRY
from tests.reporting_fixtures import report_query, reporting_user, required_query

CATALOGUE = "/api/v1/reports"


# --------------------------------------------------------------------------- #
# WHAT EACH REPORT'S PARAMS SCHEMA MUST SAY, WRITTEN OUT BY HAND.
#
# Read off `definitions/*.py` by a human and typed here as literals. This is the
# INDEPENDENT derivation: `model_json_schema()` appears nowhere in this table, so
# a change to the model that nobody meant makes this red and names the value.
#
# The four differ in every way a params model can differ, which is the point --
# a constant parameter across all fixtures is an untested parameter.
# --------------------------------------------------------------------------- #
_EXPECTED: dict[str, dict[str, Any]] = {
    "fleet-posture": {
        "properties": {
            "os_family",
            "device_group_id",
            "stale_after_days",
            "severity_at_least",
            "limit",
            "offset",
        },
        "required": [],
        "spot": {
            # A Literal WITH a default -- the shape a `<select>` binds to.
            "severity_at_least": {
                "enum": ["info", "warning", "critical"],
                "default": "info",
                "type": "string",
            },
            "stale_after_days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 365},
            # `le=DEFAULT_MAX_LIMIT`. §0.10 property 4 is the reason the number
            # exists at all, and a client that cannot see it will send 5000 and
            # be told no.
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 1000},
            "offset": {"type": "integer", "default": 0, "minimum": 0},
        },
        "absent": {"window_days", "scan_stale_after_intervals", "from", "to", "priority"},
    },
    "backup-coverage": {
        "properties": {"os_family", "device_group_id", "stale_after_days", "limit", "offset"},
        "required": [],
        "spot": {
            "stale_after_days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 365},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 1000},
        },
        # THE REPORT WITH THE FEWEST PARAMETERS. Its absences are what make
        # "every entry got fleet-posture's schema" fail rather than pass.
        "absent": {"severity_at_least", "window_days", "scan_stale_after_intervals", "from", "to"},
    },
    "patch-compliance": {
        "properties": {
            "os_family",
            "device_group_id",
            "window_days",
            "scan_stale_after_intervals",
            "limit",
            "offset",
        },
        "required": [],
        "spot": {
            "window_days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365},
            # A DIFFERENT integer range from every other parameter in the module
            # (1..10, not 1..365), so a control that hardcoded one range is wrong
            # here and only here.
            "scan_stale_after_intervals": {
                "type": "integer",
                "default": 2,
                "minimum": 1,
                "maximum": 10,
            },
        },
        "absent": {"severity_at_least", "stale_after_days", "from", "to"},
    },
    "sla-attainment": {
        "properties": {"from", "to", "queue_id", "priority", "limit", "offset"},
        # THE ONLY REPORT WITH REQUIRED PARAMETERS (§5.4's closed window), and
        # they are ALIASED. Both facts are load-bearing for a client.
        "required": ["from", "to"],
        "spot": {
            "from": {"type": "string", "format": "date-time"},
            "to": {"type": "string", "format": "date-time"},
        },
        # `window_from`/`window_to` are the PYTHON names. A schema publishing
        # them would tell a client to send parameters `extra="forbid"` rejects.
        "absent": {
            "window_from",
            "window_to",
            "severity_at_least",
            "stale_after_days",
            "window_days",
        },
    },
}

# One VALID value per wire parameter name, for probing the live route. Keyed by
# NAME, not by report, because a name means the same thing in every report that
# has it -- and `test_every_published_parameter_has_a_probe_value` is what stops
# a future report's new parameter being silently skipped by every sweep below.
_PROBE: dict[str, str] = {
    # The required window, taken from the SHARED fixture helper rather than
    # written again here -- `uncovered_required_params()` already guards that
    # helper against a future report whose required parameter it does not supply.
    **required_query("sla-attainment"),
    "os_family": "windows",
    "device_group_id": str(uuid4()),
    "stale_after_days": "7",
    "severity_at_least": "warning",
    "window_days": "30",
    "scan_stale_after_intervals": "2",
    "queue_id": str(uuid4()),
    "priority": "high",
    "limit": "1",
    "offset": "0",
}


def _flatten(router: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """COPIED FROM `tests/test_reporting_catalogue.py::_flatten`, and for the
    same reason: FastAPI 0.139.2's `include_router` appends a lazy
    `_IncludedRouter` whose own `.path` is `None`, so a naive `app.routes` filter
    finds NOTHING and every assertion over it passes over an empty list."""
    found: list[tuple[str, Any]] = []
    for route in getattr(router, "routes", []):
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(_flatten(original, prefix + (getattr(context, "prefix", "") or "")))
        else:
            found.append((prefix + getattr(route, "path", ""), route))
    return found


def _routes_for(app: Any, key: str) -> list[APIRoute]:
    """The report route AND its export route -- both take the params model, and
    an export whose params model drifted from the report's would export a
    different query than the page shows."""
    wanted = {f"/api/v1/reports/{key}", f"/api/v1/reports/{key}/export.csv"}
    matched = [
        route
        for path, route in _flatten(app.router)
        if path in wanted and isinstance(route, APIRoute) and "GET" in (route.methods or set())
    ]
    assert len(matched) == 2, f"{len(matched)} routes for {key}, expected the report and its CSV"
    return matched


async def _entries(api_env: Any, org_id: Any, *codes: str) -> dict[str, dict[str, Any]]:
    _, sub = await reporting_user(api_env, org_id, *codes)
    response = await api_env.client.get(CATALOGUE, headers=await api_env.headers(org_id, sub))
    assert response.status_code == 200, response.text
    return {entry["key"]: entry for entry in response.json()["reports"]}


def _every_code() -> list[str]:
    return sorted({"reporting.read"} | {c for key in REGISTRY for c in REGISTRY[key].requires})


# --------------------------------------------------------------------------- #
# The table above is about the reports that exist.
# --------------------------------------------------------------------------- #


async def test_the_expectation_table_covers_every_report() -> None:
    """FIRST, and the guard on everything below: a report absent from `_EXPECTED`
    would be a report whose published schema nothing here checks, and every
    parametrized sweep would go green while ignoring it."""
    assert set(_EXPECTED) == set(REGISTRY), sorted(set(REGISTRY) ^ set(_EXPECTED))
    assert len(REGISTRY) == 4, sorted(REGISTRY)


async def test_every_published_parameter_has_a_probe_value() -> None:
    """`_PROBE` must cover every parameter of every report, or the live-route
    sweep silently tests fewer parameters than exist."""
    published = {
        name
        for key in REGISTRY
        for name in REGISTRY[key].params_json_schema()["properties"]
    }
    assert published <= set(_PROBE), sorted(published - set(_PROBE))
    # And no dead probe: an entry naming a parameter no report has is a probe
    # that proves nothing and hides that the real one went missing.
    assert set(_PROBE) <= published, sorted(set(_PROBE) - published)


# --------------------------------------------------------------------------- #
# Derivation: the published schema IS the model the route validates with.
# --------------------------------------------------------------------------- #


async def test_the_catalogue_publishes_the_definitions_own_schema(api_env: Any) -> None:
    """The wire body equals `definition.params.model_json_schema()`, computed
    here from the definition rather than read back off the response."""
    org_id = await api_env.create_org("rep-ps-derived")
    entries = await _entries(api_env, org_id, "reporting.read")

    assert set(entries) == set(REGISTRY)
    for key, definition in REGISTRY.items():
        assert entries[key]["params_schema"] == definition.params.model_json_schema(), key


async def test_the_published_schema_is_the_model_the_route_validates_with(
    api_env: Any,
) -> None:
    """**THE DERIVATION, CLOSED AT THE ROUTE.** `api.py` binds
    `Annotated[definition.params, Query()]` as the handler's ONLY query
    parameter, and `params_json_schema()` reads the same attribute -- so this
    asserts, off the built app, that the object described is the object that
    validates.

    Without this, "derived from the same definition the server validates
    against" is a claim about two lines of Python that a refactor could quietly
    break in either direction.
    """
    for key, definition in REGISTRY.items():
        for route in _routes_for(api_env.app, key):
            query_params = route.dependant.query_params
            assert len(query_params) == 1, f"{key} {route.path}: {len(query_params)} query params"
            annotation = query_params[0].field_info.annotation
            assert annotation is definition.params, f"{key} {route.path}: {annotation}"


async def test_a_definition_cannot_publish_a_schema_it_does_not_enforce() -> None:
    """There is NO `params_schema=` constructor argument, the same way there is
    no `requires=`. A definition author has no place to write a second copy."""
    from dataclasses import fields

    names = {f.name for f in fields(REGISTRY["fleet-posture"])}
    assert "params_schema" not in names
    assert "params" in names


# --------------------------------------------------------------------------- #
# Content, against a hand-written table. "A schema is present" passes against a
# schema that is wrong.
# --------------------------------------------------------------------------- #


async def test_each_published_schema_matches_the_hand_written_table(api_env: Any) -> None:
    org_id = await api_env.create_org("rep-ps-content")
    entries = await _entries(api_env, org_id, "reporting.read")

    for key, expected in _EXPECTED.items():
        schema = entries[key]["params_schema"]
        properties = schema["properties"]

        assert set(properties) == expected["properties"], key
        assert schema.get("required", []) == expected["required"], key
        # `extra="forbid"`, reported. Read with `.get()` and asserted `is
        # False`: a model that stopped forbidding emits NO `additionalProperties`
        # at all, and a `schema[...]` would fail with a bare `KeyError` naming
        # nothing instead of naming the value it found.
        assert schema.get("additionalProperties") is False, (
            f"{key}: additionalProperties == {schema.get('additionalProperties')!r}"
        )
        assert schema["type"] == "object", key

        for name, facts in expected["spot"].items():
            published = properties[name]
            for fact, value in facts.items():
                assert published[fact] == value, f"{key}.{name}.{fact} == {published.get(fact)!r}"
            # Property 5 of §0.3 reaches the parameters too: a control rendered
            # from a schema with no description is a box with a name on it.
            assert (published.get("description") or "").strip(), f"{key}.{name}"

        for name in expected["absent"]:
            assert name not in properties, f"{key} publishes {name}"


async def test_the_four_published_schemas_are_pairwise_different(api_env: Any) -> None:
    """**THE FIXTURE-BLINDNESS GUARD.** A loop that published one definition's
    schema for every entry -- a closure capturing the wrong variable, a
    module-level constant, a `params_json_schema()` called on the wrong object --
    produces four IDENTICAL schemas and passes every "a schema is present" test
    ever written. Four reports, four different parameter sets, asserted pairwise
    on the property NAMES so the difference is structural rather than a stray
    description string.
    """
    org_id = await api_env.create_org("rep-ps-distinct")
    entries = await _entries(api_env, org_id, "reporting.read")

    shapes = {
        key: frozenset(entries[key]["params_schema"]["properties"]) for key in sorted(REGISTRY)
    }
    assert len(set(shapes.values())) == len(shapes), shapes
    # And the schemas themselves, not only the name sets.
    rendered = {key: repr(sorted(entries[key]["params_schema"].items())) for key in shapes}
    assert len(set(rendered.values())) == len(rendered)


async def test_the_schema_names_the_wire_parameter_not_the_python_attribute(
    api_env: Any,
) -> None:
    """`sla-attainment`'s window is `window_from`/`window_to` in Python and
    `from`/`to` on the wire. A client binding a control to the Python name would
    send a parameter `extra="forbid"` rejects, so the alias is the whole value of
    publishing at all -- and it is the one thing a naive
    `model_json_schema(by_alias=False)` would get wrong while still looking
    plausible.

    The route is asked BOTH ways, so this cannot pass by the schema and the
    server agreeing on a name neither accepts.
    """
    org_id = await api_env.create_org("rep-ps-alias")
    entries = await _entries(api_env, org_id, "reporting.read")
    schema = entries["sla-attainment"]["params_schema"]
    assert set(schema["required"]) == {"from", "to"}
    assert "window_from" not in schema["properties"]

    codes = ["reporting.read", *sorted(REGISTRY["sla-attainment"].requires)]
    _, sub = await reporting_user(api_env, org_id, *codes)
    headers = await api_env.headers(org_id, sub)

    window = required_query("sla-attainment")
    ok = await api_env.client.get(
        f"/api/v1/reports/sla-attainment{report_query('sla-attainment')}", headers=headers
    )
    assert ok.status_code == 200, ok.text

    python_names = await api_env.client.get(
        "/api/v1/reports/sla-attainment",
        params={"window_from": window["from"], "window_to": window["to"]},
        headers=headers,
    )
    assert python_names.status_code == 422, python_names.text


# --------------------------------------------------------------------------- #
# The schema DESCRIBES the gate. It does not widen it, and it is not narrower.
# --------------------------------------------------------------------------- #


async def test_every_published_parameter_is_accepted_by_the_live_route(
    api_env: Any,
) -> None:
    """Direction one: the schema is not NARROWER than the gate.

    Each published parameter is sent, ALONE (plus whatever the report requires),
    against the real route with a real entitled caller. A 422 would mean the
    catalogue advertised a parameter the server refuses.
    """
    org_id = await api_env.create_org("rep-ps-accept")

    for key, definition in REGISTRY.items():
        codes = ["reporting.read", *sorted(definition.requires)]
        _, sub = await reporting_user(api_env, org_id, *codes)
        headers = await api_env.headers(org_id, sub)
        published = sorted(definition.params_json_schema()["properties"])
        assert published, key

        for name in published:
            query = {**required_query(key), name: _PROBE[name]}
            response = await api_env.client.get(
                f"/api/v1/reports/{key}", params=query, headers=headers
            )
            assert response.status_code == 200, f"{key}?{name}={_PROBE[name]}: {response.text}"


async def test_a_parameter_the_schema_does_not_publish_is_refused(api_env: Any) -> None:
    """Direction two: the schema is not WIDER than the gate, and
    `additionalProperties: false` is a report of `extra="forbid"` rather than a
    decoration.

    The unpublished name is DERIVED from a published one by suffixing it, so it
    is a plausible misspelling of a real parameter rather than an obviously
    absurd string -- the 422 has to come from the model, not from a router that
    happened to dislike the shape.
    """
    org_id = await api_env.create_org("rep-ps-forbid")

    for key, definition in REGISTRY.items():
        codes = ["reporting.read", *sorted(definition.requires)]
        _, sub = await reporting_user(api_env, org_id, *codes)
        headers = await api_env.headers(org_id, sub)
        published = sorted(definition.params_json_schema()["properties"])

        unpublished = f"{published[0]}x"
        assert unpublished not in published
        response = await api_env.client.get(
            f"/api/v1/reports/{key}",
            params={**required_query(key), unpublished: "1"},
            headers=headers,
        )
        assert response.status_code == 422, f"{key}?{unpublished}=1: {response.text}"
        assert unpublished in response.text, response.text


async def test_publishing_a_bound_does_not_lift_it(api_env: Any) -> None:
    """The schema says `limit` has a `maximum`; the server still enforces it.
    Reading the ceiling is not permission to exceed it -- and this is the one
    place a "publish the contract" change could plausibly have turned a
    validated model into a documented one.
    """
    org_id = await api_env.create_org("rep-ps-bounds")
    entries = await _entries(api_env, org_id, "reporting.read")

    for key, definition in REGISTRY.items():
        ceiling = entries[key]["params_schema"]["properties"]["limit"]["maximum"]
        codes = ["reporting.read", *sorted(definition.requires)]
        _, sub = await reporting_user(api_env, org_id, *codes)
        headers = await api_env.headers(org_id, sub)

        over = await api_env.client.get(
            f"/api/v1/reports/{key}",
            params={**required_query(key), "limit": str(int(ceiling) + 1)},
            headers=headers,
        )
        assert over.status_code == 422, f"{key} limit={int(ceiling) + 1}: {over.text}"

        at = await api_env.client.get(
            f"/api/v1/reports/{key}",
            params={**required_query(key), "limit": str(int(ceiling))},
            headers=headers,
        )
        assert at.status_code == 200, at.text


# --------------------------------------------------------------------------- #
# Who the schema travels to.
#
# **THE CATALOGUE IS NOT FILTERED BY ENTITLEMENT, AND THAT IS THE SPEC'S
# DECISION, NOT AN OVERSIGHT** (§6.2, §11, and `build_catalogue`'s docstring):
# every report is listed with `entitled` and `missing_permissions`, because a
# catalogue that hid the reports you cannot run gives an under-permissioned
# operator the same 200 as a smaller deployment. `params_schema` travels on the
# same footing as `summary`, `description` and `requires` -- it is compiled into
# the image, it is byte-identical for every caller in every org, and it says
# nothing whatever about any org's data.
#
# The gate that DOES apply is `reporting.read`, and it applies to the whole
# response. That is what these two tests pin.
# --------------------------------------------------------------------------- #


async def test_no_schema_reaches_a_caller_without_reporting_read(api_env: Any) -> None:
    """A caller holding EVERY source code and not `reporting.read` gets a 403
    with no schema in it -- not a filtered catalogue, not an empty one.

    Asserted on the BODY TEXT for actual parameter names, not just for the
    absence of the word `params_schema`: a 403 that leaked the properties under
    another key would pass the weaker check.
    """
    org_id = await api_env.create_org("rep-ps-gate")
    every_source = sorted({code for key in REGISTRY for code in REGISTRY[key].requires})
    _, sub = await reporting_user(api_env, org_id, *every_source)

    response = await api_env.client.get(CATALOGUE, headers=await api_env.headers(org_id, sub))
    assert response.status_code == 403, response.text
    body = response.text
    assert "params_schema" not in body
    for name in sorted(_PROBE):
        assert name not in body, f"the 403 body names the parameter {name}"
    # The premise: these callers really do hold the source codes, so the 403 is
    # `reporting.read` and nothing else.
    assert every_source, "no source codes -- the fixture would prove nothing"


async def test_the_schema_is_identical_for_an_entitled_and_an_unentitled_caller(
    api_env: Any,
) -> None:
    """§11, made checkable for the new field.

    Two callers in the SAME org differing ONLY in their grants: one entitled to
    `fleet-posture`, one entitled to nothing. Both are shown the same schema for
    every report, because the schema is platform data.

    THE TEST WOULD BE VACUOUS IF THE TWO CALLERS WERE NOT OBSERVABLY DIFFERENT,
    so `entitled` is asserted to DIFFER in the same breath -- otherwise "the two
    responses are equal" could be two identical callers.
    """
    org_id = await api_env.create_org("rep-ps-entitle")
    bare = await _entries(api_env, org_id, "reporting.read")
    granted = await _entries(api_env, org_id, "reporting.read", "devices.read", "health.read")

    assert bare["fleet-posture"]["entitled"] is False
    assert granted["fleet-posture"]["entitled"] is True

    for key in REGISTRY:
        assert bare[key]["params_schema"] == granted[key]["params_schema"], key
        assert bare[key]["params_schema"]["properties"], key


async def test_the_schema_is_identical_across_orgs(api_env: Any) -> None:
    """Platform data, not org data. One org is seeded with a device and a device
    group; the other is empty. An entry that let an org's own values leak into
    the schema -- an enum of that org's queues, say, which is exactly the shape
    of "helpful" a params schema invites -- would differ here.
    """
    from tests.reporting_fixtures import seed_device, seed_device_group

    populated = await api_env.create_org("rep-ps-full")
    empty = await api_env.create_org("rep-ps-empty")
    await seed_device(api_env.admin_engine, populated, hostname="alpha.corp.local")
    group_id = await seed_device_group(api_env.admin_engine, populated, name="Workstations")

    codes = _every_code()
    first = await _entries(api_env, populated, *codes)
    second = await _entries(api_env, empty, *codes)

    for key in REGISTRY:
        assert first[key]["params_schema"] == second[key]["params_schema"], key
    # The premise: the populated org really does hold values a leak could carry.
    assert str(group_id) not in str(first)
    assert "alpha.corp.local" not in str(first)
    assert first["fleet-posture"]["entitled"] is True
