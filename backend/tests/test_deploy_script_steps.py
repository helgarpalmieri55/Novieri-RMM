"""What `deploy/scripts/deploy.sh` decides to run, and what it must not skip.

The script's own header states its purpose: every uncertainty resolves to
doing MORE, because "a skipped migration is an outage; a redundant idempotent
step costs seconds". These tests hold it to that, on the steps where a skip is
silent rather than loud.

Nothing here runs docker. They read the script, because the defect they guard
against is a missing line, and a missing line is visible in the text.
"""

import re
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO / "deploy/scripts/deploy.sh"


def _script() -> str:
    return DEPLOY_SH.read_text()


# Comments name the commands they explain, so an ordering assertion made
# against the raw text measures prose rather than steps. Strip them first.
_SH_COMMENT = re.compile(r"^\s*#.*$", re.MULTILINE)


def test_no_step_starts_an_identity_provider() -> None:
    """Own authentication phase 4. deploy.sh restarted `keycloak` on a realm
    or theme change and ran `keycloak-init` on every deploy; both services
    left the compose file, and a step naming either would stop the deploy
    with `no such service` -- after the migration had already run. No compose
    command and no one-shot names either now: the two names are left only
    where the script finds and removes the old containers (below). (Replaces
    `test_a_theme_change_is_detected_at_all`,
    `test_a_realm_or_theme_change_restarts_keycloak_not_only_keycloak_init`
    and `test_keycloak_init_runs_on_every_deploy_not_only_on_a_realm_change`,
    which pinned those steps.)"""
    steps = _SH_COMMENT.sub("", _script())
    assert not re.search(r"NEED_KEYCLOAK|KC_IMAGE_CHANGED", steps)
    commands = [
        line.strip()
        for line in steps.splitlines()
        if '"${COMPOSE[@]}"' in line or "run_oneshot " in line
    ]
    assert [c for c in commands if "keycloak" in c.lower()] == [], commands


def test_no_up_removes_orphans() -> None:
    """The coordinator's decision (2026-11-15): `up --remove-orphans` would
    also remove any container the operator added to this project. The old
    identity provider's two containers go by name instead (below)."""
    assert "--remove-orphans" not in _SH_COMMENT.sub("", _script())


def _array(name: str) -> str:
    """A top-level `NAME=( ... )` array of deploy.sh, as the script has it."""
    match = re.search(rf"^{name}=\(.*?\)$", _script(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"deploy.sh has no {name} -- if renamed, follow it"
    return match.group(0)


#: The four keys the release before own authentication phase 4 does not start
#: without (operator decision K1, 2026-11-15): its compose file needs the two
#: KEYCLOAK_ADMIN_*, its api, gateway and worker the two ITS_OIDC_*.
ROLLBACK_KEYS = (
    "KEYCLOAK_ADMIN_USER",
    "KEYCLOAK_ADMIN_PASSWORD",
    "ITS_OIDC_ISSUER",
    "ITS_OIDC_JWKS_URL",
)


def test_obsolete_env_keys_are_named_and_never_edited() -> None:
    """The keys an upgraded `.env` still holds are named once, and the file is
    left byte for byte as it was (the script never edits `.env` beyond
    IT_SUITE_VERSION). `.env.example` ships none of them. And the note says
    WHICH four a rollback needs (fix round 1: it said "four of them")."""
    listed = re.search(r"OBSOLETE_ENV_KEYS=\((.*?)\)", _script(), re.DOTALL)
    assert listed is not None
    keys = listed.group(1).split()
    assert len(keys) == 17 and len(set(keys)) == 17, keys
    rollback = re.search(r"^ROLLBACK_ENV_KEYS=\((.*?)\)$", _script(), re.MULTILINE)
    assert rollback is not None and tuple(rollback.group(1).split()) == ROLLBACK_KEYS
    assert set(ROLLBACK_KEYS) <= set(keys)
    example = (REPO / "deploy/.env.example").read_text()
    shipped = [k for k in keys if re.search(rf"^#?\s*{k}=", example, re.MULTILINE)]
    assert shipped == [], shipped
    script = f"""
set -euo pipefail
cd "$(mktemp -d)"
printf '%s\\n' 'ITS_OIDC_ISSUER=https://x/realms/it-suite' 'KEYCLOAK_ADMIN_PASSWORD=p' \\
    'ITS_DEFAULT_ORG_SLUG=toeshee' > .env
cp .env before
note() {{ printf '%s\\n' "$*"; }}
OBSOLETE_ENV_KEYS=({" ".join(keys)})
{_array("ROLLBACK_ENV_KEYS")}
{_fn("obsolete_env_keys")}
obsolete_env_keys
cmp -s .env before && echo UNCHANGED
"""
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert "2 key(s)" in run.stdout, run.stdout
    assert "KEYCLOAK_ADMIN_PASSWORD ITS_OIDC_ISSUER" in run.stdout, run.stdout
    assert "ITS_DEFAULT_ORG_SLUG" not in run.stdout.split("\n", 1)[1], run.stdout
    assert "UNCHANGED" in run.stdout, run.stdout
    # Not "harmless; remove them": the release before needs four of them
    # (operator decision K1, 2026-11-15) -- and these four, by name.
    keep = run.stdout.splitlines()[_step(run.stdout, "Do NOT remove them yet")]
    assert all(key in keep for key in ROLLBACK_KEYS), keep
    assert "Retiring the old identity provider's data" in run.stdout, run.stdout
    assert "Harmless" not in _script()


def _containers(listing: str, *, docker_fails: str = "", compose_ps: str = "") -> str:
    """deploy.sh's container functions, against a `docker` that answers the
    way the engine does. `docker ps -a ... --filter label=K=V` lists the
    containers in LISTING (one `name project service` per line) whose compose
    labels EQUAL every filter given, as Docker's label filter does; `docker
    compose config` names the project `it-suite`; `docker compose ps` answers
    COMPOSE_PS, which is empty unless a test says otherwise (whether Compose
    lists a container whose service left the file is Compose's choice). Each
    label query is written to ./queries and every other docker call to
    ./calls. DOCKER_FAILS names the call that exits 1 instead: `ps` (the
    engine) or `config` (the compose file)."""
    return f"""
set -euo pipefail
cd "$(mktemp -d)"
say()  {{ printf '%s\\n' "$*"; }}
note() {{ printf '%s\\n' "$*"; }}
warn() {{ printf 'WARNING %s\\n' "$*"; }}
die()  {{ printf 'FAILED %s\\n' "$*" >&2; exit 1; }}
printf '%b' {listing!r} > listing
docker() {{
  local project="" service="" arg
  if [ "$1" = compose ]; then
    case "$*" in
      *" config"*)
        [ {docker_fails!r} = config ] && return 1
        printf 'name: it-suite\\nservices:\\n  api: {{}}\\n' ;;
      *" ps"*) printf '%b' {compose_ps!r} ;;
      *) echo "docker $*" >> calls ;;
    esac
    return 0
  fi
  if [ "$1" = ps ]; then
    [ {docker_fails!r} = ps ] && return 1
    echo "docker $*" >> queries
    for arg in "$@"; do
      case "$arg" in
        label=com.docker.compose.project=*) project="${{arg#label=com.docker.compose.project=}}" ;;
        label=com.docker.compose.service=*) service="${{arg#label=com.docker.compose.service=}}" ;;
      esac
    done
    awk -v p="$project" -v s="$service" \\
      '(p == "" || $2 == p) && (s == "" || $3 == s) {{ print $1 }}' listing
    return 0
  fi
  echo "docker $*" >> calls
}}
COMPOSE=(docker compose)
{_fn("old_identity_containers")}
{_fn("remove_old_identity_containers")}
"""


def test_the_old_identity_providers_containers_go_by_name_and_nothing_else() -> None:
    """Compose leaves a container whose service left the file RUNNING, and
    Traefik routes to it by the labels on the container: Keycloak, its
    anonymous client registration included, would go on answering on :443.
    Exactly `keycloak` and `keycloak-init`, by service name -- not the
    operator's own `guacd`, not a `keycloak-proxy` of theirs (the
    coordinator's decision, 2026-11-15), and not a `keycloak` of another
    compose project on the same host. (Fix round 1: the name is the
    container's `com.docker.compose.service` label in this project, the query
    the Verify step makes, not a line of `compose ps`.)"""
    listing = (
        "it-suite-api-1 it-suite api\n"
        "it-suite-keycloak-1 it-suite keycloak\n"
        "it-suite-keycloak-init-1 it-suite keycloak-init\n"
        "it-suite-guacd-1 it-suite guacd\n"
        "it-suite-keycloak-proxy-1 it-suite keycloak-proxy\n"
        "lab-keycloak-1 lab keycloak\n"
    )
    compose_ps = "".join(f"{n} {s}\n" for n, _p, s in (ln.split() for ln in listing.splitlines()))
    run = _run_bash(
        _containers(listing, compose_ps=compose_ps) + "remove_old_identity_containers\ncat calls\n"
    )
    assert run.returncode == 0, run.stdout + run.stderr
    calls = [line for line in run.stdout.splitlines() if line.startswith("docker ")]
    assert calls == [
        "docker stop it-suite-keycloak-1",
        "docker rm it-suite-keycloak-1",
        "docker stop it-suite-keycloak-init-1",
        "docker rm it-suite-keycloak-init-1",
    ], run.stdout


def test_an_orphan_only_the_labels_find_is_removed() -> None:
    """Fix round 1. The removal read `docker compose ps -a`, the Verify step
    the containers' labels; if Compose did not list a container whose service
    left the file, the removal said "none left" and Verify then failed the
    deploy on a container nothing had tried to remove. Both read the labels
    now, through one function."""
    listing = "it-suite-api-1 it-suite api\nit-suite-keycloak-1 it-suite keycloak\n"
    run = _run_bash(
        _containers(listing, compose_ps="it-suite-api-1 api\n")
        + "remove_old_identity_containers\ncat calls\n"
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "none left" not in run.stdout, run.stdout
    assert "docker stop it-suite-keycloak-1" in run.stdout, run.stdout
    assert "docker rm it-suite-keycloak-1" in run.stdout, run.stdout
    listing_fn = _fn("old_identity_containers")
    assert "label=com.docker.compose.service=" in listing_fn, listing_fn
    assert '"${COMPOSE[@]}" ps' not in listing_fn, listing_fn
    assert "$(old_identity_containers)" in _fn("verify_old_identity_gone")
    assert "$(old_identity_containers)" in _fn("stop_if_already_deployed")


def test_a_docker_that_does_not_answer_stops_the_run() -> None:
    """Fix round 1. `|| true` turned a failed docker call into "none left",
    into a passing Verify, and -- before the diff -- into "Already deployed".
    The removal, the Verify check and the half-done check each stop the run
    now, with the reason, whether the engine or the compose file failed."""
    listing = "it-suite-keycloak-1 it-suite keycloak\n"
    for fails in ("ps", "config"):
        removal = _run_bash(
            _containers(listing, docker_fails=fails)
            + "remove_old_identity_containers\necho REACHED\n"
        )
        verify = _run_bash(
            _verify(listing, docker_fails=fails)
            + "verify_old_identity_gone && echo PASSED || echo LEFT\necho REACHED\n"
        )
        already = _run_bash(_already(listing, docker_fails=fails))
        for run in (removal, verify, already):
            assert run.returncode != 0, (fails, run.stdout, run.stderr)
            assert "REACHED" not in run.stdout and "CONTINUED" not in run.stdout, run.stdout
            assert "FAILED" in run.stderr, (fails, run.stderr)
            assert "the old identity provider's containers" in run.stderr, (fails, run.stderr)
        assert "none left" not in removal.stdout, removal.stdout
        assert "PASSED" not in verify.stdout and "LEFT" not in verify.stdout, verify.stdout
        assert "Already deployed" not in already.stdout, already.stdout


def test_removing_them_twice_is_a_no_op() -> None:
    script = _containers("it-suite-api-1 it-suite api\n")
    run = _run_bash(script + "remove_old_identity_containers\ntest ! -e calls && echo NO_CALLS\n")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "none left" in run.stdout and "NO_CALLS" in run.stdout, run.stdout


def test_they_go_after_the_new_app_tier_is_up() -> None:
    """Until then, a deploy that stops leaves the release before able to run."""
    text = _SH_COMMENT.sub("", _script())
    up = text.index('\n"${COMPOSE[@]}" up -d\n')
    removal = text.index("\nremove_old_identity_containers\n")
    assert up < removal < text.index('say "Verifying"')


def _already(
    listing: str, home: str = "", docker_fails: str = "", baseline: str | None = None
) -> str:
    """`stop_if_already_deployed` after a pull that brought nothing, with
    HOME (where `backups/pre-upgrade-*` live) a directory of its own.
    BASELINE is `BASE` unless one is given: `--full` leaves it empty."""
    known = BASE if baseline is None else baseline
    return _containers(listing, docker_fails=docker_fails) + f"""
HOME={home!r}; [ -n "$HOME" ] || HOME="$PWD"
git() {{ echo 2f20afbe00000000000000000000000000000000; }}
BASELINE={known!r}; AFTER=2f20afbe00000000000000000000000000000000
SHORT_AFTER=2f20afbe; REPO_ROOT=/opt/it-suite; RUNNING_TAG=2f20afbe
{_array("ROLLBACK_ENV_KEYS")}
{_fn("way_back_backup")}
{_fn("stop_if_already_deployed")}
{_fn("rollback_env_keys_check")}
ROLLBACK_BACKUP_DIR=""; ROLLBACK_BASE=""; HALF_DONE_UPGRADE=0
stop_if_already_deployed
echo "CONTINUED baseline=[$BASELINE] running=[$RUNNING_TAG] half_done=[$HALF_DONE_UPGRADE]"
echo "back=[$ROLLBACK_BACKUP_DIR] base=[$ROLLBACK_BASE]"
"""


def _backups(home: Path) -> dict[str, Path]:
    """What a host holds after the deploy.sh of the release before phase 4
    stopped part-way: an older pre-upgrade backup, the one it took (0247,
    images 53b02f81), and one taken past 0248 by an earlier finishing try."""
    made = {}
    for name, rev, tag in (
        ("pre-upgrade-2026-11-01T0900Z", "0246", "1a2b3c4d"),
        ("pre-upgrade-2026-11-20T1000Z", "0247", "53b02f81"),
        ("pre-upgrade-2026-11-20T1015Z", "0248", "2f20afbe"),
    ):
        made[rev] = home / "backups" / name
        made[rev].mkdir(parents=True)
        (made[rev] / "alembic_version").write_text(f"{rev}\n")
        (made[rev] / "image_version").write_text(f"IT_SUITE_VERSION={tag}\n")
    return made


def test_a_half_done_upgrade_is_finished_not_called_already_deployed() -> None:
    """The deploy.sh of a release before own authentication phase 4, run as
    usual, pulls this release, records its tag and migrates, then stops at
    `restart keycloak`. Re-run, this script read the new tag as running and
    answered "Already deployed" with exit 0, the app tier still on the old
    images. While either old container is there it runs a full deploy, and
    marks the run as finishing a half-done upgrade (fix round 1)."""
    for old in (
        "it-suite-keycloak-1 it-suite keycloak\n",
        "it-suite-keycloak-init-1 it-suite keycloak-init\n",
    ):
        run = _run_bash(_already("it-suite-api-1 it-suite api\n" + old))
        assert run.returncode == 0, run.stdout + run.stderr
        assert "Already deployed" not in run.stdout, run.stdout
        assert "CONTINUED baseline=[]" in run.stdout and "full deploy" in run.stdout, run.stdout
        assert "half_done=[1]" in run.stdout, run.stdout
    done = _run_bash(_already("it-suite-api-1 it-suite api\n"))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Already deployed" in done.stdout and "CONTINUED" not in done.stdout, done.stdout


def test_the_way_back_from_a_half_done_upgrade_is_the_backup_from_before_0248(
    tmp_path: Path,
) -> None:
    """Not this run's backup, which is past 0248, and not the newest one that
    holds `keycloak.dump`: the newest `pre-upgrade-*` whose schema is below
    0248, with the images its `image_version` names. The closing rollback
    help prints that backup, that tag and that commit."""
    made = _backups(tmp_path)
    finishing = tmp_path / "backups" / "pre-upgrade-2026-11-20T1030Z"
    script = _already("it-suite-keycloak-1 it-suite keycloak\n", home=str(tmp_path)) + f"""
{_fn("rollback_help")}
BACKUP_DIR={str(finishing)!r}; NEED_MIGRATE=1; NEED_COMPOSE=1
rollback_help
"""
    run = _run_bash(script)
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"back=[{made['0247']}]" in run.stdout, run.stdout
    assert "running=[53b02f81]" in run.stdout and "base=[53b02f81]" in run.stdout, run.stdout
    help_ = run.stdout[run.stdout.index("If you need to go back") :]
    assert f"ITS_RESTORE_FORCE=1 ./scripts/restore.sh {made['0247']}\n" in help_, help_
    assert "IT_SUITE_VERSION=53b02f81" in help_ and "checkout 53b02f81 -- deploy" in help_, help_
    assert "There is no image to swap back to" not in help_, help_
    restore = [line for line in help_.splitlines() if "restore.sh" in line]
    assert str(finishing) not in "\n".join(restore), help_


def test_a_full_rerun_of_a_half_done_upgrade_still_names_the_way_back(tmp_path: Path) -> None:
    """Final review of phase 4, B1. `--full` leaves BASELINE empty, and the
    check began with `[ -n "$BASELINE" ] || return 0`: a `--full` rerun of a
    half-done upgrade never looked for the old containers, and its closing
    help named this run's own backup, past 0248, as "The backup to restore".
    The check runs whenever the running tag is HEAD, whatever BASELINE is.
    "Already deployed" stays for a known BASELINE only: with nothing of the
    old provider left, `--full` still redeploys."""
    made = _backups(tmp_path)
    finishing = tmp_path / "backups" / "pre-upgrade-2026-11-20T1030Z"
    old = "it-suite-keycloak-1 it-suite keycloak\n"
    script = _already(old, home=str(tmp_path), baseline="") + f"""
{_fn("rollback_help")}
BACKUP_DIR={str(finishing)!r}; NEED_MIGRATE=1; NEED_COMPOSE=1
rollback_help
"""
    run = _run_bash(script)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "half_done=[1]" in run.stdout, run.stdout
    assert f"back=[{made['0247']}]" in run.stdout, run.stdout
    help_ = run.stdout[run.stdout.index("If you need to go back") :]
    assert f"ITS_RESTORE_FORCE=1 ./scripts/restore.sh {made['0247']}\n" in help_, help_
    assert "The backup to restore" not in help_, help_
    restore = [line for line in help_.splitlines() if "restore.sh" in line]
    assert str(finishing) not in "\n".join(restore), help_

    nothing_left = _run_bash(_already("it-suite-api-1 it-suite api\n", baseline=""))
    assert nothing_left.returncode == 0, nothing_left.stdout + nothing_left.stderr
    assert "Already deployed" not in nothing_left.stdout, nothing_left.stdout
    assert "CONTINUED baseline=[] " in nothing_left.stdout, nothing_left.stdout
    assert "half_done=[0]" in nothing_left.stdout, nothing_left.stdout


def test_without_a_backup_from_before_0248_it_says_so(tmp_path: Path) -> None:
    (tmp_path / "backups" / "pre-upgrade-2026-11-20T1015Z").mkdir(parents=True)
    (tmp_path / "backups" / "pre-upgrade-2026-11-20T1015Z" / "alembic_version").write_text("0248\n")
    run = _run_bash(_already("it-suite-keycloak-1 it-suite keycloak\n", home=str(tmp_path)))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "back=[] base=[]" in run.stdout and "from before migration 0248" in run.stdout


def test_without_a_backup_from_before_0248_the_closing_help_names_none(tmp_path: Path) -> None:
    """Fix round 1. Backups moved, or another $HOME: no `pre-upgrade-*` here
    is below 0248, and the running tag is the new one. The closing help took
    its no-images branch and named THIS run's backup, at 0248, as "The
    backup to restore" -- a way back to nowhere. It now names none, and says
    that no backup on this host goes back past phase 4."""
    past = tmp_path / "backups" / "pre-upgrade-2026-11-20T1015Z"
    past.mkdir(parents=True)
    (past / "alembic_version").write_text("0248\n")
    finishing = tmp_path / "backups" / "pre-upgrade-2026-11-20T1030Z"
    script = _already("it-suite-keycloak-1 it-suite keycloak\n", home=str(tmp_path)) + f"""
{_fn("rollback_help")}
BACKUP_DIR={str(finishing)!r}; NEED_MIGRATE=1; NEED_COMPOSE=1
rollback_help
"""
    run = _run_bash(script)
    assert run.returncode == 0, run.stdout + run.stderr
    help_ = run.stdout[run.stdout.index("If you need to go back") :]
    assert "no backup on this host goes back past" in help_.lower(), help_
    assert str(finishing) not in help_ and str(past) not in help_, help_
    assert "The backup to restore" not in help_, help_
    assert "IT_SUITE_VERSION=" not in help_ and "docker compose up -d" not in help_, help_


def _verify(listing: str, docker_fails: str = "") -> str:
    """deploy.sh's Verify check, against the same `docker` as `_containers`."""
    return _containers(listing, docker_fails=docker_fails) + _fn("verify_old_identity_gone")


def test_the_verify_step_fails_while_a_container_of_it_is_left() -> None:
    """By the containers' own compose labels, not by `compose ps`: if that
    did not list a container whose service left the file, the removal would
    say "none left" and Traefik would go on routing to Keycloak."""
    check = "verify_old_identity_gone && echo PASSED || echo FAILED\n"
    listing = "it-suite-api-1 it-suite api\nit-suite-keycloak-1 it-suite keycloak\n"
    left = _run_bash(_verify(listing) + check + "cat queries\n")
    assert "FAILED" in left.stdout.splitlines(), left.stdout
    assert "docker rm -f it-suite-keycloak-1" in left.stdout, left.stdout
    assert "label=com.docker.compose.project=it-suite" in left.stdout, left.stdout
    gone = _run_bash(_verify("it-suite-api-1 it-suite api\n") + check)
    assert "PASSED" in gone.stdout and "no container of the old identity provider" in gone.stdout
    text = _SH_COMMENT.sub("", _script())
    assert text.index('say "Verifying"') < text.index("\nverify_old_identity_gone || FAIL=1\n")


def test_the_check_runs_before_the_diff_is_taken() -> None:
    text = _SH_COMMENT.sub("", _script())
    call = text.index("\nstop_if_already_deployed\n")
    assert text.index("\nold_identity_containers() {") < call < text.index('CHANGED="$(git')


def _backup_step() -> str:
    """deploy.sh's backup step as it ships: from `BACKUP_DIR=` to the
    `backup.sh` call."""
    match = re.search(
        r'^BACKUP_DIR="\$HOME/backups/pre-upgrade-.*?'
        r'^ITS_BACKUP_OLD_IDENTITY_DB=1 \./scripts/backup\.sh "\$BACKUP_DIR"$',
        _script(),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "deploy.sh's backup step moved -- follow it"
    return match.group(0)


def test_a_rerun_in_the_same_minute_does_not_overwrite_the_backup(tmp_path: Path) -> None:
    """Final review of phase 4, B2. The backup's name has minute resolution
    and `backup.sh` writes into a directory that exists (`mkdir -p`), so two
    runs in one UTC minute shared a directory: the second overwrote the
    first's backup -- after a half-done upgrade, possibly the only one from
    before migration 0248. deploy.sh stops instead, writing nothing there;
    in the next minute the name is new and the backup runs."""
    first = tmp_path / "backups" / "pre-upgrade-2026-11-20T1000Z"
    first.mkdir(parents=True)
    (first / "itsuite.dump").write_text("the first run's\n")
    (tmp_path / "scripts").mkdir()
    stub = tmp_path / "scripts" / "backup.sh"
    stub.write_text('#!/usr/bin/env bash\nmkdir -p "$1"\necho "a rerun\'s" > "$1/itsuite.dump"\n')
    stub.chmod(0o755)

    def step(minute: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", f"""
set -euo pipefail
die() {{ printf 'FAILED %s\\n' "$*" >&2; exit 1; }}
date() {{ echo {minute}; }}
HOME={str(tmp_path)!r}
{_backup_step()}
echo BACKED_UP
"""],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
        )

    rerun = step("2026-11-20T1000Z")
    assert rerun.returncode != 0, rerun.stdout + rerun.stderr
    assert "BACKED_UP" not in rerun.stdout, rerun.stdout
    assert "FAILED" in rerun.stderr and "already exists" in rerun.stderr, rerun.stderr
    assert "Wait a minute" in rerun.stderr, rerun.stderr
    assert (first / "itsuite.dump").read_text() == "the first run's\n"

    later = step("2026-11-20T1001Z")
    assert later.returncode == 0, later.stdout + later.stderr
    assert "BACKED_UP" in later.stdout, later.stdout
    second = tmp_path / "backups" / "pre-upgrade-2026-11-20T1001Z" / "itsuite.dump"
    assert second.read_text() == "a rerun's\n"
    assert (first / "itsuite.dump").read_text() == "the first run's\n"


OBSOLETE = re.compile(
    r"KEYCLOAK_ADMIN_(USER|PASSWORD)|ITS_OIDC_[A-Z_]+|ITS_MULTI_TENANT"
    r"|ENTRA_(TENANT_ID|CLIENT_ID|CLIENT_SECRET)|ITS_KEYCLOAK_[A-Z_]+"
)
_ROLLBACK_KEY_NAMES = re.compile(r"\b(" + "|".join(ROLLBACK_KEYS) + r")\b")


def test_no_other_deploy_file_names_an_obsolete_key() -> None:
    """A script that still READS one is the failure: `preflight.sh` read
    `ITS_OIDC_ISSUER`, which a fresh `.env` no longer has, and refused every
    new install. deploy.sh names them on purpose (its list and the rollback
    check). The runbook is Task 9's guard's (`deploy/README.md`,
    `deploy/GO-LIVE.md`).

    Fix round 1:
    - No file is exempt but those this docstring names. (Until Task 7
      deleted it, `deploy/keycloak/ci_mcp_flow.py` was too, by name; before
      fix round 1 the exemption was every path under `deploy/keycloak/`. The
      final review of phase 4, B5, removed the name the deletion left
      behind.)
    - `.env.example` may name the four keys a rollback needs, in a comment
      line, so whoever compares an upgraded `.env` with it knows which to
      keep -- never as a key, commented out or not, and none of the other
      thirteen.
    - Bytecode (`__pycache__`) and a local `deploy/.env` are not shipped;
      the second is gitignored and on an upgraded host holds these keys."""
    exempt = {
        DEPLOY_SH,
        REPO / "deploy/README.md",
        REPO / "deploy/GO-LIVE.md",
        REPO / "deploy/.env",
    }
    example = REPO / "deploy/.env.example"
    found = []
    for path in sorted((REPO / "deploy").rglob("*")):
        if not path.is_file() or path in exempt or "__pycache__" in path.parts:
            continue
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            prose = line.startswith("#") and not re.match(r"#\s*[A-Z][A-Z0-9_]*=", line)
            if path == example and prose:
                line = _ROLLBACK_KEY_NAMES.sub("", line)
            if OBSOLETE.search(line):
                found.append(f"{path.relative_to(REPO)}:{number}")
    assert found == [], found
    comments = " ".join(ln for ln in example.read_text().splitlines() if ln.startswith("#"))
    assert [key for key in ROLLBACK_KEYS if key not in comments] == [], "name the four"


def test_the_preflight_takes_the_console_host_from_its_address(tmp_path: Path) -> None:
    """Its host comes from `ITS_PUBLIC_CONSOLE_URL` (until own authentication
    phase 4, `ITS_OIDC_ISSUER`, which a fresh `.env` no longer has), and a
    blank value gives no host -- not the whole line, which `sed` without
    `-n ...p` printed. Until 2026-11-23 it was checked against the certificate
    file; since then, against ITS_PUBLIC_HOST, the name Traefik asks Let's
    Encrypt for (`test_preflight_script.py`)."""
    text = (REPO / "deploy/scripts/preflight.sh").read_text()
    found = re.search(
        r"^host=\"\$\(grep -E '\^ITS_PUBLIC_CONSOLE_URL=' \.env.*?\|\| true\)\"$",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert found is not None, "preflight.sh does not take the host from ITS_PUBLIC_CONSOLE_URL"
    for value, host in (
        ("https://itms.example.test", "itms.example.test"),
        ("https://itms.example.test:8443/", "itms.example.test"),
        ("", ""),
        ("https://your-host", "your-host"),
    ):
        env = f"ITS_DEFAULT_ORG_SLUG=toeshee\nITS_PUBLIC_CONSOLE_URL={value}\n"
        (tmp_path / ".env").write_text(env)
        run = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + found.group(0) + '\nprintf "[%s]" "$host"'],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=True,
        )
        assert run.stdout == f"[{host}]", (value, run.stdout)


def test_every_one_shot_checks_its_exit_code() -> None:
    """`docker compose up <one-shot>` does not reliably fail on a non-zero
    exit, so a failed migration or realm convergence read as success."""
    text = _SH_COMMENT.sub("", _script())
    for svc in ("migrate", "postgres-grants", "openbao-init"):
        assert f"run_oneshot {svc}" in text, svc
        assert f'"${{COMPOSE[@]}}" up {svc}\n' not in text, f"{svc} bypasses run_oneshot"


# ---------------------------------------------------------------------------
# The rollback an operator follows under pressure
# ---------------------------------------------------------------------------
#
# These RUN deploy.sh's own functions in bash with `docker` stubbed, because
# every defect below was a line that read fine and did the wrong thing.
#
# History, so nobody re-derives it: the script once printed
# `IT_SUITE_VERSION=<previous sha> docker compose up -d` -- a placeholder, and a
# version that lasted one command. Its first replacement fixed those and was
# wrong four ways a review bot (Codex, PR #83) and the runbook it cited caught:
# it skipped stopping the app tier, used a sed that silently no-ops on an old
# .env, checked the backend image but not web, and sent compose and theme
# changes to a database restore that undoes neither and destroys writes and
# audit history. It now prints deploy/README.md's own procedure.

import subprocess  # noqa: E402

_FN = r"^{name}\(\) \{{\n.*?^\}}\n"
BACKUP = "/home/it/backups/pre-upgrade-2026-09-22T2100Z"
BASE = "53b02f8104a1c0ffee00000000000000000000"


def _fn(name: str) -> str:
    match = re.search(_FN.format(name=name), _script(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"deploy.sh has no {name}() -- if renamed, follow it"
    return match.group(0)


def _harness(
    *,
    running_tag: str = "53b02f81",
    backend: bool = True,
    web: bool = True,
    baseline: str = BASE,
    migrate: int = 0,
    compose: int = 0,
    old_compose: str = "",
    env: str = "",
) -> str:
    # OLD_COMPOSE is what `git show <base>:deploy/docker-compose.yml` prints;
    # ENV is the .env the rollback is printed against.
    return f"""
set -euo pipefail
cd "$(mktemp -d)"
printf '%b' {env!r} > .env
git() {{ case "$*" in *" show "*) printf '%b' {old_compose!r} ;; *) return 1 ;; esac; }}
say()  {{ LAST_STEP="$*"; printf '%s\\n' "$*"; }}
note() {{ printf '%s\\n' "$*"; }}
warn() {{ printf 'WARNING %s\\n' "$*"; }}
docker() {{
  case "$*" in
    *itsuite/backend:*) {"return 0" if backend else "return 1"} ;;
    *itsuite/web:*)     {"return 0" if web else "return 1"} ;;
    *) return 1 ;;
  esac
}}
LAST_STEP=starting
RUNNING_TAG={running_tag!r}; SHORT_AFTER=2f20afbe
BACKUP_DIR={BACKUP!r}; BASELINE={baseline!r}; REPO_ROOT=/opt/it-suite
NEED_MIGRATE={migrate}; NEED_COMPOSE={compose}
{_array("ROLLBACK_ENV_KEYS")}
{_fn("rollback_env_keys_check")}
{_fn("rollback_help")}
"""


def _rollback(**kw: Any) -> str:
    script = _harness(**kw) + "rollback_help\n"
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return run.stdout


def _step(out: str, needle: str) -> int:
    for number, line in enumerate(out.splitlines()):
        if needle in line:
            return number
    raise AssertionError(f"no step containing {needle!r} in:\n{out}")


def test_a_plain_rollback_is_the_runbooks_steps_in_the_runbooks_order() -> None:
    out = _rollback()
    stop = _step(out, "docker compose stop api gateway worker web")
    edit = _step(out, "IT_SUITE_VERSION=53b02f81")
    up = _step(out, "docker compose up -d")
    assert stop < edit < up, out
    assert "restore.sh" not in out, f"a restore offered with no schema change:\n{out}"
    assert "<" not in out and ">>" in out, f"placeholder, or no append fallback:\n{out}"


def test_the_env_edit_still_works_on_an_env_without_the_key() -> None:
    """The runbook's warning: a bare `sed -i` on a .env predating
    IT_SUITE_VERSION matches nothing and leaves the host on :dev."""
    out = _rollback()
    line = out.splitlines()[_step(out, "IT_SUITE_VERSION=53b02f81")]
    assert "grep -q '^IT_SUITE_VERSION=' .env" in line, line
    assert "|| echo 'IT_SUITE_VERSION=53b02f81' >> .env" in line, line


def test_no_image_swap_unless_both_previous_images_exist() -> None:
    """A missing web tag makes `up -d` rebuild CURRENT source and label it old."""
    for kw in (dict(web=False), dict(backend=False), dict(running_tag=""), dict(running_tag="dev")):
        out = _rollback(**kw)
        assert "sed -i" not in out and "docker compose stop" not in out, f"{kw}:\n{out}"
        assert BACKUP in out, f"{kw}:\n{out}"


def test_a_migration_adds_the_restore_between_stopping_and_starting() -> None:
    out = _rollback(migrate=1)
    restore = _step(out, f"ITS_RESTORE_FORCE=1 ./scripts/restore.sh {BACKUP}")
    assert _step(out, "docker compose stop") < restore < _step(out, "docker compose up -d"), out
    assert "audit" in out and "DROPS THE DATABASES IN THE BACKUP" in out, out


def test_a_compose_change_checks_the_whole_deploy_directory_out() -> None:
    """Restoring Postgres undoes no compose change -- the running compose file
    and what it bind-mounts come from the checkout -- and costs every write
    and audited action since the backup. And the compose file alone is not
    enough: an earlier release's compose bind-mounts files of that release
    (the old identity provider's realm and theme, until own authentication
    phase 4) which the new checkout no longer has. So `deploy`, whole. (Was
    `test_a_compose_or_theme_change_is_undone_from_git_not_by_restoring_the_database`
    and, with it, `test_a_keycloak_image_change_does_need_the_restore`.)"""
    out = _rollback(compose=1)
    assert f"checkout {BASE[:12]} -- deploy\n" in out, out
    assert "restore.sh" not in out, f"a compose change sent to a database restore:\n{out}"
    assert "restart" not in out, out


def test_a_rollback_past_phase_4_names_the_env_keys_it_needs() -> None:
    """The release before own authentication phase 4 needs four keys this one
    does not read: its compose file does not parse without the two
    KEYCLOAK_ADMIN_*, and its api, gateway and worker do not start
    without the two ITS_OIDC_*. If .env lost any, the rollback says so
    before the step that would fail."""
    old = "      KC_BOOTSTRAP_ADMIN_USERNAME: ${KEYCLOAK_ADMIN_USER:?set in .env}\n"
    out = _rollback(
        migrate=1,
        compose=1,
        old_compose=old,
        env="KEYCLOAK_ADMIN_USER=admin\nITS_OIDC_ISSUER=https://x/realms/it-suite\n",
    )
    warning = _step(out, "no longer has")
    assert "KEYCLOAK_ADMIN_PASSWORD ITS_OIDC_JWKS_URL" in out.splitlines()[warning], out
    edit = _step(out, "IT_SUITE_VERSION=53b02f81")
    assert warning < edit < _step(out, "docker compose up -d"), out
    four = ("KEYCLOAK_ADMIN_USER", "KEYCLOAK_ADMIN_PASSWORD", "ITS_OIDC_ISSUER")
    four += ("ITS_OIDC_JWKS_URL",)
    kept = "".join(f"{key}=x\n" for key in four)
    assert "no longer has" not in _rollback(migrate=1, compose=1, old_compose=old, env=kept)
    assert "no longer has" not in _rollback(migrate=1, compose=1, old_compose="services: {}\n")


def test_without_the_old_images_the_rollback_still_names_the_missing_keys() -> None:
    """Fix round 1: the check ran only on the image-swap path. Without the old
    images the way back is the backup and a rebuild of the old release, which
    needs the same four keys."""
    old = "      KC_BOOTSTRAP_ADMIN_USERNAME: ${KEYCLOAK_ADMIN_USER:?set in .env}\n"
    out = _rollback(web=False, migrate=1, old_compose=old, env="KEYCLOAK_ADMIN_USER=admin\n")
    assert "There is no image to swap back to" in out, out
    warning = out.splitlines()[_step(out, "no longer has")]
    assert "KEYCLOAK_ADMIN_PASSWORD ITS_OIDC_ISSUER ITS_OIDC_JWKS_URL" in warning, out


def test_with_no_known_base_the_rollback_says_which_keys_it_cannot_rule_out() -> None:
    """Fix round 1: with no commit to read the old compose file from, this run
    cannot tell whether the release before predates phase 4. It says so, and
    which of the four .env lacks, rather than saying nothing."""
    out = _rollback(baseline="", migrate=1, compose=1, env="ITS_OIDC_ISSUER=https://x\n")
    _step(out, "does not know which release ran before")
    warning = out.splitlines()[_step(out, "no longer has")]
    assert "KEYCLOAK_ADMIN_USER KEYCLOAK_ADMIN_PASSWORD ITS_OIDC_JWKS_URL" in warning, out
    assert "ITS_OIDC_ISSUER" not in warning, out
    kept = "".join(f"{key}=x\n" for key in ROLLBACK_KEYS)
    assert "no longer has" not in _rollback(baseline="", migrate=1, compose=1, env=kept)


def test_a_run_that_dies_part_way_still_prints_how_to_go_back() -> None:
    """With `set -e`, a failed migration exits at once. The guidance used to be
    printed only at the very end, so the worst failures got none."""
    script = _harness(migrate=1) + _fn("on_exit") + """
DEPLOY_FINISHED=0
trap on_exit EXIT
say "Migrating"
false
"""
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert run.returncode != 0
    assert 'Stopped part-way, during "Migrating"' in run.stdout, run.stdout
    assert "restore.sh" in run.stdout and "docker compose stop" in run.stdout, run.stdout


def test_the_trap_is_armed_after_the_backup_and_before_anything_changes() -> None:
    text = _script()
    trap = text.index("\ntrap on_exit EXIT")
    assert text.index('./scripts/backup.sh "$BACKUP_DIR"') < trap, "armed before BACKUP_DIR exists"
    for step in (
        'say "Building images"',
        'say "Selecting the tag"',
        'say "Migrating"',
        'say "Restarting"',
    ):
        assert trap < text.index(step), f"{step} can fail before the trap is armed"


def test_the_script_prints_no_placeholder_anywhere() -> None:
    offenders = [
        line.strip()
        for line in _script().splitlines()
        if re.match(r"\s*(say|note|warn)\b", line) and re.search(r"<[a-z][a-z ]*>", line)
    ]
    assert offenders == [], f"output lines with a placeholder to fill in: {offenders}"


# ---------------------------------------------------------------------------
# OpenBao: never let `up -d` block on a sealed vault
# ---------------------------------------------------------------------------


def _seal_harness(*, bao_rc: int, mode: str = "yes", auto_unseal: bool = False) -> str:
    env_line = "COMPOSE_FILE=docker-compose.yml:openbao/auto-unseal.yml" if auto_unseal else ""
    return f"""
set -euo pipefail
cd "$(mktemp -d)"
printf '%s\\n' '{env_line}' > .env
say()  {{ printf '%s\\n' "$*"; }}
note() {{ printf '%s\\n' "$*"; }}
warn() {{ printf 'WARNING %s\\n' "$*"; }}
die()  {{ printf 'FAILED %s\\n' "$*"; exit 1; }}
sleep() {{ :; }}
docker() {{
  case "$*" in
    *"bao status"*) return {bao_rc} ;;
    *) echo "docker $*" >> calls; return 0 ;;
  esac
}}
COMPOSE=(docker compose)
MODE={mode}
{_fn("openbao_state")}
{_fn("auto_unseal_on")}
{_fn("openbao_help")}
{_fn("preflight_openbao")}
{_fn("ensure_openbao_unsealed")}
"""


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def test_bao_status_exit_codes_map_to_states() -> None:
    for rc, state in ((0, "unsealed"), (2, "sealed"), (1, "down"), (125, "down")):
        run = _run_bash(_seal_harness(bao_rc=rc) + "openbao_state\n")
        assert run.stdout.strip() == state, (rc, run.stdout, run.stderr)


def test_a_sealed_openbao_stops_the_deploy_before_anything_changes() -> None:
    run = _run_bash(_seal_harness(bao_rc=2) + "preflight_openbao\necho REACHED\n")
    assert run.returncode != 0
    assert "REACHED" not in run.stdout
    assert "SEALED" in run.stdout
    assert "docker compose exec openbao bao operator unseal" in run.stdout
    assert "FAILED" in run.stdout and "re-run ./scripts/deploy.sh" in run.stdout


def test_a_stopped_openbao_says_how_to_start_it() -> None:
    run = _run_bash(_seal_harness(bao_rc=1) + "preflight_openbao\n")
    assert run.returncode != 0
    assert "docker compose up -d openbao" in run.stdout


def test_plan_mode_only_warns_about_the_seal() -> None:
    run = _run_bash(_seal_harness(bao_rc=2, mode="plan") + "preflight_openbao\necho REACHED\n")
    assert run.returncode == 0 and "REACHED" in run.stdout
    assert "WARNING" in run.stdout


def test_with_auto_unseal_a_sealed_vault_points_at_the_key_file() -> None:
    run = _run_bash(_seal_harness(bao_rc=2, auto_unseal=True) + "preflight_openbao\n")
    assert "openbao-seal.sh status" in run.stdout
    assert "bao operator unseal" not in run.stdout


def test_a_recreated_sealed_openbao_stops_an_unattended_deploy() -> None:
    """`--yes` has nobody to type shares; it must stop, not block."""
    run = _run_bash(_seal_harness(bao_rc=2, mode="yes") + "ensure_openbao_unsealed\necho REACHED\n")
    assert run.returncode != 0 and "REACHED" not in run.stdout
    assert "sealed after being recreated" in run.stdout


def test_an_unsealed_openbao_passes_straight_through() -> None:
    steps = "preflight_openbao\nensure_openbao_unsealed\necho REACHED\n"
    run = _run_bash(_seal_harness(bao_rc=0) + steps)
    assert run.returncode == 0 and "REACHED" in run.stdout, run.stdout + run.stderr


def test_the_seal_is_checked_before_the_pull_and_before_the_tag_moves() -> None:
    text = _SH_COMMENT.sub("", _script())
    assert text.index("\npreflight_openbao\n") < text.index('say "Pulling"')
    ensure = text.index("\nensure_openbao_unsealed\n")
    assert text.index('say "Building images"') < ensure < text.index('say "Selecting the tag"')


def test_the_verify_step_checks_the_public_host_and_its_certificate() -> None:
    """Design 2026-11-23 §2 and §3: the 401 probe asks the host this .env
    names (it named one deployment's host, for every deployment, until
    2026-11-23), and a certificate a browser would refuse fails the deploy
    rather than passing it: Traefik's self-signed fallback is silent."""
    steps = _SH_COMMENT.sub("", _script())
    assert "itms.toeshee.com" not in steps
    assert "PUBLIC_HOST=\"$(sed -n 's/^ITS_PUBLIC_HOST=//p' .env | tail -1)\"" in steps
    assert '"https://${PUBLIC_HOST:-localhost}/api/v1/devices"' in steps
    check = steps.index("./scripts/check-public-tls.sh --wait 60")
    assert steps.index("FAIL=1", check) < steps.index("DEPLOY_FINISHED=1", check)
    # ... and FAIL=1 is the else branch of the check itself, not a stray line.
    branch = steps[check : steps.index("DEPLOY_FINISHED=1", check)]
    assert re.search(
        r"\./scripts/check-public-tls\.sh --wait 60; then\n.*?\nelse\n.*?FAIL=1\nfi\n",
        branch,
        re.S | re.M,
    ), branch
