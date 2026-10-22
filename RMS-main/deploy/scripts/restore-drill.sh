#!/usr/bin/env bash
# Monthly restore drill: restore a platform backup into a THROWAWAY compose
# project on this host and check it comes up. Never touches the live stack.
#
#   ./scripts/restore-drill.sh                 # the latest nightly backup
#   ./scripts/restore-drill.sh BACKUP_DIR      # a specific one
#   ./scripts/restore-drill.sh --keep          # leave the drill running to inspect
#   ./scripts/restore-drill.sh --skip-keycloak # databases only (less memory)
#
# WHAT IT PROVES: both dumps restore; restore.sh's own end-state checks pass
# (RLS policy count equals the backup's, app_user narrowed); the schema is at
# the backup's alembic revision; app_user cannot bypass RLS; the audit chain's
# linkage is intact (docs/07 §4 pre-flight); Keycloak boots on the restored
# database and serves the realm. WHAT IT DOES NOT: key material (OpenBao, the
# CA, the audit HMAC key) and the API are not in this backup and not started.
#
# ISOLATION: the project name is fixed below and compared with the live one;
# COMPOSE_* variables are unset; the drill file publishes no ports, mounts no
# live volume, and gets passwords generated per run. Teardown removes only
# volumes of the drill project.
set -euo pipefail

readonly DRILL_PROJECT=itms-restore-drill
DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DRILL_FILE="$DEPLOY_DIR/drill/docker-compose.drill.yml"
CONF="${ITS_BACKUP_CONF:-/etc/it-suite/backup.conf}"
STATE_DIR="${ITS_BACKUP_STATE_DIR:-/var/lib/it-suite/backup}"
STATUS_DIR="${ITS_BACKUP_STATUS_DIR:-/var/lib/it-suite/backup-status}"
KC_WAIT_S="${ITS_DRILL_KEYCLOAK_WAIT_S:-240}"
KEEP=0; SKIP_KC=0; BACKUP_DIR=""

for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=1 ;;
        --skip-keycloak) SKIP_KC=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) echo "unknown argument: $arg" >&2; exit 2 ;;
        *) BACKUP_DIR="$arg" ;;
    esac
done

# status_write NAME JSON: as in scheduled-backup.sh -- atomic, world-readable
# (the containers read it through a read-only mount), never fatal.
status_write() {
    local dir="$STATUS_DIR" tmp
    {
        mkdir -p "$dir" && chmod 755 "$dir" &&
            tmp="$(mktemp "$dir/.$1.XXXXXX")" &&
            printf '%s\n' "$2" > "$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$dir/$1"
    } 2>/dev/null || true
}

RECORDED=0; STARTED=0
record_drill() {  # pass | fail
    status_write drill.json "{\"result\":\"$1\",\"at\":\"$(date -u +%FT%TZ)\"}"
    RECORDED=1
}

# A drill that stops early -- refused, no backup, a compose error -- did not
# pass, so it is recorded as failed rather than left looking like the last one.
# shellcheck disable=SC2317  # invoked by the EXIT trap
on_exit() {
    local rc=$?
    [ "$RECORDED" = 1 ] || [ "$rc" = 0 ] || record_drill fail
    [ "$STARTED" = 1 ] && teardown
    return 0
}
trap on_exit EXIT

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
FAILURES=()
check() { if [ "$2" = ok ]; then note "PASS  $1"; else note "FAIL  $1 ($2)"; FAILURES+=("$1"); fi; }
# expect NAME ACTUAL WANTED WHY-IF-NOT
expect() { if [ -n "$2" ] && [ "$2" = "$3" ]; then check "$1" ok; else check "$1" "$4"; fi; }

# ----------------------------------------------------------- the guard

live_name="$(sed -n 's/^name:[[:space:]]*//p' "$DEPLOY_DIR/docker-compose.yml" | head -1 | tr -d '"'"'"' ')"
drill_name="$(sed -n 's/^name:[[:space:]]*//p' "$DRILL_FILE" | head -1 | tr -d '"'"'"' ')"
[ "$drill_name" = "$DRILL_PROJECT" ] || { echo "REFUSED: $DRILL_FILE names project '$drill_name', not $DRILL_PROJECT." >&2; exit 1; }
if [ -z "$live_name" ] || [ "$live_name" = "$DRILL_PROJECT" ]; then
    echo "REFUSED: cannot tell the live project name apart from the drill's." >&2; exit 1
fi

# Every drill compose call goes through here: fixed project, fixed file, no
# inherited COMPOSE_FILE/COMPOSE_PROJECT_NAME, and never deploy/.env.
dc() {
    env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES -u COMPOSE_ENV_FILES \
        docker compose -p "$DRILL_PROJECT" -f "$DRILL_FILE" --project-directory "$DEPLOY_DIR/drill" "$@"
}

# ------------------------------------------------------- which backup

if [ -z "$BACKUP_DIR" ]; then
    root=/var/backups/it-suite
    if [ -r "$CONF" ]; then
        v="$(grep -E '^BACKUP_ROOT=' "$CONF" | tail -1 | cut -d= -f2- || true)"
        root="${v:-$root}"
    fi
    BACKUP_DIR="$root/latest"
fi
BACKUP_DIR="$(cd "$BACKUP_DIR" 2>/dev/null && pwd -P)" || { echo "no backup at ${BACKUP_DIR}" >&2; exit 1; }
for f in itsuite.dump keycloak.dump globals.sql alembic_version policy_count; do
    [ -s "$BACKUP_DIR/$f" ] || { echo "backup $BACKUP_DIR has no $f" >&2; exit 1; }
done

# ------------------------------------------------------------- run it

DRILL_PG_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
DRILL_APP_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
DRILL_PORTAL_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
export DRILL_PG_PASSWORD DRILL_APP_PASSWORD DRILL_PORTAL_PASSWORD

# shellcheck disable=SC2317  # invoked by the EXIT trap
teardown() {
    if [ "$KEEP" = 1 ]; then
        note "--keep: left running. Remove with:"
        note "  docker compose -p $DRILL_PROJECT -f $DRILL_FILE down -v"
        return 0
    fi
    dc down -v --remove-orphans >/dev/null 2>&1 || true
}
STARTED=1

say "Restore drill: $BACKUP_DIR -> project $DRILL_PROJECT"
note "live project '$live_name' is not touched"
dc down -v --remove-orphans >/dev/null 2>&1 || true
dc up -d --wait postgres </dev/null

say "Restoring (scripts/restore.sh)"
psql_drill() { dc exec -T postgres psql -X -A -t -v ON_ERROR_STOP=1 -U its_admin "$@" </dev/null; }
if ITS_PG_EXEC="env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME docker compose -p $DRILL_PROJECT -f $DRILL_FILE --project-directory $DEPLOY_DIR/drill exec -T postgres" \
    "$DEPLOY_DIR/scripts/restore.sh" "$BACKUP_DIR"; then
    check "restore.sh (RLS policy count, app_user narrowing)" ok
else
    check "restore.sh (RLS policy count, app_user narrowing)" "exit $?"
fi

say "Checking the restored databases"
want="$(tr -d '[:space:]' < "$BACKUP_DIR/alembic_version")"
got="$(psql_drill -d itsuite -c 'SELECT version_num FROM alembic_version' 2>/dev/null | tr -d '[:space:]' || true)"
expect "schema at $want" "$got" "$want" "restored has '${got:-nothing}'"

bypass="$(psql_drill -d itsuite -c "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname='app_user'" 2>/dev/null | tr -d '[:space:]' || true)"
expect "app_user cannot bypass RLS" "$bypass" f "got '${bypass:-nothing}'"

broken="$(psql_drill -d itsuite -c "
    WITH ordered AS (
      SELECT id, org_id, prev_hash,
             lag(hash) OVER (PARTITION BY org_id ORDER BY id) AS expected_prev
      FROM audit_events)
    SELECT count(*) FROM ordered
    WHERE prev_hash IS DISTINCT FROM coalesce(expected_prev, repeat('0', 64))" 2>/dev/null | tr -d '[:space:]' || true)"
expect "audit chain linkage intact" "$broken" 0 "${broken:-query failed} broken link(s)"

for t in organizations devices users audit_events; do
    n="$(psql_drill -d itsuite -c "SELECT count(*) FROM $t" 2>/dev/null | tr -d '[:space:]' || echo '?')"
    note "rows  $t: $n"
done

if [ "$SKIP_KC" = 0 ]; then
    say "Starting Keycloak on the restored database"
    realm="$(grep -E '^ITS_KEYCLOAK_REALM=' "$DEPLOY_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    realm="${realm:-it-suite}"
    dc up -d keycloak </dev/null
    status=""; waited=0
    while [ "$waited" -lt "$KC_WAIT_S" ]; do
        # bash's /dev/tcp inside the container: the image has no curl.
        # shellcheck disable=SC2016  # expanded inside the container
        status="$(dc exec -T -e "R=$realm" keycloak bash -c \
            'exec 3<>/dev/tcp/127.0.0.1/8080 && printf "GET /realms/$R/.well-known/openid-configuration HTTP/1.0\r\nHost: localhost\r\n\r\n" >&3 && head -1 <&3' \
            </dev/null 2>/dev/null | tr -d '\r' || true)"
        case "$status" in *" 200"*) break ;; esac
        sleep 5; waited=$((waited + 5))
    done
    case "$status" in
        *" 200"*) check "Keycloak serves realm '$realm' from the restored database" ok ;;
        *) check "Keycloak serves realm '$realm' from the restored database" "${status:-no answer} after ${waited}s" ;;
    esac
fi

objects="$(dirname "$BACKUP_DIR")/../objects"
if [ -s "$BACKUP_DIR/objects.manifest" ]; then
    note "objects: $(tr '\n' ' ' < "$BACKUP_DIR/objects.manifest")"
elif [ -d "$objects" ]; then
    note "objects mirror: $(find "$objects" -type f | wc -l) file(s) (not restored by the drill)"
fi

# --------------------------------------------------------------- result

mkdir -p "$STATE_DIR" 2>/dev/null || true
stamp="$(date -u +%FT%TZ)"
if [ ${#FAILURES[@]} -eq 0 ]; then
    say "Restore drill PASSED"
    printf '%s PASS %s\n' "$stamp" "$BACKUP_DIR" > "$STATE_DIR/last-drill" 2>/dev/null || true
    record_drill pass
    exit 0
fi
say "Restore drill FAILED: ${#FAILURES[@]} check(s)"
for f in "${FAILURES[@]}"; do note "- $f"; done
printf '%s FAIL %s\n' "$stamp" "$BACKUP_DIR" > "$STATE_DIR/last-drill" 2>/dev/null || true
record_drill fail
exit 1
