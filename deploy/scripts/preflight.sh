#!/usr/bin/env bash
#
# Pre-flight for a first deploy. Run from `deploy/`, before `docker compose up`.
#
# WHY THIS EXISTS. GO-LIVE.md is seven sections of things that must be changed
# from defaults, and the failure mode of a checklist is a human reading it at
# the end of a long day and being sure they did step 4. Every check below is
# something the checklist already requires; this just refuses instead of
# trusting. It reads `.env` and the filesystem only -- it starts nothing,
# contacts nothing, and changes nothing.
#
# Exit 0 = nothing obviously wrong. Exit 1 = at least one refusal. It cannot
# tell you the deployment is CORRECT; a checker never can. It tells you that a
# specific list of known-bad states is absent.
set -euo pipefail

cd "$(dirname "$0")/.."

fail=0
refuse() { printf '  REFUSE  %s\n' "$1"; fail=1; }
ok()     { printf '  ok      %s\n' "$1"; }

echo "ITMS pre-flight"
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
# All seven ship as `change-me`. The two OpenBao tokens are legitimately still
# `change-me` until first boot has run, so they are reported separately rather
# than blocking a first `up`.
first_boot_only="ITS_OPENBAO_TOKEN OPENBAO_TOKEN"
while IFS='=' read -r key value; do
  case "$key" in ''|\#*) continue ;; esac
  [ "$value" = "change-me" ] || continue
  if printf '%s\n' $first_boot_only | grep -qx "$key"; then
    printf '  note    %s is still change-me -- expected before first boot; §3.2/§3.4 issue it\n' "$key"
  else
    refuse "$key is still change-me. openssl rand -base64 24"
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

# --- TLS material ---------------------------------------------------------
# Two entirely separate certificate systems; GO-LIVE §3 is emphatic that they
# must not be mixed. This checks only that the public one is present, because
# the gateway's internal-CA cert is issued during first boot.
tls_present=1
for f in certs/fullchain.pem certs/privkey.pem; do
  if [ ! -s "$f" ]; then
    refuse "$f is missing or empty. The public certificate for the operator hostname goes here (§3). It must NEVER be the gateway certificate -- agents pin the internal CA at enrollment."
    tls_present=0
  else
    ok "$f present"
  fi
done

# --- the certificate is USABLE, not merely present -----------------------
# Presence was the old bar and it is the wrong one. TRAEFIK SILENTLY FALLS BACK
# to a self-signed certificate on anything it cannot use: it logs no error, the
# container reports healthy, and operators are served a certificate their
# browsers reject while every check in this script passes. Worse, endpoints
# reach object storage over the same :443, so a fallback breaks backups and
# software deployment too -- and blames them.
#
# Every check below is a state that produces exactly that fallback.
if [ "$tls_present" -eq 1 ]; then
  # 1. A passphrase-encrypted key. Traefik runs unattended; there is nowhere
  #    for it to ask.
  key_readable=1
  if grep -q 'ENCRYPTED' certs/privkey.pem; then
    refuse "certs/privkey.pem is passphrase-encrypted and traefik cannot decrypt it. Write a decrypted copy: openssl pkey -in certs/privkey.pem -out certs/privkey.new && mv certs/privkey.new certs/privkey.pem && chmod 600 certs/privkey.pem"
    key_readable=0
  else
    ok "certs/privkey.pem is not passphrase-encrypted"
  fi

  # 2. The key must actually belong to the certificate. Copying the two files
  #    from different renewals is the ordinary way to get here, and nothing
  #    reports it -- the pair is only tested when a client connects.
  #
  #    Skipped for an encrypted key: `openssl pkey` cannot read one without the
  #    passphrase, so this would add a second, WRONG diagnosis ("could not be
  #    parsed") on top of the correct one above, and send the reader looking for
  #    a file-format problem that does not exist.
  if [ "$key_readable" -eq 1 ]; then
    cert_pub="$(openssl x509 -in certs/fullchain.pem -noout -pubkey 2>/dev/null || true)"
    key_pub="$(openssl pkey -in certs/privkey.pem -pubout 2>/dev/null || true)"
    if [ -z "$cert_pub" ] || [ -z "$key_pub" ]; then
      refuse "certs/fullchain.pem or certs/privkey.pem could not be parsed as PEM by openssl. A DER file renamed to .pem lands here."
    elif [ "$cert_pub" != "$key_pub" ]; then
      refuse "certs/privkey.pem is not the private key for certs/fullchain.pem -- their public keys differ. The usual cause is two files taken from different renewals."
    else
      ok "the certificate and key are a matching pair"
    fi
  fi

  # 3. Validity window.
  if ! openssl x509 -in certs/fullchain.pem -noout -checkend 0 >/dev/null 2>&1; then
    refuse "certs/fullchain.pem has EXPIRED."
  elif ! openssl x509 -in certs/fullchain.pem -noout -checkend 604800 >/dev/null 2>&1; then
    printf '  note    certs/fullchain.pem expires within 7 days -- renew before go-live, and automate it (README §3)\n'
  else
    ok "certs/fullchain.pem is in date"
  fi

  # 4. It must cover the hostname the REST of .env names, since that is the
  #    name operators type and agents resolve.
  #
  #    `-checkhost` EXITS 0 WHETHER OR NOT THE NAME MATCHES -- it reports in
  #    its output, not its status. So this greps the text. Testing the exit
  #    code would make the check pass for every certificate ever presented,
  #    which is worse than having no check at all: it would read as verified.
  host="$(grep -E '^ITS_OIDC_ISSUER=' .env \
          | sed -E 's|^[^=]+=https?://([^/:]+).*|\1|' || true)"
  if [ -z "$host" ] || [ "$host" = "your-host" ]; then
    refuse "ITS_OIDC_ISSUER does not name a real host, so the certificate cannot be checked against it and no OIDC login will work. Set it to your operator hostname (README step 1)."
  elif ! openssl x509 -in certs/fullchain.pem -noout -checkhost "$host" 2>/dev/null \
        | grep -q 'does match'; then
    refuse "certs/fullchain.pem does not cover '$host' (the host in ITS_OIDC_ISSUER). Traefik will serve it anyway and every browser and every agent will reject it."
  else
    ok "certs/fullchain.pem covers $host"
  fi

  # 5. Intermediates. Not a refusal: a certificate issued directly by a root
  #    your fleet already trusts is legitimately one PEM block. But a
  #    publicly-issued leaf sent without its chain validates in browsers that
  #    cache intermediates and fails in Go clients that do not -- so it breaks
  #    AGENTS while looking fine to you.
  chain_len="$(grep -c 'BEGIN CERTIFICATE' certs/fullchain.pem || true)"
  if [ "${chain_len:-0}" -le 1 ]; then
    printf '  note    certs/fullchain.pem holds ONE certificate and no intermediates. Fine for a root-issued cert; if it came from a public CA, agents will fail chain validation where browsers succeed.\n'
  else
    ok "certs/fullchain.pem carries $chain_len certificates (leaf + chain)"
  fi
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
  echo "work through GO-LIVE.md §2 (Keycloak), §4 (OpenBao seal), §5 (gateway cert)"
  echo "and §6 (back these up) before anything real touches it."
else
  echo "Refused. Fix the items above and run again."
fi
exit "$fail"
