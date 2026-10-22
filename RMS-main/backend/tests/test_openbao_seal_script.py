"""`deploy/scripts/openbao-seal.sh` and the optional auto-unseal override.

The migration itself (Shamir -> static -> Shamir, with a transit key surviving
both and a restart proving the unseal) was run against a real OpenBao 2.6.1
when this was written; CI has no Docker daemon for it. What is pinned here is
what fails safe: the refusals, the OFF default, and that no key share ever
reaches a command line.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy/scripts/openbao-seal.sh"
OVERRIDE = REPO / "deploy/openbao/auto-unseal.yml"
STATIC_HCL = REPO / "deploy/openbao/seal/static.hcl"
DISABLED_HCL = REPO / "deploy/openbao/seal/static-disabled.hcl"
ENV_EXAMPLE = REPO / "deploy/.env.example"

_FAKE_DOCKER = """#!/usr/bin/env bash
case "$*" in
  info*) echo /var/lib/docker ;;
  *"bao status"*) printf '%s' "$FAKE_BAO_JSON"; exit "${FAKE_BAO_RC:-0}" ;;
  *) echo "docker $*" >> "$FAKE_LOG" ;;
esac
"""

_SHAMIR = '{"type": "shamir", "sealed": false, "storage_type": "file"}'


def _run(tmp: Path, *args: str, env_file: str = "", bao_json: str = _SHAMIR, bao_rc: int = 0):
    deploy = tmp / "repo" / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    (deploy / ".env").write_text(env_file)
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "docker"
    fake.write_text(_FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ITS_DEPLOY_DIR": str(deploy),
        "FAKE_BAO_JSON": bao_json,
        "FAKE_BAO_RC": str(bao_rc),
        "FAKE_LOG": str(tmp / "docker.log"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def _nothing_changed(tmp: Path) -> bool:
    log = tmp / "docker.log"
    return not log.exists() or not re.search(r"\b(up|stop|restart|run)\b", log.read_text())


def test_the_script_parses() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_a_relative_key_path_is_refused(tmp_path: Path) -> None:
    run = _run(tmp_path, "enable", "--key-file", "keys/unseal.key", "--yes")
    assert run.returncode != 0 and "absolute" in run.stderr
    assert _nothing_changed(tmp_path)


def test_a_key_inside_the_repository_is_refused(tmp_path: Path) -> None:
    inside = tmp_path / "repo" / "secrets" / "unseal.key"
    run = _run(tmp_path, "enable", "--key-file", str(inside), "--yes")
    assert run.returncode != 0 and "outside the repository" in run.stderr


def test_a_key_under_the_docker_data_root_is_refused(tmp_path: Path) -> None:
    """Where the Postgres, MinIO and OpenBao volumes live: a disk image or a
    volume backup would carry the key with the data it unlocks."""
    run = _run(tmp_path, "enable", "--key-file", "/var/lib/docker/volumes/k/unseal.key", "--yes")
    assert run.returncode != 0 and "Docker's data root" in run.stderr


def test_a_key_under_the_backup_root_is_refused(tmp_path: Path) -> None:
    run = _run(tmp_path, "enable", "--key-file", "/var/backups/it-suite/unseal.key", "--yes")
    assert run.returncode != 0 and "backup root" in run.stderr


def test_a_sealed_openbao_is_refused_before_anything_changes(tmp_path: Path) -> None:
    key = tmp_path / "keys" / "unseal.key"
    run = _run(
        tmp_path, "enable", "--key-file", str(key), "--yes",
        bao_json='{"type": "shamir", "sealed": true}', bao_rc=2,
    )
    assert run.returncode != 0 and "sealed" in run.stderr
    assert not key.exists(), "a key was generated for a migration that never started"
    assert _nothing_changed(tmp_path)


def test_an_operators_own_compose_file_setting_is_never_overwritten(tmp_path: Path) -> None:
    key = tmp_path / "keys" / "unseal.key"
    run = _run(
        tmp_path, "enable", "--key-file", str(key), "--yes",
        env_file="COMPOSE_FILE=docker-compose.yml:my-override.yml\n",
    )
    assert run.returncode != 0 and "Merge by hand" in run.stderr
    env = (tmp_path / "repo/deploy/.env").read_text()
    assert env == "COMPOSE_FILE=docker-compose.yml:my-override.yml\n"


def test_disable_without_auto_unseal_configured_changes_nothing(tmp_path: Path) -> None:
    run = _run(tmp_path, "disable", "--yes")
    assert run.returncode != 0 and "not configured" in run.stderr
    assert _nothing_changed(tmp_path)


def test_status_reports_off_by_default(tmp_path: Path) -> None:
    run = _run(tmp_path, "status")
    assert run.returncode == 0, run.stderr
    assert "OFF" in run.stdout and "unsealed shamir" in run.stdout


def test_no_share_is_ever_passed_as_an_argument() -> None:
    """A share on argv is readable by every user via /proc/<pid>/cmdline."""
    text = SCRIPT.read_text()
    for line in text.splitlines():
        if "bao operator unseal" in line or "sys/unseal" in line:
            assert "$share" not in line.split("|")[-1], line
    assert re.search(
        r"printf '\{\"key\":\"%s\"[^\n]*\"\$share\"[^\n]*\\\n"
        r"\s*\|[^\n]*bao write[^\n]*sys/unseal -",
        text,
    ), "the non-interactive path no longer pipes the share on stdin"


def test_auto_unseal_is_off_by_default() -> None:
    """The override is loaded only through COMPOSE_FILE in .env, which only the
    migration script writes."""
    for line in ENV_EXAMPLE.read_text().splitlines():
        assert not line.startswith("COMPOSE_FILE="), line
        assert not line.startswith("OPENBAO_UNSEAL_KEY_FILE="), line


def test_the_override_mounts_the_key_read_only_and_never_creates_it() -> None:
    service = yaml.safe_load(OVERRIDE.read_text())["services"]["openbao"]
    binds = [v for v in service["volumes"] if isinstance(v, dict)]
    assert len(binds) == 1
    key = binds[0]
    assert key["read_only"] is True
    assert key["bind"]["create_host_path"] is False, (
        "a missing key file would become an empty directory and OpenBao would "
        "start without its seal key"
    )
    assert key["target"] == "/bao/unseal/unseal.key"
    assert ":?" in key["source"], "an unset key path must fail compose, not mount nothing"
    assert "-config=/bao/config.hcl" in service["command"]


def test_both_seal_stanzas_read_the_mounted_key() -> None:
    for hcl in (STATIC_HCL, DISABLED_HCL):
        text = hcl.read_text()
        assert 'seal "static"' in text
        assert 'current_key    = "file:///bao/unseal/unseal.key"' in text
    assert 'disabled       = "true"' in DISABLED_HCL.read_text()
    assert "disabled" not in STATIC_HCL.read_text().split("seal")[-1]
