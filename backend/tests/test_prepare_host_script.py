"""`deploy/scripts/prepare-host.sh`, on a stubbed host (design
2026-11-27-despliegue-automatizado §3, §5).

Every command that changes or asks the host -- `apt-get`, `ufw`,
`timedatectl`, `usermod`, `curl`, `install`, `chmod`, `tee`, `dpkg`,
`dpkg-query`, `apt-config`, `systemctl`, `docker`, `id` -- is a stub on PATH
that logs its arguments and answers from a small state directory, so a
second run sees what the first one did. What is pinned: the commands and
their order, which are the runbook's P2 to P5; a second run that changes
nothing; and the refusals. P1 is not the script's: the operator account is
made by hand, as root, before the checkout it would run from exists
(coordinator's ruling Q5, 2026-11-27). Turning ufw on and installing Docker for real are
not run anywhere by this repository: they stay unverified, as before.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "prepare-host.sh"

STUBS = {
    "id": r"""#!/bin/sh
echo "id $*" >> "$STUB_LOG"
case "$1" in
  -u) if [ $# -eq 1 ]; then echo "$FAKE_UID"
      elif [ -e "$STUB_STATE/user-$2" ]; then echo 1001
      else exit 1; fi ;;
  -nG) cat "$STUB_STATE/groups-$2" 2>/dev/null || echo "$2" ;;
esac
""",
    "usermod": r"""#!/bin/sh
echo "usermod $*" >> "$STUB_LOG"
groups="$(cat "$STUB_STATE/groups-$3" 2>/dev/null || echo "$3")"
echo "$groups $2" > "$STUB_STATE/groups-$3"
""",
    "ufw": r"""#!/bin/sh
echo "ufw $*" >> "$STUB_LOG"
case "$1" in
  allow) echo "$2" >> "$STUB_STATE/ufw-rules" ;;
  --force) touch "$STUB_STATE/ufw-on" ;;
  status)
    [ -e "$STUB_STATE/ufw-on" ] || { echo "Status: inactive"; exit 0; }
    printf 'Status: active\nLogging: on (low)\n'
    printf 'Default: %s, allow (outgoing), disabled (routed)\n\n' \
      "${FAKE_UFW_DEFAULT:-deny (incoming)}"
    printf 'To                         Action      From\n'
    printf '%s\n' '--                         ------      ----'
    while read -r rule; do printf '%-26s ALLOW IN    Anywhere\n' "$rule"
    done < "$STUB_STATE/ufw-rules" ;;
esac
""",
    "apt-get": r"""#!/bin/sh
echo "apt-get $*" >> "$STUB_LOG"
case "$*" in
  "update") [ -z "$FAKE_APT_UPDATE_RC" ] || exit "$FAKE_APT_UPDATE_RC" ;;
  *"install -y unattended-upgrades"*) touch "$STUB_STATE/pkg-unattended-upgrades" ;;
  *"install -y docker-ce "*) touch "$STUB_STATE/pkg-docker-ce" ;;
esac
""",
    "dpkg-query": r"""#!/bin/sh
echo "dpkg-query $*" >> "$STUB_LOG"
for last; do :; done
[ -e "$STUB_STATE/pkg-$last" ] && printf 'install ok installed' || exit 1
""",
    "dpkg": r"""#!/bin/sh
echo "dpkg $*" >> "$STUB_LOG"
case "$1" in
  --print-architecture) echo amd64 ;;
  --get-selections) printf '%b' "$FAKE_CONFLICTS" ;;
esac
""",
    "apt-config": r"""#!/bin/sh
echo "apt-config $*" >> "$STUB_LOG"
cat "$STUB_STATE/tee-20auto-upgrades" 2>/dev/null
""",
    "tee": r"""#!/bin/sh
echo "tee $*" >> "$STUB_LOG"
cat > "$STUB_STATE/tee-$(basename "$1")"
cat "$STUB_STATE/tee-$(basename "$1")"
""",
    "systemctl": r"""#!/bin/sh
echo "systemctl $*" >> "$STUB_LOG"
case "$*" in
  "is-active unattended-upgrades")
    if [ -e "$STUB_STATE/pkg-unattended-upgrades" ] || [ -e "$STUB_STATE/svc-active" ]
    then echo active; else echo inactive; fi ;;
esac
""",
    "timedatectl": r"""#!/bin/sh
echo "timedatectl $*" >> "$STUB_LOG"
case "$*" in
  "show -p NTP --value") [ -e "$STUB_STATE/ntp-on" ] && echo yes || echo no ;;
  "show -p NTPSynchronized --value")
    if [ -e "$STUB_STATE/sync-pending" ]; then rm "$STUB_STATE/sync-pending"; echo no; exit 0; fi
    [ -e "$STUB_STATE/ntp-on" ] && [ -z "$FAKE_NEVER_SYNC" ] && echo yes || echo no ;;
  "set-ntp true") touch "$STUB_STATE/ntp-on" ;;
  status) [ -e "$STUB_STATE/ntp-on" ] && s=yes n=active || s=no n=inactive
          printf 'System clock synchronized: %s\n              NTP service: %s\n' "$s" "$n" ;;
esac
""",
    "docker": r"""#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
[ -e "$STUB_STATE/pkg-docker-ce" ] || { [ -n "$FAKE_OLD_ENGINE" ] || exit 1; }
case "$*" in
  "version --format {{.Server.Version}}") echo "${FAKE_ENGINE:-29.1.2}" ;;
  "compose version --short") echo "${FAKE_COMPOSE:-v5.0.1}" ;;
esac
""",
    "sleep": "#!/bin/sh\nexit 0\n",
}
for _name in ("curl", "install", "chmod"):
    STUBS[_name] = f'#!/bin/sh\necho "{_name} $*" >> "$STUB_LOG"\n'

#: What a host bare of everything the runbook adds is made to run, in order:
#: the runbook's P2, P3, P4 and P5 (deploy/README.md's appendix).
FIRST_RUN = [
    "ufw allow 22/tcp",
    "ufw allow 443/tcp",
    "ufw allow 8443/tcp",
    "ufw --force enable",
    "apt-get update",
    "apt-get install -y unattended-upgrades",
    "tee /etc/apt/apt.conf.d/20auto-upgrades",
    "timedatectl set-ntp true",
    "apt-get remove -y docker.io",
    "apt-get install -y ca-certificates curl git openssl iproute2",
    "install -m 0755 -d /etc/apt/keyrings",
    "curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc",
    "chmod a+r /etc/apt/keyrings/docker.asc",
    "tee /etc/apt/sources.list.d/docker.sources",
    "apt-get update",
    "apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin"
    " docker-compose-plugin",
    "usermod -aG docker rmmadmin",
]
#: The commands that change something. A second run logs none of them.
CHANGING = ("usermod", "ufw allow", "ufw --force", "apt-get ",
            "tee ", "timedatectl set-ntp", "curl ", "install ", "chmod ")  # fmt: skip

UBUNTU = 'ID=ubuntu\nVERSION_ID="24.04"\nUBUNTU_CODENAME=noble\nVERSION_CODENAME=noble\n'


class Box:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.state = tmp / "state"
        self.state.mkdir()
        self.log = tmp / "calls.log"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            stub = self.bin / name
            stub.write_text(body)
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.os_release = tmp / "os-release"
        self.os_release.write_text(UBUNTU)
        deploy = tmp / "repo" / "deploy" / "scripts"
        deploy.mkdir(parents=True)
        self.script = deploy / "prepare-host.sh"
        self.script.write_bytes(SCRIPT.read_bytes())

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        self.log.write_text("")
        base = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "ITS_OS_RELEASE": str(self.os_release),
            "FAKE_UID": "0",
            "FAKE_CONFLICTS": r"docker.io\tinstall\n",
            "SUDO_USER": "rmmadmin",
        }
        base.update(env)
        return subprocess.run(
            [shutil.which("bash") or "/bin/bash", str(self.script), *args],
            capture_output=True,
            text=True,
            env=base,
            stdin=subprocess.DEVNULL,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def changes(self) -> list[str]:
        return [c for c in self.calls() if c.startswith(CHANGING)]


def test_the_script_parses() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_a_bare_host_gets_the_runbooks_p2_to_p5_in_order(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert box.changes() == FIRST_RUN
    assert "ok      Docker Engine 29.1.2 (26 or newer)" in done.stdout
    assert "ok      Docker Compose 5.0.1 (2.24 or newer)" in done.stdout
    assert "log out, and log in again as rmmadmin" in done.stdout
    assert f"cd {tmp_path}/repo/deploy && ./scripts/first-boot.sh" in done.stdout
    sources = (box.state / "tee-docker.sources").read_text()
    assert sources == (
        "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\n"
        "Components: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n"
    )
    assert (box.state / "tee-20auto-upgrades").read_text() == (
        'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
    )


def test_a_second_run_changes_nothing_and_says_so(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert box.run().returncode == 0
    again = box.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert box.changes() == []
    skipped = [line.split(":")[0].strip() for line in again.stdout.splitlines()
               if line.startswith("  skip    ")]  # fmt: skip
    assert skipped == ["skip    P2", "skip    P3", "skip    P4", "skip    P5", "skip    P5"]
    assert "log out, and log in again" not in again.stdout
    assert "if you have not logged out and back in as rmmadmin" in again.stdout


def test_ntp_already_on_is_not_set_again(tmp_path: Path) -> None:
    """NTP on but not yet synchronised: P4 waits for the clock, it does not set NTP."""
    box = Box(tmp_path)
    (box.state / "ntp-on").touch()
    (box.state / "sync-pending").touch()
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "timedatectl set-ntp true" not in box.changes()
    assert "ok      P4: NTP is on and the clock is synchronised" in done.stdout


def test_a_clock_that_never_synchronises_stops_the_run(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(FAKE_NEVER_SYNC="1")
    assert done.returncode == 1
    assert "the clock is not synchronised after 120 s" in done.stderr
    assert not any(c.startswith("apt-get install -y docker-ce ") for c in box.calls())


def test_it_refuses_to_run_as_anyone_but_root(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(FAKE_UID="1000")
    assert done.returncode == 1
    assert "it runs as root" in done.stderr
    assert f"sudo {box.script}" in done.stderr
    assert box.changes() == []


def test_without_sudo_user_it_asks_for_the_operator(tmp_path: Path) -> None:
    """Run from a root login, there is no operator to put in `docker`: the
    account is P1's, made by hand first."""
    box = Box(tmp_path)
    done = box.run(SUDO_USER="")
    assert done.returncode == 1
    assert "by hand: deploy/README.md, P1" in done.stderr
    assert f"sudo {box.script}" in done.stderr
    assert done.stderr.count("FAILED") == 1
    assert box.changes() == []


def test_sudo_user_root_is_refused_like_no_operator(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(SUDO_USER="root")
    assert done.returncode == 1 and "which account is the operator?" in done.stderr
    assert box.changes() == []


def test_it_has_no_way_to_make_the_account(tmp_path: Path) -> None:
    """Coordinator's ruling Q5 (2026-11-27): P1 stays manual, so the option
    that made the account is gone and refused like any unknown one."""
    box = Box(tmp_path)
    done = box.run("--create-user", "rmmadmin", SUDO_USER="")
    assert done.returncode == 1
    assert "unknown argument: --create-user" in done.stderr
    assert box.changes() == []
    assert "adduser" not in SCRIPT.read_text()


def test_a_host_that_is_not_ubuntu_is_refused_before_anything(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.os_release.write_text('ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n')
    done = box.run()
    assert done.returncode == 1 and "this is not Ubuntu" in done.stderr
    assert box.changes() == []


def test_an_engine_too_old_after_the_install_stops_the_run(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(FAKE_ENGINE="24.0.7")
    assert done.returncode == 1
    assert "Docker Engine is 24.0.7, and it must be 26 or newer" in done.stderr


def test_ubuntus_own_docker_is_replaced_not_kept(tmp_path: Path) -> None:
    """docker.io answers `docker version` too, but it is not docker-ce."""
    box = Box(tmp_path)
    done = box.run(FAKE_OLD_ENGINE="1", FAKE_ENGINE="26.1.3")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "apt-get remove -y docker.io" in box.changes()


def test_an_extra_open_port_is_warned_about(tmp_path: Path) -> None:
    box = Box(tmp_path)
    (box.state / "ufw-rules").write_text("80/tcp\n")
    done = box.run()
    assert "ufw also lets in: 80/tcp" in done.stderr
    assert done.stderr.count("lets in") == 1


def test_the_v4_and_v6_lines_of_one_port_count_once(tmp_path: Path) -> None:
    box = Box(tmp_path)
    (box.state / "ufw-rules").write_text("80/tcp\n80/tcp (v6)\n")
    done = box.run()
    assert "ufw also lets in: 80/tcp. This stack" in done.stderr


def test_a_failure_outside_die_still_says_what_failed_and_how_to_continue(
    tmp_path: Path,
) -> None:
    """A held dpkg lock makes `apt-get update` exit 100: still FAILED, the step, a re-run."""
    box = Box(tmp_path)
    done = box.run(FAKE_APT_UPDATE_RC="100")
    assert done.returncode == 1
    assert done.stderr.count("FAILED") == 1
    assert 'FAILED  "P3 — security updates" did not finish (exit 100' in done.stderr
    assert f"        sudo {box.script}" in done.stderr


def test_a_refusal_reports_once_not_twice(tmp_path: Path) -> None:
    done = Box(tmp_path).run(FAKE_UID="1000")
    assert done.stderr.count("FAILED") == 1 and "did not finish" not in done.stderr


def test_a_firewall_that_does_not_deny_incoming_stops_with_the_fix(tmp_path: Path) -> None:
    box = Box(tmp_path)
    done = box.run(FAKE_UFW_DEFAULT="allow (incoming)")
    assert done.returncode == 1
    assert "ufw is not what P2 needs" in done.stderr
    assert "sudo ufw default deny incoming" in done.stderr
    assert f"then: sudo {box.script}" in done.stderr


def test_compose_is_compared_as_numbers_not_as_text(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ok = Box(tmp_path / "a")
    done = ok.run(FAKE_COMPOSE="v2.100.0")
    assert done.returncode == 0, done.stdout + done.stderr
    bad = Box(tmp_path / "b")
    done = bad.run(FAKE_COMPOSE="v2.3.0")
    assert done.returncode == 1
    assert "Docker Compose is 2.3.0, and it must be 2.24 or newer" in done.stderr


def test_engine_25_99_99_is_too_old(tmp_path: Path) -> None:
    done = Box(tmp_path).run(FAKE_ENGINE="25.99.99")
    assert done.returncode == 1
    assert "Docker Engine is 25.99.99, and it must be 26 or newer" in done.stderr


def test_p3_is_not_skipped_when_the_package_is_missing(tmp_path: Path) -> None:
    """The service answers and the config is there, but dpkg says no package."""
    box = Box(tmp_path)
    (box.state / "svc-active").touch()
    (box.state / "tee-20auto-upgrades").write_text(
        'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
    )
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "apt-get install -y unattended-upgrades" in box.changes()


def test_the_os_release_switch_says_it_was_used(tmp_path: Path) -> None:
    done = Box(tmp_path).run()
    assert "ITS_OS_RELEASE is set" in done.stdout
