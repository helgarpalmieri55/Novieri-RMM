#!/usr/bin/env bash
# Install the Toeshee agent as a systemd service.
#
#   ITS_AGENT_ENROLL_TOKEN=... ./install.sh --url https://control-plane.example
#
# THE TOKEN NEVER APPEARS ON THE COMMAND LINE. It arrives in the environment or
# on stdin, and lands in a 0600 file the agent deletes as soon as it has bought
# a certificate. `--token` is refused, loudly, for the reason printed there.
#
# Everything is idempotent: re-running over an installed agent replaces the
# binary and the unit and leaves the enrolled identity alone.
set -euo pipefail

# --- defaults --------------------------------------------------------------
DESTDIR=""                       # staging prefix; when set, nothing is started
BINDIR="/usr/local/bin"
UNITDIR="/etc/systemd/system"
CONFDIR="/etc/its-agent"
STATEDIR="/var/lib/its-agent"
SERVICE="its-agent"
BINARY=""
URL="${ITS_AGENT_ENROLL_URL:-}"
TOKEN_FROM_STDIN=0
START=1

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'USAGE'
Usage: ITS_AGENT_ENROLL_TOKEN=<token> install.sh --url <control-plane-url> [options]

Required:
  --url URL             Control-plane API base URL (e.g. https://its.example.com).
                        May also be given as ITS_AGENT_ENROLL_URL.
  The enrollment token, via ONE of:
      ITS_AGENT_ENROLL_TOKEN in the environment   (preferred)
      --token-stdin  and the token on stdin       (preferred for pipelines)
  Neither is needed when re-installing over an already-enrolled agent.

Options:
  --binary PATH         The its-agent binary to install.
                        Default: ../../dist/its-agent, then ./its-agent.
  --destdir DIR         Stage everything under DIR instead of installing.
                        Nothing is started, nothing is enabled, no systemctl.
  --bindir DIR          Default /usr/local/bin
  --no-start            Install and enable, but do not start.
  -h, --help            This.

Uninstall with ./uninstall.sh (see --help there for what it deliberately keeps).
USAGE
}

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

# --- arguments -------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2:-}"; shift 2 ;;
    --url=*) URL="${1#*=}"; shift ;;
    --binary) BINARY="${2:-}"; shift 2 ;;
    --binary=*) BINARY="${1#*=}"; shift ;;
    --destdir) DESTDIR="${2:-}"; shift 2 ;;
    --destdir=*) DESTDIR="${1#*=}"; shift ;;
    --bindir) BINDIR="${2:-}"; shift 2 ;;
    --bindir=*) BINDIR="${1#*=}"; shift ;;
    --token-stdin) TOKEN_FROM_STDIN=1; shift ;;
    --no-start) START=0; shift ;;
    -h|--help) usage; exit 0 ;;
    --token|--token=*)
      # Refused rather than accepted-with-a-warning. By the time a warning is
      # printed the token is already in this process's argv, in the parent
      # shell's history, and in `ps` output for anyone on the box.
      die 'the enrollment token must not be passed as an argument: argv is world-readable
       through /proc/<pid>/cmdline and is kept in your shell history.
       Use:  ITS_AGENT_ENROLL_TOKEN=<token> ./install.sh --url ...
       or:   printf %s "<token>" | ./install.sh --token-stdin --url ...' ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || [ -n "$DESTDIR" ] || die "must run as root (or use --destdir to stage)"

# --- the binary ------------------------------------------------------------
if [ -z "$BINARY" ]; then
  for candidate in "$here/../../dist/its-agent" "$here/its-agent" "./its-agent"; do
    if [ -f "$candidate" ]; then BINARY="$candidate"; break; fi
  done
fi
if [ -z "$BINARY" ] || [ ! -f "$BINARY" ]; then
  die "no agent binary found; build one with agent/packaging/build.sh and pass --binary"
fi

# --- the token -------------------------------------------------------------
# Read into a variable and never echoed. `set -x` is nowhere in this script for
# the same reason.
TOKEN="${ITS_AGENT_ENROLL_TOKEN:-}"
if [ "$TOKEN_FROM_STDIN" -eq 1 ]; then
  [ -z "$TOKEN" ] || die "--token-stdin and ITS_AGENT_ENROLL_TOKEN are both set; pick one"
  IFS= read -r TOKEN || true
fi
# Blank is legal: re-installing over an enrolled agent needs no token, and the
# agent refuses to re-enroll anyway (a token is single-use, and a second
# enrollment would mint a duplicate device record).
if [ -z "$TOKEN" ] && [ ! -f "${DESTDIR}${STATEDIR}/identity.json" ]; then
  note "no enrollment token given; the agent will fail to enroll until one is placed at"
  note "  ${STATEDIR}/enroll-token   (mode 0600)"
fi
if [ -n "$TOKEN" ] && [ -z "$URL" ]; then
  die "--url is required alongside an enrollment token"
fi

# Every file this script creates is owner-only unless it is explicitly widened.
umask 077

# --- state directory + token ----------------------------------------------
install -d -m 0700 -o root -g root "${DESTDIR}${STATEDIR}"
if [ -n "$TOKEN" ]; then
  tokenfile="${DESTDIR}${STATEDIR}/enroll-token"
  # Created empty at 0600 BEFORE anything is written to it, so the token is
  # never briefly readable through a wider default mode.
  : > "$tokenfile"
  chmod 0600 "$tokenfile"
  chown root:root "$tokenfile" 2>/dev/null || true
  printf '%s' "$TOKEN" > "$tokenfile"
  TOKEN=""
  note "enrollment token written to ${STATEDIR}/enroll-token (0600, root)"
  note "the agent deletes it as soon as it has exchanged it for a certificate"
fi

# --- config ----------------------------------------------------------------
# 0600 even though neither value is secret: the file sits next to a credential
# in the operator's mind, and a widening here is the kind of thing that gets
# copied to the next file.
install -d -m 0700 -o root -g root "${DESTDIR}${CONFDIR}"
if [ -n "$URL" ]; then
  envfile="${DESTDIR}${CONFDIR}/agent.env"
  : > "$envfile"
  chmod 0600 "$envfile"
  cat > "$envfile" <<EOF
# Written by agent/packaging/linux/install.sh. Not a secret, and deliberately
# not where the enrollment token lives -- that is ${STATEDIR}/enroll-token.
ITS_AGENT_ENROLL_URL=${URL}
EOF
fi

# --- binary + unit ---------------------------------------------------------
install -d -m 0755 "${DESTDIR}${BINDIR}"
install -m 0755 -o root -g root "$BINARY" "${DESTDIR}${BINDIR}/its-agent"

install -d -m 0755 "${DESTDIR}${UNITDIR}"
unit="${DESTDIR}${UNITDIR}/${SERVICE}.service"
install -m 0644 -o root -g root "$here/its-agent.service" "$unit"
# The unit ships with the default ExecStart; honour --bindir without asking the
# operator to edit a file.
if [ "$BINDIR" != "/usr/local/bin" ]; then
  sed -i "s|^ExecStart=/usr/local/bin/its-agent$|ExecStart=${BINDIR}/its-agent|" "$unit"
fi

if [ -n "$DESTDIR" ]; then
  printf 'staged under %s (nothing started)\n' "$DESTDIR"
  exit 0
fi

# --- service ---------------------------------------------------------------
command -v systemctl >/dev/null 2>&1 || die "systemctl not found; this host does not use systemd"
systemctl daemon-reload
systemctl enable "${SERVICE}.service"
if [ "$START" -eq 1 ]; then
  systemctl restart "${SERVICE}.service"
fi

cat <<EOF

its-agent installed.

  status   systemctl status ${SERVICE}
  logs     journalctl -u ${SERVICE} -f
  enrolled test -f ${STATEDIR}/identity.json && echo yes
  remove   $here/uninstall.sh

The first line of the log names the data dir and whether this endpoint is
enrolled. A rejected enrollment is a bare 403 by design -- the control plane
does not say which check failed.
EOF
