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


# --- the old identity provider's database (operator decision K1, 2026-11-15) ---
#
# Own authentication phase 4 removed Keycloak. A host upgraded from before keeps
# its `keycloak` database until the operator retires it, and that database holds
# Keycloak's password hashes and the realm keys that signed its tokens: only a
# backup taken before an upgrade (the way back; deploy.sh, and the manual
# upgrade's step 1, set ITS_BACKUP_OLD_IDENTITY_DB=1) keeps it, and
# restore.sh still restores a backup that has it. These run the real scripts
# against fake client tools that answer from FAKE_DBS.

RESTORE_SH = DEPLOY / "scripts/restore.sh"
_FAKE_PG = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
cmd="$1"; shift
case "$cmd" in
  pg_dumpall) printf 'CREATE ROLE its_admin;\nCREATE ROLE app_user;\nCREATE ROLE portal_user;\n' ;;
  pg_dump)
    db="${*: -1}"
    [ "$db" = keycloak ] && [ "${FAKE_EMPTY_KEYCLOAK:-0}" = 1 ] && exit 0
    echo "dump of $db" ;;
  pg_restore) cat > /dev/null ;;
  psql)
    case "$*" in
      *"datname = 'keycloak'"*|*"datname='keycloak'"*)
        case " $FAKE_DBS " in *" keycloak "*) echo 1 ;; *) echo 0 ;; esac ;;
      *"datname='itsuite'"*) echo 1 ;;
      *information_schema.tables*) echo 0 ;;
      *version_num*) echo 0248 ;;
      *pg_policies*) echo 133 ;;
      *relforcerowsecurity*) echo 122 ;;
      *role_table_grants*) echo 0 ;;
      *string_agg*) echo ;;
      *" -f -"*) cat > /dev/null ;;
      *) echo 1 ;;
    esac ;;
esac
"""


def _pg(tmp_path: Path, dbs: str, **env: str) -> dict[str, str]:
    fake = tmp_path / "fakepg.sh"
    fake.write_text(_FAKE_PG)
    log = str(tmp_path / "calls")
    return {"ITS_PG_EXEC": f"bash {fake}", "FAKE_LOG": log, "FAKE_DBS": dbs, **env}


def test_a_nightly_backup_leaves_the_old_identity_providers_database_out(tmp_path: Path) -> None:
    """While the database is still there, and says so: the reminder that it is."""
    run = _bash(f'"{BACKUP_SH}" "{tmp_path}/nightly"', **_pg(tmp_path, "itsuite keycloak"))
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "nightly/itsuite.dump").read_text() == "dump of itsuite\n"
    assert not (tmp_path / "nightly/keycloak.dump").exists()
    assert "pg_dump -Fc -U its_admin keycloak" not in (tmp_path / "calls").read_text()
    assert "Retiring the old identity provider's data" in run.stdout, run.stdout


def test_the_pre_upgrade_backup_keeps_it_while_it_exists(tmp_path: Path) -> None:
    flag = {"ITS_BACKUP_OLD_IDENTITY_DB": "1"}
    kept = _bash(f'"{BACKUP_SH}" "{tmp_path}/kept"', **_pg(tmp_path, "itsuite keycloak", **flag))
    assert kept.returncode == 0, kept.stderr
    assert (tmp_path / "kept/keycloak.dump").read_text() == "dump of keycloak\n"
    gone = _bash(f'"{BACKUP_SH}" "{tmp_path}/gone"', **_pg(tmp_path, "itsuite", **flag))
    assert gone.returncode == 0, gone.stderr
    assert not (tmp_path / "gone/keycloak.dump").exists()
    assert "--> keycloak" not in gone.stdout, gone.stdout


def test_an_empty_dump_of_it_fails_the_backup(tmp_path: Path) -> None:
    env = _pg(tmp_path, "itsuite keycloak", ITS_BACKUP_OLD_IDENTITY_DB="1", FAKE_EMPTY_KEYCLOAK="1")
    run = _bash(f'"{BACKUP_SH}" "{tmp_path}/bk"', **env)
    assert run.returncode != 0
    assert "keycloak.dump is missing or empty" in run.stderr, run.stderr


def test_the_flag_is_zero_or_one_and_checked_before_any_dump(tmp_path: Path) -> None:
    env = _pg(tmp_path, "itsuite", ITS_BACKUP_OLD_IDENTITY_DB="yes")
    run = _bash(f'"{BACKUP_SH}" "{tmp_path}/bk"', **env)
    assert run.returncode != 0 and "ITS_BACKUP_OLD_IDENTITY_DB" in run.stderr
    assert not (tmp_path / "calls").exists(), "a database was dumped before the flag was checked"


def test_only_deploy_sh_asks_for_it_and_the_nightly_run_says_no() -> None:
    deploy_sh = (DEPLOY / "scripts/deploy.sh").read_text()
    assert 'ITS_BACKUP_OLD_IDENTITY_DB=1 ./scripts/backup.sh "$BACKUP_DIR"' in deploy_sh
    nightly = SCHEDULED.read_text()
    assert 'ITS_BACKUP_OLD_IDENTITY_DB=0 "$DEPLOY_DIR/scripts/backup.sh" "$dest"' in nightly


def test_a_backup_with_the_old_database_restores_it_and_the_next_backup_has_none(
    tmp_path: Path,
) -> None:
    """A pre-upgrade backup, restored on a host that had already dropped the
    database: restore.sh creates it, restores it and says what it is. The
    nightly backup after that leaves it out again."""
    flag = {"ITS_BACKUP_OLD_IDENTITY_DB": "1"}
    pre = _bash(f'"{BACKUP_SH}" "{tmp_path}/pre"', **_pg(tmp_path, "itsuite keycloak", **flag))
    assert pre.returncode == 0, pre.stderr
    (tmp_path / "calls").unlink()
    restored = _bash(f'"{RESTORE_SH}" "{tmp_path}/pre"', **_pg(tmp_path, "itsuite"))
    assert restored.returncode == 0, restored.stdout + restored.stderr
    calls = (tmp_path / "calls").read_text()
    assert "CREATE DATABASE keycloak OWNER its_admin" in calls, calls
    assert "pg_restore -U its_admin -d keycloak" in calls, calls
    assert "keep it if you are rolling back" in restored.stdout, restored.stdout
    assert restored.stdout.rstrip().endswith("==> OK"), restored.stdout
    after = _bash(f'"{BACKUP_SH}" "{tmp_path}/after"', **_pg(tmp_path, "itsuite keycloak"))
    assert after.returncode == 0, after.stderr
    assert not (tmp_path / "after/keycloak.dump").exists()


def test_a_backup_without_it_restores_without_touching_it(tmp_path: Path) -> None:
    assert _bash(f'"{BACKUP_SH}" "{tmp_path}/bk"', **_pg(tmp_path, "itsuite")).returncode == 0
    (tmp_path / "calls").unlink()
    restored = _bash(f'"{RESTORE_SH}" "{tmp_path}/bk"', **_pg(tmp_path, "itsuite keycloak"))
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert "keycloak" not in (tmp_path / "calls").read_text()
    assert "keycloak.dump restored" not in restored.stdout


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
    # Own authentication phase 4: the drill no longer starts the old identity
    # provider; a `keycloak.dump` is checked in the restored database instead.
    assert set(drill) == {"postgres"}, set(drill)


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


# --- the drill since own authentication phase 4 (fix round 1) -----------------
#
# The real restore-drill.sh, in a copy of deploy/ with a fake `docker` on PATH
# and a fake restore.sh: the drill's own checks, against answers the test
# chooses. The fake psql answers a healthy restored database, and for the
# `keycloak` database the table count in FAKE_KC_TABLES ("fail" exits 1).

_FAKE_DRILL_DOCKER = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$*" in
  *" psql "*"-d keycloak"*)
    [ "$FAKE_KC_TABLES" = fail ] && exit 1
    echo "$FAKE_KC_TABLES" ;;
  *"IS DISTINCT FROM"*) echo 0 ;;
  *"SELECT version_num"*) echo 0248 ;;
  *rolbypassrls*) echo f ;;
  *"SELECT count(*) FROM "*) echo 1 ;;
esac
exit 0
"""


def _drill(
    tmp_path: Path, *args: str, keycloak_dump: bool, kc_tables: str = "0"
) -> subprocess.CompletedProcess[str]:
    deploy = tmp_path / "deploy"
    (deploy / "scripts").mkdir(parents=True)
    (deploy / "drill").mkdir()
    shutil.copy(DRILL, deploy / "scripts/restore-drill.sh")
    shutil.copy(LIVE_COMPOSE, deploy / "docker-compose.yml")
    shutil.copy(DRILL_COMPOSE, deploy / "drill/docker-compose.drill.yml")
    restore = deploy / "scripts/restore.sh"
    restore.write_text('#!/bin/sh\necho "restore.sh $*" >> "$FAKE_LOG"\n')
    restore.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(_FAKE_DRILL_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    backup = tmp_path / "backups/daily/2026-11-20T020000Z"
    backup.mkdir(parents=True)
    for name, body in (
        ("itsuite.dump", "dump"),
        ("globals.sql", "CREATE ROLE its_admin;"),
        ("alembic_version", "0248\n"),
        ("policy_count", "133\n"),
    ):
        (backup / name).write_text(body)
    if keycloak_dump:
        (backup / "keycloak.dump").write_text("dump")
    return _bash(
        " ".join(f'"{a}"' for a in (f"{deploy}/scripts/restore-drill.sh", *args, backup)),
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        FAKE_LOG=str(tmp_path / "docker-calls"),
        FAKE_KC_TABLES=kc_tables,
        ITS_BACKUP_STATUS_DIR=str(tmp_path / "status"),
        ITS_BACKUP_STATE_DIR=str(tmp_path / "state"),
    )


def test_the_drill_needs_no_keycloak_dump(tmp_path: Path) -> None:
    """The nightly backups it drills hold none since phase 4 (K1): requiring
    one failed every drill on every fresh install and every upgraded host."""
    run = _drill(tmp_path, keycloak_dump=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Restore drill PASSED" in run.stdout, run.stdout
    assert "-d keycloak" not in (tmp_path / "docker-calls").read_text()
    assert "keycloak database" not in run.stdout, run.stdout


def test_skip_keycloak_is_accepted_and_changes_nothing(tmp_path: Path) -> None:
    """P4-23: a timer or a cron line that still passes it does not fail, and
    the drill it runs is the drill without it -- the old dump still checked."""
    run = _drill(tmp_path, "--skip-keycloak", keycloak_dump=True, kc_tables="42")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "--skip-keycloak: nothing to skip" in run.stdout, run.stdout
    assert "PASS  the restored keycloak database holds tables" in run.stdout, run.stdout
    assert "Restore drill PASSED" in run.stdout, run.stdout


def test_a_keycloak_dump_is_checked_in_the_restored_database(tmp_path: Path) -> None:
    """A backup that holds one (taken before phase 4, or deploy.sh's
    pre-upgrade backup) must have restored a database that holds tables; none,
    or a query that fails, fails the drill."""
    for tables, verdict in (("42", "PASS"), ("0", "FAIL"), ("fail", "FAIL")):
        case = tmp_path / tables
        case.mkdir()
        run = _drill(case, keycloak_dump=True, kc_tables=tables)
        line = f"{verdict}  the restored keycloak database holds tables"
        assert line in run.stdout, (tables, run.stdout)
        assert (run.returncode == 0) == (verdict == "PASS"), (tables, run.stdout)
    assert "query failed" in run.stdout, run.stdout


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
