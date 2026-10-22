"""The MCP tool catalogue: every /api/v1 route decided, tools well-formed (MCP spec §4.1)."""

import re

import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from app.core.router_inspect import flatten
from app.mcp import exclusions
from app.mcp.catalog import build_catalog, route_decisions
from tests.tool_surface import build_router_only_app


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return build_router_only_app()


def _api_routes(app: FastAPI) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for path, route in flatten(app.router):
        if path.startswith("/api/v1/") and getattr(route, "methods", None):
            out |= {(m, path) for m in route.methods if m != "HEAD"}
    return out


def test_every_route_is_decided(app: FastAPI) -> None:
    decided = route_decisions(app)
    missing = _api_routes(app) - {k for k, v in decided.items() if v != "undecided"}
    assert not missing, (
        "undecided routes (add to the catalogue or an exclusion with a reason): "
        f"{sorted(missing)}"
    )


def test_every_exclusion_names_a_real_route_and_has_a_reason(app: FastAPI) -> None:
    real = _api_routes(app)
    tables = (exclusions.NEVER_EXPOSED_REASONS, exclusions.FILE_OR_STREAM, exclusions.NOT_FOR_MCP)
    for table in tables:
        for key, reason in table.items():
            assert key in real, f"stale exclusion {key}"
            assert reason.strip(), key


def test_never_exposed_routes_are_not_tools(app: FastAPI) -> None:
    tools = {(t.method, t.path) for t in build_catalog(app).values()}
    assert not (tools & exclusions.NEVER_EXPOSED)
    assert not (tools & set(exclusions.FILE_OR_STREAM))


def test_no_tool_is_gated_on_a_forbidden_code(app: FastAPI) -> None:
    from app.core.mcp_channel import MCP_FORBIDDEN_CODES

    assert not [t.name for t in build_catalog(app).values() if t.permission in MCP_FORBIDDEN_CODES]


def test_names_are_unique_valid_and_short(app: FastAPI) -> None:
    names = list(build_catalog(app))
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z0-9_]{1,64}", n) for n in names)


def test_examples_from_the_spec(app: FastAPI) -> None:
    cat = build_catalog(app)
    by_route = {(t.method, t.path): n for n, t in cat.items()}
    assert by_route[("GET", "/api/v1/devices")] == "devices_list"
    assert by_route[("GET", "/api/v1/devices/{device_id}")] == "devices_get"
    approve = ("POST", "/api/v1/devices/{device_id}/review/approve")
    assert by_route[approve] == "devices_review_approve"


def test_annotations(app: FastAPI) -> None:
    for t in build_catalog(app).values():
        assert t.annotations.get("readOnlyHint", False) == (t.method == "GET")
        if t.method == "DELETE" or (t.method, t.path) in exclusions.DESTRUCTIVE:
            assert t.annotations["destructiveHint"] is True
        if t.method == "PUT":
            assert t.annotations["idempotentHint"] is True


def test_ungated_routes_are_tools_only_when_listed_open(app: FastAPI) -> None:
    for t in build_catalog(app).values():
        assert t.permission is not None or (t.method, t.path) in exclusions.OPEN_TO_ANY_SIGNED_IN


def test_input_schema_is_an_object_with_path_params_required(app: FastAPI) -> None:
    t = build_catalog(app)["devices_get"]
    assert t.input_schema["type"] == "object"
    assert "device_id" in t.input_schema["properties"] and "device_id" in t.input_schema["required"]
    assert "#/components/" not in repr(t.input_schema)


# Beyond the brief's examples: properties every generated tool must hold, so an
# MCP client never meets a malformed tool.


def test_every_input_schema_is_valid_json_schema_and_self_contained(app: FastAPI) -> None:
    for name, t in build_catalog(app).items():
        Draft202012Validator.check_schema(t.input_schema)
        assert t.input_schema["type"] == "object", name
        assert t.input_schema["additionalProperties"] is False, name
        assert "#/components/" not in repr(t.input_schema), name
        assert set(t.path_params) <= set(t.input_schema["required"]), name
        defs = t.input_schema.get("$defs", {})
        for ref in re.findall(r"'\$ref': '([^']+)'", repr(t.input_schema)):
            assert ref.startswith("#/$defs/") and ref.removeprefix("#/$defs/") in defs, (name, ref)
        for d in defs.values():
            assert "$defs" not in d, name


def test_every_destructive_entry_names_a_tool(app: FastAPI) -> None:
    tools = {(t.method, t.path) for t in build_catalog(app).values()}
    assert not (exclusions.DESTRUCTIVE - tools)


def test_open_to_any_signed_in_entries_are_ungated_real_routes(app: FastAPI) -> None:
    decided = route_decisions(app)
    for key in exclusions.OPEN_TO_ANY_SIGNED_IN:
        assert decided.get(key) == "tool", key
    cat = {(t.method, t.path): t for t in build_catalog(app).values()}
    for key in exclusions.OPEN_TO_ANY_SIGNED_IN:
        assert cat[key].permission is None, (
            f"{key} is gated; it does not belong in OPEN_TO_ANY_SIGNED_IN"
        )


def test_name_overrides_name_tools(app: FastAPI) -> None:
    tools = {(t.method, t.path) for t in build_catalog(app).values()}
    assert not (set(exclusions.NAME_OVERRIDES) - tools)


def test_area_is_the_first_segment(app: FastAPI) -> None:
    for t in build_catalog(app).values():
        assert t.area == t.path.removeprefix("/api/v1/").split("/")[0]


def test_a_duplicate_name_fails_the_build(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    clash = {("GET", "/api/v1/devices/{device_id}"): "devices_list"}
    monkeypatch.setattr(exclusions, "NAME_OVERRIDES", clash)
    with pytest.raises(RuntimeError, match="devices_list"):
        build_catalog(app)


def test_an_unlisted_ungated_route_is_undecided(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exclusions, "OPEN_TO_ANY_SIGNED_IN", frozenset())
    assert route_decisions(app)[("GET", "/api/v1/me")] == "undecided"
    assert ("GET", "/api/v1/me") not in {(t.method, t.path) for t in build_catalog(app).values()}


def test_forbidden_codes_are_excluded_without_a_table_entry(app: FastAPI) -> None:
    decided = route_decisions(app)
    # credentials.read: not in any table, excluded by its permission code alone.
    assert decided[("GET", "/api/v1/credentials")].startswith("excluded: gated on credentials.")


# Fix round 1 (controller review of Task 4).


def test_confirming_an_in_app_ai_action_is_not_a_tool(app: FastAPI) -> None:
    confirm = ("POST", "/api/v1/ai/actions/{ai_action_id}/confirm")
    assert confirm in exclusions.NOT_FOR_MCP
    assert route_decisions(app)[confirm].startswith("excluded: ")
    assert confirm not in {(t.method, t.path) for t in build_catalog(app).values()}


def _names(app: FastAPI) -> dict[tuple[str, str], str]:
    return {(t.method, t.path): n for n, t in build_catalog(app).items()}


def test_a_get_without_a_trailing_param_is_get_unless_it_returns_a_collection(
    app: FastAPI,
) -> None:
    names = _names(app)
    # A single object: `get`.
    assert names[("GET", "/api/v1/me")] == "me_get"
    assert names[("GET", "/api/v1/orgs/current")] == "orgs_current_get"
    assert names[("GET", "/api/v1/settings/email")] == "settings_email_get"
    package = ("GET", "/api/v1/agent/releases/{release_id}/deployment-package")
    assert names[package] == "agent_releases_deployment_package_get"
    # A bare array: `list`.
    assert names[("GET", "/api/v1/agent/releases")] == "agent_releases_list"
    # A page, an object with an `items` array: `list`.
    assert names[("GET", "/api/v1/devices")] == "devices_list"


def test_a_post_on_a_thing_is_create_only_when_it_answers_201(app: FastAPI) -> None:
    names = _names(app)
    # 201: a new member of a sub-collection.
    assert names[("POST", "/api/v1/users/{user_id}/roles")] == "users_roles_create"
    assert names[("POST", "/api/v1/people/{person_id}/logins")] == "people_logins_create"
    # 200: an action on the thing, no suffix.
    approve = ("POST", "/api/v1/devices/{device_id}/review/approve")
    assert names[approve] == "devices_review_approve"
    assert names[("POST", "/api/v1/devices/{device_id}/review/reject")] == "devices_review_reject"


def test_retry_and_purge_are_destructive(app: FastAPI) -> None:
    cat = {(t.method, t.path): t for t in build_catalog(app).values()}
    for key in (
        ("POST", "/api/v1/jobs/{job_id}/retry"),
        ("POST", "/api/v1/docs/documents/{document_id}/versions/{version}/purge"),
    ):
        assert key in exclusions.DESTRUCTIVE
        assert cat[key].annotations["destructiveHint"] is True


def test_a_page_keyed_by_the_resource_name_is_a_list(app: FastAPI) -> None:
    # `{"users": [...]}` on `GET /users` is a page as much as `{"items": [...]}`;
    # `users_get` would read as fetching one user.
    names = _names(app)
    assert names[("GET", "/api/v1/users")] == "users_list"
    assert names[("GET", "/api/v1/roles")] == "roles_list"
    assert names[("GET", "/api/v1/email-intake/messages")] == "email_intake_messages_list"
    # An object that merely contains an array is not a page: `GET /me` has
    # `permissions` and `roles` arrays and is one person.
    assert names[("GET", "/api/v1/me")] == "me_get"
    # The one list whose page key is neither: an override.
    assert names[("GET", "/api/v1/platform/errors")] == "platform_errors_list"
