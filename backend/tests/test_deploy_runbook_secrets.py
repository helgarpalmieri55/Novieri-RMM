"""No runbook command puts a token value in argv.

**THE DEFECT THIS PINS.** `deploy/README.md`, `deploy/GO-LIVE.md` and
`deploy/scripts/issue-gateway-cert.py` all documented the same invocation:

    docker compose run --rm --no-deps \\
      -e BAO_TOKEN="$(grep '^OPENBAO_TOKEN=' .env | cut -d= -f2-)" \\
      ... /tmp/issue-gateway-cert.py itms.toeshee.com

The shell expands that before `docker` is executed, so the **root** OpenBao
token — the credential that can re-key `transit/job-signing`, the key
authorising every command sent to every endpoint in the fleet — becomes an
argument of `docker compose`. `/proc/<pid>/cmdline` is world-readable, which is
exactly why `ps` can show every argument of every process on a box and not one
environment variable. Any user on the host could read it for as long as the
container ran, and the operator's shell history kept a copy afterwards. In the
runbook of a script whose own docstring is about not leaking key material to
shell history.

**WHY A TEST AND NOT A REVIEW NOTE.** The three copies of that command drifted
into three files precisely because nothing tied them together, and prose that
says "we do not do this" is the kind of claim `CLAUDE.md` requires to be
measured rather than asserted. This is a runnable falsifier: a `sh`-fenced block
in an operator runbook that assigns a token value on a command line.

**WHAT IT DOES NOT CHECK, said plainly.** It cannot tell a secret from a
placeholder, it does not read `docker-compose.yml`'s own `environment:` maps
(those are env, not argv, and are how `openbao-init` is legitimately given
`OPENBAO_TOKEN`), and a token smuggled into a positional argument would pass.
It recognises the form that actually shipped, and it is deliberately narrow
enough that a red result always names a real edit.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUNBOOKS = (
    REPO / "deploy" / "README.md",
    REPO / "deploy" / "GO-LIVE.md",
    REPO / "deploy" / "scripts" / "issue-gateway-cert.py",
)

#: `-e NAME_TOKEN=value` / `--env NAME_TOKEN=value` on a command line.
#:
#: A BARE `-e BAO_TOKEN` (no `=`) is deliberately NOT matched: that is
#: pass-through from the caller's environment, which is the safe form and the
#: one this guard exists to leave available.
ARGV_TOKEN_ASSIGNMENT = re.compile(r"(?:^|\s)(?:-e|--env)[\s=]+[A-Za-z_][A-Za-z0-9_]*TOKEN=")

#: Fences whose contents an operator is expected to RUN. A quoted counter-example
#: belongs in a ```text block, where copying it is a deliberate act.
RUNNABLE_FENCES = ("sh", "bash", "shell", "console")

#: The one exemption, and it is EXPLICIT rather than heuristic. These runbooks
#: deliberately still SHOW the invocation that leaked, because an operator with
#: the old command in their notes needs to recognise it. A line carrying this
#: marker is a quotation of something removed, not an instruction.
#:
#: An exemption nobody counts is an exemption that spreads, so
#: `test_the_exemption_marker_is_used_exactly_where_expected` pins the count per
#: file with `==`.
REMOVED_MARKER = "[removed]"

EXPECTED_MARKED_LINES = {"README.md": 0, "GO-LIVE.md": 1, "issue-gateway-cert.py": 1}


def _runnable_lines(path: Path) -> list[tuple[int, str]]:
    """Every line inside a runnable fenced block, with its 1-based line number.

    Python files are scanned too: `issue-gateway-cert.py`'s module docstring IS
    the runbook for the command, and the leaking invocation lived there as much
    as in the markdown.
    """
    lines = path.read_text().splitlines()
    inside = False
    collected: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            language = stripped.removeprefix("```").strip().lower()
            if inside:
                inside = False
            else:
                inside = language in RUNNABLE_FENCES
            continue
        if inside:
            collected.append((number, line))
    return collected


def _all_lines(path: Path) -> list[tuple[int, str]]:
    return list(enumerate(path.read_text().splitlines(), start=1))


@pytest.mark.parametrize("path", RUNBOOKS, ids=lambda p: p.name)
def test_no_runbook_command_assigns_a_token_on_the_command_line(path: Path) -> None:
    """The whole file for `.py` (its docstring is the runbook and it has no
    fences to hide in), runnable fences only for markdown.

    ASSERTS THE OFFENDING LINES, not a boolean: `assert not offenders` would
    print `assert not [...]` and make you go find them. This prints
    `README.md:481  -e BAO_TOKEN="$(grep ...)"` — the file, the line and the
    text to delete.
    """
    scan = _all_lines(path) if path.suffix == ".py" else _runnable_lines(path)
    offenders = [
        f"{path.name}:{number}  {line.strip()}"
        for number, line in scan
        if ARGV_TOKEN_ASSIGNMENT.search(line) and REMOVED_MARKER not in line
    ]
    assert offenders == []


@pytest.mark.parametrize("path", RUNBOOKS, ids=lambda p: p.name)
def test_the_exemption_marker_is_used_exactly_where_expected(path: Path) -> None:
    """`==` on a COUNT, never `<=`.

    The marker exists so the runbooks can quote the invocation that leaked. It
    is also a way to silence this guard, so the number of lines carrying it is
    pinned per file: a new one has to be added here deliberately, in a diff that
    says what it is exempting and why.
    """
    marked = [
        f"{path.name}:{number}  {line.strip()}"
        for number, line in _all_lines(path)
        if REMOVED_MARKER in line
    ]
    assert len(marked) == EXPECTED_MARKED_LINES[path.name], marked


def test_the_guard_recognises_the_invocation_that_shipped() -> None:
    """**A GUARD THAT MATCHES NOTHING PASSES SILENTLY**, and this one is a
    regex over prose — the single most likely thing to quietly stop matching.

    So the exact line that shipped is asserted to be caught, and the two safe
    forms are asserted NOT to be: a bare `-e BAO_TOKEN` pass-through, and a
    stdin redirect. Without the negative half this would also pass as
    `re.compile(".")`.
    """
    shipped = '  -e BAO_TOKEN="$(grep \'^OPENBAO_TOKEN=\' .env | cut -d= -f2-)" \\'
    exec_form = "docker compose exec -e BAO_TOKEN=<root-token> openbao bao policy write its-app"
    passthrough = "docker compose run --rm -e BAO_TOKEN --entrypoint python gateway ..."
    from_stdin = "  --entrypoint python gateway /tmp/x.py host < /etc/it-suite/gateway-issuer.token"

    matched = [bool(ARGV_TOKEN_ASSIGNMENT.search(line)) for line in
               (shipped, exec_form, passthrough, from_stdin)]
    assert matched == [True, True, False, False]


def test_the_quoted_counter_example_is_not_in_a_runnable_fence() -> None:
    """`deploy/README.md` still SHOWS the bad line, on purpose — an operator who
    has the old command in their notes needs to recognise it.

    It sits in a ```text block, so the guard above does not see it and a reader
    copying a `sh` block cannot pick it up by accident. If somebody re-fences it
    as `sh` to make it look tidy, the guard goes red and this test explains why
    that is the right outcome.
    """
    readme = REPO / "deploy" / "README.md"
    body = readme.read_text()
    assert "-e BAO_TOKEN=\"$(grep '^OPENBAO_TOKEN=' .env | cut -d= -f2-)\"" in body
    assert not any(
        ARGV_TOKEN_ASSIGNMENT.search(line) for _number, line in _runnable_lines(readme)
    )
