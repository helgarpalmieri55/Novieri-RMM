# Provisioning & Imaging (Phase 7) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the control-plane slice of Provisioning & Imaging — a registry of OS images/profiles, deterministic answer-file rendering with vault-sealed secrets, the real `POST /enroll/v1` agent auto-enrollment handoff, a signed `provision.enroll` post-config job, reporting, API, worker, and frontend — with the site-server PXE/WoL/cache pinned as a contract, not built.

**Architecture:** A new `provisioning` business module registering into the kernel exactly like `backup`/`deploy` (models, migration, dispatch, handler, api, reporting, worker registration, frontend folder). It reuses the existing enrollment/PKI subsystem (`app/services/enrollment.py`, `POST /enroll/v1`, `CAService`) for auto-enrollment, the vault (`app/core/vault.py`) for answer-file secrets, object storage (`app/core/storage.py`) for image + answer artifacts, and the job engine for the `provision.enroll` job. `provision.prepare` (site-server-directed) is a signed contract envelope only.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async, asyncpg), Alembic, Pydantic v2, Celery+Redis; React+TypeScript+Vite+vitest frontend. venv at `backend/.venv` (run tools as `.venv/bin/…`).

## Global Constraints

Every task's requirements implicitly include this section. Copy exact values verbatim.

- **Security invariant #1 — signed jobs only.** `provision.enroll` dispatches exclusively via the kernel job engine (`job_engine.create` + `signed_envelope` + `publish_job`). The `provision.prepare` site-server envelope is Ed25519-signed via the same signer. No unsigned/direct-execute path.
- **Security invariant #2 — outbound-only mTLS.** Auto-enrollment reuses the real `POST /enroll/v1` mTLS flow. No inbound path to endpoints is opened.
- **Security invariant #3 — secrets (THE CRUX, §7).** Domain-join credentials and the local-admin password are **vault-sealed** (`app/core/vault.py` `seal_bytes`/`open_bytes`, AAD-bound to `(org_id, profile_id)`); `provision_profile.post_config` stores only vault-sealed references, NEVER plaintext. Secrets are decrypted **only inside the renderer at render time**, injected into the answer-file bytes, and NEVER stored in the DB in plaintext, returned in any API response, written to a log line, or placed in audit metadata. Every secret path ships an explicit **secret-absence** test.
- **Security invariant #4 — audit.** Every privileged action writes the hash-chained audit log: image/profile create-edit, run start, token issuance, answer-file render, `provision.prepare` intent, `provision.enroll` result. Operator actions carry the real `actor_id` with `actor_type="user"`; worker/result-handler actions use `actor_type="system"`, `actor_id=None`. `actor_type` is `Literal["user","agent","system"]`.
- **Security invariant #5 — tenancy.** EVERY `select()`/write over `os_image`/`provision_profile`/`provision_run`/`Device`/`EnrollmentToken` carries an explicit `org_id` predicate in addition to RLS. RLS is `ENABLE + FORCE` + a `tenant_isolation` policy `USING (org_id = current_setting('app.current_org', true)::uuid)` on all three new tables. Artifacts live under the per-org prefix `{org_id}/provision/…`. A missing tenant filter is a security bug.
- **Security invariant #6 — RBAC.** Server-side `Depends(require_permission("<code>"))` on every route; server-side permission check on every dispatch. Frontend gating is cosmetic. Permissions: `provisioning.read`, `provisioning.manage`, `provisioning.run`. Grants: super/org-admin all; technician read+run; auditor/read-only read.
- **Publish AFTER commit.** Dispatch functions build+sign jobs and accumulate `(job_id, device_id, envelope)` triples; the CALLER publishes to Redis only AFTER its `org_session` transaction commits (mirror `app/modules/backup/api.py`).
- **Actor threading.** Dispatch/service functions take `actor_id: UUID | None = None, actor_type: Literal["user","agent","system"] = "system"`; API routes pass `actor_id=ctx.user_id, actor_type="user"`; worker sweeps keep the system default.
- **Job-type registration in ALL THREE processes.** `register(app)` (app via `register_all`), the gateway (`app/gateway/main.py`), AND the worker (`register_worker_job_types()` in `app/worker.py`). Ship the un-masking worker test: pop the job type from `app.jobs.registry._registry`, assert `UnknownJobType`, then drive the worker bootstrap and assert it resolves.
- **Out-of-domain result coercion.** A `provision.enroll` result with an out-of-domain state (bypassing wire validation) coerces the run to `failed` — never crash/wedge the handler.
- **State vocabularies (verbatim).** `provision_run.state` ∈ {`pending`,`awaiting_enrollment`,`enrolled`,`complete`,`failed`}. `os_image.kind` ∈ {`image`,`build`,`enrollment`}. `os_image.state` ∈ {`active`,`disabled`}. `os_family` ∈ {`windows`,`linux`,`macos`}. Per-OS kind constraint: windows→image, linux→build, macos→enrollment.
- **Reuse, do not fork.** Enrollment token issuance via `app.services.enrollment.create_token(session, *, org_id, created_by, name, expires_in_hours, max_uses) -> IssuedToken` (`.id`, `.token` = `"<id>.<secret>"`, `.expires_at`, `.max_uses`); enrollment via `POST /enroll/v1`; vault `seal_bytes`/`open_bytes`; `ObjectStore`; `JobType(name, params_model, result_handler=…)` + `register()`; `job_engine.create(session, *, org_id, type, target_device_id, params, requested_by=None, ttl_s=None)` + `signed_envelope(job)`; `publish_job(redis, device_id, envelope)`; `queue_ui_event(session, org_id, event)`; `audit_service.record(session, *, org_id, actor_type, actor_id, action, target_type, target_id, metadata)`.
- **Engineering conventions.** Async; typed SQLAlchemy 2; Pydantic v2 `ConfigDict(extra="forbid")` on wire models; logic in services not routes; forward-only additive migration (`down_revision="0014"`); `mypy` + `ruff` clean; frontend strict TS no `any`. If Postgres/Redis down: `pg_ctlcluster 16 main start; redis-server --daemonize yes`. Delete any stray `backend/uv.lock`.
- **Frontend state vocabulary sourced from backend.** `provisioningState.ts` badge maps keyed ONLY on the real `provision_run.state` strings above — no invented names.

---

### Task 1: Migration 0015 + RBAC seed

**Files:**
- Create: `backend/migrations/versions/0015_provisioning.py`
- Test: `backend/tests/test_provisioning_migration.py`

**Interfaces:**
- Produces: three tables (`os_image`, `provision_profile`, `provision_run`) with RLS `ENABLE + FORCE` + `tenant_isolation`; permissions `provisioning.read`/`provisioning.manage`/`provisioning.run`; role grants.

- [ ] **Step 1: Write the failing test** — assert (a) `alembic heads` resolves to `0015`; (b) after upgrade, the three tables exist and each has `rowsecurity = true` and `relforcerowsecurity = true` in `pg_class`; (c) the three permission rows exist in the permissions table; (d) role grants match (super/org-admin have all 3, technician has read+run, auditor/read-only have read only). Mirror `backend/tests/test_backup_migration.py` for the harness and the `pg_class`/permission/grant query shapes.

Run: `.venv/bin/python -m pytest tests/test_provisioning_migration.py -v` → FAIL.

- [ ] **Step 2: Implement the migration.** `revision="0015"`, `down_revision="0014"`. Copy the `_rls(table)` helper from `0014_backup.py` verbatim. Create:
  - `os_image`: `id` (uuid7 pk), `org_id` FK→organizations, `os_family` String(16), `name` String(255), `kind` String(16), `artifact_ref` Text nullable, `artifact_sha256` String(64) nullable, `params` JSONB default `{}`, `state` String(16) default `'active'`, `created_at` timestamptz server_default now. Index `ix_os_image_family (org_id, os_family)`.
  - `provision_profile`: `id`, `org_id` FK, `name` String(255), `image_id` FK→os_image, `post_config` JSONB default `{}`, `target_scope` JSONB default `{}`, `enabled` Boolean default true, `created_at`. Index `ix_provision_profile_enabled (org_id, enabled)`.
  - `provision_run`: `id`, `org_id` FK, `profile_id` FK→provision_profile, `job_id` FK→jobs nullable, `device_id` FK→devices nullable, `target_ref` Text nullable, `enrollment_token_id` FK→enrollment_tokens nullable, `answer_ref` Text nullable, `state` String(20) default `'pending'`, `log_ref` Text nullable, `started_at` timestamptz nullable, `finished_at` timestamptz nullable, `error` Text nullable, `requested_by` FK→users nullable, `actor_type` String(10). Index `ix_provision_run_state (org_id, state)`.
  - Call `_rls("os_image")`, `_rls("provision_profile")`, `_rls("provision_run")`.
  - Seed permissions `("provisioning.read","provisioning","read")`, `("provisioning.manage","provisioning","manage")`, `("provisioning.run","provisioning","run")` and role grants (mirror `0014_backup.py`'s seed block exactly): `super`/`org-admin` → all three; `technician` → read+run; `auditor`/`read-only` → read. `downgrade()` drops grants, permissions, then the three tables.

Run: `.venv/bin/python -m pytest tests/test_provisioning_migration.py -v` → PASS. `.venv/bin/ruff check migrations/versions/0015_provisioning.py` clean.

- [ ] **Step 3: Commit** `feat(provisioning): migration 0015 — os_image/provision_profile/provision_run tables, RLS, RBAC`.

---

### Task 2: ORM models

**Files:**
- Create: `backend/app/modules/provisioning/__init__.py` (empty), `backend/app/modules/provisioning/models.py`
- Modify: `backend/app/models/__init__.py` (re-export the three models, mirror how backup models are exported)
- Test: `backend/tests/test_provisioning_models.py`

**Interfaces:**
- Produces: `OsImage`, `ProvisionProfile`, `ProvisionRun` SQLAlchemy models with column parity to migration 0015. Import site: `from app.models import OsImage, ProvisionProfile, ProvisionRun`.

- [ ] **Step 1: Write the failing test** — insert an org + one `OsImage` (kind `image`, os_family `windows`), one `ProvisionProfile` referencing it, one `ProvisionRun` referencing the profile (state `pending`, actor_type `system`); flush; assert each row round-trips with the expected column values and nullable columns default to `None`. Mirror `backend/tests/test_backup_models.py`.

Run: `.venv/bin/python -m pytest tests/test_provisioning_models.py -v` → FAIL.

- [ ] **Step 2: Implement `models.py`** mirroring `app/modules/backup/models.py` (same `TimestampMixin`, `uuid7_pk()`, `Mapped[...]` typing, `mapped_column`, `ForeignKey`, `JSONB`, `Index`). Exact columns per Task 1. `ProvisionRun.state` default `"pending"`. Re-export from `app/models/__init__.py`.

Run: `.venv/bin/python -m pytest tests/test_provisioning_models.py -v` → PASS. `.venv/bin/mypy app/modules/provisioning/models.py` + `.venv/bin/ruff check` clean.

- [ ] **Step 3: Commit** `feat(provisioning): ORM models (OsImage, ProvisionProfile, ProvisionRun)`.

---

### Task 3: Wire + API schemas, and the provision.prepare contract envelope

**Files:**
- Create: `backend/app/modules/provisioning/schema.py`
- Create: `scripts/export_wire_schema.py` is already at repo root — modify it only if it enumerates modules; otherwise the new wire models are picked up by adding them to its export list (check the script; mirror how backup wire models were exported to `docs/kernel/wire-protocol/`).
- Test: `backend/tests/test_provisioning_schema.py`

**Interfaces:**
- Produces (Pydantic v2, `_Wire` base with `ConfigDict(extra="forbid")` for wire models):
  - `ProvisionEnrollParams(_Wire)`: `provision_run_id: str`, `os_family: Literal["windows","linux","macos"]`, `hostname: str`, `post_config: dict[str, Any]` (NON-secret post-config only — domain name, OU, agent settings; NO credentials), `expected_agent_version: str | None = None`.
  - `ProvisionEnrollResult(_Wire)`: `state: Literal["success","failed"]`, `hostname_set: bool`, `domain_joined: bool | None = None`, `agent_healthy: bool`, `error: str | None = None`.
  - `ProvisionPrepareEnvelope(_Wire)` (the site-server contract, §8): `provision_run_id: str`, `os_family: Literal[...]`, `image_ref: str | None`, `image_sha256: str | None`, `answer_ref: str | None`, `target_ref: str | None`, `wake_on_lan: dict[str, Any]` (e.g. `{"mac": "..."}`).
  - API models (plain `BaseModel`, NO secret fields): `OsImageOut` (id, os_family, name, kind, artifact_ref, artifact_sha256, state, created_at — NO `params` secrets; `params` is non-secret so may be included), `OsImageCreateRequest` (os_family, name, kind, params), `ProfileOut` (id, name, image_id, post_config **with secret refs redacted — see Task 4**, target_scope, enabled, created_at), `ProfileUpsertRequest` (name, image_id, post_config, target_scope, enabled), `RunOut` (id, profile_id, device_id, target_ref, state, answer_ref, started_at, finished_at, error), `RunStartRequest` (target_ref, wake_on_lan optional), `RunStartResponse` (run_id, state — NEVER the token secret or answer bytes), `FleetProvisioningReportOut`, `DeviceProvisioningOut`.

- [ ] **Step 1: Write the failing test** — construct each wire model; assert `extra="forbid"` rejects an unknown field on `ProvisionEnrollParams`/`ProvisionEnrollResult`/`ProvisionPrepareEnvelope`; assert `ProvisionEnrollResult` requires `state`/`hostname_set`/`agent_healthy`; assert the per-OS `os_family` Literal rejects `"freebsd"`. Mirror `backend/tests/test_backup_schema.py`.

Run: `.venv/bin/python -m pytest tests/test_provisioning_schema.py -v` → FAIL.

- [ ] **Step 2: Implement `schema.py`.** `_Wire(BaseModel)` with `model_config = ConfigDict(extra="forbid")`. Define all models above. Export the three wire models to `docs/kernel/wire-protocol/` via the repo-root export script (run it; confirm `ProvisionEnrollParams.schema.json` etc. are written).

Run: tests PASS; `.venv/bin/mypy app/modules/provisioning/schema.py` + `ruff` clean; `.venv/bin/python ../scripts/export_wire_schema.py` (from repo root) writes the 3 schema files.

- [ ] **Step 3: Commit** `feat(provisioning): wire contracts + provision.prepare site-server envelope + exported schemas`.

---

### Task 4: Registry service (images + profiles, with secret-reference sealing)

**Files:**
- Create: `backend/app/modules/provisioning/repository.py`
- Test: `backend/tests/test_provisioning_repository.py`

**Interfaces:**
- Consumes: vault `seal_bytes(session, *, org_id, key_id, plaintext) -> str`; models from Task 2.
- Produces:
  - `async def create_image(session, *, org_id, os_family, name, kind, params, artifact_ref=None, artifact_sha256=None) -> OsImage` — validates the per-OS `kind` constraint (windows→image, linux→build, macos→enrollment; else `ProvisioningValidationError`); org-scoped; audits `provisioning.image_created` (actor threaded).
  - `async def list_images(session, org_id) -> list[OsImage]` / `async def get_image(session, org_id, image_id) -> OsImage | None` — org-scoped.
  - `async def upsert_profile(session, *, org_id, name, image_id, post_config, target_scope, enabled, actor_id, actor_type) -> ProvisionProfile` — org-scoped; **seals any credential fields in `post_config`**: `post_config` arriving with plaintext under keys `domain_join.password` / `local_admin.password` is sealed via `seal_bytes(session, org_id=org_id, key_id=profile.id, plaintext=...)` and REPLACED in the stored JSON by a reference `{"__sealed__": "<sealed-str>"}` — plaintext never persists. (Flush the profile first to get `profile.id` as the `key_id`, mirroring backup's `create_repository` flush-before-seal.) Audits `provisioning.profile_upserted`; secret plaintext NEVER in audit metadata.
  - `async def list_profiles(session, org_id) -> list[ProvisionProfile]` / `get_profile(session, org_id, profile_id)` — org-scoped.
  - `ProvisioningValidationError(Exception)`.
- Constant `SEALED_MARKER = "__sealed__"`.

- [ ] **Step 1: Write the failing test** — (a) `create_image` with a mismatched kind (windows+build) raises `ProvisioningValidationError`; (b) `upsert_profile` given `post_config={"domain_join":{"user":"svc","password":"s3cret"},...}` stores a profile whose persisted `post_config["domain_join"]["password"]` is a `{"__sealed__": ...}` ref and does NOT equal `"s3cret"`, and the plaintext `"s3cret"` appears in NO audit event's metadata (assert against the audit rows) and NO column; (c) `open_bytes` on the sealed ref returns `b"s3cret"` (round-trip); (d) tenancy: `get_image`/`get_profile` for another org return `None`. Mirror `backend/tests/test_backup_repository.py`.

Run: `.venv/bin/python -m pytest tests/test_provisioning_repository.py -v` → FAIL.

- [ ] **Step 2: Implement `repository.py`.** Flush-before-seal for profile id. Walk `post_config` for the two known credential paths, seal each, replace with the marker ref. Every query carries `org_id`.

Run: tests PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(provisioning): image/profile registry with vault-sealed post_config secrets`.

---

### Task 5: Answer-file renderer (THE security crux)

**Files:**
- Create: `backend/app/modules/provisioning/render.py`
- Test: `backend/tests/test_provisioning_render.py`

**Interfaces:**
- Consumes: vault `open_bytes(session, *, org_id, key_id, sealed) -> bytes`; `SEALED_MARKER`.
- Produces:
  - `async def render_answer_file(session, *, org_id, profile: ProvisionProfile, image: OsImage, token: str, now: datetime) -> bytes` — dispatches on `image.os_family`: windows→`_render_unattend`, linux→`_render_linux` (kickstart/preseed/cloud-init selected by `image.params["installer"]`), macos→raises `ProvisioningValidationError` (macOS has no answer file — enrollment kind is handled in dispatch, not here). Resolves sealed secrets in `profile.post_config` by calling `open_bytes` for each `{"__sealed__": ...}` ref **at render time only**, injects them + the `token` into the rendered bytes, and returns the bytes. The function is **deterministic** given (profile, image, token) — no timestamps or randomness in the output body (the `now` param is for audit only, not embedded).
  - Pure helpers `_render_unattend(fields: dict) -> bytes`, `_render_linux(installer: str, fields: dict) -> bytes` operating on already-resolved plaintext fields (so they are trivially unit-testable without the vault).

- [ ] **Step 1: Write the failing test** — seed a windows profile whose `post_config` has a sealed `local_admin.password` (sealed via the repository helper) and a hostname pattern; call `render_answer_file` with `token="tok123"`; assert (a) the rendered bytes are valid XML containing the hostname, the domain-join user, the **injected plaintext admin password**, and the token; (b) **byte-for-byte determinism** — two calls with identical inputs produce identical bytes; (c) **secret-absence**: the plaintext admin password appears ONLY in the returned bytes — NOT in the `profile.post_config` DB row (still a sealed ref), NOT in any audit event, NOT in logs (assert no logging occurred / capture caplog is empty of the secret); (d) a linux `build` image renders the selected installer format containing the token; (e) a macos image raises `ProvisioningValidationError`. 

Run: `.venv/bin/python -m pytest tests/test_provisioning_render.py -v` → FAIL.

- [ ] **Step 2: Implement `render.py`.** Resolve sealed refs via `open_bytes` into a local plaintext dict that never leaves the function; pass to the pure `_render_*` helpers; return bytes. No `logger`/`print` anywhere. Do not write plaintext to any column or audit.

Run: tests PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(provisioning): deterministic answer-file renderer with decrypt-at-render secret injection`.

---

### Task 6: Run start — token issuance, render, store, prepare contract

**Files:**
- Create: `backend/app/modules/provisioning/dispatch.py`
- Modify: `backend/app/core/storage.py` (add `put_provision_object`/`get_provision_object`/`delete_provision_object` mirroring the backup object helpers, key layout `{org_id}/provision/{suffix}`)
- Test: `backend/tests/test_provisioning_dispatch.py`
- Test: `backend/tests/test_provisioning_storage.py`

**Interfaces:**
- Consumes: `create_token`; `render_answer_file`; `get_profile`/`get_image`; the signer (`app.core.signing.get_signer`/`SigningService` — check how the job engine obtains it) to sign the `ProvisionPrepareEnvelope`; `ObjectStore`.
- Produces:
  - `async def start_provision_run(session, object_store, vault, signer, *, org_id, profile_id, target_ref, now, actor_id=None, actor_type="system") -> ProvisionRun` — org-scoped-load the profile + its image (fail-closed if missing/disabled → `ProvisioningValidationError`). Create the `ProvisionRun` (`pending`). Issue a provisioning-scoped token via `create_token(session, org_id=org_id, created_by=actor_id or <system-user sentinel — see note>, name=f"provision:{run.id}", expires_in_hours=24, max_uses=1)`; store `run.enrollment_token_id = issued.id`. For windows/linux: `render_answer_file(...)` → `put_provision_object(org_id, f"answers/{run.id}", bytes)` → `run.answer_ref`. For macos (`enrollment` kind): skip the answer file, set `run.answer_ref = None` (enrollment binding only). Build+sign a `ProvisionPrepareEnvelope` (contract; sign via the signer — proves invariant #1 applies to the site-server path) and audit `provisioning.prepare` intent (envelope metadata WITHOUT the token secret or answer bytes). Advance `run.state = "awaiting_enrollment"`. Audit `provisioning.run_started` (actor threaded; NO token secret / NO answer bytes / NO injected creds in metadata). Return the run. NOTE: `create_token`'s `created_by` is non-null FK→users; for a system-initiated run pass the org's system/service user id if one exists, else require `actor_id` — resolve this against how other worker-initiated flows supply a user id (if none exists, a run must be operator-initiated in v1; document that constraint).
- Storage helpers mirror `app/core/storage.py` backup helpers exactly.

- [ ] **Step 1: Write the failing tests** — storage: `put_provision_object` returns an `s3://…/{org_id}/provision/answers/{id}` ref and `get_provision_object` round-trips the bytes. dispatch: (a) `start_provision_run` for a windows profile creates a run in `awaiting_enrollment`, sets `answer_ref` (object exists + decrypts to rendered bytes containing the token), issues a token bound to the org with `run.enrollment_token_id` set; (b) **secret/token absence**: the token secret and the injected admin password appear in NO audit event metadata and NO API-facing field (assert against audit rows + the run row); (c) a macos `enrollment` profile creates a run in `awaiting_enrollment` with `answer_ref=None`; (d) a disabled/missing profile raises `ProvisioningValidationError`; (e) tenancy: a profile in another org is unreachable. Mirror `backend/tests/test_backup_dispatch.py`.

Run: both test files → FAIL.

- [ ] **Step 2: Implement** the storage helpers and `start_provision_run`. Every query org-scoped; secret material only in the answer bytes.

Run: tests PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(provisioning): run start — token issuance, answer render+store, signed prepare contract`.

---

### Task 7: Enrollment correlation + provision.enroll dispatch

**Files:**
- Create: `backend/app/modules/provisioning/enroll.py`
- Modify: `backend/app/services/enrollment.py` (additive hook: after a device is created for a consumed token, if a `provision_run` references that token, bind it)
- Test: `backend/tests/test_provisioning_enroll.py`

**Interfaces:**
- Produces:
  - `async def bind_provisioned_device(session, *, org_id, enrollment_token_id, device_id, now) -> ProvisionRun | None` — org-scoped-find the `awaiting_enrollment` run whose `enrollment_token_id` matches; set `device_id`, `state="enrolled"`; audit `provisioning.enrolled` (system). Returns the run or `None` if no matching run (a normal agent enrollment, not a provisioning one).
  - `async def dispatch_provision_enroll(session, job_engine, *, org_id, run_id, now, actor_id=None, actor_type="system") -> list[tuple[UUID, UUID, dict[str, Any]]]` — org-scoped-load the `enrolled` run + its profile/image; build `ProvisionEnrollParams` (NON-secret post_config only — strip any sealed refs; domain name/OU/hostname/agent settings), `job_engine.create(type="provision.enroll", target_device_id=run.device_id, ...)`, `signed_envelope`, set `run.job_id`; audit `provisioning.enroll_dispatched`; triple-return (no publish). 
- Modify `services/enrollment.py`: after the enroll flow creates the `Device`, call `bind_provisioned_device(session, org_id=org_id, enrollment_token_id=<the consumed token id>, device_id=device.id, now=now)` (import locally to avoid a cycle). This is the ONLY change to core enrollment — additive, guarded (no-op when the token isn't a provisioning token).

- [ ] **Step 1: Write the failing test** — (a) drive the REAL enroll path (`POST /enroll/v1` or the enrollment service function it calls) with a token issued by `start_provision_run`; assert the matching run advances `awaiting_enrollment → enrolled` and binds the new `device_id`; a non-provisioning token enroll leaves no run touched and returns `None`; (b) `dispatch_provision_enroll` on the enrolled run produces exactly one signed `provision.enroll` job targeting `run.device_id`, sets `run.job_id`, audits `provisioning.enroll_dispatched`, and the `ProvisionEnrollParams` contains NO sealed refs / NO credentials; (c) tenancy. Mirror the enrollment test harness in `backend/tests/` (find the existing enrollment test) + `test_backup_dispatch.py`.

Run: `.venv/bin/python -m pytest tests/test_provisioning_enroll.py -v` → FAIL.

- [ ] **Step 2: Implement** `enroll.py` + the additive enrollment hook. Keep the hook a guarded no-op for non-provisioning tokens.

Run: tests PASS; full enrollment suite still green (`.venv/bin/python -m pytest tests/ -k enroll -q`); mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(provisioning): enrollment correlation + signed provision.enroll post-config dispatch`.

---

### Task 8: Result ingestion + job-type registration

**Files:**
- Create: `backend/app/modules/provisioning/handler.py`
- Modify: `backend/app/modules/provisioning/enroll.py` (add `ingest_provision_result`)
- Test: `backend/tests/test_provisioning_result.py`

**Interfaces:**
- Produces:
  - `async def ingest_provision_result(session, job, result: ProvisionEnrollResult) -> None` — org-scoped-find the run by `job_id`; graceful no-op if none. `result.state == "success"` → run `complete`, `finished_at`; else (failed OR out-of-domain state that bypassed validation) → coerce `failed` with `error` (never crash). Single `provisioning.result` audit (system). 
  - `handler.register_job_types()` — registers `JobType("provision.enroll", ProvisionEnrollParams, result_handler=<handle>)` where `<handle>` validates `ProvisionEnrollResult(**result)` (→ `ProvisioningValidationError` on `ValidationError`), calls `ingest_provision_result`, then `queue_ui_event(session, job.org_id, {"kind":"provisioning_status_updated","device_id":str(job.target_id),"job_id":str(job.id)})`.

- [ ] **Step 1: Write the failing test** — (a) a success result flips the run to `complete`; (b) a failed result → `failed` with error; (c) an out-of-domain state (via `model_construct` to bypass validation) → coerced `failed`, no crash; (d) a no-matching-run report is a graceful no-op; (e) after `register_job_types()`, `provision.enroll` resolves in `app.jobs.registry._registry`; (f) a round-trip through `job_engine.complete` reconciles the run and queues the UI event. Mirror `backend/tests/test_backup_result.py`.

Run: `.venv/bin/python -m pytest tests/test_provisioning_result.py -v` → FAIL.

- [ ] **Step 2: Implement** `handler.py` + `ingest_provision_result`.

Run: tests PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(provisioning): provision.enroll result ingestion + job-type registration`.

---

### Task 9: Reporting

**Files:**
- Create: `backend/app/modules/provisioning/reporting.py`
- Test: `backend/tests/test_provisioning_reporting.py`

**Interfaces:**
- Produces (read-only, org-scoped, no writes/dispatch/audit):
  - `async def provision_run_status(session, org_id, run_id) -> dict` — one run's state/target/timings/device/log.
  - `async def fleet_provisioning_report(session, org_id) -> dict` — run counts by state; success rate = `complete` / terminal(`complete`+`failed`) runs, divide-by-zero → `0.0`; machines provisioned (runs reaching `complete`); per-profile rollup.
  - `async def device_provisioning(session, org_id, device_id) -> dict` — which profile/image provisioned the device, when it enrolled, latest run state.

- [ ] **Step 1: Write the failing test** — seed runs across states + two orgs; assert `fleet_provisioning_report` counts/success-rate against hand-computed literals (e.g. 2 complete / (2 complete + 1 failed) = 2/3); `device_provisioning` returns the binding for a provisioned device; tenancy excludes org B. Mirror `backend/tests/test_backup_reporting.py`.

Run: → FAIL.

- [ ] **Step 2: Implement `reporting.py`.** Rates are fractions [0,1].

Run: tests PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(provisioning): per-run + fleet + device reporting`.

---

### Task 10: API routes + module/gateway registration

**Files:**
- Create: `backend/app/modules/provisioning/api.py`
- Modify: `backend/app/modules/provisioning/__init__.py` (`register(app)`), `backend/app/modules/__init__.py` (`register_all`), `backend/app/gateway/main.py`
- Test: `backend/tests/test_provisioning_api.py`

**Interfaces:**
- Produces: routes under `/api/v1/provisioning` per spec §12, each `Depends(require_permission("<code>"))`; response models from Task 3 with NO secret fields (profile responses **redact** sealed refs — never echo the `{"__sealed__":...}` value; render a boolean `has_domain_join_credential` instead). `POST /profiles/{id}/run` computes `now` in-handler, `actor=user`, calls `start_provision_run`, then (for the v1 flow) does NOT publish anything (prepare is contract-only); returns `RunStartResponse` (run_id+state, NEVER token/answer). `register(app)` calls `register_job_types()` then mounts the router; `org_id`/`actor_id` from `AuthContext`.
- Modify `register_all` to call `provisioning.register(app)` (after backup); `gateway/main.py` to `register_provisioning_job_types()` alongside backup's.

- [ ] **Step 1: Write the failing test** (use the `api_env` harness like `tests/test_backup_api.py`) — `GET /images` OK for read-only; `POST /images` 403 technician, 2xx org-admin; `POST /profiles` 403 technician, 2xx org-admin, and the profile-create response body contains NO plaintext credential and NO `__sealed__` value (redacted to a boolean); `POST /profiles/{id}/run` OK for technician (`provisioning.run`), returns run_id+state and NO token/answer bytes; `GET /runs`/`GET /runs/{id}`/`GET /devices/{id}/provisioning`/`GET /report` OK for read; assert NO response body anywhere contains a token secret, an injected credential, or a `__sealed__` ref. Mirror `tests/test_backup_api.py`.

Run: → FAIL.

- [ ] **Step 2: Implement** `api.py` + the three wiring edits. Redact sealed refs in profile responses.

Run: `.venv/bin/python -m pytest tests/test_provisioning_api.py -q` PASS; then FULL suite `.venv/bin/python -m pytest -q` (shared wiring); mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(provisioning): API routes, module + gateway registration`.

---

### Task 11: Worker — timeout sweep, answer cleanup, worker registration (un-masking test)

**Files:**
- Modify: `backend/app/worker.py`
- Test: `backend/tests/test_provisioning_worker.py`

**Interfaces:**
- Produces:
  - `async def provision_timeout_sweep_all_orgs(now) -> int` — per org, mark runs in `awaiting_enrollment` older than a deadline (e.g. `started_at < now - 24h`) as `failed` (`error="enrollment timed out"`); delete the run's `answer_ref` object if present (secret cleanup); org-scoped; system-attributed. Returns count failed.
  - Answer cleanup on terminal state also runs here (or in the result handler — plan it in the sweep for simplicity): for runs newly `complete`/`failed` with a lingering `answer_ref`, delete the object and null the ref.
  - `register_worker_job_types()` ALSO registers `provision.enroll` (extend the existing function).
  - Beat entry `"provision-timeout-sweep": {"task": "app.worker.provision_timeout_sweep", "schedule": 3600.0}` + the `@celery_app.task` shell `app.worker.provision_timeout_sweep` calling `asyncio.run(provision_timeout_sweep_all_orgs(datetime.now(UTC)))`.

- [ ] **Step 1: Write the failing tests** — (a) a stale `awaiting_enrollment` run past the deadline → `failed` and its answer object deleted; a fresh one is untouched; (b) **UN-MASKING test:** pop `provision.enroll` from `app.jobs.registry._registry`, assert `UnknownJobType`, then call `worker.register_worker_job_types()` AND drive `worker.create_celery()`, assert `provision.enroll` resolves (fail-without-fix). Mirror `backend/tests/test_backup_worker.py`.

Run: → FAIL.

- [ ] **Step 2: Implement** the sweep + registration + beat/task shell.

Run: `.venv/bin/python -m pytest tests/test_provisioning_worker.py -q` PASS; FULL suite green; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(provisioning): timeout sweep + answer cleanup + worker registration`.

---

### Task 12: Frontend feature folder

**Files:**
- Create: `frontend/src/modules/provisioning/{types.ts,provisioningState.ts,api.ts,ImagesPage.tsx,ProfilesPage.tsx,ProfileEditor.tsx,ProvisioningRunsPage.tsx,DeviceProvisioningTab.tsx}`
- Modify: `frontend/src/lib/api.ts`, `frontend/src/App.tsx` (nav gated on `provisioning.read` + routes), the device detail page (provisioning tab)
- Test: `frontend/src/modules/provisioning/__tests__/provisioning.test.tsx`

**Interfaces:**
- `types.ts` mirrors backend response models exactly (NO secret fields; profile shows `has_domain_join_credential: boolean`, never a sealed ref). `provisioningState.ts` maps `provision_run.state` {pending, awaiting_enrollment, enrolled, complete, failed} → badge variants, `?? "muted"` fallback — NO invented names. `api.ts` hooks with query-key invalidation after mutations (no live event). Pages: ImagesPage (`provisioning.manage` gated create), ProfilesPage/ProfileEditor (secret fields write-only, `provisioning.manage`), ProvisioningRunsPage (state badges + a "Provision" control gated on `provisioning.run`), DeviceProvisioningTab (`provisioning.read`). Strict TS, no `any`.

- [ ] **Step 1: Write the failing test** (mirror `frontend/src/modules/backup/__tests__/`) — ProvisioningRunsPage lists runs; the "Provision" control is hidden without `provisioning.run`; ProfileEditor create is hidden without `provisioning.manage`; `provisioningState` variant for a REAL state (`enrolled`) is non-fallback and an unknown string returns `"muted"`; a profile response with `has_domain_join_credential: true` shows a boolean indicator, never a secret.

Run: → FAIL.

- [ ] **Step 2: Implement** pages + nav/routes + device tab + ApiClient methods + hooks.

Run: `npm test`, `npm run typecheck`, `npm run build` all green.

- [ ] **Step 3: Commit** `feat(provisioning): images/profiles/runs UI + device provisioning tab`.

---

### Task 13: Deploy docs

**Files:**
- Modify: `deploy/README.md`, `deploy/.env.example`
- Test: `cd deploy && cp .env.example .env && docker compose config >/dev/null && echo OK && rm -f .env`

- [ ] **Step 1:** Add a "Provisioning & imaging" section to `deploy/README.md` (mirror the Backup section style): per-tenant provisioning object layout (`{org_id}/provision/…` images + run-scoped answer files); vault-sealed answer-file secrets injected only at render time and answer objects deleted on terminal state; the `provision-timeout-sweep` beat entry (no new compose services); and the **site-server contract seam** (PXE/TFTP/DHCP, WoL, LAN cache) as a documented future deployable with the `provision.prepare` signed envelope + mTLS site-server auth. Add an `.env.example` note that provisioning reuses object storage/vault/redis/enrollment-CA and adds no new required vars.

- [ ] **Step 2: Validate** `docker compose config` prints OK.

- [ ] **Step 3: Commit** `docs(provisioning): deploy notes — object layout, answer-file secrets, timeout sweep, site-server contract`.

---

## Self-Review

**Spec coverage:** §1 scope → all tasks; §2 per-OS strategies → T4 (kind constraint) + T5 (per-OS render) + T6 (macos enrollment branch); §3 data model → T1/T2; §4 artifacts → T6 (storage helpers, answer objects); §5 workflow → T6 (start/token/render) + T7 (enroll correlation + provision.enroll) + T8 (result); §6 enrollment handoff → T7 (real /enroll/v1 reuse + additive hook); §7 secrets crux → T4 (seal on upsert) + T5 (decrypt-at-render + secret-absence tests) + T6 (no token/secret in audit) + T10 (redacted responses); §8 site-server contract → T3 (envelope model) + T6 (sign+audit prepare intent) + T13 (docs); §9 post-config verify → T8; §10 reporting → T9; §11 RBAC → T1 (seed) + T10 (enforcement); §12 API → T10; §13 worker → T11; §14 frontend → T12; §15 invariants → signed dispatch (T6/T7), secrets (T4/T5/T6/T10), audit+actor (all service tasks), tenancy (all), RBAC (T1/T10), idle budget (provision.enroll dormant); §16 testing → per-task tests incl secret-absence (T4/T5/T6) + un-masking (T11) + real-enroll handoff (T7); §17 deploy → T13; §18 deferred → not built (site-server deployable, capture, drivers, MDM). Accumulated lessons: publish-after-commit (T7 triple-return; T10 note prepare is contract-only so no publish), actor threading (all), three-process registration + un-masking (T10 gateway + T11 worker), out-of-domain coercion (T8), frontend real vocab (T12).

**Placeholder scan:** every task names concrete files, exact signatures, and vocabularies from Global Constraints; no TBD/TODO. One genuine open item is flagged inline, not hidden: T6's `create_token.created_by` non-null-FK for system-initiated runs — resolved as "v1 provisioning runs are operator-initiated (real `actor_id`); a system/service-user path is deferred," which the implementer must honor (the timeout sweep in T11 only fails stale runs, it does not start them, so it needs no user id).

**Type consistency:** `ProvisionEnrollParams`/`ProvisionEnrollResult`/`ProvisionPrepareEnvelope`/`OsImage`/`ProvisionProfile`/`ProvisionRun`/`start_provision_run`/`bind_provisioned_device`/`dispatch_provision_enroll`/`ingest_provision_result`/`render_answer_file`/`create_image`/`upsert_profile`/`SEALED_MARKER` are used consistently across tasks. State vocabularies fixed in Global Constraints and used verbatim. The dispatch triple-return `(job_id, device_id, envelope)` + publish-after-commit is consistent (T7 dispatch, T10 caller). `SEALED_MARKER`/`{"__sealed__":...}` is consistent across T4 (seal), T5 (resolve), T10 (redact).
