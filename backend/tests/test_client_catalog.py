"""Spec §5 tests 1, 2 and 11: the client-scope catalog and its guards."""

import ast
from pathlib import Path

from app.core.client_scope import ALL_SCOPE_ALLOWLIST
from app.core.client_scope_catalog import CATALOG, PENDING_PHASE_2
from app.models.base import Base

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
    assert "app/api/deps.py::admit" in found
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


# The functions that answer with a person's UN-NARROWED scope: X-Client-Id has
# not been applied to what they return. A door combines them with the
# request's own scope (`deps.admit`, `require_permission`, the portal's
# principal and gate, `read_door_identity`); anything else calling them could
# hand a route a scope wider than the one its request asked for.
UNNARROWED_SCOPE_READERS = frozenset({"person_scope", "clients_holding"})
UNNARROWED_SCOPE_DOORS = frozenset(
    {
        "app/api/deps.py",
        "app/modules/portal/principal.py",
        "app/core/identity/door.py",
        "app/core/rbac.py",
    }
)


def _unnarrowed_scope_uses(sources: list[tuple[str, ast.Module]]) -> list[str]:
    """`path:line:name` for every import of, reference to, or attribute access
    of an un-narrowed scope reader."""
    found: list[str] = []
    for path, tree in sources:
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            found.extend(
                f"{path}:{node.lineno}:{name}"
                for name in names
                if name in UNNARROWED_SCOPE_READERS
            )
    return found


def test_only_the_doors_read_an_unnarrowed_scope() -> None:
    outside = [
        use
        for use in _unnarrowed_scope_uses(_app_sources())
        if use.split(":")[0] not in UNNARROWED_SCOPE_DOORS
    ]
    assert not outside, (
        "person_scope / clients_holding return a scope X-Client-Id has not "
        "narrowed; only the doors may use them (intersect with the request's "
        f"own scope instead): {outside}"
    )


def test_the_unnarrowed_scope_guard_is_not_vacuous() -> None:
    files = {use.split(":")[0] for use in _unnarrowed_scope_uses(_app_sources())}
    assert {
        "app/api/deps.py",
        "app/modules/portal/principal.py",
        "app/core/identity/door.py",
    } <= files
    bad = ast.parse(
        "from app.core.rbac import clients_holding\n"
        "import app.core.identity.door as door\n"
        "x = door.person_scope\n"
    )
    assert _unnarrowed_scope_uses([("app/x.py", bad)]) == [
        "app/x.py:1:clients_holding",
        "app/x.py:3:person_scope",
    ]


def test_every_table_is_classified() -> None:
    live = {t.name for t in Base.metadata.sorted_tables}
    assert live - set(CATALOG) == set(), "unclassified tables (spec §5 test 1)"
    assert set(CATALOG) - live == set(), "catalog names tables that do not exist"


def test_pending_tables_are_in_the_catalog_and_not_global() -> None:
    assert PENDING_PHASE_2 <= set(CATALOG)
    assert all(CATALOG[t].kind in {"C", "H"} for t in PENDING_PHASE_2)


# H children of an O parent. Their client_id is NOT NULL, so a parent row with
# no client (Novieri's own credential) makes the insert fail -- which is the
# point: Annex A, "`credentials`, que tiene que tener cliente". Any other H
# table must hang from a C or H parent; a new exception needs a reason here.
H_OF_AN_OPTIONAL_PARENT = frozenset({"push_credential_scope", "push_credential_realm"})


def test_h_parents_are_scoped_tables() -> None:
    for table, entry in CATALOG.items():
        if entry.kind == "H":
            allowed = {"C", "H", "O"} if table in H_OF_AN_OPTIONAL_PARENT else {"C", "H"}
            assert entry.parent in CATALOG and CATALOG[entry.parent].kind in allowed, table


def test_the_optional_parent_exception_has_not_rotted() -> None:
    for table in H_OF_AN_OPTIONAL_PARENT:
        entry = CATALOG[table]
        assert entry.kind == "H" and entry.parent is not None, table
        assert CATALOG[entry.parent].kind == "O", f"{table}: exempt but its parent is not O"
