"""The MCP scrub's static half: every secret-looking response field of an
exposed tool is on the reviewed allowlist, and every allowlist entry is real.

The runtime half (a hit that is not on the allowlist blocks the whole result)
lives with the tool-call path.
"""

import collections.abc
import types
import typing
from typing import Any

import pytest
from pydantic import BaseModel

from app.mcp.scrub_allowlist import REVIEWED_FIELDS, secret_key_path, secret_key_paths


def _field_paths(ann: Any, prefix: str = "", seen: frozenset[Any] = frozenset()) -> set[str]:
    """Every JSON field path a response annotation can produce; lists as `[]`."""
    out: set[str] = set()
    origin, args = typing.get_origin(ann), typing.get_args(ann)
    if origin in (list, tuple, set, frozenset, collections.abc.Sequence):
        for a in args:
            if a is not Ellipsis:
                out |= _field_paths(a, prefix + "[]", seen)
    elif origin in (typing.Union, types.UnionType, typing.Annotated):
        for a in args:
            out |= _field_paths(a, prefix, seen)
    elif origin is dict and len(args) == 2:
        out |= _field_paths(args[1], prefix + ".*", seen)
    elif isinstance(ann, type) and issubclass(ann, BaseModel) and ann not in seen:
        for name, f in ann.model_fields.items():
            key = f.alias or name
            p = f"{prefix}.{key}" if prefix else key
            out.add(p)
            out |= _field_paths(f.annotation, p, seen | {ann})
    return out


def test_a_planted_secret_key_is_found_at_any_depth() -> None:
    assert (
        secret_key_path({"items": [{"name": "x", "nested": {"api_token": "s"}}]})
        == "items[].nested.api_token"
    )
    assert secret_key_path({"name": "x", "count": 2}) is None


def test_a_top_level_list_and_nested_lists_use_the_bracket_notation() -> None:
    assert secret_key_path([{"name": "x"}, {"password": "p"}]) == "[].password"
    assert secret_key_path({"rows": [[{"ok": 1}], [{"apiKey": "k"}]]}) == "rows[][].apiKey"
    assert secret_key_path([]) is None
    assert secret_key_path("token") is None  # a VALUE is never a key
    assert secret_key_path({"note": ["password", "token"]}) is None


def test_the_first_hit_is_reported_and_a_secret_key_blocks_even_with_a_null_value() -> None:
    assert secret_key_path({"a": 1, "secret": None, "b": {"token": "t"}}) == "secret"


def test_a_tool_carries_its_routes_response_model() -> None:
    from fastapi.routing import APIRoute

    from app.core.router_inspect import flatten
    from app.mcp.catalog import build_catalog
    from tests.tool_surface import build_router_only_app

    app = build_router_only_app()
    routes = {
        (m, p): r
        for p, r in flatten(app.router)
        if isinstance(r, APIRoute)
        for m in r.methods or set()
    }
    catalog = build_catalog(app)
    for t in catalog.values():
        assert t.response_model is routes[(t.method, t.path)].response_model, t.name
    # Most routes declare a response model (by `response_model=` or by the
    # handler's return annotation, which FastAPI also stores there); a
    # catalogue that dropped it would make the static walk vacuous.
    typed = [t for t in catalog.values() if t.response_model is not None]
    assert len(typed) > len(catalog) // 2


def test_every_secret_looking_response_field_of_an_exposed_tool_is_reviewed() -> None:
    from app.core.secrets import looks_like_secret_key
    from app.mcp.catalog import build_catalog
    from tests.tool_surface import build_router_only_app

    unreviewed = []
    for t in build_catalog(build_router_only_app()).values():
        for field_path in _field_paths(t.response_model):
            leaf = field_path.rsplit(".", 1)[-1].removesuffix("[]")
            if looks_like_secret_key(leaf) and (
                f"{t.method} {t.path}",
                field_path,
            ) not in REVIEWED_FIELDS:
                unreviewed.append((t.method, t.path, field_path))
    assert not unreviewed, unreviewed


def test_every_reviewed_field_is_real_and_reasoned() -> None:
    from app.mcp.catalog import build_catalog
    from tests.tool_surface import build_router_only_app

    fields = {
        f"{t.method} {t.path}": _field_paths(t.response_model)
        for t in build_catalog(build_router_only_app()).values()
    }
    for (route, field_path), reason in REVIEWED_FIELDS.items():
        assert route in fields, f"stale route: {route}"
        assert field_path in fields[route], f"stale field: {route} {field_path}"
        assert reason.strip(), (route, field_path)


def test_every_hit_is_found_and_a_reviewed_key_does_not_hide_one_beneath_or_after_it() -> None:
    from app.mcp.scrub_allowlist import secret_key_paths

    payload = {"has_key": True, "key": {"password": "p"}, "items": [{"token": "t"}]}
    assert list(secret_key_paths(payload)) == ["has_key", "key", "key.password", "items[].token"]


def test_the_runtime_rule_allows_only_reviewed_hits_on_their_own_route() -> None:
    from app.mcp.scrub_allowlist import first_unreviewed_secret_path

    route = "GET /api/v1/licenses/{license_id}"
    assert ("GET /api/v1/licenses/{license_id}", "has_key") in REVIEWED_FIELDS
    assert first_unreviewed_secret_path(route, {"name": "x", "has_key": True}) is None
    # A reviewed field does not excuse an unreviewed one after it ...
    assert (
        first_unreviewed_secret_path(route, {"has_key": True, "license_key": "K"})
        == "license_key"
    )
    # ... and a field reviewed on one route is not reviewed on another.
    assert first_unreviewed_secret_path("GET /api/v1/me", {"has_key": True}) == "has_key"


def test_an_open_dict_entry_matches_any_one_key_and_nothing_else() -> None:
    from app.mcp.scrub_allowlist import first_unreviewed_secret_path, is_reviewed

    route = "GET /api/v1/subscriptions/spend"
    assert (route, ".*[].group_key") in REVIEWED_FIELDS
    assert is_reviewed(route, "by_vendor[].group_key")
    assert not is_reviewed(route, "by_vendor[].nested[].group_key")
    assert not is_reviewed(route, "a.b[].group_key")
    assert not is_reviewed(route, "by_vendor[].group_keys")
    # The open dict's own key is data, and a secret-shaped one is never reviewed.
    payload = {"by_vendor": [{"group_key": "Acme"}], "token": []}
    assert first_unreviewed_secret_path(route, payload) == "token"


def test_no_tool_answers_a_notification_channels_config() -> None:
    from app.mcp.catalog import build_catalog
    from app.modules.notification.schema import ChannelOut
    from tests.tool_surface import build_router_only_app

    def mentions(ann: Any) -> bool:
        if ann is ChannelOut:
            return True
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            return any(mentions(f.annotation) for f in ann.model_fields.values())
        return any(mentions(a) for a in typing.get_args(ann))

    catalog = build_catalog(build_router_only_app())
    leaking = [t.name for t in catalog.values() if mentions(t.response_model)]
    assert not leaking, leaking


def test_anything_but_decoded_json_is_refused_rather_than_passed() -> None:
    class Leaky(BaseModel):
        api_token: str = "s"

    for payload in (Leaky(), b'{"api_token": "s"}', {"items": [object()]}):
        with pytest.raises(TypeError):
            list(secret_key_paths(payload))
    # A JSON string VALUE is data, not a container; it is never parsed or judged.
    assert list(secret_key_paths({"note": '{"api_token": "s"}'})) == []
