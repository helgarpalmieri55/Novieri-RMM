# Spec — Change Management (Phase 9)

> Full spec, expanded from `docs/modules/change-mgmt.md` via the brainstorming
> skill. Scope decision (approved): **control-plane-first**. Build the full change
> workflow (requests, multi-state approval, calendar, action-linking, SOC 2
> evidence export) now; pin **Odoo Helpdesk** as a one-way-push adapter contract
> with a null implementation (deferred); make change-action linking **advisory**
> (records/associates, does not block actions). This mirrors how Phase 7 pinned
> the site-server contract and shipped a testable control-plane slice.

---

## 1. Purpose & scope

An IT change-management workflow that records, reviews, approves, and tracks
changes to the environment, and ties the platform's own actions (patch campaigns,
deployments, provisioning runs) into formal change records. Scoped to IT change,
integrating with Odoo Helpdesk rather than reimplementing a service desk. This is
the platform's **SOC 2 evidence engine** for change control.

**v1 delivers (real, tested in this repo):**

- **Change requests** (standard / normal / emergency) with risk, affected assets,
  planned window, and rollback plan.
- A **multi-state approval workflow** with a full, insert-only decision history,
  including the emergency fast-path with mandatory retrospective approval, and a
  **segregation-of-duties** rule (no self-approval of a non-emergency change).
- A **change calendar** with window scheduling and conflict awareness.
- **Advisory action-linking**: associate the patch/deploy/provision actions that
  implement a change (org-scoped polymorphic resolution) — recorded, not gating.
- **SOC 2 evidence export**: what changed, who approved, when, which actions
  implemented it, with audit-event references.

**v1 pins as a contract but does NOT build:**

- The **Odoo Helpdesk sync** — a `ChangeTicketPort` protocol with a
  `NullChangeTicketAdapter` (records intent, no network). A real JSON-RPC adapter
  is a later phase; the record shape (`change_external_ref`) and the push call
  sites are fixed so it drops in without reworking the workflow.

**Explicitly deferred (later phases):** the real Odoo JSON-RPC adapter; two-way
sync; **enforced** change-gating (blocking an action without an approved change);
notifications / approval-request reminders (no notification mechanism exists yet);
CAB workflows; auto-change-creation from risky operations; risk scoring.

Control-plane only — **no agent capability**. The actions a change governs are
executed by other modules' already-signed, already-audited agent capabilities.
The module registers into the kernel (API routes, migration, frontend folder)
without forking core security, tenancy, or the job engine.

---

## 2. Data model

Additive Alembic revision **`0016`** after Phase 7's head (`0015`), `_rls` helper
applied to all four tables (RLS `ENABLE + FORCE` + `tenant_isolation` policy
`USING (org_id = current_setting('app.current_org', true)::uuid)`), plus the
`change.*` permission seed + role grants (§7).

- **`change_request`** — `id` (uuid7 pk), `org_id` FK, `type`
  (standard|normal|emergency), `title`, `description`, `risk` (low|medium|high),
  `affected` JSONB (list of `{kind: device|group, id}` refs), `window_start`
  (nullable timestamptz), `window_end` (nullable), `rollback_plan` (Text),
  `state`, `created_by` FK→users, `created_at`. Index `(org_id, state)`,
  `(org_id, window_start)`.
- **`change_approval`** — **insert-only decision trail** (never revoke-in-place;
  a change accumulates decisions over its lifetime): `id`, `org_id`, `change_id`
  FK, `approver_id` FK→users, `decision` (approved|rejected), `comment` (Text,
  nullable), `is_retrospective` (bool, default false), `decided_at`. Index
  `(org_id, change_id)`.
- **`change_link`** — `id`, `org_id`, `change_id` FK, `action_type`
  (patch_run|deployment|provision_run|patch_approval), `action_id` (UUID — no
  cross-table DB FK; app-level polymorphic resolution), `linked_at`, `linked_by`
  FK→users. Index `(org_id, change_id)`, unique `(org_id, change_id, action_type,
  action_id)` (no duplicate links).
- **`change_external_ref`** — `id`, `org_id`, `change_id` FK, `system` (odoo),
  `external_id` (Text), `synced_at`. Populated only when a real ticket adapter is
  wired; the null adapter writes nothing.

**`change_request.state` vocabulary (verbatim):**
`draft` → `submitted` → `approved` → `implementing` → `completed`, plus terminal
`rejected` and `cancelled`. (Emergency allows `submitted → implementing` directly;
see §3.)

**Other vocabularies:** `type` ∈ {standard, normal, emergency}; `risk` ∈ {low,
medium, high}; `change_approval.decision` ∈ {approved, rejected};
`change_link.action_type` ∈ {patch_run, deployment, provision_run,
patch_approval}.

---

## 3. Workflow (the state machine)

Transitions are enforced **server-side** by a pure transition function; an illegal
transition raises `ChangeValidationError` (never a silent no-op). Every transition
writes a hash-chained audit event (§7).

**Standard / normal:**
`draft` --submit(`change.request`)--> `submitted`
`submitted` --approve(`change.approve`)--> `approved`
`submitted` --reject(`change.approve`)--> `rejected`
`approved` --begin(`change.request`|`change.manage`)--> `implementing`
`implementing` --complete--> `completed`
`rejected` --revise(`change.request`)--> `draft` (rework loop)
`draft`/`submitted`/`approved` --cancel(`change.manage`|owner)--> `cancelled`

**Emergency fast-path:**
`draft` --submit--> `submitted` --begin-emergency(`change.request`)-->
`implementing` **immediately** (no prior approval). A `completed` transition is
**blocked** until a **retrospective** `change.approve` decision
(`is_retrospective=true`, decision `approved`) exists for the change. An emergency
that is retrospectively `rejected` cannot reach `completed` (it must be
`cancelled` or remediated) — an emergency change can never silently close without
a recorded decision.

**Segregation of duties (SoD):** for a **non-emergency** change, the approver
MUST NOT be the change's `created_by` — the state engine rejects a self-approval
with `ChangeValidationError`. (Emergency retrospective approval by the requester
is likewise forbidden; a second party must ratify.) This is enforced server-side,
not merely in the UI.

The transition function is a pure `(current_state, action, change, actor) ->
new_state` unit — fully testable without I/O.

---

## 4. Action linking (advisory) & evidence

- `POST /changes/{id}/links` (`change.manage` or the change owner with
  `change.request`) associates one action with the change. **At link time the
  target is resolved org-scoped**: the `action_id` must exist in the referenced
  table (`patch_runs` / `deployments` / `provision_run` / `patch_approvals`) for
  this `org_id`, else `ChangeValidationError`. A cross-org or nonexistent id is
  rejected. Linking is **advisory** — it records the association and does NOT
  gate, block, or alter the linked action. Duplicate links are rejected by the
  unique constraint.
- **Polymorphic resolution** is a small dispatch table mapping `action_type` → the
  org-scoped existence query for that model. (No DB-level cross-table FK exists;
  resolution is app-level, the same pattern Odoo's `model`/`res_id` uses.)
- **Evidence export** (`change.read`): `GET /changes/{id}/evidence` and
  `GET /changes/evidence` (fleet) produce a SOC 2 evidence record per change:
  `{change: {type, title, risk, window, state, created_by, created_at},
  approvals: [{approver, decision, comment, is_retrospective, decided_at}],
  links: [{action_type, action_id}], audit_refs: [audit_event_id…]}`. The
  `audit_refs` are the ids of the hash-chained audit events this change produced
  (create/submit/approve/reject/transition/link) — the tamper-evident spine of the
  evidence. CSV and JSON. Read-only aggregation (no writes).

---

## 5. Change-ticket adapter (the port now has a real backend)

- **`ChangeTicketPort` protocol** (`app/modules/change_mgmt/ticketing.py`):
  `async def push_change(session, *, org_id, change) -> str` (returns an
  `external_id`), `async def update_change(session, *, org_id, external_id,
  change) -> None`, `async def close_change(session, *, org_id, external_id) ->
  None`. **One-way push only** — create/update/close a ticket *from* a change;
  no inbound sync.
- **The Service Desk adapter is the default since 2026-09-23**
  (`ITS_CHANGE_TICKET_BACKEND=service_desk`). `NullChangeTicketAdapter` —
  every method a no-op, nothing written to `change_external_ref` — is the
  explicit opt-out, `ITS_CHANGE_TICKET_BACKEND=null`. A `.env` copied from an
  older `.env.example` still carries `null` and keeps the old behaviour.
- **Revision, made when Service Desk shipped
  (`docs/modules/service-desk/spec.md` §7.1, §13):** this section originally
  pinned an eventual **Odoo Helpdesk** adapter (JSON-RPC, vault-sealed API
  token) as the real backend behind this port. That target is superseded —
  `docs/00-vision.md`'s reversal on owning ticketing (service-desk spec §0.1)
  means Odoo is no longer this platform's ticketing system at all, so an Odoo
  *change-ticket* adapter would integrate with a system this platform is
  actively replacing. The real backend is instead
  `app.modules.service_desk.adapters.ServiceDeskChangeTicketAdapter`, selected
  by `ITS_CHANGE_TICKET_BACKEND=service_desk`: it opens a genuine internal
  Service Desk `incident` ticket (in the org's default queue) for the change,
  requested by and attributed to the change's own author, and records the link
  in `change_external_ref` (`system="service_desk"`). This is **additive** —
  this module's reviewed core (the state machine, `transition_change`,
  `decide_change`) is unmodified; `get_ticket_adapter` gained exactly one new
  branch to select the new adapter.
- **Push call sites are pinned** in the workflow: on `submitted` (create ticket),
  on each subsequent state change (update), on `completed`/`cancelled`/`rejected`
  (close). With the null adapter these are inert; the real adapter writes
  `change_external_ref`. The workflow engine calls the port through an
  interface — it never imports the Service Desk adapter directly (the
  selection happens once, in `get_ticket_adapter`).

---

## 6. Change calendar

`GET /changes/calendar?from=<ts>&to=<ts>` (`change.read`) — org-scoped, returns
changes whose `[window_start, window_end]` intersects the range, each with its
state/type/risk/affected. **Conflict awareness:** changes whose windows overlap in
time AND share an affected device/group are flagged as a conflict pair in the
response. Pure read-only aggregation; the overlap+shared-asset computation is a
testable function over the fetched rows.

---

## 7. Security invariants & RBAC

1. **Signed jobs only** — this module dispatches no agent jobs; the actions it
   links already flow through the signed-job engine. No new unsigned path.
2. **Outbound-only mTLS** — no agent. The Odoo adapter (deferred) is an outbound
   authenticated client per its contract.
3. **Secrets vault-sealed** — no credentials in v1 (null adapter). A real Odoo
   adapter's API token is vault-sealed, decrypt-at-use, never returned/logged —
   pinned in §5, not exercised by v1.
4. **Hash-chained audit** — EVERY privileged action writes the immutable audit
   log with correct actor: change created/edited/submitted (`user`),
   approved/rejected (`user`, the approver), each state transition (`user`),
   link added/removed (`user`), evidence exported (`user`). This audit trail IS
   the SOC 2 evidence. No secret in metadata. **Every change audit event sets
   `target_type="change"`, `target_id=str(change_id)`** so the evidence export
   (§4) resolves a change's `audit_refs` by querying `audit_events` on
   `(org_id, target_type="change", target_id=change_id)` — add a supporting index
   on `audit_events (org_id, target_type, target_id)` in migration `0016` since
   only `(org_id, id)` exists today.
5. **Tenancy** — RLS `ENABLE + FORCE` + explicit `org_id` predicate on every
   query over the four tables; **polymorphic link resolution is org-scoped** (an
   action from another org is unreachable). A missing tenant filter is a bug.
6. **RBAC** — server-side `require_permission` on every route and every
   transition. New permissions:
   - `change.read` — view changes, calendar, evidence export.
   - `change.request` — create/edit own drafts, submit, initiate an emergency,
     begin/complete an approved change.
   - `change.approve` — approve/reject a submitted change (and record a
     retrospective decision).
   - `change.manage` — admin: edit/cancel any change, manage links, configure.
   Grants: **super-admin / org-admin** → all four; **technician** →
   `change.read` + `change.request`; a dedicated **approver** capability sits with
   org-admin (and any role explicitly granted `change.approve`) — the requester
   role (technician) does NOT get `change.approve`, reinforcing SoD; **auditor** /
   **read-only** → `change.read`. The SoD self-approval block (§3) is enforced in
   the state engine regardless of grants.

No agent idle-budget impact (control-plane only).

---

## 8. API

Routes under `/api/v1/changes`, each RBAC-gated; `org_id`/`actor_id` from
`AuthContext`, never the request body:

- `GET /changes` (`read`), `POST /changes` (`request`, creates a `draft`),
  `GET /changes/{id}` (`read`), `PATCH /changes/{id}` (`request` owner /
  `manage`, edits a draft).
- `POST /changes/{id}/submit` (`request`), `POST /changes/{id}/approve`
  (`approve`), `POST /changes/{id}/reject` (`approve`),
  `POST /changes/{id}/transition` (begin/complete/cancel/revise — `request`/
  `manage` per the transition) — each runs the state engine + SoD checks + the
  pinned ticket-push call + audit.
- `GET /changes/{id}/links` (`read`), `POST /changes/{id}/links` (`manage`/owner),
  `DELETE /changes/{id}/links/{link_id}` (`manage`/owner).
- `GET /changes/calendar` (`read`), `GET /changes/{id}/evidence` +
  `GET /changes/evidence` (`read`).

Response models carry no secret fields (there are none in v1). `register(app)`
mounts the router; no job-type registration (no agent jobs).

---

## 9. Worker

**None required** — the module has no sweeps and dispatches no jobs. (A future
"emergency change awaiting retrospective approval > N days" reminder sweep is
deferred with notifications; noted so a later phase has a home for it.) The module
therefore does NOT touch `register_worker_job_types` or the gateway registration.

---

## 10. Frontend

Feature folder `frontend/src/modules/change/`:

- **ChangesPage** — list + filter by state/type; a "New change" control
  (`change.request`).
- **ChangeRequestForm** — create/edit a draft (type, risk, affected assets,
  window, rollback plan).
- **ApprovalQueue** — submitted changes awaiting decision; approve/reject with a
  comment (`change.approve`-gated; hidden without it). The SoD rule surfaces as a
  disabled approve control on one's own change (cosmetic; backend enforces).
- **ChangeDetail** — the change + a **timeline** of approvals (decision history)
  and linked actions; link/unlink actions (`change.manage`/owner); evidence
  export button (`change.read`).
- **ChangeCalendar** — window view with conflict highlights.
- `changeState.ts` — state→badge maps keyed on the **real backend vocabulary**
  (`draft`/`submitted`/`approved`/`implementing`/`completed`/`rejected`/
  `cancelled`) — no invented names. TanStack Query with query-key invalidation
  after mutations (no live event). Strict TS, no `any`.

---

## 11. Testing

- **State machine** — every legal transition succeeds and every illegal one
  raises `ChangeValidationError` (table-driven); the pure transition function is
  unit-tested exhaustively.
- **SoD** — a non-emergency change cannot be approved by its `created_by`
  (raises); a different approver succeeds. Emergency retrospective approval by the
  requester is rejected.
- **Emergency fast-path** — `submitted → implementing` allowed for emergency;
  `completed` is blocked until a retrospective `approved` decision exists; a
  retrospectively-rejected emergency cannot reach `completed`.
- **Advisory linking** — an org-scoped patch_run/deployment/provision_run/
  patch_approval links successfully; a cross-org or nonexistent action id is
  rejected; a duplicate link is rejected; linking does NOT change the linked
  action's state (advisory).
- **Calendar** — overlapping windows sharing an affected asset are flagged;
  non-overlapping or disjoint-asset windows are not.
- **Evidence export** — contains the change, the full approval history, the
  links, and the audit-event ids; excludes another org's data.
- **Odoo port** — the `NullChangeTicketAdapter` is invoked at the pinned call
  sites and writes no `change_external_ref`; the workflow succeeds with the null
  backend.
- **Tenancy** — every query/route excludes another org's changes/links/approvals.
- **RBAC** — 403 for under-privileged roles on `approve`/`manage` routes;
  technician can `request` but not `approve`.
- **Audit** — each privileged action writes exactly one hash-chained audit event
  with the correct actor; the evidence `audit_refs` resolve to real events.

---

## 12. Deploy notes

`deploy/README.md` + `.env.example` document: the change-management tables are
control-plane only (no new compose services, no worker sweeps); the
`ITS_CHANGE_TICKET_BACKEND` config (default `null`) and the **Odoo Helpdesk
adapter contract seam** (one-way push, vault-sealed token when a real adapter
lands); and that change-action linking is **advisory** (records evidence, does not
gate actions) in v1. No new required env vars beyond the ticket-backend selector.

---

## 13. Deferred / later phases

- The real **Odoo JSON-RPC adapter** (vault-sealed token) + two-way sync.
- **Change-gating for provisioning.** Patch approvals and deployments are gated
  (§14); provisioning runs are not.
- **Notifications / reminders** (approval-request notices, emergency
  retrospective-approval overdue) once a notification mechanism exists.
- CAB workflows; auto-change-creation from risky operations; inventory/Sentinel
  risk scoring; BCP re-test triggers on major changes.

## 14. Change gating and automatic change records (2026-09-23)

The review found change management gated nothing. Now:

- **`change_gates`** (migration 0122): per org, a device group, or no group for
  every device, with `applies_to_patches` / `applies_to_deployments`. Managed
  under `change.gate.manage` (org-admin, super-admin) at
  `GET|PUT /changes/gates`, `DELETE /changes/gates/{id}`; each change audited
  `change.gate_set` / `change.gate_removed`.
- **A change authorizes** when it is `approved` or `implementing` and linked to
  the action (`change_link`: `patch_approval` or `deployment`).
- **Enforced when requested.** `POST /patch/approvals[/bulk]` and
  `POST /deploy/deployments` take an optional `change_id`. With one, the change
  must authorize (422 otherwise, 404 if unknown) and every approval or the
  deployment is linked to it. Without one, a gate covering the machines (the
  ring's population for an approval; the target device's group, or the target
  group, for a deployment) refuses with 409 and a sentence naming the group,
  and `change.gate_refused` is committed to the audit log.
- **Enforced again at dispatch**, because a change can be cancelled in between.
  `dispatch_apply` withholds, per device, approved patches whose approval has no
  authorizing change when the device's group is gated;
  `dispatch_deployment` (the execute route and the window sweep) refuses a
  gated deployment with no authorizing change, and `POST
  /deployments/{id}/execute` answers 409. Both audit `change.gate_refused`.
- **Automatic change records.** With no gate and no `change_id`, a human
  approval that created approvals, or a deployment, gets a `completed`
  `standard` change with `origin = 'auto'`, the actor as requester, no approval
  row (none was given), linked to what was done, audited
  `change.auto_recorded`. It is not pushed to the ticket system: one per patch
  decision would flood the service desk. System-initiated approvals (PD-02
  auto-approve) have no person to name as requester and get none.
- **Console.** The Changes page lists the gates and lets an org admin add or
  remove one in words ("Every device" / a group; patch approvals, deployments).
  The patch decision bar and the new-deployment dialog offer the approved
  changes to link; a refusal shows the server's sentence. Automatic records read
  "Recorded automatically".

