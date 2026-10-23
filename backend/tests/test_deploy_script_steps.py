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


def _keycloak_branch() -> str:
    """The body of `if [ "$NEED_KEYCLOAK" = 1 ]; then ... fi`, comments removed."""
    # Either shape the branch has worn: a block, or the `&& { ... }`
    # one-liner it used to be. Matching only one would make this test fail on
    # formatting and report it as a missing step, which is a worse lie than
    # the bug it guards.
    body = re.search(
        r'if \[ "\$NEED_KEYCLOAK" = 1 \]; then(.*?)\nfi'
        # `\{\s`, not `\{`: a bash command group needs whitespace after its
        # brace, and without it this also matched `${paths:+...}` -- a
        # parameter expansion on an unrelated line -- and took that for the
        # provisioning step.
        r'|\[ "\$NEED_KEYCLOAK" = 1 \][^\n]*&&[^\n]*\{\s(.*?)\}',
        _script(),
        re.DOTALL,
    )
    assert body is not None, (
        "deploy.sh no longer reacts to NEED_KEYCLOAK at all -- if the step "
        "was renamed, this test needs to follow it rather than be deleted"
    )
    return _SH_COMMENT.sub("", body.group(1) or body.group(2))



def test_a_theme_change_is_detected_at_all() -> None:
    assert re.search(r"touched '\^deploy/\(keycloak\|realm\)'", _script()), (
        "nothing under deploy/keycloak/ sets NEED_KEYCLOAK, so a login theme "
        "or realm change deploys as a no-op"
    )


def test_a_realm_or_theme_change_restarts_keycloak_not_only_keycloak_init() -> None:
    """Provisioning clients is not the same as reloading a theme.

    THIS IS A REGRESSION TEST FOR A REAL MISS. `deploy/keycloak/` is a
    read-only bind mount and Keycloak caches the login theme and the realm in
    memory, so a changed template reaches nobody until the container restarts.
    The branch used to run `keycloak-init` alone -- which creates service
    clients over the admin API and touches neither -- so a theme change was
    detected, announced, "provisioned", and still not serving.

    The fix to `login-config-totp.ftl` on 2026-09-22 (the QR path posted no
    totpSecret, so every scan ended in an uncaught NPE) would have been
    deployed by this script and left every user still unable to enrol 2FA.
    """
    branch = _keycloak_branch()

    assert "restart keycloak" in branch, (
        "a realm or theme change runs keycloak-init but never restarts "
        "keycloak, so the bind-mounted theme stays cached and the change does "
        "not reach anyone"
    )
    text = _SH_COMMENT.sub("", _script())
    restart = text.index('"${COMPOSE[@]}" restart keycloak')
    init_step = text.index("run_oneshot keycloak-init")
    assert restart < init_step, "keycloak must restart BEFORE keycloak-init runs against it"


def test_keycloak_init_runs_on_every_deploy_not_only_on_a_realm_change() -> None:
    """`--import-realm` skips an existing realm, so keycloak-init is the only
    path by which the repo's realm allowlist reaches a live host. Gating it on
    a deploy/keycloak diff left hand-made drift (an open account console) in
    place for every release that did not touch the realm file."""
    assert "keycloak-init" not in _keycloak_branch(), (
        "keycloak-init is back inside the NEED_KEYCLOAK branch"
    )
    lines = [ln.strip() for ln in _SH_COMMENT.sub("", _script()).splitlines()]
    assert "run_oneshot keycloak-init" in lines, "keycloak-init is not run unconditionally"


def test_every_one_shot_checks_its_exit_code() -> None:
    """`docker compose up <one-shot>` does not reliably fail on a non-zero
    exit, so a failed migration or realm convergence read as success."""
    text = _SH_COMMENT.sub("", _script())
    for svc in ("migrate", "postgres-grants", "openbao-init", "keycloak-init"):
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
    keycloak: int = 0,
    compose: int = 0,
    kc_image: int = 0,
) -> str:
    return f"""
set -euo pipefail
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
NEED_MIGRATE={migrate}; NEED_KEYCLOAK={keycloak}; NEED_COMPOSE={compose}
KC_IMAGE_CHANGED={kc_image}
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
    assert "audit" in out and "DROPS BOTH DATABASES" in out, out


def test_a_compose_or_theme_change_is_undone_from_git_not_by_restoring_the_database() -> None:
    """Restoring Postgres undoes neither -- the running compose file and the
    bind-mounted theme come from the checkout -- and costs every write and
    audited action since the backup."""
    cases = (
        (dict(compose=1), "deploy/docker-compose.yml"),
        (dict(keycloak=1), "deploy/keycloak"),
    )
    for kw, path in cases:
        out = _rollback(**kw)
        assert f"checkout {BASE[:12]} -- " in out and path in out, f"{kw}:\n{out}"
        assert "restore.sh" not in out, f"{kw} sent to a database restore:\n{out}"
    assert "docker compose restart keycloak" in _rollback(keycloak=1)


def test_a_keycloak_image_change_does_need_the_restore() -> None:
    """The one compose change git cannot undo: Keycloak migrates its schema
    one way on start, so the old image cannot read the database."""
    out = _rollback(compose=1, kc_image=1)
    assert "restore.sh" in out and "deploy/docker-compose.yml" in out, out


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
