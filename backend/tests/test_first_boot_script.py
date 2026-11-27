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
"""

import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.first_boot_support import SCRIPT, Host, lines

ALLOW = {"ITS_FIRST_BOOT_ALLOW_NONTTY": "1"}
RESTART_SKIP = "the restart that proves it"
SAVED_PROMPT = "Type SAVED once the five shares and the root token are stored off this VPS: "
ROOT_PROMPT = "OpenBao root token, printed by step 3.2 (hidden): "
CLEAR = "\x1b[H\x1b[2J\x1b[3J"


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


def _recipe_removes_the_earlier_boots_files(host: Host, stderr: str) -> None:
    token, key = host.etc / "gateway-issuer.token", host.etc / "openbao-unseal" / "unseal.key"
    assert f"        sudo rm -f {token} {key}" in stderr.splitlines(), stderr
    assert "openbao-seal.sh would reuse it" in stderr


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
    assert f"        sudo rm -f {host.etc}/gateway-issuer.token" in done.stderr.splitlines()
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
    assert "        docker compose down -v && rm .env" in done.stderr.splitlines()
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
    assert f"        sudo rm -f {host.etc}/gateway-issuer.token" in done.stderr.splitlines()


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
    again = host.run(stdin="".join(s + "\n" for s in (shares[0], shares[2], shares[4])))
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
    typed = ("a-typo", shares[0], shares[1], shares[0], shares[1], shares[2])
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
    commands = [line.strip() for line in err.splitlines() if line.startswith("        ")]
    assert commands.count("docker compose exec openbao bao operator unseal -migrate") == 3
    assert (
        "sed -i '/^COMPOSE_FILE=/d;/^OPENBAO_UNSEAL_KEY_FILE=/d;/^OPENBAO_SEAL_STANZA=/d' .env"
        in commands
    )
    assert "docker compose up -d --no-deps openbao" in commands
    assert "sudo env -u COMPOSE_FILE ./scripts/openbao-seal.sh status" in commands
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
    assert "        docker compose up -d --no-deps openbao" in again.stderr.splitlines()


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
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_command_of_a_failed_block_can_be_pasted(tmp_path: Path, scenario: str) -> None:
    """Each line at the command indent is a shell command the operator can
    run as it stands, starting with the `cd` to deploy/, so that they work
    from any folder; prose is at two spaces. `Host.run` raises if not."""
    host = Host(tmp_path)
    done = SCENARIOS[scenario](host)
    assert done.returncode == 1, done.stdout + done.stderr
    block = done.stderr[done.stderr.index("FAILED  ") :].splitlines()
    commands = [line[8:] for line in block if line.startswith("        ")]
    assert commands[0] == f"cd {host.deploy}"
    assert any(c.startswith("./scripts/first-boot.sh") for c in commands), commands
