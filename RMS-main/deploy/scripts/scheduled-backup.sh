#!/usr/bin/env bash
# Nightly platform backup: backup.sh into a dated directory, retention, then
# an optional off-host copy. Run by its-platform-backup.service
# (install-backup-timer.sh); safe to run by hand as root.
#
# Settings: /etc/it-suite/backup.conf (deploy/backup/backup.conf.example).
# Pruning happens only after tonight's backup succeeded, only inside
# BACKUP_ROOT/daily, only on timestamp-named directories, and always keeps
# the newest RETAIN_MIN.
set -euo pipefail
umask 077

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$DEPLOY_DIR/.." && pwd)"
CONF="${ITS_BACKUP_CONF:-/etc/it-suite/backup.conf}"
STATE_DIR="${ITS_BACKUP_STATE_DIR:-/var/lib/it-suite/backup}"
# Results for the platform to read (mounted read-only into api and worker).
STATUS_DIR="${ITS_BACKUP_STATUS_DIR:-/var/lib/it-suite/backup-status}"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[$(date -u +%FT%TZ)] $*"; }

# ---------------------------------------------------------------- config

BACKUP_ROOT=/var/backups/it-suite
RETAIN_DAYS=14
RETAIN_MIN=3
OBJECTS=all
OFFSITE=none
OFFSITE_AGE_RECIPIENT=
OFFSITE_ALLOW_UNENCRYPTED=no
OFFSITE_OBJECTS=no
OFFSITE_RSYNC_TARGET=
OFFSITE_SSH_KEY=/etc/it-suite/backup-offsite.key
OFFSITE_S3_ENDPOINT=
OFFSITE_S3_BUCKET=
OFFSITE_S3_PREFIX=itms-platform
OFFSITE_S3_ACCESS_KEY=
OFFSITE_S3_SECRET_KEY=

# KEY=value parsing, never `source`: the file is data, not code.
load_conf() {
    local file="$1" key value
    [ -r "$file" ] || return 0
    # It can hold an S3 secret and it steers deletion: refuse a loose one.
    if [ "$(stat -c %u "$file")" != 0 ] && [ "$(id -u)" = 0 ]; then
        die "$file is not owned by root; refusing to use it."
    fi
    case "$(stat -c %a "$file")" in
        *[2367]?|*?[2367]) die "$file is group- or world-writable; chmod 600 it." ;;
    esac
    while IFS='=' read -r key value; do
        case "$key" in
            ''|\#*) continue ;;
            BACKUP_ROOT|RETAIN_DAYS|RETAIN_MIN|OBJECTS|OFFSITE|OFFSITE_AGE_RECIPIENT|\
            OFFSITE_ALLOW_UNENCRYPTED|OFFSITE_OBJECTS|OFFSITE_RSYNC_TARGET|OFFSITE_SSH_KEY|\
            OFFSITE_S3_ENDPOINT|OFFSITE_S3_BUCKET|OFFSITE_S3_PREFIX|OFFSITE_S3_ACCESS_KEY|\
            OFFSITE_S3_SECRET_KEY)
                printf -v "$key" '%s' "$value" ;;
            *) die "$file: unknown setting '$key'" ;;
        esac
    done < "$file"
}

validate() {
    case "$BACKUP_ROOT" in /?*) ;; *) die "BACKUP_ROOT must be an absolute path" ;; esac
    case "$BACKUP_ROOT" in *..*|*' '*) die "BACKUP_ROOT must not contain '..' or spaces" ;; esac
    [ "$BACKUP_ROOT" != / ] || die "BACKUP_ROOT must not be /"
    case "$BACKUP_ROOT/" in "$REPO_ROOT"/*) die "BACKUP_ROOT must be outside the repository" ;; esac
    local docker_root
    docker_root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
    case "$BACKUP_ROOT/" in "$docker_root"/*) die "BACKUP_ROOT must be outside Docker's data root" ;; esac
    [[ "$RETAIN_DAYS" =~ ^[1-9][0-9]*$ ]] || die "RETAIN_DAYS must be a whole number >= 1"
    [[ "$RETAIN_MIN" =~ ^[1-9][0-9]*$ ]] || die "RETAIN_MIN must be a whole number >= 1"
    case "$OBJECTS" in all|no-fleet-backups|none) ;; *) die "OBJECTS must be all, no-fleet-backups or none" ;; esac
    case "$OFFSITE" in none|rsync|s3) ;; *) die "OFFSITE must be none, rsync or s3" ;; esac
}

# ------------------------------------------------------------- retention

# prune_daily DIR DAYS MIN: delete timestamp-named dirs older than DAYS, but
# never the newest MIN. Prints what it removed.
prune_daily() {
    local dir="$1" days="$2" keep="$3" cutoff name i=0
    cutoff="$(date -u -d "-$days days" +%Y-%m-%dT%H%M%SZ)"
    local names=()
    while IFS= read -r name; do names+=("$name"); done < <(
        find "$dir" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
            | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z$' | sort -r
    )
    for name in "${names[@]}"; do
        i=$((i + 1))
        [ "$i" -le "$keep" ] && continue
        [[ "$name" < "$cutoff" ]] || continue
        rm -rf -- "${dir:?}/$name"
        echo "pruned $name"
    done
}

# -------------------------------------------------------------- off-host

offsite_payload() {
    # Echoes the path to ship for tonight's database backup.
    local dest="$1" ts="$2"
    if [ -n "$OFFSITE_AGE_RECIPIENT" ]; then
        command -v age >/dev/null || die "OFFSITE_AGE_RECIPIENT is set but 'age' is not installed"
        mkdir -p "$BACKUP_ROOT/outgoing"
        tar -C "$(dirname "$dest")" -cf - "$ts" | age -r "$OFFSITE_AGE_RECIPIENT" \
            > "$BACKUP_ROOT/outgoing/$ts.tar.age"
        echo "$BACKUP_ROOT/outgoing/$ts.tar.age"
    elif [ "$OFFSITE_ALLOW_UNENCRYPTED" = yes ]; then
        echo "$dest"
    else
        die "off-host copy refused: set OFFSITE_AGE_RECIPIENT, or OFFSITE_ALLOW_UNENCRYPTED=yes. The local backup succeeded."
    fi
}

offsite_rsync() {
    local payload="$1" target="${OFFSITE_RSYNC_TARGET%/}"
    [ -n "$target" ] || die "OFFSITE=rsync needs OFFSITE_RSYNC_TARGET"
    [ -r "$OFFSITE_SSH_KEY" ] || die "OFFSITE_SSH_KEY $OFFSITE_SSH_KEY is not readable"
    local ssh="ssh -i $OFFSITE_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=yes"
    rsync -a -e "$ssh" "$payload" "$target/daily/"
    if [ "$OFFSITE_OBJECTS" = yes ] && [ -d "$BACKUP_ROOT/objects" ]; then
        rsync -a -e "$ssh" "$BACKUP_ROOT/objects/" "$target/objects/"
    fi
}

offsite_s3() {
    local payload="$1" cfg
    for v in OFFSITE_S3_ENDPOINT OFFSITE_S3_BUCKET OFFSITE_S3_ACCESS_KEY OFFSITE_S3_SECRET_KEY; do
        [ -n "${!v}" ] || die "OFFSITE=s3 needs $v"
    done
    cfg="$(mktemp -d)"
    # printf is a builtin: the secret never reaches an argv.
    printf '{"version":"10","aliases":{"offsite":{"url":"%s","accessKey":"%s","secretKey":"%s","api":"s3v4","path":"auto"}}}' \
        "$OFFSITE_S3_ENDPOINT" "$OFFSITE_S3_ACCESS_KEY" "$OFFSITE_S3_SECRET_KEY" > "$cfg/config.json"
    local base="offsite/$OFFSITE_S3_BUCKET/${OFFSITE_S3_PREFIX%/}"
    local mounts=(-v "$cfg:/mc" -v "$payload:/payload/$(basename "$payload"):ro")
    local rc=0
    (
        cd "$DEPLOY_DIR"
        docker compose run --rm --no-deps -T --label traefik.enable=false "${mounts[@]}" \
            --entrypoint mc minio --config-dir /mc --quiet mirror --overwrite \
            /payload "$base/daily" </dev/null
        if [ "$OFFSITE_OBJECTS" = yes ] && [ -d "$BACKUP_ROOT/objects" ]; then
            docker compose run --rm --no-deps -T --label traefik.enable=false \
                -v "$cfg:/mc" -v "$BACKUP_ROOT/objects:/objects:ro" \
                --entrypoint mc minio --config-dir /mc --quiet mirror --overwrite \
                /objects "$base/objects" </dev/null
        fi
    ) || rc=$?
    rm -rf -- "$cfg"
    [ "$rc" = 0 ] || die "off-host S3 copy failed (exit $rc). The local backup succeeded."
}

# ---------------------------------------------------------------- status

# status_write NAME JSON: atomic and world-readable, because the containers
# read it as a non-root user. Never fails the run it reports on.
status_write() {
    local dir="$STATUS_DIR" tmp
    {
        mkdir -p "$dir" && chmod 755 "$dir" &&
            tmp="$(mktemp "$dir/.$1.XXXXXX")" &&
            printf '%s\n' "$2" > "$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$dir/$1"
    } 2>/dev/null || true
}

# The time field of a STATE_DIR file, or nothing. Timestamps only.
state_time() {
    local v=""
    [ -r "$STATE_DIR/$1" ] && v="$(cut -d' ' -f1 < "$STATE_DIR/$1" | head -1)"
    case "$v" in ''|*[!0-9TZ:-]*) v="" ;; esac
    printf '%s' "$v"
}

json_time() { if [ -n "$1" ]; then printf '"%s"' "$1"; else printf null; fi; }

CONF_OK=0; LOCKED_OUT=0; BACKUP_DONE=0; OFFSITE_DONE=0

# EXIT trap: record this run's result for the platform's backup probes.
# shellcheck disable=SC2317  # invoked by the EXIT trap
record_status() {
    [ "$LOCKED_OUT" = 1 ] && return 0
    local now first result
    now="$(date -u +%FT%TZ)"
    first="$(state_time first-run)"
    if [ -z "$first" ]; then
        first="$now"
        printf '%s\n' "$first" > "$STATE_DIR/first-run" 2>/dev/null || true
    fi
    result=failed; [ "$BACKUP_DONE" = 1 ] && result=ok
    status_write backup.json "{\"result\":\"$result\",\"at\":\"$now\",\"last_success\":$(json_time "$(state_time last-success)"),\"first_run\":\"$first\"}"
    [ "$CONF_OK" = 1 ] || return 0
    if [ "$OFFSITE" = none ]; then
        status_write offsite.json '{"configured":false,"mode":"none"}'
    elif [ "$BACKUP_DONE" = 1 ]; then
        result=failed; [ "$OFFSITE_DONE" = 1 ] && result=ok
        status_write offsite.json "{\"configured\":true,\"mode\":\"$OFFSITE\",\"result\":\"$result\",\"at\":\"$now\",\"last_success\":$(json_time "$(state_time last-offsite)")}"
    fi
    return 0
}

# ------------------------------------------------------------------ main

main() {
    trap record_status EXIT
    load_conf "$CONF"
    validate
    CONF_OK=1
    mkdir -p "$BACKUP_ROOT/daily" "$STATE_DIR"
    chmod 700 "$BACKUP_ROOT" "$STATE_DIR"

    exec 9> "$STATE_DIR/lock"
    flock -n 9 || { LOCKED_OUT=1; die "another platform backup is running"; }

    local ts dest payload
    ts="$(date -u +%Y-%m-%dT%H%M%SZ)"
    dest="$BACKUP_ROOT/daily/$ts"
    log "backup -> $dest (objects: $OBJECTS)"
    ITS_BACKUP_OBJECTS="$OBJECTS" ITS_BACKUP_OBJECTS_DIR="$BACKUP_ROOT/objects" \
        "$DEPLOY_DIR/scripts/backup.sh" "$dest"
    ln -sfn "daily/$ts" "$BACKUP_ROOT/latest"
    printf '%s %s\n' "$(date -u +%FT%TZ)" "$dest" > "$STATE_DIR/last-success"
    BACKUP_DONE=1

    log "retention: $RETAIN_DAYS days, at least $RETAIN_MIN kept"
    prune_daily "$BACKUP_ROOT/daily" "$RETAIN_DAYS" "$RETAIN_MIN"
    [ -d "$BACKUP_ROOT/outgoing" ] && prune_daily_files "$BACKUP_ROOT/outgoing" "$RETAIN_MIN"

    if [ "$OFFSITE" != none ]; then
        log "off-host copy ($OFFSITE)"
        payload="$(offsite_payload "$dest" "$ts")"
        case "$OFFSITE" in
            rsync) offsite_rsync "$payload" ;;
            s3) offsite_s3 "$payload" ;;
        esac
        printf '%s %s %s\n' "$(date -u +%FT%TZ)" "$OFFSITE" "$(basename "$payload")" > "$STATE_DIR/last-offsite"
        OFFSITE_DONE=1
    fi
    log "done"
}

# Encrypted payloads are only a staging area once shipped: keep the newest few.
prune_daily_files() {
    local dir="$1" keep="$2" i=0 f
    while IFS= read -r f; do
        i=$((i + 1))
        [ "$i" -le "$keep" ] || rm -f -- "${dir:?}/$f"
    done < <(find "$dir" -maxdepth 1 -type f -name '*.tar.age' -printf '%f\n' | sort -r)
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
