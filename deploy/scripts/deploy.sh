#!/usr/bin/env bash
# Pull, decide what actually changed, and run only the steps that change needs.
#
# WHY THIS EXISTS. The upgrade sequence in `deploy/README.md` is a dozen
# commands, most of which are no-ops on most releases, and a human running it
# by hand has to decide each time which ones matter. That decision is
# derivable: `git diff` between the commit the RUNNING IMAGE was built from and
# the commit just pulled says exactly which services need rebuilding, whether
# the schema moved, and whether the compose topology changed.
#
# THE INCIDENT THIS IS BUILT AROUND. On 2026-08-27 the console served new
# frontend code while `api` still ran image `4c51ae70` against a HEAD of
# `147d09ee`, so `POST /api/v1/agent/releases` answered 405 for a route that
# existed in the source. Nothing in the manual sequence compares the running
# tag to HEAD, so the drift was invisible until a publish failed. This script
# takes the OLDER of (running image tag, pre-pull HEAD) as its baseline -- so a
# stale image widens the diff rather than hiding it -- and VERIFIES at the end
# that the deployed tag equals HEAD.
#
# SAFE DIRECTION IS ALWAYS "DO MORE". Every uncertainty -- an unknown baseline,
# a tag that is not an ancestor, a force-push -- resolves to a full deploy, not
# a partial one. A skipped migration is an outage; a redundant idempotent step
# costs seconds.
#
# OPENBAO. It refuses to start while OpenBao is sealed or down, because
# `up -d` would block on it. If this deploy recreates OpenBao and it comes back
# sealed, an interactive run asks for the shares at the prompt (they go to
# `bao`, never to this script); `--yes` stops with the fix instead.
#
# WHAT IT WILL NOT DO. It never edits `.env` beyond IT_SUITE_VERSION, never
# stores or reads OpenBao key shares itself, never publishes an
# agent release, and never runs a `downgrade`. It ends every run by printing
# how to go back from THIS deploy -- the previous tag written into .env, or,
# when that is not enough or not available, the backup it took.
#
# THE FIRST UPGRADE PAST OWN AUTHENTICATION PHASE 4 starts with the pull:
#   git -C .. pull --ff-only && ./scripts/deploy.sh
# The release before runs its own deploy.sh, which pulls part-way through --
# and bash goes on running the text it opened, which restarts a Keycloak this
# compose file no longer has. If that already happened, run ./scripts/deploy.sh
# again: it finishes the upgrade (stop_if_already_deployed).
#
# Usage:
#   ./deploy.sh            # pull, show the plan, ask, then run it
#   ./deploy.sh --plan     # pull, show the plan, change nothing
#   ./deploy.sh --yes      # no prompt (for a scheduled run)
#   ./deploy.sh --full     # ignore the diff, run every step
# shellcheck disable=SC2015  # the plan's `A && note || note` lines: note() cannot fail
set -euo pipefail

cd "$(dirname "$0")/.."          # deploy/
REPO_ROOT="$(cd .. && pwd)"
COMPOSE=(docker compose)

MODE=run
FORCE_FULL=0
for arg in "$@"; do
    case "$arg" in
        --plan) MODE=plan ;;
        --yes)  MODE=yes ;;
        --full) FORCE_FULL=1 ;;
        -h|--help) sed -n '2,47p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

LAST_STEP="starting"
say()  { LAST_STEP="$*"; printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '  \033[33mWARNING\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[31mFAILED\033[0m  %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- preconditions

[ -f .env ] || die "deploy/.env is missing. Copy .env.example and fill it in first."
command -v docker >/dev/null || die "docker is not on PATH"
git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1 || die "not a git checkout"

# A dirty tree cannot be tagged with a bare SHA (build-images.sh refuses it, for
# the good reason that such a tag is a rollback target nobody can reproduce).
# Catch it here rather than after the pull and the backup.
if ! git -C "$REPO_ROOT" diff --quiet HEAD 2>/dev/null; then
    die "the checkout has uncommitted changes. Commit or stash them, then re-run."
fi

# ------------------------------------------------------------ OpenBao seal
#
# A sealed OpenBao makes `up -d` BLOCK: openbao-init waits on a healthcheck
# that passes only when unsealed. So check before changing anything, and
# again after `openbao` itself may have been recreated, and stop with the fix.

openbao_state() {
    local rc=0
    "${COMPOSE[@]}" exec -T openbao bao status >/dev/null 2>&1 </dev/null || rc=$?
    case "$rc" in 0) echo unsealed ;; 2) echo sealed ;; *) echo down ;; esac
}

auto_unseal_on() {
    grep -qE '^COMPOSE_FILE=.*openbao/auto-unseal\.yml' .env 2>/dev/null
}

openbao_help() {
    if [ "$1" = down ]; then
        note "Start it, then check it:"
        note "  docker compose up -d openbao"
        note "  docker compose exec openbao bao status"
    fi
    if auto_unseal_on; then
        note "Auto-unseal is configured, so a sealed OpenBao means its key file is"
        note "missing or unreadable:"
        note "  ./scripts/openbao-seal.sh status"
        note "  docker compose logs --tail 50 openbao"
    else
        note "Unseal it with 3 of the 5 key shares, one per prompt:"
        note "  docker compose exec openbao bao operator unseal"
        note "  docker compose exec openbao bao operator unseal"
        note "  docker compose exec openbao bao operator unseal"
        note "To stop doing this after every reboot: ./scripts/openbao-seal.sh enable"
        note "(read the trade-off in deploy/GO-LIVE.md section 4 first)."
    fi
}

# Before the pull. Nothing has changed yet, so stopping here costs nothing.
preflight_openbao() {
    local state
    state="$(openbao_state)"
    [ "$state" = unsealed ] && return 0
    say "OpenBao is $([ "$state" = sealed ] && echo SEALED || echo 'not running')"
    openbao_help "$state"
    if [ "$MODE" = plan ]; then
        warn "--plan continues, but a real deploy stops here until this is fixed."
        return 0
    fi
    die "OpenBao must be unsealed before a deploy: 'up -d' would block on it. Fix it, then re-run ./scripts/deploy.sh"
}

# After the build, before the tag moves: apply any change to the openbao
# service, which recreates it -- and a recreated Shamir OpenBao is sealed.
ensure_openbao_unsealed() {
    local state waited=0 tries=0
    "${COMPOSE[@]}" up -d --no-deps openbao </dev/null
    state="$(openbao_state)"
    while [ "$state" = down ] && [ "$waited" -lt 60 ]; do
        sleep 2; waited=$((waited + 2)); state="$(openbao_state)"
    done
    [ "$state" = unsealed ] && { note "OpenBao unsealed"; return 0; }
    if [ "$state" = sealed ] && [ "$MODE" = run ] && [ -t 0 ] && ! auto_unseal_on; then
        warn "OpenBao was restarted by this deploy and is SEALED. Enter 3 key shares now."
        while [ "$state" = sealed ] && [ "$tries" -lt 5 ]; do
            "${COMPOSE[@]}" exec openbao bao operator unseal || true
            tries=$((tries + 1)); state="$(openbao_state)"
        done
        [ "$state" = unsealed ] && { note "OpenBao unsealed"; return 0; }
    fi
    openbao_help "$state"
    die "OpenBao is $state after being recreated. Nothing after the build has run; fix it, then re-run ./scripts/deploy.sh"
}

# Compose one-shots: `up` alone does not fail on a non-zero exit everywhere,
# so read the container's exit code and stop on anything but 0.
run_oneshot() {
    local id code
    "${COMPOSE[@]}" up "$1"
    id="$("${COMPOSE[@]}" ps -aq "$1" | head -1)"
    code="$(docker inspect -f '{{.State.ExitCode}}' "$id" 2>/dev/null || echo unknown)"
    [ "$code" = 0 ] || die "$1 exited $code. Read: docker compose logs $1"
}

preflight_openbao

# A WARNING, NOT A STOP: a blank console address breaks sign-in, email links
# and the OAuth server for AI apps, not the deploy -- and CI's throwaway .env
# starts blank.
if ! grep -qE '^ITS_PUBLIC_CONSOLE_URL=https?://[^[:space:]]+' .env; then
    warn "ITS_PUBLIC_CONSOLE_URL is not set in .env. Nobody can sign in to the console,"
    warn "invitations and password resets carry no link, and AI apps cannot connect."
    warn "Set it to the address people open the console at."
fi

# KEYS THIS RELEASE NO LONGER READS (own authentication phase 4 removed the old
# identity provider). No compose service of this release interpolates them,
# and its containers ignore them: pydantic-settings reads an environment
# variable only for a field it declares. BUT THE RELEASE BEFORE NEEDS FOUR:
# its compose file does not parse without KEYCLOAK_ADMIN_USER and
# KEYCLOAK_ADMIN_PASSWORD, and its api, gateway and worker do not
# start without ITS_OIDC_ISSUER and ITS_OIDC_JWKS_URL. So they stay until the
# operator closes the way back, in deploy/README.md "Retiring the old
# identity provider's data" (operator decision K1, 2026-11-15). Named here,
# and never removed here: this script never edits .env beyond IT_SUITE_VERSION.
OBSOLETE_ENV_KEYS=(
    KEYCLOAK_ADMIN_USER KEYCLOAK_ADMIN_PASSWORD
    ITS_OIDC_ISSUER ITS_OIDC_JWKS_URL ITS_OIDC_AUDIENCE ITS_OIDC_ORG_CLAIM
    ITS_OIDC_CONSOLE_CLIENT_ID ITS_MULTI_TENANT
    ENTRA_TENANT_ID ENTRA_CLIENT_ID ENTRA_CLIENT_SECRET
    ITS_KEYCLOAK_ADMIN_URL ITS_KEYCLOAK_REALM
    ITS_KEYCLOAK_EVENTS_CLIENT_ID ITS_KEYCLOAK_EVENTS_SECRET_PATH
    ITS_KEYCLOAK_ADMIN_CLIENT_ID ITS_KEYCLOAK_ADMIN_SECRET_PATH
)
# The four of them the release before cannot start without.
ROLLBACK_ENV_KEYS=(KEYCLOAK_ADMIN_USER KEYCLOAK_ADMIN_PASSWORD ITS_OIDC_ISSUER ITS_OIDC_JWKS_URL)
obsolete_env_keys() {
    local key found=()
    for key in "${OBSOLETE_ENV_KEYS[@]}"; do
        if grep -qE "^${key}=" .env; then found+=("$key"); fi
    done
    if [ ${#found[@]} -gt 0 ]; then
        note ".env holds ${#found[@]} key(s) this release no longer reads:"
        note "  ${found[*]}"
        note "Do NOT remove them yet: a rollback past own authentication phase 4 needs ${ROLLBACK_ENV_KEYS[*]}."
        note "deploy/README.md \"Retiring the old identity provider's data\" removes them, when you choose."
    fi
    return 0
}
obsolete_env_keys

# THE OLD IDENTITY PROVIDER'S CONTAINERS (own authentication phase 4; the
# coordinator's decision, 2026-11-15). Compose leaves a container whose
# service left the file RUNNING, with only a warning, and Traefik keeps
# routing to it by the labels on the container itself: Keycloak would go on
# answering /realms, its anonymous client registration included, on :443.
# These two, BY NAME, and nothing else -- not `up --remove-orphans`, which
# would also remove any container the operator added to this project.
# Neither holds data: Keycloak's is in Postgres, and its image declares no
# volume.
#
# FOUND BY THE LABELS ON THE CONTAINERS THEMSELVES -- this project's
# `com.docker.compose.project` and the service's `com.docker.compose.service`,
# each an exact match -- not by `compose ps`, whose listing of a container
# whose service left the file is Compose's choice to make. The half-done
# check, the removal and the Verify step all ask this one query. And a docker
# that does not answer STOPS THE RUN: read as "none left", an error would let
# Keycloak go on answering on :443 behind a green deploy. Called in `$(...)`,
# so `die` ends only that subshell: every caller exits on its status.
old_identity_containers() {
    local cfg project service names
    if ! cfg="$("${COMPOSE[@]}" config --no-interpolate 2>&1 </dev/null)"; then
        printf '%s\n' "$cfg" >&2
        die "docker compose config failed, so this run cannot look for the old identity provider's containers."
    fi
    project="$(awk '/^name: / { print $2; exit }' <<<"$cfg")"
    [ -n "$project" ] \
        || die "docker compose config named no project, so this run cannot look for the old identity provider's containers."
    for service in keycloak keycloak-init; do
        names="$(docker ps -a --format '{{.Names}}' \
            --filter "label=com.docker.compose.project=$project" \
            --filter "label=com.docker.compose.service=$service" </dev/null)" \
            || die "docker ps failed, so this run cannot tell whether the old identity provider's containers are gone. Is the Docker daemon running?"
        [ -z "$names" ] || printf '%s\n' "$names"
    done
}
remove_old_identity_containers() {
    local name found
    found="$(old_identity_containers)" || exit 1
    if [ -z "$found" ]; then
        note "none left"
        return 0
    fi
    while read -r name; do
        [ -n "$name" ] || continue
        docker stop "$name" >/dev/null
        docker rm "$name" >/dev/null
        note "removed $name"
    done <<<"$found"
}

RUNNING_TAG="$(grep -E '^IT_SUITE_VERSION=' .env | tail -1 | cut -d= -f2- || true)"
BEFORE="$(git -C "$REPO_ROOT" rev-parse HEAD)"

# ---------------------------------------------------------------------- pull

say "Pulling"
git -C "$REPO_ROOT" pull --ff-only
AFTER="$(git -C "$REPO_ROOT" rev-parse HEAD)"
SHORT_AFTER="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
note "before: $(git -C "$REPO_ROOT" rev-parse --short "$BEFORE")   after: $SHORT_AFTER"
note "running image tag: ${RUNNING_TAG:-(none recorded)}"

# ------------------------------------------------------- choose the baseline
#
# The baseline is what the RUNNING SYSTEM was built from, which is not
# necessarily the pre-pull HEAD: a previous deploy may have pulled without
# rebuilding (the 405 incident). Take whichever of the two is the ANCESTOR --
# i.e. the older -- so a stale image produces a wider diff, never a narrower
# one. Anything we cannot resolve means a full deploy.

BASELINE=""
if [ "$FORCE_FULL" = 1 ]; then
    note "--full given: every step will run"
elif [ -z "$RUNNING_TAG" ]; then
    warn "no IT_SUITE_VERSION in .env — cannot tell what is running. Full deploy."
elif ! git -C "$REPO_ROOT" rev-parse --verify --quiet "${RUNNING_TAG}^{commit}" >/dev/null; then
    warn "IT_SUITE_VERSION '$RUNNING_TAG' is not a commit in this checkout. Full deploy."
elif ! git -C "$REPO_ROOT" merge-base --is-ancestor "$RUNNING_TAG" "$AFTER"; then
    warn "the running tag is not an ancestor of HEAD (rebase or force-push?). Full deploy."
else
    if git -C "$REPO_ROOT" merge-base --is-ancestor "$RUNNING_TAG" "$BEFORE"; then
        BASELINE="$RUNNING_TAG"    # the image is older than the checkout was
    else
        BASELINE="$BEFORE"
    fi
fi

# NOT "Already deployed" WHILE THE OLD IDENTITY PROVIDER'S CONTAINERS ARE HERE.
# The deploy.sh of a release before own authentication phase 4, run as usual,
# pulls this release, records its tag in .env and migrates -- and then stops
# at `restart keycloak`, a service this compose file no longer has, with the
# app tier still on the old images. Re-run, this script would read the new
# tag as running and stop here with exit 0. Those two containers are the mark
# of that half-done upgrade, so it finishes it as a full deploy instead.
#
# AND THE WAY BACK IS NOT THIS RUN'S BACKUP. This run backs up a database the
# stopped run already migrated past 0248, which no release before phase 4 can
# read. The way back is the backup the stopped run took first: the newest
# `pre-upgrade-*` whose schema is below 0248 (its `alembic_version`), with the
# images its `image_version` names. That backup has no `keycloak.dump` (the
# new backup.sh took it without the flag), and needs none: the old identity
# provider's database is still on the cluster, and restore.sh touches only the
# databases a backup holds. rollback_help prints that backup and that tag --
# or, when this host holds no such backup, says so and names none
# (HALF_DONE_UPGRADE): this run's own backup is no way back past phase 4.
ROLLBACK_BACKUP_DIR=""; ROLLBACK_BASE=""; HALF_DONE_UPGRADE=0
way_back_backup() {
    local dir rev found=""
    for dir in "$HOME"/backups/pre-upgrade-*/; do
        dir="${dir%/}"
        [ -r "$dir/alembic_version" ] || continue
        rev="$(tr -d '[:space:]' < "$dir/alembic_version")"
        case "$rev" in [0-9][0-9][0-9][0-9]) ;; *) continue ;; esac
        [ "$((10#$rev))" -lt 248 ] && found="$dir"
    done
    printf '%s' "$found"
}
#
# WHENEVER THE RUNNING TAG IS HEAD, WHATEVER BASELINE IS. `--full` leaves
# BASELINE empty, and a check that needed one let a `--full` rerun of a
# half-done upgrade name its own post-0248 backup as the way back. "Already
# deployed" still needs a known BASELINE: `--full` always redeploys.
stop_if_already_deployed() {
    local old_tag old running
    [ -n "$RUNNING_TAG" ] || return 0
    running="$(git -C "$REPO_ROOT" rev-parse --verify --quiet "${RUNNING_TAG}^{commit}" 2>/dev/null)" \
        || return 0
    [ "$running" = "$AFTER" ] || return 0
    old="$(old_identity_containers)" || exit 1
    if [ -n "$old" ]; then
        warn "the old identity provider's containers are still here: an upgrade past own"
        warn "authentication phase 4 stopped part-way. Finishing it as a full deploy."
        BASELINE=""
        HALF_DONE_UPGRADE=1
        ROLLBACK_BACKUP_DIR="$(way_back_backup)"
        if [ -z "$ROLLBACK_BACKUP_DIR" ]; then
            warn "No backup under $HOME/backups/pre-upgrade-* is from before migration 0248, so"
            warn "this run cannot name the way back past phase 4. This run's own backup is not it."
            return 0
        fi
        old_tag="$(sed -n 's/^IT_SUITE_VERSION=//p' "$ROLLBACK_BACKUP_DIR/image_version" 2>/dev/null | tail -1 || true)"
        case "$old_tag" in *[!A-Za-z0-9._-]*) old_tag="" ;; esac
        warn "The way back is the backup the stopped run took first, not the one this run takes:"
        warn "  $ROLLBACK_BACKUP_DIR (schema $(tr -d '[:space:]' < "$ROLLBACK_BACKUP_DIR/alembic_version"), images ${old_tag:-unrecorded})"
        if [ -n "$old_tag" ]; then
            RUNNING_TAG="$old_tag"
            if git -C "$REPO_ROOT" rev-parse --verify --quiet "${old_tag}^{commit}" >/dev/null 2>&1; then
                ROLLBACK_BASE="$old_tag"
            fi
        fi
        return 0
    fi
    [ -n "$BASELINE" ] || return 0
    [ "$(git -C "$REPO_ROOT" rev-parse "$BASELINE")" = "$AFTER" ] || return 0
    say "Already deployed"
    note "the running image was built from HEAD ($SHORT_AFTER) and nothing was pulled."
    note "Nothing to do. Use --full to redeploy anyway."
    exit 0
}
stop_if_already_deployed

# ------------------------------------------------------------- what changed

if [ -n "$BASELINE" ]; then
    CHANGED="$(git -C "$REPO_ROOT" diff --name-only "$BASELINE" "$AFTER")"
    NEW_FILES="$(git -C "$REPO_ROOT" diff --name-only --diff-filter=A "$BASELINE" "$AFTER")"
else
    CHANGED="$(git -C "$REPO_ROOT" ls-files)"   # full deploy: treat everything as changed
    NEW_FILES="$CHANGED"
fi

touched() { grep -qE "$1" <<<"$CHANGED"; }

BUILD_BACKEND=0; BUILD_WEB=0
NEED_MIGRATE=0; NEED_GRANTS=0; NEED_OPENBAO=0; NEED_COMPOSE=0
AGENT_CHANGED=0; ENV_CHANGED=0

touched '^backend/'                        && BUILD_BACKEND=1
touched '^frontend/'                       && BUILD_WEB=1
touched '^deploy/docker-compose\.yml$'     && NEED_COMPOSE=1
touched '^deploy/\.env\.example$'          && ENV_CHANGED=1
touched '^agent/'                          && AGENT_CHANGED=1
touched '^deploy/(openbao|vault)'          && NEED_OPENBAO=1

# A NEW migration file is the signal, not any edit under migrations/: shipped
# revisions are never edited (CLAUDE.md), so an edit there is a comment or the
# one approved downgrade carve-out, neither of which needs `alembic upgrade`.
MIGRATIONS="$(grep -E '^backend/migrations/versions/.*\.py$' <<<"$NEW_FILES" || true)"
[ -n "$MIGRATIONS" ] && { NEED_MIGRATE=1; NEED_GRANTS=1; }

# A new table needs `postgres-grants` even if it arrived in an edited revision,
# and grants are idempotent -- so any migrations touch at all runs them.
touched '^backend/migrations/' && NEED_GRANTS=1

# The compose file defines the services; if it moved, everything comes up.
if [ "$NEED_COMPOSE" = 1 ]; then BUILD_BACKEND=1; BUILD_WEB=1; fi
if [ -z "$BASELINE" ]; then
    BUILD_BACKEND=1; BUILD_WEB=1; NEED_MIGRATE=1; NEED_GRANTS=1
    NEED_OPENBAO=1
fi

# ------------------------------------------------------------------ the plan

say "Plan  ($(git -C "$REPO_ROOT" rev-parse --short "${BASELINE:-$AFTER}") -> $SHORT_AFTER)"
if [ -n "$BASELINE" ]; then
    note "$(wc -l <<<"$CHANGED" | tr -d ' ') file(s) changed"
else
    note "baseline unknown — running every step"
fi
[ "$BUILD_BACKEND" = 1 ] && note "build  itsuite/backend  (api, gateway, worker, migrate)" || note "skip   backend build — no backend/ change"
[ "$BUILD_WEB" = 1 ]     && note "build  web" || note "skip   web build — no frontend/ change"
if [ "$NEED_MIGRATE" = 1 ]; then
    note "RUN    migrate — new revision(s):"
    while read -r m; do [ -n "$m" ] && note "         $(basename "$m")"; done <<<"$MIGRATIONS"
else
    note "skip   migrate — no new revision"
fi
[ "$NEED_GRANTS" = 1 ]   && note "RUN    postgres-grants" || note "skip   postgres-grants — no schema change"
[ "$NEED_OPENBAO" = 1 ]  && note "RUN    openbao-init" || note "skip   openbao-init — no PKI change"
note "RUN    remove the old identity provider's containers (keycloak, keycloak-init), if any are left"

if [ "$ENV_CHANGED" = 1 ]; then
    warn ".env.example changed — a new setting may be REQUIRED. This script never edits .env."
    git -C "$REPO_ROOT" diff "${BASELINE:-$AFTER}" "$AFTER" -- deploy/.env.example \
        | grep -E '^\+[A-Z_]+=' | sed 's/^/         /' || true
fi
if [ "$AGENT_CHANGED" = 1 ]; then
    warn "agent/ changed — endpoints keep running the version they have."
    note "         Rebuild and publish a release for the new agent to reach the fleet;"
    note "         site servers need it before push-install works. Not done here."
fi

[ "$MODE" = plan ] && { say "--plan: nothing was changed."; exit 0; }
if [ "$MODE" = run ]; then
    printf '\nProceed? [y/N] '
    read -r reply
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

# -------------------------------------------------------------------- backup
#
# ALWAYS, not only when the schema moves. The cheap case costs seconds and the
# expensive case is the one nobody predicted. globals.sql carries the roles'
# SCRAM hashes -- encrypt the directory before it leaves this host.

say "Backing up"
# Kept in a variable because it is the rollback of last resort, and the end of
# this script has to be able to say where it is.
BACKUP_DIR="$HOME/backups/pre-upgrade-$(date -u +%FT%H%MZ)"
# NEVER INTO A BACKUP THAT IS ALREADY THERE. The name has minute resolution and
# backup.sh writes into a directory that exists: a rerun within the same UTC
# minute overwrote the first run's backup -- after an upgrade past own
# authentication phase 4 stopped part-way, possibly the only one from before
# migration 0248. Nothing has changed yet but the pull, so stopping costs a
# minute.
if [ -e "$BACKUP_DIR" ]; then
    die "$BACKUP_DIR already exists: a deploy backed up into it less than a minute ago, and it may be the only backup from before this upgrade. Nothing was written to it. Wait a minute, then re-run ./scripts/deploy.sh"
fi
# With the old identity provider's database while one exists: the backup taken
# before the phase-4 upgrade is the way back past it, and the nightly ones
# leave that database out (backup.sh; operator decision K1, 2026-11-15).
ITS_BACKUP_OLD_IDENTITY_DB=1 ./scripts/backup.sh "$BACKUP_DIR"

# ------------------------------------------------------------------ rollback
#
# deploy/README.md "Rolling back a deployment" IS THE PROCEDURE. This prints
# that procedure's steps, in its order, with this run's values filled in -- it
# does not invent a shorter one. An earlier version did, and got four things
# wrong: it skipped step 2 (stop the app tier, without which an old image
# meets a new schema and never starts), its sed silently matched nothing on a
# .env that predates IT_SUITE_VERSION, it checked only the backend image when
# a missing web tag makes `up -d` rebuild CURRENT source under the OLD name,
# and it sent compose and theme changes to a database restore -- which undoes
# neither, and destroys every write and audited action since the backup.
#
# INSTALLED AS AN EXIT TRAP HERE, straight after the backup and before the
# first step that changes anything, so a run that dies half-way through a
# migration still says how to get back. Printed on success too: a problem
# found an hour later is found with this still in the scrollback.
#
# A RELEASE FROM BEFORE OWN AUTHENTICATION PHASE 4 needs ROLLBACK_ENV_KEYS, which
# this one does not read. Says so if .env lost any: for certain when the commit
# that ran before is known and its compose file names them, as a condition when
# it is not known (no commit, or one this checkout does not have). Every path of
# rollback_help asks, the restore-only one included: rebuilding the old release
# needs them as much as swapping back to its images does.
rollback_env_keys_check() {
    local base="$1" old_compose key missing=()
    for key in "${ROLLBACK_ENV_KEYS[@]}"; do
        grep -qE "^${key}=." .env 2>/dev/null || missing+=("$key")
    done
    [ ${#missing[@]} -gt 0 ] || return 0
    if [ -n "$base" ] && old_compose="$(git -C "$REPO_ROOT" show "$base:deploy/docker-compose.yml" 2>/dev/null)"; then
        grep -q 'KEYCLOAK_ADMIN_USER' <<<"$old_compose" || return 0
        warn "The release you would go back to predates own authentication phase 4. Its compose file"
        warn "or its api stops without these .env keys, which .env no longer has: ${missing[*]}"
    else
        warn "This run does not know which release ran before, or cannot read it from this checkout, so it"
        warn "cannot tell whether that release predates own authentication phase 4. If it does, its compose"
        warn "file or its api stops without these .env keys, which .env no longer has: ${missing[*]}"
    fi
    warn "Put them back, with the values they had, before the next step."
    return 0
}
rollback_help() {
    local prev="$RUNNING_TAG" base="${ROLLBACK_BASE:-${BASELINE:-}}" n=1
    # Past an upgrade that stopped part-way, the backup taken before it
    # (stop_if_already_deployed), not this run's, which is past 0248.
    local way_back="${ROLLBACK_BACKUP_DIR:-$BACKUP_DIR}"
    say "If you need to go back"
    note "deploy/README.md \"Rolling back a deployment\" -- here with this run's values."
    if [ -n "${ROLLBACK_BACKUP_DIR:-}" ]; then
        note "An upgrade stopped part-way before this run: the way back is the backup it took first."
        note "This run's own backup ($BACKUP_DIR) is past migration 0248 and cannot go back past it."
    fi
    if [ "${HALF_DONE_UPGRADE:-0}" = 1 ] && [ -z "${ROLLBACK_BACKUP_DIR:-}" ]; then
        warn "An upgrade past own authentication phase 4 stopped part-way before this run."
        warn "NO BACKUP ON THIS HOST GOES BACK PAST IT: none under $HOME/backups/pre-upgrade-* is from"
        warn "before migration 0248, and the one this run took is past it. So this names none to restore."
        note "The way back is the backup that stopped run took first, if it is kept elsewhere: the"
        note "pre-upgrade-* directory whose alembic_version is below 0248, restored with"
        note "ITS_RESTORE_FORCE=1 and scripts/restore.sh. Without it, the only way is forward."
        rollback_env_keys_check ""
        return 0
    fi

    if [ -z "$prev" ] || [ "$prev" = "dev" ] || [ "$prev" = "$SHORT_AFTER" ] \
        || ! docker image inspect "itsuite/backend:$prev" >/dev/null 2>&1 \
        || ! docker image inspect "itsuite/web:$prev" >/dev/null 2>&1; then
        warn "The previous release's images are not both on this host (itsuite/backend and itsuite/web:${prev:-unrecorded})."
        warn "Pointing .env at them would make 'docker compose up -d' REBUILD current source under the old name."
        note "There is no image to swap back to. The backup to restore is at:"
        note "  $way_back"
        [ -n "$base" ] && note "and the commit that was running before is ${base:0:12}."
        rollback_env_keys_check "$base"
        return 0
    fi

    note "  $n. docker compose stop api gateway worker web"; n=$((n + 1))
    if [ "$NEED_MIGRATE" = 1 ]; then
        warn "This deploy moved the database schema (a migration), so the old images cannot run against it."
        note "  $n. ITS_RESTORE_FORCE=1 ./scripts/restore.sh $way_back"; n=$((n + 1))
        warn "     That DROPS THE DATABASES IN THE BACKUP and rewinds the audit log to it: every write and"
        warn "     every audited action since then is gone. Export anything you need first (see the runbook)."
    fi
    # THE WHOLE deploy/ DIRECTORY, not the compose file alone: an earlier
    # release's compose file bind-mounts files of that release (the old
    # identity provider's realm and theme, until own authentication phase 4),
    # which a newer checkout may no longer have.
    if [ "$NEED_COMPOSE" = 1 ]; then
        if [ -n "$base" ]; then
            note "  $n. git -C $REPO_ROOT checkout ${base:0:12} -- deploy"; n=$((n + 1))
            note "     (this leaves the checkout modified, so the next deploy.sh will refuse until you reset it -- deliberately)"
        else
            warn "This deploy changed deploy/docker-compose.yml, and this run could not tell which commit was running before."
            warn "Check deploy/ out at the previous release by hand; the images alone do not carry it."
        fi
    fi
    rollback_env_keys_check "$base"
    note "  $n. grep -q '^IT_SUITE_VERSION=' .env && sed -i 's/^IT_SUITE_VERSION=.*/IT_SUITE_VERSION=$prev/' .env || echo 'IT_SUITE_VERSION=$prev' >> .env"; n=$((n + 1))
    note "  $n. docker compose up -d"; n=$((n + 1))
    return 0
}

DEPLOY_FINISHED=0
on_exit() {
    local code=$?
    if [ "$code" -ne 0 ] && [ "$DEPLOY_FINISHED" = 0 ]; then
        warn "Stopped part-way, during \"$LAST_STEP\" (exit $code). Nothing after that step ran."
    fi
    rollback_help
}
trap on_exit EXIT

# --------------------------------------------------------------------- build

if [ "$BUILD_BACKEND" = 1 ] || [ "$BUILD_WEB" = 1 ]; then
    say "Building images"
    ./scripts/build-images.sh
fi

say "OpenBao"
ensure_openbao_unsealed

say "Selecting the tag"
if grep -qE '^IT_SUITE_VERSION=' .env; then
    sed -i "s/^IT_SUITE_VERSION=.*/IT_SUITE_VERSION=$SHORT_AFTER/" .env
else
    echo "IT_SUITE_VERSION=$SHORT_AFTER" >> .env
fi
note "IT_SUITE_VERSION=$SHORT_AFTER"

# ------------------------------------------------------------------ migrate
#
# BEFORE the application restarts, always. An api container running new code
# against an old schema is the failure this ordering exists to prevent.

if [ "$NEED_MIGRATE" = 1 ]; then
    say "Migrating"
    run_oneshot migrate
fi
if [ "$NEED_GRANTS" = 1 ]; then
    say "Granting"
    run_oneshot postgres-grants
fi
[ "$NEED_OPENBAO" = 1 ]  && { say "OpenBao provisioning";  run_oneshot openbao-init; }
# ------------------------------------------------------------------- restart
#
# --force-recreate on the services whose IMAGE changed. Compose will not
# recreate a container whose config is unchanged, and IT_SUITE_VERSION moving is
# a config change -- but being explicit is what stops a stale container
# surviving, which is the whole reason this script exists.
#
# `worker` runs celery with --beat, so there is no separate beat service: a new
# beat entry does not fire until this container is recreated.

say "Restarting"
RECREATE=()
[ "$BUILD_BACKEND" = 1 ] && RECREATE+=(api gateway worker)
[ "$BUILD_WEB" = 1 ]     && RECREATE+=(web)
if [ ${#RECREATE[@]} -gt 0 ]; then
    "${COMPOSE[@]}" up -d --force-recreate "${RECREATE[@]}"
fi
"${COMPOSE[@]}" up -d
# After the new app tier is up, not before: until then a deploy that stops
# leaves the release before it able to run.
say "Removing the old identity provider's containers"
remove_old_identity_containers

# -------------------------------------------------------------------- verify

say "Verifying"
FAIL=0

# THE OLD IDENTITY PROVIDER'S CONTAINERS ARE GONE, by the same label query the
# removal made (old_identity_containers), so this does not rest on `compose ps`
# listing a container whose service left the file. One left is still routed by
# Traefik, so it fails the deploy and says how to remove it; a docker that does
# not answer stops the run there, as it does everywhere else.
verify_old_identity_gone() {
    local left
    left="$(old_identity_containers)" || exit 1
    if [ -z "$left" ]; then
        note "no container of the old identity provider is left"
        return 0
    fi
    warn "a container of the old identity provider is still here, and Traefik routes to it:"
    warn "  docker rm -f ${left//$'\n'/ }"
    return 1
}
verify_old_identity_gone || FAIL=1

DEPLOYED="$("${COMPOSE[@]}" images api --format json 2>/dev/null | grep -o '"Tag":"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
[ -z "$DEPLOYED" ] && DEPLOYED="$("${COMPOSE[@]}" images api 2>/dev/null | awk 'NR==2{print $3}')"
if [ "$DEPLOYED" = "$SHORT_AFTER" ]; then
    note "api image tag $DEPLOYED == HEAD.  This is the check the 405 incident needed."
else
    warn "api is running tag '$DEPLOYED' but HEAD is '$SHORT_AFTER'."
    warn "That is the stale-image condition. Re-run with --full."
    FAIL=1
fi

HEAD_REV="$("${COMPOSE[@]}" run --rm migrate alembic current 2>/dev/null | grep -oE '^[0-9]{4}' | tail -1 || true)"
note "alembic current: ${HEAD_REV:-unknown}"

for svc in api gateway worker web; do
    st="$("${COMPOSE[@]}" ps --format '{{.Service}} {{.Status}}' 2>/dev/null | awk -v s="$svc" '$1==s{$1="";print}')"
    case "$st" in
        *unhealthy*|"") warn "$svc: ${st:-not running}"; FAIL=1 ;;
        *) note "$svc:$st" ;;
    esac
done

# A sealed OpenBao means no job can be signed. It comes back sealed after any
# recreate and needs the operator's key shares -- this script cannot unseal it.
if [ "$(openbao_state)" != unsealed ]; then
    warn "OpenBao is not unsealed — no job can be signed until it is."
    openbao_help "$(openbao_state)"
    FAIL=1
fi

# Scheduled platform backup: reported, not required for this deploy.
if systemctl is-enabled --quiet its-platform-backup.timer 2>/dev/null; then
    LAST_OK="$(cat /var/lib/it-suite/backup/last-success 2>/dev/null || echo never)"
    note "nightly platform backup: timer enabled, last success: $LAST_OK"
else
    warn "no scheduled platform backup. Install it once: sudo ./scripts/install-backup-timer.sh"
fi

# The host this deployment answers on, from .env (design 2026-11-23 §3):
# until 2026-11-23 this line named one deployment's host for everybody.
PUBLIC_HOST="$(sed -n 's/^ITS_PUBLIC_HOST=//p' .env | tail -1)"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://${PUBLIC_HOST:-localhost}/api/v1/devices" || echo 000)"
if [ "$CODE" = "401" ]; then
    note "API answers 401 unauthenticated — routing and auth are up."
else
    warn "API returned $CODE (expected 401). 503 means the api is not healthy yet (Traefik's api-unavailable router); 404 means Traefik has no router for this host."
    FAIL=1
fi

# The certificate on :443, which Traefik asks Let's Encrypt for and renews by
# itself. A failure there does not fail a request above -- Traefik serves its
# own self-signed default instead -- so it is checked on its own (design
# 2026-11-23 §2).
if ./scripts/check-public-tls.sh --wait 60; then
    note "the certificate on :443 is trusted and names $PUBLIC_HOST."
else
    warn "the certificate on :443 is not one a browser accepts (above)."
    FAIL=1
fi

DEPLOY_FINISHED=1
if [ "$FAIL" = 0 ]; then
    say "Deployed  $SHORT_AFTER"
else
    say "Deployed with warnings  $SHORT_AFTER"
    exit 1
fi
