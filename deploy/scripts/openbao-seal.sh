#!/usr/bin/env bash
# Optional OpenBao auto-unseal with a static key. OFF by default.
#
#   ./scripts/openbao-seal.sh status
#   ./scripts/openbao-seal.sh enable  [--key-file PATH] [--yes]   Shamir -> static
#   ./scripts/openbao-seal.sh disable [--yes]                      static -> Shamir
#
# THE TRADE-OFF (deploy/GO-LIVE.md §4 has the full text). Shamir: a stolen disk
# or backup of openbao-data is useless without 3 of 5 key shares, and every
# reboot leaves job signing and the vault dead until a person enters them.
# Static key: OpenBao unseals itself after a power cut, and anyone who gets the
# key file AND openbao-data gets the job-signing key, the CA and every vault
# key. Keep the key file on a separate mount, never in a backup that also
# carries openbao-data, and back it up off-host: if it is lost, OpenBao cannot
# be unsealed at all -- the old shares become recovery keys and cannot unseal.
#
# Unseal shares are read with a hidden prompt (or one per line from stdin when
# stdin is not a terminal) and never appear on any command line.
set -euo pipefail

DEPLOY_DIR="${ITS_DEPLOY_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$DEPLOY_DIR"
REPO_ROOT="$(cd .. && pwd)"
COMPOSE=(docker compose)
OVERRIDE_FILES="docker-compose.yml:openbao/auto-unseal.yml"
KEY_FILE="/etc/it-suite/openbao-unseal/unseal.key"
SNAP_ROOT="${ITS_SEAL_SNAPSHOT_DIR:-/var/backups/it-suite/openbao-seal}"
WAIT_S="${ITS_SEAL_WAIT_S:-90}"
ASSUME_YES=0

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '  \033[33mWARNING\033[0m  %s\n' "$*" >&2; }
die()  { printf '\n\033[31mFAILED\033[0m  %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ .env

env_get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }
env_unset() { sed -i "/^$1=/d" .env; }
env_set() { env_unset "$1"; printf '%s=%s\n' "$1" "$2" >> .env; }
auto_unseal_configured() { [ "$(env_get COMPOSE_FILE)" = "$OVERRIDE_FILES" ]; }

# --------------------------------------------------------------- OpenBao

# Every compose call that must not eat the shares waiting on stdin.
dc() { "${COMPOSE[@]}" "$@" </dev/null; }

# Prints the JSON status; exit 0 unsealed, 2 sealed, other = unreachable.
bao_status_json() { dc exec -T openbao bao status -format=json 2>/dev/null; }

# seal_state -> "unsealed <type>" | "sealed <type>" | "down"
seal_state() {
    local out rc=0 type sealed
    out="$(bao_status_json)" || rc=$?
    if [ "$rc" != 0 ] && [ "$rc" != 2 ]; then echo down; return 0; fi
    type="$(grep -oE '"type": *"[a-z-]+"' <<<"$out" | head -1 | grep -oE '"[a-z-]+"$' | tr -d '"')"
    sealed="$(grep -oE '"sealed": *(true|false)' <<<"$out" | head -1 | grep -oE '(true|false)$')"
    if [ "$sealed" = false ]; then echo "unsealed ${type:-unknown}"; else echo "sealed ${type:-unknown}"; fi
}

wait_until_up() {
    local i state
    for ((i = 0; i < WAIT_S; i += 3)); do
        state="$(seal_state)"
        [ "$state" != down ] && { echo "$state"; return 0; }
        sleep 3
    done
    echo down
}

# unseal_loop [-migrate]: ask for shares until OpenBao reports unsealed.
unseal_loop() {
    local flag="${1:-}" tries=0 share state
    while :; do
        state="$(seal_state)"
        case "$state" in unsealed*) return 0 ;; down) die "OpenBao stopped answering during unseal." ;; esac
        tries=$((tries + 1))
        [ "$tries" -le 6 ] || die "still sealed after 6 shares -- a share was wrong or from another install."
        if [ -t 0 ]; then
            # Interactive: bao prompts with hidden input inside the container.
            # shellcheck disable=SC2086  # $flag is empty or one word
            "${COMPOSE[@]}" exec openbao bao operator unseal $flag || true
        else
            # `bao operator unseal` cannot read a pipe; the generic write takes
            # a JSON body on stdin, so the share stays off every argv.
            IFS= read -r share || die "stdin ended before OpenBao unsealed."
            local migrate=false
            [ "$flag" = -migrate ] && migrate=true
            printf '{"key":"%s","migrate":%s}' "$share" "$migrate" \
                | "${COMPOSE[@]}" exec -T openbao bao write -format=json sys/unseal - >/dev/null || true
        fi
    done
}

confirm() {
    [ "$ASSUME_YES" = 1 ] && return 0
    printf '\n%s [y/N] ' "$1"
    local reply; read -r reply
    [[ "$reply" =~ ^[Yy]$ ]] || die "aborted; nothing after this point was changed."
}

# ------------------------------------------------------------- key file

# Refuse a key path that a volume backup, the repo or the backup root carries.
check_key_path() {
    local path="$1" docker_root backup_root
    case "$path" in /*) ;; *) die "--key-file must be an absolute path: $path" ;; esac
    case "$path" in *..*) die "--key-file must not contain '..': $path" ;; esac
    case "$path/" in "$REPO_ROOT"/*) die "the key file must be outside the repository ($REPO_ROOT)." ;; esac
    docker_root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
    case "$path/" in "$docker_root"/*) die "the key file must be outside Docker's data root ($docker_root): that is where the Postgres, MinIO and OpenBao volumes live." ;; esac
    backup_root="/var/backups/it-suite"
    if [ -r /etc/it-suite/backup.conf ]; then
        backup_root="$(grep -E '^BACKUP_ROOT=' /etc/it-suite/backup.conf | tail -1 | cut -d= -f2- || true)"
        backup_root="${backup_root:-/var/backups/it-suite}"
    fi
    case "$path/" in "$backup_root"/*) die "the key file must be outside the platform backup root ($backup_root)." ;; esac
}

ensure_key_file() {
    local path="$1" uid gid size
    uid="$(dc exec -T openbao id -u openbao | tr -d '\r')"
    gid="$(dc exec -T openbao id -g openbao | tr -d '\r')"
    if [ -e "$path" ]; then
        size="$(stat -c %s "$path")"
        [ "$size" = 32 ] || die "$path exists and is $size bytes, not 32. Move it aside; it is not a key this script made."
        note "reusing the existing 32-byte key at $path"
    else
        mkdir -p "$(dirname "$path")" || die "cannot create $(dirname "$path") -- run with sudo."
        chmod 700 "$(dirname "$path")"
        (umask 077 && head -c 32 /dev/urandom > "$path.tmp") || die "cannot write $path -- run with sudo."
        mv "$path.tmp" "$path"
        note "generated a new 32-byte key at $path"
    fi
    chown "$uid:$gid" "$path" 2>/dev/null || die "cannot chown $path to the OpenBao user ($uid:$gid) -- run with sudo."
    chmod 400 "$path"
}

snapshot_openbao() {
    local dir
    dir="$SNAP_ROOT/pre-$1-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$dir" && chmod 700 "$dir"
    note "stopping OpenBao for a consistent snapshot (job signing pauses)"
    dc stop openbao >/dev/null
    dc run --rm --no-deps -T -u 0:0 -v "$dir:/snap" --entrypoint tar openbao \
        czf /snap/openbao-data.tgz -C /openbao/file .
    [ -s "$dir/openbao-data.tgz" ] || die "snapshot at $dir is empty; nothing was changed."
    chmod 600 "$dir/openbao-data.tgz"
    note "snapshot: $dir/openbao-data.tgz"
    SNAP_DIR="$dir"
}

recover_help() {
    warn "Stopped part-way. OpenBao data snapshot: ${SNAP_DIR:-none taken}."
    warn "To return to plain Shamir unsealing if the migration did NOT complete:"
    warn "  cd $DEPLOY_DIR && sed -i '/^COMPOSE_FILE=/d;/^OPENBAO_UNSEAL_KEY_FILE=/d;/^OPENBAO_SEAL_STANZA=/d' .env"
    warn "  docker compose up -d --no-deps openbao && docker compose exec openbao bao operator unseal   (x3)"
    warn "If 'bao status' already shows Seal Type static, run ./scripts/openbao-seal.sh disable instead."
}

# ------------------------------------------------------------- commands

cmd_status() {
    local state
    state="$(seal_state)"
    say "OpenBao seal"
    note "running state:      $state"
    if auto_unseal_configured; then
        note "auto-unseal:        ON  (.env COMPOSE_FILE=$OVERRIDE_FILES)"
        local key; key="$(env_get OPENBAO_UNSEAL_KEY_FILE)"
        note "key file:           ${key:-UNSET}"
        if [ -n "$key" ] && [ -e "$key" ]; then
            note "key file present:   yes, $(stat -c '%s bytes, mode %a, owner %u:%g' "$key")"
        else
            warn "the key file is missing: OpenBao cannot start or unseal."
        fi
    else
        note "auto-unseal:        OFF (Shamir; 3 of 5 shares after every restart)"
        [ -n "$(env_get COMPOSE_FILE)" ] && note "COMPOSE_FILE in .env: $(env_get COMPOSE_FILE)"
    fi
    return 0
}

cmd_enable() {
    [ -f .env ] || die "deploy/.env is missing."
    check_key_path "$KEY_FILE"
    local cf state
    cf="$(env_get COMPOSE_FILE)"
    state="$(seal_state)"
    if auto_unseal_configured && [ "$state" = "unsealed static" ]; then
        say "Already enabled"; cmd_status; return 0
    fi
    [ -z "$cf" ] || [ "$cf" = docker-compose.yml ] \
        || die ".env already sets COMPOSE_FILE=$cf. Merge by hand; this script will not overwrite it."
    case "$state" in
        "unsealed shamir") ;;
        sealed*) die "OpenBao is sealed. Unseal it first (docker compose exec openbao bao operator unseal, x3) -- the migration needs the same shares." ;;
        down) die "OpenBao is not running. docker compose up -d openbao, unseal it, then re-run." ;;
        *) die "unexpected OpenBao state '$state'; expected 'unsealed shamir'." ;;
    esac

    say "Enable OpenBao auto-unseal (static key)"
    note "key file:  $KEY_FILE"
    note "snapshot:  $SNAP_ROOT"
    note "You need 3 of the 5 unseal key shares. They become RECOVERY keys afterwards."
    confirm "Proceed? OpenBao will be stopped briefly and restarted twice."

    ensure_key_file "$KEY_FILE"
    SNAP_DIR=""
    trap 'rc=$?; [ "$rc" = 0 ] || recover_help' EXIT
    snapshot_openbao enable

    say "Switching the seal configuration"
    env_set COMPOSE_FILE "$OVERRIDE_FILES"
    env_set OPENBAO_UNSEAL_KEY_FILE "$KEY_FILE"
    env_unset OPENBAO_SEAL_STANZA
    dc up -d --no-deps openbao >/dev/null
    state="$(wait_until_up)"
    [ "$state" != down ] || die "OpenBao did not come back: docker compose logs openbao"

    say "Migrating: enter unseal key shares (hidden)"
    unseal_loop -migrate
    state="$(seal_state)"
    [ "$state" = "unsealed static" ] || die "after migration OpenBao reports '$state', not 'unsealed static'."

    say "Proving it: restarting OpenBao with no shares"
    dc restart openbao >/dev/null
    state="$(wait_until_up)"
    local i=0
    while [ "$state" != "unsealed static" ] && [ "$i" -lt "$WAIT_S" ]; do
        sleep 3; i=$((i + 3)); state="$(seal_state)"
    done
    [ "$state" = "unsealed static" ] || die "OpenBao came back '$state' after a restart; auto-unseal is not working."
    trap - EXIT

    say "Auto-unseal is ON"
    note "OpenBao restarted and unsealed itself."
    note "Now, before anything else:"
    note "  1. Copy $KEY_FILE OFF this host, stored apart from any openbao-data backup."
    note "  2. Keep the 5 shares: they are recovery keys now (disable, generate-root), not unseal keys."
    note "  3. The snapshot $SNAP_DIR is Shamir-sealed; delete it once you are satisfied."
}

cmd_disable() {
    [ -f .env ] || die "deploy/.env is missing."
    local state key
    state="$(seal_state)"
    auto_unseal_configured || die "auto-unseal is not configured in .env; nothing to disable."
    [ "$state" = "unsealed static" ] || die "expected 'unsealed static', OpenBao reports '$state'."
    key="$(env_get OPENBAO_UNSEAL_KEY_FILE)"

    say "Disable auto-unseal (back to Shamir)"
    note "You need 3 of the 5 RECOVERY keys (the old unseal shares) twice:"
    note "once to migrate, once to unseal after the final restart."
    confirm "Proceed?"
    SNAP_DIR=""
    snapshot_openbao disable

    env_set OPENBAO_SEAL_STANZA static-disabled
    dc up -d --no-deps openbao >/dev/null
    state="$(wait_until_up)"
    [ "$state" != down ] || die "OpenBao did not come back: docker compose logs openbao"
    say "Migrating: enter recovery keys (hidden)"
    unseal_loop -migrate
    state="$(seal_state)"
    [ "$state" = "unsealed shamir" ] || die "after migration OpenBao reports '$state', not 'unsealed shamir'."

    env_unset COMPOSE_FILE
    env_unset OPENBAO_UNSEAL_KEY_FILE
    env_unset OPENBAO_SEAL_STANZA
    dc up -d --no-deps openbao >/dev/null
    state="$(wait_until_up)"
    [ "$state" != down ] || die "OpenBao did not come back: docker compose logs openbao"
    say "Unseal with the same shares (hidden)"
    unseal_loop
    say "Auto-unseal is OFF"
    note "OpenBao is Shamir-sealed again. The key file $key is no longer used;"
    note "destroy it when you are satisfied: shred -u $key"
}

# ------------------------------------------------------------------ main

[ $# -ge 1 ] || { sed -n '2,6p' "$0"; exit 2; }
CMD="$1"; shift
while [ $# -gt 0 ]; do
    case "$1" in
        --key-file) KEY_FILE="${2:?--key-file needs a path}"; shift 2 ;;
        --yes) ASSUME_YES=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done
command -v docker >/dev/null || die "docker is not on PATH"
case "$CMD" in
    status) cmd_status ;;
    enable) cmd_enable ;;
    disable) cmd_disable ;;
    -h|--help) sed -n '2,17p' "$0" ;;
    *) die "unknown command: $CMD (status | enable | disable)" ;;
esac
