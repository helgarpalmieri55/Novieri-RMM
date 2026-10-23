#!/usr/bin/env bash
# Remove the Toeshee agent from a Mac.
#
# Removes: the launchd job, the plist, the binary, the logs, and -- unless
# --keep-state -- the state directory (identity, private key, pins).
#
# DELIBERATELY LEFT ALONE, exactly as on Linux:
#   * the DEVICE RECORD in the control plane. Retire it in the console; an
#     uninstall on the endpoint is not authorised to delete a tenant's record,
#     and an offline Mac would look identical to an uninstalled one.
#   * the agent's CERTIFICATE at the gateway. Deleting the local copy revokes
#     nothing. Revoke in the console when decommissioning.
#   * the AUDIT LOG (append-only, invariant #4).
#
# UNTESTED ON A MAC -- see agent/packaging/README.md "What is untested".
set -euo pipefail

DESTDIR=""
BINDIR="/usr/local/libexec"
PLISTDIR="/Library/LaunchDaemons"
STATEDIR="/var/lib/its-agent"
LOGDIR="/var/log/its-agent"
LABEL="com.toeshee.itsagent"
KEEP_STATE=0

usage() {
  cat <<'USAGE'
Usage: sudo uninstall.sh [--keep-state] [--destdir DIR]

  --keep-state   Leave /var/lib/its-agent in place. Without it the enrolled
                 identity is destroyed: re-installing needs a NEW enrollment
                 token and mints a NEW device record.
  --destdir DIR  Operate on a staged tree instead of the live system.
USAGE
}

die() { printf 'uninstall.sh: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --keep-state) KEEP_STATE=1; shift ;;
    --destdir) DESTDIR="${2:-}"; shift 2 ;;
    --destdir=*) DESTDIR="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || [ -n "$DESTDIR" ] || die "must run as root (sudo), or use --destdir"

if [ -z "$DESTDIR" ] && command -v launchctl >/dev/null 2>&1; then
  launchctl bootout "system/${LABEL}" 2>/dev/null || true
fi

rm -f "${DESTDIR}${PLISTDIR}/${LABEL}.plist"
rm -f "${DESTDIR}${BINDIR}/its-agent"
rm -rf "${DESTDIR}${LOGDIR}"

if [ "$KEEP_STATE" -eq 1 ]; then
  printf 'kept %s (identity, key, pins)\n' "${DESTDIR}${STATEDIR}"
else
  # THE CREDENTIALS FIRST AND BY NAME, so a failure removing the directory still
  # takes them with it -- and ALL FOUR, not two. `agent.key.pending` is the
  # private key a certificate rotation built its CSR over, and `previous.json`
  # carries {cert, chain, KEY} for the identity a rotation replaced
  # (agent/internal/enroll/rotate.go). Both are device private key material and
  # both survived the previous two-name list.
  rm -f "${DESTDIR}${STATEDIR}/enroll-token" \
        "${DESTDIR}${STATEDIR}/agent.key" \
        "${DESTDIR}${STATEDIR}/agent.key.pending" \
        "${DESTDIR}${STATEDIR}/previous.json"
  rm -rf "${DESTDIR}${STATEDIR}"
fi

cat <<'EOF'
its-agent removed from this Mac.

Still present, on purpose:
  * the device record in the control plane (retire it in the console)
  * its certificate at the gateway (revoke it there if decommissioning)
  * the audit log, which is append-only
EOF
