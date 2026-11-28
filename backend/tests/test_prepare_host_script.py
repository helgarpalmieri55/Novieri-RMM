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

import pytest

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
  update|*" update") [ -z "$FAKE_APT_UPDATE_RC" ] || exit "$FAKE_APT_UPDATE_RC" ;;
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
  "show -p LoadState --value chrony.service")
    if [ -n "$FAKE_NO_CHRONY" ]; then echo not-found; else echo loaded; fi ;;
esac
""",
    "timedatectl": r"""#!/bin/sh
echo "timedatectl $*" >> "$STUB_LOG"
case "$*" in
  "show -p NTP --value") [ -e "$STUB_STATE/ntp-on" ] && echo yes || echo no ;;
  "show -p NTPSynchronized --value")
    if [ -e "$STUB_STATE/sync-pending" ]; then rm "$STUB_STATE/sync-pending"; echo no; exit 0; fi
    [ -n "$FAKE_NO_NTP_SERVICE" ] && [ -z "$FAKE_NEVER_SYNC" ] && { echo yes; exit 0; }
    [ -e "$STUB_STATE/ntp-on" ] && [ -z "$FAKE_NEVER_SYNC" ] && echo yes || echo no ;;
  "set-ntp true") [ -n "$FAKE_NO_NTP_SERVICE" ] || touch "$STUB_STATE/ntp-on" ;;
  status) [ -e "$STUB_STATE/ntp-on" ] && s=yes n=active || s=no n=inactive
          [ -z "$FAKE_NO_NTP_SERVICE" ] || [ -n "$FAKE_NEVER_SYNC" ] || s=yes
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
    "systemd-detect-virt": r"""#!/bin/sh
echo "systemd-detect-virt $*" >> "$STUB_LOG"
case "$*" in
  *--container*) [ -n "$FAKE_CONTAINER" ] || exit 1 ;;
esac
""",
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
    "apt-get -o DPkg::Lock::Timeout=300 update",
    "apt-get -o DPkg::Lock::Timeout=300 install -y unattended-upgrades",
    "tee /etc/apt/apt.conf.d/20auto-upgrades",
    "timedatectl set-ntp true",
    "apt-get -o DPkg::Lock::Timeout=300 remove -y docker.io",
    "apt-get -o DPkg::Lock::Timeout=300 install -y ca-certificates curl git openssl iproute2",
    "install -m 0755 -d /etc/apt/keyrings",
    "curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc",
    "chmod a+r /etc/apt/keyrings/docker.asc",
    "tee /etc/apt/sources.list.d/docker.sources",
    "apt-get -o DPkg::Lock::Timeout=300 update",
    "apt-get -o DPkg::Lock::Timeout=300 install -y docker-ce docker-ce-cli containerd.io"
    " docker-buildx-plugin"
    " docker-compose-plugin",
    "usermod -aG docker rmmadmin",
]
#: The commands that change something. A second run logs none of them.
CHANGING = ("usermod", "ufw allow", "ufw --force", "apt-get ",
            "tee ", "timedatectl set-ntp", "curl ", "install ", "chmod ")  # fmt: skip

#: The operator's decision of 2026-11-28: Ubuntu Server 26.04 LTS. These four lines are
#: what ubuntu:26.04's /etc/os-release says (26.04.1, measured 2026-11-28).
UBUNTU = 'ID=ubuntu\nVERSION_ID="26.04"\nUBUNTU_CODENAME=resolute\nVERSION_CODENAME=resolute\n'
NOBLE = 'ID=ubuntu\nVERSION_ID="24.04"\nUBUNTU_CODENAME=noble\nVERSION_CODENAME=noble\n'


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
        "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: resolute\n"
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
    assert not any(c.startswith(LOCK + "install -y docker-ce ") for c in box.calls())


def _p4_report(stderr: str) -> list[str]:
    """P4's FAILED block, the line that says what failed first."""
    assert stderr.count("FAILED") == 1, stderr
    return stderr[stderr.index("FAILED") :].splitlines()


def test_a_clock_that_never_synchronises_names_nts_and_its_port(tmp_path: Path) -> None:
    """chrony is running and the clock does not synchronise. 26.04's chrony
    takes its Ubuntu sources over NTS (/etc/chrony/sources.d/ubuntu-ntp-pools
    .sources), whose key exchange is TCP 4460: chrony's debian/NEWS
    (4.5-3ubuntu4) says the clock is not adjusted without it. That 4460 alone
    is enough is that source's. What was measured on 2026-11-28 is a sandbox
    where TCP 4460 was refused and UDP 123 got no answer either: every source
    `^?`, Reach 0. The block ends at the line that reads the terminal."""
    box = Box(tmp_path)
    done = box.run(FAKE_NEVER_SYNC="1")
    assert done.returncode == 1
    report = _p4_report(done.stderr)
    assert report[1:3] == ["        timedatectl status", "        chronyc -N sources"]
    assert "TCP 4460" in report[3] and "UDP 123" in report[3] and report[3].startswith("        # ")
    assert report[4] == f"        sudo {box.script}"
    assert len(report) == 5


CONTAINER_NOTE = (
    "  note    P4: timedatectl reports no NTP service running here, and systemd-detect-virt"
    " says this is a container, whose clock is its host's: chrony does not start in one"
    " (chrony.service, ConditionVirtualization=|!container)\n"
)


def test_a_container_whose_clock_is_synchronised_is_skipped_and_says_why(
    tmp_path: Path,
) -> None:
    """In a container chrony.service does not start (ConditionVirtualization=
    |!container), `timedatectl set-ntp true` exits 0 with NTP still no, and
    NTPSynchronized is the host kernel's (measured 2026-11-28 in a 26.04
    systemd container). There, and only there (systemd-detect-virt), a
    synchronised clock is P4 done: a skip, no set-ntp, and never "NTP is on"."""
    box = Box(tmp_path)
    done = box.run(FAKE_NO_NTP_SERVICE="1", FAKE_CONTAINER="1")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "timedatectl set-ntp true" not in box.changes()
    assert "systemd-detect-virt --quiet --container" in box.calls()
    assert "  skip    P4: the clock is synchronised, and this is a container\n" in done.stdout
    assert CONTAINER_NOTE in done.stdout
    assert "NTP is on" not in done.stdout


def test_a_second_run_in_a_container_changes_nothing(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert box.run(FAKE_NO_NTP_SERVICE="1", FAKE_CONTAINER="1").returncode == 0
    again = box.run(FAKE_NO_NTP_SERVICE="1", FAKE_CONTAINER="1")
    assert again.returncode == 0, again.stdout + again.stderr
    assert box.changes() == []


def test_a_container_whose_clock_synchronises_during_the_wait_gets_ok_and_the_note(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path)
    (box.state / "sync-pending").touch()
    done = box.run(FAKE_NO_NTP_SERVICE="1", FAKE_CONTAINER="1")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "timedatectl set-ntp true" in box.changes()
    assert "  ok      P4: the clock is synchronised\n" in done.stdout
    assert CONTAINER_NOTE in done.stdout
    assert "NTP is on" not in done.stdout


def test_outside_a_container_no_ntp_service_fails_even_with_a_synchronised_clock(
    tmp_path: Path,
) -> None:
    """Review I1 (2026-11-28): on a VPS where chrony stopped or failed to
    start, set-ntp exits 0 (timedated replies before the start job's result)
    and the kernel still says synchronised for hours on the last maxerror.
    The runbook's P4 asks for `NTP service: active` too: not a container, no
    NTP service running, is P4's FAILED, and nothing after it runs."""
    box = Box(tmp_path)
    done = box.run(FAKE_NO_NTP_SERVICE="1")
    assert done.returncode == 1
    assert "ok      P4" not in done.stdout and "skip    P4" not in done.stdout
    report = _p4_report(done.stderr)
    assert report[0].startswith("FAILED  no NTP service is running")
    assert report[1:3] == [
        "        timedatectl status", "        systemctl status chrony --no-pager"
    ]  # fmt: skip
    assert report[3].startswith("        # ") and "sudo journalctl -u chrony" in report[3]
    assert report[4] == f"        sudo {box.script}"
    assert len(report) == 5
    assert "4460" not in done.stderr
    assert not any(c.startswith(LOCK + "install -y docker-ce ") for c in box.calls())


def test_no_ntp_service_and_no_synchronised_clock_points_at_the_service_not_a_port(
    tmp_path: Path,
) -> None:
    """Review M1: with no NTP service running there is nothing for a port to
    reach; the block points at the service, as the case above."""
    box = Box(tmp_path)
    done = box.run(FAKE_NO_NTP_SERVICE="1", FAKE_NEVER_SYNC="1")
    assert done.returncode == 1
    report = _p4_report(done.stderr)
    assert report[0].startswith("FAILED  no NTP service is running")
    assert report[2] == "        systemctl status chrony --no-pager"
    assert report[-1] == f"        sudo {box.script}" and len(report) == 5
    assert "4460" not in done.stderr and "UDP 123" not in done.stderr


def test_a_container_whose_clock_never_synchronises_says_its_host_keeps_it(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path)
    done = box.run(FAKE_NO_NTP_SERVICE="1", FAKE_NEVER_SYNC="1", FAKE_CONTAINER="1")
    assert done.returncode == 1
    report = _p4_report(done.stderr)
    assert report[0].startswith("FAILED  the clock is not synchronised after 120 s, and this is"
                                " a container")  # fmt: skip
    assert report[1] == "        timedatectl status"
    assert report[2].startswith("        # ") and "its host" in report[2]
    assert report[3] == f"        sudo {box.script}" and len(report) == 4
    assert "4460" not in done.stderr


@pytest.mark.parametrize(
    "case",
    [
        {"FAKE_NEVER_SYNC": "1"},
        {"FAKE_NO_NTP_SERVICE": "1"},
        {"FAKE_NO_NTP_SERVICE": "1", "FAKE_NEVER_SYNC": "1", "FAKE_CONTAINER": "1"},
        {"FAKE_NO_NTP_SERVICE": "1", "FAKE_CONTAINER": "1"},
    ],
    ids=["service-running-unsynced", "no-service", "container-unsynced", "container-synced"],
)
def test_without_chrony_p4_names_no_chrony(tmp_path: Path, case: dict[str, str]) -> None:
    """Review M6: 24.04, which this warns about and goes on with, keeps time
    with systemd-timesyncd, and has no chronyc. Whatever P4 prints there names
    no chrony, and a FAILED block still ends at the line that reads the
    terminal."""
    box = Box(tmp_path)
    box.os_release.write_text(NOBLE)
    done = box.run(FAKE_NO_CHRONY="1", **case)
    p4 = done.stdout[done.stdout.index("P4 — the clock") :]
    p4 = p4[: p4.find("P5 — ")] if "P5 — " in p4 else p4
    # The temporary directory is named after this test, which names chrony.
    assert "chrony" not in (p4 + done.stderr).replace(str(tmp_path), "")
    if done.returncode == 1:
        report = _p4_report(done.stderr)
        assert report[-1] == f"        sudo {box.script}"
        assert all(line.startswith(("        # ", "        timedatectl ", "        sudo "))
                   for line in report[1:]), report  # fmt: skip
    else:
        assert "P4: the clock is synchronised" in done.stdout


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
    assert "# deploy/README.md, Prerequisites: Ubuntu Server 26.04 LTS" in done.stderr
    assert box.changes() == []


def test_ubuntu_26_04_is_the_release_it_targets_and_is_not_warned_about(
    tmp_path: Path,
) -> None:
    done = Box(tmp_path).run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "WARNING Ubuntu" not in done.stderr


def test_ubuntu_24_04_is_warned_about_and_goes_on(tmp_path: Path) -> None:
    """A warning, not a refusal (operator, 2026-11-28): 24.04 still gets Docker's
    repository for its own codename, and every step."""
    box = Box(tmp_path)
    box.os_release.write_text(NOBLE)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert (
        "  WARNING Ubuntu 24.04, not 26.04: deploy/README.md's Prerequisites ask for"
        " Ubuntu Server 26.04 LTS, the release this runbook targets\n"
    ) in done.stderr
    assert box.changes() == FIRST_RUN
    assert "Suites: noble\n" in (box.state / "tee-docker.sources").read_text()


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
    assert LOCK + "remove -y docker.io" in box.changes()


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
    assert LOCK + "install -y unattended-upgrades" in box.changes()


def test_the_os_release_switch_says_it_was_used(tmp_path: Path) -> None:
    done = Box(tmp_path).run()
    assert "ITS_OS_RELEASE is set" in done.stdout


#: Review N6 (2026-11-27): cloud-init or unattended-upgrades may hold dpkg's lock
#: on a VPS minutes old; each apt-get waits up to 300 s for it.
LOCK = "apt-get -o DPkg::Lock::Timeout=300 "


def test_every_apt_get_waits_for_the_lock_instead_of_stopping(tmp_path: Path) -> None:
    code = [line.strip() for line in SCRIPT.read_text().splitlines()
            if not line.lstrip().startswith("#")]  # fmt: skip
    calls = [line for line in code if line.startswith("apt-get")]
    assert len(calls) == 6, calls
    assert all(line.startswith(LOCK) for line in calls), calls
    box = Box(tmp_path)
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    apt = [c for c in box.calls() if c.startswith("apt-get")]
    assert len(apt) == 6 and all(c.startswith(LOCK) for c in apt), apt
