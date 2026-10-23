# Change Management (Phase 9) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the control-plane change-management module — change requests, a server-enforced multi-state approval workflow (with segregation-of-duties and the emergency retrospective path), advisory action-linking, a change calendar, and SOC 2 evidence export — with Odoo Helpdesk pinned as a null-adapter contract.

**Architecture:** A new `change_mgmt` business module registering into the kernel like `provisioning`/`backup` (models, migration, service, api, register_all) — but **no agent jobs, no worker, no gateway registration** (it governs and records; it dispatches nothing). The workflow is a pure server-side state machine; every transition writes a hash-chained audit event (the evidence spine). Action-linking resolves patch/deploy/provision records polymorphically at the app layer, org-scoped, and is advisory (never gates). Odoo sync is a `ChangeTicketPort` with a `NullChangeTicketAdapter`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic v2; React+TypeScript+Vite+vitest. venv at `backend/.venv`.

## Global Constraints

Every task's requirements implicitly include this section. Copy exact values verbatim.

- **No agent path.** This module dispatches NO agent jobs and registers NO job types. Do NOT touch `register_worker_job_types`, `app/gateway/main.py`, or the job engine. `register(app)` only mounts the router (there is no `register_job_types()` for this module).
- **Security invariant #4 — audit (THE evidence).** EVERY privileged action writes the hash-chained audit log via `audit_service.record(session, *, org_id, actor_type, actor_id, action, target_type, target_id, metadata)`. For every change action set `target_type="change"`, `target_id=str(change_id)` so evidence resolves. Actions: `change.created` / `change.updated` / `change.submitted` / `change.approved` / `change.rejected` / `change.transitioned` / `change.linked` / `change.unlinked` / `change.evidence_exported`. All `actor_type="user"`, `actor_id=ctx.user_id`. No secret in metadata.
- **Security invariant #5 — tenancy.** EVERY `select()`/write over `change_request`/`change_approval`/`change_link`/`change_external_ref` AND the polymorphic link targets (`patch_runs`/`deployments`/`provision_run`/`patch_approvals`) carries an explicit `org_id` predicate in addition to RLS. RLS is `ENABLE + FORCE` + `tenant_isolation` policy `USING (org_id = current_setting('app.current_org', true)::uuid)` on all four new tables. A missing tenant filter is a security bug.
- **Security invariant #6 — RBAC.** Server-side `Depends(require_permission("<code>"))` on every route; server-side permission check on every transition. Permissions: `change.read`, `change.request`, `change.approve`, `change.manage`. Grants: super-admin/org-admin → all four; technician → read+request; auditor/read-only → read. `change.approve` is NOT granted to technician (segregation of duties).
- **Segregation of duties (SoD) — server-enforced.** For a NON-emergency change, the approver MUST NOT equal the change's `created_by` → the state engine raises `ChangeValidationError`. Emergency retrospective approval by the requester is likewise forbidden. Enforced in the state engine, not just the UI/RBAC.
- **State vocabularies (verbatim).** `change_request.state` ∈ {`draft`,`submitted`,`approved`,`implementing`,`completed`,`rejected`,`cancelled`}. `type` ∈ {`standard`,`normal`,`emergency`}. `risk` ∈ {`low`,`medium`,`high`}. `change_approval.decision` ∈ {`approved`,`rejected`}. `change_link.action_type` ∈ {`patch_run`,`deployment`,`provision_run`,`patch_approval`}.
- **Emergency path.** `emergency` type may transition `submitted → implementing` without prior approval; but `implementing → completed` is BLOCKED until a `change_approval` row with `decision="approved"` AND `is_retrospective=true` exists. A retrospectively-rejected emergency cannot reach `completed`.
- **Advisory linking.** Linking records an association and NEVER gates/blocks/alters the linked action. Link targets are resolved org-scoped at link time (nonexistent/cross-org id → `ChangeValidationError`).
- **Odoo is a deferred contract.** Ship the `ChangeTicketPort` protocol + `NullChangeTicketAdapter` only; NO Odoo client, NO network. Selected by config `ITS_CHANGE_TICKET_BACKEND` (default `null`).
- **Reuse, do not fork.** `audit_service.record`; `require_permission` / `AuthContext`; `org_session`; the migration `_rls` helper + permission/grant seed pattern from `0015_provisioning.py`; the module skeleton from `app/modules/provisioning/`.
- **Engineering conventions.** Async; typed SQLAlchemy 2; Pydantic v2; logic in services not routes; forward-only additive migration (`down_revision="0015"`); `mypy` + `ruff` clean; frontend strict TS no `any`. Frontend state vocab sourced from backend. If Postgres/Redis down: `pg_ctlcluster 16 main start; redis-server --daemonize yes`. Delete any stray `backend/uv.lock`.

---

### Task 1: Migration 0016 + RBAC seed + audit index

**Files:** Create `backend/migrations/versions/0016_change_mgmt.py`; Test `backend/tests/test_change_mgmt_migration.py`.

**Interfaces:** Produces the 4 tables with RLS, the 4 permissions + grants, and a new index `ix_audit_target` on `audit_events (org_id, target_type, target_id)`.

- [ ] **Step 1: Write the failing test** (mirror `backend/tests/test_provisioning_migration.py`): assert alembic head is `0016`; the 4 tables (`change_request`,`change_approval`,`change_link`,`change_external_ref`) have `rowsecurity` AND `relforcerowsecurity` true; the 4 permissions exist; grants match (super/org-admin=all 4, technician=read+request, auditor/read-only=read; technician does NOT have `change.approve`); the `ix_audit_target` index exists on `audit_events`.

Run: `.venv/bin/python -m pytest tests/test_change_mgmt_migration.py -v` → FAIL.

- [ ] **Step 2: Implement.** `revision="0016"`, `down_revision="0015"`. Copy `_rls` from `0015_provisioning.py`. Tables (all `id` uuid7 pk, `org_id` FK→organizations, RLS applied):
  - `change_request`: org_id, `type` String(12), `title` String(255), `description` Text, `risk` String(8), `affected` JSONB default `[]`, `window_start` timestamptz nullable, `window_end` timestamptz nullable, `rollback_plan` Text, `state` String(16) default `'draft'`, `created_by` FK→users, `created_at` server_default now. Index `ix_change_request_state (org_id, state)`, `ix_change_request_window (org_id, window_start)`.
  - `change_approval`: org_id, `change_id` FK→change_request, `approver_id` FK→users, `decision` String(10), `comment` Text nullable, `is_retrospective` Boolean default false, `decided_at` server_default now. Index `ix_change_approval_change (org_id, change_id)`.
  - `change_link`: org_id, `change_id` FK, `action_type` String(20), `action_id` (Uuid), `linked_at` server_default now, `linked_by` FK→users. Unique constraint `uq_change_link (org_id, change_id, action_type, action_id)`; index `ix_change_link_change (org_id, change_id)`.
  - `change_external_ref`: org_id, `change_id` FK, `system` String(16), `external_id` Text, `synced_at` timestamptz nullable.
  - `_rls("change_request")`, `_rls("change_approval")`, `_rls("change_link")`, `_rls("change_external_ref")`.
  - `op.create_index("ix_audit_target", "audit_events", ["org_id", "target_type", "target_id"])`.
  - Seed permissions `("change.read","change","read")`,`("change.request","change","request")`,`("change.approve","change","approve")`,`("change.manage","change","manage")` + grants (mirror `0015`'s SQL). `downgrade()` drops grants, permissions, the index, then the 4 tables.

Run → PASS; `.venv/bin/ruff check migrations/versions/0016_change_mgmt.py` clean; `.venv/bin/alembic heads` shows single head `0016`.

- [ ] **Step 3: Commit** `feat(change-mgmt): migration 0016 — change tables, RLS, RBAC, audit-target index`.

---

### Task 2: ORM models

**Files:** Create `backend/app/modules/change_mgmt/__init__.py` (empty for now), `backend/app/modules/change_mgmt/models.py`; Modify `backend/app/models/__init__.py` (re-export); Test `backend/tests/test_change_mgmt_models.py`.

**Interfaces:** Produces `ChangeRequest`, `ChangeApproval`, `ChangeLink`, `ChangeExternalRef`. Import: `from app.models import ChangeRequest, ChangeApproval, ChangeLink, ChangeExternalRef`.

- [ ] **Step 1: Write the failing test** (mirror `test_provisioning_models.py`): insert an org + a `ChangeRequest` (type "normal", risk "low", state defaults "draft"), a `ChangeApproval`, a `ChangeLink`, a `ChangeExternalRef`; flush; assert column round-trip + defaults (`state=="draft"`, `affected==[]`, `is_retrospective is False`, nullable windows `None`).

Run → FAIL.

- [ ] **Step 2: Implement `models.py`** mirroring `app/modules/provisioning/models.py` (explicit `created_at` via `server_default=func.now()`, `uuid7_pk()`, `Mapped[...]`, `JSONB`, `Index`, `UniqueConstraint`). Exact columns per Task 1. Re-export from `app/models/__init__.py`.

Run → PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(change-mgmt): ORM models (ChangeRequest, ChangeApproval, ChangeLink, ChangeExternalRef)`.

---

### Task 3: State machine (pure transition function + SoD)

**Files:** Create `backend/app/modules/change_mgmt/state.py`; Test `backend/tests/test_change_mgmt_state.py`.

**Interfaces:** Produces:
- `class ChangeValidationError(Exception): pass`
- `ChangeAction = Literal["submit","approve","reject","begin","complete","cancel","revise"]`
- `def next_state(*, current: str, action: ChangeAction, type_: str, created_by: UUID, actor_id: UUID, has_retrospective_approval: bool) -> str` — a PURE function returning the new state or raising `ChangeValidationError` for an illegal transition or an SoD violation. No I/O.

Transition rules (encode exactly):
- `submit`: `draft → submitted`.
- `approve`: `submitted → approved` for standard/normal — but raise if `actor_id == created_by` (SoD). For `emergency`, `approve` records a retrospective decision and does NOT change the run state (return `current`); still raise if `actor_id == created_by`.
- `reject`: `submitted → rejected` (standard/normal); for `emergency` a retrospective reject returns `current` (state handled by the row); raise on `actor_id == created_by`.
- `begin`: standard/normal `approved → implementing`; **emergency** `submitted → implementing` (fast-path, no approval needed).
- `complete`: `implementing → completed`, but raise `ChangeValidationError` if `type_=="emergency" and not has_retrospective_approval`.
- `cancel`: `{draft,submitted,approved} → cancelled`.
- `revise`: `rejected → draft`.
- Any other `(current, action)` pair → raise `ChangeValidationError`.

- [ ] **Step 1: Write the failing test** — table-driven: every legal transition returns the expected state; a representative set of illegal ones raise (`draft`+`approve`, `completed`+`submit`, `implementing`+`approve`, etc.); SoD: non-emergency `approve`/`reject` with `actor_id==created_by` raises, with a different actor succeeds; emergency `begin` from `submitted` succeeds; emergency `complete` without retrospective approval raises, with it succeeds; emergency retrospective approve by the requester raises.

Run → FAIL.

- [ ] **Step 2: Implement `state.py`** as a pure function (a dict-of-allowed-transitions plus the emergency/SoD special cases). No DB, no session.

Run → PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(change-mgmt): pure change state machine with SoD + emergency path`.

---

### Task 4: API schemas

**Files:** Create `backend/app/modules/change_mgmt/schema.py`; Test `backend/tests/test_change_mgmt_schema.py`.

**Interfaces:** Pydantic v2 (plain `BaseModel` — no wire models, no agent jobs). Request/response models:
- `ChangeCreateRequest` (type, title, description, risk, affected: list[dict], window_start: datetime|None, window_end: datetime|None, rollback_plan), `ChangeUpdateRequest` (same fields, all optional).
- `ChangeOut` (id, type, title, description, risk, affected, window_start, window_end, rollback_plan, state, created_by, created_at).
- `ApprovalDecisionRequest` (comment: str|None), `ApprovalOut` (id, approver_id, decision, comment, is_retrospective, decided_at).
- `TransitionRequest` (action: Literal["begin","complete","cancel","revise"]).
- `LinkCreateRequest` (action_type: Literal[...], action_id: UUID), `LinkOut` (id, action_type, action_id, linked_at, linked_by).
- `CalendarEntryOut` (change_id, title, type, risk, state, window_start, window_end), `CalendarOut` (entries: list, conflicts: list[tuple[UUID,UUID]] or list[dict]).
- `EvidenceOut` (change, approvals, links, audit_refs) — nested shapes per spec §4.

- [ ] **Step 1: Write the failing test** — construct each; assert the `type`/`risk`/`action`/`action_type`/`decision` Literals reject out-of-domain values; assert `ChangeCreateRequest` requires title/type/risk.

Run → FAIL.

- [ ] **Step 2: Implement `schema.py`.**

Run → PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(change-mgmt): API request/response schemas`.

---

### Task 5: Ticket port (Odoo contract, null adapter)

**Files:** Create `backend/app/modules/change_mgmt/ticketing.py`; Test `backend/tests/test_change_mgmt_ticketing.py`.

**Interfaces:** Produces:
- `class ChangeTicketPort(Protocol)` with `async def push_change(self, session, *, org_id: UUID, change: ChangeRequest) -> str`, `async def update_change(self, session, *, org_id: UUID, external_id: str, change: ChangeRequest) -> None`, `async def close_change(self, session, *, org_id: UUID, external_id: str) -> None`.
- `class NullChangeTicketAdapter` implementing the port — every method a no-op; `push_change` returns `""` (empty ref); writes NOTHING to `change_external_ref` and performs NO network.
- `def get_ticket_adapter(settings) -> ChangeTicketPort` — returns `NullChangeTicketAdapter()` when `settings.change_ticket_backend == "null"` (the only supported value in v1); an unknown value raises a clear config error. Add `change_ticket_backend: str = "null"` to `Settings` (env `ITS_CHANGE_TICKET_BACKEND`) — check `app/core/config.py` for the settings pattern and mirror it.

- [ ] **Step 1: Write the failing test** — `get_ticket_adapter` with default settings returns a `NullChangeTicketAdapter`; its `push_change` returns `""` and creates no `change_external_ref` row; `update_change`/`close_change` are no-ops that don't raise; an unknown backend value raises.

Run → FAIL.

- [ ] **Step 2: Implement `ticketing.py`** + the `Settings` field.

Run → PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(change-mgmt): ChangeTicketPort + NullChangeTicketAdapter (Odoo contract, deferred)`.

---

### Task 6: Change service — create/edit + lifecycle transitions

**Files:** Create `backend/app/modules/change_mgmt/service.py`; Test `backend/tests/test_change_mgmt_service.py`.

**Interfaces:** Consumes `next_state` (Task 3), the ticket port (Task 5). Produces (all org-scoped, all audit-writing, `actor_id`/`actor_type` threaded):
- `async def create_change(session, *, org_id, actor_id, type_, title, description, risk, affected, window_start, window_end, rollback_plan) -> ChangeRequest` — creates a `draft`; audits `change.created`.
- `async def update_change(session, *, org_id, actor_id, change_id, **fields) -> ChangeRequest` — org-scoped-load; only a `draft` is editable (else `ChangeValidationError`); audits `change.updated`.
- `async def transition_change(session, ticket, *, org_id, actor_id, change_id, action) -> ChangeRequest` — org-scoped-load; compute `has_retrospective_approval` by an org-scoped query over `change_approval` (`decision="approved" AND is_retrospective=true`); call `next_state(...)`; set `run.state`; audit `change.submitted` (for submit) or `change.transitioned` (metadata `{from, to, action}`); call the pinned ticket-port site (submit→`push_change`, terminal→`close_change`, else `update_change`); `flush`. `action` here covers `submit`/`begin`/`complete`/`cancel`/`revise` (approve/reject are Task 7).
- `async def get_change(session, org_id, change_id) -> ChangeRequest | None`, `async def list_changes(session, org_id) -> list[ChangeRequest]` — org-scoped.

- [ ] **Step 1: Write the failing test** — create→draft (audit `change.created`); update a draft (audit `change.updated`), updating a non-draft raises; `transition_change(submit)` → submitted (+ `push_change` called on the null adapter — assert via a spy adapter recording calls) ; standard flow submit→(approve in Task 7)→begin→complete via transition where legal; an illegal transition raises; emergency `begin` from `submitted` works; emergency `complete` without retrospective approval raises; tenancy (another org's change unreachable). Use a spy `ChangeTicketPort` to assert the pinned call sites fire (push on submit, close on terminal).

Run → FAIL.

- [ ] **Step 2: Implement `service.py`.** Every query org-scoped; ticket port called through the injected interface.

Run → PASS; mypy + ruff clean; `.venv/bin/python -m pytest tests/ -k change_mgmt -q`.

- [ ] **Step 3: Commit** `feat(change-mgmt): change service — create/edit + lifecycle transitions with pinned ticket push`.

---

### Task 7: Approvals (SoD + emergency retrospective, insert-only)

**Files:** Modify `backend/app/modules/change_mgmt/service.py` (add approve/reject); Test `backend/tests/test_change_mgmt_approvals.py`.

**Interfaces:** Produces:
- `async def decide_change(session, ticket, *, org_id, actor_id, change_id, decision, comment) -> ChangeApproval` — org-scoped-load the change; determine `is_retrospective` = (`type_=="emergency"` and the change is already `implementing`); call `next_state(current=change.state, action=("approve" if decision=="approved" else "reject"), type_=change.type, created_by=change.created_by, actor_id=actor_id, has_retrospective_approval=<n/a here>)` to VALIDATE the SoD + legality and get the resulting run state; **insert a NEW `change_approval` row** (never revoke-in-place) with `approver_id=actor_id, decision, comment, is_retrospective`; set `change.state` to the returned state (unchanged for emergency retrospective); audit `change.approved`/`change.rejected` (metadata `{decision, is_retrospective}`, actor=approver); call `ticket.update_change`; `flush`; return the approval row.

- [ ] **Step 1: Write the failing test** — a submitted normal change: approve by a DIFFERENT user → change `approved` + one `change_approval` row (decision approved, is_retrospective false); approve by `created_by` → `ChangeValidationError` (SoD); reject by a different user → `rejected` + a row; multiple decisions over time all persist (insert-only — two rows after two decisions). Emergency change in `implementing`: a retrospective approve by a different user → a `change_approval(is_retrospective=true)` row and state stays `implementing`, and a subsequent `transition_change(complete)` now succeeds; retrospective approve by the requester raises. Tenancy.

Run → FAIL.

- [ ] **Step 2: Implement `decide_change`.**

Run → PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(change-mgmt): approvals — insert-only decisions, SoD, emergency retrospective`.

---

### Task 8: Advisory action-linking (polymorphic, org-scoped)

**Files:** Create `backend/app/modules/change_mgmt/links.py`; Test `backend/tests/test_change_mgmt_links.py`.

**Interfaces:** Produces:
- `_LINK_RESOLVERS: dict[str, <model>]` mapping `action_type` → the model to existence-check: `patch_run→PatchRun`, `deployment→Deployment`, `provision_run→ProvisionRun`, `patch_approval→PatchApproval` (import from their modules).
- `async def add_link(session, *, org_id, actor_id, change_id, action_type, action_id) -> ChangeLink` — org-scoped-verify the change exists; validate `action_type` is known; **org-scoped-resolve** the target (`select(Model.id).where(Model.org_id==org_id, Model.id==action_id)`) — if absent → `ChangeValidationError` (nonexistent or cross-org); insert `ChangeLink` (unique constraint dedups — a duplicate raises `ChangeValidationError` with a clear message, catch IntegrityError or pre-check); audit `change.linked` (metadata `{action_type, action_id}`); **does NOT modify the linked action** (advisory); `flush`; return the link.
- `async def remove_link(session, *, org_id, actor_id, change_id, link_id) -> None` — org-scoped delete; audit `change.unlinked`.
- `async def list_links(session, org_id, change_id) -> list[ChangeLink]` — org-scoped.

- [ ] **Step 1: Write the failing test** — seed a change + a real `Deployment`/`PatchRun`/`ProvisionRun`/`PatchApproval` in the org; `add_link` for each succeeds and the linked action's state is UNCHANGED (advisory — assert the deployment row is untouched); a nonexistent `action_id` raises; an action id belonging to ANOTHER org raises (cross-org resolution blocked); a duplicate link raises; `remove_link` deletes + audits; tenancy on list.

Run → FAIL.

- [ ] **Step 2: Implement `links.py`.**

Run → PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(change-mgmt): advisory action-linking with org-scoped polymorphic resolution`.

---

### Task 9: Evidence export + change calendar (read-only)

**Files:** Create `backend/app/modules/change_mgmt/reporting.py`; Test `backend/tests/test_change_mgmt_reporting.py`.

**Interfaces:** Produces (read-only, org-scoped, NO writes/audit-except-export-note — see below):
- `async def change_evidence(session, org_id, change_id) -> dict` — the change, its full `change_approval` history (ordered by decided_at), its `change_link` rows, and `audit_refs` = ids from `audit_events` where `org_id==org_id AND target_type=="change" AND target_id==str(change_id)` (uses the new `ix_audit_target` index). Shape per spec §4.
- `async def fleet_change_evidence(session, org_id) -> list[dict]` — one evidence record per change in the org.
- `async def change_calendar(session, org_id, *, frm: datetime, to: datetime) -> dict` — changes whose `[window_start, window_end]` intersects `[frm, to]` (both non-null); plus a `conflicts` list of change-id pairs whose windows overlap in time AND share an affected asset (`affected` JSONB intersection). The overlap+shared-asset check is a pure function over the fetched rows — unit-test it directly.
NOTE: the EVIDENCE-EXPORT audit event (`change.evidence_exported`) is written by the API route (Task 10), not here (reporting stays pure read). Document that.

- [ ] **Step 1: Write the failing test** — seed a change with 2 approvals + 2 links + several audit events (create/submit/approve); `change_evidence` returns the change, both approvals in order, both links, and `audit_refs` containing the change's audit event ids (and excluding other changes' events). Calendar: two changes with overlapping windows sharing an affected device → flagged as a conflict; disjoint windows or disjoint assets → not flagged; a change outside the range excluded. Tenancy: another org's changes never appear.

Run → FAIL.

- [ ] **Step 2: Implement `reporting.py`.**

Run → PASS; mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(change-mgmt): SOC2 evidence export + change calendar with conflict detection`.

---

### Task 10: API routes + module registration

**Files:** Create `backend/app/modules/change_mgmt/api.py`; Modify `backend/app/modules/change_mgmt/__init__.py` (`register(app)`), `backend/app/modules/__init__.py` (`register_all`); Test `backend/tests/test_change_mgmt_api.py`.

**Interfaces:** Routes under `/api/v1/changes`, each `Depends(require_permission("<code>"))`; `org_id`/`actor_id` from `AuthContext`.
- `GET /changes` (read), `POST /changes` (request → create_change), `GET /changes/{id}` (read), `PATCH /changes/{id}` (request-owner-or-manage → update_change).
- `POST /changes/{id}/submit` (request), `POST /changes/{id}/approve` (approve → decide_change approved), `POST /changes/{id}/reject` (approve → decide_change rejected), `POST /changes/{id}/transition` (request/manage → transition_change with body action).
- `GET /changes/{id}/links` (read), `POST /changes/{id}/links` (manage-or-owner → add_link), `DELETE /changes/{id}/links/{link_id}` (manage-or-owner → remove_link).
- `GET /changes/calendar` (read), `GET /changes/{id}/evidence` (read — also writes the `change.evidence_exported` audit), `GET /changes/evidence` (read).
Each mutating route wraps the service call in `async with org_session(ctx.org_id, engine=state.app_engine)`, passes `get_ticket_adapter(state.settings)` where the service needs the ticket port, and translates `ChangeValidationError` → HTTP 400/409, `None` load → 404. `register(app)` mounts the router ONLY (no job-type registration). `register_all` adds `change_mgmt.register(app)`.

- [ ] **Step 1: Write the failing test** (mirror `tests/test_provisioning_api.py` `api_env`): `POST /changes` OK for technician (request); `POST /changes/{id}/approve` 403 for technician (lacks approve), OK for org-admin; SoD — org-admin creating a change then approving it via the API is rejected 400 (self-approval) [seed so creator≠approver for the OK case]; `GET /changes`/`calendar`/`evidence` OK for read; `POST /changes/{id}/links` links an org deployment and 400s a cross-org one; `GET /changes/{id}/evidence` writes a `change.evidence_exported` audit. Assert no route 500s on a `ChangeValidationError` (maps to 4xx).

Run → FAIL.

- [ ] **Step 2: Implement `api.py` + the two wiring edits.**

Run → PASS; then FULL suite `.venv/bin/python -m pytest -q` (shared `register_all` + the new audit index — a regression must surface, incl. the `test_rbac.py`/`test_api_routes.py` permission-set expectations which MUST be updated to include `change.*` — update those hardcoded sets: org-admin gets all 4, technician gets `change.read`+`change.request`); mypy + ruff clean.

- [ ] **Step 3: Commit** `feat(change-mgmt): API routes + module registration`.

---

### Task 11: Frontend feature folder

**Files:** Create `frontend/src/modules/change/{types.ts,changeState.ts,api.ts,ChangesPage.tsx,ChangeRequestForm.tsx,ApprovalQueue.tsx,ChangeDetail.tsx,ChangeCalendar.tsx}`; Modify `frontend/src/lib/api.ts`, `frontend/src/App.tsx`; Test `frontend/src/modules/change/__tests__/change.test.tsx`.

**Interfaces:** `types.ts` mirrors backend response models exactly. `changeState.ts` maps `change_request.state` {draft, submitted, approved, implementing, completed, rejected, cancelled} → `BadgeVariant` (`"muted"|"info"|"success"|"destructive"|"warning"`), `?? "muted"` fallback — NO invented names. `api.ts` hooks with query-key invalidation after mutations (no live event). Pages: ChangesPage (list + "New change" gated on `change.request`), ChangeRequestForm, ApprovalQueue (approve/reject gated on `change.approve`, hidden without it; the approve control is disabled on one's own change — cosmetic SoD), ChangeDetail (approval timeline + linked actions + evidence export), ChangeCalendar (conflict highlights). Nav item `{ to: "/changes", label: "Changes" }` gated on `permissions.includes("change.read")`. Strict TS, no `any`.

- [ ] **Step 1: Write the failing test** (mirror `frontend/src/modules/provisioning/__tests__/`): ChangesPage lists changes; the "New change" control hidden without `change.request`; the approve control in ApprovalQueue hidden without `change.approve`; `changeStateVariant("implementing")` non-fallback and an unknown string → `"muted"`.

Run → FAIL (check package.json for the vitest command).

- [ ] **Step 2: Implement** pages + nav/route + ApiClient methods + hooks.

- [ ] **Step 3:** `npm test`, `npm run typecheck`, `npm run build` all green.

- [ ] **Step 4: Commit** `feat(change-mgmt): changes/approvals/calendar UI + evidence export`.

---

### Task 12: Deploy docs

**Files:** Modify `deploy/README.md`, `deploy/.env.example`; Test `cd deploy && cp .env.example .env && docker compose config >/dev/null && echo OK && rm -f .env`.

- [ ] **Step 1:** Add a "Change management" section to `deploy/README.md`: control-plane only (no new compose services, no worker sweeps, no agent capability); the `ITS_CHANGE_TICKET_BACKEND` selector (default `null`) and the **Odoo Helpdesk adapter contract seam** (one-way push, vault-sealed token when a real adapter lands); change-action linking is **advisory** (SOC 2 evidence, does not gate actions); the change audit trail + `ix_audit_target` index power the evidence export. Add the `ITS_CHANGE_TICKET_BACKEND=null` line to `.env.example` with a comment.

- [ ] **Step 2: Validate** `docker compose config` prints OK.

- [ ] **Step 3: Commit** `docs(change-mgmt): deploy notes — ticket-backend selector, Odoo contract, advisory linking`.

---

## Self-Review

**Spec coverage:** §1 scope → all tasks; §2 data model → T1/T2; §3 state machine + SoD + emergency → T3 (pure) + T6 (transitions) + T7 (approvals/retrospective); §4 linking + evidence → T8 (advisory polymorphic) + T9 (evidence); §5 Odoo port → T5 (null adapter) + T6 (pinned push sites); §6 calendar → T9; §7 invariants → audit (all service tasks, target_type="change"), tenancy (all), RBAC (T1 seed + T10 enforce), SoD (T3/T7); §8 API → T10; §9 worker none → correctly omitted (no worker/gateway edits); §10 frontend → T11; §11 testing → per-task incl state-machine table, SoD, emergency, advisory-non-gating, calendar conflict, evidence audit_refs, tenancy, RBAC 403; §12 deploy → T12; §13 deferred → not built (real Odoo, enforced gating, notifications). Accumulated lessons: audit actor threading (all), frontend real vocab (T11), full-suite + permission-set-test update at registration (T10 — the Phase-7 Task-1 lesson applied proactively).

**Placeholder scan:** every task names concrete files, exact signatures, exact vocabularies. No TBD/TODO. The one cross-cutting item (updating `test_rbac.py`/`test_api_routes.py` permission-set literals for `change.*`) is called out explicitly in T10 so it isn't missed (unlike Phase 7 where it surfaced late).

**Type consistency:** `ChangeRequest`/`ChangeApproval`/`ChangeLink`/`ChangeExternalRef`/`ChangeValidationError`/`next_state`/`ChangeTicketPort`/`NullChangeTicketAdapter`/`get_ticket_adapter`/`create_change`/`update_change`/`transition_change`/`decide_change`/`add_link`/`remove_link`/`change_evidence`/`change_calendar` are used consistently across tasks. State vocab and `action_type` discriminators are fixed in Global Constraints and used verbatim. `target_type="change"` / `target_id=str(change_id)` is consistent between the audit-writing tasks (T6/T7/T8/T10) and the evidence resolver (T9).
