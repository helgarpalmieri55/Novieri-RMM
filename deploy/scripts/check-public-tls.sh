#!/usr/bin/env bash
#
# After `docker compose up -d`: is :443 serving a certificate for
# ITS_PUBLIC_HOST that a browser and an agent will accept? (design
# 2026-11-23-vps-y-marca-novieri §2: "if ACME fails, the check after start
# says so, instead of leaving a certificate browsers refuse").
#
#   ./scripts/check-public-tls.sh                  # waits up to 180 s
#   ./scripts/check-public-tls.sh --wait 30
#   ./scripts/check-public-tls.sh --connect 127.0.0.1 --cafile "$RUNNER_TEMP/pebble-root.pem"
#
# Traefik asks Let's Encrypt for the certificate when it first sees a router
# for the host, and until one arrives it serves its own self-signed default
# ("TRAEFIK DEFAULT CERT") WITHOUT FAILING. So this waits, then refuses on
# anything a client would refuse: an untrusted chain, a name that is not the
# host, an expired certificate, the default one.
#
# AND A RENEWAL THAT KEEPS FAILING. Traefik renews 30 days before expiry and
# looks once a day, so a healthy certificate never has fewer than about 29
# days left; fewer than 28 means renewal has been failing for days. Nothing
# else would say so: Let's Encrypt stopped mailing expiry warnings in 2025.
#
# --connect is the address to dial (default: ITMS_BIND_IP when it names one
# address, else 127.0.0.1); the name sent as SNI and checked is always
# ITS_PUBLIC_HOST. --cafile trusts that file instead of the system's roots:
# for Pebble in CI, or Let's Encrypt staging's root on a rehearsal. --port is
# for the script's own tests (backend/tests/test_check_public_tls.py).
# Exit 0 = the certificate is good, with 28 days or more left. Exit 1 = it is
# not, and why.
set -euo pipefail

cd "$(dirname "$0")/.."   # deploy/

die() { printf 'check-public-tls.sh: %s\n' "$*" >&2; exit 1; }

connect=""
port=443
cafile=""
wait_s=180
while [ $# -gt 0 ]; do
  case "$1" in
    --connect) [ $# -ge 2 ] || die "--connect needs an address"; connect="$2"; shift 2 ;;
    --cafile) [ $# -ge 2 ] || die "--cafile needs a file"; cafile="$2"; shift 2 ;;
    --wait) [ $# -ge 2 ] || die "--wait needs seconds"; wait_s="$2"; shift 2 ;;
    --port) [ $# -ge 2 ] || die "--port needs a number"; port="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -f .env ] || die ".env does not exist"
host="$(sed -n 's/^ITS_PUBLIC_HOST=//p' .env | tail -1)"
[ -n "$host" ] && [ "$host" != "your-host" ] || die "ITS_PUBLIC_HOST is not set in .env"
if [ -z "$connect" ]; then
  bind="$(sed -n 's/^ITMS_BIND_IP=//p' .env | tail -1)"
  case "$bind" in ''|0.0.0.0) connect=127.0.0.1 ;; *) connect="$bind" ;; esac
fi
ca_args=()
if [ -n "$cafile" ]; then
  [ -s "$cafile" ] || die "--cafile $cafile is missing or empty"
  ca_args=(-CAfile "$cafile")
fi

deadline=$(( $(date +%s) + wait_s ))
last=""
while :; do
  out="$(openssl s_client -connect "$connect:$port" -servername "$host" \
           -verify_hostname "$host" "${ca_args[@]}" </dev/null 2>&1 || true)"
  code="$(printf '%s\n' "$out" | sed -n 's/^ *Verify return code: \([0-9]*\).*/\1/p' | tail -1)"
  pem="$(printf '%s\n' "$out" | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p')"
  issuer="$(printf '%s\n' "$pem" | openssl x509 -noout -issuer 2>/dev/null || true)"
  if [ "$code" = "0" ] && [ -n "$pem" ]; then
    if ! printf '%s\n' "$pem" | openssl x509 -noout -checkend $((28 * 86400)) >/dev/null; then
      printf 'REFUSE  the certificate on %s:%s for %s is trusted but has fewer than 28 days left (%s): Traefik renews at 30, so its renewal has been failing\n' \
        "$connect" "$port" "$host" "$(printf '%s\n' "$pem" | openssl x509 -noout -enddate)" >&2
      printf '        What Traefik said about it: docker compose logs traefik | grep -i acme\n' >&2
      exit 1
    fi
    printf 'ok      %s:%s presents a trusted certificate for %s\n' "$connect" "$port" "$host"
    printf '%s\n' "$pem" | openssl x509 -noout -issuer -ext subjectAltName -enddate | sed 's/^/        /'
    exit 0
  fi
  case "$issuer" in
    *"TRAEFIK DEFAULT CERT"*) last="Traefik is still serving its own default certificate: Let's Encrypt has not issued one for $host" ;;
    "") last="nothing answered TLS on $connect:$port" ;;
    *) last="the certificate on $connect:$port for $host does not verify (openssl code ${code:-none}; $issuer)" ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] || break
  sleep 5
done
printf 'REFUSE  %s\n' "$last" >&2
printf '        after %s s. What Traefik said about it: docker compose logs traefik | grep -i acme\n' "$wait_s" >&2
printf '        A staging or Pebble CA is expected to be untrusted: pass --cafile with its root.\n' >&2
exit 1
