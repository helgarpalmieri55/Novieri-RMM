"""The scheduled platform backup, its timers, and the restore drill.

The scripts were run end to end against real containers when written
(backup.sh with each object mode, the nightly run with an age-encrypted S3
off-host copy, and the drill passing on a good backup and failing on a
tampered one). CI has no Docker daemon, so these pin what must fail safe:
retention never deleting the wrong thing, the config file being data, the
off-host copy refusing to go out unencrypted by accident, and the drill being
unable to address the live project.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
SCHEDULED = DEPLOY / "scripts/scheduled-backup.sh"
INSTALL = DEPLOY / "scripts/install-backup-timer.sh"
DRILL = DEPLOY / "scripts/restore-drill.sh"
DRILL_COMPOSE = DEPLOY / "drill/docker-compose.drill.yml"
LIVE_COMPOSE = DEPLOY / "docker-compose.yml"
BACKUP_SH = DEPLOY / "scripts/backup.sh"
CONF_EXAMPLE = DEPLOY / "backup/backup.conf.example"


def _bash(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        stdin=subprocess.DEVNULL,
    )


def _with_functions(body: str) -> str:
    # The script runs main only when executed, so sourcing it defines the
    # functions and the defaults and does nothing else.
    return f'set -euo pipefail\nsource "{SCHEDULED}"\n{body}'


# --- retention --------------------------------------------------------------


def test_retention_keeps_the_newest_and_touches_only_timestamped_dirs(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    for name in (
        "2020-01-01T000000Z",
        "2020-01-02T000000Z",
        "2020-01-03T000000Z",
        "keep-me",
        "2020-01-04",
    ):
        (daily / name).mkdir(parents=True)
    (daily / "2019-01-01T000000Z.txt").write_text("a file, not a backup")

    run = _bash(_with_functions(f'prune_daily "{daily}" 14 1'))

    assert run.returncode == 0, run.stderr
    left = sorted(p.name for p in daily.iterdir())
    assert left == ["2019-01-01T000000Z.txt", "2020-01-03T000000Z", "2020-01-04", "keep-me"]


def test_retention_never_deletes_below_the_minimum(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    for day in range(1, 6):
        (daily / f"2020-01-0{day}T000000Z").mkdir(parents=True)

    run = _bash(_with_functions(f'prune_daily "{daily}" 1 5'))

    assert run.returncode == 0, run.stderr
    assert len(list(daily.iterdir())) == 5


def test_recent_backups_are_kept_whatever_the_minimum(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    today = subprocess.run(
        ["date", "-u", "+%Y-%m-%dT%H%M%SZ"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (daily / today).mkdir(parents=True)
    (daily / "2020-01-01T000000Z").mkdir()

    _bash(_with_functions(f'prune_daily "{daily}" 14 1'))

    assert sorted(p.name for p in daily.iterdir()) == [today]


# --- the config file is data ------------------------------------------------


def _conf(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / "backup.conf"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_the_example_config_parses(tmp_path: Path) -> None:
    conf = _conf(tmp_path, CONF_EXAMPLE.read_text())
    run = _bash(_with_functions(f'load_conf "{conf}"; echo "$OBJECTS $OFFSITE $RETAIN_DAYS"'))
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "all none 14"


def test_an_unknown_setting_is_refused(tmp_path: Path) -> None:
    conf = _conf(tmp_path, "BACKUP_ROOT=/srv/b\nPATH=/tmp/evil\n")
    run = _bash(_with_functions(f'load_conf "{conf}"'))
    assert run.returncode != 0 and "unknown setting 'PATH'" in run.stderr


def test_the_config_is_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    conf = _conf(tmp_path, f"OFFSITE_S3_PREFIX=$(touch {marker})\n")
    run = _bash(_with_functions(f'load_conf "{conf}"; printf "%s" "$OFFSITE_S3_PREFIX"'))
    assert run.returncode == 0, run.stderr
    assert not marker.exists()
    assert run.stdout == f"$(touch {marker})"


def test_a_world_writable_config_is_refused(tmp_path: Path) -> None:
    conf = _conf(tmp_path, "OBJECTS=none\n", mode=0o666)
    run = _bash(_with_functions(f'load_conf "{conf}"'))
    assert run.returncode != 0 and "writable" in run.stderr


def test_a_backup_root_inside_the_repository_is_refused() -> None:
    run = _bash(_with_functions(f'BACKUP_ROOT="{REPO}/backups"; validate'))
    assert run.returncode != 0 and "outside the repository" in run.stderr


def test_a_relative_backup_root_is_refused() -> None:
    run = _bash(_with_functions('BACKUP_ROOT="backups"; validate'))
    assert run.returncode != 0 and "absolute" in run.stderr


def test_an_unencrypted_offsite_copy_needs_an_explicit_yes(tmp_path: Path) -> None:
    """globals.sql holds the roles' password hashes; it does not leave the
    host in the clear by default."""
    run = _bash(_with_functions(f'offsite_payload "{tmp_path}" ts'))
    assert run.returncode != 0 and "refused" in run.stderr

    allowed = _bash(
        _with_functions(f'OFFSITE_ALLOW_UNENCRYPTED=yes; offsite_payload "{tmp_path}" ts')
    )
    assert allowed.returncode == 0 and allowed.stdout.strip() == str(tmp_path)


def test_the_s3_secret_never_reaches_an_argv() -> None:
    text = SCHEDULED.read_text()
    for line in text.splitlines():
        if "OFFSITE_S3_SECRET_KEY" in line and "docker" in line:
            raise AssertionError(line)
    assert '"$OFFSITE_S3_SECRET_KEY" > "$cfg/config.json"' in text


# --- backup.sh object modes ---------------------------------------------------


def test_backup_sh_refuses_an_unknown_object_mode_before_any_dump(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    run = _bash(
        f'"{BACKUP_SH}" "{tmp_path}/bk"',
        ITS_BACKUP_OBJECTS="everything",
        ITS_PG_EXEC=f"touch {calls}",
    )
    assert run.returncode != 0 and "ITS_BACKUP_OBJECTS" in run.stderr
    assert not calls.exists(), "a database was dumped before the mode was checked"


def test_backup_sh_defaults_to_databases_only_and_excludes_fleet_data_by_prefix() -> None:
    text = BACKUP_SH.read_text()
    assert ': "${ITS_BACKUP_OBJECTS:=none}"' in text
    assert 'exclude="*/backup/*"' in text


# --- the timers ---------------------------------------------------------------


def _install(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _bash(
        f'"{INSTALL}" {" ".join(args)}',
        ITS_SYSTEMD_DIR=str(tmp_path / "systemd"),
        ITS_ETC_DIR=str(tmp_path / "etc"),
        ITS_NO_SYSTEMCTL="1",
    )


def test_the_installer_writes_both_timers_and_a_private_config(tmp_path: Path) -> None:
    run = _install(tmp_path)
    assert run.returncode == 0, run.stderr

    units = tmp_path / "systemd"
    names = sorted(p.name for p in units.iterdir())
    assert names == [
        "its-platform-backup.service",
        "its-platform-backup.timer",
        "its-restore-drill.service",
        "its-restore-drill.timer",
    ]
    service = (units / "its-platform-backup.service").read_text()
    assert f"ExecStart={DEPLOY}/scripts/scheduled-backup.sh" in service
    assert "OnCalendar=*-*-* 02:30:00" in (units / "its-platform-backup.timer").read_text()
    assert "OnCalendar=*-*-01" in (units / "its-restore-drill.timer").read_text()
    assert f"ExecStart={DEPLOY}/scripts/restore-drill.sh" in (
        units / "its-restore-drill.service"
    ).read_text()

    conf = tmp_path / "etc/backup.conf"
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600


def test_the_installer_never_overwrites_the_operators_config(tmp_path: Path) -> None:
    _install(tmp_path)
    conf = tmp_path / "etc/backup.conf"
    conf.write_text("OFFSITE=s3\n")

    run = _install(tmp_path)

    assert run.returncode == 0, run.stderr
    assert conf.read_text() == "OFFSITE=s3\n"


def test_no_drill_installs_the_backup_only(tmp_path: Path) -> None:
    run = _install(tmp_path, "--no-drill")
    assert run.returncode == 0, run.stderr
    assert not (tmp_path / "systemd/its-restore-drill.timer").exists()


# --- the drill cannot reach the live stack --------------------------------------


def test_the_drill_compose_is_isolated_by_construction() -> None:
    drill = yaml.safe_load(DRILL_COMPOSE.read_text())
    live = yaml.safe_load(LIVE_COMPOSE.read_text())

    assert drill["name"] == "itms-restore-drill"
    assert drill["name"] != live["name"]
    for name, service in drill["services"].items():
        assert "ports" not in service, f"{name} publishes a port"
        assert "container_name" not in service, name
        assert "network_mode" not in service, name
        for volume in service.get("volumes", []):
            source = str(volume).split(":")[0]
            assert source.startswith(("drill-", "../postgres/init-app-user.sh")), volume
            assert source not in live.get("volumes", {}), f"{name} mounts live volume {source}"
    for volume in drill.get("volumes", {}).values() or ():
        assert not (volume or {}).get("external"), "the drill must not use an external volume"
    assert "networks" not in drill or not any(
        (n or {}).get("external") for n in drill["networks"].values()
    )


def test_the_drill_runs_the_images_the_live_stack_pins() -> None:
    drill = yaml.safe_load(DRILL_COMPOSE.read_text())["services"]
    live = yaml.safe_load(LIVE_COMPOSE.read_text())["services"]
    assert drill["postgres"]["image"] == live["postgres"]["image"]
    assert drill["keycloak"]["image"] == live["keycloak"]["image"]


def test_every_drill_compose_call_is_pinned_to_the_drill_project() -> None:
    text = DRILL.read_text()
    assert "readonly DRILL_PROJECT=itms-restore-drill" in text
    assert "env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "docker compose" not in stripped:
            continue
        assert '-p "$DRILL_PROJECT"' in stripped or "-p $DRILL_PROJECT" in stripped, line


def test_the_drill_refuses_a_compose_file_naming_the_live_project(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    (deploy / "scripts").mkdir(parents=True)
    (deploy / "drill").mkdir()
    shutil.copy(DRILL, deploy / "scripts/restore-drill.sh")
    shutil.copy(LIVE_COMPOSE, deploy / "docker-compose.yml")
    (deploy / "drill/docker-compose.drill.yml").write_text(
        DRILL_COMPOSE.read_text().replace("name: itms-restore-drill", "name: it-suite")
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/docker-calls"\n')
    fake.chmod(0o755)

    status = tmp_path / "status"
    run = _bash(
        f'"{deploy}/scripts/restore-drill.sh" "{tmp_path}"',
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        ITS_BACKUP_STATUS_DIR=str(status),
        ITS_BACKUP_STATE_DIR=str(tmp_path / "state"),
    )

    assert run.returncode != 0 and "REFUSED" in run.stderr
    assert not (tmp_path / "docker-calls").exists(), "docker was called before the guard"
    # A drill that could not run did not pass: the platform is told.
    assert json.loads((status / "drill.json").read_text())["result"] == "fail"


# --- results the platform reads (app/core/health/backup_status.py) -------------


def _status_run(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return _bash(
        _with_functions(
            f'STATE_DIR="{state}"; STATUS_DIR="{tmp_path}/status"\n{body}\nrecord_status'
        )
    )


def test_a_successful_night_is_recorded_for_the_platform(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state/last-success").write_text("2026-09-23T02:31:00Z /var/backups/x\n")
    (tmp_path / "state/last-offsite").write_text("2026-09-23T02:40:00Z s3 x.tar.age\n")

    run = _status_run(tmp_path, "CONF_OK=1; BACKUP_DONE=1; OFFSITE=s3; OFFSITE_DONE=1")

    assert run.returncode == 0, run.stderr
    status = tmp_path / "status"
    backup = json.loads((status / "backup.json").read_text())
    assert backup["result"] == "ok"
    assert backup["last_success"] == "2026-09-23T02:31:00Z"
    offsite = json.loads((status / "offsite.json").read_text())
    assert offsite == {
        "configured": True,
        "mode": "s3",
        "result": "ok",
        "at": offsite["at"],
        "last_success": "2026-09-23T02:40:00Z",
    }
    # Read by a non-root user in the container, through a read-only mount.
    assert stat.S_IMODE(status.stat().st_mode) == 0o755
    assert stat.S_IMODE((status / "backup.json").stat().st_mode) == 0o644
    assert not [p for p in status.iterdir() if p.name.startswith(".")], "temp file left behind"


def test_a_failed_offsite_copy_is_recorded(tmp_path: Path) -> None:
    run = _status_run(tmp_path, "CONF_OK=1; BACKUP_DONE=1; OFFSITE=rsync; OFFSITE_DONE=0")
    assert run.returncode == 0, run.stderr
    offsite = json.loads((tmp_path / "status/offsite.json").read_text())
    assert (offsite["result"], offsite["last_success"]) == ("failed", None)


def test_offsite_none_clears_the_offsite_expectation(tmp_path: Path) -> None:
    run = _status_run(tmp_path, "CONF_OK=1; BACKUP_DONE=1; OFFSITE=none")
    assert run.returncode == 0, run.stderr
    offsite = json.loads((tmp_path / "status/offsite.json").read_text())
    assert offsite["configured"] is False


def test_the_first_run_is_remembered(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state/first-run").write_text("2026-01-01T00:00:00Z\n")
    _status_run(tmp_path, "CONF_OK=1; BACKUP_DONE=1; OFFSITE=none")
    backup = json.loads((tmp_path / "status/backup.json").read_text())
    assert backup["first_run"] == "2026-01-01T00:00:00Z"


def test_a_failed_run_is_recorded_and_keeps_its_exit_code(tmp_path: Path) -> None:
    conf = _conf(tmp_path, "NOT_A_SETTING=1\n")
    run = _bash(
        f'"{SCHEDULED}"',
        ITS_BACKUP_CONF=str(conf),
        ITS_BACKUP_STATE_DIR=str(tmp_path / "state"),
        ITS_BACKUP_STATUS_DIR=str(tmp_path / "status"),
    )

    assert run.returncode == 1
    backup = json.loads((tmp_path / "status/backup.json").read_text())
    assert backup["result"] == "failed"
    assert backup["last_success"] is None
    assert not (tmp_path / "status/offsite.json").exists(), "config never loaded"


def test_what_the_scripts_write_is_what_the_probes_read(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from app.core.health.backup_status import evaluate_backup, evaluate_offsite
    from app.core.health.types import ReasonCode, Status

    (tmp_path / "state").mkdir()
    now = datetime.now(UTC)
    stamp = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "state/last-success").write_text(f"{stamp} /x\n")
    _status_run(tmp_path, "CONF_OK=1; BACKUP_DONE=1; OFFSITE=s3; OFFSITE_DONE=0")

    status = tmp_path / "status"
    assert evaluate_backup(status, now=now).status is Status.OK
    offsite = evaluate_offsite(status, now=now)
    assert (offsite.status, offsite.reason) == (Status.DOWN, ReasonCode.STALE)
