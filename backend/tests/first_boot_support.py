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
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
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
    "install", "mkdir", "mv", "readlink", "rm", "sed", "sort", "stat", "tail",
    "tee", "test", "tr",
)  # fmt: skip

#: The long-running services `docker compose up -d` starts, and the one-shots.
LONG = ("api", "backup-replicator", "gateway", "minio", "openbao", "postgres", "redis",
        "traefik", "web", "worker")  # fmt: skip
ONESHOTS = ("migrate", "postgres-grants", "openbao-init")
HEALTHCHECKED = {"api", "gateway", "minio", "openbao", "postgres", "web", "worker"}
NEWEST_REVISION = "0248"

FAKE_DOCKER = r'''
import json, os, sys

STATE = os.environ["FAKE_STATE"]
with open(STATE) as fh:
    s = json.load(fh)
argv = sys.argv[1:]
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("docker " + " ".join(argv) + "\n")
    log.write("  COMPOSE_FILE=" + os.environ.get("COMPOSE_FILE", "") + "\n")


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
                       "t": 3 if b["init"] else 0, "n": 5 if b["init"] else 0}, indent=2) + "\n"


def bao(args, token):
    b = s["bao"]
    if args[:2] == ["token", "lookup"]:
        if token == b["root"] and b["init"]:
            out(json.dumps({"data": {"policies": ["root"]}, "policies": ["root"]}, indent=2) + "\n")
            return 0
        return 2
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
                key = body["key"]
                if key not in b["progress"]:
                    b["progress"].append(key)
                if len(b["progress"]) >= 3:
                    good = all(k in b["shares"] for k in b["progress"])
                    b["progress"] = []
                    if not good:
                        return 2
                    b["sealed"] = False
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

FAKE_OPENBAO_SEAL = r'''
import json, os, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write("openbao-seal.sh " + " ".join(sys.argv[1:]) + "\n")
    log.write("  COMPOSE_FILE=" + os.environ.get("COMPOSE_FILE", "") + "\n")
with open(os.environ["FAKE_STATE"]) as fh:
    s = json.load(fh)
given = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
if os.environ.get("FAKE_SEAL_FAIL") == "1":
    print("FAILED  OpenBao did not come back", file=sys.stderr)
    sys.exit(1)
s["seal_stdin"] = len(given)
if len(given) != 3 or len(set(given)) != 3 or not all(g in s["bao"]["shares"] for g in given):
    print("FAILED  still sealed after the shares given", file=sys.stderr)
    sys.exit(1)
key = sys.argv[sys.argv.index("--key-file") + 1]
os.makedirs(os.path.dirname(key), exist_ok=True)
with open(key, "wb") as fh:
    fh.write(os.urandom(32))
lines = [l for l in open(".env").read().splitlines()
         if not l.startswith(("COMPOSE_FILE=", "OPENBAO_UNSEAL_KEY_FILE="))]
lines += ["COMPOSE_FILE=docker-compose.yml:openbao/auto-unseal.yml",
          "OPENBAO_UNSEAL_KEY_FILE=" + key]
open(".env", "w").write("\n".join(lines) + "\n")
s["bao"]["type"] = "static"
with open(os.environ["FAKE_STATE"], "w") as fh:
    json.dump(s, fh)
print("Auto-unseal is ON")
'''

FAKE_SUDO = r'''#!/bin/sh
printf 'sudo %s\n' "$*" >> "$ARGV_LOG"
exec "$@"
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


def _secret() -> str:
    return secrets.token_urlsafe(32)


class Host:
    """One scratch host. `run()` runs first-boot.sh in it; `state` is the fake's."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.deploy = self.repo / "deploy"
        scripts = self.deploy / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "first-boot.sh").write_bytes(SCRIPT.read_bytes())
        _exe(scripts / "new-env.sh", FAKE_NEW_ENV)
        _exe(scripts / "build-images.sh", FAKE_BUILD_IMAGES)
        for name in ("preflight.sh", "check-public-tls.sh"):
            _exe(scripts / name, FAKE_SIMPLE)
        _exe(scripts / "openbao-seal.sh", FAKE_OPENBAO_SEAL)
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
        for d in (self.home, self.tmpdir):
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
        for name in SHIMMED:
            real = real_command(name, real_path)
            assert real, f"{name} is not on this machine's PATH"
            _exe(
                self.bin / name,
                f'#!/bin/sh\nprintf \'%s %s\\n\' {name} "$*" >> "$ARGV_LOG"\nexec {real} "$@"\n',
                python=False,
            )
        _exe(self.bin / "sleep", "#!/bin/sh\nexit 0\n", python=False)
        _exe(self.bin / "docker", FAKE_DOCKER)
        _exe(self.bin / "sudo", FAKE_SUDO, python=False)
        _exe(self.bin / "systemctl", FAKE_SYSTEMCTL)
        _exe(self.bin / "openssl", FAKE_OPENSSL)

        self.secrets = {
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

    def run(
        self, *args: str, stdin: str = "", env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.save()
        self.log.write_text("")
        environ = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "LANG": "C.UTF-8",
            "ARGV_LOG": str(self.log),
            "FAKE_STATE": str(self.state_file),
            "FAKE_ENV_TEMPLATE": str(self.env_template),
            "ITS_ETC_DIR": str(self.etc),
            "ITS_SYSTEMD_DIR": str(self.units),
            "ITS_NO_SYSTEMCTL": "1",
            **(env or {}),
        }
        done = subprocess.run(
            [BASH, str(self.deploy / "scripts" / "first-boot.sh"), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=environ,
            cwd=self.tmp,
            timeout=100,
        )
        self.load()
        return done

    def argv(self) -> str:
        return self.log.read_text()

    def all_secrets(self) -> list[str]:
        s = self.secrets
        return [*s["shares"], s["root"], s["its-app"], s["gateway-issuer"], s["code"]]

    def files_holding(self, value: str) -> list[str]:
        """Every file under the scratch host that holds VALUE, but the fake's
        own state (the stand-in OpenBao) and the argv log (read on its own)."""
        found = []
        for path in self.tmp.rglob("*"):
            if not path.is_file() or path in (self.state_file, self.log):
                continue
            if ".git" in path.parts:
                continue
            if value in path.read_text(errors="replace"):
                found.append(str(path.relative_to(self.tmp)))
        return found


def lines(output: str, prefix: str) -> list[str]:
    """The `  run     ` / `  skip    ` / `  ok      ` lines, prefix removed."""
    tag = f"  {prefix:<8}"
    return [line[len(tag):] for line in output.splitlines() if line.startswith(tag)]
