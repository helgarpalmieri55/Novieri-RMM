#!/usr/bin/env bash
#
# Prepare a new Ubuntu Server 24.04 VPS for Novieri RMM: deploy/README.md,
# Prerequisites, P2 to P5 (design 2026-11-27-despliegue-automatizado §3).
#
#   sudo ./scripts/prepare-host.sh        # from the operator account
#
# WHAT IT DOES, in the runbook's order, each part skipped when it is done
# already: P2 the firewall (22, 443 and 8443 in, nothing else; ufw on); P3
# unattended security updates; P4 the clock (NTP on, if it is not); P5 Docker
# CE from Docker's own repository, and the operator account -- the one that
# ran sudo -- in the `docker` group. It ends by checking Docker Engine 26 and
# Compose 2.24, the two versions preflight.sh refuses below.
#
# WHAT IT DOES NOT DO: make the operator account (P1, by hand, as root: the
# checkout this script lives in is made by that account, in P6), clone the
# repository (P6: the deploy key is yours to add in GitHub), touch DNS, or
# open any other port. Its commands are the runbook's, in deploy/README.md's
# appendix, "What each script does, by hand".
#
# Prints `run` before what it changes, `skip` for what it finds done, `ok`
# for a check that held. Run it again after any failure.
set -euo pipefail

if [ -t 1 ]; then bold=$'\033[1m'; plain=$'\033[0m'; else bold=""; plain=""; fi
step="starting up"
say()  { step="$*"; printf '\n%s%s%s\n' "$bold" "$*" "$plain"; }
ok()   { printf '  ok      %s\n' "$*"; }
skip() { printf '  skip    %s\n' "$*"; }
act()  { printf '  run     %s\n' "$*"; }
note() { printf '  note    %s\n' "$*"; }
warn() { printf '  WARNING %s\n' "$*" >&2; }
die() {
  printf '\nFAILED  %s\n' "$1" >&2
  shift
  local line
  for line in "$@"; do printf '        %s\n' "$line" >&2; done
  trap - EXIT   # this is the report: the exit trap below must not print a second one
  exit 1
}

# Anything that stops the run outside `die` (apt holding its lock past the 300 s each
# apt-get waits for it, a mirror that does not answer, curl failing) ends here: FAILED,
# the step it was in, and the way to continue. The wait is for a VPS minutes old, whose
# cloud-init or unattended-upgrades may still hold dpkg's lock (review N6, 2026-11-27).
trap 'on_exit $?' EXIT
on_exit() { [ "$1" = 0 ] || fail_help "$1"; }
fail_help() {
  trap - EXIT
  printf '\nFAILED  "%s" did not finish (exit %s; the command'"'"'s own message is above)\n' "$step" "$1" >&2
  printf '        %s\n' "$rerun" >&2
  printf '        it skips what is done, and continues from here\n' >&2
  exit 1
}

# Every command printed below works from any folder: the script's own absolute path.
self="$(readlink -f "$0")"
rerun="sudo $self"

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" "$self --help" ;;
  esac
done

deploy_dir="$(cd "$(dirname "$self")/.." && pwd)"
os_release="${ITS_OS_RELEASE:-/etc/os-release}"
# Debconf asks nothing in the middle of a run; every answer is the package's default.
export DEBIAN_FRONTEND=noninteractive

[ "$(id -u)" = 0 ] || die "this changes the firewall, the packages and the groups: it runs as root" \
  "$rerun"
operator="${SUDO_USER:-}"
if [ -z "$operator" ] || [ "$operator" = root ]; then
  die "which account is the operator? Run this with sudo from that account" \
      "$rerun" \
      "a VPS with only root makes the account first, by hand: deploy/README.md, P1"
fi
ID="" VERSION_ID="" UBUNTU_CODENAME="" VERSION_CODENAME=""
# shellcheck disable=SC1090  # the system's own description of itself
. "$os_release"
[ -z "${ITS_OS_RELEASE:-}" ] || note "ITS_OS_RELEASE is set: the Ubuntu check read $os_release instead of /etc/os-release (tests only)"
[ "$ID" = ubuntu ] || die "this is not Ubuntu ($os_release says ID=$ID): P5 installs Docker from its Ubuntu repository" \
  "deploy/README.md, Prerequisites: Ubuntu Server 24.04 LTS"
[ "$VERSION_ID" = 24.04 ] || warn "Ubuntu $VERSION_ID, not 24.04: the runbook was written and rehearsed for 24.04"

# No `| grep -q` under pipefail: grep leaving early is a SIGPIPE upstream, and a false "no".
in_group() { [[ " $(id -nG "$1") " == *" $2 "* ]]; }
at_least() { [ -n "$1" ] && [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

# --------------------------------------------------------------- P2

# P2 is done when ufw is on, refuses what comes in, and lets in 22, 443 and 8443.
firewall_done() {
  local status="$1" port
  grep -q '^Status: active' <<<"$status" || return 1
  grep -q '^Default: deny (incoming)' <<<"$status" || return 1
  for port in 22 443 8443; do
    grep -qE "^$port/tcp +ALLOW IN" <<<"$status" || return 1
  done
}

say "P2 — the firewall"
status="$(ufw status verbose 2>/dev/null || true)"
if firewall_done "$status"; then
  skip "P2: ufw is on, denies what comes in, and allows 22/tcp, 443/tcp and 8443/tcp"
else
  act "ufw allow 22/tcp, 443/tcp and 8443/tcp; ufw --force enable"
  ufw allow 22/tcp
  ufw allow 443/tcp
  ufw allow 8443/tcp
  ufw --force enable
  status="$(ufw status verbose)"
  printf '%s\n' "$status"
  firewall_done "$status" || die "ufw is not what P2 needs (above)" \
    "it needs Status: active, Default: deny (incoming), and ALLOW IN for 22/tcp, 443/tcp and 8443/tcp" \
    "sudo ufw status verbose" \
    "if the default is not deny: sudo ufw default deny incoming" \
    "then: $rerun"
  ok "P2: ufw is on, denies what comes in, and allows 22/tcp, 443/tcp and 8443/tcp"
fi
extra="$(grep -E 'ALLOW IN' <<<"$status" | grep -vE '^(22|443|8443)/tcp ' | awk '{print $1}' | sort -u | tr '\n' ' ' || true)"
[ -z "$extra" ] || warn "ufw also lets in: ${extra% }. This stack needs 22, 443 and 8443 and nothing else."

# --------------------------------------------------------------- P3

say "P3 — security updates"
apt_conf="$(apt-config dump 2>/dev/null || true)"
if [ "$(dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null || true)" = "install ok installed" ] \
   && grep -qx 'APT::Periodic::Update-Package-Lists "1";' <<<"$apt_conf" \
   && grep -qx 'APT::Periodic::Unattended-Upgrade "1";' <<<"$apt_conf" \
   && [ "$(systemctl is-active unattended-upgrades 2>/dev/null || true)" = active ]; then
  skip "P3: unattended-upgrades is installed, on, and active"
else
  act "apt-get install unattended-upgrades, and its two lines in /etc/apt/apt.conf.d/20auto-upgrades"
  apt-get -o DPkg::Lock::Timeout=300 update
  apt-get -o DPkg::Lock::Timeout=300 install -y unattended-upgrades
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' | tee /etc/apt/apt.conf.d/20auto-upgrades
  [ "$(systemctl is-active unattended-upgrades 2>/dev/null || true)" = active ] \
    || die "unattended-upgrades is not active" "sudo systemctl enable --now unattended-upgrades" "$rerun"
  ok "P3: unattended-upgrades is active"
fi

# --------------------------------------------------------------- P4

say "P4 — the clock"
if [ "$(timedatectl show -p NTP --value 2>/dev/null || true)" = yes ] \
   && [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = yes ]; then
  skip "P4: NTP is on and the clock is synchronised"
else
  if [ "$(timedatectl show -p NTP --value 2>/dev/null || true)" != yes ]; then
    act "timedatectl set-ntp true"
    timedatectl set-ntp true
  fi
  for _ in $(seq 1 24); do
    [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = yes ] && break
    sleep 5
  done
  [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = yes ] \
    || die "the clock is not synchronised after 120 s: certificates, second-factor codes and the audit log depend on it" \
           "timedatectl status" \
           "check that the provider lets this VPS out to NTP (UDP 123), then: $rerun"
  ok "P4: NTP is on and the clock is synchronised"
fi
timedatectl status | grep -E 'System clock synchronized|NTP service'

# --------------------------------------------------------------- P5

say "P5 — Docker CE, from Docker's repository"
engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
composev="$(docker compose version --short 2>/dev/null | sed 's/^v//' || true)"
if [ "$(dpkg-query -W -f='${Status}' docker-ce 2>/dev/null || true)" = "install ok installed" ] \
   && at_least "$engine" 26 && at_least "$composev" 2.24; then
  skip "P5: Docker CE $engine and Compose $composev, from Docker's repository"
else
  act "the distribution's conflicting packages out; Docker's apt repository; docker-ce and its plugins"
  # shellcheck disable=SC2046  # one word per package the selection names
  apt-get -o DPkg::Lock::Timeout=300 remove -y $(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc 2>/dev/null | cut -f1)
  apt-get -o DPkg::Lock::Timeout=300 install -y ca-certificates curl git openssl iproute2
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
    "${UBUNTU_CODENAME:-$VERSION_CODENAME}" "$(dpkg --print-architecture)" | tee /etc/apt/sources.list.d/docker.sources
  apt-get -o DPkg::Lock::Timeout=300 update
  apt-get -o DPkg::Lock::Timeout=300 install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
relogin=no
if in_group "$operator" docker; then
  skip "P5: $operator is in the docker group"
else
  act "usermod -aG docker $operator"
  usermod -aG docker "$operator"
  relogin=yes
fi
note "the docker group grants root-level privileges (Docker's post-installation notes): see who is in it with: getent group docker"

# ------------------------------------------------------------- versions

say "Docker's versions"
engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
composev="$(docker compose version --short 2>/dev/null | sed 's/^v//' || true)"
at_least "$engine" 26 || die "Docker Engine is ${engine:-not answering}, and it must be 26 or newer: traefik:v3.6's Docker provider refuses an older one" \
  "sudo systemctl status docker --no-pager" "$rerun"
ok "Docker Engine $engine (26 or newer)"
at_least "$composev" 2.24 || die "Docker Compose is ${composev:-not answering}, and it must be 2.24 or newer: .env's \${...} references inside env_file need it" \
  "sudo apt-get install -y docker-compose-plugin" "$rerun"
ok "Docker Compose $composev (2.24 or newer)"

say "The host is ready for the first boot"
if [ "$relogin" = yes ]; then
  note "log out, and log in again as $operator: the docker group applies to a new login only"
else
  note "if you have not logged out and back in as $operator since it joined the docker group, do that first: the group applies to a new login only"
fi
note "then, as $operator: cd $deploy_dir && ./scripts/first-boot.sh"
