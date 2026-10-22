#!/bin/sh
# One-shot OpenBao provisioning: transit signing key + agent PKI + the
# gateway's own server-cert role.
#
# Assumes the operator has initialized/unsealed OpenBao and exported a token
# with policy to manage mounts (see deploy/README.md first-boot runbook).
# BAO_TOKEN here is the PROVISIONING token (`OPENBAO_TOKEN` in .env) -- it
# needs sys/mounts, which the runtime `its-app` policy deliberately lacks.
set -eu

bao() { command bao "$@" 2>/dev/null || vault "$@"; }

# Ed25519 job-signing key: private key never leaves the vault.
if ! bao read transit/keys/job-signing >/dev/null 2>&1; then
  bao secrets enable transit || true
  bao write -f transit/keys/job-signing type=ed25519
fi

# PKI mount for agent client certificates.
if ! bao read pki/cert/ca >/dev/null 2>&1; then
  bao secrets enable pki || true
  bao secrets tune -max-lease-ttl=87600h pki
  bao write -field=certificate pki/root/generate/internal \
    common_name="it-suite internal CA" ttl=87600h >/dev/null
fi

# The two roles are written UNCONDITIONALLY (both writes are idempotent), not
# guarded by a `bao read` the way the mounts above are. A guard means an
# upgrade never picks up a corrected role definition, and every parameter
# below is here because its absence broke a real first boot:
#
#   key_type=any              the DEFAULT IS rsa, and the Go agent enrolls
#                             with an Ed25519 key -- `pki/sign/agent` answers
#                             400 `role requires keys of type rsa` and NO
#                             DEVICE CAN EVER ENROLL. Key strength is enforced
#                             server-side instead, by
#                             app/core/ca.py::_require_strong_key (Ed25519/
#                             Ed448, RSA >= 2048, P-256/384/521), called by
#                             BOTH CA backends before the CSR is signed or
#                             forwarded.
#
#                             THIS SENTENCE WAS ONCE FALSE, WHICH IS WHY IT IS
#                             SPELLED OUT. `_require_strong_key` was called
#                             only by `LocalCA` -- the development backend --
#                             while `OpenBaoPkiCA` posted the CSR straight
#                             through. Vault performs no key validation of its
#                             own under `key_type=any`, so the deployed
#                             configuration had nothing checking key strength
#                             at either end, and an enrollment token (broadly
#                             distributable by design, `max_uses` nullable)
#                             bought a certificate on a 1024-bit RSA key.
#                             Pinned now by
#                             tests/test_ca.py::test_openbao_ca_refuses_a_weak_key_without_asking_the_pki.
#                             If you ever narrow `key_type` here, do NOT delete
#                             that check: it is the only one that sees an
#                             Ed25519 CSR.
#   allowed_uri_sans          URI SANs are DENIED unless listed. The identity
#                             binding in docs/03-security-model.md IS the
#                             `its://org/<org>/device/<device>` URI SAN that
#                             app/core/ca.py sends as `uri_sans`.
#   use_csr_sans=false        default true makes the CSR's SANs win and drops
#                             the API-supplied `uri_sans` above.
#   use_csr_common_name=false default true takes the CN from the CSR, and the
#                             agent deliberately sends an EMPTY CSR subject
#                             (agent/internal/enroll). The issued CN would be
#                             blank, and agent/internal/enroll/rotate.go's
#                             `leaf.Subject.CommonName != current.DeviceID`
#                             check then fails EVERY certificate rotation -- a
#                             fleet that silently stops rotating and expires a
#                             year later. false makes the API-supplied
#                             common_name (the device UUID) the CN.
#   max_ttl=8760h             app/core/ca.py issues 365-day agent certs
#                             (AGENT_CERT_TTL_DAYS); the previous 720h (30d)
#                             ceiling silently capped every one of them.
bao write pki/roles/agent \
  allow_any_name=true enforce_hostnames=false \
  key_type=any \
  allowed_uri_sans="its://org/*" \
  use_csr_sans=false use_csr_common_name=false \
  client_flag=true server_flag=false max_ttl=8760h

# The gateway's OWN server certificate is issued from this same CA -- the
# agent pins that CA at enrollment, so the gateway's TLS identity has to chain
# to it. The `agent` role above is client-only and CANNOT issue it: the
# first-boot step "issue the gateway's server certificate from the internal
# CA" had no role to issue from at all until this one existed.
#
# key_type is CONCRETE here (ec/256), unlike the agent role above. This role
# ISSUES (OpenBao generates the keypair) rather than signs a CSR, and an
# issuing role with `key_type=any` is refused outright:
#   role key type "any" not allowed for issuing certificates without
#   providing key_type and/or key_bits request parameters
bao write pki/roles/gateway \
  allow_any_name=true enforce_hostnames=false \
  key_type=ec key_bits=256 \
  client_flag=false server_flag=true max_ttl=8760h

echo "openbao provisioning complete"
