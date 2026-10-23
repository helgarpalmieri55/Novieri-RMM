"""Spec §5 tests 1, 2 and 11: the client-scope catalog and its guards."""

import ast
from pathlib import Path

from app.core.client_scope import ALL_SCOPE_ALLOWLIST

BACKEND = Path(__file__).resolve().parent.parent
DEFINITION = "app/core/client_scope.py"


def _app_sources() -> list[tuple[str, ast.Module]]:
    return [
        (str(path.relative_to(BACKEND)), ast.parse(path.read_text()))
        for path in sorted((BACKEND / "app").rglob("*.py"))
    ]


def _is_all_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "all"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ClientScope"
    )


def _calls_to_all(sources: list[tuple[str, ast.Module]]) -> set[str]:
    """`"<path>::<qualified name>"` of every function holding a
    `ClientScope.all()` call: the dotted names of the enclosing classes and
    functions, innermost last, or `"<module>"` at module level. The same key
    `ALL_SCOPE_ALLOWLIST` uses."""
    found: set[str] = set()

    def walk(node: ast.AST, path: str, chain: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = chain
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = (*chain, child.name)
            if _is_all_call(child):
                found.add(f"{path}::{'.'.join(chain) or '<module>'}")
            walk(child, path, inner)

    for path, tree in sources:
        walk(tree, path, ())
    return found


def _constructions(sources: list[tuple[str, ast.Module]]) -> list[str]:
    """Every place outside the definition that builds a scope by hand --
    `ClientScope(...)`, or any call passing `is_all=` (a `dataclasses.replace`
    that widens one) -- instead of through `.all()` / `.only()` / `.narrow()`."""
    found: list[str] = []
    for path, tree in sources:
        if path == DEFINITION:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None
            )
            if name == "ClientScope" or any(k.arg == "is_all" for k in node.keywords):
                found.append(f"{path}:{node.lineno}")
    return found


def test_client_scope_all_only_in_reviewed_functions() -> None:
    unreviewed = _calls_to_all(_app_sources()) - set(ALL_SCOPE_ALLOWLIST)
    assert not unreviewed, (
        "ClientScope.all() outside the reviewed list (spec §2.3). Pass the "
        f"person's scope, or add the function with a reason: {sorted(unreviewed)}"
    )


def test_the_allowlist_has_not_rotted() -> None:
    stale = set(ALL_SCOPE_ALLOWLIST) - _calls_to_all(_app_sources())
    assert not stale, (
        f"allowlisted functions that no longer call ClientScope.all(): {sorted(stale)}"
    )


def test_the_allowlist_guard_is_not_vacuous() -> None:
    found = _calls_to_all(_app_sources())
    assert "app/api/deps.py::auth_context" in found
    # Function granularity, not file: the route-reachable function next to
    # the scheduled poll is held to the caller's scope.
    assert "app/modules/email_intake/poller.py::poll_mailbox" in ALL_SCOPE_ALLOWLIST
    assert not any(k.endswith("poller.py::test_mailbox") for k in ALL_SCOPE_ALLOWLIST)


def test_no_route_file_holds_the_all_scope() -> None:
    """Spec §2.3: the HTTP route files (`api.py`, `api/v1/*`) may not use it.
    (Task 9's client creation and archiving will be the named exceptions.)"""
    routes = sorted(
        key
        for key in ALL_SCOPE_ALLOWLIST
        if key.startswith("app/api/v1/") or key.split("::")[0].endswith("/api.py")
    )
    assert routes == []


def test_nobody_builds_a_scope_by_hand() -> None:
    built = _constructions(_app_sources())
    assert not built, (
        "a ClientScope built outside app/core/client_scope.py escapes the "
        f"reviewed list; use .all() (reviewed), .only() or .narrow(): {built}"
    )


def test_the_construction_guard_is_not_vacuous() -> None:
    bad = ast.parse(
        "def f(s):\n"
        "    a = ClientScope(is_all=True, ids=frozenset())\n"
        "    b = replace(s, is_all=True)\n"
    )
    assert _constructions([("app/x.py", bad)]) == ["app/x.py:2", "app/x.py:3"]
