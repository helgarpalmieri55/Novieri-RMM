# Backup Engine (Phase 6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the control-plane slice of a content-addressed, deduplicated, encrypted, retention-managed backup engine — policies, repository catalog, signed `backup.run`/`backup.restore` job orchestration, restore + proven-restore verification, GFS retention, repository GC, and reporting — with the repository format pinned so image-level/app-aware slot in later.

**Architecture:** Two new signed agent job types (`backup.run`, `backup.restore`) register into the operator app, gateway, and Celery worker. The heavy chunking/snapshot engine is agent-side, is **not built**, and is represented in server tests by a deterministic **fake chunker**. (It was scoped to the separate `it-suite-agent` repo when this plan was written; `CLAUDE.md` `a32cead` moved the agent to `agent/` in-tree on 2026-08-05. The engine remains unwritten.) Repositories are per-tenant object-storage prefixes; each has a vault-sealed master key delivered to the agent only inside the signed job (`repo_key`). GFS retention, repository garbage collection, and verification run as control-plane worker sweeps, not agent jobs.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async/asyncpg), Alembic, Pydantic v2, Celery+Redis, MinIO (object storage), `cryptography` (AES-256-GCM), React + TypeScript (strict) + Vite + TanStack Query, vitest.

## Global Constraints

Every task's requirements implicitly include this section. Copy exact values verbatim.

- **Security invariant #1 — signed jobs only.** `backup.run`/`backup.restore` dispatch exclusively via the kernel job engine (`job_engine.create` + `signed_envelope` + `publish_job`). The `repo_key` travels inside the signed envelope. No unsigned or direct-execute path exists.
- **Security invariant #3 — keys/secrets.** The repository master key is vault-sealed (envelope), decrypted only at point of use, placed only inside signed job params (`repo_key`), and NEVER returned in an API response, written to an audit event's metadata, or logged. Backup data is encrypted at rest under the org's repository key.
- **Security invariant #4 — audit.** Every privileged action writes to the hash-chained audit log: repository/policy create-edit, backup dispatch + result, restore/verify dispatch + result, GC, evidence export. Operator actions carry the real `actor_id` with `actor_type="user"`; worker-initiated actions use `actor_type="system"`, `actor_id=None`. `actor_type` is `Literal["user", "agent", "system"]`.
- **Security invariant #5 — tenancy.** EVERY `select()`/write over `backup_repositories`/`backup_policies`/`backup_runs`/`backup_points`/`restore_jobs`/`Device` carries an explicit `org_id` predicate in addition to RLS. RLS is `ENABLE + FORCE` + a `tenant_isolation` policy `USING (org_id = current_setting('app.current_org', true)::uuid)` on all five tables. Repositories are per-tenant (object prefix `{org_id}/backup/…`); no cross-org dedup or chunk sharing. A missing tenant filter is a security bug. Every joined table gets its own `org_id` predicate.
- **Security invariant #6 — RBAC.** Server-side `Depends(require_permission("<code>"))` on every route; server-side permission check on every dispatch. Frontend gating is cosmetic only.
- **Publish AFTER commit.** Dispatch functions build+sign jobs and accumulate `(job_id, device_id, envelope)` triples; the CALLER publishes to Redis only AFTER its `org_session` transaction commits (the fix landed in patch+deploy — do it here from the start, mirroring `app/modules/patch/api.py` `POST /scan`).
- **Actor threading.** Dispatch functions take `actor_id: UUID | None = None, actor_type: Literal["user","agent","system"] = "system"`; API routes pass `actor_id=ctx.user_id, actor_type="user"`; worker sweeps keep the system default.
- **Job-type registration in ALL THREE processes.** `register(app)` (app via `register_all`), the gateway (`app/gateway/main.py` startup), AND the Celery worker (`register_worker_job_types()` in `app/worker.py`). Ship the un-masking worker test that pops the job types from `app.jobs.registry._registry`, asserts `UnknownJobType`, then drives the worker bootstrap and asserts they resolve (the Phase-4 Critical lesson).
- **Agent idle budget.** `backup.run`/`backup.restore` are dormant until a job; v1 needs snapshot APIs + file reads only, NOT raw-volume/filter-driver access. (Agent-side; this repo pins the contract.)
- **State vocabularies (verbatim).** `backup_runs.state` ∈ {`pending`,`running`,`success`,`failed`}. `backup_points.state` ∈ {`complete`,`failed`,`pruned`}. `restore_jobs.state` ∈ {`pending`,`running`,`success`,`failed`}. `restore_jobs.mode` ∈ {`restore`,`verify`}. `backup_repositories.state` ∈ {`active`,`disabled`}. `snapshot` ∈ {`vss`,`lvm`,`apfs`}. `os_family` ∈ {`windows`,`linux`,`macos`}. Manifest stream `type` (v1) = `filetree`; entry `kind` ∈ {`file`,`dir`,`symlink`}.
- **Repository format (pinned).** Chunk ID = `BLAKE3-256(plaintext)` hex; chunks AES-256-GCM store-once by ID (object `{storage_ref}/chunks/{id[:2]}/{id}` = `nonce(12)||ciphertext||tag(16)`); manifest at `{storage_ref}/manifests/{backup_point_id}` = `{schema_version, streams:[{type:"filetree", entries:[…]}]}` (encrypted); repo index at `{storage_ref}/index`. dedup_params default: `{"algo":"buzhash","avg":1048576,"min":524288,"max":4194304,"hash":"blake3-256"}`.
- **Agent engine is mocked.** Server tests use a deterministic **fake chunker/agent-result builder** (a test helper producing reproducible manifest_ref + chunk stats from fixed input) — the real Go engine is out of this repo. No real chunking runs in server tests.
- **Reuse, do not fork.** `window_open` (`app/core/schedule.py`); `JobType(name, params_model, result_handler=…)`; `ObjectStore`; the device-group→org-default resolution pattern; `queue_ui_event` (post-commit UI events, frontend refetches after mutation — NO live-event subscription); the vault (`app/core/vault.py`).
- **Engineering conventions.** Async; typed SQLAlchemy 2; Pydantic v2 `ConfigDict(extra="forbid")` on wire models; logic in services not routes; forward-only additive migration; `mypy` + `ruff` clean; frontend strict TS, no `any`. venv at `backend/.venv` (run tools as `.venv/bin/…`). If Postgres/Redis are down: `pg_ctlcluster 16 main start; redis-server --daemonize yes`. Delete any stray `backend/uv.lock` (gitignored). Add `blake3` to `backend/pyproject.toml` dependencies (Task 5 needs it) — pin `blake3>=0.4`.

---

### Task 1: Migration + RBAC seed

**Files:**
- Create: `backend/migrations/versions/0014_backup.py`
- Modify: `backend/app/core/rbac.py`, `backend/tests/test_rbac.py`, `backend/tests/test_api_routes.py`
- Test: `backend/tests/test_migration_backup.py`

**Interfaces:**
- Produces: 5 tables per spec §4 (`backup_repositories`, `backup_policies`, `backup_runs`, `backup_points`, `restore_jobs`); permissions `backup.read`, `backup.policy.manage`, `backup.run`, `backup.restore`.

- [ ] **Step 1: Write the failing test** — mirror `tests/test_migration_app_deploy.py`; assert all 5 tables have `rowsecurity` true and a `tenant_isolation` policy (query `pg_class`/`pg_policies`), and that the 4 permissions + role grants exist.
- [ ] **Step 2: Run → FAIL** (`.venv/bin/python -m pytest tests/test_migration_backup.py -v`).
- [ ] **Step 3: Write the migration.** `down_revision` = Phase 5's head (run `.venv/bin/alembic heads` to confirm — it is `0013`). Create the 5 tables per §4 (exact columns/types/UNIQUE/index). Copy the `_rls(op, table)` helper from `0013_app_deploy.py` and apply to all five. Seed the 4 permissions and grant per §11: super/org-admin → all 4; technician → `backup.read`+`backup.run`+`backup.restore` (NOT `policy.manage`); auditor → `backup.read`; read-only → `backup.read`.
- [ ] **Step 4: Sync `app/core/rbac.py`** (add 4 codes+descriptions) and update `tests/test_rbac.py` + `tests/test_api_routes.py` expected sets.
- [ ] **Step 5: Run → PASS**, `.venv/bin/alembic upgrade head` clean, mypy/ruff clean.
- [ ] **Step 6: Commit** `feat(backup): migration 0014 — repositories/policies/runs/points/restore tables, RLS, RBAC`.

---

### Task 2: ORM models

**Files:**
- Create: `backend/app/modules/backup/__init__.py`, `backend/app/modules/backup/models.py`
- Test: `backend/tests/test_backup_models.py`

**Interfaces:**
- Produces: `BackupRepository`, `BackupPolicy`, `BackupRun`, `BackupPoint`, `RestoreJob` (typed models); `__table_args__` mirroring the migration's UNIQUE + named indexes exactly.

- [ ] **Step 1: Write the failing test** — insert one row per model under `org_session`, read back, assert defaults (`BackupRun.state=="pending"`, `BackupPoint.state=="complete"` set explicitly, `RestoreJob.mode` round-trips, JSONB `retention_gfs`/`dedup_params`/`sources`/`selection`/`dedup_stats` round-trip). Mirror `tests/test_deploy_models.py`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement models** matching §4 columns/types (use `Mapped[...]`, `uuid7_pk()`, `TimestampMixin`, `JSONB` default `dict`, FK columns). Register via `app/models/__init__.py` so Alembic/metadata sees them (mirror deploy).
- [ ] **Step 4: Run → PASS**, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): ORM models (BackupRepository, BackupPolicy, BackupRun, BackupPoint, RestoreJob)`.

---

### Task 3: Vault raw-bytes seal/open + ObjectStore backup layout

**Files:**
- Modify: `backend/app/core/vault.py` (add `seal_bytes`/`open_bytes`), `backend/app/core/storage.py` (backup object methods)
- Test: `backend/tests/test_vault_bytes.py`, `backend/tests/test_storage_backup.py`

**Interfaces:**
- Consumes: existing `_EnvelopeVault` machinery in `vault.py`.
- Produces: on the `VaultService` protocol + `_EnvelopeVault`: `async def seal_bytes(session, *, org_id: UUID, key_id: UUID, plaintext: bytes) -> str` and `async def open_bytes(session, *, org_id: UUID, key_id: UUID, sealed: str) -> bytes` (envelope-encrypt raw bytes under the org DEK, AAD-bound to `org_id|key_id`, base64-in-string like the existing `encrypt`/`decrypt` but bytes-in/bytes-out). On `ObjectStore`: `_backup_key(org_id, repository_id, suffix) -> str` → `f"{org_id}/backup/{repository_id}/{suffix}"`; `put_backup_object(org_id, repository_id, suffix, data) -> str`, `get_backup_object(ref) -> bytes`, `delete_backup_object(ref) -> None`, `list_backup_chunks(org_id, repository_id) -> list[str]` (chunk IDs present).

- [ ] **Step 1: Write the failing tests** — `seal_bytes` then `open_bytes` round-trips arbitrary bytes (incl. 32 random bytes); a different `key_id` cannot open it (AAD mismatch → raises). ObjectStore: put a chunk, get it back, list includes its id, delete removes it; a second org's ref cannot be read (prefix isolation). Use `moto` `mock_aws()` like `tests/test_storage.py`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Mirror `vault.py`'s `_encrypt_with_dek`/`_decrypt_with_dek` but for bytes (no `.encode()/.decode()`); reuse `_org_dek`. Mirror `ObjectStore.put_job_artifact` for the backup methods; `list_backup_chunks` uses the S3 `list_objects_v2` under the chunks prefix (via `_run`).
- [ ] **Step 4: Run → PASS**, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): vault seal_bytes/open_bytes + ObjectStore backup layout`.

---

### Task 4: Repository provisioning + policy resolution + GFS selection

**Files:**
- Create: `backend/app/modules/backup/repository.py`, `backend/app/modules/backup/policy.py`, `backend/app/modules/backup/retention.py`
- Test: `backend/tests/test_backup_repository.py`, `backend/tests/test_backup_policy.py`, `backend/tests/test_backup_retention.py`

**Interfaces:**
- Produces:
  - `repository.py`: `async def create_repository(session, vault, *, org_id, actor_id, name, dedup_params=DEFAULT_DEDUP) -> BackupRepository` — generates a random 32-byte master key, `vault.seal_bytes(...)` it (key_id = repository id), stores `encryption_key_ref` + `storage_ref = f"{org_id}/backup/{id}"`; audits `backup.repository_created`. `async def open_repo_key(session, vault, *, org_id, repository_id) -> bytes` — org-scoped load + `vault.open_bytes(...)`. `DEFAULT_DEDUP` dict (Global Constraints).
  - `policy.py`: `ResolvedBackupPolicy` frozen dataclass (`repository_id`, `sources`, `window_cron`, `retention_gfs`) + `async def resolve_backup_policy(session, org_id, device_id) -> ResolvedBackupPolicy | None` (device-group → org-default `.order_by(id).limit(1)` → None if no policy).
  - `retention.py`: `def select_gfs(points: list[BackupPoint], gfs: dict, now: datetime) -> tuple[set[UUID], set[UUID]]` — pure function returning `(keep_ids, prune_ids)` per GFS keep-counts (daily/weekly/monthly/yearly), newest-first within each bucket. No DB.

- [ ] **Step 1: Write failing tests** — repo: created row has `encryption_key_ref` set and the key round-trips via `open_repo_key` (== the generated 32 bytes); the master key is NOT stored plaintext (assert the DB column holds the sealed ref, not raw bytes); `backup.repository_created` audit present; tenancy (org B can't open org A's repo key). policy: device-group policy wins over org-default; duplicate org-default NULL rows resolve deterministically; no policy → None. retention: given 10 daily points and `{daily:7}`, keep the 7 newest, prune 3; a point that is also the newest weekly is kept even if outside the daily window (hand-compute a fixture).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Every query org-scoped.
- [ ] **Step 4: Run → PASS**, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): repository provisioning + policy resolution + GFS selection`.

---

### Task 5: Wire contracts + pinned repository-format schemas

**Files:**
- Create: `backend/app/modules/backup/schema.py`
- Modify: `backend/pyproject.toml` (add `blake3>=0.4`), `scripts/export_wire_schema.py`
- Test: `backend/tests/test_backup_schema.py`

**Interfaces:**
- Produces (all `_Wire` base, `ConfigDict(extra="forbid")`): `BackupRunParams` (`repository_id`, `storage_ref`, `sources: dict`, `dedup_params: dict`, `repo_key: str` [base64], `snapshot: Literal["vss","lvm","apfs"]`), `BackupRunResult` (`state: Literal["success","failed"]`, `manifest_ref: str | None`, `bytes_protected: int`, `bytes_stored: int`, `chunk_count: int`, `dedup_stats: dict`, `error: str | None`), `BackupRestoreParams` (`repository_id`, `storage_ref`, `manifest_ref`, `selection: list[str]`, `target: dict`, `mode: Literal["restore","verify"]`, `repo_key: str`), `BackupRestoreResult` (`state`, `restored_bytes: int`, `verified: bool`, `mismatch: list[str] | None`, `error: str | None`). Format models (documentation/agent contract): `ManifestEntry` (`path`, `kind: Literal["file","dir","symlink"]`, `mode: int`, `size: int`, `mtime: float`, `chunk_ids: list[str] = []`, `target: str | None = None`), `FiletreeStream` (`type: Literal["filetree"]`, `entries: list[ManifestEntry]`), `BackupManifest` (`schema_version: int`, `streams: list[FiletreeStream]`).

- [ ] **Step 1: Write the failing test** — construct each model; `extra="forbid"` rejects unknown keys; invalid `snapshot`/`mode`/`kind`/`state` literals rejected; `BackupManifest` round-trips a filetree with one file entry (chunk_ids) + one dir; `repo_key` is a plain `str` field.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `schema.py`**; add `blake3` dep; register the models in `scripts/export_wire_schema.py` and run it → `BackupRunParams.schema.json` etc. + `BackupManifest.schema.json` land in `docs/kernel/wire-protocol/`.
- [ ] **Step 4: Run → PASS**, `.venv/bin/pip install -e '.[dev]'` picks up blake3, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): wire contracts + pinned repository-format schemas`.

---

### Task 6: `backup.run` dispatch (window-gated, signed, key-scoped, publish-after-commit)

**Files:**
- Create: `backend/app/modules/backup/dispatch.py`
- Test: `backend/tests/test_backup_dispatch.py`

**Interfaces:**
- Consumes: `resolve_backup_policy`, `open_repo_key`, `window_open`, the job engine.
- Produces: `async def dispatch_backup(session, job_engine, vault, *, org_id, device_id, now, actor_id=None, actor_type="system") -> list[tuple[UUID, UUID, dict]]` — resolves the device's policy (None → return `[]`); if `policy.window_cron` set and `window_open(...)` False → `[]`; loads the repo, opens the repo key, builds `BackupRunParams` (repo_key = base64 of the opened key), creates a signed `backup.run` job (`requested_by=actor_id`), creates a `BackupRun(state="running", job_id, actor_type)` row, audits `backup.dispatched` (actor threaded; **repo_key NEVER in metadata**), and RETURNS the `(job_id, device_id, envelope)` triple(s) for the CALLER to publish post-commit. Every query org-scoped.

- [ ] **Step 1: Write the failing test** — device with a windowed policy, window open → one signed `backup.run` job (assert `job.signature`), a `BackupRun` row `state="running"`, `backup.dispatched` audit with `actor_type="user"` when passed AND assert the audit metadata does NOT contain the repo key; closed window → `[]`, no job; no policy → `[]`; tenancy (another org's device/repo not touched). Assert the function does NOT itself publish (returns triples).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** (mirror `deploy/dispatch.py` for the triple-return + org-scoping; open the key via vault at dispatch).
- [ ] **Step 4: Run → PASS**, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): backup.run dispatch (signed, key-scoped, window-gated)`.

---

### Task 7: `backup.run` result ingestion + backup_point + registration

**Files:**
- Create: `backend/app/modules/backup/handler.py`
- Modify: `backend/app/modules/backup/dispatch.py` (add `ingest_backup_result`)
- Test: `backend/tests/test_backup_result.py`

**Interfaces:**
- Produces: `async def ingest_backup_result(session, job, result: BackupRunResult) -> None` — finds the `BackupRun` for `job.id` (org-scoped); on `success` sets it `success` + `bytes_protected`/`bytes_stored`/`chunk_count`/`finished_at` and creates a `BackupPoint(state="complete", manifest_ref, size=bytes_protected, dedup_stats, taken_at)`; on `failed`/out-of-domain state sets `BackupRun.state="failed"` + `error` (coerce unknown states to `failed`, record in audit — the Task-8-deploy lesson); audits `backup.result` (`actor_type="system"`). `def register_job_types()` registering `JobType("backup.run", BackupRunParams, result_handler=<handle_backup_result>)` (validates `BackupRunResult(**result)`→module exc, calls ingest, `queue_ui_event("backup_status_updated", …)`). Every query org-scoped.

- [ ] **Step 1: Write the failing test** — TDD RED via ImportError; then a dispatched `BackupRun` + a job, feed `BackupRunResult(state="success", manifest_ref="…", bytes_protected=1000, bytes_stored=400, chunk_count=5, dedup_stats={})` → run flips `success`, a `BackupPoint` row is created `complete` with the manifest_ref; a `failed` result → run `failed`, no point; a no-matching-row graceful skip; an out-of-domain state coerced to `failed`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** (mirror `deploy/handler.py` + `ingest_deploy_result`; registration into register_all/gateway/worker deferred to Tasks 12/13).
- [ ] **Step 4: Run → PASS**, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): backup.run result ingestion + backup_point + job-type registration`.

---

### Task 8: Restore dispatch + manifest browse

**Files:**
- Create: `backend/app/modules/backup/restore.py`
- Test: `backend/tests/test_backup_restore.py`

**Interfaces:**
- Produces: `async def browse_manifest(session, object_store, vault, *, org_id, backup_point_id) -> BackupManifest` — org-scoped load of the point, `get_backup_object(manifest_ref)`, decrypt with the repo key, parse `BackupManifest` (metadata only — NO file contents). `async def dispatch_restore(session, job_engine, vault, *, org_id, backup_point_id, selection, target, mode, now, actor_id=None, actor_type="system") -> list[tuple[UUID, UUID, dict]]` — builds `BackupRestoreParams` (repo_key opened at dispatch, mode restore|verify), signs a `backup.restore` job, creates a `RestoreJob(state="running", job_id, mode, selection, target, actor_type)`, audits `backup.restore_dispatched` (actor threaded, repo_key NOT in metadata), returns triples for post-commit publish. Every query org-scoped.

- [ ] **Step 1: Write the failing test** — seed a point whose encrypted manifest (written via the fake chunker helper) decrypts to a known filetree; `browse_manifest` returns the entries (and NEVER touches file contents). `dispatch_restore(mode="restore")` → one signed `backup.restore` job + a `RestoreJob` `state="running"`, `backup.restore_dispatched` audit `actor_type="user"` with NO repo_key in metadata; `mode="verify"` sets `RestoreJob.mode=="verify"`; tenancy.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run → PASS**, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): restore dispatch + manifest browse`.

---

### Task 9: Restore result ingestion + proven-restore status + registration

**Files:**
- Modify: `backend/app/modules/backup/restore.py` (add `ingest_restore_result`), `backend/app/modules/backup/handler.py` (register `backup.restore`)
- Test: `backend/tests/test_backup_restore_result.py`

**Interfaces:**
- Produces: `async def ingest_restore_result(session, job, result: BackupRestoreResult) -> None` — finds the `RestoreJob` (org-scoped); sets state `success`/`failed` + `restored_bytes`/`finished_at`/`error` (coerce out-of-domain → failed); if `mode=="verify"` AND `result.verified` is True AND no `mismatch`, set the linked `BackupPoint.restore_tested_at = now`; audits `backup.restore_result` (`system`, includes `verified`/`mismatch` count but no data). `register_job_types()` ALSO registers `JobType("backup.restore", BackupRestoreParams, result_handler=<handle_restore_result>)` (keep `backup.run` registration).

- [ ] **Step 1: Write the failing test** — a `verify`-mode RestoreJob + `BackupRestoreResult(state="success", restored_bytes=1000, verified=True, mismatch=None)` → RestoreJob `success` AND the point's `restore_tested_at` is set; a `verify` with `mismatch=["/a"]` → success but `restore_tested_at` NOT set; a `restore`-mode success does NOT set `restore_tested_at`; a `failed` result → RestoreJob `failed`; both `backup.run` and `backup.restore` are registered after `register_job_types()`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run → PASS**, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): restore result ingestion + proven-restore status + script.restore registration`.

---

### Task 10: Repository GC (mark-and-sweep) + integrity verification

**Files:**
- Create: `backend/app/modules/backup/gc.py`
- Test: `backend/tests/test_backup_gc.py`

**Interfaces:**
- Produces: `async def gc_repository(session, object_store, vault, *, org_id, repository_id, now) -> int` — the mark-and-sweep GC: gather every chunk ID referenced by all live (`state != "pruned"`) points' manifests for the repo, list stored chunks via `list_backup_chunks`, DELETE unreferenced chunks (returns deleted count); audits `backup.gc`. `async def verify_point(session, object_store, vault, *, org_id, backup_point_id, now) -> bool` — re-read the point's manifest + a sampling of its chunks, verify BLAKE3 IDs; on success set `BackupPoint.verified_at = now`; return ok. Every query org-scoped; operates only within the org's object prefix.

- [ ] **Step 1: Write the failing test** (THE load-bearing correctness test) — build a repo with 2 points sharing some chunks; mark one point `pruned`; write an extra orphan chunk not referenced by any live manifest. `gc_repository` deletes ONLY the orphan + chunks referenced solely by the pruned point, and KEEPS every chunk still referenced by the live point (assert both directions). `verify_point` on a good point sets `verified_at` and returns True; corrupt a chunk (rewrite its object with wrong bytes) → `verify_point` returns False and does NOT set `verified_at`. Tenancy: GC never touches another org's prefix.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run → PASS**, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): repository GC mark-and-sweep + integrity verification`.

---

### Task 11: Reporting

**Files:**
- Create: `backend/app/modules/backup/reporting.py`
- Test: `backend/tests/test_backup_reporting.py`

**Interfaces:**
- Produces (read-only, every query org-scoped, no writes): `async def device_backup_status(session, org_id, device_id) -> dict` (last-good point, last run state, verified_at/restore_tested_at, repository); `async def fleet_backup_report(session, org_id) -> dict` (coverage %, success rate, points needing verification, dedup savings); `async def backup_evidence_rows(session, org_id) -> list[dict]` (per-device coverage + tested-restore status for SOC 2/BCP export).

- [ ] **Step 1: Write the failing test** — seed devices with mixed run states + points (some verified/restore-tested); assert `device_backup_status` fields; `fleet_backup_report` coverage/success-rate math against hand-computed values; `backup_evidence_rows` one row per device with the tested-restore status; tenancy (second org's rows never appear). Mirror `deploy/reporting.py`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run → PASS**, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): per-device + fleet reporting + SOC2 evidence rows`.

---

### Task 12: API routes + module/gateway registration

**Files:**
- Create: `backend/app/modules/backup/api.py`
- Modify: `backend/app/modules/backup/__init__.py` (`register(app)`), `backend/app/modules/__init__.py` (`register_all`), `backend/app/gateway/main.py`
- Test: `backend/tests/test_backup_api.py`

**Interfaces:**
- Produces: routes under `/api/v1/backup` per spec §12, each RBAC-gated; Pydantic response models with **NO secret fields** (never return `repo_key`/`encryption_key_ref`). `POST /devices/{id}/run` and `POST /points/{id}/restore`|`/verify` compute `now=datetime.now(UTC)` in-handler, pass `actor_id=ctx.user_id, actor_type="user"`, dispatch via the service fns, and **publish the returned triples AFTER the `org_session` block commits**. `GET /points/{id}/manifest` returns the browse result (metadata only). `register(app)` calls `register_job_types()` then mounts the router; `org_id`/`actor_id` from `AuthContext`.

- [ ] **Step 1: Write the failing test** (use `api_env` like `tests/test_deploy_api.py`) — `GET /repositories` OK for read-only; `POST /repositories` 403 for technician, OK for org-admin; `POST /devices/{id}/run` OK for technician (`backup.run`), dispatches (fake redis sees a publish AFTER commit) and audits `backup.dispatched` `actor_type="user"`; `POST /points/{id}/restore` OK for technician (`backup.restore`); `GET /points/{id}/manifest` returns entries and NO secret fields; assert NO response body anywhere contains `repo_key`/`encryption_key_ref`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** + wire `register(app)`, `register_all`, gateway registration (mirror deploy). 
- [ ] **Step 4: Run → PASS**, then FULL suite (touches shared wiring), mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): API routes, module + gateway registration`.

---

### Task 13: Worker sweeps + worker registration (with un-masking test)

**Files:**
- Modify: `backend/app/worker.py`
- Test: `backend/tests/test_backup_worker.py`

**Interfaces:**
- Produces: `async def backup_window_sweep_all_orgs(now) -> int` (per org, dispatch `backup.run` for due windowed policies via `dispatch_backup(... actor_type="system")`, publish per-org after commit — mirror `deploy_window_sweep_all_orgs`); `async def backup_retention_gc_all_orgs(now) -> int` (per org+repo: GFS-mark superseded points `pruned`, then `gc_repository`); `async def backup_verification_sweep_all_orgs(now) -> int` (dispatch/`verify_point` for stale points). Beat entries `backup-window-sweep`@`900.0`, `backup-retention-gc`@`86400.0`, `backup-verification`@`86400.0`; task names `app.worker.backup_window_sweep`/`backup_retention_gc`/`backup_verification`. `register_worker_job_types()` ALSO registers `backup.run`+`backup.restore`.

- [ ] **Step 1: Write the failing tests** — (a) two orgs each with a due windowed policy → `backup_window_sweep_all_orgs(now)` visits both and sums; closed window dispatches nothing. (b) `backup_retention_gc_all_orgs` marks superseded points pruned then deletes their now-orphan chunks (uses Task 10). (c) **UN-MASKING test:** pop `backup.run`/`backup.restore` from `app.jobs.registry._registry`, assert `UnknownJobType`, then drive `worker.register_worker_job_types()` (and `worker.create_celery()`), assert both resolve — mirror `tests/test_deploy_worker.py`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** (only the Celery task shells compute `datetime.now(UTC)`; publish after commit).
- [ ] **Step 4: Run → PASS**, FULL suite green, mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(backup): window/retention-gc/verification sweeps + worker registration`.

---

### Task 14: Frontend feature folder

**Files:**
- Create: `frontend/src/modules/backup/RepositoriesPage.tsx`, `BackupPolicyEditor.tsx`, `BackupPointsPage.tsx`, `RestoreDialog.tsx`, `DeviceBackupTab.tsx`, `api.ts`, `types.ts`, `backupState.ts`
- Modify: `frontend/src/lib/api.ts`, `frontend/src/App.tsx` (nav gated on `backup.read` + routes), `frontend/src/modules/inventory/DeviceDetailPage.tsx` (backup tab)
- Test: `frontend/src/modules/backup/__tests__/backup.test.tsx`

**Interfaces:**
- `types.ts` mirrors backend response models EXACTLY (no secret fields); `backupState.ts` state→Badge maps keyed on the REAL backend vocabularies (runs: pending/running/success/failed; points: complete/failed/pruned; restore: pending/running/success/failed) — NO invented names (Phase-5 lesson). ApiClient methods on the central client; module `api.ts` hooks with query-key invalidation after mutations (NO live event). Pages: `RepositoriesPage` + `BackupPolicyEditor` (`backup.policy.manage`-gated), `BackupPointsPage` (points + verified/restore-tested badges), `RestoreDialog` (browse manifest tree → select → restore/verify, `backup.restore`-gated), `DeviceBackupTab` (`backup.read`). Strict TS, no `any`.

- [ ] **Step 1: Write the failing test** (mirror `deploy.test.tsx`) — `BackupPointsPage` lists points; the Run-now control calls the run mutation and is hidden without `backup.run`; the Restore control hidden without `backup.restore`; a `backupState` variant check against a REAL backend state (e.g. `success`).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** pages + nav/routes + device tab.
- [ ] **Step 4:** `npm test`, `npm run typecheck`, `npm run build` all green.
- [ ] **Step 5: Commit** `feat(backup): repositories/points/restore UI + device backup tab`.

---

### Task 15: Deploy / docs

**Files:**
- Modify: `deploy/.env.example`, `deploy/README.md`
- Test: `cd deploy && cp .env.example .env && docker compose config >/dev/null && echo OK && rm -f .env`

- [ ] **Step 1:** Document (comments; no new compose services, prefer no new `config.py`): per-tenant repository object-storage layout + capacity implications, envelope key management via the vault (repo master key sealed, job-scoped `repo_key`), the three beat sweeps (window @900s / retention-GC @daily / verification @daily), and the site-server LAN-cache seam (not built in v1).
- [ ] **Step 2: Validate** compose config prints OK.
- [ ] **Step 3: Commit** `docs(backup): deploy notes — repository layout, key management, sweeps`.

---

## Self-Review

**Spec coverage:** §4 data model → T1/T2 ✓; §5 repository format → T5 (schemas) + T3 (storage layout) ✓; §6 wire contracts → T5 ✓; §7 encryption/keys → T3 (vault bytes) + T4 (repo key) + T6/T8 (job-scoped repo_key) ✓; §8 scheduling/GFS/GC → T4 (GFS select) + T13 (window+gc sweeps) + T10 (gc impl) ✓; §9 restore/verify → T8 + T9 + T10 (integrity) ✓; §10 reporting → T11 ✓; §11 RBAC → T1 (seed) + T12 (enforcement) ✓; §12 API → T12 ✓; §13 worker + registration → T13 ✓; §14 frontend → T14 ✓; §15 invariants+idle budget → signed dispatch (T6/T8), keys vault-sealed/job-scoped/never-returned (T3/T4/T6/T8/T12 tests), tenancy (all), audit+actor (T6/T7/T8/T9/T12), RBAC (T1/T12) ✓; §16 testing (fake chunker) → T7/T8/T9/T10 use the deterministic helper ✓; §17 deploy → T15 ✓. Accumulated lessons: publish-after-commit (T6/T8/T12/T13), actor threading (T6/T7/T8/T9/T12), worker registration + un-masking test (T13), frontend real vocab (T14) ✓.

**Placeholder scan:** all tasks name concrete files, exact signatures, and vocabularies from Global Constraints; the fake-chunker helper is introduced in T7 and reused. No TBD/TODO.

**Type consistency:** `dispatch_backup`/`ingest_backup_result`/`BackupRunParams`/`BackupRunResult`/`dispatch_restore`/`ingest_restore_result`/`BackupRestoreParams`/`BackupRestoreResult`/`BackupManifest`/`gc_repository`/`verify_point`/`resolve_backup_policy`/`open_repo_key`/`seal_bytes`/`open_bytes`/`register_job_types`/`register_worker_job_types` are used consistently across tasks. State vocabularies fixed in Global Constraints and used verbatim. The dispatch triple-return `(job_id, device_id, envelope)` + publish-after-commit is consistent across T6/T8/T12/T13.
