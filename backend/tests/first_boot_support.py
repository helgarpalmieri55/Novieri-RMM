"""A stand-in host for `deploy/scripts/first-boot.sh` (design
2026-11-27-despliegue-automatizado §5): a fake `docker` that plays compose,
OpenBao and Postgres from one JSON state file, stubs for the sibling scripts
and for `sudo`, `systemctl` and `openssl`, and a shim in front of every other
command that logs its argv -- the `ps` a test reads to prove no secret was
ever an argument.

`Host(tmp_path)` builds it: a scratch checkout (`repo/deploy/scripts/` with the
real first-boot.sh, a git commit, the migration directory), `etc/` and
`systemd/` for ITS_ETC_DIR and ITS_SYSTEMD_DIR, and a PATH of nothing but the
shims and the stubs, so a command the script uses without the test knowing
fails with "command not found" instead of passing unseen. The secrets the
fake OpenBao hands out are fixed per host and listed in `Host.secrets`.

Fix round 1 (review of 2026-11-27, findings I3, M3, M6 and M9) widened what a
run leaves for a test to read, all of it under `harness/`, outside the host's
own files:
- every child's environment (`environ.log`): each shim, stub and fake writes
  the environment it was given, so a secret a caller's `export` would carry
  into a child is seen;
- what was written, as it was written (`captures/`): `tee`, `rm`, `mv`, `cat >`
  and `mktemp` are shims that keep a copy of what they write, delete or create,
  so a temporary file that is gone by the end of the run is still read;
- the files the fakes themselves write (`writes.log`).
`Host.leaks()` reads all of it. `Host.run` also checks that a failure prints
one FAILED block whose command lines can be pasted (`failed_block_problems`),
and `Host.run_pty` runs the script under a pseudo-terminal.
"""

from __future__ import annotations

import json
import os
import pty
import re
import secrets
import select
import shlex
import shutil
import stat
import subprocess
import sys
import termios
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "first-boot.sh"
PY = sys.executable
BASH = shutil.which("bash") or "/bin/bash"

#: Every external command the script, its stubs and bash reach, each behind a
#: logging shim. A new one in the script fails a test until it is listed.
SHIMMED = (
    "cat", "chmod", "cut", "diff", "dirname", "env", "find", "git", "grep", "head",
    "install", "mkdir", "mktemp", "mv", "readlink", "rm", "sed", "sort", "stat", "tail",
    "tee", "test", "tr",
)  # fmt: skip

#: The shims that also keep a copy of what they write, delete or create (M9).
CAPTURING = ("cat", "mktemp", "mv", "rm", "tee")

#: What a command line of a FAILED block may start with: the operator pastes
#: these (CLAUDE.md, "A copy-pasteable command contains no placeholders").
COMMAND_WORDS = (
    "cd", "chmod", "docker", "env", "grep", "rm", "sed", "sudo", "xargs",
    "./scripts/first-boot.sh", "./scripts/openbao-seal.sh", "./scripts/prepare-host.sh",
)  # fmt: skip

#: Where each secret may be written: where the runbook writes it.
ALLOWED = {
    "root": ["repo/deploy/.env"],
    "its-app": ["repo/deploy/.env"],
    "gateway-issuer": ["etc/gateway-issuer.token"],
}

#: The long-running services `docker compose up -d` starts, and the one-shots.
LONG = ("api", "backup-replicator", "gateway", "minio", "openbao", "postgres", "redis",
        "traefik", "web", "worker")  # fmt: skip
ONESHOTS = ("migrate", "postgres-grants", "openbao-init")
HEALTHCHECKED = {"api", "gateway", "minio", "openbao", "postgres", "web", "worker"}
NEWEST_REVISION = "0248"

#: First lines of every Python stand-in: it writes the environment it was given
#: and, through `wrote()`, each file it writes.
FAKE_PRELUDE = r'''
import os as _os, sys as _sys
_HARNESS = _os.environ["HARNESS_DIR"]
with open(_os.path.join(_HARNESS, "environ.log"), "a") as _log:
    _log.write("== " + _os.path.basename(_sys.argv[0]) + "\n")
    _log.write("".join("%s=%s\n" % kv for kv in _os.environ.items()))


def wrote(path):
    with open(_os.path.join(_HARNESS, "writes.log"), "a") as log:
        log.write(_os.path.basename(_sys.argv[0]) + " " + _os.path.abspath(path) + "\n")
'''

FAKE_DOCKER = r'''
import json, os, sys

STATE = os.environ["FAKE_STATE"]
with open(STATE) as fh:
    s = json.load(fh)
argv = sys.argv[1:]
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("docker " + " ".join(argv) + "\n")
    log.write("  COMPOSE_FILE=" + os.environ.get("COMPOSE_FILE", "") + "\n")
# A failure to order: FAKE_DOCKER_FAIL is a piece of the command line.
if os.environ.get("FAKE_DOCKER_FAIL") and os.environ["FAKE_DOCKER_FAIL"] in " ".join(argv):
    sys.stderr.write("fake docker: failing on purpose\n")
    sys.exit(17)


def save():
    with open(STATE, "w") as fh:
        json.dump(s, fh)


def out(text):
    sys.stdout.write(text)


def svc(name):
    return s["services"].setdefault(name, {"state": "absent", "health": "", "exit": 0,
                                           "restarts": 0, "id": "c" + str(len(s["services"]))})


def run_long(name):
    service = svc(name)
    if service["state"] != "running":
        service["id"] = "c%d-%s" % (s["serial"], name)
        s["serial"] += 1
    service["state"] = "running"
    service["health"] = "healthy" if name in s["healthchecked"] else ""


def run_once(name):
    service = svc(name)
    service["id"] = "c%d-%s" % (s["serial"], name)
    s["serial"] += 1
    service["state"] = "exited"
    service["exit"] = s["oneshot_exit"].get(name, 0)
    if name == "migrate" and service["exit"] == 0:
        s["pg"]["alembic"] = s["newest"]
    if name == "postgres-grants" and service["exit"] == 0:
        s["pg"]["narrow"] = "t"


def bao_json():
    b = s["bao"]
    return json.dumps({"type": b["type"], "initialized": b["init"], "sealed": b["sealed"],
                       "t": 3 if b["init"] else 0, "n": 5 if b["init"] else 0,
                       "migration": b.get("migrating", False)}, indent=2) + "\n"


def bao(args, token):
    b = s["bao"]
    if args[:2] == ["token", "lookup"]:
        if not b["init"] or b["sealed"]:
            sys.stderr.write("Vault is sealed\n")
            return 2
        if token == b["root"]:
            policies = ["root"]
        elif token in b["tokens"]:
            policies = ["default", b["tokens"][token]]
        else:
            sys.stderr.write("permission denied\n")
            return 2
        out(json.dumps({"data": {"id": token, "policies": policies}, "policies": policies},
                       indent=2) + "\n")
        return 0
    if token != b["root"]:
        sys.stderr.write("permission denied\n")
        return 2
    if args[:2] == ["policy", "write"]:
        b["policies"].append(args[2])
        out("Success! Uploaded policy: %s\n" % args[2])
        return 0
    if args[:2] == ["auth", "tune"]:
        out("Success! Tuned the auth method at: token/\n")
        return 0
    if args[:2] == ["token", "create"]:
        policy = next(a.split("=", 1)[1] for a in args if a.startswith("-policy="))
        value = s["issue"][policy]
        b["tokens"][value] = policy
        out(value + "\n")
        return 0
    if args[:2] == ["operator", "seal"]:
        b["sealed"] = True
        b["progress"] = []
        out("Success! Vault is sealed.\n")
        return 0
    return 1


def compose(args):
    sub, rest = args[0], args[1:]
    if sub == "ps":
        names = [a for a in rest if not a.startswith("-") and "{{" not in a]
        fmt = rest[rest.index("--format") + 1] if "--format" in rest else None
        shown = [(n, v) for n, v in sorted(s["services"].items())
                 if v["state"] != "absent" and (not names or n in names)]
        if "--status" in rest:
            shown = [(n, v) for n, v in shown if v["state"] == "running"]
        for name, v in shown:
            if fmt is None:
                out(v["id"] + "\n")
            elif fmt.startswith("table"):
                out("%s\t%s\n" % (name, v["state"]))
            else:
                line = (fmt.replace("{{.Service}}", name).replace("{{.State}}", v["state"])
                        .replace("{{.Health}}", v["health"])
                        .replace("{{.ExitCode}}", str(v["exit"])))
                out(line + "\n")
        return 0
    if sub == "up":
        names = [a for a in rest if not a.startswith("-") and not a.isdigit()]
        if not names:
            for name in s["long"]:
                run_long(name)
            for name in s["oneshots"]:
                if svc(name)["state"] != "exited" or svc(name)["exit"] != 0:
                    run_once(name)
            return 0
        for name in names:
            (run_once if name in s["oneshots"] else run_long)(name)
        return 0
    if sub == "restart":
        for name in rest:
            svc(name)["restarts"] += 1
            if name == "openbao":
                s["bao"]["sealed"] = s["bao"]["type"] != "static"
        return 0
    if sub == "exec":
        rest = [a for a in rest if a != "-T"]
        name, cmd = rest[0], rest[1:]
        if svc(name)["state"] != "running":
            sys.stderr.write('service "%s" is not running\n' % name)
            return 1
        if name == "openbao":
            b = s["bao"]
            if cmd[:2] == ["bao", "status"]:
                out(bao_json())
                return 2 if b["sealed"] else 0
            if cmd[:3] == ["bao", "operator", "init"]:
                if b["init"]:
                    return 2
                b["init"] = True
                for i, share in enumerate(b["shares"], 1):
                    out("Unseal Key %d: %s\n" % (i, share))
                out("\nInitial Root Token: %s\n\nVault initialized with 5 key shares\n" % b["root"])
                return 0
            if cmd[:3] == ["bao", "write", "-format=json"] and cmd[3] == "sys/unseal":
                body = json.loads(sys.stdin.read())
                if body.get("reset"):
                    b["progress"] = []
                    return 0
                # An OpenBao that stops answering part-way through a round (M5).
                b["keys_seen"] = b.get("keys_seen", 0) + 1
                if b.get("stop_after_keys") and b["keys_seen"] >= b["stop_after_keys"]:
                    svc("openbao")["state"] = "exited"
                    return 0
                if b.get("migrating") and not body.get("migrate"):
                    sys.stderr.write("migrate option not provided and seal migration is pending\n")
                    return 2
                key = body["key"]
                if key not in b["progress"]:
                    b["progress"].append(key)
                if len(b["progress"]) >= 3:
                    good = all(k in b["shares"] for k in b["progress"])
                    b["progress"] = []
                    if not good:
                        return 2
                    b["sealed"] = False
                    if b.get("migrating"):
                        b["migrating"] = False
                        b["type"] = "static"
                return 0
            if cmd[:2] == ["sh", "-c"]:
                token = sys.stdin.readline().strip()
                return bao(cmd[5:], token)
            if cmd[:2] == ["id", "-u"] or cmd[:2] == ["id", "-g"]:
                out("100\n")
                return 0
        if name == "postgres" and cmd[0] == "psql":
            sql = cmd[-1]
            if "pg_roles" in sql:
                out("%d\n" % s["pg"]["roles"])
            elif "alembic_version" in sql:
                if not s["pg"]["alembic"]:
                    sys.stderr.write('relation "alembic_version" does not exist\n')
                    return 1
                out(s["pg"]["alembic"] + "\n")
            elif "has_table_privilege" in sql:
                out(s["pg"]["narrow"] + "\n")
            return 0
        if name == "traefik" and cmd[0] == "stat":
            out(s["acme_mode"] + "\n")
            return 0
        if name == "api" and cmd[0] == "python":
            if "setup/status" in cmd[-1]:
                out(("True" if s["setup"]["needed"] else "False") + "\n")
            else:
                out("30 30\n")
            return 0
        if name == "api" and cmd[0] == "cat":
            out(s["setup"]["code"] + "\n")
            return 0
        return 0
    if sub == "run":
        script = rest[-1]
        if "--entrypoint" in rest and rest[rest.index("--entrypoint") + 1] == "python":
            token = sys.stdin.readline().strip()
            if s["bao"]["tokens"].get(token) != "gateway-issuer":
                sys.stderr.write("403 from OpenBao\n")
                return 1
            s["files"]["gateway"] = True
            out("gateway.crt  /var/lib/its/tls/gateway.crt  (CN=%s, 365d)\n" % rest[-1])
            return 0
        if "audit-hmac.key" in script:
            if script.startswith("test -s"):
                return 0 if s["files"]["audit"] else 1
            if script.startswith("set -C"):
                if s["files"]["audit"]:
                    sys.stderr.write("sh: cannot create audit-hmac.key: File exists\n")
                    return 1
                s["files"]["audit"] = True
                return 0
            out("-rw------- 1 its its 32 Nov 27 10:00 /var/lib/its/secrets/audit-hmac.key\n")
            return 0
        if "gateway.crt" in script:
            return 0 if s["files"]["gateway"] else 1
        return 0
    return 0


rc = 0
if argv[:2] == ["volume", "ls"]:
    if s.get("volume_ls_error"):
        sys.stderr.write(s["volume_ls_error"] + "\n")
        rc = 1
    else:
        out("".join(v + "\n" for v in s["volumes"]))
elif argv[:2] == ["image", "inspect"]:
    rc = 0 if all(ref in s["images"] for ref in argv[2:]) else 1
elif argv[0] == "inspect":
    fmt = argv[argv.index("-f") + 1]
    by_id = {v["id"]: (n, v) for n, v in s["services"].items()}
    for ident in [a for a in argv[1:] if a not in ("-f", fmt)]:
        name, v = by_id[ident]
        if "ExitCode" in fmt:
            out("%d\n" % v["exit"])
        else:
            out("/it-suite-%s-1 %d\n" % (name, v["restarts"] + s["restart_drift"]))
    s["restart_drift"] += s["restart_step"]
elif argv[0] == "compose":
    rc = compose(argv[1:])
save()
sys.exit(rc)
'''

#: The sibling scripts first-boot.sh runs, stood in for: their own behaviour
#: is their own tests' (test_new_env_script.py, test_preflight_script.py,
#: test_check_public_tls.py, test_openbao_seal_script.py).
FAKE_NEW_ENV = r'''
import os, shutil, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("new-env.sh " + " ".join(sys.argv[1:]) + "\n")
os.umask(0o077)
shutil.copyfile(os.environ["FAKE_ENV_TEMPLATE"], ".env")
os.chmod(".env", 0o600)
wrote(".env")
print("wrote .env (mode 600)")
'''

FAKE_BUILD_IMAGES = r'''
import json, os, subprocess
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("build-images.sh\n")
tag = subprocess.run(["git", "-C", "..", "rev-parse", "--short", "HEAD"],
                     capture_output=True, text=True, check=True).stdout.strip()
with open(os.environ["FAKE_STATE"]) as fh:
    s = json.load(fh)
s["images"] = ["itsuite/backend:" + tag, "itsuite/web:" + tag]
with open(os.environ["FAKE_STATE"], "w") as fh:
    json.dump(s, fh)
print("Recent tags:\n      itsuite/backend:" + tag)
'''

FAKE_SIMPLE = r'''
import os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write(name + " " + " ".join(sys.argv[1:]) + "\n")
print("  ok      %s (stub)" % name)
sys.exit(int(os.environ.get("FAKE_RC_" + name.split(".")[0].replace("-", "_").upper(), "0")))
'''

#: openbao-seal.sh enable, in the order the real one works (deploy/scripts/
#: openbao-seal.sh, cmd_enable): the key file, then COMPOSE_FILE and
#: OPENBAO_UNSEAL_KEY_FILE into .env and OpenBao recreated in seal-migration
#: mode, and only then the shares, one per stdin line, up to six. Shares that
#: never make three good ones leave it there, half-way, as the real one does.
#: FAKE_SEAL_FAIL=1 fails before anything changes; =midway right after .env.
FAKE_OPENBAO_SEAL = r'''
import json, os, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("openbao-seal.sh " + " ".join(sys.argv[1:]) + "\n")
    log.write("  COMPOSE_FILE=" + os.environ.get("COMPOSE_FILE", "") + "\n")
with open(os.environ["FAKE_STATE"]) as fh:
    s = json.load(fh)
given = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]


def failed(text):
    sys.stderr.write("\n\033[31mFAILED\033[0m  %s\n" % text)
    sys.stderr.write("  WARNING  Stopped part-way. (fake recover_help)\n")
    with open(os.environ["FAKE_STATE"], "w") as fh:
        json.dump(s, fh)
    sys.exit(1)


if os.environ.get("FAKE_SEAL_FAIL") == "1":
    failed("OpenBao did not come back")
key = sys.argv[sys.argv.index("--key-file") + 1]
os.makedirs(os.path.dirname(key), exist_ok=True)
with open(key, "wb") as fh:
    fh.write(os.urandom(32))
wrote(key)
lines = [l for l in open(".env").read().splitlines()
         if not l.startswith(("COMPOSE_FILE=", "OPENBAO_UNSEAL_KEY_FILE="))]
lines += ["COMPOSE_FILE=docker-compose.yml:openbao/auto-unseal.yml",
          "OPENBAO_UNSEAL_KEY_FILE=" + key]
open(".env", "w").write("\n".join(lines) + "\n")
wrote(".env")
b = s["bao"]
b.update(sealed=True, migrating=True, progress=[])
if os.environ.get("FAKE_SEAL_FAIL") == "midway":
    failed("OpenBao did not come back")
s["seal_stdin"] = len(given)
progress, tries = [], 0
for share in given:
    tries += 1
    if tries > 6:
        failed("still sealed after 6 shares -- a share was wrong or from another install.")
    if share not in progress:
        progress.append(share)
    if len(progress) == 3:
        if all(p in b["shares"] for p in progress):
            break
        progress = []
else:
    failed("stdin ended before OpenBao unsealed.")
b.update(type="static", sealed=False, migrating=False)
with open(os.environ["FAKE_STATE"], "w") as fh:
    json.dump(s, fh)
print("Auto-unseal is ON")
'''

FAKE_SYSTEMCTL = r'''
import os, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("systemctl " + " ".join(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["is-enabled"]:
    print("disabled")
'''

FAKE_OPENSSL = r'''
import os, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("openssl " + " ".join(sys.argv[1:]) + "\n")
sys.stdin.read()
if sys.argv[1] == "x509":
    print("issuer=CN = Novieri RMM internal CA")
    print("X509v3 Subject Alternative Name: ")
    print("    DNS:" + os.environ.get("FAKE_GATEWAY_HOST", "rmm.novieri.com"))
'''

#: tee, rm, mv, cat > and mktemp: the real command, and a copy of what it
#: writes, deletes or creates under harness/captures/ (M9). A temporary file is
#: read there even when nothing of it is left at the end of the run.
CAPTURE_SHIM = r'''
import os, stat, subprocess, sys
NAME, REAL = %(name)r, %(real)r
args = sys.argv[1:]
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write(NAME + " " + " ".join(args) + "\n")
captures = os.path.join(_HARNESS, "captures")
os.makedirs(captures, exist_ok=True)


def keep(kind, target, data):
    base = os.path.join(captures, "%%d-%%d" %% (os.getpid(), len(os.listdir(captures))))
    with open(base + ".data", "wb") as fh:
        fh.write(data)
    with open(base + ".what", "w") as fh:
        fh.write(kind + "\n" + os.path.abspath(target) + "\n")


def files_under(path):
    if os.path.isfile(path):
        return [path]
    return [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]


def read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return b""


paths = [a for a in args if not a.startswith("-")]
if NAME == "tee":
    data = sys.stdin.buffer.read()
    for p in paths:
        keep("tee", p, data)
    sys.exit(subprocess.run([REAL, *args], input=data).returncode)
if NAME == "rm":
    for p in paths:
        for f in files_under(p):
            keep("rm", f, read(f))
elif NAME == "mv" and len(paths) >= 2:
    for p in paths[:-1]:
        for f in files_under(p):
            keep("mv", paths[-1], read(f))
elif NAME == "cat" and stat.S_ISREG(os.fstat(1).st_mode):
    target = os.readlink("/proc/self/fd/1")
    done = subprocess.run([REAL, *args], stdout=subprocess.PIPE)
    view = memoryview(done.stdout)
    while view:
        view = view[os.write(1, view):]
    keep("cat >", target, done.stdout)
    sys.exit(done.returncode)
elif NAME == "mktemp":
    done = subprocess.run([REAL, *args], stdout=subprocess.PIPE, text=True)
    sys.stdout.write(done.stdout)
    with open(os.path.join(_HARNESS, "writes.log"), "a") as log:
        log.write("mktemp " + done.stdout.strip() + "\n")
    sys.exit(done.returncode)
os.execv(REAL, [REAL, *args])
'''


def real_command(name: str, path: str) -> str | None:
    """The system's own binary, before anything a session put on PATH: a
    wrapper that runs `name` again by name would loop through the shim."""
    for directory in ("/usr/bin", "/bin"):
        candidate = Path(directory) / name
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name, path=path)


def _exe(path: Path, body: str, python: bool = True) -> None:
    path.write_text((f"#!{PY}\n" if python else "") + body.lstrip("\n"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake(path: Path, body: str) -> None:
    """A Python stand-in that writes its environment and its files (M3, M9)."""
    _exe(path, FAKE_PRELUDE.lstrip("\n") + body)


def _secret() -> str:
    return secrets.token_urlsafe(32)


def _wait_echo_off(fd: int, limit: float = 2.0) -> None:
    """bash prints a `read -s` prompt before it turns the echo off: a reply
    written in between would be echoed by the terminal, not by the script."""
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if not termios.tcgetattr(fd)[3] & termios.ECHO:
            return
        time.sleep(0.01)


_PLACEHOLDER = re.compile(r"<[A-Za-z][^<>]*>")


def failed_block_problems(stderr: str, deploy: Path) -> list[str]:
    """What is wrong with the FAILED block of STDERR, if it has one (M4, M6, m3):
    - more than one FAILED line;
    - a line after it that is neither a command, at the eight-space indent,
      nor prose printed as a comment (`  # ...`), nor the blank line between
      two ways on;
    - more than one way on without the `# run ONE of the following` line
      before them, or that line with a single way;
    - a way on whose first command is not the `cd` to deploy/;
    - a command line that is not a shell command the operator can paste, from
      COMMAND_WORDS, with no placeholder;
    - a way on in which a line that may read the terminal -- sudo, a hidden
      prompt, first-boot.sh itself -- is not the last line: pasted whole, the
      lines after it would be read as a password or a share. So a way on runs
      first-boot.sh once at most, and last."""
    text = stderr.splitlines()
    starts = [i for i, line in enumerate(text) if line.startswith("FAILED  ")]
    if not starts:
        return []
    problems = [f"{len(starts)} FAILED lines"] if len(starts) > 1 else []
    groups: list[list[str]] = [[]]
    for line in text[starts[0] + 1 :]:
        if not line.strip():
            groups.append([])
        elif (line.startswith("        ") and line[8:9] != " ") or line.startswith("  # "):
            groups[-1].append(line)
        else:
            problems.append(f"neither a command nor prose as a comment: {line!r}")
    groups = [g for g in groups if g]
    if groups and groups[0][0].startswith("  # run ONE of the following"):
        if len(groups[0]) != 1:
            problems.append("the `run ONE of the following` line is not a block of its own")
        ways = groups[1:]
        if len(ways) < 2:
            problems.append("`run ONE of the following` before a single way on")
    else:
        ways = groups
        if len(ways) > 1:
            problems.append(f"{len(ways)} ways on, without `# run ONE of the following`")
    for way in ways:
        commands = [line[8:] for line in way if line.startswith("        ")]
        if not commands:
            problems.append(f"a way on with no command: {way!r}")
            continue
        if _segments(commands[0]) != [["cd", str(deploy)]]:
            problems.append(f"the first command is not the cd to {deploy}: {commands[0]!r}")
        for n, line in enumerate(way):
            if line.startswith("        ") and _reads_the_terminal(line[8:]) and n != len(way) - 1:
                problems.append(f"a line that may read the terminal is not the last: {line!r}")
        for command in commands:
            if subprocess.run([BASH, "-n", "-c", command], capture_output=True).returncode:
                problems.append(f"not shell: {command!r}")
                continue
            if _PLACEHOLDER.search(command.replace("2>/dev/null", "")):
                problems.append(f"a placeholder: {command!r}")
            for words in _segments(command):
                words = _bare(words)
                if words and words[0] in ("for", "done"):
                    continue
                if not words or words[0] not in COMMAND_WORDS:
                    problems.append(f"not a command the operator runs: {command!r}")
    return problems


def _bare(words: list[str]) -> list[str]:
    """A simple command without the `do`, `sudo` and `env -u NAME` before it."""
    while words[:1] in (["do"], ["sudo"]) or words[:2] == ["env", "-u"]:
        words = words[1:] if words[0] in ("do", "sudo") else words[3:]
    return words


def _reads_the_terminal(command: str) -> bool:
    """sudo (its password), first-boot.sh and the other scripts (their
    prompts), and an interactive `docker compose exec` (a hidden share)."""
    for words in _segments(command):
        if words[:1] == ["do"]:
            words = words[1:]
        if words[:1] == ["sudo"]:
            return True
        words = _bare(words)
        if words[:1] and words[0].startswith("./scripts/"):
            return True
        if words[:3] == ["docker", "compose", "exec"] and "-T" not in words:
            return True
    return False


def _segments(command: str) -> list[list[str]]:
    """COMMAND's simple commands, split at `&&`, `||`, `|` and `;` the way the
    shell splits them (a `;` inside quotes stays where it is); [] if it is not
    one shell line."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    try:
        for token in lexer:
            if token in ("&&", "||", "|", ";"):
                segments.append([])
            else:
                segments[-1].append(token)
    except ValueError:
        return []
    return segments


class Host:
    """One scratch host. `run()` runs first-boot.sh in it; `state` is the fake's."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.deploy = self.repo / "deploy"
        scripts = self.deploy / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "first-boot.sh").write_bytes(SCRIPT.read_bytes())
        _fake(scripts / "new-env.sh", FAKE_NEW_ENV)
        _fake(scripts / "build-images.sh", FAKE_BUILD_IMAGES)
        for name in ("preflight.sh", "check-public-tls.sh"):
            _fake(scripts / name, FAKE_SIMPLE)
        _fake(scripts / "openbao-seal.sh", FAKE_OPENBAO_SEAL)
        (scripts / "issue-gateway-cert.py").write_text("# stand-in\n")
        versions = self.repo / "backend" / "migrations" / "versions"
        versions.mkdir(parents=True)
        for name in ("0001_core_schema.py", f"{NEWEST_REVISION}_newest.py", "__init__.py"):
            (versions / name).write_text("")
        (self.repo / ".gitignore").write_text(".env\n")
        git = ["git", "-C", str(self.repo), "-c", "user.email=t@example.org", "-c", "user.name=t"]
        subprocess.run([*git[:3], "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "scratch"], check=True)
        self.tag = subprocess.run(
            [*git[:3], "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()

        self.etc = tmp / "etc"
        self.units = tmp / "systemd"
        self.home = tmp / "home"
        self.tmpdir = tmp / "tmpdir"
        self.harness = tmp / "harness"
        for d in (self.home, self.tmpdir, self.harness):
            d.mkdir()
        self.log = tmp / "argv.log"
        self.state_file = tmp / "fake-state.json"
        self.env_template = tmp / "env.template"
        self.env_template.write_text(
            "ITS_PUBLIC_HOST=rmm.novieri.com\n"
            "ITS_PUBLIC_CONSOLE_URL=https://rmm.novieri.com\n"
            "ITS_ACME_EMAIL=ops@novieri.example\n"
            "IT_SUITE_VERSION=dev\n"
            "POSTGRES_ADMIN_PASSWORD=" + "a" * 48 + "\n"
            "ITS_OPENBAO_TOKEN=change-me\n"
            "OPENBAO_TOKEN=change-me\n"
        )

        self.bin = tmp / "bin"
        self.bin.mkdir()
        real_path = os.environ["PATH"]
        real_env = real_command("env", real_path)
        assert real_env, "env is not on this machine's PATH"
        for name in SHIMMED:
            real = real_command(name, real_path)
            assert real, f"{name} is not on this machine's PATH"
            if name in CAPTURING:
                _exe(self.bin / name, FAKE_PRELUDE.lstrip("\n")
                     + CAPTURE_SHIM % {"name": name, "real": real})  # fmt: skip
                continue
            _exe(
                self.bin / name,
                f"#!/bin/sh\nprintf '%s %s\\n' {name} \"$*\" >> \"$ARGV_LOG\"\n"
                f"{{ printf '== %s\\n' {name}; {real_env}; }} >> \"$HARNESS_DIR/environ.log\"\n"
                f'exec {real} "$@"\n',
                python=False,
            )
        _exe(self.bin / "sleep", "#!/bin/sh\nexit 0\n", python=False)
        _fake(self.bin / "docker", FAKE_DOCKER)
        _exe(
            self.bin / "sudo",
            "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$ARGV_LOG\"\n"
            f"{{ printf '== sudo\\n'; {real_env}; }} >> \"$HARNESS_DIR/environ.log\"\n"
            'exec "$@"\n',
            python=False,
        )
        _fake(self.bin / "systemctl", FAKE_SYSTEMCTL)
        _fake(self.bin / "openssl", FAKE_OPENSSL)

        self.secrets: dict[str, Any] = {
            "shares": [_secret() for _ in range(5)],
            "root": "s." + _secret(),
            "its-app": "s." + _secret(),
            "gateway-issuer": "s." + _secret(),
            "code": "CODE" + secrets.token_hex(8).upper(),
        }
        self.state: dict[str, Any] = {
            "volumes": [],
            "images": [],
            "services": {},
            "serial": 1,
            "long": list(LONG),
            "oneshots": list(ONESHOTS),
            "healthchecked": sorted(HEALTHCHECKED),
            "oneshot_exit": {},
            "newest": NEWEST_REVISION,
            "bao": {
                "init": False,
                "sealed": True,
                "type": "shamir",
                "migrating": False,
                "progress": [],
                "shares": self.secrets["shares"],
                "root": self.secrets["root"],
                "tokens": {},
                "policies": [],
            },
            "issue": {
                "its-app": self.secrets["its-app"],
                "gateway-issuer": self.secrets["gateway-issuer"],
            },
            "pg": {"roles": 2, "alembic": "", "narrow": "f"},
            "files": {"audit": False, "gateway": False},
            "acme_mode": "600",
            "setup": {"needed": True, "code": self.secrets["code"]},
            "restart_drift": 0,
            "restart_step": 0,
        }
        self.save()

    # ----------------------------------------------------------------- state

    def save(self) -> None:
        self.state_file.write_text(json.dumps(self.state))

    def load(self) -> dict[str, Any]:
        self.state = json.loads(self.state_file.read_text())
        return self.state

    def write_env(self, **values: str) -> None:
        """`.env` as new-env.sh writes it, with VALUES on top (step 1 done)."""
        lines = self.env_template.read_text().splitlines()
        keys = {line.split("=", 1)[0] for line in lines}
        lines = [f"{k}={values[k]}" if k in values else line
                 for line in lines for k in [line.split("=", 1)[0]]]  # fmt: skip
        lines += [f"{k}={v}" for k, v in values.items() if k not in keys]
        env = self.deploy / ".env"
        env.write_text("\n".join(lines) + "\n")
        env.chmod(0o600)

    def env(self) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in (self.deploy / ".env").read_text().splitlines()
            if "=" in line
        )

    # ------------------------------------------------------------------ run

    def _environ(self, env: dict[str, str] | None) -> dict[str, str]:
        return {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "LANG": "C.UTF-8",
            "ARGV_LOG": str(self.log),
            "HARNESS_DIR": str(self.harness),
            "FAKE_STATE": str(self.state_file),
            "FAKE_ENV_TEMPLATE": str(self.env_template),
            "ITS_ETC_DIR": str(self.etc),
            "ITS_SYSTEMD_DIR": str(self.units),
            "ITS_NO_SYSTEMCTL": "1",
            **(env or {}),
        }

    def _fresh_logs(self) -> None:
        self.save()
        self.log.write_text("")
        shutil.rmtree(self.harness)
        self.harness.mkdir()
        (self.harness / "environ.log").write_text("")
        (self.harness / "writes.log").write_text("")

    def run(
        self,
        *args: str,
        stdin: str = "",
        env: dict[str, str] | None = None,
        bash_options: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        self._fresh_logs()
        done = subprocess.run(
            [BASH, *bash_options, str(self.deploy / "scripts" / "first-boot.sh"), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self._environ(env),
            cwd=self.tmp,
            timeout=100,
        )
        self.load()
        problems = failed_block_problems(done.stderr, self.deploy)
        assert not problems, (problems, done.stderr)
        return done

    def run_pty(
        self,
        *args: str,
        answers: Sequence[tuple[str, str | int]],
        stdin_tty: bool = True,
        stdin: str = "",
        env: dict[str, str] | None = None,
        timeout: float = 60,
    ) -> tuple[int, str]:
        """first-boot.sh with its output (and, unless STDIN_TTY is false, its
        input) on a pseudo-terminal, as an operator runs it. Each answer is
        written once its prompt is on the screen; one to a `(hidden)` prompt,
        once the terminal's echo is off. An answer that is a number is a
        signal, sent to the script instead. Returns the exit code and everything
        the terminal showed."""
        self._fresh_logs()
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [BASH, str(self.deploy / "scripts" / "first-boot.sh"), *args],
            stdin=slave if stdin_tty else subprocess.PIPE,
            stdout=slave,
            stderr=slave,
            env={"TERM": "xterm", **self._environ(env)},
            cwd=self.tmp,
            start_new_session=True,
        )
        os.close(slave)
        if not stdin_tty:
            assert proc.stdin
            proc.stdin.write(stdin.encode())
            proc.stdin.close()
        shown, seen, answered = b"", 0, 0
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    shown += chunk
                if answered < len(answers):
                    prompt, reply = answers[answered]
                    at = shown.find(prompt.encode(), seen)
                    if at >= 0:
                        seen = at + len(prompt.encode())
                        if isinstance(reply, int):
                            os.kill(proc.pid, reply)
                        else:
                            if "(hidden)" in prompt:
                                _wait_echo_off(master)
                            os.write(master, (reply + "\n").encode())
                        answered += 1
            else:
                proc.kill()
        finally:
            rc = proc.wait()
            os.close(master)
        self.load()
        return rc, shown.decode(errors="replace")

    # -------------------------------------------------------------- reading

    def argv(self) -> str:
        return self.log.read_text()

    def environ_log(self) -> str:
        return (self.harness / "environ.log").read_text()

    def writes(self) -> list[tuple[str, str]]:
        """(writer, path) of each file a fake wrote, and each `mktemp` made."""
        return [
            (line.split(" ", 1)[0], line.split(" ", 1)[1])
            for line in (self.harness / "writes.log").read_text().splitlines()
        ]

    def captures(self) -> list[tuple[str, str, str]]:
        """(how, where, what) of each write, deletion or move the capturing
        shims saw: WHERE relative to the host when it is inside it."""
        found = []
        for what in sorted((self.harness / "captures").glob("*.what")):
            kind, target = what.read_text().splitlines()[:2]
            path = Path(target)
            where = str(path.relative_to(self.tmp)) if path.is_relative_to(self.tmp) else target
            data = what.with_suffix(".data").read_bytes().decode(errors="replace")
            found.append((kind, where, data))
        return found

    def all_secrets(self) -> list[str]:
        s = self.secrets
        return [*s["shares"], s["root"], s["its-app"], s["gateway-issuer"], s["code"]]

    def labelled_secrets(self) -> list[tuple[str, str]]:
        s = self.secrets
        shares = [(f"share {n}", v) for n, v in enumerate(s["shares"], 1)]
        return [*shares, *((k, s[k]) for k in ("root", "its-app", "gateway-issuer", "code"))]

    def files_holding(self, value: str) -> list[str]:
        """Every file under the scratch host that holds VALUE, but the fake's
        own state (the stand-in OpenBao), the argv log (read on its own) and
        harness/ (read by `leaks`)."""
        found = []
        for path in self.tmp.rglob("*"):
            if not path.is_file() or path in (self.state_file, self.log):
                continue
            if ".git" in path.parts or path.is_relative_to(self.harness):
                continue
            if value in path.read_text(errors="replace"):
                found.append(str(path.relative_to(self.tmp)))
        return found

    def leaks(self) -> list[str]:
        """Every place a secret reached that the runbook does not name: a
        command line, a child's environment, a file left on the host, and a
        file written, moved or deleted on the way."""
        argv, environ, captured = self.argv(), self.environ_log(), self.captures()
        found = []
        for label, value in self.labelled_secrets():
            allowed = ALLOWED.get(label, [])
            if value in argv:
                found.append(f"{label}: on a command line")
            if value in environ:
                found.append(f"{label}: in a child's environment")
            found += [f"{label}: in {p}" for p in self.files_holding(value) if p not in allowed]
            found += [
                f"{label}: {kind} {where}"
                for kind, where, data in captured
                if value in data and where not in allowed
            ]
        return found


def lines(output: str, prefix: str) -> list[str]:
    """The `  run     ` / `  skip    ` / `  ok      ` lines, prefix removed."""
    tag = f"  {prefix:<8}"
    return [line[len(tag):] for line in output.splitlines() if line.startswith(tag)]
