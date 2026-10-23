# Spec — BCP / DR Test Reports (Phase 10)

> Full spec, expanded from `docs/modules/bcp.md` via the brainstorming skill.
> Scope decision (approved): **control-plane-first, manual test recording in v1**.
> Build the plan repository, manual test execution records, backup restore-test
> evidence linkage, findings + remediation with change-record references, and SOC 2
> test reports. **Automated DR drills** (reusing the backup module's
> `backup.restore` capability), **notifications/overdue alerting**, and **plan
> sign-off segregation-of-duties** are deferred. This is the last of the original
> ten modules; it mirrors the `change_mgmt` skeleton (control-plane-only, no agent
> capability).

---

## 1. Purpose & scope

Manage IT business-continuity / disaster-recovery **plans**, record **tests** of
those plans, and produce **test reports** — turning DR from a document that rots
into a tracked, evidenced practice. A **SOC 2 evidence engine** (BCP/DR testing is
a common audit requirement). The unfair advantage: the backup module already
proves recovery works (`restore_tested_at`), so a DR test can attach **hard
restore-proven evidence**, not just an assertion that backups exist.

**v1 delivers (real, tested in this repo):**

- **BCP/DR plans** — scope (covered assets), RTO/RPO targets, recovery-procedure
  references to documentation runbooks, dependencies, and a **review cadence**
  with a recorded sign-off.
- **Manual test execution records** — who ran a test, when, the steps performed,
  the outcome vs RTO/RPO, and a **point-in-time backup-evidence snapshot** for the
  plan's covered assets.
- **Findings & remediation** — issues found in a test, with severity, a
  remediation state, and an org-scoped reference to a **Change record** (Phase 9).
- **SOC 2 test reports** — structured per-test and per-period reports (pass/fail
  vs objectives, backup-evidence rollup, remediation items).
- **Review-cadence flagging** — a **computed, report-only** "overdue review" flag.

**Explicitly deferred (later phases):** automated DR drills (trigger a test
restore via `backup.restore` and verify); notifications / overdue-review alerting
(no notification mechanism exists platform-wide yet); plan-sign-off
segregation-of-duties; dependency/impact mapping; continuity trend dashboards.

Control-plane only — **no agent capability, no worker sweeps, no gateway
registration**. The module reads evidence from other modules and records human
activity. New module `bcp`, standard skeleton, migration `0017`.

---

## 2. Data model

Additive Alembic revision **`0017`** after Phase 9's head (`0016`), `_rls` helper
applied to all four tables (RLS `ENABLE + FORCE` + `tenant_isolation` policy
`USING (org_id = current_setting('app.current_org', true)::uuid)`), plus the
`bcp.*` permission seed + role grants (§8).

- **`bcp_plan`** — `id` (uuid7 pk), `org_id` FK, `name`, `description` (Text),
  `scope` JSONB (list of `{kind: device|group, id}` covered-asset refs, mirroring
  `change_request.affected`), `rto_minutes` (Integer, nullable), `rpo_minutes`
  (Integer, nullable), `procedure_refs` JSONB (list of `doc_articles.id` UUIDs —
  recovery runbooks), `dependencies` JSONB (free-form list), `review_interval_days`
  (Integer, default 365), `last_reviewed_at` (timestamptz, nullable),
  `approved_by` (FK→users, nullable), `state` (active|archived, default `active`),
  `created_by` FK→users, `created_at`. Index `(org_id, state)`.
- **`bcp_test`** — `id`, `org_id` FK, `plan_id` FK→bcp_plan, `type`
  (restore_drill|failover|tabletop|other), `scheduled_for` (timestamptz,
  nullable), `executed_at` (timestamptz, nullable), `executed_by` (FK→users,
  nullable), `outcome` (pending|pass|partial|fail, default `pending`), `rto_met`
  (Boolean, nullable), `rpo_met` (Boolean, nullable), `evidence` JSONB (default
  `{}` — the point-in-time backup-evidence snapshot captured at record time, §6),
  `notes` (Text), `created_by` FK→users, `created_at`. Index `(org_id, plan_id)`.
- **`bcp_test_step`** — `id`, `org_id` FK, `test_id` FK→bcp_test, `ordinal`
  (Integer — step order), `description` (Text), `result` (pending|pass|fail,
  default `pending`), `evidence_ref` (Text, nullable — free-text or object-store
  ref). Index `(org_id, test_id)`.
- **`bcp_finding`** — `id`, `org_id` FK, `test_id` FK→bcp_test, `description`
  (Text), `severity` (low|medium|high|critical), `remediation_state`
  (open|in_progress|resolved, default `open`), `change_ref` (UUID, nullable —
  org-scoped reference to a `change_request.id`, §5; no cross-table DB FK),
  `created_by` FK→users, `created_at`, `resolved_at` (timestamptz, nullable).
  Index `(org_id, test_id)`, `(org_id, remediation_state)`.

**Vocabularies (verbatim):** `bcp_plan.state` ∈ {active, archived};
`bcp_test.type` ∈ {restore_drill, failover, tabletop, other}; `bcp_test.outcome`
∈ {pending, pass, partial, fail}; `bcp_test_step.result` ∈ {pending, pass, fail};
`bcp_finding.severity` ∈ {low, medium, high, critical};
`bcp_finding.remediation_state` ∈ {open, in_progress, resolved}.

---

## 3. Plans & review cadence

- **CRUD** (`bcp.manage`): create/edit/archive a plan — scope, RTO/RPO,
  procedure refs (validated org-scoped against `doc_articles`), dependencies,
  review interval. `scope` covered-asset refs are validated org-scoped against
  `devices`/`device_groups` at write time (a cross-org or nonexistent asset id is
  rejected — the same org-scoped resolution guard the change-link module uses).
- **Review sign-off** (`bcp.approve`): `POST /plans/{id}/review` records a review —
  sets `last_reviewed_at = now`, `approved_by = actor`, audits `bcp.plan_reviewed`.
  v1 records the sign-off without a segregation-of-duties rule (a plan's author
  MAY record its review); SoD is a pinned later addition — noted so a future phase
  can gate it, mirroring change management's approver≠requester rule.
- **Overdue flag (report-only, computed):** a plan is `review_overdue` when
  `last_reviewed_at IS NULL` OR `last_reviewed_at + review_interval_days < now`.
  This is computed in the reporting/read layer and surfaced in the UI and reports.
  There is **no worker sweep and no notification** in v1 — the flag is derived on
  read (the plan list / dashboard shows it), consistent with the absence of a
  platform notification mechanism.

---

## 4. Tests & execution (manual)

- **Schedule / record a test** (`bcp.manage`): create a `bcp_test` for a plan
  (org-scoped plan existence checked). A test may be scheduled (`scheduled_for`
  set, `outcome=pending`) and later executed, or recorded after the fact.
- **Record execution** (`bcp.manage`): set `executed_at`, `executed_by`, per-step
  `bcp_test_step` results, `rto_met`/`rpo_met`, `outcome`, and `notes`. At the
  moment of recording execution, **capture the backup-evidence snapshot** (§6)
  into `bcp_test.evidence` — freezing the restore-proven status of the plan's
  covered assets as-of the test, so the report is durable point-in-time evidence
  rather than a live lookup that drifts.
- **Outcome semantics:** `pass` = objectives met and all steps pass; `partial` =
  objectives partially met / some step failed but recovery achieved; `fail` =
  recovery objective not met. `rto_met`/`rpo_met` are recorded explicitly (the
  operator's assessment against the plan's targets). The state engine here is
  simple (`pending → pass|partial|fail`); a test is a record of execution, not a
  multi-party approval workflow — no complex state machine like change management.
- Every create/execute/edit writes a hash-chained audit event (`target_type=
  "bcp_test"`, actor threaded).

---

## 5. Findings & remediation

- **Findings** (`bcp.manage`): a test records zero or more `bcp_finding` rows —
  description, severity, `remediation_state` (default `open`).
- **Change linkage (org-scoped reference):** a finding may reference a
  `change_request.id` via `change_ref`. Setting it **validates the change exists
  in this org** (`select(ChangeRequest.id).where(org_id==, id==change_ref)`; a
  cross-org or nonexistent id → validation error — the same cross-org-safe
  resolution guard `change_mgmt.links` uses). The reference is **advisory** — it
  does not alter the change. Remediation is tracked here; the linked change is
  where the fix is actually governed.
- **Remediation state** transitions `open → in_progress → resolved`; setting
  `resolved` stamps `resolved_at`. Each change audits `bcp.finding_updated`.

---

## 6. Backup restore-test evidence integration (read-only)

The core differentiator. The backup module already exposes a BCP-purposed read
surface (`app/modules/backup/reporting.py`), used **read-only** — BCP writes
nothing to the backup module:

- `backup_evidence_rows(session, org_id) -> list[dict]` — per-device
  `{device_id, hostname, covered, last_backup_taken_at, verified_at,
  restore_tested_at, restore_proven}`.
- `device_backup_status(session, org_id, device_id) -> dict` — per-device detail.

**Evidence snapshot at test record time:** when a test's execution is recorded,
BCP resolves the plan's `scope` to a concrete device set (devices + expanded
groups, org-scoped), reads `backup_evidence_rows` (filtered to those devices), and
stores the result in `bcp_test.evidence` as a frozen snapshot:
`{captured_at, devices: [{device_id, hostname, covered, restore_tested_at,
restore_proven, verified_at, last_backup_taken_at} …], summary: {covered_n,
restore_proven_n, total_n}}`. The report (§7) presents this as the DR proof
associated with that test — "as of this test, N of M covered devices had a
proven restore." No secret/key material is involved (backup evidence is
metadata only).

---

## 7. Reports (SOC 2 evidence)

Read-only aggregation, org-scoped, no writes (the export-audit event, if any, is
written by the API route, not the reporting layer — mirroring change management):

- **`bcp_test_report(session, org_id, test_id) -> dict`** — one test's full
  record: the plan (name/RTO/RPO/scope), the test (type/executed_at/by/outcome/
  rto_met/rpo_met/notes), its steps + results, its findings + remediation states +
  change refs, and the frozen `evidence` snapshot. This is the per-test SOC 2
  artifact.
- **`bcp_period_report(session, org_id, *, frm, to) -> dict`** — all tests executed
  in `[frm, to]` with pass/partial/fail counts, per-plan coverage (which plans were
  tested vs not), open-findings rollup by severity, and the review-overdue plan
  list. The per-period audit-evidence summary.
- **`bcp_plan_status(session, org_id) -> list[dict]`** — per-plan: last test +
  outcome, last_reviewed_at, `review_overdue` flag, open-findings count, current
  backup restore-proven coverage for the plan's assets. The dashboard/list read.

Reports are CSV/JSON exportable at the API layer.

---

## 8. Security invariants & RBAC

1. **Signed jobs only** — this module dispatches no agent jobs; the future
   automated-drill path would reuse the backup module's already-signed
   `backup.restore`. No new unsigned path.
2. **Outbound-only mTLS** — no agent; N/A.
3. **Secrets vault-sealed** — no credentials in this module; backup evidence is
   metadata only (no keys/chunks). N/A.
4. **Hash-chained audit** — EVERY privileged action writes the immutable audit
   log with correct actor: plan created/edited/archived, plan reviewed, test
   created/executed/edited, finding created/updated, evidence exported. All
   `actor_type="user"`, `actor_id=ctx.user_id`. No secret in metadata. Change
   audit events set a consistent `target_type` (`bcp_plan`/`bcp_test`/
   `bcp_finding`) + `target_id` for evidence resolution.
5. **Tenancy** — RLS `ENABLE + FORCE` + explicit `org_id` predicate on every
   query over the four `bcp_*` tables AND the cross-module reads
   (`change_request`, `devices`/`device_groups`, `doc_articles`, backup evidence
   rows — the backup reporting fns are already org-scoped). A cross-org asset/
   change/doc reference is rejected at write time by the org-scoped resolution
   guard. A missing tenant filter is a bug.
6. **RBAC** — server-side `require_permission` on every route. New permissions:
   - `bcp.read` — view plans, tests, findings, reports, evidence export.
   - `bcp.manage` — create/edit plans, record tests + steps, manage findings.
   - `bcp.approve` — record a plan review sign-off.
   Grants: super-admin/org-admin → all three; technician → `bcp.read` +
   `bcp.manage`; auditor/read-only → `bcp.read`. (`bcp.approve` sits with
   org-admin — a review sign-off is a management-level attestation.)

No agent idle-budget impact (control-plane only). The permissions are registered
in BOTH the migration seed AND `app/core/rbac.py` `PERMISSIONS` (the standard
registry — the Phase-7 lesson).

---

## 9. API

Routes under `/api/v1/bcp`, each RBAC-gated; `org_id`/`actor_id` from
`AuthContext`, never the request body:

- `GET /plans` (`read`), `POST /plans` (`manage`), `GET /plans/{id}` (`read`),
  `PATCH /plans/{id}` (`manage`), `POST /plans/{id}/review` (`approve`).
- `GET /plans/{id}/tests` (`read`), `POST /plans/{id}/tests` (`manage`),
  `GET /tests/{id}` (`read`), `PATCH /tests/{id}` (`manage` — record execution +
  captures the evidence snapshot), test-step create/edit (`manage`).
- `GET /tests/{id}/findings` (`read`), `POST /tests/{id}/findings` (`manage`),
  `PATCH /findings/{id}` (`manage` — remediation state + change_ref).
- `GET /tests/{id}/report` (`read`), `GET /report/period` (`read`, `from`/`to`),
  `GET /plans/status` (`read`).

Response models carry no secret fields (there are none). `ChangeValidationError`-
style validation errors → 400, not-found → 404 (use a typed `BcpNotFoundError`
subclass for reliable 404 routing, the Phase-9 lesson), never 500. `register(app)`
mounts the router ONLY — NO job-type registration, NO gateway edit.

---

## 10. Worker

**None.** No sweeps, no jobs, no beat entries. The review-overdue flag is computed
on read (§3); a future notification/reminder sweep is deferred with the
notification workstream. The module does NOT touch `register_worker_job_types` or
`app/gateway/main.py`.

---

## 11. Frontend

Feature folder `frontend/src/modules/bcp/`:

- **PlansPage** — list plans with `review_overdue` badges + last-test outcome; a
  "New plan" control (`bcp.manage`).
- **PlanEditor / PlanDetail** — scope (asset picker), RTO/RPO, procedure refs,
  dependencies, review interval; a "Record review" action (`bcp.approve`, hidden
  without it); the plan's tests + findings.
- **TestRecorder** — record a test: type, steps + results, RTO/RPO met, outcome,
  notes; on save the backend captures the backup-evidence snapshot, shown as the
  DR proof.
- **FindingsList** — findings with severity + remediation state; link a finding to
  a Change record (`bcp.manage`).
- **BcpReport** — the per-test and per-period SOC 2 report views with export.
- `bcpState.ts` — badge maps keyed on the **real backend vocabularies**
  (`outcome` pending/pass/partial/fail; `remediation_state` open/in_progress/
  resolved; `severity`) — no invented names. TanStack Query, query-key
  invalidation after mutations (no live event). Strict TS, no `any`.

---

## 12. Testing

- **Plan CRUD + scope validation** — a plan's `scope`/`procedure_refs` reject a
  cross-org or nonexistent device/group/doc id (org-scoped resolution).
- **Review sign-off** — `POST /plans/{id}/review` sets `last_reviewed_at`/
  `approved_by` + audits; the `review_overdue` computed flag is true when unreviewed
  or past the interval, false when freshly reviewed (test with fixed timestamps).
- **Test recording + evidence snapshot** — recording a test's execution captures a
  `bcp_test.evidence` snapshot reflecting the backup restore-tested status of the
  plan's covered devices at that time; a device with `restore_tested_at` set shows
  `restore_proven=true`, one without shows false; the snapshot is frozen (a later
  backup change does not alter a recorded test's evidence).
- **Findings + change linkage** — a finding links an org change_request; a
  cross-org/nonexistent change_ref is rejected; remediation `open→in_progress→
  resolved` stamps `resolved_at`.
- **Reports** — `bcp_test_report` contains plan+test+steps+findings+evidence;
  `bcp_period_report` counts outcomes and rolls up open findings + overdue plans
  against hand-computed values; tenancy — another org's plans/tests/findings never
  appear.
- **RBAC** — 403 for under-privileged roles on `manage`/`approve` routes;
  technician can `manage` but not `approve`.
- **Tenancy** — every query/route excludes another org's data, including the
  cross-module reads (change/doc/device/backup-evidence).
- **Audit** — each privileged action writes one hash-chained event with the
  correct actor/target; a plain report VIEW does not write an export-audit unless
  it is the explicit export route (the Phase-9 no-audit-on-view lesson).

---

## 13. Deploy notes

`deploy/README.md` + `.env.example` document: BCP is **control-plane only** (no
agent capability, no worker sweeps, no new compose services, no new required env
vars); it reads restore-test evidence from the backup module (metadata only); the
review-overdue flag is computed on read (no notification mechanism in v1); and
findings link advisorily to Change records.

---

## 14. Deferred / later phases

- **Automated DR drills** — trigger a test restore via the backup module's
  `backup.restore` capability and verify RTO/RPO automatically (the only
  agent-touching extension; reuses an existing signed capability).
- **Notifications / overdue-review alerting** once a platform notification
  mechanism exists (the flag is report-only until then).
- **Plan-sign-off segregation-of-duties** (author≠approver), mirroring change
  management's rule.
- Dependency/impact mapping across services; continuity dashboards (coverage,
  last-tested, RTO/RPO attainment trends).
