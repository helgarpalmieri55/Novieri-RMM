"""`app/` NEVER IMPORTS FROM `tests/`. Enforced by AST, not grep.

This is D-1 from the platform-status registry fix: `app/api/v1/platform.py`
did `from tests.tool_surface import flatten, build_router_only_app` inside
`_module_registry()`, called from the request handler. `pyproject.toml`'s
`packages = ["app"]` and `deploy/backend.Dockerfile` (which copies only
`backend/app`, `backend/migrations` and `alembic.ini`) mean `tests/` DOES NOT
EXIST in the deployed image. That import raised `ModuleNotFoundError` at
request time in production while every test -- run from a checkout that HAS
`tests/` -- passed, because the whole suite runs from a tree where the import
resolves. A full green suite is not evidence against this defect; only a
sweep that knows the deployed layout is.

WHY AST AND NOT GREP. A regex over the text would miss an import split across
parens/lines, would flag the string "tests" inside a docstring or comment
(this file's own docstring, or the multi-line explanation in
`app/core/module_registry.py`, would trip a naive grep), and would not
distinguish `import tests` from `from app.testing import tests` or a local
variable named `tests`. The AST only looks at actual `Import`/`ImportFrom`
nodes and only at the imported module PATH, so it cannot be fooled by prose
and cannot miss a multi-line `from tests.x import (\n    y,\n)`.

MEASURED BEFORE WRITING: after the D-1 fix, zero import sites under `app/`
name `tests` or `tests.<anything>`. This test locks that in.
"""

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def _imported_test_modules(tree: ast.AST) -> list[str]:
    """Every module name imported by `tree` whose dotted path is `tests` or
    starts with `tests.` -- covers both `import tests...` and
    `from tests... import ...`, at any nesting (module top level, inside a
    function, inside a `TYPE_CHECKING` block); `ast.walk` does not care where
    an `Import`/`ImportFrom` node sits."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tests" or alias.name.startswith("tests."):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and (module == "tests" or module.startswith("tests.")):
                found.append(module)
    return found


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for module in _imported_test_modules(tree):
            found.append(f"{path.relative_to(APP.parent)}: imports `{module}`")
    return found


def test_app_never_imports_from_tests() -> None:
    problems = _violations()
    assert not problems, (
        "`app/` must never import from `tests/` -- `tests/` does not exist in "
        "the deployed image (pyproject.toml `packages = [\"app\"]`; "
        "deploy/backend.Dockerfile copies only app, migrations and "
        "alembic.ini), so such an import is a ModuleNotFoundError waiting for "
        "production, invisible to a suite that always runs from a checkout "
        "with tests/ present:\n  " + "\n  ".join(problems)
    )


def test_the_sweep_actually_finds_a_tests_import() -> None:
    """Without this, a broken matcher reports a perfectly clean `app/`."""
    source = "from tests.tool_surface import flatten\n"
    tree = ast.parse(source, filename="<fixture>")
    assert _imported_test_modules(tree) == ["tests.tool_surface"]

    source = "import tests.tool_surface as ts\n"
    tree = ast.parse(source, filename="<fixture>")
    assert _imported_test_modules(tree) == ["tests.tool_surface"]


def test_a_relative_import_named_tests_is_not_flagged() -> None:
    """`from . import tests` (a hypothetical local submodule literally named
    `tests` inside `app/`) is not the same defect -- it resolves inside the
    deployed package and ships with it. Only an ABSOLUTE import naming the
    top-level `tests` package -- the one `pyproject.toml` excludes from the
    image -- is the failure mode this guards."""
    source = "from . import tests\n"
    tree = ast.parse(source, filename="<fixture>")
    assert _imported_test_modules(tree) == []
