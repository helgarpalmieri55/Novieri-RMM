"""`deploy/scripts/new-env.sh`: the `.env` of a new host, with nothing left to
substitute by hand (design 2026-11-23-vps-y-marca-novieri §3; `CLAUDE.md`, "A
copy-pasteable command contains no placeholders").

Run for real, against the repository's own `.env.example`, in a scratch copy
of `deploy/`. What it must do: a fresh random value in every `change-me` but
the two OpenBao tokens, the host in every URL, the ACME keys set, mode 0600 --
and print none of what it generated. What it must refuse: an existing `.env`,
a host that is not a DNS name, an address that is not one.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "new-env.sh"
EXAMPLE = REPO / "deploy" / ".env.example"
OPENBAO_TOKENS = {"ITS_OPENBAO_TOKEN", "OPENBAO_TOKEN"}


def _deploy(tmp_path: Path) -> Path:
    deploy = tmp_path / "deploy"
    (deploy / "scripts").mkdir(parents=True)
    (deploy / "scripts" / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
    (deploy / ".env.example").write_bytes(EXAMPLE.read_bytes())
    return deploy


def _run(deploy: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(deploy / "scripts" / SCRIPT.name), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _settings(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def _placeholders() -> set[str]:
    return {key for key, value in _settings(EXAMPLE).items() if value == "change-me"}


ARGS = ("--host", "rmm.novieri.com", "--acme-email", "ops@novieri.example")


def test_the_example_still_has_what_the_script_fills() -> None:
    """Non-vacuity: the six `change-me` and the three ACME keys this task added."""
    example = _settings(EXAMPLE)
    assert _placeholders() == OPENBAO_TOKENS | {
        "POSTGRES_ADMIN_PASSWORD",
        "APP_DB_PASSWORD",
        "PORTAL_DB_PASSWORD",
        "MINIO_ROOT_PASSWORD",
    }
    assert example["ITS_PUBLIC_HOST"] == "your-host"
    assert example["ITS_ACME_EMAIL"] == ""
    assert example["ITS_ACME_CA_SERVER"] == "https://acme-v02.api.letsencrypt.org/directory"
    assert example["ITS_PUBLIC_CONSOLE_URL"] == "https://your-host"
    assert example["ITS_DEFAULT_ORG_SLUG"] == "novieri"
    assert "https://acme-staging-v02.api.letsencrypt.org/directory" in EXAMPLE.read_text()


def test_a_new_env_has_fresh_secrets_the_host_and_mode_600(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    run = _run(deploy, *ARGS)
    assert run.returncode == 0, run.stderr
    env = deploy / ".env"
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    settings = _settings(env)
    for key in _placeholders() - OPENBAO_TOKENS:
        assert re.fullmatch(r"[0-9a-f]{48}", settings[key]), key
    for key in OPENBAO_TOKENS:
        assert settings[key] == "change-me", f"{key} is OpenBao's to issue (README step 3)"
    assert settings["ITS_PUBLIC_HOST"] == "rmm.novieri.com"
    assert settings["ITS_PUBLIC_CONSOLE_URL"] == "https://rmm.novieri.com"
    assert settings["ITS_GATEWAY_URL"] == "wss://rmm.novieri.com:8443/agent"
    assert settings["ITS_GATEWAY_RELAY_URL"] == "wss://rmm.novieri.com:8443/relay"
    assert settings["ITS_S3_PUBLIC_ENDPOINT"] == "https://rmm.novieri.com"
    assert settings["ITS_ACME_EMAIL"] == "ops@novieri.example"
    assert settings["ITS_ACME_CA_SERVER"] == "https://acme-v02.api.letsencrypt.org/directory"
    assert not [k for k, v in settings.items() if "your-host" in v]
    # Everything else is the example's, byte for byte, comments included.
    example_lines = EXAMPLE.read_text().splitlines()
    env_lines = env.read_text().splitlines()
    assert len(env_lines) == len(example_lines)
    assert [line for line in env_lines if line.startswith("#")] == [
        line for line in example_lines if line.startswith("#")
    ]


def test_nothing_it_generated_is_printed(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    run = _run(deploy, *ARGS)
    settings = _settings(deploy / ".env")
    secrets = [settings[key] for key in _placeholders() - OPENBAO_TOKENS]
    assert len(secrets) == 4
    for secret in secrets:
        assert secret not in run.stdout and secret not in run.stderr
    for key in _placeholders() - OPENBAO_TOKENS:
        assert key in run.stdout, "it names the keys it generated"


def test_two_hosts_get_two_sets_of_secrets(tmp_path: Path) -> None:
    first, second = _deploy(tmp_path / "a"), _deploy(tmp_path / "b")
    _run(first, *ARGS)
    _run(second, *ARGS)
    a, b = _settings(first / ".env"), _settings(second / ".env")
    assert a["POSTGRES_ADMIN_PASSWORD"] != b["POSTGRES_ADMIN_PASSWORD"]


def test_an_existing_env_is_never_overwritten(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    (deploy / ".env").write_text("POSTGRES_ADMIN_PASSWORD=the-live-one\n")
    run = _run(deploy, *ARGS)
    assert run.returncode == 1
    assert ".env already exists" in run.stderr
    assert (deploy / ".env").read_text() == "POSTGRES_ADMIN_PASSWORD=the-live-one\n"
    assert [p.name for p in deploy.iterdir() if p.name.startswith(".env.new")] == []


@pytest.mark.parametrize(
    "host",
    ["10.0.0.1", "RMM.novieri.com", "https://rmm.novieri.com", "rmm", "rmm.novieri.com:443", ""],
)
def test_a_host_that_is_not_a_dns_name_is_refused(tmp_path: Path, host: str) -> None:
    deploy = _deploy(tmp_path)
    run = _run(deploy, "--host", host, "--acme-email", "ops@novieri.example")
    assert run.returncode == 1
    assert not (deploy / ".env").exists()


@pytest.mark.parametrize("email", ["ops", "ops@novieri", "ops novieri@x.com", "@novieri.com"])
def test_an_address_that_is_not_one_is_refused(tmp_path: Path, email: str) -> None:
    deploy = _deploy(tmp_path)
    run = _run(deploy, "--host", "rmm.novieri.com", "--acme-email", email)
    assert run.returncode == 1
    assert "name@domain.tld" in run.stderr
    assert not (deploy / ".env").exists()


def test_without_a_terminal_the_address_must_be_given(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    run = _run(deploy, "--host", "rmm.novieri.com")
    assert run.returncode == 1
    assert "--acme-email is required when stdin is not a terminal" in run.stderr


def test_a_rehearsal_names_its_ca(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    staging = "https://acme-staging-v02.api.letsencrypt.org/directory"
    run = _run(deploy, *ARGS, "--acme-ca-server", staging)
    assert run.returncode == 0, run.stderr
    assert _settings(deploy / ".env")["ITS_ACME_CA_SERVER"] == staging
    refused = _run(_deploy(tmp_path / "x"), *ARGS, "--acme-ca-server", "http://pebble:14000/dir")
    assert refused.returncode == 1 and "must be an https URL" in refused.stderr


def test_it_runs_where_the_runbook_runs_it_and_nowhere_else(tmp_path: Path) -> None:
    """`cd "$(dirname "$0")/.."`: from any directory it writes deploy/.env."""
    deploy = _deploy(tmp_path)
    run = subprocess.run(
        ["bash", "deploy/scripts/new-env.sh", *ARGS],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ},
    )
    assert run.returncode == 0, run.stderr
    assert (deploy / ".env").is_file() and not (tmp_path / ".env").exists()


def test_the_temporary_file_a_crash_leaves_is_never_committable() -> None:
    """The four new secrets sit in `.env.new.XXXXXX`, in deploy/, until the
    hard link; the EXIT trap removes it, but a SIGKILL or a power cut does
    not run traps, and `git add -A` would take what is left."""
    for name in ("deploy/.env.new.Ab12Cd", "deploy/.env.new.zZ9yY8"):
        run = subprocess.run(
            ["git", "-C", str(REPO), "check-ignore", "-q", name], capture_output=True
        )
        assert run.returncode == 0, f"{name} is not gitignored"
