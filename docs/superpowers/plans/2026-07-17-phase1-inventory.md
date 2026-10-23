# Phase 1 — Inventory Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Control-plane inventory: ingest `inventory.collect` results into normalized tables with change history and dynamic grouping, expose inventory APIs/CSV exports, and ship the device-detail + fleet-software UI — per `docs/modules/inventory/spec.md`.

**Architecture:** A `backend/app/modules/inventory/` package registering into the kernel via one new extension point (`JobType.result_handler`, invoked by `JobEngine.complete` in the job's tenant transaction). Migration 0005 adds four RLS-forced tables; the Phase 0 completeness test enforces their policies automatically.

**Tech Stack:** Same as kernel (Python 3.12/FastAPI/SQLAlchemy 2/Alembic; React TS strict). No new dependencies.

## Global Constraints

- All Phase 0 global constraints apply verbatim (mypy strict, ruff, real-Postgres tests, forward-only migrations, canonical JSON untouched).
- New tables carry `org_id` + `ENABLE`+`FORCE` RLS with the `tenant_isolation` policy pattern from migration 0002.
- Result documents validate with Pydantic `extra="ignore"`; malformed documents fail the job transactionally — nothing half-ingests.
- Kernel files may only change at the defined extension point (`registry.py`, `engine.py` complete-hook, `main.py` module mount) — no forks of security/tenancy code.

---

### Task 1: Kernel extension point — `JobType.result_handler`

**Files:** Modify `backend/app/jobs/registry.py`, `backend/app/jobs/engine.py`; Test `backend/tests/test_result_handler.py`

**Interfaces:**
- Produces: `JobType(name, params_model, result_handler: ResultHandler | None = None)` where `ResultHandler = Callable[[AsyncSession, Job, dict[str, Any]], Awaitable[None]]`; `JobEngine.complete` awaits `job_type.result_handler(session, job, result)` after a `succeeded` transition (not on `failed`), before audit; handler exceptions flip the job to `failed` with `{"error": str(exc)}` instead of raising.

**Steps:**
- [ ] Failing tests: handler invoked with session/job/result on success; not invoked on failure; raising handler ⇒ job `failed`, result carries error, no exception escapes.
- [ ] Implement; run `pytest tests/test_result_handler.py -v` → PASS; full suite green.
- [ ] Commit `feat(jobs): result-handler extension point for modules`.

### Task 2: Inventory models + migration 0005

**Files:** Create `backend/app/models/inventory.py`, `backend/migrations/versions/0005_inventory.py`; Modify `backend/app/models/__init__.py`; Test extend `backend/tests/test_migrations.py`

**Interfaces:**
- Produces models exactly per spec §4: `InventorySnapshot` (`raw: dict`, `software_count`, `changes_count`, `taken_at`), `InstalledSoftware` (unique `(device_id, name)`), `InventoryChange` (`change_type`, `subject`, `from_value`, `to_value`, `ts`), `HardwareComponent` (unique `(device_id, type, identifier)`, `attributes: dict`).

**Steps:**
- [ ] Extend `EXPECTED_TABLES` with the four names → run → FAIL.
- [ ] Write models; autogenerate 0005 against a temp DB; append the RLS block (ENABLE+FORCE+`tenant_isolation` policy per table, same SQL as 0002).
- [ ] Suite green (RLS completeness test covers the new tables automatically). Commit `feat(inventory): tables and migration`.

### Task 3: Result document schema + export

**Files:** Create `backend/app/modules/__init__.py`, `backend/app/modules/inventory/__init__.py`, `backend/app/modules/inventory/schema.py`; Modify `scripts/export_wire_schema.py`; Test `backend/tests/test_inventory_schema.py`

**Interfaces:**
- Produces `InventoryDocument` per spec §5 (`hostname`, `os_family` pattern-validated, `hardware.components: list[ComponentDoc]`, `software: list[SoftwareDoc]` with optional `publisher`/`install_date: date`, `facts: dict[str, Any]`), `extra="ignore"` throughout.

**Steps:**
- [ ] Failing tests: full document parses; unknown extra keys ignored; bad `os_family` rejected; `install_date` parses ISO date; missing `software` defaults to `[]`.
- [ ] Implement; add `InventoryDocument` to the export script; regenerate `docs/kernel/wire-protocol/`. Commit `feat(inventory): result document contract`.

### Task 4: Ingestion service + rules + module registration

**Files:** Create `backend/app/modules/inventory/ingest.py`, `backend/app/modules/inventory/rules.py`; Modify `backend/app/modules/inventory/__init__.py` (register `inventory.collect` result handler + UI event publish), `backend/app/main.py` (call `app.modules.register_all()`); Test `backend/tests/test_inventory_ingest.py`, `backend/tests/test_inventory_rules.py`

**Interfaces:**
- Produces `async def ingest_inventory(session, device: Device, doc: InventoryDocument) -> IngestStats` (`dataclass: software_added, software_removed, software_updated, changes: int`); `InventoryValidationError`; `async def evaluate_rules(session, device, doc) -> UUID | None` (winning group id); handler registered via `register(JobType("inventory.collect", InventoryCollectParams, result_handler=_handle_result))` replacing the Phase 0 bare registration.

**Steps (ingest):**
- [ ] Failing tests per spec §6/§10: first sweep populates Device summary + software + components + snapshot, zero changes; second sweep (one install, one removal, one version bump, os_version change, disk_encryption flip) yields exactly 5 `inventory_changes` rows with correct from/to; malformed raw ⇒ job `failed`, tables untouched; 11th snapshot prunes to 10; default inventory `Policy` seeded once.
- [ ] Implement `ingest.py` + handler wiring; PASS.
- [ ] Commit `feat(inventory): transactional ingestion with change history`.

**Steps (rules):**
- [ ] Failing tests per spec §7: each op (`eq/ne/contains/gt/lt`); `facts.*` and `software:<name>` paths (absent software: `ne` matches, others don't); first-match by group-name order; manual placement preserved when no rule group matches; malformed rule skipped without error.
- [ ] Implement `rules.py`, call from ingest; PASS. Commit `feat(inventory): dynamic device-group rules`.

### Task 5: Inventory API + CSV exports

**Files:** Create `backend/app/modules/inventory/api.py`; Modify `backend/app/main.py` (mount); Test `backend/tests/test_inventory_api.py`

**Interfaces:**
- Produces routes per spec §8 with response models `DeviceInventoryOut`, `InventoryChangeOut`, `SoftwareAggregateOut {name, version, publisher, device_count}`, `SoftwareDeviceOut {device_id, hostname, version}`; CSV endpoints stream `text/csv` with header rows `hostname,os_family,os_version,architecture,serial,agent_status,last_seen_at` and `hostname,name,version,publisher`.

**Steps:**
- [ ] Failing tests: RBAC 403 without `devices.read`; detail 404 for foreign-org device; software search filters by substring and aggregates device_count; changes paginate newest-first; both CSVs parse via `csv.reader` with expected headers + rows.
- [ ] Implement; PASS. Commit `feat(inventory): API routes and CSV exports`.

### Task 6: E2E — fake agent answers inventory.collect

**Files:** Modify `backend/tests/test_e2e_kernel.py` (extend flow)

**Steps:**
- [ ] Extend the kernel e2e: after `hello`, dispatch `inventory.collect`; `FakeAgentRunner` returns a realistic `InventoryDocument` dict; assert device summary updated, software rows present, `GET /devices/{id}/inventory` returns them, and an `inventory_updated` UI event was published to `ui:{org}`.
- [ ] PASS ×3 runs (stability). Commit `feat(inventory): end-to-end sweep through the signed-job loop`.

### Task 7: Frontend — device detail + software pages

**Files:** Create `frontend/src/modules/inventory/DeviceDetailPage.tsx`, `frontend/src/modules/inventory/SoftwarePage.tsx`, `frontend/src/modules/inventory/factBadges.ts`; Modify `frontend/src/lib/api.ts` (inventory types + methods), `frontend/src/App.tsx` (routes `/devices/:id`, `/software`), `frontend/src/modules/devices/DevicesPage.tsx` (row link + CSV button); Test `frontend/src/modules/inventory/factBadges.test.ts`, extend `frontend/src/lib/api.test.ts`

**Interfaces:**
- Consumes Task 5 response shapes as TS types (`DeviceInventory`, `InventoryChange`, `SoftwareAggregate`).
- Produces `factBadge(key: string, value: unknown): {label: string; variant: BadgeVariant} | null` — encryption/firewall/domain true ⇒ success, false ⇒ warning, unknown keys ⇒ null.

**Steps:**
- [ ] Failing vitest: `factBadge` mapping (true/false/null cases); api client hits `/devices/:id/inventory`.
- [ ] Implement pages (tabs: Hardware/Software/Changes/Jobs per spec §9, `useLiveEvents` refresh on `inventory_updated`); `npm test`, `tsc --noEmit`, `npm run build` green.
- [ ] Commit `feat(inventory): device detail and fleet software UI`.

---

## Self-review

- **Coverage:** spec §3→T1/T4 (registration), §4→T2, §5→T3, §6→T4, §7→T4, §8→T5, §9→T7, §10→every task + T6. Default-policy seeding lives in T4 ingest tests. No gaps.
- **Placeholders:** none of the banned patterns; every task names exact files, signatures, and expected test behavior.
- **Type consistency:** `IngestStats` (T4) only used internally; `ResultHandler` signature identical in T1/T4; CSV headers identical in T5 tests and implementation; TS types in T7 mirror T5 models.
