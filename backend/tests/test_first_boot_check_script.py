"""`deploy/scripts/first-boot-check.sh`, on a stubbed host (design
2026-11-27-despliegue-automatizado §3, §5).

CI's `deploy` job runs it end to end: a real agent, enrolled over :443,
connected over :8443, a job a person asked for, the audit chain, the backup.
What a stub shows that a green run cannot: the refusals (OpenBao sealed, the
installation not claimed, a route not refused, a job refused), the reuse of an
agent left running by a run that was killed outright, the retirement of the
agent on every other way out (a refusal, a failed command, Ctrl-C, TERM, HUP,
a second signal during the cleanup), and that the enrollment token is
never an argument, never printed, and only ever in the agent's 0600 file.
A fake `docker` plays compose and the agent; every other command but bash's
builtins passes through a shim that logs its argv.
"""

import fcntl
import json
import os
import pty
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import termios
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "first-boot-check.sh"
BASH = shutil.which("bash") or "/bin/bash"
SHIMMED = ("cat", "date", "env", "find", "grep", "hostname", "id", "install", "readlink",
           "rm", "sed", "seq", "tail", "tr", "dirname")  # fmt: skip
DEVICE = "0194f3a2-7c1d-7e55-9a0b-3c4d5e6f7a8b"

FAKE_DOCKER = r'''
import json, os, signal, sys
STATE = os.environ["FAKE_STATE"]
s = json.load(open(STATE))
argv = sys.argv[1:]
open(os.environ["ARGV_LOG"], "a").write("docker " + " ".join(argv) + "\n")
rc = 0
def out(text):
    sys.stdout.write(text)
if argv[:3] == ["compose", "exec", "-T"]:
    svc, cmd = argv[3], argv[4:]
    if svc == "openbao":
        rc = s["bao_rc"]
    elif svc == "postgres":
        if s.get("pg_down"):
            sys.stderr.write('service "postgres" is not running\n')
            json.dump(s, open(STATE + ".tmp", "w"))
            os.replace(STATE + ".tmp", STATE)
            sys.exit(1)
        sql = cmd[-1]
        if "user_roles" in sql:
            out(s["admins"])
        elif "from jobs" in sql:
            s["polls"] += 1
            assert "target_id = '%s'" % s["device"] in sql, sql
            scoped = ("issued_at >= date_trunc('second', (select created_at from agents "
                      "where device_id = '%s'))" % s["device"])
            if scoped not in sql and s.get("old_job"):
                out(s["old_job"] + "\n")  # an earlier agent's row, which the query must exclude
            elif os.environ.get("FAKE_JOB_GATE") and not os.path.exists(
                os.environ["FAKE_JOB_GATE"]
            ):
                out("")  # the test has not yet closed the reader
            else:
                out(s["job"] + "\n" if s["polls"] >= s["job_after"] else "")
    elif svc == "api":
        sys.stdin.read()
        out(s["chain"])
elif argv[:2] == ["ps", "-q"]:
    out("c0ffee\n" if s["agent_running"] else "")
elif argv[:3] == ["rm", "-f", "smoke-agent"]:
    if s["agent_running"] and s.get("rm_signal"):
        os.kill(os.getppid(), getattr(signal, "SIG" + s["rm_signal"]))  # a second Ctrl-C
    if s["agent_running"] and s.get("rm_fails"):
        rc = 1
    else:
        s["agent_running"] = False
elif argv[0] == "run" and "golang:1.24-bookworm" in argv:
    s["built"] = True
elif argv[0] == "run" and "busybox:1.36.1" in argv:
    data = next(a.split(":")[0] for a in argv if a.endswith(":/var/lib/novieri-agent"))
    token_file = os.path.join(data, "enroll-token")
    s["token_seen"] = open(token_file).read()
    s["token_mode"] = oct(os.stat(token_file).st_mode & 0o777)
    s["agent_args"] = argv
    s["data_files"] = sorted(os.listdir(data))
    if s.get("run_rc"):
        rc = s["run_rc"]  # the registry refused, say: the token file stays where it was put
    else:
        if not s.get("keep_token"):
            os.remove(token_file)
        s["agent_running"] = True
elif argv[0] == "logs":
    s["token_at_logs"] = os.path.exists(
        os.path.join(os.environ["ITS_SMOKE_AGENT_DIR"], "enroll-token"))
    out("time=1 level=INFO msg=\"novieri-agent starting\" version=dev enrolled=false\n")
    out("time=2 level=INFO msg=enrolling url=https://rmm.novieri.com/enroll/v1\n")
    out("time=3 level=INFO msg=enrolled device_id=%s gateway_url=wss://rmm.novieri.com:8443/agent\n"
        % s["device"])
    s["log_polls"] = s.get("log_polls", 0) + 1
    if s["connects"] and s["log_polls"] >= s.get("connect_after", 1):
        out("time=4 level=INFO msg=\"gateway connected\" url=wss://rmm.novieri.com:8443/agent\n")
json.dump(s, open(STATE + ".tmp", "w"))
os.replace(STATE + ".tmp", STATE)  # a signal may end this process: never leave half a file
sys.exit(rc)
'''

FAKE_CURL = r'''
import os, sys
open(os.environ["ARGV_LOG"], "a").write("curl " + " ".join(sys.argv[1:]) + "\n")
path = sys.argv[-1].split("rmm.novieri.com", 1)[1]
sys.stdout.write(os.environ.get("FAKE_CODE_" + path.strip("/").replace("/", "_").replace("-", "_"),
                                "401"))
'''

FAKE_BACKUP = r'''
import os, sys
name = os.path.basename(sys.argv[0])
open(os.environ["ARGV_LOG"], "a").write(name + " " + " ".join(sys.argv[1:]) + "\n")
if name == "scheduled-backup.sh" and os.environ.get("FAKE_BACKUP_MODE") == "fails":
    sys.stderr.write("backup: pg_dump failed\n")
    sys.exit(3)
if name == "scheduled-backup.sh" and os.environ.get("FAKE_BACKUP_MODE") != "silent":
    state = os.environ["ITS_BACKUP_STATE_DIR"]
    os.makedirs(state, exist_ok=True)
    open(os.path.join(state, "last-success"), "w").write("2026-11-27T10:00:00Z\n")
if name == "install-backup-timer.sh":
    units = os.environ["ITS_SYSTEMD_DIR"]
    os.makedirs(units, exist_ok=True)
    open(os.path.join(units, "its-platform-backup.timer"), "w").write("[Timer]\n")
'''

CHAIN_OK = 'novieri {"ok": true, "count": 34, "broken_at": null}\n'


def real_command(name: str, path: str) -> str | None:
    """The system's own binary, before anything a session put on PATH: a
    wrapper that runs `name` again by name would loop through the shim."""
    for directory in ("/usr/bin", "/bin"):
        candidate = Path(directory) / name
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name, path=path)


def _exe(path: Path, body: str, python: bool = True) -> None:
    path.write_text((f"#!{sys.executable}\n" if python else "") + body.lstrip("\n"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Box:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        deploy = tmp / "repo" / "deploy"
        (deploy / "scripts").mkdir(parents=True)
        self.script = deploy / "scripts" / "first-boot-check.sh"
        self.script.write_bytes(SCRIPT.read_bytes())
        for name in ("install-backup-timer.sh", "scheduled-backup.sh"):
            _exe(deploy / "scripts" / name, FAKE_BACKUP)
        (deploy / ".env").write_text("ITS_PUBLIC_HOST=rmm.novieri.com\n")
        agent = tmp / "repo" / "agent"
        (agent / "cmd" / "agent").mkdir(parents=True)
        (agent / "cmd" / "agent" / "main.go").write_text("package main\n")
        self.agent_data = tmp / "smoke-agent"
        self.backup_state = tmp / "backup-state"
        self.units = tmp / "systemd"
        self.log = tmp / "argv.log"
        self.state_file = tmp / "state.json"
        self.token = "ENROLL-" + secrets.token_urlsafe(24)
        self.state = {
            "bao_rc": 0,
            "admins": "first@novieri.example|org-admin\n",
            "device": DEVICE,
            "job": "job-1|inventory.collect|succeeded|t",
            "job_after": 2,
            "polls": 0,
            "chain": CHAIN_OK,
            "agent_running": False,
            "connects": True,
            "built": False,
        }
        self.bin = tmp / "bin"
        self.bin.mkdir()
        real_path = os.environ["PATH"]
        for name in SHIMMED:
            real = real_command(name, real_path)
            assert real, name
            _exe(
                self.bin / name,
                f'#!/bin/sh\nprintf \'%s %s\\n\' {name} "$*" >> "$ARGV_LOG"\nexec {real} "$@"\n',
                python=False,
            )
        # `sleep` signals the script's whole process group, as a terminal's Ctrl-C does.
        _exe(self.bin / "sleep",
             '#!/bin/sh\n[ -z "$FAKE_SLEEP_SIGNAL" ] ||'
             ' { trap "" INT TERM HUP; kill -s "$FAKE_SLEEP_SIGNAL" -- "-$PPID"; }\nexit 0\n',
             python=False)  # fmt: skip
        _exe(self.bin / "systemctl",
             '#!/bin/sh\nprintf \'systemctl %s\\n\' "$*" >> "$ARGV_LOG"\n'
             '[ "$1" != start ] || exit "${FAKE_SYSTEMCTL_START_RC:-0}"\n'
             '[ "$1" != is-enabled ] || echo disabled\n',
             python=False)  # fmt: skip
        _exe(self.bin / "journalctl",
             '#!/bin/sh\nprintf \'journalctl %s\\n\' "$*" >> "$ARGV_LOG"\n'
             'echo "journal: backup failed"\n',
             python=False)  # fmt: skip
        _exe(self.bin / "sudo", '#!/bin/sh\nprintf \'sudo %s\\n\' "$*" >> "$ARGV_LOG"\nexec "$@"\n',
             python=False)  # fmt: skip
        _exe(self.bin / "docker", FAKE_DOCKER)
        _exe(self.bin / "curl", FAKE_CURL)

    def prepare(self) -> None:
        self.state_file.write_text(json.dumps(self.state))
        self.log.write_text("")

    def env(self, **env: str) -> dict[str, str]:
        return {
            "PATH": str(self.bin),
            "ARGV_LOG": str(self.log),
            "FAKE_STATE": str(self.state_file),
            "ITS_SMOKE_AGENT_DIR": str(self.agent_data),
            "ITS_BACKUP_STATE_DIR": str(self.backup_state),
            "ITS_SYSTEMD_DIR": str(self.units),
            "ITS_NO_SYSTEMCTL": "1",
            **env,
        }

    def run(
        self, *args: str, stdin: str | None = None, shell: tuple[str, ...] = (), **env: str
    ) -> subprocess.CompletedProcess[str]:
        self.prepare()
        done = subprocess.run(
            [BASH, *shell, str(self.script), *args],
            input=self.token + "\n" if stdin is None else stdin,
            capture_output=True,
            text=True,
            env=self.env(**env),
            start_new_session=True,  # its own process group: a signal to it is a terminal's
            timeout=100,
        )
        self.state = json.loads(self.state_file.read_text())
        return done

    def reload(self) -> None:
        self.state = json.loads(self.state_file.read_text())

    def argv(self) -> str:
        return self.log.read_text()


def _ok(out: str) -> list[str]:
    return [line[10:].split(":")[0] for line in out.splitlines() if line.startswith("  ok      ")]


def test_the_script_parses() -> None:
    assert subprocess.run([BASH, "-n", str(SCRIPT)]).returncode == 0


def test_nothing_in_it_traces_or_spills_a_here_string() -> None:
    text = SCRIPT.read_text().splitlines()
    code = "\n".join(line for line in text if not line.lstrip().startswith("#"))
    assert not re.search(r"set -[a-wyz]*x|set -o xtrace|BASH_XTRACEFD", code)
    assert "<<<" not in code
    for heredoc in re.findall(r"<<-?\s*(\S+)", code):
        assert heredoc.startswith("'"), heredoc


def test_a_claimed_installation_passes_every_check(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stderr == "", "a command with no shim, or a failure that was swallowed"
    assert _ok(done.stdout) == [
        "OpenBao answers, unsealed (bao status",
        "step 10", "step 11a", "step 11c", "step 11d", "step 11e", "step 11f", "step 12",
    ]  # fmt: skip
    assert "        job-1|inventory.collect|succeeded|t" in done.stdout
    assert "NOW, in the console: Devices ->" in done.stdout
    assert done.stdout.rstrip().splitlines()[-4] == "First boot is complete."
    assert box.state["built"] is True


def test_the_token_is_only_ever_in_the_agents_0600_file(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.state["token_seen"] == box.token
    assert box.state["token_mode"] == "0o600"
    assert box.token not in box.argv() + done.stdout + done.stderr
    assert not box.agent_data.exists(), "step 11f removes the agent's key and certificate"


def test_the_agent_listens_on_nothing_and_trusts_the_hosts_roots(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert box.run().returncode == 0
    args = " ".join(box.state["agent_args"])
    assert "--network host" in args
    assert "-v /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro" in args
    assert "-e ITS_AGENT_ENROLL_URL=https://rmm.novieri.com" in args
    assert "ITS_AGENT_ENROLL_TOKEN" not in args
    assert "--privileged" not in args and " -p " not in args and "--add-host" not in args


def test_the_ci_dials_an_address_and_trusts_only_the_root_it_gives(tmp_path: Path) -> None:
    box = Box(tmp_path)
    root = tmp_path / "pebble-root.pem"
    root.write_text("-----BEGIN CERTIFICATE-----\n")
    done = box.run("--tls-cafile", str(root), "--connect", "127.0.0.1")
    assert done.returncode == 0, done.stdout + done.stderr
    args = " ".join(box.state["agent_args"])
    assert f"-v {root}:/etc/ssl/certs/ca-certificates.crt:ro" in args
    assert "--add-host rmm.novieri.com:127.0.0.1" in args
    curls = [line for line in box.argv().splitlines() if line.startswith("curl ")]
    assert len(curls) == 4
    for line in curls:
        assert f"--cacert {root}" in line and "--resolve rmm.novieri.com:443:127.0.0.1" in line
        assert "--noproxy *" in line


def test_a_sealed_openbao_stops_it_before_anything(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["bao_rc"] = 2
    done = box.run()
    assert done.returncode == 1 and "OpenBao is sealed" in done.stderr
    assert "psql" not in box.argv()


def test_an_unclaimed_installation_is_sent_to_the_browser(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["admins"] = ""
    done = box.run()
    assert done.returncode == 1
    assert "no staff account holds org-admin" in done.stderr
    assert "docker compose exec api cat /var/lib/its/secrets/setup-code" in done.stderr


def test_a_route_that_is_not_refused_stops_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(FAKE_CODE_api_v1_jobs="404")
    assert done.returncode == 1
    assert "did not answer 401: /api/v1/jobs=404" in done.stderr


def test_a_refused_job_stops_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["job"] = "job-1|inventory.collect|refused|t"
    done = box.run()
    assert done.returncode == 1
    assert "the job ended: job-1|inventory.collect|refused|t" in done.stderr


def test_no_job_in_time_says_what_to_press(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["job_after"] = 10**6
    done = box.run("--wait-job", "1")
    assert done.returncode == 1
    assert "no job a person asked for reached this device in 1 s" in done.stderr
    assert "Run job -> Collect inventory" in done.stderr


def test_an_agent_left_running_is_reused_and_no_token_is_asked(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["agent_running"] = True
    done = box.run(stdin="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "skip    steps 11b and 11c: the throwaway agent smoke-agent is running" in done.stdout
    assert "busybox" not in box.argv()


def test_an_agent_that_never_connects_stops_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["connects"] = False
    done = box.run()
    assert done.returncode == 1
    assert "the agent did not connect to the gateway within 60 s" in done.stderr


def test_a_broken_audit_chain_stops_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["chain"] = CHAIN_OK + 'other {"ok": false, "count": 3, "broken_at": "x"}\n'
    done = box.run()
    assert done.returncode == 1 and "an audit chain is broken" in done.stderr


def test_a_second_run_retires_its_own_agent_and_keeps_the_backup_it_has(tmp_path: Path) -> None:
    """Coordinator's ruling Q8 (2026-11-27): safe to run again. A second whole
    run enrols another throwaway agent and retires it too; the backup it
    finds is kept."""
    box = Box(tmp_path)
    assert box.run().returncode == 0
    again = box.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert "busybox:1.36.1" in box.argv(), "the second run enrols an agent of its own"
    assert box.state["agent_running"] is False and not box.agent_data.exists()
    kept = "skip    step 12: the nightly backup timer is installed, and a backup exists"
    assert kept in again.stdout
    assert "scheduled-backup.sh" not in box.argv()


OPENING = [
    "  note    this enrols a throwaway agent on this VPS (step 11c) and retires it, with its key"
    " (11f).",
    "  note    It is safe to run again: a second run enrols and retires another; the first stays"
    " in the console, offline.",
]


def test_its_first_lines_say_it_enrols_and_retires_a_throwaway_agent(tmp_path: Path) -> None:
    """Ruling Q8: read before anything happens -- on a run that stops at the
    first check too."""
    box = Box(tmp_path)
    assert box.run().stdout.splitlines()[:2] == OPENING
    box.state["bao_rc"] = 2
    stopped = box.run()
    assert stopped.returncode == 1 and stopped.stdout.splitlines()[:2] == OPENING


def test_a_binary_newer_than_its_sources_is_not_built_again(tmp_path: Path) -> None:
    box = Box(tmp_path)
    binary = tmp_path / "repo" / "agent" / "dist" / "novieri-agent"
    binary.parent.mkdir()
    binary.write_text("elf")
    binary.chmod(0o755)
    later = (tmp_path / "repo" / "agent" / "cmd" / "agent" / "main.go").stat().st_mtime + 10
    os.utime(binary, (later, later))
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "skip    step 11c: agent/dist/novieri-agent is newer than every file" in done.stdout
    assert box.state["built"] is False


def _failed(done: subprocess.CompletedProcess[str]) -> str:
    assert done.returncode == 1, done.stdout + done.stderr
    assert done.stderr.count("FAILED") == 1, "a failure is reported once"
    return done.stderr


def _retired(box: Box) -> None:
    """Whatever stopped it, no throwaway agent, token or key is left behind."""
    assert "docker rm -f smoke-agent" in box.argv()
    assert box.state["agent_running"] is False and not box.agent_data.exists()


def test_a_database_that_does_not_answer_says_so_and_how_to_go_on(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["pg_down"] = True
    err = _failed(box.run())
    assert "step 10: the database did not answer" in err
    assert "./scripts/first-boot-check.sh" in err


def test_a_refused_docker_run_is_reported_and_costs_no_token_on_disk(tmp_path: Path) -> None:
    """Exit 125, from Docker Hub's 429 say: not a bare exit with the pasted token left in /tmp."""
    box = Box(tmp_path)
    box.state["run_rc"] = 125
    err = _failed(box.run())
    assert "Step 11c" in err and "exit 125" in err
    assert "It is safe to run again: ./scripts/first-boot-check.sh" in err
    assert not box.agent_data.exists(), "the token file went with the agent's directory"
    assert box.token not in box.argv() + err
    _retired(box)


def test_what_an_earlier_failed_run_left_in_the_data_directory_is_cleared(tmp_path: Path) -> None:
    """The token would be overwritten anyway; an earlier identity in the directory would not."""
    box = Box(tmp_path)
    box.agent_data.mkdir()
    (box.agent_data / "enroll-token").write_text("STALE-TOKEN-OF-A-FAILED-RUN")
    (box.agent_data / "agent-key.pem").write_text("STALE KEY")
    assert box.run().returncode == 0
    assert box.state["token_seen"] == box.token
    assert box.state["data_files"] == ["enroll-token"], box.state["data_files"]


def test_a_token_left_beside_an_agent_that_is_running_is_removed(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["agent_running"] = True
    box.agent_data.mkdir()
    (box.agent_data / "enroll-token").write_text("STALE-TOKEN")
    assert box.run(stdin="").returncode == 0
    assert box.state["token_at_logs"] is False


def test_a_failed_backup_reaches_its_own_message_on_the_ci_path(tmp_path: Path) -> None:
    box = Box(tmp_path)
    err = _failed(box.run(FAKE_BACKUP_MODE="fails"))
    assert "step 12: the first backup failed" in err and "backup: pg_dump failed" in err


def test_a_backup_that_leaves_no_trace_says_so(tmp_path: Path) -> None:
    box = Box(tmp_path)
    err = _failed(box.run(FAKE_BACKUP_MODE="silent"))
    assert "the first backup did not succeed" in err


def test_a_failed_backup_service_shows_its_journal_first(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(ITS_NO_SYSTEMCTL="0", FAKE_SYSTEMCTL_START_RC="1")
    err = _failed(done)
    assert "step 12: the backup service failed (its journal is above)" in err
    assert "journalctl -u its-platform-backup.service --no-pager -n 50" in box.argv()
    assert "journal: backup failed" in done.stdout


def test_a_failure_after_11c_retires_the_agent_and_its_key(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["job"] = "job-1|inventory.collect|refused|t"
    err = _failed(box.run())
    assert "the job ended: job-1|inventory.collect|refused|t" in err
    assert "The throwaway agent was retired" in err
    assert "from deploy/: ./scripts/first-boot-check.sh" in err
    _retired(box)
    box = Box(tmp_path / "again")
    box.state["chain"] = CHAIN_OK + 'other {"ok": false, "count": 3, "broken_at": "x"}\n'
    _failed(box.run())
    _retired(box)


def test_a_timeout_in_11d_retires_the_agent_and_says_what_to_do(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["job_after"] = 10**6
    err = _failed(box.run("--wait-job", "1"))
    assert "make a new enrollment token (step 11b)" in err
    _retired(box)


def test_ctrl_c_terminate_and_hangup_retire_the_agent_too(tmp_path: Path) -> None:
    """Delivered to the whole process group, as a terminal and a dropped SSH session do."""
    for signal_name, code in (("INT", 130), ("TERM", 143), ("HUP", 129)):
        box = Box(tmp_path / signal_name)
        box.state["job_after"] = 10**6
        err = _failed(box.run("--wait-job", "30", FAKE_SLEEP_SIGNAL=signal_name))
        assert "Step 11d" in err and f"interrupted (exit {code})" in err, err
        assert "The throwaway agent was retired" in err
        assert box.state["polls"] <= 2, "it stopped at the signal, not at the timeout"
        _retired(box)


def test_a_second_signal_during_the_cleanup_loses_neither_message_nor_cleanup(
    tmp_path: Path,
) -> None:
    for signal_name in ("INT", "HUP"):
        box = Box(tmp_path / signal_name)
        box.state["job"] = "job-1|inventory.collect|refused|t"
        box.state["rm_signal"] = signal_name
        err = _failed(box.run())
        assert "the job ended: job-1|inventory.collect|refused|t" in err
        assert "The throwaway agent was retired" in err
        _retired(box)
    # and in the cleanup of a signal: the first one is a Ctrl-C, the second a hangup
    box = Box(tmp_path / "both")
    box.state["job_after"] = 10**6
    box.state["rm_signal"] = "HUP"
    err = _failed(box.run("--wait-job", "30", FAKE_SLEEP_SIGNAL="INT"))
    assert "interrupted (exit 130)" in err and "The throwaway agent was retired" in err
    _retired(box)


def test_an_agent_that_cannot_be_removed_is_named_with_the_command_to_do_it(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path)
    box.state["job"] = "job-1|inventory.collect|refused|t"
    box.state["rm_fails"] = True
    err = _failed(box.run())
    assert "the job ended: job-1|inventory.collect|refused|t" in err
    assert "could NOT be retired" in err
    assert "docker rm -f smoke-agent && rm -rf" in err
    assert "agent: " in err, "its last log lines were taken before the removal was tried"


def test_a_refusal_shows_the_agents_last_log_lines_before_removing_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["job"] = "job-1|inventory.collect|refused|t"
    err = _failed(box.run())
    assert "        agent: time=3 level=INFO msg=enrolled" in err
    argv = box.argv()
    assert argv.index("docker logs --tail 20 smoke-agent") < argv.rindex("docker rm -f smoke-agent")
    assert err.index("the job ended") < err.index("agent: time=3") < err.index("was retired")


def test_step_11f_failures_give_the_command_to_go_on(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["rm_fails"] = True
    err = _failed(box.run())
    assert "step 11f: could not remove smoke-agent" in err
    assert "then: ./scripts/first-boot-check.sh" in err


def test_a_refused_row_of_an_earlier_agent_does_not_stop_a_fresh_run(tmp_path: Path) -> None:
    """11d looks only at jobs issued after this device enrolled."""
    box = Box(tmp_path)
    box.state["old_job"] = "job-1|inventory.collect|refused|t"
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_job_that_succeeded_unsigned_fails_at_once_naming_invariant_1(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["job"] = "0194-job-id|inventory.collect|succeeded|f"
    box.state["job_after"] = 1
    err = _failed(box.run("--wait-job", "30"))
    assert "INVARIANT 1 BROKEN" in err and "no server signature" in err
    assert "0194-job-id" in err, "the job id is the evidence"
    assert "Do not put this host in service" in err and "do not run this check again" in err
    assert "Keep the audit log" in err and "raise it" in err
    assert "A re-run enrols" not in err and "ready (step 11b)" not in err, "it invites no re-run"
    assert "no job a person asked for" not in err
    assert box.state["polls"] == 1, "no waiting"
    assert "agent: time=3" in err, "the agent's log is kept as evidence"
    argv = box.argv()
    assert argv.index("docker logs --tail 20 smoke-agent") < argv.rindex("docker rm -f smoke-agent")
    _retired(box)


def test_the_wait_for_the_gateway_polls_until_the_agent_connects(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["connect_after"] = 4
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.argv().count("docker logs smoke-agent") >= 4


def test_an_agent_that_never_connects_is_polled_for_its_whole_wait(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["connects"] = False
    assert box.run().returncode == 1
    assert box.argv().count("docker logs smoke-agent") >= 30
    _retired(box)


def test_xtrace_is_refused_before_the_token_is_read(tmp_path: Path) -> None:
    for kind in ("-x", "SHELLOPTS"):
        box = Box(tmp_path / ("a" if kind == "-x" else "b"))
        if kind == "-x":
            done = box.run(shell=("-x",))
        else:
            done = box.run(SHELLOPTS="xtrace")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "refusing to run under xtrace" in done.stderr
        assert box.token not in done.stdout + done.stderr + box.argv()
        assert "busybox" not in box.argv() and "docker" not in box.argv()


def test_a_token_with_no_final_newline_or_with_a_crlf_and_spaces_is_taken(tmp_path: Path) -> None:
    box = Box(tmp_path / "a")
    assert box.run(stdin=box.token).returncode == 0
    assert box.state["token_seen"] == box.token
    box = Box(tmp_path / "b")
    assert box.run(stdin=f"  {box.token} \r\n").returncode == 0
    assert box.state["token_seen"] == box.token


def test_no_token_on_the_input_stops_it_with_the_command_to_go_on(tmp_path: Path) -> None:
    box = Box(tmp_path)
    err = _failed(box.run(stdin=""))
    assert "the input ended before the enrollment token" in err


def test_an_empty_chain_is_not_a_verified_one(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["chain"] = ""
    err = _failed(box.run())
    assert "no organization's audit chain verifies" in err
    _retired(box)


def test_an_agent_that_kept_its_token_stops_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["keep_token"] = True
    err = _failed(box.run())
    assert "the agent kept its enrollment token after enrolling" in err
    assert not box.agent_data.exists()
    _retired(box)


def test_the_console_instruction_names_the_device_id(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run()
    assert f"the one that is online (device id {DEVICE})" in done.stdout


def test_an_openbao_that_does_not_answer_says_how_to_go_on(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.state["bao_rc"] = 1
    err = _failed(box.run())
    assert "OpenBao does not answer" in err and "./scripts/first-boot-check.sh" in err


def _wait_for(fd: int, marker: bytes) -> bytes:
    seen = b""
    while marker not in seen:
        chunk = os.read(fd, 4096)
        assert chunk, seen
        seen += chunk
    return seen


def _hangup_at_11d(box: Box, kind: str) -> int:
    """Start the script waiting at 11d, then lose its output the way a dropped SSH session
    does. `kind`: `sigpipe` (no HUP: the reader goes, and the script's next write is a SIGPIPE),
    `pty` (the controlling terminal's master is closed), `pipe` (the reader of its
    stdout and stderr has gone, then a HUP) or `closed` (stderr closed, then a HUP)."""
    box.state["job_after"] = 1 if kind.startswith("sigpipe") else 10**6
    gate = box.tmp / "job-gate"
    box.prepare()
    # For `sigpipe` the job appears only once the reader has gone: its row is the write that
    # meets the broken pipe, with no timing to win.
    env = box.env(FAKE_JOB_GATE=str(gate)) if kind.startswith("sigpipe") else box.env()
    popen: dict[str, object] = {}
    master = slave = -1
    if kind == "pty":
        master, slave = pty.openpty()

        def own_terminal() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        popen = {"stdout": slave, "stderr": slave, "preexec_fn": own_terminal}
        reader = master
    else:
        reader, writer = os.pipe()
        popen = {"stdout": writer, "start_new_session": True}
        if kind == "sigpipe-stdout":  # only stdout is the broken pipe: stderr can still say so
            popen["stderr"] = (box.tmp / "stderr.txt").open("w")
        elif kind in ("pipe", "sigpipe"):
            popen["stderr"] = writer
        else:
            popen["stderr"] = subprocess.DEVNULL
            popen["preexec_fn"] = lambda: os.close(2)
    proc = subprocess.Popen(  # noqa: S603
        [BASH, str(box.script), "--wait-job", "60"], stdin=subprocess.PIPE, env=env, **popen
    )
    assert proc.stdin is not None
    proc.stdin.write((box.token + "\n").encode())
    proc.stdin.close()
    if kind != "pty":
        os.close(writer)
    _wait_for(reader, b"Waiting up to")
    os.close(reader)  # the terminal's master, or the only reader of the pipe: gone
    gate.write_text("")
    if kind == "pty":
        os.close(slave)
    elif not kind.startswith("sigpipe"):
        os.killpg(proc.pid, signal.SIGHUP)
    try:
        proc.wait(timeout=30)
    finally:
        proc.kill()
    box.reload()
    return proc.returncode


def test_a_dropped_ssh_session_still_retires_the_agent_on_a_pty(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert _hangup_at_11d(box, "pty") == 1
    _retired(box)
    assert box.argv().count("docker rm -f smoke-agent") >= 2, "the retirement ran"


def test_a_hangup_after_the_reader_of_its_output_has_gone_still_retires_it(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert _hangup_at_11d(box, "pipe") == 1
    _retired(box)
    assert box.argv().count("docker rm -f smoke-agent") >= 2


def test_a_hangup_with_stderr_closed_still_retires_the_agent(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert _hangup_at_11d(box, "closed") == 1
    _retired(box)
    assert box.argv().count("docker rm -f smoke-agent") >= 2


def test_a_write_to_a_pipe_nobody_reads_still_retires_the_agent(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert _hangup_at_11d(box, "sigpipe") == 1
    _retired(box)
    assert box.argv().count("docker rm -f smoke-agent") >= 2


def test_a_closed_stdout_is_reported_as_an_interruption(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert _hangup_at_11d(box, "sigpipe-stdout") == 1
    err = (tmp_path / "stderr.txt").read_text()
    assert "interrupted (its output was closed) (exit 141)" in err
    assert "a command failed" not in err
    _retired(box)
