# Phase 0 — Platform Kernel Specification

Status: **approved** (decisions confirmed by product owner, 2026-07-17)
Sources: `docs/00-vision.md` … `docs/06-roadmap.md`. This spec turns the Phase 0
scoping brief in `docs/06-roadmap.md` into an implementable design. Where this
spec and the core docs overlap, the core docs are authoritative; this spec adds
implementation-level decisions.

---

## 1. Purpose & scope

Phase 0 delivers the platform kernel every business module registers into:
tenancy, RBAC, audit, agent enrollment/PKI, the agent gateway, the signed-job
engine, object-storage wiring, and a minimal web shell. No business module ships
in Phase 0.

**In scope (this repo — control plane):**

- Repo scaffold: `backend/`, `frontend/`, `deploy/`, `scripts/` per `CLAUDE.md`.
- PostgreSQL 16 core data model (`docs/04-data-model.md`) with multi-tenancy
  enforced by Row-Level Security, via Alembic migrations.
- Operator authentication through Keycloak (OIDC), RBAC with seeded baseline
  roles, enforced server-side on every privileged endpoint.
- Agent enrollment: enrollment tokens, CSR signing by the internal CA, device +
  agent registration, certificate lifecycle metadata.
- Agent gateway: outbound-only mTLS WebSocket service; client-cert →
  device → org binding; job dispatch down, status/results up.
- Signed-job engine: create, canonicalize, sign (Ed25519), dispatch, track,
  retry, expire.
- Immutable hash-chained audit log with chain verification.
- MinIO/S3 wiring (client, bucket layout, health check, job-artifact upload).
- Minimal web shell: login, device list, job list/status, dispatch a job.
- `deploy/docker-compose.yml` for the full on-prem stack.
- The agent **wire-protocol contract** (envelope schemas) that the agent
  implements against.

**Out of scope (Phase 0):**

- The Go agent itself — **not built**. The roadmap's exit criterion (enroll
  three OSes end-to-end) completes only when a skeleton agent exists; this
  phase must leave the control plane ready for it.

  > **Amended 2026-08-05.** This entry used to read "lives in the separate
  > `it-suite-agent` repo", and the section heading used to say "Phase 0 **or
  > this repo**". `CLAUDE.md` (commit `a32cead`) moved the agent in-tree to
  > `agent/`, so the exclusion is now one of *phase*, not of *repository*: the
  > agent is out of Phase 0 because it is Phase 0's next cycle, not because it
  > is somebody else's tree. Nothing here became built by the move — there is
  > still no `agent/` directory. What must survive the move is that the
  > envelope schemas above stay a **compatibility contract**: a deployed agent
  > is older than the control plane, so a change to
  > `docs/kernel/wire-protocol/` is additive or versioned or it breaks the
  > fleet, and this repo's tests will not tell you which.
- All business modules (inventory beyond the `inventory.collect` job type
  registration, patch, remote, etc.).
- Guacamole (Phase 3), Sentinel/Odoo integrations, Helm chart.

  > **Amended 2026-08-06: site servers are no longer out of scope, and the
  > kernel needed no change to support them.** Migration `0050` added a
  > `site_server` registry table and a `site.wake` job type. The load-bearing
  > design decision (`docs/modules/site-server/spec.md` §0.1) is that **a site
  > server IS a `Device`** — an ordinary enrolled endpoint an operator has
  > promoted, with an ordinary device-bound certificate. Under that choice
  > `Job.target_type` stays `"device"`, `JobEngine.create` is called as-is,
  > `_load_job_for_device` is untouched, identity resolution stays
  > cert → `Agent` → `(device_id, org_id)`, and the wire protocol gains no
  > message kind.
  >
  > That claim is **asserted, not reviewed for**:
  > `backend/tests/test_site_server_kernel_boundary.py` pins the map of kernel
  > files that mention the module as an exact equality (two files, both
  > append-only registration points), walks every conditional in `app/jobs/`,
  > `app/gateway/` and `app/core/` for a branch on the module's name, and checks
  > that `jobs.target_type` is still `varchar(10)` — because `"site_server"` is
  > eleven characters, so a widened column is the earliest observable sign that
  > somebody has begun building the first-class-entity design §0.1 rejected.
  >
  > Still out of scope, and each gated on something specific: the LAN artefact
  > cache and PXE (both need `CLAUDE.md` invariant #2 amended — the wording is
  > drafted in site-server spec §4.4 and **has not been accepted**), and the
  > connection relay (opaque TCP forwarding only if ever, never TLS
  > termination).
- SAML; AD/Entra federation is configured inside Keycloak, not in our code.

## 2. Decisions finalized in this phase

| Decision | Choice | Rationale |
|---|---|---|
| Component library | **shadcn/ui** (+ Tailwind, TanStack Table) | Copy-in components, no lock-in, MIT |
| Secrets engine | **OpenBao** (transit + KV + PKI) | MPL-2.0 fits open-core posture; Vault-compatible API |
| Internal CA | **OpenBao PKI** | Reuses the vault already in the stack; one fewer service |
| Job-signing algorithm | **Ed25519** via OpenBao transit | Small keys/sigs, fast verify in Go agent |
| Wire encoding | Versioned JSON envelope (`v: 1`) | Per `docs/05-agent-spec.md` |
| Backend packaging | `pyproject.toml`, `uv`-compatible, plain pip works | No exotic tooling required |
| Python | 3.12 | Per `docs/02-tech-stack.md` |

## 3. Backend architecture

Two ASGI apps sharing one codebase (`backend/app/`):

- **API app** (`app/main.py`) — operator-facing HTTPS API under `/api/v1`,
  plus the UI WebSocket for live status.
- **Gateway service** (`app/gateway/main.py`) — agent-facing mTLS WebSocket
  service built directly on asyncio + `websockets` (not uvicorn, whose ASGI
  scope hides the peer certificate). Separable: runs as its own process/
  container so it scales independently (per `docs/01-architecture.md`). It
  terminates TLS itself with client certificates required; Traefik routes it
  as TCP passthrough.

```
backend/app/
├── core/
│   ├── config.py        # pydantic-settings; all config via env
│   ├── db.py            # async engine/session; tenant context manager
│   ├── tenancy.py       # org context resolution + SET LOCAL app.current_org
│   ├── security.py      # JWT validation (JWKS), current-user dependency
│   ├── rbac.py          # require_permission() dependency; permission codes
│   ├── audit.py         # AuditService: append + verify hash chain
│   ├── signing.py       # SigningService interface + OpenBao transit + local Ed25519
│   ├── ca.py            # CAService interface + OpenBao PKI + local dev CA
│   ├── vault.py         # OpenBao HTTP client (transit/KV/PKI)
│   └── storage.py       # S3/MinIO client wrapper + bucket layout
├── models/              # SQLAlchemy 2.x typed models (core entities)
├── gateway/
│   ├── main.py          # ASGI app; WS endpoint /agent
│   ├── connections.py   # in-process registry device_id → connection
│   ├── protocol.py      # envelope schemas (Pydantic) — the wire contract
│   └── dispatch.py      # Redis pub/sub bridge: job dispatch + status fan-out
├── jobs/
│   ├── engine.py        # create/sign/dispatch/track/retry/expire
│   └── canonical.py     # canonical JSON serialization for signing
├── api/v1/              # routers: auth, orgs, devices, jobs, enrollment, audit, ws
├── modules/             # empty in Phase 0; module registration point
└── worker.py            # Celery app: job-expiry sweep, retry scheduling
```

Services own business logic; route handlers stay thin (parse → authorize →
call service → serialize).

## 4. Data model & migrations

Alembic migrations create exactly the core entities of `docs/04-data-model.md`:
`organizations`, `users`, `roles`, `permissions`, `role_permissions`,
`user_roles`, `devices`, `device_groups`, `device_tags`, `agents`, `policies`,
`credentials` (metadata only), `jobs`, `job_logs`, `audit_events`, plus
`enrollment_tokens` (Phase 0 addition: `id`, `org_id`, `token_hash`, `name`,
`expires_at`, `max_uses`, `use_count`, `revoked`, `created_by`).

Conventions (from the data model doc): UUIDv7 primary keys, `org_id` on every
tenant-scoped table, `created_at`/`updated_at`, jsonb for evolving detail.
Baseline roles and the Phase 0 permission set are seeded in a data migration.
`roles` with `org_id IS NULL` are global built-ins.

## 5. Tenancy & RLS

- The application connects as a dedicated `app_user` PostgreSQL role: not a
  superuser, no `BYPASSRLS`. Migrations run as the owning role.
- Every tenant-scoped table gets `ENABLE ROW LEVEL SECURITY` **and**
  `FORCE ROW LEVEL SECURITY`, with policy
  `USING (org_id = current_setting('app.current_org')::uuid)` (and the same
  `WITH CHECK`). Global rows (built-in roles, permissions) live in tables
  without `org_id` or with explicit `org_id IS NULL OR …` policies.
- `core/tenancy.py` provides the only way to get a DB session for
  tenant-scoped work: an async context manager that opens a transaction and
  issues `SET LOCAL app.current_org = :org` before yielding the session. API
  requests derive the org from the authenticated user; gateway connections
  derive it from the client certificate's device binding. No org context → no
  tenant-scoped session.
- Super-admin/cross-org paths (SaaS operator) are explicit: a separate,
  audited code path — not a default.
- **Adding a tenant-scoped table without an RLS policy is a security bug**; a
  test walks `pg_tables`/`pg_policies` and fails if any table with an `org_id`
  column lacks a forced RLS policy.

## 6. Authentication & RBAC

- Operators authenticate against **Keycloak** (OIDC authorization code + PKCE
  in the SPA). The API is a resource server: it validates `Bearer` JWTs
  against Keycloak's JWKS (cached, kid-rotation aware), checking issuer,
  audience, expiry, signature.
- On first authenticated request, the platform upserts the `users` projection
  row from token claims (`external_subject` = Keycloak `sub`; org resolved
  from a configured claim, single-org default on-prem).
- MFA, AD/Entra federation, and session policies are Keycloak configuration
  (documented in `deploy/keycloak/`), not application code.
- **RBAC:** permission codes are `module.action` strings (Phase 0 set:
  `devices.read`, `devices.manage`, `jobs.read`, `jobs.dispatch`,
  `enrollment.manage`, `audit.read`, `credentials.read`, `credentials.manage`,
  `policies.manage`, `orgs.manage`). Routers guard privileged endpoints with
  `require_permission("jobs.dispatch")`-style dependencies that resolve the
  user's roles → permissions within the current org (optionally scoped to a
  device group via `user_roles.device_group_id`). Enforcement is server-side
  only; the frontend merely hides what the API would refuse.
- For tests, a **fake IdP** issues tokens from a local RSA key and serves a
  JWKS document; the validation code path is identical to production.

## 7. Enrollment & agent PKI

- **Token issuance:** an operator with `enrollment.manage` creates a
  time-bounded, optionally use-limited enrollment token. The plaintext token
  (`<token_id>.<secret>`) is shown once; only a hash is stored.
- **Enrollment endpoint** (`POST /enroll/v1`, unauthenticated TLS, on the API
  app): the installer submits `{enrollment_token, csr, device_facts}`. The
  server validates the token (unexpired, unrevoked, uses remaining), creates
  the `Device` (from facts: hostname, os_family, …) and `Agent` rows, has the
  CA sign the CSR with the device identity (CN = device UUID, org bound via
  certificate SAN/OU), and returns: client cert chain, CA bundle to pin,
  gateway URL, and the **job-signing public key** to pin. Certificate serial +
  fingerprint + expiry are stored on the `Agent` row. Enrollment writes an
  audit event.
- **CAService** interface: `sign_csr(csr, device_id, org_id, ttl) → cert`,
  `revoke(serial)`. Production impl calls OpenBao PKI; a **local dev CA**
  (Python `cryptography`, self-signed root generated at startup into the data
  dir) implements the same interface for development and tests. Both are real
  CAs — there is no "skip verification" mode anywhere.
- **Rotation:** agent certs are short-lived (default 30 days); the agent
  re-submits a CSR over its authenticated connection before expiry
  (`POST /enroll/v1/renew` authenticated by current mTLS cert via the gateway
  in later phases; Phase 0 ships issuance + revocation, with renewal handled
  by re-enrollment). Revocation flips the `Agent` cert record; the gateway
  checks revocation at connect **and** disconnects live connections on
  revocation events.

## 8. Agent gateway & wire protocol

- Endpoint: `wss://gateway:8443/agent`, TLS 1.3, client certificate
  **required** (verified against the internal CA bundle). On connect the
  gateway extracts the peer certificate, matches fingerprint → `Agent` row →
  (`device_id`, `org_id`); unknown/revoked/expired certs are rejected before
  any message is processed.
- The connection parks on a blocking read; the server pushes signed jobs down;
  the agent streams `job_status` / `job_log` / `job_result` / `heartbeat`
  envelopes up (shapes exactly as in `docs/05-agent-spec.md`; Pydantic models
  in `gateway/protocol.py` are the contract, exported as JSON Schema to
  `docs/kernel/wire-protocol/` for the agent to build against).
- **Fan-out:** job dispatch publishes to Redis channel `dispatch:{device_id}`;
  each gateway replica subscribes for the devices it holds. Status/result
  envelopes update the DB and publish to `ui:{org_id}` for the UI WebSocket.
- Heartbeat cadence 60s with connection liveness timeout; `agents.status`
  (online/offline) and `last_seen_at` are maintained by the gateway.
- The gateway never trusts message content for identity: the device identity
  is the certificate binding, and any envelope referencing another device is
  rejected and audited.

## 9. Signed-job engine

- **Create:** `jobs.dispatch`-authorized API call creates a `Job` row
  (queued) for a device or group (group expands to per-device jobs at
  dispatch). Params validated against the job type's registered schema. Job
  types registry in Phase 0: `hello` (echo/no-op proof job) and
  `inventory.collect` (returns a basic hardware/OS fact document).
- **Sign:** the canonical payload is JSON with sorted keys, compact
  separators, UTF-8 — covering `{id, type, device, params, issued_at,
  expires_at, nonce}`. `SigningService.sign(payload) → base64 sig`.
  Production: OpenBao transit Ed25519 key `job-signing` (private key never
  leaves the vault). Dev/test: local Ed25519 key file — **still signs; the
  unsigned path does not exist**. The public key is exported for enrollment.
- **Dispatch:** queued → published to the device's dispatch channel →
  `dispatched` (with `attempts += 1`). If the device is offline, the job stays
  queued; on agent connect the gateway drains queued jobs for that device.
- **Track:** agent envelopes drive `running` → `succeeded`/`failed` with
  `result` jsonb; `job_logs` rows are appended (bounded per job; large output
  goes to object storage as `artifact_ref`).
- **Retry & expiry:** a Celery beat sweep marks past-`expires_at` jobs
  `expired`; jobs that failed from connectivity (never reached the agent) are
  re-dispatched up to `max_attempts` with backoff. **Re-dispatch re-signs with
  a fresh nonce + expiry** — nonces are single-use by design.
- Every dispatch, refusal, and terminal state writes an audit event.

## 10. Audit log

- `audit_events` is append-only: the app role gets `INSERT`/`SELECT` only, and
  a trigger raises on `UPDATE`/`DELETE` as defense in depth.
- Per-org hash chain: `hash = sha256(prev_hash || canonical(event))` where
  `canonical` is the same sorted-key JSON used for job signing, over
  `{org_id, ts, actor_type, actor_id, action, target_type, target_id,
  metadata}`. `prev_hash` for the first event of an org is 64 zero hex chars.
  Appends serialize per org (advisory lock) so the chain never forks.
- **Two ways in, one chain.** `AuditService.record` appends one event and is
  what almost every privileged action calls. `AuditService.record_many` appends
  N under a **single** acquisition of the same lock and a single head read,
  linking the batch in Python from the head it read under that lock. It does
  **not** drop the lock — `pg_advisory_xact_lock` is held to the end of the
  transaction however many times it is taken, so N acquisitions inside one
  transaction buy nothing the first has not already bought. It exists for the
  one caller that produces audited writes in bulk: a `net.sweep` recording every
  host it found (`asset.auto_created`, one per host — 4096 of them measured
  14.7s through `record` and 1.5s through `record_many`). Events written either
  way are indistinguishable to `verify`.
- `AuditService.verify(org_id) → first broken link or OK` and
  `GET /api/v1/audit/verify` (auditor role) re-walk the chain.
- Phase 0 audited actions: login (first-seen upsert), enrollment token
  create/revoke, device enroll, cert revoke, job dispatch/refusal/completion,
  RBAC changes, policy changes, credential metadata create/access.

## 11. Object storage

`core/storage.py` wraps an S3 client (MinIO on-prem) with the bucket layout of
`docs/04-data-model.md` (`<org_id>/job-artifacts/<job_id>/…` etc.), a health
check, and `put/get_job_artifact`. Phase 0 uses it for oversized job output;
later modules reuse the same wrapper.

## 12. Web shell (frontend)

React 18 + TypeScript strict + Vite + Tailwind + shadcn/ui + TanStack
Query/Table. `src/modules/` mirrors backend modules; Phase 0 ships `devices/`
and `jobs/` feature folders plus `lib/` (API client, auth, ws).

- **Login:** OIDC code + PKCE against Keycloak (`oidc-client-ts`); tokens in
  memory, silent renew.
- **Devices:** table (hostname, OS, group, agent status, last seen) with
  live status via the UI WebSocket.
- **Jobs:** dispatch a `hello` / `inventory.collect` job to a device; job list
  with live state transitions and a job detail pane showing logs/result.
- UI WebSocket `/api/v1/ws` authenticates with the bearer token and receives
  the `ui:{org_id}` events.

## 13. Deploy stack

`deploy/docker-compose.yml`: traefik (TLS, HTTP routing + TCP passthrough for
the gateway), `api`, `gateway`, `web`, `postgres:16`, `redis`, `minio`,
`keycloak` (with realm import in `deploy/keycloak/`), `openbao` (with an init
script creating the transit `job-signing` key and the PKI mount in
`deploy/openbao/`), `worker` (Celery). Dockerfiles for backend and frontend.
Secrets/config via `.env` (never committed).

## 14. Testing & verification

Test-first throughout (TDD skill). Backend: pytest + pytest-asyncio + httpx,
against **real PostgreSQL 16** (test harness creates a throwaway DB, runs
Alembic, connects as the non-BYPASSRLS `app_user`). Security-relevant paths get
explicit tests:

- **RLS:** two orgs; org A's session cannot see/insert org B rows even with
  raw SQL missing the org filter; the pg_policies completeness test (§5).
- **Signing:** roundtrip verify; tampered payload, wrong device, expired,
  replayed nonce all rejected (verification logic mirrored in tests exactly as
  the agent will implement it).
- **Audit:** chain verifies; tampering any historical row breaks verification
  at that link; update/delete raise.
- **RBAC:** each privileged route returns 403 without the permission; role
  seeds grant expected access; device-group-scoped technician access.
- **Enrollment:** happy path issues a cert binding device identity; expired/
  revoked/replayed tokens rejected; audit events written.
- **Gateway:** integration test with a fake agent over real mTLS (test CA):
  connect, receive signed job, send status/result; wrong/revoked cert
  refused; envelope for another device refused.
- **Job engine:** lifecycle transitions, offline queue + drain on connect,
  expiry sweep, re-sign on retry.

Frontend: `tsc --noEmit` strict, ESLint, Vitest smoke tests for the API client
and job-state rendering, production build passes. Compose file:
`docker compose config` validation (no daemon in the build environment; the
full stack is exercised at deployment).

**Definition of done for this repo:** all of the above green, plus a scripted
end-to-end demo (`scripts/demo_e2e.py`) that — against a locally running API +
gateway with the dev CA and local signer — enrolls a fake agent, dispatches a
signed `hello` job from the API, and shows the verified result and intact
audit chain. The roadmap's three-OS exit criterion additionally requires the
skeleton agent under `agent/` (next cycle, still unbuilt).
