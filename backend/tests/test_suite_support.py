"""The test suite's own plumbing (design 2026-11-23-vps-y-marca-novieri §6).

Four things a run depended on without anything holding them:

  * `agent_update_support.unique_version` drew the minor from `% 100000`, so
    a run that seeds a few hundred releases -- they are global -- had a real
    chance of two tests minting the same version;
  * 43 files named Redis database 9 by a literal, and several `flushdb()` it:
    two suites at once (two worktrees, two jobs on one runner) emptied each
    other's. Every one now takes `tests.redis_support.TEST_REDIS_URL`, which
    is `ITS_TEST_REDIS_URL` when it is set;
  * pytest-asyncio warned on every run that its fixture loop scope was
    unset, and will change the default under the suite;
  * three routes named `HTTP_422_UNPROCESSABLE_ENTITY`, which Starlette
    deprecated for RFC 9110's `HTTP_422_UNPROCESSABLE_CONTENT` (0.48).
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.agent_update.jobtypes import _SEMVER
from tests.agent_update_support import unique_version

BACKEND = Path(__file__).resolve().parents[1]
TESTS = BACKEND / "tests"


def _docstring_ids(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def test_unique_versions_are_strict_semver_and_do_not_repeat_in_a_long_run() -> None:
    """20 000 draws: with `% 100000` about 2 000 of them collide (n^2/2N);
    with `% 10**12`, the chance of one collision is about 2 in 10 000."""
    drawn = [unique_version() for _ in range(20_000)]
    assert all(re.fullmatch(_SEMVER, version) for version in drawn)
    assert len(set(drawn)) == len(drawn)


def test_no_test_names_a_redis_database_of_its_own() -> None:
    offenders = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name in ("redis_support.py", "test_suite_support.py"):
            continue
        tree = ast.parse(path.read_text())
        skip = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                # The local server's port: a test that points at a dead
                # port on purpose (6399, `test_ai_admin_api.py`) is not one.
                and re.search(r"redis://[^\s/]+:6379/\d+", node.value)
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], "take tests.redis_support.TEST_REDIS_URL instead: " + ", ".join(
        offenders
    )


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"ITS_TEST_REDIS_URL": "redis://localhost:6379/7"}, "redis://localhost:6379/7"),
        ({}, "redis://localhost:6379/9"),
    ],
)
def test_the_suites_redis_is_the_environments(environment: dict[str, str], expected: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ITS_TEST_REDIS_URL"} | environment
    printed = subprocess.run(
        [sys.executable, "-c", "from tests.redis_support import TEST_REDIS_URL as u; print(u)"],
        cwd=BACKEND,
        env=env | {"PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert printed == expected


def test_the_fixture_loop_scope_is_set(pytestconfig: pytest.Config) -> None:
    """`function`: what every async fixture of this suite already gets (none
    asks for a wider loop), written down before pytest-asyncio's default
    moves under it."""
    assert pytestconfig.getini("asyncio_default_fixture_loop_scope") == "function"


def test_no_route_names_the_deprecated_422() -> None:
    found = [
        f"{path.relative_to(BACKEND)}:{node.lineno}"
        for path in sorted((BACKEND / "app").rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and node.attr == "HTTP_422_UNPROCESSABLE_ENTITY"
    ]
    assert found == [], found
