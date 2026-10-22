#!/usr/bin/env bash
# Restore the DATABASES from a backup taken by ./backup.sh.
#
# SCOPE, and read this before using it for disaster recovery. This script
# restores Postgres and nothing else. It does NOT restore key material --
# openbao-data, the internal CA, the gateway TLS material, minio-data -- and a
# control plane with a restored database and no key material cannot sign a
# job, verify an agent, or decrypt a credential.
#
# `docs/07-backup-restore.md` section 3 is the authoritative FULL recovery
# procedure and it is ordered deliberately: key-material volumes FIRST, then
# a fresh Postgres, then the databases, then the narrowing, then unseal
# OpenBao. This script is the database half of that, packaged so a ROLLBACK
# on a live host -- where key material is untouched and only the schema moved
# -- is one command with its end state asserted. For a rebuild from nothing,
# follow docs/07 section 3 and use this only in place of its step 3+4.
#
# One deliberate difference from docs/07's step 3, since both now exist:
# docs/07 uses `pg_restore --clean --if-exists` INTO the existing database,
# which drops objects individually and leaves the database (and its standing
# ALTER DEFAULT PRIVILEGES) in place. This script refuses a populated target
# unless ITS_RESTORE_FORCE=1, and then drops the DATABASE. Both end at the
# same place because both then re-apply grant-app-privileges.sql -- see below
# for why that step is not optional either way.
#
# THE ORDER IS THE PROCEDURE.
#
#   1. roles         -- only if the target lacks them (a compose-built host
#                       already has all three; see backup.sh's header)
#   2. CREATE DATABASE ... OWNER its_admin
#   3. pg_restore
#   4. grant-app-privileges.sql        <-- NOT OPTIONAL, NOT TIDY-UP
#
# WHY STEP 4 IS LOAD-BEARING. Two earlier versions of this comment were
# wrong in opposite directions; the corrections are in the spec's sections
# 1.2 and 1.3. This is what `docs/07-backup-restore.md` section 3 ("Step 4 is
# not optional, and here is the proof") already said before any of it, and
# that document is authoritative.
#
# `init-app-user.sh` installs a standing rule on the itsuite database:
#     ALTER DEFAULT PRIVILEGES FOR ROLE its_admin IN SCHEMA public
#         GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
# Tables that pg_restore creates there are created BY its_admin, so the rule
# fires and grants blanket rights. pg_dump records each table's ACL as GRANT
# statements and cannot express "and revoke what the default privileges would
# have added", so the dump's narrower GRANT SELECT is ADDITIVE and the result
# is the union. Measured, restoring a correctly narrowed source into a target
# carrying that rule, before this step runs:
#
#     audit_events:     DELETE,INSERT,SELECT,UPDATE
#     permissions:      DELETE,INSERT,SELECT,UPDATE
#     role_permissions: DELETE,INSERT,SELECT,UPDATE
#     security_events:  DELETE,INSERT,SELECT,UPDATE
#
# `permissions` and `role_permissions` have NO trigger backstop, so in that
# state app_user can rewrite the RBAC catalogue (invariant 6) and nothing in
# the application complains. `audit_events` is backstopped by
# `audit_events_immutable` (a DELETE fails "audit_events is append-only");
# the UPDATE path is untested, not proven.
#
# DO NOT WEAKEN THIS TO "free and idempotent". A measurement saying the
# restore comes back narrow was taken against a database the test had just
# created, where pg_default_acl is empty -- which is also why the
# ITS_RESTORE_FORCE path measures narrow, since dropping the database
# destroys the rule. Neither is the database a first-boot restore lands in.
#
# Usage:
#   ./restore.sh BACKUP_DIR
#
# Override the container hop for testing or a host with local client tools:
#   ITS_PG_EXEC="" PGHOST=127.0.0.1 PGUSER=its_admin ./restore.sh /tmp/bk
set -euo pipefail

: "${ITS_PG_EXEC=docker compose exec -T postgres}"
: "${ITS_PG_ADMIN_USER:=its_admin}"

# RESOLVE THE BACKUP PATH BEFORE MOVING, THEN ANCHOR TO deploy/.
#
# This ordering is load-bearing and was got wrong once. The default grants
# path used to be `../postgres/grant-app-privileges.sql`, resolved against
# the CALLER's directory -- which is `deploy/scripts/` only if you happen to
# have cd'd there. Every documented invocation runs `./scripts/restore.sh`
# from `deploy/`, where `../postgres/` is the repository root and does not
# exist. The script then died at step 4 with a bash redirection error --
# AFTER dropping and restoring the database, so the narrowing and its
# verification never ran and the operator's only signal was "No such file or
# directory". Anchoring to the script's own location (as build-images.sh
# does) makes the path independent of where it is invoked from.
#
# BACKUP_DIR is resolved first because it is usually relative to where the
# operator is standing, and `cd` would silently repoint it.
BACKUP_DIR="${1:?usage: restore.sh BACKUP_DIR}"
case "$BACKUP_DIR" in
    /*) ;;
    *) BACKUP_DIR="$PWD/$BACKUP_DIR" ;;
esac
cd "$(dirname "$0")/.."   # deploy/
: "${ITS_GRANTS_SQL:=postgres/grant-app-privileges.sql}"

[ -r "$ITS_GRANTS_SQL" ] || {
    echo "ERROR: grants file '$ITS_GRANTS_SQL' is not readable (cwd: $PWD)." >&2
    echo "Refusing to start: this script would otherwise restore the database" >&2
    echo "and only then fail to re-narrow app_user." >&2
    exit 1
}

for f in globals.sql itsuite.dump; do
    [ -s "$BACKUP_DIR/$f" ] || {
        echo "ERROR: $BACKUP_DIR/$f is missing or empty." >&2
        echo "Refusing to start: a restore that half-succeeds is harder to" >&2
        echo "diagnose than one that never began." >&2
        exit 1
    }
done

# shellcheck disable=SC2086  # command prefix, intentionally split
run() { ${ITS_PG_EXEC} "$@"; }
psql_admin() { run psql -v ON_ERROR_STOP=1 -U "$ITS_PG_ADMIN_USER" "$@"; }

echo "==> 1. roles"
# A TRULY BARE CLUSTER CANNOT GET PAST THIS LINE, and that is a known limit
# rather than a bug to discover at 3am: the query below connects AS
# `its_admin`, so if that role does not exist there is no connection to make.
# The globals branch therefore covers "the admin role exists, one or both of
# the others do not" -- which is the realistic partial case -- and not
# "nothing exists at all". For a genuinely bare target, apply globals.sql by
# hand as a superuser first:
#     psql -U postgres -d postgres -f <backup>/globals.sql
# then re-run this script. Say so plainly rather than failing obscurely.
if ! run psql -X -A -t -U "$ITS_PG_ADMIN_USER" -d postgres -c "SELECT 1" >/dev/null 2>&1; then
    echo "ERROR: cannot connect as '$ITS_PG_ADMIN_USER'." >&2
    echo "If this is a bare cluster, apply the roles first, as a superuser:" >&2
    echo "    psql -d postgres -f $BACKUP_DIR/globals.sql" >&2
    echo "then re-run this script." >&2
    exit 1
fi
# ONLY THE MISSING ROLES, AND ONLY THEIR OWN LINES. Applying globals.sql
# wholesale would be a live-credential clobber: `pg_dumpall --roles-only`
# emits `ALTER ROLE <name> WITH ... PASSWORD 'SCRAM-SHA-256$...'` for EVERY
# role, and applied without ON_ERROR_STOP the CREATE fails harmlessly while
# the ALTER succeeds -- silently resetting existing roles' passwords to the
# backup-era hashes. If `.env`'s passwords were rotated since the backup,
# this script would print "==> OK" (its own checks run as its_admin, which
# still works) and then api/gateway/worker would fail authentication with
# nothing pointing at the cause.
#
# docs/07-backup-restore.md section 3 states the principle: the roles come
# from `.env` via init-app-user.sh, which "keeps the passwords in .env
# authoritative". So: a role that does not exist is created from the backup;
# a role that does exist is not touched at all.
absent=$(run psql -X -A -t -U "$ITS_PG_ADMIN_USER" -d postgres -c \
    "SELECT string_agg(r.name, ' ') FROM (VALUES ('its_admin'),('app_user'),('portal_user')) AS r(name)
     WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r.name)")
absent="$(echo "$absent" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//')"
if [ -z "$absent" ]; then
    echo "    all three roles already present (compose-built host) -- skipping"
    echo "    (existing role passwords left alone; .env stays authoritative)"
else
    echo "    missing: $absent -- creating from globals.sql"
    for role in $absent; do
        # That role's own CREATE/ALTER lines, nobody else's.
        grep -E "^(CREATE|ALTER) ROLE \"?${role}\"?( |;)" "$BACKUP_DIR/globals.sql" \
            | run psql -v ON_ERROR_STOP=1 -U "$ITS_PG_ADMIN_USER" -d postgres
    done
    echo "    NOTE: these roles carry the backup-era passwords. If .env has"
    echo "    been rotated since, update them before starting the app tier."
fi

# Prepare one database: create it if absent, and decide what to do if it
# already holds tables.
#
# REFUSE A NON-EMPTY TARGET BY DEFAULT. Found by falsification: running this
# script against an already-restored database produced 937 "already exists"
# errors and aborted at `set -e` -- safe, but the operator's first signal was
# a wall of scrolling errors rather than a sentence. Re-running a restore is
# a realistic thing to do at 3am, by accident or because the first attempt
# looked wrong.
#
# BOTH DATABASES GO THROUGH THIS, and they are checked BEFORE either is
# restored. Guarding only `itsuite` would abort on `keycloak` AFTER `itsuite`
# had already been replaced -- the half-done state this guard exists to
# prevent. And note a real rollback ALWAYS has a populated target, so the
# rollback runbook uses ITS_RESTORE_FORCE=1 by design; the default refusal is
# for the mistyped restore, not for that.
prepare_db() {
    local db="$1" present
    # NOT `psql ... | grep -q exists`. Under `set -o pipefail`, `grep -q` can
    # exit as soon as it matches and SIGPIPE the psql upstream, making the
    # whole pipeline non-zero for a database that DOES exist -- which would
    # divert into CREATE DATABASE and fail. Same shape as the exit-code bug
    # already found in this sub-project (an exit status read from `tail`).
    # A -tAc scalar into a string compare has no pipeline at all.
    present=$(run psql -X -A -t -U "$ITS_PG_ADMIN_USER" -d postgres -c \
        "SELECT count(*) FROM pg_database WHERE datname='$db'")
    if [ "${present//[[:space:]]/}" = "0" ]; then
        psql_admin -d postgres -c "CREATE DATABASE $db OWNER $ITS_PG_ADMIN_USER"
        return 0
    fi
    local existing
    existing=$(run psql -X -A -t -U "$ITS_PG_ADMIN_USER" -d "$db" -c \
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
    existing="${existing//[[:space:]]/}"
    [ "$existing" = "0" ] && return 0
    if [ "${ITS_RESTORE_FORCE:-0}" = "1" ]; then
        echo "    '$db' has $existing tables; ITS_RESTORE_FORCE=1 -- recreating"
        psql_admin -d postgres -c "DROP DATABASE $db WITH (FORCE)"
        psql_admin -d postgres -c "CREATE DATABASE $db OWNER $ITS_PG_ADMIN_USER"
    else
        echo "ERROR: database '$db' already has $existing tables." >&2
        echo "Refusing to restore over it: pg_restore would emit hundreds of" >&2
        echo "'already exists' errors and leave a database that is neither the" >&2
        echo "old one nor the new one." >&2
        echo "" >&2
        echo "If you intend to REPLACE it (destroying its current contents):" >&2
        echo "    ITS_RESTORE_FORCE=1 $0 $BACKUP_DIR" >&2
        exit 1
    fi
}

echo "==> 2. databases"
prepare_db itsuite
# An `if` rather than `[ -s ... ] && prepare_db keycloak`. (An earlier
# comment here claimed the `&&` form would abort under `set -e`. That is
# wrong: bash exempts every non-final command of an AND-list from errexit --
# verified. The `if` is clearer regardless, but the justification was false
# and is corrected rather than quietly deleted.)
if [ -s "$BACKUP_DIR/keycloak.dump" ]; then
    prepare_db keycloak
fi

echo "==> 3. pg_restore"
run pg_restore -U "$ITS_PG_ADMIN_USER" -d itsuite < "$BACKUP_DIR/itsuite.dump"
if [ -s "$BACKUP_DIR/keycloak.dump" ]; then
    run pg_restore -U "$ITS_PG_ADMIN_USER" -d keycloak < "$BACKUP_DIR/keycloak.dump"
fi

echo "==> 4. re-narrow app_user (grant-app-privileges.sql)"
psql_admin -d itsuite -f - < "$ITS_GRANTS_SQL"

echo "==> verifying tenant isolation survived the restore"
# INVARIANT 5, ASSERTED RATHER THAN ASSUMED. A restore that produced the
# tables without their RLS policies would be a total tenant-isolation
# failure, in the one moment nobody is looking closely. FORCE ROW LEVEL
# SECURITY is the half that matters: plain RLS does not apply to the table's
# owner, and migrations run as its_admin, so a table with RLS enabled but not
# FORCED is not actually constrained for the role that owns it.
forced=$(run psql -X -A -t -U "$ITS_PG_ADMIN_USER" -d itsuite -c \
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname='public' AND c.relforcerowsecurity")
policies=$(run psql -X -A -t -U "$ITS_PG_ADMIN_USER" -d itsuite -c \
    "SELECT count(*) FROM pg_policies WHERE schemaname='public'")
forced="${forced//[[:space:]]/}"; policies="${policies//[[:space:]]/}"
echo "    $policies policies, $forced tables with FORCE RLS"
if [ "$forced" = "0" ] || [ "$policies" = "0" ]; then
    echo "ERROR: no row-level security survived the restore." >&2
    echo "Every tenant's data would be visible to every other tenant." >&2
    echo "The restore is NOT safe to serve traffic from." >&2
    exit 1
fi
# EQUALITY, NOT A FLOOR -- the interesting failure is PARTIAL. RLS is not
# lost wholesale; the ROLE-SCOPED policies are, because `CREATE POLICY ... TO
# <role>` fails when the role is absent. Measured on a role-less restore: 122
# of 133 policies landed, FORCE RLS still 122, and all 11 missing were
# isolation policies naming portal_user or app_user. Both zero-checks above
# pass that state. Comparing against the count the backup recorded is what
# catches it.
if [ -s "$BACKUP_DIR/policy_count" ]; then
    expected=$(tr -d '[:space:]' < "$BACKUP_DIR/policy_count")
    if [ -n "$expected" ] && [ "$policies" != "$expected" ]; then
        echo "ERROR: $policies policies restored, but the backup recorded $expected." >&2
        echo "Policies are missing. The likely cause is a role that did not" >&2
        echo "exist when the policies were created, so every policy naming it" >&2
        echo "was skipped -- tenant isolation is INCOMPLETE for those tables:" >&2
        run psql -X -U "$ITS_PG_ADMIN_USER" -d itsuite -c \
            "SELECT tablename, policyname FROM pg_policies
             WHERE schemaname='public' ORDER BY tablename" >&2 || true
        exit 1
    fi
    echo "    policy count matches the backup ($expected)"
else
    echo "    NOTE: backup has no policy_count (taken before backup.sh recorded"
    echo "    it); only the zero-check above could be applied."
fi

echo "==> verifying the narrowing actually took"
wide=$(run psql -X -A -t -U "$ITS_PG_ADMIN_USER" -d itsuite -c \
    "SELECT count(*) FROM information_schema.role_table_grants
     WHERE grantee='app_user'
       AND ((table_name IN ('audit_events','security_events')
             AND privilege_type IN ('UPDATE','DELETE'))
         OR (table_name IN ('permissions','role_permissions')
             AND privilege_type IN ('INSERT','UPDATE','DELETE')))")
if [ "${wide//[[:space:]]/}" != "0" ]; then
    echo "ERROR: app_user is still wide on the protected tables ($wide grants)." >&2
    echo "The restore is NOT safe to serve traffic from." >&2
    exit 1
fi
echo "    app_user correctly narrowed"
echo "==> OK"
