"""`deploy/scripts/preflight.sh`, run for real against a stubbed host
(design 2026-11-23-vps-y-marca-novieri §2, §3).

Since 2026-11-23 there is no certificate file to check: Traefik asks Let's
Encrypt for one at the first start. What would make that request fail -- or
succeed for the wrong machine -- is checked before it is made: the public
name is the console's, it resolves to this host and has no AAAA record, 443
and 8443 are free, and Docker is new enough. `getent`, `python3`, `ip`, `ss`
and `docker` are stubs on PATH, each answering what the test sets; nothing
here resolves a name or opens a socket.

The A record is asked of DNS alone, as Let's Encrypt asks it: the `getent` stub
answers `-s dns` from FAKE_V4 and, without it, lets /etc/hosts
(FAKE_HOSTS_FILE) answer first, as `hosts: files dns` does; and, like glibc's
`getent ahosts` (AI_ADDRCONFIG), it shows no AAAA on a host with no IPv6
address of its own. The AAAA is asked through `python3` (getaddrinfo with no
AI_ADDRCONFIG), whose stub answers FAKE_V6 whatever the host holds.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "deploy" / "scripts" / "preflight.sh"

GOOD_ENV = {
    "IT_SUITE_VERSION": "1bc0e5e",
    "ITMS_BIND_IP": "0.0.0.0",
    "ITS_PUBLIC_HOST": "rmm.novieri.com",
    "ITS_PUBLIC_CONSOLE_URL": "https://rmm.novieri.com",
    "ITS_ACME_EMAIL": "ops@novieri.example",
    "ITS_ACME_CA_SERVER": "https://acme-v02.api.letsencrypt.org/directory",
    "POSTGRES_ADMIN_PASSWORD": "x" * 48,
    "ITS_OPENBAO_TOKEN": "change-me",
    "OPENBAO_TOKEN": "change-me",
}
GOOD_HOST = {
    "FAKE_V4": "203.0.113.10",
    "FAKE_V6": "",
    "FAKE_MINE": "203.0.113.10/24 172.17.0.1/16",
    "FAKE_LISTEN": "",
    "FAKE_TRAEFIK": "",
    "FAKE_ENGINE": "29.9.3",
    "FAKE_COMPOSE": "5.7.1",
}

STUBS = {
    "getent": """#!/bin/sh
if [ "$1" = "-s" ]; then
  [ "$2" = "dns" ] || exit 1
  shift 2
  found="$FAKE_V4"
else
  found="${FAKE_HOSTS_FILE:-$FAKE_V4}"
fi
case "$1" in
  ahostsv4) ;;
  ahosts) case " $FAKE_MINE " in *:*) found="$found $FAKE_V6" ;; esac ;;
  *) exit 1 ;;
esac
# Like glibc's getent: nothing found is exit 2.
[ -n "$(echo $found)" ] || exit 2
for a in $found; do echo "$a       STREAM $2"; done
""",
    "python3": """#!/bin/sh
echo $FAKE_V6
""",
    "ip": """#!/bin/sh
case "$*" in
  "-brief address") for a in $FAKE_MINE; do echo "eth0 UP $a"; done ;;
  *) for a in $FAKE_MINE; do
       echo "2: eth0    inet $a brd 0 scope global eth0"
     done ;;
esac
""",
    "ss": """#!/bin/sh
for a in $FAKE_LISTEN; do echo "LISTEN 0 4096 $a 0.0.0.0:*"; done
""",
    "docker": """#!/bin/sh
case "$*" in
  "version --format {{.Server.Version}}") [ -n "$FAKE_ENGINE" ] && echo "$FAKE_ENGINE" || exit 1 ;;
  "compose version --short") [ -n "$FAKE_COMPOSE" ] && echo "$FAKE_COMPOSE" || exit 1 ;;
  "compose ps --status running -q traefik") [ -n "$FAKE_TRAEFIK" ] && echo "$FAKE_TRAEFIK" ;;
  *) exit 1 ;;
esac
""",
}


def _run(
    tmp_path: Path, env: dict[str, str], host: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    deploy = tmp_path / "repo" / "deploy"
    (deploy / "scripts").mkdir(parents=True, exist_ok=True)
    (deploy / "scripts" / "preflight.sh").write_bytes(PREFLIGHT.read_bytes())
    (tmp_path / "repo" / ".gitignore").write_text(".env\n")
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    dotenv = deploy / ".env"
    dotenv.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    dotenv.chmod(0o600)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return subprocess.run(
        ["bash", str(deploy / "scripts" / "preflight.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, **host, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )


def test_a_host_ready_for_its_first_start_passes(tmp_path: Path) -> None:
    run = _run(tmp_path, GOOD_ENV, GOOD_HOST)
    assert run.returncode == 0, run.stdout + run.stderr
    for line in (
        "ok      ITS_PUBLIC_HOST=rmm.novieri.com is the console's host",
        "ok      rmm.novieri.com resolves to 203.0.113.10, an address of this host",
        "ok      443 and 8443 are free",
        "ok      Docker Engine 29.9.3",
        "ok      Docker Compose 5.7.1",
        "ok      ACME CA: Let's Encrypt production",
    ):
        assert line in run.stdout, run.stdout
    assert "certs/" not in run.stdout, "the certificate files are gone (design §2)"


@pytest.mark.parametrize(
    ("env", "host", "refusal"),
    [
        pytest.param(
            {"ITS_PUBLIC_HOST": "your-host"},
            {},
            "ITS_PUBLIC_HOST is 'your-host'",
            id="placeholder-host",
        ),
        pytest.param(
            {"ITS_PUBLIC_CONSOLE_URL": "https://console.novieri.com"},
            {},
            "is not the host of ITS_PUBLIC_CONSOLE_URL ('console.novieri.com')",
            id="host-differs-from-console",
        ),
        pytest.param({"ITS_ACME_EMAIL": ""}, {}, "ITS_ACME_EMAIL is empty", id="no-acme-email"),
        pytest.param(
            {"ITS_ACME_CA_SERVER": "http://pebble:14000/dir"},
            {},
            "is not an https URL",
            id="http-ca",
        ),
        pytest.param({}, {"FAKE_V4": ""}, "does not resolve to an IPv4 address", id="no-a-record"),
        pytest.param(
            {},
            {"FAKE_V4": "198.51.100.7"},
            "which is not an address of this host",
            id="a-elsewhere",
        ),
        pytest.param(
            {}, {"FAKE_V6": "2001:db8::7"}, "has an AAAA record (2001:db8::7)", id="aaaa-record"
        ),
        pytest.param(
            {},
            {"FAKE_LISTEN": "0.0.0.0:443"},
            "something already listens on 0.0.0.0:443",
            id="443-held",
        ),
        pytest.param(
            {}, {"FAKE_ENGINE": "24.0.7"}, "Docker Engine 24.0.7 is older than 26", id="old-engine"
        ),
        pytest.param(
            {},
            {"FAKE_COMPOSE": "2.20.2"},
            "Docker Compose 2.20.2 is older than 2.24",
            id="old-compose",
        ),
        pytest.param({}, {"FAKE_ENGINE": ""}, "Docker Engine does not answer", id="no-docker"),
    ],
)
def test_each_state_that_would_fail_the_certificate_is_refused(
    tmp_path: Path, env: dict[str, str], host: dict[str, str], refusal: str
) -> None:
    run = _run(tmp_path, {**GOOD_ENV, **env}, {**GOOD_HOST, **host})
    assert run.returncode == 1, run.stdout
    assert "REFUSE" in run.stdout and refusal in run.stdout, run.stdout


def test_this_stacks_own_traefik_holding_the_ports_is_not_a_refusal(tmp_path: Path) -> None:
    run = _run(
        tmp_path, GOOD_ENV, {**GOOD_HOST, "FAKE_LISTEN": "0.0.0.0:443", "FAKE_TRAEFIK": "c0ffee"}
    )
    assert run.returncode == 0, run.stdout
    assert "ok      443 and 8443 are held by this stack's traefik" in run.stdout


def test_a_bound_address_is_the_one_the_name_must_resolve_to(tmp_path: Path) -> None:
    env = {**GOOD_ENV, "ITMS_BIND_IP": "203.0.113.20"}
    host = {**GOOD_HOST, "FAKE_MINE": "203.0.113.10/24 203.0.113.20/24"}
    run = _run(tmp_path, env, host)
    assert (
        run.returncode == 1 and "which is not an address of this host (203.0.113.20)" in run.stdout
    )
    run = _run(tmp_path, env, {**host, "FAKE_V4": "203.0.113.20"})
    assert run.returncode == 0, run.stdout


def test_behind_nat_the_address_cannot_be_compared_and_it_says_so(tmp_path: Path) -> None:
    run = _run(
        tmp_path, GOOD_ENV, {**GOOD_HOST, "FAKE_V4": "198.51.100.7", "FAKE_MINE": "10.0.0.4/24"}
    )
    assert run.returncode == 0, run.stdout
    assert "every address of this host is private (10.0.0.4)" in run.stdout


def test_a_rehearsal_ca_is_noted_not_refused(tmp_path: Path) -> None:
    staging = "https://acme-staging-v02.api.letsencrypt.org/directory"
    run = _run(tmp_path, {**GOOD_ENV, "ITS_ACME_CA_SERVER": staging}, GOOD_HOST)
    assert run.returncode == 0, run.stdout
    assert f"note    ACME CA is {staging}" in run.stdout


def test_an_etc_hosts_line_for_the_name_is_not_what_lets_encrypt_sees(tmp_path: Path) -> None:
    """cloud-init writes `127.0.1.1 <this host's name>` into /etc/hosts: no
    false refusal when the VPS is named rmm.novieri.com. And a line written
    by hand does not stand in for the public A record either."""
    run = _run(tmp_path, GOOD_ENV, {**GOOD_HOST, "FAKE_HOSTS_FILE": "127.0.1.1"})
    assert run.returncode == 0, run.stdout
    assert "ok      rmm.novieri.com resolves to 203.0.113.10, an address of this host" in run.stdout
    run = _run(tmp_path, GOOD_ENV, {**GOOD_HOST, "FAKE_HOSTS_FILE": "203.0.113.10", "FAKE_V4": ""})
    assert run.returncode == 1, run.stdout
    assert "does not resolve to an IPv4 address" in run.stdout, run.stdout


def test_an_aaaa_record_is_seen_on_a_host_with_no_ipv6_of_its_own(tmp_path: Path) -> None:
    """The VPS this runbook builds has one IPv4 address and no IPv6, the host
    on which glibc's getent hides every AAAA (AI_ADDRCONFIG)."""
    host = {**GOOD_HOST, "FAKE_V6": "2001:db8::7", "FAKE_MINE": "203.0.113.10/24"}
    run = _run(tmp_path, GOOD_ENV, host)
    assert run.returncode == 1, run.stdout
    assert "has an AAAA record (2001:db8::7)" in run.stdout, run.stdout
