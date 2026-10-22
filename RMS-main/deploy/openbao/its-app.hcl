# Least-privilege runtime policy for the it-suite api / gateway / worker
# processes -- the token in ITS_OPENBAO_TOKEN.
#
# WHY THIS EXISTS. .env.example used to define OPENBAO_TOKEN=${ITS_OPENBAO_TOKEN},
# i.e. one token for both the one-shot mount provisioning and every application
# process. In practice that meant handing the api the ROOT token: a bug or an
# RCE in a request handler could then re-key, re-mount or delete `job-signing`
# -- the key that authorises every command sent to every endpoint in the fleet.
#
# Every path below is one the application actually calls. Nothing else, and in
# particular NO sys/mounts and NO pki/issue/gateway (that is the operator's
# provisioning token, used once by deploy/scripts/issue-gateway-cert.py).
#
#   backend/app/core/signing.py  transit/sign/job-signing, transit/keys/...
#   backend/app/core/vault.py    transit/{keys,encrypt,decrypt}/org-<uuid>
#   backend/app/core/ca.py       pki/sign/agent, pki/ca/pem, pki/revoke

# Sign the job envelope. The private key never leaves the vault.
path "transit/sign/job-signing" {
  capabilities = ["update"]
}

# Read the public half (the agent pins it at enrollment). Read only -- this
# policy can neither rotate nor delete the signing key.
path "transit/keys/job-signing" {
  capabilities = ["read"]
}

# Per-org envelope keys, created lazily on first use (vault.py::_ensure_key).
path "transit/keys/org-*" {
  capabilities = ["create", "update", "read"]
}

path "transit/encrypt/org-*" {
  capabilities = ["update"]
}

path "transit/decrypt/org-*" {
  capabilities = ["update"]
}

# Agent enrollment: sign the device CSR, publish the CA, revoke on decommission.
path "pki/sign/agent" {
  capabilities = ["update"]
}

path "pki/ca/pem" {
  capabilities = ["read"]
}

path "pki/revoke" {
  capabilities = ["update"]
}
