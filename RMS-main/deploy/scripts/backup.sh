#!/usr/bin/env bash
# Take a complete, restorable backup of the platform's databases.
#
# Three artefacts, not two. `deploy/README.md` and `GO-LIVE.md` previously
# prescribed only the two `-Fc` dumps:
#
#   itsuite.dump    the application database
#   keycloak.dump   the realm, users and federation config -- losing this
#                   locks every operator out even with an intact itsuite
#   globals.sql     the cluster's ROLE definitions
#
# WHAT globals.sql IS AND IS NOT FOR. A `pg_dump` of a single database emits
# ZERO CREATE ROLE statements while depending on three cluster-level roles it
# cannot create. Restoring into a cluster that lacks them is `pg_restore`
# exit 1 with ~266 "role ... does not exist" errors -- measured, from a
# realistic source (schema at head, narrowed by grant-app-privileges.sql):
#     130 role "app_user" does not exist   (an independent re-run
#     128 role "its_admin" does not exist   counted 131/128/8, so
#       8 role "portal_user" does not exist  treat these as ~figures)
# The tables and their RLS still land (125 tables, 122 FORCE RLS survived),
# so the failure looks partial rather than total.
#
# It is NOT needed for this platform's normal recovery path, and the earlier
# revision of the rollback spec was wrong to say so: `docker compose up
# postgres` creates `its_admin` as POSTGRES_USER and runs init-app-user.sh,
# which creates the other two, so a compose-built host already has all
# three and restores cleanly (exit 0, 0 errors -- also measured). globals.sql
# is what makes the backup restorable into a target this compose stack did
# NOT build: a bare cluster, or a managed Postgres. Cheap insurance, taken
# deliberately, not a fix for a broken path.
#
# Usage:
#   ./backup.sh [OUTPUT_DIR]        # default: ./backups/<UTC timestamp>
#
# Runs the client tools inside the postgres container, because the compose
# stack publishes no Postgres port. Override that for testing or for a host
# where the tools are local:
#   ITS_PG_EXEC="" PGHOST=127.0.0.1 PGUSER=its_admin ./backup.sh /tmp/bk
#
# OBJECT STORAGE (MinIO) is NOT in this backup unless asked for. One bucket
# holds everything; fleet backup data lives under `<org>/backup/<repo>/`.
#   ITS_BACKUP_OBJECTS=none               default: databases only (deploy.sh)
#   ITS_BACKUP_OBJECTS=all                mirror the whole bucket
#   ITS_BACKUP_OBJECTS=no-fleet-backups   mirror all but `*/backup/*` -- for
#                                         when the off-site endpoint-backup
#                                         copy protects fleet data instead
#   ITS_BACKUP_OBJECTS_DIR=PATH           where (default OUTPUT_DIR/objects);
#                                         incremental, never deletes
set -euo pipefail

# BEFORE anything is written. `chmod 600` at the end is too late: globals.sql
# carries SCRAM password hashes, and a backup that fails on a later artefact
# would leave them at the default mode. Belt and braces with the explicit
# chmod below.
umask 077

: "${ITS_PG_EXEC=docker compose exec -T postgres}"
: "${ITS_PG_ADMIN_USER:=its_admin}"
# The role `pg_dumpall --roles-only` connects as. It must be able to read
# pg_authid, i.e. be a superuser. On a compose host `its_admin` IS the
# container's POSTGRES_USER and therefore superuser, so the default is right
# there; against a cluster where it is not, this fails on the FIRST artefact
# with `permission denied for table pg_authid` rather than producing a
# half-backup, and you set this to a role that can.
: "${ITS_PG_SUPERUSER:=$ITS_PG_ADMIN_USER}"
: "${ITS_BACKUP_OBJECTS:=none}"
case "$ITS_BACKUP_OBJECTS" in
    none|all|no-fleet-backups) ;;
    *) echo "ERROR: ITS_BACKUP_OBJECTS must be none, all or no-fleet-backups" >&2; exit 1 ;;
esac

# Resolve before any `cd`: the caller's path is relative to where they stand.
OUT_DIR="${1:-./backups/$(date -u +%Y-%m-%dT%H%M%SZ)}"
case "$OUT_DIR" in
    /*) ;;
    *) OUT_DIR="$PWD/$OUT_DIR" ;;
esac
cd "$(dirname "$0")/.."   # deploy/, so `docker compose` finds its file
mkdir -p "$OUT_DIR"

# shellcheck disable=SC2086  # ITS_PG_EXEC is a command prefix, intentionally split
run() { ${ITS_PG_EXEC} "$@"; }

echo "==> backing up to $OUT_DIR"

echo "--> globals (roles)"
run pg_dumpall --roles-only -U "$ITS_PG_SUPERUSER" > "$OUT_DIR/globals.sql"

echo "--> itsuite"
run pg_dump -Fc -U "$ITS_PG_ADMIN_USER" itsuite > "$OUT_DIR/itsuite.dump"

echo "--> keycloak"
run pg_dump -Fc -U "$ITS_PG_ADMIN_USER" keycloak > "$OUT_DIR/keycloak.dump"

# The schema revision this backup holds, in plain text beside the dumps.
# WITHOUT THIS THE ROLLBACK DECISION IS UNANSWERABLE: the runbook's first
# question is "did the bad deploy move the schema", which means comparing the
# live `alembic_version` against the one the backup carries -- and nothing
# else records the latter. Reading it out of a -Fc dump at 3am is not a
# procedure. Also record the image tag that was running, for the same reason.
run psql -X -A -t -U "$ITS_PG_ADMIN_USER" -d itsuite \
    -c "SELECT version_num FROM alembic_version" | tr -d '[:space:]' \
    > "$OUT_DIR/alembic_version"
echo >> "$OUT_DIR/alembic_version"

# The RLS policy count, so the restore can assert EQUALITY rather than "not
# zero". A restore does not lose row-level security wholesale -- it loses the
# ROLE-SCOPED policies, because `CREATE POLICY ... TO <role>` fails when the
# role is absent. Measured on a role-less restore: 122 of 133 policies
# landed, and every one of the 11 missing was an isolation policy naming
# `portal_user` or `app_user`. A floor check of "greater than zero" passes
# that state cheerfully, which is precisely the tenant-isolation hole the
# check exists to catch.
run psql -X -A -t -U "$ITS_PG_ADMIN_USER" -d itsuite \
    -c "SELECT count(*) FROM pg_policies WHERE schemaname='public'" \
    | tr -d '[:space:]' > "$OUT_DIR/policy_count"
echo >> "$OUT_DIR/policy_count"
if [ -r .env ] && grep -q '^IT_SUITE_VERSION=' .env; then
    grep '^IT_SUITE_VERSION=' .env | tail -1 > "$OUT_DIR/image_version"
else
    echo "IT_SUITE_VERSION=(unset -- deployment predates versioned images)" \
        > "$OUT_DIR/image_version"
fi

# FAIL LOUDLY ON AN EMPTY ARTEFACT. `pg_dump`'s exit status travels through
# the pipeline, but a `docker compose exec` that never reached the database
# can still leave a 0-byte file behind, and a backup that is silently empty
# is worse than no backup: it is a backup you will trust at 3am.
fail=0
for f in globals.sql itsuite.dump keycloak.dump alembic_version policy_count image_version; do
    if [ ! -s "$OUT_DIR/$f" ]; then
        echo "ERROR: $OUT_DIR/$f is missing or empty" >&2
        fail=1
    fi
done

# The globals dump must actually contain the three roles the dumps depend on.
# An empty-but-nonzero globals.sql (a cluster where the roles were never
# created) would restore into the same role-less failure it exists to
# prevent.
for role in its_admin app_user portal_user; do
    if ! grep -q "^CREATE ROLE $role\b" "$OUT_DIR/globals.sql"; then
        echo "ERROR: globals.sql has no CREATE ROLE for '$role'" >&2
        fail=1
    fi
done
[ "$fail" -eq 0 ] || exit 1

# globals.sql CARRIES THE ROLE PASSWORD HASHES (SCRAM-SHA-256) for
# its_admin, app_user and portal_user. docs/07-backup-restore.md says of it:
# "It is a secret. Encrypt it, or skip it entirely." This script makes it
# MANDATORY, so it must not also make it world-readable: an artefact that
# went from optional-and-flagged to mandatory-and-unflagged is a downgrade.
chmod 600 "$OUT_DIR/globals.sql"
chmod 700 "$OUT_DIR" 2>/dev/null || true

# ------------------------------------------------------------ object storage
#
# `mc mirror` from a one-off container of the pinned minio image, over the
# compose network: no downtime, credentials from the service's own
# environment (never argv), and traefik told to ignore the one-off.
case "$ITS_BACKUP_OBJECTS" in
    none) ;;
    all|no-fleet-backups)
        OBJ_DIR="${ITS_BACKUP_OBJECTS_DIR:-$OUT_DIR/objects}"
        mkdir -p "$OBJ_DIR"
        bucket="$(grep -E '^ITS_S3_BUCKET=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
        bucket="${bucket:-it-suite}"
        [[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,62}$ ]] || { echo "ERROR: bad ITS_S3_BUCKET '$bucket'" >&2; exit 1; }
        exclude=""
        [ "$ITS_BACKUP_OBJECTS" = no-fleet-backups ] && exclude="*/backup/*"
        echo "--> objects ($ITS_BACKUP_OBJECTS) -> $OBJ_DIR"
        # shellcheck disable=SC2016  # expanded by the container's shell
        docker compose run --rm --no-deps -T --label traefik.enable=false \
            -v "$OBJ_DIR:/out" -e "ITS_MIRROR_BUCKET=$bucket" -e "ITS_MIRROR_EXCLUDE=$exclude" \
            --entrypoint /bin/sh minio -c '
                set -e
                mkdir -p /tmp/mc && chmod 700 /tmp/mc
                printf "{\"version\":\"10\",\"aliases\":{\"src\":{\"url\":\"http://minio:9000\",\"accessKey\":\"%s\",\"secretKey\":\"%s\",\"api\":\"s3v4\",\"path\":\"auto\"}}}" \
                    "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" > /tmp/mc/config.json
                if [ -n "$ITS_MIRROR_EXCLUDE" ]; then
                    mc --config-dir /tmp/mc --quiet mirror --overwrite --exclude "$ITS_MIRROR_EXCLUDE" \
                        "src/$ITS_MIRROR_BUCKET" "/out/$ITS_MIRROR_BUCKET"
                else
                    mc --config-dir /tmp/mc --quiet mirror --overwrite \
                        "src/$ITS_MIRROR_BUCKET" "/out/$ITS_MIRROR_BUCKET"
                fi
                mc --config-dir /tmp/mc ls --recursive "src/$ITS_MIRROR_BUCKET" | wc -l > /out/.source-count
            ' </dev/null
        mirrored="$(find "$OBJ_DIR/$bucket" -type f 2>/dev/null | wc -l)"
        source_count="$(tr -d '[:space:]' < "$OBJ_DIR/.source-count" 2>/dev/null || echo '?')"
        printf 'mode=%s\nbucket=%s\nsource_objects=%s\nmirrored_files=%s\n' \
            "$ITS_BACKUP_OBJECTS" "$bucket" "$source_count" "$mirrored" > "$OUT_DIR/objects.manifest"
        echo "    $mirrored file(s) in the mirror; the bucket lists $source_count object(s)"
        if [ "$ITS_BACKUP_OBJECTS" = all ] && [ "$source_count" != "?" ] && [ "$mirrored" -lt "$source_count" ]; then
            echo "ERROR: the mirror holds fewer files than the bucket lists." >&2
            exit 1
        fi
        ;;
    *) echo "ERROR: ITS_BACKUP_OBJECTS must be none, all or no-fleet-backups" >&2; exit 1 ;;
esac

ls -l "$OUT_DIR"
cat <<EOF
==> OK.

    globals.sql holds the role PASSWORD HASHES -- it is a secret (see
    docs/07-backup-restore.md section 2). It is mode 600 here; keep it
    encrypted at rest and off shared storage.

    Restore with, from deploy/:
        ./scripts/restore.sh $OUT_DIR
EOF
