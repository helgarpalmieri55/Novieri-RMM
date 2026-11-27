"""`deploy/ci/first_boot_answers.py` and the `deploy` job's first boot by the
operator's scripts (design 2026-11-27-despliegue-automatizado §5).

The live run is CI's. These hold what the driver may print and type -- each
secret masked before the line that carries it, the answers on the script's
stdin -- and that the job runs the scripts the way the runbook's operator
does, plus the two checks the design adds: OpenBao unsealing itself after a
restart, and first-boot.sh run again changing nothing.
"""

import ast
import importlib.util
import re
import shlex
import shutil
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DRIVER = REPO / "deploy/ci/first_boot_answers.py"
CI = REPO / ".github/workflows/ci.yml"
BASH = shutil.which("bash") or "/bin/bash"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_ci_first_boot_answers", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


answers = _load()

#: A stand-in first-boot.sh: prints five shares and a root token as the real
#: one does, then reads SAVED and three shares, which must be the first,
#: third and fifth, and says so without printing them.
FAKE = r"""#!/bin/bash
set -euo pipefail
[ "${ITS_FIRST_BOOT_ALLOW_NONTTY:-}" = 1 ] || { echo "not allowed"; exit 9; }
shares=(S1-aaaa S2-bbbb S3-cccc S4-dddd S5-eeee)
for i in 0 1 2 3 4; do printf '  share %d     %s\n' "$((i + 1))" "${shares[$i]}"; done
printf '  root token  %s\n' s.ROOT-zzzz
read -r answer; [ "$answer" = SAVED ] || { echo "no SAVED"; exit 2; }
for want in S1-aaaa S3-cccc S5-eeee; do
  read -r got; [ "$got" = "$want" ] || { echo "wrong share"; exit 3; }
done
echo "got the three shares"
echo "args: $*"
printf '\n  install code  %s\n\n' CODE-1234
echo "First boot: every step is done."
exit "${FAKE_EXIT:-0}"
"""


def _fake(tmp_path: Path, body: str = FAKE) -> Path:
    script = tmp_path / "first-boot.sh"
    script.write_text(body)
    script.chmod(0o755)
    return script


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = yaml.safe_load(CI.read_text())["jobs"]["deploy"]["steps"]
    return steps


def _run(name: str) -> str:
    for step in _steps():
        if str(step.get("name", "")).startswith(name):
            lines = step["run"].splitlines()
            return "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    raise AssertionError(f"the deploy job has no step named {name!r}...")


def test_it_imports_only_the_standard_library() -> None:
    """CI runs it with the runner's bare `python3`."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(DRIVER.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"__future__"}, names


def test_every_secret_is_masked_before_its_line_and_the_answers_are_typed(
    tmp_path: Path, capsys: Any
) -> None:
    rc = answers.main(["--script", str(_fake(tmp_path)), "--require", "got the three shares"])
    out = capsys.readouterr().out.splitlines()
    assert rc == 0, out
    secrets = ("S1-aaaa", "S2-bbbb", "S3-cccc", "S4-dddd", "S5-eeee", "s.ROOT-zzzz", "CODE-1234")
    for secret in secrets:
        mask = out.index(f"::add-mask::{secret}")
        shown = next(i for i, line in enumerate(out) if secret in line and "::" not in line)
        assert mask == shown - 1, secret
    assert "got the three shares" in out


def test_its_own_arguments_reach_the_script_after_a_double_dash(
    tmp_path: Path, capsys: Any
) -> None:
    rc = answers.main(["--script", str(_fake(tmp_path)), "--", "--tls-cafile", "/r.pem"])
    assert rc == 0
    assert "args: --tls-cafile /r.pem" in capsys.readouterr().out


def test_a_line_that_never_came_fails_the_run(tmp_path: Path, capsys: Any) -> None:
    rc = answers.main(["--script", str(_fake(tmp_path)), "--require", "something else"])
    assert rc == 1
    assert "FAIL  first-boot.sh never printed: something else" in capsys.readouterr().out


def test_the_scripts_own_failure_is_the_runs(tmp_path: Path) -> None:
    script = _fake(tmp_path, FAKE.replace('exit "${FAKE_EXIT:-0}"', "exit 4"))
    assert answers.main(["--script", str(script)]) == 4


def test_four_shares_and_a_root_token_fail_the_run(tmp_path: Path, capsys: Any) -> None:
    body = FAKE.replace("for i in 0 1 2 3 4;", "for i in 0 1 2 3;")
    assert answers.main(["--script", str(_fake(tmp_path, body))]) == 1
    assert "printed 4 shares with its root token" in capsys.readouterr().out


def test_a_script_that_hangs_is_stopped(tmp_path: Path, capsys: Any) -> None:
    body = f"#!{BASH}\nread -r never\n"
    assert answers.main(["--script", str(_fake(tmp_path, body)), "--timeout", "1"]) == 1
    assert "still running after 1 s" in capsys.readouterr().out


def test_the_answers_are_saved_and_shares_one_three_and_five() -> None:
    shares = {n: f"share-{n}" for n in range(1, 6)}
    assert answers.answers(shares) == "SAVED\nshare-1\nshare-3\nshare-5\n"


# --------------------------------------------------------------------------- #
# Beyond the brief (coordinator, 2026-11-27): a FAILED report's destructive
# ways on sit behind typed words, and the driver answers none of them; the
# raw `bao operator init` output a failed step 3.2 prints is masked too; and a
# hang in one of the script's children is stopped with it.
# --------------------------------------------------------------------------- #

FIRST_BOOT = REPO / "deploy/scripts/first-boot.sh"


def _guards_the_script_can_print() -> list[tuple[str, str]]:
    """(WORD, line) for every guard first-boot.sh writes into a report, built
    as its `guard` function prints it, and its one prompt for a word."""
    source = FIRST_BOOT.read_text()
    shape = next(line for line in source.splitlines() if line.startswith("guard() {"))
    assert "Type %s to go on: " in shape, shape
    words = sorted(set(re.findall(r'\$\(guard ([A-Z]+) "', source)))
    assert words == ["DELETE", "DESTROY", "SAVED", "UNDO"], words
    head = "        read -r -p 'This does it. Type {0} to go on: ' answer && [ \"$answer\" = {0} ]"
    lines = [(w, head.format(w)) for w in words]
    seal = next(line for line in source.splitlines() if "type SEAL to seal it now" in line)
    lines.append(("SEAL", seal[seal.index('"') + 1 : seal.rindex('"')]))
    return lines


@pytest.mark.parametrize("word,line", _guards_the_script_can_print(), ids=lambda v: v[:12])
def test_a_guard_is_never_answered_and_fails_the_run(
    tmp_path: Path, capsys: Any, word: str, line: str
) -> None:
    """A report that reaches the driver with a word to type is a run that went
    wrong: the driver closes the script's stdin, types nothing, and fails."""
    body = (
        f"#!{BASH}\nprintf '%s\\n' 'FAILED  something' {shlex.quote(line)} >&2\n"
        'read -r got || got="<eof>"\necho "typed: $got"\nexit 0\n'
    )
    rc = answers.main(["--script", str(_fake(tmp_path, body)), "--timeout", "20"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "typed: <eof>" in out, "the script was typed something, or never got EOF"
    assert f"FAIL  first-boot.sh asked for a typed {word}" in out
    assert "still running" not in out


def test_a_guard_after_the_answers_fails_even_a_run_that_exits_0(
    tmp_path: Path, capsys: Any
) -> None:
    body = FAKE.replace(
        'echo "First boot: every step is done."',
        "echo \"  read -r -p 'This DESTROYS it. Type DESTROY to go on: ' answer\"\n"
        'echo "First boot: every step is done."',
    )
    rc = answers.main(["--script", str(_fake(tmp_path, body))])
    assert rc == 1
    assert "FAIL  first-boot.sh asked for a typed DESTROY" in capsys.readouterr().out


def test_bao_operator_inits_own_output_is_masked_before_its_line(
    tmp_path: Path, capsys: Any
) -> None:
    """Step 3.2 prints `bao operator init` as it came when it cannot read five
    shares and a root token out of it: every key in it is masked first."""
    body = (
        f"#!{BASH}\nprintf 'Unseal Key 1: %s\\n' K1-aaaa\nprintf 'Unseal Key 2: %s\\n' K2-bbbb\n"
        "printf '\\nInitial Root Token: %s\\n' s.R00T-yyyy\nexit 1\n"
    )
    assert answers.main(["--script", str(_fake(tmp_path, body))]) == 1
    out = capsys.readouterr().out.splitlines()
    for secret in ("K1-aaaa", "K2-bbbb", "s.R00T-yyyy"):
        shown = next(i for i, line in enumerate(out) if secret in line and "::" not in line)
        assert out[shown - 1] == f"::add-mask::{secret}", secret


def test_a_hang_in_a_child_of_the_script_is_stopped_with_it(tmp_path: Path, capsys: Any) -> None:
    """The script's children hold its output open: a timeout that killed the
    script alone would wait for them (a `docker compose up --wait`, say)."""
    body = f"#!{BASH}\nsleep 600\necho never\n"
    started = time.monotonic()
    rc = answers.main(["--script", str(_fake(tmp_path, body)), "--timeout", "1"])
    assert rc == 1
    assert time.monotonic() - started < 30
    assert "still running after 1 s" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The deploy job
# --------------------------------------------------------------------------- #


def test_the_job_runs_the_first_boot_before_anything_else_touches_the_stack() -> None:
    names = [str(step.get("name", step.get("uses", ""))) for step in _steps()]
    order = [
        names.index("shellcheck the first-boot scripts"),
        names.index("Throwaway .env, written by the script a new host uses"),
        names.index("Pebble, and the root it signs this run's certificate with"),
        names.index("first boot -- scripts/first-boot.sh, answered as an operator would"),
        names.index("OpenBao unseals itself after a restart"),
        names.index("first-boot.sh again -- every step done, nothing changed"),
        names.index("grant-app-privileges.sql (twice — must be idempotent)"),
        next(i for i, n in enumerate(names) if n.startswith("the platform's own OAuth server")),
        names.index("backup.sh -> restore.sh round trip, invoked AS DOCUMENTED"),
        names.index("teardown"),
    ]
    assert order == sorted(order), order
    gone = ("postgres — first boot (init-app-user.sh)", "alembic upgrade head")
    assert not set(gone) & set(names), "the first boot is the scripts', not the job's own lines"


def test_the_scripts_are_shellchecked() -> None:
    run = _run("shellcheck the first-boot scripts")
    assert (
        "shellcheck deploy/scripts/prepare-host.sh deploy/scripts/first-boot.sh "
        "deploy/scripts/first-boot-check.sh"
    ) in run


def test_the_ci_knobs_are_the_ones_the_scripts_read() -> None:
    run = _run("Throwaway .env")
    for line in (
        'echo "ITS_PREFLIGHT_SKIP_DNS=1" >> "$GITHUB_ENV"',
        'echo "ITS_NO_SYSTEMCTL=1" >> "$GITHUB_ENV"',
        'echo "ITS_SYSTEMD_DIR=$RUNNER_TEMP/systemd" >> "$GITHUB_ENV"',
    ):
        assert line in run, line
    whole = "\n".join(str(step.get("run", "")) for step in _steps())
    assert "ITS_FIRST_BOOT_ALLOW_NONTTY" not in whole, "the driver sets it, for its one run"
    assert '"ITS_FIRST_BOOT_ALLOW_NONTTY": "1"' in DRIVER.read_text()


def test_the_first_boot_is_answered_and_must_end_as_a_good_one_does() -> None:
    run = _run("first boot -- scripts/first-boot.sh")
    assert "python3 ci/first_boot_answers.py" in run
    assert "--require 'First boot: every step is done.'" in run
    # Ruling Q1 (2026-11-27): the job wrote .env with new-env.sh, as step 1
    # does, so that Pebble could start first; the script must say it skipped it.
    assert (
        "--require '  skip    step 1: .env exists (new-env.sh never overwrites one), "
        "for rmm.novieri.com'"
    ) in run
    assert '-- --tls-cafile "$PEBBLE_ROOT"' in run
    assert "tee" not in run, "the shares go to the masked log, and to no file"
    assert run.index("python3 ci/first_boot_answers.py") < run.index(
        'echo "COMPOSE_FILE=docker-compose.yml:ci/acme-pebble.yml:openbao/auto-unseal.yml" >> '
        '"$GITHUB_ENV"'
    )


def test_openbao_is_restarted_and_must_come_back_unsealed_alone() -> None:
    run = _run("OpenBao unseals itself after a restart")
    assert run.index("docker compose restart openbao") < run.index("bao status -format=json")
    assert "operator unseal" not in run and "sys/unseal" not in run, "no share is given"
    assert "grep -q '\"type\": \"static\"' <<<\"$state\"" in run
    assert "grep -q '\"sealed\": false' <<<\"$state\"" in run


def test_the_second_run_must_change_nothing_and_run_nothing() -> None:
    run = _run("first-boot.sh again")
    for part in (
        "sha256sum .env",
        "sudo sha256sum /etc/it-suite/gateway-issuer.token "
        "/etc/it-suite/openbao-unseal/unseal.key",
        # A one-shot runs again on every `up -d`, under the same id (measured
        # 2026-11-27): its exit code is what must hold, a service's start time.
        "xargs docker inspect -f '{{.Name}} {{.Id}} {{if .State.Running}}{{.State.StartedAt}}"
        "{{else}}exit={{.State.ExitCode}}{{end}}'",
        "select version_num from alembic_version",
        './scripts/first-boot.sh --tls-cafile "$PEBBLE_ROOT" </dev/null',
        '[ "$before" = "$after" ]',
        "grep -q '^  run ' \"$RUNNER_TEMP/again.log\"",
        "[ \"$(grep -c '^  skip ' \"$RUNNER_TEMP/again.log\")\" -ge 16 ]",
    ):
        assert part in run, part
    assert "first_boot_answers" not in run, "no terminal and no answers: it must ask nothing"


def test_the_teardown_removes_what_the_scripts_wrote_as_root() -> None:
    run = _run("teardown")
    assert "docker compose down -v" in run
    written = (
        "/etc/it-suite/gateway-issuer.token",
        "/etc/it-suite/openbao-unseal",
        "/var/backups/it-suite",
        "/var/lib/it-suite",
        '"$RUNNER_TEMP/systemd"',
    )
    for path in written:
        assert path in run, path
