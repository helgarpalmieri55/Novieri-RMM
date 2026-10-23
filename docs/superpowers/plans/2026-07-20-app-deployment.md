# Application Deployment (Phase 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Application Deployment module — push installs/updates/uninstalls and run signed scripts to endpoints from the console, inventory-driven, windowed, and fully audited — on the existing kernel, reusing Phase 4's per-OS dispatch/window/result plumbing.

**Architecture:** Two new signed job types (`app.deploy`, `script.run`) register into the operator app, gateway, and Celery worker. A generic package registry stores installers in object storage (sha256-verified); deployments target a device or group, skip devices already in the desired state via inventory detection, dispatch signed per-device jobs (optionally within a cron maintenance window), and ingest structured results. Scripts are versioned, RBAC-gated objects whose body rides inside the signed job envelope. Results conform to a shared `InstallOutcome` contract that patch also adopts.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async/asyncpg), Alembic, Pydantic v2, Celery+Redis, MinIO (object storage), React + TypeScript (strict) + Vite + TanStack Query, vitest.

## Global Constraints

Every task's requirements implicitly include this section. Copy exact values verbatim.

- **Security invariant #1 — signed jobs only.** `app.deploy` and `script.run` dispatch exclusively via the kernel job engine (`job_engine.create` + `signed_envelope` + `publish_job`). The installer coordinates and the **script body** travel inside the signed envelope. No unsigned or inline execution path exists — not for tests.
- **Security invariant #4 — audit.** Every privileged action writes to the hash-chained audit log: package create/delete, artifact upload, deployment create/dispatch/result, script author/dispatch/result. Operator actions carry the real `actor_id` with `actor_type="user"`; worker-initiated actions use `actor_type="system"`, `actor_id=None`. `actor_type` is `Literal["user", "agent", "system"]` (matches `app/core/audit.py`).
- **Security invariant #5 — tenancy.** EVERY `select()`/write over `packages`/`deployments`/`deployment_results`/`scripts`/`script_runs`/`Device` carries an explicit `org_id` predicate in addition to RLS. RLS is `ENABLE + FORCE` + a `tenant_isolation` policy `USING (org_id = current_setting('app.current_org', true)::uuid)` on all five tables. A missing tenant filter is a security bug, not a style nit. Every joined table gets its own `org_id` predicate.
- **Security invariant #6 — RBAC.** Server-side `Depends(require_permission("<code>"))` on every route; server-side permission check on every dispatch. Frontend gating is cosmetic only.
- **Job-type registration in ALL THREE processes.** `register(app)` (operator app via `register_all`), the gateway (`app/gateway/main.py` startup), AND the Celery worker (`register_worker_job_types()` in `app/worker.py`). Phase 4's whole-branch review caught a missing worker registration that made the sweeps raise `UnknownJobType` in production — this module MUST register in the worker and ship the un-masking test (Task 12).
- **State vocabularies (verbatim).** `deployment_results.state` ∈ {`pending`, `running`, `installed`, `failed`, `not_applicable`}. `script_runs.state` ∈ {`pending`, `running`, `succeeded`, `failed`}. `deployments.state` ∈ {`pending`, `dispatching`, `complete`, `failed`}. `packages.installer_type` ∈ {`msi`, `exe`, `winget`, `choco`, `pkg`, `dmg`, `deb`, `rpm`, `script`}. `os_family` ∈ {`windows`, `linux`, `macos`}. `action` ∈ {`install`, `update`, `uninstall`}. `interpreter` ∈ {`powershell`, `bash`, `python`}.
- **`InstallOutcome.state`** (shared contract) ∈ {`installed`, `failed`, `not_applicable`, `succeeded`}. Deploy outcomes use `installed`/`failed`/`not_applicable`; script outcomes use `succeeded`/`failed`.
- **Detection-rule grammar (v1, closed set).** `detection_rule`/`uninstall_rule` JSON `type` ∈ {`file_exists`, `registry_value`, `package_installed`, `command_exit_zero`}. No arbitrary code. Validated on write.
- **Reuse, do not fork.** `window_open`/cron parser (Task 3 lifts them to `app/core/schedule.py`); `JobType(name, params_model, result_handler=…)`; `ObjectStore`; the device-group→org-default resolution pattern; `queue_ui_event` for post-commit UI events (frontend refetches after mutation — NO live-event subscription).
- **Engineering conventions.** Async throughout; typed SQLAlchemy 2; Pydantic v2 with `ConfigDict(extra="forbid")` on wire models; business logic in services not routes; forward-only additive Alembic migration; `mypy` + `ruff` clean; frontend strict TS, no `any`. venv at `backend/.venv`. If Postgres/Redis are down: `pg_ctlcluster 16 main start; redis-server --daemonize yes`. Delete any stray `backend/uv.lock` (gitignored).

---

### Task 1: Migration + RBAC seed

**Files:**
- Create: `backend/migrations/versions/0013_app_deploy.py`
- Modify: `backend/app/core/rbac.py` (add `deploy.*` permissions + role grants), `backend/tests/test_rbac.py`, `backend/tests/test_api_routes.py` (sync permission set)
- Test: `backend/tests/test_migration_app_deploy.py` (RLS present on the 5 tables)

**Interfaces:**
- Produces: five tables (`packages`, `deployments`, `deployment_results`, `scripts`, `script_runs`) per spec §4; permissions `deploy.read`, `deploy.package.manage`, `deploy.create`, `deploy.execute`, `deploy.script.manage`, `deploy.script.run`.

- [ ] **Step 1: Write the failing test** — assert each of the 5 tables has `rowsecurity` true and a `tenant_isolation` policy (query `pg_tables`/`pg_policies`), mirroring `tests/test_migration_patch.py`.
- [ ] **Step 2: Run → FAIL** (`.venv/bin/python -m pytest tests/test_migration_app_deploy.py -v`).
- [ ] **Step 3: Write the migration.** `down_revision` = Phase 4's head (`0012`; verify with `.venv/bin/alembic heads`). Create the 5 tables per §4 (exact columns/types/UNIQUE/index from the spec). Copy the `_rls(op, table)` helper from `0011_patch.py` and apply to all five. Seed the 6 permissions into the permissions table and grant per §11: super-admin + org-admin → all 6; technician → `deploy.read`, `deploy.create`, `deploy.execute`, `deploy.script.run`; auditor → `deploy.read`; read-only → `deploy.read`.
- [ ] **Step 4: Sync `app/core/rbac.py`** — add the 6 permission codes+descriptions (spec §11) and the same role grants; update `tests/test_rbac.py` and `tests/test_api_routes.py` expected permission sets.
- [ ] **Step 5: Run → PASS**, `.venv/bin/alembic upgrade head` clean, `mypy`/`ruff` clean.
- [ ] **Step 6: Commit** `feat(deploy): migration 0013 — packages/deployments/results/scripts/runs tables, RLS, RBAC`.

---

### Task 2: ORM models

**Files:**
- Create: `backend/app/modules/deploy/__init__.py` (empty for now), `backend/app/modules/deploy/models.py`
- Test: `backend/tests/test_deploy_models.py`

**Interfaces:**
- Produces: `Package`, `Deployment`, `DeploymentResult`, `Script`, `ScriptRun` (SQLAlchemy 2 typed models); `__table_args__` mirroring the migration's UNIQUE constraints + named indexes exactly.

- [ ] **Step 1: Write the failing test** — insert one row per model under an `org_session`, read it back, assert defaults (`Deployment.state == "pending"`, `DeploymentResult.state == "pending"`, JSONB `detection_rule` round-trips). Mirror `tests/test_patch_models.py`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement models** matching §4 columns/types. Use `Mapped[...]`, `uuid7_pk()` for ids (see `app/models/core.py`), `TimestampMixin`, `JSONB` for rule columns (default `dict`), `Text` for `Script.body`. Register the module's models are imported by Alembic env if needed (mirror patch).
- [ ] **Step 4: Run → PASS**, `mypy`/`ruff` clean.
- [ ] **Step 5: Commit** `feat(deploy): ORM models (Package, Deployment, DeploymentResult, Script, ScriptRun)`.

---

### Task 3: Shared schedule module (refactor `window_open` out of patch)

**Files:**
- Create: `backend/app/core/schedule.py`
- Modify: `backend/app/modules/patch/apply.py` (import `window_open` from the new module; delete the local copy), any patch imports of `_parse_cron_field`
- Test: `backend/tests/test_schedule.py` (move/duplicate the cron-matcher tests here)

**Interfaces:**
- Produces: `window_open(cron: str | None, now: datetime) -> bool` and the private cron field parser, in `app/core/schedule.py`. Consumed by patch (existing) and deploy (Tasks 7/12).

- [ ] **Step 1: Write the failing test** — `tests/test_schedule.py` importing `from app.core.schedule import window_open`; cover `None` (always open), `*` fields, `*/n`, comma, ranges, DOM/DOW OR semantics, and a bounds-validation `ValueError`. (Lift the existing cases from `tests/test_patch_apply.py`'s window tests.)
- [ ] **Step 2: Run → FAIL** (module doesn't exist).
- [ ] **Step 3: Move the implementation.** Cut `window_open` + `_parse_cron_field` from `app/modules/patch/apply.py` into `app/core/schedule.py` verbatim; in `apply.py` replace with `from app.core.schedule import window_open`. Do not change behavior.
- [ ] **Step 4: Run → PASS**, plus run the FULL patch suite to prove the refactor didn't break patch (`.venv/bin/python -m pytest tests/test_patch_apply.py -q`). `mypy`/`ruff` clean.
- [ ] **Step 5: Commit** `refactor(core): lift window_open into app/core/schedule.py, shared by patch+deploy`.

---

### Task 4: Wire contracts + shared `InstallOutcome`

**Files:**
- Create: `backend/app/modules/deploy/schema.py`
- Modify: `backend/app/modules/patch/schema.py` (align `PatchApplyOutcome` with `InstallOutcome` OR re-export a shared base — see below), `scripts/export_wire_schema.py` invocation (export deploy schemas)
- Test: `backend/tests/test_deploy_schema.py`

**Interfaces:**
- Produces: `InstallOutcome` (shared: `identifier: str`, `state: Literal["installed","failed","not_applicable","succeeded"]`, `exit_code: int | None`, `log: str`, `log_ref: str | None`, `detected_after: bool | None`), `AppDeployParams`, `AppDeployResult` (`outcomes: list[InstallOutcome]`), `ScriptRunParams` (`interpreter`, `body`, `script_id`, `script_version`, `timeout_s`), `ScriptRunResult` (`outcome: InstallOutcome`). All `_Wire` base with `ConfigDict(extra="forbid")`.

- [ ] **Step 1: Write the failing test** — construct each model from a dict; assert `extra="forbid"` rejects an unknown key; assert an invalid `state` is rejected; assert `AppDeployParams` requires `action`/`installer_type`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `schema.py`.** Define `InstallOutcome` and the four job models. Place `InstallOutcome` where both modules can import it (e.g. `app/core/wire.py` or `app/modules/deploy/schema.py` imported by patch); align `patch/schema.py`'s apply-outcome to reuse it — keep patch's tests green (if aligning is risky, have patch import `InstallOutcome` and adapt its handler; verify the patch suite).
- [ ] **Step 4: Export schemas** — run `python scripts/export_wire_schema.py`; confirm `AppDeployParams.schema.json` etc. land in `docs/kernel/wire-protocol/`.
- [ ] **Step 5: Run → PASS**, patch suite green, `mypy`/`ruff` clean.
- [ ] **Step 6: Commit** `feat(deploy): wire contracts + shared InstallOutcome; align patch apply-outcome`.

---

### Task 5: Package registry + artifact storage

**Files:**
- Create: `backend/app/modules/deploy/packages.py`
- Modify: `backend/app/core/storage.py` (add `put_package_artifact`/`get_package_artifact`)
- Test: `backend/tests/test_deploy_packages.py`

**Interfaces:**
- Consumes: `Package` model, `ObjectStore`.
- Produces: `async def create_package(session, *, org_id, actor_id, name, version, os_family, installer_type, detection_rule, uninstall_rule, install_args=None) -> Package`; `async def store_artifact(session, object_store, *, org_id, package_id, data: bytes) -> Package` (sets `artifact_ref` + `artifact_sha256`, audits `deploy.package.artifact_uploaded`); `async def delete_package(session, *, org_id, actor_id, package_id) -> bool`.

- [ ] **Step 1: Write the failing test** — create a package; store an artifact from `b"MZ..."`; assert `artifact_sha256 == hashlib.sha256(data).hexdigest()` and `artifact_ref` set; audit row `deploy.package.artifact_uploaded` present; second-org cannot read it (tenancy). Validate `detection_rule` grammar rejects an unknown `type`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** `ObjectStore.put_package_artifact(org_id, package_id, data)` mirrors `put_job_artifact`. `create_package` validates os_family/installer_type/rule-grammar; `store_artifact` computes sha256 + stores + audits. Every query org-scoped.
- [ ] **Step 4: Run → PASS**, `mypy`/`ruff` clean.
- [ ] **Step 5: Commit** `feat(deploy): package registry + sha256 artifact storage`.

---

### Task 6: Detection-rule evaluation (server-side, inventory-driven)

**Files:**
- Create: `backend/app/modules/deploy/detect.py`
- Test: `backend/tests/test_deploy_detect.py`

**Interfaces:**
- Consumes: inventory models (`app/modules/inventory/models.py` — software list per device).
- Produces: `async def device_in_desired_state(session, *, org_id, device_id, action, detection_rule) -> bool` — for `install`/`update`: True if the detection rule already matches (skip); for `uninstall`: True if it does NOT match (already gone). Evaluates the closed rule grammar against inventory (`package_installed` → check device software; `file_exists`/`registry_value`/`command_exit_zero` → cannot be evaluated server-side from inventory → return False so the agent decides).

- [ ] **Step 1: Write the failing test** — seed a device whose inventory software includes `Acme 1.2`; `package_installed` rule for `Acme` + `install` → `True` (skip); + `uninstall` → `False`. `file_exists` rule → `False` (defer to agent). Tenancy: another org's device isn't visible.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the evaluator (org-scoped inventory query; closed grammar switch).
- [ ] **Step 4: Run → PASS**, `mypy`/`ruff` clean.
- [ ] **Step 5: Commit** `feat(deploy): inventory-driven detection-rule evaluation`.

---

### Task 7: Deployment dispatch (`app.deploy`) with actor threading

**Files:**
- Create: `backend/app/modules/deploy/dispatch.py`
- Test: `backend/tests/test_deploy_dispatch.py`

**Interfaces:**
- Consumes: `window_open` (Task 3), `device_in_desired_state` (Task 6), `Deployment`/`DeploymentResult`, `AppDeployParams`, the job engine.
- Produces: `async def dispatch_deployment(session, job_engine, redis, *, org_id, deployment_id, now, actor_id: UUID | None = None, actor_type: Literal["user","agent","system"] = "system") -> list[UUID]` — resolves target device set (device or group), for each device runs detection (skip → `deployment_result.state="not_applicable"`, no job), else builds+signs an `app.deploy` job (params from the package), creates a `deployment_results` row (`state="running"`, `job_id`), audits `deploy.dispatched` using `actor_id`/`actor_type`. If `deployment.window_cron` is set and `window_open(...)` is False, dispatch nothing. Returns dispatched job ids. EVERY query carries an explicit `org_id` predicate.

- [ ] **Step 1: Write the failing test** — deployment to a 2-device group where device A already has the package (detection skip → `not_applicable`, no job) and device B does not (→ one signed `app.deploy` job, `deployment_result.state="running"`); assert the job is signed (`job.signature`); assert `deploy.dispatched` audit row has `actor_type="user"`, `actor_id==caller` when passed; assert a closed window dispatches nothing; assert explicit org scoping (second org's deployment not touched).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** mirroring Phase 4 `dispatch_apply` (explicit `org_id` predicates on every query; post-commit publish; actor threading).
- [ ] **Step 4: Run → PASS**, `mypy`/`ruff` clean.
- [ ] **Step 5: Commit** `feat(deploy): dispatch install/update/uninstall as signed app.deploy jobs`.

---

### Task 8: Deploy result ingestion + `app.deploy` job-type registration

**Files:**
- Create: `backend/app/modules/deploy/handler.py`
- Modify: `backend/app/modules/deploy/dispatch.py` (add `ingest_deploy_result`)
- Test: `backend/tests/test_deploy_result.py`

**Interfaces:**
- Consumes: `AppDeployResult`, `DeploymentResult`, `Deployment`.
- Produces: `async def ingest_deploy_result(session, job, result: AppDeployResult) -> None` — updates the `deployment_result` row (state ∈ installed|failed|not_applicable, `exit_code`, `log_ref`, `detected_after`, `finished_at`), rolls `deployment.state` to `complete`/`failed` when all device rows are terminal, audits `deploy.result` (`actor_type="system"`). `def register_job_types() -> None` registering `JobType("app.deploy", AppDeployParams, result_handler=<deploy handler>)` (mirrors `app/modules/patch/handler.py`). Every query org-scoped (`DeploymentResult.org_id == job.org_id`, `Deployment.org_id == job.org_id`).

- [ ] **Step 1: Write the failing test** — TDD RED via ImportError, then: given a dispatched deployment + a device job, feed an `AppDeployResult(outcomes=[InstallOutcome(state="installed",...)])`; assert the `deployment_result` row flips to `installed` with exit_code/finished_at, the parent `deployment.state` rolls to `complete` when all devices terminal, and a `deploy.result` audit row exists. Add a no-matching-row graceful-skip test.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `ingest_deploy_result` + `register_job_types()` (registration into `register_all`/gateway/worker is Tasks 11/12).
- [ ] **Step 4: Run → PASS**, `mypy`/`ruff` clean.
- [ ] **Step 5: Commit** `feat(deploy): app.deploy result ingestion + job-type registration`.

---

### Task 9: Scripts service + `script.run`

**Files:**
- Create: `backend/app/modules/deploy/scripts.py`
- Modify: `backend/app/modules/deploy/handler.py` (register `script.run` + its result handler), `backend/app/modules/deploy/dispatch.py` or a new `script_dispatch` (keep files focused)
- Test: `backend/tests/test_deploy_scripts.py`

**Interfaces:**
- Produces: `async def author_script(session, *, org_id, actor_id, name, os_family, interpreter, body) -> Script` (new row, `version=1`; editing bumps version — `async def edit_script(...) -> Script`); audits `script.authored`. `async def run_script(session, job_engine, redis, *, org_id, deployment_target..., script_id, now, actor_id, actor_type) -> list[UUID]` — snapshots `body`+`version` into `ScriptRunParams`, builds+signs `script.run` jobs (body inside the signed envelope), creates `script_runs` rows, audits `script.dispatched` (actor). `async def ingest_script_result(session, job, result: ScriptRunResult) -> None` — updates `script_runs` (succeeded|failed), audits `script.result`. `register_job_types()` also registers `JobType("script.run", ScriptRunParams, result_handler=<script handler>)`.

- [ ] **Step 1: Write the failing test** — author a script (v1); edit → v2; run v2 → assert the dispatched job's signed params contain the exact `body` and `script_version==2` (the body-in-signed-envelope guarantee); `script.dispatched` audit has actor; feed a `ScriptRunResult(outcome=succeeded)` → `script_runs.state="succeeded"`. Tenancy on every query.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run → PASS**, `mypy`/`ruff` clean.
- [ ] **Step 5: Commit** `feat(deploy): signed script.run — author/version + dispatch body-in-envelope + result`.

---

### Task 10: Reporting

**Files:**
- Create: `backend/app/modules/deploy/reporting.py`
- Test: `backend/tests/test_deploy_reporting.py`

**Interfaces:**
- Produces: `async def deployment_report(session, org_id, deployment_id) -> dict` (per-device states + rollup counts + log_refs); `async def fleet_deploy_report(session, org_id) -> dict` (recent deployments with success rates + recent script-run history). Read-only; every query org-scoped; no writes.

- [ ] **Step 1: Write the failing test** — seed a deployment with 3 devices (installed/failed/not_applicable); assert `deployment_report` counts and per-device rows; assert `fleet_deploy_report` success-rate math against hand-computed values; tenancy: second org's rows never appear.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run → PASS**, `mypy`/`ruff` clean.
- [ ] **Step 5: Commit** `feat(deploy): per-deployment + fleet reporting`.

---

### Task 11: API routes + module/gateway registration

**Files:**
- Create: `backend/app/modules/deploy/api.py`
- Modify: `backend/app/modules/deploy/__init__.py` (`register(app)`), `backend/app/modules/__init__.py` (`register_all` → `deploy.register(app)`), `backend/app/gateway/main.py` (call deploy `register_job_types()` next to inventory/remote/patch)
- Test: `backend/tests/test_deploy_api.py`

**Interfaces:**
- Produces: routes under `/api/v1/deploy` per spec §12, each RBAC-gated; Pydantic response models (no secret fields). `POST /packages/{id}/artifact` accepts an upload. `POST /deployments/{id}/execute` and `POST /scripts/{id}/run` compute `now=datetime.now(UTC)` in the handler and pass `actor_id=ctx.user_id, actor_type="user"`. `register(app)` calls `register_job_types()` then mounts the router at `/api/v1`.

- [ ] **Step 1: Write the failing test** (use `api_env` like `tests/test_patch_api.py`) — `GET /packages` OK for read-only; `POST /packages` 403 for technician, OK for org-admin; `POST /deployments` OK for `deploy.create`; `POST /deployments/{id}/execute` dispatches (fake redis sees a publish) and audits `deploy.dispatched` with `actor_type="user"`; `POST /scripts` 403 for technician (needs `deploy.script.manage`), `POST /scripts/{id}/run` OK for technician (`deploy.script.run`).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `api.py`** + wire `register(app)`, `register_all`, gateway registration. Mirror `app/modules/remote/__init__.py` and `patch/api.py`. `org_id`/`actor_id` from `AuthContext`.
- [ ] **Step 4: Run → PASS**, then FULL suite (touches shared wiring), `mypy`/`ruff` clean.
- [ ] **Step 5: Commit** `feat(deploy): API routes, module + gateway registration`.

---

### Task 12: Worker window sweep + worker job-type registration (with un-masking test)

**Files:**
- Modify: `backend/app/worker.py` (add `deploy_window_sweep_all_orgs` + beat entry + task shell; EXTEND `register_worker_job_types()` to also register deploy job types)
- Test: `backend/tests/test_deploy_worker.py`

**Interfaces:**
- Produces: `async def deploy_window_sweep_all_orgs(now: datetime) -> int` — per org, dispatch scheduled deployments whose window is open via `dispatch_deployment(..., actor_type="system")`; returns total dispatched. Beat entry `deploy-window-sweep` @ `900.0`, task `app.worker.deploy_window_sweep`. `register_worker_job_types()` now also calls deploy's `register_job_types()`.

- [ ] **Step 1: Write the failing tests** — (a) two orgs each with a windowed deployment whose window is open → `deploy_window_sweep_all_orgs(now)` visits both and sums counts; a closed-window deployment dispatches nothing. (b) **Un-masking test:** pop `app.deploy`/`script.run` from `app.jobs.registry._registry`, assert `get_job_type("app.deploy")` raises `UnknownJobType`, then call `worker.register_worker_job_types()` (or drive `worker.create_celery()`) and assert both resolve — proving the WORKER bootstrap registers them (mirror `tests/test_patch_worker.py`'s bootstrap tests).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the sweep + beat/task shells (only the shell computes `datetime.now(UTC)`); extend `register_worker_job_types()` to call deploy registration.
- [ ] **Step 4: Run → PASS**, FULL suite green, `mypy`/`ruff` clean.
- [ ] **Step 5: Commit** `feat(deploy): window sweep + worker job-type registration`.

---

### Task 13: Frontend feature folder

**Files:**
- Create: `frontend/src/modules/deploy/PackagesPage.tsx`, `DeployPage.tsx`, `ScriptsPage.tsx`, `DeviceDeployTab.tsx`, `api.ts` (hooks), `types.ts`
- Modify: `frontend/src/lib/api.ts` (ApiClient methods, mirror the Phase-4 resolved pattern), `frontend/src/App.tsx` (nav gated on `deploy.read` + routes), `frontend/src/modules/inventory/DeviceDetailPage.tsx` (add the deploy tab)
- Test: `frontend/src/modules/deploy/__tests__/deploy.test.tsx`

**Interfaces:**
- `types.ts` mirrors backend response models EXACTLY (no secret fields). ApiClient methods on the central client (auth in `request<T>`); module `api.ts` = TanStack Query hooks with query-key invalidation after mutations (NO live event). `PackagesPage` (upload + registry, `deploy.package.manage`-gated), `DeployPage` (target/schedule/execute, `deploy.create`/`deploy.execute`), `ScriptsPage` (author + run, `deploy.script.manage`/`deploy.script.run`), `DeviceDeployTab` (per-device history, `deploy.read`). Strict TS, no `any`.

- [ ] **Step 1: Write the failing test** (mirror `remote/SessionsPage.test.tsx` setup) — `DeployPage` lists packages from a mocked api; the **Execute** control calls the execute mutation; Execute is hidden without `deploy.execute`; Script **Run** hidden without `deploy.script.run`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** pages + nav/routes + device tab.
- [ ] **Step 4:** `npm test`, `npm run typecheck`, `npm run build` all green.
- [ ] **Step 5: Commit** `feat(deploy): packages/deploy/scripts UI + device deploy tab`.

---

### Task 14: Deploy / docs

**Files:**
- Modify: `deploy/.env.example` (artifact storage note, sha256 integrity, site-server-cache seam), `deploy/README.md` (deploy is RBAC-gated + beat-driven window sweep; script bodies ride in the signed envelope)
- Test: `cd deploy && cp .env.example .env && docker compose config >/dev/null && echo OK && rm -f .env`

- [ ] **Step 1:** Update `.env.example` + `README.md` (no new compose services; prefer no new `config.py` settings). Document the package-artifact object storage, the sha256 integrity requirement, the site-server-cache seam, and the `deploy-window-sweep` beat cadence (900s).
- [ ] **Step 2: Validate** compose config prints OK.
- [ ] **Step 3: Commit** `docs(deploy): deploy notes — artifacts, integrity, window sweep`.

---

## Self-Review

**Spec coverage:** §4 data model → T1/T2 ✓; §5 wire contracts + shared `InstallOutcome` → T4 ✓; §6 registry/artifacts → T5 ✓; detection (§6/AD-06) → T6 ✓; §7 targeting/scheduling → T7 (+ shared window from T3) ✓; §8 dispatch+result → T7/T8 ✓; §9 scripts → T9 ✓; §10 reporting → T10 ✓; §11 RBAC → T1 (seed) + T11 (enforcement) ✓; §12 API → T11 ✓; §13 worker + shared-schedule refactor → T3 + T12 ✓; §14 frontend → T13 ✓; §15 security invariants → signed dispatch (T7/T9), tenancy (all), audit+actor (T7/T8/T9/T11), RBAC (T1/T11) ✓; §16 testing → each task test-first ✓; §17 deploy → T14 ✓. The three Phase-4 lessons: shared-schedule refactor (T3), worker registration + un-masking test (T12), human-vs-system actor threading (T7/T8/T9/T11) ✓.

**Placeholder scan:** All tasks point at concrete files, exact signatures, and named vocabularies from Global Constraints. Reuse pointers name real Phase-4 files. No TBD/TODO.

**Type consistency:** `dispatch_deployment`/`ingest_deploy_result`/`AppDeployParams`/`AppDeployResult`/`InstallOutcome`/`ScriptRunParams`/`ScriptRunResult`/`register_job_types`/`register_worker_job_types`/`window_open`/`device_in_desired_state` are used with consistent names and signatures across tasks. State vocabularies are fixed in Global Constraints and used verbatim throughout.
