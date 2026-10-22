# Credential Vault Specification (kernel groundwork for Phase 3)

Status: approved (autonomous-build delegation, 2026-07-18)
Source: `docs/03-security-model.md` §Credential vault; consumer brief
`docs/modules/remote-control.md` (hard dependency). Kernel: `docs/kernel/spec.md`.

## 1. Why now

Remote control (Phase 3) cannot be built until the vault is solid — the module
brief says so explicitly. This is the last kernel-level security facility the
model calls for that Phase 0 deferred (Phase 0 shipped the `credentials`
metadata table and `credentials.read`/`credentials.manage` permissions, but no
secret storage). Building it now unblocks Phase 3 and keeps it a kernel
concern, not a module fork.

## 2. Invariants (from docs/03-security-model.md — non-negotiable)

- Secrets are **encrypted at rest** with envelope encryption: a per-org data
  key wraps each secret; the data key is wrapped by a root key in the vault
  (OpenBao transit). The application database stores **only metadata + a vault
  reference + the wrapped ciphertext** — never plaintext.
- Secrets are **decrypted only at point of use** by the specific job/session
  that needs them, then discarded from memory.
- Secrets are **never** written to logs, returned in API responses, or exposed
  to the frontend.
- Access is **scoped by RBAC** (and optionally device group) and **checked out
  with audit** — every access is an `AuditEvent`.

## 3. Envelope-encryption design

Two layers, so the application database never holds a key that can decrypt on
its own:

```
plaintext secret ──AES-256-GCM──▶ ciphertext           (stored in DB)
                     ▲
              per-org data key (DEK, 32 bytes)
                     │ wrapped by
              OpenBao transit root key  org-<org_id>    (never leaves the vault)
                     │ produces
              wrapped_dek (vault:v1:…)                  (stored in DB)
```

- On first credential for an org, generate a random 32-byte **DEK**, encrypt it
  with the org's transit key (`transit/encrypt/org-<org_id>`, auto-created),
  and cache the resulting `wrapped_dek` on the org (a `vault_dek` row, one per
  org). The plaintext DEK exists only transiently in server memory during a
  store/checkout and is zeroized after.
- Each secret: `AES-256-GCM(dek, nonce, plaintext, aad=org_id||credential_id)`
  → store `nonce || ciphertext` (base64) + `wrapped_dek` reference. AAD binds
  ciphertext to its org and credential id, so a row copied to another
  credential or org fails to decrypt.
- **Rotation-ready:** `wrapped_dek` carries the transit key version; a future
  rewrap re-encrypts DEKs without touching per-secret ciphertext. v1 ships the
  interface; scheduled rotation is a later phase.

## 4. VaultService interface (mirrors SigningService/CAService pattern)

`backend/app/core/vault.py`:

```
class VaultService(Protocol):
    async def store(session, *, org_id, credential_id, plaintext: str) -> StoredSecret
    async def checkout(session, *, org_id, credential_id) -> str   # plaintext, transient
    async def delete(session, *, org_id, credential_id) -> None
```

- **OpenBaoVaultService** (production): transit `encrypt`/`decrypt` for DEK
  wrapping via the existing OpenBao HTTP client; AES-GCM via `cryptography`.
- **LocalVaultService** (dev/test): a local root key file (0600) stands in for
  the transit root — wraps DEKs with AES-256-GCM instead of calling OpenBao.
  Identical envelope semantics; **no plaintext-at-rest path exists in either
  backend.** `get_vault(settings)` factory selects by `settings.vault_backend`
  (defaults `local`, `openbao` requires addr+token — same guard as signing).
- `StoredSecret` = `{ciphertext_b64, wrapped_dek, key_version}`; these persist
  on the `credentials` row (new columns), not a separate table.

## 5. Data model (migration 0008)

- Extend `credentials`: add `ciphertext` (Text, nullable — null = metadata-only
  placeholder), `wrapped_dek` (Text, nullable), `key_version` (int, nullable),
  `last_used_at` already exists. No secret column is ever selected into an API
  response model.
- New `org_vault_keys`: `org_id` (PK/FK), `wrapped_dek` (Text), `key_version`,
  `created_at` — the org's single DEK, wrapped. RLS-forced like every org
  table. (One DEK per org keeps transit calls minimal; per-secret DEKs are a
  possible later hardening, interface unaffected.)
- The completeness test requires the RLS policy on `org_vault_keys`.

## 6. Credential API (`app/api/v1/credentials.py`)

Phase 0 seeded the permissions but shipped no routes; add them now.

- `POST /api/v1/credentials` (`credentials.manage`) — body `{name, type,
  device_group_id?, secret}`; stores metadata + encrypts `secret`; response is
  metadata only (id, name, type, scope, created_at) — **never** the secret.
- `GET /api/v1/credentials` (`credentials.read`) — metadata list; no secret,
  no ciphertext.
- `GET /api/v1/credentials/{id}` (`credentials.read`) — metadata + whether a
  secret is set (`has_secret: bool`), never the value.
- `POST /api/v1/credentials/{id}/checkout` (`credentials.read` **plus** an
  explicit `reason` in the body) — returns the plaintext **once**, writes a
  `credential.checked_out` audit event (actor, credential, reason, ts) and
  updates `last_used_at`. This is the audited decrypt-at-use path operators and
  (later) the remote-control session engine call. Rate-note: checkout is the
  only endpoint that ever returns a secret, and only to an authorized caller
  over the authenticated API.
- `PUT /api/v1/credentials/{id}` (`credentials.manage`) — rotate the secret
  and/or edit metadata; re-encrypts; audits `credential.rotated`.
- `DELETE /api/v1/credentials/{id}` (`credentials.manage`) — audited
  `credential.deleted`.

Every store/rotate/checkout/delete writes an `AuditEvent`. The secret never
appears in any log line (structured logging redaction is unnecessary because
the value is never passed to a logger — enforced by the interface).

## 7. Internal checkout API for modules

`vault_service.checkout(...)` is called server-side by future modules
(remote-control session start) inside the job's tenant transaction — the same
audited path, no HTTP round-trip. The remote-control module will call it to
decrypt admin creds at session open and discard them after guacd consumes them.

## 8. Testing

- Envelope roundtrip: store → checkout returns the exact plaintext; ciphertext
  in the DB does not contain the plaintext bytes; two different secrets get
  different ciphertext; AAD mismatch (ciphertext moved to another credential
  id/org) fails decryption.
- Both backends (`local`, mocked `openbao`) pass the identical roundtrip;
  factory guard rejects `openbao` without addr/token.
- No-plaintext guarantees: the DB `credentials` row after store has null-safe
  metadata and non-null `ciphertext` that is not the plaintext; API create/get/
  list responses contain no `secret`/`ciphertext`/`wrapped_dek` fields (schema
  test).
- Audit: create/checkout/rotate/delete each append the expected action; the
  checkout audit carries the reason; the chain still verifies.
- RBAC/RLS: `credentials.read` cannot create/rotate/delete (403); a checkout
  requires `credentials.read`; foreign-org credential is 404 (RLS); the
  completeness test covers `org_vault_keys`.
- Org key lifecycle: first store creates one `org_vault_keys` row; subsequent
  stores reuse it.

## 9. Out of scope (later)

Scheduled DEK/root-key rotation execution, per-secret DEKs, HSM backing beyond
OpenBao transit, and the remote-control session engine itself (Phase 3, which
consumes this vault).
