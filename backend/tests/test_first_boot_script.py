"""`deploy/scripts/first-boot.sh`, on a stand-in host (design
2026-11-27-despliegue-automatizado §3, §5; `tests/first_boot_support.py`).

The CI's `deploy` job runs the script end to end against a real stack and
Pebble. What only a stand-in can show is pinned here: the refusals -- no
terminal, a mixed state, xtrace -- that a happy run never reaches; that a run
again skips every step, saying so; and that no secret is ever an argument of
any process (every command but bash's builtins goes through a logging shim),
in the environment of one, or in a file the runbook does not name, kept or
temporary.

Since the fix round of 2026-11-27 (review findings I1 to I3 and M1 to M10),
also: every token found on the host is proved against this OpenBao before it
counts as done; shares typed on a re-run are proved before openbao-seal.sh
gets them, and a seal migration left half-way is named, with the way out;
the hidden prompts and the clear are checked on a real pseudo-terminal; and
every FAILED block (checked by `Host.run` on every run) is one block whose
command lines can be pasted.

Task 3 (plan 2026-11-27) adds steps 4 to 8 and their failures, each one in
SCENARIOS and so pasted whole; and, from Task 2's last review, a paste test
that proves the paste reached the shell and ends at a fixed point (r1, r2),
five more reports pasted (r3), the custody report's clear behind SAVED (r4),
and no token blamed by an OpenBao that does not answer (r5).
"""

import os
import re
import shlex
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.first_boot_support import (
    DESTRUCTIVE_ACT,
    PASTE_STAND_INS,
    SCRIPT,
    Host,
    _segments,
    failed_block_problems,
    lines,
    paste_into_a_shell,
)

ALLOW = {"ITS_FIRST_BOOT_ALLOW_NONTTY": "1"}
RESTART_SKIP = "the restart that proves it"
SAVED_PROMPT = "Type SAVED once the five shares and the root token are stored off this VPS: "
ROOT_PROMPT = "OpenBao root token, printed by step 3.2 (hidden): "
CLEAR = "\x1b[H\x1b[2J\x1b[3J"
SEAL_PROMPT = (
    "OpenBao is sealed next, and only three good shares unseal it: type SEAL to seal it now "
    "and unseal it with these three (anything else stops here, and nothing changes): "
)


def _saved_and_three(host: Host, picks: tuple[int, ...] = (1, 3, 4)) -> str:
    shares = host.secrets["shares"]
    return "SAVED\n" + "".join(shares[i] + "\n" for i in picks)


def _share_prompt(n: int) -> str:
    return f"Unseal key share {n} of 3 (hidden): "


def _after_init(host: Host, **bao: bool) -> None:
    """A host whose earlier run stopped right after `bao operator init`."""
    host.write_env(IT_SUITE_VERSION=host.tag)
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    host.state["bao"].update(init=True, **bao)


def _done(host: Host) -> None:
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr


def _code() -> str:
    """The script without its comment lines and without the bodies of its
    here-documents (a unit file's `WantedBy=` is not an assignment)."""
    kept, end = [], None
    for line in SCRIPT.read_text().splitlines():
        if end is not None:
            end = None if line.strip() == end else end
            continue
        if line.lstrip().startswith("#"):
            continue
        kept.append(line)
        opened = re.search(r"<<-?\s*'([^']+)'", line)
        end = opened.group(1) if opened else None
    return "\n".join(kept)


def _guarded_lines(stderr: str, word: str) -> list[str]:
    """The command lines behind a typed WORD (review I1, round 3)."""
    return [
        line for line in stderr.splitlines()
        if line.startswith("        read -r -p '") and f'[ "$answer" = {word} ] && ' in line
        and f"Type {word} to go on: ' answer && " in line
    ]  # fmt: skip


def _recipe_removes_the_earlier_boots_files(host: Host, stderr: str) -> None:
    token, key = host.etc / "gateway-issuer.token", host.etc / "openbao-unseal" / "unseal.key"
    (line,) = _guarded_lines(stderr, "DESTROY")
    assert line.endswith(f" && sudo rm -f {token} {key} && ./scripts/first-boot.sh"), line
    assert "openbao-seal.sh would reuse it" in stderr


def _ways(stderr: str) -> list[list[str]]:
    """The FAILED block's lines, split at its blank lines."""
    block = stderr[stderr.index("FAILED  ") :].splitlines()[1:]
    ways: list[list[str]] = [[]]
    for line in block:
        if line.strip():
            ways[-1].append(line)
        else:
            ways.append([])
    return [w for w in ways if w]


def test_the_script_parses() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


@pytest.mark.parametrize("how", ["bash -x", "SHELLOPTS=xtrace"])
def test_it_refuses_to_run_under_xtrace(tmp_path: Path, how: str) -> None:
    """With the environment of a real run (review I3a: without it the fakes
    die on their own and the test cannot see the guard fail to exit)."""
    host = Host(tmp_path)
    if how == "bash -x":
        done = host.run(env=ALLOW, bash_options=("-x",))
    else:
        done = host.run(env={**ALLOW, "SHELLOPTS": "xtrace"})
    assert done.returncode == 1
    assert "refusing to run under xtrace" in done.stderr
    assert host.argv() == "", "a command ran under xtrace"
    assert not (host.deploy / ".env").exists(), "new-env.sh ran under xtrace"
    said = done.stderr.splitlines()
    refusal = "first-boot.sh: refusing to run under xtrace (bash -x): it would print every secret"
    after = said[said.index(refusal) + 1 :]
    assert [line for line in after if line.strip()] in ([], ["+ exit 1"]), after


def test_nothing_in_it_traces_or_spills_a_here_string() -> None:
    """`set -x` would print every secret; a here-string or an unquoted
    here-document can land in a temporary file (bash before 5.1 always, after
    it above the pipe's size). The script uses neither."""
    text = SCRIPT.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"set -[a-wyz]*x|set -o xtrace|BASH_XTRACEFD", code)
    assert "<<<" not in code
    for heredoc in re.findall(r"<<-?\s*(\S+)", code):
        assert heredoc.startswith("'"), heredoc


def test_a_new_host_runs_every_step_to_the_auto_unseal_and_asks_only_what_it_must(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    ran = lines(done.stdout, "run")
    # M10: the run line shows the command it runs, --wait-timeout included.
    assert [r.split(":")[0].split(",")[0] for r in ran][:5] == [
        "./scripts/new-env.sh --host rmm.novieri.com",
        "./scripts/build-images.sh",
        "docker compose up -d --wait --wait-timeout 120 postgres",
        "docker compose up -d openbao",
        "bao operator init -key-shares=5 -key-threshold=3",
    ]
    assert "docker compose up openbao-init" in ran
    shares = host.secrets["shares"]
    for n, share in enumerate(shares, 1):
        assert f"  share {n}     {share}" in done.stdout
    assert f"  root token  {host.secrets['root']}" in done.stdout
    env = host.env()
    assert env["ITS_OPENBAO_TOKEN"] == host.secrets["its-app"]
    assert env["OPENBAO_TOKEN"] == host.secrets["root"]
    assert env["IT_SUITE_VERSION"] == host.tag
    assert "change-me" not in (host.deploy / ".env").read_text()
    token = host.etc / "gateway-issuer.token"
    assert token.read_text() == host.secrets["gateway-issuer"] + "\n"
    assert oct(token.stat().st_mode & 0o777) == "0o600"
    assert host.state["bao"]["sealed"] is False
    assert host.state["bao"]["policies"] == ["its-app", "gateway-issuer"]
    # Right after 3.6 (coordinator's ruling Q6): the auto-unseal, with the three
    # shares of step 3.3 on openbao-seal.sh's stdin, and the restart that proves it.
    assert ran.index("docker compose up openbao-init") < ran.index("docker compose restart openbao")
    assert any(r.startswith("sudo ./scripts/openbao-seal.sh enable --key-file ") for r in ran)
    assert host.state["seal_stdin"] == 3
    assert host.state["bao"]["type"] == "static"
    assert env["COMPOSE_FILE"] == "docker-compose.yml:openbao/auto-unseal.yml"
    assert env["OPENBAO_UNSEAL_KEY_FILE"] == str(host.etc / "openbao-unseal" / "unseal.key")
    assert "ok      OpenBao restarted and came back unsealed by itself" in done.stdout
    # The shares this run printed and proved are not proved twice (review I2).
    assert "bao operator seal" not in " ".join(ran)


def test_a_second_run_skips_every_step_to_the_auto_unseal(tmp_path: Path) -> None:
    host = Host(tmp_path)
    assert host.run(stdin=_saved_and_three(host), env=ALLOW).returncode == 0
    again = host.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert lines(again.stdout, "run") == []
    skipped = [s.split(":")[0] for s in lines(again.stdout, "skip")]
    # The first ten: the steps Task 3 adds after them skip too, in their turn.
    assert skipped[:10] == [
        "step 1", "step 1b", "step 2", "step 3.1", "step 3.2", "step 3.3", "step 3.4",
        "step 3.6", "auto-unseal", RESTART_SKIP,
    ]  # fmt: skip
    for changing in ("compose restart", "openbao-seal.sh", "operator init", "tee "):
        assert changing not in host.argv(), changing
    # M7: it says what it knows -- this run did not restart OpenBao -- and no more.
    assert (
        "  skip    the restart that proves it: this run did not turn the auto-unseal on, "
        "so it does not restart OpenBao"
    ) in again.stdout.splitlines()


def test_a_second_run_proves_each_token_it_finds_on_stdin(tmp_path: Path) -> None:
    """Review I1: a token found is not a token known. Each one -- the root
    token and the its-app token in .env, the gateway-issuer token in its file
    -- is looked up in this OpenBao, reaching `bao` on stdin."""
    host = Host(tmp_path)
    _done(host)
    again = host.run()
    assert again.returncode == 0, again.stdout + again.stderr
    argv = host.argv()
    assert argv.count("bao token lookup -format=json") >= 3
    assert f"sudo cat {host.etc}/gateway-issuer.token" in argv.splitlines()
    assert host.leaks() == []


def test_no_secret_is_an_argument_or_in_a_file_the_runbook_does_not_name(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    argv = host.argv()
    assert argv.count("\n") > 50, "the shims logged nothing: the check is blind"
    for value in host.all_secrets():
        assert value not in argv
    allowed = {
        host.secrets["root"]: ["repo/deploy/.env"],
        host.secrets["its-app"]: ["repo/deploy/.env"],
        host.secrets["gateway-issuer"]: ["etc/gateway-issuer.token"],
    }
    for value in host.all_secrets():
        assert host.files_holding(value) == allowed.get(value, []), value
    # Review M3 and M9: no child's environment, and no file on the way.
    assert host.environ_log().count("== ") > 50, "no child wrote its environment"
    tee = [(where, data) for kind, where, data in host.captures() if kind == "tee"]
    token = ("etc/gateway-issuer.token", host.secrets["gateway-issuer"] + "\n")
    assert token in tee, "the capturing shims saw nothing: the check is blind"
    assert host.leaks() == []
    known = {str(host.deploy / ".env"), str(host.etc / "openbao-unseal" / "unseal.key")}
    assert {path for _, path in host.writes()} <= known, host.writes()


@pytest.mark.parametrize("how", ["mktemp, then rm", "tee, then rm", "cat >, then rm"])
def test_the_secret_scan_sees_a_temporary_file_gone_by_the_end(tmp_path: Path, how: str) -> None:
    """The harness itself (review M9): a file written and removed during the
    run leaves nothing for an end-of-run scan, and is seen all the same."""
    host = Host(tmp_path)
    write = {
        "mktemp, then rm": 'f="$(mktemp)"; printf \'%s\\n\' "$s" > "$f"; rm -f "$f"',
        "tee, then rm": 'printf \'%s\\n\' "$s" | tee "$TMPDIR/t" >/dev/null; rm "$TMPDIR/t"',
        "cat >, then rm": 'printf \'%s\\n\' "$s" | cat > "$TMPDIR/c"; rm -f "$TMPDIR/c"',
    }[how]
    probe = host.deploy / "scripts" / "first-boot.sh"
    probe.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\nIFS= read -r s\n{write}\n")
    done = host.run(stdin=host.secrets["shares"][0] + "\n")
    assert done.returncode == 0, done.stderr
    assert host.files_holding(host.secrets["shares"][0]) == [], "the file was still there"
    assert any(leak.startswith("share 1: ") for leak in host.leaks()), host.captures()


def test_the_shares_are_printed_to_a_terminal_only(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(stdin="")
    assert done.returncode == 1
    assert "the output is not a terminal" in done.stderr
    assert "operator init" not in host.argv()
    assert host.state["bao"]["init"] is False


def test_with_its_input_not_a_terminal_it_refuses_before_init(tmp_path: Path) -> None:
    """Review M8: on a terminal with SAVED piped in, nobody need have seen the
    shares before the screen is cleared."""
    host = Host(tmp_path)
    rc, shown = host.run_pty(answers=[], stdin_tty=False, stdin=_saved_and_three(host))
    assert rc == 1, shown
    assert "the input is not a terminal" in shown
    assert "operator init" not in host.argv()
    assert host.state["bao"]["init"] is False
    assert host.secrets["shares"][0] not in shown


def test_under_a_terminal_the_shares_show_once_before_the_clear_and_no_share_echoes(
    tmp_path: Path,
) -> None:
    """Review I3b and I3c, on a real pseudo-terminal: SAVED, typed at a prompt
    that is not hidden, is echoed back -- so the terminal does echo, and a
    share that does not appear was not echoed."""
    host = Host(tmp_path)
    shares = host.secrets["shares"]
    answers = [(SAVED_PROMPT, "SAVED")]
    answers += [(_share_prompt(n), shares[i]) for n, i in ((1, 0), (2, 2), (3, 4))]
    rc, shown = host.run_pty(answers=answers)
    assert rc == 0, shown
    assert CLEAR in shown, "the screen and its scrollback were not cleared"
    before, after = shown.split(CLEAR, 1)
    for share in shares:
        assert share in before
        assert share not in after, "a share is on the screen after the clear"
    assert host.secrets["root"] in before and host.secrets["root"] not in after
    assert SAVED_PROMPT + "SAVED" in before, "the terminal echoes nothing: the check is blind"
    assert host.state["bao"]["type"] == "static"
    assert host.leaks() == []


def test_under_a_terminal_a_rerun_echoes_neither_the_root_token_nor_a_share(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    _after_init(host)
    shares = host.secrets["shares"]
    answers = [(_share_prompt(n), shares[i]) for n, i in ((1, 4), (2, 1), (3, 0))]
    answers.append((ROOT_PROMPT, host.secrets["root"]))
    rc, shown = host.run_pty(answers=answers)
    assert rc == 0, shown
    assert ROOT_PROMPT in shown and _share_prompt(3) in shown
    for typed in (shares[4], shares[1], shares[0], host.secrets["root"]):
        assert typed not in shown, "a typed secret was echoed"
    assert host.leaks() == []


def test_a_typed_share_must_be_one_of_the_five_and_not_typed_twice(tmp_path: Path) -> None:
    host = Host(tmp_path)
    shares = host.secrets["shares"]
    typed = ("not-a-share", shares[0], shares[0], shares[2], shares[3])
    stdin = "SAVED\n" + "".join(x + "\n" for x in typed)
    done = host.run(stdin=stdin, env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "that is not one of the five shares printed above" in done.stdout
    assert "that share was typed already" in done.stdout
    assert host.state["bao"]["sealed"] is False


def test_saved_is_asked_until_it_is_typed(tmp_path: Path) -> None:
    host = Host(tmp_path)
    shares = host.secrets["shares"]
    done = host.run(stdin="yes\nsaved\nSAVED\n" + "".join(s + "\n" for s in shares[:3]), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count("type SAVED, in capitals, to go on") == 2


def test_volumes_without_an_env_are_a_mixed_state(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.state["volumes"] = ["it-suite_openbao-data", "it-suite_postgres-data"]
    done = host.run(env=ALLOW)
    assert done.returncode == 1
    assert "mixed state" in done.stderr and "it-suite_openbao-data" in done.stderr
    assert "docker volume ls -q --filter label=com.docker.compose.project=it-suite" in done.stderr
    assert not (host.deploy / ".env").exists()
    assert "new-env.sh" not in host.argv() and "compose up" not in host.argv()
    _recipe_removes_the_earlier_boots_files(host, done.stderr)


def test_a_docker_that_does_not_answer_is_not_a_new_host(tmp_path: Path) -> None:
    """Review M1: `docker volume ls` failing is not "no volume"."""
    host = Host(tmp_path)
    error = "permission denied while trying to connect to the Docker daemon socket"
    host.state["volume_ls_error"] = error
    done = host.run(env=ALLOW)
    assert done.returncode == 1
    assert "docker volume ls failed" in done.stderr and error in done.stderr
    assert "a new host" not in done.stdout
    assert not (host.deploy / ".env").exists()


def test_a_token_file_left_by_an_earlier_first_boot_is_refused_on_a_new_host(
    tmp_path: Path,
) -> None:
    """Review I1: what `docker compose down -v && rm .env` leaves behind. No
    OpenBao on a host with no .env and no volume knows that token."""
    host = Host(tmp_path)
    host.etc.mkdir()
    (host.etc / "gateway-issuer.token").write_text("s.token-of-an-earlier-first-boot\n")
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 1
    assert "mixed state: no .env and no it-suite volume, but this host keeps" in done.stderr
    removal = f"        sudo rm -f {host.etc}/gateway-issuer.token && ./scripts/first-boot.sh"
    assert removal in done.stderr.splitlines()
    assert "new-env.sh" not in host.argv() and host.state["bao"]["init"] is False
    assert host.state["bao"]["tokens"] == {}


def test_a_stale_its_app_token_with_no_root_token_in_env_is_a_mixed_state(
    tmp_path: Path,
) -> None:
    """Review I1: mixed state (c) looked at OPENBAO_TOKEN only."""
    host = Host(tmp_path)
    _after_init(host)
    host.write_env(IT_SUITE_VERSION=host.tag, ITS_OPENBAO_TOKEN="s.its-app-of-another-boot")
    shares = host.secrets["shares"]
    stdin = "".join(x + "\n" for x in (shares[0], shares[1], shares[2], host.secrets["root"]))
    done = host.run(stdin=stdin)
    assert done.returncode == 1
    assert "ITS_OPENBAO_TOKEN in .env is not an its-app token of this OpenBao" in done.stderr
    (zero,) = _guarded_lines(done.stderr, "DESTROY")
    assert " && docker compose down -v && rm .env && " in zero
    _recipe_removes_the_earlier_boots_files(host, done.stderr)
    assert host.state["bao"]["tokens"] == {}, "it issued a token for a .env it refused"
    assert host.env()["ITS_OPENBAO_TOKEN"] == "s.its-app-of-another-boot"
    assert host.leaks() == []


def test_a_token_file_this_openbao_does_not_know_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    _done(host)
    (host.etc / "gateway-issuer.token").write_text("s.another-openbaos\n")
    done = host.run()
    assert done.returncode == 1
    assert "gateway-issuer.token holds a token this OpenBao does not know" in done.stderr
    removal = f"        sudo rm -f {host.etc}/gateway-issuer.token && ./scripts/first-boot.sh"
    assert removal in done.stderr.splitlines()


def test_tokens_in_env_and_an_openbao_never_initialised_are_a_mixed_state(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag, ITS_OPENBAO_TOKEN="s.old", OPENBAO_TOKEN="s.oldroot")
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    done = host.run(env=ALLOW)
    assert done.returncode == 1
    assert "mixed state: .env holds OpenBao tokens" in done.stderr
    assert "docker compose down -v && rm .env" in done.stderr
    assert "operator init" not in host.argv()
    _recipe_removes_the_earlier_boots_files(host, done.stderr)


def test_a_root_token_from_another_openbao_is_a_mixed_state(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag, ITS_OPENBAO_TOKEN="s.old", OPENBAO_TOKEN="s.oldroot")
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    host.state["bao"].update(init=True, sealed=False)
    host.state["services"]["openbao"] = {
        "state": "running", "health": "healthy", "exit": 0, "restarts": 0, "id": "c-openbao"
    }  # fmt: skip
    done = host.run(env=ALLOW)
    assert done.returncode == 1
    assert "OPENBAO_TOKEN in .env is not this OpenBao's root token" in done.stderr


def test_a_run_after_init_asks_for_three_shares_and_the_root_token(tmp_path: Path) -> None:
    """A run that stopped after `bao operator init`: the shares and the root
    token are no longer in memory. It never initialises again."""
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag)
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    host.state["bao"]["init"] = True
    shares = host.secrets["shares"]
    stdin = "".join(s + "\n" for s in (shares[4], shares[1], shares[0], host.secrets["root"]))
    done = host.run(stdin=stdin)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "skip    step 3.2: OpenBao is initialised" in done.stdout
    assert "operator init" not in host.argv()
    assert host.env()["OPENBAO_TOKEN"] == host.secrets["root"]
    assert host.leaks() == []  # review M9: the re-run's prompts are scanned too


def test_a_wrong_root_token_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag)
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    host.state["bao"].update(init=True, sealed=False)
    done = host.run(stdin="s.not-the-root\n")
    assert done.returncode == 1
    assert "that is not this OpenBao's root token" in done.stderr
    assert host.env()["OPENBAO_TOKEN"] == "change-me"


def test_wrong_shares_are_asked_again_three_rounds_then_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag)
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    host.state["bao"]["init"] = True
    done = host.run(stdin="".join(f"bad-{n}\n" for n in range(9)))
    assert done.returncode == 1
    assert done.stderr.count("OpenBao is still sealed: one of those three") == 3
    assert "still sealed after three rounds" in done.stderr


def test_an_openbao_that_stops_answering_is_not_called_a_wrong_share(tmp_path: Path) -> None:
    """Review M5."""
    host = Host(tmp_path)
    _after_init(host)
    host.state["bao"]["stop_after_keys"] = 2
    shares = host.secrets["shares"]
    done = host.run(stdin="".join(s + "\n" for s in shares[:3]))
    assert done.returncode == 1
    assert "OpenBao stopped answering" in done.stderr
    assert "is not a share of this OpenBao" not in done.stderr


def test_the_preflight_refusing_stops_before_postgres(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(env={**ALLOW, "FAKE_RC_PREFLIGHT": "1"})
    assert done.returncode == 1
    assert "the pre-flight refused" in done.stderr
    assert "compose up" not in host.argv()


def test_an_inherited_compose_file_gains_the_auto_unseal_overlay(tmp_path: Path) -> None:
    """The CI's COMPOSE_FILE (the Pebble overlay) wins over .env's: after the
    auto-unseal, every docker call carries openbao/auto-unseal.yml too, and
    openbao-seal.sh itself sees no inherited COMPOSE_FILE at all."""
    host = Host(tmp_path)
    pebble = "docker-compose.yml:ci/acme-pebble.yml"
    done = host.run(stdin=_saved_and_three(host), env={**ALLOW, "COMPOSE_FILE": pebble})
    assert done.returncode == 0, done.stdout + done.stderr
    log = host.argv().splitlines()
    seal = next(i for i, line in enumerate(log) if line.startswith("openbao-seal.sh enable"))
    assert log[seal + 1] == "  COMPOSE_FILE="
    before = {line for line in log[:seal] if line.startswith("  COMPOSE_FILE=")}
    after = {line for line in log[seal + 2 :] if line.startswith("  COMPOSE_FILE=")}
    assert before == {f"  COMPOSE_FILE={pebble}"}
    assert after == {f"  COMPOSE_FILE={pebble}:openbao/auto-unseal.yml"}


def test_without_an_inherited_compose_file_none_is_set(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    composed = {line for line in host.argv().splitlines() if line.startswith("  COMPOSE_FILE=")}
    assert composed == {"  COMPOSE_FILE="}


def test_a_run_after_a_failed_auto_unseal_asks_for_three_shares(tmp_path: Path) -> None:
    host = Host(tmp_path)
    first = host.run(stdin=_saved_and_three(host), env={**ALLOW, "FAKE_SEAL_FAIL": "1"})
    assert first.returncode == 1 and "openbao-seal.sh enable failed" in first.stderr
    shares = host.secrets["shares"]
    held = "".join(s + "\n" for s in (shares[0], shares[2], shares[4]))
    again = host.run(stdin=held + "SEAL\n")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "this run holds none: type them" in again.stdout
    assert host.state["seal_stdin"] == 3 and host.state["bao"]["type"] == "static"
    # Review I2: proved first, by sealing OpenBao and unsealing it with them.
    assert "bao operator seal, with the root token on its stdin" in lines(again.stdout, "run")
    assert host.leaks() == []


def test_shares_typed_on_a_rerun_are_proved_before_openbao_seal_sh_gets_them(
    tmp_path: Path,
) -> None:
    """Review I2: a run stopped after step 3.3, with OpenBao unsealed, and the
    next run holds no share. A typo there used to reach openbao-seal.sh, which
    took it inside the seal migration and stopped half-way. Now it is a round
    of step 3.3's kind: asked again, and openbao-seal.sh gets three good ones."""
    host = Host(tmp_path)
    host.state["oneshot_exit"] = {"openbao-init": 1}
    first = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert first.returncode == 1 and "openbao-init exited 1" in first.stderr
    host.state["oneshot_exit"] = {}
    shares = host.secrets["shares"]
    typed = ("a-typo", shares[0], shares[1], "SEAL", shares[0], shares[1], shares[2])
    again = host.run(stdin="".join(s + "\n" for s in typed))
    assert again.returncode == 0, again.stdout + again.stderr
    assert again.stderr.count("one of those three is not a share of this OpenBao") == 1
    assert host.state["seal_stdin"] == 3
    assert host.state["bao"]["type"] == "static" and host.state["bao"]["migrating"] is False
    assert host.leaks() == []


def _half_migrated(host: Host) -> None:
    """openbao-seal.sh enable stopped after it wrote .env and recreated
    OpenBao in seal-migration mode (openbao-seal.sh, cmd_enable)."""
    first = host.run(stdin=_saved_and_three(host), env={**ALLOW, "FAKE_SEAL_FAIL": "midway"})
    assert first.returncode == 1 and "openbao-seal.sh enable failed" in first.stderr
    assert host.state["bao"]["migrating"] is True and host.state["bao"]["sealed"] is True
    assert host.env()["COMPOSE_FILE"] == "docker-compose.yml:openbao/auto-unseal.yml"


def test_a_seal_migration_left_half_way_is_named_with_the_ways_out(tmp_path: Path) -> None:
    host = Host(tmp_path)
    _half_migrated(host)
    again = host.run(stdin="")
    assert again.returncode == 1
    err = again.stderr
    assert "half-way through the seal migration" in err
    assert "key file is missing" not in err, "the key file is blamed for a migration"
    # Review m3: ONE of two ways, each a block of its own; the hidden prompts last.
    header, finish, undo = _ways(err)
    assert header == ["  # run ONE of the following, never more than one: each block below is "
                      "one whole way on, to paste on its own"]  # fmt: skip
    assert finish[-1] == (
        "        for n in 1 2 3; do docker compose exec openbao bao operator unseal -migrate; done"
        " && docker compose restart openbao && ./scripts/first-boot.sh"
    )
    assert any("asks for one share" in line for line in finish if line.startswith("  # "))
    # Review I1 (round 3): the undo runs only behind a typed UNDO, in one chain.
    assert _guarded_lines("\n".join(undo), "UNDO") == [undo[-1]]
    assert undo[-1].endswith(
        " && sed -i '/^COMPOSE_FILE=/d;/^OPENBAO_UNSEAL_KEY_FILE=/d;/^OPENBAO_SEAL_STANZA=/d' .env"
        " && docker compose up -d --no-deps openbao && ./scripts/first-boot.sh"
    ), undo[-1]
    assert "recreates OpenBao without its static seal" in undo[-1]
    prose = " ".join(line for line in err.splitlines() if line.startswith("  # "))
    assert "sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh status" in prose
    # Review m6: openbao-seal.sh's closing note, which this way does not reach.
    key = host.etc / "openbao-unseal" / "unseal.key"
    assert any(f"copy {key} OFF this host" in line for line in finish), finish
    assert "openbao-seal.sh" not in host.argv()


def test_after_the_migration_is_finished_by_hand_the_next_run_goes_on(tmp_path: Path) -> None:
    host = Host(tmp_path)
    _half_migrated(host)
    # The three `bao operator unseal -migrate`, then `docker compose restart openbao`.
    host.state["bao"].update(type="static", sealed=False, migrating=False)
    again = host.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert "skip    auto-unseal: on, and OpenBao is unsealed with its static key" in again.stdout


def test_after_the_migration_is_undone_the_next_run_moves_the_seal_again(tmp_path: Path) -> None:
    host = Host(tmp_path)
    _half_migrated(host)
    # The sed on .env, then `docker compose up -d --no-deps openbao`: Shamir, sealed.
    env = host.deploy / ".env"
    kept = [line for line in env.read_text().splitlines()
            if not line.startswith(("COMPOSE_FILE=", "OPENBAO_UNSEAL_KEY_FILE="))]  # fmt: skip
    env.write_text("\n".join(kept) + "\n")
    host.state["bao"].update(type="shamir", sealed=True, migrating=False)
    shares = host.secrets["shares"]
    again = host.run(stdin="".join(s + "\n" for s in (shares[3], shares[1], shares[2])))
    assert again.returncode == 0, again.stdout + again.stderr
    assert host.state["bao"]["type"] == "static" and host.state["seal_stdin"] == 3


def test_an_undo_left_half_done_is_named(tmp_path: Path) -> None:
    host = Host(tmp_path)
    _half_migrated(host)
    env = host.deploy / ".env"
    kept = [line for line in env.read_text().splitlines()
            if not line.startswith(("COMPOSE_FILE=", "OPENBAO_UNSEAL_KEY_FILE="))]  # fmt: skip
    env.write_text("\n".join(kept) + "\n")
    again = host.run(stdin="")
    assert again.returncode == 1
    assert "still waits for a seal migration, but .env no longer has" in again.stderr
    (undo,) = _guarded_lines(again.stderr, "UNDO")
    assert undo.endswith(" && docker compose up -d --no-deps openbao && ./scripts/first-boot.sh")


def test_the_step_3_4_run_line_names_only_the_token_it_creates(tmp_path: Path) -> None:
    """Review M7."""
    host = Host(tmp_path)
    _done(host)
    env = host.deploy / ".env"
    env.write_text(env.read_text().replace(host.secrets["its-app"], "change-me"))
    again = host.run()
    assert again.returncode == 0, again.stdout + again.stderr
    (ran,) = [r for r in lines(again.stdout, "run") if r.startswith("bao policy write")]
    assert ran.endswith("; a new token for its-app"), ran


def test_names_a_caller_exports_carry_no_secret_into_a_child(tmp_path: Path) -> None:
    """Review M3: bash keeps the export flag of a name the environment
    exported -- `local` included -- so a caller exporting `root` or `share`
    would hand the root token or a share to every docker, sudo and tee."""
    names = ("root", "share", "shares", "typed", "app", "gw", "t", "token", "out",
             "init_out", "line", "answer", "root_in_env", "p", "want", "name")  # fmt: skip
    exported = {name: "exported-by-the-caller" for name in names}
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env={**ALLOW, **exported})
    assert done.returncode == 0, done.stdout + done.stderr
    assert host.leaks() == []
    rerun = Host(tmp_path / "rerun")
    _after_init(rerun)
    shares = rerun.secrets["shares"]
    stdin = "".join(s + "\n" for s in (shares[4], shares[1], shares[0], rerun.secrets["root"]))
    again = rerun.run(stdin=stdin, env=exported)
    assert again.returncode == 0, again.stdout + again.stderr
    assert rerun.leaks() == []
    assert "exported-by-the-caller" not in rerun.environ_log(), "an exported name kept its flag"


def test_every_name_the_script_keeps_a_value_in_is_lowercase() -> None:
    """Review M3: the script takes the export flag off every lowercase name a
    caller exported. That protects what it keeps only while every name it
    keeps a value in is lowercase -- steps added later included."""
    names: set[str] = set()
    for line in _code().splitlines():
        s = line.strip()
        try:
            words = shlex.split(s, comments=True)
        except ValueError:
            words = []
        bare = re.sub(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'", '""', s)  # no quoted text
        if words[:1] in (["local"], ["declare"]):
            words = [w for w in words[1:] if not w.startswith("-")]
        for word in words:
            m = re.fullmatch(r"([A-Za-z_]\w*)\+?=.*", word, re.S)
            # A quoted 'NAME=...' is a word, not an assignment.
            if not m or not re.search(rf"(?:^|\s){m.group(1)}\+?=", bare):
                break
            names.add(m.group(1))
        names |= set(re.findall(r"(?:;|&&|\|\||\bthen|\bdo|\belse)\s+([A-Za-z_]\w*)\+?=", bare))
        names |= set(re.findall(r"\bfor\s+([A-Za-z_]\w*)\s+in\b", s))
        for cmd in re.findall(r"""\bread((?:\s+(?:"[^"]*"|'[^']*'|[^\s|;&<)"']+))*)""", s):
            words = shlex.split(cmd.rstrip(" \\"))
            names |= {w for w in words if re.fullmatch(r"[A-Za-z_]\w*", w)}
    assert {"root", "share", "shares", "typed", "app", "gw", "line"} <= names, "a blind parse"
    # IFS for one `read`; BAO_TOKEN is read by the openbao container's shell.
    assert {n for n in names if n != n.lower()} <= {"IFS", "BAO_TOKEN"}


def test_a_failure_outside_die_says_failed_the_step_and_how_to_go_on(tmp_path: Path) -> None:
    """Review M4: a command that fails where no `die` waits for it."""
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env={**ALLOW, "FAKE_DOCKER_FAIL": "psql"})
    assert done.returncode == 1
    assert done.stderr.count("FAILED") == 1
    assert "FAILED  Step 2 — Postgres: a command failed (exit 17)" in done.stderr
    assert "        ./scripts/first-boot.sh" in done.stderr.splitlines()
    refused = host.run(env={**ALLOW, "FAKE_RC_PREFLIGHT": "1"})
    assert refused.stderr.count("FAILED") == 1, "die reported twice"


# --------------------------------------------------------------------------- #
# Every FAILED block can be pasted (review M6; Host.run checks it on every run)
# --------------------------------------------------------------------------- #


def _volumes(host: Host) -> subprocess.CompletedProcess[str]:
    host.state["volumes"] = ["it-suite_openbao-data"]
    return host.run(env=ALLOW)


def _stale_file(host: Host) -> subprocess.CompletedProcess[str]:
    host.etc.mkdir()
    (host.etc / "gateway-issuer.token").write_text("s.old\n")
    return host.run(env=ALLOW)


def _wrong_host(host: Host) -> subprocess.CompletedProcess[str]:
    host.write_env(IT_SUITE_VERSION=host.tag)
    return host.run("--host", "other.example.org", env=ALLOW)


def _env_mode(host: Host) -> subprocess.CompletedProcess[str]:
    host.write_env(IT_SUITE_VERSION=host.tag)
    (host.deploy / ".env").chmod(0o644)
    return host.run(env=ALLOW)


def _no_roles(host: Host) -> subprocess.CompletedProcess[str]:
    host.state["pg"]["roles"] = 0
    return host.run(env=ALLOW)


def _wrong_shares(host: Host) -> subprocess.CompletedProcess[str]:
    _after_init(host)
    return host.run(stdin="".join(f"bad-{n}\n" for n in range(9)))


def _wrong_root(host: Host) -> subprocess.CompletedProcess[str]:
    _after_init(host, sealed=False)
    return host.run(stdin="s.not-the-root\n")


def _no_saved(host: Host) -> subprocess.CompletedProcess[str]:
    return host.run(stdin="", env=ALLOW)


def _seal_fails(host: Host) -> subprocess.CompletedProcess[str]:
    return host.run(stdin=_saved_and_three(host), env={**ALLOW, "FAKE_SEAL_FAIL": "1"})


def _half_way(host: Host) -> subprocess.CompletedProcess[str]:
    _half_migrated(host)
    return host.run()


SCENARIOS: dict[str, Callable[[Host], subprocess.CompletedProcess[str]]] = {
    "unknown argument": lambda host: host.run("--frobnicate"),
    "volumes without .env": _volumes,
    "docker volume ls fails": lambda host: (
        host.state.update(volume_ls_error="permission denied"),
        host.run(env=ALLOW),
    )[1],
    "a token file of an earlier boot": _stale_file,
    "--host another than .env's": _wrong_host,
    ".env mode 644": _env_mode,
    "the pre-flight refuses": lambda host: host.run(env={**ALLOW, "FAKE_RC_PREFLIGHT": "1"}),
    "postgres without its roles": _no_roles,
    "no terminal": lambda host: host.run(),
    "no SAVED": _no_saved,
    "wrong shares": _wrong_shares,
    "a wrong root token": _wrong_root,
    "openbao-seal.sh fails": _seal_fails,
    "a migration half-way": _half_way,
    "a failure outside die": lambda host: host.run(
        stdin=_saved_and_three(host), env={**ALLOW, "FAKE_DOCKER_FAIL": "psql"}
    ),
    "a leftover unseal key": lambda host: _leftover_key_run(host),
    "tokens in .env, OpenBao never initialised": lambda host: _never_initialised_run(host),
    "stopped before the seal": lambda host: _not_sealed_run(host),
}

#: What makes a first run fail in steps 4 to 8 (Task 3), and what its report
#: says: its FAILED line first, then other lines it must hold; `_late_failure`
#: sets each up.
LATE_FAILURES: dict[str, tuple[str, ...]] = {
    "no revision file in the checkout": ("no revision file in ../backend/migrations/versions",),
    "migrate exits 1": ("migrate exited 1",),
    "migrate exits 1, and compose with it": ("migrate exited 1",),
    "the schema short of the newest revision": (
        "after migrate the schema is at 0247, not 0248",),
    "postgres-grants exits 1": ("postgres-grants exited 1",),
    "postgres-grants exits 1, and compose with it": ("postgres-grants exited 1",),
    "the RBAC catalog writable after postgres-grants": (
        "after postgres-grants, app_user is not narrowed",),
    "an empty audit key": ("/var/lib/its/secrets/audit-hmac.key is there, so set -C",),
    "the gateway's certificate not issued": ("the gateway's certificate was not issued",),
    "docker compose up -d fails": ("docker compose up -d failed (above)",
                                   "        docker compose ps -a"),
    "a service that stops": ("a service stopped: worker(exited 1)",),
    # Review m8 of Task 3: the names are services, whatever the project is called.
    "a service that keeps restarting": ("after 300 s, still waiting on:", " worker(restarted)",
                                        "        docker compose logs --tail 50 "),
    "a one-shot still running": ("after 300 s, still waiting on: openbao-init(running)",),
    "the :443 certificate refused": ("the certificate on :443 is not one a browser accepts",),
    "acme.json not 600": ("Traefik's acme.json is mode 644, not 600",
                          "Traefik writes acme.json itself"),
    ":8443 not the internal CA's": (":8443 does not present the internal CA's certificate",),
    # Review m4: the runbook's named failure, the same name from another issuer.
    ":8443 the Let's Encrypt certificate": (
        ":8443 does not present the internal CA's certificate",),
    "the api does not answer setup/status": ("the api did not answer /api/v1/setup/status",),
    "the api wrote no install code": ("the api wrote no install code",),
    "no setup-code file": ("the api wrote no install code",),
}  # fmt: skip


def _late(name: str) -> Callable[[Host], subprocess.CompletedProcess[str]]:
    return lambda host: _late_failure(host, name)


# Review r3 (round 3): the five reports that guard or delete and were not
# pasted yet, two of them with an apostrophe in their FAILED line; r5's; and
# every FAILED of steps 4 to 8.
SCENARIOS.update({
    "OPENBAO_TOKEN of another OpenBao": lambda host: _another_root_run(host),
    "ITS_OPENBAO_TOKEN of another first boot": lambda host: _stale_its_app_run(host),
    "a token file this OpenBao does not know": lambda host: _unknown_token_file_run(host),
    "step 3.5 finds tokens it does not know": lambda host: _forgotten_its_app_run(host),
    ".env has no ITS_PUBLIC_HOST": lambda host: _no_public_host_run(host),
    "OpenBao stops answering a token lookup": lambda host: _lookup_unanswered_run(host, 3),
    **{name: _late(name) for name in LATE_FAILURES},
})


def _never_initialised_run(host: Host) -> subprocess.CompletedProcess[str]:
    host.write_env(IT_SUITE_VERSION=host.tag, ITS_OPENBAO_TOKEN="s.old", OPENBAO_TOKEN="s.oldroot")
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    return host.run(env=ALLOW)


def _leftover_key_run(host: Host) -> subprocess.CompletedProcess[str]:
    _leftover_key(host)
    return host.run(env=ALLOW)


def _not_sealed_run(host: Host) -> subprocess.CompletedProcess[str]:
    _no_shares_in_memory(host)
    return host.run(stdin="".join(s + "\n" for s in host.secrets["shares"][:3]) + "no\n")


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_command_of_a_failed_block_can_be_pasted(tmp_path: Path, scenario: str) -> None:
    """Each line at the command indent is a shell command the operator can
    run as it stands, starting with the `cd` to deploy/, so that they work
    from any folder; prose is at two spaces. `Host.run` raises if not.

    A failure of steps 4 to 8 also says what failed, and no secret leaves on
    the way (what test_each_failure_of_steps_4_to_8_says_what_failed checked
    on a run of its own until review m10 of Task 3)."""
    host = Host(tmp_path)
    done = SCENARIOS[scenario](host)
    assert done.returncode == 1, done.stdout + done.stderr
    block = done.stderr[done.stderr.index("FAILED  ") :].splitlines()
    commands = [line[8:] for line in block if line.startswith("        ")]
    assert commands[0] == f"cd {host.deploy}"
    assert any("./scripts/first-boot.sh" in c for c in commands), commands
    if scenario in LATE_FAILURES:
        header, *more = LATE_FAILURES[scenario]
        assert f"FAILED  {header}" in done.stderr, done.stderr
        for text in more:
            assert text in done.stderr, (text, done.stderr)
        assert host.leaks() == []
        assert host.secrets["code"] not in done.stdout + done.stderr


# --------------------------------------------------------------------------- #
# Fix round 2 (re-review of 2026-11-27, findings m1 to m6)
# --------------------------------------------------------------------------- #


def _no_shares_in_memory(host: Host) -> None:
    """A run that failed in openbao-seal.sh before it changed anything:
    OpenBao is unsealed, with Shamir, and the next run holds no share."""
    first = host.run(stdin=_saved_and_three(host), env={**ALLOW, "FAKE_SEAL_FAIL": "1"})
    assert first.returncode == 1
    assert host.state["bao"]["sealed"] is False and host.state["bao"]["type"] == "shamir"


def _leftover_key(host: Host) -> Path:
    key = host.etc / "openbao-unseal" / "unseal.key"
    key.parent.mkdir(parents=True)
    key.write_bytes(os.urandom(32))
    return key


@pytest.mark.parametrize(
    "answer", [None, "no\n", ""], ids=["at-the-first-prompt", "not-SEAL", "end-of-input-at-SEAL"]
)
def test_without_three_shares_at_hand_the_run_stops_before_the_seal(
    tmp_path: Path, answer: str | None
) -> None:
    """Review m1: the three are read and held first, and the seal waits for a
    SEAL. Stopping at the first prompt, answering anything but SEAL after the
    third, or the input ending there (n1, round 3), changes nothing: OpenBao
    stays unsealed."""
    host = Host(tmp_path)
    _no_shares_in_memory(host)
    shares = host.secrets["shares"]
    stdin = "" if answer is None else "".join(s + "\n" for s in shares[:3]) + answer
    again = host.run(stdin=stdin)
    assert again.returncode == 1
    assert host.state["bao"]["sealed"] is False and host.state["bao"]["type"] == "shamir"
    assert "operator seal" not in host.argv()
    assert "openbao-seal.sh" not in host.argv()
    assert host.leaks() == []


def test_under_a_terminal_the_seal_is_named_and_confirmed_before_it_happens(
    tmp_path: Path,
) -> None:
    """Review m1: what is about to happen is said before the first share is
    asked for, and confirmed after the third, before the seal."""
    host = Host(tmp_path)
    _no_shares_in_memory(host)
    shares = host.secrets["shares"]
    answers: list[tuple[str, str | int]] = [
        (_share_prompt(n), shares[i]) for n, i in ((1, 1), (2, 3), (3, 4))
    ]
    answers.append((SEAL_PROMPT, "SEAL"))
    rc, shown = host.run_pty(answers=answers)
    assert rc == 0, shown
    said = shown.index("OpenBao stays sealed until three good shares unseal it")
    assert said < shown.index(_share_prompt(1))
    assert shown.index(_share_prompt(3)) < shown.index(SEAL_PROMPT)
    assert shown.index(SEAL_PROMPT) < shown.index("run     bao operator seal")
    for share in shares:
        assert share not in shown
    assert host.state["bao"]["type"] == "static" and host.state["seal_stdin"] == 3
    assert host.leaks() == []


def test_three_wrong_rounds_give_the_way_out_of_lost_shares(tmp_path: Path) -> None:
    """Review m2: lost shares are a vault nobody can open; on a first boot,
    nothing is in it yet, and the way out is to start from zero."""
    host = Host(tmp_path)
    _after_init(host)
    done = host.run(stdin="".join(f"bad-{n}\n" for n in range(9)))
    assert done.returncode == 1
    header, copies, lost = _ways(done.stderr)
    assert header[0].startswith("  # run ONE of the following")
    assert copies[-1] == "        ./scripts/first-boot.sh"
    assert any("the shares are lost" in line for line in lost), lost
    assert _guarded_lines("\n".join(lost), "DESTROY") == [lost[-1]]
    assert " && docker compose down -v && rm .env && " in lost[-1]


_RERUN = "        cd {d}\n        ./scripts/first-boot.sh"
_GO = "read -r -p 'Type GO to go on: ' answer && [ \"$answer\" = GO ]"


@pytest.mark.parametrize(
    ("block", "problem"),
    [
        pytest.param("  # fine prose\n        cd {d}\n        ./scripts/first-boot.sh", None,
                     id="a-good-block"),
        pytest.param("", "no way on", id="no-way-on"),
        pytest.param("  # only prose", "a way on with no command", id="prose-alone"),
        pytest.param("  fix it, then:\n        cd {d}\n        ./scripts/first-boot.sh",
                     "neither a command nor prose", id="prose-without-#"),
        pytest.param("        cd {d}\n        ./scripts/first-boot.sh\n\n"
                     "        cd {d}\n        ./scripts/first-boot.sh",
                     "without `# run ONE of the following`", id="two-ways-no-header"),
        pytest.param("        cd {d}\n        ./scripts/first-boot.sh\n        chmod 600 .env",
                     "may read the terminal is not the last", id="a-rerun-not-last"),
        pytest.param("        cd {d}\n        sudo rm -f x\n        ./scripts/first-boot.sh",
                     "may read the terminal is not the last", id="sudo-not-last"),
        pytest.param("        cd {d}\n        docker compose exec openbao bao operator unseal"
                     " -migrate\n        docker compose restart openbao",
                     "may read the terminal is not the last", id="hidden-prompt-not-last"),
        pytest.param("        cd {d}\n        ./scripts/first-boot.sh, without --host",
                     "not a command the operator runs", id="prose-as-a-command"),
        pytest.param("        cd {d}\n        sed -i 's/x/<sha printed>/' .env",
                     "a placeholder", id="a-placeholder"),
        pytest.param("  # run ONE of the following\n\n" + _RERUN + "\n\n" + _RERUN,
                     "is not one chain behind a typed guard", id="an-unguarded-second-way"),
        pytest.param("        cd {d}\n        docker compose down -v && rm .env",
                     "destroys or undoes without a typed guard", id="destroys-unguarded"),
        pytest.param("        cd {d}\n        " + _GO + " && docker compose down -v"
                     "\n        ./scripts/first-boot.sh",
                     "may read the terminal is not the last", id="a-guard-not-last"),
        pytest.param("        cd {d}\n        " + _GO.replace("= GO ]", "= GOO ]")
                     + " && docker compose down -v",
                     "destroys or undoes without a typed guard", id="a-guard-of-another-word"),
    ],
)  # fmt: skip
def test_the_failed_block_check_fails_what_it_must(block: str, problem: str | None) -> None:
    """Review m3: the check `Host.run` makes on every run can fail."""
    deploy = Path("/srv/it-suite/deploy")
    problems = failed_block_problems("\nFAILED  something\n" + block.format(d=deploy), deploy)
    if problem is None:
        assert problems == []
    else:
        assert any(problem in found for found in problems), problems


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP],
                         ids=["INT", "TERM", "HUP"])  # fmt: skip
def test_stopped_at_saved_it_says_the_shares_on_screen_are_the_only_copy(
    tmp_path: Path, sig: signal.Signals
) -> None:
    """Review m4: Ctrl-C (or a TERM, or the terminal hanging up) at the SAVED
    prompt. The screen is not cleared, and the run says what is at stake."""
    host = Host(tmp_path)
    rc, shown = host.run_pty(answers=[(SAVED_PROMPT, int(sig))])
    text = shown.replace("\r", "")
    assert rc == 1, text
    assert CLEAR not in text, "the screen was cleared"
    assert all(share in text for share in host.secrets["shares"]), "the shares left the screen"
    assert "the five shares and the root token on this screen are their only copy" in text
    assert "OpenBao is initialised already" in text
    header, saved, lost = _ways(text)
    assert header[0].startswith("  # run ONE of the following")
    # Review n3 (round 3): the way on that keeps going clears the screen and
    # its scrollback first, where the shares still are -- and (r4) only once
    # SAVED is typed: a report pasted whole must not wipe their only copy.
    _assert_saved_clears_behind_a_guard(saved)
    assert _guarded_lines("\n".join(lost), "DESTROY") == [lost[-1]]
    assert " && docker compose down -v && rm .env && sudo rm -f " in lost[-1]
    assert failed_block_problems(text, host.deploy) == []
    assert host.state["bao"]["init"] is True


def test_a_leftover_unseal_key_alone_is_refused_on_a_new_host(tmp_path: Path) -> None:
    """Review m5: the key file of an earlier first boot, with no token file."""
    host = Host(tmp_path)
    key = _leftover_key(host)
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 1
    assert "mixed state: no .env and no it-suite volume, but this host keeps" in done.stderr
    (removal,) = _guarded_lines(done.stderr, "DELETE")
    assert removal.endswith(f" && sudo rm -f {key} && ./scripts/first-boot.sh"), removal
    assert "new-env.sh" not in host.argv() and host.state["bao"]["init"] is False


# --------------------------------------------------------------------------- #
# Fix round 3 (re-review 2 of 2026-11-27: I1, n1 to n3)
# --------------------------------------------------------------------------- #


def _assert_saved_clears_behind_a_guard(way: list[str]) -> None:
    """Review r4 (round 3): the clear of the custody report's first way on,
    the only copy of the shares, runs behind a typed SAVED."""
    assert _guarded_lines("\n".join(way), "SAVED") == [way[-1]], way
    assert way[-1].endswith(" && printf '\\033[H\\033[2J\\033[3J' && ./scripts/first-boot.sh")
    assert "only once they are stored off this VPS" in way[-1], way[-1]


def test_end_of_input_at_saved_says_the_shares_on_screen_are_the_only_copy(
    tmp_path: Path,
) -> None:
    """Review n2: the custody warning when the input ends at the SAVED prompt,
    as on INT, TERM and HUP."""
    host = Host(tmp_path)
    done = host.run(stdin="", env=ALLOW)
    assert done.returncode == 1
    assert (
        "  WARNING the five shares and the root token on this screen are their only copy"
    ) in done.stderr
    assert "FAILED  the input ended before SAVED: OpenBao is initialised already" in done.stderr
    header, saved, lost = _ways(done.stderr)
    _assert_saved_clears_behind_a_guard(saved)
    assert _guarded_lines("\n".join(lost), "DESTROY") == [lost[-1]]
    assert host.state["bao"]["init"] is True


def _acts_of(way: list[str]) -> set[str]:
    """What the paste stand-ins log when WAY's command lines run."""
    acts = set()
    for line in way:
        if not line.startswith("        "):
            continue
        for words in _segments(line[8:]):
            words = words[1:] if words[:1] == ["do"] else words
            if words[:1] == ["./scripts/first-boot.sh"]:
                acts.add(" ".join(["first-boot.sh", *words[1:]]))
            elif words[:1] and words[0] in PASTE_STAND_INS:
                acts.add(" ".join(words))
    return acts


def _report(host: Host, scenario: str) -> str:
    done = SCENARIOS[scenario](host)
    assert done.returncode == 1, done.stdout + done.stderr
    return done.stderr[done.stderr.index("FAILED  ") :]


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_a_whole_report_pasted_runs_one_way_on_at_most(tmp_path: Path, scenario: str) -> None:
    """Review I1 (round 3): an operator who copies everything under FAILED and
    pastes it into a shell. Without bracketed paste ("later-lines-fed") a typed
    guard reads the line after it; with it ("answered-empty"), the guard waits,
    and is answered with nothing. Either way, the acts of one way on at most
    run, and nothing destroys or undoes. (Every command is a logging stand-in:
    nothing real runs.) Both pastes are of the one report: one run of the
    script, not one a mode (review m10 of Task 3)."""
    host = Host(tmp_path)
    report = _report(host, scenario)
    ways = _ways(report)
    if ways[0][0].startswith("  # run ONE of the following"):
        ways = ways[1:]
    guards = sum(line.startswith("        read -r -p '") for line in report.splitlines())
    for mode, bracketed in (("later-lines-fed", False), ("answered-empty", True)):
        answers = [""] * guards if bracketed else []
        acts = [act.rstrip() for act in paste_into_a_shell(host, report, bracketed=bracketed,
                                                           answers=answers)]  # fmt: skip
        assert sum(act.startswith("first-boot.sh") for act in acts) <= 1, (mode, acts)
        assert [act for act in acts if DESTRUCTIVE_ACT.match(act)] == [], (mode, acts)
        assert any(set(acts) <= _acts_of(way) for way in ways), (mode, acts, ways)
        # Review r1 (round 3): not green for nothing. The paste helper proves the
        # shell read the paste and ran the line after it; and where the FAILED
        # line leaves no quote open, the first way on, if no guard holds it, ran.
        if _quotes_closed(report.splitlines()[0]) and not _behind_a_guard(ways[0]):
            assert _acts_of(ways[0]) <= set(acts), (mode, acts, ways[0])


def _quotes_closed(line: str) -> bool:
    try:
        shlex.split(line)
    except ValueError:
        return False
    return True


def _behind_a_guard(way: list[str]) -> bool:
    return any(line.startswith("        read -r -p '") for line in way)


@pytest.mark.parametrize(
    "scenario",
    ["--host another than .env's", "a migration half-way", "volumes without .env",
     "postgres without its roles", "a leftover unseal key", "no SAVED", "an empty audit key"],
)  # fmt: skip
def test_a_guarded_way_on_runs_once_its_word_is_typed(tmp_path: Path, scenario: str) -> None:
    """The guards are not a chain that never runs: pasted on its own, a guarded
    way on does nothing on any other answer, and does all of it on its word.
    Every guarded way of the report: the custody one has two (review r4)."""
    host = Host(tmp_path)
    report = _report(host, scenario)
    guarded = [w for w in _ways(report) if _behind_a_guard(w)]
    assert guarded, report
    for way in guarded:
        word = re.search(r'\[ "\$answer" = ([A-Z]+) \]', way[-1])
        assert word, way[-1]
        block = "\n".join(way) + "\n"
        lower = paste_into_a_shell(host, block, bracketed=True, answers=[word.group(1).lower()])
        assert lower == [], (word.group(1), lower)
        acts = [a.rstrip() for a in paste_into_a_shell(host, block, bracketed=True,
                                                       answers=[word.group(1)])]  # fmt: skip
        assert acts and acts[-1].startswith("first-boot.sh"), acts
        assert set(acts) == _acts_of(way), (acts, way)


# --------------------------------------------------------------------------- #
# Steps 4 to 8, the auto-unseal and its restart (plan 2026-11-27, Task 3)
# --------------------------------------------------------------------------- #

README = Path(__file__).resolve().parents[2] / "deploy" / "README.md"


def _done_host(tmp_path: Path) -> tuple[Host, str]:
    """A host after one whole, successful run."""
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    return host, done.stdout


def test_a_new_host_runs_every_step_to_the_install_code(tmp_path: Path) -> None:
    host, out = _done_host(tmp_path)
    ran = lines(out, "run")
    for expected in (
        "docker compose up migrate",
        "docker compose up postgres-grants",
        "32 random bytes into /var/lib/its/secrets/audit-hmac.key, mode 600",
        "docker compose up -d: it started or recreated what was not up",
        "docker compose restart openbao",
    ):
        assert expected in ran, (expected, ran)
    assert any(r.startswith("issue-gateway-cert.py rmm.novieri.com") for r in ran)
    assert any(r.startswith("sudo ./scripts/openbao-seal.sh enable --key-file ") for r in ran)
    assert f"  install code  {host.secrets['code']}" in out
    assert out.rstrip().splitlines()[-7] == "First boot: every step is done."
    env = host.env()
    assert env["COMPOSE_FILE"] == "docker-compose.yml:openbao/auto-unseal.yml"
    assert env["OPENBAO_UNSEAL_KEY_FILE"] == str(host.etc / "openbao-unseal" / "unseal.key")
    assert host.state["seal_stdin"] == 3, "openbao-seal.sh got the three shares of step 3.3"
    assert host.state["bao"]["type"] == "static" and host.state["bao"]["sealed"] is False
    assert host.state["files"] == {"audit": True, "gateway": True}
    assert "ok      OpenBao restarted and came back unsealed by itself" in out


def test_a_second_run_skips_every_step_and_changes_nothing(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    before = (host.state["services"], host.state["bao"], host.state["files"], host.env())
    again = host.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert lines(again.stdout, "run") == []
    skipped = [s.split(":")[0] for s in lines(again.stdout, "skip")]
    assert skipped == [
        "step 1", "step 1b", "step 2", "step 3.1", "step 3.2", "step 3.3", "step 3.4",
        "step 3.6", "auto-unseal", RESTART_SKIP,
        "step 4", "step 4b", "step 5", "step 6", "step 6b", "step 7",
    ]  # fmt: skip
    assert (host.state["services"], host.state["bao"], host.state["files"], host.env()) == before
    argv = host.argv()
    for changing in ("compose restart", "openbao-seal.sh", "operator init", "set -C", "tee "):
        assert changing not in argv, changing


def test_without_a_terminal_the_install_code_is_not_printed(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    again = host.run()
    assert host.secrets["code"] not in again.stdout + again.stderr
    assert "docker compose exec api cat /var/lib/its/secrets/setup-code" in again.stdout


def test_a_claimed_installation_skips_step_8(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    host.state["setup"]["needed"] = False
    again = host.run(env=ALLOW)
    assert "skip    step 8: the installation is claimed" in again.stdout
    assert host.secrets["code"] not in again.stdout


def test_the_audit_key_is_never_replaced(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.state["files"]["audit"] = True
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "skip    step 5: the audit HMAC key exists, and it is never replaced" in done.stdout
    assert "set -C" not in host.argv()


def test_the_gateway_certificate_reads_its_token_from_the_file_on_stdin(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    argv = host.argv().splitlines()
    cat = argv.index(f"sudo cat {host.etc}/gateway-issuer.token")
    issue = "/tmp/issue-gateway-cert.py rmm.novieri.com"
    run = next(i for i, line in enumerate(argv) if issue in line)
    assert run > cat
    assert argv[run].startswith("docker compose run --rm --no-deps -T -v ")


def _heredoc(text: str, opener: str) -> str:
    start = text.index(opener) + len(opener) + 1
    return text[start : text.index("\nEOF\n", start)] + "\n"


def test_step_6b_writes_the_units_the_runbook_writes(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    readme = README.read_text()
    for unit in ("its-gateway-cert.service", "its-gateway-cert.timer"):
        expected = _heredoc(readme, f"sudo tee /etc/systemd/system/{unit} >/dev/null <<'EOF'")
        expected = expected.replace("/opt/it-suite/deploy", str(host.deploy))
        expected = expected.replace("/etc/it-suite/", f"{host.etc}/")
        assert (host.units / unit).read_text() == expected, unit


def test_with_systemd_the_timer_is_enabled(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(stdin=_saved_and_three(host), env={**ALLOW, "ITS_NO_SYSTEMCTL": "0"})
    assert done.returncode == 0, done.stdout + done.stderr
    argv = host.argv()
    reload = argv.index("sudo systemctl daemon-reload")
    assert reload < argv.index("sudo systemctl enable --now its-gateway-cert.timer")
    assert "sudo systemctl list-timers its-gateway-cert.timer --no-pager" in argv


def test_a_failed_one_shot_stops_the_run(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.state["oneshot_exit"] = {"migrate": 1}
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 1
    assert "migrate exited 1" in done.stderr and "docker compose logs migrate" in done.stderr


def test_a_service_that_keeps_restarting_stops_step_7(tmp_path: Path) -> None:
    """Row 15 of "What was broken": a worker restarting for ever is up for a
    moment at a time. Up in every sample is not enough; its restarts must
    stand still."""
    host = Host(tmp_path)
    host.state["restart_step"] = 1
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 1
    assert "after 300 s, still waiting on:" in done.stderr and "(restarted)" in done.stderr


def test_the_tls_cafile_reaches_the_certificate_check(tmp_path: Path) -> None:
    host = Host(tmp_path)
    root = tmp_path / "root.pem"
    root.write_text("-----BEGIN CERTIFICATE-----\n")
    done = host.run("--tls-cafile", "root.pem", stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"check-public-tls.sh --cafile {root}" in host.argv()


def test_the_auto_unseal_comes_before_step_4(tmp_path: Path) -> None:
    """Coordinator's ruling Q6 (2026-11-27): right behind step 3.6, so the
    stack step 7 starts is already static-sealed."""
    host, _ = _done_host(tmp_path)
    argv = host.argv().splitlines()
    seal = next(i for i, line in enumerate(argv) if line.startswith("openbao-seal.sh enable"))
    restart = argv.index("docker compose restart openbao")
    migrate = argv.index("docker compose up migrate")
    assert seal < restart < migrate < argv.index("docker compose up -d")


def test_a_failed_step_7_runs_again_with_no_share(tmp_path: Path) -> None:
    """The point of ruling Q6: step 7 is where a real VPS fails (the A record,
    the provider's firewall), and by then OpenBao unseals itself. Run again
    with nothing on stdin, it asks for no share and finishes."""
    host = Host(tmp_path)
    refused_tls = {**ALLOW, "FAKE_RC_CHECK_PUBLIC_TLS": "1"}
    first = host.run(stdin=_saved_and_three(host), env=refused_tls)
    assert first.returncode == 1
    assert "the certificate on :443 is not one a browser accepts" in first.stderr
    assert host.state["bao"]["type"] == "static"
    again = host.run(stdin="")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "skip    auto-unseal: on, and OpenBao is unsealed with its static key" in again.stdout
    assert "openbao-seal.sh" not in host.argv()


# --------------------------------------------------------------------------- #
# Task 3, beyond its brief: Task 2's last review (r1 to r5), and what the
# brief's code needed to say what it does
# --------------------------------------------------------------------------- #


def _another_root_run(host: Host) -> subprocess.CompletedProcess[str]:
    """Mixed state (c): OPENBAO_TOKEN in .env is not this OpenBao's root token."""
    host.write_env(IT_SUITE_VERSION=host.tag, ITS_OPENBAO_TOKEN="s.old", OPENBAO_TOKEN="s.oldroot")
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    host.state["bao"].update(init=True, sealed=False)
    host.state["services"]["openbao"] = {
        "state": "running", "health": "healthy", "exit": 0, "restarts": 0, "id": "c-openbao"
    }  # fmt: skip
    return host.run(env=ALLOW)


def _stale_its_app_run(host: Host) -> subprocess.CompletedProcess[str]:
    _after_init(host)
    host.write_env(IT_SUITE_VERSION=host.tag, ITS_OPENBAO_TOKEN="s.its-app-of-another-boot")
    shares = host.secrets["shares"]
    return host.run(stdin="".join(s + "\n" for s in (*shares[:3], host.secrets["root"])))


def _unknown_token_file_run(host: Host) -> subprocess.CompletedProcess[str]:
    _done(host)
    (host.etc / "gateway-issuer.token").write_text("s.another-openbaos\n")
    return host.run()


def _forgotten_its_app_run(host: Host) -> subprocess.CompletedProcess[str]:
    """Step 3.5's own check: the its-app token just written to .env is not one
    this OpenBao knows (a fault only a stand-in OpenBao can make)."""
    host.state["forget"] = ["its-app"]
    return host.run(stdin=_saved_and_three(host), env=ALLOW)


def _no_public_host_run(host: Host) -> subprocess.CompletedProcess[str]:
    host.write_env(IT_SUITE_VERSION=host.tag, ITS_PUBLIC_HOST="")
    return host.run(env=ALLOW)


def _lookup_unanswered_run(host: Host, lookup: int) -> subprocess.CompletedProcess[str]:
    """A done host run again, with OpenBao gone at its LOOKUP-th token lookup:
    1, OPENBAO_TOKEN (step 3.3); 2, ITS_OPENBAO_TOKEN; 3, the token file (3.4)."""
    _done(host)
    host.state.update(lookups=0, stop_at_lookup=lookup)
    return host.run()


def _late_failure(host: Host, name: str) -> subprocess.CompletedProcess[str]:
    """A first run that fails in steps 4 to 8 as NAME (LATE_FAILURES) says."""
    env, s = dict(ALLOW), host.state
    if name == "no revision file in the checkout":
        for revision in (host.repo / "backend" / "migrations" / "versions").glob("[0-9]*.py"):
            revision.unlink()
    elif name == "migrate exits 1":
        s["oneshot_exit"] = {"migrate": 1}
    elif name == "the schema short of the newest revision":
        s["newest"] = "0247"
    elif name == "postgres-grants exits 1":
        s["oneshot_exit"] = {"postgres-grants": 1}
    elif name == "migrate exits 1, and compose with it":
        s["oneshot_exit"], s["up_returns_exit"] = {"migrate": 1}, True
    elif name == "postgres-grants exits 1, and compose with it":
        s["oneshot_exit"], s["up_returns_exit"] = {"postgres-grants": 1}, True
    elif name == "the RBAC catalog writable after postgres-grants":
        s["pg"]["grants_leave_catalog_writable"] = True
    elif name == "an empty audit key":
        s["files"]["audit"] = "empty"
    elif name == "docker compose up -d fails":
        s["up_d_fails"] = True
    elif name == "a one-shot still running":
        s["still_running"] = ["openbao-init"]
    elif name == ":8443 the Let's Encrypt certificate":
        env["FAKE_GATEWAY_ISSUER"] = "C = US, O = Let's Encrypt, CN = R11"
    elif name == "no setup-code file":
        s["setup"]["code"] = None
    elif name == "the gateway's certificate not issued":
        env["FAKE_DOCKER_FAIL"] = "issue-gateway-cert.py rmm.novieri.com"
    elif name == "a service that stops":
        s["crash"] = ["worker"]
    elif name == "a service that keeps restarting":
        s["restart_step"], s["project"] = 1, "rmm-prod"
    elif name == "the :443 certificate refused":
        env["FAKE_RC_CHECK_PUBLIC_TLS"] = "1"
    elif name == "acme.json not 600":
        s["acme_mode"] = "644"
    elif name == ":8443 not the internal CA's":
        env["FAKE_GATEWAY_HOST"] = "other.example.org"
    elif name == "the api does not answer setup/status":
        s["setup"]["needed"] = None
    elif name == "the api wrote no install code":
        s["setup"]["code"] = ""
    else:
        raise AssertionError(name)
    return host.run(stdin=_saved_and_three(host), env=env)


@pytest.mark.parametrize("lookup", [1, 2, 3], ids=["OPENBAO_TOKEN", "ITS_OPENBAO_TOKEN", "file"])
def test_an_openbao_that_stops_answering_a_lookup_is_not_an_unknown_token(
    tmp_path: Path, lookup: int
) -> None:
    """Review r5 (round 3): a token lookup that fails because OpenBao does not
    answer is no proof the token is another OpenBao's. Told it is, the operator
    would start from zero, or delete a gateway-issuer token file whose token
    stays valid for ten years with no copy anywhere."""
    host = Host(tmp_path)
    done = _lookup_unanswered_run(host, lookup)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FAILED  OpenBao did not answer when " in done.stderr
    for blame in ("mixed state", "sudo rm -f", "down -v", "another first boot", "does not know"):
        assert blame not in done.stderr, blame
    assert (host.etc / "gateway-issuer.token").read_text() == host.secrets["gateway-issuer"] + "\n"
    assert host.env()["ITS_OPENBAO_TOKEN"] == host.secrets["its-app"]


def test_step_7_waits_300_s_at_most(tmp_path: Path) -> None:
    """The brief's loop counted 5 s a round, but a round that saw a restart
    also slept 15: a service restarting for ever held the run 20 minutes, and
    its FAILED said 300 s. The fake `sleep` waits for nothing and is logged."""
    host = Host(tmp_path)
    host.state["restart_step"] = 1
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert "after 300 s, still waiting on:" in done.stderr
    argv = host.argv().splitlines()
    after = argv[argv.index("docker compose up -d") :]
    slept = sum(int(line.split()[1]) for line in after if line.startswith("sleep "))
    assert 280 <= slept <= 300, slept


def test_step_7_says_only_what_it_checked_before_it_waits(tmp_path: Path) -> None:
    """Its `skip` comes before the wait: what it knows then is that `docker
    compose up -d` made no container and replaced none (review M7 of Task 2:
    a line says what was checked, and no more). Up and healthy is the wait's
    to say, and the wait says it by not failing."""
    host, _ = _done_host(tmp_path)
    again = host.run()
    assert again.returncode == 0, again.stdout + again.stderr
    (seven,) = [s for s in lines(again.stdout, "skip") if s.startswith("step 7:")]
    assert seven == "step 7: docker compose up -d created no container and recreated none"


# --------------------------------------------------------------------------- #
# Task 3, fix round 1 (review of Task 3: I1, m1 to m9)
# --------------------------------------------------------------------------- #

STEP_4B = (
    "step 4b: app_user reads devices, cannot write the RBAC catalog (permissions,"
    " role_permissions), and cannot rewrite audit_events or security_events"
)


def test_step_4b_says_what_its_query_proves(tmp_path: Path) -> None:
    """Review I1: the line says what the query checks, run or skipped."""
    host, out = _done_host(tmp_path)
    assert f"  ok      {STEP_4B}" in out.splitlines()
    again = host.run()
    assert f"  skip    {STEP_4B}" in again.stdout.splitlines()


#: Every privilege grant-app-privileges.sql revokes from app_user.
REVOKED = [("permissions", p) for p in ("INSERT", "UPDATE", "DELETE")] + [
    ("role_permissions", p) for p in ("INSERT", "UPDATE", "DELETE")] + [
    (log, p) for log in ("audit_events", "security_events") for p in ("UPDATE", "DELETE")]


def test_a_host_the_grants_did_not_narrow_never_skips_step_4b(tmp_path: Path) -> None:
    """Review I1: after migrate, app_user may write the RBAC catalog --
    init-app-user.sh's default privileges -- and only postgres-grants takes
    that away. A host that kept any one privilege grant-app-privileges.sql
    revokes runs postgres-grants again; it never skips it. (The fake answers
    per privilege, as Postgres does: a new host starts with the catalog
    writable.) One done host, run again once for each privilege."""
    host, _ = _done_host(tmp_path)
    for table, privilege in REVOKED:
        host.state["pg"]["privs"][table].append(privilege)
        again = host.run()
        assert again.returncode == 0, (table, privilege, again.stdout + again.stderr)
        assert "skip    step 4b" not in again.stdout, (table, privilege)
        assert "docker compose up postgres-grants" in lines(again.stdout, "run"), (table, privilege)
        assert f"  ok      {STEP_4B}" in again.stdout.splitlines(), (table, privilege)


def test_a_host_that_cannot_read_devices_never_skips_step_4b(tmp_path: Path) -> None:
    host, _ = _done_host(tmp_path)
    host.state["pg"]["privs"]["devices"] = []
    again = host.run()
    assert "skip    step 4b" not in again.stdout
    assert "docker compose up postgres-grants" in lines(again.stdout, "run")


def test_a_key_the_check_misses_is_never_written_over(tmp_path: Path) -> None:
    """Review m5: the case `set -C` is there for -- a key that exists, which
    the `test -s` before did not see. The fake writes over it unless the
    command carries `set -C`, as the shell would."""
    host = Host(tmp_path)
    host.state["files"]["audit"] = True
    host.state["audit_check_fails"] = True
    done = host.run(stdin=_saved_and_three(host), env=ALLOW)
    assert done.returncode == 1
    assert "FAILED  /var/lib/its/secrets/audit-hmac.key is there, so set -C" in done.stderr
    assert not host.state.get("audit_replaced"), "the audit key was written over"
    assert host.state["files"]["audit"] is True


def test_a_claimed_installation_is_not_sent_to_enter_an_install_code(tmp_path: Path) -> None:
    """Review m9: after `skip step 8: the installation is claimed`, the closing
    notes do not send the operator to type an install code."""
    host, _ = _done_host(tmp_path)
    host.state["setup"]["needed"] = False
    again = host.run(env=ALLOW)
    assert again.returncode == 0, again.stdout + again.stderr
    closing = again.stdout.split("First boot: every step is done.\n", 1)[1]
    assert len(lines(closing, "note")) == 6
    assert "install code" not in closing
    assert "the installation is claimed" in closing


def test_an_openbao_sealed_at_a_lookup_says_it_is_sealed(tmp_path: Path) -> None:
    """Review m7: "did not answer" was also said of an OpenBao that answered,
    sealed. It still judges no token."""
    host = Host(tmp_path)
    _done(host)
    host.state.update(lookups=0, seal_at_lookup=3)
    done = host.run()
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FAILED  OpenBao is sealed, so it could not look up the token in " in done.stderr
    for blame in ("mixed state", "sudo rm -f", "down -v", "did not answer"):
        assert blame not in done.stderr, blame


# --------------------------------------------------------------------------- #
# Step 7's :8443 check reads `openssl x509 -issuer`, whose format changed:
# OpenSSL 3.0 (Ubuntu 24.04, the VPS) prints `issuer=CN = Novieri RMM internal
# CA`; the CI runner's (ubuntu-26.04) prints `issuer=CN=Novieri RMM internal CA`
# (CI run 46068835756, 2026-11-27, where step 7 refused the right certificate).


@pytest.mark.parametrize(
    "issuer", ["CN = Novieri RMM internal CA", "CN=Novieri RMM internal CA"]
)
def test_8443_accepts_the_internal_ca_in_either_openssl_format(
    tmp_path: Path, issuer: str
) -> None:
    host = Host(tmp_path)
    done = host.run(
        stdin=_saved_and_three(host), env={**ALLOW, "FAKE_GATEWAY_ISSUER": issuer}
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok      step 7: :8443 presents the internal CA's certificate" in done.stdout


@pytest.mark.parametrize(
    "issuer",
    [
        "CN = Novieri RMM internal CA 2",
        "CN=Not the Novieri RMM internal CA",
        "C = US, O = Let's Encrypt, CN = R11",
    ],
)
def test_8443_refuses_an_issuer_that_only_contains_the_name(
    tmp_path: Path, issuer: str
) -> None:
    host = Host(tmp_path)
    done = host.run(
        stdin=_saved_and_three(host), env={**ALLOW, "FAKE_GATEWAY_ISSUER": issuer}
    )
    assert done.returncode == 1
    assert "FAILED  :8443 does not present the internal CA's certificate" in done.stderr
