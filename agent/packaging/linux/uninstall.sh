#!/usr/bin/env bash
# Remove the Toeshee agent from this endpoint.
#
# What it removes: the service, the binary, the config, and -- unless you pass
# --keep-state -- the state directory, which holds the device identity, its
# private key and the pinned trust anchors.
#
# WHAT IT DELIBERATELY DOES NOT TOUCH, because an agent uninstall is not a
# fleet-management decision:
#
#   * the DEVICE RECORD in the control plane. It stays, with its history, its
#     inventory and its audit trail, until someone retires it in the console.
#     A local uninstall silently deleting a server-side record would be a
#     tenant-scoped write triggered by an unauthenticated local action -- and
#     an offline endpoint would be indistinguishable from an uninstalled one.
#   * the agent's CERTIFICATE at the gateway. Removing the state directory
#     destroys the endpoint's copy; it does not revoke anything. Revoke in the
#     console if the endpoint is being decommissioned or was compromised.
#   * the AUDIT LOG. Append-only and hash-chained by design (invariant #4).
#   * journald history. `journalctl --vacuum-*` is the operator's call.
#
# Removing the state directory is irreversible in one specific way: the device
# key and certificate are gone, so re-installing needs a NEW enrollment token
# and mints a NEW device record. Use --keep-state if you are replacing the
# binary rather than the endpoint.
set -euo pipefail

DESTDIR=""
BINDIR="/usr/local/bin"
UNITDIR="/etc/systemd/system"
CONFDIR="/etc/its-agent"
STATEDIR="/var/lib/its-agent"
SERVICE="its-agent"
KEEP_STATE=0

usage() {
  cat <<'USAGE'
Usage: uninstall.sh [--keep-state] [--destdir DIR] [--bindir DIR]

  --keep-state   Leave /var/lib/its-agent (identity, key, pins) in place.
                 Without it the enrolled identity is destroyed and a fresh
                 install needs a new enrollment token and creates a new device
                 record in the control plane.
  --destdir DIR  Operate on a staged tree instead of the live system.

Never touched: the server-side device record, its certificate at the gateway,
and the audit log. Retire or revoke the device in the console if that is what
you mean.
USAGE
}

die() { printf 'uninstall.sh: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --keep-state) KEEP_STATE=1; shift ;;
    --destdir) DESTDIR="${2:-}"; shift 2 ;;
    --destdir=*) DESTDIR="${1#*=}"; shift ;;
    --bindir) BINDIR="${2:-}"; shift 2 ;;
    --bindir=*) BINDIR="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || [ -n "$DESTDIR" ] || die "must run as root (or use --destdir)"

if [ -z "$DESTDIR" ] && command -v systemctl >/dev/null 2>&1; then
  # `|| true` throughout: uninstalling a service that is already stopped, or
  # was never enabled, must succeed. A half-uninstall that aborts leaves a
  # running agent nobody thinks is installed.
  systemctl stop "${SERVICE}.service" 2>/dev/null || true
  systemctl disable "${SERVICE}.service" 2>/dev/null || true
fi

rm -f "${DESTDIR}${UNITDIR}/${SERVICE}.service"
rm -f "${DESTDIR}${BINDIR}/its-agent"
rm -rf "${DESTDIR}${CONFDIR}"

if [ "$KEEP_STATE" -eq 1 ]; then
  printf 'kept %s (identity, key, pins)\n' "${DESTDIR}${STATEDIR}"
else
  # The enrollment token first and by name, so a partial failure below still
  # takes the credential with it.
  rm -f "${DESTDIR}${STATEDIR}/enroll-token" "${DESTDIR}${STATEDIR}/agent.key"
  rm -rf "${DESTDIR}${STATEDIR}"
fi

if [ -z "$DESTDIR" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
  systemctl reset-failed "${SERVICE}.service" 2>/dev/null || true
fi

cat <<'EOF'
its-agent removed from this endpoint.

Still present, on purpose:
  * the device record in the control plane (retire it in the console)
  * its certificate at the gateway (revoke it there if decommissioning)
  * the audit log, which is append-only
EOF
