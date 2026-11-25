#!/usr/bin/env bash
#
# Pre-flight for a first deploy. Run from `deploy/`, before `docker compose up`.
#
# WHY THIS EXISTS. GO-LIVE.md is eight sections of things that must be changed
# from defaults, and the failure mode of a checklist is a human reading it at
# the end of a long day and being sure they did step 4. Every check below is
# something the checklist already requires; this just refuses instead of
# trusting. It reads `.env`, the filesystem, DNS, this host's addresses and
# listening sockets, and Docker's versions -- it starts nothing and changes
# nothing.
#
# Exit 0 = nothing obviously wrong. Exit 1 = at least one refusal. It cannot
# tell you the deployment is CORRECT; a checker never can. It tells you that a
# specific list of known-bad states is absent.
set -euo pipefail

cd "$(dirname "$0")/.."

fail=0
refuse() { printf '  REFUSE  %s\n' "$1"; fail=1; }
ok()     { printf '  ok      %s\n' "$1"; }

echo "Novieri RMM pre-flight"
echo

# --- .env exists and is not world-readable -------------------------------
if [ ! -f .env ]; then
  refuse ".env does not exist. cp .env.example .env, then work through GO-LIVE.md §1."
  echo; echo "Refused before any other check -- nothing else can be judged."; exit 1
fi
ok ".env exists"

perms="$(stat -c '%a' .env)"
if [ "$perms" != "600" ]; then
  refuse ".env is mode $perms, not 600. It holds every password in the deployment. chmod 600 .env"
else
  ok ".env is mode 600"
fi

# --- no placeholder secrets ----------------------------------------------
# All six ship as `change-me`. The two OpenBao tokens are legitimately still
# `change-me` until first boot has run, so they are reported separately rather
# than blocking a first `up`.
first_boot_only="ITS_OPENBAO_TOKEN OPENBAO_TOKEN"
while IFS='=' read -r key value; do
  case "$key" in ''|\#*) continue ;; esac
  [ "$value" = "change-me" ] || continue
  if printf '%s\n' $first_boot_only | grep -qx "$key"; then
    printf '  note    %s is still change-me -- expected before first boot; §3.2/§3.4 issue it\n' "$key"
  else
    refuse "$key is still change-me. openssl rand -hex 24 (or start over with scripts/new-env.sh)"
  fi
done < .env

# --- IT_SUITE_VERSION is a real tag --------------------------------------
version="$(grep -E '^IT_SUITE_VERSION=' .env | cut -d= -f2- || true)"
if [ -z "$version" ] || [ "$version" = "dev" ]; then
  refuse "IT_SUITE_VERSION is '${version:-unset}'. ':dev' names whatever was last built on this host, so it is not a deployable state and NOT A ROLLBACK TARGET -- a deployment left on it has nothing to roll back to. Run ./scripts/build-images.sh and set the tag it prints."
else
  ok "IT_SUITE_VERSION=$version"
fi

# --- ITMS_BIND_IP names an address this host actually has -----------------
# A typo here fails at `docker compose up` with "cannot assign requested
# address", which reads like a Docker problem rather than a one-character .env
# problem. 0.0.0.0 (and an unset value, which compose defaults to 0.0.0.0) is
# the dedicated-host case and needs no check.
bind_ip="$(grep -E '^ITMS_BIND_IP=' .env | cut -d= -f2- || true)"
if [ -z "$bind_ip" ] || [ "$bind_ip" = "0.0.0.0" ]; then
  ok "ITMS_BIND_IP=${bind_ip:-unset} (all interfaces)"
elif ! command -v ip >/dev/null 2>&1; then
  printf '  note    ITMS_BIND_IP=%s could not be checked -- iproute2 is not installed\n' "$bind_ip"
elif ip -brief address 2>/dev/null | grep -qwF "$bind_ip"; then
  ok "ITMS_BIND_IP=$bind_ip is assigned to this host"
else
  refuse "ITMS_BIND_IP=$bind_ip is not an address on this host, so traefik cannot bind it. \`ip -brief address\` lists what is available."
fi

# --- the public address, and the certificate Traefik will ask for ----------
# Since 2026-11-23 there is no certificate file to check (design
# 2026-11-23-vps-y-marca-novieri §2): Traefik asks Let's Encrypt for one for
# ITS_PUBLIC_HOST at the first start. What can stop that is checked instead,
# BEFORE it is asked, because every failed validation counts against Let's
# Encrypt's limit of 5 per name and hour and a failed start leaves Traefik
# serving its own self-signed default. scripts/check-public-tls.sh checks the
# result after the start.
public_host="$(grep -E '^ITS_PUBLIC_HOST=' .env | tail -1 | cut -d= -f2- || true)"
# `sed -n ...p` so a blank value yields nothing rather than the whole line.
host="$(grep -E '^ITS_PUBLIC_CONSOLE_URL=' .env | tail -1 \
        | sed -nE 's|^[^=]+=https?://([^/:]+).*|\1|p' || true)"
if [ -z "$public_host" ] || [ "$public_host" = "your-host" ]; then
  if [ -n "$host" ] && [ "$host" != "your-host" ]; then
    fix="set ITS_PUBLIC_HOST=$host in .env (the host of ITS_PUBLIC_CONSOLE_URL); replace the existing ITS_PUBLIC_HOST line if there is one, do not add a second."
  else
    fix="set ITS_PUBLIC_HOST and ITS_PUBLIC_CONSOLE_URL in .env to the console's DNS name (scripts/new-env.sh writes both, but only for a host with no .env yet)."
  fi
  refuse "ITS_PUBLIC_HOST is '${public_host:-unset}': Traefik has no name to ask a certificate for; $fix"
elif [ "$host" != "$public_host" ]; then
  refuse "ITS_PUBLIC_HOST=$public_host is not the host of ITS_PUBLIC_CONSOLE_URL ('${host:-unset}'). The certificate would be for one name and sign-in checked against another; they must be the same."
else
  ok "ITS_PUBLIC_HOST=$public_host is the console's host"
fi

# The bundled MinIO's router carries Host(ITS_PUBLIC_HOST): an endpoint on
# another host gets 404 on every download. External S3 differs legitimately,
# so this is a note, not a refusal.
s3_host="$(grep -E '^ITS_S3_PUBLIC_ENDPOINT=' .env | tail -1 \
           | sed -nE 's|^[^=]+=https?://([^/:]+).*|\1|p' || true)"
if [ -n "$s3_host" ] && [ -n "$public_host" ] && [ "$s3_host" != "$public_host" ]; then
  printf '  note    ITS_S3_PUBLIC_ENDPOINT is on %s, not ITS_PUBLIC_HOST (%s): the bundled MinIO answers only on ITS_PUBLIC_HOST, so every download would get 404 unless the bucket is external S3\n' "$s3_host" "$public_host"
fi

acme_email="$(grep -E '^ITS_ACME_EMAIL=' .env | tail -1 | cut -d= -f2- || true)"
if [ -z "$acme_email" ]; then
  refuse "ITS_ACME_EMAIL is empty, and compose will not start Traefik without it. Set ITS_ACME_EMAIL in .env (replace the existing ITS_ACME_EMAIL line, do not add a second) to the Let's Encrypt account's contact address (scripts/new-env.sh asks for it, but only for a host with no .env yet)."
else
  ok "ITS_ACME_EMAIL is set"
fi
acme_ca="$(grep -E '^ITS_ACME_CA_SERVER=' .env | tail -1 | cut -d= -f2- || true)"
case "${acme_ca:-https://acme-v02.api.letsencrypt.org/directory}" in
  https://acme-v02.api.letsencrypt.org/directory) ok "ACME CA: Let's Encrypt production" ;;
  https://*) printf '  note    ACME CA is %s, not Let'"'"'s Encrypt production: its certificates are for a rehearsal, and no browser trusts them\n' "$acme_ca" ;;
  *) refuse "ITS_ACME_CA_SERVER=$acme_ca is not an https URL" ;;
esac

# The name must reach THIS machine, or Let's Encrypt's validation reaches
# another. The A record is ASKED OF DNS ALONE, as Let's Encrypt asks it:
# `getent -s dns` skips /etc/hosts, where cloud-init writes
# `127.0.1.1 <this host's name>` and where a line written by hand would hide a
# missing or wrong A record.
#
# IPv4 ONLY (decided 2026-11-24). This deployment is one IPv4 address: the
# runbook, host-network.md and this script reason about it, and nothing
# checks that :443 and :8443 answer on IPv6. So an AAAA record would send
# browsers and agents, which try IPv6 first, where nobody has checked this
# stack answers -- or, if it is another machine's, to that machine. (Let's
# Encrypt is no safety net: per https://letsencrypt.org/docs/ipv6-support/ it
# retries over IPv4 only on a connection timeout, "not on other types of
# error", so an AAAA at a host that answers but is wrong fails validation.)
# The AAAA is asked through Python's
# getaddrinfo, not `getent ahosts`: glibc's getent sets AI_ADDRCONFIG, which
# hides every AAAA on a host with no IPv6 address of its own -- exactly the
# VPS this runbook builds. getaddrinfo still reads /etc/hosts before DNS
# (`hosts: files dns`): cloud-init puts this host's name on no IPv6 line
# there (its `::1` line is localhost's), so only an IPv6 line added by hand
# for the name is read as an AAAA, and refused like one. `|| true`: getent
# exits 2 for a name with no address, which pipefail would turn into this
# script dying silently here.
if [ -n "$public_host" ] && [ "$public_host" != "your-host" ]; then
  if ! command -v getent >/dev/null 2>&1; then
    printf '  note    %s could not be resolved -- getent is not installed\n' "$public_host"
  else
    v4="$(getent -s dns ahostsv4 "$public_host" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true)"
    v6=""
    if command -v python3 >/dev/null 2>&1; then
      v6="$(python3 -I -c 'import socket, sys
try:
    found = socket.getaddrinfo(sys.argv[1], None, socket.AF_INET6, socket.SOCK_STREAM)
except OSError:
    found = []
print(" ".join(sorted({f[4][0] for f in found if not f[4][0].startswith("::ffff:")})))' "$public_host" 2>/dev/null || true)"
    else
      printf '  note    %s has no AAAA check -- python3 is not installed\n' "$public_host"
    fi
    if [ -z "$v4" ]; then
      refuse "$public_host does not resolve to an IPv4 address. Create its A record, pointing at this server, and wait for it to answer before the first start."
    else
      if [ -n "$bind_ip" ] && [ "$bind_ip" != "0.0.0.0" ]; then
        mine="$bind_ip"
      elif command -v ip >/dev/null 2>&1; then
        mine="$(ip -4 -o address show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ' || true)"
      else
        mine=""
      fi
      match=""
      for a in $v4; do
        case " $mine " in *" $a "*) match="$a" ;; esac
      done
      if [ -n "$match" ]; then
        ok "$public_host resolves to $match, an address of this host"
      elif [ -z "$mine" ]; then
        printf '  note    %s resolves to %s; this host'"'"'s addresses could not be read -- compare them by hand\n' "$public_host" "${v4% }"
      elif printf '%s\n' $mine | grep -qvE '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.)'; then
        refuse "$public_host resolves to ${v4% }, which is not an address of this host (${mine% }). Let's Encrypt would validate whatever answers there."
      else
        printf '  note    %s resolves to %s; every address of this host is private (%s): behind NAT the public address is not on an interface -- compare it by hand\n' "$public_host" "${v4% }" "${mine% }"
      fi
    fi
    if [ -n "$v6" ]; then
      refuse "$public_host has an AAAA record (${v6% }). This deployment is IPv4 only: browsers and agents try IPv6 first, and nothing has checked that this stack answers there (or the address is another machine's). Remove the AAAA record."
    fi
  fi
fi

# --- ports 443 and 8443 are free, unless this stack holds them -------------
if ! command -v ss >/dev/null 2>&1; then
  printf '  note    listeners on 443/8443 could not be checked -- iproute2 is not installed\n'
elif [ -n "$(docker compose ps --status running -q traefik 2>/dev/null || true)" ]; then
  ok "443 and 8443 are held by this stack's traefik"
else
  ss_ok=1
  held="$(ss -Hltn '( sport = :443 or sport = :8443 )' 2>/dev/null | awk '{print $4}' | tr '\n' ' ')" || ss_ok=0
  if [ "$ss_ok" = 0 ]; then
    refuse "listeners on 443/8443 could not be checked: ss failed. Run scripts/host-check.sh section 2 by hand."
  elif [ -n "$held" ]; then
    refuse "something already listens on ${held% } -- traefik needs 443 and 8443. scripts/host-check.sh section 2 names it."
  else
    ok "443 and 8443 are free"
  fi
fi

# --- Docker Engine 26+ and Compose 2.24+ (README, Prerequisites) -----------
at_least() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }
engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
composev="$(docker compose version --short 2>/dev/null | sed 's/^v//' || true)"
if [ -z "$engine" ]; then
  refuse "Docker Engine does not answer (docker version). Install Docker CE from Docker's repository (README, Prerequisites)."
elif ! at_least "$engine" 26; then
  refuse "Docker Engine $engine is older than 26: traefik:v3.6's Docker provider is refused by it, and every route answers 404."
else
  ok "Docker Engine $engine"
fi
if [ -z "$composev" ]; then
  refuse "Docker Compose does not answer (docker compose version)."
elif ! at_least "$composev" 2.24; then
  refuse "Docker Compose $composev is older than 2.24: .env's \${...} references inside env_file are not interpolated, and every database connection fails."
else
  ok "Docker Compose $composev"
fi

# --- .env must not be committable ----------------------------------------
if git -C .. check-ignore -q deploy/.env 2>/dev/null; then
  ok ".env is gitignored"
else
  refuse "deploy/.env is NOT gitignored. Every password in this deployment would be committed."
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "No known-bad state found. This does not mean the deployment is correct --"
  echo "work through GO-LIVE.md §2 (the first administrator), §4 (OpenBao seal), §5 (gateway cert)"
  echo "and §6 (back these up) before anything real touches it."
else
  echo "Refused. Fix the items above and run again."
fi
exit "$fail"
