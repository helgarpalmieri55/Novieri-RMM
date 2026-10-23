# Credential Vault Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Envelope-encrypted credential vault (per-org DEK wrapped by an OpenBao transit root / local root; AES-256-GCM per secret), with audited store/checkout/rotate/delete API and an internal checkout path modules call — per `docs/kernel/vault.md`.

**Architecture:** `VaultService` interface with `LocalVaultService` (dev/test, local root key file) and `OpenBaoVaultService` (transit DEK wrapping) — same factory pattern as signing/CA. Secrets are AES-GCM ciphertext on the `credentials` row; the org's wrapped DEK lives in a new `org_vault_keys` table. No plaintext-at-rest path exists in either backend.

**Tech Stack:** unchanged (`cryptography` AES-GCM already available; existing OpenBao HTTP client pattern from signing.py).

## Global Constraints

- All prior global constraints (mypy strict, ruff, real-Postgres tests, forced RLS, forward-only migrations).
- Invariants from `docs/03-security-model.md`: encrypted at rest via envelope encryption; decrypt-at-use only; secrets never logged, returned in API responses, or sent to the frontend; every access audited.
- AES-256-GCM with AAD = `org_id || credential_id` binding ciphertext to its owner.
- The only endpoint that ever returns a secret is `POST /credentials/{id}/checkout`, and only to a `credentials.read`-authorized caller with a `reason`.
- No secret value is ever passed to a logger — enforced by keeping it out of every response/log path.

---

### Task 1: Model changes + migration 0008

**Files:** Create `backend/app/models/vault.py` (OrgVaultKey); Modify `backend/app/models/core.py` (Credential: add `ciphertext`, `wrapped_dek`, `key_version`), `backend/app/models/__init__.py`, `backend/migrations/versions/0008_vault.py`; Test `backend/tests/test_migrations.py` (add `org_vault_keys`)

**Interfaces:**
- Produces `Credential.ciphertext: str | None`, `Credential.wrapped_dek: str | None`, `Credential.key_version: int | None` (all nullable — null = metadata-only placeholder); `OrgVaultKey` (`org_id` PK/FK, `wrapped_dek: str`, `key_version: int`, `created_at`).
- Migration: `ALTER TABLE credentials ADD` the three columns; `CREATE TABLE org_vault_keys` + RLS block (ENABLE+FORCE+tenant_isolation, policy on `org_id`).

**Steps:**
- [ ] Extend EXPECTED_TABLES (+`org_vault_keys`) → FAIL.
- [ ] Implement model + columns, autogen+edit migration with the RLS block; suite green (completeness test covers `org_vault_keys`). Commit `feat(vault): credential ciphertext columns and org_vault_keys`.

### Task 2: VaultService (local + OpenBao) with envelope encryption

**Files:** Create `backend/app/core/vault.py`; Modify `backend/app/core/config.py` (`vault_backend: Literal["local","openbao"] = "local"`, `vault_root_key_path: Path | None`); Test `backend/tests/test_vault.py`

**Interfaces:**
- Produces `StoredSecret` (`dataclass: ciphertext_b64: str, wrapped_dek: str, key_version: int`); `class VaultService(Protocol)` with `async def store(session, *, org_id, credential_id, plaintext) -> StoredSecret`, `async def checkout(session, *, org_id, credential_id) -> str`, `async def delete(session, *, org_id, credential_id) -> None`.
- `LocalVaultService(root_key_path)` — root key file (0600, generated if absent) wraps DEKs with AES-256-GCM; `OpenBaoVaultService(addr, token)` — `transit/encrypt|decrypt/org-<org_id>` (auto-creates the key) for DEK wrapping, AES-GCM for the secret. Both: one DEK per org in `org_vault_keys` (created on first store, reused after), AAD `org_id||credential_id`, `nonce||ciphertext` base64. `get_vault(settings) -> VaultService` factory; `openbao` requires addr+token (ValueError otherwise).

**Steps:**
- [ ] Failing tests: store→checkout roundtrip returns exact plaintext (local); ciphertext bytes don't contain the plaintext; two secrets differ in ciphertext; AAD mismatch (decrypt with another credential_id) raises; first store creates one `org_vault_keys` row, second store reuses it; OpenBao backend against `httpx.MockTransport` roundtrips; factory guard.
- [ ] Implement; suite green. Commit `feat(vault): envelope-encryption vault service (local + OpenBao)`.

### Task 3: Credential API with audited checkout

**Files:** Create `backend/app/api/v1/credentials.py`; Modify `backend/app/main.py` (mount router + `app.state.vault = get_vault(settings)`); Test `backend/tests/test_credentials_api.py`

**Interfaces:**
- Consumes `VaultService`, `require_permission`, `audit_service`.
- Produces routes per spec §6: `POST /credentials` (`credentials.manage`, body `{name, type, device_group_id?, secret}`, response `CredentialOut {id, name, type, device_group_id, created_at, has_secret}` — no secret/ciphertext); `GET /credentials` + `GET /credentials/{id}` (`credentials.read`, metadata only); `POST /credentials/{id}/checkout` (`credentials.read`, body `{reason: str}`, returns `{secret: str}` once, audits `credential.checked_out` with reason, updates `last_used_at`); `PUT /credentials/{id}` (`credentials.manage`, rotate secret/edit metadata, audits `credential.rotated`); `DELETE /credentials/{id}` (`credentials.manage`, audits `credential.deleted`). Store/checkout/rotate/delete also audit `credential.created` on create.

**Steps:**
- [ ] Failing tests: create returns metadata with `has_secret=true` and NO `secret`/`ciphertext`/`wrapped_dek` keys; checkout returns the exact stored secret and writes `credential.checked_out` carrying the reason + bumps `last_used_at`; a second checkout still works (idempotent read); rotate changes the secret (old checkout value no longer returned, new one is) and audits `credential.rotated`; `credentials.read` role gets 403 on create/rotate/delete; foreign-org credential 404; delete audits and subsequent get 404; audit chain verifies across the lifecycle.
- [ ] Implement; suite green. Commit `feat(api): audited credential vault endpoints`.

### Task 4: Frontend — credentials management + verification gate

**Files:** Create `frontend/src/modules/credentials/CredentialsPage.tsx`; Modify `frontend/src/lib/api.ts` (types + methods), `frontend/src/App.tsx` (route `/credentials`, nav gated on `credentials.read`); Modify `README.md`; Test `frontend/src/lib/api.test.ts` (extend: credential create omits secret from any stored client state)

**Interfaces:**
- Produces `Credential {id, name, type, device_group_id, created_at, has_secret}`, `createCredential(body)`, `listCredentials()`, `checkoutCredential(id, reason)` (returns `{secret}` shown once in a modal, never cached in query state), `deleteCredential(id)`.

**Steps:**
- [ ] Failing vitest: the checkout helper returns the secret to its caller but the client never writes it into a react-query cache key (helper is a plain method, asserted by call shape).
- [ ] Implement page (list with type badges + `has_secret` indicator, create form, checkout-with-reason modal that reveals the secret once with a copy button and a warning it won't be shown again, delete); nav gated on `credentials.read`; mutations hidden without `credentials.manage`.
- [ ] Full gate: backend pytest, `mypy app`, `ruff check app tests`; frontend tsc/vitest/build; `docker compose config -q`. README status entry.
- [ ] Commit `feat(credentials): vault management console and verification`.

---

## Self-review

- **Coverage:** spec §3 envelope→T2; §4 interface/backends/factory→T2; §5 model→T1; §6 API→T3; §7 internal checkout→T2 (`checkout` is the same method modules call, exercised via T3); §8 testing→T1–T4; §2 invariants→T2 (no-plaintext) + T3 (no-secret-in-response, audited). No gaps.
- **Placeholders:** none; each task carries concrete signatures, routes, audit action names, and test intents.
- **Type consistency:** `StoredSecret` fields (T2) map to the three `Credential` columns (T1); `has_secret` derives from `ciphertext IS NOT NULL` consistently in T3/T4; audit action names (`credential.created|checked_out|rotated|deleted`) identical across T3 and spec §6.
