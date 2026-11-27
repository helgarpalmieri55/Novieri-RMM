"""`deploy/scripts/first-boot.sh`, on a stand-in host (design
2026-11-27-despliegue-automatizado §3, §5; `tests/first_boot_support.py`).

The CI's `deploy` job runs the script end to end against a real stack and
Pebble. What only a stand-in can show is pinned here: the refusals -- no
terminal, a mixed state, xtrace -- that a happy run never reaches; that a run
again skips every step, saying so; and that no secret is ever an argument of
any process (every command but bash's builtins goes through a logging shim) or
lands in a file the runbook does not name.
"""

import re
import shutil
import subprocess
from pathlib import Path

from tests.first_boot_support import SCRIPT, Host, lines

ALLOW = {"ITS_FIRST_BOOT_ALLOW_NONTTY": "1"}
RESTART_SKIP = "the restart that proves it ran when the auto-unseal was turned on"


def _saved_and_three(host: Host, picks: tuple[int, ...] = (1, 3, 4)) -> str:
    shares = host.secrets["shares"]
    return "SAVED\n" + "".join(shares[i] + "\n" for i in picks)


def test_the_script_parses() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_it_refuses_to_run_under_xtrace(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.log.write_text("")
    done = subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-x", str(host.deploy / "scripts" / "first-boot.sh")],
        capture_output=True,
        text=True,
        env={"PATH": str(host.bin), "ARGV_LOG": str(host.log)},
    )
    assert done.returncode == 1
    assert "refusing to run under xtrace" in done.stderr
    assert "docker" not in host.argv()


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
    assert [r.split(":")[0].split(",")[0] for r in ran][:5] == [
        "./scripts/new-env.sh --host rmm.novieri.com",
        "./scripts/build-images.sh",
        "docker compose up -d --wait postgres",
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


def test_the_shares_are_printed_to_a_terminal_only(tmp_path: Path) -> None:
    host = Host(tmp_path)
    done = host.run(stdin="")
    assert done.returncode == 1
    assert "the output is not a terminal" in done.stderr
    assert "operator init" not in host.argv()
    assert host.state["bao"]["init"] is False


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


def test_tokens_in_env_and_an_openbao_never_initialised_are_a_mixed_state(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.write_env(IT_SUITE_VERSION=host.tag, ITS_OPENBAO_TOKEN="s.old", OPENBAO_TOKEN="s.oldroot")
    host.state["images"] = [f"itsuite/backend:{host.tag}", f"itsuite/web:{host.tag}"]
    done = host.run(env=ALLOW)
    assert done.returncode == 1
    assert "mixed state: .env holds OpenBao tokens" in done.stderr
    assert "docker compose down -v && rm .env" in done.stderr
    assert "operator init" not in host.argv()


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
