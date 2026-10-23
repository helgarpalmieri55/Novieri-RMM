#!/usr/bin/env bash
#
# HOST readiness for an ITMS deploy. Run this on the target server BEFORE
# anything else, and send the whole output back.
#
# `preflight.sh` checks ITMS's own configuration. This checks the machine it is
# landing on -- which matters far more when the host already runs something
# else, because the thing most likely to stop this stack dead is a port another
# platform already holds, and that is invisible from the repository.
#
# IT CHANGES NOTHING. Every command here reads. No package is installed, no
# service started, no file written. Safe to run on a production host with
# another platform live on it.
#
# Some checks need root to see process names behind sockets; run with sudo for
# the complete picture. Without sudo it still works and says what it could not
# see.
set -uo pipefail

say() { printf '\n=== %s ===\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "ITMS host check -- $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "Run as: $(id -un)$([ "$(id -u)" -eq 0 ] || echo '  (not root -- socket owners may be hidden)')"

say "1. What this machine is"
# The deploy runbook and every verified first-boot command target Ubuntu Server
# 24.04/26.04, so an Ubuntu host is the documented path. A RHEL-family host is
# not a worse choice but is UNTESTED for this stack -- see section 3.
cat /etc/os-release 2>/dev/null | grep -E '^(NAME|VERSION|VERSION_ID|ID|ID_LIKE)=' || echo "no /etc/os-release"
echo "kernel: $(uname -r)   arch: $(uname -m)"

say "2. Ports ITMS needs: 443 and 8443"
# The ONLY published ports in docker-compose.yml, both on traefik. A conflict
# here means `docker compose up` fails with "address already in use" and
# nothing starts. 8443 is TCP passthrough for the agent gateway's mTLS, so it
# cannot be shared with an HTTP reverse proxy -- it needs its own port or IP.
# WHAT THE LISTENER IS BOUND TO IS THE WHOLE QUESTION, not merely whether the
# port is busy. A container published as `443:443` binds 0.0.0.0:443 and holds
# the port on EVERY interface, so giving ITMS its own IP does NOT free it --
# the bind still fails with EADDRINUSE. Only a listener already constrained to
# one address (`10.0.0.5:443`) leaves the port available on another.
if have ss; then
  echo "--- listeners on 443 / 8443, WITH their bind address ---"
  ss -lntp 2>/dev/null | awk 'NR==1 || $4 ~ /:(443|8443)$/'
  echo
  echo "Read the Local Address column:"
  echo "  0.0.0.0:443 or *:443  -> held on ALL interfaces. ITMS cannot bind 443"
  echo "                           anywhere until that listener is constrained"
  echo "                           to its own IP."
  echo "  1.2.3.4:443           -> held on ONE address. ITMS can use another."
  echo
  echo "--- every listening address, for context ---"
  ss -lntp 2>/dev/null | awk 'NR>1 {print $4}' | sort -u | head -30
else
  echo "ss not present; try: netstat -lntp"
fi

say "3. Mandatory access control (SELinux / AppArmor)"
# docker-compose.yml bind-mounts EIGHT host paths read-only into containers.
# On Ubuntu (the documented target, and where first boot was actually executed)
# this is a non-issue: AppArmor's docker profile does not block bind-mount
# reads. It is checked anyway because on a RHEL-family host with SELinux
# ENFORCING the same mounts produce avc denials and the affected containers
# cannot read their config -- postgres never creates the app role, traefik
# serves no certificate, openbao does not start -- and that combination has
# never been run, so it would be unverified territory rather than a setting to
# flip.
if have getenforce; then
  echo "getenforce: $(getenforce)"
elif [ -r /sys/fs/selinux/enforce ]; then
  echo "selinux enforce flag: $(cat /sys/fs/selinux/enforce)"
else
  echo "no SELinux on this host (expected on Ubuntu/Debian)"
fi
if have aa-status; then
  echo "AppArmor: $(aa-status --enabled 2>/dev/null && echo enabled || echo 'present, not enabled')"
fi

say "4. Docker"
if have docker; then
  docker --version
  docker compose version 2>/dev/null || echo "NO 'docker compose' subcommand -- v1 'docker-compose' will not do: the compose file uses a top-level 'name:' key"
  # Engine 26+ refuses Traefik v3.1's API negotiation. The compose file pins
  # v3.6 for this reason and says so in a comment; the check is here because
  # the symptom -- every route answering a bare 404 while every container
  # looks healthy -- is expensive to diagnose.
  # Captured, not piped straight to stdout: `docker info --format` still
  # prints the rendered template with EMPTY fields when the daemon is
  # unreachable, so a naive `cmd || echo` prints both the useless line and the
  # error. Ask for one field and test it.
  _dv="$(docker info --format '{{.ServerVersion}}' 2>/dev/null || true)"
  if [ -n "$_dv" ]; then
    docker info --format 'server version: {{.ServerVersion}}   storage: {{.Driver}}   cgroup: {{.CgroupVersion}}' 2>/dev/null
  else
    echo "docker present but the daemon is not reachable as $(id -un) -- try with sudo"
  fi
  echo "--- containers already running on this host ---"
  docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null || echo "(cannot list)"
  echo "--- who else mounts the Docker socket (read this carefully) ---"
  # A container with the socket can read EVERY container's env on this host.
  # ITMS's env holds the database passwords, the OpenBao token and the S3
  # secret, and ITMS is domain-admin-equivalent over the fleet. If the other
  # platform has a socket-mounting component, co-location makes that component
  # equivalent to ITMS.
  for c in $(docker ps -q 2>/dev/null); do
    if docker inspect "$c" --format '{{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null | grep -q docker.sock; then
      docker inspect "$c" --format '  {{.Name}}  ({{.Config.Image}})' 2>/dev/null
    fi
  done
  echo "  (nothing listed above = nothing else mounts it)"
else
  echo "docker NOT installed."
  echo "On Ubuntu use Docker's OWN apt repo, not the distro's docker.io: the older"
  echo "Engine makes Traefik v3 discover no containers and answer a bare 404 on"
  echo "everything while every container looks healthy (deploy/README.md)."
fi

say "5. Resources"
# ITMS brings up postgres, redis, minio, keycloak, openbao, traefik, the
# api, a worker and the gateway -- on top of whatever is already here.
free -h 2>/dev/null | head -2
echo "--- disk ---"
df -h / /var /var/lib/docker 2>/dev/null | sort -u
echo "--- cpu ---"
nproc 2>/dev/null | sed 's/^/cores: /'

say "6. Network interfaces"
# Giving ITMS its own IP is the cleanest fix for a 443 conflict: both
# platforms keep standard ports and agents keep pointing at :8443.
if have ip; then
  ip -brief address 2>/dev/null
  echo "--- routes (ALL of them, not just the default) ---"
  # ASYMMETRIC ROUTING is the failure that appears after everything looks
  # correct. If ITMS answers on a second NIC whose subnet does not contain the
  # managed endpoints, replies leave via the DEFAULT route on the other NIC
  # with the wrong source address, and a stateful firewall drops them. The
  # symptom is an agent that connects intermittently, or completes a TLS
  # handshake and then stalls -- and it gets blamed on the gateway for a while.
  ip route show 2>/dev/null
  echo "--- policy routing rules (non-default ones mean source routing is in use) ---"
  ip rule show 2>/dev/null
  echo
  echo "If agents will reach ITMS on a NIC that is NOT the default route's, check"
  echo "their networks are on-link for that NIC or that a route/rule sends the"
  echo "replies back out of it."
else
  echo "iproute2 not present"
fi
if have nmcli; then
  echo "--- NetworkManager (RHEL-family / anything using NM) ---"
  nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>/dev/null
elif [ -d /etc/netplan ]; then
  echo "--- netplan (Ubuntu) ---"
  ls -1 /etc/netplan/ 2>/dev/null
fi

say "7. Firewall"
# :8443 must be reachable from every managed endpoint, and :443 from operators.
for fw in firewall-cmd ufw; do
  if have "$fw"; then
    case "$fw" in
      firewall-cmd) firewall-cmd --state 2>/dev/null; firewall-cmd --list-all 2>/dev/null | head -20 ;;
      ufw) ufw status verbose 2>/dev/null | head -20 ;;
    esac
  fi
done
have firewall-cmd || have ufw || echo "neither firewalld nor ufw present; check nftables/iptables directly"

say "8. Time"
# The agent gateway is mTLS and the console is OIDC. Both fail in confusing
# ways on a host whose clock is wrong -- certificate validity and token
# expiry are absolute times.
timedatectl 2>/dev/null | head -6 || date -u

echo
echo "=== done. Send this whole output back. ==="
echo "Nothing was changed on this host."
