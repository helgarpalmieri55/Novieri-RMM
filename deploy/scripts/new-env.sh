#!/usr/bin/env bash
#
# Write deploy/.env for a NEW host from .env.example, with nothing left to
# substitute by hand (design 2026-11-23-vps-y-marca-novieri §3).
#
#   ./scripts/new-env.sh --host rmm.novieri.com
#   ./scripts/new-env.sh --host rmm.novieri.com --acme-email ops@novieri.com
#   ./scripts/new-env.sh --host rmm.novieri.com --acme-email ops@novieri.com \
#       --acme-ca-server https://acme-staging-v02.api.letsencrypt.org/directory
#
# WHAT IT WRITES. Every `change-me` gets a fresh random value (`openssl rand
# -hex 24`: 48 hex characters, safe inside the database URLs that interpolate
# them) -- except ITS_OPENBAO_TOKEN and OPENBAO_TOKEN, which stay `change-me`:
# their real values are tokens OpenBao issues at first boot (README steps 3.2
# and 3.4), and a random string there would only turn "you skipped step 3.5"
# into an api failing with 403 from the vault. `your-host` becomes the host in
# every URL; ITS_PUBLIC_HOST, ITS_PUBLIC_CONSOLE_URL, ITS_ACME_EMAIL and, when
# given, ITS_ACME_CA_SERVER are set.
#
# WHAT IT REFUSES. An existing .env (it never overwrites one: that file holds
# the deployment's passwords, and a second run would orphan the database's);
# a host that is not a lower-case DNS name with a dot (an IP address included:
# the public certificate, the gateway's and the Windows installer all need a
# name); an address that is not one.
#
# WHAT IT NEVER DOES. Print a secret. It prints the host, the URLs, the
# contact address, the CA and the NAMES of the keys it generated, and nothing
# it generated. The file is created 0600, before anything is written to it.
set -euo pipefail

self="$(readlink -f "$0")"
cd "$(dirname "$self")/.."   # deploy/

die() { printf 'new-env.sh: %s\n' "$*" >&2; exit 1; }

host=""
email=""
ca=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) [ $# -ge 2 ] || die "--host needs a value"; host="$2"; shift 2 ;;
    --acme-email) [ $# -ge 2 ] || die "--acme-email needs a value"; email="$2"; shift 2 ;;
    --acme-ca-server) [ $# -ge 2 ] || die "--acme-ca-server needs a value"; ca="$2"; shift 2 ;;
    -h|--help) awk 'NR==1{next} /^#/{print; next} {exit}' "$self"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[ -e .env ] && die ".env already exists; this never overwrites one. Move it aside first if you really mean to start over."
[ -f .env.example ] || die ".env.example is missing; run this from a checkout of the repository"
[ -n "$host" ] || die "--host is required, e.g. --host rmm.novieri.com"
# A DNS name: lower-case labels of letters, digits and inner hyphens, at least
# two of them. Four all-digit labels are an IPv4 address, and refused.
# bash's own anchored match: grep works line by line, so a value with a newline
# in it would pass on its first line and inject the rest into .env.
[[ $host =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
  || die "--host must be a lower-case DNS name such as rmm.novieri.com, not '$host'"

if [ -z "$email" ]; then
  [ -t 0 ] || die "--acme-email is required when stdin is not a terminal"
  read -r -p "Let's Encrypt account contact (not kept on the account; passed to ISRG's mailing list): " email
fi
[[ $email =~ ^[^@[:space:]]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$ ]] \
  || die "the ACME address must look like name@domain.tld, not '$email'"
if [ -n "$ca" ]; then
  [[ $ca =~ ^https://[^[:space:]]+$ ]] || die "--acme-ca-server must be an https URL"
fi
command -v openssl >/dev/null 2>&1 || die "openssl is required (apt-get install -y openssl)"

first_boot_tokens=" ITS_OPENBAO_TOKEN OPENBAO_TOKEN "
generated=()
umask 077
tmp="$(mktemp .env.new.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    \#*|'') printf '%s\n' "$line"; continue ;;
  esac
  key="${line%%=*}"
  value="${line#*=}"
  if [ "$value" = "change-me" ] && [[ "$first_boot_tokens" != *" $key "* ]]; then
    value="$(openssl rand -hex 24)"
    generated+=("$key")
  fi
  case "$key" in
    ITS_PUBLIC_HOST) value="$host" ;;
    ITS_PUBLIC_CONSOLE_URL) value="https://$host" ;;
    ITS_ACME_EMAIL) value="$email" ;;
    ITS_ACME_CA_SERVER) [ -z "$ca" ] || value="$ca" ;;
  esac
  value="${value//your-host/$host}"
  printf '%s=%s\n' "$key" "$value"
done < .env.example > "$tmp"
for key in ITS_PUBLIC_HOST ITS_PUBLIC_CONSOLE_URL ITS_ACME_EMAIL ITS_ACME_CA_SERVER; do
  grep -q "^$key=." "$tmp" || die ".env.example has no $key line; it is older than this script"
done
! grep -v '^#' "$tmp" | grep -q 'your-host' || die "a setting still names your-host; nothing was written"
# A hard link never replaces an existing file, so a .env that appeared while
# this ran is not overwritten either. mktemp made the file 0600.
if ! err="$(ln "$tmp" .env 2>&1)"; then
  [ -e .env ] && die ".env appeared while this ran; nothing was written"
  die "could not create .env: $err"
fi
rm -f "$tmp"
trap - EXIT

printf 'wrote .env (mode %s)\n' "$(stat -c %a .env)"
printf '  host            %s\n' "$host"
grep -E '^(ITS_PUBLIC_CONSOLE_URL|ITS_GATEWAY_URL|ITS_GATEWAY_RELAY_URL|ITS_S3_PUBLIC_ENDPOINT|ITS_ACME_EMAIL|ITS_ACME_CA_SERVER)=' .env \
  | sed 's/^/  /'
printf '  new secrets     %s\n' "${generated[*]}"
printf '  still change-me ITS_OPENBAO_TOKEN OPENBAO_TOKEN (README step 3.5 fills them)\n'
