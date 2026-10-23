# Pre-Phase-3 Kernel Security Review — 2026-07-18

The roadmap requires a security-hardening pass on the kernel **before Phase 3
(remote control) ships**, since that phase unlocks live privileged sessions.
Two independent audit passes reviewed the security-critical paths (auth,
gateway, signing, enrollment; tenancy, RLS, audit, vault, CA). This document
records every confirmed finding, what was fixed in this pass, and the items
deliberately deferred for a product decision.

## Fixed in this pass (committed, test-first)

| # | Sev | Finding | Fix |
|---|-----|---------|-----|
| 1 | HIGH | Enrollment-token `max_uses` TOCTOU: validate-then-increment across two transactions let a concurrent burst on a single-use token mint unlimited trusted agents. | Atomic guarded `UPDATE … WHERE hash/revoked/expiry/use_count … RETURNING`; consumption shares the enrollment transaction. Concurrency test proves exactly one agent from a single-use token under an 8-way burst. |
| 2 | HIGH | Credential checkout gated on `credentials.read`, which the read-only **auditor** holds → an auditor could decrypt every stored admin secret. | New `credentials.checkout` permission (migration 0009), granted to operators only; auditor keeps metadata-only `credentials.read`. |
| 3 | MED | `audit_events` append-only `REVOKE UPDATE, DELETE` existed only in the test harness, not a migration (prod runs migrations). | Added the REVOKE to migration 0009, matching the `security_events` pattern. Trigger already blocked live tampering; this restores the intended second layer. |
| 4 | MED | Unauthenticated JWKS refetch-amplification: a flood of tokens with random `kid`s each forced a blocking IdP fetch and flushed the key cache. | `JwksCache` gains a single-flight lock, a minimum refetch interval, and never clobbers the cache on a failed fetch. |
| 5 | LOW/MED | CA signed CSRs with any key strength → an agent's mTLS identity could rest on a 1024-bit RSA key. | Key allowlist: Ed25519/Ed448 and EC P-256/384/521 accepted; RSA must be ≥ 2048; everything else rejected. (Mirror on the OpenBao PKI role in deploy.) |
| 6 | LOW | Credential fetch/decrypt selected by `id` only (RLS + GCM AAD already protected it). | Explicit `org_id` predicate on the credential fetch and vault decrypt query (defense in depth). |
| — | — | Response hardening (not from a finding). | `X-Content-Type-Options: nosniff` on every response; `Cache-Control: no-store` + `Pragma: no-cache` on the credential-checkout response so a decrypted secret is never cached. |

## Deferred items — now fixed (2026-07-18, test-first, second pass)

All four items flagged for a product decision were implemented. Each is
gated behind explicit configuration so existing dev/test behavior is unchanged
and only production posture tightens.

- **A. Authenticate the audit log (MED). FIXED.** Each chain link is now an
  HMAC-SHA256 keyed by a secret the DB role cannot read (`ITS_AUDIT_HMAC_KEY_PATH`),
  computed in `AuditService._link` and checked with `hmac.compare_digest` in
  `verify()`. An attacker with only a SQL-write primitive knows the algorithm
  and the readable head hash but not the key, so they can neither recompute a
  tampered link nor forge a new valid-looking event. The API and gateway load
  the same key at startup (`load_audit_hmac_key`); production fails closed if it
  is absent, dev runs unkeyed unless a key is configured (backward compatible).
  Tests: keyed chain verifies; wrong key fails; a correctly-chained event forged
  with plain SHA-256 (attacker without the key) is caught at its row.

- **B. Live revocation disconnect + operator agent-revoke (LOW/MED). FIXED.**
  New `POST /api/v1/agents/{device_id}/revoke` (`enrollment.manage`) sets
  `Agent.cert_revoked` (committed first — the gateway's admission gate), updates
  the CA CRL via `ca.revoke`, records `enrollment.agent_revoked`, and publishes
  on the `agent_revoke` Redis channel. Every gateway replica runs a
  `RevokeSubscriber` that closes the revoked device's live connection (close
  code 4403), so revocation takes effect immediately, not only at next connect.
  Tests: endpoint sets flag + CRL + publishes; subscriber drops the right
  connection and leaves others untouched; permission and 404 paths.

- **C. Fail closed on missing signing/CA key material in production (LOW).
  FIXED.** `LocalEd25519Signer` and `LocalCA` take `allow_generate`, wired to
  `settings.environment != "production"`. In production a missing signing key or
  CA root raises (`MissingSigningKeyError` / `MissingCAKeyError`) instead of
  silently generating one and rotating the fleet identity; dev/test still
  auto-generate. Tests cover both the raise and the reuse-existing paths.

- **D. Fail closed on a missing org claim in multi-tenant mode (LOW). FIXED.**
  New `ITS_MULTI_TENANT` flag: when set, `validate_token` requires the org claim
  and never falls back to `default_org_slug`, so a token issued for one tenant
  cannot be admitted into the default org. Single-org on-prem behavior is
  unchanged. Tests cover reject-without-claim-even-with-default and accept-with-claim.

## Verified sound (no action)

RS256-pinned JWT validation (no alg-confusion; iss/aud/exp/sub enforced); mTLS
admission (TLS 1.3, `CERT_REQUIRED`, identity = peer-cert fingerprint → Agent
row, CSR proof-of-possession, server-set subject/SAN); cross-device job
rejection from the cert-bound identity; RLS coverage on every `org_id` table
(`ENABLE`+`FORCE` + policy; `app_user` NOSUPERUSER NOBYPASSRLS); transaction-
local tenant GUC; envelope encryption (AES-256-GCM, 96-bit random nonce, per-
org DEK, AAD binds org+credential, transient DEK, no plaintext-at-rest, no
secret in responses/logs); UUIDv7-ordered audit chain with per-org advisory
lock.
