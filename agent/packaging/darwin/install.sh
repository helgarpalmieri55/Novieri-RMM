#!/usr/bin/env bash
# Install the Toeshee agent as a launchd system daemon on macOS.
#
#   sudo ITS_AGENT_ENROLL_TOKEN=... ./install.sh --url https://control-plane.example
#
# UNTESTED ON A MAC. This repository's CI is ubuntu-latest and no macOS host has
# run this script; what is verified is its structure (agent/packaging/
# packaging_test.go) and its shell (shellcheck). Read agent/packaging/README.md
# "What is untested" before trusting it on a fleet.
#
# As on Linux, the token never appears in argv: it comes from the environment or
# stdin, and lands in a 0600 file the agent deletes once it has bought a
# certificate.
set -euo pipefail

DESTDIR=""
BINDIR="/usr/local/libexec"
PLISTDIR="/Library/LaunchDaemons"
STATEDIR="/var/lib/its-agent"
LOGDIR="/var/log/its-agent"
LABEL="com.toeshee.itsagent"
BINARY=""
URL="${ITS_AGENT_ENROLL_URL:-}"
TOKEN_FROM_STDIN=0
START=1

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'USAGE'
Usage: sudo ITS_AGENT_ENROLL_TOKEN=<token> install.sh --url <control-plane-url>

Required:
  --url URL             Control-plane API base URL. Also ITS_AGENT_ENROLL_URL.
  The enrollment token, via ONE of:
      ITS_AGENT_ENROLL_TOKEN in the environment
      --token-stdin  with the token on stdin

Options:
  --binary PATH   The its-agent binary (default ../../dist/its-agent-darwin-arm64,
                  then ../../dist/its-agent, then ./its-agent).
  --destdir DIR   Stage under DIR; nothing is loaded into launchd.
  --no-start      Install the plist but do not bootstrap it.
  -h, --help      This.

The state dir is /var/lib/its-agent -- NOT ~/Library or /Library/Application
Support. That is the agent binary's own default for every non-Windows platform
(internal/config.defaultDataDir); the installer follows the binary rather than
the platform convention, because a mismatch here is an agent that silently
re-enrolls into a second device record.
USAGE
}

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2:-}"; shift 2 ;;
    --url=*) URL="${1#*=}"; shift ;;
    --binary) BINARY="${2:-}"; shift 2 ;;
    --binary=*) BINARY="${1#*=}"; shift ;;
    --destdir) DESTDIR="${2:-}"; shift 2 ;;
    --destdir=*) DESTDIR="${1#*=}"; shift ;;
    --token-stdin) TOKEN_FROM_STDIN=1; shift ;;
    --no-start) START=0; shift ;;
    -h|--help) usage; exit 0 ;;
    --token|--token=*)
      die 'the enrollment token must not be passed as an argument: argv is visible in ps
       output to every user on the machine, and is kept in your shell history.
       Use:  sudo ITS_AGENT_ENROLL_TOKEN=<token> ./install.sh --url ...
       or:   printf %s "<token>" | sudo ./install.sh --token-stdin --url ...' ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || [ -n "$DESTDIR" ] || die "must run as root (sudo), or use --destdir to stage"

if [ -z "$BINARY" ]; then
  for candidate in \
      "$here/../../dist/its-agent-darwin-arm64" \
      "$here/../../dist/its-agent-darwin-amd64" \
      "$here/../../dist/its-agent" \
      "$here/its-agent" "./its-agent"; do
    if [ -f "$candidate" ]; then BINARY="$candidate"; break; fi
  done
fi
if [ -z "$BINARY" ] || [ ! -f "$BINARY" ]; then
  die "no agent binary found; build with agent/packaging/build.sh and pass --binary"
fi

TOKEN="${ITS_AGENT_ENROLL_TOKEN:-}"
if [ "$TOKEN_FROM_STDIN" -eq 1 ]; then
  [ -z "$TOKEN" ] || die "--token-stdin and ITS_AGENT_ENROLL_TOKEN are both set; pick one"
  IFS= read -r TOKEN || true
fi
if [ -n "$TOKEN" ] && [ -z "$URL" ]; then
  die "--url is required alongside an enrollment token"
fi

umask 077

install -d -m 0700 "${DESTDIR}${STATEDIR}"
install -d -m 0750 "${DESTDIR}${LOGDIR}"
install -d -m 0755 "${DESTDIR}${BINDIR}"

if [ -n "$TOKEN" ]; then
  tokenfile="${DESTDIR}${STATEDIR}/enroll-token"
  : > "$tokenfile"
  chmod 0600 "$tokenfile"
  printf '%s' "$TOKEN" > "$tokenfile"
  TOKEN=""
  note "enrollment token written to ${STATEDIR}/enroll-token (0600, root)"
  note "the agent deletes it as soon as it has exchanged it for a certificate"
fi

install -m 0755 "$BINARY" "${DESTDIR}${BINDIR}/its-agent"

install -d -m 0755 "${DESTDIR}${PLISTDIR}"
plist="${DESTDIR}${PLISTDIR}/${LABEL}.plist"
# launchd refuses a plist that is group- or world-writable, and it must be
# root-owned. 0644 root:wheel is what it wants.
install -m 0644 "$here/${LABEL}.plist" "$plist"
chown root:wheel "$plist" 2>/dev/null || true

# The control-plane URL goes into the plist's EnvironmentVariables dict. It is
# not a secret; the token is, and is not here. PlistBuddy ships with macOS.
if [ -n "$URL" ] && [ -x /usr/libexec/PlistBuddy ]; then
  /usr/libexec/PlistBuddy \
    -c "Delete :EnvironmentVariables:ITS_AGENT_ENROLL_URL" "$plist" >/dev/null 2>&1 || true
  /usr/libexec/PlistBuddy \
    -c "Add :EnvironmentVariables:ITS_AGENT_ENROLL_URL string ${URL}" "$plist"
elif [ -n "$URL" ]; then
  die "PlistBuddy not found; cannot set ITS_AGENT_ENROLL_URL in $plist"
fi

if [ -n "$DESTDIR" ]; then
  printf 'staged under %s (nothing loaded into launchd)\n' "$DESTDIR"
  exit 0
fi

command -v launchctl >/dev/null 2>&1 || die "launchctl not found; this is not macOS"
# bootout first so a re-install replaces a running daemon rather than failing
# with "service already loaded".
launchctl bootout "system/${LABEL}" 2>/dev/null || true

# AN UNENROLLED AGENT IS NOT BOOTSTRAPPED. This is the macOS half of the rule
# linux/debian/postinst has always held and windows/install.ps1 was corrected to
# hold on 2026-08-26: the agent exits 2 on every start when it has neither a
# token to spend nor an identity it already holds, and the plist's
# `KeepAlive` is UNCONDITIONAL. ThrottleInterval bounds the RATE of that loop
# (one exec per 10s) -- it does not end it, and launchd will respawn a daemon
# that can never start until somebody notices. Better a daemon that is not
# loaded than one that is failing forever.
if [ ! -f "${STATEDIR}/identity.json" ] && [ ! -f "${STATEDIR}/enroll-token" ]; then
  START=0
  note "plist installed and NOT bootstrapped: this Mac has no enrollment token and no"
  note "identity, so the agent would exit 2 on every start and KeepAlive would respawn"
  note "it every 10 seconds (ThrottleInterval). Enroll it with:"
  note "  sudo ITS_AGENT_ENROLL_TOKEN=<token> $here/install.sh --url <control-plane-url>"
fi

if [ "$START" -eq 1 ]; then
  # ENABLE BEFORE BOOTSTRAP, not after. A label that is in launchd's disabled
  # database -- which `launchctl disable` writes and which survives reboots and
  # reinstalls -- makes `bootstrap` fail, and under `set -e` the enable that
  # would have fixed it never ran. The install could then never recover from a
  # single earlier `launchctl disable`.
  launchctl enable "system/${LABEL}"
  launchctl bootstrap system "$plist"
fi

cat <<EOF

its-agent installed.

  status   sudo launchctl print system/${LABEL}
  logs     tail -f ${LOGDIR}/agent.log
  enrolled test -f ${STATEDIR}/identity.json && echo yes
  remove   $here/uninstall.sh

Not done here, and required before any real fleet rollout: the binary is
NEITHER SIGNED NOR NOTARIZED. See agent/packaging/README.md "Code signing".
EOF
