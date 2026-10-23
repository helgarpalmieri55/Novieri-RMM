"""Spec §5 tests 1, 2 and 11: the client-scope catalog and its guards."""

import ast
from pathlib import Path

from app.core.client_scope import ALL_SCOPE_ALLOWLIST

BACKEND = Path(__file__).resolve().parent.parent


def _files_calling_all() -> set[str]:
    found: set[str] = set()
    for path in (BACKEND / "app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "all"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ClientScope"
            ):
                found.add(str(path.relative_to(BACKEND)))
    return found


def test_client_scope_all_only_in_reviewed_files() -> None:
    unreviewed = _files_calling_all() - set(ALL_SCOPE_ALLOWLIST)
    assert not unreviewed, (
        "ClientScope.all() outside the reviewed list (spec §2.3). Pass the "
        f"person's scope, or add the file with a reason: {sorted(unreviewed)}"
    )


def test_the_allowlist_has_not_rotted() -> None:
    stale = set(ALL_SCOPE_ALLOWLIST) - _files_calling_all() - {"app/core/client_scope.py"}
    assert not stale, f"allowlisted files that no longer call ClientScope.all(): {sorted(stale)}"


def test_the_allowlist_guard_is_not_vacuous() -> None:
    assert "app/api/deps.py" in _files_calling_all()
