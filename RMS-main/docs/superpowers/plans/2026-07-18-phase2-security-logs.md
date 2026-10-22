# Phase 2 — Security Log Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Central security-event collection: agents stream normalized logon/privilege events over the existing mTLS channel; the control plane dedupes, stores append-only, enforces audited retention, and serves search + SOC 2 evidence CSV + a Security console page — per `docs/modules/security-logs/spec.md`.

**Architecture:** New `security_events` wire message handled by the gateway's `handle_agent_message`, delegating to `app/modules/security_logs/ingest.py`. One RLS-forced table with `(device_id, event_uid)` dedupe. Retention runs in the worker via the admin engine (the app role cannot delete) and audits every sweep. No hash chain — deviation documented in spec §3.

**Tech Stack:** unchanged (FastAPI/SQLAlchemy 2/Alembic backend, React TS strict frontend). No new dependencies.

## Global Constraints

- All prior global constraints (mypy strict, ruff, real-Postgres tests, forced RLS on org_id tables, forward-only migrations).
- `security_events` grants: revoke UPDATE/DELETE from `app_user` (migration + conftest grants block).
- Taxonomy is closed: `logon_success|logon_failure|logoff|account_lockout|privilege_elevation|admin_group_change|policy_change`; unknown types reject the whole batch.
- Max 500 events per wire message, enforced at parse.
- Device identity always from the cert-bound connection, never message content.

---

### Task 1: Wire message + `logs.ship` job type

**Files:** Modify `backend/app/gateway/protocol.py`, `backend/app/jobs/registry.py`, `scripts/export_wire_schema.py`; Test extend `backend/tests/test_gateway_protocol.py`

**Interfaces:**
- Produces `SecurityEventDoc` (`event_uid: str(1..64)`, `ts: str`, `event_type: Literal[...taxonomy...]`, `username: str = ""`, `source: str = ""`, `outcome: Literal["success","failure","n_a"] = "n_a"`, `raw: dict = {}`) and `SecurityEventsMsg` (`v: Literal[1]`, `kind: Literal["security_events"]`, `events: list[SecurityEventDoc]` max_length=500), added to the `AgentMessage` union; `LogsShipParams` registered as job type `logs.ship` (no result handler).

**Steps:**
- [ ] Failing tests: message with 2 events parses; 501 events rejected; bad event_type rejected; `logs.ship` registered.
- [ ] Implement; export schemas (`SecurityEventsMsg.schema.json`); full suite green. Commit `feat(security-logs): wire contract and logs.ship job type`.

### Task 2: Model + migration 0006 (append-only for the app role)

**Files:** Create `backend/app/models/security.py`, `backend/migrations/versions/0006_security_events.py`; Modify `backend/app/models/__init__.py`, `backend/tests/test_migrations.py` (add table), `backend/tests/conftest.py` (revoke UPDATE/DELETE on security_events in grants)

**Interfaces:**
- Produces `SecurityEvent` model per spec §5 with unique `(device_id, event_uid)` and the three query indexes.

**Steps:**
- [ ] Extend EXPECTED_TABLES → FAIL; write model; autogen 0006 + append the standard RLS block + `REVOKE UPDATE, DELETE ON security_events FROM app_user`.
- [ ] New test `test_security_events_append_only` (UPDATE/DELETE as app_user raise DBAPIError). Suite green. Commit `feat(security-logs): security_events table, RLS, append-only grants`.

### Task 3: Ingestion service + gateway branch

**Files:** Create `backend/app/modules/security_logs/__init__.py`, `backend/app/modules/security_logs/ingest.py`; Modify `backend/app/gateway/dispatch.py` (SecurityEventsMsg branch), `backend/app/modules/__init__.py` (register security_logs); Test `backend/tests/test_security_ingest.py`

**Interfaces:**
- Produces `async def ingest_events(session, device: Device, events: list[SecurityEventDoc]) -> int` (inserted count; raises `SecurityIngestError` on unknown type — pre-validated by Pydantic, so the error path covers tampered/legacy payloads); `register(app)` mounting the module. Gateway branch: `SecurityEventsMsg` → tenant session → `ingest_events` for the bound device → queue `security_events_ingested` UI event `{kind, device_id, count}` post-commit; a rejected batch audits `security_logs.ingest_rejected` and inserts nothing.

**Steps:**
- [ ] Failing tests: batch of 3 inserts 3 with correct columns; re-ingesting the same `event_uid`s inserts 0; default `security_logs` Policy seeded once (`{"retention_days": 365, "ship_interval_s": 60}`); via `handle_agent_message`: events land for the bound device, UI event queued post-commit, forged batch for another connection's message path untouched (identity from connection binding).
- [ ] Implement; suite green. Commit `feat(security-logs): deduped gateway ingestion`.

### Task 4: Retention worker

**Files:** Modify `backend/app/worker.py` (task + beat entry); Create `backend/app/modules/security_logs/retention.py`; Test `backend/tests/test_security_retention.py`

**Interfaces:**
- Produces `async def apply_retention(org_id: UUID, admin_engine: AsyncEngine, app_engine: AsyncEngine) -> int` (deleted count): reads `retention_days` from the org's `security_logs` policy (default 365), deletes older rows via admin engine in batches of 10_000, audits `security_logs.retention_applied` with `{"cutoff", "deleted"}` (skips the audit when 0 deleted); Celery beat task `apply_security_log_retention` daily (86400s) iterating orgs like `sweep_all_orgs`.

**Steps:**
- [ ] Failing tests: 5 old + 3 recent → deletes 5, keeps 3, audit event carries deleted=5; org policy `retention_days=30` honored; second run deletes 0 and writes no audit event.
- [ ] Implement; suite green. Commit `feat(security-logs): audited retention sweep`.

### Task 5: Search API + evidence CSV

**Files:** Create `backend/app/modules/security_logs/api.py`; Modify `backend/app/modules/security_logs/__init__.py` (mount); Test `backend/tests/test_security_api.py`

**Interfaces:**
- Produces per spec §8: `GET /security-events` (filters `device_id`, `username` ilike, `event_type`, `from_ts`/`to_ts` query params ISO, paged `{items,total}` newest first; `SecurityEventOut {id, device_id, hostname, ts, event_type, username, source, outcome}`); `GET /security-events/{id}` (+`raw`); `GET /reports/access.csv?from_ts=&to_ts=` header `ts,hostname,username,event_type,source,outcome`. All `audit.read`.

**Steps:**
- [ ] Failing tests: technician 403; auditor filters by username substring + type + time window; foreign org sees nothing (RLS); detail includes raw; CSV header exact + rows filtered to period.
- [ ] Implement; suite green. Commit `feat(security-logs): search API and SOC 2 access evidence export`.

### Task 6: Gateway e2e extension

**Files:** Modify `backend/tests/test_e2e_kernel.py`

**Steps:**
- [ ] Extend the kernel e2e: after the inventory sweep, the fake agent sends a `security_events` batch (a logon_success + a logon_failure) over the live mTLS socket; assert both appear via `GET /api/v1/security-events`, a re-send of the identical batch changes the total by 0, and the `security_events_ingested` UI event fired. 3 stable runs. Commit `feat(security-logs): end-to-end ship through the mTLS channel`.

### Task 7: Security console page

**Files:** Create `frontend/src/modules/security/SecurityPage.tsx`, `frontend/src/modules/security/eventBadges.ts`; Modify `frontend/src/lib/api.ts` (types + `listSecurityEvents(params)`), `frontend/src/App.tsx` (route `/security`, nav gated on `audit.read` via `/me`); Test `frontend/src/modules/security/eventBadges.test.ts`

**Interfaces:**
- Produces `eventBadgeVariant(eventType: string): BadgeVariant` — `logon_failure`/`account_lockout` ⇒ `destructive`, `privilege_elevation`/`admin_group_change`/`policy_change` ⇒ `warning`, `logon_success`/`logoff` ⇒ `muted`, unknown ⇒ `muted`.

**Steps:**
- [ ] Failing vitest: badge mapping total over the taxonomy; api client builds query string with filters.
- [ ] Implement page (filter bar with 24h/7d/30d presets, table, raw-detail drawer, evidence CSV link with current period, live refresh on `security_events_ingested`); `npm test`, `tsc --noEmit`, `npm run build` green.
- [ ] Commit `feat(security-logs): security console page`.

---

## Self-review

- **Coverage:** spec §4→T1, §5→T2, §6→T3, §7→T4, §8→T5, §9→T7, §10→T1–T7 (+e2e in T6); §3 immutability→T2 (grants) + T4 (admin-engine deletes + audit). Nav gating on `/me` permissions→T7. No gaps.
- **Placeholders:** none of the banned patterns; each task names exact routes, signatures, defaults, and test intents.
- **Type consistency:** `SecurityEventDoc` field names identical across T1 (wire), T3 (ingest), T5 (API out); `from_ts`/`to_ts` param names consistent T5/T7; taxonomy string set identical everywhere.
