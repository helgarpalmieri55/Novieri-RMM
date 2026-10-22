# Least-privilege policy for issuing the GATEWAY's own server certificate --
# the token `deploy/scripts/issue-gateway-cert.py` is given, and nothing else.
#
# WHY THIS EXISTS. The runbook used to hand that script the ROOT token, and to
# hand it over on a command line:
#
#   docker compose run --rm --no-deps \
#     -e BAO_TOKEN="$(grep '^OPENBAO_TOKEN=' .env | cut -d= -f2-)" ...
#
# Two defects in one line. The value is expanded into the argv of `docker
# compose`, where /proc/<pid>/cmdline is world-readable and any user on the host
# can lift it out of `ps`; and the value being lifted is the credential that can
# re-key or delete `transit/job-signing` -- the key authorising every command
# sent to every endpoint in the fleet -- plus re-mount the PKI, read every
# org's envelope key, and issue itself a certificate for any name.
#
# `its-app.hcl` already establishes that the application does not run as root.
# A certificate issuer that runs monthly from a systemd timer, reading a token
# out of a file on the host, has exactly the same claim on that reasoning and a
# smaller need: ONE capability on ONE path.
#
# The script also GETs `pki/ca/pem`. That endpoint is unauthenticated by design
# -- a CA certificate is public -- so it is NOT granted here. Adding it would
# suggest the read needs authority it does not need.
#
# Mint a token for it (deploy/README.md step 3.4):
#
#   bao policy write gateway-issuer /bao/gateway-issuer.hcl
#   bao token create -orphan -policy=gateway-issuer -ttl=87600h
#
# `-orphan` for the same reason as the its-app token: a child of the root token
# dies when the root token is revoked, and revoking the root token after first
# boot is the correct thing to do.

# Issue the gateway's TLS server certificate from the internal CA. `update`,
# not `create`: Vault/OpenBao's `pki/issue/<role>` is a write endpoint.
path "pki/issue/gateway" {
  capabilities = ["update"]
}
