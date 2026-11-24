"""NO OLD NAME IN WHAT A PERSON, OR A CLIENT'S COMPUTER, IS SHOWN (design
2026-11-23-vps-y-marca-novieri §1; operator decisions B4 and A3).

The product is "Novieri RMM" since 2026-11-23. This fixes the list of §1.1:
`ITMS` (the word), `Toeshee` in any case, `ITSuite`, `its-agent`,
`it-suite internal CA` and `itms.toeshee.com` (which the `toeshee` rule
covers) are refused in

  * `backend/app/`: every string literal a Python module holds -- not its
    docstrings, not its comments -- and every line of any other file (the
    installer copies the install command carries to a computer);
  * `agent/`: every string literal a Go file compiles in -- not its comments
    -- and every line of any other file (the packaging scripts, the unit,
    the plist, the Debian control file), tests and `testdata/` excepted;
  * `deploy/`: every line of every file, the Python ones by their literals.

THE SAME TECHNIQUE AS `test_no_vendor_names_backend.py`: source is parsed,
not grepped, where it can be -- so a comment that records history stays
true, and implicit concatenation and f-strings are read whole.

WHAT KEEPS ITS NAME (`KEPT`, design §1.2), removed from a line before it is
read: the `ITS_`/`ITMS_` environment keys, the `X-ITMS-*` headers and the
`ITMSRSP1` magic (the protocol with the console and the agent), the release
signature's domain string and the Go module path. NOT the notifications'
webhook header: the operator renamed it to X-Novieri-Event-Id on 2026-11-24,
while it had no consumer, so the old one coming back is an offence.

Lower-case `itms` is not read here. The lower-case names a client or a third
party sees were renamed by plan 2026-11-23's Tasks 1 and 8, and their own
tests pin the new ones (the install command's `novieri-agent:`, the snapshot
and restore names, the Entra certificate's CN, the test mail's tag). What is
left is identifiers, crypto domain strings and stored values
(`__Host-itms_session`, `itms-local:`, `itms-rsp.1`, `urn:itms:loopback`) and
the operator's own bucket locations and recovery tool, kept on purpose.

ONE DELIBERATE EXCEPTION (decision D17): the line of deploy/README.md that warns
the old host `itms.toeshee.com` not to pull this `main`.

DATED HISTORY IS ALLOWED in the files read line by line: a line the runbooks'
rule (`test_runbook_without_keycloak._dated`) reads as history -- own
authentication's phase 4 by name, or a date of 2026-11 or later, in the past
tense -- may name what the product was called.
"""

import ast
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_runbook_without_keycloak import _dated

REPO = Path(__file__).resolve().parents[2]

OLD = re.compile(r"\bITMS\b|(?i:toeshee)|ITSuite|its-agent|it-suite internal CA")
KEPT = re.compile(
    r"\bITS_[A-Z0-9_]+|\bITMS_[A-Z0-9_]+"
    r"|X-ITMS-[A-Za-z]+|\bITMSRSP1\b"
    r"|its-agent-release/v1"
    r"|github\.com/toeshee/it-suite/agent"
)


# The ONE line that names the old host on purpose: deploy/README.md warns that
# the old host must not pull this `main` (plan decision D17). Task 9 restored it
# after the plan's names script had inverted it, and the review confirmed that
# was right. Matched by its text, for that file only; anything else naming the
# host is still an offence.
WARNING_FILE = "deploy/README.md"
WARNING_LINE = "**Not on `itms.toeshee.com`, the host this VPS replaces.**"


def offending(text: str) -> bool:
    return OLD.search(KEPT.sub("", text)) is not None


# --------------------------------------------------------------------------- #
# The extractors
# --------------------------------------------------------------------------- #


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}


def _published(node: ast.AST) -> bool:
    """A docstring FastAPI/Pydantic publish in the OpenAPI schema: a route
    handler's (decorated `x.get/post/put/patch/delete/...` or `x.api_route`)
    and a model's (a base named `BaseModel`, or ending `Model` or `Schema`,
    which catches a project base model by name)."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for deco in node.decorator_list:
            call = deco.func if isinstance(deco, ast.Call) else deco
            if isinstance(call, ast.Attribute) and call.attr in HTTP_METHODS:
                return True
    if isinstance(node, ast.ClassDef):
        for base in node.bases:
            name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
            if name == "BaseModel" or name.endswith(("Model", "Schema")):
                return True
    return False


def python_strings(source: str) -> list[tuple[int, str]]:
    """`(line, text)` of every string constant but a docstring; an f-string
    is its literal parts, joined. The docstrings of route handlers and
    Pydantic models are kept: the OpenAPI schema publishes them."""
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str) and not _published(node):
                    docstrings.add(id(body[0].value))
    found: list[tuple[int, str]] = []
    joined: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = [v for v in node.values if isinstance(v, ast.Constant)]
            joined.update(id(v) for v in parts)
            found.append((node.lineno, "".join(str(v.value) for v in parts)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and id(node) not in joined
        ):
            found.append((node.lineno, node.value))
    return found


def go_strings(source: str) -> list[tuple[int, str]]:
    """`(line, text)` of every interpreted, raw and rune literal; comments
    are skipped. Escapes are left as written: `C:\\\\ITSuite` still reads
    `ITSuite`."""
    found: list[tuple[int, str]] = []
    i, line, n = 0, 1, len(source)
    while i < n:
        char = source[i]
        if char == "\n":
            line += 1
            i += 1
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end < 0 else end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += source.count("\n", i, end)
            i = end
        elif char == "`":
            end = source.find("`", i + 1)
            end = n if end < 0 else end
            found.append((line, source[i + 1 : end]))
            line += source.count("\n", i, end)
            i = end + 1
        elif char in "\"'":
            end = i + 1
            while end < n and source[end] not in (char, "\n"):
                end += 2 if source[end] == "\\" else 1
            found.append((line, source[i + 1 : end]))
            i = end + 1
        else:
            i += 1
    return found


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #


def _tracked(*roots: str) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--", *roots],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO / rel for rel in out.splitlines()]


def _in_scope(path: Path) -> bool:
    rel = path.relative_to(REPO).as_posix()
    if rel.startswith("agent/"):
        return not rel.endswith("_test.go") and "/testdata/" not in rel
    return rel.startswith(("backend/app/", "deploy/"))


def offenders_in(path: Path) -> Iterator[str]:
    rel = path.relative_to(REPO).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return
    if path.suffix == ".py":
        pairs = python_strings(source)
    elif path.suffix == ".go":
        pairs = go_strings(source)
    else:
        lines = source.splitlines()
        pairs = [
            (index + 1, line)
            for index, line in enumerate(lines)
            if offending(line)
            and not _dated(lines, index)
            and not (rel == WARNING_FILE and not offending(line.removeprefix(WARNING_LINE)))
        ]
    for line, text in pairs:
        if offending(text):
            yield f"{rel}:{line}: {text.strip()[:100]!r}"


def _files() -> list[Path]:
    return [p for p in _tracked("backend/app", "agent", "deploy") if _in_scope(p)]


# --------------------------------------------------------------------------- #
# The extractors are not vacuous, and stay inside their claim
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('X = "Install the ITMS agent"', "Install the ITMS agent"),
        ('X = ("[Toeshee] " "Ticket")', "[Toeshee] Ticket"),
        ('X = f"its-agent-{os}"', "its-agent-"),
        ('def f() -> str:\n    return "C:\\\\ITSuite"', "C:\\ITSuite"),
    ],
)
def test_the_python_extractor_reads_what_a_module_can_show(source: str, expected: str) -> None:
    assert expected in [text for _line, text in python_strings(source)]


def test_the_python_extractor_leaves_docstrings_and_comments() -> None:
    source = '"""The ITMS module."""\n# Toeshee wrote this\ndef f() -> None:\n    """ITSuite."""\n'
    assert python_strings(source) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('const a = "ITMS Agent"', "ITMS Agent"),
        ("const b = `ExecStart=/usr/local/bin/its-agent`", "ExecStart=/usr/local/bin/its-agent"),
        ('const c = "C:\\\\ITSuite\\\\agent"', "C:\\\\ITSuite\\\\agent"),
        ('const d = "say \\"Toeshee\\" twice"', 'say \\"Toeshee\\" twice'),
    ],
)
def test_the_go_extractor_reads_every_literal(source: str, expected: str) -> None:
    assert expected in [text for _line, text in go_strings(source)]


def test_the_go_extractor_leaves_comments_and_counts_lines() -> None:
    source = '// ITMS\n/* Toeshee\n */\nconst x = 1 // its-agent\nvar y = `a\nb`\nvar z = "ok"\n'
    assert go_strings(source) == [(5, "a\nb"), (7, "ok")]


@pytest.mark.parametrize(
    "kept",
    [
        "X-ITMS-Request: 1",
        "ITMS_BIND_IP=0.0.0.0",
        "ITS_AGENT_ENROLL_URL",
        "its-agent-release/v1\\n",
        '"github.com/toeshee/it-suite/agent/internal/jobs"',
        "magic ITMSRSP1",
        "__Host-itms_session",
        "itms-local:",
    ],
)
def test_what_section_1_2_keeps_is_not_an_offence(kept: str) -> None:
    assert not offending(kept)


@pytest.mark.parametrize(
    "old",
    [
        "Install the ITMS agent",
        "[Toeshee] Ticket",
        "%ProgramData%\\ITSuite",
        "/usr/local/bin/its-agent",
        "https://itms.toeshee.com",
        "com.toeshee.itsagent",
        "/opt/toeshee",
        "CN = it-suite internal CA",
        "X-ITMS-Request and then ITMS",
        '{"X-Toeshee-Event-Id": "1"}',
    ],
)
def test_every_old_name_of_section_1_1_is_an_offence(old: str) -> None:
    assert offending(old)


def test_a_planted_name_is_found_where_it_would_ship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk reads the file it is given, by its kind: a Go literal, a
    script's line, a Python constant."""
    monkeypatch.setattr(f"{__name__}.REPO", tmp_path)
    for rel, body in (
        ("agent/internal/x/x.go", 'package x\nconst N = "ITMS Agent"\n'),
        ("agent/packaging/linux/x.sh", "#!/bin/sh\necho its-agent\n"),
        ("backend/app/x.py", 'X = "Sign in to Toeshee"\n'),
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        assert list(offenders_in(path)), rel


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #


def test_the_walk_reaches_all_three_trees() -> None:
    files = [p.relative_to(REPO).as_posix() for p in _files()]
    for root, floor in (("backend/app/", 400), ("agent/", 150), ("deploy/", 30)):
        count = sum(1 for rel in files if rel.startswith(root))
        assert count > floor, f"only {count} files under {root}: the walk is broken"
    assert "agent/internal/install/mode.go" in files
    assert "backend/app/modules/notification/delivery.py" in files
    assert "deploy/docker-compose.yml" in files
    assert not [rel for rel in files if rel.endswith("_test.go") or "/testdata/" in rel]


def test_no_old_name_is_shown() -> None:
    found = [hit for path in _files() for hit in offenders_in(path)]
    assert found == [], "old names (design 2026-11-23 §1.1):\n" + "\n".join(found)


@pytest.mark.parametrize(
    "source",
    [
        "router = APIRouter()\n"
        '@router.get("/x")\n'
        "async def f() -> None:\n"
        '    """The ITMS rows."""\n',
        'app = object()\n@app.api_route("/x")\ndef f() -> None:\n    """Toeshee."""\n',
        'from pydantic import BaseModel\nclass M(BaseModel):\n    """The ITMS model."""\n',
        'class M(ProjectModel):\n    """The ITMS model."""\n',
    ],
)
def test_published_docstrings_are_read(source: str) -> None:
    assert any(offending(text) for _line, text in python_strings(source))


def test_plain_docstrings_stay_skipped() -> None:
    source = 'class C:\n    """ITMS."""\ndef g() -> None:\n    """ITMS."""\n'
    assert python_strings(source) == []


def test_the_d17_allowance_is_exactly_one_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{__name__}.REPO", tmp_path)

    def hits(rel: str, line: str) -> list[str]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# x\n\n{line}\n")
        return list(offenders_in(path))

    assert hits("deploy/README.md", WARNING_LINE + " Until that host is off.") == []
    assert hits("deploy/OTHER.md", WARNING_LINE + " Until that host is off.")
    assert hits("deploy/README.md", "Pull from `itms.toeshee.com` now.")
    assert hits("deploy/README.md", WARNING_LINE + " Ask the ITMS team.")
