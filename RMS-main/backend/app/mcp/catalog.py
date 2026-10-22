"""MCP tools generated from the live API (MCP server design §4.1).

One tool per decided `/api/v1` route. `route_decisions` says, for every HTTP
`(method, path)` under `/api/v1`, whether it is a tool or excluded and why;
`build_catalog` turns the tools into `Tool`s with a name, an input schema taken
from the route's own OpenAPI operation, and MCP annotations.

WebSocket routes are never tools and are skipped here without an entry, because
they are not HTTP `(method, path)` pairs: `/api/v1/ws` (the agent gateway) and
`/api/v1/remote/sessions/{session_id}/screen` (the remote-session relay).
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.core.mcp_channel import MCP_FORBIDDEN_CODES
from app.core.rbac import route_permission
from app.core.router_inspect import flatten
from app.mcp import exclusions
from app.mcp.descriptions import DESCRIPTIONS

PREFIX = "/api/v1/"
_VERB = {"GET": "get", "POST": "create", "PUT": "set", "PATCH": "update", "DELETE": "delete"}
_NAME = re.compile(r"[a-z0-9_]{1,64}")
_REF_PREFIXES = ("#/$defs/", "#/components/schemas/")

TOOL = "tool"
UNDECIDED = "undecided"


@dataclass(frozen=True)
class Tool:
    name: str  # [a-z0-9_]{1,64}
    method: str  # GET/POST/PUT/PATCH/DELETE
    path: str  # full template, e.g. /api/v1/devices/{device_id}
    area: str  # first segment after /api/v1/
    permission: str | None  # rbac.route_permission(route); None only if OPEN_TO_ANY_SIGNED_IN
    path_params: tuple[str, ...]
    query_params: tuple[str, ...]
    has_body: bool
    input_schema: dict[str, Any]  # JSON Schema, type=object, $defs inlined
    annotations: dict[str, bool]  # readOnlyHint / destructiveHint / idempotentHint
    description: str
    # The route's declared response model (`response_model=` or the handler's
    # return annotation, which FastAPI stores in the same place), or None. The
    # scrub allowlist's static test walks it; the runtime scrub never trusts it.
    response_model: Any | None = None


def derive_name(
    method: str, path: str, *, returns_collection: bool = False, answers_201: bool = False
) -> str:
    """The tool's name, from the route. AI clients key a person's per-tool
    approval settings on it, so it must say what the tool does.

    - `GET` ending in a `{param}` is `get`. Any other `GET` is `list` when it
      `returns_collection` (its 200 body is an array or a page; see
      `_returns_collection`), and `get` otherwise (`GET /me` -> `me_get`).
    - `POST` whose path has a `{param}` and ends in a literal is an action on a
      thing, with no suffix (`POST /devices/{id}/review/approve` ->
      `devices_review_approve`), unless it `answers_201`: then it adds a member
      to a sub-collection (`POST /users/{id}/roles` -> `users_roles_create`).
      Any other `POST` is `create`.
    - `PUT` -> `set`, `PATCH` -> `update`, `DELETE` -> `delete`.
    """
    segments = [s for s in path.removeprefix(PREFIX).split("/") if s]
    words = [
        re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") for s in segments if not s.startswith("{")
    ]
    ends_with_param = bool(segments) and segments[-1].startswith("{")
    has_param = any(s.startswith("{") for s in segments)
    if method == "GET":
        verb = "list" if returns_collection and not ends_with_param else "get"
    elif method == "POST":
        action = has_param and not ends_with_param
        verb = "" if action and not answers_201 else "create"
    else:
        verb = _VERB[method]
    return "_".join(w for w in [*words, verb] if w)[:64]


def _resolve(node: Any, components: dict[str, Any]) -> Any:
    """`node` with a top-level component `$ref` followed (repeatedly)."""
    seen: set[str] = set()
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if ref in seen or not ref.startswith("#/components/schemas/"):
            break
        seen.add(ref)
        node = components.get(ref.removeprefix("#/components/schemas/"), {})
    return node


def _returns_collection(op: dict[str, Any], components: dict[str, Any], path: str) -> bool:
    """Whether the operation's 200 JSON body is a collection: an array, or a
    page -- an object whose `items` property is an array, or whose array
    property is named after the path's last segment (`GET /users` answering
    `{"users": [...]}`). An object that merely contains an array is not a page:
    `GET /me` carries `permissions` and `roles` and is one person."""
    content = op.get("responses", {}).get("200", {}).get("content", {})
    schema = _resolve(content.get("application/json", {}).get("schema", {}), components)
    if not isinstance(schema, dict):
        return False
    if schema.get("type") == "array":
        return True
    last = path.rstrip("/").rsplit("/", 1)[-1].replace("-", "_")
    properties = schema.get("properties") or {}
    for key in ("items", last):
        member = _resolve(properties.get(key, {}), components)
        if isinstance(member, dict) and member.get("type") == "array":
            return True
    return False


def _http_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    """Every HTTP `(method, path, route)` under `/api/v1`, `HEAD` excluded."""
    out: list[tuple[str, str, APIRoute]] = []
    for path, route in flatten(app.router):
        if not path.startswith(PREFIX) or not isinstance(route, APIRoute):
            continue
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            out.append((method, path, route))
    return out


def _decide(method: str, path: str, route: APIRoute) -> str:
    key = (method, path)
    for table in (
        exclusions.NEVER_EXPOSED_REASONS,
        exclusions.FILE_OR_STREAM,
        exclusions.NOT_FOR_MCP,
    ):
        if key in table:
            return f"excluded: {table[key]}"
    permission = route_permission(route)
    if permission in MCP_FORBIDDEN_CODES:
        return f"excluded: gated on {permission}, which is never reachable over MCP"
    if permission is not None or key in exclusions.OPEN_TO_ANY_SIGNED_IN:
        return TOOL
    return UNDECIDED


def route_decisions(app: FastAPI) -> dict[tuple[str, str], str]:
    """`(method, path)` -> `"tool"`, `"excluded: <reason>"`, or `"undecided"`
    (a route nobody has decided on; the partition test fails on it)."""
    return {(m, p): _decide(m, p, r) for m, p, r in _http_routes(app)}


def _referenced_defs(node: Any, components: dict[str, Any]) -> dict[str, Any]:
    """Every component schema reachable from `node` by `$ref`, transitively."""
    found: dict[str, Any] = {}
    stack: list[Any] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            ref = cur.get("$ref")
            if isinstance(ref, str):
                for prefix in _REF_PREFIXES:
                    if ref.startswith(prefix):
                        name = ref.removeprefix(prefix)
                        if name not in found:
                            found[name] = components[name]
                            stack.append(components[name])
            stack.extend(v for k, v in cur.items() if k != "$ref")
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


def _rebase(node: Any) -> Any:
    """A deep copy of `node` with every component `$ref` pointed at `$defs`."""
    return json.loads(json.dumps(node).replace('"#/components/schemas/', '"#/$defs/'))


def _schema_for(
    op: dict[str, Any], components: dict[str, Any], where: str
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...], bool]:
    props: dict[str, Any] = {}
    required: list[str] = []
    path_params: list[str] = []
    query_params: list[str] = []
    for p in op.get("parameters", []):
        if p["in"] not in ("path", "query"):
            continue  # headers and cookies are never tool inputs
        name = p["name"]
        if name in props:
            raise RuntimeError(  # i18n-exempt: build-time catalogue error, never shown to a person
                f"{where}: parameter {name!r} appears twice"
            )
        prop = dict(p.get("schema", {}))
        if p.get("description"):
            prop["description"] = p["description"]
        props[name] = prop
        (path_params if p["in"] == "path" else query_params).append(name)
        if p.get("required") or p["in"] == "path":
            required.append(name)
    has_body = "requestBody" in op
    if has_body:
        content = op["requestBody"].get("content", {})
        if "application/json" not in content:
            # Non-JSON bodies are uploads, which belong in FILE_OR_STREAM.
            raise RuntimeError(  # i18n-exempt: build-time catalogue error, never shown to a person
                f"{where}: request body is not JSON ({sorted(content)})"
            )
        if "body" in props:
            raise RuntimeError(  # i18n-exempt: build-time catalogue error, never shown to a person
                f"{where}: a parameter named 'body' collides with the body"
            )
        props["body"] = content["application/json"].get("schema", {})
        if op["requestBody"].get("required"):
            required.append("body")
    schema: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }
    used = _referenced_defs(schema, components)
    schema = _rebase(schema)
    if used:
        schema["$defs"] = _rebase(dict(sorted(used.items())))
    return schema, tuple(path_params), tuple(query_params), has_body


def _annotations(method: str, path: str) -> dict[str, bool]:
    annotations = {"readOnlyHint": method == "GET"}
    if method == "DELETE" or (method, path) in exclusions.DESTRUCTIVE:
        annotations["destructiveHint"] = True
    if method == "PUT":
        annotations["idempotentHint"] = True
    return annotations


def build_catalog(app: FastAPI) -> dict[str, Tool]:
    """name -> `Tool`, for every route `route_decisions` calls a tool.

    Raises `RuntimeError` on a duplicate or invalid name, or on a tool whose
    operation the OpenAPI document lacks or whose body is not JSON: a malformed
    catalogue must fail loudly, never serve a half-built tool.
    """
    openapi = app.openapi()  # computed once per call; FastAPI caches it too
    components: dict[str, Any] = openapi.get("components", {}).get("schemas", {})
    catalog: dict[str, Tool] = {}
    for method, path, route in _http_routes(app):
        if _decide(method, path, route) != TOOL:
            continue
        where = f"{method} {path}"
        try:
            op = openapi["paths"][path][method.lower()]
        except KeyError:
            raise RuntimeError(  # i18n-exempt: build-time catalogue error, never shown to a person
                f"{where}: not in the OpenAPI document"
            ) from None
        schema, path_params, query_params, has_body = _schema_for(op, components, where)
        name = exclusions.NAME_OVERRIDES.get((method, path)) or derive_name(
            method,
            path,
            returns_collection=_returns_collection(op, components, path),
            answers_201=route.status_code == 201,
        )
        if not _NAME.fullmatch(name):
            raise RuntimeError(  # i18n-exempt: build-time catalogue error, never shown to a person
                f"{where}: invalid tool name {name!r}"
            )
        if name in catalog:
            other = catalog[name]
            raise RuntimeError(  # i18n-exempt: build-time catalogue error, never shown to a person
                f"tool name {name!r} is derived for both {other.method} {other.path} and "
                f"{where}; add a NAME_OVERRIDES entry"
            )
        catalog[name] = Tool(
            name=name,
            method=method,
            path=path,
            area=path.removeprefix(PREFIX).split("/")[0],
            permission=route_permission(route),
            path_params=path_params,
            query_params=query_params,
            has_body=has_body,
            input_schema=schema,
            annotations=_annotations(method, path),
            description=DESCRIPTIONS.get(where, ""),
            response_model=route.response_model,
        )
    return catalog
