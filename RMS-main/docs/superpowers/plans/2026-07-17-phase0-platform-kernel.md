# Phase 0 — Platform Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the control-plane kernel: tenancy/RLS data core, Keycloak-validated auth + RBAC, hash-chained audit, agent enrollment/PKI, Ed25519 signed-job engine, mTLS agent gateway, object-storage wiring, minimal web shell, and the on-prem compose stack — per `docs/kernel/spec.md`.

**Architecture:** One backend codebase, two ASGI apps (operator API + agent gateway) sharing `core/` services; PostgreSQL 16 with forced RLS as the tenant backstop; Redis pub/sub bridging job dispatch and UI live status; OpenBao (prod) or local implementations (dev/test) behind `SigningService`/`CAService` interfaces — both always sign/verify, no bypass path.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async, asyncpg), Alembic, Pydantic v2, Celery+Redis, `cryptography`; React 18 + TS strict + Vite + Tailwind + shadcn/ui + TanStack Query/Table; PostgreSQL 16, Redis, MinIO, Keycloak, OpenBao, Traefik.

## Global Constraints

- Python **3.12**; `uv` for env/deps (`uv 0.8.17` available); type hints mandatory, `mypy --strict`-clean for `app/` (pragmatic per-module relaxations allowed only in `pyproject.toml`, never inline).
- Frontend TypeScript **strict**, no `any`.
- Security invariants of `CLAUDE.md`/`docs/03-security-model.md` are non-negotiable: no unsigned job path, RLS on every `org_id` table (`ENABLE` + `FORCE`), append-only audit, server-side RBAC on every privileged endpoint, secrets never logged/returned.
- UUIDv7 PKs (`uuid_extensions.uuid7`); `created_at`/`updated_at` on mutable entities; jsonb for evolving detail.
- Tests run against **real PostgreSQL 16** as non-BYPASSRLS role `app_user`; local cluster `pg_ctlcluster 16 main start` in this environment.
- Alembic migrations forward-only; never edit a shipped migration.
- Async throughout the backend; business logic in services, thin route handlers.
- Commit after every green task; conventional-commit style messages.
- Canonical JSON everywhere a hash/signature is computed: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()`.
- Wire protocol envelopes exactly as `docs/05-agent-spec.md` (`v: 1`).

---

### Task 1: Backend scaffold + test harness

**Files:**
- Create: `backend/pyproject.toml`, `backend/app/__init__.py`, `backend/app/core/config.py`, `backend/tests/conftest.py`, `backend/tests/test_config.py`, `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/script.py.mako`, `backend/.python-version`
- Modify: root `.gitignore` (add `.venv/`, `__pycache__/`, `.env`, `*.egg-info`, `.pytest_cache/`, `.mypy_cache/`, `node_modules/`, `dist/`)

**Interfaces:**
- Produces: `app.core.config.Settings` (pydantic-settings, env prefix `ITS_`) with fields: `database_url: str`, `database_admin_url: str`, `redis_url: str = "redis://localhost:6379/0"`, `oidc_issuer: str`, `oidc_audience: str = "it-suite"`, `oidc_jwks_url: str`, `signing_backend: Literal["local","openbao"] = "local"`, `signing_key_path: Path | None`, `ca_backend: Literal["local","openbao"] = "local"`, `ca_dir: Path`, `openbao_addr: str | None`, `openbao_token: str | None`, `s3_endpoint: str | None`, `s3_access_key/s3_secret_key: str | None`, `s3_bucket: str = "it-suite"`, `gateway_heartbeat_timeout_s: int = 180`, `job_default_ttl_s: int = 3600`, `job_max_attempts: int = 3`; `get_settings()` cached accessor + `override_settings()` test helper.
- Produces: pytest fixtures `pg_cluster` (session-scoped: ensures local PG16 up, creates throwaway DB + `app_user` non-BYPASSRLS role, yields DSNs, drops on teardown), `db_engine`, `db_session`.

**Steps:**
- [ ] Write `pyproject.toml`: project `it-suite-backend`, requires-python `>=3.12`; deps: `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]>=2.0`, `asyncpg`, `alembic`, `pydantic>=2`, `pydantic-settings`, `uuid7` (or `uuid-extensions`), `cryptography`, `httpx`, `redis>=5`, `celery`, `python-jose[cryptography]` **no** — use `PyJWT[crypto]`, `boto3`; dev deps: `pytest`, `pytest-asyncio`, `mypy`, `ruff`, `moto[s3]`, `websockets`. Tool config: `ruff` (line 100), `mypy` strict, `pytest` asyncio_mode auto.
- [ ] `uv venv --python 3.12 && uv pip install -e ".[dev]"` inside `backend/`.
- [ ] Write failing `tests/test_config.py`: `Settings` loads from env (`ITS_DATABASE_URL=...`), defaults hold, `override_settings` works.
- [ ] Run `pytest tests/test_config.py -v` → FAIL (module missing).
- [ ] Implement `core/config.py`; run → PASS.
- [ ] Write `tests/conftest.py` Postgres harness (start cluster if down via `sudo pg_ctlcluster 16 main start` fallback `pg_ctlcluster`, `CREATE DATABASE its_test_<rand>`, `CREATE ROLE app_user LOGIN PASSWORD 'app' NOSUPERUSER NOBYPASSRLS`, grant connect) + `alembic upgrade head` hook (no-op until Task 3).
- [ ] Add sanity test `test_pg_harness.py::test_app_user_cannot_bypass_rls` asserting `rolbypassrls = false`; run → PASS.
- [ ] Commit `feat(backend): scaffold, settings, and Postgres test harness`.

### Task 2: Canonical JSON

**Files:**
- Create: `backend/app/jobs/__init__.py`, `backend/app/jobs/canonical.py`, `backend/tests/test_canonical.py`

**Interfaces:**
- Produces: `canonical_bytes(obj: Mapping[str, Any]) -> bytes` — sorted keys, compact separators, `ensure_ascii=False`, UTF-8; rejects non-JSON-serializable values by raising `TypeError`; datetimes must already be RFC3339 strings (raise `TypeError` on `datetime` input to force explicit formatting); `rfc3339(dt: datetime) -> str` (UTC, `Z` suffix, second precision).

**Steps:**
- [ ] Failing tests: key order independence (`canonical_bytes({"b":1,"a":2}) == canonical_bytes({"a":2,"b":1})`), compact output `b'{"a":2,"b":1}'`, nested dicts sorted, unicode preserved not escaped, `datetime` input raises `TypeError`, `rfc3339(datetime(2026,1,1,tzinfo=UTC)) == "2026-01-01T00:00:00Z"`.
- [ ] Run → FAIL; implement; run → PASS; commit `feat(jobs): canonical JSON serialization for signing and hashing`.

### Task 3: Core models + initial migration

**Files:**
- Create: `backend/app/models/__init__.py`, `backend/app/models/base.py`, `backend/app/models/core.py`, `backend/migrations/versions/0001_core_schema.py`, `backend/tests/test_migrations.py`
- Modify: `backend/migrations/env.py` (async engine, target_metadata)

**Interfaces:**
- Produces: SQLAlchemy 2 typed models exactly per `docs/04-data-model.md` §Core entities plus `EnrollmentToken` per spec §4: `Organization`, `User`, `Role`, `Permission`, `RolePermission`, `UserRole`, `Device`, `DeviceGroup`, `DeviceTag`, `Agent`, `Policy`, `Credential`, `Job`, `JobLog`, `AuditEvent`, `EnrollmentToken`. `base.py`: `Base(DeclarativeBase)`, `uuid7_pk()` column factory, `TimestampMixin`. `Job.status` enum: `queued|dispatched|running|succeeded|failed|expired|refused`. `AuditEvent` columns include `prev_hash: str(64)`, `hash: str(64)`, no `updated_at`.
- Produces: migration `0001` creating all tables + indexes (`devices(org_id, hostname)`, `jobs(org_id, status)`, `jobs(target_id)`, `audit_events(org_id, id)`, `agents(cert_fingerprint) unique`, `enrollment_tokens(token_hash) unique`).

**Steps:**
- [ ] Failing `test_migrations.py::test_upgrade_head_creates_core_tables` (inspect `information_schema.tables` for all 16 names) and `::test_job_status_enum_values`.
- [ ] Implement models + autogen-then-hand-check migration; run → PASS.
- [ ] Wire conftest to run `alembic upgrade head` on the throwaway DB (as admin role), grant `app_user` table privileges (`SELECT/INSERT/UPDATE/DELETE` on all, then revoke `UPDATE/DELETE` on `audit_events` in Task 6).
- [ ] Commit `feat(db): core data model and initial migration`.

### Task 4: RLS + tenant context

**Files:**
- Create: `backend/migrations/versions/0002_rls.py`, `backend/app/core/db.py`, `backend/app/core/tenancy.py`, `backend/tests/test_rls.py`

**Interfaces:**
- Produces: `db.py`: `get_engine()`, `session_factory()` (app_user DSN). `tenancy.py`: `@asynccontextmanager async def org_session(org_id: UUID) -> AsyncIterator[AsyncSession]` — opens txn, `SET LOCAL app.current_org = :org` (via `set_config($1,$2,true)`), yields, commits/rolls back; `async def admin_session()` for explicitly cross-org paths (separate factory using admin DSN; callers must audit).
- Produces: migration `0002`: for every table with `org_id` — `ALTER TABLE … ENABLE ROW LEVEL SECURITY; ALTER TABLE … FORCE ROW LEVEL SECURITY; CREATE POLICY tenant_isolation ON … USING (org_id = current_setting('app.current_org')::uuid) WITH CHECK (org_id = current_setting('app.current_org')::uuid);` `organizations` policy: `USING (id = current_setting('app.current_org')::uuid)`. `roles`/`permissions`: `permissions` is global (no RLS); `roles` policy `USING (org_id IS NULL OR org_id = current_setting('app.current_org')::uuid) WITH CHECK (org_id = current_setting('app.current_org')::uuid)`.

**Steps:**
- [ ] Failing tests: (a) org A session sees only A's devices even via raw `text("SELECT * FROM devices")`; (b) insert with wrong `org_id` raises; (c) no-context session sees zero rows and cannot insert (current_setting errors → wrap: policy uses `current_setting('app.current_org', true)` returning NULL → no rows); (d) **completeness**: query `information_schema.columns` for tables having `org_id`, assert each is in `pg_policies` with `forcerowsecurity = true` in `pg_class`.
- [ ] Implement migration + tenancy; run → PASS. Commit `feat(core): row-level security and tenant context sessions`.

### Task 5: AuthN (JWT/JWKS) + RBAC

**Files:**
- Create: `backend/app/core/security.py`, `backend/app/core/rbac.py`, `backend/migrations/versions/0003_rbac_seed.py`, `backend/tests/fake_idp.py`, `backend/tests/test_security.py`, `backend/tests/test_rbac.py`

**Interfaces:**
- Produces: `security.py`: `TokenClaims` (sub, email, name, org_slug), `validate_token(token: str) -> TokenClaims` (PyJWT + JWKS cache keyed by kid, verifies iss/aud/exp/sig), FastAPI dependency `current_user(...) -> User` (upserts projection row inside `org_session`, updates `last_login_at`, writes `auth.login` audit event on first-seen — audit call lands in Task 6, stub interface now).
- Produces: `rbac.py`: `PERMISSIONS: dict[str, str]` (the 10 codes from spec §6), `require_permission(code: str)` returning a dependency that 403s unless the current user's roles grant `code` in the current org; honors `UserRole.device_group_id` scoping via optional `device_group_id` comparator argument.
- Produces: `tests/fake_idp.py`: `FakeIdp` — local RSA key, `.token(sub, org_slug, roles=None, aud="it-suite", exp_in=300)`, `.jwks()` dict; fixture `fake_idp` wires `Settings.oidc_jwks_url` to a served JWKS (httpx MockTransport).
- Produces: migration `0003` seeds global roles (`super-admin`, `org-admin`, `technician`, `auditor`, `read-only`), all permissions, and role→permission grants: org-admin=all in-org; technician=`devices.read`, `jobs.read`, `jobs.dispatch`; auditor=`devices.read`, `jobs.read`, `audit.read`, `credentials.read`; read-only=`devices.read`, `jobs.read`.

**Steps:**
- [ ] Failing `test_security.py`: valid token → claims; expired/wrong-aud/wrong-iss/garbage → `401 InvalidTokenError`; kid rotation refetches JWKS.
- [ ] Implement; PASS.
- [ ] Failing `test_rbac.py`: technician passes `jobs.dispatch`, fails `enrollment.manage` (403); auditor passes `audit.read`; unassigned user 403 everywhere; group-scoped role limits to its device group.
- [ ] Implement + seed migration; PASS. Commit `feat(core): OIDC token validation and server-side RBAC`.

### Task 6: Audit log (hash chain, append-only)

**Files:**
- Create: `backend/app/core/audit.py`, `backend/migrations/versions/0004_audit_hardening.py`, `backend/tests/test_audit.py`

**Interfaces:**
- Produces: `AuditService` with `async def record(session, *, org_id, actor_type: Literal["user","agent","system"], actor_id, action: str, target_type: str, target_id, metadata: dict) -> AuditEvent` — takes `pg_advisory_xact_lock(hashtext('audit:'||org_id))`, reads last event's `hash` for org, computes `hash = sha256(prev_hash_bytes_hex + canonical_bytes(event_fields)).hexdigest()` with genesis `"0"*64`; `async def verify(org_id) -> VerifyResult` walking the chain in id order returning `ok: bool, broken_at: UUID | None`.
- Produces: migration `0004`: trigger `audit_events_immutable` raising exception on UPDATE/DELETE; `REVOKE UPDATE, DELETE ON audit_events FROM app_user`.

**Steps:**
- [ ] Failing tests: two appends chain (`e2.prev_hash == e1.hash`); `verify` ok; direct SQL tamper of `e1.metadata` (via admin session) → `verify` reports broken at e2 or e1; `UPDATE`/`DELETE` as app_user raise; concurrent appends (10 tasks) yield a linear unbroken chain.
- [ ] Implement; PASS. Wire `current_user` first-seen upsert to `record(action="auth.login")`. Commit `feat(core): immutable hash-chained audit log`.

### Task 7: SigningService

**Files:**
- Create: `backend/app/core/signing.py`, `backend/tests/test_signing.py`

**Interfaces:**
- Produces: `class SigningService(Protocol)`: `async def sign(self, payload: bytes) -> str` (base64), `async def public_key_pem(self) -> str`. `LocalEd25519Signer(key_path)` — generates key file (0600) if absent. `OpenBaoTransitSigner(addr, token, key_name="job-signing")` — POSTs `/v1/transit/sign/job-signing` (input base64), parses `vault:v1:` prefix; pubkey from `/v1/transit/keys/job-signing`. `verify_signature(pub_pem, payload, sig_b64) -> bool` helper (mirrors agent-side check; used by tests and the demo agent).
- Produces: `get_signer(settings) -> SigningService` factory. **There is no None/noop signer.**

**Steps:**
- [ ] Failing tests: local sign→verify roundtrip; tampered payload fails; wrong key fails; key file persisted and reloaded stable; OpenBao impl against `httpx.MockTransport` (sign + pubkey parse); factory returns per config.
- [ ] Implement; PASS. Commit `feat(core): Ed25519 job signing service (local + OpenBao transit)`.

### Task 8: CAService

**Files:**
- Create: `backend/app/core/ca.py`, `backend/tests/test_ca.py`

**Interfaces:**
- Produces: `class CAService(Protocol)`: `async def sign_csr(self, csr_pem: str, *, device_id: UUID, org_id: UUID, ttl_days: int = 30) -> IssuedCert` (`cert_pem, chain_pem, serial: str, fingerprint_sha256: str, not_after: datetime`), `async def ca_bundle_pem(self) -> str`, `async def revoke(self, serial: str) -> None`. `LocalCA(ca_dir)` — self-signed root (10y) created on first use, issues certs with `CN=<device_id>`, `O=<org_id>`, SAN URI `its://org/<org_id>/device/<device_id>`; tracks revoked serials in `ca_dir/revoked.json`; `is_revoked(serial)`. `OpenBaoPkiCA` — `/v1/pki/sign/agent` role, mocked in tests.
- Produces: `fingerprint_sha256(cert_pem) -> str` module function (hex, lowercase) — the gateway and enrollment share it.

**Steps:**
- [ ] Failing tests: CSR (generated with `cryptography`) signed → cert validates against bundle, CN/O/SAN correct, ttl honored; fingerprint stable; revoke → `is_revoked`; OpenBao impl parses mocked response.
- [ ] Implement; PASS. Commit `feat(core): internal CA service (local dev CA + OpenBao PKI)`.

### Task 9: Enrollment API

**Files:**
- Create: `backend/app/api/__init__.py`, `backend/app/api/v1/__init__.py`, `backend/app/api/v1/enrollment.py`, `backend/app/services/enrollment.py`, `backend/app/main.py`, `backend/tests/test_enrollment.py`

**Interfaces:**
- Consumes: `CAService`, `AuditService`, `require_permission("enrollment.manage")`, `SigningService.public_key_pem`.
- Produces: `app.main.create_app() -> FastAPI` (API app; routers mount under `/api/v1`, enrollment under `/enroll/v1`). Endpoints: `POST /api/v1/enrollment-tokens` (create; returns plaintext `<id>.<secret>` once; stores `sha256(secret)`), `POST /api/v1/enrollment-tokens/{id}/revoke`, `GET /api/v1/enrollment-tokens` (no secrets). `POST /enroll/v1` body `{token: str, csr_pem: str, device_facts: {hostname, os_family, os_version, architecture, serial?}}` → 201 `{device_id, cert_pem, chain_pem, ca_bundle_pem, gateway_url, job_signing_public_key_pem}`; 403 on bad/expired/revoked/exhausted token. Service `enroll(...)` creates Device+Agent (fingerprint, serial, expiry), increments `use_count`, audits `enrollment.device_enrolled`.

**Steps:**
- [ ] Failing httpx tests: token create requires permission (403 for technician); enroll happy path returns cert whose CN == returned device_id and response carries signing pubkey; second use over `max_uses=1` → 403; expired → 403; revoked → 403; audit events exist for issue + enroll.
- [ ] Implement; PASS. Commit `feat(api): enrollment tokens and agent enrollment endpoint`.

### Task 10: Job engine + registry

**Files:**
- Create: `backend/app/jobs/registry.py`, `backend/app/jobs/engine.py`, `backend/tests/test_job_engine.py`

**Interfaces:**
- Consumes: `canonical_bytes`, `rfc3339`, `SigningService`, `AuditService`.
- Produces: `registry.py`: `JobType(name, params_model: type[BaseModel])`; `register(job_type)`, `get(name)`; Phase 0 registrations `hello` (`HelloParams(message: str = "ping")`) and `inventory.collect` (`InventoryCollectParams()`).
- Produces: `engine.py` `JobEngine`: `async def create(session, *, org_id, type: str, target_device_id: UUID, params: dict, requested_by: UUID, ttl_s: int | None) -> Job` (validates params via registry, status `queued`, `nonce=str(uuid4())`, `issued_at=now`, `expires_at=now+ttl`); `def signed_envelope(job) -> dict` returns the spec §8/`05-agent-spec` `{"v":1,"kind":"job","job":{...},"sig":...}` — **signature over `canonical_bytes({id,type,device,params,issued_at,expires_at,nonce})`**; `async def mark_dispatched/mark_running/complete(job, state, result)/refuse(job, reason)`; `async def expire_due(session) -> int`; `async def redispatchable(session) -> list[Job]` (failed-connectivity under max_attempts) and `async def resign_for_retry(job)` (fresh nonce + expiry, attempts+1).
- Produces: envelope schemas live here for signing; gateway reuses them (Task 11).

**Steps:**
- [ ] Failing tests: create validates params (unknown type / bad params raise); envelope verifies with `verify_signature` and pinned pubkey; tamper any covered field → verify fails; lifecycle transitions enforce legal order (running before dispatched raises `InvalidTransition`); `expire_due` flips overdue queued/dispatched to `expired`; `resign_for_retry` changes nonce+expiry and re-verifies; dispatch + completion write audit events.
- [ ] Implement; PASS. Commit `feat(jobs): signed-job engine and job-type registry`.

### Task 11: Gateway protocol + dispatch bridge

**Files:**
- Create: `backend/app/gateway/__init__.py`, `backend/app/gateway/protocol.py`, `backend/app/gateway/connections.py`, `backend/app/gateway/dispatch.py`, `backend/tests/test_gateway_protocol.py`, `backend/tests/test_dispatch.py`
- Create: `scripts/export_wire_schema.py`, `docs/kernel/wire-protocol/README.md` (+ generated `*.schema.json`)

**Interfaces:**
- Consumes: `JobEngine.signed_envelope`, Redis (`redis.asyncio`).
- Produces: `protocol.py` Pydantic models: `JobEnvelope`, `JobStatusMsg`, `JobLogMsg`, `JobResultMsg`, `HeartbeatMsg`, discriminated union `AgentMessage` on `kind`; all with `v: Literal[1]`.
- Produces: `connections.py`: `ConnectionRegistry` — `add(device_id, ws)`, `remove`, `get`, `devices()`; single instance per gateway process.
- Produces: `dispatch.py`: `async def publish_job(redis, device_id, envelope: dict)` → channel `dispatch:{device_id}`; `async def publish_ui_event(redis, org_id, event: dict)` → `ui:{org_id}`; `DispatchSubscriber(registry, redis)` background task subscribing `dispatch:*` (psubscribe) and forwarding to local connections; `async def handle_agent_message(session_factory, msg: AgentMessage, *, device_id, org_id)` — updates job state via `JobEngine`, appends `job_logs`, publishes UI events; **rejects and audits any message whose `job.device`/job row org doesn't match the bound identity**.
- Produces: JSON Schemas exported to `docs/kernel/wire-protocol/` for the agent to build against. (The agent was a separate repository when this plan was written; `CLAUDE.md` `a32cead` moved it to `agent/` in-tree on 2026-08-05. The export is unchanged and is still a versioned compatibility contract.)

**Steps:**
- [ ] Failing protocol tests: parse each envelope kind; unknown kind rejected; wrong `v` rejected.
- [ ] Failing dispatch tests (real local Redis, random DB index): publish→subscriber delivers to fake ws (recorded messages); `handle_agent_message` for a result flips job to `succeeded` and publishes UI event; mismatched device identity → job refused + `gateway.identity_mismatch` audit event.
- [ ] Implement; PASS; run schema export script and commit outputs. Commit `feat(gateway): wire protocol, connection registry, and dispatch bridge`.

### Task 12: Gateway ASGI app with mTLS

**Files:**
- Create: `backend/app/gateway/main.py`, `backend/tests/test_gateway_mtls.py`

**Interfaces:**
- Consumes: `ConnectionRegistry`, `DispatchSubscriber`, `handle_agent_message`, `fingerprint_sha256`, `LocalCA` (test certs), `JobEngine` (queued-job drain).
- Produces: `create_gateway_app() -> FastAPI` with `WS /agent`: on connect, pull peer cert (uvicorn `ssl_cert_reqs=CERT_REQUIRED`; access via `websocket.scope["transport"]`/`ssl_object.getpeercert(binary_form=True)`), fingerprint → `Agent` lookup (admin path is fine: cert IS the tenant proof; then bind `org_id`), reject unknown/revoked/expired (close code 4401, audit `gateway.rejected`); on accept: register, mark agent online (audit `gateway.connected`), drain queued jobs for the device, then loop reading `AgentMessage`s → `handle_agent_message`; heartbeat timeout closes; disconnect marks offline + `last_seen_at`.
- Produces: `run_gateway()` entrypoint wiring uvicorn with `ssl_keyfile/certfile/ca_certs/cert_reqs` from settings.

**Steps:**
- [ ] Failing integration test: start gateway via `uvicorn.Server` in-process on localhost TLS with LocalCA-issued server cert; fake agent = `websockets.connect` with client cert for an enrolled device → receives queued signed job envelope, replies status/result → job `succeeded` in DB; connect with un-enrolled cert → closed 4401; revoked cert → closed 4401.
- [ ] Implement; PASS. Commit `feat(gateway): mTLS agent gateway with cert-bound identity`.

### Task 13: Operator API routes + UI WebSocket

**Files:**
- Create: `backend/app/api/v1/devices.py`, `backend/app/api/v1/jobs.py`, `backend/app/api/v1/audit.py`, `backend/app/api/v1/me.py`, `backend/app/api/v1/ws.py`, `backend/tests/test_api_devices.py`, `backend/tests/test_api_jobs.py`, `backend/tests/test_api_audit.py`, `backend/tests/test_ui_ws.py`
- Modify: `backend/app/main.py` (mount routers)

**Interfaces:**
- Consumes: `require_permission`, `org_session`, `JobEngine`, `publish_job`, `AuditService.verify`.
- Produces: `GET /api/v1/me` (user + permissions list, for UI gating); `GET /api/v1/devices` (`devices.read`; paged, filter by os_family/status/group); `GET /api/v1/devices/{id}`; `POST /api/v1/jobs` (`jobs.dispatch`; `{type, device_id, params}` → creates+signs+publishes, 201 job); `GET /api/v1/jobs` + `GET /api/v1/jobs/{id}` (+ `?include=logs`); `GET /api/v1/audit` (`audit.read`; paged) + `GET /api/v1/audit/verify`; `WS /api/v1/ws?token=` — validates JWT, subscribes `ui:{org_id}`, forwards events.
- Produces: Pydantic response models in each router module; **`Credential.vault_ref` and any secret material never serialized** (no credential routes in Phase 0 beyond model existence — omit routes entirely).

**Steps:**
- [ ] Failing tests per router: RBAC (403 without permission), happy paths, job dispatch end-to-end to a subscribed fake (Redis message observed), audit verify endpoint returns ok, UI WS receives the job-status event on dispatch, unauthenticated WS refused.
- [ ] Implement; PASS. Commit `feat(api): devices, jobs, audit routes and UI websocket`.

### Task 14: Storage wrapper + worker

**Files:**
- Create: `backend/app/core/storage.py`, `backend/app/worker.py`, `backend/tests/test_storage.py`, `backend/tests/test_worker.py`

**Interfaces:**
- Produces: `storage.py`: `ObjectStore(settings)` — `health() -> bool`, `put_job_artifact(org_id, job_id, name, data: bytes) -> str` (returns `s3://bucket/<org_id>/job-artifacts/<job_id>/<name>`), `get_job_artifact(ref) -> bytes`; boto3 in thread executor.
- Produces: `worker.py`: Celery app `its`; beat task `sweep_expired_jobs` (every 60s) calling `JobEngine.expire_due` + re-sign/redispatch loop for `redispatchable()` publishing via `publish_job`. Task functions are thin wrappers over engine methods so tests hit the logic directly.

**Steps:**
- [ ] Failing tests: storage against `moto` (put/get roundtrip, ref format, health); `sweep_expired_jobs` (called directly, no broker) expires an overdue job and republishes a connectivity-failed one with a fresh nonce.
- [ ] Implement; PASS. Commit `feat(core): object storage wrapper and expiry/retry worker`.

### Task 15: Frontend shell

**Files:**
- Create: `frontend/` via Vite react-ts template; `frontend/src/lib/api.ts`, `frontend/src/lib/auth.ts` (oidc-client-ts wrapper), `frontend/src/lib/ws.ts`, `frontend/src/modules/devices/DevicesPage.tsx`, `frontend/src/modules/jobs/JobsPage.tsx`, `frontend/src/modules/jobs/DispatchJobDialog.tsx`, `frontend/src/App.tsx` (router + auth guard + layout), shadcn/ui setup (`components.json`, `src/components/ui/*` for button, table, dialog, badge, card, input, select), Tailwind config; `frontend/src/lib/api.test.ts`, `frontend/src/modules/jobs/jobState.test.ts`
- Modify: root `.gitignore` already covers `node_modules/`.

**Interfaces:**
- Consumes: API shapes from Task 13 (mirror as TS types in `src/lib/api.ts`: `Device`, `Job`, `JobStatus = "queued"|"dispatched"|"running"|"succeeded"|"failed"|"expired"|"refused"`, `Me`).
- Produces: routes `/login`, `/devices`, `/jobs`; TanStack Query for lists; WS hook `useLiveEvents(onEvent)` updating query caches; job dispatch dialog (device select, type select hello/inventory.collect, params); status badges per state.

**Steps:**
- [ ] Scaffold: `npm create vite@latest frontend -- --template react-ts`; add `@tanstack/react-query`, `@tanstack/react-table`, `oidc-client-ts`, `react-router-dom`, Tailwind v4, shadcn init; strict tsconfig (`"strict": true`, `noUncheckedIndexedAccess`).
- [ ] Failing Vitest tests: `api.ts` builds authed requests + maps 401 to sign-in redirect callback; job state → badge variant mapping total over all 7 states.
- [ ] Implement pages/components; `npm test`, `tsc --noEmit`, `npm run build` all green.
- [ ] Commit `feat(frontend): web shell — login, devices, jobs with live status`.

### Task 16: Deploy stack

**Files:**
- Create: `deploy/docker-compose.yml`, `deploy/backend.Dockerfile`, `deploy/frontend.Dockerfile`, `deploy/traefik/traefik.yml`, `deploy/keycloak/realm-it-suite.json`, `deploy/openbao/init.sh`, `deploy/.env.example`, `deploy/README.md`

**Interfaces:**
- Consumes: env names from `Settings` (`ITS_*`).
- Produces: services `traefik` (websecure entrypoint + TCP passthrough router `gateway` on 8443), `api`, `gateway`, `worker`, `web`, `postgres:16`, `redis:7`, `minio`, `keycloak:26` (realm import: realm `it-suite`, SPA client `it-suite-web` public+PKCE, audience mapper), `openbao` (+ `openbao-init` one-shot: enable transit, create ed25519 `job-signing`, enable pki, role `agent`); healthchecks; volumes; no secrets in the file (`.env`).

**Steps:**
- [ ] Write everything; validate with `docker compose -f deploy/docker-compose.yml config -q` (daemon-less validation) — expect exit 0.
- [ ] `deploy/README.md`: first-boot runbook (init bao, keycloak admin, create org + token, install agent).
- [ ] Commit `feat(deploy): on-prem docker-compose stack`.

### Task 17: End-to-end demo + full verification

**Files:**
- Create: `scripts/demo_e2e.py`, `backend/tests/test_e2e_kernel.py`

**Interfaces:**
- Consumes: everything.
- Produces: `test_e2e_kernel.py` — the kernel exit test, fully in-process where possible: create org → seed admin user via fake IdP → issue enrollment token → generate keypair+CSR, enroll → start gateway (TLS) → connect fake agent with issued cert, **agent verifies the job signature against the pinned pubkey exactly as `docs/05-agent-spec.md` §Signed-job runner (signature, device match, freshness, nonce)** → dispatch `hello` via API → agent runs, returns result → API shows `succeeded` → audit chain verifies. `scripts/demo_e2e.py` = same flow against a running dev stack, printing each step.

**Steps:**
- [ ] Write the failing e2e test; implement glue until PASS.
- [ ] Full gate: `pytest` (all), `mypy app`, `ruff check`, frontend `tsc --noEmit && npm test && npm run build`, `docker compose config -q`.
- [ ] Update root `README.md` (repo layout, dev quickstart, test commands).
- [ ] Commit `feat(kernel): end-to-end signed-job demo and verification`.

---

## Self-review

- **Spec coverage:** §3 scaffold→T1; §4 models→T3; §5 RLS→T4; §6 auth/RBAC→T5; §10 audit→T6; §9 signing→T7,T10; §7 enrollment/PKI→T8,T9; §8 gateway/protocol→T11,T12; API/UI WS→T13; §11 storage + Celery→T14; §12 web shell→T15; §13 deploy→T16; §14 definition of done→T17. Gap check: credential routes intentionally omitted (spec lists `credentials.*` permissions only — model + permissions seeded, routes deferred to Phase 3). OK.
- **Type consistency:** `IssuedCert.fingerprint_sha256` (T8) = `Agent.cert_fingerprint` (T3) consumer in T9/T12; `JobStatus` enum identical in T3/T10/T15; envelope field names identical in T10/T11/T17.
- **Placeholders:** none of the banned patterns; each task carries concrete test intent, exact routes/signatures, and commands.
