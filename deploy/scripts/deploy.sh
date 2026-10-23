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
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
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

# A WARNING, NOT A STOP: a blank console address breaks email links and
# Microsoft sign-in, not the deploy -- and CI's throwaway .env leaves it blank.
if ! grep -qE '^ITS_PUBLIC_CONSOLE_URL=https?://[^[:space:]]+' .env; then
    warn "ITS_PUBLIC_CONSOLE_URL is not set in .env. Invitation and password-reset"
    warn "emails will link to the internal address http://keycloak:8080, which"
    warn "nobody can open. Set it to the address people open the console at."
fi

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

if [ -n "$BASELINE" ] && [ "$(git -C "$REPO_ROOT" rev-parse "$BASELINE")" = "$AFTER" ]; then
    say "Already deployed"
    note "the running image was built from HEAD ($SHORT_AFTER) and nothing was pulled."
    note "Nothing to do. Use --full to redeploy anyway."
    exit 0
fi

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
NEED_MIGRATE=0; NEED_GRANTS=0; NEED_KEYCLOAK=0; NEED_OPENBAO=0; NEED_COMPOSE=0
AGENT_CHANGED=0; ENV_CHANGED=0

touched '^backend/'                        && BUILD_BACKEND=1
touched '^frontend/'                       && BUILD_WEB=1
touched '^deploy/docker-compose\.yml$'     && NEED_COMPOSE=1
touched '^deploy/\.env\.example$'          && ENV_CHANGED=1
touched '^agent/'                          && AGENT_CHANGED=1
touched '^deploy/(keycloak|realm)'         && NEED_KEYCLOAK=1
touched '^deploy/(openbao|vault)'          && NEED_OPENBAO=1

# A compose change that moves KEYCLOAK'S IMAGE is not undone by checking the old
# compose file out: Keycloak migrates its own database one way on start, so the
# old image cannot read it. Only that case sends a compose rollback to restore.
# (The diff is captured first and then grepped, not piped: under pipefail a
# `grep -q` that exits early SIGPIPEs `git diff` and the test reads as false.)
KC_IMAGE_CHANGED=0
if [ "$NEED_COMPOSE" = 1 ]; then
    if [ -z "$BASELINE" ]; then
        KC_IMAGE_CHANGED=1    # unknown baseline: assume the worse case
    else
        COMPOSE_DIFF="$(git -C "$REPO_ROOT" diff "$BASELINE" "$AFTER" -- deploy/docker-compose.yml)"
        grep -qE '^[-+][[:space:]]*image:[[:space:]]*quay\.io/keycloak/keycloak:' <<<"$COMPOSE_DIFF" \
            && KC_IMAGE_CHANGED=1
    fi
fi

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
    NEED_KEYCLOAK=1; NEED_OPENBAO=1
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
[ "$NEED_KEYCLOAK" = 1 ] && note "RUN    restart keycloak — realm or theme changed" || note "skip   keycloak restart — no realm or theme change"
note "RUN    keycloak-init — converges the realm allowlist (every deploy)"

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
./scripts/backup.sh "$BACKUP_DIR"

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
rollback_help() {
    local prev="$RUNNING_TAG" base="${BASELINE:-}" n=1
    say "If you need to go back"
    note "deploy/README.md \"Rolling back a deployment\" -- here with this run's values."

    if [ -z "$prev" ] || [ "$prev" = "dev" ] || [ "$prev" = "$SHORT_AFTER" ] \
        || ! docker image inspect "itsuite/backend:$prev" >/dev/null 2>&1 \
        || ! docker image inspect "itsuite/web:$prev" >/dev/null 2>&1; then
        warn "The previous release's images are not both on this host (itsuite/backend and itsuite/web:${prev:-unrecorded})."
        warn "Pointing .env at them would make 'docker compose up -d' REBUILD current source under the old name."
        note "There is no image to swap back to. The backup this run took is at:"
        note "  $BACKUP_DIR"
        [ -n "$base" ] && note "and the commit that was running before is ${base:0:12}."
        return 0
    fi

    note "  $n. docker compose stop api gateway worker web"; n=$((n + 1))
    if [ "$NEED_MIGRATE" = 1 ] || [ "$KC_IMAGE_CHANGED" = 1 ]; then
        warn "This deploy moved a database schema (a migration, or Keycloak's image), so the old images cannot run against it."
        note "  $n. ITS_RESTORE_FORCE=1 ./scripts/restore.sh $BACKUP_DIR"; n=$((n + 1))
        warn "     That DROPS BOTH DATABASES and rewinds the audit log to the backup: every write and every"
        warn "     audited action since then is gone. Export anything you need first (see the runbook)."
    fi
    if [ "$NEED_COMPOSE" = 1 ] || [ "$NEED_KEYCLOAK" = 1 ]; then
        local paths=""
        [ "$NEED_COMPOSE" = 1 ] && paths="deploy/docker-compose.yml"
        [ "$NEED_KEYCLOAK" = 1 ] && paths="${paths:+$paths }deploy/keycloak"
        if [ -n "$base" ]; then
            note "  $n. git -C $REPO_ROOT checkout ${base:0:12} -- $paths"; n=$((n + 1))
            note "     (this leaves the checkout modified, so the next deploy.sh will refuse until you reset it -- deliberately)"
        else
            warn "This deploy changed $paths, and this run could not tell which commit was running before."
            warn "Check those paths out at the previous release by hand; the images alone do not carry them."
        fi
    fi
    note "  $n. grep -q '^IT_SUITE_VERSION=' .env && sed -i 's/^IT_SUITE_VERSION=.*/IT_SUITE_VERSION=$prev/' .env || echo 'IT_SUITE_VERSION=$prev' >> .env"; n=$((n + 1))
    note "  $n. docker compose up -d"; n=$((n + 1))
    [ "$NEED_KEYCLOAK" = 1 ] && note "  $n. docker compose restart keycloak    # it caches the theme and realm in memory"
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
if [ "$NEED_KEYCLOAK" = 1 ]; then
    # RESTART IT, AND RESTART IT FIRST. `deploy/keycloak/` is a read-only bind
    # mount and Keycloak caches the login theme and the realm in memory, so a
    # changed template reaches nobody until this container restarts.
    # `keycloak-init` creates service clients over the admin API and does
    # nothing about either -- so before this, a theme change was detected,
    # announced, "provisioned", and still not serving.
    #
    # That is not hypothetical: the 2026-09-22 fix to login-config-totp.ftl
    # (the QR path posted no totpSecret, so every scan 500'd) would have been
    # deployed by this script and left every user still unable to enrol.
    #
    # Ordering is safe: keycloak-init depends on `service_started` only and
    # polls the realm's discovery document itself before doing anything.
    say "Identity provider"
    "${COMPOSE[@]}" restart keycloak
fi
# EVERY deploy, not only on a realm change: `--import-realm` skips an existing
# realm, so this is the only path by which the repo's realm allowlist reaches
# a running host, and drift made by hand is corrected here too.
say "Identity provisioning"
run_oneshot keycloak-init

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

# -------------------------------------------------------------------- verify

say "Verifying"
FAIL=0

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

CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://itms.toeshee.com/api/v1/devices || echo 000)"
if [ "$CODE" = "401" ]; then
    note "API answers 401 unauthenticated — routing and auth are up."
else
    warn "API returned $CODE (expected 401). 404 usually means api is still starting."
    FAIL=1
fi

DEPLOY_FINISHED=1
if [ "$FAIL" = 0 ]; then
    say "Deployed  $SHORT_AFTER"
else
    say "Deployed with warnings  $SHORT_AFTER"
    exit 1
fi
